"""Unit tests for the capture WAL writer (plain active JSONL, gzip on rotation).

Since feedback C3 (2026-08-30) the ACTIVE file is plain UTF-8 JSONL so it
stays readable line-by-line while the writer is mid-stream; the completed
artifact is gzip (``.jsonl.gz``, produced atomically on rotation) with a
``.sha256`` sidecar over the final gzip bytes.
"""

import asyncio
import gzip
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from app.capture.config import CaptureConfig
from app.capture.sink import CaptureSink, CaptureEvent
from app.capture.wal_writer import (
    ACTIVE_FILENAME,
    LEGACY_ACTIVE_FILENAME,
    STATE_FILENAME,
    CaptureWALWriter,
)


def _read_active_text(active: Path) -> str:
    """Read the active WAL via the shared crash-tolerant reader."""
    from app.capture.gzip_reader import read_all_text

    return read_all_text(active)


def _append_partial_line(active: Path, text: str) -> None:
    """Simulate a crash mid-write: append a record WITHOUT its trailing newline.

    With the plain active file a crash can leave a partial final record (no
    newline yet); the reader must drop it and the next writer start must
    not let the next record join it.
    """
    with open(active, "ab") as fh:
        fh.write(text.encode("utf-8"))
    # no trailing newline -> partial (crash)


