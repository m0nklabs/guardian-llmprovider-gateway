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
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED

from collections import defaultdict
from app.proxy.optimizer import RequestOptimizer
from app.proxy.scaler import DynamicScaler
from app.engine.manager import ModelManager, ModelLoadError
from app.proxy.auth import build_request_auth_context, get_request_auth_context, set_request_auth_context, verify_api_key, generate_api_key, load_api_keys, _token_fingerprint
from app.proxy.providers import CloudProvider, ProviderRegistry
from app.proxy.cloud_keys import CloudCredentialStore, parse_guardian_route, mask_api_key
from app.proxy.failover import FailoverRegistry, ProviderHealthTracker, FAILURE_THRESHOLD, COOLDOWN_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS
from app.proxy.ratelimit import RateLimitConfig, RateLimitRetryManager
from app.proxy.usage import ApiUsageTracker
from app.proxy.anthropic_bridge import (
    _format_sse_event,
    provider_needs_anthropic_translation,
    translate_anthropic_request_to_openai,
    translate_openai_error_to_anthropic,
    translate_openai_response_to_anthropic,
    translate_openai_stream_to_anthropic,
)
from app.proxy.queue import InferenceQueue, QueueAdmissionRejected, QueueRequestCancelled
from app.proxy.metrics import (
    track_request,
    update_queue_metrics,
    update_gpu_metrics,
    update_system_metrics,
    update_capture_metrics,
    get_metrics_output,
    MODEL_SWITCHES,
    MODEL_CRASHES,
    QUEUE_TOTAL_QUEUED,
    QUEUE_TOTAL_COMPLETED,
    QUEUE_TOTAL_TIMEOUTS,
    AUTH_FAILURES,
)

# ── Capture subsystem (opt-in, fail-open, disabled by default) ───────
from app.capture.integration import (
    capture_controller,
    get_capture_controller,
    get_capture_sink_snapshot,
)
from app.capture.config import PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC, PROTOCOL_OLLAMA, ROUTE_LOCAL
from app.capture.redactor import anthropic_messages_to_openai
from app.capture.schema import BuildContext
from app.capture.stream_assembler import StreamResponseAssembler

# ── Gateway helpers (Phase 5 extraction) ─────────────────────────────
from app.gateway import context_metadata as _ctx_meta

# Load configuration from settings.yaml
def load_config() -> dict:

    """Load configuration from settings.yaml with sensible defaults."""
    config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
    default_config = {
        "proxy": {
            "stream_heartbeat_seconds": 15,
            "stream_close_timeout_seconds": 5,
        },
        "cloud_retry": RateLimitConfig().to_dict(),
        "timeouts": {
            "tiers": {
                "tier_70b": {"min_size_mb": 40000, "timeout_seconds": 3600},
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
            if "cloud_retry" in file_config:
                default_config["cloud_retry"].update(file_config["cloud_retry"])
            if "timeouts" in file_config:
                default_config["timeouts"].update(file_config["timeouts"])
            if "failover_health" in file_config:
                default_config["failover_health"] = file_config["failover_health"]
            return default_config
    except Exception as e:
        logging.warning(f"Failed to load config from {config_path}: {e}. Using defaults.")
    
    return default_config

# Load config at module level
CONFIG = load_config()

# Cloud LLM provider registry (OpenRouter, NVIDIA, …) — enables Guardian to
# act as a unified LLM router alongside its local GPU-backed llama-server.
provider_registry = ProviderRegistry()

# Per-key cloud credential store — allows linking cloud provider credentials
# (NVIDIA, OpenRouter) to individual Guardian API keys so each key can route
# to its own cloud backend via guardian/{provider}/{model} routes.
cloud_cred_store = CloudCredentialStore()

# Cross-provider failover — lets a single logical model (e.g. minimax-m3) be
# served by multiple cloud providers via guardian/failover/{group} routes,
# automatically skipping a provider that is currently erroring/degraded.
failover_registry = FailoverRegistry()
_failover_health_cfg = CONFIG.get("failover_health", {}) or {}
failover_health = ProviderHealthTracker(
    failure_threshold=int(_failover_health_cfg.get("failure_threshold", FAILURE_THRESHOLD)),
    cooldown_seconds=float(_failover_health_cfg.get("cooldown_seconds", COOLDOWN_SECONDS)),
    rate_limit_cooldown_seconds=float(_failover_health_cfg.get("rate_limit_cooldown_seconds", RATE_LIMIT_COOLDOWN_SECONDS)),
)
cloud_rate_limiter = RateLimitRetryManager(
    RateLimitConfig.from_mapping(CONFIG.get("cloud_retry", {}))
)

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


def _enrich_anthropic_sse_line(line: str, *, input_tokens: int = 0, cache_read_tokens: int = 0) -> tuple[str, int, int]:
    """Enrich an Anthropic SSE line from llama-server with missing usage fields.

    llama-server's ``/v1/messages`` endpoint is missing:
    - ``input_tokens`` and ``cache_creation_input_tokens`` in ``message_delta`` usage
    - ``cache_creation_input_tokens`` in ``message_start`` usage

    Returns ``(enriched_line, new_input_tokens, new_cache_read_tokens)``.
    """
    if not line.startswith("data: "):
        return line, input_tokens, cache_read_tokens

    data_str = line[6:].strip()
    if not data_str:
        return line, input_tokens, cache_read_tokens

    try:
        data = json.loads(data_str)
    except (json.JSONDecodeError, TypeError):
        return line, input_tokens, cache_read_tokens

    changed = False

    # Enrich message_start usage
    if data.get("type") == "message_start":
        msg = data.get("message", {})
        usage = msg.get("usage", {})
        if isinstance(usage, dict):
            if "input_tokens" in usage:
                input_tokens = usage["input_tokens"]
            if "cache_read_input_tokens" in usage:
                cache_read_tokens = usage["cache_read_input_tokens"]
            if "cache_creation_input_tokens" not in usage:
                usage["cache_creation_input_tokens"] = 0
                changed = True
            msg["usage"] = usage

    # Enrich message_delta usage (cumulative — must include input_tokens)
    if data.get("type") == "message_delta":
        delta = data.get("delta", {})
        usage = data.get("usage", {})
        if isinstance(usage, dict):
            if "input_tokens" not in usage:
                usage["input_tokens"] = input_tokens
                changed = True
            if "cache_creation_input_tokens" not in usage:
                usage["cache_creation_input_tokens"] = 0
                changed = True
            if "cache_read_input_tokens" not in usage:
                usage["cache_read_input_tokens"] = cache_read_tokens
                changed = True
            data["usage"] = usage

        # Fix stop_reason: llama-server returns "end_turn" even when a
        # stop_sequence was matched. Anthropic expects "stop_sequence".
        if isinstance(delta, dict):
            if delta.get("stop_reason") == "end_turn" and delta.get("stop_sequence"):
                delta["stop_reason"] = "stop_sequence"
                changed = True

    if changed:
        return f"data: {json.dumps(data)}\n", input_tokens, cache_read_tokens

    return line, input_tokens, cache_read_tokens


def _enrich_anthropic_response(payload: dict) -> dict:
    """Enrich a non-streaming Anthropic response from llama-server with missing fields.

    llama-server's ``/v1/messages`` endpoint has several quirks:
    - Missing ``cache_creation_input_tokens`` and ``cache_read_input_tokens`` in usage
    - ``stop_reason`` is ``"end_turn"`` even when a ``stop_sequence`` was matched
    """
    usage = payload.get("usage", {})
    if isinstance(usage, dict):
        if "cache_creation_input_tokens" not in usage:
            usage["cache_creation_input_tokens"] = 0
        if "cache_read_input_tokens" not in usage:
            usage["cache_read_input_tokens"] = 0
        if "input_tokens" not in usage:
            usage["input_tokens"] = 0
        if "output_tokens" not in usage:
            usage["output_tokens"] = 0
        payload["usage"] = usage
    else:
        payload["usage"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    # Fix stop_reason: llama-server returns "end_turn" even when a stop_sequence
    # was matched. Anthropic expects "stop_sequence" in that case.
    if payload.get("stop_reason") == "end_turn" and payload.get("stop_sequence"):
        payload["stop_reason"] = "stop_sequence"

    return payload


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
    """Resolve an inference model name and reject unknown or unserved values.

    Cloud-provider models (OpenRouter, NVIDIA, …) are accepted as-is so they
    can be forwarded to their upstream API instead of the local backend.

    Per-key cloud routes using the ``guardian/{provider}/{model}`` convention
    are also accepted — the actual upstream model name is extracted at
    forwarding time once the requesting client's linked credential is known.
    """
    resolved_model = _resolve_inference_model(raw_model, current_model)
    if not resolved_model or resolved_model == "__MISMATCH__":
        _reject_unserved_inference_model(raw_model)
    if resolved_model in model_manager.models:
        return resolved_model
    if provider_registry.is_cloud_model(resolved_model):
        return resolved_model
    # Per-key cloud route: guardian/{provider}/{model_path}
    if parse_guardian_route(resolved_model) is not None:
        return resolved_model
    _reject_unserved_inference_model(raw_model)


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


# ── Capture helpers (fail-open, never block inference) ──────────────────

def _capture_client_fingerprint(request: Request, client_id: str) -> Optional[str]:
    """Extract the key fingerprint from the request's auth context for capture."""
    try:
        auth_context = get_request_auth_context(request) or {}
        fingerprint = auth_context.get("key_fingerprint")
        if isinstance(fingerprint, str) and fingerprint.strip():
            return fingerprint.strip()
    except Exception:
        pass
    return None


def _capture_ingress_protocol(path: str, endpoint: str) -> str:
    """Determine the ingress protocol for capture based on the route."""
    if endpoint.startswith("/v1/"):
        # Check if it's an Anthropic-style /v1/messages path
        if path == "messages" or endpoint == "/v1/messages":
            return "anthropic"
        return PROTOCOL_OPENAI
    elif endpoint.startswith("/api/chat"):
        return PROTOCOL_OLLAMA
    return PROTOCOL_OPENAI


def _capture_endpoint_from_request(request: Request) -> str:
    """Extract the canonical endpoint path from a request."""
    url_path = request.url.path if hasattr(request, "url") else ""
    if "/v1/" in url_path:
        # Strip the /v1/ prefix for canonical form
        return "/v1/" + url_path.split("/v1/", 1)[-1]
    return url_path or ""


def _dispatch_capture_request_received(
    request: Request,
    client_id: str,
    *,
    request_id: str,
    endpoint: str,
    ingress_protocol: str,
    route_type: str,
    requested_model: Optional[str],
    resolved_model: Optional[str] = None,
    request_messages: Optional[List[Dict[str, Any]]] = None,
    request_parameters: Optional[Dict[str, Any]] = None,
    queue_wait_ms: Optional[float] = None,
) -> Optional["PolicyResult"]:
    """Dispatch a request_received capture event (fail-open).

    Returns the PolicyResult so the caller can skip completed-event capture
    when the request was not captured.
    """
    try:
        controller = get_capture_controller()
        client_fingerprint = _capture_client_fingerprint(request, client_id)
        return controller.maybe_capture_request_received(
            request_id=request_id,
            client_fingerprint=client_fingerprint,
            endpoint=endpoint,
            ingress_protocol=ingress_protocol,
            route_type=route_type,
            requested_model=requested_model,
            resolved_model=resolved_model,
            request_messages=request_messages,
            request_parameters=request_parameters,
            queue_wait_ms=queue_wait_ms,
        )
    except Exception:
        return None


def _dispatch_capture_request_completed(
    ctx: BuildContext,
    *,
    policy_result: Optional["PolicyResult"] = None,
    response_content: Optional[str] = None,
    tool_calls: Optional[list] = None,
    tool_results: Optional[list] = None,
    reasoning_content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    http_status: Optional[int] = None,
    streamed: Optional[bool] = None,
    incomplete: Optional[bool] = None,
    attempts: Optional[int] = None,
) -> None:
    """Dispatch a request_completed capture event (fail-open)."""
    try:
        controller = get_capture_controller()
        controller.capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=response_content,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            http_status=http_status,
            streamed=streamed,
            incomplete=incomplete,
            attempts=attempts,
        )
    except Exception:
        pass


def _dispatch_capture_request_failed(
    ctx: BuildContext,
    *,
    error_code: str,
    http_status: Optional[int] = None,
    sanitized_message: Optional[str] = None,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    policy_result: Optional["PolicyResult"] = None,
) -> None:
    """Dispatch a request_failed capture event (fail-open)."""
    try:
        controller = get_capture_controller()
        controller.capture_request_failed(
            ctx,
            error_code=error_code,
            http_status=http_status,
            sanitized_message=sanitized_message,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            attempts=attempts,
        )
    except Exception:
        pass


def _dispatch_capture_request_cancelled(
    ctx: BuildContext,
    *,
    cancel_reason: str,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    policy_result: Optional["PolicyResult"] = None,
) -> None:
    """Dispatch a request_cancelled capture event (fail-open)."""
    try:
        controller = get_capture_controller()
        controller.capture_request_cancelled(
            ctx,
            cancel_reason=cancel_reason,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            attempts=attempts,
        )
    except Exception:
        pass


def _dispatch_capture_stream_completed(
    request: Request,
    request_id: str,
    client_id: str,
    model_name: str,
    ctx: Optional[BuildContext],
    policy_result: Optional["PolicyResult"],
    assembler: Optional[StreamResponseAssembler],
    usage_totals: Dict[str, Any],
    path: str,
    status_code: int,
) -> None:
    """Dispatch request_completed for the streaming path (fail-open).

    Uses the StreamResponseAssembler to reconstruct the full semantic response.
    """
    if ctx is None or policy_result is None or not policy_result.should_capture:
        return
    try:
        assembled = assembler.assemble() if assembler is not None else {"content": None}
        _dispatch_capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=assembled.get("content"),
            tool_calls=assembled.get("tool_calls"),
            finish_reason=assembled.get("finish_reason"),
            prompt_tokens=usage_totals.get("prompt_tokens") or None,
            completion_tokens=usage_totals.get("completion_tokens") or None,
            http_status=status_code,
            streamed=True,
            incomplete=assembled.get("incomplete"),
        )
    except Exception:
        pass


def _dispatch_capture_nonstream_completed(
    request: Request,
    request_id: str,
    client_id: str,
    model_name: str,
    ctx: Optional[BuildContext],
    policy_result: Optional["PolicyResult"],
    payload: Optional[Dict[str, Any]],
    status_code: int,
    request_start_time: float,
) -> None:
    """Dispatch request_completed for the non-streaming path (fail-open)."""
    if ctx is None or policy_result is None or not policy_result.should_capture:
        return
    try:
        response_content = None
        finish_reason = None
        prompt_tokens = None
        completion_tokens = None
        tool_calls = None

        if isinstance(payload, dict):
            # Check if this is an Anthropic-style response (has 'content' array, not 'choices')
            if "choices" not in payload and "content" in payload:
                # Anthropic /v1/messages response format
                # content is a list of content blocks
                response_content_parts: list[str] = []
                content_blocks = payload.get("content", [])
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict):
                            block_type = block.get("type", "")
                            if block_type == "text":
                                text = block.get("text", "")
                                if isinstance(text, str) and text:
                                    response_content_parts.append(text)
                            elif block_type == "tool_use":
                                # Tool use content — extract as a tool call
                                if "tool_calls" not in locals() or tool_calls is None:
                                    tool_calls = []
                                tool_calls.append({
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", ""),
                                        "arguments": json.dumps(block.get("input", {})) if isinstance(block.get("input"), dict) else str(block.get("input", "")),
                                    },
                                })
                if response_content_parts:
                    response_content = "\n".join(response_content_parts)
                finish_reason = payload.get("stop_reason")

                # Anthropic usage is at top-level 'usage' field
                usage = payload.get("usage", {})
                if isinstance(usage, dict):
                    prompt_tokens = _coerce_usage_int(usage.get("input_tokens", 0))
                    completion_tokens = _coerce_usage_int(usage.get("output_tokens", 0))

            else:
                # OpenAI chat/completions response format
                choices = payload.get("choices", [])
                if isinstance(choices, list) and choices:
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    message = first.get("message", first)
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            response_content = content
                        finish_reason = message.get("finish_reason")
                        tc = message.get("tool_calls")
                        if isinstance(tc, list):
                            tool_calls = tc
                    delta = first.get("delta", {})
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str) and not response_content:
                            response_content = content

                # Extract usage
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens = _coerce_usage_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
                    completion_tokens = _coerce_usage_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))

        _dispatch_capture_request_completed(
            ctx,
            policy_result=policy_result,
            response_content=response_content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_calls=tool_calls,
            http_status=status_code,
            streamed=False,
            incomplete=False,
            duration_ms=(time.monotonic() - request_start_time) * 1000,
        )
    except Exception:
        pass


