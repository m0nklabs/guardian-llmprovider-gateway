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


class _FakeModelManager:
    """Minimal manager double with the unload-bookkeeping method the watcher
    now reconciles through (mark_unloaded_by_caretaker)."""

    def __init__(
        self,
        *,
        idle_unload_minutes: float | None,
        is_unloaded: bool,
        active_requests: int,
        last_request_time: float,
    ) -> None:
        self.idle_unload_minutes = idle_unload_minutes
        self.is_unloaded = is_unloaded
        self.active_requests = active_requests
        self.last_request_time = last_request_time
        self.mark_unloaded_calls = 0
        self.local_unload_calls = 0
        self._model_verified = True
        self._last_verification_at = "2026-08-29T00:00:00Z"
        self._last_backend_model = "glm-4.7"

    def mark_unloaded_by_caretaker(self) -> None:
        self.is_unloaded = True
        self._model_verified = False
        self._last_verification_at = None
        self._last_backend_model = None
        self.mark_unloaded_calls += 1

    def snapshot_unload_state(self) -> dict:
        return {
            "is_unloaded": self.is_unloaded,
            "model_verified": self._model_verified,
            "last_verification_at": self._last_verification_at,
            "last_backend_model": self._last_backend_model,
        }

    def rollback_unload_state(
        self,
        *,
        is_unloaded: bool,
        model_verified: bool,
        last_verification_at: str | None,
        last_backend_model: str | None,
    ) -> None:
        self.is_unloaded = is_unloaded
        self._model_verified = model_verified
        self._last_verification_at = last_verification_at
        self._last_backend_model = last_backend_model

    def rollback_unload_if_unchanged(self, prev_state: dict) -> bool:
        still_optimistic = (
            self.is_unloaded is True
            and self._model_verified is False
            and self._last_verification_at is None
            and self._last_backend_model is None
        )
        if still_optimistic:
            self.rollback_unload_state(**prev_state)
            return True
        return False

    async def unload(self) -> None:
        """Local fallback unload (no caretaker configured)."""
        self.is_unloaded = True
        self.local_unload_calls += 1


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
    manager = _FakeModelManager(
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


async def test_watcher_concurrent_reload_during_roundtrip_logs_not_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible race): a hotpath reload completing DURING the
    caretaker unload round-trip flips the manager state back to loaded.  The
    watcher must NOT clobber it (no rollback, no local unload) and must NOT
    crash — it logs the uncertain backend state and keeps running."""
    class _ReloadDuringCallClient:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def unload(self) -> dict:
            self.unload_calls += 1
            # Concurrent reload completes mid-round-trip: loaded again.
            mgr = lifespan_mod._model_manager
            mgr.is_unloaded = False
            mgr._model_verified = True
            mgr._last_verification_at = "2026-08-29T01:00:00Z"
            mgr._last_backend_model = "glm-4.7"
            return {"ok": True, "is_unloaded": True}

    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,
        caretaker_client=_ReloadDuringCallClient,
    )
    mgr = lifespan_mod._model_manager
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    # The concurrent reload state was preserved, not rolled back or replaced.
    assert mgr.is_unloaded is False
    assert mgr._model_verified is True
    assert mgr.local_unload_calls == 0


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


async def test_watcher_falls_back_to_local_unload_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (deployment dependency): without a caretaker client the
    watcher must still free VRAM via the local unload — never silently stop."""
    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,  # idle → branch reached
        caretaker_client=lambda: None,
    )
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    mgr = lifespan_mod._model_manager
    assert mgr.is_unloaded is True
    assert mgr.local_unload_calls == 1
    assert mgr.mark_unloaded_calls == 0  # caretaker path not taken


