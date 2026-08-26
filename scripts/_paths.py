from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Re-exported for scripts: `from _paths import DOCS_DIR` etc. The noqa keeps
# ruff from "fixing" this as F401 (it is a re-export, not an unused import).
from app.paths import (  # noqa: E402,F401  (re-export for scripts)
    CONFIG_DIR,
    DATA_DIR,
    DOCS_DIR,
    MODELS_DIR,
    OFFICIAL_LLAMA_SERVER_BIN,
)

