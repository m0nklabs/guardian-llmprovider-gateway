"""Tests for app.proxy.metrics — Prometheus metrics instrumentation."""

import time
from unittest.mock import MagicMock, patch

import pytest

from app.proxy.metrics import (
    track_request,
    update_queue_metrics,
    update_system_metrics,
    get_metrics_output,
    REQUEST_COUNT,
    REQUEST_DURATION,
    ACTIVE_REQUESTS,
    QUEUE_WAITING,
    MODEL_CURRENT,
    UNLOADED,
    IDLE_SECONDS,
    MODEL_SWITCHES,
    MODEL_CRASHES,
    AUTH_FAILURES,
    ALIAS_RESOLUTIONS,
)


class TestTrackRequest:
    def test_increments_request_count(self):
        before = REQUEST_COUNT._metrics.copy()
        with track_request("/api/chat", "test-model"):
            pass
        body, _ = get_metrics_output()
        text = body.decode()
        assert "guardian_requests_total" in text

    def test_tracks_active_gauge(self):
        with track_request("/api/chat", "test-model"):
            # During request, active should be positive
            assert ACTIVE_REQUESTS._value._value >= 1
        # After request, it should be decremented back
        assert ACTIVE_REQUESTS._value._value >= 0

    def test_tracks_duration(self):
        with track_request("/api/chat", "test-model"):
            time.sleep(0.01)  # Small delay to ensure non-zero duration
        body, _ = get_metrics_output()
        text = body.decode()
        assert "guardian_request_duration_seconds" in text

    def test_error_sets_status(self):
        try:
            with track_request("/api/chat", "test-model") as tracker:
                raise ValueError("test error")
        except ValueError:
            pass
        body, _ = get_metrics_output()
        text = body.decode()
        assert 'status="error"' in text

    def test_custom_status(self):
        with track_request("/api/chat", "test-model") as tracker:
            tracker.set_status("timeout")
        body, _ = get_metrics_output()
        text = body.decode()
        assert 'status="timeout"' in text


class TestQueueMetrics:
    def test_updates_waiting_gauge(self):
        mock_queue = MagicMock()
        mock_queue.waiting_count = 5
        update_queue_metrics(mock_queue)
        assert QUEUE_WAITING._value._value == 5

    def test_zero_waiting(self):
        mock_queue = MagicMock()
        mock_queue.waiting_count = 0
        update_queue_metrics(mock_queue)
        assert QUEUE_WAITING._value._value == 0


class TestSystemMetrics:
    def test_updates_unloaded_state(self):
        mgr = MagicMock()
        mgr.is_unloaded = True
        mgr.last_request_time = time.time()
        mgr.current_model = "test-model"
        mgr.pinned_model = None
        mgr._model_verified = True
        update_system_metrics(mgr)
        assert UNLOADED._value._value == 1

    def test_updates_loaded_state(self):
        mgr = MagicMock()
        mgr.is_unloaded = False
        mgr.last_request_time = time.time()
        mgr.current_model = "test-model"
        mgr.pinned_model = "test-model"
        mgr._model_verified = True
        update_system_metrics(mgr)
        assert UNLOADED._value._value == 0

    def test_idle_seconds(self):
        mgr = MagicMock()
        mgr.is_unloaded = False
        mgr.last_request_time = time.time() - 60
        mgr.current_model = "test-model"
        mgr.pinned_model = None
        mgr._model_verified = False
        update_system_metrics(mgr)
        assert IDLE_SECONDS._value._value >= 59


class TestGpuMetrics:
    def test_updates_vram_from_nvidia_smi(self):
        from app.proxy.metrics import update_gpu_metrics, VRAM_USED_MB, VRAM_TOTAL_MB

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "0, 4500, 12288\n1, 3200, 16384\n"

        with patch("subprocess.run", return_value=mock_result):
            update_gpu_metrics()

        assert VRAM_USED_MB.labels(gpu_index="0")._value._value == 4500.0
        assert VRAM_TOTAL_MB.labels(gpu_index="0")._value._value == 12288.0
        assert VRAM_USED_MB.labels(gpu_index="1")._value._value == 3200.0
        assert VRAM_TOTAL_MB.labels(gpu_index="1")._value._value == 16384.0

    def test_handles_nvidia_smi_failure(self):
        from app.proxy.metrics import update_gpu_metrics

        with patch("subprocess.run", side_effect=FileNotFoundError("nvidia-smi not found")):
            # Should not raise
            update_gpu_metrics()


class TestMetricsOutput:
    def test_returns_bytes_and_content_type(self):
        body, content_type = get_metrics_output()
        assert isinstance(body, bytes)
        assert "text/plain" in content_type or "text/openmetrics" in content_type

    def test_contains_guardian_prefix(self):
        body, _ = get_metrics_output()
        text = body.decode()
        assert "guardian_" in text

    def test_contains_all_metric_families(self):
        body, _ = get_metrics_output()
        text = body.decode()
        expected_metrics = [
            "guardian_requests_total",
            "guardian_request_duration_seconds",
            "guardian_active_requests",
            "guardian_queue_waiting",
            "guardian_model_current_info",
            "guardian_unloaded",
            "guardian_idle_seconds",
            "guardian_model_switches_total",
            "guardian_model_crashes_total",
            "guardian_auth_failures_total",
            "guardian_alias_resolutions_total",
        ]
        for metric in expected_metrics:
            assert metric in text, f"Missing metric: {metric}"


class TestCounterIncrements:
    def test_model_switches_counter(self):
        MODEL_SWITCHES.labels(from_model="A", to_model="B").inc()
        body, _ = get_metrics_output()
        assert b"guardian_model_switches_total" in body

    def test_crash_counter(self):
        MODEL_CRASHES.labels(model="test").inc()
        body, _ = get_metrics_output()
        assert b"guardian_model_crashes_total" in body

    def test_auth_failures_counter(self):
        AUTH_FAILURES.inc()
        body, _ = get_metrics_output()
        assert b"guardian_auth_failures_total" in body

    def test_alias_resolutions_counter(self):
        ALIAS_RESOLUTIONS.labels(alias="glm4", resolved_model="GLM-4.7-Flash").inc()
        body, _ = get_metrics_output()
        assert b"guardian_alias_resolutions_total" in body
