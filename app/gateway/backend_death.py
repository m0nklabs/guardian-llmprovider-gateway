"""Backend-death visibility: the caretaker's out-of-band death signal.

Guardian used to learn that the backend died through exactly one channel: the
NEXT ``POST /ensure`` failing with ``503 model_load_failed`` and
``crash_details`` (see ``app/gateway/caretaker_runtime._map_caretaker_error``).
A llama-server crash or an OOM kill during a live-but-idle session was
therefore invisible until some request forced a reload: ``/api/crashes`` stayed
empty and ``/api/status`` kept claiming the backend was fine.

This module consumes the contract Guardian needs from the caretaker control API
for a death that did NOT happen during ``/ensure``: an OPTIONAL ``last_death``
record plus an OPTIONAL ``backend_alive`` flag on ``GET {management_url}/status``
(wire shape, semantics and error cases in ``docs/API_REFERENCE.md``; the
compatibility contract in ``docs/ARCHITECTURE.md`` §3).

Fail-open, strictly:

- **Signal absent** (older daemon): ``death_signal`` is ``unsupported`` and
  nothing else changes — no crash record, no new required field, no health
  claim.  Behaviour is exactly the pre-#34 behaviour.
- **Signal unreadable** (transport error, timeout, non-200, non-JSON, or a
  malformed record): ``death_signal`` is ``unavailable``/``malformed`` and the
  derived backend state falls back to ``unknown`` — never ``alive``.  Silence
  from an unreachable daemon is not evidence of health.
- **Signal reported**: the death becomes a :class:`~app.engine.manager.CrashRecord`
  with ``origin='reported_death'`` and the daemon's ``cause``, recorded once
  (fingerprint dedupe, bounded) so ``/api/crashes`` keeps the death
  distinguishable from a load failure.

Nothing here polls: the signal is read where ``/api/status`` already reads
backend state, behind a short TTL cache so a hot status endpoint cannot turn
into a caretaker load generator.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config_loader import load_caretaker_runtime_config
from app.engine.manager import (
    CRASH_ORIGIN_REPORTED_DEATH,
    CrashRecord,
)
from app.gateway.caretaker_client import CaretakerError

logger = logging.getLogger("Guardian")

# ── Caretaker → Guardian contract field names (docs/API_REFERENCE.md) ──
# Optional members of the caretaker's ``GET /status`` body.  Absent on any
# daemon that predates issue #34; Guardian must keep working unchanged then.
LAST_DEATH_FIELD = "last_death"
BACKEND_ALIVE_FIELD = "backend_alive"
IS_UNLOADED_FIELD = "is_unloaded"

# Cause vocabulary the contract carries.  A newer daemon may report a cause
# outside this set: it is recorded verbatim (lower-cased) rather than rejected,
# because dropping the record would lose a death Guardian can see.
KNOWN_DEATH_CAUSES = frozenset({"oom", "signal", "exit_code"})

# ── Death-signal states (what the last status read told us) ─────────────
DEATH_SIGNAL_NOT_CONFIGURED = "not_configured"  # no caretaker client at all
DEATH_SIGNAL_UNSUPPORTED = "unsupported"  # daemon predates the field
DEATH_SIGNAL_EMPTY = "empty"  # field present, explicitly no death to report
DEATH_SIGNAL_REPORTED = "reported"  # a valid death record
DEATH_SIGNAL_MALFORMED = "malformed"  # field present but unusable
DEATH_SIGNAL_UNAVAILABLE = "unavailable"  # read failed / timed out / non-200

# ── Derived backend state (fail-closed: "alive" needs positive evidence) ──
BACKEND_STATE_ALIVE = "alive"
BACKEND_STATE_DEAD = "dead"
BACKEND_STATE_UNLOADED = "unloaded"
BACKEND_STATE_UNKNOWN = "unknown"

# Observation read timeout: shorter than the client's control-API timeout so a
# hung daemon cannot stall the dashboard's status poll.  Never longer than the
# operator's configured ``caretaker.client_timeout_seconds`` (read live, so
# POST /api/config/reload retunes it) — a daemon with a tighter budget is not
# given a longer one by this module.
DEFAULT_OBSERVE_TIMEOUT_SECONDS = 2.0

# TTL for the coalesced observation.  ``/api/status`` is polled a few times per
# minute; a read-only death check does not need to hit the daemon every time.
DEFAULT_OBSERVE_CACHE_SECONDS = 5.0

# Bounded memory for the dedupe fingerprints (one reported death per crash
# record; the daemon may keep re-reporting the same one for the rest of its
# lifetime).
_SEEN_FINGERPRINTS_LIMIT = 64

_ISO_WITH_Z = re.compile(r"Z$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean_str(value: Any) -> str | None:
    """Return a stripped non-empty string, or None for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_exit_code(value: Any) -> int | None:
    """Accept an int or a numeric string; reject bools and everything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _valid_timestamp(value: str) -> bool:
    """True when ``value`` parses as ISO-8601 (``Z`` suffix accepted)."""
    try:
        datetime.fromisoformat(_ISO_WITH_Z.sub("+00:00", value))
    except ValueError:
        return False
    return True


def _optional_bool(status: dict, field_name: str) -> bool | None:
    """Read an optional tri-state boolean without inventing a value.

    Only a real JSON boolean counts.  A string ``"true"``/``"false"`` is NOT
    coerced: a daemon that cannot express a boolean must read as unknown, not
    as an accidental health claim.
    """
    value = status.get(field_name)
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class BackendDeathReport:
    """A validated backend-death record reported by the caretaker.

    ``cause``/``model``/``timestamp`` are the contract minimum; the rest is
    optional detail the daemon may add.  ``observed_at`` is Guardian's own read
    time, not part of the contract.
    """

    cause: str
    model: str
    timestamp: str
    exit_code: int | None = None
    signal: str | None = None
    detail: str | None = None
    event_id: str | None = None
    observed_at: str = ""

    def fingerprint(self) -> tuple[str, ...]:
        """Stable identity used to record a death only once.

        ``event_id`` wins when the daemon provides one; otherwise the record's
        own fields are the identity.  Two byte-identical deaths inside the same
        timestamp therefore collapse into one record — documented in
        ``docs/API_REFERENCE.md``; a daemon that needs them counted separately
        supplies ``event_id``.
        """
        if self.event_id:
            return ("event_id", self.event_id)
        return (
            "record",
            self.timestamp,
            self.cause,
            self.model,
            "" if self.exit_code is None else str(self.exit_code),
            self.signal or "",
        )

    def summary(self) -> str:
        """One-line human summary for the crash record's ``error_message``."""
        parts = [f"caretaker reported backend death: cause={self.cause}"]
        if self.signal:
            parts.append(f"signal={self.signal}")
        if self.exit_code is not None:
            parts.append(f"exit_code={self.exit_code}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "cause": self.cause,
            "model": self.model,
            "timestamp": self.timestamp,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "detail": self.detail,
            "event_id": self.event_id,
            "origin": CRASH_ORIGIN_REPORTED_DEATH,
            "observed_at": self.observed_at,
        }


