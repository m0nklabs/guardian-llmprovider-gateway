# Guardian Anthropic Messages API Bridge

Guardian provides full **Anthropic Messages API** (`/v1/messages`) compatibility
for both **local GPU models** and **cloud LLM providers**. This allows clients
that speak the Anthropic protocol — Claude Code, the `anthropic` Python SDK,
and any Anthropic-compatible tool — to use Guardian as a drop-in replacement
for `api.anthropic.com`.

## Architecture Overview

```
Client (Claude Code / anthropic SDK)
    │
    │  POST /v1/messages  (Anthropic format)
    │
    ▼
┌──────────────────────────────────────────────┐
│              Guardian Proxy                   │
│                                               │
│  ┌─────────────────────────────────────────┐ │
│  │       Route detection                   │ │
│  │  model name → local or cloud?           │ │
│  └──────────────┬──────────────┬───────────┘ │
│                 │              │              │
│     ┌───────────▼──┐    ┌─────▼──────────┐   │
│     │ LOCAL MODEL  │    │  CLOUD MODEL   │   │
│     │              │    │                 │   │
│     │ llama-server │    │ anthropic_      │   │
│     │ /v1/messages │    │ bridge.py       │   │
│     │ (native      │    │ Anthropic→OpenAI│   │
│     │  Anthropic)  │    │ translation    │   │
│     │              │    │                 │   │
│     │ + enrichment │    │ (only if provider│   │
│     │   layer      │    │  doesn't speak   │   │
│     │   (see below)│    │  Anthropic natively)│
│     └──────┬───────┘    └──────┬──────────┘   │
│            │                   │              │
└────────────┼───────────────────┼──────────────┘
             │                   │
             ▼                   ▼
      llama-server         NVIDIA NIM API
      (local GPU)          OpenRouter API
                           (native Anthropic)
```

## Two Translation Paths

### Path 1: Local Models (llama-server enrichment)

llama-server has **native Anthropic Messages API support** via its `/v1/messages`
endpoint (registered in `server.cpp`). It internally converts:

1. **Request**: `server_chat_convert_anthropic_to_oai()` converts Anthropic → OpenAI
2. **Response**: `to_json_anthropic()` converts OpenAI → Anthropic
3. **Streaming**: `to_json_anthropic_stream()` generates Anthropic SSE events

However, llama-server's implementation has several gaps that Guardian's
**enrichment layer** fills. The enrichment is applied transparently in
`app/proxy/server.py` without modifying the upstream response body when
no enrichment is needed.

### Path 2: Cloud Providers (full translation bridge)

For cloud providers that **don't** natively support `/v1/messages` (e.g. NVIDIA
NIM), Guardian uses `app/proxy/anthropic_bridge.py` to perform full bidirectional
translation:

1. **Request**: `translate_anthropic_request_to_openai()` — Anthropic → OpenAI
2. **Response**: `translate_openai_response_to_anthropic()` — OpenAI → Anthropic
3. **Streaming**: `translate_openai_stream_to_anthropic()` — OpenAI SSE → Anthropic SSE
4. **Errors**: `translate_openai_error_to_anthropic()` — OpenAI errors → Anthropic errors

**OpenRouter** natively supports `/v1/messages`, so the translation bridge is
**skipped** for OpenRouter. The `provider_needs_anthropic_translation()` function
determines whether translation is needed based on provider name and request path.

## Local Model Enrichment Layer

The enrichment layer in `app/proxy/server.py` fixes the following gaps in
llama-server's Anthropic implementation:

### 1. Thinking Configuration Translation

llama-server's `/v1/messages` endpoint ignores `thinking: {type: "disabled"}`.
Claude Code sends this parameter to disable extended thinking for simple requests.

**Fix**: `_apply_anthropic_thinking_to_llama_params()` converts Anthropic thinking
config to llama-server's native parameters:

| Anthropic `thinking` | llama-server params |
|---|---|
| `{type: "disabled"}` | `reasoning_budget=0`, `chat_template_kwargs.enable_thinking=false` |
| `{type: "enabled", budget_tokens: N}` | `reasoning_budget=N` |
| `{type: "adaptive"}` | Left as-is (default behavior) |

Also updated `_request_explicitly_disables_thinking()` to detect the Anthropic
`thinking: {type: "disabled"}` parameter.

### 2. Usage Field Enrichment

llama-server's Anthropic responses are missing several usage fields that Claude
Code expects:

**Non-streaming** (`_enrich_anthropic_response()`):
- Adds `cache_creation_input_tokens: 0` (always missing)
- Adds `cache_read_input_tokens: 0` if missing
- Ensures `input_tokens` and `output_tokens` are present

**Streaming** (`_enrich_anthropic_sse_line()`):
- `message_start`: adds `cache_creation_input_tokens: 0`
- `message_delta`: injects `input_tokens` (tracked from `message_start`), adds
  `cache_creation_input_tokens: 0` and `cache_read_input_tokens`

