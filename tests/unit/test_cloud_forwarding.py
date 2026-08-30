"""Regression tests for cloud forwarding edge cases."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from app.cloud_inference import forwarding
from app.gateway import queue_helpers


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


# ── G2: non-streamed disconnect propagation + max_call_seconds cap ──────


class _FakeNonStreamClient:
    """AsyncClient stand-in whose post() hangs until released or cancelled."""

    def __init__(self) -> None:
        self.closed = False
        self.post_cancelled = False
        self._release = asyncio.Event()

    async def post(self, *args, **kwargs):
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.post_cancelled = True
            raise
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        self.closed = True
        self._release.set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
        return False


class _InstantNonStreamClient:
    """AsyncClient stand-in that answers immediately."""

    def __init__(self) -> None:
        self.closed = False

    async def post(self, *args, **kwargs):
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
        return False


class _FakeDisconnectRequest:
    """Request stand-in reporting is_disconnected() after N polls."""

    def __init__(self, disconnect_after_polls: int) -> None:
        self.polls = 0
        self.disconnect_after_polls = disconnect_after_polls

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls >= self.disconnect_after_polls


class _RecordingHealthTracker:
    """Failover-health stand-in recording every call."""

    def __init__(self) -> None:
        self.failures = []
        self.successes = []

    def record_success(self, provider_name, model_name):
        self.successes.append(provider_name)

    def record_failure(self, provider_name, model_name):
        self.failures.append(provider_name)

    def record_rate_limited(self, provider_name, model_name):
        return None


def _cloud_provider(name: str = "openrouter", max_call_seconds=None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        base_url=f"https://{name}.example/v1",
        api_key="test-key",
        timeout_seconds=30,
        max_call_seconds=max_call_seconds,
        extra_headers={},
    )


def _patch_forwarding(monkeypatch, attempts):
    """Monkeypatch forward_to_cloud_provider's injected deps; return recorders."""
    captured = {"completed": [], "cancelled": [], "failed": [], "finished": []}
    health = _RecordingHealthTracker()
    monkeypatch.setattr(forwarding, "_resolve_cloud_attempts", lambda *a, **k: (attempts, None))
    monkeypatch.setattr(
        forwarding,
        "_prepare_cloud_candidate_request",
        lambda provider, upstream_model, path, body, fingerprint: (path, body, b"{}", False),
    )
    monkeypatch.setattr(forwarding, "_messages_contain_image_input", lambda messages: False)
    monkeypatch.setattr(forwarding, "_get_cloud_key_fingerprint", lambda request, client_id: "fingerprint")
    monkeypatch.setattr(forwarding, "_set_request_usage_metadata", lambda *a, **k: None)
    monkeypatch.setattr(forwarding, "_start_live_request_usage", lambda *a, **k: None)
    monkeypatch.setattr(forwarding, "_update_live_request_usage", lambda *a, **k: None)
    monkeypatch.setattr(
        forwarding,
        "_finish_live_request_usage",
        lambda request, **kwargs: captured["finished"].append(kwargs),
    )
    monkeypatch.setattr(forwarding, "_record_request_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(forwarding, "_record_usage_from_payload", lambda *a, **k: None)
    monkeypatch.setattr(forwarding, "_coerce_usage_int", lambda value: int(value or 0))
    monkeypatch.setattr(forwarding, "_extract_cloud_response_content", lambda payload: ("ok", None))
    monkeypatch.setattr(forwarding, "_extract_cloud_reasoning_content", lambda payload: None)
    monkeypatch.setattr(
        forwarding,
        "_dispatch_capture_request_completed",
        lambda *a, **k: captured["completed"].append(k),
    )
    monkeypatch.setattr(
        forwarding,
        "_dispatch_capture_request_cancelled",
        lambda *a, **k: captured["cancelled"].append(k),
    )
    monkeypatch.setattr(
        forwarding,
        "_dispatch_capture_request_failed",
        lambda *a, **k: captured["failed"].append(k),
    )
    monkeypatch.setattr(forwarding, "_guardian_debug_headers", lambda *a, **k: {})
    monkeypatch.setattr(forwarding, "_is_retryable_cloud_error", lambda *a, **k: False)
    monkeypatch.setattr(forwarding, "_sanitize_proxied_response_headers", lambda headers: {})
    monkeypatch.setattr(forwarding, "cloud_rate_limiter", _FakeRateLimiter())
    monkeypatch.setattr(forwarding, "failover_health", health)
    return captured, health


def _arm_fast_disconnect(monkeypatch) -> None:
    """Wire the real queue_helpers poller with a fast poll cadence."""
    monkeypatch.setattr(forwarding, "_await_request_disconnect", queue_helpers.await_request_disconnect)
    monkeypatch.setattr(queue_helpers, "DISCONNECT_POLL_INTERVAL_S", 0.01)


# T1: a downstream disconnect cancels the in-flight non-streamed upstream call.
@pytest.mark.asyncio
async def test_disconnect_cancels_non_stream_upstream(monkeypatch):
    provider = _cloud_provider()
    captured, _ = _patch_forwarding(monkeypatch, [(provider, "provider/model")])
    _arm_fast_disconnect(monkeypatch)
    fake_client = _FakeNonStreamClient()
    fake_request = _FakeDisconnectRequest(disconnect_after_polls=2)

    with patch.object(forwarding.httpx, "AsyncClient", return_value=fake_client):
        with pytest.raises(HTTPException) as excinfo:
            await forwarding.forward_to_cloud_provider(
                "chat/completions",
                b"{}",
                {"model": "openrouter/provider/model", "messages": [], "stream": False},
                "openrouter/provider/model",
                fake_request,
                "dsh",
                capture_ctx=object(),
                capture_policy_result=object(),
                cloud_capture_start_time=0.0,
            )

    assert excinfo.value.status_code == 499
    assert excinfo.value.detail["error"] == "request_cancelled"
    assert excinfo.value.detail["message"] == "client_disconnected"
    assert fake_client.post_cancelled is True
    assert fake_client.closed is True
    assert len(captured["cancelled"]) == 1
    assert captured["cancelled"][0]["cancel_reason"] == "client_disconnect"
    assert captured["finished"][0]["status_code"] == 499
    assert captured["completed"] == []


# T2: regression — a fast non-streamed response with no disconnect is untouched.
@pytest.mark.asyncio
async def test_non_stream_fast_response_no_disconnect(monkeypatch):
    provider = _cloud_provider()
    captured, _ = _patch_forwarding(monkeypatch, [(provider, "provider/model")])
    _arm_fast_disconnect(monkeypatch)
    fake_request = _FakeDisconnectRequest(disconnect_after_polls=10**9)

    with patch.object(forwarding.httpx, "AsyncClient", return_value=_InstantNonStreamClient()):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            {"model": "openrouter/provider/model", "messages": [], "stream": False},
            "openrouter/provider/model",
            fake_request,
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )

    assert response.status_code == 200
    assert len(captured["completed"]) == 1
    assert captured["completed"][0]["http_status"] == 200
    assert captured["cancelled"] == []
    assert captured["failed"] == []


