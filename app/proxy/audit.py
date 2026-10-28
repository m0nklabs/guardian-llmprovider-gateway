"""Metadata-only audit records for authorization and admission decisions (G1-03).

Decision record: ``docs/decisions/G1-03-admission-and-audit.md`` §11. Part 1 of
the record (§1-§10) bounded ingress; this module is the audit half: **one
record per decision, allow and deny**, on the privileged and inference surfaces.

**What a record may contain** (§11.1): UTC timestamp, key fingerprint, route
*template* and method, the resolved scope(s), the decision, a reason code, the
HTTP status, and — when an admission bound refused the request — the limit that
refused it. ``key_fingerprint`` identifies the **presented** key: a denied
request is audited with the fingerprint of the token it presented, validated or
not (that is what makes a failed attempt correlatable), while ``key_name`` and
``scopes`` only exist once the key store resolved the token. Nothing else. No payload, no headers, no query string, no prompt
text, no model output, no client IP. The record is diag data for the operator's
log aggregator, so the field set is closed and every string is bounded:
:data:`RECORD_FIELDS` is the schema, :func:`_build_record` is the only producer.

**Three sinks, all bounded** (§11.1/§11.2):

* one structured JSON log line on logger ``Guardian.Audit`` per decision
  (``event="audit"``), which is what ships to the operator's aggregator;
* a bounded in-process ring buffer (:data:`MAX_BUFFER_SIZE` records hard cap,
  default :data:`DEFAULT_BUFFER_SIZE`) for post-incident inspection without log
  shipping;
* the bounded-cardinality Prometheus counter
  ``guardian_audit_records_total{decision,reason}``, where ``reason`` is mapped
  onto the fixed vocabulary below (anything else becomes ``other``), so a
  caller cannot create unbounded metric series.

A million requests therefore cost a million log lines and **one** buffer of
fixed size: no dict, list or counter in this module is keyed on request data.

**Fail-safe, not fail-closed.** A broken ``audit`` block must not refuse
traffic: the audit sink is observability, and refusing a request because its
audit line could not be configured would be an availability attack on the
operator. Each key falls back to the *safe* side instead (see
:func:`resolve_config`), loudly, and the counter still shows the decisions.

How the record reaches a decision-point: :class:`AuditMiddleware` creates one
mutable state dict per HTTP request and stores it in the ASGI scope
(``scope["guardian_audit"]``) and in a :class:`contextvars.ContextVar`, so an
enforcement point that has a ``Request`` *and* one that only has the current
context (the outbound destination guard) can mark the same record.
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app.log_safety import clean_log_value

logger = logging.getLogger("Guardian.Audit")

# ── Configuration keys (``audit`` in config/global.settings.yaml) ──────

SECTION = "audit"
KEY_BUFFER_SIZE = "audit.buffer_size"
KEY_LOG_DECISIONS = "audit.log_decisions"
KEY_INCLUDE_KEY_NAME = "audit.include_key_name"

#: Ring-buffer size when the key is absent.
DEFAULT_BUFFER_SIZE = 256
#: Hard ceiling on the ring buffer, whatever the configuration says: the bound
#: is a memory guarantee, so it cannot be configured away.
MAX_BUFFER_SIZE = 1024
DEFAULT_LOG_DECISIONS = True
DEFAULT_INCLUDE_KEY_NAME = True

# ── Decision vocabulary ────────────────────────────────────────────────

ALLOW = "allow"
DENY = "deny"

#: Reason codes. Closed vocabulary: the record and the metric label both draw
#: from it, so no request value can widen either.
REASON_SCOPE_GRANTED = "scope_granted"
REASON_SCOPE_GRACE = "scope_grace"
REASON_MISSING_API_KEY = "missing_api_key"
REASON_INVALID_API_KEY = "invalid_api_key"
REASON_INSUFFICIENT_SCOPE = "insufficient_scope"
REASON_INVALID_SCOPE_DECLARATION = "invalid_scope_declaration"
REASON_UNDECLARED_ROUTE = "scope_undeclared_route"
REASON_RATE_LIMITED = "rate_limited"
REASON_QUEUE_FULL = "queue_full"
REASON_ADMISSION_MISCONFIGURED = "admission_misconfigured"
REASON_REQUEST_TOO_LARGE = "request_too_large"
REASON_DESTINATION_DENIED = "destination_denied"
REASON_OTHER = "other"

REASONS = frozenset(
    {
        REASON_SCOPE_GRANTED,
        REASON_SCOPE_GRACE,
        REASON_MISSING_API_KEY,
        REASON_INVALID_API_KEY,
        REASON_INSUFFICIENT_SCOPE,
        REASON_INVALID_SCOPE_DECLARATION,
        REASON_UNDECLARED_ROUTE,
        REASON_RATE_LIMITED,
        REASON_QUEUE_FULL,
        REASON_ADMISSION_MISCONFIGURED,
        REASON_REQUEST_TOO_LARGE,
        REASON_DESTINATION_DENIED,
        REASON_OTHER,
    }
)

#: The closed field set of one record, in emission order. A test asserts this
#: list matches what :func:`_build_record` produces, so a field cannot be added
#: without a decision-record update.
RECORD_FIELDS = (
    "ts",
    "event",
    "surface",
    "decision",
    "reason",
    "detail",
    "method",
    "route",
    "required_scope",
    "scopes",
    "key_fingerprint",
    "key_name",
    "status",
    "limit",
    "limit_value",
)

#: Bounded lengths: every free-form field is truncated to one of these, so a
#: record's size has a fixed ceiling (``_MAX_RECORD_BYTES`` below).
_MAX_ROUTE_CHARS = 120
_MAX_NAME_CHARS = 64
_MAX_DETAIL_CHARS = 64
_MAX_SURFACE_CHARS = 16

#: Conservative ceiling for one serialized record: the fixed integers/enums plus
#: the three bounded strings and the (max 5) bounded scope tokens. Used by the
#: bound test and stated in the decision record.
_MAX_RECORD_BYTES = 1024

_SCOPE_TOKEN = re.compile(r"[a-z][a-z0-9-]{0,23}")
_METHOD_TOKEN = re.compile(r"^[A-Z]{3,7}$")
_FINGERPRINT_TOKEN = re.compile(r"^[0-9a-f]{6,64}$")

#: Marker for "the key is not in the configuration at all".
_ABSENT = object()


# ── Configuration resolution (fail-safe per key) ───────────────────────


@dataclass(frozen=True)
class AuditConfig:
    """Resolved audit sink settings."""

    buffer_size: int = DEFAULT_BUFFER_SIZE
    log_decisions: bool = DEFAULT_LOG_DECISIONS
    include_key_name: bool = DEFAULT_INCLUDE_KEY_NAME
    source: str = "configured"


#: One-shot log markers, so a per-request resolver does not spam the log.
_LOGGED: set[str] = set()
_CONFIG_CACHE: dict[str, AuditConfig] = {}


def _log_once(marker: str, level: int, message: str, *args: Any) -> None:
    if marker in _LOGGED:
        return
    _LOGGED.add(marker)
    logger.log(level, message, *args)


def _parse_config(section: Any) -> AuditConfig:
    """Resolve the ``audit`` section; never raises, never disables auditing."""
    buffer_size = DEFAULT_BUFFER_SIZE
    log_decisions = DEFAULT_LOG_DECISIONS
    include_key_name = DEFAULT_INCLUDE_KEY_NAME
    sources: set[str] = set()

    if section is not _ABSENT and not isinstance(section, Mapping):
        # Present but not a mapping: nothing can be parsed. Keep auditing with
        # the shipped settings and refuse to ship key names until it is fixed.
        _log_once(
            "audit:section",
            logging.ERROR,
            "🛑 %s is %s, not a mapping; using the shipped audit settings "
            "(key names omitted from audit records until this is fixed)",
            SECTION,
            type(section).__name__,
        )
        return AuditConfig(
            include_key_name=False, source="shipped-default"
        )

    def _raw(key: str) -> tuple[Any, bool]:
        if section is _ABSENT:
            return None, True
        if key not in section:
            return None, True
        return section[key], False

    raw_size, absent = _raw("buffer_size")
    if not absent:
        if isinstance(raw_size, bool) or not isinstance(raw_size, int):
            _log_once(
                f"audit:buffer_size:{type(raw_size).__name__}",
                logging.ERROR,
                "🛑 %s is %s, not an integer; using the shipped default %s",
                KEY_BUFFER_SIZE,
                type(raw_size).__name__,
                DEFAULT_BUFFER_SIZE,
            )
            sources.add("shipped-default")
        elif raw_size < 1:
            _log_once(
                "audit:buffer_size:range",
                logging.ERROR,
                "🛑 %s is %s, below the minimum 1; using the shipped default %s",
                KEY_BUFFER_SIZE,
                raw_size,
                DEFAULT_BUFFER_SIZE,
            )
            sources.add("shipped-default")
        elif raw_size > MAX_BUFFER_SIZE:
            _log_once(
                "audit:buffer_size:clamped",
                logging.ERROR,
                "🛑 %s is %s, above the hard ceiling %s; clamping (the buffer "
                "bound is a memory guarantee and cannot be configured away)",
                KEY_BUFFER_SIZE,
                raw_size,
                MAX_BUFFER_SIZE,
            )
            buffer_size = MAX_BUFFER_SIZE
            sources.add("configured")
        else:
            buffer_size = raw_size
            sources.add("configured")
    else:
        sources.add("absent")

    raw_log, absent = _raw("log_decisions")
    if not absent:
        if isinstance(raw_log, bool):
            log_decisions = raw_log
            sources.add("configured")
        else:
            _log_once(
                f"audit:log_decisions:{type(raw_log).__name__}",
                logging.ERROR,
                "🛑 %s is %s, not a boolean; keeping audit log lines on",
                KEY_LOG_DECISIONS,
                type(raw_log).__name__,
            )
            sources.add("shipped-default")
    else:
        sources.add("absent")

    raw_name, absent = _raw("include_key_name")
    if not absent:
        if isinstance(raw_name, bool):
            include_key_name = raw_name
            sources.add("configured")
        else:
            # Privacy-safe direction: a typo must never *add* a name to a record.
            include_key_name = False
            _log_once(
                f"audit:include_key_name:{type(raw_name).__name__}",
                logging.ERROR,
                "🛑 %s is %s, not a boolean; omitting key names from audit "
                "records until this is fixed",
                KEY_INCLUDE_KEY_NAME,
                type(raw_name).__name__,
            )
            sources.add("shipped-default")
    else:
        sources.add("absent")

    if sources <= {"absent", "shipped-default"}:
        source = "shipped-default"
    elif "absent" in sources or "shipped-default" in sources:
        source = "mixed"
    else:
        source = "configured"
    return AuditConfig(
        buffer_size=buffer_size,
        log_decisions=log_decisions,
        include_key_name=include_key_name,
        source=source,
    )


def resolve_config(config: Mapping[str, Any] | None = None) -> AuditConfig:
    """Return the live audit settings (cached per config signature)."""
    if config is None:
        try:
            from app.config_loader import CONFIG

            config = CONFIG
        except Exception:  # pragma: no cover - import failure is fatal anyway
            return AuditConfig(source="shipped-default")

    section = config.get(SECTION, _ABSENT) if isinstance(config, Mapping) else _ABSENT
    signature = repr(section)
    cached = _CONFIG_CACHE.get(signature)
    if cached is not None:
        return cached
    resolved = _parse_config(section)
    _CONFIG_CACHE.clear()
    _CONFIG_CACHE[signature] = resolved
    if section is _ABSENT:
        _log_once(
            "audit:absent",
            logging.WARNING,
            "🛡️ %s is absent; using the shipped audit settings (log lines on, "
            "buffer %s, key names included). Add the section to "
            "config/global.settings.yaml to make it explicit.",
            SECTION,
            DEFAULT_BUFFER_SIZE,
        )
    logger.info(
        "🛡️ audit sink: log_decisions=%s, buffer %s records, key names %s [%s]",
        resolved.log_decisions,
        resolved.buffer_size,
        "included" if resolved.include_key_name else "omitted",
        resolved.source,
    )
    return resolved


def reset_cache() -> None:
    """Drop the resolution cache and the one-shot markers (tests)."""
    _CONFIG_CACHE.clear()
    _LOGGED.clear()


# ── Bounded storage ────────────────────────────────────────────────────

_BUFFER_LOCK = threading.Lock()
_BUFFER: collections.deque[dict[str, Any]] = collections.deque(maxlen=DEFAULT_BUFFER_SIZE)
_BUFFER_SIZE = DEFAULT_BUFFER_SIZE
#: Monotonic count of records emitted since process start (the ring buffer only
#: keeps the newest ones; this counter never grows memory).
_EMITTED = 0


def _ensure_capacity(size: int) -> None:
    """Resize the ring buffer to *size* (1..:data:`MAX_BUFFER_SIZE`), keeping items."""
    global _BUFFER, _BUFFER_SIZE
    bounded = max(1, min(int(size), MAX_BUFFER_SIZE))
    with _BUFFER_LOCK:
        if bounded == _BUFFER_SIZE:
            return
        kept = list(_BUFFER)[-bounded:]
        _BUFFER = collections.deque(kept, maxlen=bounded)
        _BUFFER_SIZE = bounded


def buffer_size() -> int:
    """Return the current ring-buffer bound."""
    with _BUFFER_LOCK:
        return _BUFFER_SIZE


def emitted_count() -> int:
    """Return how many records have been emitted (not how many are retained)."""
    with _BUFFER_LOCK:
        return _EMITTED


def recent(limit: int | None = None) -> list[dict[str, Any]]:
    """Return the newest retained records, oldest first (bounded by the buffer)."""
    with _BUFFER_LOCK:
        records = list(_BUFFER)
    if limit is None:
        return records
    if limit <= 0:
        return []
    return records[-limit:]


def clear() -> None:
    """Drop every retained record (tests and a deliberate operator reset)."""
    global _EMITTED
    with _BUFFER_LOCK:
        _BUFFER.clear()
        _EMITTED = 0


# ── Record construction ────────────────────────────────────────────────


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _bounded_token(value: Any, pattern: re.Pattern[str], limit: int) -> str | None:
    """Return a vocabulary-shaped token, or ``None`` when it does not fit."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > limit or not pattern.match(candidate):
        return None
    return candidate


