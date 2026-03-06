# Guardian Architecture

## Overview

Guardian sits in front of `llama-server` and turns a raw local inference process into a manageable service.

It is responsible for request translation, auth, model switching, lifecycle recovery, benchmarking, and GPU-aware orchestration.

## Current Topology

```
Clients / apps / tools
        │
        ▼
Guardian proxy (:11434)
  ├─ auth
  ├─ endpoint compatibility layer
  ├─ model routing
  ├─ optimizer
  └─ scheduler integration hooks
        │
        ▼
llama-server (:11440)
        │
        ▼
Configured backend binary
  ├─ ik_llama.cpp fork (default)
  └─ official llama.cpp (fallback for unsupported architectures)
```

Dashboard/UI runs separately on `:11437`.

## Core Responsibilities

### 1. Protocol Compatibility

Guardian exposes both:

- OpenAI-style `/v1/*` endpoints
- Ollama-style `/api/*` endpoints

This lets clients with very different assumptions talk to the same local serving layer.

### 2. Model Lifecycle Management

Guardian owns the active `llama-server` process and decides:

- which model should be loaded
- which backend binary to use
- which arguments should be passed
- whether an idle model should be unloaded

Important current safeguards:
- concurrency lock around model switching
- crash detection
- active model pinning
- dynamic reading of current model args/binary files

### 3. Auth and Access Control

Guardian uses API key / bearer-style auth, not the older permissive basic-auth concept that earlier docs described.

Auth covers the proxy surface, including metadata/model-info style endpoints that previously lagged behind.

### 4. Benchmarking and Optimization

Guardian benchmarks configured models and records performance data such as:

- tokens per second
- latency / time to first token
- context-size behavior
- GPU/VRAM fit characteristics

Benchmark runs are resumable and no longer block the main proxy event loop.

### 5. Scheduler / Maintenance Windows

The scheduler can:

- run benchmarks during configured idle windows
- stop or start configured external services during maintenance
- use values sourced from `settings.yaml` rather than hardcoded defaults

## Backend Selection Model

Per-model backend selection is part of the design:

- **ik fork** for the normal fast path
- **official llama.cpp** when a model architecture demands it

This matters because not every GGUF model behaves equally well across both backends.

## Configuration Surfaces

### `config/models.yaml`

Defines model-level runtime behavior:
- model path
- context size
- kv cache type
- backend choice
- tensor split
- extra args

### `config/settings.yaml`

Defines system-level behavior:
- proxy ports and target
- VRAM budget
- timeout tiers
- scheduler windows
- benchmark settings
- managed services

## GPU / VRAM Strategy

Guardian assumes a shared GPU environment and should not behave like it owns the whole machine.

Key principles:
- use tensor splits for large models
- respect configured VRAM ceilings
- unload idled models when helpful
- coexist with other GPU consumers on the host

## Directory Overview

```text
app/
├── engine/      # process lifecycle and model switching
├── proxy/       # API layer, auth, protocol translation, optimizer
├── scheduler/   # scheduled maintenance and benchmark orchestration
├── tweaker/     # benchmark logic and result handling
├── ui/          # dashboard UI
└── main.py      # startup wiring

config/
├── models.yaml
├── settings.yaml
├── api_keys.json
├── current_model.args
└── current_model.binary

data/
├── benchmark_state.json
├── benchmark_results / stats files
└── runtime state artifacts
```

## Operational Reality

Guardian is not just a reverse proxy.

It is effectively the control plane for local llama.cpp serving on this machine:
- it decides what is loaded
- it constrains how requests reach the backend
- it captures performance knowledge over time
- it shields clients from backend churn

## Documentation Notes

Older docs referred to permissive Basic Auth and older port assumptions. Those descriptions are obsolete and should not be used as implementation truth. The current source of truth is the code plus `config/models.yaml` and `config/settings.yaml`.
