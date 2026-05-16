# Changelog

## [Unreleased]

### Added
- Public liveness probe `GET /healthz` (no auth) returning `{"ok": true}` for external monitors (monifuse, uptime checks). Does not reflect llama-server backend health; for that use the auth-gated `/api/status`.
- Qwen3.6 Agent profile and `qwen3-35b-uncensored-agent` alias using Qwen's official non-thinking llama.cpp chat template for low-latency tool-facing agents.
- Bounded Qwen3.6 reasoning agent profile and `qwen3-35b-reasoning-agent` alias with 65k context and a 2048-token reasoning budget for daily local-agent work.
- Gemma4 31B uncensored max-reasoning Agent Zero profile based on `TrevorJS/gemma-4-31B-it-uncensored`, with unrestricted reasoning and anti-repeat sampler settings under `gemma4-31b-uncensored-max-agent`.
- Explicit `gemma4-26b-agent` alias for the stable 26B Agent Zero route; the 31B uncensored route remains opt-in as `gemma4-31b-uncensored-max-agent`.
- Gemma4 Agent now uses the same multimodal projector as the proven OpenWebUI Gemma4 profile so Agent Zero can route image tasks through the bounded agent alias.

### Changed
- Kept `qwen3-35b-uncensored` as the unrestricted deep-reasoning alias while adding a bounded 65k-context agent variant for Agent Zero/OpenAI-compatible tool clients.
- Raised both Qwen3.6 CrewAI/agent-facing aliases (`qwen3-35b-uncensored-agent` and `qwen3-35b-reasoning-agent`) from 65k to their full 131072-token context so long CrewAI traces stop tripping Guardian with context-overflow 400s.
- Restored `gemma4-agent` to the stable 26B Agent Zero profile after the 31B uncensored route proved too slow for default AZ work.

### Fixed
- Guardian now starts answering on `11434` immediately after restart by running startup model verification in the background instead of holding FastAPI startup open until `llama-server` on `11440` finishes warming up.
- Guardian no longer kills `systemctl --user restart llama-guardian-live.service` because of a momentarily live `guardian.pid`; the PID-file guard now overwrites old entries and relies on socket binding to reject real duplicate listeners.
- `/admin/load` now accepts public model aliases such as `qwen3-35b-uncensored` and serializes manual loads behind the shared model-switch lock so operator-triggered loads cannot race the background startup check.
- Guardian runtime status now uses generation-tracked operation snapshots, so an older background startup task cannot overwrite a newer manual load or auto-switch status in `/api/status`.
- `/api/status` now exposes richer proxy/routing diagnostics, including the live listener owner, pid-file state, preferred tool/reasoning models, and backend verification metadata for faster live debugging.
- `/api/status` now also exposes explicit switch-state diagnostics (`pending`/`checking`/`switching`/`ready`), queue state, current requested target, switch owner, and the last successful backend verification timestamp.
- Auto-routed inference requests now prefer a tool-friendly sibling profile when the current family is an unbounded reasoning model, which keeps `model: auto` practical for tool clients without changing explicit model requests.
- Ollama-compatible `/api/chat` and `/api/generate` responses now fall back to `reasoning_content` when a reasoning model emits no visible `content`, so tool clients no longer see a misleading empty answer.
- The live integration suite now includes a restart-race regression that restarts the active Guardian systemd unit, immediately issues `/admin/load` with an alias, checks `/api/status`, and runs a mini chat request.
- `/api/status` now exposes a `startup` object so authenticated clients can tell whether Guardian is still verifying the backend model, already ready, or ended startup with an error.
- Claude Code can now switch Guardian models with its dedicated `claudecode_*` API key instead of inheriting whichever sibling app model was already loaded, which prevented restarts from getting stuck on NerveSplat's lighter `gemma4-e4b` runtime.
- Anthropic-compatible clients such as Claude Code now authenticate successfully through Guardian because the proxy accepts `x-api-key` and `api-key` headers in addition to OpenAI-style `Authorization: Bearer` tokens.
- OpenAI-compatible inference requests now detect a stale stopped `llama-server` backend, reload the active model once, and retry instead of leaking an ASGI 500 traceback to Agent Zero/LiteLLM clients.
- Startup model detection now distinguishes profiles that share the same GGUF path by matching generated runtime args, preventing the non-thinking Qwen agent profile from being mistaken for the deep-reasoning profile after Guardian restarts.
- Forced a live unload/reload of the Qwen3.6 reasoning agent after the context bump and verified Guardian rewrote `current_model.args` to `-c 131072`, confirming the hotfix was actually live instead of only sitting in YAML.
- `/v1/models` now includes configured aliases alongside canonical model names, so clients that talk to Guardian through IDs such as `qwen3-35b-uncensored` can resolve metadata for the exact model string they send.
- Guardian runtime sizing now treats `context` as the only active runtime window; `benchmark_context_limit` is treated as a separate benchmark or paper ceiling instead of feeding the advertised runtime headroom calculation.
- `/v1/models` now exposes the benchmark ceiling under the clearer `benchmark_context_limit` field alongside the configured runtime `context` and the conservative `advertised_context` headroom field.
- Claude Code specifically still receives the conservative `advertised_context` value through the OpenAI-compatible `max_context` response field because this Claude build compacts against that field only; the response keeps the explicit runtime and benchmark fields visible next to that compatibility override.

