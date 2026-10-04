"""Additive OpenRouter-parity metadata enrichment for model entries.

Both ``/v1/models`` (via :mod:`app.gateway.model_discovery`) and the local-model
entry builder (via :mod:`app.gateway.context_metadata`) attach the same
OpenRouter-shaped metadata to their entries.  This module owns that merge so the
two paths cannot drift apart.

The merge is deliberately **additive**: an existing Guardian key is never
overwritten, so no current client contract changes.  Unknown values are written
as ``None`` rather than omitted, which is OpenRouter's own convention, and every
value carries provenance in ``metadata_sources`` so an operator can tell an
upstream fact from a cross-provider gap fill.

Reference data is *fail-open* by construction: a missing or broken reference
catalog degrades to the upstream value, never to an error.  See
``docs/OPENROUTER_PARITY.md`` for the full field contract.
"""

from __future__ import annotations

import logging
from typing import Any

from app.gateway.model_metadata_presentation import resolve_model_metadata, split_identity
from app.log_safety import clean_log_value

logger = logging.getLogger("Guardian.MetadataEnrichment")


#: Guardian keys the parity merge must never overwrite.
#:
#: ``context_length`` is resolved by Guardian's own chain (config override →
#: upstream catalog → cross-provider reference → safe fallback) and must stay
#: consistent with ``max_context`` / ``max_input_tokens`` / ``meta.n_ctx``,
#: which are derived from that same resolved value.  Letting the upstream
#: advertisement win here would make those keys disagree.
#:
#: ``reasoning`` is protected so it keeps its documented reduced shape: the
#: route's own advertisement (attached by the caller) wins, and a
#: reference-derived block is added only when the route advertises none.
#:
#: ``served_by`` / ``provider`` are Guardian's own routing labels.  The
#: presentation layer cannot know them (it sees no entry) and therefore reports
#: ``None``, so letting it write would erase the real value.
_PARITY_PROTECTED_KEYS = frozenset(
    {
        "id",
        "object",
        "owned_by",
        "permission",
        "served_by",
        "provider",
        "context_length",
        "reasoning",
    }
)


def reduce_reasoning(raw: Any) -> dict[str, Any]:
    """Reduce a reasoning block to Guardian's documented safe subset.

    Mirrors ``CloudModelCatalog._extract_reasoning`` so upstream-advertised and
    reference-derived reasoning metadata have identical shapes; Guardian never
    forwards unknown keys from an upstream reasoning block.
    """
    if not isinstance(raw, dict):
        return {}
    supported = raw.get("supported_efforts")
    if not isinstance(supported, list):
        return {}
    efforts = [s for s in supported if isinstance(s, str) and s]
    if not efforts:
        return {}
    result: dict[str, Any] = {"supported_efforts": efforts}
    if isinstance(raw.get("default_effort"), str) and raw.get("default_effort"):
        result["default_effort"] = raw["default_effort"]
    if isinstance(raw.get("mandatory"), bool):
        result["mandatory"] = raw["mandatory"]
    if isinstance(raw.get("default_enabled"), bool):
        result["default_enabled"] = raw["default_enabled"]
    return result


def _safe_metadata(catalog: Any, provider_name: str | None, identity: str) -> dict[str, Any] | None:
    """Read one model's captured metadata, fail-open on any error."""
    if catalog is None or not provider_name:
        return None
    try:
        raw = catalog.get_model_metadata(provider_name, identity)
    except Exception as exc:  # fail-open: metadata never breaks discovery
        logger.debug("Catalog metadata lookup failed for %s/%s: %s", clean_log_value(provider_name), clean_log_value(identity), clean_log_value(exc))
        return None
    return raw if isinstance(raw, dict) and raw else None


