"""Unit tests for scripts/capture_query.py (feedback C9 query CLI + C10 rollup).

All fixtures are synthetic and built in pytest tmp dirs — the real
data/capture tree is NEVER read by these tests.  The script is exercised
through subprocess (same pattern as tests/unit/test_finetune_v2_contracts_script.py).
"""

import gzip
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "capture_query.py"


# ── Fixture record factory ─────────────────────────────────────────────


def _rec(**overrides):
    """A representative guardian_capture_v1 request_completed record."""
    record = {
        "schema_name": "guardian_capture_v1",
        "schema_version": "1.0.0",
        "event_type": "request_completed",
        "request_id": "rq",
        "timestamp_utc": "2026-08-20T09:00:00.000Z",
        "route_type": "local",
        "requested_model": "local-a",
        "resolved_model": "local-a",
        "client_ref": "c0ffee00000000",
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "response_content": "ok",
    }
    record.update(overrides)
    return record


R1 = _rec(
    event_type="request_received",
    request_id="rq-gz1-a",
    timestamp_utc="2026-08-20T09:00:00.000Z",
    client_ref="c0ffee000001",
    response_content=None,
)
R2 = _rec(
    request_id="rq-gz1-b",
    timestamp_utc="2026-08-20T09:00:05.000Z",
    client_ref="c0ffee000001",
    prompt_tokens=10,
    completion_tokens=20,
    response_content="ok",
    finish_reason="stop",
    cost=0.5,
)
R3 = _rec(
    request_id="rq-gz2-a",
    timestamp_utc="2026-08-21T23:30:00.000Z",
    route_type="cloud",
    requested_model="anthropic/claude-3",
    resolved_model="anthropic/claude-3-ymq",
    client_ref="deadbeef000002",
    prompt_tokens=100,
    completion_tokens=7,
    response_content="",
    finish_reason="length",
)
R4 = _rec(
    event_type="request_failed",
    request_id="rq-gz2-b",
    timestamp_utc="2026-08-21T23:31:00.000Z",
    route_type="cloud",
    resolved_model="anthropic/claude-3-ymq",
    client_ref="deadbeef000002",
    error_code="upstream_5xx",
    response_content=None,
)
T1 = _rec(
    request_id="rq-trunc-a",
    timestamp_utc="2026-08-22T08:00:00.000Z",
    client_ref="ffff00001111",
    prompt_tokens=1,
    completion_tokens=3,
    response_content="x",
)
T2 = _rec(
    request_id="rq-trunc-b",
    timestamp_utc="2026-08-22T08:00:01.000Z",
    client_ref="ffff00001111",
    prompt_tokens=1,
    completion_tokens=4,
    response_content="y",
)
LEGACY = _rec(
    event_type="request_received",
    request_id="rq-legacygz",
    timestamp_utc="2026-08-22T09:00:00.000Z",
    client_ref="beefbeef0001",
    response_content=None,
)
P1 = _rec(  # legacy float-encoded token counters
    request_id="rq-float",
    timestamp_utc="2026-08-23T10:00:00.000Z",
    client_ref="c0ffee00000f",
    prompt_tokens=1024.0,
    completion_tokens=131072.0,
    response_content="legacy-ok",
)
P2 = {  # minimal legacy record: most fields missing entirely
    "schema_name": "guardian_capture_v1",
    "schema_version": "1.0.0",
    "event_type": "request_received",
    "request_id": "rq-minimal",
    "timestamp_utc": "2026-08-24T09:00:00.000Z",
}
P3 = _rec(
    request_id="rq-waste-null",
    timestamp_utc="2026-08-24T11:00:00.000Z",
    route_type="local",
    requested_model="local-b",
    resolved_model="local-b",
    client_ref="c0ffee000009",
    prompt_tokens=11,
    completion_tokens=9,
    response_content=None,
    finish_reason="stop",
)
P4 = _rec(
    request_id="rq-waste-empty",
    timestamp_utc="2026-08-24T11:01:00.000Z",
    route_type="local",
    requested_model="local-b",
    resolved_model="local-b",
    client_ref="c0ffee000009",
    prompt_tokens=12,
    completion_tokens=9,
    response_content="",
    finish_reason="stop",
)
P5 = _rec(
    request_id="rq-ok",
    timestamp_utc="2026-08-24T11:02:00.000Z",
    route_type="local",
    requested_model="local-b",
    resolved_model="local-b",
    client_ref="c0ffee000009",
    prompt_tokens=13,
    completion_tokens=9,
    response_content="real",
    finish_reason="stop",
    cost=1.25,
    completion_tokens_details={"reasoning_tokens": 5},
)
P6 = _rec(
    request_id="rq-ok2",
    timestamp_utc="2026-08-24T12:00:00.000Z",
    route_type="local",
    requested_model="local-b",
    resolved_model="local-b",
    client_ref="c0ffee000009",
    prompt_tokens=14,
    completion_tokens=6,
    response_content="r2",
    finish_reason="length",
    native_tokens_reasoning=7,
)
P7 = _rec(  # completion-time semantics: completed_at_utc dominates timestamp_utc
    request_id="rq-ctime",
    timestamp_utc="2026-08-19T00:00:00.000Z",
    completed_at_utc="2026-08-25T00:00:00.000Z",
    client_ref="nope-0001",
    completion_tokens=1,
    response_content="c",
)
P8 = _rec(  # zero completion tokens with empty content: NOT wasted output
    request_id="rq-zero",
    timestamp_utc="2026-08-23T11:03:00.000Z",
    requested_model="local-c",
    resolved_model="local-c",
    client_ref="c0ffee000009",
    prompt_tokens=5,
    completion_tokens=0,
    response_content="",
    finish_reason="stop",
)
DECOY = _rec(request_id="rq-decoy")


