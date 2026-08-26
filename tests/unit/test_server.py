"""Unit tests for Guardian server startup behavior."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.proxy.usage import ApiUsageTracker

from app.proxy.failover import FailoverCandidate, FailoverGroup, FailoverRegistry
from app.proxy import server
from app.gateway import streaming as _streaming
from app.gateway import queue_helpers as _queue_helpers


def _metadata_request(key_fingerprint: str = "test-key") -> SimpleNamespace:
    """Build the minimal authenticated request shape used by metadata routes."""
    return SimpleNamespace(state=SimpleNamespace(auth_context={"key_fingerprint": key_fingerprint}))


@pytest.mark.asyncio
async def test_lifespan_does_not_wait_for_startup_check(tmp_path: Path):
    """Guardian should bind immediately even if backend startup verification is slow."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_startup_check() -> None:
        started.set()
        await release.wait()

    async def fake_idle_unload_watcher() -> None:
        await asyncio.Future()

    pid_file = tmp_path / "guardian-startup-test.pid"

    with (
        patch.object(server._process, "_pid_file", str(pid_file)),
        patch.object(server.model_manager, "startup_check", fake_startup_check),
        patch.object(server, "_idle_unload_watcher", fake_idle_unload_watcher),
        patch.object(server._lifespan, "_get_proxy_listener_info", return_value=None),
    ):
        start = asyncio.get_running_loop().time()
        async with server.lifespan(server.app):
            elapsed = asyncio.get_running_loop().time() - start
            assert elapsed < 0.1

            await asyncio.sleep(0)

            status = server._get_startup_check_status()
            assert started.is_set()
            assert status["state"] == "checking"
            assert status["task_active"] is True

            release.set()
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_background_startup_check_marks_ready():
    """Successful startup verification should publish a ready state."""

    async def fake_startup_check() -> None:
        return None

    with patch.object(server.model_manager, "startup_check", fake_startup_check):
        generation = server._reset_startup_check_status(
            source="startup",
            phase="startup_check",
            target_model="GLM-4.7-Flash",
            requested_model="GLM-4.7-Flash",
        )

        await server._run_startup_check_in_background(generation, "GLM-4.7-Flash")

        status = server._get_startup_check_status()
        assert status["state"] in {"ready", "degraded"}


@pytest.mark.asyncio
async def test_lifespan_tolerates_existing_live_pid_file(tmp_path: Path):
    """A live PID in guardian.pid should not kill a restart; the new process should overwrite it."""

    async def fake_startup_check() -> None:
        return None

    async def fake_idle_unload_watcher() -> None:
        await asyncio.Future()

    pid_file = tmp_path / "guardian-live-pid-test.pid"
    pid_file.write_text(f"{os.getppid()}\n")

    with (
        patch.object(server._process, "_pid_file", str(pid_file)),
        patch.object(server.model_manager, "startup_check", fake_startup_check),
        patch.object(server, "_idle_unload_watcher", fake_idle_unload_watcher),
        patch.object(server._lifespan, "_get_proxy_listener_info", return_value=None),
    ):
        async with server.lifespan(server.app):
            assert pid_file.exists()
            assert pid_file.read_text().strip() == str(os.getpid())


def test_stale_status_update_is_ignored():
    """Older background operations must not overwrite newer runtime status."""
    generation_one = server._reset_startup_check_status(
        source="startup",
        phase="startup_check",
        target_model="Step3-VL-10B",
        requested_model="Step3-VL-10B",
    )
    server._mark_startup_check_status(
        "checking",
        generation=generation_one,
        source="startup",
        phase="startup_check",
        owner="startup",
        target_model="Step3-VL-10B",
        requested_model="Step3-VL-10B",
    )

    generation_two = server._reset_startup_check_status(
        source="admin",
        phase="manual_load",
        target_model="Qwen-Agent",
        requested_model="qwen-agent",
    )
    server._mark_startup_check_status(
        "switching",
        generation=generation_two,
        source="admin",
        phase="manual_load",
        owner="claudecode",
        target_model="Qwen-Agent",
        requested_model="qwen-agent",
    )

    server._mark_startup_check_status(
        "ready",
        generation=generation_one,
        effective_model="Step3-VL-10B",
    )

    status = server._get_startup_check_status()
    assert status["source"] == "admin"
    assert status["phase"] == "manual_load"
    assert status["state"] == "switching"
    assert status["owner"] == "claudecode"
    assert status["effective_model"] is None


def test_desired_runtime_vision_enabled_requires_image_and_configured_model():
    with patch.object(
        server.model_manager,
        "get_vision_capability",
        return_value={"configured": True},
    ):
        assert server._desired_runtime_vision_enabled("Vision-Model", True) is True
        assert server._desired_runtime_vision_enabled("Vision-Model", False) is False


def test_resolve_inference_model_prefers_tool_profile():
    """Auto requests should prefer a tool-friendly sibling when available."""
    with patch.object(server.model_manager, "get_preferred_tool_model", return_value="Qwen-Agent"):
        assert server._resolve_inference_model("auto", "Qwen-Deep") == "Qwen-Agent"


def test_resolve_or_reject_inference_model_rejects_unserved_model():
    """Unknown inference models should be rejected before queue admission."""
    with (
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}}),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not found")),
    ):
        with pytest.raises(server.HTTPException) as exc_info:
            server._resolve_or_reject_inference_model("ghost-model", "Qwen-Agent")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "model_not_served"
    assert exc_info.value.detail["reason"] == "requested_model_not_served"
    assert exc_info.value.detail["requested_model"] == "ghost-model"


def test_reasoning_falls_back_for_ollama_clients():
    """Ollama bridges should use reasoning text when no visible content is present."""
    assert server._extract_assistant_delta_text({"reasoning_content": "thinking"}) == "thinking"
    assert server._extract_assistant_message_text({"reasoning_content": "answer"}) == "answer"


def test_request_reasoning_defaults_keep_normal_chat_reasoning_enabled():
    payload = {"model": "Qwen-Agent", "messages": [{"role": "user", "content": "hi"}]}

    with patch.object(server.model_manager, "models", {"Qwen-Agent": {"profile_role": "agent"}}):
        changed = server._apply_request_reasoning_defaults("chat/completions", payload, "Qwen-Agent")

    assert changed is False
    assert "reasoning_budget" not in payload
    assert "chat_template_kwargs" not in payload


def test_request_reasoning_defaults_disable_thinking_for_embedding_chat_payload():
    payload = {"model": "nomic-embed", "messages": [{"role": "user", "content": "embed this"}]}

    with patch.object(
        server.model_manager,
        "models",
        {"nomic-embed": {"model_type": "embedding", "default_enable_thinking": False}},
    ):
        changed = server._apply_request_reasoning_defaults("chat/completions", payload, "nomic-embed")

    assert changed is True
    assert payload["reasoning_budget"] == 0
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_request_reasoning_defaults_honor_explicit_no_thinking_request():
    payload = {
        "model": "Qwen-Agent",
        "reasoning_budget": 0,
        "messages": [{"role": "user", "content": "short answer"}],
    }

    with patch.object(server.model_manager, "models", {"Qwen-Agent": {"profile_role": "agent"}}):
        changed = server._apply_request_reasoning_defaults("chat/completions", payload, "Qwen-Agent")

    assert changed is True
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_sanitize_messages_for_qwen_chat_template_demotes_late_system_messages():
    messages = [
        {"role": "system", "content": "primary"},
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "update"},
        {"role": "assistant", "content": "ok"},
    ]

    sanitized = server._sanitize_messages_for_qwen_chat_template(messages)

    assert sanitized[0] == {"role": "system", "content": "primary"}
    assert sanitized[2]["role"] == "user"
    assert sanitized[2]["content"] == "[System Context Update]:\nupdate"
    assert sanitized[3] == {"role": "assistant", "content": "ok"}


def test_sanitize_messages_for_qwen_chat_template_preserves_multimodal_content():
    messages = [
        {"role": "system", "content": "primary"},
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "update"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        },
    ]

    sanitized = server._sanitize_messages_for_qwen_chat_template(messages)

    assert sanitized[1]["role"] == "user"
    assert sanitized[1]["content"].startswith("[System Context Update]:\nupdate")
    assert "image_url" in sanitized[1]["content"]


def test_stream_watchdog_extends_timeout_for_healthy_progress():
    watchdog = server.StreamProgressWatchdog(base_timeout_s=300)

    for index in range(16):
        line = f'data: {json.dumps({"choices": [{"delta": {"content": f"token-{index}"}}]})}'
        watchdog.observe_sse_line(line)

    assert watchdog.loop_detected is False
    assert watchdog.healthy_chunk_count == 16
    assert watchdog.current_timeout_s == 450.0


def test_stream_watchdog_refuses_to_extend_repeated_chunks():
    watchdog = server.StreamProgressWatchdog(base_timeout_s=300)
    repeated_line = f'data: {json.dumps({"choices": [{"delta": {"content": "loop"}}]})}'

    for _ in range(server.STREAM_LOOP_REPEAT_THRESHOLD + 2):
        watchdog.observe_sse_line(repeated_line)

    assert watchdog.loop_detected is True
    assert watchdog.healthy_chunk_count == 1
    assert watchdog.current_timeout_s == 300.0


def test_get_usage_attribution_falls_back_to_request_headers_without_auth_context():
    request = SimpleNamespace(
        state=SimpleNamespace(),
        scope={},
        client=SimpleNamespace(host="10.0.0.8", port=5555),
        headers={
            "authorization": "Bearer definitely-not-a-real-key",
            "host": "guardian.local",
            "user-agent": "guardian-debug-check/1.0",
        },
    )

    attribution = server._get_usage_attribution(request)

    assert attribution["source_ip"] == "10.0.0.8"
    assert attribution["header_name"] == "authorization"
    assert attribution["key_prefix"] == "legacy"
    assert attribution["key_fingerprint"]
    assert attribution["user_agent"] == "guardian-debug-check/1.0"
    assert attribution["valid"] is False


