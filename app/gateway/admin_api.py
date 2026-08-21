"""Admin API — keys, cloud credentials, status, capture, scaler, queue.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
The route decorators and thin wrappers stay in server.py; the handler logic
lives here.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

import httpx
from fastapi import HTTPException, Request

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_model_manager = None
_provider_registry = None
_cloud_catalog = None
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
_load_api_keys = None
_generate_api_key = None
_token_fingerprint = None
_model_switch_lock = None
_reset_startup_check_status = None
_run_guardian_operation = None

# Config-reload plumbing (injected at startup; see reload_runtime_config)
_reload_settings_config = None  # callable() -> reloaded settings dict
_failover_registry = None
_failover_health = None


def init(
    *,
    _model_manager,
    _provider_registry,
    _cloud_catalog,
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
    _load_api_keys,
    _generate_api_key,
    _token_fingerprint,
    _model_switch_lock,
    _reset_startup_check_status,
    _run_guardian_operation,
    _reload_settings_config=None,
    _failover_registry=None,
    _failover_health=None,
) -> None:
    """Inject all dependencies. Called once at startup."""
    globals().update({k: v for k, v in locals().items() if k != "_init"})


async def list_api_keys(client_id: str) -> Any:
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



async def list_cloud_catalog(client_id: str) -> Any:
    """Return the current dynamic cloud catalog state (per provider).

    Cloud-access redesign (2026-08-21): the catalog replaces the removed
    per-key credential/link model listings. Per provider it reports the
    model count, the full ``{provider}/{brand}/{model}`` addresses, and the
    last successful fetch time (``None`` when not yet fetched).
    """
    providers = []
    for p in _provider_registry.get_enabled_providers():
        data = _cloud_catalog._catalogs.get(p.name)
        fetched_at = None
        if isinstance(data, dict) and data.get("fetched_at"):
            fetched_at = data["fetched_at"]
        catalog = _cloud_catalog.get_models_for_provider(p.name)
        providers.append({
            "name": p.name,
            "configured": p.is_configured,
            "model_count": len(catalog),
            "addresses": [f"{p.name}/{normalized}" for normalized in catalog],
            "last_fetch": fetched_at,
        })
    return {"catalog": providers}


async def refresh_cloud_catalog(client_id: str) -> Any:
    """Force a background-fresh fetch of every configured provider's catalog."""
    await _cloud_catalog.refresh_all()
    providers = []
    for p in _provider_registry.get_enabled_providers():
        data = _cloud_catalog._catalogs.get(p.name)
        fetched_at = None
        if isinstance(data, dict) and data.get("fetched_at"):
            fetched_at = data["fetched_at"]
        catalog = _cloud_catalog.get_models_for_provider(p.name)
        providers.append({
            "name": p.name,
            "configured": p.is_configured,
            "model_count": len(catalog),
            "last_fetch": fetched_at,
        })
    return {"status": "refreshed", "catalog": providers}


async def get_cloud_ratelimit_stats(request: Request, client_id: str) -> Any:
    key_fingerprint = _get_cloud_key_fingerprint(request, client_id)
    return _cloud_rate_limiter.get_stats(key_fingerprint)


async def list_cloud_providers(client_id: str) -> Any:
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
    """List provider-global cloud models from the dynamic catalog.

    Cloud-access redesign (2026-08-21): the per-key linked-route listing is
    gone; every enabled + configured provider's catalog is listed.
    """
    models = []
    for p in _provider_registry.get_enabled_providers():
        if not p.is_configured:
            continue
        catalog = _cloud_catalog.get_models_for_provider(p.name)
        if not catalog:
            catalog = {m: m for m in p.models}
        for normalized in catalog:
            full_id = f"{p.name}/{normalized}"
            entry = _provider_registry.build_model_metadata_entry(full_id)
            if entry is None:
                entry = {
                    "id": full_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": p.name,
                    "permission": [],
                    "served_by": "cloud",
                    "provider": p.name,
                }
            models.append(entry)
    return {"models": models}



async def get_crash_history(client_id: str) -> Any:
    return {
        "total_crashes": len(_model_manager.crash_history),
        "last_crash": _model_manager.last_crash.to_dict() if _model_manager.last_crash else None,
        "history": _model_manager.get_crash_history(),
    }



async def get_server_status(client_id: str) -> Any:
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


