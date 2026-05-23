import os
import base64
import json
import asyncio
import logging
import re
import signal
import subprocess
import time
import errno
import struct
import zlib
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.status import HTTP_401_UNAUTHORIZED

from collections import defaultdict
from app.proxy.optimizer import RequestOptimizer
from app.proxy.scaler import DynamicScaler
from app.engine.manager import ModelManager, ModelLoadError
from app.proxy.auth import verify_api_key
from app.proxy.queue import InferenceQueue
from app.proxy.metrics import (
    track_request,
    update_queue_metrics,
    update_gpu_metrics,
    update_system_metrics,
    get_metrics_output,
    MODEL_SWITCHES,
    MODEL_CRASHES,
    QUEUE_TOTAL_QUEUED,
    QUEUE_TOTAL_COMPLETED,
    QUEUE_TOTAL_TIMEOUTS,
    AUTH_FAILURES,
)

# Load configuration from settings.yaml
def load_config() -> dict:

    """Load configuration from settings.yaml with sensible defaults."""
    config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
    default_config = {
        "timeouts": {
            "tiers": {
                "tier_70b": {"min_size_mb": 40000, "timeout_seconds": 900},
                "tier_32b": {"min_size_mb": 20000, "timeout_seconds": 600},
                "tier_13b": {"min_size_mb": 10000, "timeout_seconds": 300},
                "tier_8b": {"min_size_mb": 5000, "timeout_seconds": 180},
                "tier_small": {"min_size_mb": 0, "timeout_seconds": 120},
            },
            "default_timeout": 300
        }
    }
    
    try:
        if config_path.exists():
            with open(config_path, 'r') as f:
                file_config = yaml.safe_load(f) or {}
            # Merge with defaults (file config takes precedence)
            if "timeouts" in file_config:
                default_config["timeouts"].update(file_config["timeouts"])
            return default_config
    except Exception as e:
        logging.warning(f"Failed to load config from {config_path}: {e}. Using defaults.")
    
    return default_config

# Load config at module level
CONFIG = load_config()

# Configuration
LLAMA_SERVER_URL = "http://127.0.0.1:11440"

# Total VRAM available (approx 28GB: 12GB + 16GB)
# Read from settings.yaml proxy.vram_limit_mb, fallback to hardcoded value
def _load_vram_limit() -> int:
    try:
        config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        if config_path.exists():
            with open(config_path, 'r') as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("proxy", {}).get("vram_limit_mb", 27000)
    except Exception:
        pass
    return 27000

SAFE_VRAM_LIMIT_MB = _load_vram_limit()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Guardian")

PID_FILE = "guardian.pid"
PROXY_PORT = 11434
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

_VISION_PROBE_IMAGE_DATA_URL: Optional[str] = None


def _get_pid_file_path() -> Path:
    return Path(__file__).parent.parent.parent / PID_FILE


def _describe_process(pid: int) -> Optional[str]:
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


def _get_process_cgroup(pid: int) -> Optional[str]:
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except Exception:
        return None

    for line in lines:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return None


def _get_proxy_listener_info(port: int = PROXY_PORT) -> Optional[Dict[str, Optional[object]]]:
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
            cgroup = _get_process_cgroup(pid)
            systemd_unit = None
            if cgroup:
                cgroup_name = Path(cgroup).name
                if cgroup_name.endswith(".service"):
                    systemd_unit = cgroup_name
            return {
                "pid": pid,
                "process_name": name_match.group(1) if name_match else None,
                "command": _describe_process(pid),
                "cgroup": cgroup,
                "systemd_unit": systemd_unit,
                "port": port,
                "is_current_process": pid == os.getpid(),
            }
    except Exception as e:
        logger.debug(f"Failed to inspect proxy listener on {port}: {e}")
    return None


def _get_pid_file_status() -> Dict[str, Optional[object]]:
    pid_path = _get_pid_file_path()
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


