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

# ── Gateway v1 routing (Phase 5 extraction) ─────────────────────────
from app.gateway import routing as _gw_routing

# ── Proxy process management (Phase 5 extraction) ───────────────────
from app.proxy import process as _process

# ── Local model helpers (Phase 5 extraction) ──────────────────────────
from app.local_inference import models as _local_models

# ── Model discovery (Phase 5 extraction) ─────────────────────────────
from app.gateway import model_discovery as _model_discovery

# ── Admin API (Phase 5 extraction) ────────────────────────────────────
from app.gateway import admin_api as _admin_api

# ── Proxy lifespan (Phase 5 extraction) ──────────────────────────────
from app.proxy import lifespan as _lifespan

# ── Session slots (Phase 5 extraction) ───────────────────────────────
from app.gateway import sessions as _sessions

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
_VISION_PROBE_IMAGE_DATA_URL: Optional[str] = None


def _get_pid_file_path() -> Path:
    """Return the guardian pid file path (Phase 5: delegated)."""
    return _process.get_pid_file_path()


def _describe_process(pid: int) -> Optional[str]:
    """Describe a process via ps (Phase 5: delegated)."""
    return _process.describe_process(pid)


def _get_process_cgroup(pid: int) -> Optional[str]:
    """Return the cgroup slice for a pid (Phase 5: delegated)."""
    return _process.get_process_cgroup(pid)


def _get_proxy_listener_info(port: int = PROXY_PORT) -> Optional[Dict[str, Optional[object]]]:
    """Inspect the process listening on the proxy port (Phase 5: delegated)."""
    return _process.get_proxy_listener_info(port=port)


def _get_pid_file_status() -> Dict[str, Optional[object]]:
    """Return pid file existence/alive status (Phase 5: delegated)."""
    return _process.get_pid_file_status()


async def _wait_for_proxy_listener_release(old_pid: int, timeout: float = 3.0) -> bool:
    """Wait until the old pid no longer owns the proxy port (Phase 5: delegated)."""
    return await _process.wait_for_proxy_listener_release(old_pid, timeout=timeout)


def _is_guardian_uvicorn_listener(listener: Optional[Dict[str, Optional[object]]]) -> bool:
    """Return whether a listener looks like this Guardian app (Phase 5: delegated)."""
    return _process.is_guardian_uvicorn_listener(listener)


async def _stop_stale_guardian_listener(
    listener: Optional[Dict[str, Optional[object]]], timeout: float = 3.0
) -> bool:
    """Terminate a stale Guardian listener before rebinding (Phase 5: delegated)."""
    return await _process.stop_stale_guardian_listener(listener, timeout=timeout)


def _operation_state_for_phase(phase: str) -> str:
    """Map an operation phase to its startup-state value (Phase 5: delegated)."""
    return _process.operation_state_for_phase(phase)


def _startup_state_is_in_progress(state: Optional[str]) -> bool:
    """Return whether a startup state is still in progress (Phase 5: delegated)."""
    return _process.startup_state_is_in_progress(state)


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




def _reset_startup_check_status(
    *,
    source: str,
    phase: str,
    target_model: Optional[str],
    requested_model: Optional[str] = None,
    owner: Optional[str] = None,
) -> int:
    """Start a new startup-check generation (Phase 5: delegated)."""
    return _process.reset_startup_check_status(
        source=source,
        phase=phase,
        target_model=target_model,
        requested_model=requested_model,
        owner=owner,
    )


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
    """Update the startup-check state machine (Phase 5: delegated)."""
    _process.mark_startup_check_status(
        state,
        error,
        generation=generation,
        phase=phase,
        source=source,
        owner=owner,
        target_model=target_model,
        requested_model=requested_model,
        effective_model=effective_model,
    )


def _get_startup_check_status() -> Dict[str, Optional[object]]:
    """Return a snapshot of the startup-check state (Phase 5: delegated)."""
    return _process.get_startup_check_status()


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
    """Run a model operation with startup-state tracking (Phase 5: delegated)."""
    return await _process.run_guardian_operation(
        source=source,
        phase=phase,
        target_model=target_model,
        requested_model=requested_model,
        owner=owner,
        operation=operation,
        generation=generation,
    )


async def _run_startup_check_in_background(generation: int, target_model: Optional[str]) -> None:
    """Run the startup check under the switch lock (Phase 5: delegated)."""
    await _process.run_startup_check_in_background(generation, target_model)


