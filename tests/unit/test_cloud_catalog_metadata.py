"""OpenRouter-parity per-model metadata capture (cloud catalog).

Pins ``app.proxy.cloud_catalog`` metadata capture (docs/OPENROUTER_PARITY.md
§3): the curated subset stored under a provider catalog's ``metadata`` key, its
defensive typing against arbitrary upstream JSON, the ``get_model_metadata``
accessor (catalog_allowlist included), and — the trap that has already bitten
this module twice for other maps — that the metadata map survives a
persist → restart round trip.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.proxy.cloud_catalog import (
    ARCHITECTURE_FIELDS,
    METADATA_FIELDS,
    TOP_PROVIDER_FIELDS,
    CloudModelCatalog,
)
from app.proxy.providers import ProviderRegistry

SAMPLE_SETTINGS = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    timeout_seconds: 300
    models:
      - openai/gpt-4o
"""

ALLOWLIST_SETTINGS = """\
providers:
  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: nvapi-test-key
    timeout_seconds: 300
    catalog_allowlist:
      - minimaxai/minimax-m3
      - moonshotai/kimi-k3
    models:
      - minimaxai/minimax-m3
"""

#: A realistic OpenRouter /v1/models entry (public API shape).
FULL_ENTRY = {
    "id": "anthropic/claude-sonnet-4.6",
    "canonical_slug": "anthropic/claude-4.6-sonnet-20260217",
    "hugging_face_id": "anthropic/claude-4.6-sonnet",
    "name": "Anthropic: Claude Sonnet 4.6",
    "description": "Claude Sonnet 4.6 is the latest Sonnet-class model.",
    "created": 1771342990,
    "context_length": 1000000,
    "architecture": {
        "modality": "text+image+file->text",
        "input_modalities": ["text", "image", "file"],
        "output_modalities": ["text"],
        "tokenizer": "Claude",
        "instruct_type": None,
    },
    "pricing": {"prompt": "0.000003", "completion": "0.000015", "internal_reasoning": "0.000003"},
    "top_provider": {"context_length": 1000000, "max_completion_tokens": 128000, "is_moderated": False},
    "per_request_limits": {"prompt_tokens": 8000},
    "supported_parameters": ["tools", "tool_choice", "reasoning", "temperature"],
    "default_parameters": {"temperature": 1.0, "top_p": 1.0},
    "knowledge_cutoff": "2025-03-31",
    "expiration_date": None,
    "reasoning": {
        "mandatory": False,
        "default_enabled": True,
        "supported_efforts": ["max", "high", "medium", "low"],
        "default_effort": "high",
    },
}


class _FakeResponse:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _write_settings(tmp_path: Path, content: str = SAMPLE_SETTINGS) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _make_catalog(tmp_path: Path, content: str = SAMPLE_SETTINGS, **kwargs) -> CloudModelCatalog:
    registry = ProviderRegistry(settings_path=_write_settings(tmp_path, content))
    return CloudModelCatalog(
        provider_registry=registry,
        cache_file=tmp_path / "cache.json",
        overrides_file=tmp_path / "overrides.yaml",
        **kwargs,
    )


