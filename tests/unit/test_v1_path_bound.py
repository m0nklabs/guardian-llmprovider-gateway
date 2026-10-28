"""Issue #75, at the four sites: what each handler actually sends.

The unit tests in ``test_upstream_url.py`` prove the builder and the guard in
isolation. These tests drive the real handlers so the claim holds where it
matters — the ``/v1/{path:path}`` routes:

| Site | Handler | Request-derived | What must happen |
| --- | --- | --- | --- |
| ``routing.py`` POST passthrough | ``route_v1_post`` (non-inference) | ``path`` | refused before the body is read, or forwarded as ``/v1/<path>`` |
| ``routing.py`` inference hop | ``route_v1_post`` (non-streaming) | ``path`` | same validated URL, sent once |
| ``routing.py`` reload retry | ``route_v1_post`` (ConnectError retry) | ``path`` | same validated URL, not a second assembly |
| ``proxy/server.py`` GET | ``proxy_v1_get`` | ``path`` (+ query) | refused, or forwarded with the query preserved |

The destination half is not theirs to enforce: with the backend URL pointing at
an unapproved host/port the *guard* refuses, with its own reason and without a
socket ever opening. That is the positive control that the allowlist is really
below these call sites.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.destination_policy import DestinationDenied
from app.proxy import server

BACKEND_URL = "http://localhost:11440"


# ── fakes ─────────────────────────────────────────────────────────────


class _FakeRequest:
    """The request parts ``route_v1_post`` touches, and nothing else."""

    def __init__(
        self,
        *,
        route: str,
        payload: dict | None = None,
        query: str = "",
    ) -> None:
        self.headers = {"Content-Type": "application/json"}
        self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
        self.url = SimpleNamespace(path=f"/v1/{route}")
        self.method = "POST"
        self.query_params = httpx.QueryParams(query)
        self.payload = payload
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        if self.payload is None:
            raise AssertionError("the body must not be read for a refused path")
        return json.dumps(self.payload).encode("utf-8")


class _FakeUpstreamResponse:
    def __init__(self, *, content: bytes = b'{"ok":true}', stream: bool = False) -> None:
        self.content = content
        self.status_code = 200
        self.headers = {
            "content-type": "text/event-stream" if stream else "application/json"
        }
        self.request = httpx.Request(
            "POST", "http://127.0.0.1:11440/v1/chat/completions"
        )

    async def aiter_lines(self):
        yield 'data: {"choices": [{"delta": {"content": "ok"}}]}'

    def json(self):
        return {"ok": True, "choices": [], "usage": {}}


class _RecordingClient:
    """Records every outbound URL the handler hands to httpx."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url, content=None, headers=None):
        type(self).calls.append(str(url))
        return _FakeUpstreamResponse()

    async def get(self, url, params=None, **kwargs):
        type(self).calls.append(str(url))
        type(self).params = params
        return _FakeUpstreamResponse()

    def build_request(self, method, url, json=None, content=None, timeout=None, headers=None):
        type(self).calls.append(str(url))
        return SimpleNamespace(method=method, url=url, content=content, headers=headers)

    async def send(self, req, stream=False):
        return _FakeUpstreamResponse(stream=True)

    async def aclose(self) -> None:
        return None


class _ConnectErrorClient(_RecordingClient):
    """First call fails the way a dead backend does; the retry must be recorded."""

    async def post(self, url, content=None, headers=None):
        type(self).calls.append(str(url))
        if len(type(self).calls) == 1:
            raise httpx.ConnectError("connection refused")
        return _FakeUpstreamResponse()


@pytest.fixture(autouse=True)
def _reset_calls():
    _RecordingClient.calls = []
    yield
    _RecordingClient.calls = []