def _bounded_text(value: Any, limit: int) -> str | None:
    """Return control-character-free text truncated to *limit* chars."""
    if value is None:
        return None
    text = clean_log_value(value, limit=limit)
    return text or None


def _scopes_list(value: Any) -> list[str] | None:
    """Return the resolved scope names, each validated against the vocabulary."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    tokens = sorted(
        {
            token
            for token in (_bounded_token(item, _SCOPE_TOKEN, 24) for item in value)
            if token is not None
        }
    )
    return tokens or None


def _reason_token(reason: Any) -> str:
    """Map *reason* onto the closed vocabulary (unknown values become ``other``)."""
    token = _bounded_token(reason, _SCOPE_TOKEN, 40)
    if token is None or token not in REASONS:
        return REASON_OTHER
    return token


def _bounded_int(value: Any, *, minimum: int = 0, maximum: int = 10**9) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum or value > maximum:
        return None
    return value


def _build_record(
    *,
    surface: str,
    decision: str,
    reason: str,
    status: int | None,
    method: Any = None,
    route: Any = None,
    required_scope: Any = None,
    scopes: Any = None,
    key_fingerprint: Any = None,
    key_name: Any = None,
    include_key_name: bool = True,
    detail: Any = None,
    limit: Any = None,
    limit_value: Any = None,
) -> dict[str, Any]:
    """Build one metadata-only record; the only producer of the record schema."""
    fingerprint = _bounded_token(key_fingerprint, _FINGERPRINT_TOKEN, 64)
    return {
        "ts": _utc_now(),
        "event": "audit",
        "surface": _bounded_text(surface, _MAX_SURFACE_CHARS) or "unknown",
        "decision": ALLOW if decision == ALLOW else DENY,
        "reason": _reason_token(reason),
        "detail": _bounded_text(detail, _MAX_DETAIL_CHARS),
        "method": _bounded_token(method, _METHOD_TOKEN, 7),
        "route": _bounded_text(route, _MAX_ROUTE_CHARS),
        "required_scope": _bounded_token(required_scope, _SCOPE_TOKEN, 24),
        "scopes": _scopes_list(scopes),
        "key_fingerprint": fingerprint,
        "key_name": (
            _bounded_text(key_name, _MAX_NAME_CHARS) if include_key_name else None
        ),
        "status": _bounded_int(status, minimum=100, maximum=599),
        "limit": _bounded_text(limit, _MAX_DETAIL_CHARS),
        "limit_value": _bounded_int(limit_value, minimum=0, maximum=2**63),
    }


def _bump_counter(decision: str, reason: str) -> None:
    """Count the record; metrics are observability, never policy."""
    try:
        from app.proxy.metrics import AUDIT_RECORDS

        AUDIT_RECORDS.labels(decision=decision, reason=reason).inc()
    except Exception:  # pragma: no cover - never let metrics break a request
        pass


def _emit(record: dict[str, Any], *, config: AuditConfig) -> None:
    """Write one record to the sinks. Never raises."""
    global _EMITTED
    try:
        _ensure_capacity(config.buffer_size)
        with _BUFFER_LOCK:
            _BUFFER.append(record)
            _EMITTED += 1
        _bump_counter(record["decision"], record["reason"])
        if config.log_decisions:
            line = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if record["decision"] == ALLOW:
                logger.info("📋 audit %s", line)
            else:
                logger.warning("📋 audit %s", line)
    except Exception:  # pragma: no cover - an audit sink must never break a request
        pass


# ── Per-request decision state ─────────────────────────────────────────

#: The state dict of the request being served in this context (if any). The
#: dict is *mutated*, never rebound, so a copy of the context (an anyio task or
#: a threadpool worker) still marks the same record.
_CURRENT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "guardian_audit_state", default=None
)

_STATE_KEY = "guardian_audit"


def state_for(
    request: Any = None, scope: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return the audit state of the current request, or ``None`` outside one."""
    candidate = scope
    if candidate is None and request is not None:
        candidate = getattr(request, "scope", None)
    if isinstance(candidate, Mapping):
        existing = candidate.get(_STATE_KEY)
        if isinstance(existing, dict):
            return existing
    return _CURRENT.get()


