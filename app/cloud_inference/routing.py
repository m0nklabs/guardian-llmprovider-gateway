"""Cloud routing — attempt resolution, candidate preparation, capture setup.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
These functions handle the routing layer between a client request and the
actual cloud provider call: resolving which provider(s) to try, preparing
the request body, and setting up capture context.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from app.proxy.cloud_keys import parse_guardian_route
from app.proxy.providers import CloudProvider, ProviderRegistry

logger = logging.getLogger("Guardian")

# ── Injected ─────────────────────────────────────────────────────────
_provider_registry: Optional[ProviderRegistry] = None
_cloud_cred_store = None
_failover_registry = None
_failover_health = None
_get_request_auth_context = None
_capture_client_fingerprint = None
_capture_endpoint_from_request = None
_dispatch_capture_request_received = None
_get_capture_controller = None
_provider_base_url = None
_cloud_provider_for_request = None
_cloud_provider_unavailable_error = None
_adapt_openai_reasoning_params = None

# Protocol constants (lazily imported to avoid cycles)
PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OLLAMA = "ollama"
ROUTE_CLOUD = "cloud"

def init(
    provider_registry: ProviderRegistry,
    cloud_cred_store,
    failover_registry,
    failover_health,
    get_request_auth_context,
    capture_client_fingerprint,
    capture_endpoint_from_request,
    dispatch_capture_request_received,
    get_capture_controller,
    provider_base_url,
    cloud_provider_for_request,
    cloud_provider_unavailable_error,
    adapt_openai_reasoning_params,
) -> None:
    """Inject all dependencies. Called once at startup."""
    global _provider_registry, _cloud_cred_store, _failover_registry, _failover_health
    global _get_request_auth_context, _capture_client_fingerprint, _capture_endpoint_from_request
    global _dispatch_capture_request_received, _get_capture_controller
    global _provider_base_url, _cloud_provider_for_request, _cloud_provider_unavailable_error
    global _adapt_openai_reasoning_params

    _provider_registry = provider_registry
    _cloud_cred_store = cloud_cred_store
    _failover_registry = failover_registry
    _failover_health = failover_health
    _get_request_auth_context = get_request_auth_context
    _capture_client_fingerprint = capture_client_fingerprint
    _capture_endpoint_from_request = capture_endpoint_from_request
    _dispatch_capture_request_received = dispatch_capture_request_received
    _get_capture_controller = get_capture_controller
    _provider_base_url = provider_base_url
    _cloud_provider_for_request = cloud_provider_for_request
    _cloud_provider_unavailable_error = cloud_provider_unavailable_error
    _adapt_openai_reasoning_params = adapt_openai_reasoning_params


# ── Cloud attempt resolution ─────────────────────────────────────────


def resolve_cloud_attempts(
    model_name: str,
    request: Request,
    client_id: str,
    *,
    requires_vision: bool = False,
) -> Tuple[List[Tuple[CloudProvider, str]], Optional[str]]:
    """Resolve the ordered list of (provider, upstream_model) attempts.

    Returns (attempts, failover_group_name). Raises HTTPException on failure.
    """
    guardian_route = parse_guardian_route(model_name)
    if guardian_route is None:
        # Global cloud model from settings.yaml providers config
        provider = _cloud_provider_for_request(model_name)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' is not a cloud model")
        if not provider.is_configured:
            raise _cloud_provider_unavailable_error(provider)
        return [(provider, ProviderRegistry.canonical_model_id(model_name))], None

    provider_name, model_path = guardian_route
    auth_ctx = _get_request_auth_context(request) or {}
    key_fingerprint = auth_ctx.get("key_fingerprint") or client_id

    if provider_name == "failover":
        group = _failover_registry.get_group(model_path)
        if group is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "failover_group_not_found",
                    "group": model_path,
                    "message": f"No failover group named '{model_path}' is configured.",
                },
            )
        ordered = _failover_health.order_candidates(group.candidates)
        attempts: List[Tuple[CloudProvider, str]] = []
        for candidate in ordered:
            if requires_vision and "image" not in candidate.modalities:
                continue
            cred = _cloud_cred_store.get_credential_for_key(key_fingerprint, candidate.provider)
            if cred is None:
                continue
            if candidate.provider == "google" and candidate.model not in cred.models:
                continue
            attempts.append((
                CloudProvider(
                    name=candidate.provider,
                    base_url=_provider_base_url(candidate.provider),
                    api_key=cred.api_key,
                    models=[candidate.model],
                ),
                candidate.model,
            ))
        if not attempts:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "cloud_credential_not_linked",
                    "reason": "no_credential_for_failover_group",
                    "message": (
                        f"No credential is linked to your Guardian API key for any "
                        f"provider in failover group '{model_path}'."
                    ),
                    "group": model_path,
                },
            )
        return attempts, model_path

    # Per-key cloud route: guardian/{provider}/{model_path}
    cred = _cloud_cred_store.get_credential_for_key(key_fingerprint, provider_name)
    if cred is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "cloud_credential_not_linked",
                "reason": "no_credential_for_provider",
                "message": (
                    f"No {provider_name} credential is linked to your Guardian API key. "
                    f"Link a credential via the Guardian admin API or dashboard."
                ),
                "provider": provider_name,
                "requested_route": model_name,
            },
        )
    if provider_name == "google" and model_path not in cred.models:
        raise HTTPException(
            status_code=404,
            detail=f"Google model '{model_path}' is not available for this credential",
        )
    provider = CloudProvider(
        name=provider_name,
        base_url=_provider_base_url(provider_name),
        api_key=cred.api_key,
        models=[model_path],
    )
    return [(provider, model_path)], None


def resolve_cloud_vision_fallback(model_name: str) -> Optional[str]:
    """Return a local vision fallback for a text-only cloud model."""
    guardian_route = parse_guardian_route(model_name)
    if guardian_route is None:
        return _failover_registry.get_image_fallback_for_model(
            ProviderRegistry.canonical_model_id(model_name)
        )
    provider_name, model_path = guardian_route
    if provider_name != "failover":
        return _failover_registry.get_image_fallback_for_model(model_path)
    group = _failover_registry.get_group(model_path)
    if group is None or group.has_image_capable_candidate():
        return None
    return group.image_fallback_model


# ── Candidate request preparation ──────────────────────────────────


def prepare_cloud_candidate_request(
    provider: CloudProvider,
    upstream_model: str,
    path: str,
    base_json_body: Dict[str, Any],
    client_user_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], bytes, bool]:
    """Build the request body/path for one failover candidate.

    Returns (effective_path, json_body, body_bytes, needs_translation).
    """
    from app.proxy.anthropic_bridge import (
        provider_needs_anthropic_translation,
        translate_anthropic_request_to_openai,
    )

    candidate_json_body = {**base_json_body, "model": upstream_model}

    if client_user_id and provider.name == "openrouter" and "user" not in candidate_json_body:
        candidate_json_body["user"] = client_user_id

    needs_translation = provider_needs_anthropic_translation(provider.name, path)
    effective_path = path
    if needs_translation:
        candidate_json_body = translate_anthropic_request_to_openai(candidate_json_body)
        effective_path = "chat/completions"

    model_defaults = _cloud_cred_store.get_model_defaults(upstream_model)
    if model_defaults:
        missing = {k: v for k, v in model_defaults.items() if k not in candidate_json_body}
        if missing:
            candidate_json_body = {**candidate_json_body, **missing}
            logger.info("☁️  Applied model defaults for '%s': %s", upstream_model, missing)

    candidate_json_body = _adapt_openai_reasoning_params(
        provider, upstream_model, candidate_json_body
    )

    candidate_body = json.dumps(candidate_json_body).encode("utf-8")
    return effective_path, candidate_json_body, candidate_body, needs_translation


# ── Response content extraction ─────────────────────────────────────


def extract_cloud_response_content(
    payload: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[list]]:
    """Extract text content and tool_calls from a non-streaming cloud response."""
    if not isinstance(payload, dict):
        return None, None

    content_parts: list[str] = []
    tool_calls: Optional[list] = None

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if isinstance(msg, dict):
            text = msg.get("content")
            if isinstance(text, str) and text:
                content_parts.append(text)
            elif isinstance(text, list):
                for block in text:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content_parts.append(block.get("text", ""))
            tc = msg.get("tool_calls")
            if isinstance(tc, list) and tc:
                tool_calls = tc
            reasoning = msg.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning and not content_parts:
                content_parts.append(reasoning)

    if not content_parts and tool_calls is None:
        anthropic_content = payload.get("content")
        if isinstance(anthropic_content, list):
            for block in anthropic_content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    content_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append({
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })

    content = "\n".join(content_parts) if content_parts else None
    return content, tool_calls


# ── Cloud capture setup ─────────────────────────────────────────────


def setup_cloud_capture(
    request: Request,
    client_id: str,
    *,
    model_name: str,
    json_body: Dict[str, Any],
    path: str,
) -> Tuple[Optional[Any], Optional[Any], Optional[str], Optional[float]]:
    """Set up capture context for a cloud route.

    Returns (ctx, policy_result, request_id, start_time).
    All values may be None when capture is disabled or evaluation fails.
    """
    from app.capture.integration import get_capture_controller
    from app.capture.schema import BuildContext
    from app.capture.config import PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC, PROTOCOL_OLLAMA
    from app.capture.redactor import anthropic_messages_to_openai

    try:
        controller = get_capture_controller()
        client_fp = _capture_client_fingerprint(request, client_id)
        endpoint = _capture_endpoint_from_request(request)
        if endpoint.startswith("/v1/messages"):
            protocol = PROTOCOL_ANTHROPIC
        elif endpoint.startswith("/api/chat") or endpoint.startswith("/api/generate"):
            protocol = PROTOCOL_OLLAMA
        else:
            protocol = PROTOCOL_OPENAI

        cloud_request_id = str(uuid.uuid4())

        capture_messages = None
        capture_params = None
        if isinstance(json_body, dict):
            if protocol == PROTOCOL_ANTHROPIC:
                capture_messages = anthropic_messages_to_openai(
                    messages=json_body.get("messages", []),
                    system=json_body.get("system"),
                )
                capture_params = {
                    k: v for k, v in json_body.items()
                    if k not in ("messages", "system")
                }
            else:
                capture_messages = json_body.get("messages")
                capture_params = {
                    k: v for k, v in json_body.items() if k != "messages"
                }

        policy_result = _dispatch_capture_request_received(
            request, client_id,
            request_id=cloud_request_id,
            endpoint=endpoint,
            ingress_protocol=protocol,
            route_type=ROUTE_CLOUD,
            requested_model=model_name,
            resolved_model=model_name,
            request_messages=capture_messages,
            request_parameters=capture_params,
            queue_wait_ms=0,
        )

        if policy_result is not None and policy_result.should_capture:
            ctx = BuildContext(
                request_id=cloud_request_id,
                endpoint=endpoint,
                ingress_protocol=protocol,
                route_type=ROUTE_CLOUD,
                requested_model=model_name,
                resolved_model=model_name,
                capture_policy_version=controller.config.policy_version,
                instance_id=controller.config.instance_id,
                client_fingerprint=client_fp,
            )
            start_time = time.monotonic()
            return ctx, policy_result, cloud_request_id, start_time

    except Exception:
        pass

    return None, None, None, None
