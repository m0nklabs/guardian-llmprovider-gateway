"""Offline SSE and real Starlette ASGI disconnect lifecycle coverage."""
import asyncio
import json
import time

import anyio
import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

from app.copilot_stream import (
    ClientDisconnected, CopilotStreamingResponse, await_inference, stream_events,
)


class Chunk:
    def __init__(self, encoded):
        self.encoded = encoded
        self.dump_calls = []

    def model_dump_json(self, **kwargs):
        self.dump_calls.append(kwargs)
        return self.encoded


class Stream:
    """Instrument an already-created result, with no inference/network calls."""
    def __init__(self, steps=(), *, close_delay=0, hang_close=False):
        self.steps = iter(steps)
        self.iterations = 0
        self.reads = 0
        self.active_reads = 0
        self.max_active_reads = 0
        self.read_started = asyncio.Event()
        self.read_cancelled = False
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.close_completed = False
        self.close_cancelled = False
        self.close_delay = close_delay
        self.hang_close = hang_close

    def __aiter__(self):
        self.iterations += 1
        return self

    async def __anext__(self):
        self.reads += 1
        self.active_reads += 1
        self.max_active_reads = max(self.active_reads, self.max_active_reads)
        self.read_started.set()
        try:
            step = next(self.steps, StopAsyncIteration())
            if isinstance(step, Exception):
                raise step
            if callable(step):
                return await step()
            return step
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise
        finally:
            self.active_reads -= 1

    async def aclose(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            if self.hang_close:
                await asyncio.Event().wait()
            await asyncio.sleep(self.close_delay)
            self.close_completed = True
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise


def error_payload(exc):
    return {"error": {"message": "Safe upstream failure", "type": "upstream_error",
                      "code": "upstream_timeout" if isinstance(exc, TimeoutError) else "upstream_error"}}


def response(stream, **overrides):
    return CopilotStreamingResponse(stream, **{
        "heartbeat_seconds": 0.01, "idle_seconds": 0.2,
        "cleanup_seconds": 0.1, "error_payload": error_payload, **overrides})


async def run_asgi(resp, *, spec="2.3", disconnect=None, send_hook=None):
    """Use actual StreamingResponse.__call__, not buffered httpx transport."""
    sent = []
    never = asyncio.Event()

    async def receive():
        if disconnect is None:
            await never.wait()
        else:
            await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if send_hook is not None:
            await send_hook(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
             "method": "POST", "path": "/v1/chat/completions", "headers": []}
    await asyncio.wait_for(resp(scope, receive, send), timeout=1)
    return sent


def body(sent):
    return b"".join(m.get("body", b"") for m in sent).decode()


def data_frames(text):
    return [frame.removeprefix("data: ") for frame in text.split("\n\n")
            if frame.startswith("data: ")]


def stream_tasks():
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("copilot-stream-")]


