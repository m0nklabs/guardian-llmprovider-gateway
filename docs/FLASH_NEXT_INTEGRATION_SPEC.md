# Qwen3.8-Flash-Next Integration Spec — Guardian local managed provider

> Status: **paper plan** (2026-10-11). Not executed. Nothing in this document has
> been applied: no config edited, no binary pin written, no model downloaded, no
> service touched. This spec is for operator review and later execution.
> Evidence labels: **[V]** = verified by reading code/binary help/system state in
> this session (file:line in §9), **[I]** = inferred from verified facts,
> **[A]** = assumption — validate at execution time.

## 0. Goal

Add model `qwen3.8-flash-next` (Qwen/Qwen3.8-Flash-Next: 125B-total / 6B-active
MoE + 51B n-gram "PLE" embeddings + QSA sparse attention, 48 layers + MTP,
262144 native context, vision) to the local managed provider
(`ai-node-local`), using the already-built qwen4exp llama.cpp binary:

- New binary (built, not yet pinned): `/home/flip/llama_cpp_official/worktrees/qwen4exp-latest/build-latest/bin/llama-server`
  — version `0.6.0-dev (build 11514, commit de7fa0a3c)`, 2026-10-08 tip [V],
  `qwen4exp` arch support verified in-binary via strings (session context) [V].
- Production binary today: `cuda128-laguna-tq-full/build-cuda128-full` (b2111
  era, bac23a3f9, 2026-07-29) — does NOT support qwen4exp [V, §2.1].

Constraints honored by this plan: config-only integration (no code change),
single llama-server restart, reversible, vision off by default.

## 1. Current runtime topology [V]

The production launch chain is **caretaker-owned**, not the Guardian repo's
wrapper:

1. `llama-server.service` (`/etc/systemd/system/llama-server.service:8-10`):
   `WorkingDirectory=/home/flip/caretaker-llamacpp/scripts`,
   `ExecStart=/home/flip/caretaker-llamacpp/scripts/start_llama.sh`,
   `Restart=always` (3 s). The Guardian repo's `scripts/start_llama.sh` is a
   mirror of the same resolution logic but is NOT what the unit executes
   (this split moved to the caretaker at the F6 cut-over;
   `docs/ARCHIVED_HANDOFFS.md:600-602`).
2. Caretaker launcher (`caretaker-llamacpp/scripts/start_llama.sh:46-47`):
   `DEFAULT_BINARY="$OFFICIAL_ROOT/build/bin/llama-server"` with
   `OFFICIAL_ROOT="${LLAMA_CPP_OFFICIAL_ROOT:-<caretaker repo>/../llama_cpp_official}"`
   (line 21), then `BINARY="${LLAMA_SERVER_BINARY:-$DEFAULT_BINARY}"`.
   Line 16 documents: *"BINARY <- LLAMA_SERVER_BINARY (set by the unit drop-ins)"*.
3. Binary pins are systemd drop-ins under
   `/etc/systemd/system/llama-server.service.d/`:
   - `10-b1295-backend.conf`: `LLAMA_SERVER_BINARY=.../cuda128-bleeding/build-cuda128-bleeding/bin/llama-server`
     + `LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64`.
   - `20-turboquant-backend.conf`: `LLAMA_SERVER_BINARY=.../cuda128-laguna-tq-full/build-cuda128-full/bin/llama-server`
     + `TURBO_AUTO_ASYMMETRIC=0` — **this wins today** (systemd reads drop-ins
     in lexical order; later `Environment=` assignment of the same var
     overrides the earlier one) [I, systemd semantics; consistent with the
     session-verified fact that production runs b2111].
4. Model selection: the caretaker writes
   `/home/flip/caretaker-llamacpp/config/current_model.args` (live today,
   mtime Oct 10) and the unit restart reads it
   (`caretaker start_llama.sh:55-57`); the launcher execs `$BINARY $ARGS`
   **unquoted** (line 73). Guardian writes its own
   `config/current_model.args` for its local lifecycle + drift signature
   (`app/paths.py:33`, `app/engine/manager.py:1322-1328`); the two registries
   are separate files (§4).
5. The gateway switches models through the caretaker (`/ensure` flow) or its
   own local lifecycle (`sudo systemctl stop/start llama-server`,
   `app/engine/manager.py:1397-1414`); switch triggers re-read the model
   registry each time (`manager.py:617-618`, `1012-1013`).
