"""Issue #34 — caretaker-reported backend deaths (the out-of-band death signal).

Pins the three behaviours the issue requires:

1. signal present → the death becomes a ``CrashRecord`` with its cause;
2. signal absent (older daemon) → behaviour unchanged, no record, no new
   requirement;
3. daemon unreachable → ``unknown``, never a false "alive"/"healthy".

Plus the discriminability requirement: a load-failure crash and a reported
death stay distinguishable in ``/api/crashes``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.engine.manager import (
    CRASH_ORIGIN_LOAD_FAILURE,
    CRASH_ORIGIN_REPORTED_DEATH,
    CrashRecord,
)
from app.gateway import admin_api, backend_death
from app.gateway.backend_death import (
    BACKEND_STATE_ALIVE,
    BACKEND_STATE_DEAD,
    BACKEND_STATE_UNKNOWN,
    BACKEND_STATE_UNLOADED,
    DEATH_SIGNAL_EMPTY,
    DEATH_SIGNAL_MALFORMED,
    DEATH_SIGNAL_NOT_CONFIGURED,
    DEATH_SIGNAL_REPORTED,
    DEATH_SIGNAL_UNAVAILABLE,
    DEATH_SIGNAL_UNSUPPORTED,
    BackendDeathObserver,
    CaretakerObservation,
    derive_backend_state,
    parse_death_signal,
    status_fields,
)
from app.gateway.caretaker_client import CaretakerUnavailable
from tests.unit.test_manager import _make_manager

DEATH_TS = "2026-10-28T21:14:03+00:00"


@pytest.fixture(autouse=True)
def _isolate_runtime_state_files(tmp_path, monkeypatch):
    """Keep the temp ModelManager away from the real repo runtime-state files."""
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        "app.engine.manager.CURRENT_MODEL_ARGS_FILE", config_dir / "current_model.args"
    )
    monkeypatch.setattr(
        "app.engine.manager.CURRENT_MODEL_ENV_FILE", config_dir / "current_model.env"
    )
    monkeypatch.setattr(
        "app.engine.manager.CURRENT_MODEL_SIG_FILE", config_dir / "current_model.sig"
    )


class _StubCaretaker:
    """Minimal caretaker client: returns a status body or raises."""

    def __init__(self, status: dict | None = None, error: Exception | None = None) -> None:
        self._status = status
        self._error = error
        self.calls = 0
        self.timeouts: list[float | None] = []

    async def status(self, *, timeout: float | None = None) -> dict:
        self.calls += 1
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        return self._status or {}


def _death_payload(**overrides) -> dict:
    payload = {
        "cause": "oom",
        "model": "Qwen3-30B-A3B",
        "timestamp": DEATH_TS,
        "exit_code": 137,
        "signal": "SIGKILL",
        "detail": "killed by the kernel OOM killer",
    }
    payload.update(overrides)
    return payload


async def _fields(
    observer: BackendDeathObserver, *, probe_healthy: bool, gateway_unloaded: bool = False
) -> dict:
    """Mirror the admin API's composition: observe, then build the fields."""
    observation = await observer.observe()
    return status_fields(
        observation,
        probe_healthy=probe_healthy,
        gateway_unloaded=gateway_unloaded,
        last_report=observer.last_report,
    )


def _install_admin_runtime(monkeypatch, mgr, observer, *, probe_healthy: bool) -> None:
    """Point the admin API at fakes for everything ``get_server_status`` reads."""
    monkeypatch.setattr(admin_api, "_model_manager", mgr)
    monkeypatch.setattr(
        admin_api, "_get_startup_check_status", lambda: {"_state": "idle", "phase": "idle"}
    )
    monkeypatch.setattr(admin_api, "_startup_state_is_in_progress", lambda state: False)
    monkeypatch.setattr(
        admin_api, "_inference_queue", SimpleNamespace(get_status=lambda: {})
    )
    monkeypatch.setattr(admin_api, "_get_gpu_metrics", lambda: {})
    monkeypatch.setattr(admin_api, "_get_model_size", lambda model: 0)
    monkeypatch.setattr(admin_api, "_get_proxy_listener_info", lambda: {})
    monkeypatch.setattr(admin_api, "_get_pid_file_status", lambda: {})
    monkeypatch.setattr(admin_api, "_proxy_port", 11434)
    monkeypatch.setattr(admin_api, "_llama_server_url", "http://127.0.0.1:11440")
    monkeypatch.setattr(
        admin_api, "_state", SimpleNamespace(scaler=SimpleNamespace(config={}))
    )
    monkeypatch.setattr(
        admin_api, "_model_switch_lock", SimpleNamespace(locked=lambda: False)
    )
    monkeypatch.setattr(admin_api, "_caretaker_death_observer", observer)
    monkeypatch.setattr(
        admin_api, "_probe_backend_health", AsyncMock(return_value=probe_healthy)
    )
    monkeypatch.setattr(mgr, "_get_backend_model_path", lambda: None)


