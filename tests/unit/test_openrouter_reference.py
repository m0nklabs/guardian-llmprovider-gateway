"""Unit tests for app.proxy.openrouter_reference — cross-provider reference data.

Covers docs/OPENROUTER_PARITY.md §2: source loading from provider settings,
first-wins merging with gap filling across two sources, disk-cache persistence
and url-change invalidation, TTL gating, ``send_api_key`` header handling, and
the fail-open policy (a fetch error keeps the last successful catalog).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.proxy.cloud_catalog import METADATA_FIELDS
from app.proxy.openrouter_reference import ModelReferenceCatalog, merge_metadata_gaps
from app.proxy.providers import ProviderRegistry

PRIMARY_URL = "https://primary.example/v1/models"
SECONDARY_URL = "https://secondary.example/v1/models"

PRIMARY_PAYLOAD = {
    "data": [
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context_length": 128000,
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            "architecture": {"input_modalities": ["text", "image"], "modality": "text+image->text"},
            "top_provider": {"context_length": 128000, "max_completion_tokens": 16384},
        }
    ]
}

SECONDARY_PAYLOAD = {
    "data": [
        {
            "id": "openai/gpt-4o",
            # first source wins for a field it already provides
            "name": "SECONDARY MUST NOT WIN",
            "description": "OpenAI's flagship multimodal model.",
            "supported_parameters": ["tools", "tool_choice", "temperature"],
            "architecture": {"output_modalities": ["text"], "tokenizer": "GPT"},
            "pricing": {"prompt": "9.99"},
            "knowledge_cutoff": "2024-10-01",
        },
        {
            "id": "meta-llama/llama-4-maverick",
            "name": "Llama 4 Maverick",
            "supported_parameters": ["tools"],
        },
    ]
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _client(payloads: dict[str, Any]):
    """Return ``(FakeClient, calls)`` serving *payloads* keyed by url.

    A payload that is an exception instance is raised instead (failure paths).
    """
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls.append((url, dict(headers or {})))
            payload = payloads.get(url)
            if isinstance(payload, Exception):
                raise payload
            return _FakeResponse(payload)

    return FakeClient, calls


def _write_providers(tmp_path: Path, monkeypatch, documents: dict[str, str]) -> Path:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir(exist_ok=True)
    for name, content in documents.items():
        (providers_dir / f"{name}.settings.yaml").write_text(textwrap.dedent(content))
    monkeypatch.setattr("app.paths.PROVIDERS_DIR", providers_dir)
    return providers_dir


OPENROUTER_PROVIDER = """\
enabled: true
base_url: https://openrouter.ai/api/v1
api_key: sk-or-test-key
timeout_seconds: 300
reference_catalog:
  sources:
    - name: primary
      url: {primary}
      enabled: true
      ttl_seconds: 3600
    - name: secondary
      url: {secondary}
      enabled: true
      ttl_seconds: 3600
