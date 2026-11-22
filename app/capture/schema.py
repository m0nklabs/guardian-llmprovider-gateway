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
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.capture.config import CaptureConfig

logger = logging.getLogger("Guardian.Capture.Schema")

#: Wire-contract identifier — readers reject unknown schema_name values.
SCHEMA_NAME = "guardian_capture_v1"

#: Semantic version within the v1 contract.  Minor/patch bumps for additive
#: field changes; a major change requires renaming schema_name (e.g. v2).
#: 1.1.0 (2026-08-30): additive capture-feedback fields — started_at_utc /
#: completed_at_utc, always-present finish_reason + native_finish_reason,
#: rich upstream usage mirror (completion_tokens_details, native token
#: counts, cost, provider_name), caller correlation identity
#: (caller_request_id / app_title / app_referer) and explicit streamed legs.
#: 1.2.0: additive degeneration_cutoff flag on completed events.
#: 1.3.0 (issue #125): additive in-stream upstream error metadata on
#: request_completed (error_code / error_type) — metadata only, never text.
SCHEMA_VERSION = "1.3.0"

#: Maximum length for caller-supplied identity strings stored on events
#: (caller_request_id, app_title, app_referer).
CALLER_IDENTITY_MAX_LEN = 256

#: Maximum length for provider-reported error metadata (code/type) retained
#: from an error delivered inside an HTTP 200 stream.  The values are
#: provider-defined enum members, not free text; a longer value is rejected
#: outright, never truncated (truncating first would strip a prose or
#: credential suffix and let an identifier-shaped prefix through).
STREAM_ERROR_METADATA_MAX_LEN = 64

#: Stable Guardian code used when an in-stream error carries no retainable
#: code or type, so the failure is never invisible in the capture record.
STREAM_ERROR_FALLBACK_CODE = "upstream_stream_error"

#: Retained error metadata must be identifier-shaped — a leading alphanumeric
#: followed by word characters or ``. : -`` (e.g. ``502``,
#: ``invalid_request_error``, ``google.rpc.Code``) — and the rule applies to
#: the **whole** stripped value.  This is the privacy boundary, not
#: cosmetics: payload-shaped values must never survive — whitespace-free JSON
#: (``{"prompt":"private"}``), URLs (``https://host/secret``), query strings
#: (``k=v&x=y``) and prose after an identifier prefix
#: (``"sk-aaa… was rejected"``) are all rejected, because they carry content
#: rather than an enum member.
STREAM_ERROR_METADATA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

#: Maximum length for a provider-reported model identifier retained on a
#: completed event.  Longer values are rejected outright, never truncated.
UPSTREAM_MODEL_MAX_LEN = 128

#: A model identifier is **not** an enum member: it legitimately carries
#: ``/ . : - _ @`` and digits (``openai/gpt-4o``, ``claude-3.5-sonnet``,
#: ``qwen2.5-coder:32b@q4_k_m``), so it needs its own bounded rule: one token,
#: alphanumeric at both ends, model-id characters in between, applied to the
#: **whole** stripped value.  Charset alone cannot separate an id from a
#: payload — ``:`` and ``/`` are legal inside an id — which is why
#: :func:`sanitize_upstream_model` additionally rejects scheme and
#: authority separators.
UPSTREAM_MODEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:/@-]*[A-Za-z0-9])?$")


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion; returns None when the value is not coercible.

    Defensive against upstream providers emitting token counters as floats
    or strings: a non-coercible value yields None (field omitted) instead of
    raising and silently dropping the whole event.  Non-finite floats
    (``inf``/``nan`` — Python parses JSON ``1e999`` as ``inf``) also yield
    None: ``int(inf)`` raises OverflowError and an infinite counter is not a
    real value (review finding).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion; returns None when not coercible.

    Non-finite results (``inf``/``nan``) yield None: ``json.dumps`` would
    emit bare ``Infinity``/``NaN`` which strict JSON parsers (e.g. jq)
    cannot read (review finding).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return result


