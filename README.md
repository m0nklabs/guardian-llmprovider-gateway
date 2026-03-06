# Llama CPP Guardian

Guardian is a proxy, lifecycle manager, and benchmark layer for `llama-server`, built to make local LLM serving predictable on shared GPU hardware.

## Core Model

```
Client / App / Tooling
        │
        ▼
Guardian Proxy :11434
  ├─ Ollama-compatible endpoints
  ├─ OpenAI-style /v1 endpoints
  ├─ auth + routing
  ├─ model selection / switching
  └─ request optimization
        │
        ▼
llama-server :11440
```

Dashboard/UI runs separately on port `11437`.

## Dual Backend Strategy

Guardian intentionally supports two llama.cpp backends:

- **Primary**: `ik_llama.cpp` fork for almost all models
- **Fallback**: official `llama.cpp` for architectures the ik fork does not support well

Backend selection is controlled per model in `config/models.yaml`.

## What Guardian Handles

### Request Proxying
- OpenAI-style `/v1/chat/completions`
- model listing
- Ollama-compatible `/api/chat`, `/api/generate`, `/api/tags`, `/api/version`

### Model Lifecycle
- detects requested model
- switches llama-server arguments/binary when needed
- uses a concurrency lock to prevent overlapping model switches
- supports model pinning
- unloads idle models to free VRAM
- detects crashes and recovers state more cleanly than a raw server wrapper

### Performance / Capacity
- multi-GPU tensor-split support for large models
- per-tier timeout logic
- benchmark-driven optimization hints
- resumable benchmark state

### Compatibility
- OpenWebUI compatibility improvements
- API key auth rather than permissive basic auth
- vision-model support in the current stack

## Recent Direction

Important recent changes reflected in the current codebase:

- model pinning
- crash detection improvements
- idle unload behavior
- vision model support
- scheduler settings wired from config instead of hardcoded values
- benchmark fixes for current API behavior
- auth coverage for metadata/model-info endpoints

## Running Guardian

```bash
pip install -r requirements.txt
python3 app/main.py
```

## Key Configuration Files

### `config/models.yaml`

Defines:
- model paths
- backend selection
- context sizes
- GPU/tensor split settings
- extra llama-server arguments

### `config/settings.yaml`

Defines:
- proxy port and target
- VRAM budget
- scheduler timing and managed services
- timeout tiers
- benchmark behavior

## Main Directories

```text
app/
├── engine/        # model switching and process lifecycle
├── proxy/         # HTTP API layer, auth, optimizer
├── scheduler/     # maintenance windows and service orchestration
├── tweaker/       # benchmarking logic
├── ui/            # dashboard assets and UI code
└── main.py        # application entrypoint

config/
├── models.yaml
├── settings.yaml
├── api_keys.json
├── current_model.args
└── current_model.binary

scripts/
└── start_llama.sh
```

## GPU Notes

Guardian runs on a mixed-GPU host and needs to coexist with other GPU consumers.

Typical setup:
- RTX 3060 12GB
- RTX 5060 Ti 16GB
- Frigate using part of the GPU budget independently

Large models use configured tensor splits so the system can stay responsive without blind OOM roulette.

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [docs/BENCHMARK_SUMMARY.md](docs/BENCHMARK_SUMMARY.md)
- [docs/REAL_BENCHMARK_RESULTS.md](docs/REAL_BENCHMARK_RESULTS.md)