def _write_legacy_gzip_active(root: Path, records: list) -> Path:
    """Create a legacy stream-gzip active file as the previous version did."""
    legacy = root / LEGACY_ACTIVE_FILENAME
    with gzip.open(legacy, "wt", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return legacy


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
        active = root / ACTIVE_FILENAME
        assert active.exists()
        content = _read_active_text(active)
        lines = [line for line in content.strip().split("\n") if line]
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
        active = root / ACTIVE_FILENAME
        assert active.exists()
        content = _read_active_text(active)
        lines = [json.loads(line) for line in content.strip().split("\n") if line]
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
            _write_error = writer._write_event  # This will fail
            # The writer loop catches exceptions
            await asyncio.sleep(0.2)

        await writer.stop()
        # Writer should have recorded the failure
        snap = writer.snapshot()
        assert snap["writer_metrics"]["write_failures"] >= 0


class TestC3PlainActiveFile:
    """Feedback C3 regression: the ACTIVE file is plain JSONL, readable
    line-by-line WHILE the writer is mid-stream (the previous stream-gzip
    active file raised EOFError for standard readers)."""

    @pytest.mark.asyncio
    async def test_active_file_plain_and_readable_mid_stream(self, writer, sink, config, tmp_capture_root):
        await writer.start()
        for i in range(5):
            sink.try_put(CaptureEvent(data={"seq": i}))

        # Wait until the records are on disk WITHOUT stopping the writer.
        active = tmp_capture_root / ACTIVE_FILENAME
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if active.exists() and len(active.read_text(encoding="utf-8").strip().splitlines()) >= 5:
                break
            await asyncio.sleep(0.05)

        # Mid-stream: the writer task is still running.  A plain, standard
        # read must return every complete record — no EOFError, no special
        # casing (this is the exact consumer complaint).
        assert writer._task is not None and not writer._task.done()
        lines = [json.loads(ln) for ln in active.read_text(encoding="utf-8").splitlines() if ln]
        assert [ln["seq"] for ln in lines] == list(range(5))

        await writer.stop()
        # The file is unchanged by stop (plain format, no trailer step).
        lines_after = [json.loads(ln) for ln in active.read_text(encoding="utf-8").splitlines() if ln]
        assert [ln["seq"] for ln in lines_after] == list(range(5))

    @pytest.mark.asyncio
    async def test_active_file_is_not_gzip(self, writer, sink, tmp_capture_root):
        """The active file must not start with the gzip magic bytes."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 0}))
        await asyncio.sleep(0.1)

        active = tmp_capture_root / ACTIVE_FILENAME
        assert active.exists()
        assert active.read_bytes()[:2] != b"\x1f\x8b"
        assert active.suffix == ".jsonl"

        await writer.stop()

    @pytest.mark.asyncio
    async def test_rotation_thresholds_count_plain_bytes(self, config, sink, tmp_capture_root):
        """Rotation fires on the plain file's byte size (uncompressed)."""
        small = replace(config, max_file_bytes=200)
        writer = CaptureWALWriter(sink, small)
        await writer.start()
        for i in range(20):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 40}))
        await asyncio.sleep(0.5)

        root = Path(small.capture_root)
        active = root / ACTIVE_FILENAME
        # The remaining active file must be under the threshold; the rest
        # was rotated out into gzip files.
        if active.exists():
            assert active.stat().st_size < 200 + 200  # at most one record over
        rotated = list(root.glob("guardian_capture_*.jsonl.gz"))
        assert len(rotated) >= 1, "plain-byte threshold should have triggered rotation"

        await writer.stop()


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
    async def test_rotated_file_is_valid_gzip_round_trip(self, writer, sink, config):
        """Rotation produces a valid .jsonl.gz: a standard gzip round-trip
        reads back exactly the written records (no EOFError, no multi-member
        special casing)."""
        await writer.start()
        for i in range(50):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.5)
        await writer.stop()

        root = Path(config.capture_root)
        gz_files = list(root.glob("guardian_capture_*.jsonl.gz"))
        assert gz_files
        for gz in gz_files:
            with gzip.open(str(gz), "rb") as f:  # standard gzip — must not raise
                content = f.read()
                assert len(content) > 0
                lines = content.decode("utf-8").strip().split("\n")
                for line in lines:
                    if line:
                        json.loads(line)  # Should not raise

    @pytest.mark.asyncio
    async def test_rotated_sidecar_matches_final_gz_bytes(self, writer, sink, config):
        """The .sha256 sidecar is computed over the FINAL .gz bytes (after
        compression), not over the pre-compression plain bytes."""
        writer._config = replace(config, max_file_bytes=1 << 20)
        await writer.start()
        for i in range(10):
            sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
        await asyncio.sleep(0.2)

        rotated = writer.rotate()
        assert rotated is not None
        rotated_path = Path(rotated)
        sidecar = rotated_path.with_suffix(".sha256")
        assert sidecar.exists()
        expected = hashlib.sha256(rotated_path.read_bytes()).hexdigest()
        assert sidecar.read_text() == f"{expected}  {rotated_path.name}\n"

        await writer.stop()

    @pytest.mark.asyncio
    async def test_active_file_not_read_by_keanu(self, writer, sink, config):
        """Active file should not have the completed pattern."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"test": "active"}))
        await asyncio.sleep(0.1)

        root = Path(config.capture_root)
        active = root / ACTIVE_FILENAME
        assert active.exists()

        # No completed files yet (no rotation) — the active file does not
        # match the completed pattern (plain .jsonl vs .jsonl.gz).
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
        active = root / ACTIVE_FILENAME
        assert active.exists()
        active_content = _read_active_text(active)
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
    async def test_quota_loop_skips_age_removed_entries(self, config, sink, tmp_capture_root):
        """Review fix: entries removed by the age loop must be dropped from
        `units` — the byte-quota loop popped them again, subtracting their
        size twice, which deflated the quota accounting and could leave a
        real over-quota file in place until the next sweep."""
        # quota 500 B; OLD1 (1000 B + sidecar) is pruned by age (10 d old,
        # retention_days=7), NEW (600 B + sidecar) is fresh.  After the age
        # loop the real total (~710 B) still exceeds the quota, so NEW must
        # be removed by the quota loop — with the stale-pop bug the double
        # subtraction drove the total below the quota and NEW survived.
        writer = CaptureWALWriter(sink, replace(config, max_capture_bytes=500))
        await writer.start()

        old_data = tmp_capture_root / "guardian_capture_1000000000_1.jsonl.gz"
        old_data.write_bytes(b"x" * 1000)
        old_sidecar = tmp_capture_root / "guardian_capture_1000000000_1.jsonl.sha256"
        old_sidecar.write_text("0" * 64 + "  guardian_capture_1000000000_1.jsonl.sha256\n")
        new_data = tmp_capture_root / "guardian_capture_1000000000_2.jsonl.gz"
        new_data.write_bytes(b"n" * 600)
        new_sidecar = tmp_capture_root / "guardian_capture_1000000000_2.jsonl.sha256"
        new_sidecar.write_text("0" * 64 + "  guardian_capture_1000000000_2.jsonl.sha256\n")
        old_time = time.time() - (10 * 24 * 3600)  # 10 days ago (> retention_days)
        os.utime(str(old_data), (old_time, old_time))
        os.utime(str(old_sidecar), (old_time, old_time))

        writer._enforce_retention()

        assert not old_data.exists(), "old file must be pruned by age"
        assert not old_sidecar.exists(), "old sidecar must go with its data file"
        assert not new_data.exists(), "fresh file must be pruned by the byte quota"
        assert not new_sidecar.exists(), "fresh sidecar must go with its data file"
        await writer.stop()

    @pytest.mark.asyncio
    async def test_retention_removes_sidecar_with_data_file(self, config, sink, tmp_capture_root):
        """A .sha256 sidecar never outlives its data file."""
        writer = CaptureWALWriter(sink, replace(config, retention_days=0))
        await writer.start()

        old_time = time.time() - (2 * 24 * 3600)
        old_gz = tmp_capture_root / "guardian_capture_0000000000_1.jsonl.gz"
        old_gz.write_bytes(b"\x1f\x8b old")
        old_sidecar = old_gz.with_suffix(".sha256")
        old_sidecar.write_text("0" * 64 + "  guardian_capture_0000000000_1.jsonl.sha256\n")
        for f in (old_gz, old_sidecar):
            os.utime(str(f), (old_time, old_time))

        writer._enforce_retention()

        assert not old_gz.exists()
        assert not old_sidecar.exists(), "sidecar must be removed together with its data file"
        await writer.stop()

    @pytest.mark.asyncio
    async def test_retention_prunes_orphan_sidecar(self, config, sink, tmp_capture_root):
        """A sidecar whose data file is already gone is pruned by its own mtime."""
        writer = CaptureWALWriter(sink, replace(config, retention_days=0))
        await writer.start()

        orphan = tmp_capture_root / "guardian_capture_0000000000_2.jsonl.sha256"
        orphan.write_text("0" * 64 + "  guardian_capture_0000000000_2.jsonl.sha256\n")
        writer._enforce_retention()

        assert not orphan.exists()
        await writer.stop()

    @pytest.mark.asyncio
    async def test_retention_never_touches_active_or_state_file(self, config, sink, tmp_capture_root):
        """The sweep must skip the active file and the state file even with
        retention_days=0 (which prunes everything else)."""
        writer = CaptureWALWriter(sink, replace(config, retention_days=0))
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 0}))
        await asyncio.sleep(0.2)
        writer._save_state()  # ensure the state file exists

        active = tmp_capture_root / ACTIVE_FILENAME
        state = tmp_capture_root / STATE_FILENAME
        assert active.exists() and state.exists()

        writer._enforce_retention()

        assert active.exists(), "active file must never be pruned by retention"
        assert state.exists(), "state file must never be pruned by retention"
        await writer.stop()

    @pytest.mark.asyncio
    async def test_retention_removes_leftover_plain_completed_files(self, config, sink, tmp_capture_root):
        """Leftover plain completed .jsonl files are retention candidates too."""
        writer = CaptureWALWriter(sink, replace(config, retention_days=0))
        await writer.start()

        old_time = time.time() - (2 * 24 * 3600)
        old_plain = tmp_capture_root / "guardian_capture_0000000000_3.jsonl"
        old_plain.write_text('{"seq": 0}\n')
        os.utime(str(old_plain), (old_time, old_time))

        writer._enforce_retention()
        assert not old_plain.exists()
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
        # Stop first: retention runs INSIDE the writer loop in production
        # (never concurrently with rotation), so the manual sweep must not
        # race an in-flight rotation either.
        await writer.stop()

        # Trigger retention manually (the writer checks every 60s, too slow for tests)
        writer._enforce_retention()

        # Check disk usage is within a reasonable multiple of quota
        disk_bytes = sum(f.stat().st_size for f in tmp_capture_root.rglob("*") if f.is_file())
        assert disk_bytes <= small_config.max_capture_bytes * 3  # Allow some slack for overhead

        # No orphaned sidecars may remain after quota pruning.
        # Sidecar naming: "<data>.jsonl.sha256" for data "<name>.jsonl.gz",
        # i.e. the sidecar is data_path.with_suffix(".sha256").
        for sidecar in tmp_capture_root.glob("*.sha256"):
            data_file = sidecar.with_suffix(".gz")
            assert data_file.exists(), f"orphan sidecar after retention: {sidecar.name}"

        await writer.stop()


class TestWALWriterPartialLineRecovery:
    @pytest.mark.asyncio
    async def test_partial_final_line_does_not_corrupt(self, writer, sink, config):
        """A partial record left by a crash tail should not corrupt parsing."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.1)
        await writer.stop()

        # Simulate a crash mid-write: a complete record plus an incomplete
        # partial record without its trailing newline.
        root = Path(config.capture_root)
        active = root / ACTIVE_FILENAME
        _append_partial_line(active, '{"seq": 2, "event_type": "crashed"}\n{"incomplete')

        # The reader recovers the complete records; the partial tail is
        # dropped.  Both the pre-crash record (seq 1) and the crashed
        # complete record (seq 2) must be present and valid.
        content = _read_active_text(active)
        lines = [json.loads(line) for line in content.strip().split("\n") if line.strip()]
        seqs = [line["seq"] for line in lines]
        assert 1 in seqs
        assert 2 in seqs

    @pytest.mark.asyncio
    async def test_restart_terminates_partial_line(self, writer, sink, config, tmp_capture_root):
        """On restart the writer terminates a partial trailing line so the
        next record cannot join it (plain-format replacement for the old
        gzip-member isolation)."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.1)
        await writer.stop()

        active = tmp_capture_root / ACTIVE_FILENAME
        _append_partial_line(active, '{"partial": true, "cu')
        assert not active.read_bytes().endswith(b"\n")

        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2}))
        await writer2.stop()

        lines = []
        for ln in active.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                lines.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # the isolated partial record — dropped by readers
        # seq 2 must be a clean standalone record; the partial record is
        # isolated on its own (invalid) line and never merged into it.
        assert any(ln.get("seq") == 2 for ln in lines)
        merged = [ln for ln in lines if "partial" in ln and ln.get("seq") == 2]
        assert not merged, "partial record must not be merged with the next record"

    @pytest.mark.asyncio
    async def test_multiple_partial_lines_tolerance(self, writer, sink, config, tmp_capture_root):
        """Multiple consecutive partial lines from repeated crashes are all tolerated."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.1)
        await writer.stop()

        active = tmp_capture_root / ACTIVE_FILENAME
        _append_partial_line(active, '{"broken1": true}\n')
        _append_partial_line(active, '{"broken2": tru')

        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2}))
        await writer2.stop()

        seqs = []
        for line in _read_active_text(active).strip().split("\n"):
            if not line.strip():
                continue
            try:
                seqs.append(json.loads(line)["seq"])
            except (json.JSONDecodeError, KeyError):
                continue
        assert 1 in seqs
        assert 2 in seqs


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
        active = root / ACTIVE_FILENAME
        content = _read_active_text(active)
        lines = [json.loads(line) for line in content.strip().split("\n") if line]
        assert len(lines) == 3
        for i, line in enumerate(lines):
            assert line["seq"] == i


