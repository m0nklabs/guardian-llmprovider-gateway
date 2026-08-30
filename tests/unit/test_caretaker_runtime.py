"""Tests for app.gateway.caretaker_runtime — F5 remote-first hotpath ensure.

Pins the decision logic of the request-path lifecycle bridge:
caretaker /ensure first, gateway ModelManager fallback when the caretaker is
not configured or unreachable, and error mapping so the hotpath callers keep
their existing crash/503 handling.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from app.engine.manager import ModelLoadError
from app.gateway import caretaker_runtime as crt
from app.gateway.caretaker_client import (
    CaretakerInvalidRequest,
    CaretakerModelLoadFailed,
    CaretakerModelNotFound,
    CaretakerUnavailable,
    CaretakerVramExceeded,
)


class _StubClient:
    def __init__(self) -> None:
        self.ensure = AsyncMock(return_value={"ok": True, "loaded_model": "m"})
        # Default: daemon does not ship fresh_load yet (capability not detected).
        self.supports_fresh_load = False


@pytest.fixture(autouse=True)
def _reset_caretaker_process_state():
    """The runtime keeps module-global process-lifetime state (whether the
    caretaker daemon was ever observed up).  Reset it before every test so
    no test leaks its daemon-observation into the next one."""
    crt._ever_reached_caretaker = False
    yield


def _timeout_unavailable() -> CaretakerUnavailable:
    """A transport CaretakerUnavailable whose cause is a timeout — the daemon
    accepted the connection, so it is alive but busy."""
    err = CaretakerUnavailable("http://x:11441")
    err.__cause__ = httpx.ReadTimeout("timed out")
    return err


@pytest.fixture(autouse=True)
def _reset_runtime():
    crt.init(model_manager=None, caretaker_client=None)
    yield
    crt.init(model_manager=None, caretaker_client=None)


def _manager(**attrs) -> MagicMock:
    mgr = MagicMock()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=False)
    mgr.save_current_context = AsyncMock()
    mgr.restore_current_context = AsyncMock()
    mgr._config_drifted = Mock(return_value=False)
    mgr.is_switch_allowed = Mock(return_value=True)
    # Identity resolver by default — tests override it when they need alias
    # canonicalization semantics.
    mgr.resolve_model = Mock(side_effect=lambda name: name)
    # Live-process probe defaults: a text-only process unless a test says
    # otherwise (drives the effective-vision mirror and the vision guards).
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    for k, v in attrs.items():
        setattr(mgr, k, v)
    return mgr


async def test_client_none_runs_local_fallback():
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    fallback.assert_awaited_once()


async def test_remote_success_calls_ensure_and_marks_loaded():
    client = _StubClient()
    mgr = _manager()
    # Vision is requested and the live process confirms mmproj -> success.
    mgr.current_runtime_uses_mmproj = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m",
        enable_vision=True,
        context_hint=4096,
        local_fallback=AsyncMock(),
    )
    assert result == "remote"
    # A successful /ensure records that the daemon was observed up: later
    # refused re-binds must keep their full 15s poll (the daemon can be
    # restarting), never skip it.
    assert crt._ever_reached_caretaker is True
    client.ensure.assert_awaited_once_with(
        "m", enable_vision=True, context_hint=4096
    )
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=4096
    )
    # Reload sites (no pre_switch_save) start a fresh context — restore must
    # NOT run (mirrors load(), which does not restore).
    mgr.restore_current_context.assert_not_awaited()


async def test_remote_success_vision_request_without_mmproj_fails_closed():
    """The daemon may resolve vision differently than the gateway (own config
    / hot config edit dropping mmproj).  On the SUCCESS path too, stamping
    current_vision_enabled=True on a text-only live process would forward
    image requests to a backend that cannot serve them -> fail closed."""
    client = _StubClient()
    mgr = _manager()
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    with pytest.raises(ModelLoadError, match="without mmproj"):
        await crt.ensure_backend(
            model="m", enable_vision=True, local_fallback=AsyncMock()
        )
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_remote_success_vision_request_with_mmproj_marks_loaded():
    """When the live process DOES use mmproj, the success path marks the model
    loaded with the requested vision flag."""
    client = _StubClient()
    mgr = _manager()
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", enable_vision=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=None
    )


async def test_remote_success_vision_none_textonly_process_serves_text_request():
    """With enable_vision=None (ollama bridge / connect-error recovery
    reloads) the request has NO vision need — a daemon legitimately serving a
    vision-capable model text-only (VRAM limits, daemon config without mmproj)
    must NOT 503 the text request even though the gateway's current stamp is
    vision-enabled.  The mark mirrors the LIVE text-only state instead."""
    client = _StubClient()
    mgr = _manager()
    # The gateway's CURRENT stamp says vision (stale vs the daemon's process).
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "remote"
    # Truthful stamp: the live process is text-only -> current_vision_enabled
    # is cleared so a later image request triggers a vision-enabled switch.
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_remote_success_vision_none_process_with_mmproj_marks_true():
    """With enable_vision=None and a LIVE process that uses mmproj, the mark
    mirrors the process (vision enabled) — same as the resolved default for
    the already-active model."""
    client = _StubClient()
    mgr = _manager()
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "remote"
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=None
    )


async def test_remote_success_daemon_confirmed_vision_wins_over_probe():
    """A daemon-confirmed ``vision_enabled`` bool in the /ensure response is
    the authoritative vision state (F6 remote-host topology: the local probe
    reads stale/absent state there).  When the daemon ships the field, it
    wins over the gateway-local mmproj probe for the guard AND the mark."""
    client = _StubClient()
    client.ensure = AsyncMock(
        return_value={
            "ok": True,
            "loaded_model": "m",
            "vision_enabled": True,
        }
    )
    mgr = _manager()
    # The local probe would say text-only — must be IGNORED because the
    # daemon confirmed vision.
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", enable_vision=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.current_runtime_uses_mmproj.assert_not_called()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=None
    )


async def test_remote_success_daemon_confirmed_text_only_fails_vision_request():
    """Daemon-confirmed text-only (vision_enabled: false) + explicit vision
    request -> fail closed on the daemon's own state, independent of what the
    local probe would say."""
    client = _StubClient()
    client.ensure = AsyncMock(
        return_value={
            "ok": True,
            "loaded_model": "m",
            "vision_enabled": False,
        }
    )
    mgr = _manager()
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    with pytest.raises(ModelLoadError, match="without mmproj"):
        await crt.ensure_backend(
            model="m", enable_vision=True, local_fallback=AsyncMock()
        )
    mgr.current_runtime_uses_mmproj.assert_not_called()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_remote_success_daemon_confirmed_vision_with_none_marks_true():
    """Daemon-confirmed vision + enable_vision=None -> the mark mirrors the
    daemon-confirmed state (no local probe involved)."""
    client = _StubClient()
    client.ensure = AsyncMock(
        return_value={
            "ok": True,
            "loaded_model": "m",
            "vision_enabled": True,
        }
    )
    mgr = _manager()
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)  # must be ignored
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "remote"
    mgr.current_runtime_uses_mmproj.assert_not_called()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=None
    )


async def test_unavailable_timeout_rereprobe_succeeds_remote():
    """A transport TIMEOUT is NOT a dead daemon: the daemon accepted the
    connection, so it is alive (mid-switch).  Re-probing once must recover a
    merely-slow daemon -> remote, no local fallback."""
    client = _StubClient()
    client.ensure.side_effect = [
        _timeout_unavailable(),
        {"ok": True, "loaded_model": "m"},
    ]
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    assert client.ensure.await_count == 2
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_ensure_unparseable_200_body_reprobes_once(monkeypatch):
    """A malformed/empty 200 body from /ensure is mapped to
    CaretakerUnavailable(status_code=200) — the daemon answered (alive), but
    the body was unparseable (intermediary HTML/empty page, momentary
    glitch).  That is NOT an auth/ownership rejection: re-probe once like
    transport errors instead of failing closed on a single glitch."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure = AsyncMock(
        side_effect=[
            CaretakerUnavailable("http://x:11441", status_code=200),
            {"ok": True, "loaded_model": "m"},
        ]
    )
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "remote"
    assert client.ensure.await_count == 2
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_ensure_unparseable_200_body_twice_fails_closed(monkeypatch):
    """A SECOND 200-body parse failure still fails closed: the daemon is
    demonstrably alive (it answered 200), so the local lifecycle stays
    forbidden as a second controller."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure = AsyncMock(
        side_effect=[
            CaretakerUnavailable("http://x:11441", status_code=200),
            CaretakerUnavailable("http://x:11441", status_code=200),
        ]
    )
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 2
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_timeout_twice_healthy_wrong_model_polls_full_window(monkeypatch):
    """A HEALTHY backend that serves a different model is NOT a determined
    outcome: the daemon's pre-stop phases (VRAM acquire, context auto-save)
    and serialization of concurrent /ensure requests leave a healthy
    old-model backend while our switch is still on its way.  The poll keeps
    probing the FULL bounded _ADOPT_POLL_SECONDS window (pre-F5 waited
    without a cap) and only then fails closed — a short grace would turn a
    legitimate cold switch that completes moments later into a spurious
    503."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)  # serves other model
    mgr.backend_health_ok = AsyncMock(return_value=True)  # healthy
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 2
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()
    # The full bounded window was polled — not a short grace bailout.
    assert mgr.backend_serves_model.await_count == crt._ADOPT_POLL_SECONDS


