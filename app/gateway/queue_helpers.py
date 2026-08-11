"""Queue lifecycle helpers — request registration, disconnect watch, cancel.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
These helpers manage the inference-queue lifecycle around each request.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED

from app.proxy.queue import QueueAdmissionRejected, QueueRequestCancelled

logger = logging.getLogger("Guardian")

# ── Injected ─────────────────────────────────────────────────────────
_inference_queue = None
_get_queue_owner_id = None  # callable(request, client_id) -> str | None
_update_live_request_usage = None  # callable(request, **kwargs) -> None
STREAM_CLOSE_TIMEOUT_S: float = 5.0


def init(inference_queue, get_queue_owner_id, update_live_request_usage, close_timeout_s: float) -> None:
    """Inject the queue singleton and helper callables. Called once at startup."""
    global _inference_queue, _get_queue_owner_id, _update_live_request_usage, STREAM_CLOSE_TIMEOUT_S
    _inference_queue = inference_queue
    _get_queue_owner_id = get_queue_owner_id
    _update_live_request_usage = update_live_request_usage
    STREAM_CLOSE_TIMEOUT_S = close_timeout_s


# ── Cancel exception ─────────────────────────────────────────────────


class GuardianRequestCancelled(Exception):
    """Raised when Guardian cancels or abandons a tracked request lifecycle."""

    def __init__(self, request_id: str, reason: str = "cancelled"):
        super().__init__(f"request {request_id} cancelled: {reason}")
        self.request_id = request_id
        self.reason = reason


# ── Public functions ─────────────────────────────────────────────────


def queue_headers(request_id: str, queue_wait_ms: float) -> Dict[str, str]:
    return {
        "X-Request-Id": request_id,
        "X-Queue-Wait-Ms": str(int(queue_wait_ms)),
    }


def request_cancel_http_exception(request_id: str, reason: str) -> HTTPException:
    """Translate internal request cancellation into a client-facing HTTP error."""
    return HTTPException(
        status_code=499,
        detail={
            "error": "request_cancelled",
            "request_id": request_id,
            "message": reason,
        },
    )


async def stop_background_task(task: Optional[asyncio.Task]) -> None:
    """Cancel and await a background task without leaking cancellation noise."""
    if task is None:
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=STREAM_CLOSE_TIMEOUT_S)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out after %.1fs while stopping background task %s",
            STREAM_CLOSE_TIMEOUT_S,
            task.get_name(),
        )


async def watch_request_disconnect(request: Request, request_id: str, client_id: str) -> None:
    """Cancel the tracked queue request as soon as the downstream client disconnects."""
    while True:
        if await request.is_disconnected():
            snapshot = _inference_queue.cancel(
                request_id,
                client_id=client_id,
                reason="client_disconnected",
            )
            logger.info(
                "🔌 [%s] Client '%s' disconnected (%s)",
                request_id[:8],
                client_id,
                (snapshot or {}).get("status", "unknown"),
            )
            return
        await asyncio.sleep(0.25)


async def begin_queued_request(request: Request, client_id: str, model: str) -> tuple[str, asyncio.Task]:
    """Register a queue request immediately and wait until Guardian grants a slot."""
    normalized_client_id = client_id.strip() if isinstance(client_id, str) else ""
    if not normalized_client_id or normalized_client_id.lower() == "unauthenticated":
        logger.warning("🚫 Rejecting queue access without an authenticated client id")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Authenticated client required for queue access",
            headers={"WWW-Authenticate": "Bearer"},
        )

    queue_owner_id = _get_queue_owner_id(request, normalized_client_id)
    if not queue_owner_id:
        logger.warning("🚫 Rejecting queue access without an authenticated API key fingerprint")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Authenticated API key fingerprint required for queue access",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        request_id = _inference_queue.submit(
            normalized_client_id,
            model,
            owner_id=queue_owner_id,
        )
    except QueueAdmissionRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "queue_admission_rejected",
                "reason": exc.reason,
                "message": exc.message,
                "existing_request_id": exc.existing_request_id,
                "existing_status": exc.existing_status,
                "client_id": exc.client_id,
            },
        ) from exc

    _update_live_request_usage(request, queue_request_id=request_id, phase="queued")
    disconnect_task = asyncio.create_task(watch_request_disconnect(request, request_id, normalized_client_id))
    try:
        await _inference_queue.wait_for_turn(request_id)
    except QueueRequestCancelled as exc:
        _update_live_request_usage(request, queue_request_id=request_id, phase="cancelled")
        await stop_background_task(disconnect_task)
        raise GuardianRequestCancelled(request_id, exc.reason) from exc
    _update_live_request_usage(
        request,
        queue_request_id=request_id,
        phase="running",
        queue_wait_ms=_inference_queue.get_queue_wait_ms(request_id),
    )
    return request_id, disconnect_task


async def await_or_cancel_request(
    operation_task: asyncio.Task,
    request_id: str,
    cleanup: Optional[Callable[[], Awaitable[None]]] = None,
) -> Any:
    """Wait for backend work to finish, but abort promptly if the tracked request is cancelled."""
    cancel_event = _inference_queue.get_cancel_event(request_id)
    if cancel_event is None:
        return await operation_task

    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, pending = await asyncio.wait(
            {operation_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancel_event.is_set():
            if cleanup is not None:
                with suppress(Exception):
                    await asyncio.wait_for(cleanup(), timeout=STREAM_CLOSE_TIMEOUT_S)
            if not operation_task.done():
                operation_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(operation_task, timeout=STREAM_CLOSE_TIMEOUT_S)
            snapshot = _inference_queue.get_request_status(request_id)
            reason = (snapshot or {}).get("cancel_reason", "cancelled")
            raise GuardianRequestCancelled(request_id, reason)
        return await operation_task
    finally:
        cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task


async def close_stream_resources(response, client) -> None:
    """Close the upstream streaming response and client without surfacing cleanup noise."""
    for resource_name, closer in (
        ("response", response.aclose),
        ("client", client.aclose),
    ):
        try:
            await asyncio.wait_for(closer(), timeout=STREAM_CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs closing upstream stream %s during cancellation",
                STREAM_CLOSE_TIMEOUT_S,
                resource_name,
            )
        except Exception:
            pass


async def close_on_request_cancel(
    request_id: str,
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    """Wait for request cancellation and then run the provided cleanup coroutine."""
    cancel_event = _inference_queue.get_cancel_event(request_id)
    if cancel_event is None:
        return
    await cancel_event.wait()
    try:
        await asyncio.wait_for(cleanup(), timeout=STREAM_CLOSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out after %.1fs while closing upstream resources for cancelled request %s",
            STREAM_CLOSE_TIMEOUT_S,
            request_id[:8],
        )


def request_outcome(request_id: str) -> str:
    """Map the tracked request lifecycle to a final queue outcome."""
    return "cancelled" if _inference_queue.is_cancel_requested(request_id) else "completed"
