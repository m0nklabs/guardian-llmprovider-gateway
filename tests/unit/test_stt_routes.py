"""Speech route pins — route-oriented STT resolution (no opt-in switches).

A speech ``model`` address ``[guardian/]{provider}/{brand}/{model}`` resolves
through the provider file alone (app/gateway/speech_routing.py): ``stt_url``
-> the local engine, ``base_url`` + ``api_key`` -> the OpenAI-compatible cloud
endpoint. An explicit route is EXACT: failures surface honestly and never
fall back to a different provider. Without an address the default local
failover chain (``stt.providers``) serves.
"""

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace
from urllib.parse import urlparse

from app.gateway import speech_routing as sr
from app.gateway import stt as stt_mod


def _is_cloud_host(url: str) -> bool:
    """Exact-host match against the test fixture (CodeQL-safe: an
    attacker-shaped sibling host does not match)."""
    return urlparse(url).hostname == "stt.cloudtest.invalid"


def _provider_docs():
    return {
        "providers": {
            "windows-gpu-local": {
                "management_url": "http://192.168.1.W:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "stt_url": "http://192.168.1.W:11451",
                "tts_url": "http://192.168.1.W:11450",
            },
            "cloudstt": {
                "base_url": "https://stt.cloudtest.invalid/openai/v1",
                "api_key": "${CLOUD_STT_API_KEY}",
            },
            "openrouter": {
                "base_url": "https://stt.cloudtest.invalid/openai/v1",
                "api_key": "${CLOUD_STT_API_KEY}",
            },
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 300,
        "ensure_timeout_seconds": 420,
        "providers": ["windows-gpu-local"],
    }
    base.update(overrides)
    return base


class _Upload:
    def __init__(self, data=b"RIFFaudio", content_type="audio/wav"):
        self._data = data
        self.content_type = content_type

    async def read(self):
        return self._data


class _FakeRequest:
    def __init__(self, fields):
        self._fields = fields

    async def form(self):
        return self._fields


def _patch(monkeypatch, cfg=None, docs=None):
    docs = docs or _provider_docs()
    monkeypatch.setattr(stt_mod, "load_stt_config", lambda: cfg or _cfg())
    monkeypatch.setattr(stt_mod, "CONFIG", docs)
    monkeypatch.setattr(sr, "CONFIG", docs)
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")
    monkeypatch.setenv("CLOUD_STT_API_KEY", "stt_test_key")


def _patch_client(monkeypatch, handler):
    calls = []

    async def post(url, json=None, content=None, params=None, headers=None, files=None, data=None):
        calls.append({"url": url, "content": content, "params": params,
                      "headers": headers, "files": files, "data": data})
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


# --- pure resolution ---


def test_parse_accepts_guardian_prefix_and_bare_form():
    parse_speech_model = sr.parse_speech_model
    # the upstream id is everything after the provider segment, verbatim
    assert parse_speech_model("guardian/windows-gpu-local/qwen/qwen3-asr-sherpa") == ("windows-gpu-local", "qwen/qwen3-asr-sherpa")
    assert parse_speech_model("guardian/groq/whisper-large-v3") == ("groq", "whisper-large-v3")
    assert parse_speech_model("groq/canopylabs/orpheus-v1-english") == ("groq", "canopylabs/orpheus-v1-english")
    assert parse_speech_model("guardian/openrouter/x-ai/grok-stt-1.0") == ("openrouter", "x-ai/grok-stt-1.0")
    assert parse_speech_model("qwen3-asr") is None  # a bare name is not an address
    assert parse_speech_model("") is None


def test_resolve_local_and_cloud_from_provider_file_alone(monkeypatch):
    _patch(monkeypatch)
    resolve_speech_route = sr.resolve_speech_route
    local = resolve_speech_route("guardian/windows-gpu-local/qwen/qwen3-asr-sherpa", "stt_url")
    assert local["kind"] == "local"
    assert local["stt_url"] == "http://192.168.1.W:11451"
    assert local["upstream_model"] == "qwen/qwen3-asr-sherpa"
    cloud = resolve_speech_route("guardian/cloudstt/brand/model-x", "stt_url")
    assert cloud["kind"] == "cloud"
    assert cloud["upstream_model"] == "brand/model-x"
    assert resolve_speech_route("guardian/nosuchprovider/brand/model", "stt_url") is None


# --- handler: explicit routes are EXACT ---


@pytest.mark.asyncio
async def test_cloud_route_forwards_brand_model_id_and_language(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, json={"text": "cloud"}))
    resp = await stt_mod.handle_audio_transcriptions(
        _FakeRequest({"file": _Upload(b"RIFF", "audio/wav"), "language": "nl",
                      "model": "guardian/cloudstt/groq/whisper-large-v3"}), "dsh")
    assert resp.status_code == 200
    cloud = [c for c in calls if _is_cloud_host(c["url"])][0]
    assert cloud["data"]["model"] == "groq/whisper-large-v3"  # upstream = brand/model
    assert cloud["data"]["language"] == "nl"                   # ISO verbatim to cloud
    assert cloud["headers"]["Authorization"] == "Bearer stt_test_key"


