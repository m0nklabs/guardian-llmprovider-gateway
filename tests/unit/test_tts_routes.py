"""Speech route pins — route-oriented TTS resolution (no opt-in switches).

A speech ``model`` address ``[guardian/]{provider}/{brand}/{model}`` resolves
through the provider file alone (app/gateway/speech_routing.py): ``tts_url``
-> the local engine, ``base_url`` + ``api_key`` -> the OpenAI-compatible
``/audio/speech`` endpoint (upstream id = ``{brand}/{model}``). An explicit
route is EXACT: failures surface honestly and never fall back to a different
provider. Without an address the default local failover chain serves. The
forwarded payload is the canonical OpenAI shape (clone passthroughs and
``instruct`` do not travel to cloud providers).
"""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace
from urllib.parse import urlparse

from app.gateway import speech_routing as sr
from app.gateway import tts as tts_mod


def _is_cloud_host(url: str) -> bool:
    """Exact-host match against the test fixture (CodeQL-safe)."""
    return urlparse(url).hostname == "tts.cloudtest.invalid"


def _provider_docs():
    return {
        "providers": {
            "windows-gpu-local": {
                "management_url": "http://192.168.1.W:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "tts_url": "http://192.168.1.W:11450",
                "stt_url": "http://192.168.1.W:11451",
            },
            "cloudtts": {
                "base_url": "https://tts.cloudtest.invalid/openai/v1",
                "api_key": "${CLOUD_TTS_API_KEY}",
            },
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 120,
        "ensure_timeout_seconds": 300,
        "providers": ["windows-gpu-local"],
    }
    base.update(overrides)
    return base


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _body(**overrides):
    base = {
        "model": "guardian/cloudtts/canopylabs/orpheus-v1-english",
        "input": "Believe me, this is a tremendous test.",
        "response_format": "wav",
    }
    base.update(overrides)
    return base


def _patch(monkeypatch, cfg=None, docs=None):
    docs = docs or _provider_docs()
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: cfg or _cfg())
    monkeypatch.setattr(tts_mod, "CONFIG", docs)
    monkeypatch.setattr(sr, "CONFIG", docs)
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")
    monkeypatch.setenv("CLOUD_TTS_API_KEY", "ctts_test")


def _patch_client(monkeypatch, handler):
    calls = []

    async def post(url, json=None, content=None, params=None, headers=None, files=None, data=None):
        calls.append({"url": url, "json": json, "content": content, "params": params,
                      "headers": headers, "files": files, "data": data})
        return handler(url, json, content, params, headers, files, data)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(tts_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


# --- pure resolution ---


def test_resolve_local_and_cloud_from_provider_file_alone(monkeypatch):
    _patch(monkeypatch)
    local = sr.resolve_speech_route("guardian/windows-gpu-local/qwen/qwen3-tts", "tts_url")
    assert local["kind"] == "local"
    assert local["tts_url"] == "http://192.168.1.W:11450"
    assert local["upstream_model"] == "qwen/qwen3-tts"
    cloud = sr.resolve_speech_route("cloudtts/canopylabs/orpheus-v1-english", "tts_url")
    assert cloud["kind"] == "cloud"
    assert cloud["upstream_model"] == "canopylabs/orpheus-v1-english"  # brand/model, not the last segment
    assert sr.resolve_speech_route("guardian/nosuchprovider/brand/model", "tts_url") is None


# --- handler: explicit routes are EXACT ---


@pytest.mark.asyncio
async def test_cloud_route_forwards_canonical_payload(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, content=b"audio-bytes", headers={"content-type": "audio/wav"})))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(
        voice="alloy", ref_audio="trump.wav", zero_shot=True)), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"audio-bytes"
    cloud = [c for c in calls if _is_cloud_host(c["url"])][0]
    # canonical OpenAI shape: the upstream id is brand/model, clone passthroughs stay local
    assert cloud["json"] == {
        "model": "canopylabs/orpheus-v1-english",
        "input": "Believe me, this is a tremendous test.",
        "response_format": "wav",
        "voice": "alloy",
    }
    assert "instruct" not in cloud["json"]
    assert cloud["headers"]["Authorization"] == "Bearer ctts_test"