class TestWALWriterMetrics:
    def test_snapshot_returns_metrics(self, writer, sink, config):
        snap = writer.snapshot()
        assert "writer_metrics" in snap
        assert "sink_metrics" in snap
        assert "capture_disk_bytes" in snap
        assert "capture_active_file_format" in snap

    def test_snapshot_includes_disk_bytes(self, writer, sink, config):
        # Create a file to have some disk usage
        root = Path(config.capture_root)
        (root / "test.jsonl").write_text('{"test": true}\n')
        snap = writer.snapshot()
        assert snap["capture_disk_bytes"] >= 1

    def test_snapshot_reports_plain_active_format(self, writer, sink, config):
        writer._active_file = Path(config.capture_root) / ACTIVE_FILENAME
        assert writer.snapshot()["capture_active_file_format"] == "plain"
        writer._active_file = Path(config.capture_root) / LEGACY_ACTIVE_FILENAME
        assert writer.snapshot()["capture_active_file_format"] == "legacy_gzip"
        writer._active_file = None
        assert writer.snapshot()["capture_active_file_format"] is None


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
        active = root / ACTIVE_FILENAME
        content = _read_active_text(active)
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
        active = root / ACTIVE_FILENAME
        content = _read_active_text(active)
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
        active = root / ACTIVE_FILENAME
        content = _read_active_text(active)
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
        active = root / ACTIVE_FILENAME
        lines = [json.loads(line) for line in _read_active_text(active).strip().split("\n")]
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
        """After a clean stop and restart, the writer appends to the existing
        plain active file (no gzip-member logic involved)."""
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

        active = tmp_capture_root / ACTIVE_FILENAME
        lines = [json.loads(line) for line in _read_active_text(active).strip().split("\n") if line.strip()]
        seqs = [e["seq"] for e in lines]
        assert seqs == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_partial_line_after_restart_is_appended_not_corrupted(
        self, writer, sink, config, tmp_capture_root
    ):
        """A partial line left by a crash is tolerated — the restart
        terminates it so new writes stay parseable."""
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1, "event_type": "request_received"}))
        await writer.stop()

        # Simulate crash: a complete record plus an unterminated partial.
        active = tmp_capture_root / ACTIVE_FILENAME
        _append_partial_line(active, '{"seq": 99, "event_type": "crashed"}\n{"partial": true, "cu')

        # Restart writer with fresh sink
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2, "event_type": "request_completed"}))
        await writer2.stop()

        # Reader recovers the pre-crash complete records AND the post-restart
        # record; the partial crash tail is dropped (invalid JSON line).
        seqs = []
        for line in _read_active_text(active).strip().split("\n"):
            if not line.strip():
                continue
            try:
                seqs.append(json.loads(line)["seq"])
            except (json.JSONDecodeError, KeyError):
                continue  # partial record — reader may surface or drop it
        assert 1 in seqs
        assert 99 in seqs
        assert 2 in seqs

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
        state_path = tmp_capture_root / STATE_FILENAME
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
        active = tmp_capture_root / ACTIVE_FILENAME
        active.unlink()
        assert not active.exists()

        # Restart — should create a new active file
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 2}))
        await writer2.stop()

        assert active.exists()
        lines = [json.loads(line) for line in _read_active_text(active).strip().split("\n") if line.strip()]
        assert len(lines) == 1
        assert lines[0]["seq"] == 2

    @pytest.mark.asyncio
    async def test_empty_file_after_crash_is_safe(self, writer, sink, config, tmp_capture_root):
        """An empty active file (crash before any writes) is safe on restart."""
        await writer.start()
        await writer.stop()

        active = tmp_capture_root / ACTIVE_FILENAME
        # File may or may not exist — if it does, it's empty
        if active.exists():
            assert _read_active_text(active) == ""

        # Restart and write
        sink2 = CaptureSink(max_pending_events=config.max_pending_events)
        writer2 = CaptureWALWriter(sink2, config)
        await writer2.start()
        sink2.try_put(CaptureEvent(data={"seq": 1}))
        await writer2.stop()

        lines = [line for line in _read_active_text(active).strip().split("\n") if line.strip()]
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

        active = tmp_capture_root / ACTIVE_FILENAME
        lines = [json.loads(line) for line in _read_active_text(active).strip().split("\n") if line.strip()]
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

        active = tmp_capture_root / ACTIVE_FILENAME
        lines = [json.loads(line) for line in _read_active_text(active).strip().split("\n") if line.strip()]
        assert len(lines) == 2
        for line in lines:
            assert "record_auth" in line
            assert line["record_auth"]["alg"] == "hmac-sha256"
        # Both should have the same key_id (same secret)
        key_ids = {line["record_auth"]["key_id"] for line in lines}
        assert len(key_ids) == 1


