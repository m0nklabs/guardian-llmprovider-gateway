"""Unit tests for app.proxy.anthropic_bridge — Anthropic ↔ OpenAI translation."""

import json
import pytest

from app.proxy.anthropic_bridge import (
    provider_needs_anthropic_translation,
    translate_anthropic_request_to_openai,
    translate_openai_error_to_anthropic,
    translate_openai_response_to_anthropic,
    translate_openai_stream_to_anthropic,
    _format_sse_event,
    _convert_content_blocks_to_openai,
    _convert_anthropic_tools_to_openai,
)


async def _async_iter(lines):
    """Helper: yield a list of strings as an async iterator."""
    for line in lines:
        yield line


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


# ── Image URL source ──────────────────────────────────────────────────


class TestImageUrlSource:
    def test_image_url_source_converted(self):
        """Anthropic image with source.type=url should become OpenAI image_url."""
        blocks = _convert_content_blocks_to_openai(
            [{"type": "image", "source": {"type": "url", "url": "https://example.com/img.png"}}],
            "user",
        )
        assert len(blocks) == 1
        assert blocks[0]["content"][0]["type"] == "image_url"
        assert blocks[0]["content"][0]["image_url"]["url"] == "https://example.com/img.png"


# ── PDF / document blocks ─────────────────────────────────────────────