@pytest.mark.asyncio
async def test_cloud_route_failure_is_exact_no_local_fallback(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(500, content=b'{"error":"upstream down"}') if _is_cloud_host(url)
        else httpx.Response(200, json={"ok": True})))
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _FakeRequest({"file": _Upload(), "model": "cloudstt/brand/model-x"}), "dsh")
    assert excinfo.value.status_code == 502
    assert "cloudstt" in str(excinfo.value.detail)
    # EXACT: the local engine was never asked (no ensure, no local transcribe)
    assert not any(c["url"].endswith("/stt/ensure") for c in calls)
    assert all(_is_cloud_host(c["url"]) for c in calls if c["url"].endswith("/transcriptions"))


@pytest.mark.asyncio
async def test_unknown_provider_returns_404_model_not_served(monkeypatch):
    _patch(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, json={}))
    # two-segment addresses (provider + bare id) are valid now; unknown
    # PROVIDERS still 404 regardless of upstream-id shape
    for bad in ("guardian/nosuchprovider/brand/model", "guardian/nosuchprovider/whisper-large-v3"):
        with pytest.raises(HTTPException) as excinfo:
            await stt_mod.handle_audio_transcriptions(
                _FakeRequest({"file": _Upload(), "model": bad}), "dsh")
        assert excinfo.value.status_code == 404
        assert "model_not_served" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_explicit_local_route_ensures_and_forwards(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, json={"ok": True, "cold_start": False}) if url.endswith("/stt/ensure")
        else httpx.Response(200, json={"text": "lokaal", "language": "Dutch"})))
    resp = await stt_mod.handle_audio_transcriptions(
        _FakeRequest({"file": _Upload(b"RIFF", "audio/wav"), "language": "nl",
                      "model": "guardian/windows-gpu-local/qwen/qwen3-asr-sherpa"}), "dsh")
    assert resp.status_code == 200
    assert b"lokaal" in resp.body
    engine = [c for c in calls if c["url"].endswith("/transcriptions")][0]
    assert engine["content"] == b"RIFF"
    assert engine["params"] == {"language": "Dutch"}  # Qwen name mapping for the LOCAL engine
    assert [c["url"] for c in calls if "url" in c] == [
        "http://192.168.1.W:11441/stt/ensure",
        "http://192.168.1.W:11451/transcriptions",
    ]


@pytest.mark.asyncio
async def test_no_model_keeps_default_failover_chain(monkeypatch):
    _patch(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: (
        httpx.Response(200, json={"ok": True}) if url.endswith("/stt/ensure")
        else httpx.Response(200, json={"text": "lokaal"})))
    resp = await stt_mod.handle_audio_transcriptions(_FakeRequest({"file": _Upload()}), "dsh")
    assert resp.status_code == 200
    assert [c["url"] for c in calls if "url" in c] == [
        "http://192.168.1.W:11441/stt/ensure",
        "http://192.168.1.W:11451/transcriptions",
    ]


@pytest.mark.asyncio
async def test_cloud_error_body_never_reaches_client_detail(monkeypatch):
    _patch(monkeypatch)

    def handler(url, json, content, params, headers, files=None, data=None):
        if _is_cloud_host(url):
            return httpx.Response(500, content=b'{"error":"internal detail http://10.0.0.12:9200/_cluster"}')
        return httpx.Response(500, content=b"engine down")

    _patch_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(
            _FakeRequest({"file": _Upload(), "model": "cloudstt/brand/model-x"}), "dsh")
    detail = str(excinfo.value.detail)
    assert "cloud HTTP 500" in detail
    assert "10.0.0.12" not in detail and "_cluster" not in detail


@pytest.mark.asyncio
async def test_disabled_default_path_returns_404(monkeypatch):
    _patch(monkeypatch, cfg=_cfg(enabled=False))
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(_FakeRequest({"file": _Upload()}), "dsh")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_default_chain_without_stt_capable_provider_returns_503(monkeypatch):
    # openrouter has no stt_url: the DEFAULT chain has no backends -> 503
    _patch(monkeypatch, cfg=_cfg(providers=["openrouter"]))
    with pytest.raises(HTTPException) as excinfo:
        await stt_mod.handle_audio_transcriptions(_FakeRequest({"file": _Upload()}), "dsh")
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_fish_adapter_transcribes_native_dialect(monkeypatch):
    """fish.audio ASR: POST /v1/asr with the multipart field named "audio" (no
    model field) and the language hint — response JSON passes through."""
    docs = _provider_docs()
    docs["providers"]["fishstt"] = {
        "base_url": "https://stt.cloudtest.invalid",
        "api_key": "${CLOUD_STT_API_KEY}",
        "speech_adapter": "fish",
    }
    _patch(monkeypatch, docs=docs)

    def handler(url, j, c, p, h, f=None, d=None):
        assert url.endswith("/v1/asr")
        return httpx.Response(200, json={"text": "gevist", "duration": 3.0})

    calls = _patch_client(monkeypatch, handler)
    resp = await stt_mod.handle_audio_transcriptions(
        _FakeRequest({"file": _Upload(b"RIFF", "audio/wav"), "language": "nl",
                      "model": "fishstt/anything"}), "dsh")
    assert resp.status_code == 200
    assert b"gevist" in resp.body
    call = [c for c in calls if c["url"].endswith("/v1/asr")][0]
    assert set(call["files"].keys()) == {"audio"}          # fish field name
    assert call["data"] == {"language": "nl"}              # hint only, no model field
    # fish selects the ASR model via the model header (route upstream id)
    assert call["headers"]["model"] == "anything"
    assert call["headers"]["Authorization"] == "Bearer stt_test_key"
