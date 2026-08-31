"""Capture dispatch helpers — thin fail-open wrappers around the capture controller.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).

These functions bridge the FastAPI request pipeline to the privacy-aware
capture subsystem.  They are all fail-open: any exception is swallowed so
that capture never blocks inference.

Dependencies (injected via ``init()``):
- ``_get_request_auth_context`` — the auth context extractor from server.py
- ``_coerce_usage_int`` — the usage int coercer from server.py
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

from fastapi import Request

from app.capture.config import (
    DEFAULT_CORRELATION_HEADERS,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OLLAMA,
    PROTOCOL_OPENAI,
)
from app.capture.integration import get_capture_controller
from app.capture.schema import BuildContext
from app.capture.stream_assembler import StreamResponseAssembler

logger = logging.getLogger("Guardian")

# ── Caller-origin extraction (C5 app origin / C6 correlation) ────────

#: Maximum stored length for caller-supplied identity header values.
CALLER_IDENTITY_MAX_LEN = 256

#: OpenRouter app-attribution headers captured as ``app_title``/``app_referer``
#: when the inbound client actually sent them (never fabricated).
APP_TITLE_HEADER = "x-title"
APP_REFERER_HEADER = "http-referer"


def _capped_header_value(request: Request, header_name: str) -> str | None:
    """Return a stripped, length-capped inbound header value (or None)."""
    try:
        raw = request.headers.get(header_name)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    return value[:CALLER_IDENTITY_MAX_LEN]


def _caller_request_id(request: Request, config: Any) -> str | None:
    """First match (in config order) among the configured correlation headers.

    Only headers listed in ``config.correlation_headers`` are ever read.  The
    config value is authoritative at this layer: an explicitly empty list is
    the operator opt-out established by the config loader and must disable
    the echo here too — the default is restored only when the config object
    itself is unusable (review finding: ``if not headers`` defeated the
    opt-out by silently restoring the default).
    """
    try:
        headers = config.correlation_headers
    except Exception:
        headers = DEFAULT_CORRELATION_HEADERS
    if headers is None:
        headers = DEFAULT_CORRELATION_HEADERS
    for header_name in headers:
        value = _capped_header_value(request, str(header_name))
        if value:
            return value
    return None


# ── Injected helpers ─────────────────────────────────────────────────
# These are set by ``init()`` at startup.

_get_request_auth_context = None  # callable(request) -> dict | None
_coerce_usage_int = None  # callable(value) -> int


def init(get_request_auth_context, coerce_usage_int) -> None:
    """Inject helper callables.  Called once at startup."""
    global _get_request_auth_context, _coerce_usage_int
    _get_request_auth_context = get_request_auth_context
    _coerce_usage_int = coerce_usage_int


# ── Client fingerprint ──────────────────────────────────────────────


def capture_client_fingerprint(request: Request, client_id: str) -> str | None:
    """Extract the key fingerprint from the request's auth context for capture."""
    try:
        auth_context = _get_request_auth_context(request) or {}
        fingerprint = auth_context.get("key_fingerprint")
        if isinstance(fingerprint, str) and fingerprint.strip():
            return fingerprint.strip()
    except Exception:
        pass
    return None


# ── Protocol / endpoint helpers ─────────────────────────────────────


def capture_ingress_protocol(path: str, endpoint: str) -> str:
    """Determine the ingress protocol for capture based on the route."""
    if endpoint.startswith("/v1/"):
        if path == "messages" or endpoint == "/v1/messages":
            return PROTOCOL_ANTHROPIC
        return PROTOCOL_OPENAI
    elif endpoint.startswith("/api/chat"):
        return PROTOCOL_OLLAMA
    return PROTOCOL_OPENAI


def capture_endpoint_from_request(request: Request) -> str:
    """Extract the canonical endpoint path from a request."""
    url_path = request.url.path if hasattr(request, "url") else ""
    if "/v1/" in url_path:
        return "/v1/" + url_path.split("/v1/", 1)[-1]
    return url_path or ""


# ── Dispatch wrappers ───────────────────────────────────────────────


