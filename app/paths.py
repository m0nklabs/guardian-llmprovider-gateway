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

LLAMA_SLOTS_DIR = _expand_path(
    os.getenv("LLAMA_CPP_GUARDIAN_SLOTS_DIR", str(Path.home() / "llama_slots"))
)
LLAMA_CPP_OFFICIAL_ROOT = _expand_path(
    os.getenv("LLAMA_CPP_OFFICIAL_ROOT", str(REPO_ROOT.parent / "llama_cpp_official"))
)
OFFICIAL_LLAMA_SERVER_BIN = _expand_path(
    os.getenv("LLAMA_SERVER_BINARY", str(LLAMA_CPP_OFFICIAL_ROOT / "build" / "bin" / "llama-server"))
)