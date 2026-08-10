#!/usr/bin/env python3
"""Benchmark Qwen3.6-35B-A3B-Uncensored speed across KV-cache / DFlash variants.

Variants measured (all on the cuda128-laguna-tq-full fork llama-server):
  1. normal      - q4_0 KV cache, no speculative decoding (Guardian baseline)
  2. tq4         - turbo4 KV cache, no speculative decoding
  3. dflash      - q4_0 KV cache + DFlash draft model
  4. dflash-tq4  - turbo4 KV cache + DFlash draft model (Guardian production entry)

Each variant: launch llama-server on a scratch port, warm up, run 3 fixed
completion requests (greedy, fixed seed, prompt cache disabled), collect the
server-reported timings, then shut the server down and wait for VRAM release.

Logs and results land in data/bench-qwen36/.
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "bench-qwen36"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SERVER_BIN = (
    "/home/flip/llama_cpp_official/worktrees/cuda128-laguna-tq-full/"
    "build-cuda128-full/bin/llama-server"
)
MODEL = "/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf"
DRAFT = "/home/flip/models/Qwen3.6-35B-A3B-DFlash-BF16.gguf"

PORT = 11899
CTX = 32768
N_PREDICT = 512
RUNS = 3
HEALTH_TIMEOUT_S = 420

BASE_ARGS = [
    SERVER_BIN,
    "-m", MODEL,
    "-c", str(CTX),
    "-ngl", "99",
    "-ts", "0.38,0.62",
    "--flash-attn", "on",
    "--host", "127.0.0.1",
    "--port", str(PORT),
    "--parallel", "1",
]

VARIANTS = {
    "normal": ["-ctk", "q4_0", "-ctv", "q4_0"],
    "tq4": ["-ctk", "turbo4", "-ctv", "turbo4"],
    "dflash": [
        "-ctk", "q4_0", "-ctv", "q4_0",
        "--spec-type", "draft-dflash",
        "--model-draft", DRAFT,
        "--spec-draft-n-max", "8",
        "--spec-draft-n-min", "1",
        "--cache-type-k-draft", "f16",
        "--cache-type-v-draft", "f16",
    ],
    "dflash-tq4": [
        "-ctk", "turbo4", "-ctv", "turbo4",
        "--spec-type", "draft-dflash",
        "--model-draft", DRAFT,
        "--spec-draft-n-max", "8",
        "--spec-draft-n-min", "1",
        "--cache-type-k-draft", "f16",
        "--cache-type-v-draft", "f16",
    ],
}

# ~2500-token deterministic prompt: repeated numbered filler + an instruction.
PARAGRAPH = (
    "The archive described a coastal observatory where engineers logged tidal "
    "patterns, lantern rotations, and shipping lanes in careful ledgers. "
)
PROMPT = (
    "".join(f"Entry {i:04d}. {PARAGRAPH}" for i in range(240))
    + "\n\nSummarize the ledger entries above in a few sentences."
)


def post_completion(n_predict: int) -> dict:
    payload = {
        "prompt": PROMPT,
        "n_predict": n_predict,
        "temperature": 0.0,
        "seed": 42,
        "cache_prompt": False,
        "stream": False,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/completion",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.loads(resp.read())


def wait_healthy(proc: subprocess.Popen) -> bool:
    deadline = time.time() + HEALTH_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=5
            ) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def gpu1_free_mib() -> int:
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
            "-i", "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def wait_vram_released(timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if gpu1_free_mib() < 300:
            return
        time.sleep(3)
    print("WARNING: GPU1 VRAM not fully released before next variant", flush=True)


def run_variant(name: str, extra_args: list) -> dict:
    log_path = OUT_DIR / f"{name}.server.log"
    print(f"=== {name}: launching server (log: {log_path.name}) ===", flush=True)
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            BASE_ARGS + extra_args,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        if not wait_healthy(proc):
            tail = log_path.read_text()[-3000:]
            raise RuntimeError(f"server failed to become healthy:\n{tail}")
        print(f"=== {name}: healthy, warmup ===", flush=True)
        post_completion(64)
        runs = []
        for i in range(RUNS):
            result = post_completion(N_PREDICT)
            timings = result.get("timings", {})
            runs.append(timings)
            print(
                f"=== {name}: run {i + 1}/{RUNS} "
                f"pp={timings.get('prompt_per_second', 0):.1f} t/s "
                f"tg={timings.get('predicted_per_second', 0):.2f} t/s "
                f"(n={timings.get('predicted_n')}) ===",
                flush=True,
            )
        tg_values = sorted(r.get("predicted_per_second", 0.0) for r in runs)
        pp_values = sorted(r.get("prompt_per_second", 0.0) for r in runs)
        return {
            "runs": runs,
            "tg_median": tg_values[len(tg_values) // 2],
            "pp_median": pp_values[len(pp_values) // 2],
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
        wait_vram_released()


def main() -> int:
    results = {}
    for name, extra in VARIANTS.items():
        try:
            results[name] = run_variant(name, extra)
        except Exception as exc:  # keep going with remaining variants
            print(f"!!! {name} FAILED: {exc}", flush=True)
            results[name] = {"error": str(exc)}

    results_path = OUT_DIR / "results.json"
    results_path.write_text(json.dumps(results, indent=2))

    print("\n===== RESULTS (median of", RUNS, "runs) =====")
    print(f"{'variant':<12} {'prompt t/s':>12} {'gen t/s':>10} {'gen speedup':>12}")
    base = results.get("normal", {}).get("tg_median")
    for name, data in results.items():
        if "error" in data:
            print(f"{name:<12} ERROR: {data['error'][:80]}")
            continue
        speedup = f"{data['tg_median'] / base:.2f}x" if base else "-"
        print(
            f"{name:<12} {data['pp_median']:>12.1f} "
            f"{data['tg_median']:>10.2f} {speedup:>12}"
        )
    print(f"\nFull results: {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
