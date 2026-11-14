"""Pin the operator-selected local coverage contract, without installing packages."""
from pathlib import Path
import os
import shlex
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def coverage_steps():
    workflow = yaml.safe_load((ROOT / ".github/workflows/python-ci.yml").read_text())
    return workflow["jobs"]["coverage"]["steps"]


def test_coverage_job_is_local_and_preserves_non_live_selection():
    steps = coverage_steps()
    command = next(step["run"] for step in steps if step.get("name") == "Run pytest with coverage")
    tokens = shlex.split(command)
    pytest_args = tokens[tokens.index("pytest") + 1:]
    assert "--cov=app" in pytest_args
    assert "--cov-fail-under=80" in pytest_args
    assert "--cov-report=term-missing" in pytest_args
    assert "-m" not in pytest_args  # Keep the project's integration exclusions.
    assert all("codecov" not in step.get("uses", "").lower() for step in steps)
    assert not (ROOT / "codecov.yml").exists()
    assert "pytest-cov==7.1.0" in (ROOT / "requirements.txt").read_text().splitlines()


def test_coverage_dependency_failure_is_visible_and_fatal(tmp_path):
    command = next(step["run"] for step in coverage_steps() if step.get("name") == "Install deps")
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *requirements.txt*) echo synthetic-install-failure >&2; exit 23;;\n"
        "  *) exit 0;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", command], cwd=ROOT,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 23
    assert "synthetic-install-failure" in result.stderr
