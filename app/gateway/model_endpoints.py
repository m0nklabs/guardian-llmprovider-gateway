"""Per-model endpoint listing — ``GET /v1/models/{model_id}/endpoints``.

Implements section 4 of ``docs/OPENROUTER_PARITY.md``: the OpenRouter-shaped
mirror of ``/api/v1/models/{author}/{slug}/endpoints``.

Guardian's ``endpoints`` are the **concrete routes Guardian can actually use**
for that model — the honest analogue of OpenRouter's upstream provider list:

* a **cloud model** yields one endpoint per enabled and configured provider
  whose own catalog genuinely contains the same identity key, plus the local
  backend when a local provider serves it;
* a **``failover/{group}``** address yields one endpoint per configured
  candidate of that group;
* a **local model** yields its local backend endpoint.

Like :mod:`app.gateway.model_discovery`, this module is dependency-injected:
the route decorator stays in ``app.proxy.server`` and ``init()`` is called once
at startup.  Collaborators that the frozen ``init`` signature does not carry
(the metadata-presentation module, a per-route health reader and the auth-context
reader) are resolved defensively and can be attached explicitly with
:func:`attach` — see that function's docstring.

Honesty rules (non-negotiable, §4):

* ``uptime_last_30m`` / ``uptime_last_5m`` / ``uptime_last_1d`` /
  ``latency_last_30m`` / ``throughput_last_30m`` are always ``null``: nothing in
  this codebase records per-provider time series.
* ``native_tools`` is ``null`` unless the provider object declares one; no
  provider config in this repository does.
* ``pricing`` is never borrowed from another provider: a cloud route with no
  advertised pricing of its own (and no reference entry for the same identity
  key) reports ``null``.

Decisions worth knowing when reading the output (§4 does not pin them):

* ``model_id`` on an endpoint is the id that *that route* sends upstream
  (``models/gemini-2.5-flash`` for google, ``openai/gpt-4o`` for openrouter,
  the configured candidate model for a failover route, the canonical local model
  name for a local route) — Guardian's ``endpoints`` are routes, not aliases of
  the top-level model id, and every endpoint also carries the model's display
  name in ``model_name``.
* ``tag`` is the provider name, and ``{provider}/{group}`` for failover
  candidates so the group name is visible on the route.
* ``supports_tool_choice`` is derived from the route advertising ``tools``; the
  individual ``tool_choice`` modes are not advertised by any upstream, so a
  tool-capable route reports all four modes as supported.
* the response ``data`` object carries §4's five model keys plus ``endpoints``
  and, when the presentation layer reports provenance, the §5
  ``metadata_sources`` map (additive, §6).
"""

from __future__ import annotations

import importlib
import inspect
import logging
import sys
import time
from typing import Any, Callable

from fastapi import HTTPException, Request

from app.log_safety import clean_log_value

logger = logging.getLogger("Guardian")

#: Frozen per-endpoint key list (``docs/OPENROUTER_PARITY.md`` §4).  Every key is
#: always present on every endpoint; a value Guardian cannot ground is ``None``.
ENDPOINT_KEYS: tuple[str, ...] = (
    "name",
    "model_id",
    "model_name",
    "context_length",
    "pricing",
    "provider_name",
    "tag",
    "quantization",
    "max_completion_tokens",
    "max_prompt_tokens",
    "supported_parameters",
    "supports_tool_choice",
    "native_tools",
    "supports_implicit_caching",
    "supports_image_reference",
    "status",
    "uptime_last_30m",
    "uptime_last_5m",
    "uptime_last_1d",
    "latency_last_30m",
    "throughput_last_30m",
)

#: Top-level ``data`` keys of the response (§4), ``endpoints`` excluded.
MODEL_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "created",
    "description",
    "architecture",
)

#: OpenRouter-style route status codes.  ``None`` means "Guardian has no health
#: signal for this route" and is the only honest answer when nothing tracks it.
STATUS_HEALTHY = 0
STATUS_DEGRADED = 1  # failover circuit breaker tripped for (provider, model)
STATUS_RATE_LIMITED = 2  # provider answered 429 and is in its cooldown window

#: Pricing keys a local model reports as ``"0"`` (§3.2).  Local inference costs
#: nothing — a fact, not a guess — and this is the only price Guardian states
#: without having received it from an upstream.
LOCAL_PRICING_KEYS: tuple[str, ...] = (
    "prompt",
    "completion",
    "web_search",
    "input_cache_read",
    "input_cache_write",
    "input_cache_write_1h",
)

#: Metadata-presentation modules (the frozen interface of the parity feature).
#: Imported lazily and defensively — see :func:`_presentation_candidates`.
_PRESENTATION_MODULE_NAMES: tuple[str, ...] = (
    "app.gateway.model_metadata_presentation",
    "app.gateway.metadata_enrichment",
)

# ── Injected (set once at startup by init()) ─────────────────────────
_provider_registry = None
_cloud_catalog = None
_reference_catalog = None
_failover_registry = None
_model_manager = None
_resolve_cloud_attempts = None
_resolve_context_window = None

# ── Optional collaborators (see attach()) ────────────────────────────
_attached_presentation: Any = None
_presentation_modules: tuple[Any, ...] | None = None
_route_health: Callable[[str, str], Any] | None = None
_auth_context_reader: Callable[[Request], Any] | None = None
_enrichment_modules: tuple[Any, ...] | None = None


def init(
    *,
    _provider_registry,
    _cloud_catalog,
    _reference_catalog,
    _failover_registry,
    _model_manager,
    _resolve_cloud_attempts,
    _resolve_context_window,
) -> None:
    """Inject dependencies once at startup. Mirror app/gateway/model_discovery.init."""
    globals()["_provider_registry"] = _provider_registry
    globals()["_cloud_catalog"] = _cloud_catalog
    globals()["_reference_catalog"] = _reference_catalog
    globals()["_failover_registry"] = _failover_registry
    globals()["_model_manager"] = _model_manager
    globals()["_resolve_cloud_attempts"] = _resolve_cloud_attempts
    globals()["_resolve_context_window"] = _resolve_context_window