### 3. Stop Reason Correction

llama-server returns `stop_reason: "end_turn"` even when a `stop_sequence` was
matched (the `stop_sequence` field is set but `stop_reason` is wrong).

**Fix**: Both `_enrich_anthropic_response()` and `_enrich_anthropic_sse_line()`
correct `"end_turn"` → `"stop_sequence"` when `stop_sequence` is non-null.

### 4. Ping Events (Keepalive)

llama-server doesn't emit Anthropic `ping` SSE events. Claude Code has a 5-minute
idle timeout (`API_FORCE_IDLE_TIMEOUT`) that aborts streaming connections when no
data arrives.

**Fix**: Guardian's existing keepalive mechanism (SSE comments) is extended for
`/v1/messages` paths — keepalive comments are converted to proper Anthropic
`event: ping` with `{"type": "ping"}` data.

### 5. Prefill Workaround Fix

Guardian has a prefill workaround for llama.cpp's "Assistant response prefill is
incompatible with enable_thinking" limitation. This workaround previously used
`str(content)` which produced garbage when content was a list of Anthropic
content blocks (e.g. `[{"type": "text", "text": "..."}]`).

**Fix**: Uses `_stringify_message_content()` which properly extracts text from
Anthropic content block arrays.

## Cloud Bridge: `anthropic_bridge.py`

The `app/proxy/anthropic_bridge.py` module provides full bidirectional translation
between Anthropic Messages API and OpenAI Chat Completions API.

### Request Translation

`translate_anthropic_request_to_openai(anthropic_body)` converts:

| Anthropic | OpenAI |
|---|---|
| Top-level `system` (string or array) | `{"role": "system"}` message |
| `messages` with content blocks | `messages` with string/array content |
| `max_tokens` (required) | `max_tokens` |
| `tools` with `input_schema` | `tools` with `function.parameters` |
| `tool_choice` (auto/any/none/tool) | `tool_choice` (auto/required/none/function) |
| `disable_parallel_tool_use` | `parallel_tool_calls: false` |
| `stop_sequences` | `stop` |
| `temperature`, `top_p`, `top_k` | Passed through |
| `stream` | `stream` + `stream_options: {include_usage: true}` |

**Content block conversion** (`_convert_content_blocks_to_openai()`):

| Anthropic block | OpenAI equivalent |
|---|---|
| `{type: "text", text: "..."}` | Text content |
| `{type: "image", source: {type: "base64", ...}}` | `{type: "image_url", image_url: {url: "data:..."}}` |
| `{type: "image", source: {type: "url", url: "..."}}` | `{type: "image_url", image_url: {url: "..."}}` |
| `{type: "document", source: {type: "base64", ...}}` | `{type: "image_url", image_url: {url: "data:..."}}` |
| `{type: "thinking", thinking: "..."}` | Converted to text (for context) |
| `{type: "redacted_thinking"}` | Skipped |
| `{type: "tool_use", id, name, input}` | `tool_calls` on assistant message |
| `{type: "tool_result", tool_use_id, content, is_error}` | `{"role": "tool"}` message with `is_error` |

### Response Translation

`translate_openai_response_to_anthropic(openai_response, model_name)` converts:

| OpenAI | Anthropic |
|---|---|
| `choices[0].message.content` | `{type: "text", text: "..."}` block |
| `choices[0].message.reasoning_content` | `{type: "thinking", thinking: "...", signature: ""}` block |
| `choices[0].message.tool_calls` | `{type: "tool_use", id, name, input}` blocks |
| `finish_reason: "stop"` | `stop_reason: "end_turn"` |
| `finish_reason: "length"` | `stop_reason: "max_tokens"` |
| `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` |
| `finish_reason: "content_filter"` | `stop_reason: "refusal"` |
| `usage.prompt_tokens` | `usage.input_tokens` |
| `usage.completion_tokens` | `usage.output_tokens` |
| (always added) | `usage.cache_creation_input_tokens: 0` |
| (always added) | `usage.cache_read_input_tokens: 0` |

Also performs stop_sequence detection: if the response text ends with one of the
requested `stop_sequences`, sets `stop_reason: "stop_sequence"` and
`stop_sequence: "<matched sequence>"`.

### Streaming Translation

`translate_openai_stream_to_anthropic(openai_sse_lines, model_name)` converts
OpenAI SSE chunks to Anthropic SSE events:

**Event flow**:
```
message_start
  → content_block_start (thinking, if reasoning_content)
  → thinking_delta (×N)
  → signature_delta
  → content_block_stop
  → content_block_start (text)
  → text_delta (×N)
  → content_block_stop
  → content_block_start (tool_use, if tool_calls)
  → input_json_delta (×N)
  → content_block_stop
message_delta (with stop_reason + usage)
message_stop
```

