"""Unit tests for Guardian server startup behavior."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

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


def test_reasoning_falls_back_for_ollama_clients():
    """Ollama bridges should use reasoning text when no visible content is present."""
    assert server._extract_assistant_delta_text({"reasoning_content": "thinking"}) == "thinking"
    assert server._extract_assistant_message_text({"reasoning_content": "answer"}) == "answer"


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
    request_id = queue.submit("observer", "model-x")

    with patch.object(server, "inference_queue", queue):
        snapshot = await server.queue_request_status(request_id, client_id="observer")

    assert snapshot["request_id"] == request_id
    assert snapshot["status"] == "queued"
    assert snapshot["position"] == 1


@pytest.mark.asyncio
async def test_cancel_queue_request_endpoint_marks_running_request():
    queue = server.InferenceQueue(max_concurrent=1)
    request_id = await queue.acquire("observer", "model-x")

    with patch.object(server, "inference_queue", queue):
        snapshot = await server.cancel_queue_request(request_id, client_id="observer")

    assert snapshot["request_id"] == request_id
    assert snapshot["status"] == "cancelling"
    assert snapshot["cancel_reason"] == "client_requested_cancel"


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
        payload = await server.list_models(client_id="test-user")

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
        payload = await server.list_models(client_id="test-user")

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
