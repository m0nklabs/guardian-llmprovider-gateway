# Guardian LLM Router — Cloud Provider Integration

Guardian acts as a **unified LLM router**: it serves local GPU-backed models via
`llama-server` **and** transparently forwards requests for cloud-hosted models
to upstream providers like **OpenRouter** and **NVIDIA NIM**.

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
| **Backend** | `llama-server` on `:11440` | OpenRouter / NVIDIA API |
| **Queue** | Inference queue (serialized, single-slot) | Bypassed — cloud handles concurrency |
| **VRAM** | VRAM scheduler, model switching, idle unload | Not applicable |
| **Model switching** | Auto-switch with allowlist | Not needed — each request is independent |
| **Rate limiting** | Guardian queue (max_concurrent) | Cloud provider's own limits |
| **Usage tracking** | Token usage from llama-server response | Token usage from cloud API response |
| **Streaming** | SSE passthrough with watchdog | SSE passthrough with watchdog |

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
- **☁️ Cloud Credentials** — add/delete cloud provider credentials (NVIDIA, OpenRouter)
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
```

### API Key Security

API keys support **environment variable expansion** using `${VAR_NAME}`
syntax. This keeps secrets out of the repository:

```bash
# Set environment variables before starting Guardian
export OPENROUTER_API_KEY="sk-or-v1-..."
export NVIDIA_API_KEY="nvapi-..."
```

If an environment variable is not set, the key expands to an empty string.
Requests for that provider's models will return `503 provider_unavailable`
until the key is configured.

### Enabling / Disabling Providers

Set `enabled: false` to disable a provider without removing its config:

```yaml
providers:
  openrouter:
    enabled: false  # Models won't be served, won't appear in /v1/models
    ...
```

### Hot Reload

The provider registry hot-reloads from `settings.yaml` on every model
resolution. Edit the file and the changes take effect immediately — no
Guardian restart needed.

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
