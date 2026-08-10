"""Tests for app.main dashboard stats API."""

from collections import defaultdict
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
async def test_dashboard_add_google_credential_discovers_catalog_and_assigns_owner():
    request = JsonRequest(
        {
            "provider": "google",
            "name": "Google AI Studio",
            "api_key": "google-test-key",
        }
    )
    stored_credential = {"id": "cred_google", "provider": "google", "models": ["gemini-2.5-flash"]}
    with (
        patch.object(main, "_discover_google_models", AsyncMock(return_value=["gemini-2.5-flash"])) as discover,
        patch.object(main.cloud_cred_store, "add_credential", AsyncMock(return_value=stored_credential)) as add_credential,
    ):
        result = await main.add_cloud_cred_ui(request, "owner-client")

    assert result == stored_credential
    discover.assert_awaited_once_with("google-test-key")
    assert add_credential.call_args.args == (
        "google",
        "Google AI Studio",
        "google-test-key",
        ["gemini-2.5-flash"],
    )
    assert add_credential.call_args.kwargs == {"owner_key_fingerprint": "owner-key"}


@pytest.mark.asyncio
async def test_dashboard_rejects_linking_foreign_credential():
    request = JsonRequest(
        {
            "guardian_key_fingerprint": "shared-key",
            "provider": "google",
            "credential_id": "cred_google",
        },
        key_fingerprint="foreign-key",
    )
    with (
        patch.object(main.cloud_cred_store, "is_credential_owned_by", return_value=False),
        patch.object(main.cloud_cred_store, "link_credential", AsyncMock()) as link_credential,
        pytest.raises(main.HTTPException) as exc_info,
    ):
        await main.link_cloud_cred_ui(request, "foreign-client")

    assert exc_info.value.status_code == 404
    link_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_dashboard_lists_only_credentials_owned_by_current_key():
    request = JsonRequest({}, key_fingerprint="owner-key")
    credentials = [{"id": "cred_google", "provider": "google"}]
    with patch.object(main.cloud_cred_store, "list_credentials_for_owner", return_value=credentials) as list_credentials:
        result = await main.list_cloud_creds_ui(request, "owner-client")

    assert result == {"credentials": credentials}
    list_credentials.assert_called_once_with("owner-key")
