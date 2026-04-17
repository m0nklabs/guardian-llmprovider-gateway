# Guardian Architecture

> Last Updated: 2026-04-17

## Overview

Guardian is middleware that sits between clients and `llama-server`, turning a raw inference process into a managed service with request queuing, lifecycle control, cooperative GPU memory management, and benchmark-driven optimization.

It is **not** a simple proxy — Guardian actively manages the backend process, serializes access through a queue, coordinates GPU memory with 3rd-party processes, and optimizes request parameters based on empirical benchmarks.

## Topology

```
Clients / Apps / Tools
        │
        ▼
┌────────────────────────────────────────────┐
│ Guardian Middleware (:11434)                │
│  ├─ Bearer token auth (api_keys.json)      │
│  ├─ FIFO inference queue (semaphore-based) │
│  ├─ Protocol bridges (Ollama + OpenAI)     │
│  ├─ Model routing + switch lock            │
│  ├─ RequestOptimizer (benchmark-driven)    │
│  ├─ VramScheduler (budget enforcement)     │
│  ├─ Idle unload watcher (background)       │
│  └─ 3rd-party GPU process coordination     │
│       ├─ ComfyUI /free API integration     │
│       └─ VRAM budgeting for Frigate, etc.  │
└────────────────────────────────────────────┘
        │
        ▼
  llama-server (:11440)
        │
        ▼
  Backend binary (per-model selection)
    ├─ official llama.cpp (primary)
    └─ ik_llama.cpp fork (fallback)

  Dashboard UI (:11437)
    └─ Chart.js + Tailwind dark mode
```

## Core Responsibilities

### 1. Request Queue

Guardian serializes all inference requests through a FIFO queue backed by `asyncio.Semaphore(1)`. Only one inference runs at a time; others wait.

| Aspect | Implementation |
|--------|---------------|
| **Serialization** | `asyncio.Semaphore(max_concurrent)` — FIFO ordering in CPython |
| **Tracking** | `QueueEntry` dataclass with `request_id`, `client_id`, `enqueued_at`, `started_at` |
| **Response headers** | `X-Request-Id` (UUID) and `X-Queue-Wait-Ms` (integer) on every inference response |
| **Status endpoint** | `GET /v1/queue/status` — always responds immediately (not queued) |
| **Timeout** | `queue_timeout_seconds` (default 300s) → HTTP 429 on timeout |
| **Client disconnect** | `asyncio.CancelledError` removes request from queue |
| **Lifetime counters** | `total_queued`, `total_completed`, `total_timeouts` |
| **Scope** | Only inference endpoints queued (`chat/completions`, `completions`, `embeddings`, `/api/chat`, `/api/generate`) |

Model switching happens **inside** the queue slot, preventing concurrent inference from interfering with switches.

### 2. Protocol Compatibility

Guardian exposes both API styles so clients with different assumptions can talk to the same backend:

- **OpenAI**: `/v1/chat/completions`, `/v1/models`, generic `/v1/{path}` passthrough
- **Ollama**: `/api/chat`, `/api/generate`, `/api/tags`, `/api/version`

Ollama requests are translated to OpenAI format internally, forwarded to llama-server, and translated back.

### 3. Model Lifecycle Management

Guardian owns the `llama-server` process and manages its full lifecycle:

| Capability | Implementation |
|------------|---------------|
| **Model switching** | Concurrency-safe via `asyncio.Lock()`. Stops server → writes args/binary → frees GPU memory → starts server → health check → verifies backend model. |
| **Model pinning** | `guardian.pinned_model` in models.yaml locks the system to one model. Only `force=True` or allowlisted clients can override. |
| **Client allowlist** | `guardian.switch_allowlist` restricts which API key holders can trigger model switches. |
| **Idle unload** | Background task checks every 60s. If `idle_unload_minutes` exceeded and no active/queued requests, stops llama-server. Auto-reloads on next request. |
| **Crash detection** | Records up to 50 `CrashRecord` events with timestamps, error messages, exit codes, and config snapshots. |
| **Backend verification** | Post-switch check confirms the actual running model matches Guardian's config. Prevents silent desync. |
| **Config hot-reload** | Re-reads `models.yaml` on every `load()`/`switch_model()` call — config edits take effect without restarting Guardian. |

#### Switch flow:

```
switch_model(model_name, client_id, force)
  │
  ├─ Verify model exists in config
  ├─ Check pinned_model + allowlist
  ├─ Auto-save current context
  ├─ Stop llama-server
  ├─ Write model args + binary selection
  ├─ Free GPU memory (3rd-party coordination)
  │   └─ Request ComfyUI to release VRAM
  ├─ Start llama-server
  ├─ Wait for health check
  ├─ Detect crash if startup fails
  ├─ Verify backend model matches
  └─ Restore context if exists
```

### 4. 3rd-Party GPU Process Awareness

Guardian operates on a shared GPU host alongside other GPU consumers. Instead of killing competing processes, it cooperates:

**Cooperative VRAM Release**:
- Before loading a model, `_free_gpu_memory()` orchestrates VRAM cleanup
- ComfyUI: `POST http://127.0.0.1:8188/free` with `{"unload_models": true, "free_memory": true}`
- ComfyUI gracefully releases VRAM (e.g., 6768MB → 174MB) while staying alive
- ComfyUI auto-reloads its models on the next workflow execution
- Timeout: 15s with graceful fallback on connection errors

