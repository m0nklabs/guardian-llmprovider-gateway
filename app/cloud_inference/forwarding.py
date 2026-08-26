"""Cloud forwarding — the full upstream forwarding path for cloud routes.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Contains ``forward_to_cloud_provider``: streaming/non-streaming forwarding to
cloud providers with failover attempt ordering, per-key rate-limit handling,
Anthropic\u2194OpenAI translation, live usage tracking, and capture dispatch.
All external dependencies are injected once at startup via :func:`init`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.proxy.providers import CloudProvider, ProviderRegistry
from app.capture.schema import BuildContext
from app.capture.policy import PolicyResult
from app.capture.stream_assembler import StreamResponseAssembler
from app.gateway.streaming import StreamProgressWatchdog

logger = logging.getLogger("Guardian")

# ── Injected (set once at startup by init()) ─────────────────────────
_resolve_cloud_attempts = None
_prepare_cloud_candidate_request = None
_extract_cloud_response_content = None
_guardian_debug_headers = None
_is_retryable_cloud_error = None
_sanitize_proxied_response_headers = None
_messages_contain_image_input = None
_get_cloud_key_fingerprint = None
_set_request_usage_metadata = None
_start_live_request_usage = None
_update_live_request_usage = None
_finish_live_request_usage = None
_record_request_token_usage = None
_record_usage_from_payload = None
_coerce_usage_int = None
_dispatch_capture_request_completed = None
_dispatch_capture_request_cancelled = None
_dispatch_capture_request_failed = None
_classify_capture_error = None
_sanitize_capture_error_message = None
_iter_sse_lines_with_watchdog = None
_translate_openai_error_to_anthropic = None
_translate_openai_response_to_anthropic = None
_translate_openai_stream_to_anthropic = None
cloud_rate_limiter = None
failover_health = None
_GuardianRequestCancelled = None
STREAM_HEARTBEAT_INTERVAL_S = 15.0
_grammar_cloud_auto_convert_json = False
_grammar_cloud_strict_mode = False
_grammar_enabled = True


def init(
    *,
    resolve_cloud_attempts,
    prepare_cloud_candidate_request,
    extract_cloud_response_content,
    guardian_debug_headers,
    is_retryable_cloud_error,
    sanitize_proxied_response_headers,
    messages_contain_image_input,
    get_cloud_key_fingerprint,
    set_request_usage_metadata,
    start_live_request_usage,
    update_live_request_usage,
    finish_live_request_usage,
    record_request_token_usage,
    record_usage_from_payload,
    coerce_usage_int,
    dispatch_capture_request_completed,
    dispatch_capture_request_cancelled,
    dispatch_capture_request_failed,
    classify_capture_error,
    sanitize_capture_error_message,
    iter_sse_lines_with_watchdog,
    translate_openai_error_to_anthropic,
    translate_openai_response_to_anthropic,
    translate_openai_stream_to_anthropic,
    rate_limiter,
    health_tracker,
    guardian_request_cancelled,
    stream_heartbeat_interval_s,
    grammar_enabled=True,
    grammar_cloud_auto_convert_json=False,
    grammar_cloud_strict_mode=False,
) -> None:
    """Inject all dependencies. Called once at startup."""
    global _resolve_cloud_attempts, _prepare_cloud_candidate_request
    global _extract_cloud_response_content, _guardian_debug_headers
    global _is_retryable_cloud_error, _sanitize_proxied_response_headers
    global _messages_contain_image_input, _get_cloud_key_fingerprint
    global _set_request_usage_metadata, _start_live_request_usage
    global _update_live_request_usage, _finish_live_request_usage
    global _record_request_token_usage, _record_usage_from_payload, _coerce_usage_int
    global _dispatch_capture_request_completed, _dispatch_capture_request_cancelled
    global _dispatch_capture_request_failed, _classify_capture_error
    global _sanitize_capture_error_message, _iter_sse_lines_with_watchdog
    global _translate_openai_error_to_anthropic, _translate_openai_response_to_anthropic
    global _translate_openai_stream_to_anthropic
    global cloud_rate_limiter, failover_health, _GuardianRequestCancelled
    global STREAM_HEARTBEAT_INTERVAL_S
    global _grammar_cloud_auto_convert_json, _grammar_cloud_strict_mode, _grammar_enabled
    _resolve_cloud_attempts = resolve_cloud_attempts
    _prepare_cloud_candidate_request = prepare_cloud_candidate_request
    _extract_cloud_response_content = extract_cloud_response_content
    _guardian_debug_headers = guardian_debug_headers
    _is_retryable_cloud_error = is_retryable_cloud_error
    _sanitize_proxied_response_headers = sanitize_proxied_response_headers
    _messages_contain_image_input = messages_contain_image_input
    _get_cloud_key_fingerprint = get_cloud_key_fingerprint
    _set_request_usage_metadata = set_request_usage_metadata
    _start_live_request_usage = start_live_request_usage
    _update_live_request_usage = update_live_request_usage
    _finish_live_request_usage = finish_live_request_usage
    _record_request_token_usage = record_request_token_usage
    _record_usage_from_payload = record_usage_from_payload
    _coerce_usage_int = coerce_usage_int
    _dispatch_capture_request_completed = dispatch_capture_request_completed
    _dispatch_capture_request_cancelled = dispatch_capture_request_cancelled
    _dispatch_capture_request_failed = dispatch_capture_request_failed
    _classify_capture_error = classify_capture_error
    _sanitize_capture_error_message = sanitize_capture_error_message
    _iter_sse_lines_with_watchdog = iter_sse_lines_with_watchdog
    _translate_openai_error_to_anthropic = translate_openai_error_to_anthropic
    _translate_openai_response_to_anthropic = translate_openai_response_to_anthropic
    _translate_openai_stream_to_anthropic = translate_openai_stream_to_anthropic
    cloud_rate_limiter = rate_limiter
    failover_health = health_tracker
    _GuardianRequestCancelled = guardian_request_cancelled
    STREAM_HEARTBEAT_INTERVAL_S = stream_heartbeat_interval_s
    _grammar_enabled = grammar_enabled
    _grammar_cloud_auto_convert_json = grammar_cloud_auto_convert_json
    _grammar_cloud_strict_mode = grammar_cloud_strict_mode


def _derive_response_format_from_grammar(grammar: Any, json_schema: Any) -> Optional[Dict[str, Any]]:
    """Convert a JSON-targeting grammar/schema to OpenAI-native response_format.

    Returns ``None`` when the payload cannot be converted (a real GBNF
    grammar string is not convertible without a full GBNF parser).
    """
    if isinstance(json_schema, dict):
        return {"type": "json_schema", "json_schema": json_schema}
    if isinstance(grammar, str):
        try:
            parsed = json.loads(grammar)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(parsed, dict):
            return {"type": "json_schema", "json_schema": parsed}
    return None


def _strip_cloud_grammar(body: Dict[str, Any], *, allow_json_convert: bool) -> Dict[str, Any]:
    """Strip GBNF/llama-server-specific grammar fields from a cloud-bound body.

    Cloud providers do not accept GBNF grammar strings or llama-server's
    ``json_schema`` field. OpenAI-native ``response_format`` is preserved
    as-is. When ``allow_json_convert`` is True, a JSON-targeting grammar
    or schema is converted to the OpenAI-native ``response_format`` form.
    """
    if not isinstance(body, dict):
        return body
    stripped = dict(body)
    grammar = stripped.pop("grammar", None)
    json_schema = stripped.pop("json_schema", None)
    if allow_json_convert and (grammar is not None or json_schema is not None):
        converted = _derive_response_format_from_grammar(grammar, json_schema)
        if converted is not None and "response_format" not in stripped:
            stripped["response_format"] = converted
    return stripped


async def forward_to_cloud_provider(
    path: str,
    body: bytes,
    json_body: Dict[str, Any],
    model_name: str,
    request: Request,
    client_id: str,
    *,
    capture_ctx: Optional[BuildContext] = None,
    capture_policy_result: Optional["PolicyResult"] = None,
    cloud_request_id: Optional[str] = None,
    cloud_capture_start_time: Optional[float] = None,
) -> Response:
    """Forward an inference request to a cloud LLM provider.

    Cloud requests bypass the VRAM scheduler, model switch logic, and inference
    queue — the cloud API handles its own rate limiting and concurrency.
    Streaming responses are proxied in real-time so SSE tokens reach the client
    without buffering.

    Supports three routing modes:
    - **Global cloud models** (e.g. ``openai/gpt-4o``): routed via the
      ``ProviderRegistry`` using the provider's global API key from settings.yaml.
    - **Cloud addresses** (e.g. ``openrouter/deepseek/deepseek-v4-flash-0731``):
      routed via the dynamic ``CloudModelCatalog`` using the provider's settings
      API key, gated on the requesting key's ``cloud_gateway_access``.
    - **Failover groups** (``failover/{group}``): tries each provider
      candidate configured for *group* in health-ordered priority, skipping a
      candidate that is currently tripped (see :mod:`app.proxy.failover`) and
      falling through to the next one on a connection failure or retryable
      upstream error (429/5xx). A successful response resets that candidate's
      health so Guardian prefers it again once it recovers.
    """
    requires_vision = _messages_contain_image_input(json_body.get("messages", []))
    attempts, failover_group = _resolve_cloud_attempts(
        model_name,
        request,
        client_id,
        requires_vision=requires_vision,
    )

    # ── Grammar-Constrained Decoding (GCD): cloud path ──────────────
    # Cloud providers do not accept GBNF grammar strings or llama-server's
    # ``json_schema`` field. Strip them (or, in strict mode, reject the
    # request outright). OpenAI-native ``response_format`` is preserved.
    if "grammar" in json_body or "json_schema" in json_body:
        # Kill-switch takes precedence over strict-mode and auto-convert: when
        # ``grammar.enabled`` is false, strip unconditionally on both local and
        # cloud paths (per settings.yaml) — never 400 in kill-switch mode.
        if not _grammar_enabled:
            json_body = _strip_cloud_grammar(json_body, allow_json_convert=False)
        elif _grammar_cloud_strict_mode:
            provider_name = attempts[0][0].name if attempts else "cloud provider"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Grammar-Constrained Decoding is not supported by cloud provider "
                    f"'{provider_name}'. Use OpenAI-native `response_format` instead, "
                    f"or enable grammar.cloud_auto_convert_json for JSON-targeting grammars."
                ),
            )
        else:
            json_body = _strip_cloud_grammar(
                json_body,
                allow_json_convert=_grammar_cloud_auto_convert_json,
            )
    cloud_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)

    is_stream = bool(json_body.get("stream", False))
    _set_request_usage_metadata(request, model=model_name, streamed=is_stream)
    _start_live_request_usage(request)
    stream_http_client: Optional[httpx.AsyncClient] = None

    # Track cloud capture metadata
    _cloud_capture_attempts = 0

    for attempt_index, (provider, upstream_model) in enumerate(attempts):
        is_last_attempt = attempt_index == len(attempts) - 1
        effective_path, candidate_json_body, candidate_body, needs_translation = (
            _prepare_cloud_candidate_request(provider, upstream_model, path, json_body, cloud_key_fingerprint)
        )

        if needs_translation:
            logger.info(
                "🌉 Anthropic→OpenAI bridge: translating /v1/messages for provider '%s'",
                provider.name,
            )

        forward_headers = ProviderRegistry.build_forward_headers(provider, cloud_key_fingerprint, app_name=client_id)
        forward_url = ProviderRegistry.build_forward_url(provider, effective_path)
        timeout = httpx.Timeout(provider.timeout_seconds, connect=15.0)

        if failover_group is not None:
            logger.info(
                "🔀 Failover group '%s': attempt %d/%d via '%s'",
                failover_group,
                attempt_index + 1,
                len(attempts),
                provider.name,
            )
        logger.info(
            "☁️  Cloud route: client '%s' → %s /v1/%s (model: %s, stream: %s)",
            client_id,
            provider.name,
            path,
            model_name,
            is_stream,
        )

        if is_stream:
            stream_client: Optional[httpx.AsyncClient] = None

            async def send_stream_request() -> httpx.Response:
                nonlocal stream_client
                stream_client = httpx.AsyncClient(timeout=timeout)
                req = stream_client.build_request(
                    "POST",
                    forward_url,
                    content=candidate_body,
                    headers=forward_headers,
                )
                return await stream_client.send(req, stream=True)

            async def read_stream_rate_limit(response: httpx.Response) -> str:
                nonlocal stream_client
                try:
                    body_bytes = await response.aread()
                finally:
                    try:
                        await response.aclose()
                    finally:
                        if stream_client is not None:
                            await stream_client.aclose()
                            stream_client = None
                return body_bytes.decode("utf-8", errors="replace")

            try:
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_stream_request,
                    on_429=read_stream_rate_limit,
                    retry_429=failover_group is None,
                )
            except Exception as e:
                if stream_client is not None:
                    await stream_client.aclose()
                failover_health.record_failure(provider.name, upstream_model)
                logger.error(
                    "☁️  Cloud provider '%s' request failed (attempt %d/%d): %s",
                    provider.name, attempt_index + 1, len(attempts), e,
                )
                if not is_last_attempt:
                    _cloud_capture_attempts = attempt_index + 1
                    continue
                _finish_live_request_usage(request, status_code=502, response_bytes=0)
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=_classify_capture_error(e),
                    http_status=502,
                    sanitized_message=_sanitize_capture_error_message(e),
                    queue_wait_ms=0,
                    duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
                raise HTTPException(status_code=502, detail=f"Cloud provider request failed: {e}")

            # ── Failover 429 probe: wait and retry once before falling through ──
            # When a failover candidate returns HTTP 429, the priority source
            # (e.g. NVIDIA's free tier) gets one more chance after a 60s wait.
            # Concurrent requests skip the rate-limited candidate and go
            # directly to the next one (OR), keeping them responsive.
            # Skipped when cloud_retry.enabled=false (agent harness owns 429s).
            if (
                getattr(resp, "status_code", 0) == 429
                and failover_group is not None
                and not is_last_attempt
                and cloud_rate_limiter.config.enabled
            ):
                _probe_wait = failover_health._rate_limit_cooldown_seconds
                logger.info(
                    "⏳ Failover 429: '%s' rate-limited; waiting %.0fs before one retry...",
                    provider.name, _probe_wait,
                )
                failover_health.record_rate_limited(provider.name, upstream_model)
                await asyncio.sleep(_probe_wait)
                failover_health.clear_rate_limit(provider.name, upstream_model)
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_stream_request,
                    on_429=read_stream_rate_limit,
                    retry_429=False,
                )

            stream_http_client = stream_client

            # ── Error translation for Anthropic clients ───────────────────
            # If the upstream provider returned an error (non-SSE body), translate
            # it to Anthropic error format instead of trying to stream it.
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                await resp.aclose()
                if stream_http_client is not None:
                    await stream_http_client.aclose()
                if resp.status_code != 429:
                    # 429 (rate limited) does not count against a provider's
                    # health — Claude Code already retries these itself and
                    # the provider is usually fine, just busy.
                    failover_health.record_failure(provider.name, upstream_model)
                else:
                    # Mark provider as rate-limited so concurrent requests
                    # skip it and fall through to the next candidate directly.
                    failover_health.record_rate_limited(provider.name, upstream_model)
                if _is_retryable_cloud_error(resp.status_code, body_bytes.decode("utf-8", errors="replace")) and not is_last_attempt:
                    logger.warning(
                        "☁️  Cloud provider '%s' returned %s after local retry budget "
                        "(attempt %d/%d); trying next candidate",
                        provider.name, resp.status_code, attempt_index + 1, len(attempts),
                    )
                    continue
                _finish_live_request_usage(request, status_code=resp.status_code, response_bytes=len(body_bytes))
                # ── Capture: request_failed (streaming HTTP error) ──
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=f"cloud_http_{resp.status_code}",
                    http_status=resp.status_code,
                    sanitized_message=f"Cloud provider returned HTTP {resp.status_code}",
                    queue_wait_ms=0,
                    duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
                if needs_translation:
                    try:
                        error_payload = json.loads(body_bytes)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        error_payload = body_bytes.decode("utf-8", errors="replace")
                    anthropic_error = _translate_openai_error_to_anthropic(resp.status_code, error_payload)
                    logger.warning(
                        "🌉 Anthropic bridge: translated %s error from %s: %s",
                        resp.status_code,
                        provider.name,
                        anthropic_error["error"]["message"][:200],
                    )
                    return Response(
                        content=json.dumps(anthropic_error).encode("utf-8"),
                        status_code=resp.status_code,
                        headers={"Content-Type": "application/json"},
                    )
                return Response(
                    content=body_bytes,
                    status_code=resp.status_code,
                    headers={
                        **_sanitize_proxied_response_headers(resp.headers),
                        **_guardian_debug_headers(provider, upstream_model, failover_group),
                    },
                )

            # Success — this candidate wins. Bind the winning json_body and
            # fall through to the streaming response construction below.
            failover_health.record_success(provider.name, upstream_model)
            json_body = candidate_json_body
            break

        # Non-streaming
        async with httpx.AsyncClient(timeout=timeout) as non_stream_http_client:
            async def send_non_stream_request() -> httpx.Response:
                return await non_stream_http_client.post(
                    forward_url,
                    content=candidate_body,
                    headers=forward_headers,
                )

            try:
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_non_stream_request,
                    retry_429=failover_group is None,
                )
            except Exception as e:
                failover_health.record_failure(provider.name, upstream_model)
                logger.error(
                    "☁️  Cloud provider '%s' request failed (attempt %d/%d): %s",
                    provider.name, attempt_index + 1, len(attempts), e,
                )
                if not is_last_attempt:
                    _cloud_capture_attempts = attempt_index + 1
                    continue
                _finish_live_request_usage(request, status_code=502, response_bytes=0)
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=_classify_capture_error(e),
                    http_status=502,
                    sanitized_message=_sanitize_capture_error_message(e),
                    queue_wait_ms=0,
                    duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
                raise HTTPException(status_code=502, detail=f"Cloud provider request failed: {e}")

            # ── Failover 429 probe: wait and retry once before falling through ──
            # Skipped when cloud_retry.enabled=false (agent harness owns 429s).
            if (
                getattr(resp, "status_code", 0) == 429
                and failover_group is not None
                and not is_last_attempt
                and cloud_rate_limiter.config.enabled
            ):
                _probe_wait = failover_health._rate_limit_cooldown_seconds
                logger.info(
                    "⏳ Failover 429: '%s' rate-limited; waiting %.0fs before one retry...",
                    provider.name, _probe_wait,
                )
                failover_health.record_rate_limited(provider.name, upstream_model)
                await asyncio.sleep(_probe_wait)
                failover_health.clear_rate_limit(provider.name, upstream_model)
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_non_stream_request,
                    retry_429=False,
                )

            if (
                resp.status_code >= 400
                and _is_retryable_cloud_error(resp.status_code, resp.text)
                and not is_last_attempt
            ):
                if resp.status_code != 429:
                    failover_health.record_failure(provider.name, upstream_model)
                else:
                    failover_health.record_rate_limited(provider.name, upstream_model)
                logger.warning(
                    "☁️  Cloud provider '%s' returned %s after local retry budget "
                    "(attempt %d/%d); trying next candidate",
                    provider.name, resp.status_code, attempt_index + 1, len(attempts),
                )
                continue

            if resp.status_code < 400:
                failover_health.record_success(provider.name, upstream_model)
            elif resp.status_code != 429:
                # 429 (rate limited) does not count against a provider's health
                # — Claude Code already retries these itself and the provider is
                # usually fine, just busy.
                failover_health.record_failure(provider.name, upstream_model)

            # Record token usage from response payload
            try:
                payload = resp.json()
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            _record_usage_from_payload(client_id, f"/v1/{path}", model_name, payload, request=request)

            # ── Capture: request_completed or request_failed (non-streaming) ──
            _cloud_capture_attempts = attempt_index + 1
            _cloud_capture_duration_ms = (
                (time.monotonic() - cloud_capture_start_time) * 1000
                if cloud_capture_start_time else None
            )
            if resp.status_code >= 400:
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=f"cloud_http_{resp.status_code}",
                    http_status=resp.status_code,
                    sanitized_message=f"Cloud provider returned HTTP {resp.status_code}",
                    queue_wait_ms=0,
                    duration_ms=_cloud_capture_duration_ms,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
            else:
                _cloud_content, _cloud_tool_calls = _extract_cloud_response_content(payload)
                _dispatch_capture_request_completed(
                    capture_ctx,
                    policy_result=capture_policy_result,
                    response_content=_cloud_content,
                    tool_calls=_cloud_tool_calls,
                    prompt_tokens=payload.get("usage", {}).get("prompt_tokens", payload.get("usage", {}).get("input_tokens", 0)) if isinstance(payload, dict) else None,
                    completion_tokens=payload.get("usage", {}).get("completion_tokens", payload.get("usage", {}).get("output_tokens", 0)) if isinstance(payload, dict) else None,
                    http_status=resp.status_code,
                    streamed=False,
                    incomplete=False,
                    attempts=_cloud_capture_attempts,
                    duration_ms=_cloud_capture_duration_ms,
                ) if capture_ctx is not None else None

            debug_headers = _guardian_debug_headers(provider, upstream_model, failover_group)
            # Suffix the client-visible model field with the winning provider on
            # failover routes only, so an ambiguous "which provider answered?"
            # is resolvable from the response body itself.
            response_model_name = f"{model_name}@{provider.name}" if failover_group else model_name

            # ── Anthropic response translation (non-streaming) ───────────
            if needs_translation and payload and isinstance(payload, dict):
                # Translate errors first
                if resp.status_code >= 400:
                    anthropic_error = _translate_openai_error_to_anthropic(resp.status_code, payload)
                    return Response(
                        content=json.dumps(anthropic_error).encode("utf-8"),
                        status_code=resp.status_code,
                        headers={"Content-Type": "application/json", **debug_headers},
                    )
                anthropic_response = _translate_openai_response_to_anthropic(
                    payload,
                    response_model_name,
                    request_stop_sequences=candidate_json_body.get("stop_sequences"),
                )
                translated_content = json.dumps(anthropic_response).encode("utf-8")
                return Response(
                    content=translated_content,
                    status_code=resp.status_code,
                    headers={"Content-Type": "application/json", **debug_headers},
                )

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={**_sanitize_proxied_response_headers(resp.headers), **debug_headers},
            )

    if stream_http_client is None:
        _finish_live_request_usage(request, status_code=502, response_bytes=0)
        raise HTTPException(status_code=502, detail="Cloud streaming client was not initialized")
    stream_response_client = stream_http_client

    # ── Streaming response construction ─────────────────────────────────
    # Only reached after a successful `break` in the streaming branch above —
    # the non-streaming branch always returns from within the loop.
    debug_headers = _guardian_debug_headers(provider, upstream_model, failover_group)
    # Suffix the client-visible model field with the winning provider on
    # failover routes only (see _guardian_debug_headers docstring).
    response_model_name = f"{model_name}@{provider.name}" if failover_group else model_name
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}

    # ── Capture: streaming assembler for cloud route ──
    # Assembled via StreamResponseAssembler().add_sse_line() (raw SSE lines),
    # matching the local paths in gateway/routing.py and local_inference/ollama.py.
    # Every assembler call is fail-open: capture must never break the stream
    # (this path returned HTTP 500 on 2026-08-26 when the assembler was called
    # with a wrong API — pinned by scripts/pre_restart_check.py call-site check).
    _cloud_assembler: Optional[StreamResponseAssembler] = None
    if capture_ctx is not None:
        _cloud_assembler = StreamResponseAssembler()

    async def _read_sse_lines():
        """Yield raw SSE lines from the upstream response with watchdog."""
        watchdog = StreamProgressWatchdog(provider.timeout_seconds)
        async for line in _iter_sse_lines_with_watchdog(
            resp,
            watchdog,
            request_id=str(uuid.uuid4()),
            route=f"/v1/{path}",
            client_id=client_id,
            model_name=model_name,
            heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S,
        ):
            yield line

    _cloud_stream_cancelled = False

    async def cloud_stream():
        nonlocal _cloud_stream_cancelled
        try:
            if needs_translation:
                # ── Anthropic streaming translation ───────────────
                # Translate OpenAI SSE chunks to Anthropic SSE events
                async for event_line in _translate_openai_stream_to_anthropic(
                    _read_sse_lines(),
                    response_model_name,
                    request_stop_sequences=json_body.get("stop_sequences") if needs_translation else None,
                ):
                    # Extract usage from the translated events
                    if "message_delta" in event_line:
                        try:
                            # Parse the data line to get output_tokens
                            for part in event_line.split("\n"):
                                if part.startswith("data: "):
                                    data = json.loads(part[6:])
                                    if data.get("type") == "message_delta":
                                        usage_totals["completion_tokens"] = max(
                                            usage_totals["completion_tokens"],
                                            data.get("usage", {}).get("output_tokens", 0),
                                        )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    # ── Capture: feed every translated data: line to assembler ──
                    if _cloud_assembler is not None:
                        for part in event_line.split("\n"):
                            if part.startswith("data: "):
                                try:
                                    _cloud_assembler.add_sse_line(part)
                                except Exception:
                                    pass
                    encoded_line = event_line.encode("utf-8")
                    _update_live_request_usage(
                        request,
                        response_bytes_delta=len(encoded_line),
                    )
                    yield encoded_line
            else:
                # ── Pass-through (no translation needed) ──────────
                async for line in _read_sse_lines():
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            usage = data.get("usage") or {}
                            if isinstance(usage, dict):
                                usage_totals["prompt_tokens"] = max(
                                    usage_totals["prompt_tokens"],
                                    _coerce_usage_int(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
                                )
                                usage_totals["completion_tokens"] = max(
                                    usage_totals["completion_tokens"],
                                    _coerce_usage_int(
                                        usage.get("completion_tokens", usage.get("output_tokens", 0))
                                    ),
                                )
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
                    encoded_line = (line + "\n").encode("utf-8")
                    _update_live_request_usage(
                        request,
                        response_bytes_delta=len(encoded_line),
                    )
                    # ── Capture: feed raw SSE line to assembler (fail-open) ──
                    if _cloud_assembler is not None:
                        try:
                            _cloud_assembler.add_sse_line(line)
                        except Exception:
                            pass
                    yield encoded_line
        except (asyncio.CancelledError, _GuardianRequestCancelled, httpx.StreamClosed, httpx.ReadError, httpx.RemoteProtocolError):
            _cloud_stream_cancelled = True
        finally:
            await resp.aclose()
            await stream_response_client.aclose()
            _record_request_token_usage(
                client_id,
                f"/v1/{path}",
                model_name,
                request=request,
                prompt_tokens=usage_totals["prompt_tokens"],
                completion_tokens=usage_totals["completion_tokens"],
            )
            _finish_live_request_usage(
                request,
                status_code=resp.status_code,
            )
            # ── Capture: request_completed or request_cancelled (streaming, cloud) ──
            if capture_ctx is not None:
                if _cloud_stream_cancelled:
                    _dispatch_capture_request_cancelled(
                        capture_ctx,
                        cancel_reason="client_disconnect",
                        duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                        attempts=_cloud_capture_attempts,
                        policy_result=capture_policy_result,
                    )
                else:
                    _cloud_stream_content = None
                    _cloud_stream_tool_calls = None
                    if _cloud_assembler is not None:
                        try:
                            _cloud_assembled = _cloud_assembler.assemble()
                            _cloud_stream_content = _cloud_assembled.get("content")
                            _cloud_stream_tool_calls = _cloud_assembled.get("tool_calls")
                        except Exception:
                            # Fail-open: a broken assembler must never turn a
                            # successful upstream response into a 500.
                            _cloud_stream_content = None
                            _cloud_stream_tool_calls = None
                    _dispatch_capture_request_completed(
                        capture_ctx,
                        policy_result=capture_policy_result,
                        response_content=_cloud_stream_content,
                        tool_calls=_cloud_stream_tool_calls,
                        prompt_tokens=usage_totals["prompt_tokens"],
                        completion_tokens=usage_totals["completion_tokens"],
                        http_status=resp.status_code,
                        streamed=True,
                        incomplete=resp.status_code != 200,
                        attempts=_cloud_capture_attempts,
                        duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    )

    return StreamingResponse(
        cloud_stream(),
        status_code=resp.status_code,
        media_type="text/event-stream",
        headers={
            **_sanitize_proxied_response_headers(resp.headers),
            **debug_headers,
        },
    )
