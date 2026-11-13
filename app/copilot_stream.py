"""Cost-neutral disconnect-aware inference and SSE lifecycle for Copilot.

Only a single pending upstream read exists. Heartbeats are SSE comments, not
model requests, and never reset the upstream idle deadline. The response
subclass also owns cleanup when ASGI disconnects before iteration starts or
while sending a chunk (neither path reliably closes a stock async generator).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

logger = logging.getLogger("guardian.copilot_bridge")
ErrorPayload = Callable[[Exception], dict[str, Any]]


def _consume_task(task: asyncio.Task) -> None:
    """Retrieve failures without logging potentially credential-bearing text."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("Copilot stream cleanup failed (%s)", type(exc).__name__)


async def _next_chunk(iterator: AsyncIterator) -> Any:
    return await anext(iterator)


async def _close_target(target: Any) -> None:
    await target.aclose()


async def _join_cleanup(task: asyncio.Task) -> None:
    """Join the same cleanup even under repeated raw Task.cancel() calls.

    AnyIO shields cancel scopes; asyncio.shield protects the independently owned
    child from direct cancellation of this waiter. Remember raw cancellation and
    propagate it only after that child finishes (within its original deadline).
    """
    cancellation: asyncio.CancelledError | None = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancellation = exc
        try:
            task.result()
        finally:
            if cancellation is not None:
                raise cancellation


class _Cleanup:
    """One independently-owned operation and deadline shared by every caller."""

    def __init__(self, operation: Callable[[float], Awaitable[None]], seconds: float,
                 *, deadline: float | None = None):
        self.operation = operation
        self.seconds = seconds
        self.deadline = deadline
        self.task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self.task is None:
            if self.deadline is None:
                self.deadline = asyncio.get_running_loop().time() + self.seconds
            self.task = asyncio.create_task(self.run(), name="copilot-stream-cleanup")
            self.task.add_done_callback(_consume_task)
        return self.task

    async def run(self) -> None:
        with anyio.CancelScope(shield=True):
            await self.operation(self.deadline)

    async def wait(self) -> None:
        await _join_cleanup(self.start())


async def _finish_tasks(tasks: set[asyncio.Task], deadline: float) -> None:
    """Bound child operations without waiting forever for cancellation to finish."""
    for task in tasks:
        task.add_done_callback(_consume_task)
    _, pending = await asyncio.wait(
        tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()))
    for task in pending:
        task.cancel()
    if pending:
        logger.warning("Copilot cleanup exceeded cleanup budget")


class ClientDisconnected(ClientDisconnect):
    """The client abandoned inference before a response was available."""


class _InferenceSession:
    def __init__(self, awaitable: Awaitable, cleanup_seconds: float):
        self.awaitable = awaitable
        self.cleanup_seconds = cleanup_seconds
        self.result: Any = None
        self.abandoned = False
        self.result_cleanup: _Cleanup | None = None
        self.abandonment_cleanup: _Cleanup | None = None
        self.deadline: float | None = None

    async def run(self) -> Any:
        try:
            self.result = await self.awaitable
            return self.result
        finally:
            if self.abandoned:
                await self.close_result()

    def start_result_close(self) -> asyncio.Task | None:
        if not callable(getattr(self.result, "aclose", None)):
            return None
        if self.result_cleanup is None:
            # A result that first appears AFTER abandonment's deadline needs a
            # new bounded orphan close. Duplicate calls still share that task.
            deadline = self.deadline
            if deadline is not None and deadline <= asyncio.get_running_loop().time():
                deadline = None
            self.result_cleanup = _Cleanup(
                self._close_result, self.cleanup_seconds, deadline=deadline)
        return self.result_cleanup.start()

    async def _close_result(self, deadline: float) -> None:
        task = asyncio.create_task(_close_target(self.result), name="copilot-stream-close")
        await _finish_tasks({task}, deadline)

    async def close_result(self) -> None:
        task = self.start_result_close()
        if task is not None:
            await _join_cleanup(task)

    async def abandon(self, inference: asyncio.Task, watcher: asyncio.Task) -> None:
        self.abandoned = True
        if self.abandonment_cleanup is None:
            self.abandonment_cleanup = _Cleanup(
                lambda deadline: self._abandon(inference, watcher, deadline),
                self.cleanup_seconds, deadline=self.deadline)
        await self.abandonment_cleanup.wait()

    async def _abandon(self, inference: asyncio.Task, watcher: asyncio.Task,
                       deadline: float) -> None:
        self.deadline = deadline
        if not watcher.done():
            watcher.cancel()
        if not inference.done():
            inference.cancel()
        await _finish_tasks({watcher, inference}, deadline)
        if inference.done():
            task = self.start_result_close()
            if task is not None and deadline > asyncio.get_running_loop().time():
                await _join_cleanup(task)