# ── Fixture file builders ──────────────────────────────────────────────


def _gz_bytes(records) -> bytes:
    """Deterministic gzip bytes (fixed mtime, no embedded filename)."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as gz:
        for record in records:
            gz.write((json.dumps(record) + "\n").encode("utf-8"))
    return buffer.getvalue()


TRUNCATED_GZ_BYTES = _gz_bytes([T1, T2])[:-10]  # cut the last 10 bytes off


def _write_gz(path: Path, records) -> None:
    path.write_bytes(_gz_bytes(records))


def _write_jsonl(path: Path, records) -> None:
    path.write_text("".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8")


# 8 records + 2 empty lines + 1 malformed line (skipped and counted).
PLAIN_LINES = "\n".join(
    [
        json.dumps(P1),
        json.dumps(P2),
        "",
        "not json {{{",
        json.dumps(P3),
        "",
        json.dumps(P4),
        json.dumps(P5),
        json.dumps(P6),
        json.dumps(P7),
        json.dumps(P8),
    ]
) + "\n"


@pytest.fixture(scope="module")
def main_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("capture_main")
    _write_gz(root / "guardian_capture_1780000000_1.jsonl.gz", [R1, R2])
    _write_gz(root / "guardian_capture_1780000100_2.jsonl.gz", [R3, R4])
    _write_gz(root / "guardian_capture_current.jsonl.gz", [LEGACY])  # legacy active .gz
    (root / "guardian_capture_1780000200_3.jsonl.gz").write_bytes(TRUNCATED_GZ_BYTES)
    (root / "guardian_capture_current.jsonl").write_text(PLAIN_LINES, encoding="utf-8")
    # Noise that discovery must skip.
    (root / ".capture_state.json").write_text('{"rotated_seq": 4}')
    (root / "guardian_capture_1780000300_4.jsonl.sha256").write_text("deadbeef\n")
    media = root / "media"
    media.mkdir()
    _write_jsonl(media / "decoy.jsonl", [DECOY])
    return root


@pytest.fixture(scope="module")
def truncated_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("capture_trunc")
    (root / "guardian_capture_1780000200_3.jsonl.gz").write_bytes(TRUNCATED_GZ_BYTES)
    return root


# ── Helpers ────────────────────────────────────────────────────────────


def run_query(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--root", str(root), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )


def _count(root, *args) -> int:
    proc = run_query(root, *args, "--count")
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


def _json_records(root, *args) -> list[dict]:
    proc = run_query(root, *args, "--json")
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines()]


# ── Full scan / tolerance ──────────────────────────────────────────────


def test_full_scan_reads_all_records_tolerating_truncated_gz(main_root, truncated_root):
    # Truncated file alone: decodes 1 or 2 records, then stops cleanly.
    t_proc = run_query(truncated_root, "--count")
    assert t_proc.returncode == 0, t_proc.stderr
    t = int(t_proc.stdout.strip())
    assert 1 <= t <= 2

    proc = run_query(main_root, "--count")
    assert proc.returncode == 0, proc.stderr
    complete_total = 2 + 2 + 1 + 8  # gz1 + gz2 + legacy active gz + plain records
    assert int(proc.stdout.strip()) == complete_total + t

    # First record of the truncated file always survives; second only if the
    # truncation point left its line intact.
    assert _count(main_root, "--request-id", "rq-trunc-a") == 1
    assert _count(main_root, "--request-id", "rq-trunc-b") == t - 1

    # media/ decoy and skipped sidecars/state files never leak into the scan.
    assert _count(main_root, "--request-id", "rq-decoy") == 0


def test_verbose_reports_scan_statistics(main_root, truncated_root):
    t = _count(truncated_root)
    proc = run_query(main_root, "--count", "--verbose")
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr
    assert "files_scanned=5" in err  # 3 rotated gz + legacy active gz + plain
    assert f"records_read={13 + t}" in err
    assert "skipped_json_errors=1" in err
    assert "skipped_empty_lines=2" in err
    assert "partial_files=1" in err
    assert "EOFError" in err
    # Verbose goes to stderr; stdout stays a clean count for piping.
    assert int(proc.stdout.strip()) == 13 + t


# ── Numeric tolerance ──────────────────────────────────────────────────


def test_min_completion_matches_float_token_record(main_root):
    # Legacy record stores completion_tokens as 131072.0 (float).
    assert _count(main_root, "--request-id", "rq-float", "--min-completion", "131072") == 1
    assert _count(main_root, "--request-id", "rq-float", "--min-completion", "131073") == 0
    # Same numeric semantics for the prompt-side float (1024.0).
    assert _count(main_root, "--request-id", "rq-float", "--min-prompt", "1024") == 1
    assert _count(main_root, "--request-id", "rq-float", "--min-prompt", "1025") == 0


# ── Time filtering ─────────────────────────────────────────────────────


def test_since_until_boundaries(main_root):
    rid = "rq-gz2-a"  # completed at 2026-08-21T23:30:00.000Z
    # --since is inclusive, --until is exclusive (documented half-open window).
    assert _count(main_root, "--request-id", rid, "--since", "2026-08-21T23:30:00Z") == 1
    assert _count(main_root, "--request-id", rid, "--until", "2026-08-21T23:30:00Z") == 0
    # Naive ISO timestamps are treated as UTC.
    assert _count(main_root, "--request-id", rid, "--since", "2026-08-21T23:30:00") == 1
    # Epoch seconds are accepted too.
    epoch = datetime(2026, 8, 21, 23, 30, tzinfo=timezone.utc).timestamp()
    assert _count(main_root, "--request-id", rid, "--since", str(epoch)) == 1
    # A plain timestamp_utc record is unaffected by the same window.
    assert _count(main_root, "--request-id", "rq-gz1-a", "--since", "2026-08-20T09:00:00Z") == 1


def test_completion_time_semantics_use_completed_at_utc(main_root):
    # P7 carries timestamp_utc=2026-08-19 but completed_at_utc=2026-08-25.
    assert _count(main_root, "--request-id", "rq-ctime", "--since", "2026-08-24T00:00:00Z") == 1
    # If the tool used timestamp_utc, this --until would INCLUDE the record;
    # completion-time semantics exclude it.
    assert _count(main_root, "--request-id", "rq-ctime", "--until", "2026-08-21T00:00:00Z") == 0


# ── Wasted-output filter ───────────────────────────────────────────────


def test_empty_content_only_picks_exactly_the_waste_records(main_root):
    records = _json_records(main_root, "--empty-content-only")
    ids = {rec["request_id"] for rec in records}
    assert ids == {"rq-gz2-a", "rq-waste-null", "rq-waste-empty"}
    assert _count(main_root, "--empty-content-only") == 3
    # Zero-token completed events are not "waste".
    assert "rq-zero" not in ids
    # Real content is not "waste".
    assert "rq-ok" not in ids


# ── Combinable filters ─────────────────────────────────────────────────


def test_filters_client_model_route_event_type_request_id(main_root):
    assert _count(
        main_root, "--client", "c0ffee00", "--route", "local", "--event-type", "request_completed"
    ) == 7  # R2, P1, P3, P4, P5, P6, P8

    # --model is a case-insensitive substring on requested_model OR resolved_model.
    assert _count(main_root, "--model", "ymq", "--event-type", "request_completed") == 1  # R3 resolved
    assert _count(main_root, "--model", "ANTHROPIC") == 2  # R3 + R4

    # Missing fields never match a filter and never raise.
    assert _count(main_root, "--request-id", "rq-minimal") == 1
    assert _count(main_root, "--request-id", "rq-minimal", "--model", "local") == 0

    assert _count(main_root, "--limit", "3") == 3


def test_json_mode_prints_lines_exactly_as_stored(main_root):
    proc = run_query(main_root, "--request-id", "rq-float", "--json")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0] == json.dumps(P1)  # raw stored line, float not re-cast
    assert json.loads(lines[0])["completion_tokens"] == 131072.0


# ── Daily rollup (C10) ─────────────────────────────────────────────────


def test_rollup_daily_math(main_root):
    proc = run_query(
        main_root,
        "--since", "2026-08-24T00:00:00Z",
        "--until", "2026-08-25T00:00:00Z",
        "--rollup", "daily",
    )
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines()]

    def find(route, model):
        return [r for r in rows if r["route"] == route and r["model"] == model]

    # P3..P6 share one bucket: local / local-b / c0ffee000009 on 2026-08-24.
    buckets = find("local", "local-b")
    assert len(buckets) == 1
    row = buckets[0]
    assert row["date"] == "2026-08-24"
    assert row["client_ref"] == "c0ffee000009"
    assert row["calls"] == 4
    assert row["received"] == 0
    assert row["completed"] == 4
    assert row["failed"] == 0
    assert row["prompt_tokens"] == 50  # 11 + 12 + 13 + 14
    assert row["completion_tokens"] == 33  # 9 + 9 + 9 + 6
    assert row["completion_with_empty_content"] == 2  # P3 (null) + P4 ("")
    assert row["finish_reason_counts"] == {"stop": 3, "length": 1}
    assert row["cost_sum"] == 1.25  # only P5 carries cost
    assert row["reasoning_tokens_sum"] == 12  # P5 (5) + P6 (7)

    # The minimal P2 record (all bucket fields missing) forms a null bucket.
    nulls = [r for r in rows if r["route"] is None and r["model"] is None]
    assert len(nulls) == 1
    assert nulls[0]["calls"] == 1
    assert nulls[0]["received"] == 1
    assert nulls[0]["client_ref"] is None


def test_rollup_cost_and_reasoning_null_safety(main_root):
    # Bucket with a cost but no reasoning tokens.
    rows = [
        json.loads(line)
        for line in run_query(main_root, "--request-id", "rq-gz1-b", "--rollup", "daily").stdout.splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["cost_sum"] == 0.5
    assert rows[0]["reasoning_tokens_sum"] is None
    assert rows[0]["finish_reason_counts"] == {"stop": 1}
    assert rows[0]["completion_with_empty_content"] == 0

    # Bucket with neither cost nor reasoning tokens -> both null.
    rows = [
        json.loads(line)
        for line in run_query(main_root, "--request-id", "rq-gz2-a", "--rollup", "daily").stdout.splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["cost_sum"] is None
    assert rows[0]["reasoning_tokens_sum"] is None
    assert rows[0]["route"] == "cloud"
    assert rows[0]["completion_with_empty_content"] == 1
    assert rows[0]["finish_reason_counts"] == {"length": 1}


def test_rollup_respects_route_filter_and_counts_failed(main_root):
    proc = run_query(main_root, "--route", "cloud", "--rollup", "daily")
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(rows) == 1  # R3 + R4 share day/model/client bucket
    assert rows[0]["date"] == "2026-08-21"
    assert rows[0]["calls"] == 2
    assert rows[0]["completed"] == 1
    assert rows[0]["failed"] == 1


def test_rollup_empty_selection_prints_nothing(main_root):
    proc = run_query(main_root, "--event-type", "request_cancelled", "--rollup", "daily")
    assert proc.returncode == 0
    assert proc.stdout == ""


# ── Exit codes and usage errors ────────────────────────────────────────


def test_exit_code_2_on_missing_root(tmp_path):
    proc = run_query(tmp_path / "does-not-exist", "--count")
    assert proc.returncode == 2
    assert "does not exist" in proc.stderr


def test_empty_results_exit_zero(tmp_path):
    empty_root = tmp_path / "empty-capture"
    empty_root.mkdir()
    proc = run_query(empty_root, "--json")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert _count(empty_root) == 0


def test_no_match_prints_nothing_in_json_mode(main_root):
    proc = run_query(main_root, "--request-id", "no-such-request", "--json")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_usage_errors_exit_2(main_root):
    assert run_query(main_root, "--min-completion", "abc").returncode == 2
    assert run_query(main_root, "--since", "not-a-date").returncode == 2
    assert run_query(main_root, "--route", "satellite").returncode == 2


def test_help_documents_completion_time_semantics():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "completed_at_utc" in proc.stdout
    assert "half-open" in proc.stdout
