"""Inference request queue for Guardian middleware.

Serializes access to the single-slot llama-server backend.
Only one inference request is processed at a time; others wait in FIFO order.
Clients can poll GET /v1/queue/status for position info without polluting
the OpenAI-compatible response stream.
"""

import asyncio
import uuid
import time
import logging
from typing import Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger("Queue")


@dataclass
class QueueEntry:
    """Represents a request in the inference queue."""
    request_id: str
    client_id: str
    model: str
    enqueued_at: float
    started_at: Optional[float] = None  # Set when inference begins


class InferenceQueue:
    """FIFO queue serializing access to llama-server.

    - ``max_concurrent`` slots (default 1) can run simultaneously.
    - Waiters block on an ``asyncio.Semaphore``; CPython asyncio uses FIFO
      ordering for semaphore waiters.
    - A parallel tracking list allows the status endpoint to report positions.
    - ``queue_timeout`` caps how long a request waits before getting a slot.
    """

    def __init__(self, max_concurrent: int = 1, queue_timeout: float = 300.0):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._waiting: List[QueueEntry] = []
        self._active: List[QueueEntry] = []
        self._lock = asyncio.Lock()  # Protects _waiting / _active mutations
        self.max_concurrent = max_concurrent
        self.queue_timeout = queue_timeout

        # Lifetime counters
        self._total_queued = 0
        self._total_completed = 0
        self._total_timeouts = 0

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    # ------------------------------------------------------------------
    # Slot acquire / release
    # ------------------------------------------------------------------

    async def acquire(self, client_id: str, model: str) -> str:
        """Enter the queue.  Blocks until a slot is available.

        Returns a ``request_id`` (UUID) that the caller must pass to
        ``release()`` when done.

        Raises ``asyncio.TimeoutError`` if *queue_timeout* is exceeded.
        Raises ``asyncio.CancelledError`` if the client disconnects while
        waiting.
        """
        request_id = str(uuid.uuid4())
        entry = QueueEntry(
            request_id=request_id,
            client_id=client_id,
            model=model,
            enqueued_at=time.time(),
        )

        async with self._lock:
            self._waiting.append(entry)
            self._total_queued += 1
            position = len(self._waiting)

        if position > 1:
            logger.info(
                f"📋 [{request_id[:8]}] Queued at position {position} "
                f"(client: {client_id}, model: {model})"
            )

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.queue_timeout,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                self._waiting = [e for e in self._waiting if e.request_id != request_id]
                self._total_timeouts += 1
            logger.warning(
                f"⏰ [{request_id[:8]}] Queue timeout after {self.queue_timeout}s "
                f"(client: {client_id})"
            )
            raise
        except asyncio.CancelledError:
            async with self._lock:
                self._waiting = [e for e in self._waiting if e.request_id != request_id]
            logger.info(f"🚫 [{request_id[:8]}] Cancelled while queued (client: {client_id})")
            raise

        # Got the slot — move from waiting → active
        entry.started_at = time.time()
        async with self._lock:
            self._waiting = [e for e in self._waiting if e.request_id != request_id]
            self._active.append(entry)

        wait_time = entry.started_at - entry.enqueued_at
        if wait_time > 0.1:
            logger.info(f"🟢 [{request_id[:8]}] Slot acquired after {wait_time:.1f}s wait")

        return request_id

    def release(self, request_id: str) -> float:
        """Release a slot.  Returns total time (enqueue → now) in **ms**.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        total_ms = 0.0
        found = False
        for i, entry in enumerate(self._active):
            if entry.request_id == request_id:
                total_ms = (time.time() - entry.enqueued_at) * 1000
                self._active.pop(i)
                self._total_completed += 1
                found = True
                break

        if found:
            self._semaphore.release()
            logger.debug(f"🔓 [{request_id[:8]}] Released slot ({total_ms:.0f}ms total)")
        return total_ms

    def get_queue_wait_ms(self, request_id: str) -> float:
        """Return how long the request waited in the queue (enqueue → start) in ms."""
        for entry in self._active:
            if entry.request_id == request_id and entry.started_at:
                return (entry.started_at - entry.enqueued_at) * 1000
        return 0.0

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self, client_id: Optional[str] = None) -> dict:
        """Build a status dict for the ``GET /v1/queue/status`` endpoint."""
        result: dict = {
            "queue_length": len(self._waiting),
            "active_count": len(self._active),
            "max_concurrent": self.max_concurrent,
            "queue_timeout_s": self.queue_timeout,
            "stats": {
                "total_queued": self._total_queued,
                "total_completed": self._total_completed,
                "total_timeouts": self._total_timeouts,
            },
        }

        if self._active:
            result["active_requests"] = [
                {
                    "request_id": e.request_id[:8],
                    "client_id": e.client_id,
                    "model": e.model,
                    "elapsed_s": round(time.time() - (e.started_at or e.enqueued_at), 1),
                }
                for e in self._active
            ]

        if self._waiting:
            result["waiting"] = [
                {
                    "position": i + 1,
                    "request_id": e.request_id[:8],
                    "client_id": e.client_id,
                    "model": e.model,
                    "waiting_s": round(time.time() - e.enqueued_at, 1),
                }
                for i, e in enumerate(self._waiting)
            ]

        # Per-client view
        if client_id:
            for i, e in enumerate(self._waiting):
                if e.client_id == client_id:
                    result["your_position"] = i + 1
                    result["your_status"] = "queued"
                    result["your_wait_s"] = round(time.time() - e.enqueued_at, 1)
                    break
            else:
                for e in self._active:
                    if e.client_id == client_id:
                        result["your_position"] = 0
                        result["your_status"] = "processing"
                        result["your_elapsed_s"] = round(
                            time.time() - (e.started_at or e.enqueued_at), 1
                        )
                        break
                else:
                    result["your_position"] = -1
                    result["your_status"] = "idle"

        return result
