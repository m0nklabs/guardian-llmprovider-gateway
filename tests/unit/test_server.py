"""Unit tests for Guardian server startup behavior."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.proxy import server


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
        patch.object(server, "PID_FILE", str(pid_file)),
        patch.object(server.model_manager, "startup_check", fake_startup_check),
        patch.object(server, "_idle_unload_watcher", fake_idle_unload_watcher),
        patch.object(server, "_get_proxy_listener_info", return_value=None),
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
        patch.object(server, "PID_FILE", str(pid_file)),
        patch.object(server.model_manager, "startup_check", fake_startup_check),
        patch.object(server, "_idle_unload_watcher", fake_idle_unload_watcher),
        patch.object(server, "_get_proxy_listener_info", return_value=None),
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
    tracker = server.ApiUsageTracker(state_file=tmp_path / "usage_state.json")
    request = SimpleNamespace(
        state=SimpleNamespace(),
        scope={},
        client=SimpleNamespace(host="10.0.0.8", port=5555),
        headers={"user-agent": "guardian-missing-key/1.0"},
        url=SimpleNamespace(path="/v1/models"),
        method="GET",
    )

    with patch.object(server, "state", SimpleNamespace(api_usage=tracker)):
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

    with patch.object(server, "inference_queue", queue):
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

    with patch.object(server, "inference_queue", queue):
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

    with patch.object(server, "inference_queue", queue):
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

    with patch.object(server, "inference_queue", queue):
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

    with patch.object(server, "inference_queue", queue):
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

    with patch.object(server, "inference_queue", queue):
        snapshot = await server.queue_request_status(request_id, request=request, client_id="observer")

    assert snapshot["request_id"] == request_id
    assert snapshot["status"] == "queued"
    assert snapshot["position"] == 1


@pytest.mark.asyncio
async def test_cancel_queue_request_endpoint_marks_running_request():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = await queue.acquire("observer", "model-x", owner_id="key:observer")
    request = SimpleNamespace(state=SimpleNamespace(auth_context={"key_fingerprint": "observer"}))

    with patch.object(server, "inference_queue", queue):
        snapshot = await server.cancel_queue_request(request_id, request=request, client_id="observer")

    assert snapshot["request_id"] == request_id
    assert snapshot["status"] == "cancelling"
    assert snapshot["cancel_reason"] == "client_requested_cancel"


@pytest.mark.asyncio
async def test_queue_request_status_endpoint_is_isolated_per_api_key():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = queue.submit("observer", "model-x", owner_id="key:owner-a")
    foreign_request = SimpleNamespace(state=SimpleNamespace(auth_context={"key_fingerprint": "owner-b"}))

    with patch.object(server, "inference_queue", queue):
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
        patch.object(server, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server, "_begin_queued_request", side_effect=AssertionError("queue should not be used")),
        patch.object(server.httpx, "AsyncClient", _FakeAsyncClient),
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
        patch.object(server, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server, "_begin_queued_request", side_effect=AssertionError("queue should not be used")),
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
        patch.object(server, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server, "_resolve_or_reject_inference_model", return_value="Qwen-Agent"),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="Qwen-Agent")),
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}, "auto": {}}),
        patch.object(server.httpx, "AsyncClient", _FakeAsyncClient),
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
        patch.object(server, "_set_request_usage_metadata", lambda *args, **kwargs: None),
        patch.object(server, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server, "_resolve_or_reject_inference_model", return_value="Qwen-Agent"),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="Qwen-Agent")),
        patch.object(server.model_manager, "models", {"Qwen-Agent": {}, "auto": {}}),
        patch.object(server.httpx, "AsyncClient", _FakeAsyncClient),
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

    with patch.object(server, "inference_queue", queue):
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
            client_id="hermes-ai-node",
            model_name="Qwen3.6-35B-A3B-HauhauCS-Aggressive",
        )

        with pytest.raises(server.httpx.ReadTimeout, match="request_id=req-123"):
            await stream_iter.__anext__()

    warning_text = warning_mock.call_args[0][0]
    assert "route=/v1/chat/completions" in warning_text
    assert "client=hermes-ai-node" in warning_text
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
        payload = await server.get_model_metadata("qwen3.6-35b-uncensored", client_id="test-user")

    assert payload["id"] == "qwen3.6-35b-uncensored"
    assert payload["context"] == 262144
    assert payload["input_modalities"] == ["text", "image"]


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

    with patch.object(server, "_run_guardian_operation", side_effect=raise_validation_error):
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
        patch.object(server, "_run_guardian_operation", side_effect=capture_operation),
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
                {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": False}
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
        patch.object(server.provider_registry, "get_provider_for_model", return_value=fake_provider),
        patch.object(server.ProviderRegistry, "build_forward_headers", return_value={"Authorization": "Bearer sk-or-test", "Content-Type": "application/json"}),
        patch.object(server.ProviderRegistry, "build_forward_url", return_value="https://openrouter.ai/api/v1/chat/completions"),
        patch.object(server.httpx, "AsyncClient", _FakeAsyncClient),
        patch.object(server, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server, "_start_live_request_usage", lambda *a, **k: None),
        patch.object(server, "_finish_live_request_usage", lambda *a, **k: None),
        patch.object(server, "_record_usage_from_payload", lambda *a, **k: None),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        patch.object(server, "_begin_queued_request", side_effect=AssertionError("queue must be bypassed for cloud models")),
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
                {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": False}
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
        patch.object(server, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server, "_start_live_request_usage", lambda *a, **k: None),
        patch.object(server, "_finish_live_request_usage", lambda *a, **k: None),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
        patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        patch.object(server, "_begin_queued_request", side_effect=AssertionError("queue must be bypassed")),
    ):
        with pytest.raises(server.HTTPException) as exc_info:
            await server.proxy_v1_post("chat/completions", _FakeRequest(), client_id="test-user")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "provider_unavailable"
    assert exc_info.value.detail["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_list_models_includes_cloud_models():
    """The /v1/models endpoint should include cloud-provider models."""
    fake_provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["openai/gpt-4o", "anthropic/claude-3.5-sonnet"],
    )

    with (
        patch.object(server.provider_registry, "get_all_cloud_models", return_value=["openai/gpt-4o", "anthropic/claude-3.5-sonnet"]),
        patch.object(server.provider_registry, "build_model_metadata_entry") as build_mock,
        patch.object(server.model_manager, "get_public_model_map", return_value={"local-model": "local-model"}),
        patch.object(server, "_build_model_metadata_entry", return_value={"id": "local-model", "object": "model"}),
    ):
        build_mock.side_effect = lambda name: {"id": name, "object": "model", "owned_by": "openrouter", "served_by": "cloud"}
        result = await server.list_models(request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"), client_id="test-user")

    ids = [m["id"] for m in result["data"]]
    assert "local-model" in ids
    assert "openai/gpt-4o" in ids
    assert "anthropic/claude-3.5-sonnet" in ids


@pytest.mark.asyncio
async def test_get_model_metadata_returns_cloud_model():
    """GET /v1/models/{model_id} should return cloud metadata for cloud models."""
    with (
        patch.object(server.provider_registry, "is_cloud_model", return_value=True),
        patch.object(server.provider_registry, "build_model_metadata_entry") as build_mock,
    ):
        build_mock.return_value = {"id": "openai/gpt-4o", "object": "model", "owned_by": "openrouter", "served_by": "cloud"}
        result = await server.get_model_metadata("openai/gpt-4o", client_id="test-user")

    assert result["id"] == "openai/gpt-4o"
    assert result["owned_by"] == "openrouter"
    assert result["served_by"] == "cloud"


@pytest.mark.asyncio
async def test_list_models_includes_failover_groups():
    """/v1/models must surface failover groups as guardian/failover/{name} entries."""
    fake_group = SimpleNamespace(name="glm-5.2")

    with (
        patch.object(server, "model_manager") as mm_mock,
        patch.object(server, "provider_registry") as pr_mock,
        patch.object(server, "cloud_cred_store") as cc_mock,
        patch.object(server, "failover_registry") as fr_mock,
        patch.object(server, "_build_model_metadata_entry", return_value={"id": "local", "object": "model"}),
    ):
        mm_mock.get_public_model_map.return_value = {}
        pr_mock.get_all_cloud_models.return_value = []
        cc_mock.get_linked_models_for_key.return_value = []
        fr_mock._groups = {"glm-5.2": fake_group}

        result = await server.list_models(
            request=SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/v1/models"), method="GET"),
            client_id="test-user",
        )

    ids = [m["id"] for m in result["data"]]
    assert "guardian/failover/glm-5.2" in ids
    failover_entry = next(m for m in result["data"] if m["id"] == "guardian/failover/glm-5.2")
    assert failover_entry["served_by"] == "failover"
    assert failover_entry["provider"] == "failover"
    assert failover_entry["owned_by"] == "failover"
    assert failover_entry["failover_group"] == "glm-5.2"


@pytest.mark.asyncio
async def test_get_model_metadata_returns_failover_group():
    """GET /v1/models/guardian/failover/{name} must resolve the failover entry."""
    fake_group = SimpleNamespace(name="glm-5.2")

    with patch.object(server, "failover_registry") as fr_mock:
        fr_mock.get_group.return_value = fake_group
        result = await server.get_model_metadata("guardian/failover/glm-5.2", client_id="test-user")

    assert result["id"] == "guardian/failover/glm-5.2"
    assert result["served_by"] == "failover"
    assert result["failover_group"] == "glm-5.2"
    fr_mock.get_group.assert_called_once_with("glm-5.2")


@pytest.mark.asyncio
async def test_get_model_metadata_returns_404_for_unknown_failover_group():
    """GET /v1/models/guardian/failover/{unknown} must 404, not 500."""
    with patch.object(server, "failover_registry") as fr_mock:
        fr_mock.get_group.return_value = None
        with pytest.raises(Exception) as excinfo:
            await server.get_model_metadata("guardian/failover/does-not-exist", client_id="test-user")
    assert excinfo.value.status_code == 404


# ── _prepare_cloud_candidate_request: user field injection ────────────


def test_prepare_cloud_candidate_injects_user_for_openrouter():
    """The client_user_id must be injected as ``user`` in the body for OpenRouter."""
    provider = CloudProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        models=["z-ai/glm-5.2"],
    )
    base_body = {"model": "guardian/openrouter/z-ai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]}
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
    base_body = {"model": "guardian/nvidia/minimaxai/minimax-m3", "messages": [{"role": "user", "content": "hi"}]}
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
        "model": "guardian/openrouter/z-ai/glm-5.2",
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
    base_body = {"model": "guardian/openrouter/z-ai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]}
    _path, json_body, _body, _needs_tr = server._prepare_cloud_candidate_request(
        provider, "z-ai/glm-5.2", "chat/completions", base_body,
    )
    assert "user" not in json_body
