"""Inference request queue for Guardian middleware.

Serializes access to the single-slot llama-server backend while keeping a real
request lifecycle: queued, running, cancelling, cancelled, completed, failed,
or expired. A queued request that holds a live connection is bounded by the
configured ``queue_timeout`` wait budget (``queue.queue_timeout_seconds``): once
it elapses the request finalizes as ``expired`` with ``cancel_reason:
"queue_timeout"`` and the waiting client receives the usual typed cancellation.
A non-positive (or non-finite) budget disables enforcement and restores the
unbounded wait, in which case the request stays queued until it disconnects or
is cancelled.

**Waiting depth** is bounded independently (G1-03): ``max_queue_depth`` caps how
many requests may be *waiting*; a submission beyond it raises
:class:`QueueDepthExceeded` before any entry is registered and is reported to
the client as the stable ``503 queue_full`` (``app/proxy/admission.py``). The
two bounds answer different questions — the depth bound keeps Guardian from
accumulating waiting clients, the wait budget keeps one waiting client from
waiting forever — and neither replaces the other.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.log_safety import clean_log_value

logger = logging.getLogger("Queue")

FINAL_STATES = {"completed", "cancelled", "failed", "expired"}
ACTIVE_STATES = {"running", "cancelling"}

#: Shipped default wait budget for a queued request, mirrored by
#: ``queue.queue_timeout_seconds`` in ``config/global.settings.yaml``.
DEFAULT_QUEUE_TIMEOUT_S = 1800.0

#: Shipped default bound on *waiting* requests (G1-03), mirrored by
#: ``admission.queue_max_depth`` in ``config/global.settings.yaml``. Thirty-two
#: times the highest depth ever measured (``docs/G0_BASELINE.md`` §2.5: depth 1).
DEFAULT_QUEUE_MAX_DEPTH = 32

#: Cancel reason recorded when a queued request exceeds its wait budget.
QUEUE_TIMEOUT_REASON = "queue_timeout"


def _normalize_queue_timeout(queue_timeout: object) -> float:
    """Return the enforced queue wait budget in seconds.

    ``0``, negative values, ``None``, non-finite values (``nan``, ``inf``) and
    non-numeric input all mean "no queue timeout" and normalize to ``0.0``, so
    the advertised enforcement flag and the status payload stay finite.
    """
    if queue_timeout is None or isinstance(queue_timeout, bool):
        return 0.0
    try:
        timeout = float(queue_timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not 0 < timeout < float("inf"):  # catches 0, negatives and non-finite values
        return 0.0
    return timeout


def _normalize_client_id(client_id: object) -> str | None:
    """Return a safe queue client id, or ``None`` when the caller is not authenticated."""
    if not isinstance(client_id, str):
        return None
    normalized = client_id.strip()
    if not normalized:
        return None
    if normalized.lower() == "unauthenticated":
        return None
    return normalized


def _normalize_owner_id(owner_id: object, fallback_client_id: object = None) -> str | None:
    """Return the queue ownership identity, falling back to the client name when needed."""
    if isinstance(owner_id, str):
        normalized_owner = owner_id.strip()
        if normalized_owner:
            return normalized_owner
    return _normalize_client_id(fallback_client_id)


class QueueDepthExceeded(Exception):
    """Raised when the waiting queue is at its configured bound.

    ``reason`` is the stable error name the HTTP layer reports: ``queue_full``
    for a reached bound, ``admission_misconfigured`` when the bound could not be
    resolved at startup (``max_queue_depth is None``) — the request is refused
    either way, so a broken configuration can never mean "unbounded".
    """

    def __init__(
        self,
        *,
        max_queue_depth: int | None,
        queue_length: int,
        max_concurrent: int,
        reason: str = "queue_full",
    ):
        super().__init__(
            f"queue admission refused: {queue_length} waiting, limit {max_queue_depth}"
        )
        self.max_queue_depth = max_queue_depth
        self.queue_length = queue_length
        self.max_concurrent = max_concurrent
        self.reason = reason


class QueueRequestCancelled(Exception):
    """Raised when a queued request is cancelled before it can run."""

    def __init__(self, request_id: str, reason: str = "cancelled"):
        super().__init__(f"request {request_id} was cancelled: {reason}")
        self.request_id = request_id
        self.reason = reason


@dataclass
class QueueEntry:
    """Represents a request tracked by Guardian's inference queue."""

    request_id: str
    client_id: str
    owner_id: str = field(repr=False)
    model: str
    enqueued_at: float
    status: str = "queued"
    started_at: float | None = None
    completed_at: float | None = None
    cancel_requested_at: float | None = None
    cancel_reason: str | None = None
    detail: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    def snapshot(self, now: float | None = None, position: int | None = None) -> dict:
        """Return a client-facing status payload for this queue entry."""
        now = now or time.time()
        payload = {
            "request_id": self.request_id,
            "client_id": self.client_id,
            "model": self.model,
            "status": self.status,
            "enqueued_at": self.enqueued_at,
        }
        if position is not None:
            payload["position"] = position
        if self.started_at is not None:
            queue_wait_s = max(self.started_at - self.enqueued_at, 0.0)
            payload["started_at"] = self.started_at
            payload["queue_wait_s"] = round(queue_wait_s, 1)
            payload["queue_wait_ms"] = round(queue_wait_s * 1000.0, 1)
        if self.status == "queued":
            payload["waiting_s"] = round(max(now - self.enqueued_at, 0.0), 1)
        if self.status in ACTIVE_STATES:
            payload["elapsed_s"] = round(max(now - (self.started_at or self.enqueued_at), 0.0), 1)
        if self.cancel_requested_at is not None:
            payload["cancel_requested_at"] = self.cancel_requested_at
        if self.completed_at is not None:
            payload["completed_at"] = self.completed_at
            payload["total_s"] = round(max(self.completed_at - self.enqueued_at, 0.0), 1)
        if self.cancel_reason:
            payload["cancel_reason"] = self.cancel_reason
        if self.detail:
            payload["detail"] = self.detail
        return payload


