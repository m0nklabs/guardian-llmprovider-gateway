import os
from pathlib import Path


def _expand_path(value: str) -> Path:
    """Expand a filesystem path without requiring it to exist yet."""
    return Path(value).expanduser()


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = _expand_path(os.getenv("LLAMA_CPP_GUARDIAN_ROOT", str(APP_DIR.parent)))
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"
MODELS_DIR = _expand_path(os.getenv("MODELS_DIR", str(REPO_ROOT.parent / "models")))

CURRENT_MODEL_ARGS_FILE = CONFIG_DIR / "current_model.args"
CURRENT_MODEL_ENV_FILE = CONFIG_DIR / "current_model.env"
CURRENT_MODEL_SIG_FILE = CONFIG_DIR / "current_model.sig"

# ── Config-schema files (2026-08-21, docs/CONFIG_SCHEMA.md) ─────────────
# Canonical names are domain-first and self-describing.  Legacy names remain
# as backward-compat aliases/symlinks and are resolved first-if-present by the
# resolve_config_file helpers below.

# Global infra + subsystem (was: settings.yaml).  Holds proxy/queue/timeouts/
# scaler/capture/grammar/cloud_retry/failover_health/services/services_to_stop/
# benchmark/failover_groups.
GLOBAL_SETTINGS_FILE = CONFIG_DIR / "global.settings.yaml"
LEGACY_SETTINGS_FILE = CONFIG_DIR / "settings.yaml"

# Cloud gateways: provider defaults + per-provider overrides.
PROVIDERS_SETTINGS_FILE = CONFIG_DIR / "providers.settings.yaml"
PROVIDERS_OVERRIDES_FILE = CONFIG_DIR / "providers.overrides.yaml"

# Models: local registry defaults/overrides + cloud model defaults/overrides.
MODELS_LOCAL_SETTINGS_FILE = CONFIG_DIR / "models.local.settings.yaml"
MODELS_LOCAL_OVERRIDES_FILE = CONFIG_DIR / "models.local.overrides.yaml"
MODELS_CLOUD_SETTINGS_FILE = CONFIG_DIR / "models.cloud.settings.yaml"
MODELS_CLOUD_OVERRIDES_FILE = CONFIG_DIR / "models.cloud.overrides.yaml"

# Guardian API keys (entity file).
GUARDIAN_KEYS_FILE = CONFIG_DIR / "guardian.keys.yaml"

# Local model registry (new name preferred; old names are backward-compat aliases).
LEGACY_LOCAL_MODELS_FILE = CONFIG_DIR / "local_models.yaml"
LEGACY_MODELS_FILE = CONFIG_DIR / "models.yaml"

# Guardian API keys (legacy aliases).
LEGACY_GUARDIAN_APIKEYS_FILE = CONFIG_DIR / "guardian_apikeys.yaml"
LEGACY_APIKEYS_FILE = CONFIG_DIR / "api_keys.json"

# Cloud model overrides (legacy alias of models.cloud.overrides.yaml).
LEGACY_CLOUD_MODELS_OVERRIDES_FILE = CONFIG_DIR / "cloud_models.yaml"

# Backward-compat aliases for callers that reference the legacy constant
# names directly.  Resolve to the canonical new file when present.
CLOUD_MODELS_OVERRIDES_FILE = MODELS_CLOUD_OVERRIDES_FILE
GUARDIAN_APIKEYS_FILE = GUARDIAN_KEYS_FILE
LEGACY_APIKEYS_FILE = CONFIG_DIR / "api_keys.json"

# Cloud catalog runtime cache (data, gitignored).
CLOUD_CATALOG_CACHE_FILE = DATA_DIR / "cloud_catalog_cache.json"


def resolve_config_file(*names: str) -> "Path":
    """Return the first existing path among *names*, else the last one.

    Used for backward-compatible config renames: prefer the new file name and
    fall back to the legacy name when the new one is absent.
    """
    for name in names:
        candidate = CONFIG_DIR / name
        if candidate.exists():
            return candidate
    return CONFIG_DIR / names[-1]


def global_settings_file() -> "Path":
    """Resolve the global settings path (new name first, legacy alias)."""
    return resolve_config_file("global.settings.yaml", "settings.yaml")


def providers_defaults_file() -> "Path":
    return resolve_config_file("providers.settings.yaml")


def providers_overrides_file() -> "Path":
    return resolve_config_file("providers.overrides.yaml")


def models_cloud_overrides_file() -> "Path":
    """Resolve the cloud-model overrides path (new name first, legacy alias)."""
    return resolve_config_file("models.cloud.overrides.yaml", "cloud_models.yaml")


def local_models_file() -> "Path":
    """Resolve the local model registry path (new name first, legacy alias)."""
    return resolve_config_file("models.local.settings.yaml", "local_models.yaml", "models.yaml")


def guardian_apikeys_file() -> "Path":
    """Resolve the Guardian API key store path (new name first, legacy alias)."""
    return resolve_config_file("guardian.keys.yaml", "guardian_apikeys.yaml", "api_keys.json")

LLAMA_SLOTS_DIR = _expand_path(
    os.getenv("LLAMA_CPP_GUARDIAN_SLOTS_DIR", str(Path.home() / "llama_slots"))
)
LLAMA_CPP_OFFICIAL_ROOT = _expand_path(
    os.getenv("LLAMA_CPP_OFFICIAL_ROOT", str(REPO_ROOT.parent / "llama_cpp_official"))
)
OFFICIAL_LLAMA_SERVER_BIN = _expand_path(
    os.getenv("LLAMA_SERVER_BINARY", str(LLAMA_CPP_OFFICIAL_ROOT / "build" / "bin" / "llama-server"))
)
