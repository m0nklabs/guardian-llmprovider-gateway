"""Model-mismatch contract tests (incident 2026-09-01).

A client requested local model 'qwen3.8-27b-uncensored-ymq' while the backend
ran 'gemma-4-E4B-it-uncensored'.  The caretaker /ensure returned OK, the
post-ensure verification only LOGGED "MODEL MISMATCH", and the gateway still
forwarded the request — the client got HTTP 200 answered by the wrong model.

These tests pin the gateway-side contract: after a local-model switch/ensure
attempt, the request must FAIL closed (503 model_switch_failed) when the live
backend does not serve the requested model, on every local entry path
(OpenAI /v1 + Ollama /api/chat + /api/generate), while the same-model
cold-reload path keeps working.
"""

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.gateway import caretaker_runtime as crt
from app.proxy import server

REQUESTED = "qwen3.8-27b-uncensored-ymq"
ACTUAL = "gemma-4-E4B-it-uncensored"


@pytest.fixture(autouse=True)
def _bind_caretaker_runtime_manager():
    """Point caretaker_runtime's injected manager at the real server instance.

    test_caretaker_runtime.py resets the module to ``None`` in its fixtures,
    so tests that exercise the real verification helper must bind it
    explicitly.  The previous value is restored afterwards.
    """
    original = crt._model_manager
    crt._model_manager = server.model_manager
    yield
    crt._model_manager = original


