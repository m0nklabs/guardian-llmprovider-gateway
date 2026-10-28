"""Bounded admission for Guardian's ingress (G1-03, part 1).

Decision record: ``docs/decisions/G1-03-admission-and-audit.md`` §2-§6.
Part 2 (metadata audit) extends the same record and is not implemented here.

Three bounds, each with its own stable error:

===============  =========================  ==============================
Bound            Enforcement point          Stable error
===============  =========================  ==============================
request body     ASGI middleware, both apps ``413 request_too_large``
queue depth      ``app/proxy/queue.py``     ``503 queue_full``
per-client rate  ``app/proxy/auth.py``      ``429 rate_limited``
===============  =========================  ==============================

**Single process, deliberately.** Every counter here lives in this process'
memory: the token buckets reset with the service and two Guardian replicas
would each enforce its own budget. That is the documented scope of G1-03
("multi-replica accounting — single-process limits only"); nothing in this
module claims cluster-wide fairness.

**Configuration.** ``admission`` in ``config/global.settings.yaml``, re-read
from the shared :data:`app.config_loader.CONFIG` dict on every resolution, so
``POST /api/config/reload`` applies the body and rate bounds without a restart.
The queue bound is read once when the queue object is built (restart-only),
exactly like ``queue.max_concurrent``.

**Fail-closed.** A limit that cannot be resolved is never "unlimited":

* **absent** key -> the shipped default, with a WARNING (a config file that
  predates this record must not take admission down on the next restart);
* **malformed** key -> that bound **refuses** the requests it guards with a
  typed ``503 admission_misconfigured`` naming the limit and the reason.

There is no ``0``/``off`` value for any of the three bounds: a bound that can be
disabled by a number is one typo away from disabled, and the record says so.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from app.proxy.queue import DEFAULT_QUEUE_MAX_DEPTH, QueueDepthExceeded

logger = logging.getLogger("Guardian.Admission")

# ── Shipped defaults (documented as "safe but not tuned") ──────────────
#
# max_body_bytes: 32 MiB. Chat with large prompts and inline images is the
# traffic this must not break; the G0 baseline's chat bodies are kilobytes, and
# the capture/queue path already holds a whole body in memory
# (``request.body()``), so the bound is an abuse ceiling, not a performance
# target. Speech uploads (``/v1/audio/transcriptions``) sit far below it.
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024

# queue_max_depth: 32 waiting requests, i.e. 32x the highest depth ever measured
# (``docs/G0_BASELINE.md`` §2.4/§2.5: depth 1, ``max_concurrent: 1``).
DEFAULT_QUEUE_MAX_DEPTH_BOUND = DEFAULT_QUEUE_MAX_DEPTH

# rate_limit: 300 requests/minute sustained with a 300-request burst, per key.
# The baseline's heaviest observed minute is a 20-sample probe series (~9 local
# samples/minute at ~7 s each, cloud samples faster); a busy agent session adds
# queue-status polling. 300/min is an order of magnitude above the measured
# peak and a whole minute's allowance can be spent at once, so a reconnect storm
# or a batch of parallel tool calls does not trip it.
DEFAULT_REQUESTS_PER_MINUTE = 300
DEFAULT_BURST = 300

# Retry hint for a full queue: long enough for the slot holder to make progress,
# short enough that a client that honours it is not abandoned. The slot queue is
# 1-deep (``max_concurrent: 1``), so one generation (tens of seconds) is the
# scale.
DEFAULT_QUEUE_FULL_RETRY_AFTER_S = 60

#: Config section and key names (the ``limit`` field of every typed error).
SECTION = "admission"
LIMIT_BODY = "admission.max_body_bytes"
LIMIT_QUEUE_DEPTH = "admission.queue_max_depth"
LIMIT_QUEUE_RETRY = "admission.queue_full_retry_after_seconds"
LIMIT_RATE = "admission.rate_limit.requests_per_minute"
LIMIT_BURST = "admission.rate_limit.burst"

_ABSENT = object()

#: One-shot log markers, so a per-request resolver does not spam the log.
_LOGGED: set[str] = set()

#: Resolved limits for the current config signature (one live entry).
_LIMITS_CACHE: dict[str, "AdmissionLimits"] = {}


def _log_once(marker: str, level: int, message: str, *args: Any) -> None:
    if marker in _LOGGED:
        return
    _LOGGED.add(marker)
    logger.log(level, message, *args)


def _bump_rejection(limit: str) -> None:
    """Count a rejection; metrics are observability, never policy."""
    try:
        from app.proxy.metrics import ADMISSION_REJECTIONS

        ADMISSION_REJECTIONS.labels(limit=limit).inc()
    except Exception:  # pragma: no cover - never let metrics break admission
        pass


# ── Typed errors ───────────────────────────────────────────────────────


def body_too_large_detail(
    limit_bytes: int,
    *,
    observed_bytes: int | None = None,
    declared_bytes: int | None = None,
) -> dict[str, Any]:
    """Body of the stable ``413``: names the limit, never echoes the payload."""
    detail: dict[str, Any] = {
        "error": "request_too_large",
        "limit": LIMIT_BODY,
        "limit_bytes": limit_bytes,
        "message": (
            f"request body exceeds {LIMIT_BODY} ({limit_bytes} bytes); "
            "reduce the request size or raise the configured limit"
        ),
    }
    if declared_bytes is not None:
        detail["declared_bytes"] = declared_bytes
    if observed_bytes is not None:
        detail["observed_bytes"] = observed_bytes
    return detail


def misconfigured_detail(limit: str, reason: str) -> dict[str, Any]:
    """Body of the stable ``503 admission_misconfigured`` (fail-closed config)."""
    return {
        "error": "admission_misconfigured",
        "limit": limit,
        "reason": reason,
        "message": (
            f"admission limit {limit} cannot be resolved ({reason}); the bound "
            "refuses these requests until the configuration is fixed "
            "(fail-closed)"
        ),
    }


def rate_limited_detail(
    *,
    requests_per_minute: int,
    burst: int,
    retry_after_s: int,
) -> dict[str, Any]:
    """Body of the stable ``429``: names the limit and the retry hint."""
    return {
        "error": "rate_limited",
        "limit": LIMIT_RATE,
        "requests_per_minute": requests_per_minute,
        "burst": burst,
        "retry_after_s": retry_after_s,
        "message": (
            f"per-key request rate exceeded ({requests_per_minute}/min, burst "
            f"{burst}); retry after {retry_after_s}s"
        ),
    }


def queue_full_detail(
    *,
    max_queue_depth: int,
    queue_length: int,
    max_concurrent: int,
    retry_after_s: int | None,
) -> dict[str, Any]:
    """Body of the stable ``503 queue_full`` (the waiting queue is at its bound)."""
    detail: dict[str, Any] = {
        "error": "queue_full",
        "limit": LIMIT_QUEUE_DEPTH,
        "max_queue_depth": max_queue_depth,
        "queue_length": queue_length,
        "max_concurrent": max_concurrent,
        "message": (
            f"the inference queue is full ({queue_length} waiting, limit "
            f"{max_queue_depth}); the request was not queued"
        ),
    }
    if retry_after_s is not None:
        detail["retry_after_s"] = retry_after_s
        detail["message"] += f"; retry after {retry_after_s}s"
    return detail


# ── Configuration resolution ───────────────────────────────────────────


@dataclass(frozen=True)
class AdmissionLimits:
    """Resolved admission bounds; ``invalid`` marks the ones that must refuse."""

    max_body_bytes: int | None = None
    queue_max_depth: int | None = None
    queue_full_retry_after_s: int | None = None
    requests_per_minute: int | None = None
    burst: int | None = None
    #: ``{limit: reason}`` for every bound that could not be resolved.
    invalid: Mapping[str, str] = field(default_factory=dict)
    #: ``configured``, ``shipped-default`` or ``mixed`` — for the startup line.
    source: str = "configured"

    def error(self, limit: str) -> str | None:
        """Return the reason this limit is unusable, or ``None`` when valid."""
        return self.invalid.get(limit)


def _coerce_bound(
    raw: Any,
    *,
    limit: str,
    minimum: int = 1,
) -> tuple[int | None, str | None]:
    """Validate one positive-integer bound -> ``(value, error_reason)``.

    ``bool`` is rejected explicitly (``isinstance(True, int)`` is true), as are
    numeric strings and floats: a bound that arrives in the wrong type is a
    malformed declaration, not an invitation to guess.
    """
    if isinstance(raw, bool):
        return None, "boolean is not a byte/request count"
    if isinstance(raw, int):
        if raw < minimum:
            return None, f"{raw} is below the minimum {minimum} (no 'off' value)"
        return raw, None
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None, f"{raw!r} is not finite"
        if raw != int(raw):
            return None, f"{raw!r} is not a whole number"
        value = int(raw)
        if value < minimum:
            return None, f"{value} is below the minimum {minimum} (no 'off' value)"
        return value, None
    if raw is None:
        return None, "null"
    return None, f"{type(raw).__name__} is not a positive integer"


def _resolve_one(
    section: Any,
    path: tuple[str, ...],
    default: int,
    *,
    limit: str,
) -> tuple[int | None, str | None, bool]:
    """Resolve one key inside ``admission`` -> ``(value, error, absent)``.

    An absent key (missing section, missing key) yields the shipped default and
    an ``absent=True`` marker for the warning; a present-but-broken value yields
    an error, which the caller turns into a refusal.
    """
    node = section
    for part in path:
        if node is _ABSENT:
            break
        if not isinstance(node, Mapping):
            return None, f"{part} is not a mapping ({type(node).__name__})", False
        if part not in node:
            return default, None, True
        node = node[part]
    if node is _ABSENT:
        return default, None, True
    value, error = _coerce_bound(node, limit=limit)
    return value, error, False


def _parse_limits(section: Any) -> AdmissionLimits:
    """Turn the raw ``admission`` value into resolved limits (never raises)."""
    invalid: dict[str, str] = {}
    sources: set[str] = set()

    def _take(path: tuple[str, ...], default: int, limit: str) -> int | None:
        value, error, absent = _resolve_one(section, path, default, limit=limit)
        if error is not None:
            invalid[limit] = error
            sources.add("invalid")
            return None
        if absent:
            sources.add("shipped-default")
            _log_once(
                f"absent:{limit}",
                logging.WARNING,
                "🛡️ %s is absent; using the shipped default %s. Add the key to "
                "config/global.settings.yaml to make it explicit.",
                limit,
                default,
            )
        else:
            sources.add("configured")
        return value

    if section is not _ABSENT and not isinstance(section, Mapping):
        # A present-but-broken section cannot fall back to defaults (that would
        # hide the typo); every bound refuses.
        reason = f"{SECTION} is {type(section).__name__}, not a mapping"
        return AdmissionLimits(
            invalid={
                LIMIT_BODY: reason,
                LIMIT_QUEUE_DEPTH: reason,
                LIMIT_QUEUE_RETRY: reason,
                LIMIT_RATE: reason,
                LIMIT_BURST: reason,
            },
            source="invalid",
        )

    max_body_bytes = _take(("max_body_bytes",), DEFAULT_MAX_BODY_BYTES, LIMIT_BODY)
    queue_max_depth = _take(("queue_max_depth",), DEFAULT_QUEUE_MAX_DEPTH_BOUND, LIMIT_QUEUE_DEPTH)
    queue_full_retry_after_s = _take(
        ("queue_full_retry_after_seconds",),
        DEFAULT_QUEUE_FULL_RETRY_AFTER_S,
        LIMIT_QUEUE_RETRY,
    )
    requests_per_minute = _take(
        ("rate_limit", "requests_per_minute"),
        DEFAULT_REQUESTS_PER_MINUTE,
        LIMIT_RATE,
    )
    burst = _take(("rate_limit", "burst"), DEFAULT_BURST, LIMIT_BURST)

    # The retry hint belongs to the queue-depth bound: a broken hint must not
    # silently turn into "no hint", so it refuses the same requests the bound
    # does (one bound, one refusal).
    if LIMIT_QUEUE_RETRY in invalid:
        invalid.setdefault(
            LIMIT_QUEUE_DEPTH, f"{LIMIT_QUEUE_RETRY}: {invalid[LIMIT_QUEUE_RETRY]}"
        )

    if sources == {"shipped-default"}:
        source = "shipped-default"
    elif "invalid" in sources:
        source = "invalid"
    elif "shipped-default" in sources and "configured" in sources:
        source = "mixed"
    else:
        source = "configured"

    return AdmissionLimits(
        max_body_bytes=max_body_bytes,
        queue_max_depth=queue_max_depth,
        queue_full_retry_after_s=queue_full_retry_after_s,
        requests_per_minute=requests_per_minute,
        burst=burst,
        invalid=invalid,
        source=source,
    )


def resolve_limits(config: Mapping[str, Any] | None = None) -> AdmissionLimits:
    """Return the live admission bounds (cached per config signature).

    Reads the shared CONFIG dict on every call so ``POST /api/config/reload``
    takes effect without a restart. ``config`` is injectable for tests; by
    default the process-wide configuration is used.
    """
    if config is None:
        try:
            from app.config_loader import CONFIG

            config = CONFIG
        except Exception as exc:  # pragma: no cover - import failure is fatal anyway
            reason = f"configuration unavailable ({exc})"
            return AdmissionLimits(
                invalid={
                    LIMIT_BODY: reason,
                    LIMIT_QUEUE_DEPTH: reason,
                    LIMIT_QUEUE_RETRY: reason,
                    LIMIT_RATE: reason,
                    LIMIT_BURST: reason,
                },
                source="invalid",
            )

    section = config.get(SECTION, _ABSENT) if isinstance(config, Mapping) else _ABSENT
    signature = repr(section)
    cached = _LIMITS_CACHE.get(signature)
    if cached is not None:
        return cached

    limits = _parse_limits(section)
    _LIMITS_CACHE.clear()
    _LIMITS_CACHE[signature] = limits
    if limits.invalid:
        logger.error(
            "🛑 admission configuration refuses traffic: %s",
            ", ".join(f"{limit} ({reason})" for limit, reason in sorted(limits.invalid.items())),
        )
    else:
        logger.info(
            "🛡️ admission bounds: body %s bytes, queue depth %s, rate %s/min "
            "(burst %s), queue-full Retry-After %ss [%s]",
            limits.max_body_bytes,
            limits.queue_max_depth,
            limits.requests_per_minute,
            limits.burst,
            limits.queue_full_retry_after_s,
            limits.source,
        )
    return limits


def reset_cache() -> None:
    """Drop the resolution cache and the one-shot log markers (tests)."""
    _LIMITS_CACHE.clear()
    _LOGGED.clear()


def queue_full_retry_after_s() -> int | None:
    """Return the configured ``Retry-After`` for a full queue, or ``None``."""
    limits = resolve_limits()
    if limits.error(LIMIT_QUEUE_DEPTH) is not None:
        return None
    return limits.queue_full_retry_after_s


def check_queue_admission() -> None:
    """Refuse a submission when the queue-depth bound cannot be resolved.

    Checked per submission (not only at startup) so that a malformed
    ``queue_full_retry_after_seconds`` introduced by a hot reload still produces
    the promised ``503 admission_misconfigured`` instead of quietly dropping the
    retry hint — the queue object keeps a usable numeric depth, so the bound
    itself cannot catch that case.
    """
    from fastapi import HTTPException

    reason = resolve_limits().error(LIMIT_QUEUE_DEPTH)
    if reason is None:
        return
    _bump_rejection(LIMIT_QUEUE_DEPTH)
    logger.error(
        "🛑 queue admission refused (configuration): %s",
        reason,
    )
    raise HTTPException(status_code=503, detail=misconfigured_detail(LIMIT_QUEUE_DEPTH, reason))


def queue_depth_error(exc: QueueDepthExceeded) -> "Exception":
    """Translate a refused queue submission into the stable ``503``.

    ``queue_full`` carries ``Retry-After`` (the configured hint); a bound that
    could not be resolved carries ``admission_misconfigured`` with the reason and
    **no** retry hint, because retrying cannot help until the config is fixed.
    """
    from fastapi import HTTPException

    _bump_rejection(LIMIT_QUEUE_DEPTH)
    if exc.reason == "admission_misconfigured":
        reason = resolve_limits().error(LIMIT_QUEUE_DEPTH) or "queue depth bound is not configured"
        logger.error(
            "🛑 queue admission refused (configuration): queue_length=%s reason=%s",
            exc.queue_length,
            reason,
        )
        return HTTPException(
            status_code=503,
            detail=misconfigured_detail(LIMIT_QUEUE_DEPTH, reason),
        )

    retry_after = queue_full_retry_after_s()
    logger.warning(
        "🛑 queue-full %s",
        json.dumps(
            {
                "event": "queue_full",
                "queue_length": exc.queue_length,
                "max_queue_depth": exc.max_queue_depth,
                "max_concurrent": exc.max_concurrent,
                "retry_after_s": retry_after,
            },
            sort_keys=True,
        ),
    )
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(
        status_code=503,
        detail=queue_full_detail(
            max_queue_depth=exc.max_queue_depth if exc.max_queue_depth is not None else 0,
            queue_length=exc.queue_length,
            max_concurrent=exc.max_concurrent,
            retry_after_s=retry_after,
        ),
        headers=headers,
    )


# ── Per-client rate limiting (token bucket, single process) ────────────


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """Cheap single-process token bucket keyed by authenticated identity.

    One bucket per key fingerprint (the G1-01 identity surface), never per IP:
    an IP is shared by an office, a NAT and a proxy, and it is not the thing a
    key's budget belongs to. A request costs one token; ``burst`` tokens exist
    at rest; ``requests_per_minute`` refills the bucket. No timer, no task, no
    lock held across an await: a check is a dict lookup plus arithmetic.
    """

    #: Above this many tracked identities, idle buckets are dropped.
    _PRUNE_THRESHOLD = 4096

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._rate_per_minute = DEFAULT_REQUESTS_PER_MINUTE
        self._burst = DEFAULT_BURST

    # -- configuration ---------------------------------------------------

    def configure(self, requests_per_minute: int, burst: int) -> None:
        """Apply the configured budget; a change resets every bucket."""
        with self._lock:
            if (requests_per_minute, burst) == (self._rate_per_minute, self._burst):
                return
            self._rate_per_minute = requests_per_minute
            self._burst = burst
            self._buckets.clear()

    @property
    def requests_per_minute(self) -> int:
        return self._rate_per_minute

    @property
    def burst(self) -> int:
        return self._burst

    # -- accounting ------------------------------------------------------

    def check(self, identity: str, *, now: float | None = None) -> tuple[bool, int]:
        """Consume one token for *identity*.

        Returns ``(allowed, retry_after_s)`` — ``retry_after_s`` is the whole
        number of seconds until one token is available again, at least ``1``.
        """
        timestamp = time.monotonic() if now is None else now
        rate_per_second = max(self._rate_per_minute, 1) / 60.0
        with self._lock:
            bucket = self._buckets.get(identity)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._burst), updated_at=timestamp)
                self._buckets[identity] = bucket
                if len(self._buckets) > self._PRUNE_THRESHOLD:
                    self._prune(timestamp)
            else:
                elapsed = max(timestamp - bucket.updated_at, 0.0)
                bucket.tokens = min(float(self._burst), bucket.tokens + elapsed * rate_per_second)
                bucket.updated_at = timestamp

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0

            missing = 1.0 - bucket.tokens
            retry_after = max(1, math.ceil(missing / rate_per_second))
            return False, retry_after

    def _prune(self, now: float) -> None:
        """Drop buckets that have been idle long enough to be full again."""
        idle_after = max(self._burst / max(self._rate_per_minute, 1) * 60.0, 60.0)
        for identity, bucket in list(self._buckets.items()):
            if now - bucket.updated_at > idle_after and bucket.tokens >= self._burst - 1:
                self._buckets.pop(identity, None)

    def reset(self) -> None:
        """Forget every bucket (tests, and a deliberate operator reset)."""
        with self._lock:
            self._buckets.clear()

    def snapshot(self) -> dict[str, Any]:
        """Return a small, payload-free view for diagnostics/tests."""
        with self._lock:
            return {
                "identities": len(self._buckets),
                "requests_per_minute": self._rate_per_minute,
                "burst": self._burst,
            }


#: The process-wide limiter. One instance, on purpose: the budget belongs to the
#: identity, not to a route or a worker.
RATE_LIMITER = TokenBucketRateLimiter()


def enforce_rate_limit(identity: str | None) -> None:
    """Raise the typed ``429`` when *identity* exhausted its budget.

    Called from :func:`app.proxy.auth.verify_api_key`, i.e. after the key is
    authenticated and authorized and **before** any handler runs — a
    rate-limited request therefore never reaches the inference queue.
    """
    from fastapi import HTTPException

    limits = resolve_limits()
    misconfigured = limits.error(LIMIT_RATE) or limits.error(LIMIT_BURST)
    if misconfigured is not None:
        limit = LIMIT_RATE if limits.error(LIMIT_RATE) else LIMIT_BURST
        _bump_rejection(limit)
        raise HTTPException(status_code=503, detail=misconfigured_detail(limit, misconfigured))

    assert limits.requests_per_minute is not None and limits.burst is not None
    if not identity:
        # No identity could be derived for an authenticated request: fail closed
        # rather than hand out an unlimited bucket.
        _bump_rejection(LIMIT_RATE)
        raise HTTPException(
            status_code=503,
            detail=misconfigured_detail(LIMIT_RATE, "authenticated request without an identity fingerprint"),
        )

    RATE_LIMITER.configure(limits.requests_per_minute, limits.burst)
    allowed, retry_after = RATE_LIMITER.check(identity)
    if allowed:
        return

    _bump_rejection(LIMIT_RATE)
    logger.warning(
        "🛑 rate-limited %s",
        json.dumps(
            {
                "event": "rate_limited",
                "identity": identity,
                "requests_per_minute": limits.requests_per_minute,
                "burst": limits.burst,
                "retry_after_s": retry_after,
            },
            sort_keys=True,
        ),
    )
    raise HTTPException(
        status_code=429,
        detail=rate_limited_detail(
            requests_per_minute=limits.requests_per_minute,
            burst=limits.burst,
            retry_after_s=retry_after,
        ),
        headers={"Retry-After": str(retry_after)},
    )


# ── Request/body size (ASGI middleware, both apps) ─────────────────────


def _declared_length(scope: Mapping[str, Any]) -> int | None:
    """Return the declared ``Content-Length`` of the request, or ``None``."""
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() == b"content-length":
            try:
                value = int(raw_value.decode("latin-1").strip())
            except (TypeError, ValueError, UnicodeDecodeError):
                return None
            return value if value >= 0 else None
    return None


async def _empty_receive() -> dict:
    return {"type": "http.disconnect"}


async def _send_json(scope: Mapping[str, Any], send: Callable, status_code: int, detail: dict) -> None:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    await response(scope, _empty_receive, send)


def _has_transfer_encoding(scope: Mapping[str, Any]) -> bool:
    """Whether the request declares a transfer encoding (i.e. chunked framing).

    RFC 7230: when both ``Transfer-Encoding`` and ``Content-Length`` are present
    the message is malformed, and a server that accepts it frames by the transfer
    encoding (h11/uvicorn do exactly that). A declared length is therefore only
    trustworthy when the body is *not* chunked — see the middleware, which counts
    the bytes instead of trusting the header in that case.
    """
    for raw_name, _raw_value in scope.get("headers") or []:
        if raw_name.lower() == b"transfer-encoding":
            return True
    return False


async def _read_bounded_body(
    receive: Callable[[], Awaitable[dict]],
    limit_bytes: int,
) -> tuple[bytes, int, bool, list[dict], bool]:
    """Read an unlabelled body up to ``limit_bytes``.

    Returns ``(body, seen, overflow, tail)``; ``tail`` holds any non-body message
    the channel produced (a disconnect) so the replay can still deliver it.

    The body is coalesced into one ``bytearray`` instead of a list of ASGI
    messages: a sender that delivers millions of one-byte messages must not be
    able to allocate per message, and replaying a coalesced body is linear.

    On overflow the crossing message is **not** retained and no copy of the
    buffer is made: the caller discards it, so allocating past the bound here
    would hand an unauthenticated sender an allocation primitive (PR-Piet, #63).
    ``finished`` records whether the body actually ended, so a replay can keep a
    disconnect visible instead of pretending the truncated body was complete.
    ``seen`` is the exact number of bytes the channel produced, so the ``413``
    body reports the real size even when the crossing chunk was not retained.
    """
    buffer = bytearray()
    seen = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            # The channel ended before the body did: keep the disconnect in the
            # tail so the replay still delivers it.
            return bytes(buffer), seen, False, [message], False
        chunk = message.get("body") or b""
        # Counted before the append: the crossing chunk is never retained, so a
        # rejected request allocates nothing past the bound (PR-Piet, #63).
        seen += len(chunk)
        if seen > limit_bytes:
            return b"", seen, True, [], False
        buffer += chunk
        if not message.get("more_body", False):
            return bytes(buffer), seen, False, [], True


class _ReplayReceive:
    """Replay a buffered body as one message, then delegate to the real channel.

    A single ``http.request`` message with ``more_body: False`` is behaviourally
    equivalent for every handler on this gateway (they all buffer the body) and
    keeps the replay linear.
    """

    __slots__ = ("_body", "_tail", "_receive", "_sent", "_finished")

    def __init__(
        self,
        body: bytes,
        tail: list[dict],
        receive: Callable[[], Awaitable[dict]],
        *,
        finished: bool = True,
    ) -> None:
        self._body = body
        self._tail = tail
        self._receive = receive
        self._sent = False
        self._finished = finished

    async def __call__(self) -> dict:
        if not self._sent:
            self._sent = True
            # A body that never finished must not look complete: reporting
            # ``more_body: False`` here would let a handler accept a truncated
            # request (a syntactically complete JSON prefix) instead of seeing
            # the disconnect (PR-Piet, #63).
            return {
                "type": "http.request",
                "body": self._body,
                "more_body": not self._finished,
            }
        if self._tail:
            return self._tail.pop(0)
        return await self._receive()


class BodySizeLimitMiddleware:
    """Reject an oversized request before its body reaches a handler.

    Two paths, one error body, and neither of them buffers more than the bound:

    * a declared ``Content-Length`` over the bound is refused without reading a
      byte — but only when no transfer encoding is in play, because a chunked
      body is framed by the encoding even if a (small, wrong) length is present;
    * a chunked or unlabelled body is read up to the bound and refused as soon
      as it crosses it.

    Deliberately **not** an exception raised from ``receive``: handlers on this
    gateway catch broad exceptions while parsing a body (``except Exception`` ->
    ``400``), which would swallow the bound and turn a clean ``413`` into a
    misleading client error. Responding from the middleware cannot be swallowed.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limits = resolve_limits()
        misconfigured = limits.error(LIMIT_BODY)
        if misconfigured is not None:
            _bump_rejection(LIMIT_BODY)
            await _send_json(scope, send, 503, misconfigured_detail(LIMIT_BODY, misconfigured))
            return

        assert limits.max_body_bytes is not None
        limit_bytes = limits.max_body_bytes
        declared = _declared_length(scope)
        # A Content-Length is only trustworthy when the body is not framed by a
        # transfer encoding: with both headers present the encoding wins, so a
        # small declared length next to a chunked body must not skip counting.
        chunked = _has_transfer_encoding(scope)

        if not chunked and declared is not None and declared > limit_bytes:
            await self._refuse(
                scope,
                send,
                body_too_large_detail(limit_bytes, declared_bytes=declared),
                limit_bytes=limit_bytes,
                declared_bytes=declared,
            )
            return

        if chunked or declared is None:
            body, seen, overflow, tail, finished = await _read_bounded_body(
                receive, limit_bytes
            )
            if overflow:
                await self._refuse(
                    scope,
                    send,
                    body_too_large_detail(limit_bytes, observed_bytes=seen),
                    limit_bytes=limit_bytes,
                    observed_bytes=seen,
                )
                return
            receive = _ReplayReceive(body, tail, receive, finished=finished)

        await self.app(scope, receive, send)

    async def _refuse(
        self,
        scope: dict,
        send: Callable,
        detail: dict,
        *,
        limit_bytes: int,
        declared_bytes: int | None = None,
        observed_bytes: int | None = None,
    ) -> None:
        _bump_rejection(LIMIT_BODY)
        logger.warning(
            "🛑 body-too-large %s",
            json.dumps(
                {
                    "event": "request_too_large",
                    "limit_bytes": limit_bytes,
                    "declared_bytes": declared_bytes,
                    "observed_bytes": observed_bytes,
                    "path": scope.get("path"),
                },
                sort_keys=True,
            ),
        )
        await _send_json(scope, send, 413, detail)


def install_body_limit(application: Any) -> None:
    """Install the body bound on a FastAPI/Starlette app (both surfaces).

    Inserted behind the first user middleware (CORS on the proxy app) so an
    oversized browser request still carries the headers the client needs to read
    the typed ``413``, and ahead of the router so the body is never buffered past
    the bound.
    """
    application.user_middleware.insert(1, Middleware(BodySizeLimitMiddleware))
