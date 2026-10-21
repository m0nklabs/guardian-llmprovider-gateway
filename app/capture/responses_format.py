"""Shared Responses-shape helpers for capture.

The OpenAI Responses API (``/v1/responses``, implemented natively by
llama.cpp) carries its semantics in a different wire shape than chat
completions: the response object has an ``output`` list of typed items
(message / function_call / reasoning) instead of ``choices``, and usage
uses ``input_tokens``/``output_tokens``.  These helpers normalize that
shape into the capture-semantic fields used by the non-stream dispatcher
(``app.gateway.capture_dispatch``) and the stream assembler
(``app.capture.stream_assembler``).

All functions here are pure and defensive: they never raise on odd
payload shapes (missing keys, non-dict items are tolerated), because a
conversion error inside capture must result in a capture skip — never an
exception escaping into the inference path.
"""

from __future__ import annotations

from typing import Any

#: Responses ``incomplete_details.reason`` → chat ``finish_reason``.
RESPONSES_INCOMPLETE_REASON_TO_FINISH = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


def responses_status_to_finish_reason(
    status: str | None,
    incomplete_details: Any,
) -> str | None:
    """Map a Responses object ``status`` (+ ``incomplete_details``) to a
    capture ``finish_reason``.

    - ``"completed"`` → ``"stop"``;
    - ``"incomplete"`` → the mapped reason (unknown reason → ``None``);
    - ``failed``/``queued``/``in_progress``/``cancelled``/missing → ``None``.
    """
    if not isinstance(status, str):
        return None
    if status == "completed":
        return "stop"
    if status == "incomplete":
        reason = None
        if isinstance(incomplete_details, dict):
            reason = incomplete_details.get("reason")
        if isinstance(reason, str) and reason:
            return RESPONSES_INCOMPLETE_REASON_TO_FINISH.get(reason)
        return None
    return None


def _responses_message_parts(item: dict[str, Any]) -> list[str]:
    """Collect text (and refusal) strings from a Responses message item."""
    parts: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif part_type == "refusal":
                refusal = part.get("refusal")
                if isinstance(refusal, str) and refusal:
                    parts.append(refusal)
    elif isinstance(content, str) and content:
        parts.append(content)
    return parts


def _responses_reasoning_parts(item: dict[str, Any]) -> list[str]:
    """Collect reasoning_text strings from a Responses reasoning item."""
    parts: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "reasoning_text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return parts


def extract_responses_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract capture-semantic fields from a non-stream Responses object.

    Returns a dict with keys ``response_content`` (joined output_text and
    refusal text, ``None`` when empty), ``tool_calls`` (chat-shaped
    function-call dicts, ``None`` when none), ``reasoning_content``
    (joined reasoning_text, ``None`` when empty), ``finish_reason`` (via
    :func:`responses_status_to_finish_reason`) and ``usage`` (the payload
    usage dict as-is, ``None`` when absent).
    """
    result: dict[str, Any] = {
        "response_content": None,
        "tool_calls": None,
        "reasoning_content": None,
        "finish_reason": None,
        "usage": None,
    }
    if not isinstance(payload, dict):
        return result

    output = payload.get("output")
    if isinstance(output, list):
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                text_parts.extend(_responses_message_parts(item))
            elif item_type == "reasoning":
                reasoning_parts.extend(_responses_reasoning_parts(item))
            elif item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                tool_calls.append({
                    "id": call_id if isinstance(call_id, str) else "",
                    "type": "function",
                    "function": {
                        "name": name if isinstance(name, str) else "",
                        "arguments": arguments if isinstance(arguments, str) else "",
                    },
                })

        if text_parts:
            result["response_content"] = "\n".join(text_parts)
        if reasoning_parts:
            result["reasoning_content"] = "\n".join(reasoning_parts)
        if tool_calls:
            result["tool_calls"] = tool_calls

    result["finish_reason"] = responses_status_to_finish_reason(
        payload.get("status"),
        payload.get("incomplete_details"),
    )
    usage = payload.get("usage")
    if isinstance(usage, dict):
        result["usage"] = usage
    return result
