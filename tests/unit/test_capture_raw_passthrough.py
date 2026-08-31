"""Unit tests for RAW capture passthrough (operator architecture decision).

Guardian stores RAW events: request messages, response content, reasoning
and tool results go into the WAL exactly as seen — redaction is Keanu's job
(scripts/keanu_redact.py).  These tests pin that the capture pipeline does
NOT redact, and that media extraction (the only in-pipeline transform)
still happens.
"""

import asyncio
import base64
import json

import pytest

from app.capture.config import CaptureConfig
from app.capture.gzip_reader import read_all_text
from app.capture.integration import CaptureController
from app.capture.sink import CaptureSink
from app.capture.wal_writer import CaptureWALWriter

PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: Candidate ACTIVE WAL filenames, newest writer layout first.  The C3 writer
#: keeps the active file as plain ``guardian_capture_current.jsonl`` (gzip
#: only on rotation); the legacy writer stream-gzipped the active file under
#: the ``.jsonl.gz`` name.
ACTIVE_WAL_CANDIDATES = (
    "guardian_capture_current.jsonl",
    "guardian_capture_current.jsonl.gz",
)


def _read_active_wal(root) -> str:
    """Read the ACTIVE capture WAL regardless of writer version/layout.

    ``read_all_text`` (app.capture.gzip_reader) detects the format from the
    gzip magic bytes, so plain JSONL and gzip variants both work, including a
    name/format mismatch between writer versions.  Returns "" when the writer
    has not created any active file yet.
    """
    for name in ACTIVE_WAL_CANDIDATES:
        path = root / name
        if path.exists():
            return read_all_text(path)
    return ""


def _make_config(tmp_path, **overrides) -> CaptureConfig:
    base = dict(
        enabled=True,
        local_capture=True,
        cloud_capture=True,
        cloud_allowlist_enabled=False,
        per_client_opt_in=False,
        instance_id="raw-test",
        policy_version="1.0.0",
        capture_root=str(tmp_path),
        max_file_bytes=1 << 20,
        max_file_age_seconds=3600,
        retention_days=-1,
        max_capture_bytes=-1,
        max_pending_events=100,
        file_mode=0o640,
        directory_mode=0o750,
    )
    base.update(overrides)
    return CaptureConfig(**base)


@pytest.fixture
async def controller(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_CAPTURE_CLIENT_REF_SECRET", "test-client-ref-secret")
    monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", "test-record-auth-secret")
    cfg = _make_config(tmp_path)
    controller = CaptureController.__new__(CaptureController)
    controller._config = cfg
    controller._sink = CaptureSink(max_pending_events=cfg.max_pending_events)
    controller._writer = CaptureWALWriter(controller._sink, cfg)
    controller._writer_started = False
    controller._request_origin = {}
    await controller.start_writer()
    yield controller, cfg, tmp_path
    await controller.stop_writer()


class TestConfigInfiniteRetention:
    def test_retention_minus_one_accepted(self, tmp_path):
        cfg = _make_config(tmp_path, retention_days=-1)
        assert cfg.retention_days == -1

    def test_max_capture_bytes_minus_one_accepted(self, tmp_path):
        cfg = _make_config(tmp_path, max_capture_bytes=-1)
        assert cfg.max_capture_bytes == -1

    def test_less_than_minus_one_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _make_config(tmp_path, retention_days=-2)
        with pytest.raises(ValueError):
            _make_config(tmp_path, max_capture_bytes=-2)


class TestRawPassthrough:
    @pytest.mark.asyncio
    async def test_request_messages_stored_raw_with_secret(self, controller):
        ctrl, cfg, root = controller
        fake_secret = "sk-ant-notreally-secret-abcdef123456"
        messages = [
            {"role": "system", "content": f"The system prompt contains {fake_secret}."},
            {"role": "user", "content": "What is 2+2?"},
        ]
        ctrl.maybe_capture_request_received(
            request_id="req-raw-1",
            client_fingerprint="fp-test",
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            request_messages=messages,
        )
        await asyncio.sleep(0.3)
        text = _read_active_wal(root)
        # The raw secret MUST be present — Guardian does not redact (Keanu does).
        assert fake_secret in text
        event = json.loads(text.strip().splitlines()[0])
        assert event["event_type"] == "request_received"
        assert event["request_messages"][0]["content"] == (
            f"The system prompt contains {fake_secret}."
        )

    @pytest.mark.asyncio
    async def test_completed_event_stores_reasoning_and_tool_results_raw(self, controller):
        ctrl, cfg, root = controller
        ctrl.maybe_capture_request_received(
            request_id="req-raw-2",
            client_fingerprint="fp-test",
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            request_messages=[{"role": "user", "content": "hi"}],
        )
        await asyncio.sleep(0.1)
        ctx = ctrl._build_context(
            "req-raw-2", "/v1/chat/completions", "openai", "local",
            "llama3.2-3b", "fp-test",
        )
        ctrl.capture_request_completed(
            ctx,
            response_content="The answer is 42.",
            reasoning_content="Wait, let me think. The answer is 42.",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "add", "arguments": "{\"a\":1,\"b\":2}"}}],
            tool_results=[{"tool_call_id": "call_1", "content": "result: sk-something-secret-xyz"}],
            finish_reason="stop",
        )
        await asyncio.sleep(0.3)
        text = _read_active_wal(root)
        assert "The answer is 42." in text
        assert "Wait, let me think" in text
        assert "sk-something-secret-xyz" in text  # tool results NOT redacted
        assert "call_1" in text

    @pytest.mark.asyncio
    async def test_image_extracted_to_media_file_with_reference(self, controller):
        ctrl, cfg, root = controller
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
            ],
        }]
        ctrl.maybe_capture_request_received(
            request_id="req-raw-img",
            client_fingerprint="fp-test",
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            request_messages=messages,
        )
        await asyncio.sleep(0.3)
        text = _read_active_wal(root)
        # Base64 payload must NOT be in the WAL; a media reference must be.
        assert PNG_1PX not in text
        event = json.loads(text.strip().splitlines()[0])
        media = event["request_messages"][0]["content"][1]["image_media"]
        assert media["path"].startswith("media/req-raw-img_")
        target = root / media["path"]
        assert target.exists()
        assert target.read_bytes() == base64.b64decode(PNG_1PX)

    @pytest.mark.asyncio
    async def test_no_capture_without_client_identity(self, controller):
        """Unauthenticated requests (no client fingerprint) are never captured."""
        ctrl, cfg, root = controller
        ctrl.maybe_capture_request_received(
            request_id="req-anon",
            client_fingerprint=None,
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            request_messages=[{"role": "user", "content": "hi"}],
        )
        await asyncio.sleep(0.3)
        # The writer may not have created the file at all (no events to
        # write) — the helper returns "" in that case.
        text = _read_active_wal(root)
        assert "req-anon" not in text