def _client_for(payload):
    """Return a fake httpx.AsyncClient class serving *payload*."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return _FakeResponse(payload)

    return FakeClient


# ── Extraction shape ─────────────────────────────────────────────────


class TestExtractionShape:
    def test_every_metadata_field_is_always_present(self):
        got = CloudModelCatalog.extract_model_metadata({})
        assert set(got) == set(METADATA_FIELDS)
        # Architecture/top_provider are explicit-null blocks, never omitted and
        # never a bare {} (spec §3.1).
        assert set(got["architecture"]) == set(ARCHITECTURE_FIELDS)
        assert set(got["top_provider"]) == set(TOP_PROVIDER_FIELDS)
        assert all(value is None for value in got["architecture"].values())
        assert all(value is None for value in got["top_provider"].values())
        for key in METADATA_FIELDS:
            if key not in ("architecture", "top_provider"):
                assert got[key] is None

    def test_tolerates_non_dict_entry(self):
        assert CloudModelCatalog.extract_model_metadata(None)["name"] is None

    def test_captures_full_openrouter_entry(self):
        got = CloudModelCatalog.extract_model_metadata(FULL_ENTRY)
        assert got["canonical_slug"] == "anthropic/claude-4.6-sonnet-20260217"
        assert got["hugging_face_id"] == "anthropic/claude-4.6-sonnet"
        assert got["name"] == "Anthropic: Claude Sonnet 4.6"
        assert got["description"].startswith("Claude Sonnet 4.6")
        assert got["created"] == 1771342990
        assert got["context_length"] == 1000000
        assert got["architecture"] == {
            "modality": "text+image+file->text",
            "input_modalities": ["text", "image", "file"],
            "output_modalities": ["text"],
            "tokenizer": "Claude",
            "instruct_type": None,
        }
        # Pricing values pass through verbatim (string decimals).
        assert got["pricing"] == {
            "prompt": "0.000003",
            "completion": "0.000015",
            "internal_reasoning": "0.000003",
        }
        assert got["top_provider"] == {
            "context_length": 1000000,
            "max_completion_tokens": 128000,
            "is_moderated": False,
        }
        assert got["per_request_limits"] == {"prompt_tokens": 8000}
        assert got["supported_parameters"] == ["tools", "tool_choice", "reasoning", "temperature"]
        assert got["default_parameters"] == {"temperature": 1.0, "top_p": 1.0}
        assert got["knowledge_cutoff"] == "2025-03-31"
        assert got["expiration_date"] is None

    def test_reasoning_block_stored_in_full(self):
        got = CloudModelCatalog.extract_model_metadata(FULL_ENTRY)
        # The metadata block keeps the FULL upstream reasoning object, while the
        # legacy accessor keeps its reduced subset — both must stay available.
        assert got["reasoning"] == FULL_ENTRY["reasoning"]
        assert CloudModelCatalog._extract_reasoning(FULL_ENTRY) == {
            "supported_efforts": ["max", "high", "medium", "low"],
            "default_effort": "high",
            "mandatory": False,
            "default_enabled": True,
        }


# ── Defensive typing ─────────────────────────────────────────────────


class TestDefensiveTyping:
    def test_wrong_scalar_types_become_none(self):
        got = CloudModelCatalog.extract_model_metadata(
            {
                "id": "brand/model",
                "context_length": True,  # bool is an int subclass — never accepted
                "created": "1771342990",
                "name": 42,
                "description": None,
                "knowledge_cutoff": 2025,
                "expiration_date": {"date": "2026-01-01"},
                "per_request_limits": ["prompt_tokens"],
                "default_parameters": "temperature=1",
            }
        )
        assert got["context_length"] is None
        assert got["created"] is None
        assert got["name"] is None
        assert got["description"] is None
        assert got["knowledge_cutoff"] is None
        assert got["expiration_date"] is None
        assert got["per_request_limits"] is None
        assert got["default_parameters"] is None

    def test_wrong_list_types_become_none(self):
        got = CloudModelCatalog.extract_model_metadata(
            {
                "id": "brand/model",
                "supported_parameters": "tools",
                "architecture": {"input_modalities": "text", "output_modalities": {"text"}},
            }
        )
        assert got["supported_parameters"] is None
        assert got["architecture"]["input_modalities"] is None
        assert got["architecture"]["output_modalities"] is None

    def test_mixed_list_keeps_only_strings(self):
        got = CloudModelCatalog.extract_model_metadata(
            {"id": "m", "supported_parameters": ["tools", 42, "", None, "reasoning"]}
        )
        assert got["supported_parameters"] == ["tools", "reasoning"]

    def test_empty_containers_count_as_not_advertised(self):
        got = CloudModelCatalog.extract_model_metadata(
            {
                "id": "m",
                "pricing": {},
                "supported_parameters": [],
                "default_parameters": {},
                "per_request_limits": [],
                "reasoning": {},
                "architecture": [],
                "top_provider": "yes",
            }
        )
        assert got["pricing"] is None
        assert got["supported_parameters"] is None
        assert got["default_parameters"] is None
        assert got["per_request_limits"] is None
        assert got["reasoning"] is None
        assert set(got["architecture"]) == set(ARCHITECTURE_FIELDS)
        assert set(got["top_provider"]) == set(TOP_PROVIDER_FIELDS)

    def test_top_provider_wrong_types_and_bool_guard(self):
        got = CloudModelCatalog.extract_model_metadata(
            {
                "id": "m",
                "top_provider": {
                    "context_length": True,
                    "max_completion_tokens": "128000",
                    "is_moderated": "false",
                },
            }
        )
        assert got["top_provider"] == {
            "context_length": None,
            "max_completion_tokens": None,
            "is_moderated": None,
        }
        assert CloudModelCatalog.extract_model_metadata(
            {"id": "m", "top_provider": {"is_moderated": True}}
        )["top_provider"]["is_moderated"] is True


# ── Refresh + accessor ───────────────────────────────────────────────


class TestRefreshAndAccessor:
    @pytest.mark.asyncio
    async def test_refresh_stores_metadata_and_accessor_reads_it(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        provider = catalog._registry.get_provider_for_model("openai/gpt-4o")
        assert provider is not None

        payload = {"data": [FULL_ENTRY, {"id": "openai/gpt-4o"}]}
        with patch("app.proxy.cloud_catalog.httpx.AsyncClient", _client_for(payload)):
            await catalog.refresh_provider(provider)

        got = catalog.get_model_metadata("openrouter", "anthropic/claude-sonnet-4.6")
        assert set(got) == set(METADATA_FIELDS)
        assert got["name"] == "Anthropic: Claude Sonnet 4.6"
        assert got["architecture"]["tokenizer"] == "Claude"
        assert got["pricing"]["prompt"] == "0.000003"
        assert got["supported_parameters"] == ["tools", "tool_choice", "reasoning", "temperature"]
        assert got["top_provider"]["max_completion_tokens"] == 128000

        # A model the upstream advertises nothing about still gets the full
        # explicit-null shape (spec §3: keys are always present).
        sparse = catalog.get_model_metadata("openrouter", "openai/gpt-4o")
        assert set(sparse) == set(METADATA_FIELDS)
        assert sparse["architecture"]["modality"] is None
        assert sparse["pricing"] is None

    @pytest.mark.asyncio
    async def test_existing_accessors_are_unchanged(self, tmp_path: Path):
        """get_model_reasoning / get_context_window / get_model_modalities keep
        their old outputs and signatures next to the new metadata map."""
        catalog = _make_catalog(tmp_path)
        provider = catalog._registry.get_provider_for_model("openai/gpt-4o")
        assert provider is not None

        with patch("app.proxy.cloud_catalog.httpx.AsyncClient", _client_for({"data": [FULL_ENTRY]})):
            await catalog.refresh_provider(provider)

        assert catalog.get_model_reasoning("openrouter", "anthropic/claude-sonnet-4.6") == {
            "supported_efforts": ["max", "high", "medium", "low"],
            "default_effort": "high",
            "mandatory": False,
            "default_enabled": True,
        }
        assert catalog.get_context_window("openrouter", "anthropic/claude-sonnet-4.6") == 1000000
        assert catalog.get_model_modalities("openrouter", "anthropic/claude-sonnet-4.6") == {
            "input": ["file", "image", "text"],
            "output": ["text"],
        }
        # And the same model advertises its parameters through the new accessor.
        assert "tools" in catalog.get_model_metadata("openrouter", "anthropic/claude-sonnet-4.6")[
            "supported_parameters"
        ]

    def test_accessor_returns_empty_for_unknown_provider_or_model(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        assert catalog.get_model_metadata("unknown", "brand/model") == {}
        catalog._catalogs["openrouter"] = {
            "fetched_at": 1.0,
            "models": {"openai/gpt-4o": "gpt-4o"},
        }
        # Model present in `models` but no metadata map captured (older state).
        assert catalog.get_model_metadata("openrouter", "openai/gpt-4o") == {}
        assert catalog.get_model_metadata("openrouter", "missing/model") == {}

    def test_allowlist_filters_metadata(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path, ALLOWLIST_SETTINGS)
        catalog._catalogs["nvidia"] = {
            "fetched_at": 1.0,
            "models": {
                "minimaxai/minimax-m3": "minimaxai/minimax-m3",
                "nvidia/llama-3.1-nemotron-70b-instruct": "nvidia/llama-3.1-nemotron-70b-instruct",
            },
            "metadata": {
                "minimaxai/minimax-m3": CloudModelCatalog.extract_model_metadata(
                    {"id": "minimaxai/minimax-m3", "name": "MiniMax M3"}
                ),
                "nvidia/llama-3.1-nemotron-70b-instruct": CloudModelCatalog.extract_model_metadata(
                    {"id": "nvidia/llama-3.1-nemotron-70b-instruct", "name": "Nemotron 70B"}
                ),
            },
        }
        # Allowlisted model: metadata visible.
        assert catalog.get_model_metadata("nvidia", "minimaxai/minimax-m3")["name"] == "MiniMax M3"
        # Not allowlisted: hidden, exactly like get_model_reasoning.
        assert catalog.get_model_metadata("nvidia", "nvidia/llama-3.1-nemotron-70b-instruct") == {}


# ── Persistence trap 3: metadata survives persist → restart ──────────


class TestPersistenceTrap:
    @pytest.mark.asyncio
    async def test_refresh_writes_metadata_to_the_disk_cache(self, tmp_path: Path):
        """End-to-end trap check: a real refresh persists metadata to disk, and
        a fresh catalog (a restart) serves it again."""
        catalog = _make_catalog(tmp_path)
        provider = catalog._registry.get_provider_for_model("openai/gpt-4o")
        assert provider is not None
        with patch("app.proxy.cloud_catalog.httpx.AsyncClient", _client_for({"data": [FULL_ENTRY]})):
            await catalog.refresh_provider(provider)

        restarted = _make_catalog(tmp_path)
        got = restarted.get_model_metadata("openrouter", "anthropic/claude-sonnet-4.6")
        assert got["name"] == "Anthropic: Claude Sonnet 4.6"
        assert got["supported_parameters"] == ["tools", "tool_choice", "reasoning", "temperature"]
        assert got["pricing"]["internal_reasoning"] == "0.000003"
        assert got["top_provider"]["is_moderated"] is False

    def test_metadata_survives_persist_and_restart(self, tmp_path: Path):
        """The trap that has bitten this module twice: write the disk cache,
        build a FRESH catalog (cold start) and assert the metadata is back."""
        catalog = _make_catalog(tmp_path)
        catalog._catalogs["openrouter"] = {
            "fetched_at": 1.0,
            "models": {"anthropic/claude-sonnet-4.6": "anthropic/claude-sonnet-4.6"},
            "reasoning": {"anthropic/claude-sonnet-4.6": {"supported_efforts": ["high"]}},
            "metadata": {
                "anthropic/claude-sonnet-4.6": CloudModelCatalog.extract_model_metadata(FULL_ENTRY)
            },
        }
        catalog._persist_cache()

        # The persisted document must actually carry the map.
        stored = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
        assert "metadata" in stored["openrouter"]

        restarted = _make_catalog(tmp_path)
        got = restarted.get_model_metadata("openrouter", "anthropic/claude-sonnet-4.6")
        assert set(got) == set(METADATA_FIELDS)
        assert got["canonical_slug"] == "anthropic/claude-4.6-sonnet-20260217"
        assert got["description"].startswith("Claude Sonnet 4.6")
        assert got["architecture"]["modality"] == "text+image+file->text"
        assert got["pricing"]["completion"] == "0.000015"
        assert got["top_provider"]["context_length"] == 1000000
        assert got["supported_parameters"][0] == "tools"
        assert got["reasoning"]["default_effort"] == "high"
        # The sibling maps still survive too (regression guard).
        assert restarted.get_model_reasoning("openrouter", "anthropic/claude-sonnet-4.6") == {
            "supported_efforts": ["high"]
        }

    def test_legacy_cache_without_metadata_is_normalised(self, tmp_path: Path):
        legacy = tmp_path / "cache.json"
        legacy.write_text(
            json.dumps(
                {
                    "openrouter": {
                        "fetched_at": 1.0,
                        "models": {"openai/gpt-4o": "openai/gpt-4o"},
                        "reasoning": {},
                        "context": {"openai/gpt-4o": 128000},
                        "modalities": {"openai/gpt-4o": {"input": ["text"]}},
                        "source": "https://openrouter.ai/api/v1|/models",
                    }
                }
            )
        )
        catalog = _make_catalog(tmp_path)
        assert catalog._catalogs["openrouter"]["metadata"] == {}
        assert catalog.get_model_metadata("openrouter", "openai/gpt-4o") == {}
        # Sibling maps are untouched by the normalisation.
        assert catalog.get_context_window("openrouter", "openai/gpt-4o") == 128000
        assert catalog.get_model_modalities("openrouter", "openai/gpt-4o") == {"input": ["text"]}

    def test_malformed_metadata_in_cache_is_ignored(self, tmp_path: Path):
        bad = tmp_path / "cache.json"
        bad.write_text(
            json.dumps(
                {
                    "openrouter": {
                        "fetched_at": 1.0,
                        "models": {"openai/gpt-4o": "openai/gpt-4o"},
                        "metadata": ["not", "a", "map"],
                        "source": "https://openrouter.ai/api/v1|/models",
                    }
                }
            )
        )
        catalog = _make_catalog(tmp_path)
        assert catalog.get_model_metadata("openrouter", "openai/gpt-4o") == {}


TWO_PROVIDER_SETTINGS = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    timeout_seconds: 300
    models:
      - openai/gpt-4o
  google:
    enabled: true
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key: gk-test-key
    timeout_seconds: 300
    models:
      - gemini-3.5-flash
"""


