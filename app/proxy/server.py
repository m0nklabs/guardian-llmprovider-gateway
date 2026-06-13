import os
import base64
import json
import asyncio
import logging
import re
import signal
import subprocess
import time
import uuid
import errno
import struct
import zlib
from dataclasses import dataclass, field
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.status import HTTP_401_UNAUTHORIZED

from collections import defaultdict
from app.proxy.optimizer import RequestOptimizer
from app.proxy.scaler import DynamicScaler
from app.engine.manager import ModelManager, ModelLoadError
from app.proxy.auth import build_request_auth_context, get_request_auth_context, set_request_auth_context, verify_api_key
from app.proxy.queue import InferenceQueue, QueueAdmissionRejected, QueueRequestCancelled
from app.proxy.usage import ApiUsageTracker
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
        "proxy": {
            "stream_heartbeat_seconds": 15,
            "stream_close_timeout_seconds": 5,
        },
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
            if "proxy" in file_config:
                default_config["proxy"].update(file_config["proxy"])
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


def _load_stream_heartbeat_interval_s() -> Optional[float]:
    """Return the configured SSE heartbeat interval, or None when disabled."""
    try:
        interval = float(CONFIG.get("proxy", {}).get("stream_heartbeat_seconds", 15))
    except (TypeError, ValueError):
        interval = 15.0
    return interval if interval > 0 else None


STREAM_HEARTBEAT_INTERVAL_S = _load_stream_heartbeat_interval_s()


def _load_stream_close_timeout_s() -> float:
    """Return the bounded timeout used for upstream stream cleanup."""
    try:
        timeout = float(CONFIG.get("proxy", {}).get("stream_close_timeout_seconds", 5))
    except (TypeError, ValueError):
        timeout = 5.0
    return max(timeout, 0.5)


STREAM_CLOSE_TIMEOUT_S = _load_stream_close_timeout_s()

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


STREAM_TIMEOUT_EXTENSION_STEPS = (
    (64, 2.0),
    (16, 1.5),
)
STREAM_LOOP_REPEAT_THRESHOLD = 12


def _normalize_stream_progress_text(text: object) -> str:
    """Normalize streamed content for lightweight loop detection."""
    if not isinstance(text, str):
        return ""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if len(normalized) < 2:
        return ""
    return normalized[:120]


def _extract_stream_progress_text(line: str) -> str:
    """Extract the assistant delta text from an OpenAI-compatible SSE line."""
    if not isinstance(line, str) or not line.startswith("data: "):
        return ""

    payload = line[6:].strip()
    if not payload or payload == "[DONE]":
        return ""

    try:
        data = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""

    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = first_choice.get("delta")
        if isinstance(delta, dict):
            text = _extract_assistant_delta_text(delta)
            if text:
                return text
        message = first_choice.get("message")
        if isinstance(message, dict):
            text = _extract_assistant_message_text(message)
            if text:
                return text

    response_text = data.get("response")
    return response_text if isinstance(response_text, str) else ""


@dataclass
class StreamProgressWatchdog:
    """Bound streaming stall time while rewarding healthy non-looping output."""

    base_timeout_s: float
    current_timeout_s: float = field(init=False)
    healthy_chunk_count: int = 0
    repeated_chunk_count: int = 0
    last_chunk: str = ""
    loop_detected: bool = False

    def __post_init__(self) -> None:
        self.base_timeout_s = max(float(self.base_timeout_s), 1.0)
        self.current_timeout_s = self.base_timeout_s

    def observe_sse_line(self, line: str) -> None:
        """Grow the stall timeout only when the stream keeps making novel progress."""
        normalized = _normalize_stream_progress_text(_extract_stream_progress_text(line))
        if not normalized:
            return

        if normalized == self.last_chunk:
            self.repeated_chunk_count += 1
            if self.repeated_chunk_count >= STREAM_LOOP_REPEAT_THRESHOLD:
                self.loop_detected = True
            return

        self.last_chunk = normalized
        self.repeated_chunk_count = 1
        self.loop_detected = False
        self.healthy_chunk_count += 1

        multiplier = 1.0
        for minimum_chunks, candidate_multiplier in STREAM_TIMEOUT_EXTENSION_STEPS:
            if self.healthy_chunk_count >= minimum_chunks:
                multiplier = candidate_multiplier
                break

        self.current_timeout_s = self.base_timeout_s * multiplier


def _build_stream_timeout(base_timeout_s: float) -> httpx.Timeout:
    """Allow streaming reads to run under Guardian's own watchdog instead of a fixed read timeout."""
    base_timeout_s = max(float(base_timeout_s), 1.0)
    return httpx.Timeout(connect=10.0, read=None, write=base_timeout_s, pool=base_timeout_s)


async def _pump_sse_lines(
    iterator: AsyncIterator[str],
    queue: "asyncio.Queue[tuple[str, Optional[Any]]]",
) -> None:
    """Read upstream SSE lines without cancelling the underlying iterator during keepalive gaps."""
    try:
        async for line in iterator:
            await queue.put(("line", line))
    except Exception as exc:
        await queue.put(("error", exc))
    else:
        await queue.put(("eof", None))


def _build_sse_keepalive_comment(request_id: Optional[str] = None) -> str:
    """Emit a lightweight SSE comment to keep downstream clients from idling out."""
    suffix = f" request_id={request_id}" if request_id else ""
    return f": guardian-keepalive{suffix}"


