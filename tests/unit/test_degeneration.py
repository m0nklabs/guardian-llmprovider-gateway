"""Degeneration guard pins — repetition-loop cutoff (operator feature).

The detector cuts streaming/non-stream responses that fall into a
repetition loop server-side (standard finish_reason "length"), so clients
can use roomier max_tokens budgets. These pins cover the detector math,
the false-positive guards (short echoes, lists, code), the kill-switch,
the schema field, and the cloud streaming cutoff wiring.
"""

import json

import httpx
import pytest
from unittest.mock import patch

from app.gateway.degeneration import (
    DegenerationConfig,
    DegenerationDetector,
    load_degeneration_config,
)


@pytest.fixture
def cfg():
    return DegenerationConfig()


class TestDetectorRepetition:
    def test_long_loop_detected(self, cfg):
        """period 120 x 3 (360 bytes) -> verdict."""
        d = DegenerationDetector(cfg)
        text = "Een normale zin. " + ("X" * 118 + "|") * 3
        v = d.run_full(text)
        assert v is not None
        assert v.period == 119  # fundamental period of ("X"*118 + "|")
        assert v.repeats >= 3

    def test_word_level_loop_detected(self, cfg):
        """Fundamental period 6 x 40 -> verdict (classic token-loop)."""
        d = DegenerationDetector(cfg)
        v = d.run_full("Intro. " + "token " * 40)
        assert v is not None
        assert v.period == 6
        assert v.repeat_bytes >= 240

    def test_letter_level_loop_exempt(self, cfg):
        """Deliberate trade-off: letter-level loops (fundamental q=1) are
        exempt — indistinguishable from legitimate separators/fills. Word- and
        phrase-level loops (the real LLM degeneration mode) are caught."""
        d = DegenerationDetector(cfg)
        assert d.run_full("Intro. " + "a" * 6 * 40) is None

    def test_cut_from_end_keeps_one_instance(self, cfg):
        d = DegenerationDetector(cfg)
        loop = "L" * 118 + "|"
        text = "start " + loop * 4
        v = d.run_full(text)
        assert v is not None
        trimmed = text[: len(text) - v.cut_from_end]
        # exactly one instance of the loop remains after the trim
        assert trimmed.endswith(loop)
        assert trimmed.count(loop) == 1


class TestDetectorFalsePositives:
    def test_short_echo_not_flagged(self, cfg):
        """'ha ' repetition (period 3 < min_period 6) must never trigger."""
        d = DegenerationDetector(cfg)
        assert d.run_full("ha " * 200) is None

    def test_ok_echo_not_flagged(self, cfg):
        d = DegenerationDetector(cfg)
        assert d.run_full("OK " * 100) is None

    def test_identical_list_lines_not_flagged(self, cfg):
        """period 7 x 20 = 140 bytes < min_repeat_bytes 240 -> no verdict."""
        d = DegenerationDetector(cfg)
        assert d.run_full("Header\n" + "- item\n" * 20) is None

    def test_small_code_loop_not_flagged(self, cfg):
        d = DegenerationDetector(cfg)
        body = "for(i=0;i<10;i++){\n"
        assert d.run_full(body * 5) is None  # 5 * 19 = 95 bytes < 240

    def test_short_text_not_flagged(self, cfg):
        d = DegenerationDetector(cfg)
        assert d.run_full("aaaaaa" * 10) is None  # 60 bytes < 240

    def test_normal_text_not_flagged(self, cfg):
        """Gevarieerde zinnen — een herhaalde IDENTIEKE zin is per definitie
        zelf degeneratie (en correct gevangen)."""
        d = DegenerationDetector(cfg)
        sentences = [
            "De gateway routeert verzoeken naar lokale of cloud modellen.",
            "Failover-groepen filteren kandidaten op gezondheid en capabiliteit.",
            "De inference-queue bewaakt lifecycle en disconnect per verzoek.",
            "Capture schrijft privacy-bewuste events naar de WAL-schrijver.",
            "TLS wordt via nginx stream-preread gemultiplexerd op beide poorten.",
            "De scheduler ontlaadt modellen na vijf minuten inactiviteit.",
            "Context-metadata komt eerst uit overrides en daarna uit de catalog.",
            "De structural guard verhindert sync-blokkades op de event loop.",
        ]
        text = " ".join(sentences) + " En nog een afsluitende zin zonder patroon."
        assert d.run_full(text) is None


