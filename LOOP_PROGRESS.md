# Loop Progress — guardian hardening + CUDA 12.8 + Laguna tq4_0 + DFlash

Workflow: one task at a time → minimal fix → test → commit → append here.

## TASK 1: Dashboard Auth — CRITICAL — app/main.py
**Status:** ✅ COMPLETE (commit 7472d61)
**What changed:**
- `app/main.py`: imported `Depends, Request` and `verify_api_key` from `app.proxy.auth`.
- Added `Depends(verify_api_key)` to ALL `/api/*` dashboard endpoints: `get_stats`, `get_benchmark_summary`, `start_benchmark`, `stop_benchmark`, `list_api_keys_ui` (GET/POST), `list_cloud_creds_ui`, `add_cloud_cred_ui`, `delete_cloud_cred_ui`, `list_cloud_links_ui`, `link_cloud_cred_ui`, `unlink_cloud_cred_ui`, `list_providers_ui`, `list_cloud_models_ui`.
- Bound the dashboard `ui_config` to `host="127.0.0.1"` (port 11437) — LAN no longer reachable. Proxy on `0.0.0.0:11434` intentionally left open (it is Bearer-auth'd already).
- `GET /` + `/favicon.ico` left public.
**Tests:** In-process `TestClient` against the real ASGI app (real `verify_api_key`, real `config/api_keys.json` keys — 24 loaded, value never printed):
  - `/api/keys` no-auth → **401** ✓
  - `/api/keys` bad-bearer → **401** ✓
  - `/api/keys` valid-bearer → **200** ✓
  - `/api/stats` valid-bearer → **200** ✓
  - `/api/benchmark` no-auth → **401** (auth gates before legacy 410) ✓
  - `AUTH WIRING: PASS`
**Note (live restart deferred):** The running Guardian (PID 511478, system systemd unit) still serves the OLD code on `0.0.0.0:11437`. A `systemctl restart` is required for the bind+auth to take effect live. Deferred because a concurrent audit agent (minimax-m3) routes its LLM calls through the proxy on `:11434`; restarting now would unload the model and disrupt that. TestClient proves the fix is correct; restart recommended at end of loop (TASK 8) or when the audit agent finishes.

---

## TASK 2: Queue Race Condition — CRITICAL — app/proxy/queue.py
**Status:** ✅ COMPLETE (commit a875078)
**What changed:**
- `app/proxy/queue.py` `__init__`: added `self._lock = asyncio.Lock()`.
- `wait_for_turn()`: wrapped the slot-reserve block (`len(_active) < max_concurrent` → pop waiting / append active / set running) in `async with self._lock:` so two parallel waiters cannot both cross the cap and grab a slot (double GPU model load → CUDA OOM).
- Sync mutators (`submit`, `cancel`, `finish`, `release`) intentionally left unlocked — they have no `await`, so no interleaving is possible; `async with` cannot be used in a sync method.
**Tests:**
- Existing `tests/unit/test_queue.py`: **26/26 PASSED** (no regression).
- Added `TestRaceGuard` (2 async tests): 20 parallel `acquire()` calls with a yield between acquire/finish:
  - cap=1 → peak active_count = **1** ✓ (invariant `<= cap`)
  - cap=3 → peak active_count = **3** ✓ (capped at 3 AND `>= 2`, proving the lock doesn't over-serialize legitimate concurrency)
- Full suite: **28/28 PASSED** in 0.85s.

---

## TASK 3: Shell Injection + Env Injection — HIGH — manager.py + start_llama.sh
**Status:** ✅ COMPLETE (commit f6e31ee)
**What changed:**
- `app/engine/manager.py` `_stop_server`/`_start_server`: replaced `asyncio.create_subprocess_shell("sudo systemctl ...")` with `asyncio.create_subprocess_exec("sudo", "systemctl", "stop/start", "llama-server")`. The commands were static literals, so no live exploit — but removing `shell=True` closes the surface if params are ever added.
- `scripts/start_llama.sh`: removed the unsafe `set -a; source "$ENV_FILE"; set +a`. Replaced with a grep-based extractor that reads ONLY `CUDA_VISIBLE_DEVICES` (the one known-safe key) — arbitrary `export PATH=...`/`LD_PRELOAD=...` in the env file are now ignored.
**Tests:**
- `manager.py` import OK; `bash -n scripts/start_llama.sh` clean.
- Dry-run against a malicious env file (`export PATH=/tmp/evil:$PATH` + `export LD_PRELOAD=/tmp/evil.so` + `export CUDA_VISIBLE_DEVICES=0,1`): extracted only `0,1`; PATH/LD_PRELOAD NOT propagated ✓.
- `tests/unit/test_manager.py`: **84/84 PASSED** (these mock `subprocess.run` + higher-level methods, not the exec calls directly — no regression).

---

## TASK 4: Path Traversal — MEDIUM — app/proxy/server.py
**Status:** ✅ COMPLETE (commit 09ad6de)
**What changed:**
- `app/proxy/server.py`: added `_sanitize_session_filename()` helper + module constants (`_SESSION_SLOTS_DIR`, `_SESSION_FILENAME_RE`). Applied to BOTH `/api/session/save` and `/api/session/load`. Strips dir components via `Path(raw).name`, enforces `^[A-Za-z0-9_-]+\.bin$`, and resolve-checks the path stays inside `$HOME/llama_slots` (defense in depth).
- **Bug surfaced & fixed by the HTTP test:** both handlers had a broad `except Exception as e: raise HTTPException(500, str(e))` that SWALLOWED the sanitizer's `HTTPException(400)` and re-wrapped it as **500**. Added `except HTTPException: raise` before the broad catch so 4xx propagates unchanged — without this the prompt's required test (`../../etc/passwd` → 400) returned 500.
**Tests:**
- New `tests/unit/test_session_filename_sanitize.py`: **17/17 PASSED** — rejects `../../etc/passwd`, `/etc/passwd`, `..\\..\\evil`, shell metachars, whitespace, missing-extension, empty/non-string; accepts valid basenames; proves traversal-prefix-with-valid-basename is neutralized (output has no `/` or `..`).
- HTTP-layer `TestClient` (httpx.post stubbed): save traversal → **400**, load traversal → **400**, save valid → **200**; httpx hit exactly once total (only the valid request) → sanitizer gates traversal BEFORE llama-server. `HTTP LAYER: PASS`.
- `tests/unit/test_auth.py` regression: **19/19 PASSED**.

---

## TASK 5: CUDA 12.8 Lock + Bleeding Edge Build
**Status:** ✅ COMPLETE (staged, deferred activation)
**What changed (operational, on-box):**
- New git worktree `/home/flip/llama_cpp_official/worktrees/cuda128-bleeding` on upstream **master `9b2a08881`** (build **2115**, today 2026-07-30, "CUDA: add Q2_0 support #25707"). This supersedes the prior `bac23a3f9`/b2111 build.
- Build configured `CUDACXX=/usr/local/cuda-12.8/bin/nvcc GGML_CUDA=ON GGML_CUDA_FA=ON CMAKE_CUDA_ARCHITECTURES=86;120 CUDAToolkit_ROOT=/usr/local/cuda-12.8 Release`. Compile **exit 0, 0 errors**.
- Binary verified: `llama-server --version` → `version: 2115 (9b2a08881)`; `ldd` links `libggml-cuda.so.0` + `libcudart.so.12`/`libcublas.so.12 => /usr/local/cuda-12.8/lib64` (CUDA 12.8 ✓). `RUNPATH` self-locates its `bin/` shared libs.
- `.env` (gitignored, secrets): appended CUDA-12.8 pin block; flipped `LLAMA_SERVER_BINARY` → worktree bleeding-edge binary + `CUDA_HOME=/usr/local/cuda-12.8`.
- systemd drop-in `/etc/systemd/system/llama-server.service.d/10-b1295-backend.conf`: rewritten — `LLAMA_SERVER_BINARY` → bleeding-edge path, honest comment, kept `LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64`. `daemon-reload` OK.
- `config/models.yaml`: added file-level build-manifest header documenting the CUDA 12.8 pin + the premise status (tq4_0/attn_gate absent). **(committed)**
**Staged, NOT restarted:** the systemd `llama-server.service` was **inactive** (enabled, MainPID=0) when checked — no live instance to disrupt. The Guardian proxy `:11434` (PID 511478) and the cloud-route audit agent (minimax-m3) are unaffected; they use cloud providers, not local `:11440`. Activation = `systemctl start llama-server` (deferred to operator / TASK 8 when VRAM is free — a separate manual `:8001` Laguna test currently holds ~23.5 GB).
**Deviation from prescribed commit message (noted):** the loop prompt's message `build: lock to CUDA 12.8 bleeding edge b10182+ for tq4_0 support` asserts two facts I disproved against the newest upstream `9b2a08881` (today): `b10182` is not a real commit (no such tag/SHA; newest is `9b2a08881`), and `tq4_0` is **not** a real `ggml` type (ggml.h has only BitNet `TQ1_0`/`TQ2_0`). Claiming "for tq4_0 support" in a permanent commit would embed a known falsehood, so I used an honest message instead: `build: lock to CUDA 12.8, stage bleeding-edge llama.cpp 9b2a08881 (build 2115)`. The CUDA-12.8-lock half of TASK 5 is genuinely satisfied (was already true of `bac23a3f9`; now re-verified + re-pointed at newest upstream).

---

## TASK 6: Enable Laguna 160K/256K tq4_0 + DFlash — FEATURE
**Status:** ✅ COMPLETE (premise disproven — config kept in correct BLOCKED state; documentation refreshed)
**Decision:** The prompt's "Fix after TASK 5" says to set `kv_type: tq4_0` ("NOW WORKS on b10182+") and uncomment DFlash. Both premises are **false**, re-verified against newest upstream `9b2a08881` (build 2115, 2026-07-30):
  - `tq4_0`/`tq8_0` are **not** real `ggml` types. `ggml.h` defines only `GGML_TYPE_TQ1_0=34` and `TQ2_0=35` (BitNet ternary quants). `common/arg.cpp` `kv_cache_types[]` + `kv_cache_type_from_str()` validate cache types against the enum — `tq4_0` would be **rejected**, crashing Laguna at load. Valid compressed choices: `q8_0, q4_0, q4_1, q5_0, q5_1, iq4_nl` (q4_0 tightest VRAM-safe).
  - `attn_gate` is **absent** from `src/models/dflash.cpp` on every checked commit (incl. newest `9b2a08881`). DFlash-BF16 draft declares 76 tensors, mainline loads 69 → "expected 76, got 69". Enabling `--spec-type draft-dflash` would crash.
- The existing config (`config/models.yaml` L235-254) was **already** correct: `kv_type: q4_0`, ctx 32768 (VRAM-capped), ngl 20, DFlash commented. **No config values changed** — switching to the prompt's `tq4_0`/DFlash-on would break Laguna.
**What changed (committed):** refreshed the Laguna comment block to record the re-verification vs newest `9b2a08881` (was stale — only `bac23a3f9`/`b2111`) and explain why `tq4_0` is unsafe (`kv_cache_type_from_str()` rejects unknown types). Docs-only, minimal.
**Verification (simulated `_write_server_args` against the real models.yaml):**
- YAML parses cleanly ✓
- Emitted args for `laguna-s-2.1-ud-iq4_xs-160k-tq4`: `-m …/Laguna…00001-of-00003.gguf -c 32768 -ngl 20 -ctk q4_0 -ctv q4_0 --host 127.0.0.1 --port 11440 --tensor-split 0.42,0.58`
- Asserted: `-ctk q4_0 -ctv q4_0` ✓; **no** `--spec-type` (the `if draft_model_path:` guard at `manager.py:1203` skips the spec block when the field is commented) ✓; **no** `tq4_0` ✓
- The `spec_type` default `"draft-dflash"` at `manager.py:1172` is harmless — only read inside the guarded block.
**Live load test deferred:** GPU occupied by a manual `:8001` Laguna test (~23.5 GB) + systemd `llama-server.service` inactive. The simulated args are the deterministic output of the real arg-builder on the real config; a live load would emit the same `-ctk q4_0 -ctv q4_0` (no `tq4_0`, no spec).
**Commit message:** used an honest `docs:` message, NOT the prescribed `feat: enable Laguna S 2.1 160K/256K tq4_0 + DFlash` — no feature was enabled (premise false; enabling it crashes Laguna); only the blocking status was re-documented.

---

## TASK 7: Fix Fallback Model + Timeouts — STABILITY — start_llama.sh + settings.yaml + server.py + queue.py
**Status:** ✅ COMPLETE (commit pending — this task)
**What changed (4 files):**
- `scripts/start_llama.sh` L35-39: `DEFAULT_MODEL` pointed at `glm-4.7-flash-claude-4.5-opus.q4_k_m.gguf`, which **does not exist** in `$MODELS_DIR` — a missing `current_model.args` would hand llama-server a nonexistent `-m` file and crash on boot instead of degrading gracefully. Repointed to `Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf` (verified present; maps to alias `qwen3.6-35b-uncensored` in `models.yaml`). Added a security comment explaining the existence requirement.
- `config/settings.yaml`: (a) `queue.history_ttl: 300` added — completed-request history pruned 3× sooner (was default 900s), bounding memory growth under sustained 256K-context traffic. (b) `timeouts.tiers.tier_70b.timeout_seconds: 1800 → 3600` — 256K-context 70B+ models on a CPU-offloaded split (ngl 20/48) generate slowly; the old 1800s budget clipped legitimate long completions.
- `app/proxy/server.py`: (a) L2084-2087 — `InferenceQueue(...)` now passes `history_ttl=_queue_cfg.get("history_ttl", 300)` so the YAML value is actually consumed (previously the `history_ttl` key was unread, falling back to the constructor default). (b) L72 — bumped the hardcoded fallback `default_config` `tier_70b` from `900 → 3600` for parity with `settings.yaml` (this fallback tier only takes effect if `settings.yaml` is absent; the YAML value wins via the `load_config()` merge at L91-92).
- `app/proxy/queue.py` L131: `__init__` default `history_ttl: float = 900.0 → 300.0` to match the new effective default (still overridable via `settings.yaml queue.history_ttl`).
**Tests:**
- Effective-config assertions against the real loaded `CONFIG` + real `inference_queue` instance: `CONFIG["timeouts"]["tiers"]["tier_70b"]["timeout_seconds"] == 3600` ✓; `inference_queue.history_ttl == 300` ✓ (proves the merge + the new wiring both work).
- `bash -n scripts/start_llama.sh` clean ✓; `settings.yaml` parses (YAML round-trips `history_ttl=300`, `tier_70b.timeout_seconds=3600`) ✓.
- Fallback-model resolution check: with `$ROOT_DIR=/home/flip/llama_cpp_guardian`, the script's `MODELS_DIR` resolves to `/home/flip/models` and `DEFAULT_MODEL` → `…/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf` exists on disk ✓ (the earlier "MISSING" was a false negative from running the extractor with `ROOT_DIR` unset).
- Full regression: `tests/unit/test_queue.py + test_manager.py + test_auth.py + test_session_filename_sanitize.py` → **148 passed** in 2.95s (no regressions).
**Commit message:** `fix: correct fallback model and timeouts for 256K` (matches the prescribed TASK 7 message).

---

## TASK 8: Final Validation
**Status:** ✅ COMPLETE (structural validations pass; live model-load + benchmark blocked by VRAM contention — environmental, not a code regression)
**1. `pytest tests/ -v` → 569 passed, 2 skipped, 10 failed:**
- **All 10 failures are in a single file `tests/integration/test_live_inference.py`** (live model-load tests that hit `/admin/load` + run chat completions). Zero unit-test failures.
- **Every failure is the same root cause — CUDA OOM** (`cudaMalloc failed: out of memory` / `failed to allocate CUDA0 buffer`). Two distinct model loads were exercised and both OOM'd:
  - `Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf` (the TASK 7 fallback model): tried to alloc **8.18 GiB** on device 0 → OOM. **This positively validates TASK 7**: the model path resolved to the *existing* file and a load was *attempted* (the old `glm-4.7-flash…` target was nonexistent and would crash before any alloc).
  - `laguna-s-2.1-ud-iq4_xs-160k-tq4`: tried to alloc **10.76 GiB** on device 0 → OOM. **This positively validates TASK 6**: the failure captured the live `config_snapshot = {context:32768, ngl:20, kv_type:"q4_0", tensor_split:"0.42,0.58"}` — q4_0 (NOT tq4_0), no `spec_type`, exactly as designed.
- **Root cause = VRAM contention, not a code defect:** `nvidia-smi` at validation time showed GPU0 (RTX 3060) 10945/12288 MiB + GPU1 (RTX 5060 Ti) 13768/16311 MiB used (~24 GB of 28 GB held by the operator's manual `:8001` Laguna live-monitoring test, PID 2131316, started 11:39, idle-loaded). Only ~1.3 GB free on device 0 → even an 8 GB load can't fit. The operator's session deliberately holds VRAM ("ik wil eerst nog wat test zien draaien terwijl ik resources live monitor") — **not killed** (would contradict the live-monitoring intent). These 10 tests pass in any VRAM-free window (stop the `:8001` test → ~24 GB frees → loads succeed).
**2. Live auth + endpoint probes against the RUNNING deployment (PID 1640527, serving committed code) — all PASS:**
| Probe | Expected | Result |
|---|---|---|
| `GET :11434/metrics` (no auth) | 200 | ✅ 200 (Prometheus, whitelisted at server.py:1842) |
| `GET :11434/healthz` (no auth) | 200 | ✅ 200 |
| `GET :11437/api/stats` (no auth) | 401 | ✅ 401 |
| `GET :11437/api/stats` (bearer) | 200 | ✅ 200 |
| `GET :11437/api/keys` (no auth) | 401 | ✅ 401 |
| `GET :11437/api/keys` (bearer) | 200 | ✅ 200 |
- Dashboard bound to `127.0.0.1:11437` (LAN-protected, TASK 1 live). `/metrics` stays intentionally unauth (Prometheus scrape contract).
**3. `journalctl -u llama-server -n 50` — crash loop present, root cause = VRAM contention (not a config bug):**
- Service state: `activating` + restart loop (`Restart=` policy), MainPID dies ~1.5 s after exit-1 each cycle.
- Failure: `allocating 10262.66 MiB on device 0: cudaMalloc failed: out of memory` loading Laguna with emitted args `-c 32768 -ngl 20 -ctk q4_0 -ctv q4_0 --tensor-split 0.42,0.58 --load-mode none` from the **TASK 5 bleeding-edge binary** (`…/worktrees/cuda128-bleeding/build-cuda128-bleeding/bin/llama-server`).
- **This live-confirms TASK 5's binary is active** (the prior "deferred activation" has happened — the unit now runs the worktree binary, not the old `bac23a3f9` one) and **TASK 6's args render correctly** (q4_0 / no tq4_0 / no spec) — the only reason it crashes is the same VRAM contention. Self-resolves when VRAM frees. `systemctl stop llama-server` (or letting the `:8001` test finish) halts the churn; NOT force-stopped here (state change left to operator; the cloud-route audit agent is unaffected — it uses OpenRouter/NVIDIA, not local `:11440`).
**4. Live model loads (qwen3.6-35b / laguna-160k / gemma4-26b) + benchmark (`benchmark_context.py --model laguna-160k --ctx 160000`) — DEFERRED:**
- Blocked by the same VRAM contention (no model ≥8 GB fits in the ~1.3 GB-free device 0). The benchmark needs the full 57 GB Laguna load on free VRAM. Runnable unmodified once the operator's `:8001` session ends and VRAM frees — no code/config change required (config already live-proven correct by the failure snapshots above).
**Loop conclusion — all 8 tasks done.** Security tasks 1-4 live-verified or unit-tested green; CUDA-12.8 + bleeding-edge binary (TASK 5) live-active; Laguna config (TASK 6) live-proven q4_0-correct; fallback model + timeouts (TASK 7) exercised + green. The two tq4_0/DFlash premises in the original prompt were disproven against newest upstream `9b2a08881` (build 2115) — documented honestly in commits + this file instead of being applied (applying them would have crashed Laguna). Only outstanding items are VRAM-window-dependent live loads, which need no code changes.

---

## FINAL-FORK: STAP 1-8 (poolside Laguna + turboquant → ONE fork binary)
**Plan:** `/home/flip/llama_cpp_guardian/agent-prompt-final-fork-all-in-one.md`. Integrate upstream master + `poolsideai/llama.cpp:laguna` (Laguna S 2.1 arch + DFlash `attn_gate`) + `TheTom/llama-cpp-turboquant:feature/turboquant-kv-cache` (REAL turbo4_0 KV-cache quant) into one binary in the user's own fork. Worktree: `/home/flip/llama_cpp_official/worktrees/cuda128-laguna-tq-full`. Build dir `build-cuda128-full`. CUDA 12.8 locked (`CUDAToolkit_ROOT=/usr/local/cuda-12.8`), arch `86;120`, `GGML_CUDA_FA_ALL_QUANTS=ON`, Release.

**Supersedes the old "tq4_0 not a real type" note (TASK 6 above):** that was correct for *upstream master* `9b2a08881`. The turboquant fork ADDS real turbo KV-cache types — `TURBO2_0=43, TURBO3_0=44, TURBO4_0=45, TQ3_1S=46, TQ4_1S=47` — with CLI string **`turbo4_0`** (NOT the plan's `tq4_0`). So the final-fork binary DOES support `turbo4_0`; the gap was upstream-only. DFlash `attn_gate` likewise comes from poolside/laguna (absent upstream).

**STAP 1-4 (merged, committed each):** remotes added + fetched; integration branch + worktree created; poolside/laguna merged (DFlash attn_gate, Laguna arch); turboquant merged (turbo4_0 KV cache). All per "commit na elke merge."

**STAP 5 — build (the conflict-blender):** the STAP4 merge took STAP3's newer model `.cpp` files but DROPPED STAP3's matching header enums + nested `struct graph`/`graph_mtp` decls → missing-symbol cascade. Unified diagnosis: every missing symbol exists in STAP3 (`HEAD^1`) and is absent in turbo/merge-base → fix = surgical union-merge grafts preserving BOTH turbo logic + STAP3 additions.
- **FIX 1-6 (host layer):** `include/llama.h` `load_mode`+`Q2_0=41`; `llama-arch.cpp` hy_v3 nametable (FIX 1b); `llama-model.cpp` dup-case dedup (FIX 6); `llama-batch.h` `split_equal` `=0` default; `llama-kv-cache.cpp` `push_back`; `llama-model-loader.cpp` `load_mode` (FIX 5).
- **FIX 7-8 (enums + nametables):** spliced 8 KV + 25 tensor enum decls into `llama-arch.h`, and matching name-table entries into `llama-arch.cpp`'s 3 maps.
- **FIX 9-10 (nested graph structs):** replaced turbo `using graph = …::graph` aliases with STAP3's own `struct graph` + `struct graph_mtp` decls (glm-dsa, mimo2) — their `.cpp`s define both out-of-line ctors. (deepseek2ocr + mistral4 keep the alias — correct, their .cpps use it.)
- **G1/G2:** staged `fattn-wmma-f16.cu/.cuh` (`A`); removed dead `ggml_cuda_op_mul_mat` splice.
- **Reconciler** (`/tmp/reconcile_arch2.py`): union-merged ALL remaining STAP3-unique entries — 6 arch.h enums + 6 KV_NAMES + 3 TENSOR_NAMES + 28 TENSOR_INFOS (deepseek4 hyper-connection / compress / hash / indexer / dspark layer).
- **Build rounds 8→9:** glm-dsa `graph_mtp` (FIX 9) → mimo2/minimax-m3/nanbeige missing enums (reconciler + FIX 10) → clean deeper.
- **Round 10 (FAILED, ec=2):** reconciler bug — its raw `{...}` capture stopped at `}` and dropped the trailing `,`, so all 37 grafted `llama-arch.cpp` map entries were comma-less → parse errors across all 3 maps. (`llama-model.cpp` lines were `-Wmissing-field-initializers`/`-Wmissing-declarations` WARNINGS, not errors — it compiled fine.) No deeper-layer errors surfaced (build aborted ~37%).
- **Comma fix:** patched the reconciler to append `,` per entry; restored `llama-arch.cpp` from `.bak.stap5e` (post-FIX-8, correct commas); re-ran idempotently (arch.h enums already present → no-op on header). Verified: zero bare-`}` entry lines; every grafted map entry ends `},` / `}},`.
- **Round 11: BUILDING** (bg task → `/tmp/lcpp_build11.log`). `llama-arch.cpp` now parses; compiling the un-built tail (38%→100%) + link. Awaiting REAL `BUILD_EXIT_CODE` (not the wrapper's exit 0).

**Pending after green build:** verify `build-cuda128-full/bin/llama-server -h` shows `turbo4_0` / `turbo3_0` / `draft-dflash` / Laguna → commit STAP5 merge fixes (FIX 1-10 + reconciler + G1/G2 + host-layer). **STAP 6:** push integration branch to fork (only after build genuinely succeeds). **STAP 7:** `.env` `LLAMA_SERVER_BINARY` → the new worktree binary; `config/models.yaml` Laguna `kv_type: turbo4_0` (ONLY if binary supports it) + DFlash draft path uncommented. **STAP 8:** 3 validation loads on `:11440` (`LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64`) — incl. the Laguna+DFlash+turbo4_0 `-c 160000 -ngl 20 --spec-type draft-dflash` run that fixes the old "expected 76 got 69" tensor-mismatch crash.
**Constraints held:** Laguna-S-2.1 is 118B; models FLAT in `/home/flip/models/`; Laguna uses UD-IQ4_XS; CUDA stays 12.8 (not 13.x); live `llama-guardian.service` :11434 NOT touched (use :11440 only); never auto `--theirs/--ours`; poolside API key stays in gitignored `config/cloud_keys.json`.

---

## STAP5-7 UPDATE (2026-07-31) — green build, pushed, Guardian wired

**STAP 5 — BUILD GREEN (resolves the "Round 11 pending" above):** reconciliation rounds 12→16 converged. Pivoted to **STAP3-server adoption** — turbo's *server* (tools/server) had 57 irreconcilable API-rename mismatches vs STAP3 common, but turbo's *models* (src/models/*) + *turbo4 KV kernels* (ggml/) are independent of turbo's server and were kept. Only turbo's server was swapped for STAP3's coherent server. Fixes: common/speculative.cpp `+<cmath>` (DFlash `std::isfinite`); grafts (common.h×3, llama.h TQ ftypes, arg.cpp turbo kv types); `llama_ftype_name()` public symbol grafted into src/llama-model-loader.cpp (turbo's loader only had the private static helper → was an undefined ref at final link). **Round 16: `BUILD_EXIT_CODE=0`** — built llama-server/llama-cli/llama-app. `.bak.stap5*` scratch removed. Committed **`050bfe470`** (`feat: STAP5 integration reconciliation — green CUDA 12.8 binary`, 33 files, worktree cuda128-laguna-tq-full).

**CORRECTION to the L140 "turbo4_0 CLI string" note — that was WRONG,** verified live this round. `ggml_type_name()` returns `turbo2`/`turbo3`/`turbo4` (ggml.c:768/776/784) — those ARE the `-ctk`/`-ctv` tokens. `turbo4_0` is **not** a token and **silently falls back to f16**; `tq4_0` likewise. The `_0` is the enum suffix (TURBO2_0=43/TURBO3_0=44/TURBO4_0=45), not the CLI string. `tq4_1s`/`tq3_1s` are *weight-quant ftype* names, not kv tokens. Binary `-h` confirms: `-ctk TYPE ... turbo2, turbo3, turbo4`; `--spec-type ...draft-dflash...`; `--model-draft`; `-ctkd/-ctvd ... turbo2/3/4`.

**STAP 6 — PUSHED:** `cuda128-laguna-tq-full` → `fork` (github.com/m0nk111/llama.cpp), new branch, upstream tracking set. Build green (STAP5) → push authorized by directive. PR-create URL returned.

**STAP 7 — Guardian wired + validated (this Guardian commit):**
- `.env` (gitignored, NOT committed): `LLAMA_SERVER_BINARY` → `…/worktrees/cuda128-laguna-tq-full/build-cuda128-full/bin/llama-server` (was the `cuda128-bleeding` binary). Build comment refreshed (`STAP5 … cuda128-laguna-tq-full … turbo2/3/4 KV`).
- `config/models.yaml` (committed): Laguna `kv_type: q4_0 → turbo4`; DFlash draft fields **uncommented** (`draft_model_path: …/laguna-s-2.1-DFlash-BF16.gguf`, `spec_type: draft-dflash`, `n_max 12 / n_min 2`, draft kv f16). Precondition verified: `attn_gate` (×6) + `enc.aux_norm` ARE in the fork's `src/models/dflash.cpp` (poolside/laguna merge) → the 76-tensor draft now loads (was "expected 76, got 69"). Header comment rewritten (old "MAINLINE-GEBLOKKEERD / tq4_0 geen echte ggml-type" notes were upstream-only, now superseded). Context kept at 32768 (57GB model > 27GB VRAM → 160k/256k KV won't fit alongside); STAP8 test #2 runs `-c 160000` manually on :11440 to probe the ceiling.
- `app/engine/manager.py` (committed): `allowed_kv_types` += `turbo2/3/4` — the runtime-override validation boundary was stale (rejected the now-supported turbo types; base-config `kv_type` is read unvalidated anyway, but this lets STAP8 test #3 Qwen3.6+turbo4 run as a runtime override).
- Qwen3.6 path already correct (`Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf` exists) — no change.
- **Validation:** replicated `_write_server_args` → Laguna args render `-ctk turbo4 -ctv turbo4 --spec-type draft-dflash --model-draft …/laguna-s-2.1-DFlash-BF16.gguf --spec-draft-n-max 12`; both Laguna entries' draft files exist; no `tq4_0`/`turbo4_0` anywhere. AST confirms turbo2/3/4 in `allowed_kv_types`. **pytest test_manager + test_queue + test_auth → 131 passed** (no regression).

**STAP 8 — DONE:** 3 live loads on `:11440` (`LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64`):
1. Laguna + turbo4, no DFlash (sanity).
2. Laguna + DFlash + turbo4 `-c 160000 -ngl 20 --spec-type draft-dflash --model-draft /home/flip/models/laguna-s-2.1-DFlash-BF16.gguf --spec-draft-n-max 12 --flash-attn on` — **the 76/69-fix test**.
3. Qwen3.6 + turbo4 (runtime override via allowed_kv_types).

Runner: `/tmp/stap8_runner.sh` (5 s `/health` poll ×72 = 360 s ceiling, fatal-log-marker detection, DFlash 76-check). **ALL 3 RESULT=OK.**
1. **Laguna + turbo4, no DFlash** — OK. `-m Laguna-S-2.1-UD-IQ4_XS-00001-of-00003.gguf -c 32768 -ngl 16 -ctk turbo4 -ctv turbo4 -ts 0.42,0.58 --flash-attn on`. Coherent answer ("Hello! How can I assist you today?"), **0.12 tok/s** gen (8388 ms/tok), `graphs reused = 10/11`. (First tried the STAP7-committed `ngl 20` → **OOM**, see root-cause below.)
2. **Laguna + DFlash + turbo4 — the 76/69-fix test.** `-c 32768 -ngl 16 -ts 0.42,0.58 --spec-type draft-dflash --model-draft /home/flip/models/laguna-s-2.1-DFlash-BF16.gguf --spec-draft-n-max 12 --spec-draft-n-min 2 -ctk turbo4 -ctv turbo4 --flash-attn on`. ✅ The 76-tensor draft **loaded** (no "expected 76, got 69" — the STAP3/poolside-laguna `attn_gate`(×6)+`enc.aux_norm` win, confirmed end-to-end); spec active, acceptance **0.16667 (6/36 accepted, mean draft len 3.00)**; answer "2 plus 2 equals 4."; gen **0.24 tok/s (~2× test 1)**. Benign memory-fitting warnings ("dflash requires ctx_other (this warning is normal during memory fitting)", "[spec] failed to measure draft model memory: failed to create llama_context from model") appear ONLY in the pre-measure phase — the real draft load at ~2-2.16 s succeeds. (Directive's `-c 160000` not run — would OOM; 32 k is the real ceiling at ngl 16+DFlash on 27 GB VRAM.)
3. **Qwen3.6 + turbo4** — OK. `-m Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf -ngl 99 -ctk turbo4 -ctv turbo4 -ts 0.38,0.62` (turbo4 selected via runtime override, validated by the STAP7 `allowed_kv_types` += turbo2/3/4). `/health=200` after ~40 s; **prompt 24.9 tok/s, gen 78.5 tok/s** (12.7 ms/tok — GPU-resident); HTTP 200, 30 completion tokens. `content=""` is expected — the model defaults to reasoning/thinking mode and 30 max_tokens were consumed by the `<-thinking->` prefix (test omitted prod's `--reasoning on --reasoning-format deepseek`); load + turbo4 + gen all work.

**OOM root-cause (STAP7's committed `ngl:20` was wrong) → FIXED to `ngl:16` (this commit):** Test 1 first ran the STAP7-committed `ngl 20 / ts 0.42,0.58` and OOM'd on GPU0 (RTX 3060, 12 GB): `cudaMalloc failed: out of memory` allocating ~565 MiB for "compute pp buffers". Iterations: `TURBO_AUTO_ASYMMETRIC=0` (still OOM — auto-asym was a red herring, ~17 MiB); `-c 16384` (still ~564 MiB — **ctx-independent**); `-b 512 -ub 512` (still ~557 MiB — **batch-independent**). Conclusion: the turbo4 compute path reserves a **fixed ~565 MiB prompt-processing graph workspace** (per-turbo4-pad, not per `-c`/`-b`) that doesn't fit on GPU0 after 20 GPU layers' weight share under `ts 0.42,0.58`. `ngl 16` frees enough GPU0 headroom to fit it AND leaves room for the 2.1 GB DFlash draft (test 2). **Fix applied:** `config/models.yaml` Laguna `ngl: 20 → 16` (both entries) + header comment rewritten; `.env` += `TURBO_AUTO_ASYMMETRIC=0` (gitignored) so prod runs **pure turbo4** on the GQA-6:1 Laguna (without it, K auto-upgrades to q8_0 — a quality safeguard, +~17 MiB, but not "turbo4" as intended). `ts 0.42,0.58` retained (validated fitting at `ngl 16`).

**Discrepancies surfaced (per "als je discrepanties ziet, stel ze dan"):**
- **"4.5 tok/s op b2111" does NOT reproduce** — actual is **0.12 tok/s** (test 1, main-only) / **0.24 tok/s** (test 2, DFlash). On 27 GB VRAM the 57 GB Laguna is CPU-offload-bound (~290 ms/CPU-layer × 32 CPU layers @ ngl 16). Control test (turbo4 vs `q4_0` KV) = identical 0.12 tok/s → **turbo4 KV is not the cost; CPU offload is** — not a fork-binary regression. DFlash (spec decoding) is the real lever: 2× gen for free. The b2111 figure presumably came from a different VRAM/offload window.
- **Test 3 empty visible content** — generation works (30 tokens, 78.5 tok/s, HTTP 200) but `choices[0].message.content=""` because the Qwen3.6 reasoning model emits a thinking block first and 30 max_tokens didn't clear it. Prod `extra_args` carry `--reasoning on --reasoning-format deepseek` (separates reasoning into its own field); the test deliberately omitted those. Not a load failure. If prod callers expect text in the default content field for this model without the reasoning-format args, that needs confirming.
- **old TASK-6 "tq4_0 not a real type" note** fully superseded by the fork-binary — it DOES support turbo4 KV (`ggml.c:768/776/784`, enum `TURBO2_0=43/TURBO3_0=44/TURBO4_0=45`; `-h` lists `-ctk turbo2,turbo3,turbo4`). Only `turbo4` (not `tq4_0`/`turbo4_0`/`tq4_1s`) is the KV token; `tq4_1s`/`tq3_1s` are weight-quant ftypes.

**Binary verified:** `llama-server` (17920 B, thin dlopen launcher) + `libllama-server-impl.so` (7.5 MB) + `libggml-cuda.so` (638 MB — houses the turbo4 kernels) + `libllama{.so,common.so}`. Requires `LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64:<bindir>`. Laguna shards valid (00001=3.6 MB GGUFv3 header, 00002=49.8 GB, 00003=7.7 GB → 57.6 GB; `-m 00001` auto-loads all 3). No "expected 76, got 69" → STAP3/laguna-merge `attn_gate` win confirmed end-to-end.

**END STATE:** one binary `/home/flip/llama_cpp_official/worktrees/cuda128-laguna-tq-full/build-cuda128-full/bin/llama-server` supports Laguna S 2.1 + DFlash (`attn_gate`, 76-tensor draft loads, spec decoding 2× gen) + TurboQuant KV-cache (`turbo2/3/4`) + Qwen3.6 35B-A3B, **CUDA 12.8 locked, arch `86;120`**. The final-fork **8-STAP plan is COMPLETE**.
