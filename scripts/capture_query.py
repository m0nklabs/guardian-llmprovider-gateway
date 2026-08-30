#!/usr/bin/env python3
"""Query and roll up Guardian capture JSONL archives (guardian_capture_v1).

Feedback items C9 (query tool) and C10 (daily rollup): a supported,
pitfall-tolerant CLI so consumers stop writing throwaway scanners over
data/capture.  Reads rotated ``*.jsonl.gz`` files, the active plain
``guardian_capture_current.jsonl`` and legacy variants; tolerates truncated
gzip members, malformed lines, float-encoded token counters and records with
missing fields.  Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

#: Default capture root, resolved relative to the current working directory.
DEFAULT_ROOT = "data/capture"

#: The currently-active plain JSONL file; always scanned last.
ACTIVE_PLAIN_NAME = "guardian_capture_current.jsonl"

EVENT_TYPES = (
    "request_received",
    "request_completed",
    "request_failed",
    "request_cancelled",
)

ROUTE_CHOICES = ("local", "cloud")

# Numeric user input is treated as epoch seconds (see parse_timestamp).
_EPOCH_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Where cost / reasoning-token fields may live in a record.  The first path
# that yields a parseable number wins; missing paths are simply skipped.
COST_PATHS = (("cost",), ("usage", "cost"))
REASONING_PATHS = (
    ("completion_tokens_details", "reasoning_tokens"),
    ("usage", "completion_tokens_details", "reasoning_tokens"),
    ("native_tokens_reasoning",),
    ("usage", "native_tokens_reasoning"),
)


@dataclass
class CaptureRecord:
    """One parsed capture record plus its raw stored line."""

    data: dict
    raw: str
    path: Path


@dataclass
class ScanStats:
    """Counters for --verbose; everything is fail-open."""

    files_scanned: int = 0
    records_read: int = 0
    skipped_json_errors: int = 0
    skipped_empty_lines: int = 0
    partial_files: list[str] = field(default_factory=list)
    unreadable_files: list[str] = field(default_factory=list)


# ── File discovery ─────────────────────────────────────────────────────


def _file_sort_key(path: Path) -> tuple:
    """Oldest -> newest by mtime, with the active plain file forced last."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if path.name == ACTIVE_PLAIN_NAME:
        return (1, 0.0, path.name)
    return (0, mtime, path.name)


def discover_files(root: Path) -> list[Path]:
    """List capture files to scan: rotated *.jsonl.gz, plain *.jsonl variants.

    Top-level scan only: skips subdirectories (including ``media/``),
    ``*.sha256`` sidecars and dotfiles such as ``.capture_state.json``.
    """
    if not root.is_dir():
        return []
    found: list[Path] = []
    for entry in root.iterdir():
        try:
            if not entry.is_file():
                continue  # also skips media/ and any other subdirectory
        except OSError:
            continue
        name = entry.name
        if name.startswith("."):
            continue  # .capture_state.json and friends
        if name.endswith(".sha256"):
            continue
        if name.endswith(".jsonl.gz") or name.endswith(".jsonl"):
            found.append(entry)
    found.sort(key=_file_sort_key)
    return found


# ── Tolerant record reading ────────────────────────────────────────────


def _parse_line(line: str, stats: ScanStats) -> dict | None:
    """Parse one JSONL line; skip (and count) empty or malformed lines."""
    stripped = line.strip()
    if not stripped:
        stats.skipped_empty_lines += 1
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        stats.skipped_json_errors += 1
        return None
    if not isinstance(obj, dict):
        stats.skipped_json_errors += 1  # valid JSON, but not an object
        return None
    return obj