@pytest.mark.asyncio
async def test_heartbeat_is_comment_preserves_exact_chunk_json_and_single_read():
    chunks = [Chunk('{ "id": "chat_1", "choices": [{"delta": {"tool_calls": [{"id": "call_1", "function": {"name": "lookup", "arguments": "{\\\"x\\\":"}}]}}]}'),
              Chunk('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10},"vendor_extra":true}')]

    async def delayed_first():
        await asyncio.sleep(0.045)
        return chunks[0]

    stream = Stream([delayed_first, chunks[1]])
    sent = await run_asgi(response(stream, heartbeat_seconds=0.008))
    text = body(sent)
    assert text.startswith(": guardian-keepalive\n\n")
    assert text.count(": guardian-keepalive\n\n") >= 2
    assert data_frames(text) == [c.encoded for c in chunks] + ["[DONE]"]
    assert all(c.dump_calls == [{"exclude_none": True}] for c in chunks)
    assert stream.iterations == 1
    assert stream.reads == 3  # Two real chunks and exactly one EOF read.
    assert stream.max_active_reads == 1
    assert stream.close_calls == 1 and stream.close_completed
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert headers[b"x-accel-buffering"] == b"no"
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_midstream_error_is_sanitized_and_never_success_done():
    stream = Stream([Chunk('{"choices":[{"delta":{"content":"partial"}}]}'),
                     RuntimeError("Bearer private-secret http://upstream-private/path")])
    text = body(await run_asgi(response(stream)))
    frames = data_frames(text)
    assert len(frames) == 2
    assert json.loads(frames[0])["choices"][0]["delta"]["content"] == "partial"
    assert json.loads(frames[1]) == error_payload(RuntimeError())
    assert "[DONE]" not in text
    assert "private" not in text
    assert stream.reads == 2 and stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_chunk_serialization_error_never_emits_success_done():
    class BrokenChunk:
        def model_dump_json(self, **kwargs):
            raise ValueError("serialization-private")

    stream = Stream([BrokenChunk()])
    text = body(await run_asgi(response(stream)))
    assert len(data_frames(text)) == 1
    assert json.loads(data_frames(text)[0]) == error_payload(ValueError())
    assert "[DONE]" not in text and "private" not in text
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_direct_task_cancel_propagates_after_shielded_cleanup():
    async def stalled():
        await asyncio.Event().wait()

    stream = Stream([stalled], close_delay=0.025)
    sent = []

    async def send_hook(message):
        sent.append(message)

    # ASGI 2.4 uses this same task to consume the stream. This pins ordinary
    # asyncio cancellation as well as AnyIO's disconnect cancel-scope path.
    task = asyncio.create_task(run_asgi(response(stream), spec="2.4", send_hook=send_hook))
    await stream.read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "[DONE]" not in body(sent) and "error" not in body(sent)
    assert stream.read_cancelled and stream.active_reads == 0
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_nondefault_idle_timeout_not_reset_by_heartbeats():
    async def stalled():
        await asyncio.Event().wait()

    stream = Stream([stalled])
    start = time.monotonic()
    text = body(await run_asgi(response(stream, heartbeat_seconds=0.008, idle_seconds=0.05)))
    elapsed = time.monotonic() - start
    assert 0.04 <= elapsed < 0.6
    assert text.count(": guardian-keepalive\n\n") >= 2
    assert json.loads(data_frames(text)[-1])["error"]["code"] == "upstream_timeout"
    assert "[DONE]" not in text
    assert stream.iterations == stream.reads == stream.max_active_reads == 1
    assert stream.read_cancelled and stream.active_reads == 0
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_idle_budget_restarts_after_real_chunk_not_total_stream_duration():
    async def delayed():
        await asyncio.sleep(0.03)
        return Chunk('{"choices":[]}')

    stream = Stream([delayed, delayed, delayed])
    text = body(await run_asgi(response(stream, heartbeat_seconds=0.008, idle_seconds=0.05)))
    assert data_frames(text) == ['{"choices":[]}'] * 3 + ["[DONE]"]
    assert stream.reads == 4 and stream.max_active_reads == 1
    assert stream.close_completed


@pytest.mark.asyncio
async def test_asgi_disconnect_while_upstream_reading_shields_delayed_close():
    disconnect = asyncio.Event()

    async def stalled():
        disconnect.set()
        await asyncio.Event().wait()

    stream = Stream([stalled], close_delay=0.025)
    sent = await run_asgi(response(stream), disconnect=disconnect)
    assert "[DONE]" not in body(sent)
    assert "error" not in body(sent)
    assert stream.read_cancelled and stream.active_reads == 0
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_asgi_disconnect_before_generator_started_closes_result():
    disconnect = asyncio.Event()
    stream = Stream([Chunk('{"choices":[]}')], close_delay=0.02)

    async def send_hook(message):
        assert message["type"] == "http.response.start"
        disconnect.set()
        await asyncio.Event().wait()

    sent = await run_asgi(response(stream), disconnect=disconnect, send_hook=send_hook)
    assert body(sent) == ""
    assert stream.iterations == stream.reads == 0
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_asgi_disconnect_while_sending_chunk_closes_suspended_generator():
    disconnect = asyncio.Event()
    stream = Stream([Chunk('{"choices":[]}')], close_delay=0.02)

    async def send_hook(message):
        if message["type"] == "http.response.body":
            disconnect.set()
            await asyncio.Event().wait()

    text = body(await run_asgi(response(stream), disconnect=disconnect, send_hook=send_hook))
    assert data_frames(text) == ['{"choices":[]}']
    assert stream.reads == 1
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", ["http.response.start", "http.response.body"])
async def test_asgi_24_transport_disconnect_closes_even_if_not_started(fail_on):
    stream = Stream([Chunk('{"choices":[]}')], close_delay=0.015)

    async def send_hook(message):
        if message["type"] == fail_on:
            raise OSError("peer disconnected")

    with pytest.raises(ClientDisconnect):
        await run_asgi(response(stream), spec="2.4", send_hook=send_hook)
    assert stream.close_calls == 1 and stream.close_completed
    assert stream.reads == (0 if fail_on == "http.response.start" else 1)
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_hung_close_is_bounded_under_asgi_disconnect():
    disconnect = asyncio.Event()
    stream = Stream([], hang_close=True)

    async def send_hook(message):
        if message["type"] == "http.response.start":
            disconnect.set()
            await asyncio.Event().wait()

    start = time.monotonic()
    sent = await run_asgi(response(stream, cleanup_seconds=0.025),
                          disconnect=disconnect, send_hook=send_hook)
    assert time.monotonic() - start < 0.6
    assert body(sent) == ""
    await asyncio.sleep(0)  # Deliver the close task's already-issued cancellation.
    assert stream.close_calls == 1 and stream.close_cancelled and not stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_result_close_releases_read_that_needs_io_shutdown():
    disconnect = asyncio.Event()
    released = asyncio.Event()

    async def stuck_until_closed():
        disconnect.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Some adapters finish a pending read only once the I/O is closed.
            await released.wait()
            raise

    class ShutdownStream(Stream):
        async def aclose(self):
            await super().aclose()
            released.set()

    stream = ShutdownStream([stuck_until_closed], close_delay=0.01)
    sent = await run_asgi(response(stream, cleanup_seconds=0.05), disconnect=disconnect)
    assert "[DONE]" not in body(sent)
    assert stream.close_calls == 1 and stream.close_completed and released.is_set()
    assert stream.read_cancelled and stream.active_reads == 0
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_cancellation_resistant_close_cannot_hold_asgi_response():
    disconnect = asyncio.Event()
    release_close = asyncio.Event()

    class ResistantStream(Stream):
        async def aclose(self):
            self.close_calls += 1
            self.close_started.set()
            try:
                await release_close.wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                await release_close.wait()
            self.close_completed = True

    stream = ResistantStream()

    async def send_hook(message):
        if message["type"] == "http.response.start":
            disconnect.set()
            await asyncio.Event().wait()

    start = time.monotonic()
    await run_asgi(response(stream, cleanup_seconds=0.025),
                   disconnect=disconnect, send_hook=send_hook)
    assert time.monotonic() - start < 0.6
    await asyncio.sleep(0)
    assert stream.close_calls == 1 and stream.close_cancelled and not stream.close_completed
    # Python cannot kill a cancellation-resistant coroutine. Release the fake
    # explicitly so this test itself leaves no task behind.
    remaining = stream_tasks()
    assert len(remaining) == 1
    release_close.set()
    await asyncio.gather(*remaining)
    assert stream.close_completed and not stream_tasks()


