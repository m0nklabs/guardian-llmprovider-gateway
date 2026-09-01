"""Pins for the /v1/models catalog self-heal wiring (2026-09-02).

Before this wiring nothing called ``CloudModelCatalog.ensure_fresh`` — the
TTL self-heal was dead code and catalogs only ever refreshed on startup,
``POST /api/cloud/catalog/refresh``, or a cold disk cache. ``/v1/models`` now
schedules one fire-and-forget ``ensure_all_fresh()`` (no-op under a warm TTL,
fail-open on refresh errors, deduped while one is in flight).
"""

from __future__ import annotations

import asyncio

import pytest

from app.gateway import model_discovery


class _StubCatalog:
    def __init__(self) -> None:
        self.calls = 0

    async def ensure_all_fresh(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_schedule_catalog_self_heal_fires_and_resets(monkeypatch) -> None:
    stub = _StubCatalog()
    monkeypatch.setattr(model_discovery, "_cloud_catalog", stub)
    monkeypatch.setattr(model_discovery, "_ensure_fresh_inflight", False)

    model_discovery._schedule_catalog_self_heal()
    for _ in range(3):
        await asyncio.sleep(0)  # let the fire-and-forget task complete

    assert stub.calls == 1
    assert model_discovery._ensure_fresh_inflight is False


@pytest.mark.asyncio
async def test_schedule_catalog_self_heal_dedups_while_inflight(monkeypatch) -> None:
    stub = _StubCatalog()
    monkeypatch.setattr(model_discovery, "_cloud_catalog", stub)
    monkeypatch.setattr(model_discovery, "_ensure_fresh_inflight", True)

    model_discovery._schedule_catalog_self_heal()
    for _ in range(3):
        await asyncio.sleep(0)

    assert stub.calls == 0, "an in-flight self-heal must suppress new spawns"


@pytest.mark.asyncio
async def test_schedule_catalog_self_heal_noop_without_catalog(monkeypatch) -> None:
    monkeypatch.setattr(model_discovery, "_cloud_catalog", None)
    monkeypatch.setattr(model_discovery, "_ensure_fresh_inflight", False)

    model_discovery._schedule_catalog_self_heal()  # must not raise
    for _ in range(3):
        await asyncio.sleep(0)
