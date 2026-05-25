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