def test_start_live_request_usage_seeds_request_auth_context(tmp_path: Path):
    tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")
    request = SimpleNamespace(
        state=SimpleNamespace(),
        scope={},
        client=SimpleNamespace(host="10.0.0.8", port=5555),
        headers={"user-agent": "guardian-missing-key/1.0"},
        url=SimpleNamespace(path="/v1/models"),
        method="GET",
    )

    with patch.object(server._usage, "_state", SimpleNamespace(api_usage=tracker)):
        server._start_live_request_usage(request)

    assert request.state.auth_context["source_ip"] == "10.0.0.8"
    assert request.state.auth_context["user_agent"] == "guardian-missing-key/1.0"
    assert request.scope["guardian_auth_context"]["source_ip"] == "10.0.0.8"
    snapshot = tracker.snapshot()
    assert snapshot["active_requests"][0]["source_ip"] == "10.0.0.8"
    assert snapshot["active_requests"][0]["user_agent"] == "guardian-missing-key/1.0"


@pytest.mark.asyncio
async def test_begin_queued_request_cleans_up_waiter_on_disconnect():
    queue = server.InferenceQueue(max_concurrent=1)
    blocker_id = await queue.acquire("blocker", "model-x")
    disconnected = asyncio.Event()

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return disconnected.is_set()

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        waiter_task = asyncio.create_task(
            server._begin_queued_request(_FakeRequest(), "waiter-client", "model-x")
        )
        await asyncio.sleep(0.05)

        assert queue.waiting_count == 1

        disconnected.set()

        with pytest.raises(server._GuardianRequestCancelled, match="client_disconnected"):
            await asyncio.wait_for(waiter_task, timeout=1.0)

        status = queue.get_status(client_id="waiter-client")
        assert status["your_status"] == "cancelled"
        assert status["your_cancel_reason"] == "client_disconnected"
        assert queue.waiting_count == 0

    queue.release(blocker_id)


@pytest.mark.asyncio
async def test_begin_queued_request_rejects_unauthenticated_client():
    queue = server.InferenceQueue(max_concurrent=1)

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        with pytest.raises(server.HTTPException) as exc_info:
            await server._begin_queued_request(_FakeRequest(), "unauthenticated", "model-x")

    assert exc_info.value.status_code == 401
    assert queue.waiting_count == 0
    assert queue.active_count == 0


@pytest.mark.asyncio
async def test_begin_queued_request_waits_behind_running_request_for_same_api_key():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = queue.submit("openclaw", "model-x", owner_id="key:abc123")
    await queue.wait_for_turn(request_id)

    fake_request = SimpleNamespace(
        state=SimpleNamespace(auth_context={"key_fingerprint": "abc123"}),
        is_disconnected=lambda: asyncio.sleep(0, result=False),
    )

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        waiter_task = asyncio.create_task(server._begin_queued_request(fake_request, "openclaw", "model-y"))
        await asyncio.sleep(0.05)

        assert not waiter_task.done()
        assert queue.active_count == 1
        assert queue.waiting_count == 1

        queue.release(request_id)
        waiter_id, disconnect_task = await asyncio.wait_for(waiter_task, timeout=1.0)

        assert waiter_id != request_id
        assert queue.active_count == 1
        assert queue.waiting_count == 0

        await server._stop_background_task(disconnect_task)
        queue.release(waiter_id)



@pytest.mark.asyncio
async def test_begin_queued_request_allows_multiple_queued_requests_for_same_api_key():
    queue = server.InferenceQueue(max_concurrent=1)
    blocker_id = await queue.acquire("blocker", "model-x", owner_id="key:blocker")
    queued_id = queue.submit("openclaw", "model-x", owner_id="key:abc123")

    fake_request = SimpleNamespace(
        state=SimpleNamespace(auth_context={"key_fingerprint": "abc123"}),
        is_disconnected=lambda: asyncio.sleep(0, result=False),
    )

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        waiter_task = asyncio.create_task(server._begin_queued_request(fake_request, "openclaw", "model-y"))
        await asyncio.sleep(0.05)

        assert not waiter_task.done()
        assert queue.waiting_count == 2
        assert queued_id in queue._waiting

        queue.cancel(queued_id, owner_id="key:abc123", reason="test_cleanup")
        queue.release(blocker_id)
        waiter_id, disconnect_task = await asyncio.wait_for(waiter_task, timeout=1.0)

        assert waiter_id != queued_id
        await server._stop_background_task(disconnect_task)
        queue.release(waiter_id)



@pytest.mark.asyncio
async def test_await_or_cancel_request_cleans_up_running_request():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = queue.submit("runner", "model-x")
    await queue.wait_for_turn(request_id)
    cleanup_called = asyncio.Event()

    async def _cleanup() -> None:
        cleanup_called.set()

    operation_task = asyncio.create_task(asyncio.sleep(10))

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        queue.cancel(request_id, client_id="runner", reason="client_requested_cancel")

        with pytest.raises(server._GuardianRequestCancelled, match="client_requested_cancel"):
            await server._await_or_cancel_request(operation_task, request_id, cleanup=_cleanup)

        queue.finish(request_id, outcome=server._request_outcome(request_id))
        assert queue.active_count == 0
        assert queue.get_request_status(request_id, client_id="runner")["status"] == "cancelled"

    assert cleanup_called.is_set()
    assert operation_task.cancelled() is True


@pytest.mark.asyncio
async def test_queue_request_status_endpoint_returns_snapshot():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = queue.submit("observer", "model-x", owner_id="key:observer")
    request = SimpleNamespace(state=SimpleNamespace(auth_context={"key_fingerprint": "observer"}))

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        snapshot = await server.queue_request_status(request_id, request=request, client_id="observer")

    assert snapshot["request_id"] == request_id
    assert snapshot["status"] == "queued"
    assert snapshot["position"] == 1


@pytest.mark.asyncio
async def test_cancel_queue_request_endpoint_marks_running_request():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = await queue.acquire("observer", "model-x", owner_id="key:observer")
    request = SimpleNamespace(state=SimpleNamespace(auth_context={"key_fingerprint": "observer"}))

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        snapshot = await server.cancel_queue_request(request_id, request=request, client_id="observer")

    assert snapshot["request_id"] == request_id
    assert snapshot["status"] == "cancelling"
    assert snapshot["cancel_reason"] == "client_requested_cancel"


@pytest.mark.asyncio
async def test_queue_request_status_endpoint_is_isolated_per_api_key():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = queue.submit("observer", "model-x", owner_id="key:owner-a")
    foreign_request = SimpleNamespace(state=SimpleNamespace(auth_context={"key_fingerprint": "owner-b"}))

    with patch.object(server, "inference_queue", queue), patch.object(_queue_helpers, "_inference_queue", queue), patch.object(server._admin_api, "_inference_queue", queue):
        with pytest.raises(server.HTTPException) as exc_info:
            await server.queue_request_status(request_id, request=foreign_request, client_id="observer")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_non_gpu_v1_route_bypasses_queue_even_with_same_key_busy():
    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/models")
            self.method = "POST"

        async def body(self) -> bytes:
            return b'{"input":"metadata only"}'

    class _FakeAsyncClient:
        class _FakeHTTPXResponse:
            def __init__(self):
                self.content = b'{"ok":true}'
                self.status_code = 200
                self.headers = {"content-type": "application/json"}

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, content=None, headers=None):
            return self._FakeHTTPXResponse()

    request = _FakeRequest()

    with (
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server._gw_routing, "_begin_queued_request", side_effect=AssertionError("queue should not be used")),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _FakeAsyncClient),
    ):
        response = await server.proxy_v1_post("models", request, client_id="openclaw")

    assert response.status_code == 200
    assert response.body == b'{"ok":true}'


@pytest.mark.asyncio
async def test_v1_inference_rejects_unserved_model_before_queue_admission():
    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/chat/completions")
            self.method = "POST"

        async def body(self) -> bytes:
            return json.dumps({"model": "ghost-model", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")

    request = _FakeRequest()

    with (
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server._gw_routing, "_begin_queued_request", side_effect=AssertionError("queue should not be used")),
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}}),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="Qwen-Agent")),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not found")),
    ):
        with pytest.raises(server.HTTPException) as exc_info:
            await server.proxy_v1_post("chat/completions", request, client_id="openclaw")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "model_not_served"
    assert exc_info.value.detail["reason"] == "requested_model_not_served"
    assert exc_info.value.detail["requested_model"] == "ghost-model"


@pytest.mark.asyncio
async def test_v1_chat_completions_sanitizes_late_system_messages_before_forwarding():
    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/chat/completions")
            self.method = "POST"

        async def body(self) -> bytes:
            return json.dumps(
                {
                        "model": "auto",
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": "primary"},
                        {"role": "user", "content": "hello"},
                        {"role": "system", "content": "update"},
                    ],
                }
            ).encode("utf-8")

    captured_request = {}

    class _FakeResponse:
        def __init__(self):
            self.content = b'{"ok":true}'
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

        async def aiter_lines(self):
            if False:
                yield ""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method, url, json=None, content=None, timeout=None, headers=None):
            captured_request["method"] = method
            captured_request["url"] = url
            captured_request["json"] = json
            captured_request["content"] = content
            captured_request["timeout"] = timeout
            return SimpleNamespace(
                method=method,
                url=url,
                json=json,
                content=content,
                timeout=timeout,
                headers=headers,
            )

        async def send(self, req, stream=False):
            captured_request["stream"] = stream
            return _FakeResponse()

        async def post(self, url, content=None, headers=None):
            captured_request["method"] = "POST"
            captured_request["url"] = url
            captured_request["content"] = content
            captured_request["headers"] = headers
            return _FakeResponse()

        async def aclose(self):
            return None

    request = _FakeRequest()

    with (
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server._gw_routing, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._gw_routing, "_resolve_or_reject_inference_model", return_value="Qwen-Agent"),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="Qwen-Agent")),
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}, "auto": {}}),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _FakeAsyncClient),
    ):
        response = await server.proxy_v1_post("chat/completions", request, client_id="openclaw")

    assert response.status_code == 200
    assert captured_request["method"] == "POST"
    assert captured_request["url"].endswith("/v1/chat/completions")
    forwarded = json.loads(captured_request["content"].decode("utf-8"))
    assert forwarded["messages"][0]["role"] == "system"
    assert forwarded["messages"][2]["role"] == "user"
    assert forwarded["messages"][2]["content"] == "[System Context Update]:\nupdate"


