# Guardian LLM Router — Cloud Provider Integration

Guardian acts as a **unified LLM router**: it serves local GPU-backed models via
`llama-server` **and** transparently forwards requests for cloud-hosted models
to upstream providers like **OpenRouter**, **NVIDIA NIM**, and **Poolside Platform**.

Clients talk to a single endpoint (`http://guardian:11434/v1/chat/completions`)
and just specify the model name. Guardian automatically routes to the right
backend — local GPU or cloud API — based on the model name in the request.

## How It Works

```
Client (OpenAI-compatible)
    │
    │  POST /v1/chat/completions  {"model": "openai/gpt-4o", ...}
    │
    ▼
┌──────────────────────────────────┐
│         Guardian Proxy            │
│  ┌────────────────────────────┐  │
│  │   ProviderRegistry         │  │
│  │   model → provider map     │  │
│  └────────────┬───────────────┘  │
│               │                   │
│      ┌────────┴────────┐         │
│      ▼                 ▼         │
│  Cloud model?      Local model?   │
│      │                 │         │
│      ▼                 ▼         │
│  Forward to       Queue → VRAM   │
│  cloud API        scheduler →     │
│  (bypass queue)   llama-server    │
└──────────────────────────────────┘
    │                    │
    ▼                    ▼
 OpenRouter          llama-server
 NVIDIA NIM          (local GPU)
```

### Key Differences: Cloud vs Local

| Aspect | Local GPU models | Cloud provider models |
| --- | --- | --- |
| **Backend** | `llama-server` on `:11440` | OpenRouter / NVIDIA / Poolside API |
| **Queue** | Inference queue (serialized, single-slot) | Bypassed — cloud handles concurrency |
| **VRAM** | VRAM scheduler, model switching, idle unload | Not applicable |
| **Model switching** | Auto-switch with allowlist | Not needed — each request is independent |
| **Rate limiting** | Guardian queue (max_concurrent) | Cloud provider's own limits |
| **Usage tracking** | Token usage from llama-server response | Token usage from cloud API response |
| **Streaming** | SSE passthrough with watchdog | SSE passthrough with watchdog |

## Namespace-based cloud recognition (key-independent)

Guardian recognises a request as a **cloud** model vs. a **local** model purely
from the `model` name in the request — independently of which Guardian API key
made the request. A model is treated as cloud-hosted when it matches either:

1. **an explicit entry** in a provider's `models:` list (exact match wins, so a
   model listed under more than one provider is disambiguated), or
2. **a namespace prefix** in that provider's `model_prefixes:` list (e.g.
   `nvidia/`, `anthropic/`, `openai/`).

This lets a client send a raw upstream model name that is *not* explicitly
listed (e.g. `anthropic/claude-opus-4.1`) and have Guardian forward it to the
matching provider using that provider's **global** API key from `settings.yaml`
— no `guardian/{provider}/{model}` per-key route required.

```yaml
providers:
  nvidia:
    model_prefixes: [nvidia/, deepseek-ai/, minimaxai/]
    models: [...]
  openrouter:
    model_prefixes: [anthropic/, openai/, google/, meta-llama/, deepseek/, qwen/, mistralai/]
    models: [...]
```

Prefixes match whole namespace segments (a trailing `/` is enforced, so `nvidia`
matches `nvidia/...` but not `nvidia-foo`). Provider declaration order in
`settings.yaml` breaks ties between overlapping prefixes; keep provider
namespace sets disjoint to avoid ambiguity. Prefix-matched models are routable
on every inference endpoint (`/v1/chat/completions`, `/v1/messages`,
`/v1/completions`, `/api/chat`, `/api/generate`) even though they do not appear
in `GET /v1/models` (discovery lists only explicitly configured models).

## Per-Key Cloud Credential Routing

In addition to the global provider config in `settings.yaml`, Guardian supports
**per-key cloud credential routing** — each Guardian API key can be linked to
its own cloud credentials with specific model lists.

### How It Works

