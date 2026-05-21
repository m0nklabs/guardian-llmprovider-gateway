"""Smoke tests for the finetune v2 wrapper script."""

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "test_finetune_v2_contracts.py"


def test_wrapper_forwards_hyphenated_pytest_flags():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "-q",
            "--collect-only",
            "-k",
            "convergence",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "unrecognized arguments: -k" not in result.stderr
    assert "convergence" in result.stdout
