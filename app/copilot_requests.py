"""Small pre-inference request checks; never rewrite prompts or tool schemas."""
from __future__ import annotations

from fastapi import HTTPException


_ROLES = {"system", "developer", "user", "assistant", "tool", "function"}


def _invalid(message):
    raise HTTPException(400, message)


def validate_chat(body, protocol):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        _invalid("messages must be a nonempty list")
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str) or message["role"] not in _ROLES:
            _invalid("Each message requires a supported role")
        content = message.get("content")
        refusal = message.get("refusal")
        if refusal is not None and not isinstance(refusal, str):
            _invalid("Message refusal must be text or null")
        if content is None:
            if message["role"] != "assistant" or not (message.get("tool_calls") or message.get("function_call") or refusal):
                _invalid("A message requires content, an assistant tool call or an assistant refusal")
        elif not isinstance(content, (str, list)):
            _invalid("Message content must be text or a list of content parts")
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                    _invalid("Each content part requires a type")
                if part["type"] == "text" and not isinstance(part.get("text"), str):
                    _invalid("Text content parts require text")
                if part["type"] == "image_url":
                    image = part.get("image_url")
                    if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                        _invalid("image_url content requires a URL string")
                    # LiteLLM's messages translation fetches remote image URLs
                    # with a module-global unguarded client. Do not permit that
                    # separate egress path; caller must provide inline image data.
                    if protocol == "messages" and not image["url"].startswith("data:image/"):
                        _invalid("The messages adapter requires inline data:image URLs; remote media fetching is not permitted")
    if protocol == "responses":
        # These Chat-only fields are absent from Responses' request contract;
        # its translator otherwise silently discards them. Explicit null is an
        # omitted option, but an actual requested value must be rejected.
        for key in ("stop", "seed", "presence_penalty", "frequency_penalty", "logprobs"):
            if key in body and body[key] is not None:
                _invalid(f"{key} is not supported by the Responses adapter")
    if "tools" in body:
        tools = body["tools"]
        if not isinstance(tools, list):
            _invalid("tools must be a list")
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                _invalid("The chat adapter supports function tools only")
            function = tool.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
                _invalid("A function tool requires a name")
            if "description" in function and not isinstance(function["description"], str):
                _invalid("A function tool description must be text")
            if "parameters" in function and not isinstance(function["parameters"], dict):
                _invalid("A function tool parameters schema must be an object")
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                _invalid(f"{key} must be a positive integer or null")
    if body.get("stream_options") is not None and not isinstance(body["stream_options"], dict):
        _invalid("stream_options must be an object or null")


def validate_embedding(body):
    value = body.get("input")
    valid = isinstance(value, str) and bool(value)
    if isinstance(value, list) and value:
        valid = all(isinstance(item, str) and bool(item) for item in value)
        valid = valid or all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value)
        valid = valid or all(isinstance(item, list) and item and all(isinstance(token, int) and not isinstance(token, bool) and token >= 0 for token in item) for item in value)
    if not valid:
        _invalid("input must be text, a nonempty text list, token IDs or a list of token-ID lists")
    if "dimensions" in body:
        value = body["dimensions"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _invalid("dimensions must be a positive integer")
    if "encoding_format" in body and body["encoding_format"] not in ("float", "base64"):
        _invalid("encoding_format must be float or base64")
