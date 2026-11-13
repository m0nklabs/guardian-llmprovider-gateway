"""Reject malformed input and unguarded media fetching before inference."""
from copy import deepcopy

from fastapi import HTTPException
import pytest

from app.copilot_requests import validate_chat, validate_embedding


@pytest.mark.parametrize("body", [{}, {"messages": None}, {"messages": "text"}, {"messages": []},
    {"messages": [{"role": "invalid", "content": {}}]}, {"messages": [{"role": "user", "content": {}}]},
    {"messages": [{"role": "user"}]}, {"messages": [{"role": "user", "content": [{"type": "text"}]}]},
    {"messages": [{"role": "user", "content": "hello"}], "tools": "invalid"},
    {"messages": [{"role": "user", "content": "hello"}], "tools": [{"type": "function", "function": {"name": "fn", "parameters": []}}]},
    {"messages": [{"role": "user", "content": "hello"}], "max_tokens": -1},
    {"messages": [{"role": "user", "content": "hello"}], "max_completion_tokens": True}])
def test_bad_shapes_rejected(body):
    with pytest.raises(HTTPException) as error:
        validate_chat(body, "chat")
    assert error.value.status_code == 400


@pytest.mark.parametrize("url", ["http://example.test/image.png", "https://public.example/image.png", "file:///etc/passwd", "http://127.0.0.1/image.png"])
def test_messages_does_not_fetch_remote_images(url):
    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    with pytest.raises(HTTPException) as error:
        validate_chat(body, "messages")
    assert error.value.status_code == 400


@pytest.mark.parametrize("key", ["stop", "seed", "presence_penalty", "frequency_penalty", "logprobs"])
def test_responses_does_not_silently_drop_chat_only_options(key):
    with pytest.raises(HTTPException) as error:
        validate_chat({"messages": [{"role": "user", "content": "text"}], key: 1}, "responses")
    assert error.value.status_code == 400


def test_inline_multimodal_and_assistant_tool_results_not_rewritten():
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Please inspect"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}]},
             {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "call_1", "content": "tool reply"}],
            "tools": [{"type": "function", "function": {"name": "probe", "description": "exact café\nDocs",
                    "parameters": {"type": "object", "properties": {}}}}]}
    original = deepcopy(body)
    validate_chat(body, "messages")
    assert body == original


@pytest.mark.parametrize("value", [None, [], True, 3, [-1], [[-1]], [1, "text"], ["text", None]])
def test_bad_embedding_shape(value):
    with pytest.raises(HTTPException) as error:
        validate_embedding({"input": value})
    assert error.value.status_code == 400


@pytest.mark.parametrize("value", ["text", ["a", "b"], [0, 2, 10], [[0, 10], [4, 5]]])
def test_embedding_shapes_preserved(value):
    validate_embedding({"input": value, "dimensions": 4, "encoding_format": "float"})
