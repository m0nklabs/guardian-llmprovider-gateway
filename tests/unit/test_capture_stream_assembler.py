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

    def test_accumulates_legacy_completions_text_deltas(self):
        """Legacy /v1/completions chunks carry ``delta.text`` (no content)."""
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"text":"Once "}}]}')
        asm.add_sse_line('data: {"choices":[{"delta":{"text":"upon a time"}}]}')
        asm.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        result = asm.assemble()
        assert result["content"] == "Once upon a time"
        assert result["finish_reason"] == "stop"


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


class TestStreamAssemblerResponses:
    """OpenAI Responses SSE events (llama.cpp /v1/responses stream)."""

    def test_output_text_deltas_accumulate(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"type":"response.created","response":{"id":"r1"}}')
        asm.add_sse_line('data: {"type":"response.output_text.delta","delta":"Hello"}')
        asm.add_sse_line('data: {"type":"response.output_text.delta","delta":" world"}')
        result = asm.assemble()
        assert result["content"] == "Hello world"
        assert result["incomplete"] is True  # no terminal event yet

    def test_reasoning_text_deltas_accumulate(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"type":"response.reasoning_text.delta","delta":"Thinking"}')
        asm.add_sse_line('data: {"type":"response.reasoning_text.delta","delta":" hard"}')
        asm.add_sse_line('data: {"type":"response.output_text.delta","delta":"Answer"}')
        result = asm.assemble()
        assert result["content"] == "Answer"
        assert result["reasoning_content"] == "Thinking hard"

    def test_function_call_output_item_done_lands_in_tool_calls(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"type":"response.output_item.done","output_index":1,'
            '"item":{"type":"function_call","call_id":"call_9","name":"get_weather",'
            '"arguments":"{\\"city\\":\\"Boston\\"}","status":"completed"}}'
        )
        result = asm.assemble()
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["id"] == "call_9"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "get_weather"
        assert tc["function"]["arguments"] == '{"city":"Boston"}'

    def test_response_completed_sets_usage_and_stop_finish(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"type":"response.output_text.delta","delta":"Hi"}')
        asm.add_sse_line(
            'data: {"type":"response.completed",'
            '"response":{"status":"completed","usage":{"input_tokens":7,'
            '"output_tokens":3,"total_tokens":10}}}'
        )
        result = asm.assemble()
        assert result["prompt_tokens"] == 7
        assert result["completion_tokens"] == 3
        assert result["finish_reason"] == "stop"
        assert result["incomplete"] is False

    def test_response_incomplete_max_output_tokens_maps_to_length(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"type":"response.incomplete",'
            '"response":{"status":"incomplete",'
            '"incomplete_details":{"reason":"max_output_tokens"},'
            '"usage":{"input_tokens":7,"output_tokens":3}}}'
        )
        result = asm.assemble()
        assert result["finish_reason"] == "length"
        assert result["completion_tokens"] == 3

    def test_response_completed_maps_output_tokens_details_mirror(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"type":"response.completed",'
            '"response":{"status":"completed","usage":{"input_tokens":7,'
            '"output_tokens":3,"output_tokens_details":{"reasoning_tokens":2}}}}'
        )
        result = asm.assemble()
        assert result["completion_tokens_details"] == {"reasoning_tokens": 2}

    def test_non_terminal_responses_events_do_not_set_finish_reason(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"type":"response.in_progress","response":{"status":"in_progress"}}')
        result = asm.assemble()
        assert result["finish_reason"] is None
        assert result["content"] == ""

    def test_chat_behavior_unchanged_alongside_responses_events(self):
        """A Responses event must not corrupt a chat-completions stream."""
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"A"}}]}')
        asm.add_sse_line('data: {"type":"response.output_text.delta","delta":"B"}')
        asm.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        result = asm.assemble()
        assert result["content"] == "AB"
        assert result["finish_reason"] == "stop"


class TestStreamAssemblerUpstreamError:
    """In-stream upstream errors (HTTP 200 carrying an error payload, #125).

    The provider-defined code/type are metadata; the message is payload and
    must never be retained.
    """

    def test_error_only_stream_retains_code_and_type(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"error":{"code":502,"message":"Provider returned an empty '
            'response","type":"provider_error"}}'
        )
        result = asm.assemble()
        assert result["error_code"] == "502"
        assert result["error_type"] == "provider_error"
        # Existing contract untouched: no finish reason, still incomplete.
        assert result["finish_reason"] is None
        assert result["incomplete"] is True

    def test_error_then_done_keeps_metadata(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"error":{"code":502,"type":"provider_error"}}')
        asm.add_sse_line("data: [DONE]")
        result = asm.assemble()
        assert result["error_code"] == "502"
        assert result["error_type"] == "provider_error"

    def test_error_mixed_with_content_deltas(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"partial"}}]}')
        asm.add_sse_line('data: {"error":{"code":429,"type":"rate_limit_exceeded"}}')
        asm.add_sse_line('data: [DONE]')
        result = asm.assemble()
        assert result["content"] == "partial"
        assert result["error_code"] == "429"
        assert result["error_type"] == "rate_limit_exceeded"

    def test_normal_tool_call_stream_is_unchanged(self):
        """A normal stream keeps today's exact assembled key set (#125 is additive)."""
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"type":"function","function":{"name":"get_weather","arguments":"{}"}}]}}]}'
        )
        asm.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}')
        asm.add_sse_line("data: [DONE]")
        result = asm.assemble()
        assert set(result) == {
            "content",
            "finish_reason",
            "native_finish_reason",
            "completion_tokens_details",
            "native_tokens_reasoning",
            "native_tokens_cached",
            "cost",
            "provider_name",
            "tool_calls",
            "reasoning_content",
            "prompt_tokens",
            "completion_tokens",
            "incomplete",
        }
        assert result["finish_reason"] == "tool_calls"
        assert result["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_message_text_is_never_retained(self):
        message = "Provider returned an empty response"
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"error":{"code":502,"message":"%s","type":"provider_error"}}'
            % message
        )
        result = asm.assemble()
        assert message not in json.dumps(result)
        assert message.split()[0] not in json.dumps(result)

    def test_bare_string_error_with_prose_keeps_no_text(self):
        """A message-shaped bare string is dropped; the failure is still visible."""
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"error":"Provider returned an empty response"}')
        result = asm.assemble()
        assert result["error_code"] == "upstream_stream_error"
        assert "error_type" not in result
        assert "empty response" not in json.dumps(result)

    def test_bare_string_error_code_shaped_is_retained(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"error":"rate_limited"}')
        result = asm.assemble()
        assert result["error_code"] == "rate_limited"

    def test_error_metadata_is_bounded(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line(
            'data: {"error":{"code":"%s","type":"%s"}}' % ("9" * 200, "t" * 200)
        )
        result = asm.assemble()
        assert result["error_code"] == "9" * 64
        assert result["error_type"] == "t" * 64

    def test_error_without_code_or_type_falls_back(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"error":{"message":"boom"}}')
        result = asm.assemble()
        assert result["error_code"] == "upstream_stream_error"
        assert "error_type" not in result

    def test_non_error_payload_does_not_set_error_keys(self):
        asm = StreamResponseAssembler()
        asm.add_sse_line('data: {"choices":[{"delta":{"content":"ok"}}]}')
        asm.add_sse_line('data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}')
        result = asm.assemble()
        assert "error_code" not in result
        assert "error_type" not in result