# T1b: a BROKEN disconnect watcher fails open — the request must complete.
@pytest.mark.asyncio
async def test_broken_disconnect_watcher_fails_open(monkeypatch):
    provider = _cloud_provider()
    captured, _ = _patch_forwarding(monkeypatch, [(provider, "provider/model")])
    _arm_fast_disconnect(monkeypatch)

    class _BrokenRequest:
        async def is_disconnected(self):
            raise RuntimeError("receive channel unavailable")

    with patch.object(forwarding.httpx, "AsyncClient", return_value=_InstantNonStreamClient()):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            {"model": "openrouter/provider/model", "messages": [], "stream": False},
            "openrouter/provider/model",
            _BrokenRequest(),
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )

    assert response.status_code == 200
    assert len(captured["completed"]) == 1
    assert captured["cancelled"] == []


# T3: max_call_seconds abandons a slow candidate and fails over to the next.
@pytest.mark.asyncio
async def test_max_call_seconds_times_out_and_fails_over(monkeypatch):
    provider_slow = _cloud_provider("slow", max_call_seconds=0.05)
    provider_fast = _cloud_provider("fast", max_call_seconds=0.05)
    captured, health = _patch_forwarding(monkeypatch, [(provider_slow, "slow/m"), (provider_fast, "fast/m")])
    _arm_fast_disconnect(monkeypatch)
    fake_request = _FakeDisconnectRequest(disconnect_after_polls=10**9)

    async def handler(request):
        if request.url.host == "slow.example":
            await asyncio.sleep(1.0)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient  # patched attr IS httpx.AsyncClient — avoid recursion

    def _client_factory(*args, **kwargs):
        return real_async_client(transport=transport, timeout=None)

    with patch.object(forwarding.httpx, "AsyncClient", side_effect=_client_factory):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            {"model": "slow/m", "messages": [], "stream": False},
            "slow/m",
            fake_request,
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )

    assert response.status_code == 200
    assert health.failures == ["slow"]
    assert health.successes == ["fast"]
    assert len(captured["completed"]) == 1
    assert captured["failed"] == []