@pytest.mark.asyncio
async def test_distinct_iterator_finalizer_is_closed():
    finalized = asyncio.Event()

    class Container:
        close_calls = 0

        def __aiter__(self):
            return self.generate()

        async def generate(self):
            try:
                yield Chunk('{"choices":[]}')
            finally:
                finalized.set()

        async def aclose(self):
            self.close_calls += 1

    stream = Container()
    disconnect = asyncio.Event()

    async def send_hook(message):
        if message["type"] == "http.response.body":
            disconnect.set()
            await asyncio.Event().wait()

    await run_asgi(response(stream), disconnect=disconnect, send_hook=send_hook)
    assert stream.close_calls == 1 and finalized.is_set()
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_direct_iterator_and_broken_error_formatter_fallback():
    def broken_formatter(exc):
        raise ValueError("do not expose formatter-secret")

    stream = Stream([ValueError("upstream-secret")])
    frames = [frame async for frame in stream_events(
        stream, heartbeat_seconds=0.01, idle_seconds=0.1,
        cleanup_seconds=0.1, error_payload=broken_formatter)]
    assert len(frames) == 1
    assert json.loads(data_frames(frames[0])[0])["error"]["code"] == "upstream_error"
    assert "secret" not in frames[0] and "[DONE]" not in frames[0]
    assert stream.close_calls == 1 and stream.close_completed


async def inference_app(scope, receive, send, completion, *, streaming=False, cleanup_seconds=0.1):
    request = Request(scope, receive)
    raw = await request.body()
    assert json.loads(raw) == {"messages": [{"content": "complete body"}]}
    result = await await_inference(request, completion(), cleanup_seconds=cleanup_seconds)
    resp = response(result) if streaming else JSONResponse(result)
    await resp(scope, receive, send)


