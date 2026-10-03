"""Unit tests for :mod:`app.gateway.model_metadata_presentation`.

The module under test is pure: no I/O, no network, no filesystem. These tests
pin the frozen contract in ``docs/OPENROUTER_PARITY.md`` (sections 3, 3.1-3.4, 4
and 5): null discipline, architecture synthesis in both directions, reference
gap filling, upstream precedence, pricing honesty and local parameter honesty.
"""

from __future__ import annotations

import time

import pytest

from app.gateway.model_metadata_presentation import (
    ARCHITECTURE_KEYS,
    LOCAL_PRICING_KEYS,
    LOCAL_SUPPORTED_PARAMETERS,
    PARITY_FIELDS,
    TOP_PROVIDER_KEYS,
    derive_name,
    endpoint_entry_fields,
    endpoints_link,
    local_supported_parameters,
    resolve_model_metadata,
    split_identity,
    supports_tool_choice_shape,
    synthesize_architecture,
    synthesize_pricing,
    synthesize_supported_parameters,
    synthesize_top_provider,
)

# The normalised per-model subset another track produces for upstream/reference.
SUBSET_KEYS = (
    "canonical_slug",
    "hugging_face_id",
    "name",
    "description",
    "created",
    "context_length",
    "architecture",
    "pricing",
    "top_provider",
    "per_request_limits",
    "supported_parameters",
    "default_parameters",
    "knowledge_cutoff",
    "expiration_date",
    "reasoning",
)

#: Sections the module is allowed to report in ``metadata_sources``.
ALLOWED_LABELS = {"upstream", "override", "local", "derived"}


def subset(**values: object) -> dict:
    """Build a normalised upstream/reference subset: every key, ``None`` default."""
    row: dict = {key: None for key in SUBSET_KEYS}
    row.update(values)
    return row


def architecture(**values: object) -> dict:
    """Build an architecture object with all five keys, ``None`` default."""
    row: dict = {key: None for key in ARCHITECTURE_KEYS}
    row.update(values)
    return row


# ── Identity (spec section 1) ────────────────────────────────────────


def test_split_identity_one_two_and_three_segment_ids():
    # A bare key has no provider segment: it is its own identity.
    assert split_identity("gpt-4o") == "gpt-4o"
    # Two segments: the first segment is the provider, so the identity is the
    # remainder (the frozen rule is "everything after the first /").
    assert split_identity("openai/gpt-4o") == "gpt-4o"
    # Three segments: {provider}/{brand}/{model} -> {brand}/{model}, which is
    # exactly the key an OpenRouter-style reference catalog is keyed on.
    assert split_identity("openai/openai/gpt-4o") == "openai/gpt-4o"
    assert (
        split_identity("openrouter/anthropic/claude-sonnet-4.6")
        == "anthropic/claude-sonnet-4.6"
    )
    assert (
        split_identity("nvidia/meta-llama/llama-3.3-70b-instruct")
        == "meta-llama/llama-3.3-70b-instruct"
    )
    assert split_identity("ai-node-local/local/gemma-3-27b") == "local/gemma-3-27b"
    assert split_identity("") == ""


def test_endpoints_link_is_guardians_own_route():
    assert endpoints_link("openrouter/anthropic/claude-sonnet-4.6") == {
        "details": "/v1/models/openrouter/anthropic/claude-sonnet-4.6/endpoints"
    }


# ── (g) derive_name ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("identity_key", "expected"),
    [
        ("openai/gpt-4o", "OpenAI: GPT-4o"),
        # Verified against the live OpenRouter id -> name mapping.
        ("openai/gpt-6.1-sol-pro", "OpenAI: GPT-6.1 Sol Pro"),
        ("anthropic/claude-sonnet-4.6", "Anthropic: Claude Sonnet 4.6"),
        ("anthropic/claude-sonnet-4.6:batch", "Anthropic: Claude Sonnet 4.6 (batch)"),
        ("z-ai/glm-4.6", "Z-AI: GLM 4.6"),
        ("meta-llama/llama-3.3-70b-instruct", "Meta Llama: Llama 3.3 70B Instruct"),
        ("minimaxai/minimax-m2", "MinimaxAI: Minimax M2"),
        ("x-ai/grok-4", "xAI: Grok 4"),
        ("mistralai/mistral-large-2411", "Mistral AI: Mistral Large 2411"),
        (
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "NVIDIA: Llama 3.1 Nemotron 70B Instruct",
        ),
        ("deepseek/deepseek-r1", "DeepSeek: DeepSeek R1"),
        ("qwen/qwen3.8-27b", "Qwen: Qwen3.8 27B"),
        ("google/gemini-2.5-pro", "Google: Gemini 2.5 Pro"),
        # unknown brand: deterministic generic fallback
        ("some-brand/model-v1", "Some-Brand: Model V1"),
        ("unknownvendor/thing-2", "Unknownvendor: Thing 2"),
        # bare key: returned unchanged
        ("gpt-4o", "gpt-4o"),
        ("llama-3.2-3b-instruct", "llama-3.2-3b-instruct"),
    ],
)
def test_derive_name_known_brands_bare_keys_and_fallback(identity_key, expected):
    assert derive_name(identity_key) == expected


