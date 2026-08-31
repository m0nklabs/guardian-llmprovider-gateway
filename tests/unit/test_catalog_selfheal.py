"""Catalog self-heal pins: interval accessor, ensure_all_fresh gating, and the
lifespan refresh loop (fail-open).

Background: the cloud catalog used to populate ONLY via the manual refresh
endpoint — a fresh install served local models until an operator intervened
(2026-08-31 e2e). The TTL-gated refresher closes that gap.
"""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, patch

import pytest

from app.config_loader import get_catalog_refresh_interval_seconds
from app.proxy import lifespan as lifespan_mod


class TestGetCatalogRefreshIntervalSeconds:
    def test_default_is_60(self):
        assert get_catalog_refresh_interval_seconds({}) == 60.0

    def test_override_respected(self):
        cfg = {"proxy": {"catalog_refresh_interval_seconds": 120}}
        assert get_catalog_refresh_interval_seconds(cfg) == 120.0

    def test_sub_floor_value_clamped_to_default(self):
        # A mistyped near-zero value must not become a hot poll loop.
        cfg = {"proxy": {"catalog_refresh_interval_seconds": 0.001}}
        assert get_catalog_refresh_interval_seconds(cfg) == 60.0

    def test_garbage_falls_back_to_default(self):
        cfg = {"proxy": {"catalog_refresh_interval_seconds": "abc"}}
        assert get_catalog_refresh_interval_seconds(cfg) == 60.0

    def test_missing_section_falls_back_to_default(self):
        assert get_catalog_refresh_interval_seconds({"proxy": {}}) == 60.0


class TestEnsureAllFresh:
    @pytest.mark.asyncio
    async def test_refreshes_only_stale_providers_and_fails_open(self, tmp_path):
        from tests.unit.test_cloud_catalog import _make_catalog

        catalog = _make_catalog(tmp_path)
        names = [p.name for p in catalog._registry.get_enabled_providers() if p.is_configured]
        assert names, "fixture must expose at least one configured provider"
        stale_one = names[0]

        calls: list[str] = []

        async def fake_refresh(provider):
            calls.append(provider.name)
            if provider.name == stale_one:
                raise RuntimeError("provider exploded")

        with (
            patch.object(catalog, "is_stale", side_effect=lambda name: name == stale_one),
            patch.object(catalog, "refresh_provider", side_effect=fake_refresh),
        ):
            # Must not raise despite the failing provider: fail-open.
            await catalog.ensure_all_fresh()

        assert calls == [stale_one], "only the stale provider may be fetched"

    @pytest.mark.asyncio
    async def test_skips_everything_when_all_fresh(self, tmp_path):
        from tests.unit.test_cloud_catalog import _make_catalog

        catalog = _make_catalog(tmp_path)
        refresh = AsyncMock()
        with (
            patch.object(catalog, "is_stale", return_value=False),
            patch.object(catalog, "refresh_provider", refresh),
        ):
            await catalog.ensure_all_fresh()
        refresh.assert_not_awaited()


    @pytest.mark.asyncio
    async def test_loop_runs_passes_and_survives_failures(self, monkeypatch):
        """The refresher keeps looping when a pass raises, and stops cleanly
        on cancellation (shutdown path)."""
        mock_catalog = AsyncMock()
        mock_catalog.ensure_all_fresh = AsyncMock(side_effect=[None, RuntimeError("boom"), None, None])
        monkeypatch.setattr(lifespan_mod, "_cloud_catalog", mock_catalog)
        monkeypatch.setattr(lifespan_mod, "_catalog_refresh_interval_s", 0.01)

        task = asyncio.create_task(lifespan_mod._catalog_refresh_loop())
        await asyncio.sleep(0.08)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert mock_catalog.ensure_all_fresh.await_count >= 3, (
            "a raising pass must not stop the loop"
        )

