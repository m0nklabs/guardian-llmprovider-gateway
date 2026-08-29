"""Tests for app.gateway.caretaker_runtime — F5 remote-first hotpath ensure.

Pins the decision logic of the request-path lifecycle bridge:
caretaker /ensure first, gateway ModelManager fallback when the caretaker is
not configured or unreachable, and error mapping so the hotpath callers keep
their existing crash/503 handling.
"""

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
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m",
        enable_vision=True,
        context_hint=4096,
        local_fallback=AsyncMock(),
    )
    assert result == "remote"
    client.ensure.assert_awaited_once_with(
        "m", enable_vision=True, context_hint=4096
    )
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=True, context_hint=4096
    )
    # Reload sites (no pre_switch_save) start a fresh context — restore must
    # NOT run (mirrors load(), which does not restore).
    mgr.restore_current_context.assert_not_awaited()


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
        "m", enable_vision=None, context_hint=None
    )


async def test_unavailable_timeout_twice_fails_closed():
    """Two consecutive timeouts mean the daemon accepted the connection but is
    unresponsive (still mid-switch — a load can outlast the client timeout
    more than once).  Spawning locally would create a second controller on a
    live daemon's backend -> fail closed with ModelLoadError."""
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


async def test_unavailable_refused_then_timeout_fails_closed():
    """First probe connection-refused (daemon restarting, gone at that
    instant), re-probe times out (daemon came back but is busy): the local
    lifecycle must NOT run — the daemon is alive -> fail closed."""
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


async def test_remote_switch_restores_target_context():
    """Switch sites (pre_switch_save=True) on a FRESH load mirror switch_model's
    restore (backend did not serve the model before the ensure)."""
    client = _StubClient()
    mgr = _manager()  # backend_serves_model False -> fresh_load True
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.restore_current_context.assert_awaited_once()


async def test_remote_idempotent_ensure_does_not_restore():
    """Idempotent /ensure (backend already served the model before) must NOT
    restore — the slot already holds the live session and restoring the stale
    auto-save would clobber it."""
    client = _StubClient()
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)  # fresh_load False
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.restore_current_context.assert_not_awaited()


async def test_remote_ensure_fresh_load_field_overrides_probe():
    """The daemon's fresh_load field overrides the probe: backend_serves_model
    misdetects an already-serving backend (probe says NOT fresh) but /ensure
    reports the model was freshly loaded -> restore (empty slot)."""
    client = _StubClient()
    client.ensure = AsyncMock(
        return_value={"ok": True, "loaded_model": "m", "fresh_load": True}
    )
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)  # probe: not fresh
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.restore_current_context.assert_awaited_once()


async def test_remote_ensure_fresh_load_false_skips_restore():
    """The daemon contradicts the probe: /ensure says the load was idempotent
    (fresh_load False) even though the probe thought it was fresh -> no
    restore (live session authoritative)."""
    client = _StubClient()
    client.ensure = AsyncMock(
        return_value={"ok": True, "loaded_model": "m", "fresh_load": False}
    )
    mgr = _manager()  # backend_serves_model False -> probe says fresh
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
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local-healthy"
    # backend_serves_model runs once for the fresh_load probe and once in the
    # adopt condition.
    mgr.backend_serves_model.assert_awaited_with("m")
    mgr.backend_health_ok.assert_awaited_once_with()
    mgr._config_drifted.assert_called_once_with(
        "m", enable_vision=None, context_hint=None
    )
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=None, context_hint=None
    )
    fallback.assert_not_awaited()
    # Adopt never restores: the backend was already serving the model, so the
    # live session in slot 0 is authoritative (no clobbering).
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_adopt_never_restores():
    """Adoption with an already-serving backend (fresh_load False) never
    restores — even for a switch: the live session in slot 0 is
    authoritative."""
    client = _StubClient()
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


async def test_unavailable_adopt_freshly_loaded_switch_restores():
    """A timed-out /ensure may have COMPLETED the switch (backend only started
    serving the model during the ensure): fresh_load True, adopt succeeds, and
    restoring the auto-saved context mirrors switch_model (empty slot)."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    # 1st call = fresh_load probe (before ensure) -> False -> fresh_load True;
    # 2nd call = adopt condition -> True.
    mgr.backend_serves_model = AsyncMock(side_effect=[False, True])
    mgr.backend_health_ok = AsyncMock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "local-healthy"
    mgr.restore_current_context.assert_awaited_once()


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


async def test_unavailable_drifted_config_runs_local_fallback():
    """Backend serves the model + healthy, but the requested launch config
    (vision/context_hint) drifts from the persisted one -> must NOT adopt; the
    drift-checked local fallback must reload instead."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=True)
    mgr.backend_health_ok = AsyncMock(return_value=True)
    mgr._config_drifted = Mock(return_value=True)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(
        model="m",
        enable_vision=True,
        context_hint=4096,
        local_fallback=fallback,
    )
    assert result == "local"
    fallback.assert_awaited_once()
    mgr.mark_loaded_by_caretaker.assert_not_called()


async def test_unavailable_hung_backend_runs_local_fallback():
    """Backend serves the model but is NOT healthy (hung) -> must NOT adopt."""
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


async def test_unavailable_healthy_backend_wrong_model_runs_fallback():
    """Backend up but serving a different model must NOT adopt loaded state."""
    client = _StubClient()
    client.ensure.side_effect = CaretakerUnavailable("http://x:11441")
    mgr = _manager()
    mgr.backend_serves_model = AsyncMock(return_value=False)
    crt.init(model_manager=mgr, caretaker_client=client)
    fallback = AsyncMock()
    result = await crt.ensure_backend(model="m", local_fallback=fallback)
    assert result == "local"
    fallback.assert_awaited_once()
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


async def test_invalid_request_maps_to_value_error():
    client = _StubClient()
    client.ensure.side_effect = CaretakerInvalidRequest("bad payload")
    crt.init(model_manager=_manager(), caretaker_client=client)
    with pytest.raises(ValueError):
        await crt.ensure_backend(model="m", local_fallback=AsyncMock())


async def test_pre_switch_save_saves_context_before_ensure():
    client = _StubClient()
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
        "m", enable_vision=None, context_hint=None
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
