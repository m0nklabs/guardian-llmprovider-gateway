"""Upstream invalid-response pins — HTTP 200 garbage must surface as 502.

Setup-agent handoff (2026-09-09): some upstream failure shapes pass through
as HTTP 200 today — OpenRouter embeds per-choice errors (``choices[].error``)
and some providers return a body with no ``choices`` at all (nemotron 200 +
null content + embedded error 500; poolside body without choices).  Callers
cannot distinguish "empty answer" from "provider down" without bespoke
parsing, so the gateway returns a real 502 instead.
"""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace
from unittest.mock import patch

from app.cloud_inference import forwarding
from tests.unit.test_cloud_forwarding import (
    _FakeNonStreamClient,
    _patch_nonstream_common,
)


def _provider():
    return SimpleNamespace(
        name="openrouter",
        base_url="https://provider.example/v1",
        api_key="test-key",
        timeout_seconds=30,
        extra_headers={},
    )


async def _forward(monkeypatch, payload, path="chat/completions", stream=False):
    """Run one non-stream forward against a canned 200 payload; return
    (failed_captures, completed_captures)."""
    provider = _provider()
    capture_failed = []
    capture_completed = []
    http_client = _FakeNonStreamClient(httpx.Response(200, json=payload))
    _patch_nonstream_common(monkeypatch, capture_completed, provider, http_client)
    monkeypatch.setattr(
        forwarding,
        "_dispatch_capture_request_failed",
        lambda *a, **k: capture_failed.append(k),
    )

    request_body = {
        "model": "openrouter/provider/model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    with patch.object(forwarding.httpx, "AsyncClient", return_value=http_client):
        try:
            await forwarding.forward_to_cloud_provider(
                path,
                b"{}",
                request_body,
                "openrouter/provider/model",
                SimpleNamespace(),
                "dsh",
                capture_ctx=object(),
                capture_policy_result=object(),
                cloud_capture_start_time=0.0,
            )
        except HTTPException as exc:
            return capture_failed, capture_completed, exc
    return capture_failed, capture_completed, None


@pytest.mark.asyncio
async def test_embedded_choice_error_surfaces_502(monkeypatch):
    """OpenRouter's embedded-error shape (choices[0].error) must not pass
    through as HTTP 200 — the gateway returns 502 upstream_invalid_response."""
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": None},
                "error": {"code": 500, "error_type": "server"},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 0},
    }
    capture_failed, capture_completed, exc = await _forward(monkeypatch, payload)
    assert exc is not None
    assert exc.status_code == 502
    assert exc.detail["error"] == "upstream_invalid_response"
    assert "500" in exc.detail["message"]
    assert len(capture_failed) == 1
    assert capture_failed[0]["error_code"] == "upstream_invalid_response"
    assert capture_failed[0]["http_status"] == 502
    assert len(capture_completed) == 0


@pytest.mark.asyncio
async def test_body_without_choices_surfaces_502_on_chat_paths(monkeypatch):
    payload = {"id": "x", "object": "chat.completion"}
    capture_failed, capture_completed, exc = await _forward(monkeypatch, payload)
    assert exc is not None
    assert exc.status_code == 502
    assert "no choices" in exc.detail["message"]
    assert len(capture_failed) == 1


@pytest.mark.asyncio
async def test_valid_payload_still_passes_through(monkeypatch):
    """Regression: a normal completion is untouched (no 502, completed capture)."""
    payload = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "hello"}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    capture_failed, capture_completed, exc = await _forward(monkeypatch, payload)
    assert exc is None
    assert len(capture_failed) == 0
    assert len(capture_completed) == 1


@pytest.mark.asyncio
async def test_embeddings_path_not_judged_by_choices(monkeypatch):
    """Embeddings answer with a ``data`` list (different contract) — the
    choices-requirement must not apply there."""
    payload = {"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}]}
    capture_failed, capture_completed, exc = await _forward(
        monkeypatch, payload, path="embeddings"
    )
    assert exc is None
    assert len(capture_failed) == 0
