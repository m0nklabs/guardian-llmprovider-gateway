#!/usr/bin/env python3
"""Generate a Guardian-capture contract WAL with the REAL capture pipeline.

Produces a small, realistic WAL file using the actual ``CaptureSink`` +
``CaptureWALWriter`` + event builders (``app.capture.*``) — not a synthetic
reimplementation — so cross-repo contract tests (Keanu Factory
``guardian_capture_parser`` / ``capture_ingest``) can verify against
authentic producer output without enabling live capture.

Usage:
    GUARDIAN_CAPTURE_RECORD_AUTH_SECRET="secret" ./venv/bin/python \\
        scripts/generate_contract_wal.py --out /tmp/contract_wal --request-id req-001

The active WAL file is ``<out>/guardian_capture_current.jsonl.gz`` (stream
gzip; a completed member, written cleanly on stop). Keanu ingestion should
use ``--include-active`` (or copy/rename the file so it is treated as a
completed WAL).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.capture.config import CaptureConfig  # noqa: E402
from app.capture.gzip_reader import iter_records  # noqa: E402
from app.capture.schema import (  # noqa: E402
    BuildContext,
    build_request_completed_event,
    build_request_received_event,
)
from app.capture.sink import CaptureEvent, CaptureSink  # noqa: E402
from app.capture.wal_writer import CaptureWALWriter  # noqa: E402

#: A realistic multi-turn conversation (>= 3 pairs, assistant replies >= 12
#: words) so the Keanu pipeline accepts the staged record (chatml.MIN_PAIRS
#: and _PLACEHOLDER_MIN_WORDS quality gates).
HISTORY = [
    {"role": "user", "content": "Explain the water cycle in simple terms."},
    {"role": "assistant", "content": (
        "The water cycle describes how water moves between the earth and the "
        "atmosphere through evaporation, condensation, and precipitation."
    )},
    {"role": "user", "content": "What happens during evaporation?"},
    {"role": "assistant", "content": (
        "Evaporation happens when the sun heats water in rivers, lakes, and "
        "oceans, turning it into vapor that rises into the sky."
    )},
    {"role": "user", "content": "And what about condensation?"},
]

RESPONSE = (
    "Condensation is the process where water vapor in the air cools down and "
    "turns back into tiny liquid droplets, forming clouds."
)


async def _main(out_dir: Path, request_id: str, instance_id: str) -> None:
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    config = CaptureConfig(
        capture_root=str(out_dir),
        instance_id=instance_id,
        enabled=True,
        local_capture=True,
        cloud_capture=False,
        per_client_opt_in=False,
        policy_version="1",
    )
    sink = CaptureSink()
    writer = CaptureWALWriter(sink, config)
    await writer.start()

    ctx = BuildContext(
        request_id=request_id,
        endpoint="/v1/chat/completions",
        ingress_protocol="openai",
        route_type="local",
        requested_model="llama3.2-3b",
        resolved_model="llama3.2-3b",
        capture_policy_version="1",
        instance_id=instance_id,
        client_fingerprint="contract-client",
    )
    sink.try_put(CaptureEvent(data=build_request_received_event(
        config, ctx, request_messages=HISTORY)))
    sink.try_put(CaptureEvent(data=build_request_completed_event(
        config, ctx,
        response_content=RESPONSE,
        prompt_tokens=120,
        completion_tokens=40,
        streamed=False,
    )))
    await asyncio.sleep(0.6)
    await writer.stop()

    wals = sorted(out_dir.glob("*.jsonl.gz"))
    if not wals:
        raise SystemExit("ERROR: no WAL file produced")
    print(f"WAL: {wals[0]}")
    for w in wals:
        import json

        lines = list(iter_records(w))
        print(f"  lines={len(lines)}")
        for ln in lines:
            ev = json.loads(ln)
            auth = "record_auth=yes" if ev.get("record_auth") else "record_auth=NO"
            print(f"    {ev['event_type']} {auth}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/contract_wal"),
                        help="output WAL directory (default: /tmp/contract_wal)")
    parser.add_argument("--request-id", default="contract-req-001")
    parser.add_argument("--instance-id", default="contract-test-instance")
    args = parser.parse_args()
    asyncio.run(_main(args.out, args.request_id, args.instance_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
