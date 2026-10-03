"""Launch the dynamic Copilot adapter using its configured listener."""
from pathlib import Path
import os
import sys

import uvicorn
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    path = Path(os.environ.get("GUARDIAN_COPILOT_BRIDGE_CONFIG", ROOT / "config/github-copilot-bridge.settings.yaml"))
    config = yaml.safe_load(path.read_text())
    port = config["listen_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("listen_port must be an integer between 1 and 65535")
    uvicorn.run("app.copilot_bridge:create_app", factory=True,
                host=config["listen_host"], port=port, log_level="info")


if __name__ == "__main__":
    main()