def _resolve_inference_model(raw_model: Optional[str], current_model: str) -> Optional[str]:
    """Resolve an inference model name (Phase 5: delegated)."""
    return _local_models.resolve_inference_model(raw_model, current_model)


def _reject_unserved_inference_model(raw_model: Optional[str]) -> None:
    """Raise a client-facing error for a model Guardian does not serve (Phase 5: delegated)."""
    _local_models.reject_unserved_inference_model(raw_model)


def _resolve_or_reject_inference_model(raw_model: Optional[str], current_model: str) -> str:
    """Resolve an inference model name and reject unknown or unserved values (Phase 5: delegated)."""
    return _local_models.resolve_or_reject_inference_model(raw_model, current_model)


def _resolve_auto_reload_model(requested_model: Optional[str] = None) -> str:
    """Resolve the model Guardian should load when the backend is absent (Phase 5: delegated)."""
    return _local_models.resolve_auto_reload_model(requested_model)


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
    """Startup/shutdown orchestration (Phase 5: delegated to app.proxy.lifespan)."""
    async with _lifespan.run_lifespan(app):
        yield


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
    """Background task: auto-unload llama-server after N minutes of inactivity (Phase 5: delegated)."""
    await _lifespan.idle_unload_watcher()


def get_gpu_metrics():
    """Query nvidia-smi totals (Phase 5: delegated)."""
    return _local_models.get_gpu_metrics()


def get_model_size(model_name: str) -> int:
    """Estimate model VRAM footprint in MB (Phase 5: delegated)."""
    return _local_models.get_model_size(model_name)


def get_model_timeout(model_name: str) -> int:
    """Calculate timeout based on model size using config tiers (Phase 5: delegated)."""
    return _local_models.get_model_timeout(model_name)


# Model switch concurrency lock - prevents race conditions when
# multiple requests try to switch models simultaneously
_model_switch_lock = asyncio.Lock()

# Initialize process management with constants + singletons
_process.init(
    model_manager=model_manager,
    model_switch_lock=_model_switch_lock,
    pid_file=PID_FILE,
    proxy_port=PROXY_PORT,
)



# Auth replaced by verify_api_key imported from app.proxy.auth

# VRAM scheduler + model helpers (Phase 5: delegated to app.local_inference.models)
VramScheduler = _local_models.VramScheduler
_local_models.init(
    model_manager=model_manager,
    provider_registry=provider_registry,
    parse_guardian_route=parse_guardian_route,
    config=CONFIG,
    safe_vram_limit_mb=SAFE_VRAM_LIMIT_MB,
    model_switch_lock=_model_switch_lock,
    reset_startup_check_status=_reset_startup_check_status,
    run_guardian_operation=_run_guardian_operation,
    model_load_error_cls=ModelLoadError,
)




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

_lifespan.init(
    proxy_port=PROXY_PORT,
    pid_file=PID_FILE,
    get_pid_file_path=_process.get_pid_file_path,
    get_pid_file_status=_process.get_pid_file_status,
    get_proxy_listener_info=_process.get_proxy_listener_info,
    wait_for_proxy_listener_release=_process.wait_for_proxy_listener_release,
    is_guardian_uvicorn_listener=_process.is_guardian_uvicorn_listener,
    stop_stale_guardian_listener=_process.stop_stale_guardian_listener,
    reset_startup_check_status=_process.reset_startup_check_status,
    mark_startup_check_status=_process.mark_startup_check_status,
    operation_state_for_phase=_process.operation_state_for_phase,
    run_startup_check_in_background=_process.run_startup_check_in_background,
    set_startup_check_task=_process.set_startup_check_task,
    cancel_startup_check_task=_process.cancel_startup_check_task,
    model_manager=model_manager,
    capture_controller=capture_controller,
    inference_queue=inference_queue,
)

