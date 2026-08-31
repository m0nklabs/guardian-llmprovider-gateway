"""Unit tests for the stream response assembler."""

import json
from app.capture.stream_assembler import StreamResponseAssembler


class TestStreamAssemblerOpenAI:
    def test_accumulates_content_deltas(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hello"}}]}')
        asm.add_sse_line('data: {"choices":[{"delta":{"content":" world"}}]}')
        result = asm.assemble()
        assert result["content"] == "Hello world"

    def test_extracts_finish_reason(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hello"}}]}')
        asm.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        result = asm.assemble()
        assert result["finish_reason"] == "stop"

    def test_extracts_usage_from_final_chunk(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
        asm.add_sse_line('data: {"usage":{"prompt_tokens":10,"completion_tokens":3}}')
        asm.add_sse_line('data: [DONE]')
        result = asm.assemble()
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 3

    def test_handles_empty_stream(self):
        asm = StreamResponseAssembler()
        result = asm.assemble()
        assert result["content"] == ""
        assert result["incomplete"] is True  # No finish_reason

    def test_accumulates_tool_calls(self):
        asm = StreamResponseAssembler()
        # First delta: tool call id + type
        payload = json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function"}]}}]})
        asm.add_sse_line(f"data: {payload}")
        # Second delta: function name + first part of arguments
        payload = json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "get_weather", "arguments": '{"city"'}}]}}]})
        asm.add_sse_line(f"data: {payload}")
        # Third delta: second part of arguments
        payload = json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ':"Boston"}'}}]}}]})
        asm.add_sse_line(f"data: {payload}")
        result = asm.assemble()
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["function"]["name"] == "get_weather"
        assert tc["function"]["arguments"] == '{"city":"Boston"}'

    def test_ignores_non_data_lines(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(": keepalive")
        asm.add_sse_line("")
        asm.add_sse_line("event: ping")
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
        result = asm.assemble()
        assert result["content"] == "Hi"

    def test_handles_malformed_json(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line("data: not-json")
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
        result = asm.assemble()
        assert result["content"] == "Hi"

    def test_accumulates_openrouter_reasoning_field(self):
        # OpenRouter proxies reasoning deltas as `delta.reasoning`
        # (DeepInfra-style), not `reasoning_content`.
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"reasoning":"1."}}]}')
        asm.add_sse_line('data: {"choices":[{"delta":{"reasoning":" Let me think"}}]}')
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Answer"},"finish_reason":"stop"}]}')
        result = asm.assemble()
        assert result["content"] == "Answer"
        assert result["reasoning_content"] == "1. Let me think"

    def test_reasoning_content_takes_precedence_over_reasoning(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"reasoning_content":"A","reasoning":"B"}}]}')
        result = asm.assemble()
        assert result["reasoning_content"] == "A"


class TestStreamAssemblerAnthropic:
    def test_accumulates_anthropic_deltas(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"type":"content_block_delta","index":0,"delta":{"text":"Hello"}}')
        asm.add_sse_line('data: {"type":"content_block_delta","index":0,"delta":{"text":" world"}}')
        asm.add_sse_line('data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}')
        result = asm.assemble()
        assert result["content"] == "Hello world"
        assert result["finish_reason"] == "end_turn"
        assert result["completion_tokens"] == 5

    def test_extracts_input_tokens_from_message_start(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}')
        asm.add_sse_line('data: {"type":"content_block_delta","delta":{"text":"Hi"}}')
        asm.add_sse_line('data: {"type":"message_delta","delta":{},"usage":{"output_tokens":2}}')
        result = asm.assemble()
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 2

    def test_uses_output_tokens_from_usage(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"usage":{"output_tokens":42}}')
        result = asm.assemble()
        assert result["completion_tokens"] == 42


