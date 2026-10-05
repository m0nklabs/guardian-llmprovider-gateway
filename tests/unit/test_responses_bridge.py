"""Unit tests for the OpenAI Responses bridge helpers (app.proxy.responses_bridge)."""

import json

import pytest

from app.proxy.responses_bridge import (
    ResponsesRequestError,
    provider_needs_responses_translation,
    responses_contains_image_input,
    translate_chat_error_to_responses,
    translate_chat_response_to_responses,
    translate_openai_stream_to_responses,
    translate_responses_request_to_chat,
)


class TestResponsesContainsImageInput:
    def test_string_input_has_no_images(self):
        assert responses_contains_image_input("hello") is False

    def test_none_input_has_no_images(self):
        assert responses_contains_image_input(None) is False

    def test_non_list_input_has_no_images(self):
        assert responses_contains_image_input({"type": "input_image"}) is False

    def test_text_only_items_have_no_images(self):
        body_input = [
            {"type": "message", "role": "user", "content": "plain string"},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "describe this"}],
            },
        ]
        assert responses_contains_image_input(body_input) is False

    def test_input_image_part_detected(self):
        body_input = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is in this image?"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
            }
        ]
        assert responses_contains_image_input(body_input) is True

    def test_input_image_after_other_items_detected(self):
        body_input = [
            {"type": "message", "role": "user", "content": "first turn"},
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "https://example.com/i.png"}
                ],
            },
        ]
        assert responses_contains_image_input(body_input) is True

    def test_non_dict_items_ignored(self):
        assert responses_contains_image_input(["just a string", 42, None]) is False

    def test_non_dict_content_parts_ignored(self):
        body_input = [{"type": "message", "role": "user", "content": ["text", 7]}]
        assert responses_contains_image_input(body_input) is False


def test_gateway_routing_wires_responses_image_detection():
    """Wiring regression: gateway/routing.py must import the Responses image
    detector from the bridge — the vision-fallback/preflight path depends on
    it for /v1/responses requests carrying input_image parts."""
    from app.gateway import routing as gw_routing

    assert gw_routing.responses_contains_image_input is responses_contains_image_input


# ── Request translation ──────────────────────────────────────────────