async def test_unavailable_timeout_twice_fails_closed(monkeypatch):
    """Two consecutive read/write timeouts mean the daemon accepted the
    connection but is unresponsive (still mid-switch — a load can outlast the
    client timeout more than once).  Spawning locally would create a second
    controller on a live daemon's backend -> fail closed with ModelLoadError
    after the bounded adoption poll finds nothing serving."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 2
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_timeout_twice_adopts_when_backend_now_serves(monkeypatch):
    """A cold large-model load can outlast the client timeout more than once
    while the daemon's in-flight /ensure is the only controller.  Pre-F5
    load()/switch_model() waited for backend health, so 503ing would be a
    regression — when the backend now serves the model, adopt it instead."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    assert client.ensure.await_count == 2
    fallback.assert_not_awaited()
    # Adopt semantics: no restore (no daemon fresh_load confirmation).
    mgr.restore_current_context.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_unavailable_timeout_twice_adopts_when_load_confirms_late(monkeypatch):
    """The adoption poll gives the cold load time to confirm: the backend is
    still loading at the first probe and only serves the model one probe
    later -> adopt the confirmed load instead of 503ing."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(side_effect=[False, True])
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    assert mgr.backend_serves_model.await_count == 2
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_timeout_poll_adopts_despite_drift_when_switch_to_other_model(monkeypatch):
    """r30 review: the drift check must NOT block adoption during a switch to
    a DIFFERENT model.  The gateway only rewrites the persisted launch
    signature via mark_loaded_by_caretaker (after a successful /ensure or an
    adoption), so while a timed-out switch A->B is in flight the persisted
    signature still describes A — _config_drifted(B) would be True for the
    whole poll and adoption would be refused even after the daemon finishes
    loading B, burning the full window into a spurious 503.  The live GGUF
    identity + health already validate the running process, so drift only
    blocks adoption when the gateway ALREADY believes the requested model is
    current."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)  # stale sig describes old model
    mgr.current_model = "old-model"  # gateway still believes the OLD model
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_timeout_poll_fails_closed_on_drift_when_model_already_current(monkeypatch):
    """Drift still blocks adoption when the gateway ALREADY believes the
    requested model is current: then the persisted signature describes the
    same model the backend serves, and a mismatch (settings edited in
    models.yaml / a client context hint differing from the running launch)
    means the running process does not match what this request needs."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)
    mgr.current_model = "m"  # gateway believes the requested model IS current
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_timeout_poll_adopts_same_model_vision_toggle_despite_drift(monkeypatch):
    """r32 review: during a timed-out SAME-MODEL parameter switch (vision
    toggle), the persisted launch signature is stale by construction (it still
    describes the old text-only launch) — the drift check would report drift
    for the whole poll and refuse adoption even after the daemon finishes the
    reload with vision.  A request WITH a parameter delta must not be blocked
    by the drift check; the live GGUF identity + health + the vision mmproj
    guard validate the running process (pre-F5 switch_model waited on backend
    health and succeeded here)."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)  # stale sig describes old launch
    mgr.current_model = "m"  # same model, vision toggle in flight
    mgr.current_runtime_uses_mmproj = Mock(return_value=True)  # live process uses mmproj
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(
        model="m", enable_vision=True, local_fallback=fallback
    )
    assert result == "remote"
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=None
    )