6. Guardian-side binary resolution (`app/paths.py:202-207`) mirrors the same
   env vars (`LLAMA_CPP_OFFICIAL_ROOT`, `LLAMA_SERVER_BINARY`) but the
   resolved path is only **logged** (`manager.py:1229`) — the args string
   never embeds a binary path.

Live production args today (caretaker `current_model.args`): ymq model,
`-c 150000 -ngl 99 -ctk turbo4 -ctv turbo4 --load-mode none
--tensor-split 0.42,0.58 --spec-type draft-mtp …` [V].

## 2. Binary selection mechanism (research task 1)

### 2.1 Options assessed

- **(a) Env override `LLAMA_CPP_OFFICIAL_ROOT` at the qwen4exp worktree.**
  REJECTED. The launcher always resolves `$OFFICIAL_ROOT/build/bin/llama-server`
  (caretaker `start_llama.sh:46`), but
  `/home/flip/llama_cpp_official/worktrees/qwen4exp-latest/` has **no `build/`
  directory** — only `build-latest/` (verified with ls). The override would
  resolve to a nonexistent binary; the launcher's fallback (`:66-70`) then
  re-falls back to the same nonexistent path → llama-server never starts.
- **(b) Rebuild the main official root from master.** REJECTED. Affects every
  local model at once (the main root is the DEFAULT_BINARY base), needs a full
  rebuild, and the drop-in pin (option below) makes the rebuild redundant for
  this integration.
- **(c) Per-model binary config key.** REJECTED. The supported per-model
  config keys are exactly: `path, context, ngl, kv_type, tensor_split, mmproj,
  extra_args, cuda_visible_devices, draft_model_path, spec_type,
  spec_draft_n_max, spec_draft_n_min, draft_cache_type_k, draft_cache_type_v`
  (`app/engine/manager.py:1314-1321`) plus `n_slots` (`:1296`). **No binary
  key exists**; adding one means code changes in both guardian and caretaker —
  out of scope for a config-only integration (and not invented here).
- **(d) Swap the main build directory.** REJECTED. Non-atomic, destroys the
  drop-in pin provenance (the 10-/20- files record exactly which build runs),
  and is strictly worse than a drop-in.