def _classify_capture_error(exc: Exception) -> str:
    """Map an exception to a stable capture error code (never leaks internals)."""
    exc_name = type(exc).__name__
    mapping = {
        "ConnectError": "connection_error",
        "ConnectTimeout": "connection_timeout",
        "ReadTimeout": "read_timeout",
        "WriteTimeout": "write_timeout",
        "PoolTimeout": "pool_timeout",
        "TimeoutException": "timeout",
        "HTTPStatusError": "http_error",
        "ModelLoadError": "model_load_error",
        "HTTPException": "http_exception",
    }
    return mapping.get(exc_name, "internal_error")


def _sanitize_capture_error_message(exc: Exception) -> str:
    """Produce a sanitized error message for capture (no credentials/paths)."""
    exc_name = type(exc).__name__
    # Only return a generic description — never str(exc) which may contain paths
    return f"{exc_name}: request to backend failed"


def _messages_contain_image_input(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"image_url", "input_image", "image"}:
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
    if isinstance(template_kwargs, dict) and template_kwargs.get("enable_thinking") is False:
        return True
    # Anthropic format: thinking: {"type": "disabled"}
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return True
    return False


def _apply_anthropic_thinking_to_llama_params(payload: Dict[str, Any]) -> bool:
    """Convert Anthropic ``thinking`` config to llama-server parameters.

    llama-server's ``/v1/messages`` endpoint doesn't properly handle
    ``thinking: {type: "disabled"}`` — thinking stays enabled. This function
    translates the Anthropic thinking config to llama-server's native
    ``reasoning_budget`` and ``chat_template_kwargs.enable_thinking``
    parameters so that thinking is correctly controlled.

    Also handles ``thinking: {type: "enabled", budget_tokens: N}`` by
    setting ``reasoning_budget: N``.
    """
    thinking = payload.get("thinking")
    if not isinstance(thinking, dict):
        return False

    changed = False
    t_type = thinking.get("type", "")

    if t_type == "disabled":
        # Disable thinking entirely
        if payload.get("reasoning_budget") != 0:
            payload["reasoning_budget"] = 0
            changed = True
        template_kwargs = payload.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            template_kwargs = {}
            payload["chat_template_kwargs"] = template_kwargs
            changed = True
        if template_kwargs.get("enable_thinking") is not False:
            template_kwargs["enable_thinking"] = False
            changed = True

    elif t_type == "enabled":
        # Map budget_tokens → reasoning_budget
        budget = thinking.get("budget_tokens", 0)
        if budget and payload.get("reasoning_budget") != budget:
            payload["reasoning_budget"] = budget
            changed = True

    # type == "adaptive": leave as-is (llama-server's default behavior)
    return changed


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

    # Start capture writer (fail-open: disabled by default, errors are logged not raised)
    capture_writer_task: Optional[asyncio.Task] = None
    try:
        capture_controller.initialize_writer()
        if capture_controller.config.is_active:
            await capture_controller.start_writer()
            logger.info("📸 Capture writer started (instance_id=%s)",
                        capture_controller.config.instance_id)
        else:
            logger.info("📸 Capture subsystem is disabled (enabled=false)")
    except Exception as exc:
        logger.warning("Capture writer initialization failed (fail-open): %s", exc)

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

    # Shutdown: Stop capture writer
    try:
        await capture_controller.stop_writer()
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

app = FastAPI(lifespan=lifespan)

# CORS — allow the dashboard UI on :11437 to call the proxy API on :11434
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

model_manager = ModelManager()

# Initialize gateway context_metadata with singleton dependencies
_ctx_meta.init(model_manager, provider_registry, failover_registry)

DEFAULT_CONTEXT_WINDOW = _ctx_meta.DEFAULT_CONTEXT_WINDOW
BACKEND_CONTEXT_CACHE_SECONDS = 5.0
_backend_context_cache: Dict[str, Tuple[float, int]] = {}
_backend_context_lock = asyncio.Lock()
_context_fallback_warnings: set[str] = set()


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


def _get_cloud_key_fingerprint(request: Request, client_id: Optional[str]) -> str:
    """Return the stable Guardian-key identity used for cloud rate limiting."""
    auth_context = get_request_auth_context(request) or {}
    fingerprint = auth_context.get("key_fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint.strip()
    return str(client_id or "unknown-client")


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
    history_ttl=_queue_cfg.get("history_ttl", 300),
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

    # ── Cloud LLM router: forward to OpenRouter / NVIDIA / … ─────────
    # Ollama-style requests are translated to OpenAI format and forwarded
    # to the cloud provider directly.
    if _is_cloud_or_guardian_route(model):
        messages = body.get("messages", [])
        stream = body.get("stream", True)
        options = body.get("options", {})
        openai_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": options.get("temperature", 0.7),
        }
        return await _forward_to_cloud_provider(
            path="chat/completions",
            body=json.dumps(openai_body).encode("utf-8"),
            json_body=openai_body,
            model_name=model,
            request=request,
            client_id=client_id,
        )

    _ollama_request_start_time = time.monotonic()

    try:
        request_id, disconnect_task = await _begin_queued_request(request, client_id, model)
    except _GuardianRequestCancelled as exc:
        raise _request_cancel_http_exception(exc.request_id, exc.reason)

    # ── Capture: request_received event (fail-open, disabled by default) ──
    _capture_endpoint = "/api/chat"
    _capture_client_fp = _capture_client_fingerprint(request, client_id)
    _capture_policy_result = _dispatch_capture_request_received(
        request, client_id,
        request_id=request_id,
        endpoint=_capture_endpoint,
        ingress_protocol=PROTOCOL_OLLAMA,
        route_type=ROUTE_LOCAL,
        requested_model=model,
        resolved_model=model,
        request_messages=body.get("messages"),
        request_parameters={k: v for k, v in body.items() if k != "messages"},
        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
    )
    _capture_ctx: Optional[BuildContext] = None
    if _capture_policy_result is not None and _capture_policy_result.should_capture:
        _capture_ctx = BuildContext(
            request_id=request_id,
            endpoint=_capture_endpoint,
            ingress_protocol=PROTOCOL_OLLAMA,
            route_type=ROUTE_LOCAL,
            requested_model=model,
            resolved_model=model,
            capture_policy_version=capture_controller.config.policy_version
            if capture_controller is not None else "1.0.0",
            instance_id=capture_controller.config.instance_id
            if capture_controller is not None else "unknown",
            client_fingerprint=_capture_client_fp,
        )

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
            _ollama_capture_assembler: Optional[StreamResponseAssembler] = None
            if _capture_ctx is not None and _capture_policy_result is not None and _capture_policy_result.should_capture:
                _ollama_capture_assembler = StreamResponseAssembler()

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
                                        # ── Capture: feed SSE line to stream assembler ──
                                        if _ollama_capture_assembler is not None:
                                            try:
                                                _ollama_capture_assembler.add_sse_line(chunk)
                                            except Exception:
                                                pass
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
                except (asyncio.CancelledError, _GuardianRequestCancelled, httpx.StreamClosed, httpx.ReadError, httpx.RemoteProtocolError):
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
                    # ── Capture: request_completed (streaming) ──
                    if _capture_ctx is not None and _capture_policy_result is not None and _capture_policy_result.should_capture and _ollama_capture_assembler is not None:
                        try:
                            _dispatch_capture_stream_completed(
                                request, request_id, client_id,
                                model, _capture_ctx,
                                _capture_policy_result, _ollama_capture_assembler,
                                usage_totals, "chat/completions", r.status_code,
                            )
                        except Exception:
                            pass
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
                # ── Capture: request_cancelled (Ollama non-streaming) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - _ollama_request_start_time) * 1000,
                    )
                raise _request_cancel_http_exception(exc.request_id, exc.reason)
            except Exception as e:
                # ── Capture: request_failed (Ollama non-streaming) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_failed(
                        _capture_ctx,
                        error_code=_classify_capture_error(e),
                        http_status=500,
                        sanitized_message=_sanitize_capture_error_message(e),
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - _ollama_request_start_time) * 1000,
                    )
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
            else:
                # ── Capture: request_completed (Ollama non-streaming) ──
                if _capture_ctx is not None and _capture_policy_result is not None and _capture_policy_result.should_capture:
                    _dispatch_capture_nonstream_completed(
                        request, request_id, client_id,
                        model, _capture_ctx,
                        _capture_policy_result, data, r.status_code,
                        _ollama_request_start_time,
                    )
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

    # ── Cloud LLM router: forward to OpenRouter / NVIDIA / … ─────────
    if _is_cloud_or_guardian_route(model):
        messages = body.get("messages", [])
        stream = body.get("stream", True)
        openai_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        return await _forward_to_cloud_provider(
            path="chat/completions",
            body=json.dumps(openai_body).encode("utf-8"),
            json_body=openai_body,
            model_name=model,
            request=request,
            client_id=client_id,
        )

    _generate_request_start_time = time.monotonic()

    try:
        request_id, disconnect_task = await _begin_queued_request(request, client_id, model)
    except _GuardianRequestCancelled as exc:
        raise _request_cancel_http_exception(exc.request_id, exc.reason)

    # ── Capture: request_received event (fail-open, disabled by default) ──
    _capture_endpoint = "/api/chat"
    _capture_client_fp = _capture_client_fingerprint(request, client_id)
    _capture_policy_result = _dispatch_capture_request_received(
        request, client_id,
        request_id=request_id,
        endpoint=_capture_endpoint,
        ingress_protocol=PROTOCOL_OLLAMA,
        route_type=ROUTE_LOCAL,
        requested_model=model,
        resolved_model=model,
        request_messages=body.get("messages"),
        request_parameters={k: v for k, v in body.items() if k != "messages"},
        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
    )
    _capture_ctx: Optional[BuildContext] = None
    if _capture_policy_result is not None and _capture_policy_result.should_capture:
        _capture_ctx = BuildContext(
            request_id=request_id,
            endpoint=_capture_endpoint,
            ingress_protocol=PROTOCOL_OLLAMA,
            route_type=ROUTE_LOCAL,
            requested_model=model,
            resolved_model=model,
            capture_policy_version=capture_controller.config.policy_version
            if capture_controller is not None else "1.0.0",
            instance_id=capture_controller.config.instance_id
            if capture_controller is not None else "unknown",
            client_fingerprint=_capture_client_fp,
        )

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
                                        # ── Capture: feed SSE line to stream assembler ──
                                        if _ollama_capture_assembler is not None:
                                            try:
                                                _ollama_capture_assembler.add_sse_line(chunk)
                                            except Exception:
                                                pass
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
                except (asyncio.CancelledError, _GuardianRequestCancelled, httpx.StreamClosed, httpx.ReadError, httpx.RemoteProtocolError):
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
                    # ── Capture: request_completed (streaming) ──
                    if _capture_ctx is not None and _capture_policy_result is not None and _capture_policy_result.should_capture and _ollama_capture_assembler is not None:
                        try:
                            _dispatch_capture_stream_completed(
                                request, request_id, client_id,
                                model, _capture_ctx,
                                _capture_policy_result, _ollama_capture_assembler,
                                usage_totals, "chat/completions", r.status_code,
                            )
                        except Exception:
                            pass
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
                # ── Capture: request_cancelled (Ollama non-streaming) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - _generate_request_start_time) * 1000,
                    )
                raise _request_cancel_http_exception(exc.request_id, exc.reason)
            except Exception as e:
                # ── Capture: request_failed (Ollama non-streaming) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_failed(
                        _capture_ctx,
                        error_code=_classify_capture_error(e),
                        http_status=500,
                        sanitized_message=_sanitize_capture_error_message(e),
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - _generate_request_start_time) * 1000,
                    )
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
            else:
                # ── Capture: request_completed (Ollama non-streaming) ──
                if _capture_ctx is not None and _capture_policy_result is not None and _capture_policy_result.should_capture:
                    _dispatch_capture_nonstream_completed(
                        request, request_id, client_id,
                        model, _capture_ctx,
                        _capture_policy_result, data, r.status_code,
                        _generate_request_start_time,
                    )
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


