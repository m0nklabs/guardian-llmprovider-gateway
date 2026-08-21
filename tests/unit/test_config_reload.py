"""Reload-support regression tests for live (no-restart) config reloads.

Covers:
- config_loader.reload_config(): atomic in-place CONFIG swap + fail-safe
- ProviderHealthTracker.reconfigure(): in-place threshold tuning
- FailoverRegistry reload with the proposed free-tier groups (schema check)
- admin_api.reload_config(): end-to-end orchestration via injected deps
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import config_loader
from app.proxy.failover import FailoverRegistry, ProviderHealthTracker
from app.proxy.ratelimit import RateLimitConfig

# ── config_loader.reload_config ───────────────────────────────────────


def test_reload_config_swaps_contents_in_place(monkeypatch):
    """CONFIG keeps its identity so existing accessor references stay valid."""
    fresh = {"proxy": {"vram_limit_mb": 1111}}
    monkeypatch.setattr(config_loader, "CONFIG", fresh)
    original_id = id(fresh)

    def fake_load():
        return {"proxy": {"vram_limit_mb": 2222}}

    monkeypatch.setattr(config_loader, "load_config", fake_load)
    result = config_loader.reload_config()

    assert id(result) == original_id  # in-place, same dict object
    assert config_loader.CONFIG["proxy"]["vram_limit_mb"] == 2222


def test_reload_config_keeps_previous_on_parse_error(monkeypatch):
    """A failed parse leaves the previous config fully intact."""
    fresh = {"proxy": {"vram_limit_mb": 1111}}
    monkeypatch.setattr(config_loader, "CONFIG", fresh)

    def boom():
        raise Exception("boom")

    monkeypatch.setattr(config_loader, "load_config", boom)
    result = config_loader.reload_config()

    assert result is fresh
    assert config_loader.CONFIG["proxy"]["vram_limit_mb"] == 1111


# ── ProviderHealthTracker.reconfigure ─────────────────────────────────


def test_health_reconfigure_updates_thresholds_in_place():
    tracker = ProviderHealthTracker(failure_threshold=1, cooldown_seconds=10.0)
    tracker.record_failure("nvidia", "a/b")
    assert tracker.is_tripped("nvidia", "a/b") is True
    # Cooldown already started; reconfigure only the threshold.
    tracker.reconfigure(failure_threshold=5, cooldown_seconds=60.0)
    assert tracker._failure_threshold == 5
    assert tracker._cooldown_seconds == 60.0
    # In-flight cooldown survived the reconfigure.
    assert tracker.is_tripped("nvidia", "a/b") is True


def test_health_reconfigure_ignores_none_fields():
    tracker = ProviderHealthTracker(failure_threshold=2, cooldown_seconds=5.0)
    tracker.reconfigure()  # nothing to do — must not raise
    assert tracker._failure_threshold == 2
    assert tracker._cooldown_seconds == 5.0


# ── FailoverRegistry reload (free-tier proposed groups) ───────────────


def test_failover_registry_loads_proposed_groups(tmp_path: Path):
    """The configuration proposed in docs/free-tier-pool-request.md parses."""
    cfg_path = tmp_path / "cloud_keys.json"
    cfg_path.write_text(
        '{"failover_groups": {'
        '"minimax-m3": {"candidates": ['
        '{"provider": "nvidia", "model": "minimaxai/minimax-m3"},'
        '{"provider": "openrouter", "model": "minimax/minimax-m3:free"}]},'
        '"laguna": {"candidates": ['
        '{"provider": "openrouter", "model": "poolside/laguna-s-2.1:free"},'
        '{"provider": "nvidia", "model": "poolside/laguna-xs-2.1"}]},'
        '"gemini-flash": {"candidates": ['
        '{"provider": "google", "model": "models/gemini-2.5-flash"}]},'
        '"kimi-k3": {"candidates": ['
        '{"provider": "openrouter", "model": "moonshotai/kimi-k3"}]}'
        "}}"
    )
    registry = FailoverRegistry(cfg_path)
    groups = set(registry._groups.keys())
    assert groups == {"gemini-flash", "kimi-k3", "laguna", "minimax-m3"}
    m3 = registry.get_group("minimax-m3")
    assert [c.model for c in m3.candidates] == [
        "minimaxai/minimax-m3",
        "minimax/minimax-m3:free",
    ]
    # Default modalities apply
    assert m3.candidates[0].modalities == ("text",)


# ── admin_api.reload_config orchestration ─────────────────────────────


class _FakeController:
    def __init__(self):
        self.config = MagicMock(is_active=True)
        self.writer = None
        self.reload_config = AsyncMock()

    def initialize_writer(self):
        self.writer = MagicMock()


class _FakeRateLimiter:
    def __init__(self):
        self.config = RateLimitConfig(enabled=False)


@pytest.mark.asyncio
async def test_admin_reload_config_orchestrates_all_subsystems(monkeypatch):
    from app.gateway import admin_api

    registry = MagicMock()
    catalog = MagicMock()
    controller = _FakeController()
    health = MagicMock()
    limiter = _FakeRateLimiter()
    fake_config = {"cloud_retry": {"enabled": False}, "failover_health": {}}

    monkeypatch.setattr(admin_api, "_provider_registry", registry)
    monkeypatch.setattr(admin_api, "_failover_registry", registry)
    monkeypatch.setattr(admin_api, "_cloud_catalog", catalog)
    monkeypatch.setattr(admin_api, "_get_capture_controller", lambda: controller)
    monkeypatch.setattr(admin_api, "_failover_health", health)
    monkeypatch.setattr(admin_api, "_cloud_rate_limiter", limiter)
    monkeypatch.setattr(
        admin_api, "_reload_settings_config", MagicMock(return_value=fake_config)
    )

    result = await admin_api.reload_config("test-client")

    assert result["status"] == "ok"
    assert "settings.yaml (CONFIG)" in result["reloaded"]
    assert "providers" in result["reloaded"]
    assert "failover_groups" in result["reloaded"]
    assert "cloud_catalog" in result["reloaded"]
    assert "capture (cloud_capture, prefixes, policies)" in result["reloaded"]
    controller.reload_config.assert_awaited_once()
    registry.reload.assert_called()
    catalog.reload.assert_called()
    health.reconfigure.assert_called_once()
    assert limiter.config.enabled is False  # honors the reloaded cloud_retry


@pytest.mark.asyncio
async def test_reload_config_skips_retry_defaulting_when_settings_failed(monkeypatch):
    """When settings.yaml fails to reload, cloud_retry stays untouched."""
    from app.gateway import admin_api

    registry = MagicMock()
    catalog = MagicMock()
    health = MagicMock()
    limiter = _FakeRateLimiter()
    monkeypatch.setattr(admin_api, "_provider_registry", registry)
    monkeypatch.setattr(admin_api, "_failover_registry", registry)
    monkeypatch.setattr(admin_api, "_cloud_catalog", catalog)
    monkeypatch.setattr(admin_api, "_get_capture_controller", lambda: _FakeController())
    monkeypatch.setattr(admin_api, "_failover_health", health)
    monkeypatch.setattr(admin_api, "_cloud_rate_limiter", limiter)
    monkeypatch.setattr(
        admin_api, "_reload_settings_config", MagicMock(return_value=None)
    )

    result = await admin_api.reload_config("test-client")

    assert result["status"] == "partial"
    assert "failover_health" in result["not_reloaded"]
    assert "cloud_retry" in result["not_reloaded"]
    # cloud_retry untouched (config stayed at its pre-reload value)
    assert limiter.config.enabled is False