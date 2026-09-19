"""STT routing pins — provider-driven transcription failover (mirrors the TTS
route pins).  A host participates when its provider file declares stt_url +
management_url; stt.providers is the failover order; language maps ISO→Qwen."""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.gateway import stt as stt_mod


def _provider_docs():
    return {
        "providers": {
            "14700k-local": {
                "management_url": "http://192.168.1.245:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "stt_url": "http://192.168.1.245:11451",
            },
            "ai-kvm2-local": {
                "management_url": "http://127.0.0.1:11441",
                "management_key": "${CARETAKER_KEY}",
                # no stt_url: ai-kvm2 runs no ASR engine — must be skipped
            },
            "openrouter": {"api_key": "sk-x"},
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 300,
        "ensure_timeout_seconds": 420,
        "providers": ["14700k-local", "ai-kvm2-local"],
    }
    base.update(overrides)
    return base


class _Upload:
    def __init__(self, data=b"RIFFaudio", content_type="audio/wav"):
        self._data = data
        self.content_type = content_type

    async def read(self):
        return self._data


def _form(fields):
    async def _form():
        return fields
    return _form


def _fake_request(fields=None):
    req = SimpleNamespace()
    if fields is None:
        async def _bad():
            raise ValueError("not multipart")
        req.form = _bad
    else:
        req.form = _form(fields)
    return req


def _patch_config(monkeypatch, cfg=None, docs=None):
    monkeypatch.setattr(stt_mod, "load_stt_config", lambda: cfg or _cfg())
    monkeypatch.setattr(stt_mod, "CONFIG", docs or _provider_docs())
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")


def _patch_client(monkeypatch, handler):
    calls = []

    async def post(url, json=None, content=None, params=None, headers=None):
        calls.append({"url": url, "content": content, "params": params, "headers": headers})
        return handler(url, json, content, params, headers)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(stt_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


# --- mapping ---


def test_iso_language_maps_to_qwen_name():
    assert stt_mod._qwen_language("nl") == "Dutch"
    assert stt_mod._qwen_language("EN") == "English"
    assert stt_mod._qwen_language(None) is None
    assert stt_mod._qwen_language("fy") == "Fy"  # unknown code passes capitalized


def test_backends_skip_hosts_without_stt_url(monkeypatch):
    _patch_config(monkeypatch)
    backends = stt_mod._stt_backends(_cfg())
    assert [b["name"] for b in backends] == ["14700k-local"]
    assert backends[0]["key"] == "ctk_windows"


# --- handler ---


@pytest.mark.asyncio
async def test_success_forwards_raw_audio_and_language(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        if url.endswith("/stt/ensure"):
            assert headers["Authorization"] == "Bearer ctk_windows"
            return httpx.Response(200, json={"ok": True, "cold_start": True})
        return httpx.Response(200, json={"text": "hallo", "language": "Dutch"})

    calls = _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _fake_request({"file": _Upload(b"RIFFaudio", "audio/wav"), "language": "nl"}), "dsh"
    )
    assert resp.status_code == 200
    engine_call = [c for c in calls if c["url"].endswith("/transcriptions")][0]
    assert engine_call["content"] == b"RIFFaudio"          # raw audio bytes
    assert engine_call["params"] == {"language": "Dutch"}  # mapped per request
    assert [c["url"] for c in calls if "url" in c] == [
        "http://192.168.1.245:11441/stt/ensure",
        "http://192.168.1.245:11451/transcriptions",
    ]


@pytest.mark.asyncio
async def test_engine_failure_returns_502_with_reason(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        if url.endswith("/stt/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500, content=b'{"error":"boom"}')

    _patch_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _fake_request({"file": _Upload()}), "dsh"
        )
    assert excinfo.value.status_code == 502
    assert "14700k-local" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_ensure_failure_returns_502(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        return httpx.Response(200, json={"ok": False, "reason": "insufficient VRAM free"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _fake_request({"file": _Upload()}), "dsh"
        )
    assert "insufficient VRAM" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_disabled_returns_404(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(enabled=False))
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _fake_request({"file": _Upload()}), "dsh"
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_missing_file_returns_400(monkeypatch):
    _patch_config(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _fake_request({"model": "qwen3-asr"}), "dsh"
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_no_stt_capable_provider_returns_503(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(providers=["openrouter"]))
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _fake_request({"file": _Upload()}), "dsh"
        )
    assert excinfo.value.status_code == 503
