"""Cross-host failover (F6 'local → windows'): managed providers participate in
failover groups as LOCAL candidates.

Two contracts:
1. Resolution: a failover group candidate whose provider is the managed local
   provider resolves into the attempts list (it was implicitly allowed before,
   but never exercised — this pin makes the cross-host group config loadable).
2. Execution: before forwarding to a managed candidate, the local lifecycle
   MUST run — ``caretaker_runtime.ensure_backend(upstream_model)`` — otherwise
   llama-server silently serves whatever model happens to be loaded (it ignores
   the request's model name; the 2026-09-01 mismatch incident class). An ensure
   failure skips to the next candidate like any failed attempt.
"""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest

from app.cloud_inference import forwarding, routing
from tests.unit.test_cloud_forwarding import (
    _FakeNonStreamClient,
    _patch_nonstream_common,
)

# The resolution tests re-init the routing module with stubs; save/restore the
# exact globals routing.init() owns so later test files in the same pytest
# session see the state they expect (test pollution without this).
_ROUTING_INIT_GLOBALS = (
    "_provider_registry", "_cloud_catalog", "_failover_registry", "_failover_health",
    "_get_request_auth_context", "_capture_client_fingerprint", "_capture_endpoint_from_request",
    "_dispatch_capture_request_received", "_get_capture_controller",
    "_cloud_provider_for_request", "_cloud_provider_unavailable_error",
    "_adapt_openai_reasoning_params",
)


@pytest.fixture(autouse=True)
def _restore_routing_globals():
    saved = {name: getattr(routing, name, None) for name in _ROUTING_INIT_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(routing, name, value)


# ---------------------------------------------------------------------------
# 1. Resolution: managed candidates resolve into the attempts list
# ---------------------------------------------------------------------------


def test_failover_group_includes_managed_local_candidate():
    managed = SimpleNamespace(
        name="ai-kvm2-local", enabled=True, is_configured=True, managed=True
    )
    windows = SimpleNamespace(
        name="14700k-local", enabled=True, is_configured=True, managed=False
    )
    group = SimpleNamespace(
        candidates=[
            SimpleNamespace(provider="ai-kvm2-local", model="qwen3.5-9b"),
            SimpleNamespace(provider="14700k-local", model="windows/qwen3.5-9b"),
        ]
    )
    routing.init(
        SimpleNamespace(_providers={"ai-kvm2-local": managed, "14700k-local": windows}),
        None,  # cloud_catalog (not used for failover resolution)
        SimpleNamespace(get_group=lambda name: group),
        SimpleNamespace(order_candidates=lambda candidates: candidates),
        lambda request: {},  # no cloud_gateway_access key -> defaults True
        lambda req, cid: None,
        lambda req: "",
        lambda *a, **k: None,
        lambda: None,
        lambda *a, **k: None,
        None,
        lambda provider, upstream, body: body,
    )
    attempts, group_name = routing.resolve_cloud_attempts(
        "failover/qwen35", SimpleNamespace(), "dsh"
    )
    assert group_name == "qwen35"
    assert [(p.name, m) for p, m in attempts] == [
        ("ai-kvm2-local", "qwen3.5-9b"),
        ("14700k-local", "windows/qwen3.5-9b"),
    ]


def test_failover_group_skips_disabled_managed_candidate():
    """A disabled/unconfigured managed candidate is skipped like any other —
    it must not poison the group when the local provider is off."""
    managed = SimpleNamespace(
        name="ai-kvm2-local", enabled=False, is_configured=True, managed=True
    )
    windows = SimpleNamespace(
        name="14700k-local", enabled=True, is_configured=True, managed=False
    )
    group = SimpleNamespace(
        candidates=[
            SimpleNamespace(provider="ai-kvm2-local", model="qwen3.5-9b"),
            SimpleNamespace(provider="14700k-local", model="windows/qwen3.5-9b"),
        ]
    )
    routing.init(
        SimpleNamespace(_providers={"ai-kvm2-local": managed, "14700k-local": windows}),
        None,
        SimpleNamespace(get_group=lambda name: group),
        SimpleNamespace(order_candidates=lambda candidates: candidates),
        lambda request: {},
        lambda req, cid: None,
        lambda req: "",
        lambda *a, **k: None,
        lambda: None,
        lambda *a, **k: None,
        None,
        lambda provider, upstream, body: body,
    )
    attempts, _ = routing.resolve_cloud_attempts(
        "failover/qwen35", SimpleNamespace(), "dsh"
    )
    assert [(p.name, m) for p, m in attempts] == [
        ("14700k-local", "windows/qwen3.5-9b"),
    ]


# ---------------------------------------------------------------------------
# 2. Execution: ensure_backend runs before a managed forward
# ---------------------------------------------------------------------------


def _patch_attempts(monkeypatch, attempts):
    """Re-patch the attempts list (the shared helper fixes a single attempt)."""
    monkeypatch.setattr(forwarding, "_resolve_cloud_attempts", lambda *a, **k: (attempts, None))


@pytest.mark.asyncio
async def test_managed_candidate_ensures_backend_before_forward(monkeypatch):
    """The managed local candidate triggers ensure_backend before its forward."""
    provider = SimpleNamespace(
        name="ai-kvm2-local",
        base_url="http://127.0.0.1:11440/v1",
        api_key=None,
        timeout_seconds=30,
        extra_headers={},
        managed=True,
    )
    payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": "local answer"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }
    capture_completed = []
    ensure_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.gateway.caretaker_runtime.ensure_backend", ensure_mock
    )
    _patch_nonstream_common(
        monkeypatch, capture_completed, provider, _FakeNonStreamClient(httpx.Response(200, json=payload))
    )

    request_body = {
        "model": "failover/qwen35",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    with patch.object(
        forwarding.httpx, "AsyncClient", return_value=_FakeNonStreamClient(httpx.Response(200, json=payload))
    ):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            request_body,
            "failover/qwen35",
            SimpleNamespace(),
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )
    assert response.status_code == 200
    ensure_mock.assert_awaited_once_with(
        model="provider/model", local_fallback=ANY
    )