def mark_allow(
    *,
    reason: str = REASON_SCOPE_GRANTED,
    request: Any = None,
    scope: Mapping[str, Any] | None = None,
) -> None:
    """Mark the current request as *allowed* at an authorization point.

    Only a real decision point marks an allow, so "the request presented a key"
    is not the same as "an authorization decision was made": a public route
    stays out of the trail even when a caller sends an ``Authorization`` header
    to it. A later ``mark_denial`` overwrites this (a scope allow followed by a
    rate refusal is one request and one deny record).
    """
    try:
        state = state_for(request=request, scope=scope)
        if state is None or state.get("decision") == DENY:
            return
        state["decision"] = ALLOW
        state["reason"] = _reason_token(reason)
    except Exception:  # pragma: no cover - marking must never break a request
        pass


def mark_denial(
    *,
    reason: str,
    status: int,
    limit: str | None = None,
    limit_value: int | None = None,
    detail: Any = None,
    request: Any = None,
    scope: Mapping[str, Any] | None = None,
) -> None:
    """Mark the current request as denied at a specific enforcement point.

    Called *before* the enforcement point raises, so the middleware still emits
    exactly one record for the request (with the status the response carries).
    A no-op outside an audited request — an enforcement point reused in a unit
    test must not need an audit sink to work.
    """
    try:
        state = state_for(request=request, scope=scope)
        if state is None:
            return
        state["decision"] = DENY
        state["reason"] = _reason_token(reason)
        state["status"] = status
        if limit is not None:
            state["limit"] = _bounded_text(limit, _MAX_DETAIL_CHARS)
        if limit_value is not None:
            state["limit_value"] = limit_value
        if detail is not None:
            state["detail"] = detail
    except Exception:  # pragma: no cover - marking must never break a request
        pass