def test_derive_name_is_deterministic_and_empty_safe():
    assert derive_name("openai/gpt-4o") == derive_name("openai/gpt-4o")
    assert derive_name("") == ""
    assert derive_name(None) == ""  # type: ignore[arg-type]


# ── (a) null discipline ──────────────────────────────────────────────


def test_bare_model_entry_has_every_parity_key_and_unknown_as_none():
    fields, sources = resolve_model_metadata("x")

    assert set(PARITY_FIELDS) <= set(fields)
    for key in (
        "owned_by",
        "provider",
        "served_by",
        "canonical_slug",
        "hugging_face_id",
        "description",
        "context_length",
        "pricing",
        "per_request_limits",
        "supported_parameters",
        "knowledge_cutoff",
        "expiration_date",
        "reasoning",
    ):
        assert fields[key] is None, key

    # Structural values are never None: the spec types them as objects.
    assert fields["architecture"] == {key: None for key in ARCHITECTURE_KEYS}
    assert fields["top_provider"] == {key: None for key in TOP_PROVIDER_KEYS}
    assert fields["default_parameters"] == {}
    assert fields["permission"] == []
    assert fields["id"] == "x"
    assert fields["object"] == "model"
    assert fields["name"] == "x"
    assert fields["links"] == {"details": "/v1/models/x/endpoints"}
    assert isinstance(fields["created"], int)

    # Nothing came from an upstream, so the derived facts are listed.
    assert sources == {
        "name": "derived",
        "architecture": "derived",
        "top_provider": "derived",
    }


def test_created_falls_back_to_the_request_time():
    before = int(time.time())
    fields, _ = resolve_model_metadata("x")
    after = int(time.time())
    assert before <= fields["created"] <= after

    # An explicit value and an upstream value both win over the clock.
    explicit, _ = resolve_model_metadata("x", created=1234567890)
    assert explicit["created"] == 1234567890
    from_upstream, _ = resolve_model_metadata("x", upstream=subset(created=42))
    assert from_upstream["created"] == 42


def test_every_metadata_sources_label_is_from_the_frozen_vocabulary():
    fields, sources = resolve_model_metadata(
        "openrouter/anthropic/claude-sonnet-4.6",
        upstream=subset(created=1),
        reference=subset(name="Anthropic: Claude Sonnet 4.6"),
        reference_source="openrouter",
        # A facts dict that declares a cloud route must not be treated as local
        # facts: no local architecture, no local provenance.
        local={"served_by": "cloud"},
    )
    assert fields["name"] == "Anthropic: Claude Sonnet 4.6"
    assert sources["name"] == "reference:openrouter"
    assert sources["architecture"] == "derived"  # empty skeleton, Guardian's shape
    assert fields["served_by"] == "cloud"
    for label in sources.values():
        assert label in ALLOWED_LABELS or label.startswith("reference:"), label


# ── (b) architecture synthesis, both directions ──────────────────────


def test_architecture_modality_is_synthesized_from_the_lists():
    """lists known, string unknown -> modality derived."""
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            architecture=architecture(
                input_modalities=["text", "image"],
                output_modalities=["text"],
            )
        ),
    )
    assert fields["architecture"] == {
        "modality": "text+image->text",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "tokenizer": None,
        "instruct_type": None,
    }
    # Wholly upstream: absent from metadata_sources (spec section 5).
    assert "architecture" not in sources


def test_architecture_lists_are_parsed_from_the_modality_string():
    """string known, lists unknown -> lists derived (reference fills them)."""
    fields, sources = resolve_model_metadata(
        "openrouter/anthropic/claude-sonnet-4.6",
        reference=subset(architecture=architecture(modality="text+image+file->text")),
        reference_source="openrouter",
    )
    assert fields["architecture"] == {
        "modality": "text+image+file->text",
        "input_modalities": ["text", "image", "file"],
        "output_modalities": ["text"],
        "tokenizer": None,
        "instruct_type": None,
    }
    assert sources["architecture"] == "reference:openrouter"


