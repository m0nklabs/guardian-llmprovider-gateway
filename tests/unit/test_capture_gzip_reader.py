"""Unit tests for the crash-tolerant multi-member gzip WAL reader.

The active capture file is gzip with Z_SYNC_FLUSH per record; a crash leaves
it without a trailer and a restart appends a NEW gzip member.  These tests
pin the reader's recovery behavior across those layouts.
"""

import json
import zlib
from pathlib import Path

import pytest

from app.capture.gzip_reader import iter_events, iter_records, read_all_text


def _write_member(fh, text: str, finish: bool) -> None:
    """Write one gzip member; ``finish=False`` simulates a crash (no trailer)."""
    comp = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    fh.write(comp.compress(text.encode("utf-8")))
    fh.write(comp.flush(zlib.Z_SYNC_FLUSH))
    if finish:
        fh.write(comp.flush(zlib.Z_FINISH))


@pytest.fixture
def make_file(tmp_path):
    """Return a helper that writes a multi-member gzip file and returns its path."""
    def _make(members):
        path = tmp_path / "wal.jsonl.gz"
        with open(path, "wb") as fh:
            for text, finish in members:
                _write_member(fh, text, finish)
        return path
    return _make


class TestGzipReaderSingleMember:
    def test_clean_single_member(self, make_file):
        path = make_file([('{"seq": 0}\n{"seq": 1}\n', True)])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 1]

    def test_truncated_member_without_restart(self, make_file):
        path = make_file([('{"seq": 0}\n{"seq": 1}\n', False)])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 1]

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl.gz"
        path.write_bytes(b"")
        assert list(iter_records(path)) == []
        assert read_all_text(path) == ""


class TestGzipReaderMultiMember:
    def test_two_complete_members(self, make_file):
        path = make_file([
            ('{"seq": 0}\n', True),
            ('{"seq": 1}\n', True),
        ])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 1]

    def test_crash_then_restart_recovers_both_members(self, make_file):
        """Complete member + crashed (truncated) member + new complete member."""
        path = make_file([
            ('{"seq": 0}\n', True),
            ('{"seq": 99}\n', False),   # crash after restart
            ('{"seq": 100}\n', True),
        ])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 99, 100]

    def test_multiple_crashes_in_a_row(self, make_file):
        path = make_file([
            ('{"seq": 0}\n', False),
            ('{"seq": 1}\n', False),
            ('{"seq": 2}\n', True),
        ])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 1, 2]

    def test_partial_tail_after_crash_is_dropped(self, make_file):
        """A record truncated mid-write (no trailing newline) is not emitted."""
        path = make_file([
            ('{"seq": 0}\n', True),
            ('{"seq": 99}\n{"incomplete', False),  # partial tail
            ('{"seq": 100}\n', True),
        ])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 99, 100]

    def test_garbage_between_members_is_skipped(self, make_file):
        path = make_file([
            ('{"seq": 0}\n', True),
        ])
        with open(path, "ab") as fh:
            fh.write(b"GARBAGE-NOT-GZIP")
            _write_member(fh, '{"seq": 1}\n', True)
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0, 1]

    def test_bad_json_records_skipped_by_iter_events(self, make_file):
        path = make_file([
            ('NOT JSON\n{"seq": 0}\n', True),
        ])
        recs = list(iter_events(path))
        assert [r["seq"] for r in recs] == [0]

    def test_read_all_text_joins_records(self, make_file):
        path = make_file([
            ('{"seq": 0}\n', True),
            ('{"seq": 1}\n', False),
        ])
        assert read_all_text(path) == '{"seq": 0}\n{"seq": 1}'


class TestGzipReaderRoundTripWithWriter:
    """End-to-end: CaptureWALWriter output (incl. crash simulation) is readable."""

    @pytest.mark.asyncio
    async def test_writer_clean_stop_then_read(self, tmp_path, monkeypatch):
        from app.capture.config import CaptureConfig
        from app.capture.sink import CaptureSink, CaptureEvent
        from app.capture.wal_writer import CaptureWALWriter

        cfg = CaptureConfig(
            enabled=True, local_capture=True, cloud_capture=False,
            instance_id="rt", policy_version="1.0.0",
            capture_root=str(tmp_path), max_file_bytes=1 << 20,
            max_file_age_seconds=3600, retention_days=-1,
            max_pending_events=100, file_mode=0o640, directory_mode=0o750,
        )
        sink = CaptureSink(max_pending_events=100)
        writer = CaptureWALWriter(sink, cfg)
        await writer.start()
        for i in range(10):
            sink.try_put(CaptureEvent(data={"seq": i, "event_type": "test"}))
        await writer.stop()

        active = tmp_path / "guardian_capture_current.jsonl.gz"
        recs = list(iter_events(active))
        assert [r["seq"] for r in recs] == list(range(10))

    @pytest.mark.asyncio
    async def test_writer_rotate_then_read_completed(self, tmp_path):
        from app.capture.config import CaptureConfig
        from app.capture.sink import CaptureSink, CaptureEvent
        from app.capture.wal_writer import CaptureWALWriter

        cfg = CaptureConfig(
            enabled=True, local_capture=True, cloud_capture=False,
            instance_id="rt", policy_version="1.0.0",
            capture_root=str(tmp_path), max_file_bytes=1 << 20,
            max_file_age_seconds=3600, retention_days=-1,
            max_pending_events=100, file_mode=0o640, directory_mode=0o750,
        )
        sink = CaptureSink(max_pending_events=100)
        writer = CaptureWALWriter(sink, cfg)
        await writer.start()
        for i in range(5):
            sink.try_put(CaptureEvent(data={"seq": i, "event_type": "test"}))
        await asyncio_tiny_sleep()
        await asyncio_tiny_sleep()
        await asyncio_tiny_sleep()
        rotated = writer.rotate()
        await writer.stop()
        assert rotated is not None
        recs = list(iter_events(rotated))
        assert [r["seq"] for r in recs] == list(range(5))


async def asyncio_tiny_sleep():
    import asyncio
    await asyncio.sleep(0.15)
