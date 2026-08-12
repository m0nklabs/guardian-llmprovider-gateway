"""Multimodal request normalization — vision probing, error mapping, thinking params.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Handles image-input detection, multimodal runtime probing/preflight, backend
error extraction/translation, OpenAI error responses, reasoning/thinking
parameter defaults, and qwen chat-template sanitization.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("Guardian")

# ── Module state ─────────────────────────────────────────────────────
_VISION_PROBE_IMAGE_DATA_URL: Optional[str] = None

# ── Injected (set once at startup by init()) ─────────────────────────
_model_manager = None
_llama_server_url = None
_queue_headers = None


def init(*, model_manager, llama_server_url, queue_headers) -> None:
    """Inject all dependencies. Called once at startup."""
    global _model_manager, _llama_server_url, _queue_headers
    _model_manager = model_manager
    _llama_server_url = llama_server_url
    _queue_headers = queue_headers


def messages_contain_image_input(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"image_url", "input_image", "image"}:
                return True
    return False


def build_probe_image_data_url() -> str:
    global _VISION_PROBE_IMAGE_DATA_URL
    if _VISION_PROBE_IMAGE_DATA_URL is not None:
        return _VISION_PROBE_IMAGE_DATA_URL

    width = 128
    height = 128
    row = b"\x00" + (b"\xff\xff\xff" * width)
    raw = row * height
    compressed = zlib.compress(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", checksum)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    _VISION_PROBE_IMAGE_DATA_URL = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return _VISION_PROBE_IMAGE_DATA_URL


def extract_backend_error_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or text)
        detail = parsed.get("detail")
        if isinstance(detail, str):
            return detail
    return text


def truncate_error_message(message: str, limit: int = 300) -> str:
    cleaned = " ".join(message.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def openai_error_response(
    *,
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    payload = {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }
    return JSONResponse(status_code=status_code, content=payload, headers=headers or {})


async def probe_multimodal_runtime(model_name: str) -> Dict[str, Any]:
    capability = _model_manager.get_vision_capability(model_name)
    if capability["status"] in {"supported", "unsupported", "misconfigured", "text_only", "load_failed"}:
        return capability

    payload = {
        "model": model_name,
        "stream": False,
        "max_tokens": 1,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": build_probe_image_data_url()}},
                    {"type": "text", "text": "Reply with one short word."},
                ],
            }
        ],
    }

    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(3):
            resp = await client.post(f"{_llama_server_url}/v1/chat/completions", json=payload)
            message = extract_backend_error_message(resp.content)
            lowered = message.lower()

            if 200 <= resp.status_code < 300:
                _model_manager.mark_vision_validation(model_name, "supported")
                return _model_manager.get_vision_capability(model_name)

            if resp.status_code == 503 and "loading model" in lowered and attempt < 2:
                _model_manager.mark_vision_validation(model_name, "loading", message)
                await asyncio.sleep(1.0)
                continue

            if resp.status_code == 503 and "loading model" in lowered:
                _model_manager.mark_vision_validation(model_name, "loading", message)
                return _model_manager.get_vision_capability(model_name)

            failure_status = "unsupported"
            if resp.status_code == 503:
                failure_status = "loading"
            _model_manager.mark_vision_validation(model_name, failure_status, message or f"HTTP {resp.status_code}")
            return _model_manager.get_vision_capability(model_name)

    return _model_manager.get_vision_capability(model_name)


async def preflight_multimodal_request(
    model_name: str,
    request_id: str,
    queue_wait_ms: float,
) -> Optional[JSONResponse]:
    headers = _queue_headers(request_id, queue_wait_ms)
    capability = _model_manager.get_vision_capability(model_name)

    if not capability["configured"]:
        return openai_error_response(
            status_code=400,
            message=f"Model '{model_name}' is text-only in Guardian and cannot accept image_url content.",
            error_type="invalid_request_error",
            code="vision_not_configured",
            headers=headers,
        )

    if not capability["mmproj_exists"]:
        return openai_error_response(
            status_code=400,
            message=f"Model '{model_name}' is configured for vision but its mmproj file is missing.",
            error_type="invalid_request_error",
            code="mmproj_missing",
            headers=headers,
        )

    if capability["status"] != "supported":
        capability = await probe_multimodal_runtime(model_name)

    status = capability["status"]
    if status == "supported":
        return None

    if status in {"loading", "load_failed"}:
        return openai_error_response(
            status_code=503,
            message=f"Model '{model_name}' is not ready for image requests yet: {truncate_error_message(capability.get('last_error') or 'still loading')}",
            error_type="unavailable_error",
            code="vision_model_unavailable",
            headers=headers,
        )

    return openai_error_response(
        status_code=422,
        message=(
            f"Model '{model_name}' is configured for vision, but its runtime rejected OpenAI image_url content. "
            f"Backend detail: {truncate_error_message(capability.get('last_error') or 'unknown multimodal error')}"
        ),
        error_type="invalid_request_error",
        code="vision_not_supported",
        headers=headers,
    )


def desired_runtime_vision_enabled(model_name: str, has_image_inputs: bool) -> bool:
    """Return whether this request should load the target model with mmproj."""
    capability = _model_manager.get_vision_capability(model_name)
    return bool(has_image_inputs and capability.get("configured"))


def model_disables_thinking_by_default(model_name: str) -> bool:
    """Return whether a configured model is a non-reasoning/special runtime."""
    config = _model_manager.models.get(model_name, {})
    if config.get("default_enable_thinking") is False or config.get("enable_thinking") is False:
        return True

    model_type = str(config.get("model_type", "")).strip().lower()
    if model_type in {"embedding", "embeddings"}:
        return True

    searchable = " ".join(
        str(value).lower()
        for value in (model_name, config.get("path", ""), config.get("extra_args", ""))
    )
    return "embed" in searchable or "--reasoning off" in searchable


def request_explicitly_disables_thinking(payload: Dict[str, Any]) -> bool:
    if payload.get("reasoning_budget") == 0:
        return True
    template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(template_kwargs, dict) and template_kwargs.get("enable_thinking") is False:
        return True
    # Anthropic format: thinking: {"type": "disabled"}
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return True
    return False


def apply_anthropic_thinking_to_llama_params(payload: Dict[str, Any]) -> bool:
    """Convert Anthropic ``thinking`` config to llama-server parameters.

    llama-server's ``/v1/messages`` endpoint doesn't properly handle
    ``thinking: {type: "disabled"}`` — thinking stays enabled. This function
    translates the Anthropic thinking config to llama-server's native
    ``reasoning_budget`` and ``chat_template_kwargs.enable_thinking``
    parameters so that thinking is correctly controlled.

    Also handles ``thinking: {type: "enabled", budget_tokens: N}`` by
    setting ``reasoning_budget: N``.
    """
    thinking = payload.get("thinking")
    if not isinstance(thinking, dict):
        return False

    changed = False
    t_type = thinking.get("type", "")

    if t_type == "disabled":
        # Disable thinking entirely
        if payload.get("reasoning_budget") != 0:
            payload["reasoning_budget"] = 0
            changed = True
        template_kwargs = payload.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            template_kwargs = {}
            payload["chat_template_kwargs"] = template_kwargs
            changed = True
        if template_kwargs.get("enable_thinking") is not False:
            template_kwargs["enable_thinking"] = False
            changed = True

    elif t_type == "enabled":
        # Map budget_tokens → reasoning_budget
        budget = thinking.get("budget_tokens", 0)
        if budget and payload.get("reasoning_budget") != budget:
            payload["reasoning_budget"] = budget
            changed = True

    # type == "adaptive": leave as-is (llama-server's default behavior)
    return changed


def apply_request_reasoning_defaults(path: str, payload: Dict[str, Any], model_name: str) -> bool:
    """Apply no-thinking request flags only for explicit or special runtimes."""
    if path not in {"chat/completions", "messages", "completions"}:
        return False

    should_disable = (
        request_explicitly_disables_thinking(payload)
        or model_disables_thinking_by_default(model_name)
    )
    if not should_disable:
        return False

    changed = False
    if payload.get("reasoning_budget") != 0:
        payload["reasoning_budget"] = 0
        changed = True

    if path in {"chat/completions", "messages"}:
        template_kwargs = payload.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            template_kwargs = {}
            payload["chat_template_kwargs"] = template_kwargs
            changed = True
        if template_kwargs.get("enable_thinking") is not False:
            template_kwargs["enable_thinking"] = False
            changed = True

    return changed


_SYSTEM_CONTEXT_UPDATE_PREFIX = "[System Context Update]:\n"


def stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                else:
                    parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
            elif part is not None:
                parts.append(str(part))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def sanitize_messages_for_qwen_chat_template(messages: Any) -> Any:
    """Demote later system messages so strict Qwen templates can render them."""
    if not isinstance(messages, list):
        return messages

    sanitized: list[Any] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            sanitized.append(message)
            continue

        if message.get("role") == "system" and index > 0:
            updated = dict(message)
            updated["role"] = "user"
            content = stringify_message_content(message.get("content"))
            updated["content"] = (
                f"{_SYSTEM_CONTEXT_UPDATE_PREFIX}{content}"
                if content
                else _SYSTEM_CONTEXT_UPDATE_PREFIX.rstrip("\n")
            )
            sanitized.append(updated)
            continue

        sanitized.append(message)

    return sanitized


def map_multimodal_backend_error(
    model_name: str,
    status_code: int,
    body: bytes,
    request_id: str,
    queue_wait_ms: float,
) -> Optional[JSONResponse]:
    message = extract_backend_error_message(body)
    lowered = message.lower()
    headers = _queue_headers(request_id, queue_wait_ms)

    if status_code == 503 and "loading model" in lowered:
        _model_manager.mark_vision_validation(model_name, "loading", message)
        return openai_error_response(
            status_code=503,
            message=f"Model '{model_name}' is still loading its multimodal runtime. Retry shortly.",
            error_type="unavailable_error",
            code="vision_model_unavailable",
            headers=headers,
        )

    if "image input is not supported" in lowered or "mmproj" in lowered:
        _model_manager.mark_vision_validation(model_name, "unsupported", message)
        return openai_error_response(
            status_code=422,
            message=f"Model '{model_name}' rejected image_url content at runtime: {truncate_error_message(message)}",
            error_type="invalid_request_error",
            code="vision_not_supported",
            headers=headers,
        )

    if status_code >= 500:
        _model_manager.mark_vision_validation(model_name, "unsupported", message or f"HTTP {status_code}")
        return openai_error_response(
            status_code=422,
            message=(
                f"Model '{model_name}' is configured for vision, but the backend image path failed. "
                f"Backend detail: {truncate_error_message(message or f'HTTP {status_code}') }"
            ),
            error_type="invalid_request_error",
            code="vision_runtime_unavailable",
            headers=headers,
        )

    return None
