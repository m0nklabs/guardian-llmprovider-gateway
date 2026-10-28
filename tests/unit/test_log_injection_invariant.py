"""A request value must not be able to shape a log line.

CodeQL's ``py/log-injection`` family (issue #73) was 80 alerts on log arguments
whose value traces back to a request field. Wrapping those arguments in
:func:`app.log_safety.clean_log_value` is the fix, and this module is the
invariant that keeps the fix honest: it drives real log calls of the changed
paths with a hostile model id — a forged ``event=deny`` line, an ANSI escape and
a NUL — and refuses any captured record that still carries line structure or an
escape character.

The invariant is checked for teeth by
:func:`test_the_invariant_catches_a_deliberate_leak`, which hands the same
assertion a leaky record on purpose: a guard that cannot fail is decoration
(the same spirit as the G1-03 auth-header invariant in
``tests/unit/test_audit_metadata.py``).
"""

import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.gateway import degeneration
from app.gateway import speech_routing as sr
from app.gateway import stt as stt_mod
from app.gateway import tts as tts_mod
from app.proxy.queue import InferenceQueue

#: A model address an attacker fully controls: a forged log entry, a terminal
#: escape sequence and a NUL byte in one value.
HOSTILE_MODEL = (
    'guardian/windows-gpu-local/qwen/evil"\n'
    "2026-10-28 12:00:00 INFO event=deny user=admin action=read\n"
    "\x1b[2J\x1b[0m\x00"
)
FORGED_FRAGMENT = "event=deny"


class _FakeRequest:
    """Stands in for the Starlette request: the TTS handler only calls ``.json()``."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeUpload:
    filename = "audio.wav"
    content_type = "audio/wav"

    async def read(self) -> bytes:
        return b"RIFF0000WAVE"


class _FakeFormRequest:
    """Stands in for a multipart request on the STT handler."""

    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def _provider_docs():
    return {
        "providers": {
            "windows-gpu-local": {
                "management_url": "http://192.168.1.W:11441",
                "management_key": "${WINDOWS_LAN_KEY}",
                "tts_url": "http://192.168.1.W:11450",
                "stt_url": "http://192.168.1.W:11451",
            },
        }
    }


def _cfg(**overrides):
    base = {
        "enabled": True,
        "timeout_seconds": 5,
        "ensure_timeout_seconds": 5,
        "providers": ["windows-gpu-local"],
    }
    base.update(overrides)
    return base


def _patch_config(monkeypatch, module, config_loader, cfg):
    docs = _provider_docs()
    monkeypatch.setattr(module, config_loader, lambda: cfg)
    monkeypatch.setattr(module, "CONFIG", docs)
    monkeypatch.setattr(sr, "CONFIG", docs)
    monkeypatch.setenv("WINDOWS_LAN_KEY", "ctk_windows")


def _patch_failing_client(monkeypatch, module):
    """Every upstream hop fails, with attacker-influenced text in the error."""

    class _FailingClient:
        async def __aenter__(self):
            return SimpleNamespace(post=self._fail, get=self._fail)

        async def __aexit__(self, *args):
            return False

        async def _fail(self, *args, **kwargs):
            raise httpx.ConnectError(f"connect failed for {HOSTILE_MODEL}")

    def _factory(*args, **kwargs):
        return _FailingClient()

    monkeypatch.setattr(module, "guarded_async_client", _factory)


def _invariant_violations(caplog) -> list[str]:
    """Every captured record that still carries line structure or an escape.

    ``record.getMessage()`` is exactly the string a handler would emit — the
    promise the sanitizer makes.
    """
    offending: list[str] = []
    for record in caplog.records:
        message = record.getMessage()
        for character in ("\n", "\r", "\x1b", "\x00", "\x07"):
            if character in message:
                offending.append(f"{record.name}: {message!r} carries {character!r}")
    return offending


def _assert_no_line_structure(caplog) -> None:
    assert caplog.records, "no log line captured — the test would be vacuous"
    offending = _invariant_violations(caplog)
    assert not offending, "hostile value shaped a log line: " + repr(offending[:3])


@pytest.mark.asyncio
async def test_hostile_model_id_cannot_forge_a_tts_log_line(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _patch_config(monkeypatch, tts_mod, "load_tts_config", _cfg())
    _patch_failing_client(monkeypatch, tts_mod)

    with pytest.raises(HTTPException):
        await tts_mod.handle_audio_speech(
            _FakeRequest({"model": HOSTILE_MODEL, "input": "hello"}), "dsh"
        )

    _assert_no_line_structure(caplog)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "evil" in messages, "the hostile value never reached a log line — vacuous"
    assert FORGED_FRAGMENT in messages


@pytest.mark.asyncio
async def test_hostile_model_id_cannot_forge_an_stt_log_line(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _patch_config(monkeypatch, stt_mod, "load_stt_config", _cfg())
    _patch_failing_client(monkeypatch, stt_mod)

    with pytest.raises(HTTPException):
        await stt_mod.handle_audio_transcriptions(
            _FakeFormRequest({"file": _FakeUpload(), "model": HOSTILE_MODEL}), "dsh"
        )

    _assert_no_line_structure(caplog)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "evil" in messages, "the hostile value never reached a log line — vacuous"


def test_hostile_model_id_cannot_forge_a_degeneration_cutoff_line(caplog):
    caplog.set_level(logging.DEBUG)
    degeneration.log_cutoff(HOSTILE_MODEL, degeneration.DegenerationVerdict(6, 4, 24))

    _assert_no_line_structure(caplog)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "evil" in messages, "the hostile value never reached a log line — vacuous"


def test_hostile_client_and_model_cannot_forge_a_queue_line(caplog):
    caplog.set_level(logging.DEBUG)
    queue = InferenceQueue()
    queue.submit("client-a", "warm-up", request_id="warm")
    queue.submit(HOSTILE_MODEL, HOSTILE_MODEL, request_id="hostile")

    _assert_no_line_structure(caplog)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "evil" in messages, "the hostile value never reached a log line — vacuous"


def test_the_invariant_catches_a_deliberate_leak(caplog):
    """Non-vacuity: the assertion above must fail on an unsanitized value."""
    logging.getLogger("Guardian.Test").warning("leaky value: %s", HOSTILE_MODEL)
    with pytest.raises(AssertionError):
        _assert_no_line_structure(caplog)
