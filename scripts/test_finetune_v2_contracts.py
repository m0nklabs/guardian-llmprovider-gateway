#!/usr/bin/env python3
"""Run deterministic finetune v2 contracts, with an opt-in live smoke layer."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from _paths import REPO_ROOT


def parse_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Also run Guardian-backed live smoke checks "
            "(sets FINETUNE_V2_LIVE=1; requires GUARDIAN_TEST_KEY; "
            "uses GUARDIAN_URL when set)"
        ),
    )
    return parser.parse_known_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args, pytest_args = parse_args(argv)
    test_paths = [
        "tests/unit/test_finetune_v2_contracts.py",
        "tests/unit/test_finetune_v2_runner.py",
        "tests/unit/test_finetune_v2_model_config_script.py",
    ]
    env = os.environ.copy()
    if args.live:
        env["FINETUNE_V2_LIVE"] = "1"
        test_paths.append("tests/integration/test_finetune_v2_live_smoke.py")

    command = [sys.executable, "-m", "pytest", *test_paths, *pytest_args]
    return subprocess.call(command, cwd=Path(REPO_ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
