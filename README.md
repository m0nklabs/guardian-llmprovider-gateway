# Llama CPP Guardian

An intelligent proxy and management layer for llama-server (llama.cpp), providing model switching, VRAM scheduling, benchmarking, and Ollama API compatibility.

## Architecture

```
Client (Copilot, OpenWebUI, etc.)
  │
  ▼
Guardian Proxy (Port 11434) ── Ollama-compat + OpenAI /v1/ API
  │
  ▼
llama-server (Port 11440) ── Raw llama.cpp backend (locked)
```

### Dual-Backend System
- **Primary**: `ik_llama.cpp` fork (ikawrakow) — optimized, used for all models by default
- **Fallback**: Official `llama.cpp` (ggml-org) — only for unsupported architectures (e.g., Nemotron)
- Backend selection per model via `backend: official` in `models.yaml`

## Features

1. **Guardian Proxy (Port 11434)**
   - Intercepts and routes requests to llama-server (Port 11440)
   - **Auto Model Switching**: Detects requested model, stops/starts llama-server with correct args
   - **Concurrency Protection**: Lock prevents race conditions during model switches
   - **Multi-GPU tensor-split**: Distributes model weights across GPUs (RTX 3060 + RTX 5060 Ti)
   - **Ollama API Bridge**: Translates `/api/chat`, `/api/generate`, `/api/tags` to OpenAI format
   - **OpenAI Compatibility**: Native `/v1/chat/completions`, `/v1/models` passthrough
   - **API Key Auth**: Bearer token authentication (`flip_` prefix keys)
   - **Session Management**: Save/load KV cache slots for context preservation
   - **Dynamic Timeouts**: Configurable per-model-tier timeout from `settings.yaml`

2. **Benchmark Suite**
   - Automated testing across all configured models with variable context/batch sizes
   - **Resumable**: State persisted to `data/benchmark_state.json`
   - **Non-blocking**: Runs via `asyncio.to_thread()` to avoid blocking the proxy event loop
   - **Record Tracking**: Logs 🏆 records when TPS improves for a model
   - Models loaded dynamically from `models.yaml` config

3. **Scheduler**
   - Runs benchmarks during configurable idle window (default: 04:00-11:00 weekdays)
   - Stops/starts configured services during maintenance mode
   - All settings loaded from `settings.yaml`

4. **Request Optimizer**
   - Learns optimal `num_ctx` from benchmark results
   - Auto-injects optimized settings when not explicitly set by client

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Start Guardian (proxy on 11434, UI on 11437)
python3 app/main.py

# Generate an API key
python3 -m app.proxy.auth <client-name>
```

## Configuration

### models.yaml
Each model entry supports:
```yaml
models:
  model-name:
    path: /path/to/model.gguf
    context: 131072          # Context window size
    ngl: 99                  # GPU layers (99 = all)
    kv_type: q4_0            # KV cache quantization (q4_0, q8_0, f16)
    backend: ik_fork         # Backend binary (ik_fork or official)
    tensor_split: "0.55,0.45"  # Multi-GPU weight distribution
    extra_args: ""           # Additional llama-server CLI flags
```

### settings.yaml
```yaml
proxy:
  port: 11434
  target: http://localhost:11440
  vram_limit_mb: 27000       # Total usable VRAM budget

benchmark:
  schedule:
    start_hour: 4
    end_hour: 11
    days: ["mon", "tue", "wed", "thu", "fri"]

services_to_stop:            # Services paused during benchmarks
  - caramba-backend
  - agent-forge

timeouts:                    # Per-tier request timeouts
  tiers:
    tier_70b: { min_size_mb: 40000, timeout_seconds: 1800 }
    tier_32b: { min_size_mb: 20000, timeout_seconds: 1200 }
    # ...
```

## Directory Structure

```
app/
├── engine/manager.py     # Model switching, server args, dual-backend
├── proxy/
│   ├── server.py         # FastAPI proxy, Ollama bridge, OpenAI passthrough
│   ├── auth.py           # API key management
│   └── optimizer.py      # Benchmark-driven request optimization
├── scheduler/manager.py  # Idle-window scheduling
├── tweaker/benchmark.py  # Benchmark suite
├── ui/                   # Dashboard (port 11437)
└── main.py               # Entry point
config/
├── models.yaml           # Model definitions + tensor_split
├── settings.yaml         # Proxy, scheduler, timeout config
├── api_keys.json         # API keys store
├── current_model.args    # Active model CLI args (dynamic)
└── current_model.binary  # Active backend binary path (dynamic)
scripts/
└── start_llama.sh        # Systemd wrapper, reads args/binary from config
```

## GPU Setup

| GPU | Model | VRAM | Role |
|-----|-------|------|------|
| GPU 0 | RTX 3060 | 12 GB | LLM weights (55%) |
| GPU 1 | RTX 5060 Ti | 16 GB | LLM weights (45%) + Frigate NVR (~1.5 GB) |

Models >12 GB use `tensor_split` to distribute across both GPUs, leaving room for Frigate on GPU 1.
