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
  means the daemon accepted the connection (alive): a second one fails closed
  UNLESS the backend now serves the requested model — the adoption probe polls
  a bounded ~60s window (a cold load can outlast the client timeout and pre-F5
  ``load()``/``switch_model()`` waited for backend health, so a single probe
  right after ~2× the client timeout would 503 most cold switches; the
  in-flight /ensure is the only controller, so adopting the confirmed load is
  safe).  Connection-refused/DNS/ConnectTimeout mean the daemon may be gone:
  the backend may still be healthy (daemon died but llama-server survived) —
  if so, adopt the loaded state without spawning.  A hard connection-refused
  with a healthy backend is the one case that preserves the pre-F5
  auto-switch: nothing listening on the management port is the strongest
  evidence the daemon process is gone, so after a short bounded re-bind poll
  (5× 1s — a restarting daemon's port can stay closed for RestartSec +
  startup) the local lifecycle runs — unless any poll attempt finds the
  daemon re-bound and rejecting (status_code) or re-bound and busy
  (read/write timeout), which fail closed like every other live-daemon case.
  Transport errors where a connection WAS established (``ReadError``/
  ``WriteError`` resets, ``RemoteProtocolError``, ``PoolTimeout``) mean the
  daemon accepted a connection — it is alive (or was when it dropped us) and
  still owns the backend, even when the backend health probe fails; those
  fail closed too.  Only a hard refused/ConnectTimeout reaches the local
  lifecycle with the backend down.  If the backend is ALIVE but not serving
  the requested model (or serves it without the requested vision config),
  the local lifecycle would race a second controller → fail closed.  Only
  when the backend is confirmed DOWN does the original local lifecycle
  (``local_fallback``) run — safe, because with the daemon down nothing else
  owns the backend port.
- Vision integrity: whenever state is mirrored to the gateway manager
  (``mark_loaded_by_caretaker``) — on the success path or on adoption — and
  the request needs vision, the LIVE process must confirm mmproj via
  ``current_runtime_uses_mmproj`` (the daemon writes the args file it launches
  with, same path the gateway reads).  Stamping ``current_vision_enabled=True``
  on a text-only process would forward image requests to a backend that cannot
  serve them; both paths fail closed instead.
