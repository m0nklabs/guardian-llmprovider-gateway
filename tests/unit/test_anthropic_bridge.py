"""Unit tests for app.proxy.anthropic_bridge — Anthropic ↔ OpenAI translation."""

import json
import pytest

from app.proxy.anthropic_bridge import (
    provider_needs_anthropic_translation,
    translate_anthropic_request_to_openai,
    translate_openai_response_to_anthropic,
    translate_openai_stream_to_anthropic,
    _format_sse_event,
    _convert_content_blocks_to_openai,
    _convert_anthropic_tools_to_openai,
)


# ── provider_needs_anthropic_translation ──────────────────────────────


class TestProviderNeedsTranslation:
    def test_nvidia_needs_translation(self):
        assert provider_needs_anthropic_translation("nvidia", "messages") is True

    def test_openrouter_does_not_need_translation(self):
        assert provider_needs_anthropic_translation("openrouter", "messages") is False

    def test_non_messages_path_never_needs_translation(self):
        assert provider_needs_anthropic_translation("nvidia", "chat/completions") is False
        assert provider_needs_anthropic_translation("nvidia", "completions") is False

    def test_unknown_provider_needs_translation(self):
        assert provider_needs_anthropic_translation("custom_provider", "messages") is True


# ── translate_anthropic_request_to_openai ─────────────────────────────


