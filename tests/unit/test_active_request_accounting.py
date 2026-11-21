"""In-flight request accounting on the local inference paths (issue #120).

A successful non-streaming local request released its
``model_manager.active_requests`` slot twice: the success path decremented the
counter and the function-level ``finally`` block decremented it again, because
the "release in the finally" flag was only cleared by the streaming branch.
With two overlapping requests the counter could therefore reach 0 while one
request was still running, defeating the idle-unload guard in
``app/proxy/lifespan.py``.  ``max(0, ...)`` hid the under-count whenever a
single request was in flight.

These tests trace every ``active_requests`` write through a delegating proxy
that records the module:line of the writing frame, so a double release stays
visible even when the clamp keeps the counter at its floor.  Every test reads
the counter inside its own body; no surrounding fixture is trusted to hide an
imbalance inside a request.
"""

import asyncio
import json
import traceback
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException, Response

from app.engine.manager import ActiveRequestSlot
from app.proxy import server

MODEL = "test-local-model"


# ── Traced counter ──────────────────────────────────────────────────────


class _CounterWrite:
    """One observed write to ``ModelManager.active_requests``."""

    __slots__ = ("delta", "origin")

    def __init__(self, delta: int, origin: str) -> None:
        self.delta = delta
        self.origin = origin

    def __repr__(self) -> str:
        return f"{self.delta:+d}@{self.origin}"


def _counter_write_origin() -> str:
    """Return the ``module:line`` in the app that wrote the counter."""
    for frame in reversed(traceback.extract_stack()):
        if frame.filename.endswith(
            ("app/gateway/routing.py", "app/local_inference/ollama.py")
        ):
            return f"{Path(frame.filename).name}:{frame.lineno}"
    return "unknown"


class _TracedModelManager:
    """Delegate to the real manager, recording every counter write."""

    def __init__(self, inner) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "writes", [])

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        inner = object.__getattribute__(self, "_inner")
        if name != "active_requests":
            setattr(inner, name, value)
            return
        previous = inner.active_requests
        inner.active_requests = value
        object.__getattribute__(self, "writes").append(
            _CounterWrite(value - previous, _counter_write_origin())
        )

    def releases(self) -> list[_CounterWrite]:
        return [write for write in self.writes if write.delta < 0]

    def acquisitions(self) -> list[_CounterWrite]:
        return [write for write in self.writes if write.delta > 0]


@pytest.fixture(autouse=True)
def _restore_counter():
    """Keep one test's counter state out of the next; assertions stay local."""
    original = server.model_manager.active_requests
    yield
    server.model_manager.active_requests = original


# ── Fake backend transport ──────────────────────────────────────────────


