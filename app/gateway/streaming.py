"""SSE streaming helpers — watchdog, keepalives, Anthropic enrichment.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).

These helpers handle the mechanics of reading SSE lines from an upstream
provider (llama.cpp or cloud), detecting stall/infinite-loop conditions,
emitting heartbeat comments, and enriching Anthropic-format SSE lines
with token usage metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("Guardian")

# ── Constants ───────────────────────────────────────────────────────

STREAM_TIMEOUT_EXTENSION_STEPS: List[Tuple[int, float]] = [
    (5, 1.5),
    (10, 2.0),
    (20, 3.0),
]

STREAM_LOOP_REPEAT_THRESHOLD = 12

# ── Injected ─────────────────────────────────────────────────────────
_inference_queue = None
_GuardianRequestCancelled = None
STREAM_HEARTBEAT_INTERVAL_S: float = 15.0
STREAM_CLOSE_TIMEOUT_S: float = 5.0


def init(inference_queue, guardian_request_cancelled_exc, heartbeat_interval_s: float, close_timeout_s: float) -> None:
    """Inject the inference queue, cancel exception, and streaming constants."""
    global _inference_queue, _GuardianRequestCancelled
    global STREAM_HEARTBEAT_INTERVAL_S, STREAM_CLOSE_TIMEOUT_S
    _inference_queue = inference_queue
    _GuardianRequestCancelled = guardian_request_cancelled_exc
    STREAM_HEARTBEAT_INTERVAL_S = heartbeat_interval_s
    STREAM_CLOSE_TIMEOUT_S = close_timeout_s


# ── Text extraction ─────────────────────────────────────────────────


def extract_assistant_message_text(message: Dict[str, object]) -> str:
    """Extract text from an OpenAI-format assistant message."""
    content = str(message.get("content") or "")
    if content:
        return content
    return str(message.get("reasoning_content") or "")


def extract_assistant_delta_text(delta: Dict[str, object]) -> str:
    """Extract incremental text from an SSE delta object."""
    content = str(delta.get("content") or "")
    if content:
        return content
    return str(delta.get("reasoning_content") or "")


def normalize_stream_progress_text(text: object) -> str:
    """Normalise whitespace for progress comparison."""
    import re
    return re.sub(r"\s+", " ", str(text or "")).strip()


def extract_stream_progress_text(line: str) -> str:
    """Join all content deltas in a single SSE data line for loop detection."""
    try:
        payload = json.loads(line.removeprefix("data: ").strip())
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        if isinstance(delta, dict):
            return normalize_stream_progress_text(extract_assistant_delta_text(delta))
    return ""


# ── Watchdog ────────────────────────────────────────────────────────


@dataclass
class StreamProgressWatchdog:
    """Bound streaming stall time while rewarding healthy non-looping output."""

    base_timeout_s: float
    current_timeout_s: float = field(init=False)
    healthy_chunk_count: int = 0
    repeated_chunk_count: int = 0
    last_chunk: str = ""
    loop_detected: bool = False

    def __post_init__(self) -> None:
        self.base_timeout_s = max(float(self.base_timeout_s), 1.0)
        self.current_timeout_s = self.base_timeout_s

    def observe_sse_line(self, line: str) -> None:
        """Grow the stall timeout only when the stream keeps making novel progress."""
        normalized = normalize_stream_progress_text(extract_stream_progress_text(line))
        if not normalized:
            return

        if normalized == self.last_chunk:
            self.repeated_chunk_count += 1
            if self.repeated_chunk_count >= STREAM_LOOP_REPEAT_THRESHOLD:
                self.loop_detected = True
            return

        self.last_chunk = normalized
        self.repeated_chunk_count = 1
        self.loop_detected = False
        self.healthy_chunk_count += 1

        multiplier = 1.0
        for minimum_chunks, candidate_multiplier in STREAM_TIMEOUT_EXTENSION_STEPS:
            if self.healthy_chunk_count >= minimum_chunks:
                multiplier = candidate_multiplier
                break

        self.current_timeout_s = self.base_timeout_s * multiplier


# ── Timeout / keepalive ─────────────────────────────────────────────


def build_stream_timeout(base_timeout_s: float) -> httpx.Timeout:
    """Allow streaming reads to run under Guardian's own watchdog instead of a fixed read timeout."""
    base_timeout_s = max(float(base_timeout_s), 1.0)
    return httpx.Timeout(connect=10.0, read=None, write=base_timeout_s, pool=base_timeout_s)


