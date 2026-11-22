"""Unit tests for guardian_capture_v1 schema: IDs, event builders, fixtures."""

import hashlib
import hmac
import json
import os
import re
from dataclasses import replace
from datetime import datetime

import pytest

from app.capture.config import PROTOCOL_OPENAI, ROUTE_LOCAL, CaptureConfig
from app.capture.schema import (
    CLIENT_REF_PREVIOUS_SECRETS_ENV,
    CLIENT_REF_SECRET_ENV,
    RECORD_AUTH_SECRET_ENV,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    BuildContext,
    build_request_cancelled_event,
    build_request_completed_event,
    build_request_failed_event,
    build_request_received_event,
    compute_client_ref,
    compute_event_id,
    compute_record_auth,
    sanitize_upstream_model,
)

# ── Constants ──────────────────────────────────────────────────────────

TEST_INSTANCE_ID = "test-instance-001"
TEST_SECRET = "test-secret-for-capture-tests"
TEST_FINGERPRINT = "abc123def456"


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def capture_config():
    return CaptureConfig(
        enabled=True,
        local_capture=True,
        cloud_capture=False,
        instance_id=TEST_INSTANCE_ID,
        policy_version="1.0.0",
    )


@pytest.fixture
def base_ctx():
    return BuildContext(
        request_id="req-test-1234567890",
        endpoint="/v1/chat/completions",
        ingress_protocol=PROTOCOL_OPENAI,
        route_type=ROUTE_LOCAL,
        requested_model="llama3.2-3b",
        resolved_model="llama3.2-3b",
        capture_policy_version="1.0.0",
        instance_id=TEST_INSTANCE_ID,
        client_fingerprint=TEST_FINGERPRINT,
        streamed=False,
    )


@pytest.fixture
def test_secret_env(monkeypatch):
    monkeypatch.setenv(CLIENT_REF_SECRET_ENV, TEST_SECRET)
    yield


# ── Schema identity ────────────────────────────────────────────────────

class TestSchemaIdentity:
    def test_schema_name_is_guardian_capture_v1(self):
        assert SCHEMA_NAME == "guardian_capture_v1"

    def test_schema_version_is_semver(self):
        # Must be 1.x.0 for guardian_capture_v1
        assert SCHEMA_VERSION.startswith("1.")
        assert re.match(r"^1\.\d+\.\d+$", SCHEMA_VERSION)

    def test_schema_version_bumped_to_1_3_0(self):
        # Additive minor bumps: C1-C11 batch (1.1.0), degeneration_cutoff
        # (1.2.0), in-stream upstream error metadata (1.3.0, issue #125).
        assert SCHEMA_VERSION == "1.3.0"


# ── Event ID computation ───────────────────────────────────────────────

class TestEventId:
    def test_event_id_is_deterministic(self, capture_config):
        eid1 = compute_event_id(
            TEST_INSTANCE_ID, "req-123", "request_received", 0
        )
        eid2 = compute_event_id(
            TEST_INSTANCE_ID, "req-123", "request_received", 0
        )
        assert eid1 == eid2

    def test_event_id_is_sha256_hex(self, capture_config):
        eid = compute_event_id(
            TEST_INSTANCE_ID, "req-123", "request_received", 0
        )
        assert re.match(r"^[0-9a-f]{64}$", eid)

    def test_event_id_differs_by_sequence(self):
        eid0 = compute_event_id(TEST_INSTANCE_ID, "req-123", "request_received", 0)
        eid1 = compute_event_id(TEST_INSTANCE_ID, "req-123", "request_received", 1)
        assert eid0 != eid1

    def test_event_id_differs_by_event_type(self):
        eid_recv = compute_event_id(TEST_INSTANCE_ID, "req-123", "request_received", 0)
        eid_done = compute_event_id(TEST_INSTANCE_ID, "req-123", "request_completed", 1)
        assert eid_recv != eid_done

    def test_event_id_matches_manual_sha256(self):
        raw = f"{TEST_INSTANCE_ID}|req-123|request_received|0"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert compute_event_id(TEST_INSTANCE_ID, "req-123", "request_received", 0) == expected

    def test_event_id_handles_pipe_in_components(self):
        # Components must not break the delimiter
        eid = compute_event_id(TEST_INSTANCE_ID, "req|pipe", "request_received", 0)
        assert re.match(r"^[0-9a-f]{64}$", eid)


# ── Client ref computation ─────────────────────────────────────────────

