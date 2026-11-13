"""Bridge boundaries: no replay, safe errors, and stable cache/tool payloads."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import httpx
import pytest

from app.copilot_bridge import Settings, create_app, normalize_model
from app.copilot_errors import error_headers, retry_after

SETTINGS = Settings("http://copilot.test/v1", "upstream-secret", "client-secret")
HEADERS = {"Authorization": "Bearer client-secret"}


def model(protocol="chat"):
    endpoints = {"chat": ["/chat/completions"], "responses": ["/responses"], "messages": ["/v1/messages"]}
    return normalize_model({"id": "test-model", "vendor": "OpenAI", "supported_endpoints": endpoints.get(protocol, []),
                            "capabilities": {"type": "embeddings" if protocol == "embeddings" else "chat"}})


def ready_app(upstream, completion, embedding, protocol="chat"):
    app = create_app(SETTINGS, upstream, completion, embedding)
    row = model(protocol)
    app.state.catalog.models = {row["id"]: row}
    app.state.catalog.last_success = time.time()
    app.state.catalog._last_success_monotonic = time.monotonic()
    return app, row["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
async def test_cache_tools_and_usage_preserved_without_retries(protocol):
    response = SimpleNamespace(model_dump=lambda **_: {"usage": {"prompt_tokens": 100, "completion_tokens": 2,
                                                 "prompt_tokens_details": {"cached_tokens": 90}}, "choices": []})
    completion = AsyncMock(return_value=response)
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, completion, AsyncMock(), protocol)
        payload = {"model": name, "messages": [{"role": "system", "content": "stable cache prefix"},
                    {"role": "user", "content": "same input"}],
                   "tools": [{"type": "function", "function": {"name": "probe", "description": "Tool café\nDo not rewrite.",
                              "parameters": {"type": "object", "properties": {"x": {"type": "string", "description": "Nested docs"}},
                                             "required": ["x"], "additionalProperties": False}}}],
                   "prompt_cache_key": "client-stable-cache", "prompt_cache_retention": "in-memory", "n": 1}
        if protocol == "messages":
            payload.pop("prompt_cache_key")
            payload.pop("prompt_cache_retention")
        original = deepcopy(payload)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post("/v1/chat/completions", json=payload, headers=HEADERS)
        assert result.status_code == 200
        assert completion.await_count == 1
        kwargs = completion.call_args.kwargs
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0
        assert kwargs["messages"] == original["messages"]
        assert kwargs["tools"] == original["tools"]
        if protocol != "messages":
            assert kwargs["prompt_cache_key"] == original["prompt_cache_key"]
            assert kwargs["extra_body"]["prompt_cache_retention"] == original["prompt_cache_retention"]
        assert result.json()["usage"]["prompt_tokens_details"]["cached_tokens"] == 90
        assert payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,protocol", [("/v1/chat/completions", "chat"), ("/v1/embeddings", "embeddings")])
async def test_rate_limit_forwarded_safely_one_attempt(endpoint, protocol):
    class UpstreamError(Exception):
        status_code = 429
        response = SimpleNamespace(headers=httpx.Headers({"Retry-After": "17", "Authorization": "Bearer upstream-secret"}))
    operation = AsyncMock(side_effect=UpstreamError("upstream-secret raw prompt must not escape"))
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, operation, operation, protocol)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post(endpoint, json={"model": name, **({"input": "hi"} if protocol == "embeddings" else {"messages": [{"role": "user", "content": "Hi"}]})}, headers=HEADERS)
    assert operation.await_count == 1
    assert operation.call_args.kwargs["max_retries"] == 0
    assert result.status_code == 429
    assert result.headers["Retry-After"] == "17"
    assert "Authorization" not in result.headers
    assert "upstream-secret" not in result.text
    assert result.json()["error"]["code"] == "rate_limit_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,protocol", [("/v1/chat/completions", "chat"), ("/v1/embeddings", "embeddings")])
@pytest.mark.parametrize("status", [429, 503])
async def test_overflow_retry_date_does_not_break_http_error(endpoint, protocol, status):
    class UpstreamError(Exception):
        status_code = status
        response = SimpleNamespace(headers=httpx.Headers({"Retry-After": "Fri, 31 Dec 9999 23:59:59 -0100"}))
    operation = AsyncMock(side_effect=UpstreamError("secret upstream detail"))
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, operation, operation, protocol)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post(endpoint, json={"model": name, **({"input": "hi"} if protocol == "embeddings" else {"messages": [{"role": "user", "content": "Hi"}]})}, headers=HEADERS)
    assert operation.await_count == 1
    assert result.status_code == status
    assert "Retry-After" not in result.headers
    assert "error" in result.json()
    assert "secret" not in result.text


@pytest.mark.parametrize("value", ["bearer secret", "17\r\nX: evil", "17\n", -1, "1.5", "9" * 129, "Wed, 01 Jan 2025 12:00:00",
                                   "Fri, 31 Dec 9999 23:59:59 -0100", "Mon, 01 Jan 0001 00:00:00 +0100"])
def test_invalid_retry_header_dropped(value):
    assert retry_after(value) is None


@pytest.mark.parametrize("value", ["17", "0", "Wed, 01 Jan 2025 12:00:00 GMT"])
def test_retry_delay_and_http_date(value):
    assert retry_after(value) == value
    assert error_headers(SimpleNamespace(status_code=429, headers={"Retry-After": value})) == {"Retry-After": value}


@pytest.mark.asyncio
async def test_unknown_options_fail_before_inference_and_transport_overrides_cannot_escape():
    completion = AsyncMock()
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, completion, AsyncMock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "Hi"}], "made_up_option": 1}, headers=HEADERS)
    assert result.status_code == 400
    assert completion.await_count == 0


@pytest.mark.asyncio
async def test_stale_catalog_refuses_without_refresh_or_inference():
    completion = AsyncMock()
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, completion, AsyncMock())
        app.state.catalog.last_success = time.time() - 4000
        app.state.catalog._last_success_monotonic = time.monotonic() - 4000
        app.state.catalog.refresh = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "Hi"}]}, headers=HEADERS)
    assert result.status_code == 503
    assert completion.await_count == 0
    app.state.catalog.refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("option", [{"n": None}, {"stream": None}, {"n": None, "stream": None},
                                     {"prompt_cache_retention": None}, {"reasoning_effort": None},
                                     {"prompt_cache_key": None}])
async def test_nullable_completion_flags_mean_omission(protocol, option):
    result = SimpleNamespace(model_dump=lambda **_: {"choices": []})
    completion = AsyncMock(return_value=result)
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, completion, AsyncMock(), protocol)
        app.state.catalog.models[name]["capabilities"]["supports"] = {"reasoning_effort": ["low", "high"]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            response = await client.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "Hi"}], **option}, headers=HEADERS)
    assert response.status_code == 200
    assert completion.await_count == 1
    assert "n" not in completion.call_args.kwargs
    assert "stream" not in completion.call_args.kwargs
    for key in option:
        assert key not in completion.call_args.kwargs
    assert "extra_body" not in completion.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("option", [{"n": 2}, {"n": True}, {"n": 1.0}, {"stream": "true"},
                                    {"prompt_cache_key": ""}, {"prompt_cache_retention": "arbitrary"}])
async def test_invalid_cost_or_request_options_make_zero_calls(option):
    completion = AsyncMock()
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, completion, AsyncMock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "Hi"}], **option}, headers=HEADERS)
    assert result.status_code == 400
    assert completion.await_count == 0


@pytest.mark.asyncio
async def test_explicit_no_tool_support_refused_before_call():
    completion = AsyncMock()
    async with httpx.AsyncClient() as upstream:
        app, name = ready_app(upstream, completion, AsyncMock())
        app.state.catalog.models[name]["capabilities"]["supports"] = {"tool_calls": False}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            result = await client.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "Hi"}], "tools": [{"type": "function", "function": {"name": "probe", "parameters": {"type": "object", "properties": {}}}}]}, headers=HEADERS)
    assert result.status_code == 400
    assert "explicitly does not support tools" in result.json()["detail"]
    assert completion.await_count == 0