def sanitize_stream_error_metadata(value: Any) -> str | None:
    """Return bounded, text-free error metadata for a capture event.

    Providers report in-stream errors as machine-readable codes/types
    (``{"code": 502, "type": "provider_error"}``), but the same shape may
    carry a human-readable message.  Only identifier-shaped values survive
    (:data:`STREAM_ERROR_METADATA_RE`), applied to the **whole** stripped
    value: an int is stringified, an integral float keeps its numeric
    identity (``502.0`` → ``"502"``), and a value longer than
    :data:`STREAM_ERROR_METADATA_MAX_LEN` is rejected rather than truncated.
    Prose (``"Provider returned an empty response"``), payload-shaped values
    (embedded JSON, URLs, query strings) and a fractional code (``502.5``,
    which cannot be stored without changing the provider's code) are all
    rejected.

    ``None`` means "nothing retainable" — the caller omits the field.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        # Integral floats (``502.0``) keep their numeric identity; non-finite
        # values must not raise on int(), and a fractional code would be
        # rewritten into a *different* code, so both are dropped.
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = str(int(value))
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    # Validate the whole value *before* any bounding.  Truncating first would
    # let an identifier-shaped prefix through while the suffix that proves it
    # is not metadata (prose, or a credential fragment) fell off the end.
    if len(candidate) > STREAM_ERROR_METADATA_MAX_LEN:
        return None
    if not STREAM_ERROR_METADATA_RE.match(candidate):
        return None
    return candidate


def sanitize_upstream_model(value: Any) -> str | None:
    """Return a provider-reported model id bounded for a capture event.

    The value is what the **provider** says served the request, so it is not
    an enum member and not free text: it is one model-id token
    (:data:`UPSTREAM_MODEL_RE`) of at most :data:`UPSTREAM_MODEL_MAX_LEN`
    characters.  Payload-shaped values never survive — embedded JSON, URLs
    (``http://``/``https://``, and any ``//`` authority separator), query
    strings (``k=v&x=y``), prose with whitespace and pure separator soup are
    all rejected, because they carry content or route somewhere rather than
    naming a model.

    The whole stripped value is validated **before** any bounding: an
    over-long value is dropped, never truncated, because truncating first
    would leave an identifier-shaped prefix while the suffix that proves it
    is not a model id (prose, a credential fragment) fell off the end — the
    defect PR #126 had to fix for error metadata.

    ``None`` means "the provider reported no usable model" — the caller omits
    or nulls the field; it never invents a value from ``resolved_model``.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > UPSTREAM_MODEL_MAX_LEN:
        return None
    if not UPSTREAM_MODEL_RE.match(candidate):
        return None
    # ``:`` and ``/`` are legal *inside* a model id, so the character rule
    # alone would let a URL through.  What makes it a URL (or any address) is
    # the scheme or the ``//`` authority separator, not the separator itself.
    if "//" in candidate or candidate.lower().startswith(("http:", "https:")):
        return None
    return candidate

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
    key_fingerprint: str | None,
    *,
    allowed_refs: list[str] | None = None,
) -> str | None:
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
    secrets: list[str] = []
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