class TestCallerOriginRegistry:
    """Caller correlation identity captured at request_received reaches the
    terminal event via the controller's per-request registry (C6)."""

    @pytest.mark.asyncio
    async def test_caller_request_id_flows_via_registry(self, controller):
        ctrl, cfg, root = controller
        ctrl.maybe_capture_request_received(
            request_id="req-orig-1",
            client_fingerprint="fp-test",
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            request_messages=[{"role": "user", "content": "hi"}],
            caller_request_id="client-req-abc",
            app_title="probe-app",
        )
        await asyncio.sleep(0.1)
        # Terminal event uses a BuildContext built at a DIFFERENT call site
        # (no caller identity) — the registry must attach it.
        ctx = ctrl._build_context(
            "req-orig-1", "/v1/chat/completions", "openai", "local",
            "llama3.2-3b", "fp-test",
        )
        assert ctx.caller_request_id is None
        ctrl.capture_request_completed(ctx, response_content="done")
        await asyncio.sleep(0.3)
        text = _read_active_wal(root)
        events = [json.loads(line) for line in text.strip().splitlines()]
        by_type = {e["event_type"]: e for e in events}
        assert by_type["request_received"]["caller_request_id"] == "client-req-abc"
        assert by_type["request_received"]["app_title"] == "probe-app"
        completed = by_type["request_completed"]
        assert completed["caller_request_id"] == "client-req-abc"
        assert completed["app_title"] == "probe-app"
        # Registry entry is consumed by the terminal event.
        assert "req-orig-1" not in ctrl._request_origin

    @pytest.mark.asyncio
    async def test_absent_headers_leave_identity_absent(self, controller):
        ctrl, cfg, root = controller
        ctrl.maybe_capture_request_received(
            request_id="req-orig-2",
            client_fingerprint="fp-test",
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            request_messages=[{"role": "user", "content": "hi"}],
        )
        await asyncio.sleep(0.1)
        ctx = ctrl._build_context(
            "req-orig-2", "/v1/chat/completions", "openai", "local",
            "llama3.2-3b", "fp-test",
        )
        ctrl.capture_request_completed(ctx, response_content="done")
        await asyncio.sleep(0.3)
        text = _read_active_wal(root)
        for line in text.strip().splitlines():
            event = json.loads(line)
            assert "caller_request_id" not in event
            assert "app_title" not in event
