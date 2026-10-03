"""OpenRouter-parity presentation helpers for model metadata.

Pure functions over plain dicts: this module performs **no** I/O, no network
access and no filesystem access, and it deliberately imports nothing from
``app.proxy`` (no import-cycle risk). It implements the presentation contract
frozen in ``docs/OPENROUTER_PARITY.md``:

* section 3 - the per-model field contract, including the ``null``-discipline
  rule (every key is always present, unknown values are ``None``);
* section 3.1 - ``architecture`` synthesis in both directions;
* section 3.2 - ``pricing`` with grounded values only (never fabricate a price);
* section 3.3 - ``top_provider``;
* section 3.4 - ``supported_parameters``, including the honest local list;
* section 4 - one entry of the ``endpoints`` list;
* section 5 - the ``metadata_sources`` provenance map.

Precedence for every field (spec section 3): provider config ``overrides`` ->
this route's own ``upstream`` -> ``reference`` catalog -> grounded synthesis
(``local``) -> ``None``. The one exception is ``links``, which is always
Guardian's own route so ``canonical_slug``-style upstream links never leak out.

``pricing`` and ``top_provider.is_moderated`` are **route** properties and are
therefore never filled from the reference catalog (spec section 3.2): OpenRouter's
price for ``openai/gpt-4o`` says nothing about what another provider charges for
the same model. Everything else in section 3 describes the *model* and may be
completed from the reference. As the catalog always emits full skeletons with
``None`` for unknown values, every merge decision is made per field (or per
sub-key), never per block - a present-but-empty block is a gap, not an answer.
"""

from __future__ import annotations

import re
import time
from typing import Any

__all__ = [
    "ARCHITECTURE_KEYS",
    "LOCAL_PRICING_KEYS",
    "LOCAL_SUPPORTED_PARAMETERS",
    "PARITY_FIELDS",
    "TOP_PROVIDER_KEYS",
    "derive_name",
    "endpoint_entry_fields",
    "endpoints_link",
    "local_supported_parameters",
    "resolve_model_metadata",
    "split_identity",
    "supports_tool_choice_shape",
    "synthesize_architecture",
    "synthesize_pricing",
    "synthesize_supported_parameters",
    "synthesize_top_provider",
]

# ── Field contract ───────────────────────────────────────────────────

#: Every key of the spec's section-3 table. ``resolve_model_metadata`` always
#: returns all of them; unknown values are ``None`` (OpenRouter's own
#: convention: it returns ``"knowledge_cutoff": null`` rather than omitting
#: the key).
PARITY_FIELDS: tuple[str, ...] = (
    "id",
    "object",
    "created",
    "owned_by",
    "permission",
    "served_by",
    "provider",
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
    "reasoning",
)

ARCHITECTURE_KEYS: tuple[str, ...] = (
    "modality",
    "input_modalities",
    "output_modalities",
    "tokenizer",
    "instruct_type",
)

TOP_PROVIDER_KEYS: tuple[str, ...] = (
    "context_length",
    "max_completion_tokens",
    "is_moderated",
)

#: Pricing keys zeroed for a local route (spec section 3.2: "all known keys
#: ``"0"``, plus ``"local": "true"``"). Only the keys the spec itself names are
#: zeroed: the modality add-on keys OpenRouter also publishes (``image``,
#: ``audio``, ``image_output``, ...) describe capabilities Guardian's local path
#: does not bill at all, and asserting a price for a capability a route does not
#: have is exactly what the "never fabricate" rule forbids.
LOCAL_PRICING_KEYS: tuple[str, ...] = (
    "prompt",
    "completion",
    "web_search",
    "input_cache_read",
    "input_cache_write",
    "input_cache_write_1h",
)

#: Parameters Guardian's local path genuinely forwards (spec section 3.4).
LOCAL_SUPPORTED_PARAMETERS: tuple[str, ...] = (
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "stop",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "response_format",
    "structured_outputs",
)

# ── Brand / token naming ─────────────────────────────────────────────

#: Known brand capitalisation. Keys are lowercase; values are the display form.
_BRAND_NAMES: dict[str, str] = {
    "ai21": "AI21",
    "aion-labs": "Aion Labs",
    "alibaba": "Alibaba",
    "allenai": "AllenAI",
    "amazon": "Amazon",
    "anthropic": "Anthropic",
    "arcee-ai": "Arcee AI",
    "baidu": "Baidu",
    "bytedance": "ByteDance",
    "cognitivecomputations": "Cognitive Computations",
    "cohere": "Cohere",
    "deepseek": "DeepSeek",
    "google": "Google",
    "gryphe": "Gryphe",
    "ibm-granite": "IBM Granite",
    "inclusionai": "InclusionAI",
    "infermatic": "Infermatic",
    "inflection": "Inflection",
    "internlm": "InternLM",
    "liquid": "Liquid",
    "meta": "Meta",
    "meta-llama": "Meta Llama",
    "microsoft": "Microsoft",
    "minimax": "MiniMax",
    "minimaxai": "MinimaxAI",
    "mistral": "Mistral",
    "mistralai": "Mistral AI",
    "moonshot": "Moonshot",
    "moonshotai": "MoonshotAI",
    "nex-agi": "Nex AGI",
    "nous-research": "Nous Research",
    "nousresearch": "Nous Research",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "opengvlab": "OpenGVLab",
    "openrouter": "OpenRouter",
    "perplexity": "Perplexity",
    "qwen": "Qwen",
    "sao10k": "Sao10K",
    "stepfun": "StepFun",
    "tencent": "Tencent",
    "thudm": "THUDM",
    "undi95": "Undi95",
    "x-ai": "xAI",
    "xai": "xAI",
    "z-ai": "Z-AI",
    "zhipu": "Zhipu",
}