@pytest.mark.asyncio
async def test_ensure_failure_falls_through_to_next_candidate(monkeypatch):
    """A failed local ensure skips to the next candidate (cloud) — the response
    must come from the later attempt, not the failed local one."""
    managed = SimpleNamespace(
        name="ai-kvm2-local",
        base_url="http://127.0.0.1:11440/v1",
        api_key=None,
        timeout_seconds=30,
        extra_headers={},
        managed=True,
    )
    windows = SimpleNamespace(
        name="14700k-local",
        base_url="https://windows.example/v1",
        api_key="test-key",
        timeout_seconds=30,
        extra_headers={},
        managed=False,
    )
    payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": "windows answer"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }
    capture_completed = []
    ensure_mock = AsyncMock(side_effect=RuntimeError("llama-server down"))
    monkeypatch.setattr(
        "app.gateway.caretaker_runtime.ensure_backend", ensure_mock
    )
    http_client = _patch_nonstream_common(
        monkeypatch, capture_completed, windows, _FakeNonStreamClient(httpx.Response(200, json=payload))
    )
    _patch_attempts(monkeypatch, [(managed, "qwen3.5-9b"), (windows, "windows/qwen3.5-9b")])

    request_body = {
        "model": "failover/qwen35",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    with patch.object(forwarding.httpx, "AsyncClient", return_value=http_client):
        response = await forwarding.forward_to_cloud_provider(
            "chat/completions",
            b"{}",
            request_body,
            "failover/qwen35",
            SimpleNamespace(),
            "dsh",
            capture_ctx=object(),
            capture_policy_result=object(),
            cloud_capture_start_time=0.0,
        )
    assert response.status_code == 200
    body = json_loads_safe(response)
    assert body["choices"][0]["message"]["content"] == "windows answer"
    ensure_mock.assert_awaited_once_with(
        model="qwen3.5-9b", local_fallback=ANY
    )


@pytest.mark.asyncio
async def test_all_managed_candidates_failed_ensure_raises_503(monkeypatch):
    """A group whose ONLY candidate is a managed provider that cannot be
    ensured surfaces 503 (model_load_failed) instead of a silent wrong-model
    forward."""
    managed = SimpleNamespace(
        name="ai-kvm2-local",
        base_url="http://127.0.0.1:11440/v1",
        api_key=None,
        timeout_seconds=30,
        extra_headers={},
        managed=True,
    )
    capture_completed = []
    ensure_mock = AsyncMock(side_effect=RuntimeError("backend dead"))
    monkeypatch.setattr(
        "app.gateway.caretaker_runtime.ensure_backend", ensure_mock
    )
    _patch_nonstream_common(
        monkeypatch, capture_completed, managed, _FakeNonStreamClient(httpx.Response(200, json={}))
    )

    request_body = {
        "model": "failover/qwen35",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    with patch.object(
        forwarding.httpx, "AsyncClient", return_value=_FakeNonStreamClient(httpx.Response(200, json={}))
    ):
        with pytest.raises(Exception) as excinfo:
            await forwarding.forward_to_cloud_provider(
                "chat/completions",
                b"{}",
                request_body,
                "failover/qwen35",
                SimpleNamespace(),
                "dsh",
                capture_ctx=object(),
                capture_policy_result=object(),
                cloud_capture_start_time=0.0,
            )
    assert "failed to load" in str(excinfo.value)


def json_loads_safe(response) -> dict:
    import json

    return json.loads(response.body)
