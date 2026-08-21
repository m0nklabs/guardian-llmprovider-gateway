"""Context window resolution and model metadata construction.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).

This module centralises all logic for resolving a model's context window —
whether it comes from a YAML override, the live llama.cpp ``/props`` endpoint,
the cloud provider catalog, or the safe fallback — and for building the
OpenAI-compatible model metadata entry that ``/v1/models`` and ``/api/show``
return.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.proxy.providers import CloudProvider

logger = logging.getLogger("Guardian")

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_CONTEXT_WINDOW = 131_072
LLAMA_SERVER_URL = None  # injected via init()
BACKEND_CONTEXT_CACHE_SECONDS = 5.0

# ── Module-level state ───────────────────────────────────────────────

_backend_context_cache: Dict[str, Tuple[float, int]] = {}
_backend_context_lock = asyncio.Lock()
_context_fallback_warnings: set[str] = set()

# ── Injected dependencies ────────────────────────────────────────────
# These are set by ``init()`` at startup, before any request is served.

_model_manager = None  # ModelManager instance
_provider_registry = None  # ProviderRegistry instance
_failover_registry = None  # FailoverRegistry instance
_cloud_catalog = None  # CloudModelCatalog instance


def init(model_manager, provider_registry, failover_registry, *, llama_server_url=None, cloud_catalog=None) -> None:
    """Inject the singleton dependencies.  Called once at startup."""
    global _model_manager, _provider_registry, _failover_registry, LLAMA_SERVER_URL, _cloud_catalog
    _model_manager = model_manager
    _provider_registry = provider_registry
    _failover_registry = failover_registry
    _cloud_catalog = cloud_catalog
    LLAMA_SERVER_URL = llama_server_url


# ── Public functions ─────────────────────────────────────────────────


def apply_context_metadata(model_entry: Dict[str, Any], context_window: int) -> Dict[str, Any]:
    """Add stable context aliases used by OpenAI, llama.cpp, and LiteLLM clients."""
    model_entry["context"] = context_window
    model_entry["context_length"] = context_window
    model_entry["max_input_tokens"] = context_window
    # Ensure max_context and benchmark_context_limit are always present so
    # every /v1/models entry — including cloud models — advertises a cap.
    if "max_context" not in model_entry:
        model_entry["max_context"] = context_window
    if "benchmark_context_limit" not in model_entry:
        model_entry["benchmark_context_limit"] = context_window
    metadata = model_entry.get("meta")
    if not isinstance(metadata, dict):
        metadata = {}
        model_entry["meta"] = metadata
    metadata["n_ctx"] = context_window
    return model_entry


async def get_loaded_backend_context_window(canonical_name: str) -> Optional[int]:
    """Read llama.cpp's actual configured context for the currently loaded model."""
    try:
        current_model = await _model_manager.get_current_model()
    except Exception:
        current_model = getattr(_model_manager, "current_model", None)
    if current_model != canonical_name:
        return None

    now = time.monotonic()
    cached = _backend_context_cache.get(canonical_name)
    if cached is not None and now - cached[0] < BACKEND_CONTEXT_CACHE_SECONDS:
        return cached[1]

    async with _backend_context_lock:
        now = time.monotonic()
        cached = _backend_context_cache.get(canonical_name)
        if cached is not None and now - cached[0] < BACKEND_CONTEXT_CACHE_SECONDS:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{LLAMA_SERVER_URL}/props")
            response.raise_for_status()
            payload = response.json()
            raw_context = payload.get("default_generation_settings", {}).get("n_ctx")
            if isinstance(raw_context, int) and not isinstance(raw_context, bool) and raw_context > 0:
                if await _model_manager.get_current_model() != canonical_name:
                    return None
                _backend_context_cache[canonical_name] = (now, raw_context)
                return raw_context
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            logger.debug("Unable to read llama.cpp context from /props: %s", exc)
    return None