def dispatch_capture_request_received(
    request: Request,
    client_id: str,
    *,
    request_id: str,
    endpoint: str,
    ingress_protocol: str,
    route_type: str,
    requested_model: str | None,
    resolved_model: str | None = None,
    request_messages: list[dict[str, Any]] | None = None,
    request_parameters: dict[str, Any] | None = None,
    queue_wait_ms: float | None = None,
) -> Any | None:
    """Dispatch a request_received capture event (fail-open).

    Returns the PolicyResult so the caller can skip completed-event capture
    when the request was not captured.
    """
    try:
        controller = get_capture_controller()
        client_fingerprint = capture_client_fingerprint(request, client_id)
        # Grammar-Constrained Decoding presence flags (content is never
        # stored — only whether a grammar/schema was requested).
        params = request_parameters if isinstance(request_parameters, dict) else {}
        # Ollama clients carry grammar/schema under ``options.format`` (not at the
        # top level). Honor it for the presence flag — the content itself is
        # stripped by `redact_request_parameters` before storage.
        options_fmt = params.get("options")
        options_dict = options_fmt if isinstance(options_fmt, dict) else {}
        grammar_present = bool(
            "grammar" in params
            or "json_schema" in params
            or "response_format" in params
            or "format" in options_dict
        )
        response_format_present = bool("response_format" in params)
        # Caller correlation + app origin (C5/C6): read ONLY the configured
        # correlation headers plus the fixed app-attribution headers.  Values
        # are length-capped; absent headers leave the fields absent.
        caller_request_id = _caller_request_id(request, controller.config)
        app_title = _capped_header_value(request, APP_TITLE_HEADER)
        app_referer = _capped_header_value(request, APP_REFERER_HEADER)
        return controller.maybe_capture_request_received(
            request_id=request_id,
            client_fingerprint=client_fingerprint,
            endpoint=endpoint,
            ingress_protocol=ingress_protocol,
            route_type=route_type,
            requested_model=requested_model,
            resolved_model=resolved_model,
            request_messages=request_messages,
            request_parameters=request_parameters,
            queue_wait_ms=queue_wait_ms,
            grammar_present=grammar_present,
            response_format_present=response_format_present,
            caller_request_id=caller_request_id,
            app_title=app_title,
            app_referer=app_referer,
        )
    except Exception:
        return None


def dispatch_capture_request_completed(
    ctx: BuildContext,
    *,
    policy_result: Any | None = None,
    response_content: str | None = None,
    tool_calls: list | None = None,
    tool_results: list | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    native_finish_reason: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    completion_tokens_details: dict[str, Any] | None = None,
    native_tokens_reasoning: int | None = None,
    native_tokens_cached: int | None = None,
    cost: float | None = None,
    provider_name: str | None = None,
    queue_wait_ms: float | None = None,
    duration_ms: float | None = None,
    http_status: int | None = None,
    streamed: bool | None = None,
    streamed_ingress: bool | None = None,
    streamed_upstream: bool | None = None,
    incomplete: bool | None = None,
    attempts: int | None = None,
) -> None:
    """Dispatch a request_completed capture event (fail-open)."""
    try:
        controller = get_capture_controller()
        controller.capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=response_content,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            native_finish_reason=native_finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            completion_tokens_details=completion_tokens_details,
            native_tokens_reasoning=native_tokens_reasoning,
            native_tokens_cached=native_tokens_cached,
            cost=cost,
            provider_name=provider_name,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            http_status=http_status,
            streamed=streamed,
            streamed_ingress=streamed_ingress,
            streamed_upstream=streamed_upstream,
            incomplete=incomplete,
            attempts=attempts,
        )
    except Exception:
        pass


def dispatch_capture_request_failed(
    ctx: BuildContext,
    *,
    error_code: str,
    http_status: int | None = None,
    sanitized_message: str | None = None,
    queue_wait_ms: float | None = None,
    duration_ms: float | None = None,
    attempts: int | None = None,
    policy_result: Any | None = None,
) -> None:
    """Dispatch a request_failed capture event (fail-open)."""
    try:
        controller = get_capture_controller()
        controller.capture_request_failed(
            ctx,
            error_code=error_code,
            http_status=http_status,
            sanitized_message=sanitized_message,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            attempts=attempts,
        )
    except Exception:
        pass


