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

> **⚠️ Client timeout warning:** Your HTTP client timeout must be **longer than** `queue_timeout_seconds` (default: 300s) **plus** your expected inference time. If it's shorter, your client will time out *before* Guardian can respond with a meaningful HTTP 429 queue timeout — you'll just see a generic connection timeout instead.
>
> **Rule of thumb:** `client_timeout = queue_timeout_seconds + max_inference_seconds + 30s buffer`
>
> With defaults: `300 + 120 + 30 = 450s` minimum. We recommend **600s** to be safe.

---

## Client Implementation Patterns

### ⚠️ Timeout vs Queue Wait — Understanding the Problem

Guardian's queue is **server-side blocking**: your HTTP request stays open until a slot opens **and** inference completes. Your client timeout therefore covers **two phases**:

```
|--- queue wait ---|--- inference ---|  ← both happen inside one HTTP request
|------------ client timeout -----------|
```

If your timeout is too short, it may expire during queue wait — before inference even starts. Example:

> You set `timeout=120s`. Request waits 90s in the queue → only 30s left for inference → times out even though the answer was almost ready.

Without queue awareness, you can't tell **why** a request is slow: still queued (safe to wait) or inference stuck (should abort).

### Three Independent Axes

The patterns below are **composable building blocks**, not mutually exclusive choices. Your client sits on three independent axes:

| Axis | Options | Trade-off |
|------|---------|----------|
| **Response mode** | Blocking vs Streaming | Latency perception vs complexity |
| **Queue awareness** | None vs Status polling | Simplicity vs observability |
| **Timeout strategy** | Static vs Dynamic per-phase | Simplicity vs precision |

Pick one option per axis. Any combination works:

| Combination | Use case | Complexity |
|-------------|----------|------------|
| Blocking + no polling + static | Scripts, batch jobs, cron | Minimal |
| Blocking + polling + static | Production backend services | Moderate |
| Blocking + polling + dynamic | Latency-sensitive services, SLO enforcement | Higher |
| Streaming + no polling + static | Simple chat UIs | Moderate |
| Streaming + polling + static | Chat UIs with queue feedback | Moderate |
| Streaming + polling + dynamic | **Full-featured production chat** | Highest |

The sections below document each building block independently, then show how to combine them.

### Baseline: Blocking Request (Simplest)

Just send the request and wait. The queue is transparent — your request blocks server-side until a slot opens.

Sufficient for **background tasks** and **batch jobs** where latency doesn't matter. This is the starting point — the other building blocks layer on top of this.

**Timeout rule:** Set `timeout ≥ queue_timeout_seconds + max_inference_time`. Safe default: **600s**.

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

### Building Block: Queue Polling

Run a background task that polls `/v1/queue/status` alongside your inference request. Adds **queue awareness** to any request (blocking or streaming). Benefits:

1. **Progress feedback** — show "position 3 in queue" or "processing..." to users
2. **Observability** — log queue wait times, detect bottlenecks, feed dashboards
3. **Phase detection** — know whether you're waiting in queue or in inference (foundation for dynamic timeout)

The `/v1/queue/status` endpoint is **never queued itself** — it always responds instantly, even while inference is running.

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

### Building Block: Streaming

Add `"stream": true` to any request. Queue headers (`X-Queue-Wait-Ms`, `X-Request-Id`) arrive on the initial HTTP response before any SSE chunks.

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

### Building Block: Dynamic Timeout

Layered on top of **queue polling** — uses the phase information from `/v1/queue/status`
to apply separate deadlines per phase instead of one static timeout.

Dynamic timeout separates the request lifecycle into **two independent deadlines**:

```
|--- queue wait ---|--- inference ---|
     ↑ deadline 1       ↑ deadline 2    ← each phase has its own budget
```

- **Queue deadline**: Match or derive from the server's `queue_timeout_s` (default 300s). No point setting it shorter — the server will 429 you anyway.
- **Inference deadline**: Based on your expectations for the model and prompt size. A 50-token prompt on a 7B model finishes in seconds; a 4K-token prompt on a 30B model may need 2 minutes.

**Why this beats static:**

| Scenario | Static 600s | Dynamic (300s queue + 90s inference) |
|----------|-------------|--------------------------------------|
| Queue 5s → inference 10s | Waits up to 600s on failure | Detects stuck inference at 90s |
| Queue 280s → inference 10s | Works, but can't tell phases apart | Queue phase OK, inference starts fresh |
| Queue 5s → inference stuck | Waits full 600s before giving up | Aborts after 90s of inference |
| Queue 310s → timeout | Generic timeout, no info | Server 429 with clear "queue_timeout" error |

The trick: poll `/v1/queue/status` to know which phase you're in, and apply the
matching deadline.

#### Python (asyncio + httpx)

