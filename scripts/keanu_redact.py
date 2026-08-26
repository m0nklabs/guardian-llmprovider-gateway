#!/usr/bin/env python3
"""keanu_redact — turn raw Guardian capture WAL events into a redacted dataset.

Architecture (operator decision 2026-08-26): Guardian stores RAW events in
the WAL — full request/response content, system prompts, reasoning and tool
results included.  Redaction and dataset construction is Keanu's job; this
script is the standalone tool for it.  It consumes the replayable WAL
directly (gzip or plain JSONL; a file or a whole directory) and emits one
JSON line per processed event with the requested field policies applied.

Policies (defaults mirror the ``capture:`` section of global.settings.yaml,
which since 2026-08-26 are *Keanu defaults*, not Guardian pipeline rules):

  --policy system_prompts=strip|capture
  --policy reasoning=strip|capture
  --policy tool_definitions=strip|capture
  --policy tool_calls=strip|capture
  --policy tool_results=strip|capture
  --policy unknown_content_blocks=strip|capture

Media references (``image_media`` blocks) are payload-free pointers written
by Guardian; this tool resolves them per ``--images``:

  hash (default)  → replace with image_metadata (hash/mime/size/dims, no path)
  strip           → drop the block
  keep            → keep the reference (you can copy the files yourself)

Usage:
  ./venv/bin/python scripts/keanu_redact.py --input data/capture --output dataset.jsonl
  ./venv/bin/python scripts/keanu_redact.py --input guardianctl_export.jsonl --images keep
  ./venv/bin/python scripts/keanu_redact.py --input data/capture --policy reasoning=capture

Exit code 0 on success; 1 on unreadable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Make `app` importable when run as `python scripts/keanu_redact.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.capture.redactor import (  # noqa: E402
    redact_request_messages,
    redact_request_parameters,
    redact_response_content,
    redact_reasoning_content,
    redact_tool_calls,
    redact_tool_results,
)

DEFAULT_POLICIES = {
    "system_prompts": "strip",
    "reasoning": "strip",
    "tool_definitions": "capture",
    "tool_calls": "capture",
    "tool_results": "strip",
    "unknown_content_blocks": "strip",
}

MEDIA_REF_TYPES = ("image_media", "image_metadata")


def _transform_media_blocks(content: Any, mode: str) -> Any:
    """Apply the --images policy to media reference blocks in a content list."""
    if not isinstance(content, list):
        return content
    out: List[Any] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in MEDIA_REF_TYPES:
            out.append(block)
            continue
        if mode == "strip":
            continue
        if mode == "keep":
            out.append(block)
            continue
        # hash (default): strip the path, keep the integrity metadata
        meta = block.get("image_media") or block.get("image_metadata") or {}
        safe = {k: v for k, v in meta.items() if k != "path"}
        out.append({"type": "image_metadata", "image_metadata": safe})
    return out


def _redact_messages(messages: Any, policies: Dict[str, str], images_mode: str) -> Any:
    if not isinstance(messages, list):
        return messages
    transformed = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            msg = {**msg, "content": _transform_media_blocks(msg["content"], images_mode)}
        transformed.append(msg)
    return redact_request_messages(transformed, policies)


def _redact_event(event: Dict[str, Any], policies: Dict[str, str], images_mode: str) -> Dict[str, Any]:
    """Return a redacted copy of one raw WAL event."""
    out = dict(event)
    et = out.get("event_type")

    if et == "request_received":
        if out.get("request_messages") is not None:
            out["request_messages"] = _redact_messages(out["request_messages"], policies, images_mode)
        if out.get("request_parameters") is not None:
            out["request_parameters"] = redact_request_parameters(out["request_parameters"], policies)

    elif et == "request_completed":
        if out.get("response_content") is not None:
            out["response_content"] = redact_response_content(out["response_content"])
        if out.get("reasoning_content") is not None:
            out["reasoning_content"] = redact_reasoning_content(
                out["reasoning_content"], policies.get("reasoning", "strip")
            )
        if out.get("tool_calls") is not None:
            out["tool_calls"] = redact_tool_calls(out["tool_calls"], policies.get("tool_calls", "capture"))
        if out.get("tool_results") is not None:
            out["tool_results"] = redact_tool_results(out["tool_results"], policies.get("tool_results", "strip"))

    elif et == "request_failed":
        if out.get("sanitized_message") is not None:
            out["sanitized_message"] = redact_response_content(out["sanitized_message"])

    return out


def _iter_input_events(path: Path) -> Any:
    """Yield raw event dicts from a file or directory (sorted, gzip-aware).

    gzip inputs are read with the shared crash-tolerant reader (a restart
    after a crash appends a new gzip member to the active WAL); plain JSONL
    inputs are read line by line.
    """
    from app.capture.gzip_reader import iter_events

    files: List[Path] = []
    if path.is_dir():
        files = sorted(path.glob("*.jsonl*"))
    elif path.exists():
        files = [path]
    else:
        raise SystemExit(f"Input not found: {path}")

    for f in files:
        if f.suffix == ".gz":
            for event in iter_events(f):
                yield event
        else:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="WAL file or directory of WAL files")
    parser.add_argument("--output", help="Dataset JSONL path (default: stdout)")
    parser.add_argument(
        "--policy", action="append", default=[], metavar="KEY=VALUE",
        help="Override a field policy (repeatable)",
    )
    parser.add_argument(
        "--images", choices=("hash", "strip", "keep"), default="hash",
        help="How to treat image_media references (default: hash)",
    )
    args = parser.parse_args()

    policies = dict(DEFAULT_POLICIES)
    for item in args.policy:
        if "=" not in item:
            parser.error(f"--policy expects KEY=VALUE, got '{item}'")
        key, _, value = item.partition("=")
        if key not in policies:
            parser.error(f"Unknown policy '{key}' (valid: {', '.join(sorted(policies))})")
        if value not in ("strip", "capture"):
            parser.error(f"Policy '{key}' must be 'strip' or 'capture', got '{value}'")
        policies[key] = value

    out_fh = sys.stdout
    close_out = False
    if args.output:
        out_fh = open(args.output, "w", encoding="utf-8")
        close_out = True

    input_path = Path(args.input)
    count = 0
    try:
        for event in _iter_input_events(input_path):
            redacted = _redact_event(event, policies, args.images)
            out_fh.write(json.dumps(redacted, ensure_ascii=False, default=str) + "\n")
            count += 1
    finally:
        if close_out:
            out_fh.close()

    print(f"✅ Redacted {count} events (images={args.images})", file=sys.stderr)


if __name__ == "__main__":
    main()