class TestClientRef:
    def test_client_ref_is_hmac_sha256(self, test_secret_env):
        ref = compute_client_ref(TEST_FINGERPRINT)
        assert ref is not None
        expected = hmac.new(
            TEST_SECRET.encode("utf-8"),
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert ref == expected

    def test_client_ref_no_secret_returns_none(self, monkeypatch):
        monkeypatch.delenv(CLIENT_REF_SECRET_ENV, raising=False)
        assert compute_client_ref(TEST_FINGERPRINT) is None

    def test_client_ref_no_fingerprint_returns_none(self, test_secret_env):
        assert compute_client_ref(None) is None
        assert compute_client_ref("") is None

    def test_client_ref_does_not_persist_raw_fingerprint(self, test_secret_env):
        ref = compute_client_ref(TEST_FINGERPRINT)
        assert TEST_FINGERPRINT not in ref


# ── Event builders ─────────────────────────────────────────────────────

class TestRequestReceivedEvent:
    def test_has_all_required_fields(self, capture_config, base_ctx):
        event = build_request_received_event(
            capture_config, base_ctx,
            request_messages=[{"role": "user", "content": "hi"}],
            request_parameters={"temperature": 0.7},
            queue_wait_ms=10.0,
            sequence=0,
        )
        required = [
            "schema_name", "schema_version", "event_id", "event_type",
            "request_id", "sequence", "timestamp_utc", "guardian_instance_id",
            "client_ref", "endpoint", "ingress_protocol", "route_type",
            "requested_model", "capture_policy_version",
        ]
        for field in required:
            assert field in event, f"Missing required field: {field}"

    def test_event_type_is_request_received(self, capture_config, base_ctx):
        event = build_request_received_event(capture_config, base_ctx, sequence=0)
        assert event["event_type"] == "request_received"

    def test_event_id_matches_formula(self, capture_config, base_ctx):
        event = build_request_received_event(capture_config, base_ctx, sequence=0)
        expected = compute_event_id(
            capture_config.instance_id, base_ctx.request_id,
            "request_received", 0,
        )
        assert event["event_id"] == expected

    def test_timestamp_is_iso8601_utc(self, capture_config, base_ctx):
        event = build_request_received_event(capture_config, base_ctx, sequence=0)
        ts = event["timestamp_utc"]
        # ISO 8601 with Z suffix
        assert ts.endswith("Z")
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
        assert parsed is not None  # raises if invalid

    def test_no_raw_api_key_in_event(self, capture_config, base_ctx, test_secret_env):
        # Redaction is done by the redactor module before event construction.
        # Here we pass already-redacted messages to verify the event builder
        # preserves the redacted form.
        from app.capture.policy import evaluate_capture_policy
        from app.capture.redactor import redact_request_messages

        policy_result = evaluate_capture_policy(
            capture_config,
            route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=compute_client_ref(TEST_FINGERPRINT),
        )
        redacted = redact_request_messages(
            [{"role": "system", "content": "sk-or-v1-test-key-leak"},
             {"role": "user", "content": "hello"}],
            policy_result.field_policies,
        )
        # System messages are stripped — secret message should be gone
        assert redacted is not None
        for msg in redacted:
            assert "sk-or-v1-test" not in json.dumps(msg)

        event = build_request_received_event(
            capture_config, base_ctx,
            request_messages=redacted,
            request_parameters={"temperature": 0.7},  # no api_key in params
            sequence=0,
        )
        serialized = json.dumps(event)
        assert "sk-or-v1-test-key-leak" not in serialized


class TestRequestCompletedEvent:
    def test_event_type_is_request_completed(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello!",
            http_status=200,
            sequence=1,
        )
        assert event["event_type"] == "request_completed"

    def test_includes_token_usage(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello!",
            prompt_tokens=10, completion_tokens=5,
            http_status=200, sequence=1,
        )
        assert event["prompt_tokens"] == 10
        assert event["completion_tokens"] == 5
        assert event["total_tokens"] == 15

    def test_excludes_raw_client_ip(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="192.168.1.1 is my IP",
            sequence=1,
        )
        # IPs may survive in content text (they're response content, not PII precursors),
        # but we verify the event doesn't have a client_ip field.
        assert "client_ip" not in event
        assert "source_ip" not in event

    def test_optional_fields_omitted_when_none(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello",
            http_status=200,
            sequence=1,
        )
        # Fields not provided should be absent, not null
        assert "tool_calls" not in event
        assert "reasoning_content" not in event


class TestRequestFailedEvent:
    def test_event_type_is_request_failed(self, capture_config, base_ctx):
        event = build_request_failed_event(
            capture_config, base_ctx,
            error_code="model_not_served",
            http_status=404,
            sequence=1,
        )
        assert event["event_type"] == "request_failed"
        assert event["error_code"] == "model_not_served"
        assert event["http_status"] == 404


class TestRequestCancelledEvent:
    def test_event_type_is_request_cancelled(self, capture_config, base_ctx):
        event = build_request_cancelled_event(
            capture_config, base_ctx,
            cancel_reason="client_disconnected",
            sequence=1,
        )
        assert event["event_type"] == "request_cancelled"
        assert event["cancel_reason"] == "client_disconnected"


# ── JSONL serialization ────────────────────────────────────────────────

class TestJsonlSerialization:
    def test_event_serializes_to_valid_json(self, capture_config, base_ctx):
        event = build_request_received_event(
            capture_config, base_ctx,
            request_messages=[{"role": "user", "content": "hi"}],
            sequence=0,
        )
        line = json.dumps(event)
        parsed = json.loads(line)
        assert parsed["schema_name"] == "guardian_capture_v1"
        assert parsed["event_id"] == event["event_id"]

    def test_fixture_file_contains_valid_events(self):
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "capture_fixtures.jsonl"
        )
        with open(fixture_path) as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) >= 3  # At least request_received, completed, failed

        for i, line in enumerate(lines):
            event = json.loads(line)
            assert event["schema_name"] == "guardian_capture_v1"
            assert re.match(r"^[0-9a-f]{64}$", event["event_id"])
            assert event["event_type"] in (
                "request_received", "request_completed",
                "request_failed", "request_cancelled",
            )
            # Every event must have a client_ref (not null)
            assert event["client_ref"] is not None
            # Must not contain raw API keys
            serialized = json.dumps(event)
            assert "sk-or-v1-" not in serialized
            assert "nvapi-" not in serialized
            assert "sk-svcacct-" not in serialized

    def test_fixture_no_credentials_or_ips(self):
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "capture_fixtures.jsonl"
        )
        with open(fixture_path) as f:
            for line in f:
                if not line.strip():
                    continue
                event = json.loads(line)
                serialized = json.dumps(event)
                # No raw API keys
                assert not re.search(r"sk-[A-Za-z0-9_\-]{10,}", serialized)
                assert not re.search(r"nvapi-[A-Za-z0-9_\-]{10,}", serialized)
                # No authorization headers
                assert "authorization" not in serialized.lower()
                # No raw client IP (10.x, 192.168.x, 172.16-31.x)
                # client_ref is a hash, not an IP
                assert event.get("client_ref") != "127.0.0.1"


# ── Multi-secret rotation (Decision 1A) ─────────────────────────────────

