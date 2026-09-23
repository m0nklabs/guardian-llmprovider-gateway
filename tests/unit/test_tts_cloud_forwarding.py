"""Cloud TTS forwarding pins — model-based provider routing.

Opt-in twice: global ``tts.cloud_forwarding.enabled`` + per-provider
``cloud_tts: true``. A requested model like ``cloudtts/cloudtts/orpheus-v1-english``
forwards the request to the provider's OpenAI-compatible ``/audio/speech``;
the upstream model id is the final path segment and the forwarded payload is
the canonical OpenAI shape (clone passthroughs and ``instruct`` do not travel
to cloud providers). Any cloud failure falls through to the local engine chain
unchanged; with the switch off (or no model field) behavior is identical to
the pre-forwarding route."""

import httpx
import pytest
from fastapi import HTTPException
from urllib.parse import urlparse
from types import SimpleNamespace

from app.gateway import tts as tts_mod


def _is_cloud_host(url: str) -> bool:
    """Exact-host match against the test fixture — startswith would also match
    an attacker-shaped sibling host (CodeQL incomplete-URL-substring)."""
    return urlparse(url).hostname == "tts.cloudtest.invalid"


def _provider_docs(**overrides):
    cloud = {
        "base_url": "https://tts.cloudtest.invalid/openai/v1",
        "api_key": "${CLOUD_TTS_API_KEY}",
        "cloud_tts": True,
    }
    cloud.update(overrides)
    return {
        "providers": {
            "cloudtts": cloud,
            "14700k-local": {
                "management_url": "http://192.168.1.245:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "tts_url": "http://192.168.1.245:11450",
            },
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 120,
        "ensure_timeout_seconds": 420,
        "providers": ["14700k-local"],
        "cloud_forwarding": {"enabled": True},
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
        "model": "cloudtts/cloudtts/orpheus-v1-english",
        "input": "Believe me, this is a tremendous test.",
        "response_format": "wav",
    }
    base.update(overrides)
    return base


def _patch_config(monkeypatch, cfg=None, docs=None):
    monkeypatch.setattr(tts_mod, "load_tts_config", lambda: cfg or _cfg())
    monkeypatch.setattr(tts_mod, "CONFIG", docs or _provider_docs())
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")
    monkeypatch.setenv("CLOUD_TTS_API_KEY", "ctts_test")


def _patch_client(monkeypatch, handler):
    calls = []

    async def post(url, json=None, content=None, params=None, headers=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return handler(url, json, content, params, headers)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(tts_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


# --- target resolution ---


def test_target_strips_to_final_model_segment(monkeypatch):
    _patch_config(monkeypatch)
    target = tts_mod._cloud_tts_target("cloudtts/cloudtts/orpheus-v1-english")
    assert target == {
        "name": "cloudtts",
        "base_url": "https://tts.cloudtest.invalid/openai/v1",
        "api_key": "ctts_test",
        "model": "orpheus-v1-english",
    }


def test_target_rejects_non_matching_models(monkeypatch):
    _patch_config(monkeypatch)
    assert tts_mod._cloud_tts_target("orpheus-v1-english") is None     # no namespace
    assert tts_mod._cloud_tts_target("") is None                       # empty
    assert tts_mod._cloud_tts_target("other/openai/tts-1") is None     # no cloud_tts flag


def test_target_rejects_provider_without_credentials(monkeypatch):
    _patch_config(monkeypatch, docs=_provider_docs(api_key=""))
    assert tts_mod._cloud_tts_target("cloudtts/cloudtts/orpheus-v1-english") is None


# --- handler routing ---


@pytest.mark.asyncio
async def test_disabled_switch_keeps_local_only(monkeypatch):
    _patch_config(monkeypatch, cfg=_cfg(cloud_forwarding={"enabled": False}))

    def handler(url, json, content, params, headers):
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=b"WAVBYTES", headers={"content-type": "audio/wav"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body()), "dsh")
    assert resp.status_code == 200
    assert not any(_is_cloud_host(c["url"]) for c in calls)
    assert any(c["url"].endswith("/tts") for c in calls)


@pytest.mark.asyncio
async def test_cloud_success_serves_request_without_local_engines(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        assert url == "https://tts.cloudtest.invalid/openai/v1/audio/speech"
        assert headers["Authorization"] == "Bearer ctts_test"
        assert json == {
            "model": "orpheus-v1-english",
            "input": "Believe me, this is a tremendous test.",
            "response_format": "wav",
        }
        return httpx.Response(200, content=b"CLOUDAUDIO", headers={"content-type": "audio/wav"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body()), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"CLOUDAUDIO"
    assert len([c for c in calls if _is_cloud_host(c["url"])]) == 1
    assert not any(c["url"].endswith("/tts/ensure") for c in calls)  # local untouched


@pytest.mark.asyncio
async def test_cloud_payload_never_carries_clone_fields_or_instruct(monkeypatch):
    _patch_config(monkeypatch)

    captured = {}

    def handler(url, json, content, params, headers):
        if _is_cloud_host(url):
            captured["payload"] = json
            return httpx.Response(200, content=b"CLOUDAUDIO", headers={"content-type": "audio/wav"})
        return httpx.Response(200, content=b"WAVBYTES", headers={"content-type": "audio/wav"})

    _patch_config(monkeypatch)
    body = _body(
        voice="alloy",
        instruct="clone this voice",
        ref_audio="trump.wav",
        ref_text="some anchor text",
        zero_shot=True,
        speed=1.0,
    )
    _patch_client(monkeypatch, handler)
    await tts_mod.handle_audio_speech(_FakeRequest(body), "dsh")
    assert captured["payload"] == {
        "model": "orpheus-v1-english",
        "input": "Believe me, this is a tremendous test.",
        "voice": "alloy",
        "response_format": "wav",
        "speed": 1.0,
    }


@pytest.mark.asyncio
async def test_cloud_failure_falls_through_to_local_engine(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        if _is_cloud_host(url):
            return httpx.Response(503, content=b"model not ready")
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=b"WAVBYTES", headers={"content-type": "audio/wav"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body()), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"WAVBYTES"
    assert any(c["url"].endswith("/tts/ensure") for c in calls)


@pytest.mark.asyncio
async def test_cloud_unreachable_falls_through(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        if _is_cloud_host(url):
            raise httpx.ConnectError("DNS fail")
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=b"WAVBYTES", headers={"content-type": "audio/wav"})

    _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body()), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"WAVBYTES"


@pytest.mark.asyncio
async def test_no_matching_cloud_model_keeps_local_flow(monkeypatch):
    _patch_config(monkeypatch)

    def handler(url, json, content, params, headers):
        if url.endswith("/tts/ensure"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=b"WAVBYTES", headers={"content-type": "audio/wav"})

    calls = _patch_client(monkeypatch, handler)
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(model="qwen3tts")), "dsh")
    assert resp.status_code == 200
    assert not any(_is_cloud_host(c["url"]) for c in calls)


@pytest.mark.asyncio
async def test_cloud_routed_request_allows_upstream_formats(monkeypatch):
    """The wav/pcm restriction is a property of the LOCAL engine — a
    cloud-routed request passes response_format verbatim (mp3 etc.) instead of
    getting a 400 the cloud provider could have served."""
    _patch_config(monkeypatch)
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"mp3data", headers={"content-type": "audio/mpeg"}))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(
        response_format="mp3")), "dsh")
    assert resp.status_code == 200
    assert resp.body == b"mp3data"
    assert resp.media_type == "audio/mpeg"
    cloud_call = [c for c in calls if _is_cloud_host(c["url"])][0]
    assert cloud_call["json"]["response_format"] == "mp3"


