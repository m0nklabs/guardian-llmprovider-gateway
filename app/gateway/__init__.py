"""Gateway package — shared helpers extracted from the monolithic server.py.

Phase 5 (Structural Separation) extracts cross-cutting concerns into
focused modules so that the FastAPI router in ``app.proxy.server`` becomes
a thin presentation layer.
"""

from app.gateway.context_metadata import (
    apply_context_metadata,
    get_loaded_backend_context_window,
    resolve_context_window,
    warn_context_fallback,
    enrich_model_context_metadata,
    build_model_metadata_entry,
    init as init_context_metadata,
)

from app.gateway.capture_dispatch import (
    capture_client_fingerprint,
    capture_ingress_protocol,
    capture_endpoint_from_request,
    dispatch_capture_request_received,
    dispatch_capture_request_completed,
    dispatch_capture_request_failed,
    dispatch_capture_request_cancelled,
    dispatch_capture_stream_completed,
    dispatch_capture_nonstream_completed,
    classify_capture_error,
    sanitize_capture_error_message,
    init as init_capture_dispatch,
)

from app.gateway.streaming import (
    extract_assistant_message_text,
    extract_assistant_delta_text,
    normalize_stream_progress_text,
    extract_stream_progress_text,
    StreamProgressWatchdog,
    build_stream_timeout,
    build_sse_keepalive_comment,
    enrich_anthropic_sse_line,
    enrich_anthropic_response,
    iter_sse_lines_with_watchdog,
    init as init_streaming,
)

__all__ = [
    # context metadata
    "apply_context_metadata",
    "get_loaded_backend_context_window",
    "resolve_context_window",
    "warn_context_fallback",
    "enrich_model_context_metadata",
    "build_model_metadata_entry",
    "init_context_metadata",
    # capture dispatch
    "capture_client_fingerprint",
    "capture_ingress_protocol",
    "capture_endpoint_from_request",
    "dispatch_capture_request_received",
    "dispatch_capture_request_completed",
    "dispatch_capture_request_failed",
    "dispatch_capture_request_cancelled",
    "dispatch_capture_stream_completed",
    "dispatch_capture_nonstream_completed",
    "classify_capture_error",
    "sanitize_capture_error_message",
    "init_capture_dispatch",
    # streaming
    "extract_assistant_message_text",
    "extract_assistant_delta_text",
    "normalize_stream_progress_text",
    "extract_stream_progress_text",
    "StreamProgressWatchdog",
    "build_stream_timeout",
    "build_sse_keepalive_comment",
    "enrich_anthropic_sse_line",
    "enrich_anthropic_response",
    "iter_sse_lines_with_watchdog",
    "init_streaming",
]
