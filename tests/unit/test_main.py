"""Tests for app.main dashboard stats API."""

from collections import defaultdict

import pytest

from app import main
from app.proxy.usage import ApiUsageTracker


@pytest.mark.asyncio
async def test_get_stats_includes_api_usage(monkeypatch, tmp_path):
    """Dashboard stats include the persisted API usage snapshot."""
    tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")
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

    stats = await main.get_stats()

    assert stats["api_usage"]["summary"]["total_requests"] == 1
    assert stats["api_usage"]["summary"]["total_tokens"] == 13
    assert stats["api_usage"]["top_clients"][0]["client_id"] == "test-user"
    assert stats["cached_models"][0]["name"] == "GLM-4.7-Flash"