def compute_record_auth(record_line: str) -> dict[str, str] | None:
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
    client_fingerprint: str | None = None,
    endpoint: str,
    ingress_protocol: str,
    route_type: str,
    requested_model: str | None = None,
    resolved_model: str | None = None,
) -> dict[str, Any]:
    """Build the common base fields present on every capture event."""
    client_ref = compute_client_ref(
        client_fingerprint,
        allowed_refs=config.allowed_client_refs if config.per_client_opt_in else None,
    ) if client_fingerprint else None

    event: dict[str, Any] = {
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
    requested_model: str | None
    capture_policy_version: str
    instance_id: str

    # Identity
    client_fingerprint: str | None = None

    # Caller-supplied correlation identity (from configured inbound request
    # headers; absent when the client did not send any of them).  Stamped by
    # the capture controller from a bounded per-request registry, so it is
    # present on every event of the request regardless of which call site
    # built the BuildContext.
    caller_request_id: str | None = None
    app_title: str | None = None
    app_referer: str | None = None

    # Optional resolved metadata (set as request progresses)
    resolved_model: str | None = None
    upstream_model: str | None = None
    provider: str | None = None
    failover_group: str | None = None
    attempts: int | None = None

    # Timing
    # started_at_utc: wall-clock UTC when capture began tracking this request
    # (≈ the request_received moment).  Auto-stamped at BuildContext
    # construction when not provided explicitly, so every terminal event can
    # reference the request start even though the monotonic lifecycle
    # timestamps below are not serializable.
    started_at_utc: str | None = None
    request_received_ts: float | None = None  # monotonic
    request_completed_ts: float | None = None  # monotonic

    # Token usage
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    # Streaming metadata
    streamed: bool = False
    incomplete: bool = False

    # HTTP metadata
    http_status: int | None = None

    # Error metadata
    error_code: str | None = None

    # Content (already redacted)
    request_messages: list[dict[str, Any]] | None = None
    request_parameters: dict[str, Any] | None = None
    response_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_results: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None
    finish_reason: str | None = None

    # Grammar-Constrained Decoding presence flags (content is never stored)
    grammar_present: bool = False
    response_format_present: bool = False

    def __post_init__(self) -> None:
        # Auto-stamp the wall-clock request start.  BuildContext is created
        # at the point capture starts tracking a request (immediately after
        # the request_received dispatch on every route), so construction time
        # is the best available proxy for "when the request entered capture".
        if self.started_at_utc is None:
            self.started_at_utc = _utc_now_iso()

    def to_config(self) -> CaptureConfig:
        """Reconstruct a minimal CaptureConfig from known context."""
        return CaptureConfig(
            instance_id=self.instance_id,
            policy_version=self.capture_policy_version,
        )


def _apply_request_origin(event: dict[str, Any], ctx: BuildContext) -> None:
    """Copy caller-supplied correlation identity from the context onto an event.

    Fields stay absent when the caller did not provide them — nothing is
    fabricated.  Values are already length-capped by the dispatch layer.
    """
    if ctx.caller_request_id:
        event["caller_request_id"] = ctx.caller_request_id
    if ctx.app_title:
        event["app_title"] = ctx.app_title
    if ctx.app_referer:
        event["app_referer"] = ctx.app_referer


def build_request_received_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    request_messages: list[dict[str, Any]] | None = None,
    request_parameters: dict[str, Any] | None = None,
    queue_wait_ms: float | None = None,
    sequence: int = 0,
) -> dict[str, Any]:
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
    event["grammar_present"] = bool(ctx.grammar_present)
    event["response_format_present"] = bool(ctx.response_format_present)
    # Wall-clock request start — on request_received it equals timestamp_utc
    # (the event is built when the request enters capture).
    event["started_at_utc"] = event["timestamp_utc"]
    _apply_request_origin(event, ctx)
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
    response_content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
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
    upstream_model: str | None = None,
    queue_wait_ms: float | None = None,
    duration_ms: float | None = None,
    http_status: int | None = None,
    streamed: bool | None = None,
    streamed_ingress: bool | None = None,
    streamed_upstream: bool | None = None,
    incomplete: bool | None = None,
    degeneration_cutoff: bool | None = None,
    error_code: str | None = None,
    error_type: str | None = None,
    attempts: int | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
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
    # Provider-reported served model (issue #127), the same provider-reported
    # shape as ``provider_name``: an explicit value from the upstream payload
    # wins over anything recorded on the context, only a usable value is
    # retained, and ``null`` stays the answer when the provider reported
    # nothing — the field is never inferred from ``resolved_model``.
    event["upstream_model"] = sanitize_upstream_model(
        upstream_model if upstream_model is not None else ctx.upstream_model
    )
    event["provider"] = ctx.provider
    event["failover_group"] = ctx.failover_group
    # Wall-clock lifecycle timestamps: started_at_utc references the request
    # start (from the context); completed_at_utc equals timestamp_utc (this
    # event is built at completion time).  Both are additive in 1.1.0;
    # timestamp_utc is kept for backward compatibility.
    if ctx.started_at_utc:
        event["started_at_utc"] = ctx.started_at_utc
    event["completed_at_utc"] = event["timestamp_utc"]
    _apply_request_origin(event, ctx)
    if response_content is not None:
        event["response_content"] = _serialize_for_output(response_content)
    if tool_calls is not None:
        event["tool_calls"] = _serialize_for_output(tool_calls)
    if tool_results is not None:
        event["tool_results"] = _serialize_for_output(tool_results)
    if reasoning_content is not None:
        event["reasoning_content"] = _serialize_for_output(reasoning_content)
    # finish_reason is ALWAYS present on request_completed (null when the
    # upstream did not report one) so consumers can distinguish
    # length-vs-stop truncation from "field not captured".
    if finish_reason is not None:
        event["finish_reason"] = _serialize_for_output(finish_reason)
    else:
        event["finish_reason"] = None
    if native_finish_reason is not None:
        event["native_finish_reason"] = _serialize_for_output(native_finish_reason)
    event["http_status"] = http_status if http_status is not None else ctx.http_status

    # Token usage — int-coerced defensively; a non-coercible value omits the
    # field instead of dropping the whole event.
    pt_raw = prompt_tokens if prompt_tokens is not None else ctx.prompt_tokens
    ct_raw = completion_tokens if completion_tokens is not None else ctx.completion_tokens
    pt = _coerce_int(pt_raw)
    ct = _coerce_int(ct_raw)
    if pt is not None:
        event["prompt_tokens"] = pt
    if ct is not None:
        event["completion_tokens"] = ct
    if pt is not None and ct is not None:
        event["total_tokens"] = pt + ct

    # Rich upstream usage mirror (additive 1.1.0) — stored as reported by the
    # upstream provider; fields stay absent when the provider did not supply
    # them.  completion_tokens_details keeps the OpenAI/OpenRouter shape
    # (contains reasoning_tokens) as-is.
    if isinstance(completion_tokens_details, dict) and completion_tokens_details:
        event["completion_tokens_details"] = _serialize_for_output(completion_tokens_details)
    ntr = _coerce_int(native_tokens_reasoning)
    if ntr is not None:
        event["native_tokens_reasoning"] = ntr
    ntc = _coerce_int(native_tokens_cached)
    if ntc is not None:
        event["native_tokens_cached"] = ntc
    cost_val = _coerce_float(cost)
    if cost_val is not None:
        event["cost"] = cost_val
    if isinstance(provider_name, str) and provider_name:
        event["provider_name"] = provider_name

    # Additive 1.2.0 — repetition-loop cutoff (server-side degeneration
    # guard). Field stays absent unless the cutoff actually fired.
    if degeneration_cutoff:
        event["degeneration_cutoff"] = True

    # Additive 1.3.0 — upstream error delivered inside an HTTP 200 stream
    # (issue #125).  Metadata only: the provider-defined code and type,
    # bounded and text-free.  Both keys stay absent when no in-stream error
    # was seen, and a normal completion is unchanged.
    stream_error_code = sanitize_stream_error_metadata(error_code)
    if stream_error_code is not None:
        event["error_code"] = stream_error_code
    stream_error_type = sanitize_stream_error_metadata(error_type)
    if stream_error_type is not None:
        event["error_type"] = stream_error_type

    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    if duration_ms is not None:
        event["duration_ms"] = float(duration_ms)
    if streamed is not None:
        event["streamed"] = bool(streamed)
    elif streamed_ingress is not None:
        # Compat definition: "streamed" is the ingress leg.
        event["streamed"] = bool(streamed_ingress)
    else:
        event["streamed"] = ctx.streamed
    if streamed_ingress is not None:
        event["streamed_ingress"] = bool(streamed_ingress)
    if streamed_upstream is not None:
        event["streamed_upstream"] = bool(streamed_upstream)
    if incomplete is not None:
        event["incomplete"] = bool(incomplete)
    elif ctx.incomplete:
        event["incomplete"] = True
    attempts_val = _coerce_int(attempts if attempts is not None else ctx.attempts)
    if attempts_val is not None:
        event["attempts"] = attempts_val
    return event


