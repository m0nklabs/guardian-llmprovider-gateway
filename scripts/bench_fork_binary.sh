#!/usr/bin/env bash
# bench_fork_binary.sh — validate the cuda128-laguna-tq-full fork-binary's NEW features
# (TurboQuant KV cache: turbo2/3/4) on a KNOWN-GOOD model before risking unproven ones.
#
# Operator strategy: Qwen3.6-35B-A3B already runs problem-free on the OLD binary with
#   q4_0 KV. So it is the safe candidate to prove the NEW turbo4 KV path works on a real
#   GQA MoE. If turbo4 loads + benchmarks cleanly here, the feature is validated; only
#   THEN do we squeeze Laguna (57GB) into 26GB VRAM + 80GB RAM with the same tricks.
#
# Runs two labeled passes: q4_0 KV (baseline, proven) and turbo4 KV (new feature).
# Explicit per-pass invocation (NOT ctk comma-iteration) for guaranteed-correct output.
#
# Usage:
#   ./scripts/bench_fork_binary.sh
#   MODEL=... NGL=99 KV1=q4_0 KV2=turbo4 ./scripts/bench_fork_binary.sh
#
# Later (Laguna, when ready):
#   MODEL=/home/flip/models/Laguna-S-2.1-UD-IQ4_XS-00001-of-00003.gguf NGL=16 TS=0.42/0.58 \
#     KV1=q4_0 KV2=turbo4 ./scripts/bench_fork_binary.sh
set -u
BINDIR=/home/flip/llama_cpp_official/worktrees/cuda128-laguna-tq-full/build-cuda128-full/bin
export LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64:"$BINDIR"

: "${MODEL:=/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf}"
: "${NGL:=99}"
: "${TS:=0.38/0.62}"
: "${KV1:=q4_0}"      # baseline (proven on old binary)
: "${KV2:=turbo4}"    # NEW fork feature under test
: "${FA:=on}"
: "${PP:=512}"
: "${TG:=128}"
: "${REPS:=2}"

[ -f "$MODEL" ] || { echo "ERR: model not found: $MODEL"; exit 2; }
[ -x "$BINDIR/llama-bench" ] || { echo "ERR: llama-bench not found: $BINDIR/llama-bench"; exit 2; }

run_pass() {
  local kv="$1" label="$2"
  echo "=========================================================="
  echo "pass: $label   (ctk=ctv=$kv)"
  echo "  model=$MODEL  ngl=$NGL  ts=$TS  fa=$FA  pp=$PP tg=$TG reps=$REPS"
  echo "  started: $(date -u +%H:%M:%SZ)"
  echo "=========================================================="
  "$BINDIR/llama-bench" -m "$MODEL" -ngl "$NGL" -ts "$TS" \
    -ctk "$kv" -ctv "$kv" -fa "$FA" -p "$PP" -n "$TG" -r "$REPS" 2>&1
  echo "PASS_EXIT[$label]=$?"
  echo
}

run_pass "$KV1" "baseline (proven)"
run_pass "$KV2" "NEW feature (turboquant KV)"
echo "all passes finished: $(date -u +%H:%M:%SZ)"
