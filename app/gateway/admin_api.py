"""Admin API — keys, cloud credentials, status, capture, scaler, queue.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
The route decorators and thin wrappers stay in server.py; the handler logic
lives here.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, Request

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_model_manager = None
_provider_registry = None
_cloud_cred_store = None
_cloud_rate_limiter = None
_inference_queue = None
_state = None
_llama_server_url = None
_proxy_port = None
_PROVIDER_BASE_URLS: Dict[str, str] = {}
_get_cloud_key_fingerprint = None
_get_request_auth_context = None
_get_queue_owner_id = None
_get_startup_check_status = None
_startup_state_is_in_progress = None
_get_proxy_listener_info = None
_get_pid_file_status = None
_get_capture_controller = None
_get_gpu_metrics = None
_get_model_size = None
_discover_google_models = None
_load_api_keys = None
_generate_api_key = None
_token_fingerprint = None
_model_switch_lock = None


def init(
    *,
    _model_manager,
    _provider_registry,
    _cloud_cred_store,
    _cloud_rate_limiter,
    _inference_queue,
    _state,
    _llama_server_url,
    _proxy_port,
    _PROVIDER_BASE_URLS,
    _get_cloud_key_fingerprint,
    _get_request_auth_context,
    _get_queue_owner_id,
    _get_startup_check_status,
    _startup_state_is_in_progress,
    _get_proxy_listener_info,
    _get_pid_file_status,
    _get_capture_controller,
    _get_gpu_metrics,
    _get_model_size,
    _discover_google_models,
    _load_api_keys,
    _generate_api_key,
    _token_fingerprint,
    _model_switch_lock,
) -> None:
    """Inject all dependencies. Called once at startup."""
    globals().update({k: v for k, v in locals().items() if k != "_init"})


async def list_api_keys(request: Request, client_id: str) -> Any:
    keys = _load_api_keys()
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



async def create_api_key(request: Request, client_id: str) -> Any:
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    prefix = body.get("prefix")
    metadata = body.get("metadata")
    api_key = _generate_api_key(name, metadata=metadata, prefix=prefix)
    logger.info("🔑 Admin '%s' generated new API key for '%s'", client_id, name)
    return {
        "api_key": api_key,
        "key_fingerprint": _token_fingerprint(api_key),
        "name": name,
        "message": "Store this key securely — it will not be shown again.",
    }



async def list_cloud_credentials(request: Request, client_id: str) -> Any:
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return {"credentials": _cloud_cred_store.list_credentials_for_owner(owner_key_fingerprint)}



async def add_cloud_credential(request: Request, client_id: str) -> Any:
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
    cred = await _cloud_cred_store.add_credential(
        provider=provider,
        name=name,
        api_key=api_key,
        models=[str(m) for m in models if m],
        owner_key_fingerprint=owner_key_fingerprint,
    )
    logger.info("☁️  Admin '%s' added cloud credential '%s' for provider '%s'", client_id, cred["id"], provider)
    return cred



async def refresh_cloud_credential_models(cred_id: str, request: Request, client_id: str) -> Any:
    credential = _cloud_cred_store.get_credential_by_id(cred_id)
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if credential is None or not _cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    if credential.provider != "google":
        raise HTTPException(
            status_code=400,
            detail="Automatic model refresh is currently supported only for Google credentials",
        )

    models = await _discover_google_models(credential.api_key)
    replaced = await _cloud_cred_store.replace_models_for_credential(cred_id, models)
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



async def delete_cloud_credential(request: Request, client_id: str) -> Any:
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not _cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    deleted = await _cloud_cred_store.delete_credential(cred_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    logger.info("☁️  Admin '%s' deleted cloud credential '%s'", client_id, cred_id)
    return {"status": "deleted", "credential_id": cred_id}



async def add_model_to_credential(request: Request, client_id: str) -> Any:
    body = await request.json()
    model_name = str(body.get("model", "")).strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not _cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found")
    added = await _cloud_cred_store.add_model_to_credential(cred_id, model_name)
    if not added:
        raise HTTPException(status_code=404, detail=f"Credential '{cred_id}' not found or model already present")
    return {"status": "added", "credential_id": cred_id, "model": model_name}



async def remove_model_from_credential(request: Request, client_id: str) -> Any:
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if not _cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail="Credential or model not found")
    removed = await _cloud_cred_store.remove_model_from_credential(cred_id, model_name)
    if not removed:
        raise HTTPException(status_code=404, detail="Credential or model not found")
    return {"status": "removed", "credential_id": cred_id, "model": model_name}



async def list_cloud_links(request: Request, client_id: str) -> Any:
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return {"links": _cloud_cred_store.list_links_for_owner(owner_key_fingerprint)}



async def get_cloud_ratelimit_stats(request: Request, client_id: str) -> Any:
    key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return _cloud_rate_limiter.get_stats(key_fingerprint)



async def link_credential(request: Request, client_id: str) -> Any:
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
    if not _cloud_cred_store.is_credential_owned_by(cred_id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail="Credential not found")
    linked = await _cloud_cred_store.link_credential(key_fp, provider, cred_id)
    if not linked:
        raise HTTPException(status_code=404, detail="Credential not found")
    logger.info("☁️  Admin '%s' linked credential '%s' to key '%s' for provider '%s'", client_id, cred_id, key_fp, provider)
    return {"status": "linked", "guardian_key_fingerprint": key_fp, "provider": provider, "credential_id": cred_id}



async def unlink_credential(request: Request, client_id: str) -> Any:
    body = await request.json()
    key_fp = str(body.get("guardian_key_fingerprint", "")).strip()
    provider = str(body.get("provider", "")).strip().lower()
    if not key_fp:
        raise HTTPException(status_code=400, detail="guardian_key_fingerprint is required")
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    credential = _cloud_cred_store.get_credential_for_key(key_fp, provider)
    owner_key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    if credential is None or not _cloud_cred_store.is_credential_owned_by(credential.id, owner_key_fingerprint):
        raise HTTPException(status_code=404, detail="Link not found")
    unlinked = await _cloud_cred_store.unlink_credential(key_fp, provider)
    if not unlinked:
        raise HTTPException(status_code=404, detail="Link not found")
    return {"status": "unlinked", "guardian_key_fingerprint": key_fp, "provider": provider}



async def list_cloud_providers(request: Request, client_id: str) -> Any:
    providers = []
    for p in _provider_registry.get_enabled_providers():
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



async def list_cloud_models(request: Request, client_id: str) -> Any:
    models = []
    # Global cloud models
    for model_name in _provider_registry.get_all_cloud_models():
        entry = _provider_registry.build_model_metadata_entry(model_name)
        if entry:
            models.append(entry)
    # Per-key cloud routes
    auth_ctx = _get_request_auth_context(request) or {}
    key_fp = auth_ctx.get("key_fingerprint") or client_id
    for cloud_model in _cloud_cred_store.get_linked_models_for_key(key_fp):
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



async def get_crash_history(request: Request, client_id: str) -> Any:
    return {
        "total_crashes": len(_model_manager.crash_history),
        "last_crash": _model_manager.last_crash.to_dict() if _model_manager.last_crash else None,
        "history": _model_manager.get_crash_history(),
    }



async def get_server_status(request: Request, client_id: str) -> Any:
    current_model = await _model_manager.get_current_model()
    startup_status = _get_startup_check_status()
    queue_status = _inference_queue.get_status()
    switch_in_progress = _startup_state_is_in_progress(startup_status.get("_state")) and startup_status.get("phase") != "idle"
    current_requested_target = startup_status.get("target_model") if switch_in_progress else None
    active_switch_owner = startup_status.get("owner") if switch_in_progress else None
    healthy = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_llama_server_url}/health")
            healthy = resp.status_code == 200
    except Exception:
        pass

    preferred_tool_model = _model_manager.get_preferred_tool_model(current_model)
    preferred_reasoning_model = _model_manager.get_preferred_reasoning_model(current_model)
    backend_model_path = _model_manager._get_backend_model_path()
    backend_model_name = _model_manager._last_backend_model
    if backend_model_name is None and backend_model_path:
        backend_model_name = _model_manager._identify_model_by_path(backend_model_path)
    vram = _get_gpu_metrics()
    idle_minutes = _model_manager.idle_unload_minutes
    idle_secs = time.time() - _model_manager.last_request_time
    return {
        "current_model": current_model,
        "backend_healthy": healthy,
        "is_unloaded": _model_manager.is_unloaded,
        "idle_seconds": round(idle_secs),
        "idle_unload_minutes": idle_minutes,
        "backend_url": _llama_server_url,
        "total_crashes": len(_model_manager.crash_history),
        "last_crash": _model_manager.last_crash.to_dict() if _model_manager.last_crash else None,
        "vram": vram,
        "vram_model_mb": _get_model_size(current_model),
        "security": {
            "pinned_model": _model_manager.pinned_model,
            "switch_allowlist": list(_model_manager._switch_allowlist) if _model_manager._switch_allowlist else None,
            "backend_verified": _model_manager._model_verified,
            "last_backend_verification_at": _model_manager._last_verification_at,
            "last_successful_backend_verification_at": _model_manager._last_successful_verification_at,
            "last_verified_model": _model_manager._last_verified_model,
            "backend_model": backend_model_name,
            "backend_model_path": backend_model_path,
        },
        "startup": startup_status,
        "current_requested_target": current_requested_target,
        "switch": {
            "active": switch_in_progress,
            "_state": startup_status.get("_state"),
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
            "port": _proxy_port,
            "listener": _get_proxy_listener_info(),
            "pid_file": _get_pid_file_status(),
        },
        "scaler": {
            "enabled": _state.scaler.config.get("enabled", False),
            "profiles": list(_state.scaler.config.get("profiles", {}).keys()),
        },
    }


# --- Capture status endpoint (admin) ---


async def get_capture_status(request: Request, client_id: str) -> Any:
    controller = _get_capture_controller()
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



async def rotate_capture_file(request: Request, client_id: str) -> Any:
    controller = _get_capture_controller()
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



async def get_scaler_config(request: Request, client_id: str) -> Any:
    return _state.scaler.get_config()



async def update_scaler_config(request: Request, client_id: str) -> Any:
    patch = await request.json()
    persist = patch.pop("_persist", True)
    updated = _state.scaler.update_config(patch, persist=persist)
    return {"status": "updated", "config": updated}



async def reset_scaler_config(request: Request, client_id: str) -> Any:
    config = _state.scaler.reset_config()
    return {"status": "reset", "config": config}



async def scaler_recommend(request: Request, client_id: str) -> Any:
    body = await request.json()
    messages = body.get("messages", [])

    # Classify complexity
    profile_name, complexity = _state.scaler._classify_complexity(messages)
    profile = _state.scaler.config["profiles"].get(profile_name, {})

    base_thinking = profile.get("thinking_budget", -1)
    base_max_tokens = profile.get("max_tokens", 8192)

    # Apply queue pressure
    thinking_budget, max_tokens = _state.scaler._apply_queue_pressure(
        base_thinking, base_max_tokens, _inference_queue.waiting_count
    )
    pressure = _state.scaler._pressure_label(_inference_queue.waiting_count)

    if _state.scaler.config.get("log_decisions"):
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



async def queue_status(request: Request, client_id: str) -> Any:
    return _inference_queue.get_status(
        client_id=client_id,
        owner_id=_get_queue_owner_id(request, client_id),
    )



async def queue_request_status(request_id: str, request: Request, client_id: str) -> Any:
    snapshot = _inference_queue.get_request_status(
        request_id,
        client_id=client_id,
        owner_id=_get_queue_owner_id(request, client_id),
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Queue request not found")
    return snapshot



async def cancel_queue_request(request_id: str, request: Request, client_id: str) -> Any:
    snapshot = _inference_queue.cancel(
        request_id,
        client_id=client_id,
        owner_id=_get_queue_owner_id(request, client_id),
        reason="client_requested_cancel",
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Queue request not found")
    return snapshot



