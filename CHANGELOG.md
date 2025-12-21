# Changelog

## [Unreleased] - 2025-12-21

### Added
- **Configurable Timeout Tiers**: Timeout values per model tier are now configurable in `config/settings.yaml` under `timeouts.tiers`. Each tier has `min_size_mb` and `timeout_seconds` settings.

### Changed
- **Dynamic Timeouts**: Refactored `get_model_timeout()` to read from config file instead of hardcoded values. Supports hot-reload via config file changes.

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
