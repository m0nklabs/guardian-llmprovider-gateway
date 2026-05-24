#!/usr/bin/env python3
"""Compatibility wrapper for the root-level finetune_v2.py CLI."""

from __future__ import annotations

from _paths import REPO_ROOT  # noqa: F401 - importing ensures repo root is on sys.path
from app.tweaker.finetune_v2_cli import main, parse_args, validate_args


__all__ = ["main", "parse_args", "validate_args"]


if __name__ == "__main__":
    raise SystemExit(main())