def test_architecture_reference_completes_a_listless_upstream():
    """Upstream states only a tokenizer; the reference states the modality."""
    fields, sources = resolve_model_metadata(
        "openrouter/google/gemini-2.5-pro",
        upstream=subset(
            architecture=architecture(tokenizer="Gemini"),
        ),
        reference=subset(architecture=architecture(modality="text+image->text")),
        reference_source="openrouter",
    )
    assert fields["architecture"] == {
        "modality": "text+image->text",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "tokenizer": "Gemini",
        "instruct_type": None,
    }
    assert sources["architecture"] == "reference:openrouter"


def test_architecture_merges_two_partial_sources_without_inconsistency():
    """Neither source can state the triple alone: components are merged and the
    modality string is derived from the merged lists."""
    fields, _ = resolve_model_metadata(
        "nvidia/meta-llama/llama-3.3-70b-instruct",
        upstream=subset(architecture=architecture(input_modalities=["text"])),
        reference=subset(architecture=architecture(output_modalities=["text"])),
        reference_source="openrouter",
    )
    assert fields["architecture"]["input_modalities"] == ["text"]
    assert fields["architecture"]["output_modalities"] == ["text"]
    assert fields["architecture"]["modality"] == "text->text"


def test_architecture_helper_returns_skeleton_and_provenance():
    value, label = synthesize_architecture(None, None, None)
    assert value == {key: None for key in ARCHITECTURE_KEYS}
    assert label == "derived"

    value, label = synthesize_architecture(
        subset(architecture=architecture(modality="text->text")), None, None
    )
    assert value["input_modalities"] == ["text"]
    assert label == "upstream"

    value, label = synthesize_architecture(
        None, subset(architecture=architecture(modality="text->text")), None
    )
    assert label == "reference"  # no source name known -> honest bare label

    value, label = synthesize_architecture(None, None, {"served_by": "local"})
    assert value == {
        "modality": "text->text",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tokenizer": None,
        "instruct_type": None,
    }
    assert label == "local"

    value, label = synthesize_architecture(
        None, None, {"served_by": "local", "model_type": "embedding"}
    )
    assert value["modality"] == "text->embedding"
    assert value["output_modalities"] == ["embedding"]

    value, label = synthesize_architecture(
        None, None, {"served_by": "local", "vision": {"configured": True}}
    )
    assert value["modality"] == "text+image->text"
    assert value["input_modalities"] == ["text", "image"]


def test_architecture_reference_fills_an_all_none_upstream_skeleton():
    """Regression: the catalog always emits the full 5-key skeleton with ``None``
    for unknown, so a present-but-empty ``architecture`` block must count as a
    gap (never as an answer) and the reference must still complete it.

    This is the integration repro reported against an earlier revision: a
    truthiness check on the block would silently disable the headline
    cross-provider fill.
    """
    reference = subset(
        name="OpenAI: GPT-4o",
        architecture=architecture(
            modality="text+image+file->text",
            input_modalities=["text", "image", "file"],
            output_modalities=["text"],
            tokenizer="GPT",
        ),
    )
    upstream = subset(name=None, architecture=architecture())  # every key None

    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=upstream,
        reference=reference,
        reference_source="openrouter",
    )

    assert fields["architecture"] == {
        "modality": "text+image+file->text",
        "input_modalities": ["text", "image", "file"],
        "output_modalities": ["text"],
        "tokenizer": "GPT",
        "instruct_type": None,
    }
    assert sources["architecture"] == "reference:openrouter"
    assert fields["name"] == "OpenAI: GPT-4o"


def test_architecture_skeleton_is_filled_per_sub_key():
    """A partly-filled upstream block is completed sub-key by sub-key: the
    upstream's real value is kept, only its ``None`` fields are filled."""
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            architecture=architecture(
                modality="text->text", input_modalities=["text"], output_modalities=["text"]
            )
        ),
        reference=subset(
            architecture=architecture(modality="text+image->text", tokenizer="GPT")
        ),
        reference_source="openrouter",
    )
    # Upstream's own modality triple wins; the reference fills the tokenizer.
    assert fields["architecture"]["modality"] == "text->text"
    assert fields["architecture"]["input_modalities"] == ["text"]
    assert fields["architecture"]["tokenizer"] == "GPT"
    assert sources["architecture"] == "reference:openrouter"


def test_a_real_value_is_never_treated_as_a_gap():
    """``False``/``0``/``""`` are real values (the catalog's merge rule), and an
    upstream that states one is not "empty"."""
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            context_length=0,
            top_provider={"context_length": 0, "max_completion_tokens": 0, "is_moderated": False},
            supported_parameters=None,
        ),
        reference=subset(
            context_length=128000,
            top_provider={
                "context_length": 128000,
                "max_completion_tokens": 16384,
                "is_moderated": True,
            },
            supported_parameters=["tools"],
        ),
        reference_source="openrouter",
    )
    assert fields["context_length"] == 0
    assert fields["top_provider"]["context_length"] == 0
    assert fields["top_provider"]["max_completion_tokens"] == 0
    assert fields["top_provider"]["is_moderated"] is False
    # A ``None`` upstream field is still a gap and still gets filled.
    assert fields["supported_parameters"] == ["tools"]
    assert sources["supported_parameters"] == "reference:openrouter"


