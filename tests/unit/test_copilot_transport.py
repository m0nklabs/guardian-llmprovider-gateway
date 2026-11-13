"""Actual HTTP resource cleanup without importing LiteLLM or calling a provider."""
import asyncio
from contextvars import ContextVar
from types import SimpleNamespace

import anyio
import httpx
import pytest
from starlette.requests import Request

from app.copilot_stream import ClientDisconnected, await_inference
from app.copilot_transport import LitellmOperations, _OwnedHTTPStream


class ClosingBytes(httpx.AsyncByteStream):
    def __init__(self, *, delay=0.02, hang=False, fail=False, resist=False):
        self.delay, self.hang, self.fail, self.resist = delay, hang, fail, resist
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.cancelled = False
        self.close_calls = 0

    async def __aiter__(self):
        yield b"synthetic"

    async def aclose(self):
        self.close_calls += 1
        self.started.set()
        try:
            if self.hang or self.resist:
                await self.release.wait()
            else:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            if not self.resist:
                raise
            await self.release.wait()
        if self.fail:
            raise RuntimeError("credential-bearing cleanup-secret must not escape")
        self.closed = True


def operations(cleanup_seconds=0.1):
    # Constructor needs the separately installed LiteLLM. _invoke and _capture
    # do not: inject only its process-global fallback state, never skip coverage.
    result = LitellmOperations.__new__(LitellmOperations)
    result._litellm = SimpleNamespace(model_fallbacks=None)
    result._state = ContextVar("copilot_transport_test_state", default=None)
    result._cleanup_seconds = cleanup_seconds
    return result


