"""Wiring tests for the OpenRouter-parity metadata enrichment.

These pin the behaviour of the *integration* layer (``metadata_enrichment`` and
the context-resolution reference fallback), not the internals of the catalog,
presentation or endpoints modules — those have their own suites.

The contract under test is ``docs/OPENROUTER_PARITY.md``: the enrichment is
additive, unknown values are reported as ``None`` rather than omitted, official
Guardian keys keep their values, and reference data only ever fills a gap.
"""

from __future__ import annotations

import pytest

from app.gateway import context_metadata
from app.gateway.metadata_enrichment import (
    attach_parity_metadata,
    identity_key,
    local_model_facts,
    reduce_reasoning,
)

#: Parity keys the spec guarantees on every entry (the Guardian-native keys
#: ``id``/``object``/``created``/``owned_by``/``permission``/``served_by``/
#: ``provider`` already exist and are asserted separately).  ``reasoning`` is
#: deliberately NOT here: it keeps its pre-existing documented contract of
#: being *optional* — absent when no source advertises reasoning metadata.
PARITY_FIELDS = (
    "canonical_slug",
    "hugging_face_id",
    "name",
    "description",
    "context_length",
    "architecture",
    "pricing",
    "top_provider",
    "per_request_limits",
    "supported_parameters",
    "default_parameters",
    "knowledge_cutoff",
    "expiration_date",
    "links",
)

UPSTREAM_METADATA = {
    "canonical_slug": "openai/gpt-4o-2024-08-06",
    "hugging_face_id": None,
    "name": "OpenAI: GPT-4o",
    "description": "GPT-4o is OpenAI's flagship multimodal model.",
    "created": 1715558400,
    "context_length": 128000,
    "architecture": {
        "modality": "text+image+file->text",
        "input_modalities": ["text", "image", "file"],
        "output_modalities": ["text"],
        "tokenizer": "GPT",
        "instruct_type": None,
    },
    "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
    "top_provider": {
        "context_length": 128000,
        "max_completion_tokens": 16384,
        "is_moderated": True,
    },
    "per_request_limits": None,
    "supported_parameters": ["tools", "tool_choice", "temperature", "response_format"],
    "default_parameters": {},
    "knowledge_cutoff": None,
    "expiration_date": None,
    "reasoning": {"supported_efforts": ["high", "medium", "low"], "default_effort": "medium"},
}


class _FakeCatalog:
    """Stand-in for CloudModelCatalog's metadata/override accessors."""

    def __init__(self, metadata=None, overrides=None, explode=False):
        self._metadata = metadata or {}
        self._overrides = overrides or {}
        self._explode = explode

    def get_model_metadata(self, provider_name, normalized_id):
        if self._explode:
            raise RuntimeError("catalog exploded")
        return dict(self._metadata.get(normalized_id, {}))

    def get_model_overrides(self, normalized_id, provider_name=""):
        if self._explode:
            raise RuntimeError("catalog exploded")
        return dict(self._overrides.get(normalized_id, {}))


class _FakeReference:
    """Stand-in for ModelReferenceCatalog's lookup accessors."""

    def __init__(self, metadata=None, source="openrouter", explode=False):
        self._metadata = metadata or {}
        self._source = source
        self._explode = explode

    def metadata(self, identity_key):
        if self._explode:
            raise RuntimeError("reference exploded")
        return dict(self._metadata.get(identity_key, {}))

    def source_name(self, identity_key):
        return self._source if identity_key in self._metadata else None


def _cloud_entry(model_id: str = "openai/openai/gpt-4o") -> dict:
    return {
        "id": model_id,
        "object": "model",
        "created": 1700000000,
        "owned_by": "openai",
        "permission": [],
        "served_by": "cloud",
        "provider": "openai",
        "context": 131072,
        "context_length": 131072,
        "max_input_tokens": 131072,
        "max_context": 131072,
        "meta": {"n_ctx": 131072},
    }


# ── Additivity ───────────────────────────────────────────────────────