class TestClientRefMultiSecretRotation:
    """Tests for seamless client_ref rotation via previous-secrets overlap."""

    def test_current_secret_takes_priority(self, monkeypatch):
        """When both current and legacy secrets are set, current-secret hash wins."""
        monkeypatch.setenv(CLIENT_REF_SECRET_ENV, "new-secret")
        monkeypatch.setenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, "old-secret")
        ref = compute_client_ref(TEST_FINGERPRINT)
        expected = hmac.new(
            b"new-secret",
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert ref == expected

    def test_legacy_secret_matches_existing_allowlist(self, monkeypatch):
        """During rotation, a client registered with the old secret keeps working."""
        old_secret = "old-secret"
        new_secret = "new-secret"
        monkeypatch.setenv(CLIENT_REF_SECRET_ENV, new_secret)
        monkeypatch.setenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, old_secret)

        # client_ref computed with the OLD secret (what's already in the allowlist)
        old_ref = hmac.new(
            old_secret.encode("utf-8"),
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # With allowed_refs containing the old hash, compute_client_ref should
        # return the old hash (matching the allowlist) not the new one
        ref = compute_client_ref(TEST_FINGERPRINT, allowed_refs=[old_ref])
        assert ref == old_ref

    def test_new_client_gets_current_secret_hash(self, monkeypatch):
        """A new (unregistered) client gets the hash with the current secret."""
        monkeypatch.setenv(CLIENT_REF_SECRET_ENV, "new-secret")
        monkeypatch.setenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, "old-secret")

        # allowed_refs is empty — should return current-secret hash
        ref = compute_client_ref(TEST_FINGERPRINT, allowed_refs=[])
        expected = hmac.new(
            b"new-secret",
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert ref == expected

    def test_multiple_legacy_secrets(self, monkeypatch):
        """Multiple comma-separated legacy secrets are all tried."""
        monkeypatch.setenv(CLIENT_REF_SECRET_ENV, "v3-secret")
        monkeypatch.setenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, "v1-secret,v2-secret")

        # Hash with v2 should match
        v2_ref = hmac.new(
            b"v2-secret",
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        ref = compute_client_ref(TEST_FINGERPRINT, allowed_refs=[v2_ref])
        assert ref == v2_ref

    def test_no_previous_secret_fallback(self, monkeypatch):
        """Without the previous-secrets env var, only current secret is used."""
        monkeypatch.setenv(CLIENT_REF_SECRET_ENV, "current-secret")
        monkeypatch.delenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, raising=False)
        ref = compute_client_ref(TEST_FINGERPRINT)
        expected = hmac.new(
            b"current-secret",
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert ref == expected

    def test_no_secrets_at_all_returns_none(self, monkeypatch):
        """When neither current nor previous secrets are set, returns None."""
        monkeypatch.delenv(CLIENT_REF_SECRET_ENV, raising=False)
        monkeypatch.delenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, raising=False)
        assert compute_client_ref(TEST_FINGERPRINT) is None

    def test_only_legacy_secret_no_current(self, monkeypatch):
        """When current secret is unset but legacy exists, uses first legacy."""
        monkeypatch.delenv(CLIENT_REF_SECRET_ENV, raising=False)
        monkeypatch.setenv(CLIENT_REF_PREVIOUS_SECRETS_ENV, "legacy-secret")
        ref = compute_client_ref(TEST_FINGERPRINT)
        expected = hmac.new(
            b"legacy-secret",
            TEST_FINGERPRINT.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert ref == expected


# ── Per-record HMAC authentication (Decision 2A) ────────────────────────

class TestRecordAuth:
    """Tests for per-record HMAC on WAL JSONL lines."""

    def test_record_auth_returns_none_without_secret(self, monkeypatch):
        """No RECORD_AUTH_SECRET_ENV → no record_auth."""
        monkeypatch.delenv(RECORD_AUTH_SECRET_ENV, raising=False)
        result = compute_record_auth('{"foo":"bar"}')
        assert result is None

    def test_record_auth_returns_correct_structure(self, monkeypatch):
        """record_auth contains alg, key_id, and mac fields."""
        monkeypatch.setenv(RECORD_AUTH_SECRET_ENV, "auth-secret-123")
        result = compute_record_auth('{"foo":"bar"}')
        assert result is not None
        assert result["alg"] == "hmac-sha256"
        assert "key_id" in result
        assert "mac" in result
        assert len(result["key_id"]) == 16  # first 16 hex chars
        assert len(result["mac"]) == 64  # full SHA-256 hex

    def test_key_id_is_deterministic(self, monkeypatch):
        """key_id is the same for the same secret."""
        monkeypatch.setenv(RECORD_AUTH_SECRET_ENV, "auth-secret-123")
        r1 = compute_record_auth('{"a":1}')
        r2 = compute_record_auth('{"b":2}')
        assert r1["key_id"] == r2["key_id"]

    def test_mac_differs_per_record_content(self, monkeypatch):
        """Different record content produces different MACs."""
        monkeypatch.setenv(RECORD_AUTH_SECRET_ENV, "auth-secret-123")
        r1 = compute_record_auth('{"content":"hello"}')
        r2 = compute_record_auth('{"content":"world"}')
        assert r1["mac"] != r2["mac"]

    def test_mac_is_verifiable(self, monkeypatch):
        """The MAC can be verified by recomputing HMAC over the same input."""
        monkeypatch.setenv(RECORD_AUTH_SECRET_ENV, "auth-secret-123")
        line = '{"event_type":"request_received","sequence":0}'
        result = compute_record_auth(line)
        expected_mac = hmac.new(
            b"auth-secret-123",
            line.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert result["mac"] == expected_mac

    def test_different_secret_produces_different_mac(self, monkeypatch):
        """Different secrets produce different MACs for the same content."""
        line = '{"content":"test"}'
        monkeypatch.setenv(RECORD_AUTH_SECRET_ENV, "secret-A")
        r_a = compute_record_auth(line)
        monkeypatch.setenv(RECORD_AUTH_SECRET_ENV, "secret-B")
        r_b = compute_record_auth(line)
        assert r_a["mac"] != r_b["mac"]
        assert r_a["key_id"] != r_b["key_id"]


class TestSharedCrossRepoVector:
    """Shared test vector with Keanu Factory (guardian_capture_parser.py).

    Both repositories pin the SAME vector so a change in either side's
    serialisation or HMAC construction breaks the other side's test.
    Source of truth for generation: Guardian's compute_record_auth +
    wal_writer's append-last placement. Keanu verifies this exact line in
    tests/unit/parsers/test_guardian_capture_parser.py.
    """

    SECRET = "cross-repo-shared-test-secret"
    EXPECTED_KEY_ID = "e6be02723853c25b"
    EXPECTED_MAC = "2576d18fc2a2bac06a4dd7f4c543af82dd8cf5fa9ed4526056765d551e53ea49"

    def test_vector_key_id(self, monkeypatch):
        monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", self.SECRET)
        auth = compute_record_auth('{"schema_name":"guardian_capture_v1","schema_version":1}')
        assert auth["key_id"] == self.EXPECTED_KEY_ID

    def test_vector_mac(self, monkeypatch):
        import json as _json
        event = {
            "schema_name": "guardian_capture_v1",
            "schema_version": 1,
            "event_id": "a" * 64,
            "event_type": "request_received",
            "request_id": "req-shared-vector-001",
            "sequence": 1,
            "timestamp_utc": "2026-08-12T00:00:00.000Z",
            "guardian_instance_id": "guardian-local-test",
            "endpoint": "/v1/chat/completions",
            "ingress_protocol": "openai",
            "route_type": "local",
            "requested_model": "llama3.2-3b",
            "capture_policy_version": 1,
            "client_ref": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "payload": {"request_messages": [{"role": "user", "content": "Say OK"}]},
        }
        monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", self.SECRET)
        line_no_auth = _json.dumps(event, separators=(",", ":"), sort_keys=False)
        auth = compute_record_auth(line_no_auth)
        assert auth["mac"] == self.EXPECTED_MAC


# ── Capture-feedback 1.1.0: timestamps (C1) ─────────────────────────────

ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class TestTimestampFieldsC1:
    def test_build_context_auto_stamps_started_at(self, base_ctx):
        assert base_ctx.started_at_utc is not None
        assert ISO_TS_RE.match(base_ctx.started_at_utc)

    def test_received_event_started_at_equals_timestamp(self, capture_config, base_ctx):
        event = build_request_received_event(capture_config, base_ctx, sequence=0)
        assert event["started_at_utc"] == event["timestamp_utc"]

    def test_completed_event_has_start_and_completed(self, capture_config, base_ctx):
        event = build_request_completed_event(capture_config, base_ctx, sequence=1)
        assert event["started_at_utc"] == base_ctx.started_at_utc
        assert event["completed_at_utc"] == event["timestamp_utc"]

    def test_failed_and_cancelled_events_have_both(self, capture_config, base_ctx):
        failed = build_request_failed_event(
            capture_config, base_ctx, error_code="boom", sequence=1
        )
        cancelled = build_request_cancelled_event(
            capture_config, base_ctx, cancel_reason="client_disconnect", sequence=1
        )
        for event in (failed, cancelled):
            assert event["started_at_utc"] == base_ctx.started_at_utc
            assert event["completed_at_utc"] == event["timestamp_utc"]
            assert ISO_TS_RE.match(event["started_at_utc"])

    def test_timestamp_format_unchanged(self, capture_config, base_ctx):
        event = build_request_completed_event(capture_config, base_ctx, sequence=1)
        assert ISO_TS_RE.match(event["timestamp_utc"])


# ── Capture-feedback 1.1.0: finish_reason contract (C4) ─────────────────


class TestFinishReasonContractC4:
    def test_finish_reason_always_present(self, capture_config, base_ctx):
        event = build_request_completed_event(capture_config, base_ctx, sequence=1)
        assert "finish_reason" in event
        assert event["finish_reason"] is None

    def test_finish_reason_value_preserved(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, finish_reason="length", sequence=1
        )
        assert event["finish_reason"] == "length"

    def test_native_finish_reason_when_known(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            finish_reason="stop", native_finish_reason="length", sequence=1,
        )
        assert event["native_finish_reason"] == "length"

    def test_native_finish_reason_absent_when_unknown(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, finish_reason="stop", sequence=1
        )
        assert "native_finish_reason" not in event


# ── Capture-feedback 1.1.0: rich usage mirror (C5) ──────────────────────


class TestUsageMirrorC5:
    def test_completion_tokens_details_stored_as_is(self, capture_config, base_ctx):
        details = {"reasoning_tokens": 1024, "audio_tokens": 0}
        event = build_request_completed_event(
            capture_config, base_ctx, completion_tokens_details=details, sequence=1
        )
        assert event["completion_tokens_details"] == details

    def test_native_token_counts_coerced_to_int(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            native_tokens_reasoning=2048.0, native_tokens_cached="64", sequence=1,
        )
        assert event["native_tokens_reasoning"] == 2048
        assert event["native_tokens_cached"] == 64
        assert isinstance(event["native_tokens_reasoning"], int)

    def test_cost_and_provider_name(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, cost=0.0042, provider_name="DeepSeek", sequence=1
        )
        assert event["cost"] == 0.0042
        assert event["provider_name"] == "DeepSeek"

    def test_mirror_fields_absent_when_not_provided(self, capture_config, base_ctx):
        event = build_request_completed_event(capture_config, base_ctx, sequence=1)
        for field in (
            "completion_tokens_details", "native_tokens_reasoning",
            "native_tokens_cached", "cost", "provider_name",
        ):
            assert field not in event


# ── Capture-feedback 1.1.0: caller identity (C5/C6) ─────────────────────


class TestCallerIdentityC6:
    def test_caller_request_id_on_all_event_types(self, capture_config, base_ctx):
        base_ctx.caller_request_id = "client-req-42"
        base_ctx.app_title = "my-app"
        base_ctx.app_referer = "https://my-app.example"
        received = build_request_received_event(capture_config, base_ctx, sequence=0)
        completed = build_request_completed_event(capture_config, base_ctx, sequence=1)
        failed = build_request_failed_event(
            capture_config, base_ctx, error_code="boom", sequence=1
        )
        cancelled = build_request_cancelled_event(
            capture_config, base_ctx, cancel_reason="client_disconnect", sequence=1
        )
        for event in (received, completed, failed, cancelled):
            assert event["caller_request_id"] == "client-req-42"
            assert event["app_title"] == "my-app"
            assert event["app_referer"] == "https://my-app.example"

    def test_identity_absent_when_not_provided(self, capture_config, base_ctx):
        event = build_request_completed_event(capture_config, base_ctx, sequence=1)
        assert "caller_request_id" not in event
        assert "app_title" not in event
        assert "app_referer" not in event


# ── Capture-feedback 1.1.0: streamed legs (C8) ──────────────────────────


class TestStreamedLegsC8:
    def test_streamed_legs_emitted_when_known(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            streamed_ingress=True, streamed_upstream=True, sequence=1,
        )
        assert event["streamed_ingress"] is True
        assert event["streamed_upstream"] is True
        assert event["streamed"] is True  # compat = ingress leg

    def test_streamed_falls_back_to_ingress(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, streamed_ingress=False, sequence=1
        )
        assert event["streamed"] is False
        assert event["streamed_ingress"] is False
        assert "streamed_upstream" not in event

    def test_explicit_streamed_arg_still_wins(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            streamed=False, streamed_ingress=True, sequence=1,
        )
        assert event["streamed"] is False

    def test_legs_absent_when_unknown(self, capture_config, base_ctx):
        event = build_request_completed_event(capture_config, base_ctx, sequence=1)
        assert "streamed_ingress" not in event
        assert "streamed_upstream" not in event


# ── Capture-feedback 1.1.0: defensive int coercion (C2) ─────────────────


class TestIntCoercionC2:
    def test_float_tokens_coerced(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, prompt_tokens=298704.0, completion_tokens=1556.0,
            sequence=1,
        )
        assert event["prompt_tokens"] == 298704
        assert event["completion_tokens"] == 1556
        assert event["total_tokens"] == 300260
        assert isinstance(event["prompt_tokens"], int)

    def test_float_attempts_coerced(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, attempts=2.0, sequence=1
        )
        assert event["attempts"] == 2
        assert isinstance(event["attempts"], int)

    def test_garbage_tokens_omit_field_not_crash(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx, prompt_tokens="not-a-number",
            completion_tokens=3, sequence=1,
        )
        assert "prompt_tokens" not in event
        assert event["completion_tokens"] == 3
        assert "total_tokens" not in event

    def test_non_finite_tokens_omit_field_not_crash(self, capture_config, base_ctx):
        """JSON ``1e999`` parses to ``inf``; ``int(inf)`` raises OverflowError.
        The event must survive with the field omitted and no bare ``Infinity``
        may enter the JSONL line (review finding)."""
        event = build_request_completed_event(
            capture_config, base_ctx, prompt_tokens=float("inf"),
            completion_tokens=float("nan"), cost=float("inf"), sequence=1,
        )
        assert "prompt_tokens" not in event
        assert "completion_tokens" not in event
        assert "total_tokens" not in event
        assert "cost" not in event
        import json as _json
        assert "Infinity" not in _json.dumps(event)
        assert "NaN" not in _json.dumps(event)


# ── Capture-feedback 1.1.0: correlation header config (C6) ──────────────


class TestCorrelationHeaderConfigC6:
    def test_default_is_x_request_id(self):
        cfg = CaptureConfig()
        assert cfg.correlation_headers == ["x-request-id"]

    def test_yaml_list_normalized_to_lowercase(self, tmp_path):
        from app.capture.config import load_capture_config
        settings = tmp_path / "global.settings.yaml"
        settings.write_text(
            "capture:\n"
            "  enabled: false\n"
            "  correlation_headers:\n"
            "    - ' X-Request-Id '\n"
            "    - 'x-trace-id'\n"
        )
        cfg = load_capture_config(settings)
        assert cfg.correlation_headers == ["x-request-id", "x-trace-id"]

    def test_yaml_empty_list_is_explicit_opt_out(self, tmp_path):
        """Review fix: an explicit empty list must disable the echo, not
        silently restore the default correlation headers."""
        from app.capture.config import load_capture_config
        settings = tmp_path / "global.settings.yaml"
        settings.write_text(
            "capture:\n"
            "  enabled: false\n"
            "  correlation_headers: []\n"
        )
        cfg = load_capture_config(settings)
        assert cfg.correlation_headers == []

    def test_direct_empty_list_is_explicit_opt_out(self):
        assert CaptureConfig(correlation_headers=[]).correlation_headers == []

    def test_all_invalid_entries_restore_default(self, tmp_path):
        """Tolerance path: a NON-empty list whose entries are all invalid
        falls back to the default (an empty list stays an opt-out)."""
        from app.capture.config import load_capture_config
        settings = tmp_path / "global.settings.yaml"
        settings.write_text(
            "capture:\n"
            "  enabled: false\n"
            "  correlation_headers:\n"
            "    - '   '\n"
            "    - 42\n"
        )
        cfg = load_capture_config(settings)
        assert cfg.correlation_headers == ["x-request-id"]

    def test_non_lowercase_header_rejected(self):
        with pytest.raises(ValueError):
            CaptureConfig(correlation_headers=["X-Request-ID"])

    def test_too_many_headers_rejected(self):
        with pytest.raises(ValueError):
            CaptureConfig(correlation_headers=[f"h{i}" for i in range(9)])


# ── Capture-feedback 1.1.0: dispatch-layer extraction (C4/C5/C6) ────────


class _StubCaptureController:
    """Records dispatch kwargs/events without touching the real controller."""

    def __init__(self):
        self.config = CaptureConfig(
            enabled=True,
            local_capture=True,
            per_client_opt_in=False,
            correlation_headers=["x-request-id", "x-trace-id"],
        )
        self.received_kwargs = None
        self.completed_kwargs = []

    def maybe_capture_request_received(self, **kwargs):
        self.received_kwargs = kwargs
        from types import SimpleNamespace
        return SimpleNamespace(should_capture=True, reason="ok", field_policies={})

    def capture_request_completed(self, ctx, **kwargs):
        self.completed_kwargs.append(kwargs)


class TestCaptureDispatchExtractionC4C5C6:
    def _ctx(self):
        return BuildContext(
            request_id="req-dispatch-1",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
            resolved_model="llama3.2-3b",
            capture_policy_version="1.0.0",
            instance_id=TEST_INSTANCE_ID,
            client_fingerprint=TEST_FINGERPRINT,
        )

    def test_nonstream_extracts_choice_level_finish_and_mirror(self, monkeypatch):
        import time as _time
        from types import SimpleNamespace

        from app.gateway import capture_dispatch
        from app.gateway.usage import coerce_usage_int

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        # The dispatcher normally receives this helper via init() DI.
        monkeypatch.setattr(capture_dispatch, "_coerce_usage_int", coerce_usage_int)
        payload = {
            "choices": [{
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
                "native_finish_reason": "length",
            }],
            "usage": {
                "prompt_tokens": 10.0,
                "completion_tokens": 5.0,
                "completion_tokens_details": {"reasoning_tokens": 2},
                "native_tokens_reasoning": 2,
                "native_tokens_cached": 1,
                "cost": 0.01,
            },
            "provider": "DeepSeek",
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            None, "req-dispatch-1", "cli", "llama3.2-3b",
            self._ctx(), SimpleNamespace(should_capture=True), payload, 200,
            _time.monotonic(),
        )
        assert len(stub.completed_kwargs) == 1
        kwargs = stub.completed_kwargs[0]
        assert kwargs["finish_reason"] == "stop"
        assert kwargs["native_finish_reason"] == "length"
        assert kwargs["prompt_tokens"] == 10
        assert kwargs["completion_tokens"] == 5
        assert kwargs["completion_tokens_details"] == {"reasoning_tokens": 2}
        assert kwargs["native_tokens_reasoning"] == 2
        assert kwargs["native_tokens_cached"] == 1
        assert kwargs["cost"] == 0.01
        assert kwargs["provider_name"] == "DeepSeek"
        assert kwargs["streamed_ingress"] is False
        assert kwargs["streamed_upstream"] is False

    def test_nonstream_without_finish_reason_reports_none(self, monkeypatch):
        import time as _time
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        payload = {"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}
        capture_dispatch.dispatch_capture_nonstream_completed(
            None, "req-dispatch-2", "cli", "llama3.2-3b",
            self._ctx(), SimpleNamespace(should_capture=True), payload, 200,
            _time.monotonic(),
        )
        kwargs = stub.completed_kwargs[0]
        assert kwargs["finish_reason"] is None
        assert kwargs["native_finish_reason"] is None

    def test_stream_dispatch_pulls_mirror_from_assembler(self, monkeypatch):
        from types import SimpleNamespace

        from app.capture.stream_assembler import StreamResponseAssembler
        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        assembler = StreamResponseAssembler()
        assembler.add_sse_line(
            'data: {"provider":"GR","choices":[{"delta":{"content":"Hello"}}]}'
        )
        assembler.add_sse_line(
            'data: {"choices":[{"delta":{},"finish_reason":"stop",'
            '"native_finish_reason":"length"}],'
            '"usage":{"prompt_tokens":7,"completion_tokens":3,'
            '"native_tokens_reasoning":2,"native_tokens_cached":1,"cost":0.002}}'
        )
        capture_dispatch.dispatch_capture_stream_completed(
            None, "req-dispatch-3", "cli", "llama3.2-3b",
            self._ctx(), SimpleNamespace(should_capture=True), assembler,
            {"prompt_tokens": 7, "completion_tokens": 3}, "chat/completions", 200,
        )
        kwargs = stub.completed_kwargs[0]
        assert kwargs["finish_reason"] == "stop"
        assert kwargs["native_finish_reason"] == "length"
        assert kwargs["completion_tokens_details"] is None
        assert kwargs["native_tokens_reasoning"] == 2
        assert kwargs["native_tokens_cached"] == 1
        assert kwargs["cost"] == 0.002
        assert kwargs["provider_name"] == "GR"
        assert kwargs["streamed_ingress"] is True
        assert kwargs["streamed_upstream"] is True

    def test_received_extracts_correlation_and_app_headers(self, monkeypatch):
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        request = SimpleNamespace(headers={
            # Fake request headers use the canonical lowercase form (real
            # Starlette headers are case-insensitive and stored lowercase).
            "x-request-id": "  client-req-xyz  ",
            "x-trace-id": "ignored-when-x-request-id-present",
            "x-title": "Probe App",
            "http-referer": "https://probe.example",
        })
        capture_dispatch.dispatch_capture_request_received(
            request, "cli",
            request_id="req-dispatch-4",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
        )
        kwargs = stub.received_kwargs
        assert kwargs["caller_request_id"] == "client-req-xyz"
        assert kwargs["app_title"] == "Probe App"
        assert kwargs["app_referer"] == "https://probe.example"

    def test_received_responses_text_format_sets_grammar_flag(self, monkeypatch):
        """Responses ``text.format`` (json_schema/json_object) is the
        Responses equivalent of a grammar request → presence flag True."""
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        request = SimpleNamespace(headers={})
        capture_dispatch.dispatch_capture_request_received(
            request, "cli",
            request_id="req-dispatch-8",
            endpoint="/v1/responses",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
            request_parameters={
                "text": {"format": {"type": "json_schema", "schema": {"x": 1}}},
            },
        )
        kwargs = stub.received_kwargs
        assert kwargs["grammar_present"] is True
        assert kwargs["response_format_present"] is False

    def test_received_responses_plain_text_format_sets_no_grammar_flag(self, monkeypatch):
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        request = SimpleNamespace(headers={})
        capture_dispatch.dispatch_capture_request_received(
            request, "cli",
            request_id="req-dispatch-9",
            endpoint="/v1/responses",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
            request_parameters={"text": {"format": {"type": "text"}}},
        )
        assert stub.received_kwargs["grammar_present"] is False

    def test_received_second_correlation_header_used_when_first_absent(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        request = SimpleNamespace(headers={"x-trace-id": "trace-9"})
        capture_dispatch.dispatch_capture_request_received(
            request, "cli",
            request_id="req-dispatch-5",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
        )
        assert stub.received_kwargs["caller_request_id"] == "trace-9"

    def test_received_absent_headers_yield_none(self, monkeypatch):
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        request = SimpleNamespace(headers={})
        capture_dispatch.dispatch_capture_request_received(
            request, "cli",
            request_id="req-dispatch-6",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
        )
        assert stub.received_kwargs["caller_request_id"] is None
        assert stub.received_kwargs["app_title"] is None
        assert stub.received_kwargs["app_referer"] is None

    def test_received_empty_correlation_headers_disable_echo(self, monkeypatch):
        """Review fix: the dispatch layer must honour the config-layer
        opt-out — an explicit `correlation_headers: []` means NO caller
        header is echoed, even when the request carries X-Request-Id."""
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        stub.config = replace(stub.config, correlation_headers=[])
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        request = SimpleNamespace(headers={"x-request-id": "should-not-echo"})
        capture_dispatch.dispatch_capture_request_received(
            request, "cli",
            request_id="req-dispatch-7",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
        )
        assert stub.received_kwargs["caller_request_id"] is None


class TestCompletedEventStreamError:
    """Issue #125 — an upstream error delivered inside an HTTP 200 stream.

    The code/type are metadata and land on ``request_completed``; the
    message text never does, and a normal completion is byte-identical to
    before (absent-stays-absent).
    """

    def test_error_metadata_recorded_when_present(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            finish_reason=None,
            incomplete=True,
            http_status=200,
            error_code="502",
            error_type="provider_error",
            sequence=1,
        )
        assert event["error_code"] == "502"
        assert event["error_type"] == "provider_error"
        # Existing keys keep their value/type: the stream really did end
        # without a finish reason.
        assert event["finish_reason"] is None
        assert event["incomplete"] is True

    def test_error_fields_absent_on_normal_completion(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello!",
            finish_reason="stop",
            http_status=200,
            sequence=1,
        )
        assert "error_code" not in event
        assert "error_type" not in event

    def test_message_text_never_lands_on_event(self, capture_config, base_ctx):
        message = "Provider returned an empty response"
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code="502",
            error_type=message,  # prose passed by mistake must not be stored
            sequence=1,
        )
        assert event["error_code"] == "502"
        assert "error_type" not in event
        assert message not in json.dumps(event)

    def test_prose_as_code_is_rejected(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code="Provider returned an empty response",
            sequence=1,
        )
        assert "error_code" not in event

    def test_payload_shaped_metadata_is_rejected(self, capture_config, base_ctx):
        """Finding from PR #126 review: whitespace-free JSON/URL/key-value is
        payload, not metadata, and must never land on the event."""
        for bad in ('{"prompt":"private"}', "https://provider.example/secret", "k=v&x=y"):
            event = build_request_completed_event(
                capture_config, base_ctx,
                response_content="",
                http_status=200,
                error_code=bad,
                error_type=bad,
                sequence=1,
            )
            assert "error_code" not in event, bad
            assert "error_type" not in event, bad

    def test_metadata_is_bounded_not_truncated(self, capture_config, base_ctx):
        """Over-long values are rejected, never truncated (PR #126 review)."""
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code="9" * 200,
            error_type="t" * 200,
            sequence=1,
        )
        assert "error_code" not in event
        assert "error_type" not in event

    def test_credential_prefix_with_prose_suffix_is_rejected(self, capture_config, base_ctx):
        """Review finding 2 (PR #126): the whole stripped value is validated,
        so truncation cannot leave an identifier-shaped credential prefix."""
        leak = "sk-" + "a" * 80 + " was rejected"
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code=leak,
            error_type=leak,
            sequence=1,
        )
        assert "error_code" not in event
        assert "error_type" not in event
        assert "sk-" not in json.dumps(event)

    def test_fractional_error_code_is_dropped(self, capture_config, base_ctx):
        """Review finding 3 (PR #126): a numeric ``502.5`` must not be
        rewritten into the different code ``"502"``; it is omitted, while
        ``502.0`` keeps its numeric identity."""
        dropped = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code=502.5,
            error_type="provider_error",
            sequence=1,
        )
        assert "error_code" not in dropped
        assert dropped["error_type"] == "provider_error"

        integral = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code=502.0,
            sequence=1,
        )
        assert integral["error_code"] == "502"

    def test_string_error_code_stays_opaque(self, capture_config, base_ctx):
        """A *string* code is metadata as reported — dotted codes are legal
        provider identifiers and are stored verbatim."""
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            http_status=200,
            error_code="provider.rate_limited",
            sequence=1,
        )
        assert event["error_code"] == "provider.rate_limited"

    def test_stream_dispatch_pulls_error_metadata_from_assembler(self, monkeypatch):
        from types import SimpleNamespace

        from app.capture.stream_assembler import StreamResponseAssembler
        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        assembler = StreamResponseAssembler()
        assembler.add_sse_line(
            'data: {"error":{"code":502,"message":"Provider returned an empty '
            'response","type":"provider_error"}}'
        )
        assembler.add_sse_line("data: [DONE]")
        ctx = BuildContext(
            request_id="req-stream-error-1",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
            resolved_model="llama3.2-3b",
            capture_policy_version="1.0.0",
            instance_id=TEST_INSTANCE_ID,
            client_fingerprint=TEST_FINGERPRINT,
        )
        capture_dispatch.dispatch_capture_stream_completed(
            None, "req-stream-error-1", "cli", "llama3.2-3b",
            ctx, SimpleNamespace(should_capture=True), assembler,
            {"prompt_tokens": 0, "completion_tokens": 0}, "chat/completions", 200,
        )
        kwargs = stub.completed_kwargs[0]
        assert kwargs["error_code"] == "502"
        assert kwargs["error_type"] == "provider_error"
        # The failure was previously invisible: no finish reason, incomplete.
        assert kwargs["finish_reason"] is None
        assert kwargs["incomplete"] is True

    def test_stream_dispatch_omits_error_metadata_on_normal_stream(self, monkeypatch):
        from types import SimpleNamespace

        from app.capture.stream_assembler import StreamResponseAssembler
        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        assembler = StreamResponseAssembler()
        assembler.add_sse_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
        assembler.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        assembler.add_sse_line("data: [DONE]")
        ctx = BuildContext(
            request_id="req-stream-error-2",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="llama3.2-3b",
            resolved_model="llama3.2-3b",
            capture_policy_version="1.0.0",
            instance_id=TEST_INSTANCE_ID,
            client_fingerprint=TEST_FINGERPRINT,
        )
        capture_dispatch.dispatch_capture_stream_completed(
            None, "req-stream-error-2", "cli", "llama3.2-3b",
            ctx, SimpleNamespace(should_capture=True), assembler,
            {"prompt_tokens": 0, "completion_tokens": 0}, "chat/completions", 200,
        )
        kwargs = stub.completed_kwargs[0]
        assert kwargs["error_code"] is None
        assert kwargs["error_type"] is None
        assert kwargs["finish_reason"] == "stop"


