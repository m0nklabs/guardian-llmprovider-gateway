"""G1-02: the two Ollama bridge hops go through the destination guard (#65).

``app/local_inference/ollama.py`` serves ``/api/chat`` and ``/api/generate`` by
building the llama-server request itself (``build_request`` + streaming
``send``) — which is exactly why both hops were missed by the G1-02 conversion
and kept a *bare* ``httpx.AsyncClient``. Issue #65 fixed that; this module pins:

* both paths **refuse** an unapproved ``_llama_server_url`` with the typed
  ``destination_denied`` error and the policy's reason;
* both paths refuse a resolver answer **outside the pin** of an approved name;
* the allowed path still streams (no buffering helper crept in), still connects
  to the *validated* address (``Host``/SNI keep the name), and keeps the
  caller's timeout — ``_build_stream_timeout`` for streaming, the plain timeout
  otherwise — instead of a timeout the guard invented;
* a refused hop through one of these paths now leaves a G1-03 **audit record**
  (before #65 the hop did not exist as a decision: no check, no record).

No test opens a socket: the guard runs for real, and only the innermost
``httpx.AsyncHTTPTransport`` network call is replaced by a canned response.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import destination_policy as dp
from app.gateway import queue_helpers
from app.gateway.streaming import build_stream_timeout
from app.local_inference import ollama as _ollama
from app.proxy import audit

REPO_ROOT = Path(__file__).resolve().parents[2]

MODEL = "test-local-model"
REQUEST_ID = "req-65"
CLIENT_ID = "test-client"


# ── Fakes: everything the bridges read from their injected dependencies ─


class _FakeRequest:
    """The two ``request`` methods the bridge touches before hopping."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


class _FakeInferenceQueue:
    """Minimal queue: nothing is cancelled, so the hop is awaited normally."""

    def __init__(self) -> None:
        self.finished: list[tuple[str, str]] = []

    def get_cancel_event(self, request_id: str) -> None:
        return None

    def get_queue_wait_ms(self, request_id: str) -> float:
        return 0.0

    def is_cancel_requested(self, request_id: str) -> bool:
        return False

    def finish(self, request_id: str, outcome: str = "completed") -> None:
        self.finished.append((request_id, outcome))


class _FakeModelManager:
    """The backend already serves the requested model — no switch, no reload."""

    def __init__(self) -> None:
        self.is_unloaded = False
        self.models: dict[str, Any] = {}
        self.active_requests = 0
        self.last_request_time = 0.0

    async def get_current_model(self) -> str:
        return MODEL

    def is_switch_allowed(self, client_id: str) -> bool:
        return True


async def _noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _begin_queued_request(request: Any, client_id: str, model: str):
    return REQUEST_ID, asyncio.create_task(asyncio.sleep(3600))