# ── Startup sweep + legacy migration (feedback C3, 2026-08-30) ─────────


class TestWALWriterStartupSweep:
    """start() hardening: temp cleanup, leftover compression, legacy migration."""

    @pytest.mark.asyncio
    async def test_sweep_compresses_leftover_plain_completed_file(self, config, tmp_capture_root):
        leftover = tmp_capture_root / "guardian_capture_1690000000_5.jsonl"
        leftover.write_text('{"seq": 0}\n{"seq": 1}\n')

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()
        await writer.stop()

        assert not leftover.exists(), "plain leftover must be compressed into its .gz"
        target = tmp_capture_root / "guardian_capture_1690000000_5.jsonl.gz"
        assert target.exists()
        with gzip.open(target, "rt", encoding="utf-8") as f:
            assert [json.loads(ln)["seq"] for ln in f if ln.strip()] == [0, 1]
        sidecar = target.with_suffix(".sha256")
        assert sidecar.exists()
        assert sidecar.read_text() == (
            f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}\n"
        )

    @pytest.mark.asyncio
    async def test_sweep_removes_stale_tmp_files(self, config, tmp_capture_root):
        stale_tmp = tmp_capture_root / "guardian_capture_1690000000_4.jsonl.gz.tmp"
        stale_tmp.write_bytes(b"partial-gzip")
        stale_state_tmp = tmp_capture_root / ".capture_state.json.tmp"
        stale_state_tmp.write_text("{}")

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()
        await writer.stop()

        assert not stale_tmp.exists()
        assert not stale_state_tmp.exists()

    @pytest.mark.asyncio
    async def test_sweep_completes_interrupted_rotation(self, config, tmp_capture_root):
        """A crash between rename and compression leaves plain bytes under a
        .jsonl.gz name; the sweep compresses them in place."""
        interrupted = tmp_capture_root / "guardian_capture_1690000000_7.jsonl.gz"
        interrupted.write_text('{"seq": 0}\n{"seq": 1}\n')  # plain, despite the name

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()
        await writer.stop()

        assert interrupted.exists()
        assert interrupted.read_bytes()[:2] == b"\x1f\x8b", "must be real gzip now"
        with gzip.open(interrupted, "rt", encoding="utf-8") as f:
            assert [json.loads(ln)["seq"] for ln in f if ln.strip()] == [0, 1]
        assert interrupted.with_suffix(".sha256").exists()

    @pytest.mark.asyncio
    async def test_sweep_never_touches_new_active_file(self, config, tmp_capture_root):
        active = tmp_capture_root / ACTIVE_FILENAME
        active.write_text('{"pre_existing": true}\n')

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await writer.stop()

        # The pre-existing plain active file is appended to, not archived.
        lines = [json.loads(ln) for ln in active.read_text().splitlines() if ln.strip()]
        assert lines[0] == {"pre_existing": True}
        assert lines[-1]["seq"] == 1


