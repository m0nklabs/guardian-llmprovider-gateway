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

# ── Cloud inference helpers (Phase 5 extraction) ────────────────────
import app.cloud_inference as _cloud_inf

# ── Cloud inference routing (Phase 5 extraction) ────────────────────
from app.cloud_inference import routing as _cloud_routing

# ── Cloud inference forwarding (Phase 5 extraction) ──────────────────
from app.cloud_inference import forwarding as _cloud_forwarding

# ── Local inference (Phase 5 extraction) ─────────────────────────────
from app.local_inference import ollama as _local_ollama

# ── Gateway capture dispatch (Phase 5 extraction) ────────────────────
from app.gateway import capture_dispatch as _capture_dispatch

# ── Gateway usage tracking (Phase 5 extraction) ──────────────────────
from app.gateway import usage as _usage

# ── Gateway normalization (Phase 5 extraction) ───────────────────────
from app.gateway import normalization as _normalization

# ── Gateway streaming helpers (Phase 5 extraction) ──────────────────
from app.gateway import streaming as _streaming

# ── Gateway queue helpers (Phase 5 extraction) ──────────────────────
from app.gateway import queue_helpers as _queue_helpers

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

# Initialize cloud_inference helpers with singleton registry
_cloud_inf.init(provider_registry)

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


# ── Streaming helpers (delegated to app.gateway.streaming) ──────────
# Phase 5: extracted to app/gateway/streaming.py.

STREAM_TIMEOUT_EXTENSION_STEPS = _streaming.STREAM_TIMEOUT_EXTENSION_STEPS
STREAM_LOOP_REPEAT_THRESHOLD = _streaming.STREAM_LOOP_REPEAT_THRESHOLD

def _extract_assistant_message_text(message: Dict[str, object]) -> str:
    return _streaming.extract_assistant_message_text(message)

def _extract_assistant_delta_text(delta: Dict[str, object]) -> str:
    return _streaming.extract_assistant_delta_text(delta)

def _normalize_stream_progress_text(text: object) -> str:
    return _streaming.normalize_stream_progress_text(text)

def _extract_stream_progress_text(line: str) -> str:
    return _streaming.extract_stream_progress_text(line)

StreamProgressWatchdog = _streaming.StreamProgressWatchdog

def _build_stream_timeout(base_timeout_s: float) -> httpx.Timeout:
    return _streaming.build_stream_timeout(base_timeout_s)

def _build_sse_keepalive_comment(request_id: Optional[str] = None) -> str:
    return _streaming.build_sse_keepalive_comment(request_id)

def _enrich_anthropic_sse_line(line: str, *, input_tokens: int = 0, cache_read_tokens: int = 0) -> tuple[str, int, int]:
    return _streaming.enrich_anthropic_sse_line(line, input_tokens=input_tokens, cache_read_tokens=cache_read_tokens)

def _enrich_anthropic_response(payload: dict) -> dict:
    return _streaming.enrich_anthropic_response(payload)

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
    async for line in _streaming.iter_sse_lines_with_watchdog(
        response,
        watchdog,
        request_id=request_id,
        route=route,
        client_id=client_id,
        model_name=model_name,
        heartbeat_interval_s=heartbeat_interval_s,
        cancel_event=cancel_event,
    ):
        yield line


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
# ── Queue helpers (delegated to app.gateway.queue_helpers) ──────────
# Phase 5: extracted to app/gateway/queue_helpers.py.

_GuardianRequestCancelled = _queue_helpers.GuardianRequestCancelled

def _queue_headers(request_id: str, queue_wait_ms: float) -> Dict[str, str]:
    return _queue_helpers.queue_headers(request_id, queue_wait_ms)

def _request_cancel_http_exception(request_id: str, reason: str) -> HTTPException:
    return _queue_helpers.request_cancel_http_exception(request_id, reason)

async def _stop_background_task(task: Optional[asyncio.Task]) -> None:
    await _queue_helpers.stop_background_task(task)

async def _watch_request_disconnect(request: Request, request_id: str, client_id: str) -> None:
    await _queue_helpers.watch_request_disconnect(request, request_id, client_id)

async def _begin_queued_request(request: Request, client_id: str, model: str) -> tuple[str, asyncio.Task]:
    return await _queue_helpers.begin_queued_request(request, client_id, model)

