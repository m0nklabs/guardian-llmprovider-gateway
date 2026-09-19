"""STT routing — provider-driven, platform-agnostic (the inbound half of the
speech chain; mirrors app/gateway/tts.py).

A host participates in transcription routing when its provider file declares
``stt_url`` (the qwen3-asr HTTP sidecar on :11451) plus ``management_url`` +
``management_key`` (the caretaker control API).  ``stt.providers`` lists the
provider names in failover order.  Per attempt: ensure via the caretaker
(``POST {management_url}/stt/ensure`` — the same on-demand lifecycle as TTS:
VRAM gate, wait-or-give-up, spawn/idle-stop), then forward the RAW audio bytes
to ``POST {stt_url}/transcriptions``.

Endpoint: ``POST /v1/audio/transcriptions`` — OpenAI Whisper-compatible
multipart (``file`` + optional ``model``/``language``).  ``language`` is an
ISO-639-1 code (``nl``/``en``) mapped to the Qwen3-ASR language names; without
it the host default (``STT_LANGUAGE`` env on the engine) applies.

Config (``stt:`` section, read at request time so POST /api/config/reload
applies without a restart): ``enabled`` / ``providers`` / ``timeout_seconds`` /
``ensure_timeout_seconds`` (same arithmetic as TTS: must cover the caretaker's
``CARETAKER_STT_START_TIMEOUT`` + ``CARETAKER_STT_VRAM_WAIT_SECONDS``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from app.config_loader import CONFIG, load_stt_config
from app.proxy.providers import _expand_env

logger = logging.getLogger("Guardian.STT")

# OpenAI ISO-639-1 codes -> the language names Qwen3-ASR understands (it is a
# decoder prompt, not a tag; the example uses names like "Korean", "English").
_ISO_TO_QWEN_LANGUAGE: dict[str, str] = {
    "nl": "Dutch",
    "en": "English",
    "de": "German",
    "fr": "French",
    "zh": "Chinese",
}


def _qwen_language(iso: str | None) -> str | None:
    if not iso:
        return None
    return _ISO_TO_QWEN_LANGUAGE.get(iso.strip().lower(), iso.strip().capitalize())


def _stt_backends(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Resolve the ordered provider backends from the provider documents
    (identical shape to the TTS backends — see app/gateway/tts.py)."""
    provider_docs = CONFIG.get("providers") or {}
    backends: list[dict[str, str]] = []
    for name in cfg.get("providers") or []:
        doc = provider_docs.get(name)
        if not isinstance(doc, dict):
            continue
        stt_url = _expand_env(str(doc.get("stt_url") or "")).rstrip("/")
        mgmt_url = _expand_env(str(doc.get("management_url") or "")).rstrip("/")
        if not stt_url or not mgmt_url:
            continue
        key = _expand_env(str(doc.get("management_key") or doc.get("api_key") or ""))
        backends.append({"name": name, "stt_url": stt_url, "management_url": mgmt_url, "key": key})
    return backends


async def handle_audio_transcriptions(request: Request, client_id: str) -> Response:
    """POST /v1/audio/transcriptions — OpenAI-compatible STT with failover."""
    cfg = load_stt_config()
    if not cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="STT routing is disabled")
    timeout_s = float(cfg.get("timeout_seconds", 300) or 300)
    ensure_timeout_s = float(cfg.get("ensure_timeout_seconds", 420) or 420)
    backends = _stt_backends(cfg)
    if not backends:
        raise HTTPException(status_code=503, detail="no provider declares an STT engine (stt_url)")

    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="request must be multipart/form-data with a 'file' field")

    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        raise HTTPException(status_code=400, detail="'file' (audio upload) is required")
    audio_bytes = await upload.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="'file' contains no audio bytes")
    audio_content_type = upload.content_type or "audio/wav"

    language = _qwen_language(str(form.get("language") or "") or None)
    failures: list[str] = []
    for backend in backends:
        result = await _try_backend(backend, audio_bytes, audio_content_type, language, client_id, timeout_s, ensure_timeout_s)
        if isinstance(result, Response):
            return result
        failures.append(result)
    logger.warning("STT: all providers failed for client '%s': %s", client_id, "; ".join(failures))
    raise HTTPException(status_code=502, detail="; ".join(failures))


async def _try_backend(
    backend: dict[str, str],
    audio_bytes: bytes,
    audio_content_type: str,
    language: str | None,
    client_id: str,
    timeout_s: float,
    ensure_timeout_s: float,
) -> Response | str:
    """ensure -> forward on one provider backend; returns a Response on success
    or a failure string for the failover log."""
    name, stt_url, mgmt_url = backend["name"], backend["stt_url"], backend["management_url"]
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {backend['key']}"} if backend["key"] else {}
    try:
        async with httpx.AsyncClient(timeout=ensure_timeout_s) as client:
            ensure_resp = await client.post(f"{mgmt_url}/stt/ensure", headers=headers)
        ensure_body: dict[str, Any] = {}
        try:
            parsed = ensure_resp.json()
            if isinstance(parsed, dict):
                ensure_body = parsed
        except Exception:  # noqa: BLE001
            pass
        if ensure_resp.status_code != 200 or ensure_body.get("ok") is False:
            reason = str(ensure_body.get("reason") or ensure_resp.text)[:160]
            return f"{name}: ensure failed (HTTP {ensure_resp.status_code}): {reason}"
        logger.info("🎙 STT ensure via %s: cold_start=%s", name, ensure_body.get("cold_start", False))

        forward_headers = dict(headers)
        forward_headers["Content-Type"] = audio_content_type
        if language:
            forward_headers["X-Transcription-Language"] = language
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{stt_url}/transcriptions",
                params={"language": language} if language else None,
                content=audio_bytes,
                headers=forward_headers,
            )
        if resp.status_code != 200:
            return f"{name}: engine HTTP {resp.status_code}: {resp.text[:120]}"
        duration_ms = (time.monotonic() - started) * 1000
        body = resp.json()
        logger.info(
            "🎙 STT: client '%s' served by '%s' -> %s chars in %.1fs",
            client_id, name, len(body.get("text") or ""), duration_ms / 1000,
        )
        return Response(
            content=resp.content,
            status_code=200,
            media_type="application/json",
        )
    except httpx.HTTPError as exc:
        logger.warning("STT provider '%s' unreachable: %s", name, exc)
        return f"{name}: unreachable ({exc})"