def build_request_failed_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    error_code: str,
    http_status: int | None = None,
    sanitized_message: str | None = None,
    queue_wait_ms: float | None = None,
    duration_ms: float | None = None,
    attempts: int | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
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
    if ctx.started_at_utc:
        event["started_at_utc"] = ctx.started_at_utc
    event["completed_at_utc"] = event["timestamp_utc"]
    _apply_request_origin(event, ctx)
    if http_status is not None:
        event["http_status"] = int(http_status)
    if sanitized_message is not None:
        event["sanitized_message"] = _serialize_for_output(sanitized_message)
    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    if duration_ms is not None:
        event["duration_ms"] = float(duration_ms)
    attempts_val = _coerce_int(attempts if attempts is not None else ctx.attempts)
    if attempts_val is not None:
        event["attempts"] = attempts_val
    return event


def build_request_cancelled_event(
    config: CaptureConfig,
    ctx: BuildContext,
    *,
    cancel_reason: str,
    queue_wait_ms: float | None = None,
    duration_ms: float | None = None,
    attempts: int | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
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
    if ctx.started_at_utc:
        event["started_at_utc"] = ctx.started_at_utc
    event["completed_at_utc"] = event["timestamp_utc"]
    _apply_request_origin(event, ctx)
    if queue_wait_ms is not None:
        event["queue_wait_ms"] = float(queue_wait_ms)
    if duration_ms is not None:
        event["duration_ms"] = float(duration_ms)
    attempts_val = _coerce_int(attempts if attempts is not None else ctx.attempts)
    if attempts_val is not None:
        event["attempts"] = attempts_val
    return event
