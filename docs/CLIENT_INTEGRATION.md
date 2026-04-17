# Guardian Client Integration Guide

This document describes how to integrate with Guardian middleware, including authentication, inference requests, the request queue, and status polling.

## Base URL

```
http://<guardian-host>:11434
```

Guardian exposes both OpenAI-compatible and Ollama-compatible endpoints on the same port.

## Authentication

All endpoints require a Bearer token:

```
Authorization: Bearer <api_key>
```

API keys follow the format `{prefix}_{32-char-hex}` (e.g., `flip_abc123def456...`).

Keys are managed in `config/api_keys.json`. Generate new keys with:

```bash
python3 scripts/generate_key.py --name "my-app"
```

---

## Inference Endpoints

### OpenAI-compatible

```
POST /v1/chat/completions
POST /v1/completions
POST /v1/embeddings
GET  /v1/models
```

Standard OpenAI request/response format. See [OpenAI API reference](https://platform.openai.com/docs/api-reference/chat).

**Example — Non-streaming:**

```bash
curl -s http://guardian:11434/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "GLM-4.7-Flash",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

**Example — Streaming (SSE):**

```bash
curl -N http://guardian:11434/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "GLM-4.7-Flash",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

### Ollama-compatible

```
POST /api/chat
POST /api/generate
GET  /api/tags
GET  /api/version
```

Standard Ollama request/response format. Existing Ollama clients work without modification.

---

## Request Queue

Guardian runs a single-slot `llama-server` backend. Only one inference request can be processed at a time. A FIFO queue serializes concurrent requests.

### How It Works

1. Client sends an inference request (e.g., `POST /v1/chat/completions`).
2. Guardian places the request in the queue.
3. If the slot is free, inference starts immediately.
4. If the slot is occupied, the request **blocks** until the current inference completes.
5. The response is delivered — identical to a standard OpenAI/Ollama response.

The queue is transparent to clients that don't care about it. Responses are **100% OpenAI/Ollama-compatible** — no queue metadata is injected into the response body or SSE stream.

### Queue Information Channels

Queue info is available through two separate, opt-in channels:

#### 1. Response Headers (automatic)

Every inference response includes two extra HTTP headers:

| Header | Type | Description |
|--------|------|-------------|
| `X-Request-Id` | UUID string | Unique identifier for this request in the queue |
| `X-Queue-Wait-Ms` | Integer | Milliseconds this request waited before processing started |

These headers are present on both streaming and non-streaming responses. Standard OpenAI client libraries ignore unknown headers, so this is fully backward compatible.

**Example response headers:**

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Request-Id: 28b9fd72-8aed-4426-9645-156a43ec9074
X-Queue-Wait-Ms: 0
```

A `X-Queue-Wait-Ms` value of `0` means the request was processed immediately (no queue wait).

#### 2. Status Polling Endpoint (opt-in)

```
GET /v1/queue/status
Authorization: Bearer <api_key>
```

This endpoint is **not queued** — it always responds immediately, even when inference is running. Use it to poll for queue position while your main request is blocking.

**Response:**

```json
{
  "queue_length": 2,
  "active_count": 1,
  "max_concurrent": 1,
  "queue_timeout_s": 300,
  "your_position": 2,
  "your_status": "queued",
  "your_wait_s": 12.5,
  "stats": {
    "total_queued": 47,
    "total_completed": 45,
    "total_timeouts": 0
  },
  "active_requests": [
    {
      "request_id": "28b9fd72",
      "client_id": "m0nk111",
      "model": "GLM-4.7-Flash",
      "elapsed_s": 8.3
    }
  ],
  "waiting": [
    {
      "position": 1,
      "request_id": "a1b2c3d4",
      "client_id": "oelala",
      "model": "GLM-4.7-Flash",
      "waiting_s": 3.1
    }
  ]
}
```

**`your_status` values:**

| Value | Meaning |
|-------|---------|
| `"idle"` | You have no active or queued requests |
| `"queued"` | Your request is waiting (check `your_position` and `your_wait_s`) |
| `"processing"` | Your request is being processed (check `your_elapsed_s`) |

**`your_position` values:**

| Value | Meaning |
|-------|---------|
| `-1` | No request from your client in the queue |
| `0` | Your request is currently being processed |
| `1+` | Your position in the waiting queue |

### Queue Timeout

If a request waits longer than the configured timeout (default: 300 seconds), Guardian returns:

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json

{
  "detail": {
    "error": "queue_timeout",
    "message": "Waited 300s in queue"
  }
}
```

Clients should handle HTTP 429 with a retry or user notification.

> **Client timeout warning:** Your HTTP client timeout must be **greater than** `queue_timeout_seconds` + expected inference time. Otherwise your client may time out *before* Guardian sends the 429, and you'll see a generic timeout error instead of a meaningful queue timeout response. Recommended minimum: `queue_timeout_seconds + 300` (e.g., `600s` for the default 300s queue timeout).

---

## Client Implementation Patterns

### ⚠️ Timeout vs Queue — The Key Problem

Guardian's queue is **server-side blocking**: your HTTP request blocks until a slot opens and inference completes. This means your **client-side timeout covers both queue wait time AND inference time**.

Example: You set `timeout=120s`. Your request waits 90s in the queue. That leaves only 30s for inference — which may not be enough for a large prompt. Your client times out, even though Guardian was about to respond.

**This is why queue-aware patterns exist.** Without them, you can't distinguish between "still waiting for a queue slot" (safe to keep waiting) and "inference is stuck" (should abort/retry).

**Solutions:**
- **Pattern A** (simple): Set a generous timeout (e.g., `600s`) that covers worst-case queue + inference. Accept that you can't tell *why* it's slow.
- **Pattern B** (recommended for production): Poll `/v1/queue/status` in a background task. This tells you exactly whether you're queued or processing, so you can set **separate** timeout policies:
  - Queue wait: up to `queue_timeout_seconds` (Guardian handles this with HTTP 429)
  - Inference: your own deadline based on expected generation time
- **Pattern C** (streaming): Same as A/B but for SSE streams — queue headers arrive on the initial response before chunks start.

### Pattern A: Fire-and-Forget (Simplest)

Just send the request and wait. The queue is transparent. Sufficient for **background tasks** where latency doesn't matter.

**Important:** Set your client timeout high enough to cover maximum queue wait (`queue_timeout_seconds`, default 300s) PLUS maximum inference time. A safe default is `600s`.

```python
import httpx

resp = httpx.post(
    "http://guardian:11434/v1/chat/completions",
    json={"model": "GLM-4.7-Flash", "messages": [{"role": "user", "content": "Hi"}]},
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=600.0,
)
result = resp.json()
```

No queue logic needed. The request blocks server-side until a slot is available.

### Pattern B: Queue-Aware (Background Status Polling)

Send the inference request in one task, poll `/v1/queue/status` in another. This is the **recommended pattern for production services** because it lets you:

1. **Show progress** to the user ("position 3 in queue", "processing...")
2. **Set smart timeouts** — e.g., abort only if *inference* exceeds your deadline, not because the queue was slow
3. **Log queue metrics** for monitoring (how often are your requests queued? for how long?)

The status endpoint is **never queued** — it always responds instantly, even during inference.

#### Python (asyncio + httpx)

```python
import asyncio
import httpx

GUARDIAN_URL = "http://guardian:11434"
API_KEY = "your_api_key_here"

async def chat_with_status(messages: list, model: str):
    headers = {"Authorization": f"Bearer {API_KEY}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        # Start status polling in background
        poll_task = asyncio.create_task(_poll_status(client, headers))

        try:
            resp = await client.post(
                f"{GUARDIAN_URL}/v1/chat/completions",
                json={"model": model, "messages": messages, "stream": False},
                headers=headers,
            )

            if resp.status_code == 429:
                raise TimeoutError(f"Queue timeout: {resp.json()}")

            # Check how long we waited
            wait_ms = int(resp.headers.get("X-Queue-Wait-Ms", "0"))
            if wait_ms > 1000:
                print(f"Waited {wait_ms / 1000:.1f}s in queue")

            return resp.json()
        finally:
            poll_task.cancel()


async def _poll_status(client: httpx.AsyncClient, headers: dict, interval: float = 2.0):
    """Poll queue status while the main request is blocking."""
    try:
        while True:
            resp = await client.get(f"{GUARDIAN_URL}/v1/queue/status", headers=headers)
            info = resp.json()
            status = info.get("your_status", "idle")
            if status == "queued":
                print(f"Queue position: {info['your_position']}, waiting: {info['your_wait_s']}s")
            elif status == "processing":
                print(f"Processing... elapsed: {info.get('your_elapsed_s', '?')}s")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

#### TypeScript / JavaScript (fetch)

```typescript
const GUARDIAN_URL = "http://guardian:11434";

async function chatWithStatus(
  messages: { role: string; content: string }[],
  model: string,
  apiKey: string,
): Promise<any> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
  };

  // Start polling in background
  const pollController = new AbortController();
  pollQueueStatus(headers, pollController.signal);

  try {
    const resp = await fetch(`${GUARDIAN_URL}/v1/chat/completions`, {
      method: "POST",
      headers,
      body: JSON.stringify({ model, messages, stream: false }),
    });

    if (resp.status === 429) {
      throw new Error(`Queue timeout: ${await resp.text()}`);
    }

    const waitMs = parseInt(resp.headers.get("X-Queue-Wait-Ms") || "0");
    if (waitMs > 1000) {
      console.log(`Waited ${(waitMs / 1000).toFixed(1)}s in queue`);
    }

    return await resp.json();
  } finally {
    pollController.abort();
  }
}