@pytest.mark.asyncio
async def test_cloud_route_failure_is_exact_no_local_fallback(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(503, content=b"rate limited") if _is_cloud_host(url)
        else httpx.Response(200, content=b"local-bytes")))
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest(_body()), "dsh")
    assert excinfo.value.status_code == 502
    assert "cloudtts" in str(excinfo.value.detail)
    # EXACT: the local engine was never asked
    assert not any(c["url"].endswith("/tts/ensure") for c in calls)
    assert all(_is_cloud_host(c["url"]) for c in calls if c["url"].endswith("/audio/speech"))


@pytest.mark.asyncio
async def test_unknown_provider_returns_404_model_not_served(monkeypatch):
    _patch(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"x"))
    for bad in ("guardian/nosuch/brand/model", "guardian/nosuch/whisper-large-v3"):
        with pytest.raises(HTTPException) as excinfo:
            await tts_mod.handle_audio_speech(_FakeRequest(_body(model=bad)), "dsh")
        assert excinfo.value.status_code == 404
        assert "model_not_served" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_explicit_local_route_ensures_and_forwards(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, json={"ok": True, "already_running": True}) if url.endswith("/tts/ensure")
        else httpx.Response(200, content=b"local-wav")))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(
        model="guardian/windows-gpu-local/qwen/qwen3-tts", voice="alloy")), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"local-wav"
    assert [c["url"] for c in calls if "url" in c] == [
        "http://192.168.1.W:11441/tts/ensure",
        "http://192.168.1.W:11450/tts",
    ]
    local_payload = [c for c in calls if c["url"].endswith("/tts")][0]["json"]
    # voice->instruct mapping applies on the local engine (alloy is a stock voice)
    assert local_payload["instruct"] == tts_mod._STOCK_VOICE_INSTRUCT["alloy"]


@pytest.mark.asyncio
async def test_no_model_keeps_default_failover_chain(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, json={"ok": True}) if url.endswith("/tts/ensure")
        else httpx.Response(200, content=b"local-wav")))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(model=None)), "dsh")
    assert resp.status_code == 200
    assert [c["url"] for c in calls if "url" in c] == [
        "http://192.168.1.W:11441/tts/ensure",
        "http://192.168.1.W:11450/tts",
    ]


@pytest.mark.asyncio
async def test_cloud_routed_request_allows_upstream_formats(monkeypatch):
    """The wav/pcm restriction is a property of the LOCAL engine — a routed
    request passes response_format verbatim (mp3 etc.)."""
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, content=b"mp3data", headers={"content-type": "audio/mpeg"})))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(response_format="mp3")), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"mp3data"
    assert resp.media_type == "audio/mpeg"
    cloud = [c for c in calls if _is_cloud_host(c["url"])][0]
    assert cloud["json"]["response_format"] == "mp3"


@pytest.mark.asyncio
async def test_local_routed_request_keeps_format_400(monkeypatch):
    """Without an opted-in cloud target the wav/pcm restriction still applies."""
    _patch(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"wav"))
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest(_body(
            model="unknown-brand/brand/model", response_format="mp3")), "dsh")
    # unknown-brand is not an addressable provider -> 404 model_not_served (route contract)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_speed_validated_early_with_clear_400(monkeypatch):
    _patch(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"x"))
    for bad in ("fast", 10.0, True):
        with pytest.raises(HTTPException) as excinfo:
            await tts_mod.handle_audio_speech(_FakeRequest(_body(speed=bad)), "dsh")
        assert excinfo.value.status_code == 400
        assert "speed" in str(excinfo.value.detail)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, content=b"x", headers={"content-type": "audio/wav"})))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(speed=1.5)), "dsh")
    assert resp.status_code == 200
    cloud = [c for c in calls if _is_cloud_host(c["url"])][0]
    assert cloud["json"]["speed"] == 1.5