## [2026-05-06] - Model Registry Cleanup, Qwen 3.6, Gemma Deep, and Load Guard

### Added
- **Qwen3.6 uncensored profile**: Registered `Qwen3.6-35B-A3B-HauhauCS-Aggressive` with 131k context, GPU KV offload enabled, unrestricted reasoning, and `qwen3-35b-uncensored` alias.
- **Gemma4 Heretic Deep profile**: Added `gemma-4-31B-it-uncensored-heretic-Deep` / `gemma4-heretic-deep` as a text-focused reasoning profile with 216k context, GPU KV offload enabled, unrestricted reasoning, and no multimodal projection overhead.
- **Gemma4 E4B profile**: Added `gemma-4-E4B-it-uncensored` / `gemma4-e4b` for a smaller text-only Gemma profile.

### Changed
- **Gemma Deep VRAM tuning**: Tuned the Deep profile to `context: 216064` and `tensor_split: "0.62,0.38"`, leaving measured runtime headroom on both GPUs while using substantially more context than the initial 131k load.
- **Model registry cleanup**: Removed obsolete GLM 4.7 entries and aliases after the GLM models were retired from the local Guardian registry.
- **Alias cleanup**: Removed orphaned aliases that pointed at deleted model entries.
- **README model examples**: Updated `config/models.yaml` documentation to reflect the current Qwen/Gemma registry instead of retired GLM examples.

### Fixed
- **Admin load idle-unload race**: `/admin/load` now increments `active_requests` and refreshes `last_request_time` during model loads so the idle-unload watcher does not terminate `llama-server` in the middle of heavy loads.

### Verified
- `models.yaml` parses successfully and all aliases resolve to existing model entries.
- `gemma-4-31B-it-uncensored-heretic-Deep` loads through Guardian at 216k context with backend health reporting `true`.
- Short `/v1/chat/completions` smoke test succeeds through Guardian on the Gemma Deep profile.

## [2026-04-17] - Backend Strategy Flip, Middleware Rebrand & Documentation Overhaul

### Changed
- **Backend strategy flipped**: Official llama.cpp is now the PRIMARY backend; ik_llama.cpp fork is FALLBACK. `DEFAULT_BACKEND` changed from `"ik_fork"` to `"official"` in `manager.py`.
- **Middleware rebrand**: Guardian is now positioned as middleware (not proxy). Logger renamed from `"Proxy"` to `"Guardian"` in `server.py`.
- **3rd-party GPU process awareness**: Replaced Frigate-specific language with generalized "3rd-party GPU process" awareness throughout configuration and documentation.
- **models.yaml cleanup**: Removed explicit `backend: official` from all 10 models that had it — they now use the default (official).
- **README.md**: Complete rewrite — middleware positioning, queue system documentation, dual backend strategy, 3rd-party GPU awareness, full API reference, directory structure.
- **ARCHITECTURE.md**: Complete rewrite — detailed queue architecture, cooperative VRAM management, backend selection, GPU strategy, timeout tiers, model lifecycle flows.
- **CLIENT_INTEGRATION.md**: Updated heading to reflect middleware terminology.

