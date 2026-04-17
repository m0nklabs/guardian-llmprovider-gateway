# Llama CPP Guardian

Guardian is a proxy, lifecycle manager, and benchmark layer for `llama-server`, built to make local LLM serving predictable on shared GPU hardware.

It sits between clients and the raw llama.cpp server, providing model switching with crash detection, VRAM scheduling with cooperative GPU memory management, OpenAI & Ollama-compatible API bridging, benchmarking, and API key auth.

## Architecture

```
Clients / Apps / Tools
        │
        ▼
┌──────────────────────────────────┐
│  Guardian Proxy :11434           │
│  ├─ Bearer token auth            │
│  ├─ Ollama-compatible endpoints  │
│  ├─ OpenAI-style /v1 endpoints   │
│  ├─ Model selection / switching  │
│  ├─ Request optimization         │
│  ├─ Idle unload & auto-reload    │
│  └─ Cooperative VRAM management  │
└──────────────────────────────────┘
        │
        ▼
  llama-server :11440
        │
        ▼
  Configured backend binary
    ├─ ik_llama.cpp fork (primary)
    └─ official llama.cpp (fallback)
```

Dashboard/UI runs on port `11437`.

## Key Features

### Dual Backend Strategy

Guardian supports two llama.cpp backends per model:

| Backend | Binary | Use Case |
|---------|--------|----------|
| **ik_fork** (primary) | `/home/flip/ik_llama_cpp_build/build/bin/llama-server` | Default for all models |
| **official** (fallback) | `/home/flip/llama_cpp_official/build/bin/llama-server` | Architectures the ik fork doesn't support (e.g., Nemotron) |

Backend selection is per-model in `config/models.yaml`.

### Protocol Bridging

Guardian exposes both API styles simultaneously:

- **OpenAI**: `/v1/chat/completions`, `/v1/models`, `/v1/*` proxy
- **Ollama**: `/api/chat`, `/api/generate`, `/api/tags`, `/api/version`

Automatic model switching: if a request specifies a different model than what's loaded, Guardian switches transparently (subject to pinning and allowlist rules).

### Model Lifecycle Management

- **Model switching** with concurrency lock (`asyncio.Lock`) to prevent races
- **Model pinning** — lock the system to a single model via `guardian.pinned_model`
- **Client allowlist** — restrict which API keys can trigger model switches
- **Idle unload** — automatically stops llama-server after configurable idle time, reloads on next request
- **Crash detection** — records up to 50 crash events with config snapshots for debugging
- **Backend verification** — post-switch check confirms the correct model is actually running

### Cooperative VRAM Management

Guardian coexists with other GPU services (ComfyUI, Frigate NVR) on shared hardware:

- **ComfyUI integration**: Before loading a model, Guardian calls ComfyUI's `POST /free` API to request graceful VRAM release. ComfyUI stays alive and auto-reloads models on its next workflow.
- **Frigate awareness**: Frigate's ~440MB ffmpeg VRAM usage is expected and accounted for in the VRAM budget — never touched.
- **VRAM scheduling**: Configurable hard limit (`proxy.vram_limit_mb`, default 27000) prevents OOM crashes.

### Security

- **Bearer token auth** on all endpoints via `config/api_keys.json`
- Token format: `{prefix}_{32-char-hex}` (e.g., `flip_abc123...`, `hydro_def456...`)
- **Model pinning** prevents unauthorized model switches
- **Switch allowlist** restricts which clients can trigger model changes
- **Backend verification** prevents desync between Guardian and llama-server

### Benchmarking & Optimization

- Automated benchmark suite across models × context sizes × batch sizes
- Resumable benchmark state persisted to `data/benchmark_state.json`
- `RequestOptimizer` injects best-known context/batch settings into requests
- Scheduled maintenance windows for unattended benchmark runs
- Real-time dashboard visualization of results

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
GLM-4.7-Flash-Claude-Opus-Reasoning:
  path: /home/flip/models/GLM-4.7-Flash-Claude-Opus-Reasoning-Q4_K_M.gguf
  context: 262144
  ngl: 99
  kv_type: q4_0
  backend: ik_fork
  tensor_split: "0.57,0.43"

guardian:
  pinned_model: "GLM-4.7-Flash-Claude-Opus-Reasoning"
  switch_allowlist: ["m0nk111", "oelala"]
  idle_unload_minutes: 5
```

40+ models configured, including vision models (mmproj support), MoE models, and reasoning models.

### `config/settings.yaml` — System Configuration

```yaml
proxy:
  port: 11434
  target: http://localhost:11440
  vram_limit_mb: 27000

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