def test_enrichment_is_purely_additive():
    entry = _cloud_entry()
    before = dict(entry)
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": UPSTREAM_METADATA}),
        provider_name="openai",
    )
    for key, value in before.items():
        assert entry[key] == value, f"existing key {key!r} was overwritten"
    assert set(PARITY_FIELDS) <= set(entry)


def test_resolved_context_window_is_never_overwritten():
    """The upstream advertises 128000; Guardian resolved 131072 (its own chain).

    ``context_length`` must keep Guardian's value so it stays consistent with
    ``max_context`` / ``max_input_tokens`` / ``meta.n_ctx``, which are derived
    from the same resolved number.
    """
    entry = _cloud_entry()
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": UPSTREAM_METADATA}),
        provider_name="openai",
    )
    assert entry["context_length"] == 131072
    assert entry["max_context"] == 131072
    assert entry["max_input_tokens"] == 131072
    assert entry["meta"]["n_ctx"] == 131072


def test_unknown_values_are_none_not_missing():
    """A route with no metadata anywhere still carries every parity key."""
    entry = _cloud_entry("mystery/mystery/unknown-model")
    attach_parity_metadata(entry, catalog=_FakeCatalog(), provider_name="mystery")
    for key in PARITY_FIELDS:
        assert key in entry, f"parity key {key!r} missing"
    assert entry["pricing"] is None
    assert entry["knowledge_cutoff"] is None


# ── Cross-provider gap filling ───────────────────────────────────────


def test_reference_fills_the_gap_the_upstream_left():
    """openai's own catalog says nothing; the reference catalog does."""
    entry = _cloud_entry()
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": {"name": None, "supported_parameters": None}}),
        reference_catalog=_FakeReference({"openai/gpt-4o": UPSTREAM_METADATA}),
        provider_name="openai",
    )
    assert entry["supported_parameters"] == UPSTREAM_METADATA["supported_parameters"]
    assert entry["architecture"]["input_modalities"] == ["text", "image", "file"]
    assert entry["metadata_sources"]["supported_parameters"] == "reference:openrouter"


def test_upstream_wins_and_is_absent_from_metadata_sources():
    entry = _cloud_entry()
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": UPSTREAM_METADATA}),
        reference_catalog=_FakeReference({"openai/gpt-4o": {**UPSTREAM_METADATA, "name": "WRONG"}}),
        provider_name="openai",
    )
    assert entry["name"] == "OpenAI: GPT-4o"
    assert "name" not in (entry.get("metadata_sources") or {})


def test_metadata_sources_key_is_present_even_when_empty():
    """An empty map is a statement, not an absence: "every section came from
    this route's own upstream advertisement". The contract keys are never
    omitted, so a client can read the field without a presence check."""
    entry = _cloud_entry("openrouter/openai/gpt-4o")
    entry["provider"] = "openrouter"
    entry["context_length"] = UPSTREAM_METADATA["context_length"]
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": UPSTREAM_METADATA}),
        provider_name="openrouter",
    )
    assert "metadata_sources" in entry
    assert entry["metadata_sources"] == {}


def test_metadata_sources_is_present_for_a_totally_unknown_route():
    entry = _cloud_entry("mystery/mystery/nothing-known")
    attach_parity_metadata(entry, catalog=_FakeCatalog(), provider_name="mystery")
    assert isinstance(entry.get("metadata_sources"), dict)


# ── Identity key ─────────────────────────────────────────────────────


def test_identity_key_resolves_the_cross_provider_join():
    # {provider}/{brand}/{model} -> {brand}/{model}
    assert identity_key("openai/openai/gpt-4o", "openai") == "openai/gpt-4o"
    assert identity_key("nvidia/minimaxai/minimax-m3", "nvidia") == "minimaxai/minimax-m3"
    assert identity_key("openrouter/anthropic/claude-sonnet-4.6", "openrouter") == (
        "anthropic/claude-sonnet-4.6"
    )
    # A two-segment cloud address omits the brand, and the bare upstream id is
    # branded with the provider's name — so the address IS the identity key.
    assert identity_key("openai/gpt-4o", "openai") == "openai/gpt-4o"
    # Local aliases and provider-less lookups are untouched.
    assert identity_key("llama3.2-3b", None) == "llama3.2-3b"
    assert identity_key("llama3.2-3b", "ai-node-local") == "llama3.2-3b"