- **(e) RECOMMENDED — new systemd drop-in `30-flashnext-backend.conf`.**
  The identical mechanism the operator already used for the b1295 and
  turboquant pins (`10-b1295-backend.conf` header: *"llama.cpp backend pin
  (guardian hardening TASK 5, 2026-07-30)"*). One file, atomic to add, and
  removal (`rm` + `daemon-reload` + restart) restores the 20- pin exactly.
  `CHANGELOG.md:311` documents the same env var as the supported way to pin a
  known-good binary ("`start_llama.sh` now honors `LLAMA_SERVER_BINARY`").

### 2.2 Drop-in content (to be written by the operator)

`/etc/systemd/system/llama-server.service.d/30-flashnext-backend.conf`:

```ini
[Service]
# llama.cpp backend pin — qwen4exp arch support (Flash-Next integration)
# Binary: worktree qwen4exp-latest build-latest, 0.6.0-dev build 11514,
#   commit de7fa0a3c (2026-10-08 tip).
# RUNPATH self-locates build-latest/bin shared libs (readelf verified);
# CUDA runtime via the 12.8 toolkit path, mirroring 10-b1295-backend.conf.
Environment=LLAMA_SERVER_BINARY=/home/flip/llama_cpp_official/worktrees/qwen4exp-latest/build-latest/bin/llama-server
Environment=LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64
```

`readelf -d` on the new binary shows `RUNPATH=[.../qwen4exp-latest/build-latest/bin:]`
[V] — libllama/libggml self-locate; `LD_LIBRARY_PATH` covers CUDA runtime libs
the same way the existing pins do. Validate with `ldd` at execution [A].

### 2.3 Scope of the pin — the turbo4 disjointness (key risk)

The pin applies to the single shared llama-server unit, i.e. **all local
models**, and the two binaries accept disjoint model/KV sets:

| | b2111 laguna-tq-full (production today) | build-latest 11514 (new) |
|---|---|---|
| `qwen4exp` arch (Flash-Next) | ✗ (session-verified) | ✓ |
| `kv_type: turbo4` / `turbo2/3` | ✓ (live ymq runs `-ctk turbo4` today [V]) | **✗** — `-ctk` allowed values are `f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1` (verified from `--help`) |

The Guardian gateway accepts `turbo*` in `runtime_overrides` and reads base
config `kv_type` unvalidated (`app/engine/manager.py:1040-1056`, comment:
*"TurboQuant KV-cache types supported by the cuda128-laguna-tq-full
fork-binary"*), so the incompatibility only surfaces as a hard llama-server
launch failure ("invalid cache type") [I]. Today **15 local entries** carry
`kv_type: turbo4` (gemma-4-26B ×3, granite, qwen3.8-27b ×7, qwen3.5-9b ×4 —
`config/providers/ai-node-local.settings.yaml`) — every one of them is
unlaunchable while the 30- drop-in is active.

Consequences folded into §5/§6: fallback insurance entries with `q8_0` KV
(valid on both binaries), and strict cutover ordering (never restart the unit
with the new binary active while the args file still names a turbo4 model —
the unit `Restart=always` would crash-loop, and guardian crash detection only
runs during switches, `manager.py:1416-1449`).

## 3. Model config entry (research task 2)

### 3.1 Two registries must both get the entry [V]

The caretaker reads **its own** registry:
`CARETAKER_MODELS_FILE` is unset in `/etc/caretaker/caretaker.env`
(verified), so `caretaker/config.py:47` falls back to
`models.local.settings.yaml` relative to `/home/flip/caretaker-llamacpp` — a
separate operator-owned draft copy, *"derived from guardian config/
providers/ai-kvm2-local.settings.yaml on 2026-08-31"* (its header comment),
mirroring the guardian registry minus `brand:`-style keys. The caretaker
builds the LIVE launch args from that copy (`caretaker/manager.py:433,472`).
Guardian reads `config/providers/ai-node-local.settings.yaml` for routing,
discovery and the launch-signature drift check (`app/paths.py:160-172`,
`manager.py:67-72`). Existing precedent: ymq/pi entries exist in **both**
files (verified by grep).

### 3.2 Guardian entry (add to `config/providers/ai-node-local.settings.yaml`,
inside `models:`, plus one alias in `aliases:`)

```yaml
  # Qwen3.8-Flash-Next (Qwen/Qwen3.8-Flash-Next; 125B-total/6B-active MoE +
  # 51B n-gram PLE embeddings + QSA sparse attention, 48 layers + MTP,
  # 262144 native context). ISTA-DASLab GSQ-RCO IQ3_S 2-shard GGUF +
  # separate MTP draft file. Experts and PLE host-resident (-ot exps=CPU);
  # vision omitted (open llama.cpp quality issue on qwen4exp). Needs the
  # qwen4exp binary pin (drop-in 30-flashnext-backend.conf, spec §2.2).
  qwen3.8-flash-next:
    path: /home/flip/models/qwen3.8-flash-next/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf
    brand: qwen
    total_layers: 49
    benchmark_context_limit: 262144
    context: 150000
    ngl: 99
    kv_type: q8_0
    tensor_split: 0.42,0.58
    default_enable_thinking: true
    spec_type: draft-mtp
    spec_draft_n_max: 3
    spec_draft_n_min: 1
    draft_model_path: /home/flip/models/qwen3.8-flash-next/mtp-Qwen3.8-Flash-Next-Q4_0.gguf
    draft_cache_type_k: f16
    draft_cache_type_v: f16
    extra_args: --jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0
      --presence-penalty 0.0 --repeat-penalty 1.0 --load-mode mmap -ot exps=CPU
```

Alias (in `aliases:`, mirroring `qwen3.8-pi` etc.):
`qwen3.8-flash: qwen3.8-flash-next`

The snippet parses (validated with the repo venv's `yaml.safe_load` in this
session). The `path` filename is an **[A]** placeholder — see D1/D2 below.

### 3.3 Caretaker entry (add to `/home/flip/caretaker-llamacpp/models.local.settings.yaml`,
same shape as its existing entries — no `brand` key)

```yaml
  qwen3.8-flash-next:
    path: /home/flip/models/qwen3.8-flash-next/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf
    total_layers: 49
    benchmark_context_limit: 262144
    context: 150000
    ngl: 99
    kv_type: q8_0
    tensor_split: 0.42,0.58
    default_enable_thinking: true
    spec_type: draft-mtp
    spec_draft_n_max: 3
    spec_draft_n_min: 1
    draft_model_path: /home/flip/models/qwen3.8-flash-next/mtp-Qwen3.8-Flash-Next-Q4_0.gguf
    draft_cache_type_k: f16
    draft_cache_type_v: f16
    extra_args: --jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0
      --presence-penalty 0.0 --repeat-penalty 1.0 --load-mode mmap -ot exps=CPU
```