def _inference_stack(client_cls=None):
    """The minimum for a local inference request to reach the backend hop.

    ``client_cls=None`` leaves httpx alone, so the *real* guard runs — the only
    way to observe a refusal the allowlist (not the handler) produces.
    """
    stack = ExitStack()
    stack.enter_context(
        patch.object(
            server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None
        )
    )
    stack.enter_context(
        patch.object(
            server._gw_routing, "_begin_queued_request", return_value=("req-123", None)
        )
    )
    stack.enter_context(
        patch.object(
            server._gw_routing,
            "_resolve_or_reject_inference_model",
            return_value="Qwen-Agent",
        )
    )
    stack.enter_context(
        patch.object(
            server.model_manager,
            "get_current_model",
            AsyncMock(return_value="Qwen-Agent"),
        )
    )
    stack.enter_context(
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}, "auto": {}})
    )
    if client_cls is not None:
        stack.enter_context(
            patch.object(server._ctx_meta.httpx, "AsyncClient", client_cls)
        )
    return stack


# ── POST passthrough (routing.py:255) ─────────────────────────────────


@pytest.mark.asyncio
async def test_post_passthrough_refuses_a_dot_segment_before_reading_the_body():
    """`/v1/../props` never becomes a request: the body is not even read."""
    request = _FakeRequest(route="../props")

    with patch.object(server._ctx_meta.httpx, "AsyncClient", _RecordingClient):
        with pytest.raises(server.HTTPException) as excinfo:
            await server.proxy_v1_post("../props", request, client_id="openclaw")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["reason"] == "path_not_allowed"
    assert excinfo.value.detail["path_reason"] == "dot_segment"
    assert request.body_reads == 0
    assert _RecordingClient.calls == []


@pytest.mark.asyncio
async def test_post_passthrough_forwards_a_legitimate_path_unchanged():
    """The positive case: `/v1/models` still reaches `/v1/models`."""
    request = _FakeRequest(route="models", payload={"input": "metadata only"})

    with (
        patch.object(
            server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None
        ),
        patch.object(
            server._gw_routing,
            "_begin_queued_request",
            side_effect=AssertionError("non-inference routes are not queued"),
        ),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _RecordingClient),
    ):
        response = await server.proxy_v1_post("models", request, client_id="openclaw")

    assert response.status_code == 200
    assert _RecordingClient.calls == [f"{BACKEND_URL}/v1/models"]


# ── POST inference hops (routing.py:1048 and the retry at :1063) ──────


@pytest.mark.asyncio
async def test_post_inference_refuses_a_dot_segment_before_queue_admission():
    request = _FakeRequest(
        route="chat/completions",
        payload={"model": "auto", "stream": False, "messages": []},
    )

    with (
        patch.object(
            server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None
        ),
        patch.object(
            server._gw_routing,
            "_begin_queued_request",
            side_effect=AssertionError("a refused path must not be queued"),
        ),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _RecordingClient),
    ):
        with pytest.raises(server.HTTPException) as excinfo:
            await server.proxy_v1_post(
                "a/../../slots", request, client_id="openclaw"
            )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["path_reason"] == "dot_segment"
    assert request.body_reads == 0
    assert _RecordingClient.calls == []


@pytest.mark.asyncio
async def test_nonstreaming_inference_hop_sends_the_validated_v1_url():
    """Site ``routing.py:1048``: one hop, the template-built URL, no drift."""
    request = _FakeRequest(
        route="chat/completions",
        payload={"model": "auto", "stream": False, "messages": []},
    )

    with _inference_stack(_RecordingClient):
        response = await server.proxy_v1_post(
            "chat/completions", request, client_id="openclaw"
        )

    assert response.status_code == 200
    assert _RecordingClient.calls == [f"{BACKEND_URL}/v1/chat/completions"]


@pytest.mark.asyncio
async def test_the_reload_retry_reuses_the_same_validated_url():
    """Site ``routing.py:1063``: the retry after a ConnectError is not reassembled."""
    request = _FakeRequest(
        route="chat/completions",
        payload={"model": "auto", "stream": False, "messages": []},
    )
    with (
        _inference_stack(_ConnectErrorClient),
        patch.object(
            server._gw_routing,
            "_reload_backend_after_connect_error",
            AsyncMock(return_value=None),
        ),
    ):
        response = await server.proxy_v1_post(
            "chat/completions", request, client_id="openclaw"
        )

    assert response.status_code == 200
    assert _RecordingClient.calls == [
        f"{BACKEND_URL}/v1/chat/completions",
        f"{BACKEND_URL}/v1/chat/completions",
    ]


