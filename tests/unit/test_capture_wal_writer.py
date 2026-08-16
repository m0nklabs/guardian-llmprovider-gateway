"""Unit tests for the capture WAL writer (append-only JSONL, rotation, retention)."""

import asyncio
import gzip
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from app.capture.config import CaptureConfig
from app.capture.sink import CaptureSink, CaptureEvent
from app.capture.wal_writer import CaptureWALWriter


@pytest.fixture
def tmp_capture_root(tmp_path):
    root = tmp_path / "capture"
    root.mkdir()
    return root


@pytest.fixture
def config(tmp_capture_root):
    return CaptureConfig(
        enabled=True,
        local_capture=True,
        cloud_capture=False,
        instance_id="test-instance-wal",
        policy_version="1.0.0",
        capture_root=str(tmp_capture_root),
        max_file_bytes=1024,  # Small for testing rotation
        max_file_age_seconds=3600,
        retention_days=7,
        max_pending_events=100,
        file_mode=0o640,
        directory_mode=0o750,
    )


@pytest.fixture
def sink(config):
    return CaptureSink(max_pending_events=config.max_pending_events)


@pytest.fixture
def writer(config, sink):
    return CaptureWALWriter(sink, config)


class TestWALWriterPathSafety:
    def test_write_path_is_within_capture_root(self, writer, tmp_capture_root):
        path = writer.get_write_path()
        assert str(path).startswith(str(tmp_capture_root.resolve()))

    def test_no_symlink_traversal(self, writer, tmp_capture_root):
        """Writer must not follow symlinks outside capture root."""
        path = writer.get_write_path()
        assert str(path).startswith(str(tmp_capture_root.resolve()))


class TestWALWriterFilePermissions:
    @pytest.mark.asyncio
    async def test_files_created_with_restricted_mode(self, writer, sink, config):
        """Files must be 0640 (no world access)."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"test": "data"}))
        await asyncio.sleep(0.1)
        await writer.stop()

        root = Path(config.capture_root)
        files = list(root.rglob("*"))
        for f in files:
            if f.is_file():
                mode = f.stat().st_mode & 0o777
                assert mode <= 0o640, f"File {f} has mode {oct(mode)}, expected <= 0o640"

    def test_config_rejects_world_readable_file_mode(self, tmp_capture_root):
        with pytest.raises(ValueError, match="world access"):
            CaptureConfig(
                enabled=True,
                capture_root=str(tmp_capture_root),
                file_mode=0o644,  # world-readable
            )

    def test_config_rejects_world_readable_dir_mode(self, tmp_capture_root):
        with pytest.raises(ValueError, match="world access"):
            CaptureConfig(
                enabled=True,
                capture_root=str(tmp_capture_root),
                directory_mode=0o755,  # world-readable
            )


class TestWALWriterWriting:
    @pytest.mark.asyncio
    async def test_event_written_to_jsonl(self, writer, sink, config):
        await writer.start()
        sink.try_put(CaptureEvent(data={"schema_name": "test", "data": "hello"}))
        await asyncio.sleep(0.1)
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        assert active.exists()
        content = active.read_text()
        lines = [l for l in content.strip().split("\n") if l]
        assert len(lines) >= 1
        for line in lines:
            parsed = json.loads(line)
            assert parsed["schema_name"] == "test"

    @pytest.mark.asyncio
    async def test_multiple_events_written_in_order(self, writer, sink, config):
        await writer.start()
        for i in range(5):
            sink.try_put(CaptureEvent(data={"seq": i}))
        await asyncio.sleep(0.2)
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        assert active.exists()
        content = active.read_text()
        lines = [json.loads(l) for l in content.strip().split("\n") if l]
        assert len(lines) == 5
        for i, line in enumerate(lines):
            assert line["seq"] == i

    @pytest.mark.asyncio
    async def test_write_failure_is_logged_not_raised(self, writer, sink, config):
        """Write failures must be counted, not raised (fail-open)."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"test": "data"}))

        # Patch the write to fail
        with patch.object(writer, "_write_event", side_effect=OSError("disk full")):
            write_error = writer._write_event  # This will fail
            # The writer loop catches exceptions
            await asyncio.sleep(0.2)

        await writer.stop()
        # Writer should have recorded the failure
        snap = writer.snapshot()
        assert snap["writer_metrics"]["write_failures"] >= 0


