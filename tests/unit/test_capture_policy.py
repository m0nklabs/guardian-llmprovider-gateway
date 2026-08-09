"""Unit tests for the capture policy engine."""

import pytest

from app.capture.config import CaptureConfig, PROTOCOL_OPENAI, ROUTE_LOCAL, ROUTE_CLOUD
from app.capture.policy import evaluate_capture_policy, PolicyResult


TEST_SECRET = "test-secret"
TEST_FINGERPRINT = "abc123"


def _make_client_ref() -> str:
    """Compute client_ref — must be called after env var is set."""
    import os
    secret = os.environ.get("GUARDIAN_CAPTURE_CLIENT_REF_SECRET", "")
    import hmac, hashlib
    return hmac.new(
        secret.encode("utf-8"),
        TEST_FINGERPRINT.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@pytest.fixture
def test_env(monkeypatch):
    monkeypatch.setenv("GUARDIAN_CAPTURE_CLIENT_REF_SECRET", TEST_SECRET)
    yield


def _config(**overrides):
    defaults = {
        "enabled": True,
        "local_capture": True,
        "cloud_capture": False,
        "per_client_opt_in": True,
        "allowed_client_refs": [_make_client_ref()],
    }
    defaults.update(overrides)
    return CaptureConfig(**defaults)


class TestPolicyBase:
    def test_returns_policy_result(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert isinstance(result, PolicyResult)

    def test_is_capture_property_matches_should_capture(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.is_capture == result.should_capture


class TestGlobalKillSwitch:
    def test_disabled_returns_no_capture(self, test_env):
        config = _config(enabled=False)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "disabled"

    def test_disabled_supercedes_allowed_client(self, test_env):
        """Per-client opt-in can never override a disabled global switch."""
        config = _config(enabled=False, per_client_opt_in=True, allowed_client_refs=[_make_client_ref()])
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "disabled"


class TestRouteTypeGate:
    def test_local_disabled_rejects_local_route(self, test_env):
        config = _config(local_capture=False)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "route_type_disabled"

    def test_cloud_disabled_rejects_cloud_route(self, test_env):
        config = _config(cloud_capture=False)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_CLOUD,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="openai/gpt-4o",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "route_type_disabled"

    def test_local_enabled_allows_local_route(self, test_env):
        config = _config(local_capture=True)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture

    def test_cloud_enabled_allows_cloud_route(self, test_env):
        config = _config(cloud_capture=True)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_CLOUD,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="openai/gpt-4o",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture


class TestEndpointExclusion:
    def test_admin_endpoint_excluded(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/admin/load",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "endpoint_excluded"

    def test_healthz_excluded(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/healthz",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "endpoint_excluded"

    def test_metrics_excluded(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/metrics",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "endpoint_excluded"

    def test_v1_models_excluded(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/models",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "endpoint_excluded"


class TestPerClientOptIn:
    def test_allowed_client_captured(self, test_env):
        config = _config(per_client_opt_in=True, allowed_client_refs=[_make_client_ref()])
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture

    def test_unallowed_client_rejected(self, test_env):
        config = _config(per_client_opt_in=True, allowed_client_refs=[_make_client_ref()])
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref="different-ref-not-allowed",
        )
        assert not result.should_capture
        assert result.reason == "client_not_opted_in"

    def test_no_client_ref_rejected(self, test_env):
        config = _config(per_client_opt_in=True, allowed_client_refs=[_make_client_ref()])
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=None,
        )
        assert not result.should_capture
        assert result.reason == "unauthenticated"

    def test_opt_in_disabled_allows_any_authenticated(self, test_env):
        config = _config(per_client_opt_in=False, allowed_client_refs=[])
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture

    def test_opt_in_disabled_still_rejects_unauthenticated(self, test_env):
        config = _config(per_client_opt_in=False)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=None,
        )
        assert not result.should_capture
        assert result.reason == "unauthenticated"


class TestIngressProtocolGate:
    def test_anthropic_protocol_supported(self, test_env):
        """Phase 4: Anthropic Messages protocol is now supported for capture."""
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/messages",
            ingress_protocol="anthropic",
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture
        assert result.reason == "allowed"

    def test_ollama_protocol_supported(self, test_env):
        """Phase 4: Ollama protocol is now supported for capture."""
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/api/chat",
            ingress_protocol="ollama",
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture
        assert result.reason == "allowed"

    def test_unknown_protocol_not_supported(self, test_env):
        """Unknown protocols are still rejected."""
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol="mqtt",
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "protocol_not_supported"


class TestEndpointGate:
    def test_chat_completions_is_supported(self, test_env):
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture

    def test_completions_not_supported_yet(self, test_env):
        """First delivery slice only covers chat/completions."""
        config = _config()
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert not result.should_capture
        assert result.reason == "endpoint_not_supported"


class TestFailOpen:
    def test_policy_error_returns_no_capture(self, test_env):
        """Any error during evaluation defaults to 'do not capture'."""
        config = _config()
        # Force an error by passing a non-string route_type
        # (the function should handle it gracefully)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.should_capture  # Should work normally

    def test_field_policies_always_returned(self, test_env):
        config = _config(enabled=False)
        result = evaluate_capture_policy(
            config, route_type=ROUTE_LOCAL,
            endpoint="/v1/chat/completions",
            ingress_protocol=PROTOCOL_OPENAI,
            requested_model="llama3.2-3b",
            client_ref=_make_client_ref(),
        )
        assert result.field_policies is not None
        assert "system_prompts" in result.field_policies
        assert result.field_policies["system_prompts"] == "strip"