# ── Context metadata helpers (delegated to app.gateway.context_metadata) ──
# Phase 5: extracted to app/gateway/context_metadata.py.  These thin
# wrappers preserve the existing call sites in server.py.

def _apply_context_metadata(model_entry: Dict[str, Any], context_window: int) -> Dict[str, Any]:
    """Delegate to app.gateway.context_metadata.apply_context_metadata."""
    return _ctx_meta.apply_context_metadata(model_entry, context_window)


async def _get_loaded_backend_context_window(canonical_name: str) -> Optional[int]:
    """Delegate to app.gateway.context_metadata.get_loaded_backend_context_window."""
    return await _ctx_meta.get_loaded_backend_context_window(canonical_name)


async def _resolve_context_window(
    public_name: str,
    canonical_name: Optional[str] = None,
    cloud_attempts: Optional[List[Tuple[CloudProvider, str]]] = None,
) -> int:
    """Delegate to app.gateway.context_metadata.resolve_context_window."""
    return await _ctx_meta.resolve_context_window(public_name, canonical_name, cloud_attempts)


def _warn_context_fallback(model_name: str) -> None:
    """Delegate to app.gateway.context_metadata.warn_context_fallback."""
    _ctx_meta.warn_context_fallback(model_name)


async def _enrich_model_context_metadata(
    model_entry: Dict[str, Any],
    canonical_name: Optional[str] = None,
    cloud_attempts: Optional[List[Tuple[CloudProvider, str]]] = None,
) -> Dict[str, Any]:
    """Delegate to app.gateway.context_metadata.enrich_model_context_metadata."""
    return await _ctx_meta.enrich_model_context_metadata(model_entry, canonical_name, cloud_attempts)


async def _build_model_metadata_entry(public_name: str, canonical_name: str, client_id: str) -> Dict[str, Any]:
    """Delegate to app.gateway.context_metadata.build_model_metadata_entry."""
    return await _ctx_meta.build_model_metadata_entry(public_name, canonical_name, client_id)