def dispatch_capture_request_cancelled(
    ctx: BuildContext,
    *,
    cancel_reason: str,
    queue_wait_ms: float | None = None,
    duration_ms: float | None = None,
    attempts: int | None = None,
    policy_result: Any | None = None,
) -> None:
    """Dispatch a request_cancelled capture event (fail-open)."""
    try:
        controller = get_capture_controller()
        controller.capture_request_cancelled(
            ctx,
            cancel_reason=cancel_reason,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            attempts=attempts,
        )
    except Exception:
        pass


def dispatch_capture_stream_completed(
    request: Request,
    request_id: str,
    client_id: str,
    model_name: str,
    ctx: BuildContext | None,
    policy_result: Any | None,
    assembler: StreamResponseAssembler | None,
    usage_totals: dict[str, Any],
    path: str,
    status_code: int,
) -> None:
    """Dispatch request_completed for the streaming path (fail-open)."""
    if ctx is None or policy_result is None or not policy_result.should_capture:
        return
    try:
        assembled = assembler.assemble() if assembler is not None else {"content": None}
        dispatch_capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=assembled.get("content"),
            tool_calls=assembled.get("tool_calls"),
            reasoning_content=assembled.get("reasoning_content"),
            finish_reason=assembled.get("finish_reason"),
            native_finish_reason=assembled.get("native_finish_reason"),
            completion_tokens_details=assembled.get("completion_tokens_details"),
            native_tokens_reasoning=assembled.get("native_tokens_reasoning"),
            native_tokens_cached=assembled.get("native_tokens_cached"),
            cost=assembled.get("cost"),
            provider_name=assembled.get("provider_name"),
            prompt_tokens=usage_totals.get("prompt_tokens") or None,
            completion_tokens=usage_totals.get("completion_tokens") or None,
            http_status=status_code,
            streamed=True,
            # Local streaming: the upstream leg streamed because the client
            # requested streaming (this wrapper only runs on stream branches).
            streamed_ingress=True,
            streamed_upstream=True,
            incomplete=assembled.get("incomplete"),
        )
    except Exception:
        pass


