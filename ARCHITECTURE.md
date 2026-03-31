# Guardian Architecture

> Last Updated: 2026-03-31

## Overview

Guardian sits in front of `llama-server` and turns a raw local inference process into a managed service with auth, model lifecycle control, cooperative VRAM management, and benchmark-driven optimization.

## Topology

```
Clients / Apps / Tools
        │
        ▼
┌──────────────────────────────────────────┐
│ Guardian Proxy (:11434)                  │
│  ├─ Bearer token auth (api_keys.json)    │
│  ├─ Protocol bridges (Ollama + OpenAI)   │
│  ├─ Model routing + switch lock          │
│  ├─ RequestOptimizer (benchmark-driven)  │
│  ├─ VramScheduler (budget enforcement)   │
│  ├─ Idle unload watcher (background)     │
│  └─ Cooperative VRAM manager             │
│       └─ ComfyUI /free API integration   │
└──────────────────────────────────────────┘
        │
        ▼
  llama-server (:11440)
        │
        ▼
  Backend binary (per-model selection)
    ├─ ik_llama.cpp fork (primary)
    └─ official llama.cpp (fallback)

  Dashboard UI (:11437)
    └─ Chart.js + Tailwind dark mode
```

## Core Responsibilities

### 1. Protocol Compatibility

Guardian exposes both API styles simultaneously so clients with different assumptions can talk to the same backend:

- **OpenAI**: `/v1/chat/completions`, `/v1/models`, generic `/v1/{path}` proxy
- **Ollama**: `/api/chat`, `/api/generate`, `/api/tags`, `/api/version`

Ollama requests are translated to OpenAI format internally, forwarded to llama-server, and translated back.

### 2. Model Lifecycle Management

Guardian owns the `llama-server` process and manages its full lifecycle:

| Capability | Implementation |
|------------|---------------|
| **Model switching** | Concurrency-safe via `asyncio.Lock()`. Stops server → writes args/binary → frees VRAM → starts server → health check → verifies backend model. |
| **Model pinning** | `guardian.pinned_model` in models.yaml locks the system to one model. Only `force=True` or allowlisted clients can override. |
| **Client allowlist** | `guardian.switch_allowlist` restricts which API key holders can trigger model switches. |
| **Idle unload** | Background task checks every 60s. If `idle_unload_minutes` exceeded, stops llama-server. Auto-reloads on next request. |
| **Crash detection** | Records up to 50 `CrashRecord` events with timestamps, error messages, exit codes, and config snapshots. |
| **Backend verification** | Post-switch and post-startup verification confirms the actual running model matches Guardian's config. Prevents silent desync. |

#### Switch flow:

```
switch_model(model_name, client_id, force)
  │
  ├─ Verify model exists in config
  ├─ Check pinned_model + allowlist
  ├─ Auto-save current context
  ├─ Stop llama-server
  ├─ Write model args + binary selection
  ├─ Free GPU memory (cooperative)
  │   └─ Request ComfyUI to release VRAM
  ├─ Start llama-server
  ├─ Wait for health check
  ├─ Detect crash if startup fails
  ├─ Verify backend model matches
  └─ Restore context if exists
```

### 3. Cooperative VRAM Management

Guardian operates on a shared GPU host alongside ComfyUI and Frigate NVR. Instead of killing competing processes, it cooperates:

**ComfyUI Integration**:
- Before loading a model, `_request_comfyui_free()` calls `POST http://127.0.0.1:8188/free` with `{"unload_models": true, "free_memory": true}`
- ComfyUI gracefully releases VRAM (e.g., 6768MB → 174MB) while staying alive
- ComfyUI auto-reloads its models on the next workflow execution
- Timeout: 10s with graceful fallback on connection errors

**Frigate NVR**:
- Frigate's ffmpeg hardware decoding uses ~440MB on GPU — this is expected
- Guardian never touches Frigate processes
- VRAM budget (27000MB) already accounts for Frigate's reservation

**VramScheduler**:
- Tracks active model VRAM usage per request
- Waits on `asyncio.Condition` if adding a model would exceed `vram_limit_mb`
- Enforces the configured hard limit (default 27000MB)

### 4. Auth and Access Control

All endpoints require Bearer token authentication:

- Tokens stored in `config/api_keys.json`
- Format: `{prefix}_{32-char-hex}` (e.g., `flip_`, `oelala_`, `hydro_`)
- FastAPI dependency injection via `@Depends(verify_api_key)`
- Returns client name for allowlist checking

Current registered clients: m0nk111, openclaw, oelala, hungryfoodtool, hydroponics.

### 5. Request Optimization

`RequestOptimizer` injects benchmark-derived settings into requests:

- Reads from `data/benchmark_state.json` and `docs/benchmark_results.json`
- Lazy-reloads when file mtime changes
- Injects best `num_ctx` and `num_batch` for the current model
- Respects user overrides — only injects if the user didn't set the value

### 6. Benchmarking

`BenchmarkSuite` tests models across context sizes and batch configurations:

- **Test matrix**: models × `[2048, 4096, 8192, 16384, 24576, 28672, 32768]` × `[128, 256, 512, 1024]`
- **Metrics**: TPS, total duration, eval count, VRAM delta, peak VRAM
- Runs via `asyncio.to_thread()` to avoid blocking the event loop
- Resumable: state persisted to `data/benchmark_state.json`
- Records new TPS records per model with 🏆 logging

### 7. Scheduler / Maintenance Windows

`SchedulerManager` handles unattended maintenance:

