"""Unit tests for the capture sink (bounded async queue)."""

import asyncio
import pytest

from app.capture.sink import CaptureSink, CaptureEvent, SinkMetrics


@pytest.fixture
def sink():
    return CaptureSink(max_pending_events=10)


@pytest.fixture
def event():
    return CaptureEvent(data={"test": "data"})


class TestCaptureEvent:
    def test_serialize_produces_valid_json(self, event):
        line = event.serialize()
        import json
        parsed = json.loads(line)
        assert parsed["test"] == "data"

    def test_serialize_no_trailing_newline(self, event):
        line = event.serialize()
        assert not line.endswith("\n")


class TestSinkTryPut:
    def test_try_put_returns_true_on_success(self, sink, event):
        assert sink.try_put(event) is True
        assert sink.queue_depth == 1

    def test_try_put_drops_when_full(self, sink):
        # Fill the queue (max_pending=10)
        for i in range(10):
            assert sink.try_put(CaptureEvent(data={"i": i})) is True
        # 11th event should be dropped
        assert sink.try_put(CaptureEvent(data={"overflow": True})) is False
        assert sink.metrics.events_dropped_total == 1

    def test_metrics_incremented_correctly(self, sink, event):
        sink.try_put(event)
        sink.try_put(event)
        assert sink.metrics.events_total == 2
        assert sink.metrics.events_dropped_total == 0

    def test_put_on_closed_sink_drops(self, sink, event):
        sink.close()
        assert sink.try_put(event) is False
        assert sink.metrics.events_dropped_total == 1


class TestSinkGet:
    @pytest.mark.asyncio
    async def test_get_returns_events_in_order(self, sink, event):
        sink.try_put(CaptureEvent(data={"seq": 1}))
        sink.try_put(CaptureEvent(data={"seq": 2}))
        first = await sink.get()
        second = await sink.get()
        assert first.data["seq"] == 1
        assert second.data["seq"] == 2

    @pytest.mark.asyncio
    async def test_get_returns_none_when_closed(self, sink):
        sink.close()
        result = await sink.get()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_when_closed_empty(self, sink):
        # Closing an empty queue should make get() return None promptly
        sink.close()
        result = await asyncio.wait_for(sink.get(), timeout=3.0)
        assert result is None


class TestSinkDrain:
    @pytest.mark.asyncio
    async def test_drain_remaining_returns_all_events(self, sink):
        for i in range(5):
            sink.try_put(CaptureEvent(data={"i": i}))
        events = await sink.drain_remaining()
        assert len(events) == 5

    @pytest.mark.asyncio
    async def test_drain_remaining_on_empty_queue(self, sink):
        events = await sink.drain_remaining()
        assert len(events) == 0


class TestSinkMetrics:
    def test_snapshot_returns_dict(self, sink, event):
        sink.try_put(event)
        snap = sink.snapshot()
        assert isinstance(snap, dict)
        assert "metrics" in snap
        assert "max_pending" in snap
        assert "is_closed" in snap

    def test_queue_depth_reflects_actual_state(self, sink, event):
        sink.try_put(event)
        sink.try_put(event)
        assert sink.queue_depth == 2
        snap = sink.snapshot()
        assert snap["metrics"]["guardian_capture_queue_depth"] == 2


class TestSinkBackpressure:
    @pytest.mark.asyncio
    async def test_concurrent_producers_dont_block_inference(self, sink):
        """Stress test: many producers putting events, none should block."""
        event = CaptureEvent(data={"stress": True})

        async def producer():
            for _ in range(1000):
                sink.try_put(event)

        # Run multiple producers concurrently
        await asyncio.gather(*[producer() for _ in range(10)])

        # Queue should have at most max_pending_events entries
        assert sink.queue_depth <= sink.max_pending
        assert sink.metrics.events_dropped_total > 0  # Some should be dropped