- Any other ``CaretakerError`` (model not found, VRAM limit, load failed,
  invalid request) → mapped to the same error types the hotpath callers
  already handle (``ModelLoadError`` / ``ValueError``), so crash recording
  (the daemon's ``crash_details`` become the raised ``crash_record``) and the
  503 paths keep working unchanged.
- Client ``None`` (no caretaker configured) → ``local_fallback`` directly —
  exact pre-F5 behaviour.
"""

from __future__ import annotations

import asyncio
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

# Bounded adoption-poll window in seconds.  The poll runs under the caller's
# model-switch lock (auto-reload/auto-switch hold it for the whole ensure), so
# the bound must stay tight — but pre-F5 load()/switch_model() waited on
# backend health WITHOUT any cap, so a too-short bound would 503 cold loads
# (the in-flight /ensure is the only controller; the daemon is healthy and
# mid-load).  120 covers the documented 10-30s cold-load range, the ~10s the
# two /ensure attempts already consumed, and the deployment's largest models
# (14700K-class GGUFs can exceed 60s on a cold start); the poll is bounded
# and exits as soon as the backend confirms, so the window only costs lock
# time while a cold load is genuinely in flight.  Configurable via settings
# once the runtime is wired to global.settings.yaml; tests patch asyncio.sleep.
_ADOPT_POLL_SECONDS = 120


def init(*, model_manager, caretaker_client) -> None:
    """Bind the runtime dependencies (mirrors the Phase-5 init() pattern)."""
    global _caretaker_client, _model_manager
    _caretaker_client = caretaker_client
    _model_manager = model_manager


async def _backend_now_serving(
    model: str,
    *,
    enable_vision: bool | None,
    context_hint: int | None,
) -> dict | None:
    """Return a synthetic /ensure success if the backend now serves ``model``.

    Used when the daemon's in-flight /ensure outlived our re-probe timeouts (a
    cold large-model load can take 10-30s while the client timeout is seconds):
    adoption only mirrors state the live process already reports — no spawn,
    no args-file write, no second controller — so the daemon stays the sole
    controller.  ``None`` when the backend does not serve the requested model,
    is not healthy, or its launch config drifts from what this request needs
    (same guards as the outage-adopt path).  The vision guard runs in the
    common success path that consumes this dict.
    """
    mgr = _model_manager
    if mgr is None:
        return None
    if not await mgr.backend_serves_model(model):
        return None
    if not await mgr.backend_health_ok():
        return None
    if mgr._config_drifted(
        model, enable_vision=enable_vision, context_hint=context_hint
    ):
        return None
    return {"loaded_model": model}


async def _await_backend_serving(
    model: str,
    *,
    enable_vision: bool | None,
    context_hint: int | None,
) -> dict | None:
    """Poll a bounded window for the backend to confirm it serves ``model``.

    A cold model load can outlast the client timeout more than once; the
    daemon's in-flight /ensure is the only controller.  Pre-F5
    load()/switch_model() waited for backend health, so fail-closing on a
    single probe would still 503 most cold switches — the probe runs right
    after ~2× the client timeout (10s at the 5s default), while a 10-30s load
    is barely started.  Poll once per second up to the bounded
    ``_ADOPT_POLL_SECONDS`` window; ``None`` if the load never confirms.
    Each probe is local and cheap (no daemon round-trip), and the loop exits
    as soon as the backend confirms — a healthy loading daemon only costs the
    lock-hold time a cold switch inherently needs.

    A HEALTHY backend that does not serve the requested model is a
    determined outcome, not a load in flight: the daemon's switch is
    stop→start (stop old llama-server, free GPU, start new), so while a
    model is actually loading the backend is DOWN, not healthy.  If the
    backend is healthy and serves a different model, the in-flight ensure
    evidently resolved elsewhere (or never arrived) — waiting the full
    window cannot change that, and holding the global switch lock for
    ``_ADOPT_POLL_SECONDS`` would block every other auto-reload/auto-switch
    across all clients.  Give an in-place model swap a 3s grace, then fail
    closed promptly (``None``) instead of burning the whole window.
    """
    wrong_model_probes = 0
    for _ in range(_ADOPT_POLL_SECONDS):
        await asyncio.sleep(1.0)
        adopted = await _backend_now_serving(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
        if adopted is not None:
            return adopted
        # The backend is healthy but does not serve the requested model:
        # the in-flight ensure is evidently not loading it (a real load
        # keeps the backend DOWN during stop→start, so health-ok means the
        # outcome is already determined).  Give an in-place model swap a
        # short grace (3s), then fail closed promptly instead of holding
        # the switch lock for the full _ADOPT_POLL_SECONDS window.
        if _model_manager is not None and await _model_manager.backend_health_ok():
            wrong_model_probes += 1
            if wrong_model_probes >= 3:
                return None
    return None


def _map_caretaker_error(exc: CaretakerError) -> Exception:
    """Map a caretaker error to the error type the hotpath callers handle.

    ``CaretakerModelNotFound``/``CaretakerVramExceeded``/``CaretakerModelLoadFailed``
    become ``ModelLoadError`` (the daemon's ``crash_details`` — its
    ``CrashRecord.to_dict()`` — become the raised ``crash_record`` so the
    hotpath crash recording keeps working unchanged); ``CaretakerInvalidRequest``
    becomes ``ValueError``; anything else fails closed into ``ModelLoadError``.
    """
    if isinstance(exc, CaretakerModelNotFound):
        return ModelLoadError(str(exc))
    if isinstance(exc, CaretakerVramExceeded):
        return ModelLoadError(str(exc))
    if isinstance(exc, CaretakerModelLoadFailed):
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
        return ModelLoadError(str(exc), crash_record=crash_record)
    if isinstance(exc, CaretakerInvalidRequest):
        return ValueError(str(exc))
    logger.error("F5: caretaker ensure failed unexpectedly: %s", exc)
    return ModelLoadError(str(exc))


async def _complete_remote(
    model: str,
    result: dict,
    *,
    enable_vision: bool | None,
    context_hint: int | None,
    pre_switch_save: bool,
    capability_before: bool,
) -> str:
    """Finalize a SUCCESSFUL remote ensure: verify the response names the
    requested model, guard vision integrity, mirror the loaded state and
    return ``"remote"``.

    Shared by the primary path and the bounded re-probe after a refused
    connection (restart window), so both run identical validation.
    """
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
        # Mirror the adopt-path guard: when the request EXPLICITLY needs
        # vision (enable_vision=True) the daemon may have resolved it
        # differently than the gateway (own config / hot config edit /
        # daemon-side vision resolution dropping mmproj).  Stamping
        # current_vision_enabled=True on a text-only process would forward
        # image requests to a backend that cannot serve them — fail closed
        # instead of desyncing (the probe is reliable here: the daemon writes
        # the args file it launches with, in the same CURRENT_MODEL_ARGS_FILE
        # the gateway reads).
        #
        # With enable_vision=None (ollama bridge / connect-error recovery
        # reloads) the resolved flag reflects the gateway's CURRENT stamp,
        # not the request's need — a daemon legitimately serving a
        # vision-capable model text-only (VRAM limits, daemon config without
        # mmproj) must NOT 503 a text request.  Mirror the LIVE process's
        # mmproj state instead so current_vision_enabled stays truthful and a
        # later image request still triggers a (vision-enabled) switch.
        if (
            enable_vision is True
            and not _model_manager.current_runtime_uses_mmproj(model)
        ):
            raise ModelLoadError(
                f"Caretaker loaded '{loaded}' without mmproj while vision "
                "was requested"
            )
        effective_vision = (
            _model_manager.current_runtime_uses_mmproj(model)
            if enable_vision is None
            else bool(enable_vision)
        )
        _model_manager.mark_loaded_by_caretaker(
            model,
            enable_vision=effective_vision,
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
        if (
            pre_switch_save
            and capability_before
            and result.get("fresh_load") is True
            and _model_manager is not None
        ):
            await _model_manager.restore_current_context()
    return "remote"


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

    - status_code set (≠200) → daemon alive but rejected the request
      (auth/ownership/malformed status — deterministic, no re-probe).
    - ``status_code == 200`` with a non-dict/empty body → the daemon answered
      but the body was unparseable (transient intermediary HTML/empty page,
      momentary glitch).  That is NOT an auth/ownership rejection, so it is
      re-probed once like transport errors; a second 200-body failure still
      fails closed (the daemon is demonstrably alive — it answered 200 — so
      the local lifecycle stays forbidden).
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
        # A body-parse failure is mapped with status_code=200 (the daemon
        # answered, so it IS alive) — but it is not an auth/ownership
        # rejection, so give it the transport re-probe instead of failing
        # closed on a single transient glitch (intermediary HTML/empty page).
        # A second 200-body failure still fails closed below: the daemon is
        # demonstrably alive, so the local lifecycle stays forbidden.
        if exc.status_code is not None and exc.status_code != 200:
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
                    # A cold model load can outlast the client timeout more
                    # than once; the daemon's in-flight /ensure is the only
                    # controller.  Pre-F5 load()/switch_model() waited for
                    # backend health, so 503ing on a single probe would be a
                    # regression on every cold switch (the probe runs right
                    # after ~2× the client timeout, while a 10-30s load is
                    # barely started).  Poll a bounded window for the load to
                    # confirm before failing closed.
                    adopted = await _await_backend_serving(
                        model,
                        enable_vision=enable_vision,
                        context_hint=context_hint,
                    )
                    if adopted is not None:
                        logger.info(
                            "F5: /ensure timed out but backend now serves "
                            "'%s' — adopting loaded state",
                            model,
                        )
                        return adopted
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
                # Same as above: the daemon came back but is busy — poll a
                # bounded window for the backend to confirm the load, else
                # fail closed.
                adopted = await _await_backend_serving(
                    model,
                    enable_vision=enable_vision,
                    context_hint=context_hint,
                )
                if adopted is not None:
                    logger.info(
                        "F5: /ensure re-probe timed out but backend now serves "
                        "'%s' — adopting loaded state",
                        model,
                    )
                    return adopted
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

    # Snapshot the daemon capability BEFORE the /ensure call.  The RESTORE
    # stays gated on that snapshot: gating it on the response's fresh_load
    # instead would be asymmetric on the very first switch after the daemon
    # starts shipping fresh_load — the restore (gated on the response, now
    # present) would run while this switch's session was never saved,
    # re-injecting the target model's possibly stale auto-save and silently
    # losing the current session.  The PRE-SAVE is deliberately NOT gated:
    # with pre_switch_save the caller explicitly asked for context
    # persistence, and skipping it loses the active session on every A->B->A
    # cycle while the daemon has not shipped fresh_load yet (pre-F5
    # switch_model always saved).  Saving unconditionally only prevents loss;
    # the transition switch still behaves pre-capability (no restore) and
    # save/restore come back symmetrically from the second switch onward.
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
        # ALWAYS save before a remote switch: with pre_switch_save the caller
        # explicitly asked for context persistence, and skipping it loses the
        # active session on every A->B->A cycle (pre-F5 switch_model always
        # saved).  Only the RESTORE stays gated on the same capability
        # snapshot (see _complete_remote), so the first switch after the
        # daemon starts shipping fresh_load still behaves pre-capability —
        # saving now only prevents the current session from being lost in the
        # meantime.
        if not capability_before:
            logger.warning(
                "F5: /ensure cannot confirm fresh_load — remote switch to "
                "'%s' SAVES context but will NOT restore it until the daemon "
                "ships the field",
                model,
            )
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
            # backend that cannot serve it.  So when the request EXPLICITLY
            # needs vision (enable_vision=True), also require the LIVE
            # process to actually use mmproj (the args file is written by
            # whoever launched the backend — gateway OR daemon — so the
            # probe reflects the real process).  With enable_vision=None
            # (reloads) the request has no vision need, so adoption must not
            # fail closed on the stamp mismatch — the mark below mirrors the
            # live state instead.
            and not (
                enable_vision is True
                and not _model_manager.current_runtime_uses_mmproj(model)
            )
        ):
            logger.warning(
                "F5: caretaker unreachable but backend already serves '%s' — "
                "adopting loaded state without respawn",
                model,
            )
            effective_vision = (
                _model_manager.current_runtime_uses_mmproj(model)
                if enable_vision is None
                else bool(enable_vision)
            )
            _model_manager.mark_loaded_by_caretaker(
                model,
                enable_vision=effective_vision,
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
        # A transport failure does NOT by itself prove the daemon is gone: a
        # firewall DROP on the management port, saturated accept backlog or a
        # restart window all surface as timeouts while the daemon still owns
        # llama-server.  If the backend is ALIVE (a real llama-server answers),
        # spawning/switching locally would race a second controller on it
        # (args-file write + systemctl stop/start) — the same hazard the
        # timeout branch fails closed on, and the state may be a live daemon
        # serving a different model than requested.
        #
        # A hard connection-refused is the one exception: nothing is listening
        # on the management port (RST on connect), which is the strongest
        # evidence the daemon process is definitively gone — no live daemon
        # owns the backend, so the local lifecycle is the sole controller and
        # the pre-F5 auto-switch is preserved.  ConnectTimeout is a SUBCLASS
        # of ConnectError and stays fail-closed (SYN never answered — DROP,
        # not dead).
        if _model_manager is not None and await _model_manager.backend_health_ok():
            if (
                isinstance(exc.__cause__, httpx.ConnectError)
                and not isinstance(exc.__cause__, httpx.ConnectTimeout)
            ):
                # A daemon that is merely RESTARTING (systemd Restart=always,
                # deploy window) can have its management port momentarily
                # closed (RST) for LONGER than a single re-bind wait while its
                # llama-server child survives (RestartSec + startup).  Poll
                # the port in small bounded steps so a re-bind within the
                # window completes the remote path instead of taking over the
                # backend as a second controller, which would then race the
                # restarting daemon on the shared backend / launch-args file.
                # The 15s window covers the deployment's RestartSec + python
                # startup budget (commonly >7s); both probes only take ~1s
                # each on refused connects, so the poll dominates the
                # restart-window wait as intended.
                rechecked = None
                for _ in range(15):
                    await asyncio.sleep(1.0)
                    try:
                        rechecked = await _caretaker_client.ensure(
                            model,
                            enable_vision=enable_vision,
                            context_hint=context_hint,
                        )
                        break  # daemon re-bound: remote path completes below
                    except CaretakerUnavailable as recheck_unavail:
                        # The re-probe result decides whether the daemon is
                        # really gone or merely still up:
                        # - status_code set → the daemon re-bound its port and
                        #   rejected us (e.g. 401/403 after a key rotation):
                        #   it owns the backend — fail closed like the outer
                        #   branch.
                        # - Read/WriteTimeout → the daemon re-bound and
                        #   accepted the connection but is busy: alive — fail
                        #   closed.
                        # - any other transport error (refused/DNS/
                        #   ConnectTimeout) → still nothing listening: keep
                        #   polling; if the window elapses the local lifecycle
                        #   is treated as the sole controller.
                        if recheck_unavail.status_code is not None:
                            raise ModelLoadError(
                                f"Caretaker control-API rejected the request "
                                f"(status {recheck_unavail.status_code}); refusing "
                                "local fallback to avoid a second controller on the "
                                "backend"
                            ) from recheck_unavail
                        if isinstance(
                            recheck_unavail.__cause__,
                            (httpx.ReadTimeout, httpx.WriteTimeout),
                        ):
                            raise ModelLoadError(
                                "Caretaker re-bound but unresponsive; refusing "
                                "local fallback to avoid a second controller on "
                                "the backend"
                            ) from recheck_unavail
                        # still down — continue polling; rechecked stays None
                    except CaretakerError as recheck_exc:
                        # The daemon is back and answered with an error: map it
                        # the same way the main path does (fail closed).
                        raise _map_caretaker_error(recheck_exc) from recheck_exc
                if rechecked is not None:
                    logger.info(
                        "F5: caretaker re-bound after restart window — "
                        "completing remote ensure for '%s'",
                        model,
                    )
                    return await _complete_remote(
                        model,
                        rechecked,
                        enable_vision=enable_vision,
                        context_hint=context_hint,
                        pre_switch_save=pre_switch_save,
                        capability_before=capability_before,
                    )
                logger.warning(
                    "F5: caretaker management port closed (connection refused) "
                    "— running local lifecycle fallback for '%s'",
                    model,
                )
            else:
                raise ModelLoadError(
                    "Caretaker unreachable but backend is alive; refusing local "
                    "fallback to avoid a second controller on the backend"
                ) from exc
        # Transport errors where a connection was ESTABLISHED (read/write
        # resets, protocol errors, pool exhaustion) mean the daemon accepted a
        # connection — it is alive (or was alive when it dropped us) and still
        # owns the backend, even when the backend health probe fails (the
        # daemon is busy mid-switch and dropped the control connection while
        # llama-server restarts).  Spawning/switching locally would race it;
        # fail closed instead — same rationale as the timeout branch.  Only a
        # hard connection-refused (RST) or ConnectTimeout is strong evidence
        # the daemon process is truly gone, and only those reach the local
        # lifecycle below.  ReadTimeout/WriteTimeout are handled inside
        # _ensure_with_retry and never surface here.
        cause = exc.__cause__
        if isinstance(
            cause,
            (
                httpx.ReadError,
                httpx.WriteError,
                httpx.PoolTimeout,
                httpx.RemoteProtocolError,
            ),
        ):
            raise ModelLoadError(
                "Caretaker unreachable but accepted a connection "
                f"({type(cause).__name__}); refusing local fallback to avoid "
                "a second controller on the backend"
            ) from exc
        logger.warning(
            "F5: caretaker unavailable and backend down — local load fallback "
            "for '%s'",
            model,
        )
        await local_fallback()
        return "local"
    except CaretakerError as exc:
        raise _map_caretaker_error(exc) from exc

    return await _complete_remote(
        model,
        result,
        enable_vision=enable_vision,
        context_hint=context_hint,
        pre_switch_save=pre_switch_save,
        capability_before=capability_before,
    )