def test_top_provider_skeleton_is_a_gap_not_an_answer():
    fields, _ = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            top_provider={"context_length": None, "max_completion_tokens": None, "is_moderated": None}
        ),
        reference=subset(
            top_provider={"context_length": 128000, "max_completion_tokens": 16384}
        ),
        reference_source="openrouter",
    )
    assert fields["top_provider"]["max_completion_tokens"] == 16384
    assert fields["top_provider"]["context_length"] == 128000


def test_empty_string_handling_is_explicit():
    """Top-level scalars pass an empty string through verbatim (the catalog's
    ``hugging_face_id: ""`` convention is data, and OpenRouter itself emits it),
    while an empty *architecture* string counts as unknown: ``modality`` and
    ``tokenizer`` have no meaningful empty form, so the reference may fill them
    and ``modality`` can still be synthesized from the lists."""
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            hugging_face_id="",
            description="",
            architecture=architecture(modality="", tokenizer=""),
        ),
        reference=subset(
            hugging_face_id="openai/gpt-4o",
            description="OpenAI's flagship multimodal model.",
            architecture=architecture(modality="text+image->text", tokenizer="GPT"),
        ),
        reference_source="openrouter",
    )
    # Verbatim pass-through: a stated empty value is not replaced.
    assert fields["hugging_face_id"] == ""
    assert fields["description"] == ""
    # Architecture: empty strings are gaps, so the reference completes them.
    assert fields["architecture"]["modality"] == "text+image->text"
    assert fields["architecture"]["input_modalities"] == ["text", "image"]
    assert fields["architecture"]["tokenizer"] == "GPT"
    assert sources["architecture"] == "reference:openrouter"


def test_architecture_tokenizer_and_instruct_type_are_never_guessed():
    fields, _ = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(architecture=architecture(modality="text->text")),
    )
    assert fields["architecture"]["tokenizer"] is None
    assert fields["architecture"]["instruct_type"] is None


# ── (c) + (d) reference filling gaps, upstream precedence ────────────


def test_reference_fills_gaps_the_upstream_left_empty():
    """OpenAI-style upstream (name only) completed by the OpenRouter reference."""
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(name="gpt-4o", created=1715367049, context_length=None),
        reference=subset(
            name="OpenAI: GPT-4o",
            description="GPT-4o is OpenAI's flagship multimodal model.",
            canonical_slug="openai/gpt-4o-2024-11-20",
            hugging_face_id="openai/gpt-4o",
            knowledge_cutoff="2024-06",
            architecture=architecture(modality="text+image->text", tokenizer="GPT"),
            supported_parameters=["tools", "tool_choice", "temperature"],
            context_length=128000,
        ),
        reference_source="openrouter",
    )
    # name came from upstream -> reference value must NOT win
    assert fields["name"] == "gpt-4o"
    assert fields["description"] == "GPT-4o is OpenAI's flagship multimodal model."
    assert fields["canonical_slug"] == "openai/gpt-4o-2024-11-20"
    assert fields["hugging_face_id"] == "openai/gpt-4o"
    assert fields["knowledge_cutoff"] == "2024-06"
    assert fields["supported_parameters"] == ["tools", "tool_choice", "temperature"]
    assert fields["architecture"]["modality"] == "text+image->text"
    assert fields["architecture"]["input_modalities"] == ["text", "image"]
    assert fields["context_length"] == 128000

    assert sources["architecture"] == "reference:openrouter"
    assert sources["supported_parameters"] == "reference:openrouter"
    assert sources["context_length"] == "reference:openrouter"
    assert "name" not in sources  # came from the upstream
    assert "pricing" not in sources  # still unknown