async def _iter_sse_lines_with_watchdog(
    response: httpx.Response,
    watchdog: StreamProgressWatchdog,
    *,
    request_id: Optional[str] = None,
    route: Optional[str] = None,
    client_id: Optional[str] = None,
    model_name: Optional[str] = None,
    heartbeat_interval_s: Optional[float] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[str]:
    """Yield SSE lines while enforcing a dynamic stall timeout and optional downstream keepalives."""
    queue: "asyncio.Queue[tuple[str, Optional[Any]]]" = asyncio.Queue()
    pump_task = asyncio.create_task(_pump_sse_lines(response.aiter_lines(), queue))
    last_data_at = time.monotonic()

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
                if request_id:
                    snapshot = inference_queue.get_request_status(request_id)
                    reason = (snapshot or {}).get("cancel_reason") or reason
                raise _GuardianRequestCancelled(request_id or "unknown", reason)

            timeout_exc: Optional[asyncio.TimeoutError] = None
            elapsed_without_data_s = time.monotonic() - last_data_at
            remaining_timeout_s = watchdog.current_timeout_s - elapsed_without_data_s
            if remaining_timeout_s <= 0:
                timeout_exc = asyncio.TimeoutError()
                elapsed_without_data_s = max(elapsed_without_data_s, watchdog.current_timeout_s)
            else:
                wait_timeout_s = remaining_timeout_s
                if heartbeat_interval_s is not None:
                    wait_timeout_s = min(wait_timeout_s, heartbeat_interval_s)
                try:
                    if cancel_event is None:
                        event_type, payload = await asyncio.wait_for(queue.get(), timeout=wait_timeout_s)
                    else:
                        queue_task = asyncio.create_task(queue.get())
                        cancel_task = asyncio.create_task(cancel_event.wait())
                        try:
                            done, pending = await asyncio.wait(
                                {queue_task, cancel_task},
                                timeout=wait_timeout_s,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for pending_task in pending:
                                pending_task.cancel()
                            for pending_task in pending:
                                with suppress(asyncio.CancelledError):
                                    await pending_task
                            if not done:
                                raise asyncio.TimeoutError()
                            if cancel_task in done and cancel_event.is_set():
                                reason = "cancelled"
                                if request_id:
                                    snapshot = inference_queue.get_request_status(request_id)
                                    reason = (snapshot or {}).get("cancel_reason") or reason
                                raise _GuardianRequestCancelled(request_id or "unknown", reason)
                            event_type, payload = queue_task.result()
                        finally:
                            if not queue_task.done():
                                queue_task.cancel()
                            if not cancel_task.done():
                                cancel_task.cancel()
                except asyncio.TimeoutError as exc:
                    timeout_exc = exc
                    elapsed_without_data_s = time.monotonic() - last_data_at
                    remaining_timeout_s = watchdog.current_timeout_s - elapsed_without_data_s
                    if heartbeat_interval_s is not None and remaining_timeout_s > 0:
                        yield _build_sse_keepalive_comment(request_id)
                        yield ""
                        continue
                else:
                    if event_type == "eof":
                        return
                    if event_type == "error":
                        error = payload
                        if isinstance(error, Exception):
                            raise error
                        raise RuntimeError(f"Unexpected SSE pump error payload: {error!r}")

                    line = str(payload or "")
                    last_data_at = time.monotonic()
                    watchdog.observe_sse_line(line)
                    yield line
                    continue

            context_parts = []
            if request_id:
                context_parts.append(f"request_id={request_id}")
            if route:
                context_parts.append(f"route={route}")
            if client_id:
                context_parts.append(f"client={client_id}")
            if model_name:
                context_parts.append(f"model={model_name}")
            context_suffix = f" [{' '.join(context_parts)}]" if context_parts else ""
            message = (
                f"Guardian stream stalled after {watchdog.current_timeout_s:.0f}s without new SSE data "
                f"(healthy_chunks={watchdog.healthy_chunk_count}, loop_detected={watchdog.loop_detected}, "
                f"silence_s={elapsed_without_data_s:.1f})"
                f"{context_suffix}"
            )
            logger.warning(message)
            if timeout_exc is None:
                raise httpx.ReadTimeout(message, request=response.request)
            raise httpx.ReadTimeout(message, request=response.request) from timeout_exc
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task


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
        if preferred and preferred != "__MISMATCH__":
            return preferred
        return model_manager.resolve_reload_target(current_model)
    try:
        return model_manager.resolve_model(raw_model)
    except ValueError:
        return raw_model


def _reject_unserved_inference_model(raw_model: Optional[str]) -> None:
    """Raise a client-facing error for a model Guardian does not serve."""
    requested_model = str(raw_model or "").strip() or "(missing)"
    raise HTTPException(
        status_code=404,
        detail={
            "error": "model_not_served",
            "reason": "requested_model_not_served",
            "message": f"Model '{requested_model}' is not configured in Guardian and cannot be served.",
            "requested_model": requested_model,
            "hint": "Use /v1/models to discover the models currently served by Guardian.",
        },
    )


def _resolve_or_reject_inference_model(raw_model: Optional[str], current_model: str) -> str:
    """Resolve an inference model name and reject unknown or unserved values."""
    resolved_model = _resolve_inference_model(raw_model, current_model)
    if not resolved_model or resolved_model == "__MISMATCH__":
        _reject_unserved_inference_model(raw_model)
    if resolved_model not in model_manager.models:
        _reject_unserved_inference_model(raw_model)
    return resolved_model


def _resolve_auto_reload_model(requested_model: Optional[str] = None) -> str:
    """Resolve the model Guardian should load when the backend is absent."""
    return model_manager.resolve_reload_target(requested_model)


def _queue_headers(request_id: str, queue_wait_ms: float) -> Dict[str, str]:
    return {
        "X-Request-Id": request_id,
        "X-Queue-Wait-Ms": str(int(queue_wait_ms)),
    }


class _GuardianRequestCancelled(Exception):
    """Raised when Guardian cancels or abandons a tracked request lifecycle."""

    def __init__(self, request_id: str, reason: str = "cancelled"):
        super().__init__(f"request {request_id} cancelled: {reason}")
        self.request_id = request_id
        self.reason = reason


def _request_cancel_http_exception(request_id: str, reason: str) -> HTTPException:
    """Translate internal request cancellation into a client-facing HTTP error."""
    return HTTPException(
        status_code=499,
        detail={
            "error": "request_cancelled",
            "request_id": request_id,
            "message": reason,
        },
    )


async def _stop_background_task(task: Optional[asyncio.Task]) -> None:
    """Cancel and await a background task without leaking cancellation noise."""
    if task is None:
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=STREAM_CLOSE_TIMEOUT_S)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out after %.1fs while stopping background task %s",
            STREAM_CLOSE_TIMEOUT_S,
            task.get_name(),
        )


async def _watch_request_disconnect(request: Request, request_id: str, client_id: str) -> None:
    """Cancel the tracked queue request as soon as the downstream client disconnects."""
    while True:
        if await request.is_disconnected():
            snapshot = inference_queue.cancel(
                request_id,
                client_id=client_id,
                reason="client_disconnected",
            )
            logger.info(
                "🔌 [%s] Client '%s' disconnected (%s)",
                request_id[:8],
                client_id,
                (snapshot or {}).get("status", "unknown"),
            )
            return
        await asyncio.sleep(0.25)