@pytest.mark.asyncio
async def test_local_routed_request_keeps_format_400(monkeypatch):
    """Without an opted-in cloud target the wav/pcm restriction still applies —
    the 400 is the local engine's contract, unchanged."""
    _patch_config(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"wav"))
    with pytest.raises(HTTPException) as excinfo:
        await tts_mod.handle_audio_speech(_FakeRequest(_body(
            model="unknown-brand/brand/model", response_format="mp3")), "dsh")
    assert excinfo.value.status_code == 400
    assert "response_format" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_speed_validated_early_with_clear_400(monkeypatch):
    """A non-numeric or out-of-range speed is a clear client 400 — not a burned
    cloud attempt followed by an opaque upstream 4xx."""
    _patch_config(monkeypatch)
    _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"x"))
    for bad in ("fast", 10.0, True):
        with pytest.raises(HTTPException) as excinfo:
            await tts_mod.handle_audio_speech(_FakeRequest(_body(speed=bad)), "dsh")
        assert excinfo.value.status_code == 400
        assert "speed" in str(excinfo.value.detail)
    # a valid speed passes through verbatim
    calls = _patch_client(monkeypatch, lambda url, j, c, p, h, f=None, d=None: httpx.Response(200, content=b"x"))
    resp = await tts_mod.handle_audio_speech(_FakeRequest(_body(speed=1.5)), "dsh")
    assert resp.status_code == 200
    cloud_call = [c for c in calls if _is_cloud_host(c["url"])][0]
    assert cloud_call["json"]["speed"] == 1.5
