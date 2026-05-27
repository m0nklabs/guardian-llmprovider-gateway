"""Tests for app.proxy.queue — FIFO inference queue."""

import asyncio
import time

import pytest

from app.proxy.queue import InferenceQueue, QueueEntry


# ── QueueEntry ─────────────────────────────────────────────────────────


class TestQueueEntry:
    def test_fields(self):
        now = time.time()
        entry = QueueEntry(
            request_id="abc-123",
            client_id="test",
            owner_id="key:test",
            model="GLM-4.7-Flash",
            enqueued_at=now,
        )
        assert entry.request_id == "abc-123"
        assert entry.client_id == "test"
        assert entry.model == "GLM-4.7-Flash"
        assert entry.started_at is None

    def test_started_at_set(self):
        entry = QueueEntry(
            request_id="x", client_id="y", owner_id="key:y", model="m", enqueued_at=1.0, started_at=2.0
        )
        assert entry.started_at == 2.0


# ── InferenceQueue init ───────────────────────────────────────────────


class TestQueueInit:
    def test_defaults(self):
        q = InferenceQueue()
        assert q.max_concurrent == 1
        assert q.queue_timeout == 300.0
        assert q.active_count == 0
        assert q.waiting_count == 0

    def test_custom_params(self):
        q = InferenceQueue(max_concurrent=3, queue_timeout=60.0)
        assert q.max_concurrent == 3
        assert q.queue_timeout == 60.0

    def test_submit_rejects_unauthenticated_client(self):
        q = InferenceQueue()

        with pytest.raises(ValueError, match="authenticated client_id"):
            q.submit("unauthenticated", "model-x")

        assert q.waiting_count == 0
        assert q.active_count == 0

    def test_submit_allows_multiple_queued_requests_for_same_owner(self):
        q = InferenceQueue()
        first_id = q.submit("openclaw", "model-x", owner_id="key:openclaw")
        second_id = q.submit("openclaw", "model-y", owner_id="key:openclaw")

        assert first_id != second_id
        assert q.waiting_count == 2

    @pytest.mark.asyncio
    async def test_same_owner_request_waits_while_owner_is_running(self):
        q = InferenceQueue(max_concurrent=2)
        rid = await q.acquire("openclaw", "model-x", owner_id="key:openclaw")
        acquired = asyncio.Event()

        async def second_request():
            second_id = await q.acquire("openclaw", "model-y", owner_id="key:openclaw")
            acquired.set()
            return second_id

        task = asyncio.create_task(second_request())
        await asyncio.sleep(0.05)

        assert not acquired.is_set()
        assert q.active_count == 1
        assert q.waiting_count == 1

        q.release(rid)
        second_id = await asyncio.wait_for(task, timeout=1.0)
        assert acquired.is_set()
        q.release(second_id)


# ── acquire / release ─────────────────────────────────────────────────


class TestAcquireRelease:
    @pytest.mark.asyncio
    async def test_acquire_returns_uuid(self):
        q = InferenceQueue()
        rid = await q.acquire("client-a", "model-x")
        assert isinstance(rid, str)
        assert len(rid) == 36  # UUID format
        q.release(rid)

    @pytest.mark.asyncio
    async def test_acquire_sets_active(self):
        q = InferenceQueue()
        rid = await q.acquire("client-a", "model-x")
        assert q.active_count == 1
        assert q.waiting_count == 0
        q.release(rid)

    @pytest.mark.asyncio
    async def test_release_clears_active(self):
        q = InferenceQueue()
        rid = await q.acquire("client-a", "model-x")
        total_ms = q.release(rid)
        assert q.active_count == 0
        assert total_ms >= 0

    @pytest.mark.asyncio
    async def test_release_idempotent(self):
        q = InferenceQueue()
        rid = await q.acquire("client-a", "model-x")
        q.release(rid)
        # Second release is a no-op
        total_ms = q.release(rid)
        assert total_ms == 0.0
        assert q.active_count == 0

    @pytest.mark.asyncio
    async def test_counters_increment(self):
        q = InferenceQueue()
        rid = await q.acquire("c", "m")
        assert q._total_queued == 1
        q.release(rid)
        assert q._total_completed == 1

    @pytest.mark.asyncio
    async def test_get_queue_wait_ms(self):
        q = InferenceQueue()
        rid = await q.acquire("c", "m")
        wait_ms = q.get_queue_wait_ms(rid)
        # Should be very small (near-instant acquire)
        assert wait_ms >= 0
        assert wait_ms < 1000  # less than 1 second
        q.release(rid)

    @pytest.mark.asyncio
    async def test_get_queue_wait_ms_unknown_id(self):
        q = InferenceQueue()
        assert q.get_queue_wait_ms("nonexistent") == 0.0


# ── FIFO ordering ─────────────────────────────────────────────────────


