"""Unit tests for bare-name provider resolution with the catalog probe (G3).

G3 incident (2026-09-01/02): both nvidia and openrouter declared the
``z-ai/`` ``model_prefixes`` namespace, so a bare ``z-ai/glm-5.3-flash``
resolved to the FIRST declaration (nvidia) — which 404s because NVIDIA's
free-tier ``catalog_allowlist`` contains no z-ai model. The fix makes
:meth:`ProviderRegistry._get_configured_provider_for_model` collect ALL
prefix candidates and disambiguate with an injected catalog probe
(:meth:`ProviderRegistry.set_catalog_probe`) that answers whether a
provider's live serving catalog actually contains the model. The probe
only disambiguates on positive evidence; it never narrows to ``None``.
"""

import textwrap
from pathlib import Path

import pytest
import yaml

from app.paths import provider_settings_file
from app.proxy.providers import ProviderRegistry

# ── Fixtures ───────────────────────────────────────────────────────────


SHARED_PREFIX_PROVIDERS_YAML = """\
providers:
  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: nvapi-test-key
    timeout_seconds: 600
    model_prefixes:
      - nvidia/
      - z-ai/
    models:
      - nvidia/llama-3.1-nemotron-70b-instruct
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    timeout_seconds: 300
    model_prefixes:
      - z-ai/
      - anthropic/
    models:
      - z-ai/glm-5.2
"""

SINGLE_CLAIMANT_PROVIDERS_YAML = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    timeout_seconds: 300
    model_prefixes:
      - z-ai/
"""


def _write_settings(tmp_path: Path, content: str) -> Path:
    """Write a settings.yaml snippet to a temp file and return its path."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(textwrap.dedent(content))
    return settings


def _probe_from_catalogs(catalogs: dict[str, set[str]]):
    """Build a probe answering from ``{provider_name: {model_id, ...}}`` sets."""

    def _probe(canonical_model_id: str, provider_name: str) -> bool:
        return canonical_model_id in catalogs.get(provider_name, set())

    return _probe


@pytest.fixture
def shared_prefix_registry(tmp_path: Path) -> ProviderRegistry:
    """Registry where nvidia AND openrouter both claim ``z-ai/`` (G3 shape).

    Declaration order mirrors the production directory scan: nvidia is
    declared before openrouter, so legacy first-match resolution picks
    nvidia.
    """
    return ProviderRegistry(settings_path=_write_settings(tmp_path, SHARED_PREFIX_PROVIDERS_YAML))


# ── Bare-name disambiguation via the catalog probe ─────────────────────


