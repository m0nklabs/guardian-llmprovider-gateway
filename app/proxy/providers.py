"""Cloud LLM provider registry for Guardian's multi-backend router.

Guardian traditionally proxies every inference request to a single local
``llama-server`` backend.  This module adds support for *cloud* providers —
currently OpenRouter, NVIDIA, and Poolside — so Guardian can act as a unified
LLM router.

A provider is configured in ``config/settings.yaml`` under the top-level
``providers`` key::

    providers:
      openrouter:
        enabled: true
        base_url: https://openrouter.ai/api/v1
        api_key: ${OPENROUTER_API_KEY}
        timeout_seconds: 600
        models:
          - anthropic/claude-3.5-sonnet
          - openai/gpt-4o
      nvidia:
        enabled: true
        base_url: https://integrate.api.nvidia.com/v1
        api_key: ${NVIDIA_API_KEY}
        timeout_seconds: 600
        models:
          - nvidia/llama-3.1-nemotron-70b-instruct

When a requested model matches a cloud provider entry, Guardian forwards the
request directly to that provider instead of routing through the local
GPU-backed ``llama-server``.  Cloud models bypass the VRAM scheduler, model
switch logic, and inference queue entirely — the cloud API handles its own
rate limiting and concurrency.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

from app.proxy.cloud_keys import parse_guardian_route

logger = logging.getLogger("Guardian.Providers")

# Matches ``${ENV_VAR}`` or ``$ENV_VAR`` in config strings.
_ENV_VAR_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")

CONTEXT_CATALOG_TTL_SECONDS = 3600.0


@dataclass(frozen=True)
class ContextCatalog:
    """A timestamped upstream model catalog reduced to context windows."""

    fetched_at: float
    context_windows: Dict[str, int]


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` references in a string using process environment.

    Unknown variables are replaced with an empty string so misconfiguration
    fails loudly at request time rather than leaking the literal placeholder.
    """
    def _replace(match: re.Match) -> str:
        return os.environ.get(match.group("name"), "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


@dataclass
class CloudProvider:
    """A single upstream cloud LLM provider."""

    name: str
    base_url: str
    api_key: str
    models: List[str] = field(default_factory=list)
    enabled: bool = True
    timeout_seconds: float = 600.0
    # Provider-specific extra headers (e.g. OpenRouter ranking headers).
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        """True when the provider has a non-empty API key."""
        return bool(self.api_key and self.api_key.strip())


class ProviderRegistry:
    """Registry of cloud LLM providers and their model-to-provider mapping.

    The registry is cheap to reconstruct and designed for hot-reload: call
    :meth:`reload` after editing ``settings.yaml`` to pick up new providers or
    model lists without restarting Guardian.
    """

    def __init__(self, settings_path: Optional[Path] = None) -> None:
        if settings_path is None:
            settings_path = (
                Path(__file__).parent.parent.parent / "config" / "settings.yaml"
            )
        self._settings_path = settings_path
        self._providers: Dict[str, CloudProvider] = {}
        self._model_to_provider: Dict[str, CloudProvider] = {}
        # Ordered (prefix, provider) pairs for namespace-based cloud
        # recognition; populated in :meth:`reload` from each provider's
        # ``model_prefixes`` config.  Exact ``models`` entries always win
        # over prefix matches (see :meth:`get_provider_for_model`).
        self._prefix_to_provider: List[Tuple[str, CloudProvider]] = []
        self._context_overrides: Dict[str, int] = {}
        self._context_catalogs: Dict[str, ContextCatalog] = {}
        self._context_catalog_locks: Dict[str, asyncio.Lock] = {}
        self.reload()

    # ── Loading ──────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read provider configuration from ``settings.yaml``."""
        self._providers.clear()
        self._model_to_provider.clear()
        self._prefix_to_provider.clear()
        self._context_catalogs.clear()
        self._context_catalog_locks.clear()

        raw_config = self._load_settings_config()
        raw_overrides = raw_config.get("context_overrides", {})
        self._context_overrides = self._parse_context_overrides(raw_overrides)
        raw_providers = raw_config.get("providers", {})
        if not isinstance(raw_providers, dict):
            logger.warning("⚠️  'providers' in settings.yaml is not a dict; ignoring")
            raw_providers = {}
        for provider_name, cfg in raw_providers.items():
            if not isinstance(cfg, dict):
                logger.warning("⚠️  Provider '%s' config is not a dict; skipping", provider_name)
                continue

            enabled = bool(cfg.get("enabled", True))
            base_url = str(cfg.get("base_url", "")).rstrip("/")
            api_key = _expand_env(str(cfg.get("api_key", "")))
            models = [str(m) for m in (cfg.get("models") or []) if m]
            # Namespace prefixes (e.g. ``nvidia/``) let Guardian recognise a
            # cloud model by its raw upstream name without an explicit listing
            # or a per-key ``guardian/...`` route.  A trailing ``/`` is enforced
            # so prefixes match whole namespace segments, not partial names.
            prefixes = [
                (p if p.endswith("/") else p + "/")
                for p in (cfg.get("model_prefixes") or [])
                if isinstance(p, str) and p.strip()
            ]
            timeout = float(cfg.get("timeout_seconds", 600.0))
            extra_headers: Dict[str, str] = {}
            if isinstance(cfg.get("extra_headers"), dict):
                extra_headers = {
                    str(k): _expand_env(str(v))
                    for k, v in cfg["extra_headers"].items()
                }

            provider = CloudProvider(
                name=provider_name,
                base_url=base_url,
                api_key=api_key,
                models=models,
                enabled=enabled,
                timeout_seconds=timeout,
                extra_headers=extra_headers,
            )
            self._providers[provider_name] = provider

            if not enabled:
                logger.info("☁️  Provider '%s' is disabled", provider_name)
                continue
            if not provider.is_configured:
                logger.warning(
                    "⚠️  Provider '%s' has no API key — global cloud models will not be advertised",
                    provider_name,
                )
            for model_name in models:
                if model_name in self._model_to_provider:
                    existing = self._model_to_provider[model_name]
                    logger.warning(
                        "⚠️  Model '%s' is registered on both '%s' and '%s'; "
                        "keeping the first ('%s')",
                        model_name,
                        existing.name,
                        provider_name,
                        existing.name,
                    )
                    continue
                self._model_to_provider[model_name] = provider

            # Register namespace prefixes for this enabled provider.  Exact
            # ``models`` entries above take priority at lookup time; prefixes
            # provide key-independent recognition for unlisted cloud models
            # that share a namespace with a configured model.
            for prefix in prefixes:
                self._prefix_to_provider.append((prefix, provider))

        if self._model_to_provider:
            logger.info(
                "☁️  Loaded %d cloud model(s) across %d provider(s): %s",
                len(self._model_to_provider),
                sum(1 for p in self._providers.values() if p.enabled),
                ", ".join(sorted(self._model_to_provider.keys())),
            )
        if self._prefix_to_provider:
            logger.info(
                "☁️  Loaded %d cloud namespace prefix(es): %s",
                len(self._prefix_to_provider),
                ", ".join(sorted(p for p, _ in self._prefix_to_provider)),
            )

    def _load_settings_config(self) -> Dict[str, Any]:
        """Read the complete Guardian settings document."""
        try:
            if not self._settings_path.exists():
                return {}
            with open(self._settings_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception as e:
            logger.warning("Failed to load providers config from %s: %s", self._settings_path, e)
            return {}

    @staticmethod
    def _parse_positive_integer(value: Any) -> Optional[int]:
        """Return *value* as a positive integer, or ``None`` when invalid."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else None
        return None

    @classmethod
    def canonical_model_id(cls, model_name: str) -> str:
        """Normalize supported OpenRouter and Guardian route prefixes."""
        if not isinstance(model_name, str):
            return ""
        route = parse_guardian_route(model_name)
        canonical = route[1] if route is not None else model_name
        if canonical.startswith("openrouter/"):
            canonical = canonical[len("openrouter/"):]
        return canonical

    @classmethod
    def _parse_context_overrides(cls, raw_overrides: Any) -> Dict[str, int]:
        """Normalize configured context override values by canonical model ID."""
        if not isinstance(raw_overrides, dict):
            logger.warning("⚠️  'context_overrides' in settings.yaml is not a map; ignoring")
            return {}

        overrides: Dict[str, int] = {}
        for model_name, value in raw_overrides.items():
            context_window = cls._parse_positive_integer(value)
            canonical_name = cls.canonical_model_id(str(model_name))
            if context_window is None or not canonical_name:
                logger.warning("⚠️  Ignoring invalid context override for '%s'", model_name)
                continue
            overrides[canonical_name] = context_window
        return overrides

    # ── Public API ───────────────────────────────────────────────────

    def is_cloud_model(self, model_name: str) -> bool:
        """Return True if *model_name* is served by a cloud provider.

        A model counts as cloud-hosted when it is either explicitly listed in
        a provider's ``models`` config **or** matches one of that provider's
        ``model_prefixes`` namespace patterns (e.g. ``nvidia/...``).  This
        recognition is purely name-based and independent of the requesting
        client's API key — Guardian classifies cloud vs. local before any
        per-key credential lookup happens.
        """
        return self.get_provider_for_model(model_name) is not None

    def get_provider_for_model(self, model_name: str) -> Optional[CloudProvider]:
        """Return the :class:`CloudProvider` that serves *model_name*.

        Resolution order: an exact ``models`` entry wins (this preserves
        explicit disambiguation when a model is listed on more than one
        provider); otherwise the first matching ``model_prefixes`` entry in
        provider declaration order is used, so a cloud model can be reached by
        its raw upstream name without a per-key ``guardian/...`` route.
        """
        if model_name.startswith("openrouter/"):
            canonical_name = self.canonical_model_id(model_name)
            if canonical_name.startswith("openrouter/"):
                return None
            provider = self._get_configured_provider_for_model(canonical_name)
            if provider is not None and provider.name == "openrouter":
                return provider
            return None

        return self._get_configured_provider_for_model(model_name)

    def _get_configured_provider_for_model(self, model_name: str) -> Optional[CloudProvider]:
        """Resolve an unprefixed model against configured exact names and namespaces."""
        provider = self._model_to_provider.get(model_name)
        if provider is not None:
            return provider
        for prefix, candidate in self._prefix_to_provider:
            if model_name.startswith(prefix):
                return candidate
        return None

    def get_all_cloud_models(self) -> List[str]:
        """Return global cloud models backed by configured provider keys."""
        return [
            model_name
            for model_name, provider in self._model_to_provider.items()
            if provider.is_configured
        ]

    def get_enabled_providers(self) -> List[CloudProvider]:
        """Return all enabled providers (regardless of API-key presence)."""
        return [p for p in self._providers.values() if p.enabled]

    def get_context_override(self, model_name: str) -> Optional[int]:
        """Return a configured context override for any supported ID variant."""
        return self._context_overrides.get(self.canonical_model_id(model_name))

    @staticmethod
    def _catalog_cache_key(provider: CloudProvider) -> str:
        """Return a non-secret cache key scoped to the effective credential."""
        credential_fingerprint = hashlib.sha256(provider.api_key.encode("utf-8")).hexdigest()[:16]
        return f"{provider.name}:{provider.base_url}:{credential_fingerprint}"

    def _get_cloud_context_target(
        self,
        model_name: str,
    ) -> Tuple[Optional[CloudProvider], str]:
        """Return the upstream provider and canonical model ID for a cloud route."""
        route = parse_guardian_route(model_name)
        if route is not None:
            provider = self._providers.get(route[0])
            return provider, self.canonical_model_id(model_name)

        canonical_name = self.canonical_model_id(model_name)
        if model_name.startswith("openrouter/"):
            return self._providers.get("openrouter"), canonical_name
        return self.get_provider_for_model(model_name), canonical_name

    @classmethod
    def _extract_context_windows(cls, payload: Any) -> Dict[str, int]:
        """Extract valid context sizes from OpenAI-compatible model catalogs."""
        if not isinstance(payload, dict):
            return {}
        entries = payload.get("data", payload.get("models", []))
        if not isinstance(entries, list):
            return {}

        context_windows: Dict[str, int] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id") or entry.get("name")
            if not isinstance(model_id, str) or not model_id:
                continue
            context_window = cls._parse_positive_integer(entry.get("context_length"))
            if context_window is None:
                context_window = cls._parse_positive_integer(entry.get("max_input_tokens"))
            if context_window is not None:
                context_windows[cls.canonical_model_id(model_id)] = context_window
        return context_windows

    async def _get_context_catalog(self, provider: CloudProvider) -> ContextCatalog:
        """Fetch a provider catalog at most once per configured TTL window."""
        cache_key = self._catalog_cache_key(provider)
        now = time.monotonic()
        cached = self._context_catalogs.get(cache_key)
        if cached is not None and now - cached.fetched_at < CONTEXT_CATALOG_TTL_SECONDS:
            return cached

        lock = self._context_catalog_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._context_catalogs.get(cache_key)
            if cached is not None and now - cached.fetched_at < CONTEXT_CATALOG_TTL_SECONDS:
                return cached

            context_windows: Dict[str, int] = {}
            catalog_url = f"{provider.base_url}/models"
            try:
                headers = self.build_forward_headers(provider)
                timeout_seconds = min(max(provider.timeout_seconds, 1.0), 10.0)
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.get(catalog_url, headers=headers)
                response.raise_for_status()
                context_windows = self._extract_context_windows(response.json())
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                logger.warning(
                    "⚠️  Unable to refresh context catalog for provider '%s'; preserving last known context data: %s",
                    provider.name,
                    exc,
                )
                if cached is not None:
                    context_windows = cached.context_windows

            catalog = ContextCatalog(fetched_at=now, context_windows=context_windows)
            self._context_catalogs[cache_key] = catalog
            return catalog

    async def get_cloud_context_window(
        self,
        model_name: str,
        provider: Optional[CloudProvider] = None,
    ) -> Optional[int]:
        """Return the configured or upstream-catalog context size for a cloud model."""
        override = self.get_context_override(model_name)
        if override is not None:
            return override

        resolved_provider, canonical_name = self._get_cloud_context_target(model_name)
        effective_provider = provider or resolved_provider
        if effective_provider is None or not canonical_name:
            return None
        catalog = await self._get_context_catalog(effective_provider)
        return catalog.context_windows.get(canonical_name)

    # ── Model metadata ───────────────────────────────────────────────

    def build_model_metadata_entry(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Build an OpenAI-style ``/v1/models`` entry for a cloud model."""
        provider = self.get_provider_for_model(model_name)
        if provider is None:
            return None
        return {
            "id": model_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": provider.name,
            "permission": [],
            "served_by": "cloud",
            "provider": provider.name,
        }

    # ── Request forwarding helpers ───────────────────────────────────

    @staticmethod
    def build_forward_headers(
        provider: CloudProvider,
        client_user_id: Optional[str] = None,
        app_name: Optional[str] = None,
    ) -> Dict[str, str]:
        """Build the HTTP headers for forwarding a request to *provider*.

        When *app_name* is provided, it is used for OpenRouter attribution
        (``X-Title`` and ``HTTP-Referer``) so each app appears separately in
        OpenRouter analytics/rankings instead of all showing as "Guardian".

        When *client_user_id* is provided and the provider is OpenRouter, it is
        **not** sent as a header — OpenRouter expects the per-user identifier
        in the request body ``user`` field (see :mod:`app.proxy.server`).
        This parameter is accepted here for future providers that may use a
        header-based approach.
        """
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
        }
        # OpenRouter benefits from attribution headers for ranking/leaderboards.
        if provider.name == "openrouter":
            # Per-app attribution: show the actual app name (e.g. "goose")
            # instead of a generic "Guardian" so analytics/rankings on
            # OpenRouter distinguish between apps.
            #
            # The app identifier is encoded as a SUBDOMAIN (not a URL path)
            # because OpenRouter's dashboard groups attributions by request
            # origin and strips the path component of HTTP-Referer — a path
            # like https://guardian.local/goose would collapse back to the bare
            # origin https://guardian.local and all apps would merge. A subdomain
            # like https://goose.guardian.local keeps each app as a distinct
            # origin in OpenRouter's logs/dashboard. App names are lowercased
            # (subdomains are case-insensitive; lowercase is the convention).
            if app_name:
                app_slug = app_name.lower()
                referer = f"https://{app_slug}.guardian.local"
                display_name = f"Guardian/{app_name}"
            else:
                referer = "https://guardian.local"
                display_name = "Guardian"
            headers.setdefault("HTTP-Referer", referer)
            headers.setdefault("X-Title", display_name)
            # Enable response caching so identical requests from the same app
            # get zero-cost cache hits.  The cache key includes a SHA-256 of
            # the request body, and the per-client ``user`` field injected by
            # the proxy ensures different apps get separate cache entries.
            headers.setdefault("X-OpenRouter-Cache", "true")
        headers.update(provider.extra_headers)
        return headers

    @staticmethod
    def build_forward_url(provider: CloudProvider, path: str) -> str:
        """Build the full upstream URL for a given OpenAI-style *path*.

        ``path`` is the part after ``/v1/`` (e.g. ``chat/completions``).
        """
        return f"{provider.base_url}/{path}"
