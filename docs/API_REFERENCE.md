# API Reference

## Ports and Auth Boundaries

| Port | Surface | Auth in current code |
| --- | --- | --- |
| `11434` | Guardian proxy and control plane | Required for everything except `/healthz` and `/metrics` |
| `11437` | Dashboard UI and stats API | No auth |
| `11440` | Raw `llama-server` backend | Private Guardian-managed backend |

## Authentication

Guardian accepts any of these request styles on `:11434`:

- `Authorization: Bearer <token>`
- `x-api-key: <token>`
- `api-key: <token>`

Keys live in [../config/api_keys.json](../config/api_keys.json).

Missing or invalid keys return `401 Unauthorized`.

## Common Queue Behavior

Queued inference responses include:

- `X-Request-Id`: Guardian UUID for the queued request
- `X-Queue-Wait-Ms`: time spent waiting before inference started

Guardian keeps a live queued request waiting until one of these things happens:

- the request reaches the front of the queue and runs
- the client disconnects and Guardian cancels it
- the client explicitly cancels it through `DELETE /v1/queue/requests/{request_id}`

The configured `queue.queue_timeout_seconds` is surfaced as queue-budget
telemetry in status responses, but Guardian no longer drops a healthy waiter
just because that budget has elapsed.

There is no token-bucket or per-client rate limiter in current code.
Backpressure is enforced by the FIFO inference queue and model-size-dependent
backend timeouts.

## Error Model

Common statuses:

- `200`: success
- `400`: bad admin load override or malformed request contract
- `401`: missing or invalid API key
- `404`: model metadata lookup failed
- `422`: vision runtime unavailable or backend image path rejected
- `499`: Guardian cancelled the request because the downstream client
  disconnected or the client explicitly cancelled its queue entry
- `500`: unexpected switch or proxy failure
- `503`: model load, auto-reload, or backend recovery failure
- `410`: legacy benchmark start/stop endpoints are disabled

## Proxy Surface (`:11434`)

### Public endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | No | Liveness probe for the Guardian process only |
| `GET` | `/metrics` | No | Prometheus scrape target |

Notes:

- `/healthz` does not validate backend health. Use `/api/status` for that.
- `/metrics` is intentionally unauthenticated in current code.

### Model metadata and compatibility endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/models` | No | List configured canonical models and aliases |
| `GET` | `/v1/models/{model_id}` | No | Return metadata for one model or alias |
| `GET` | `/api/tags` | No | Ollama-compatible model list |
| `GET` | `/api/version` | No | Ollama-compatible version endpoint |

#### `GET /v1/models`

Returns OpenAI-style model entries enriched with Guardian-specific metadata.

Representative item:

```json
{
  "id": "qwen3.6-35b-uncensored",
  "object": "model",
  "created": 1716650000,
  "owned_by": "organization-owner",
  "permission": [],
  "max_context": 262144,
  "benchmark_context_limit": 262144,
  "context": 262144,
  "advertised_context": 258048,
  "input_modalities": ["text", "image"],
  "configured_input_modalities": ["text", "image"],
  "vision": {
    "configured": true,
    "status": "supported",
    "validated": true
  }
}
```

Notes:

- `max_context` is normally the benchmark ceiling.
- for `client_id == claudecode`, Guardian may return the safer
  `advertised_context` value as `max_context` so Claude compacts earlier.
- `vision.status` comes from Guardian's runtime validation state, not just the
  presence of an `mmproj` path.

#### `GET /api/tags`

Returns a synthetic Ollama-compatible `{"models": [...]}` list based on the
configured model registry.

Compatibility notes:

- `size` is derived from Guardian's `get_model_size()` heuristic
- `details.family`, `details.parameter_size`, and related fields are generic
  compatibility placeholders, not authoritative GGUF introspection

#### `GET /api/version`

Always returns:

```json
{"version": "0.1.27"}
```

### Inference endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/chat/completions` | Yes | OpenAI-compatible chat completions |
| `POST` | `/v1/completions` | Yes | OpenAI-compatible text completions |
| `POST` | `/v1/embeddings` | Yes | OpenAI-compatible embeddings |
| `POST` | `/v1/messages` | Yes | Anthropic-style message passthrough when supported by backend |
| `POST` | `/api/chat` | Yes | Ollama-compatible chat bridge |
| `POST` | `/api/generate` | Yes | Ollama-compatible prompt bridge |