class TestRequestTranslation:
    def test_basic_text_message(self):
        anthropic = {
            "model": "minimaxai/minimax-m3",
            "messages": [{"role": "user", "content": "Hello!"}],
            "max_tokens": 100,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["model"] == "minimaxai/minimax-m3"
        assert len(openai["messages"]) == 1
        assert openai["messages"][0]["role"] == "user"
        assert openai["messages"][0]["content"] == "Hello!"
        assert openai["max_tokens"] == 100

    def test_system_prompt_as_string(self):
        anthropic = {
            "model": "test-model",
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["messages"][0]["role"] == "system"
        assert openai["messages"][0]["content"] == "You are a helpful assistant."
        assert openai["messages"][1]["role"] == "user"

    def test_system_prompt_as_content_blocks(self):
        anthropic = {
            "model": "test-model",
            "system": [{"type": "text", "text": "System part 1"}, {"type": "text", "text": "System part 2"}],
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["messages"][0]["role"] == "system"
        assert "System part 1" in openai["messages"][0]["content"]
        assert "System part 2" in openai["messages"][0]["content"]

    def test_temperature_and_top_p(self):
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
            "temperature": 0.5,
            "top_p": 0.9,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["temperature"] == 0.5
        assert openai["top_p"] == 0.9

    def test_stop_sequences(self):
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
            "stop_sequences": ["\n\n"],
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["stop"] == ["\n\n"]

    def test_stream_flag(self):
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
            "stream": True,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["stream"] is True

    def test_stream_options_include_usage_when_streaming(self):
        """When stream=True, stream_options.include_usage must be set so
        that providers like NVIDIA NIM return usage in the final chunk."""
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
            "stream": True,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["stream_options"] == {"include_usage": True}

    def test_no_stream_options_when_not_streaming(self):
        """When stream is not set, stream_options should not be present."""
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert "stream_options" not in openai

    def test_content_blocks_text(self):
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            "max_tokens": 50,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["messages"][0]["content"] == "Hello"

    def test_content_blocks_image(self):
        anthropic = {
            "model": "test",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR..."}},
                ],
            }],
            "max_tokens": 50,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        content = openai["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert "data:image/png;base64," in content[1]["image_url"]["url"]

    def test_tool_use_and_result(self):
        anthropic = {
            "model": "test",
            "messages": [
                {"role": "user", "content": "What's the weather?"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "tool_1", "name": "get_weather", "input": {"city": "Amsterdam"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "Sunny, 22°C"}]},
            ],
            "max_tokens": 50,
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        # Should have: user msg, assistant with tool_calls, tool msg
        assert len(openai["messages"]) == 3
        assert openai["messages"][1]["role"] == "assistant"
        assert openai["messages"][1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert openai["messages"][2]["role"] == "tool"
        assert openai["messages"][2]["tool_call_id"] == "tool_1"

    def test_tools_conversion(self):
        anthropic = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
            "tools": [{
                "name": "get_weather",
                "description": "Get weather for a city",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }],
        }
        openai = translate_anthropic_request_to_openai(anthropic)
        assert openai["tools"][0]["type"] == "function"
        assert openai["tools"][0]["function"]["name"] == "get_weather"
        assert openai["tools"][0]["function"]["parameters"]["properties"]["city"]["type"] == "string"


# ── translate_openai_response_to_anthropic ───────────────────────────


class TestResponseTranslation:
    def test_basic_text_response(self):
        openai_resp = {
            "id": "chatcmpl-123",
            "choices": [{
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
        anthropic = translate_openai_response_to_anthropic(openai_resp, "test-model")
        assert anthropic["type"] == "message"
        assert anthropic["role"] == "assistant"
        assert anthropic["model"] == "test-model"
        assert anthropic["content"][0]["type"] == "text"
        assert anthropic["content"][0]["text"] == "Hello!"
        assert anthropic["stop_reason"] == "end_turn"
        assert anthropic["usage"]["input_tokens"] == 10
        assert anthropic["usage"]["output_tokens"] == 3

    def test_max_tokens_stop_reason(self):
        openai_resp = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"role": "assistant", "content": "..."}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 100},
        }
        anthropic = translate_openai_response_to_anthropic(openai_resp, "test-model")
        assert anthropic["stop_reason"] == "max_tokens"

    def test_tool_calls_response(self):
        openai_resp = {
            "id": "chatcmpl-123",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Amsterdam"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        anthropic = translate_openai_response_to_anthropic(openai_resp, "test-model")
        assert anthropic["stop_reason"] == "tool_use"
        assert anthropic["content"][0]["type"] == "tool_use"
        assert anthropic["content"][0]["name"] == "get_weather"
        assert anthropic["content"][0]["input"] == {"city": "Amsterdam"}

    def test_empty_content(self):
        openai_resp = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0},
        }
        anthropic = translate_openai_response_to_anthropic(openai_resp, "test-model")
        # Should have at least one text block (even if empty)
        assert len(anthropic["content"]) >= 1


# ── Streaming translation ─────────────────────────────────────────────


class TestStreamingTranslation:
    @pytest.mark.asyncio
    async def test_basic_text_streaming(self):
        """Test that OpenAI SSE chunks are translated to Anthropic events."""
        openai_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in openai_lines:
                yield line

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        event_types = [e.split("\n")[0].replace("event: ", "") for e in events]
        assert "message_start" in event_types
        assert "content_block_start" in event_types
        assert "content_block_delta" in event_types
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert "message_stop" in event_types

        delta_texts = []
        for event in events:
            for part in event.split("\n"):
                if part.startswith("data: "):
                    data = json.loads(part[6:])
                    if data.get("type") == "content_block_delta":
                        delta_texts.append(data["delta"].get("text", ""))
        assert "".join(delta_texts) == "Hello world"

    @pytest.mark.asyncio
    async def test_streaming_text_then_tool_use(self):
        """Text followed by tool_call — should produce text block then tool_use block."""
        openai_lines = [
            'data: {"choices":[{"delta":{"content":"Let me check the weather."},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"Paris\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":15}}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in openai_lines:
                yield line

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        # Extract events and their data
        parsed = []
        for event in events:
            for part in event.split("\n"):
                if part.startswith("data: "):
                    parsed.append(json.loads(part[6:]))

        # Should have: message_start, content_block_start (text), content_block_delta (text),
        # content_block_stop (text), content_block_start (tool_use), content_block_delta (input_json_delta x2),
        # content_block_stop (tool_use), message_delta, message_stop

        starts = [p for p in parsed if p.get("type") == "content_block_start"]
        assert len(starts) == 2
        assert starts[0]["content_block"]["type"] == "text"
        assert starts[1]["content_block"]["type"] == "tool_use"
        assert starts[1]["content_block"]["id"] == "call_1"
        assert starts[1]["content_block"]["name"] == "get_weather"

        deltas = [p for p in parsed if p.get("type") == "content_block_delta"]
        # First delta is text, next two are input_json_delta
        assert deltas[0]["delta"]["type"] == "text_delta"
        assert deltas[0]["delta"]["text"] == "Let me check the weather."
        assert deltas[1]["delta"]["type"] == "input_json_delta"
        assert deltas[1]["delta"]["partial_json"] == '{"city":'
        assert deltas[2]["delta"]["type"] == "input_json_delta"
        assert deltas[2]["delta"]["partial_json"] == '"Paris"}'

        stops = [p for p in parsed if p.get("type") == "content_block_stop"]
        assert len(stops) == 2

        msg_delta = [p for p in parsed if p.get("type") == "message_delta"]
        assert msg_delta[0]["delta"]["stop_reason"] == "tool_use"
        assert msg_delta[0]["usage"]["output_tokens"] == 15

    @pytest.mark.asyncio
    async def test_streaming_message_delta_includes_input_tokens(self):
        """The message_delta event must include input_tokens, not just output_tokens.
        Without this, clients like Claude Code show 0 tokens used in their status bar."""
        openai_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":42,"completion_tokens":7}}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in openai_lines:
                yield line

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        parsed = []
        for event in events:
            for part in event.split("\n"):
                if part.startswith("data: "):
                    parsed.append(json.loads(part[6:]))

        msg_delta = [p for p in parsed if p.get("type") == "message_delta"]
        assert len(msg_delta) == 1
        assert msg_delta[0]["usage"]["input_tokens"] == 42
        assert msg_delta[0]["usage"]["output_tokens"] == 7

    @pytest.mark.asyncio
    async def test_streaming_tool_use_only(self):
        """Tool call without any preceding text — should produce only tool_use block."""
        openai_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":\\"Paris\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in openai_lines:
                yield line

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        parsed = []
        for event in events:
            for part in event.split("\n"):
                if part.startswith("data: "):
                    parsed.append(json.loads(part[6:]))

        starts = [p for p in parsed if p.get("type") == "content_block_start"]
        assert len(starts) == 1
        assert starts[0]["content_block"]["type"] == "tool_use"

        deltas = [p for p in parsed if p.get("type") == "content_block_delta"]
        assert deltas[0]["delta"]["type"] == "input_json_delta"
        assert deltas[0]["delta"]["partial_json"] == '{"city":"Paris"}'

    @pytest.mark.asyncio
    async def test_streaming_stop_reason(self):
        openai_lines = [
            'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":"length"}]}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in openai_lines:
                yield line

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        for event in events:
            for part in event.split("\n"):
                if part.startswith("data: "):
                    data = json.loads(part[6:])
                    if data.get("type") == "message_delta":
                        assert data["delta"]["stop_reason"] == "max_tokens"

    @pytest.mark.asyncio
    async def test_empty_stream(self):
        async def line_gen():
            return
            yield

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        event_types = [e.split("\n")[0].replace("event: ", "") for e in events]
        assert "message_start" in event_types
        assert "message_stop" in event_types

    @pytest.mark.asyncio
    async def test_streaming_multiple_tool_calls(self):
        """Two tool calls in the same stream — should produce two tool_use blocks."""
        openai_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":\\"Paris\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_2","type":"function","function":{"name":"get_time","arguments":"{\\"zone\\":\\"CET\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in openai_lines:
                yield line

        events = []
        async for event in translate_openai_stream_to_anthropic(line_gen(), "test-model"):
            events.append(event)

        parsed = []
        for event in events:
            for part in event.split("\n"):
                if part.startswith("data: "):
                    parsed.append(json.loads(part[6:]))

        starts = [p for p in parsed if p.get("type") == "content_block_start"]
        assert len(starts) == 2
        assert starts[0]["content_block"]["name"] == "get_weather"
        assert starts[1]["content_block"]["name"] == "get_time"
        assert starts[0]["index"] != starts[1]["index"]

        msg_delta = [p for p in parsed if p.get("type") == "message_delta"]
        assert msg_delta[0]["delta"]["stop_reason"] == "tool_use"


# ── _format_sse_event ─────────────────────────────────────────────────


class TestSSEFormat:
    def test_format(self):
        event = _format_sse_event("test_event", {"type": "test_event", "data": "hello"})
        assert event.startswith("event: test_event\n")
        assert "data: " in event
        assert event.endswith("\n\n")
        data = json.loads(event.split("data: ")[1].strip())
        assert data["type"] == "test_event"