async def resolve_context_window(
    public_name: str,
    canonical_name: Optional[str] = None,
    cloud_attempts: Optional[List[Tuple[CloudProvider, str]]] = None,
) -> int:
    """Resolve a positive context size for every locally served or cloud-routed model."""
    override = _provider_registry.get_context_override(public_name)
    if override is None and canonical_name is not None:
        override = _provider_registry.get_context_override(canonical_name)
    if override is not None:
        return override

    if canonical_name is not None:
        backend_context = await get_loaded_backend_context_window(canonical_name)
        if backend_context is not None:
            return backend_context
        configured_context = _model_manager.get_runtime_context_window(canonical_name)
        if configured_context is not None and configured_context > 0:
            return configured_context
    else:
        if cloud_attempts is not None:
            attempt_contexts: List[int] = []
            for provider, upstream_model in cloud_attempts:
                candidate_context = _cloud_context_override(upstream_model, provider.name)
                if candidate_context is None:
                    candidate_context = await _provider_registry.get_cloud_context_window(
                        f"{provider.name}/{upstream_model}",
                        provider=provider,
                    )
                if candidate_context is None:
                    warn_context_fallback(f"{provider.name}/{upstream_model}")
                    candidate_context = DEFAULT_CONTEXT_WINDOW
                attempt_contexts.append(candidate_context)
            if attempt_contexts:
                return min(attempt_contexts)

        if _is_failover_address(public_name):
            group = _failover_registry.get_group(public_name.partition("/")[2])
            if group is not None:
                candidate_contexts: List[int] = []
                for candidate in getattr(group, "candidates", ()):
                    candidate_context = _cloud_context_override(
                        candidate.model, candidate.provider
                    )
                    if candidate_context is None:
                        candidate_context = await _provider_registry.get_cloud_context_window(
                            f"{candidate.provider}/{candidate.model}"
                        )
                    if candidate_context is None:
                        warn_context_fallback(f"{candidate.provider}/{candidate.model}")
                        candidate_context = DEFAULT_CONTEXT_WINDOW
                    candidate_contexts.append(candidate_context)
                if candidate_contexts:
                    return min(candidate_contexts)
        cloud_context = await _provider_registry.get_cloud_context_window(public_name)
        if cloud_context is not None:
            return cloud_context

    warn_context_fallback(canonical_name or public_name)
    return DEFAULT_CONTEXT_WINDOW


def _is_failover_address(model_name: str) -> bool:
    """Return True when *model_name* is a ``failover/{group}`` address."""
    first, sep, _ = model_name.partition("/")
    return bool(sep and first == "failover")


def _cloud_context_override(upstream_model: str, provider_name: str) -> Optional[int]:
    """Return a cloud_models.yaml context-window override, or None.

    Checks the full ``{provider}/{brand}/{model}`` address first, then the
    namespaced ``{brand}/{model}`` / bare upstream id.
    """
    if _cloud_catalog is None:
        return None
    for key in (f"{provider_name}/{upstream_model}", upstream_model):
        override = _cloud_catalog.get_override(key)
        if not isinstance(override, dict):
            continue
        cw = override.get("context_window")
        if isinstance(cw, int) and not isinstance(cw, bool) and cw > 0:
            return cw
    return None


def warn_context_fallback(model_name: str) -> None:
    """Log each missing context source once while retaining a safe fallback."""
    if model_name in _context_fallback_warnings:
        return
    _context_fallback_warnings.add(model_name)
    logger.warning(
        "⚠️  Context size for model '%s' could not be resolved; using fallback %d",
        model_name,
        DEFAULT_CONTEXT_WINDOW,
    )


async def enrich_model_context_metadata(
    model_entry: Dict[str, Any],
    canonical_name: Optional[str] = None,
    cloud_attempts: Optional[List[Tuple[CloudProvider, str]]] = None,
) -> Dict[str, Any]:
    """Attach context metadata without changing the entry's existing shape."""
    context_window = await resolve_context_window(
        model_entry["id"],
        canonical_name,
        cloud_attempts,
    )
    return apply_context_metadata(model_entry, context_window)


async def build_model_metadata_entry(public_name: str, canonical_name: str, client_id: str) -> Dict[str, Any]:
    model_entry: Dict[str, Any] = {
        "id": public_name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "organization-owner",
        "permission": [],
    }
    benchmark_context_limit = _model_manager.get_benchmark_context_limit(canonical_name)
    runtime_context = _model_manager.get_runtime_context_window(canonical_name)
    advertised_context = _model_manager.get_advertised_context_window(canonical_name)
    if benchmark_context_limit is not None:
        model_entry["max_context"] = benchmark_context_limit
        model_entry["benchmark_context_limit"] = benchmark_context_limit
    if runtime_context is not None:
        model_entry["context"] = runtime_context
    if advertised_context is not None:
        model_entry["advertised_context"] = advertised_context

    vision = _model_manager.get_vision_capability(canonical_name)
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
    return await enrich_model_context_metadata(model_entry, canonical_name)