# Initialize session slots with the llama-server URL + slots dir
_sessions.init(
    llama_server_url=LLAMA_SERVER_URL,
    session_slots_dir=Path("/home/flip/llama_slots"),
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
    """Reload llama-server once after Guardian detects stale backend state (Phase 5: delegated)."""
    await _local_models.reload_backend_after_connect_error(path, error)


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
    """Simulate Ollama /api/tags endpoint (Phase 5: delegated)."""
    return await _model_discovery.tags_ollama()


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
    """List available models from config and cloud providers (Phase 5: delegated)."""
    return await _model_discovery.list_models(request, client_id)


@app.get("/v1/models/{model_id:path}")
async def get_model_metadata(
    model_id: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """Return metadata for a configured canonical model, public alias, or cloud model (Phase 5: delegated)."""
    return await _model_discovery.model_metadata(model_id, request, client_id)


@app.post("/api/show")
async def show_model_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Return Ollama-compatible metadata with an always-present context size (Phase 5: delegated)."""
    return await _model_discovery.show_model(request, client_id)


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
    """Reload llama-server. Optionally pass {"model": "name"} to load a specific model (Phase 5: delegated)."""
    return await _admin_api.admin_load(request, client_id)


# ── Cloud credential management API ───────────────────────────────────


@app.get("/api/keys")
async def list_api_keys(client_id: str = Depends(verify_api_key)):
    """list_api_keys (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.list_api_keys(client_id)


@app.post("/api/keys")
async def create_api_key(request: Request, client_id: str = Depends(verify_api_key)):
    """create_api_key (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.create_api_key(request, client_id)



@app.get("/api/cloud/credentials")
async def list_cloud_credentials(request: Request, client_id: str = Depends(verify_api_key)):
    """list_cloud_credentials (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.list_cloud_credentials(request, client_id)


@app.post("/api/cloud/credentials")
async def add_cloud_credential(request: Request, client_id: str = Depends(verify_api_key)):
    """add_cloud_credential (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.add_cloud_credential(request, client_id)


@app.post("/api/cloud/credentials/{cred_id}/refresh-models")
async def refresh_cloud_credential_models(
    cred_id: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """refresh_cloud_credential_models (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.refresh_cloud_credential_models(cred_id, request, client_id)


@app.delete("/api/cloud/credentials/{cred_id}")
async def delete_cloud_credential(
    cred_id: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """delete_cloud_credential (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.delete_cloud_credential(cred_id, request, client_id)


@app.post("/api/cloud/credentials/{cred_id}/models")
async def add_model_to_credential(cred_id: str, request: Request, client_id: str = Depends(verify_api_key)):
    """add_model_to_credential (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.add_model_to_credential(cred_id, request, client_id)


@app.delete("/api/cloud/credentials/{cred_id}/models/{model_name:path}")
async def remove_model_from_credential(
    cred_id: str,
    model_name: str,
    request: Request,
    client_id: str = Depends(verify_api_key),
):
    """remove_model_from_credential (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.remove_model_from_credential(cred_id, model_name, request, client_id)


@app.get("/api/cloud/links")
async def list_cloud_links(request: Request, client_id: str = Depends(verify_api_key)):
    """list_cloud_links (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.list_cloud_links(request, client_id)


@app.get("/api/cloud/ratelimit-stats")
async def get_cloud_ratelimit_stats(request: Request, client_id: str = Depends(verify_api_key)):
    """get_cloud_ratelimit_stats (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.get_cloud_ratelimit_stats(request, client_id)


@app.post("/api/cloud/links")
async def link_credential(request: Request, client_id: str = Depends(verify_api_key)):
    """link_credential (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.link_credential(request, client_id)


@app.delete("/api/cloud/links")
async def unlink_credential(request: Request, client_id: str = Depends(verify_api_key)):
    """unlink_credential (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.unlink_credential(request, client_id)


@app.get("/api/cloud/providers")
async def list_cloud_providers(client_id: str = Depends(verify_api_key)):
    """list_cloud_providers (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.list_cloud_providers(client_id)


@app.get("/api/cloud/models")
async def list_cloud_models(request: Request, client_id: str = Depends(verify_api_key)):
    """list_cloud_models (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.list_cloud_models(request, client_id)



@app.get("/api/crashes")
async def get_crash_history(client_id: str = Depends(verify_api_key)):
    """get_crash_history (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.get_crash_history(client_id)


@app.get("/api/status")
async def get_server_status(client_id: str = Depends(verify_api_key)):
    """get_server_status (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.get_server_status(client_id)


@app.get("/api/capture/status")
async def get_capture_status(client_id: str = Depends(verify_api_key)):
    """get_capture_status (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.get_capture_status(client_id)


@app.post("/api/capture/rotate")
async def rotate_capture_file(client_id: str = Depends(verify_api_key)):
    """rotate_capture_file (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.rotate_capture_file(client_id)



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
    """get_scaler_config (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.get_scaler_config(client_id)


@app.put("/api/scaler")
async def update_scaler_config(request: Request, client_id: str = Depends(verify_api_key)):
    """update_scaler_config (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.update_scaler_config(request, client_id)


@app.post("/api/scaler/reset")
async def reset_scaler_config(client_id: str = Depends(verify_api_key)):
    """reset_scaler_config (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.reset_scaler_config(client_id)


@app.post("/api/scaler/recommend")
async def scaler_recommend(request: Request, client_id: str = Depends(verify_api_key)):
    """scaler_recommend (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.scaler_recommend(request, client_id)



# --- Queue status endpoint (non-queued, always immediately available) ---

@app.get("/v1/queue/status")
async def queue_status(request: Request, client_id: str = Depends(verify_api_key)):
    """queue_status (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.queue_status(request, client_id)


@app.get("/v1/queue/requests/{request_id}")
async def queue_request_status(request_id: str, request: Request, client_id: str = Depends(verify_api_key)):
    """queue_request_status (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.queue_request_status(request_id, request, client_id)


@app.delete("/v1/queue/requests/{request_id}")
async def cancel_queue_request(request_id: str, request: Request, client_id: str = Depends(verify_api_key)):
    """cancel_queue_request (Phase 5: delegated to app.gateway.admin_api)."""
    return await _admin_api.cancel_queue_request(request_id, request, client_id)



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
    """OpenAI-compatible inference endpoint — cloud/local dispatch.

    Phase 5: implementation extracted to :mod:`app.gateway.routing`;
    this wrapper preserves the route registration and test surface.
    """
    return await _gw_routing.route_v1_post(path, request, client_id)

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

# Initialize gateway v1 routing with all dependencies
_gw_routing.init(
    resolve_or_reject_inference_model=_resolve_or_reject_inference_model,
    is_cloud_or_guardian_route=_is_cloud_or_guardian_route,
    resolve_cloud_attempts=_cloud_routing.resolve_cloud_attempts,
    resolve_cloud_vision_fallback=_cloud_routing.resolve_cloud_vision_fallback,
    setup_cloud_capture=_cloud_routing.setup_cloud_capture,
    forward_to_cloud_provider=_forward_to_cloud_provider,
    apply_anthropic_thinking_to_llama_params=_normalization.apply_anthropic_thinking_to_llama_params,
    apply_request_reasoning_defaults=_normalization.apply_request_reasoning_defaults,
    sanitize_messages_for_qwen_chat_template=_normalization.sanitize_messages_for_qwen_chat_template,
    messages_contain_image_input=_normalization.messages_contain_image_input,
    begin_queued_request=_queue_helpers.begin_queued_request,
    request_cancel_http_exception=_queue_helpers.request_cancel_http_exception,
    capture_client_fingerprint=_capture_dispatch.capture_client_fingerprint,
    capture_endpoint_from_request=_capture_dispatch.capture_endpoint_from_request,
    capture_ingress_protocol=_capture_dispatch.capture_ingress_protocol,
    dispatch_capture_request_received=_capture_dispatch.dispatch_capture_request_received,
    dispatch_capture_request_cancelled=_capture_dispatch.dispatch_capture_request_cancelled,
    dispatch_capture_request_failed=_capture_dispatch.dispatch_capture_request_failed,
    dispatch_capture_nonstream_completed=_capture_dispatch.dispatch_capture_nonstream_completed,
    dispatch_capture_stream_completed=_capture_dispatch.dispatch_capture_stream_completed,
    classify_capture_error=_capture_dispatch.classify_capture_error,
    sanitize_capture_error_message=_capture_dispatch.sanitize_capture_error_message,
    resolve_auto_reload_model=_resolve_auto_reload_model,
    reset_startup_check_status=_reset_startup_check_status,
    run_guardian_operation=_run_guardian_operation,
    desired_runtime_vision_enabled=_normalization.desired_runtime_vision_enabled,
    preflight_multimodal_request=_normalization.preflight_multimodal_request,
    map_multimodal_backend_error=_normalization.map_multimodal_backend_error,
    set_request_usage_metadata=_set_request_usage_metadata,
    update_live_request_usage=_update_live_request_usage,
    finish_live_request_usage=_finish_live_request_usage,
    record_request_token_usage=_record_request_token_usage,
    record_usage_from_payload=_record_usage_from_payload,
    coerce_usage_int=_coerce_usage_int,
    get_model_timeout=get_model_timeout,
    build_stream_timeout=_streaming.build_stream_timeout,
    await_or_cancel_request=_queue_helpers.await_or_cancel_request,
    close_on_request_cancel=_queue_helpers.close_on_request_cancel,
    close_stream_resources=_queue_helpers.close_stream_resources,
    iter_sse_lines_with_watchdog=_streaming.iter_sse_lines_with_watchdog,
    extract_assistant_delta_text=_streaming.extract_assistant_delta_text,
    stringify_message_content=_normalization.stringify_message_content,
    reload_backend_after_connect_error=_reload_backend_after_connect_error,
    request_outcome=_queue_helpers.request_outcome,
    stop_background_task=_queue_helpers.stop_background_task,
    queue_headers=_queue_helpers.queue_headers,
    enrich_anthropic_response=_streaming.enrich_anthropic_response,
    enrich_anthropic_sse_line=_streaming.enrich_anthropic_sse_line,
    guardian_request_cancelled=_GuardianRequestCancelled,
    model_switch_lock=_model_switch_lock,
    llama_server_url=LLAMA_SERVER_URL,
    stream_heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S,
    model_manager=model_manager,
    inference_queue=inference_queue,
    capture_controller=capture_controller,
)

# Initialize model discovery with injected helpers (after _gw_routing.init so
# the shared cloud-attempt resolver is available)
_model_discovery.init(
    _model_manager=model_manager,
    _provider_registry=provider_registry,
    _cloud_cred_store=cloud_cred_store,
    _failover_registry=failover_registry,
    _get_request_auth_context=get_request_auth_context,
    _parse_guardian_route=parse_guardian_route,
    _CloudProvider=CloudProvider,
    _provider_base_url=_cloud_inf.provider_base_url,
    _resolve_cloud_attempts=_cloud_routing.resolve_cloud_attempts,
    _build_model_metadata_entry=_build_model_metadata_entry,
    _enrich_model_context_metadata=_enrich_model_context_metadata,
    _resolve_context_window=_resolve_context_window,
    _get_model_size=get_model_size,
)

# Initialize admin API with injected helpers (after all helpers are defined)
_admin_api.init(
    _model_manager=model_manager,
    _provider_registry=provider_registry,
    _cloud_cred_store=cloud_cred_store,
    _cloud_rate_limiter=cloud_rate_limiter,
    _inference_queue=inference_queue,
    _state=state,
    _llama_server_url=LLAMA_SERVER_URL,
    _proxy_port=PROXY_PORT,
    _PROVIDER_BASE_URLS=_PROVIDER_BASE_URLS,
    _get_cloud_key_fingerprint=_get_cloud_key_fingerprint,
    _get_request_auth_context=get_request_auth_context,
    _get_queue_owner_id=_get_queue_owner_id,
    _get_startup_check_status=_get_startup_check_status,
    _startup_state_is_in_progress=_startup_state_is_in_progress,
    _get_proxy_listener_info=_get_proxy_listener_info,
    _get_pid_file_status=_get_pid_file_status,
    _get_capture_controller=get_capture_controller,
    _get_gpu_metrics=get_gpu_metrics,
    _get_model_size=get_model_size,
    _discover_google_models=_discover_google_models,
    _load_api_keys=load_api_keys,
    _generate_api_key=generate_api_key,
    _token_fingerprint=_token_fingerprint,
    _model_switch_lock=_model_switch_lock,
    _reset_startup_check_status=_reset_startup_check_status,
    _run_guardian_operation=_run_guardian_operation,
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


def _sanitize_session_filename(raw: object) -> str:
    """Return a safe basename for a session slot, or raise HTTP 400 (Phase 5: delegated)."""
    return _sessions.sanitize_session_filename(raw)


@app.post("/api/session/save")
async def save_session(request: Request, client_id: str = Depends(verify_api_key)):
    """Save the current llama-server slot state to a session file (Phase 5: delegated)."""
    return await _sessions.save_session(request, client_id)


@app.post("/api/session/load")
async def load_session(request: Request, client_id: str = Depends(verify_api_key)):
    """Restore a llama-server slot state from a session file (Phase 5: delegated)."""
    return await _sessions.load_session(request, client_id)


@app.get("/api/session/list")
async def list_sessions(client_id: str = Depends(verify_api_key)):
    """List available session files (Phase 5: delegated)."""
    return await _sessions.list_sessions(client_id)


if __name__ == "__main__":
    import asyncio
    asyncio.run(start_proxy())