# ── 1. Signal present → CrashRecord with its cause ─────────────────────


async def test_reported_death_becomes_crash_record_with_cause(tmp_path):
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker(
        {"loaded_model": None, "is_unloaded": False, "last_death": _death_payload()}
    )
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    observation = await observer.observe()

    assert observation.reachable is True
    assert observation.death_signal == DEATH_SIGNAL_REPORTED
    assert observation.death is not None
    assert observation.death.cause == "oom"

    assert len(mgr.crash_history) == 1
    record = mgr.crash_history[0]
    assert record.origin == CRASH_ORIGIN_REPORTED_DEATH
    assert record.cause == "oom"
    assert record.model == "Qwen3-30B-A3B"
    assert record.timestamp == DEATH_TS
    assert record.exit_code == 137
    assert "cause=oom" in record.error_message
    assert "signal=SIGKILL" in record.error_message
    assert "killed by the kernel OOM killer" in record.error_message
    assert mgr.last_crash is record
    assert mgr.last_reported_death is record


async def test_reported_death_is_recorded_once_per_death(tmp_path):
    """The daemon keeps reporting its last death; the history records events."""
    mgr = _make_manager(tmp_path)
    payload = _death_payload()
    client = _StubCaretaker({"last_death": payload})
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    await observer.observe()
    await observer.observe()
    assert len(mgr.crash_history) == 1

    # A genuinely new death (new timestamp) is a new record.
    client._status = {"last_death": _death_payload(timestamp="2026-10-28T22:00:00+00:00")}
    await observer.observe()
    assert len(mgr.crash_history) == 2

    # An explicit event_id wins over field equality: same id, new timestamp.
    client._status = {
        "last_death": _death_payload(event_id="evt-7", timestamp="2026-10-28T23:00:00+00:00")
    }
    await observer.observe()
    client._status = {
        "last_death": _death_payload(event_id="evt-7", timestamp="2026-10-28T23:05:00+00:00")
    }
    await observer.observe()
    assert len(mgr.crash_history) == 3
    assert mgr.last_reported_death.cause == "oom"


def test_unknown_cause_is_recorded_verbatim():
    """A newer daemon may add causes; the death is still a death."""
    state, report = parse_death_signal(
        {"last_death": _death_payload(cause="Segfault", exit_code=None)}
    )
    assert state == DEATH_SIGNAL_REPORTED
    assert report is not None
    assert report.cause == "segfault"


# ── 2. Signal absent (older daemon) → today's behaviour ────────────────


async def test_absent_death_signal_is_unsupported_and_records_nothing(tmp_path):
    mgr = _make_manager(tmp_path)
    # Exactly the /status body a pre-#34 daemon returns.
    client = _StubCaretaker(
        {
            "loaded_model": "Qwen3-30B-A3B",
            "vision_enabled": False,
            "is_unloaded": False,
            "needs_reload": False,
            "loaded_at": 1234.5,
            "idle_since": 1234.5,
        }
    )
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    observation = await observer.observe()

    assert observation.reachable is True
    assert observation.death_signal == DEATH_SIGNAL_UNSUPPORTED
    assert observation.death is None
    assert observation.backend_alive is None
    assert mgr.crash_history == []
    assert mgr.last_crash is None
    assert mgr.last_reported_death is None

    fields = status_fields(
        observation,
        probe_healthy=True,
        gateway_unloaded=False,
        last_report=observer.last_report,
    )
    assert fields["backend_state"] == BACKEND_STATE_ALIVE
    assert fields["backend_death"] is None
    assert fields["caretaker"] == {
        "configured": True,
        "reachable": True,
        "death_signal": DEATH_SIGNAL_UNSUPPORTED,
        "backend_alive": None,
        "status_read_at": observation.status_read_at,
    }