def attach(
    *,
    presentation=None,
    route_health: Callable[[str, str], Any] | None = None,
    auth_context_reader: Callable[[Request], Any] | None = None,
) -> None:
    """Optionally attach live collaborators that are not part of ``init()``.

    Only the arguments that are not ``None`` are applied.

    ``presentation``
        The metadata-presentation module (or an object standing in for it).  It
        is found automatically by import when the frozen module
        ``app.gateway.model_metadata_presentation`` is present; attaching one
        explicitly is for tests and for wiring without an import.

    ``route_health``
        Per-route health reader ``(provider_name, model_id) -> int | None`` in
        OpenRouter status semantics (``0`` healthy, non-zero degraded, ``None``
        unknown).  :func:`failover_route_health` builds one from the live
        :class:`~app.proxy.failover.ProviderHealthTracker`.  Without a reader
        this module still reports a non-zero status for a provider whose own
        catalog fetch failed with 401/403 (``CloudModelCatalog.is_auth_error``)
        and ``None`` everywhere else — it never invents "healthy".

    ``auth_context_reader``
        The injected ``get_request_auth_context`` equivalent used for cloud
        access gating.  Without it this module uses the sibling route's helper
        (``app.gateway.model_discovery._get_request_auth_context``) when it is
        wired, then falls back to reading the authenticated request context
        directly.
    """
    global _attached_presentation, _presentation_modules
    global _route_health, _auth_context_reader
    if presentation is not None:
        _attached_presentation = presentation
        _presentation_modules = None
    if route_health is not None:
        _route_health = route_health
    if auth_context_reader is not None:
        _auth_context_reader = auth_context_reader


def failover_route_health(health_tracker: Any) -> Callable[[str, str], int | None]:
    """Build a route-health reader from a ``ProviderHealthTracker``.

    The tracker is the only per-route health state Guardian keeps: its circuit
    breaker (``is_tripped``) and its 429 cooldown (``is_rate_limited``), both
    keyed on ``(provider, model)``.  It only knows routes that actually went
    through the cloud/failover forwarding path, and it deliberately reports
    ``0`` for a route it has no tripped/rate-limited entry for — that is the
    tracker's own statement that the route is not currently degraded, paired
    with the route having been discovered as enabled + configured.
    """

    def reader(provider_name: str, model_id: str) -> int | None:
        if not provider_name or not model_id:
            return None
        try:
            if health_tracker.is_tripped(provider_name, model_id):
                return STATUS_DEGRADED
            if health_tracker.is_rate_limited(provider_name, model_id):
                return STATUS_RATE_LIMITED
        except Exception as exc:  # fail-open: discovery never breaks on health
            logger.debug(
                "Health lookup failed for %s/%s: %s",
                clean_log_value(provider_name),
                clean_log_value(model_id),
                clean_log_value(exc),
            )
            return None
        return STATUS_HEALTHY

    return reader


# ── Small helpers ────────────────────────────────────────────────────


async def _maybe_await(value: Any) -> Any:
    """Await *value* when it is awaitable (injected callables may be either)."""
    if inspect.isawaitable(value):
        return await value
    return value