async function pollQueueStatus(
  headers: Record<string, string>,
  signal: AbortSignal,
  intervalMs = 2000,
) {
  while (!signal.aborted) {
    try {
      const resp = await fetch(`${GUARDIAN_URL}/v1/queue/status`, {
        headers,
        signal,
      });
      const info = await resp.json();
      if (info.your_status === "queued") {
        console.log(
          `Queue position: ${info.your_position}, waiting: ${info.your_wait_s}s`,
        );
      }
    } catch {
      break;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
```

### Pattern C: Streaming with Queue Headers

For streaming responses, the queue headers are on the initial HTTP response (before any SSE chunks). Read them before consuming the stream.

#### Python (httpx streaming)

```python
async def chat_streaming(messages: list, model: str):
    headers = {"Authorization": f"Bearer {API_KEY}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        async with client.stream(
            "POST",
            f"{GUARDIAN_URL}/v1/chat/completions",
            json={"model": model, "messages": messages, "stream": True},
            headers=headers,
        ) as resp:
            if resp.status_code == 429:
                raise TimeoutError("Queue timeout")

            # Queue headers are available immediately
            wait_ms = int(resp.headers.get("X-Queue-Wait-Ms", "0"))
            request_id = resp.headers.get("X-Request-Id", "")

            # Consume SSE chunks
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    # Process chunk...
                    print(chunk)
```

---

## Model Switching

Guardian automatically switches models when a request specifies a different model than the currently loaded one. This happens **inside** the queue slot, so:

- No concurrent inference can interfere with the switch.
- Other requests wait in the queue until the switch + inference completes.
- Model switching adds latency (10–60s depending on model size).

### Pinned Model

If `guardian.pinned_model` is set in `models.yaml`, only that model can be loaded. Requests for other models will use the pinned model instead.

### Switch Allowlist

Only API keys whose `client_id` is in `guardian.switch_allowlist` can trigger model switches. Others will use whatever model is currently loaded.

---

## Other Useful Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Current model, health, VRAM usage, uptime |
| `/api/tags` | GET | List available models (Ollama format) |
| `/v1/models` | GET | List available models (OpenAI format) |
| `/admin/load` | POST | Force-load a specific model |
| `/admin/unload` | POST | Unload current model (free VRAM) |

All require `Authorization: Bearer <token>`.

---

## Error Handling Summary

| HTTP Status | Meaning | Client Action |
|-------------|---------|---------------|
| 200 | Success | Process response normally |
| 401 | Invalid/missing API key | Check your Bearer token |
| 429 | Queue timeout | Retry after delay, or notify user |
| 500 | Backend error | Log and retry; check `/api/status` for health |
| 503 | Model not loaded / backend down | Wait and retry; Guardian auto-reloads on next request |

---

## Configuration Reference

Queue behavior is configured in `config/settings.yaml`:

```yaml
queue:
  max_concurrent: 1              # Simultaneous inference slots (match llama-server --parallel)
  queue_timeout_seconds: 300     # Max wait time before HTTP 429
```