class InferenceQueue:
    """FIFO queue serializing access to llama-server with explicit request state."""

    def __init__(
        self,
        max_concurrent: int = 1,
        queue_timeout: float | None = DEFAULT_QUEUE_TIMEOUT_S,
        history_ttl: float = 300.0,  # more aggressive pruning (was 900); overridable via settings.yaml queue.history_ttl
        max_queue_depth: int | None = DEFAULT_QUEUE_MAX_DEPTH,
    ):
        self.max_concurrent = max_concurrent
        # None means "the configured bound could not be resolved": refuse, never
        # fall back to unbounded (see QueueDepthExceeded).
        if max_queue_depth is not None and (
            isinstance(max_queue_depth, bool) or not isinstance(max_queue_depth, int) or max_queue_depth < 1
        ):
            raise ValueError(
                f"max_queue_depth must be a positive integer or None, got {max_queue_depth!r}"
            )
        self.max_queue_depth = max_queue_depth
        # Normalized once here: 0.0 means "no queue timeout" (not enforced).
        self.queue_timeout = _normalize_queue_timeout(queue_timeout)
        self.history_ttl = history_ttl

        self._waiting: list[str] = []
        self._active: list[str] = []
        self._entries: dict[str, QueueEntry] = {}
        self._change_event = asyncio.Event()
        # Guards the async reserve path in wait_for_turn() so two parallel waiters
        # cannot both cross len(_active) < max_concurrent and grab a slot (double
        # GPU model load -> CUDA OOM). Sync mutators (submit/cancel/finish/release)
        # are inherently atomic (no await = no interleaving) and need no lock.
        self._lock = asyncio.Lock()

        self._total_queued = 0
        self._total_completed = 0
        self._total_timeouts = 0
        self._total_cancelled = 0
        self._total_failed = 0
        self._total_expired = 0

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    @property
    def queue_timeout_enforced(self) -> bool:
        """Whether the configured wait budget is actually enforced."""
        return self.queue_timeout > 0

    def _signal_change(self) -> None:
        current = self._change_event
        current.set()
        self._change_event = asyncio.Event()

    def _prune_history(self) -> None:
        if self.history_ttl <= 0:
            return
        cutoff = time.time() - self.history_ttl
        for request_id, entry in list(self._entries.items()):
            if request_id in self._waiting or request_id in self._active:
                continue
            if entry.completed_at is None or entry.completed_at >= cutoff:
                continue
            self._entries.pop(request_id, None)

    def _get_entry(self, request_id: str) -> QueueEntry | None:
        self._prune_history()
        return self._entries.get(request_id)

    def _queue_wait_remaining(self, entry: QueueEntry) -> float | None:
        """Return seconds left on *entry*'s wait budget, or ``None`` when unenforced."""
        if not self.queue_timeout_enforced:
            return None
        return max(entry.enqueued_at + self.queue_timeout - time.time(), 0.0)

    def _expire_queued_request(self, request_id: str) -> str:
        """Finalize a queued request whose wait budget elapsed; return its cancel reason.

        Reuses ``finish(outcome="timeout")`` so the terminal state (``expired``),
        the ``total_timeouts``/``total_expired`` counters and the
        ``cancel_reason`` vocabulary stay exactly as the queue already defines
        them. When another path (client cancel, disconnect) finalized the entry
        first, that reason wins and nothing is counted twice.
        """
        entry = self._get_entry(request_id)
        if entry is None:
            return "unknown_request"
        if entry.status != "queued":
            return entry.cancel_reason or entry.status
        self.finish(request_id, outcome="timeout")
        reason = entry.cancel_reason or QUEUE_TIMEOUT_REASON
        logger.info(
            "⏰ [%s] Queue wait budget of %.0fs exceeded — cancelled (%s)",
            request_id[:8],
            self.queue_timeout,
            reason,
        )
        return reason

    def _owner_has_active_request(self, owner_id: str) -> bool:
        """Return whether *owner_id* already owns a running GPU slot."""
        for active_request_id in self._active:
            entry = self._entries.get(active_request_id)
            if entry is None:
                continue
            if entry.owner_id == owner_id and entry.status in ACTIVE_STATES:
                return True
        return False

    def submit(
        self,
        client_id: str,
        model: str,
        request_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> str:
        """Register a new queued request and return its request id immediately."""
        self._prune_history()
        normalized_client_id = _normalize_client_id(client_id)
        if normalized_client_id is None:
            raise ValueError("authenticated client_id required for queue submission")
        normalized_owner_id = _normalize_owner_id(owner_id, normalized_client_id)
        if normalized_owner_id is None:
            raise ValueError("authenticated owner_id required for queue submission")

        if self.max_queue_depth is None:
            raise QueueDepthExceeded(
                max_queue_depth=None,
                queue_length=len(self._waiting),
                max_concurrent=self.max_concurrent,
                reason="admission_misconfigured",
            )
        if len(self._waiting) >= self.max_queue_depth:
            raise QueueDepthExceeded(
                max_queue_depth=self.max_queue_depth,
                queue_length=len(self._waiting),
                max_concurrent=self.max_concurrent,
            )

        request_id = request_id or str(uuid.uuid4())
        if request_id in self._entries:
            raise ValueError(f"request_id '{request_id}' is already in use")

        entry = QueueEntry(
            request_id=request_id,
            client_id=normalized_client_id,
            owner_id=normalized_owner_id,
            model=model,
            enqueued_at=time.time(),
        )
        self._entries[request_id] = entry
        self._waiting.append(request_id)
        self._total_queued += 1

        position = len(self._waiting)
        if position > 1:
            logger.info(
                "📋 [%s] Queued at position %s (client: %s, model: %s)",
                request_id[:8],
                position,
                clean_log_value(normalized_client_id),
                clean_log_value(model),
            )
        self._signal_change()
        return request_id

    async def wait_for_turn(self, request_id: str) -> str:
        """Block until the request reaches the front of the queue and can run."""
        while True:
            entry = self._get_entry(request_id)
            if entry is None:
                raise QueueRequestCancelled(request_id, "unknown_request")

            if entry.status in FINAL_STATES:
                raise QueueRequestCancelled(request_id, entry.cancel_reason or entry.status)

            if entry.cancel_event.is_set():
                self.cancel(request_id, reason=entry.cancel_reason or "cancelled")
                raise QueueRequestCancelled(request_id, entry.cancel_reason or "cancelled")

            # Wait budget: a live connection does not buy an unbounded wait, and
            # an elapsed budget wins over a slot that frees at the same instant.
            remaining = self._queue_wait_remaining(entry)
            if remaining is not None and remaining <= 0:
                raise QueueRequestCancelled(request_id, self._expire_queued_request(request_id))

            async with self._lock:
                if (
                    entry.status == "queued"
                    and self._waiting
                    and self._waiting[0] == request_id
                    and len(self._active) < self.max_concurrent
                    and not self._owner_has_active_request(entry.owner_id)
                ):
                    self._waiting.pop(0)
                    entry.status = "running"
                    entry.started_at = time.time()
                    self._active.append(request_id)
                    wait_time = entry.started_at - entry.enqueued_at
                    if wait_time > 0.1:
                        logger.info("🟢 [%s] Slot acquired after %.1fs wait", request_id[:8], wait_time)
                    self._signal_change()
                    return request_id

            wait_event = self._change_event
            wait_task = asyncio.create_task(wait_event.wait())
            cancel_task = asyncio.create_task(entry.cancel_event.wait())
            try:
                try:
                    done, pending = await asyncio.wait(
                        {wait_task, cancel_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    self.cancel(request_id, reason="wait_cancelled")
                    raise
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if cancel_task in done and entry.cancel_event.is_set():
                    self.cancel(request_id, reason=entry.cancel_reason or "cancelled")
            finally:
                if not wait_task.done():
                    wait_task.cancel()
                if not cancel_task.done():
                    cancel_task.cancel()

    async def acquire(self, client_id: str, model: str, *, owner_id: str | None = None) -> str:
        """Legacy convenience wrapper: submit then wait for execution."""
        request_id = self.submit(client_id, model, owner_id=owner_id)
        await self.wait_for_turn(request_id)
        return request_id

    def cancel(
        self,
        request_id: str,
        *,
        client_id: str | None = None,
        owner_id: str | None = None,
        reason: str = "cancelled",
        detail: str | None = None,
    ) -> dict | None:
        """Cancel a queued request or request cancellation of a running one."""
        entry = self._get_entry(request_id)
        if entry is None:
            return None
        normalized_owner_id = _normalize_owner_id(owner_id)
        if normalized_owner_id is not None and entry.owner_id != normalized_owner_id:
            return None
        normalized_client_id = _normalize_client_id(client_id) if client_id is not None else None
        if normalized_owner_id is None and normalized_client_id is not None and entry.client_id != normalized_client_id:
            return None

        now = time.time()
        entry.cancel_reason = reason
        if detail:
            entry.detail = detail
        entry.cancel_event.set()

        if entry.status == "queued":
            if request_id in self._waiting:
                self._waiting.remove(request_id)
            entry.status = "cancelled"
            entry.cancel_requested_at = now
            entry.completed_at = now
            self._total_cancelled += 1
            logger.info("🚫 [%s] Cancelled while queued (%s)", request_id[:8], reason)
        elif entry.status in ACTIVE_STATES or entry.status == "running":
            entry.status = "cancelling"
            entry.cancel_requested_at = now
            logger.info("🛑 [%s] Cancellation requested while running (%s)", request_id[:8], reason)

        self._signal_change()
        return self.get_request_status(request_id, client_id=client_id, owner_id=owner_id)

    def finish(self, request_id: str, outcome: str = "completed", detail: str | None = None) -> float:
        """Finalize a request and free its slot if it was running."""
        entry = self._get_entry(request_id)
        if entry is None:
            return 0.0

        if entry.completed_at is not None and entry.status in FINAL_STATES:
            return 0.0

        if request_id in self._waiting:
            self._waiting.remove(request_id)
        if request_id in self._active:
            self._active.remove(request_id)

        now = time.time()
        entry.completed_at = now
        if detail:
            entry.detail = detail

        if outcome == "cancelled":
            entry.status = "cancelled"
            entry.cancel_requested_at = entry.cancel_requested_at or now
            entry.cancel_reason = entry.cancel_reason or detail or "cancelled"
            self._total_cancelled += 1
        elif outcome == "failed":
            entry.status = "failed"
            self._total_failed += 1
        elif outcome == "expired":
            entry.status = "expired"
            self._total_expired += 1
        elif outcome == "timeout":
            entry.status = "expired"
            entry.cancel_reason = entry.cancel_reason or QUEUE_TIMEOUT_REASON
            self._total_timeouts += 1
            self._total_expired += 1
        else:
            entry.status = "completed"
            self._total_completed += 1

        self._signal_change()
        total_ms = max((now - entry.enqueued_at) * 1000.0, 0.0)
        logger.debug("🔓 [%s] Finalized as %s (%.0fms total)", request_id[:8], entry.status, total_ms)
        return total_ms

    def release(self, request_id: str) -> float:
        """Legacy alias for ``finish(..., outcome='completed')``."""
        return self.finish(request_id, outcome="completed")

    def get_cancel_event(self, request_id: str) -> asyncio.Event | None:
        """Return the cancellation event for a tracked request, if present."""
        entry = self._get_entry(request_id)
        return entry.cancel_event if entry is not None else None

    def is_cancel_requested(self, request_id: str) -> bool:
        """Return whether the request has a pending cancellation signal."""
        event = self.get_cancel_event(request_id)
        return event.is_set() if event is not None else False

    def get_queue_wait_ms(self, request_id: str) -> float:
        """Return how long the request waited in the queue before it started, in ms."""
        entry = self._get_entry(request_id)
        if entry is None or entry.started_at is None:
            return 0.0
        return max((entry.started_at - entry.enqueued_at) * 1000.0, 0.0)

    def get_request_status(
        self,
        request_id: str,
        client_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict | None:
        """Return a request-specific status payload, if visible to the caller."""
        entry = self._get_entry(request_id)
        if entry is None:
            return None
        normalized_owner_id = _normalize_owner_id(owner_id)
        if normalized_owner_id is not None and entry.owner_id != normalized_owner_id:
            return None
        normalized_client_id = _normalize_client_id(client_id) if client_id is not None else None
        if normalized_owner_id is None and normalized_client_id is not None and entry.client_id != normalized_client_id:
            return None

        position = None
        if request_id in self._waiting:
            position = self._waiting.index(request_id) + 1
        elif request_id in self._active:
            position = 0
        else:
            position = -1
        return entry.snapshot(now=time.time(), position=position)

    def get_status(self, client_id: str | None = None, owner_id: str | None = None) -> dict:
        """Build a status dict for the ``GET /v1/queue/status`` endpoint."""
        self._prune_history()
        now = time.time()
        result: dict = {
            "queue_length": len(self._waiting),
            "active_count": len(self._active),
            "max_concurrent": self.max_concurrent,
            "max_queue_depth": self.max_queue_depth,
            "queue_timeout_s": self.queue_timeout,
            # Derived from the configured budget: 0 (or absent/negative, which
            # normalize to 0) means the wait is unbounded and thus unenforced.
            "queue_timeout_enforced": self.queue_timeout_enforced,
            "wait_policy": "disconnect_or_cancel",
            "stats": {
                "total_queued": self._total_queued,
                "total_completed": self._total_completed,
                "total_timeouts": self._total_timeouts,
                "total_cancelled": self._total_cancelled,
                "total_failed": self._total_failed,
                "total_expired": self._total_expired,
            },
        }

        if self._active:
            result["active_requests"] = [
                self._entries[request_id].snapshot(now=now, position=0)
                for request_id in self._active
                if request_id in self._entries
            ]

        if self._waiting:
            result["waiting"] = [
                self._entries[request_id].snapshot(now=now, position=index + 1)
                for index, request_id in enumerate(self._waiting)
                if request_id in self._entries
            ]

        normalized_owner_id = _normalize_owner_id(owner_id)
        normalized_client_id = _normalize_client_id(client_id) if client_id is not None else None

        if normalized_owner_id or normalized_client_id:
            def _matches_request_owner(entry: QueueEntry) -> bool:
                if normalized_owner_id is not None:
                    return entry.owner_id == normalized_owner_id
                return entry.client_id == normalized_client_id

            your_requests = [
                entry.snapshot(
                    now=now,
                    position=(self._waiting.index(entry.request_id) + 1)
                    if entry.request_id in self._waiting
                    else (0 if entry.request_id in self._active else -1),
                )
                for entry in self._entries.values()
                if _matches_request_owner(entry)
            ]
            your_requests.sort(key=lambda item: item["enqueued_at"], reverse=True)
            result["your_requests"] = your_requests

            active_match = next((item for item in your_requests if item["status"] in ACTIVE_STATES), None)
            queued_match = next((item for item in your_requests if item["status"] == "queued"), None)
            latest_match = active_match or queued_match or (your_requests[0] if your_requests else None)

            if latest_match is None:
                result["your_position"] = -1
                result["your_status"] = "idle"
            else:
                result["your_position"] = latest_match.get("position", -1)
                result["your_status"] = latest_match["status"]
                result["your_request_id"] = latest_match["request_id"]
                if "waiting_s" in latest_match:
                    result["your_wait_s"] = latest_match["waiting_s"]
                if "elapsed_s" in latest_match:
                    result["your_elapsed_s"] = latest_match["elapsed_s"]
                if "cancel_reason" in latest_match:
                    result["your_cancel_reason"] = latest_match["cancel_reason"]

        return result
