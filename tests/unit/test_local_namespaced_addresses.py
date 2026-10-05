"""Unit tests for unified-provider-model phase A: namespaced local addresses.

``{local-provider}/{brand}/{model}`` (and ``{local-provider}/{model}``)
resolve to the same canonical registry entry as the bare alias; the brand
segment is validated strictly against the registry entry's ``brand:`` field.
Bare aliases remain the compat surface and are never renamed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app.gateway.model_discovery as model_discovery
from app.local_inference import models as local_models
from app.local_inference.model_registry import ModelRegistry
from app.proxy.providers import ProviderRegistry

REGISTRY_NAME = "lan-node-local"

REGISTRY_YAML = """\
models:
  qwen3.8-27b:
    path: /tmp/models/qwen3.8-27b.gguf
    brand: qwen
    total_layers: 65
  Phi-4-reasoning-plus:
    path: /tmp/models/phi4.gguf
    brand: phi
    total_layers: 40
  nobrand-model:
    path: /tmp/models/nobrand.gguf
    total_layers: 10
aliases:
  phi4: Phi-4-reasoning-plus
"""

PROVIDER_SETTINGS = f"""\
providers:
  {REGISTRY_NAME}:
    enabled: true
    local: true
    base_url: http://127.0.0.1:11440/v1
"""


@pytest.fixture()
def registry_pair(tmp_path: Path) -> tuple[ProviderRegistry, ModelRegistry, Path]:
    registry_file = tmp_path / f"{REGISTRY_NAME}.settings.yaml"
    registry_file.write_text(REGISTRY_YAML)
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(PROVIDER_SETTINGS)
    provider_registry = ProviderRegistry(settings_path=settings_path)
    model_registry = ModelRegistry(config_path=str(registry_file))
    return provider_registry, model_registry, registry_file


# ── registry brand accessors ─────────────────────────────────────────


class TestRegistryBrandAccessors:
    def test_get_model_brand(self, registry_pair):
        _, model_registry, _ = registry_pair
        assert model_registry.get_model_brand("qwen3.8-27b") == "qwen"
        assert model_registry.get_model_brand("Phi-4-reasoning-plus") == "phi"
        assert model_registry.get_model_brand("nobrand-model") is None
        assert model_registry.get_model_brand("unknown-model") is None

    def test_get_model_brand_rejects_slashed_value(self, registry_pair, tmp_path):
        registry_file = tmp_path / f"{REGISTRY_NAME}.settings.yaml"
        broken = REGISTRY_YAML.replace("    brand: qwen\n", "    brand: qwen/x\n")
        registry_file.write_text(broken)
        model_registry = ModelRegistry(config_path=str(registry_file))
        assert model_registry.get_model_brand("qwen3.8-27b") is None

    def test_namespaced_addresses(self, registry_pair):
        _, model_registry, _ = registry_pair
        addresses = model_registry.namespaced_addresses(REGISTRY_NAME)
        assert addresses == {
            f"{REGISTRY_NAME}/qwen/qwen3.8-27b": "qwen3.8-27b",
            f"{REGISTRY_NAME}/phi/Phi-4-reasoning-plus": "Phi-4-reasoning-plus",
        }


# ── namespaced local address stripping ───────────────────────────────


class TestStripNamespacedLocalAddress:
    def test_three_segment_with_matching_brand(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.strip_namespaced_local_address(f"{REGISTRY_NAME}/qwen/qwen3.8-27b")
            == "qwen3.8-27b"
        )

    def test_three_segment_with_alias(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.strip_namespaced_local_address(f"{REGISTRY_NAME}/phi/phi4")
            == "Phi-4-reasoning-plus"
        )

    def test_two_segment_form(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.strip_namespaced_local_address(f"{REGISTRY_NAME}/qwen3.8-27b")
            == "qwen3.8-27b"
        )

    def test_mismatched_brand_is_not_local(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.strip_namespaced_local_address(f"{REGISTRY_NAME}/gemma/qwen3.8-27b")
            is None
        )

    def test_non_managed_provider_returns_none(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.strip_namespaced_local_address("openrouter/deepseek/deepseek-chat")
            is None
        )

    def test_unknown_model_returns_none(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.strip_namespaced_local_address(f"{REGISTRY_NAME}/qwen/unknown-model")
            is None
        )

    def test_bare_name_returns_none(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert local_models.strip_namespaced_local_address("qwen3.8-27b") is None


# ── inference resolution wiring ──────────────────────────────────────


class TestResolveInferenceModelNamespaced:
    def test_namespaced_resolves_like_bare_alias(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.resolve_inference_model(f"{REGISTRY_NAME}/qwen/qwen3.8-27b", "auto")
            == "qwen3.8-27b"
        )

    def test_bare_alias_still_resolves(self, registry_pair):
        provider_registry, model_registry, _ = registry_pair
        local_models._provider_registry = provider_registry
        local_models._model_manager = model_registry
        assert (
            local_models.resolve_inference_model("phi4", "auto") == "Phi-4-reasoning-plus"
        )


# ── discovery advertisement helper ───────────────────────────────────


class TestLocalNamespacedAddressesHelper:
    def test_prefers_registry_document_provider(self, registry_pair, monkeypatch):
        provider_registry, model_registry, registry_file = registry_pair
        monkeypatch.setattr(model_discovery, "_provider_registry", provider_registry)
        monkeypatch.setattr(model_discovery, "_model_manager", model_registry)
        monkeypatch.setattr(model_discovery, "local_models_file", lambda: registry_file)
        mapping = model_discovery._local_namespaced_addresses()
        assert mapping == {
            "qwen3.8-27b": f"{REGISTRY_NAME}/qwen/qwen3.8-27b",
            "Phi-4-reasoning-plus": f"{REGISTRY_NAME}/phi/Phi-4-reasoning-plus",
        }

    def test_unresolvable_provider_returns_empty(self, registry_pair, monkeypatch, tmp_path):
        _, model_registry, registry_file = registry_pair
        # The registry document stem matches no provider (patched doc name) AND
        # two managed providers exist, so the unique-single-managed fallback is
        # ambiguous too → no namespaced advertisement.
        other = tmp_path / "settings.yaml"
        other.write_text(
            PROVIDER_SETTINGS
            + """\
  other-host:
    enabled: true
    local: true
    base_url: http://127.0.0.1:11441/v1
"""
        )
        provider_registry2 = ProviderRegistry(settings_path=other)
        patched_doc = tmp_path / "unknown-node.settings.yaml"
        patched_doc.write_text(REGISTRY_YAML)
        monkeypatch.setattr(model_discovery, "_provider_registry", provider_registry2)
        monkeypatch.setattr(model_discovery, "_model_manager", model_registry)
        monkeypatch.setattr(model_discovery, "local_models_file", lambda: patched_doc)
        assert model_discovery._local_namespaced_addresses() == {}
