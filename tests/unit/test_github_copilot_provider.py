"""Pin canonical Copilot naming and exclude unrelated bridge aliases."""

from pathlib import Path

import pytest
import yaml

from app.proxy.cloud_catalog import CloudModelCatalog
from app.proxy.providers import ProviderRegistry


@pytest.fixture
def copilot_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_API_KEY", "test-copilot-key")
    root = Path(__file__).resolve().parents[2]
    provider_file = root / "config/providers/github-copilot.settings.yaml"
    provider_config = yaml.safe_load(provider_file.read_text())
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(yaml.safe_dump({"providers": {"github-copilot": provider_config}}))
    registry = ProviderRegistry(settings_path=settings_file)
    catalog = CloudModelCatalog(
        registry,
        cache_file=tmp_path / "cache.json",
        overrides_file=tmp_path / "overrides.yaml",
    )
    return registry, catalog


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
def test_canonical_address_preserves_maker_and_bridge_id(copilot_catalog, model):
    registry, catalog = copilot_catalog
    address = f"github-copilot/openai/{model}"
    provider = registry.get_provider_for_model(address)
    assert provider is not None
    assert provider.name == "github-copilot"
    assert catalog.resolve_cloud_target(address) == ("github-copilot", f"openai/{model}")
    entry = registry.build_model_metadata_entry(address)
    assert entry["id"] == address
    assert entry["provider"] == "github-copilot"


def test_catalog_excludes_non_copilot_bridge_aliases(copilot_catalog):
    _, catalog = copilot_catalog
    catalog._catalogs["github-copilot"] = {
        "fetched_at": 1.0,
        "models": {
            "openai/gpt-6-astra": "openai/gpt-6-astra",
            "openai/gpt-6.1-sol": "openai/gpt-6.1-sol",
            "github-copilot/claude-3-7-sonnet-20250219": "claude-3-7-sonnet-20250219",
            "copilot/gpt-6.1-sol": "copilot/gpt-6.1-sol",
        },
    }
    assert set(catalog.get_models_for_provider("github-copilot")) == {
        "openai/gpt-6-astra", "openai/gpt-6.1-sol",
    }


def test_obsolete_github_provider_is_not_registered(copilot_catalog):
    registry, _ = copilot_catalog
    assert "github" not in registry._providers
    assert registry.get_provider_for_model("github/copilot/gpt-6.1-sol") is None
    root = Path(__file__).resolve().parents[2]
    assert not (root / "config/providers/github.settings.yaml").exists()