def pending_cleanup():
    return [task for task in asyncio.all_tasks()
            if task.get_name() in {"copilot-stream-cleanup", "copilot-stream-http-close"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_captured_http_close_survives_repeated_raw_cancel(status):
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(status, stream=stream, headers={"Retry-After": "17"})
    calls = 0

    async def operation(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0
        await ops._capture(response)
        if status == 429:
            raise ValueError("synthetic upstream failure")
        return {"choices": []}

    task = asyncio.create_task(ops._invoke(operation, {}))
    await stream.started.wait()
    task.cancel()
    await asyncio.sleep(0)  # Deliver cancellation to the cleanup waiter.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1 and stream.close_calls == 1
    # httpx marks is_closed before stream.aclose finishes, so assert both.
    assert response.is_closed and stream.closed and not stream.cancelled
    assert not pending_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_inference_disconnect_during_http_close_keeps_cleanup_owned(status):
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(status, stream=stream)

    async def operation(**kwargs):
        await ops._capture(response)
        if status == 429:
            raise ValueError("synthetic upstream failure")
        return {"choices": []}

    async def receive():
        await stream.started.wait()
        return {"type": "http.disconnect"}

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ClientDisconnected):
        await await_inference(request, ops._invoke(operation, {}), cleanup_seconds=0.1)
    assert response.is_closed and stream.closed and not stream.cancelled
    assert stream.close_calls == 1 and not pending_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_outer_inference_repeated_raw_cancel_during_http_cleanup(status):
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(status, stream=stream)
    never = asyncio.Event()

    async def operation(**kwargs):
        await ops._capture(response)
        if status == 429:
            raise ValueError("synthetic upstream failure")
        return {"choices": []}

    async def receive():
        await never.wait()

    task = asyncio.create_task(await_inference(
        Request({"type": "http", "headers": []}, receive),
        ops._invoke(operation, {}), cleanup_seconds=0.1))
    await stream.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and not stream.cancelled and stream.close_calls == 1
    assert not pending_cleanup()


@pytest.mark.asyncio
async def test_anyio_cancellation_does_not_abort_http_close():
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(200, stream=stream)
    returned = False

    async def operation(**kwargs):
        await ops._capture(response)
        return {"choices": []}

    async def worker():
        nonlocal returned
        await ops._invoke(operation, {})
        # Scope cancellation remains pending after shielded cleanup.
        await anyio.lowlevel.checkpoint_if_cancelled()
        returned = True

    async with anyio.create_task_group() as group:
        group.start_soon(worker)
        await stream.started.wait()
        group.cancel_scope.cancel()
    assert not returned and stream.closed and not stream.cancelled
    assert not pending_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_hung_close_is_bounded_and_does_not_starve_other_responses(status):
    ops = operations(cleanup_seconds=0.035)
    hung = ClosingBytes(hang=True)
    fast = ClosingBytes(delay=0.005)
    responses = [httpx.Response(status, stream=hung), httpx.Response(200, stream=fast)]
    failure = ValueError("synthetic upstream failure")

    async def operation(**kwargs):
        for response in responses:
            await ops._capture(response)
        if status == 429:
            raise failure
        return {"choices": []}

    started = asyncio.get_running_loop().time()
    if status == 429:
        with pytest.raises(ValueError) as caught:
            await ops._invoke(operation, {})
        assert caught.value is failure and failure.status_code == 429
    else:
        assert await ops._invoke(operation, {}) == {"choices": []}
    assert asyncio.get_running_loop().time() - started < 0.25
    await asyncio.sleep(0)  # Deliver the bounded child's issued cancellation.
    assert hung.close_calls == fast.close_calls == 1
    assert not hung.closed and fast.closed
    # At the shared deadline the response-close waiter is cancelled while its
    # independently owned physical close finishes that same deadline. Join the
    # already-issued cooperative cancellation rather than assume one loop tick.
    remaining = pending_cleanup()
    if remaining:
        await asyncio.wait_for(asyncio.gather(*remaining, return_exceptions=True), timeout=0.1)
    assert hung.cancelled
    assert not pending_cleanup()


@pytest.mark.asyncio
async def test_inference_disconnect_with_hung_http_close_is_bounded():
    ops = operations(cleanup_seconds=0.025)
    stream = ClosingBytes(hang=True)
    response = httpx.Response(200, stream=stream)

    async def operation(**kwargs):
        await ops._capture(response)
        return {"choices": []}

    async def receive():
        await stream.started.wait()
        return {"type": "http.disconnect"}

    start = asyncio.get_running_loop().time()
    with pytest.raises(ClientDisconnected):
        await await_inference(Request({"type": "http", "headers": []}, receive),
                              ops._invoke(operation, {}), cleanup_seconds=0.1)
    assert asyncio.get_running_loop().time() - start < 0.25
    await asyncio.sleep(0)
    assert stream.cancelled and stream.close_calls == 1
    assert not pending_cleanup()


@pytest.mark.asyncio
async def test_cleanup_failure_is_observed_without_masking_upstream_error(caplog):
    ops = operations()
    broken = ClosingBytes(delay=0, fail=True)
    healthy = ClosingBytes(delay=0.005)
    response = httpx.Response(429, stream=broken, headers={"Retry-After": "17"})
    failure = ValueError("original provider failure")

    async def operation(**kwargs):
        await ops._capture(response)
        await ops._capture(httpx.Response(200, stream=healthy))
        raise failure

    with pytest.raises(ValueError) as caught:
        await ops._invoke(operation, {})
    assert caught.value is failure and failure.status_code == 429
    assert failure.headers == {"Retry-After": "17"}
    assert healthy.closed
    assert "RuntimeError" in caplog.text
    assert "cleanup-secret" not in caplog.text
    assert not pending_cleanup()


@pytest.mark.asyncio
async def test_cancellation_resistant_http_close_outlives_only_bounded_wait():
    ops = operations(cleanup_seconds=0.025)
    stream = ClosingBytes(resist=True)
    response = httpx.Response(200, stream=stream)

    async def operation(**kwargs):
        await ops._capture(response)
        return {"choices": []}

    started = asyncio.get_running_loop().time()
    result = await ops._invoke(operation, {})
    assert result == {"choices": []}
    assert asyncio.get_running_loop().time() - started < 0.25
    await asyncio.sleep(0)
    remaining = pending_cleanup()
    assert len(remaining) == 1 and stream.cancelled and not stream.closed
    # Python cannot kill cancellation-resistant provider code. Release this
    # synthetic child, ensuring its eventual completion is observed and no leak
    # survives the test itself.
    stream.release.set()
    await asyncio.gather(*remaining)
    assert stream.closed and not pending_cleanup()


@pytest.mark.asyncio
async def test_stream_success_transfers_http_ownership_without_premature_close():
    ops = operations()
    stream = ClosingBytes(delay=0.005)
    response = httpx.Response(200, stream=stream)
    upstream = SimpleNamespace(aclose=lambda: asyncio.sleep(0))

    async def operation(**kwargs):
        await ops._capture(response)
        return upstream

    result = await ops._invoke(operation, {"stream": True})
    assert not stream.started.is_set() and not response.is_closed
    await result.aclose()
    assert stream.closed and response.is_closed
    assert not pending_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize("cancel_mode", ["disconnect", "raw_repeated"])
async def test_real_nonstream_httpx_autoclose_survives_disconnect_and_raw_cancel(status, cancel_mode):
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(status, stream=stream, headers={"Retry-After": "17"})
    requests = []

    def handle(request):
        requests.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                 event_hooks={"response": [ops._capture]}) as client:
        async def operation(**kwargs):
            result = await client.post("http://synthetic.invalid/v1/messages", json={"synthetic": True})
            result.raise_for_status()
            return result

        async def receive():
            if cancel_mode == "disconnect":
                await stream.started.wait()
                return {"type": "http.disconnect"}
            await asyncio.Event().wait()

        task = asyncio.create_task(await_inference(
            Request({"type": "http", "headers": []}, receive),
            ops._invoke(operation, {}), cleanup_seconds=0.1))
        await stream.started.wait()
        assert response.is_closed  # HTTPX set this BEFORE physical close finished.
        if cancel_mode == "raw_repeated":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(ClientDisconnected if cancel_mode == "disconnect" else asyncio.CancelledError):
            await task
        assert len(requests) == 1 and not client.is_closed
        assert stream.closed and not stream.cancelled and stream.close_calls == 1
        assert not pending_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_real_nonstream_httpx_autoclose_preserves_response_status_body_headers(status):
    ops = operations()
    stream = ClosingBytes(delay=0.005)
    response = httpx.Response(status, stream=stream, headers={"Retry-After": "17", "X-Synthetic": "preserved"})
    requests = []

    def handle(request):
        requests.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                 event_hooks={"response": [ops._capture]}) as client:
        async def operation(**kwargs):
            result = await client.post("http://synthetic.invalid/v1/messages", json={"synthetic": True})
            result.raise_for_status()
            return result

        if status == 429:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await ops._invoke(operation, {})
            assert caught.value.status_code == 429
            assert caught.value.headers == {"Retry-After": "17"}
        else:
            result = await ops._invoke(operation, {})
            assert result is response and result.content == b"synthetic"
            assert result.headers["X-Synthetic"] == "preserved"
        assert len(requests) == 1 and stream.closed and stream.close_calls == 1
        assert not client.is_closed and not pending_cleanup()


