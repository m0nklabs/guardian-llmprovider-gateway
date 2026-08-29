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
- ``CaretakerUnavailable`` (transport/timeout/auth) → a single re-probe of
  ``/ensure`` happens first (a live daemon mid-switch would otherwise race a
  local spawn — two controllers on the same backend).  Only a second
  ``CaretakerUnavailable`` counts as "daemon really gone": the backend may
  still be healthy (daemon died but llama-server survived) — if so, adopt the
  loaded state without spawning.  Otherwise run the original local lifecycle
  (``local_fallback``) — safe, because with the daemon down nothing else owns
  the backend port.
- Any other ``CaretakerError`` (model not found, VRAM limit, load failed,
  invalid request) → mapped to the same error types the hotpath callers
  already handle (``ModelLoadError`` / ``ValueError``), so crash recording and
  the 503 paths keep working unchanged.
- Client ``None`` (no caretaker configured) → ``local_fallback`` directly —
  exact pre-F5 behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.engine.manager import ModelLoadError
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

    ``CaretakerUnavailable`` covers transport timeouts as well as a dead
    daemon.  A live daemon that is mid-switch (VRAM freeing + a large-model
    load can outlast the client timeout) would race a local spawn — two
    controllers on the same backend port / launch-args file.  Re-probing once
    lets a merely-slow daemon answer; only a second ``CaretakerUnavailable``
    is treated as "the daemon is really gone", where the local lifecycle is
    the only controller on the backend.  A ``CaretakerUnavailable`` with a
    ``status_code`` (daemon alive but rejected the request, e.g. 401/403) is
    deterministic — re-probing would just get the same status again, so it
    propagates immediately.
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
    return await _caretaker_client.ensure(
        model,
        enable_vision=enable_vision,
        context_hint=context_hint,
    )


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

    if pre_switch_save and _model_manager is not None:
        await _model_manager.save_current_context()

    # Was the target already being served before the ensure?  If yes, the
    # /ensure is idempotent — the backend slot already holds the live session
    # for that model, and a later restore would clobber it.  If not, the
    # caretaker freshly loaded the model (empty slot) and restoring the
    # auto-saved history is the safe mirror of switch_model's restore.
    fresh_load = not (
        _model_manager is not None
        and await _model_manager.backend_serves_model(model)
    )

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
            # Restore the auto-saved context only when the model was FRESHLY
            # loaded (fresh_load=True): the timed-out /ensure may have
            # completed the switch, so slot 0 is empty and restoring mirrors
            # switch_model's restore.  When the backend was already serving
            # the model before the ensure (fresh_load=False), the live session
            # in slot 0 is authoritative — never restore, it would clobber an
            # active conversation.
            if pre_switch_save and fresh_load and _model_manager is not None:
                await _model_manager.restore_current_context()
            return "local-healthy"
        logger.warning(
            "F5: caretaker unavailable — local load fallback for '%s'", model
        )
        await local_fallback()
        return "local"
    except CaretakerModelNotFound as exc:
        raise ModelLoadError(str(exc)) from exc
    except CaretakerVramExceeded as exc:
        raise ModelLoadError(str(exc)) from exc
    except CaretakerModelLoadFailed as exc:
        raise ModelLoadError(str(exc)) from exc
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
        # hotpath).  Only restore when the ensure actually freshly loaded the
        # model (backend was NOT serving it before, so the slot is empty) AND
        # this is a switch site (pre_switch_save): on an idempotent /ensure
        # the slot already holds the live session and restoring would clobber
        # it; reload sites (auto-reload, connect-error recovery) start a
        # fresh context via load() and must not re-inject a stale auto-save.
        if pre_switch_save and fresh_load:
            await _model_manager.restore_current_context()
    return "remote"