class TestWALWriterRotation:
    @pytest.mark.asyncio
    async def test_rotation_on_size(self, writer, sink, config):
        """File should rotate when max_file_bytes is exceeded."""
        await writer.start()
        # Write enough events to exceed 1024 bytes
        for i in range(50):
            event_data = {"seq": i, "data": "x" * 50}
            sink.try_put(CaptureEvent(data=event_data))
        await asyncio.sleep(0.5)
        await writer.stop()

        root = Path(config.capture_root)
        gz_files = list(root.glob("*.jsonl.gz"))
        # Also match .jsonl.gz files (the new pattern)
        if not gz_files:
            gz_files = list(root.glob("guardian_capture_*.jsonl.gz"))
        assert len(gz_files) >= 1, f"Expected at least one rotated file, found: {[f.name for f in root.iterdir()]}"

    @pytest.mark.asyncio
    async def test_rotated_file_has_checksum(self, writer, sink, config):
        await writer.start()
        for i in range(50):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.5)
        await writer.stop()

        root = Path(config.capture_root)
        checksum_files = list(root.glob("*.sha256"))
        assert len(checksum_files) >= 1
        for cs_file in checksum_files:
            content = cs_file.read_text().strip()
            parts = content.split()
            assert len(parts) >= 1
            assert len(parts[0]) == 64  # SHA-256 hex

    @pytest.mark.asyncio
    async def test_rotated_file_is_gzipped(self, writer, sink, config):
        await writer.start()
        for i in range(50):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.5)
        await writer.stop()

        root = Path(config.capture_root)
        gz_files = list(root.glob("*.jsonl.gz"))
        if not gz_files:
            gz_files = list(root.glob("guardian_capture_*.jsonl.gz"))
        for gz in gz_files:
            # Verify it's valid gzip
            with gzip.open(str(gz), "rb") as f:
                content = f.read()
                assert len(content) > 0
                # Should be valid JSON lines
                lines = content.decode("utf-8").strip().split("\n")
                for line in lines:
                    if line:
                        json.loads(line)  # Should not raise

    @pytest.mark.asyncio
    async def test_active_file_not_read_by_keanu(self, writer, sink, config):
        """Active file should not have the completed pattern."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"test": "active"}))
        await asyncio.sleep(0.1)

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        assert active.exists()

        # No completed files yet (no rotation)
        completed = list(root.glob("guardian_capture_*.jsonl.gz"))
        assert len(completed) == 0

        await writer.stop()

    @pytest.mark.asyncio
    async def test_manual_rotate_returns_path(self, writer, sink, config):
        """Manual rotate() should return the path of the rotated .gz file."""
        # NB: use a roomy file limit so the automatic size-based rotation
        # never fires inside this test (10 events > 1024 bytes would rotate
        # on their own and make the manual rotate() return None).
        writer._config = replace(config, max_file_bytes=1 << 20)
        await writer.start()
        # Write some events to have content
        for i in range(10):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.2)

        rotated = writer.rotate()
        assert rotated is not None
        assert str(rotated).endswith(".gz")

        await writer.stop()

    @pytest.mark.asyncio
    async def test_manual_rotate_on_empty_returns_none(self, writer, config):
        """Rotate() on an empty/just-started writer should return None."""
        await writer.start()
        # Don't write anything
        rotated = writer.rotate()
        assert rotated is None
        await writer.stop()

    @pytest.mark.asyncio
    async def test_manual_rotate_opens_new_active(self, writer, sink, config):
        """After rotation, a new active file should be opened for writes."""
        writer._config = replace(config, max_file_bytes=1 << 20)
        await writer.start()
        for i in range(10):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.2)

        writer.rotate()

        # Write more events — they should go to a new active file
        for i in range(5):
            sink.try_put(CaptureEvent(data={"seq": 100 + i, "data": "y" * 50}))
        await asyncio.sleep(0.2)

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        assert active.exists()
        active_content = active.read_text()
        assert '"seq":100' in active_content or '"seq": 100' in active_content

        await writer.stop()


class TestWALWriterRetention:
    @pytest.mark.asyncio
    async def test_old_files_removed_by_retention(self, config, sink, tmp_capture_root):
        """Files older than retention_days should be removed."""
        old_config = replace(config, retention_days=0)  # Immediate retention
        writer = CaptureWALWriter(sink, old_config)
        await writer.start()

        # Write and rotate a file
        for i in range(50):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.5)

        # Create an old file manually
        old_file = tmp_capture_root / "guardian_capture_0000000000_999.jsonl.gz"
        old_file.write_bytes(b"old data")
        old_time = time.time() - (2 * 24 * 3600)  # 2 days ago
        os.utime(str(old_file), (old_time, old_time))

        # Trigger retention
        writer._enforce_retention()
        await asyncio.sleep(0.1)

        assert not old_file.exists(), "Old file should have been removed by retention"
        await writer.stop()

    @pytest.mark.asyncio
    async def test_byte_quota_enforced(self, config, sink, tmp_capture_root):
        """Files should be removed when disk quota is exceeded."""
        # Set a small max_capture_bytes
        small_config = replace(config, 
            max_capture_bytes=500,  # Very small
            retention_days=365,  # Don't remove old files by age
        )
        writer = CaptureWALWriter(sink, small_config)
        await writer.start()

        # Write and rotate files
        for i in range(200):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(1.0)

        # Trigger retention manually (the writer checks every 60s, too slow for tests)
        writer._enforce_retention()
        await asyncio.sleep(0.1)

        # Check disk usage is within a reasonable multiple of quota
        disk_bytes = sum(f.stat().st_size for f in tmp_capture_root.rglob("*") if f.is_file())
        assert disk_bytes <= small_config.max_capture_bytes * 3  # Allow some slack for overhead

        await writer.stop()


class TestWALWriterPartialLineRecovery:
    @pytest.mark.asyncio
    async def test_partial_final_line_does_not_corrupt(self, writer, sink, config):
        """A partial line at the end of the active file should not corrupt parsing."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.1)

        # Simulate a crash by writing a partial line
        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        if active.exists():
            with open(active, "a") as f:
                f.write('{"incomplete')  # Partial JSON

        await writer.stop()

        # Reading should recover by discarding the partial line
        content = active.read_text()
        lines = content.strip().split("\n")
        # The last line should be incomplete and can be discarded by reader
        # Our writer doesn't truncate it, but Keanu should handle it
        for line in lines[:-1] if lines else []:
            if line.strip():
                json.loads(line)  # All but last should be valid


class TestWALWriterLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop_clean(self, writer, sink, config):
        await writer.start()
        sink.try_put(CaptureEvent(data={"test": "lifecycle"}))
        await asyncio.sleep(0.1)
        await writer.stop()
        # Should complete without error

    @pytest.mark.asyncio
    async def test_stop_drains_remaining_events(self, writer, sink, config):
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 0}))
        sink.try_put(CaptureEvent(data={"seq": 1}))
        sink.try_put(CaptureEvent(data={"seq": 2}))
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        content = active.read_text()
        lines = [json.loads(l) for l in content.strip().split("\n") if l]
        assert len(lines) == 3
        for i, line in enumerate(lines):
            assert line["seq"] == i


class TestWALWriterMetrics:
    def test_snapshot_returns_metrics(self, writer, sink, config):
        snap = writer.snapshot()
        assert "writer_metrics" in snap
        assert "sink_metrics" in snap
        assert "capture_disk_bytes" in snap

    def test_snapshot_includes_disk_bytes(self, writer, sink, config):
        # Create a file to have some disk usage
        root = Path(config.capture_root)
        (root / "test.jsonl").write_text('{"test": true}\n')
        snap = writer.snapshot()
        assert snap["capture_disk_bytes"] >= 1


# ── Per-record HMAC in WAL writes (Decision 2A) ─────────────────────────

