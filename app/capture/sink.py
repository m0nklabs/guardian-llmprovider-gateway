"""Bounded non-blocking event queue for capture events.

The sink decouples event production (request handlers) from event consumption
(the background WAL writer).  It uses a bounded ``asyncio.Queue`` with
``put_nowait`` semantics: when the queue is full, new events are silently
dropped and a ``dropped`` counter is incremented.  This guarantees capture
never blocks inference.

A single background writer task (see :class:`app.capture.wal_writer.CaptureWALWriter`)
consumes from this queue.  The sink itself is transport-agnostic — it just
holds events in memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("Guardian.Capture.Sink")


@dataclass
class CaptureEvent:
    """A single capture event ready for JSONL serialization.

    The ``data`` field is the complete event dict (including schema_name,
    schema_version, event_id, etc. as built by the schema module).
    """

    data: Dict[str, Any]
    priority: int = 0  # 0 = normal event; higher = more important

    def serialize(self) -> str:
        """Serialize to a single JSON line (no trailing newline)."""
        import json
        return json.dumps(self.data, separators=(",", ":"), sort_keys=False, default=str)


@dataclass
class SinkMetrics:
    """Runtime counters for the capture sink."""

    events_total: int = 0
    events_dropped_total: int = 0
    queue_depth: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guardian_capture_events_total": self.events_total,
            "guardian_capture_events_dropped_total": self.events_dropped_total,
            "guardian_capture_queue_depth": self.queue_depth,
        }


class CaptureSink:
    """Bounded, non-blocking async event queue for capture events.

    The sink is designed to be thread-safe within a single asyncio event loop.
    It is not thread-safe across multiple event loops.
    """

    def __init__(
        self,
        max_pending_events: int = 10_000,
    ) -> None:
        self._max_pending = max(max_pending_events, 1)
        self._queue: "asyncio.Queue[Optional[CaptureEvent]]" = asyncio.Queue(
            maxsize=self._max_pending
        )
        self._metrics = SinkMetrics()
        self._closed = False
        self._consumers: int = 0

    def register_consumer(self) -> None:
        """Mark that a background consumer is active (for metrics/debugging)."""
        self._consumers += 1

    def unregister_consumer(self) -> None:
        self._consumers = max(0, self._consumers - 1)

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @property
    def metrics(self) -> SinkMetrics:
        """Return a snapshot of the current metrics."""
        self._metrics.queue_depth = self._queue.qsize()
        return self._metrics

    @property
    def queue_depth(self) -> int:
        """Current number of events waiting in the queue."""
        return self._queue.qsize()

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def has_consumer(self) -> bool:
        return self._consumers > 0

    def try_put(self, event: CaptureEvent) -> bool:
        """Attempt to enqueue an event without blocking.

        Returns True if the event was enqueued, False if the queue was full
        (the event was dropped and the dropped counter was incremented).
        """
        if self._closed:
            self._metrics.events_dropped_total += 1
            return False

        try:
            self._queue.put_nowait(event)
            self._metrics.events_total += 1
            self._metrics.queue_depth = self._queue.qsize()
            return True
        except asyncio.QueueFull:
            self._metrics.events_dropped_total += 1
            logger.debug(
                "Capture sink full (%d events) — dropping event (total dropped: %d)",
                self._queue.qsize(),
                self._metrics.events_dropped_total,
            )
            return False

    async def put(self, event: CaptureEvent) -> bool:
        """Async put — tries non-blocking first, falls back to bounded wait.

        This is the preferred method for non-hot-path callers.  The request
        handlers use :meth:`try_put` to guarantee they never block.
        """
        return self.try_put(event)

    async def get(self) -> Optional[CaptureEvent]:
        """Retrieve the next event from the queue.

        Returns None when the sink is closed and drained.
        """
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                if item is None:
                    return None
                return item
            except asyncio.TimeoutError:
                if self._closed:
                    # Drain remaining items
                    try:
                        item = self._queue.get_nowait()
                        if item is None:
                            return None
                        return item
                    except asyncio.QueueEmpty:
                        return None

    async def drain_remaining(self) -> list:
        """Drain all currently-queued events without blocking (for shutdown)."""
        events: list = []
        while True:
            try:
                item = self._queue.get_nowait()
                if item is None:
                    continue
                events.append(item)
            except asyncio.QueueEmpty:
                break
        return events

    def close(self) -> None:
        """Signal that no more events will be produced (shutdown)."""
        self._closed = True
        # Put a sentinel to unblock any waiting get()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def snapshot(self) -> Dict[str, Any]:
        """Return a metrics snapshot for Prometheus/gauges."""
        self._metrics.queue_depth = self._queue.qsize()
        return {
            "metrics": self._metrics.to_dict(),
            "max_pending": self._max_pending,
            "is_closed": self._closed,
            "has_consumer": self._consumers > 0,
        }