async def test_absent_signal_with_dead_backend_is_unknown_not_alive(tmp_path):
    """No signal + no probe answer is unknown; the pre-#34 field stays False."""
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker({"loaded_model": "Qwen3-30B-A3B", "is_unloaded": False})
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    fields = await _fields(observer, probe_healthy=False)

    assert fields["backend_state"] == BACKEND_STATE_UNKNOWN
    assert fields["backend_state"] != BACKEND_STATE_ALIVE
    assert fields["backend_death"] is None
    assert mgr.crash_history == []


def test_empty_death_field_is_not_a_death():
    state, report = parse_death_signal({"last_death": None})
    assert state == DEATH_SIGNAL_EMPTY
    assert report is None


def test_missing_field_is_unsupported():
    assert parse_death_signal({}) == (DEATH_SIGNAL_UNSUPPORTED, None)
    assert parse_death_signal({"is_unloaded": True}) == (DEATH_SIGNAL_UNSUPPORTED, None)


# ── 3. Daemon unreachable → fail-closed, never a false healthy ─────────


async def test_unreachable_caretaker_is_unknown_never_alive(tmp_path):
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker(error=CaretakerUnavailable("http://127.0.0.1:11441"))
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    observation = await observer.observe()

    assert observation.configured is True
    assert observation.reachable is False
    assert observation.death_signal == DEATH_SIGNAL_UNAVAILABLE
    assert observation.backend_alive is None
    assert observation.death is None
    # The failure is observability, not a crash record.
    assert mgr.crash_history == []

    fields = await _fields(observer, probe_healthy=False)
    assert fields["backend_state"] == BACKEND_STATE_UNKNOWN
    assert fields["backend_state"] != BACKEND_STATE_ALIVE
    assert fields["backend_death"] is None


async def test_unexpected_exception_while_reading_is_unknown(tmp_path):
    """Never let the death read break /api/status, and never claim alive."""
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker(error=RuntimeError("boom"))
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    observation = await observer.observe()

    assert observation.reachable is False
    assert observation.death_signal == DEATH_SIGNAL_UNAVAILABLE
    assert derive_backend_state(
        probe_healthy=False,
        unloaded=False,
        daemon_alive=observation.backend_alive,
        death_signal=observation.death_signal,
    ) == BACKEND_STATE_UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        {"last_death": []},
        {"last_death": "oom"},
        {"last_death": {"cause": "oom"}},  # no model / timestamp
        {"last_death": {"cause": "oom", "model": "m"}},  # no timestamp
        {"last_death": {"cause": "oom", "model": "m", "timestamp": "yesterday"}},
        {"last_death": {"model": "m", "timestamp": DEATH_TS}},  # no cause
    ],
)
def test_malformed_death_records_are_malformed_not_deaths(payload):
    assert parse_death_signal(payload) == (DEATH_SIGNAL_MALFORMED, None)


async def test_malformed_record_is_not_recorded_and_not_alive(tmp_path):
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker({"last_death": {"cause": "oom"}})
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    observation = await observer.observe()
    fields = await _fields(observer, probe_healthy=False)

    assert observation.death_signal == DEATH_SIGNAL_MALFORMED
    assert mgr.crash_history == []
    assert fields["backend_state"] == BACKEND_STATE_UNKNOWN


def test_daemon_health_claim_never_overrides_a_dead_probe():
    """A daemon claiming health while nothing answers must not read alive."""
    fields = status_fields(
        CaretakerObservation(
            configured=True,
            reachable=True,
            death_signal=DEATH_SIGNAL_EMPTY,
            backend_alive=True,
        ),
        probe_healthy=False,
        gateway_unloaded=False,
        last_report=None,
    )
    assert fields["backend_state"] == BACKEND_STATE_UNKNOWN
    assert fields["backend_state"] != BACKEND_STATE_ALIVE


def test_daemon_dead_claim_wins_over_a_healthy_probe():
    """The lifecycle owner saying "dead" beats a leftover listener."""
    fields = status_fields(
        CaretakerObservation(
            configured=True,
            reachable=True,
            death_signal=DEATH_SIGNAL_EMPTY,
            backend_alive=False,
        ),
        probe_healthy=True,
        gateway_unloaded=False,
        last_report=None,
    )
    assert fields["backend_state"] == BACKEND_STATE_DEAD