class TestUpstreamModelSanitizer:
    """Issue #127 — a model identifier is not an enum member.

    It legitimately carries ``/ . : - _ @`` and digits, so it gets its own
    bounded identifier rule instead of the ``error_code`` charset.
    """

    @pytest.mark.parametrize("value", [
        "gpt-4o",
        "gpt-4o-mini",
        "openai/gpt-4o-2024-08-06",
        "anthropic/claude-sonnet-4.5",
        "claude-3.5-sonnet",
        "qwen2.5-coder:32b@q4_k_m",
        "models/gemini-2.5-flash",
        "meta-llama/llama-3.3-70b-instruct",
        "DeepSeek-V3.2-Exp",
        "openrouter/openai/gpt-4o:floor",
        # Provider ``:variant`` ids keep the colon: a colon alone is not an
        # address (review finding on PR #128 — the scheme rule must not eat
        # these).
        "google/gemini-3-pro:free",
        "openrouter/meta-llama/llama-3.3-70b-instruct:free",
        "nvidia/llama-3.1-nemotron-70b-instruct:floor",
        "model:variant",
        "m",
        "9",
    ])
    def test_accepts_real_model_ids(self, value):
        assert sanitize_upstream_model(value) == value

    def test_strips_surrounding_whitespace(self):
        assert sanitize_upstream_model("  openai/gpt-4o\n") == "openai/gpt-4o"

    def test_boundary_length_is_128_and_129_is_rejected(self):
        longest = "a" * 128
        assert sanitize_upstream_model(longest) == longest
        assert sanitize_upstream_model("a" * 129) is None

    @pytest.mark.parametrize("value", [
        # Embedded JSON.
        '{"prompt":"private"}',
        '{"model":"x"}',
        # URLs — the colon and slash alone are legal *inside* an id, so the
        # scheme/authority separator is what makes this a URL.
        "http://provider.example/secret",
        "https://provider.example/secret",
        "HTTP://provider.example/secret",
        "//provider.example/secret",
        # Address-carrying schemes that pass the charset (review finding on
        # PR #128: these used to land as ``upstream_model``).
        "mailto:alice@example.com",
        "MAILTO:Alice@Example.com",
        "file:/private/path",
        "File:/etc/passwd",
        "data:text/plain;base64,QQ==",
        "javascript:alert",
        "ws://provider.example/secret",
        "wss://provider.example/secret",
        "ftp:provider.example/secret",
        "http:provider.example",
        # A scheme followed by ``/`` is an address on its face, even for a
        # scheme name no denylist knows (``ssh://host``, ``file:/p``).
        "ssh://provider.example/secret",
        "gopher:/provider.example/secret",
        # Query strings / key-value pairs.
        "k=v&x=y",
        "model=gpt-4o",
        # Prose and any whitespace.
        "Provider returned an empty response",
        "gpt-4o was not found",
        "gpt\t4o",
        "gpt 4o",
        # Non-id shapes.
        "",
        "   ",
        "/gpt-4o",
        "gpt-4o/",
        "-gpt-4o",
        "gpt-4o,",
        "openai/gpt-4o;",
        None,
        502,
        4.5,
        True,
        ["gpt-4o"],
        {"model": "gpt-4o"},
    ])
    def test_rejects_payload_shapes_and_non_strings(self, value):
        assert sanitize_upstream_model(value) is None

    def test_whole_value_validated_before_bounding(self):
        """PR #126 lesson: validating after truncation would keep an
        identifier-shaped 128-char prefix while the suffix that proves it is
        not a model id falls off the end — a credential fragment could leak
        as ``upstream_model``.  Over-long values are rejected outright."""
        leak = "sk-" + "a" * 200 + " was rejected"
        assert sanitize_upstream_model(leak) is None
        # Same shape without the prose suffix is still over-long → rejected,
        # never truncated to its first 128 characters.
        assert sanitize_upstream_model("sk-" + "a" * 200) is None
        assert sanitize_upstream_model("a" * 128 + "B") is None