@pytest.mark.asyncio
async def test_v1_chat_completions_keeps_stream_flag_while_sanitizing_messages():
    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/chat/completions")
            self.method = "POST"

        async def body(self) -> bytes:
            return json.dumps(
                {
                        "model": "auto",
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "primary"},
                        {"role": "system", "content": "update"},
                    ],
                }
            ).encode("utf-8")

    captured_request = {}

    class _FakeResponse:
        def __init__(self):
            self.request = server.httpx.Request("POST", "http://127.0.0.1:11440/v1/chat/completions")
            self.status_code = 200
            self.headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"choices": [{"delta": {"content": "ok"}}]}'

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method, url, json=None, content=None, timeout=None, headers=None):
            captured_request["method"] = method
            captured_request["url"] = url
            captured_request["json"] = json
            captured_request["content"] = content
            captured_request["timeout"] = timeout
            return SimpleNamespace(
                method=method,
                url=url,
                json=json,
                content=content,
                timeout=timeout,
                headers=headers,
            )

        async def send(self, req, stream=False):
            captured_request["stream"] = stream
            return _FakeResponse()

        async def post(self, url, content=None, headers=None):
            captured_request["method"] = "POST"
            captured_request["url"] = url
            captured_request["content"] = content
            captured_request["headers"] = headers
            return _FakeResponse()

        async def aclose(self):
            return None

    request = _FakeRequest()

    with (
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server._gw_routing, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._gw_routing, "_resolve_or_reject_inference_model", return_value="Qwen-Agent"),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="Qwen-Agent")),
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}, "auto": {}}),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _FakeAsyncClient),
    ):
        response = await server.proxy_v1_post("chat/completions", request, client_id="openclaw")

    assert response.status_code == 200
    assert captured_request["stream"] is True
    forwarded = json.loads(captured_request["content"].decode("utf-8"))
    assert forwarded["messages"][1]["role"] == "user"
    assert forwarded["messages"][1]["content"] == "[System Context Update]:\nupdate"


@pytest.mark.asyncio
async def test_stream_watchdog_emits_keepalive_before_timeout():
    release_line = asyncio.Event()

    class _FakeResponse:
        def __init__(self):
            self.request = server.httpx.Request("POST", "http://127.0.0.1:11440/v1/chat/completions")

        async def aiter_lines(self):
            await release_line.wait()
            yield 'data: {"choices": [{"delta": {"content": "hello"}}]}'

    response = _FakeResponse()
    watchdog = server.StreamProgressWatchdog(base_timeout_s=0.2)
    stream_iter = server._iter_sse_lines_with_watchdog(
        response,
        watchdog,
        request_id="req-heartbeat",
        route="/v1/chat/completions",
        heartbeat_interval_s=0.01,
    )

    try:
        first = await asyncio.wait_for(stream_iter.__anext__(), timeout=0.1)
        second = await asyncio.wait_for(stream_iter.__anext__(), timeout=0.1)
        release_line.set()
        third = await asyncio.wait_for(stream_iter.__anext__(), timeout=0.1)
    finally:
        await stream_iter.aclose()

    assert first == ": guardian-keepalive request_id=req-heartbeat"
    assert second == ""
    assert third.startswith("data: ")


@pytest.mark.asyncio
async def test_stream_watchdog_exits_when_queue_request_is_cancelled():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = await queue.acquire("runner", "model-x")

    class _FakeResponse:
        def __init__(self):
            self.request = server.httpx.Request("POST", "http://127.0.0.1:11440/v1/chat/completions")

        async def aiter_lines(self):
            await asyncio.Future()
            yield ""

    response = _FakeResponse()
    watchdog = server.StreamProgressWatchdog(base_timeout_s=300)

    with patch.object(server, "inference_queue", queue), patch.object(_streaming, "_inference_queue", queue):
        stream_iter = server._iter_sse_lines_with_watchdog(
            response,
            watchdog,
            request_id=request_id,
            route="/v1/chat/completions",
            cancel_event=queue.get_cancel_event(request_id),
        )
        queue.cancel(request_id, client_id="runner", reason="client_requested_cancel")

        try:
            with pytest.raises(server._GuardianRequestCancelled, match="client_requested_cancel"):
                await asyncio.wait_for(stream_iter.__anext__(), timeout=0.1)
        finally:
            await stream_iter.aclose()


@pytest.mark.asyncio
async def test_stream_watchdog_timeout_logs_request_context():
    class _NeverYields:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise AssertionError("wait_for should time out before consuming the iterator")

    class _FakeResponse:
        def __init__(self):
            self.request = server.httpx.Request("POST", "http://127.0.0.1:11440/v1/chat/completions")

        def aiter_lines(self):
            return _NeverYields()

    async def _timeout(*args, **kwargs):
        awaitable = args[0] if args else None
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError()

    response = _FakeResponse()
    watchdog = server.StreamProgressWatchdog(base_timeout_s=300)

    with (
        patch.object(server.asyncio, "wait_for", side_effect=_timeout),
        patch.object(server.logger, "warning") as warning_mock,
    ):
        stream_iter = server._iter_sse_lines_with_watchdog(
            response,
            watchdog,
            request_id="req-123",
            route="/v1/chat/completions",
            client_id="hermes-ai-kvm2",
            model_name="Qwen3.6-35B-A3B-HauhauCS-Aggressive",
        )

        with pytest.raises(server.httpx.ReadTimeout, match="request_id=req-123"):
            await stream_iter.__anext__()

    warning_text = warning_mock.call_args[0][0]
    assert "route=/v1/chat/completions" in warning_text
    assert "client=hermes-ai-kvm2" in warning_text
    assert "model=Qwen3.6-35B-A3B-HauhauCS-Aggressive" in warning_text


def test_get_model_size_recognizes_large_35b_and_31b_models():
    assert server.get_model_size("Qwen3.6-35B-A3B-HauhauCS-Aggressive") == 22000
    assert server.get_model_size("gemma-4-31B-it-uncensored-heretic") == 20000


def test_messages_contain_image_input_detects_openai_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]

    assert server._messages_contain_image_input(messages) is True


def test_messages_contain_image_input_detects_anthropic_image_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
                },
            ],
        }
    ]

    assert server._messages_contain_image_input(messages) is True


@pytest.mark.parametrize("messages", [None, {}, "not a message list", [{"content": None}], [None]])
def test_messages_contain_image_input_ignores_malformed_messages(messages):
    assert server._messages_contain_image_input(messages) is False


@pytest.mark.parametrize(
    "model_name",
    [
        "openrouter/z-ai/glm-5.2",
        "nvidia/z-ai/glm-5.2",
        "failover/glm-5.2",
        "z-ai/glm-5.2",
    ],
)
def test_cloud_vision_fallback_resolves_by_underlying_model(tmp_path: Path, model_name: str):
    config_path = tmp_path / "cloud_keys.json"
    config_path.write_text(
        json.dumps(
            {
                "failover_groups": {
                    "glm-5.2": {
                        "candidates": [
                            {"provider": "nvidia", "model": "z-ai/glm-5.2"},
                            {"provider": "openrouter", "model": "z-ai/glm-5.2"},
                        ],
                        "image_fallback": {
                            "local_model": "Qwen3.6-35B-A3B-HauhauCS-Aggressive-Q8KV"
                        },
                    }
                }
            }
        )
    )
    registry = FailoverRegistry(config_path)

    with patch.object(server._cloud_routing, "_failover_registry", registry):
        fallback = server._resolve_cloud_vision_fallback(model_name)

    assert fallback == "Qwen3.6-35B-A3B-HauhauCS-Aggressive-Q8KV"


def test_cloud_vision_fallback_ignores_unconfigured_models(tmp_path: Path):
    registry = FailoverRegistry(tmp_path / "cloud_keys.json")

    with patch.object(server._cloud_routing, "_failover_registry", registry):
        fallback = server._resolve_cloud_vision_fallback("openrouter/openai/gpt-4o")

    assert fallback is None


@pytest.mark.parametrize(
    "model_name",
    [
        "openrouter/acme/vision-model",
        "failover/vision-model",
        "acme/vision-model",
    ],
)
def test_cloud_vision_fallback_skips_image_capable_models(tmp_path: Path, model_name: str):
    config_path = tmp_path / "cloud_keys.json"
    config_path.write_text(
        json.dumps(
            {
                "failover_groups": {
                    "vision-model": {
                        "candidates": [
                            {
                                "provider": "openrouter",
                                "model": "acme/vision-model",
                                "modalities": ["text", "image"],
                            }
                        ],
                        "image_fallback": {"local_model": "local-vision-model"},
                    }
                }
            }
        )
    )
    registry = FailoverRegistry(config_path)

    with patch.object(server._cloud_routing, "_failover_registry", registry):
        fallback = server._resolve_cloud_vision_fallback(model_name)

    assert fallback is None


def _routing_configured_registry():
    """A stub ProviderRegistry whose settings providers are all configured.

    The routing module now resolves the attempt provider from the *settings*
    provider registry (not a credential store), so tests must provide
    configured providers for the provider names they exercise.
    """
    providers = {
        "openrouter": CloudProvider(
            "openrouter", "https://openrouter.ai/api/v1", "sk-or-test"
        ),
        "nvidia": CloudProvider(
            "nvidia", "https://integrate.api.nvidia.com/v1", "nvapi-test"
        ),
        "google": CloudProvider(
            "google",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gk-test",
        ),
    }
    reg = SimpleNamespace()
    reg._providers = providers
    reg.get_provider_for_model = lambda m: providers.get("openrouter")
    return reg


def _routing_auth_request(cloud_gateway_access: bool = True):
    return SimpleNamespace(
        state=SimpleNamespace(auth_context={"cloud_gateway_access": cloud_gateway_access})
    )


def test_cloud_attempts_for_images_skip_text_only_failover_candidates():
    group = FailoverGroup(
        name="mixed-capabilities",
        candidates=[
            FailoverCandidate("nvidia", "acme/text-model"),
            FailoverCandidate("openrouter", "acme/vision-model", ("text", "image")),
        ],
    )
    request = _routing_auth_request()

    with (
        patch.object(server.failover_registry, "get_group", return_value=group),
        patch.object(server.failover_health, "order_candidates", return_value=group.candidates),
        patch.object(server._cloud_routing, "_provider_registry", _routing_configured_registry()),
    ):
        attempts, failover_group = server._resolve_cloud_attempts(
            "failover/mixed-capabilities",
            request,
            "goose",
            requires_vision=True,
        )

    assert failover_group == "mixed-capabilities"
    assert [(provider.name, model) for provider, model in attempts] == [
        ("openrouter", "acme/vision-model")
    ]


