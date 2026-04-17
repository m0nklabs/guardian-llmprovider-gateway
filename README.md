# Llama CPP Guardian

Guardian is middleware for `llama-server` that turns a raw inference process into a managed service with request queuing, model lifecycle control, cooperative GPU memory management, API key auth, and benchmark-driven optimization.

It sits between clients and the llama.cpp backend, providing protocol bridging (OpenAI + Ollama APIs), automatic model switching, FIFO request queuing, idle unload/auto-reload, crash detection, and shared GPU coordination with 3rd-party processes.

## Architecture

```
Clients / Apps / Tools
        │
        ▼
┌──────────────────────────────────────┐
│  Guardian Middleware :11434           │
│  ├─ Bearer token auth                │
│  ├─ FIFO request queue (serialized)  │
│  ├─ OpenAI /v1 + Ollama /api bridges │
│  ├─ Model selection / switching      │
│  ├─ Request optimization             │
│  ├─ Idle unload & auto-reload        │
│  ├─ VRAM budget enforcement          │
│  └─ 3rd-party GPU process awareness  │
└──────────────────────────────────────┘
        │
        ▼
  llama-server :11440
        │
        ▼
  Configured backend binary
    └─ official llama.cpp (default)
        (extensible — register additional backends in BACKEND_BINARIES)

  Dashboard UI :11437
```

## Key Features

### Request Queue

Guardian runs a single-slot backend. Only one inference request is processed at a time — concurrent requests wait in a FIFO queue.

- Transparent to simple clients (just blocks until a slot is free)
- `X-Request-Id` and `X-Queue-Wait-Ms` response headers on every inference response
- `GET /v1/queue/status` — separate polling endpoint for queue position (not queued itself, always responds immediately)
- Configurable timeout → HTTP 429 when exceeded
- Client disconnect detection (cancels queued request)

See [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md) for client implementation patterns and code examples.

### Extensible Backend

Guardian supports per-model backend selection. The official llama.cpp binary is the default. Additional backends (forks, custom builds) can be registered in the `BACKEND_BINARIES` dict — models opt in via the `backend:` field in `config/models.yaml`.

| Backend | Binary | Use Case |
|---------|--------|----------|
| **official** (default) | `/home/flip/llama_cpp_official/build/bin/llama-server` | All models |
| *(custom)* | Register in `app/engine/manager.py` | Specific optimizations |

Models without an explicit `backend:` key use the official binary.

### Protocol Bridging

Guardian exposes both API styles simultaneously:

- **OpenAI**: `/v1/chat/completions`, `/v1/models`, `/v1/*`
- **Ollama**: `/api/chat`, `/api/generate`, `/api/tags`, `/api/version`

Automatic model switching: if a request specifies a different model than what's loaded, Guardian switches transparently (subject to pinning and allowlist rules).

### Model Lifecycle Management

- **Model switching** — concurrency-safe via `asyncio.Lock()`, happens inside queue slot
- **Model pinning** — lock the system to a single model via `guardian.pinned_model`
- **Client allowlist** — restrict which API keys can trigger model switches
- **Idle unload** — stops llama-server after configurable idle time, auto-reloads on next request
- **Crash detection** — records up to 50 crash events with config snapshots
- **Backend verification** — post-switch check confirms the correct model is running
- **Config hot-reload** — re-reads `models.yaml` on every load/switch (no Guardian restart needed)

### 3rd-Party GPU Process Awareness

Guardian operates on shared GPU hardware alongside other processes. Instead of killing competing processes, it cooperates:

- **ComfyUI integration**: Before loading a model, calls `POST /free` to request graceful VRAM release. ComfyUI stays alive and auto-reloads its models on next workflow.
- **3rd-party budgeting**: The VRAM budget (`proxy.vram_limit_mb`) accounts for memory reserved by other GPU processes (Frigate NVR, etc.). These processes are never touched.
- **VRAM scheduling**: Enforces a hard VRAM limit to prevent OOM crashes on multi-GPU setups.

### Security

- **Bearer token auth** on all endpoints via `config/api_keys.json`
- Token format: `{prefix}_{32-char-hex}` (e.g., `flip_abc123...`, `hydro_def456...`)
- **Model pinning** prevents unauthorized model switches
- **Switch allowlist** restricts which clients can trigger model changes

### Benchmarking & Optimization

- Automated benchmark suite: models × context sizes × batch sizes
- Resumable state persisted to `data/benchmark_state.json`
- `RequestOptimizer` injects best-known context/batch settings into requests
- Scheduled maintenance windows for unattended benchmark runs
- Dashboard visualization of results

## Running Guardian

Guardian runs as a systemd service (`llama-guardian.service`). For development:

```bash
pip install -r requirements.txt
python3 app/main.py
```

## Configuration

### `config/models.yaml` — Model Registry

Defines per-model runtime behavior:

```yaml
models:
  GLM-4.7-Flash-Claude-4.5-Opus:
    path: /home/flip/models/glm-4.7-flash-claude-4.5-opus.q4_k_m.gguf
    context: 262144
    ngl: 99
    kv_type: q4_0
    tensor_split: "0.57,0.43"
    extra_args: "-nkvo --parallel 4"

guardian:
  pinned_model: "GLM-4.7-Flash-Claude-4.5-Opus"
  switch_allowlist: ["m0nk111", "oelala"]
  idle_unload_minutes: 5
```

Supported per-model fields: `path`, `context`, `max_context`, `ngl`, `kv_type`, `backend`, `tensor_split`, `mmproj`, `extra_args`.

### `config/settings.yaml` — System Configuration