async def test_timeout_poll_adopts_same_model_context_hint_change_despite_drift(monkeypatch):
    """r32 review companion: the same stale-signature reasoning holds for a
    same-model context_hint change — drift must not block adoption of a load
    the daemon finished with the hinted context."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)
    mgr.current_model = "m"
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(
        model="m", context_hint=8192, local_fallback=fallback
    )
    assert result == "remote"
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=8192
    )


async def test_unavailable_connect_timeout_runs_local():
    """ConnectTimeout/PoolTimeout mean NO connection was ever accepted — the
    daemon may as well be down (full backlog, firewall DROP, LAN host powered
    off).  The local lifecycle is then safe (sole controller)."""
    client = _StubClient()

    def _connect_timeout() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectTimeout("connect timed out")
        return err

    client.ensure.side_effect = [_connect_timeout(), _connect_timeout()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    assert client.ensure.await_count == 2
    fallback.assert_awaited_once()


async def test_unavailable_connect_timeout_alive_backend_fails_closed():
    """ConnectTimeout means the SYN was never answered (DROP/backlog), so the
    daemon may STILL be alive owning the backend.  A live backend serving a
    different model -> the local lifecycle would race it -> fail closed."""
    client = _StubClient()

    def _connect_timeout() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectTimeout("connect timed out")
        return err

    client.ensure.side_effect = [_connect_timeout(), _connect_timeout()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    fallback.assert_not_awaited()


async def test_unavailable_connection_established_error_fails_closed(monkeypatch):
    """Transport errors that prove a connection WAS established (read/write
    resets, protocol errors, pool exhaustion) mean the daemon accepted a
    connection — it is alive (or was when it dropped us) and still owns the
    backend, even when the backend health probe fails (busy mid-switch while
    llama-server restarts).  The local lifecycle would race it -> fail closed
    just like the timeout branch."""
    for cause_cls in (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.PoolTimeout):
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        client = _StubClient()

        def _established(cause_cls=cause_cls) -> CaretakerUnavailable:
            err = CaretakerUnavailable("http://x:11441")
            err.__cause__ = cause_cls("connection dropped")
            return err

        client.ensure.side_effect = [_established(), _established()]
        mgr = _manager()
        mgr.backend_serves_model = AsyncMock(return_value=False)
        mgr.backend_health_ok = AsyncMock(return_value=False)  # backend down too
        crt.init(model_manager=mgr, caretaker_client=client)
        fallback = AsyncMock()
        with pytest.raises(ModelLoadError, match=cause_cls.__name__):
            await crt.ensure_backend(model="m", local_fallback=fallback)
        assert client.ensure.await_count == 2
        fallback.assert_not_awaited()
        mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_connection_refused_alive_backend_runs_local(monkeypatch):
    """A hard connection-refused (RST — nothing listening on the management
    port) is the strongest evidence the daemon process is definitively gone:
    no live daemon owns the backend, so the local lifecycle is the sole
    controller and the pre-F5 auto-switch is preserved even when a llama-server
    survives serving a different model.  A bounded re-probe poll first gives a
    merely-restarting daemon a window to re-bind."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [_refused() for _ in range(17)]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before it went down
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    assert client.ensure.await_count == 17  # 2 probes + 15-iteration re-bind poll
    fallback.assert_awaited_once()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_refused_alive_backend_log_reflects_not_serving(monkeypatch, caplog):
    """The connection-refused fallback with a HEALTHY backend must not log
    'backend down': the backend is alive, just not serving the requested
    model (the daemon process is gone, its llama-server child survives).  An
    operator debugging the switch failure gets a truthful signal."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [_refused() for _ in range(17)]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before it went down
    with caplog.at_level(logging.WARNING, logger="app.gateway.caretaker_runtime"):
        result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "local"
    assert "backend not serving the requested model" in caplog.text
    assert "backend down" not in caplog.text


async def test_unavailable_connection_refused_then_daemon_rebounds_remote(monkeypatch):
    """A merely-restarting daemon (systemd Restart=always, deploy window) can
    have its management port briefly closed while its llama-server child
    survives.  The bounded re-probe poll succeeds after the daemon re-binds ->
    the remote path completes normally instead of taking over as a second
    controller."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [
        _refused(),
        _refused(),
        {"ok": True, "loaded_model": "m"},
    ]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart window
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    assert client.ensure.await_count == 3
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_rebound_recheck_rejected_fails_closed(monkeypatch):
    """The bounded re-probe after a refused connection can find the daemon
    RE-BOUND but rejecting us (status_code, e.g. 401/403 after a key
    rotation): the daemon is back and owns the backend — the local lifecycle
    must not become a second controller, fail closed with the mapped error."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [
        _refused(),
        _refused(),
        CaretakerUnavailable("http://x:11441", status_code=401),
    ]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart window
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError, match="status 401"):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 3
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_rebound_recheck_timeout_fails_closed(monkeypatch):
    """The bounded re-probe can find the daemon RE-BOUND but busy (ReadTimeout):
    it accepted the connection — alive — so the local lifecycle must not run,
    fail closed even though the backend is healthy."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [
        _refused(),
        _refused(),
        _timeout_unavailable(),
    ]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart window
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError, match="re-bound but unresponsive"):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 3
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_connection_refused_late_rebind_remote(monkeypatch):
    """The re-bind poll gives a restarting daemon (RestartSec + startup,
    longer than a single second) time to re-bind: nothing answers the first
    two poll attempts, then the daemon is back -> the remote path completes
    instead of handing the backend to the local lifecycle."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [
        _refused(),
        _refused(),
        _refused(),
        _refused(),
        {"ok": True, "loaded_model": "m"},
    ]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart window
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    assert client.ensure.await_count == 5  # 2 probes + 3 poll attempts
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )
    mgr.restore_current_context.assert_not_awaited()


async def test_refused_never_observed_daemon_skips_rebind_poll(monkeypatch):
    """Roll-out: caretaker configured but its daemon never answered a
    control-API call in this process lifetime.  It cannot be 'restarting', so
    the 15s re-bind poll is skipped — the local lifecycle runs immediately
    (2 refused probes only, not 17) instead of stalling every local-fallback
    switch for ~15s during rollout."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [_refused() for _ in range(2)]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    # _ever_reached_caretaker stays False (the autouse reset) — the daemon
    # has never been observed up in this process lifetime.
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    assert client.ensure.await_count == 2  # no 15-iteration re-bind poll
    fallback.assert_awaited_once()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_refused_backend_down_ever_observed_polls_rebind(monkeypatch):
    """r32 review: with systemd KillMode=control-group the daemon's llama-server
    child dies with it, so during a daemon restart BOTH the management port and
    the backend are down (backend_healthy False).  An ever-observed daemon can
    still be restarting — the re-bind poll must run there too, otherwise the
    gateway would spawn locally and race the returning daemon (two controllers
    on the same backend port)."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [_refused() for _ in range(17)]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=False)  # backend DOWN (KillMode)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    assert client.ensure.await_count == 17  # 2 probes + 15-iteration re-bind poll
    fallback.assert_awaited_once()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_refused_backend_down_ever_observed_rebinds_remote(monkeypatch):
    """r32 review companion: with the backend down but an ever-observed daemon
    that re-binds during the poll, the remote path completes — the local
    lifecycle must NOT take over the backend while the daemon comes back."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [
        _refused(),
        _refused(),
        _refused(),
        {"ok": True, "loaded_model": "m"},
    ]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=False)  # backend DOWN (KillMode)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "remote"
    assert client.ensure.await_count == 4  # 2 probes + 2 poll attempts
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_unavailable_rereprobe_also_fails_then_local():
    """Two non-timeout CaretakerUnavailable in a row (connection refused =
    daemon really gone) let the local lifecycle run — it is then the sole
    controller."""
    client = _StubClient()
    client.ensure.side_effect = [
        CaretakerUnavailable("http://x:11441"),
        CaretakerUnavailable("http://x:11441"),
    ]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    assert client.ensure.await_count == 2
    fallback.assert_awaited_once()


