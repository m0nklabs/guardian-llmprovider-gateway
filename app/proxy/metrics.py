"""Prometheus metrics for Guardian inference middleware.

Exposes counters, gauges, and histograms for monitoring via /metrics endpoint.
All metrics are prefixed with 'guardian_' for easy Grafana dashboard filtering.
"""

import logging
import time
from contextlib import contextmanager
from typing import Optional

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    REGISTRY,
)

logger = logging.getLogger("guardian-metrics")

# === Request Metrics ===

REQUEST_COUNT = Counter(
    "guardian_requests_total",
    "Total inference requests received",
    ["endpoint", "model", "status"],
)

REQUEST_DURATION = Histogram(
    "guardian_request_duration_seconds",
    "Inference request duration in seconds",
    ["endpoint", "model"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600],
)

ACTIVE_REQUESTS = Gauge(
    "guardian_active_requests",
    "Number of currently in-flight inference requests",
)

# === Queue Metrics ===

QUEUE_WAITING = Gauge(
    "guardian_queue_waiting",
    "Number of requests waiting in the inference queue",
)

QUEUE_TOTAL_QUEUED = Counter(
    "guardian_queue_total_queued",
    "Total number of requests that entered the queue",
)

QUEUE_TOTAL_COMPLETED = Counter(
    "guardian_queue_total_completed",
    "Total number of requests that completed through the queue",
)

QUEUE_TOTAL_TIMEOUTS = Counter(
    "guardian_queue_total_timeouts",
    "Total number of queue timeout events",
)

# === Model Metrics ===

MODEL_CURRENT = Info(
    "guardian_model_current",
    "Currently loaded model info",
)

MODEL_SWITCHES = Counter(
    "guardian_model_switches_total",
    "Total number of model switches performed",
    ["from_model", "to_model"],
)

MODEL_CRASHES = Counter(
    "guardian_model_crashes_total",
    "Total number of model/backend crash events",
    ["model"],
)

# === GPU / VRAM Metrics ===

VRAM_USED_MB = Gauge(
    "guardian_vram_used_mb",
    "GPU VRAM usage in MB",
    ["gpu_index"],
)

VRAM_TOTAL_MB = Gauge(
    "guardian_vram_total_mb",
    "Total GPU VRAM in MB",
    ["gpu_index"],
)

# === System Metrics ===

UNLOADED = Gauge(
    "guardian_unloaded",
    "Whether the backend is currently unloaded (1=unloaded, 0=loaded)",
)

IDLE_SECONDS = Gauge(
    "guardian_idle_seconds",
    "Seconds since last inference request",
)

ALIAS_RESOLUTIONS = Counter(
    "guardian_alias_resolutions_total",
    "Total alias/case-insensitive model name resolutions",
    ["alias", "resolved_model"],
)

AUTH_FAILURES = Counter(
    "guardian_auth_failures_total",
    "Total authentication failures",
)


@contextmanager
def track_request(endpoint: str, model: str):
    """Context manager to track request metrics (duration, count, active gauge).

    Usage:
        with track_request("/api/chat", "GLM-4.7-Flash") as tracker:
            # ... do inference ...
            tracker.set_status("success")
    """
    tracker = _RequestTracker(endpoint, model)
    ACTIVE_REQUESTS.inc()
    try:
        yield tracker
    except Exception:
        tracker.set_status("error")
        raise
    finally:
        ACTIVE_REQUESTS.dec()
        duration = time.monotonic() - tracker.start_time
        REQUEST_DURATION.labels(endpoint=endpoint, model=model).observe(duration)
        REQUEST_COUNT.labels(endpoint=endpoint, model=model, status=tracker.status).inc()


class _RequestTracker:
    """Internal tracker used by track_request context manager."""

    def __init__(self, endpoint: str, model: str):
        self.endpoint = endpoint
        self.model = model
        self.status = "success"
        self.start_time = time.monotonic()

    def set_status(self, status: str) -> None:
        self.status = status


def update_queue_metrics(queue) -> None:
    """Sync queue gauges from InferenceQueue state."""
    QUEUE_WAITING.set(queue.waiting_count)


def update_gpu_metrics() -> None:
    """Read nvidia-smi and update VRAM gauges."""
    import subprocess

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 3:
                    idx, used, total = parts
                    VRAM_USED_MB.labels(gpu_index=idx).set(float(used))
                    VRAM_TOTAL_MB.labels(gpu_index=idx).set(float(total))
    except Exception as e:
        logger.debug(f"Could not read GPU metrics: {e}")


def update_system_metrics(model_manager) -> None:
    """Update system-level gauges from model manager state."""
    UNLOADED.set(1 if model_manager.is_unloaded else 0)
    IDLE_SECONDS.set(time.time() - model_manager.last_request_time)
    MODEL_CURRENT.info({
        "name": model_manager.current_model or "none",
        "pinned": model_manager.pinned_model or "none",
        "verified": str(model_manager._model_verified),
    })


def get_metrics_output() -> tuple[bytes, str]:
    """Generate Prometheus metrics output.

    Returns (body_bytes, content_type) ready for HTTP response.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