#### `POST /v1/chat/completions`

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "messages": [
    {"role": "user", "content": "Reply with exactly: FIT OK"}
  ],
  "max_tokens": 16,
  "stream": false
}
```

Representative multimodal request:

```json
{
  "model": "qwen3.6-35b-heretic-mtp",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        {"type": "text", "text": "Describe the image in one sentence."}
      ]
    }
  ],
  "max_tokens": 64
}
```

Runtime behavior:

- if the requested model differs from the active model and the client is
  allowed to switch, Guardian reloads inside the queue slot before proxying
- if the same model needs a text-to-vision or vision-to-text runtime flip,
  Guardian reloads that same model with the new runtime mode
- streaming holds the queue slot until the SSE stream closes

#### `POST /api/chat`

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "messages": [
    {"role": "user", "content": "Say hello"}
  ],
  "stream": false,
  "options": {
    "temperature": 0.7
  }
}
```

Representative non-streaming response:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "created_at": "2026-05-25T12:00:00.000Z",
  "message": {
    "role": "assistant",
    "content": "hello"
  },
  "done": true,
  "total_duration": 0,
  "load_duration": 0,
  "prompt_eval_count": 8,
  "eval_count": 2
}
```

#### `POST /api/generate`

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "prompt": "Reply with exactly: FIT OK",
  "stream": false
}
```

Representative response:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "created_at": "2026-05-25T12:00:00.000Z",
  "response": "FIT OK",
  "done": true,
  "context": [],
  "total_duration": 0,
  "load_duration": 0,
  "prompt_eval_count": 8,
  "eval_count": 2
}
```

### Generic OpenAI passthrough

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/{path:path}` | No | Direct passthrough to backend `GET /v1/...` |
| `POST` | `/v1/{path:path}` | Sometimes | Direct passthrough to backend `POST /v1/...` |

Queue rule for generic `POST /v1/{path:path}`:

- queued only when `path` is `chat/completions`, `completions`, `embeddings`,
  or `messages`
- direct passthrough for all other `POST /v1/*` paths

### Queue and status endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/queue/status` | No | Return queue depth, active requests, and per-client status |
| `GET` | `/v1/queue/requests/{request_id}` | No | Return lifecycle state for one tracked queued request |
| `DELETE` | `/v1/queue/requests/{request_id}` | No | Cancel a queued request or request cancellation of a running one |
| `GET` | `/api/status` | No | Return backend, queue, startup, switch, proxy, and security state |
| `GET` | `/api/crashes` | No | Return crash history and the last crash |

#### `GET /v1/queue/status`

Representative response:

```json
{
  "queue_length": 1,
  "active_count": 1,
  "max_concurrent": 1,
  "queue_timeout_s": 300,
  "queue_timeout_enforced": false,
  "wait_policy": "disconnect_or_cancel",
  "stats": {
    "total_queued": 12,
    "total_completed": 11,
    "total_timeouts": 0,
    "total_cancelled": 1,
    "total_failed": 0,
    "total_expired": 0
  },
  "your_position": 1,
  "your_status": "queued",
  "your_wait_s": 3.2,
  "your_request_id": "28b9fd72-8aed-4426-9645-156a43ec9074"
}
```

#### `GET /v1/queue/requests/{request_id}`

Representative response:

```json
{
  "request_id": "28b9fd72-8aed-4426-9645-156a43ec9074",
  "client_id": "telegram-grow-bot",
  "model": "qwen3.6-35b-uncensored",
  "status": "queued",
  "position": 1,
  "enqueued_at": 1748262000.0,
  "waiting_s": 14.2
}
```

#### `DELETE /v1/queue/requests/{request_id}`

Representative response for a running request:

```json
{
  "request_id": "28b9fd72-8aed-4426-9645-156a43ec9074",
  "client_id": "telegram-grow-bot",
  "model": "qwen3.6-35b-uncensored",
  "status": "cancelling",
  "position": 0,
  "cancel_reason": "client_requested_cancel"
}
```

#### `GET /api/status`

Representative top-level fields:

```json
{
  "current_model": "Qwen3.6-35B-A3B-HauhauCS-Aggressive",
  "backend_healthy": true,
  "is_unloaded": false,
  "idle_seconds": 17,
  "idle_unload_minutes": 5,
  "backend_url": "http://127.0.0.1:11440",
  "queue": {},
  "startup": {},
  "switch": {},
  "security": {},
  "proxy": {},
  "routing": {},
  "scaler": {}
}
```

Important subtrees:

- `queue`: live queue snapshot
- `startup`: generation-tracked background startup verification state
- `switch`: active switch owner, requested target, and lock state
- `security`: pinned model, allowlist, backend verification timestamps
- `proxy`: PID, listener ownership, PID file state
- `routing`: preferred tool model and reasoning model

#### `GET /api/crashes`

Representative response:

```json
{
  "total_crashes": 3,
  "last_crash": {
    "timestamp": "2026-05-25T12:00:00",
    "model": "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved",
    "error_message": "CUDA error ...",
    "exit_code": 1,
    "config_snapshot": {}
  },
  "history": []
}
```

