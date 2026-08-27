"""Regression tests for the config-schema split (docs/CONFIG_SCHEMA.md).

The monolith ``config/settings.yaml`` is split into domain files.  Since F2
(docs/CONFIG_PROVIDER_FILES.md) the provider split is a single file per
provider in ``config/providers/<name>.settings.yaml``, replacing the old
``providers.settings.yaml`` + ``providers.overrides.yaml`` +
``models.local.settings.yaml`` + ``models.cloud.overrides.yaml`` layout.

These tests pin the behaviours the split must preserve:

1. ``config_loader.load_config()`` carries the per-provider documents keyed by
   provider name (directory scan, one document per provider).
2. Production-default ``ProviderRegistry`` (no explicit settings_path) reads
   the ``providers/`` directory, excluding the local provider, and derives
   ``context_overrides`` from the cloud providers' ``models:`` blocks.
"""

import yaml
from pathlib import Path


from app import config_loader
from app.proxy.providers import ProviderRegistry


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def _make_provider_dir(tmp_path: Path) -> Path:
    """Create a config/providers/ directory with a couple of provider files."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    _write(
        providers_dir,
        "openrouter.settings.yaml",
        yaml.safe_dump(
            {
                "enabled": True,
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-or-test",
                "timeout_seconds": 1200,
                "model_prefixes": ["anthropic/"],
                "catalog_url": "/models/user",
                "models": {
                    "moonshotai/kimi-k3": {"context_window": 1048576},
                    "gpt-4o": {"max_tokens": 4096, "temperature": 0.7},
                },
            },
            sort_keys=False,
        ),
    )
    _write(
        providers_dir,
        "nvidia.settings.yaml",
        yaml.safe_dump(
            {
                "enabled": True,
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": "nv-test",
                "catalog_allowlist": ["moonshotai/kimi-k3"],
                "models": {"minimaxai/minimax-m3": {"max_tokens": 8192}},
            },
            sort_keys=False,
        ),
    )
    return providers_dir


def test_load_config_scans_providers_directory(monkeypatch, tmp_path: Path):
    """config_loader.load_config() carries one document per provider."""
    global_f = tmp_path / "global.settings.yaml"
    global_f.write_text(
        yaml.safe_dump(
            {
                "proxy": {"port": 11434},
                "grammar": {"enabled": True},
                "queue": {"max_concurrent": 7, "queue_timeout_seconds": 123},
            },
            sort_keys=False,
        )
    )
    _make_provider_dir(tmp_path)

    monkeypatch.setattr(config_loader, "CONFIG_PATH", global_f)
    monkeypatch.setattr("app.paths.PROVIDERS_DIR", tmp_path / "providers")

    cfg = config_loader.load_config()

    # Same top-level keys as the legacy settings.yaml merge.
    assert "proxy" in cfg
    assert "grammar" in cfg
    assert "queue" in cfg
    assert "providers" in cfg
    assert cfg["queue"]["max_concurrent"] == 7
    assert cfg["queue"]["queue_timeout_seconds"] == 123
    # Per-provider documents, keyed by provider name.
    assert "openrouter" in cfg["providers"]
    assert "nvidia" in cfg["providers"]
    or_cfg = cfg["providers"]["openrouter"]
    assert or_cfg["base_url"] == "https://openrouter.ai/api/v1"
    assert or_cfg["timeout_seconds"] == 1200
    assert or_cfg["catalog_url"] == "/models/user"
    # The provider's own models: block is preserved.
    assert or_cfg["models"]["gpt-4o"] == {"max_tokens": 4096, "temperature": 0.7}


def test_provider_registry_production_default_reads_provider_directory(monkeypatch, tmp_path: Path):
    """ProviderRegistry() (no explicit settings_path) reads the providers/ dir."""
    _make_provider_dir(tmp_path)

    monkeypatch.setattr("app.paths.PROVIDERS_DIR", tmp_path / "providers")

    reg = ProviderRegistry()  # no explicit settings_path -> directory scan

    assert "openrouter" in reg._providers
    assert reg._providers["openrouter"].catalog_url == "/models/user"
    assert reg.get_context_override("moonshotai/kimi-k3") == 1048576
    assert reg.get_context_override("gpt-4o") is None  # gpt-4o has no context_window


def test_provider_registry_excludes_local_provider(monkeypatch, tmp_path: Path):
    """A *-local provider is not treated as a cloud gateway."""
    _make_provider_dir(tmp_path)
    _write(
        tmp_path / "providers",
        "ai-kvm2-local.settings.yaml",
        yaml.safe_dump(
            {
                "enabled": True,
                "base_url": "http://127.0.0.1:11440/v1",
                "local": True,
                "models": {"llama3.2-3b": {"path": "/home/flip/models/llama3.2-3b.gguf"}},
            },
            sort_keys=False,
        ),
    )

    monkeypatch.setattr("app.paths.PROVIDERS_DIR", tmp_path / "providers")

    reg = ProviderRegistry()
    # F3: the local provider is a managed entry in the registry — present,
    # flagged managed, keyless-configured, and NEVER a cloud gateway.
    assert "ai-kvm2-local" in reg._providers
    local_provider = reg._providers["ai-kvm2-local"]
    assert local_provider.managed is True
    assert local_provider.is_configured is True
    assert not reg.is_cloud_model("ai-kvm2-local/llama3.2-3b")
    assert not reg.is_cloud_model("llama3.2-3b")
    assert reg._provider_from_address("ai-kvm2-local/llama3.2-3b") is local_provider


def test_path_aliases_resolve_schema_names(monkeypatch, tmp_path: Path):
    """Legacy constants resolve to the canonical new names (backward compat)."""
    import app.paths as paths

    for name in (
        "global.settings.yaml",
        "models.local.settings.yaml",
        "models.cloud.overrides.yaml",
        "guardian.keys.yaml",
    ):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "ai-kvm2-local.settings.yaml").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(paths, "PROVIDERS_DIR", providers_dir)
    monkeypatch.setattr(paths, "GUARDIAN_KEYS_FILE", tmp_path / "guardian.keys.yaml")
    monkeypatch.setattr(paths, "MODELS_CLOUD_OVERRIDES_FILE", tmp_path / "models.cloud.overrides.yaml")
    monkeypatch.setattr(paths, "GUARDIAN_APIKEYS_FILE", tmp_path / "guardian.keys.yaml")
    monkeypatch.setattr(paths, "CLOUD_MODELS_OVERRIDES_FILE", tmp_path / "models.cloud.overrides.yaml")

    # local_models_file now resolves to the per-provider local file (F2).
    assert paths.local_models_file() == providers_dir / "ai-kvm2-local.settings.yaml"
    assert paths.guardian_apikeys_file().name == "guardian.keys.yaml"
    assert paths.global_settings_file().name == "global.settings.yaml"
    assert paths.models_cloud_overrides_file().name == "models.cloud.overrides.yaml"
    # Alias constants point at the canonical files.
    assert paths.CLOUD_MODELS_OVERRIDES_FILE == paths.MODELS_CLOUD_OVERRIDES_FILE
    assert paths.GUARDIAN_APIKEYS_FILE == paths.GUARDIAN_KEYS_FILE


def test_provider_names_and_local_marker(monkeypatch, tmp_path: Path):
    """provider_names() scans providers/*.settings.yaml; *-local is the local marker."""
    import app.paths as paths

    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    for name in ("openrouter", "nvidia", "ai-kvm2-local"):
        (providers_dir / f"{name}.settings.yaml").write_text("{}", encoding="utf-8")
    # A non-settings file is ignored.
    (providers_dir / "notes.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(paths, "PROVIDERS_DIR", providers_dir)

    assert paths.provider_names() == ["ai-kvm2-local", "nvidia", "openrouter"]
    assert paths.is_local_provider_name("ai-kvm2-local") is True
    assert paths.is_local_provider_name("14700k-local") is True
    assert paths.is_local_provider_name("openrouter") is False