**VRAM Budgeting**:
- 3rd-party processes (Frigate NVR ~440MB, etc.) are accounted for in the VRAM budget but never killed
- Configurable hard limit (`proxy.vram_limit_mb`, default 27000MB) prevents OOM crashes
- `VramScheduler` tracks active model VRAM usage per request and waits on `asyncio.Condition` if budget would be exceeded

**GPU Process Visibility**:
- After VRAM cleanup, Guardian runs `nvidia-smi` to log remaining GPU consumers for observability

### 5. Auth and Access Control

All endpoints require Bearer token authentication:

- Tokens stored in `config/api_keys.json`
- Format: `{prefix}_{32-char-hex}` (e.g., `flip_`, `oelala_`, `hydro_`)
- FastAPI dependency injection via `@Depends(verify_api_key)`
- Returns client name for allowlist checking and queue tracking

### 6. Request Optimization

`RequestOptimizer` injects benchmark-derived settings into requests:

- Reads from `data/benchmark_state.json` and `docs/benchmark_results.json`
- Lazy-reloads when file mtime changes
- Injects best `num_ctx` and `num_batch` for the current model
- Respects user overrides — only injects if the user didn't set the value

### 7. Benchmarking

`BenchmarkSuite` tests models across context sizes and batch configurations:

- **Test matrix**: models × `[2048, 4096, 8192, 16384, 24576, 28672, 32768]` × `[128, 256, 512, 1024]`
- **Metrics**: TPS, total duration, eval count, VRAM delta, peak VRAM
- Runs via `asyncio.to_thread()` to avoid blocking the event loop
- Resumable: state persisted to `data/benchmark_state.json`
- Records new TPS records per model with 🏆 logging

### 8. Scheduler / Maintenance Windows

`SchedulerManager` handles unattended maintenance:

- Configurable idle windows (hours + days of week) from `settings.yaml`
- On maintenance entry: stops configured services, triggers benchmark suite
- On maintenance exit: stops benchmark, restarts services
- Service management via `sudo systemctl {action} {service_name}`

### 9. Session Management

- **Session save/load/list**: Persist and restore conversation contexts via llama-server's slot API
- **Crash history**: Inspect crash events via `/api/crashes`
- **Status**: Current model, backend health, VRAM usage, idle state, security info
- **Dashboard stats**: VRAM, active models, cached models, benchmark records

## Backend Selection

Per-model backend selection via the `backend:` field in `models.yaml`:

```python
BACKEND_BINARIES = {
    "official": "/home/flip/llama_cpp_official/build/bin/llama-server",
    "ik_fork": "/home/flip/ik_llama_cpp_build/build/bin/llama-server",
}
DEFAULT_BACKEND = "official"
```

- **official** (ggml-org/llama.cpp): Default for all models. Actively maintained upstream.
- **ik_fork** (ikawrakow/ik_llama.cpp): Fallback for specific optimizations if needed.
- Set via `backend:` key in `config/models.yaml` per model.
- Written to `config/current_model.binary` at switch time for `start_llama.sh`.

## GPU Strategy

| GPU | VRAM | Typical Use |
|-----|------|-------------|
| RTX 3060 (cuda:0) | 12GB | Model weight storage via tensor split |
| RTX 5060 Ti (cuda:1) | 16GB | Primary compute + activations |
| **Total budget** | **27GB** | 28GB minus ~1GB reserved for 3rd-party GPU processes |

**3rd-party GPU processes** (always running, never killed):
- Frigate NVR: ~440MB (ffmpeg hardware decoding)
- ComfyUI: Variable — releases VRAM cooperatively on request

**Tensor splits** in `models.yaml`: Small models offload 100% to one GPU. Large models (>12GB) use splits like `"0.57,0.43"` or `"0.55,0.45"`.

**VRAM scheduling flow**:
1. Request arrives → queue slot acquired
2. Guardian estimates VRAM needed for requested model
3. If (current + needed) > limit: wait on condition
4. Request cooperative VRAM release from 3rd-party processes
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

### Inference (queued, serialized)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/chat/completions` | POST | OpenAI chat (streaming + non-streaming) |
| `/v1/completions` | POST | OpenAI text completion |
| `/v1/embeddings` | POST | OpenAI embeddings |
| `/api/chat` | POST | Ollama chat (translates to/from OpenAI internally) |
| `/api/generate` | POST | Ollama prompt generation |

### Queue & Status (not queued)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/queue/status` | GET | Queue position, wait time, active requests |
| `/api/status` | GET | Model, health, VRAM, crashes, security |
| `/api/crashes` | GET | Crash history |

### Model Management

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/models` | GET | OpenAI model list |
| `/api/tags` | GET | Ollama model list |
| `/api/version` | GET | Ollama version compat |
| `/admin/load` | POST | Reload llama-server |
| `/admin/unload` | POST | Stop llama-server (free VRAM) |
| `/v1/{path}` | GET/POST | OpenAI-compatible passthrough (non-inference paths) |

### Session & Benchmark

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/session/save` | POST | Save conversation context |
| `/api/session/load` | POST | Load conversation context |
| `/api/session/list` | GET | List saved sessions |
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
│   ├── queue.py         # InferenceQueue: FIFO semaphore, status reporting
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
├── models.yaml          # Model registry + guardian security config
├── settings.yaml        # Ports, VRAM budget, timeouts, queue, scheduler
├── api_keys.json        # API key store
├── current_model.args   # Runtime: llama-server CLI args
├── current_model.binary # Runtime: backend binary path
└── current_model.env    # Runtime: per-model env vars (optional)

data/
└── benchmark_state.json # Persisted benchmark queue + results

scripts/                 # Startup, key gen, tests, analysis, model sync
docs/                    # Client integration, benchmarks, glossary
```
