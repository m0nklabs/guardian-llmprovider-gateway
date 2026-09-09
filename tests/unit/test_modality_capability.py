"""Modality capability pins — trap 2 catalog consolidation follow-up.

Upstream-advertised modality capability (OpenRouter architecture.modalities)
rides along on the single catalog fetch; failover image-capability decisions
consult it for candidates whose config does not declare modalities.  Explicit
config always wins.
"""

import httpx
import pytest

from app.proxy.cloud_catalog import CloudModelCatalog
from app.proxy.failover import FailoverCandidate, FailoverGroup, FailoverRegistry
from app.proxy.providers import ProviderRegistry

_OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "z-ai/glm-5.3-flash",
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
            },
        },
        {
            # string-form fallback: "text+image+video->text"
            "id": "some/brand/video-model",
            "architecture": {"modality": "text+image+video->text"},
        },
        {
            # no modality data at all
            "id": "some/brand/plain-text",
        },
    ]
}


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return _OPENROUTER_PAYLOAD


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, headers):
        return FakeResponse()


@pytest.fixture
def wired(tmp_path):
    settings = tmp_path / "providers.yaml"
    settings.write_text(
        """
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test
    timeout_seconds: 30
"""
    )
    registry = ProviderRegistry(settings_path=settings)
    catalog = CloudModelCatalog(registry)
    failover = FailoverRegistry()
    failover.set_modality_lookup(
        lambda p, m: tuple(catalog.get_model_modalities(p, m)["input"])
        if catalog.get_model_modalities(p, m)
        else None
    )
    return registry, catalog, failover


class TestCatalogModalityStorage:
    def test_input_modalities_stored_from_list_form(self, wired):
        registry, catalog, _failover = wired
        provider = next(iter(registry.get_enabled_providers()))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.proxy.cloud_catalog.httpx.AsyncClient", FakeAsyncClient)
            import asyncio

            asyncio.run(catalog.refresh_provider(provider))
        mods = catalog.get_model_modalities("openrouter", "z-ai/glm-5.3-flash")
        assert mods is not None
        assert "image" in mods["input"]
        assert mods["output"] == ["text"]

    def test_string_form_modality_parsed(self, wired):
        registry, catalog, _failover = wired
        provider = next(iter(registry.get_enabled_providers()))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.proxy.cloud_catalog.httpx.AsyncClient", FakeAsyncClient)
            import asyncio

            asyncio.run(catalog.refresh_provider(provider))
        mods = catalog.get_model_modalities("openrouter", "some/brand/video-model")
        assert mods is not None
        assert mods["input"] == ["image", "text", "video"]

    def test_unknown_model_returns_none(self, wired):
        _registry, catalog, _failover = wired
        assert catalog.get_model_modalities("openrouter", "does/not-exist") is None
        assert catalog.get_model_modalities("ghost-provider", "x/y") is None

    def test_old_cache_without_modalities_map_is_tolerated(self, wired):
        _registry, catalog, _failover = wired
        catalog._catalogs["openrouter"] = {"fetched_at": 1.0, "models": {}}
        assert catalog.get_model_modalities("openrouter", "x/y") is None


class TestFailoverDecision:
    def _group(self, **candidate_kwargs):
        candidate = FailoverCandidate(
            provider="openrouter",
            model="z-ai/glm-5.3-flash",
            **candidate_kwargs,
        )
        return FailoverGroup(name="vision", candidates=[candidate], image_fallback_model="local-vision")

    def test_catalog_capability_makes_candidate_image_capable(self, wired):
        _registry, catalog, failover = wired
        provider = next(iter(_registry.get_enabled_providers()))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.proxy.cloud_catalog.httpx.AsyncClient", FakeAsyncClient)
            import asyncio

            asyncio.run(catalog.refresh_provider(provider))
        # config does not declare modalities -> catalog decides
        assert failover.group_has_image_capable_candidate(self._group()) is True

    def test_explicit_config_wins_over_catalog(self, wired):
        _registry, _catalog, failover = wired
        group = self._group(modalities=("text",))
        assert failover.group_has_image_capable_candidate(group) is False

    def test_no_lookup_keeps_legacy_behavior(self, wired):
        _registry, _catalog, failover = wired
        group = self._group()
        assert failover.group_has_image_capable_candidate(group) is False

    def test_unknown_model_is_text_only(self, wired):
        _registry, catalog, failover = wired
        group = FailoverGroup(
            name="plain",
            candidates=[FailoverCandidate(provider="openrouter", model="ghost/model")],
        )
        assert failover.group_has_image_capable_candidate(group) is False

    def test_image_fallback_skipped_for_catalog_capable(self, wired):
        _registry, catalog, failover = wired
        provider = next(iter(_registry.get_enabled_providers()))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.proxy.cloud_catalog.httpx.AsyncClient", FakeAsyncClient)
            import asyncio

            asyncio.run(catalog.refresh_provider(provider))
        # upstream says image-capable -> no local fallback redirect
        assert failover.get_image_fallback_for_model("z-ai/glm-5.3-flash") is None

    def test_text_only_still_gets_fallback(self, wired):
        _registry, catalog, failover = wired
        group = FailoverGroup(
            name="plain",
            candidates=[FailoverCandidate(provider="openrouter", model="ghost/model")],
            image_fallback_model="local-vision",
        )
        failover._groups["plain"] = group
        assert failover.get_image_fallback_for_model("ghost/model") == "local-vision"