def _iter_gz(path: Path, stats: ScanStats) -> Iterator[CaptureRecord]:
    """Yield records from a .jsonl.gz file, tolerating a truncated tail.

    A truncated or still-being-written final gzip member raises EOFError
    (or BadGzipFile / zlib.error); that ends THIS file cleanly — records
    decoded so far are kept and the scan continues with the next file.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            for line in fh:
                data = _parse_line(line, stats)
                if data is not None:
                    stats.records_read += 1
                    yield CaptureRecord(data, line.rstrip("\r\n"), path)
    except (EOFError, OSError, zlib.error) as exc:
        stats.partial_files.append(f"{path.name} ({type(exc).__name__}: {exc})")


def _iter_plain(path: Path, stats: ScanStats) -> Iterator[CaptureRecord]:
    """Yield records from a plain .jsonl file (the active file layout)."""
    try:
        with open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            for line in fh:
                data = _parse_line(line, stats)
                if data is not None:
                    stats.records_read += 1
                    yield CaptureRecord(data, line.rstrip("\r\n"), path)
    except OSError as exc:
        stats.unreadable_files.append(f"{path.name} ({type(exc).__name__}: {exc})")


def iter_records(paths: Iterable[Path], stats: ScanStats) -> Iterator[CaptureRecord]:
    """Yield records from all files in the given (chronological) order."""
    for path in paths:
        stats.files_scanned += 1
        if path.name.endswith(".gz"):
            yield from _iter_gz(path, stats)
        else:
            yield from _iter_plain(path, stats)


# ── Value tolerance helpers ────────────────────────────────────────────


def _text(value: object) -> str:
    """Stringify any field value without crashing (None -> empty string)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def as_int(value: object) -> int | None:
    """Cast a numeric field to int; tolerates floats (legacy 131072.0).

    Returns None when the value is missing or not numeric.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def as_float(value: object) -> float | None:
    """Cast a numeric field to float; returns None when not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_timestamp(value: object) -> datetime | None:
    """Parse ISO-8601 (Z / offset / naive-as-UTC) or epoch seconds to UTC.

    Returns None for anything unparseable — callers treat that as "no time".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _EPOCH_RE.fullmatch(text):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_arg(text: str) -> datetime:
    """argparse type for --since/--until: like parse_timestamp but strict."""
    parsed = parse_timestamp(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            f"invalid timestamp {text!r}: use ISO-8601 "
            f"(e.g. 2026-08-26T10:00:00Z) or epoch seconds"
        )
    return parsed


def record_time(record: dict) -> datetime | None:
    """Completion time of a record: completed_at_utc if present, else
    timestamp_utc (completion-time semantics).  A present-but-unparseable
    completed_at_utc is NOT silently replaced by timestamp_utc.
    """
    value = record.get("completed_at_utc")
    if value is None:
        value = record.get("timestamp_utc")
    return parse_timestamp(value)


# ── Filtering ──────────────────────────────────────────────────────────


def is_wasted_completion(record: dict) -> bool:
    """True for completed events that generated tokens but no usable content.

    The "wasted output" signal: event_type == request_completed,
    completion_tokens > 0 and response_content empty, whitespace-only or
    absent.
    """
    if record.get("event_type") != "request_completed":
        return False
    completion = as_int(record.get("completion_tokens"))
    if completion is None or completion <= 0:
        return False
    content = record.get("response_content")
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    return False


def matches(record: dict, args: argparse.Namespace) -> bool:
    """Apply every requested filter; all fields are optional (never KeyError)."""
    if args.event_type is not None and record.get("event_type") != args.event_type:
        return False
    if args.route is not None and record.get("route_type") != args.route:
        return False
    if args.client is not None and not _text(record.get("client_ref")).startswith(args.client):
        return False
    if args.model is not None:
        needle = args.model.lower()
        if (
            needle not in _text(record.get("requested_model")).lower()
            and needle not in _text(record.get("resolved_model")).lower()
        ):
            return False
    if args.request_id is not None and record.get("request_id") != args.request_id:
        return False
    if args.min_completion is not None:
        value = as_int(record.get("completion_tokens"))
        if value is None or value < args.min_completion:
            return False
    if args.min_prompt is not None:
        value = as_int(record.get("prompt_tokens"))
        if value is None or value < args.min_prompt:
            return False
    if args.empty_content_only and not is_wasted_completion(record):
        return False
    if args.since is not None or args.until is not None:
        when = record_time(record)
        if when is None:
            return False
        if args.since is not None and when < args.since:
            return False
        if args.until is not None and when >= args.until:  # until is exclusive
            return False
    return True


# ── Output formatting ──────────────────────────────────────────────────


def format_summary_line(record: dict) -> str:
    """One human-readable line per record."""
    when = record_time(record)
    if when is not None:
        stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    else:
        raw = record.get("completed_at_utc")
        if raw is None:
            raw = record.get("timestamp_utc")
        stamp = _text(raw) or "-"
    event = _text(record.get("event_type")) or "-"
    route = _text(record.get("route_type")) or "-"
    model = _text(record.get("resolved_model")) or _text(record.get("requested_model")) or "-"
    prompt = as_int(record.get("prompt_tokens"))
    completion = as_int(record.get("completion_tokens"))
    parts = [
        stamp,
        event,
        route,
        model,
        f"in={prompt if prompt is not None else '-'}",
        f"out={completion if completion is not None else '-'}",
    ]
    finish = record.get("finish_reason")
    if finish:
        parts.append(f"finish={_text(finish)}")
    parts.append(f"client={_text(record.get('client_ref'))[:12] or '-'}")
    parts.append(f"req={_text(record.get('request_id'))[:8] or '-'}")
    return " ".join(parts)


def _dig(record: dict, *keys: str) -> object:
    """Walk nested dicts; return None as soon as a level is missing."""
    node: object = record
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _first_number(record: dict, paths: tuple, caster) -> float | int | None:
    """First parseable number along any of the candidate paths, else None."""
    for path in paths:
        value = caster(_dig(record, *path))
        if value is not None:
            return value
    return None


# ── Daily rollup (C10) ─────────────────────────────────────────────────


def build_daily_rollup(records: Iterable[dict]) -> list[dict]:
    """Aggregate selected records per UTC day x (route, resolved_model,
    client_ref).  The day bucket is the completion timestamp's UTC date
    (see record_time); records without a parseable time land in "unknown".
    """
    buckets: dict[tuple, dict] = {}
    for record in records:
        when = record_time(record)
        date = when.date().isoformat() if when is not None else "unknown"
        key = (
            date,
            record.get("route_type"),
            record.get("resolved_model"),
            record.get("client_ref"),
        )
        agg = buckets.setdefault(
            key,
            {
                "calls": 0,
                "received": 0,
                "completed": 0,
                "failed": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "completion_with_empty_content": 0,
                "finish_reason_counts": {},
                "costs": [],
                "reasoning": [],
            },
        )
        agg["calls"] += 1
        event = record.get("event_type")
        if event == "request_received":
            agg["received"] += 1
        elif event == "request_completed":
            agg["completed"] += 1
        elif event == "request_failed":
            agg["failed"] += 1
        prompt = as_int(record.get("prompt_tokens"))
        if prompt is not None:
            agg["prompt_tokens"] += prompt
        completion = as_int(record.get("completion_tokens"))
        if completion is not None:
            agg["completion_tokens"] += completion
        if is_wasted_completion(record):
            agg["completion_with_empty_content"] += 1
        finish = record.get("finish_reason")
        if isinstance(finish, str) and finish:
            counts = agg["finish_reason_counts"]
            counts[finish] = counts.get(finish, 0) + 1
        cost = _first_number(record, COST_PATHS, as_float)
        if cost is not None:
            agg["costs"].append(cost)
        reasoning = _first_number(record, REASONING_PATHS, as_int)
        if reasoning is not None:
            agg["reasoning"].append(reasoning)

    rows: list[dict] = []
    order = sorted(buckets, key=lambda k: (k[0], _text(k[1]), _text(k[2]), _text(k[3])))
    for key in order:
        agg = buckets[key]
        costs = agg["costs"]
        reasoning = agg["reasoning"]
        rows.append(
            {
                "date": key[0],
                "route": key[1],
                "model": key[2],
                "client_ref": key[3],
                "calls": agg["calls"],
                "received": agg["received"],
                "completed": agg["completed"],
                "failed": agg["failed"],
                "prompt_tokens": agg["prompt_tokens"],
                "completion_tokens": agg["completion_tokens"],
                "completion_with_empty_content": agg["completion_with_empty_content"],
                "finish_reason_counts": dict(sorted(agg["finish_reason_counts"].items())),
                "cost_sum": round(sum(costs), 6) if costs else None,
                "reasoning_tokens_sum": sum(reasoning) if reasoning else None,
            }
        )
    return rows


# ── CLI ────────────────────────────────────────────────────────────────

EPILOG = """\
timestamps:
  --since/--until accept ISO-8601 (with or without Z / offset; a naive
  timestamp is treated as UTC) or epoch seconds.  The window is half-open:
  a record is selected when since <= t < until.