class TestTranslateResponsesRequestToChat:
    def test_string_input_becomes_single_user_message(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hello there",
        })
        assert chat["model"] == "m"
        assert chat["messages"] == [{"role": "user", "content": "hello there"}]

    def test_input_item_list_converts_roles_and_parts(self):
        chat = translate_responses_request_to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "developer", "content": "be brief"},
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "look at this"},
                    {"type": "input_image", "image_url": "https://x/img.png"},
                ]},
            ],
        })
        messages = chat["messages"]
        assert messages[0] == {"role": "system", "content": "be brief"}
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
        ]

    def test_function_call_and_output_round_trip(self):
        chat = translate_responses_request_to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "weather in Paris?"},
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_abc",
                    "output": [{"type": "input_text", "text": "sunny, 22C"}],
                },
            ],
        })
        messages = chat["messages"]
        assert messages[1]["role"] == "assistant"
        assert messages[1]["tool_calls"] == [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }]
        assert messages[2] == {
            "role": "tool", "tool_call_id": "call_abc", "content": "sunny, 22C",
        }

    def test_consecutive_assistant_items_merge(self):
        chat = translate_responses_request_to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "go on"},
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "part one"},
                ]},
                {"type": "message", "role": "assistant", "content": "part two"},
            ],
        })
        assistant = chat["messages"][1]
        assert assistant["role"] == "assistant"
        assert "part one" in assistant["content"]
        assert "part two" in assistant["content"]

    def test_reasoning_item_becomes_reasoning_content(self):
        chat = translate_responses_request_to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "think"},
                {"type": "reasoning", "content": [
                    {"type": "reasoning_text", "text": "hmm..."},
                ]},
                {"type": "message", "role": "assistant", "content": "answer"},
            ],
        })
        # reasoning arrives before its assistant message: a carrying assistant
        # message is created and the following assistant message merges into it.
        assistant = chat["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["reasoning_content"] == "hmm..."
        assert "answer" in assistant["content"]

    def test_instructions_become_leading_system_message(self):
        chat = translate_responses_request_to_chat({
            "model": "m",
            "instructions": "You are terse.",
            "input": "hi",
        })
        assert chat["messages"][0] == {
            "role": "system", "content": "You are terse.",
        }
        assert chat["messages"][1]["role"] == "user"

    def test_tools_flat_to_nested_and_non_function_dropped(self):
        chat = translate_responses_request_to_chat({
            "model": "m",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
                {"type": "web_search"},
            ],
        })
        assert len(chat["tools"]) == 1
        assert chat["tools"][0] == {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        # strict must NOT be injected into the nested function (cloud providers
        # may reject it) and the web_search tool must be dropped.
        assert "strict" not in chat["tools"][0]["function"]

    def test_tool_choice_mapping(self):
        body = {"model": "m", "input": "hi"}
        for responses_choice, expected in [
            ("auto", "auto"),
            ("none", "none"),
            ("required", "required"),
            ({"type": "function", "name": "get_weather"},
             {"type": "function", "function": {"name": "get_weather"}}),
        ]:
            chat = translate_responses_request_to_chat(
                {**body, "tool_choice": responses_choice}
            )
            assert chat["tool_choice"] == expected

    def test_max_output_tokens_maps_to_max_tokens(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi", "max_output_tokens": 123,
        })
        assert chat["max_tokens"] == 123
        assert "max_output_tokens" not in chat

    def test_reasoning_effort_maps_to_reasoning_effort(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi", "reasoning": {"effort": "high"},
        })
        assert chat["reasoning_effort"] == "high"

    def test_text_format_json_schema_maps_to_response_format(self):
        schema = {"type": "object", "properties": {"a": {"type": "number"}}}
        chat = translate_responses_request_to_chat({
            "model": "m",
            "input": "hi",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": schema,
                    "strict": True,
                },
            },
        })
        assert chat["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": schema, "strict": True},
        }

    def test_text_format_json_object_and_text_dropped(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi",
            "text": {"format": {"type": "json_object"}},
        })
        assert chat["response_format"] == {"type": "json_object"}
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi", "text": {"format": {"type": "text"}},
        })
        assert "response_format" not in chat

    def test_stream_true_adds_stream_options_include_usage(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi", "stream": True,
        })
        assert chat["stream"] is True
        assert chat["stream_options"] == {"include_usage": True}

    @pytest.mark.parametrize("tier", ["default", "flex", "priority", "fast", "ultrafast"])
    def test_service_tier_passthrough(self, tier):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi", "service_tier": tier,
        })
        assert chat["service_tier"] == tier

    def test_service_tier_absent_stays_absent(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi",
        })
        assert "service_tier" not in chat

    def test_unsupported_keys_dropped_silently(self):
        chat = translate_responses_request_to_chat({
            "model": "m", "input": "hi",
            "store": True, "background": True, "include": ["x"], "truncation": "auto",
        })
        for key in ("store", "background", "include", "truncation"):
            assert key not in chat

    def test_non_empty_previous_response_id_rejected(self):
        with pytest.raises(ResponsesRequestError):
            translate_responses_request_to_chat({
                "model": "m", "input": "hi", "previous_response_id": "resp_123",
            })

    def test_item_reference_rejected(self):
        with pytest.raises(ResponsesRequestError):
            translate_responses_request_to_chat({
                "model": "m",
                "input": [{"type": "item_reference", "id": "item_1"}],
            })

    def test_input_file_part_rejected(self):
        with pytest.raises(ResponsesRequestError):
            translate_responses_request_to_chat({
                "model": "m",
                "input": [
                    {"type": "message", "role": "user", "content": [
                        {"type": "input_file", "file_data": "pdf-bytes"},
                    ]},
                ],
            })


# ── Response back-translation ────────────────────────────────────────