def test_two_segment_cloud_address_still_receives_cross_provider_metadata():
    """The regression the identity key exists to prevent: stripping the first
    segment of ``openai/gpt-4o`` yields ``gpt-4o``, the reference lookup misses,
    and the route silently loses every gap fill."""
    entry = _cloud_entry("openai/gpt-4o")
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog(),
        reference_catalog=_FakeReference({"openai/gpt-4o": UPSTREAM_METADATA}),
        provider_name="openai",
    )
    assert entry["supported_parameters"] == UPSTREAM_METADATA["supported_parameters"]
    assert entry["metadata_sources"]["supported_parameters"] == "reference:openrouter"
    assert entry["architecture"]["input_modalities"] == ["text", "image", "file"]


def test_reference_never_changes_a_price_it_does_not_serve():
    """A cloud route with no advertised price stays null, never borrowed."""
    reference_only = {**UPSTREAM_METADATA, "pricing": {"prompt": "0.0000025", "completion": "0.00001"}}
    entry = _cloud_entry("nvidia/openai/gpt-4o")
    entry["provider"] = "nvidia"
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": {"pricing": None}}),
        reference_catalog=_FakeReference({"openai/gpt-4o": reference_only}),
        provider_name="nvidia",
    )
    # The reference price describes OpenRouter's route, not NVIDIA's.
    assert entry["pricing"] is None


# ── Local models ─────────────────────────────────────────────────────


def test_local_route_reports_grounded_zero_pricing():
    entry = _cloud_entry("llama3.2-3b")
    entry.update({"served_by": "local", "provider": None})
    attach_parity_metadata(
        entry,
        local={"served_by": "local", "grammar_decoding": True},
    )
    assert entry["pricing"] is not None
    assert all(v == "0" for k, v in entry["pricing"].items() if k != "local")


def test_local_supported_parameters_only_claims_declared_capabilities():
    """``tools``/``grammar`` are gated on real config declarations."""
    plain = _cloud_entry("local-plain")
    attach_parity_metadata(plain, local={"served_by": "local"})
    assert "tools" not in (plain["supported_parameters"] or [])
    assert "grammar" not in (plain["supported_parameters"] or [])

    declared = _cloud_entry("local-full")
    attach_parity_metadata(
        declared,
        local={"served_by": "local", "grammar_decoding": True, "tool_support": True},
    )
    assert "tools" in declared["supported_parameters"]
    assert "grammar" in declared["supported_parameters"]


class _FakeModelManager:
    def __init__(self, models):
        self.models = models

    def get_vision_capability(self, name):
        return {"configured": False, "status": "text_only", "validated": False}


def test_local_model_facts_report_only_declarations():
    manager = _FakeModelManager({
        "with-decls": {"grammar_decoding": True, "tool_profile": True, "max_tokens": 4096, "model_type": "chat"},
        "bare": {"path": "/tmp/whatever.gguf"},
    })
    rich = local_model_facts(manager, "with-decls")
    assert rich["served_by"] == "local"
    assert rich["grammar_decoding"] is True
    # The config's ``tool_profile`` switch is published as ``tool_support``,
    # the key the presentation layer consumes.
    assert rich["tool_support"] is True
    assert rich["max_tokens"] == 4096
    assert rich["model_type"] == "chat"

    sparse = local_model_facts(manager, "bare")
    assert sparse["served_by"] == "local"
    assert "grammar_decoding" not in sparse
    assert "tool_support" not in sparse
    assert "max_tokens" not in sparse