def test_cloud_attempts_normalize_openrouter_model_alias():
    request = _routing_auth_request()
    provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
    )

    with patch.object(server._cloud_routing, "_provider_registry", _routing_configured_registry()):
        attempts, failover_group = server._resolve_cloud_attempts(
            "openrouter/moonshotai/kimi-k3",
            request,
            "goose",
        )

    assert failover_group is None
    assert [(attempt.name, upstream_model) for attempt, upstream_model in attempts] == [
        ("openrouter", "moonshotai/kimi-k3")
    ]


def test_cloud_attempts_resolve_google_full_address():
    request = _routing_auth_request()

    with patch.object(server._cloud_routing, "_provider_registry", _routing_configured_registry()):
        attempts, failover_group = server._resolve_cloud_attempts(
            "google/google/gemini-2.5-flash",
            request,
            "goose",
        )

    assert failover_group is None
    assert [(attempt.name, attempt.base_url, upstream_model) for attempt, upstream_model in attempts] == [
        (
            "google",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            # cold-start fallback: with no warm catalog entry for google, the
            # upstream model falls back to the {brand}/{model} remainder of the
            # address (google/gemini-2.5-flash).
            "google/gemini-2.5-flash",
        )
    ]


def test_cloud_attempts_reject_unknown_provider_catalog():
    request = _routing_auth_request()

    with (
        patch.object(server._cloud_routing, "_provider_registry", _routing_configured_registry()),
        patch.object(server.cloud_catalog, "resolve_cloud_target", return_value=None),
        patch.object(server._cloud_routing, "_cloud_provider_for_request", return_value=None),
        pytest.raises(server.HTTPException) as exc_info,
    ):
        server._resolve_cloud_attempts(
            "unknown/brand/model",
            request,
            "goose",
        )

    assert exc_info.value.status_code == 404


def test_cloud_attempts_denied_key_raises_403():
    request = _routing_auth_request(cloud_gateway_access=False)

    with patch.object(server._cloud_routing, "_provider_registry", _routing_configured_registry()):
        with pytest.raises(server.HTTPException) as exc_info:
            server._resolve_cloud_attempts(
                "openrouter/moonshotai/kimi-k3",
                request,
                "goose",
            )

    assert exc_info.value.status_code == 403


def test_cloud_attempts_skip_unconfigured_failover_candidate():
    group = FailoverGroup(
        name="google-catalog",
        candidates=[FailoverCandidate("google", "gemini-2.5-pro")],
    )
    request = _routing_auth_request()
    # Only openrouter/nvidia configured; google is absent -> candidate skipped.
    reg = _routing_configured_registry()
    del reg._providers["google"]

    with (
        patch.object(server.failover_registry, "get_group", return_value=group),
        patch.object(server.failover_health, "order_candidates", return_value=group.candidates),
        patch.object(server._cloud_routing, "_provider_registry", reg),
        pytest.raises(server.HTTPException) as exc_info,
    ):
        server._resolve_cloud_attempts("failover/google-catalog", request, "goose")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_image_fallback_validates_cloud_route_before_local_model_resolution():
    class _FakeRequest:
        headers = {"Content-Type": "application/json"}
        state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
        url = SimpleNamespace(path="/v1/chat/completions")
        method = "POST"

        async def body(self) -> bytes:
            return json.dumps(
                {
                    "model": "openrouter/z-ai/glm-5.2",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe this"},
                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                            ],
                        }
                    ],
                }
            ).encode("utf-8")

    with (
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
        patch.object(server._gw_routing, "_resolve_or_reject_inference_model", return_value="openrouter/z-ai/glm-5.2"),
        patch.object(server._gw_routing, "_resolve_cloud_vision_fallback", return_value="vision-model"),
        patch.object(
            server._gw_routing,
            "_resolve_cloud_attempts",
            side_effect=server.HTTPException(status_code=403, detail="cloud credential not linked"),
        ),
        patch.object(
            server,
            "_resolve_inference_model",
            side_effect=AssertionError("local fallback must not run before cloud route validation"),
        ),
    ):
        with pytest.raises(server.HTTPException) as exc_info:
            await server.proxy_v1_post("chat/completions", _FakeRequest(), client_id="goose")

    assert exc_info.value.status_code == 403


def test_map_multimodal_backend_error_returns_clean_422():
    with patch.object(server.model_manager, "mark_vision_validation") as mark_validation:
        response = server._map_multimodal_backend_error(
            "Qwen3-VL-30B-A3B-Thinking",
            500,
            b"Internal Server Error",
            "req-123",
            0,
        )

    assert response is not None
    assert response.status_code == 422
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "vision_runtime_unavailable"
    mark_validation.assert_called_once_with(
        "Qwen3-VL-30B-A3B-Thinking",
        "unsupported",
        "Internal Server Error",
    )


@pytest.mark.asyncio
async def test_list_models_includes_vision_metadata():
    with (
        patch.object(server.model_manager, "get_current_model", return_value="Vision-Model"),
        patch.object(server.model_manager, "get_public_model_map", return_value={"vision-alias": "Vision-Model"}),
        patch.object(server.model_manager, "get_benchmark_context_limit", return_value=65536),
        patch.object(server.model_manager, "get_runtime_context_window", return_value=32768),
        patch.object(server.model_manager, "get_advertised_context_window", return_value=31744),
        patch.object(
            server.model_manager,
            "get_vision_capability",
            return_value={
                "configured": True,
                "status": "supported",
                "validated": True,
            },
        ),
    ):
        payload = await server.list_models(request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"), client_id="test-user")

    model_entry = payload["data"][0]
    assert model_entry["id"] == "vision-alias"
    assert model_entry["input_modalities"] == ["text", "image"]
    assert model_entry["configured_input_modalities"] == ["text", "image"]
    assert model_entry["vision"]["status"] == "supported"


@pytest.mark.asyncio
async def test_list_models_treats_configured_unverified_vision_as_image_capable():
    with (
        patch.object(server.model_manager, "get_current_model", return_value="Vision-Model"),
        patch.object(server.model_manager, "get_public_model_map", return_value={"vision-alias": "Vision-Model"}),
        patch.object(server.model_manager, "get_benchmark_context_limit", return_value=262144),
        patch.object(server.model_manager, "get_runtime_context_window", return_value=262144),
        patch.object(server.model_manager, "get_advertised_context_window", return_value=258048),
        patch.object(
            server.model_manager,
            "get_vision_capability",
            return_value={
                "configured": True,
                "status": "unverified",
                "validated": False,
            },
        ),
    ):
        payload = await server.list_models(request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"), client_id="test-user")

    model_entry = payload["data"][0]
    assert model_entry["input_modalities"] == ["text", "image"]
    assert model_entry["configured_input_modalities"] == ["text", "image"]
    assert model_entry["vision"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_get_model_metadata_resolves_public_alias():
    with (
        patch.object(
            server.model_manager,
            "get_public_model_map",
            return_value={"qwen3.6-35b-uncensored": "Qwen3.6-35B-A3B-HauhauCS-Aggressive"},
        ),
        patch.object(server.model_manager, "get_benchmark_context_limit", return_value=262144),
        patch.object(server.model_manager, "get_runtime_context_window", return_value=262144),
        patch.object(server.model_manager, "get_advertised_context_window", return_value=258048),
        patch.object(
            server.model_manager,
            "get_vision_capability",
            return_value={
                "configured": True,
                "status": "unverified",
                "validated": False,
            },
        ),
    ):
        payload = await server.get_model_metadata(
            "qwen3.6-35b-uncensored",
            _metadata_request(),
            client_id="test-user",
        )

    assert payload["id"] == "qwen3.6-35b-uncensored"
    assert payload["context"] == 262144
    assert payload["input_modalities"] == ["text", "image"]


@pytest.mark.asyncio
async def test_list_models_adds_context_aliases_to_every_served_model():
    request = SimpleNamespace(
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/v1/models"),
        method="GET",
    )

    with (
        patch.object(server.model_manager, "get_public_model_map", return_value={"local": "Local"}),
        patch.object(server.model_manager, "get_benchmark_context_limit", return_value=None),
        patch.object(server.model_manager, "get_runtime_context_window", return_value=32768),
        patch.object(server.model_manager, "get_advertised_context_window", return_value=31744),
        patch.object(
            server.model_manager,
            "get_vision_capability",
            return_value={"configured": False, "status": "text_only", "validated": False},
        ),
        patch.object(
            server.provider_registry,
            "get_enabled_providers",
            return_value=[CloudProvider("openrouter", "https://example.test/v1", "test-key")],
        ),
        patch.object(
            server.provider_registry,
            "build_model_metadata_entry",
            side_effect=lambda name: {"id": name, "object": "model", "served_by": "cloud"},
        ),
        patch.object(
            server.cloud_catalog,
            "get_models_for_provider",
            return_value={"moonshotai/kimi-k3": "moonshotai/kimi-k3"},
        ),
        patch.object(
            server.failover_registry,
            "_groups",
            {"kimi-k3": FailoverGroup(name="kimi-k3")},
        ),
        patch.object(server._model_discovery, "resolve_cloud_attempts", return_value=([], "kimi-k3")),
        patch.object(server._ctx_meta, "resolve_context_window", new=AsyncMock(return_value=1048576)),
    ):
        payload = await server.list_models(request=request, client_id="test-user")

    assert len(payload["data"]) == 3
    assert {model["id"] for model in payload["data"]} >= {
        "local",
        "openrouter/moonshotai/kimi-k3",
        "failover/kimi-k3",
    }
    for model in payload["data"]:
        assert model["context_length"] == 1048576
        assert model["max_input_tokens"] == 1048576
        assert model["meta"]["n_ctx"] == 1048576


@pytest.mark.asyncio
async def test_ollama_show_reports_context_for_cloud_model():
    class Request:
        async def json(self):
            return {"model": "openrouter/moonshotai/kimi-k3"}

    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server._ctx_meta, "resolve_context_window", new=AsyncMock(return_value=1048576)),
        patch.object(server._model_discovery, "resolve_cloud_attempts", return_value=([], None)),
    ):
        payload = await server.show_model_ollama(Request(), client_id="test-user")

    assert payload["model"] == "openrouter/moonshotai/kimi-k3"
    assert payload["model_info"]["general.context_length"] == 1048576
    assert payload["model_info"]["guardian.context_length"] == 1048576
    assert payload["parameters"] == "num_ctx 1048576"


@pytest.mark.asyncio
async def test_model_metadata_returns_cloud_entry_even_when_resolution_denied():
    """Cloud metadata is returned even when attempt resolution is denied
    (cloud_gateway_access=false): the discovery layer stays informative and
    the denial surfaces only on the actual forwarding path."""
    request = _metadata_request("unlinked-key")
    expected_error = server.HTTPException(
        status_code=403,
        detail={"error": "cloud_gateway_access_denied"},
    )

    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server.provider_registry, "build_model_metadata_entry",
                     return_value={"id": "openrouter/moonshotai/kimi-k3", "object": "model", "served_by": "cloud"}),
        patch.object(server._model_discovery, "resolve_cloud_attempts", side_effect=expected_error),
    ):
        result = await server.get_model_metadata(
            "openrouter/moonshotai/kimi-k3",
            request,
            client_id="test-user",
        )

    assert result["id"] == "openrouter/moonshotai/kimi-k3"
    assert result["served_by"] == "cloud"