def _sse(*payloads: str):
    async def _iterator(response, watchdog, **kwargs):
        for payload in payloads:
            yield payload
    return _iterator


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    """Wire the ollama module to fakes and expose the knobs a test needs."""
    queue = _FakeInferenceQueue()
    model_manager = _FakeModelManager()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(_ollama, "_model_manager", model_manager)
    monkeypatch.setattr(_ollama, "_inference_queue", queue)
    monkeypatch.setattr(_ollama, "_capture_controller", None)
    monkeypatch.setattr(_ollama, "_model_switch_lock", asyncio.Lock())
    # Keep the real awaiting/cancellation helper, which is what the caller's
    # ``cleanup=client.aclose`` contract rides on; it reads the queue module's
    # singleton, so point that at the fake too.
    monkeypatch.setattr(_ollama, "_await_or_cancel_request", queue_helpers.await_or_cancel_request)
    monkeypatch.setattr(queue_helpers, "_inference_queue", queue)
    monkeypatch.setattr(_ollama, "_GuardianRequestCancelled", queue_helpers.GuardianRequestCancelled)

    monkeypatch.setattr(_ollama, "_resolve_or_reject_inference_model", lambda raw, current: raw or current)
    monkeypatch.setattr(_ollama, "_is_cloud_or_guardian_route", lambda model: False)
    monkeypatch.setattr(_ollama, "_set_request_usage_metadata", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_capture_client_fingerprint", lambda *a, **k: "test-fingerprint")
    monkeypatch.setattr(_ollama, "_dispatch_capture_request_received", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_get_model_timeout", lambda model: 30)
    monkeypatch.setattr(_ollama, "_build_stream_timeout", build_stream_timeout)
    monkeypatch.setattr(_ollama, "_begin_queued_request", _begin_queued_request)
    monkeypatch.setattr(_ollama, "_stop_background_task", queue_helpers.stop_background_task)
    monkeypatch.setattr(_ollama, "_request_outcome", lambda request_id: "completed")
    monkeypatch.setattr(_ollama, "_close_on_request_cancel", _noop)
    monkeypatch.setattr(_ollama, "_close_stream_resources", _noop)
    monkeypatch.setattr(_ollama, "_coerce_usage_int", lambda value: int(value or 0))
    monkeypatch.setattr(_ollama, "_extract_assistant_delta_text", lambda delta: delta.get("content", ""))
    monkeypatch.setattr(_ollama, "_extract_assistant_message_text", lambda message: message.get("content", ""))
    # Called synchronously by the streaming path, hence plain lambdas.
    monkeypatch.setattr(_ollama, "_update_live_request_usage", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_record_request_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_finish_live_request_usage", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_record_usage_from_payload", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_dispatch_capture_stream_completed", lambda *a, **k: None)
    monkeypatch.setattr(_ollama, "_dispatch_capture_nonstream_completed", lambda *a, **k: None)
    monkeypatch.setattr(
        _ollama,
        "_request_cancel_http_exception",
        lambda request_id, reason: RuntimeError(f"cancelled: {request_id}/{reason}"),
    )

    def _install_backend(payload: bytes = b"", content_type: str = "text/event-stream") -> dict[str, Any]:
        """Replace only the innermost network call; the guard stays in the path."""
        async def _handle(self, request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200, headers={"content-type": content_type}, content=payload, request=request
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _handle)
        return captured

    def _set_url(url: str) -> None:
        monkeypatch.setattr(_ollama, "_llama_server_url", url)

    def _set_resolver(*addresses: str) -> None:
        async def _resolve(host: str, port: int) -> list[str]:
            return list(addresses)

        monkeypatch.setattr(dp, "resolve_addresses", _resolve)

    return {
        "queue": queue,
        "captured": captured,
        "install_backend": _install_backend,
        "set_url": _set_url,
        "set_resolver": _set_resolver,
        "monkeypatch": monkeypatch,
    }


def _chat_body(stream: bool) -> dict[str, Any]:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def _generate_body(stream: bool) -> dict[str, Any]:
    return {"model": MODEL, "prompt": "hello", "stream": stream}


# ── 1. Refusal: the destination is not on the allowlist ────────────────


async def test_chat_hop_refuses_unapproved_destination(bridge) -> None:
    bridge["set_url"]("http://not-approved.example:11440")

    with pytest.raises(dp.DestinationDenied) as excinfo:
        await _ollama.chat_ollama(_FakeRequest(_chat_body(stream=True)), CLIENT_ID)

    error = excinfo.value
    assert error.code == "destination_denied"
    assert error.reason == dp.REASON_HOST_NOT_ALLOWED
    assert error.destination == "http://not-approved.example:11440"
    assert "allowlist" in error.detail
    assert bridge["queue"].finished, "the queue slot must still be released"


async def test_generate_hop_refuses_unapproved_destination(bridge) -> None:
    bridge["set_url"]("http://not-approved.example:11440")

    with pytest.raises(dp.DestinationDenied) as excinfo:
        await _ollama.generate_ollama(_FakeRequest(_generate_body(stream=False)), CLIENT_ID)

    error = excinfo.value
    assert error.code == "destination_denied"
    assert error.reason == dp.REASON_HOST_NOT_ALLOWED
    assert error.destination == "http://not-approved.example:11440"
    assert bridge["queue"].finished, "the queue slot must still be released"


async def test_chat_hop_refuses_loopback_port_that_is_not_approved(bridge) -> None:
    """The reason must be the policy's, not a generic connection failure."""
    bridge["set_url"]("http://127.0.0.1:11439")

    with pytest.raises(dp.DestinationDenied) as excinfo:
        await _ollama.generate_ollama(_FakeRequest(_generate_body(stream=True)), CLIENT_ID)

    assert excinfo.value.reason == dp.REASON_PORT_NOT_ALLOWED
    assert "11439" in excinfo.value.detail


# ── 2. Refusal: the approved name resolves outside its pin ─────────────


async def test_chat_hop_refuses_pinned_address_outside_the_pin(bridge) -> None:
    bridge["set_url"]("http://localhost:11440")
    bridge["set_resolver"]("10.9.9.9")

    with pytest.raises(dp.DestinationDenied) as excinfo:
        await _ollama.chat_ollama(_FakeRequest(_chat_body(stream=True)), CLIENT_ID)

    assert excinfo.value.reason == dp.REASON_DNS_ADDRESS_NOT_ALLOWED
    assert "outside the pinned set" in excinfo.value.detail


async def test_generate_hop_refuses_pinned_address_outside_the_pin(bridge) -> None:
    bridge["set_url"]("http://localhost:11440")
    bridge["set_resolver"]("10.9.9.9")

    with pytest.raises(dp.DestinationDenied) as excinfo:
        await _ollama.generate_ollama(_FakeRequest(_generate_body(stream=False)), CLIENT_ID)

    assert excinfo.value.reason == dp.REASON_DNS_ADDRESS_NOT_ALLOWED
    assert "outside the pinned set" in excinfo.value.detail


# ── 3. The allowed hop: still streaming, still pinned, same timeout ────


async def test_chat_hop_streams_through_the_guard_and_pins_the_address(bridge) -> None:
    bridge["set_url"]("http://localhost:11440")
    bridge["set_resolver"]("127.0.0.1")
    captured = bridge["install_backend"]()
    lines = _sse(
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        "data: [DONE]",
    )
    bridge["monkeypatch"].setattr(_ollama, "_iter_sse_lines_with_watchdog", lines)

    response = await _ollama.chat_ollama(_FakeRequest(_chat_body(stream=True)), CLIENT_ID)
    chunks = [chunk async for chunk in response.body_iterator]

    # It is still a stream, translated to Ollama NDJSON, not a buffered body.
    assert response.media_type == "application/x-ndjson"
    text = "".join(chunks)
    assert '"content": "Hel"' in text and '"content": "lo"' in text
    assert '"done": true' in text

    # The guard connected to the *validated* address, keeping the approved name.
    request = captured["request"]
    assert str(request.url) == "http://127.0.0.1:11440/v1/chat/completions"
    assert request.headers["host"] == "localhost:11440"
    assert request.extensions["sni_hostname"] == "localhost"
    # The streaming timeout is the caller's (read=None → watchdog owns it).
    assert request.extensions["timeout"]["read"] is None
    assert request.extensions["timeout"]["write"] == 30.0


async def test_generate_nonstream_hop_stays_guarded_with_the_plain_timeout(bridge) -> None:
    bridge["set_url"]("http://localhost:11440")
    bridge["set_resolver"]("127.0.0.1")
    payload = b'{"choices":[{"message":{"role":"assistant","content":"hi"}}]}'
    captured = bridge["install_backend"](payload, "application/json")

    result = await _ollama.generate_ollama(
        _FakeRequest(_generate_body(stream=False)), CLIENT_ID
    )

    assert result["response"] == "hi"
    request = captured["request"]
    assert str(request.url) == "http://127.0.0.1:11440/v1/chat/completions"
    assert request.headers["host"] == "localhost:11440"
    # Non-streaming keeps the plain timeout for every phase.
    assert set(request.extensions["timeout"].values()) == {30.0}


# ── 4. G1-03: the refused hop is in the audit trail ────────────────────


def _audit_probe_app() -> FastAPI:
    """A minimal app serving the real ``/api/chat`` handler under audit."""
    probe = FastAPI()

    @probe.post("/api/chat")
    async def _chat(request: Request):
        return await _ollama.chat_ollama(request, CLIENT_ID)

    audit.install(probe, surface="proxy")
    return probe


def test_refused_chat_hop_emits_one_audit_record(bridge) -> None:
    bridge["set_url"]("http://not-approved.example:11440")
    audit.clear()

    client = TestClient(_audit_probe_app(), raise_server_exceptions=False)
    response = client.post("/api/chat", json=_chat_body(stream=True))

    records = [record for record in audit.recent() if record["route"] == "/api/chat"]
    assert len(records) == 1, f"expected exactly one audit record, got {records}"
    record = records[0]
    assert record["decision"] == audit.DENY
    assert record["reason"] == audit.REASON_DESTINATION_DENIED
    assert record["detail"] == dp.REASON_HOST_NOT_ALLOWED
    assert record["status"] == response.status_code
