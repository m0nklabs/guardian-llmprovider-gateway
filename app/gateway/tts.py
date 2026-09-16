"""TTS routing (F6 extension): OpenAI-compatible /v1/audio/speech → the
Windows qwen3tts-http engine (teams-host :11450).

The engine's contract is NOT OpenAI-shaped (own ``POST /tts`` → audio/wav;
see guardian AGENT_JOURNAL 2026-09-16): ``{"text", "instruct", "seed",
"sub_seed", "temperature"}``.  This module maps the OpenAI speech-request
fields onto it:

- ``input``  → ``text``
- ``voice``  → ``instruct`` (OpenAI stock voice names map to voice-design
  instructions; any other string passes through verbatim as the instruction)
- ``seed`` / ``sub_seed`` / ``temperature`` pass through when present
- ``response_format`` must be ``wav`` (the engine serves 16-bit PCM mono
  24 kHz WAV only)

Config (``tts:`` in global.settings.yaml, read at request time so
POST /api/config/reload applies without a restart):
``enabled`` / ``backend_url`` / ``timeout_seconds`` / ``default_instruct``.

Lifecycle note: the engine runs as an always-on NSSM service on the Windows
host (auto-start, keyless, LAN-firewall-covered) — no ensure/unload semantics
here; the caretaker does not manage this process (see caretaker HANDOFF).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from app.config_loader import load_tts_config

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


async def handle_audio_speech(request: Request, client_id: str) -> Response:
    """POST /v1/audio/speech — OpenAI-compatible TTS via the Windows engine."""
    cfg = load_tts_config()
    if not cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="TTS routing is disabled")
    backend_url = str(cfg.get("backend_url", "")).rstrip("/")
    if not backend_url:
        raise HTTPException(status_code=503, detail="TTS backend_url is not configured")
    timeout_s = float(cfg.get("timeout_seconds", 120) or 120)

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

    # On-demand lifecycle: the Windows caretaker starts the engine when it is
    # down and refreshes its idle timer on every ensure (the caretaker's idle
    # watcher stops the service when the route goes unused — VRAM freed).
    if cfg.get("ondemand_enabled", True) and cfg.get("caretaker_url"):
        ensure_url = str(cfg["caretaker_url"]).rstrip("/") + "/tts/ensure"
        # The tts section is read raw from YAML: expand ${VAR} references the
        # same way the provider registry does (single source of truth).
        from app.proxy.providers import _expand_env

        ensure_key = _expand_env(str(cfg.get("caretaker_key") or ""))
        ensure_headers = {"Authorization": f"Bearer {ensure_key}"} if ensure_key else {}
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                ensure_resp = await client.post(ensure_url, headers=ensure_headers)
        except httpx.HTTPError as exc:
            logger.warning("TTS caretaker unreachable at %s: %s", ensure_url, exc)
            raise HTTPException(status_code=502, detail=f"TTS caretaker unreachable: {exc}")
        ensure_body: dict[str, Any] = {}
        try:
            parsed = ensure_resp.json()
            if isinstance(parsed, dict):
                ensure_body = parsed
        except Exception:  # noqa: BLE001 — a non-JSON body still fails the check below
            pass
        if ensure_resp.status_code != 200 or ensure_body.get("ok") is False:
            detail = str(ensure_body.get("reason") or ensure_resp.text)[:200]
            logger.warning(
                "TTS ensure failed for client '%s': HTTP %s %s",
                client_id, ensure_resp.status_code, detail,
            )
            raise HTTPException(
                status_code=502,
                detail=f"TTS engine could not be started: {detail or f'HTTP {ensure_resp.status_code}'}",
            )
        logger.info("🔊 TTS ensure: %s", ensure_resp.json().get("already_running", "?"))

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{backend_url}/tts", json=payload)
    except httpx.HTTPError as exc:
        logger.warning("TTS engine unreachable at %s: %s", backend_url, exc)
        raise HTTPException(status_code=502, detail=f"TTS engine unreachable: {exc}")

    duration_ms = (time.monotonic() - started) * 1000
    if resp.status_code != 200:
        logger.warning(
            "TTS engine error for client '%s': HTTP %s (%s bytes)",
            client_id, resp.status_code, len(resp.content),
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    logger.info(
        "🔊 TTS: client '%s' → %s bytes audio/wav in %.1fs (instruct: %r)",
        client_id, len(resp.content), duration_ms / 1000, payload["instruct"][:60],
    )
    return Response(content=resp.content, status_code=200, media_type="audio/wav")
