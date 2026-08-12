"""Model discovery endpoints — Ollama /api/tags, /v1/models, /api/show.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
The route decorators and thin wrappers stay in server.py; the handler logic
lives here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_model_manager = None
_provider_registry = None
_cloud_cred_store = None
_failover_registry = None
_get_request_auth_context = None
_parse_guardian_route = None
_CloudProvider = None
provider_base_url = None
resolve_cloud_attempts = None
build_model_metadata_entry = None
enrich_model_context_metadata = None
resolve_context_window = None
get_model_size = None


def init(
    *,
    _model_manager,
    _provider_registry,
    _cloud_cred_store,
    _failover_registry,
    _get_request_auth_context,
    _parse_guardian_route,
    _CloudProvider,
    _provider_base_url,
    _resolve_cloud_attempts,
    _build_model_metadata_entry,
    _enrich_model_context_metadata,
    _resolve_context_window,
    _get_model_size,
) -> None:
    """Inject all dependencies. Called once at startup."""
    globals()["_model_manager"] = _model_manager
    globals()["_provider_registry"] = _provider_registry
    globals()["_cloud_cred_store"] = _cloud_cred_store
    globals()["_failover_registry"] = _failover_registry
    globals()["_get_request_auth_context"] = _get_request_auth_context
    globals()["_parse_guardian_route"] = _parse_guardian_route
    globals()["_CloudProvider"] = _CloudProvider
    globals()["provider_base_url"] = _provider_base_url
    globals()["resolve_cloud_attempts"] = _resolve_cloud_attempts
    globals()["build_model_metadata_entry"] = _build_model_metadata_entry
    globals()["enrich_model_context_metadata"] = _enrich_model_context_metadata
    globals()["resolve_context_window"] = _resolve_context_window
    globals()["get_model_size"] = _get_model_size


async def tags_ollama() -> Dict[str, Any]:
    """Build the Ollama /api/tags model list (Phase 5: delegated)."""
    import traceback
    models = []
    try:
        # Get models from our manager config
        if not hasattr(_model_manager, 'models') or _model_manager.models is None:
            logger.error("_model_manager.models is missing or None")
            return {"models": []}
            
        for name in _model_manager.models.keys():
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


async def list_models(request: Request, client_id: str) -> Dict[str, Any]:
    """List available models from config and cloud providers (Phase 5: delegated)."""
    models_list = []
    try:
        for public_name, canonical_name in _model_manager.get_public_model_map().items():
            models_list.append(await build_model_metadata_entry(public_name, canonical_name, client_id))
    except Exception as e:
        logger.error(f"Failed to list models: {e}")

    # Append global cloud-provider models (OpenRouter, NVIDIA, …)
    try:
        for cloud_model in _provider_registry.get_all_cloud_models():
            entry = _provider_registry.build_model_metadata_entry(cloud_model)
            if entry is not None:
                models_list.append(await enrich_model_context_metadata(entry))
                provider = _provider_registry.get_provider_for_model(cloud_model)
                if provider is not None and provider.name == "openrouter":
                    alias_entry = dict(entry)
                    alias_entry["id"] = f"openrouter/{cloud_model}"
                    models_list.append(await enrich_model_context_metadata(alias_entry))
    except Exception as e:
        logger.error(f"Failed to list cloud models: {e}")

    # Append per-key cloud routes (guardian/{provider}/{model})
    try:
        auth_ctx = _get_request_auth_context(request) or {}
        key_fp = auth_ctx.get("key_fingerprint") or client_id
        for cloud_model in _cloud_cred_store.get_linked_models_for_key(key_fp):
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
            credential = _cloud_cred_store.get_credential_for_key(key_fp, cloud_model["provider"])
            cloud_attempts = None
            if credential is not None:
                cloud_attempts = [
                    (
                        _CloudProvider(
                            name=cloud_model["provider"],
                            base_url=provider_base_url(cloud_model["provider"]),
                            api_key=credential.api_key,
                            models=[cloud_model["model"]],
                        ),
                        cloud_model["model"],
                    )
                ]
            models_list.append(await enrich_model_context_metadata(entry, cloud_attempts=cloud_attempts))
    except Exception as e:
        logger.error(f"Failed to list per-key cloud models: {e}")

    # Append failover groups as synthetic model entries (guardian/failover/{group}).
    # A failover group spans multiple providers; surface it so discovery clients
    # (Goose, Open WebUI, etc.) can offer cross-provider failover routes without
    # the caller needing to know the underlying (provider, model) candidates.
    try:
        for group_name in _failover_registry._groups.keys():
            try:
                cloud_attempts, _ = resolve_cloud_attempts(
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
                await enrich_model_context_metadata(entry, cloud_attempts=cloud_attempts)
            )
    except Exception as e:
        logger.error(f"Failed to list failover groups: {e}")

    return {"object": "list", "data": models_list}


async def model_metadata(model_id: str, request: Request, client_id: str) -> Dict[str, Any]:
    """Return metadata for a configured canonical model, public alias, or cloud model (Phase 5: delegated)."""
    # Failover groups surface as guardian/failover/{group}; resolve them here so
    # /v1/models/<id> returns a stable shape rather than 404'ing on the discovery
    # entry the list endpoint just advertised.
    if model_id.startswith("guardian/failover/"):
        group_name = model_id[len("guardian/failover/"):]
        if _failover_registry.get_group(group_name) is not None:
            cloud_attempts, _ = resolve_cloud_attempts(model_id, request, client_id)
            return await enrich_model_context_metadata({
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
    if _provider_registry.is_cloud_model(model_id):
        entry = _provider_registry.build_model_metadata_entry(model_id)
        if entry is not None:
            return await enrich_model_context_metadata(entry)

    guardian_route = _parse_guardian_route(model_id)
    if guardian_route is not None:
        provider_name, _ = guardian_route
        cloud_attempts, _ = resolve_cloud_attempts(model_id, request, client_id)
        return await enrich_model_context_metadata({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": provider_name,
            "permission": [],
            "served_by": "cloud",
            "provider": provider_name,
        }, cloud_attempts=cloud_attempts)

    public_models = _model_manager.get_public_model_map()
    canonical_name = public_models.get(model_id)
    if canonical_name is None:
        try:
            canonical_name = _model_manager.resolve_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await build_model_metadata_entry(model_id, canonical_name, client_id)


async def show_model(request: Request, client_id: str) -> Dict[str, Any]:
    """Return Ollama-compatible metadata with an always-present context size (Phase 5: delegated)."""
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
    cloud_attempts: Optional[List[Tuple[_CloudProvider, str]]] = None
    guardian_route = _parse_guardian_route(model_name)
    if model_name.startswith("guardian/failover/"):
        group_name = model_name[len("guardian/failover/"):]
        if _failover_registry.get_group(group_name) is None:
            raise HTTPException(status_code=404, detail=f"Failover group '{group_name}' not found")
        cloud_attempts, _ = resolve_cloud_attempts(model_name, request, client_id)
    elif guardian_route is not None:
        cloud_attempts, _ = resolve_cloud_attempts(model_name, request, client_id)
    elif not _provider_registry.is_cloud_model(model_name):
        try:
            canonical_name = _model_manager.resolve_model(model_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    context_window = await resolve_context_window(
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


