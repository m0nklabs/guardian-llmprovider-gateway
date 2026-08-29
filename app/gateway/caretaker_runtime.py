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
- ``CaretakerUnavailable`` (transport/timeout/auth) → the backend may still be
  healthy (daemon died but llama-server survived): if so, adopt the loaded
  state without spawning.  Otherwise run the original local lifecycle
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


async def ensure_backend(
    *,
    model: str,
    enable_vision: bool | None = None,
    context_hint: int | None = None,
    local_fallback: Callable[[], Awaitable[None]],
    pre_switch_save: bool = False,
) -> str:
    """Ensure ``model`` is loaded/active on the local backend.

    Returns ``"remote"`` (caretaker performed the ensure), ``"local"`` (the
    gateway's own lifecycle ran) or ``"local-healthy"`` (no spawn — the
    backend was already healthy after a caretaker outage).  Raises the same
    error types the hotpath callers already handle (``ModelLoadError`` /
    ``ValueError``).
    """
    if _caretaker_client is None:
        await local_fallback()
        return "local"

    if pre_switch_save and _model_manager is not None:
        await _model_manager.save_current_context()

    try:
        await _caretaker_client.ensure(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
    except CaretakerUnavailable:
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

    if _model_manager is not None:
        _model_manager.mark_loaded_by_caretaker(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
    return "remote"