```python
import asyncio
import time
import httpx

GUARDIAN_URL = "http://guardian:11434"
API_KEY = "your_api_key_here"


class DynamicTimeoutError(Exception):
    """Raised when inference exceeds the dynamic deadline."""
    def __init__(self, phase: str, elapsed: float, deadline: float):
        self.phase = phase
        self.elapsed = elapsed
        self.deadline = deadline
        super().__init__(
            f"{phase} exceeded deadline: {elapsed:.1f}s > {deadline:.1f}s"
        )


async def chat_with_dynamic_timeout(
    messages: list,
    model: str,
    inference_deadline: float = 120.0,
):
    """
    Send an inference request with adaptive per-phase timeouts.

    - Queue deadline is read from the server's queue_timeout_s.
    - Inference deadline is caller-defined (default: 120s).
    - The HTTP-level timeout is a safety net covering both phases.
    """
    headers = {"Authorization": f"Bearer {API_KEY}"}

    # Step 1: Read the server's queue timeout to set our safety-net HTTP timeout
    async with httpx.AsyncClient(timeout=10.0) as probe:
        status = (await probe.get(
            f"{GUARDIAN_URL}/v1/queue/status", headers=headers
        )).json()
        server_queue_timeout = status.get("queue_timeout_s", 300)

    # Safety-net: covers both phases + buffer
    http_timeout = server_queue_timeout + inference_deadline + 60

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(http_timeout)
    ) as client:
        # Start the dynamic monitor in the background
        cancel_event = asyncio.Event()
        monitor = asyncio.create_task(
            _dynamic_monitor(
                client, headers, inference_deadline, cancel_event
            )
        )

        try:
            resp = await client.post(
                f"{GUARDIAN_URL}/v1/chat/completions",
                json={"model": model, "messages": messages, "stream": False},
                headers=headers,
            )

            if resp.status_code == 429:
                raise TimeoutError(f"Queue timeout: {resp.json()}")

            wait_ms = int(resp.headers.get("X-Queue-Wait-Ms", "0"))
            return {
                "response": resp.json(),
                "queue_wait_s": wait_ms / 1000,
            }
        except asyncio.CancelledError:
            # Monitor detected a deadline breach and cancelled us
            raise monitor.result()  # re-raise the DynamicTimeoutError
        finally:
            cancel_event.set()
            monitor.cancel()


async def _dynamic_monitor(
    client: httpx.AsyncClient,
    headers: dict,
    inference_deadline: float,
    cancel_event: asyncio.Event,
    poll_interval: float = 2.0,
):
    """Poll queue status and enforce the inference-phase deadline."""
    inference_started_at: float | None = None

    while not cancel_event.is_set():
        try:
            resp = await client.get(
                f"{GUARDIAN_URL}/v1/queue/status", headers=headers
            )
            info = resp.json()
            status = info.get("your_status", "idle")

            if status == "queued":
                # Queue phase — server handles its own timeout, just log
                pos = info.get("your_position", "?")
                wait = info.get("your_wait_s", 0)
                print(f"⏳ Queue position {pos}, waiting {wait:.0f}s")
                inference_started_at = None  # reset if re-queued

            elif status == "processing":
                if inference_started_at is None:
                    inference_started_at = time.monotonic()
                    print("🧠 Inference started")

                elapsed = time.monotonic() - inference_started_at
                print(f"🧠 Inference running: {elapsed:.0f}s / {inference_deadline:.0f}s")

                if elapsed > inference_deadline:
                    raise DynamicTimeoutError(
                        "inference", elapsed, inference_deadline
                    )

        except (httpx.RequestError, httpx.HTTPStatusError):
            pass  # Status endpoint unavailable — skip this tick

        await asyncio.sleep(poll_interval)
```

**Usage:**

```python
# Short prompt → tight inference deadline
result = await chat_with_dynamic_timeout(messages, "GLM-4.7-Flash", inference_deadline=30.0)

# Long generation → generous inference deadline
result = await chat_with_dynamic_timeout(messages, "Qwen3-30B", inference_deadline=180.0)
```

**When to add dynamic timeout to your client:**
- You need to **fail fast** on stuck inference without aborting queue waits
- You serve multiple models with **different speed profiles**
- You want **separate alerting** for queue congestion vs inference slowness
- You're building a production service where timeout granularity matters for SLOs

---

### Combining Building Blocks: Streaming + Dynamic Timeout

The most powerful combination for production chat UIs: real-time token output
with per-phase deadline enforcement. This merges the **streaming** and **dynamic
timeout** building blocks into a single client.

