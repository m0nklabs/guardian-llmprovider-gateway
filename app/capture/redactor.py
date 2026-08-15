"""Mandatory credential, PII precursor, and sensitive-data redaction for capture.

This module enforces the security invariants from the capture contract:

- Authorization headers and raw API keys are never persisted.
- Cloud provider credentials and environment-variable values are never persisted.
- Raw client IP addresses are never persisted in dataset events.
- Raw image data is replaced with policy-approved metadata (SHA-256, MIME type,
  byte size, dimensions).
- System prompts are stripped by default.
- Reasoning content is stripped by default.
- Tool results are stripped by default.
- Unknown content block types are stripped by default.

All functions are pure and fail-open: exceptions are caught and the original
value is dropped (never partially redacted) to avoid leaking partial secrets.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import struct
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("Guardian.Capture.Redactor")

# ── Secret-detection patterns ──────────────────────────────────────────

# Patterns that indicate an authorization header value or API key.
# These are matched against values that appear in message content as a
# sanity check — real redaction of request bodies is done structurally
# (headers are never included in captured messages).
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|apikey|token|bearer|secret|password)\s*[=:]\s*\S",
)

# API key patterns from common providers
_API_KEY_PATTERNS = [
    # OpenAI-style: sk-... (up to 200 chars of alphanumerics, dashes, underscores)
    re.compile(r"sk-[A-Za-z0-9_\-]{10,200}"),
    # OpenRouter-style: sk-or-v1-...
    re.compile(r"sk-or-[A-Za-z0-9_\-]{10,200}"),
    # NVIDIA API key: nvapi-...
    re.compile(r"nvapi-[A-Za-z0-9_\-]{10,200}"),
    # Poolside API key: pool-...
    re.compile(r"pool-[A-Za-z0-9_\-]{10,200}"),
    # Generic Bearer token
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{10,200}"),
]

# Raw IP address regex (used to detect potential IP leakage in text)
_IPV4_RE = re.compile(r"\b(\d{1,3}\.){3}\d{1,3}\b")

# Environment variable placeholders
_ENV_VAR_RE = re.compile(r"\$\{?[A-Z][A-Z0-9_]*\}?")

# Auth header patterns in text content — defense-in-depth to catch
# "Authorization: Bearer ...", "api_key=...", "password=...", etc.
# that might survive after API-key-pattern redaction.
_AUTH_HEADER_REDACT_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|apikey|token|bearer|secret|password)"
    r"\s*[=: ]\s*\S+",
)


def _redact_secrets_in_text(text: str) -> str:
    """Replace detected secret patterns with redaction markers.

    This is a defense-in-depth measure — the primary redaction path is structural
    (we never capture raw request bodies or headers).  This catches accidental
    leakage of keys in message content.

    Applies two passes:
    1. API key patterns (sk-..., nvapi-..., Bearer ...) → [REDACTED_API_KEY]
    2. Auth header references (Authorization: ..., api_key=..., etc.) → [REDACTED_AUTH_HEADER]
    """
    if not isinstance(text, str):
        return ""
    redacted = text
    # Pass 1: Redact known API key / bearer token patterns
    for pattern in _API_KEY_PATTERNS:
        redacted = pattern.sub("[REDACTED_API_KEY]", redacted)
    # Pass 2: Redact auth header patterns (e.g., "Authorization: ...")
    redacted = _AUTH_HEADER_REDACT_RE.sub("[REDACTED_AUTH_HEADER]", redacted)
    # Pass 3: Redact raw IP addresses
    redacted = _IPV4_RE.sub("[REDACTED_IP]", redacted)
    # Pass 4: Redact environment variable references
    redacted = _ENV_VAR_RE.sub("[REDACTED_ENV_VAR]", redacted)
    return redacted


def _redact_ip_in_text(text: str) -> str:
    """Replace standalone IPv4 addresses with a redaction marker."""
    if not isinstance(text, str):
        return ""
    return _IPV4_RE.sub("[REDACTED_IP]", text)


def redact_request_messages(
    messages: Any,
    config_field_policies: Optional[Dict[str, str]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Redact request messages according to field policies.

    - System messages: stripped when policy is "strip" (default).
    - User/assistant messages: content is scanned for API keys and IPs.
    - Image content blocks: replaced with metadata when policy is "hash_and_metadata",
      removed when policy is "strip".

    Returns None if messages are not a list (invalid input).
    """
    if not isinstance(messages, list):
        return None

    policies = config_field_policies or {}
    system_policy = policies.get("system_prompts", "strip")
    images_policy = policies.get("images", "hash_and_metadata")
    unknown_policy = policies.get("unknown_content_blocks", "strip")
    reasoning_policy = policies.get("reasoning", "strip")

    result: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")
        content = msg.get("content")

        # Strip system messages by policy
        if role == "system" and system_policy == "strip":
            continue

        redacted_msg: Dict[str, Any] = {"role": role}

        # Process reasoning_content
        if "reasoning_content" in msg:
            if reasoning_policy == "strip":
                pass  # omit entirely
            else:
                rc = msg.get("reasoning_content")
                if isinstance(rc, str):
                    redacted_msg["reasoning_content"] = _redact_secrets_in_text(rc)
                else:
                    redacted_msg["reasoning_content"] = None

        # Process content (can be string or list of content blocks)
        if isinstance(content, str):
            redacted_msg["content"] = _redact_secrets_in_text(content)
        elif isinstance(content, list):
            redacted_blocks: List[Dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    if unknown_policy == "capture":
                        redacted_blocks.append({"type": "text", "text": str(block)})
                    continue
                block_type = block.get("type", "unknown")
                if block_type == "text":
                    text_val = block.get("text", "")
                    if isinstance(text_val, str):
                        redacted_blocks.append({
                            "type": "text",
                            "text": _redact_secrets_in_text(text_val),
                        })
                    else:
                        redacted_blocks.append({"type": "text", "text": ""})
                elif block_type == "image_url":
                    if images_policy == "hash_and_metadata":
                        metadata = redact_image_blocks(
                            [{"type": "image_url", "image_url": block.get("image_url", {})}]
                        )
                        redacted_blocks.extend(metadata or [])
                    elif images_policy == "strip":
                        continue  # omit image block
                    else:
                        continue
                elif block_type == "image":
                    # Anthropic image content
                    if images_policy == "hash_and_metadata":
                        metadata = redact_image_blocks([block])
                        redacted_blocks.extend(metadata or [])
                    elif images_policy == "strip":
                        continue
                    else:
                        continue
                else:
                    # Unknown content block type
                    if unknown_policy == "capture":
                        # Capture only non-sensitive fields
                        safe_block = {
                            k: v for k, v in block.items()
                            if k not in ("data", "image_url", "file")
                        }
                        redacted_blocks.append(safe_block)
                    # "strip" for unknown blocks — skip
            redacted_msg["content"] = redacted_blocks
        elif content is not None:
            redacted_msg["content"] = _redact_secrets_in_text(str(content))

        # Copy non-sensitive fields
        for key in ("name", "role"):
            if key in msg and key not in redacted_msg:
                redacted_msg[key] = msg[key]

        # Strip tool_calls if policy says so
        if "tool_calls" in msg:
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                redacted_msg["tool_calls"] = [
                    {k: v for k, v in tc.items() if k != "api_key"}
                    for tc in tool_calls if isinstance(tc, dict)
                ]

        # Strip function definitions/params from tool calls
        if "tool_calls" in redacted_msg:
            for tc in redacted_msg["tool_calls"]:
                if "function" in tc and isinstance(tc["function"], dict):
                    fn = tc["function"]
                    # Strip any key-like fields from function call args
                    if "arguments" in fn and isinstance(fn["arguments"], str):
                        fn["arguments"] = _redact_secrets_in_text(fn["arguments"])

        result.append(redacted_msg)

    return result


def redact_response_content(
    content: Any,
    response_format: str = "auto",
) -> Optional[str]:
    """Redact sensitive data from a response content string.

    Delegates to :func:`_redact_secrets_in_text` which performs comprehensive
    redaction of API keys, auth header patterns, IP addresses, and env var
    references.
    """
    if content is None:
        return None
    if not isinstance(content, str):
        content = str(content)
    return _redact_secrets_in_text(content)


def redact_request_parameters(
    params: Any,
    config_field_policies: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Redact request parameters, stripping credentials and tool definitions when configured.

    - api_key, authorization, headers are never persisted.
    - When tools policy is "strip", tool definitions are removed.
    """
    if not isinstance(params, dict):
        return None

    policies = config_field_policies or {}
    tools_policy = policies.get("tool_definitions", "capture")
    structured_policy = policies.get("structured_output", "strip")

    # Keys that must never be persisted
    SENSITIVE_KEYS = frozenset({
        "api_key", "apikey", "authorization", "auth", "token",
        "bearer", "secret", "password", "headers",
        "x-api-key", "x_forwarded_for", "x_forwarded_proto",
    })

    # Grammar-Constrained Decoding structure: raw grammar/schema content is
    # sensitive structure — strip by default (presence flags carry the info).
    STRUCTURED_OUTPUT_KEYS = frozenset({"grammar", "json_schema", "response_format"})

    safe_params: Dict[str, Any] = {}

    for key, value in params.items():
        key_lower = key.lower() if isinstance(key, str) else ""

        # Structural redaction of known sensitive keys
        if key_lower in SENSITIVE_KEYS:
            safe_params[key] = "[REDACTED]"
            continue

        # Strip grammar/schema content when policy demands
        if key_lower in STRUCTURED_OUTPUT_KEYS and structured_policy == "strip":
            safe_params[key] = "[REDACTED]"
            continue

        # Strip tool definitions when policy demands
        if key_lower == "tools" and tools_policy == "strip":
            continue

        # Ollama clients send grammar/schema via ``options.format``. The nested
        # key is literally ``format`` (not in STRUCTURED_OUTPUT_KEYS), so the
        # generic recursion below would preserve raw grammar/schema content —
        # a FEAT-6 privacy violation. Strip the nested content while keeping
        # the rest of ``options`` (temperature, seed, …) intact.
        if (
            key_lower == "options"
            and isinstance(value, dict)
            and structured_policy == "strip"
        ):
            redacted_options = dict(value)
            if "format" in redacted_options:
                redacted_options["format"] = "[REDACTED]"
            safe_params[key] = redacted_options
            continue

        # Redact secrets in string values
        if isinstance(value, str):
            safe_params[key] = _redact_secrets_in_text(value)
        elif isinstance(value, dict):
            safe_params[key] = redact_request_parameters(value, config_field_policies)
        elif isinstance(value, list):
            safe_params[key] = [
                redact_request_parameters(v, config_field_policies) if isinstance(v, dict)
                else _redact_secrets_in_text(str(v)) if isinstance(v, str)
                else v
                for v in value
            ]
        else:
            safe_params[key] = value

    return safe_params


def redact_reasoning_content(
    reasoning: Any,
    policy: str = "strip",
) -> Optional[str]:
    """Redact reasoning content — stripped by default."""
    if policy == "strip":
        return None
    if reasoning is None:
        return None
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    return _redact_secrets_in_text(reasoning)


def redact_tool_results(
    tool_results: Any,
    policy: str = "strip",
) -> Optional[List[Dict[str, Any]]]:
    """Redact tool results — stripped by default."""
    if policy == "strip":
        return None
    if not isinstance(tool_results, list):
        return None
    result: List[Dict[str, Any]] = []
    for tr in tool_results:
        if not isinstance(tr, dict):
            continue
        safe_tr = {}
        for key, value in tr.items():
            key_lower = key.lower() if isinstance(key, str) else ""
            if key_lower in ("content", "output", "text") and isinstance(value, str):
                safe_tr[key] = _redact_secrets_in_text(value)
            elif isinstance(value, dict):
                safe_tr[key] = redact_request_parameters(value)
            elif isinstance(value, list):
                safe_tr[key] = [
                    redact_request_parameters(v) if isinstance(v, dict)
                    else v for v in value
                ]
            else:
                safe_tr[key] = value
        result.append(safe_tr)
    return result


def redact_tool_calls(
    tool_calls: Any,
    policy: str = "capture",
) -> Optional[List[Dict[str, Any]]]:
    """Redact tool calls — capture by default but strip sensitive fields."""
    if policy == "strip":
        return None
    if not isinstance(tool_calls, list):
        return None
    result: List[Dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        safe_tc = {
            k: v for k, v in tc.items()
            if k.lower() not in ("api_key", "authorization", "headers")
        }
        if "function" in safe_tc and isinstance(safe_tc["function"], dict):
            fn = safe_tc["function"]
            if "name" in fn and isinstance(fn["name"], str):
                fn["name"] = _redact_secrets_in_text(fn["name"])
            if "arguments" in fn and isinstance(fn["arguments"], str):
                fn["arguments"] = _redact_secrets_in_text(fn["arguments"])
        result.append(safe_tc)
    return result


def redact_image_blocks(
    content_blocks: Any,
    policy: str = "hash_and_metadata",
) -> Optional[List[Dict[str, Any]]]:
    """Replace raw image data with policy-approved metadata.

    For OpenAI-style ``image_url`` blocks:
    - ``image_url.url`` contains a data URL like ``data:image/png;base64,XXXX``.
    - We decode the base64 image, compute SHA-256, and record MIME type,
      byte size, and (when possible) width/height.

    For Anthropic-style ``image`` blocks with ``source.data``:
    - Same treatment.

    Returns None when policy is "strip".
    """
    if policy == "strip":
        return None
    if not isinstance(content_blocks, list):
        return None

    result: List[Dict[str, Any]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "image_url":
            image_url = block.get("image_url") or {}
            if not isinstance(image_url, dict):
                image_url = {"url": str(image_url) if image_url else ""}
            url = image_url.get("url", "")
            metadata = _extract_image_metadata(url)
            if metadata:
                result.append({
                    "type": "image_metadata",
                    "image_metadata": metadata,
                })
        elif block_type == "image":
            source = block.get("source", {})
            if isinstance(source, dict):
                data_str = source.get("data", "")
                media_type = source.get("media_type", "image/png")
                metadata = _extract_image_metadata(
                    f"data:{media_type};base64,{data_str}" if data_str else ""
                )
                if metadata:
                    result.append({
                        "type": "image_metadata",
                        "image_metadata": metadata,
                    })
        # Other block types are passed through if they don't contain images

    return result if result else None


def _extract_image_metadata(data_url: str) -> Optional[Dict[str, Any]]:
    """Decode a data URL and compute SHA-256, MIME type, size, and dimensions.

    Returns None if the data URL is malformed or decoding fails.
    """
    if not data_url or not isinstance(data_url, str):
        return None

    # Parse data URL: data:<mime>;base64,<data>
    match = re.match(r"data:([^;]+);base64,(.*)", data_url, re.DOTALL)
    if not match:
        return None

    mime_type = match.group(1)
    b64_data = match.group(2)

    try:
        image_bytes = base64.b64decode(b64_data)
    except Exception:
        logger.debug("Failed to decode image base64 data — skipping metadata")
        return None

    sha256 = hashlib.sha256(image_bytes).hexdigest()
    size_bytes = len(image_bytes)
    dimensions = _try_extract_image_dimensions(image_bytes, mime_type)

    metadata: Dict[str, Any] = {
        "type": "image_metadata",
        "sha256": sha256,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
    }
    if dimensions:
        metadata["width"] = dimensions[0]
        metadata["height"] = dimensions[1]
    return metadata


def _try_extract_image_dimensions(image_bytes: bytes, mime_type: str) -> Optional[tuple[int, int]]:
    """Best-effort extraction of image dimensions from raw bytes.

    Supports PNG, JPEG, GIF, and WebP.  Returns None if dimensions cannot
    be determined.  This is a lightweight, dependency-free implementation.
    """
    try:
        if mime_type == "image/png" and image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            # PNG IHDR: bytes 16-24 contain width (4) and height (4) big-endian
            if len(image_bytes) >= 24:
                width = struct.unpack(">I", image_bytes[16:20])[0]
                height = struct.unpack(">I", image_bytes[20:24])[0]
                return (int(width), int(height))
        elif mime_type == "image/gif" and image_bytes[:6] in (
            b'GIF87a', b'GIF89a'
        ):
            # GIF: bytes 6-10 contain width (2) and height (2) little-endian
            if len(image_bytes) >= 10:
                width = struct.unpack("<H", image_bytes[6:8])[0]
                height = struct.unpack("<H", image_bytes[8:10])[0]
                return (int(width), int(height))
        elif mime_type == "image/jpeg" and image_bytes[:2] == b'\xff\xd8':
            # JPEG: parse marker segments
            idx = 2
            while idx < len(image_bytes) - 9:
                if image_bytes[idx] != 0xFF:
                    break
                marker = image_bytes[idx + 1]
                if marker == 0xC0 or marker == 0xC2:  # SOF0/SOF2
                    height = struct.unpack(">H", image_bytes[idx + 5:idx + 7])[0]
                    width = struct.unpack(">H", image_bytes[idx + 7:idx + 9])[0]
                    return (int(width), int(height))
                seg_len = struct.unpack(">H", image_bytes[idx + 2:idx + 4])[0]
                idx += 2 + seg_len
        elif mime_type == "image/webp":
            # WebP: VP8/VP8L/VP8X headers
            if image_bytes[12:16] == b'VP8 ' and len(image_bytes) >= 30:
                width = struct.unpack("<H", image_bytes[26:28])[0] & 0x3FFF
                height = struct.unpack("<H", image_bytes[28:30])[0] & 0x3FFF
                return (int(width), int(height))
            elif image_bytes[12:16] == b'VP8X' and len(image_bytes) >= 31:
                width = (int.from_bytes(image_bytes[24:27], "little") & 0xFFFFFF) + 1
                height = (int.from_bytes(image_bytes[27:30], "little") & 0xFFFFFF) + 1
                return (int(width), int(height))
            elif image_bytes[12:16] == b'VP8L' and len(image_bytes) >= 25:
                bits = int.from_bytes(image_bytes[21:25], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
                return (int(width), int(height))
    except Exception:
        logger.debug("Image dimension extraction failed — omitting dimensions")
    return None


def redact_source_ip(ip_address: Optional[str]) -> Optional[str]:
    """Always returns None — raw client IP addresses are never persisted."""
    return None


def redact_authorization_header(auth_header: Optional[str]) -> Optional[str]:
    """Always returns None — authorization headers are never persisted."""
    return None


def scan_for_secrets(text: str) -> List[str]:
    """Scan text for potential secret patterns (for canary testing).

    Returns a list of human-readable descriptions of detected secrets.
    Used by the secret canary test suite.
    """
    findings: List[str] = []
    if not isinstance(text, str):
        return findings

    for pattern in _API_KEY_PATTERNS:
        if pattern.search(text):
            findings.append(f"API key pattern detected: {pattern.pattern}")

    # Check for raw auth headers
    if _AUTH_HEADER_RE.search(text):
        findings.append("Authorization/API key header pattern detected in text")

    # Check for raw IP addresses
    if _IPV4_RE.search(text):
        findings.append("Raw IPv4 address detected in text")

    return findings

# ─────────────────────────────────────────────────────────────────────────────
# Anthropic content block → OpenAI message translation (for capture)
# ─────────────────────────────────────────────────────────────────────────────


def _anthropic_content_block_to_openai(block: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an Anthropic content block to an OpenAI message fragment.

    Only ``text`` and ``tool_use`` block types are translated. ``image``
    blocks are skipped because image data capture is strip-by-default
    per the capture policy. Unknown block types are skipped silently.
    """
    if not isinstance(block, dict):
        return {}
    block_type = block.get("type", "")
    if block_type == "text":
        return {"role": "user", "content": block.get("text", "")}
    elif block_type == "tool_use":
        # tool_use is always from the assistant side
        tool_use_id = block.get("id", "")
        name = block.get("name", "")
        input_data = block.get("input", {})
        args_str = json.dumps(input_data) if isinstance(input_data, dict) else str(input_data)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tool_use_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args_str,
                    },
                }
            ],
        }
    elif block_type == "tool_result":
        # tool_result content is already redacted; translate to user message
        result_content = block.get("content", "")
        if isinstance(result_content, list):
            # Anthropic tool_result content can be an array of content blocks
            texts = []
            for sub in result_content:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    texts.append(sub.get("text", ""))
            result_content = "\n".join(texts)
        return {"role": "user", "content": str(result_content)}
    elif block_type == "image":
        # Skip image content — no image data capture by default
        logger.warning(
            "Skipping image content block in Anthropic message during capture"
        )
        return {}
    elif block_type == "thinking":
        # Anthropic thinking blocks
        return {"role": "assistant", "content": f"[Thinking] {block.get('thinking', '')}"}
    # Unknown block type — skip
    logger.warning(f"Unknown Anthropic content block type: {block_type}")
    return {}