def test_upstream_wins_over_reference_and_is_absent_from_metadata_sources():
    upstream = subset(
        name="Upstream Name",
        supported_parameters=["tools"],
        architecture=architecture(modality="text+image->text"),
        context_length=200000,
        pricing={"prompt": "0.000001", "completion": "0.000002"},
        top_provider={
            "context_length": 200000,
            "max_completion_tokens": 8192,
            "is_moderated": True,
        },
    )
    reference = subset(
        name="Reference Name",
        supported_parameters=["other"],
        architecture=architecture(modality="text->text"),
        context_length=999,
        pricing={"prompt": "9.99"},
        top_provider={
            "context_length": 999,
            "max_completion_tokens": 1,
            "is_moderated": False,
        },
    )
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=upstream,
        reference=reference,
        reference_source="openrouter",
    )

    assert fields["name"] == "Upstream Name"
    assert fields["supported_parameters"] == ["tools"]
    assert fields["architecture"]["modality"] == "text+image->text"
    assert fields["context_length"] == 200000
    assert fields["pricing"] == {"prompt": "0.000001", "completion": "0.000002"}
    assert fields["top_provider"] == {
        "context_length": 200000,
        "max_completion_tokens": 8192,
        "is_moderated": True,
    }

    # Every section came from this route's own upstream -> map is empty.
    assert sources == {}


def test_overrides_win_over_upstream_and_report_the_override_label():
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            context_length=128000,
            top_provider={"context_length": 128000, "max_completion_tokens": 4096},
            name="gpt-4o",
        ),
        overrides={"context_window": 1048576, "max_tokens": 8192, "temperature": 1.0},
    )
    assert fields["context_length"] == 1048576
    assert fields["top_provider"]["context_length"] == 1048576
    assert fields["top_provider"]["max_completion_tokens"] == 8192
    assert sources["context_length"] == "override"
    assert sources["top_provider"] == "override"


def test_local_context_is_used_when_nothing_else_knows_a_context():
    fields, sources = resolve_model_metadata(
        "ai-node-local/local/gemma-3-27b",
        local={"served_by": "local", "context": 32768, "max_tokens": 4096},
    )
    assert fields["context_length"] == 32768
    assert sources["context_length"] == "local"
    assert fields["top_provider"]["context_length"] == 32768
    assert sources["top_provider"] == "local"


# ── (e) pricing: no fabrication, ever ────────────────────────────────


def test_local_pricing_is_grounded_zeros():
    fields, sources = resolve_model_metadata(
        "ai-node-local/local/gemma-3-27b", local={"served_by": "local"}
    )
    expected = {key: "0" for key in LOCAL_PRICING_KEYS}
    expected["local"] = "true"
    assert fields["pricing"] == expected
    assert sources["pricing"] == "local"
    assert all(isinstance(value, str) for value in fields["pricing"].values())


def test_local_pricing_ignores_a_reference_price_block():
    fields, sources = resolve_model_metadata(
        "ai-node-local/local/gemma-3-27b",
        reference=subset(pricing={"prompt": "0.000003"}),
        reference_source="openrouter",
        local={"served_by": "local"},
    )
    assert fields["pricing"]["prompt"] == "0"
    assert fields["pricing"]["local"] == "true"
    assert sources["pricing"] == "local"


def test_upstream_pricing_is_passed_through_verbatim():
    """The exact block from the live OpenRouter payload, values untouched."""
    upstream_pricing = {
        "prompt": "0.000003",
        "completion": "0.000015",
        "web_search": "0.01",
        "input_cache_read": "0.0000003",
        "input_cache_write": "0.00000375",
        "input_cache_write_1h": "0.000006",
    }
    fields, sources = resolve_model_metadata(
        "openrouter/anthropic/claude-sonnet-4.6",
        upstream=subset(pricing=dict(upstream_pricing)),
    )
    assert fields["pricing"] == upstream_pricing
    # Values are verbatim strings, not re-parsed floats, and not mutated.
    assert fields["pricing"]["prompt"] == "0.000003"
    assert fields["pricing"]["input_cache_read"] == "0.0000003"
    assert all(isinstance(value, str) for value in fields["pricing"].values())
    # Upstream pricing is not reported as a foreign source.
    assert "pricing" not in sources


def test_pricing_is_none_when_nothing_is_advertised():
    """A cloud route that advertises no pricing and has no reference entry for
    the model reports None: no zero block, no borrowed number. (Guardian never
    invents a price for a route whose cost it does not know.)"""
    fields, sources = resolve_model_metadata(
        "nvidia/meta-llama/llama-3.3-70b-instruct",
        upstream=subset(name="Llama 3.3 70B Instruct"),
    )
    assert fields["pricing"] is None
    assert "pricing" not in sources

    # An empty advertised block counts as unknown too.
    empty, _ = resolve_model_metadata(
        "nvidia/meta-llama/llama-3.3-70b-instruct", upstream=subset(pricing={})
    )
    assert empty["pricing"] is None