def build_sse_keepalive_comment(request_id: Optional[str] = None) -> str:
    """Emit a lightweight SSE comment to keep downstream clients from idling out."""
    suffix = f" request_id={request_id}" if request_id else ""
    return f": guardian-keepalive{suffix}"


# ── Anthropic SSE enrichment ────────────────────────────────────────


def enrich_anthropic_sse_line(line: str, *, input_tokens: int = 0, cache_read_tokens: int = 0) -> tuple[str, int, int]:
    """Enrich an Anthropic SSE line from llama-server with missing usage fields.

    llama-server's ``/v1/messages`` endpoint is missing:
    - ``input_tokens`` and ``cache_creation_input_tokens`` in ``message_delta`` usage
    - ``cache_creation_input_tokens`` in ``message_start`` usage

    Returns ``(enriched_line, new_input_tokens, new_cache_read_tokens)``.
    """
    if not line.startswith("data: "):
        return line, input_tokens, cache_read_tokens

    data_str = line[6:].strip()
    if not data_str:
        return line, input_tokens, cache_read_tokens

    try:
        data = json.loads(data_str)
    except (json.JSONDecodeError, TypeError):
        return line, input_tokens, cache_read_tokens

    changed = False

    # Enrich message_start usage
    if data.get("type") == "message_start":
        msg = data.get("message", {})
        usage = msg.get("usage", {})
        if isinstance(usage, dict):
            if "input_tokens" in usage:
                input_tokens = usage["input_tokens"]
            if "cache_read_input_tokens" in usage:
                cache_read_tokens = usage["cache_read_input_tokens"]
            if "cache_creation_input_tokens" not in usage:
                usage["cache_creation_input_tokens"] = 0
                changed = True
            msg["usage"] = usage

    # Enrich message_delta usage (cumulative — must include input_tokens)
    if data.get("type") == "message_delta":
        delta = data.get("delta", {})
        usage = data.get("usage", {})
        if isinstance(usage, dict):
            if "input_tokens" not in usage:
                usage["input_tokens"] = input_tokens
                changed = True
            if "cache_creation_input_tokens" not in usage:
                usage["cache_creation_input_tokens"] = 0
                changed = True
            if "cache_read_input_tokens" not in usage:
                usage["cache_read_input_tokens"] = cache_read_tokens
                changed = True
            data["usage"] = usage

        # Fix stop_reason: llama-server returns "end_turn" even when a
        # stop_sequence was matched. Anthropic expects "stop_sequence".
        if isinstance(delta, dict):
            if delta.get("stop_reason") == "end_turn" and delta.get("stop_sequence"):
                delta["stop_reason"] = "stop_sequence"
                changed = True

    if changed:
        return f"data: {json.dumps(data)}\n", input_tokens, cache_read_tokens

    return line, input_tokens, cache_read_tokens