@pytest.mark.asyncio
async def test_resolve_context_uses_loaded_backend_props_before_configured_value():
    server._ctx_meta._backend_context_cache.clear()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"default_generation_settings": {"n_ctx": 65536}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url):
            assert url == f"{server.LLAMA_SERVER_URL}/props"
            return FakeResponse()

    with (
        patch.object(server.provider_registry, "get_context_override", return_value=None),
        patch.object(server.model_manager, "get_current_model", new=AsyncMock(return_value="Local")),
        patch.object(server.model_manager, "get_runtime_context_window", return_value=32768),
        patch.object(server._ctx_meta.httpx, "AsyncClient", FakeAsyncClient),
    ):
        context_window = await server._resolve_context_window("local", "Local")

    assert context_window == 65536


@pytest.mark.asyncio
async def test_resolve_context_does_not_cache_props_after_backend_switches():
    server._ctx_meta._backend_context_cache.clear()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"default_generation_settings": {"n_ctx": 65536}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url):
            return FakeResponse()

    with (
        patch.object(server.provider_registry, "get_context_override", return_value=None),
        patch.object(
            server.model_manager,
            "get_current_model",
            new=AsyncMock(side_effect=["Local", "Other"]),
        ),
        patch.object(server.model_manager, "get_runtime_context_window", return_value=32768),
        patch.object(server._ctx_meta.httpx, "AsyncClient", FakeAsyncClient),
    ):
        context_window = await server._resolve_context_window("local", "Local")

    assert context_window == 32768
    assert "Local" not in server._ctx_meta._backend_context_cache


@pytest.mark.asyncio
async def test_resolve_context_uses_logged_default_when_no_source_has_a_value(caplog):
    server._ctx_meta._context_fallback_warnings.discard("unknown/cloud-model")

    with (
        patch.object(server.provider_registry, "get_context_override", return_value=None),
        patch.object(server.provider_registry, "get_cloud_context_window", new=AsyncMock(return_value=None)),
    ):
        context_window = await server._resolve_context_window("unknown/cloud-model")

    assert context_window == server.DEFAULT_CONTEXT_WINDOW
    assert "unknown/cloud-model" in caplog.text


@pytest.mark.asyncio
async def test_resolve_context_uses_safe_minimum_for_failover_candidates():
    group = FailoverGroup(
        name="kimi-k3",
        candidates=[
            FailoverCandidate("nvidia", "moonshotai/kimi-k3"),
            FailoverCandidate("openrouter", "moonshotai/kimi-k3"),
        ],
    )

    with (
        patch.object(server.provider_registry, "get_context_override", return_value=None),
        patch.object(server.failover_registry, "get_group", return_value=group),
        patch.object(server.cloud_catalog, "get_override", return_value=None),
        patch.object(
            server.provider_registry,
            "get_cloud_context_window",
            new=AsyncMock(side_effect=[1048576, None]),
        ) as context_mock,
    ):
        context_window = await server._resolve_context_window("failover/kimi-k3")

    assert context_window == server.DEFAULT_CONTEXT_WINDOW
    assert context_mock.await_args_list[0].args == ("nvidia/moonshotai/kimi-k3",)
    assert context_mock.await_args_list[1].args == ("openrouter/moonshotai/kimi-k3",)


@pytest.mark.asyncio
async def test_resolve_context_uses_safe_minimum_for_authorized_failover_attempts():
    attempts = [
        (
            CloudProvider("nvidia", "https://example.test/v1", "nvidia-key"),
            "moonshotai/kimi-k3",
        ),
        (
            CloudProvider("openrouter", "https://example.test/v1", "openrouter-key"),
            "moonshotai/kimi-k3",
        ),
    ]

    with (
        patch.object(server.provider_registry, "get_context_override", return_value=None),
        patch.object(server.cloud_catalog, "get_override", return_value=None),
        patch.object(
            server.provider_registry,
            "get_cloud_context_window",
            new=AsyncMock(side_effect=[1048576, 131072]),
        ),
    ):
        context_window = await server._resolve_context_window(
            "failover/kimi-k3",
            cloud_attempts=attempts,
        )

    assert context_window == 131072


@pytest.mark.asyncio
async def test_wait_for_proxy_listener_release_returns_true_once_listener_disappears():
    """Restart hardening should stop waiting once the old listener releases the port."""
    with patch.object(
        server,
        "_get_proxy_listener_info",
        side_effect=[{"pid": 1234}, {"pid": 1234}, None],
    ):
        assert await server._wait_for_proxy_listener_release(1234, timeout=0.5) is True


@pytest.mark.asyncio
async def test_stop_stale_guardian_listener_terminates_orphan():
    """A mismatched Guardian uvicorn listener should be terminated before bind."""
    repo_root = Path(server.__file__).resolve().parents[2]
    listener = {
        "pid": 4242,
        "process_name": "uvicorn",
        "command": f"{repo_root}/venv/bin/python3.14 {repo_root}/venv/bin/uvicorn app.proxy.server:app --host 0.0.0.0 --port 11434",
        "is_current_process": False,
    }

    with (
        patch.object(server, "_wait_for_proxy_listener_release", return_value=True),
        patch.object(server.os, "kill") as kill_mock,
    ):
        assert await server._stop_stale_guardian_listener(listener) is True
        kill_mock.assert_called_once_with(4242, server.signal.SIGTERM)


@pytest.mark.asyncio
async def test_admin_load_returns_400_for_runtime_override_validation_error():
    class DummyRequest:
        async def json(self):
            return {"runtime_overrides": {"context": 0}}

    async def raise_validation_error(**kwargs):
        raise ValueError("runtime_overrides.context must be > 0")

    with patch.object(server._admin_api, "_run_guardian_operation", side_effect=raise_validation_error):
        with pytest.raises(server.HTTPException) as exc:
            await server.admin_load(DummyRequest(), client_id="test-user")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_admin_load_passes_kv_type_runtime_override():
    class DummyRequest:
        async def json(self):
            return {"model": "llama3", "runtime_overrides": {"kv_type": "f16"}}

    captured_operation = None

    async def capture_operation(**kwargs):
        nonlocal captured_operation
        captured_operation = kwargs["operation"]

    with (
        patch.object(server.model_manager, "resolve_model", return_value="llama3"),
        patch.object(server.model_manager, "load", new_callable=AsyncMock) as load_mock,
        patch.object(server._admin_api, "_run_guardian_operation", side_effect=capture_operation),
    ):
        await server.admin_load(DummyRequest(), client_id="test-user")
        assert captured_operation is not None
        await captured_operation()

    load_mock.assert_awaited_once_with(
        "llama3",
        enable_vision=None,
        runtime_overrides={"kv_type": "f16"},
    )


# ── Cloud LLM router tests ─────────────────────────────────────────────

from app.proxy.providers import CloudProvider