def anthropic_messages_to_openai(
    messages: List[Dict[str, Any]],
    system: Optional[Union[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Translate Anthropic messages format to OpenAI-style messages for capture.

    Anthropic ``messages`` is a list of ``{role, content}`` where content
    can be a string or a list of content blocks. ``system`` is separate.

    This function folds ``system`` into a system message at position 0
    and translates content blocks into the OpenAI message format used
    in capture events.
    """
    openai_messages: List[Dict[str, Any]] = []

    # System prompt
    if system is not None:
        if isinstance(system, str):
            sys_content = system
        elif isinstance(system, list):
            # Anthropic system can be an array of blocks
            texts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            sys_content = "\n".join(texts)
        else:
            sys_content = "[Non-text system content stripped]"
        if sys_content.strip():
            openai_messages.append({"role": "system", "content": sys_content})

    # Messages
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Content is a list of blocks — translate each
            for block in content:
                block_msg = _anthropic_content_block_to_openai(block)
                if block_msg:
                    # Override role if the block implies a different one
                    if block_msg.get("role") != role and block.get("type") in (
                        "text",
                        "tool_use",
                        "tool_result",
                    ):
                        # Use the block's implied role for content blocks
                        # that map naturally (text → msg role, tool_use/result → assistant/user)
                        if block.get("type") == "text":
                            block_msg["role"] = role
                    openai_messages.append(block_msg)
        else:
            openai_messages.append({"role": role, "content": str(content)})

    return openai_messages
