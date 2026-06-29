"""Anthropic ↔ OpenAI translation bridge for cloud LLM routing.

When a client sends a request to Guardian's ``/v1/messages`` endpoint (the
Anthropic Messages API format) and the target is a cloud provider that only
speaks OpenAI format (e.g. NVIDIA NIM), this module transparently translates:

1. **Request**: Anthropic ``/v1/messages`` → OpenAI ``/v1/chat/completions``
2. **Response (non-streaming)**: OpenAI JSON → Anthropic JSON
3. **Response (streaming)**: OpenAI SSE chunks → Anthropic SSE events

This allows clients that speak Anthropic protocol (Claude Code, the
``anthropic`` Python SDK, etc.) to use NVIDIA NIM and other OpenAI-only
cloud providers without any code changes on their side.

OpenRouter natively supports ``/v1/messages``, so translation is only
applied when the provider does not offer a native Anthropic API.

## Anthropic Messages API vs OpenAI Chat Completions — key differences

| Aspect               | Anthropic /v1/messages          | OpenAI /v1/chat/completions          |
|----------------------|---------------------------------|---------------------------------------|
| System prompt        | Top-level ``system`` field      | A ``{"role":"system"}`` message       |
| Max tokens            | Required ``max_tokens``         | Optional ``max_tokens``               |
| Response format       | ``content`` is a list of blocks  | ``choices[0].message.content`` string |
| Tool calling         | ``tools`` with different schema | ``tools`` with ``function`` wrapper   |
| Streaming events      | ``message_start``, ``content_block_delta``, ``message_delta``, ``message_stop`` | ``chat.completion.chunk`` with deltas |
| Stop reason          | ``stop_reason: "end_turn"``     | ``finish_reason: "stop"``             |
| Usage                | ``input_tokens``, ``output_tokens`` | ``prompt_tokens``, ``completion_tokens`` |
"""

from __future__ import annotations

import json
import time
import uuid
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger("Guardian.AnthropicBridge")


# ── Request translation: Anthropic → OpenAI ───────────────────────────


