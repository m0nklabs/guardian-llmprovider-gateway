#!/usr/bin/env python3
"""guardianctl — CLI for Guardian capture subsystem control.

Subcommands:
  status       Show capture subsystem status (config + runtime + retention/on-disk inventory)
  config       Show effective capture configuration
  files        List capture WAL files on disk
  permissions  Report permission drift on the capture tree (report-only unless --apply)
  rotate       Force rotation of the active capture file
  enable       Enable capture (modifies settings.yaml, requires server restart)
  disable      Disable capture (modifies settings.yaml, requires server restart)
  test-event   Emit a synthetic test event to verify the pipeline end-to-end
  export       Replay raw WAL events (Keanu handoff) with integrity checks

Usage:
  ./venv/bin/python scripts/guardianctl.py status
  ./venv/bin/python scripts/guardianctl.py files --json
  ./venv/bin/python scripts/guardianctl.py permissions
  ./venv/bin/python scripts/guardianctl.py permissions --apply   # operator action on production data
  GUARDIAN_API_KEY=flip... ./venv/bin/python scripts/guardianctl.py rotate
  ./venv/bin/python scripts/guardianctl.py export --verify --out dataset.jsonl

Note: `status` and `rotate` talk to the running Guardian API.
      `status` also reports the configured retention and the on-disk
      capture file inventory (active/rotated files, feedback C11).
      `config`, `enable`, `disable` read/modify settings.yaml directly.
      `files`, `export`, `permissions` inspect the filesystem.
      `export` replays the RAW WAL (Guardian stores raw since 2026-08-26;
      redaction is Keanu's job via scripts/keanu_redact.py).
      `permissions` reports drift only; `--apply` is the single code path
      that changes a mode, and it prints every change (issue #131).
"""

import argparse
import contextlib
import errno
import json
import os
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _paths import DATA_DIR, REPO_ROOT

# The capture-configuration error lives in app.capture.errors, and nothing on
# the path to it imports PyYAML at module scope: `--help` and the CLI's own
# "PyYAML not installed" message have to work before any configuration is
# touched (issue #135).  Importing this module still runs app/capture/__init__.py,
# so that invariant covers the whole package, not just this file; it is pinned
# by tests/unit/test_capture_import_path.py.  `app.capture.config` re-exports
# the same class, so the parser and this import can never drift apart.
from app.capture.errors import CaptureConfigError

# Capture config lives in the global settings file (config-schema, 2026-08-21).
# Resolve through app.paths.global_settings_file() so an installation still on
# the legacy settings.yaml keeps reading/writing its current file instead of a
# divergent new one (matches the migrated readers).
from app.paths import global_settings_file

SETTINGS_YAML = global_settings_file()
CAPTURE_ROOT = DATA_DIR / "capture"


def _load_yaml() -> dict:
    """Load settings.yaml and return the capture section."""
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML not installed. Run: ./venv/bin/pip install pyyaml")
    data = yaml.safe_load(SETTINGS_YAML.read_text())
    return data.get("capture", {})


def _save_yaml_capture(capture_section: dict) -> None:
    """Update the capture: section in settings.yaml in-place."""
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML not installed. Run: ./venv/bin/pip install pyyaml")
    data = yaml.safe_load(SETTINGS_YAML.read_text())
    data["capture"] = capture_section
    SETTINGS_YAML.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True))
    print(f"✅ Updated {SETTINGS_YAML.relative_to(REPO_ROOT)}")
    print("⚠️  Requires server restart: sudo systemctl restart guardian-llmprovider-gateway")


def _api_request(method: str, endpoint: str, *, base_url: str = "http://127.0.0.1:11434", json_body: dict | None = None) -> dict:
    """Make an HTTP request to the Guardian API."""
    import httpx
    from _auth import resolve_api_key

    headers = {"Authorization": f"Bearer {resolve_api_key()}"}
    url = f"{base_url}{endpoint}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.request(method, url, headers=headers, json=json_body)
            if resp.status_code >= 400:
                print(f"❌ API returned {resp.status_code}: {resp.text}", file=sys.stderr)
                raise SystemExit(1)
            return resp.json()
    except httpx.ConnectError:
        print(f"❌ Cannot connect to Guardian at {base_url}", file=sys.stderr)
        print("   Is the server running? Check: sudo systemctl status guardian-llmprovider-gateway", file=sys.stderr)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Retention / on-disk visibility (feedback C11)
# ---------------------------------------------------------------------------

def _fmt_age(seconds: int) -> str:
    """Human-readable age (e.g. ``42s``, ``13m``, ``5h``, ``9d``)."""
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _capture_retention_summary(cfg: Any | None = None) -> dict:
    """Retention config + actual on-disk capture state (no API needed).

    Retention is enforced inside the WAL writer, so it is invisible in the
    runtime API; this summary combines the configured values (capture
    config) with the file inventory on disk so operators can see what
    retention is set to and what it is actually doing (feedback C11).

    ``cfg`` defaults to the loaded capture config; tests inject a config
    bound to a temporary capture root.
    """
    from app.capture.wal_writer import ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME

    if cfg is None:
        from app.capture.config import load_capture_config
        cfg = load_capture_config()

    root = Path(cfg.capture_root)
    if not root.is_absolute():
        root = REPO_ROOT / root

    summary: dict = {
        "retention_days": cfg.retention_days,
        "max_capture_bytes": cfg.max_capture_bytes,
        "max_file_bytes": cfg.max_file_bytes,
        "max_file_age_seconds": cfg.max_file_age_seconds,
        "capture_root": str(root),
        "active_file": None,
        "rotated_files": 0,
        "oldest_rotated_file": None,
        "newest_rotated_file": None,
    }
    if not root.is_dir():
        return summary

    now = time.time()

    def _info(path: Path) -> dict:
        stat = path.stat()
        return {
            "name": path.name,
            "age_seconds": max(0, int(now - stat.st_mtime)),
            "size_bytes": stat.st_size,
        }

    # Active file: plain (new format) or legacy gzip (migrates at next
    # writer start).
    for name, fmt in ((ACTIVE_FILENAME, "plain"), (LEGACY_ACTIVE_FILENAME, "legacy_gzip")):
        candidate = root / name
        if candidate.is_file():
            info = _info(candidate)
            info["format"] = fmt
            summary["active_file"] = info
            break

    # Rotated (completed) .jsonl.gz archive, oldest first by name — the
    # timestamp prefix makes lexicographic order chronological.
    rotated = sorted(
        (p for p in root.glob("guardian_capture_*.jsonl.gz")
         if p.name not in (ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME) and p.is_file()),
        key=lambda p: p.name,
    )
    summary["rotated_files"] = len(rotated)
    if rotated:
        summary["oldest_rotated_file"] = _info(rotated[0])
        summary["newest_rotated_file"] = _info(rotated[-1])
    return summary