async def test_unavailable_refused_then_timeout_fails_closed(monkeypatch):
    """First probe connection-refused (daemon restarting, gone at that
    instant), re-probe times out (daemon came back but is busy): the local
    lifecycle must NOT run — the daemon is alive -> fail closed."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = _StubClient()
    client.ensure.side_effect = [
        CaretakerUnavailable("http://x:11441"),  # refused, no cause
        _timeout_unavailable(),
    ]
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 2
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_switch_without_fresh_load_support_saves_but_does_not_restore():
    """While the daemon does not ship fresh_load, a remote switch cannot
    restore context.  The SWITCH stays remote-first — the local lifecycle
    while the daemon is alive would race a second controller — and the
    pre-save ALWAYS runs (pre_switch_save = caller asked for persistence;
    pre-F5 switch_model always saved; skipping it would lose the active
    session on every A->B->A cycle).  Only the restore stays gated."""
    client = _StubClient()  # supports_fresh_load False
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=fallback
    )
    assert result == "remote"
    client.ensure.assert_awaited_once()
    fallback.assert_not_awaited()
    mgr.save_current_context.assert_awaited_once()
    mgr.restore_current_context.assert_not_awaited()


async def test_remote_ensure_fresh_load_true_restores():
    """The daemon's explicit fresh_load: true is the ONLY restore signal — a
    freshly loaded model (empty slot) mirrors switch_model's restore."""
    client = _StubClient()
    client.supports_fresh_load = True
    client.ensure = AsyncMock(
        return_value={"ok": True, "loaded_model": "m", "fresh_load": True}
    )
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.restore_current_context.assert_awaited_once()


