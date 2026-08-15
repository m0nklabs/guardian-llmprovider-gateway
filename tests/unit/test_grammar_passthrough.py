"""FEAT-1: local OpenAI passthrough contract for Grammar-Constrained Decoding.

Locks in the no-whitelist contract: the local /v1/chat/completions path
forwards ``response_format``, ``json_schema``, and ``grammar`` (GBNF) fields
byte-identical to llama-server. A future body-normalization step that strips
unknown fields must fail these tests.
"""

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.proxy import server


def _make_fake_request(body_dict: dict):
    class _FakeRequest:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
            self.url = SimpleNamespace(path="/v1/chat/completions")
            self.method = "POST"

        async def body(self) -> bytes:
            return json.dumps(body_dict).encode("utf-8")

    return _FakeRequest()


def _make_fake_backend(captured: dict):
    """A fake httpx.AsyncClient capturing the forwarded request body."""

    class _FakeResponse:
        def __init__(self):
            self.content = b'{"ok":true}'
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, content=None, headers=None):
            captured["method"] = "POST"
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return _FakeResponse()

        async def aclose(self):
            return None

    return _FakeAsyncClient


def _base_patches(captured: dict):
    return [
        patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(server._gw_routing, "_begin_queued_request", return_value=("req-123", None)),
        patch.object(server._gw_routing, "_resolve_or_reject_inference_model", return_value="llama3.2-3b"),
        patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="llama3.2-3b")),
        patch.object(server.model_manager, "models", {"llama3.2-3b": {}}),
        patch.object(server._ctx_meta.httpx, "AsyncClient", _make_fake_backend(captured)),
    ]


@pytest.mark.asyncio
async def test_local_passthrough_preserves_all_gcd_fields_byte_identical():
    """response_format + json_schema + grammar reach llama-server untouched."""
    body = {
        "model": "llama3.2-3b",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "distill_record",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            },
        },
        "json_schema": {
            "name": "legacy_schema",
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        },
        "grammar": 'root ::= answer\nanswer ::= "yes" | "no"',
    }
    captured = {}
    request = _make_fake_request(body)

    with ExitStack() as stack:
        for p in _base_patches(captured):
            stack.enter_context(p)
        response = await server.proxy_v1_post("chat/completions", request, client_id="test-user")

    assert response.status_code == 200
    forwarded = json.loads(captured["content"].decode("utf-8"))
    assert forwarded["response_format"] == body["response_format"]
    assert forwarded["json_schema"] == body["json_schema"]
    assert forwarded["grammar"] == body["grammar"]
    # Byte-identical: the grammar string must appear verbatim in the raw body.
    assert '"grammar": "root ::= answer\\nanswer ::= \\"yes\\" | \\"no\\""' in captured["content"].decode("utf-8")


@pytest.mark.asyncio
async def test_local_passthrough_preserves_gcd_fields_with_default_settings():
    """Default config (grammar.enabled=true) leaves all three fields intact."""
    body = {
        "model": "llama3.2-3b",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
        "json_schema": {"name": "s", "schema": {"type": "object"}},
        "grammar": 'root ::= "hello"',
    }
    captured = {}
    request = _make_fake_request(body)

    with ExitStack() as stack:
        for p in _base_patches(captured):
            stack.enter_context(p)
        response = await server.proxy_v1_post("chat/completions", request, client_id="test-user")

    assert response.status_code == 200
    forwarded = json.loads(captured["content"].decode("utf-8"))
    assert forwarded["response_format"] == {"type": "json_object"}
    assert forwarded["json_schema"] == {"name": "s", "schema": {"type": "object"}}
    assert forwarded["grammar"] == 'root ::= "hello"'
