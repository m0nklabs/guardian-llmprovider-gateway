# Changelog

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
