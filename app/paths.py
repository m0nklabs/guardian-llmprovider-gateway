import os
from pathlib import Path


def _expand_path(value: str) -> Path:
    """Expand a filesystem path without requiring it to exist yet."""
    return Path(value).expanduser()


APP_DIR = Path(__file__).resolve().parent
# Legacy env vars are GONE (F0 rename, issue #1) — no fallback. If anything
# still sets them, fail loudly so stale deployments surface the rename
# instead of silently resolving wrong paths.
_LEGACY_ENV_RENAMES = {
    "LLAMA_CPP_GUARDIAN_ROOT": "GUARDIAN_LLMPROVIDER_GATEWAY_ROOT",
    "LLAMA_CPP_GUARDIAN_SLOTS_DIR": "GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR",
}
_legacy_set = [v for v in _LEGACY_ENV_RENAMES if os.getenv(v)]
if _legacy_set:
    _detail = "; ".join(
        f"{old} -> use {_LEGACY_ENV_RENAMES[old]}" for old in _legacy_set
    )
    raise RuntimeError(
        "Legacy LLAMA_CPP_GUARDIAN_* env vars are no longer supported "
        f"(F0 rename, issue #1): {_detail}"
    )
REPO_ROOT = _expand_path(os.getenv("GUARDIAN_LLMPROVIDER_GATEWAY_ROOT", str(APP_DIR.parent)))
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

# Cloud gateways + local provider: one settings file per provider (F2,
# docs/CONFIG_PROVIDER_FILES.md).  The old providers.settings.yaml +
# providers.overrides.yaml + models.local.settings.yaml +
# models.cloud.overrides.yaml split is replaced by a `providers/` directory
# scan; these legacy constants are retained for backward-compat readers/tests
# that still reference the old filenames.
PROVIDERS_SETTINGS_FILE = CONFIG_DIR / "providers.settings.yaml"
PROVIDERS_OVERRIDES_FILE = CONFIG_DIR / "providers.overrides.yaml"

# Models: local registry + cloud model overrides (settings/overrides for the
# other model files are reserved — no runtime consumer yet, see CONFIG_SCHEMA).)
MODELS_LOCAL_SETTINGS_FILE = CONFIG_DIR / "models.local.settings.yaml"
MODELS_CLOUD_OVERRIDES_FILE = CONFIG_DIR / "models.cloud.overrides.yaml"

# Per-provider config files (F2).  Each `config/providers/<name>.settings.yaml`
# holds that provider's whole document: enabled/base_url/api_key/timeout/
# model_prefixes (+ catalog_url/catalog_allowlist/local) and a `models:` block
# with per-model overrides (and, for the local provider, the local registry +
# aliases + guardian policy).
PROVIDERS_DIR = CONFIG_DIR / "providers"

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
# names directly.  These resolve at import time via the helpers below so an
# installation still on a legacy filename keeps reading its current file
# rather than silently falling back to an even older format.

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


def provider_settings_file(name: str) -> "Path":
    """Resolve ``config/providers/<name>.settings.yaml``."""
    return PROVIDERS_DIR / f"{name}.settings.yaml"


def provider_names() -> "list[str]":
    """Return the sorted provider names from ``config/providers/*.settings.yaml``.

    The provider name is the file basename without the ``.settings.yaml``
    suffix.  The local provider carries a ``-local`` suffix in its name (and an
    explicit ``local: true`` marker in its document); cloud providers do not.
    """
    if not PROVIDERS_DIR.is_dir():
        return []
    suffix = ".settings.yaml"
    names = sorted(
        p.name[: -len(suffix)]
        for p in PROVIDERS_DIR.glob(f"*{suffix}")
        if p.is_file()
    )
    return names


def is_local_provider_name(name: str) -> bool:
    """Return True when a provider name marks a local (managed) provider.

    Local providers are identified by the ``-local`` name suffix (F2, docs/
    CONFIG_PROVIDER_FILES.md) — e.g. ``ai-kvm2-local``, ``14700k-local``.  The
    ``local: true`` marker in the document is read separately by the directory
    scan; this helper is a cheap name-based fallback.
    """
    return name.endswith("-local")


def models_cloud_overrides_file() -> "Path":
    """Resolve the cloud-model overrides path (new name first, legacy alias).

    Deprecated since F2 (per-provider config): overrides now live in each
    provider's ``models:`` block.  Retained only for backward-compat readers.
    """
    return resolve_config_file("models.cloud.overrides.yaml", "cloud_models.yaml")


def local_models_file() -> "Path":
    """Resolve the local model registry path.

    Since F2 (docs/CONFIG_PROVIDER_FILES.md) the local registry lives in the
    local provider's file ``config/providers/ai-kvm2-local.settings.yaml``
    (with its ``models:``/``aliases:``/``guardian:`` blocks).  This helper
    keeps resolving there so ``app/engine/manager.py``, scripts and tests that
    import it keep working unchanged.  Legacy single-file names are kept as a
    final fallback for older installations still on the pre-F2 layout.
    """
    return resolve_config_file(
        "providers/ai-kvm2-local.settings.yaml",
        "models.local.settings.yaml",
        "local_models.yaml",
        "models.yaml",
    )


def guardian_apikeys_file() -> "Path":
    """Resolve the Guardian API key store path (new name first, legacy alias)."""
    return resolve_config_file("guardian.keys.yaml", "guardian_apikeys.yaml", "api_keys.json")


# Backward-compat constant aliases for callers that import the legacy names
# directly (e.g. app/proxy/auth.py, app/proxy/cloud_catalog.py).  These are
# resolved at import time so a deployment still on the legacy filename keeps
# reading its current file instead of falling back to an even older format.
GUARDIAN_APIKEYS_FILE = guardian_apikeys_file()
CLOUD_MODELS_OVERRIDES_FILE = models_cloud_overrides_file()

LLAMA_SLOTS_DIR = _expand_path(
    os.getenv("GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR", str(Path.home() / "llama_slots"))
)
LLAMA_CPP_OFFICIAL_ROOT = _expand_path(
    os.getenv("LLAMA_CPP_OFFICIAL_ROOT", str(REPO_ROOT.parent / "llama_cpp_official"))
)
OFFICIAL_LLAMA_SERVER_BIN = _expand_path(
    os.getenv("LLAMA_SERVER_BINARY", str(LLAMA_CPP_OFFICIAL_ROOT / "build" / "bin" / "llama-server"))
)
