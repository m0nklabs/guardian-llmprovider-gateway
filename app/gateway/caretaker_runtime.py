"""F5 gateway-wiring (tranche 2): remote-first lifecycle execution for the
request hotpath.

The gateway no longer spawns llama-server itself on the happy path; the
request-path lifecycle calls (auto-reload, auto-switch, connect-error
recovery) go to the caretaker daemon via ``POST /ensure`` first.  Only when
the caretaker is not configured or is unreachable does the gateway fall back
to its own ``ModelManager`` spawn/switch (the pre-F5 behaviour), so a
caretaker outage never takes local inference down if the backend can be
(started) from the gateway.

Decision logic:

- ``CaretakerClient`` built and reachable → ``POST /ensure``; on 200 the
  manager state is mirrored via ``mark_loaded_by_caretaker`` and the result is
  ``"remote"``.
- SWITCHES (``pre_switch_save=True``) restore the target's session context
  only when the daemon reports ``fresh_load`` (observed via
  ``CaretakerClient.supports_fresh_load``, re-validated on every /ensure
  response).  Until the field ships, remote switches run WITHOUT context
  restore and log a loud warning; the pre-save is gated on the same
  capability so no wasteful slot-save/ps-scan runs.  The switch itself stays
  remote-first: a local-lifecycle switch while the daemon is alive would
  race a second controller on the backend (see the unreachable branch), so
  the local lifecycle is only ever used when the daemon is confirmed
  down/absent.  Reload sites (no ``pre_switch_save``) always go remote-first.
- ``CaretakerUnavailable`` (transport/timeout/auth) → a single re-probe of
  ``/ensure`` happens first (a live daemon mid-switch would otherwise race a
  local spawn — two controllers on the same backend).  A read/write timeout
  means the daemon accepted the connection (alive): a second one fails closed.
  Connection-refused/DNS/ConnectTimeout mean the daemon may be gone: the
  backend may still be healthy (daemon died but llama-server survived) — if
  so, adopt the loaded state without spawning.  If the backend is ALIVE but
  not serving the requested model, the local lifecycle would race a second
  controller → fail closed.  Only when the backend is confirmed DOWN does the
  original local lifecycle (``local_fallback``) run — safe, because with the
  daemon down nothing else owns the backend port.
- Any other ``CaretakerError`` (model not found, VRAM limit, load failed,
  invalid request) → mapped to the same error types the hotpath callers
  already handle (``ModelLoadError`` / ``ValueError``), so crash recording
  (the daemon's ``crash_details`` become the raised ``crash_record``) and the
  503 paths keep working unchanged.
- Client ``None`` (no caretaker configured) → ``local_fallback`` directly —
  exact pre-F5 behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from app.engine.manager import CrashRecord, ModelLoadError
from app.gateway.caretaker_client import (
    CaretakerError,
    CaretakerInvalidRequest,
    CaretakerModelLoadFailed,
    CaretakerModelNotFound,
    CaretakerUnavailable,
    CaretakerVramExceeded,
)

logger = logging.getLogger("Guardian")

# Injected by server.init(); None = no caretaker configured (legacy behaviour).
_caretaker_client = None  # CaretakerClient | None
_model_manager = None  # ModelManager | None


def init(*, model_manager, caretaker_client) -> None:
    """Bind the runtime dependencies (mirrors the Phase-5 init() pattern)."""
    global _caretaker_client, _model_manager
    _caretaker_client = caretaker_client
    _model_manager = model_manager


async def _ensure_with_retry(
    model: str,
    *,
    enable_vision: bool | None = None,
    context_hint: int | None = None,
) -> dict:
    """POST /ensure, re-probing once on ``CaretakerUnavailable``.

    ``CaretakerUnavailable`` covers transport timeouts, connection-refused and
    unexpected HTTP statuses.  The transport cause matters for the fallback
    decision:

    - status_code set → daemon alive but rejected the request (deterministic,
      no re-probe).
    - ``__cause__`` is :class:`httpx.ReadTimeout`/:class:`httpx.WriteTimeout`
      → a connection was ESTABLISHED, so the daemon is alive but busy (likely
      mid-switch; VRAM freeing + a large-model load can outlast the client
      timeout).  Re-probe once; a second such timeout must NOT run the local
      lifecycle — a live daemon would race a local spawn (two controllers on
      the same backend port / launch-args file).  Fail closed instead.
      ``ConnectTimeout``/``PoolTimeout`` are deliberately excluded: no
      connection was ever accepted there, so the daemon may as well be down
      (restarting with a full backlog, firewall DROP, LAN host powered off)
      and the safe local fallback still applies.
    - any other transport error (connection refused, DNS, …) → the daemon is
      really gone; re-probe once more so the caller can adopt a surviving
      backend or run the local fallback (it is then the only controller).
    """
    try:
        return await _caretaker_client.ensure(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
    except CaretakerUnavailable as exc:
        if exc.status_code is not None:
            raise  # daemon alive but rejected us — no point re-probing
        cause = exc.__cause__
        if isinstance(cause, (httpx.ReadTimeout, httpx.WriteTimeout)):
            # Alive but busy: re-probe once; a second timeout must not spawn
            # locally against a live daemon.
            try:
                return await _caretaker_client.ensure(
                    model,
                    enable_vision=enable_vision,
                    context_hint=context_hint,
                )
            except CaretakerUnavailable as exc2:
                if exc2.status_code is not None:
                    raise
                if isinstance(exc2.__cause__, (httpx.ReadTimeout, httpx.WriteTimeout)):
                    raise ModelLoadError(
                        "Caretaker alive but unresponsive; refusing local "
                        "fallback to avoid a second controller on the backend"
                    ) from exc2
                raise
        # Non-timeout transport errors (connection refused, DNS, ConnectTimeout,
        # ...) mean the daemon is really gone: re-probe once more so the caller
        # can adopt a surviving backend or run the local fallback.  A READ/WRITE
        # timeout on that re-probe means the daemon came back but is busy
        # (alive) — running the local lifecycle would race a live daemon, so
        # fail closed the same way the timeout branch does.
        try:
            return await _caretaker_client.ensure(
                model,
                enable_vision=enable_vision,
                context_hint=context_hint,
            )
        except CaretakerUnavailable as exc2:
            if isinstance(exc2.__cause__, (httpx.ReadTimeout, httpx.WriteTimeout)):
                raise ModelLoadError(
                    "Caretaker alive but unresponsive; refusing local "
                    "fallback to avoid a second controller on the backend"
                ) from exc2
            raise


async def ensure_backend(
    *,
    model: str,
    enable_vision: bool | None = None,
    context_hint: int | None = None,
    local_fallback: Callable[[], Awaitable[None]],
    pre_switch_save: bool = False,
    client_id: str | None = None,
) -> str:
    """Ensure ``model`` is loaded/active on the local backend.

    Returns ``"remote"`` (caretaker performed the ensure), ``"local"`` (the
    gateway's own lifecycle ran) or ``"local-healthy"`` (no spawn — the
    backend was already healthy after a caretaker outage).  Raises the same
    error types the hotpath callers already handle (``ModelLoadError`` /
    ``ValueError``).

    ``client_id`` is optional defense-in-depth: the request-hotpath switch
    sites already gate on ``is_switch_allowed(client_id)`` before calling
    this (routing.py / ollama.py), but the switch sites also pass their
    client id through so a future un-gated call site cannot trigger a remote
    switch for an allowlist-excluded client.  Reload sites (same-model
    reload) deliberately omit it — they are not switches.
    """
    if _caretaker_client is None:
        await local_fallback()
        return "local"

    if (
        client_id is not None
        and _model_manager is not None
        and not _model_manager.is_switch_allowed(client_id)
    ):
        raise ValueError(f"Client '{client_id}' is not allowed to switch models")

    # Snapshot the daemon capability BEFORE the /ensure call and gate BOTH
    # the pre-save and the post-ensure restore on that same snapshot.  Gating
    # them on different observations is asymmetric on the very first switch
    # after the daemon starts shipping fresh_load: the pre-save (gated on the
    # pre-call flag, still False) would be skipped while the restore (gated
    # on the response's fresh_load, now present) would run and re-inject the
    # target model's possibly stale auto-save — the current session would be
    # silently lost and the target would boot a stale one.  Snapshotting both
    # on the same observation keeps the transition switch on the
    # pre-capability path (no save, no restore) and brings save/restore back
    # symmetrically from the second switch onward.
    #
    # The SWITCH itself stays remote-first.  A local-lifecycle switch
    # (switch_model) while the caretaker daemon is alive would race a second
    # controller on the backend (the thread-2 invariant: local lifecycle is
    # only safe when the daemon is down/absent, which the CaretakerUnavailable
    # branch below already routes to the fallback).  Until the daemon ships
    # fresh_load, remote switches run WITHOUT context restore and log a loud
    # warning; the field ships as the immediate caretaker follow-up, after
    # which the capability is observed on the first reload/switch /ensure and
    # restore comes back automatically (self-healing).
    capability_before = getattr(_caretaker_client, "supports_fresh_load", False)
    if pre_switch_save and _model_manager is not None:
        if not capability_before:
            logger.warning(
                "F5: /ensure cannot confirm fresh_load — remote switch to "
                "'%s' proceeds WITHOUT context restore until the daemon "
                "ships the field",
                model,
            )
        else:
            await _model_manager.save_current_context()

    try:
        result = await _ensure_with_retry(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
    except CaretakerUnavailable as exc:
        # CaretakerUnavailable covers transport/timeout failures AND unexpected
        # HTTP statuses.  A status_code means the daemon IS alive but rejected
        # the gateway (e.g. 401/403 after a key rotation): running the local
        # lifecycle would create a second controller on a backend the daemon
        # still owns — fail closed.  Only status_code None (transport/timeout)
        # means the daemon may be gone; then the backend may still be running
        # (daemon died, llama-server survived).
        if exc.status_code is not None:
            raise ModelLoadError(
                f"Caretaker control-API rejected the request "
                f"(status {exc.status_code}); refusing local fallback to avoid "
                "a second controller on the backend"
            ) from exc
        # Caretaker unreachable — the backend may still be running (daemon
        # died, llama-server survived).  Only adopt the loaded state when the
        # running backend actually serves the requested model AND accepts
        # requests (a hung-but-alive llama-server must not be adopted as
        # healthy) AND its launch configuration still matches what this
        # request needs (no vision/context drift — otherwise the drift-checked
        # local load/switch fallback must run instead of forwarding to a
        # mismatched backend).  Otherwise the original local lifecycle is the
        # fallback (safe — with the daemon down nothing else owns the backend
        # port).
        if (
            _model_manager is not None
            and await _model_manager.backend_serves_model(model)
            and await _model_manager.backend_health_ok()
            and not _model_manager._config_drifted(
                model,
                enable_vision=enable_vision,
                context_hint=context_hint,
            )
            # The drift check above only compares GATEWAY-side persisted
            # state; the daemon may have launched the backend with settings
            # that differ from the gateway's persisted signature (e.g. a hot
            # config edit or a daemon-side vision resolution that drops
            # mmproj).  Adopting then stamps current_vision_enabled=True via
            # mark_loaded_by_caretaker even though the real process is
            # text-only — a subsequent image request would be forwarded to a
            # backend that cannot serve it.  So when the request needs vision,
            # also require the LIVE process to actually use mmproj (the args
            # file is written by whoever launched the backend — gateway OR
            # daemon — so the probe reflects the real process).
            and not (
                _model_manager._resolve_runtime_vision_flag(model, enable_vision)
                and not _model_manager.current_runtime_uses_mmproj(model)
            )
        ):
            logger.warning(
                "F5: caretaker unreachable but backend already serves '%s' — "
                "adopting loaded state without respawn",
                model,
            )
            _model_manager.mark_loaded_by_caretaker(
                model,
                enable_vision=enable_vision,
                context_hint=context_hint,
            )
            # NO restore on the adopt path: adoption only happens when the
            # daemon is UNREACHABLE, so there is no /ensure response to
            # confirm the model was freshly loaded (the timed-out ensure may
            # or may not have completed the switch).  Restoring on a
            # gateway-side probe alone risks clobbering a live slot-0 session
            # (the gguf-arg probe can misdetect a caretaker-launched
            # process) — the live session is authoritative.  A later A->B->A
            # re-ensure restores correctly once the daemon reports fresh_load.
            return "local-healthy"
        # ConnectTimeout/connection-refused does NOT prove the daemon is gone:
        # a firewall DROP on the management port, saturated accept backlog or
        # a restart window all surface as transport errors while the daemon
        # still owns llama-server.  If the backend is ALIVE (a real llama-server
        # answers), spawning/switching locally would race a second controller
        # on it (args-file write + systemctl stop/start) — the same hazard the
        # timeout branch fails closed on, and the state may be a live daemon
        # serving a different model than requested.  Only run the local
        # lifecycle when the backend is confirmed DOWN.
        if _model_manager is not None and await _model_manager.backend_health_ok():
            raise ModelLoadError(
                "Caretaker unreachable but backend is alive; refusing local "
                "fallback to avoid a second controller on the backend"
            ) from exc
        logger.warning(
            "F5: caretaker unavailable and backend down — local load fallback "
            "for '%s'",
            model,
        )
        await local_fallback()
        return "local"
    except CaretakerModelNotFound as exc:
        raise ModelLoadError(str(exc)) from exc
    except CaretakerVramExceeded as exc:
        raise ModelLoadError(str(exc)) from exc
    except CaretakerModelLoadFailed as exc:
        # The daemon's crash_details (its CrashRecord.to_dict()) must reach the
        # hotpath callers' crash recording, exactly as load()/switch_model()
        # populated crash_record before F5 — otherwise the remote primary path
        # would silently record nothing on backend crashes.
        crash_record = None
        details = exc.crash_details
        if isinstance(details, dict):
            try:
                crash_record = CrashRecord(
                    timestamp=details.get("timestamp", ""),
                    model=details.get("model", getattr(exc, "model", "")),
                    error_message=details.get("error_message", str(exc)),
                    exit_code=details.get("exit_code"),
                    config_snapshot=details.get("config_snapshot"),
                )
            except Exception:  # noqa: BLE001 — telemetry mapping must never break the error path
                crash_record = None
        raise ModelLoadError(str(exc), crash_record=crash_record) from exc
    except CaretakerInvalidRequest as exc:
        raise ValueError(str(exc)) from exc
    except CaretakerError as exc:  # safety net — never claim success from an unknown caretaker state
        logger.error("F5: caretaker ensure failed unexpectedly: %s", exc)
        raise ModelLoadError(str(exc)) from exc

    # The caretaker response names the model it actually loaded.  Without a
    # truthful loaded_model the gateway cannot tell whether the backend now
    # serves the requested model — never claim success from an unknown
    # caretaker state.  The same holds when the caretaker resolved the request
    # to a different model (caretaker-side alias/fallback/partial load): do
    # NOT adopt the requested model's loaded state, the backend would desync
    # from gateway state.
    #
    # NOTE: fail CLOSED here (ModelLoadError), not local_fallback().  The
    # local lifecycle safety rationale ("nothing else owns the backend port")
    # only holds when the caretaker is DOWN.  Here the daemon is alive and
    # owns the backend — running gateway load()/switch_model() against an
    # already-running instance would be a no-op that still flips current_model
    # to the requested model while the backend actually serves the caretaker's
    # different model: the exact desync we are preventing.  The hotpath maps
    # ModelLoadError to its existing crash/503 handling.
    if not isinstance(result, dict) or "loaded_model" not in (result or {}):
        logger.warning(
            "F5: caretaker /ensure response did not name a loaded_model — "
            "failing closed to avoid a second controller on the backend"
        )
        raise ModelLoadError(
            "Caretaker /ensure succeeded but did not report the loaded model"
        )
    loaded = result["loaded_model"]
    # Canonicalize BOTH sides through the gateway's resolver before comparing:
    # the caretaker may report the canonical name for an alias we sent (or
    # vice versa), and a raw string compare would turn a legitimate
    # alias-load into a false ModelLoadError/503 on every such request.
    # Unknown names on either side fall back to the raw string (the equality
    # then fails closed, which is correct).
    try:
        resolved_loaded = _model_manager.resolve_model(loaded) if _model_manager else loaded
    except ValueError:
        resolved_loaded = loaded
    try:
        resolved_requested = _model_manager.resolve_model(model) if _model_manager else model
    except ValueError:
        resolved_requested = model
    if resolved_loaded != resolved_requested:
        logger.warning(
            "F5: caretaker loaded '%s' instead of requested '%s' — "
            "failing closed to avoid a second controller on the backend",
            loaded,
            model,
        )
        raise ModelLoadError(
            f"Caretaker loaded '{loaded}' instead of requested '{model}'"
        )

    if _model_manager is not None:
        _model_manager.mark_loaded_by_caretaker(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
        # The remote path no longer runs the local switch_model body, which
        # owned the context restore — mirror it so a fresh remote load
        # recovers the target's auto-saved session history (missing/corrupt
        # save is tolerated inside the manager; restore never blocks the
        # hotpath).  Restore ONLY on the daemon's explicit "fresh_load": true
        # confirmation — the /ensure response is the authoritative freshness
        # signal.  A gateway-side probe (parsing the running llama-server's
        # command line) can misdetect a caretaker-launched process and would
        # clobber a live slot-0 session with a stale auto-save, so never
        # restore on a probe result alone.  The restore is gated on the SAME
        # capability snapshot as the pre-save (see the switch gate above), so
        # the first fresh_load-capable switch after a daemon upgrade behaves
        # like the pre-capability path (no save, no restore) instead of
        # restoring a stale auto-save without having saved the current
        # session.  Reload sites (auto-reload, connect-error recovery) start
        # a fresh context via load() and must not re-inject a stale auto-save
        # — they never set pre_switch_save.
        if pre_switch_save and capability_before and result.get("fresh_load") is True:
            await _model_manager.restore_current_context()
    return "remote"