@app.get("/v1/models")
async def list_models(request: Request, client_id: str = Depends(verify_api_key)):
    """List available models from config and cloud providers.

    Returns local GPU-backed models, global cloud-provider models from
    settings.yaml, and per-key cloud routes (guardian/{provider}/{model})
    linked to the requesting client's API key.
    """
    models_list = []
    try:
        for public_name, canonical_name in model_manager.get_public_model_map().items():
            models_list.append(await _build_model_metadata_entry(public_name, canonical_name, client_id))
    except Exception as e:
        logger.error(f"Failed to list models: {e}")

    # Append global cloud-provider models (OpenRouter, NVIDIA, …)
    try:
        for cloud_model in provider_registry.get_all_cloud_models():
            entry = provider_registry.build_model_metadata_entry(cloud_model)
            if entry is not None:
                models_list.append(await _enrich_model_context_metadata(entry))
                provider = provider_registry.get_provider_for_model(cloud_model)
                if provider is not None and provider.name == "openrouter":
                    alias_entry = dict(entry)
                    alias_entry["id"] = f"openrouter/{cloud_model}"
                    models_list.append(await _enrich_model_context_metadata(alias_entry))
    except Exception as e:
        logger.error(f"Failed to list cloud models: {e}")

    # Append per-key cloud routes (guardian/{provider}/{model})
    try:
        auth_ctx = get_request_auth_context(request) or {}
        key_fp = auth_ctx.get("key_fingerprint") or client_id
        for cloud_model in cloud_cred_store.get_linked_models_for_key(key_fp):
            entry = {
                "id": cloud_model["id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": cloud_model["provider"],
                "permission": [],
                "served_by": "cloud",
                "provider": cloud_model["provider"],
                "credential_id": cloud_model["credential_id"],
            }
            credential = cloud_cred_store.get_credential_for_key(key_fp, cloud_model["provider"])
            cloud_attempts = None
            if credential is not None:
                cloud_attempts = [
                    (
                        CloudProvider(
                            name=cloud_model["provider"],
                            base_url=_provider_base_url(cloud_model["provider"]),
                            api_key=credential.api_key,
                            models=[cloud_model["model"]],
                        ),
                        cloud_model["model"],
                    )
                ]
            models_list.append(await _enrich_model_context_metadata(entry, cloud_attempts=cloud_attempts))
    except Exception as e:
        logger.error(f"Failed to list per-key cloud models: {e}")

    # Append failover groups as synthetic model entries (guardian/failover/{group}).
    # A failover group spans multiple providers; surface it so discovery clients
    # (Goose, Open WebUI, etc.) can offer cross-provider failover routes without
    # the caller needing to know the underlying (provider, model) candidates.
    try:
        for group_name in failover_registry._groups.keys():
            try:
                cloud_attempts, _ = _resolve_cloud_attempts(
                    f"guardian/failover/{group_name}",
                    request,
                    client_id,
                )
            except HTTPException as exc:
                if exc.status_code == 403:
                    logger.debug("Skipping unauthorized failover group '%s' from discovery", group_name)
                    continue
                raise
            entry = {
                "id": f"guardian/failover/{group_name}",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "failover",
                "permission": [],
                "served_by": "failover",
                "provider": "failover",
                "failover_group": group_name,
            }
            models_list.append(
                await _enrich_model_context_metadata(entry, cloud_attempts=cloud_attempts)
            )
    except Exception as e:
        logger.error(f"Failed to list failover groups: {e}")

    return {"object": "list", "data": models_list}


@app.get("/v1/models/{model_id:path}")
async def get_model_metadata(
    model_id: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """Return metadata for a configured canonical model, public alias, or cloud model."""
    # Failover groups surface as guardian/failover/{group}; resolve them here so
    # /v1/models/<id> returns a stable shape rather than 404'ing on the discovery
    # entry the list endpoint just advertised.
    if model_id.startswith("guardian/failover/"):
        group_name = model_id[len("guardian/failover/"):]
        if failover_registry.get_group(group_name) is not None:
            cloud_attempts, _ = _resolve_cloud_attempts(model_id, request, client_id)
            return await _enrich_model_context_metadata({
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "failover",
                "permission": [],
                "served_by": "failover",
                "provider": "failover",
                "failover_group": group_name,
            }, cloud_attempts=cloud_attempts)
        raise HTTPException(status_code=404, detail=f"Failover group '{group_name}' not found")

    # Cloud-provider models first (they may contain slashes like "openai/gpt-4o")
    if provider_registry.is_cloud_model(model_id):
        entry = provider_registry.build_model_metadata_entry(model_id)
        if entry is not None:
            return await _enrich_model_context_metadata(entry)

    guardian_route = parse_guardian_route(model_id)
    if guardian_route is not None:
        provider_name, _ = guardian_route
        cloud_attempts, _ = _resolve_cloud_attempts(model_id, request, client_id)
        return await _enrich_model_context_metadata({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": provider_name,
            "permission": [],
            "served_by": "cloud",
            "provider": provider_name,
        }, cloud_attempts=cloud_attempts)

    public_models = model_manager.get_public_model_map()
    canonical_name = public_models.get(model_id)
    if canonical_name is None:
        try:
            canonical_name = model_manager.resolve_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await _build_model_metadata_entry(model_id, canonical_name, client_id)


@app.post("/api/show")
async def show_model_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Return Ollama-compatible metadata with an always-present context size."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    model_name = body.get("model", body.get("name"))
    if not isinstance(model_name, str) or not model_name.strip():
        raise HTTPException(status_code=400, detail="'model' must be a non-empty string")
    model_name = model_name.strip()

    canonical_name: Optional[str] = None
    cloud_attempts: Optional[List[Tuple[CloudProvider, str]]] = None
    guardian_route = parse_guardian_route(model_name)
    if model_name.startswith("guardian/failover/"):
        group_name = model_name[len("guardian/failover/"):]
        if failover_registry.get_group(group_name) is None:
            raise HTTPException(status_code=404, detail=f"Failover group '{group_name}' not found")
        cloud_attempts, _ = _resolve_cloud_attempts(model_name, request, client_id)
    elif guardian_route is not None:
        cloud_attempts, _ = _resolve_cloud_attempts(model_name, request, client_id)
    elif not provider_registry.is_cloud_model(model_name):
        try:
            canonical_name = model_manager.resolve_model(model_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    context_window = await _resolve_context_window(
        model_name,
        canonical_name,
        cloud_attempts,
    )
    return {
        "modelfile": "",
        "parameters": f"num_ctx {context_window}",
        "template": "",
        "details": {"family": "guardian"},
        "model_info": {
            "general.context_length": context_window,
            "guardian.context_length": context_window,
        },
        "model": model_name,
        "context_window": context_window,
    }


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


# ── Cloud credential management API ───────────────────────────────────


@app.get("/api/keys")
async def list_api_keys(client_id: str = Depends(verify_api_key)):
    """List all Guardian API keys (tokens masked, fingerprints shown)."""
    keys = load_api_keys()
    result = []
    for token, data in keys.items():
        result.append({
            "key_fingerprint": _token_fingerprint(token),
            "key_prefix": token.split("_")[0] if "_" in token else "legacy",
            "name": data.get("name"),
            "created_at": data.get("created_at"),
            "metadata": data.get("metadata", {}),
        })
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"keys": result}


@app.post("/api/keys")
async def create_api_key(request: Request, client_id: str = Depends(verify_api_key)):
    """Generate a new Guardian API key.

    Body: ``{"name": "my-app", "prefix": "myapp", "metadata": {"client": "my-app"}}``
    Returns the full API key (only shown once).
    """
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    prefix = body.get("prefix")
    metadata = body.get("metadata")
    api_key = generate_api_key(name, metadata=metadata, prefix=prefix)
    logger.info("🔑 Admin '%s' generated new API key for '%s'", client_id, name)
    return {
        "api_key": api_key,
        "key_fingerprint": _token_fingerprint(api_key),
        "name": name,
        "message": "Store this key securely — it will not be shown again.",
    }


@app.get("/api/cloud/credentials")
async def list_cloud_credentials(request: Request, client_id: str = Depends(verify_api_key)):
    """List cloud credentials owned by the authenticated Guardian key."""
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return {"credentials": cloud_cred_store.list_credentials_for_owner(owner_key_fingerprint)}


@app.post("/api/cloud/credentials")
async def add_cloud_credential(request: Request, client_id: str = Depends(verify_api_key)):
    """Add a new cloud provider credential.

    Body: ``{"provider": "nvidia", "name": "NVIDIA Default", "api_key": "nvapi-xxx", "models": ["minimax/minimax-m3"]}``
    """
    body = await request.json()
    provider = str(body.get("provider", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    models = body.get("models") or []
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    if not isinstance(models, list):
        raise HTTPException(status_code=400, detail="models must be a list")
    if provider == "google":
        models = await _discover_google_models(api_key)
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    cred = await cloud_cred_store.add_credential(
        provider=provider,
        name=name,
        api_key=api_key,
        models=[str(m) for m in models if m],
        owner_key_fingerprint=owner_key_fingerprint,
    )
    logger.info("☁️  Admin '%s' added cloud credential '%s' for provider '%s'", client_id, cred["id"], provider)
    return cred


@app.post("/api/cloud/credentials/{cred_id}/refresh-models")
async def refresh_cloud_credential_models(
    cred_id: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """Refresh the Google model catalog stored for a cloud credential."""
    credential = cloud_cred_store.get_credential_by_id(cred_id)
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if credential is None or not cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    if credential.provider != "google":
        raise HTTPException(
            status_code=400,
            detail="Automatic model refresh is currently supported only for Google credentials",
        )

    models = await _discover_google_models(credential.api_key)
    replaced = await cloud_cred_store.replace_models_for_credential(cred_id, models)
    if not replaced:
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    logger.info(
        "☁️  Admin '%s' refreshed %d Google model(s) for credential '%s'",
        client_id,
        len(models),
        cred_id,
    )
    return {
        "status": "refreshed",
        "credential_id": cred_id,
        "model_count": len(models),
        "models": models,
    }


@app.delete("/api/cloud/credentials/{cred_id}")
async def delete_cloud_credential(
    cred_id: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """Delete a cloud provider credential and all its links."""
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    deleted = await cloud_cred_store.delete_credential(cred_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    logger.info("☁️  Admin '%s' deleted cloud credential '%s'", client_id, cred_id)
    return {"status": "deleted", "credential_id": cred_id}


@app.post("/api/cloud/credentials/{cred_id}/models")
async def add_model_to_credential(cred_id: str, request: Request, client_id: str = Depends(verify_api_key)):
    """Add a model to an existing credential's model list.

    Body: ``{"model": "minimax/minimax-m3"}``
    """
    body = await request.json()
    model_name = str(body.get("model", "")).strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    added = await cloud_cred_store.add_model_to_credential(cred_id, model_name)
    if not added:
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found or model already present")
    return {"status": "added", "credential_id": cred_id, "model": model_name}


@app.delete("/api/cloud/credentials/{cred_id}/models/{model_name:path}")
async def remove_model_from_credential(
    cred_id: str,
    model_name: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """Remove a model from a credential's model list."""
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail="Credential or model not found")
    removed = await cloud_cred_store.remove_model_from_credential(cred_id, model_name)
    if not removed:
        raise HTTPException(status_code=404, detail="Credential or model not found")
    return {"status": "removed", "credential_id": cred_id, "model": model_name}


@app.get("/api/cloud/links")
async def list_cloud_links(request: Request, client_id: str = Depends(verify_api_key)):
    """List links for cloud credentials owned by the authenticated Guardian key."""
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return {"links": cloud_cred_store.list_links_for_owner(owner_key_fingerprint)}


@app.get("/api/cloud/ratelimit-stats")
async def get_cloud_ratelimit_stats(request: Request, client_id: str = Depends(verify_api_key)):
    """Return current per-key cloud 429 counters and provider hints."""
    key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return cloud_rate_limiter.get_stats(key_fingerprint)


@app.post("/api/cloud/links")
async def link_credential(request: Request, client_id: str = Depends(verify_api_key)):
    """Link a cloud credential to a Guardian API key.

    Body: ``{"guardian_key_fingerprint": "abc123...", "provider": "nvidia", "credential_id": "cred_001"}``
    """
    body = await request.json()
    key_fp = str(body.get("guardian_key_fingerprint", "")).strip()
    provider = str(body.get("provider", "")).strip().lower()
    cred_id = str(body.get("credential_id", "")).strip()
    if not key_fp:
        raise HTTPException(status_code=400, detail="guardian_key_fingerprint is required")
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    if not cred_id:
        raise HTTPException(status_code=400, detail="credential_id is required")
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail="Credential not found")
    linked = await cloud_cred_store.link_credential(key_fp, provider, cred_id)
    if not linked:
        raise HTTPException(status_code=404, detail="Credential not found")
    logger.info("☁️  Admin '%s' linked credential '%s' to key '%s' for provider '%s'", client_id, cred_id, key_fp, provider)
    return {"status": "linked", "guardian_key_fingerprint": key_fp, "provider": provider, "credential_id": cred_id}


@app.delete("/api/cloud/links")
async def unlink_credential(request: Request, client_id: str = Depends(verify_api_key)):
    """Unlink a cloud credential from a Guardian API key.

    Body: ``{"guardian_key_fingerprint": "abc123...", "provider": "nvidia"}``
    """
    body = await request.json()
    key_fp = str(body.get("guardian_key_fingerprint", "")).strip()
    provider = str(body.get("provider", "")).strip().lower()
    if not key_fp:
        raise HTTPException(status_code=400, detail="guardian_key_fingerprint is required")
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    credential = cloud_cred_store.get_credential_for_key(key_fp, provider)
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if credential is None or not cloud_cred_store.is_credential_owned_by(credential.id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail="Link not found")
    unlinked = await cloud_cred_store.unlink_credential(key_fp, provider)
    if not unlinked:
        raise HTTPException(status_code=404, detail="Link not found")
    return {"status": "unlinked", "guardian_key_fingerprint": key_fp, "provider": provider}


@app.get("/api/cloud/providers")
async def list_cloud_providers(client_id: str = Depends(verify_api_key)):
    """List all configured cloud providers and their status."""
    providers = []
    for p in provider_registry.get_enabled_providers():
        providers.append({
            "name": p.name,
            "base_url": p.base_url,
            "configured": p.is_configured,
            "model_count": len(p.models),
            "models": p.models,
        })
    # Include known providers even if not in settings.yaml
    known = set(_PROVIDER_BASE_URLS.keys())
    configured = {p["name"] for p in providers}
    for name in known - configured:
        providers.append({"name": name, "base_url": _PROVIDER_BASE_URLS[name], "configured": False, "model_count": 0, "models": []})
    return {"providers": providers}


@app.get("/api/cloud/models")
async def list_cloud_models(request: Request, client_id: str = Depends(verify_api_key)):
    """List all cloud models available to the requesting client.

    Combines global cloud models from settings.yaml with per-key cloud routes
    linked to the requesting Guardian API key.
    """
    models = []
    # Global cloud models
    for model_name in provider_registry.get_all_cloud_models():
        entry = provider_registry.build_model_metadata_entry(model_name)
        if entry:
            models.append(entry)
    # Per-key cloud routes
    auth_ctx = get_request_auth_context(request) or {}
    key_fp = auth_ctx.get("key_fingerprint") or client_id
    for cloud_model in cloud_cred_store.get_linked_models_for_key(key_fp):
        models.append({
            "id": cloud_model["id"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": cloud_model["provider"],
            "permission": [],
            "served_by": "cloud",
            "provider": cloud_model["provider"],
            "credential_id": cloud_model["credential_id"],
        })
    return {"models": models}


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


# --- Capture status endpoint (admin) ---

@app.get("/api/capture/status")
async def get_capture_status(client_id: str = Depends(verify_api_key)):
    """Return capture subsystem status, config, and runtime metrics.

    Requires an API key.  Shows whether capture is enabled, the kill switch
    state, queue depth, writer status, and disk usage.
    """
    controller = get_capture_controller()
    cfg = controller.config

    # Build config summary (without secrets)
    config_summary = {
        "enabled": cfg.enabled,
        "active": cfg.is_active,
        "local_capture": cfg.local_capture,
        "cloud_capture": cfg.cloud_capture,
        "per_client_opt_in": cfg.per_client_opt_in,
        "allowed_client_refs_count": len(cfg.allowed_client_refs),
        "policy_version": cfg.policy_version,
        "instance_id": cfg.instance_id,
        "capture_root": cfg.capture_root,
        "retention_days": cfg.retention_days,
        "max_capture_bytes": cfg.max_capture_bytes,
        "max_pending_events": cfg.max_pending_events,
        "max_file_bytes": cfg.max_file_bytes,
        "max_file_age_seconds": cfg.max_file_age_seconds,
        "file_mode": oct(cfg.file_mode),
        "directory_mode": oct(cfg.directory_mode),
        "field_policies": {
            "system_prompts": cfg.system_prompts,
            "reasoning": cfg.reasoning,
            "tool_definitions": cfg.tool_definitions,
            "tool_calls": cfg.tool_calls,
            "tool_results": cfg.tool_results,
            "images": cfg.images,
            "unknown_content_blocks": cfg.unknown_content_blocks,
        },
    }

    # Sink snapshot (queue depth, dropped events, etc.)
    sink_snap = controller.sink.snapshot()

    # Writer snapshot (if writer exists)
    writer_snap = {}
    if controller.writer is not None:
        writer_snap = controller.writer.snapshot()
        writer_snap["running"] = controller._writer_started
    else:
        writer_snap = {"running": False, "reason": "writer_not_initialized"}

    # Disk usage
    disk_bytes = 0
    capture_root_path = None
    if controller.writer is not None:
        disk_bytes = writer_snap.get("capture_disk_bytes", 0) or 0
        capture_root_path = str(controller.writer.get_write_path())
    else:
        try:
            root = __import__("pathlib").Path(cfg.capture_root).resolve()
            if root.exists():
                capture_root_path = str(root)
                disk_bytes = sum(
                    f.stat().st_size for f in root.rglob("*") if f.is_file()
                )
        except OSError:
            pass

    return {
        "config": config_summary,
        "sink": sink_snap,
        "writer": writer_snap,
        "disk": {
            "bytes_used": disk_bytes,
            "bytes_budget": cfg.max_capture_bytes,
            "root": capture_root_path,
            "retention_days": cfg.retention_days,
        },
    }


@app.post("/api/capture/rotate")
async def rotate_capture_file(client_id: str = Depends(verify_api_key)):
    """Force rotation of the active capture WAL file.

    Requires an API key.  The current active file is closed (and gzipped
    if compression is configured), and a new active file is opened for
    subsequent events.
    """
    controller = get_capture_controller()
    if not controller.config.is_active:
        return {"message": "Capture is not active", "rotated": False}

    writer = controller.writer
    if writer is None:
        return {"message": "Capture writer is not initialized", "rotated": False}

    rotated_path = None
    active_path = None
    try:
        rotated_path = writer.rotate()
        active_path = str(writer.get_write_path())
    except Exception as e:
        return {"message": f"Rotation failed: {e}", "rotated": False}

    return {
        "message": "Rotation complete",
        "rotated": True,
        "rotated_file": rotated_path,
        "active_file": active_path,
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
    update_capture_metrics(get_capture_sink_snapshot())
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


# ── Cloud LLM router: forward to OpenRouter / NVIDIA / … ─────────────

_PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "poolside": "https://inference.poolside.ai/v1",
    # Direct OpenAI (service-account key stored in ${OPENAI_API_KEY}).  OpenAI
    # uses BARE model names — no namespace — so global recognition comes from
    # the explicit ``models:`` list in settings.yaml, and per-key routes use
    # the guardian/openai/{model} convention (any model, no listing required).
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}

_GOOGLE_MODEL_CATALOG_URL = f"{_PROVIDER_BASE_URLS['google']}/models"
_GOOGLE_MODEL_CATALOG_TIMEOUT_S = 30.0


def _normalize_google_model_id(model_id: str) -> str:
    """Normalize Google catalog IDs to bare OpenAI-compatible model names.

    Google's ``/v1beta/openai/models`` listing may return resource-style IDs
    such as ``models/gemini-2.5-flash``. Chat completions expect the bare name
    (``gemini-2.5-flash``), so strip a single leading ``models/`` prefix.
    """
    normalized = model_id.strip()
    if normalized.startswith("models/"):
        normalized = normalized[len("models/") :].strip()
    return normalized


def _parse_google_model_catalog(payload: Any) -> List[str]:
    """Validate and normalize Google OpenAI-compatible model catalog data."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Google model catalog response is missing model data")
    models = sorted(
        {
            normalized
            for entry in payload["data"]
            if isinstance(entry, dict)
            for model_id in [entry.get("id")]
            if isinstance(model_id, str)
            for normalized in [_normalize_google_model_id(model_id)]
            if normalized
        }
    )
    if not models:
        raise ValueError("Google model catalog response has no model data")
    return models


async def _discover_google_models(api_key: str) -> List[str]:
    """Fetch the current Google AI Studio OpenAI-compatible model catalog."""
    try:
        timeout = httpx.Timeout(_GOOGLE_MODEL_CATALOG_TIMEOUT_S, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                _GOOGLE_MODEL_CATALOG_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        response.raise_for_status()
        return _parse_google_model_catalog(response.json())
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        logger.warning("Google model catalog discovery failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail={
                "error": "google_model_discovery_failed",
                "message": "Google model catalog could not be retrieved.",
            },
        ) from exc


def _provider_base_url(provider_name: str) -> str:
    """Return the base URL for a known provider, or empty string."""
    return _PROVIDER_BASE_URLS.get(provider_name, "")


def _cloud_provider_for_request(model_name: str) -> Optional[CloudProvider]:
    """Return the configured cloud provider for *model_name*, or None."""
    return provider_registry.get_provider_for_model(model_name)


def _is_cloud_or_guardian_route(model_name: str) -> bool:
    """Check if a model name is a cloud model or a per-key guardian route."""
    if provider_registry.is_cloud_model(model_name):
        return True
    return parse_guardian_route(model_name) is not None


def _cloud_provider_unavailable_error(provider: CloudProvider) -> HTTPException:
    """Build a 503 error for a provider that lacks an API key."""
    return HTTPException(
        status_code=503,
        detail={
            "error": "provider_unavailable",
            "reason": "missing_api_key",
            "message": (
                f"Cloud provider '{provider.name}' is enabled but has no API key "
                f"configured. Set the {provider.name.upper()}_API_KEY environment "
                f"variable or disable the provider in settings.yaml."
            ),
            "provider": provider.name,
        },
    )


#: Upstream status codes worth retrying against the next failover candidate.
#: A 429 is handled first by the per-key retry manager. If that local hold
#: budget is exhausted, the failover route may try its next candidate. A 429
#: never counts against the provider health tracker because rate limiting does
#: not by itself indicate a broken provider.
_RETRYABLE_STATUS_CODES = {408, 409, 425, 500, 502, 503, 504}

#: Some providers (observed on NVIDIA NIM) report a degraded/unavailable
#: backend as an HTTP 400 with a descriptive message instead of a 5xx, e.g.
#: ``"Function id '...': DEGRADED function cannot be invoked"``. These
#: substrings (checked case-insensitively against the error body) are treated
#: as retryable even though the status code itself is not in
#: :data:`_RETRYABLE_STATUS_CODES`.
_DEGRADED_ERROR_MARKERS = (
    "degraded function",
    "function cannot be invoked",
    "service is degraded",
    "service unavailable",
    "temporarily unavailable",
)


def _is_retryable_cloud_error(status_code: int, error_body_text: str) -> bool:
    """Return True if a failover candidate's error is worth retrying on the next one.

    A final 429 qualifies after the per-key retry manager has exhausted its
    local hold budget, allowing a failover group to try its next provider.
    Standard retryable status codes (5xx/408/409/425) always qualify. A 400
    also qualifies when its body matches a known "provider is degraded"
    pattern — other 400s (malformed request, bad schema) are left alone since
    they would fail identically on every candidate.
    """
    if status_code == 429:
        return True
    if status_code in _RETRYABLE_STATUS_CODES:
        return True
    if status_code == 400 and error_body_text:
        lowered = error_body_text.lower()
        return any(marker in lowered for marker in _DEGRADED_ERROR_MARKERS)
    return False


_HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # Recompute body framing locally. Upstream providers (notably Google)
        # may emit both Content-Length and Transfer-Encoding, which nginx
        # rejects as 502 when Guardian is fronted by the loopback HTTP proxy.
        "content-length",
        # Body is already decompressed by httpx; forwarding content-encoding
        # would lie about the bytes we return.
        "content-encoding",
    }
)


def _sanitize_proxied_response_headers(headers: Any) -> Dict[str, str]:
    """Strip hop-by-hop and body-framing headers from an upstream response."""
    return {
        key: value
        for key, value in dict(headers or {}).items()
        if key.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }


def _guardian_debug_headers(
    provider: CloudProvider,
    upstream_model: str,
    failover_group: Optional[str],
) -> Dict[str, str]:
    """Build response headers revealing which provider actually served a request.

    Claude Code's own model badge is a static label set once at launch
    (``ANTHROPIC_DEFAULT_SONNET_MODEL_NAME``) and never updates per-turn, so
    it cannot show which failover candidate answered a given request. These
    headers — plus the ``@provider`` suffix applied to the translated Anthropic
    response's ``model`` field for failover routes — are the only per-request
    signal of the winning provider; inspect them via ``claude --verbose``
    network traces or Guardian's own logs (``journalctl -u llama-guardian.service
    | grep 'Cloud route'``).
    """
    headers = {
        "X-Guardian-Provider": provider.name,
        "X-Guardian-Upstream-Model": upstream_model,
    }
    if failover_group:
        headers["X-Guardian-Failover-Group"] = failover_group
    return headers


def _resolve_cloud_attempts(
    model_name: str,
    request: Request,
    client_id: str,
    *,
    requires_vision: bool = False,
) -> Tuple[List[Tuple[CloudProvider, str]], Optional[str]]:
    """Resolve the ordered list of ``(provider, upstream_model)`` attempts.

    Returns ``(attempts, failover_group_name)``. *failover_group_name* is only
    set for ``guardian/failover/{group}`` routes (used for logging); every
    other route resolves to exactly one attempt.

    Raises the same ``HTTPException``s the single-provider code used to raise
    when a route, credential, or provider cannot be resolved.
    """
    guardian_route = parse_guardian_route(model_name)
    if guardian_route is None:
        # Global cloud model from settings.yaml providers config
        provider = _cloud_provider_for_request(model_name)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' is not a cloud model")
        if not provider.is_configured:
            raise _cloud_provider_unavailable_error(provider)
        return [(provider, ProviderRegistry.canonical_model_id(model_name))], None

    provider_name, model_path = guardian_route
    auth_ctx = get_request_auth_context(request) or {}
    key_fingerprint = auth_ctx.get("key_fingerprint") or client_id

    if provider_name == "failover":
        # guardian/failover/{group}: try each candidate in the group,
        # health-ordered, until one succeeds.
        group = failover_registry.get_group(model_path)
        if group is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "failover_group_not_found",
                    "group": model_path,
                    "message": f"No failover group named '{model_path}' is configured.",
                },
            )
        ordered = failover_health.order_candidates(group.candidates)
        attempts: List[Tuple[CloudProvider, str]] = []
        for candidate in ordered:
            if requires_vision and "image" not in candidate.modalities:
                continue
            cred = cloud_cred_store.get_credential_for_key(key_fingerprint, candidate.provider)
            if cred is None:
                continue
            if candidate.provider == "google" and candidate.model not in cred.models:
                continue
            attempts.append((
                CloudProvider(
                    name=candidate.provider,
                    base_url=_provider_base_url(candidate.provider),
                    api_key=cred.api_key,
                    models=[candidate.model],
                ),
                candidate.model,
            ))
        if not attempts:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "cloud_credential_not_linked",
                    "reason": "no_credential_for_failover_group",
                    "message": (
                        f"No credential is linked to your Guardian API key for any "
                        f"provider in failover group '{model_path}'."
                    ),
                    "group": model_path,
                },
            )
        return attempts, model_path

    # Per-key cloud route: guardian/{provider}/{model_path}
    cred = cloud_cred_store.get_credential_for_key(key_fingerprint, provider_name)
    if cred is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "cloud_credential_not_linked",
                "reason": "no_credential_for_provider",
                "message": (
                    f"No {provider_name} credential is linked to your Guardian API key. "
                    f"Link a credential via the Guardian admin API or dashboard."
                ),
                "provider": provider_name,
                "requested_route": model_name,
            },
        )
    if provider_name == "google" and model_path not in cred.models:
        raise HTTPException(
            status_code=404,
            detail=f"Google model '{model_path}' is not available for this credential",
        )
    provider = CloudProvider(
        name=provider_name,
        base_url=_provider_base_url(provider_name),
        api_key=cred.api_key,
        models=[model_path],
    )
    return [(provider, model_path)], None


def _resolve_cloud_vision_fallback(model_name: str) -> Optional[str]:
    """Return a local vision fallback for a text-only cloud model.

    Direct provider routes and global cloud model routes resolve the fallback
    from their upstream model name. Failover routes retain their group-level
    behavior: if any candidate accepts images, the cloud group handles them.
    """
    guardian_route = parse_guardian_route(model_name)
    if guardian_route is None:
        return failover_registry.get_image_fallback_for_model(
            ProviderRegistry.canonical_model_id(model_name)
        )
    provider_name, model_path = guardian_route
    if provider_name != "failover":
        return failover_registry.get_image_fallback_for_model(model_path)
    group = failover_registry.get_group(model_path)
    if group is None or group.has_image_capable_candidate():
        return None
    return group.image_fallback_model


# ── OpenAI reasoning-model parameter adaptation ───────────────────────

#: OpenAI model prefixes whose API rejects ``max_tokens`` in favour of
#: ``max_completion_tokens``.  This covers the entire ``o1``/``o3``/``o4``
#: reasoning family and the ``gpt-5*`` generation.
_OPENAI_REASONING_MODEL_PREFIXES: Tuple[str, ...] = (
    "o1", "o3", "o4", "gpt-5",
)

#: Models that reject ``temperature`` entirely (o-series) or only accept the
#: default value of 1 (gpt-5*).
_OPENAI_TEMP_RESTRICTED_PREFIXES: Tuple[str, ...] = (
    "o1", "o3", "o4", "gpt-5",
)


def _is_openai_reasoning_model(model_name: str) -> bool:
    """Return True for the OpenAI reasoning models that reject ``max_tokens``."""
    return model_name.startswith(_OPENAI_REASONING_MODEL_PREFIXES)


def _adapt_openai_reasoning_params(
    provider: CloudProvider,
    upstream_model: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate client params for direct-OpenAI reasoning models.

    Many OpenAI-compatible clients (Claude Code, OpenWebUI, Aider, …) send
    ``max_tokens`` and ``temperature`` unconditionally.  OpenAI's reasoning
    models reject both:

    - **``max_tokens``** → rejected; must be ``max_completion_tokens``.
    - **``temperature``** on the o-series → rejected entirely.
    - **``temperature`` on the gpt-5 family → only the value ``1`` is accepted.

    This function silently adapts the body so the request succeeds without
    the client needing to know about per-model API differences.  Only applied
    to the direct ``openai`` provider — OpenRouter handles its own param
    translation, and other providers are unaffected.

    Rules:
    - If the client already set ``max_completion_tokens``, the stray
      ``max_tokens`` is simply dropped (never overrides the explicit value).
    - A client-supplied ``max_completion_tokens`` always wins over
      ``max_tokens``.
    - For o-series, ``temperature`` is stripped unconditionally.
    - For gpt-5*, ``temperature`` is forced to ``1`` when set to anything
      else; omitted is left omitted (OpenAI defaults to 1).
    """
    if provider.name != "openai":
        return body
    if not _is_openai_reasoning_model(upstream_model):
        return body

    adapted = dict(body)

    # max_tokens → max_completion_tokens (original dropped if both present)
    if "max_tokens" in adapted:
        if "max_completion_tokens" not in adapted:
            adapted["max_completion_tokens"] = adapted["max_tokens"]
        adapted.pop("max_tokens", None)

    # Temperature handling
    if upstream_model.startswith(_OPENAI_TEMP_RESTRICTED_PREFIXES):
        temp = adapted.get("temperature")
        if temp is not None:
            # o-series: strip entirely. gpt-5*: force to 1 (only accepted value).
            if upstream_model.startswith(("o1", "o3", "o4")):
                adapted.pop("temperature", None)
            elif temp != 1:
                adapted["temperature"] = 1

    return adapted


def _prepare_cloud_candidate_request(
    provider: CloudProvider,
    upstream_model: str,
    path: str,
    base_json_body: Dict[str, Any],
    client_user_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], bytes, bool]:
    """Build the request body/path for one failover candidate.

    Rewrites the ``model`` field to *upstream_model*, applies the Anthropic ↔
    OpenAI bridge translation when the candidate provider needs it, and fills
    in any per-model default sampling params the client did not already
    specify.

    When *client_user_id* is provided, it is injected as the ``user`` field
    in the request body (only for OpenRouter).  This gives OpenRouter a stable
    per-end-user identifier so that:

    - **Abuse isolation** — a provider policy block triggered by one app does
      not affect other apps sharing the same OpenRouter API key.
    - **Cache correctness** — the ``user`` field is part of the SHA-256 cache
      key, so different apps get separate cache entries while the same app
      benefits from cache hits.

    Returns ``(effective_path, json_body, body_bytes, needs_translation)``.
    """
    candidate_json_body = {**base_json_body, "model": upstream_model}

    # Inject a stable per-client identifier for OpenRouter.  OpenRouter folds
    # this into a hashed identity sent upstream and never forwards it raw,
    # so using the Guardian key fingerprint (a 12-char SHA-256 prefix) is
    # privacy-safe.  We never override a ``user`` value the client already set.
    if client_user_id and provider.name == "openrouter" and "user" not in candidate_json_body:
        candidate_json_body["user"] = client_user_id

    # When the client sends a /v1/messages (Anthropic) request but the target
    # provider only speaks OpenAI format (e.g. NVIDIA NIM), translate the
    # request transparently. OpenRouter supports /v1/messages natively, so
    # translation is skipped for that provider.
    needs_translation = provider_needs_anthropic_translation(provider.name, path)
    effective_path = path
    if needs_translation:
        candidate_json_body = translate_anthropic_request_to_openai(candidate_json_body)
        effective_path = "chat/completions"

    # Some providers (NVIDIA NIM) recommend specific sampling defaults per
    # model (e.g. temperature/top_p/max_tokens/seed). These are configured in
    # cloud_keys.json's top-level "model_defaults" map and only fill in
    # fields the client did not already specify — an explicit value from the
    # client (Claude Code, etc.) always wins.
    model_defaults = cloud_cred_store.get_model_defaults(upstream_model)
    if model_defaults:
        missing = {k: v for k, v in model_defaults.items() if k not in candidate_json_body}
        if missing:
            candidate_json_body = {**candidate_json_body, **missing}
            logger.info("☁️  Applied model defaults for '%s': %s", upstream_model, missing)

    # Translate params for direct OpenAI reasoning models (o-series, gpt-5*).
    # Many clients send max_tokens + temperature unconditionally; OpenAI rejects
    # both for reasoning models.  OpenRouter handles this itself, so this only
    # applies to the direct openai provider.
    candidate_json_body = _adapt_openai_reasoning_params(
        provider, upstream_model, candidate_json_body
    )

    candidate_body = json.dumps(candidate_json_body).encode("utf-8")
    return effective_path, candidate_json_body, candidate_body, needs_translation


def _extract_cloud_response_content(
    payload: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[list]]:
    """Extract text content and tool_calls from a non-streaming cloud response.

    Handles both OpenAI-format (choices[0].message) and Anthropic-format
    (content blocks) responses.
    """
    if not isinstance(payload, dict):
        return None, None

    content_parts: list[str] = []
    tool_calls: Optional[list] = None

    # OpenAI format: choices[0].message
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if isinstance(msg, dict):
            text = msg.get("content")
            if isinstance(text, str) and text:
                content_parts.append(text)
            elif isinstance(text, list):
                # OpenAI content blocks (vision)
                for block in text:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content_parts.append(block.get("text", ""))
            tc = msg.get("tool_calls")
            if isinstance(tc, list) and tc:
                tool_calls = tc
            reasoning = msg.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning and not content_parts:
                content_parts.append(reasoning)

    # Anthropic format: content blocks at top level
    if not content_parts and tool_calls is None:
        anthropic_content = payload.get("content")
        if isinstance(anthropic_content, list):
            for block in anthropic_content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    content_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append({
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })

    content = "\n".join(content_parts) if content_parts else None
    return content, tool_calls


def _setup_cloud_capture(
    request: Request,
    client_id: str,
    *,
    model_name: str,
    json_body: Dict[str, Any],
    path: str,
) -> Tuple[Optional[BuildContext], Optional["PolicyResult"], Optional[str], Optional[float]]:
    """Set up capture context for a cloud route.

    Cloud routes bypass the inference queue, so we generate a request_id
    and start a timer here.  Returns (ctx, policy_result, request_id, start_time).
    All values may be None when capture is disabled or evaluation fails.
    """
    try:
        controller = get_capture_controller()
        client_fp = _capture_client_fingerprint(request, client_id)
        endpoint = _capture_endpoint_from_request(request)
        # For cloud routes via proxy_v1_post, path is "chat/completions" or "messages"
        # For cloud routes via Ollama bridge, path is "chat/completions"
        # Determine protocol from the endpoint/route
        if endpoint.startswith("/v1/messages"):
            protocol = PROTOCOL_ANTHROPIC
        elif endpoint.startswith("/api/chat") or endpoint.startswith("/api/generate"):
            protocol = PROTOCOL_OLLAMA
        else:
            protocol = PROTOCOL_OPENAI

        # Generate a unique request_id for cloud routes (not from queue)
        cloud_request_id = str(uuid.uuid4())

        # Build capture messages
        capture_messages = None
        capture_params = None
        if isinstance(json_body, dict):
            if protocol == PROTOCOL_ANTHROPIC:
                capture_messages = anthropic_messages_to_openai(
                    messages=json_body.get("messages", []),
                    system=json_body.get("system"),
                )
                capture_params = {
                    k: v for k, v in json_body.items()
                    if k not in ("messages", "system")
                }
            else:
                capture_messages = json_body.get("messages")
                capture_params = {
                    k: v for k, v in json_body.items() if k != "messages"
                }

        policy_result = _dispatch_capture_request_received(
            request, client_id,
            request_id=cloud_request_id,
            endpoint=endpoint,
            ingress_protocol=protocol,
            route_type=ROUTE_CLOUD,
            requested_model=model_name,
            resolved_model=model_name,
            request_messages=capture_messages,
            request_parameters=capture_params,
            queue_wait_ms=0,
        )

        if policy_result is not None and policy_result.should_capture:
            ctx = BuildContext(
                request_id=cloud_request_id,
                endpoint=endpoint,
                ingress_protocol=protocol,
                route_type=ROUTE_CLOUD,
                requested_model=model_name,
                resolved_model=model_name,
                capture_policy_version=capture_controller.config.policy_version,
                instance_id=capture_controller.config.instance_id,
                client_fingerprint=client_fp,
            )
            start_time = time.monotonic()
            return ctx, policy_result, cloud_request_id, start_time

    except Exception:
        pass

    return None, None, None, None


async def _forward_to_cloud_provider(
    path: str,
    body: bytes,
    json_body: Dict[str, Any],
    model_name: str,
    request: Request,
    client_id: str,
    *,
    capture_ctx: Optional[BuildContext] = None,
    capture_policy_result: Optional["PolicyResult"] = None,
    cloud_request_id: Optional[str] = None,
    cloud_capture_start_time: Optional[float] = None,
) -> Response:
    """Forward an inference request to a cloud LLM provider.

    Cloud requests bypass the VRAM scheduler, model switch logic, and inference
    queue — the cloud API handles its own rate limiting and concurrency.
    Streaming responses are proxied in real-time so SSE tokens reach the client
    without buffering.

    Supports three routing modes:
    - **Global cloud models** (e.g. ``openai/gpt-4o``): routed via the
      ``ProviderRegistry`` using the provider's global API key from settings.yaml.
    - **Per-key cloud routes** (e.g. ``guardian/nvidia/minimax/minimax-m3``):
      routed via the ``CloudCredentialStore`` using the credential linked to
      the requesting client's Guardian API key. The upstream model name is
      extracted from the route prefix.
    - **Failover groups** (``guardian/failover/{group}``): tries each provider
      candidate configured for *group* in health-ordered priority, skipping a
      candidate that is currently tripped (see :mod:`app.proxy.failover`) and
      falling through to the next one on a connection failure or retryable
      upstream error (429/5xx). A successful response resets that candidate's
      health so Guardian prefers it again once it recovers.
    """
    requires_vision = _messages_contain_image_input(json_body.get("messages", []))
    attempts, failover_group = _resolve_cloud_attempts(
        model_name,
        request,
        client_id,
        requires_vision=requires_vision,
    )
    cloud_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)

    is_stream = bool(json_body.get("stream", False))
    _set_request_usage_metadata(request, model=model_name, streamed=is_stream)
    _start_live_request_usage(request)
    stream_http_client: Optional[httpx.AsyncClient] = None

    # Track cloud capture metadata
    _cloud_capture_attempts = 0

    for attempt_index, (provider, upstream_model) in enumerate(attempts):
        is_last_attempt = attempt_index == len(attempts) - 1
        effective_path, candidate_json_body, candidate_body, needs_translation = (
            _prepare_cloud_candidate_request(provider, upstream_model, path, json_body, cloud_key_fingerprint)
        )

        if needs_translation:
            logger.info(
                "🌉 Anthropic→OpenAI bridge: translating /v1/messages for provider '%s'",
                provider.name,
            )

        forward_headers = ProviderRegistry.build_forward_headers(provider, cloud_key_fingerprint, app_name=client_id)
        forward_url = ProviderRegistry.build_forward_url(provider, effective_path)
        timeout = httpx.Timeout(provider.timeout_seconds, connect=15.0)

        if failover_group is not None:
            logger.info(
                "🔀 Failover group '%s': attempt %d/%d via '%s'",
                failover_group,
                attempt_index + 1,
                len(attempts),
                provider.name,
            )
        logger.info(
            "☁️  Cloud route: client '%s' → %s /v1/%s (model: %s, stream: %s)",
            client_id,
            provider.name,
            path,
            model_name,
            is_stream,
        )

        if is_stream:
            stream_client: Optional[httpx.AsyncClient] = None

            async def send_stream_request() -> httpx.Response:
                nonlocal stream_client
                stream_client = httpx.AsyncClient(timeout=timeout)
                req = stream_client.build_request(
                    "POST",
                    forward_url,
                    content=candidate_body,
                    headers=forward_headers,
                )
                return await stream_client.send(req, stream=True)

            async def read_stream_rate_limit(response: httpx.Response) -> str:
                nonlocal stream_client
                try:
                    body_bytes = await response.aread()
                finally:
                    try:
                        await response.aclose()
                    finally:
                        if stream_client is not None:
                            await stream_client.aclose()
                            stream_client = None
                return body_bytes.decode("utf-8", errors="replace")

            try:
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_stream_request,
                    on_429=read_stream_rate_limit,
                    retry_429=failover_group is None,
                )
            except Exception as e:
                if stream_client is not None:
                    await stream_client.aclose()
                failover_health.record_failure(provider.name, upstream_model)
                logger.error(
                    "☁️  Cloud provider '%s' request failed (attempt %d/%d): %s",
                    provider.name, attempt_index + 1, len(attempts), e,
                )
                if not is_last_attempt:
                    _cloud_capture_attempts = attempt_index + 1
                    continue
                _finish_live_request_usage(request, status_code=502, response_bytes=0)
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=_classify_capture_error(e),
                    http_status=502,
                    sanitized_message=_sanitize_capture_error_message(e),
                    queue_wait_ms=0,
                    duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
                raise HTTPException(status_code=502, detail=f"Cloud provider request failed: {e}")

            # ── Failover 429 probe: wait and retry once before falling through ──
            # When a failover candidate returns HTTP 429, the priority source
            # (e.g. NVIDIA's free tier) gets one more chance after a 60s wait.
            # Concurrent requests skip the rate-limited candidate and go
            # directly to the next one (OR), keeping them responsive.
            if (
                getattr(resp, "status_code", 0) == 429
                and failover_group is not None
                and not is_last_attempt
            ):
                _probe_wait = failover_health._rate_limit_cooldown_seconds
                logger.info(
                    "⏳ Failover 429: '%s' rate-limited; waiting %.0fs before one retry...",
                    provider.name, _probe_wait,
                )
                failover_health.record_rate_limited(provider.name, upstream_model)
                await asyncio.sleep(_probe_wait)
                failover_health.clear_rate_limit(provider.name, upstream_model)
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_stream_request,
                    on_429=read_stream_rate_limit,
                    retry_429=False,
                )

            stream_http_client = stream_client

            # ── Error translation for Anthropic clients ───────────────────
            # If the upstream provider returned an error (non-SSE body), translate
            # it to Anthropic error format instead of trying to stream it.
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                await resp.aclose()
                if stream_http_client is not None:
                    await stream_http_client.aclose()
                if resp.status_code != 429:
                    # 429 (rate limited) does not count against a provider's
                    # health — Claude Code already retries these itself and
                    # the provider is usually fine, just busy.
                    failover_health.record_failure(provider.name, upstream_model)
                else:
                    # Mark provider as rate-limited so concurrent requests
                    # skip it and fall through to the next candidate directly.
                    failover_health.record_rate_limited(provider.name, upstream_model)
                if _is_retryable_cloud_error(resp.status_code, body_bytes.decode("utf-8", errors="replace")) and not is_last_attempt:
                    logger.warning(
                        "☁️  Cloud provider '%s' returned %s after local retry budget "
                        "(attempt %d/%d); trying next candidate",
                        provider.name, resp.status_code, attempt_index + 1, len(attempts),
                    )
                    continue
                _finish_live_request_usage(request, status_code=resp.status_code, response_bytes=len(body_bytes))
                # ── Capture: request_failed (streaming HTTP error) ──
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=f"cloud_http_{resp.status_code}",
                    http_status=resp.status_code,
                    sanitized_message=f"Cloud provider returned HTTP {resp.status_code}",
                    queue_wait_ms=0,
                    duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
                if needs_translation:
                    try:
                        error_payload = json.loads(body_bytes)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        error_payload = body_bytes.decode("utf-8", errors="replace")
                    anthropic_error = translate_openai_error_to_anthropic(resp.status_code, error_payload)
                    logger.warning(
                        "🌉 Anthropic bridge: translated %s error from %s: %s",
                        resp.status_code,
                        provider.name,
                        anthropic_error["error"]["message"][:200],
                    )
                    return Response(
                        content=json.dumps(anthropic_error).encode("utf-8"),
                        status_code=resp.status_code,
                        headers={"Content-Type": "application/json"},
                    )
                return Response(
                    content=body_bytes,
                    status_code=resp.status_code,
                    headers={
                        **_sanitize_proxied_response_headers(resp.headers),
                        **_guardian_debug_headers(provider, upstream_model, failover_group),
                    },
                )

            # Success — this candidate wins. Bind the winning json_body and
            # fall through to the streaming response construction below.
            failover_health.record_success(provider.name, upstream_model)
            json_body = candidate_json_body
            break

        # Non-streaming
        async with httpx.AsyncClient(timeout=timeout) as non_stream_http_client:
            async def send_non_stream_request() -> httpx.Response:
                return await non_stream_http_client.post(
                    forward_url,
                    content=candidate_body,
                    headers=forward_headers,
                )

            try:
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_non_stream_request,
                    retry_429=failover_group is None,
                )
            except Exception as e:
                failover_health.record_failure(provider.name, upstream_model)
                logger.error(
                    "☁️  Cloud provider '%s' request failed (attempt %d/%d): %s",
                    provider.name, attempt_index + 1, len(attempts), e,
                )
                if not is_last_attempt:
                    _cloud_capture_attempts = attempt_index + 1
                    continue
                _finish_live_request_usage(request, status_code=502, response_bytes=0)
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=_classify_capture_error(e),
                    http_status=502,
                    sanitized_message=_sanitize_capture_error_message(e),
                    queue_wait_ms=0,
                    duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
                raise HTTPException(status_code=502, detail=f"Cloud provider request failed: {e}")

            # ── Failover 429 probe: wait and retry once before falling through ──
            if (
                getattr(resp, "status_code", 0) == 429
                and failover_group is not None
                and not is_last_attempt
            ):
                _probe_wait = failover_health._rate_limit_cooldown_seconds
                logger.info(
                    "⏳ Failover 429: '%s' rate-limited; waiting %.0fs before one retry...",
                    provider.name, _probe_wait,
                )
                failover_health.record_rate_limited(provider.name, upstream_model)
                await asyncio.sleep(_probe_wait)
                failover_health.clear_rate_limit(provider.name, upstream_model)
                resp = await cloud_rate_limiter.execute_with_retry(
                    cloud_key_fingerprint,
                    provider.name,
                    send_non_stream_request,
                    retry_429=False,
                )

            if (
                resp.status_code >= 400
                and _is_retryable_cloud_error(resp.status_code, resp.text)
                and not is_last_attempt
            ):
                if resp.status_code != 429:
                    failover_health.record_failure(provider.name, upstream_model)
                else:
                    failover_health.record_rate_limited(provider.name, upstream_model)
                logger.warning(
                    "☁️  Cloud provider '%s' returned %s after local retry budget "
                    "(attempt %d/%d); trying next candidate",
                    provider.name, resp.status_code, attempt_index + 1, len(attempts),
                )
                continue

            if resp.status_code < 400:
                failover_health.record_success(provider.name, upstream_model)
            elif resp.status_code != 429:
                # 429 (rate limited) does not count against a provider's health
                # — Claude Code already retries these itself and the provider is
                # usually fine, just busy.
                failover_health.record_failure(provider.name, upstream_model)

            # Record token usage from response payload
            try:
                payload = resp.json()
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            _record_usage_from_payload(client_id, f"/v1/{path}", model_name, payload, request=request)

            # ── Capture: request_completed or request_failed (non-streaming) ──
            _cloud_capture_attempts = attempt_index + 1
            _cloud_capture_duration_ms = (
                (time.monotonic() - cloud_capture_start_time) * 1000
                if cloud_capture_start_time else None
            )
            if resp.status_code >= 400:
                _dispatch_capture_request_failed(
                    capture_ctx,
                    error_code=f"cloud_http_{resp.status_code}",
                    http_status=resp.status_code,
                    sanitized_message=f"Cloud provider returned HTTP {resp.status_code}",
                    queue_wait_ms=0,
                    duration_ms=_cloud_capture_duration_ms,
                    attempts=_cloud_capture_attempts,
                    policy_result=capture_policy_result,
                ) if capture_ctx is not None else None
            else:
                _cloud_content, _cloud_tool_calls = _extract_cloud_response_content(payload)
                _dispatch_capture_request_completed(
                    capture_ctx,
                    policy_result=capture_policy_result,
                    response_content=_cloud_content,
                    tool_calls=_cloud_tool_calls,
                    prompt_tokens=payload.get("usage", {}).get("prompt_tokens", payload.get("usage", {}).get("input_tokens", 0)) if isinstance(payload, dict) else None,
                    completion_tokens=payload.get("usage", {}).get("completion_tokens", payload.get("usage", {}).get("output_tokens", 0)) if isinstance(payload, dict) else None,
                    http_status=resp.status_code,
                    streamed=False,
                    incomplete=False,
                    attempts=_cloud_capture_attempts,
                    duration_ms=_cloud_capture_duration_ms,
                ) if capture_ctx is not None else None

            debug_headers = _guardian_debug_headers(provider, upstream_model, failover_group)
            # Suffix the client-visible model field with the winning provider on
            # failover routes only, so an ambiguous "which provider answered?"
            # is resolvable from the response body itself.
            response_model_name = f"{model_name}@{provider.name}" if failover_group else model_name

            # ── Anthropic response translation (non-streaming) ───────────
            if needs_translation and payload and isinstance(payload, dict):
                # Translate errors first
                if resp.status_code >= 400:
                    anthropic_error = translate_openai_error_to_anthropic(resp.status_code, payload)
                    return Response(
                        content=json.dumps(anthropic_error).encode("utf-8"),
                        status_code=resp.status_code,
                        headers={"Content-Type": "application/json", **debug_headers},
                    )
                anthropic_response = translate_openai_response_to_anthropic(
                    payload,
                    response_model_name,
                    request_stop_sequences=candidate_json_body.get("stop_sequences"),
                )
                translated_content = json.dumps(anthropic_response).encode("utf-8")
                return Response(
                    content=translated_content,
                    status_code=resp.status_code,
                    headers={"Content-Type": "application/json", **debug_headers},
                )

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={**_sanitize_proxied_response_headers(resp.headers), **debug_headers},
            )

    if stream_http_client is None:
        _finish_live_request_usage(request, status_code=502, response_bytes=0)
        raise HTTPException(status_code=502, detail="Cloud streaming client was not initialized")
    stream_response_client = stream_http_client

    # ── Streaming response construction ─────────────────────────────────
    # Only reached after a successful `break` in the streaming branch above —
    # the non-streaming branch always returns from within the loop.
    debug_headers = _guardian_debug_headers(provider, upstream_model, failover_group)
    # Suffix the client-visible model field with the winning provider on
    # failover routes only (see _guardian_debug_headers docstring).
    response_model_name = f"{model_name}@{provider.name}" if failover_group else model_name
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}

    # ── Capture: streaming assembler for cloud route ──
    _cloud_assembler: Optional[StreamResponseAssembler] = None
    if capture_ctx is not None:
        _cloud_assembler = StreamResponseAssembler(
            protocol=PROTOCOL_ANTHROPIC if needs_translation else PROTOCOL_OPENAI,
        )

    async def _read_sse_lines():
        """Yield raw SSE lines from the upstream response with watchdog."""
        watchdog = StreamProgressWatchdog(provider.timeout_seconds)
        async for line in _iter_sse_lines_with_watchdog(
            resp,
            watchdog,
            request_id=str(uuid.uuid4()),
            route=f"/v1/{path}",
            client_id=client_id,
            model_name=model_name,
            heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S,
        ):
            yield line

    _cloud_stream_cancelled = False

    async def cloud_stream():
        try:
            if needs_translation:
                # ── Anthropic streaming translation ───────────────
                # Translate OpenAI SSE chunks to Anthropic SSE events
                async for event_line in translate_openai_stream_to_anthropic(
                    _read_sse_lines(),
                    response_model_name,
                    request_stop_sequences=json_body.get("stop_sequences") if needs_translation else None,
                ):
                    # Extract usage from the translated events
                    if "message_delta" in event_line:
                        try:
                            # Parse the data line to get output_tokens
                            for part in event_line.split("\n"):
                                if part.startswith("data: "):
                                    data = json.loads(part[6:])
                                    if data.get("type") == "message_delta":
                                        usage_totals["completion_tokens"] = max(
                                            usage_totals["completion_tokens"],
                                            data.get("usage", {}).get("output_tokens", 0),
                                        )
                                    # ── Capture: feed translated Anthropic event to assembler ──
                                    if _cloud_assembler is not None:
                                        _cloud_assembler.feed(data)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    encoded_line = event_line.encode("utf-8")
                    _update_live_request_usage(
                        request,
                        response_bytes_delta=len(encoded_line),
                    )
                    yield encoded_line
            else:
                # ── Pass-through (no translation needed) ──────────
                async for line in _read_sse_lines():
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            usage = data.get("usage") or {}
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
                            # ── Capture: feed chunk to assembler ──
                            if _cloud_assembler is not None:
                                _cloud_assembler.feed(data)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
                    encoded_line = (line + "\n").encode("utf-8")
                    _update_live_request_usage(
                        request,
                        response_bytes_delta=len(encoded_line),
                    )
                    yield encoded_line
        except (asyncio.CancelledError, _GuardianRequestCancelled, httpx.StreamClosed, httpx.ReadError, httpx.RemoteProtocolError):
            _cloud_stream_cancelled = True
        finally:
            await resp.aclose()
            await stream_response_client.aclose()
            _record_request_token_usage(
                client_id,
                f"/v1/{path}",
                model_name,
                request=request,
                prompt_tokens=usage_totals["prompt_tokens"],
                completion_tokens=usage_totals["completion_tokens"],
            )
            _finish_live_request_usage(
                request,
                status_code=resp.status_code,
            )
            # ── Capture: request_completed or request_cancelled (streaming, cloud) ──
            if capture_ctx is not None:
                if _cloud_stream_cancelled:
                    _dispatch_capture_request_cancelled(
                        capture_ctx,
                        cancel_reason="client_disconnect",
                        duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                        attempts=_cloud_capture_attempts,
                        policy_result=capture_policy_result,
                    )
                else:
                    _cloud_stream_content = None
                    _cloud_stream_tool_calls = None
                    if _cloud_assembler is not None:
                        _cloud_assembled = _cloud_assembler.assemble()
                        _cloud_stream_content = _cloud_assembled.get("content")
                        _cloud_stream_tool_calls = _cloud_assembled.get("tool_calls")
                    _dispatch_capture_request_completed(
                        capture_ctx,
                        policy_result=capture_policy_result,
                        response_content=_cloud_stream_content,
                        tool_calls=_cloud_stream_tool_calls,
                        prompt_tokens=usage_totals["prompt_tokens"],
                        completion_tokens=usage_totals["completion_tokens"],
                        http_status=resp.status_code,
                        streamed=True,
                        incomplete=resp.status_code != 200,
                        attempts=_cloud_capture_attempts,
                        duration_ms=(time.monotonic() - cloud_capture_start_time) * 1000 if cloud_capture_start_time else None,
                    )

    return StreamingResponse(
        cloud_stream(),
        status_code=resp.status_code,
        media_type="text/event-stream",
        headers={
            **_sanitize_proxied_response_headers(resp.headers),
            **debug_headers,
        },
    )


