"""Cloud LLM provider registry for Guardian's multi-backend router.

Guardian traditionally proxies every inference request to a single local
``llama-server`` backend.  This module adds support for *cloud* providers —
currently OpenRouter and NVIDIA — so Guardian can act as a unified LLM router.

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

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("Guardian.Providers")

# Matches ``${ENV_VAR}`` or ``$ENV_VAR`` in config strings.
_ENV_VAR_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")


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
        self.reload()

    # ── Loading ──────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read provider configuration from ``settings.yaml``."""
        self._providers.clear()
        self._model_to_provider.clear()

        raw_providers = self._load_providers_config()
        for provider_name, cfg in raw_providers.items():
            if not isinstance(cfg, dict):
                logger.warning("⚠️  Provider '%s' config is not a dict; skipping", provider_name)
                continue

            enabled = bool(cfg.get("enabled", True))
            base_url = str(cfg.get("base_url", "")).rstrip("/")
            api_key = _expand_env(str(cfg.get("api_key", "")))
            models = [str(m) for m in (cfg.get("models") or []) if m]
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
                    "⚠️  Provider '%s' has no API key — cloud models will return 503",
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

        if self._model_to_provider:
            logger.info(
                "☁️  Loaded %d cloud model(s) across %d provider(s): %s",
                len(self._model_to_provider),
                sum(1 for p in self._providers.values() if p.enabled),
                ", ".join(sorted(self._model_to_provider.keys())),
            )

    def _load_providers_config(self) -> Dict[str, Any]:
        """Read the ``providers`` section from settings.yaml."""
        try:
            if not self._settings_path.exists():
                return {}
            with open(self._settings_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
            providers = cfg.get("providers", {})
            if not isinstance(providers, dict):
                logger.warning("⚠️  'providers' in settings.yaml is not a dict; ignoring")
                return {}
            return providers
        except Exception as e:
            logger.warning("Failed to load providers config from %s: %s", self._settings_path, e)
            return {}

    # ── Public API ───────────────────────────────────────────────────

    def is_cloud_model(self, model_name: str) -> bool:
        """Return True if *model_name* is served by a cloud provider."""
        return model_name in self._model_to_provider

    def get_provider_for_model(self, model_name: str) -> Optional[CloudProvider]:
        """Return the :class:`CloudProvider` that serves *model_name*."""
        return self._model_to_provider.get(model_name)

    def get_all_cloud_models(self) -> List[str]:
        """Return all cloud model names across all enabled providers."""
        return list(self._model_to_provider.keys())

    def get_enabled_providers(self) -> List[CloudProvider]:
        """Return all enabled providers (regardless of API-key presence)."""
        return [p for p in self._providers.values() if p.enabled]

    # ── Model metadata ───────────────────────────────────────────────

    def build_model_metadata_entry(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Build an OpenAI-style ``/v1/models`` entry for a cloud model."""
        provider = self._model_to_provider.get(model_name)
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
            display_name = f"Guardian/{app_name}" if app_name else "Guardian"
            headers.setdefault("HTTP-Referer", f"https://guardian.local/{app_name}" if app_name else "https://guardian.local")
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