#: Tokens whose display form is fixed rather than capitalised per segment. Covers
#: the acronyms (``gpt`` -> ``GPT``) and the vendor names that carry inner
#: capitals, so a model slug renders the way OpenRouter renders it
#: (``deepseek/deepseek-v4.1-flash`` -> ``DeepSeek: DeepSeek V4.1 Flash``).
_TOKEN_NAMES: dict[str, str] = {
    "ai": "AI",
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "gpt": "GPT",
    "llm": "LLM",
    "moe": "MoE",
    "ocr": "OCR",
    "r1": "R1",
    "stt": "STT",
    "tts": "TTS",
    "vl": "VL",
    "vlm": "VLM",
}

#: Model slug tokens that bind to the next token with a hyphen instead of a
#: space. OpenRouter renders ``openai/gpt-4o`` as ``OpenAI: GPT-4o`` but
#: ``claude-sonnet-4.6`` as ``Anthropic: Claude Sonnet 4.6``, so ``gpt`` is the
#: one prefix that keeps its hyphen (verified against the live
#: ``/api/v1/models`` id-to-name mapping: ``gpt-6.1-sol-pro`` ->
#: ``OpenAI: GPT-6.1 Sol Pro``).
_HYPHEN_BINDING_TOKENS: frozenset[str] = frozenset({"gpt"})

#: Parameter-size tokens (``70b``, ``27b``, ``a22b``) render their unit as ``B``,
#: matching OpenRouter's own naming (``qwen3.8-27b`` -> ``Qwen: Qwen3.8 27B``).
_PARAMETER_SIZE_RE = re.compile(r"^[a-z]?\d+(?:\.\d+)?b$")

#: Shape of OpenRouter's ``supports_tool_choice`` map.
_TOOL_CHOICE_KEYS: tuple[str, ...] = ("none", "auto", "required", "function")

# Provenance labels
_UPSTREAM = "upstream"
_OVERRIDE = "override"
_LOCAL = "local"
_DERIVED = "derived"

#: Which provenance label wins when one section drew on several sources: the
#: lowest rank is reported, so a section that was partly filled from the reference
#: catalog reads as a reference fill rather than as a purely upstream fact (spec
#: section 5: a section absent from ``metadata_sources`` came from the upstream
#: itself).
_LABEL_RANK: dict[str, int] = {
    "reference": 0,
    _OVERRIDE: 1,
    _LOCAL: 2,
    _DERIVED: 3,
    _UPSTREAM: 4,
}


# ── Small internal helpers ───────────────────────────────────────────


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _first_not_none(*values: Any) -> Any:
    """Return the first value that is not ``None`` (``None`` when all are)."""
    for value in values:
        if value is not None:
            return value
    return None


