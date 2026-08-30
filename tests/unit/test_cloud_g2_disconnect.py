"""G2 unit tests: downstream-disconnect poller + poll-interval config.

Covers the queue-free ``await_request_disconnect`` poller (used by the cloud
non-streamed disconnect race), the refactored ``watch_request_disconnect``
(which must still cancel the tracked queue request), and the
``proxy.disconnect_poll_seconds`` config accessor.
"""

import asyncio

import pytest

from app.config_loader import load_disconnect_poll_seconds
from app.gateway import queue_helpers


class _FakeDisconnectRequest:
    """Request stand-in reporting is_disconnected() after N polls."""

    def __init__(self, disconnect_after_polls: int) -> None:
        self.polls = 0
        self.disconnect_after_polls = disconnect_after_polls

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls >= self.disconnect_after_polls


class _StubQueue:
    """InferenceQueue stand-in recording cancel() calls."""

    def __init__(self) -> None:
        self.calls = []

    def cancel(self, request_id, client_id=None, reason=None):
        self.calls.append((request_id, client_id, reason))
        return {"status": "cancelled"}


@pytest.mark.asyncio
async def test_await_request_disconnect_returns_on_disconnect(monkeypatch):
    monkeypatch.setattr(queue_helpers, "DISCONNECT_POLL_INTERVAL_S", 0.01)
    request = _FakeDisconnectRequest(disconnect_after_polls=1)

    await asyncio.wait_for(
        queue_helpers.await_request_disconnect(request),
        timeout=1.0,
    )

    assert request.polls == 1


@pytest.mark.asyncio
async def test_await_request_disconnect_polls_until_disconnected(monkeypatch):
    monkeypatch.setattr(queue_helpers, "DISCONNECT_POLL_INTERVAL_S", 0.01)
    request = _FakeDisconnectRequest(disconnect_after_polls=4)

    await asyncio.wait_for(
        queue_helpers.await_request_disconnect(request),
        timeout=2.0,
    )

    assert request.polls == 4


@pytest.mark.asyncio
async def test_watch_request_disconnect_still_cancels_queue_request(monkeypatch):
    """Regression: the G2 refactor of watch_request_disconnect must preserve
    the local-path behavior — cancel the queue request on client disconnect."""
    monkeypatch.setattr(queue_helpers, "DISCONNECT_POLL_INTERVAL_S", 0.01)
    stub_queue = _StubQueue()
    monkeypatch.setattr(queue_helpers, "_inference_queue", stub_queue)
    request = _FakeDisconnectRequest(disconnect_after_polls=1)

    await asyncio.wait_for(
        queue_helpers.watch_request_disconnect(request, "req-12345678", "client-a"),
        timeout=1.0,
    )

    assert stub_queue.calls == [("req-12345678", "client-a", "client_disconnected")]


def test_init_wires_disconnect_poll_interval():
    """init() must override the module-level poll cadence from config.

    Saves and restores ALL injected globals — init() writes them directly
    (bypassing monkeypatch), and clobbering e.g. ``_inference_queue`` with
    None would break unrelated suites that run after this one.
    """
    saved = (
        queue_helpers._inference_queue,
        queue_helpers._get_queue_owner_id,
        queue_helpers._update_live_request_usage,
        queue_helpers.STREAM_CLOSE_TIMEOUT_S,
        queue_helpers.DISCONNECT_POLL_INTERVAL_S,
    )
    try:
        queue_helpers.init(None, None, None, 5.0, disconnect_poll_s=0.5)
        assert queue_helpers.DISCONNECT_POLL_INTERVAL_S == 0.5
    finally:
        queue_helpers.init(saved[0], saved[1], saved[2], saved[3], disconnect_poll_s=saved[4])
    assert queue_helpers.DISCONNECT_POLL_INTERVAL_S == saved[4]


def test_load_disconnect_poll_seconds_defaults():
    # Missing section → documented default.
    assert load_disconnect_poll_seconds({}) == 0.25
    # Invalid value → default (fail-safe, like the other accessors).
    assert load_disconnect_poll_seconds({"proxy": {"disconnect_poll_seconds": "abc"}}) == 0.25


def test_load_disconnect_poll_seconds_overrides():
    assert load_disconnect_poll_seconds({"proxy": {"disconnect_poll_seconds": 0.5}}) == 0.5
    # Floor: a mistyped near-zero value must not become a hot poll loop.
    assert load_disconnect_poll_seconds({"proxy": {"disconnect_poll_seconds": 0.001}}) == 0.05
    assert load_disconnect_poll_seconds({"proxy": {"disconnect_poll_seconds": 0}}) == 0.05


def test_provider_config_files_carry_max_call_seconds():
    """All six cloud provider files set the G2 cap explicitly (1200 s).

    Reads the REAL config files so a provider file drifting back to
    cap-less fails loudly instead of silently running unbounded. Per-provider
    files are top-level documents: the provider name IS the file stem (F2).
    """
    import yaml
    from pathlib import Path

    providers_dir = Path(__file__).resolve().parents[2] / "config" / "providers"
    cloud_files = [
        p for p in sorted(providers_dir.glob("*.settings.yaml"))
        if p.name != "ai-kvm2-local.settings.yaml"
    ]
    assert len(cloud_files) == 6
    for path in cloud_files:
        doc = yaml.safe_load(path.read_text())
        assert doc.get("max_call_seconds") == 1200, f"{path.name} missing max_call_seconds"


def test_global_config_carries_disconnect_poll_seconds():
    """global.settings.yaml ships the documented poll cadence explicitly."""
    import yaml
    from pathlib import Path

    global_path = Path(__file__).resolve().parents[2] / "config" / "global.settings.yaml"
    doc = yaml.safe_load(global_path.read_text())
    assert doc["proxy"]["disconnect_poll_seconds"] == 0.25