### 3.4 Key-by-key rationale

| Key | Value | Basis |
|---|---|---|
| `path` | shard-1 absolute path | `llama-server` takes `-m <first shard>` for multi-part GGUFs; guardian/backend-model verification parses exactly `-m (\S+\.gguf)` from the process cmdline (`manager.py:385-402`), so the config path must be shard 1. Exact shard filename **[A]** — confirm from the HF repo file listing at download (D2). llama.cpp multi-part convention requires both shards side-by-side with `-NNNNN-of-NNNNN` naming [I]. No existence pre-validation on the main path in `_build_args_string` (only mmproj/draft are checked, `manager.py:1244-1260`) — a wrong filename surfaces at launch. |
| `total_layers: 49` | 48 + MTP head, mirroring the 27B convention (`total_layers: 65` for the 64-layer 27B with MTP, config line 113) [I]. Only used for `ngl <= total_layers` validation in `runtime_overrides` (`manager.py:1095-1099`) — verify against the load log's `n_layer` at first launch [A]. |
| `benchmark_context_limit: 262144` | native context [V session]. |
| `context: 150000` | First-launch conservatism, mirroring the live ymq args (`-c 150000` [V]) and the pi/swift/ymq entries (150000, config lines 171/192/218). QSA KV is tiny per session context (~6 GB at 262k F16, less quantized), so 262144 is a plausible follow-up — decision point D3; changing `context` is config-only (launch-signature drift → reload, `manager.py:1376-1395`). |
| `ngl: 99` | Convention of every local entry (offload all layers; base-config `ngl` is unvalidated, `manager.py:1051-1053` comment). |
| `kv_type: q8_0` | **NOT `turbo4`**: build-latest rejects turbo* (§2.3 [V]). `q8_0` is accepted by both binaries [V `--help`]. QSA KV stays small; q8_0 ≈ ½ F16. |
| `tensor_split: 0.42,0.58` | Mirrors ALL draft-mtp qwen3.8 entries (pi/ymq/swift, config lines 174/195/221) and the live ymq args [V] — the larger share to GPU1 (5060 Ti 16 GB). With experts on CPU the split governs dense+attention weights and KV; the draft-model entries use 0.42/0.58 to leave draft VRAM headroom [I]. |
| `spec_type: draft-mtp` + `draft_model_path` | For qwen4exp the MTP head ships as a separate file; community pattern is `--spec-type draft-mtp` **plus** `--model-draft <mtp-*.gguf>` (session context, differs from the 27B entries which rely on the embedded head with no draft file — that path exists in code too: `manager.py:1274-1280` emits only `--spec-type` when no draft file is set). With `draft_model_path` set and existing, the manager emits the full block: `--spec-type draft-mtp --model-draft <file> --spec-draft-n-max 3 --spec-draft-n-min 1 --cache-type-k-draft f16 --cache-type-v-draft f16` (`manager.py:1254-1273`) [V]. All four flags verified present in build-latest `--help` [V]. MTP gives 1.3–1.7× (session context). |
| `spec_draft_n_max: 3` / `n_min: 1` | Mirrors the pi entry (draft-3 = +50–58% decode, config comment lines 183-186); binary defaults would be 3/0 (`--help` [V]). |
| `draft_cache_type_k/v: f16` | Mirrors pi (`config lines 205-206`); binary default f16 [V]. |
| `mmproj` | **Omitted** → `--mmproj` never emitted (`manager.py:1243-1249`), vision impossible on this entry (guardian's runtime mmproj flag resolves to False without one, `manager.py:181-182`). This is deliberate: the qwen4exp vision-quality regression vs 27B is open upstream (ggml-org/llama.cpp #27886, session context). Re-adding mmproj later is config-only (drift → reload) — see OQ list. |
| `extra_args` | Sampling block identical to the other qwen3.8 thinking entries (config lines 207-208 etc.). `--jinja` verified in `--help` (default enabled anyway, kept for entry consistency) [V]. |
| `--load-mode mmap` (in extra_args) | Overrides the **hardcoded** `--load-mode none` the manager (and caretaker) prepend to every args string (`manager.py:1234` [V]; live caretaker args show it too [V]). Rationale: swap is 8/8 GB FULL right now (`/proc/swaps`: 8388600/8388604 KB used [V]); `none` = no mmap → the whole 83.6 GB file is read eagerly into RAM at load, while `mmap` pages lazily and matches the PLE table's MADV_RANDOM + batched-prefetch host-residency design (llama.cpp issue #27886, session context). Later occurrence wins is standard llama.cpp arg parsing [I] — **verify in the smoke log** that mmap is active (OQ3). |
| `-ot exps=CPU` (in extra_args) | Exact syntax verified from `--help`: `-ot, --override-tensor <tensor name pattern>=<buffer type>,...` [V]. **No quotes** in `extra_args`: the args string is written raw (`manager.py:1301-1304`) and exec'd unquoted (`caretaker start_llama.sh:73`) — bash variable expansion does not strip quotes, so `-ot "exps=CPU"` would pass literal quote characters. `exps=CPU` contains no spaces and survives word splitting. This is the community-verified offload pattern (issue #27886): dense path on GPU, experts in RAM; last-match-wins semantics allow more specific per-layer overrides later. A verified alternative knob exists: `-ncmoe, --n-cpu-moe N` (keep first-N layers' MoE weights on CPU, `--help` [V]) — tuning lever only (D4). |