async def test_first_switch_after_capability_transition_saves_and_restores():
    """The very first switch after the daemon starts shipping fresh_load
    RESTORES like pre-F5 switch_model did: the pre-save is unconditional with
    pre_switch_save (r25), so the current session IS saved before the switch —
    there is no 'restore without save' asymmetry to guard against.  The
    response-level fresh_load: true is the only authoritative freshness
    signal; the stale pre-request capability flag must not suppress the
    restore (that would silently drop one A->B->A recovery)."""
    client = _StubClient()
    client.supports_fresh_load = False  # stale pre-upgrade flag
    client.ensure = AsyncMock(
        return_value={"ok": True, "loaded_model": "m", "fresh_load": True}
    )  # daemon upgraded mid-request
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    client.ensure.assert_awaited_once()
    mgr.save_current_context.assert_awaited_once()
    mgr.restore_current_context.assert_awaited_once()


async def test_remote_ensure_fresh_load_false_skips_restore():
    """The daemon says the load was idempotent (fresh_load False) -> no restore
    (live session authoritative)."""
    client = _StubClient()
    client.supports_fresh_load = True
    client.ensure = AsyncMock(
        return_value={"ok": True, "loaded_model": "m", "fresh_load": False}
    )
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_with_healthy_backend_adopts_state():
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    # Same-model + no parameter delta: the narrow case in which the persisted
    # signature is meaningful, so the drift check actually runs (and passes,
    # letting adoption proceed).
    mgr.current_model = "m"
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local-healthy"
    # backend_serves_model runs once — in the adopt condition (no pre-ensure
    # probe anymore).
    mgr.backend_serves_model.assert_awaited_once_with("m")
    mgr.backend_health_ok.assert_awaited_once_with()
    mgr._config_drifted.assert_called_once_with(
        "m", enable_vision=None, context_hint=None
    )
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )
    fallback.assert_not_awaited()
    # Adopt never restores: there is no /ensure response to confirm freshness,
    # so the live session in slot 0 is authoritative (no clobbering).
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_vision_request_backend_with_mmproj_adopts():
    """A vision request against a healthy backend whose LIVE process uses
    mmproj may adopt — the vision flag is stamped only when the real process
    confirms it."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", enable_vision=True, local_fallback=AsyncMock()
    )
    assert result == "local-healthy"
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=None
    )


async def test_unavailable_vision_request_backend_without_mmproj_fails_closed():
    """The drift check only compares GATEWAY-side persisted state; the daemon
    may have launched the backend without mmproj.  Adopting would stamp
    current_vision_enabled=True on a text-only process — a subsequent image
    request would be forwarded to a backend that cannot serve it.  When the
    live process does NOT use mmproj, adoption is refused and the
    backend-alive guard fails closed."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(
            model="m", enable_vision=True, local_fallback=fallback
        )
    fallback.assert_not_awaited()