completion-time semantics:
  Filtering and the daily rollup bucket use a record's completion time:
  completed_at_utc when that field is present (schema 1.1.0+), otherwise
  timestamp_utc.  A present-but-unparseable completed_at_utc is NOT
  silently replaced by timestamp_utc.

tolerant reading (fail-open):
  - Truncated or still-being-written .jsonl.gz files (EOFError from the
    trailing gzip member) end that file cleanly; records decoded so far
    are kept and the scan continues.
  - Malformed or non-object JSON lines are skipped and counted (--verbose).
  - Numeric token fields may be int or float (legacy 131072.0); they are
    compared numerically.
  - Every field is optional per record: missing fields are treated as
    absent and simply do not match the corresponding filter.

rollup (--rollup daily):
  Aggregates the SELECTED records (all filters apply) into one JSON object
  per UTC day and (route, resolved_model, client_ref), printed as JSONL so
  it can be piped.  Bucket fields missing on a record are emitted as null.
  cost_sum / reasoning_tokens_sum come from the 1.1.0 fields (cost,
  completion_tokens_details.reasoning_tokens, native_tokens_reasoning)
  when present, else null.

exit status:
  0  success, including zero selected records
  2  usage error, invalid --since/--until value, or missing --root
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture_query.py",
        description=(
            "Query Guardian capture JSONL archives (guardian_capture_v1): "
            "filter records and/or produce a daily rollup.  Scans rotated "
            "*.jsonl.gz files plus the active/legacy plain files in --root."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        metavar="DIR",
        help=f"capture directory to scan (default: {DEFAULT_ROOT}, relative to the current directory)",
    )

    filters = parser.add_argument_group("filters (all optional, combinable)")
    filters.add_argument(
        "--client", metavar="PREFIX", help="client_ref starts with PREFIX"
    )
    filters.add_argument(
        "--model",
        metavar="SUBSTR",
        help="case-insensitive substring match on requested_model OR resolved_model",
    )
    filters.add_argument(
        "--route", choices=ROUTE_CHOICES, help="route_type equals local or cloud"
    )
    filters.add_argument(
        "--event-type", choices=EVENT_TYPES, help="event_type equals the given value"
    )
    filters.add_argument(
        "--request-id", metavar="ID", help="request_id equals ID (exact match)"
    )
    filters.add_argument(
        "--min-completion",
        type=int,
        metavar="N",
        help="completion_tokens >= N (float-encoded legacy values like 131072.0 compare numerically)",
    )
    filters.add_argument(
        "--min-prompt", type=int, metavar="N", help="prompt_tokens >= N"
    )
    filters.add_argument(
        "--empty-content-only",
        action="store_true",
        help=(
            "keep only completed events with completion_tokens > 0 and an "
            "empty/absent response_content (the wasted-output signal)"
        ),
    )
    filters.add_argument(
        "--since", type=timestamp_arg, metavar="TS", help="completion time >= TS (inclusive)"
    )
    filters.add_argument(
        "--until", type=timestamp_arg, metavar="TS", help="completion time < TS (exclusive)"
    )
    filters.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="stop after N selected records",
    )

    outputs = parser.add_argument_group("output modes (mutually exclusive)")
    group = outputs.add_mutually_exclusive_group()
    group.add_argument(
        "--json",
        action="store_true",
        help="print the selected records as raw JSON lines, exactly as stored",
    )
    group.add_argument(
        "--count", action="store_true", help="print only the number of selected records"
    )
    group.add_argument(
        "--rollup",
        choices=("daily",),
        metavar="MODE",
        help="print a daily aggregate (JSONL) over the selected records instead of individual records",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="report scan statistics (files, records, skipped/bad lines, partial files) on stderr",
    )
    return parser


