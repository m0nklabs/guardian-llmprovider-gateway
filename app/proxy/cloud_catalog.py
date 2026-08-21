"""Dynamic cloud model catalog — fetched from each provider's /v1/models.

Part of the Cloud Access Redesign (2026-08-21): replaces the hand-maintained
per-key ``guardian/{provider}/{model}`` routes / linked-credential model lists
with a single, consistent ``{provider}/{brand}/{model}`` cloud model catalog
built from each configured provider's own OpenAI-compatible ``/v1/models``
endpoint.

For every *enabled and configured* provider this module:

- Fetches ``{base_url}/models`` using the provider's settings API key
  (``providers.<name>.api_key`` → ``$ENV``).
- Normalizes each upstream model id to ``{brand}/{model}`` so the
  ``{provider}/{brand}/{model}`` address is structurally identical across
  providers.  A bare upstream id (no ``/``) is prefixed with the provider's
  declared ``brand`` (default: the provider name), so google's ``gemini-…``
  becomes ``google/gemini-…`` and openai's ``gpt-4o`` becomes ``openai/gpt-4o``.
- Caches the result in memory with a TTL, and persists it to a runtime cache
  file (``data/cloud_catalog_cache.json``) so Guardian can serve a usable
  catalog at startup *before* the first fetch completes (cold-start fallback,
  reviewer #2) and keeps the last successful list on a failed refresh (like
  today's google fallback).

``config/cloud_models.yaml`` supplies per-model **overrides** (context window,
thinking capability, tool support, …) layered *above* the default template —
it is not a hand-maintained catalog, only exceptions from defaults.

This module is cheap to reconstruct and hot-reload aware: call
:meth:`CloudModelCatalog.reload` after a ``settings.yaml`` edit to pick up
provider/brand changes without a restart.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

from app.paths import CLOUD_CATALOG_CACHE_FILE, CLOUD_MODELS_OVERRIDES_FILE
from app.proxy.providers import CloudProvider, ProviderRegistry

logger = logging.getLogger("Guardian.CloudCatalog")

#: Default in-memory/persisted-cache TTL before a background refresh is allowed.
DEFAULT_TTL_SECONDS = 3600.0

#: Default ``{brand}`` used when a provider's upstream model ids are bare.
DEFAULT_BRAND_BY_PROVIDER: Dict[str, str] = {
    "openai": "openai",
    "google": "google",
    "nvidia": "nvidia",
}


class CloudModelCatalog:
    """Fetches, normalizes, and caches the cloud model catalog per provider."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        cache_file: Optional[Path] = None,
        overrides_file: Optional[Path] = None,
    ) -> None:
        self._registry = provider_registry
        self._ttl_seconds = float(ttl_seconds)
        self._cache_file = cache_file or CLOUD_CATALOG_CACHE_FILE
        self._overrides_file = overrides_file or CLOUD_MODELS_OVERRIDES_FILE

        # provider name -> {"fetched_at": float, "models": {normalized_id: upstream_id}}
        self._catalogs: Dict[str, Dict[str, Any]] = {}
        self._overrides: Dict[str, Dict[str, Any]] = {}

        self._load_overrides()
        self._load_disk_cache()
        self.reload()

    # ── Overrides / disk cache ────────────────────────────────────────

    def _load_overrides(self) -> None:
        try:
            if not self._overrides_file.exists():
                self._overrides = {}
                return
            raw = yaml.safe_load(self._overrides_file.read_text(encoding="utf-8")) or {}
            self._overrides = raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning("⚠️  Failed to load cloud_models.yaml overrides: %s", e)
            self._overrides = {}

    def _load_disk_cache(self) -> None:
        """Restore a previously persisted catalog for cold-start resilience."""
        try:
            if not self._cache_file.exists():
                return
            raw = json.loads(self._cache_file.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                return
            for provider in self._registry.get_enabled_providers():
                stored = raw.get(provider.name)
                if isinstance(stored, dict) and isinstance(stored.get("models"), dict):
                    self._catalogs[provider.name] = stored
            if self._catalogs:
                logger.info(
                    "☁️  Restored cold-start cloud catalog from %s (%d provider(s))",
                    self._cache_file,
                    len(self._catalogs),
                )
        except Exception as e:
            logger.debug("Cloud catalog disk cache not restored: %s", e)

    def _persist_cache(self) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                provider: {
                    "fetched_at": data["fetched_at"],
                    "models": data["models"],
                }
                for provider, data in self._catalogs.items()
                if isinstance(data, dict) and data.get("models")
            }
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning("⚠️  Failed to persist cloud catalog cache: %s", e)

    def reload(self) -> None:
        """Re-read overrides and align cached entries with current providers."""
        self._load_overrides()
        enabled_names = {p.name for p in self._registry.get_enabled_providers()}
        for stale in [p for p in self._catalogs if p not in enabled_names]:
            self._catalogs.pop(stale, None)

    # ── Brand normalization ───────────────────────────────────────────

    def _default_brand(self, provider: CloudProvider) -> str:
        return DEFAULT_BRAND_BY_PROVIDER.get(provider.name, provider.name)

    @staticmethod
    def _normalize_upstream_id(raw_id: str, brand: str) -> str:
        """Return ``{brand}/{model}`` for an upstream model id.

        A bare id (no ``/``) gets the *brand* prefix; a namespaced id is kept
        as-is so an already-branded upstream id (e.g. nvidia's
        ``minimaxai/minimax-m3``) is preserved.
        """
        raw_id = (raw_id or "").strip()
        if not raw_id:
            return ""
        if "/" in raw_id:
            return raw_id
        return f"{brand}/{raw_id}"

    # ── Fetching ──────────────────────────────────────────────────────

    async def refresh_provider(self, provider: CloudProvider) -> Dict[str, str]:
        """Fetch and normalize one provider's catalog.

        Returns ``{normalized_id: upstream_id}``.  On failure the previously
        cached list is kept (persisted from last successful run).
        """
        headers = ProviderRegistry.build_forward_headers(provider)
        url = f"{provider.base_url}/models"
        timeout = min(max(float(provider.timeout_seconds), 1.0), 30.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning(
                "⚠️  Cloud catalog fetch failed for provider '%s' (%s); keeping last successful list",
                provider.name,
                exc,
            )
            return dict(self._catalogs.get(provider.name, {}).get("models", {}))

        brand = self._default_brand(provider)
        normalized: Dict[str, str] = {}
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            logger.warning("⚠️  Provider '%s' /v1/models returned no 'data' list", provider.name)
            return dict(self._catalogs.get(provider.name, {}).get("models", {}))

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id") or entry.get("name")
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            norm = self._normalize_upstream_id(raw_id, brand)
            if norm:
                normalized[norm] = raw_id.strip()
        if not normalized:
            logger.warning("⚠️  Provider '%s' /v1/models returned an empty catalog", provider.name)
            return dict(self._catalogs.get(provider.name, {}).get("models", {}))

        self._catalogs[provider.name] = {
            "fetched_at": time.time(),
            "models": normalized,
        }
        self._persist_cache()
        logger.info(
            "☁️  Cloud catalog refreshed for provider '%s': %d model(s)",
            provider.name,
            len(normalized),
        )
        return normalized

    async def refresh_all(self) -> None:
        """Fetch every enabled+configured provider catalog concurrently-ish."""
        for provider in self._registry.get_enabled_providers():
            if not provider.is_configured:
                logger.info("☁️  Provider '%s' has no API key; skipping catalog fetch", provider.name)
                continue
            try:
                await self.refresh_provider(provider)
            except Exception as e:
                logger.warning("☁️  Catalog refresh for '%s' failed: %s", provider.name, e)

    def is_stale(self, provider_name: str) -> bool:
        data = self._catalogs.get(provider_name)
        if data is None:
            return True
        return (time.time() - float(data.get("fetched_at", 0))) > self._ttl_seconds

    async def ensure_fresh(self, provider_name: str) -> None:
        """Refresh a provider's catalog only when its TTL has elapsed."""
        provider = self._registry._providers.get(provider_name)
        if provider is None or not provider.is_configured:
            return
        if not self.is_stale(provider_name):
            return
        try:
            await self.refresh_provider(provider)
        except Exception as e:
            logger.warning("☁️  ensure_fresh failed for '%s': %s", provider_name, e)

    # ── Queries ───────────────────────────────────────────────────────

    def get_models_for_provider(self, provider_name: str) -> Dict[str, str]:
        """Return ``{normalized_id: upstream_id}`` for a provider (cached view)."""
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return {}
        return dict(data.get("models", {}))

    def get_model_overrides(self, normalized_id: str, provider_name: str = "") -> Dict[str, Any]:
        """Return per-model overrides layered from cloud_models.yaml.

        Keys may be the full ``{provider}/{brand}/{model}``, ``{brand}/{model}``,
        or the bare upstream id.  Precedence: full address > namespaced > bare.
        """
        return dict(self._overrides.get(normalized_id, {}) or {})

    def get_override(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self._overrides.get(key)
        if isinstance(raw, dict):
            return dict(raw)
        return None

    def addresses(self, provider_name: str) -> List[str]:
        """Return the full ``{provider}/{brand}/{model}`` addresses for a provider."""
        provider = provider_name
        return [
            f"{provider}/{norm}"
            for norm in self.get_models_for_provider(provider_name)
        ]

    # ── Addressing / resolution ───────────────────────────────────────

    def resolve_cloud_target(
        self,
        model_name: str,
        fallback: Optional[CloudProvider] = None,
    ) -> Optional[Tuple[str, str]]:
        """Resolve a cloud model address to ``(provider_name, upstream_model)``.

        Accepts either the full ``{provider}/{brand}/{model}`` address (the
        ``{provider}`` segment names a configured provider) or a bare upstream
        name that matches a configured provider (``model_prefixes``/``models``).

        The upstream id is looked up in the fetched per-provider catalog so a
        provider that answers with bare ids (openai ``gpt-4o``, google
        ``gemini-…``) maps to the bare id the upstream API actually expects.
        When the catalog has not been fetched yet (cold-start) it falls back
        to stripping the ``{provider}/`` segment from the address.
        """
        # Full {provider}/{brand}/{model}: first segment is a known provider.
        first, sep, rest = model_name.partition("/")
        if sep and first and first in self._registry._providers:
            provider = self._registry._providers[first]
            catalog = self.get_models_for_provider(first)
            upstream = catalog.get(rest) if rest else None
            if upstream is None:
                upstream = rest or None
            if upstream is None:
                return None
            return first, upstream

        # Bare upstream name: resolve via the existing provider registry.
        provider = fallback or self._registry.get_provider_for_model(model_name)
        if provider is None:
            return None
        canonical = ProviderRegistry.canonical_model_id(model_name)
        catalog = self.get_models_for_provider(provider.name)
        return provider.name, catalog.get(canonical, canonical)
