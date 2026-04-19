"""Unit tests for DynamicScaler — adaptive reasoning budget & max_tokens."""

import copy
import pytest
from unittest.mock import patch
from app.proxy.scaler import DynamicScaler, _DEFAULT_CONFIG


@pytest.fixture
def scaler():
    """Create a fresh DynamicScaler with default config."""
    with patch("app.proxy.scaler._load_scaler_config", return_value=copy.deepcopy(_DEFAULT_CONFIG)):
        s = DynamicScaler()
    return s


@pytest.fixture
def disabled_scaler():
    """Scaler with enabled=False."""
    cfg = dict(_DEFAULT_CONFIG)
    cfg["enabled"] = False
    with patch("app.proxy.scaler._load_scaler_config", return_value=cfg):
        s = DynamicScaler()
        # Prevent reload_config from re-enabling via real settings.yaml
        s.reload_config = lambda: None
    return s


# ---------------------------------------------------------------
# Complexity classification
# ---------------------------------------------------------------

class TestComplexityClassification:

    def test_trivial_prompt(self, scaler):
        messages = [{"role": "user", "content": "hi"}]
        profile, _ = scaler._classify_complexity(messages)
        assert profile == "trivial"

    def test_simple_prompt(self, scaler):
        messages = [{"role": "user", "content": "x" * 400}]
        profile, _ = scaler._classify_complexity(messages)
        assert profile == "simple"

    def test_moderate_prompt(self, scaler):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "x" * 2000},
            {"role": "assistant", "content": "y" * 500},
            {"role": "user", "content": "z" * 500},
        ]
        profile, complexity = scaler._classify_complexity(messages)
        assert profile == "moderate"
        assert complexity["has_system"] is True

    def test_complex_prompt(self, scaler):
        messages = [{"role": "user", "content": "x" * 10000}]
        profile, _ = scaler._classify_complexity(messages)
        assert profile == "complex"

    def test_deep_prompt(self, scaler):
        messages = [{"role": "user", "content": "x" * 20000}]
        profile, _ = scaler._classify_complexity(messages)
        assert profile == "deep"

    def test_many_messages_escalates(self, scaler):
        # 15 short messages should push past "simple" into "moderate" or higher
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(15)]
        profile, complexity = scaler._classify_complexity(messages)
        assert profile in ("moderate", "complex")
        assert complexity["num_messages"] == 15

    def test_multimodal_image(self, scaler):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                ],
            }
        ]
        profile, complexity = scaler._classify_complexity(messages)
        assert complexity["has_images"] is True
        # Image adds ~2000 chars of complexity → should be at least "moderate"
        assert profile in ("moderate", "complex", "deep")

    def test_empty_messages(self, scaler):
        profile, complexity = scaler._classify_complexity([])
        assert profile == "trivial"
        assert complexity["total_chars"] == 0


# ---------------------------------------------------------------
# Request scaling
# ---------------------------------------------------------------

class TestScaleRequest:

    def test_injects_thinking_budget_and_max_tokens(self, scaler):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        result = scaler.scale_request(body)
        assert "thinking_budget_tokens" in result
        assert "max_tokens" in result

    def test_respects_explicit_thinking_budget(self, scaler):
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "thinking_budget_tokens": 9999,
        }
        result = scaler.scale_request(body)
        assert result["thinking_budget_tokens"] == 9999  # Untouched

    def test_respects_explicit_max_tokens(self, scaler):
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 42000,
        }
        result = scaler.scale_request(body)
        assert result["max_tokens"] == 42000  # Untouched

    def test_both_explicit_skips_scaler(self, scaler):
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "thinking_budget_tokens": 100,
            "max_tokens": 200,
        }
        result = scaler.scale_request(body)
        assert result["thinking_budget_tokens"] == 100
        assert result["max_tokens"] == 200

    def test_disabled_scaler_no_changes(self, disabled_scaler):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        result = disabled_scaler.scale_request(body)
        assert "thinking_budget_tokens" not in result
        assert "max_tokens" not in result

    def test_deep_prompt_gets_unlimited_thinking(self, scaler):
        body = {"messages": [{"role": "user", "content": "x" * 20000}]}
        result = scaler.scale_request(body)
        # Deep profile has thinking_budget=-1 → should NOT inject thinking_budget_tokens
        assert "thinking_budget_tokens" not in result
        assert "max_tokens" in result


# ---------------------------------------------------------------
# Queue pressure
# ---------------------------------------------------------------

class TestQueuePressure:

    def test_no_pressure(self, scaler):
        body = {"messages": [{"role": "user", "content": "x" * 500}]}
        result = scaler.scale_request(body, waiting_count=0)
        base_tokens = result["max_tokens"]
        # With 0 waiting, should get full profile values
        assert base_tokens > 0

    def test_moderate_pressure_reduces(self, scaler):
        # Simple prompt → thinking_budget=1024, max_tokens=4096
        body1 = {"messages": [{"role": "user", "content": "x" * 500}]}
        result_calm = scaler.scale_request(dict(body1), waiting_count=0)

        body2 = {"messages": [{"role": "user", "content": "x" * 500}]}
        result_busy = scaler.scale_request(dict(body2), waiting_count=2)

        if "thinking_budget_tokens" in result_calm and "thinking_budget_tokens" in result_busy:
            assert result_busy["thinking_budget_tokens"] <= result_calm["thinking_budget_tokens"]
        assert result_busy["max_tokens"] <= result_calm["max_tokens"]

    def test_heavy_pressure_reduces_more(self, scaler):
        body1 = {"messages": [{"role": "user", "content": "x" * 500}]}
        result_calm = scaler.scale_request(dict(body1), waiting_count=0)

        body2 = {"messages": [{"role": "user", "content": "x" * 500}]}
        result_heavy = scaler.scale_request(dict(body2), waiting_count=5)

        assert result_heavy["max_tokens"] < result_calm["max_tokens"]

    def test_pressure_has_floor(self, scaler):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        result = scaler.scale_request(body, waiting_count=100)
        assert result["max_tokens"] >= 512
        if "thinking_budget_tokens" in result:
            assert result["thinking_budget_tokens"] >= 128

    def test_unlimited_thinking_stays_unlimited_under_pressure(self, scaler):
        """Deep profile thinking_budget=-1 must stay unlimited even under queue pressure."""
        body = {"messages": [{"role": "user", "content": "x" * 20000}]}
        result = scaler.scale_request(body, waiting_count=10)
        # Should NOT inject thinking_budget_tokens for unlimited profiles
        assert "thinking_budget_tokens" not in result


