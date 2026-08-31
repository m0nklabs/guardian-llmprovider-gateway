"""Unit tests for the guardianctl CLI capture-visibility pieces.

Covers the feedback C11 additions (retention config + on-disk inventory in
`status`) and the export file discovery across the new plain active file,
legacy gzip active file and completed gzip files.
"""

import argparse
import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.capture.config import CaptureConfig
from app.capture.wal_writer import ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_guardianctl():
    """Import scripts/guardianctl.py as a module (it needs scripts/ on sys.path
    for its sibling `_paths` import)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("guardianctl_test_module", SCRIPTS_DIR / "guardianctl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gctl():
    return _load_guardianctl()


@pytest.fixture
def capture_cfg(tmp_path):
    return CaptureConfig(
        enabled=True,
        local_capture=True,
        capture_root=str(tmp_path / "capture"),
        retention_days=7,
        max_capture_bytes=1024 * 1024,
        max_file_bytes=4096,
        max_file_age_seconds=600,
    )


def _write_gz(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _write_sidecar(path: Path) -> Path:
    sidecar = path.with_suffix(".sha256")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar.write_text(f"{digest}  {path.name}\n")
    return sidecar


class TestFmtAge:
    def test_seconds_minutes_hours_days(self, gctl):
        assert gctl._fmt_age(42) == "42s"
        assert gctl._fmt_age(90) == "1m"
        assert gctl._fmt_age(5 * 60) == "5m"
        assert gctl._fmt_age(3 * 3600) == "3h"
        assert gctl._fmt_age(5 * 86400) == "5d"


class TestCaptureRetentionSummary:
    def test_empty_root_reports_config_only(self, gctl, capture_cfg):
        summary = gctl._capture_retention_summary(capture_cfg)
        assert summary["retention_days"] == 7
        assert summary["max_capture_bytes"] == 1024 * 1024
        assert summary["max_file_bytes"] == 4096
        assert summary["max_file_age_seconds"] == 600
        assert summary["active_file"] is None
        assert summary["rotated_files"] == 0
        assert summary["oldest_rotated_file"] is None
        assert summary["newest_rotated_file"] is None

    def test_missing_root_is_not_an_error(self, gctl, tmp_path):
        cfg = CaptureConfig(capture_root=str(tmp_path / "does_not_exist"))
        summary = gctl._capture_retention_summary(cfg)
        assert summary["rotated_files"] == 0

    def test_active_file_plain_reported(self, gctl, capture_cfg):
        root = Path(capture_cfg.capture_root)
        root.mkdir(parents=True)
        active = root / ACTIVE_FILENAME
        active.write_text('{"seq": 0}\n')

        summary = gctl._capture_retention_summary(capture_cfg)
        assert summary["active_file"]["name"] == ACTIVE_FILENAME
        assert summary["active_file"]["format"] == "plain"
        assert summary["active_file"]["size_bytes"] == len('{"seq": 0}\n')

    def test_active_file_legacy_gzip_reported(self, gctl, capture_cfg):
        root = Path(capture_cfg.capture_root)
        root.mkdir(parents=True)
        _write_gz(root / LEGACY_ACTIVE_FILENAME, [{"seq": 0}])

        summary = gctl._capture_retention_summary(capture_cfg)
        assert summary["active_file"]["name"] == LEGACY_ACTIVE_FILENAME
        assert summary["active_file"]["format"] == "legacy_gzip"

    def test_oldest_newest_rotated_files(self, gctl, capture_cfg):
        import os
        import time

        root = Path(capture_cfg.capture_root)
        root.mkdir(parents=True)
        _write_gz(root / "guardian_capture_1000000000_1.jsonl.gz", [{"seq": 0}])
        _write_gz(root / "guardian_capture_2000000000_2.jsonl.gz", [{"seq": 1}])
        # Age is mtime-based — age the oldest file 2 days back.
        old = root / "guardian_capture_1000000000_1.jsonl.gz"
        old_time = time.time() - (2 * 86400)
        os.utime(old, (old_time, old_time))

        summary = gctl._capture_retention_summary(capture_cfg)
        assert summary["rotated_files"] == 2
        assert summary["oldest_rotated_file"]["name"] == "guardian_capture_1000000000_1.jsonl.gz"
        assert summary["newest_rotated_file"]["name"] == "guardian_capture_2000000000_2.jsonl.gz"
        assert summary["oldest_rotated_file"]["age_seconds"] >= 0
        assert summary["newest_rotated_file"]["age_seconds"] >= 0
        assert summary["oldest_rotated_file"]["age_seconds"] > 86400
        assert summary["newest_rotated_file"]["age_seconds"] < 86400

    def test_sidecars_and_state_are_not_rotated_files(self, gctl, capture_cfg):
        root = Path(capture_cfg.capture_root)
        root.mkdir(parents=True)
        gz = _write_gz(root / "guardian_capture_1000000000_1.jsonl.gz", [{"seq": 0}])
        _write_sidecar(gz)
        (root / ".capture_state.json").write_text("{}")

        summary = gctl._capture_retention_summary(capture_cfg)
        assert summary["rotated_files"] == 1

    def test_relative_capture_root_resolved_against_repo_root(self, gctl):
        cfg = CaptureConfig(capture_root="data/capture")
        summary = gctl._capture_retention_summary(cfg)
        assert Path(summary["capture_root"]).is_absolute()


class TestIterWalEvents:
    def test_reads_completed_and_active_last(self, gctl, tmp_path):
        _write_gz(tmp_path / "guardian_capture_1000000000_1.jsonl.gz", [{"seq": 1}])
        # plain completed leftover (pre-compression) is also readable
        (tmp_path / "guardian_capture_2000000000_2.jsonl").write_text('{"seq": 2}\n')
        (tmp_path / ACTIVE_FILENAME).write_text('{"seq": 3}\n')

        events = [(p.name, ev["seq"]) for p, ev in gctl._iter_wal_events(
            tmp_path, verify_auth=False, verify_checksums=False, secret="")]
        assert events == [
            ("guardian_capture_1000000000_1.jsonl.gz", 1),
            ("guardian_capture_2000000000_2.jsonl", 2),
            (ACTIVE_FILENAME, 3),
        ]

    def test_legacy_gzip_active_read_last(self, gctl, tmp_path):
        _write_gz(tmp_path / "guardian_capture_1000000000_1.jsonl.gz", [{"seq": 1}])
        _write_gz(tmp_path / LEGACY_ACTIVE_FILENAME, [{"seq": 9}])

        events = [(p.name, ev["seq"]) for p, ev in gctl._iter_wal_events(
            tmp_path, verify_auth=False, verify_checksums=False, secret="")]
        assert events[-1] == (LEGACY_ACTIVE_FILENAME, 9)

    def test_checksum_verification(self, gctl, tmp_path, capsys):
        gz = _write_gz(tmp_path / "guardian_capture_1000000000_1.jsonl.gz", [{"seq": 1}])
        _write_sidecar(gz)

        events = list(gctl._iter_wal_events(
            tmp_path, verify_auth=False, verify_checksums=True, secret=""))
        assert len(events) == 1
        assert "CHECKSUM MISMATCH" not in capsys.readouterr().err

        # Corrupt the sidecar -> mismatch warning, records still yielded.
        gz.with_suffix(".sha256").write_text("0" * 64 + "  x.jsonl.sha256\n")
        events = list(gctl._iter_wal_events(
            tmp_path, verify_auth=False, verify_checksums=True, secret=""))
        assert len(events) == 1
        assert "CHECKSUM MISMATCH" in capsys.readouterr().err

    def test_checksum_not_checked_for_active_files(self, gctl, tmp_path, capsys):
        # Active files have no sidecar; verification must not warn about them.
        _write_gz(tmp_path / LEGACY_ACTIVE_FILENAME, [{"seq": 9}])
        (tmp_path / ACTIVE_FILENAME).write_text('{"seq": 3}\n')

        events = list(gctl._iter_wal_events(
            tmp_path, verify_auth=False, verify_checksums=True, secret=""))
        assert len(events) == 2
        assert "CHECKSUM MISMATCH" not in capsys.readouterr().err


class TestCmdFiles:
    def test_labels_new_and_legacy_active_files(self, gctl, tmp_path, capsys, monkeypatch):
        root = tmp_path / "capture"
        root.mkdir()
        (root / ACTIVE_FILENAME).write_text('{"seq": 0}\n')
        _write_gz(root / LEGACY_ACTIVE_FILENAME, [{"seq": 1}])
        rotated = _write_gz(root / "guardian_capture_1000000000_1.jsonl.gz", [{"seq": 2}])
        _write_sidecar(rotated)

        monkeypatch.setattr(gctl, "CAPTURE_ROOT", root)
        gctl.cmd_files(argparse.Namespace(json=False))
        out = capsys.readouterr().out
        assert "active (plain)" in out
        assert "legacy (gzip)" in out
        assert "gzip (rotated)" in out
        assert "checksum" in out

    def test_json_output_uses_relpath(self, gctl, tmp_path, capsys, monkeypatch):
        root = tmp_path / "capture"
        root.mkdir()
        (root / ACTIVE_FILENAME).write_text('{"seq": 0}\n')
        monkeypatch.setattr(gctl, "CAPTURE_ROOT", root)
        gctl.cmd_files(argparse.Namespace(json=True))
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["size_bytes"] == len('{"seq": 0}\n')
        assert not data[0]["path"].startswith("/"), "paths are repo-relative"


class TestStatusContract:
    def test_status_json_includes_retention_summary(self, gctl, monkeypatch, capture_cfg):
        """`status --json` merges the local retention summary into the API
        result without touching the API call itself."""
        captured = {}

        def fake_api_request(method, endpoint, **kwargs):
            captured["endpoint"] = endpoint
            return {"config": {"enabled": True}, "runtime": {"foo": 1}}

        monkeypatch.setattr(gctl, "_api_request", fake_api_request)
        monkeypatch.setattr(gctl, "_capture_retention_summary", lambda: {"retention_days": 7})

        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gctl.cmd_status(argparse.Namespace(json=True))
        data = json.loads(buf.getvalue())
        assert captured["endpoint"] == "/api/capture/status"
        assert data["config"] == {"enabled": True}
        assert data["retention"] == {"retention_days": 7}
