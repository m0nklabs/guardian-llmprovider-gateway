"""Ollama-protocol local inference — /api/chat and /api/generate bridges.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Bridges Ollama-style requests to the local llama-server OpenAI endpoint,
handling queue admission, model auto-reload/switch, streaming translation,
usage tracking, and capture dispatch. All external dependencies are injected
once at startup via :func:`init`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from app.engine.manager import ModelLoadError
from app.capture.config import PROTOCOL_OLLAMA, ROUTE_LOCAL
from app.capture.schema import BuildContext
from app.capture.stream_assembler import StreamResponseAssembler
from app.gateway.streaming import StreamProgressWatchdog

logger = logging.getLogger("Guardian")

# ── Injected (set once at startup by init()) ─────────────────────────
_resolve_or_reject_inference_model = None
_is_cloud_or_guardian_route = None
_forward_to_cloud_provider = None
_begin_queued_request = None
_request_cancel_http_exception = None
_capture_client_fingerprint = None
_dispatch_capture_request_received = None
_resolve_auto_reload_model = None
_reset_startup_check_status = None
_run_guardian_operation = None
_set_request_usage_metadata = None
_build_stream_timeout = None
_await_or_cancel_request = None
_close_on_request_cancel = None
_close_stream_resources = None
_iter_sse_lines_with_watchdog = None
_coerce_usage_int = None
_extract_assistant_delta_text = None
_update_live_request_usage = None
_record_request_token_usage = None
_finish_live_request_usage = None
_dispatch_capture_stream_completed = None
_request_outcome = None
_stop_background_task = None
_extract_assistant_message_text = None
_record_usage_from_payload = None
_dispatch_capture_request_cancelled = None
_dispatch_capture_request_failed = None
_classify_capture_error = None
_sanitize_capture_error_message = None
_dispatch_capture_nonstream_completed = None
_get_model_timeout = None
_GuardianRequestCancelled = None
_model_switch_lock = None
_llama_server_url = None
_model_manager = None
_inference_queue = None
_capture_controller = None


def init(
    *,
    resolve_or_reject_inference_model,
    is_cloud_or_guardian_route,
    forward_to_cloud_provider,
    begin_queued_request,
    request_cancel_http_exception,
    capture_client_fingerprint,
    dispatch_capture_request_received,
    resolve_auto_reload_model,
    reset_startup_check_status,
    run_guardian_operation,
    set_request_usage_metadata,
    build_stream_timeout,
    await_or_cancel_request,
    close_on_request_cancel,
    close_stream_resources,
    iter_sse_lines_with_watchdog,
    coerce_usage_int,
    extract_assistant_delta_text,
    update_live_request_usage,
    record_request_token_usage,
    finish_live_request_usage,
    dispatch_capture_stream_completed,
    request_outcome,
    stop_background_task,
    extract_assistant_message_text,
    record_usage_from_payload,
    dispatch_capture_request_cancelled,
    dispatch_capture_request_failed,
    classify_capture_error,
    sanitize_capture_error_message,
    dispatch_capture_nonstream_completed,
    get_model_timeout,
    guardian_request_cancelled,
    model_switch_lock,
    llama_server_url,
    model_manager,
    inference_queue,
    capture_controller,
) -> None:
    """Inject all dependencies. Called once at startup."""
    global _resolve_or_reject_inference_model, _is_cloud_or_guardian_route
    global _forward_to_cloud_provider, _begin_queued_request
    global _request_cancel_http_exception, _capture_client_fingerprint
    global _dispatch_capture_request_received, _resolve_auto_reload_model
    global _reset_startup_check_status, _run_guardian_operation
    global _set_request_usage_metadata, _build_stream_timeout
    global _await_or_cancel_request, _close_on_request_cancel
    global _close_stream_resources, _iter_sse_lines_with_watchdog
    global _coerce_usage_int, _extract_assistant_delta_text
    global _update_live_request_usage, _record_request_token_usage
    global _finish_live_request_usage, _dispatch_capture_stream_completed
    global _request_outcome, _stop_background_task
    global _extract_assistant_message_text, _record_usage_from_payload
    global _dispatch_capture_request_cancelled, _dispatch_capture_request_failed
    global _classify_capture_error, _sanitize_capture_error_message
    global _dispatch_capture_nonstream_completed, _get_model_timeout
    global _GuardianRequestCancelled, _model_switch_lock, _llama_server_url
    global _model_manager, _inference_queue, _capture_controller
    _resolve_or_reject_inference_model = resolve_or_reject_inference_model
    _is_cloud_or_guardian_route = is_cloud_or_guardian_route
    _forward_to_cloud_provider = forward_to_cloud_provider
    _begin_queued_request = begin_queued_request
    _request_cancel_http_exception = request_cancel_http_exception
    _capture_client_fingerprint = capture_client_fingerprint
    _dispatch_capture_request_received = dispatch_capture_request_received
    _resolve_auto_reload_model = resolve_auto_reload_model
    _reset_startup_check_status = reset_startup_check_status
    _run_guardian_operation = run_guardian_operation
    _set_request_usage_metadata = set_request_usage_metadata
    _build_stream_timeout = build_stream_timeout
    _await_or_cancel_request = await_or_cancel_request
    _close_on_request_cancel = close_on_request_cancel
    _close_stream_resources = close_stream_resources
    _iter_sse_lines_with_watchdog = iter_sse_lines_with_watchdog
    _coerce_usage_int = coerce_usage_int
    _extract_assistant_delta_text = extract_assistant_delta_text
    _update_live_request_usage = update_live_request_usage
    _record_request_token_usage = record_request_token_usage
    _finish_live_request_usage = finish_live_request_usage
    _dispatch_capture_stream_completed = dispatch_capture_stream_completed
    _request_outcome = request_outcome
    _stop_background_task = stop_background_task
    _extract_assistant_message_text = extract_assistant_message_text
    _record_usage_from_payload = record_usage_from_payload
    _dispatch_capture_request_cancelled = dispatch_capture_request_cancelled
    _dispatch_capture_request_failed = dispatch_capture_request_failed
    _classify_capture_error = classify_capture_error
    _sanitize_capture_error_message = sanitize_capture_error_message
    _dispatch_capture_nonstream_completed = dispatch_capture_nonstream_completed
    _get_model_timeout = get_model_timeout
    _GuardianRequestCancelled = guardian_request_cancelled
    _model_switch_lock = model_switch_lock
    _llama_server_url = llama_server_url
    _model_manager = model_manager
    _inference_queue = inference_queue
    _capture_controller = capture_controller


async def chat_ollama(request: Request, client_id: str):
    """Bridge Ollama-style chat requests to OpenAI-style Llama Server"""
    try:
        body = await request.json()
    except:
        body = {}
        
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    current_model = await _model_manager.get_current_model()
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
        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
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
            capture_policy_version=_capture_controller.config.policy_version
            if _capture_controller is not None else "1.0.0",
            instance_id=_capture_controller.config.instance_id
            if _capture_controller is not None else "unknown",
            client_fingerprint=_capture_client_fp,
        )

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if _model_manager.is_unloaded:
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
                if _model_manager.is_unloaded:
                    await _run_guardian_operation(
                        source="proxy",
                        phase="auto_reload",
                        target_model=reload_model,
                        requested_model=model,
                        owner=client_id,
                        operation=lambda: _model_manager.load(reload_model),
                        generation=generation,
                    )

        # Check if model switch needed (safe — we hold the queue slot)
        current_model = await _model_manager.get_current_model()
        if model != current_model and model in _model_manager.models:
            # SECURITY: Check client permission and pin
            if not _model_manager.is_switch_allowed(client_id):
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
                    current_model = await _model_manager.get_current_model()
                    if model != current_model:
                        try:
                            await _run_guardian_operation(
                                source="proxy",
                                phase="auto_switch",
                                target_model=model,
                                requested_model=body.get("model"),
                                owner=client_id,
                                operation=lambda: _model_manager.switch_model(model, client_id=client_id),
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

        _model_manager.last_request_time = time.time()
        _model_manager.active_requests += 1

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
            f"{_llama_server_url}/v1/chat/completions",
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
                        cancel_event=_inference_queue.get_cancel_event(request_id),
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
                    if not _inference_queue.is_cancel_requested(request_id):
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
                        status_code=499 if _inference_queue.is_cancel_requested(request_id) else r.status_code,
                    )
                    _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
                    _model_manager.last_request_time = time.time()
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
                    _inference_queue.finish(request_id, outcome=_request_outcome(request_id))
                    await _stop_background_task(disconnect_task)

            queue_wait_ms = _inference_queue.get_queue_wait_ms(request_id)
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
                _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
                _model_manager.last_request_time = time.time()
                return ollama_resp
            except _GuardianRequestCancelled as exc:
                # ── Capture: request_cancelled (Ollama non-streaming) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id) if request_id else None,
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
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - _ollama_request_start_time) * 1000,
                    )
                await r.aclose()
                await client.aclose()
                _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
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
            _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
            _inference_queue.finish(request_id, outcome=_request_outcome(request_id))

# Legacy endpoint for Ollama generate


async def generate_ollama(request: Request, client_id: str):
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

    current_model = await _model_manager.get_current_model()
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
        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
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
            capture_policy_version=_capture_controller.config.policy_version
            if _capture_controller is not None else "1.0.0",
            instance_id=_capture_controller.config.instance_id
            if _capture_controller is not None else "unknown",
            client_fingerprint=_capture_client_fp,
        )

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if _model_manager.is_unloaded:
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
                if _model_manager.is_unloaded:
                    await _run_guardian_operation(
                        source="proxy",
                        phase="auto_reload",
                        target_model=reload_model,
                        requested_model=model,
                        owner=client_id,
                        operation=lambda: _model_manager.load(reload_model),
                        generation=generation,
                    )

        # Model switch (safe — we hold the queue slot)
        current_model = await _model_manager.get_current_model()
        if model != current_model and model in _model_manager.models:
            if not _model_manager.is_switch_allowed(client_id):
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
                    current_model = await _model_manager.get_current_model()
                    if model != current_model:
                        try:
                            await _run_guardian_operation(
                                source="proxy",
                                phase="auto_switch",
                                target_model=model,
                                requested_model=body.get("model"),
                                owner=client_id,
                                operation=lambda: _model_manager.switch_model(model, client_id=client_id),
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

        _model_manager.last_request_time = time.time()
        _model_manager.active_requests += 1

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
            f"{_llama_server_url}/v1/chat/completions",
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
                        cancel_event=_inference_queue.get_cancel_event(request_id),
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
                    if not _inference_queue.is_cancel_requested(request_id):
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
                        status_code=499 if _inference_queue.is_cancel_requested(request_id) else r.status_code,
                    )
                    _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
                    _model_manager.last_request_time = time.time()
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
                    _inference_queue.finish(request_id, outcome=_request_outcome(request_id))
                    await _stop_background_task(disconnect_task)

            queue_wait_ms = _inference_queue.get_queue_wait_ms(request_id)
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
                _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
                _model_manager.last_request_time = time.time()
                return ollama_resp
            except _GuardianRequestCancelled as exc:
                # ── Capture: request_cancelled (Ollama non-streaming) ──
                if _capture_ctx is not None:
                    _dispatch_capture_request_cancelled(
                        _capture_ctx, cancel_reason=exc.reason,
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id) if request_id else None,
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
                        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id) if request_id else None,
                        duration_ms=(time.monotonic() - _generate_request_start_time) * 1000,
                    )
                await r.aclose()
                await client.aclose()
                _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
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
            _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
            _inference_queue.finish(request_id, outcome=_request_outcome(request_id))


