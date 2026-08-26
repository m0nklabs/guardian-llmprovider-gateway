"""Capture integration helpers — thin adapters that bridge Guardian's request
lifecycle to the capture subsystem.

These helpers are intentionally fail-open: every function wraps capture
operations in try/except so that capture failures NEVER block or alter
inference behavior.  They are called from ``app/proxy/server.py`` at
strategic points in the request lifecycle.

Architecture (operator decision 2026-08-26): **Guardian stores RAW events**.
Redaction is NOT applied in the capture pipeline anymore — the raw
request/response content (system prompts, reasoning, tool results included)
goes into the WAL exactly as seen.  Redaction/processing is Keanu's job
(``scripts/keanu_redact.py``), consuming the replayable WAL.  The only
in-pipeline transformation is *media extraction*: binary image payloads are
written to separate files under ``data/capture/media/`` and replaced in the
event by a reference block (see ``app.capture.media``) — base64 bytes never
enter the WAL.

Capture applies to both local and cloud chat routes (OpenAI, Anthropic,
Ollama protocols); admin/health/metrics endpoints are excluded by policy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.capture.config import CaptureConfig, load_capture_config
from app.capture.schema import (
    BuildContext,
    build_request_received_event,
    build_request_completed_event,
    build_request_failed_event,
    build_request_cancelled_event,
    compute_client_ref,
)
from app.capture.policy import PolicyResult, evaluate_capture_policy
from app.capture.media import extract_media_from_messages
from app.capture.sink import CaptureSink, CaptureEvent
from app.capture.wal_writer import CaptureWALWriter

logger = logging.getLogger("Guardian.Capture.Integration")


class CaptureController:
    """Central facade for capture operations, wired into the Guardian request lifecycle.

    The controller is initialized once at module load (with a disabled config)
    and reconfigured when settings reload.  All methods are fail-open.
    """

    def __init__(self) -> None:
        self._config: CaptureConfig = load_capture_config()
        self._sink: CaptureSink = CaptureSink(
            max_pending_events=self._config.max_pending_events,
        )
        self._writer: Optional[CaptureWALWriter] = None
        self._writer_started: bool = False

    @property
    def config(self) -> CaptureConfig:
        return self._config

    @property
    def sink(self) -> CaptureSink:
        return self._sink

    @property
    def writer(self) -> Optional[CaptureWALWriter]:
        return self._writer

    async def reload_config(self) -> None:
        """Re-read capture config and rebuild sink/writer when needed.

        Live (no-restart) path used by the ``/api/config/reload`` admin
        endpoint:
        - config (incl. cloud_capture / cloud_model_prefixes / policies)
          is swapped immediately — policy evaluation reads
          :attr:`_config`, so new requests use the new rules at once;
        - the sink is rebuilt when ``max_pending_events`` changed;
        - the WAL writer is rebuilt (stop → re-create → start) only when the
          sink changed or the subsystem (de)activated, so a pure
          capture-policy/prefix change stays zero-touch for the writer.
        """
        new_config = load_capture_config()
        old_max = self._config.max_pending_events
        was_active = self._config.is_active
        self._config = new_config
        sink_changed = new_config.max_pending_events != old_max
        if sink_changed:
            self._sink = CaptureSink(max_pending_events=new_config.max_pending_events)
        now_active = new_config.is_active

        writer_needs_rebuild = sink_changed or (was_active != now_active)
        if writer_needs_rebuild:
            await self.stop_writer()
            self.initialize_writer()
            if now_active:
                await self.start_writer()
        logger.info(
            "Capture controller reloaded: enabled=%s, local=%s, cloud=%s "
            "(writer_rebuilt=%s)",
            new_config.enabled, new_config.local_capture, new_config.cloud_capture,
            writer_needs_rebuild,
        )

    def initialize_writer(self, sink: Optional[CaptureSink] = None) -> None:
        """Create the WAL writer (not started — call start_writer to begin)."""
        write_sink = sink or self._sink
        if self._config.is_active:
            self._writer = CaptureWALWriter(write_sink, self._config)
        else:
            logger.info("Capture is disabled — no WAL writer created")
            self._writer = None

    async def start_writer(self) -> None:
        """Start the background WAL writer task."""
        if self._writer is None:
            logger.info("Capture disabled — skipping WAL writer start")
            return
        if self._writer_started:
            return
        await self._writer.start()
        self._writer_started = True
        logger.info("Capture WAL writer started")

    async def stop_writer(self) -> None:
        """Stop the background WAL writer task."""
        if self._writer is None or not self._writer_started:
            return
        await self._writer.stop()
        self._writer_started = False
        logger.info("Capture WAL writer stopped")

    # ── Event dispatch (all fail-open) ─────────────────────────────────

    def _dispatch(self, event: Dict[str, Any]) -> None:
        """Enqueue an event to the sink — never raises."""
        try:
            if not self._config.enabled:
                return
            capture_event = CaptureEvent(data=event)
            if not self._sink.try_put(capture_event):
                # Already logged by the sink
                pass
        except Exception as exc:
            logger.warning("Capture dispatch error (fail-open): %s", exc)

    def _build_context(
        self,
        request_id: str,
        endpoint: str,
        ingress_protocol: str,
        route_type: str,
        requested_model: Optional[str],
        client_fingerprint: Optional[str],
        *,
        resolved_model: Optional[str] = None,
        grammar_present: bool = False,
        response_format_present: bool = False,
    ) -> BuildContext:
        """Build a BuildContext from request metadata.

        ``duration_ms`` is intentionally not set here: BuildContext carries
        lifecycle metadata only, and event builders take ``duration_ms`` as a
        direct argument (the completed/failed paths do).
        """
        return BuildContext(
            request_id=request_id,
            endpoint=endpoint,
            ingress_protocol=ingress_protocol,
            route_type=route_type,
            requested_model=requested_model,
            capture_policy_version=self._config.policy_version,
            instance_id=self._config.instance_id,
            client_fingerprint=client_fingerprint,
            resolved_model=resolved_model,
            grammar_present=grammar_present,
            response_format_present=response_format_present,
        )

    def maybe_capture_request_received(
        self,
        request_id: str,
        *,
        client_fingerprint: Optional[str],
        endpoint: str,
        ingress_protocol: str,
        route_type: str,
        requested_model: Optional[str],
        resolved_model: Optional[str] = None,
        request_messages: Optional[List[Dict[str, Any]]] = None,
        request_parameters: Optional[Dict[str, Any]] = None,
        queue_wait_ms: Optional[float] = None,
        grammar_present: bool = False,
        response_format_present: bool = False,
        sequence: int = 0,
    ) -> Optional[PolicyResult]:
        """Evaluate policy and, if approved, dispatch a request_received event.

        Returns the PolicyResult so the caller can short-circuit further
        capture work when the request is not captured.
        """
        client_ref = compute_client_ref(client_fingerprint) if client_fingerprint else None

        try:
            policy_result = evaluate_capture_policy(
                self._config,
                route_type=route_type,
                endpoint=endpoint,
                ingress_protocol=ingress_protocol,
                requested_model=requested_model,
                client_ref=client_ref,
            )
        except Exception as exc:
            logger.warning("Policy evaluation error (fail-open: not capturing): %s", exc)
            return PolicyResult(should_capture=False, reason="policy_error", detail=str(exc))

        if not policy_result.should_capture:
            return policy_result

        ctx = self._build_context(
            request_id, endpoint, ingress_protocol, route_type,
            requested_model, client_fingerprint,
            resolved_model=resolved_model,
            grammar_present=grammar_present,
            response_format_present=response_format_present,
        )

        try:
            # Raw capture: messages go into the WAL as-is, with only binary
            # image payloads extracted to media files (references stay in the
            # event).  Redaction is Keanu's job — NOT applied here.
            raw_messages = request_messages
            if isinstance(request_messages, list):
                raw_messages = extract_media_from_messages(
                    request_messages,
                    Path(self._config.capture_root),
                    request_id,
                )

            event = build_request_received_event(
                self._config, ctx,
                request_messages=raw_messages,
                request_parameters=request_parameters,
                queue_wait_ms=queue_wait_ms,
                sequence=sequence,
            )
            self._dispatch(event)
        except Exception as exc:
            logger.warning("request_received capture error (fail-open): %s", exc)

        return policy_result

    def capture_request_completed(
        self,
        ctx: BuildContext,
        *,
        policy_result: Optional[PolicyResult] = None,
        client_fingerprint: Optional[str] = None,
        response_content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        reasoning_content: Optional[str] = None,
        finish_reason: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        queue_wait_ms: Optional[float] = None,
        duration_ms: Optional[float] = None,
        http_status: Optional[int] = None,
        streamed: Optional[bool] = None,
        incomplete: Optional[bool] = None,
        attempts: Optional[int] = None,
        sequence: int = 1,
    ) -> None:
        """Dispatch a request_completed event (fail-open)."""
        if policy_result is not None and not policy_result.should_capture:
            return

        try:
            # Raw capture: response content is stored unredacted (Keanu
            # processes it downstream).  No in-pipeline redaction anymore.
            event = build_request_completed_event(
                self._config, ctx,
                response_content=response_content,
                tool_calls=tool_calls,
                tool_results=tool_results,
                reasoning_content=reasoning_content,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                http_status=http_status,
                streamed=streamed,
                incomplete=incomplete,
                attempts=attempts,
                sequence=sequence,
            )
            self._dispatch(event)
        except Exception as exc:
            logger.warning("request_completed capture error (fail-open): %s", exc)

    def capture_request_failed(
        self,
        ctx: BuildContext,
        *,
        policy_result: Optional[PolicyResult] = None,
        error_code: str,
        http_status: Optional[int] = None,
        sanitized_message: Optional[str] = None,
        queue_wait_ms: Optional[float] = None,
        duration_ms: Optional[float] = None,
        attempts: Optional[int] = None,
        sequence: int = 1,
    ) -> None:
        """Dispatch a request_failed event (fail-open)."""
        if policy_result is not None and not policy_result.should_capture:
            return

        try:
            # Raw capture: the sanitized error message is stored as-is
            # (it is already sanitized by the caller for client safety).
            event = build_request_failed_event(
                self._config, ctx,
                error_code=error_code,
                http_status=http_status,
                sanitized_message=sanitized_message,
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                attempts=attempts,
                sequence=sequence,
            )
            self._dispatch(event)
        except Exception as exc:
            logger.warning("request_failed capture error (fail-open): %s", exc)

    def capture_request_cancelled(
        self,
        ctx: BuildContext,
        *,
        policy_result: Optional[PolicyResult] = None,
        cancel_reason: str,
        queue_wait_ms: Optional[float] = None,
        duration_ms: Optional[float] = None,
        attempts: Optional[int] = None,
        sequence: int = 1,
    ) -> None:
        """Dispatch a request_cancelled event (fail-open)."""
        if policy_result is not None and not policy_result.should_capture:
            return

        try:
            event = build_request_cancelled_event(
                self._config, ctx,
                cancel_reason=cancel_reason,
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                attempts=attempts,
                sequence=sequence,
            )
            self._dispatch(event)
        except Exception as exc:
            logger.warning("request_cancelled capture error (fail-open): %s", exc)


# ── Module-level singleton ──────────────────────────────────────────────

capture_controller = CaptureController()


def get_capture_controller() -> CaptureController:
    """Return the singleton capture controller."""
    return capture_controller


def get_capture_sink_snapshot() -> Dict[str, Any]:
    """Return a metrics snapshot from the capture sink (for /metrics)."""
    try:
        controller = get_capture_controller()
        snapshot = controller.sink.snapshot()
        if controller.writer is not None:
            writer_snap = controller.writer.snapshot()
            snapshot.update({
                "writer": writer_snap.get("writer_metrics", {}),
                "disk_bytes": writer_snap.get("capture_disk_bytes", 0),
                "active_file": writer_snap.get("capture_active_file"),
            })
        return snapshot
    except Exception:
        return {"error": True}