def dispatch_capture_nonstream_completed(
    request: Request,
    request_id: str,
    client_id: str,
    model_name: str,
    ctx: BuildContext | None,
    policy_result: Any | None,
    payload: dict[str, Any] | None,
    status_code: int,
    request_start_time: float,
) -> None:
    """Dispatch request_completed for the non-streaming path (fail-open)."""
    if ctx is None or policy_result is None or not policy_result.should_capture:
        return
    try:
        response_content = None
        finish_reason = None
        native_finish_reason = None
        prompt_tokens = None
        completion_tokens = None
        tool_calls = None
        captured_reasoning = None
        completion_tokens_details = None
        native_tokens_reasoning = None
        native_tokens_cached = None
        cost = None
        provider_name = None

        def _accept_usage_mirror(usage: Any) -> None:
            """Fill the rich usage mirror fields from an upstream usage dict."""
            nonlocal completion_tokens_details, native_tokens_reasoning
            nonlocal native_tokens_cached, cost
            if not isinstance(usage, dict):
                return
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict) and details:
                completion_tokens_details = details
            # math.isfinite: 1e999-style upstream numbers parse to inf;
            # int(inf) raises OverflowError which the outer fail-open except
            # would turn into a silently DROPPED completed event, and
            # float(inf) would serialize as bare Infinity.
            ntr = usage.get("native_tokens_reasoning")
            if (isinstance(ntr, (int, float)) and not isinstance(ntr, bool)
                    and math.isfinite(ntr)):
                native_tokens_reasoning = int(ntr)
            ntc = usage.get("native_tokens_cached")
            if (isinstance(ntc, (int, float)) and not isinstance(ntc, bool)
                    and math.isfinite(ntc)):
                native_tokens_cached = int(ntc)
            cost_val = usage.get("cost")
            if (isinstance(cost_val, (int, float)) and not isinstance(cost_val, bool)
                    and math.isfinite(cost_val)):
                cost = float(cost_val)

        if isinstance(payload, dict):
            # Provider-reported serving provider slug (OpenRouter shape).
            reported_provider = payload.get("provider")
            if isinstance(reported_provider, str) and reported_provider:
                provider_name = reported_provider
            # Check if this is an Anthropic-style response (has 'content' array, not 'choices')
            if "choices" not in payload and "content" in payload:
                # Anthropic /v1/messages response format
                response_content_parts: list[str] = []
                content_blocks = payload.get("content", [])
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict):
                            block_type = block.get("type", "")
                            if block_type == "text":
                                text = block.get("text", "")
                                if isinstance(text, str) and text:
                                    response_content_parts.append(text)
                            elif block_type == "tool_use":
                                if tool_calls is None:
                                    tool_calls = []
                                tool_calls.append({
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", ""),
                                        "arguments": json.dumps(block.get("input", {})) if isinstance(block.get("input"), dict) else str(block.get("input", "")),
                                    },
                                })
                if response_content_parts:
                    response_content = "\n".join(response_content_parts)
                finish_reason = payload.get("stop_reason")

                usage = payload.get("usage", {})
                if isinstance(usage, dict):
                    prompt_tokens = _coerce_usage_int(usage.get("input_tokens", 0))
                    completion_tokens = _coerce_usage_int(usage.get("output_tokens", 0))
                    _accept_usage_mirror(usage)
            else:
                # OpenAI chat/completions response format
                choices = payload.get("choices", [])
                if isinstance(choices, list) and choices:
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    message = first.get("message", first)
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            response_content = content
                        # finish_reason lives on choices[0] for OpenAI
                        # responses; fall back to message-level for tolerant
                        # clients that put it there (non-str values ignored).
                        choice_finish = first.get("finish_reason")
                        if isinstance(choice_finish, str) and choice_finish:
                            finish_reason = choice_finish
                        else:
                            message_finish = message.get("finish_reason")
                            if isinstance(message_finish, str) and message_finish:
                                finish_reason = message_finish
                        # Provider-reported native stop reason (OpenRouter
                        # shape: choices[0].native_finish_reason).
                        choice_native = first.get("native_finish_reason")
                        if isinstance(choice_native, str) and choice_native:
                            native_finish_reason = choice_native
                        else:
                            message_native = message.get("native_finish_reason")
                            if isinstance(message_native, str) and message_native:
                                native_finish_reason = message_native
                        tc = message.get("tool_calls")
                        if isinstance(tc, list):
                            tool_calls = tc
                        # Reasoning is captured separately from content —
                        # some providers send reasoning_content, others
                        # (OpenRouter-style) send reasoning.
                        reasoning = message.get("reasoning_content")
                        if not isinstance(reasoning, str) or not reasoning:
                            reasoning = message.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            captured_reasoning = reasoning
                    delta = first.get("delta", {})
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str) and not response_content:
                            response_content = content

                usage = payload.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens = _coerce_usage_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
                    completion_tokens = _coerce_usage_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
                    _accept_usage_mirror(usage)

        dispatch_capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=response_content,
            finish_reason=finish_reason,
            native_finish_reason=native_finish_reason,
            reasoning_content=captured_reasoning,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            completion_tokens_details=completion_tokens_details,
            native_tokens_reasoning=native_tokens_reasoning,
            native_tokens_cached=native_tokens_cached,
            cost=cost,
            provider_name=provider_name,
            tool_calls=tool_calls,
            http_status=status_code,
            streamed=False,
            # Local non-streaming: neither leg streamed.
            streamed_ingress=False,
            streamed_upstream=False,
            incomplete=(finish_reason is None or finish_reason == "null"),
            duration_ms=(time.monotonic() - request_start_time) * 1000,
        )
    except Exception:
        pass


# ── Error classification ────────────────────────────────────────────


def classify_capture_error(exc: Exception) -> str:
    """Map an exception to a stable capture error code (never leaks internals)."""
    exc_name = type(exc).__name__
    mapping = {
        "ConnectError": "connection_error",
        "ConnectTimeout": "connection_timeout",
        "ReadTimeout": "read_timeout",
        "WriteTimeout": "write_timeout",
        "PoolTimeout": "pool_timeout",
        "TimeoutException": "timeout",
        "HTTPStatusError": "http_error",
        "ModelLoadError": "model_load_error",
        "HTTPException": "http_exception",
    }
    return mapping.get(exc_name, "internal_error")


def sanitize_capture_error_message(exc: Exception) -> str:
    """Produce a sanitized error message for capture (no credentials/paths)."""
    exc_name = type(exc).__name__
    return f"{exc_name}: request to backend failed"