async def _begin_queued_request(request: Request, client_id: str, model: str) -> tuple[str, asyncio.Task]:
    """Register a queue request immediately and wait until Guardian grants a slot."""
    normalized_client_id = client_id.strip() if isinstance(client_id, str) else ""
    if not normalized_client_id or normalized_client_id.lower() == "unauthenticated":
        logger.warning("🚫 Rejecting queue access without an authenticated client id")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Authenticated client required for queue access",
            headers={"WWW-Authenticate": "Bearer"},
        )

    queue_owner_id = _get_queue_owner_id(request, normalized_client_id)
    if not queue_owner_id:
        logger.warning("🚫 Rejecting queue access without an authenticated API key fingerprint")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Authenticated API key fingerprint required for queue access",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        request_id = inference_queue.submit(
            normalized_client_id,
            model,
            owner_id=queue_owner_id,
        )
    except QueueAdmissionRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "queue_admission_rejected",
                "reason": exc.reason,
                "message": exc.message,
                "existing_request_id": exc.existing_request_id,
                "existing_status": exc.existing_status,
                "client_id": exc.client_id,
            },
        ) from exc

    _update_live_request_usage(request, queue_request_id=request_id, phase="queued")
    disconnect_task = asyncio.create_task(_watch_request_disconnect(request, request_id, normalized_client_id))
    try:
        await inference_queue.wait_for_turn(request_id)
    except QueueRequestCancelled as exc:
        _update_live_request_usage(request, queue_request_id=request_id, phase="cancelled")
        await _stop_background_task(disconnect_task)
        raise _GuardianRequestCancelled(request_id, exc.reason) from exc
    _update_live_request_usage(
        request,
        queue_request_id=request_id,
        phase="running",
        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
    )
    return request_id, disconnect_task