class TestBareNameCatalogDisambiguation:
    def test_bare_name_prefers_provider_whose_catalog_confirms(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """G3 core case: both claimants declare ``z-ai/``; only openrouter's
        catalog really contains the model -> openrouter wins over the
        declaration-order first candidate (nvidia)."""
        shared_prefix_registry.set_catalog_probe(
            _probe_from_catalogs({"openrouter": {"z-ai/glm-5.3-flash"}})
        )
        provider = shared_prefix_registry.get_provider_for_model("z-ai/glm-5.3-flash")
        assert provider is not None
        assert provider.name == "openrouter"

    def test_bare_name_falls_back_to_declaration_order_when_no_catalog_confirms(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """Probe injected but confirms NO candidate (model not cached yet /
        in no catalog) -> declaration-order first candidate (back-compat)."""
        shared_prefix_registry.set_catalog_probe(_probe_from_catalogs({}))
        provider = shared_prefix_registry.get_provider_for_model("z-ai/glm-5.3-flash")
        assert provider is not None
        assert provider.name == "nvidia"

    def test_no_probe_returns_declaration_order_first(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """No probe injected (cold start / not wired yet) -> legacy behavior:
        first declaration-order prefix candidate."""
        provider = shared_prefix_registry.get_provider_for_model("z-ai/glm-5.3-flash")
        assert provider is not None
        assert provider.name == "nvidia"

    def test_exact_entry_beats_prefix_and_catalog(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """An exact ``models`` entry always wins — even when the probe would
        confirm a different prefix claimant's catalog."""
        shared_prefix_registry.set_catalog_probe(
            _probe_from_catalogs({"nvidia": {"z-ai/glm-5.2"}})
        )
        provider = shared_prefix_registry.get_provider_for_model("z-ai/glm-5.2")
        assert provider is not None
        assert provider.name == "openrouter"

    def test_single_claimant_with_probe_confirms(self, tmp_path: Path):
        """One claimant + probe confirmation -> that provider."""
        reg = ProviderRegistry(
            settings_path=_write_settings(tmp_path, SINGLE_CLAIMANT_PROVIDERS_YAML)
        )
        reg.set_catalog_probe(_probe_from_catalogs({"openrouter": {"z-ai/glm-5.3-flash"}}))
        provider = reg.get_provider_for_model("z-ai/glm-5.3-flash")
        assert provider is not None
        assert provider.name == "openrouter"

    def test_raising_probe_falls_back_to_declaration_order(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """A probe that raises counts as "no evidence": resolution fails safe
        to the declaration-order first candidate instead of erroring."""

        def _broken_probe(canonical_model_id: str, provider_name: str) -> bool:
            raise RuntimeError("catalog unavailable")

        shared_prefix_registry.set_catalog_probe(_broken_probe)
        provider = shared_prefix_registry.get_provider_for_model("z-ai/glm-5.3-flash")
        assert provider is not None
        assert provider.name == "nvidia"


# ── Cloud classification with the probe ────────────────────────────────


class TestCloudClassificationWithProbe:
    def test_is_cloud_model_bare_name_with_probe_confirming_openrouter(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """A bare name confirmed in openrouter's catalog classifies as cloud
        and resolves to a non-managed provider."""
        shared_prefix_registry.set_catalog_probe(
            _probe_from_catalogs({"openrouter": {"z-ai/glm-5.3-flash"}})
        )
        assert shared_prefix_registry.is_cloud_model("z-ai/glm-5.3-flash")
        provider = shared_prefix_registry.get_provider_for_model("z-ai/glm-5.3-flash")
        assert provider is not None
        assert provider.managed is False


# ── Config pin: the hijacking prefix must stay removed ─────────────────


class TestConfigPinSharedPrefix:
    def test_nvidia_settings_yaml_no_longer_claims_zai_prefix(self):
        """config/providers/nvidia.settings.yaml must not claim ``z-ai/``:
        NVIDIA's catalog_allowlist serves no z-ai model, so the prefix was
        broader than the serving set (the G3 hijack). openrouter keeps it."""
        nvidia_cfg = yaml.safe_load(
            provider_settings_file("nvidia").read_text(encoding="utf-8")
        )
        openrouter_cfg = yaml.safe_load(
            provider_settings_file("openrouter").read_text(encoding="utf-8")
        )
        nvidia_prefixes = nvidia_cfg.get("model_prefixes") or []
        openrouter_prefixes = openrouter_cfg.get("model_prefixes") or []
        # Sanity: we parsed the intended structure.
        assert "nvidia/" in nvidia_prefixes
        assert "z-ai/" in openrouter_prefixes
        assert "z-ai/" not in nvidia_prefixes


# ── Address path unaffected (regression guard) ─────────────────────────


class TestAddressPathUnaffected:
    def test_openrouter_address_still_resolves_to_openrouter(
        self, shared_prefix_registry: ProviderRegistry
    ):
        """The working ``{provider}/{brand}/{model}`` address path must keep
        resolving via the first segment, regardless of any probe."""
        shared_prefix_registry.set_catalog_probe(_probe_from_catalogs({}))
        provider = shared_prefix_registry.get_provider_for_model(
            "openrouter/z-ai/glm-5.3-flash"
        )
        assert provider is not None
        assert provider.name == "openrouter"
