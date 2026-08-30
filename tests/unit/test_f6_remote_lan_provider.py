"""F6: the 14700K Windows GPU as a passive LAN provider.

Pins the F6 provider semantics (docs/IMPLEMENTATION_PLAN.md §F6):
- an EXPLICIT ``local: false`` document marker overrides the ``-local``
  name-suffix heuristic: ``14700k-local`` is a REMOTE LAN host managed by its
  own caretaker — a passive cloud-routed LAN provider, never owned by this
  gateway's lifecycle;
- without an explicit marker the ``-local`` suffix keeps the F2 semantics
  (name-based fallback → managed);
- ``local: true`` stays managed (regression);
- the shipped ``14700k-local.settings.yaml`` carries the markers that make it
  a disabled, non-managed LAN provider until the operator fills in the real
  Windows address.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.proxy.providers import ProviderRegistry


@pytest.fixture
def provider_dir(tmp_path: Path) -> Path:
    """A providers/ tree exercising all three managed-decision branches."""
    d = tmp_path / "providers"
    d.mkdir()
    (d / "14700k-local.settings.yaml").write_text(textwrap.dedent("""\
        local: false
        enabled: true
        base_url: http://192.168.1.x:11440/v1
        api_key: win-test
        brand: windows
        catalog_url: /v1/models
        # Registry list-form: statically advertised cloud models (the
        # per-model override dict is a separate mechanism).
        models:
          - windows/qwen3-8b-q5
    """))
    (d / "someday-remote-local.settings.yaml").write_text(textwrap.dedent("""\
        enabled: true
        base_url: http://127.0.0.1:11441/v1
    """))
    (d / "ai-kvm2-local.settings.yaml").write_text(textwrap.dedent("""\
        local: true
        enabled: true
        base_url: http://127.0.0.1:11440/v1
        models:
          llama3.2-3b:
            path: /home/flip/models/llama3.2-3b.gguf
    """))
    return d


@pytest.fixture
def reg(monkeypatch, provider_dir: Path) -> ProviderRegistry:
    monkeypatch.setattr("app.paths.PROVIDERS_DIR", provider_dir)
    return ProviderRegistry()


async def test_explicit_local_false_overrides_name_suffix(reg: ProviderRegistry):
    """`14700k-local` + `local: false` is a passive LAN provider: NOT managed,
    configured via its api_key, cloud-routable — despite the -local suffix."""
    p = reg._providers["14700k-local"]
    assert p is not None
    assert p.managed is False, "explicit local: false must override the suffix"
    assert p.is_configured is True  # api_key present → LAN inference works


async def test_name_suffix_still_managed_without_explicit_marker(
    reg: ProviderRegistry,
):
    """Without an explicit `local:` marker the -local suffix keeps the F2
    semantics (name-based managed fallback) — backwards compatibility."""
    p = reg._providers["someday-remote-local"]
    assert p is not None
    assert p.managed is True


async def test_local_true_still_managed(reg: ProviderRegistry):
    """`local: true` (this host's llama-server) stays a managed entry."""
    p = reg._providers["ai-kvm2-local"]
    assert p is not None
    assert p.managed is True


async def test_cloud_models_include_windows_brand_not_managed(reg: ProviderRegistry):
    """The non-managed LAN provider's models are advertised as cloud models
    (brand prefix `windows`), while the managed host is excluded."""
    cloud = reg.get_all_cloud_models()
    # Statically declared models surface under the brand prefix; the dynamic
    # /v1/models catalog adds the rest at refresh time (brand-normalized).
    assert "windows/qwen3-8b-q5" in cloud
    assert "ai-kvm2-local" not in cloud
    # The managed host's models are never advertised as cloud.
    assert reg.is_cloud_model("windows/qwen3-8b-q5") is True
    assert reg.is_cloud_model("ai-kvm2-local/llama3.2-3b") is False


def test_shipped_14700k_provider_file_markers():
    """The shipped 14700k-local.settings.yaml is disabled and explicitly
    non-managed until the operator fills in the real Windows address — safe
    to merge before the Windows-side setup exists."""
    import yaml

    from app.paths import PROVIDERS_DIR

    path = PROVIDERS_DIR / "14700k-local.settings.yaml"
    assert path.exists(), "F6 provider file must ship with the repo"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Permanent structural markers — these never change when the operator
    # activates the provider on a deployed checkout.
    assert cfg["local"] is False
    assert cfg["brand"] == "windows"
    assert cfg["catalog_url"] == "/models"  # appended to base_url (…/v1)
    # Activation switches: only pinned while the placeholder address is still
    # present.  Once the operator fills in the real Windows host (HANDOFF F6),
    # the deployed checkout legitimately diverges — otherwise the full-suite
    # pre-restart gate would fail on every later restart.
    if "192.168.1.x" in cfg.get("base_url", ""):
        assert cfg["enabled"] is False, "placeholder config must stay disabled"
        assert cfg["api_key"] == "${WINDOWS_LAN_KEY}"  # env-only secret, never inline