class TestTranslateChatResponseToResponses:
    def test_text_message_becomes_output_item(self):
        resp = translate_chat_response_to_responses({
            "id": "chatcmpl-1",
            "created": 1700000000,
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello!"},
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }, "test-model")
        assert resp["id"].startswith("resp_")
        assert resp["object"] == "response"
        assert resp["created_at"] == 1700000000
        assert resp["completed_at"] == 1700000000
        assert resp["status"] == "completed"
        assert resp["error"] is None
        assert resp["model"] == "test-model"
        message_item = resp["output"][0]
        assert message_item["type"] == "message"
        assert message_item["role"] == "assistant"
        assert message_item["content"] == [
            {"type": "output_text", "text": "Hello!", "annotations": []},
        ]

    def test_tool_calls_become_function_call_items(self):
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }],
                },
            }],
        }, "test-model")
        types = [item["type"] for item in resp["output"]]
        assert types == ["message", "function_call"]
        call_item = resp["output"][1]
        assert call_item["call_id"] == "call_1"
        assert call_item["name"] == "get_weather"
        assert call_item["arguments"] == '{"city":"Paris"}'
        assert call_item["status"] == "completed"

    def test_reasoning_content_becomes_reasoning_item(self):
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Answer",
                    "reasoning_content": "thinking hard",
                },
            }],
        }, "test-model")
        reasoning_item = resp["output"][0]
        assert reasoning_item["type"] == "reasoning"
        assert reasoning_item["id"].startswith("rs_")
        assert reasoning_item["summary"] == []
        assert reasoning_item["content"] == [
            {"type": "reasoning_text", "text": "thinking hard"},
        ]

    def test_usage_mapping_with_details(self):
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }, "test-model")
        usage = resp["usage"]
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 4
        assert usage["total_tokens"] == 14
        assert usage["input_tokens_details"] == {"cached_tokens": 3}
        assert usage["output_tokens_details"] == {"reasoning_tokens": 2}

    def test_length_finish_becomes_incomplete_with_reason(self):
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
        }, "test-model")
        assert resp["status"] == "incomplete"
        assert resp["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_source_body_parameters_echoed(self):
        source_body = {
            "model": "client/model",
            "instructions": "be brief",
            "temperature": 0.5,
            "top_p": 0.9,
            "tools": [{"type": "function", "name": "f", "parameters": {}}],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "max_output_tokens": 64,
            "metadata": {"k": "v"},
            "reasoning": {"effort": "low"},
        }
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
        }, "test-model", source_body=source_body)
        for key, value in source_body.items():
            if key == "model":
                continue
            assert resp[key] == value
        assert resp["previous_response_id"] is None
        assert resp["store"] is False

    def test_served_service_tier_reported_from_chat_payload(self):
        """The response's service_tier reflects the tier that ACTUALLY served
        the request (upstream chat payload), not the requested one —
        OpenRouter/OpenAI semantics."""
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "service_tier": "default",
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
        }, "test-model", source_body={"service_tier": "priority"})
        assert resp["service_tier"] == "default"

    def test_requested_service_tier_echoed_without_upstream_report(self):
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
        }, "test-model", source_body={"service_tier": "flex"})
        assert resp["service_tier"] == "flex"

    def test_service_tier_absent_everywhere_stays_absent(self):
        resp = translate_chat_response_to_responses({
            "created": 1700000000,
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
        }, "test-model")
        assert "service_tier" not in resp


# ── Error translation ────────────────────────────────────────────────


class TestTranslateChatErrorToResponses:
    def test_openai_envelope_passthrough(self):
        err = translate_chat_error_to_responses(429, {
            "error": {"message": "rate limited", "type": "rate_limit_error", "code": "r1"},
        })
        assert err == {"error": {
            "message": "rate limited", "type": "rate_limit_error", "code": "r1",
        }}

    def test_detail_string_shape(self):
        err = translate_chat_error_to_responses(502, {"detail": "upstream exploded"})
        assert err["error"]["message"] == "upstream exploded"
        assert err["error"]["type"] == "api_error"
        assert err["error"]["code"] is None

    def test_plain_string_shape_and_status_mapping(self):
        err = translate_chat_error_to_responses(401, "bad key")
        assert err == {"error": {
            "message": "bad key", "type": "authentication_error", "code": None,
        }}