```
Client sends: {"model": "guardian/nvidia/minimax/minimax-m3"}
                       │
                       ▼
         Guardian parses: provider=nvidia, model=minimax/minimax-m3
                       │
                       ▼
         Looks up client's key_fingerprint in cloud_keys.json
                       │
                       ▼
         Finds linked NVIDIA credential with API key
                       │
                       ▼
         Rewrites model to "minimax/minimax-m3"
         Forwards to NVIDIA API with the credential's API key
```

### Route Convention

Use the `guardian/{provider}/{model_path}` format:

| Route | Provider | Upstream Model |
| --- | --- | --- |
| `guardian/nvidia/minimax/minimax-m3` | NVIDIA | `minimax/minimax-m3` |
| `guardian/nvidia/deepseek-ai/deepseek-r1` | NVIDIA | `deepseek-ai/deepseek-r1` |
| `guardian/openrouter/openai/gpt-4o` | OpenRouter | `openai/gpt-4o` |
| `guardian/openrouter/anthropic/claude-3.5-sonnet` | OpenRouter | `anthropic/claude-3.5-sonnet` |
| `guardian/google/gemini-2.5-flash` | Google AI Studio | `gemini-2.5-flash` |

### Google AI Studio Catalogs

Google AI Studio credentials use the OpenAI-compatible endpoint at
`https://generativelanguage.googleapis.com/v1beta/openai`. When a `google`
credential is added, Guardian requests its available models and stores the
validated catalog with the credential. Every Guardian key linked to that
credential discovers the resulting routes as `guardian/google/<model>`.

The catalog is intentionally tied to the credential rather than a global
provider setting, so an unlinked Guardian key cannot discover or use the
credential's Google quota. Google publishes new models over time; refresh an
existing credential to replace its stored catalog. If the upstream request
fails or returns invalid data, Guardian keeps the last successful catalog.
Guardian also rejects a Google route that is absent from the stored catalog.

The Guardian key that creates a credential owns its management access. Only
that key can list, refresh, modify, delete, or link the credential. An owner
may explicitly share the credential with another Guardian key through the link
endpoint; shared keys can use the linked routes but cannot manage the
credential itself. Existing credentials with exactly one link retain that
linked key as their owner; ambiguous legacy credentials remain inference-only
until they are replaced.

### Managing Credentials

Use the admin API endpoints (visible in the Guardian dashboard at `:11437`):

```bash
# Add a cloud credential
curl -X POST http://localhost:11434/api/cloud/credentials \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "nvidia",
    "name": "NVIDIA Default",
    "api_key": "nvapi-xxx",
    "models": ["minimax/minimax-m3", "deepseek-ai/deepseek-r1"]
  }'

# Add a Google AI Studio credential. Google supplies the model list; omit models.
curl -X POST http://localhost:11434/api/cloud/credentials \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "google",
    "name": "Google AI Studio",
    "api_key": "${GOOGLE_API_KEY}"
  }'

# Refresh every discoverable guardian/google/<model> route for a credential.
curl -X POST http://localhost:11434/api/cloud/credentials/cred_001/refresh-models \
  -H "Authorization: Bearer flip_..."

# Generate a Guardian API key
curl -X POST http://localhost:11434/api/keys \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app", "prefix": "myapp"}'

# Link the credential to a key
curl -X POST http://localhost:11434/api/cloud/links \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "guardian_key_fingerprint": "abc123def456",
    "provider": "nvidia",
    "credential_id": "cred_001"
  }'

# List available cloud models for the requesting key
curl http://localhost:11434/api/cloud/models \
  -H "Authorization: Bearer flip_..."
```

### Dashboard UI

The Guardian dashboard at `http://localhost:11437` now includes:

- **🔑 Guardian API Keys** — generate new keys, list existing keys with fingerprints
- **☁️ Cloud Credentials** — add/delete cloud provider credentials (NVIDIA, OpenRouter, Google AI Studio)
- **🔗 Key Links** — link cloud credentials to Guardian API keys
- **🧭 Available Cloud Models** — shows all cloud models (global + per-key routes)

### Intelligent 429 handling

