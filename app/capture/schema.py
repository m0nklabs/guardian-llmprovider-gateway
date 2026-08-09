"""guardian_capture_v1 event schema, deterministic IDs, and builders.

Every captured event is a single JSON object written as one line in a JSONL
file.  The schema is versioned: ``schema_name`` carries the wire-contract major
version (``guardian_capture_v1``), and ``schema_version`` carries the
minor/patch level within that contract.

Identity rules (see GUARDIAN_KEANU_CAPTURE_PLAN.json §capture_contract.identity):
- ``event_id`` = lowercase hex SHA-256 of ``{instance_id}|{request_id}|{event_type}|{sequence}``
- ``client_ref`` = lowercase hex HMAC-SHA-256 using ``GUARDIAN_CAPTURE_CLIENT_REF_SECRET``
  as key, and the authenticated Guardian key fingerprint as message
- ``request_id`` = the existing Guardian request ID (UUID from inference queue)
- ``sequence`` = monotonically increasing integer within one request
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from app.capture.config import CaptureConfig, PROTOCOL_OPENAI, ROUTE_LOCAL, ROUTE_CLOUD

logger = logging.getLogger("Guardian.Capture.Schema")

#: Wire-contract identifier — readers reject unknown schema_name values.
SCHEMA_NAME = "guardian_capture_v1"

#: Semantic version within the v1 contract.  Minor/patch bumps for additive
#: field changes; a major change requires renaming schema_name (e.g. v2).
SCHEMA_VERSION = "1.0.0"

#: Delimiter used in event_id computation — must not appear in any component.
EVENT_ID_DELIMITER = "|"

#: Environment variable holding the current HMAC key for client_ref computation.
CLIENT_REF_SECRET_ENV = "GUARDIAN_CAPTURE_CLIENT_REF_SECRET"

#: Environment variable holding one or more legacy HMAC keys for rotation overlap.
#: Comma-separated list of legacy secrets.  When set, client_ref values computed
#: with either the current or any legacy secret are accepted for opt-in matching.
CLIENT_REF_PREVIOUS_SECRETS_ENV = "GUARDIAN_CAPTURE_CLIENT_REF_SECRET_PREVIOUS"

#: Environment variable holding the HMAC key for per-record authentication.
#: If unset, per-record HMAC is not added to WAL lines.
RECORD_AUTH_SECRET_ENV = "GUARDIAN_CAPTURE_RECORD_AUTH_SECRET"


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def compute_event_id(
    instance_id: str,
    request_id: str,
    event_type: str,
    sequence: int,
) -> str:
    """Deterministic event ID: SHA-256 hex of the delimited component string.

    Components must not contain the ``|`` delimiter character.
    """
    # Guard against delimiter contamination — if any component contains '|',
    # we hex-escape it first so the delimiter is unambiguous.
    def _safe(component: str) -> str:
        if EVENT_ID_DELIMITER in component:
            return component.replace(EVENT_ID_DELIMITER, "\\x7c")
        return component

    raw = EVENT_ID_DELIMITER.join(
        _safe(str(v)) for v in (instance_id, request_id, event_type, sequence)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_client_ref(
    key_fingerprint: Optional[str],
    *,
    allowed_refs: Optional[List[str]] = None,
) -> Optional[str]:
    """Compute a privacy-safe HMAC-SHA-256 client identifier.

    Uses ``GUARDIAN_CAPTURE_CLIENT_REF_SECRET`` from the environment as the
    HMAC key.  Returns ``None`` when there is no current secret or the
    fingerprint is missing — callers must treat ``None`` client_ref as
    "do not capture".

    Multi-secret rotation support:
    ``GUARDIAN_CAPTURE_CLIENT_REF_SECRET_PREVIOUS`` may contain one or more
    comma-separated legacy secrets.  When ``allowed_refs`` is provided
    (the opt-in list), this function computes the HMAC with each secret in
    turn (current first, then legacy) and returns the first hash that
    matches an entry in ``allowed_refs``.  This preserves existing opt-in
    continuity during secret rotation — old opt-in entries keep working
    without re-registration until the operator migrates them.

    When ``allowed_refs`` is ``None`` or empty, always returns the hash
    computed with the current secret (for first-registration use cases).
    """
    if not key_fingerprint:
        return None

    current = os.environ.get(CLIENT_REF_SECRET_ENV, "")
    previous_raw = os.environ.get(CLIENT_REF_PREVIOUS_SECRETS_ENV, "")

    # Build ordered list of secrets to try (current first, then legacy).
    secrets: List[str] = []
    if current:
        secrets.append(current)
    if previous_raw:
        for s in previous_raw.split(","):
            s = s.strip()
            if s and s not in secrets:
                secrets.append(s)

    if not secrets:
        return None

    # Without an allowlist to match against, return the current-secret hash.
    if not allowed_refs:
        return hmac.new(
            secrets[0].encode("utf-8"),
            key_fingerprint.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # With an allowlist, try each secret and return the first matching hash.
    allowed_set = set(allowed_refs)
    for secret in secrets:
        ref = hmac.new(
            secret.encode("utf-8"),
            key_fingerprint.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if ref in allowed_set:
            return ref

    # No match — return the current-secret hash so a new client gets a
    # stable identifier the operator can add to the allowlist.
    return hmac.new(
        secrets[0].encode("utf-8"),
        key_fingerprint.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def compute_record_auth(record_line: str) -> Optional[Dict[str, str]]:
    """Compute per-record HMAC for a WAL JSONL line.

    Returns a dict ``{"alg": "hmac-sha256", "key_id": "<16-char hex prefix>",
    "mac": "<hex digest>"}`` or ``None`` when the signing secret is unset.

    The MAC is computed over the raw JSON string of the line *without* the
    ``record_auth`` field so Keanu can recompute by stripping ``record_auth``
    from the parsed JSON before serialising.
    """
    secret = os.environ.get(RECORD_AUTH_SECRET_ENV, "")
    if not secret:
        return None
    secret_bytes = secret.encode("utf-8")
    key_id = hashlib.sha256(secret_bytes).hexdigest()[:16]
    mac = hmac.new(secret_bytes, record_line.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"alg": "hmac-sha256", "key_id": key_id, "mac": mac}


def _serialize_for_output(value: Any) -> Any:
    """Ensure a value is JSON-serializable and does not contain non-serializable objects."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {str(k): _serialize_for_output(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_for_output(v) for v in value]
    # Fallback: stringify anything else
    return str(value)