async def test_unavailable_adopt_vision_none_textonly_process_adopts():
    """enable_vision=None (reloads) has no vision need — a daemon-served
    text-only process on a vision-capable model may be adopted and the mark
    mirrors the LIVE text-only state (a later image request triggers a
    vision-enabled switch) instead of failing the text request."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._resolve_runtime_vision_flag = Mock(return_value=True)
    mgr.current_runtime_uses_mmproj = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "local-healthy"
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_unavailable_adopt_never_restores():
    """Adoption never restores — the daemon is unreachable, so no fresh_load
    confirmation is available; the live session in slot 0 is authoritative."""
    client = _StubClient()
    client.supports_fresh_load = True  # gate passes; adopt path is reached
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "local-healthy"
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_adopt_never_restores_even_after_timed_out_switch():
    """A timed-out /ensure may have COMPLETED the switch — but the daemon is
    unreachable, so no fresh_load confirmation exists.  Adopt never restores
    on a probe result alone (it could clobber a live session)."""
    client = _StubClient()
    client.supports_fresh_load = True
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "local-healthy"
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_auth_status_fails_closed():
    """A CaretakerUnavailable WITH a status_code means the daemon is alive but
    rejected the gateway (e.g. 401/403 after key rotation).  The local
    lifecycle would become a second controller on a backend the daemon still
    owns -> fail closed, no re-probe, no fallback."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441", status_code=401)
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    assert client.ensure.await_count == 1  # no pointless re-probe
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()
    # A rejection still means the daemon EXISTS (it answered with a status) —
    # the "ever observed up" flag must be set so refused re-binds keep their
    # full poll instead of skipping it.
    assert crt._ever_reached_caretaker is True


