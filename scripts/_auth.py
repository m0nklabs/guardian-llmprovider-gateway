"""Shared Guardian script authentication helpers."""

from __future__ import annotations

import json
import os

from _paths import CONFIG_DIR


def resolve_api_key(explicit_key: str | None = None) -> str:
    """Resolve a Guardian API key from CLI/env/config in that order."""
    for candidate in (
        explicit_key,
        os.environ.get("GUARDIAN_API_KEY"),
        os.environ.get("GUARDIAN_TEST_KEY"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    api_keys_path = CONFIG_DIR / "api_keys.json"
    if api_keys_path.exists():
        keys = json.loads(api_keys_path.read_text())
        if keys:
            return next(iter(keys))

    raise SystemExit(
        "No Guardian API key found. Set GUARDIAN_API_KEY/GUARDIAN_TEST_KEY or populate config/api_keys.json."
    )


def build_auth_headers(explicit_key: str | None = None) -> dict[str, str]:
    """Build Bearer auth headers for Guardian requests."""
    return {"Authorization": f"Bearer {resolve_api_key(explicit_key)}"}