## 4. Downloads & placement (research task 3, part 1)

Decision point **D1 — target directory**. **RESOLVED 2026-10-11 (post-cleanup
update): use the convention target `/home/flip/models/qwen3.8-flash-next/` on
`/` (vda2).** The original constraint is gone: the lossless cleanup pass freed
77 GB, so vda2 now has 161 GB free at 80% usage — IQ3_S's 86.4 GB footprint
fits with wide margin. vda2 is also the MEASURED-fastest disk on this host
(benchmark 2026-10-11: seq read ~1.2 GB/s (cache-tainted upper bound), 4K
random 2766 IOPS, vs `/mnt/ali_nvme_500gb` 312 MB/s seq / 1860 IOPS), it hosts
all other models (config-convention consistency), and the PLE random-gather
rate is low (tens of KB/s) so 4K IOPS dominate — vda2 wins there too.
`/mnt/ali_nvme_500gb` (258 GB free) remains the documented fallback if `/`
refills again. **[I, updated after cleanup; disk numbers measured on-host]**

D2 — shard filenames. Exact HF filenames are an [A]: list them at download
time from the repo API before writing the config paths:

```bash
curl -s https://huggingface.co/api/models/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF | grep -o '"rfilename":"[^"]*"'
curl -s https://huggingface.co/api/models/ggml-org/Qwen3.8-Flash-Next-GGUF | grep -o '"rfilename":"[^"]*"'
```

Download (into `/home/flip/models/qwen3.8-flash-next/`):

```bash
mkdir -p /home/flip/models/qwen3.8-flash-next
cd /home/flip/models/qwen3.8-flash-next
# shard 1 + shard 2 (PLE table) — IQ3_S variant:
curl -L -C - -o <shard-1-name>.gguf https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/resolve/main/<shard-1-name>.gguf
curl -L -C - -o <shard-2-name>.gguf https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/resolve/main/<shard-2-name>.gguf
# MTP draft + (optional, later) mmproj:
curl -L -C - -o mtp-Qwen3.8-Flash-Next-Q4_0.gguf https://huggingface.co/ggml-org/Qwen3.8-Flash-Next-GGUF/resolve/main/mtp-Qwen3.8-Flash-Next-Q4_0.gguf
```

Fallback quant: IQ3_XXS (75.8 GB total, same PLE shard) if RAM/timing at IQ3_S
disappoints — same procedure, different filenames [A].

Verify: byte sizes vs the HF listing; `sha256sum` vs HF if published; the
build ships `llama-gguf` (`build-latest/bin/llama-gguf` [V]) — if its CLI
accepts a file path it dumps GGUF metadata (`general.architecture=qwen4exp`)
as a pre-launch check [A]; the smoke launch itself is the definitive check.

## 5. Deploy & switch procedure (research task 3, part 2)

Order matters because the binary pin is unit-global and the two binaries
accept disjoint model sets (§2.3). Numbered steps; `[A]` items to validate at
execution.

**Pre-flight (all read-only checks, no service interaction):**

