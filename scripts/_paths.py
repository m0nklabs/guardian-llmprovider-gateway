import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Re-exported for scripts: `from _paths import DOCS_DIR` etc. The noqa keeps
# ruff from "fixing" this as F401 (it is a re-export, not an unused import).
from app.paths import (  # noqa: F401, E402  (re-export for scripts; intentional late import after path bootstrap)
    CONFIG_DIR,
    DATA_DIR,
    DOCS_DIR,
    MODELS_DIR,
    OFFICIAL_LLAMA_SERVER_BIN,
)