def enrich_anthropic_response(payload: dict) -> dict:
    """Enrich a non-streaming Anthropic response from llama-server with missing fields."""
    usage = payload.get("usage", {})
    if isinstance(usage, dict):
        if "cache_creation_input_tokens" not in usage:
            usage["cache_creation_input_tokens"] = 0
        if "cache_read_input_tokens" not in usage:
            usage["cache_read_input_tokens"] = 0
        if "input_tokens" not in usage:
            usage["input_tokens"] = 0
        if "output_tokens" not in usage:
            usage["output_tokens"] = 0
        payload["usage"] = usage
    else:
        payload["usage"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    # Fix stop_reason: llama-server returns "end_turn" even when a stop_sequence
    # was matched. Anthropic expects "stop_sequence" in that case.
    if payload.get("stop_reason") == "end_turn" and payload.get("stop_sequence"):
        payload["stop_reason"] = "stop_sequence"

    return payload


# ── SSE pump ────────────────────────────────────────────────────────


async def _pump_sse_lines(
    iterator: AsyncIterator[str],
    queue: "asyncio.Queue[tuple[str, Optional[Any]]]",
) -> None:
    """Read upstream SSE lines without cancelling the underlying iterator during keepalive gaps."""
    try:
        async for line in iterator:
            await queue.put(("line", line))
    except Exception as exc:
        await queue.put(("error", exc))
    else:
        await queue.put(("eof", None))


# ── SSE line iterator with watchdog ─────────────────────────────────


async def iter_sse_lines_with_watchdog(
    response: httpx.Response,
    watchdog: StreamProgressWatchdog,
    *,
    request_id: Optional[str] = None,
    route: Optional[str] = None,
    client_id: Optional[str] = None,
    model_name: Optional[str] = None,
    heartbeat_interval_s: Optional[float] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[str]:
    """Yield SSE lines while enforcing a dynamic stall timeout and optional downstream keepalives."""
    queue: "asyncio.Queue[tuple[str, Optional[Any]]]" = asyncio.Queue()
    pump_task = asyncio.create_task(_pump_sse_lines(response.aiter_lines(), queue))
    last_data_at = time.monotonic()

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
                if request_id:
                    snapshot = _inference_queue.get_request_status(request_id)
                    reason = (snapshot or {}).get("cancel_reason") or reason
                raise _GuardianRequestCancelled(request_id or "unknown", reason)

            timeout_exc: Optional[asyncio.TimeoutError] = None
            elapsed_without_data_s = time.monotonic() - last_data_at
            remaining_timeout_s = watchdog.current_timeout_s - elapsed_without_data_s
            if remaining_timeout_s <= 0:
                timeout_exc = asyncio.TimeoutError()
                elapsed_without_data_s = max(elapsed_without_data_s, watchdog.current_timeout_s)
            else:
                wait_timeout_s = remaining_timeout_s
                if heartbeat_interval_s is not None:
                    wait_timeout_s = min(wait_timeout_s, heartbeat_interval_s)
                try:
                    if cancel_event is None:
                        event_type, payload = await asyncio.wait_for(queue.get(), timeout=wait_timeout_s)
                    else:
                        queue_task = asyncio.create_task(queue.get())
                        cancel_task = asyncio.create_task(cancel_event.wait())
                        try:
                            done, pending = await asyncio.wait(
                                {queue_task, cancel_task},
                                timeout=wait_timeout_s,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for pending_task in pending:
                                pending_task.cancel()
                            for pending_task in pending:
                                with suppress(asyncio.CancelledError):
                                    await pending_task
                            if not done:
                                raise asyncio.TimeoutError()
                            if cancel_task in done and cancel_event.is_set():
                                reason = "cancelled"
                                if request_id:
                                    snapshot = _inference_queue.get_request_status(request_id)
                                    reason = (snapshot or {}).get("cancel_reason") or reason
                                raise _GuardianRequestCancelled(request_id or "unknown", reason)
                            event_type, payload = queue_task.result()
                        finally:
                            if not queue_task.done():
                                queue_task.cancel()
                            if not cancel_task.done():
                                cancel_task.cancel()
                except asyncio.TimeoutError as exc:
                    timeout_exc = exc
                    elapsed_without_data_s = time.monotonic() - last_data_at
                    remaining_timeout_s = watchdog.current_timeout_s - elapsed_without_data_s
                    if heartbeat_interval_s is not None and remaining_timeout_s > 0:
                        yield build_sse_keepalive_comment(request_id)
                        yield ""
                        continue
                else:
                    if event_type == "eof":
                        return
                    if event_type == "error":
                        error = payload
                        if isinstance(error, Exception):
                            raise error
                        raise RuntimeError(f"Unexpected SSE pump error payload: {error!r}")

                    line = str(payload or "")
                    last_data_at = time.monotonic()
                    watchdog.observe_sse_line(line)
                    yield line
                    continue

            context_parts = []
            if request_id:
                context_parts.append(f"request_id={request_id}")
            if route:
                context_parts.append(f"route={route}")
            if client_id:
                context_parts.append(f"client={client_id}")
            if model_name:
                context_parts.append(f"model={model_name}")
            context_suffix = f" [{' '.join(context_parts)}]" if context_parts else ""
            message = (
                f"Guardian stream stalled after {watchdog.current_timeout_s:.0f}s without new SSE data "
                f"(healthy_chunks={watchdog.healthy_chunk_count}, loop_detected={watchdog.loop_detected}, "
                f"silence_s={elapsed_without_data_s:.1f})"
                f"{context_suffix}"
            )
            logger.warning(message)
            if timeout_exc is None:
                raise httpx.ReadTimeout(message, request=response.request)
            raise httpx.ReadTimeout(message, request=response.request) from timeout_exc
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task