### Added
- GitHub issue #1: 5-phase roadmap for Guardian improvements (backend flip, middleware rebrand, 3rd-party awareness, docs, future roadmap).

## [2026-03-31] - Cooperative VRAM Management & Documentation Overhaul

### Added
- **Cooperative VRAM management**: Guardian now calls ComfyUI's `POST /free` API to request graceful VRAM release before loading models. ComfyUI stays alive and auto-reloads models on next workflow.
- **`_request_comfyui_free()`**: New method in `ModelManager` that sends `{"unload_models": true, "free_memory": true}` to `http://127.0.0.1:8188/free` with 10s timeout and graceful error handling.
- **`_free_gpu_memory()`**: Orchestrator method that coordinates VRAM cleanup from coexisting services before model loads.
- **Hydroponics API key**: Added `hydro_` prefixed key for Mycodo/Pi4 nutrient automation integration.

### Changed
- **README.md**: Complete rewrite with full API reference table, directory structure, cooperative VRAM management docs, GPU configuration details, and all current features.
- **ARCHITECTURE.md**: Complete rewrite reflecting cooperative VRAM management (ComfyUI /free integration), VramScheduler, timeout tiers, backend verification flow, model switch sequence diagram, and implementation notes.
- **Model load flow**: `load()` and `switch_model()` now call `_free_gpu_memory()` before `_start_server()` to ensure VRAM availability.

### Design Decision
- **Cooperative over destructive**: Instead of killing GPU processes (ComfyUI, etc.), Guardian politely requests VRAM release via API calls. This preserves service uptime and lets ComfyUI auto-recover its models on the next workflow execution.

## [2026-02-16] - Comprehensive Code Review & Multi-GPU Fixes

### Fixed (CRITICAL)
- **Unreachable code in `get_model_size()`**: `return 8000` was placed before embed/0.5b checks, causing embed models (e.g., nomic-embed) to report 8000MB instead of 500MB.
- **Default model `"glm-4"` didn't exist**: Changed to `"GLM-4.7-Flash"` to match actual `models.yaml` key.
- **Benchmark suite non-functional**: Was using Ollama `/api/generate` endpoint (404 on llama-server). Migrated to `/v1/chat/completions` with OpenAI-format response parsing.
- **Benchmark model names**: Were Ollama-style (`deepseek-r1:32b`). Now loaded dynamically from `models.yaml`.
- **Model switch race condition**: Added `asyncio.Lock()` to prevent concurrent model switches from colliding.

### Fixed (IMPORTANT)
- **Dead config `vram_limit_mb`**: `settings.yaml` value (27000) was never read — `server.py` hardcoded 26000. Now properly loaded from config.
- **Dead config `proxy.port` and `proxy.target`**: Documented as config-driven but were hardcoded. `vram_limit_mb` now wired; port/target remain hardcoded (intentional).
- **Scheduler ignored `settings.yaml`**: Hours, days, and services were hardcoded. Now reads `benchmark.schedule` and `services_to_stop` from config.
- **`manage_service()` was a no-op**: `subprocess.run()` was commented out. Re-enabled with timeout protection.
- **Unauthenticated endpoints**: `/api/tags` and `/api/version` bypassed API key auth. Fixed.
- **Benchmark blocked event loop**: Sync `requests.post()` inside async `run_suite()`. Fixed via `asyncio.to_thread()` + migrated from `requests` to `httpx`.

### Added
- **`tensor_split` for all >12GB models**: 16 models configured with multi-GPU weight distribution (`0.55,0.45` for ≤19GB, `0.45,0.55` for >20GB). Enables coexistence with Frigate NVR on GPU 1.
- **`_model_switch_lock`**: Global asyncio lock prevents concurrent model switches across `/api/chat` and `/v1/chat/completions`.

