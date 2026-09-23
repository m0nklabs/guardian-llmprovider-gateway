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
``ensure_timeout_seconds`` / ``default_instruct``.

Speech model addresses (2026-09-23, route-oriented — no opt-in switches):
a requested ``model`` shaped ``[guardian/]{provider}/{brand}/{model}`` resolves
through the provider file ALONE (see app/gateway/speech_routing.py):
``tts_url`` -> that provider's local engine, ``base_url`` + ``api_key`` -> its
OpenAI-compatible ``/audio/speech`` (upstream id = ``{brand}/{model}``, e.g.
``guardian/groq/canopylabs/orpheus-v1-english``). An explicit address is EXACT
— failures surface honestly for that route and never fall back to a different
provider. Without an addressable ``model`` field the default local chain
(``tts.providers``) serves, unchanged. The forwarded payload is the canonical
OpenAI shape (``model``/``input``/``voice``/``response_format``[/``speed``]);
the local engine's clone passthroughs (``ref_audio``/``ref_text``/``zero_shot``)
and ``instruct`` do NOT travel to cloud providers.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from app.config_loader import CONFIG, load_tts_config
from app.gateway.speech_routing import address_intent, parse_speech_model, resolve_speech_route
from app.proxy.providers import _expand_env

logger = logging.getLogger("Guardian.TTS")

_LOG_UNSAFE = re.compile(r"[\r\n\t\x00-\x1f]+")


def _clean_log(value: Any, limit: int = 160) -> str:
    """Log-injection guard (mirror of app/gateway/stt.py): collapse control
    characters (fake log lines via user-provided model ids, upstream response
    bodies, exception texts) and cap the length. Response-bodies-in-502-details
    are JSON-escaped by FastAPI; only log sinks need this."""
    return _LOG_UNSAFE.sub(" ", str(value)).strip()[:limit]

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
    """Map the OpenAI speech request onto the engine's /tts contract.

    ``ref_audio`` (filename inside the engine's samples dir), ``ref_text``
    and ``language`` enable the engine's clone mode; they are optional and
    change nothing for design-mode clients.
    """
    payload: dict[str, Any] = {
        "text": str(body.get("input", "")).strip(),
        "instruct": _resolve_instruct(body, cfg),
    }
    for passthrough in ("seed", "sub_seed", "temperature", "ref_audio", "ref_text",
                        "language", "zero_shot"):
        value = body.get(passthrough)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
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
    # Must exceed the caretaker's CARETAKER_TTS_START_TIMEOUT: a cold start
    # (model load) can take minutes and the ensure BLOCKS until the engine is
    # healthy — a shorter client timeout silently turns a working primary
    # into a failover.
    ensure_timeout_s = float(cfg.get("ensure_timeout_seconds", 300) or 300)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="request body must be valid JSON")
    if not isinstance(body, dict) or not str(body.get("input", "")).strip():
        raise HTTPException(status_code=400, detail="'input' (non-empty string) is required")

    response_format = str(body.get("response_format", "wav") or "wav").lower()

    # Route resolution happens BEFORE response-format validation: a routed
    # request may request any format the upstream provider supports (e.g.
    # mp3); the wav/pcm restriction is a property of the LOCAL engine only and
    # must not 400 a request the route could serve. guardian/-prefixed values
    # always intend to be addresses — malformed or unknown ones 404 here.
    requested_model = str(body.get("model") or "")
    cloud_target = None
    if requested_model and (address_intent(requested_model) or parse_speech_model(requested_model)):
        if not parse_speech_model(requested_model):
            raise HTTPException(status_code=404, detail="model_not_served")
        cloud_target = resolve_speech_route(requested_model, "tts_url")
        if cloud_target is None:
            raise HTTPException(status_code=404, detail="model_not_served")

    speed = body.get("speed")
    if speed is not None and (
        not isinstance(speed, (int, float)) or isinstance(speed, bool) or not (0.25 <= speed <= 4.0)
    ):
        # OpenAI /audio/speech contract (0.25-4.0). Validating early gives the
        # client a clear 400 instead of a burned cloud attempt followed by an
        # opaque upstream 4xx; the local engine ignores it but must not become
        # the excuse for passing garbage upstream.
        raise HTTPException(status_code=400, detail="'speed' must be a number between 0.25 and 4.0")

    # The wav/pcm restriction binds every LOCAL route (default chain and
    # explicit local address): the engine serves wav only and ignores the
    # field, so an mp3 request must 400 instead of silently returning wav.
    # Cloud routes pass the format verbatim (the upstream validates).
    if response_format not in ("wav", "pcm") and (cloud_target is None or cloud_target["kind"] != "cloud"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"response_format '{response_format}' is not supported; "
                "the TTS engine serves wav (16-bit PCM mono 24 kHz) only"
            ),
        )

    payload = _build_engine_payload(body, cfg)
    failures: list[str] = []

    # Route-oriented resolution: an addressable model is EXACT (no silent
    # fallback to another provider); only the default path (no address) uses
    # the configured failover chain. The backends-empty 503 is only raised on
    # the default path, so a cloud-only deployment stays possible.
    if cloud_target is not None:
        if cloud_target["kind"] == "cloud":
            result = await _try_cloud_backend(cloud_target, body, response_format, client_id, timeout_s)
            if isinstance(result, Response):
                return result
            logger.warning(
                "TTS: exact route '%s' failed for client '%s': %s",
                _clean_log(requested_model, 96), _clean_log(client_id, 64), _clean_log(result),
            )
            raise HTTPException(status_code=502, detail=result)
        # explicit local route: this provider's engine only
        result = await _try_backend(
            {"name": cloud_target["provider"], "tts_url": cloud_target["tts_url"],
             "management_url": cloud_target["management_url"], "key": cloud_target["management_key"]},
            payload, client_id, timeout_s, ensure_timeout_s,
        )
        if isinstance(result, Response):
            return result
        raise HTTPException(status_code=502, detail=result)

    backends = _tts_backends(cfg)
    if not backends:
        raise HTTPException(status_code=503, detail="no provider declares a TTS engine (tts_url)")

    for backend in backends:
        result = await _try_backend(backend, payload, client_id, timeout_s, ensure_timeout_s)
        if isinstance(result, Response):
            return result
        failures.append(result)
    logger.warning(
        "TTS: all providers failed for client '%s': %s",
        _clean_log(client_id, 64), _clean_log("; ".join(failures)),
    )
    raise HTTPException(status_code=502, detail="; ".join(failures))