def inference_scope():
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST", "path": "/v1/chat/completions", "headers": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_asgi_disconnect_cancels_preheader_inference_once(streaming):
    disconnect = asyncio.Event()
    messages = asyncio.Queue()
    # Split payload proves watching receive starts after ALL body messages.
    await messages.put({"type": "http.request", "body": b'{"messages":', "more_body": True})
    await messages.put({"type": "http.request", "body": b'[{"content":"complete body"}]}', "more_body": False})
    calls = 0
    cancelled = False
    cleaned = False
    sent = []

    async def receive():
        if not messages.empty():
            return await messages.get()
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def completion():
        nonlocal calls, cancelled, cleaned
        calls += 1
        disconnect.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            await asyncio.sleep(0.02)
            cleaned = True
            raise

    with pytest.raises(ClientDisconnected):
        await asyncio.wait_for(inference_app(inference_scope(), receive, send, completion,
                                            streaming=streaming), timeout=1)
    assert calls == 1 and cancelled and cleaned
    assert sent == []  # No headers, error frame or successful DONE were sent.
    assert messages.empty() and not stream_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_preheader_success_transfers_result_and_releases_receive(streaming):
    messages = asyncio.Queue()
    await messages.put({"type": "http.request", "body": b'{"messages":[{"content":"complete body"}]}', "more_body": False})
    calls = 0
    sent = []
    stream = Stream([Chunk('{"choices":[]}')])
    waiting = 0
    max_waiting = 0

    async def receive():
        nonlocal waiting, max_waiting
        waiting += 1
        max_waiting = max(waiting, max_waiting)
        try:
            return await messages.get()
        finally:
            waiting -= 1

    async def send(message):
        sent.append(message)

    async def completion():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return stream if streaming else {"choices": []}

    await asyncio.wait_for(inference_app(inference_scope(), receive, send, completion,
                                        streaming=streaming), timeout=1)
    assert calls == 1 and max_waiting == 1 and waiting == 0
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    if streaming:
        assert data_frames(body(sent)) == ['{"choices":[]}', "[DONE]"]
        assert stream.close_calls == 1 and stream.close_completed
    else:
        assert json.loads(body(sent)) == {"choices": []}
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_preheader_disconnect_wins_result_race_and_closes_orphan():
    ready = asyncio.Event()
    stream = Stream(close_delay=0.02)

    async def receive():
        await ready.wait()
        return {"type": "http.disconnect"}

    async def inference():
        ready.set()
        return stream

    with pytest.raises(ClientDisconnected):
        await await_inference(Request(inference_scope(), receive), inference(), cleanup_seconds=0.1)
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_preheader_cancel_propagates_and_closes_result_returned_on_cancel():
    started = asyncio.Event()
    never = asyncio.Event()
    stream = Stream(close_delay=0.02)

    async def receive():
        await never.wait()

    async def inference():
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            return stream

    task = asyncio.create_task(await_inference(
        Request(inference_scope(), receive), inference(), cleanup_seconds=0.1))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_preheader_anyio_cancellation_shields_cleanup():
    started = asyncio.Event()
    never = asyncio.Event()
    cleaned = False
    returned = False

    async def receive():
        await never.wait()

    async def inference():
        nonlocal cleaned
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)
            cleaned = True
            raise

    async def worker():
        nonlocal returned
        await await_inference(Request(inference_scope(), receive), inference(), cleanup_seconds=0.1)
        returned = True

    async with anyio.create_task_group() as group:
        group.start_soon(worker)
        await started.wait()
        group.cancel_scope.cancel()
    assert cleaned and not returned and not stream_tasks()


@pytest.mark.asyncio
async def test_preheader_resistant_inference_has_bounded_cancel_and_late_result_close():
    started = asyncio.Event()
    disconnect = asyncio.Event()
    release = asyncio.Event()
    stream = Stream(close_delay=0.01)

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def inference():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        return stream

    task = asyncio.create_task(await_inference(
        Request(inference_scope(), receive), inference(), cleanup_seconds=0.025))
    await started.wait()
    start = time.monotonic()
    disconnect.set()
    with pytest.raises(ClientDisconnected):
        await asyncio.wait_for(task, timeout=1)
    assert time.monotonic() - start < 0.6
    remaining = stream_tasks()
    assert len(remaining) == 1
    release.set()
    await asyncio.gather(*remaining)
    assert stream.close_calls == 1 and stream.close_completed
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_preheader_raced_hung_close_does_not_exceed_budget():
    ready = asyncio.Event()
    stream = Stream(hang_close=True)

    async def receive():
        await ready.wait()
        return {"type": "http.disconnect"}

    async def inference():
        ready.set()
        return stream

    start = time.monotonic()
    with pytest.raises(ClientDisconnected):
        await await_inference(Request(inference_scope(), receive), inference(), cleanup_seconds=0.025)
    assert time.monotonic() - start < 0.6
    await asyncio.sleep(0)
    assert stream.close_calls == 1 and stream.close_cancelled
    assert not stream_tasks()


@pytest.mark.asyncio
async def test_preheader_inference_error_unchanged_and_watcher_stopped():
    never = asyncio.Event()
    failure = ValueError("provider failure")

    async def receive():
        await never.wait()

    async def inference():
        raise failure

    with pytest.raises(ValueError) as caught:
        await await_inference(Request(inference_scope(), receive), inference(), cleanup_seconds=0.1)
    assert caught.value is failure
    assert not stream_tasks()


@pytest.mark.parametrize("key", ["heartbeat_seconds", "idle_seconds", "cleanup_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_intervals_must_be_positive_finite(key, value):
    with pytest.raises(ValueError, match="positive and finite"):
        response(None, **{key: value})