def _clean_str(value: Any) -> str | None:
    """Return a stripped string, or ``None`` when there is no usable string."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _reference_label(reference_source: str | None) -> str:
    """Provenance label for a value taken from the reference catalog."""
    name = _clean_str(reference_source)
    return f"reference:{name}" if name else "reference"


def _pick_label(labels: list[str]) -> str | None:
    """Pick the reported provenance label out of the contributing labels."""
    if not labels:
        return None
    return min(labels, key=lambda item: _LABEL_RANK.get(_label_kind(item), 99))


def _label_kind(label: str) -> str:
    """Reduce a provenance label to its rank key (``reference:x`` -> reference)."""
    return "reference" if label.startswith("reference") else label


def _local_dict(local: Any) -> dict[str, Any]:
    return _as_dict(local)


def _is_local_route(served_by: str | None, local: Any) -> bool:
    """True when the route is served by Guardian's own local backend.

    Grounded when either the caller says ``served_by == "local"`` or the
    ``local`` facts dict declares it. A ``local`` facts dict without an explicit
    ``served_by`` is still local by construction (the input contract documents
    the dict as local-model facts), so it counts as local too.
    """
    if isinstance(served_by, str) and served_by.strip().lower() == _LOCAL:
        return True
    loc = _local_dict(local)
    declared = _clean_str(loc.get("served_by"))
    if declared is not None:
        return declared.lower() == _LOCAL
    return bool(loc)


# ── Identity and naming ──────────────────────────────────────────────


def split_identity(model_id: str) -> str:
    """Return the identity key of a Guardian address.

    The identity key is everything after the first ``/`` (spec section 1), which
    is what a reference catalog such as OpenRouter's is keyed on:

    * ``"openai/openai/gpt-4o"`` -> ``"openai/gpt-4o"``
    * ``"openrouter/anthropic/claude-sonnet-4.6"`` -> ``"anthropic/claude-sonnet-4.6"``
    * ``"gpt-4o"`` -> ``"gpt-4o"`` (no provider segment)
    """
    if not isinstance(model_id, str):
        return ""
    _provider, sep, rest = model_id.partition("/")
    return rest if sep else model_id


def _capitalize_segment(segment: str) -> str:
    """Capitalise one ``-``/``_`` separated token."""
    lowered = segment.lower()
    named = _TOKEN_NAMES.get(lowered)
    if named is not None:
        return named
    if _PARAMETER_SIZE_RE.match(lowered):
        # "70b" -> "70B", "a22b" -> "A22B": the unit stays uppercase.
        return f"{lowered[:-1].capitalize()}B"
    return lowered.capitalize()


def _brand_display(brand: str) -> str:
    """Display form of a brand segment, with a deterministic generic fallback."""
    key = brand.strip().lower()
    mapped = _BRAND_NAMES.get(key)
    if mapped is not None:
        return mapped
    parts = [part for part in key.replace("_", "-").split("-") if part]
    if not parts:
        return brand
    return "-".join(_capitalize_segment(part) for part in parts)


def _model_display(model: str) -> str:
    """Display form of the model part of an identity key.

    ``-``/``_`` separated tokens become space separated words, except after a
    hyphen-binding token (``gpt``), which keeps its hyphen. A ``:variant`` suffix
    (OpenRouter's ``:batch``/``:free`` suffix convention) is rendered the way
    OpenRouter does, as a trailing ``(variant)``.
    """
    base, sep, variant = model.partition(":")
    tokens: list[str] = []
    previous = ""
    for token in base.replace("_", "-").split("-"):
        if not token:
            continue
        rendered = _capitalize_segment(token)
        if tokens and previous in _HYPHEN_BINDING_TOKENS:
            tokens[-1] = f"{tokens[-1]}-{rendered}"
        else:
            tokens.append(rendered)
        previous = token.lower()
    display = " ".join(tokens) if tokens else base
    cleaned_variant = _clean_str(variant)
    if sep and cleaned_variant is not None:
        display = f"{display} ({cleaned_variant})"
    return display


def derive_name(identity_key: str) -> str:
    """Derive a deterministic OpenRouter-style display name from an identity key.

    * ``"openai/gpt-4o"`` -> ``"OpenAI: GPT-4o"``
    * ``"anthropic/claude-sonnet-4.6"`` -> ``"Anthropic: Claude Sonnet 4.6"``
    * ``"z-ai/glm-4.6"`` -> ``"Z-AI: GLM 4.6"``
    * a bare key without ``/`` -> the key itself, unchanged.

    Brand capitalisation uses a known-brand table with a deterministic generic
    fallback (per-segment capitalisation with a small acronym table), so an
    unknown brand still renders predictably instead of being guessed at.
    """
    key = _clean_str(identity_key)
    if key is None:
        return ""
    brand, sep, model = key.partition("/")
    if not sep:
        return key
    return f"{_brand_display(brand)}: {_model_display(model)}"


def endpoints_link(model_id: str) -> dict[str, str]:
    """Guardian's own endpoints route for ``model_id``.

    Never the upstream's link: OpenRouter advertises
    ``/api/v1/models/{slug}/endpoints``, which does not exist on Guardian.
    """
    return {"details": f"/v1/models/{model_id}/endpoints"}


# ── Architecture (spec 3.1) ──────────────────────────────────────────


def _split_modality_string(modality: Any) -> tuple[list[str] | None, list[str] | None]:
    """Parse ``"text+image->text"`` into ``(["text", "image"], ["text"])``."""
    if not isinstance(modality, str) or "->" not in modality:
        return None, None
    left, _, right = modality.partition("->")
    inputs = [part for part in left.split("+") if part]
    outputs = [part for part in right.split("+") if part]
    return (inputs or None, outputs or None)


def _join_modality_string(
    inputs: list[str] | None, outputs: list[str] | None
) -> str | None:
    """Build ``"text+image->text"`` from the modality lists."""
    if not inputs or not outputs:
        return None
    return "+".join(inputs) + "->" + "+".join(outputs)


def _architecture_components(architecture: Any) -> tuple[Any, Any, Any, Any, Any] | None:
    """Normalise one source's architecture into a 5-tuple of components.

    Fills ``modality`` from the lists when only the lists are known and fills the
    lists from the string when only the string is known - both directions are
    required so a reference entry can complete a partial upstream entry.
    ``tokenizer``/``instruct_type`` are never guessed.
    """
    arch = _as_dict(architecture)
    if not arch:
        return None
    modality = _clean_str(arch.get("modality"))
    inputs = arch.get("input_modalities")
    outputs = arch.get("output_modalities")
    inputs = list(inputs) if isinstance(inputs, list) and inputs else None
    outputs = list(outputs) if isinstance(outputs, list) and outputs else None
    if inputs is None or outputs is None:
        from_string_in, from_string_out = _split_modality_string(modality)
        inputs = inputs if inputs is not None else from_string_in
        outputs = outputs if outputs is not None else from_string_out
    if modality is None:
        modality = _join_modality_string(inputs, outputs)
    components = (
        modality,
        inputs,
        outputs,
        _clean_str(arch.get("tokenizer")),
        arch.get("instruct_type"),
    )
    if all(component is None for component in components):
        return None
    return components


def _local_architecture(local: Any) -> tuple[Any, Any, Any, Any, Any] | None:
    """Derive the architecture of a local route from grounded local facts.

    Only grounded facts are used: ``model_type`` (an embedding model does not
    produce text) and the configured vision capability. ``tokenizer`` and
    ``instruct_type`` stay ``None`` - llama.cpp does not advertise them here.
    """
    loc = _local_dict(local)
    if not loc:
        return None
    model_type = _clean_str(loc.get("model_type"))
    if model_type is not None and model_type.lower() == "embedding":
        return ("text->embedding", ["text"], ["embedding"], None, None)
    vision = _as_dict(loc.get("vision"))
    if vision.get("configured") is True:
        return ("text+image->text", ["text", "image"], ["text"], None, None)
    return ("text->text", ["text"], ["text"], None, None)


def synthesize_architecture(
    upstream: dict | None,
    reference: dict | None,
    local: dict | None,
    *,
    reference_source: str | None = None,
) -> tuple[dict | None, str | None]:
    """Merge ``architecture`` from upstream, reference and local facts.

    Precedence is upstream -> reference -> local. The modality triple
    (``modality`` + ``input_modalities`` + ``output_modalities``) is taken as a
    whole from the first source that can state it completely - either as a
    string or as both lists - so a completed entry is never internally
    inconsistent (an upstream ``input_modalities`` of ``["text"]`` never gets
    paired with a reference modality string of ``"text+image->text"``). When no
    single source can state the triple completely, the three components are
    merged one by one and the modality string is derived from the merged lists.
    ``tokenizer``/``instruct_type`` are merged independently and are ``None``
    when unknown - never guessed.

    Deliberate choice (the spec's section 3.1 rule): an entry with no modality
    information at all still gets the full 5-key object with all values ``None``
    - never ``None`` for the whole object, and never an omitted key. That
    skeleton carries no information, so it is reported as ``"derived"``
    (Guardian's own shape, not an upstream fact, and therefore listed in
    ``metadata_sources``).
    """
    ref_label = _reference_label(reference_source)
    candidates = (
        (_UPSTREAM, _architecture_components(_as_dict(upstream).get("architecture"))),
        (ref_label, _architecture_components(_as_dict(reference).get("architecture"))),
        # Local facts only describe a route Guardian itself serves locally: a
        # ``local`` dict supplied for a cloud route must not invent a modality.
        (_LOCAL, _local_architecture(local) if _is_local_route(None, local) else None),
    )
    sources: list[tuple[str, tuple[Any, Any, Any, Any, Any]]] = [
        (label, components) for label, components in candidates if components is not None
    ]

    labels: list[str] = []
    modality_triple: tuple[Any, Any, Any] | None = None
    for label, components in sources:
        if all(value is not None for value in components[:3]):
            modality_triple = (components[0], components[1], components[2])
            labels.append(label)
            break
    if modality_triple is None:
        modality: Any = None
        inputs: Any = None
        outputs: Any = None
        for label, components in sources:
            contributed = False
            if inputs is None and components[1] is not None:
                inputs, contributed = components[1], True
            if outputs is None and components[2] is not None:
                outputs, contributed = components[2], True
            if modality is None and components[0] is not None:
                modality, contributed = components[0], True
            if contributed:
                labels.append(label)
        if modality is None:
            modality = _join_modality_string(inputs, outputs)
        modality_triple = (modality, inputs, outputs)

    tokenizer: Any = None
    instruct_type: Any = None
    for label, components in sources:
        contributed = False
        if tokenizer is None and components[3] is not None:
            tokenizer, contributed = components[3], True
        if instruct_type is None and components[4] is not None:
            instruct_type, contributed = components[4], True
        if contributed:
            labels.append(label)

    modality, inputs, outputs = modality_triple
    architecture = dict(
        zip(
            ARCHITECTURE_KEYS,
            (modality, inputs, outputs, tokenizer, instruct_type),
        )
    )
    if not labels:
        return architecture, _DERIVED
    return architecture, _pick_label(labels)


# ── Pricing (spec 3.2) ───────────────────────────────────────────────


def synthesize_pricing(
    *,
    served_by: str | None,
    upstream: dict | None,
    reference: dict | None,
    local: dict | None,
    reference_source: str | None = None,
) -> tuple[dict | None, str | None]:
    """Resolve ``pricing`` without ever fabricating a number.

    * local route -> grounded zeros plus ``"local": "true"``, provenance
      ``"local"`` (local inference costs nothing - a fact, not a guess);
    * otherwise the route's own upstream pricing block, passed through verbatim
      with every value left untouched, provenance ``"upstream"``;
    * otherwise ``None`` - never an invented number.

    ``pricing`` is a property of the **route**, not of the model (spec section
    3.2): the reference catalog is deliberately not a pricing source, so the
    ``reference`` argument is accepted for signature stability and never
    consulted. OpenRouter charges for ``openai/gpt-4o`` while another provider
    may serve the same model for nothing, so borrowing the number would state
    something false about what this route costs.
    """
    if _is_local_route(served_by, local):
        pricing = {key: "0" for key in LOCAL_PRICING_KEYS}
        pricing["local"] = "true"
        return pricing, _LOCAL

    upstream_pricing = _as_dict(upstream).get("pricing")
    if isinstance(upstream_pricing, dict) and upstream_pricing:
        return dict(upstream_pricing), _UPSTREAM

    return None, None


# ── Top provider (spec 3.3) ──────────────────────────────────────────


def _context_length_label(
    *,
    value: Any,
    override: Any,
    upstream_value: Any,
    reference_value: Any,
    reference_source: str | None,
) -> str:
    """Infer where a resolved context length came from by value comparison.

    ``context_length`` arrives as an already-resolved number, so its origin is
    recovered by matching it against the sources in precedence order; an
    unmatched value is reported as ``"derived"`` (Guardian resolved it itself).
    """
    if value is None:
        return _DERIVED
    if override is not None and value == override:
        return _OVERRIDE
    if upstream_value is not None and value == upstream_value:
        return _UPSTREAM
    if reference_value is not None and value == reference_value:
        return _reference_label(reference_source)
    return _DERIVED


def synthesize_top_provider(
    *,
    upstream: dict | None,
    reference: dict | None,
    context_length: int | None,
    overrides: dict | None,
    reference_source: str | None = None,
) -> tuple[dict | None, str | None]:
    """Resolve ``top_provider`` (spec section 3.3).

    * ``context_length``: the context Guardian itself resolved for the model
      (the value the caller passes in, which already folds in overrides and
      catalog data); falls back to the provider override, then upstream, then
      reference. It is model-shaped, so the reference may fill it.
    * ``max_completion_tokens``: override (``max_tokens``) -> upstream -> reference
      -> ``None``. Also model-shaped: how long the model can answer.
    * ``is_moderated``: the route's own upstream only, else ``None``. Moderation
      is a property of the *route* (spec section 3.2's model/route distinction),
      so the reference catalog is never consulted for it - one provider may
      moderate a model that another serves unfiltered.

    Every sub-key is resolved independently, because the catalog always emits the
    full skeleton with ``None`` for unknown: an all-``None`` upstream block is a
    gap, not an answer, and only a real value (including ``False``/``0``/``""``)
    blocks the next source.

    The object itself is always returned (its section-3 type is ``object``, not
    ``object|null``); an entry with no information at all is the all-``None``
    skeleton, reported as ``"derived"`` like the empty architecture skeleton.
    """
    up = _as_dict(_as_dict(upstream).get("top_provider"))
    ref = _as_dict(_as_dict(reference).get("top_provider"))
    override = _as_dict(overrides)
    ref_label = _reference_label(reference_source)

    context_override = _first_not_none(
        override.get("context_window"), override.get("context_length")
    )
    # The upstream's own top-level ``context_length`` counts as an upstream
    # statement of the same fact (OpenRouter publishes both), so it is compared
    # when recovering where a resolved value came from.
    upstream_context = _first_not_none(
        up.get("context_length"), _as_dict(upstream).get("context_length")
    )
    reference_context = _first_not_none(
        ref.get("context_length"), _as_dict(reference).get("context_length")
    )

    resolved_context = _first_not_none(
        context_length, context_override, upstream_context, reference_context
    )
    context_label = _context_length_label(
        value=resolved_context,
        override=context_override,
        upstream_value=upstream_context,
        reference_value=reference_context,
        reference_source=reference_source,
    )

    max_override = _first_not_none(
        override.get("max_tokens"), override.get("max_completion_tokens")
    )
    upstream_max = _first_not_none(up.get("max_completion_tokens"))
    reference_max = _first_not_none(ref.get("max_completion_tokens"))
    resolved_max = _first_not_none(max_override, upstream_max, reference_max)
    if resolved_max is None:
        max_label = None
    elif max_override is not None and resolved_max == max_override:
        max_label = _OVERRIDE
    elif upstream_max is not None and resolved_max == upstream_max:
        max_label = _UPSTREAM
    else:
        max_label = ref_label

    moderated = _first_not_none(up.get("is_moderated"))
    if moderated is None:
        moderated_label = None
    else:
        moderated_label = _UPSTREAM

    top_provider = {
        "context_length": resolved_context,
        "max_completion_tokens": resolved_max,
        "is_moderated": moderated,
    }
    labels = [
        label
        for label in (context_label if resolved_context is not None else None,
                      max_label, moderated_label)
        if label is not None
    ]
    if not labels:
        return top_provider, _DERIVED
    return top_provider, _pick_label(labels)


# ── Supported parameters (spec 3.4) ──────────────────────────────────


def local_supported_parameters(local: dict | None) -> list[str]:
    """The parameters Guardian's local path genuinely forwards (spec 3.4).

    ``grammar`` is added only when the model's config enables grammar decoding;
    ``tools``/``tool_choice`` only when the model config declares tool support.
    A capability Guardian would drop is never advertised.
    """
    loc = _local_dict(local)
    parameters = list(LOCAL_SUPPORTED_PARAMETERS)
    if loc.get("grammar_decoding") is True:
        parameters.append("grammar")
    if loc.get("tool_support") is True:
        parameters.append("tools")
        parameters.append("tool_choice")
    return parameters


def synthesize_supported_parameters(
    *,
    upstream: dict | None,
    reference: dict | None,
    local: dict | None,
    reference_source: str | None = None,
) -> tuple[list[str] | None, str | None]:
    """Resolve ``supported_parameters`` (spec section 3.4).

    * A local route uses the honest local list (never the upstream's
      advertisement, which would claim capabilities Guardian drops on that path).
    * Otherwise upstream passthrough verbatim, then the reference catalog to fill
      a silent upstream (the headline cross-provider win), then ``None``.
    """
    if _is_local_route(None, local):
        return local_supported_parameters(local), _LOCAL

    upstream_parameters = _as_dict(upstream).get("supported_parameters")
    if isinstance(upstream_parameters, list) and upstream_parameters:
        return list(upstream_parameters), _UPSTREAM

    reference_parameters = _as_dict(reference).get("supported_parameters")
    if isinstance(reference_parameters, list) and reference_parameters:
        return list(reference_parameters), _reference_label(reference_source)

    return None, None


# ── Endpoint entries (spec section 4) ────────────────────────────────


def supports_tool_choice_shape(supports_tools: bool | None) -> dict[str, bool] | None:
    """OpenRouter's ``supports_tool_choice`` map for a known tool capability.

    ``None`` in -> ``None`` out: an unknown capability is never rendered as a
    fabricated all-``False`` map. When the route genuinely has no tool support,
    only ``"none"`` stays true (asking for "no tool call" is always satisfiable)
    while ``auto``/``required``/``function`` are false.
    """
    if supports_tools is None:
        return None
    if supports_tools:
        return {key: True for key in _TOOL_CHOICE_KEYS}
    return {"none": True, "auto": False, "required": False, "function": False}


def endpoint_entry_fields(
    *,
    model_id: str,
    name: str | None = None,
    model_name: str | None = None,
    context_length: int | None = None,
    pricing: dict | None = None,
    provider_name: str | None = None,
    tag: str | None = None,
    quantization: str | None = None,
    max_completion_tokens: int | None = None,
    max_prompt_tokens: int | None = None,
    supported_parameters: list | None = None,
    supports_tool_choice: dict | None = None,
    native_tools: dict | None = None,
    supports_implicit_caching: bool | None = None,
    supports_image_reference: bool | None = None,
    status: int | None = None,
    uptime_last_30m: float | None = None,
    uptime_last_5m: float | None = None,
    uptime_last_1d: float | None = None,
    latency_last_30m: float | None = None,
    throughput_last_30m: float | None = None,
    identity_key: str | None = None,
    supports_tools: bool | None = None,
) -> dict[str, Any]:
    """Build one entry of the section-4 ``endpoints`` list.

    Every §4 key is accepted as a keyword of the same name, so a caller that
    already holds a §4-shaped dict can complete it with ``fn(**fields)``. Values
    are passed through exactly as supplied; a field Guardian has no grounded
    value for stays ``None`` (spec section 4: uptime, latency and throughput are
    ``null`` rather than fabricated, and ``native_tools`` is ``null`` unless a
    provider config declares it).

    Parameters
    ----------
    model_id:
        The Guardian address this endpoint route serves (required).
    name:
        OpenRouter-style endpoint label. When omitted it is composed as
        ``"{provider_name} | {identity_key}"`` - OpenRouter's own shape, e.g.
        ``"Claude Platform on AWS | anthropic/claude-4.6-sonnet-20260217"`` - or
        ``None`` when there is no grounded provider name to build it from.
    model_name:
        Display name of the model (the section-3 ``name`` of the parent entry).
    context_length:
        Context window Guardian resolved for this route.
    pricing:
        The route's own pricing map, verbatim; ``None`` when unknown.
    provider_name:
        Display name of the provider serving the route (e.g. ``"OpenAI"``).
    tag:
        Provider route tag, e.g. ``"azure/global"``, or the failover group name.
    quantization:
        Quantization of the route's weights, when the provider declares it.
    max_completion_tokens, max_prompt_tokens:
        Route completion/prompt limits; ``None`` when not advertised.
    supported_parameters:
        Parameters this specific route forwards (never a sibling route's list).
    supports_tool_choice:
        A grounded OpenRouter-shaped map, passed through verbatim when supplied.
    native_tools:
        OpenRouter's provider-native tool map; ``None`` unless declared.
    supports_implicit_caching, supports_image_reference:
        Provider-declared booleans; ``None`` when Guardian has no signal.
    status:
        ``0`` when healthy, non-zero when known-degraded, ``None`` when Guardian
        has no health signal for the route.
    uptime_last_30m, uptime_last_5m, uptime_last_1d, latency_last_30m,
    throughput_last_30m:
        Per-route time series; ``None`` unless a real measured value is supplied.
    identity_key:
        Identity key of the model; defaults to ``split_identity(model_id)`` and is
        used only to compose ``name``.
    supports_tools:
        Convenience input for callers that know only the boolean: it derives
        ``supports_tool_choice`` via :func:`supports_tool_choice_shape`. An
        explicit ``supports_tool_choice`` always wins over this derivation.
    """
    key = identity_key if identity_key is not None else split_identity(model_id)
    resolved_name = name
    if resolved_name is None and provider_name is not None:
        resolved_name = f"{provider_name} | {key}"
    resolved_tool_choice = supports_tool_choice
    if resolved_tool_choice is None:
        resolved_tool_choice = supports_tool_choice_shape(supports_tools)
    return {
        "name": resolved_name,
        "model_id": model_id,
        "model_name": model_name,
        "context_length": context_length,
        "pricing": pricing,
        "provider_name": provider_name,
        "tag": tag,
        "quantization": quantization,
        "max_completion_tokens": max_completion_tokens,
        "max_prompt_tokens": max_prompt_tokens,
        "supported_parameters": supported_parameters,
        "supports_tool_choice": resolved_tool_choice,
        "native_tools": native_tools,
        "supports_implicit_caching": supports_implicit_caching,
        "supports_image_reference": supports_image_reference,
        "status": status,
        "uptime_last_30m": uptime_last_30m,
        "uptime_last_5m": uptime_last_5m,
        "uptime_last_1d": uptime_last_1d,
        "latency_last_30m": latency_last_30m,
        "throughput_last_30m": throughput_last_30m,
    }


# ── Section-3 assembly ───────────────────────────────────────────────


def _sections_with_provenance() -> tuple[str, ...]:
    """Sections reported in ``metadata_sources`` (spec section 5)."""
    return (
        "name",
        "context_length",
        "architecture",
        "pricing",
        "top_provider",
        "supported_parameters",
    )


def _resolve_context_length(
    *,
    overrides: dict[str, Any],
    upstream: dict[str, Any],
    reference: dict[str, Any],
    local: dict[str, Any],
    context_length: int | None,
    reference_source: str | None,
) -> tuple[int | None, str | None]:
    """Resolve ``context_length`` per the section-3 precedence row.

    Provider config override -> upstream -> reference -> resolved context (the
    value the caller passes in, then grounded local facts).
    """
    override = _first_not_none(
        overrides.get("context_window"), overrides.get("context_length")
    )
    if override is not None:
        return override, _OVERRIDE
    upstream_value = upstream.get("context_length")
    if upstream_value is not None:
        return upstream_value, _UPSTREAM
    reference_value = reference.get("context_length")
    if reference_value is not None:
        return reference_value, _reference_label(reference_source)
    if context_length is not None:
        return context_length, _DERIVED
    local_value = local.get("context")
    if local_value is not None:
        return local_value, _LOCAL
    return None, None


def resolve_model_metadata(
    model_id: str,
    *,
    upstream: dict | None = None,
    reference: dict | None = None,
    reference_source: str | None = None,
    overrides: dict | None = None,
    local: dict | None = None,
    context_length: int | None = None,
    created: int | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Assemble the section-3 parity fields for one Guardian model entry.

    Returns ``(parity_fields, metadata_sources)``:

    * ``parity_fields`` contains every key of the spec's section-3 table,
      always present, ``None`` where unknown (the null discipline that is the
      point of the feature). Two values are structural rather than unknown and
      are therefore never ``None``: ``architecture`` and ``top_provider`` are
      the all-``None`` skeleton objects when nothing is known, and
      ``default_parameters`` falls back to ``{}`` exactly as the spec's table
      prescribes.
    * ``metadata_sources`` holds the section-5 provenance map: only sections
      whose value did not come from this route's own upstream. A section that
      came from the upstream is absent from the map.

    Three section-3 keys cannot be known from this function's inputs and are
    therefore ``None`` here, to be supplied by the caller's own entry: ``owned_by``
    and ``provider`` (the address's provider segment) and, when it declines to
    pass ``local``, ``served_by``. ``id``, ``object`` and ``permission`` are
    filled with their existing Guardian values so the contract is complete.

    ``created`` follows the spec's row: the caller's value, then upstream, then
    reference, then the request time. ``links`` is always Guardian's own route.

    Pricing policy (spec section 3.2): ``pricing`` and
    ``top_provider.is_moderated`` are properties of the *route*, so they are
    never taken from the reference catalog. A cloud route reports only the price
    its own upstream advertises - OpenRouter's price for ``openai/gpt-4o`` says
    nothing about what another provider charges for the same model, so borrowing
    it would state something false about what this route costs. Every other field
    describes the *model* and may be completed from the reference.

    The frozen ``synthesize_*`` helpers additionally accept an optional
    ``reference_source`` keyword (defaulting to ``None``) so that a label reads
    ``reference:<name>`` when the source name is known and stays the honest
    ``reference`` when it is not; the documented positional/keyword call forms
    are unchanged.
    """
    up = _as_dict(upstream)
    ref = _as_dict(reference)
    ov = _as_dict(overrides)
    loc = _local_dict(local)
    ref_label = _reference_label(reference_source)
    sources: dict[str, str] = {}

    # -- identity -----------------------------------------------------
    identity_key = split_identity(model_id)
    served_by = _clean_str(loc.get("served_by"))
    if served_by is None and loc:
        served_by = _LOCAL

    # -- simple upstream -> reference passthroughs ---------------------
    canonical_slug = _first_not_none(up.get("canonical_slug"), ref.get("canonical_slug"))
    hugging_face_id = _first_not_none(
        up.get("hugging_face_id"), ref.get("hugging_face_id")
    )
    description = _first_not_none(up.get("description"), ref.get("description"))
    per_request_limits = _first_not_none(
        up.get("per_request_limits"), ref.get("per_request_limits")
    )
    knowledge_cutoff = _first_not_none(
        up.get("knowledge_cutoff"), ref.get("knowledge_cutoff")
    )
    expiration_date = _first_not_none(
        up.get("expiration_date"), ref.get("expiration_date")
    )
    reasoning = _first_not_none(up.get("reasoning"), ref.get("reasoning"))

    default_parameters = _first_not_none(
        up.get("default_parameters"), ref.get("default_parameters")
    )
    if not isinstance(default_parameters, dict):
        default_parameters = {}

    resolved_created = _first_not_none(
        created, up.get("created"), ref.get("created")
    )
    if resolved_created is None:
        resolved_created = int(time.time())

    # -- name ---------------------------------------------------------
    upstream_name = _clean_str(up.get("name"))
    reference_name = _clean_str(ref.get("name"))
    if upstream_name is not None:
        name = upstream_name
    elif reference_name is not None:
        name = reference_name
        sources["name"] = ref_label
    else:
        name = derive_name(identity_key)
        sources["name"] = _DERIVED

    # -- context length ------------------------------------------------
    resolved_context, context_label = _resolve_context_length(
        overrides=ov,
        upstream=up,
        reference=ref,
        local=loc,
        context_length=context_length,
        reference_source=reference_source,
    )
    if context_label is not None and context_label != _UPSTREAM:
        sources["context_length"] = context_label

    # -- synthesized sections ------------------------------------------
    architecture, architecture_label = synthesize_architecture(
        upstream, reference, local, reference_source=reference_source
    )
    if architecture_label is not None and architecture_label != _UPSTREAM:
        sources["architecture"] = architecture_label

    pricing, pricing_label = synthesize_pricing(
        served_by=served_by,
        upstream=upstream,
        # ``pricing`` is a property of the route, not of the model (spec section
        # 3.2), so the reference catalog is not a pricing source and is not
        # passed here at all: a cloud route reports only its own advertisement,
        # and ``None`` when it has none.
        reference=None,
        local=local,
        reference_source=reference_source,
    )
    if pricing_label is not None and pricing_label != _UPSTREAM:
        sources["pricing"] = pricing_label

    top_provider, top_provider_label = synthesize_top_provider(
        upstream=upstream,
        reference=reference,
        context_length=resolved_context,
        overrides=overrides,
        reference_source=reference_source,
    )
    if (
        top_provider_label == _DERIVED
        and resolved_context is not None
        and resolved_context == loc.get("context")
    ):
        # A context length taken from grounded local facts is a local fact, not
        # an unexplained derivation.
        top_provider_label = _LOCAL
    if top_provider_label is not None and top_provider_label != _UPSTREAM:
        sources["top_provider"] = top_provider_label

    supported_parameters, parameters_label = synthesize_supported_parameters(
        upstream=upstream,
        reference=reference,
        local=local,
        reference_source=reference_source,
    )
    if parameters_label is not None and parameters_label != _UPSTREAM:
        sources["supported_parameters"] = parameters_label

    parity_fields: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": resolved_created,
        "owned_by": None,
        "permission": [],
        "served_by": served_by,
        "provider": None,
        "canonical_slug": canonical_slug,
        "hugging_face_id": hugging_face_id,
        "name": name,
        "description": description,
        "context_length": resolved_context,
        "architecture": architecture,
        "pricing": pricing,
        "top_provider": top_provider,
        "per_request_limits": per_request_limits,
        "supported_parameters": supported_parameters,
        "default_parameters": default_parameters,
        "knowledge_cutoff": knowledge_cutoff,
        "expiration_date": expiration_date,
        "links": endpoints_link(model_id),
        "reasoning": reasoning,
    }
    # Defensive: the contract is that every section-3 key is present.
    for key in PARITY_FIELDS:
        parity_fields.setdefault(key, None)
    metadata_sources = {
        section: sources[section]
        for section in _sections_with_provenance()
        if section in sources
    }
    return parity_fields, metadata_sources