async def reload_config(client_id: str) -> Any:
    """Re-read settings.yaml live WITHOUT restarting.

    Reloads every subsystem that can safely swap config at runtime, in
    dependency order:

    1. settings.yaml: re-parsed into the shared ``CONFIG`` dict (config_loader
       keeps the same dict object, so all existing accessors see the update).
    2. ``ProviderRegistry.reload()`` — provider lists / prefixes / models.
    3. ``FailoverRegistry.reload()`` — ``failover_groups`` (settings.yaml or
       legacy cloud_keys.json).
    4. ``CloudModelCatalog.reload()`` — re-reads cloud_models.yaml overrides.
    5. ``CaptureController.reload_config()`` — capture config, incl.
       cloud_capture / cloud_model_prefixes; WAL writer re-initialised when
       capture becomes active or sink bounds change.
    6. Failover health thresholds + cloud_retry limits — retuned in place.

    Anything that cannot change at runtime (listen port, pid file, TLS, ...
    stays as-is and is reported in the ``not_reloaded`` list.  Fail-safe:
    a YAML/JSON parse error anywhere leaves the previous state intact.
    """
    from app.proxy.ratelimit import RateLimitConfig

    reloaded: list[str] = []
    not_reloaded: list[str] = []
    errors: list[str] = []

    # 1. config files (global.settings.yaml + providers.*) → shared CONFIG
    #    (atomic in-place swap).  The telemetry label keeps the legacy name so
    #    dashboards/scripts that grep for it keep working.
    try:
        new_config = _reload_settings_config() if _reload_settings_config else None
        reloaded.append("settings.yaml (CONFIG)")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"settings.yaml: {exc}")
        not_reloaded.append("settings.yaml")

    # 2. Provider registry (reads providers.settings.yaml + overrides itself)
    try:
        _provider_registry.reload()
        reloaded.append("providers")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"providers: {exc}")
        not_reloaded.append("providers")

    # 3. Failover groups (settings.yaml or legacy cloud_keys.json)
    try:
        _failover_registry.reload()
        reloaded.append("failover_groups")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"failover_groups: {exc}")
        not_reloaded.append("failover_groups")

    # 4. Cloud model catalog (re-reads cloud_models.yaml overrides; keeps
    #    fetched per-provider lists, dropping providers no longer configured).
    try:
        _cloud_catalog.reload()
        reloaded.append("cloud_catalog")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"cloud_catalog: {exc}")
        not_reloaded.append("cloud_catalog")

    # 5. Capture controller + WAL writer (settings.yaml capture block)
    try:
        controller = _get_capture_controller()
        await controller.reload_config()
        reloaded.append("capture (cloud_capture, prefixes, policies)")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"capture: {exc}")
        not_reloaded.append("capture")

    # 6a/6b: failover health + cloud_retry apply only when settings.yaml
    # actually reloaded — otherwise their previous values stay untouched
    # (avoids defaulting cloud_retry.enabled back to true on a failed parse).
    if new_config is None:
        not_reloaded.append("failover_health")
        not_reloaded.append("cloud_retry")
    else:
        try:
            fh_cfg = new_config.get("failover_health", {}) or {}
            _failover_health.reconfigure(
                failure_threshold=fh_cfg.get("failure_threshold"),
                cooldown_seconds=fh_cfg.get("cooldown_seconds"),
                rate_limit_cooldown_seconds=fh_cfg.get("rate_limit_cooldown_seconds"),
            )
            reloaded.append("failover_health")
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"failover_health: {exc}")
            not_reloaded.append("failover_health")

        try:
            _cloud_rate_limiter.config = RateLimitConfig.from_mapping(
                new_config.get("cloud_retry", {})
            )
            reloaded.append("cloud_retry")
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"cloud_retry: {exc}")
            not_reloaded.append("cloud_retry")

    logger.info(
        "🔄 Config reload by admin '%s': reloaded=%s errors=%s",
        client_id, reloaded, errors or "none",
    )
    finalized_not_reloaded = sorted(set(not_reloaded))
    return {
        "status": "ok" if not errors and not finalized_not_reloaded else "partial",
        "reloaded": reloaded,
        "not_reloaded": finalized_not_reloaded,
        "errors": errors,
        "note": (
            "Runtime-only value (port/pid/TLS) can NOT be reloaded — "
            "change those in settings.yaml and restart."
        ),
    }


# --- Capture status endpoint (admin) ---


async def get_capture_status(client_id: str) -> Any:
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



async def rotate_capture_file(client_id: str) -> Any:
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



async def get_scaler_config(client_id: str) -> Any:
    return _state.scaler.get_config()



async def update_scaler_config(request: Request, client_id: str) -> Any:
    patch = await request.json()
    persist = patch.pop("_persist", True)
    updated = _state.scaler.update_config(patch, persist=persist)
    return {"status": "updated", "config": updated}



async def reset_scaler_config(client_id: str) -> Any:
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

async def admin_load(request: Request, client_id: str) -> Any:
    """Reload llama-server. Optionally pass {"model": "name"} to load a specific model."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    target = body.get("model", None)
    if target:
        try:
            target = _model_manager.resolve_model(target)
        except ValueError:
            pass
    enable_vision = body.get("enable_vision")
    runtime_overrides = body.get("runtime_overrides")
    if runtime_overrides is not None and not isinstance(runtime_overrides, dict):
        raise HTTPException(status_code=400, detail="runtime_overrides must be an object")
    generation = _reset_startup_check_status(
        source="admin",
        phase="manual_load",
        target_model=target or _model_manager.current_model,
        requested_model=body.get("model"),
        owner=client_id,
    )
    _model_manager.last_request_time = time.time()
    _model_manager.active_requests += 1
    try:
        async with _model_switch_lock:
            await _run_guardian_operation(
                source="admin",
                phase="manual_load",
                target_model=target or _model_manager.current_model,
                requested_model=body.get("model"),
                owner=client_id,
                operation=lambda: _model_manager.load(
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
        _model_manager.active_requests = max(0, _model_manager.active_requests - 1)
        _model_manager.last_request_time = time.time()
    return {"status": "loaded", "model": _model_manager.current_model}

