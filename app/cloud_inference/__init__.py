"""Cloud inference helpers — stateless routing, retry, and param adaptation.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).

This module holds the cloud-provider helpers that have minimal coupling to
the request pipeline: provider URL resolution, model-route classification,
Google AI Studio catalog discovery, retry classification, response-header
sanitisation, debug headers, and OpenAI reasoning-model parameter adaptation.

Functions that need the request object (auth context, capture dispatch,
usage tracking, streaming) remain in ``server.py`` pending further
extraction.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from app.proxy.cloud_keys import parse_guardian_route
from app.proxy.providers import CloudProvider, ProviderRegistry

logger = logging.getLogger("Guardian")

# ── Injected dependencies ────────────────────────────────────────────

_provider_registry: Optional[ProviderRegistry] = None


def init(provider_registry: ProviderRegistry) -> None:
    """Inject the singleton ProviderRegistry.  Called once at startup."""
    global _provider_registry
    _provider_registry = provider_registry


# ── Provider base URLs ───────────────────────────────────────────────

_PROVIDER_BASE_URLS: Dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "poolside": "https://inference.poolside.ai/v1",
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}

_GOOGLE_MODEL_CATALOG_URL = f"{_PROVIDER_BASE_URLS['google']}/models"
_GOOGLE_MODEL_CATALOG_TIMEOUT_S = 30.0


# ── Google model discovery ───────────────────────────────────────────


def normalize_google_model_id(model_id: str) -> str:
    """Normalize Google catalog IDs to bare OpenAI-compatible model names."""
    normalized = model_id.strip()
    if normalized.startswith("models/"):
        normalized = normalized[len("models/") :]
    return normalized


def parse_google_model_catalog(payload: Any) -> List[str]:
    """Validate and normalize Google OpenAI-compatible model catalog data."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Google model catalog response is missing model data")
    models = sorted(
        {
            normalized
            for entry in payload["data"]
            if isinstance(entry, dict)
            for model_id in [entry.get("id")]
            if isinstance(model_id, str)
            for normalized in [normalize_google_model_id(model_id)]
            if normalized
        }
    )
    if not models:
        raise ValueError("Google model catalog response has no model data")
    return models


async def discover_google_models(api_key: str) -> List[str]:
    """Fetch the current Google AI Studio OpenAI-compatible model catalog."""
    try:
        timeout = httpx.Timeout(_GOOGLE_MODEL_CATALOG_TIMEOUT_S, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                _GOOGLE_MODEL_CATALOG_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        response.raise_for_status()
        return parse_google_model_catalog(response.json())
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        logger.warning("Google model catalog discovery failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail={
                "error": "google_model_discovery_failed",
                "message": "Google model catalog could not be retrieved.",
            },
        ) from exc


# ── Provider routing ─────────────────────────────────────────────────


def provider_base_url(provider_name: str) -> str:
    """Return the base URL for a known provider, or empty string."""
    return _PROVIDER_BASE_URLS.get(provider_name, "")


def cloud_provider_for_request(model_name: str) -> Optional[CloudProvider]:
    """Return the configured cloud provider for *model_name*, or None."""
    return _provider_registry.get_provider_for_model(model_name)


def is_cloud_or_guardian_route(model_name: str) -> bool:
    """Check if a model name is a cloud model or a per-key guardian route."""
    if _provider_registry.is_cloud_model(model_name):
        return True
    return parse_guardian_route(model_name) is not None


def cloud_provider_unavailable_error(provider: CloudProvider) -> HTTPException:
    """Build a 503 error for a provider that lacks an API key."""
    return HTTPException(
        status_code=503,
        detail={
            "error": "provider_unavailable",
            "reason": "missing_api_key",
            "message": (
                f"Cloud provider '{provider.name}' is enabled but has no API key "
                f"configured. Set the {provider.name.upper()}_API_KEY environment "
                f"variable or disable the provider in settings.yaml."
            ),
            "provider": provider.name,
        },
    )