# ── Provider gate ────────────────────────────────────────────────────


class TestProviderNeedsResponsesTranslation:
    def test_true_only_for_responses_path(self):
        assert provider_needs_responses_translation("nvidia", "responses") is True
        assert provider_needs_responses_translation("openrouter", "responses") is True
        assert provider_needs_responses_translation("nvidia", "chat/completions") is False
        assert provider_needs_responses_translation("nvidia", "messages") is False


# ── Streaming translation ────────────────────────────────────────────


def _parse_sse_events(chunks: list[str]) -> list[tuple[str, dict]]:
    """Parse (event_name, data_dict) pairs out of collected SSE chunks."""
    events: list[tuple[str, dict]] = []
    for chunk in chunks:
        for block in chunk.split("\n\n"):
            if not block.strip():
                continue
            event_name = None
            data = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            if event_name and data is not None:
                events.append((event_name, data))
    return events


async def _collect(async_gen) -> list[str]:
    return [chunk async for chunk in async_gen]


class TestTranslateOpenaiStreamToResponses:
    async def test_text_stream_event_flow_and_completed_usage(self):
        async def chat_lines():
            yield 'data: {"choices":[{"delta":{"content":"Hel"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"lo"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield ('data: {"choices":[],"usage":{"prompt_tokens":3,'
                   '"completion_tokens":2,"total_tokens":5}}')
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="test-model",
        ))
        events = _parse_sse_events(chunks)
        names = [name for name, _ in events]

        # Ordered Responses event flow with exactly one occurrence each of
        # created/in_progress at the start and completed at the end.
        assert names[0] == "response.created"
        assert names[1] == "response.in_progress"
        assert names.count("response.output_item.added") == 1
        assert names.count("response.content_part.added") == 1
        assert names.count("response.output_text.delta") == 2
        assert names.count("response.output_text.done") == 1
        assert names.count("response.content_part.done") == 1
        assert names.count("response.output_item.done") == 1
        assert names[-1] == "response.completed"

        delta_events = [data for name, data in events
                        if name == "response.output_text.delta"]
        assert [d["delta"] for d in delta_events] == ["Hel", "lo"]
        assert delta_events[0]["item_id"].startswith("msg_")

        text_done = next(data for name, data in events
                         if name == "response.output_text.done")
        assert text_done["text"] == "Hello"

        completed = next(data for name, data in events
                         if name == "response.completed")
        response = completed["response"]
        created_resp = next(data for name, data in events
                            if name == "response.created")["response"]
        assert response["id"] == created_resp["id"]
        assert response["object"] == "response"
        assert response["status"] == "completed"
        assert response["model"] == "test-model"
        assert response["usage"]["input_tokens"] == 3
        assert response["usage"]["output_tokens"] == 2
        assert response["usage"]["total_tokens"] == 5

    async def test_created_and_completed_share_response_id(self):
        async def chat_lines():
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}'
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        events = _parse_sse_events(chunks)
        created_resp = next(data for name, data in events
                            if name == "response.created")["response"]
        completed_resp = next(data for name, data in events
                              if name == "response.completed")["response"]
        assert created_resp["id"] == completed_resp["id"]
        assert created_resp["id"].startswith("resp_")
        assert created_resp["status"] == "in_progress"
        assert completed_resp["status"] == "completed"

    async def test_tool_call_stream_emits_function_call_arguments_events(self):
        async def chat_lines():
            yield ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                   '"id":"call_9","type":"function","function":'
                   '{"name":"get_weather","arguments":""}}]}}]}')
            yield ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                   '"function":{"arguments":"{\\"city\\""}}]}}]}')
            yield ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                   '"function":{"arguments":":\\"Paris\\"}"}}]}}]}')
            yield 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}'
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        events = _parse_sse_events(chunks)
        names = [name for name, _ in events]
        assert "response.function_call_arguments.delta" in names
        assert "response.function_call_arguments.done" in names
        assert names[-1] == "response.completed"

        arg_deltas = [data["delta"] for name, data in events
                      if name == "response.function_call_arguments.delta"]
        assert "".join(arg_deltas) == '{"city":"Paris"}'

        args_done = next(data for name, data in events
                         if name == "response.function_call_arguments.done")
        assert args_done["arguments"] == '{"city":"Paris"}'

        completed = next(data for name, data in events
                         if name == "response.completed")["response"]
        call_items = [i for i in completed["output"] if i["type"] == "function_call"]
        assert len(call_items) == 1
        assert call_items[0]["call_id"] == "call_9"
        assert call_items[0]["name"] == "get_weather"
        assert call_items[0]["arguments"] == '{"city":"Paris"}'
        assert completed["status"] == "completed"

    async def test_reasoning_deltas_become_reasoning_text_events(self):
        async def chat_lines():
            yield 'data: {"choices":[{"delta":{"reasoning_content":"think..."}}]}'
            yield 'data: {"choices":[{"delta":{"reasoning_content":"more"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"answer"}}]}'
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        events = _parse_sse_events(chunks)
        names = [name for name, _ in events]
        assert names.count("response.reasoning_text.delta") == 2
        assert "response.reasoning_text.done" in names
        # The reasoning item (first output_item.added) must be CLOSED before
        # the text item (message-typed output_item.added) opens.
        message_item_added = next(
            i for i, (name, data) in enumerate(events)
            if name == "response.output_item.added" and data["item"]["type"] == "message"
        )
        assert names.index("response.reasoning_text.done") < message_item_added

        reasoning_deltas = [data["delta"] for name, data in events
                            if name == "response.reasoning_text.delta"]
        assert reasoning_deltas == ["think...", "more"]

        completed = next(data for name, data in events
                         if name == "response.completed")["response"]
        reasoning_items = [i for i in completed["output"] if i["type"] == "reasoning"]
        assert reasoning_items and reasoning_items[0]["content"][0]["text"] == "think...more"

    async def test_midstream_error_passthrough_and_stop(self):
        async def chat_lines():
            yield 'data: {"choices":[{"delta":{"content":"par"}}]}'
            yield 'data: {"error": {"message": "boom", "type": "api_error"}}'
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        # Last chunk is the raw error data line (llama.cpp parity).
        assert chunks[-1].startswith('data: {"error"')
        events = _parse_sse_events(chunks[:-1])
        assert [name for name, _ in events][-1] != "response.completed"

    async def test_keepalive_comments_pass_through(self):
        async def chat_lines():
            yield ": guardian-keepalive"
            yield ""
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        assert chunks[0].startswith(": guardian-keepalive")
        events = _parse_sse_events(chunks)
        assert events[-1][0] == "response.completed"

    async def test_length_cut_stream_ends_with_response_incomplete(self):
        """A chat stream cut by finish_reason "length" must end with the
        response.incomplete event (status "incomplete" +
        incomplete_details.reason "max_output_tokens"), carrying usage —
        official semantics (LiteLLM bridge parity)."""
        async def chat_lines():
            yield 'data: {"choices":[{"delta":{"content":"partial ans"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'
            yield ('data: {"choices":[],"usage":{"prompt_tokens":4,'
                   '"completion_tokens":2,"total_tokens":6}}')
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        events = _parse_sse_events(chunks)
        names = [name for name, _ in events]
        assert names[-1] == "response.incomplete"
        assert "response.completed" not in names

        terminal = events[-1][1]
        response = terminal["response"]
        assert response["status"] == "incomplete"
        assert response["incomplete_details"] == {"reason": "max_output_tokens"}
        assert response["usage"]["input_tokens"] == 4
        assert response["usage"]["output_tokens"] == 2

    async def test_content_filter_cut_stream_ends_with_response_incomplete(self):
        async def chat_lines():
            yield 'data: {"choices":[{"delta":{"content":"no"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}'
            yield "data: [DONE]"

        chunks = await _collect(translate_openai_stream_to_responses(
            chat_lines(), model="m",
        ))
        events = _parse_sse_events(chunks)
        assert events[-1][0] == "response.incomplete"
        response = events[-1][1]["response"]
        assert response["status"] == "incomplete"
        assert response["incomplete_details"] == {"reason": "content_filter"}


