# Guardian

> Policy and traffic gateway for connected services: secure access, routing,
> capacity control and observability.

Guardian sits between clients and explicitly onboarded services. Its existing
implementation routes local/cloud LLM requests and TTS/STT speech requests,
with authentication, queueing, failover, streaming and capture mechanisms.
The product direction expands that shared foundation to registered non-AI HTTP
services and, later, tool endpoints; these extensions are planned, not already
implemented. Generalized authorization and load balancing have explicit delivery
gates rather than being inferred from today's keys and ordered failover.

Guardian is the gateway above the conceptual **`{provider}/{brand}/{service}`**
namespace: specific serving operator/platform offering, product or model maker
namespace, and concrete application/API service/model. Existing cloud
`{provider}/{brand}/{model}` addresses are its LLM specialization, with no
mandatory `guardian/` prefix. APIs are interfaces, LLMs are capabilities, engines
execute services, and bridges adapt protocols; these are not alternative address
layers. Unrelated existing aliases and upstream IDs remain unchanged; the
Copilot-specific migration below removes its old route. See the
[canonical naming contract](docs/ROADMAP.md#service-naming-and-namespace-canonical-conceptual-contract)
for compatibility boundaries and the unresolved policy for services without a
natural brand namespace; this is not a runtime identifier migration or a generic
resolver.

Concrete Copilot destinations follow the same three layers:
`github-copilot/openai/gpt-6-astra` and `github-copilot/openai/gpt-6.1-sol`.
Here `github-copilot` identifies GitHub's Copilot offering, not every GitHub API;
`openai` is the model brand. There is no separate `copilot` channel layer.
The migration removes `github/copilot/gpt-6.1-sol`: only the two canonical
Copilot destinations above remain, with no legacy Copilot aliases. They are
served through an external LiteLLM bridge that adapts Chat Completions to the
upstream Responses-only GPT models; see [the routing migration contract](docs/LLM_ROUTER.md#github-copilot-provider-and-external-bridge-migration).
The bridge is deployed and running (LiteLLM process with its own listen port;
unauthenticated requests are rejected).

Start with the [product roadmap](docs/ROADMAP.md) for scope, priorities,
architecture boundaries and acceptance criteria. The
[Guardian 2.0 implementation plan](docs/IMPLEMENTATION_PLAN.md) is the historical
F0–F7 migration specification, not the current product roadmap.

**Documentation caveat:** the operational sections below keep host-specific
details and examples. For current configuration and operations, the canonical
sources are [the config schema](docs/CONFIG_SCHEMA.md),
[provider files](docs/CONFIG_PROVIDER_FILES.md), [LLM routing](docs/LLM_ROUTER.md)
and [the operator runbook](docs/skills/operator-runbook.md); where they and this
README disagree, they win. Broader documentation reconciliation is tracked in
roadmap phase G0; this README was last reconciled against the running system on
2026-10-04.

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

- Authenticated inference gateway with model resolution, cloud routing, ordered
  failover with health tracking, and cloud rate limiting
- Protocol surfaces on the same authenticated port: OpenAI Chat Completions
  (`/v1/chat/completions`), the OpenAI Responses API (`/v1/responses`, native
  llama.cpp passthrough plus a full Responses⇄chat translation for cloud
  providers), Anthropic Messages (`/v1/messages`, native or translated), legacy
  completions/embeddings, and Ollama-compatible `/api/chat` + `/api/generate`
- Service-tier support for cloud routes: `service_tier` rides through on every
  ingress form, `:nitro`/`:floor` model variants survive resolution, and the
  OpenRouter provider defaults unpinned requests to `flex`
- Inference queue with explicit request lifecycle tracking, disconnect-aware
  cleanup, queue polling, per-request status/cancel endpoints, and
  `X-Request-Id` / `X-Queue-Wait-Ms` headers
- Admission control for GPU-backed inference: unauthenticated requests never
  enter the queue, each API key may own multiple waiting requests but only one
  running GPU slot, and unknown model names fail fast with clear `404` payloads
- Backend lifecycle through the llama.cpp caretaker (`caretaker-llamacpp`
  service, control API on `:11441`): remote-first idempotent `/ensure` for
  model switches and runtime flips, with crash-aware reload recovery
- Hot-reloaded model registry from [config/providers/ai-node-local.settings.yaml](config/providers/ai-node-local.settings.yaml),
  including aliases, text and vision runtime fields, and switch policy
- Cooperative VRAM fencing via `POST {comfyui_url}/free` before every load or
  switch
- Auth-gated control plane on `:11434` (HTTP and TLS through the nginx stream
  multiplexer), with model pinning and a switch allowlist
- Dashboard and monitoring surfaces on `:11437` (API endpoints require a
  Bearer key), plus `/metrics` and `/api/status`
- Privacy-aware capture: raw request/response events in a JSONL WAL under
  `data/capture/` with media extraction and offline redaction
  (`scripts/keanu_redact.py`, `scripts/guardianctl.py`)
- Host-specific finetune v2 workflow that tunes `context`, `ngl`, and
  `tensor_split` without mutating the model registry until `--apply`

## Runtime Topology

```text
Clients
  |
  v
nginx stream mux :11434        (plain HTTP and TLS on the same port)
  |
  v
Guardian proxy :11434
  - auth
  - queue + admission control
  - cloud routing / failover
  - OpenAI, Responses, Anthropic and Ollama protocol surfaces
  |
  ├─> llama-server :11440      (official llama.cpp binary, lifecycle via
  |                             the caretaker :11441, args in
  |                             config/current_model.args)
  └─> cloud providers          (openrouter, nvidia, google, groq, … from
                                config/providers/*.settings.yaml)

Guardian UI :11437
  - dashboard
  - /api/stats
  - /api/benchmark
```

## Current Host Assumptions

Guardian is currently configured for a shared dual-GPU host with:

- `proxy.vram_limit_mb: 27000` in [config/global.settings.yaml](config/global.settings.yaml)
- mixed `tensor_split` profiles in the local model registry
  ([config/providers/ai-node-local.settings.yaml](config/providers/ai-node-local.settings.yaml))
- optional ComfyUI integration at `http://127.0.0.1:8188/free`
- backend path resolution via [app/paths.py](app/paths.py) and
  [scripts/start_llama.sh](scripts/start_llama.sh)

## Installation

### 1. Python environment

```bash
cd /home/flip/guardian-llmprovider-gateway
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
- on this host, the live build is
  `/home/flip/llama_cpp_official/worktrees/cuda128-laguna-tq-full/build-cuda128-full/bin/llama-server`,
  pinned through the `llama-server.service.d/20-turboquant-backend.conf`
  systemd drop-in (`LLAMA_SERVER_BINARY`)
- GGUFs in `${MODELS_DIR}` (default: sibling `../models`)
- the backend lifecycle owned by the llama.cpp caretaker
  (`caretaker-llamacpp.service`, control API on `:11441`), which launches
  [scripts/start_llama.sh](scripts/start_llama.sh); Guardian delegates model
  switches and runtime flips to the caretaker (remote-first idempotent
  `/ensure` with local fallback)

Important: run the combined Guardian service with `python -m app.main`.
Starting only `uvicorn app.proxy.server:app` gives you the proxy API but not
the dashboard on `:11437`.

### 3. Configure the repo

Edit these files before the first load:

- [config/providers/ai-node-local.settings.yaml](config/providers/ai-node-local.settings.yaml):
  local model registry, aliases, runtime fields, pinning, switch allowlist
- [config/global.settings.yaml](config/global.settings.yaml): proxy, queue,
  timeouts, VRAM budget, capture policy, context overrides, ComfyUI URL,
  maintenance window, cloud provider registry wiring
- [config/providers/](config/providers/): one settings file per provider
  (base URL, key reference, models, model defaults such as `service_tier`)
- [config/guardian.keys.yaml](config/guardian.keys.yaml): API keys used by clients

Create the first key with the bundled helper:

```bash
python scripts/generate_key.py local-dev --prefix flip
```

### Model context metadata

Guardian always returns a positive context window for every model it lists or
routes. OpenAI-compatible discovery exposes `context_length`, `meta.n_ctx`,
and `max_input_tokens`; Ollama-compatible `POST /api/show` exposes
`model_info.general.context_length`.

Context metadata resolves in a fixed order: per-model `context_window` entries
in a provider's `models:` block (the successor of the former
`context_overrides` map), cloud-provider overrides, the live catalog or the
active local backend's `/props` `n_ctx`, and finally a conservative `131072`
fallback with a warning when no source is available. Keys use the canonical
upstream model ID:

```yaml
# config/providers/<provider>.settings.yaml
models:
  deepseek/deepseek-chat:
    context_window: 1000000
```

### 4. Start Guardian

```bash
cd /home/flip/guardian-llmprovider-gateway
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

### Send a Responses API request

The same gateway also speaks the OpenAI Responses API (`client.responses.create`,
Codex CLI) for local and cloud models:

```bash
curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-35b-uncensored",
    "input": "Reply with exactly: GUARDIAN OK",
    "max_output_tokens": 16
  }' \
  http://127.0.0.1:11434/v1/responses
```

Anthropic-protocol clients use `/v1/messages` (see
[docs/ANTHROPIC_BRIDGE.md](docs/ANTHROPIC_BRIDGE.md)).

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
| [config/global.settings.yaml](config/global.settings.yaml) | Proxy, queue, timeouts, VRAM budget, capture policy, context overrides, ComfyUI URL |
| [config/providers/](config/providers/) | Per-provider settings: base URL, key reference, models, model defaults (`service_tier`, `context_window`, …) |
| [config/providers/ai-node-local.settings.yaml](config/providers/ai-node-local.settings.yaml) | Local model registry, aliases, runtime fields, Guardian policy |
| [config/guardian.keys.yaml](config/guardian.keys.yaml) | Bearer and x-api-key registry |
| [config/current_model.args](config/current_model.args) | Generated `llama-server` arguments for the active runtime |
| [scripts/start_llama.sh](scripts/start_llama.sh) | Backend launcher used by the caretaker |
| [data/api_usage_state.json](data/api_usage_state.json) | Persistent usage snapshot for the dashboard |
| [data/capture/](data/capture/) | Capture WAL (raw events + media extraction) |
| [data/model_finetune_v2_results.json](data/model_finetune_v2_results.json) | Append-only finetune v2 results log |

## Documentation Map

Detailed docs now live under `docs/` instead of cluttering the repo root.

Client maintainers should start with
[docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md). It is the canonical
handoff document for auth, model discovery, queue ownership, rejection
contracts, polling, and timeout behavior.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md)
- [docs/LLM_ROUTER.md](docs/LLM_ROUTER.md) (cloud routing, failover, Responses ingress, service tiers)
- [docs/ANTHROPIC_BRIDGE.md](docs/ANTHROPIC_BRIDGE.md)
- [docs/CONFIG_SCHEMA.md](docs/CONFIG_SCHEMA.md) and [docs/CONFIG_PROVIDER_FILES.md](docs/CONFIG_PROVIDER_FILES.md)
- [docs/HARDWARE_TUNING.md](docs/HARDWARE_TUNING.md)
- [docs/FINETUNE_V2_REQUIREMENTS.md](docs/FINETUNE_V2_REQUIREMENTS.md)

## Security Model

On port `11434`, every endpoint requires authentication except:

- `GET /healthz`
- `GET /metrics`

Port `11434` serves both plain HTTP and TLS: an nginx stream multiplexer
pre-reads the TLS ClientHello and passes TLS unchanged to Guardian on
`127.0.0.1:11435`, routing plain HTTP through nginx on `127.0.0.1:11436`
(see `deploy/nginx/`).

Guardian accepts:

- `Authorization: Bearer <token>`
- `x-api-key: <token>`
- `api-key: <token>`

Model switching can be restricted with:

- `guardian.pinned_model`
- `guardian.switch_allowlist`

The dashboard on `:11437` is a separate UI surface; its `/api/*` endpoints
require a Bearer key (the dashboard stores
`guardian_dashboard_api_key` in localStorage). Keep the port localhost-only
or protect it at the network/reverse-proxy layer.