async def _await_or_cancel_request(
    operation_task: asyncio.Task,
    request_id: str,
    cleanup: Optional[Callable[[], Awaitable[None]]] = None,
) -> Any:
    """Wait for backend work to finish, but abort promptly if the tracked request is cancelled."""
    cancel_event = inference_queue.get_cancel_event(request_id)
    if cancel_event is None:
        return await operation_task

    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, pending = await asyncio.wait(
            {operation_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancel_event.is_set():
            if cleanup is not None:
                with suppress(Exception):
                    await asyncio.wait_for(cleanup(), timeout=STREAM_CLOSE_TIMEOUT_S)
            if not operation_task.done():
                operation_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(operation_task, timeout=STREAM_CLOSE_TIMEOUT_S)
            snapshot = inference_queue.get_request_status(request_id)
            reason = (snapshot or {}).get("cancel_reason", "cancelled")
            raise _GuardianRequestCancelled(request_id, reason)
        return await operation_task
    finally:
        cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task


async def _close_stream_resources(response: httpx.Response, client: httpx.AsyncClient) -> None:
    """Close the upstream streaming response and client without surfacing cleanup noise."""
    for resource_name, closer in (
        ("response", response.aclose),
        ("client", client.aclose),
    ):
        try:
            await asyncio.wait_for(closer(), timeout=STREAM_CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs closing upstream stream %s during cancellation",
                STREAM_CLOSE_TIMEOUT_S,
                resource_name,
            )
        except Exception:
            pass


async def _close_on_request_cancel(
    request_id: str,
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    """Wait for request cancellation and then run the provided cleanup coroutine."""
    cancel_event = inference_queue.get_cancel_event(request_id)
    if cancel_event is None:
        return
    await cancel_event.wait()
    try:
        await asyncio.wait_for(cleanup(), timeout=STREAM_CLOSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out after %.1fs while closing upstream resources for cancelled request %s",
            STREAM_CLOSE_TIMEOUT_S,
            request_id[:8],
        )


def _request_outcome(request_id: str) -> str:
    """Map the tracked request lifecycle to a final queue outcome."""
    return "cancelled" if inference_queue.is_cancel_requested(request_id) else "completed"


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


def _model_disables_thinking_by_default(model_name: str) -> bool:
    """Return whether a configured model is a non-reasoning/special runtime."""
    config = model_manager.models.get(model_name, {})
    if config.get("default_enable_thinking") is False or config.get("enable_thinking") is False:
        return True

    model_type = str(config.get("model_type", "")).strip().lower()
    if model_type in {"embedding", "embeddings"}:
        return True

    searchable = " ".join(
        str(value).lower()
        for value in (model_name, config.get("path", ""), config.get("extra_args", ""))
    )
    return "embed" in searchable or "--reasoning off" in searchable


def _request_explicitly_disables_thinking(payload: Dict[str, Any]) -> bool:
    if payload.get("reasoning_budget") == 0:
        return True
    template_kwargs = payload.get("chat_template_kwargs")
    return isinstance(template_kwargs, dict) and template_kwargs.get("enable_thinking") is False


def _apply_request_reasoning_defaults(path: str, payload: Dict[str, Any], model_name: str) -> bool:
    """Apply no-thinking request flags only for explicit or special runtimes."""
    if path not in {"chat/completions", "messages", "completions"}:
        return False

    should_disable = (
        _request_explicitly_disables_thinking(payload)
        or _model_disables_thinking_by_default(model_name)
    )
    if not should_disable:
        return False

    changed = False
    if payload.get("reasoning_budget") != 0:
        payload["reasoning_budget"] = 0
        changed = True

    if path in {"chat/completions", "messages"}:
        template_kwargs = payload.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            template_kwargs = {}
            payload["chat_template_kwargs"] = template_kwargs
            changed = True
        if template_kwargs.get("enable_thinking") is not False:
            template_kwargs["enable_thinking"] = False
            changed = True

    return changed


_SYSTEM_CONTEXT_UPDATE_PREFIX = "[System Context Update]:\n"


def _stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                else:
                    parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
            elif part is not None:
                parts.append(str(part))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _sanitize_messages_for_qwen_chat_template(messages: Any) -> Any:
    """Demote later system messages so strict Qwen templates can render them."""
    if not isinstance(messages, list):
        return messages

    sanitized: list[Any] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            sanitized.append(message)
            continue

        if message.get("role") == "system" and index > 0:
            updated = dict(message)
            updated["role"] = "user"
            content = _stringify_message_content(message.get("content"))
            updated["content"] = (
                f"{_SYSTEM_CONTEXT_UPDATE_PREFIX}{content}"
                if content
                else _SYSTEM_CONTEXT_UPDATE_PREFIX.rstrip("\n")
            )
            sanitized.append(updated)
            continue

        sanitized.append(message)

    return sanitized


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
    if "35b" in model_lower: return 22000
    if "31b" in model_lower: return 20000
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
        self.api_usage = ApiUsageTracker()
        # VRAM Scheduler
        self.scheduler = VramScheduler(SAFE_VRAM_LIMIT_MB)
        # Optimizer
        self.optimizer = RequestOptimizer()
        # Dynamic scaler — adaptive reasoning budget & max_tokens
        self.scaler = DynamicScaler()

state = State()


def _coerce_usage_int(value: object) -> int:
    """Convert token usage values to non-negative integers."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _coerce_header_int(value: object) -> int:
    """Convert a header-like byte count to a non-negative integer."""
    try:
        return max(int(str(value).strip()), 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _request_size_bytes(request: Request) -> int:
    """Best-effort byte count for the inbound request body."""
    return _coerce_header_int(request.headers.get("content-length"))


def _response_size_bytes(response: Response) -> int:
    """Best-effort byte count for the outbound response body."""
    header_value = response.headers.get("content-length")
    if header_value not in (None, ""):
        return _coerce_header_int(header_value)
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    return 0


def _should_track_api_usage(path: str) -> bool:
    """Return whether the request path should count toward API usage."""
    if path in {"/healthz", "/metrics"}:
        return False
    return path.startswith("/api/") or path.startswith("/v1/") or path.startswith("/admin/")


def _get_usage_client_id(request: Request) -> Optional[str]:
    """Extract the authenticated client name attached by auth."""
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        name = user.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    auth_context = getattr(request.state, "auth_context", None)
    if isinstance(auth_context, dict):
        name = auth_context.get("client_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _get_usage_attribution(request: Request) -> Optional[Dict[str, Any]]:
    """Return request attribution details collected during auth."""
    auth_context = get_request_auth_context(request)
    if isinstance(auth_context, dict):
        return auth_context
    return build_request_auth_context(request)


def _get_queue_owner_id(request: Request, client_id: Optional[str]) -> Optional[str]:
    """Return the per-key queue ownership identity for the current request."""
    state_obj = getattr(request, "state", None)
    auth_context = getattr(state_obj, "auth_context", None)
    if isinstance(auth_context, dict):
        fingerprint = auth_context.get("key_fingerprint")
        if isinstance(fingerprint, str) and fingerprint.strip():
            return f"key:{fingerprint.strip()}"
    if isinstance(client_id, str) and client_id.strip():
        return f"client:{client_id.strip()}"
    return None


def _get_live_usage_request_id(request: Request) -> Optional[str]:
    """Return the dashboard request id bound to the current FastAPI request."""
    state_obj = getattr(request, "state", None)
    if state_obj is None:
        return None
    request_id = getattr(state_obj, "guardian_usage_request_id", None)
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    return None


def _start_live_request_usage(request: Request) -> None:
    """Register the current API request as in-flight for dashboard polling."""
    if not isinstance(get_request_auth_context(request), dict):
        set_request_auth_context(request, build_request_auth_context(request))
    live_request_id = str(uuid.uuid4())
    request.state.guardian_usage_request_id = live_request_id
    request.state.guardian_usage_started_monotonic = time.monotonic()
    state.api_usage.start_request(
        request_id=live_request_id,
        client_id=_get_usage_client_id(request),
        endpoint=request.url.path,
        method=request.method,
        model=getattr(request.state, "guardian_usage_model", None),
        request_bytes=_request_size_bytes(request),
        streamed=bool(getattr(request.state, "guardian_usage_streamed", False)),
        attribution=_get_usage_attribution(request),
    )


def _update_live_request_usage(
    request: Request,
    *,
    model: Optional[str] = None,
    streamed: Optional[bool] = None,
    queue_request_id: Optional[str] = None,
    phase: Optional[str] = None,
    queue_wait_ms: Optional[float] = None,
    prompt_tokens: Optional[object] = None,
    completion_tokens: Optional[object] = None,
    output_chars_delta: object = 0,
    response_bytes_delta: object = 0,
) -> None:
    """Push incremental request metadata into the live dashboard tracker."""
    live_request_id = _get_live_usage_request_id(request)
    if live_request_id is None:
        return
    state.api_usage.update_active_request(
        request_id=live_request_id,
        model=model,
        streamed=streamed,
        queue_request_id=queue_request_id,
        phase=phase,
        queue_wait_ms=queue_wait_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        output_chars_delta=output_chars_delta,
        response_bytes_delta=response_bytes_delta,
    )


def _finish_live_request_usage(
    request: Request,
    *,
    status_code: int,
    response_bytes: Optional[int] = None,
) -> None:
    """Finalize the live dashboard request entry and fold it into history."""
    live_request_id = _get_live_usage_request_id(request)
    if live_request_id is None or getattr(request.state, "guardian_usage_finished", False):
        return
    started = getattr(request.state, "guardian_usage_started_monotonic", None)
    duration_ms = None
    if isinstance(started, (int, float)):
        duration_ms = max((time.monotonic() - float(started)) * 1000.0, 0.0)
    state.api_usage.finish_request(
        request_id=live_request_id,
        client_id=_get_usage_client_id(request),
        endpoint=request.url.path,
        method=request.method,
        status_code=status_code,
        model=getattr(request.state, "guardian_usage_model", None),
        duration_ms=duration_ms,
        request_bytes=_request_size_bytes(request),
        response_bytes=response_bytes,
        streamed=bool(getattr(request.state, "guardian_usage_streamed", False)),
        attribution=_get_usage_attribution(request),
    )
    request.state.guardian_usage_finished = True


def _set_request_usage_metadata(
    request: Request,
    *,
    model: Optional[str] = None,
    streamed: Optional[bool] = None,
) -> None:
    """Attach request metadata for dashboard usage snapshots."""
    if model is not None:
        request.state.guardian_usage_model = model
    if streamed is not None:
        request.state.guardian_usage_streamed = streamed
    _update_live_request_usage(request, model=model, streamed=streamed)


def _record_request_token_usage(
    client_id: Optional[str],
    endpoint: str,
    model: Optional[str],
    *,
    request: Optional[Request] = None,
    attribution: Optional[Dict[str, Any]] = None,
    prompt_tokens: object = 0,
    completion_tokens: object = 0,
) -> None:
    """Store token usage for a completed request when available."""
    resolved_attribution = attribution
    if resolved_attribution is None and request is not None:
        resolved_attribution = _get_usage_attribution(request)
    state.api_usage.record_tokens(
        client_id=client_id,
        endpoint=endpoint,
        model=model,
        prompt_tokens=_coerce_usage_int(prompt_tokens),
        completion_tokens=_coerce_usage_int(completion_tokens),
        attribution=resolved_attribution,
    )


def _record_usage_from_payload(
    client_id: Optional[str],
    endpoint: str,
    model: Optional[str],
    payload: Optional[Dict[str, Any]],
    *,
    request: Optional[Request] = None,
    attribution: Optional[Dict[str, Any]] = None,
) -> None:
    """Extract OpenAI-style usage fields from a JSON payload."""
    if not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    _record_request_token_usage(
        client_id,
        endpoint,
        model,
        request=request,
        attribution=attribution,
        prompt_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        completion_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
    )


@app.middleware("http")
async def track_api_usage_middleware(request: Request, call_next):
    """Track aggregate API usage for dashboard monitoring."""
    path = request.url.path
    if not _should_track_api_usage(path):
        return await call_next(request)

    _start_live_request_usage(request)
    try:
        response = await call_next(request)
    except Exception:
        _finish_live_request_usage(request, status_code=500, response_bytes=0)
        raise

    is_streaming_response = bool(getattr(request.state, "guardian_usage_streamed", False)) and isinstance(response, StreamingResponse)
    if not is_streaming_response:
        _finish_live_request_usage(
            request,
            status_code=response.status_code,
            response_bytes=_response_size_bytes(response),
        )
    return response


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
    reload_model = _resolve_auto_reload_model(current_model)
    logger.warning(
        f"⚠️ Backend unreachable while proxying /v1/{path}; "
        f"reloading '{reload_model}' once before retry: {error}"
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
                target_model=reload_model,
                requested_model=current_model,
                owner="backend_recovery",
            )
            await _run_guardian_operation(
                source="proxy",
                phase="backend_reload",
                target_model=reload_model,
                requested_model=current_model,
                owner="backend_recovery",
                operation=lambda: model_manager.load(reload_model),
                generation=generation,
            )
        except ModelLoadError as e:
            crash = e.crash_record
            detail = {
                "error": f"Backend reload failed for '{reload_model}'",
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
    model = _resolve_or_reject_inference_model(model, current_model)

    logger.info(f"bridge: Ollama chat request for '{model}' -> Translating to OpenAI format")

    try:
        request_id, disconnect_task = await _begin_queued_request(request, client_id, model)
    except _GuardianRequestCancelled as exc:
        raise _request_cancel_http_exception(exc.request_id, exc.reason)

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if model_manager.is_unloaded:
            reload_model = _resolve_auto_reload_model(model)
            logger.info(f"🔄 Auto-reloading '{reload_model}'...")
            generation = _reset_startup_check_status(
                source="proxy",
                phase="auto_reload",
                target_model=reload_model,
                requested_model=model,
                owner=client_id,
            )
            async with _model_switch_lock:
                if model_manager.is_unloaded:
                    await _run_guardian_operation(
                        source="proxy",
                        phase="auto_reload",
                        target_model=reload_model,
                        requested_model=model,
                        owner=client_id,
                        operation=lambda: model_manager.load(reload_model),
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
        _set_request_usage_metadata(request, model=model, streamed=stream)
        
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
        request_timeout = _build_stream_timeout(timeout_sec) if stream else timeout_sec
        client = httpx.AsyncClient(timeout=request_timeout)
        
        req = client.build_request(
            "POST",
            f"{LLAMA_SERVER_URL}/v1/chat/completions",
            json=openai_body,
            timeout=request_timeout
        )
        
        try:
            send_task = asyncio.create_task(client.send(req, stream=stream))
            r = await _await_or_cancel_request(
                send_task,
                request_id,
                cleanup=client.aclose,
            )
        except _GuardianRequestCancelled:
            await client.aclose()
            raise
        except Exception as e:
            await client.aclose()
            raise e

        if stream:
            usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}

            async def stream_adapter():
                cancel_cleanup_task = asyncio.create_task(
                    _close_on_request_cancel(
                        request_id,
                        lambda: _close_stream_resources(r, client),
                    )
                )
                try:
                    watchdog = StreamProgressWatchdog(timeout_sec)
                    async for chunk in _iter_sse_lines_with_watchdog(
                        r,
                        watchdog,
                        request_id=request_id,
                        route="/api/chat",
                        client_id=client_id,
                        model_name=model,
                        cancel_event=inference_queue.get_cancel_event(request_id),
                    ):
                        if not chunk or chunk.strip() == "data: [DONE]": 
                            continue
                        if chunk.startswith("data: "):
                            try:
                                data = json.loads(chunk[6:])
                                usage = data.get("usage") or {}
                                if isinstance(usage, dict):
                                    usage_totals["prompt_tokens"] = max(
                                        usage_totals["prompt_tokens"],
                                        _coerce_usage_int(usage.get("prompt_tokens", 0)),
                                    )
                                    usage_totals["completion_tokens"] = max(
                                        usage_totals["completion_tokens"],
                                        _coerce_usage_int(usage.get("completion_tokens", 0)),
                                    )
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
                                        payload = json.dumps(ollama_chunk) + "\n"
                                        _update_live_request_usage(
                                            request,
                                            prompt_tokens=usage_totals["prompt_tokens"],
                                            completion_tokens=usage_totals["completion_tokens"],
                                            output_chars_delta=len(content),
                                            response_bytes_delta=len(payload.encode("utf-8")),
                                        )
                                        yield payload
                            except:
                                pass
                    if not inference_queue.is_cancel_requested(request_id):
                        yield json.dumps({
                            "model": model, 
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), 
                            "done": True,
                            "total_duration": 0,
                            "load_duration": 0,
                            "prompt_eval_count": 0,
                            "eval_count": 0
                        }) + "\n"
                except (asyncio.CancelledError, _GuardianRequestCancelled):
                    pass
                finally:
                    cancel_cleanup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await cancel_cleanup_task
                    await r.aclose()
                    await client.aclose()
                    _record_request_token_usage(
                        client_id,
                        "/api/chat",
                        model,
                        request=request,
                        prompt_tokens=usage_totals["prompt_tokens"],
                        completion_tokens=usage_totals["completion_tokens"],
                    )
                    _finish_live_request_usage(
                        request,
                        status_code=499 if inference_queue.is_cancel_requested(request_id) else r.status_code,
                    )
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.finish(request_id, outcome=_request_outcome(request_id))
                    await _stop_background_task(disconnect_task)

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
                data = await _await_or_cancel_request(
                    asyncio.create_task(r.aread()),
                    request_id,
                    cleanup=lambda: _close_stream_resources(r, client),
                )
                data = json.loads(data)
                content = _extract_assistant_message_text(data["choices"][0]["message"])
                _record_usage_from_payload(client_id, "/api/chat", model, data, request=request)
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
            except _GuardianRequestCancelled as exc:
                raise _request_cancel_http_exception(exc.request_id, exc.reason)
            except Exception as e:
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
    finally:
        await _stop_background_task(locals().get("disconnect_task"))
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.finish(request_id, outcome=_request_outcome(request_id))

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
    model = _resolve_or_reject_inference_model(model, current_model)

    try:
        request_id, disconnect_task = await _begin_queued_request(request, client_id, model)
    except _GuardianRequestCancelled as exc:
        raise _request_cancel_http_exception(exc.request_id, exc.reason)

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if model_manager.is_unloaded:
            reload_model = _resolve_auto_reload_model(model)
            logger.info(f"🔄 Auto-reloading '{reload_model}'...")
            generation = _reset_startup_check_status(
                source="proxy",
                phase="auto_reload",
                target_model=reload_model,
                requested_model=model,
                owner=client_id,
            )
            async with _model_switch_lock:
                if model_manager.is_unloaded:
                    await _run_guardian_operation(
                        source="proxy",
                        phase="auto_reload",
                        target_model=reload_model,
                        requested_model=model,
                        owner=client_id,
                        operation=lambda: model_manager.load(reload_model),
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
        _set_request_usage_metadata(request, model=model, streamed=stream)
        options = body.get("options", {})
        temperature = options.get("temperature", 0.7)
        
        openai_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature
        }

        timeout_sec = get_model_timeout(model)
        request_timeout = _build_stream_timeout(timeout_sec) if stream else timeout_sec
        client = httpx.AsyncClient(timeout=request_timeout)
        
        req = client.build_request(
            "POST",
            f"{LLAMA_SERVER_URL}/v1/chat/completions",
            json=openai_body,
            timeout=request_timeout
        )

        try:
            send_task = asyncio.create_task(client.send(req, stream=stream))
            r = await _await_or_cancel_request(
                send_task,
                request_id,
                cleanup=client.aclose,
            )
        except _GuardianRequestCancelled:
            await client.aclose()
            raise
        except Exception as e:
            await client.aclose()
            raise e

        if stream:
            usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}

            async def stream_adapter_generate():
                cancel_cleanup_task = asyncio.create_task(
                    _close_on_request_cancel(
                        request_id,
                        lambda: _close_stream_resources(r, client),
                    )
                )
                try:
                    watchdog = StreamProgressWatchdog(timeout_sec)
                    async for chunk in _iter_sse_lines_with_watchdog(
                        r,
                        watchdog,
                        request_id=request_id,
                        route="/api/generate",
                        client_id=client_id,
                        model_name=model,
                        cancel_event=inference_queue.get_cancel_event(request_id),
                    ):
                        if not chunk or chunk.strip() == "data: [DONE]": 
                            continue
                        if chunk.startswith("data: "):
                            try:
                                data = json.loads(chunk[6:])
                                usage = data.get("usage") or {}
                                if isinstance(usage, dict):
                                    usage_totals["prompt_tokens"] = max(
                                        usage_totals["prompt_tokens"],
                                        _coerce_usage_int(usage.get("prompt_tokens", 0)),
                                    )
                                    usage_totals["completion_tokens"] = max(
                                        usage_totals["completion_tokens"],
                                        _coerce_usage_int(usage.get("completion_tokens", 0)),
                                    )
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
                                        payload = json.dumps(ollama_chunk) + "\n"
                                        _update_live_request_usage(
                                            request,
                                            prompt_tokens=usage_totals["prompt_tokens"],
                                            completion_tokens=usage_totals["completion_tokens"],
                                            output_chars_delta=len(content),
                                            response_bytes_delta=len(payload.encode("utf-8")),
                                        )
                                        yield payload
                            except:
                                pass
                    if not inference_queue.is_cancel_requested(request_id):
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
                except (asyncio.CancelledError, _GuardianRequestCancelled):
                    pass
                finally:
                    cancel_cleanup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await cancel_cleanup_task
                    await r.aclose()
                    await client.aclose()
                    _record_request_token_usage(
                        client_id,
                        "/api/generate",
                        model,
                        request=request,
                        prompt_tokens=usage_totals["prompt_tokens"],
                        completion_tokens=usage_totals["completion_tokens"],
                    )
                    _finish_live_request_usage(
                        request,
                        status_code=499 if inference_queue.is_cancel_requested(request_id) else r.status_code,
                    )
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.finish(request_id, outcome=_request_outcome(request_id))
                    await _stop_background_task(disconnect_task)

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
                data = await _await_or_cancel_request(
                    asyncio.create_task(r.aread()),
                    request_id,
                    cleanup=lambda: _close_stream_resources(r, client),
                )
                data = json.loads(data)
                content = _extract_assistant_message_text(data["choices"][0]["message"])
                _record_usage_from_payload(client_id, "/api/generate", model, data, request=request)
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
            except _GuardianRequestCancelled as exc:
                raise _request_cancel_http_exception(exc.request_id, exc.reason)
            except Exception as e:
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
    finally:
        await _stop_background_task(locals().get("disconnect_task"))
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.finish(request_id, outcome=_request_outcome(request_id))


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
def _build_model_metadata_entry(public_name: str, canonical_name: str, client_id: str) -> Dict[str, Any]:
    model_entry: Dict[str, Any] = {
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
    if vision["configured"] and vision["status"] not in {
        "misconfigured",
        "text_only",
        "unknown",
        "unsupported",
    }:
        model_entry["input_modalities"].append("image")
    model_entry["configured_input_modalities"] = ["text"]
    if vision["configured"]:
        model_entry["configured_input_modalities"].append("image")
    model_entry["vision"] = {
        "configured": vision["configured"],
        "status": vision["status"],
        "validated": vision["validated"],
    }

    # Claude Code currently compacts against the OpenAI-compatible max_context
    # field only. Preserve benchmark-cap semantics for normal clients, but
    # return the safer advertised window for Claude so it compacts before hard
    # overflow.
    if client_id == "claudecode" and advertised_context is not None:
        if benchmark_context_limit is not None:
            model_entry["benchmark_context_limit"] = benchmark_context_limit
        model_entry["max_context"] = advertised_context
    return model_entry


@app.get("/v1/models")
async def list_models(client_id: str = Depends(verify_api_key)):
    """List available models from config."""
    models_list = []
    try:
        for public_name, canonical_name in model_manager.get_public_model_map().items():
            models_list.append(_build_model_metadata_entry(public_name, canonical_name, client_id))
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        
    return {"object": "list", "data": models_list}


@app.get("/v1/models/{model_id:path}")
async def get_model_metadata(model_id: str, client_id: str = Depends(verify_api_key)):
    """Return metadata for a configured canonical model or public alias."""
    public_models = model_manager.get_public_model_map()
    canonical_name = public_models.get(model_id)
    if canonical_name is None:
        try:
            canonical_name = model_manager.resolve_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _build_model_metadata_entry(model_id, canonical_name, client_id)


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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
async def queue_status(request: Request, client_id: str = Depends(verify_api_key)):
    """Return current queue status.  Clients should poll this while waiting."""
    return inference_queue.get_status(
        client_id=client_id,
        owner_id=_get_queue_owner_id(request, client_id),
    )


@app.get("/v1/queue/requests/{request_id}")
async def queue_request_status(request_id: str, request: Request, client_id: str = Depends(verify_api_key)):
    """Return the lifecycle state for one tracked queue request."""
    snapshot = inference_queue.get_request_status(
        request_id,
        client_id=client_id,
        owner_id=_get_queue_owner_id(request, client_id),
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Queue request not found")
    return snapshot


@app.delete("/v1/queue/requests/{request_id}")
async def cancel_queue_request(request_id: str, request: Request, client_id: str = Depends(verify_api_key)):
    """Cancel a waiting request or request cancellation of a running one."""
    snapshot = inference_queue.cancel(
        request_id,
        client_id=client_id,
        owner_id=_get_queue_owner_id(request, client_id),
        reason="client_requested_cancel",
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Queue request not found")
    return snapshot


# OpenAI-compatible /v1/ routes (used by OpenClaw and other OpenAI-compatible clients)
@app.get("/v1/{path:path}")
async def proxy_v1_get(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{LLAMA_SERVER_URL}/v1/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/v1/{path:path}")
async def proxy_v1_post(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    body = await request.body()
    _set_request_usage_metadata(request, streamed=False)

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

    try:
        json_body = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "reason": "invalid_json_body",
                "message": "Inference requests must provide a valid JSON object body.",
                "parse_error": str(exc),
            },
        )

    if not isinstance(json_body, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "reason": "invalid_json_body",
                "message": "Inference requests must provide a JSON object body.",
            },
        )

    requested_model = json_body.get("model")
    if not requested_model:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "reason": "model_not_specified",
                "message": "Inference requests must include a model name.",
            },
        )

    current_model = await model_manager.get_current_model()
    requested_model = _resolve_or_reject_inference_model(requested_model, current_model)
    json_body["model"] = requested_model
    _apply_request_reasoning_defaults(path, json_body, requested_model)
    if path in ("chat/completions", "messages"):
        json_body["messages"] = _sanitize_messages_for_qwen_chat_template(
            json_body.get("messages", [])
        )
    body = json.dumps(json_body).encode("utf-8")
    has_image_inputs = False
    if path in ("chat/completions", "messages"):
        has_image_inputs = _messages_contain_image_input(json_body.get("messages", []))

    try:
        request_id, disconnect_task = await _begin_queued_request(request, client_id, requested_model)
    except _GuardianRequestCancelled as exc:
        raise _request_cancel_http_exception(exc.request_id, exc.reason)

    _release_in_finally = True
    try:
        # If llama-server was unloaded, auto-reload before forwarding
        if model_manager.is_unloaded:
            reload_model = _resolve_auto_reload_model(requested_model)
            logger.info(f"🔄 Incoming request while unloaded — auto-reloading '{reload_model}'...")
            try:
                generation = _reset_startup_check_status(
                    source="proxy",
                    phase="auto_reload",
                    target_model=reload_model,
                    requested_model=requested_model or current_model,
                    owner=client_id,
                )
                async with _model_switch_lock:
                    if model_manager.is_unloaded:  # double-check under lock
                        await _run_guardian_operation(
                            source="proxy",
                            phase="auto_reload",
                            target_model=reload_model,
                            requested_model=requested_model or current_model,
                            owner=client_id,
                            operation=lambda: model_manager.load(
                                reload_model,
                                enable_vision=_desired_runtime_vision_enabled(
                                    reload_model,
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

        # Auto-switch logic for GPU-backed inference routes (with concurrency lock)
        if path in ("chat/completions", "messages", "completions", "embeddings"):
            try:
                current_model = await model_manager.get_current_model()
                desired_model = requested_model or current_model
                if desired_model in model_manager.models:
                    desired_vision = (
                        _desired_runtime_vision_enabled(desired_model, has_image_inputs)
                        if path in ("chat/completions", "messages")
                        else False
                    )
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
                            desired_vision = (
                                _desired_runtime_vision_enabled(desired_model, has_image_inputs)
                                if path in ("chat/completions", "messages")
                                else False
                            )
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
            except HTTPException:
                raise  # Let model-load errors propagate to the client
            except Exception as e:
                logger.error(f"Error checking model switch: {e}")

        active_model_for_request = requested_model or await model_manager.get_current_model()
        _set_request_usage_metadata(request, model=active_model_for_request)
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

        timeout_sec = float(get_model_timeout(active_model_for_request))
        timeout = httpx.Timeout(timeout_sec, connect=10.0)
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
            _set_request_usage_metadata(request, streamed=True)
            # Stream SSE chunks in real-time instead of buffering entire response
            stream_timeout = _build_stream_timeout(timeout_sec)
            client = httpx.AsyncClient(timeout=stream_timeout)
            req = client.build_request(
                "POST",
                f"{LLAMA_SERVER_URL}/v1/{path}",
                content=body,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            )
            try:
                send_task = asyncio.create_task(client.send(req, stream=True))
                resp = await _await_or_cancel_request(
                    send_task,
                    request_id,
                    cleanup=client.aclose,
                )
            except httpx.ConnectError as e:
                await client.aclose()
                await _reload_backend_after_connect_error(path, e)

                client = httpx.AsyncClient(timeout=stream_timeout)
                req = client.build_request(
                    "POST",
                    f"{LLAMA_SERVER_URL}/v1/{path}",
                    content=body,
                    headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                )
                try:
                    send_task = asyncio.create_task(client.send(req, stream=True))
                    resp = await _await_or_cancel_request(
                        send_task,
                        request_id,
                        cleanup=client.aclose,
                    )
                except _GuardianRequestCancelled as exc:
                    await client.aclose()
                    raise _request_cancel_http_exception(exc.request_id, exc.reason)
                except Exception as retry_error:
                    await client.aclose()
                    raise HTTPException(status_code=502, detail=f"Backend request failed after reload: {retry_error}")
            except _GuardianRequestCancelled as exc:
                await client.aclose()
                raise _request_cancel_http_exception(exc.request_id, exc.reason)
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

            usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}

            async def stream_passthrough():
                cancel_cleanup_task = asyncio.create_task(
                    _close_on_request_cancel(
                        request_id,
                        lambda: _close_stream_resources(resp, client),
                    )
                )
                try:
                    watchdog = StreamProgressWatchdog(timeout_sec)
                    async for line in _iter_sse_lines_with_watchdog(
                        resp,
                        watchdog,
                        request_id=request_id,
                        route=f"/v1/{path}",
                        client_id=client_id,
                        model_name=active_model_for_request,
                        heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S,
                        cancel_event=inference_queue.get_cancel_event(request_id),
                    ):
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                usage = data.get("usage") or {}
                                output_chars_delta = 0
                                if isinstance(usage, dict):
                                    usage_totals["prompt_tokens"] = max(
                                        usage_totals["prompt_tokens"],
                                        _coerce_usage_int(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
                                    )
                                    usage_totals["completion_tokens"] = max(
                                        usage_totals["completion_tokens"],
                                        _coerce_usage_int(
                                            usage.get("completion_tokens", usage.get("output_tokens", 0))
                                        ),
                                    )
                                if "choices" in data and isinstance(data.get("choices"), list) and data["choices"]:
                                    delta = data["choices"][0].get("delta", {})
                                    if isinstance(delta, dict):
                                        output_chars_delta = len(_extract_assistant_delta_text(delta))
                                encoded_line = (line + "\n").encode("utf-8")
                                _update_live_request_usage(
                                    request,
                                    prompt_tokens=usage_totals["prompt_tokens"],
                                    completion_tokens=usage_totals["completion_tokens"],
                                    output_chars_delta=output_chars_delta,
                                    response_bytes_delta=len(encoded_line),
                                )
                            except (TypeError, ValueError, json.JSONDecodeError):
                                encoded_line = (line + "\n").encode("utf-8")
                                _update_live_request_usage(request, response_bytes_delta=len(encoded_line))
                        else:
                            encoded_line = (line + "\n").encode("utf-8")
                            _update_live_request_usage(request, response_bytes_delta=len(encoded_line))
                        yield encoded_line
                except (asyncio.CancelledError, _GuardianRequestCancelled):
                    pass
                finally:
                    cancel_cleanup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await cancel_cleanup_task
                    await resp.aclose()
                    await client.aclose()
                    _record_request_token_usage(
                        client_id,
                        f"/v1/{path}",
                        active_model_for_request,
                        request=request,
                        prompt_tokens=usage_totals["prompt_tokens"],
                        completion_tokens=usage_totals["completion_tokens"],
                    )
                    _finish_live_request_usage(
                        request,
                        status_code=499 if inference_queue.is_cancel_requested(request_id) else resp.status_code,
                    )
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.finish(request_id, outcome=_request_outcome(request_id))
                    await _stop_background_task(disconnect_task)

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
                    post_task = asyncio.create_task(
                        client.post(
                            f"{LLAMA_SERVER_URL}/v1/{path}",
                            content=body,
                            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                        )
                    )
                    resp = await _await_or_cancel_request(
                        post_task,
                        request_id,
                        cleanup=client.aclose,
                    )
                except httpx.ConnectError as e:
                    await _reload_backend_after_connect_error(path, e)
                    try:
                        post_task = asyncio.create_task(
                            client.post(
                                f"{LLAMA_SERVER_URL}/v1/{path}",
                                content=body,
                                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                            )
                        )
                        resp = await _await_or_cancel_request(
                            post_task,
                            request_id,
                            cleanup=client.aclose,
                        )
                    except Exception as retry_error:
                        raise HTTPException(status_code=502, detail=f"Backend request failed after reload: {retry_error}")
                except _GuardianRequestCancelled as exc:
                    raise _request_cancel_http_exception(exc.request_id, exc.reason)
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
                if path in ("chat/completions", "completions", "embeddings", "messages"):
                    try:
                        payload = resp.json()
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                    _record_usage_from_payload(client_id, f"/v1/{path}", active_model_for_request, payload, request=request)
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
        await _stop_background_task(locals().get("disconnect_task"))
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.finish(request_id, outcome=_request_outcome(request_id))

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
