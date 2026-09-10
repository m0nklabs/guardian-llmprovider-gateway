"""Nemotron reasoning-intent dialect pins (operator feature, 2026-09-10).

Nemotron 3.5 hybrid-reasoning models think by default and IGNORE the
OpenAI-style ``reasoning_effort`` through OpenRouter.  When the budget runs
out mid-thinking the serving-side reasoning parser never sees the close
signal and duplicates the thinking text into ``content`` (live proof:
content_len == reasoning_len, finish=length) — the reported "bad outputs".

Live dialect evidence (2026-09-10, 14 instrumented calls against
nemotron-3.5-lightning:free via openrouter):
- ``chat_template_kwargs: {"enable_thinking": false}`` → NOT honored
  (reasoning_tokens > 0 in every call);
- ``reasoning: {"enabled": false}`` → ``reasoning_tokens=0`` (H1);
- ``reasoning: {"max_tokens": 100}`` → thinking capped, clean answer (H3).

So the adapter speaks each provider's dialect: OpenRouter gets the unified
reasoning object; NVIDIA's own NIM endpoint keeps the documented
chat-template toggle.
"""

from app.cloud_inference.routing import adapt_nemotron_reasoning_intent
from app.proxy.providers import CloudProvider

_NV = CloudProvider(name="nvidia", base_url="https://integrate.api.nvidia.com/v1", api_key="k")
_OR = CloudProvider(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="k")
NEMOTRON = "nvidia/nemotron-3.5-lightning-20260807:free"
OTHER = "z-ai/glm-5.3-flash"


class TestOpenRouterDialect:
    def test_effort_low_maps_to_unified_reasoning_disabled(self):
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert out["reasoning"]["enabled"] is False
        assert "chat_template_kwargs" not in out

    def test_effort_none_maps_to_unified_reasoning_disabled(self):
        body = {"messages": [], "reasoning_effort": "none"}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert out["reasoning"]["enabled"] is False

    def test_no_effort_keeps_default_thinking_on(self):
        """Default request → nemotron KEEPS reasoning (model default ON)."""
        body = {"messages": []}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert "reasoning" not in out
        assert "chat_template_kwargs" not in out

    def test_unified_effort_low_is_translated(self):
        """Client sent OpenRouter's own dialect with effort=low, but OpenRouter
        maps effort to nothing on nvidia models (live H2: 417 reasoning
        tokens) — translate to the knob that provably fires."""
        body = {"messages": [], "reasoning": {"effort": "low"}}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert out["reasoning"]["enabled"] is False

    def test_client_enabled_true_wins(self):
        body = {"messages": [], "reasoning": {"enabled": True}, "reasoning_effort": "low"}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert out["reasoning"]["enabled"] is True

    def test_existing_reasoning_keys_preserved(self):
        body = {"messages": [], "reasoning": {"exclude": True}, "reasoning_effort": "low"}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert out["reasoning"]["exclude"] is True
        assert out["reasoning"]["enabled"] is False

    def test_effort_high_untouched(self):
        body = {"messages": [], "reasoning_effort": "high"}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {})
        assert "reasoning" not in out


class TestNvidiaDirectDialect:
    def test_effort_low_uses_chat_template_toggle(self):
        """NVIDIA's own NIM documents enable_thinking via chat template —
        that dialect stays for the direct nvidia provider."""
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_reasoning_intent(_NV, "nemotron-3.5-lightning", body, {})
        assert out["chat_template_kwargs"]["enable_thinking"] is False


class TestConfigOverride:
    def test_override_false_applies_unified_dialect(self):
        body = {"messages": []}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {"enable_thinking": False})
        assert out["reasoning"]["enabled"] is False

    def test_override_false_applies_nvidia_dialect(self):
        body = {"messages": []}
        out = adapt_nemotron_reasoning_intent(_NV, "nemotron-3.5-lightning", body, {"enable_thinking": False})
        assert out["chat_template_kwargs"]["enable_thinking"] is False

    def test_client_enabled_true_wins_over_override(self):
        body = {"messages": [], "reasoning": {"enabled": True}}
        out = adapt_nemotron_reasoning_intent(_OR, NEMOTRON, body, {"enable_thinking": False})
        assert out["reasoning"]["enabled"] is True


class TestScope:
    def test_non_nemotron_model_untouched(self):
        body = {"messages": [], "reasoning_effort": "low"}
        out = adapt_nemotron_reasoning_intent(_OR, OTHER, body, {})
        assert out is body