```python
import asyncio
import time
import httpx

GUARDIAN_URL = "http://guardian:11434"
API_KEY = "your_api_key_here"


async def streaming_chat_with_dynamic_timeout(
    messages: list,
    model: str,
    inference_deadline: float = 120.0,
    on_token: callable = None,
    on_status: callable = None,
):
    """
    Streaming inference with per-phase timeout enforcement.

    Combines:
    - Streaming (real-time tokens via SSE)
    - Queue polling (phase detection + progress feedback)
    - Dynamic timeout (inference-phase deadline)

    Args:
        on_token: Called with each content delta string.
        on_status: Called with (phase, detail) tuples for UI feedback.
    """
    headers = {"Authorization": f"Bearer {API_KEY}"}
    on_token = on_token or (lambda t: print(t, end="", flush=True))
    on_status = on_status or (lambda phase, detail: None)

    # Read server's queue timeout for safety-net HTTP timeout
    async with httpx.AsyncClient(timeout=10.0) as probe:
        status = (await probe.get(
            f"{GUARDIAN_URL}/v1/queue/status", headers=headers
        )).json()
        server_queue_timeout = status.get("queue_timeout_s", 300)

    http_timeout = server_queue_timeout + inference_deadline + 60

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(http_timeout)
    ) as client:
        cancel_event = asyncio.Event()
        monitor = asyncio.create_task(
            _phase_monitor(
                client, headers, inference_deadline, cancel_event, on_status
            )
        )

        try:
            async with client.stream(
                "POST",
                f"{GUARDIAN_URL}/v1/chat/completions",
                json={"model": model, "messages": messages, "stream": True},
                headers=headers,
            ) as resp:
                if resp.status_code == 429:
                    raise TimeoutError(f"Queue timeout: {await resp.aread()}")

                wait_ms = int(resp.headers.get("X-Queue-Wait-Ms", "0"))
                on_status("connected", f"queue wait: {wait_ms}ms")

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        chunk = line[6:]
                        if chunk == "[DONE]":
                            break
                        import json as _json
                        data = _json.loads(chunk)
                        delta = data["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            on_token(delta)

                return {"queue_wait_ms": wait_ms}
        finally:
            cancel_event.set()
            monitor.cancel()


async def _phase_monitor(
    client: httpx.AsyncClient,
    headers: dict,
    inference_deadline: float,
    cancel_event: asyncio.Event,
    on_status: callable,
    poll_interval: float = 2.0,
):
    """Shared phase monitor — works with both blocking and streaming."""
    inference_started_at: float | None = None

    while not cancel_event.is_set():
        try:
            resp = await client.get(
                f"{GUARDIAN_URL}/v1/queue/status", headers=headers
            )
            info = resp.json()
            phase = info.get("your_status", "idle")

            if phase == "queued":
                pos = info.get("your_position", "?")
                wait = info.get("your_wait_s", 0)
                on_status("queued", f"position {pos}, {wait:.0f}s")
                inference_started_at = None

            elif phase == "processing":
                if inference_started_at is None:
                    inference_started_at = time.monotonic()
                    on_status("inference_start", "model is generating")

                elapsed = time.monotonic() - inference_started_at
                if elapsed > inference_deadline:
                    from app.proxy.queue import DynamicTimeoutError  # or define locally
                    raise DynamicTimeoutError("inference", elapsed, inference_deadline)

        except (httpx.RequestError, httpx.HTTPStatusError):
            pass

        await asyncio.sleep(poll_interval)
```

**Usage with a chat UI:**