async def _wait_for_disconnect(request: Request) -> None:
    # Caller has fully consumed the payload. A blocking ASGI receive is the
    # notification mechanism, not is_disconnected() polling which can steal body.
    while True:
        if (await request.receive())["type"] == "http.disconnect":
            return


async def await_inference(request: Request, awaitable: Awaitable, *, cleanup_seconds: float) -> Any:
    """Await exactly one inference, abandoning it when its client disconnects.

    Invoke ONLY after the request body has been fully consumed, and before any
    response owns ``receive``. Success transfers ownership of the result to the
    caller; disconnect raises ``ClientDisconnected`` and cancellation propagates.
    Any result acquired in a disconnect/cancellation race is closed, not returned.
    Cancellation-resistant upstream code is observed and late results are closed
    without making the abandoned request wait beyond its cleanup budget.
    """
    if not math.isfinite(cleanup_seconds) or cleanup_seconds <= 0:
        raise ValueError("Inference cleanup interval must be positive and finite")
    session = _InferenceSession(awaitable, cleanup_seconds)
    inference = asyncio.create_task(session.run(), name="copilot-stream-inference")
    watcher = asyncio.create_task(_wait_for_disconnect(request), name="copilot-stream-disconnect")
    transferred = False
    try:
        await asyncio.wait({inference, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher.done():
            watcher.result()
            raise ClientDisconnected()
        watcher.cancel()
        session.deadline = asyncio.get_running_loop().time() + cleanup_seconds
        with anyio.CancelScope(shield=True):
            await asyncio.wait({watcher}, timeout=cleanup_seconds)
        if not watcher.done():
            raise RuntimeError("Copilot disconnect watcher failed to stop")
        if not watcher.cancelled():
            watcher.result()
            raise ClientDisconnected()
        await anyio.lowlevel.checkpoint_if_cancelled()
        result = inference.result()
        transferred = True
        return result
    finally:
        # No await after successful ownership transfer: cancellation at that
        # checkpoint would otherwise lose the result before the caller gets it.
        if not transferred:
            await session.abandon(inference, watcher)


class _StreamSession:
    def __init__(self, result: Any, *, heartbeat_seconds: float, idle_seconds: float,
                 cleanup_seconds: float, error_payload: ErrorPayload):
        for value in (heartbeat_seconds, idle_seconds, cleanup_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Stream intervals must be positive and finite")
        self.result = result
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_seconds = idle_seconds
        self.cleanup_seconds = cleanup_seconds
        self.error_payload = error_payload
        self.iterator: AsyncIterator | None = None
        self.pending: asyncio.Task | None = None
        self.cleanup = _Cleanup(self._close, cleanup_seconds)

    async def close(self) -> None:
        """All callers join one owned cleanup task and its original deadline.

        Raw/AnyIO cancellation never interrupts cleanup management. A coroutine
        that ignores cancellation can still outlive the deadline, but its eventual
        result is observed and it cannot keep the ASGI response waiting forever.
        """
        await self.cleanup.wait()

    async def _close(self, deadline: float) -> None:
        tasks: set[asyncio.Task] = set()
        if self.pending is not None:
            task, self.pending = self.pending, None
            if not task.done():
                task.cancel()
                # Allow cooperative read cancellation before closing a generator;
                # result.aclose may itself be what releases a stuck I/O read.
                await asyncio.wait({task}, timeout=0)
            tasks.add(task)
        if callable(getattr(self.result, "aclose", None)):
            tasks.add(asyncio.create_task(
                _close_target(self.result), name="copilot-stream-close"))
        if tasks:
            await _finish_tasks(tasks, deadline)
        if (self.iterator is not None and self.iterator is not self.result
                and callable(getattr(self.iterator, "aclose", None))):
            if deadline <= asyncio.get_running_loop().time():
                logger.warning("Copilot stream iterator close exceeded cleanup budget")
                return
            task = asyncio.create_task(
                _close_target(self.iterator), name="copilot-stream-close")
            await _finish_tasks({task}, deadline)

    async def events(self) -> AsyncIterator[str]:
        succeeded = False
        try:
            self.iterator = aiter(self.result)
            loop = asyncio.get_running_loop()
            next_heartbeat = loop.time() + self.heartbeat_seconds
            while True:
                self.pending = asyncio.create_task(
                    _next_chunk(self.iterator), name="copilot-stream-read")
                idle_deadline = loop.time() + self.idle_seconds
                while not self.pending.done():
                    now = loop.time()
                    if now >= idle_deadline:
                        raise TimeoutError("Copilot upstream stream idle deadline exceeded")
                    await asyncio.wait({self.pending}, timeout=max(
                        0, min(next_heartbeat, idle_deadline) - now))
                    if self.pending.done():
                        break
                    now = loop.time()
                    if now >= idle_deadline:
                        raise TimeoutError("Copilot upstream stream idle deadline exceeded")
                    if now >= next_heartbeat:
                        next_heartbeat = now + self.heartbeat_seconds
                        yield ": guardian-keepalive\n\n"
                task, self.pending = self.pending, None
                try:
                    chunk = task.result()
                except StopAsyncIteration:
                    succeeded = True
                    break
                # Preserve LiteLLM's JSON (including tools, usage and finish_reason).
                yield "data: " + chunk.model_dump_json(exclude_none=True) + "\n\n"
                next_heartbeat = loop.time() + self.heartbeat_seconds
        except Exception as exc:
            # The bridge owns classification/sanitization; never serialize exc.
            try:
                payload = self.error_payload(exc)
                encoded = json.dumps(payload, separators=(",", ":"))
            except Exception:
                encoded = '{"error":{"message":"Copilot upstream stream failed","type":"upstream_error","code":"upstream_error"}}'
            yield "data: " + encoded + "\n\n"
        finally:
            await self.close()
        if succeeded:
            # Cleanup shielding must not turn an AnyIO disconnect cancellation
            # into success when the next send would complete synchronously.
            await anyio.lowlevel.checkpoint_if_cancelled()
            yield "data: [DONE]\n\n"


def stream_events(result: Any, *, heartbeat_seconds: float, idle_seconds: float,
                  cleanup_seconds: float, error_payload: ErrorPayload) -> AsyncIterator[str]:
    """Wrap an existing result; direct consumers must explicitly close iteration.

    For ASGI use ``CopilotStreamingResponse`` instead: it covers disconnects
    before the generator starts and while the transport is sending its output.
    """
    return _StreamSession(
        result, heartbeat_seconds=heartbeat_seconds, idle_seconds=idle_seconds,
        cleanup_seconds=cleanup_seconds, error_payload=error_payload).events()


class CopilotStreamingResponse(StreamingResponse):
    """StreamingResponse with explicit ownership of the upstream lifecycle."""

    def __init__(self, result: Any, *, heartbeat_seconds: float, idle_seconds: float,
                 cleanup_seconds: float, error_payload: ErrorPayload):
        self._session = _StreamSession(
            result, heartbeat_seconds=heartbeat_seconds, idle_seconds=idle_seconds,
            cleanup_seconds=cleanup_seconds, error_payload=error_payload)
        super().__init__(self._session.events(), media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self._session.close()
