"""Legacy LLAMA_CPP_GUARDIAN_* env vars must fail loudly (F0 rename, issue #1).

There is deliberately NO fallback: if anything still sets the old env vars,
``app.paths`` raises at import time so stale deployments surface the rename
instead of silently resolving wrong paths.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PY = REPO_ROOT / "venv" / "bin" / "python"


def _run(code: str, extra_env: dict) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(extra_env)
    return subprocess.run(
        [str(VENV_PY), "-c", code],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def test_legacy_root_env_var_rejected():
    proc = _run("import app.paths", {"LLAMA_CPP_GUARDIAN_ROOT": "/tmp/legacy-root"})
    assert proc.returncode != 0, proc.stdout
    assert "LLAMA_CPP_GUARDIAN_ROOT" in proc.stderr
    assert "GUARDIAN_LLMPROVIDER_GATEWAY_ROOT" in proc.stderr


def test_legacy_slots_env_var_rejected():
    proc = _run("import app.paths", {"LLAMA_CPP_GUARDIAN_SLOTS_DIR": "/tmp/legacy-slots"})
    assert proc.returncode != 0, proc.stdout
    assert "LLAMA_CPP_GUARDIAN_SLOTS_DIR" in proc.stderr


def test_new_env_var_still_works():
    proc = _run(
        "import app.paths; print(app.paths.REPO_ROOT)",
        {"GUARDIAN_LLMPROVIDER_GATEWAY_ROOT": "/tmp/root-override"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "/tmp/root-override" in proc.stdout


def test_no_legacy_vars_imports_cleanly():
    proc = _run("import app.paths; print(app.paths.REPO_ROOT.name)", {})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == REPO_ROOT.name