@pytest.mark.asyncio
async def test_explicit_local_route_keeps_format_400(monkeypatch):
    """The wav/pcm 400 binds explicit LOCAL addresses too — the engine serves
    wav only and silently ignoring a requested mp3 would be worse than a clear
    400. Only cloud routes get format verbatim."""
    _patch(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"wav"))
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest(_body(
            model="guardian/windows-gpu-local/qwen/qwen3-tts", response_format="mp3")), "dsh")
    assert excinfo.value.status_code == 400
    assert "response_format" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_fish_adapter_translates_tts_dialect(monkeypatch):
    """fish.audio is not OpenAI-shaped: the client's OpenAI speech request is
    translated to POST /v1/tts (text/reference_id/format) and the audio bytes
    pass through unchanged. voice wins over the route's upstream id."""
    docs = _provider_docs()
    docs["providers"]["fishtts"] = {
        "base_url": "https://tts.cloudtest.invalid",
        "api_key": "${CLOUD_TTS_API_KEY}",
        "speech_adapter": "fish",
    }
    _patch(monkeypatch, docs=docs)

    def handler(url, j, c, p, h, f=None, d=None):
        assert url.endswith("/v1/tts")
        return httpx.Response(200, content=b"fishaudio", headers={"content-type": "audio/mpeg"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(
        model="fishtts/s2.1-pro-free", voice="alloy", response_format="wav")), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"fishaudio"
    call = calls[0]
    assert call["json"] == {
        "text": "Believe me, this is a tremendous test.",
        "reference_id": "alloy",           # client voice -> fish reference_id
        "format": "wav",                    # verbatim (fish supports wav)
    }
    # fish selects the ENGINE via the model header: the route's upstream id
    assert call["headers"]["model"] == "s2.1-pro-free"
    assert call["headers"]["Authorization"] == "Bearer ctts_test"


@pytest.mark.asyncio
async def test_fish_adapter_uses_route_id_when_no_voice(monkeypatch):
    """Without a voice field the route's upstream id IS the fish reference."""
    docs = _provider_docs()
    docs["providers"]["fishtts"] = {
        "base_url": "https://tts.cloudtest.invalid",
        "api_key": "${CLOUD_TTS_API_KEY}",
        "speech_adapter": "fish",
    }
    _patch(monkeypatch, docs=docs)

    def handler(url, j, c, p, h, f=None, d=None):
        return httpx.Response(200, content=b"a", headers={"content-type": "audio/mpeg"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(
        model="fishtts/s2.1-pro-free")), "dsh")
    assert resp.status_code == 200
    # no voice -> no reference_id (the model header carries the route id)
    assert "reference_id" not in calls[0]["json"]
    assert calls[0]["headers"]["model"] == "s2.1-pro-free"


@pytest.mark.asyncio
async def test_fish_upstream_content_type_sanitized(monkeypatch):
    """The upstream content-type reaches the log and the client response
    header — control characters from a hostile upstream must not survive."""
    docs = _provider_docs()
    docs["providers"]["fishtts"] = {
        "base_url": "https://tts.cloudtest.invalid",
        "api_key": "${CLOUD_TTS_API_KEY}",
        "speech_adapter": "fish",
    }
    _patch(monkeypatch, docs=docs)
    # httpx rejects raw CRLF headers itself; the sanitizer is the second
    # layer — pin it directly on a hostile token value.
    assert tts_mod._clean_log("audio/mpeg\r\nX-Injected: yes", 64) == "audio/mpeg X-Injected: yes"
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(
        200, content=b"x", headers={"content-type": "audio/mpeg; boundary=weird"}))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(
        model="fishtts/voice-1", voice="alloy")), "dsh")
    assert resp.status_code == 200
    assert resp.media_type == "audio/mpeg; boundary=weird"
