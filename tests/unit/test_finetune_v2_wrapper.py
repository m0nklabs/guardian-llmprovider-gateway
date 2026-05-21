"""Smoke tests for the finetune v2 contract wrapper script."""

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def test_wrapper_passes_through_standard_pytest_flags():
    """Ensure parse_known_args passes hyphenated flags like -k to pytest."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "test_finetune_v2_contracts.py"),
            "-k",
            "test_no_probe_candidate_exceeds_total_layers_after_clamping",
        ],
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS_DIR.parent),
        timeout=60,
    )
    assert result.returncode == 0, f"Wrapper failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "passed" in result.stdout or "passed" in result.stderr