class _ForbiddenAsyncClient:
    """Sentinel: any attempt to open an HTTP client means the request was
    forwarded to the backend — which the mismatch contract forbids."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "request must not be forwarded to llama-server after a "
            "model_switch_failed verification"
        )


def _openai_fake_request(model: str) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"Content-Type": "application/json"},
        state=SimpleNamespace(auth_context={"key_fingerprint": "test-key"}),
        url=SimpleNamespace(path="/v1/chat/completions"),
        method="POST",
    )


async def _openai_request_body(model: str, stream: bool = False) -> SimpleNamespace:
    request = _openai_fake_request(model)

    async def body() -> bytes:
        return json.dumps(
            {
                "model": model,
                "stream": stream,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")

    request.body = body
    return request


def _ollama_fake_request(payload: dict):
    request = SimpleNamespace(
        headers={"Content-Type": "application/json"},
        state=SimpleNamespace(auth_context={"key_fingerprint": "test-key"}),
        url=SimpleNamespace(path="/api/chat"),
        method="POST",
    )

    async def json_body():
        return payload

    request.json = json_body
    return request


def _routing_mismatch_patches(models: dict):
    """Common patch set for the /v1 local path with a switch/ensure attempt."""
    return (
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server._gw_routing, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._gw_routing, "_resolve_or_reject_inference_model", return_value=REQUESTED),
        patch.object(server._gw_routing, "_is_cloud_or_guardian_route", return_value=False),
        patch.object(server._gw_routing, "_run_guardian_operation", new_callable=AsyncMock),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value=ACTUAL)),
        patch.object(server.model_manager, "models", models),
        patch.object(server.model_manager, "is_unloaded", False),
        patch.object(server.model_manager, "is_switch_allowed", Mock(return_value=True)),
        patch.object(server.model_manager, "get_vision_capability", Mock(return_value={"configured": False})),
        patch.object(server.model_manager, "current_runtime_uses_mmproj", Mock(return_value=False)),
        patch.object(server.model_manager, "resolve_model", Mock(side_effect=lambda name: name)),
        patch.object(
            server.model_manager,
            "backend_serving_model_name",
            AsyncMock(return_value=ACTUAL),
        ),
    )


@pytest.mark.asyncio
async def test_v1_switch_verify_fails_closed_when_backend_serves_other_model():
    """Requested local model + /ensure OK but the backend serves another
    model → HTTP 503 with the OpenAI model_switch_failed body, NOT forwarded."""
    request = await _openai_request_body(REQUESTED)

    with ExitStack() as stack:
        for cm in _routing_mismatch_patches({REQUESTED: {}, ACTUAL: {}}):
            stack.enter_context(cm)
        stack.enter_context(patch("httpx.AsyncClient", _ForbiddenAsyncClient))
        response = await server.proxy_v1_post("chat/completions", request, client_id="goose")

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": {
            "message": (
                f"Model '{REQUESTED}' failed to load; backend serves '{ACTUAL}'"
            ),
            "type": "model_switch_failed",
            "code": "model_switch_failed",
        }
    }
    # Queue semantics: the error is tagged with the queued request's id.
    assert response.headers["X-Request-Id"] == "req-123"


@pytest.mark.asyncio
async def test_v1_switch_verify_fails_closed_for_completions_path():
    """The plain /v1/completions path gets the same fail-closed contract."""
    request = SimpleNamespace(
        headers={"Content-Type": "application/json"},
        state=SimpleNamespace(auth_context={"key_fingerprint": "test-key"}),
        url=SimpleNamespace(path="/v1/completions"),
        method="POST",
    )

    async def body() -> bytes:
        return json.dumps({"model": REQUESTED, "stream": False, "prompt": "hi"}).encode("utf-8")

    request.body = body

    with ExitStack() as stack:
        for cm in _routing_mismatch_patches({REQUESTED: {}, ACTUAL: {}}):
            stack.enter_context(cm)
        stack.enter_context(patch("httpx.AsyncClient", _ForbiddenAsyncClient))
        response = await server.proxy_v1_post("completions", request, client_id="goose")

    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "model_switch_failed"


@pytest.mark.asyncio
async def test_v1_same_model_reload_still_succeeds_and_forwards():
    """Same-model cold reload (backend unloaded, ensure loads exactly the
    requested model) must keep working: forwarded to llama-server, HTTP 200."""
    request = await _openai_request_body(ACTUAL)
    captured_request = {}

    class _FakeResponse:
        def __init__(self):
            self.content = b'{"choices": [{"message": {"role": "assistant", "content": "ok"}}]}'
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, content=None, headers=None):
            captured_request["url"] = url
            captured_request["content"] = content
            return _FakeResponse()

        async def aclose(self):
            return None

    before_active = server.model_manager.active_requests
    with (
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server._gw_routing, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._gw_routing, "_resolve_or_reject_inference_model", return_value=ACTUAL),
        patch.object(server._gw_routing, "_is_cloud_or_guardian_route", return_value=False),
        patch.object(server._gw_routing, "_resolve_auto_reload_model", return_value=ACTUAL),
        patch.object(server._gw_routing, "_run_guardian_operation", new_callable=AsyncMock) as run_op,
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value=ACTUAL)),
        patch.object(server.model_manager, "models", {ACTUAL: {}}),
        patch.object(server.model_manager, "is_unloaded", True),
        patch.object(server.model_manager, "get_vision_capability", Mock(return_value={"configured": False})),
        patch.object(server.model_manager, "current_runtime_uses_mmproj", Mock(return_value=False)),
        patch.object(server.model_manager, "resolve_model", Mock(side_effect=lambda name: name)),
        patch.object(
            server.model_manager,
            "backend_serving_model_name",
            AsyncMock(return_value=ACTUAL),
        ),
        patch("httpx.AsyncClient", _FakeAsyncClient),
    ):
        response = await server.proxy_v1_post("chat/completions", request, client_id="goose")

    assert response.status_code == 200
    forwarded = json.loads(captured_request["content"].decode("utf-8"))
    assert forwarded["model"] == ACTUAL
    assert captured_request["url"].endswith("/v1/chat/completions")
    # The cold reload ran through the guardian-operation lifecycle exactly once.
    run_op.assert_awaited_once()
    assert run_op.await_args.kwargs["phase"] == "auto_reload"
    # Queue/request accounting must stay balanced (no leaked slot).
    assert server.model_manager.active_requests == before_active


def _ollama_mismatch_patches():
    """Common patch set for the Ollama bridges with a switch/ensure attempt."""
    return (
        patch.object(server._local_ollama, "_resolve_or_reject_inference_model", return_value=REQUESTED),
        patch.object(server._local_ollama, "_is_cloud_or_guardian_route", return_value=False),
        patch.object(server._local_ollama, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._local_ollama, "_run_guardian_operation", new_callable=AsyncMock),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value=ACTUAL)),
        patch.object(server.model_manager, "models", {REQUESTED: {}, ACTUAL: {}}),
        patch.object(server.model_manager, "is_unloaded", False),
        patch.object(server.model_manager, "is_switch_allowed", Mock(return_value=True)),
        patch.object(server.model_manager, "resolve_model", Mock(side_effect=lambda name: name)),
        patch.object(
            server.model_manager,
            "backend_serving_model_name",
            AsyncMock(return_value=ACTUAL),
        ),
    )


@pytest.mark.asyncio
async def test_ollama_chat_fails_closed_when_backend_serves_other_model():
    """/api/chat with a requested local model the ensure did not actually load
    → HTTPException 503 model_switch_failed (the bridge's local-failure shape),
    NOT forwarded to llama-server."""
    request = _ollama_fake_request(
        {"model": REQUESTED, "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    )

    with ExitStack() as stack:
        for cm in _ollama_mismatch_patches():
            stack.enter_context(cm)
        stack.enter_context(patch("httpx.AsyncClient", _ForbiddenAsyncClient))
        with pytest.raises(HTTPException) as excinfo:
            await server.proxy_chat_ollama(request, client_id="goose")

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == {
        "error": "model_switch_failed",
        "message": f"Model '{REQUESTED}' failed to load; backend serves '{ACTUAL}'",
        "requested_model": REQUESTED,
        "actual_model": ACTUAL,
    }


@pytest.mark.asyncio
async def test_ollama_generate_fails_closed_when_backend_serves_other_model():
    """/api/generate gets the same fail-closed contract as /api/chat."""
    request = _ollama_fake_request({"model": REQUESTED, "prompt": "hi", "stream": False})

    with ExitStack() as stack:
        for cm in _ollama_mismatch_patches():
            stack.enter_context(cm)
        stack.enter_context(patch("httpx.AsyncClient", _ForbiddenAsyncClient))
        with pytest.raises(HTTPException) as excinfo:
            await server.proxy_generate_ollama(request, client_id="goose")

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"] == "model_switch_failed"
    assert excinfo.value.detail["requested_model"] == REQUESTED
    assert excinfo.value.detail["actual_model"] == ACTUAL


@pytest.mark.asyncio
async def test_ollama_chat_same_model_reload_still_succeeds():
    """Same-model cold reload on /api/chat: ensure loads exactly the requested
    model → the request proceeds (forwards to llama-server)."""
    request = _ollama_fake_request(
        {"model": ACTUAL, "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    )

    class _FakeResponse:
        content = b'{"choices": [{"message": {"role": "assistant", "content": "ok"}}]}'
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aread(self):
            return self.content

        async def aclose(self):
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, method, url, json=None, timeout=None, **kwargs):
            return SimpleNamespace(method=method, url=url)

        async def send(self, req, stream=False):
            return _FakeResponse()

        async def aclose(self):
            return None

    with (
        patch.object(server._local_ollama, "_resolve_or_reject_inference_model", return_value=ACTUAL),
        patch.object(server._local_ollama, "_is_cloud_or_guardian_route", return_value=False),
        patch.object(server._local_ollama, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._local_ollama, "_resolve_auto_reload_model", return_value=ACTUAL),
        patch.object(server._local_ollama, "_run_guardian_operation", new_callable=AsyncMock),
        patch.object(server._local_ollama, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value=ACTUAL)),
        patch.object(server.model_manager, "models", {ACTUAL: {}}),
        patch.object(server.model_manager, "is_unloaded", True),
        patch.object(server.model_manager, "resolve_model", Mock(side_effect=lambda name: name)),
        patch.object(
            server.model_manager,
            "backend_serving_model_name",
            AsyncMock(return_value=ACTUAL),
        ),
        patch("httpx.AsyncClient", _FakeAsyncClient),
    ):
        result = await server.proxy_chat_ollama(request, client_id="goose")

    assert result["done"] is True
    assert result["message"]["role"] == "assistant"
    assert result["model"] == ACTUAL


# ── Helper-contract tests (app.gateway.caretaker_runtime) ────────────


def _runtime_manager(**attrs) -> MagicMock:
    mgr = MagicMock()
    mgr.backend_serving_model_name = AsyncMock(return_value=None)
    mgr.resolve_model = Mock(side_effect=lambda name: name)
    for key, value in attrs.items():
        setattr(mgr, key, value)
    return mgr


@pytest.mark.asyncio
async def test_verify_helper_reports_actual_model_on_mismatch():
    mgr = _runtime_manager(backend_serving_model_name=AsyncMock(return_value=ACTUAL))
    crt.init(model_manager=mgr, caretaker_client=None)
    assert await crt.verify_requested_model_served(REQUESTED) == ACTUAL


@pytest.mark.asyncio
async def test_verify_helper_passes_when_requested_model_is_served():
    mgr = _runtime_manager(backend_serving_model_name=AsyncMock(return_value=REQUESTED))
    crt.init(model_manager=mgr, caretaker_client=None)
    assert await crt.verify_requested_model_served(REQUESTED) is None


@pytest.mark.asyncio
async def test_verify_helper_passes_on_unprobeable_backend():
    """No visible llama-server process (remote F6 topology) → cannot verify,
    never a mismatch (request behaves as before instead of a false 503)."""
    mgr = _runtime_manager(backend_serving_model_name=AsyncMock(return_value=None))
    crt.init(model_manager=mgr, caretaker_client=None)
    assert await crt.verify_requested_model_served(REQUESTED) is None


@pytest.mark.asyncio
async def test_verify_helper_canonicalizes_aliases_on_both_sides():
    """A client-facing alias and the canonical name must not be a mismatch."""
    canonical = {"alias-qwen": REQUESTED, REQUESTED: REQUESTED, ACTUAL: ACTUAL}
    mgr = _runtime_manager(
        backend_serving_model_name=AsyncMock(return_value=REQUESTED),
        resolve_model=Mock(side_effect=lambda name: canonical.get(name, name)),
    )
    crt.init(model_manager=mgr, caretaker_client=None)
    # Requested via alias, backend serves the canonical name — same model.
    assert await crt.verify_requested_model_served("alias-qwen") is None
    # But a genuinely different model is still reported.
    assert await crt.verify_requested_model_served(ACTUAL) == REQUESTED


@pytest.mark.asyncio
async def test_verify_helper_without_manager_is_never_a_mismatch():
    crt.init(model_manager=None, caretaker_client=None)
    assert await crt.verify_requested_model_served(REQUESTED) is None


# ── Manager probe contract (app.engine.manager) ──────────────────────


@pytest.mark.asyncio
async def test_backend_serving_model_name_returns_identified_model():
    with (
        patch.object(
            server.model_manager, "_get_backend_model_path", return_value="/models/gemma.gguf"
        ),
        patch.object(
            server.model_manager,
            "_identify_model_by_path",
            return_value=ACTUAL,
        ),
    ):
        assert await server.model_manager.backend_serving_model_name() == ACTUAL


@pytest.mark.asyncio
async def test_backend_serving_model_name_none_without_backend_process():
    with patch.object(server.model_manager, "_get_backend_model_path", return_value=None):
        assert await server.model_manager.backend_serving_model_name() is None