class TestMarker:
    def test_marker_delta_default_on(self, cfg):
        d = DegenerationDetector(cfg)
        assert d.marker_delta() == "\n\n[guardian: generation cut off - repetition loop detected]"

    def test_marker_delta_off(self):
        d = DegenerationDetector(DegenerationConfig(marker_enabled=False))
        assert d.marker_delta() is None

    def test_marker_delta_custom_text(self):
        d = DegenerationDetector(DegenerationConfig(marker_text="[truncated]"))
        assert d.marker_delta() == "\n\n[truncated]"

    def test_marker_delta_off_when_disabled(self):
        d = DegenerationDetector(DegenerationConfig(enabled=False))
        assert d.marker_delta() is None


class TestKillSwitch:
    def test_disabled_config_never_verdicts(self):
        d = DegenerationDetector(DegenerationConfig(enabled=False))
        assert d.feed("X" * 118 + "|" * 300) is None
        assert d.run_full("a" * 6 * 100) is None


class TestStreamingFeed:
    def test_feed_triggers_midstream(self, cfg):
        """Deltas fed incrementally must trigger as soon as the window fills."""
        d = DegenerationDetector(cfg)
        unit = "X" * 118 + "|"
        verdicts = [d.feed(unit) for _ in range(10)]
        assert all(v is None for v in verdicts[:2])
        assert any(v is not None for v in verdicts)


class TestConfigLoader:
    def test_defaults_when_section_missing(self, tmp_path):
        import yaml

        p = tmp_path / "settings.yaml"
        p.write_text(yaml.safe_dump({"proxy": {"port": 11434}}))
        cfg = load_degeneration_config(path=p)
        assert cfg.enabled is True
        assert cfg.min_repeat_bytes == 240

    def test_yaml_overrides(self, tmp_path):
        import yaml

        p = tmp_path / "settings.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "degeneration": {
                        "enabled": False,
                        "min_repeat_bytes": 500,
                    }
                }
            )
        )
        cfg = load_degeneration_config(path=p)
        assert cfg.enabled is False
        assert cfg.min_repeat_bytes == 500


class TestSchemaField:
    @pytest.fixture
    def base_ctx(self):
        from app.capture.config import PROTOCOL_OPENAI, ROUTE_LOCAL
        from app.capture.schema import BuildContext

        return BuildContext(
            request_id="req-test-1234567890",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
            resolved_model="llama3.2-3b",
            capture_policy_version="1.0.0",
            instance_id="01e6ae75-1357-4656-aa61-a6cfe3d51fac",
            client_fingerprint="fp-test",
            streamed=False,
        )

    @pytest.fixture
    def capture_config(self):
        from app.capture.schema import CaptureConfig

        return CaptureConfig(
            enabled=True,
            local_capture=True,
            cloud_capture=False,
            instance_id="01e6ae75-1357-4656-aa61-a6cfe3d51fac",
            policy_version="1.0.0",
        )

    def test_completed_event_carries_cutoff_flag(self, base_ctx, capture_config):
        from app.capture.schema import SCHEMA_VERSION, build_request_completed_event

        event = build_request_completed_event(
            capture_config, base_ctx,
            http_status=200,
            degeneration_cutoff=True,
            sequence=1,
        )
        assert event["degeneration_cutoff"] is True
        assert event["schema_version"] == "1.2.0"

    def test_completed_event_omits_flag_when_false(self, base_ctx, capture_config):
        from app.capture.schema import build_request_completed_event

        event = build_request_completed_event(
            capture_config, base_ctx, http_status=200, sequence=1
        )
        assert "degeneration_cutoff" not in event