- Configurable idle windows (hours + days of week) from `settings.yaml`
- On maintenance entry: stops configured services, triggers benchmark suite
- On maintenance exit: stops benchmark, restarts services
- Service management via `sudo systemctl {action} {service_name}`

### 8. Session Management

Runtime state helpers beyond request proxying:

- **Session save/load/list**: Persist and restore conversation contexts
- **Crash history**: Inspect crash events via `/api/crashes`
- **Status**: Current model, backend health, VRAM usage, idle state, security info
- **Dashboard stats**: VRAM, active models, cached models, benchmark records

## Backend Selection

Per-model backend selection is a first-class design choice:

```python
BACKEND_BINARIES = {
    "ik_fork": "/home/flip/ik_llama_cpp_build/build/bin/llama-server",
    "official": "/home/flip/llama_cpp_official/build/bin/llama-server"
}
```

- **ik_fork**: Default for everything. Optimized, supports split-mode.
- **official**: Only for architectures the ik fork doesn't handle (e.g., Nemotron).
- Set via `backend:` key in `config/models.yaml` per model.
- Written to `config/current_model.binary` at switch time for `start_llama.sh`.

## GPU Strategy

| GPU | VRAM | Typical Use |
|-----|------|-------------|
| RTX 3060 (cuda:0) | 12GB | Model weight storage via tensor split |
| RTX 5060 Ti (cuda:1) | 16GB | Primary compute + activations |
| **Total budget** | **27GB** | 28GB minus ~1GB for Frigate |

**Tensor splits** in `models.yaml`: Small models offload 100% to one GPU. Large models (>12GB) use splits like `"0.57,0.43"` (≤19GB) or `"0.45,0.55"` (>20GB).

**VRAM scheduling flow**:
1. Request arrives
2. Guardian estimates VRAM needed for requested model
3. If (current + needed) > limit: wait on condition
4. Free cooperating services' VRAM (ComfyUI /free)
5. Load model across GPUs with configured tensor split

## Timeout System

Model-tier-based timeouts prevent runaway requests:

| Tier | Min Size | Timeout |
|------|----------|---------|
| tier_70b | 40GB | 30 min |
| tier_32b | 20GB | 20 min |
| tier_13b | 10GB | 10 min |
| tier_8b | 5GB | 6 min |
| tier_small | 0 | 10 min |

Configured in `settings.yaml` under `timeouts.tiers`.

## API Surface

### Proxy / Compatibility
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/chat` | POST | Ollama chat (translates to/from OpenAI internally) |
| `/api/generate` | POST | Ollama prompt generation |
| `/api/tags` | GET | Ollama model list |
| `/api/version` | GET | Ollama version compat |
| `/v1/models` | GET | OpenAI model list |
| `/v1/chat/completions` | POST | OpenAI chat (auto-switches model) |
| `/v1/{path}` | GET/POST | OpenAI-compatible proxy passthrough |

### Operational
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/status` | GET | Model, health, VRAM, crashes, security |
| `/api/crashes` | GET | Crash history |
| `/admin/load` | POST | Reload llama-server |
| `/admin/unload` | POST | Stop llama-server (free VRAM) |
| `/api/session/save` | POST | Save conversation context |
| `/api/session/load` | POST | Load conversation context |
| `/api/session/list` | GET | List saved sessions |

### Dashboard / Benchmark
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/stats` | GET | Dashboard metrics |
| `/api/benchmark` | GET | Benchmark results |
| `/api/benchmark/start` | POST | Run benchmark suite |
| `/api/benchmark/stop` | POST | Stop running benchmark |

## Directory Structure

```text
app/
├── engine/
│   └── manager.py       # ModelManager: lifecycle, switching, VRAM, crash detection
├── proxy/
│   ├── server.py        # FastAPI app: endpoints, VramScheduler, idle watcher
│   ├── auth.py          # Bearer token auth (api_keys.json)
│   └── optimizer.py     # RequestOptimizer: benchmark-driven tuning
├── scheduler/
│   └── manager.py       # SchedulerManager: maintenance windows
├── tweaker/
│   └── benchmark.py     # BenchmarkSuite: model × ctx × batch testing
├── ui/
│   └── index.html       # Dashboard (Tailwind dark, Chart.js)
└── main.py              # GuardianService: startup orchestration

config/
├── models.yaml          # Model registry (40+ models, guardian security)
├── settings.yaml        # Ports, VRAM budget, timeouts, scheduler
├── api_keys.json        # API key store
├── current_model.args   # Runtime: llama-server CLI args
├── current_model.binary # Runtime: backend binary path
└── current_model.env    # Runtime: per-model env vars (optional)

data/
└── benchmark_state.json # Persisted benchmark queue + results

scripts/                 # Startup, key gen, tests, analysis, model sync
docs/                    # Benchmark reports, context recommendations, glossary
```

## Implementation Notes

- **Concurrency**: Model switches protected by `asyncio.Lock()` — no parallel switches possible
- **Async I/O**: `httpx.AsyncClient` for non-blocking HTTP to backend
- **Background tasks**: Idle-unload watcher (60s interval), benchmark runs (asyncio.to_thread)
- **Lazy loading**: Benchmark results only reloaded when file mtime changes
- **PID guard**: `guardian.pid` prevents duplicate instances
- **Signal handling**: Graceful shutdown via `GuardianService.stop()`
- **Platform**: Python 3.14, FastAPI + uvicorn, Tailwind + Chart.js dashboard