async def _try_cloud_backend(
    route: dict[str, Any],
    body: dict[str, Any],
    response_format: str,
    client_id: str,
    timeout_s: float,
) -> Response | str:
    """Forward the request to a cloud OpenAI-compatible ``/audio/speech``
    endpoint. Returns a Response on success or a failure string for the
    failover log — the local engines still get a chance afterwards. The
    forwarded payload is the canonical OpenAI shape; the local engine's clone
    passthroughs and ``instruct`` do not travel to cloud providers."""
    name = route["provider"]
    started = time.monotonic()
    if route.get("speech_adapter") == "fish":
        return await _try_fish_tts(route, body, response_format, client_id, timeout_s)
    payload: dict[str, Any] = {
        "model": route["upstream_model"],
        "input": body["input"],
        "response_format": response_format,
    }
    if str(body.get("voice") or "").strip():
        payload["voice"] = str(body["voice"])
    speed = body.get("speed")
    if speed is not None:
        payload["speed"] = speed
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{route['base_url']}/audio/speech",
                json=payload,
                headers={"Authorization": f"Bearer {route['api_key']}"},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "TTS cloud provider '%s' unreachable: %s", _clean_log(name), _clean_log(exc)
        )
        # Client detail is structured-only: the exception text may carry
        # internal hosts/proxy details — the guarded log line keeps the full
        # message for the operator.
        return f"{name}: unreachable ({type(exc).__name__})"
    if resp.status_code != 200:
        logger.warning(
            "TTS cloud provider '%s' HTTP %s: %s",
            _clean_log(name), resp.status_code, _clean_log(resp.text),
        )
        return f"{name}: cloud HTTP {resp.status_code}"
    duration_ms = (time.monotonic() - started) * 1000
    logger.info(
        "🔊 TTS: client '%s' served by cloud '%s' (%s) -> %s bytes %s in %.1fs",
        _clean_log(client_id, 64), _clean_log(name, 64), _clean_log(route["upstream_model"], 64),
        len(resp.content), resp.headers.get("content-type", "audio/wav"), duration_ms / 1000,
    )
    return Response(
        content=resp.content,
        status_code=200,
        media_type=resp.headers.get("content-type", "audio/wav"),
    )