def _safe_reference(reference_catalog: Any, identity: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read one model's cross-provider reference metadata, fail-open."""
    if reference_catalog is None:
        return None, None
    try:
        raw = reference_catalog.metadata(identity)
        source = reference_catalog.source_name(identity)
    except Exception as exc:  # fail-open
        logger.debug("Reference metadata lookup failed for %s: %s", clean_log_value(identity), clean_log_value(exc))
        return None, None
    return (raw if isinstance(raw, dict) and raw else None), source


def _safe_overrides(catalog: Any, provider_name: str | None, identity: str) -> dict[str, Any] | None:
    """Read the operator's per-model config overrides, fail-open."""
    if catalog is None or not provider_name:
        return None
    try:
        raw = catalog.get_model_overrides(identity, provider_name)
    except Exception as exc:  # fail-open
        logger.debug("Override lookup failed for %s/%s: %s", clean_log_value(provider_name), clean_log_value(identity), clean_log_value(exc))
        return None
    return raw if isinstance(raw, dict) and raw else None


def identity_key(model_id: str, provider_name: str | None = None) -> str:
    """Resolve the cross-provider identity key for a Guardian address.

    Spec §1: for ``{provider}/{brand}/{model}`` the key is ``{brand}/{model}``,
    which is what the presentation layer's pure ``split_identity`` computes.

    Two-segment cloud addresses omit the brand, and Guardian's convention is
    that a bare upstream id is branded with the provider's name — so the identity
    key of ``openai/gpt-4o`` is ``openai/gpt-4o``, *not* ``gpt-4o``.
    ``split_identity`` cannot make that call because it is pure and never sees
    the provider; the enrichment layer does, so the ambiguous case is resolved
    here.  Getting it wrong is silent: the reference lookup simply misses and the
    route loses its cross-provider metadata.
    """
    if isinstance(model_id, str) and provider_name:
        parts = model_id.split("/")
        if len(parts) == 2 and parts[0] == provider_name:
            return model_id
    return split_identity(model_id)


def attach_parity_metadata(
    model_entry: dict[str, Any],
    *,
    catalog: Any = None,
    reference_catalog: Any = None,
    provider_name: str | None = None,
    local: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge OpenRouter-parity metadata into *model_entry* in place.

    Sources, in precedence order: this route's operator config overrides, the
    provider's own catalog advertisement, the cross-provider reference catalog,
    then grounded local facts.  Returns the entry for convenience.
    """
    full_id = model_entry.get("id")
    if not isinstance(full_id, str) or not full_id:
        return model_entry

    identity = identity_key(full_id, provider_name)
    upstream = _safe_metadata(catalog, provider_name, identity)
    overrides = _safe_overrides(catalog, provider_name, identity)
    reference, reference_source = _safe_reference(reference_catalog, identity)

    try:
        parity, sources = resolve_model_metadata(
            full_id,
            upstream=upstream,
            reference=reference,
            reference_source=reference_source,
            overrides=overrides,
            local=local,
            context_length=model_entry.get("context_length"),
            created=model_entry.get("created"),
        )
    except Exception as exc:  # fail-open: metadata never breaks discovery
        logger.warning("⚠️  Parity metadata failed for %s: %s", clean_log_value(full_id), clean_log_value(exc))
        return model_entry

    for key, value in parity.items():
        if key in _PARITY_PROTECTED_KEYS:
            continue
        # A ``None`` from the presentation layer means "unknown", never "clear
        # the existing value": an existing Guardian value is always at least as
        # informed as a missing upstream field.
        if value is None and key in model_entry:
            continue
        model_entry[key] = value

    # ``reasoning`` keeps its reduced shape; only fill it when the route's own
    # advertisement did not already provide one.
    if not model_entry.get("reasoning"):
        reduced = reduce_reasoning(parity.get("reasoning"))
        if reduced:
            model_entry["reasoning"] = reduced

    # Always present, even when empty: ``{}`` is the meaningful statement
    # "every section came from this route's own upstream advertisement", and
    # the spec's null-discipline rule says contract keys are never omitted.
    model_entry["metadata_sources"] = sources
    return model_entry


def local_model_facts(model_manager: Any, canonical_name: str) -> dict[str, Any]:
    """Collect the grounded facts Guardian has about a locally served model.

    Only real declarations are reported.  ``tools``/``tool_choice`` and
    ``grammar`` are advertised solely when the model config states them, because
    Guardian must never claim a capability it would silently drop.  Anything
    unknown stays absent so the presentation layer reports ``None``.
    """
    facts: dict[str, Any] = {"served_by": "local"}
    config: dict[str, Any] = {}
    try:
        models = getattr(model_manager, "models", None)
        if isinstance(models, dict):
            candidate = models.get(canonical_name)
            if isinstance(candidate, dict):
                config = candidate
    except Exception as exc:  # fail-open
        logger.debug("Local model config lookup failed for %s: %s", clean_log_value(canonical_name), clean_log_value(exc))

    model_type = config.get("model_type")
    if isinstance(model_type, str) and model_type:
        facts["model_type"] = model_type
    else:
        # A model launched with llama-server's ``--embedding`` flag is an
        # embedding model even when the config omits ``model_type``.  Without
        # this the presentation layer would default the modality to
        # ``text->text``, which claims the route returns text when it actually
        # returns a vector.
        extra_args = config.get("extra_args")
        if isinstance(extra_args, str) and "--embedding" in extra_args.split():
            facts["model_type"] = "embedding"
    if isinstance(config.get("grammar_decoding"), bool):
        facts["grammar_decoding"] = config["grammar_decoding"]
    # ``tool_profile`` is the config's declared tool-support switch; the
    # presentation layer consumes it as ``tool_support`` (see the frozen
    # contract in docs/OPENROUTER_PARITY.md §3.4).
    if isinstance(config.get("tool_profile"), bool):
        facts["tool_support"] = config["tool_profile"]
    max_tokens = config.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        facts["max_tokens"] = max_tokens

    try:
        facts["vision"] = model_manager.get_vision_capability(canonical_name)
    except Exception as exc:  # fail-open
        logger.debug("Vision capability lookup failed for %s: %s", clean_log_value(canonical_name), clean_log_value(exc))
    return facts
