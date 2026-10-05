"""OpenAI Responses ↔ Chat Completions translation bridge.

Clients speaking the OpenAI Responses API (``POST /v1/responses`` — the
OpenAI SDK ``client.responses.create``, Codex CLI, and other modern
agents) get full gateway support:

- **Local models**: llama.cpp implements ``/v1/responses`` natively (it
  converts internally to chat completions and emits Responses SSE events),
  so Guardian routes the request through the normal inference pipeline
  (queue admission, model switch, usage tracking, vision preflight) and
  streams the backend's Responses SSE events through unmodified.
- **Cloud models**: cloud providers speak OpenAI chat completions only, so
  Guardian translates both directions — Responses request → chat
  completions request, and the chat response / SSE stream / error back
  into Responses format.  This mirrors the Anthropic bridge (see
  ``app.proxy.anthropic_bridge``) and plugs into the same cloud forwarding
  integration points.

## Supported surface

**Request translation (cloud):**
- ``input``: string or item list (message with ``input_text`` /
  ``input_image`` parts, assistant output messages, ``function_call`` /
  ``function_call_output`` items, ``reasoning`` items)
- ``instructions`` → system message
- function tools (flat Responses shape → nested chat shape); non-function
  tool types (web_search, file_search, computer_use, …) have no chat
  equivalent and are dropped with a warning
- ``tool_choice``: none / auto / required / named
- ``max_output_tokens`` → ``max_tokens``
- ``text.format`` (json_schema) → ``response_format``
- ``temperature``, ``top_p``, ``stream``, ``parallel_tool_calls``,
  ``metadata`` (echoed back on the response object)

**Response translation (cloud):**
- assistant message → ``output`` message item with ``output_text`` parts
- tool_calls → ``function_call`` output items
- usage: ``prompt_tokens``↔``input_tokens``,
  ``completion_tokens``↔``output_tokens``, plus details objects
- ``finish_reason: "length"`` → ``status: "incomplete"`` with
  ``incomplete_details.reason: "max_output_tokens"``

**Streaming translation (cloud):**
- chat chunks → the Responses SSE event flow: ``response.created`` →
  ``response.in_progress`` → ``response.output_item.added`` →
  ``response.content_part.added`` → ``response.output_text.delta`` →
  ``response.output_text.done`` → ``response.content_part.done`` →
  ``response.output_item.done`` → ``response.completed`` (carrying the
  full response object including usage).  A chat stream cut by
  ``finish_reason`` ``length`` / ``content_filter`` / ``refusal`` ends
  with ``response.incomplete`` (status ``incomplete`` +
  ``incomplete_details``) instead.

## Not supported (no chat-completions equivalent)

- ``previous_response_id`` (stateful conversations) — rejected with 400
- ``store``, ``background``, ``include``, ``item_reference``,
  ``truncation``, built-in tools, ``reasoning.encrypted_content``

Stateless usage only: Guardian does not persist responses, so
``GET /v1/responses/{id}`` / ``DELETE`` are backend-decided (llama.cpp
returns 404 for unknown ids).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.proxy.anthropic_bridge import _format_sse_event

logger = logging.getLogger("Guardian.ResponsesBridge")


# ── Detection helpers (used by gateway routing) ──────────────────────


def responses_contains_image_input(input_value: Any) -> bool:
    """Return True when a Responses ``input`` carries ``input_image`` parts.

    The Responses API stores images as content parts of type
    ``input_image`` (with ``image_url``) inside input message items, unlike
    chat completions which use ``image_url`` parts directly.  Used by the
    routing pipeline for vision fallback and mmproj preflight decisions.
    """
    if not isinstance(input_value, list):
        return False
    for item in input_value:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_image":
                return True
    return False


# ── Provider capability detection ─────────────────────────────────────


def provider_needs_responses_translation(provider_name: str, path: str) -> bool:
    """Return True iff the ingress path is ``responses``.

    No cloud provider is trusted to speak the Responses API natively in this
    slice — every ``/v1/responses`` cloud route is translated to chat
    completions.  ``provider_name`` is part of the signature so the gate
    stays a symmetric function to ``provider_needs_anthropic_translation``
    (and a native-Responses provider can later be allowlisted in one place).
    """
    return path == "responses"


# ── Request translation: Responses → OpenAI chat completions ──────────


class ResponsesRequestError(ValueError):
    """Raised for Responses requests that have no chat-completions equivalent.

    Mirrors llama.cpp's behavior of throwing on unsupported Responses
    features (e.g. a non-empty ``previous_response_id``).  The cloud
    forwarding path converts this into a client-actionable 400 error in
    Responses error format — never forwarded upstream.
    """


# Roles of Responses input message items that map 1:1 to chat roles.
_INPUT_ROLES = {"user": "user", "system": "system", "developer": "system"}


def _input_content_parts_to_chat(part: Any) -> dict[str, Any] | None:
    """Convert one Responses input content part to a chat content part.

    ``input_text`` → ``{"type": "text", "text": ...}``;
    ``input_image`` → ``{"type": "image_url", "image_url": {"url": ...}}``.
    ``input_file`` parts raise (no chat mapping); plain strings and
    unparseable parts are best-effort converted to text.
    """
    if isinstance(part, str):
        return {"type": "text", "text": part} if part else None
    if not isinstance(part, dict):
        if part is None:
            return None
        return {"type": "text", "text": str(part)}

    part_type = part.get("type")
    if part_type in ("input_text", "text", "output_text"):
        # ``output_text`` parts appear in round-tripped assistant output
        # items (llama.cpp accepts them in assistant message items too).
        text = part.get("text", "")
        return {"type": "text", "text": text} if text else None
    if part_type == "input_image":
        image_url = part.get("image_url")
        if not image_url:
            return None
        return {"type": "image_url", "image_url": {"url": image_url}}
    if part_type == "input_file":
        raise ResponsesRequestError(
            "Input content part type 'input_file' is not supported for cloud "
            "models through this route. Upload files directly to the provider "
            "or paste their text content into an input_text part."
        )
    if part_type == "refusal":
        # Refusal parts only appear in assistant output items that we produce
        # ourselves; tolerate them in round-tripped inputs.
        text = part.get("refusal", "")
        return {"type": "text", "text": text} if text else None
    raise ResponsesRequestError(
        f"Unsupported input content part type '{part_type}'. Supported "
        "content parts are 'input_text' and 'input_image'."
    )


def _message_content_to_chat(content: Any) -> Any:
    """Convert a Responses message ``content`` value to chat message content.

    A plain string passes through; a list of content parts becomes a chat
    content-part list (empty parts dropped).  Returns ``""`` when nothing
    usable remains.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    chat_parts: list[dict[str, Any]] = []
    for part in content:
        converted = _input_content_parts_to_chat(part)
        if converted is not None:
            chat_parts.append(converted)
    if not chat_parts:
        return ""
    # Text-only part lists collapse to a plain string (widest provider support).
    if all(p.get("type") == "text" for p in chat_parts):
        return "\n".join(p.get("text", "") for p in chat_parts)
    return chat_parts