async def test_watcher_syncs_is_unloaded_after_successful_unload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix 4: after the caretaker confirms the unload, the gateway-local
    manager flag is synced so the watcher guard stops re-issuing /unload and the
    hotpath auto-reload fires on the next request."""
    client = _install_globals(
        monkeypatch, last_request_time=time.time() - 600
    )  # idle
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert client.unload_calls == 1
    mgr = lifespan_mod._model_manager
    assert mgr.is_unloaded is True
    assert mgr.mark_unloaded_calls == 1


async def test_watcher_unavailable_then_local_fallback_failure_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible bug): with a caretaker unavailable AND the local
    fallback unload raising, the watcher task must NOT die — the fallback is
    wrapped in its own try/except (an exception inside an except-handler is not
    caught by the sibling except Exception)."""
    class _UnavailableClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

    class _FailingLocalManager(_FakeModelManager):
        async def unload(self) -> None:
            self.local_unload_calls += 1
            raise RuntimeError("systemctl stop failed")

    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,
        caretaker_client=_UnavailableClient,
    )
    failing = _FailingLocalManager(
        idle_unload_minutes=5,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time() - 600,
    )
    monkeypatch.setattr(lifespan_mod, "_model_manager", failing)

    # The watcher runs a full iteration and STOPS via our sleep-sentinel
    # (_StopWatcher): it did not die on the fallback failure.
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    assert failing.local_unload_calls == 1
    # The fallback failed so is_unloaded stayed False (rollback kept it too).
    assert failing.is_unloaded is False


async def test_watcher_fallback_unload_error_is_logged_not_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible bug): when the local fallback unload() raises and
    NO snapshot was taken, the except-handler must not reference _prev_state
    (UnboundLocalError, which would kill the watcher) — it just logs."""
    class _FailingLocalManager(_FakeModelManager):
        async def unload(self) -> None:
            self.local_unload_calls += 1
            raise RuntimeError("systemctl stop failed")

    # Build the usual manager then swap in the failing local-unload one.
    original_install = _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,
        caretaker_client=lambda: None,
    )
    del original_install
    failing = _FailingLocalManager(
        idle_unload_minutes=5,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time() - 600,
    )
    monkeypatch.setattr(lifespan_mod, "_model_manager", failing)

    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    # Watcher survived; local unload attempted, nothing rolled back (no mark).
    mgr = lifespan_mod._model_manager
    assert mgr.local_unload_calls == 1
    assert mgr.is_unloaded is False
    assert mgr.mark_unloaded_calls == 0


async def test_watcher_rolls_back_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible bug): a non-CaretakerError failure (transport /
    coding) means the unload was NOT confirmed — the optimistic mark must be
    rolled back too, or is_unloaded stays True over a running backend and
    every later unload attempt is skipped."""
    class _TransportFailingClient:
        async def unload(self) -> dict:
            raise RuntimeError("connection reset mid-call")

    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,
        caretaker_client=_TransportFailingClient,
    )
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    mgr = lifespan_mod._model_manager
    assert mgr.is_unloaded is False  # rolled back (unconfirmed)
    assert mgr._model_verified is True   # verification restored
    assert mgr.mark_unloaded_calls == 1


async def test_watcher_never_rolls_back_a_concurrent_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible race): if a request starts a reload during the
    round-trip, the state is no longer the optimistic mark — the stale snapshot
    must not clobber the fresh (loaded) state."""
    class _MutatingFailingClient:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def unload(self) -> dict:
            self.unload_calls += 1
            # Simulate a concurrent hotpath reload completing mid-call.
            mgr = lifespan_mod._model_manager
            mgr.is_unloaded = False
            mgr._model_verified = True
            mgr._last_verification_at = "2026-08-29T00:10:00Z"
            mgr._last_backend_model = "glm-4.7-other"
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,
        caretaker_client=_MutatingFailingClient,
    )
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    mgr = lifespan_mod._model_manager
    # Fresh reload state preserved — NOT overwritten by the stale snapshot.
    assert mgr.is_unloaded is False
    assert mgr._model_verified is True
    assert mgr._last_verification_at == "2026-08-29T00:10:00Z"
    assert mgr._last_backend_model == "glm-4.7-other"


async def test_watcher_falls_back_to_local_unload_on_caretaker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible regression): a transport failure
    (CaretakerUnavailable) means we cannot know whether the unload happened —
    the watcher restores the optimistic state and falls back to the idempotent
    local unload so VRAM is still freed (daemon not deployed yet / crashed)."""
    class _FailingClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,
        caretaker_client=_FailingClient,
    )
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    mgr = lifespan_mod._model_manager
    # Optimistic mark fired, then rolled back and replaced by the local unload.
    assert mgr.mark_unloaded_calls == 1
    assert mgr.local_unload_calls == 1
    assert mgr.is_unloaded is True  # local unload put it in the unloaded state