async def _await_or_cancel_request(
    operation_task: asyncio.Task,
    request_id: str,
    cleanup: Optional[Callable[[], Awaitable[None]]] = None,
) -> Any:
    return await _queue_helpers.await_or_cancel_request(operation_task, request_id, cleanup)

async def _close_stream_resources(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await _queue_helpers.close_stream_resources(response, client)

async def _close_on_request_cancel(
    request_id: str,
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    await _queue_helpers.close_on_request_cancel(request_id, cleanup)

def _request_outcome(request_id: str) -> str:
    return _queue_helpers.request_outcome(request_id)


# ── Capture helpers (fail-open, never block inference) ──────────────────

# ── Capture dispatch (delegated to app.gateway.capture_dispatch) ─────
# Phase 5: extracted to app/gateway/capture_dispatch.py.  Thin wrappers
# preserve existing call sites in server.py.

def _capture_client_fingerprint(request: Request, client_id: str) -> Optional[str]:
    return _capture_dispatch.capture_client_fingerprint(request, client_id)

def _capture_ingress_protocol(path: str, endpoint: str) -> str:
    return _capture_dispatch.capture_ingress_protocol(path, endpoint)

def _capture_endpoint_from_request(request: Request) -> str:
    return _capture_dispatch.capture_endpoint_from_request(request)

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
    return _capture_dispatch.dispatch_capture_request_received(
        request, client_id,
        request_id=request_id, endpoint=endpoint,
        ingress_protocol=ingress_protocol, route_type=route_type,
        requested_model=requested_model, resolved_model=resolved_model,
        request_messages=request_messages, request_parameters=request_parameters,
        queue_wait_ms=queue_wait_ms,
    )

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
    _capture_dispatch.dispatch_capture_request_completed(
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
    _capture_dispatch.dispatch_capture_request_failed(
        ctx,
        error_code=error_code,
        http_status=http_status,
        sanitized_message=sanitized_message,
        queue_wait_ms=queue_wait_ms,
        duration_ms=duration_ms,
        attempts=attempts,
    )

def _dispatch_capture_request_cancelled(
    ctx: BuildContext,
    *,
    cancel_reason: str,
    queue_wait_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
    attempts: Optional[int] = None,
    policy_result: Optional["PolicyResult"] = None,
) -> None:
    _capture_dispatch.dispatch_capture_request_cancelled(
        ctx,
        cancel_reason=cancel_reason,
        queue_wait_ms=queue_wait_ms,
        duration_ms=duration_ms,
        attempts=attempts,
    )

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
    _capture_dispatch.dispatch_capture_stream_completed(
        request, request_id, client_id, model_name,
        ctx, policy_result, assembler, usage_totals, path, status_code,
    )

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
    _capture_dispatch.dispatch_capture_nonstream_completed(
        request, request_id, client_id, model_name,
        ctx, policy_result, payload, status_code, request_start_time,
    )

def _classify_capture_error(exc: Exception) -> str:
    return _capture_dispatch.classify_capture_error(exc)

def _sanitize_capture_error_message(exc: Exception) -> str:
    return _capture_dispatch.sanitize_capture_error_message(exc)

def _messages_contain_image_input(messages: Any) -> bool:
    """Return True when any message carries an image input (Phase 5: delegated)."""
    return _normalization.messages_contain_image_input(messages)


def _build_probe_image_data_url() -> str:
    """Build a tiny white PNG data URL for multimodal runtime probes (Phase 5: delegated)."""
    return _normalization.build_probe_image_data_url()


def _extract_backend_error_message(body: bytes) -> str:
    """Extract a human-readable error message from a backend error body (Phase 5: delegated)."""
    return _normalization.extract_backend_error_message(body)


def _truncate_error_message(message: str, limit: int = 300) -> str:
    """Collapse and truncate an error message (Phase 5: delegated)."""
    return _normalization.truncate_error_message(message, limit=limit)


def _openai_error_response(
    *,
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """Build a standard OpenAI-style error response (Phase 5: delegated)."""
    return _normalization.openai_error_response(
        status_code=status_code,
        message=message,
        error_type=error_type,
        code=code,
        headers=headers,
    )


async def _probe_multimodal_runtime(model_name: str) -> Dict[str, Any]:
    """Probe the loaded backend for vision capability (Phase 5: delegated)."""
    return await _normalization.probe_multimodal_runtime(model_name)


async def _preflight_multimodal_request(
    model_name: str,
    request_id: str,
    queue_wait_ms: float,
) -> Optional[JSONResponse]:
    """Return an error response when the backend cannot serve image input (Phase 5: delegated)."""
    return await _normalization.preflight_multimodal_request(model_name, request_id, queue_wait_ms)


def _desired_runtime_vision_enabled(model_name: str, has_image_inputs: bool) -> bool:
    """Return whether the backend should run with vision enabled (Phase 5: delegated)."""
    return _normalization.desired_runtime_vision_enabled(model_name, has_image_inputs)


def _model_disables_thinking_by_default(model_name: str) -> bool:
    """Return whether a configured model is a non-reasoning/special runtime (Phase 5: delegated)."""
    return _normalization.model_disables_thinking_by_default(model_name)


def _request_explicitly_disables_thinking(payload: Dict[str, Any]) -> bool:
    """Return whether the request body disables thinking explicitly (Phase 5: delegated)."""
    return _normalization.request_explicitly_disables_thinking(payload)


def _apply_anthropic_thinking_to_llama_params(payload: Dict[str, Any]) -> bool:
    """Translate Anthropic thinking blocks to llama-server params (Phase 5: delegated)."""
    return _normalization.apply_anthropic_thinking_to_llama_params(payload)


def _apply_request_reasoning_defaults(path: str, payload: Dict[str, Any], model_name: str) -> bool:
    """Apply model-specific reasoning defaults to the request body (Phase 5: delegated)."""
    return _normalization.apply_request_reasoning_defaults(path, payload, model_name)


def _stringify_message_content(content: Any) -> str:
    """Flatten message content blocks into a plain string (Phase 5: delegated)."""
    return _normalization.stringify_message_content(content)


def _sanitize_messages_for_qwen_chat_template(messages: Any) -> Any:
    """Strip unsupported content shapes for the qwen chat template (Phase 5: delegated)."""
    return _normalization.sanitize_messages_for_qwen_chat_template(messages)


def _map_multimodal_backend_error(
    model_name: str,
    status_code: int,
    body: bytes,
    request_id: str,
    queue_wait_ms: float,
) -> Optional[JSONResponse]:
    """Map multimodal backend failures to OpenAI error responses (Phase 5: delegated)."""
    return _normalization.map_multimodal_backend_error(
        model_name, status_code, body, request_id, queue_wait_ms,
    )


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

# Initialize usage tracking with the server State object
_usage.init(state)

# Initialize normalization with model manager + queue header helper
_normalization.init(
    model_manager=model_manager,
    llama_server_url=LLAMA_SERVER_URL,
    queue_headers=_queue_headers,
)


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

def _coerce_usage_int(value: object) -> int:
    """Convert token usage values to non-negative integers (Phase 5: delegated)."""
    return _usage.coerce_usage_int(value)


# Initialize capture dispatch with injected helpers
_capture_dispatch.init(get_request_auth_context, _coerce_usage_int)

# Initialize cloud routing with all dependencies
_cloud_routing.init(
    provider_registry, cloud_cred_store, failover_registry, failover_health,
    get_request_auth_context,
    _capture_dispatch.capture_client_fingerprint,
    _capture_dispatch.capture_endpoint_from_request,
    _capture_dispatch.dispatch_capture_request_received,
    get_capture_controller,
    _cloud_inf.provider_base_url,
    _cloud_inf.cloud_provider_for_request,
    _cloud_inf.cloud_provider_unavailable_error,
    _cloud_inf.adapt_openai_reasoning_params,
)


def _coerce_header_int(value: object) -> int:
    """Convert a header-like byte count to a non-negative integer (Phase 5: delegated)."""
    return _usage.coerce_header_int(value)


def _request_size_bytes(request: Request) -> int:
    """Best-effort byte count for the inbound request body (Phase 5: delegated)."""
    return _usage.request_size_bytes(request)


def _response_size_bytes(response: Response) -> int:
    """Best-effort byte count for the outbound response body (Phase 5: delegated)."""
    return _usage.response_size_bytes(response)


def _should_track_api_usage(path: str) -> bool:
    """Return whether the request path should count toward API usage (Phase 5: delegated)."""
    return _usage.should_track_api_usage(path)


def _get_usage_client_id(request: Request) -> Optional[str]:
    """Extract the authenticated client name attached by auth (Phase 5: delegated)."""
    return _usage.get_usage_client_id(request)


def _get_usage_attribution(request: Request) -> Optional[Dict[str, Any]]:
    """Return request attribution details collected during auth (Phase 5: delegated)."""
    return _usage.get_usage_attribution(request)


def _get_live_usage_request_id(request: Request) -> Optional[str]:
    """Return the dashboard request id bound to the current FastAPI request (Phase 5: delegated)."""
    return _usage.get_live_usage_request_id(request)


def _start_live_request_usage(request: Request) -> None:
    """Register the current API request as in-flight for dashboard polling (Phase 5: delegated)."""
    _usage.start_live_request_usage(request)


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
    """Push incremental request metadata into the live dashboard tracker (Phase 5: delegated)."""
    _usage.update_live_request_usage(
        request,
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
    """Finalize the live dashboard request entry and fold it into history (Phase 5: delegated)."""
    _usage.finish_live_request_usage(request, status_code=status_code, response_bytes=response_bytes)


def _set_request_usage_metadata(
    request: Request,
    *,
    model: Optional[str] = None,
    streamed: Optional[bool] = None,
) -> None:
    """Attach request metadata for dashboard usage snapshots (Phase 5: delegated)."""
    _usage.set_request_usage_metadata(request, model=model, streamed=streamed)


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
    """Store token usage for a completed request when available (Phase 5: delegated)."""
    _usage.record_request_token_usage(
        client_id,
        endpoint,
        model,
        request=request,
        attribution=attribution,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
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
    """Extract OpenAI-style usage fields from a JSON payload (Phase 5: delegated)."""
    _usage.record_usage_from_payload(
        client_id,
        endpoint,
        model,
        payload,
        request=request,
        attribution=attribution,
    )


@app.middleware("http")
async def track_api_usage_middleware(request: Request, call_next):
    """Track aggregate API usage for dashboard monitoring (Phase 5: delegated)."""
    return await _usage.track_api_usage(request, call_next)


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

# Initialize streaming helpers with queue + timeout constants
_streaming.init(inference_queue, _GuardianRequestCancelled, STREAM_HEARTBEAT_INTERVAL_S, STREAM_CLOSE_TIMEOUT_S)

# Initialize queue helpers with queue + usage helpers
_queue_helpers.init(inference_queue, _get_queue_owner_id, _update_live_request_usage, STREAM_CLOSE_TIMEOUT_S)

# Initialize cloud forwarding with all dependencies
_cloud_forwarding.init(
    resolve_cloud_attempts=_cloud_routing.resolve_cloud_attempts,
    prepare_cloud_candidate_request=_cloud_routing.prepare_cloud_candidate_request,
    extract_cloud_response_content=_cloud_routing.extract_cloud_response_content,
    guardian_debug_headers=_cloud_inf.guardian_debug_headers,
    is_retryable_cloud_error=_cloud_inf.is_retryable_cloud_error,
    sanitize_proxied_response_headers=_cloud_inf.sanitize_proxied_response_headers,
    messages_contain_image_input=_messages_contain_image_input,
    get_cloud_key_fingerprint=_get_cloud_key_fingerprint,
    set_request_usage_metadata=_set_request_usage_metadata,
    start_live_request_usage=_start_live_request_usage,
    update_live_request_usage=_update_live_request_usage,
    finish_live_request_usage=_finish_live_request_usage,
    record_request_token_usage=_record_request_token_usage,
    record_usage_from_payload=_record_usage_from_payload,
    coerce_usage_int=_coerce_usage_int,
    dispatch_capture_request_completed=_capture_dispatch.dispatch_capture_request_completed,
    dispatch_capture_request_cancelled=_capture_dispatch.dispatch_capture_request_cancelled,
    dispatch_capture_request_failed=_capture_dispatch.dispatch_capture_request_failed,
    classify_capture_error=_capture_dispatch.classify_capture_error,
    sanitize_capture_error_message=_capture_dispatch.sanitize_capture_error_message,
    iter_sse_lines_with_watchdog=_streaming.iter_sse_lines_with_watchdog,
    translate_openai_error_to_anthropic=translate_openai_error_to_anthropic,
    translate_openai_response_to_anthropic=translate_openai_response_to_anthropic,
    translate_openai_stream_to_anthropic=translate_openai_stream_to_anthropic,
    rate_limiter=cloud_rate_limiter,
    health_tracker=failover_health,
    guardian_request_cancelled=_GuardianRequestCancelled,
    stream_heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S,
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
    """Bridge Ollama-style chat requests to OpenAI-style Llama Server.

    Phase 5: implementation extracted to :mod:`app.local_inference.ollama`.
    """
    return await _local_ollama.chat_ollama(request, client_id)

@app.post("/api/generate")
async def proxy_generate_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Bridge Ollama /api/generate (prompt-based) to /api/chat logic.

    Phase 5: implementation extracted to :mod:`app.local_inference.ollama`.
    """
    return await _local_ollama.generate_ollama(request, client_id)

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

# ── Cloud inference helpers (delegated to app.cloud_inference) ──────
# Phase 5: extracted to app/cloud_inference/__init__.py.  These thin
# wrappers preserve the existing call sites in server.py.

_PROVIDER_BASE_URLS = _cloud_inf.get_provider_base_urls()
_GOOGLE_MODEL_CATALOG_URL = _cloud_inf._GOOGLE_MODEL_CATALOG_URL
_GOOGLE_MODEL_CATALOG_TIMEOUT_S = _cloud_inf._GOOGLE_MODEL_CATALOG_TIMEOUT_S

def _normalize_google_model_id(model_id: str) -> str:
    return _cloud_inf.normalize_google_model_id(model_id)

def _parse_google_model_catalog(payload: Any) -> List[str]:
    return _cloud_inf.parse_google_model_catalog(payload)

async def _discover_google_models(api_key: str) -> List[str]:
    return await _cloud_inf.discover_google_models(api_key)

def _provider_base_url(provider_name: str) -> str:
    return _cloud_inf.provider_base_url(provider_name)

def _cloud_provider_for_request(model_name: str) -> Optional[CloudProvider]:
    return _cloud_inf.cloud_provider_for_request(model_name)

def _is_cloud_or_guardian_route(model_name: str) -> bool:
    return _cloud_inf.is_cloud_or_guardian_route(model_name)

def _cloud_provider_unavailable_error(provider: CloudProvider) -> HTTPException:
    return _cloud_inf.cloud_provider_unavailable_error(provider)

_RETRYABLE_STATUS_CODES = _cloud_inf._RETRYABLE_STATUS_CODES
_DEGRADED_ERROR_MARKERS = _cloud_inf._DEGRADED_ERROR_MARKERS

def _is_retryable_cloud_error(status_code: int, error_body_text: str) -> bool:
    return _cloud_inf.is_retryable_cloud_error(status_code, error_body_text)

_HOP_BY_HOP_RESPONSE_HEADERS = _cloud_inf._HOP_BY_HOP_RESPONSE_HEADERS

def _sanitize_proxied_response_headers(headers: Any) -> Dict[str, str]:
    return _cloud_inf.sanitize_proxied_response_headers(headers)

def _guardian_debug_headers(
    provider: CloudProvider,
    upstream_model: str,
    failover_group: Optional[str],
) -> Dict[str, str]:
    return _cloud_inf.guardian_debug_headers(provider, upstream_model, failover_group)


# ── Cloud routing (delegated to app.cloud_inference.routing) ────────
# Phase 5: extracted to app/cloud_inference/routing.py.

def _resolve_cloud_attempts(model_name: str, request: Request, client_id: str, *, requires_vision: bool = False) -> Tuple[List[Tuple[CloudProvider, str]], Optional[str]]:
    return _cloud_routing.resolve_cloud_attempts(model_name, request, client_id, requires_vision=requires_vision)

def _resolve_cloud_vision_fallback(model_name: str) -> Optional[str]:
    return _cloud_routing.resolve_cloud_vision_fallback(model_name)

# OpenAI reasoning wrappers (delegated to app.cloud_inference)
def _is_openai_reasoning_model(model_name: str) -> bool:
    return _cloud_inf.is_openai_reasoning_model(model_name)

def _adapt_openai_reasoning_params(provider: CloudProvider, upstream_model: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _cloud_inf.adapt_openai_reasoning_params(provider, upstream_model, body)

def _prepare_cloud_candidate_request(provider: CloudProvider, upstream_model: str, path: str, base_json_body: Dict[str, Any], client_user_id: Optional[str] = None) -> Tuple[str, Dict[str, Any], bytes, bool]:
    return _cloud_routing.prepare_cloud_candidate_request(provider, upstream_model, path, base_json_body, client_user_id)

def _extract_cloud_response_content(payload: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[list]]:
    return _cloud_routing.extract_cloud_response_content(payload)

def _setup_cloud_capture(request: Request, client_id: str, *, model_name: str, json_body: Dict[str, Any], path: str) -> Tuple[Optional[BuildContext], Optional["PolicyResult"], Optional[str], Optional[float]]:
    return _cloud_routing.setup_cloud_capture(request, client_id, model_name=model_name, json_body=json_body, path=path)
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

    Phase 5: implementation extracted to :mod:`app.cloud_inference.forwarding`;
    this wrapper preserves the server-side call sites and test surface.
    """
    return await _cloud_forwarding.forward_to_cloud_provider(
        path,
        body,
        json_body,
        model_name,
        request,
        client_id,
        capture_ctx=capture_ctx,
        capture_policy_result=capture_policy_result,
        cloud_request_id=cloud_request_id,
        cloud_capture_start_time=cloud_capture_start_time,
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

# Initialize local Ollama inference with all dependencies
_local_ollama.init(
    resolve_or_reject_inference_model=_resolve_or_reject_inference_model,
    is_cloud_or_guardian_route=_is_cloud_or_guardian_route,
    forward_to_cloud_provider=_forward_to_cloud_provider,
    begin_queued_request=_queue_helpers.begin_queued_request,
    request_cancel_http_exception=_queue_helpers.request_cancel_http_exception,
    capture_client_fingerprint=_capture_dispatch.capture_client_fingerprint,
    dispatch_capture_request_received=_capture_dispatch.dispatch_capture_request_received,
    resolve_auto_reload_model=_resolve_auto_reload_model,
    reset_startup_check_status=_reset_startup_check_status,
    run_guardian_operation=_run_guardian_operation,
    set_request_usage_metadata=_set_request_usage_metadata,
    build_stream_timeout=_streaming.build_stream_timeout,
    await_or_cancel_request=_queue_helpers.await_or_cancel_request,
    close_on_request_cancel=_queue_helpers.close_on_request_cancel,
    close_stream_resources=_queue_helpers.close_stream_resources,
    iter_sse_lines_with_watchdog=_streaming.iter_sse_lines_with_watchdog,
    coerce_usage_int=_coerce_usage_int,
    extract_assistant_delta_text=_streaming.extract_assistant_delta_text,
    update_live_request_usage=_update_live_request_usage,
    record_request_token_usage=_record_request_token_usage,
    finish_live_request_usage=_finish_live_request_usage,
    dispatch_capture_stream_completed=_capture_dispatch.dispatch_capture_stream_completed,
    request_outcome=_queue_helpers.request_outcome,
    stop_background_task=_queue_helpers.stop_background_task,
    extract_assistant_message_text=_streaming.extract_assistant_message_text,
    record_usage_from_payload=_record_usage_from_payload,
    dispatch_capture_request_cancelled=_capture_dispatch.dispatch_capture_request_cancelled,
    dispatch_capture_request_failed=_capture_dispatch.dispatch_capture_request_failed,
    classify_capture_error=_capture_dispatch.classify_capture_error,
    sanitize_capture_error_message=_capture_dispatch.sanitize_capture_error_message,
    dispatch_capture_nonstream_completed=_capture_dispatch.dispatch_capture_nonstream_completed,
    get_model_timeout=get_model_timeout,
    guardian_request_cancelled=_GuardianRequestCancelled,
    model_switch_lock=_model_switch_lock,
    llama_server_url=LLAMA_SERVER_URL,
    model_manager=model_manager,
    inference_queue=inference_queue,
    capture_controller=capture_controller,
)


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
