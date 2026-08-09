"""Capture policy engine — deterministically decides whether a request is captured.

The policy is evaluated *after* authentication and model resolution but *before*
any route-specific transport translation.  This ensures the policy sees the
normalized semantic request (same messages and parameters that are forwarded to
llama-server or a cloud provider), not the raw client ingress.

Evaluation order:
1. Global kill switch (``config.enabled``) — if False, never capture.
2. Route-type switch (``config.local_capture`` / ``config.cloud_capture``) —
   matches against the ``route_type`` (local vs cloud).
3. Endpoint exclusion — admin/health/metrics endpoints are never captured.
4. Per-client opt-in — when enabled, only clients whose HMAC ``client_ref``
   appears in ``allowed_client_refs`` are captured.

The decision is **fail-open**: any unexpected error during evaluation results
in a "do not capture" decision, never blocking inference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.capture.config import CaptureConfig, EXCLUDED_PATH_PREFIXES

logger = logging.getLogger("Guardian.Capture.Policy")


@dataclass
class PolicyResult:
    """The result of a capture policy evaluation for a single request."""

    #: Whether this request should be captured.
    should_capture: bool

    #: Machine-readable reason code for the decision.
    reason: str

    #: Human-readable explanation for debug logging.
    detail: str = ""

    #: The capture policy version that produced this decision.
    policy_version: str = ""

    #: The resolved capture fields policy (redactor guidance).
    field_policies: Optional[Dict[str, str]] = None

    @property
    def is_capture(self) -> bool:
        return self.should_capture


def _matches_allowed_clients(
    client_ref: Optional[str],
    allowed_refs: List[str],
) -> bool:
    """Constant-time comparison of client_ref against the allowlist."""
    if not client_ref:
        return False
    matched = False
    for ref in allowed_refs:
        if _const_time_eq(client_ref, ref):
            matched = True
    return matched


def _const_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to avoid timing leaks."""
    import hmac as _hmac
    return _hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _matches_cloud_model(
    model_name: Optional[str],
    allowed_models: List[str],
    allowed_prefixes: List[str],
) -> bool:
    """Check if a cloud model name matches the capture allowlist.

    First checks exact matches against ``allowed_models``, then checks
    namespace prefix matches against ``allowed_prefixes``.

    Returns False when model_name is None.
    """
    if not model_name:
        return False
    if model_name in allowed_models:
        return True
    for prefix in allowed_prefixes:
        if model_name.startswith(prefix):
            return True
    return False