@pytest.mark.asyncio
async def test_resolve_or_reject_accepts_cloud_model():
    """Cloud-provider models should pass model resolution without 404."""
    fake_provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["openai/gpt-4o"],
    )
    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server.model_manager, "models", {"local-model": {}}),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
    ):
        resolved = server._resolve_or_reject_inference_model("openai/gpt-4o", "local-model")
    assert resolved == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_resolve_or_reject_still_rejects_unknown_non_cloud_model():
    """Non-cloud, non-local models should still be rejected with 404."""
    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=False),
        patch.object(server.model_manager, "models", {"local-model": {}}),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not found")),
    ):
        with pytest.raises(server.HTTPException) as exc_info:
            server._resolve_or_reject_inference_model("ghost-model", "local-model")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_v1_post_forwards_cloud_model_to_provider_not_local():
    """A cloud model request should bypass the queue and hit the cloud provider URL."""

    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/chat/completions")
            self.method = "POST"

        async def body(self) -> bytes:
            return json.dumps(
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": False}
            ).encode("utf-8")

    fake_provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["openai/gpt-4o"],
    )

    captured = {}

    class _FakeResponse:
        def __init__(self):
            self.content = b'{"choices":[{"message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}'
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

        def json(self):
            return json.loads(self.content)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            return _FakeResponse()

    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server._cloud_routing, "_provider_registry", _routing_configured_registry()),
        patch.object(server.provider_registry, "get_provider_for_model", return_value=fake_provider),
        patch.object(server.ProviderRegistry, "build_forward_headers", return_value={"Authorization": "Bearer sk-or-test", "Content-Type": "application/json"}),
        patch.object(server.ProviderRegistry, "build_forward_url", return_value="https://openrouter.ai/api/v1/chat/completions"),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _FakeAsyncClient),
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server._cloud_forwarding, "_start_live_request_usage", lambda *a, **k: None),
        patch.object(server._cloud_forwarding, "_finish_live_request_usage", lambda *a, **k: None),
        patch.object(server._cloud_forwarding, "_record_usage_from_payload", lambda *a, **k: None),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        patch.object(server._gw_routing, "_begin_queued_request", side_effect=AssertionError("queue must be bypassed for cloud models")),
    ):
        response = await server.proxy_v1_post("chat/completions", _FakeRequest(), client_id="test-user")

    assert response.status_code == 200
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert "sk-or-test" in captured["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_v1_post_cloud_model_without_api_key_returns_503():
    """A cloud model whose provider has no API key should return 503."""

    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/chat/completions")
            self.method = "POST"

        async def body(self) -> bytes:
            return json.dumps(
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": False}
            ).encode("utf-8")

    fake_provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="",  # no key configured
        models=["openai/gpt-4o"],
    )

    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server.provider_registry, "get_provider_for_model", return_value=fake_provider),
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server._cloud_forwarding, "_start_live_request_usage", lambda *a, **k: None),
        patch.object(server._cloud_forwarding, "_finish_live_request_usage", lambda *a, **k: None),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        patch.object(server._gw_routing, "_begin_queued_request", side_effect=AssertionError("queue must be bypassed")),
    ):
        with pytest.raises(server.HTTPException) as exc_info:
            await server.proxy_v1_post("chat/completions", _FakeRequest(), client_id="test-user")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "provider_unavailable"
    assert exc_info.value.detail["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_list_models_includes_cloud_models():
    """The /v1/models endpoint should include provider-global catalog models."""
    fake_provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["openai/gpt-4o", "anthropic/claude-3.5-sonnet"],
    )

    with (
        patch.object(
            server.provider_registry,
            "get_enabled_providers",
            return_value=[fake_provider],
        ),
        patch.object(
            server.cloud_catalog,
            "get_models_for_provider",
            return_value={
                "openai/gpt-4o": "openai/gpt-4o",
                "anthropic/claude-3.5-sonnet": "anthropic/claude-3.5-sonnet",
            },
        ),
        patch.object(server.provider_registry, "build_model_metadata_entry") as build_mock,
        patch.object(server.model_manager, "get_public_model_map", return_value={"local-model": "local-model"}),
        patch.object(server._ctx_meta, "build_model_metadata_entry", return_value={"id": "local-model", "object": "model"}),
    ):
        build_mock.side_effect = lambda name: {"id": name, "object": "model", "owned_by": "openrouter", "served_by": "cloud"}
        result = await server.list_models(request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"), client_id="test-user")

    ids = [m["id"] for m in result["data"]]
    assert "local-model" in ids
    assert "openrouter/openai/gpt-4o" in ids
    assert "openrouter/anthropic/claude-3.5-sonnet" in ids


@pytest.mark.asyncio
async def test_list_models_annotates_reasoning_effort_metadata():
    """Cloud /v1/models entries carry reasoning effort metadata when the catalog advertises it."""
    fake_provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["deepseek/deepseek-v4-flash-0731", "openai/gpt-4o"],
    )
    reasoning = {
        "deepseek/deepseek-v4-flash-0731": {
            "supported_efforts": ["max", "high", "low"],
            "default_effort": "high",
        },
    }

    with (
        patch.object(
            server.provider_registry,
            "get_enabled_providers",
            return_value=[fake_provider],
        ),
        patch.object(
            server.cloud_catalog,
            "get_models_for_provider",
            return_value={
                "deepseek/deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
                "openai/gpt-4o": "openai/gpt-4o",
            },
        ),
        patch.object(server.cloud_catalog, "get_model_reasoning") as reasoning_mock,
        patch.object(server.provider_registry, "build_model_metadata_entry") as build_mock,
        patch.object(server.model_manager, "get_public_model_map", return_value={}),
    ):
        reasoning_mock.side_effect = lambda p, n: reasoning.get(n, {})
        build_mock.side_effect = lambda name: {"id": name, "object": "model", "owned_by": "openrouter", "served_by": "cloud"}
        result = await server.list_models(
            request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"),
            client_id="test-user",
        )

    by_id = {m["id"]: m for m in result["data"]}
    annotated = by_id.get("openrouter/deepseek/deepseek-v4-flash-0731")
    assert annotated is not None
    assert annotated["reasoning"] == {
        "supported_efforts": ["max", "high", "low"],
        "default_effort": "high",
    }
    # Models without advertised reasoning stay unannotated.
    assert "reasoning" not in by_id["openrouter/openai/gpt-4o"]


@pytest.mark.asyncio
async def test_get_model_metadata_annotates_reasoning_for_cloud_model():
    """GET /v1/models/{id} for a cloud model includes reasoning metadata."""
    reasoning = {"supported_efforts": ["max", "high", "low"], "default_effort": "high"}
    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server.provider_registry, "build_model_metadata_entry") as build_mock,
        patch.object(server._model_discovery, "_cloud_catalog") as cat_mock,
    ):
        build_mock.return_value = {
            "id": "openrouter/deepseek/deepseek-v4-flash-0731",
            "object": "model",
            "owned_by": "openrouter",
            "served_by": "cloud",
            "provider": "openrouter",
        }
        cat_mock.get_model_reasoning.return_value = reasoning
        result = await server.get_model_metadata(
            "openrouter/deepseek/deepseek-v4-flash-0731",
            _metadata_request(),
            client_id="test-user",
        )

    assert result["reasoning"] == reasoning


@pytest.mark.asyncio
async def test_get_model_metadata_returns_cloud_model():
    """GET /v1/models/{model_id} should return cloud metadata for cloud models."""
    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server.provider_registry, "build_model_metadata_entry") as build_mock,
    ):
        build_mock.return_value = {"id": "openai/gpt-4o", "object": "model", "owned_by": "openrouter", "served_by": "cloud"}
        result = await server.get_model_metadata(
            "openai/gpt-4o",
            _metadata_request(),
            client_id="test-user",
        )

    assert result["id"] == "openai/gpt-4o"
    assert result["owned_by"] == "openrouter"
    assert result["served_by"] == "cloud"


@pytest.mark.asyncio
async def test_list_models_includes_failover_groups():
    """/v1/models must surface failover groups as failover/{name} entries."""
    fake_group = SimpleNamespace(name="glm-5.2")

    with (
        patch.object(server._model_discovery, "_model_manager") as mm_mock,
        patch.object(server._model_discovery, "_provider_registry") as pr_mock,
        patch.object(server._model_discovery, "_cloud_catalog") as cat_mock,
        patch.object(server._model_discovery, "_failover_registry") as fr_mock,
        patch.object(server._ctx_meta, "build_model_metadata_entry", return_value={"id": "local", "object": "model"}),
        patch.object(server._model_discovery, "resolve_cloud_attempts", return_value=([], "glm-5.2")),
    ):
        mm_mock.get_public_model_map.return_value = {}
        pr_mock.get_enabled_providers.return_value = []
        cat_mock.get_models_for_provider.return_value = {}
        fr_mock._groups = {"glm-5.2": fake_group}

        result = await server.list_models(
            request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"),
            client_id="test-user",
        )

    ids = [m["id"] for m in result["data"]]
    assert "failover/glm-5.2" in ids
    failover_entry = next(m for m in result["data"] if m["id"] == "failover/glm-5.2")
    assert failover_entry["served_by"] == "failover"
    assert failover_entry["provider"] == "failover"
    assert failover_entry["owned_by"] == "failover"
    assert failover_entry["failover_group"] == "glm-5.2"


@pytest.mark.asyncio
async def test_get_model_metadata_returns_failover_group():
    """GET /v1/models/failover/{name} must resolve the failover entry."""
    fake_group = SimpleNamespace(name="glm-5.2")

    with (
        patch.object(server._model_discovery, "_failover_registry") as fr_mock,
        patch.object(server._model_discovery, "resolve_cloud_attempts", return_value=([], "glm-5.2")),
    ):
        fr_mock.get_group.return_value = fake_group
        result = await server.get_model_metadata(
            "failover/glm-5.2",
            _metadata_request(),
            client_id="test-user",
        )

    assert result["id"] == "failover/glm-5.2"
    assert result["served_by"] == "failover"
    assert result["failover_group"] == "glm-5.2"
    fr_mock.get_group.assert_called_with("glm-5.2")


@pytest.mark.asyncio
async def test_get_model_metadata_returns_404_for_unknown_failover_group():
    """GET /v1/models/failover/{unknown} must 404, not 500."""
    with patch.object(server, "failover_registry") as fr_mock:
        fr_mock.get_group.return_value = None
        with pytest.raises(Exception) as excinfo:
            await server.get_model_metadata(
                "failover/does-not-exist",
                _metadata_request(),
                client_id="test-user",
            )
    assert excinfo.value.status_code == 404


# ── _prepare_cloud_candidate_request: user field injection ────────────


def test_provider_base_url_includes_poolside():
    assert server._provider_base_url("poolside") == "https://inference.poolside.ai/v1"


def test_prepare_cloud_candidate_injects_user_for_openrouter():
    """The client_user_id must be injected as ``user`` in the body for OpenRouter."""
    provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["z-ai/glm-5.2"],
    )
    base_body = {"model": "openrouter/z-ai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]}
    _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
        provider, "z-ai/glm-5.2", "chat/completions", base_body, client_user_id="fp_abc123def456",
    )
    assert json_body["user"] == "fp_abc123def456"
    assert json_body["model"] == "z-ai/glm-5.2"


def test_prepare_cloud_candidate_no_user_for_nvidia():
    """Non-OpenRouter providers must NOT get a ``user`` field injected."""
    provider = CloudProvider(
        name="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="nvapi-test",
        models=["minimaxai/minimax-m3"],
    )
    base_body = {"model": "nvidia/minimaxai/minimax-m3", "messages": [{"role": "user", "content": "hi"}]}
    _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
        provider, "minimaxai/minimax-m3", "chat/completions", base_body, client_user_id="fp_abc123def456",
    )
    assert "user" not in json_body


def test_prepare_cloud_candidate_respects_existing_user():
    """An existing ``user`` field from the client must not be overridden."""
    provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["z-ai/glm-5.2"],
    )
    base_body = {
        "model": "openrouter/z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "user": "client_set_user",
    }
    _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
        provider, "z-ai/glm-5.2", "chat/completions", base_body, client_user_id="fp_abc123def456",
    )
    assert json_body["user"] == "client_set_user"


