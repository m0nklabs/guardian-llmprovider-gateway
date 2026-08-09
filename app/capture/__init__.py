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
from app.capture.schema import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
    compute_event_id,
    compute_client_ref,
    BuildContext,
    build_request_received_event,
    build_request_completed_event,
    build_request_failed_event,
    build_request_cancelled_event,
)
from app.capture.policy import PolicyResult, evaluate_capture_policy
from app.capture.redactor import (
    redact_request_messages,
    redact_response_content,
    redact_request_parameters,
    redact_reasoning_content,
    redact_tool_results,
    redact_tool_calls,
    redact_image_blocks,
    anthropic_messages_to_openai,
    scan_for_secrets,
)
from app.capture.stream_assembler import StreamResponseAssembler
from app.capture.sink import CaptureSink, CaptureEvent
from app.capture.wal_writer import CaptureWALWriter

__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "CaptureConfig",
    "load_capture_config",
    "PolicyResult",
    "evaluate_capture_policy",
    "compute_event_id",
    "compute_client_ref",
    "BuildContext",
    "build_request_received_event",
    "build_request_completed_event",
    "build_request_failed_event",
    "build_request_cancelled_event",
    "redact_request_messages",
    "redact_response_content",
    "redact_request_parameters",
    "redact_reasoning_content",
    "redact_tool_results",
    "redact_tool_calls",
    "redact_image_blocks",
    "anthropic_messages_to_openai",
    "StreamResponseAssembler",
    "CaptureSink",
    "CaptureEvent",
    "CaptureWALWriter",
    "scan_for_secrets",
]
