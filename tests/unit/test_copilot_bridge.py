"""Dynamic catalog discovery, protocol selection and boundary tests."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from app.copilot_bridge import Catalog, Settings, create_app, normalize_model

SETTINGS = Settings("http://copilot.test/v1", "upstream-secret", "client-secret", refresh_seconds=0.01)


def entry(name="new-opus", vendor="Anthropic", endpoints=None, **extra):
    return {"id": name, "vendor": vendor, "supported_endpoints": endpoints or ["/chat/completions"],
            "model_picker_enabled": True, "capabilities": {"type": "chat", "limits": {
                "max_context_window_tokens": 200000, "max_output_tokens": 32000},
                "supports": {"vision": True, "tool_calls": True}}, **extra}


class Reply(BaseModel):
    model: str = "upstream-model"
    choices: list = []


@pytest.mark.parametrize("vendor,brand", [("Anthropic", "anthropic"), ("OpenAI", "openai"),
    ("Azure OpenAI", "openai"), ("Google", "google"), ("xAI", "x-ai"),
    ("Moonshot AI", "moonshotai"), ("Future Maker", "future-maker")])
def test_brands_come_from_vendor_not_provider(vendor, brand):
    assert normalize_model(entry(vendor=vendor))["id"] == f"{brand}/new-opus"


def test_metadata_and_policy():
    model = normalize_model(entry())
    assert model["context_length"] == 200000
    assert model["architecture"]["input_modalities"] == ["text", "image"]
    assert normalize_model(entry(policy={"state": "disabled"})) is None
    assert normalize_model(entry(model_picker_enabled=False)) is None
    assert normalize_model(entry(endpoints=["ws:/responses"])) is None
    assert normalize_model(entry(vendor="")) is None


@pytest.mark.asyncio
async def test_refresh_adds_and_removes_models_and_preserves_on_failure():
    state = {"data": [entry()]}
    async def handle(request):
        assert request.headers["authorization"] == "Bearer upstream-secret"
        return httpx.Response(200, json=state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = Catalog(SETTINGS, client)
        await catalog.refresh()
        assert set(catalog.models) == {"anthropic/new-opus"}
        state["data"] = [entry("brand-new-model", "Future Maker", ["/responses"])]
        await catalog.refresh()
        assert set(catalog.models) == {"future-maker/brand-new-model"}
        state["data"] = None
        await catalog.refresh()
        assert set(catalog.models) == {"future-maker/brand-new-model"}
        assert catalog.last_error == "ValueError"
        state["data"] = [entry(policy={"state": "disabled"})]
        await catalog.refresh()
        assert catalog.models == {}  # Explicitly disabled entries retire immediately.


@pytest.mark.asyncio
async def test_periodic_refresh_without_client_requests():
    import asyncio
    calls = 0
    async def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [entry(str(calls))]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        app = create_app(SETTINGS, client, AsyncMock(), AsyncMock())
        async with app.router.lifespan_context(app):
            for _ in range(30):
                if calls > 1:
                    break
                await asyncio.sleep(0.01)
            assert calls > 1
            assert f"anthropic/{calls}" in app.state.catalog.models


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoints,prefix", [(["/responses"], "openai/responses/"),
    (["/chat/completions"], "openai/"), (["/v1/messages"], "anthropic/")])
async def test_protocol_dispatch_and_untrusted_payload_cannot_override_transport(endpoints, prefix):
    async with httpx.AsyncClient() as upstream:
        completion = AsyncMock(return_value=Reply())
        app = create_app(SETTINGS, upstream, completion, AsyncMock())
        model = normalize_model(entry(endpoints=endpoints))
        app.state.catalog.models = {model["id"]: model}
        app.state.catalog.last_success = 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            payload = {"model": model["id"], "messages": [{"role": "user", "content": "Hi"}],
                       "api_base": "http://attacker", "api_key": "bad", "custom_llm_provider": "evil"}
            assert (await client.post("/v1/chat/completions", json=payload)).status_code == 401
            r = await client.post("/v1/chat/completions", json=payload,
                                  headers={"Authorization": "Bearer client-secret"})
            assert r.status_code == 200
            kwargs = completion.call_args.kwargs
            assert kwargs["model"] == prefix + "new-opus"
            assert kwargs["api_key"] == SETTINGS.upstream_key
            assert "attacker" not in kwargs["api_base"]
            assert "custom_llm_provider" not in kwargs
            payload["model"] = "anthropic/nonexistent"
            assert (await client.post("/v1/chat/completions", json=payload,
                    headers={"Authorization": "Bearer client-secret"})).status_code == 404


@pytest.mark.asyncio
async def test_stream_tool_calls_and_close():
    class Stream:
        closed = False
        def __aiter__(self):
            return self.generate()
        async def generate(self):
            yield Reply(choices=[{"delta": {"tool_calls": [{"id": "call_1", "function": {"name": "test"}}]}}])
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async with httpx.AsyncClient() as upstream:
        app = create_app(SETTINGS, upstream, AsyncMock(return_value=stream), AsyncMock())
        model = normalize_model(entry())
        app.state.catalog.models = {model["id"]: model}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            r = await client.post("/v1/chat/completions", json={"model": model["id"], "stream": True,
                    "messages": []}, headers={"Authorization": "Bearer client-secret"})
            assert '"tool_calls"' in r.text
            assert r.text.endswith("data: [DONE]\n\n")
            assert stream.closed


def test_settings_validate_nondefault_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("UP", "up-secret")
    monkeypatch.setenv("DOWN", "down-secret")
    p = tmp_path / "bridge.yaml"
    p.write_text("upstream_url: http://localhost:9999/v1\nupstream_key_env: UP\nclient_key_env: DOWN\nrefresh_seconds: 17\n")
    assert Settings.load(p).refresh_seconds == 17
    p.write_text(p.read_text() + "timeout_seconds: -1\n")
    with pytest.raises(ValueError):
        Settings.load(p)