def translate_anthropic_request_to_openai(anthropic_body: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an Anthropic Messages API request body to OpenAI format.

    Handles:
    - ``system`` field → ``{"role": "system"}`` message
    - ``messages`` passthrough (content already compatible)
    - ``max_tokens`` → ``max_tokens`` (required in Anthropic, optional in OpenAI)
    - ``temperature``, ``top_p`` passthrough
    - ``stop_sequences`` → ``stop``
    - ``stream`` passthrough
    - ``model`` passthrough (already rewritten by caller)
    """
    openai_body: Dict[str, Any] = {}

    # Model name (already rewritten to upstream model by caller)
    openai_body["model"] = anthropic_body.get("model", "")

    # System prompt: Anthropic puts it at top level, OpenAI uses a message
    messages: List[Dict[str, Any]] = []
    system = anthropic_body.get("system")
    if system:
        # system can be a string or a list of content blocks
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # Concatenate text blocks
            text_parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            if text_parts:
                messages.append({"role": "system", "content": "\n".join(text_parts)})

    # Messages: Anthropic and OpenAI both use {"role", "content"} objects,
    # but Anthropic content can be a list of blocks (text, image, tool_use, tool_result)
    for msg in anthropic_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Convert Anthropic content blocks to OpenAI format
            converted = _convert_content_blocks_to_openai(content, role)
            messages.extend(converted)
        else:
            messages.append({"role": role, "content": content or ""})

    openai_body["messages"] = messages

    # Parameters
    if "max_tokens" in anthropic_body:
        openai_body["max_tokens"] = anthropic_body["max_tokens"]
    if "temperature" in anthropic_body:
        openai_body["temperature"] = anthropic_body["temperature"]
    if "top_p" in anthropic_body:
        openai_body["top_p"] = anthropic_body["top_p"]
    if "top_k" in anthropic_body:
        # OpenAI doesn't support top_k; pass through anyway (some providers accept it)
        openai_body["top_k"] = anthropic_body["top_k"]
    if "stop_sequences" in anthropic_body:
        openai_body["stop"] = anthropic_body["stop_sequences"]
    if "stream" in anthropic_body:
        openai_body["stream"] = anthropic_body["stream"]

    # Pass through tools if present (best-effort conversion)
    if "tools" in anthropic_body:
        openai_body["tools"] = _convert_anthropic_tools_to_openai(anthropic_body["tools"])
    if "tool_choice" in anthropic_body:
        openai_body["tool_choice"] = _convert_tool_choice(anthropic_body["tool_choice"])

    return openai_body


def _convert_content_blocks_to_openai(
    blocks: List[Any], role: str
) -> List[Dict[str, Any]]:
    """Convert Anthropic content blocks to OpenAI message format.

    Anthropic content blocks:
    - ``{"type": "text", "text": "..."}``
    - ``{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}``
    - ``{"type": "tool_use", "id": "...", "name": "...", "input": {...}}``
    - ``{"type": "tool_result", "tool_use_id": "...", "content": "..."}``

    OpenAI equivalents:
    - Text: just string content
    - Image: ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``
    - Tool use: ``{"role": "assistant", "tool_calls": [{"id": ..., "type": "function", "function": {"name": ..., "arguments": ...}}]}``
    - Tool result: ``{"role": "tool", "tool_call_id": "...", "content": "..."}``
    """
    # Separate text blocks, image blocks, tool_use, and tool_result
    text_parts: List[str] = []
    image_parts: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    result_messages: List[Dict[str, Any]] = []

    for block in blocks:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue

        block_type = block.get("type", "text")

        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                })
        elif block_type == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })
        elif block_type == "tool_result":
            # Tool results in Anthropic are in assistant/user messages with type tool_result
            # In OpenAI, these become separate "tool" role messages
            tool_content = block.get("content", "")
            if isinstance(tool_content, list):
                # Extract text from content blocks
                parts = []
                for c in block.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif isinstance(c, str):
                        parts.append(c)
                tool_content = "\n".join(parts)
            elif not isinstance(tool_content, str):
                tool_content = json.dumps(tool_content)

            result_messages.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": tool_content,
            })

    # Build the primary message (text + images + tool_calls)
    primary: Dict[str, Any] = {"role": role}

    if image_parts:
        # OpenAI multimodal: content is a list of text + image_url parts
        content_parts = []
        if text_parts:
            content_parts.append({"type": "text", "text": "\n".join(text_parts)})
        content_parts.extend(image_parts)
        primary["content"] = content_parts
    elif text_parts:
        primary["content"] = "\n".join(text_parts)
    else:
        primary["content"] = ""

    if tool_calls:
        primary["tool_calls"] = tool_calls

    # If role is user and we have tool_results, those become separate tool messages
    if result_messages:
        # The primary message might be empty if it was purely tool results
        if primary["content"] or tool_calls:
            result_messages.insert(0, primary)
        return result_messages
    else:
        return [primary]


def _convert_anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function format."""
    openai_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # Anthropic: {"name": "...", "description": "...", "input_schema": {...}}
        # OpenAI: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
        if "name" in tool:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", tool.get("parameters", {"type": "object"})),
                },
            })
        elif "function" in tool:
            # Already in OpenAI format
            openai_tools.append(tool)
    return openai_tools


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Convert Anthropic tool_choice to OpenAI format."""
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto"
        if tool_choice == "any":
            return "required"
        if tool_choice == "none":
            return "none"
    elif isinstance(tool_choice, dict):
        if "name" in tool_choice:
            return {"type": "function", "function": {"name": tool_choice["name"]}}
    return tool_choice


# ── Response translation: OpenAI → Anthropic ─────────────────────────


def translate_openai_response_to_anthropic(
    openai_response: Dict[str, Any],
    model_name: str,
) -> Dict[str, Any]:
    """Convert an OpenAI chat completion response to Anthropic Messages format.

    Handles:
    - ``choices[0].message.content`` → ``content`` list of text blocks
    - ``finish_reason`` → ``stop_reason``
    - ``usage`` token fields
    - ``tool_calls`` → ``content`` tool_use blocks
    """
    choices = openai_response.get("choices", [])
    choice = choices[0] if choices else {}
    message = choice.get("message", {})

    # Build content blocks
    content_blocks: List[Dict[str, Any]] = []

    # Text content
    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Tool calls
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function", {})
        try:
            tool_input = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", str(uuid.uuid4())),
            "name": func.get("name", ""),
            "input": tool_input,
        })

    # Stop reason mapping
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    # Usage
    usage = openai_response.get("usage", {})
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))

    response = {
        "id": openai_response.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content_blocks if content_blocks else [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    return response


# ── Streaming translation: OpenAI SSE → Anthropic SSE ─────────────────


async def translate_openai_stream_to_anthropic(
    openai_sse_lines: AsyncIterator[str],
    model_name: str,
    *,
    request_id: str = "",
) -> AsyncIterator[str]:
    """Translate an OpenAI streaming SSE response to Anthropic SSE events.

    Supports both text deltas and tool_call deltas.

    Anthropic streaming events emitted:
    1. ``message_start`` — initial message metadata
    2. ``content_block_start`` — start of a content block (text or tool_use)
    3. ``content_block_delta`` — incremental deltas:
       - ``text_delta`` for text content
       - ``input_json_delta`` for tool call arguments
    4. ``content_block_stop`` — end of content block
    5. ``message_delta`` — stop_reason and final usage
    6. ``message_stop`` — end of message

    OpenAI streaming chunks contain ``choices[0].delta`` with either:
    - ``content``: incremental text
    - ``tool_calls``: list of ``{index, id, type, function: {name, arguments}}``
      where the first chunk has id+name, subsequent chunks append to arguments.
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    if request_id:
        message_id = f"msg_{request_id[:24]}" if len(request_id) >= 24 else message_id

    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"

    # Track open content blocks.  We support interleaved text + tool_use blocks.
    # block_index 0 = text (if any), then tool_use blocks at indices 1, 2, ...
    # OpenAI sends text deltas and tool_calls deltas separately, often as:
    #   chunk 1: delta.content = "Let me check..."
    #   chunk 2: delta.tool_calls[0] = {id, function.name, arguments=""}
    #   chunk 3: delta.tool_calls[0].function.arguments += '{"city"'
    #   chunk 4: delta.tool_calls[0].function.arguments += ':"Paris"}'
    #   chunk 5: finish_reason = "tool_calls"
    #
    # We need to:
    # - Start a text block for content, close it when tool_calls appear.
    # - Start a tool_use block per tool_calls[index], stream arguments as input_json_delta.
    # - Close all blocks at the end.

    # State: which block indices are currently open, and what type
    open_blocks: Dict[int, str] = {}  # {index: "text" | "tool_use"}
    next_block_index = 0
    # Map OpenAI tool_call.index → our content block index
    tool_call_index_map: Dict[int, int] = {}

    def close_block(idx: int):
        if idx in open_blocks:
            open_blocks.pop(idx)

    # Emit message_start
    yield _format_sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model_name,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    async for line in openai_sse_lines:
        if not line or not line.startswith("data: "):
            continue

        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break

        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue

        # Extract usage if present
        usage = data.get("usage")
        if isinstance(usage, dict):
            input_tokens = max(input_tokens, usage.get("prompt_tokens", usage.get("input_tokens", 0)))
            output_tokens = max(output_tokens, usage.get("completion_tokens", usage.get("output_tokens", 0)))

        choices = data.get("choices", [])
        if not choices:
            continue

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        if not isinstance(delta, dict):
            delta = {}

        # ── Text content delta ────────────────────────────────────────
        text_delta = delta.get("content")
        if text_delta:
            text_block_idx = 0  # text is always block 0
            if text_block_idx not in open_blocks:
                yield _format_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_idx,
                    "content_block": {"type": "text", "text": ""},
                })
                open_blocks[text_block_idx] = "text"
                next_block_index = max(next_block_index, 1)

            yield _format_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_idx,
                "delta": {"type": "text_delta", "text": text_delta},
            })

        # ── Tool call deltas ─────────────────────────────────────────
        # OpenAI streams tool calls across multiple chunks. The first chunk
        # for a given tool_calls[i].index contains id and function.name.
        # Subsequent chunks contain function.arguments fragments.
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            # Close any open text block before emitting tool_use blocks
            if 0 in open_blocks and open_blocks[0] == "text":
                yield _format_sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": 0,
                })
                close_block(0)

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue

                tc_index = tc.get("index", 0)
                func = tc.get("function", {})
                if not isinstance(func, dict):
                    func = {}

                # Is this the first chunk for this tool call?
                if tc_index not in tool_call_index_map:
                    # Assign a new content block index
                    block_idx = next_block_index
                    tool_call_index_map[tc_index] = block_idx
                    next_block_index += 1

                    tool_id = tc.get("id", f"call_{uuid.uuid4().hex[:24]}")
                    tool_name = func.get("name", "")

                    # Emit content_block_start for tool_use
                    yield _format_sse_event("content_block_start", {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": {},
                        },
                    })
                    open_blocks[block_idx] = "tool_use"

                    # If the first chunk already has arguments, emit them
                    args_fragment = func.get("arguments", "")
                    if args_fragment:
                        yield _format_sse_event("content_block_delta", {
                            "type": "content_block_delta",
                            "index": block_idx,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": args_fragment,
                            },
                        })
                else:
                    # Subsequent chunk — stream arguments as input_json_delta
                    block_idx = tool_call_index_map[tc_index]
                    args_fragment = func.get("arguments", "")
                    if args_fragment:
                        yield _format_sse_event("content_block_delta", {
                            "type": "content_block_delta",
                            "index": block_idx,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": args_fragment,
                            },
                        })

        # ── Finish reason ─────────────────────────────────────────────
        if finish_reason:
            stop_reason_map = {
                "stop": "end_turn",
                "length": "max_tokens",
                "tool_calls": "tool_use",
                "function_call": "tool_use",
                "content_filter": "end_turn",
            }
            stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    # ── Close all open content blocks ──────────────────────────────────
    for idx in sorted(open_blocks.keys()):
        yield _format_sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": idx,
        })

    # ── Emit message_delta with stop_reason and usage ──────────────────
    yield _format_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })

    # ── Emit message_stop ──────────────────────────────────────────────
    yield _format_sse_event("message_stop", {
        "type": "message_stop",
    })


def _format_sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format a single SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ── Provider capability detection ─────────────────────────────────────


# Providers that natively support the Anthropic Messages API (/v1/messages)
# and do NOT need translation.
_PROVIDERS_WITH_NATIVE_ANTHROPIC = {"openrouter"}


def provider_needs_anthropic_translation(provider_name: str, path: str) -> bool:
    """Return True if the request path is ``messages`` and the provider
    does not natively support the Anthropic Messages API.

    OpenRouter supports ``/v1/messages`` natively, so no translation is needed.
    NVIDIA NIM and other OpenAI-only providers need translation.
    """
    if path != "messages":
        return False
    return provider_name not in _PROVIDERS_WITH_NATIVE_ANTHROPIC
