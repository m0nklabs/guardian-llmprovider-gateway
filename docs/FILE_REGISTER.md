# FILE_REGISTER.md — Living File Register

> **Status: DRAFT (F1) — work in progress.**
> This document is the *living file register* for the **guardian-llmprovider-gateway**
> repository (part of Guardian 2.0 issue #1, phase F1). It lists **every tracked file**,
> its function (one line), and the processes/files it relates to (one line). It is
> intentionally a **starting-point draft**: later phases refine descriptions, prune
> stale entries, and add cross-links as the codebase evolves.
>
> Generated from the authoritative `git ls-files` list (**194 tracked files**). The
> register is best-effort and derives function descriptions from module docstrings,
> filenames, and the `AGENTS.md` directory map — it is not a substitute for reading
> the code.

---

## app/ — FastAPI inference gateway and capture subsystem (60 files)

### app/ root (4)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/__init__.py` | Package marker for the `app` package | — |
| `app/main.py` | FastAPI/uvicorn entrypoint; builds the app, mounts dashboard UI + static, signals | `app/proxy/server.py`, `scripts/start_llama.sh` |
| `app/paths.py` | Central path resolution (`REPO_ROOT`, `CONFIG_DIR`, `MODELS_DIR`, env overrides) | `config/*`, env vars `GUARDIAN_LLMPROVIDER_GATEWAY_*` |
| `app/config_loader.py` | Loads & deep-merges `global.settings.yaml` + provider files into shared CONFIG; typed accessors | `config/*.settings.yaml`, `app/paths.py` |

### app/capture/ — privacy-aware capture subsystem (11)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/capture/__init__.py` | Package marker for capture subsystem | — |
| `app/capture/config.py` | Validated capture configuration from `global.settings.yaml` `capture:` section | `config/global.settings.yaml` |
| `app/capture/gzip_reader.py` | Crash-tolerant multi-member gzip reader for the capture WAL | `app/capture/wal_writer.py` |
| `app/capture/integration.py` | Thin adapters bridging request lifecycle to the capture controller | `app/capture/*`, route handlers |
| `app/capture/media.py` | Media/raw-image extraction stored out-of-band with WAL refs | `app/capture/wal_writer.py`, `data/capture/` |
| `app/capture/policy.py` | Determines whether a request is captured (after auth/model resolution) | `config/global.settings.yaml`, `app/capture/config.py` |
| `app/capture/redactor.py` | Mandatory credential/PII/sensitive redaction before capture | `app/capture/policy.py`, `app/capture/schema.py` |
| `app/capture/schema.py` | `guardian_capture_v1` event schema, deterministic IDs, builders | Keanu capture contract JSONs, `docs/GUARDIAN_KEANU_CAPTURE_PLAN.json` |
| `app/capture/sink.py` | Bounded non-blocking event queue decoupling producers from WAL | `app/capture/wal_writer.py` |
| `app/capture/stream_assembler.py` | Accumulates SSE deltas into a final semantic response | `app/gateway/streaming.py` |
| `app/capture/wal_writer.py` | Append-only JSONL WAL writer with rotation, retention, HMAC auth | `app/capture/sink.py`, `data/capture/` |

### app/cloud_inference/ — cloud routing & forwarding (3)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/cloud_inference/__init__.py` | Package marker | — |
| `app/cloud_inference/forwarding.py` | Full upstream forwarding for cloud routes (streaming/non-streaming, failover, 429) | `app/proxy/cloud_catalog.py`, `app/proxy/ratelimit.py`, `app/proxy/anthropic_bridge.py` |
| `app/cloud_inference/routing.py` | Cloud attempt resolution, candidate preparation, capture setup | `app/proxy/failover.py`, `app/gateway/routing.py` |

### app/engine/ — local llama-server lifecycle (1)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/engine/manager.py` | llama-server lifecycle (spawn/start/stop/reload, launch-signature drift reload, VRAM) | `scripts/start_llama.sh`, `app/proxy/process.py`, `app/scheduler/manager.py` |

### app/gateway/ — Phase-5 extracted routing logic (13)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/gateway/__init__.py` | Package marker | — |
| `app/gateway/admin_api.py` | Admin/status/credential/scaler/queue/capture/keys handlers | `app/proxy/server.py`, `app/proxy/auth.py` |
| `app/gateway/capture_dispatch.py` | Capture event dispatch hooks (fail-open wrappers) | `app/capture/*` |
| `app/gateway/caretaker_client.py` | F5 caretaker control-API HTTP client (/ensure, /unload, /status; error hierarchy; fail-closed build) | `m0nklabs/caretaker-llamacpp`, `app/proxy/lifespan.py`, `app/proxy/server.py` |
| `app/gateway/caretaker_runtime.py` | F5 remote-first hotpath lifecycle execution (ensure_backend: /ensure → local fallback, error mapping) | `app/gateway/caretaker_client.py`, `app/engine/manager.py`, `app/gateway/routing.py`, `app/local_inference/ollama.py`, `app/local_inference/models.py` |
| `app/gateway/context_metadata.py` | Context window resolution + model metadata construction | `app/cloud_inference/*`, `config/models.cloud.overrides.yaml` |
| `app/gateway/model_discovery.py` | `/api/tags`, `/v1/models`, `/api/show` handler bodies | `app/proxy/server.py`, `app/proxy/cloud_catalog.py` |
| `app/gateway/normalization.py` | Multimodal preflight, backend error mapping, thinking params, qwen sanitization | `app/gateway/routing.py`, `app/local_inference/ollama.py` |
| `app/gateway/queue_helpers.py` | Request lifecycle, disconnect watch, cancel cleanup | `app/proxy/queue.py` |
| `app/gateway/routing.py` | `/v1/{path}` dispatch node (count_tokens, cloud/local, vision fallback, queues; F5 remote-first auto-reload/switch) | `app/proxy/server.py`, all gateway/cloud/local modules |
| `app/gateway/sessions.py` | Session slot save/load/list + filename sanitizer | `app/local_inference/models.py` |
| `app/gateway/streaming.py` | SSE watchdog, keepalives, Anthropic enrichment | `app/proxy/anthropic_bridge.py` |
| `app/gateway/usage.py` | Live usage request lifecycle + token accounting | `app/proxy/usage.py`, dashboard API |

### app/local_inference/ — local llama-server bridges (3)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/local_inference/__init__.py` | Package marker | — |
| `app/local_inference/models.py` | Local model resolution, size heuristics, timeouts, VRAM scheduler, backend reload | `config/models.local.settings.yaml`, `app/engine/manager.py` |
| `app/local_inference/ollama.py` | Ollama-protocol `/api/chat` + `/api/generate` bridges, SSE translation | `app/gateway/routing.py`, `app/proxy/queue.py` |

### app/proxy/ — core server shell & supporting services (16)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/proxy/__init__.py` | Package marker | — |
| `app/proxy/anthropic_bridge.py` | Anthropic ↔ OpenAI SSE translation + ping keepalives | `app/gateway/streaming.py` |
| `app/proxy/auth.py` | API key verification against `guardian.keys.yaml` | `config/guardian.keys.yaml` |
| `app/proxy/cloud_catalog.py` | Dynamic cloud model catalog from each provider `/v1/models` (+cold-start disk cache) | `data/cloud_catalog_cache.json`, `config/providers.settings.yaml` |
| `app/proxy/failover.py` | Cross-provider failover registry, health tracking, candidate ordering | `config/global.settings.yaml`, `app/cloud_inference/routing.py` |
| `app/proxy/lifespan.py` | FastAPI startup/shutdown orchestration + idle-unload watcher | `app/proxy/process.py`, `app/engine/manager.py` |
| `app/proxy/metrics.py` | Prometheus counters/gauges/histograms via `/metrics` | dashboard, monitoring |
| `app/proxy/optimizer.py` | Static/dynamic optimisation helpers (e.g. context/reasoning params) | `app/cloud_inference/*` |
| `app/proxy/process.py` | pid files, listener inspection, stale termination, startup-check state | `app/engine/manager.py`, `app/proxy/paths.py` |
| `app/proxy/providers.py` | Cloud provider registry (exact + prefix model recognition) | `config/providers.overrides.yaml`, `config/models.cloud.overrides.yaml` |
| `app/proxy/queue.py` | FIFO inference queue serializing access to single-slot llama-server | `app/gateway/queue_helpers.py` |
| `app/proxy/ratelimit.py` | Per-key cloud rate-limit backoff/retry on upstream 429 | `app/cloud_inference/forwarding.py` |
| `app/proxy/scaler.py` | Adaptive dynamic request scaler (queue/max-request injection) | `app/proxy/queue.py`, `app/proxy/state.py` |
| `app/proxy/server.py` | Thin app shell: route registration + init() wiring (Phase-5 target) | all extracted gateway/cloud/local modules |
| `app/proxy/state.py` | Runtime `State` container (VRAM scheduler, scaler, optimizer, usage) | app bootstrap wiring |
| `app/proxy/usage.py` | Persistent API usage tracking for dashboard | `app/gateway/usage.py`, dashboard |

### app/scheduler/ — auto switch / idle-unload (1)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/scheduler/manager.py` | Idle-unload + auto model-switch scheduler | `app/engine/manager.py`, `app/proxy/lifespan.py` |

### app/tweaker/ — Finetune v2 + legacy (8)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/tweaker/finetune_v2_cli.py` | CLI entrypoint for Finetune v2 | `finetune_v2.py`, `app/tweaker/finetune_v2_contracts.py` |
| `app/tweaker/finetune_v2_contracts.py` | Finetune v2 data contracts/models | `docs/FINETUNE_V2_REQUIREMENTS.md` |
| `app/tweaker/finetune_v2_runner.py` | Finetune v2 execution runner | `app/tweaker/finetune_v2_support.py` |
| `app/tweaker/finetune_v2_support.py` | Support helpers for the finetune v2 runner | `app/tweaker/finetune_v2_runner.py` |
| `app/tweaker/finetune_v2_telemetry.py` | Finetune v2 telemetry/metrics | `app/tweaker/finetune_v2_runner.py` |
| `app/tweaker/legacy/__init__.py` | Package marker for legacy finetune | — |
| `app/tweaker/legacy/benchmark_suite_v1.py` | Legacy v1 benchmark suite (kept for reference) | `scripts/bench_all_models.py` |
| `app/tweaker/legacy/model_finetune_v1.py` | Legacy v1 model finetune (kept for reference) | `scripts/finetune_model_config.py` |

### app/ui/ — dashboard frontend shell (2)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `app/ui/index.html` | Dashboard HTML shell with key-input modal + auth fetch-wrapper | `dashboard/*`, `app/main.py` |
| `app/ui/static/tailwind.min.css` | Tailwind CSS bundle for the dashboard shell | `app/ui/index.html` |

---

## config/ — runtime configuration (6)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `config/global.settings.yaml` | Global settings: proxy (port/target/pid), queue, timeouts, capture, cloud_retry, failover_health | `app/config_loader.py`, scripts, systemd |
| `config/local_models.yaml` | Model registry for local models | `app/local_inference/models.py` |
| `config/models.cloud.overrides.yaml` | Merged cloud model overrides (context_window, model_defaults) | `app/proxy/providers.py`, `app/gateway/context_metadata.py` |
| `config/models.local.settings.yaml` | Local model registry (aliases, runtime, tensor_split, switch policy) | `app/local_inference/models.py` |
| `config/providers.overrides.yaml` | Per-provider overrides (win over defaults) | `app/config_loader.py`, `app/proxy/providers.py` |
| `config/providers.settings.yaml` | Provider defaults (base_url, timeout, catalog info) | `app/proxy/providers.py`, `app/proxy/cloud_catalog.py` |

*Note: `config/guardian.keys.yaml` (API keys) is gitignored secrets — intentionally not tracked.*

---

## deploy/ — systemd + nginx + TLS (5)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `deploy/nginx/guardian-llmprovider-gateway-dashboard.conf` | nginx LAN reverse-proxy for the dashboard on :11437 | `guardian-llmprovider-gateway-protocol-mux.conf`, dashboard |
| `deploy/nginx/guardian-llmprovider-gateway-loopback-http.conf` | nginx loopback HTTP route for plain-HTTP clients; TLS trusted cert path | `deploy/tls/guardian-192.168.1.35.crt` |
| `deploy/nginx/guardian-llmprovider-gateway-protocol-mux.conf` | nginx stream TLS-preread multiplexer (both protocols on :11434) | `guardian-llmprovider-gateway-loopback-http.conf` |
| `deploy/systemd/guardian-llmprovider-gateway.service.d/20-tls.conf` | systemd drop-in binding TLS server (cert/key paths, host, port) | `deploy/tls/*`, env `GUARDIAN_TLS_*` |
| `deploy/tls/guardian-192.168.1.35.crt` | **TLS certificate identity** (host `guardian-192.168.1.35`) — not a service name, do not rename | `deploy/nginx/*`, `deploy/systemd/*` |

*Note: `deploy/systemd/guardian-llmprovider-gateway.service` (main unit) and the private `.key` are not tracked in this working tree snapshot.*

---

## scripts/ — operator tooling (30)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `scripts/_auth.py` | Shared auth helper for CLI scripts | scripts that hit the API |
| `scripts/_paths.py` | Shared path-resolution helper for CLI scripts | `app/paths.py` |
| `scripts/bench_all_models.py` | Benchmark runner across models | `docs/MODEL_BENCHMARKS.md` |
| `scripts/bench_dflash.sh` | Benchmark helper for dflash binary/model | llama.cpp bench |
| `scripts/bench_fork_binary.sh` | Fork/binary benchmark helper | llama.cpp build |
| `scripts/bench_qwen36_variants.py` | Benchmark qwen3.6 variants | `docs/MODEL_BENCHMARKS.md` |
| `scripts/cleanup_invalid_benchmarks.py` | Clean invalid benchmark results | `data/benchmarks` |
| `scripts/download_new_models.sh` | Download new GGUF models | `config/models.local.settings.yaml` |
| `scripts/finetune_model_config.py` | Generate v1 finetune model config | `app/tweaker/legacy/*` |
| `scripts/finetune_v2_model_config.py` | Generate v2 finetune model config | `app/tweaker/finetune_v2_*` |
| `scripts/generate_contract_wal.py` | Generate synthetic capture WAL fixtures | `tests/fixtures/*`, `app/capture/*` |
| `scripts/generate_key.py` | Mint Guardian API keys into `guardian.keys.yaml` | `config/guardian.keys.yaml` (secret) |
| `scripts/gguf_meta.py` | Inspect GGUF metadata | llama.cpp GGUF files |
| `scripts/guardianctl.py` | Capture subsystem CLI (status/config/files/rotate/enable/disable) | `app/capture/*`, admin API |
| `scripts/keanu_redact.py` | Redaction helper for capture data | `app/capture/redactor.py` |
| `scripts/legacy/test_benchmark_v1.py` | Legacy v1 benchmark test | `app/tweaker/legacy/*` |
| `scripts/needle_test.py` | Needle-in-haystack context test | llama-server |
| `scripts/pre_restart_check.py` | Restart gate: py_compile + pyflakes + signature + full pytest | `app/*`, `scripts/*`, pytest |
| `scripts/recommend_context.py` | Recommend context window sizes | guardrails tuning |
| `scripts/start_llama.sh` | Launch llama-server backend; honors `GUARDIAN_LLMPROVIDER_GATEWAY_*` env | `app/engine/manager.py`, llama-server |
| `scripts/stress_test.py` | Load/stress test | /v1 routes |
| `scripts/sync_models.py` | Sync model registry | `config/models.local.settings.yaml` |
| `scripts/sync_nvidia_free_models.js` | Sync NVIDIA free-filter model list | provider overrides |
| `scripts/test_combo.py` | Combined smoke test | pytest |
| `scripts/test_finetune_v2_contracts.py` | Finetune v2 contracts test | `app/tweaker/finetune_v2_contracts.py` |
| `scripts/test_system.py` | System smoke tests | /api/system |
| `scripts/test_vision_models.py` | Vision-model spot check | multimodal routes |
| `scripts/update_guardian_config.py` | Live config mutation helper | `config/global.settings.yaml` |
| `scripts/verify_post_restart.py` | Post-restart verification (service active, keys, config, catalog) | systemd service, `app/proxy/auth.py`, `app/config_loader.py` |
| `scripts/verify_prompts.py` | Verify prompt/health behavior | /healthz, model routes |

---

## tests/ — pytest suite (46)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `tests/__init__.py` | Test package marker | — |
| `tests/conftest.py` | Pytest fixtures/config | `app/*` |
| `tests/fixtures/capture_fixtures.jsonl` | Capture WAL fixtures | `app/capture/*`, `scripts/generate_contract_wal.py` |
| `tests/fixtures/finetune_v2_probe_fixtures.json` | Finetune v2 probe fixtures | `app/tweaker/finetune_v2_*` |
| `tests/integration/__init__.py` | Integration test package marker | — |
| `tests/integration/test_finetune_v2_live_smoke.py` | Live finetune v2 smoke (live-only) | `app/tweaker/finetune_v2_*` |
| `tests/integration/test_live_inference.py` | Live inference integration (needs running service) | `app/*`, scripts |
| `tests/unit/__init__.py` | Unit test package marker | — |
| `tests/unit/test_anthropic_bridge.py` | Anthrothropic bridge unit tests | `app/proxy/anthropic_bridge.py` |
| `tests/unit/test_auth.py` | API key auth unit tests | `app/proxy/auth.py` |
| `tests/unit/test_capture_gzip_reader.py` | Capture gzip reader tests | `app/capture/gzip_reader.py` |
| `tests/unit/test_capture_keanu_tools.py` | Keanu capture tools tests | `app/capture/*` |
| `tests/unit/test_capture_media.py` | Capture media tests | `app/capture/media.py` |
| `tests/unit/test_capture_policy.py` | Capture policy tests | `app/capture/policy.py` |
| `tests/unit/test_capture_raw_passthrough.py` | Raw capture passthrough tests | `app/capture/*` |
| `tests/unit/test_capture_redactor.py` | Capture redactor tests | `app/capture/redactor.py` |
| `tests/unit/test_capture_schema.py` | Capture schema tests | `app/capture/schema.py` |
| `tests/unit/test_capture_secret_canary.py` | Secret-canary tests (no secrets leak into capture) | `app/capture/redactor.py`, `app/capture/schema.py` |
| `tests/unit/test_capture_sink.py` | Capture sink tests | `app/capture/sink.py` |
| `tests/unit/test_capture_stream_assembler.py` | Stream assembler tests | `app/capture/stream_assembler.py` |
| `tests/unit/test_capture_wal_writer.py` | WAL writer tests (rotation, HMAC, crash recovery) | `app/capture/wal_writer.py` |
| `tests/unit/test_cloud_catalog.py` | Cloud catalog tests | `app/proxy/cloud_catalog.py` |
| `tests/unit/test_cloud_forwarding.py` | Cloud forwarding tests | `app/cloud_inference/forwarding.py` |
| `tests/unit/test_config_reload.py` | Config hot-reload tests | `app/config_loader.py`, `/api/config/reload` |
| `tests/unit/test_config_schema.py` | Config schema tests | `app/config_loader.py`, `config/*` |
| `tests/unit/test_finetune_v2_contracts.py` | Finetune v2 contracts tests | `app/tweaker/finetune_v2_contracts.py` |
| `tests/unit/test_finetune_v2_contracts_script.py` | Finetune v2 contracts script tests | `scripts/test_finetune_v2_contracts.py` |
| `tests/unit/test_finetune_v2_model_config_script.py` | Finetune v2 config script tests | `scripts/finetune_v2_model_config.py` |
| `tests/unit/test_finetune_v2_runner.py` | Finetune v2 runner tests | `app/tweaker/finetune_v2_runner.py` |
| `tests/unit/test_finetune_v2_telemetry.py` | Finetune v2 telemetry tests | `app/tweaker/finetune_v2_telemetry.py` |
| `tests/unit/test_grammar_capture_validation.py` | Grammar capture validation tests | `app/capture/*`, `app/gateway/normalization.py` |
| `tests/unit/test_grammar_cloud_stripping.py` | Cloud grammar stripping tests | `app/cloud_inference/*` |
| `tests/unit/test_grammar_passthrough.py` | Grammar passthrough pin tests | `app/local_inference/ollama.py` |
| `tests/unit/test_main.py` | App entrypoint tests | `app/main.py` |
| `tests/unit/test_manager.py` | Engine manager tests | `app/engine/manager.py` |
| `tests/unit/test_metrics.py` | Prometheus metrics tests | `app/proxy/metrics.py` |
| `tests/unit/test_model_finetune.py` | Finetune model tests | `app/tweaker/finetune_v2_*` |
| `tests/unit/test_ollama_grammar_mapping.py` | Ollama grammar mapping tests | `app/local_inference/ollama.py` |
| `tests/unit/test_optimizer.py` | Optimizer tests | `app/proxy/optimizer.py` |
| `tests/unit/test_providers.py` | Provider registry tests | `app/proxy/providers.py` |
| `tests/unit/test_queue.py` | Queue tests | `app/proxy/queue.py` |
| `tests/unit/test_ratelimit.py` | Rate-limit tests | `app/proxy/ratelimit.py` |
| `tests/unit/test_scaler.py` | Scaler tests | `app/proxy/scaler.py` |
| `tests/unit/test_server.py` | Server shell/routing tests | `app/proxy/server.py` |
| `tests/unit/test_session_filename_sanitize.py` | Session filename sanitizer tests | `app/gateway/sessions.py` |
| `tests/unit/test_usage.py` | Usage tracking tests | `app/proxy/usage.py`, `app/gateway/usage.py` |

---

## dashboard/ — React/Vite/Tailwind frontend (12)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `dashboard/README.md` | Dashboard README | — |
| `dashboard/index.html` | Vite entry HTML | `dashboard/src/main.jsx` |
| `dashboard/package-lock.json` | Lockfile for dashboard JS deps | `dashboard/package.json` |
| `dashboard/package.json` | Node deps + scripts for dashboard | `dashboard/vite.config.js` |
| `dashboard/postcss.config.js` | PostCSS config for Tailwind | `dashboard/tailwind.config.js` |
| `dashboard/src/App.jsx` | Main React app (view toggle incl. capture panel) | `dashboard/src/CapturePanel.jsx` |
| `dashboard/src/CapturePanel.jsx` | Capture subsystem React panel (status, WAL, force-rotate) | `app/capture/*`, admin API |
| `dashboard/src/index.css` | Global CSS | `dashboard/src/main.jsx` |
| `dashboard/src/main.jsx` | React bootstrap | `dashboard/index.html`, `dashboard/src/App.jsx` |
| `dashboard/tailwind.config.js` | Tailwind config | `dashboard/src/*` |
| `dashboard/tailwind.config.ui.js` | Secondary Tailwind config for UI shell | `app/ui/index.html` |
| `dashboard/vite.config.js` | Vite build + dev proxy to Guardian | `app/ui/*` |

---

## docs/ — documentation (23)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `docs/ANTHROPIC_BRIDGE.md` | Anthropic API bridge documentation | `app/proxy/anthropic_bridge.py` |
| `docs/API_REFERENCE.md` | Public API surface reference | `app/proxy/server.py` |
| `docs/ARCHITECTURE.md` | System architecture overview | all modules |
| `docs/ARCHIVED_HANDOFFS.md` | **Archived session history** (do-not-touch; intentional old names) | AGENTS.md |
| `docs/CLIENT_INTEGRATION.md` | Client setup/integration guide | `app/proxy/auth.py` |
| `docs/CLIENT_KEY_LINKING.md` | Client↔API-key linking operations | `config/guardian.keys.yaml` |
| `docs/CLOUD_ACCESS_REDESIGN.md` | Cloud access redesign plan (implemented 2026-08-21) | `app/proxy/cloud_catalog.py` |
| `docs/CONFIG_PROVIDER_FILES.md` | Per-provider config-file plan (F2 groundwork) | `config/*`, `docs/IMPLEMENTATION_PLAN.md` |
| `docs/CONFIG_SCHEMA.md` | Config schema split reference | `app/config_loader.py`, `config/*` |
| `docs/FINETUNE_V2_REQUIREMENTS.md` | Finetune v2 requirements spec | `app/tweaker/finetune_v2_*` |
| `docs/FILE_REGISTER.md` | **This living file register (F1)** | whole repo |
| `docs/GATEWAY_MANAGER_SPLIT.md` | Gateway/manager split plan (F4) | `app/proxy/server.py`, `app/engine/manager.py` |
| `docs/GCD_IMPLEMENTATION_SPEC.json` | GCD implementation JSON spec | `app/gateway/normalization.py` |
| `docs/GUARDIAN_KEANU_CAPTURE_PLAN.json` | Capture contract between Guardian & Keanu | `app/capture/schema.py` |
| `docs/HARDWARE_TUNING.md` | GPU/hardware tuning guide | `app/local_inference/models.py` |
| `docs/IMPLEMENTATION_PLAN.md` | **Guardian 2.0 masterplan (F0-F7)** — do-not-touch plan text | GitHub issue #1 |
| `docs/LAN_GPU_BACKENDS.md` | LAN GPU backends plan (F3/F6) | `app/engine/manager.py` |
| `docs/LLM_ROUTER.md` | Model resolution / cloud routing doc | `app/proxy/providers.py`, `app/cloud_inference/*` |
| `docs/LLM_TERMINOLOGY.md` | LLM terminology reference | — |
| `docs/MODEL_BENCHMARKS.md` | Model benchmark results | `scripts/bench_all_models.py` |
| `docs/MTP_STUDY.md` | Multi-Token-Prediction study | llama-server MTP |
| `docs/free-tier-pool-request.md` | Free-tier provider pool request | provider config |
| `docs/free-tier-pool-verification.md` | Free-tier pool verification | provider config |
| `docs/skills/operator-runbook.md` | Operator runbook (deployment & operations) | `scripts/pre_restart_check.py`, systemd/nginx |

---

## Root files (12) + .github (2)
| Path | Function | Related processes/files |
|------|----------|------------------------|
| `.github/copilot-instructions.md` | Copilot agent instructions (points to AGENTS.md) | `AGENTS.md` |
| `.github/workflows/codeql.yml` | CodeQL security scanning workflow | org reusable workflows |
| `.gitignore` | Ignore rules (secrets, data/, venv, logs) | all gitignored paths |
| `.goosehints` | Goose symlink → AGENTS.md | `AGENTS.md` |
| `AGENTS.md` | **Canonical AI-agent context (read first)** — lead maintains | all docs |
| `CHANGELOG.md` | **Historical changelog** (do-not-touch; intentional old names) | releases |
| `CLAUDE.md` | Claude Code symlink → AGENTS.md | `AGENTS.md` |
| `README.md` | Project README | — |
| `finetune_v2.py` | Finetune v2 top-level entry point | `app/tweaker/finetune_v2_*` |
| `guardian-llmprovider-gateway.code-workspace` | VS Code multi-root workspace (renamed in F0) | dev IDE |
| `pyproject.toml` | Python project metadata / tool config | app packaging |
| `requirements.txt` | Python dependencies | app venv |

---

## Legend / notes
- **Do-not-touch (archival/historical) files:** `docs/ARCHIVED_HANDOFFS.md`, `CHANGELOG.md`, `docs/IMPLEMENTATION_PLAN.md`, `AGENTS.md` intentionally still reference the legacy `llama-guardian` / `LLAMA_CPP_GUARDIAN_*` names — this is deliberate, not a leftover.
- **TLS identity:** `deploy/tls/guardian-192.168.1.35.crt` is the TLS *certificate* identity for host `guardian-192.168.1.35.crt`; its filename must **not** be renamed to the service name.
- **`llama-server`** is a separate backend service (llama.cpp) and is distinct from `guardian-llmprovider-gateway` — references to it stay untouched.
- **Gitignored / not tracked:** `.env`, `config/guardian.keys.yaml`, `data/`, `venv/`, `.scratch/`, logs, caches. Secrets are never committed.
- **Tracked count:** `git ls-files` = **194 files** at the time of this draft (F1).

_End of draft. This register is a living document; update it as files are added/removed/renamed._
