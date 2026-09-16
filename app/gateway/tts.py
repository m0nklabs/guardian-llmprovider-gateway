"""TTS routing — provider-driven, platform-agnostic (F6 follow-up).

Guardian makes no distinction between cloud and local: a TTS backend is a
PROVIDER capability declared in ``config/providers/<name>.settings.yaml`` —

    management_url: http://…:11441      # the host's caretaker control API
    management_key: ${VAR}              # the caretaker's Bearer key reference
    tts_url: http://…:11450             # the host's qwen3tts-http engine

``tts.providers`` in global.settings.yaml lists the provider names in failover
order (first = primary). The handler walks them: ensure via the caretaker
(spawn/idle-timer — the same contract on a LAN box or a RunPod), then forward
to the engine; any failure falls through to the next provider like any other
failover attempt.

Engine contract (NOT OpenAI-shaped): ``POST /tts`` ``{"text", "instruct",
"seed", "sub_seed", "temperature"}`` -> audio/wav (16-bit PCM mono 24 kHz).
Mapping: ``input``->``text``; ``voice``->``instruct`` (OpenAI stock voice names
map to VoiceDesign instructions; unknown strings pass through verbatim);
``seed``/``sub_seed``/``temperature`` pass through when present;
``response_format`` must be ``wav``.

Config (``tts:`` section, read at request time so POST /api/config/reload
applies without a restart): ``enabled`` / ``providers`` / ``timeout_seconds`` /
``default_instruct``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from app.config_loader import CONFIG, load_tts_config
from app.proxy.providers import _expand_env

logger = logging.getLogger("Guardian.TTS")

# OpenAI stock voice names (clients like Open WebUI send these verbatim) map to
# Qwen3-TTS VoiceDesign instructions; unknown strings pass through as-is.
_STOCK_VOICE_INSTRUCT: dict[str, str] = {
    "alloy": "neutral, balanced voice",
    "echo": "calm male voice",
    "fable": "expressive storyteller voice",
    "onyx": "deep, authoritative male voice",
    "nova": "bright, friendly female voice",
    "shimmer": "soft, warm female voice",
    "coral": "warm, engaging female voice",
    "sage": "calm, wise voice",
    "ash": "clear, neutral male voice",
    "ballad": "melodic, expressive voice",
}


def _resolve_instruct(body: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Resolve the voice-design instruction: explicit ``instruct`` extra wins,
    then ``voice`` (stock-mapped or verbatim), then the config default."""
    explicit = body.get("instruct")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    voice = body.get("voice")
    if isinstance(voice, str) and voice.strip():
        key = voice.strip().lower()
        return _STOCK_VOICE_INSTRUCT.get(key, voice.strip())
    default = cfg.get("default_instruct")
    if isinstance(default, str) and default.strip():
        return default.strip()
    return ""


def _build_engine_payload(body: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Map the OpenAI speech request onto the engine's /tts contract."""
    payload: dict[str, Any] = {
        "text": str(body.get("input", "")).strip(),
        "instruct": _resolve_instruct(body, cfg),
    }
    for passthrough in ("seed", "sub_seed", "temperature"):
        value = body.get(passthrough)
        if value is not None:
            payload[passthrough] = value
    return payload


def _tts_backends(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Resolve the ordered provider backends from the provider documents.

    Every backend needs ``tts_url`` (engine) and ``management_url`` (the
    host's caretaker).  Providers missing either are skipped silently — a
    provider without a TTS engine simply doesn't participate in speech
    routing.
    """
    provider_docs = CONFIG.get("providers") or {}
    backends: list[dict[str, str]] = []
    for name in cfg.get("providers") or []:
        doc = provider_docs.get(name)
        if not isinstance(doc, dict):
            continue
        tts_url = _expand_env(str(doc.get("tts_url") or "")).rstrip("/")
        mgmt_url = _expand_env(str(doc.get("management_url") or "")).rstrip("/")
        if not tts_url or not mgmt_url:
            continue
        key = _expand_env(str(doc.get("management_key") or doc.get("api_key") or ""))
        backends.append({"name": name, "tts_url": tts_url, "management_url": mgmt_url, "key": key})
    return backends


async def handle_audio_speech(request: Request, client_id: str) -> Response:
    """POST /v1/audio/speech — OpenAI-compatible TTS with provider failover."""
    cfg = load_tts_config()
    if not cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="TTS routing is disabled")
    timeout_s = float(cfg.get("timeout_seconds", 120) or 120)
    backends = _tts_backends(cfg)
    if not backends:
        raise HTTPException(status_code=503, detail="no provider declares a TTS engine (tts_url)")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="request body must be valid JSON")
    if not isinstance(body, dict) or not str(body.get("input", "")).strip():
        raise HTTPException(status_code=400, detail="'input' (non-empty string) is required")

    response_format = str(body.get("response_format", "wav") or "wav").lower()
    if response_format not in ("wav", "pcm"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"response_format '{response_format}' is not supported; "
                "the TTS engine serves wav (16-bit PCM mono 24 kHz) only"
            ),
        )

    payload = _build_engine_payload(body, cfg)
    failures: list[str] = []
    for backend in backends:
        result = await _try_backend(backend, payload, client_id, timeout_s)
        if isinstance(result, Response):
            return result
        failures.append(result)
    logger.warning("TTS: all providers failed for client '%s': %s", client_id, "; ".join(failures))
    raise HTTPException(status_code=502, detail="; ".join(failures))


async def _try_backend(
    backend: dict[str, str], payload: dict[str, Any], client_id: str, timeout_s: float
) -> Response | str:
    """ensure -> forward on one provider backend; returns a Response on success
    or a failure string for the failover log."""
    name, tts_url, mgmt_url = backend["name"], backend["tts_url"], backend["management_url"]
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {backend['key']}"} if backend["key"] else {}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            ensure_resp = await client.post(f"{mgmt_url}/tts/ensure", headers=headers)
        ensure_body: dict[str, Any] = {}
        try:
            parsed = ensure_resp.json()
            if isinstance(parsed, dict):
                ensure_body = parsed
        except Exception:  # noqa: BLE001 — a non-JSON body still fails the check below
            pass
        if ensure_resp.status_code != 200 or ensure_body.get("ok") is False:
            reason = str(ensure_body.get("reason") or ensure_resp.text)[:160]
            return f"{name}: ensure failed (HTTP {ensure_resp.status_code}): {reason}"
        logger.info("🔊 TTS ensure via %s: %s", name, ensure_body.get("already_running", "?"))

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{tts_url}/tts", json=payload, headers=headers)
        if resp.status_code != 200:
            return f"{name}: engine HTTP {resp.status_code}: {resp.text[:120]}"
        duration_ms = (time.monotonic() - started) * 1000
        logger.info(
            "🔊 TTS: client '%s' served by '%s' -> %s bytes audio/wav in %.1fs (instruct: %r)",
            client_id, name, len(resp.content), duration_ms / 1000, payload["instruct"][:60],
        )
        return Response(content=resp.content, status_code=200, media_type="audio/wav")
    except httpx.HTTPError as exc:
        logger.warning("TTS provider '%s' unreachable: %s", name, exc)
        return f"{name}: unreachable ({exc})"
