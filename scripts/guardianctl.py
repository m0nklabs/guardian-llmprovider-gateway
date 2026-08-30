#!/usr/bin/env python3
"""guardianctl — CLI for Guardian capture subsystem control.

Subcommands:
  status     Show capture subsystem status (config + runtime + retention/on-disk inventory)
  config     Show effective capture configuration
  files      List capture WAL files on disk
  rotate     Force rotation of the active capture file
  enable     Enable capture (modifies settings.yaml, requires server restart)
  disable    Disable capture (modifies settings.yaml, requires server restart)
  test-event Emit a synthetic test event to verify the pipeline end-to-end
  export     Replay raw WAL events (Keanu handoff) with integrity checks

Usage:
  ./venv/bin/python scripts/guardianctl.py status
  ./venv/bin/python scripts/guardianctl.py files --json
  GUARDIAN_API_KEY=flip... ./venv/bin/python scripts/guardianctl.py rotate
  ./venv/bin/python scripts/guardianctl.py export --verify --out dataset.jsonl

Note: `status` and `rotate` talk to the running Guardian API.
      `status` also reports the configured retention and the on-disk
      capture file inventory (active/rotated files, feedback C11).
      `config`, `enable`, `disable` read/modify settings.yaml directly.
      `files`, `export` inspect the filesystem.
      `export` replays the RAW WAL (Guardian stores raw since 2026-08-26;
      redaction is Keanu's job via scripts/keanu_redact.py).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from _paths import DATA_DIR, REPO_ROOT

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


def _capture_retention_summary(cfg: Optional[Any] = None) -> dict:
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
    from app.capture.sink import CaptureSink, CaptureEvent
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
