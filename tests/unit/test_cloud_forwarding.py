"""Regression tests for cloud forwarding edge cases."""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.cloud_inference import forwarding
from app.cloud_inference.routing import (
    extract_cloud_finish_reason,
    extract_cloud_reasoning_content,
    extract_cloud_response_content,
)


class _FakeStreamClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def build_request(self, *args, **kwargs):
        return object()

    async def send(self, request, stream=False):
        return self.response

    async def aclose(self):
        return None


class _FakeNonStreamClient:
    """Fake httpx.AsyncClient for the non-streaming `async with` path."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def post(self, url, content=None, headers=None):
        return self.response

    async def aclose(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
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


def _patch_nonstream_common(monkeypatch, capture_completed, provider, http_client):
    """Shared fake wiring for the non-streaming forward tests (mirrors the
    streaming test's monkeypatches plus the non-stream-only globals)."""
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
    monkeypatch.setattr(forwarding, "_record_usage_from_payload", lambda *args, **kwargs: None)
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
    monkeypatch.setattr(forwarding, "_iter_sse_lines_with_watchdog", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(forwarding, "cloud_rate_limiter", _FakeRateLimiter())
    monkeypatch.setattr(forwarding, "failover_health", _FakeHealthTracker())
    monkeypatch.setattr(forwarding, "_GuardianRequestCancelled", type("RequestCancelled", (Exception,), {}))
    # Exercise the REAL extraction functions, not mocks.
    monkeypatch.setattr(forwarding, "_extract_cloud_response_content", extract_cloud_response_content)
    monkeypatch.setattr(forwarding, "_extract_cloud_reasoning_content", extract_cloud_reasoning_content)
    monkeypatch.setattr(forwarding, "_extract_cloud_finish_reason", extract_cloud_finish_reason)
    return http_client


@pytest.mark.asyncio
async def test_cloud_nonstream_capture_with_null_content_keeps_reasoning_and_finish_reason(monkeypatch):
    """Regression (2026-08-30): non-streamed responses where the model returned
    content: null plus reasoning (OpenRouter-style message.reasoning) must be
    captured with reasoning_content AND finish_reason populated while
    response_content stays None — previously both were empty because reasoning
    extraction was gated on non-empty content."""
    provider = SimpleNamespace(
        name="openrouter",
        base_url="https://provider.example/v1",
        api_key="test-key",
        timeout_seconds=30,
        extra_headers={},
    )
    payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": None, "reasoning": "chain of thought"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 100},
    }
    capture_completed = []
    http_client = _patch_nonstream_common(
        monkeypatch, capture_completed, provider, _FakeNonStreamClient(httpx.Response(200, json=payload))
    )

    request_body = {
        "model": "openrouter/provider/model",
        "messages": [{"role": "user", "content": "Think step by step"}],
        "stream": False,
    }
    with patch.object(forwarding.httpx, "AsyncClient", return_value=http_client):
        await forwarding.forward_to_cloud_provider(
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

    assert len(capture_completed) == 1
    assert capture_completed[0]["reasoning_content"] == "chain of thought"
    assert capture_completed[0]["finish_reason"] == "length"
    assert capture_completed[0]["response_content"] is None
    assert capture_completed[0]["incomplete"] is False
    assert capture_completed[0]["streamed"] is False


@pytest.mark.asyncio
async def test_cloud_nonstream_capture_without_finish_reason_marks_incomplete(monkeypatch):
    """A missing (None) finish_reason anywhere in the payload must yield
    finish_reason=None and incomplete=True on the capture record."""
    provider = SimpleNamespace(
        name="openrouter",
        base_url="https://provider.example/v1",
        api_key="test-key",
        timeout_seconds=30,
        extra_headers={},
    )
    payload = {
        "choices": [
            {
                "finish_reason": None,
                "message": {"content": None, "reasoning": "chain of thought"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 100},
    }
    capture_completed = []
    http_client = _patch_nonstream_common(
        monkeypatch, capture_completed, provider, _FakeNonStreamClient(httpx.Response(200, json=payload))
    )

    request_body = {
        "model": "openrouter/provider/model",
        "messages": [{"role": "user", "content": "Think step by step"}],
        "stream": False,
    }
    with patch.object(forwarding.httpx, "AsyncClient", return_value=http_client):
        await forwarding.forward_to_cloud_provider(
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

    assert len(capture_completed) == 1
    assert capture_completed[0]["finish_reason"] is None
    assert capture_completed[0]["incomplete"] is True
    assert capture_completed[0]["streamed"] is False


def test_usage_mirror_skips_non_finite_values():
    """JSON 1e999 parses to inf: int(inf) raises OverflowError, which would
    turn an already-successful 200 into a client-facing failure.  The mirror
    must skip non-finite values instead (review finding)."""
    from app.cloud_inference.forwarding import _extract_cloud_usage_mirror

    payload = {
        "provider": "Z.AI",
        "usage": {
            "completion_tokens_details": {"reasoning_tokens": 3129},
            "native_tokens_reasoning": 1e999,  # parses to inf
            "native_tokens_cached": float("inf"),
            "cost": 1e999,
        },
    }
    mirror = _extract_cloud_usage_mirror(payload)
    assert mirror["provider_name"] == "Z.AI"
    assert mirror["completion_tokens_details"] == {"reasoning_tokens": 3129}
    assert "native_tokens_reasoning" not in mirror
    assert "native_tokens_cached" not in mirror
    assert "cost" not in mirror
