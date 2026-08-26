#!/usr/bin/env python3
"""Benchmark every local model in config/models.yaml THROUGH Guardian.

Why through Guardian (not standalone llama-server): we want to measure the real
production config — the exact entry in models.yaml (path, ngl, kv_type,
tensor_split, mmproj, extra_args) as Guardian loads and serves it, including the
model-switch / auto-unload behaviour of the scheduler. A standalone llama-server
launch would bypass all of that.

For each model entry under `models:` in config/models.yaml (skipping embedding
models and the `aliases:` mapping):
  1. POST /v1/chat/completions (stream=True) to Guardian with a fixed prompt.
  2. First request includes model load + switch time — measured separately as
     `load_switch_s` (time to first token on the first run).
  3. Run N times (default 3); measure time-to-first-token (ttft), generation
     tokens/s, prompt eval t/s, wall-clock. Take median of the steady-state runs.
  4. Append a row to docs/MODEL_BENCHMARKS.md (markdown table).

Resumable: a state file data/bench-models/state.json records completed models;
re-running skips them. Pass --reset to start over.

Usage:
  GUARDIAN_KEY=flip_... ./scripts/bench_all_models.py
  GUARDIAN_KEY=flip_... ./scripts/bench_all_models.py --only granite-4.1-8b,qwen3.8-27b
  GUARDIAN_KEY=flip_... ./scripts/bench_all_models.py --reset

Auth: the dashboard read-only key works (flip_168c...). Pass via GUARDIAN_KEY
env or --key. Endpoint defaults to http://192.168.1.35:11434/v1 (LAN nginx) —
override with GUARDIAN_BASE_URL for 127.0.0.1:11435 (TLS) etc.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.paths import local_models_file

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = local_models_file()
OUT_DIR = REPO_ROOT / "data" / "bench-models"
STATE_FILE = OUT_DIR / "state.json"
DOC_FILE = REPO_ROOT / "docs" / "MODEL_BENCHMARKS.md"

DEFAULT_BASE_URL = "http://192.168.1.35:11434/v1"
DEFAULT_N_PREDICT = 256
DEFAULT_RUNS = 3
DEFAULT_TIMEOUT_S = 900  # 15 min per request — allows slow model loads

# Fixed deterministic prompt (~short, same for every model). Instruction is
# neutral so any instruct/chat model can answer; warm-up uses n_predict=32.
PROMPT = (
    "Explain in three sentences how a lighthouse keeper would log tidal "
    "patterns, lantern rotations, and shipping lanes in a coastal ledger. "
    "Be concise and practical."
)

# Headers we want to strip from the stream (none needed; we parse SSE directly).
STREAM_MEDIA = "text/event-stream"


# ───────────────────────── helpers ─────────────────────────


def load_model_entries() -> list[dict[str, Any]]:
    """Return the canonical (non-alias, non-embedding) model entries."""
    data = yaml.safe_load(MODELS_YAML.read_text())
    models = data.get("models", {}) or {}
    entries: list[dict[str, Any]] = []
    for name, cfg in models.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("model_type") == "embedding":
            continue
        entries.append({"name": name, "config": cfg})
    return entries


def guardian_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def iter_sse_events(resp: httpx.Response):
    """Yield parsed SSE events (dicts) from a streaming httpx response."""
    event_type = None
    data_lines: list[str] = []
    for raw in resp.iter_lines():
        if raw is None:
            continue
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if line == "":
            if data_lines:
                data_str = "\n".join(data_lines)
                if data_str:
                    try:
                        payload = json.loads(data_str)
                    except json.JSONDecodeError:
                        payload = {"_raw": data_str}
                    yield {"event": event_type, "data": payload}
            event_type = None
            data_lines = []
        elif line.startswith("event: "):
            event_type = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: ") :])
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :])


def benchmark_one(
    base_url: str,
    headers: dict[str, str],
    model: str,
    n_predict: int,
    runs: int,
    is_warmup: bool = False,
) -> dict[str, Any]:
    """Run one streaming chat completion; return timings dict.

    Measures from the client side:
      wall_s            — total wall-clock for the whole streaming response
      ttft_s            — time-to-first-token (first data chunk with content)
      tokens_generated  — completion_tokens (from usage or counted from deltas)
      gen_tps           — tokens_generated / (wall_s - ttft_s)  [steady-state gen]
      prompt_tokens     — from usage field
      prompt_eval_tps   — prompt_tokens / ttft_s  [rough prompt processing rate]
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": n_predict if not is_warmup else 32,
        "temperature": 0.0,
        "seed": 42,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.monotonic()
    ttft: float | None = None
    content_chunks = 0
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    with httpx.Client(timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=30.0), follow_redirects=True) as client:
        with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", errors="replace")[:500]
                return {
                    "error": f"HTTP {resp.status_code}: {body}",
                    "wall_s": time.monotonic() - t0,
                }
            for ev in iter_sse_events(resp):
                data = ev.get("data") or {}
                if data == "[DONE]":
                    break
                choices = data.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        if ttft is None:
                            ttft = time.monotonic() - t0
                        content_chunks += 1
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                if data.get("usage"):
                    usage = data["usage"]
    wall = time.monotonic() - t0
    completion_tokens = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or content_chunks
    )
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    gen_duration = (wall - ttft) if ttft else wall
    gen_tps = (completion_tokens / gen_duration) if gen_duration > 0 else 0.0
    return {
        "wall_s": round(wall, 3),
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "tokens_generated": completion_tokens,
        "gen_tps": round(gen_tps, 2),
        "prompt_tokens": prompt_tokens,
        "prompt_eval_tps": round(prompt_tokens / ttft, 1) if ttft else None,
        "finish_reason": finish_reason,
    }


