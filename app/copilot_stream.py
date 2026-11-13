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


class ClientDisconnected(ClientDisconnect):
    """The client abandoned inference before a response was available."""


class _InferenceSession:
    def __init__(self, awaitable: Awaitable, cleanup_seconds: float):
        self.awaitable = awaitable
        self.cleanup_seconds = cleanup_seconds
        self.result: Any = None
        self.abandoned = False
        self.result_closed = False
        self.deadline: float | None = None

    async def run(self) -> Any:
        try:
            self.result = await self.awaitable
            return self.result
        finally:
            if self.abandoned:
                await self.close_result()

    async def close_result(self) -> None:
        if self.result_closed or not callable(getattr(self.result, "aclose", None)):
            return
        self.result_closed = True
        loop = asyncio.get_running_loop()
        # A cancellation-resistant inference can produce its result after the
        # request's cleanup budget expired. Still close that late orphan with a
        # fresh bounded budget; the abandoned ASGI request is no longer waiting.
        deadline = self.deadline
        if deadline is None or deadline <= loop.time():
            deadline = loop.time() + self.cleanup_seconds
        with anyio.CancelScope(shield=True):
            task = asyncio.create_task(_close_target(self.result), name="copilot-stream-close")
            await asyncio.wait({task}, timeout=max(0, deadline - loop.time()))
            if task.done():
                _consume_task(task)
            else:
                task.cancel()
                task.add_done_callback(_consume_task)
                logger.warning("Copilot abandoned inference close exceeded cleanup budget")


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
            session.abandoned = True
            if session.deadline is None:
                session.deadline = asyncio.get_running_loop().time() + cleanup_seconds
            with anyio.CancelScope(shield=True):
                if not watcher.done():
                    watcher.cancel()
                if not inference.done():
                    inference.cancel()
                done, pending = await asyncio.wait(
                    {watcher, inference}, timeout=max(
                        0, session.deadline - asyncio.get_running_loop().time()))
                for task in done:
                    _consume_task(task)
                for task in pending:
                    task.cancel()
                    task.add_done_callback(_consume_task)
                if pending:
                    logger.warning("Copilot inference cancellation exceeded cleanup budget")
                if inference.done():
                    if session.deadline > asyncio.get_running_loop().time():
                        await session.close_result()
                    elif not session.result_closed:
                        task = asyncio.create_task(
                            session.close_result(), name="copilot-stream-orphan-close")
                        task.add_done_callback(_consume_task)


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
        self.closed = False

    async def close(self) -> None:
        """Cancel reading, then close upstream within one shielded time budget.

        A coroutine which ignores cancellation cannot be forcibly terminated by
        Python. Such a task is cancelled and its eventual result is observed;
        it cannot keep the ASGI response waiting past the cleanup budget.
        """
        if self.closed:
            return
        self.closed = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cleanup_seconds
        with anyio.CancelScope(shield=True):
            tasks: set[asyncio.Task] = set()
            if self.pending is not None:
                task, self.pending = self.pending, None
                if not task.done():
                    task.cancel()
                    # Deliver cooperative cancellation before aclose on a
                    # generator. Do not spend the whole budget joining a read:
                    # result.aclose may be what releases its underlying I/O.
                    await asyncio.wait({task}, timeout=0)
                tasks.add(task)
            if callable(getattr(self.result, "aclose", None)):
                tasks.add(asyncio.create_task(
                    _close_target(self.result), name="copilot-stream-close"))
            if tasks:
                done, pending = await asyncio.wait(
                    tasks, timeout=max(0, deadline - loop.time()))
                for task in done:
                    _consume_task(task)
                for task in pending:
                    task.cancel()
                    task.add_done_callback(_consume_task)
                if pending:
                    logger.warning("Copilot stream cleanup exceeded cleanup budget")
            # __aiter__ may return a distinct generator owned by the result.
            # Its read must have stopped before we can explicitly finalize it.
            if (self.iterator is not None and self.iterator is not self.result
                    and callable(getattr(self.iterator, "aclose", None))):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning("Copilot stream iterator close exceeded cleanup budget")
                    return
                task = asyncio.create_task(
                    _close_target(self.iterator), name="copilot-stream-close")
                await asyncio.wait({task}, timeout=remaining)
                if task.done():
                    _consume_task(task)
                else:
                    task.cancel()
                    task.add_done_callback(_consume_task)
                    logger.warning("Copilot stream iterator close exceeded cleanup budget")

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