def test_prepare_cloud_candidate_no_user_without_client_id():
    """When no client_user_id is provided, no ``user`` field is injected."""
    provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["z-ai/glm-5.2"],
    )
    base_body = {"model": "openrouter/z-ai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]}
    _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
        provider, "z-ai/glm-5.2", "chat/completions", base_body,
    )
    assert "user" not in json_body


def test_prepare_cloud_candidate_strips_context_metadata_from_defaults():
    """Guardian metadata keys (context_window, …) must never be forwarded to a
    cloud provider as request params. Cloud APIs reject unknown parameters —
    NVIDIA returns HTTP 400 on ``context_window``. Real OpenAI params such as
    ``max_tokens`` must still be applied."""
    provider = CloudProvider(
        name="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="nvapi-test",
        models=["moonshotai/kimi-k3"],
    )
    base_body = {"model": "nvidia/moonshotai/kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
    override = {
        "context_window": 1048576,
        "context_length": 1048576,
        "max_input_tokens": 1048576,
        "max_tokens": 4096,
        "temperature": 0.7,
    }
    with patch.object(server.cloud_catalog, "get_override", return_value=override):
        _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
            provider, "moonshotai/kimi-k3", "chat/completions", base_body,
        )
    # Metadata-only keys are stripped…
    assert "context_window" not in json_body
    assert "context_length" not in json_body
    assert "max_input_tokens" not in json_body
    # …while real OpenAI request parameters are applied.
    assert json_body["max_tokens"] == 4096
    assert json_body["temperature"] == 0.7



# ── OpenAI reasoning-model parameter adaptation ───────────────────────


def test_provider_base_url_includes_openai():
    """The OpenAI base URL must be registered for openai/ cloud routes."""
    assert server._provider_base_url("openai") == "https://api.openai.com/v1"


def test_provider_base_url_includes_google():
    """Google AI Studio must be registered for google/ cloud routes."""
    assert (
        server._provider_base_url("google")
        == "https://generativelanguage.googleapis.com/v1beta/openai"
    )


def test_parse_google_model_catalog_normalizes_unique_model_ids():
    models = server._parse_google_model_catalog(
        {
            "data": [
                {"id": "gemini-2.5-pro"},
                {"id": "gemini-2.5-flash"},
                {"id": "models/gemini-2.5-flash"},
                {"id": "gemini-2.5-pro"},
                {"id": " "},
                {"id": "models/"},
                {"name": "models/not-a-route"},
            ]
        }
    )

    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]


def test_normalize_google_model_id_strips_resource_prefix():
    assert server._normalize_google_model_id("models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert server._normalize_google_model_id("  gemini-2.5-pro  ") == "gemini-2.5-pro"
    assert server._normalize_google_model_id("models/") == ""


def test_sanitize_proxied_response_headers_strips_conflicting_framing():
    headers = server._sanitize_proxied_response_headers(
        {
            "Content-Type": "application/json",
            "Content-Length": "123",
            "Transfer-Encoding": "chunked",
            "Content-Encoding": "gzip",
            "X-Request-Id": "abc",
            "Set-Cookie": "NID=secret",
        }
    )

    assert headers == {
        "Content-Type": "application/json",
        "X-Request-Id": "abc",
        "Set-Cookie": "NID=secret",
    }


def test_parse_google_model_catalog_rejects_invalid_payload():
    with pytest.raises(ValueError, match="model data"):
        server._parse_google_model_catalog({"models": []})


@pytest.mark.asyncio
async def test_discover_google_models_uses_bearer_auth_without_exposing_key():
    response = Mock()
    response.json.return_value = {"data": [{"id": "gemini-2.5-flash"}]}
    client = AsyncMock()
    client.get.return_value = response
    http_client = AsyncMock()
    http_client.__aenter__.return_value = client
    http_client.__aexit__.return_value = False

    with patch.object(server._cloud_inf.httpx, "AsyncClient", return_value=http_client):
        models = await server._cloud_inf.discover_google_models("google-test-key")

    assert models == ["gemini-2.5-flash"]
    response.raise_for_status.assert_called_once_with()
    client.get.assert_awaited_once_with(
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        headers={"Authorization": "Bearer google-test-key"},
    )


@pytest.mark.asyncio
async def test_discover_google_models_converts_upstream_failure_to_generic_502():
    response = Mock()
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "unauthorized",
        request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        response=httpx.Response(401),
    )
    client = AsyncMock()
    client.get.return_value = response
    http_client = AsyncMock()
    http_client.__aenter__.return_value = client
    http_client.__aexit__.return_value = False

    with (
        patch.object(server._cloud_inf.httpx, "AsyncClient", return_value=http_client),
        pytest.raises(server.HTTPException) as exc_info,
    ):
        await server._cloud_inf.discover_google_models("google-test-key")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "error": "google_model_discovery_failed",
        "message": "Google model catalog could not be retrieved.",
    }


@pytest.mark.asyncio
async def test_list_cloud_catalog_admin_reports_provider_state():
    fake_provider = CloudProvider("openrouter", "https://openrouter.ai/api/v1", "sk-test")
    with (
        patch.object(server._admin_api, "_provider_registry") as pr_mock,
        patch.object(server._admin_api, "_cloud_catalog") as cat_mock,
    ):
        pr_mock.get_enabled_providers.return_value = [fake_provider]
        cat_mock.get_models_for_provider.return_value = {"moonshotai/kimi-k3": "moonshotai/kimi-k3"}
        cat_mock._catalogs = {"openrouter": {"fetched_at": 100.0, "models": {}}}

        result = await server.list_cloud_catalog("admin")

    assert result["catalog"][0]["name"] == "openrouter"
    assert result["catalog"][0]["configured"] is True
    assert result["catalog"][0]["model_count"] == 1
    assert result["catalog"][0]["addresses"] == ["openrouter/moonshotai/kimi-k3"]
    assert result["catalog"][0]["last_fetch"] == 100.0


@pytest.mark.asyncio
async def test_refresh_cloud_catalog_admin_force_refresh():
    with (
        patch.object(server._admin_api, "_provider_registry") as pr_mock,
        patch.object(server._admin_api, "_cloud_catalog") as cat_mock,
    ):
        cat_mock.refresh_all = AsyncMock()
        cat_mock.get_models_for_provider.return_value = {}
        cat_mock._catalogs = {}
        pr_mock.get_enabled_providers.return_value = []

        result = await server.refresh_cloud_catalog("admin")

    assert result["status"] == "refreshed"
    cat_mock.refresh_all.assert_awaited_once()


def test_adapt_openai_reasoning_converts_max_tokens_for_o3():
    """o3-mini must get max_tokens renamed to max_completion_tokens."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["o3-mini"],
    )
    body = {"model": "o3-mini", "messages": [], "max_tokens": 100, "temperature": 0.5}
    adapted = server._adapt_openai_reasoning_params(provider, "o3-mini", body)
    assert "max_tokens" not in adapted
    assert adapted.get("max_completion_tokens") == 100


def test_adapt_openai_reasoning_strips_temperature_for_o3():
    """o-series models must have temperature stripped entirely."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["o3-mini"],
    )
    body = {"model": "o3-mini", "messages": [], "temperature": 0.5}
    adapted = server._adapt_openai_reasoning_params(provider, "o3-mini", body)
    assert "temperature" not in adapted


def test_adapt_openai_reasoning_converts_max_tokens_for_gpt5():
    """gpt-5* must also get max_tokens renamed to max_completion_tokens."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["gpt-5-mini"],
    )
    body = {"model": "gpt-5-mini", "messages": [], "max_tokens": 200}
    adapted = server._adapt_openai_reasoning_params(provider, "gpt-5-mini", body)
    assert "max_tokens" not in adapted
    assert adapted.get("max_completion_tokens") == 200


def test_adapt_openai_reasoning_fixes_temperature_for_gpt5():
    """gpt-5* must have temperature forced to 1 when set to anything else."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["gpt-5-mini"],
    )
    body = {"model": "gpt-5-mini", "messages": [], "temperature": 0}
    adapted = server._adapt_openai_reasoning_params(provider, "gpt-5-mini", body)
    assert adapted.get("temperature") == 1


def test_adapt_openai_reasoning_does_not_touch_temperature_1_for_gpt5():
    """temperature=1 is the only accepted value for gpt-5*; leave it alone."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["gpt-5-mini"],
    )
    body = {"model": "gpt-5-mini", "messages": [], "temperature": 1}
    adapted = server._adapt_openai_reasoning_params(provider, "gpt-5-mini", body)
    assert adapted.get("temperature") == 1


def test_adapt_openai_reasoning_passes_through_non_reasoning():
    """gpt-4o is NOT a reasoning model; params must be untouched."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["gpt-4o"],
    )
    body = {"model": "gpt-4o", "messages": [], "max_tokens": 100, "temperature": 0.7}
    adapted = server._adapt_openai_reasoning_params(provider, "gpt-4o", body)
    assert adapted is body or adapted == body  # may be same or equal
    assert adapted.get("max_tokens") == 100
    assert adapted.get("temperature") == 0.7
    assert "max_completion_tokens" not in adapted


def test_adapt_openai_reasoning_skips_non_openai_provider():
    """OpenRouter has its own param handling; adaptation must NOT apply."""
    provider = CloudProvider(
        name="openrouter", base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test", models=["openai/gpt-5"],
    )
    body = {"model": "openai/gpt-5", "messages": [], "max_tokens": 100, "temperature": 0.5}
    adapted = server._adapt_openai_reasoning_params(provider, "openai/gpt-5", body)
    assert adapted.get("max_tokens") == 100
    assert adapted.get("temperature") == 0.5
    assert "max_completion_tokens" not in adapted