async def test_unavailable_same_model_no_delta_drifted_fails_closed():
    """Same-model + no parameter delta + drifted persisted signature -> must
    NOT adopt: in this narrow case the sig is the only source of truth about
    how the backend was launched, and a drifted sig means the daemon relaunched
    with settings this gateway does not know.  The backend is ALIVE, so the
    local lifecycle must NOT run either (second controller race on a live
    llama-server) -> fail closed."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.current_model = "m"  # gateway believes the same model is already loaded
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    mgr._config_drifted.assert_called_once_with(
        "m", enable_vision=None, context_hint=None
    )
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_param_delta_adopts_despite_stale_sig():
    """A parameter-delta request (explicit vision need / context hint) must not
    be vetoed by the persisted signature: the sig never described the launch
    this request asks for, so it says nothing useful — the serving, healthy
    backend is adopted (its launch is stale until mark_loaded_by_caretaker
    rewrites the sig AFTER adoption) and the explicit need is stamped."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.current_model = "m"
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)  # sig stale — irrelevant here
    mgr.current_runtime_uses_mmproj = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(
        model="m", enable_vision=True, context_hint=4096, local_fallback=fallback
    )
    assert result == "local-healthy"
    mgr._config_drifted.assert_not_called()
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=4096
    )


async def test_unavailable_cross_model_adopts_despite_stale_sig():
    """Cross-model adoption: the gateway believes another model is loaded, but
    the LIVE backend serves the requested one and is healthy.  The persisted
    signature describes the OTHER model's launch, so the drift check is
    meaningless and must not veto adoption."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.current_model = "other"  # gateway state describes another model
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)  # stale sig — must not run
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local-healthy"
    mgr._config_drifted.assert_not_called()
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_unavailable_hung_backend_runs_local_fallback():
    """Backend serves the model but is NOT healthy (hung) -> must NOT adopt;
    the backend is DOWN, so the local lifecycle is the sole controller."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    fallback.assert_awaited_once()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_healthy_backend_wrong_model_fails_closed():
    """Backend up + healthy but serving a different model -> must NOT adopt.
    The backend is ALIVE (a live daemon may surface as unreachable via
    firewall DROP/backlog while still owning llama-server): spawning locally
    would race a second controller -> fail closed."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_with_dead_backend_runs_local_fallback():
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()  # backend_health_ok False
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    fallback.assert_awaited_once()
    mgr.mark_loaded_by_caretaker.assert_not_called()


@pytest.mark.parametrize(
    "exc",
    [
        CaretakerModelNotFound("m"),
        CaretakerVramExceeded("m"),
        CaretakerModelLoadFailed("m"),
    ],
)
async def test_caretaker_errors_map_to_model_load_error(exc):
    client = _StubClient()
    client.ensure.side_effect = exc
    crt.init(model_manager=_manager(), caretaker_client=client)
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=AsyncMock())


async def test_model_load_failed_preserves_crash_telemetry():
    """The daemon's crash_details (its CrashRecord.to_dict()) must reach the
    raised ModelLoadError.crash_record so the hotpath crash recording keeps
    working unchanged on the remote primary path."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerModelLoadFailed(
        "m",
        crash_details={
            "timestamp": "2026-08-29T00:00:00+00:00",
            "model": "m",
            "error_message": "llama-server crashed",
            "exit_code": -11,
            "config_snapshot": {"ngl": 99},
        },
    )
    crt.init(model_manager=_manager(), caretaker_client=client)
    with pytest.raises(ModelLoadError) as err:
        await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    crash = err.value.crash_record
    assert crash is not None
    assert crash.model == "m"
    assert crash.error_message == "llama-server crashed"
    assert crash.exit_code == -11
    assert crash.config_snapshot == {"ngl": 99}