def test_local_facts_treat_llama_server_embedding_flag_as_an_embedding_model():
    """A model launched with ``--embedding`` is an embedding model even when the
    config omits ``model_type``.  Without this the presentation layer would
    default the modality to ``text->text``, claiming the route returns text
    when it actually returns a vector."""
    manager = _FakeModelManager({"embed-no-type": {"extra_args": "--embedding --reasoning off"}})
    assert local_model_facts(manager, "embed-no-type")["model_type"] == "embedding"


def test_local_facts_prefer_an_explicit_model_type_over_the_flag():
    manager = _FakeModelManager({"m": {"model_type": "chat", "extra_args": "--embedding"}})
    assert local_model_facts(manager, "m")["model_type"] == "chat"


def test_local_facts_do_not_invent_a_model_type_for_a_plain_model():
    manager = _FakeModelManager({"plain": {"path": "/tmp/x.gguf"}})
    assert "model_type" not in local_model_facts(manager, "plain")


# ── Reasoning shape ──────────────────────────────────────────────────


def test_reasoning_keeps_the_documented_reduced_shape():
    reduced = reduce_reasoning({
        "mandatory": False,
        "default_enabled": True,
        "supported_efforts": ["high", "low"],
        "default_effort": "high",
        "unknown_internal_key": "must not leak",
    })
    assert reduced == {
        "supported_efforts": ["high", "low"],
        "default_effort": "high",
        "mandatory": False,
        "default_enabled": True,
    }
    assert reduce_reasoning(None) == {}
    assert reduce_reasoning({"mandatory": True}) == {}


def test_existing_reasoning_is_not_replaced_by_the_parity_block():
    entry = _cloud_entry("openai/openai/gpt-4o")
    entry["reasoning"] = {"supported_efforts": ["high"], "default_effort": "high"}
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog({"openai/gpt-4o": UPSTREAM_METADATA}),
        provider_name="openai",
    )
    assert entry["reasoning"] == {"supported_efforts": ["high"], "default_effort": "high"}


# ── Fail-open ────────────────────────────────────────────────────────


def test_enrichment_fails_open_when_a_catalog_raises():
    entry = _cloud_entry()
    before = dict(entry)
    attach_parity_metadata(
        entry,
        catalog=_FakeCatalog(explode=True),
        reference_catalog=_FakeReference(explode=True),
        provider_name="openai",
    )
    # The entry survives; whatever the presentation layer produced is still there.
    for key, value in before.items():
        assert entry[key] == value


def test_enrichment_ignores_an_entry_without_an_id():
    entry = {"object": "model"}
    result = attach_parity_metadata(entry, catalog=_FakeCatalog({"x": UPSTREAM_METADATA}))
    assert result == {"object": "model"}


# ── Context resolution reference fallback ────────────────────────────


@pytest.fixture
def clean_context_metadata():
    """Restore the module-level injected deps after each test."""
    saved = (
        context_metadata._reference_catalog,
        context_metadata._cloud_catalog,
        context_metadata._provider_registry,
    )
    yield
    (
        context_metadata._reference_catalog,
        context_metadata._cloud_catalog,
        context_metadata._provider_registry,
    ) = saved


def test_reference_context_window_is_used_as_a_last_grounded_source(clean_context_metadata):
    context_metadata._reference_catalog = _FakeReference({
        "openai/gpt-4o": {"context_length": 128000},
    })
    assert context_metadata._reference_context_window("openai/openai/gpt-4o") == 128000
    # A model the reference catalog does not know yields nothing (not a guess).
    assert context_metadata._reference_context_window("openai/openai/unknown") is None


def test_reference_context_window_fails_open(clean_context_metadata):
    context_metadata._reference_catalog = _FakeReference(explode=True)
    assert context_metadata._reference_context_window("openai/openai/gpt-4o") is None


def test_reference_context_window_is_skipped_without_a_catalog(clean_context_metadata):
    context_metadata._reference_catalog = None
    assert context_metadata._reference_context_window("openai/openai/gpt-4o") is None
