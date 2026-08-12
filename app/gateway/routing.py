"""Gateway v1 routing — the /v1/{path} inference dispatch node.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Routes inference requests: count_tokens interception, cloud-vs-local dispatch
with vision fallback, queue admission, model auto-reload/switch, multimodal
preflight, local llama-server transport (streaming + non-streaming), Anthropic
enrichment, usage tracking, and capture dispatch.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.engine.manager import ModelLoadError
from app.capture.config import PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI, ROUTE_LOCAL
from app.capture.redactor import anthropic_messages_to_openai
from app.capture.schema import BuildContext
from app.capture.policy import PolicyResult
from app.capture.stream_assembler import StreamResponseAssembler
from app.gateway.streaming import StreamProgressWatchdog
from app.proxy.anthropic_bridge import _format_sse_event

import logging

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_resolve_or_reject_inference_model = None
_resolve_inference_model = None
_is_cloud_or_guardian_route = None
_resolve_cloud_attempts = None
_resolve_cloud_vision_fallback = None
_setup_cloud_capture = None
_forward_to_cloud_provider = None
_apply_anthropic_thinking_to_llama_params = None
_apply_request_reasoning_defaults = None
_sanitize_messages_for_qwen_chat_template = None
_messages_contain_image_input = None
_begin_queued_request = None
_request_cancel_http_exception = None
_capture_client_fingerprint = None
_capture_endpoint_from_request = None
_capture_ingress_protocol = None
_dispatch_capture_request_received = None
_dispatch_capture_request_cancelled = None
_dispatch_capture_request_failed = None
_dispatch_capture_nonstream_completed = None
_dispatch_capture_stream_completed = None
_classify_capture_error = None
_sanitize_capture_error_message = None
_resolve_auto_reload_model = None
_reset_startup_check_status = None
_run_guardian_operation = None
_desired_runtime_vision_enabled = None
_preflight_multimodal_request = None
_map_multimodal_backend_error = None
_set_request_usage_metadata = None
_update_live_request_usage = None
_finish_live_request_usage = None
_record_request_token_usage = None
_record_usage_from_payload = None
_coerce_usage_int = None
_get_model_timeout = None
_build_stream_timeout = None
_await_or_cancel_request = None
_close_on_request_cancel = None
_close_stream_resources = None
_iter_sse_lines_with_watchdog = None
_extract_assistant_delta_text = None
_stringify_message_content = None
_reload_backend_after_connect_error = None
_request_outcome = None
_stop_background_task = None
_queue_headers = None
_enrich_anthropic_response = None
_enrich_anthropic_sse_line = None
_GuardianRequestCancelled = None
_model_switch_lock = None
_llama_server_url = None
_stream_heartbeat_interval_s = None
_model_manager = None
_inference_queue = None
_capture_controller = None


def init(
    *,
    resolve_or_reject_inference_model,
    resolve_inference_model,
    is_cloud_or_guardian_route,
    resolve_cloud_attempts,
    resolve_cloud_vision_fallback,
    setup_cloud_capture,
    forward_to_cloud_provider,
    apply_anthropic_thinking_to_llama_params,
    apply_request_reasoning_defaults,
    sanitize_messages_for_qwen_chat_template,
    messages_contain_image_input,
    begin_queued_request,
    request_cancel_http_exception,
    capture_client_fingerprint,
    capture_endpoint_from_request,
    capture_ingress_protocol,
    dispatch_capture_request_received,
    dispatch_capture_request_cancelled,
    dispatch_capture_request_failed,
    dispatch_capture_nonstream_completed,
    dispatch_capture_stream_completed,
    classify_capture_error,
    sanitize_capture_error_message,
    resolve_auto_reload_model,
    reset_startup_check_status,
    run_guardian_operation,
    desired_runtime_vision_enabled,
    preflight_multimodal_request,
    map_multimodal_backend_error,
    set_request_usage_metadata,
    update_live_request_usage,
    finish_live_request_usage,
    record_request_token_usage,
    record_usage_from_payload,
    coerce_usage_int,
    get_model_timeout,
    build_stream_timeout,
    await_or_cancel_request,
    close_on_request_cancel,
    close_stream_resources,
    iter_sse_lines_with_watchdog,
    extract_assistant_delta_text,
    stringify_message_content,
    reload_backend_after_connect_error,
    request_outcome,
    stop_background_task,
    queue_headers,
    enrich_anthropic_response,
    enrich_anthropic_sse_line,
    guardian_request_cancelled,
    model_switch_lock,
    llama_server_url,
    stream_heartbeat_interval_s,
    model_manager,
    inference_queue,
    capture_controller,
) -> None:
    """Inject all dependencies. Called once at startup."""
    _vars = [
        "_resolve_or_reject_inference_model", "_resolve_inference_model",
        "_is_cloud_or_guardian_route",
        "_resolve_cloud_attempts", "_resolve_cloud_vision_fallback",
        "_setup_cloud_capture", "_forward_to_cloud_provider",
        "_apply_anthropic_thinking_to_llama_params", "_apply_request_reasoning_defaults",
        "_sanitize_messages_for_qwen_chat_template", "_messages_contain_image_input",
        "_begin_queued_request", "_request_cancel_http_exception",
        "_capture_client_fingerprint", "_capture_endpoint_from_request",
        "_capture_ingress_protocol", "_dispatch_capture_request_received",
        "_dispatch_capture_request_cancelled", "_dispatch_capture_request_failed",
        "_dispatch_capture_nonstream_completed", "_dispatch_capture_stream_completed",
        "_classify_capture_error", "_sanitize_capture_error_message",
        "_resolve_auto_reload_model", "_reset_startup_check_status",
        "_run_guardian_operation", "_desired_runtime_vision_enabled",
        "_preflight_multimodal_request", "_map_multimodal_backend_error",
        "_set_request_usage_metadata", "_update_live_request_usage",
        "_finish_live_request_usage", "_record_request_token_usage",
        "_record_usage_from_payload", "_coerce_usage_int", "_get_model_timeout",
        "_build_stream_timeout", "_await_or_cancel_request",
        "_close_on_request_cancel", "_close_stream_resources",
        "_iter_sse_lines_with_watchdog", "_extract_assistant_delta_text",
        "_stringify_message_content", "_reload_backend_after_connect_error",
        "_request_outcome", "_stop_background_task", "_queue_headers",
        "_enrich_anthropic_response", "_enrich_anthropic_sse_line",
        "_model_switch_lock", "_llama_server_url",
        "_stream_heartbeat_interval_s", "_model_manager", "_inference_queue",
        "_capture_controller",
    ]
    for _v in _vars:
        globals()[_v] = locals()[_v[1:]]
    # Special cases: camelCase global vs snake_case parameter, plus an
    # underscore-prefixed param that does not map 1:1 via the loop above.
    globals()["_GuardianRequestCancelled"] = guardian_request_cancelled
    globals()["_get_model_timeout"] = get_model_timeout


async def route_v1_post(path: str, request: Request, client_id: str):
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
                f"{_llama_server_url}/v1/{path}",
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

    current_model = await _model_manager.get_current_model()
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
            capture_policy_version=_capture_controller.config.policy_version,
            instance_id=_capture_controller.config.instance_id,
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
        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
    )
    if _capture_policy_result is not None and _capture_policy_result.should_capture:
        _capture_ctx = BuildContext(
            request_id=request_id,
            endpoint=_capture_endpoint,
            ingress_protocol=_capture_protocol,
            route_type=ROUTE_LOCAL,
            requested_model=requested_model,
            resolved_model=requested_model,
            capture_policy_version=_capture_controller.config.policy_version,
            instance_id=_capture_controller.config.instance_id,
            client_fingerprint=_capture_client_fp,
        )

    _release_in_finally = True
    capture_dispatched = False
    try:
        # If llama-server was unloaded, auto-reload before forwarding
        if _model_manager.is_unloaded:
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
                    if _model_manager.is_unloaded:  # double-check under lock
                        await _run_guardian_operation(
                            source="proxy",
                            phase="auto_reload",
                            target_model=reload_model,
                            requested_model=requested_model or current_model,
                            owner=client_id,
                            operation=lambda: _model_manager.load(
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
        _model_manager.last_request_time = time.time()
        _model_manager.active_requests += 1

        # Auto-switch logic for GPU-backed inference routes (with concurrency lock)
        if path in ("chat/completions", "messages", "completions", "embeddings"):
            try:
                current_model = await _model_manager.get_current_model()
                desired_model = requested_model or current_model
                if desired_model in _model_manager.models:
                    desired_vision = (
                        _desired_runtime_vision_enabled(desired_model, has_image_inputs)
                        if path in ("chat/completions", "messages")
                        else False
                    )
                    current_vision = _model_manager.current_runtime_uses_mmproj(current_model)
                    needs_model_switch = desired_model != current_model
                    needs_runtime_reload = desired_model == current_model and desired_vision != current_vision

                    if needs_model_switch and not _model_manager.is_switch_allowed(client_id):
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
                            current_model = await _model_manager.get_current_model()
                            desired_model = requested_model or current_model
                            desired_vision = (
                                _desired_runtime_vision_enabled(desired_model, has_image_inputs)
                                if path in ("chat/completions", "messages")
                                else False
                            )
                            current_vision = _model_manager.current_runtime_uses_mmproj(current_model)
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
                                        (lambda: _model_manager.switch_model(
                                            desired_model,
                                            client_id=client_id,
                                            enable_vision=desired_vision,
                                        ))
                                        if needs_model_switch
                                        else (lambda: _model_manager.load(
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
                                        _model_manager.mark_vision_validation(desired_model, "load_failed", str(e))
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

        active_model_for_request = requested_model or await _model_manager.get_current_model()
        _set_request_usage_metadata(request, model=active_model_for_request)
        if path in ("chat/completions", "messages") and has_image_inputs:
            queue_wait_ms = _inference_queue.get_queue_wait_ms(request_id)
            preflight_error = await _preflight_multimodal_request(
                active_model_for_request,
                request_id,
                queue_wait_ms,
            )
            if preflight_error is not None:
                _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
                return preflight_error

        timeout_sec = float(_get_model_timeout(active_model_for_request))
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
                f"{_llama_server_url}/v1/{path}",
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
                    f"{_llama_server_url}/v1/{path}",
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
                            queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
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
                            queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
                            duration_ms=(time.monotonic() - request_start_time) * 1000,
                        )
                    await client.aclose()
                    raise HTTPException(status_code=502, detail=f"Backend request failed after reload: {retry_error}")
            except _GuardianRequestCancelled as exc:
                # ── Capture: request_cancelled (streaming, pre-stream outer) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
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
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
                        duration_ms=(time.monotonic() - request_start_time) * 1000,
                    )
                await client.aclose()
                raise HTTPException(status_code=502, detail=f"Backend request failed: {e}")

            if has_image_inputs:
                queue_wait_ms = _inference_queue.get_queue_wait_ms(request_id)
                if 200 <= resp.status_code < 400:
                    _model_manager.mark_vision_validation(active_model_for_request, "supported")
                else:
                    body_bytes = await resp.aread()
                    headers = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "content-length")
                    }
                    await resp.aclose()
                    await client.aclose()
                    _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
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
                        heartbeat_interval_s=_stream_heartbeat_interval_s,
                        cancel_event=_inference_queue.get_cancel_event(request_id),
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
                        status_code=499 if _inference_queue.is_cancel_requested(request_id) else resp.status_code,
                    )
                    _model_manager.active_requests = max(0, _model_manager.active_requests - 1)

                    # ── Capture: request_completed or request_cancelled ──
                    _dispatch_capture_stream_completed(
                        request, request_id, client_id,
                        active_model_for_request, _capture_ctx,
                        _capture_policy_result, _local_capture_assembler,
                        usage_totals, path, resp.status_code,
                    )
                    capture_dispatched = True

                    _model_manager.last_request_time = time.time()
                    _inference_queue.finish(request_id, outcome=_request_outcome(request_id))
                    await _stop_background_task(disconnect_task)

            queue_wait_ms = _inference_queue.get_queue_wait_ms(request_id)
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
                            f"{_llama_server_url}/v1/{path}",
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
                                f"{_llama_server_url}/v1/{path}",
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
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
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
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - request_start_time) * 1000,
                    ) if _capture_ctx is not None else None
                    raise HTTPException(status_code=502, detail=f"Backend request failed: {e}")
                _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
                _model_manager.last_request_time = time.time()
                queue_wait_ms = _inference_queue.get_queue_wait_ms(request_id)
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
                        _model_manager.mark_vision_validation(active_model_for_request, "supported")
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
            _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
            _inference_queue.finish(request_id, outcome=_request_outcome(request_id))
