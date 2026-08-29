"""Tests for app.gateway.caretaker_runtime — F5 remote-first hotpath ensure.

Pins the decision logic of the request-path lifecycle bridge:
caretaker /ensure first, gateway ModelManager fallback when the caretaker is
not configured or unreachable, and error mapping so the hotpath callers keep
their existing crash/503 handling.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

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


async def test_remote_switch_restores_target_context():
    """Switch sites (pre_switch_save=True) mirror switch_model's restore."""
    client = _StubClient()
    mgr = _manager()
    crt.init(model_manager=mgr, caretaker_client=client)
    result = await crt.ensure_backend(
        model="m", pre_switch_save=True, local_fallback=AsyncMock()
    )
    assert result == "remote"
    mgr.restore_current_context.assert_awaited_once()


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
    mgr.backend_serves_model.assert_awaited_once_with("m")
    mgr.backend_health_ok.assert_awaited_once_with()
    mgr._config_drifted.assert_called_once_with(
        "m", enable_vision=None, context_hint=None
    )
    mgr.mark_loaded_by_caretaker.assert_called_once_with(
        "m", enable_vision=None, context_hint=None
    )
    fallback.assert_not_awaited()
    # Reload adopt (no pre_switch_save) must not restore.
    mgr.restore_current_context.assert_not_awaited()


async def test_unavailable_adopt_switch_restores_context():
    """Adopt path with a switch (pre_switch_save=True) mirrors the remote
    restore: a freshly (re)started backend serving the target model must
    recover its auto-saved session context."""
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
    mgr.restore_current_context.assert_awaited_once()


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
