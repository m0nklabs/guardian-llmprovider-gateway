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

# Local model registry (new name preferred; old name is a backward-compat alias).
LOCAL_MODELS_FILE = CONFIG_DIR / "local_models.yaml"
LEGACY_MODELS_FILE = CONFIG_DIR / "models.yaml"

# Guardian API keys (new name preferred; old name is a backward-compat alias).
GUARDIAN_APIKEYS_FILE = CONFIG_DIR / "guardian_apikeys.yaml"
LEGACY_APIKEYS_FILE = CONFIG_DIR / "api_keys.json"

# Cloud model catalog overrides + runtime cache (data, gitignored).
CLOUD_MODELS_OVERRIDES_FILE = CONFIG_DIR / "cloud_models.yaml"
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


def local_models_file() -> "Path":
    """Resolve the local model registry path (new name first, legacy alias)."""
    return resolve_config_file("local_models.yaml", "models.yaml")


def guardian_apikeys_file() -> "Path":
    """Resolve the Guardian API key store path (new name first, legacy alias)."""
    return resolve_config_file("guardian_apikeys.yaml", "api_keys.json")

LLAMA_SLOTS_DIR = _expand_path(
    os.getenv("LLAMA_CPP_GUARDIAN_SLOTS_DIR", str(Path.home() / "llama_slots"))
)
LLAMA_CPP_OFFICIAL_ROOT = _expand_path(
    os.getenv("LLAMA_CPP_OFFICIAL_ROOT", str(REPO_ROOT.parent / "llama_cpp_official"))
)
OFFICIAL_LLAMA_SERVER_BIN = _expand_path(
    os.getenv("LLAMA_SERVER_BINARY", str(LLAMA_CPP_OFFICIAL_ROOT / "build" / "bin" / "llama-server"))
)