@app.post("/v1/{path:path}")
async def proxy_v1_post(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    body = await request.body()
    _set_request_usage_metadata(request, streamed=False)

    # Intercept count_tokens locally — no cloud/local model needed.
    # Claude Code uses this for context window management; a rough estimate
    # is sufficient.  Without this, the request would be forwarded to the
    # local llama-server which is down in cloud-only setups → 500 error.
    if path == "messages/count_tokens" or path.startswith("messages/count_tokens"):
        try:
            ct_body = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            ct_body = {}
        # Estimate tokens from message content (~4 chars per token)
        total_chars = 0
        for msg in ct_body.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        total_chars += len(block.get("text", ""))
        system_field = ct_body.get("system", "")
        if isinstance(system_field, str):
            total_chars += len(system_field)
        elif isinstance(system_field, list):
            for block in system_field:
                if isinstance(block, dict) and block.get("type") == "text":
                    total_chars += len(block.get("text", ""))
        estimated_tokens = max(1, total_chars // 4)
        return Response(
            content=json.dumps({"input_tokens": estimated_tokens}).encode("utf-8"),
            status_code=200,
            headers={"Content-Type": "application/json", "X-Token-Count-Estimate": "true"},
        )

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

    # ── Detect image inputs early (needed for cloud vision fallback + local path) ──
    has_image_inputs = False
    if path in ("chat/completions", "messages"):
        has_image_inputs = _messages_contain_image_input(json_body.get("messages", []))

    # ── Cloud LLM router: forward to OpenRouter / NVIDIA / … ─────────
    # Cloud models bypass the VRAM scheduler, model switch logic, and inference
    # queue entirely — the cloud API handles its own rate limiting.
    #
    # When a text-only cloud model receives image input, Guardian transparently
    # redirects to its configured local vision fallback. Image-capable cloud
    # models continue to use their native cloud image support.
    if _is_cloud_or_guardian_route(requested_model):
        if has_image_inputs:
            vision_fallback = _resolve_cloud_vision_fallback(requested_model)
            if vision_fallback:
                # Preserve cloud-route authorization even though the image is
                # handled locally. This prevents arbitrary guardian/* routes
                # from using a local model fallback.
                _resolve_cloud_attempts(requested_model, request, client_id)
                logger.info(
                    "🖼️  Cloud route '%s' is text-only with image input — "
                    "redirecting to local vision model '%s'",
                    requested_model, vision_fallback,
                )
                # Resolve alias → canonical model name so the local inference
                # path (model switch, vision preflight, mmproj loading) works.
                requested_model = _resolve_inference_model(vision_fallback, current_model)
                json_body["model"] = requested_model
                # Fall through to local inference path below.
            else:
                body = json.dumps(json_body).encode("utf-8")
                # ── Capture: cloud request_received (fail-open) ──
                _cloud_ctx, _cloud_policy, _cloud_req_id, _cloud_start = _setup_cloud_capture(
                    request, client_id,
                    model_name=requested_model,
                    json_body=json_body,
                    path=path,
                )
                return await _forward_to_cloud_provider(
                    path=path,
                    body=body,
                    json_body=json_body,
                    model_name=requested_model,
                    request=request,
                    client_id=client_id,
                    capture_ctx=_cloud_ctx,
                    capture_policy_result=_cloud_policy,
                    cloud_request_id=_cloud_req_id,
                    cloud_capture_start_time=_cloud_start,
                )
        else:
            # ── Capture: cloud request_received (fail-open, no image) ──
            body = json.dumps(json_body).encode("utf-8")
            _cloud_ctx2, _cloud_policy2, _cloud_req_id2, _cloud_start2 = _setup_cloud_capture(
                request, client_id,
                model_name=requested_model,
                json_body=json_body,
                path=path,
            )
            return await _forward_to_cloud_provider(
                path=path,
                body=body,
                json_body=json_body,
                model_name=requested_model,
                request=request,
                client_id=client_id,
                capture_ctx=_cloud_ctx2,
                capture_policy_result=_cloud_policy2,
                cloud_request_id=_cloud_req_id2,
                cloud_capture_start_time=_cloud_start2,
            )

    _apply_anthropic_thinking_to_llama_params(json_body)
    _apply_request_reasoning_defaults(path, json_body, requested_model)
    if path in ("chat/completions", "messages"):
        json_body["messages"] = _sanitize_messages_for_qwen_chat_template(
            json_body.get("messages", [])
        )
    body = json.dumps(json_body).encode("utf-8")
    # has_image_inputs already computed above

    request_start_time = time.monotonic()
    _capture_policy_result: Optional["PolicyResult"] = None
    _capture_ctx: Optional[BuildContext] = None

    try:
        request_id, disconnect_task = await _begin_queued_request(request, client_id, requested_model)
    except _GuardianRequestCancelled as exc:
        # Capture: request_cancelled before queue admission
        _capture_ctx = BuildContext(
            request_id=exc.request_id,
            endpoint=_capture_endpoint_from_request(request),
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model=requested_model,
            resolved_model=requested_model,
            capture_policy_version=capture_controller.config.policy_version,
            instance_id=capture_controller.config.instance_id,
            client_fingerprint=_capture_client_fingerprint(request, client_id),
        )
        _dispatch_capture_request_cancelled(
            _capture_ctx, cancel_reason=exc.reason,
            duration_ms=(time.monotonic() - request_start_time) * 1000,
        )
        raise _request_cancel_http_exception(exc.request_id, exc.reason)

    # ── Capture: request_received event (fail-open, disabled by default) ──
    # Dispatch only for local OpenAI chat/completions — the first delivery slice.
    # The hook is wrapped in try/except so capture failures never block inference.
    _capture_client_fp = _capture_client_fingerprint(request, client_id)
    _capture_endpoint = _capture_endpoint_from_request(request)
    _capture_protocol = _capture_ingress_protocol(path, _capture_endpoint)

    # Translate request messages to OpenAI format for capture if Anthropic
    _capture_request_messages = None
    _capture_request_params = None
    if isinstance(json_body, dict):
        if _capture_protocol == PROTOCOL_ANTHROPIC:
            # Anthropic /v1/messages: content is [messages, system, ...]
            _capture_request_messages = anthropic_messages_to_openai(
                messages=json_body.get("messages", []),
                system=json_body.get("system"),
            )
            _capture_request_params = {
                k: v for k, v in json_body.items()
                if k not in ("messages", "system")
            }
        else:
            _capture_request_messages = json_body.get("messages")
            _capture_request_params = {
                k: v for k, v in json_body.items() if k != "messages"
            }

    _capture_policy_result = _dispatch_capture_request_received(
        request, client_id,
        request_id=request_id,
        endpoint=_capture_endpoint,
        ingress_protocol=_capture_protocol,
        route_type=ROUTE_LOCAL,
        requested_model=requested_model,
        resolved_model=requested_model,
        request_messages=_capture_request_messages,
        request_parameters=_capture_request_params,
        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
    )
    if _capture_policy_result is not None and _capture_policy_result.should_capture:
        _capture_ctx = BuildContext(
            request_id=request_id,
            endpoint=_capture_endpoint,
            ingress_protocol=_capture_protocol,
            route_type=ROUTE_LOCAL,
            requested_model=requested_model,
            resolved_model=requested_model,
            capture_policy_version=capture_controller.config.policy_version,
            instance_id=capture_controller.config.instance_id,
            client_fingerprint=_capture_client_fp,
        )

    _release_in_finally = True
    capture_dispatched = False
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
                        # Handle both string content and Anthropic content blocks
                        trailing_assistant_contents.insert(0, _stringify_message_content(content))
                        
                if trailing_assistant_contents and len(msgs) >= 1:
                    combined_prefill = "\\n".join(trailing_assistant_contents)
                    
                    # Find the last user message and append the prefill instruction
                    last_user_idx = -1
                    for i in range(len(msgs)-1, -1, -1):
                        if msgs[i].get("role") == "user":
                            last_user_idx = i
                            break
                            
                    if last_user_idx != -1:
                        user_content = _stringify_message_content(msgs[last_user_idx].get("content", ""))
                        msgs[last_user_idx]["content"] = user_content + f"\n\n[System directive: Please start your response exactly with the following text: {combined_prefill}]"
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
                    # ── Capture: request_cancelled (streaming, pre-stream) ──
                    if _capture_ctx is not None:
                        _dispatch_capture_request_cancelled(
                            _capture_ctx, cancel_reason=exc.reason,
                            queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
                            duration_ms=(time.monotonic() - request_start_time) * 1000,
                        )
                    await client.aclose()
                    raise _request_cancel_http_exception(exc.request_id, exc.reason)
                except Exception as retry_error:
                    # ── Capture: request_failed (streaming, pre-stream) ──
                    if _capture_ctx is not None:
                        _dispatch_capture_request_failed(
                            _capture_ctx,
                            error_code="backend_reload_failed",
                            http_status=502,
                            sanitized_message="Backend request failed after reload",
                            queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
                            duration_ms=(time.monotonic() - request_start_time) * 1000,
                        )
                    await client.aclose()
                    raise HTTPException(status_code=502, detail=f"Backend request failed after reload: {retry_error}")
            except _GuardianRequestCancelled as exc:
                # ── Capture: request_cancelled (streaming, pre-stream outer) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
                        duration_ms=(time.monotonic() - request_start_time) * 1000,
                    )
                await client.aclose()
                raise _request_cancel_http_exception(exc.request_id, exc.reason)
            except Exception as e:
                # ── Capture: request_failed (streaming, pre-stream outer) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_failed(
                        _capture_ctx,
                        error_code="backend_request_failed",
                        http_status=502,
                        sanitized_message="Backend request failed",
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
                        duration_ms=(time.monotonic() - request_start_time) * 1000,
                    )
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

            # ── Capture: per-request stream assembler (fail-open) ──────
            _local_capture_assembler: Optional[StreamResponseAssembler] = None
            if _capture_ctx is not None and _capture_policy_result is not None and _capture_policy_result.should_capture:
                _local_capture_assembler = StreamResponseAssembler()

            async def stream_passthrough():
                cancel_cleanup_task = asyncio.create_task(
                    _close_on_request_cancel(
                        request_id,
                        lambda: _close_stream_resources(resp, client),
                    )
                )
                is_anthropic_stream = path == "messages"
                anthropic_input_tokens = 0
                anthropic_cache_read = 0
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
                        # ── Anthropic /v1/messages enrichment ───────────────────
                        # llama-server's Anthropic endpoint is missing some fields
                        # that Claude Code expects. Enrich SSE events on the fly.
                        if is_anthropic_stream:
                            # Convert keepalive comments to Anthropic ping events
                            if line.startswith(": guardian-keepalive"):
                                ping_event = _format_sse_event("ping", {"type": "ping"})
                                encoded_line = ping_event.encode("utf-8")
                                _update_live_request_usage(request, response_bytes_delta=len(encoded_line))
                                yield encoded_line
                                continue

                            # Enrich Anthropic SSE data lines with missing usage fields
                            if line.startswith("data: "):
                                enriched, anthropic_input_tokens, anthropic_cache_read = _enrich_anthropic_sse_line(
                                    line,
                                    input_tokens=anthropic_input_tokens,
                                    cache_read_tokens=anthropic_cache_read,
                                )
                                line = enriched

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
                        # ── Capture: feed SSE line to stream assembler ──
                        if _local_capture_assembler is not None:
                            try:
                                _local_capture_assembler.add_sse_line(line)
                            except Exception:
                                pass
                        yield encoded_line
                except (asyncio.CancelledError, _GuardianRequestCancelled, httpx.StreamClosed, httpx.ReadError, httpx.RemoteProtocolError):
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

                    # ── Capture: request_completed or request_cancelled ──
                    _dispatch_capture_stream_completed(
                        request, request_id, client_id,
                        active_model_for_request, _capture_ctx,
                        _capture_policy_result, _local_capture_assembler,
                        usage_totals, path, resp.status_code,
                    )
                    capture_dispatched = True

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
                    # ── Capture: request_cancelled (non-streaming) ──
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id),
                        duration_ms=(time.monotonic() - request_start_time) * 1000,
                    ) if _capture_ctx is not None else None
                    raise _request_cancel_http_exception(exc.request_id, exc.reason)
                except Exception as e:
                    # ── Capture: request_failed ──
                    _capture_error_code = _classify_capture_error(e)
                    _dispatch_capture_request_failed(
                        _capture_ctx,
                        error_code=_capture_error_code,
                        http_status=502,
                        sanitized_message=_sanitize_capture_error_message(e),
                        queue_wait_ms=inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - request_start_time) * 1000,
                    ) if _capture_ctx is not None else None
                    raise HTTPException(status_code=502, detail=f"Backend request failed: {e}")
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
                if path in ("chat/completions", "completions", "embeddings", "messages"):
                    try:
                        payload = resp.json()
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                    _record_usage_from_payload(client_id, f"/v1/{path}", active_model_for_request, payload, request=request)
                # ── Capture: request_completed (non-streaming) ──
                if path in ("chat/completions", "messages") and not capture_dispatched:
                    _dispatch_capture_nonstream_completed(
                        request, request_id, client_id,
                        active_model_for_request, _capture_ctx,
                        _capture_policy_result, payload, resp.status_code,
                        request_start_time,
                    )
                    capture_dispatched = True
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
                # ── Anthropic /v1/messages non-streaming enrichment ──────
                # Enrich llama-server's Anthropic response with missing usage
                # fields (cache_creation_input_tokens, etc.) that Claude Code expects.
                if path == "messages" and 200 <= resp.status_code < 400:
                    try:
                        anthropic_payload = json.loads(resp.content)
                        if isinstance(anthropic_payload, dict):
                            anthropic_payload = _enrich_anthropic_response(anthropic_payload)
                            enriched_content = json.dumps(anthropic_payload).encode("utf-8")
                            # Strip content-length/transfer-encoding — enriched
                            # content has a different size than the original.
                            safe_headers = {
                                k: v for k, v in resp.headers.items()
                                if k.lower() not in ("transfer-encoding", "content-length", "content-encoding")
                            }
                            return Response(
                                content=enriched_content,
                                status_code=resp.status_code,
                                headers=safe_headers | _queue_headers(request_id, queue_wait_ms),
                                media_type="application/json",
                            )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
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