async def test_watcher_rolls_back_and_logs_on_definitive_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix: a DEFINITIVE refusal (e.g. CaretakerUnloadFailed — the
    caretaker is up but explicitly refused) is not a transport failure: no
    local fallback, just roll back the optimistic mark and log."""
    class _RefusingClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnloadFailed

            raise CaretakerUnloadFailed()

    _install_globals(
        monkeypatch,
        last_request_time=time.time() - 600,  # idle → error path reached
        caretaker_client=_RefusingClient,
    )
    with pytest.raises(_StopWatcher):
        await lifespan_mod.idle_unload_watcher()
    mgr = lifespan_mod._model_manager
    assert mgr.mark_unloaded_calls == 1  # optimistic mark fired
    assert mgr.local_unload_calls == 0   # NO local fallback on a refusal
    assert mgr.is_unloaded is False      # mark was rolled back
    assert mgr._model_verified is True   # verification metadata restored


def test_idle_unload_watcher_defined_in_lifespan() -> None:
    assert callable(lifespan_mod.idle_unload_watcher)
    assert hasattr(lifespan_mod, "_caretaker_client")


# ---------------------------------------------------------------------------
# /admin/unload route (server.py) delegation
# ---------------------------------------------------------------------------


def _make_unauthed_app(monkeypatch: pytest.MonkeyPatch):
    """Return the shared app with verify_api_key overridden via FastAPI's
    dependency_overrides.

    The route froze the ``Depends()`` reference at import, so patching the
    module attribute does NOT affect it.  We replace the whole overrides dict
    through ``monkeypatch.setattr`` so pytest restores the original dict
    automatically at teardown (no leaking auth override into later tests).
    """
    from app.proxy import server as server_mod

    async def _noop_auth():
        return "test-key"

    monkeypatch.setattr(
        server_mod.app,
        "dependency_overrides",
        {server_mod.verify_api_key: _noop_auth},
    )
    return server_mod.app


def test_admin_unload_delegates_to_caretaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _StubClient:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def status(self) -> dict:
            # The caretaker has nothing loaded — the already_unloaded shortcut
            # on the second call may trust it.
            return {"is_unloaded": True, "loaded_model": None}

        async def unload(self) -> dict:
            self.unload_calls += 1
            return {"ok": True, "is_unloaded": True}

    stub = _StubClient()
    monkeypatch.setattr(server_mod, "caretaker_client", stub)
    # model_manager is a module-level object; reconcile through the same
    # unload-bookkeeping method the production manager exposes.
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "unloaded"
    assert stub.unload_calls == 1
    # Review fix 5: the confirmed unload is mirrored back into the
    # gateway-local manager, so a repeat /admin/unload reports
    # already_unloaded via the guard — no redundant idempotent caretaker call.
    assert fake_mgr.is_unloaded is True
    r2 = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_unloaded"
    assert stub.unload_calls == 1  # unchanged


def test_admin_unload_falls_back_to_local_unload_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (deployment dependency): /admin/unload must still free VRAM
    when no caretaker client is configured — local fallback, not a 503."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)
    monkeypatch.setattr(server_mod, "caretaker_client", None)
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unloaded"
    assert fake_mgr.local_unload_calls == 1
    assert fake_mgr.is_unloaded is True


def test_admin_unload_falls_back_to_local_unload_on_caretaker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible regression): /admin/unload with a caretaker that
    is UP but unreachable (transport) must fall back to the idempotent local
    unload instead of a 503 — the operator can always free VRAM."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _UnreachableClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

    monkeypatch.setattr(server_mod, "caretaker_client", _UnreachableClient())
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unloaded"
    assert fake_mgr.local_unload_calls == 1
    assert fake_mgr.is_unloaded is True


def test_admin_unload_preserves_concurrent_reload_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible race): if a concurrent reload completes during the
    /admin/unload round-trip, marking after the fact would clobber the fresh
    loaded state — the optimistic mark + guarded rollback must leave it alone."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _ReloadDuringCallClient:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def unload(self) -> dict:
            self.unload_calls += 1
            # Simulate a concurrent hotpath reload completing mid-round-trip.
            mgr = server_mod.model_manager
            mgr.is_unloaded = False
            mgr._model_verified = True
            mgr._last_verification_at = "2026-08-29T00:10:00Z"
            mgr._last_backend_model = "glm-4.7-other"
            return {"ok": True, "is_unloaded": True}

    monkeypatch.setattr(server_mod, "caretaker_client", _ReloadDuringCallClient())
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    # Fresh reload state preserved — NOT clobbered by the post-fact mark.
    assert fake_mgr.is_unloaded is False
    assert fake_mgr._model_verified is True
    assert fake_mgr._last_backend_model == "glm-4.7-other"


def test_admin_unload_stale_flag_still_unloads_when_caretaker_has_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (focus-area, stale local flag): with the F5 split the
    gateway-local is_unloaded can be stale — the caretaker (independent owner)
    may have a model loaded via a direct /ensure call.  A subsequent
    /admin/unload must then still free VRAM, not report already_unloaded."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _Client:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def status(self) -> dict:
            return {"is_unloaded": False, "loaded_model": "minimal"}

        async def unload(self) -> dict:
            self.unload_calls += 1
            return {"ok": True, "is_unloaded": True}

    client_stub = _Client()
    monkeypatch.setattr(server_mod, "caretaker_client", client_stub)
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=True,  # stale: gateway thinks unloaded, caretaker disagrees
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unloaded"
    assert client_stub.unload_calls == 1  # delegated to the caretaker


def test_admin_unload_keeps_already_unloaded_when_caretaker_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix: when the caretaker confirms it has nothing loaded, the
    already_unloaded shortcut stays (no redundant remote call)."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _Client:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def status(self) -> dict:
            return {"is_unloaded": True, "loaded_model": None}

        async def unload(self) -> dict:
            self.unload_calls += 1
            return {"ok": True, "is_unloaded": True}

    client_stub = _Client()
    monkeypatch.setattr(server_mod, "caretaker_client", client_stub)
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=True,
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "already_unloaded"
    assert client_stub.unload_calls == 0


def test_admin_unload_unknown_status_unloads_instead_of_already_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible issue): when the local flag is stale-True and the
    caretaker /status is unavailable (daemon down / transport), the route must
    fail safe — attempt the idempotent unload instead of claiming
    already_unloaded while the backend may still hold VRAM."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _StatusDownClient:
        def __init__(self) -> None:
            self.unload_calls = 0

        async def status(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

        async def unload(self) -> dict:
            self.unload_calls += 1
            return {"ok": True, "is_unloaded": True}

    client_stub = _StatusDownClient()
    monkeypatch.setattr(server_mod, "caretaker_client", client_stub)
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=True,  # stale
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unloaded"  # NOT already_unloaded
    assert client_stub.unload_calls == 1


def test_admin_unload_503_when_unavailable_and_local_fallback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible bug): caretaker unavailable AND the local fallback
    unload failing surfaces as a clear 503 (with cause), not a raw escaping
    500 / empty detail."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _UnavailableClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnavailable

            raise CaretakerUnavailable("http://127.0.0.1:11441")

    monkeypatch.setattr(server_mod, "caretaker_client", _UnavailableClient())

    class _FailingLocalManager(_FakeModelManager):
        async def unload(self) -> None:
            self.local_unload_calls += 1
            raise RuntimeError("systemctl stop failed")

    failing = _FailingLocalManager(
        idle_unload_minutes=None,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time(),
    )
    failing.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", failing)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 503, r.text
    assert "local fallback unload failed" in r.json()["detail"]
    assert failing.local_unload_calls == 1


def test_admin_unload_503_on_definitive_caretaker_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix: a DEFINITIVE refusal (caretaker up but explicitly refused)
    still surfaces as 503 — no local fallback for a real rejection."""
    from fastapi.testclient import TestClient

    from app.proxy import server as server_mod

    app = _make_unauthed_app(monkeypatch)

    class _RefusingClient:
        async def unload(self) -> dict:
            from app.gateway.caretaker_client import CaretakerUnloadFailed

            raise CaretakerUnloadFailed()

    monkeypatch.setattr(server_mod, "caretaker_client", _RefusingClient())
    fake_mgr = _FakeModelManager(
        idle_unload_minutes=None,
        is_unloaded=False,
        active_requests=0,
        last_request_time=time.time(),
    )
    fake_mgr.current_model = "minimal"
    monkeypatch.setattr(server_mod, "model_manager", fake_mgr)

    client = TestClient(app)
    r = client.post("/admin/unload", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 503, r.text
    # The default message is now set — detail is not empty.
    assert "unload_failed" in r.json()["detail"]
    assert fake_mgr.local_unload_calls == 0
    assert fake_mgr.is_unloaded is False