class TestFIFOOrdering:
    @pytest.mark.asyncio
    async def test_second_request_waits(self):
        q = InferenceQueue(max_concurrent=1)
        rid1 = await q.acquire("client-a", "model-x")

        # Second acquire should block — start it as a task
        acquired = asyncio.Event()

        async def second():
            rid = await q.acquire("client-b", "model-x")
            acquired.set()
            return rid

        task = asyncio.create_task(second())
        await asyncio.sleep(0.05)  # Give it time to queue up

        assert not acquired.is_set()
        assert q.waiting_count == 1
        assert q.active_count == 1

        # Release first → second should proceed
        q.release(rid1)
        await asyncio.sleep(0.05)
        assert acquired.is_set()

        rid2 = await task
        q.release(rid2)

    @pytest.mark.asyncio
    async def test_fifo_order(self):
        """Three requests should be processed in FIFO order."""
        q = InferenceQueue(max_concurrent=1)
        order: list[str] = []

        rid1 = await q.acquire("first", "m")

        async def worker(name: str):
            rid = await q.acquire(name, "m")
            order.append(name)
            q.release(rid)

        t2 = asyncio.create_task(worker("second"))
        await asyncio.sleep(0.02)
        t3 = asyncio.create_task(worker("third"))
        await asyncio.sleep(0.02)

        # Both should be waiting
        assert q.waiting_count == 2

        order.append("first")
        q.release(rid1)

        await asyncio.gather(t2, t3)
        assert order == ["first", "second", "third"]


# ── Waiting semantics ─────────────────────────────────────────────────


class TestWaitingSemantics:
    @pytest.mark.asyncio
    async def test_waiting_request_is_not_dropped_by_queue_timeout(self):
        q = InferenceQueue(max_concurrent=1, queue_timeout=0.01)
        rid1 = await q.acquire("blocker", "m")

        waiter_task = asyncio.create_task(q.acquire("waiter", "m"))
        await asyncio.sleep(0.05)

        assert not waiter_task.done()
        status = q.get_status(client_id="waiter")
        assert status["your_status"] == "queued"
        assert status["queue_timeout_enforced"] is False
        assert status["stats"]["total_timeouts"] == 0

        q.release(rid1)
        rid2 = await waiter_task
        q.release(rid2)


# ── Cancellation ───────────────────────────────────────────────────────


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_removes_from_waiting(self):
        q = InferenceQueue(max_concurrent=1)
        rid1 = await q.acquire("blocker", "m")

        async def cancelled_req():
            await q.acquire("cancelled", "m")

        task = asyncio.create_task(cancelled_req())
        await asyncio.sleep(0.05)
        assert q.waiting_count == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert q.waiting_count == 0
        q.release(rid1)

    @pytest.mark.asyncio
    async def test_cancel_marks_running_request_cancelling(self):
        q = InferenceQueue(max_concurrent=1)
        rid = await q.acquire("runner", "m")

        status = q.cancel(rid, client_id="runner", reason="client_disconnect")

        assert status is not None
        assert status["status"] == "cancelling"
        assert status["cancel_reason"] == "client_disconnect"
        assert q.is_cancel_requested(rid) is True

        q.finish(rid, outcome="cancelled")
        assert q.get_request_status(rid, client_id="runner")["status"] == "cancelled"


# ── get_status ─────────────────────────────────────────────────────────


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_empty_status(self):
        q = InferenceQueue()
        status = q.get_status()
        assert status["queue_length"] == 0
        assert status["active_count"] == 0
        assert status["max_concurrent"] == 1
        assert status["stats"]["total_queued"] == 0

    @pytest.mark.asyncio
    async def test_active_request_shown(self):
        q = InferenceQueue()
        rid = await q.acquire("test-client", "test-model")
        status = q.get_status()
        assert status["active_count"] == 1
        assert status["active_requests"][0]["client_id"] == "test-client"
        assert status["active_requests"][0]["model"] == "test-model"
        q.release(rid)

    @pytest.mark.asyncio
    async def test_client_status_processing(self):
        q = InferenceQueue()
        rid = await q.acquire("my-client", "m")
        status = q.get_status(client_id="my-client")
        assert status["your_position"] == 0
        assert status["your_status"] == "running"
        assert status["your_request_id"] == rid
        q.release(rid)

    @pytest.mark.asyncio
    async def test_client_status_idle(self):
        q = InferenceQueue()
        status = q.get_status(client_id="nobody")
        assert status["your_position"] == -1
        assert status["your_status"] == "idle"

    @pytest.mark.asyncio
    async def test_client_status_queued(self):
        q = InferenceQueue(max_concurrent=1)
        rid1 = await q.acquire("blocker", "m")

        queued_event = asyncio.Event()

        async def waiter():
            queued_event.set()
            return await q.acquire("waiter-client", "m")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)

        status = q.get_status(client_id="waiter-client")
        assert status["your_position"] == 1
        assert status["your_status"] == "queued"
        assert status["your_requests"][0]["request_id"]

        q.release(rid1)
        rid2 = await task
        q.release(rid2)

    @pytest.mark.asyncio
    async def test_stats_track_lifetime(self):
        q = InferenceQueue(max_concurrent=2)
        ra = await q.acquire("a", "m")
        rb = await q.acquire("b", "m")
        q.release(ra)
        q.finish(rb, outcome="failed")

        status = q.get_status()
        assert status["stats"]["total_queued"] == 2
        assert status["stats"]["total_completed"] == 1
        assert status["stats"]["total_failed"] == 1
        assert status["stats"]["total_timeouts"] == 0

    @pytest.mark.asyncio
    async def test_request_specific_status(self):
        q = InferenceQueue(max_concurrent=1)
        rid = q.submit("client-a", "model-x")

        status = q.get_request_status(rid, client_id="client-a")

        assert status is not None
        assert status["request_id"] == rid
        assert status["status"] == "queued"
        assert status["position"] == 1