1. Swap emptied: `free -h && cat /proc/swaps`. Today it is FULL
   (8388600/8388604 KB used [V]). Fix with `sudo swapoff -a && sudo swapon -a`
   (needs ~8 GB reclaimable RAM — 77 Gi available now [V]) or a reboot [A].
2. Disk: ≥ 90 GB free on the chosen target (D1); `df -h /home/flip/models`.
3. Smoke test passed (§5.1).
4. `qwen3.8-27b-*` fallback knowledge: with the pin active, turbo4 entries
   cannot launch (§2.3); fallback insurance from step 7 in place.

**Smoke test on a standalone port (no config touched):**

Use port **11442** — *not* 11441, which is the caretaker control API
(`ai-node-local.settings.yaml:3` `management_url`, `caretaker-llamacpp.service:9`)
[V]. The corrected target:

```bash
cd /home/flip/models/qwen3.8-flash-next
BIN=/home/flip/llama_cpp_official/worktrees/qwen4exp-latest/build-latest/bin/llama-server
LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64 "$BIN" \
  -m <shard-1-name>.gguf \
  -c 150000 -ngl 99 -ctk q8_0 -ctv q8_0 \
  --host 127.0.0.1 --port 11442 --load-mode mmap \
  --tensor-split 0.42,0.58 \
  --spec-type draft-mtp --model-draft mtp-Qwen3.8-Flash-Next-Q4_0.gguf \
  --spec-draft-n-max 3 --spec-draft-n-min 1 \
  -ot exps=CPU \
  --jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
  --presence-penalty 0.0 --repeat-penalty 1.0
```

Expected: startup completes without `unknown model architecture`; the log
shows the n-gram/PLE table host-resident (no VRAM allocation for it) and
`exps` buffers on CPU [A]; watch `free -h` during load for RAM headroom;
`nvidia-smi` shows GPU usage ≈ dense layers + KV only. Then:

```bash
curl -s http://127.0.0.1:11442/health   # expect {"status":"ok"}
# one completion; read tokens/s from the server log timings:
curl -s http://127.0.0.1:11442/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Write a haiku about RAM bandwidth."}],"max_tokens":200}'
```

Pass criteria: decode ≥ 8 tok/s (session-context expectation 8–14 at IQ3_S
with all experts on CPU); MTP speculative stats visible in the log with
acceptance > 0 [A]; confirm mmap active in the load log (OQ3). Kill the
server. If decode is too slow, consider IQ3_XXS or the per-layer `-ot` form
(D4) **before** touching production.

**Cutover (config + one restart):**

5. Add the entry + alias to BOTH registries (§3.2/§3.3).
6. Add fallback insurance (both registries): a `q8_0`-KV duplicate of one
   fallback, e.g. `qwen3.5-9b-q8kv` mirroring the existing
   `qwen3.8-27b-q8kv` pattern (`ai-node-local.settings.yaml:123-138`) —
   valid on BOTH binaries, launchable while the pin is active.
7. Hot-reload guardian config — endpoint verified:
   `POST /api/config/reload`, admin-only, any valid Guardian key reaching the
   admin API (`app/proxy/server.py:1426-1435`); runbook command form:
   `curl -X POST -H "Authorization: Bearer <key>" localhost:11434/api/config/reload`
   (`docs/skills/operator-runbook.md:27`). Key from `.env` or
   `config/guardian.keys.yaml` — never print or commit it [V].
8. Write drop-in `30-flashnext-backend.conf` (§2.2) then
   `sudo systemctl daemon-reload` — **no restart yet**.
9. Trigger the switch through the gateway (e.g. a chat completion addressed to
   alias `qwen3.8-flash`). The caretaker `/ensure` path writes the flash-next
   args file and restarts `llama-server.service`; that restart picks up BOTH
   the new drop-in and the new args in one launch [I]. This is the ONLY
   restart — never restart the unit before step 8/9 with a turbo4 args file
   active under the new binary (crash-loop, §2.3).
10. Verify: `pgrep -a llama-server` shows the build-latest binary + the
    flash-next shard-1 path; guardian `GET /api/status` reports
    `qwen3.8-flash-next`; log shows PLE host-side, MTP acceptance; tok/s ≥ 8.

**Expected traffic cut:** all local-model traffic is down for the load
duration (86 GB from NVMe ≈ 30–60 s + model init [I]; the guardian health
window is 120 s, `manager.py:1425`). Cloud models unaffected. If the
operator's own session runs on a local model, expect the drop.