def _join_text(parts: list[str]) -> str:
    """Join merged text fragments from consecutive items."""
    return "\n".join(t for t in parts if t)


def _convert_input_items(input_value: Any) -> list[dict[str, Any]]:
    """Convert a Responses ``input`` item list to chat ``messages``.

    Mirrors llama.cpp's converter semantics:
    - user/system/developer message items with ``input_text``/``input_image``
      parts → chat messages (developer/system → ``system`` role);
    - assistant message items → assistant messages (string or
      output_text/refusal parts); consecutive assistant items merge;
    - ``function_call`` items → ``tool_calls`` on an (possibly new) assistant
      message, keyed by ``call_id``;
    - ``function_call_output`` items → ``role: "tool"`` messages;
    - ``reasoning`` items → ``reasoning_content`` on the previous assistant
      message when one is present, otherwise on a new assistant message.
    """
    messages: list[dict[str, Any]] = []

    def _last_message() -> dict[str, Any] | None:
        return messages[-1] if messages else None

    for item in input_value:
        if not isinstance(item, dict):
            # Tolerate plain strings inside the item list (EasyInputMessage).
            messages.append({"role": "user", "content": str(item)})
            continue

        item_type = item.get("type")
        if item_type is None or item_type == "message":
            role = item.get("role", "user")
            chat_role = _INPUT_ROLES.get(role)
            if chat_role is None:
                if role != "assistant":
                    raise ResponsesRequestError(
                        f"Unsupported input message role '{role}'. Supported "
                        "roles are 'user', 'assistant', 'system', and 'developer'."
                    )
                chat_role = "assistant"
            text = _message_content_to_chat(item.get("content"))
            if chat_role == "assistant":
                previous = _last_message()
                if previous is not None and previous.get("role") == "assistant":
                    # Consecutive assistant items merge into one message.
                    merged = _join_text([previous.get("content") or "", text])
                    previous["content"] = merged
                else:
                    messages.append({"role": "assistant", "content": text})
            else:
                messages.append({"role": chat_role, "content": text})
        elif item_type == "function_call":
            tool_call = {
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            }
            previous = _last_message()
            if previous is not None and previous.get("role") == "assistant":
                previous.setdefault("tool_calls", []).append(tool_call)
            else:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [tool_call],
                })
        elif item_type == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                output = _join_text([
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in output
                ])
            elif not isinstance(output, str):
                output = "" if output is None else json.dumps(output)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": output,
            })
        elif item_type == "reasoning":
            content = item.get("content")
            reasoning_text = ""
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "reasoning_text":
                        reasoning_text = part.get("text", "")
                        break
            elif isinstance(content, str):
                reasoning_text = content
            if not reasoning_text:
                continue
            previous = _last_message()
            if previous is not None and previous.get("role") == "assistant":
                # Merge into the previous assistant message (llama.cpp parity).
                previous["reasoning_content"] = reasoning_text
            else:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": reasoning_text,
                })
        elif item_type == "item_reference":
            raise ResponsesRequestError(
                "Input item type 'item_reference' is not supported for cloud "
                "models: referenced conversation items cannot be resolved "
                "without stored response state. Send the full item inline instead."
            )
        else:
            raise ResponsesRequestError(
                f"Unsupported input item type '{item_type}' for cloud models: "
                "this item type has no chat-completions equivalent."
            )

    return messages