def parse_death_signal(status: Any) -> tuple[str, BackendDeathReport | None]:
    """Parse the caretaker's status body into a signal state + death record.

    Returns ``(state, report)`` where ``report`` is non-None only for
    ``DEATH_SIGNAL_REPORTED``.  A record missing one of the contract's required
    members (``cause``, ``model``, a parseable ISO-8601 ``timestamp``) is
    ``malformed``: Guardian never invents a missing field to force a record.
    """
    if not isinstance(status, dict):
        return DEATH_SIGNAL_MALFORMED, None
    if LAST_DEATH_FIELD not in status:
        # Older daemon: the contract is absent, so nothing changed for us.
        return DEATH_SIGNAL_UNSUPPORTED, None
    payload = status[LAST_DEATH_FIELD]
    if payload is None:
        # Field present, explicitly "no death to report".
        return DEATH_SIGNAL_EMPTY, None
    if not isinstance(payload, dict):
        return DEATH_SIGNAL_MALFORMED, None
    cause = _clean_str(payload.get("cause"))
    model = _clean_str(payload.get("model"))
    timestamp = _clean_str(payload.get("timestamp"))
    if cause is None or model is None or timestamp is None or not _valid_timestamp(timestamp):
        return DEATH_SIGNAL_MALFORMED, None
    return DEATH_SIGNAL_REPORTED, BackendDeathReport(
        cause=cause.lower(),
        model=model,
        timestamp=timestamp,
        exit_code=_coerce_exit_code(payload.get("exit_code")),
        signal=_clean_str(payload.get("signal")),
        detail=_clean_str(payload.get("detail")),
        event_id=_clean_str(payload.get("event_id")),
        observed_at=_now_iso(),
    )


