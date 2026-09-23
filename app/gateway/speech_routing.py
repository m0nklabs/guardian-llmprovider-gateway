"""Route-oriented speech model resolution (2026-09-23, operator mandate).

A speech ``model`` field is an ADDRESS, not a capability request:

    [guardian/]{provider}/{brand}/{model}

The provider file alone decides how the address resolves — there are no
opt-in or on/off switches anywhere in the speech path:

- ``tts_url`` / ``stt_url`` (+ ``management_url``) declared  -> the provider's
  LOCAL engine (caretaker-managed on-demand lifecycle);
- ``base_url`` + ``api_key`` declared                        -> the provider's
  OpenAI-compatible cloud endpoint;
- both declared                                              -> the local engine
  (LAN-first);
- neither, or an unknown provider                            -> the caller
  raises ``404 model_not_served`` (the chat routing contract).

The upstream model id is EVERYTHING after the provider segment, verbatim —
the provider's real model id, bare (``guardian/groq/whisper-large-v3`` ->
``whisper-large-v3``) or namespaced (``guardian/groq/canopylabs/orpheus-v1-english``
-> ``canopylabs/orpheus-v1-english``, ``guardian/openrouter/x-ai/grok-stt-1.0``
-> ``x-ai/grok-stt-1.0``), exactly the chat routing resolution
(``resolve_cloud_target`` accepts the same shape). The optional ``guardian/``
prefix is the operator's canonical speech form; the bare chat-style form
resolves identically.

An explicit address is EXACT: a failure surfaces honestly for that route and
never silently falls back to a different provider. The default path (no
addressable ``model`` field) keeps each modality's local failover chain.
"""

from __future__ import annotations

from typing import Any

from app.config_loader import CONFIG
from app.proxy.providers import _expand_env


def parse_speech_model(model: str) -> tuple[str, str] | None:
    """Split a speech address into ``(provider_name, upstream_model_id)``.

    Accepts the optional ``guardian/`` prefix and the bare chat-style form.
    Returns ``None`` when the value is not an address (empty or fewer than
    three segments), so the request falls to the default chain unchanged.
    """
    if not model or not model.strip():
        return None
    parts = [p.strip() for p in model.strip().split("/")]
    if parts and parts[0].lower() == "guardian":
        parts = parts[1:]
    if len(parts) < 2 or not parts[0] or not all(parts[1:]):
        return None
    return parts[0].lower(), "/".join(parts[1:])


def address_intent(model: str) -> bool:
    """True when the value INTENDS to be an address: it carries the
    ``guardian/`` prefix (malformed forms must 404, not fall through) or has
    enough segments to be a bare chat-style address."""
    if not model or not model.strip():
        return False
    parts = [p.strip() for p in model.strip().split("/") if p.strip()]
    if parts and parts[0].lower() == "guardian":
        return True
    return len(parts) >= 2


def resolve_speech_route(model: str, local_capability: str) -> dict[str, Any] | None:
    """Resolve a speech address to a route from the provider file alone.

    ``local_capability`` is ``"tts_url"`` or ``"stt_url"``. Returns:

    - ``{"kind": "local", "provider", "upstream_model", "<capability>",
      "management_url", "management_key"}`` when the provider declares its
      local engine;
    - ``{"kind": "cloud", "provider", "upstream_model", "base_url",
      "api_key"}`` when it declares the cloud credentials;
    - ``None`` when the provider is unknown or declares neither (caller:
      ``404 model_not_served``).
    """
    parsed = parse_speech_model(model)
    if parsed is None:
        return None
    provider_name, upstream_model = parsed
    doc = (CONFIG.get("providers") or {}).get(provider_name)
    if not isinstance(doc, dict):
        return None
    local_url = _expand_env(str(doc.get(local_capability) or "")).rstrip("/")
    mgmt_url = _expand_env(str(doc.get("management_url") or "")).rstrip("/")
    if local_url and mgmt_url:
        return {
            "kind": "local",
            "provider": provider_name,
            "upstream_model": upstream_model,
            local_capability: local_url,
            "management_url": mgmt_url,
            "management_key": _expand_env(str(doc.get("management_key") or doc.get("api_key") or "")),
        }
    base_url = _expand_env(str(doc.get("base_url") or "")).rstrip("/")
    api_key = _expand_env(str(doc.get("api_key") or ""))
    if base_url and api_key:
        return {
            "kind": "cloud",
            "provider": provider_name,
            "upstream_model": upstream_model,
            "base_url": base_url,
            "api_key": api_key,
        }
    return None