def responses_capture_request(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Best-effort capture normalization for a ``/v1/responses`` request body.

    Converts the Responses ``input`` to OpenAI-style chat messages for the
    capture record (a plain string input becomes a single user message;
    an item list goes through :func:`_convert_input_items`).  ``params``
    is the request body minus ``input`` and ``instructions``.

    Fail-open by contract: this must NEVER raise.  An unsupported input
    item type (ResponsesRequestError from the bridge converter, raised on
    the cloud path before inference) or any unexpected shape results in
    ``messages=None`` — the capture record skips the messages instead of
    breaking inference.
    """
    params: dict[str, Any] | None = None
    try:
        if not isinstance(body, dict):
            return None, None
        params = {
            k: v for k, v in body.items()
            if k not in ("input", "instructions")
        }
        input_value = body.get("input")
        if isinstance(input_value, str):
            messages = [{"role": "user", "content": input_value}]
        elif isinstance(input_value, list):
            messages = _convert_input_items(input_value)
        else:
            messages = []
        return messages, params
    except Exception:
        return None, params


def _convert_responses_tools_to_openai(
    tools: list[Any],
) -> tuple[list[dict[str, Any]], int]:
    """Convert flat Responses function tools to nested chat tools.

    Returns (chat_tools, dropped_count).  Non-function tool types (web_search,
    file_search, computer_use, custom tools, …) have no chat equivalent and
    are dropped with a warning.  ``strict`` stays OUT of the nested function
    object — cloud providers may reject it; pass through what the client sent.
    """
    chat_tools: list[dict[str, Any]] = []
    dropped = 0
    for tool in tools:
        if not isinstance(tool, dict):
            dropped += 1
            logger.warning(
                "Responses bridge: dropping non-dict tool definition: %r", tool
            )
            continue
        tool_type = tool.get("type", "function")
        if tool_type != "function":
            dropped += 1
            logger.warning(
                "Responses bridge: dropping tool type '%s' — no chat "
                "completions equivalent", tool_type,
            )
            continue
        function: dict[str, Any] = {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object"}),
        }
        if tool.get("description") is None:
            # Avoid sending an explicit null description; keep it minimal.
            function["description"] = ""
        chat_tools.append({"type": "function", "function": function})
    return chat_tools, dropped


def _convert_responses_tool_choice(tool_choice: Any) -> Any:
    """Map a Responses ``tool_choice`` to the chat completions form.

    ``"none"``/``"auto"``/``"required"`` pass through unchanged;
    ``{"type": "function", "name": x}`` →
    ``{"type": "function", "function": {"name": x}}``.
    """
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {
            "type": "function",
            "function": {"name": tool_choice.get("name", "")},
        }
    if tool_choice is None:
        return None
    raise ResponsesRequestError(
        f"Unsupported tool_choice value {tool_choice!r}. Supported values are "
        "'none', 'auto', 'required', and {'type': 'function', 'name': ...}."
    )


def _convert_text_format_to_response_format(text: Any) -> dict[str, Any] | None:
    """Map a Responses ``text.format`` to a chat ``response_format``.

    - ``{"type": "json_schema", "name", "schema", "strict"?}`` →
      ``{"type": "json_schema", "json_schema": {"name", "schema", "strict"?}}``
    - ``{"type": "json_object"}`` → ``{"type": "json_object"}``
    - ``{"type": "text"}`` → None (chat default; nothing to send)
    """
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    fmt_type = fmt.get("type")
    if fmt_type == "text":
        return None
    if fmt_type == "json_object":
        return {"type": "json_object"}
    if fmt_type == "json_schema":
        json_schema: dict[str, Any] = {
            "name": fmt.get("name", "response"),
            "schema": fmt.get("schema", {}),
        }
        if "strict" in fmt:
            json_schema["strict"] = fmt["strict"]
        return {"type": "json_schema", "json_schema": json_schema}
    logger.warning(
        "Responses bridge: dropping unsupported text.format type '%s'", fmt_type
    )
    return None


def translate_responses_request_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI Responses API request body to chat completions form.

    Semantics follow llama.cpp's native ``/v1/responses`` converter (the
    normative reference), with cloud-safety deviations documented inline:

    - ``input`` string or item list → ``messages``
    - ``instructions`` → leading ``system`` message
    - tools: flat Responses shape → nested chat shape (non-function types
      dropped with a warning; ``strict`` NOT injected into nested functions)
    - ``tool_choice`` mapping, ``max_output_tokens`` → ``max_tokens``,
      ``reasoning.effort`` → ``reasoning_effort``
    - ``text.format`` → ``response_format``
    - ``stream: true`` → adds ``stream_options: {"include_usage": true}`` so
      the final chat chunk carries usage for ``response.completed``
    - ``previous_response_id`` (non-empty) raises :class:`ResponsesRequestError`
    - ``store`` / ``background`` / ``include`` / ``truncation`` and other
      unsupported keys are dropped by omission; ``temperature`` / ``top_p`` /
      ``metadata`` / ``user`` / ``safety_identifier`` pass through
    - ``model`` passes through unchanged (already rewritten by the caller)
    """
    if not isinstance(body, dict):
        raise ResponsesRequestError(
            "Request body must be a JSON object for the Responses API."
        )

    previous_response_id = body.get("previous_response_id")
    if previous_response_id:
        raise ResponsesRequestError(
            "previous_response_id is not supported by cloud models routed "
            "through Guardian: responses are not stored, so there is no "
            "state to chain from. Send the full conversation in `input` "
            "(stateless usage) instead."
        )

    chat_body: dict[str, Any] = {}
    chat_body["model"] = body.get("model", "")

    # Instructions → leading system message (llama.cpp parity).
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        if isinstance(instructions, str):
            messages.append({"role": "system", "content": instructions})
        elif isinstance(instructions, list):
            text_parts = []
            for item in instructions:
                if isinstance(item, dict) and item.get("type") == "message":
                    text_parts.append(_message_content_to_chat(item.get("content")))
                elif isinstance(item, str):
                    text_parts.append(item)
            joined = _join_text(text_parts)
            if joined:
                messages.append({"role": "system", "content": joined})
        else:
            logger.warning(
                "Responses bridge: ignoring unsupported instructions type %s",
                type(instructions).__name__,
            )

    input_value = body.get("input")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        messages.extend(_convert_input_items(input_value))
    elif input_value is not None:
        raise ResponsesRequestError(
            "Unsupported `input` type: use a string or a list of input items."
        )
    if not messages:
        raise ResponsesRequestError("`input` must not be empty.")
    chat_body["messages"] = messages

    # Simple parameter passthrough. "service_tier" (OpenRouter/OpenAI service
    # tiers: default/flex/priority/fast/ultrafast) rides along unchanged so
    # tier selection works identically on chat and Responses ingress.
    for key in ("temperature", "top_p", "parallel_tool_calls", "metadata",
                "user", "safety_identifier", "stream", "service_tier"):
        if key in body:
            chat_body[key] = body[key]

    if "max_output_tokens" in body:
        chat_body["max_tokens"] = body["max_output_tokens"]

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat_body["reasoning_effort"] = reasoning["effort"]

    response_format = _convert_text_format_to_response_format(body.get("text"))
    if response_format is not None:
        chat_body["response_format"] = response_format

    tools = body.get("tools")
    if isinstance(tools, list):
        chat_tools, _dropped = _convert_responses_tools_to_openai(tools)
        if chat_tools:
            chat_body["tools"] = chat_tools
    if "tool_choice" in body:
        chat_body["tool_choice"] = _convert_responses_tool_choice(body["tool_choice"])

    # When streaming, request usage in the final chunk so response.completed
    # can carry accurate token counts (same rationale as the Anthropic bridge).
    if chat_body.get("stream"):
        chat_body["stream_options"] = {"include_usage": True}

    return chat_body


# ── Response translation: OpenAI chat completions → Responses ──────────

#: chat ``finish_reason`` → Responses ``incomplete_details.reason``.
#: A length- or content-filter-cut completion is status ``incomplete``
#: (official semantics, verified against LiteLLM's bridge implementation and
#: the create.md reference — emitting ``completed`` breaks OpenAI SDK
#: clients that switch on the status field).
_INCOMPLETE_REASON_BY_FINISH_REASON = {
    "length": "max_output_tokens",
    "content_filter": "content_filter",
    "refusal": "content_filter",
}


def _build_responses_usage(chat_usage: Any) -> dict[str, Any]:
    """Map a chat ``usage`` object to the Responses usage shape."""
    if not isinstance(chat_usage, dict):
        chat_usage = {}
    input_tokens = chat_usage.get("prompt_tokens", chat_usage.get("input_tokens", 0)) or 0
    output_tokens = chat_usage.get("completion_tokens", chat_usage.get("output_tokens", 0)) or 0
    total_tokens = chat_usage.get("total_tokens", input_tokens + output_tokens) or 0
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = chat_usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
        usage["input_tokens_details"] = {"cached_tokens": prompt_details["cached_tokens"]}
    completion_details = chat_usage.get("completion_tokens_details")
    if isinstance(completion_details, dict) and completion_details.get("reasoning_tokens"):
        usage["output_tokens_details"] = {"reasoning_tokens": completion_details["reasoning_tokens"]}
    return usage


def _build_reasoning_item(reasoning: str) -> dict[str, Any]:
    """Build a Responses ``reasoning`` output item (llama.cpp shape)."""
    return {
        "type": "reasoning",
        "id": f"rs_{uuid.uuid4().hex[:24]}",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": reasoning}],
        "status": "completed",
    }


def _build_message_item(text: str, refusal: str | None = None) -> dict[str, Any]:
    """Build a Responses ``message`` output item with content parts."""
    if refusal:
        content: list[dict[str, Any]] = [{"type": "refusal", "refusal": refusal}]
    else:
        content = [{"type": "output_text", "text": text, "annotations": []}]
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": "completed",
        "role": "assistant",
        "content": content,
    }