def median(values: list[float]) -> float:
    s = sorted(v for v in values if v is not None)
    if not s:
        return 0.0
    return s[len(s) // 2]


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed": {}, "started_at": None}


def save_state(state: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def write_doc(results: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Write/refresh docs/MODEL_BENCHMARKS.md from the full state."""
    by_name = {e["name"]: e["config"] for e in entries}
    lines: list[str] = []
    lines.append("# Guardian Model Benchmarks")
    lines.append("")
    lines.append(
        "Speed measurements for every local model in `config/models.yaml` served "
        "through Guardian (`/v1/chat/completions`, streaming). Each model was "
        "loaded on demand via Guardian's model-switch path, warmed up once, then "
        "measured over N runs. Generation speed (`gen_tps`) excludes the "
        "time-to-first-token; `load_switch_s` is the first-run wall-clock "
        "including model load + KV cache init."
    )
    lines.append("")
    lines.append(f"- **Date:** {results.get('finished_at', datetime.now(timezone.utc).isoformat())}")
    lines.append(f"- **Endpoint:** `{DEFAULT_BASE_URL}`")
    n_predict = next(iter(results['completed'].values())).get('n_predict') if results['completed'] else DEFAULT_N_PREDICT
    lines.append("- **Prompt tokens (approx):** varies per model tokenizer")
    lines.append(f"- **Max tokens per run:** {n_predict}")
    lines.append(f"- **Runs per model:** {DEFAULT_RUNS} (median reported)")
    lines.append(f"- **Prompt:** `{PROMPT[:80]}...`")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("Sorted by generation speed (fastest first). Failed and pending models are listed at the bottom.")
    lines.append("")
    lines.append(
        "| Model | KV type | ngl | load+switch (s) | TTFT (s) | gen tok/s | prompt eval tok/s | status |"
    )
    lines.append(
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |"
    )
    # Sort the table by generation speed (fastest first); failed models
    # next (alphabetical), pending last. The doc's purpose is a speed ranking.
    def _table_sort_key(name: str) -> tuple:
        r = results["completed"].get(name)
        if r and "error" not in r:
            try:
                return (0, -float(r.get("gen_tps") or 0.0), name)
            except (TypeError, ValueError):
                return (0, 0.0, name)
        if r and "error" in r:
            return (1, 0.0, name)
        return (2, 0.0, name)
    # Iterate over the UNION of configured models and all completed benchmarks
    # in state.json. This ensures `--only` runs don't erase other models' results
    # from the doc — every benchmarked model is always shown, plus any configured-
    # but-unbenchmarked model shows as pending.
    all_names = set(by_name.keys()) | set(results.get("completed", {}).keys())
    for name in sorted(all_names, key=_table_sort_key):
        r = results["completed"].get(name)
        if not r:
            lines.append(f"| `{name}` | — | — | — | — | — | — | pending |")
            continue
        cfg = by_name.get(name, {})
        kv = cfg.get("kv_type", "—")
        ngl = cfg.get("ngl", "—")
        if "error" in r:
            err = r["error"]
            # Classificeer de error tot een korte, leesbare statusregel.
            # De volledige foutmelding staat in state.json voor debuggen.
            err_lower = err.lower()
            # Specifieke GGUF-corruptie-check eerst (Ornith bevat "geen OOM" in
            # de diagnose, dus de brede OOM-check mag niet oordeelvoerend zijn).
            if "gguf" in err_lower and ("corrupt" in err_lower or "laad-fout" in err_lower or "failed to load model" in err_lower):
                short = "GGUF laad-fout (corrupt/truncated?)"
            elif (
                "out of memory" in err_lower
                or "cudamalloc failed" in err_lower
                or err_lower.startswith("oom")
                or "(oom" in err_lower
            ):
                short = "OOM (KV-cache / VRAM)"
            elif "failed to load model" in err_lower or "failed to load" in err_lower:
                short = "load failed (GGUF/config)"
            elif "crash" in err_lower or "exitin" in err_lower:
                short = "crash on load"
            else:
                import re as _re
                m = _re.search(r"HTTP (\d{3})", err)
                prefix = f"HTTP {m.group(1)}: " if m else ""
                short = prefix + err.split("`")[0][:50].strip()
            lines.append(
                f"| `{name}` | {kv} | {ngl} | — | — | — | — | ❌ {short} |"
            )
            continue
        lines.append(
            f"| `{name}` | {kv} | {ngl} | "
            f"{r.get('load_switch_s', '—')} | "
            f"{r.get('ttft_s', '—')} | "
            f"{r.get('gen_tps', '—')} | "
            f"{r.get('prompt_eval_tps', '—')} | "
            f"✅ |"
        )
    lines.append("")
    lines.append("## Per-model detail")
    lines.append("")
    for name in sorted(set(by_name.keys()) | set(results.get("completed", {}).keys())):
        r = results["completed"].get(name)
        if not r:
            # pending (never attempted) — still list config
            lines.append(f"### `{name}`")
            lines.append("")
            lines.append(f"- path: `{by_name.get(name, {}).get('path', '—')}`")
            lines.append(f"- ngl: {by_name.get(name, {}).get('ngl', '—')}, kv_type: `{by_name.get(name, {}).get('kv_type', 'f16')}`")
            lines.append("- status: **pending (not benchmarked this run)**")
            lines.append("")
            continue
        if "error" in r:
            # failed — document config + diagnosis so the failure is actionable
            lines.append(f"### `{name}`")
            lines.append("")
            lines.append(f"- path: `{by_name.get(name, {}).get('path', '—')}`")
            lines.append(f"- ngl: {by_name.get(name, {}).get('ngl', '—')}, kv_type: `{by_name.get(name, {}).get('kv_type', 'f16')}`")
            lines.append(f"- **status: FAILED — {r['error']}**")
            lines.append("")
            continue
        lines.append(f"### `{name}`")
        lines.append("")
        lines.append(f"- path: `{by_name.get(name, {}).get('path', '—')}`")
        lines.append(f"- mmproj: `{by_name.get(name, {}).get('mmproj', '—')}`" if by_name.get(name, {}).get("mmproj") else "- mmproj: none")
        lines.append(f"- ngl: {by_name.get(name, {}).get('ngl', '—')}, tensor_split: `{by_name.get(name, {}).get('tensor_split', '—')}`, kv_type: `{by_name.get(name, {}).get('kv_type', 'f16')}`")
        lines.append(f"- extra_args: `{by_name.get(name, {}).get('extra_args', '—')}`")
        lines.append(f"- load+switch (first run): **{r.get('load_switch_s', '—')} s**")
        lines.append(f"- median TTFT: **{r.get('ttft_s', '—')} s**")
        lines.append(f"- median gen speed: **{r.get('gen_tps', '—')} tok/s**")
        lines.append(f"- median prompt eval: **{r.get('prompt_eval_tps', '—')} tok/s**")
        if r.get("runs"):
            lines.append(f"- all runs gen tok/s: {', '.join(str(x.get('gen_tps')) for x in r['runs'])}")
        lines.append("")
    DOC_FILE.write_text("\n".join(lines))


# ───────────────────────── main ─────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=os.getenv("GUARDIAN_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--key", default=os.getenv("GUARDIAN_KEY"))
    ap.add_argument("--n-predict", type=int, default=DEFAULT_N_PREDICT)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--only", default="", help="comma-separated model names to benchmark (skip rest)")
    ap.add_argument("--reset", action="store_true", help="discard prior state and start over")
    args = ap.parse_args()

    if not args.key:
        print("ERROR: set GUARDIAN_KEY or pass --key", file=sys.stderr)
        return 2

    all_entries = load_model_entries()
    entries = all_entries
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        entries = [e for e in entries if e["name"] in wanted]
    if not entries:
        print("No models to benchmark.", file=sys.stderr)
        return 1

    names = [e["name"] for e in entries]
    print(f"Models to benchmark ({len(names)}): {', '.join(names)}", flush=True)

    state: dict[str, Any] = {"completed": {}, "started_at": datetime.now(timezone.utc).isoformat()} if args.reset else load_state()
    state.setdefault("completed", {})
    if not state.get("started_at"):
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    if args.reset:
        state["completed"] = {}

    headers = guardian_headers(args.key)
    # Quick auth + reachability check.
    try:
        r = httpx.get(f"{args.base_url}/models", headers=headers, timeout=30.0)
        if r.status_code == 401:
            print("ERROR: auth failed (401) — key invalid", file=sys.stderr)
            return 2
        r.raise_for_status()
    except Exception as exc:
        print(f"ERROR: cannot reach Guardian at {args.base_url}: {exc}", file=sys.stderr)
        return 2

    for entry in entries:
        name = entry["name"]
        if name in state["completed"] and not args.reset:
            print(f"--- SKIP {name} (already benchmarked) ---", flush=True)
            continue
        print(f"\n=== {name} ===", flush=True)
        cfg = entry["config"]
        print(f"  path: {cfg.get('path')}", flush=True)
        print(f"  ngl={cfg.get('ngl')} kv={cfg.get('kv_type','f16')} ts={cfg.get('tensor_split','—')}", flush=True)
        run_results: list[dict[str, Any]] = []
        load_switch_s: float | None = None
        errored = False
        for i in range(args.runs + 1):  # +1 warmup
            is_warmup = i == 0
            label = "warmup" if is_warmup else f"run {i}/{args.runs}"
            print(f"  [{label}] sending stream...", flush=True, end="")
            try:
                res = benchmark_one(
                    args.base_url, headers, name,
                    n_predict=args.n_predict,
                    runs=1,
                    is_warmup=is_warmup,
                )
            except Exception as exc:
                print(f" EXCEPTION: {exc}", flush=True)
                state["completed"][name] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "n_predict": args.n_predict,
                }
                save_state(state)
                write_doc(state, all_entries)
                errored = True
                break
            if "error" in res:
                print(f" ERROR: {res['error'][:120]}", flush=True)
                state["completed"][name] = {
                    "error": res["error"][:200],
                    "n_predict": args.n_predict,
                }
                save_state(state)
                write_doc(state, all_entries)
                errored = True
                break
            print(
                f" ttft={res['ttft_s']}s gen={res['gen_tps']} tok/s "
                f"toks={res['tokens_generated']} wall={res['wall_s']}s fr={res['finish_reason']}",
                flush=True,
            )
            if is_warmup:
                load_switch_s = res["wall_s"]
                continue
            run_results.append(res)
        if errored:
            continue
        # Medians of steady-state runs.
        ttft_med = median([r.get("ttft_s") or 0 for r in run_results])
        gen_med = median([r.get("gen_tps") or 0 for r in run_results])
        peval_med = median([r.get("prompt_eval_tps") or 0 for r in run_results])
        state["completed"][name] = {
            "load_switch_s": round(load_switch_s, 3) if load_switch_s else None,
            "ttft_s": round(ttft_med, 3),
            "gen_tps": round(gen_med, 2),
            "prompt_eval_tps": round(peval_med, 1),
            "n_predict": args.n_predict,
            "runs": run_results,
        }
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        write_doc(state, all_entries)
        # Let Guardian unload / release VRAM before the next model.
        print("  waiting 20s for VRAM release before next model...", flush=True)
        time.sleep(20)

    write_doc(state, all_entries)
    print(f"\n=== DONE — results in {DOC_FILE} ===", flush=True)
    print(f"State: {STATE_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
