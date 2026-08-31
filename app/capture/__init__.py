"""Guardian capture subsystem — privacy-aware, non-blocking capture boundary.

This package implements the guardian_capture_v1 schema and transport for
recording eligible Guardian request/response events to append-only JSONL files
on the local filesystem.  Keanu Factory consumes the completed, rotated files
as its Bronze dataset source.

Design invariants (never violated):
- Capture is disabled by default (opt-in).
- Capture failure never blocks or changes inference output (fail-open).
- Authorization headers and raw API keys are never persisted.
- Raw client IP addresses are never persisted in dataset events.
- Admin/health/metrics/embedding/key-management endpoints are excluded.
"""

from app.capture.config import CaptureConfig, load_capture_config
from app.capture.policy import PolicyResult, evaluate_capture_policy
from app.capture.redactor import (
    anthropic_messages_to_openai,
    redact_image_blocks,
    redact_reasoning_content,
    redact_request_messages,
    redact_request_parameters,
    redact_response_content,
    redact_tool_calls,
    redact_tool_results,
    scan_for_secrets,
)
from app.capture.schema import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
    BuildContext,
    build_request_cancelled_event,
    build_request_completed_event,
    build_request_failed_event,
    build_request_received_event,
    compute_client_ref,
    compute_event_id,
)
from app.capture.sink import CaptureEvent, CaptureSink
from app.capture.stream_assembler import StreamResponseAssembler
from app.capture.wal_writer import CaptureWALWriter

__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "BuildContext",
    "CaptureConfig",
    "CaptureEvent",
    "CaptureSink",
    "CaptureWALWriter",
    "PolicyResult",
    "StreamResponseAssembler",
    "anthropic_messages_to_openai",
    "build_request_cancelled_event",
    "build_request_completed_event",
    "build_request_failed_event",
    "build_request_received_event",
    "compute_client_ref",
    "compute_event_id",
    "evaluate_capture_policy",
    "load_capture_config",
    "redact_image_blocks",
    "redact_reasoning_content",
    "redact_request_messages",
    "redact_request_parameters",
    "redact_response_content",
    "redact_tool_calls",
    "redact_tool_results",
    "scan_for_secrets",
]