def _stored_provider(provider: str, base_url: str, models: dict, fetched_at: float) -> dict:
    return {
        "fetched_at": fetched_at,
        "models": models,
        "reasoning": {},
        "context": {},
        "modalities": {},
        "metadata": {},
        "source": f"{base_url}|/models",
    }


class TestCrossProcessPersistence:
    """The disk cache is shared runtime state, not a private scratch file.

    Regression (observed in production, 2026-10-03): a maintenance script built
    a catalog on the default cache path, refreshed a single provider, and
    ``_persist_cache`` replaced the whole file with that one provider — ten
    providers / 308 models collapsed to one provider / one model, degrading
    cold-start discovery for everything else until the next successful fetch.
    """

    def test_persist_preserves_providers_this_instance_never_fetched(self, tmp_path: Path):
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps(
                {
                    "openrouter": _stored_provider(
                        "openrouter",
                        "https://openrouter.ai/api/v1",
                        {"openai/gpt-4o": "openai/gpt-4o"},
                        1.0,
                    ),
                    "google": _stored_provider(
                        "google",
                        "https://generativelanguage.googleapis.com/v1beta/openai",
                        {"google/gemini-3.5-flash": "gemini-3.5-flash"},
                        2.0,
                    ),
                }
            ),
            encoding="utf-8",
        )

        catalog = _make_catalog(tmp_path, TWO_PROVIDER_SETTINGS)
        # Simulate an instance that only ever fetched google.
        catalog._catalogs = {
            "google": {
                "fetched_at": 3.0,
                "models": {"google/gemini-3.5-flash": "gemini-3.5-flash"},
                "reasoning": {},
                "context": {},
                "modalities": {},
                "metadata": {},
            }
        }
        catalog._persist_cache()

        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert "openrouter" in stored, "an untouched provider must survive a partial persist"
        assert stored["openrouter"]["models"] == {"openai/gpt-4o": "openai/gpt-4o"}
        assert stored["google"]["fetched_at"] == 3.0

    def test_a_scratch_registry_cannot_prune_other_providers(self, tmp_path: Path):
        """The damaging process used its own scratch settings, so its registry
        knew only one provider.  Pruning by "providers this registry knows"
        would therefore delete every other provider — the write must be purely
        additive instead, and pruning is unnecessary because
        ``_load_disk_cache`` already ignores entries that are no longer enabled
        or whose endpoint signature changed."""
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps(
                {
                    "openrouter": _stored_provider(
                        "openrouter",
                        "https://openrouter.ai/api/v1",
                        {"openai/gpt-4o": "openai/gpt-4o"},
                        1.0,
                    ),
                    "google": _stored_provider(
                        "google",
                        "https://generativelanguage.googleapis.com/v1beta/openai",
                        {"google/gemini-3.5-flash": "gemini-3.5-flash"},
                        2.0,
                    ),
                    "nvidia": _stored_provider(
                        "nvidia",
                        "https://integrate.api.nvidia.com/v1",
                        {"minimaxai/minimax-m3": "minimaxai/minimax-m3"},
                        3.0,
                    ),
                }
            ),
            encoding="utf-8",
        )

        # A scratch catalog whose registry defines only openrouter.
        catalog = _make_catalog(tmp_path, SAMPLE_SETTINGS)
        catalog._catalogs = {
            "openrouter": {
                "fetched_at": 9.0,
                "models": {"openai/gpt-4o": "openai/gpt-4o"},
                "reasoning": {},
                "context": {},
                "modalities": {},
                "metadata": {},
            }
        }
        catalog._persist_cache()

        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert set(stored) == {"openrouter", "google", "nvidia"}, (
            "providers this instance never fetched must survive its persist"
        )
        assert stored["openrouter"]["fetched_at"] == 9.0
        assert stored["google"]["models"] == {"google/gemini-3.5-flash": "gemini-3.5-flash"}
