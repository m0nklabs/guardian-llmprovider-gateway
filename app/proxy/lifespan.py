"""Proxy lifespan — startup/shutdown orchestration and idle-unload watcher.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Owns pid-file handling, stale-listener cleanup, background startup model
verification, capture writer startup/shutdown, and the idle-unload watcher.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from typing import Optional

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_proxy_port = 11434
_pid_file = "guardian.pid"
_get_pid_file_path = None
_get_pid_file_status = None
_get_proxy_listener_info = None
_wait_for_proxy_listener_release = None
_is_guardian_uvicorn_listener = None
_stop_stale_guardian_listener = None
_reset_startup_check_status = None
_mark_startup_check_status = None
_operation_state_for_phase = None
_run_startup_check_in_background = None
_set_startup_check_task = None
_cancel_startup_check_task = None
_model_manager = None
_capture_controller = None
_inference_queue = None


def init(
    *,
    proxy_port,
    pid_file,
    get_pid_file_path,
    get_pid_file_status,
    get_proxy_listener_info,
    wait_for_proxy_listener_release,
    is_guardian_uvicorn_listener,
    stop_stale_guardian_listener,
    reset_startup_check_status,
    mark_startup_check_status,
    operation_state_for_phase,
    run_startup_check_in_background,
    set_startup_check_task,
    cancel_startup_check_task,
    model_manager,
    capture_controller,
    inference_queue,
) -> None:
    """Inject all dependencies. Called once at startup."""
    globals()["_cancel_startup_check_task"] = cancel_startup_check_task
    globals()["_proxy_port"] = proxy_port
    globals()["_pid_file"] = pid_file
    globals()["_get_pid_file_path"] = get_pid_file_path
    globals()["_get_pid_file_status"] = get_pid_file_status
    globals()["_get_proxy_listener_info"] = get_proxy_listener_info
    globals()["_wait_for_proxy_listener_release"] = wait_for_proxy_listener_release
    globals()["_is_guardian_uvicorn_listener"] = is_guardian_uvicorn_listener
    globals()["_stop_stale_guardian_listener"] = stop_stale_guardian_listener
    globals()["_reset_startup_check_status"] = reset_startup_check_status
    globals()["_mark_startup_check_status"] = mark_startup_check_status
    globals()["_operation_state_for_phase"] = operation_state_for_phase
    globals()["_run_startup_check_in_background"] = run_startup_check_in_background
    globals()["_set_startup_check_task"] = set_startup_check_task
    globals()["_model_manager"] = model_manager
    globals()["_capture_controller"] = capture_controller
    globals()["_inference_queue"] = inference_queue


@asynccontextmanager
async def run_lifespan(app):

    # Startup: Check and write PID file
    pid_path = _get_pid_file_path()
    pid_file_status = _get_pid_file_status()
    if pid_path.exists():
        try:
            with open(pid_path, 'r') as f:
                content = f.read().strip()
                if content:
                    old_pid = int(content)
                    # Check if process exists
                    if old_pid != os.getpid():
                        try:
                            os.kill(old_pid, 0)
                            listener = _get_proxy_listener_info()
                            if listener and listener.get("pid") == old_pid:
                                released = await _wait_for_proxy_listener_release(old_pid)
                                if released:
                                    logger.info(
                                        f"Existing Guardian listener PID {old_pid} released port {_proxy_port} during restart handoff"
                                    )
                                else:
                                    logger.warning(
                                        f"Listener PID {old_pid} still holds port {_proxy_port}; continuing and relying on bind protection"
                                    )
                            logger.warning(
                                f"Found active PID {old_pid} in {_pid_file}; overwriting it and continuing startup. "
                                "Socket binding will still prevent duplicate Guardian listeners."
                            )
                        except OSError as e:
                            if e.errno == errno.ESRCH:
                                logger.warning(f"Found stale PID file for PID {old_pid}. Overwriting.")
                            else:
                                raise e
        except ValueError:
             logger.warning("Invalid PID file found. Overwriting.")
        except FileNotFoundError:
            pass

    existing_listener = _get_proxy_listener_info()
    if existing_listener and not existing_listener.get("is_current_process"):
        listener_pid = existing_listener.get("pid")
        pid_file_pid = pid_file_status.get("pid")
        if isinstance(listener_pid, int) and listener_pid == pid_file_pid:
            released = await _wait_for_proxy_listener_release(listener_pid)
            if released:
                logger.info(
                    f"Existing Guardian listener PID {listener_pid} released port {_proxy_port} during startup handoff"
                )
            else:
                logger.warning(
                    f"Listener PID {listener_pid} still holds port {_proxy_port}; continuing and relying on bind protection"
                )
        elif _is_guardian_uvicorn_listener(existing_listener):
            stopped = await _stop_stale_guardian_listener(existing_listener)
            if not stopped:
                logger.warning(
                    f"Detected Guardian listener PID {listener_pid} on port {_proxy_port}, but it did not exit during orphan cleanup"
                )
        else:
            logger.warning(
                f"Port {_proxy_port} is already owned by an unexpected process; startup may fail: {existing_listener}"
            )

    try:
        with open(pid_path, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Guardian started with PID {os.getpid()}")
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")

    # SECURITY: Run startup model verification in the background so Guardian
    # binds on 11434 immediately while llama-server is still warming up.
    startup_target = _model_manager.pinned_model or _model_manager.current_model
    generation = _reset_startup_check_status(
        source="startup",
        phase="startup_check",
        target_model=startup_target,
        requested_model=startup_target,
        owner="startup",
    )
    _mark_startup_check_status(
        _operation_state_for_phase("startup_check"),
        generation=generation,
        source="startup",
        phase="startup_check",
        owner="startup",
        target_model=startup_target,
        requested_model=startup_target,
    )
    logger.info("🔄 Scheduling startup model verification in background")
    _set_startup_check_task(
        asyncio.create_task(_run_startup_check_in_background(generation, startup_target))
    )

    # Start capture writer (fail-open: disabled by default, errors are logged not raised)
    capture_writer_task: Optional[asyncio.Task] = None
    try:
        _capture_controller.initialize_writer()
        if _capture_controller.config.is_active:
            await _capture_controller.start_writer()
            logger.info("📸 Capture writer started (instance_id=%s)",
                        _capture_controller.config.instance_id)
        else:
            logger.info("📸 Capture subsystem is disabled (enabled=false)")
    except Exception as exc:
        logger.warning("Capture writer initialization failed (fail-open): %s", exc)

    # Start idle-unload background watcher
    idle_task = asyncio.create_task(idle_unload_watcher())

    yield

    idle_task.cancel()
    _set_startup_check_task(None)
    _cancel_startup_check_task()

    with suppress(asyncio.CancelledError):
        await idle_task

    # Shutdown: Stop capture writer
    try:
        await _capture_controller.stop_writer()
    except Exception as exc:
        logger.warning("Capture writer shutdown error: %s", exc)

    # Shutdown: Remove PID file
    if pid_path.exists():
        try:
            with open(pid_path, 'r') as f:
                content = f.read().strip()
                if content and int(content) == os.getpid():
                     pid_path.unlink()
                     logger.info("PID file removed.")
        except Exception as e:
            logger.warning(f"Failed to clean up PID file: {e}")



async def idle_unload_watcher():
    """Background task: auto-unload llama-server after N minutes of inactivity."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        idle_minutes = _model_manager.idle_unload_minutes
        if idle_minutes is None:
            continue  # Feature disabled
        if _model_manager.is_unloaded:
            continue  # Already free
        if _model_manager.active_requests > 0:
            continue  # Don't unload while requests are in-flight
        if _inference_queue.active_count > 0 or _inference_queue.waiting_count > 0:
            continue  # Don't unload while queue has pending work
        idle_secs = time.time() - _model_manager.last_request_time
        if idle_secs >= idle_minutes * 60:
            logger.info(f"💤 Idle for {idle_secs/60:.1f}m (limit {idle_minutes}m) — auto-unloading to free VRAM")
            try:
                await _model_manager.unload()
            except Exception as e:
                logger.error(f"❌ Auto-unload failed: {e}")
