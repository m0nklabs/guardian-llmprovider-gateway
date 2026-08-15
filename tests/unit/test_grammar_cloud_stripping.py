"""FEAT-3: cloud grammar stripping + JSON auto-conversion in forwarding.

Cloud providers do not accept GBNF grammar strings or llama-server's
``json_schema`` field. Guardian strips them; OpenAI-native
``response_format`` is preserved. With ``grammar.cloud_auto_convert_json``
a JSON-targeting grammar/schema is converted to response_format. With
``grammar.cloud_strict_mode`` an unsupported grammar returns HTTP 400
naming the provider.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cloud_inference import forwarding as _forwarding
from app.proxy import server
from app.proxy.providers import CloudProvider

_JSON_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


class TestStripCloudGrammarHelper:
    def test_grammar_stripped_from_cloud_body(self):
        body = {"model": "x", "grammar": 'root ::= "yes" | "no"', "stream": False}
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=False)
        assert "grammar" not in result
        assert result["model"] == "x"

    def test_json_schema_stripped_from_cloud_body(self):
        body = {"model": "x", "json_schema": _JSON_SCHEMA}
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=False)
        assert "json_schema" not in result

    def test_response_format_preserved_on_cloud(self):
        body = {"model": "x", "response_format": {"type": "json_schema", "json_schema": _JSON_SCHEMA}}
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=False)
        assert result["response_format"] == {"type": "json_schema", "json_schema": _JSON_SCHEMA}

    def test_original_body_not_mutated(self):
        body = {"model": "x", "grammar": 'root ::= "hi"'}
        _forwarding._strip_cloud_grammar(body, allow_json_convert=False)
        assert "grammar" in body

    def test_json_schema_converted_when_allow_convert(self):
        body = {"model": "x", "json_schema": _JSON_SCHEMA}
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=True)
        assert "json_schema" not in result
        assert result["response_format"] == {"type": "json_schema", "json_schema": _JSON_SCHEMA}

    def test_json_grammar_converted_when_allow_convert(self):
        grammar_json = json.dumps(_JSON_SCHEMA)
        body = {"model": "x", "grammar": grammar_json}
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=True)
        assert "grammar" not in result
        assert result["response_format"] == {"type": "json_schema", "json_schema": _JSON_SCHEMA}

    def test_real_gbnf_not_converted_when_allow_convert(self):
        body = {"model": "x", "grammar": 'root ::= "yes" | "no"'}
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=True)
        assert "grammar" not in result
        assert "response_format" not in result

    def test_existing_response_format_wins_over_conversion(self):
        body = {
            "model": "x",
            "json_schema": _JSON_SCHEMA,
            "response_format": {"type": "json_object"},
        }
        result = _forwarding._strip_cloud_grammar(body, allow_json_convert=True)
        assert result["response_format"] == {"type": "json_object"}


class TestCloudGrammarStrictMode:
    def _make_fake_request(self, body_dict):
        class _FakeRequest:
            def __init__(self):
                self.headers = {"Content-Type": "application/json"}
                self.state = SimpleNamespace(auth_context={"key_fingerprint": "abc123"})
                self.url = SimpleNamespace(path="/v1/chat/completions")
                self.method = "POST"

            async def body(self) -> bytes:
                return json.dumps(body_dict).encode("utf-8")

        return _FakeRequest()

    @pytest.mark.asyncio
    async def test_strict_mode_400_on_grammar_for_cloud_model(self):
        """Strict mode rejects GBNF on cloud routes with a provider-naming 400."""
        fake_provider = CloudProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
            models=["openai/gpt-4o"],
        )
        request = self._make_fake_request({
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "grammar": 'root ::= "yes" | "no"',
        })
        with (
            patch.object(server.provider_registry, "is_cloud_model", return_value=True),
            patch.object(server.provider_registry, "get_provider_for_model", return_value=fake_provider),
            patch.object(server.ProviderRegistry, "build_forward_headers", return_value={"Authorization": "Bearer sk-or-test"}),
            patch.object(server.ProviderRegistry, "build_forward_url", return_value="https://openrouter.ai/api/v1/chat/completions"),
            patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_start_live_request_usage", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_finish_live_request_usage", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_record_usage_from_payload", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_grammar_cloud_strict_mode", True),
            patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
            patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        ):
            with pytest.raises(server.HTTPException) as exc_info:
                await server.proxy_v1_post("chat/completions", request, client_id="test-user")

        assert exc_info.value.status_code == 400
        assert "openrouter" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_cloud_path_strips_grammar_when_not_strict(self):
        """Grammar is silently stripped from the cloud-bound body by default."""
        fake_provider = CloudProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
            models=["openai/gpt-4o"],
        )
        captured = {}

        class _FakeResponse:
            def __init__(self):
                self.content = b'{"choices":[{"message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}'
                self.status_code = 200
                self.headers = {"content-type": "application/json"}

            def json(self):
                return json.loads(self.content)

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, content=None, headers=None):
                captured["url"] = url
                captured["content"] = content
                return _FakeResponse()

        request = self._make_fake_request({
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "grammar": 'root ::= "yes" | "no"',
            "response_format": {"type": "json_object"},
        })
        with (
            patch.object(server.provider_registry, "is_cloud_model", return_value=True),
            patch.object(server.provider_registry, "get_provider_for_model", return_value=fake_provider),
            patch.object(server.ProviderRegistry, "build_forward_headers", return_value={"Authorization": "Bearer sk-or-test"}),
            patch.object(server.ProviderRegistry, "build_forward_url", return_value="https://openrouter.ai/api/v1/chat/completions"),
            patch.object(server._ctx_meta.httpx, "AsyncClient", _FakeAsyncClient),
            patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_start_live_request_usage", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_finish_live_request_usage", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_record_usage_from_payload", lambda *a, **k: None),
            patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
            patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        ):
            response = await server.proxy_v1_post("chat/completions", request, client_id="test-user")

        assert response.status_code == 200
        forwarded = json.loads(captured["content"].decode("utf-8"))
        assert "grammar" not in forwarded
        assert forwarded["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_kill_switch_overrides_strict_mode(self):
        """FEAT-4 kill-switch precedence: when ``grammar.enabled`` is false the
        cloud path must STRIP grammar (never 400), even if ``cloud_strict_mode``
        is true. Strict-mode only applies when grammar is enabled."""
        fake_provider = CloudProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
            models=["openai/gpt-4o"],
        )
        captured = {}

        class _FakeResponse:
            def __init__(self):
                self.content = b'{"choices":[{"message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}'
                self.status_code = 200
                self.headers = {"content-type": "application/json"}

            def json(self):
                return json.loads(self.content)

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, content=None, headers=None):
                captured["content"] = content
                return _FakeResponse()

        request = self._make_fake_request({
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "grammar": 'root ::= "yes" | "no"',
        })
        with (
            patch.object(server.provider_registry, "is_cloud_model", return_value=True),
            patch.object(server.provider_registry, "get_provider_for_model", return_value=fake_provider),
            patch.object(server.ProviderRegistry, "build_forward_headers", return_value={"Authorization": "Bearer sk-or-test"}),
            patch.object(server.ProviderRegistry, "build_forward_url", return_value="https://openrouter.ai/api/v1/chat/completions"),
            patch.object(server._ctx_meta.httpx, "AsyncClient", _FakeAsyncClient),
            patch.object(server._gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_start_live_request_usage", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_finish_live_request_usage", lambda *a, **k: None),
            patch.object(server._cloud_forwarding, "_record_usage_from_payload", lambda *a, **k: None),
            # Kill-switch OFF wins over strict-mode ON → must strip, not 400.
            patch.object(server._cloud_forwarding, "_grammar_enabled", False),
            patch.object(server._cloud_forwarding, "_grammar_cloud_strict_mode", True),
            patch.object(server.model_manager, "get_current_model", AsyncMock(return_value="local-model")),
            patch.object(server.model_manager, "resolve_model", side_effect=ValueError("not local")),
        ):
            response = await server.proxy_v1_post("chat/completions", request, client_id="test-user")

        assert response.status_code == 200
        forwarded = json.loads(captured["content"].decode("utf-8"))
        assert "grammar" not in forwarded