Stores Bearer tokens with client names, creation timestamps, and optional metadata.

## API Reference

For detailed client integration examples (Python, TypeScript), queue-aware patterns, and error handling, see **[docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md)**.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/chat` | POST | Ollama-style chat (auto-switches model) |
| `/api/generate` | POST | Ollama-style prompt generation |
| `/api/tags` | GET | List available models (Ollama format) |
| `/api/version` | GET | Ollama version compat |
| `/v1/models` | GET | OpenAI model list |
| `/v1/chat/completions` | POST | OpenAI chat (auto-switches model) |
| `/v1/queue/status` | GET | Queue position, wait time, active requests |
| `/v1/{path}` | GET/POST | OpenAI-compatible proxy passthrough |
| `/api/status` | GET | Current model, health, VRAM, crash info |
| `/api/crashes` | GET | Crash history (up to 50 records) |
| `/admin/load` | POST | Reload llama-server (optionally specify model) |
| `/admin/unload` | POST | Stop llama-server to free VRAM |
| `/api/session/save` | POST | Save conversation context |
| `/api/session/load` | POST | Load conversation context |
| `/api/session/list` | GET | List saved sessions |
| `/api/stats` | GET | Dashboard stats (VRAM, models, records) |
| `/api/benchmark` | GET | Benchmark results & summary |
| `/api/benchmark/start` | POST | Trigger benchmark suite |
| `/api/benchmark/stop` | POST | Stop running benchmark |

All endpoints require `Authorization: Bearer <token>`.

## Directory Structure

```text
app/
├── engine/        # Model switching, process lifecycle, VRAM management
│   └── manager.py # ModelManager — core lifecycle control
├── proxy/         # HTTP API layer, auth, optimizer
│   ├── server.py  # FastAPI app, all endpoints, VramScheduler
│   ├── auth.py    # Bearer token auth (api_keys.json)
│   └── optimizer.py # Benchmark-driven request optimization
├── scheduler/     # Maintenance windows, service orchestration
│   └── manager.py # SchedulerManager — idle-window benchmarks
├── tweaker/       # Benchmarking logic, TPS measurement
│   └── benchmark.py # BenchmarkSuite — model × ctx × batch tests
├── ui/            # Dashboard (Tailwind dark mode, Chart.js)
└── main.py        # Application entrypoint (GuardianService)

config/
├── models.yaml          # Model registry (40+ models)
├── settings.yaml        # System config (ports, timeouts, scheduler)
├── api_keys.json        # API key store
├── current_model.args   # Runtime: active llama-server args
├── current_model.binary # Runtime: active backend binary path
└── current_model.env    # Runtime: per-model env vars (optional)

scripts/
├── start_llama.sh       # Backend startup wrapper
├── generate_key.py      # CLI key generation
├── test_system.py       # End-to-end system test
├── benchmark_context.py # Context size benchmarking
├── stress_test.py       # Load testing
└── ...                  # Analysis, vision tests, model sync

data/
└── benchmark_state.json # Persisted benchmark queue + results

docs/
├── BENCHMARK_SUMMARY.md      # Global rankings and model comparisons
├── CONTEXT_BENCHMARKS.md     # Optimal context sizes per model
├── REAL_BENCHMARK_RESULTS.md # Empirical test results
└── LLM_TERMINOLOGY.md       # Model collection overview + glossary
```

## GPU Environment

Guardian runs on a dual-GPU host and cooperates with other GPU services:

| GPU | VRAM | Role |
|-----|------|------|
| RTX 3060 (cuda:0) | 12GB | Model weight storage (tensor split) |
| RTX 5060 Ti (cuda:1) | 16GB | Primary compute + model weights |

**Coexisting services**:
- **Frigate NVR**: ~440MB on GPU (ffmpeg hardware decoding) — never touched
- **ComfyUI**: Releases VRAM on request via `/free` API — cooperative sharing

Models use configured tensor splits (e.g., `"0.57,0.43"`) to distribute weights across both GPUs. The VRAM budget (default 27000MB) accounts for Frigate's reservation.

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — Detailed architecture and design decisions
- [docs/BENCHMARK_SUMMARY.md](docs/BENCHMARK_SUMMARY.md) — Model performance rankings
- [docs/REAL_BENCHMARK_RESULTS.md](docs/REAL_BENCHMARK_RESULTS.md) — Empirical benchmark results
- [docs/CONTEXT_BENCHMARKS.md](docs/CONTEXT_BENCHMARKS.md) — Context size recommendations
- [docs/LLM_TERMINOLOGY.md](docs/LLM_TERMINOLOGY.md) — Model glossary and collection overview