```python
async def handle_user_message(user_input: str):
    def show_token(t: str):
        ui.append_to_chat(t)  # Render token in real-time

    def show_status(phase: str, detail: str):
        if phase == "queued":
            ui.show_spinner(f"In queue: {detail}")
        elif phase == "inference_start":
            ui.hide_spinner()
        elif phase == "connected":
            ui.log(f"Connected ({detail})")

    await streaming_chat_with_dynamic_timeout(
        messages=[{"role": "user", "content": user_input}],
        model="GLM-4.7-Flash",
        inference_deadline=60.0,
        on_token=show_token,
        on_status=show_status,
    )
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
| `/api/scaler` | GET | Current scaler configuration |
| `/api/scaler` | PUT | Update scaler config (partial merge, persists) |
| `/api/scaler/reset` | POST | Reset scaler to defaults |
| `/api/scaler/recommend` | POST | Advisory: get recommended budgets for a prompt |

All require `Authorization: Bearer <token>`.

---

## Dynamic Scaler (Advisory)

Guardian includes a **DynamicScaler** that analyzes prompt complexity and queue pressure to recommend optimal `thinking_budget_tokens` and `max_tokens` values. The scaler is **advisory-only** — it never modifies your requests automatically. Your client decides whether and how to use the recommendations.

**Philosophy:** Full-power requests pass through Guardian completely unmodified. Only clients that explicitly opt in to scaling get adjusted values. This means a quick polling bot can request lean budgets while your interactive sessions keep full reasoning power.

### Recommend Endpoint

```
POST /api/scaler/recommend
Authorization: Bearer <api_key>
Content-Type: application/json
```

Send the same `messages` array you'd send to `/v1/chat/completions`. Guardian classifies the prompt and returns recommended values without touching the actual inference.

**Request:**

```json
{
  "messages": [
    {"role": "user", "content": "What is 2+2?"}
  ]
}
```

**Response:**

```json
{
  "profile": "trivial",
  "complexity": {
    "total_chars": 14,
    "num_messages": 1,
    "has_system": false,
    "has_images": false
  },
  "pressure": "none",
  "recommended": {
    "thinking_budget_tokens": 256,
    "max_tokens": 1024
  }
}
```

**Profiles:**

| Profile | Prompt size | Messages | Thinking budget | Max tokens | Use case |
|---------|-------------|----------|-----------------|------------|----------|
| `trivial` | ≤200 chars | ≤2 | 256 | 1024 | "hi", "what time is it" |
| `simple` | ≤800 chars | ≤4 | 1024 | 4096 | Single questions, short tasks |
| `moderate` | ≤4000 chars | ≤12 | 4096 | 8192 | Multi-turn conversations |
| `complex` | ≤15000 chars | ≤30 | 8192 | 16384 | Long context, code review |
| `deep` | >15000 chars | >30 | -1 (unlimited) | 32768 | Full documents, large codebases |

**Queue pressure adjustment:** When the queue has waiting requests, recommended budgets are reduced to improve throughput:

| Pressure | Condition | Thinking factor | Max tokens factor |
|----------|-----------|-----------------|-------------------|
| `none` | 0-1 waiting | 100% | 100% |
| `moderate` | 2-3 waiting | 50% | 75% |
| `heavy` | 4+ waiting | 25% | 50% |

Floors: `thinking_budget ≥ 128`, `max_tokens ≥ 512`. Unlimited thinking (`-1`) stays unlimited under any pressure.

### Client Usage Pattern

**Two-step: recommend → inject → infer**

```python
import httpx

GUARDIAN = "http://guardian:11434"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

async def smart_chat(messages: list, full_power: bool = False):
    """Chat with optional scaler recommendations."""
    body = {"model": "auto", "messages": messages, "stream": False}

    if not full_power:
        # Step 1: Ask Guardian what it recommends
        async with httpx.AsyncClient(timeout=5.0) as client:
            rec = (await client.post(
                f"{GUARDIAN}/api/scaler/recommend",
                json={"messages": messages},
                headers=HEADERS,
            )).json()

        # Step 2: Client decides — apply the recommendations
        body["thinking_budget_tokens"] = rec["recommended"]["thinking_budget_tokens"]
        body["max_tokens"] = rec["recommended"]["max_tokens"]

    # Step 3: Send the (possibly enriched) request
    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            f"{GUARDIAN}/v1/chat/completions",
            json=body,
            headers=HEADERS,
        )
        return resp.json()

# Polling bot — lean budgets for throughput
result = await smart_chat(messages, full_power=False)

# Interactive session — full reasoning power, no scaler
result = await smart_chat(messages, full_power=True)
```

**Selective application — use only what you want:**

```python
rec = (await client.post(f"{GUARDIAN}/api/scaler/recommend", json=body, headers=HEADERS)).json()

# Only cap max_tokens, keep unlimited thinking
body["max_tokens"] = rec["recommended"]["max_tokens"]
# Don't set thinking_budget_tokens → model uses its default (unlimited)
```

### Scaler Configuration Endpoints

View and tune scaler profiles at runtime — changes persist to `settings.yaml`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/scaler` | GET | Current scaler configuration |
| `/api/scaler` | PUT | Partial merge update (persists to disk) |
| `/api/scaler/reset` | POST | Reset to built-in defaults |
| `/api/scaler/recommend` | POST | Get recommendations for a request |

**Read current config:**

```bash
curl -H "Authorization: Bearer $KEY" http://guardian:11434/api/scaler
```

**Update a profile:**

```bash
curl -X PUT -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  http://guardian:11434/api/scaler \
  -d '{"profiles": {"trivial": {"thinking_budget": 512, "max_tokens": 2048}}}'
```

**Disable scaler entirely:**

```bash
curl -X PUT -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  http://guardian:11434/api/scaler \
  -d '{"enabled": false}'
```

**Adjust queue pressure thresholds:**

```bash
curl -X PUT -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  http://guardian:11434/api/scaler \
  -d '{"queue_pressure": {"heavy_threshold": 6, "moderate_thinking_factor": 0.6}}'
```

**Update without persisting (runtime-only, resets on restart):**

```bash
curl -X PUT -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  http://guardian:11434/api/scaler \
  -d '{"enabled": false, "_persist": false}'
```

**Reset to defaults:**

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
  http://guardian:11434/api/scaler/reset
```

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
