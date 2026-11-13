# Copilot bridge reliability and request-cost contract

## Routes and activation boundary

The shipped Guardian provider configuration selects two Copilot aliases through the standalone LiteLLM proxy (`config/providers/github-copilot.settings.yaml`, `base_url: http://127.0.0.1:4000/v1`). Its GPT aliases use Responses against copilot-api on loopback 4141. The dynamic adapter implemented by `app/copilot_bridge.py` has a separate configured listener, loopback 4142. Running that adapter does not mean Guardian already routes through it.

The candidate consolidated route is Guardian → dynamic adapter → copilot-api. Keep Guardian's explicit `catalog_allowlist` and model overrides: discovering a model is not permission to onboard it, and cheaper transport does not justify enabling every subscription model. Do not route traffic through both adapters, automatically fall back to a direct paid provider, or replace upstream model identities with guessed aliases. This change improves the candidate adapter but deliberately does not alter the live or shipped Guardian provider route.

Activation is operator-controlled: compare the selected models' chat, tool-call/result IDs, reasoning, image input, cache/usage metadata, and streaming behavior first; then reconcile destination policy, provider base URL and the explicitly selected catalog. Use a small operator-approved live smoke request if necessary, with no automatic replays. A production restart/switch and verified provider billing/cache hit rate are not evidence supplied by these offline tests.

## Cost controls

- One completion per request: the adapter rejects `n` other than integer `1`. It invokes the selected translator once and supplies `num_retries=0` and `max_retries=0`; the caller owns retry decisions. Its borrowed HTTP handler overrides LiteLLM's separate connection-error replay with one guarded send. Embeddings use the configured zero-retry OpenAI SDK endpoint directly because the installed LiteLLM embedding path turns zero retries into its default and mutates the supplied SDK client. Both destination and authentication are pinned on the SDK client rather than assumed to follow per-call kwargs. It never resumes a partial generation by replaying the request and never configures a fallback model. Test the installed dependency's actual behavior instead of assuming kwargs disable every SDK retry.
- Heartbeats are SSE comments. They generate no model request, prompt content or output token, and do not extend the upstream idle deadline. The default idle/request budget remains 1200 seconds to avoid turning legitimate long reasoning into repeated costly requests.
- Message/tool content and order are not rewritten by the adapter; provider protocol translation remains LiteLLM's responsibility. Response usage and upstream cached-token counters are retained. No shared response cache stores user prompts or answers in this adapter.
- OpenAI-compatible protocols accept an explicit `prompt_cache_key`. `prompt_cache_retention` accepts only `in-memory` or `24h`, with no forced default. The deployed LiteLLM 1.103.2 conversion drops retention as a normal kwarg, so the adapter forwards only that validated field through its internal `extra_body`. User-supplied arbitrary `extra_body`, credentials, headers, provider, client and retry settings never override transport. These OpenAI cache controls are rejected on the messages protocol, not silently transformed into Anthropic caching promises.
- No cache key is manufactured from a mutable timestamp, model-call ID or global user identity. Upstream provider caching and plan billing remain external policies; preserved cache controls are not proof of a cache hit or a monetary saving. Extended retention is never enabled automatically.

## Streaming lifecycle

The response wrapper owns one existing upstream stream and one pending asynchronous read. It emits the original LiteLLM JSON chunks, including tool deltas, finish reasons and usage. A successful iterator end emits `[DONE]`. An iterator error or idle timeout emits a sanitized OpenAI-style `error` object in SSE `data` and does not emit a successful terminal marker.

An ASGI disconnect watcher also owns inference before headers, for streaming, nonstreaming and embeddings. It starts only after full request-body consumption, cancels one abandoned invocation, and closes a result acquired in the cancellation race. The HTTP operations retain the underlying response so messages iterators without an `aclose` cannot leak it.

Cleanup is cancellation-shielded and shares one bounded deadline across the result and distinct iterator. An independently owned cleanup task handles both AnyIO-scope cancellation and direct `asyncio.Task.cancel`, including cancellation during `aclose` after EOF; duplicate callers join the same task/deadline. It covers disconnect before iteration, during a read, and while ASGI sends a chunk. Python cannot forcibly stop a coroutine that suppresses cancellation or synchronous blocking provider code: cooperative cleanup is tested, and a cancellation-resistant task may outlive the response until it cooperates. No error handler retries inference.

## Errors and request options

Upstream errors retain a valid HTTP status and use a fixed credential-safe OpenAI error envelope. Only a validated `Retry-After` delay or IMF-fixdate is forwarded on 429 or 503. Raw upstream exception text, body and unrelated headers are never returned. Errors after HTTP 200 are in-band; HTTP headers cannot be retroactively changed.