# ── Retry classification ─────────────────────────────────────────────

_RETRYABLE_STATUS_CODES = {408, 409, 425, 500, 502, 503, 504}

_DEGRADED_ERROR_MARKERS = (
    "degraded function",
    "function cannot be invoked",
    "service is degraded",
    "service unavailable",
    "temporarily unavailable",
)


def is_retryable_cloud_error(status_code: int, error_body_text: str) -> bool:
    """Return True if a failover candidate's error is worth retrying on the next.

    A 429 qualifies after the per-key retry manager has exhausted its budget.
    Standard retryable 5xx/408/409/425 always qualify.  A 400 also qualifies
    when its body matches a known "provider is degraded" pattern.
    """
    if status_code == 429:
        return True
    if status_code in _RETRYABLE_STATUS_CODES:
        return True
    if status_code == 400 and error_body_text:
        lowered = error_body_text.lower()
        return any(marker in lowered for marker in _DEGRADED_ERROR_MARKERS)
    return False


# ── Response header handling ────────────────────────────────────────

_HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)


def sanitize_proxied_response_headers(headers: Any) -> Dict[str, str]:
    """Strip hop-by-hop and body-framing headers from an upstream response."""
    return {
        key: value
        for key, value in dict(headers or {}).items()
        if key.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }


def guardian_debug_headers(
    provider: CloudProvider,
    upstream_model: str,
    failover_group: Optional[str],
) -> Dict[str, str]:
    """Build response headers revealing which provider served a request."""
    headers: Dict[str, str] = {
        "X-Guardian-Provider": provider.name,
        "X-Guardian-Upstream-Model": upstream_model,
    }
    if failover_group:
        headers["X-Guardian-Failover-Group"] = failover_group
    return headers


# ── OpenAI reasoning-model parameter adaptation ──────────────────────

_OPENAI_REASONING_MODEL_PREFIXES: Tuple[str, ...] = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
)

_OPENAI_TEMP_RESTRICTED_PREFIXES: Tuple[str, ...] = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
)


def is_openai_reasoning_model(model_name: str) -> bool:
    """Return True for the OpenAI reasoning models that reject ``max_tokens``."""
    return model_name.startswith(_OPENAI_REASONING_MODEL_PREFIXES)


def adapt_openai_reasoning_params(
    provider: CloudProvider,
    upstream_model: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate client params for direct-OpenAI reasoning models.

    Many OpenAI-compatible clients send ``max_tokens`` and ``temperature``
    unconditionally.  OpenAI's reasoning models reject both:

    - **``max_tokens``** → rejected; must be ``max_completion_tokens``.
    - **``temperature``** on the o-series → rejected entirely.
    - **``temperature``** on gpt-5* → only the value ``1`` is accepted.

    Only applied to the direct ``openai`` provider.
    """
    if provider.name != "openai":
        return body
    if not is_openai_reasoning_model(upstream_model):
        return body

    adapted = dict(body)

    # max_tokens → max_completion_tokens (original dropped if both present)
    if "max_tokens" in adapted:
        if "max_completion_tokens" not in adapted:
            adapted["max_completion_tokens"] = adapted["max_tokens"]
        adapted.pop("max_tokens", None)

    # Temperature handling
    if upstream_model.startswith(_OPENAI_TEMP_RESTRICTED_PREFIXES):
        temp = adapted.get("temperature")
        if temp is not None:
            if upstream_model.startswith(("o1", "o3", "o4")):
                adapted.pop("temperature", None)
            elif temp != 1:
                adapted["temperature"] = 1

    return adapted


# ── Provider base URLs dict (for list_cloud_providers endpoint) ──────

def get_provider_base_urls() -> Dict[str, str]:
    """Return a copy of the provider base URL mapping."""
    return dict(_PROVIDER_BASE_URLS)
