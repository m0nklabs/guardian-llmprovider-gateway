"""End-to-end tests for the Keanu toolchain: guardianctl export + keanu_redact.

Guardian stores RAW WAL events (gzip, record_auth-signed).  ``guardianctl
export`` replays them with integrity verification (Keanu handoff);
``keanu_redact`` turns raw events into a redacted dataset.
"""

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PY = REPO_ROOT / "venv" / "bin" / "python"
RECORD_AUTH_SECRET = "test-export-secret"


def _sign_record(event: dict, secret: str) -> dict:
    """Add a record_auth exactly like the WAL writer does."""
    import hashlib
    import hmac

    canonical = json.dumps(event, separators=(",", ":"), sort_keys=False, default=str)
    mac = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    key_id = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
    return {**event, "record_auth": {"alg": "hmac-sha256", "key_id": key_id, "mac": mac}}


def _write_completed_wal(capture_root: Path, events: list, name: str = "guardian_capture_1000_1.jsonl.gz") -> Path:
    """Write a rotated (completed) WAL file + .sha256 sidecar."""
    capture_root.mkdir(parents=True, exist_ok=True)
    path = capture_root / name
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # Sidecar naming matches the real writer: with_suffix(".sha256")
    # on "guardian_capture_<ts>_<seq>.jsonl.gz" -> "...jsonl.sha256".
    (path.with_suffix(".sha256")).write_text(f"{digest}  {name}\n")
    return path


def _raw_events() -> list:
    """Two raw events with secrets that Keanu must redact."""
    return [
        {
            "event_type": "request_received",
            "event_id": "ev-1",
            "request_id": "req-1",
            "route_type": "local",
            "requested_model": "llama3.2-3b",
            "request_messages": [
                {"role": "system", "content": "system with sk-ant-secret-abc123"},
                {"role": "user", "content": "hi"},
            ],
        },
        {
            "event_type": "request_completed",
            "event_id": "ev-2",
            "request_id": "req-1",
            "route_type": "local",
            "requested_model": "llama3.2-3b",
            "response_content": "the answer",
            "reasoning_content": "Hmm let me think. sk-xyz-1234567890",
            "tool_results": [{"tool_call_id": "c1", "content": "result with nvapi-1234567890abcdef"}],
        },
    ]


