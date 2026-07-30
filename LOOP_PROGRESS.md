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
