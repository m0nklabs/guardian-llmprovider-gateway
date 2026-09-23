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

Cloud forwarding (opt-in twice, 2026-09-22): when ``stt.cloud_forwarding.enabled``
is set AND the requested ``model`` maps to a provider that declares
``cloud_stt: true`` (plus ``base_url`` + ``api_key``), the upload is forwarded
to that provider's OpenAI-compatible ``/audio/transcriptions`` endpoint. The
upstream model id is the final path segment of the requested name
(``cloudstt/cloudstt/whisper-large-v3`` -> ``whisper-large-v3``). A cloud
attempt runs
before the local engine chain; any cloud failure falls through to the local
engines unchanged, so disabling the switch (or omitting the model field)
restores the pre-forwarding behavior exactly.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from app.config_loader import CONFIG, load_stt_config
from app.proxy.providers import _expand_env

logger = logging.getLogger("Guardian.STT")

_LOG_UNSAFE = re.compile(r"[\r\n\t\x00-\x1f]+")


def _clean_log(value: Any, limit: int = 160) -> str:
    """Log-injection guard: collapse control characters (fake log lines via
    user-provided model ids, upstream response bodies, exception texts) and
    cap the length.  Response-bodies-in-502-details are JSON-escaped by
    FastAPI; only log sinks need this."""
    return _LOG_UNSAFE.sub(" ", str(value)).strip()[:limit]

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


def _cloud_stt_target(model: str) -> dict[str, str] | None:
    """Resolve a cloud STT target from the requested ``model`` form field.

    Returns ``{"name", "base_url", "api_key", "model"}`` when the provider
    (first path segment of the requested name) opted into cloud STT via
    ``cloud_stt: true`` in its provider file and exposes ``base_url`` +
    ``api_key``; None otherwise, so the request falls through to the local
    engine chain unchanged.
    """
    if not model or "/" not in model:
        return None
    provider_name = model.split("/", 1)[0].strip().lower()
    doc = (CONFIG.get("providers") or {}).get(provider_name)
    if not isinstance(doc, dict) or not doc.get("cloud_stt"):
        return None
    base_url = _expand_env(str(doc.get("base_url") or "")).rstrip("/")
    api_key = _expand_env(str(doc.get("api_key") or ""))
    if not base_url or not api_key:
        return None
    return {
        "name": provider_name,
        "base_url": base_url,
        "api_key": api_key,
        "model": model.split("/")[-1].strip(),
    }


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

    raw_language = str(form.get("language") or "") or None
    language = _qwen_language(raw_language)
    requested_model = str(form.get("model") or "") or None
    failures: list[str] = []

    # Cloud forwarding (opt-in): a cloud attempt runs before the local engine
    # chain and never blocks local failover on failure.
    if (cfg.get("cloud_forwarding") or {}).get("enabled", False) and requested_model:
        target = _cloud_stt_target(requested_model)
        if target is not None:
            result = await _try_cloud_backend(
                target, audio_bytes, audio_content_type, raw_language,
                getattr(upload, "filename", None), client_id, timeout_s,
            )
            if isinstance(result, Response):
                return result
            failures.append(result)

    for backend in backends:
        result = await _try_backend(backend, audio_bytes, audio_content_type, language, client_id, timeout_s, ensure_timeout_s)
        if isinstance(result, Response):
            return result
        failures.append(result)
    logger.warning(
        "STT: all providers failed for client '%s': %s",
        _clean_log(client_id, 64), _clean_log("; ".join(failures)),
    )
    raise HTTPException(status_code=502, detail="; ".join(failures))


async def _try_cloud_backend(
    target: dict[str, str],
    audio_bytes: bytes,
    audio_content_type: str,
    language: str | None,
    upload_filename: str | None,
    client_id: str,
    timeout_s: float,
) -> Response | str:
    """Forward the upload to a cloud OpenAI-compatible transcription endpoint
    (any OpenAI-compatible endpoint). Returns a Response on success or a
    failure string for the
    failover log — the local engines still get a chance afterwards. The
    ``language`` value is passed through verbatim (ISO-639-1); the Qwen name
    mapping is engine-specific and does not apply here."""
    name = target["name"]
    started = time.monotonic()
    files = {"file": (upload_filename or "audio.wav", audio_bytes, audio_content_type)}
    data: dict[str, str] = {"model": target["model"]}
    if language:
        data["language"] = language
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{target['base_url']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {target['api_key']}"},
                files=files,
                data=data,
            )
    except httpx.HTTPError as exc:
        logger.warning("STT cloud provider '%s' unreachable: %s", _clean_log(name), _clean_log(exc))
        return f"{name}: unreachable ({exc})"
    if resp.status_code != 200:
        return f"{name}: cloud HTTP {resp.status_code}: {resp.text[:120]}"
    duration_ms = (time.monotonic() - started) * 1000
    try:
        text_len = len(resp.json().get("text") or "")
    except Exception:  # noqa: BLE001 — non-JSON body is still passed through
        text_len = -1
    logger.info(
        "🎙 STT: client '%s' served by cloud '%s' (%s) -> %s chars in %.1fs",
        _clean_log(client_id, 64), _clean_log(name, 64), _clean_log(target["model"], 64),
        text_len, duration_ms / 1000,
    )
    return Response(content=resp.content, status_code=200, media_type="application/json")


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