@dataclass(frozen=True)
class CaretakerObservation:
    """Result of one caretaker status read, as far as the death signal goes.

    ``configured=False`` (the default) is also the shape used when no caretaker
    client exists at all, so callers always get a complete answer.
    """

    configured: bool = False
    reachable: bool = False
    death_signal: str = DEATH_SIGNAL_NOT_CONFIGURED
    backend_alive: bool | None = None
    unloaded: bool | None = None
    status_read_at: str | None = None
    death: BackendDeathReport | None = None


def derive_backend_state(
    *,
    probe_healthy: bool,
    unloaded: bool,
    daemon_alive: bool | None,
    death_signal: str,
) -> str:
    """Combine the direct health probe with the caretaker's death signal.

    Precedence (fail-closed: ``alive`` requires positive evidence):

    1. a deliberate stop — the gateway's own ``is_unloaded`` or the daemon's —
       is ``unloaded``, neither healthy nor a death;
    2. the daemon asserting ``backend_alive=false`` is ``dead`` even when a
       probe still answers (a lying/leftover listener must not read healthy);
    3. a successful ``/health`` probe is ``alive`` — the one positive signal;
    4. a probe failure plus a reported death is ``dead`` (cause known);
    5. anything else — including an unreachable daemon and a daemon claiming
       ``backend_alive=true`` that the probe contradicts — is ``unknown``.
       Never ``alive``: silence from an unreachable daemon is not health.
    """
    if unloaded:
        return BACKEND_STATE_UNLOADED
    if daemon_alive is False:
        return BACKEND_STATE_DEAD
    if probe_healthy:
        return BACKEND_STATE_ALIVE
    if death_signal == DEATH_SIGNAL_REPORTED:
        return BACKEND_STATE_DEAD
    return BACKEND_STATE_UNKNOWN


def status_fields(
    observation: CaretakerObservation,
    *,
    probe_healthy: bool,
    gateway_unloaded: bool,
    last_report: BackendDeathReport | None,
) -> dict:
    """Build the additive ``/api/status`` fields for the death signal.

    Additive only: no existing key changes meaning.  ``backend_healthy`` stays
    the direct probe result; ``backend_state`` is the combined judgement and
    ``backend_death`` the last death Guardian has observed (``null`` when none).
    """
    unloaded = bool(gateway_unloaded) or observation.unloaded is True
    return {
        "backend_state": derive_backend_state(
            probe_healthy=probe_healthy,
            unloaded=unloaded,
            daemon_alive=observation.backend_alive,
            death_signal=observation.death_signal,
        ),
        "backend_death": last_report.to_dict() if last_report is not None else None,
        "caretaker": {
            "configured": observation.configured,
            "reachable": observation.reachable,
            "death_signal": observation.death_signal,
            "backend_alive": observation.backend_alive,
            "status_read_at": observation.status_read_at,
        },
    }


def _observe_timeout() -> float:
    """Read timeout for the observation (live config, defensive fallback)."""
    try:
        configured = float(load_caretaker_runtime_config()["client_timeout_seconds"])
    except Exception:  # noqa: BLE001 — config must never break /api/status
        return DEFAULT_OBSERVE_TIMEOUT_SECONDS
    return max(min(DEFAULT_OBSERVE_TIMEOUT_SECONDS, configured), 0.1)


