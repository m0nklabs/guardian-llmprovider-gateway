"""API usage tracking — live dashboard request lifecycle + token accounting.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Tracks in-flight requests for the dashboard, folds finished requests into
history, and records token usage from response payloads. The single injected
dependency is the server ``State`` object (its ``api_usage`` tracker).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from app.proxy.auth import build_request_auth_context, get_request_auth_context, set_request_auth_context

logger = None  # reserved; no logging currently

# ── Injected (set once at startup by init()) ─────────────────────────
_state = None


def init(state) -> None:
    """Inject the server State object (holds the ApiUsageTracker)."""
    global _state
    _state = state


def _api_usage():
    """Return the live usage tracker, never None at runtime."""
    return _state.api_usage


def coerce_usage_int(value: object) -> int:
    """Convert token usage values to non-negative integers."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def coerce_header_int(value: object) -> int:
    """Convert a header-like byte count to a non-negative integer."""
    try:
        return max(int(str(value).strip()), 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def request_size_bytes(request: Request) -> int:
    """Best-effort byte count for the inbound request body."""
    return coerce_header_int(request.headers.get("content-length"))


def response_size_bytes(response: Response) -> int:
    """Best-effort byte count for the outbound response body."""
    header_value = response.headers.get("content-length")
    if header_value not in (None, ""):
        return coerce_header_int(header_value)
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    return 0


def should_track_api_usage(path: str) -> bool:
    """Return whether the request path should count toward API usage."""
    if path in {"/healthz", "/metrics"}:
        return False
    return path.startswith("/api/") or path.startswith("/v1/") or path.startswith("/admin/")


def get_usage_client_id(request: Request) -> Optional[str]:
    """Extract the authenticated client name attached by auth."""
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        name = user.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    auth_context = getattr(request.state, "auth_context", None)
    if isinstance(auth_context, dict):
        name = auth_context.get("client_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def get_usage_attribution(request: Request) -> Optional[Dict[str, Any]]:
    """Return request attribution details collected during auth."""
    auth_context = get_request_auth_context(request)
    if isinstance(auth_context, dict):
        return auth_context
    return build_request_auth_context(request)


def get_live_usage_request_id(request: Request) -> Optional[str]:
    """Return the dashboard request id bound to the current FastAPI request."""
    state_obj = getattr(request, "state", None)
    if state_obj is None:
        return None
    request_id = getattr(state_obj, "guardian_usage_request_id", None)
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    return None


def start_live_request_usage(request: Request) -> None:
    """Register the current API request as in-flight for dashboard polling."""
    if not isinstance(get_request_auth_context(request), dict):
        set_request_auth_context(request, build_request_auth_context(request))
    live_request_id = str(uuid.uuid4())
    request.state.guardian_usage_request_id = live_request_id
    request.state.guardian_usage_started_monotonic = time.monotonic()
    _state.api_usage.start_request(
        request_id=live_request_id,
        client_id=get_usage_client_id(request),
        endpoint=request.url.path,
        method=request.method,
        model=getattr(request.state, "guardian_usage_model", None),
        request_bytes=request_size_bytes(request),
        streamed=bool(getattr(request.state, "guardian_usage_streamed", False)),
        attribution=get_usage_attribution(request),
    )


def update_live_request_usage(
    request: Request,
    *,
    model: Optional[str] = None,
    streamed: Optional[bool] = None,
    queue_request_id: Optional[str] = None,
    phase: Optional[str] = None,
    queue_wait_ms: Optional[float] = None,
    prompt_tokens: Optional[object] = None,
    completion_tokens: Optional[object] = None,
    output_chars_delta: object = 0,
    response_bytes_delta: object = 0,
) -> None:
    """Push incremental request metadata into the live dashboard tracker."""
    live_request_id = get_live_usage_request_id(request)
    if live_request_id is None:
        return
    _state.api_usage.update_active_request(
        request_id=live_request_id,
        model=model,
        streamed=streamed,
        queue_request_id=queue_request_id,
        phase=phase,
        queue_wait_ms=queue_wait_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        output_chars_delta=output_chars_delta,
        response_bytes_delta=response_bytes_delta,
    )


def finish_live_request_usage(
    request: Request,
    *,
    status_code: int,
    response_bytes: Optional[int] = None,
) -> None:
    """Finalize the live dashboard request entry and fold it into history."""
    live_request_id = get_live_usage_request_id(request)
    if live_request_id is None or getattr(request.state, "guardian_usage_finished", False):
        return
    started = getattr(request.state, "guardian_usage_started_monotonic", None)
    duration_ms = None
    if isinstance(started, (int, float)):
        duration_ms = max((time.monotonic() - float(started)) * 1000.0, 0.0)
    _state.api_usage.finish_request(
        request_id=live_request_id,
        client_id=get_usage_client_id(request),
        endpoint=request.url.path,
        method=request.method,
        status_code=status_code,
        model=getattr(request.state, "guardian_usage_model", None),
        duration_ms=duration_ms,
        request_bytes=request_size_bytes(request),
        response_bytes=response_bytes,
        streamed=bool(getattr(request.state, "guardian_usage_streamed", False)),
        attribution=get_usage_attribution(request),
    )
    request.state.guardian_usage_finished = True


def set_request_usage_metadata(
    request: Request,
    *,
    model: Optional[str] = None,
    streamed: Optional[bool] = None,
) -> None:
    """Attach request metadata for dashboard usage snapshots."""
    if model is not None:
        request.state.guardian_usage_model = model
    if streamed is not None:
        request.state.guardian_usage_streamed = streamed
    update_live_request_usage(request, model=model, streamed=streamed)


def record_request_token_usage(
    client_id: Optional[str],
    endpoint: str,
    model: Optional[str],
    *,
    request: Optional[Request] = None,
    attribution: Optional[Dict[str, Any]] = None,
    prompt_tokens: object = 0,
    completion_tokens: object = 0,
) -> None:
    """Store token usage for a completed request when available."""
    resolved_attribution = attribution
    if resolved_attribution is None and request is not None:
        resolved_attribution = get_usage_attribution(request)
    _state.api_usage.record_tokens(
        client_id=client_id,
        endpoint=endpoint,
        model=model,
        prompt_tokens=coerce_usage_int(prompt_tokens),
        completion_tokens=coerce_usage_int(completion_tokens),
        attribution=resolved_attribution,
    )


def record_usage_from_payload(
    client_id: Optional[str],
    endpoint: str,
    model: Optional[str],
    payload: Optional[Dict[str, Any]],
    *,
    request: Optional[Request] = None,
    attribution: Optional[Dict[str, Any]] = None,
) -> None:
    """Extract OpenAI-style usage fields from a JSON payload."""
    if not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    record_request_token_usage(
        client_id,
        endpoint,
        model,
        request=request,
        attribution=attribution,
        prompt_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        completion_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
    )






async def track_api_usage(request: Request, call_next):
    """Track aggregate API usage for dashboard monitoring."""
    path = request.url.path
    if not should_track_api_usage(path):
        return await call_next(request)

    start_live_request_usage(request)
    try:
        response = await call_next(request)
    except Exception:
        finish_live_request_usage(request, status_code=500, response_bytes=0)
        raise

    is_streaming_response = bool(getattr(request.state, "guardian_usage_streamed", False)) and isinstance(response, StreamingResponse)
    if not is_streaming_response:
        finish_live_request_usage(
            request,
            status_code=response.status_code,
            response_bytes=response_size_bytes(response),
        )
    return response
