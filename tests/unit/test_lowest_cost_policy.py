"""Mocked routing/config contract: no billable upstream probes."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from app.cloud_inference import routing
from app.proxy.providers import CloudProvider, ProviderRegistry


@pytest.fixture
def catalog(monkeypatch):
    catalog = SimpleNamespace(get_override=Mock(return_value={}))
    monkeypatch.setattr(routing, "_cloud_catalog", catalog)
    monkeypatch.setattr(routing, "_adapt_openai_reasoning_params", lambda p, m, b: b)
    return catalog


def provider(**kwargs):
    return CloudProvider(name="openrouter", base_url="https://openrouter.ai/api/v1",
                         api_key="mock", service_tier_policy="lowest_cost", **kwargs)


def prepare(p=None, model="brand/model", path="chat/completions", **body):
    return routing.prepare_cloud_candidate_request(
        p or provider(), model, path,
        {"model": "openrouter/brand/client-alias", "messages": [], **body}, "fingerprint",
    )


@pytest.mark.parametrize("path", ["chat/completions", "messages", "responses"])
@pytest.mark.parametrize("model,expected", [
    ("brand/model", "brand/model:floor"),
    ("brand/model:floor", "brand/model:floor"),
    ("brand/model:free", "brand/model:free"),
])
def test_wire_model_identity_protocol_and_legacy_flex(catalog, path, model, expected):
    body = {"model": "openrouter/brand/client-alias", "messages": [], "input": "hi"}
    original = dict(body)
    effective, out, wire, translated = routing.prepare_cloud_candidate_request(
        provider(service_tier="flex"), model, path, body, "fingerprint",
    )
    assert out["model"] == expected
    assert "service_tier" not in out
    assert json.loads(wire) == out
    assert effective == ("chat/completions" if path == "responses" else path)
    assert not translated  # OpenRouter speaks native Messages.
    assert body == original
    catalog.get_override.assert_called_once_with(model)


@pytest.mark.parametrize("body", [
    {"service_tier": tier} for tier in ["flex", "default", "priority", "ultrafast", None]
] + [
    {"speed": "fast"}, {"models": ["paid/fallback"]}, {"fallbacks": ["paid/fallback"]},
    {"route": "fallback"}, {"preset": "foo"}, {"plugins": [{"id": "auto-router"}]},
    {"provider": "openai"}, {"provider": ["openai"]},
    {"provider": {"order": ["openai"]}}, {"provider": {"sort": "price"}},
    {"provider": {"sort": {"by": "price", "partition": "none"}}},
    {"provider": {"preferred_min_throughput": 10}},
    {"provider": {"preferred_max_latency": 1}},
    {"provider": {"allow_fallbacks": False}}, {"provider": {"unknown": True}},
    {"provider": {"only": ["openai/ultrafast"]}},
    {"provider": {"only": ["deepinfra/turbo"]}},
])
@pytest.mark.parametrize("path", ["chat/completions", "messages", "responses"])
def test_raw_conflicts_rejected_before_translation(catalog, body, path):
    with pytest.raises(routing.CloudPolicyRequestError, match="lowest_cost"):
        prepare(path=path, input="hi", **body)


@pytest.mark.parametrize("defaults", [
    {"service_tier": "flex"}, {"speed": "fast"},
    {"provider": {"order": ["openai"]}}, {"models": ["other/model"]},
])
def test_model_defaults_cannot_bypass_policy(catalog, defaults):
    catalog.get_override.return_value = defaults
    with pytest.raises(routing.CloudPolicyRequestError):
        prepare()


@pytest.mark.parametrize("path", ["chat/completions", "messages", "responses"])
def test_preserves_eligibility_privacy_and_price_constraints(catalog, path):
    restrictions = {"only": ["openai", "google-vertex"], "ignore": ["other/europe"],
                    "data_collection": "deny", "zdr": True, "max_price": {"prompt": 0.1},
                    "require_parameters": True, "quantizations": ["fp8"]}
    catalog.get_override.return_value = {"provider": {"only": ["different"]}}
    _, out, _, _ = prepare(path=path, provider=restrictions, temperature=0.3, input="hi")
    assert out["provider"] == restrictions
    assert out["temperature"] == 0.3


@pytest.mark.parametrize("model", ["brand/model:nitro", "brand/model:thinking",
    "brand/model:online", "brand/model:free:floor", "brand/model:floor:floor",
    "brand/model:unknown", "openrouter/auto", "@preset"])
def test_incompatible_or_unknown_variants_never_rewrite_model(catalog, model):
    with pytest.raises(routing.CloudPolicyRequestError, match="concrete model"):
        prepare(model=model)


@pytest.mark.parametrize("path", ["embeddings", "completions", "audio/speech"])
def test_enabled_policy_fails_closed_on_unsupported_endpoints(catalog, path):
    with pytest.raises(routing.CloudPolicyRequestError, match="endpoint"):
        prepare(path=path)


@pytest.mark.parametrize("name,path,tier", [
    ("openrouter", "chat/completions", "priority"),
    ("openrouter", "messages", "standard_only"),
    ("nvidia", "embeddings", "custom"),
])
def test_disabled_policy_preserves_native_values_and_other_providers(catalog, name, path, tier):
    p = CloudProvider(name=name, base_url="https://example.test", api_key="mock")
    _, out, _, _ = prepare(p=p, path=path, service_tier=tier)
    assert out["service_tier"] == tier
    assert out["model"] == "brand/model"


@pytest.mark.parametrize("name,policy,expected", [
    ("openrouter", "lowest_cost", "lowest_cost"),
    ("openrouter", "disabled", "disabled"),
    ("nvidia", "disabled", "disabled"),
    ("openrouter", "typo", None), ("openrouter", None, None),
    ("openrouter", {}, None), ("anthropic", "lowest_cost", None),
])
def test_explicit_configuration_validation(tmp_path, name, policy, expected):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"providers": {name: {
        "base_url": "https://example.test", "api_key": "mock", "service_tier_policy": policy,
    }}}))
    if expected is None:
        with pytest.raises(ValueError, match="service_tier_policy"):
            ProviderRegistry(path)
    else:
        assert ProviderRegistry(path)._providers[name].service_tier_policy == expected


def test_policy_disabled_by_default(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("providers:\n  openrouter:\n    api_key: mock\n")
    assert ProviderRegistry(path)._providers["openrouter"].service_tier_policy == "disabled"


def test_programmatically_misconfigured_policy_also_fails_closed(catalog):
    p = CloudProvider(name="anthropic", base_url="https://example.test", api_key="mock",
                      service_tier_policy="lowest_cost")
    with pytest.raises(routing.CloudPolicyRequestError, match="unsupported provider"):
        prepare(p=p)


@pytest.mark.parametrize("late_error", ["service_tier_policy: typo", "timeout_seconds: not-a-float"])
def test_failed_reload_preserves_enforced_policy_and_all_registry_maps(tmp_path, late_error):
    path = tmp_path / "settings.yaml"
    path.write_text("""context_overrides:
  brand/model: 12345
providers:
  openrouter:
    api_key: mock
    service_tier_policy: lowest_cost
    models: [brand/model]
    model_prefixes: [brand/]
""")
    registry = ProviderRegistry(path)
    old_provider = registry._providers["openrouter"]
    old_state = (registry._providers, registry._model_to_provider,
                 registry._prefix_to_provider, registry._context_overrides)
    path.write_text(f"""context_overrides:
  brand/model: 999
providers:
  openrouter:
    api_key: mock
    service_tier_policy: disabled
    models: [new/model]
    model_prefixes: [new/]
  later:
    api_key: mock
    {late_error}
""")
    with pytest.raises(ValueError):
        registry.reload()
    assert all(old is current for old, current in zip(old_state, (
        registry._providers, registry._model_to_provider,
        registry._prefix_to_provider, registry._context_overrides)))
    assert registry._providers["openrouter"] is old_provider
    assert old_provider.service_tier_policy == "lowest_cost"
    assert registry._model_to_provider["brand/model"] is old_provider
    assert registry._prefix_to_provider == [("brand/", old_provider)]
    assert registry._context_overrides == {"brand/model": 12345}