def evaluate_capture_policy(
    config: CaptureConfig,
    *,
    route_type: str,
    endpoint: str,
    ingress_protocol: str,
    requested_model: Optional[str],
    client_ref: Optional[str],
) -> PolicyResult:
    """Evaluate the capture policy for a single request.

    Parameters
    ----------
    config : CaptureConfig
        The active capture configuration.
    route_type : str
        ``"local"`` or ``"cloud"``.
    endpoint : str
        The request path (e.g. ``/v1/chat/completions``).
    ingress_protocol : str
        ``"openai"``, ``"anthropic"``, or ``"ollama"``.
    requested_model : str or None
        The model name requested by the client (before resolution).
    client_ref : str or None
        The HMAC-SHA-256 client reference computed from the key fingerprint.

    Returns
    -------
    PolicyResult
        The capture decision.  ``should_capture=False`` is always the result
        when the global switch is off or an error occurs.
    """
    # Always include field policies so the redactor knows what to do, even
    # when capture is denied (for debug logging).
    field_policies = {
        "system_prompts": config.system_prompts,
        "reasoning": config.reasoning,
        "tool_definitions": config.tool_definitions,
        "tool_calls": config.tool_calls,
        "tool_results": config.tool_results,
        "images": config.images,
        "unknown_content_blocks": config.unknown_content_blocks,
    }

    try:
        # 1. Global kill switch
        if not config.enabled:
            return PolicyResult(
                should_capture=False,
                reason="disabled",
                detail="Global capture switch is disabled",
                policy_version=config.policy_version,
                field_policies=field_policies,
            )

        # 2. Endpoint exclusion — admin/health/metrics endpoints never captured
        if config.is_endpoint_excluded(endpoint):
            return PolicyResult(
                should_capture=False,
                reason="endpoint_excluded",
                detail=f"Endpoint '{endpoint}' is in the excluded list",
                policy_version=config.policy_version,
                field_policies=field_policies,
            )

        # 3. Route-type switch
        if not config.should_capture_route(route_type):
            route_label = "local" if route_type == "local" else "cloud"
            return PolicyResult(
                should_capture=False,
                reason="route_type_disabled",
                detail=f"Capture for {route_label} routes is disabled",
                policy_version=config.policy_version,
                field_policies=field_policies,
            )

        # 4. Per-client auth check — always required for an authenticated client_ref.
        # When per_client_opt_in is True, only explicitly-allowed clients are captured.
        # When per_client_opt_in is False, all authenticated clients are captured.
        # In both cases, an unauthenticated request (no client_ref) is never captured.
        if not config.should_capture_client(client_ref):
            if not config.per_client_opt_in and client_ref is None:
                return PolicyResult(
                    should_capture=False,
                    reason="unauthenticated",
                    detail="No client_ref — request cannot be authenticated for capture",
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )
            if config.per_client_opt_in and client_ref is None:
                return PolicyResult(
                    should_capture=False,
                    reason="unauthenticated",
                    detail="No client_ref — request cannot be authenticated for capture",
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )
            # Client is authenticated but not in the allowlist
            if config.per_client_opt_in:
                return PolicyResult(
                    should_capture=False,
                    reason="client_not_opted_in",
                    detail=f"Client {client_ref[:8]}… is not in the capture allowlist",
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )

        # 5. Ingress protocol gate — OpenAI, Anthropic, and Ollama chat are supported
        supported_protocols = ("openai", "anthropic", "ollama")
        if ingress_protocol not in supported_protocols:
            return PolicyResult(
                should_capture=False,
                reason="protocol_not_supported",
                detail=f"Protocol '{ingress_protocol}' is not in supported set {supported_protocols}",
                policy_version=config.policy_version,
                field_policies=field_policies,
            )

        # 6. Endpoint must be a chat completions endpoint for the given protocol
        if ingress_protocol == "anthropic":
            if endpoint not in ("/v1/messages",):
                return PolicyResult(
                    should_capture=False,
                    reason="endpoint_not_supported",
                    detail=f"Endpoint '{endpoint}' is not in the capture allowlist for Anthropic protocol",
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )
        elif ingress_protocol == "ollama":
            if endpoint not in ("/api/chat", "/api/generate"):
                return PolicyResult(
                    should_capture=False,
                    reason="endpoint_not_supported",
                    detail=f"Endpoint '{endpoint}' is not in the capture allowlist for Ollama protocol",
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )
        else:
            # OpenAI
            if endpoint not in ("/v1/chat/completions",):
                return PolicyResult(
                    should_capture=False,
                    reason="endpoint_not_supported",
                    detail=f"Endpoint '{endpoint}' is not in the capture allowlist for this slice",
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )

        # 7. Cloud model allowlist — when cloud routes are enabled and the
        # allowlist is active, only explicitly-listed models or models
        # under an allowed namespace prefix are captured.
        if route_type == "cloud" and config.cloud_allowlist_enabled:
            if not _matches_cloud_model(requested_model, config.allowed_cloud_models, config.cloud_model_prefixes):
                return PolicyResult(
                    should_capture=False,
                    reason="cloud_model_not_in_allowlist",
                    detail=(
                        f"Cloud model '{requested_model}' is not in the capture allowlist "
                        f"(allowlist_enabled={config.cloud_allowlist_enabled}, "
                        f"explicit={len(config.allowed_cloud_models)}, "
                        f"prefixes={len(config.cloud_model_prefixes)})"
                    ),
                    policy_version=config.policy_version,
                    field_policies=field_policies,
                )

        # All checks passed
        return PolicyResult(
            should_capture=True,
            reason="allowed",
            detail="Request meets all capture policy criteria",
            policy_version=config.policy_version,
            field_policies=field_policies,
        )

    except Exception as exc:
        # Fail-open: any error in policy evaluation means "do not capture",
        # never block inference.
        logger.warning("Capture policy evaluation error (fail-open: not capturing): %s", exc)
        return PolicyResult(
            should_capture=False,
            reason="policy_error",
            detail=f"Policy evaluation error: {exc}",
            policy_version=config.policy_version,
            field_policies=field_policies,
        )
