"""Tests for app.main dashboard stats API."""

from collections import defaultdict
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
import pytest

from app import main
from app.proxy.usage import ApiUsageTracker


class JsonRequest:
    """Minimal authenticated request fixture for dashboard endpoint tests."""

    def __init__(self, payload: dict, key_fingerprint: str = "owner-key") -> None:
        self._payload = payload
        self.state = SimpleNamespace(auth_context={"key_fingerprint": key_fingerprint})

    async def json(self) -> dict:
        return self._payload


def test_configure_static_mount_skips_missing_dir(tmp_path, caplog):
    """Missing built dashboard assets should not block Guardian startup."""
    application = FastAPI()

    with caplog.at_level(logging.WARNING):
        main._configure_static_mount(application, tmp_path / "static")

    assert all(getattr(route, "path", None) != "/static" for route in application.routes)
    assert "skipping /static mount" in caplog.text


@pytest.mark.asyncio
async def test_get_stats_includes_api_usage(monkeypatch, tmp_path):
    """Dashboard stats include the persisted API usage snapshot."""
    tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")
    tracker.start_request(
        request_id="live-req-1",
        client_id="test-user",
        endpoint="/v1/chat/completions",
        method="POST",
        model="GLM-4.7-Flash",
        streamed=True,
    )
    tracker.update_active_request(
        request_id="live-req-1",
        phase="running",
        queue_request_id="queue-req-1",
        prompt_tokens=8,
        completion_tokens=5,
    )
    tracker.record_request(
        client_id="test-user",
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        model="GLM-4.7-Flash",
    )
    tracker.record_tokens(
        client_id="test-user",
        endpoint="/v1/chat/completions",
        model="GLM-4.7-Flash",
        prompt_tokens=8,
        completion_tokens=5,
    )

    monkeypatch.setattr(main, "get_gpu_metrics", lambda: {"used": 1024, "free": 2048, "total": 3072})
    monkeypatch.setattr(main, "get_model_size", lambda model: 4096)
    monkeypatch.setattr(main.proxy_state, "last_used", defaultdict(float, {"GLM-4.7-Flash": 1000.0}))
    monkeypatch.setattr(main.proxy_state.scheduler, "active_counts", {"GLM-4.7-Flash": 1}, raising=False)
    monkeypatch.setattr(main.proxy_state, "api_usage", tracker, raising=False)
    monkeypatch.setattr(
        main.inference_queue,
        "get_status",
        lambda: {
            "queue_length": 2,
            "active_count": 1,
            "wait_policy": "disconnect_or_cancel",
            "active_requests": [{"client_id": "test-user", "status": "running"}],
            "waiting": [{"client_id": "hydroponics", "position": 1}],
        },
    )

    stats = await main.get_stats()

    assert stats["api_usage"]["summary"]["total_requests"] == 1
    assert stats["api_usage"]["summary"]["total_tokens"] == 13
    assert stats["api_usage"]["summary"]["active_requests_count"] == 1
    assert stats["api_usage"]["active_requests"][0]["queue_request_id"] == "queue-req-1"
    assert stats["api_usage"]["top_clients"][0]["client_id"] == "test-user"
    assert stats["cached_models"][0]["name"] == "GLM-4.7-Flash"
    assert stats["queue_size"] == 2
    assert stats["queue_status"]["wait_policy"] == "disconnect_or_cancel"
    assert stats["queue_status"]["active_requests"][0]["client_id"] == "test-user"
    assert stats["queue_status"]["waiting"][0]["client_id"] == "hydroponics"


@pytest.mark.asyncio
async def test_dashboard_cloud_catalog_lists_provider_state(monkeypatch):
    fake_provider = SimpleNamespace(name="openrouter", is_configured=True)
    fake_catalog = MagicMock()
    fake_catalog._catalogs.get.return_value = {"fetched_at": 123.0}
    fake_catalog.get_models_for_provider.return_value = {
        "deepseek/deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731"
    }
    monkeypatch.setattr(
        main, "provider_registry", SimpleNamespace(get_enabled_providers=lambda: [fake_provider])
    )
    monkeypatch.setattr(main, "cloud_catalog", fake_catalog)

    result = await main.list_cloud_catalog_ui("client")

    assert result["catalog"][0]["name"] == "openrouter"
    assert result["catalog"][0]["configured"] is True
    assert result["catalog"][0]["model_count"] == 1
    assert result["catalog"][0]["addresses"] == ["openrouter/deepseek/deepseek-v4-flash-0731"]
    assert result["catalog"][0]["last_fetch"] == 123.0


@pytest.mark.asyncio
async def test_dashboard_cloud_catalog_refresh(monkeypatch):
    fake_catalog = MagicMock()
    fake_catalog.refresh_all = AsyncMock()
    monkeypatch.setattr(main, "cloud_catalog", fake_catalog)

    result = await main.refresh_cloud_catalog_ui("client")

    assert result["status"] == "refreshed"
    fake_catalog.refresh_all.assert_awaited_once()
