"""Regression tests for the config-schema split (docs/CONFIG_SCHEMA.md).

The monolith ``config/settings.yaml`` is split into domain files:
``global.settings.yaml`` (+ legacy alias), ``providers.settings.yaml`` +
``providers.overrides.yaml``, ``models.local.settings.yaml`` + overrides,
``models.cloud.settings.yaml`` + ``models.cloud.overrides.yaml``, and
``guardian.keys.yaml``.  These tests pin the two behaviours that the split
must preserve:

1. ``config_loader.load_config()`` merges providers defaults + overrides with
   overrides winning, and still carries the same top-level keys.
2. Production-default ``ProviderRegistry`` (no explicit settings_path) reads
   the merged providers + context overrides from the new files.
"""

import yaml
from pathlib import Path

import pytest

from app import config_loader
from app.proxy.providers import ProviderRegistry


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def test_load_config_merges_providers_overrides_over_defaults(monkeypatch, tmp_path: Path):
    """providers.settings.yaml defaults + providers.overrides.yaml (override wins)."""
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
    defaults_f = _write(
        tmp_path,
        "providers.settings.yaml",
        yaml.safe_dump(
            {
                "providers": {
                    "openrouter": {
                        "enabled": True,
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "sk-or-test",
                        "timeout_seconds": 1200,
                        "model_prefixes": ["anthropic/"],
                    }
                }
            },
            sort_keys=False,
        ),
    )
    overrides_f = _write(
        tmp_path,
        "providers.overrides.yaml",
        yaml.safe_dump(
            {"providers": {"openrouter": {"catalog_url": "/models/user"}}},
            sort_keys=False,
        ),
    )

    monkeypatch.setattr(config_loader, "CONFIG_PATH", global_f)
    monkeypatch.setattr(config_loader, "providers_defaults_file", lambda: defaults_f)
    monkeypatch.setattr(config_loader, "providers_overrides_file", lambda: overrides_f)

    cfg = config_loader.load_config()

    # Same top-level keys as the legacy settings.yaml merge.
    assert "proxy" in cfg
    assert "grammar" in cfg
    assert "queue" in cfg
    assert "providers" in cfg
    assert cfg["queue"]["max_concurrent"] == 7
    assert cfg["queue"]["queue_timeout_seconds"] == 123
    or_cfg = cfg["providers"]["openrouter"]
    # defaults preserved
    assert or_cfg["base_url"] == "https://openrouter.ai/api/v1"
    assert or_cfg["timeout_seconds"] == 1200
    # override wins
    assert or_cfg["catalog_url"] == "/models/user"


def test_provider_registry_production_default_reads_merged_providers(monkeypatch, tmp_path: Path):
    """ProviderRegistry() (no explicit settings_path) reads the new files."""
    defaults_f = _write(
        tmp_path,
        "providers.settings.yaml",
        yaml.safe_dump(
            {
                "providers": {
                    "openrouter": {
                        "enabled": True,
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "sk-or-test",
                        "models": ["openai/gpt-4o"],
                    }
                }
            },
            sort_keys=False,
        ),
    )
    overrides_f = _write(
        tmp_path,
        "providers.overrides.yaml",
        yaml.safe_dump(
            {"providers": {"openrouter": {"catalog_url": "/models/user"}}},
            sort_keys=False,
        ),
    )
    cloud_ov_f = _write(
        tmp_path,
        "models.cloud.overrides.yaml",
        yaml.safe_dump(
            {"moonshotai/kimi-k3": {"context_window": 1048576}},
            sort_keys=False,
        ),
    )

    import app.proxy.providers as pmod

    monkeypatch.setattr(pmod, "PROVIDERS_SETTINGS_FILE", defaults_f)
    monkeypatch.setattr(pmod, "PROVIDERS_OVERRIDES_FILE", overrides_f)
    monkeypatch.setattr(pmod, "MODELS_CLOUD_OVERRIDES_FILE", cloud_ov_f)

    reg = ProviderRegistry()  # no explicit settings_path -> merged production default

    assert "openrouter" in reg._providers
    assert reg._providers["openrouter"].catalog_url == "/models/user"
    assert reg.get_context_override("moonshotai/kimi-k3") == 1048576


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

    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(paths, "GUARDIAN_KEYS_FILE", tmp_path / "guardian.keys.yaml")
    monkeypatch.setattr(paths, "MODELS_CLOUD_OVERRIDES_FILE", tmp_path / "models.cloud.overrides.yaml")
    monkeypatch.setattr(paths, "GUARDIAN_APIKEYS_FILE", tmp_path / "guardian.keys.yaml")
    monkeypatch.setattr(paths, "CLOUD_MODELS_OVERRIDES_FILE", tmp_path / "models.cloud.overrides.yaml")

    # Existing canonical files -> local_models_file resolves to the new name.
    assert paths.local_models_file().name == "models.local.settings.yaml"
    assert paths.guardian_apikeys_file().name == "guardian.keys.yaml"
    assert paths.global_settings_file().name == "global.settings.yaml"
    assert paths.models_cloud_overrides_file().name == "models.cloud.overrides.yaml"
    # Alias constants point at the canonical files.
    assert paths.CLOUD_MODELS_OVERRIDES_FILE == paths.MODELS_CLOUD_OVERRIDES_FILE
    assert paths.GUARDIAN_APIKEYS_FILE == paths.GUARDIAN_KEYS_FILE