def cmd_status(args: argparse.Namespace) -> None:
    """Show capture subsystem status via the running Guardian API."""
    result = _api_request("GET", "/api/capture/status")
    if args.json:
        try:
            result["retention"] = _capture_retention_summary()
        except Exception as exc:
            result["retention"] = {"error": str(exc)}
        print(json.dumps(result, indent=2, default=str))
        return

    cfg = result.get("config", {})
    runtime = result.get("runtime", {})
    print("━" * 60)
    print("  Guardian Capture Status")
    print("━" * 60)
    print(f"  Enabled:          {cfg.get('enabled', '?')}")
    print(f"  Active:           {cfg.get('active', '?')}")
    print(f"  Local capture:   {cfg.get('local_capture', '?')}")
    print(f"  Cloud capture:    {cfg.get('cloud_capture', '?')}")
    print(f"  Per-client opt-in: {cfg.get('per_client_opt_in', '?')}")
    print(f"  Policy version:   {cfg.get('policy_version', '?')}")
    print(f"  Instance ID:      {cfg.get('instance_id', '?')}")
    print(f"  Capture root:     {cfg.get('capture_root', '?')}")
    print()
    print("  Field policies:")
    for k, v in (cfg.get("field_policies") or {}).items():
        print(f"    {k:30s} {v}")
    print()
    if runtime:
        print("  Runtime:")
        for k, v in runtime.items():
            print(f"    {k:30s} {v}")
    print()
    print("  Retention & capture files (local disk):")
    try:
        ret = _capture_retention_summary()
    except Exception as exc:
        ret = None
        print(f"    (unavailable: {exc})")
    if ret is not None:
        policy = "keep forever" if ret["retention_days"] < 0 else f"{ret['retention_days']} day(s)"
        budget = ("unlimited" if ret["max_capture_bytes"] < 0
                  else f"{ret['max_capture_bytes']:,} bytes")
        print(f"    retention_days:      {ret['retention_days']}  ({policy})")
        print(f"    max_capture_bytes:   {ret['max_capture_bytes']}  ({budget})")
        print(f"    rotation thresholds: {ret['max_file_bytes']:,} bytes or {ret['max_file_age_seconds']}s per file")
        active = ret.get("active_file")
        if active:
            fmt_label = ("plain (new format — mid-write readable)"
                         if active["format"] == "plain"
                         else "legacy gzip (renamed at next writer start)")
            print(f"    Active file:         {active['name']}  "
                  f"({active['size_bytes']:,} bytes, {fmt_label})")
        else:
            print("    Active file:         (none yet — created on first capture event)")
        if ret.get("oldest_rotated_file"):
            oldest = ret["oldest_rotated_file"]
            newest = ret["newest_rotated_file"]
            print(f"    Oldest rotated file: {oldest['name']}  (age {_fmt_age(oldest['age_seconds'])})")
            print(f"    Newest rotated file: {newest['name']}  (age {_fmt_age(newest['age_seconds'])})")
        print(f"    Rotated files:       {ret['rotated_files']}")
    print("━" * 60)


def cmd_config(args: argparse.Namespace) -> None:
    """Show effective capture configuration from settings.yaml."""
    capture = _load_yaml()
    if args.json:
        print(json.dumps(capture, indent=2, default=str))
        return

    print("━" * 60)
    print("  Capture Configuration (settings.yaml)")
    print("━" * 60)
    for k, v in capture.items():
        if isinstance(v, (list, dict)):
            print(f"  {k}:")
            if isinstance(v, list):
                for item in v:
                    print(f"    - {item}")
            else:
                for sk, sv in v.items():
                    print(f"    {sk}: {sv}")
        else:
            print(f"  {k:30s} {v}")
    print("━" * 60)


