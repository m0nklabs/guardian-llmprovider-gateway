"""Nemotron thinking-toggle mapping pins (operator feature, 2026-09-09).

Nemotron hybrid-reasoning models ignore ``reasoning_effort`` and think by
default, burning the completion budget on visible reasoning until nothing is
left for the answer (live A/B: 400 reasoning tokens + finish=length without
the toggle; a clean answer with finish=stop with it).  The mapping resolves:
client-explicit chat_template_kwargs wins, then the provider-config
``models:`` override, then reasoning_effort (low/none → thinking off);
otherwise untouched — nemotron keeps reasoning by default.
"""

import pytest

from app.cloud_inference.routing import adapt_nemotron_thinking_params


PROVIDER_NV = pytest.param  # placeholder readability
from app.proxy.providers import CloudProvider

_NV = CloudProvider(name="nvidia", base_url="https://integrate.api.nvidia.com/v1", api_key="k")
_OR = CloudProvider(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="k")
NEMOTRON = "nemotron-3.5-lightning-20260807:free"
OTHER = "z-ai/glm-5.3-flash"


class TestEffortMapping:
    def test_effort_low_disables_thinking(self):
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_thinking_params(_NV, NEMOTRON, body, {})
        assert out["chat_template_kwargs"]["enable_thinking"] is False

    def test_effort_none_disables_thinking(self):
        body = {"messages": [], "reasoning_effort": "none"}
        out = adapt_nemotron_thinking_params(_NV, NEMOTRON, body, {})
        assert out["chat_template_kwargs"]["enable_thinking"] is False

    def test_effort_high_leaves_default_thinking_on(self):
        body = {"messages": [], "reasoning_effort": "high"}
        out = adapt_nemotron_thinking_params(_NV, NEMOTRON, body, {})
        assert "chat_template_kwargs" not in out

    def test_no_effort_leaves_default_thinking_on(self):
        """Default request → nemotron KEEPS reasoning (model default ON)."""
        body = {"messages": []}
        out = adapt_nemotron_thinking_params(_NV, NEMOTRON, body, {})
        assert "chat_template_kwargs" not in out


class TestResolutionOrder:
    def test_client_explicit_wins_over_effort(self):
        body = {
            "messages": [],
            "reasoning_effort": "low",
            "chat_template_kwargs": {"enable_thinking": True},
        }
        out = adapt_nemotron_thinking_params(_NV, NEMOTRON, body, {})
        assert out["chat_template_kwargs"]["enable_thinking"] is True

    def test_config_override_wins_over_effort(self):
        body = {"messages": [], "reasoning_effort": "high"}
        out = adapt_nemotron_thinking_params(
            _NV, NEMOTRON, body, {"enable_thinking": False}
        )
        assert out["chat_template_kwargs"]["enable_thinking"] is False

    def test_config_override_true_forces_thinking_on(self):
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_thinking_params(
            _NV, NEMOTRON, body, {"enable_thinking": True}
        )
        assert out["chat_template_kwargs"]["enable_thinking"] is True


class TestScope:
    def test_non_nemotron_model_untouched(self):
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_thinking_params(_OR, OTHER, body, {})
        assert out is body

    def test_openrouter_provider_nemotron_is_mapped(self):
        """The free failover group routes nemotron through openrouter — the
        mapping must be provider-agnostic on the model id."""
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_thinking_params(_OR, "nvidia/" + NEMOTRON, body, {})
        assert out["chat_template_kwargs"]["enable_thinking"] is False

    def test_existing_other_template_kwargs_preserved(self):
        body = {"messages": [], "reasoning_effort": "low", "chat_template_kwargs": {"foo": 1}}
        out = adapt_nemotron_thinking_params(_NV, NEMOTRON, body, {})
        assert out["chat_template_kwargs"]["foo"] == 1
        assert out["chat_template_kwargs"]["enable_thinking"] is False