class TestIncompleteFinishReasonMapping:
    def test_nonstream_length_maps_to_max_output_tokens(self):
        payload = {
            "created": 1,
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "partial"},
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        response = translate_chat_response_to_responses(payload, "m")
        assert response["status"] == "incomplete"
        assert response["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_nonstream_content_filter_maps_to_content_filter(self):
        payload = {
            "created": 1,
            "choices": [{
                "finish_reason": "content_filter",
                "message": {"content": ""},
            }],
        }
        response = translate_chat_response_to_responses(payload, "m")
        assert response["status"] == "incomplete"
        assert response["incomplete_details"] == {"reason": "content_filter"}

    def test_nonstream_refusal_maps_to_content_filter(self):
        payload = {
            "created": 1,
            "choices": [{
                "finish_reason": "refusal",
                "message": {"refusal": "I cannot help with that."},
            }],
        }
        response = translate_chat_response_to_responses(payload, "m")
        assert response["status"] == "incomplete"
        assert response["incomplete_details"] == {"reason": "content_filter"}
        # The refusal text still surfaces as a refusal content part.
        content = response["output"][0]["content"]
        assert content[0]["type"] == "refusal"

    def test_nonstream_stop_maps_to_completed(self):
        payload = {
            "created": 1,
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        }
        response = translate_chat_response_to_responses(payload, "m")
        assert response["status"] == "completed"
        assert response["incomplete_details"] is None


class TestResponsesCaptureRequest:
    """responses_capture_request — fail-open capture normalization."""

    def test_string_input_becomes_single_user_message(self):
        from app.proxy.responses_bridge import responses_capture_request

        body = {"model": "m", "input": "hello", "temperature": 0.5}
        messages, params = responses_capture_request(body)
        assert messages == [{"role": "user", "content": "hello"}]
        assert params == {"model": "m", "temperature": 0.5}

    def test_item_list_converts_to_chat_messages(self):
        from app.proxy.responses_bridge import responses_capture_request

        body = {
            "model": "m",
            "instructions": "be brief",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
        messages, params = responses_capture_request(body)
        assert messages == [{"role": "user", "content": "hi"}]
        assert params == {"model": "m"}
        assert "instructions" not in params

    def test_unsupported_item_type_skips_messages_not_raises(self):
        from app.proxy.responses_bridge import responses_capture_request

        body = {
            "model": "m",
            "input": [{"type": "input_file", "file": {"id": "f1"}}],
        }
        messages, params = responses_capture_request(body)
        assert messages is None  # capture skips the messages
        assert params == {"model": "m"}  # params still available

    def test_params_exclude_input_and_instructions(self):
        from app.proxy.responses_bridge import responses_capture_request

        body = {
            "model": "m",
            "input": [],
            "instructions": "sys",
            "temperature": 1,
            "max_output_tokens": 64,
        }
        _, params = responses_capture_request(body)
        assert "input" not in params
        assert "instructions" not in params
        assert params["temperature"] == 1
        assert params["max_output_tokens"] == 64

    def test_never_raises_on_oddball_shapes(self):
        from app.proxy.responses_bridge import responses_capture_request

        # Non-dict body
        assert responses_capture_request("nope") == (None, None)  # type: ignore[arg-type]
        # input of an unsupported non-list type → no messages, params kept
        messages, params = responses_capture_request({"model": "m", "input": 5})
        assert messages == []
        assert params == {"model": "m"}