async def _try_fish_tts(
    route: dict[str, Any],
    body: dict[str, Any],
    response_format: str,
    client_id: str,
    timeout_s: float,
) -> Response | str:
    """Fish Audio native TTS (POST /v1/tts, JSON): ``text`` required,
    ``reference_id`` selects the voice/model (the client's ``voice`` field, or
    the route's upstream id when no voice is given), ``format`` = the
    response_format (fish: wav/pcm/mp3/opus). Response: raw audio bytes — the
    same passthrough shape as the OpenAI-compatible path."""
    name = route["provider"]
    started = time.monotonic()
    voice = str(body.get("voice") or "").strip() or route["upstream_model"]
    payload: dict[str, Any] = {
        "text": str(body.get("input", "")),
        "reference_id": voice,
        "format": response_format,
    }
    speed = body.get("speed")
    if speed is not None:
        payload["speed"] = speed
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{route['base_url']}/v1/tts",
                json=payload,
                headers={"Authorization": f"Bearer {route['api_key']}"},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "TTS cloud provider '%s' unreachable: %s", _clean_log(name), _clean_log(exc)
        )
        return f"{name}: unreachable ({type(exc).__name__})"
    if resp.status_code != 200:
        logger.warning(
            "TTS cloud provider '%s' HTTP %s: %s",
            _clean_log(name), resp.status_code, _clean_log(resp.text),
        )
        return f"{name}: cloud HTTP {resp.status_code}"
    duration_ms = (time.monotonic() - started) * 1000
    logger.info(
        "🔊 TTS: client '%s' served by cloud '%s' (%s, fish) -> %s bytes %s in %.1fs",
        _clean_log(client_id, 64), _clean_log(name, 64), _clean_log(route["upstream_model"], 64),
        len(resp.content), resp.headers.get("content-type", "audio/mpeg"), duration_ms / 1000,
    )
    return Response(
        content=resp.content,
        status_code=200,
        media_type=resp.headers.get("content-type", "audio/mpeg"),
    )


async def _try_backend(
    backend: dict[str, str],
    payload: dict[str, Any],
    client_id: str,
    timeout_s: float,
    ensure_timeout_s: float = 300.0,
) -> Response | str:
    """ensure -> forward on one provider backend; returns a Response on success
    or a failure string for the failover log."""
    name, tts_url, mgmt_url = backend["name"], backend["tts_url"], backend["management_url"]
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {backend['key']}"} if backend["key"] else {}
    try:
        async with httpx.AsyncClient(timeout=ensure_timeout_s) as client:
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
            return f"{name}: ensure failed (HTTP {ensure_resp.status_code}): {_clean_log(reason)}"
        logger.info("🔊 TTS ensure via %s: %s", name, ensure_body.get("already_running", "?"))

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{tts_url}/tts", json=payload, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "TTS engine '%s' HTTP %s: %s", _clean_log(name), resp.status_code, _clean_log(resp.text)
            )
            return f"{name}: engine HTTP {resp.status_code}"
        duration_ms = (time.monotonic() - started) * 1000
        logger.info(
            "🔊 TTS: client '%s' served by '%s' -> %s bytes audio/wav in %.1fs (instruct: %r)",
            client_id, name, len(resp.content), duration_ms / 1000, payload["instruct"][:60],
        )
        return Response(content=resp.content, status_code=200, media_type="audio/wav")
    except httpx.HTTPError as exc:
        logger.warning("TTS provider '%s' unreachable: %s", _clean_log(name), _clean_log(exc))
        return f"{name}: unreachable ({type(exc).__name__})"
