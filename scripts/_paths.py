from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.paths import CONFIG_DIR, DATA_DIR, DOCS_DIR, MODELS_DIR, OFFICIAL_LLAMA_SERVER_BIN