The authenticated `/v1/models` response exposes the adapter's `bridge.chat_fields` and `bridge.embedding_fields` separately from upstream model capabilities. Unknown top-level request options are rejected before inference instead of being accepted and discarded. The Responses adapter also rejects non-null Chat-only `stop`, `seed`, `presence_penalty`, `frequency_penalty` and `logprobs`, which are not in its upstream contract. Credential/transport override fields remain inert for boundary compatibility. Explicit upstream `tool_calls: false`, `streaming: false` or a listed reasoning-effort restriction is enforced before generation; missing metadata is not interpreted as a prohibition or as verified support.

Minimum message/tool/token-limit and embedding structures are validated before any generation. Nullable optional token limits and `stream_options` mean unspecified. Assistant refusal-only history is accepted unchanged at validation; protocol-local copies encode the refusal as a Responses refusal item or messages assistant text so upstream translation cannot silently drop the safety turn. Native chat retains its original refusal field. Messages-protocol image input must use inline `data:image` content; remote image URLs are rejected because LiteLLM otherwise downloads them via an unrelated module-global client outside Guardian's destination allowlist. Chat/Responses retain their native media URL handling without the adapter performing a download.

Actual asynchronous Responses translation in LiteLLM 1.103.2 drops the supplied HTTP client. The adapter reuses the dependency's Chat↔Responses translators around a direct `aresponses` call with an explicitly guarded handler, rather than patching globals or opening a second inference route. Stream usage options are carried into the chat chunk wrapper.

This strict option handling is an intentional request-contract change for the candidate adapter. Check actual client payloads before activation; do not silently drop unsupported fields merely to make a smoke test green.

## Catalog snapshots

Refresh happens at startup and periodically, never as an extra operation on every inference. Invalid entries are isolated and counted. Duplicate public identities are excluded conservatively; a revoked model is not kept alive merely because another row is malformed. A valid empty/all-disabled response publishes an empty snapshot. Transport or invalid top-level failures keep the prior snapshot and record only the exception class, without extending freshness.

Catalog status contains `usable`, `is_stale`, `age_seconds`, timestamp/error and counters. Usability ages on a monotonic clock; wall-clock timestamps are diagnostics only, so clock corrections cannot prolong a revoked snapshot's usable grace. After failed discovery `is_stale` is true, while `usable` remains true during the configured age-based grace period. The adapter rejects inference after the maximum snapshot age; it does not repeatedly refresh or call a missing model to discover availability. `/v1/models` may still expose the last snapshot with an explicit stale status for diagnosis.

## Configuration

Use `config/github-copilot-bridge.settings.yaml`; it references existing environment-variable names for credentials, never inline tokens.

| Setting | Default | Meaning |
| --- | --- | --- |
| `refresh_seconds` | 300 | Periodic catalog interval |
| `catalog_timeout_seconds` | 30 | Discovery request timeout |
| `timeout_seconds` | 1200 | LiteLLM request budget |
| `heartbeat_seconds` | 15 | SSE comment cadence |
| `stream_idle_seconds` | 1200 | Maximum silence from the upstream iterator |
| `stream_cleanup_seconds` | 5 | Shared disconnect cleanup budget |
| `catalog_max_age_seconds` | 3600 | Maximum usable age since successful discovery |
| `max_body_bytes` | 16777216 | Buffered client request limit |

Intervals must be positive finite numbers and the body limit a positive integer; booleans are not numbers for this configuration. A missing `stream_idle_seconds` follows the configured `timeout_seconds`. Settings changes are read at process startup, not advertised as live reload.

## Offline verification

Use Guardian's existing pytest environment for bridge/catalog/SSE/error tests:

```bash
./venv/bin/python -m pytest tests/unit/test_copilot_bridge.py tests/unit/test_copilot_catalog.py tests/unit/test_copilot_stream.py tests/unit/test_copilot_cost_contract.py -q
```

Run the installed dependency's transport check with the existing bridge interpreter; it replaces HTTP with a mock and uses synthetic credentials, never real Copilot traffic:

```bash
/home/flip/.venvs/litellm/bin/python3.12 -B scripts/check_copilot_runtime.py
```

The checker denies all socket/DNS operations and asserts one actual HTTP attempt on synthetic 429, connection error and protocol error for chat, Responses and messages. It validates streaming/nonstreaming Responses tool documentation/schema, tool-result ID roundtrip, explicit cache controls and cached/reasoning usage, plus messages successful tools/cache usage and idle cleanup, embeddings float/base64/default formatting and all three failure cases, and remote-media rejection. All HTTP callbacks assert exact configured destinations and upstream authentication. It reports the installed LiteLLM version; provider cache-hit rate and billing savings remain explicitly unmeasured.
