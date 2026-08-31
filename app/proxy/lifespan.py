"""Proxy lifespan — startup/shutdown orchestration and idle-unload watcher.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Owns pid-file handling, stale-listener cleanup, background startup model
verification, capture writer startup/shutdown, and the idle-unload watcher.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from contextlib import asynccontextmanager, suppress

from app.gateway.caretaker_client import CaretakerError, CaretakerUnavailable

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_proxy_port = None  # injected via init()
_pid_file = None  # injected via init()
_get_pid_file_path = None
_get_pid_file_status = None
_get_proxy_listener_info = None
_wait_for_proxy_listener_release = None
_is_guardian_uvicorn_listener = None
_stop_stale_guardian_listener = None
_reset_startup_check_status = None
_mark_startup_check_status = None
_operation_state_for_phase = None
_run_startup_check_in_background = None
_set_startup_check_task = None
_cancel_startup_check_task = None
_cloud_catalog = None
_catalog_refresh_interval_s = 60.0
_model_manager = None
_capture_controller = None
_inference_queue = None
# Injected via init() — the remote caretaker control-API client used for the
# lifecycle *execution* of idle-unload.  None until injected by server.py.
_caretaker_client = None


def init(
    *,
    proxy_port,
    pid_file,
    get_pid_file_path,
    get_pid_file_status,
    get_proxy_listener_info,
    wait_for_proxy_listener_release,
    is_guardian_uvicorn_listener,
    stop_stale_guardian_listener,
    reset_startup_check_status,
    mark_startup_check_status,
    operation_state_for_phase,
    run_startup_check_in_background,
    set_startup_check_task,
    cancel_startup_check_task,
    model_manager,
    capture_controller,
    inference_queue,
    caretaker_client=None,
    cloud_catalog=None,
    catalog_refresh_interval_s: float = 60.0,
) -> None:
    """Inject all dependencies. Called once at startup."""
    globals()["_cancel_startup_check_task"] = cancel_startup_check_task
    globals()["_proxy_port"] = proxy_port
    globals()["_pid_file"] = pid_file
    globals()["_get_pid_file_path"] = get_pid_file_path
    globals()["_get_pid_file_status"] = get_pid_file_status
    globals()["_get_proxy_listener_info"] = get_proxy_listener_info
    globals()["_wait_for_proxy_listener_release"] = wait_for_proxy_listener_release
    globals()["_is_guardian_uvicorn_listener"] = is_guardian_uvicorn_listener
    globals()["_stop_stale_guardian_listener"] = stop_stale_guardian_listener
    globals()["_reset_startup_check_status"] = reset_startup_check_status
    globals()["_mark_startup_check_status"] = mark_startup_check_status
    globals()["_operation_state_for_phase"] = operation_state_for_phase
    globals()["_run_startup_check_in_background"] = run_startup_check_in_background
    globals()["_set_startup_check_task"] = set_startup_check_task
    globals()["_model_manager"] = model_manager
    globals()["_capture_controller"] = capture_controller
    globals()["_inference_queue"] = inference_queue
    globals()["_caretaker_client"] = caretaker_client
    globals()["_cloud_catalog"] = cloud_catalog
    globals()["_catalog_refresh_interval_s"] = float(catalog_refresh_interval_s)


async def _catalog_refresh_loop() -> None:
    """Periodically refresh stale provider catalogs (fail-open).

    Each pass is TTL-gated per provider: while every cached catalog is fresh
    the pass costs nothing; a cold cache (fresh install) or an expired TTL
    self-heals without operator action. Any pass failure is logged and the
    loop continues — a broken provider catalog must never take the refresher
    down.
    """
    while True:
        try:
            await _cloud_catalog.ensure_all_fresh()
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            logger.warning("☁️  Catalog refresh pass failed: %s", exc)
        await asyncio.sleep(_catalog_refresh_interval_s)


@asynccontextmanager
async def run_lifespan(app):

    # Startup: Check and write PID file
    pid_path = _get_pid_file_path()
    pid_file_status = _get_pid_file_status()
    if pid_path.exists():
        try:
            with open(pid_path, 'r') as f:
                content = f.read().strip()
                if content:
                    old_pid = int(content)
                    # Check if process exists
                    if old_pid != os.getpid():
                        try:
                            os.kill(old_pid, 0)
                            listener = _get_proxy_listener_info()
                            if listener and listener.get("pid") == old_pid:
                                released = await _wait_for_proxy_listener_release(old_pid)
                                if released:
                                    logger.info(
                                        f"Existing Guardian listener PID {old_pid} released port {_proxy_port} during restart handoff"
                                    )
                                else:
                                    logger.warning(
                                        f"Listener PID {old_pid} still holds port {_proxy_port}; continuing and relying on bind protection"
                                    )
                            logger.warning(
                                f"Found active PID {old_pid} in {_pid_file}; overwriting it and continuing startup. "
                                "Socket binding will still prevent duplicate Guardian listeners."
                            )
                        except OSError as e:
                            if e.errno == errno.ESRCH:
                                logger.warning(f"Found stale PID file for PID {old_pid}. Overwriting.")
                            else:
                                raise e
        except ValueError:
             logger.warning("Invalid PID file found. Overwriting.")
        except FileNotFoundError:
            pass

    existing_listener = _get_proxy_listener_info()
    if existing_listener and not existing_listener.get("is_current_process"):
        listener_pid = existing_listener.get("pid")
        pid_file_pid = pid_file_status.get("pid")
        if isinstance(listener_pid, int) and listener_pid == pid_file_pid:
            released = await _wait_for_proxy_listener_release(listener_pid)
            if released:
                logger.info(
                    f"Existing Guardian listener PID {listener_pid} released port {_proxy_port} during startup handoff"
                )
            else:
                logger.warning(
                    f"Listener PID {listener_pid} still holds port {_proxy_port}; continuing and relying on bind protection"
                )
        elif _is_guardian_uvicorn_listener(existing_listener):
            stopped = await _stop_stale_guardian_listener(existing_listener)
            if not stopped:
                logger.warning(
                    f"Detected Guardian listener PID {listener_pid} on port {_proxy_port}, but it did not exit during orphan cleanup"
                )
        else:
            logger.warning(
                f"Port {_proxy_port} is already owned by an unexpected process; startup may fail: {existing_listener}"
            )

    try:
        with open(pid_path, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Guardian started with PID {os.getpid()}")
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")

    # SECURITY: Run startup model verification in the background so Guardian
    # binds on 11434 immediately while llama-server is still warming up.
    startup_target = _model_manager.pinned_model or _model_manager.current_model
    generation = _reset_startup_check_status(
        source="startup",
        phase="startup_check",
        target_model=startup_target,
        requested_model=startup_target,
        owner="startup",
    )
    _mark_startup_check_status(
        _operation_state_for_phase("startup_check"),
        generation=generation,
        source="startup",
        phase="startup_check",
        owner="startup",
        target_model=startup_target,
        requested_model=startup_target,
    )
    logger.info("🔄 Scheduling startup model verification in background")
    _set_startup_check_task(
        asyncio.create_task(_run_startup_check_in_background(generation, startup_target))
    )

    # Start capture writer (fail-open: disabled by default, errors are logged not raised)
    _capture_writer_task: asyncio.Task | None = None  # writer task owned by _capture_controller
    try:
        _capture_controller.initialize_writer()
        if _capture_controller.config.is_active:
            await _capture_controller.start_writer()
            logger.info("📸 Capture writer started (instance_id=%s)",
                        _capture_controller.config.instance_id)
        else:
            logger.info("📸 Capture subsystem is disabled (enabled=false)")
    except Exception as exc:
        logger.warning("Capture writer initialization failed (fail-open): %s", exc)

    # Start idle-unload background watcher
    idle_task = asyncio.create_task(idle_unload_watcher())

    # Start TTL-gated cloud-catalog refresher (self-heals a cold/stale cache;
    # None when the catalog was not injected — e.g. in unit tests).
    catalog_task: Optional[asyncio.Task] = None
    if _cloud_catalog is not None:
        catalog_task = asyncio.create_task(_catalog_refresh_loop())

    yield

    if catalog_task is not None:
        catalog_task.cancel()
    idle_task.cancel()
    _set_startup_check_task(None)
    _cancel_startup_check_task()

    if catalog_task is not None:
        with suppress(asyncio.CancelledError):
            await catalog_task
    with suppress(asyncio.CancelledError):
        await idle_task

    # Shutdown: Stop capture writer
    try:
        await _capture_controller.stop_writer()
    except Exception as exc:
        logger.warning("Capture writer shutdown error: %s", exc)

    # Shutdown: Release the caretaker httpx connection pool (review: resource
    # leak — the AsyncClient was built eagerly at import; close it so the pool
    # does not linger for the process lifetime / per re-import).
    await _close_caretaker_client()

    # Shutdown: Remove PID file
    if pid_path.exists():
        try:
            with open(pid_path, 'r') as f:
                content = f.read().strip()
                if content and int(content) == os.getpid():
                     pid_path.unlink()
                     logger.info("PID file removed.")
        except Exception as e:
            logger.warning(f"Failed to clean up PID file: {e}")


async def _close_caretaker_client() -> None:
    """Close the caretaker httpx connection pool if one was built."""
    if _caretaker_client is not None:
        try:
            await _caretaker_client.close()
        except Exception as exc:  # noqa: BLE001 — shutdown guard; any failure is logged, never propagated
            logger.warning("Caretaker client shutdown error: %s", exc)


async def idle_unload_watcher():
    """Background task: auto-unload llama-server after N minutes of inactivity."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        idle_minutes = _model_manager.idle_unload_minutes
        if idle_minutes is None:
            continue  # Feature disabled
        if _model_manager.is_unloaded:
            continue  # Already free
        if _model_manager.active_requests > 0:
            continue  # Don't unload while requests are in-flight
        if _inference_queue.active_count > 0 or _inference_queue.waiting_count > 0:
            continue  # Don't unload while queue has pending work
        idle_secs = time.time() - _model_manager.last_request_time
        if idle_secs >= idle_minutes * 60:
            logger.info(f"💤 Idle for {idle_secs/60:.1f}m (limit {idle_minutes}m) — auto-unloading to free VRAM")
            # Snapshot is only taken on the caretaker branch; None here means
            # no optimistic mark was made, so the except-handlers must skip the
            # rollback (avoids UnboundLocalError when the local fallback unload
            # raises — review: possible bug).
            _prev_state = None
            try:
                if _caretaker_client is None:
                    # No remote caretaker configured (management_url/CARETAKER_KEY
                    # or the daemon itself absent): fall back to the local unload
                    # so VRAM freeing keeps working during the F5 transition — the
                    # caretaker owns the lifecycle only once it is deployed.  The
                    # review painted this as a deployment-dependency regression if
                    # merged without a fallback (review: possible issue).
                    logger.warning("Caretaker client not configured; falling back to local unload")
                    await _model_manager.unload()
                    continue
                # Optimistic: mark unloaded BEFORE the round-trip so a request
                # arriving while the caretaker is stopping the backend already
                # sees is_unloaded=True and the hotpath auto-reload fires —
                # instead of routing it at a backend that is about to stop.
                # Same end-state as unload() (minus the process stop the
                # caretaker does): is_unloaded + verification-state reset.
                _prev_state = _model_manager.snapshot_unload_state()
                _model_manager.mark_unloaded_by_caretaker()
                await _caretaker_client.unload()
                # A concurrent hotpath reload may have started during the
                # round-trip (the state is no longer the optimistic mark the
                # caretaker's stop raced against).  The caretaker's stop and
                # the reload's start race; log so the operator knows the
                # backend state is uncertain (review: possible race).
                if not (
                    _model_manager.is_unloaded is True
                    and _model_manager._model_verified is False
                    and _model_manager._last_verification_at is None
                    and _model_manager._last_backend_model is None
                ):
                    logger.warning(
                        "Caretaker unload completed but a concurrent reload changed "
                        "the manager state during the round-trip; backend state uncertain"
                    )
            except CaretakerUnavailable as e:
                # Transport failure: the daemon may be down (not deployed yet
                # during F5, crashed, network down).  We cannot know whether the
                # unload happened; the local systemctl stop is idempotent either
                # way.  Only fall back when the optimistic state is STILL ours
                # (rollback succeeded) — a concurrent reload means another path
                # manages the backend (review: possible regression — VRAM would
                # otherwise never be freed until the daemon returns).
                if _prev_state is not None and _model_manager.rollback_unload_if_unchanged(_prev_state):
                    logger.warning("Auto-unload via caretaker unavailable; falling back to local unload: %s", e)
                    try:
                        await _model_manager.unload()
                    except Exception:
                        # An exception raised inside an except-handler is NOT
                        # caught by the later sibling 'except Exception' — wrap
                        # the fallback so a failing local unload logs instead of
                        # killing the watcher task (review: possible bug).
                        logger.exception("Local fallback unload failed after caretaker unavailable")
                else:
                    logger.error(f"❌ Auto-unload via caretaker failed: {e}")
            except CaretakerError as e:
                # Expected: remote refusal — the caretaker never stopped the
                # backend.  Roll back the FULL optimistic state (flag AND the
                # verification metadata mark_unloaded_by_caretaker cleared) —
                # but only if nothing mutated it meanwhile, otherwise a stale
                # snapshot would clobber a concurrent reload (review: possible
                # race).  Guarded: returns False when a newer state superseded it.
                if _prev_state is not None:
                    _model_manager.rollback_unload_if_unchanged(_prev_state)
                logger.error(f"❌ Auto-unload via caretaker failed: {e}")
            except Exception as e:
                # Unexpected (coding error, transport bug): the unload was NOT
                # confirmed (CaretakerError covers the confirmed refusals), so
                # the backend is still up — roll the optimistic mark back the
                # same guarded way, else is_unloaded stays True over a running
                # backend and every later unload attempt is skipped (review:
                # possible bug).  Surface the traceback so the root cause shows.
                if _prev_state is not None:
                    _model_manager.rollback_unload_if_unchanged(_prev_state)
                logger.exception("❌ Auto-unload via caretaker failed unexpectedly")
                logger.error(f"  {e}")