def _build_function_call_item(tool_call: dict[str, Any]) -> dict[str, Any] | None:
    """Build a Responses ``function_call`` output item from a chat tool_call."""
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function") or {}
    if not isinstance(function, dict):
        function = {}
    return {
        "type": "function_call",
        "call_id": tool_call.get("id", ""),
        "name": function.get("name", ""),
        "arguments": function.get("arguments", ""),
        "status": "completed",
    }


def _build_responses_object(
    chat_payload: dict[str, Any],
    model: str,
    *,
    response_id: str | None = None,
    created_at: int | None = None,
    source_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full Responses object from a chat completions payload.

    Shared by the non-streaming translator and the streaming assembler so
    both emit identical field shapes.

    ID policy: the Responses id is always a fresh ``resp_<uuid4-hex>`` — chat
    ids carry provider-specific prefixes (``chatcmpl-…``) with different
    semantics, and Guardian never persists responses, so a collision-free
    random id is the honest choice.
    """
    choices = chat_payload.get("choices") if isinstance(chat_payload, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}

    created_at = created_at if created_at is not None else int(time.time())
    created_raw = chat_payload.get("created") if isinstance(chat_payload, dict) else None
    try:
        created_at = int(created_raw) if created_raw else created_at
    except (TypeError, ValueError):
        pass

    response_id = response_id or f"resp_{uuid.uuid4().hex}"

    finish_reason = choice.get("finish_reason")
    incomplete_reason = _INCOMPLETE_REASON_BY_FINISH_REASON.get(finish_reason)
    incomplete = incomplete_reason is not None

    output: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        output.append(_build_reasoning_item(str(reasoning)))
    text = message.get("content")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    refusal = message.get("refusal")
    output.append(_build_message_item(text, refusal if isinstance(refusal, str) else None))
    for tool_call in message.get("tool_calls") or []:
        item = _build_function_call_item(tool_call)
        if item is not None:
            output.append(item)

    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "completed_at": created_at,
        "status": "incomplete" if incomplete else "completed",
        "error": None,
        "incomplete_details": (
            {"reason": incomplete_reason} if incomplete else None
        ),
        "model": model,
        "output": output,
        "usage": _build_responses_usage(chat_payload.get("usage")),
    }

    if source_body and isinstance(source_body, dict):
        for key in ("instructions", "temperature", "top_p", "tools", "tool_choice",
                    "parallel_tool_calls", "max_output_tokens", "metadata",
                    "reasoning", "text", "service_tier"):
            if key in source_body:
                response[key] = source_body[key]
    # Served-tier reporting (OpenRouter/OpenAI semantics): the response's
    # service_tier reflects the tier that ACTUALLY served the request, not
    # the one requested — the upstream chat payload reports it.
    served_tier = chat_payload.get("service_tier") if isinstance(chat_payload, dict) else None
    if isinstance(served_tier, str) and served_tier:
        response["service_tier"] = served_tier
    response["previous_response_id"] = None
    response["store"] = False
    return response


def translate_chat_response_to_responses(
    chat_payload: dict[str, Any],
    model: str,
    *,
    source_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a chat completions response payload to a full Responses object.

    ``model`` is the client-visible model name (the gateway suffixes the
    winning provider on failover routes).  ``source_body`` is the original
    Responses request body used to echo request parameters back.
    """
    return _build_responses_object(chat_payload, model, source_body=source_body)


# ── Error translation: chat/completions → Responses ───────────────────


def translate_chat_error_to_responses(status_code: int, payload: Any) -> dict[str, Any]:
    """Normalize an upstream error body into the Responses error envelope.

    Returns ``{"error": {"message", "type", "code"}}``.  The HTTP status
    decision stays at the call site.  Extraction follows
    ``translate_openai_error_to_anthropic`` (OpenAI envelope, ``detail``
    string, and plain-string bodies).
    """
    error_detail: Any = {}
    if isinstance(payload, dict):
        error_detail = payload.get("error", payload)
    elif isinstance(payload, str):
        error_detail = {"message": payload}

    if not isinstance(error_detail, dict):
        error_detail = {"message": str(error_detail)}

    error_type = error_detail.get("type", "")
    if not error_type:
        if status_code == 400:
            error_type = "invalid_request_error"
        elif status_code == 401:
            error_type = "authentication_error"
        elif status_code == 403:
            error_type = "permission_error"
        elif status_code == 404:
            error_type = "not_found_error"
        elif status_code == 429:
            error_type = "rate_limit_error"
        elif status_code == 500:
            error_type = "api_error"
        elif status_code in (503, 529):
            error_type = "overloaded_error"
        else:
            error_type = "api_error"

    message = error_detail.get("message", error_detail.get("detail", "Unknown error"))
    code = error_detail.get("code")

    return {
        "error": {
            "message": message if isinstance(message, str) else json.dumps(message),
            "type": error_type,
            "code": code,
        }
    }


# ── Streaming translation: OpenAI chat SSE → Responses SSE ────────────


def _stream_skeleton_response(
    response_id: str, model: str, created_at: int,
) -> dict[str, Any]:
    """Build the in-progress Response skeleton for created/in_progress events."""
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": model,
        "output": [],
        "error": None,
        "usage": None,
    }


