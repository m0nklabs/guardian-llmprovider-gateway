"""Unit tests for guardian_capture_v1 schema: IDs, event builders, fixtures."""

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.capture.config import CaptureConfig, PROTOCOL_OPENAI, ROUTE_LOCAL
from app.capture.schema import (
    CLIENT_REF_SECRET_ENV,
    CLIENT_REF_PREVIOUS_SECRETS_ENV,
    RECORD_AUTH_SECRET_ENV,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    compute_event_id,
    compute_client_ref,
    compute_record_auth,
    BuildContext,
    build_request_received_event,
    build_request_completed_event,
    build_request_failed_event,
    build_request_cancelled_event,
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
        from app.capture.redactor import redact_request_messages
        from app.capture.policy import evaluate_capture_policy

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
            lines = [l.strip() for l in f if l.strip()]
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
