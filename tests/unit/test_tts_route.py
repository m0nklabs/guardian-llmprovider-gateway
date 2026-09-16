"""TTS routing pins (F6 extension): /v1/audio/speech → qwen3tts-http engine.

The engine contract is NOT OpenAI-shaped (own POST /tts → audio/wav): the
route maps input→text, voice→instruct (stock-voice map), passes seed /
sub_seed / temperature through, and enforces wav-only output.
"""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.gateway import tts as tts_mod


def _cfg(**overrides):
    base = {
        "enabled": True,
        "backend_url": "http://192.168.1.245:11450",
        "timeout_seconds": 120,
        "default_instruct": "",
    }
    base.update(overrides)
    return base


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _patch_cfg(monkeypatch, **overrides):
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: _cfg(**overrides))


def _patch_client(monkeypatch, responder):
    """Patch httpx.AsyncClient in the tts module to return a fake transport."""
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=responder)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(tts_mod.httpx, "AsyncClient", _FakeAsyncClient)


# ---------------------------------------------------------------------------
# Mapping (pure functions — the OpenAI → engine contract)
# ---------------------------------------------------------------------------


def test_mapping_voice_stock_names_map_to_instruct():
    cfg = _cfg()
    payload = tts_mod._build_engine_payload(
        {"input": "Hello", "voice": "nova"}, cfg
    )
    assert payload["text"] == "Hello"
    assert payload["instruct"] == "bright, friendly female voice"


def test_mapping_unknown_voice_passes_through_verbatim():
    payload = tts_mod._build_engine_payload(
        {"input": "Hallo", "voice": "zachte vrouwelijke stem, blij"}, _cfg()
    )
    assert payload["instruct"] == "zachte vrouwelijke stem, blij"


def test_mapping_explicit_instruct_wins_over_voice():
    payload = tts_mod._build_engine_payload(
        {"input": "x", "voice": "alloy", "instruct": "boze stem"}, _cfg()
    )
    assert payload["instruct"] == "boze stem"


def test_mapping_default_instruct_fallback():
    payload = tts_mod._build_engine_payload(
        {"input": "x"}, _cfg(default_instruct="neutral voice")
    )
    assert payload["instruct"] == "neutral voice"


def test_mapping_engine_extras_passthrough():
    payload = tts_mod._build_engine_payload(
        {"input": "x", "seed": 42, "sub_seed": 7, "temperature": 0.9}, _cfg()
    )
    assert payload["seed"] == 42
    assert payload["sub_seed"] == 7
    assert payload["temperature"] == 0.9


# ---------------------------------------------------------------------------
# Handler behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_returns_audio_wav_passthrough(monkeypatch):
    _patch_cfg(monkeypatch)
    seen = {}

    async def responder(url, json=None):
        seen["url"] = url
        seen["payload"] = json
        return httpx.Response(200, content=b"RIFFwavdata", headers={"content-type": "audio/wav"})

    _patch_client(monkeypatch, responder)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "Hallo", "voice": "nova"}), "dsh")
    assert resp.status_code == 200
    assert resp.media_type == "audio/wav"
    assert resp.body == b"RIFFwavdata"
    assert seen["url"] == "http://192.168.1.245:11450/tts"
    assert seen["payload"]["instruct"] == "bright, friendly female voice"


@pytest.mark.asyncio
async def test_disabled_config_returns_404(monkeypatch):
    _patch_cfg(monkeypatch, enabled=False)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_non_wav_response_format_rejected(monkeypatch):
    _patch_cfg(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(
            _FakeRequest({"input": "x", "response_format": "mp3"}), "dsh"
        )
    assert excinfo.value.status_code == 400
    assert "wav" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_empty_input_rejected(monkeypatch):
    _patch_cfg(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "  "}), "dsh")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_engine_unreachable_returns_502(monkeypatch):
    _patch_cfg(monkeypatch)

    async def responder(url, json=None):
        raise httpx.ConnectError("connection refused")

    _patch_client(monkeypatch, responder)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_engine_error_status_passthrough(monkeypatch):
    _patch_cfg(monkeypatch)

    async def responder(url, json=None):
        return httpx.Response(422, content=b'{"detail":"bad"}')

    _patch_client(monkeypatch, responder)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# On-demand lifecycle (ensure via the Windows caretaker before every forward)
# ---------------------------------------------------------------------------


def _patch_routing_client(monkeypatch, handler):
    """handler(url, json=None, headers=None) -> httpx.Response; records calls."""
    calls = []

    async def post(url, json=None, headers=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return handler(url, json, headers)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(tts_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


def _cfg_ondemand(**overrides):
    base = _cfg(
        ondemand_enabled=True,
        caretaker_url="http://192.168.1.245:11441",
        caretaker_key="ctk_test",
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_ensure_called_before_engine_forward(monkeypatch):
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: _cfg_ondemand())

    def handler(url, json, headers):
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True, "already_running": False})
        return httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"})

    calls = _patch_routing_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "Hallo"}), "dsh")
    assert resp.status_code == 200
    assert [c["url"] for c in calls] == [
        "http://192.168.1.245:11441/tts/ensure",
        "http://192.168.1.245:11450/tts",
    ]
    assert calls[0]["headers"]["Authorization"] == "Bearer ctk_test"


@pytest.mark.asyncio
async def test_ensure_unreachable_returns_502(monkeypatch):
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: _cfg_ondemand())

    def handler(url, json=None, headers=None):
        raise httpx.ConnectError("down")

    _patch_routing_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 502
    assert "caretaker" in str(excinfo.value.detail).lower()


@pytest.mark.asyncio
async def test_ensure_engine_wont_start_returns_502(monkeypatch):
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: _cfg_ondemand())

    def handler(url, json, headers):
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": False, "reason": "engine not healthy within 90s"})
        return httpx.Response(200, content=b"RIFFwav")

    _patch_routing_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 502
    assert "could not be started" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_ondemand_disabled_skips_ensure(monkeypatch):
    monkeypatch.setattr(
        tts_mod, "load_tts_config", lambda: _cfg_ondemand(ondemand_enabled=False)
    )

    def handler(url, json, headers):
        return httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"})

    calls = _patch_routing_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert resp.status_code == 200
    assert [c["url"] for c in calls] == ["http://192.168.1.245:11450/tts"]