class TestPdfDocumentBlocks:
    def test_pdf_base64_converted_to_data_url(self):
        """Anthropic document blocks should be passed as data URLs."""
        blocks = _convert_content_blocks_to_openai(
            [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0x"}}],
            "user",
        )
        assert len(blocks) == 1
        content = blocks[0]["content"]
        assert any(
            p.get("type") == "image_url" and "data:application/pdf;base64,JVBERi0x" in p["image_url"]["url"]
            for p in content
        )

    def test_pdf_url_source(self):
        """Anthropic document with URL source should become image_url."""
        blocks = _convert_content_blocks_to_openai(
            [{"type": "document", "source": {"type": "url", "url": "https://example.com/doc.pdf"}}],
            "user",
        )
        assert len(blocks) == 1
        content = blocks[0]["content"]
        assert any(
            p.get("type") == "image_url" and p["image_url"]["url"] == "https://example.com/doc.pdf"
            for p in content
        )


# ── Thinking blocks in request ─────────────────────────────────────────


class TestThinkingBlocksInRequest:
    def test_thinking_block_converted_to_text(self):
        """Thinking blocks in assistant messages should become text for OpenAI."""
        blocks = _convert_content_blocks_to_openai(
            [
                {"type": "thinking", "thinking": "Let me reason about this...", "signature": "sig123"},
                {"type": "text", "text": "The answer is 42."},
            ],
            "assistant",
        )
        # Should produce one message with combined text
        assert len(blocks) == 1
        content = blocks[0]["content"]
        assert "Let me reason about this..." in content
        assert "The answer is 42." in content

    def test_redacted_thinking_skipped(self):
        """Redacted thinking blocks should be silently dropped."""
        blocks = _convert_content_blocks_to_openai(
            [
                {"type": "redacted_thinking", "data": "cmVkYWN0ZWQ="},
                {"type": "text", "text": "Hello"},
            ],
            "assistant",
        )
        assert len(blocks) == 1
        assert blocks[0]["content"] == "Hello"


# ── Thinking in non-streaming response ────────────────────────────────


class TestThinkingInResponse:
    def test_reasoning_content_becomes_thinking_block(self):
        """OpenAI reasoning_content should become a thinking block."""
        resp = translate_openai_response_to_anthropic(
            {
                "id": "chatcmpl-1",
                "choices": [{
                    "message": {
                        "reasoning_content": "I need to calculate 6*7.",
                        "content": "The answer is 42.",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            "test-model",
        )
        blocks = resp["content"]
        assert blocks[0]["type"] == "thinking"
        assert blocks[0]["thinking"] == "I need to calculate 6*7."
        assert blocks[1]["type"] == "text"
        assert blocks[1]["text"] == "The answer is 42."

    def test_reasoning_field_also_handled(self):
        """Some providers use 'reasoning' instead of 'reasoning_content'."""
        resp = translate_openai_response_to_anthropic(
            {
                "choices": [{
                    "message": {"reasoning": "Thinking...", "content": "Answer."},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            "m",
        )
        assert resp["content"][0]["type"] == "thinking"
        assert resp["content"][0]["thinking"] == "Thinking..."


# ── Cache usage fields ─────────────────────────────────────────────────


class TestCacheUsageFields:
    def test_cache_usage_in_non_streaming_response(self):
        """Non-streaming response should include cache usage fields."""
        resp = translate_openai_response_to_anthropic(
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 20,
                },
            },
            "m",
        )
        assert resp["usage"]["cache_creation_input_tokens"] == 10
        assert resp["usage"]["cache_read_input_tokens"] == 20

    def test_cache_usage_defaults_to_zero(self):
        """Cache usage fields should default to 0 when not present."""
        resp = translate_openai_response_to_anthropic(
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
            "m",
        )
        assert resp["usage"]["cache_creation_input_tokens"] == 0
        assert resp["usage"]["cache_read_input_tokens"] == 0

    @pytest.mark.asyncio
    async def test_cache_usage_in_streaming_message_delta(self):
        """Streaming message_delta should include cache usage fields."""
        chunks = [
            'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3}}',
            "data: [DONE]",
        ]
        events = []
        async for evt in translate_openai_stream_to_anthropic(_async_iter(chunks), "m"):
            events.append(evt)
        msg_delta = [json.loads(e.split("data: ")[1].strip()) for e in events if "message_delta" in e]
        assert msg_delta[0]["usage"]["cache_creation_input_tokens"] == 0
        assert msg_delta[0]["usage"]["cache_read_input_tokens"] == 0


# ── Thinking in streaming ─────────────────────────────────────────────


class TestThinkingInStreaming:
    @pytest.mark.asyncio
    async def test_reasoning_content_produces_thinking_delta(self):
        """Streaming reasoning_content should produce thinking_delta events."""
        chunks = [
            'data: {"choices":[{"delta":{"reasoning_content":"Let me think..."},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"reasoning_content":" about this."},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"42"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = []
        async for evt in translate_openai_stream_to_anthropic(_async_iter(chunks), "m"):
            events.append(evt)
        parsed = [json.loads(e.split("data: ")[1].strip()) for e in events if e.startswith("event:")]

        # Should have thinking_delta events
        thinking_deltas = [p for p in parsed if p.get("delta", {}).get("type") == "thinking_delta"]
        assert len(thinking_deltas) == 2
        assert thinking_deltas[0]["delta"]["thinking"] == "Let me think..."
        assert thinking_deltas[1]["delta"]["thinking"] == " about this."

        # Should have a thinking content_block_start
        starts = [p for p in parsed if p.get("type") == "content_block_start"]
        thinking_starts = [s for s in starts if s["content_block"]["type"] == "thinking"]
        assert len(thinking_starts) == 1

        # Should also have text content_block_start and text_delta
        text_starts = [s for s in starts if s["content_block"]["type"] == "text"]
        assert len(text_starts) == 1
        text_deltas = [p for p in parsed if p.get("delta", {}).get("type") == "text_delta"]
        assert len(text_deltas) == 1
        assert text_deltas[0]["delta"]["text"] == "42"

        # Thinking block should be closed before text block starts
        thinking_stops = [p for p in parsed if p.get("type") == "content_block_stop" and p["index"] == thinking_starts[0]["index"]]
        assert len(thinking_stops) == 1

    @pytest.mark.asyncio
    async def test_thinking_then_tool_use(self):
        """Thinking deltas should close before tool_use blocks start."""
        chunks = [
            'data: {"choices":[{"delta":{"reasoning_content":"Thinking..."},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"Paris\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = []
        async for evt in translate_openai_stream_to_anthropic(_async_iter(chunks), "m"):
            events.append(evt)
        parsed = [json.loads(e.split("data: ")[1].strip()) for e in events if e.startswith("event:")]

        # Should have thinking_delta
        thinking_deltas = [p for p in parsed if p.get("delta", {}).get("type") == "thinking_delta"]
        assert len(thinking_deltas) == 1

        # Should have tool_use content_block_start
        tool_starts = [s for s in parsed if s.get("type") == "content_block_start" and s["content_block"]["type"] == "tool_use"]
        assert len(tool_starts) == 1

        # Should have input_json_delta
        json_deltas = [p for p in parsed if p.get("delta", {}).get("type") == "input_json_delta"]
        assert len(json_deltas) == 1

        # stop_reason should be tool_use
        msg_delta = [p for p in parsed if p.get("type") == "message_delta"]
        assert msg_delta[0]["delta"]["stop_reason"] == "tool_use"


# ── Error translation ─────────────────────────────────────────────────


class TestErrorTranslation:
    def test_openai_error_to_anthropic_format(self):
        """OpenAI error body should become Anthropic error format."""
        result = translate_openai_error_to_anthropic(
            400,
            {"error": {"message": "Invalid model", "type": "invalid_request_error"}},
        )
        assert result["type"] == "error"
        assert result["error"]["type"] == "invalid_request_error"
        assert result["error"]["message"] == "Invalid model"

    def test_error_status_code_to_type_mapping(self):
        """HTTP status codes should map to correct Anthropic error types."""
        assert translate_openai_error_to_anthropic(401, {"error": {"message": "x"}})["error"]["type"] == "authentication_error"
        assert translate_openai_error_to_anthropic(403, {"error": {"message": "x"}})["error"]["type"] == "permission_error"
        assert translate_openai_error_to_anthropic(429, {"error": {"message": "x"}})["error"]["type"] == "rate_limit_error"
        assert translate_openai_error_to_anthropic(503, {"error": {"message": "x"}})["error"]["type"] == "overloaded_error"
        assert translate_openai_error_to_anthropic(500, {"error": {"message": "x"}})["error"]["type"] == "api_error"

    def test_error_with_string_body(self):
        """Non-JSON error body should still produce a valid error."""
        result = translate_openai_error_to_anthropic(502, "Bad Gateway")
        assert result["type"] == "error"
        assert result["error"]["message"] == "Bad Gateway"

    def test_error_preserves_type_from_body(self):
        """If OpenAI body has a type, it should be preserved."""
        result = translate_openai_error_to_anthropic(
            400,
            {"error": {"message": "Rate limited", "type": "rate_limit_exceeded"}},
        )
        assert result["error"]["type"] == "rate_limit_exceeded"

    def test_error_extracts_detail_field(self):
        """Fallback to 'detail' field if 'message' is missing."""
        result = translate_openai_error_to_anthropic(
            422,
            {"error": {"detail": "Validation failed"}},
        )
        assert result["error"]["message"] == "Validation failed"


# ── Stop sequence detection ──────────────────────────────────────────


class TestStopSequenceDetection:
    def test_stop_sequence_detected_non_streaming(self):
        """When the response text ends with a requested stop sequence, stop_reason
        should be 'stop_sequence' and stop_sequence should contain the matched string."""
        resp = translate_openai_response_to_anthropic(
            {
                "choices": [{
                    "message": {"content": "Hello world\nSTOP"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
            "m",
            request_stop_sequences=["STOP", "END"],
        )
        assert resp["stop_reason"] == "stop_sequence"
        assert resp["stop_sequence"] == "STOP"

    def test_no_stop_sequence_when_not_matched(self):
        """If text doesn't end with any stop sequence, stop_reason stays 'end_turn'."""
        resp = translate_openai_response_to_anthropic(
            {
                "choices": [{
                    "message": {"content": "Hello world"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
            "m",
            request_stop_sequences=["STOP", "END"],
        )
        assert resp["stop_reason"] == "end_turn"
        assert resp["stop_sequence"] is None

    def test_no_stop_sequences_in_request(self):
        """If no stop_sequences in the request, stop_reason stays 'end_turn'."""
        resp = translate_openai_response_to_anthropic(
            {
                "choices": [{
                    "message": {"content": "Hello"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            "m",
        )
        assert resp["stop_reason"] == "end_turn"
        assert resp["stop_sequence"] is None

    @pytest.mark.asyncio
    async def test_stop_sequence_detected_streaming(self):
        """Streaming should also detect stop sequences in accumulated text."""
        chunks = [
            'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"STOP"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = []
        async for evt in translate_openai_stream_to_anthropic(
            _async_iter(chunks), "m", request_stop_sequences=["STOP"],
        ):
            events.append(evt)
        msg_delta = [json.loads(e.split("data: ")[1].strip()) for e in events if "message_delta" in e]
        assert msg_delta[0]["delta"]["stop_reason"] == "stop_sequence"
        assert msg_delta[0]["delta"]["stop_sequence"] == "STOP"


# ── Interleaved text + tool_use in streaming ─────────────────────────


class TestInterleavedStreaming:
    @pytest.mark.asyncio
    async def test_text_after_tool_use_gets_new_block(self):
        """Text appearing after a tool_use block should get a new content block index,
        not reuse the closed text block."""
        chunks = [
            # First text block
            'data: {"choices":[{"delta":{"content":"Let me check"},"finish_reason":null}]}',
            # Tool call
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"q\\":\\"Paris\\"}"}}]},"finish_reason":null}]}',
            # More text after tool call
            'data: {"choices":[{"delta":{"content":"Based on the weather..."},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = []
        async for evt in translate_openai_stream_to_anthropic(_async_iter(chunks), "m"):
            events.append(evt)
        parsed = [json.loads(e.split("data: ")[1].strip()) for e in events if e.startswith("event:")]

        # Should have 2 text content_block_starts (different indices)
        text_starts = [p for p in parsed if p.get("type") == "content_block_start" and p["content_block"]["type"] == "text"]
        assert len(text_starts) == 2
        assert text_starts[0]["index"] != text_starts[1]["index"]

        # Should have 1 tool_use block
        tool_starts = [p for p in parsed if p.get("type") == "content_block_start" and p["content_block"]["type"] == "tool_use"]
        assert len(tool_starts) == 1

        # All blocks should be closed
        starts_count = len([p for p in parsed if p.get("type") == "content_block_start"])
        stops_count = len([p for p in parsed if p.get("type") == "content_block_stop"])
        assert starts_count == stops_count

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_different_indices(self):
        """Multiple text segments separated by tool calls should each get unique indices."""
        chunks = [
            'data: {"choices":[{"delta":{"content":"First"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"f","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"Second"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"c2","type":"function","function":{"name":"g","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"Third"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = []
        async for evt in translate_openai_stream_to_anthropic(_async_iter(chunks), "m"):
            events.append(evt)
        parsed = [json.loads(e.split("data: ")[1].strip()) for e in events if e.startswith("event:")]

        text_starts = [p for p in parsed if p.get("type") == "content_block_start" and p["content_block"]["type"] == "text"]
        tool_starts = [p for p in parsed if p.get("type") == "content_block_start" and p["content_block"]["type"] == "tool_use"]

        assert len(text_starts) == 3  # three separate text blocks
        assert len(tool_starts) == 2  # two tool_use blocks

        # All 5 blocks should be closed
        starts_count = len([p for p in parsed if p.get("type") == "content_block_start"])
        stops_count = len([p for p in parsed if p.get("type") == "content_block_stop"])
        assert starts_count == stops_count

        # All indices should be unique
        all_indices = [p["index"] for p in parsed if p.get("type") == "content_block_start"]
        assert len(all_indices) == len(set(all_indices))
