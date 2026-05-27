# Llama-CPP Guardian

> Hardware-aware queue manager and VRAM control plane for shared local LLM hosts.

Llama-CPP Guardian sits in front of `llama-server` and turns a raw inference
process into an operator-grade service. It serializes inference, owns backend
reloads, cooperates with ComfyUI to free VRAM, protects model switching with
auth and allowlists, and keeps a mixed 12 GB + 16 GB GPU host stable while
large-context text and vision runtimes share the same machine with other GPU
tenants.

## Why It Exists

Local multi-tenant inference turns into VRAM Tetris fast:

- a 256k context Qwen or Gemma runtime can reserve most of a shared dual-GPU
  budget by itself
- ComfyUI may keep several GB resident between workflows
- Frigate or other always-on GPU services never fully leave the box
- raw `llama-server` gives you no queue, no switch lock, no ComfyUI handshake,
  and no crash-aware reload recovery

Guardian adds those missing control surfaces so the host fails gracefully
instead of hard-crashing into CUDA OOM loops or restart storms.

## What Guardian Actually Does

- Single-slot FIFO inference queue with explicit request lifecycle tracking,
  disconnect-aware cleanup, queue polling, per-request status/cancel endpoints,
  and `X-Request-Id` / `X-Queue-Wait-Ms` headers
- Admission control for GPU-backed inference: unauthenticated requests never
  enter the queue, each API key may own multiple waiting requests but only one
  running GPU slot, and unknown model names fail fast with clear `404` payloads
- Model lifecycle ownership through `sudo systemctl start|stop llama-server`
- Hot-reloaded model registry from [config/models.yaml](config/models.yaml),
  including aliases, text and vision runtime fields, and switch policy
- Cooperative VRAM fencing via `POST {comfyui_url}/free` before every load or
  switch
- Auth-gated control plane on `:11434`, with model pinning and a switch
  allowlist
- Dashboard and monitoring surfaces on `:11437`, plus `/metrics` and
  `/api/status`
- Host-specific finetune v2 workflow that tunes `context`, `ngl`, and
  `tensor_split` without mutating `models.yaml` until `--apply`

## Runtime Topology

```text
Clients
  |
  v
Guardian proxy :11434
  - auth
  - queue
  - model switching
  - auto-reload / idle-unload
  - Ollama and OpenAI-compatible endpoints
  |
  v
llama-server :11440
  - official llama.cpp binary
  - started from scripts/start_llama.sh
  - args generated into config/current_model.args

Guardian UI :11437
  - dashboard
  - /api/stats
  - /api/benchmark
```

## Current Host Assumptions

Guardian is currently configured for a shared dual-GPU host with:

- `proxy.vram_limit_mb: 27000` in [config/settings.yaml](config/settings.yaml)
- mixed `tensor_split` profiles in [config/models.yaml](config/models.yaml)
- optional ComfyUI integration at `http://127.0.0.1:8188/free`
- backend path resolution via [app/paths.py](app/paths.py) and
  [scripts/start_llama.sh](scripts/start_llama.sh)

## Installation

### 1. Python environment

```bash
cd /home/flip/llama_cpp_guardian
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Backend prerequisites

Guardian does not spawn the backend directly with `subprocess.Popen`. It
expects:

- the official `llama-server` binary at
  `${LLAMA_CPP_OFFICIAL_ROOT}/build/bin/llama-server`
  or an explicit known-good `LLAMA_SERVER_BINARY` override
- GGUFs in `${MODELS_DIR}` (default: sibling `../models`)
- a `llama-server` systemd unit that starts
  [scripts/start_llama.sh](scripts/start_llama.sh)
- Guardian to have permission to run `sudo systemctl start llama-server` and
  `sudo systemctl stop llama-server`

Important: run the combined Guardian service with `python -m app.main`.
Starting only `uvicorn app.proxy.server:app` gives you the proxy API but not
the dashboard on `:11437`.

### 3. Configure the repo

Edit these files before the first load:

- [config/models.yaml](config/models.yaml): model paths, aliases, runtime
  fields, pinning, switch allowlist, idle unload
- [config/settings.yaml](config/settings.yaml): queue wait budget telemetry,
  VRAM budget, timeout tiers, ComfyUI URL, maintenance window
- [config/api_keys.json](config/api_keys.json): API keys used by clients

Create the first key with the bundled helper:

```bash
python scripts/generate_key.py local-dev --prefix flip
```

### 4. Start Guardian

```bash
cd /home/flip/llama_cpp_guardian
source venv/bin/activate
python -m app.main
```

This starts:

- the authenticated Guardian proxy on `http://127.0.0.1:11434`
- the dashboard on `http://127.0.0.1:11437`
- background startup verification and idle-unload watching

## Quickstart

### Load a model

```bash
export GUARDIAN_KEY="flip_your_key_here"

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-35b-uncensored"}' \
  http://127.0.0.1:11434/admin/load
```

### Send a chat completion

```bash
curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-35b-uncensored",
    "messages": [{"role": "user", "content": "Reply with exactly: GUARDIAN OK"}],
    "max_tokens": 16
  }' \
  http://127.0.0.1:11434/v1/chat/completions
```

### Inspect health and queue state

```bash
curl -sS http://127.0.0.1:11434/healthz

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  http://127.0.0.1:11434/api/status

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  http://127.0.0.1:11434/v1/queue/status

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  http://127.0.0.1:11434/v1/queue/requests/<request-id>

curl -sS -X DELETE \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  http://127.0.0.1:11434/v1/queue/requests/<request-id>
```

### Open the dashboard

Browse to `http://127.0.0.1:11437/`.

## Key Files

| File | Purpose |
| --- | --- |
| [config/models.yaml](config/models.yaml) | Model registry, aliases, text and vision runtime fields, Guardian policy |
| [config/settings.yaml](config/settings.yaml) | Queue, timeouts, VRAM budget, ComfyUI URL, maintenance schedule |
| [config/api_keys.json](config/api_keys.json) | Bearer and x-api-key registry |
| [config/current_model.args](config/current_model.args) | Generated `llama-server` arguments for the active runtime |
| [scripts/start_llama.sh](scripts/start_llama.sh) | Backend launcher used by the `llama-server` service |
| [data/api_usage_state.json](data/api_usage_state.json) | Persistent usage snapshot for the dashboard |
| [data/model_finetune_v2_results.json](data/model_finetune_v2_results.json) | Append-only finetune v2 results log |

## Documentation Map

Detailed docs now live under `docs/` instead of cluttering the repo root.

Client maintainers should start with
[docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md). It is the canonical
handoff document for auth, model discovery, queue ownership, rejection
contracts, polling, and timeout behavior.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/HARDWARE_TUNING.md](docs/HARDWARE_TUNING.md)
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md)
- [docs/FINETUNE_V2_REQUIREMENTS.md](docs/FINETUNE_V2_REQUIREMENTS.md)
- [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md)

## Security Model

On port `11434`, every endpoint requires authentication except:

- `GET /healthz`
- `GET /metrics`

Guardian accepts:

- `Authorization: Bearer <token>`
- `x-api-key: <token>`
- `api-key: <token>`

Model switching can be restricted with:

- `guardian.pinned_model`
- `guardian.switch_allowlist`

Note: the dashboard port `11437` is a separate UI surface. In current code,
its `/` and `/api/*` endpoints are not auth-gated. Protect that port at the
network or reverse-proxy layer if it is not localhost-only.