def test_deliberate_unload_is_unloaded_not_dead():
    fields = status_fields(
        CaretakerObservation(
            configured=True,
            reachable=True,
            death_signal=DEATH_SIGNAL_EMPTY,
            unloaded=True,
        ),
        probe_healthy=False,
        gateway_unloaded=False,
        last_report=None,
    )
    assert fields["backend_state"] == BACKEND_STATE_UNLOADED


def test_gateway_unloaded_flag_is_unloaded_not_dead():
    assert derive_backend_state(
        probe_healthy=False,
        unloaded=True,
        daemon_alive=None,
        death_signal=DEATH_SIGNAL_UNSUPPORTED,
    ) == BACKEND_STATE_UNLOADED


def test_reported_death_with_dead_probe_is_dead():
    """The reported death supplies the cause for a backend that is down."""
    fields = status_fields(
        CaretakerObservation(
            configured=True, reachable=True, death_signal=DEATH_SIGNAL_REPORTED
        ),
        probe_healthy=False,
        gateway_unloaded=False,
        last_report=None,
    )
    assert fields["backend_state"] == BACKEND_STATE_DEAD


# ── Endpoint wiring: /api/status and /api/crashes ──────────────────────


async def test_api_status_reports_last_death_and_dead_state(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker({"is_unloaded": False, "last_death": _death_payload()})
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)
    _install_admin_runtime(monkeypatch, mgr, observer, probe_healthy=False)

    payload = await admin_api.get_server_status("test")

    # Additive fields describe the death ...
    assert payload["backend_state"] == BACKEND_STATE_DEAD
    assert payload["backend_death"] is not None
    assert payload["backend_death"]["cause"] == "oom"
    assert payload["backend_death"]["timestamp"] == DEATH_TS
    assert payload["backend_death"]["model"] == "Qwen3-30B-A3B"
    assert payload["backend_death"]["origin"] == CRASH_ORIGIN_REPORTED_DEATH
    assert payload["caretaker"]["death_signal"] == DEATH_SIGNAL_REPORTED
    assert payload["caretaker"]["reachable"] is True
    # ... while the pre-#34 fields keep their meaning.
    assert payload["backend_healthy"] is False
    assert payload["total_crashes"] == 1
    assert payload["last_crash"]["cause"] == "oom"


async def test_api_status_without_observer_keeps_the_pre_34_body(tmp_path, monkeypatch):
    """No caretaker configured / older wiring: nothing new claims anything."""
    mgr = _make_manager(tmp_path)
    _install_admin_runtime(monkeypatch, mgr, None, probe_healthy=True)

    payload = await admin_api.get_server_status("test")

    assert payload["backend_death"] is None
    assert payload["backend_state"] == BACKEND_STATE_ALIVE
    assert payload["caretaker"] == {
        "configured": False,
        "reachable": False,
        "death_signal": DEATH_SIGNAL_NOT_CONFIGURED,
        "backend_alive": None,
        "status_read_at": None,
    }
    for key in (
        "current_model",
        "backend_healthy",
        "is_unloaded",
        "idle_seconds",
        "idle_unload_minutes",
        "backend_url",
        "total_crashes",
        "last_crash",
        "vram",
        "vram_model_mb",
        "security",
        "startup",
        "current_requested_target",
        "switch",
        "queue",
        "routing",
        "proxy",
        "scaler",
    ):
        assert key in payload, key


async def test_api_status_unreachable_caretaker_is_unknown(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker(error=CaretakerUnavailable("http://127.0.0.1:11441"))
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)
    _install_admin_runtime(monkeypatch, mgr, observer, probe_healthy=False)

    payload = await admin_api.get_server_status("test")

    assert payload["backend_state"] == BACKEND_STATE_UNKNOWN
    assert payload["backend_state"] != BACKEND_STATE_ALIVE
    assert payload["caretaker"]["reachable"] is False
    assert payload["caretaker"]["death_signal"] == DEATH_SIGNAL_UNAVAILABLE
    assert payload["backend_death"] is None
    assert payload["total_crashes"] == 0