def test_a_cloud_route_never_borrows_a_reference_price():
    """A cloud route that advertises no price of its own reports None.

    The reference price describes the reference provider's route (OpenRouter's),
    not this route's (NVIDIA's): stating it would be a fabricated cost. This
    mirrors the sibling wiring test
    ``test_reference_never_changes_a_price_it_does_not_serve``.
    """
    fields, sources = resolve_model_metadata(
        "nvidia/openai/gpt-4o",
        upstream=subset(name="gpt-4o", pricing=None),
        reference=subset(
            name="OpenAI: GPT-4o",
            supported_parameters=["tools"],
            pricing={"prompt": "0.0000025", "completion": "0.00001"},
        ),
        reference_source="openrouter",
    )
    assert fields["pricing"] is None
    assert "pricing" not in sources
    # The same reference entry still fills the fields it legitimately describes.
    assert fields["supported_parameters"] == ["tools"]
    assert sources["supported_parameters"] == "reference:openrouter"


def test_the_pricing_helper_never_borrows_the_reference_block():
    """``pricing`` is a route property (spec section 3.2): even the low-level
    helper refuses the reference block, so no caller can borrow a price."""
    value, label = synthesize_pricing(
        served_by="cloud",
        upstream=subset(pricing=None),
        reference=subset(pricing={"prompt": "0.0000025"}),
        local=None,
        reference_source="openrouter",
    )
    assert (value, label) == (None, None)


def test_top_provider_is_moderated_is_never_borrowed_from_the_reference():
    """Spec section 3.2's asymmetry: ``context_length`` and
    ``max_completion_tokens`` are model-shaped and may be filled from the
    reference in the same call, while ``is_moderated`` describes the *route* -
    one provider may moderate a model that another serves unfiltered - and must
    come only from this route's own upstream advertisement.
    """
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            top_provider={
                "context_length": None,
                "max_completion_tokens": None,
                "is_moderated": None,
            }
        ),
        reference=subset(
            context_length=128000,
            top_provider={
                "context_length": 128000,
                "max_completion_tokens": 16384,
                "is_moderated": True,
            },
        ),
        reference_source="openrouter",
    )
    assert fields["top_provider"]["is_moderated"] is None
    # ... while the model-shaped sub-keys are filled from the reference.
    assert fields["top_provider"]["context_length"] == 128000
    assert fields["top_provider"]["max_completion_tokens"] == 16384
    assert sources["top_provider"] == "reference:openrouter"


def test_top_provider_is_moderated_comes_from_the_routes_own_upstream():
    """The route's own advertisement is the only source, and ``False`` is a real
    answer that the reference may not overwrite."""
    fields, sources = resolve_model_metadata(
        "openai/openai/gpt-4o",
        upstream=subset(
            top_provider={
                "context_length": 128000,
                "max_completion_tokens": 16384,
                "is_moderated": False,
            }
        ),
        reference=subset(
            top_provider={
                "context_length": 128000,
                "max_completion_tokens": 16384,
                "is_moderated": True,
            }
        ),
        reference_source="openrouter",
    )
    assert fields["top_provider"]["is_moderated"] is False
    assert "top_provider" not in sources


def test_synthesize_pricing_helper_labels():
    value, label = synthesize_pricing(
        served_by="local", upstream=None, reference=None, local=None
    )
    assert label == "local"
    assert value is not None and value["local"] == "true"

    value, label = synthesize_pricing(
        served_by="cloud",
        upstream=subset(pricing={"prompt": "1"}),
        reference=subset(pricing={"prompt": "2"}),
        local=None,
    )
    assert (value, label) == ({"prompt": "1"}, "upstream")

    value, label = synthesize_pricing(
        served_by="cloud",
        upstream=None,
        reference=subset(pricing={"prompt": "2"}),
        local=None,
        reference_source="openrouter",
    )
    # Route property (spec 3.2): the reference block is never consulted.
    assert (value, label) == (None, None)

    value, label = synthesize_pricing(
        served_by="cloud", upstream=None, reference=None, local=None
    )
    assert (value, label) == (None, None)

    # A local facts dict alone is enough to make the route local.
    value, label = synthesize_pricing(
        served_by=None, upstream=None, reference=None, local={"served_by": "local"}
    )
    assert label == "local"


# ── (f) local supported_parameters honesty ───────────────────────────


