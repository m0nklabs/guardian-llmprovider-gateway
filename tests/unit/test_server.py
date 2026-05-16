"""Unit tests for Guardian server startup behavior."""

import asyncio
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


def test_resolve_inference_model_prefers_tool_profile():
    """Auto requests should prefer a tool-friendly sibling when available."""
    with patch.object(server.model_manager, "get_preferred_tool_model", return_value="Qwen-Agent"):
        assert server._resolve_inference_model("auto", "Qwen-Deep") == "Qwen-Agent"


def test_reasoning_falls_back_for_ollama_clients():
    """Ollama bridges should use reasoning text when no visible content is present."""
    assert server._extract_assistant_delta_text({"reasoning_content": "thinking"}) == "thinking"
    assert server._extract_assistant_message_text({"reasoning_content": "answer"}) == "answer"


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
    listener = {
        "pid": 4242,
        "process_name": "uvicorn",
        "command": "/home/flip/llama_cpp_guardian/venv/bin/python3.14 /home/flip/llama_cpp_guardian/venv/bin/uvicorn app.proxy.server:app --host 0.0.0.0 --port 11434",
        "is_current_process": False,
    }

    with (
        patch.object(server, "_wait_for_proxy_listener_release", return_value=True),
        patch.object(server.os, "kill") as kill_mock,
    ):
        assert await server._stop_stale_guardian_listener(listener) is True
        kill_mock.assert_called_once_with(4242, server.signal.SIGTERM)