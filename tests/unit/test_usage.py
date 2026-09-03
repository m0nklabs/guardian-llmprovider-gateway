"""Tests for persistent dashboard API usage tracking."""

import time

from app.proxy.usage import ApiUsageTracker


class TestApiUsageTracker:
    """Regression coverage for dashboard usage state."""

    def test_records_requests_and_tokens_per_client(self, tmp_path):
        """Request and token counters are grouped by authenticated client."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.record_request(
            client_id="m0nk111",
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            model="GLM-4.7-Flash",
            duration_ms=125.0,
            request_bytes=512,
            response_bytes=2048,
            streamed=True,
        )
        tracker.record_tokens(
            client_id="m0nk111",
            endpoint="/v1/chat/completions",
            model="GLM-4.7-Flash",
            prompt_tokens=12,
            completion_tokens=34,
        )

        snapshot = tracker.snapshot()
        summary = snapshot["summary"]

        assert summary["total_requests"] == 1
        assert summary["total_tokens"] == 46
        assert summary["unique_clients"] == 1
        assert snapshot["top_clients"][0]["client_id"] == "m0nk111"
        assert snapshot["top_clients"][0]["streaming_requests"] == 1
        assert snapshot["top_clients"][0]["total_tokens"] == 46
        assert snapshot["summary"]["streaming_requests"] == 1
        assert snapshot["summary"]["total_request_bytes"] == 512
        assert snapshot["summary"]["total_response_bytes"] == 2048
        assert snapshot["summary"]["average_duration_ms"] == 125.0
        assert snapshot["top_clients"][0]["request_bytes"] == 512
        assert snapshot["top_clients"][0]["response_bytes"] == 2048
        assert snapshot["top_clients"][0]["avg_duration_ms"] == 125.0
        assert snapshot["recent_requests"][0]["model"] == "GLM-4.7-Flash"
        assert snapshot["recent_requests"][0]["request_bytes"] == 512
        assert snapshot["recent_requests"][0]["response_bytes"] == 2048

    def test_preserves_request_attribution_details(self, tmp_path):
        """Non-secret key and source metadata are retained for dashboard rows."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.record_request(
            client_id="openclaw",
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            model="GLM-4.7-Flash",
            request_bytes=128,
            response_bytes=4096,
            duration_ms=88.4,
            attribution={
                "project_prefix": "openclaw",
                "key_prefix": "openclaw",
                "key_fingerprint": "f1e2d3c4b5a6",
                "source_ip": "192.168.1.50",
                "host": "guardian.local",
                "user_agent": "OpenClaw/1.0",
                "metadata_client": "openclaw-ui",
                "metadata_note": "desktop operator",
            },
        )

        snapshot = tracker.snapshot()
        top_client = snapshot["top_clients"][0]
        recent = snapshot["recent_requests"][0]

        assert top_client["project_prefix"] == "openclaw"
        assert top_client["last_key_fingerprint"] == "f1e2d3c4b5a6"
        assert top_client["last_source_ip"] == "192.168.1.50"
        assert top_client["request_bytes"] == 128
        assert top_client["response_bytes"] == 4096
        assert top_client["avg_duration_ms"] == 88.4
        assert recent["metadata_client"] == "openclaw-ui"
        assert recent["user_agent"] == "OpenClaw/1.0"

    def test_preserves_preferred_non_loopback_source_for_client_bucket(self, tmp_path):
        """Client buckets keep the last meaningful LAN source even after localhost follow-up calls."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.record_request(
            client_id="hydroponics",
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            attribution={
                "project_prefix": "hydroponics",
                "source_ip": "192.168.1.201",
                "host": "192.168.1.35:11434",
                "metadata_client": "hydroponics",
            },
        )
        tracker.record_request(
            client_id="hydroponics",
            endpoint="/v1/queue/status",
            method="GET",
            status_code=200,
            attribution={
                "project_prefix": "hydroponics",
                "source_ip": "127.0.0.1",
                "host": "127.0.0.1:11434",
                "metadata_client": "hydroponics",
            },
        )

        snapshot = tracker.snapshot()
        top_client = snapshot["top_clients"][0]
        recent = snapshot["recent_requests"]

        assert top_client["last_source_ip"] == "127.0.0.1"
        assert top_client["preferred_source_ip"] == "192.168.1.201"
        assert top_client["preferred_host"] == "192.168.1.35:11434"
        assert recent[0]["source_ip"] == "127.0.0.1"
        assert recent[1]["source_ip"] == "192.168.1.201"

    def test_separates_shared_client_ids_by_key_fingerprint(self, tmp_path):
        """Shared display names stay isolated per authenticated key fingerprint."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.record_request(
            client_id="shared-client",
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            attribution={
                "project_prefix": "shared-client",
                "key_prefix": "shared-client-a",
                "key_fingerprint": "fingerprint-a",
                "source_ip": "10.0.0.11",
                "host": "guardian-a.local",
            },
        )
        tracker.record_tokens(
            client_id="shared-client",
            endpoint="/v1/chat/completions",
            model="GLM-4.7-Flash",
            prompt_tokens=10,
            completion_tokens=5,
            attribution={
                "key_fingerprint": "fingerprint-a",
                "source_ip": "10.0.0.11",
                "host": "guardian-a.local",
            },
        )

        tracker.record_request(
            client_id="shared-client",
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            attribution={
                "project_prefix": "shared-client",
                "key_prefix": "shared-client-b",
                "key_fingerprint": "fingerprint-b",
                "source_ip": "10.0.0.22",
                "host": "guardian-b.local",
            },
        )
        tracker.record_tokens(
            client_id="shared-client",
            endpoint="/v1/chat/completions",
            model="GLM-4.7-Flash",
            prompt_tokens=2,
            completion_tokens=3,
            attribution={
                "key_fingerprint": "fingerprint-b",
                "source_ip": "10.0.0.22",
                "host": "guardian-b.local",
            },
        )

        snapshot = tracker.snapshot(top_n=10)
        clients = {row["bucket_key"]: row for row in snapshot["top_clients"]}

        assert len(clients) == 2
        assert clients["fingerprint:fingerprint-a"]["client_id"] == "shared-client"
        assert clients["fingerprint:fingerprint-a"]["total_tokens"] == 15
        assert clients["fingerprint:fingerprint-a"]["last_source_ip"] == "10.0.0.11"
        assert clients["fingerprint:fingerprint-b"]["client_id"] == "shared-client"
        assert clients["fingerprint:fingerprint-b"]["total_tokens"] == 5
        assert clients["fingerprint:fingerprint-b"]["last_source_ip"] == "10.0.0.22"

    def test_restores_persisted_state_after_restart(self, tmp_path):
        """Counters survive creating a new tracker with the same state file."""
        state_file = tmp_path / "api_usage_state.json"

        tracker = ApiUsageTracker(state_file=state_file)
        tracker.record_request(
            client_id="openclaw",
            endpoint="/v1/models",
            method="GET",
            status_code=200,
            request_bytes=64,
            response_bytes=1024,
            duration_ms=12.5,
            attribution={"project_prefix": "openclaw", "source_ip": "127.0.0.1"},
        )
        tracker.record_tokens(
            client_id="openclaw",
            endpoint="/v1/chat/completions",
            model="GLM-4.7-Flash",
            prompt_tokens=9,
            completion_tokens=4,
        )
        # Persistence is debounced (structural rule: no disk writes on the
        # event loop per request) — force it, as a shutdown would.
        tracker.flush()

        restarted = ApiUsageTracker(state_file=state_file)
        snapshot = restarted.snapshot()

        assert snapshot["summary"]["total_requests"] == 1
        assert snapshot["summary"]["total_tokens"] == 13
        assert snapshot["summary"]["total_request_bytes"] == 64
        assert snapshot["summary"]["total_response_bytes"] == 1024
        assert snapshot["top_clients"][0]["client_id"] == "openclaw"
        assert snapshot["top_clients"][0]["project_prefix"] == "openclaw"
        assert snapshot["recent_requests"][0]["source_ip"] == "127.0.0.1"

    def test_persist_is_debounced_not_per_call(self, tmp_path):
        """Structural rule pin: persistence runs at most once per debounce
        window — _save_locked used to write the FULL state twice per request
        on the event loop (a streaming-gap source). First call persists
        (cold cache), follow-ups within the window must not rewrite."""
        state_file = tmp_path / "api_usage_state.json"
        tracker = ApiUsageTracker(state_file=state_file)

        def record(n: int) -> None:
            tracker.record_request(
                client_id="openclaw",
                endpoint="/v1/models",
                method="GET",
                status_code=200,
                request_bytes=64 + n,
                response_bytes=1024 + n,
                duration_ms=12.5,
                attribution={"project_prefix": "openclaw", "source_ip": "127.0.0.1"},
            )

        record(1)
        assert state_file.exists(), "cold-cache first persist expected"
        mtime1 = state_file.stat().st_mtime_ns

        record(2)
        record(3)
        assert state_file.stat().st_mtime_ns == mtime1, (
            "calls within the debounce window must not rewrite the state file"
        )

        time.sleep(0.01)  # guarantee a later filesystem mtime tick
        tracker.flush()
        assert state_file.stat().st_mtime_ns != mtime1, "flush() must force-persist"

    def test_backfills_preferred_source_from_recent_requests_on_restart(self, tmp_path):
        """Older persisted state without preferred fields is repaired from recent history."""
        state_file = tmp_path / "api_usage_state.json"
        state_file.write_text(
            """
{
    "schema_version": 3,
    "started_at": 1,
    "total_requests": 2,
    "total_errors": 0,
    "unauthenticated_requests": 0,
    "streaming_requests": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "total_request_bytes": 0,
    "total_response_bytes": 0,
    "total_duration_ms": 0,
    "requests_with_duration": 0,
    "endpoint_counts": {},
    "recent_requests": [
        {
            "timestamp": 10,
            "client_id": "hydroponics",
            "endpoint": "/v1/chat/completions",
            "method": "POST",
            "status_code": 200,
            "source_ip": "192.168.1.201",
            "host": "192.168.1.35:11434",
            "project_prefix": "hydroponics"
        },
        {
            "timestamp": 11,
            "client_id": "hydroponics",
            "endpoint": "/v1/queue/status",
            "method": "GET",
            "status_code": 200,
            "source_ip": "127.0.0.1",
            "host": "127.0.0.1:11434",
            "project_prefix": "hydroponics"
        }
    ],
    "clients": {
        "hydroponics": {
            "requests": 2,
            "errors": 0,
            "streaming_requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "request_bytes": 0,
            "response_bytes": 0,
            "duration_total_ms": 0,
            "requests_with_duration": 0,
            "last_seen": 11,
            "last_model": null,
            "last_endpoint": "/v1/queue/status",
            "last_key_prefix": "hydro",
            "last_key_fingerprint": "fingerprint",
            "last_auth_header": "authorization",
            "last_source_ip": "127.0.0.1",
            "last_forwarded_for": null,
            "last_host": "127.0.0.1:11434",
            "last_origin": null,
            "last_referer": null,
            "last_user_agent": "python-httpx/0.28.1",
            "project_prefix": "hydroponics",
            "metadata_client": "hydroponics",
            "metadata_note": "Mycodo/Pi4 hydroponics automation",
            "categories": {"inference": 2},
            "endpoints": {"/v1/chat/completions": 1, "/v1/queue/status": 1},
            "methods": {"POST": 1, "GET": 1}
        }
    }
}
            """.strip(),
            encoding="utf-8",
        )

        restarted = ApiUsageTracker(state_file=state_file)
        snapshot = restarted.snapshot()
        top_client = snapshot["top_clients"][0]

        assert top_client["last_source_ip"] == "127.0.0.1"
        assert top_client["preferred_source_ip"] == "192.168.1.201"
        assert top_client["preferred_host"] == "192.168.1.35:11434"

    def test_persists_unauthenticated_requests_across_restart(self, tmp_path):
        """401-style unauthenticated requests are part of persisted history."""
        state_file = tmp_path / "api_usage_state.json"

        tracker = ApiUsageTracker(state_file=state_file)
        tracker.record_request(
            client_id=None,
            endpoint="/api/status",
            method="GET",
            status_code=401,
        )

        restarted = ApiUsageTracker(state_file=state_file)
        snapshot = restarted.snapshot()

        assert snapshot["summary"]["total_requests"] == 1
        assert snapshot["summary"]["total_errors"] == 1
        assert snapshot["summary"]["unauthenticated_requests"] == 1

    def test_preserves_unauthenticated_request_attribution_details(self, tmp_path):
        """Unauthenticated history rows should still retain non-secret source metadata."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.record_request(
            client_id=None,
            endpoint="/v1/models",
            method="GET",
            status_code=401,
            attribution={
                "header_name": "authorization",
                "key_prefix": "legacy",
                "key_fingerprint": "deadbeefcafe",
                "source_ip": "10.0.0.8",
                "host": "guardian.local",
                "user_agent": "guardian-debug-check/1.0",
            },
        )

        snapshot = tracker.snapshot()
        recent = snapshot["recent_requests"][0]

        assert snapshot["summary"]["unauthenticated_requests"] == 1
        assert recent["client_id"] == "unauthenticated"
        assert recent["header_name"] == "authorization"
        assert recent["key_prefix"] == "legacy"
        assert recent["key_fingerprint"] == "deadbeefcafe"
        assert recent["source_ip"] == "10.0.0.8"
        assert recent["user_agent"] == "guardian-debug-check/1.0"

    def test_tracks_live_active_requests_until_finish(self, tmp_path):
        """In-flight requests expose live counters before being finalized."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.start_request(
            request_id="live-req-1",
            client_id="hydroponics",
            endpoint="/v1/chat/completions",
            method="POST",
            model="Qwen3.6-35B-A3B-HauhauCS-Aggressive",
            request_bytes=512,
            streamed=True,
            attribution={"source_ip": "127.0.0.1", "project_prefix": "hydro"},
        )
        tracker.update_active_request(
            request_id="live-req-1",
            phase="running",
            queue_request_id="queue-req-1",
            queue_wait_ms=250.0,
            prompt_tokens=48,
            completion_tokens=96,
            output_chars_delta=180,
            response_bytes_delta=1024,
        )

        live_snapshot = tracker.snapshot()
        live_entry = live_snapshot["active_requests"][0]

        assert live_snapshot["summary"]["active_requests_count"] == 1
        assert live_snapshot["summary"]["active_streaming_requests"] == 1
        assert live_entry["client_id"] == "hydroponics"
        assert live_entry["queue_request_id"] == "queue-req-1"
        assert live_entry["phase"] == "running"
        assert live_entry["total_tokens"] == 144
        assert live_entry["output_chars"] == 180
        assert live_entry["response_bytes"] == 1024

        tracker.finish_request(
            request_id="live-req-1",
            status_code=200,
            duration_ms=912.0,
        )

        finished_snapshot = tracker.snapshot()
        assert finished_snapshot["summary"]["active_requests_count"] == 0
        assert finished_snapshot["summary"]["total_requests"] == 1
        assert finished_snapshot["recent_requests"][0]["client_id"] == "hydroponics"
