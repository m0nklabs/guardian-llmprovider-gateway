"""Shared Guardian script authentication helpers."""

from __future__ import annotations

import os

import yaml

import _paths  # noqa: F401  (adds repo root to sys.path)
from app.paths import guardian_apikeys_file


def resolve_api_key(explicit_key: str | None = None) -> str:
    """Resolve a Guardian API key from CLI/env/config in that order."""
    for candidate in (
        explicit_key,
        os.environ.get("GUARDIAN_API_KEY"),
        os.environ.get("GUARDIAN_TEST_KEY"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    # Canonical key source: guardian.keys.yaml (entity YAML file). The legacy
    # api_keys.json is no longer read — it has been deprecated (2026-08-22).
    keys_path = guardian_apikeys_file()
    if keys_path.exists():
        keys = yaml.safe_load(keys_path.read_text()) or {}
        if isinstance(keys, dict) and keys:
            return next(iter(keys))

    raise SystemExit(
        "No Guardian API key found. Set GUARDIAN_API_KEY/GUARDIAN_TEST_KEY or populate recognized Guardian key files."
    )


def build_auth_headers(explicit_key: str | None = None) -> dict[str, str]:
    """Build Bearer auth headers for Guardian requests."""
    return {"Authorization": f"Bearer {resolve_api_key(explicit_key)}"}