# ---------------------------------------------------------------
# Config reload
# ---------------------------------------------------------------

class TestConfigReload:

    def test_reload_is_noop_without_file_change(self, scaler):
        # Calling reload multiple times shouldn't crash or change behavior
        scaler.reload_config()
        scaler.reload_config()
        assert scaler.config["enabled"] is True

    def test_default_profiles_present(self, scaler):
        profiles = scaler.config["profiles"]
        assert "trivial" in profiles
        assert "simple" in profiles
        assert "moderate" in profiles
        assert "complex" in profiles
        assert "deep" in profiles


# ---------------------------------------------------------------
# Pressure label
# ---------------------------------------------------------------

class TestPressureLabel:

    def test_none(self, scaler):
        assert scaler._pressure_label(0) == "none"
        assert scaler._pressure_label(1) == "none"

    def test_moderate(self, scaler):
        assert scaler._pressure_label(2) == "moderate"
        assert scaler._pressure_label(3) == "moderate"

    def test_heavy(self, scaler):
        assert scaler._pressure_label(4) == "heavy"
        assert scaler._pressure_label(100) == "heavy"


class TestUpdateConfig:
    """Test update_config and reset_config API methods."""

    def test_update_enabled(self, scaler):
        assert scaler.config["enabled"] is True
        scaler.update_config({"enabled": False}, persist=False)
        assert scaler.config["enabled"] is False

    def test_update_profile_partial(self, scaler):
        scaler.update_config(
            {"profiles": {"trivial": {"thinking_budget": 512}}},
            persist=False,
        )
        assert scaler.config["profiles"]["trivial"]["thinking_budget"] == 512
        # Other trivial fields unchanged
        assert scaler.config["profiles"]["trivial"]["max_chars"] == 200

    def test_update_queue_pressure(self, scaler):
        scaler.update_config(
            {"queue_pressure": {"heavy_threshold": 8}},
            persist=False,
        )
        assert scaler.config["queue_pressure"]["heavy_threshold"] == 8
        # Other fields unchanged
        assert scaler.config["queue_pressure"]["moderate_threshold"] == 2

    def test_reset_config(self, scaler):
        scaler.update_config({"enabled": False}, persist=False)
        assert scaler.config["enabled"] is False
        scaler.reset_config(persist=False)
        assert scaler.config["enabled"] is True

    def test_get_config_is_deep_copy(self, scaler):
        cfg = scaler.get_config()
        cfg["enabled"] = False
        cfg["profiles"]["trivial"]["thinking_budget"] = 9999
        # Original unchanged
        assert scaler.config["enabled"] is True
        assert scaler.config["profiles"]["trivial"]["thinking_budget"] == 256

    def test_update_returns_full_config(self, scaler):
        result = scaler.update_config({"enabled": False}, persist=False)
        assert result["enabled"] is False
        assert "profiles" in result
        assert "queue_pressure" in result


class TestAdvisoryRecommend:
    """Test the advisory pattern: classify + pressure → recommendation."""

    def test_recommend_trivial(self, scaler):
        messages = [{"role": "user", "content": "hi"}]
        profile, complexity = scaler._classify_complexity(messages)
        p = scaler.config["profiles"][profile]
        thinking, max_tokens = scaler._apply_queue_pressure(
            p["thinking_budget"], p["max_tokens"], waiting_count=0
        )
        assert profile == "trivial"
        assert thinking == 256
        assert max_tokens == 1024

    def test_recommend_under_pressure(self, scaler):
        messages = [{"role": "user", "content": "x" * 500}]
        profile, _ = scaler._classify_complexity(messages)
        p = scaler.config["profiles"][profile]
        thinking_calm, tokens_calm = scaler._apply_queue_pressure(
            p["thinking_budget"], p["max_tokens"], waiting_count=0
        )
        thinking_busy, tokens_busy = scaler._apply_queue_pressure(
            p["thinking_budget"], p["max_tokens"], waiting_count=4
        )
        assert thinking_busy < thinking_calm
        assert tokens_busy < tokens_calm

    def test_recommend_deep_keeps_unlimited(self, scaler):
        messages = [{"role": "user", "content": "x" * 20000}]
        profile, _ = scaler._classify_complexity(messages)
        p = scaler.config["profiles"][profile]
        thinking, _ = scaler._apply_queue_pressure(
            p["thinking_budget"], p["max_tokens"], waiting_count=10
        )
        assert profile == "deep"
        assert thinking == -1  # unlimited stays unlimited
