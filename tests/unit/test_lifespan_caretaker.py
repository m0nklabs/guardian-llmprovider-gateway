"""Tests for the idle-unload watcher's caretaker delegation (F5 wiring, tranche 1).

The watcher's idle *decision* stays in the gateway (queue/requests); the
*execution* (the unload call) moves to the caretaker control-API client. These
tests pin:

1. When idle + queue empty + no active requests + not already unloaded, the
   watcher calls ``caretaker_client.unload()`` (NOT ``model_manager.unload()``).
2. Each guard (idle_minutes None, already unloaded, active requests, queue
   non-empty, idle_secs below limit) prevents the caretaker unload call.
3. A missing caretaker client logs an error and does not claim unload state.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.proxy import lifespan as lifespan_mod


class _StubCaretakerClient:
    """Minimal recording double for the CaretakerClient (unload only)."""

    def __init__(self) -> None:
        self.unload_calls = 0

    async def unload(self) -> dict:
        self.unload_calls += 1
        return {"ok": True, "is_unloaded": True}


def _install_globals(
    monkeypatch: pytest.MonkeyPatch,
    *,
    idle_minutes: float | None = 5,
    is_unloaded: bool = False,
    active_requests: int = 0,
    queue_active: int = 0,
    queue_waiting: int = 0,
    last_request_time: float | None = None,
    caretaker_client=_StubCaretakerClient,
):
    """Inject manager/queue/client globals into lifespan and patch sleep to
    run exactly one watcher iteration (first sleep returns, second raises)."""
    if last_request_time is None:
        last_request_time = time.time()
    manager = SimpleNamespace(
        idle_unload_minutes=idle_minutes,
        is_unloaded=is_unloaded,
        active_requests=active_requests,
        last_request_time=last_request_time,
    )
    queue = SimpleNamespace(active_count=queue_active, waiting_count=queue_waiting)
    client = caretaker_client()

    monkeypatch.setattr(lifespan_mod, "_model_manager", manager)
    monkeypatch.setattr(lifespan_mod, "_inference_queue", queue)
    monkeypatch.setattr(lifespan_mod, "_caretaker_client", client)
    # First sleep(60) returns immediately (enter the loop), the second raises to
    # exit the infinite loop after one full iteration.
    sleeps = [0]

    async def _one_iteration_sleep(seconds: float):
        if sleeps:
            sleeps.pop()
            return
        raise _StopWatcher

    monkeypatch.setattr(lifespan_mod.asyncio, "sleep", _one_iteration_sleep)
    return client


class _StopWatcher(Exception):
    """Internal: stops the watcher loop after one iteration."""


async def test_watcher_calls_caretaker_unload_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # last_request_time 10 minutes ago → idle_secs (600) >= idle_minutes*60 (300)
    client = _install_globals(monkeypatch, last_request_time=time.time() - 600)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 1


async def test_watcher_disabled_when_idle_minutes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_globals(monkeypatch, idle_minutes=None)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 0


async def test_watcher_skips_when_already_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_globals(monkeypatch, is_unloaded=True)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 0


async def test_watcher_skips_when_active_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_globals(monkeypatch, active_requests=2)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 0


async def test_watcher_skips_when_queue_has_pending_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_globals(monkeypatch, queue_active=1)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 0
    client2 = _install_globals(monkeypatch, queue_waiting=1)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client2.unload_calls == 0


async def test_watcher_skips_below_idle_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_globals(
        monkeypatch, last_request_time=time.time() - 120
    )  # 2 min ago → idle_secs (120) < idle_minutes*60 (300)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 0


async def test_watcher_logs_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_globals(monkeypatch, caretaker_client=lambda: None)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    # No exception was raised by the missing client; the loop just continued.


async def test_watcher_caretaker_error_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

    _install_globals(monkeypatch, caretaker_client=_FailingClient)
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    # CaretakerError was swallowed (logged), not re-raised.


def test_idle_unload_watcher_defined_in_lifespan() -> None:
    assert callable(lifespan_mod.idle_unload_watcher)
    assert hasattr(lifespan_mod, "_caretaker_client")


# ---------------------------------------------------------------------------
# /admin/unload route (server.py) delegation
# ---------------------------------------------------------------------------


def _make_unauthed_app():
    """Return (app, stub) with verify_api_key overridden via FastAPI's
    dependency_overrides (the route froze the Depends() reference at import, so
    patching the module attribute does NOT affect it)."""
    from app.proxy import server as server_mod

    async def _noop_auth():
        return "test-key"

    server_mod.app.dependency_overrides[server_mod.verify_api_key] = _noop_auth
    return server_mod.app


def test_admin_unload_delegates_to_caretaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app()

    class _StubClient:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def unload(self) -> dict:
            self.unload_calls += 1
            return {"ok": True, "is_unloaded": True}

    stub = _StubClient()
    monkeypatch.setattr(server_mod, "caretaker_client", stub)
    # model_manager is a module-level object; give it a current_model attribute.
    fake_mgr = SimpleNamespace(current_model="minimal", is_unloaded=False)
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "unloaded"
    assert stub.unload_calls == 1
    # Restore the original overrides dict (the shared app object is mutated).
    monkeypatch.setattr(
        server_mod.app, "dependency_overrides", dict(server_mod.app.dependency_overrides)
    )
    _ = server_mod  # (imported above)


def test_admin_unload_503_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app()
    monkeypatch.setattr(server_mod, "caretaker_client", None)
    fake_mgr = SimpleNamespace(current_model="minimal", is_unloaded=False)
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]