def _as_int(value: Any) -> int | None:
    """Return *value* as an ``int`` (bools excluded), or ``None``."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_positive_int(value: Any) -> int | None:
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _as_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    cleaned = [item for item in value if isinstance(item, str) and item.strip()]
    return cleaned or None


def _as_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) and value else None


def _safe_call(label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any | None:
    """Call *fn* and swallow any failure (fail-open discovery)."""
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("%s failed: %s", label, clean_log_value(exc))
        return None


def _provider_object(name: str | None):
    """Return the configured provider object for *name*, or ``None``."""
    if not name or _provider_registry is None:
        return None
    providers = getattr(_provider_registry, "_providers", None)
    if isinstance(providers, dict):
        return providers.get(name)
    return None


def _enabled_providers() -> list[Any]:
    getter = getattr(_provider_registry, "get_enabled_providers", None)
    providers = _safe_call("Provider enumeration", getter)
    return list(providers) if isinstance(providers, list) else []


# ── Metadata presentation layer (optional module) ────────────────────


def _presentation_candidates() -> tuple[Any, ...]:
    """Return the presentation modules, importing them lazily.

    ``app.gateway.model_metadata_presentation`` is the frozen interface; the
    import is guarded so this module stays importable while that file is not on
    disk yet (it is written by another track), and so a broken optional helper
    can never take the endpoint route down.  ``metadata_enrichment`` is included
    because it re-exports the same two functions and is the sibling that already
    consumes them.
    """
    global _presentation_modules
    if _presentation_modules is None:
        found: list[Any] = []
        if _attached_presentation is not None:
            found.append(_attached_presentation)
        for module_name in _PRESENTATION_MODULE_NAMES:
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                logger.debug(
                    "Metadata presentation module %s unavailable: %s",
                    module_name,
                    clean_log_value(exc),
                )
                continue
            if module not in found:
                found.append(module)
        _presentation_modules = tuple(found)
    return _presentation_modules


def _presentation_fn(name: str) -> Callable[..., Any] | None:
    for module in _presentation_candidates():
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _enrichment_module() -> Any | None:
    """Return ``app.gateway.metadata_enrichment`` when it imports cleanly."""
    global _enrichment_modules
    if _enrichment_modules is None:
        try:
            module = importlib.import_module("app.gateway.metadata_enrichment")
        except Exception as exc:
            logger.debug("metadata_enrichment unavailable: %s", clean_log_value(exc))
            module = None
        _enrichment_modules = (module,) if module is not None else ()
    return _enrichment_modules[0] if _enrichment_modules else None


def _split_identity(model_id: str) -> str:
    """Return the identity key of *model_id* (§1): everything after the provider.

    The leading ``{provider}`` segment is dropped **only when it actually names a
    configured, enabled, non-local provider**.  That guard matters on this
    surface: a client may ask for the bare address ``openai/gpt-4o``, which is
    already the identity key every provider's catalog is keyed on
    (``openai/gpt-4o``), while ``openrouter/openai/gpt-4o`` is the same model
    addressed through one provider.  Stripping the first segment unconditionally
    would turn the bare form into ``gpt-4o`` and lose the cross-provider join.
    A local model name without a provider segment is its own identity key.
    """
    if not model_id:
        return ""
    parts = model_id.split("/")
    if len(parts) >= 3 and parts[0]:
        provider = _safe_call(
            "Address provider lookup",
            getattr(_provider_registry, "_provider_from_address", None),
            model_id,
        )
        provider_known = provider is not None and not bool(getattr(provider, "managed", False))
        if provider_known or _provider_registry is None:
            fn = _presentation_fn("split_identity")
            if fn is not None:
                identity = _safe_call("split_identity", fn, model_id)
                if isinstance(identity, str) and identity.strip():
                    return identity.strip()
            return "/".join(parts[1:])
    return model_id


def _derived_name(identity: str) -> str:
    """Derive a display name from the identity key (§3: upstream → … → derived)."""
    brand, sep, model = (identity or "").partition("/")
    if sep and brand and model:
        return f"{brand.title()}: {model}"
    return identity or "unknown"


def _empty_architecture() -> dict[str, Any]:
    return {
        "modality": None,
        "input_modalities": None,
        "output_modalities": None,
        "tokenizer": None,
        "instruct_type": None,
    }


def _fallback_parity_fields(model_id: str) -> dict[str, Any]:
    """Minimal §3 field set for when the presentation module is unavailable.

    Only facts Guardian holds without it: the address, a name derived from the
    identity key (spec §3 precedence), the request time, and nulls elsewhere.
    """
    identity = _split_identity(model_id)
    return {
        "id": model_id,
        "name": _derived_name(identity),
        "created": int(time.time()),
        "description": None,
        "architecture": _empty_architecture(),
    }


def _resolve_model_metadata(
    model_id: str,
    *,
    upstream: dict[str, Any] | None,
    reference: dict[str, Any] | None,
    reference_source: str | None,
    overrides: dict[str, Any] | None,
    local: dict[str, Any] | None,
    context_length: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(parity_fields, metadata_sources)`` from the presentation layer.

    Falls back to a minimal, honest field set when the presentation module is
    not installed or raises; the parity merge is never allowed to break the
    endpoint route.
    """
    fn = _presentation_fn("resolve_model_metadata")
    if fn is None:
        return _fallback_parity_fields(model_id), {}
    try:
        parity, sources = fn(
            model_id,
            upstream=upstream,
            reference=reference,
            reference_source=reference_source,
            overrides=overrides,
            local=local,
            context_length=context_length,
            # ``created`` is deliberately absent: §4 resolves it
            # ``upstream -> reference -> request time``, and the presentation
            # layer gives a caller-supplied value the highest priority. Passing
            # the request time here buried the upstream timestamp forever.
        )
    except Exception as exc:
        logger.warning(
            "⚠️  Model metadata resolution failed for %s: %s",
            clean_log_value(model_id),
            clean_log_value(exc),
        )
        return _fallback_parity_fields(model_id), {}
    parity_fields = dict(parity) if isinstance(parity, dict) else {}
    metadata_sources = dict(sources) if isinstance(sources, dict) else {}
    for key, value in _fallback_parity_fields(model_id).items():
        parity_fields.setdefault(key, value)
    return parity_fields, metadata_sources


def _present_endpoint(fields: dict[str, Any]) -> dict[str, Any]:
    """Complete one endpoint dict, delegating the §4 key list to the helper.

    ``model_metadata_presentation.endpoint_entry_fields`` owns the §4 shape; when
    it is available it is called with the field names as keyword arguments, so a
    differently-shaped implementation degrades to the locally built dict instead
    of failing the route.  Its result is always completed from the locally built
    values, so every §4 key is present even for a partial helper result.
    """
    fn = _presentation_fn("endpoint_entry_fields")
    if fn is None:
        return fields
    try:
        presented = fn(**fields)
    except TypeError:
        logger.debug(
            "endpoint_entry_fields() does not accept the §4 keyword set; "
            "using the locally built endpoint fields"
        )
        return fields
    except Exception as exc:
        logger.warning("⚠️  endpoint_entry_fields() failed: %s", clean_log_value(exc))
        return fields
    if not isinstance(presented, dict):
        return fields
    merged = {**fields, **presented}
    for key in ENDPOINT_KEYS:
        merged.setdefault(key, None)
    return merged


# ── Cloud / reference / catalog accessors ────────────────────────────


def _catalog_models(provider_name: str) -> dict[str, str]:
    """Return the provider's advertised ``{normalized_id: upstream_id}`` map.

    ``CloudModelCatalog.get_models_for_provider`` applies the provider's
    ``catalog_allowlist``, so a model the provider cannot really reach is
    invisible here — and therefore never advertised as an endpoint.
    """
    getter = getattr(_cloud_catalog, "get_models_for_provider", None)
    models = _safe_call("Catalog lookup", getter, provider_name)
    return dict(models) if isinstance(models, dict) else {}


def _catalog_metadata(provider_name: str, identity: str) -> dict[str, Any] | None:
    """Return the route's own upstream metadata (§3 keys), or ``None``."""
    getter = getattr(_cloud_catalog, "get_model_metadata", None)
    metadata = _safe_call("Catalog metadata lookup", getter, provider_name, identity)
    return _as_dict(metadata)


def _catalog_overrides(identity: str) -> dict[str, Any] | None:
    getter = getattr(_cloud_catalog, "get_model_overrides", None)
    overrides = _safe_call("Override lookup", getter, identity)
    return _as_dict(overrides)


def _catalog_context(provider_name: str, identity: str) -> int | None:
    getter = getattr(_cloud_catalog, "get_context_window", None)
    return _as_positive_int(_safe_call("Context lookup", getter, provider_name, identity))


def _provider_auth_error(provider_name: str) -> bool:
    """True when the provider's last catalog fetch failed with 401/403."""
    getter = getattr(_cloud_catalog, "is_auth_error", None)
    return bool(_safe_call("Auth-error lookup", getter, provider_name))