Cloud inference requests are held by Guardian when an upstream provider returns
HTTP 429. The retry policy is per Guardian API key and provider, so one key's
rate limit does not delay another key. Guardian first honors `Retry-After`, then
provider `X-RateLimit-Reset` hints, and otherwise uses bounded exponential
backoff with jitter. The defaults are configured in `settings.yaml`:

```yaml
cloud_retry:
  enabled: true
  max_retries: 3
  max_hold_seconds: 90
  base_backoff_seconds: 1
  max_backoff_seconds: 30
  jitter_factor: 0.25
  respect_retry_after: true
```

The current in-memory counters and safe provider details can be read with:

```bash
curl http://localhost:11434/api/cloud/ratelimit-stats \
  -H "Authorization: Bearer flip_..."
```

The response includes total 429s, retries, retry successes, exhausted retry
budgets, current cooldown, remaining/limit hints, reset time, and the latest
provider error message per Guardian-key fingerprint and provider. A final 429
is returned only after the retry count or hold-time budget is exhausted. For
`guardian/failover/{group}` routes, Guardian then tries the next configured
provider before returning 429 to clients that do not implement retries. A 429
does not trip cross-provider failover health.

## Vision-aware Cloud Routing

Cloud candidates declare their supported input modalities in
`config/cloud_keys.json`. Guardian leaves text requests on the selected cloud
route. When a request includes an image, it only selects a local vision fallback
when the resolved cloud model is explicitly text-only and has an
`image_fallback.local_model` configured. This applies equally to direct
`guardian/{provider}/{model}` routes, global cloud model names, and failover
routes.

For a failover group containing both text-only and image-capable candidates,
Guardian forwards image requests only to image-capable candidates. It does not
start a local vision runtime merely because an image is present. Restart
`llama-guardian.service` after changing this capability configuration.

## Configuration

Cloud providers are configured in [`config/settings.yaml`](../config/settings.yaml)
under the top-level `providers` key:

```yaml
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}
    timeout_seconds: 600
    models:
      - anthropic/claude-3.5-sonnet
      - openai/gpt-4o
      - google/gemini-2.0-flash-exp
      - meta-llama/llama-3.3-70b-instruct

  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: ${NVIDIA_API_KEY}
    timeout_seconds: 600
    models:
      - nvidia/llama-3.1-nemotron-70b-instruct
      - deepseek-ai/deepseek-r1

  poolside:
    enabled: true
    base_url: https://inference.poolside.ai/v1
    api_key: ${POOLSIDE_API_KEY}
    timeout_seconds: 600
    model_prefixes: [poolside/]
    models:
      - poolside/laguna-xs-2.1
      - poolside/laguna-s-2.1
```

### API Key Security

API keys support **environment variable expansion** using `${VAR_NAME}`
syntax. This keeps secrets out of the repository:

```bash
# Set environment variables before starting Guardian
export OPENROUTER_API_KEY="sk-or-v1-..."
export NVIDIA_API_KEY="nvapi-..."
export POOLSIDE_API_KEY="<poolside-api-key>"
```

If an environment variable is not set, the key expands to an empty string.
Requests for that provider's models will return `503 provider_unavailable`
until the key is configured. Guardian does not advertise those unusable global
model IDs through `GET /v1/models` or `GET /api/cloud/models`. Per-key
`guardian/{provider}/{model}` routes backed by credentials in
`config/cloud_keys.json` remain discoverable for linked Guardian keys.

### Grammar-Constrained Decoding on cloud routes

Cloud routes strip GBNF `grammar` strings and llama-server's non-OpenAI
`json_schema` field (providers reject them). OpenAI-native `response_format`
is preserved as-is. Optional behavior is controlled by the `grammar` block in
[`config/settings.yaml`](../config/settings.yaml):

- `grammar.cloud_auto_convert_json: true` — convert a JSON-targeting
  grammar/schema to OpenAI `response_format` before forwarding.
- `grammar.cloud_strict_mode: true` — return HTTP 400 naming the provider
  instead of silently stripping.