# T4: max_call_seconds absent (None) disables the cap entirely.
@pytest.mark.asyncio
async def test_no_max_call_seconds_keeps_old_behavior(monkeypatch):
    provider = _cloud_provider(max_call_seconds=None)
    captured, health = _patch_forwarding(monkeypatch, [(provider, "provider/model")])
    _arm_fast_disconnect(monkeypatch)
    fake_request = _FakeDisconnectRequest(disconnect_after_polls=10**9)

    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient  # patched attr IS httpx.AsyncClient — avoid recursion

    def _client_factory(*args, **kwargs):
        return real_async_client(transport=transport, timeout=None)

    with patch.object(forwarding.httpx, "AsyncClient", side_effect=_client_factory):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            {"model": "openrouter/provider/model", "messages": [], "stream": False},
            "openrouter/provider/model",
            fake_request,
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )

    assert response.status_code == 200
    assert health.failures == []
    assert len(captured["completed"]) == 1


# T5: max_call_seconds also bounds the streaming branch (time-to-headers),
# closing the stream client and surfacing cloud_max_duration/504.
@pytest.mark.asyncio
async def test_max_call_seconds_cancels_streaming_send(monkeypatch):
    provider = _cloud_provider(max_call_seconds=0.05)
    captured, health = _patch_forwarding(monkeypatch, [(provider, "provider/model")])

    class _HangingStreamClient:
        def __init__(self) -> None:
            self.closed = False

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=False):
            await asyncio.Event().wait()  # hang until cancelled

        async def aclose(self):
            self.closed = True

    fake_client = _HangingStreamClient()

    with patch.object(forwarding.httpx, "AsyncClient", return_value=fake_client):
        with pytest.raises(HTTPException) as excinfo:
            await forwarding.forward_to_cloud_provider(
                "chat/completions",
                b"{}",
                {"model": "openrouter/provider/model", "messages": [], "stream": True},
                "openrouter/provider/model",
                SimpleNamespace(),
                "dsh",
                capture_ctx=object(),
                capture_policy_result=object(),
                cloud_capture_start_time=0.0,
            )

    assert excinfo.value.status_code == 504
    assert fake_client.closed is True
    assert health.failures == ["openrouter"]
    assert len(captured["failed"]) == 1
    assert captured["failed"][0]["error_code"] == "cloud_max_duration"
    assert captured["failed"][0]["http_status"] == 504
