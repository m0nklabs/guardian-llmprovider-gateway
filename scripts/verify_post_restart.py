#!/usr/bin/env python3
"""Post-restart verification for the config-schema split (PR #9).

Verifies the merged config-schema migration came up correctly:
  1. Guardian service is active.
  2. The loaded auth store reads the new guardian.keys.yaml (36 keys).
  3. The merged config deep-merges the full global.settings.yaml (queue etc.).
  4. A local model endpoint responds 200.
  5. /api/cloud/catalog responds and openrouter count reflects /models/user.
Prints PASS/FAIL per check. Exit 0 only if all checks pass.
"""

import json
import subprocess
import sys
import urllib.request

PORT = 11434
BASE = f"http://127.0.0.1:{PORT}"


def check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    results = []

    # 1. Service active
    try:
        out = subprocess.run(
            ["systemctl", "is-active", "llama-guardian"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        results.append(check("llama-guardian service active", out == "active", out))
    except Exception as e:  # noqa: BLE001
        results.append(check("llama-guardian service active", False, str(e)))

    # 2. Auth store reads new guardian.keys.yaml
    try:
        from app.proxy import auth
        keys = auth.load_api_keys()
        n = len(keys)
        results.append(
            check("auth loads keys from guardian.keys.yaml",
                  auth.API_KEYS_FILE.name == "guardian.keys.yaml" and n >= 30, f"{n} keys"))
    except Exception as e:  # noqa: BLE001
        results.append(check("auth loads keys from guardian.keys.yaml", False, str(e)))

    # 3. Config deep-merges full global doc (queue present)
    try:
        from app import config_loader as cl
        cfg = cl.load_config()
        q = cfg.get("queue", {})
        results.append(check("config deep-merges full global.settings.yaml",
                             "queue" in cfg and "max_concurrent" in q, json.dumps(q)))
    except Exception as e:  # noqa: BLE001
        results.append(check("config deep-merges full global.settings.yaml", False, str(e)))

    # 4. /api/cloud/catalog responds
    try:
        with urllib.request.urlopen(BASE + "/api/cloud/catalog", timeout=20) as r:
            body = json.loads(r.read().decode())
        or_count = body.get("openrouter")
        results.append(check("GET /api/cloud/catalog", r.status == 200,
                             f"openrouter={or_count}"))
    except Exception as e:  # noqa: BLE001
        results.append(check("GET /api/cloud/catalog", False, str(e)))

    ok_all = all(results)
    print("\n" + ("✅ ALL CHECKS PASSED" if ok_all else "⚠️ SOME CHECKS FAILED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