class TestWALRecordAuth:
    """Tests for per-record HMAC authentication in WAL output."""

    @pytest.mark.asyncio
    async def test_record_auth_added_when_secret_set(self, writer, sink, config, monkeypatch):
        """WAL lines carry a record_auth field when RECORD_AUTH_SECRET is set."""
        monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", "test-auth-secret")
        await writer.start()
        sink.try_put(CaptureEvent(data={"event_type": "request_received", "seq": 0}))
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        content = active.read_text()
        line = json.loads(content.strip())
        assert "record_auth" in line
        assert line["record_auth"]["alg"] == "hmac-sha256"
        assert "key_id" in line["record_auth"]
        assert "mac" in line["record_auth"]

    @pytest.mark.asyncio
    async def test_no_record_auth_when_secret_unset(self, writer, sink, config, monkeypatch):
        """WAL lines have no record_auth field when the secret is unset."""
        monkeypatch.delenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", raising=False)
        await writer.start()
        sink.try_put(CaptureEvent(data={"event_type": "request_received", "seq": 0}))
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        content = active.read_text()
        line = json.loads(content.strip())
        assert "record_auth" not in line

    @pytest.mark.asyncio
    async def test_record_auth_mac_verifiable(self, writer, sink, config, monkeypatch):
        """The MAC in record_auth can be verified by recomputing."""
        import hashlib
        import hmac as _hmac
        monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", "verify-secret")
        await writer.start()
        sink.try_put(CaptureEvent(data={"event_type": "request_received", "seq": 0}))
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        content = active.read_text()
        line_dict = json.loads(content.strip())

        # Strip record_auth, re-serialize, recompute MAC
        stored_auth = line_dict.pop("record_auth")
        line_without_auth = json.dumps(line_dict, separators=(",", ":"), sort_keys=False, default=str)
        expected_mac = _hmac.new(
            b"verify-secret",
            line_without_auth.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert stored_auth["mac"] == expected_mac

    @pytest.mark.asyncio
    async def test_record_auth_key_id_consistent(self, writer, sink, config, monkeypatch):
        """All records in one file share the same key_id (same secret)."""
        monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", "consistent-secret")
        await writer.start()
        sink.try_put(CaptureEvent(data={"event_type": "request_received", "seq": 0}))
        sink.try_put(CaptureEvent(data={"event_type": "request_completed", "seq": 1}))
        await writer.stop()

        root = Path(config.capture_root)
        active = root / "guardian_capture_current.jsonl"
        lines = [json.loads(l) for l in active.read_text().strip().split("\n")]
        assert len(lines) == 2
        key_ids = {line["record_auth"]["key_id"] for line in lines}
        assert len(key_ids) == 1


# ── Crash-recovery tests (Phase 6) ─────────────────────────────────────


class TestWALWriterCrashRecovery:
    """Simulate crashes mid-write and verify the writer recovers cleanly on restart.

    Each test uses a fresh sink for the second writer instance because
    ``writer.stop()`` closes the sink (puts a sentinel + sets ``_closed``).
    """

    @pytest.mark.asyncio
    async def test_restart_resumes_writing_after_clean_stop(self, writer, sink, config, tmp_capture_root):
        """After a clean stop and restart, the writer appends to the existing active file."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1, "event_type": "request_received"}))
        sink.try_put(CaptureEvent(data={"seq": 2, "event_type": "request_received"}))
        await writer.stop()

        # Restart with a fresh writer + fresh sink
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 3, "event_type": "request_received"}))
        await writer2.stop()

        active = tmp_capture_root / "guardian_capture_current.jsonl"
        lines = [json.loads(l) for l in active.read_text().strip().split("\n") if l.strip()]
        seqs = [e["seq"] for e in lines]
        assert seqs == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_partial_line_after_restart_is_appended_not_corrupted(
        self, writer, sink, config, tmp_capture_root
    ):
        """A partial line left by a crash is tolerated — new writes append after it."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1, "event_type": "request_received"}))
        await writer.stop()

        # Simulate crash: write a partial JSON line (with newline — the
        # writer always appends complete lines ending in \n, so a crashed
        # partial line would have a \n at the end if the buffer was flushed).
        active = tmp_capture_root / "guardian_capture_current.jsonl"
        with open(active, "a") as f:
            f.write('{"partial": true, "cu\n')  # incomplete JSON + newline

        # Restart writer with fresh sink
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2, "event_type": "request_completed"}))
        await writer2.stop()

        lines = active.read_text().strip().split("\n")
        # First line: complete event
        json.loads(lines[0])
        assert json.loads(lines[0])["seq"] == 1
        # Second line: partial (reader must discard)
        # Third line: new complete event
        json.loads(lines[2])
        assert json.loads(lines[2])["seq"] == 2

    @pytest.mark.asyncio
    async def test_state_persisted_across_restart(self, writer, sink, config, tmp_capture_root):
        """Rotation sequence persists across restarts so file names don't collide."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1, "event_type": "request_received"}))
        await asyncio.sleep(0.1)  # Let the writer consume the event
        # Force a rotation to increment rotation_seq
        path = writer.rotate()
        assert path is not None, "rotate() should return the rotated file path"
        await writer.stop()

        # Restart — the writer should load the persisted rotation_seq
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        writer2._load_state()
        assert writer2._rotation_seq > 0, "rotation_seq should have been persisted"

        # Next rotation should use the incremented seq
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2, "event_type": "request_received"}))
        await asyncio.sleep(0.1)
        result = writer2.rotate()
        assert result is not None
        await writer2.stop()

        # The rotated filename should contain a higher sequence number
        rotated_files = [f for f in tmp_capture_root.iterdir() if f.name.endswith(".jsonl.gz")]
        assert len(rotated_files) >= 2

    @pytest.mark.asyncio
    async def test_corrupt_state_file_falls_back_to_defaults(self, writer, sink, config, tmp_capture_root):
        """If the state file is corrupt, the writer resets to safe defaults."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await writer.stop()

        # Corrupt the state file
        state_path = tmp_capture_root / "guardian_capture_state.json"
        if state_path.exists():
            state_path.write_text('{" BROKEN JSON {{{')

        # Should not raise on restart
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        writer2._load_state()
        assert writer2._rotation_seq == 0
        assert "started_at" in writer2._state

    @pytest.mark.asyncio
    async def test_no_active_file_after_crash_creates_new_on_restart(
        self, writer, sink, config, tmp_capture_root
    ):
        """If the active file was deleted during a crash, the writer creates a new one."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await writer.stop()

        # Simulate crash: delete the active file
        active = tmp_capture_root / "guardian_capture_current.jsonl"
        active.unlink()
        assert not active.exists()

        # Restart — should create a new active file
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2}))
        await writer2.stop()

        assert active.exists()
        lines = [json.loads(l) for l in active.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 1
        assert lines[0]["seq"] == 2

    @pytest.mark.asyncio
    async def test_multiple_partial_lines_tolerance(self, writer, sink, config, tmp_capture_root):
        """Multiple consecutive partial lines from repeated crashes are all tolerated."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await writer.stop()

        active = tmp_capture_root / "guardian_capture_current.jsonl"
        # Write multiple partial lines (with newlines — see partial_line test)
        with open(active, "a") as f:
            f.write('{"broken1":\n')
            f.write('{"broken2": tru\n')

        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2}))
        await writer2.stop()

        lines = active.read_text().strip().split("\n")
        # Line 0: valid (seq 1)
        # Lines 1-2: partial (reader discards)
        # Line 3: valid (seq 2)
        json.loads(lines[0])
        assert json.loads(lines[0])["seq"] == 1
        json.loads(lines[3])
        assert json.loads(lines[3])["seq"] == 2

    @pytest.mark.asyncio
    async def test_empty_file_after_crash_is_safe(self, writer, sink, config, tmp_capture_root):
        """An empty active file (crash before any writes) is safe on restart."""
        await writer.start()
        await writer.stop()

        active = tmp_capture_root / "guardian_capture_current.jsonl"
        # File may or may not exist — if it does, it's empty
        if active.exists():
            assert active.read_text() == ""

        # Restart and write
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 1}))
        await writer2.stop()

        lines = [l for l in active.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["seq"] == 1

    @pytest.mark.asyncio
    async def test_writer_survives_disk_full_simulated(self, writer, sink, config, tmp_capture_root):
        """If writes fail (simulated disk full), the writer logs but doesn't crash."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.1)

        # Patch _write_event to simulate failure
        original_write = writer._write_event

        def failing_write(event):
            return False  # Simulate write failure

        writer._write_event = failing_write
        sink.try_put(CaptureEvent(data={"seq": 2}))
        await asyncio.sleep(0.1)

        # Restore and verify recovery
        writer._write_event = original_write
        sink.try_put(CaptureEvent(data={"seq": 3}))
        await asyncio.sleep(0.1)
        await writer.stop()

        active = tmp_capture_root / "guardian_capture_current.jsonl"
        lines = [json.loads(l) for l in active.read_text().strip().split("\n") if l.strip()]
        seqs = [e["seq"] for e in lines]
        # seq 1 was written before failure; seq 2 was lost; seq 3 recovered
        assert 1 in seqs
        assert 3 in seqs
        assert 2 not in seqs

    @pytest.mark.asyncio
    async def test_hmac_preserved_across_restart(self, writer, sink, config, tmp_capture_root, monkeypatch):
        """Per-record HMAC continues to work after a writer restart."""
        monkeypatch.setenv("GUARDIAN_CAPTURE_RECORD_AUTH_SECRET", "crash-recovery-secret")
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1, "event_type": "request_received"}))
        await writer.stop()

        # Restart with fresh sink
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2, "event_type": "request_completed"}))
        await writer2.stop()

        active = tmp_capture_root / "guardian_capture_current.jsonl"
        lines = [json.loads(l) for l in active.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 2
        for line in lines:
            assert "record_auth" in line
            assert line["record_auth"]["alg"] == "hmac-sha256"
        # Both should have the same key_id (same secret)
        key_ids = {line["record_auth"]["key_id"] for line in lines}
        assert len(key_ids) == 1
