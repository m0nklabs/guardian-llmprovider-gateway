"""Tests for app.main dashboard stats API."""

import logging
from collections import defaultdict
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

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
async def test_dashboard_cloud_catalog_delegates_to_the_shared_implementation(monkeypatch):
    """The dashboard app (port 11437) and the API app (11436) are two separate
    FastAPI apps, and this route used to hold its own hand-built copy of the
    payload.  That copy silently drifted: it never returned the enriched
    ``models`` array, so the dashboard's model catalog view could only ever show
    the legacy address pills.  Pin the delegation instead of a local shape."""
    enriched = [{"id": "openrouter/openai/gpt-4o", "name": "OpenAI: GPT-4o"}]
    seen: list[str] = []

    async def fake_list(client_id):
        seen.append(client_id)
        return {"catalog": [{"name": "openrouter", "addresses": [], "models": enriched}]}

    monkeypatch.setattr(main._admin_api, "list_cloud_catalog", fake_list)

    result = await main.list_cloud_catalog_ui("client")

    assert seen == ["client"], "the dashboard must call the shared implementation"
    assert result["catalog"][0]["models"] == enriched


@pytest.mark.asyncio
async def test_dashboard_cloud_catalog_refresh_delegates(monkeypatch):
    """Refresh delegates too, so it also refreshes the reference catalog and
    returns the refreshed payload rather than a bare status string."""
    seen: list[str] = []

    async def fake_refresh(client_id):
        seen.append(client_id)
        return {"status": "refreshed", "catalog": []}

    monkeypatch.setattr(main._admin_api, "refresh_cloud_catalog", fake_refresh)

    result = await main.refresh_cloud_catalog_ui("client")

    assert seen == ["client"]
    assert result["status"] == "refreshed"


@pytest.mark.asyncio
async def test_both_surfaces_serve_the_same_catalog_payload():
    """Regression guard for the drift above: whatever the API app returns, the
    dashboard app returns the same document, including the ``models`` array the
    catalogue view renders."""
    from app.gateway import admin_api

    dashboard = await main.list_cloud_catalog_ui("client")
    api = await admin_api.list_cloud_catalog("client")

    assert dashboard == api
    assert any(p.get("models") for p in dashboard["catalog"]), (
        "the dashboard payload must carry enriched models, not just addresses"
    )