- `grammar.enabled: false` — global kill-switch; strips `grammar` and
  `json_schema` on both local and cloud paths.

See [API_REFERENCE.md](API_REFERENCE.md) → Grammar-Constrained Decoding for
the full field semantics.

### Enabling / Disabling Providers

Set `enabled: false` to disable a provider without removing its config:

```yaml
providers:
  openrouter:
    enabled: false  # Models won't be served, won't appear in /v1/models
    ...
```

### Hot Reload

The provider registry loads its provider/model configuration from
`settings.yaml` at startup. To apply changes to a provider's `models` or
`model_prefixes`, restart Guardian — the registry is not re-read per request:

```bash
sudo systemctl restart llama-guardian
```

## Usage

### Discovering Models

```bash
# List all available models (local + cloud)
curl http://localhost:11434/v1/models \
  -H "Authorization: Bearer flip_..."
```

Cloud models appear with `"served_by": "cloud"` and `"owned_by": "<provider>"`:

```json
{
  "id": "openai/gpt-4o",
  "object": "model",
  "owned_by": "openrouter",
  "served_by": "cloud",
  "provider": "openrouter"
}
```

### Chat Completions

Use cloud models exactly like local models — Guardian handles the routing
transparently:

```bash
# Cloud model (Poolside Platform)
curl http://localhost:11434/v1/chat/completions \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "poolside/laguna-s-2.1",
    "messages": [{"role": "user", "content": "What are channels in Go?"}],
    "stream": true
  }'

# Cloud model (OpenRouter)
curl http://localhost:11434/v1/chat/completions \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'

# Local model (GPU-backed)
curl http://localhost:11434/v1/chat/completions \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-35b-uncensored",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

### Ollama-Compatible API

Cloud models also work through the Ollama-style bridge endpoints (`/api/chat`
and `/api/generate`). Guardian translates the Ollama format to OpenAI format
before forwarding to the cloud provider:

```bash
curl http://localhost:11434/api/chat \
  -H "Authorization: Bearer flip_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/llama-3.1-nemotron-70b-instruct",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Supported Providers

### OpenRouter

- **Base URL**: `https://openrouter.ai/api/v1`
- **Auth**: `Authorization: Bearer <key>`
- **Special headers**: Guardian automatically adds `HTTP-Referer` and `X-Title`
  headers for OpenRouter ranking/attribution.
- **Model names**: Use the OpenRouter model slug (e.g.,
  `anthropic/claude-3.5-sonnet`, `openai/gpt-4o`).
- **Get an API key**: https://openrouter.ai/keys

### NVIDIA NIM

- **Base URL**: `https://integrate.api.nvidia.com/v1`
- **Auth**: `Authorization: Bearer <key>`
- **Model names**: Use the NVIDIA model identifier (e.g.,
  `nvidia/llama-3.1-nemotron-70b-instruct`).
- **Get an API key**: https://build.nvidia.com/

### Poolside Platform

- **Base URL**: `https://inference.poolside.ai/v1`
- **Auth**: `Authorization: Bearer <key>`
- **Model names**: `poolside/laguna-xs-2.1` and `poolside/laguna-s-2.1`
- **Capabilities**: OpenAI Chat Completions, SSE streaming, tools, structured
  output, and text-only input/output. Guardian translates Anthropic
  `/v1/messages` requests through its existing OpenAI bridge.
- **Context**: Laguna XS 2.1 supports 256K tokens; Laguna S 2.1 supports 1M
  tokens. Poolside does not publish a fixed maximum output token value in the
  accessible API reference; use `max_completion_tokens` to bound generation.
- **Thinking**: max thinking is enabled by default. Direct Poolside requests can
  disable it with `chat_template_kwargs: {enable_thinking: false}`; intermediate
  effort levels are not available for Laguna S 2.1.
- **Rate limits**: Poolside's rate-limit page is access restricted and the public
  API reference does not state universal request/token quotas. Treat limits as
  account-specific. Guardian honors upstream `429` responses and retry hints via
  the configured `cloud_retry` policy; inspect `/api/cloud/ratelimit-stats` for
  observed limits and cooldowns.
