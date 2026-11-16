"""Copilot bridge tool-passthrough contract (#102).

The bridge's transport responsibility is to forward the tools JSON verbatim
into its completion callable: every description (including non-ASCII and
multiline), nested property/item/union documentation, required lists,
additionalProperties, tool_choice and tool-call/result identity must survive
unchanged. Verified here at the bridge boundary with deep-equal assertions.

Downstream LiteLLM protocol conversion is exercised by the deployed LiteLLM
service; the conversion layer itself is not testable from this repository
because litellm is not installed in the gateway venv (see docs and #102 for
the verified contract split)."""

import json
import time

import httpx
import pytest
from unittest.mock import AsyncMock

from app.copilot_bridge import create_app, normalize_model
from tests.unit.test_copilot_bridge import SETTINGS, entry, Reply


def usable_catalog(app, model):
    app.state.catalog.models = {model["id"]: model}
    app.state.catalog.last_success = time.time()
    app.state.catalog._last_success_monotonic = time.monotonic()


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Haalt het weer op — {city} (_ascii checks: émphasis, 日本語, emoji 🌦) \
met multiline uitleg:\n  - regen? ☔\n  - sneeuw? ❄️",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "Plaatsnaam, bijv. \"Amsterdam\"",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Aantal dagen vooruit",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 7,
                    },
                    "detail": {
                        "description": "Detailed breakdown — union of shapes",
                        "anyOf": [
                            {"type": "string", "title": "Short"},
                            {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "hour": {"type": "integer"},
                                        "mm": {"type": "number", "description": "Neerslag in mm"},
                                    },
                                    "required": ["hour"],
                                    "additionalProperties": False,
                                },
                            },
                        ],
                    },
                    "units": {
                        "type": "string",
                        "enum": ["metric", "imperial"],
                        "description": "Meeteenheid",
                    },
                },
                "required": ["city"],
                "additionalProperties": {
                    "type": "string",
                    "description": "Extra free-form key (documented passthrough)",
                },
            },
            "strict": False,
        },
    },
]

TOOL_CHOICE = {"type": "function", "function": {"name": "get_weather"}}


@pytest.mark.asyncio
async def test_tools_forwarded_verbatim_with_descriptions_and_schemas():
    completion = AsyncMock(return_value=Reply())
    async with httpx.AsyncClient() as upstream:
        app = create_app(SETTINGS, upstream, completion, AsyncMock())
        model = normalize_model(entry())
        usable_catalog(app, model)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://bridge") as client:
            r = await client.post(
                "/v1/chat/completions",
                json={"model": model["id"], "messages": [{"role": "user", "content": "Weer?"}],
                      "tools": TOOLS, "tool_choice": TOOL_CHOICE,
                      "parallel_tool_calls": False},
                headers={"Authorization": "Bearer " + SETTINGS.client_key})
        assert r.status_code == 200
        kwargs = completion.call_args.kwargs
        # Exact JSON: descriptions, multiline, non-ASCII, nested anyOf/items,
        # required, additionalProperties (object-form), defaults, strict.
        assert kwargs["tools"] == json.loads(json.dumps(TOOLS))
        assert kwargs["tool_choice"] == TOOL_CHOICE
        assert kwargs["parallel_tool_calls"] is False
        assert kwargs["tools"][0]["function"]["description"].startswith("Haalt het weer op — {city}")
        assert "☔" in kwargs["tools"][0]["function"]["description"]
        assert kwargs["tools"][0]["function"]["parameters"]["properties"]["detail"]["anyOf"][1][
            "items"]["properties"]["mm"]["description"] == "Neerslag in mm"


@pytest.mark.asyncio
async def test_tool_choice_string_variants_forwarded():
    completion = AsyncMock(return_value=Reply())
    async with httpx.AsyncClient() as upstream:
        app = create_app(SETTINGS, upstream, completion, AsyncMock())
        model = normalize_model(entry())
        usable_catalog(app, model)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://bridge") as client:
            for choice in ("auto", "none", "required"):
                r = await client.post(
                    "/v1/chat/completions",
                    json={"model": model["id"],
                          "messages": [{"role": "user", "content": "Weer?"}],
                          "tools": TOOLS, "tool_choice": choice},
                    headers={"Authorization": "Bearer " + SETTINGS.client_key})
                assert r.status_code == 200
                assert completion.call_args.kwargs["tool_choice"] == choice


@pytest.mark.asyncio
async def test_tool_call_result_identity_forwarded():
    """Assistant tool_calls and role:'tool' results pass through with their
    ids and raw argument strings intact."""
    completion = AsyncMock(return_value=Reply())
    messages = [
        {"role": "user", "content": "Weer in Amsterdam?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_9f2", "type": "function",
             "function": {"name": "get_weather",
                          "arguments": "{\"city\":\"Amsterdam\",\"days\":2}"}}],
         },
        {"role": "tool", "tool_call_id": "call_9f2", "content": "12°C, bewolkt"},
    ]
    async with httpx.AsyncClient() as upstream:
        app = create_app(SETTINGS, upstream, completion, AsyncMock())
        model = normalize_model(entry())
        usable_catalog(app, model)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://bridge") as client:
            r = await client.post(
                "/v1/chat/completions",
                json={"model": model["id"], "messages": messages, "tools": TOOLS},
                headers={"Authorization": "Bearer " + SETTINGS.client_key})
        assert r.status_code == 200
        assert completion.call_args.kwargs["messages"] == messages


@pytest.mark.asyncio
async def test_optional_default_semantics_preserved_not_invented():
    """Omitted optional fields stay absent (never null-injected), and fields
    the client sends outside CHAT_FIELDS are rejected rather than guessed."""
    completion = AsyncMock(return_value=Reply())
    async with httpx.AsyncClient() as upstream:
        app = create_app(SETTINGS, upstream, completion, AsyncMock())
        model = normalize_model(entry())
        usable_catalog(app, model)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://bridge") as client:
            r = await client.post(
                "/v1/chat/completions",
                json={"model": model["id"],
                      "messages": [{"role": "user", "content": "Weer?"}],
                      "tools": TOOLS, "tool_choice": "auto"},
                headers={"Authorization": "Bearer " + SETTINGS.client_key})
        assert r.status_code == 200
        kwargs = completion.call_args.kwargs
        assert "parallel_tool_calls" not in kwargs  # absent stays absent
        assert "stream" not in kwargs  # non-stream request has no stream flag