async def _wait_for_proxy_listener_release(old_pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listener = _get_proxy_listener_info()
        if listener is None or listener.get("pid") != old_pid:
            return True
        await asyncio.sleep(0.1)
    return False


def _operation_state_for_phase(phase: str) -> str:
    if phase == "startup_check":
        return "checking"
    if phase in {"manual_load", "auto_switch", "auto_reload", "backend_reload"}:
        return "switching"
    return "running"


def _startup_state_is_in_progress(state: Optional[str]) -> bool:
    return state in {"pending", "running", "checking", "switching"}


def _extract_assistant_message_text(message: Dict[str, object]) -> str:
    content = str(message.get("content") or "")
    if content:
        return content
    return str(message.get("reasoning_content") or "")


def _extract_assistant_delta_text(delta: Dict[str, object]) -> str:
    content = str(delta.get("content") or "")
    if content:
        return content
    return str(delta.get("reasoning_content") or "")


def _is_guardian_uvicorn_listener(listener: Optional[Dict[str, Optional[object]]]) -> bool:
    if not listener:
        return False
    command = str(listener.get("command") or "")
    repo_root = str(Path(__file__).parent.parent.parent)
    return (
        listener.get("process_name") == "uvicorn"
        and "app.proxy.server:app" in command
        and repo_root in command
        and f"--port {PROXY_PORT}" in command
    )


async def _stop_stale_guardian_listener(
    listener: Optional[Dict[str, Optional[object]]], timeout: float = 3.0
) -> bool:
    if not _is_guardian_uvicorn_listener(listener):
        return False

    pid = listener.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return False

    logger.warning(
        f"Terminating stale Guardian listener PID {pid} before binding port {PROXY_PORT}: "
        f"{listener.get('command')}"
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return True
        raise

    if await _wait_for_proxy_listener_release(pid, timeout=timeout):
        return True

    logger.warning(f"Stale Guardian listener PID {pid} ignored SIGTERM; sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise

    return await _wait_for_proxy_listener_release(pid, timeout=1.0)


def _reset_startup_check_status(
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


def _mark_startup_check_status(
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
    if _startup_state_is_in_progress(state):
        _startup_check_status["started_at"] = now
        _startup_check_status["completed_at"] = None
        _startup_check_status["error"] = None
        return

    if _startup_check_status["started_at"] is None:
        _startup_check_status["started_at"] = now
    _startup_check_status["completed_at"] = now
    _startup_check_status["error"] = error


def _get_startup_check_status() -> Dict[str, Optional[object]]:
    snapshot = dict(_startup_check_status)
    snapshot["task_active"] = _startup_check_task is not None and not _startup_check_task.done()
    return snapshot


async def _run_guardian_operation(
    *,
    source: str,
    phase: str,
    target_model: Optional[str],
    requested_model: Optional[str],
    owner: Optional[str],
    operation,
    generation: int,
):
    in_progress_state = _operation_state_for_phase(phase)
    _mark_startup_check_status(
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
        _mark_startup_check_status("cancelled", generation=generation)
        raise
    except Exception as e:
        _mark_startup_check_status("error", str(e), generation=generation)
        raise

    healthy = await model_manager.backend_health_ok()
    verified = await model_manager.verify_backend_model() if healthy else False
    effective_model = await model_manager.get_current_model()

    if healthy and verified:
        _mark_startup_check_status(
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
        _mark_startup_check_status(
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


async def _run_startup_check_in_background(generation: int, target_model: Optional[str]) -> None:
    try:
        async with _model_switch_lock:
            await _run_guardian_operation(
                source="startup",
                phase="startup_check",
                target_model=target_model,
                requested_model=target_model,
                owner="startup",
                operation=model_manager.startup_check,
                generation=generation,
            )
    except Exception as e:
        logger.error(f"⚠️ Startup check error (non-fatal): {e}")
    else:
        logger.info("✅ Startup check completed in background")


def _resolve_inference_model(raw_model: Optional[str], current_model: str) -> Optional[str]:
    if not raw_model:
        return raw_model
    if raw_model == "auto":
        preferred = model_manager.get_preferred_tool_model(current_model)
        return preferred or current_model
    try:
        return model_manager.resolve_model(raw_model)
    except ValueError:
        return raw_model


def _queue_headers(request_id: str, queue_wait_ms: float) -> Dict[str, str]:
    return {
        "X-Request-Id": request_id,
        "X-Queue-Wait-Ms": str(int(queue_wait_ms)),
    }


def _messages_contain_image_input(messages: List[Dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"image_url", "input_image"}:
                return True
    return False


def _build_probe_image_data_url() -> str:
    global _VISION_PROBE_IMAGE_DATA_URL
    if _VISION_PROBE_IMAGE_DATA_URL is not None:
        return _VISION_PROBE_IMAGE_DATA_URL

    width = 128
    height = 128
    row = b"\x00" + (b"\xff\xff\xff" * width)
    raw = row * height
    compressed = zlib.compress(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", checksum)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    _VISION_PROBE_IMAGE_DATA_URL = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return _VISION_PROBE_IMAGE_DATA_URL


def _extract_backend_error_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or text)
        detail = parsed.get("detail")
        if isinstance(detail, str):
            return detail
    return text


def _truncate_error_message(message: str, limit: int = 300) -> str:
    cleaned = " ".join(message.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _openai_error_response(
    *,
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    payload = {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }
    return JSONResponse(status_code=status_code, content=payload, headers=headers or {})


async def _probe_multimodal_runtime(model_name: str) -> Dict[str, Any]:
    capability = model_manager.get_vision_capability(model_name)
    if capability["status"] in {"supported", "unsupported", "misconfigured", "text_only", "load_failed"}:
        return capability

    payload = {
        "model": model_name,
        "stream": False,
        "max_tokens": 1,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _build_probe_image_data_url()}},
                    {"type": "text", "text": "Reply with one short word."},
                ],
            }
        ],
    }

    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(3):
            resp = await client.post(f"{LLAMA_SERVER_URL}/v1/chat/completions", json=payload)
            message = _extract_backend_error_message(resp.content)
            lowered = message.lower()

            if 200 <= resp.status_code < 300:
                model_manager.mark_vision_validation(model_name, "supported")
                return model_manager.get_vision_capability(model_name)

            if resp.status_code == 503 and "loading model" in lowered and attempt < 2:
                model_manager.mark_vision_validation(model_name, "loading", message)
                await asyncio.sleep(1.0)
                continue

            if resp.status_code == 503 and "loading model" in lowered:
                model_manager.mark_vision_validation(model_name, "loading", message)
                return model_manager.get_vision_capability(model_name)

            failure_status = "unsupported"
            if resp.status_code == 503:
                failure_status = "loading"
            model_manager.mark_vision_validation(model_name, failure_status, message or f"HTTP {resp.status_code}")
            return model_manager.get_vision_capability(model_name)

    return model_manager.get_vision_capability(model_name)


async def _preflight_multimodal_request(
    model_name: str,
    request_id: str,
    queue_wait_ms: float,
) -> Optional[JSONResponse]:
    headers = _queue_headers(request_id, queue_wait_ms)
    capability = model_manager.get_vision_capability(model_name)

    if not capability["configured"]:
        return _openai_error_response(
            status_code=400,
            message=f"Model '{model_name}' is text-only in Guardian and cannot accept image_url content.",
            error_type="invalid_request_error",
            code="vision_not_configured",
            headers=headers,
        )

    if not capability["mmproj_exists"]:
        return _openai_error_response(
            status_code=400,
            message=f"Model '{model_name}' is configured for vision but its mmproj file is missing.",
            error_type="invalid_request_error",
            code="mmproj_missing",
            headers=headers,
        )

    if capability["status"] != "supported":
        capability = await _probe_multimodal_runtime(model_name)

    status = capability["status"]
    if status == "supported":
        return None

    if status in {"loading", "load_failed"}:
        return _openai_error_response(
            status_code=503,
            message=f"Model '{model_name}' is not ready for image requests yet: {_truncate_error_message(capability.get('last_error') or 'still loading')}",
            error_type="unavailable_error",
            code="vision_model_unavailable",
            headers=headers,
        )

    return _openai_error_response(
        status_code=422,
        message=(
            f"Model '{model_name}' is configured for vision, but its runtime rejected OpenAI image_url content. "
            f"Backend detail: {_truncate_error_message(capability.get('last_error') or 'unknown multimodal error')}"
        ),
        error_type="invalid_request_error",
        code="vision_not_supported",
        headers=headers,
    )


def _desired_runtime_vision_enabled(model_name: str, has_image_inputs: bool) -> bool:
    """Return whether this request should load the target model with mmproj."""
    capability = model_manager.get_vision_capability(model_name)
    return bool(has_image_inputs and capability.get("configured"))


def _map_multimodal_backend_error(
    model_name: str,
    status_code: int,
    body: bytes,
    request_id: str,
    queue_wait_ms: float,
) -> Optional[JSONResponse]:
    message = _extract_backend_error_message(body)
    lowered = message.lower()
    headers = _queue_headers(request_id, queue_wait_ms)

    if status_code == 503 and "loading model" in lowered:
        model_manager.mark_vision_validation(model_name, "loading", message)
        return _openai_error_response(
            status_code=503,
            message=f"Model '{model_name}' is still loading its multimodal runtime. Retry shortly.",
            error_type="unavailable_error",
            code="vision_model_unavailable",
            headers=headers,
        )

    if "image input is not supported" in lowered or "mmproj" in lowered:
        model_manager.mark_vision_validation(model_name, "unsupported", message)
        return _openai_error_response(
            status_code=422,
            message=f"Model '{model_name}' rejected image_url content at runtime: {_truncate_error_message(message)}",
            error_type="invalid_request_error",
            code="vision_not_supported",
            headers=headers,
        )

    if status_code >= 500:
        model_manager.mark_vision_validation(model_name, "unsupported", message or f"HTTP {status_code}")
        return _openai_error_response(
            status_code=422,
            message=(
                f"Model '{model_name}' is configured for vision, but the backend image path failed. "
                f"Backend detail: {_truncate_error_message(message or f'HTTP {status_code}') }"
            ),
            error_type="invalid_request_error",
            code="vision_runtime_unavailable",
            headers=headers,
        )

    return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_check_task

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
                                        f"Existing Guardian listener PID {old_pid} released port {PROXY_PORT} during restart handoff"
                                    )
                                else:
                                    logger.warning(
                                        f"Listener PID {old_pid} still holds port {PROXY_PORT}; continuing and relying on bind protection"
                                    )
                            logger.warning(
                                f"Found active PID {old_pid} in {PID_FILE}; overwriting it and continuing startup. "
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
                    f"Existing Guardian listener PID {listener_pid} released port {PROXY_PORT} during startup handoff"
                )
            else:
                logger.warning(
                    f"Listener PID {listener_pid} still holds port {PROXY_PORT}; continuing and relying on bind protection"
                )
        elif _is_guardian_uvicorn_listener(existing_listener):
            stopped = await _stop_stale_guardian_listener(existing_listener)
            if not stopped:
                logger.warning(
                    f"Detected Guardian listener PID {listener_pid} on port {PROXY_PORT}, but it did not exit during orphan cleanup"
                )
        else:
            logger.warning(
                f"Port {PROXY_PORT} is already owned by an unexpected process; startup may fail: {existing_listener}"
            )

    try:
        with open(pid_path, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Guardian started with PID {os.getpid()}")
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")

    # SECURITY: Run startup model verification in the background so Guardian
    # binds on 11434 immediately while llama-server is still warming up.
    startup_target = model_manager.pinned_model or model_manager.current_model
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
    _startup_check_task = asyncio.create_task(_run_startup_check_in_background(generation, startup_target))

    # Start idle-unload background watcher
    idle_task = asyncio.create_task(_idle_unload_watcher())

    yield

    idle_task.cancel()
    if _startup_check_task is not None:
        _startup_check_task.cancel()

    with suppress(asyncio.CancelledError):
        await idle_task

    if _startup_check_task is not None:
        with suppress(asyncio.CancelledError):
            await _startup_check_task
        _startup_check_task = None
    
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

app = FastAPI(lifespan=lifespan)
model_manager = ModelManager()


async def _idle_unload_watcher():
    """Background task: auto-unload llama-server after N minutes of inactivity."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        idle_minutes = model_manager.idle_unload_minutes
        if idle_minutes is None:
            continue  # Feature disabled
        if model_manager.is_unloaded:
            continue  # Already free
        if model_manager.active_requests > 0:
            continue  # Don't unload while requests are in-flight
        if inference_queue.active_count > 0 or inference_queue.waiting_count > 0:
            continue  # Don't unload while queue has pending work
        idle_secs = time.time() - model_manager.last_request_time
        if idle_secs >= idle_minutes * 60:
            logger.info(f"💤 Idle for {idle_secs/60:.1f}m (limit {idle_minutes}m) — auto-unloading to free VRAM")
            try:
                await model_manager.unload()
            except Exception as e:
                logger.error(f"❌ Auto-unload failed: {e}")


def get_gpu_metrics():
    try:
        result = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=memory.used,memory.free,memory.total', '--format=csv,nounits,noheader'],
            encoding='utf-8'
        )
        lines = result.strip().split('\n')
        total_used = 0
        total_free = 0
        total_cap = 0
        for line in lines:
            u, f, t = map(int, line.split(','))
            total_used += u
            total_free += f
            total_cap += t
        return {'used': total_used, 'free': total_free, 'total': total_cap}
    except Exception as e:
        logger.error(f"Failed to get GPU metrics: {e}")
        return {'used': 0, 'free': SAFE_VRAM_LIMIT_MB, 'total': SAFE_VRAM_LIMIT_MB}

def get_model_size(model_name: str) -> int:
    if not model_name: return 0
    model_lower = model_name.lower()
    # Specific overrides for new models
    if "glm-4" in model_lower: return 26000  # ~24GB
    if "qwen3" in model_lower and "30b" in model_lower: return 20000 # ~18GB
    if "deepseek-r1" in model_lower and "32b" in model_lower: return 22000 # ~19GB
    
    # Generic heuristics
    if "70b" in model_lower: return 40000
    if "32b" in model_lower: return 20000
    if "30b" in model_lower: return 20000
    if "27b" in model_lower: return 18000
    if "13b" in model_lower: return 10000
    if "14b" in model_lower: return 11000
    if "8b" in model_lower: return 6000
    if "7b" in model_lower: return 5000
    if "1.5b" in model_lower: return 1500
    
    # Small models
    if "0.5b" in model_lower: return 600
    if "embed" in model_lower: return 500
    
    # Default fallback
    return 4000

def get_model_timeout(model_name: str) -> int:
    """Calculate timeout based on model size using config tiers.
    
    Tiers are configurable in config/settings.yaml under 'timeouts.tiers'.
    Each tier has min_size_mb and timeout_seconds.
    """
    size = get_model_size(model_name)
    timeout_config = CONFIG.get("timeouts", {})
    tiers = timeout_config.get("tiers", {})
    default_timeout = timeout_config.get("default_timeout", 300)
    
    # Sort tiers by min_size_mb descending to match largest first
    sorted_tiers = sorted(
        tiers.items(),
        key=lambda x: x[1].get("min_size_mb", 0),
        reverse=True
    )
    
    for tier_name, tier_config in sorted_tiers:
        min_size = tier_config.get("min_size_mb", 0)
        timeout = tier_config.get("timeout_seconds", default_timeout)
        
        if size >= min_size:
            logger.debug(f"Model {model_name} ({size}MB) matched tier '{tier_name}' -> {timeout}s timeout")
            return timeout
    
    # Fallback to default
    logger.debug(f"Model {model_name} ({size}MB) using default timeout -> {default_timeout}s")
    return default_timeout


# Model switch concurrency lock - prevents race conditions when
# multiple requests try to switch models simultaneously
_model_switch_lock = asyncio.Lock()

# Auth replaced by verify_api_key imported from app.proxy.auth

# VramScheduler
class VramScheduler:

    def __init__(self, limit_mb):
        self.limit_mb = limit_mb
        self.active_counts = defaultdict(int) # model -> count
        self.condition = asyncio.Condition()

    async def acquire(self, model_name, model_size_mb):
        async with self.condition:
            while True:
                # Calculate what VRAM would be if we proceed
                current_active_models = [m for m, c in self.active_counts.items() if c > 0]
                
                needed_vram = 0
                for m in current_active_models:
                    needed_vram += get_model_size(m)
                
                # If this model is NOT already active, we need to add its size
                if model_name not in current_active_models:
                    needed_vram += model_size_mb
                
                if needed_vram <= self.limit_mb:
                    self.active_counts[model_name] += 1
                    logger.info(f"VRAM Acquired for {model_name}. Active: {current_active_models + [model_name] if model_name not in current_active_models else current_active_models}")
                    return # Success
                
                # Wait
                logger.info(f"Wait: {model_name} ({model_size_mb}MB) needs space. Active: {current_active_models} (Total: {needed_vram}MB > {self.limit_mb}MB)")
                await self.condition.wait()

    async def release(self, model_name):
        async with self.condition:
            self.active_counts[model_name] -= 1
            if self.active_counts[model_name] <= 0:
                del self.active_counts[model_name]
            self.condition.notify_all()
            logger.info(f"VRAM Released for {model_name}.")

# State
class State:
    def __init__(self):
        self.active_generations: Dict[str, int] = {} # request_id -> vram_usage
        self.model_stats: Dict[str, int] = {}
        self.last_used: Dict[str, float] = defaultdict(float)
        # VRAM Scheduler
        self.scheduler = VramScheduler(SAFE_VRAM_LIMIT_MB)
        # Optimizer
        self.optimizer = RequestOptimizer()
        # Dynamic scaler — adaptive reasoning budget & max_tokens
        self.scaler = DynamicScaler()

state = State()


# --- Inference queue: serializes access to single-slot backend ---
def _load_queue_config() -> dict:
    try:
        config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        if config_path.exists():
            with open(config_path, 'r') as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("queue", {})
    except Exception:
        pass
    return {}

_queue_cfg = _load_queue_config()
inference_queue = InferenceQueue(
    max_concurrent=_queue_cfg.get("max_concurrent", 1),
    queue_timeout=_queue_cfg.get("queue_timeout_seconds", 300),
)


async def _reload_backend_after_connect_error(path: str, error: Exception) -> None:
    """Reload llama-server once after Guardian detects stale backend state."""
    current_model = await model_manager.get_current_model()
    logger.warning(
        f"⚠️ Backend unreachable while proxying /v1/{path}; "
        f"reloading '{current_model}' once before retry: {error}"
    )

    async with _model_switch_lock:
        if await model_manager.backend_health_ok():
            model_manager.is_unloaded = False
            logger.info("✅ Backend became healthy before retry")
            return

        model_manager.is_unloaded = True
        try:
            generation = _reset_startup_check_status(
                source="proxy",
                phase="backend_reload",
                target_model=current_model,
                requested_model=current_model,
                owner="backend_recovery",
            )
            await _run_guardian_operation(
                source="proxy",
                phase="backend_reload",
                target_model=current_model,
                requested_model=current_model,
                owner="backend_recovery",
                operation=lambda: model_manager.load(current_model),
                generation=generation,
            )
        except ModelLoadError as e:
            crash = e.crash_record
            detail = {
                "error": f"Backend reload failed for '{current_model}'",
                "message": str(e),
                "crash_details": crash.to_dict() if crash else None,
            }
            logger.error(f"💥 Backend reload crash: {detail}")
            raise HTTPException(status_code=503, detail=detail)
        except Exception as e:
            logger.error(f"❌ Backend reload failed after connect error: {e}")
            raise HTTPException(status_code=503, detail=f"Backend reload failed: {e}")


@app.post("/api/chat")
async def proxy_chat_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Bridge Ollama-style chat requests to OpenAI-style Llama Server"""
    try:
        body = await request.json()
    except:
        body = {}
        
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    current_model = await model_manager.get_current_model()
    model = _resolve_inference_model(model, current_model) or model

    logger.info(f"bridge: Ollama chat request for '{model}' -> Translating to OpenAI format")

    # Acquire inference slot (blocks if another request is active)
    try:
        request_id = await inference_queue.acquire(client_id, model)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "queue_timeout", "message": f"Waited {inference_queue.queue_timeout}s in queue"},
        )

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if model_manager.is_unloaded:
            logger.info(f"🔄 Auto-reloading '{model_manager.current_model}'...")
            generation = _reset_startup_check_status(
                source="proxy",
                phase="auto_reload",
                target_model=model_manager.current_model,
                requested_model=model_manager.current_model,
                owner=client_id,
            )
            async with _model_switch_lock:
                if model_manager.is_unloaded:
                    await _run_guardian_operation(
                        source="proxy",
                        phase="auto_reload",
                        target_model=model_manager.current_model,
                        requested_model=model_manager.current_model,
                        owner=client_id,
                        operation=model_manager.load,
                        generation=generation,
                    )

        # Check if model switch needed (safe — we hold the queue slot)
        current_model = await model_manager.get_current_model()
        if model != current_model and model in model_manager.models:
            # SECURITY: Check client permission and pin
            if not model_manager.is_switch_allowed(client_id):
                logger.warning(f"🔒 Client '{client_id}' not in switch_allowlist, blocked Ollama switch to '{model}'")
            else:
                generation = _reset_startup_check_status(
                    source="proxy",
                    phase="auto_switch",
                    target_model=model,
                    requested_model=body.get("model"),
                    owner=client_id,
                )
                async with _model_switch_lock:
                    # Re-check after acquiring lock (another request may have switched already)
                    current_model = await model_manager.get_current_model()
                    if model != current_model:
                        try:
                            await _run_guardian_operation(
                                source="proxy",
                                phase="auto_switch",
                                target_model=model,
                                requested_model=body.get("model"),
                                owner=client_id,
                                operation=lambda: model_manager.switch_model(model, client_id=client_id),
                                generation=generation,
                            )
                        except ModelLoadError as e:
                            crash = e.crash_record
                            detail = {
                                "error": f"Model '{model}' failed to load",
                                "message": str(e),
                                "crash_details": crash.to_dict() if crash else None,
                            }
                            logger.error(f"💥 Model load crash: {detail}")
                            raise HTTPException(status_code=503, detail=detail)
                        except ValueError as e:
                            logger.warning(f"🔒 Switch denied: {e}")
                        except Exception as e:
                            logger.error(f"❌ Switch failed: {e}")
                            raise HTTPException(status_code=500, detail=f"Model switch failed: {e}")

        model_manager.last_request_time = time.time()
        model_manager.active_requests += 1

        # Translate Ollama request to OpenAI format
        messages = body.get("messages", [])
        stream = body.get("stream", True)
        
        # Basic options mapping
        options = body.get("options", {})
        temperature = options.get("temperature", 0.7)
        
        openai_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature
        }

        # Forward to Llama Server (OpenAI Endpoint)
        timeout_sec = get_model_timeout(model)
        client = httpx.AsyncClient(timeout=timeout_sec)
        
        req = client.build_request(
            "POST",
            f"{LLAMA_SERVER_URL}/v1/chat/completions",
            json=openai_body,
            timeout=timeout_sec
        )
        
        try:
            r = await client.send(req, stream=stream)
        except Exception as e:
            await client.aclose()
            raise e

        if stream:
            async def stream_adapter():
                try:
                    async for chunk in r.aiter_lines():
                        if not chunk or chunk.strip() == "data: [DONE]": 
                            continue
                        if chunk.startswith("data: "):
                            try:
                                data = json.loads(chunk[6:])
                                # Translate OpenAI chunk back to Ollama chunk
                                if "choices" in data and len(data["choices"]) > 0:
                                    delta = data["choices"][0].get("delta", {})
                                    content = _extract_assistant_delta_text(delta)
                                    if content:
                                        ollama_chunk = {
                                            "model": model,
                                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                            "message": {"role": "assistant", "content": content},
                                            "done": False
                                        }
                                        yield json.dumps(ollama_chunk) + "\n"
                            except:
                                pass
                    # Final done message
                    yield json.dumps({
                        "model": model, 
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), 
                        "done": True,
                        "total_duration": 0,
                        "load_duration": 0,
                        "prompt_eval_count": 0,
                        "eval_count": 0
                    }) + "\n"
                finally:
                    await r.aclose()
                    await client.aclose()
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.release(request_id)

            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            response = StreamingResponse(
                stream_adapter(),
                media_type="application/x-ndjson",
                headers={"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
            )
            _release_in_finally = False
            return response
        else:
            # Handle non-streaming response
            try:
                data = r.json()
                content = _extract_assistant_message_text(data["choices"][0]["message"])
                ollama_resp = {
                    "model": model,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                    "total_duration": 0,
                    "load_duration": 0,
                    "prompt_eval_count": data.get("usage", {}).get("prompt_tokens", 0),
                    "eval_count": data.get("usage", {}).get("completion_tokens", 0)
                }
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                return ollama_resp
            except Exception as e:
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
    finally:
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.release(request_id)

# Legacy endpoint for Ollama generate
@app.post("/api/generate")
async def proxy_generate_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Bridge Ollama /api/generate (prompt-based) to /api/chat logic"""
    try:
        body = await request.json()
    except:
        body = {}
        
    prompt = body.get("prompt", "")
    if prompt and "messages" not in body:
        body["messages"] = [{"role": "user", "content": prompt}]
    
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    current_model = await model_manager.get_current_model()
    model = _resolve_inference_model(model, current_model) or model

    # Acquire inference slot
    try:
        request_id = await inference_queue.acquire(client_id, model)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "queue_timeout", "message": f"Waited {inference_queue.queue_timeout}s in queue"},
        )

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if model_manager.is_unloaded:
            logger.info(f"🔄 Auto-reloading '{model_manager.current_model}'...")
            generation = _reset_startup_check_status(
                source="proxy",
                phase="auto_reload",
                target_model=model_manager.current_model,
                requested_model=model_manager.current_model,
                owner=client_id,
            )
            async with _model_switch_lock:
                if model_manager.is_unloaded:
                    await _run_guardian_operation(
                        source="proxy",
                        phase="auto_reload",
                        target_model=model_manager.current_model,
                        requested_model=model_manager.current_model,
                        owner=client_id,
                        operation=model_manager.load,
                        generation=generation,
                    )

        # Model switch (safe — we hold the queue slot)
        current_model = await model_manager.get_current_model()
        if model != current_model and model in model_manager.models:
            if not model_manager.is_switch_allowed(client_id):
                logger.warning(f"🔒 Client '{client_id}' not in switch_allowlist, blocked switch to '{model}'")
            else:
                generation = _reset_startup_check_status(
                    source="proxy",
                    phase="auto_switch",
                    target_model=model,
                    requested_model=body.get("model"),
                    owner=client_id,
                )
                async with _model_switch_lock:
                    current_model = await model_manager.get_current_model()
                    if model != current_model:
                        try:
                            await _run_guardian_operation(
                                source="proxy",
                                phase="auto_switch",
                                target_model=model,
                                requested_model=body.get("model"),
                                owner=client_id,
                                operation=lambda: model_manager.switch_model(model, client_id=client_id),
                                generation=generation,
                            )
                        except ModelLoadError as e:
                            crash = e.crash_record
                            raise HTTPException(status_code=503, detail={
                                "error": f"Model '{model}' failed to load",
                                "message": str(e),
                                "crash_details": crash.to_dict() if crash else None,
                            })
                        except ValueError as e:
                            logger.warning(f"🔒 Switch denied: {e}")
                        except Exception as e:
                            raise HTTPException(status_code=500, detail=f"Model switch failed: {e}")

        model_manager.last_request_time = time.time()
        model_manager.active_requests += 1

        # Translate to OpenAI
        messages = body.get("messages", [{"role": "user", "content": prompt}])
        stream = body.get("stream", True)
        options = body.get("options", {})
        temperature = options.get("temperature", 0.7)
        
        openai_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature
        }

        timeout_sec = get_model_timeout(model)
        client = httpx.AsyncClient(timeout=timeout_sec)
        
        req = client.build_request(
            "POST",
            f"{LLAMA_SERVER_URL}/v1/chat/completions",
            json=openai_body,
            timeout=timeout_sec
        )

        try:
            r = await client.send(req, stream=stream)
        except Exception as e:
            await client.aclose()
            raise e

        if stream:
            async def stream_adapter_generate():
                try:
                    async for chunk in r.aiter_lines():
                        if not chunk or chunk.strip() == "data: [DONE]": 
                            continue
                        if chunk.startswith("data: "):
                            try:
                                data = json.loads(chunk[6:])
                                if "choices" in data and len(data["choices"]) > 0:
                                    delta = data["choices"][0].get("delta", {})
                                    content = _extract_assistant_delta_text(delta)
                                    if content:
                                        # /api/generate response format: { "response": "..." }
                                        ollama_chunk = {
                                            "model": model,
                                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                            "response": content,
                                            "done": False
                                        }
                                        yield json.dumps(ollama_chunk) + "\n"
                            except:
                                pass
                    yield json.dumps({
                        "model": model, 
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), 
                        "done": True,
                        "response": "",
                        "total_duration": 0,
                        "load_duration": 0,
                        "prompt_eval_count": 0,
                        "eval_count": 0
                    }) + "\n"
                finally:
                    await r.aclose()
                    await client.aclose()
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.release(request_id)

            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            response = StreamingResponse(
                stream_adapter_generate(),
                media_type="application/x-ndjson",
                headers={"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
            )
            _release_in_finally = False
            return response
        else:
            try:
                data = r.json()
                content = _extract_assistant_message_text(data["choices"][0]["message"])
                ollama_resp = {
                    "model": model,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "response": content,
                    "done": True,
                    "context": [],
                    "total_duration": 0,
                    "load_duration": 0,
                    "prompt_eval_count": data.get("usage", {}).get("prompt_tokens", 0),
                    "eval_count": data.get("usage", {}).get("completion_tokens", 0)
                }
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                return ollama_resp
            except Exception as e:
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
    finally:
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.release(request_id)


@app.get("/api/version")
async def get_version(client_id: str = Depends(verify_api_key)):
    """Mimic Ollama version endpoint"""
    return {"version": "0.1.27"}

@app.get("/api/tags")
async def proxy_tags_ollama(client_id: str = Depends(verify_api_key)):
    """Simulate Ollama /api/tags endpoint"""
    import traceback
    models = []
    try:
        # Get models from our manager config
        if not hasattr(model_manager, 'models') or model_manager.models is None:
            logger.error("model_manager.models is missing or None")
            return {"models": []}
            
        for name in model_manager.models.keys():
            models.append({
                "name": name,
                "model": name,
                "modified_at": "2024-01-01T00:00:00.0000000+00:00",
                "size": get_model_size(name) * 1024 * 1024,
                "digest": "000000000000",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "llama",
                    "families": ["llama"],
                    "parameter_size": "7B",
                    "quantization_level": "Q4_0"
                }
            })
    except Exception as e:
        logger.error(f"Error in proxy_tags_ollama: {e}")
        traceback.print_exc()
        # Return empty list instead of crashing
        pass
    return {"models": models}


# Public liveness probe — no auth, no info leak.
# Used by external monitoring (monifuse, uptime checks). Returns 200 if Guardian
# proxy process is up; does NOT reflect llama-server backend health.
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# Model listing endpoint (Before catch-all)
@app.get("/v1/models")
async def list_models(client_id: str = Depends(verify_api_key)):
    """List available models from config."""
    models_list = []
    try:
        current = await model_manager.get_current_model()
        for public_name, canonical_name in model_manager.get_public_model_map().items():
            model_entry = {
                "id": public_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "organization-owner",
                "permission": [],
            }
            benchmark_context_limit = model_manager.get_benchmark_context_limit(canonical_name)
            runtime_context = model_manager.get_runtime_context_window(canonical_name)
            advertised_context = model_manager.get_advertised_context_window(canonical_name)
            if benchmark_context_limit is not None:
                model_entry["max_context"] = benchmark_context_limit
                model_entry["benchmark_context_limit"] = benchmark_context_limit
            if runtime_context is not None:
                model_entry["context"] = runtime_context
            if advertised_context is not None:
                model_entry["advertised_context"] = advertised_context

            vision = model_manager.get_vision_capability(canonical_name)
            model_entry["input_modalities"] = ["text"]
            if vision["status"] == "supported":
                model_entry["input_modalities"].append("image")
            model_entry["configured_input_modalities"] = ["text"]
            if vision["configured"]:
                model_entry["configured_input_modalities"].append("image")
            model_entry["vision"] = {
                "configured": vision["configured"],
                "status": vision["status"],
                "validated": vision["validated"],
            }

            # Claude Code currently compacts against the OpenAI-compatible
            # max_context field only. Preserve benchmark-cap semantics for
            # normal clients, but return the safer advertised window for Claude
            # so it compacts before hard overflow.
            if client_id == "claudecode" and advertised_context is not None:
                if benchmark_context_limit is not None:
                    model_entry["benchmark_context_limit"] = benchmark_context_limit
                model_entry["max_context"] = advertised_context
            models_list.append(model_entry)
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        
    return {"object": "list", "data": models_list}


# --- Crash history & status endpoints ---

@app.post("/admin/unload")
async def admin_unload(client_id: str = Depends(verify_api_key)):
    """Stop llama-server immediately to free all VRAM (e.g. before running ComfyUI)."""
    if model_manager.is_unloaded:
        return {"status": "already_unloaded", "message": "llama-server is already stopped"}
    await model_manager.unload()
    return {"status": "unloaded", "message": f"Model '{model_manager.current_model}' unloaded — VRAM is free"}


@app.post("/admin/load")
async def admin_load(request: Request, client_id: str = Depends(verify_api_key)):
    """Reload llama-server. Optionally pass {\"model\": \"name\"} to load a specific model."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    target = body.get("model", None)
    if target:
        try:
            target = model_manager.resolve_model(target)
        except ValueError:
            pass
    enable_vision = body.get("enable_vision")
    runtime_overrides = body.get("runtime_overrides")
    if runtime_overrides is not None and not isinstance(runtime_overrides, dict):
        raise HTTPException(status_code=400, detail="runtime_overrides must be an object")
    generation = _reset_startup_check_status(
        source="admin",
        phase="manual_load",
        target_model=target or model_manager.current_model,
        requested_model=body.get("model"),
        owner=client_id,
    )
    model_manager.last_request_time = time.time()
    model_manager.active_requests += 1
    try:
        async with _model_switch_lock:
            await _run_guardian_operation(
                source="admin",
                phase="manual_load",
                target_model=target or model_manager.current_model,
                requested_model=body.get("model"),
                owner=client_id,
                operation=lambda: model_manager.load(
                    target,
                    enable_vision=enable_vision,
                    runtime_overrides=runtime_overrides,
                ),
                generation=generation,
            )
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    finally:
        model_manager.active_requests = max(0, model_manager.active_requests - 1)
        model_manager.last_request_time = time.time()
    return {"status": "loaded", "model": model_manager.current_model}


@app.get("/api/crashes")
async def get_crash_history(client_id: str = Depends(verify_api_key)):
    """Return the crash history of llama-server load failures."""
    return {
        "total_crashes": len(model_manager.crash_history),
        "last_crash": model_manager.last_crash.to_dict() if model_manager.last_crash else None,
        "history": model_manager.get_crash_history(),
    }


@app.get("/api/status")
async def get_server_status(client_id: str = Depends(verify_api_key)):
    """Return current model status and backend health."""
    current_model = await model_manager.get_current_model()
    startup_status = _get_startup_check_status()
    queue_status = inference_queue.get_status()
    switch_in_progress = _startup_state_is_in_progress(startup_status.get("state")) and startup_status.get("phase") != "idle"
    current_requested_target = startup_status.get("target_model") if switch_in_progress else None
    active_switch_owner = startup_status.get("owner") if switch_in_progress else None
    healthy = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LLAMA_SERVER_URL}/health")
            healthy = resp.status_code == 200
    except Exception:
        pass

    preferred_tool_model = model_manager.get_preferred_tool_model(current_model)
    preferred_reasoning_model = model_manager.get_preferred_reasoning_model(current_model)
    backend_model_path = model_manager._get_backend_model_path()
    backend_model_name = model_manager._last_backend_model
    if backend_model_name is None and backend_model_path:
        backend_model_name = model_manager._identify_model_by_path(backend_model_path)
    vram = get_gpu_metrics()
    idle_minutes = model_manager.idle_unload_minutes
    idle_secs = time.time() - model_manager.last_request_time
    return {
        "current_model": current_model,
        "backend_healthy": healthy,
        "is_unloaded": model_manager.is_unloaded,
        "idle_seconds": round(idle_secs),
        "idle_unload_minutes": idle_minutes,
        "backend_url": LLAMA_SERVER_URL,
        "total_crashes": len(model_manager.crash_history),
        "last_crash": model_manager.last_crash.to_dict() if model_manager.last_crash else None,
        "vram": vram,
        "vram_model_mb": get_model_size(current_model),
        "security": {
            "pinned_model": model_manager.pinned_model,
            "switch_allowlist": list(model_manager._switch_allowlist) if model_manager._switch_allowlist else None,
            "backend_verified": model_manager._model_verified,
            "last_backend_verification_at": model_manager._last_verification_at,
            "last_successful_backend_verification_at": model_manager._last_successful_verification_at,
            "last_verified_model": model_manager._last_verified_model,
            "backend_model": backend_model_name,
            "backend_model_path": backend_model_path,
        },
        "startup": startup_status,
        "current_requested_target": current_requested_target,
        "switch": {
            "active": switch_in_progress,
            "state": startup_status.get("state"),
            "phase": startup_status.get("phase"),
            "owner": active_switch_owner,
            "requested_target": current_requested_target,
            "requested_model": startup_status.get("requested_model"),
            "lock_held": _model_switch_lock.locked(),
        },
        "queue": queue_status,
        "routing": {
            "tool_model": preferred_tool_model,
            "reasoning_model": preferred_reasoning_model,
            "auto_behavior": "tool_friendly_same_weights_if_available",
        },
        "proxy": {
            "pid": os.getpid(),
            "port": PROXY_PORT,
            "listener": _get_proxy_listener_info(),
            "pid_file": _get_pid_file_status(),
        },
        "scaler": {
            "enabled": state.scaler.config.get("enabled", False),
            "profiles": list(state.scaler.config.get("profiles", {}).keys()),
        },
    }


# --- Prometheus metrics endpoint (no auth — standard for scraping) ---

@app.get("/metrics")
async def prometheus_metrics():
    """Expose Prometheus-compatible metrics for Grafana/alerting.

    No auth required — standard Prometheus convention for scrape targets.
    """
    update_queue_metrics(inference_queue)
    update_gpu_metrics()
    update_system_metrics(model_manager)
    body, content_type = get_metrics_output()
    return Response(content=body, media_type=content_type)


# --- Scaler configuration endpoints ---

@app.get("/api/scaler")
async def get_scaler_config(client_id: str = Depends(verify_api_key)):
    """Return current scaler configuration."""
    return state.scaler.get_config()


@app.put("/api/scaler")
async def update_scaler_config(request: Request, client_id: str = Depends(verify_api_key)):
    """Update scaler configuration (partial merge).

    Body examples::

        {"enabled": false}
        {"profiles": {"trivial": {"thinking_budget": 512}}}
        {"queue_pressure": {"heavy_threshold": 6}}
    """
    patch = await request.json()
    persist = patch.pop("_persist", True)
    updated = state.scaler.update_config(patch, persist=persist)
    return {"status": "updated", "config": updated}


@app.post("/api/scaler/reset")
async def reset_scaler_config(client_id: str = Depends(verify_api_key)):
    """Reset scaler configuration to built-in defaults."""
    config = state.scaler.reset_config()
    return {"status": "reset", "config": config}


@app.post("/api/scaler/recommend")
async def scaler_recommend(request: Request, client_id: str = Depends(verify_api_key)):
    """Return recommended thinking_budget_tokens and max_tokens for a request.

    Advisory only — the client decides whether to use these values.

    Body: same shape as a chat/completions request (needs ``messages``).
    Returns recommended values and the classification details.
    """
    body = await request.json()
    messages = body.get("messages", [])

    # Classify complexity
    profile_name, complexity = state.scaler._classify_complexity(messages)
    profile = state.scaler.config["profiles"].get(profile_name, {})

    base_thinking = profile.get("thinking_budget", -1)
    base_max_tokens = profile.get("max_tokens", 8192)

    # Apply queue pressure
    thinking_budget, max_tokens = state.scaler._apply_queue_pressure(
        base_thinking, base_max_tokens, inference_queue.waiting_count
    )
    pressure = state.scaler._pressure_label(inference_queue.waiting_count)

    if state.scaler.config.get("log_decisions"):
        logger.info(
            f"📋 [{client_id}] Scaler recommend: profile={profile_name} "
            f"pressure={pressure} → thinking_budget={thinking_budget}, max_tokens={max_tokens}"
        )

    return {
        "profile": profile_name,
        "complexity": complexity,
        "pressure": pressure,
        "recommended": {
            "thinking_budget_tokens": thinking_budget,
            "max_tokens": max_tokens,
        },
    }


# --- Queue status endpoint (non-queued, always immediately available) ---

@app.get("/v1/queue/status")
async def queue_status(client_id: str = Depends(verify_api_key)):
    """Return current queue status.  Clients should poll this while waiting."""
    return inference_queue.get_status(client_id=client_id)


# OpenAI-compatible /v1/ routes (used by OpenClaw and other OpenAI-compatible clients)
@app.get("/v1/{path:path}")
async def proxy_v1_get(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{LLAMA_SERVER_URL}/v1/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/v1/{path:path}")
async def proxy_v1_post(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    body = await request.body()

    # Only queue inference endpoints; everything else passes through directly
    is_inference = path in ("chat/completions", "completions", "embeddings", "messages")

    if not is_inference:
        timeout = httpx.Timeout(600.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{LLAMA_SERVER_URL}/v1/{path}",
                content=body,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
            )
            return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

    # --- Inference path: acquire queue slot ---
    # Determine requested model for queue tracking
    requested_model = "_unknown"
    try:
        json_body = json.loads(body)
        requested_model = json_body.get("model", requested_model)
    except (json.JSONDecodeError, Exception):
        pass

    try:
        request_id = await inference_queue.acquire(client_id, requested_model)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "queue_timeout", "message": f"Waited {inference_queue.queue_timeout}s in queue"},
        )

    _release_in_finally = True
    try:
        json_body: Optional[Dict[str, Any]] = None
        has_image_inputs = False
        requested_model: Optional[str] = None
        try:
            json_body = json.loads(body)
            if path in ("chat/completions", "messages"):
                has_image_inputs = _messages_contain_image_input(json_body.get("messages", []))
        except (json.JSONDecodeError, Exception):
            json_body = None

        # If llama-server was unloaded, auto-reload before forwarding
        if model_manager.is_unloaded:
            logger.info(f"🔄 Incoming request while unloaded — auto-reloading '{model_manager.current_model}'...")
            try:
                generation = _reset_startup_check_status(
                    source="proxy",
                    phase="auto_reload",
                    target_model=model_manager.current_model,
                    requested_model=model_manager.current_model,
                    owner=client_id,
                )
                async with _model_switch_lock:
                    if model_manager.is_unloaded:  # double-check under lock
                        await _run_guardian_operation(
                            source="proxy",
                            phase="auto_reload",
                            target_model=model_manager.current_model,
                            requested_model=model_manager.current_model,
                            owner=client_id,
                            operation=lambda: model_manager.load(
                                model_manager.current_model,
                                enable_vision=_desired_runtime_vision_enabled(
                                    model_manager.current_model,
                                    has_image_inputs,
                                ),
                            ),
                            generation=generation,
                        )
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Auto-reload failed: {e}")

        # Track last request time for idle-unload
        model_manager.last_request_time = time.time()
        model_manager.active_requests += 1

        # Auto-switch logic for chat completions & messages (with concurrency lock)
        if path in ("chat/completions", "messages"):
            try:
                if json_body is None:
                    json_body = json.loads(body)
                requested_model = json_body.get("model")

                # Resolve aliases and case-insensitive names
                current_model = await model_manager.get_current_model()
                if requested_model:
                    requested_model = _resolve_inference_model(requested_model, current_model)
                
                desired_model = requested_model or current_model
                if desired_model in model_manager.models:
                    desired_vision = _desired_runtime_vision_enabled(desired_model, has_image_inputs)
                    current_vision = model_manager.current_runtime_uses_mmproj(current_model)
                    needs_model_switch = desired_model != current_model
                    needs_runtime_reload = desired_model == current_model and desired_vision != current_vision

                    if needs_model_switch and not model_manager.is_switch_allowed(client_id):
                        logger.warning(
                            f"🔒 Client '{client_id}' not in switch_allowlist, blocked switch to '{desired_model}'. Forwarding to current model."
                        )
                    elif needs_model_switch or needs_runtime_reload:
                        phase = "auto_switch" if needs_model_switch else "runtime_reload"
                        generation = _reset_startup_check_status(
                            source="proxy",
                            phase=phase,
                            target_model=desired_model,
                            requested_model=json_body.get("model"),
                            owner=client_id,
                        )
                        async with _model_switch_lock:
                            current_model = await model_manager.get_current_model()
                            desired_model = requested_model or current_model
                            desired_vision = _desired_runtime_vision_enabled(desired_model, has_image_inputs)
                            current_vision = model_manager.current_runtime_uses_mmproj(current_model)
                            needs_model_switch = desired_model != current_model
                            needs_runtime_reload = desired_model == current_model and desired_vision != current_vision

                            if needs_model_switch or needs_runtime_reload:
                                logger.info(
                                    "🔄 Adjusting backend from %s [%s] to %s [%s] (client: %s)",
                                    current_model,
                                    "vision" if current_vision else "text",
                                    desired_model,
                                    "vision" if desired_vision else "text",
                                    client_id,
                                )
                                try:
                                    operation = (
                                        (lambda: model_manager.switch_model(
                                            desired_model,
                                            client_id=client_id,
                                            enable_vision=desired_vision,
                                        ))
                                        if needs_model_switch
                                        else (lambda: model_manager.load(
                                            desired_model,
                                            enable_vision=desired_vision,
                                        ))
                                    )
                                    await _run_guardian_operation(
                                        source="proxy",
                                        phase=phase,
                                        target_model=desired_model,
                                        requested_model=json_body.get("model"),
                                        owner=client_id,
                                        operation=operation,
                                        generation=generation,
                                    )
                                except ModelLoadError as e:
                                    if has_image_inputs and desired_model:
                                        model_manager.mark_vision_validation(desired_model, "load_failed", str(e))
                                    crash = e.crash_record
                                    detail = {
                                        "error": f"Model '{desired_model}' failed to load",
                                        "message": str(e),
                                        "crash_details": crash.to_dict() if crash else None,
                                    }
                                    logger.error(f"💥 Model load crash: {detail}")
                                    raise HTTPException(status_code=503, detail=detail)
                                except ValueError as e:
                                    logger.warning(f"🔒 Switch denied: {e}")
                                except Exception as e:
                                    logger.error(f"❌ Switch failed: {e}")
                                    raise HTTPException(status_code=500, detail="Model switch failed")
                elif requested_model:
                    logger.warning(f"⚠️ Requested model {requested_model} not managed by Guardian. Forwarding to current.")
            except json.JSONDecodeError:
                pass
            except HTTPException:
                raise  # Let model-load errors propagate to the client
            except Exception as e:
                logger.error(f"Error checking model switch: {e}")

        active_model_for_request = requested_model or await model_manager.get_current_model()
        if path in ("chat/completions", "messages") and has_image_inputs:
            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            preflight_error = await _preflight_multimodal_request(
                active_model_for_request,
                request_id,
                queue_wait_ms,
            )
            if preflight_error is not None:
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                return preflight_error

        timeout = httpx.Timeout(600.0, connect=10.0)
        logger.info(f"OpenAI-compat request from client '{client_id}': POST /v1/{path}")

        # Detect streaming requests for chat/completions and messages — must proxy SSE in real-time
        is_stream = False
        if path in ("chat/completions", "messages"):
            try:
                json_body = json.loads(body)
                is_stream = json_body.get("stream", False)
                # WORKAROUND: llama.cpp "Assistant response prefill is incompatible with enable_thinking"
                msgs = json_body.get("messages", [])
                
                # Consolidate ALL trailing assistant messages
                trailing_assistant_contents = []
                while len(msgs) > 0 and msgs[-1].get("role") == "assistant":
                    popped = msgs.pop()
                    content = popped.get("content", "")
                    if content:
                        trailing_assistant_contents.insert(0, str(content))
                        
                if trailing_assistant_contents and len(msgs) >= 1:
                    combined_prefill = "\\n".join(trailing_assistant_contents)
                    
                    # Find the last user message and append the prefill instruction
                    last_user_idx = -1
                    for i in range(len(msgs)-1, -1, -1):
                        if msgs[i].get("role") == "user":
                            last_user_idx = i
                            break
                            
                    if last_user_idx != -1:
                        msgs[last_user_idx]["content"] = str(msgs[last_user_idx].get("content", "")) + f"\n\n[System directive: Please start your response exactly with the following text: {combined_prefill}]"
                        json_body["messages"] = msgs
                        body = json.dumps(json_body).encode("utf-8")
                    else:
                        import logging
                        logging.getLogger("uvicorn.error").warning("Found trailing assistant messages but no user message to attach to.")
            except (json.JSONDecodeError, Exception):
                pass

        if is_stream:
            # Stream SSE chunks in real-time instead of buffering entire response
            client = httpx.AsyncClient(timeout=timeout)
            req = client.build_request(
                "POST",
                f"{LLAMA_SERVER_URL}/v1/{path}",
                content=body,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            )
            try:
                resp = await client.send(req, stream=True)
            except httpx.ConnectError as e:
                await client.aclose()
                await _reload_backend_after_connect_error(path, e)

                client = httpx.AsyncClient(timeout=timeout)
                req = client.build_request(
                    "POST",
                    f"{LLAMA_SERVER_URL}/v1/{path}",
                    content=body,
                    headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                )
                try:
                    resp = await client.send(req, stream=True)
                except Exception as retry_error:
                    await client.aclose()
                    raise HTTPException(status_code=502, detail=f"Backend request failed after reload: {retry_error}")
            except Exception as e:
                await client.aclose()
                raise HTTPException(status_code=502, detail=f"Backend request failed: {e}")

            if has_image_inputs:
                queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
                if 200 <= resp.status_code < 400:
                    model_manager.mark_vision_validation(active_model_for_request, "supported")
                else:
                    body_bytes = await resp.aread()
                    headers = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "content-length")
                    }
                    await resp.aclose()
                    await client.aclose()
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    mapped = _map_multimodal_backend_error(
                        active_model_for_request,
                        resp.status_code,
                        body_bytes,
                        request_id,
                        queue_wait_ms,
                    )
                    if mapped is not None:
                        return mapped
                    return Response(
                        content=body_bytes,
                        status_code=resp.status_code,
                        headers=headers | _queue_headers(request_id, queue_wait_ms),
                    )

            async def stream_passthrough():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.release(request_id)

            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            response = StreamingResponse(
                stream_passthrough(),
                status_code=resp.status_code,
                media_type="text/event-stream",
                headers={
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-length")
                } | {"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
            )
            _release_in_finally = False
            return response
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    resp = await client.post(
                        f"{LLAMA_SERVER_URL}/v1/{path}",
                        content=body,
                        headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
                    )
                except httpx.ConnectError as e:
                    await _reload_backend_after_connect_error(path, e)
                    try:
                        resp = await client.post(
                            f"{LLAMA_SERVER_URL}/v1/{path}",
                            content=body,
                            headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
                        )
                    except Exception as retry_error:
                        raise HTTPException(status_code=502, detail=f"Backend request failed after reload: {retry_error}")
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
                if has_image_inputs:
                    if 200 <= resp.status_code < 400:
                        model_manager.mark_vision_validation(active_model_for_request, "supported")
                    else:
                        mapped = _map_multimodal_backend_error(
                            active_model_for_request,
                            resp.status_code,
                            resp.content,
                            request_id,
                            queue_wait_ms,
                        )
                        if mapped is not None:
                            return mapped
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=dict(resp.headers) | _queue_headers(request_id, queue_wait_ms),
                )
    finally:
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.release(request_id)

async def start_proxy():
    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@app.post("/api/session/save")
async def save_session(request: Request, client_id: str = Depends(verify_api_key)):
    logger.info(f"💾 Session SAVE request from {client_id}")
    try:
        data = await request.json()
        filename = data.get("filename")
        if not filename:
            raise HTTPException(status_code=400, detail="Filename required")
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{LLAMA_SERVER_URL}/slots/0?action=save",
                json={"filename": filename},
                timeout=60.0
            )  
            if resp.status_code != 200:
                logger.error(f"Llama save failed: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Llama save failed: {resp.text}")
                
            return resp.json()
    except Exception as e:
        logger.error(f"Save session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/session/load")
async def load_session(request: Request, client_id: str = Depends(verify_api_key)):
    logger.info(f"📂 Session LOAD request from {client_id}")
    try:
        data = await request.json()
        filename = data.get("filename")
        if not filename:
            raise HTTPException(status_code=400, detail="Filename required")
            
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{LLAMA_SERVER_URL}/slots/0?action=restore",
                json={"filename": filename},
                timeout=60.0 # Loading takes time
            )
            if resp.status_code != 200:
                logger.error(f"Llama load failed: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Llama load failed: {resp.text}")
                
            return resp.json()
    except Exception as e:
        logger.error(f"Load session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/session/list")
async def list_sessions(client_id: str = Depends(verify_api_key)):
    logger.debug(f"📜 Session LIST request from {client_id}")
    try:
        save_path = Path("/home/flip/llama_slots") 
        if not save_path.exists():
            return {"sessions": []}
            
        files = [f.stem for f in save_path.glob("*.bin")]
        return {"sessions": sorted(files)}
    except Exception as e:
        logger.error(f"List sessions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import asyncio
    asyncio.run(start_proxy())