class BackendDeathObserver:
    """Reads the caretaker death signal and records reported deaths.

    One instance per process, built where the caretaker client is built
    (``app/proxy/server.py``) and injected into the admin API.  Never raises:
    every failure is reported as an ``unknown``/``unavailable`` observation so
    ``/api/status`` keeps answering.
    """

    def __init__(
        self,
        client: Any | None,
        model_manager: Any | None,
        *,
        cache_seconds: float = DEFAULT_OBSERVE_CACHE_SECONDS,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._model_manager = model_manager
        self._cache_seconds = float(cache_seconds)
        self._log = logger or logging.getLogger("Guardian.BackendDeathObserver")
        self._monotonic = monotonic
        self._last: CaretakerObservation | None = None
        self._last_at: float | None = None
        self._last_success_read_at: str | None = None
        self._seen: list[tuple[str, ...]] = []
        #: Newest death record the daemon has reported in this process
        #: lifetime (also the source for ``/api/status.backend_death``).
        self.last_report: BackendDeathReport | None = None

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def observe(self) -> CaretakerObservation:
        """Return the (cached) caretaker observation; never raises."""
        if self._client is None:
            return CaretakerObservation(configured=False)
        cached = self._cached()
        if cached is not None:
            return cached
        observation = await self._read()
        self._last = observation
        self._last_at = self._monotonic()
        return observation

    # ── internals ───────────────────────────────────────────────────────

    def _cached(self) -> CaretakerObservation | None:
        if self._last is None or self._last_at is None or self._cache_seconds <= 0:
            return None
        if self._monotonic() - self._last_at >= self._cache_seconds:
            return None
        return self._last

    def _unavailable(self, death_signal: str) -> CaretakerObservation:
        return CaretakerObservation(
            configured=True,
            reachable=False,
            death_signal=death_signal,
            status_read_at=self._last_success_read_at,
        )

    async def _read(self) -> CaretakerObservation:
        try:
            status = await self._client.status(timeout=_observe_timeout())
        except CaretakerError as exc:
            # Includes CaretakerUnavailable (transport/timeout/non-200) and the
            # defensive non-JSON/non-dict 200 mapping inside the client.
            self._log.warning(
                "Caretaker /status unreadable for the death signal (%s); "
                "backend state is unknown, not alive",
                exc,
            )
            return self._unavailable(DEATH_SIGNAL_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — /api/status must keep answering
            self._log.error(
                "Caretaker /status observation failed unexpectedly (%s); "
                "backend state is unknown, not alive",
                exc,
            )
            return self._unavailable(DEATH_SIGNAL_UNAVAILABLE)

        death_signal, report = parse_death_signal(status)
        if death_signal == DEATH_SIGNAL_MALFORMED:
            self._log.warning(
                "Caretaker /status carried an unusable %s record (needs cause, "
                "model and an ISO-8601 timestamp); ignoring it",
                LAST_DEATH_FIELD,
            )
        if report is not None:
            try:
                self._record_death(report)
            except Exception as exc:  # noqa: BLE001 — recording must never break /api/status
                self._log.error(
                    "Could not record the reported backend death (%s): %s",
                    report.summary(),
                    exc,
                )
        self._last_success_read_at = _now_iso()
        return CaretakerObservation(
            configured=True,
            reachable=True,
            death_signal=death_signal,
            backend_alive=_optional_bool(status, BACKEND_ALIVE_FIELD),
            unloaded=_optional_bool(status, IS_UNLOADED_FIELD),
            status_read_at=self._last_success_read_at,
            death=report,
        )

    def _record_death(self, report: BackendDeathReport) -> None:
        """Record ``report`` once, as a crash history entry."""
        fingerprint = report.fingerprint()
        self.last_report = report
        if report.cause not in KNOWN_DEATH_CAUSES:
            # A newer daemon may add causes; record them verbatim instead of
            # losing a death Guardian can see.  The documented vocabulary
            # (oom | signal | exit_code) is what clients can rely on.
            self._log.info(
                "Caretaker reported an undocumented death cause %r; recording it verbatim",
                report.cause,
            )
        if fingerprint in self._seen:
            # The daemon keeps reporting its last death until it observes a new
            # one; the crash history records the event, not every poll.
            return
        self._seen.append(fingerprint)
        if len(self._seen) > _SEEN_FINGERPRINTS_LIMIT:
            self._seen = self._seen[-_SEEN_FINGERPRINTS_LIMIT:]
        if self._model_manager is None:
            self._log.error(
                "Cannot record the reported backend death (%s): no model manager",
                report.summary(),
            )
            return
        crash = CrashRecord(
            timestamp=report.timestamp,
            model=report.model,
            error_message=report.summary(),
            exit_code=report.exit_code,
            config_snapshot=None,
            origin=CRASH_ORIGIN_REPORTED_DEATH,
            cause=report.cause,
        )
        self._model_manager.record_crash(crash)
        self._log.error("💥 Backend death reported by the caretaker: %s", report.summary())