@pytest.mark.asyncio
async def test_the_streaming_hop_uses_the_same_validated_url():
    """Sibling of ``routing.py:795/:813`` (same assembly, same rule)."""
    request = _FakeRequest(
        route="chat/completions",
        payload={"model": "auto", "stream": True, "messages": []},
    )
    with _inference_stack(_RecordingClient):
        response = await server.proxy_v1_post(
            "chat/completions", request, client_id="openclaw"
        )

    assert response.status_code == 200
    assert _RecordingClient.calls == [f"{BACKEND_URL}/v1/chat/completions"]


# ── GET passthrough (proxy/server.py:1552) ────────────────────────────


@pytest.mark.asyncio
async def test_get_passthrough_refuses_a_dot_segment():
    request = _FakeRequest(route="props")

    with patch.object(server._ctx_meta.httpx, "AsyncClient", _RecordingClient):
        with pytest.raises(server.HTTPException) as excinfo:
            await server.proxy_v1_get("%2e%2e/props", request, client_id="openclaw")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["path_reason"] == "dot_segment"
    assert _RecordingClient.calls == []


@pytest.mark.asyncio
async def test_get_passthrough_forwards_a_legitimate_path_and_its_query():
    """The query string stays a query on the same endpoint (no path effect)."""
    request = _FakeRequest(route="models", query="limit=5")

    with patch.object(server._ctx_meta.httpx, "AsyncClient", _RecordingClient):
        response = await server.proxy_v1_get("models", request, client_id="openclaw")

    assert response.status_code == 200
    assert _RecordingClient.calls == [f"{BACKEND_URL}/v1/models"]
    assert _RecordingClient.params == httpx.QueryParams("limit=5")


# ── The destination half still belongs to the guard ──────────────────


@pytest.mark.asyncio
async def test_inference_hop_surfaces_the_guards_refusal():
    """With the backend URL unapproved, the guard refuses before any I/O.

    The path here is benign, so nothing in the handler can be blamed: this is
    the allowlist doing the job these four sites rely on it for. The inference
    branch wraps upstream failures in its own 502 (unchanged by #75), so the
    transport's typed refusal reaches the caller as that 502's detail — code
    and reason intact — and one G1-03 audit record is written by the transport.
    """
    request = _FakeRequest(
        route="chat/completions",
        payload={"model": "auto", "stream": False, "messages": []},
    )

    with _inference_stack(), patch.object(
        server._gw_routing, "_llama_server_url", "http://169.254.169.254"
    ):
        with pytest.raises(server.HTTPException) as excinfo:
            await server.proxy_v1_post(
                "chat/completions", request, client_id="openclaw"
            )

    assert excinfo.value.status_code == 502
    # DestinationDenied's message form: code, then the policy reason.
    assert "destination denied" in excinfo.value.detail
    assert "metadata_host_blocked" in excinfo.value.detail


@pytest.mark.asyncio
async def test_post_passthrough_refuses_an_unapproved_backend_port():
    """The passthrough branch lets the typed refusal through unchanged."""
    request = _FakeRequest(route="models", payload={"input": "x"})

    with patch.object(server._gw_routing, "_llama_server_url", "http://127.0.0.1:11437"):
        with pytest.raises(DestinationDenied) as excinfo:
            await server.proxy_v1_post("models", request, client_id="openclaw")

    assert excinfo.value.reason == "port_not_allowed"


@pytest.mark.asyncio
async def test_get_route_refuses_an_unapproved_backend_destination():
    request = _FakeRequest(route="models")

    with patch.object(server, "LLAMA_SERVER_URL", "http://127.0.0.1:11437"):
        with pytest.raises(DestinationDenied) as excinfo:
            await server.proxy_v1_get("models", request, client_id="openclaw")

    assert excinfo.value.reason == "port_not_allowed"
    assert _RecordingClient.calls == []