## 6. Rollback plan

The switch failed / performance unacceptable:

1. Remove the pin and reload systemd:
   `sudo rm /etc/systemd/system/llama-server.service.d/30-flashnext-backend.conf && sudo systemctl daemon-reload`
   → production binary back to the `20-turboquant-backend.conf` pin (b2111
   laguna build) [V mechanism].
2. Switch back through the gateway to `qwen3.8-ymq` (alias, config line 292).
   Order matters: restore the binary pin FIRST — qwen4exp args fail on b2111
   ("unknown architecture"), turbo4 args fail on build-latest (§2.3); the
   caretaker rewrites the args file on the switch, so requesting ymq after
   the daemon-reload lands the exact pre-change state [I].
3. Nothing permanent changed: the flash-next config entries can stay (read
   only on switch); the q8kv fallback entry is harmless on both binaries.
   For a later retry, only the drop-in is re-added.

If the flash-next launch crashes: guardian records a `ModelLoadError` and
stops the service to prevent a restart loop (`manager.py:1484-1515`); proceed
with the rollback steps above [V].

## 7. Risks & open questions (research task 4)

**Risks**

- **R1 Vision regression** (open upstream issue ggml-org/llama.cpp #27886):
  vision disabled by design (no mmproj in the entry). Re-adding mmproj later
  is config-only; expect the `vision_context`-style keys used by the 27B
  entries (`config lines 133-135, 197-199`) as the shape [I].
- **R2 Unit-global binary pin + turbo4 disjointness**: while the 30- drop-in
  is active, 15 existing entries are unlaunchable (§2.3). Fallback insurance
  (step 6) and the strict ordering (steps 8→9) are the mitigations. The pin
  and the active model must always be moved together.
- **R3 RAM headroom**: 86 GB model, experts + PLE host-resident; `--load-mode
  none` (hardcoded) would read everything eagerly. Mitigations: `--load-mode
  mmap` override, swap cleared, current model unloaded before launch (the
  switch does that). Baseline host usage today: 30 Gi total, 78 Gi
  buff/cache, 77 Gi available [V] — validate during the smoke load [A].
- **R4 Decode speed**: 8–14 tok/s expected at IQ3_S (session context). Tuning
  levers: IQ3_XXS, per-layer `-ot` (e.g. first-N-layers experts on GPU,
  issue #27886), or the verified `-ncmoe N` flag (inverse semantics —
  validate at tuning time) [A].
- **R5 Health window vs load time**: 120 s (manager) vs an 86 GB load — with
  `--load-mode mmap` the launch is lazy, and D1 now points at vda2
  (measured-fastest disk, §4) [updated post-cleanup 2026-10-11].
- **R6 Slot/session compatibility**: `--slot-save-path /home/flip/llama_slots`
  is always on (launcher default line 53 / manager `LLAMA_SLOTS_DIR`); session
  state fixes for qwen4exp landed early Sep and the build has them (session
  context), but slot files saved by b2111 may not restore under build-latest
  and vice versa (session-state version bump) [I]. Guardian tolerates restore
  failures ("starting fresh", `manager.py:722-726`) and save failures
  (`manager.py:902-907`) — so the effect is one-time context loss across the
  binary boundary, never a failed switch. Stale `auto_save_*` files are
  harmless after that.

**Open questions for the operator**

- **D1** Target directory: **RESOLVED** — `/home/flip/models/qwen3.8-flash-next/`
  on `/` (vda2); the 2026-10-11 lossless cleanup freed 161 GB there (§4).
  Fallback if `/` refills: `/mnt/ali_nvme_500gb` (258 GB free).
- **D2** Confirm exact HF shard filenames at download time (§4) and write
  them into both registry entries.
- **D3** Context 150000 first launch → raise to 262144 after validation?
  (config-only; QSA KV is tiny).
- **D4** If decode < 8 tok/s: IQ3_XXS vs per-layer `-ot` vs `-ncmoe` tuning —
  decided from smoke-test numbers, not blind.
- **OQ1** `llama-gguf` CLI usability for pre-launch metadata check [A].
- **OQ2** Last-arg-wins for `--load-mode mmap` over the hardcoded `none`
  (standard parsing [I]) — confirm in the smoke log.
- **OQ3** The caretaker's own `/ensure` load timeout (its repo, not read
  here) — verify before cutover that an 86 GB load doesn't trip it [A].