- **Live discovery**: `GET https://inference.poolside.ai/v1/models` returned both
  model IDs above for the configured account on 2026-08-01.
- **Get an API key**: https://platform.poolside.ai/

### OpenAI (direct)

- **Base URL**: `https://api.openai.com/v1`
- **Auth**: `Authorization: Bearer <key>` (service-account keys `sk-svcacct-…`
  work for inference and are stored in `${OPENAI_API_KEY}` / `.env`).
- **Model names**: BARE names (e.g. `gpt-4o`, `gpt-4o-mini`, `gpt-5.2`,
  `o3`, `chat-latest`).  OpenAI has **no namespace**, so global recognition
  is via the explicit `models:` list in `settings.yaml` only — there is no
  `model_prefixes:` entry.  Send unlisted models (dated snapshots, etc.)
  via the per-key `guardian/openai/{model}` route, which requires no listing.
- **Naming caveat**: OpenRouter serves the **slug** `openai/{model}` (e.g.
  `openai/gpt-4o`) and is unaffected; this direct provider answers the
  **bare** name (`gpt-4o`).  `gpt-4o` → direct OpenAI; `openai/gpt-4o` →
  OpenRouter.  Both work, to different backends.
- **Added on**: 2026-08-01.  `https://api.openai.com/v1` is also registered in
  `_PROVIDER_BASE_URLS` (in `app/cloud_inference/routing.py` since the
  Phase 5 extraction; previously `app/proxy/server.py`) so per-key
  `guardian/openai/{model}` routes resolve a base URL.
- **Reasoning-model parameter adaptation**: OpenAI's reasoning models (the
  `o1`/`o3`/`o4` family and the entire `gpt-5*` generation) reject
  `max_tokens` (must use `max_completion_tokens`) and restrict
  `temperature` (o-series: unsupported; gpt-5*: only value `1`).  Many
  OpenAI-compatible clients (Claude Code, OpenWebUI, Aider, …) send these
  params unconditionally.  Guardian's `_adapt_openai_reasoning_params` (in
  `app/cloud_inference/routing.py`; called inside
  `_prepare_cloud_candidate_request`) silently adapts them
  **only for the direct `openai` provider** — OpenRouter handles its own
  param translation.  An explicit client-supplied `max_completion_tokens`
  always wins; the stray `max_tokens` is dropped, not overridden.
- **Failover groups**: `gpt-4o` and `gpt-4o-mini` are registered in
  `failover_groups` (`config/cloud_keys.json`) so `guardian/failover/gpt-4o`
  tries direct OpenAI first, then falls back to OpenRouter
  (`openai/gpt-4o`) on 429/5xx.

### Adding Custom Providers

The provider system is extensible. Any OpenAI-compatible API can be added:

```yaml
providers:
  my_custom_provider:
    enabled: true
    base_url: https://api.example.com/v1
    api_key: ${CUSTOM_API_KEY}
    timeout_seconds: 300
    models:
      - custom/model-1
      - custom/model-2
    extra_headers:
      X-Custom-Header: value
```

## Error Handling

| Scenario | HTTP Status | Error |
| --- | --- | --- |
| Model not in any provider or local config | `404` | `model_not_served` |
| Provider enabled but no API key | `503` | `provider_unavailable` |
| Cloud provider request fails | `502` | Backend request failed |
| Cloud provider returns error | Passthrough | Cloud status code + body |

## Architecture Notes

- **No queue for cloud models**: Cloud requests bypass the inference queue
  entirely. The cloud API handles its own rate limiting and concurrency. This
  means a long-running cloud request does not block local GPU requests.
- **Usage tracking**: Cloud request token usage is recorded in the same
  dashboard/usage system as local requests, so operators see a unified view.
- **Streaming**: Cloud SSE streams are proxied in real-time with the same
  `StreamProgressWatchdog` timeout protection as local streams.
- **No model switching**: Cloud models don't trigger VRAM scheduler or model
  switch logic — each cloud request is fully independent.