# ── Decision-point helpers ─────────────────────────────────────────────


def _route_template(scope: Mapping[str, Any]) -> str | None:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else None


def _record_for_request(
    scope: Mapping[str, Any],
    state: Mapping[str, Any],
    status: int | None,
    surface: str,
    config: AuditConfig,
) -> dict[str, Any] | None:
    """Turn the request + its marked state into one record, or ``None``.

    ``None`` means "this request made no authorization decision": nothing marked
    an allow and nothing marked a denial (a public route never runs the auth
    dependency), so there is nothing to audit. That is what keeps ``/healthz``
    and the docs routes out of the trail.

    A denial can therefore carry a fingerprint without a name: the usage
    middleware records the *presented* token's fingerprint before
    authentication runs (``app/gateway/usage.py``), which is exactly what an
    audit of a refused request needs. A validated request carries the name and
    the resolved scopes as well.
    """
    auth = scope.get("guardian_auth_context")
    auth_context = auth if isinstance(auth, Mapping) else {}
    fingerprint = auth_context.get("key_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        fingerprint = None

    decision = state.get("decision")
    if decision not in (ALLOW, DENY):
        return None

    method = scope.get("method")
    template = _route_template(scope)
    required = None
    if isinstance(method, str) and isinstance(template, str):
        try:
            from app.proxy.scopes import required_scope_for

            resolved = required_scope_for(method, template)
            required = resolved.value if resolved is not None else None
        except Exception:  # pragma: no cover - vocabulary is observability here
            required = None

    reason = str(state.get("reason") or REASON_OTHER)
    detail = state.get("detail") if decision == DENY else None
    limit = state.get("limit") if decision == DENY else None
    limit_value = state.get("limit_value") if decision == DENY else None

    return _build_record(
        surface=surface,
        decision=decision,
        reason=reason,
        status=status,
        method=method,
        route=template,
        required_scope=required,
        scopes=auth_context.get("scopes"),
        key_fingerprint=fingerprint,
        key_name=auth_context.get("client_name"),
        include_key_name=config.include_key_name,
        detail=detail,
        limit=limit,
        limit_value=limit_value,
    )


# ── ASGI middleware ────────────────────────────────────────────────────


class AuditMiddleware:
    """Emit exactly one metadata-only audit record per request decision.

    Outermost middleware on both surfaces, so it observes the final status of
    every request — including a ``413`` refused by the body bound before the
    router ran and a ``401`` raised by the auth dependency. It writes no
    response and buffers nothing: it only watches ``http.response.start``.
    """

    def __init__(self, app: Any, *, surface: str = "unknown") -> None:
        self.app = app
        self.surface = surface

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = {}
        scope[_STATE_KEY] = state
        token = _CURRENT.set(state)
        status: int | None = None

        async def _send(message: dict) -> None:
            nonlocal status
            if message.get("type") == "http.response.start" and status is None:
                observed = message.get("status")
                status = observed if isinstance(observed, int) else None
            await send(message)

        failed = False
        try:
            await self.app(scope, receive, _send)
        except BaseException:
            failed = True
            raise
        finally:
            _CURRENT.reset(token)
            observed_status = status if status is not None else (500 if failed else None)
            try:
                config = resolve_config()
                record = _record_for_request(
                    scope, state, observed_status, self.surface, config
                )
                if record is not None:
                    _emit(record, config=config)
            except Exception:  # pragma: no cover - audit never breaks a request
                pass


def install(app: Any, *, surface: str) -> None:
    """Install the audit middleware as the outermost middleware of *app*.

    Idempotent. Installed after the route definitions on purpose: the module
    that owns each app calls this at import time, and keeping the call below the
    handlers leaves their pinned line numbers in
    ``docs/G0_ENDPOINT_AUTH_MATRIX.md`` untouched. Starlette builds the
    middleware stack at startup, so registration order in the module is
    irrelevant; the list index is what makes this the outermost layer.
    """
    from starlette.middleware import Middleware

    for existing in getattr(app, "user_middleware", []):
        if getattr(existing, "cls", None) is AuditMiddleware:
            return
    app.user_middleware.insert(0, Middleware(AuditMiddleware, surface=surface))