def _build_base_event(
    config: CaptureConfig,
    request_id: str,
    event_type: str,
    sequence: int,
    *,
    client_fingerprint: Optional[str] = None,
    endpoint: str,
    ingress_protocol: str,
    route_type: str,
    requested_model: Optional[str] = None,
    resolved_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the common base fields present on every capture event."""
    client_ref = compute_client_ref(
        client_fingerprint,
        allowed_refs=config.allowed_client_refs if config.per_client_opt_in else None,
    ) if client_fingerprint else None

    event: Dict[str, Any] = {
        # Core schema identity
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "event_id": compute_event_id(config.instance_id, request_id, event_type, sequence),
        "event_type": event_type,
        "request_id": request_id,
        "sequence": sequence,
        "timestamp_utc": _utc_now_iso(),

        # Guardian identity
        "guardian_instance_id": config.instance_id,

        # Client identity (privacy-safe)
        "client_ref": client_ref,

        # Request context
        "endpoint": endpoint,
        "ingress_protocol": ingress_protocol,
        "route_type": route_type,
        "requested_model": requested_model,
        "resolved_model": resolved_model,

        # Policy metadata
        "capture_policy_version": config.policy_version,
    }
    return event


@dataclass
class BuildContext:
    """Context assembled from the request lifecycle for event construction.

    Carries the resolved metadata from authentication, routing, and inference
    so that each event builder has everything it needs without re-reading
    the request object.
    """

    # Required fields
    request_id: str
    endpoint: str
    ingress_protocol: str
    route_type: str
    requested_model: Optional[str]
    capture_policy_version: str
    instance_id: str

    # Identity
    client_fingerprint: Optional[str] = None

    # Optional resolved metadata (set as request progresses)
    resolved_model: Optional[str] = None
    upstream_model: Optional[str] = None
    provider: Optional[str] = None
    failover_group: Optional[str] = None
    attempts: Optional[int] = None

    # Timing
    request_received_ts: Optional[float] = None  # monotonic
    request_completed_ts: Optional[float] = None  # monotonic

    # Token usage
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    # Streaming metadata
    streamed: bool = False
    incomplete: bool = False

    # HTTP metadata
    http_status: Optional[int] = None

    # Error metadata
    error_code: Optional[str] = None

    # Content (already redacted)
    request_messages: Optional[List[Dict[str, Any]]] = None
    request_parameters: Optional[Dict[str, Any]] = None
    response_content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_results: Optional[List[Dict[str, Any]]] = None
    reasoning_content: Optional[str] = None
    finish_reason: Optional[str] = None

    def to_config(self) -> CaptureConfig:
        """Reconstruct a minimal CaptureConfig from known context."""
        return CaptureConfig(
            instance_id=self.instance_id,
            policy_version=self.capture_policy_version,
        )


def build_request_received_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    request_messages: Optional[List[Dict[str, Any]]] = None,
    request_parameters: Optional[Dict[str, Any]] = None,
    queue_wait_ms: Optional[float] = None,
    sequence: int = 0,
) -> Dict[str, Any]:
    """Build a ``request_received`` event — emitted after auth + normalization."""
    event = _build_base_event(
        config=config,
        request_id=ctx.request_id,
        event_type="request_received",
        sequence=sequence,
        client_fingerprint=ctx.client_fingerprint,
        endpoint=ctx.endpoint,
        ingress_protocol=ctx.ingress_protocol,
        route_type=ctx.route_type,
        requested_model=ctx.requested_model,
        resolved_model=ctx.resolved_model or ctx.requested_model,
    )
    event["resolved_model"] = ctx.resolved_model
    event["upstream_model"] = ctx.upstream_model
    event["provider"] = ctx.provider
    event["failover_group"] = ctx.failover_group
    if request_messages is not None:
        event["request_messages"] = _serialize_for_output(request_messages)
    if request_parameters is not None:
        event["request_parameters"] = _serialize_for_output(request_parameters)
    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    return event


def build_request_completed_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    response_content: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
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
    sequence: int = 1,
) -> Dict[str, Any]:
    """Build a ``request_completed`` event — emitted after successful response."""
    event = _build_base_event(
        config=config,
        request_id=ctx.request_id,
        event_type="request_completed",
        sequence=sequence,
        client_fingerprint=ctx.client_fingerprint,
        endpoint=ctx.endpoint,
        ingress_protocol=ctx.ingress_protocol,
        route_type=ctx.route_type,
        requested_model=ctx.requested_model,
        resolved_model=ctx.resolved_model,
    )
    event["upstream_model"] = ctx.upstream_model
    event["provider"] = ctx.provider
    event["failover_group"] = ctx.failover_group
    if response_content is not None:
        event["response_content"] = _serialize_for_output(response_content)
    if tool_calls is not None:
        event["tool_calls"] = _serialize_for_output(tool_calls)
    if tool_results is not None:
        event["tool_results"] = _serialize_for_output(tool_results)
    if reasoning_content is not None:
        event["reasoning_content"] = _serialize_for_output(reasoning_content)
    if finish_reason is not None:
        event["finish_reason"] = _serialize_for_output(finish_reason)
    event["http_status"] = http_status if http_status is not None else ctx.http_status

    # Token usage
    pt = prompt_tokens if prompt_tokens is not None else ctx.prompt_tokens
    ct = completion_tokens if completion_tokens is not None else ctx.completion_tokens
    if pt is not None:
        event["prompt_tokens"] = int(pt)
    if ct is not None:
        event["completion_tokens"] = int(ct)
    if pt is not None and ct is not None:
        event["total_tokens"] = int(pt) + int(ct)

    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    if duration_ms is not None:
        event["duration_ms"] = float(duration_ms)
    if streamed is not None:
        event["streamed"] = bool(streamed)
    else:
        event["streamed"] = ctx.streamed
    if incomplete is not None:
        event["incomplete"] = bool(incomplete)
    elif ctx.incomplete:
        event["incomplete"] = True
    if attempts is not None:
        event["attempts"] = int(attempts)
    elif ctx.attempts is not None:
        event["attempts"] = int(ctx.attempts)
    return event


def build_request_failed_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    error_code: str,
    http_status: Optional[int] = None,
    sanitized_message: Optional[str] = None,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    sequence: int = 1,
) -> Dict[str, Any]:
    """Build a ``request_failed`` event — emitted on sanitized errors."""
    event = _build_base_event(
        config=config,
        request_id=ctx.request_id,
        event_type="request_failed",
        sequence=sequence,
        client_fingerprint=ctx.client_fingerprint,
        endpoint=ctx.endpoint,
        ingress_protocol=ctx.ingress_protocol,
        route_type=ctx.route_type,
        requested_model=ctx.requested_model,
        resolved_model=ctx.resolved_model,
    )
    event["upstream_model"] = ctx.upstream_model
    event["provider"] = ctx.provider
    event["failover_group"] = ctx.failover_group
    event["error_code"] = str(error_code)
    if http_status is not None:
        event["http_status"] = int(http_status)
    if sanitized_message is not None:
        event["sanitized_message"] = _serialize_for_output(sanitized_message)
    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    if duration_ms is not None:
        event["duration_ms"] = float(duration_ms)
    if attempts is not None:
        event["attempts"] = int(attempts)
    elif ctx.attempts is not None:
        event["attempts"] = int(ctx.attempts)
    return event


def build_request_cancelled_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    cancel_reason: str,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    sequence: int = 1,
) -> Dict[str, Any]:
    """Build a ``request_cancelled`` event — emitted on client disconnect/timeout."""
    event = _build_base_event(
        config=config,
        request_id=ctx.request_id,
        event_type="request_cancelled",
        sequence=sequence,
        client_fingerprint=ctx.client_fingerprint,
        endpoint=ctx.endpoint,
        ingress_protocol=ctx.ingress_protocol,
        route_type=ctx.route_type,
        requested_model=ctx.requested_model,
        resolved_model=ctx.resolved_model,
    )
    event["upstream_model"] = ctx.upstream_model
    event["provider"] = ctx.provider
    event["failover_group"] = ctx.failover_group
    event["cancel_reason"] = _serialize_for_output(cancel_reason)
    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    if duration_ms is not None:
        event["duration_ms"] = float(duration_ms)
    if attempts is not None:
        event["attempts"] = int(attempts)
    elif ctx.attempts is not None:
        event["attempts"] = int(ctx.attempts)
    return event
