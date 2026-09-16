"""TTS routing pins — provider-driven, platform-agnostic speech routing.

Guardian makes no cloud/local distinction: a TTS backend is a provider
capability (tts_url + management_url + management_key in the provider file);
``tts.providers`` gives the failover order.  The caretaker on EVERY host runs
the same on-demand lifecycle (spawn/idle-stop).
"""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.gateway import tts as tts_mod


def _provider_docs():
    return {
        "providers": {
            "ai-node-local": {
                "management_url": "http://127.0.0.1:11441",
                "management_key": "${CARETAKER_KEY}",
                "tts_url": "http://127.0.0.1:11450",
            },
            "windows-gpu-local": {
                "management_url": "http://192.168.1.W:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "tts_url": "http://192.168.1.W:11450",
            },
            "openrouter": {"api_key": "sk-x"},  # no TTS capability — skipped
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 120,
        "ensure_timeout_seconds": 300,
        "default_instruct": "",
        "providers": ["ai-node-local", "windows-gpu-local"],
    }
    base.update(overrides)
    return base


def _patch_config(monkeypatch, cfg=None, docs=None):
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: cfg or _cfg())
    monkeypatch.setattr(tts_mod, "CONFIG", docs or _provider_docs())
    monkeypatch.setenv("CARETAKER_KEY", "ctk_local")
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _patch_client(monkeypatch, handler):
    """handler(url, json=None, headers=None) -> httpx.Response; records calls."""
    calls = []

    async def post(url, json=None, headers=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return handler(url, json, headers)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            calls.append({"client_timeout": kwargs.get("timeout")})

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(tts_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


# ---------------------------------------------------------------------------
# Mapping (pure functions — the OpenAI → engine contract)
# ---------------------------------------------------------------------------


def test_mapping_voice_stock_names_map_to_instruct():
    payload = tts_mod._build_engine_payload({"input": "Hello", "voice": "nova"}, _cfg())
    assert payload["text"] == "Hello"
    assert payload["instruct"] == "bright, friendly female voice"


def test_mapping_unknown_voice_passes_through_verbatim():
    payload = tts_mod._build_engine_payload({"input": "Hallo", "voice": "zachte stem"}, _cfg())
    assert payload["instruct"] == "zachte stem"


def test_mapping_explicit_instruct_wins_and_extras_passthrough():
    payload = tts_mod._build_engine_payload(
        {"input": "x", "voice": "alloy", "instruct": "boze stem", "seed": 42, "temperature": 0.9},
        _cfg(),
    )
    assert payload["instruct"] == "boze stem"
    assert payload["seed"] == 42
    assert payload["temperature"] == 0.9


# ---------------------------------------------------------------------------
# Backend resolution (provider-driven, no cloud/local distinction)
# ---------------------------------------------------------------------------


def test_backends_resolve_in_configured_order(monkeypatch):
    _patch_config(monkeypatch)
    backends = tts_mod._tts_backends(_cfg())
    assert [b["name"] for b in backends] == ["ai-node-local", "windows-gpu-local"]
    assert backends[0]["tts_url"] == "http://127.0.0.1:11450"
    assert backends[0]["key"] == "ctk_local"
    assert backends[1]["key"] == "ctk_windows"


def test_provider_without_tts_capability_is_skipped(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(providers=["openrouter", "windows-gpu-local"]))
    backends = tts_mod._tts_backends(_cfg(providers=["openrouter", "windows-gpu-local"]))
    assert [b["name"] for b in backends] == ["windows-gpu-local"]


# ---------------------------------------------------------------------------
# Handler behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_primary_provider_and_expanded_keys(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, headers):
        if url.endswith("/tts/ensure"):
            assert headers["Authorization"] == ("Bearer ctk_local" if "127.0.0.1" in url else "Bearer ctk_windows")
            return httpx.Response(200, json={"ok": True, "already_running": True})
        return httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "Hallo", "voice": "nova"}), "dsh")
    assert resp.status_code == 200
    assert resp.media_type == "audio/wav"
    assert [c["url"] for c in calls if "url" in c] == [
        "http://127.0.0.1:11441/tts/ensure",
        "http://127.0.0.1:11450/tts",
    ]


@pytest.mark.asyncio
async def test_primary_failure_falls_over_to_second_provider(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, headers):
        if url.endswith("/tts/ensure"):
            if "127.0.0.1" in url:
                return httpx.Response(200, json={"ok": False, "reason": "engine exited during startup"})
            return httpx.Response(200, json={"ok": True, "already_running": True})
        return httpx.Response(200, content=b"RIFFwav-windows", headers={"content-type": "audio/wav"})

    _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"RIFFwav-windows"


@pytest.mark.asyncio
async def test_all_providers_failed_returns_502(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, headers):
        raise httpx.ConnectError("down")

    _patch_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 502
    assert "ai-node-local" in str(excinfo.value.detail) and "windows-gpu-local" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_disabled_config_returns_404(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(enabled=False))
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_no_tts_capable_provider_returns_503(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(providers=["openrouter"]))
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_non_wav_response_format_rejected(monkeypatch):
    _patch_config(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(
            _FakeRequest({"input": "x", "response_format": "mp3"}), "dsh"
        )
    assert excinfo.value.status_code == 400
    assert "wav" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_empty_input_rejected(monkeypatch):
    _patch_config(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest({"input": "  "}), "dsh")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_engine_error_status_falls_through(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, headers):
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True, "already_running": True})
        if "127.0.0.1" in url:
            return httpx.Response(500, content=b'{"error":"boom"}')
        return httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"})

    _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ensure_timeout_is_configurable_and_above_cold_start(monkeypatch):
    """The ensure client timeout must honor ensure_timeout_seconds — a cold
    model load blocks the caretaker's /tts/ensure for minutes; a hardcoded
    short timeout would silently fail over a healthy primary."""
    _patch_config(monkeypatch)

    def handler(url, json, headers):
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True, "already_running": True})
        return httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"})

    calls = _patch_client(monkeypatch, handler)
    await tts_mod.handle_audio_speech(_FakeRequest({"input": "x"}), "dsh")
    timeouts = [c["client_timeout"] for c in calls if "client_timeout" in c]
    assert 300.0 in timeouts
