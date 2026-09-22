"""Cloud STT forwarding pins — model-based provider routing.

Opt-in twice: global ``stt.cloud_forwarding.enabled`` + per-provider
``cloud_stt: true``. A requested model like ``groq/groq/whisper-large-v3``
forwards the multipart upload to the provider's OpenAI-compatible
``/audio/transcriptions``; the upstream model id is the final path segment and
``language`` passes through verbatim (ISO-639-1, no Qwen mapping). Any cloud
failure falls through to the local engine chain unchanged; with the switch off
(or no model field) behavior is byte-identical to the pre-forwarding route."""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.gateway import stt as stt_mod


def _provider_docs(**groq_overrides):
    groq = {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "${GROQ_API_KEY}",
        "cloud_stt": True,
    }
    groq.update(groq_overrides)
    return {
        "providers": {
            "groq": groq,
            "14700k-local": {
                "management_url": "http://192.168.1.245:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "stt_url": "http://192.168.1.245:11451",
            },
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 300,
        "ensure_timeout_seconds": 420,
        "providers": ["14700k-local"],
        "cloud_forwarding": {"enabled": True},
    }
    base.update(overrides)
    return base


class _Upload:
    def __init__(self, data=b"RIFFaudio", content_type="audio/wav", filename="clip.wav"):
        self._data = data
        self.content_type = content_type
        self.filename = filename

    async def read(self):
        return self._data


def _fake_request(fields=None):
    req = SimpleNamespace()

    async def _form():
        return fields or {}

    req.form = _form
    return req


def _patch_config(monkeypatch, cfg=None, docs=None):
    monkeypatch.setattr(stt_mod, "load_stt_config", lambda: cfg or _cfg())
    monkeypatch.setattr(stt_mod, "CONFIG", docs or _provider_docs())
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")


def _patch_client(monkeypatch, handler):
    calls = []

    async def post(url, json=None, content=None, params=None, headers=None, files=None, data=None):
        calls.append({
            "url": url, "content": content, "params": params,
            "headers": headers, "files": files, "data": data,
        })
        return handler(url, json, content, params, headers, files, data)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(stt_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


# --- target resolution ---


def test_target_strips_to_final_model_segment(monkeypatch):
    _patch_config(monkeypatch)
    target = stt_mod._cloud_stt_target("groq/groq/whisper-large-v3")
    assert target == {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk_test",
        "model": "whisper-large-v3",
    }


def test_target_rejects_non_matching_models(monkeypatch):
    _patch_config(monkeypatch)
    assert stt_mod._cloud_stt_target("whisper-large-v3") is None      # no namespace
    assert stt_mod._cloud_stt_target("") is None                      # empty
    assert stt_mod._cloud_stt_target("nvidia/openai/whisper-1") is None  # no cloud_stt flag


def test_target_rejects_provider_without_credentials(monkeypatch):
    _patch_config(monkeypatch, docs=_provider_docs(api_key=""))
    assert stt_mod._cloud_stt_target("groq/groq/whisper-large-v3") is None


# --- handler routing ---


@pytest.mark.asyncio
async def test_disabled_switch_keeps_local_only(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(cloud_forwarding={"enabled": False}))

    def handler(url, json, content, params, headers, files, data):
        if url.endswith("/stt/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"text": "lokaal"})

    calls = _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _fake_request({"file": _Upload(), "model": "groq/groq/whisper-large-v3"}), "dsh"
    )
    assert resp.status_code == 200
    assert not any(c["url"].startswith("https://api.groq.com") for c in calls)
    assert any(c["url"].endswith("/transcriptions") for c in calls)


@pytest.mark.asyncio
async def test_cloud_success_serves_request_without_local_engines(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers, files, data):
        assert url == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert headers["Authorization"] == "Bearer gsk_test"
        return httpx.Response(200, json={"text": "cloud transcript"})

    calls = _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _fake_request({
            "file": _Upload(b"RIFFaudio", "audio/wav", "clip.wav"),
            "model": "groq/groq/whisper-large-v3",
            "language": "nl",
        }), "dsh"
    )
    assert resp.status_code == 200
    assert b"cloud transcript" in resp.body
    cloud_calls = [c for c in calls if c["url"].startswith("https://api.groq.com")]
    assert len(cloud_calls) == 1
    part = cloud_calls[0]["files"]["file"]
    assert part[0] == "clip.wav" and part[1] == b"RIFFaudio" and part[2] == "audio/wav"
    assert cloud_calls[0]["data"] == {"model": "whisper-large-v3", "language": "nl"}
    assert not any(c["url"].endswith("/stt/ensure") for c in calls)  # local untouched


@pytest.mark.asyncio
async def test_cloud_failure_falls_through_to_local_engine(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers, files, data):
        if url.startswith("https://api.groq.com"):
            return httpx.Response(503, content=b"rate limited")
        if url.endswith("/stt/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"text": "lokaal"})

    calls = _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _fake_request({"file": _Upload(), "model": "groq/groq/whisper-large-v3"}), "dsh"
    )
    assert resp.status_code == 200
    assert b"lokaal" in resp.body
    assert any(c["url"].endswith("/stt/ensure") for c in calls)


@pytest.mark.asyncio
async def test_no_matching_cloud_model_keeps_local_flow(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers, files, data):
        if url.endswith("/stt/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"text": "lokaal"})

    calls = _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _fake_request({"file": _Upload(), "model": "qwen3-asr"}), "dsh"
    )
    assert resp.status_code == 200
    assert not any(c["url"].startswith("https://api.groq.com") for c in calls)


@pytest.mark.asyncio
async def test_cloud_unreachable_falls_through(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers, files, data):
        if url.startswith("https://api.groq.com"):
            raise httpx.ConnectError("DNS fail")
        if url.endswith("/stt/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"text": "lokaal"})

    _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _fake_request({"file": _Upload(), "model": "groq/groq/whisper-large-v3"}), "dsh"
    )
    assert resp.status_code == 200
    assert b"lokaal" in resp.body