class _JsonResponse:
    """Minimal buffered upstream response for the non-streaming paths."""

    def __init__(
        self,
        payload: dict | None = None,
        *,
        status_code: int = 200,
        read_error: BaseException | None = None,
        headers: dict | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.content = json.dumps(self.payload).encode("utf-8")
        self.read_error = read_error
        self.closed = False

    def json(self) -> dict:
        return self.payload

    async def aread(self) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.content

    async def aclose(self) -> None:
        self.closed = True


class _BackendClient:
    """httpx.AsyncClient stand-in; handlers decide each test's backend."""

    def __init__(self, *, post_handler=None, send_handler=None, **kwargs) -> None:
        self._post_handler = post_handler
        self._send_handler = send_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def build_request(self, method, url, **kwargs):
        return SimpleNamespace(method=method, url=url, kwargs=kwargs)

    async def post(self, url, content=None, headers=None):
        if self._post_handler is None:
            raise AssertionError("unexpected non-streaming backend POST")
        return await self._post_handler(json.loads(content))

    async def send(self, request, stream=False):
        if self._send_handler is None:
            raise AssertionError("unexpected streaming backend send")
        return await self._send_handler(request, stream)

    async def aclose(self) -> None:
        return None


class _ForbiddenBackend:
    """Fail loudly when a path is expected to answer before forwarding."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("request must not be forwarded to llama-server")


async def _ok_post(payload: dict) -> _JsonResponse:
    return _JsonResponse()


def _sse_generator(*lines: str):
    async def generator(response, watchdog, **kwargs):
        for line in lines:
            yield line

    return generator


def _stream_response(*, status_code: int = 200) -> _JsonResponse:
    return _JsonResponse(status_code=status_code)


# ── Requests ───────────────────────────────────────────────────────────


def _openai_request(payload: dict):
    request = SimpleNamespace(
        headers={"Content-Type": "application/json"},
        state=SimpleNamespace(auth_context={"key_fingerprint": "test-key"}),
        url=SimpleNamespace(path="/v1/chat/completions"),
        method="POST",
    )

    async def body() -> bytes:
        return json.dumps({"model": MODEL, **payload}).encode("utf-8")

    request.body = body
    return request


def _ollama_request(payload: dict):
    request = SimpleNamespace(
        headers={"Content-Type": "application/json"},
        state=SimpleNamespace(auth_context={"key_fingerprint": "test-key"}),
        url=SimpleNamespace(path="/api/chat"),
        method="POST",
    )

    async def json_body() -> dict:
        return dict(payload)

    request.json = json_body
    return request


# ── Patch sets ─────────────────────────────────────────────────────────


def _manager_patches():
    """Common model-manager stubs: current model == requested, no switch."""
    return (
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value=MODEL)),
        patch.object(server.model_manager, "models", {MODEL: {}}),
        patch.object(server.model_manager, "is_unloaded", False),
        patch.object(server.model_manager, "is_switch_allowed", Mock(return_value=True)),
        patch.object(server.model_manager, "resolve_model", Mock(side_effect=lambda name: name)),
        patch.object(
            server.model_manager, "current_runtime_uses_mmproj", Mock(return_value=False)
        ),
    )


def _routing_patches(
    trace: _TracedModelManager,
    *,
    backend,
    image_inputs: bool = False,
    preflight=None,
    sse_lines=None,
    await_or_cancel=None,
    map_multimodal_error=None,
):
    patches = [
        patch.object(server._gw_routing, "_model_manager", trace),
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(
            server._gw_routing,
            "_begin_queued_request",
            AsyncMock(return_value=("req-120", None)),
        ),
        patch.object(
            server._gw_routing,
            "_resolve_or_reject_inference_model",
            lambda model, current=None: MODEL,
        ),
        patch.object(server._gw_routing, "_is_cloud_or_guardian_route", lambda model: False),
        patch.object(
            server._gw_routing,
            "_messages_contain_image_input",
            lambda messages: image_inputs,
        ),
        patch.object(
            server._gw_routing, "_desired_runtime_vision_enabled", lambda *a, **k: False
        ),
        patch.object(
            server._gw_routing,
            "_preflight_multimodal_request",
            preflight if preflight is not None else AsyncMock(return_value=None),
        ),
        patch.object(server._gw_routing, "_get_model_timeout", lambda model: 5.0),
        patch.object(server._gw_routing, "_record_usage_from_payload", lambda *a, **k: None),
        patch.object(server._gw_routing, "_update_live_request_usage", lambda *a, **k: None),
        patch.object(server._gw_routing, "_finish_live_request_usage", lambda *a, **k: None),
        patch.object(
            server._gw_routing, "_record_request_token_usage", lambda *a, **k: None
        ),
        patch.object(
            server._gw_routing,
            "_close_on_request_cancel",
            lambda *a, **k: asyncio.sleep(3600),
        ),
        patch("httpx.AsyncClient", backend),
    ]
    if sse_lines is not None:
        patches.append(
            patch.object(server._gw_routing, "_iter_sse_lines_with_watchdog", sse_lines)
        )
    if await_or_cancel is not None:
        patches.append(
            patch.object(server._gw_routing, "_await_or_cancel_request", await_or_cancel)
        )
    if map_multimodal_error is not None:
        patches.append(
            patch.object(
                server._gw_routing, "_map_multimodal_backend_error", map_multimodal_error
            )
        )
    patches.extend(_manager_patches())
    return tuple(patches)


def _ollama_patches(trace: _TracedModelManager, *, backend):
    return (
        patch.object(server._local_ollama, "_model_manager", trace),
        patch.object(server._local_ollama, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(
            server._local_ollama,
            "_begin_queued_request",
            AsyncMock(return_value=("req-120", None)),
        ),
        patch.object(
            server._local_ollama,
            "_resolve_or_reject_inference_model",
            lambda model, current=None: MODEL,
        ),
        patch.object(server._local_ollama, "_is_cloud_or_guardian_route", lambda model: False),
        patch.object(server._local_ollama, "_get_model_timeout", lambda model: 5.0),
        patch.object(server._local_ollama, "_record_usage_from_payload", lambda *a, **k: None),
        patch.object(
            server._local_ollama, "_dispatch_capture_request_received", lambda *a, **k: None
        ),
        patch.object(
            server._local_ollama,
            "_dispatch_capture_nonstream_completed",
            lambda *a, **k: None,
        ),
        patch.object(
            server._local_ollama,
            "_dispatch_capture_request_cancelled",
            lambda *a, **k: None,
        ),
        patch.object(
            server._local_ollama, "_dispatch_capture_request_failed", lambda *a, **k: None
        ),
        patch("httpx.AsyncClient", backend),
        *_manager_patches(),
    )


# ── Routing: non-streaming ─────────────────────────────────────────────


async def test_nonstreaming_success_releases_slot_once():
    """One completed request must leave the counter exactly where it started.

    The baseline of 1 stands for another request still in flight, so the
    ``max(0, ...)`` floor cannot mask the second release.
    """
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)

    with ExitStack() as stack:
        for cm in _routing_patches(
            trace, backend=partial(_BackendClient, post_handler=_ok_post)
        ):
            stack.enter_context(cm)
        response = await server.proxy_v1_post(
            "chat/completions", _openai_request({"stream": False}), client_id="tester"
        )

    assert response.status_code == 200
    assert len(trace.acquisitions()) == 1, trace.writes
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


async def test_overlapping_nonstreaming_requests_keep_counter_until_last_finishes():
    """Two overlapping requests must never let the counter reach 0 early."""
    baseline = 0
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)

    arrivals = 0
    both_in_backend = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    async def post_handler(payload):
        nonlocal arrivals
        tag = payload["messages"][0]["content"]
        arrivals += 1
        if arrivals == 2:
            both_in_backend.set()
        await (release_a if tag == "A" else release_b).wait()
        return _JsonResponse()

    backend = partial(_BackendClient, post_handler=post_handler)
    with ExitStack() as stack:
        for cm in _routing_patches(trace, backend=backend):
            stack.enter_context(cm)

        def make(tag: str):
            return _openai_request(
                {"stream": False, "messages": [{"role": "user", "content": tag}]}
            )

        task_a = asyncio.create_task(
            server.proxy_v1_post("chat/completions", make("A"), client_id="tester")
        )
        task_b = asyncio.create_task(
            server.proxy_v1_post("chat/completions", make("B"), client_id="tester")
        )
        await asyncio.wait_for(both_in_backend.wait(), 5)
        # Both requests acquired their slot before entering the backend.
        assert server.model_manager.active_requests == baseline + 2, trace.writes

        observed_after_a = None
        task_b_done = False
        try:
            release_a.set()
            await asyncio.wait_for(task_a, 5)
            observed_after_a = server.model_manager.active_requests
            task_b_done = task_b.done()
        finally:
            release_b.set()
        await asyncio.wait_for(task_b, 5)

    assert observed_after_a is not None
    assert observed_after_a >= 1, trace.writes
    assert task_b_done is False
    assert [write.delta for write in trace.writes].count(-1) == 2, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


async def test_nonstreaming_backend_error_releases_slot_once():
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)

    async def failing_post(payload):
        raise RuntimeError("backend exploded")

    with ExitStack() as stack:
        for cm in _routing_patches(
            trace, backend=partial(_BackendClient, post_handler=failing_post)
        ):
            stack.enter_context(cm)
        with pytest.raises(Exception) as excinfo:
            await server.proxy_v1_post(
                "chat/completions", _openai_request({"stream": False}), client_id="tester"
            )

    assert getattr(excinfo.value, "status_code", None) == 502
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


async def test_nonstreaming_cancellation_releases_slot_once():
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)
    cancelled = server._gw_routing._GuardianRequestCancelled("req-120", "client_disconnected")

    with ExitStack() as stack:
        for cm in _routing_patches(
            trace,
            backend=partial(_BackendClient, post_handler=_ok_post),
            await_or_cancel=AsyncMock(side_effect=cancelled),
        ):
            stack.enter_context(cm)
        with pytest.raises(Exception) as excinfo:
            await server.proxy_v1_post(
                "chat/completions", _openai_request({"stream": False}), client_id="tester"
            )

    assert getattr(excinfo.value, "status_code", None) == 499
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


async def test_multimodal_preflight_error_releases_slot_once():
    """An early error response (preflight rejects the images) also releases once."""
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)
    rejected = Response(
        content=b'{"error": "vision not supported"}',
        status_code=422,
        media_type="application/json",
    )

    with ExitStack() as stack:
        for cm in _routing_patches(
            trace,
            backend=_ForbiddenBackend,
            image_inputs=True,
            preflight=AsyncMock(return_value=rejected),
        ):
            stack.enter_context(cm)
        response = await server.proxy_v1_post(
            "chat/completions", _openai_request({"stream": False}), client_id="tester"
        )

    assert response is rejected
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


async def test_failure_before_slot_acquisition_does_not_release():
    """A request that fails before forwarding owns no slot to give back."""
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)

    with ExitStack() as stack:
        patches = list(_routing_patches(trace, backend=_ForbiddenBackend))
        patches.append(patch.object(server.model_manager, "is_unloaded", True))
        patches.append(
            patch.object(
                server._gw_routing, "_resolve_auto_reload_model", lambda model: MODEL
            )
        )
        patches.append(
            patch.object(
                server._gw_routing,
                "_run_guardian_operation",
                AsyncMock(side_effect=RuntimeError("reload boom")),
            )
        )
        for cm in patches:
            stack.enter_context(cm)
        with pytest.raises(HTTPException) as excinfo:
            await server.proxy_v1_post(
                "chat/completions", _openai_request({"stream": False}), client_id="tester"
            )

    assert excinfo.value.status_code == 503
    assert "Auto-reload failed" in str(excinfo.value.detail)
    assert trace.writes == [], trace.writes
    assert server.model_manager.active_requests == baseline


# ── Routing: streaming ─────────────────────────────────────────────────


async def test_streaming_releases_slot_only_when_body_consumed():
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)

    async def send(request, stream=False):
        return _stream_response()

    with ExitStack() as stack:
        for cm in _routing_patches(
            trace,
            backend=partial(_BackendClient, send_handler=send),
            sse_lines=_sse_generator('data: {"choices":[{"delta":{"content":"hi"}}]}'),
        ):
            stack.enter_context(cm)
        response = await server.proxy_v1_post(
            "chat/completions", _openai_request({"stream": True}), client_id="tester"
        )

        assert response.status_code == 200
        # The slot is still held: the SSE generator releases it when the body ends.
        assert server.model_manager.active_requests == baseline + 1, trace.writes
        assert trace.releases() == [], trace.writes

        chunks = [chunk async for chunk in response.body_iterator]

    assert chunks
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


async def test_streaming_image_error_response_releases_slot_once():
    """The pre-stream image rejection path also returns from inside the try."""
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)

    async def send(request, stream=False):
        return _JsonResponse({"error": "vision unsupported"}, status_code=503)

    with ExitStack() as stack:
        for cm in _routing_patches(
            trace,
            backend=partial(_BackendClient, send_handler=send),
            image_inputs=True,
            map_multimodal_error=lambda *a, **k: None,
        ):
            stack.enter_context(cm)
        response = await server.proxy_v1_post(
            "chat/completions", _openai_request({"stream": True}), client_id="tester"
        )

    assert response.status_code == 503
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


# ── Ollama bridges: same idiom, same contract ──────────────────────────

_OLLAMA_ENDPOINTS = [
    pytest.param("chat", {"messages": [{"role": "user", "content": "hi"}]}, id="chat"),
    pytest.param("generate", {"prompt": "hi"}, id="generate"),
]


async def _call_ollama(kind: str, request):
    if kind == "chat":
        return await server.proxy_chat_ollama(request, client_id="tester")
    return await server.proxy_generate_ollama(request, client_id="tester")


@pytest.mark.parametrize("kind,extra_body", _OLLAMA_ENDPOINTS)
async def test_ollama_nonstreaming_success_releases_slot_once(kind, extra_body):
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)
    payload = {"model": MODEL, "stream": False, **extra_body}

    async def send(request, stream=False):
        return _JsonResponse()

    with ExitStack() as stack:
        for cm in _ollama_patches(trace, backend=partial(_BackendClient, send_handler=send)):
            stack.enter_context(cm)
        result = await _call_ollama(kind, _ollama_request(payload))

    assert result["done"] is True
    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


@pytest.mark.parametrize("kind,extra_body", _OLLAMA_ENDPOINTS)
async def test_ollama_nonstreaming_error_releases_slot_once(kind, extra_body):
    baseline = 1
    server.model_manager.active_requests = baseline
    trace = _TracedModelManager(server.model_manager)
    payload = {"model": MODEL, "stream": False, **extra_body}
    failing = _JsonResponse(read_error=RuntimeError("backend read failed"))

    async def send(request, stream=False):
        return failing

    with ExitStack() as stack:
        for cm in _ollama_patches(trace, backend=partial(_BackendClient, send_handler=send)):
            stack.enter_context(cm)
        with pytest.raises(RuntimeError, match="backend read failed"):
            await _call_ollama(kind, _ollama_request(payload))

    assert [write.delta for write in trace.writes].count(-1) == 1, trace.writes
    assert server.model_manager.active_requests == baseline, trace.writes


# ── Slot guard semantics ───────────────────────────────────────────────


def test_active_request_slot_releases_at_most_once():
    manager = SimpleNamespace(active_requests=0)
    slot = ActiveRequestSlot(manager)

    assert slot.release() is False  # never acquired → never released
    assert manager.active_requests == 0

    slot.acquire()
    assert manager.active_requests == 1
    assert slot.release() is True
    assert manager.active_requests == 0
    assert slot.release() is False
    assert manager.active_requests == 0