async def translate_openai_stream_to_responses(
    sse_lines: AsyncIterator[str],
    *,
    model: str,
    source_body: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Translate an OpenAI chat-completions SSE stream to Responses events.

    ``source_body`` (the original Responses request body) is echoed onto the
    terminal ``response.completed`` object so streaming and non-streaming
    responses carry the same request-parameter echo.

    Consumes chat SSE lines (``data: {...}``, ``data: [DONE]``, keepalive
    comments pass through) and emits the Responses event flow:

        response.created → response.in_progress →
        response.output_item.added → response.content_part.added →
        response.output_text.delta … → response.output_text.done →
        response.content_part.done → response.output_item.done →
        (per tool_call: output_item.added → function_call_arguments.delta …
        → function_call_arguments.done → output_item.done) →
        response.completed (full assembled Response object incl. usage)

    Reasoning deltas map to ``response.reasoning_text.delta`` on their own
    item.  A mid-stream ``data: {"error": ...}`` line is re-emitted as a
    plain ``data:`` line (llama.cpp parity) and the stream stops.

    Note: no ``sequence_number`` on emitted events — llama.cpp's local
    Responses stream omits it too, and clients accept absent sequence
    numbers, so we keep parity with the local path.
    """
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    skeleton = _stream_skeleton_response(response_id, model, created_at)

    # ── Accumulated stream state ──────────────────────────────────────
    created_emitted = False
    finished = False
    next_output_index = 0

    text_item_id: str | None = None
    text_output_index: int | None = None
    text_parts: list[str] = []

    reasoning_item_id: str | None = None
    reasoning_output_index: int | None = None
    reasoning_text: str = ""

    # OpenAI tool_call index → (item_id, output_index, name, call_id, args)
    tool_items: dict[int, dict[str, Any]] = {}
    tool_order: list[int] = []

    finish_reason: str | None = None
    usage: dict[str, Any] = {}

    def _ensure_created() -> str:
        nonlocal created_emitted
        if created_emitted:
            return ""
        created_emitted = True
        return "".join([
            _format_sse_event("response.created", {
                "type": "response.created", "response": skeleton,
            }),
            _format_sse_event("response.in_progress", {
                "type": "response.in_progress", "response": skeleton,
            }),
        ])

    def _open_text_item() -> str:
        nonlocal text_item_id, text_output_index, next_output_index
        if text_item_id is not None:
            return ""
        text_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        text_output_index = next_output_index
        next_output_index += 1
        return "".join([
            _format_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": text_output_index,
                "item": {
                    "type": "message",
                    "id": text_item_id,
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }),
            _format_sse_event("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }),
        ])

    def _close_text_item() -> str:
        nonlocal text_item_id, text_output_index
        if text_item_id is None:
            return ""
        full_text = "".join(text_parts)
        events = "".join([
            _format_sse_event("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "text": full_text,
            }),
            _format_sse_event("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "part": {
                    "type": "output_text",
                    "text": full_text,
                    "annotations": [],
                },
            }),
            _format_sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": text_output_index,
                "item": {
                    "type": "message",
                    "id": text_item_id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": full_text,
                        "annotations": [],
                    }],
                },
            }),
        ])
        text_item_id = None
        text_output_index = None
        return events

    def _close_reasoning_item() -> str:
        nonlocal reasoning_item_id, reasoning_output_index
        if reasoning_item_id is None:
            return ""
        events = "".join([
            _format_sse_event("response.reasoning_text.done", {
                "type": "response.reasoning_text.done",
                "item_id": reasoning_item_id,
                "output_index": reasoning_output_index,
                "content_index": 0,
                "text": reasoning_text,
            }),
            _format_sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": reasoning_output_index,
                "item": _build_reasoning_item(reasoning_text) | {"id": reasoning_item_id},
            }),
        ])
        reasoning_item_id = None
        reasoning_output_index = None
        return events

    def _finalize() -> str:
        """Close open items and emit the terminal response.completed event."""
        nonlocal finished
        if finished:
            return ""
        finished = True
        events = _close_reasoning_item() + _close_text_item()

        # Function-call items: emit arguments.done + output_item.done.
        for tc_index in tool_order:
            state = tool_items[tc_index]
            events += _format_sse_event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "arguments": state["arguments"],
            })
            events += _format_sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "item": {
                    "type": "function_call",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": state["arguments"],
                    "status": "completed",
                },
            })

        # Assemble the final Response object from the accumulated state via
        # the shared builder (identical field shapes as the non-stream path).
        chat_payload: dict[str, Any] = {
            "created": created_at,
            "choices": [{
                "finish_reason": finish_reason,
                "message": {
                    "content": "".join(text_parts),
                    "tool_calls": [
                        {
                            "id": tool_items[i]["call_id"],
                            "type": "function",
                            "function": {
                                "name": tool_items[i]["name"],
                                "arguments": tool_items[i]["arguments"],
                            },
                        }
                        for i in tool_order
                    ] or None,
                    "reasoning_content": reasoning_text or None,
                },
            }],
            "usage": usage,
        }
        final_response = _build_responses_object(
            chat_payload, model, response_id=response_id, created_at=created_at,
            source_body=source_body,
        )
        # Terminal event: a length/content-filter-cut stream ends with
        # response.incomplete (status "incomplete" + incomplete_details),
        # everything else with response.completed — official semantics.
        terminal_event = (
            "response.incomplete"
            if final_response.get("status") == "incomplete"
            else "response.completed"
        )
        events += _format_sse_event(terminal_event, {
            "type": terminal_event, "response": final_response,
        })
        return events

    async for line in sse_lines:
        if not line:
            continue
        if not line.startswith("data: "):
            # Keepalive comments (": guardian-keepalive…") and other
            # non-data lines pass through unmodified.
            yield line + "\n"
            continue

        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            yield _finalize()
            break

        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(data, dict):
            continue

        # Mid-stream upstream error (llama.cpp parity): re-emit as a plain
        # data line and stop — same wire shape as the local path.
        if "error" in data:
            yield "data: " + json.dumps(data) + "\n\n"
            return

        # Usage may arrive on a final dedicated chunk (empty choices).
        chunk_usage = data.get("usage")
        if isinstance(chunk_usage, dict) and chunk_usage:
            usage = chunk_usage

        choices = data.get("choices") or []
        if not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        chunk_finish = choice.get("finish_reason")
        if chunk_finish:
            finish_reason = chunk_finish

        events = ""

        # ── Reasoning delta ───────────────────────────────────────────
        reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning_delta:
            events += _ensure_created()
            if reasoning_item_id is None:
                reasoning_item_id = f"rs_{uuid.uuid4().hex[:24]}"
                reasoning_output_index = next_output_index
                next_output_index += 1
                events += _format_sse_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": reasoning_output_index,
                    "item": {
                        "type": "reasoning",
                        "id": reasoning_item_id,
                        "summary": [],
                        "content": [],
                        "status": "in_progress",
                    },
                })
            reasoning_text += reasoning_delta
            events += _format_sse_event("response.reasoning_text.delta", {
                "type": "response.reasoning_text.delta",
                "item_id": reasoning_item_id,
                "output_index": reasoning_output_index,
                "content_index": 0,
                "delta": reasoning_delta,
            })

        # ── Text delta ────────────────────────────────────────────────
        text_delta = delta.get("content")
        if text_delta:
            events += _ensure_created()
            # Close reasoning before text starts (item ordering).
            if reasoning_item_id is not None:
                events += _close_reasoning_item()
            if text_item_id is None:
                events += _open_text_item()
            text_parts.append(text_delta)
            events += _format_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "delta": text_delta,
            })

        # ── Tool-call deltas (indexed across chunks) ──────────────────
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            events += _ensure_created()
            # Close text/reasoning before the first function_call item.
            if reasoning_item_id is not None:
                events += _close_reasoning_item()
            if text_item_id is not None:
                events += _close_text_item()
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_index = tc.get("index", 0)
                function = tc.get("function") or {}
                if not isinstance(function, dict):
                    function = {}
                state = tool_items.get(tc_index)
                if state is None:
                    state = {
                        "item_id": f"fc_{uuid.uuid4().hex[:24]}",
                        "output_index": next_output_index,
                        "name": function.get("name", ""),
                        "call_id": tc.get("id", ""),
                        "arguments": "",
                    }
                    next_output_index += 1
                    tool_items[tc_index] = state
                    tool_order.append(tc_index)
                    events += _format_sse_event("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": state["output_index"],
                        "item": {
                            "type": "function_call",
                            "call_id": state["call_id"],
                            "name": state["name"],
                            "arguments": "",
                            "status": "in_progress",
                        },
                    })
                elif function.get("name") and not state["name"]:
                    state["name"] = function["name"]
                if tc.get("id") and not state["call_id"]:
                    state["call_id"] = tc["id"]
                args_fragment = function.get("arguments", "")
                if args_fragment:
                    state["arguments"] += args_fragment
                    events += _format_sse_event(
                        "response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": state["item_id"],
                            "output_index": state["output_index"],
                            "delta": args_fragment,
                        },
                    )

        if events:
            yield events

        # Finish reason arrives on the last content chunk; the final usage
        # chunk (and [DONE]) usually follow, so only close items here —
        # response.completed is deferred until [DONE] or stream end.
        if chunk_finish and not finished:
            events_close = _close_reasoning_item() + _close_text_item()
            if events_close:
                yield events_close

    # Stream ended without [DONE] (upstream cut): still emit a terminal
    # completed event so clients never hang on an unterminated stream.
    closing_events = _finalize()
    if closing_events:
        yield closing_events
