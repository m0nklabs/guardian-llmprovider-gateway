"""Regression tests for cloud forwarding edge cases."""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.cloud_inference import forwarding


class _FakeStreamClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def build_request(self, *args, **kwargs):
        return object()

    async def send(self, request, stream=False):
        return self.response

    async def aclose(self):
        return None


class _FakeRateLimiter:
    config = SimpleNamespace(enabled=False)

    async def execute_with_retry(self, fingerprint, provider_name, operation, **kwargs):
        return await operation()


class _FakeHealthTracker:
    def record_success(self, provider_name, model_name):
        return None

    def record_failure(self, provider_name, model_name):
        return None

    def record_rate_limited(self, provider_name, model_name):
        return None


@pytest.mark.asyncio
async def test_cloud_streaming_with_capture_bypasses_disabled_assembler(monkeypatch):
    response = httpx.Response(200, headers={"content-type": "text/event-stream"})
    stream_client = _FakeStreamClient(response)
    provider = SimpleNamespace(
        name="openrouter",
        base_url="https://provider.example/v1",
        api_key="test-key",
        timeout_seconds=30,
        extra_headers={},
    )
    capture_completed = []

    async def iter_sse_lines(*args, **kwargs):
        yield 'data: {"choices":[{"delta":{"content":"OK"}}]}'
        yield "data: [DONE]"

    monkeypatch.setattr(
        forwarding,
        "_resolve_cloud_attempts",
        lambda *args, **kwargs: ([(provider, "provider/model")], None),
    )
    monkeypatch.setattr(
        forwarding,
        "_prepare_cloud_candidate_request",
        lambda provider, upstream_model, path, body, fingerprint: (
            path,
            body,
            b"{}",
            False,
        ),
    )
    monkeypatch.setattr(forwarding, "_messages_contain_image_input", lambda messages: False)
    monkeypatch.setattr(forwarding, "_get_cloud_key_fingerprint", lambda request, client_id: "fingerprint")
    monkeypatch.setattr(forwarding, "_set_request_usage_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_start_live_request_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_update_live_request_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_finish_live_request_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_record_request_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_coerce_usage_int", lambda value: int(value or 0))
    monkeypatch.setattr(
        forwarding,
        "_dispatch_capture_request_completed",
        lambda *args, **kwargs: capture_completed.append(kwargs),
    )
    monkeypatch.setattr(forwarding, "_dispatch_capture_request_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_dispatch_capture_request_failed", lambda *args, **kwargs: None)
    monkeypatch.setattr(forwarding, "_guardian_debug_headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(forwarding, "_is_retryable_cloud_error", lambda *args, **kwargs: False)
    monkeypatch.setattr(forwarding, "_sanitize_proxied_response_headers", lambda headers: {})
    monkeypatch.setattr(forwarding, "_iter_sse_lines_with_watchdog", iter_sse_lines)
    monkeypatch.setattr(forwarding, "cloud_rate_limiter", _FakeRateLimiter())
    monkeypatch.setattr(forwarding, "failover_health", _FakeHealthTracker())
    monkeypatch.setattr(forwarding, "_GuardianRequestCancelled", type("RequestCancelled", (Exception,), {}))

    request_body = {
        "model": "openrouter/provider/model",
        "messages": [{"role": "user", "content": "Say OK"}],
        "stream": True,
    }
    with patch.object(forwarding.httpx, "AsyncClient", return_value=stream_client):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            request_body,
            "openrouter/provider/model",
            SimpleNamespace(),
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )
        chunks = [chunk async for chunk in response.body_iterator]

    assert b"OK" in b"".join(chunks)
    assert len(capture_completed) == 1
    # The cloud streaming assembler is active again (2026-08-26): raw SSE
    # lines are fed via add_sse_line() so assembled response content is
    # captured, not omitted.
    assert capture_completed[0]["response_content"] == "OK"
    assert capture_completed[0]["tool_calls"] is None