def _safe_reference(identity: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read the cross-provider reference entry for *identity*, fail-open."""
    if _reference_catalog is None:
        return None, None
    metadata = _safe_call(
        "Reference metadata lookup", getattr(_reference_catalog, "metadata", None), identity
    )
    source = _safe_call(
        "Reference source lookup", getattr(_reference_catalog, "source_name", None), identity
    )
    return _as_dict(metadata), _as_str(source)


def _route_context_length(
    provider_name: str | None,
    identity: str,
    reference: dict[str, Any] | None,
    model_context: int | None,
) -> int | None:
    """Resolve the context window for one route.

    Precedence mirrors §3: operator override, the route's own upstream
    advertisement, the reference entry for the same identity key, then the value
    Guardian resolved for the model itself.
    """
    override = _safe_call(
        "Context override lookup",
        getattr(_provider_registry, "get_context_override", None),
        identity,
    )
    override_value = _as_positive_int(override)
    if override_value is not None:
        return override_value
    if provider_name:
        upstream_value = _catalog_context(provider_name, identity)
        if upstream_value is not None:
            return upstream_value
    reference_value = _as_positive_int(reference.get("context_length")) if reference else None
    if reference_value is not None:
        return reference_value
    return model_context


def _top_provider_value(
    sources: list[dict[str, Any] | None], key: str
) -> Any:
    """First non-null ``top_provider.<key>`` across *sources* (§3.3)."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        top_provider = source.get("top_provider")
        if not isinstance(top_provider, dict):
            continue
        value = top_provider.get(key)
        if isinstance(value, bool):
            continue
        if value is not None:
            return value
    return None


def _supported_parameters(
    sources: list[dict[str, Any] | None], model_value: Any
) -> list[str] | None:
    """First advertised ``supported_parameters`` list across *sources* (§3.4)."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = _as_str_list(source.get("supported_parameters"))
        if value:
            return value
    return _as_str_list(model_value)


def _supports_tool_choice(parameters: list[str] | None) -> dict[str, bool] | None:
    """Derive OpenRouter's ``supports_tool_choice`` from advertised tool support.

    Guardian only knows *whether* a route advertises tools; it does not
    enumerate the ``tool_choice`` modes, so a tool-capable route reports the full
    OpenRouter mode set and a route that advertises parameters without tools
    reports ``none`` only.  No advertised parameters at all means no signal →
    ``None``.
    """
    if parameters is None:
        return None
    if "tools" in parameters:
        return {"none": True, "auto": True, "required": True, "function": True}
    return {"none": True, "auto": False, "required": False, "function": False}


def _route_health_reader() -> Callable[[str, str], Any] | None:
    """Return the per-route health reader, if Guardian has one.

    Precedence: an explicitly attached reader (:func:`attach`) wins; otherwise the
    live ``ProviderHealthTracker`` of the running application is used.  That
    tracker is only reachable through ``app.proxy.server`` (the parent builds it
    as ``failover_health`` and the frozen ``init`` signature does not carry it),
    so it is looked up in ``sys.modules`` — deliberately *without* importing the
    proxy server from this module, which keeps request handling free of a heavy
    import and keeps this module testable in isolation.
    """
    if _route_health is not None:
        return _route_health
    server_module = sys.modules.get("app.proxy.server")
    tracker = getattr(server_module, "failover_health", None) if server_module is not None else None
    if tracker is None:
        return None
    return failover_route_health(tracker)


def _route_status(provider_name: str | None, model_id: str | None, *, health_governed: bool) -> int | None:
    """Return the OpenRouter status code for one route, or ``None``.

    ``health_governed`` is ``True`` for routes that go through the cloud /
    failover forwarding path, which is what the health tracker records.  A local
    backend route is never reported as healthy on the strength of a tracker that
    has no entry for it.
    """
    if not health_governed or not provider_name or not model_id:
        return None
    reader = _route_health_reader()
    if reader is not None:
        value = _as_int(_safe_call("Route health lookup", reader, provider_name, model_id))
        if value == STATUS_HEALTHY:
            provider = _provider_object(provider_name)
            # Failover lists every candidate, but an untripped tracker alone
            # cannot establish that an absent or unusable provider is healthy.
            if (
                provider is None
                or not bool(getattr(provider, "enabled", False))
                or not bool(getattr(provider, "is_configured", False))
            ):
                return None
        return value
    # No tracker at all: the only positive degradation signal left is a provider
    # whose own catalog fetch was rejected (401/403).
    if _provider_auth_error(provider_name):
        return STATUS_DEGRADED
    return None


def _local_pricing(model_pricing: Any) -> dict[str, Any]:
    """Pricing for a local route: the §3.2 grounded zeros (+ ``local``).

    Only the local zero block is reused from the model metadata; a cloud
    passthrough price is never inherited by a local route (§3.2 forbids borrowing
    a provider's price for a route that does not serve it).
    """
    if isinstance(model_pricing, dict) and model_pricing.get("local") == "true":
        return dict(model_pricing)
    pricing: dict[str, Any] = {key: "0" for key in LOCAL_PRICING_KEYS}
    pricing["local"] = "true"
    return pricing


def _local_config(canonical_name: str | None) -> dict[str, Any]:
    """Return the local model config block, or ``{}``."""
    if not canonical_name or _model_manager is None:
        return {}
    models = getattr(_model_manager, "models", None)
    if not isinstance(models, dict):
        return {}
    config = models.get(canonical_name)
    return dict(config) if isinstance(config, dict) else {}


def _fallback_local_facts(canonical_name: str | None) -> dict[str, Any]:
    """Grounded local facts when ``metadata_enrichment`` is unavailable.

    Mirrors ``app.gateway.metadata_enrichment.local_model_facts``: only real
    declarations are reported, everything unknown stays absent so the
    presentation layer reports ``None``.
    """
    facts: dict[str, Any] = {"served_by": "local"}
    config = _local_config(canonical_name)
    model_type = _as_str(config.get("model_type"))
    if model_type:
        facts["model_type"] = model_type
    else:
        # Mirror ``metadata_enrichment.local_model_facts``: a model launched with
        # llama-server's ``--embedding`` flag is an embedding model even when the
        # config omits ``model_type``, and without it the presentation layer
        # would default the modality to ``text->text``.
        extra_args = config.get("extra_args")
        if isinstance(extra_args, str) and "--embedding" in extra_args.split():
            facts["model_type"] = "embedding"
    if isinstance(config.get("grammar_decoding"), bool):
        facts["grammar_decoding"] = config["grammar_decoding"]
    # ``tool_profile`` is the config's declared tool-support switch; the
    # presentation layer consumes it as ``tool_support`` and advertises
    # tools/tool_choice only on that key. Emitting the config name here meant a
    # local model that declares tool support silently lost the capability when
    # this fallback ran.
    if isinstance(config.get("tool_profile"), bool):
        facts["tool_support"] = config["tool_profile"]
    max_tokens = _as_positive_int(config.get("max_tokens"))
    if max_tokens is not None:
        facts["max_tokens"] = max_tokens
    if _model_manager is not None:
        vision = _safe_call(
            "Vision capability lookup",
            getattr(_model_manager, "get_vision_capability", None),
            canonical_name,
        )
        if vision is not None:
            facts["vision"] = vision
    return facts


def _local_facts(canonical_name: str | None) -> dict[str, Any]:
    """Local model facts, preferring the sibling module's collector."""
    module = _enrichment_module()
    collector = getattr(module, "local_model_facts", None) if module is not None else None
    if callable(collector):
        facts = _safe_call("Local model facts", collector, _model_manager, canonical_name)
        if isinstance(facts, dict) and facts:
            return dict(facts)
    return _fallback_local_facts(canonical_name)


def _local_quantization(canonical_name: str | None) -> str | None:
    """Return a quantization *declared in the local model config*, else ``None``.

    No model in this repository declares one — the quant is only part of the
    GGUF filename, which is not a grounded declaration — so this reports
    ``None`` today and will report the configured value if a config adds it.
    """
    config = _local_config(canonical_name)
    for key in ("quantization", "quantization_level", "quant"):
        value = _as_str(config.get(key))
        if value:
            return value
    return None


# ── Access gating ────────────────────────────────────────────────────


def _inline_auth_context(request: Request) -> dict[str, Any] | None:
    """Read the authenticated request context the way ``app.proxy.auth`` stores it."""
    state = getattr(request, "state", None)
    auth_context = getattr(state, "auth_context", None)
    if isinstance(auth_context, dict):
        return auth_context
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        candidate = scope.get("guardian_auth_context")
        if isinstance(candidate, dict):
            return candidate
    return None


def _sibling_auth_context_reader() -> Callable[[Request], Any] | None:
    """Use ``model_discovery``'s injected auth-context helper when it is wired.

    ``init()``'s signature is frozen and does not carry
    ``_get_request_auth_context``, and ``app/gateway/model_discovery.py`` is owned
    by another track, so the sibling's injected helper is read lazily instead of
    duplicated or re-injected.
    """
    try:
        from app.gateway import model_discovery
    except Exception as exc:  # pragma: no cover - import failure is not fatal
        logger.debug("Sibling model_discovery unavailable: %s", clean_log_value(exc))
        return None
    reader = getattr(model_discovery, "_get_request_auth_context", None)
    return reader if callable(reader) else None


def _auth_context(request: Request) -> dict[str, Any] | None:
    for reader in (_auth_context_reader, _sibling_auth_context_reader()):
        if reader is None:
            continue
        context = _safe_call("Auth-context lookup", reader, request)
        if isinstance(context, dict):
            return context
    return _inline_auth_context(request)


def _key_can_access_cloud(request: Request, client_id: str) -> bool:
    """Return whether the requesting key may see / use cloud entries.

    Mirrors ``app.gateway.model_discovery._key_can_access_cloud``: absent
    ``cloud_gateway_access`` means allowed (the redesign default keeps existing
    keys on cloud).
    """
    auth_context = _auth_context(request) or {}
    return bool(auth_context.get("cloud_gateway_access", True))


def _cloud_access_denied() -> HTTPException:
    """The 403 the cloud router raises for a key without cloud access."""
    return HTTPException(status_code=403, detail="cloud access disabled for this Guardian key")


def _is_failover_address(model_name: str) -> bool:
    """Return True when *model_name* is a ``failover/{group}`` address."""
    first, sep, _ = (model_name or "").partition("/")
    return bool(sep and first == "failover")


def _is_cloud_address(model_name: str) -> bool:
    """Whether the provider registry recognises *model_name* as cloud-served.

    Mirrors ``app.gateway.model_discovery.model_metadata``: either the registry
    classifies it as a cloud model, or its first segment names a non-managed
    provider.
    """
    if _safe_call("Cloud-model check", getattr(_provider_registry, "is_cloud_model", None), model_name):
        return True
    provider = _safe_call(
        "Address provider lookup",
        getattr(_provider_registry, "_provider_from_address", None),
        model_name,
    )
    return provider is not None and not bool(getattr(provider, "managed", False))


# ── Route discovery ──────────────────────────────────────────────────


def _cloud_routes(model_id: str, identity: str) -> list[dict[str, Any]]:
    """Return one route per enabled+configured cloud provider serving *identity*.

    A provider only qualifies when its own catalog — with ``catalog_allowlist``
    already applied by ``CloudModelCatalog.get_models_for_provider`` — contains
    the identity key, so a provider that could not actually serve the model is
    never advertised.  When no catalog has any evidence at all (cold start,
    before the first refresh), the provider the registry resolves for the
    address is reported instead: that is the route the cloud router itself would
    use.
    """
    routes: list[dict[str, Any]] = []
    for provider in _enabled_providers():
        if bool(getattr(provider, "managed", False)):
            continue  # local providers are reported by _local_routes()
        name = getattr(provider, "name", None)
        if not name or not bool(getattr(provider, "is_configured", False)):
            continue
        catalog = _catalog_models(name)
        upstream = catalog.get(identity)
        if upstream:
            routes.append({"provider": provider, "model": upstream})
    if routes:
        return routes

    fallback = _safe_call(
        "Provider resolution",
        getattr(_provider_registry, "get_provider_for_model", None),
        model_id,
    )
    if fallback is None or bool(getattr(fallback, "managed", False)):
        return []
    name = getattr(fallback, "name", None)
    if not name or not bool(getattr(fallback, "enabled", True)):
        return []
    if not bool(getattr(fallback, "is_configured", False)):
        return []
    # Allowlist consistency: a filtered-out model stays unadvertised.
    allowlist = getattr(fallback, "catalog_allowlist", None)
    if allowlist and identity not in allowlist:
        return []
    # Cold-start guard: this prefix fallback exists for the window before the
    # provider's own catalog exists. Once that catalog has been fetched or
    # restored from disk — which, with routes empty, means it lacks the
    # identity — the model is genuinely unserved, and a prefix match alone must
    # not fabricate an endpoint for it.
    if _catalog_has_state(name):
        return []
    upstream = _catalog_models(name).get(identity) or identity
    return [{"provider": fallback, "model": upstream}]


def _catalog_has_state(provider_name: str) -> bool:
    """True when *provider_name*'s catalog has been fetched or restored from disk.

    Distinguishes 'not fetched yet' — the window the cold-start fallback exists
    for — from 'fetched and the model is absent', where absence is evidence.
    """
    getter = getattr(_cloud_catalog, "is_provider_catalog_known", None)
    return bool(_safe_call("Catalog state lookup", getter, provider_name))


def _local_registry_provider_name() -> str | None:
    """Name of the provider whose file holds the local model registry."""
    try:
        from app.paths import local_models_file

        filename = local_models_file().name
    except Exception as exc:  # pragma: no cover - path helper is stable
        logger.debug("Local model registry path unavailable: %s", clean_log_value(exc))
        return None
    return _as_str(filename.split(".settings")[0])


def _local_owner_provider(local_providers: list[Any]) -> Any | None:
    """The local provider that owns ``ModelManager``'s model registry."""
    declared = _local_registry_provider_name()
    if declared:
        for provider in local_providers:
            if getattr(provider, "name", None) == declared:
                return provider
    return local_providers[0] if local_providers else None


def _local_routes(model_id: str, identity: str, canonical: str | None) -> list[dict[str, Any]]:
    """Return one route per local (managed) provider serving this model."""
    local_providers = [
        provider
        for provider in _enabled_providers()
        if bool(getattr(provider, "managed", False))
    ]
    if not local_providers:
        return []

    routes: list[dict[str, Any]] = []
    for provider in local_providers:
        name = getattr(provider, "name", None)
        catalog = _catalog_models(name) if name else {}
        upstream = None
        if canonical:
            upstream = catalog.get(canonical)
        if upstream is None and identity:
            upstream = catalog.get(identity)
        if upstream:
            routes.append({"provider": provider, "model": upstream})
    if routes:
        return routes
    if not canonical:
        return []
    # No catalog evidence (cold start): report the provider that owns the model
    # registry, which is the backend Guardian actually loads the model on.
    owner = _local_owner_provider(local_providers)
    return [{"provider": owner, "model": canonical}] if owner is not None else []


# ── Endpoint construction ────────────────────────────────────────────


def _endpoint_fields(
    *,
    provider_name: str | None,
    model_id: str | None,
    model_name: str | None,
    context_length: int | None,
    pricing: dict[str, Any] | None,
    tag: str | None,
    quantization: str | None,
    max_completion_tokens: int | None,
    supported_parameters: list[str] | None,
    status: int | None,
    native_tools: Any = None,
) -> dict[str, Any]:
    """Build the full §4 key set for one route.

    ``None`` is written for every value Guardian cannot ground — including the
    whole uptime/latency/throughput family, which this codebase does not collect
    per provider at all.
    """
    label = f"{provider_name} | {model_id}" if provider_name and model_id else model_id
    fields: dict[str, Any] = {key: None for key in ENDPOINT_KEYS}
    fields.update(
        {
            "name": label,
            "model_id": model_id,
            "model_name": model_name,
            "context_length": context_length,
            "pricing": pricing,
            "provider_name": provider_name,
            "tag": tag,
            "quantization": quantization,
            "max_completion_tokens": max_completion_tokens,
            "max_prompt_tokens": None,  # Guardian collects no prompt-token cap
            "supported_parameters": supported_parameters,
            "supports_tool_choice": _supports_tool_choice(supported_parameters),
            "native_tools": native_tools if isinstance(native_tools, dict) else None,
            "supports_implicit_caching": None,  # no grounded source
            "supports_image_reference": None,  # no grounded source
            "status": status,
            "uptime_last_30m": None,  # no per-provider time series exists
            "uptime_last_5m": None,
            "uptime_last_1d": None,
            "latency_last_30m": None,
            "throughput_last_30m": None,
        }
    )
    return _present_endpoint(fields)


def _build_cloud_endpoint(
    route: dict[str, Any],
    *,
    identity: str,
    model_name: str | None,
    model_context: int | None,
    reference: dict[str, Any] | None,
    tag: str | None = None,
) -> dict[str, Any]:
    """One endpoint for a cloud route (a provider + the model id it expects)."""
    provider = route.get("provider")
    provider_name = getattr(provider, "name", None)
    upstream_model = route.get("model")
    upstream = _catalog_metadata(provider_name, identity) if provider_name else None
    # The operator's per-model override block is applied to every candidate route
    # by the forwarding path, so it is a grounded route-level source (§3.3).
    operator_max_tokens = (_catalog_overrides(identity) or {}).get("max_tokens")

    # §3.2: pricing describes the ROUTE, not the model, so it is never filled
    # from the reference catalog. OpenRouter charges for openai/gpt-4o while
    # NVIDIA serves it free; borrowing would state something false about what
    # this route costs. A route with no price of its own reports null.
    pricing = _as_dict(upstream.get("pricing")) if upstream else None

    return _endpoint_fields(
        provider_name=provider_name,
        model_id=upstream_model,
        model_name=model_name,
        context_length=_route_context_length(provider_name, identity, reference, model_context),
        pricing=pricing,
        tag=tag if tag is not None else provider_name,
        quantization=_as_str(upstream.get("quantization")) if upstream else None,
        max_completion_tokens=_as_positive_int(
            _top_provider_value(
                [
                    {"top_provider": {"max_completion_tokens": operator_max_tokens}},
                    upstream,
                    reference,
                ],
                "max_completion_tokens",
            )
        ),
        # Only this route's own advertisement and the identity-keyed reference
        # entry may fill the route: the model-level value came from one of the
        # other routes (or from the reference) and is never borrowed here.
        supported_parameters=_supported_parameters([upstream, reference], None),
        status=_route_status(provider_name, upstream_model, health_governed=True),
        native_tools=getattr(provider, "native_tools", None),
    )


def _build_local_endpoint(
    route: dict[str, Any],
    *,
    provider_name: str | None,
    model_name: str | None,
    model_context: int | None,
    model_parity: dict[str, Any],
    local_facts: dict[str, Any] | None,
) -> dict[str, Any]:
    """One endpoint for a local backend route (§4: grounded facts only).

    Model-level parity values are reused only when they were resolved *from this
    model's* local facts (``local_facts``); a cloud model that a local provider
    additionally serves gets nulls instead of the cloud route's advertisement.
    """
    canonical = route.get("model")
    grounded = bool(local_facts)
    supported_parameters = (
        _supported_parameters([], model_parity.get("supported_parameters")) if grounded else None
    )
    max_completion_tokens = None
    if grounded:
        max_completion_tokens = _as_positive_int(
            _top_provider_value([model_parity], "max_completion_tokens")
        )
    if max_completion_tokens is None:
        max_completion_tokens = _as_positive_int(_local_config(canonical).get("max_tokens"))
    return _endpoint_fields(
        provider_name=provider_name,
        model_id=canonical,
        model_name=model_name,
        context_length=model_context,
        pricing=_local_pricing(model_parity.get("pricing")),
        tag=provider_name,
        quantization=_local_quantization(canonical),
        max_completion_tokens=max_completion_tokens,
        supported_parameters=supported_parameters,
        # A local backend route is served by Guardian's own lifecycle: the
        # failover health tracker has no entry for it, so no status is claimed.
        status=None,
        native_tools=None,
    )


# ── Response assembly ────────────────────────────────────────────────


def _as_model_data(
    model_id: str,
    parity: dict[str, Any],
    endpoints: list[dict[str, Any]],
    metadata_sources: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the ``data`` object of the §4 response."""
    architecture = _as_dict(parity.get("architecture")) or _empty_architecture()
    resolved_created = _as_int(parity.get("created"))
    data: dict[str, Any] = {
        "id": model_id,
        "name": _as_str(parity.get("name")) or _derived_name(_split_identity(model_id)),
        # An explicit None check, not ``or``: a resolved ``created`` of ``0`` is
        # a real value, not a missing one, and must not become the request time.
        "created": int(time.time()) if resolved_created is None else resolved_created,
        "description": _as_str(parity.get("description")),
        "architecture": architecture,
        "endpoints": endpoints,
    }
    # §5 provenance is additive (§6) and *always present*: an empty map is the
    # statement "nothing was filled in", never an omitted key. The /v1/models
    # path always writes the key (including {}), so omitting it here would let
    # the same model carry {} on one surface and no key on the other.
    data["metadata_sources"] = (
        dict(metadata_sources) if isinstance(metadata_sources, dict) else {}
    )
    return data


async def _resolved_context_window(
    model_id: str,
    canonical: str | None,
    cloud_attempts: list[tuple[Any, str]] | None,
) -> int | None:
    """Resolve the model's context window through the injected resolver."""
    if _resolve_context_window is None:
        return None
    try:
        value = await _maybe_await(_resolve_context_window(model_id, canonical, cloud_attempts))
    except Exception as exc:
        logger.debug(
            "Context resolution failed for %s: %s",
            clean_log_value(model_id),
            clean_log_value(exc),
        )
        return None
    return _as_positive_int(value)


async def _cloud_attempts_for(
    model_id: str, request: Request, client_id: str
) -> list[tuple[Any, str]] | None:
    """Ask the shared cloud router for the model's attempts, fail-soft.

    A 403 means the key may not use cloud at all and is propagated — the endpoint
    surface applies exactly the same authorisation as the model routes.  Any
    other failure is treated as "no attempt evidence" and never hides a route
    that the catalog does show.
    """
    if _resolve_cloud_attempts is None:
        return None
    try:
        result = await _maybe_await(_resolve_cloud_attempts(model_id, request, client_id))
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.debug(
            "Cloud attempt resolution refused for %s: %s",
            clean_log_value(model_id),
            clean_log_value(exc.detail),
        )
        return None
    except Exception as exc:
        logger.debug(
            "Cloud attempt resolution failed for %s: %s",
            clean_log_value(model_id),
            clean_log_value(exc),
        )
        return None
    attempts = result[0] if isinstance(result, tuple) and result else result
    if isinstance(attempts, list) and attempts:
        return attempts
    return None


def _local_canonical(model_id: str) -> str | None:
    """Resolve *model_id* against the local model registry, or ``None``."""
    if _model_manager is None:
        return None
    public_map = _safe_call(
        "Public model map", getattr(_model_manager, "get_public_model_map", None)
    )
    if isinstance(public_map, dict):
        canonical = public_map.get(model_id)
        if isinstance(canonical, str) and canonical:
            return canonical
    try:
        canonical = _model_manager.resolve_model(model_id)
    except Exception:
        return None
    return canonical if isinstance(canonical, str) and canonical else None


def _failover_identity(group: Any) -> str:
    """Identity key of the logical model a failover group stands for.

    The group itself has no upstream identity; its first configured candidate is
    the best grounded answer for the reference-catalog join, because every
    candidate serves the same logical model.
    """
    for candidate in getattr(group, "candidates", ()) or ():
        model = _as_str(getattr(candidate, "model", None))
        if model:
            return model
    return ""


def _failover_endpoints(
    group_name: str,
    group: Any,
    *,
    model_name: str | None,
    model_context: int | None,
) -> list[dict[str, Any]]:
    """One endpoint per configured candidate of a ``failover/{group}`` address."""
    candidates = list(getattr(group, "candidates", ()) or ())
    endpoints: list[dict[str, Any]] = []
    for candidate in candidates:
        provider_name = _as_str(getattr(candidate, "provider", None))
        candidate_model = _as_str(getattr(candidate, "model", None))
        if not provider_name or not candidate_model:
            continue
        # A candidate's model id is already the provider's own catalog key
        # (``{brand}/{model}`` or a bare upstream id): no identity stripping.
        # The reference entry is resolved per candidate, so one candidate never
        # reports another candidate's cross-provider model metadata.
        identity = candidate_model
        reference = _safe_reference(identity)[0]
        upstream = _catalog_metadata(provider_name, identity)
        provider = _provider_object(provider_name)
        overrides = _catalog_overrides(identity) or {}
        # §3.2: pricing is route-shaped and is never borrowed from the reference.
        pricing = _as_dict(upstream.get("pricing")) if upstream else None
        endpoints.append(
            _endpoint_fields(
                provider_name=provider_name,
                model_id=candidate_model,
                model_name=model_name,
                context_length=_route_context_length(
                    provider_name, identity, reference, model_context
                ),
                pricing=pricing,
                # §4: the tag carries the group name for failover candidates, so a
                # client can tell which logical group a route belongs to (and to
                # which provider that candidate routes).
                tag=f"{provider_name}/{group_name}",
                quantization=_as_str(upstream.get("quantization")) if upstream else None,
                max_completion_tokens=_as_positive_int(
                    _top_provider_value(
                        [
                            {"top_provider": {"max_completion_tokens": overrides.get("max_tokens")}},
                            upstream,
                            reference,
                        ],
                        "max_completion_tokens",
                    )
                ),
                supported_parameters=_supported_parameters([upstream, reference], None),
                status=_route_status(provider_name, candidate_model, health_governed=True),
                native_tools=getattr(provider, "native_tools", None),
            )
        )
    return endpoints


async def model_endpoints(model_id: str, request: Request, client_id: str) -> dict[str, Any]:
    """Return OpenRouter-shaped ``{'data': {...}}`` for ``/v1/models/{id}/endpoints``.

    Errors mirror the neighbouring model routes: an unknown model raises
    ``HTTPException(404)`` and a key without ``cloud_gateway_access`` raises
    ``HTTPException(403)``.
    """
    raw_id = (model_id or "").strip()
    while raw_id.endswith("/"):
        raw_id = raw_id[:-1]
    if not raw_id:
        raise HTTPException(
            status_code=404, detail="Model '' not found in configuration (no alias match)"
        )

    if _is_failover_address(raw_id):
        group_name = raw_id.partition("/")[2]
        group = _safe_call(
            "Failover group lookup",
            getattr(_failover_registry, "get_group", None),
            group_name,
        )
        if group is None:
            # Same body shape as app.gateway.model_discovery.model_metadata.
            raise HTTPException(status_code=404, detail=f"Failover group '{group_name}' not found")
        if not _key_can_access_cloud(request, client_id):
            # A failover group is a cloud-routing construct: the model routes hide
            # unauthorized groups and the cloud router refuses them with 403.
            raise _cloud_access_denied()
        cloud_attempts = await _cloud_attempts_for(raw_id, request, client_id)
        model_context = await _resolved_context_window(raw_id, None, cloud_attempts)
        identity = _failover_identity(group)
        reference, reference_source = _safe_reference(identity)
        parity, sources = _resolve_model_metadata(
            raw_id,
            upstream=None,
            reference=reference,
            reference_source=reference_source,
            overrides=None,
            local=None,
            context_length=model_context,
        )
        model_name = _as_str(parity.get("name")) or _derived_name(identity or raw_id)
        endpoints = _failover_endpoints(
            group_name,
            group,
            model_name=model_name,
            model_context=model_context,
        )
        return {"data": _as_model_data(raw_id, parity, endpoints, sources)}

    identity = _split_identity(raw_id)
    canonical = _local_canonical(raw_id)
    can_access_cloud = _key_can_access_cloud(request, client_id)
    cloud_address = _is_cloud_address(raw_id)
    local_routes = _local_routes(raw_id, identity, canonical)

    if not cloud_address and not local_routes and canonical is None:
        # Neither a cloud address nor a locally served model: identical to the
        # neighbouring model route's unknown-model error.
        raise HTTPException(
            status_code=404,
            detail=f"Model '{raw_id}' not found in configuration (no alias match)",
        )
    if cloud_address and not can_access_cloud:
        raise _cloud_access_denied()

    cloud_attempts: list[tuple[Any, str]] | None = None
    if cloud_address:
        cloud_attempts = await _cloud_attempts_for(raw_id, request, client_id)

    cloud_routes = _cloud_routes(raw_id, identity) if can_access_cloud else []
    reference, reference_source = _safe_reference(identity)

    # Model-level (§3) fields come from the route's own upstream when there is
    # one; a locally registered model contributes its grounded local facts.
    model_upstream: dict[str, Any] | None = None
    if cloud_routes:
        model_upstream = _catalog_metadata(
            getattr(cloud_routes[0]["provider"], "name", None), identity
        )
    local_facts = _local_facts(canonical) if canonical else None
    model_context = await _resolved_context_window(raw_id, canonical, cloud_attempts)
    parity, sources = _resolve_model_metadata(
        raw_id,
        upstream=model_upstream,
        reference=reference,
        reference_source=reference_source,
        overrides=_catalog_overrides(identity),
        local=local_facts,
        context_length=model_context,
    )
    model_name = _as_str(parity.get("name")) or _derived_name(identity)

    endpoints = [
        _build_cloud_endpoint(
            route,
            identity=identity,
            model_name=model_name,
            model_context=model_context,
            reference=reference,
        )
        for route in cloud_routes
    ]
    for route in local_routes:
        endpoints.append(
            _build_local_endpoint(
                route,
                provider_name=getattr(route.get("provider"), "name", None),
                model_name=model_name,
                model_context=model_context,
                model_parity=parity,
                local_facts=local_facts,
            )
        )

    # A locally registered model with no local provider configured is still
    # served by Guardian's own backend: report the route with an unknown provider
    # rather than dropping it or inventing a provider name.
    if canonical and not local_routes:
        endpoints.append(
            _build_local_endpoint(
                {"model": canonical},
                provider_name=None,
                model_name=model_name,
                model_context=model_context,
                model_parity=parity,
                local_facts=local_facts,
            )
        )

    return {"data": _as_model_data(raw_id, parity, endpoints, sources)}