def test_local_supported_parameters_are_honest_and_gated():
    base = local_supported_parameters({"served_by": "local"})
    assert base == list(LOCAL_SUPPORTED_PARAMETERS)
    assert "grammar" not in base
    assert "tools" not in base
    assert "tool_choice" not in base

    with_grammar = local_supported_parameters(
        {"served_by": "local", "grammar_decoding": True}
    )
    assert "grammar" in with_grammar
    assert "tools" not in with_grammar

    with_tools = local_supported_parameters({"served_by": "local", "tool_support": True})
    assert "tools" in with_tools
    assert "tool_choice" in with_tools
    assert "grammar" not in with_tools

    everything = local_supported_parameters(
        {"served_by": "local", "grammar_decoding": True, "tool_support": True}
    )
    assert everything == [
        *LOCAL_SUPPORTED_PARAMETERS,
        "grammar",
        "tools",
        "tool_choice",
    ]

    # Explicitly false flags never advertise the capability.
    off = local_supported_parameters(
        {"served_by": "local", "grammar_decoding": False, "tool_support": False}
    )
    assert "grammar" not in off and "tools" not in off and "tool_choice" not in off

    # Never advertise a parameter Guardian's local path would drop.
    forbidden = {"logit_bias", "min_p", "repetition_penalty", "include_reasoning"}
    assert forbidden.isdisjoint(set(everything))


def test_local_route_uses_the_local_parameter_list_not_the_upstream_advertisement():
    fields, sources = resolve_model_metadata(
        "ai-node-local/local/gemma-3-27b",
        upstream=subset(supported_parameters=["tools", "logit_bias", "include_reasoning"]),
        local={"served_by": "local", "grammar_decoding": False, "tool_support": True},
    )
    assert fields["supported_parameters"] == [
        *LOCAL_SUPPORTED_PARAMETERS,
        "tools",
        "tool_choice",
    ]
    assert sources["supported_parameters"] == "local"


def test_supported_parameters_fall_through_upstream_reference_none():
    value, label = synthesize_supported_parameters(
        upstream=subset(supported_parameters=["tools", "reasoning"]),
        reference=subset(supported_parameters=["other"]),
        local=None,
    )
    assert (value, label) == (["tools", "reasoning"], "upstream")

    value, label = synthesize_supported_parameters(
        upstream=subset(),
        reference=subset(supported_parameters=["tools", "reasoning"]),
        local=None,
        reference_source="openrouter",
    )
    assert (value, label) == (["tools", "reasoning"], "reference:openrouter")

    value, label = synthesize_supported_parameters(
        upstream=subset(), reference=subset(), local=None
    )
    assert (value, label) == (None, None)


# ── top_provider helper ──────────────────────────────────────────────


def test_synthesize_top_provider_precedence_and_skeleton():
    value, label = synthesize_top_provider(
        upstream=subset(),
        reference=subset(),
        context_length=None,
        overrides=None,
    )
    assert value == {key: None for key in TOP_PROVIDER_KEYS}
    assert label == "derived"

    value, label = synthesize_top_provider(
        upstream=subset(
            top_provider={"context_length": 128000, "max_completion_tokens": 4096}
        ),
        reference=subset(
            top_provider={"max_completion_tokens": 8192, "is_moderated": False}
        ),
        context_length=128000,
        overrides=None,
    )
    assert value == {
        "context_length": 128000,
        "max_completion_tokens": 4096,  # the route's own upstream wins
        "is_moderated": None,  # route property: never borrowed from the reference
    }
    assert label == "upstream"

    # The model-shaped sub-key is still filled from the reference when the route
    # advertises none, and the fill is reported.
    value, label = synthesize_top_provider(
        upstream=subset(
            top_provider={"context_length": 128000, "max_completion_tokens": None}
        ),
        reference=subset(
            top_provider={"max_completion_tokens": 8192, "is_moderated": False}
        ),
        context_length=128000,
        overrides=None,
        reference_source="openrouter",
    )
    assert value["max_completion_tokens"] == 8192
    assert value["is_moderated"] is None
    assert label == "reference:openrouter"

    value, label = synthesize_top_provider(
        upstream=subset(top_provider={"context_length": 1000000}),
        reference=None,
        context_length=131072,
        overrides=None,
    )
    assert value["context_length"] == 131072  # Guardian's resolved value wins
    assert label == "derived"  # ... and it is nobody else's number


# ── endpoint_entry_fields (spec section 4) ───────────────────────────

