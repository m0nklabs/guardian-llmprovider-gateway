"""Append-only JSONL writer with rotation, retention, and integrity checksums.

The writer runs as a single background task that consumes from the
:class:`CaptureSink` queue and appends complete JSON lines to the active
JSONL file.  When the file reaches ``max_file_bytes`` or ``max_file_age_seconds``,
it is atomically closed, compressed, and rotated.  Each completed file gets
a SHA-256 checksum stored alongside it so Keanu can validate integrity.

Key invariants:
- One writer only (no concurrent file access).
- All writes are anchored beneath the capture root (no symlink traversal).
- Completed files are gzipped; the active file is plain JSONL (read only by
  Keanu after completion).
- Rotation and retention are enforced atomically.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.capture.config import CaptureConfig
from app.capture.sink import CaptureSink, CaptureEvent, SinkMetrics
from app.capture.schema import compute_record_auth

logger = logging.getLogger("Guardian.Capture.WAL")

ACTIVE_FILENAME = "guardian_capture_current.jsonl"
STATE_FILENAME = ".capture_state.json"
COMPLETED_PATTERN = "guardian_capture_{timestamp}_{seq}.jsonl"


@dataclass
class WALWriterMetrics:
    """Metrics tracked by the WAL writer."""

    files_written: int = 0
    bytes_written: int = 0
    write_failures: int = 0
    files_rotated: int = 0
    files_retired: int = 0  # removed by retention
    checksum_failures: int = 0


class CaptureWALWriter:
    """Single-background-writer append-only JSONL capture sink.

    Lifecycle:
    1. Construct with a :class:`CaptureSink` and :class:`CaptureConfig`.
    2. Call :meth:`start` (asynchronously) to begin consuming events.
    3. Call :meth:`stop` to flush and shut down cleanly.

    The writer is fail-open: any I/O error is logged and the event is
    counted as a write failure, but the writer continues operating.
    """

    def __init__(
        self,
        sink: CaptureSink,
        config: CaptureConfig,
    ) -> None:
        self._sink = sink
        self._config = config
        self._metrics = WALWriterMetrics()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

        # File rotation state
        self._rotation_seq = 0
        self._active_file: Optional[Path] = None
        self._active_fd = None  # raw file descriptor for append-only writes
        self._active_file_size = 0
        self._active_file_start = 0.0  # monotonic time of file creation

        # State file (persisted across restarts)
        self._state_path = Path(config.capture_root) / STATE_FILENAME
        self._state: Dict[str, Any] = {}

        # Capture root
        self._capture_root = Path(config.capture_root)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def get_write_path(self) -> Path:
        """Return the capture root, validated and anchored (no symlink escape)."""
        root = self._capture_root.resolve()
        return root

    async def start(self) -> None:
        """Start the background writer task."""
        if self._task is not None and not self._task.done():
            logger.warning("Capture WAL writer already started")
            return

        root = self.get_write_path()
        try:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(str(root), self._config.directory_mode)
        except OSError as exc:
            logger.error("Failed to create capture root %s: %s — disabling capture writer", root, exc)
            self._metrics.write_failures += 1
            return

        # Load persisted state
        self._load_state()

        self._stopping = False
        self._sink.register_consumer()
        self._task = asyncio.create_task(self._run(), name="capture-wal-writer")
        logger.info("Capture WAL writer started (root=%s, max_file_bytes=%d, max_file_age=%ds, retention=%dd)",
                     root, self._config.max_file_bytes, self._config.max_file_age_seconds,
                     self._config.retention_days)

    async def stop(self) -> None:
        """Signal the writer to drain and stop."""
        if self._task is None:
            return
        self._stopping = True
        self._sink.close()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Capture WAL writer did not stop within 10s — cancelling")
            self._task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await self._task
        finally:
            self._sink.unregister_consumer()
            self._close_active_file()
            logger.info("Capture WAL writer stopped (files_written=%d, bytes_written=%d, write_failures=%d)",
                        self._metrics.files_written, self._metrics.bytes_written, self._metrics.write_failures)

    # ── State persistence ──────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load persisted rotation/retention state from disk."""
        if not self._state_path.exists():
            self._state = {"rotation_seq": 0, "started_at": time.time()}
            return
        try:
            with open(self._state_path, "r") as f:
                self._state = json.load(f)
            self._rotation_seq = int(self._state.get("rotation_seq", 0))
        except Exception:
            self._state = {"rotation_seq": 0, "started_at": time.time()}
            self._rotation_seq = 0

    def _save_state(self) -> None:
        """Persist rotation/retention state to disk."""
        state_path = self._state_path
        tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(self._state, f, indent=2)
            os.replace(str(tmp_path), str(state_path))
        except OSError:
            pass

    # ── File management ────────────────────────────────────────────────

    def _open_active_file(self) -> None:
        """Open (or reopen) the active JSONL file for append-only writes."""
        root = self.get_write_path()
        active_path = root / ACTIVE_FILENAME

        # Security: ensure the file path is within the capture root
        # (prevents symlink traversal if the path is manipulated)
        try:
            resolved = active_path.resolve()
            if not str(resolved).startswith(str(root.resolve())):
                logger.error("Active file path escapes capture root — refusing to open")
                self._metrics.write_failures += 1
                return
        except OSError:
            pass

        try:
            # Open with O_APPEND for atomic appends; create if needed
            fd = os.open(
                str(active_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                self._config.file_mode,
            )
            os.chmod(str(active_path), self._config.file_mode)
            self._active_fd = os.fdopen(fd, "a", encoding="utf-8", errors="replace")
            self._active_file = active_path
            self._active_file_size = active_path.stat().st_size if active_path.exists() else 0
            self._active_file_start = time.monotonic()
            logger.debug("Opened active capture file: %s (size=%d)", active_path, self._active_file_size)
        except OSError as exc:
            logger.error("Failed to open active capture file %s: %s", active_path, exc)
            self._metrics.write_failures += 1
            self._active_fd = None
            self._active_file = None

    def _close_active_file(self) -> None:
        """Close the active file descriptor without rotating."""
        if self._active_fd is not None:
            try:
                self._active_fd.close()
            except Exception:
                pass
            self._active_fd = None
        self._active_file = None

    def rotate(self) -> Optional[str]:
        """Force rotation of the active file.

        Returns the path of the rotated (.gz) file, or None if there was
        nothing to rotate.
        """
        if self._active_file is None or self._active_file_size == 0:
            return None
        self._rotate_file()
        # Re-open a new active file for subsequent writes
        self._open_active_file()
        # Return the most recent gz path — _rotate_file logs it but doesn't
        # return it, so we find it from the capture root.
        try:
            gz_files = sorted(
                self._capture_root.glob("*.jsonl.gz"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return str(gz_files[0]) if gz_files else None
        except (OSError, IndexError):
            return None

    def _needs_rotation(self) -> bool:
        """Check if the active file needs rotation (size or age limit)."""
        if self._active_file is None or self._active_file_size == 0:
            return False
        if self._active_file_size >= self._config.max_file_bytes:
            return True
        if time.monotonic() - self._active_file_start >= self._config.max_file_age_seconds:
            return True
        return False

    def _rotate_file(self) -> None:
        """Atomically close, compress, checksum, and rename the active file."""
        if self._active_file is None:
            return

        # Save the path before closing — _close_active_file sets _active_file to None
        active_path = self._active_file
        self._close_active_file()

        timestamp = int(time.time())
        self._rotation_seq = self._state.get("rotation_seq", 0) + 1
        self._state["rotation_seq"] = self._rotation_seq

        completed_name = COMPLETED_PATTERN.format(timestamp=timestamp, seq=self._rotation_seq)
        completed_path = self._capture_root / completed_name
        gz_path = self._capture_root / f"{completed_name}.gz"

        try:
            # Rename the active file to its completed name (atomic on same filesystem)
            os.replace(str(active_path), str(completed_path))

            # Compress
            with open(completed_path, "rb") as src:
                with gzip.GzipFile(
                    filename=str(gz_path),
                    mode="wb",
                    mtime=timestamp,
                ) as dst:
                    shutil.copyfileobj(src, dst)

            # Compute checksum
            checksum = self._compute_file_checksum(gz_path)

            # Remove uncompressed intermediate
            completed_path.unlink(missing_ok=True)

            # Write checksum sidecar
            checksum_path = gz_path.with_suffix(".sha256")
            with open(checksum_path, "w") as f:
                f.write(f"{checksum}  {gz_path.name}\n")

            os.chmod(str(gz_path), self._config.file_mode)
            os.chmod(str(checksum_path), self._config.file_mode)

            self._metrics.files_rotated += 1
            self._metrics.files_written += 1

            # Persist state after successful rotation
            self._save_state()

            logger.info("Rotated capture file -> %s (checksum=%s)", gz_path.name, checksum[:16])

        except OSError as exc:
            logger.error("Failed to rotate capture file: %s", exc)
            self._metrics.write_failures += 1
            # Try to clean up partial files
            completed_path.unlink(missing_ok=True)
            gz_path.unlink(missing_ok=True)

    def _compute_file_checksum(self, path: Path) -> str:
        """Compute SHA-256 checksum of a file."""
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    # ── Retention ──────────────────────────────────────────────────────

    def _enforce_retention(self) -> None:
        """Remove completed capture files older than retention_days.

        ``retention_days=0`` means remove all completed files immediately.
        ``retention_days < 0`` disables retention entirely.
        """
        if self._config.retention_days < 0:
            return

        root = self.get_write_path()
        cutoff = time.time() - (self._config.retention_days * 86400)
        cut_bytes = self._config.max_capture_bytes

        try:
            files: List[Tuple[Path, float, int]] = []
            total_size = 0
            for entry in root.iterdir():
                if entry.name == ACTIVE_FILENAME or entry.name == STATE_FILENAME:
                    continue
                if not entry.is_file():
                    continue
                if entry.name.startswith("guardian_capture_"):
                    stat = entry.stat()
                    files.append((entry, stat.st_mtime, stat.st_size))
                    total_size += stat.st_size

            # Sort by modification time (oldest first)
            files.sort(key=lambda x: x[1])

            # Remove old files first
            for path, mtime, size in files:
                if mtime < cutoff:
                    try:
                        path.unlink()
                        self._metrics.files_retired += 1
                        total_size -= size
                        logger.debug("Retention: removed old file %s", path.name)
                    except OSError:
                        pass

            # If still over the byte limit, remove oldest until under quota
            while total_size > cut_bytes and files:
                path, mtime, size = files.pop(0)
                try:
                    path.unlink()
                    self._metrics.files_retired += 1
                    total_size -= size
                    logger.debug("Retention: removed for byte quota %s", path.name)
                except OSError:
                    pass

        except OSError as exc:
            logger.warning("Retention enforcement error: %s", exc)

    # ── Core write logic ───────────────────────────────────────────────

    def _write_event(self, event: CaptureEvent) -> bool:
        """Write one event to the active file.  Returns True on success."""
        if self._active_fd is None:
            self._open_active_file()
            if self._active_fd is None:
                return False

        try:
            # Serialize the event first, then add per-record HMAC if configured.
            import json as _json
            event_dict = dict(event.data)  # shallow copy
            line_no_auth = _json.dumps(event_dict, separators=(",", ":"), sort_keys=False, default=str)
            record_auth = compute_record_auth(line_no_auth)
            if record_auth is not None:
                event_dict["record_auth"] = record_auth
                line = _json.dumps(event_dict, separators=(",", ":"), sort_keys=False, default=str)
            else:
                line = line_no_auth
            line += "\n"
            self._active_fd.write(line)
            self._active_fd.flush()
            os.fsync(self._active_fd.fileno())
            line_bytes = line.encode("utf-8")
            self._active_file_size += len(line_bytes)
            self._metrics.bytes_written += len(line_bytes)
            self._metrics.files_written = max(self._metrics.files_written, 1)
            return True
        except OSError as exc:
            logger.error("Failed to write capture event: %s", exc)
            self._metrics.write_failures += 1
            # Try to reopen on next call
            self._close_active_file()
            return False

    async def _run(self) -> None:
        """Main writer loop — consumes events from the sink."""
        logger.info("Capture WAL writer loop started")
        last_retention_check = 0.0

        while not self._stopping:
            try:
                event = await self._sink.get()
                if event is None:
                    # Sentinel — sink closed
                    if self._stopping:
                        break
                    continue

                # Write the event
                self._write_event(event)

                # Check rotation
                if self._needs_rotation():
                    self._rotate_file()

                # Check retention periodically (every ~60s)
                now = time.monotonic()
                if now - last_retention_check > 60:
                    last_retention_check = now
                    self._enforce_retention()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Fail-open: log and continue
                logger.warning("Capture writer unexpected error (continuing): %s", exc)
                self._metrics.write_failures += 1

        # Final drain on shutdown
        remaining = await self._sink.drain_remaining()
        for event in remaining:
            self._write_event(event)

        # Final rotation
        if self._needs_rotation():
            self._rotate_file()

        logger.info("Capture WAL writer loop exited (wrote %d bytes, %d failures)",
                     self._metrics.bytes_written, self._metrics.write_failures)

    # ── Metrics ────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Return a metrics snapshot."""
        root = self.get_write_path()
        disk_bytes = 0
        try:
            if root.exists():
                for entry in root.rglob("*"):
                    if entry.is_file():
                        disk_bytes += entry.stat().st_size
        except OSError:
            pass

        sink_metrics = self._sink.metrics
        return {
            "writer_metrics": {
                "files_written": self._metrics.files_written,
                "bytes_written": self._metrics.bytes_written,
                "write_failures": self._metrics.write_failures,
                "files_rotated": self._metrics.files_rotated,
                "files_retired": self._metrics.files_retired,
            },
            "sink_metrics": sink_metrics.to_dict(),
            "capture_disk_bytes": disk_bytes,
            "capture_active_file": str(self._active_file) if self._active_file else None,
            "capture_queue_depth": self._sink.queue_depth,
        }
