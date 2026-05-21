#!/usr/bin/env python3
"""Run deterministic finetune v2 contracts, with an opt-in live smoke layer."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from _paths import REPO_ROOT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run Guardian-backed live smoke checks (requires FINETUNE_V2_LIVE inputs)",
    )
    parser.add_argument("pytest_args", nargs="*", help="Extra arguments forwarded to pytest")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    test_paths = ["tests/unit/test_finetune_v2_contracts.py"]
    env = os.environ.copy()
    if args.live:
        env["FINETUNE_V2_LIVE"] = "1"
        test_paths.append("tests/integration/test_finetune_v2_live_smoke.py")

    command = [sys.executable, "-m", "pytest", *test_paths, *args.pytest_args]
    return subprocess.call(command, cwd=Path(REPO_ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