def cmd_files(args: argparse.Namespace) -> None:
    """List capture WAL files on disk."""
    if not CAPTURE_ROOT.exists():
        print(f"Capture root does not exist: {CAPTURE_ROOT}")
        return

    files = sorted(CAPTURE_ROOT.rglob("*.jsonl*"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        print("No capture files found.")
        return

    if args.json:
        out = [
            {
                "path": os.path.relpath(str(f), str(REPO_ROOT)),
                "size_bytes": f.stat().st_size,
                "modified": f.stat().st_mtime,
                "suffix": f.suffix,
            }
            for f in files
        ]
        print(json.dumps(out, indent=2))
        return

    total_bytes = sum(f.stat().st_size for f in files)
    print(f"{'File':<50s} {'Size':>12s}  Type")
    print("─" * 72)
    for f in files:
        size = f.stat().st_size
        if f.name.startswith("guardian_capture_current"):
            # The active WAL is plain .jsonl since 2026-08-30 (feedback C3:
            # readable line-by-line while the writer is mid-stream); a gzip
            # "current" file is a legacy leftover from the previous
            # stream-gzip writer (renamed by the next writer start).
            ftype = "active (plain)" if f.suffix == ".jsonl" else "legacy (gzip)"
        elif f.suffix == ".sha256":
            ftype = "checksum"
        elif f.suffix == ".gz":
            ftype = "gzip (rotated)"
        else:
            ftype = "plain (pending gzip at next writer start)"
        print(f"{os.path.relpath(str(f), str(REPO_ROOT)):<50s} {size:>12,}  {ftype}")
    print("─" * 72)
    print(f"{'Total:':<50s} {total_bytes:>12,}")
    print(f"\nTotal files: {len(files)}")
    print(f"Total size:  {total_bytes / (1024**2):.2f} MB")


# ---------------------------------------------------------------------------
# Permission drift on the capture tree (issue #131)
# ---------------------------------------------------------------------------
#
# Every capture artefact — the plain WAL, the rotated ``.jsonl.gz`` archives,
# their ``.sha256`` sidecars, extracted media and the directories holding them —
# is supposed to carry the modes in capture configuration
# (``CaptureConfig.file_mode``/``directory_mode``).  The writers apply them since
# commit 5a174dc, but nothing detects artefacts written *before* that (or by an
# older checkout): the tree is the only record, and "is this right?" was
# answered with a hand-typed ``chmod -R`` against a hardcoded mode.
#
# This subcommand is that check, with the modes taken from configuration and the
# mutation behind an explicit flag.  Nothing here runs on its own and nothing
# changes without ``--apply``; ``report`` is the default and reads only mode
# bits (never file content, so an owner-unreadable file still gets reported).

#: Owner access the configuration implies on any artefact: a file must stay
#: readable and writable by its owner, a directory traversable by it.  A mode
#: that lacks those bits drifts even though it is *stricter* than configured —
#: the WAL writer could no longer append, the consumer could no longer travel.
_OWNER_IMPLIED_BITS = 0o700

#: Exit status: a check needs a stable contract, so the values are named.
PERMISSIONS_EXIT_CLEAN = 0
PERMISSIONS_EXIT_DRIFT = 1
PERMISSIONS_EXIT_ERROR = 2
PERMISSIONS_EXIT_INTERRUPTED = 130  # 128 + SIGINT

#: Mode-bit names for human drift reasons, widest first.
_MODE_BIT_NAMES: tuple[tuple[int, str], ...] = (
    (0o4000, "setuid"),
    (0o2000, "setgid"),
    (0o1000, "sticky"),
    (0o400, "owner-read"),
    (0o200, "owner-write"),
    (0o100, "owner-execute"),
    (0o040, "group-read"),
    (0o020, "group-write"),
    (0o010, "group-execute"),
    (0o004, "world-read"),
    (0o002, "world-write"),
    (0o001, "world-execute"),
)


#: Errors that mean "this entry disappeared" — routine in a live tree that
#: rotates and purges between the listing and the stat.  Every *other* error is
#: a failure to audit, and reporting it as "skipped" would let a partial walk
#: masquerade as a clean result.  ``ENOTDIR`` is deliberately **not** here: a
#: path that used to be a directory and is now a file (or a symlink) was
#: *replaced*, and calling that "vanished" would let ``--apply`` exit 0 while
#: the replacement keeps a world-readable mode.
_VANISHED_ERRNOS = frozenset({errno.ENOENT, getattr(errno, "ESTALE", errno.ENOENT)})

#: Flags for opening a directory that is part of the walk: read-only (so it can
#: be listed and used as a ``dir_fd``), a real directory (``O_DIRECTORY``) and
#: never a symlink (``O_NOFOLLOW``).  A directory swapped for a symlink cannot
#: pull the walk out of the capture tree, because no path is ever re-resolved —
#: the child is opened relative to the parent's descriptor.
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)

#: Flags for opening one audited entry to change its mode.  ``O_NONBLOCK`` is
#: load-bearing: a FIFO swapped in for the entry would block a plain
#: ``O_RDONLY`` open forever, so ``fstat`` — and every check after it — would
#: never run and the command would hang instead of failing.
_ENTRY_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: Depth guard for the descriptor walk: recursion holds one descriptor per
#: level, so an unbounded depth would exhaust them.  A capture tree is flat
#: (root plus ``media/``), so this only fires on something pathological.
_MAX_WALK_DEPTH = 128


class CaptureTreeError(RuntimeError):
    """Part of the capture tree could not be audited; the result is incomplete."""


@dataclass(frozen=True)
class FileIdentity:
    """What identifies one filesystem object: its device and inode.

    Recorded for every artefact the walk sees *and* for the directory holding
    it, then re-checked on the descriptor ``--apply`` is about to change.  An
    entry whose identity moved is not the entry that was audited, whatever the
    path says by then — inode identity cannot be fooled by a rename, a symlink
    or a reused name.
    """

    device: int
    inode: int

    @classmethod
    def of(cls, stat_result: os.stat_result) -> "FileIdentity":
        return cls(stat_result.st_dev, stat_result.st_ino)


@dataclass(frozen=True)
class PermissionDrift:
    """One capture artefact whose mode does not match the configuration."""

    path: Path
    kind: str  # "file" | "directory"
    actual_mode: int
    expected_mode: int
    reasons: tuple[str, ...]
    #: Identity observed by the walk; ``--apply`` re-checks it on the descriptor.
    identity: FileIdentity
    #: Directory holding the entry and the identity the walk saw for it, so the
    #: correction can re-open that directory and reach the entry by name
    #: relative to it.  ``None`` for the scan root, which is opened by its own
    #: (resolved) path.
    parent: Path | None
    parent_identity: FileIdentity | None
    name: str


@dataclass(frozen=True)
class PermissionChange:
    """One artefact corrected by ``--apply`` (audit trail for the operator).

    ``old_mode`` is the mode read from the descriptor at correction time, not
    the mode the scan saw: if anything tightened the entry in between, that is
    what the operator needs to see next to the new mode.
    """

    path: Path
    kind: str
    old_mode: int
    new_mode: int


@dataclass
class CorrectionResult:
    """What one ``--apply`` pass did (also when it was interrupted)."""

    changes: list[PermissionChange] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    interrupted: bool = False


@dataclass
class PermissionScan:
    """Result of walking the capture tree once."""

    root: Path
    file_mode: int
    directory_mode: int
    drift: list[PermissionDrift]
    files_checked: int = 0
    directories_checked: int = 0
    skipped: list[str] = field(default_factory=list)


def _capture_settings(settings_path: Path | None = None) -> Any:
    """Return the validated :class:`CaptureConfig` for permission work.

    Deliberately strict about *reaching* the configuration:
    :func:`app.capture.config.load_capture_config` falls back to its defaults
    when the file is unreadable or unparsable.  That is right for a fail-open
    writer, and wrong for a permission audit — measured: the fallback replaces a
    configured ``0600``/``0700`` with ``0640``/``0750``, i.e. it grants group
    read.  So a missing file, a broken YAML document, an absent ``capture:``
    section or a rejected mode raises instead, and the caller turns that into a
    clear error.

    The file is read **once**.  The validated document is handed to
    :func:`app.capture.config.capture_config_from_section` rather than to the
    runtime loader, so a file that vanishes or changes after validation cannot
    swap the modes this audit will ``chmod`` to.  The modes still come from
    :class:`CaptureConfig` (``file_mode``/``directory_mode``, validated against
    the no-world-access rule) — never from a literal in this script.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - venv always has PyYAML
        raise CaptureConfigError(
            "PyYAML not installed. Run: ./venv/bin/pip install pyyaml"
        ) from exc

    path = Path(settings_path if settings_path is not None else SETTINGS_YAML)
    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        # A settings file that is not valid UTF-8 raises UnicodeDecodeError,
        # which is a ValueError and not an OSError: unwrapped it escaped as a
        # traceback with exit 1 instead of the documented config error (exit 2).
        detail = getattr(exc, "strerror", None) or str(exc)
        raise CaptureConfigError(
            f"cannot read capture configuration {path}: {detail}"
        ) from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CaptureConfigError(
            f"cannot parse capture configuration {path}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise CaptureConfigError(
            f"capture configuration {path} is not a YAML mapping"
        )
    if not isinstance(document.get("capture"), dict):
        raise CaptureConfigError(
            f"capture configuration {path} has no 'capture' section"
        )

    from app.capture.config import capture_config_from_section

    # Build from the document that was just validated: one read, so nothing can
    # substitute a different mode between the validation and this line.
    try:
        return capture_config_from_section(document["capture"])
    except (TypeError, ValueError) as exc:
        raise CaptureConfigError(
            f"capture configuration {path} is invalid: {exc}"
        ) from exc


def _capture_root(cfg: Any) -> Path:
    """Resolve ``cfg.capture_root`` the same way the WAL writer does."""
    root = Path(cfg.capture_root)
    if not root.is_absolute():
        root = REPO_ROOT / root
    return root


def _describe_mode_bits(bits: int) -> str:
    """Human names for a set of mode bits (``0o004`` → ``world-read``)."""
    names = [name for bit, name in _MODE_BIT_NAMES if bits & bit]
    return ", ".join(names) if names else format(bits, "04o")


def _permission_drift(actual_mode: int, expected_mode: int) -> tuple[str, ...]:
    """Return why ``actual_mode`` drifts from ``expected_mode`` (empty = clean).

    Drift is exactly two things (issue #131): the entry grants a bit the
    configured mode does not (looser than configuration), or it lacks owner
    access the configured mode implies (unusable for the writer/consumer).  A
    mode that is merely *tighter* — no extra bits, owner access intact — is
    correct and stays untouched, which is why the owner-only
    ``.capture_state.json`` (``0600``) and legacy owner-only media files report
    clean instead of being widened by ``--apply``.
    """
    extra = actual_mode & ~expected_mode & 0o7777
    missing_owner = expected_mode & _OWNER_IMPLIED_BITS & ~actual_mode
    reasons: list[str] = []
    if extra:
        reasons.append(
            f"looser than {format(expected_mode, '04o')}: {_describe_mode_bits(extra)}"
        )
    if missing_owner:
        reasons.append(f"owner access missing: {_describe_mode_bits(missing_owner)}")
    return tuple(reasons)


def _scan_permission_drift(
    root: Path, *, file_mode: int, directory_mode: int
) -> PermissionScan:
    """Walk ``root`` and collect every drifting file and directory.

    The walk is **descriptor-based**: the root is opened once, every directory
    is opened with ``O_DIRECTORY | O_NOFOLLOW`` *relative to its parent's
    descriptor*, its identity (``st_dev``/``st_ino``) is compared against what
    the listing said, and the children are read with ``os.scandir(fd)`` and
    ``os.stat(name, dir_fd=fd)``.  No path is ever re-resolved, so a directory
    swapped for a symlink between the listing and the descent is refused
    (``ENOTDIR``/``ELOOP``) instead of pulling the audit — and a later
    ``--apply`` — into a tree outside the capture root; every child inherits
    the protection of the descriptor it was reached through.

    Nothing is opened for reading: an entry's mode comes from ``stat``, so an
    owner-unreadable file is still reported.

    Failure discipline: only *disappearance* is tolerated (``ENOENT``,
    ``ESTALE`` — routine in a live tree that rotates and purges) and recorded as
    skipped.  Any other error — an unreadable directory, a replacement, an I/O
    error — raises :class:`CaptureTreeError`: a walk that never entered a
    directory cannot vouch for what is inside it, and reporting a partial tree
    as clean is exactly the false confidence this check exists to prevent.
    """
    scan = PermissionScan(
        root=root, file_mode=file_mode, directory_mode=directory_mode, drift=[]
    )

    def _consider(
        path: Path,
        kind: str,
        entry_stat: os.stat_result,
        *,
        parent: Path | None,
        parent_identity: FileIdentity | None,
    ) -> None:
        expected = directory_mode if kind == "directory" else file_mode
        if kind == "directory":
            scan.directories_checked += 1
        else:
            scan.files_checked += 1
        mode = stat.S_IMODE(entry_stat.st_mode)
        reasons = _permission_drift(mode, expected)
        if reasons:
            scan.drift.append(
                PermissionDrift(
                    path=path,
                    kind=kind,
                    actual_mode=mode,
                    expected_mode=expected,
                    reasons=reasons,
                    identity=FileIdentity.of(entry_stat),
                    parent=parent,
                    parent_identity=parent_identity,
                    name=path.name,
                )
            )

    def _walk(
        directory_fd: int,
        directory: Path,
        directory_identity: FileIdentity,
        depth: int,
    ) -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            if exc.errno in _VANISHED_ERRNOS:
                scan.skipped.append(f"{directory}: vanished during the walk")
                return
            raise CaptureTreeError(
                f"cannot list directory {directory}: {exc.strerror or exc}"
            ) from exc

        for child in children:
            name = child.name
            path = directory / name
            try:
                child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                if exc.errno in _VANISHED_ERRNOS:
                    scan.skipped.append(f"{path}: vanished during the walk")
                    continue
                raise CaptureTreeError(
                    f"cannot inspect {path}: {exc.strerror or exc}"
                ) from exc

            if stat.S_ISLNK(child_stat.st_mode):
                scan.skipped.append(f"{path}: symlink, not followed")
                continue

            if stat.S_ISDIR(child_stat.st_mode):
                child_identity = FileIdentity.of(child_stat)
                try:
                    child_fd = os.open(
                        name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd
                    )
                except OSError as exc:
                    if exc.errno in _VANISHED_ERRNOS:
                        scan.skipped.append(f"{path}: vanished during the walk")
                        continue
                    detail = exc.strerror or str(exc)
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        detail += (
                            " — it is no longer the directory that was listed, "
                            "refusing to follow it"
                        )
                    raise CaptureTreeError(
                        f"cannot open directory {path}: {detail}"
                    ) from exc
                try:
                    if FileIdentity.of(os.fstat(child_fd)) != child_identity:
                        raise CaptureTreeError(
                            f"directory {path} was replaced during the walk; "
                            "refusing to audit it"
                        )
                    _consider(
                        path,
                        "directory",
                        child_stat,
                        parent=directory,
                        parent_identity=directory_identity,
                    )
                    if depth >= _MAX_WALK_DEPTH:
                        raise CaptureTreeError(
                            f"capture tree is deeper than {_MAX_WALK_DEPTH} "
                            f"levels at {path}"
                        )
                    _walk(child_fd, path, child_identity, depth + 1)
                finally:
                    os.close(child_fd)
                continue

            if stat.S_ISREG(child_stat.st_mode):
                _consider(
                    path,
                    "file",
                    child_stat,
                    parent=directory,
                    parent_identity=directory_identity,
                )
                continue

            scan.skipped.append(f"{path}: not a regular file or directory")

    # The root is opened by its own path — it is the anchor the operator named
    # (``cmd_permissions`` resolves it first, so it is not a symlink).  A
    # missing root raises FileNotFoundError and a non-directory NotADirectoryError
    # (O_DIRECTORY), both reported as scan errors by the caller.
    root_fd = os.open(root, _DIRECTORY_OPEN_FLAGS)
    try:
        root_stat = os.fstat(root_fd)
        _consider(
            root,
            "directory",
            root_stat,
            parent=None,
            parent_identity=None,
        )
        _walk(root_fd, root, FileIdentity.of(root_stat), 1)
    finally:
        os.close(root_fd)

    scan.drift.sort(key=lambda entry: str(entry.path))
    return scan


class _CorrectionRefused(Exception):
    """A correction was refused before it could change anything."""


@contextlib.contextmanager
def _opened_for_correction(entry: PermissionDrift) -> Iterator[int]:
    """Open the exact object ``entry`` was audited as, for an ``fchmod``.

    The entry is reached through its parent's descriptor: the parent is opened
    with ``O_DIRECTORY | O_NOFOLLOW``, its identity is re-checked against what
    the walk saw, and the entry is then opened *by name relative to it* with
    ``O_NOFOLLOW``.  Nothing is opened through a path that could have been
    re-pointed since the scan, so a directory swapped for a symlink cannot
    redirect the mode change at a file outside the capture tree.

    ``O_NONBLOCK`` is load-bearing: a FIFO swapped in for the entry would
    otherwise block this ``O_RDONLY`` open forever and every check after it
    would never run.

    The scan root has no audited parent and is opened by its own resolved path;
    the identity check in :func:`_apply_permission_fixes` still applies to it.
    """
    descriptors: list[int] = []
    try:
        if entry.parent is None:
            flags = _DIRECTORY_OPEN_FLAGS if entry.kind == "directory" else _ENTRY_OPEN_FLAGS
            descriptor = os.open(entry.path, flags)
        else:
            parent_fd = os.open(entry.parent, _DIRECTORY_OPEN_FLAGS)
            descriptors.append(parent_fd)
            if entry.parent_identity is not None:
                if FileIdentity.of(os.fstat(parent_fd)) != entry.parent_identity:
                    raise _CorrectionRefused(
                        "its directory was replaced since the scan"
                    )
            flags = _ENTRY_OPEN_FLAGS
            if entry.kind == "directory":
                flags |= os.O_DIRECTORY
            descriptor = os.open(entry.name, flags, dir_fd=parent_fd)
        descriptors.append(descriptor)
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _print_change(change: PermissionChange, entry: PermissionDrift) -> None:
    """Report one applied change immediately, on stderr, flushed.

    The audit trail is written the moment the ``fchmod`` succeeds, not when the
    run ends: an interrupted ``--apply`` (Ctrl-C while a later entry is being
    opened) must still leave a record of every mode it already changed.  It goes
    to stderr so ``--json`` keeps stdout a single clean document.
    """
    print(
        f"  FIXED {change.kind:<9s} "
        f"{format(change.old_mode, '04o')} -> {format(change.new_mode, '04o')}  "
        f"{_display_path(change.path)}  ({'; '.join(entry.reasons)})",
        file=sys.stderr,
        flush=True,
    )


def _apply_permission_fixes(scan: PermissionScan) -> CorrectionResult:
    """Normalise every drifting entry to its configured mode.

    Returns a :class:`CorrectionResult`; ``interrupted`` is set when the operator
    stops the run part-way, and the changes made before that point are already
    on stderr.  This is the only function in the CLI that changes a mode on
    capture data, and it only ever receives entries
    :func:`_scan_permission_drift` flagged.

    Every correction is checked on the descriptor it is about to change:

    * the identity (``st_dev``/``st_ino``) must be the one the scan recorded,
      otherwise the path was replaced and the entry is not corrected;
    * the node must still be the audited *type* (regular file or directory), so
      a FIFO or a file standing in for a directory is refused;
    * the drift is **recomputed from the mode read now** against the configured
      mode — an entry someone else tightened in the meantime no longer drifts
      and is left alone.  ``--apply`` never widens a mode because of a stale
      scan result.

    Each drifting entry is then set *exactly* to its configured mode, and the
    change is printed (stderr, flushed) before the loop moves on.  A refused
    symlink or replacement (``ELOOP``/``ENOTDIR``), an unopenable entry
    (``EACCES``: the same mask that makes it drift also denies the ``O_RDONLY``
    the correction needs) and any other error are reported failures with a
    non-zero exit — never a silent skip.  Entries that merely vanished need no
    repair and are listed separately.
    """
    result = CorrectionResult()
    try:
        for entry in scan.drift:
            try:
                with _opened_for_correction(entry) as descriptor:
                    opened_stat = os.fstat(descriptor)
                    if FileIdentity.of(opened_stat) != entry.identity:
                        result.failures.append(
                            f"{entry.path}: replaced since the scan (inode changed) — not corrected"
                        )
                        continue
                    if entry.kind == "directory" and not stat.S_ISDIR(opened_stat.st_mode):
                        result.failures.append(
                            f"{entry.path}: no longer a directory — not corrected"
                        )
                        continue
                    if entry.kind == "file" and not stat.S_ISREG(opened_stat.st_mode):
                        result.failures.append(
                            f"{entry.path}: no longer a regular file — not corrected"
                        )
                        continue
                    current_mode = stat.S_IMODE(opened_stat.st_mode)
                    if not _permission_drift(current_mode, entry.expected_mode):
                        result.unchanged.append(
                            f"{entry.path}: no longer drifts "
                            f"({format(current_mode, '04o')} now satisfies "
                            f"{format(entry.expected_mode, '04o')}) — left untouched"
                        )
                        continue
                    os.fchmod(descriptor, entry.expected_mode)
                    change = PermissionChange(
                        path=entry.path,
                        kind=entry.kind,
                        old_mode=current_mode,
                        new_mode=entry.expected_mode,
                    )
                    result.changes.append(change)
                    _print_change(change, entry)
            except _CorrectionRefused as exc:
                result.failures.append(f"{entry.path}: {exc}")
            except OSError as exc:
                if exc.errno in _VANISHED_ERRNOS:
                    result.vanished.append(
                        f"{entry.path}: vanished before it could be corrected"
                    )
                elif exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    result.failures.append(
                        f"{entry.path}: is now a symlink or a different type — "
                        "refusing to follow it (nothing outside the audited entry "
                        "was touched)"
                    )
                elif exc.errno == errno.EACCES:
                    result.failures.append(
                        f"{entry.path}: cannot open it to correct it "
                        f"({exc.strerror or exc}) — fix this one from a shell"
                    )
                else:
                    result.failures.append(f"{entry.path}: {exc.strerror or exc}")
    except KeyboardInterrupt:
        # Every change up to this point is already on stderr; report the partial
        # result honestly instead of dying in a traceback with no audit trail.
        result.interrupted = True
    return result


def _display_path(path: Path) -> str:
    """Repo-relative when inside the checkout, absolute otherwise."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _print_notes(label: str, notes: list[str]) -> None:
    """Surface per-entry notes on stderr without flooding it."""
    for note in notes[:10]:
        print(f"  {label}: {note}", file=sys.stderr)
    if len(notes) > 10:
        print(f"  {label}: … and {len(notes) - 10} more", file=sys.stderr)


def cmd_permissions(args: argparse.Namespace) -> None:
    """Report permission drift on the capture tree; ``--apply`` fixes it."""
    apply = bool(getattr(args, "apply", False))
    as_json = bool(getattr(args, "json", False))

    try:
        cfg = _capture_settings()
    except CaptureConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print(
            "   Refusing to compare the capture tree against a guessed mode "
            "(fix the capture configuration first).",
            file=sys.stderr,
        )
        raise SystemExit(PERMISSIONS_EXIT_ERROR)

    root = Path(args.root).expanduser() if getattr(args, "root", None) else _capture_root(cfg)
    # Absolute (and symlink-resolved) before anything scans or reports it: the
    # JSON contract promises absolute paths, and a relative ``--root`` would
    # otherwise leak a cwd-relative root and relative entry paths into it.
    root = root.resolve()
    try:
        scan = _scan_permission_drift(
            root,
            file_mode=int(cfg.file_mode),
            directory_mode=int(cfg.directory_mode),
        )
    except CaptureTreeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print(
            "   The tree was not fully audited — refusing to report a result "
            "(an incomplete walk can hide drift).",
            file=sys.stderr,
        )
        raise SystemExit(PERMISSIONS_EXIT_ERROR)
    except OSError as exc:
        print(f"❌ Cannot scan capture tree {root}: {exc.strerror or exc}", file=sys.stderr)
        raise SystemExit(PERMISSIONS_EXIT_ERROR)

    result = CorrectionResult()
    if apply and scan.drift:
        result = _apply_permission_fixes(scan)
    changes = result.changes
    failures = result.failures
    vanished = result.vanished
    unchanged = result.unchanged

    if as_json:
        print(
            json.dumps(
                {
                    "root": str(scan.root),
                    "file_mode": format(scan.file_mode, "04o"),
                    "directory_mode": format(scan.directory_mode, "04o"),
                    "apply": apply,
                    "checked": {
                        "files": scan.files_checked,
                        "directories": scan.directories_checked,
                    },
                    # Absolute paths: a machine consumer must be able to act
                    # on an entry without guessing a base directory.
                    "drift": [
                        {
                            "path": str(entry.path),
                            "kind": entry.kind,
                            "actual_mode": format(entry.actual_mode, "04o"),
                            "expected_mode": format(entry.expected_mode, "04o"),
                            "reasons": list(entry.reasons),
                        }
                        for entry in scan.drift
                    ],
                    "changed": [
                        {
                            "path": str(change.path),
                            "kind": change.kind,
                            "old_mode": format(change.old_mode, "04o"),
                            "new_mode": format(change.new_mode, "04o"),
                        }
                        for change in changes
                    ],
                    "interrupted": result.interrupted,
                    "failures": failures,
                    "vanished": vanished,
                    "unchanged": unchanged,
                    "skipped": scan.skipped,
                },
                indent=2,
            )
        )
    else:
        print("━" * 66)
        print("  Guardian capture permissions")
        print("━" * 66)
        print(f"  Capture root:   {_display_path(scan.root)}")
        print(f"  file_mode:      {format(scan.file_mode, '04o')}  (from capture configuration)")
        print(f"  directory_mode: {format(scan.directory_mode, '04o')}  (from capture configuration)")
        if apply:
            print("  Mode:           APPLY — drifting entries are normalised")
        else:
            print("  Mode:           REPORT (default, safe) — nothing is changed; add --apply to fix")
        print()
        applied = {change.path: change for change in changes}
        for entry in scan.drift:
            # A corrected entry reports the mode the correction actually saw —
            # not the mode the scan saw, which can be stale by then.
            change = applied.get(entry.path)
            old_mode = change.old_mode if change is not None else entry.actual_mode
            marker = "FIXED" if change is not None else "DRIFT"
            print(
                f"  {marker:<5s} {entry.kind:<9s} "
                f"{format(old_mode, '04o')} -> {format(entry.expected_mode, '04o')}  "
                f"{_display_path(entry.path)}  ({'; '.join(entry.reasons)})"
            )
        if not scan.drift:
            print("  No drift: every capture artefact matches the configured modes.")
        print()
        satisfied = (
            f", {len(unchanged)} already satisfied" if unchanged else ""
        )
        print(
            f"  Checked {scan.files_checked} file(s) and "
            f"{scan.directories_checked} directories: {len(scan.drift)} drifting, "
            f"{len(changes)} corrected{satisfied}, {len(failures)} failed."
        )
        if result.interrupted:
            print(
                f"  INTERRUPTED after {len(changes)} correction(s) — every change "
                "already made is printed above and on stderr; re-run to finish."
            )
        elif scan.drift and not apply:
            print("  Drift found — exit status 1. --apply normalises it (operator action on production data).")
        print("━" * 66)

    _print_notes("skipped", scan.skipped + vanished)
    _print_notes("changed meanwhile", unchanged)
    for failure in failures:
        print(f"❌ {failure}", file=sys.stderr)

    if result.interrupted:
        raise SystemExit(PERMISSIONS_EXIT_INTERRUPTED)
    if failures:
        raise SystemExit(PERMISSIONS_EXIT_DRIFT)
    if apply:
        raise SystemExit(PERMISSIONS_EXIT_CLEAN)
    raise SystemExit(PERMISSIONS_EXIT_DRIFT if scan.drift else PERMISSIONS_EXIT_CLEAN)


def cmd_rotate(args: argparse.Namespace) -> None:
    """Force rotation of the active capture file."""
    result = _api_request("POST", "/api/capture/rotate")
    print(f"✅ {result.get('message', 'Rotation triggered')}")
    if "rotated_file" in result:
        print(f"   Rotated: {result['rotated_file']}")
    if "active_file" in result:
        print(f"   Active:  {result['active_file']}")


def cmd_enable(args: argparse.Namespace) -> None:
    """Enable capture in settings.yaml."""
    capture = _load_yaml()
    if args.local_only:
        capture["enabled"] = True
        capture["local_capture"] = True
        print("Enabling local capture only (cloud capture remains disabled)")
    elif args.full:
        capture["enabled"] = True
        capture["local_capture"] = True
        capture["cloud_capture"] = True
        print("Enabling full capture (local + cloud)")
        if not args.force:
            print("⚠️  Cloud capture requires provider terms review!")
            print("   Use --force to suppress this warning.")
            raise SystemExit(1)
    else:
        capture["enabled"] = True
        print("Enabling capture (local_capture and cloud_capture remain as-is)")

    _save_yaml_capture(capture)


def cmd_disable(args: argparse.Namespace) -> None:
    """Disable all capture in settings.yaml."""
    capture = _load_yaml()
    capture["enabled"] = False
    print("Disabling all capture (kill switch)")
    _save_yaml_capture(capture)


def cmd_test_event(args: argparse.Namespace) -> None:
    """Emit a synthetic test event to verify the capture pipeline."""
    from app.capture.config import load_capture_config
    from app.capture.schema import BuildContext, build_request_received_event
    from app.capture.sink import CaptureEvent, CaptureSink
    from app.capture.wal_writer import CaptureWALWriter

    cfg = load_capture_config()
    if not cfg.is_active:
        print("❌ Capture is not active. Enable it first:")
        print("   ./venv/bin/python scripts/guardianctl.py enable --local-only")
        raise SystemExit(1)

    async def _emit() -> None:
        import asyncio
        sink = CaptureSink(max_pending_events=cfg.max_pending_events)
        writer = CaptureWALWriter(sink, cfg)
        await writer.start()
        try:
            sink.try_put(CaptureEvent(data=event))
            await asyncio.sleep(0.5)  # let the writer drain
        finally:
            await writer.stop()

    ctx = BuildContext(
        request_id="test-" + str(int(__import__("time").time())),
        endpoint="/v1/chat/completions",
        ingress_protocol="openai",
        route_type="local",
        requested_model="test-model",
        resolved_model="test-model",
        capture_policy_version=cfg.policy_version,
        instance_id=cfg.instance_id,
        client_fingerprint="test-fingerprint",
    )
    event = build_request_received_event(
        cfg,
        ctx,
        request_messages=[{"role": "user", "content": "This is a test message"}],
        request_parameters={"temperature": 0.0},
    )
    __import__("asyncio").run(_emit())
    print(f"✅ Test event emitted: {event.get('event_id', '?')}")
    print(f"   Event type: {event.get('event_type', '?')}")
    print(f"   Schema:     {event.get('schema_name', '?')} v{event.get('schema_version', '?')}")
    print(f"   Request ID: {event.get('request_id', '?')}")


def _load_env_secret(name: str) -> str:
    """Load a secret from the environment, falling back to the repo .env."""
    import os

    val = os.environ.get(name, "")
    if val:
        return val
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            raw = raw.strip()
            if raw.startswith("#") or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def _iter_wal_events(
    root: Path,
    *,
    verify_auth: bool,
    verify_checksums: bool,
    secret: str,
) -> Any:
    """Yield (path, event_dict) for every WAL record, oldest file first.

    Order: completed files by name (timestamp_seq), then the active file
    (plain new format and/or legacy gzip).  Completed files are checked
    against their ``.sha256`` sidecar when ``verify_checksums``.  Records
    are read with the crash-tolerant reader (plain and gzip files through
    the same API; a pre-2026-08-30 active file may contain multi-member
    gzip from restarts after a crash).  With ``verify_auth`` (and a
    secret), every record's ``record_auth`` HMAC is validated.
    """
    import hashlib
    import hmac

    from app.capture.gzip_reader import iter_records
    from app.capture.wal_writer import ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME

    active_names = (ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME)

    completed: set = set(root.glob("guardian_capture_*.jsonl.gz"))
    completed.update(root.glob("guardian_capture_*.jsonl"))
    completed = {p for p in completed if p.is_file() and p.name not in active_names}
    files = sorted(completed, key=lambda p: p.name)
    # Active file(s) last — they hold the newest records.
    for name in active_names:
        active = root / name
        if active.is_file():
            files.append(active)

    for path in files:
        if verify_checksums and path.name not in active_names:
            sidecar = path.with_suffix(".sha256")
            if sidecar.exists():
                expected = sidecar.read_text().split()[0]
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != expected:
                    print(f"⚠️  CHECKSUM MISMATCH: {path.name}", file=sys.stderr)
        for record in iter_records(path):
            if not record.strip():
                continue
            try:
                event = json.loads(record.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                print(f"⚠️  Unparseable record in {path.name} (skipped)", file=sys.stderr)
                continue
            if verify_auth and secret:
                auth = event.pop("record_auth", None)
                canon = json.dumps(event, separators=(",", ":"), sort_keys=False, default=str)
                mac = hmac.new(secret.encode("utf-8"), canon.encode("utf-8"), hashlib.sha256).hexdigest()
                if auth is None or auth.get("mac") != mac:
                    print(
                        f"⚠️  RECORD_AUTH MISMATCH in {path.name}: "
                        f"event_id={event.get('event_id', '?')}",
                        file=sys.stderr,
                    )
                if auth is not None:
                    event["record_auth"] = auth
            yield path, event


def cmd_export(args: argparse.Namespace) -> None:
    """Replay raw WAL events with integrity checks (Keanu handoff)."""
    if not CAPTURE_ROOT.exists():
        print(f"Capture root does not exist: {CAPTURE_ROOT}")
        raise SystemExit(1)

    secret = _load_env_secret("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET")
    if args.no_verify:
        verify_auth = verify_checksums = False
    else:
        verify_auth = args.verify_auth or bool(secret)
        verify_checksums = args.verify_checksums

    out_fh = sys.stdout
    close_out = False
    if args.out and not args.verify_only:
        out_fh = open(args.out, "w", encoding="utf-8")
        close_out = True

    count = 0
    try:
        for path, event in _iter_wal_events(
            CAPTURE_ROOT,
            verify_auth=verify_auth,
            verify_checksums=verify_checksums,
            secret=secret,
        ):
            if not args.verify_only:
                out_fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            count += 1
    except OSError as exc:
        print(f"⚠️  Export aborted on unreadable stream: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if close_out:
            out_fh.close()

    if args.verify_only:
        print(f"✅ Verified {count} raw events — no data written", file=sys.stderr)
    else:
        print(f"✅ Exported {count} raw events", file=sys.stderr)
    if not verify_auth and not args.no_verify:
        print("   (record_auth verification skipped — no signing secret found)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="guardianctl",
        description="Guardian capture subsystem CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="Show capture status (requires running server)")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")
    p_status.set_defaults(func=cmd_status)

    # config
    p_config = sub.add_parser("config", help="Show capture config from settings.yaml")
    p_config.add_argument("--json", action="store_true", help="Output as JSON")
    p_config.set_defaults(func=cmd_config)

    # files
    p_files = sub.add_parser("files", help="List capture WAL files on disk")
    p_files.add_argument("--json", action="store_true", help="Output as JSON")
    p_files.set_defaults(func=cmd_files)

    # permissions
    p_perm = sub.add_parser(
        "permissions",
        help="Report capture-tree permission drift (report-only unless --apply)",
        description=(
            "Compare every capture artefact (WAL files, rotated .gz archives, "
            "checksum sidecars, media files and the directories holding them) "
            "against the configured capture.file_mode/capture.directory_mode. "
            "Report-only by default: nothing is changed and the exit status is "
            "non-zero when drift is found. --apply normalises the drifting "
            "entries and prints every change."
        ),
    )
    p_perm.add_argument("--apply", action="store_true",
                        help="Normalise drifting entries to the configured modes "
                             "(operator action on production data; dry-run otherwise)")
    p_perm.add_argument("--root", metavar="DIR",
                        help="Inspect DIR instead of the configured capture root")
    p_perm.add_argument("--json", action="store_true", help="Output as JSON")
    p_perm.set_defaults(func=cmd_permissions)

    # rotate
    p_rotate = sub.add_parser("rotate", help="Force rotation of active capture file (requires running server)")
    p_rotate.set_defaults(func=cmd_rotate)

    # enable
    p_enable = sub.add_parser("enable", help="Enable capture in settings.yaml")
    p_enable.add_argument("--local-only", action="store_true", help="Enable only local capture")
    p_enable.add_argument("--full", action="store_true", help="Enable local + cloud capture")
    p_enable.add_argument("--force", action="store_true", help="Force enable without confirmation")
    p_enable.set_defaults(func=cmd_enable)

    # disable
    p_disable = sub.add_parser("disable", help="Disable all capture in settings.yaml")
    p_disable.set_defaults(func=cmd_disable)

    # test-event
    p_test = sub.add_parser("test-event", help="Emit synthetic test event to verify pipeline")
    p_test.set_defaults(func=cmd_test_event)

    # export
    p_export = sub.add_parser("export", help="Replay raw WAL events with integrity checks")
    p_export.add_argument("--out", metavar="FILE", help="Write events to FILE (default: stdout)")
    p_export.add_argument("--verify", dest="verify_auth", action="store_true",
                          help="Verify per-record HMAC (default: on when secret is present)")
    p_export.add_argument("--verify-checksums", action="store_true",
                          help="Verify rotated files against their .sha256 sidecars")
    p_export.add_argument("--verify-only", action="store_true",
                          help="Only verify integrity; do not emit events")
    p_export.add_argument("--no-verify", action="store_true",
                          help="Skip all integrity verification")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
