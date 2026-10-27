#!/usr/bin/env python3
"""G0 baseline measurement harness (read-only against the running Guardian).

Produces the numbers recorded in ``docs/G0_BASELINE.md``. Every subcommand is
read-only: it issues ordinary authenticated API calls (and, for ``queue``, one
``DELETE /v1/queue/requests/{id}`` against a request this script itself
enqueued). It never touches configuration, never restarts anything and never
prints the API key.

Latency is measured client-side with ``time.perf_counter()`` around a single
non-streaming request and covers the full round trip through nginx
(``http://127.0.0.1:11434`` → Guardian). Percentiles use the nearest-rank
method: ``sorted(values)[ceil(p/100 * n) - 1]``.

Examples
--------
    ./venv/bin/python scripts/g0_baseline_measure.py models --n 20
    ./venv/bin/python scripts/g0_baseline_measure.py chat --n 20 \
        --model ai-kvm2-local/qwen/qwen3.8-27b-uncensored-ymq --max-tokens 32
    ./venv/bin/python scripts/g0_baseline_measure.py chat --n 20 \
        --model github-copilot/openai/gpt-6.1-sol --max-tokens 32 --temperature 1
    ./venv/bin/python scripts/g0_baseline_measure.py queue
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# The key store is gitignored, so a worktree copy does not carry it: point at the
# deployment checkout with --keys-file (or GUARDIAN_KEYS_FILE) when running from one.
KEYS_FILE = Path(os.environ.get("GUARDIAN_KEYS_FILE", REPO_ROOT / "config" / "guardian.keys.yaml"))
DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_PROMPT = "Reply with exactly: baseline-ok"
LONG_PROMPT = "Write the integers from 1 to 150 in ascending order, one per line, no other text."


def load_key(name: str, keys_file: Path) -> str:
    """Return the named Guardian key from the key store (never printed)."""
    data = yaml.safe_load(keys_file.read_text(encoding="utf-8"))
    for key_id, meta in data.items():
        if isinstance(meta, dict) and meta.get("name") == name:
            return key_id
    raise SystemExit(f"key '{name}' not found in {keys_file}")


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile: sorted[ceil(pct/100 * n) - 1]."""
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def call(url: str, key: str, payload: dict | None = None, method: str = "POST") -> tuple[int, object, float, dict]:
    """Issue one authenticated request; return (status, parsed_body, seconds, headers)."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            raw, status, headers = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:  # an HTTP error is still a measurement
        raw, status, headers = exc.read(), exc.code, exc.headers or {}
    elapsed = time.perf_counter() - started
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        parsed = {"_raw": raw[:400].decode("utf-8", "replace")}
    # Header names are case-insensitive on the wire (uvicorn sends lowercase);
    # normalise once here so callers never silently read None.
    normalised = {str(name).lower(): value for name, value in (headers or {}).items()}
    return status, parsed, elapsed, normalised


def summarise(rows: list[dict]) -> dict:
    ok = [row for row in rows if row["status"] == 200]
    latencies = [row["latency_ms"] for row in ok]
    rates = [row["tok_per_s"] for row in ok if row.get("tok_per_s")]
    return {
        "n_requested": len(rows),
        "n_ok": len(ok),
        "statuses": sorted({row["status"] for row in rows}),
        "p50_ms": round(percentile(latencies, 50), 2) if latencies else None,
        "p95_ms": round(percentile(latencies, 95), 2) if latencies else None,
        "min_ms": round(min(latencies), 2) if latencies else None,
        "max_ms": round(max(latencies), 2) if latencies else None,
        "mean_tok_per_s": round(sum(rates) / len(rates), 2) if rates else None,
    }


def run_series(args: argparse.Namespace) -> int:
    key = load_key(args.key_name, Path(args.keys_file))
    rows: list[dict] = []
    for index in range(args.n):
        if args.command == "models":
            status, parsed, elapsed, headers = call(f"{args.base}/v1/models", key, None, method="GET")
            detail = {"n_models": len(parsed.get("data", [])) if isinstance(parsed, dict) else 0}
        else:
            payload = {
                "model": args.model,
                "messages": [{"role": "user", "content": args.prompt}],
                "max_tokens": args.max_tokens,
                "temperature": args.temperature,
                "stream": False,
            }
            status, parsed, elapsed, headers = call(f"{args.base}/v1/chat/completions", key, payload)
            usage = (parsed.get("usage") or {}) if isinstance(parsed, dict) else {}
            tokens_out = int(usage.get("completion_tokens") or 0)
            detail = {
                "tokens_out": tokens_out,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "tok_per_s": round(tokens_out / elapsed, 3) if tokens_out and elapsed > 0 else None,
            }
        row = {
            "i": index,
            "status": status,
            "latency_ms": round(elapsed * 1000.0, 2),
            "request_id": headers.get("x-request-id"),
            "queue_wait_ms": headers.get("x-queue-wait-ms"),
            **detail,
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
        if args.gap_ms:
            time.sleep(args.gap_ms / 1000.0)

    summary = summarise(rows)
    print(json.dumps({"workload": args.command, "model": args.model or None, "base": args.base, **summary}, indent=2))
    return 0 if summary["n_ok"] == args.n else 1


def run_queue_probe(args: argparse.Namespace) -> int:
    """Occupy the single slot with one request and cancel a second, queued one."""
    key = load_key(args.key_name, Path(args.keys_file))
    results: dict[str, dict] = {}

    def chat(label: str, max_tokens: int) -> None:
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": LONG_PROMPT}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
        }
        status, parsed, elapsed, headers = call(f"{args.base}/v1/chat/completions", key, payload)
        results[label] = {
            "status": status,
            "elapsed_ms": round(elapsed * 1000.0, 2),
            "request_id": headers.get("x-request-id"),
            "queue_wait_ms": headers.get("x-queue-wait-ms"),
            "detail": (parsed.get("detail") or parsed.get("error")) if isinstance(parsed, dict) else str(parsed)[:300],
        }

    def status() -> dict:
        _, parsed, _, _ = call(f"{args.base}/v1/queue/status", key, None, method="GET")
        return parsed if isinstance(parsed, dict) else {}

    # This probe cancels a queued request, so it must never guess which one.
    # The queue is owner-scoped per key: `your_requests` is the only trustworthy
    # correlation surface, and it is used exclusively. If this key already owns a
    # queued or active request, or the queue is busy, we refuse to start — the
    # alternative is cancelling somebody else's production work.
    before = status()
    if before.get("active_count") or before.get("waiting"):
        print(
            "queue not idle before the probe (active_count="
            f"{before.get('active_count')}, waiting={len(before.get('waiting') or [])}); "
            "refusing to enqueue and cancel — rerun when Guardian is idle",
            file=sys.stderr,
        )
        return 1

    # Mirrors app/proxy/queue.py:ACTIVE_STATES — "running"/"cancelling" are the
    # active states; the queue has no plain "active" status.
    active_states = {"running", "cancelling"}

    def own(status_payload: dict, wanted: str) -> list[dict]:
        """Owner-scoped entries in the given state, newest first."""
        return [
            item
            for item in (status_payload.get("your_requests") or [])
            if item.get("status") == wanted
        ]

    def own_active(status_payload: dict) -> list[dict]:
        """Owner-scoped entries currently holding or cancelling the slot."""
        return [
            item
            for item in (status_payload.get("your_requests") or [])
            if item.get("status") in active_states
        ]

    first = threading.Thread(target=chat, args=("A", 256), daemon=True)
    first.start()

    deadline = time.monotonic() + args.timeout
    active = {}
    while time.monotonic() < deadline:
        active = status()
        if len(own_active(active)) == 1:
            break
        time.sleep(0.02)
    else:
        print("request A never became active", file=sys.stderr)
        return 2

    a_id = own_active(active)[0]["request_id"]

    second = threading.Thread(target=chat, args=("B", 32), daemon=True)
    second.start()

    deadline = time.monotonic() + args.timeout
    queued = {}
    while time.monotonic() < deadline:
        queued = status()
        queued_own = [item for item in own(queued, "queued") if item["request_id"] != a_id]
        if queued_own:
            break
        time.sleep(0.02)
    else:
        print("request B never appeared in this key's queue", file=sys.stderr)
        return 3

    if len(queued_own) != 1:
        # Two entries of our own key queued: B's identity is ambiguous, and
        # cancelling the wrong one would destroy a real request.
        print(
            "ambiguous queue correlation: expected exactly one queued request of this key, got "
            f"{[item['request_id'] for item in queued_own]}; no DELETE issued",
            file=sys.stderr,
        )
        second.join(timeout=args.timeout)
        first.join(timeout=args.timeout)
        return 4

    request_id = queued_own[0]["request_id"]
    del_status, del_body, _, _ = call(f"{args.base}/v1/queue/requests/{request_id}", key, None, method="DELETE")

    second.join(timeout=args.timeout)
    first.join(timeout=args.timeout)
    after = status()

    # Post-condition: the cancellation must be observable on request B itself.
    b_result = results.get("B") or {}
    b_cancelled = b_result.get("status") == 499 or "cancel" in str(b_result.get("detail") or "").lower()
    if not b_cancelled:
        print(
            "post-condition failed: request B did not report cancellation "
            f"(status={b_result.get('status')}, detail={b_result.get('detail')!r}); "
            "the cancelled id may not have been B",
            file=sys.stderr,
        )

    print(json.dumps({
        "status_before": before,
        "status_with_queued_request": queued,
        "cancelled_request_id": request_id,
        "active_request_id": a_id,
        "cancellation_correlated": b_cancelled,
        "delete_status": del_status,
        "delete_body": del_body,
        "request_A": results.get("A"),
        "request_B": results.get("B"),
        "status_after": after,
    }, indent=2))
    return 0 if b_cancelled else 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE, help="Guardian public base URL")
    parser.add_argument("--key-name", default="oelala", help="named Guardian key to use")
    parser.add_argument("--keys-file", default=str(KEYS_FILE), help="Guardian key store path")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, needs_model in (("models", False), ("chat", True)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--n", type=int, default=20, help="number of samples (>= 20 for a baseline)")
        cmd.add_argument("--max-tokens", type=int, default=32)
        cmd.add_argument("--temperature", type=float, default=0.0)
        cmd.add_argument("--prompt", default=DEFAULT_PROMPT)
        cmd.add_argument("--gap-ms", type=float, default=0.0, help="pause between samples")
        if needs_model:
            cmd.add_argument("--model", required=True)
        else:
            cmd.set_defaults(model=None)

    queue = sub.add_parser("queue")
    queue.add_argument("--model", default="ai-kvm2-local/qwen/qwen3.8-27b-uncensored-ymq")
    queue.add_argument("--timeout", type=float, default=120.0, help="per-phase poll deadline in seconds")

    args = parser.parse_args(argv)
    if args.command == "queue":
        return run_queue_probe(args)
    return run_series(args)


if __name__ == "__main__":
    raise SystemExit(main())
