#!/usr/bin/env python3
"""Mint the scoped keys the 2026-11-11 deadline needs, in one reviewable command.

Every key in the live store is a **legacy** key: it declares no scopes, so it
keeps full access until ``auth.scopes_enforced_after`` and is a plain ``client``
key from that instant on. On the shipped deadline (2026-11-11T00:00:00Z) each of
them loses:

* ``operator`` — ``POST /admin/load``, ``POST /admin/unload``
* ``admin`` — the key store, ``POST /api/config/reload``, ``/api/cloud/*``,
  ``/api/status``, ``/api/crashes``, the scaler, sessions, the dashboard's
  ``/api/*`` surface
* ``capture-export`` — ``GET /api/capture/status``, ``POST /api/capture/rotate``

Inference keeps working for every client. The fix is four keys, not forty-one.

This script mints exactly those four, with the scopes each consumer needs, and
prints each token **once**. It is a dry run unless ``--apply`` is passed.

Usage::

    ./venv/bin/python scripts/mint_scoped_keys.py            # show the plan
    ./venv/bin/python scripts/mint_scoped_keys.py --apply    # mint them

The tokens are printed to stdout and nowhere else: they are not written to a
file, not logged, and not recoverable afterwards — the store keeps only the
token itself, so copy them into the consumers as they appear.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_STORE = CONFIG_DIR / "guardian.keys.yaml"
DEFAULT_PREFIX = "flip"

#: The four keys, with the reason each one exists. ``admin`` implies
#: ``operator``, ``client`` and ``capture-export`` (see app/proxy/scopes.py), so
#: the dashboard and the operator tooling can share one admin key.
PLAN: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "dashboard",
        ("admin",),
        "the browser dashboard: its /api/* calls are admin-scoped",
    ),
    (
        "operator-tools",
        ("admin",),
        "scripts that read /api/status, /api/crashes, /api/cloud/* and reload config",
    ),
    (
        "capture-export",
        ("capture-export",),
        "guardianctl.py and verify_post_restart.py: capture WAL export/rotate only",
    ),
    (
        "lifecycle",
        ("operator",),
        "load/unload automation, deliberately without the key store",
    ),
)


def load_store(path: Path) -> dict:
    """Load the key store, refusing to continue on an unreadable file.

    A malformed store must never be silently replaced: it holds every live
    token, and overwriting it would lock every client out.
    """
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"refusing to touch {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemExit(f"refusing to touch {path}: expected a mapping, got {type(raw).__name__}")
    return raw


def save_store(path: Path, store: dict) -> None:
    """Write the store atomically, keeping the file's mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(store, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def existing_by_name(store: dict) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Map key name -> (token, declared scopes) for the current store."""
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for token, entry in store.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        meta = entry.get("metadata") or {}
        scopes = meta.get("scopes") if isinstance(meta, dict) else None
        out[name] = (token, tuple(scopes) if isinstance(scopes, list) else ())
    return out


def build_plan(store: dict, *, force: bool) -> list[tuple[str, tuple[str, ...], str, str]]:
    """Return ``(name, scopes, reason, action)`` rows; action is mint/skip/replace."""
    present = existing_by_name(store)
    rows = []
    for name, scopes, reason in PLAN:
        if name not in present:
            rows.append((name, scopes, reason, "mint"))
            continue
        _token, declared = present[name]
        if declared == scopes and not force:
            rows.append((name, scopes, reason, "skip"))
        elif force:
            rows.append((name, scopes, reason, "replace"))
        else:
            rows.append((name, scopes, reason, "skip"))
    return rows


def render(rows, *, apply: bool, store: Path) -> str:
    lines = [
        f"Scoped-key plan — {store}",
        f"Mode: {'APPLY' if apply else 'DRY RUN — nothing is written. Pass --apply to mint.'}",
        "",
    ]
    for name, scopes, reason, action in rows:
        lines.append(f"  [{action:7}] {name:<16} scopes={list(scopes)}")
        lines.append(f"            {reason}")
    minted = [r for r in rows if r[3] in ("mint", "replace")]
    lines.append("")
    if not minted:
        lines.append("Nothing to mint: every planned key already exists with the right scopes.")
    else:
        lines.append(f"{len(minted)} key(s) would be minted. Tokens are shown once, on --apply.")
    return "\n".join(lines)


def mint(store: dict, rows, *, prefix: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Add the planned keys to ``store``; return ``(name, token, scopes)``."""
    created = []
    for name, scopes, _reason, action in rows:
        if action not in ("mint", "replace"):
            continue
        if action == "replace":
            for token, entry in list(store.items()):
                if isinstance(entry, dict) and entry.get("name") == name:
                    del store[token]
        token = f"{prefix}_{secrets.token_hex(16)}"
        store[token] = {
            "name": name,
            "created_at": time.time(),
            "metadata": {"scopes": list(scopes)},
            "cloud_gateway_access": True,
        }
        created.append((name, token, scopes))
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mint the four scoped keys the 2026-11-11 scope deadline needs. "
            "Dry run by default; --apply writes the key store and prints each token once."
        )
    )
    parser.add_argument("--apply", action="store_true", help="write the store and print the tokens")
    parser.add_argument("--force", action="store_true", help="re-mint a key that already exists")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="token prefix (default: flip)")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="key store path")
    parser.add_argument("--json", action="store_true", help="machine-readable plan (never includes tokens)")
    args = parser.parse_args(argv)

    store_path = Path(args.store)
    store = load_store(store_path)
    rows = build_plan(store, force=args.force)

    if args.json:
        print(json.dumps(
            {
                "store": str(store_path),
                "apply": args.apply,
                "plan": [
                    {"name": n, "scopes": list(s), "reason": r, "action": a}
                    for n, s, r, a in rows
                ],
            },
            indent=2,
        ))
    else:
        print(render(rows, apply=args.apply, store=store_path))

    if not args.apply:
        return 0

    created = mint(store, rows, prefix=args.prefix)
    if not created:
        return 0
    save_store(store_path, store)

    print("\n" + "=" * 72)
    print("TOKENS — shown once, not stored anywhere else. Copy them now.")
    print("=" * 72)
    for name, token, scopes in created:
        print(f"\n{name}  (scopes: {', '.join(scopes)})")
        print(f"  {token}")
    print("\n" + "=" * 72)
    print("Next: put each token in its consumer, then POST /api/config/reload")
    print("(or restart) so the key store is re-read. The 41 legacy keys keep")
    print("inference forever and fall back to `client` for privileged routes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
