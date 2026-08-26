"""Stream response assembler — accumulates SSE deltas into a final semantic response.

Per the capture contract, streaming responses are accumulated in memory and
emitted as a single completed response event instead of persisting individual
chunks.  This ensures that streaming and non-streaming requests produce
equivalent semantic records.

The assembler supports two wire formats:
- **OpenAI chat completions** SSE: ``data: {"choices":[{"delta":{...}}]}``
- **Anthropic messages** SSE: ``event: content_block_delta\\ndata: {"delta":{"...":...}}``
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Guardian.Capture.StreamAssembler")


class StreamResponseAssembler:
    """Accumulate streaming SSE deltas into a single final response.

    This is instantiated per-request (at most once per request lifecycle).
    Each SSE line is fed to :meth:`add_sse_line`, and :meth:`assemble` returns
    the final semantic response text and metadata.
    """

    def __init__(self) -> None:
        self._content_parts: List[str] = []
        self._reasoning_parts: List[str] = []
        self._tool_calls: List[Dict[str, Any]] = []
        self._finish_reason: Optional[str] = None
        self._prompt_tokens: Optional[int] = None
        self._completion_tokens: Optional[int] = None
        self._has_content: bool = False
        self._line_count: int = 0
        self._error: Optional[str] = None

    def add_sse_line(self, line: str) -> None:
        """Process one SSE line from the upstream stream.

        Handles both OpenAI chat-completion deltas and Anthropic message deltas.
        """
        if not isinstance(line, str) or not line.strip():
            return

        self._line_count += 1

        # Anthropic SSE may have an event-type prefix line:
        #   event: content_block_delta
        #   data: {...}
        # We handle the data lines.
        if not line.startswith("data: "):
            # Could be an Anthropic event-type line or just a blank separator
            return

        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            return

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return

        if not isinstance(data, dict):
            return

        # ── OpenAI chat/completions delta ──────────────────────────────
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    # Text content
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        self._content_parts.append(content)
                        self._has_content = True
                    # Reasoning content — OpenAI sends `reasoning_content`,
                    # OpenRouter proxies `reasoning` (same text; never both).
                    reasoning = delta.get("reasoning_content")
                    if not isinstance(reasoning, str) or not reasoning:
                        reasoning = delta.get("reasoning")
                    if isinstance(reasoning, str) and reasoning:
                        self._reasoning_parts.append(reasoning)
                    # Tool calls
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        self._merge_tool_calls(tool_calls)
                # Finish reason
                finish_reason = choice.get("finish_reason")
                if isinstance(finish_reason, str) and finish_reason:
                    self._finish_reason = finish_reason

                # Non-delta message (final chunk in some providers)
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content and not self._has_content:
                        self._content_parts.append(content)
                        self._has_content = True
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        self._extract_usage(usage)

        # ── Anthropic content_block_delta ─────────────────────────────
        if data.get("type") == "content_block_delta":
            delta = data.get("delta", {})
            if isinstance(delta, dict):
                text = delta.get("text")
                if isinstance(text, str) and text:
                    self._content_parts.append(text)
                    self._has_content = True
                # Input/JSON tool-call deltas use 'partial_json'
                partial_json = delta.get("partial_json")
                if isinstance(partial_json, str) and partial_json:
                    self._content_parts.append(partial_json)
                    self._has_content = True

        # ── Anthropic message_delta ────────────────────────────────────
        if data.get("type") == "message_delta":
            delta = data.get("delta", {})
            if isinstance(delta, dict):
                text = delta.get("text")
                if isinstance(text, str) and text:
                    self._content_parts.append(text)
                    self._has_content = True
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    self._finish_reason = stop_reason
            usage = data.get("usage", {})
            if isinstance(usage, dict):
                self._extract_usage(usage)

        if data.get("type") == "message_start":
            usage = data.get("message", {}).get("usage", {})
            if isinstance(usage, dict):
                self._extract_usage(usage)

        # ── Direct usage field (some providers send usage in the final chunk) ──
        usage = data.get("usage")
        if isinstance(usage, dict):
            self._extract_usage(usage)

    def _merge_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        """Merge incremental tool call deltas into the accumulated list."""
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            index = tc.get("index", 0)
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = 0

            # Extend the tool_calls list to accommodate new indices
            while len(self._tool_calls) <= index:
                self._tool_calls.append({})

            existing = self._tool_calls[index]
            if "id" not in existing and "id" in tc:
                existing["id"] = tc["id"]
            if "type" not in existing and "type" in tc:
                existing["type"] = tc["type"]
            if "function" in tc and isinstance(tc["function"], dict):
                if "function" not in existing:
                    existing["function"] = {}
                fn_delta = tc["function"]
                if "name" in fn_delta and "name" not in existing["function"]:
                    existing["function"]["name"] = fn_delta["name"]
                if "arguments" in fn_delta and isinstance(fn_delta["arguments"], str):
                    existing_fn = existing["function"]
                    existing_fn["arguments"] = existing_fn.get("arguments", "") + fn_delta["arguments"]

    def _extract_usage(self, usage: Dict[str, Any]) -> None:
        """Extract token usage from OpenAI or Anthropic usage objects."""
        pt = usage.get("prompt_tokens") or usage.get("input_tokens")
        ct = usage.get("completion_tokens") or usage.get("output_tokens")
        if isinstance(pt, (int, float)):
            if self._prompt_tokens is None or int(pt) > self._prompt_tokens:
                self._prompt_tokens = int(pt)
        if isinstance(ct, (int, float)):
            if self._completion_tokens is None or int(ct) > self._completion_tokens:
                self._completion_tokens = int(ct)

    @property
    def content(self) -> str:
        """The assembled response content text."""
        return "".join(self._content_parts)

    @property
    def reasoning_content(self) -> Optional[str]:
        """The assembled reasoning content text (or None if no reasoning)."""
        parts = self._reasoning_parts
        return "".join(parts) if parts else None

    @property
    def tool_calls(self) -> Optional[List[Dict[str, Any]]]:
        """The assembled tool calls (or None if none)."""
        return self._tool_calls if self._tool_calls else None

    @property
    def finish_reason(self) -> Optional[str]:
        return self._finish_reason

    @property
    def prompt_tokens(self) -> Optional[int]:
        return self._prompt_tokens

    @property
    def completion_tokens(self) -> Optional[int]:
        return self._completion_tokens

    @property
    def has_content(self) -> bool:
        return self._has_content

    @property
    def is_empty(self) -> bool:
        """True when no content was accumulated at all."""
        return not self._has_content and not self._reasoning_parts and not self._tool_calls

    def get_usage(self) -> Tuple[Optional[int], Optional[int]]:
        """Return (prompt_tokens, completion_tokens)."""
        return self._prompt_tokens, self._completion_tokens

    def assemble(self) -> Dict[str, Any]:
        """Return the final assembled response as a semantic dict.

        This is the single source of truth for the captured response content.
        """
        result: Dict[str, Any] = {
            "content": self.content,
            "finish_reason": self._finish_reason,
            "tool_calls": self._tool_calls if self._tool_calls else None,
            "reasoning_content": "".join(self._reasoning_parts) if self._reasoning_parts else None,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "incomplete": self._finish_reason is None or self._finish_reason == "null",
        }
        return result
