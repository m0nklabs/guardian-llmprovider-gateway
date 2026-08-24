"""Model discovery endpoints — Ollama /api/tags, /v1/models, /api/show.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
The route decorators and thin wrappers stay in server.py; the handler logic
lives here.

Since the cloud-access redesign (2026-08-21) the cloud section of
``/v1/models`` is provider-global (not per-key): every enabled + configured
provider contributes its dynamic ``CloudModelCatalog`` entries as
``{provider}/{brand}/{model}`` addresses.  A Guardian key with
``cloud_gateway_access=false`` sees no cloud entries.
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
_cloud_catalog = None
_failover_registry = None
_get_request_auth_context = None
resolve_cloud_attempts = None
build_model_metadata_entry = None
enrich_model_context_metadata = None
resolve_context_window = None
get_model_size = None


def init(
    *,
    _model_manager,
    _provider_registry,
    _cloud_catalog,
    _failover_registry,
    _get_request_auth_context,
    _resolve_cloud_attempts,
    _build_model_metadata_entry,
    _enrich_model_context_metadata,
    _resolve_context_window,
    _get_model_size,
) -> None:
    """Inject all dependencies. Called once at startup."""
    globals()["_model_manager"] = _model_manager
    globals()["_provider_registry"] = _provider_registry
    globals()["_cloud_catalog"] = _cloud_catalog
    globals()["_failover_registry"] = _failover_registry
    globals()["_get_request_auth_context"] = _get_request_auth_context
    globals()["resolve_cloud_attempts"] = _resolve_cloud_attempts
    globals()["build_model_metadata_entry"] = _build_model_metadata_entry
    globals()["enrich_model_context_metadata"] = _enrich_model_context_metadata
    globals()["resolve_context_window"] = _resolve_context_window
    globals()["get_model_size"] = _get_model_size


def _is_failover_address(model_name: str) -> bool:
    """Return True when *model_name* is a ``failover/{group}`` address."""
    first, sep, _ = (model_name or "").partition("/")
    return bool(sep and first == "failover")


def _key_can_access_cloud(request: Request, client_id: str) -> bool:
    """Return whether the requesting key may see / use cloud entries."""
    auth_ctx = _get_request_auth_context(request) or {}
    return bool(auth_ctx.get("cloud_gateway_access", True))


def _cloud_entries_for_provider(provider_name: str) -> List[str]:
    """Return the full ``{provider}/{brand}/{model}`` addresses for a provider.

    Uses the dynamic catalog when available; falls back to the configured
    ``models:`` names (with their ``{provider}`` prefix) on cold start.
    """
    catalog = _cloud_catalog.get_models_for_provider(provider_name)
    if catalog:
        return [f"{provider_name}/{normalized}" for normalized in catalog]
    provider = _provider_registry._providers.get(provider_name)
    if provider is None:
        return []
    return [f"{provider_name}/{m}" for m in provider.models]


async def _build_cloud_entry(full_id: str, provider_name: str) -> Dict[str, Any]:
    """Build and context-enrich a single cloud model entry."""
    entry = _provider_registry.build_model_metadata_entry(full_id)
    if entry is None:
        entry = {
            "id": full_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": provider_name,
            "permission": [],
            "served_by": "cloud",
            "provider": provider_name,
        }
    entry = await enrich_model_context_metadata(entry)
    _attach_reasoning_metadata(entry, provider_name)
    return entry


def _attach_reasoning_metadata(model_entry: Dict[str, Any], provider_name: str) -> None:
    """Attach per-model reasoning-effort metadata when the catalog has it.

    Uses the ``{brand}/{model}`` remainder of the full ``{provider}/...``
    address as the catalog key and only annotates entries whose provider
    catalog actually advertised a ``reasoning`` block (OpenRouter-style).
    Local / failover entries never carry the field.
    """
    full_id = model_entry.get("id")
    if not isinstance(full_id, str):
        return
    _prefix = f"{provider_name}/"
    normalized = full_id[len(_prefix) :] if full_id.startswith(_prefix) else full_id
    reasoning = _cloud_catalog.get_model_reasoning(provider_name, normalized)
    if reasoning:
        model_entry["reasoning"] = reasoning


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

    # Append provider-global cloud models from the dynamic catalog
    # ({provider}/{brand}/{model}). Per-key gated on cloud_gateway_access.
    if _key_can_access_cloud(request, client_id):
        try:
            for provider_name in [
                p.name for p in _provider_registry.get_enabled_providers() if p.is_configured
            ]:
                for full_id in _cloud_entries_for_provider(provider_name):
                    models_list.append(await _build_cloud_entry(full_id, provider_name))
        except Exception as e:
            logger.error(f"Failed to list cloud models: {e}")

    # Append failover groups as synthetic model entries (failover/{group}).
    # A failover group spans multiple providers; surface it so discovery clients
    # (Goose, Open WebUI, etc.) can offer cross-provider failover routes without
    # the caller needing to know the underlying (provider, model) candidates.
    try:
        for group_name in _failover_registry._groups.keys():
            try:
                cloud_attempts, _ = resolve_cloud_attempts(
                    f"failover/{group_name}",
                    request,
                    client_id,
                )
            except HTTPException as exc:
                if exc.status_code == 403:
                    logger.debug("Skipping unauthorized failover group '%s' from discovery", group_name)
                    continue
                raise
            entry = {
                "id": f"failover/{group_name}",
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
    # Failover groups surface as failover/{group}; resolve them here so
    # /v1/models/<id> returns a stable shape rather than 404'ing on the discovery
    # entry the list endpoint just advertised.
    if _is_failover_address(model_id):
        group_name = model_id.partition("/")[2]
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

    # Cloud-provider models first (they may contain slashes like "openai/gpt-4o").
    if (
        _provider_registry.is_cloud_model(model_id)
        or _provider_registry._provider_from_address(model_id) is not None
    ):
        entry = _provider_registry.build_model_metadata_entry(model_id)
        if entry is not None:
            cloud_attempts = None
            try:
                cloud_attempts, _ = resolve_cloud_attempts(model_id, request, client_id)
            except HTTPException:
                cloud_attempts = None
            entry = await enrich_model_context_metadata(entry, cloud_attempts=cloud_attempts)
            provider_name = entry.get("provider") or model_id.partition("/")[0]
            _attach_reasoning_metadata(entry, provider_name)
            return entry

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
    cloud_attempts: Optional[List[Tuple[Any, str]]] = None
    if _is_failover_address(model_name):
        group_name = model_name.partition("/")[2]
        if _failover_registry.get_group(group_name) is None:
            raise HTTPException(status_code=404, detail=f"Failover group '{group_name}' not found")
        cloud_attempts, _ = resolve_cloud_attempts(model_name, request, client_id)
    elif (
        _provider_registry.is_cloud_model(model_name)
        or _provider_registry._provider_from_address(model_name) is not None
    ):
        cloud_attempts, _ = resolve_cloud_attempts(model_name, request, client_id)
    else:
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