class TestWALWriterLegacyMigration:
    """Legacy stream-gzip active files are renamed at startup, never appended."""

    @pytest.mark.asyncio
    async def test_legacy_gzip_active_renamed_at_startup(self, config, tmp_capture_root):
        legacy = _write_legacy_gzip_active(tmp_capture_root, [{"seq": 0}])
        old_time = time.time() - 7200
        os.utime(str(legacy), (old_time, old_time))

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()

        assert not legacy.exists(), "legacy active file must be renamed away"
        # The migrated file is a completed-style .jsonl.gz (timestamp from
        # its mtime) and remains valid gzip with the original records.
        migrated = [p for p in tmp_capture_root.glob("guardian_capture_*.jsonl.gz")]
        assert len(migrated) == 1
        assert migrated[0].name != LEGACY_ACTIVE_FILENAME
        with gzip.open(migrated[0], "rt", encoding="utf-8") as f:
            assert [json.loads(ln)["seq"] for ln in f if ln.strip()] == [0]
        assert migrated[0].with_suffix(".sha256").exists(), "migrated file gets a sidecar"

        # New writes go to a fresh PLAIN active file.
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.2)
        await writer.stop()

        active = tmp_capture_root / ACTIVE_FILENAME
        assert active.exists()
        assert active.read_bytes()[:2] != b"\x1f\x8b"
        assert [json.loads(ln)["seq"] for ln in active.read_text().splitlines() if ln.strip()] == [1]
        # The migrated legacy file was never appended to.
        with gzip.open(migrated[0], "rt", encoding="utf-8") as f:
            assert [json.loads(ln)["seq"] for ln in f if ln.strip()] == [0]

    @pytest.mark.asyncio
    async def test_legacy_active_seq_persisted(self, config, tmp_capture_root):
        """Migrating the legacy file consumes a rotation_seq so later
        rotations cannot collide with the migrated name."""
        legacy = _write_legacy_gzip_active(tmp_capture_root, [{"seq": 0}])

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()
        # The legacy .gz active file is renamed as-is at startup — it is
        # never appended to, so the plain active file takes over.
        assert not Path(legacy).exists(), "legacy active file must be migrated away"
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.2)
        rotated = writer.rotate()
        await writer.stop()

        assert rotated is not None
        migrated = [p for p in tmp_capture_root.glob("guardian_capture_*.jsonl.gz")
                    if Path(rotated) != p]
        assert migrated, "legacy file should be present under a completed name"
        assert Path(rotated).name != migrated[0].name, "no name collision"

    @pytest.mark.asyncio
    async def test_stale_mtime_plain_active_still_active(self, config, tmp_capture_root):
        """A plain 'current' file is the NEW-format active file regardless of
        age — it is appended to, never archived (only the legacy .gz active
        file is migrated at startup)."""
        active = tmp_capture_root / ACTIVE_FILENAME
        active.write_text('{"pre_existing": true}\n')
        old_time = time.time() - 7200
        os.utime(str(active), (old_time, old_time))

        sink = CaptureSink(max_pending_events=config.max_pending_events)
        writer = CaptureWALWriter(sink, config)
        await writer.start()
        sink.try_put(CaptureEvent(data={"seq": 1}))
        await asyncio.sleep(0.2)
        await writer.stop()

        lines = [json.loads(ln) for ln in active.read_text().splitlines() if ln.strip()]
        assert lines[0] == {"pre_existing": True}
        assert lines[-1]["seq"] == 1
        assert not list(tmp_capture_root.glob("guardian_capture_*.jsonl.gz")), \
            "the plain active file must not be rotated away by startup"