```yaml
proxy:
  port: 11434
  target: http://localhost:11440
  vram_limit_mb: 27000

queue:
  max_concurrent: 1
  queue_timeout_seconds: 300

timeouts:
  tiers:
    tier_70b: { min_size_mb: 40000, timeout_seconds: 1800 }
    tier_32b: { min_size_mb: 20000, timeout_seconds: 1200 }
    tier_13b: { min_size_mb: 10000, timeout_seconds: 600 }
    tier_8b:  { min_size_mb: 5000,  timeout_seconds: 360 }
    tier_small: { min_size_mb: 0,   timeout_seconds: 600 }

benchmark:
  schedule:
    start_hour: 4
    end_hour: 11
    days: ["mon", "tue", "wed", "thu", "fri"]

services_to_stop: ["caramba-backend", "agent-forge"]
```

### `config/api_keys.json` — API Key Registry

Stores Bearer tokens with client names, creation timestamps, and optional metadata. Generate new keys with `python3 scripts/generate_key.py --name "my-app"`.

## API Reference

For detailed client integration examples (Python, TypeScript), queue-aware patterns, and error handling, see **[docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md)**.

### Inference (queued)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/chat/completions` | POST | OpenAI chat (streaming + non-streaming) |
| `/v1/completions` | POST | OpenAI text completion |
| `/v1/embeddings` | POST | OpenAI embeddings |
| `/api/chat` | POST | Ollama-style chat |
| `/api/generate` | POST | Ollama-style prompt generation |

All inference responses include `X-Request-Id` and `X-Queue-Wait-Ms` headers.

### Queue & Status

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/queue/status` | GET | Queue position, wait time, active requests |
| `/api/status` | GET | Current model, health, VRAM, crash info |
| `/api/crashes` | GET | Crash history (up to 50 records) |

### Model Management

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/models` | GET | OpenAI model list |
| `/api/tags` | GET | Ollama model list |
| `/api/version` | GET | Ollama version compat |
| `/admin/load` | POST | Force-load a specific model |
| `/admin/unload` | POST | Stop llama-server (free VRAM) |

### Session & Benchmark

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/session/save` | POST | Save conversation context |
| `/api/session/load` | POST | Load conversation context |
| `/api/session/list` | GET | List saved sessions |
| `/api/stats` | GET | Dashboard metrics (VRAM, models, records) |
| `/api/benchmark` | GET | Benchmark results & summary |
| `/api/benchmark/start` | POST | Trigger benchmark suite |
| `/api/benchmark/stop` | POST | Stop running benchmark |

All endpoints require `Authorization: Bearer <token>`.

## Directory Structure

```text
app/
├── engine/
│   └── manager.py       # ModelManager: lifecycle, switching, VRAM, crash detection
├── proxy/
│   ├── server.py        # FastAPI app: all endpoints, idle watcher
│   ├── queue.py         # InferenceQueue: FIFO semaphore, status reporting
│   ├── auth.py          # Bearer token auth (api_keys.json)
│   └── optimizer.py     # RequestOptimizer: benchmark-driven tuning
├── scheduler/
│   └── manager.py       # SchedulerManager: maintenance windows, service control
├── tweaker/
│   └── benchmark.py     # BenchmarkSuite: model × ctx × batch testing
├── ui/
│   └── index.html       # Dashboard (Tailwind dark mode, Chart.js)
└── main.py              # GuardianService: startup orchestration

config/
├── models.yaml          # Model registry + guardian security config
├── settings.yaml        # System config (ports, VRAM, timeouts, queue, scheduler)
├── api_keys.json        # API key store
├── current_model.args   # Runtime: active llama-server CLI args
├── current_model.binary # Runtime: active backend binary path
└── current_model.env    # Runtime: per-model env vars (optional)

scripts/
├── start_llama.sh       # Backend startup wrapper (reads current_model.binary)
├── generate_key.py      # CLI key generation
├── test_system.py       # End-to-end system test
├── benchmark_context.py # Context size benchmarking
├── stress_test.py       # Load testing
└── ...                  # Analysis, vision tests, model sync

data/
└── benchmark_state.json # Persisted benchmark queue + results

docs/
├── CLIENT_INTEGRATION.md     # Client API guide with code examples
├── BENCHMARK_SUMMARY.md      # Global rankings and model comparisons
├── CONTEXT_BENCHMARKS.md     # Optimal context sizes per model
├── REAL_BENCHMARK_RESULTS.md # Empirical test results
└── LLM_TERMINOLOGY.md       # Model collection overview + glossary
```

## GPU Environment

Guardian runs on a dual-GPU host and coordinates VRAM with 3rd-party processes:

| GPU | VRAM | Role |
|-----|------|------|
| RTX 3060 (cuda:0) | 12GB | Model weight storage (tensor split) |
| RTX 5060 Ti (cuda:1) | 16GB | Primary compute + model weights |

**3rd-party GPU processes** (accounted for in VRAM budget, never killed):
- **Frigate NVR**: ~440MB (ffmpeg hardware decoding) — always running
- **ComfyUI**: Releases VRAM on request via `/free` API — cooperative sharing

Models use configured tensor splits (e.g., `"0.57,0.43"`) to distribute weights across both GPUs.

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — Architecture and design decisions
- [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md) — Client API guide with queue-aware patterns
- [docs/BENCHMARK_SUMMARY.md](docs/BENCHMARK_SUMMARY.md) — Model performance rankings
- [docs/REAL_BENCHMARK_RESULTS.md](docs/REAL_BENCHMARK_RESULTS.md) — Empirical benchmark results
- [docs/CONTEXT_BENCHMARKS.md](docs/CONTEXT_BENCHMARKS.md) — Context size recommendations
- [docs/LLM_TERMINOLOGY.md](docs/LLM_TERMINOLOGY.md) — Model glossary and collection overview