class TestStreamAssemblerEdgeCases:
    def test_none_content_delta(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{}}]}')
        result = asm.assemble()
        assert result["content"] == ""

    def test_non_dict_data(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: "just a string"')
        asm.add_sse_line('data: [1, 2, 3]')
        result = asm.assemble()
        assert result["content"] == ""

    def test_has_content_property(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
        assert asm.has_content is True

    def test_is_empty_when_no_content(self):
        asm = StreamResponseAssembler()
        assert asm.is_empty is True

    def test_not_empty_when_content_present(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
        assert not asm.is_empty

    def test_get_usage_returns_tokens(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"usage":{"prompt_tokens":10,"completion_tokens":5}}')
        pt, ct = asm.get_usage()
        assert pt == 10
        assert ct == 5

    def test_get_usage_returns_none_when_absent(self):
        asm = StreamResponseAssembler()
        pt, ct = asm.get_usage()
        assert pt is None
        assert ct is None

    def test_uses_max_prompt_tokens(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"usage":{"prompt_tokens":10,"completion_tokens":5}}')
        asm.add_sse_line('data: {"usage":{"prompt_tokens":15,"completion_tokens":7}}')
        result = asm.assemble()
        assert result["prompt_tokens"] == 15
        assert result["completion_tokens"] == 7


class TestStreamAssemblerUsageMirror:
    """Rich upstream usage mirror collected from the final usage chunk (C5)."""

    def test_extracts_native_finish_reason_openrouter_shape(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"choices":[{"delta":{},"finish_reason":"stop",'
            '"native_finish_reason":"length"}]}'
        )
        result = asm.assemble()
        assert result["finish_reason"] == "stop"
        assert result["native_finish_reason"] == "length"

    def test_native_finish_reason_absent_for_plain_openai(self):
        # llama.cpp / plain OpenAI-compatible backends never send it.
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        result = asm.assemble()
        assert result["native_finish_reason"] is None

    def test_extracts_completion_tokens_details(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"usage":{"prompt_tokens":9,"completion_tokens":4,'
                         '"completion_tokens_details":{"reasoning_tokens":3}}}')
        result = asm.assemble()
        assert result["completion_tokens_details"] == {"reasoning_tokens": 3}

    def test_extracts_openrouter_native_token_counts_and_cost(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"provider":"DeepSeek","usage":{"prompt_tokens":100,'
            '"completion_tokens":20,"native_tokens_reasoning":7,'
            '"native_tokens_cached":11,"cost":0.0025}}'
        )
        result = asm.assemble()
        assert result["native_tokens_reasoning"] == 7
        assert result["native_tokens_cached"] == 11
        assert result["cost"] == 0.0025
        assert result["provider_name"] == "DeepSeek"

    def test_non_finite_usage_omitted_not_infinite(self):
        """1e999-style upstream numbers parse to inf: the rich mirror must
        omit them instead of raising OverflowError inside add_sse_line or
        serializing bare Infinity/NaN into the JSONL record."""
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"provider":"DeepSeek","usage":{"prompt_tokens":1e999,'
            '"completion_tokens":1e999,"native_tokens_reasoning":1e999,'
            '"native_tokens_cached":1e999,"cost":1e999}}'
        )
        result = asm.assemble()
        assert result["native_tokens_reasoning"] is None
        assert result["native_tokens_cached"] is None
        assert result["cost"] is None
        line = json.dumps(result, separators=(",", ":"))
        assert "Infinity" not in line and "NaN" not in line

    def test_mirror_fields_absent_when_provider_omits_them(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}')
        result = asm.assemble()
        assert result["completion_tokens_details"] is None
        assert result["native_tokens_reasoning"] is None
        assert result["native_tokens_cached"] is None
        assert result["cost"] is None
        assert result["provider_name"] is None

    def test_latest_provider_name_wins(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"provider":"A","choices":[{"delta":{"content":"x"}}]}')
        asm.add_sse_line('data: {"provider":"B","choices":[{"delta":{},"finish_reason":"stop"}]}')
        result = asm.assemble()
        assert result["provider_name"] == "B"