# ── Session slot filename sanitization ─────────────────────────────────
# Slots are written by llama-server under --slot-save-path ($HOME/llama_slots).
# Block path traversal: strip directory components, allow only
# [A-Za-z0-9_-]+.bin, then confirm the resolved path stays inside the slots
# dir (defense in depth — redundant after basename + regex, but explicit).
_SESSION_SLOTS_DIR = Path("/home/flip/llama_slots")
_SESSION_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.bin$")


def _sanitize_session_filename(raw: object) -> str:
    """Return a safe basename for a session slot, or raise HTTP 400."""
    if not isinstance(raw, str) or not raw:
        raise HTTPException(status_code=400, detail="Filename required")
    basename = Path(raw).name  # drop any directory components
    if not _SESSION_FILENAME_RE.fullmatch(basename):
        raise HTTPException(
            status_code=400,
            detail="Invalid filename: use letters, digits, '_' or '-' with a .bin suffix",
        )
    resolved = (_SESSION_SLOTS_DIR / basename).resolve()
    if resolved.parent != _SESSION_SLOTS_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename")
    return basename


@app.post("/api/session/save")
async def save_session(request: Request, client_id: str = Depends(verify_api_key)):
    logger.info(f"💾 Session SAVE request from {client_id}")
    try:
        data = await request.json()
        filename = _sanitize_session_filename(data.get("filename"))
        
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
    except HTTPException:
        # Let client-facing 4xx (e.g. filename-sanitization 400) propagate unchanged.
        raise
    except Exception as e:
        logger.error(f"Save session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/session/load")
async def load_session(request: Request, client_id: str = Depends(verify_api_key)):
    logger.info(f"📂 Session LOAD request from {client_id}")
    try:
        data = await request.json()
        filename = _sanitize_session_filename(data.get("filename"))
            
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
    except HTTPException:
        # Let client-facing 4xx (e.g. filename-sanitization 400) propagate unchanged.
        raise
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