- **OQ4** `total_layers: 49` vs the load log's `n_layer` — adjust if needed.
- **OQ5** Vision: when the upstream issue resolves, re-add mmproj + the
  `vision_*` keys as an experimental entry (drift → reload).

## 8. State after execution (for the eventual `docs/HANDOFF.md` entry)

When the operator approves and executes this plan, the handoff note should
record: model `qwen3.8-flash-next` added to BOTH registries (guardian
`config/providers/ai-node-local.settings.yaml` + caretaker
`/home/flip/caretaker-llamacpp/models.local.settings.yaml`) with alias
`qwen3.8-flash`; llama-server binary pinned to
`worktrees/qwen4exp-latest/build-latest` via drop-in
`30-flashnext-backend.conf` (b2111 laguna pin preserved in drop-in `20-` for
instant rollback); **while pinned, all `kv_type: turbo4` entries are
unlaunchable** and the q8kv fallback entry is the escape hatch; flash-next
runs `-ot exps=CPU` + PLE host-resident, vision off (mmproj omitted) pending
the upstream fix; measured decode tok/s and MTP acceptance from the smoke
test; measured context ceiling from D3; slot files are not portable across
the two binaries (one-time context loss on each binary move).

## 9. Evidence index (all read in this session)

| Fact | Source |
|---|---|
| Unit ExecStart → caretaker launcher | `/etc/systemd/system/llama-server.service:8-10` |
| Binary resolution + `LLAMA_SERVER_BINARY` pin mechanism | `caretaker-llamacpp/scripts/start_llama.sh:16,21,46-47,66-70,73` |
| Guardian-side mirror resolution (logging only) | `scripts/start_llama.sh:7,31-33`; `app/paths.py:202-207`; `app/engine/manager.py:1229` |
| Production pin = laguna-tq-full (drop-in precedence) | `/etc/systemd/system/llama-server.service.d/10-b1295-backend.conf`, `20-turboquant-backend.conf` |
| Live production args (ymq, turbo4, `--load-mode none`) | `/home/flip/caretaker-llamacpp/config/current_model.args` (mtime 2026-10-10) |
| Supported per-model config keys (no binary key) | `app/engine/manager.py:1296,1314-1321` |
| Draft-model emission (`--spec-type` + `--model-draft` + n_max/n_min + draft cache) | `app/engine/manager.py:1254-1273`; no-draft branch `:1274-1280` |
| Hardcoded `--load-mode none` in args | `app/engine/manager.py:1234`; live caretaker args [V] |
| `extra_args` raw pass-through, unquoted exec | `app/engine/manager.py:1301-1304`; `caretaker start_llama.sh:73` |
| Switch = systemctl stop/start, health window 120 s, crash handling | `app/engine/manager.py:1397-1414,1425,1484-1515` |
| turbo* accepted by gateway (fork-binary comment), unvalidated base config | `app/engine/manager.py:1040-1056` |
| Drift check / hot config edits | `app/engine/manager.py:617-618,1012-1013,1376-1395` |
| Reload endpoint (admin, any valid key) | `app/proxy/server.py:1426-1435`; `docs/skills/operator-runbook.md:27` |
| Model registry paths (guardian vs caretaker) | `app/paths.py:160-172`; `caretaker/config.py:40-48`; `/etc/systemd/system/caretaker-llamacpp.service:15,22-24`; caretaker draft header comment |
| Draft-model precedent entry | `config/providers/ai-node-local.settings.yaml:187-208` (qwen3.8-27b-pi) |
| 27B total_layers 65 / q8kv pattern / aliases / pin null | same file `:113,123-138,282-301` |
| F6 launch-path split history | `docs/ARCHIVED_HANDOFFS.md:600-602`; `CHANGELOG.md:311` |
| Binary: version, `-ot` syntax, `-ctk` values (no turbo), `--spec-type` enum incl. draft-mtp, `--spec-draft-*`, `--load-mode`, `-ncmoe`, `-fa`, `--slot-save-path`, `--jinja`, `llama-gguf` present, RUNPATH | `llama-server --help/--version` and `readelf -d` on `worktrees/qwen4exp-latest/build-latest/bin/llama-server`; `ls build-latest/bin`; no `build/` dir in the worktree |
| Swap 8/8 GB FULL; disk free (/, ssd, hgst, ali_nvme) | `/proc/swaps`; `df -h`; `lsblk` |