### Removed
- Unused imports: `secrets`, `base64`, `BackgroundTask`, `HTTPBasic`, `HTTPBasicCredentials`
- Dead constants: `DEFAULT_CONTEXT_SIZE`, `MAX_CONCURRENT_REQUESTS`, `MAX_REQUEST_TIMEOUT`, `STATS_FILE`, `CLIENTS_FILE`
- Dead functions: `unload_model()` (used Ollama API), `update_model_stats()` (no-op), `check_and_free_vram()` (no-op)
- Stale `# ...existing code...` placeholder comments

### Changed
- **`start_llama.sh`**: Fixed default model filename from `GLM-4.7-Flash-Q4_K_M-latest.gguf` to `GLM-4.7-Flash-Q4_K_M.gguf`.
- **`settings.yaml`**: Cleaned dead `benchmark.models` list (now loaded from `models.yaml`), added VRAM documentation comments.
- **README.md**: Complete rewrite reflecting current architecture, dual-backend system, multi-GPU setup, and all features.

## [2026-02-14] - Refactor to Llama Server

### Changed
- **Ollama to Llama Server**: Renamed all component references from "Ollama" to "Llama Server" to reflect the backend change.
- **Port standardization**: Default internal Llama Server port updated to 11440.
- **Environment Variables**: Renamed `OLLAMA_URL` and similar vars to `LLAMA_SERVER_URL`.
- **Legacy Cleanup**: Removed deprecated `configure_ollama.sh` and `modelfile_template.txt`.
- **VRAM Logic**: Disabled legacy `check_and_free_vram` in favor of new manager.

## [Unreleased] - 2025-12-21

### Added
- **Configurable Timeout Tiers**: Timeout values per model tier are now configurable in `config/settings.yaml` under `timeouts.tiers`. Each tier has `min_size_mb` and `timeout_seconds` settings.
- **Benchmark Visualization in UI**: Dashboard now visualizes benchmark results (best TPS per model + last-run metadata) via a new `/api/benchmark` endpoint.
- **Manual Benchmark Control**: Added `/api/benchmark/start` and `/api/benchmark/stop` to run benchmarks on-demand.

### Changed
- **Dynamic Timeouts**: Refactored `get_model_timeout()` to read from config file instead of hardcoded values. Supports hot-reload via config file changes.
- **Benchmark Resuming Behavior**: Benchmark queue is regenerated from current settings and filtered by completed tests to avoid no-op runs when the persisted queue is empty/stale.

---

## [2025-12-03]

### Added
- **Feedback Loop**: Implemented `RequestOptimizer` which injects the best `num_ctx` and `num_batch` settings from `benchmark_results.json` into incoming requests.
- **Smart Combo Caching**: Implemented LRU (Least Recently Used) eviction policy. Models are only unloaded if VRAM is actually needed.
- **Multi-GPU Support**: Updated VRAM monitoring to sum memory across all available GPUs.
- **Triple Hit Verification**: Added `scripts/test_combo.py` to verify concurrent model loading.
- **Dashboard UI**: Real-time monitoring dashboard on port 11437 (Dark Mode, Tailwind).
- **Record Alerts**: Benchmark suite now logs "🏆 NEW RECORD" when TPS improves.
- **API Stats**: Added `/api/stats` endpoint for frontend integration.
- **Architecture Docs**: Updated `ARCHITECTURE.md` with port mappings and flow diagrams.

### Fixed
- **Service Architecture**: Moved Guardian to port 11435 to avoid conflict with Nginx (which proxies 11434 -> 11435).
- **Crash Loop**: Fixed missing imports and initialization errors in `app/proxy/server.py`.
- **VRAM Monitoring**: Replaced static estimates with real-time `nvidia-smi` queries.

### Changed
- **Port Migration**: Guardian now listens on port 11434 (replacing Nginx/Ollama default).
- **Nginx**: Disabled Nginx Ollama config to allow Guardian to take over the entry port.
- **Architecture**: Simplified flow: Client -> Guardian (11434) -> Ollama (11436).