### Model management endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/admin/load` | No | Reload current or specified model |
| `POST` | `/admin/unload` | No | Stop backend immediately and free VRAM |

#### `POST /admin/load`

Request body:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "enable_vision": false,
  "runtime_overrides": {
    "context": 262144,
    "ngl": 40,
    "tensor_split": "0.36,0.64"
  }
}
```

Rules:

- `model` may be a canonical model name or an alias
- `enable_vision` is optional
- `runtime_overrides` must be an object and may contain only:
  - `context`
  - `ngl`
  - `tensor_split`
- `tensor_split` must be exactly two finite non-negative comma-separated values
- invalid override contracts return `400`
- load failures return `503`

Representative success response:

```json
{
  "status": "loaded",
  "model": "Qwen3.6-35B-A3B-HauhauCS-Aggressive"
}
```

#### `POST /admin/unload`

Representative response when backend was active:

```json
{
  "status": "unloaded",
  "message": "Model 'Qwen3.6-35B-A3B-HauhauCS-Aggressive' unloaded - VRAM is free"
}
```

Representative response when already unloaded:

```json
{
  "status": "already_unloaded",
  "message": "llama-server is already stopped"
}
```

### Scaler endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/scaler` | No | Return current scaler config |
| `PUT` | `/api/scaler` | No | Partially update scaler config |
| `POST` | `/api/scaler/reset` | No | Reset scaler config to defaults |
| `POST` | `/api/scaler/recommend` | No | Return advisory `thinking_budget_tokens` and `max_tokens` |

Important note: in current code the scaler is an advisory API and config store.
It is not automatically rewriting every inference request body in the hot path.

#### `PUT /api/scaler`

Representative request:

```json
{
  "enabled": true,
  "queue_pressure": {
    "heavy_threshold": 6
  }
}
```

Representative response:

```json
{
  "status": "updated",
  "config": {}
}
```

#### `POST /api/scaler/recommend`

Representative request:

```json
{
  "messages": [
    {"role": "user", "content": "Explain the runtime tradeoffs."}
  ]
}
```

Representative response:

```json
{
  "profile": "moderate",
  "complexity": {
    "total_chars": 36,
    "num_messages": 1,
    "has_system": false,
    "has_images": false
  },
  "pressure": "none",
  "recommended": {
    "thinking_budget_tokens": 4096,
    "max_tokens": 8192
  }
}
```

### Session endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/session/save` | No | Save llama.cpp slot 0 state to disk |
| `POST` | `/api/session/load` | No | Restore llama.cpp slot 0 state from disk |
| `GET` | `/api/session/list` | No | List saved slot files |

#### `POST /api/session/save`

Request body:

```json
{
  "filename": "my-session"
}
```

Guardian forwards to llama.cpp slot API:

- `POST /slots/0?action=save`

#### `POST /api/session/load`

Request body:

```json
{
  "filename": "my-session"
}
```

Guardian forwards to llama.cpp slot API:

- `POST /slots/0?action=restore`

#### `GET /api/session/list`

Representative response:

```json
{
  "sessions": ["my-session", "other-session"]
}
```

Current implementation scans `~/llama_slots` for `*.bin` files.

## Dashboard and Monitoring Surface (`:11437`)

These endpoints are served by the separate UI app in `app/main.py`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/` | No | Serve static dashboard UI |
| `GET` | `/api/stats` | No | Aggregate VRAM, cached models, and API usage snapshot |
| `GET` | `/api/benchmark` | No | Read-only benchmark summary |
| `POST` | `/api/benchmark/start` | No | Returns `410 Gone` |
| `POST` | `/api/benchmark/stop` | No | Returns `410 Gone` |

### `GET /api/stats`

Representative top-level fields:

```json
{
  "vram": {
    "used": 12000,
    "free": 14000,
    "total": 26000
  },
  "active_models": [],
  "queue_size": 0,
  "optimized_count": 0,
  "cached_models": [],
  "records": [],
  "api_usage": {}
}
```

Important note: `api_usage` is backed by
[../data/api_usage_state.json](../data/api_usage_state.json), so request
totals and top-client summaries survive Guardian restarts.

### `GET /api/benchmark`

Representative fields:

- `completed_count`
- `queue_count`
- `last_completed`
- `best_by_model`

This endpoint is historical and read-only. The old benchmark runner is no
longer a live control path.

## Security and Backpressure Summary

- Auth is enforced on `:11434` except for `/healthz` and `/metrics`.
- The dashboard/UI port `:11437` is unauthenticated in current code.
- Model changes are governed by `guardian.pinned_model` and
  `guardian.switch_allowlist`.
- Backpressure comes from the FIFO queue and model-size timeout tiers, not from
  per-client rate limiting.