"""Proxy process management — pid files, listener inspection, startup state.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Owns the guardian pid file, stale-listener detection/termination, the startup
check state machine (reset/mark/get), and the guarded model operations that
serialize model loads behind the switch lock.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import logging

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_model_manager = None
_model_switch_lock = None
_pid_file = "guardian.pid"
_proxy_port = 11434


def init(*, model_manager, model_switch_lock, pid_file, proxy_port) -> None:
    """Inject all dependencies. Called once at startup."""
    global _model_manager, _model_switch_lock, _pid_file, _proxy_port
    _model_manager = model_manager
    _model_switch_lock = model_switch_lock
    _pid_file = pid_file
    _proxy_port = proxy_port


_startup_check_task: Optional[asyncio.Task] = None
_startup_check_status: Dict[str, Optional[object]] = {
    "state": "idle",
    "phase": "idle",
    "source": None,
    "owner": None,
    "target_model": None,
    "requested_model": None,
    "effective_model": None,
    "started_at": None,
    "completed_at": None,
    "error": None,
    "generation": 0,
}

def get_pid_file_path() -> Path:
    return Path(__file__).parent.parent.parent / _pid_file


def describe_process(pid: int) -> Optional[str]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        return None
    return None


def get_process_cgroup(pid: int) -> Optional[str]:
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except Exception:
        return None

    for line in lines:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return None


def get_proxy_listener_info(port: int = _proxy_port) -> Optional[Dict[str, Optional[object]]]:
    try:
        result = subprocess.run(
            ["ss", "-ltnp", f"( sport = :{port} )"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        for line in result.stdout.splitlines():
            if f":{port}" not in line or "pid=" not in line:
                continue
            pid_match = re.search(r"pid=(\d+)", line)
            name_match = re.search(r'"([^"]+)"', line)
            if not pid_match:
                continue
            pid = int(pid_match.group(1))
            cgroup = get_process_cgroup(pid)
            systemd_unit = None
            if cgroup:
                cgroup_name = Path(cgroup).name
                if cgroup_name.endswith(".service"):
                    systemd_unit = cgroup_name
            return {
                "pid": pid,
                "process_name": name_match.group(1) if name_match else None,
                "command": describe_process(pid),
                "cgroup": cgroup,
                "systemd_unit": systemd_unit,
                "port": port,
                "is_current_process": pid == os.getpid(),
            }
    except Exception as e:
        logger.debug(f"Failed to inspect proxy listener on {port}: {e}")
    return None


def get_pid_file_status() -> Dict[str, Optional[object]]:
    pid_path = get_pid_file_path()
    status: Dict[str, Optional[object]] = {
        "path": str(pid_path),
        "exists": pid_path.exists(),
        "pid": None,
        "alive": None,
    }
    if not pid_path.exists():
        return status

    try:
        raw = pid_path.read_text().strip()
        if not raw:
            return status
        pid = int(raw)
        status["pid"] = pid
        try:
            os.kill(pid, 0)
            status["alive"] = True
        except OSError as exc:
            status["alive"] = exc.errno != errno.ESRCH
    except Exception:
        status["alive"] = False
    return status


async def wait_for_proxy_listener_release(old_pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listener = get_proxy_listener_info()
        if listener is None or listener.get("pid") != old_pid:
            return True
        await asyncio.sleep(0.1)
    return False


def operation_state_for_phase(phase: str) -> str:
    if phase == "startup_check":
        return "checking"
    if phase in {"manual_load", "auto_switch", "auto_reload", "backend_reload"}:
        return "switching"
    return "running"


def startup_state_is_in_progress(state: Optional[str]) -> bool:
    return state in {"pending", "running", "checking", "switching"}


def reset_startup_check_status(
    *,
    source: str,
    phase: str,
    target_model: Optional[str],
    requested_model: Optional[str] = None,
    owner: Optional[str] = None,
) -> int:
    generation = int(_startup_check_status.get("generation", 0)) + 1
    _startup_check_status.update(
        {
            "state": "pending",
            "phase": phase,
            "source": source,
            "owner": owner,
            "target_model": target_model,
            "requested_model": requested_model,
            "effective_model": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "generation": generation,
        }
    )
    return generation


def mark_startup_check_status(
    state: str,
    error: Optional[str] = None,
    *,
    generation: Optional[int] = None,
    phase: Optional[str] = None,
    source: Optional[str] = None,
    owner: Optional[str] = None,
    target_model: Optional[str] = None,
    requested_model: Optional[str] = None,
    effective_model: Optional[str] = None,
) -> None:
    if generation is not None and generation != _startup_check_status.get("generation"):
        return

    now = time.time()
    _startup_check_status["state"] = state
    if phase is not None:
        _startup_check_status["phase"] = phase
    if source is not None:
        _startup_check_status["source"] = source
    if owner is not None:
        _startup_check_status["owner"] = owner
    if target_model is not None:
        _startup_check_status["target_model"] = target_model
    if requested_model is not None:
        _startup_check_status["requested_model"] = requested_model
    if effective_model is not None:
        _startup_check_status["effective_model"] = effective_model
    if startup_state_is_in_progress(state):
        _startup_check_status["started_at"] = now
        _startup_check_status["completed_at"] = None
        _startup_check_status["error"] = None
        return

    if _startup_check_status["started_at"] is None:
        _startup_check_status["started_at"] = now
    _startup_check_status["completed_at"] = now
    _startup_check_status["error"] = error


def get_startup_check_status() -> Dict[str, Optional[object]]:
    snapshot = dict(_startup_check_status)
    snapshot["task_active"] = _startup_check_task is not None and not _startup_check_task.done()
    return snapshot


async def run_guardian_operation(
    *,
    source: str,
    phase: str,
    target_model: Optional[str],
    requested_model: Optional[str],
    owner: Optional[str],
    operation,
    generation: int,
):
    in_progress_state = operation_state_for_phase(phase)
    mark_startup_check_status(
        in_progress_state,
        generation=generation,
        source=source,
        phase=phase,
        owner=owner,
        target_model=target_model,
        requested_model=requested_model,
    )

    try:
        result = await operation()
    except asyncio.CancelledError:
        mark_startup_check_status("cancelled", generation=generation)
        raise
    except Exception as e:
        mark_startup_check_status("error", str(e), generation=generation)
        raise

    healthy = await _model_manager.backend_health_ok()
    verified = await _model_manager.verify_backend_model() if healthy else False
    effective_model = await _model_manager.get_current_model()

    if healthy and verified:
        mark_startup_check_status(
            "ready",
            generation=generation,
            source=source,
            phase=phase,
            target_model=target_model,
            requested_model=requested_model,
            effective_model=effective_model,
        )
    else:
        reasons = []
        if not healthy:
            reasons.append("backend_health_check_failed")
        if not verified:
            reasons.append("backend_model_unverified")
        mark_startup_check_status(
            "degraded",
            ", ".join(reasons) or None,
            generation=generation,
            source=source,
            phase=phase,
            target_model=target_model,
            requested_model=requested_model,
            effective_model=effective_model,
        )
    return result


async def run_startup_check_in_background(generation: int, target_model: Optional[str]) -> None:
    try:
        async with _model_switch_lock:
            await run_guardian_operation(
                source="startup",
                phase="startup_check",
                target_model=target_model,
                requested_model=target_model,
                owner="startup",
                operation=_model_manager.startup_check,
                generation=generation,
            )
    except Exception as e:
        logger.error(f"⚠️ Startup check error (non-fatal): {e}")
    else:
        logger.info("✅ Startup check completed in background")


def is_guardian_uvicorn_listener(listener: Optional[Dict[str, Optional[object]]]) -> bool:
    if not listener:
        return False
    command = str(listener.get("command") or "")
    repo_root = str(Path(__file__).parent.parent.parent)
    return (
        listener.get("process_name") == "uvicorn"
        and "app.proxy.server:app" in command
        and repo_root in command
        and f"--port {_proxy_port}" in command
    )


async def stop_stale_guardian_listener(
    listener: Optional[Dict[str, Optional[object]]], timeout: float = 3.0
) -> bool:
    if not is_guardian_uvicorn_listener(listener):
        return False

    pid = listener.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return False

    logger.warning(
        f"Terminating stale Guardian listener PID {pid} before binding port {_proxy_port}: "
        f"{listener.get('command')}"
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return True
        raise

    if await wait_for_proxy_listener_release(pid, timeout=timeout):
        return True

    logger.warning(f"Stale Guardian listener PID {pid} ignored SIGTERM; sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise

    return await wait_for_proxy_listener_release(pid, timeout=1.0)



def set_startup_check_task(task: Optional[asyncio.Task]) -> None:
    """Bind the background startup-check task (used by lifespan)."""
    global _startup_check_task
    _startup_check_task = task


def cancel_startup_check_task() -> None:
    """Cancel and await the background startup-check task, if any."""
    global _startup_check_task
    if _startup_check_task is None:
        return
    _startup_check_task.cancel()
    try:
        if not _startup_check_task.done():
            # best-effort; caller may be in a shutdown path
            pass
    except Exception:
        pass
    _startup_check_task = None