def _limited(records: Iterator[dict], limit: int | None) -> Iterator[dict]:
    """Pass records through, stopping after `limit` of them (None = all)."""
    if limit is None:
        yield from records
        return
    emitted = 0
    for record in records:
        yield record
        emitted += 1
        if emitted >= limit:
            return


def _print_stats(stats: ScanStats, selected: int) -> None:
    print(f"files_scanned={stats.files_scanned}", file=sys.stderr)
    print(f"records_read={stats.records_read}", file=sys.stderr)
    print(f"selected={selected}", file=sys.stderr)
    print(f"skipped_json_errors={stats.skipped_json_errors}", file=sys.stderr)
    print(f"skipped_empty_lines={stats.skipped_empty_lines}", file=sys.stderr)
    print(f"partial_files={len(stats.partial_files)}", file=sys.stderr)
    for entry in stats.partial_files:
        print(f"  partial: {entry}", file=sys.stderr)
    print(f"unreadable_files={len(stats.unreadable_files)}", file=sys.stderr)
    for entry in stats.unreadable_files:
        print(f"  unreadable: {entry}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")

    root = Path(args.root)
    if not root.is_dir():
        print(
            f"error: capture root does not exist or is not a directory: {root}",
            file=sys.stderr,
        )
        return 2

    stats = ScanStats()
    generator = iter_records(discover_files(root), stats)
    selected = 0

    if args.rollup:
        rows = build_daily_rollup(
            _limited((rec.data for rec in generator if matches(rec.data, args)), args.limit)
        )
        selected = sum(row["calls"] for row in rows)
        for row in rows:
            print(json.dumps(row))
    else:
        for rec in generator:
            if not matches(rec.data, args):
                continue
            if args.limit is not None and selected >= args.limit:
                break
            selected += 1
            if args.json:
                print(rec.raw)
            elif not args.count:
                print(format_summary_line(rec.data))
        if args.count:
            print(selected)

    if args.verbose:
        _print_stats(stats, selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