async def test_model_load_failed_without_details_keeps_no_crash():
    """A 503 without a crash_details dict must not crash the mapping — the
    ModelLoadError simply carries no crash_record."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerModelLoadFailed("m")
    crt.init(model_manager=_manager(), caretaker_client=client)
    with pytest.raises(ModelLoadError) as err:
        await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert err.value.crash_record is None


async def test_invalid_request_maps_to_value_error():
    client = _StubClient()
    client.ensure.side_effect = CaretakerInvalidRequest("bad payload")
    crt.init(model_manager=_manager(), caretaker_client=client)
    with pytest.raises(ValueError):
        await crt.ensure_backend(model="m", local_fallback=AsyncMock())


async def test_pre_switch_save_saves_context_before_ensure():
    client = _StubClient()
    client.supports_fresh_load = True
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    await crt.ensure_backend(
        model="m",
        pre_switch_save=True,
        local_fallback=AsyncMock(),
    )
    mgr.save_current_context.assert_awaited_once()


async def test_pre_switch_save_false_skips_context_save():
    client = _StubClient()
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    mgr.save_current_context.assert_not_awaited()


async def test_switch_guard_blocks_allowlist_excluded_client():
    """Defense-in-depth: a switch call site passing client_id is gated even if
    a future call site forgets the hotpath is_switch_allowed check."""
    client = _StubClient()
    mgr = _manager()
    mgr.is_switch_allowed = Mock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    with pytest.raises(ValueError, match="not allowed to switch models"):
        await crt.ensure_backend(
            model="m", client_id="blocked-client", local_fallback=AsyncMock()
        )
    client.ensure.assert_not_awaited()
    mgr.save_current_context.assert_not_awaited()


async def test_reload_without_client_id_not_gated():
    """Reload sites (same-model reload, no switch) omit client_id -> no gate."""
    client = _StubClient()
    mgr = _manager()
    mgr.is_switch_allowed = Mock(return_value=False)  # would block if gated
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "remote"
    client.ensure.assert_awaited_once()


async def test_remote_loaded_model_mismatch_fails_closed():
    """Caretaker resolved the request to a different model -> fail CLOSED with
    ModelLoadError (NOT local_fallback: the daemon is alive and owns the
    backend; the gateway must not become a second controller)."""
    client = _StubClient()
    client.ensure = AsyncMock(return_value={"ok": True, "loaded_model": "other-model"})
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_remote_loaded_model_alias_does_not_mismatch():
    """The mismatch comparison canonicalizes both sides: the caretaker
    reporting the canonical name for an alias we sent is NOT a mismatch (a
    raw string compare would have turned a legitimate load into a 503)."""
    client = _StubClient()
    client.ensure = AsyncMock(return_value={"ok": True, "loaded_model": "M"})
    mgr = _manager()
    # Case-insensitive resolver: "M" canonicalizes to "m".
    mgr.resolve_model = Mock(
        side_effect=lambda name: "m" if name.lower() == "m" else name
    )
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(model="m", local_fallback=AsyncMock())
    assert result == "remote"
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=False, context_hint=None
    )


async def test_remote_success_without_loaded_model_fails_closed():
    """A 200 without a truthful loaded_model must NOT be adopted blindly —
    never claim success from an unknown caretaker state (fail closed)."""
    client = _StubClient()
    client.ensure = AsyncMock(return_value={"ok": True})
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    fallback.assert_not_awaited()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_poll_windows_hot_reloadable_from_config(monkeypatch):
    """The adoption window comes from the live ``caretaker`` config section
    (F5 follow-up): patching the section — exactly what POST
    /api/config/reload does — retunes the poll without a restart, and the
    iteration cap equals the configured seconds (deadline and cap share one
    read)."""
    from app.config_loader import CONFIG, load_caretaker_runtime_config

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setitem(
        CONFIG, "caretaker", dict(load_caretaker_runtime_config(), adopt_poll_seconds=5)
    )
    client = _StubClient()
    client.ensure.side_effect = [_timeout_unavailable(), _timeout_unavailable()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)  # healthy, other model
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    with pytest.raises(ModelLoadError):
        await crt.ensure_backend(model="m", local_fallback=fallback)
    # The configured window — not the 120s module default — bounded the poll.
    assert mgr.backend_serves_model.await_count == 5
    fallback.assert_not_awaited()


async def test_rebind_poll_window_from_config(monkeypatch):
    """The re-bind poll attempts/interval come from the live ``caretaker``
    config section: a reduced attempts count shortens the poll and the sleep
    receives the configured interval."""
    from app.config_loader import CONFIG, load_caretaker_runtime_config

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", _record_sleep)
    monkeypatch.setitem(
        CONFIG,
        "caretaker",
        dict(load_caretaker_runtime_config(), rebind_poll_attempts=2, rebind_poll_interval_seconds=0.25),
    )
    client = _StubClient()

    def _refused() -> CaretakerUnavailable:
        err = CaretakerUnavailable("http://x:11441")
        err.__cause__ = httpx.ConnectError("connection refused")
        return err

    client.ensure.side_effect = [_refused(), _refused(), _refused(), _refused()]
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    crt._ever_reached_caretaker = True  # daemon was up before the restart window
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    # 2 initial probes + 2 configured re-bind attempts, then the local
    # lifecycle (daemon never re-bound within the configured window).
    assert result == "local"
    assert client.ensure.await_count == 4
    assert fallback.await_count == 1
    assert sleeps == [0.25, 0.25], "configured interval, not the hardcoded 1s"