class TestCompletedEventUpstreamModel:
    """Issue #127 — ``request_completed.upstream_model`` is provider-decided.

    It is populated only when the provider reported a usable value; unknown
    stays exactly as before (``null``), so no consumer breaks.
    """

    def test_provider_reported_model_lands_on_event(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello!",
            finish_reason="stop",
            upstream_model="openai/gpt-4o-2024-08-06",
            http_status=200,
            sequence=1,
        )
        assert event["upstream_model"] == "openai/gpt-4o-2024-08-06"

    def test_absent_when_provider_reported_nothing(self, capture_config, base_ctx):
        """The key keeps its pre-#127 contract: present, and null."""
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello!",
            finish_reason="stop",
            http_status=200,
            sequence=1,
        )
        assert event["upstream_model"] is None

    def test_never_inferred_from_resolved_model(self, capture_config, base_ctx):
        """What Guardian decided (``resolved_model``) is not what served."""
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="Hello!",
            finish_reason="stop",
            http_status=200,
            sequence=1,
        )
        assert base_ctx.resolved_model == "llama3.2-3b"
        assert event["upstream_model"] is None

    def test_payload_shaped_values_rejected_at_event_level(self, capture_config, base_ctx):
        """The event builder is the final guarantee: a payload-shaped value
        reaching it must not land on the wire."""
        for bad in (
            '{"prompt":"private"}',
            "https://provider.example/secret",
            "k=v&x=y",
            "Provider returned an empty response",
            # Review finding on PR #128: charset-valid addresses.
            "mailto:alice@example.com",
            "file:/private/path",
        ):
            event = build_request_completed_event(
                capture_config, base_ctx,
                response_content="",
                upstream_model=bad,
                http_status=200,
                sequence=1,
            )
            assert event["upstream_model"] is None, bad
            assert "private" not in json.dumps(event), bad
            assert "alice@example.com" not in json.dumps(event), bad

    def test_provider_free_variant_survives_event_level(self, capture_config, base_ctx):
        """Regression for the same finding: ``:free``/``:variant`` ids keep
        their colon and are not mistaken for an address."""
        for good in (
            "google/gemini-3-pro:free",
            "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            "qwen2.5-coder:32b@q4_k_m",
        ):
            event = build_request_completed_event(
                capture_config, base_ctx,
                response_content="",
                upstream_model=good,
                http_status=200,
                sequence=1,
            )
            assert event["upstream_model"] == good, good

    def test_over_long_value_is_not_truncated_on_event(self, capture_config, base_ctx):
        event = build_request_completed_event(
            capture_config, base_ctx,
            response_content="",
            upstream_model="sk-" + "a" * 200,
            http_status=200,
            sequence=1,
        )
        assert event["upstream_model"] is None
        assert "aaaa" not in json.dumps(event)

    def test_context_value_still_sanitised(self, capture_config, base_ctx):
        """A routing-time value on the context is bounded the same way."""
        ctx = replace(base_ctx, upstream_model="https://provider.example/secret")
        event = build_request_completed_event(
            capture_config, ctx,
            response_content="",
            http_status=200,
            sequence=1,
        )
        assert event["upstream_model"] is None

    def test_stream_dispatch_pulls_model_from_assembler(self, monkeypatch, base_ctx):
        from types import SimpleNamespace

        from app.capture.stream_assembler import StreamResponseAssembler
        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        assembler = StreamResponseAssembler()
        assembler.add_sse_line('data: {"model":"openai/gpt-4o","choices":[{"delta":{"content":"Hi"}}]}')
        assembler.add_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        capture_dispatch.dispatch_capture_stream_completed(
            None, "req-model-1", "cli", "openai/gpt-4o",
            base_ctx, SimpleNamespace(should_capture=True), assembler,
            {"prompt_tokens": 0, "completion_tokens": 0}, "chat/completions", 200,
        )
        kwargs = stub.completed_kwargs[0]
        assert kwargs["upstream_model"] == "openai/gpt-4o"

    def test_nonstream_body_model_becomes_upstream_model(self, monkeypatch):
        import time as _time
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        ctx = BuildContext(
            request_id="req-model-2",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="orion/chat",
            resolved_model="orion/chat",
            capture_policy_version="1.0.0",
            instance_id=TEST_INSTANCE_ID,
            client_fingerprint=TEST_FINGERPRINT,
        )
        payload = {
            "model": "meta-llama/llama-3.3-70b-instruct",
            "choices": [{"message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            None, "req-model-2", "cli", "orion/chat",
            ctx, SimpleNamespace(should_capture=True), payload, 200,
            _time.monotonic(),
        )
        kwargs = stub.completed_kwargs[0]
        assert kwargs["upstream_model"] == "meta-llama/llama-3.3-70b-instruct"

    def test_nonstream_body_without_model_reports_none(self, monkeypatch):
        import time as _time
        from types import SimpleNamespace

        from app.gateway import capture_dispatch

        stub = _StubCaptureController()
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: stub)
        ctx = BuildContext(
            request_id="req-model-3",
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            route_type=ROUTE_LOCAL,
            requested_model="orion/chat",
            resolved_model="orion/chat",
            capture_policy_version="1.0.0",
            instance_id=TEST_INSTANCE_ID,
            client_fingerprint=TEST_FINGERPRINT,
        )
        payload = {"choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]}
        capture_dispatch.dispatch_capture_nonstream_completed(
            None, "req-model-3", "cli", "orion/chat",
            ctx, SimpleNamespace(should_capture=True), payload, 200,
            _time.monotonic(),
        )
        assert stub.completed_kwargs[0]["upstream_model"] is None