**Key features**:
- **Dynamic block indexing**: thinking → text → tool_use each get unique indices
- **Interleaved blocks**: text after tool_use gets a new block index
- **`signature_delta`**: emitted before `content_block_stop` for thinking blocks
- **`input_tokens` in `message_delta`**: cumulative from `message_start`
- **`cache_creation_input_tokens`** and **`cache_read_input_tokens`**: always present
- **Ping events**: emitted every 15s when upstream is idle (prevents Claude Code timeout)
- **Stop sequence detection**: accumulates text and checks against requested stop sequences

### Error Translation

`translate_openai_error_to_anthropic(status_code, error_body)` converts OpenAI
error responses to Anthropic format:

| HTTP Status | Anthropic Error Type |
|---|---|
| 400 | `invalid_request_error` |
| 401 | `authentication_error` |
| 403 | `permission_denied_error` |
| 404 | `not_found_error` |
| 413 | `request_too_large` |
| 429 | `rate_limit_error` |
| 500+ | `api_error` |
| 503 | `overloaded_error` |

Response format:
```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "..."
  }
}
```

### Provider Detection

`provider_needs_anthropic_translation(provider_name, path)` returns `True` when:
- The path is `messages` (Anthropic endpoint)
- The provider is NOT OpenRouter (which natively supports `/v1/messages`)

## Configuration

### Cloud Providers (`config/settings.yaml`)

```yaml
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}
    timeout_seconds: 600
  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: ${NVIDIA_API_KEY}
    models:
      - minimaxai/minimax-m3
      - nvidia/llama-3.1-nemotron-70b-instruct
```

### Per-Key Credentials (`config/cloud_keys.json`)

Cloud credentials can be linked to specific Guardian API keys, allowing
per-key routing via the `guardian/{provider}/{model}` convention.

## Claude Code Integration

### Connecting Claude Code to Guardian

```bash
# For cloud models (NVIDIA NIM):
claude --model guardian/nvidia/minimaxai/minimax-m3

# For local models:
claude --model qwen3.6-35b-uncensored
```

Claude Code automatically detects the Anthropic Messages API endpoint at
`/v1/messages` and sends requests in Anthropic format. Guardian handles the
rest.

### What Claude Code Sends

Claude Code's typical request includes:
- `system`: Top-level system prompt (string or content block array)
- `messages`: Conversation with text, tool_use, and tool_result blocks
- `tools`: Tool definitions with `input_schema`
- `tool_choice`: Usually `{"type": "auto", "disable_parallel_tool_use": true}`
- `thinking`: `{"type": "enabled", "budget_tokens": N}` or `{"type": "disabled"}`
- `max_tokens`: Usually 16384 or higher
- `stream`: `true` (Claude Code always streams)
- `stop_sequences`: Custom stop words

### What Claude Code Expects

Claude Code expects these fields in responses:
- `type: "message"` with `content` array of blocks
- `stop_reason`: One of `end_turn`, `max_tokens`, `tool_use`, `stop_sequence`, `refusal`
- `usage`: With `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`
- Streaming: `message_start`, `content_block_start/delta/stop`, `message_delta`, `message_stop`, `ping`
- `message_delta` usage must include cumulative `input_tokens` (for status bar display)
- `signature_delta` before thinking `content_block_stop`
- Ping events to prevent 5-minute idle timeout

## Testing

### Unit Tests

```bash
python -m pytest tests/unit/test_anthropic_bridge.py -v
```

63 tests covering:
- Provider detection
- Request translation (all content block types, tools, tool_choice, thinking)
- Response translation (text, thinking, tool_use, stop reasons, usage)
- Streaming translation (text, thinking, tool_use, interleaved, ping, signature_delta)
- Error translation (status code mapping, body extraction)
- Stop sequence detection (streaming + non-streaming)
- Cache usage fields
- Parallel tool use

### E2E Testing

E2E tests can be run against a live Guardian instance with a loaded model:

```bash
# Non-streaming
curl -X POST http://localhost:11434/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your-key>" \
  -d '{"model":"qwen3.6-35b-uncensored","messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'

# Streaming
curl -X POST http://localhost:11434/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your-key>" \
  -d '{"model":"qwen3.6-35b-uncensored","messages":[{"role":"user","content":"Hello"}],"max_tokens":50,"stream":true}'
```

## File Reference

| File | Purpose |
|---|---|
| `app/proxy/anthropic_bridge.py` | Cloud bridge: full Anthropic ↔ OpenAI translation |
| `app/proxy/server.py` | Local model enrichment layer + cloud bridge integration |
| `tests/unit/test_anthropic_bridge.py` | 63 unit tests for the bridge |
| `docs/LLM_ROUTER.md` | Cloud provider routing documentation |
| `config/settings.yaml` | Provider configuration |
| `config/cloud_keys.json` | Per-key cloud credentials |