"""


def _two_source_setup(tmp_path: Path, monkeypatch, *, openrouter_extra: str = "") -> tuple[ModelReferenceCatalog, dict]:
    _write_providers(
        tmp_path,
        monkeypatch,
        {
            "openrouter": OPENROUTER_PROVIDER.format(primary=PRIMARY_URL, secondary=SECONDARY_URL)
            + openrouter_extra,
        },
    )
    registry = ProviderRegistry()
    catalog = ModelReferenceCatalog(registry, cache_file=tmp_path / "reference.json")
    return catalog, {"registry": registry}


# ── Source loading ───────────────────────────────────────────────────


class TestSourceLoading:
    def test_sources_come_from_provider_settings(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        assert [source["url"] for source in catalog._sources] == [PRIMARY_URL, SECONDARY_URL]
        assert [source["name"] for source in catalog._sources] == ["primary", "secondary"]
        assert catalog._sources[0]["send_api_key"] is False
        assert catalog._sources[0]["ttl_seconds"] == 3600.0

    def test_provider_without_block_contributes_nothing(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "nvidia": """\
                    enabled: true
                    base_url: https://integrate.api.nvidia.com/v1
                    api_key: nvapi-test
                    """,
                "openrouter": OPENROUTER_PROVIDER.format(primary=PRIMARY_URL, secondary=SECONDARY_URL),
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        assert len(catalog._sources) == 2

    def test_no_sources_at_all_is_an_empty_catalog(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {"nvidia": "enabled: true\nbase_url: https://example.invalid/v1\napi_key: k\n"},
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        assert catalog._sources == []
        assert catalog.count() == 0
        assert catalog.is_stale() is False

    def test_disabled_source_is_skipped(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-test
                    reference_catalog:
                      sources:
                        - name: switched-off
                          url: https://off.example/v1/models
                          enabled: false
                        - name: switched-on
                          url: https://on.example/v1/models
                    """,
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        assert [source["name"] for source in catalog._sources] == ["switched-on"]

    def test_source_without_url_is_ignored(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-test
                    reference_catalog:
                      sources:
                        - name: broken
                        - name: good
                          url: https://good.example/v1/models
                    """,
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        assert [source["url"] for source in catalog._sources] == ["https://good.example/v1/models"]

    def test_duplicate_url_fetched_once(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-test
                    reference_catalog:
                      sources:
                        - name: first
                          url: https://same.example/v1/models
                        - name: second
                          url: https://same.example/v1/models
                    """,
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        assert [source["name"] for source in catalog._sources] == ["first"]

    def test_explicit_sources_bypass_the_config_scan(self, tmp_path: Path, monkeypatch):
        sources = [
            {"name": "injected", "url": "https://injected.example/v1/models", "ttl_seconds": 60}
        ]
        catalog = ModelReferenceCatalog(
            ProviderRegistry(settings_path=tmp_path / "missing.yaml"),
            cache_file=tmp_path / "reference.json",
            sources=sources,
        )
        assert [source["url"] for source in catalog._sources] == ["https://injected.example/v1/models"]
        assert catalog._sources[0]["ttl_seconds"] == 60.0


# ── Fetch + merge (spec §2: first non-null wins, later sources fill gaps) ──


class TestMergeAndGaps:
    @pytest.mark.asyncio
    async def test_two_sources_merge_with_gap_filling(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, calls = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})

        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()

        assert [url for url, _ in calls] == [PRIMARY_URL, SECONDARY_URL]
        assert catalog.count() == 2

        got = catalog.metadata("openai/gpt-4o")
        assert set(got) == set(METADATA_FIELDS)
        # First source wins for the fields it advertises ...
        assert got["name"] == "GPT-4o"
        assert got["context_length"] == 128000
        assert got["pricing"] == {"prompt": "0.0000025", "completion": "0.00001"}
        assert got["architecture"]["modality"] == "text+image->text"
        # ... later sources only fill the gaps (per field, including sub-keys).
        assert got["description"] == "OpenAI's flagship multimodal model."
        assert got["supported_parameters"] == ["tools", "tool_choice", "temperature"]
        assert got["knowledge_cutoff"] == "2024-10-01"
        assert got["architecture"]["input_modalities"] == ["text", "image"]
        assert got["architecture"]["output_modalities"] == ["text"]
        assert got["architecture"]["tokenizer"] == "GPT"
        # top_provider gaps are filled per sub-key too.
        assert got["top_provider"]["context_length"] == 128000
        assert got["top_provider"]["max_completion_tokens"] == 16384
        assert got["top_provider"]["is_moderated"] is None

        # A model only the second source knows is still available.
        second_only = catalog.metadata("meta-llama/llama-4-maverick")
        assert second_only["name"] == "Llama 4 Maverick"
        assert second_only["supported_parameters"] == ["tools"]

        # Provenance + raw lookup.
        assert catalog.source_name("openai/gpt-4o") == "primary"
        assert catalog.source_name("meta-llama/llama-4-maverick") == "secondary"
        raw = catalog.lookup("meta-llama/llama-4-maverick")
        assert raw == SECONDARY_PAYLOAD["data"][1]

    @pytest.mark.asyncio
    async def test_unknown_identity_key_is_empty(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.metadata("nope/not-there") == {}
        assert catalog.lookup("nope/not-there") is None
        assert catalog.source_name("nope/not-there") is None

    def test_merge_metadata_gaps_helper(self):
        base = {
            "name": "upstream name",
            "pricing": None,
            "architecture": {"modality": None, "tokenizer": "Claude"},
            "supported_parameters": [],
        }
        extra = {
            "name": "reference name",
            "pricing": {"prompt": "0.000003"},
            "architecture": {"modality": "text->text", "tokenizer": "Reference"},
            "supported_parameters": ["tools"],
        }
        merged = merge_metadata_gaps(base, extra)
        assert merged["name"] == "upstream name"
        assert merged["pricing"] == {"prompt": "0.000003"}
        assert merged["architecture"] == {"modality": "text->text", "tokenizer": "Claude"}
        # A falsey-but-real value ([]) is not treated as a gap by the helper;
        # the extraction layer normalises empty containers to None instead.
        assert merged["supported_parameters"] == []

    def test_merge_metadata_gaps_handles_missing_extra(self):
        assert merge_metadata_gaps({"a": None}, {}) == {"a": None}
        assert merge_metadata_gaps({}, {"a": 1}) == {"a": 1}
        assert merge_metadata_gaps(None, {"a": 1}) == {"a": 1}


# ── Disk cache: round trip and url-change invalidation ────────────────


class TestDiskCache:
    @pytest.mark.asyncio
    async def test_round_trip_restores_without_a_fetch(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert (tmp_path / "reference.json").exists()
        stored = json.loads((tmp_path / "reference.json").read_text(encoding="utf-8"))
        assert stored["source"] == f"{PRIMARY_URL}|{SECONDARY_URL}"
        assert stored["fetched_at"] > 0

        # Cold start: no fetch at all, data still there.
        FakeClient2, calls2 = _client({})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient2):
            restarted = ModelReferenceCatalog(
                ProviderRegistry(), cache_file=tmp_path / "reference.json"
            )
            assert calls2 == []
            assert restarted.count() == 2
            assert restarted.metadata("openai/gpt-4o")["description"] == (
                "OpenAI's flagship multimodal model."
            )
            assert restarted.source_name("openai/gpt-4o") == "primary"
            # The restored catalog is fresh, so the TTL gate stays closed.
            assert restarted.is_stale() is False
            await restarted.ensure_all_fresh()
            assert calls2 == []

    @pytest.mark.asyncio
    async def test_url_change_invalidates_the_cache(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.count() == 2

        # The operator changes the primary url -> the cached signature no longer
        # matches, so the stale catalog must not be restored.
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": OPENROUTER_PROVIDER.format(
                    primary="https://primary.example/v2/models", secondary=SECONDARY_URL
                )
            },
        )
        restarted = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        assert restarted.count() == 0
        assert restarted.metadata("openai/gpt-4o") == {}
        assert restarted.is_stale() is True

    @pytest.mark.asyncio
    async def test_reload_drops_the_catalog_when_the_url_changes(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.count() == 2

        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": OPENROUTER_PROVIDER.format(
                    primary="https://primary.example/v2/models", secondary=SECONDARY_URL
                )
            },
        )
        catalog.reload()
        assert catalog._sources[0]["url"] == "https://primary.example/v2/models"
        assert catalog.count() == 0

    @pytest.mark.asyncio
    async def test_renamed_source_does_not_serve_a_silently_empty_catalog(
        self, tmp_path: Path, monkeypatch
    ):
        """The signature only covers urls: a rename must refetch, not restore
        nothing and look empty for a whole TTL."""
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.count() == 2

        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-or-test-key
                    reference_catalog:
                      sources:
                        - name: renamed-primary
                          url: {primary}
                        - name: secondary
                          url: {secondary}
                    """.format(primary=PRIMARY_URL, secondary=SECONDARY_URL),
            },
        )
        restarted = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        # The renamed source is NOT restored under its new name ...
        assert "renamed-primary" not in restarted._source_entries
        # ... and it is treated as stale, so the next ensure refetches it
        # instead of serving a silently incomplete catalog for a whole TTL.
        assert restarted.is_stale() is True

        FakeClient2, calls2 = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient2):
            await restarted.ensure_all_fresh()
        # Only the renamed (stale) source is refetched; the untouched one is
        # restored from the cache.
        assert [url for url, _ in calls2] == [PRIMARY_URL]
        assert restarted.count() == 2
        assert restarted.source_name("openai/gpt-4o") == "renamed-primary"

    def test_cache_document_shape_and_unknown_shape_is_ignored(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {"openrouter": OPENROUTER_PROVIDER.format(primary=PRIMARY_URL, secondary=SECONDARY_URL)},
        )
        cache = tmp_path / "reference.json"
        # A document with the right signature but no per-source entries carries
        # no usable data and must be ignored (never crash, never half-restore).
        cache.write_text(
            json.dumps(
                {
                    "fetched_at": 123.0,
                    "source": f"{PRIMARY_URL}|{SECONDARY_URL}",
                    "source_entries": ["not", "a", "map"],
                }
            )
        )
        catalog = ModelReferenceCatalog(ProviderRegistry(), cache_file=cache)
        assert catalog.count() == 0
        assert catalog.metadata("openai/gpt-4o") == {}
        assert catalog.is_stale() is True

    @pytest.mark.asyncio
    async def test_persisted_document_holds_only_non_derivable_state(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        doc = json.loads((tmp_path / "reference.json").read_text(encoding="utf-8"))
        assert sorted(doc) == ["fetched_at", "source", "source_entries", "source_fetched_at"]
        assert sorted(doc["source_entries"]) == ["primary", "secondary"]
        assert doc["source_entries"]["primary"]["openai/gpt-4o"]["name"] == "GPT-4o"


# ── Fail-open ────────────────────────────────────────────────────────


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_fetch_failure_keeps_last_successful_catalog(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        good_client, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", good_client):
            await catalog.refresh_all()
        before = catalog.metadata("openai/gpt-4o")
        assert before["description"] == "OpenAI's flagship multimodal model."

        failing_client, _ = _client({PRIMARY_URL: RuntimeError("reference outage")})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", failing_client):
            await catalog.refresh_all()  # must not raise
            await catalog.ensure_all_fresh()
            catalog._source_fetched_at["primary"] = 0.0
            catalog._source_fetched_at["secondary"] = 0.0
            await catalog.ensure_all_fresh()

        assert catalog.metadata("openai/gpt-4o") == before
        assert catalog.count() == 2

    @pytest.mark.asyncio
    async def test_failure_with_no_previous_catalog_stays_empty(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        failing_client, _ = _client(
            {PRIMARY_URL: RuntimeError("boom"), SECONDARY_URL: RuntimeError("boom")}
        )
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", failing_client):
            await catalog.refresh_all()
        assert catalog.count() == 0
        assert catalog.metadata("openai/gpt-4o") == {}
        assert not (tmp_path / "reference.json").exists()

    @pytest.mark.asyncio
    async def test_payload_without_data_list_is_rejected(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client(
            {PRIMARY_URL: {"unexpected": "shape"}, SECONDARY_URL: {"data": []}}
        )
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.count() == 0

    @pytest.mark.asyncio
    async def test_one_failing_source_keeps_the_other(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client(
            {PRIMARY_URL: RuntimeError("primary down"), SECONDARY_URL: SECONDARY_PAYLOAD}
        )
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.count() == 2
        assert catalog.source_name("openai/gpt-4o") == "secondary"
        assert catalog.metadata("openai/gpt-4o")["description"] == (
            "OpenAI's flagship multimodal model."
        )


# ── TTL gating ───────────────────────────────────────────────────────


class TestTtlGating:
    @pytest.mark.asyncio
    async def test_ensure_all_fresh_is_ttl_gated(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, calls = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.ensure_all_fresh()  # cold: both fetched
            assert [url for url, _ in calls] == [PRIMARY_URL, SECONDARY_URL]
            await catalog.ensure_all_fresh()  # fresh: nothing refetched
            assert [url for url, _ in calls] == [PRIMARY_URL, SECONDARY_URL]

    @pytest.mark.asyncio
    async def test_stale_source_is_refetched_alone(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, calls = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.ensure_all_fresh()
            calls.clear()
            catalog._source_fetched_at["primary"] = 0.0  # TTL elapsed for one source
            assert catalog.is_stale() is True
            await catalog.ensure_all_fresh()
            assert [url for url, _ in calls] == [PRIMARY_URL]

    @pytest.mark.asyncio
    async def test_refresh_all_ignores_the_ttl(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, calls = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
            calls.clear()
            await catalog.refresh_all()
            assert [url for url, _ in calls] == [PRIMARY_URL, SECONDARY_URL]


# ── send_api_key ─────────────────────────────────────────────────────


class TestSendApiKey:
    @pytest.mark.asyncio
    async def test_send_api_key_uses_the_declaring_providers_credential(
        self, tmp_path: Path, monkeypatch
    ):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-or-secret
                    timeout_seconds: 300
                    reference_catalog:
                      sources:
                        - name: account-scoped
                          url: https://account.example/v1/models
                          send_api_key: true
                        - name: public
                          url: https://public.example/v1/models
                          send_api_key: false
                    """,
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        FakeClient, calls = _client(
            {
                "https://account.example/v1/models": PRIMARY_PAYLOAD,
                "https://public.example/v1/models": SECONDARY_PAYLOAD,
            }
        )
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()

        headers_by_url = {url: headers for url, headers in calls}
        assert headers_by_url["https://account.example/v1/models"]["Authorization"] == "Bearer sk-or-secret"
        assert "Authorization" not in headers_by_url["https://public.example/v1/models"]

    @pytest.mark.asyncio
    async def test_send_api_key_without_a_credential_falls_back_to_anonymous(
        self, tmp_path: Path, monkeypatch
    ):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: ""
                    reference_catalog:
                      sources:
                        - name: wants-key
                          url: https://account.example/v1/models
                          send_api_key: true
                    """,
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        FakeClient, calls = _client({"https://account.example/v1/models": PRIMARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert len(calls) == 1
        assert "Authorization" not in calls[0][1]
        assert catalog.count() == 1


# ── Hot reload ───────────────────────────────────────────────────────


class TestHotReload:
    @pytest.mark.asyncio
    async def test_reload_picks_up_an_added_source(self, tmp_path: Path, monkeypatch):
        _write_providers(
            tmp_path,
            monkeypatch,
            {
                "openrouter": """\
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-test
                    reference_catalog:
                      sources:
                        - name: primary
                          url: {primary}
                    """.format(primary=PRIMARY_URL),
            },
        )
        catalog = ModelReferenceCatalog(
            ProviderRegistry(), cache_file=tmp_path / "reference.json"
        )
        FakeClient, calls = _client({PRIMARY_URL: PRIMARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        assert catalog.count() == 1

        _write_providers(
            tmp_path,
            monkeypatch,
            {"openrouter": OPENROUTER_PROVIDER.format(primary=PRIMARY_URL, secondary=SECONDARY_URL)},
        )
        catalog.reload()
        assert [source["url"] for source in catalog._sources] == [PRIMARY_URL, SECONDARY_URL]
        assert catalog.count() == 0  # signature changed -> stale catalog dropped

        FakeClient2, calls2 = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient2):
            await catalog.ensure_all_fresh()
        assert [url for url, _ in calls2] == [PRIMARY_URL, SECONDARY_URL]
        assert catalog.count() == 2

    @pytest.mark.asyncio
    async def test_reload_keeps_a_matching_catalog(self, tmp_path: Path, monkeypatch):
        catalog, _ = _two_source_setup(tmp_path, monkeypatch)
        FakeClient, _ = _client({PRIMARY_URL: PRIMARY_PAYLOAD, SECONDARY_URL: SECONDARY_PAYLOAD})
        with patch("app.proxy.openrouter_reference.httpx.AsyncClient", FakeClient):
            await catalog.refresh_all()
        catalog.reload()
        assert catalog.count() == 2
        assert catalog.is_stale() is False
