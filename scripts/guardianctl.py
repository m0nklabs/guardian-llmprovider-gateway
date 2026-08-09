#!/usr/bin/env python3
"""guardianctl — CLI for Guardian capture subsystem control.

Subcommands:
  status     Show capture subsystem status (config + runtime)
  config     Show effective capture configuration
  files      List capture WAL files on disk
  rotate     Force rotation of the active capture file
  enable     Enable capture (modifies settings.yaml, requires server restart)
  disable    Disable capture (modifies settings.yaml, requires server restart)
  test-event Emit a synthetic test event to verify the pipeline end-to-end

Usage:
  ./venv/bin/python scripts/guardianctl.py status
  ./venv/bin/python scripts/guardianctl.py files --json
  GUARDIAN_API_KEY=flip... ./venv/bin/python scripts/guardianctl.py rotate

Note: `status` and `rotate` talk to the running Guardian API.
      `config`, `enable`, `disable` read/modify settings.yaml directly.
      `files` inspects the filesystem.
"""

import argparse
import json
import sys
from pathlib import Path

from _paths import CONFIG_DIR, DATA_DIR, REPO_ROOT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SETTINGS_YAML = CONFIG_DIR / "settings.yaml"
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
    print("⚠️  Requires server restart: sudo systemctl restart llama-guardian")


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
        print("   Is the server running? Check: sudo systemctl status llama-guardian", file=sys.stderr)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> None:
    """Show capture subsystem status via the running Guardian API."""
    result = _api_request("GET", "/api/capture/status")
    if args.json:
        print(json.dumps(result, indent=2))
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
                "path": str(f.relative_to(REPO_ROOT)),
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
        ftype = "gzip" if f.suffix == ".gz" else "active" if ".active" in f.name else "complete"
        print(f"{str(f.relative_to(REPO_ROOT)):<50s} {size:>12,}  {ftype}")
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
    from app.capture.sink import CaptureSink

    cfg = load_capture_config()
    if not cfg.is_active:
        print("❌ Capture is not active. Enable it first:")
        print("   ./venv/bin/python scripts/guardianctl.py enable --local-only")
        raise SystemExit(1)

    sink = CaptureSink(config=cfg)
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
        ctx,
        request_messages=[{"role": "user", "content": "This is a test message"}],
        request_parameters={"temperature": 0.0},
    )
    sink.write(event)
    print(f"✅ Test event emitted: {event.get('event_id', '?')}")
    print(f"   Event type: {event.get('event_type', '?')}")
    print(f"   Schema:     {event.get('schema_name', '?')} v{event.get('schema_version', '?')}")
    print(f"   Request ID: {event.get('request_id', '?')}")


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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