def test_adapt_openai_reasoning_keeps_explicit_max_completion_tokens():
    """If client already set max_completion_tokens, don't override — only drop max_tokens."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["o3-mini"],
    )
    body = {"model": "o3-mini", "messages": [], "max_tokens": 50, "max_completion_tokens": 200}
    adapted = server._adapt_openai_reasoning_params(provider, "o3-mini", body)
    assert "max_tokens" not in adapted
    assert adapted.get("max_completion_tokens") == 200


def test_prepare_cloud_adapts_openai_reasoning_end_to_end():
    """_prepare_cloud_candidate_request must apply adaptation for direct OpenAI o3-mini."""
    provider = CloudProvider(
        name="openai", base_url="https://api.openai.com/v1",
        api_key="sk-test", models=["o3-mini"],
    )
    base_body = {"model": "openai/openai/o3-mini", "messages": [], "max_tokens": 64, "temperature": 0}
    _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
        provider, "o3-mini", "chat/completions", base_body,
    )
    assert "max_tokens" not in json_body
    assert json_body.get("max_completion_tokens") == 64
    assert "temperature" not in json_body


# ── Capture: cloud response content extraction ──────────────────────────

class TestExtractCloudResponseContent:
    """Tests for _extract_cloud_response_content helper."""

    def test_extracts_openai_text_content(self):
        payload = {"choices": [{"message": {"content": "Hello world"}}]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content == "Hello world"
        assert tool_calls is None

    def test_extracts_openai_tool_calls(self):
        tcs = [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "SF"}'}}]
        payload = {"choices": [{"message": {"content": None, "tool_calls": tcs}}]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content is None
        assert tool_calls == tcs

    def test_extracts_openai_reasoning_fallback(self):
        payload = {"choices": [{"message": {"content": None, "reasoning_content": "Thinking..."}}]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content == "Thinking..."

    def test_extracts_reasoning_content_separately(self):
        payload = {"choices": [{"message": {"content": "Answer", "reasoning_content": "Let me think"}}]}
        reasoning = server._cloud_routing.extract_cloud_reasoning_content(payload)
        assert reasoning == "Let me think"

    def test_extracts_openrouter_reasoning_field(self):
        payload = {"choices": [{"message": {"content": "Answer", "reasoning": "1. think"}}]}
        reasoning = server._cloud_routing.extract_cloud_reasoning_content(payload)
        assert reasoning == "1. think"

    def test_reasoning_extraction_returns_none_when_absent(self):
        assert server._cloud_routing.extract_cloud_reasoning_content(None) is None
        assert server._cloud_routing.extract_cloud_reasoning_content({}) is None
        assert server._cloud_routing.extract_cloud_reasoning_content(
            {"choices": [{"message": {"content": "Hi"}}]}
        ) is None

    def test_extracts_openai_content_block_list(self):
        payload = {"choices": [{"message": {"content": [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}]}}]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content == "Part 1\nPart 2"

    def test_extracts_anthropic_text_block(self):
        payload = {"content": [{"type": "text", "text": "Hello from Claude"}]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content == "Hello from Claude"
        assert tool_calls is None

    def test_extracts_anthropic_tool_use_block(self):
        payload = {"content": [{"type": "text", "text": "Let me check"}, {"type": "tool_use", "id": "tool_1", "name": "search", "input": {"q": "test"}}]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content == "Let me check"
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "tool_1"
        assert tool_calls[0]["function"]["name"] == "search"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"q": "test"}

    def test_returns_none_for_empty_payload(self):
        content, tool_calls = server._extract_cloud_response_content(None)
        assert content is None
        assert tool_calls is None

    def test_returns_none_for_missing_choices(self):
        payload = {"model": "gpt-4o"}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content is None
        assert tool_calls is None

    def test_returns_none_for_empty_choices(self):
        payload = {"choices": []}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content is None
        assert tool_calls is None

    def test_handles_mixed_text_and_tool_use_anthropic(self):
        payload = {"content": [
            {"type": "text", "text": "I'll search for that"},
            {"type": "tool_use", "id": "tu1", "name": "lookup", "input": {"id": 42}},
            {"type": "text", "text": "and show results"},
        ]}
        content, tool_calls = server._extract_cloud_response_content(payload)
        assert content == "I'll search for that\nand show results"
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "lookup"


@pytest.mark.asyncio
async def test_gw_routing_init_binds_camelcase_globals():
    """Regression: init() must bind _GuardianRequestCancelled and _get_model_timeout
    as module globals — a local-only assignment left them None, which turns the
    `except _GuardianRequestCancelled` clauses in route_v1_post into a TypeError
    at runtime whenever a request cancellation actually occurs.
    """
    assert server._gw_routing._GuardianRequestCancelled is server._GuardianRequestCancelled
    assert server._gw_routing._GuardianRequestCancelled is not None
    assert callable(server._gw_routing._get_model_timeout)


@pytest.mark.asyncio
async def test_resolve_auto_reload_model_delegates_to_model_manager():
    """Regression: the function was an empty stub after the queue_helpers
    extraction, silently returning None instead of the reload target."""
    with patch.object(server._local_models, "_model_manager") as mm:
        mm.resolve_reload_target.return_value = "qwen3-30b"
        result = server._resolve_auto_reload_model("llama3.2-3b")
        assert result == "qwen3-30b"
        mm.resolve_reload_target.assert_called_once_with("llama3.2-3b")


@pytest.mark.asyncio
async def test_reload_backend_after_connect_error_skips_when_healthy():
    """Backend-reload recovery must not reload when the backend is healthy again."""
    with (
        patch.object(server._local_models, "_model_manager") as mm,
        patch.object(server._local_models, "_model_switch_lock", asyncio.Lock()),
        patch.object(server._local_models, "_run_guardian_operation", new_callable=AsyncMock) as run_op,
    ):
        mm.get_current_model = AsyncMock(return_value="llama3.2-3b")
        mm.backend_health_ok = AsyncMock(return_value=True)
        mm.load = AsyncMock()
        await server._reload_backend_after_connect_error("chat/completions", RuntimeError("boom"))
        run_op.assert_not_awaited()
        mm.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_backend_after_connect_error_reloads_when_down():
    """Backend-reload recovery must load the resolved target when the backend is down."""
    with (
        patch.object(server._local_models, "_model_manager") as mm,
        patch.object(server._local_models, "_model_switch_lock", asyncio.Lock()),
        patch.object(server._local_models, "_reset_startup_check_status", return_value=42),
        patch.object(server._local_models, "_run_guardian_operation", new_callable=AsyncMock) as run_op,
    ):
        mm.get_current_model = AsyncMock(return_value="llama3.2-3b")
        mm.backend_health_ok = AsyncMock(return_value=False)
        mm.resolve_reload_target.return_value = "qwen3-30b"
        mm.load = AsyncMock()
        await server._reload_backend_after_connect_error("chat/completions", RuntimeError("boom"))
        run_op.assert_awaited_once()
        args, kwargs = run_op.await_args
        assert kwargs["phase"] == "backend_reload"
        assert kwargs["source"] == "proxy"
        assert kwargs["target_model"] == "qwen3-30b"
        assert mm.is_unloaded is True


def test_admin_api_handler_signatures_match_wrappers():
    """Regression: the admin_api extraction gave every handler the same
    (request, client_id) signature — wrappers calling with the original
    argument lists crashed at runtime (e.g. get_server_status 500).
    """
    import inspect
    from app.gateway import admin_api as mod
    # (handler, wrapper call arity, expected params)
    expectations = {
        "list_api_keys": ["client_id"],
        "create_api_key": ["request", "client_id"],
        "get_cloud_ratelimit_stats": ["request", "client_id"],
        "list_cloud_providers": ["client_id"],
        "list_cloud_models": ["request", "client_id"],
        "list_cloud_catalog": ["client_id"],
        "refresh_cloud_catalog": ["client_id"],
        "get_crash_history": ["client_id"],
        "get_server_status": ["client_id"],
        "get_capture_status": ["client_id"],
        "rotate_capture_file": ["client_id"],
        "get_scaler_config": ["client_id"],
        "update_scaler_config": ["request", "client_id"],
        "reset_scaler_config": ["client_id"],
        "scaler_recommend": ["request", "client_id"],
        "queue_status": ["request", "client_id"],
        "queue_request_status": ["request_id", "request", "client_id"],
        "cancel_queue_request": ["request_id", "request", "client_id"],
    }
    for handler, params in expectations.items():
        fn = getattr(mod, handler)
        actual = list(inspect.signature(fn).parameters)
        assert actual == params, f"{handler}: expected {params}, got {actual}"


def test_all_wrapper_calls_match_module_signatures():
    """Generic Phase 5 regression: every `_module.func(...)` call in server.py
    must reference an existing function and pass exactly the parameters the
    module function accepts. The admin_api extraction broke 11 of 25 handlers
    by giving them uniform signatures; this check covers every delegation.
    """
    import ast
    import importlib
    import inspect

    tree = ast.parse(inspect.getsource(server))
    alias_map = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.asname and a.asname.startswith("_"):
                    alias_map[a.asname] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and a.asname.startswith("_"):
                    alias_map[a.asname] = a.name

    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id in alias_map):
            continue
        fn_name = node.func.attr
        if fn_name == "init":
            continue
        mod = importlib.import_module(alias_map[node.func.value.id])
        fn = getattr(mod, fn_name, None)
        if fn is None:
            problems.append(f"{node.lineno}: {alias_map[node.func.value.id]}.{fn_name} does not exist")
            continue
        sig = inspect.signature(fn)
        params = list(sig.parameters)
        var_pos = any(v.kind == inspect.Parameter.VAR_POSITIONAL for v in sig.parameters.values())
        # positional args fill params in order (excluding already-keyword-filled ones)
        pos = len(node.args)
        kw = {k.arg for k in node.keywords if k.arg}
        if not var_pos and pos > len(params):
            problems.append(f"{node.lineno}: {fn_name} gets {pos} positional but only has {len(params)} params")
            continue
        # required params that are neither positionally nor by keyword provided
        filled = set(params[:pos]) | kw
        for p, v in sig.parameters.items():
            if v.default is inspect.Parameter.empty and p not in filled and v.kind not in (
                inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
            ):
                problems.append(f"{node.lineno}: {fn_name} missing required param '{p}'")
        for k in kw:
            if k not in sig.parameters and not any(
                v.kind == inspect.Parameter.VAR_KEYWORD for v in sig.parameters.values()
            ):
                problems.append(f"{node.lineno}: {fn_name} unexpected kwarg '{k}'")

    assert not problems, "\n".join(problems)
