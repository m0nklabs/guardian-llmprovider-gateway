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
import time
from typing import Any, Dict, List, Optional

from fastapi import Request

from app.capture.config import PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC, PROTOCOL_OLLAMA
from app.capture.integration import get_capture_controller
from app.capture.schema import BuildContext
from app.capture.stream_assembler import StreamResponseAssembler

logger = logging.getLogger("Guardian")

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


def capture_client_fingerprint(request: Request, client_id: str) -> Optional[str]:
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
    requested_model: Optional[str],
    resolved_model: Optional[str] = None,
    request_messages: Optional[List[Dict[str, Any]]] = None,
    request_parameters: Optional[Dict[str, Any]] = None,
    queue_wait_ms: Optional[float] = None,
) -> Optional[Any]:
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
        )
    except Exception:
        return None


def dispatch_capture_request_completed(
    ctx: BuildContext,
    *,
    policy_result: Optional[Any] = None,
    response_content: Optional[str] = None,
    tool_calls: Optional[list] = None,
    tool_results: Optional[list] = None,
    reasoning_content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    http_status: Optional[int] = None,
    streamed: Optional[bool] = None,
    incomplete: Optional[bool] = None,
    attempts: Optional[int] = None,
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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            http_status=http_status,
            streamed=streamed,
            incomplete=incomplete,
            attempts=attempts,
        )
    except Exception:
        pass


def dispatch_capture_request_failed(
    ctx: BuildContext,
    *,
    error_code: str,
    http_status: Optional[int] = None,
    sanitized_message: Optional[str] = None,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    policy_result: Optional[Any] = None,
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
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    policy_result: Optional[Any] = None,
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
    ctx: Optional[BuildContext],
    policy_result: Optional[Any],
    assembler: Optional[StreamResponseAssembler],
    usage_totals: Dict[str, Any],
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
            finish_reason=assembled.get("finish_reason"),
            prompt_tokens=usage_totals.get("prompt_tokens") or None,
            completion_tokens=usage_totals.get("completion_tokens") or None,
            http_status=status_code,
            streamed=True,
            incomplete=assembled.get("incomplete"),
        )
    except Exception:
        pass


def dispatch_capture_nonstream_completed(
    request: Request,
    request_id: str,
    client_id: str,
    model_name: str,
    ctx: Optional[BuildContext],
    policy_result: Optional[Any],
    payload: Optional[Dict[str, Any]],
    status_code: int,
    request_start_time: float,
) -> None:
    """Dispatch request_completed for the non-streaming path (fail-open)."""
    if ctx is None or policy_result is None or not policy_result.should_capture:
        return
    try:
        response_content = None
        finish_reason = None
        prompt_tokens = None
        completion_tokens = None
        tool_calls = None

        if isinstance(payload, dict):
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
                        finish_reason = message.get("finish_reason")
                        tc = message.get("tool_calls")
                        if isinstance(tc, list):
                            tool_calls = tc
                    delta = first.get("delta", {})
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str) and not response_content:
                            response_content = content

                usage = payload.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens = _coerce_usage_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
                    completion_tokens = _coerce_usage_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))

        dispatch_capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=response_content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_calls=tool_calls,
            http_status=status_code,
            streamed=False,
            incomplete=False,
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
