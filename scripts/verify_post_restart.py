#!/usr/bin/env python3
"""Post-restart verification for Guardian.

Verifies the service came up correctly:
  1. Guardian service is active.
  2. The loaded auth store reads the new guardian.keys.yaml (36 keys).
  3. The merged config deep-merges the full global.settings.yaml (queue etc.).
  4. /api/cloud/catalog responds; every configured provider reports a
     credential_status (ok / broken / unconfigured) and NO provider is marked
     broken (a 401/403 catalog fetch — the "broken credentials" unrelated to
     this script flagged loudly, not as a silent model_count:0).
  5. Reasoning-effort metadata is present in the openrouter catalog. After a
     code deploy that changes the catalog cache shape, run
     `POST /api/cloud/catalog/refresh` once and re-run this script.
Prints PASS/FAIL per check. Exit 0 only if all checks pass.
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

# Make `app` importable when run as `python scripts/verify_post_restart.py`
STAGE_ROOT = Path(__file__).resolve().parent.parent
if str(STAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(STAGE_ROOT))

PORT = 11434
BASE = f"http://127.0.0.1:{PORT}"


def _api_key() -> str:
    """Return a valid Bearer key for the loopback checks.

    guardian.keys.yaml maps full token strings (``oelala_<hash>``) to per-key
    metadata, so any dict key is a usable token. A named key is loaded because
    /api/cloud/catalog and /v1/models require auth.
    """
    from app.proxy import auth
    keys = auth.load_api_keys()
    if not isinstance(keys, dict) or not keys:
        return ""
    for token in keys:
        if token.startswith("oelala_") or token.startswith("goose_"):
            return token
    return next(iter(keys))


def check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _fetch(path: str):
    """GET a loopback Guardian path with a Bearer key, return (status, body)."""
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode())


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

    # 4. /api/cloud/catalog: reads providers, checks credential_status
    try:
        status, body = _fetch("/api/cloud/catalog")
        items = body.get("catalog", []) if isinstance(body, dict) else []
        if not isinstance(items, list):
            items = []
        configured = [p for p in items if p.get("configured")]
        broken = [p["name"] for p in configured if p.get("credential_status") == "broken"]
        all_have_status = all("credential_status" in p for p in configured)
        results.append(
            check("GET /api/cloud/catalog: no broken credentials",
                  status == 200 and all_have_status and not broken,
                  f"{len(configured)} configured provider(s); broken={broken or 'none'}"))
    except Exception as e:  # noqa: BLE001
        results.append(check("GET /api/cloud/catalog: no broken credentials", False, str(e)))

    # 5. Reasoning-effort metadata present in the openrouter catalog
    try:
        status, body = _fetch("/v1/models")
        entries = body.get("data", []) if isinstance(body, dict) else []
        if not isinstance(entries, list):
            entries = []
        or_with = [e for e in entries
                   if str(e.get("id", "")).startswith("openrouter/") and e.get("reasoning")]
        results.append(
            check("openrouter catalog carries reasoning-effort metadata",
                  status == 200 and len(or_with) > 0,
                  f"{len(or_with)} openrouter model(s) with reasoning metadata"))
    except Exception as e:  # noqa: BLE001
        results.append(check("openrouter catalog carries reasoning-effort metadata", False, str(e)))

    # 6. Capture: enabled for all authenticated clients, infinite retention,
    #    unlimited byte budget, writer running (raw-capture config).
    try:
        status, body = _fetch("/api/capture/status")
        cfg = body.get("config", {}) if isinstance(body, dict) else {}
        writer = body.get("writer", {}) if isinstance(body, dict) else {}
        ok = (
            status == 200
            and cfg.get("enabled") is True
            and cfg.get("active") is True
            and cfg.get("cloud_capture") is True
            and cfg.get("per_client_opt_in") is False
            and cfg.get("retention_days") == -1
            and cfg.get("max_capture_bytes") == -1
            and writer.get("running") is True
        )
        results.append(
            check("capture enabled, all clients, infinite retention, writer running",
                  ok,
                  f"cloud={cfg.get('cloud_capture')} opt_in={cfg.get('per_client_opt_in')} "
                  f"retention={cfg.get('retention_days')} budget={cfg.get('max_capture_bytes')} "
                  f"writer_running={writer.get('running')}"))
    except Exception as e:  # noqa: BLE001
        results.append(check("capture enabled, all clients, infinite retention, writer running", False, str(e)))

    ok_all = all(results)
    print("\n" + ("✅ ALL CHECKS PASSED" if ok_all else "⚠️ SOME CHECKS FAILED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