async def test_load_failure_and_reported_death_are_distinguishable(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path)
    # A load failure, as Guardian records it at /ensure/load time today.
    mgr.record_crash(
        CrashRecord(
            timestamp="2026-10-28T20:00:00+00:00",
            model="Qwen3-30B-A3B",
            error_message="failed to fit params to free device memory",
            exit_code=1,
            config_snapshot={"ngl": 99},
        )
    )
    client = _StubCaretaker(
        {"is_unloaded": False, "last_death": _death_payload(cause="signal")}
    )
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)
    _install_admin_runtime(monkeypatch, mgr, observer, probe_healthy=False)

    # The death is observed through /api/status, which is the read point.
    await admin_api.get_server_status("test")
    payload = await admin_api.get_crash_history("test")

    assert payload["total_crashes"] == 2
    by_origin = {entry["origin"]: entry for entry in payload["history"]}
    assert set(by_origin) == {CRASH_ORIGIN_LOAD_FAILURE, CRASH_ORIGIN_REPORTED_DEATH}

    load_failure = by_origin[CRASH_ORIGIN_LOAD_FAILURE]
    reported = by_origin[CRASH_ORIGIN_REPORTED_DEATH]
    # Distinguishable: the load failure has no invented cause, and keeps its
    # launch snapshot; the reported death carries the daemon's cause.
    assert load_failure["cause"] is None
    assert load_failure["config_snapshot"] == {"ngl": 99}
    assert reported["cause"] == "signal"
    assert reported["config_snapshot"] is None
    assert reported["exit_code"] == 137

    assert payload["last_crash"]["origin"] == CRASH_ORIGIN_REPORTED_DEATH
    assert payload["last_reported_death"]["cause"] == "signal"
    assert payload["last_reported_death"]["timestamp"] == DEATH_TS


async def test_crashes_without_a_death_keep_last_reported_death_null(
    tmp_path, monkeypatch
):
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr(admin_api, "_model_manager", mgr)
    mgr.record_crash(
        CrashRecord(timestamp="2026-10-28T20:00:00+00:00", model="m", error_message="boom")
    )
    payload = await admin_api.get_crash_history("test")
    assert payload["last_reported_death"] is None
    assert payload["last_crash"]["origin"] == CRASH_ORIGIN_LOAD_FAILURE
    assert payload["last_crash"]["cause"] is None


# ── Observation plumbing ───────────────────────────────────────────────


async def test_observer_without_client_is_not_configured(tmp_path):
    mgr = _make_manager(tmp_path)
    observer = BackendDeathObserver(None, mgr)

    observation = await observer.observe()
    assert observation.configured is False
    assert observation.death_signal == DEATH_SIGNAL_NOT_CONFIGURED

    fields = await _fields(observer, probe_healthy=True)
    assert fields["backend_state"] == BACKEND_STATE_ALIVE
    assert fields["backend_death"] is None


async def test_observation_is_coalesced_for_the_cache_window(tmp_path):
    mgr = _make_manager(tmp_path)
    clock = [1000.0]
    client = _StubCaretaker({"is_unloaded": False})
    observer = BackendDeathObserver(
        client, mgr, cache_seconds=5.0, monotonic=lambda: clock[0]
    )

    await observer.observe()
    await observer.observe()
    assert client.calls == 1

    clock[0] += 6.0
    await observer.observe()
    assert client.calls == 2


async def test_observation_uses_a_short_bounded_timeout(tmp_path):
    mgr = _make_manager(tmp_path)
    client = _StubCaretaker({"is_unloaded": False})
    observer = BackendDeathObserver(client, mgr, cache_seconds=0)

    await observer.observe()

    assert len(client.timeouts) == 1
    assert client.timeouts[0] is not None
    assert 0 < client.timeouts[0] <= backend_death.DEFAULT_OBSERVE_TIMEOUT_SECONDS


def test_observe_timeout_respects_the_configured_client_budget(monkeypatch):
    """A tighter configured caretaker timeout is never widened by this module."""
    monkeypatch.setattr(
        backend_death,
        "load_caretaker_runtime_config",
        lambda: {"client_timeout_seconds": 0.5},
    )
    assert backend_death._observe_timeout() == 0.5


def test_observe_timeout_falls_back_when_config_is_broken(monkeypatch):
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(backend_death, "load_caretaker_runtime_config", _boom)
    assert (
        backend_death._observe_timeout()
        == backend_death.DEFAULT_OBSERVE_TIMEOUT_SECONDS
    )