ENDPOINT_KEYS = (
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


def test_endpoint_entry_always_carries_every_section_4_key():
    entry = endpoint_entry_fields(
        model_id="openrouter/anthropic/claude-sonnet-4.6",
        provider_name="Anthropic",
        model_name="Anthropic: Claude Sonnet 4.6",
        context_length=1000000,
        pricing={"prompt": "0.000003"},
        tag="anthropic",
        quantization=None,
        max_completion_tokens=128000,
        supported_parameters=["tools"],
        supports_tools=True,
        status=0,
    )
    assert tuple(entry) == ENDPOINT_KEYS
    assert entry["name"] == "Anthropic | anthropic/claude-sonnet-4.6"
    assert entry["model_id"] == "openrouter/anthropic/claude-sonnet-4.6"
    assert entry["model_name"] == "Anthropic: Claude Sonnet 4.6"
    assert entry["provider_name"] == "Anthropic"
    assert entry["tag"] == "anthropic"
    assert entry["status"] == 0
    assert entry["supports_tool_choice"] == {
        "none": True,
        "auto": True,
        "required": True,
        "function": True,
    }
    for key in (
        "quantization",
        "max_prompt_tokens",
        "native_tools",
        "supports_implicit_caching",
        "supports_image_reference",
        "uptime_last_30m",
        "uptime_last_5m",
        "uptime_last_1d",
        "latency_last_30m",
        "throughput_last_30m",
    ):
        assert entry[key] is None, key


def test_endpoint_entry_has_no_grounded_value_for_unknown_fields():
    entry = endpoint_entry_fields(model_id="ai-node-local/local/gemma-3-27b")
    assert tuple(entry) == ENDPOINT_KEYS
    assert entry["model_id"] == "ai-node-local/local/gemma-3-27b"
    # Unknown tool capability -> None, never a fabricated all-false map.
    assert entry["supports_tool_choice"] is None
    assert entry["name"] is None  # no grounded provider name
    for key in ENDPOINT_KEYS:
        if key in ("model_id", "supports_tool_choice", "name"):
            continue
        assert entry[key] is None, key


def test_endpoint_entry_tool_choice_shapes():
    assert supports_tool_choice_shape(None) is None
    assert supports_tool_choice_shape(True) == {
        "none": True,
        "auto": True,
        "required": True,
        "function": True,
    }
    assert supports_tool_choice_shape(False) == {
        "none": True,
        "auto": False,
        "required": False,
        "function": False,
    }
    assert (
        endpoint_entry_fields(model_id="x", supports_tools=False)["supports_tool_choice"]
        == supports_tool_choice_shape(False)
    )


def test_endpoint_entry_is_completable_from_a_section_4_shaped_dict():
    """Sibling code holds a §4-shaped dict and completes it with ``fn(**fields)``:
    every §4 key must therefore be an accepted keyword."""
    fields = {
        "name": "Groq | meta-llama/llama-3.3-70b-instruct",
        "model_id": "failover/gpu",
        "model_name": "Meta Llama: Llama 3.3 70b Instruct",
        "context_length": 131072,
        "pricing": {"prompt": "0.00000059"},
        "provider_name": "Groq",
        "tag": "failover/gpu",
        "quantization": None,
        "max_completion_tokens": 8192,
        "max_prompt_tokens": None,
        "supported_parameters": ["tools", "temperature"],
        "supports_tool_choice": {
            "none": True,
            "auto": True,
            "required": True,
            "function": True,
        },
        "native_tools": None,
        "supports_implicit_caching": None,
        "supports_image_reference": None,
        "status": 0,
        "uptime_last_30m": None,
        "uptime_last_5m": None,
        "uptime_last_1d": None,
        "latency_last_30m": None,
        "throughput_last_30m": None,
    }
    assert tuple(fields) == ENDPOINT_KEYS
    entry = endpoint_entry_fields(**fields)
    assert tuple(entry) == ENDPOINT_KEYS
    assert entry == fields


def test_endpoint_entry_passes_grounded_values_through_untouched():
    entry = endpoint_entry_fields(
        model_id="failover/gpu",
        identity_key="meta-llama/llama-3.3-70b-instruct",
        provider_name="Groq",
        model_name="Meta Llama: Llama 3.3 70b Instruct",
        name="Groq | meta-llama/llama-3.3-70b-instruct",
        context_length=131072,
        pricing={"prompt": "0.00000059"},
        tag="failover/gpu",
        quantization="fp8",
        max_completion_tokens=8192,
        max_prompt_tokens=4096,
        supported_parameters=["tools", "temperature"],
        supports_tools=True,
        native_tools={"groq:code_execution": {"type": "python"}},
        supports_implicit_caching=False,
        supports_image_reference=False,
        status=3,
        uptime_last_30m=99.98,
        uptime_last_5m=100.0,
        uptime_last_1d=99.9,
        latency_last_30m=0.42,
        throughput_last_30m=310.5,
    )
    assert entry["name"] == "Groq | meta-llama/llama-3.3-70b-instruct"
    assert entry["pricing"] == {"prompt": "0.00000059"}
    assert entry["tag"] == "failover/gpu"
    assert entry["quantization"] == "fp8"
    assert entry["native_tools"] == {"groq:code_execution": {"type": "python"}}
    assert entry["supports_implicit_caching"] is False
    assert entry["supports_image_reference"] is False
    assert entry["status"] == 3
    assert entry["uptime_last_30m"] == 99.98
    assert entry["latency_last_30m"] == 0.42
    assert entry["throughput_last_30m"] == 310.5