class TestCloudStreamCutoff:
    @pytest.mark.asyncio
    async def test_degenerate_stream_cut_with_length_finish(self, monkeypatch):
        """Full wiring pin: a looping upstream SSE stream gets cut and closed
        with a synthesized finish_reason "length" chunk + [DONE], and the
        capture event carries degeneration_cutoff=True."""
        import asyncio
        import types

        from app.cloud_inference import forwarding

        loop_unit = "X" * 118 + "|"

        async def iter_sse_lines(*args, **kwargs):
            for _ in range(12):
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"content": loop_unit}}]}
                )

        from tests.unit.test_cloud_forwarding import (
            _FakeHealthTracker,
            _FakeRateLimiter,
            _FakeStreamClient,
        )

        provider = types.SimpleNamespace(
            name="openrouter",
            base_url="https://provider.example/v1",
            api_key="test-key",
            timeout_seconds=30,
            extra_headers={},
        )
        response = __import__("httpx").Response(
            200, headers={"content-type": "text/event-stream"}
        )
        stream_client = _FakeStreamClient(response)

        async def iter_sse_lines_dying(*args, **kwargs):
            for _ in range(12):
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"content": loop_unit}}]}
                )

        captured_kwargs = {}

        def fake_completed(ctx, **kwargs):
            captured_kwargs.update(kwargs)

        monkeypatch.setattr(
            forwarding, "_resolve_cloud_attempts",
            lambda *a, **k: ([(provider, "provider/model")], None),
        )
        monkeypatch.setattr(
            forwarding, "_prepare_cloud_candidate_request",
            lambda provider, upstream_model, path, body, fingerprint: (path, body, b"{}", False),
        )
        monkeypatch.setattr(forwarding, "_messages_contain_image_input", lambda m: False)
        monkeypatch.setattr(forwarding, "_get_cloud_key_fingerprint", lambda request, cid: "fp")
        monkeypatch.setattr(forwarding, "_set_request_usage_metadata", lambda *a, **k: None)
        monkeypatch.setattr(forwarding, "_start_live_request_usage", lambda *a, **k: None)
        monkeypatch.setattr(forwarding, "_update_live_request_usage", lambda *a, **k: None)
        monkeypatch.setattr(forwarding, "_finish_live_request_usage", lambda *a, **k: None)
        monkeypatch.setattr(forwarding, "_record_request_token_usage", lambda *a, **k: None)
        monkeypatch.setattr(forwarding, "_coerce_usage_int", lambda v: int(v or 0))
        monkeypatch.setattr(forwarding, "_guardian_debug_headers", lambda *a, **k: {})
        monkeypatch.setattr(forwarding, "_is_retryable_cloud_error", lambda *a, **k: False)
        monkeypatch.setattr(forwarding, "_sanitize_proxied_response_headers", lambda h: {})
        monkeypatch.setattr(
            forwarding, "_dispatch_capture_request_completed", fake_completed
        )
        monkeypatch.setattr(
            forwarding, "_dispatch_capture_request_cancelled", lambda *a, **k: None
        )
        monkeypatch.setattr(
            forwarding, "_dispatch_capture_request_failed", lambda *a, **k: None
        )
        monkeypatch.setattr(forwarding, "_iter_sse_lines_with_watchdog", iter_sse_lines_dying)
        monkeypatch.setattr(forwarding, "cloud_rate_limiter", _FakeRateLimiter())
        monkeypatch.setattr(forwarding, "failover_health", _FakeHealthTracker())

        stream_client = _FakeStreamClient(
            httpx.Response(200, headers={"content-type": "text/event-stream"})
        )
        with patch.object(forwarding.httpx, "AsyncClient", return_value=stream_client):
            response = await forwarding.forward_to_cloud_provider(
                "chat/completions",
                b"{}",
                {
                    "model": "openrouter/test/model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                "openrouter/test/model",
                types.SimpleNamespace(),
                "test-client",
                capture_ctx=object(),
                capture_policy_result=object(),
                cloud_capture_start_time=0.0,
            )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)

        body = "".join(chunks)
        assert '"finish_reason":"length"' in body or '"finish_reason": "length"' in body
        assert body.rstrip().endswith("data: [DONE]")
        assert captured_kwargs.get("degeneration_cutoff") is True
        # human-visible marker injected BEFORE the finish chunk
        assert "[guardian: generation cut off" in body
        assert body.index("[guardian: generation cut off") < body.index("finish_reason")
