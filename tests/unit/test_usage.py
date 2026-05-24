"""Tests for persistent dashboard API usage tracking."""

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
        assert snapshot["recent_requests"][0]["model"] == "GLM-4.7-Flash"

    def test_preserves_request_attribution_details(self, tmp_path):
        """Non-secret key and source metadata are retained for dashboard rows."""
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")

        tracker.record_request(
            client_id="openclaw",
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            model="GLM-4.7-Flash",
            attribution={
                "project_prefix": "openclaw",
                "key_prefix": "openclaw",
                "key_fingerprint": "f1e2d3c4b5a6",
                "source_ip": "192.168.1.F",
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
        assert top_client["last_source_ip"] == "192.168.1.F"
        assert recent["metadata_client"] == "openclaw-ui"
        assert recent["user_agent"] == "OpenClaw/1.0"

    def test_restores_persisted_state_after_restart(self, tmp_path):
        """Counters survive creating a new tracker with the same state file."""
        state_file = tmp_path / "api_usage_state.json"

        tracker = ApiUsageTracker(state_file=state_file)
        tracker.record_request(
            client_id="openclaw",
            endpoint="/v1/models",
            method="GET",
            status_code=200,
            attribution={"project_prefix": "openclaw", "source_ip": "127.0.0.1"},
        )
        tracker.record_tokens(
            client_id="openclaw",
            endpoint="/v1/chat/completions",
            model="GLM-4.7-Flash",
            prompt_tokens=9,
            completion_tokens=4,
        )

        restarted = ApiUsageTracker(state_file=state_file)
        snapshot = restarted.snapshot()

        assert snapshot["summary"]["total_requests"] == 1
        assert snapshot["summary"]["total_tokens"] == 13
        assert snapshot["top_clients"][0]["client_id"] == "openclaw"
        assert snapshot["top_clients"][0]["project_prefix"] == "openclaw"
        assert snapshot["recent_requests"][0]["source_ip"] == "127.0.0.1"

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