@pytest.mark.asyncio
async def test_real_nonstream_httpx_autoclose_survives_anyio_disconnect_scope():
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response),
                                 event_hooks={"response": [ops._capture]}) as client:
        async def operation(**kwargs):
            return await client.post("http://synthetic.invalid/v1/messages", json={"synthetic": True})

        async def worker():
            await ops._invoke(operation, {})
            await anyio.lowlevel.checkpoint_if_cancelled()

        async with anyio.create_task_group() as group:
            group.start_soon(worker)
            await stream.started.wait()
            group.cancel_scope.cancel()
        assert stream.closed and not stream.cancelled and stream.close_calls == 1
        assert not client.is_closed and not pending_cleanup()


@pytest.mark.asyncio
async def test_real_streaming_httpx_eof_close_survives_raw_repeated_cancel():
    ops = operations()
    stream = ClosingBytes(delay=0.035)
    response = httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response),
                                 event_hooks={"response": [ops._capture]}) as client:
        async def operation(**kwargs):
            return await client.send(client.build_request("POST", "http://synthetic.invalid/v1/messages"), stream=True)

        owned = await ops._invoke(operation, {"stream": True})
        assert not stream.started.is_set() and not response.is_closed
        read = asyncio.create_task(owned.result.aread())
        await stream.started.wait()
        read.cancel()
        await asyncio.sleep(0)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        await owned.aclose()  # Response flag is already set; physical close was owned before it.
        assert stream.closed and stream.close_calls == 1 and not stream.cancelled
        assert not client.is_closed and not pending_cleanup()


@pytest.mark.asyncio
async def test_response_hook_does_not_wrap_unowned_discovery_streams():
    ops = operations()
    stream = ClosingBytes(delay=0)
    response = httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response),
                                 event_hooks={"response": [ops._capture]}) as client:
        result = await client.send(client.build_request("GET", "http://synthetic.invalid/v1/models"), stream=True)
        assert not isinstance(result.stream, _OwnedHTTPStream)
        await result.aclose()
    assert stream.closed and not pending_cleanup()


@pytest.mark.asyncio
async def test_wrapped_stream_iteration_and_close_are_idempotent_with_one_deadline():
    stream = ClosingBytes(delay=0.02)
    wrapper = _OwnedHTTPStream(stream, 0.1)
    assert [chunk async for chunk in wrapper] == [b"synthetic"]
    first = asyncio.create_task(wrapper.aclose())
    await stream.started.wait()
    original_task, original_deadline = wrapper.cleanup.task, wrapper.cleanup.deadline
    second = asyncio.create_task(wrapper.aclose())
    await asyncio.gather(first, second)
    await wrapper.aclose()
    assert wrapper.cleanup.task is original_task and wrapper.cleanup.deadline == original_deadline
    assert stream.close_calls == 1 and stream.closed and not pending_cleanup()


@pytest.mark.asyncio
async def test_real_nonstream_httpx_hung_autoclose_has_one_bounded_deadline():
    ops = operations(cleanup_seconds=0.025)
    stream = ClosingBytes(hang=True)
    response = httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response),
                                 event_hooks={"response": [ops._capture]}) as client:
        async def operation(**kwargs):
            return await client.post("http://synthetic.invalid/v1/messages", json={"synthetic": True})

        async def receive():
            await stream.started.wait()
            return {"type": "http.disconnect"}

        start = asyncio.get_running_loop().time()
        with pytest.raises(ClientDisconnected):
            await await_inference(Request({"type": "http", "headers": []}, receive),
                                  ops._invoke(operation, {}), cleanup_seconds=0.1)
        assert asyncio.get_running_loop().time() - start < 0.25
        await asyncio.sleep(0)
        assert stream.cancelled and stream.close_calls == 1
        assert not pending_cleanup()