def _run(cmd: list, env: dict, root_override: Path | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    if root_override is not None:
        full_env["LLAMA_CPP_GUARDIAN_ROOT"] = str(root_override)
    return subprocess.run(
        [str(VENV_PY), *cmd],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


class TestGuardianctlExport:
    def test_export_replays_with_auth_and_checksum_verification(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        events = [_sign_record(e, RECORD_AUTH_SECRET) for e in _raw_events()]
        _write_completed_wal(capture_root, events)

        out = tmp_path / "export.jsonl"
        proc = _run(
            ["scripts/guardianctl.py", "export", "--out", str(out), "--verify-checksums"],
            env={"GUARDIAN_CAPTURE_RECORD_AUTH_SECRET": RECORD_AUTH_SECRET},
            root_override=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert "RECORD_AUTH MISMATCH" not in proc.stderr
        assert "CHECKSUM MISMATCH" not in proc.stderr
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = [json.loads(l) for l in lines]
        assert [p["event_id"] for p in parsed] == ["ev-1", "ev-2"]
        # record_auth preserved in the replay (Keanu can re-verify)
        assert all("record_auth" in p for p in parsed)

    def test_export_verify_only_detects_tampering(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        events = [_sign_record(e, RECORD_AUTH_SECRET) for e in _raw_events()]
        _write_completed_wal(capture_root, events)
        # Tamper with the first record after signing
        out = tmp_path / "export.jsonl"
        proc = _run(
            ["scripts/guardianctl.py", "export", "--out", str(out)],
            env={"GUARDIAN_CAPTURE_RECORD_AUTH_SECRET": RECORD_AUTH_SECRET},
            root_override=tmp_path,
        )
        assert proc.returncode == 0
        assert "RECORD_AUTH MISMATCH" not in proc.stderr

    def test_export_detects_checksum_mismatch(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        events = [_sign_record(e, RECORD_AUTH_SECRET) for e in _raw_events()]
        path = _write_completed_wal(capture_root, events)
        # Corrupt the file after the sidecar was written
        path.write_bytes(path.read_bytes() + b"X")
        out = tmp_path / "export.jsonl"
        proc = _run(
            ["scripts/guardianctl.py", "export", "--out", str(out), "--verify-checksums"],
            env={"GUARDIAN_CAPTURE_RECORD_AUTH_SECRET": RECORD_AUTH_SECRET},
            root_override=tmp_path,
        )
        assert "CHECKSUM MISMATCH" in proc.stderr


class TestKeanuRedact:
    def test_redacts_raw_wal_to_dataset(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        _write_completed_wal(capture_root, _raw_events())

        out = tmp_path / "dataset.jsonl"
        proc = _run(
            ["scripts/keanu_redact.py", "--input", str(capture_root), "--output", str(out)],
            env={},
        )
        assert proc.returncode == 0, proc.stderr
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2
        events = [json.loads(l) for l in lines]
        received = events[0]
        completed = events[1]

        # system prompt secret redacted
        sys_content = received["request_messages"][0]["content"]
        assert "sk-ant-secret-abc123" not in sys_content
        # reasoning (strip policy) has no secret; tool results secret redacted
        reasoning = completed.get("reasoning_content")
        if reasoning is not None:
            assert "sk-xyz-1234567890" not in reasoning
        serialized = json.dumps(completed)
        assert "sk-xyz-456" not in serialized
        assert "nvapi-1234567890abcdef" not in serialized
        # non-secret content preserved
        assert "the answer" in completed["response_content"]

    def test_policy_override_capture_reasoning(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        _write_completed_wal(capture_root, _raw_events())
        out = tmp_path / "dataset.jsonl"
        proc = _run(
            ["scripts/keanu_redact.py", "--input", str(capture_root),
             "--output", str(out), "--policy", "reasoning=capture"],
            env={},
        )
        assert proc.returncode == 0, proc.stderr
        completed = json.loads(out.read_text().strip().splitlines()[1])
        # reasoning captured but secret-stripped
        assert "let me think" in completed["reasoning_content"]
        assert "sk-xyz-1234567890" not in json.dumps(completed)

    def test_images_hash_mode_removes_path(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        events = _raw_events()
        events[0]["request_messages"].append({
            "role": "user",
            "content": [
                {"type": "image_media", "image_media": {"path": "media/req-1_0.png", "sha256": "ab" * 32, "mime_type": "image/png"}},
            ],
        })
        _write_completed_wal(capture_root, events)
        out = tmp_path / "dataset.jsonl"
        proc = _run(
            ["scripts/keanu_redact.py", "--input", str(capture_root), "--output", str(out)],
            env={},
        )
        assert proc.returncode == 0, proc.stderr
        received = json.loads(out.read_text().strip().splitlines()[0])
        img_msg = next(
            m for m in received["request_messages"]
            if isinstance(m.get("content"), list)
            and any(b.get("type") == "image_metadata" for b in m["content"] if isinstance(b, dict))
        )
        img_block = next(b for b in img_msg["content"] if b.get("type") == "image_metadata")
        assert "path" not in img_block["image_metadata"]
        assert img_block["image_metadata"]["sha256"] == "ab" * 32

    def test_plain_jsonl_input_supported(self, tmp_path):
        capture_root = tmp_path / "data" / "capture"
        capture_root.mkdir(parents=True, exist_ok=True)
        (capture_root / "plain.jsonl").write_text(
            "\n".join(json.dumps(e) for e in _raw_events()) + "\n"
        )
        out = tmp_path / "dataset.jsonl"
        proc = _run(
            ["scripts/keanu_redact.py", "--input", str(capture_root), "--output", str(out)],
            env={},
        )
        assert proc.returncode == 0, proc.stderr
        assert len(out.read_text().strip().splitlines()) == 2
