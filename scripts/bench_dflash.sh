#!/usr/bin/env bash
# bench_dflash.sh — measure DFlash speculative-decoding speedup on the proven
# Qwen3.6-35B-A3B target, using the cuda128-laguna-tq-full fork binary.
#
# WHY a separate harness (not bench_fork_binary.sh): llama-bench has NO spec flags,
# so speculative decoding MUST be measured via llama-server (real completion + HTTP
# /v1/chat/completions `timings`). This harness drives the server lifecycle.
#
# Operator strategy (per user, 2026-08-01): DFlash is the SPEED lever. Validate the
#   Qwen3.6-35B DFlash draft (z-lab/Qwen3.6-35B-A3B-DFlash, 0.4B, BF16) for a real
#   tok/s speedup on the proven Qwen target BEFORE risking Laguna. z-lab's headline
#   3.61x is an SGLang/vLLM datacenter-GPU number — NOT directly comparable to
#   llama.cpp on 2 consumer GPUs; report the measured ratio honestly.
#
# Method: apples-to-apples — baseline (no spec) vs DFlash (n-max 8 + 16), same
#   server path, same prompt, greedy (temp 0, seed 42) for clean acceptance.
#
# Usage:
#   ./scripts/bench_dflash.sh
#   MODEL=... DRAFT=... NMAX1=8 NMAX2=16 ./scripts/bench_dflash.sh
set -uo pipefail

BINDIR=/home/flip/llama_cpp_official/worktrees/cuda128-laguna-tq-full/build-cuda128-full/bin
export LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64:"$BINDIR"

: "${MODEL:=/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf}"
: "${DRAFT:=/home/flip/models/Qwen3.6-35B-A3B-DFlash-BF16.gguf}"
: "${NGL:=99}"; : "${TS:=0.38/0.62}"; : "${CTK:=q4_0}"; : "${CTV:=q4_0}"; : "${FA:=on}"; : "${CTXD:=8192}"
: "${NGLD:=99}"; : "${CTKD:=f16}"; : "${CTVD:=f16}"
: "${NMAX1:=8}"; : "${NMAX2:=16}"
: "${N_PREDICT:=256}"; : "${SEED:=42}"; : "${REPS:=2}"
: "${PORT:=11450}"; : "${HOST:=127.0.0.1}"

[ -f "$MODEL" ] || { echo "ERR: target model not found: $MODEL"; exit 2; }
[ -x "$BINDIR/llama-server" ] || { echo "ERR: llama-server not found: $BINDIR/llama-server"; exit 2; }

PROMPT='Write a detailed step-by-step explanation of how multi-head self-attention works in a transformer neural network, including the roles of queries, keys, values, the scaling factor, and the softmax operation.'

BODY=$(mktemp); WARM=$(mktemp)
python3 -c 'import json,sys; print(json.dumps({"messages":[{"role":"user","content":sys.argv[1]}],"max_tokens":int(sys.argv[2]),"temperature":0,"seed":42}))' "$PROMPT" "$N_PREDICT" > "$BODY"
python3 -c 'import json,sys; print(json.dumps({"messages":[{"role":"user","content":sys.argv[1]}],"max_tokens":8,"temperature":0,"seed":42}))' "Hi" > "$WARM"
trap 'rm -f "$BODY" "$WARM"' EXIT

json_tps() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["timings"]["predicted_per_second"])'; }

wait_ready() {
  for _ in $(seq 1 120); do
    if curl -sf -o /dev/null --max-time 2 "http://$HOST:$PORT/health" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  return 1
}

# run_pass <label> [extra server args...]
run_pass() {
  local label="$1"; shift
  local srvlog="/tmp/dflash_srv_${label}.log"
  echo "=========================================================="
  echo "pass: $label"
  echo "  target=$MODEL  draft=${DRAFT:-<none>}  ngl=$NGL ts=$TS ctk=$CTK ctv=$CTV fa=$FA"
  echo "  started: $(date -u +%H:%M:%SZ)"
  echo "=========================================================="
  "$BINDIR/llama-server" \
    --model "$MODEL" --host "$HOST" --port "$PORT" \
    -ngl "$NGL" -ts "$TS" -ctk "$CTK" -ctv "$CTV" -fa "$FA" -c "$CTXD" \
    --no-context-shift --log-disable \
    "$@" > "$srvlog" 2>&1 &
  local pid=$!
  if ! wait_ready; then
    echo "ERR: server did not become ready"; tail -25 "$srvlog"; kill "$pid" 2>/dev/null; return 1
  fi
  echo "  server ready (pid $pid)"
  # warmup (load model weights + first kernel compile outside the timed run)
  curl -sf -X POST "http://$HOST:$PORT/v1/chat/completions" -H 'content-type: application/json' --data @"$WARM" -o /dev/null 2>/dev/null || true
  local sum=0 n=0 tps
  for r in $(seq 1 "$REPS"); do
    tps=$(curl -sf -X POST "http://$HOST:$PORT/v1/chat/completions" -H 'content-type: application/json' --data @"$BODY" 2>/dev/null | json_tps)
    [ -z "$tps" ] && { echo "  rep $r: <no timings>"; continue; }
    echo "  rep $r: ${tps} tok/s"
    sum=$(python3 -c "print($sum+$tps)"); n=$((n+1))
  done
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; sleep 2
  [ "$n" -eq 0 ] && { echo "ERR: no successful reps"; tail -15 "$srvlog"; return 1; }
  local mean; mean=$(python3 -c "print(round($sum/$n,2))")
  echo "  mean tg: ${mean} tok/s"
  echo "  --- spec stats (if any) from server log ---"
  grep -iE 'spec|draft|accept' "$srvlog" | grep -ivE 'spec-draft|speculative params|cache_type' | tail -6 || echo "  (no spec lines)"
  echo "PASS_TPS[$label]=$mean"
  echo "$mean"
}

BASE=$(run_pass "baseline-no-spec" || echo "0")
DF8=$(run_pass  "dflash-nmax${NMAX1}" --spec-type draft-dflash --spec-draft-model "$DRAFT" --spec-draft-n-max "$NMAX1" -ngld "$NGLD" -ctkd "$CTKD" -ctvd "$CTVD" || echo "0")
DF16=$(run_pass "dflash-nmax${NMAX2}" --spec-type draft-dflash --spec-draft-model "$DRAFT" --spec-draft-n-max "$NMAX2" -ngld "$NGLD" -ctkd "$CTKD" -ctvd "$CTVD" || echo "0")

echo
echo "############################################################"
echo "## DFlash BENCH RESULT (Qwen3.6-35B-A3B, greedy, n_predict=$N_PREDICT)"
echo "##   baseline : ${BASE} tok/s"
echo "##   dflash n=$NMAX1 : ${DF8} tok/s   (x$(python3 -c "print(f'{$DF8/$BASE:.2f}' if $BASE else 'n/a')"))"
echo "##   dflash n=$NMAX2: ${DF16} tok/s   (x$(python3 -c "print(f'{$DF16/$BASE:.2f}' if $BASE else 'n/a')"))"
echo "## ref: llama-bench tg128 (no spec, q4_0 KV) = ~103 tok/s ; z-lab SGLang claim = up to 3.61x (datacenter, NOT comparable)"
echo "############################################################"
echo "all passes finished: $(date -u +%H:%M:%SZ)"
