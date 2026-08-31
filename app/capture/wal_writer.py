"""Append-only JSONL writer with rotation, retention, and integrity checksums.

The writer runs as a single background task that consumes from the
:class:`CaptureSink` queue and appends complete JSON lines (plain UTF-8, one
record per line) to the ACTIVE file.  When the file reaches
``max_file_bytes`` or ``max_file_age_seconds``, it is closed, renamed to its
completed name, and gzip-compressed atomically.  Each completed file gets a
SHA-256 checksum stored alongside it so Keanu can validate integrity.

Compression (since 2026-08-30, feedback C3): the ACTIVE file is PLAIN
``.jsonl`` so consumers can stream it line-by-line with standard tools
(``tail -f``, ``jq``, plain ``open()``) while the writer is mid-stream — the
previous stream-gzip active file raised ``EOFError`` for any reader that did
not special-case the missing gzip trailer.  Gzip compression happens ON
ROTATION: the closed plain file is renamed to its ``.jsonl.gz`` completed
name and compressed via a temp file + ``os.replace`` (atomic), so the final
artifact stays a clean single-member gzip for downstream gzip readers.
Legacy active files written by the previous stream-gzip writer are renamed
to a completed-style name at startup and never appended to.

Key invariants:
- One writer only (no concurrent file access).
- All writes are anchored beneath the capture root (no symlink traversal).
- The active file is plain UTF-8 JSONL (readable mid-write); completed
  files are single-member gzip (``.jsonl.gz``) readable by ``gzip`` or the
  crash-tolerant reader (which also reads plain files transparently).
- Rotation and retention are enforced atomically; a ``.sha256`` sidecar is
  never orphaned from its data file.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.capture.config import CaptureConfig
from app.capture.schema import compute_record_auth
from app.capture.sink import CaptureEvent, CaptureSink

logger = logging.getLogger("Guardian.Capture.WAL")

ACTIVE_FILENAME = "guardian_capture_current.jsonl"
LEGACY_ACTIVE_FILENAME = "guardian_capture_current.jsonl.gz"
STATE_FILENAME = ".capture_state.json"
COMPLETED_PATTERN = "guardian_capture_{timestamp}_{seq}.jsonl.gz"

_GZIP_MAGIC = b"\x1f\x8b"


def _is_gzip_file(path: Path) -> bool:
    """True when the file starts with the gzip magic bytes."""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == _GZIP_MAGIC
    except OSError:
        return False


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
        self._task: asyncio.Task | None = None
        self._stopping = False

        # File rotation state
        self._rotation_seq = 0
        self._active_file: Path | None = None
        self._active_fd = None  # raw file descriptor for append-only writes
        self._active_file_size = 0  # plain byte count used for rotation thresholds
        self._active_file_start = 0.0  # monotonic time of file (re)open

        # State file (persisted across restarts)
        self._state_path = Path(config.capture_root) / STATE_FILENAME
        self._state: dict[str, Any] = {}

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

        # Load persisted state, then run the idempotent startup sweep
        # (temp cleanup, leftover compression, legacy active migration)
        # before any event can be written.
        self._load_state()
        try:
            self._sweep_startup()
        except Exception as exc:  # defensive: the sweep must never block startup
            logger.warning("Capture startup sweep failed (continuing): %s", exc)

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

    # ── Startup sweep (migration + crash hardening) ────────────────────

    def _next_completed_path(self, timestamp: int) -> tuple[Path, int]:
        """Return a free completed-file path and its sequence number.

        Bumps the sequence until the name is not already taken (guards
        against collisions after a state reset or a same-second rotation).
        """
        seq = self._state.get("rotation_seq", 0) + 1
        candidate: Path | None = None
        for _ in range(1000):
            path = self._capture_root / COMPLETED_PATTERN.format(timestamp=timestamp, seq=seq)
            if not path.exists():
                candidate = path
                break
            seq += 1
        if candidate is None:  # pragma: no cover — 1000 collisions is pathological
            candidate = self._capture_root / COMPLETED_PATTERN.format(
                timestamp=timestamp, seq=f"{seq}-{os.getpid()}")
        return candidate, seq

    def _compress_atomically(self, src_path: Path, dst_path: Path) -> None:
        """Gzip-compress ``src_path`` into ``dst_path`` atomically.

        The gzip data is written to ``<dst_path>.tmp`` in the same directory
        and then ``os.replace``d over ``dst_path``.  The source file is NOT
        removed; callers decide whether to unlink it.  Raises OSError on
        failure (after cleaning up the temp file).
        """
        tmp_path = dst_path.with_name(dst_path.name + ".tmp")
        try:
            with open(src_path, "rb") as src, open(tmp_path, "wb") as dst:
                # filename="" keeps the gzip header free of the temp name;
                # mtime=0 keeps the output deterministic.
                with gzip.GzipFile(filename="", mode="wb", fileobj=dst,
                                   compresslevel=6, mtime=0) as gz:
                    shutil.copyfileobj(src, gz, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(str(tmp_path), str(dst_path))
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _write_sidecar(self, data_path: Path) -> str:
        """Write the ``.sha256`` sidecar for ``data_path`` and return the hash."""
        checksum = self._compute_file_checksum(data_path)
        checksum_path = data_path.with_suffix(".sha256")
        with open(checksum_path, "w") as f:
            f.write(f"{checksum}  {data_path.name}\n")
        os.chmod(str(checksum_path), self._config.file_mode)
        return checksum

    def _sweep_startup(self) -> None:
        """Idempotent startup hardening (migration + crash recovery).

        (a) Delete stale ``*.tmp`` leftovers in the capture root (a previous
            crash during an atomic write/compress can leave them).
        (b) Compress leftover plain completed files: a
            ``guardian_capture_{ts}_{seq}.jsonl`` without its ``.gz``, or a
            crash mid-rotation that left plain bytes under a ``.jsonl.gz``
            name (renamed but not yet compressed).
        (c) Migrate a LEGACY active file ``guardian_capture_current.jsonl.gz``
            (written by the previous stream-gzip writer): rename it as-is to
            a completed-style name (timestamp from its mtime, next
            rotation_seq from state).  It is no longer the active file and
            is never appended to.

        Every step is best-effort: failures are logged and counted, never
        raised (fail-open).
        """
        root = self._capture_root
        if not root.is_dir():
            return

        # (a) stale temp files
        try:
            for entry in root.glob("*.tmp"):
                try:
                    entry.unlink()
                    logger.info("Startup sweep: removed stale temp file %s", entry.name)
                except OSError as exc:
                    logger.warning("Startup sweep: could not remove temp file %s: %s", entry.name, exc)
        except OSError as exc:
            logger.warning("Startup sweep: failed to list capture root for temp cleanup: %s", exc)

        # (b) leftover plain completed files
        try:
            leftovers = sorted(root.glob("guardian_capture_*.jsonl*"))
        except OSError as exc:
            logger.warning("Startup sweep: failed to list capture root: %s", exc)
            leftovers = []
        for entry in leftovers:
            name = entry.name
            if name in (ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME):
                continue
            if name.endswith(".sha256") or name.endswith(".tmp"):
                continue
            if not entry.is_file():
                continue
            if _is_gzip_file(entry):
                continue  # already a valid gzip artifact
            try:
                if name.endswith(".gz"):
                    # Plain bytes under a .gz name: rotation crashed between
                    # rename and compression — compress in place.
                    target = entry
                    source = None
                else:
                    # Plain completed file without its .gz.
                    target = entry.with_suffix(".jsonl.gz")
                    source = entry
                self._compress_atomically(entry, target)
                if source is not None:
                    source.unlink(missing_ok=True)
                self._write_sidecar(target)
                os.chmod(str(target), self._config.file_mode)
                self._metrics.files_rotated += 1
                logger.info("Startup sweep: compressed leftover plain capture file -> %s", target.name)
            except OSError as exc:
                logger.warning("Startup sweep: could not compress leftover %s: %s", entry.name, exc)

        # (c) legacy gzip active file — rename as-is, never append
        legacy = root / LEGACY_ACTIVE_FILENAME
        if legacy.is_file():
            try:
                if _is_gzip_file(legacy):
                    mtime = int(legacy.stat().st_mtime)
                    target, seq = self._next_completed_path(mtime)
                    self._rotation_seq = seq
                    self._state["rotation_seq"] = seq
                    os.replace(str(legacy), str(target))
                    self._write_sidecar(target)
                    os.chmod(str(target), self._config.file_mode)
                    self._metrics.files_rotated += 1
                    self._save_state()
                    logger.info(
                        "Startup sweep: renamed legacy gzip active file -> %s (never appended to)",
                        target.name)
                else:
                    # Even older plain active leftover: archive it as gzip.
                    mtime = int(legacy.stat().st_mtime)
                    target, seq = self._next_completed_path(mtime)
                    self._rotation_seq = seq
                    self._state["rotation_seq"] = seq
                    self._compress_atomically(legacy, target)
                    legacy.unlink(missing_ok=True)
                    self._write_sidecar(target)
                    os.chmod(str(target), self._config.file_mode)
                    self._metrics.files_rotated += 1
                    self._save_state()
                    logger.info(
                        "Startup sweep: compressed legacy plain active file -> %s (never appended to)",
                        target.name)
            except OSError as exc:
                logger.warning("Startup sweep: could not migrate legacy active file: %s", exc)

    # ── File management ────────────────────────────────────────────────

    def _terminate_partial_line(self, active_path: Path) -> None:
        """Ensure an existing active file ends with a newline before appending.

        A crash mid-write can leave a partial record without its trailing
        newline.  Appending after it would join the partial record and the
        next record into one corrupt line.  Terminating the partial line
        isolates it: the reader drops the incomplete record and every
        subsequent record stays parseable.  (This replaces the gzip-member
        isolation of the previous stream-gzip format.)
        """
        try:
            size = active_path.stat().st_size
            if size == 0:
                return
            with open(active_path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                last = fh.read(1)
            if last != b"\n":
                with open(active_path, "ab") as fh:
                    fh.write(b"\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                logger.warning("Terminated partial trailing record in %s (crash recovery)",
                               active_path.name)
        except OSError:
            pass  # fail-open: worst case one appended record joins a partial line

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
            # Isolate any partial trailing record left by a crash BEFORE
            # appending (plain text has no gzip-member boundaries to rely on).
            if active_path.exists():
                self._terminate_partial_line(active_path)
            # Open with O_APPEND for atomic appends; create if needed.
            # Binary mode: records are plain UTF-8 JSON lines.
            fd = os.open(
                str(active_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                self._config.file_mode,
            )
            os.chmod(str(active_path), self._config.file_mode)
            self._active_fd = os.fdopen(fd, "ab")
            self._active_file = active_path
            # Rotation thresholds count the plain file's byte size directly;
            # seed from the on-disk size so a restarted writer rotates on the
            # real file size, not just this session's appended bytes.
            try:
                self._active_file_size = active_path.stat().st_size
            except OSError:
                self._active_file_size = 0
            self._active_file_start = time.monotonic()
            logger.debug("Opened active capture file: %s (size=%d)", active_path, self._active_file_size)
        except OSError as exc:
            logger.error("Failed to open active capture file %s: %s", active_path, exc)
            self._metrics.write_failures += 1
            self._active_fd = None
            self._active_file = None

    def _close_active_file(self) -> None:
        """Close the active file without rotating."""
        if self._active_fd is not None:
            try:
                self._active_fd.close()
            except Exception:
                pass
            self._active_fd = None
        self._active_file = None
        # Keep the size/start consistent: a closed file has no pending bytes.
        # (Bug: after an automatic rotation the stale size made rotate()
        # refuse to rotate the already-rotated data and report None.)
        self._active_file_size = 0
        self._active_file_start = 0.0

    def rotate(self) -> str | None:
        """Force rotation of the active file.

        Returns the path of the rotated (.gz) file, or None if there was
        nothing to rotate.
        """
        if self._active_file is None or self._active_file_size == 0:
            return None
        rotated = self._rotate_file()
        # Re-open a new active file for subsequent writes
        self._open_active_file()
        return rotated

    def _needs_rotation(self) -> bool:
        """Check if the active file needs rotation (size or age limit)."""
        if self._active_file is None or self._active_file_size == 0:
            return False
        if self._active_file_size >= self._config.max_file_bytes:
            return True
        if time.monotonic() - self._active_file_start >= self._config.max_file_age_seconds:
            return True
        return False

    def _recover_failed_rotation(self, active_path: Path, completed_path: Path) -> None:
        """Best-effort recovery after a failed rotation (fail-open, no data loss).

        - remove any leftover temp file;
        - if the data was renamed but not yet compressed (still plain under
          the ``.gz`` name), move it back to the active slot so the next
          rotation attempt can retry;
        - if compression already succeeded (gzip bytes present), keep the
          completed file and regenerate its sidecar best-effort.
        """
        tmp_path = completed_path.with_name(completed_path.name + ".tmp")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

        if not completed_path.exists():
            return
        if not _is_gzip_file(completed_path):
            if not active_path.exists():
                try:
                    os.replace(str(completed_path), str(active_path))
                    logger.warning("Rotation failed — data restored to %s for retry", active_path.name)
                except OSError:
                    logger.error("Rotation failed and data could not be restored to %s", active_path.name)
            return

        # Compression succeeded; only the sidecar/chmod stage failed.
        try:
            self._write_sidecar(completed_path)
            os.chmod(str(completed_path), self._config.file_mode)
            self._metrics.files_rotated += 1
            self._metrics.files_written += 1
            self._save_state()
            logger.warning("Rotation failed mid-flight — completed file kept: %s", completed_path.name)
        except OSError as exc:
            logger.error("Rotation failed and sidecar regeneration failed for %s: %s",
                         completed_path.name, exc)

    def _rotate_file(self) -> str | None:
        """Close the active plain file, rename it to its completed name, and
        gzip-compress it atomically.

        Returns the path of the rotated (gzipped) file, or ``None`` if there
        was nothing to rotate.
        """
        if self._active_file is None:
            return None

        # Save the path before closing — _close_active_file sets _active_file to None
        active_path = self._active_file
        self._close_active_file()

        timestamp = int(time.time())
        completed_path, seq = self._next_completed_path(timestamp)
        self._rotation_seq = seq
        self._state["rotation_seq"] = seq

        try:
            # 1. Rename the closed plain active file to its completed name
            #    (atomic on the same filesystem).  At this instant the file
            #    under the .gz name is still plain; step 2 replaces it with
            #    gzip bytes before anything else consumes it.
            os.replace(str(active_path), str(completed_path))

            # 2. Gzip-compress atomically: compress to a temp file in the
            #    same directory, then os.replace over the final .gz name.
            self._compress_atomically(completed_path, completed_path)

            # 3. Checksum over the final .gz bytes + sidecar.
            checksum = self._write_sidecar(completed_path)

            os.chmod(str(completed_path), self._config.file_mode)

            self._metrics.files_rotated += 1
            self._metrics.files_written += 1

            # Persist state after successful rotation
            self._save_state()

            logger.info("Rotated capture file -> %s (checksum=%s)", completed_path.name, checksum[:16])
            return str(completed_path)

        except OSError as exc:
            logger.error("Failed to rotate capture file: %s", exc)
            self._metrics.write_failures += 1
            self._recover_failed_rotation(active_path, completed_path)
            return None

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
        ``retention_days < 0`` disables retention entirely.  A ``.sha256``
        sidecar is always removed together with its data file (never
        orphaned); a sidecar whose data file is already gone is pruned on
        its own mtime.  The active file and the state file are never touched.
        """
        if self._config.retention_days < 0:
            return

        root = self.get_write_path()
        cutoff = time.time() - (self._config.retention_days * 86400)
        cut_bytes = self._config.max_capture_bytes

        try:
            # Collect removable units: (paths_to_unlink, mtime, total_size).
            # A data file carries its sidecar (removed together).
            units: list[tuple[list[Path], float, int]] = []
            seen_sidecars: set = set()
            total_size = 0
            for entry in root.iterdir():
                name = entry.name
                if name in (ACTIVE_FILENAME, LEGACY_ACTIVE_FILENAME, STATE_FILENAME):
                    # The legacy active file matches the completed-file
                    # pattern below; if the startup migration failed
                    # (fail-open), it still holds un-rotated records and
                    # must never be swept.
                    continue
                if not entry.is_file():
                    continue
                if not name.startswith("guardian_capture_"):
                    continue
                if name.endswith(".tmp") or name.endswith(".sha256"):
                    continue
                stat = entry.stat()
                sidecar = entry.with_suffix(".sha256")
                paths = [entry]
                size = stat.st_size
                if sidecar.is_file():
                    paths.append(sidecar)
                    size += sidecar.stat().st_size
                    seen_sidecars.add(sidecar.name)
                units.append((paths, stat.st_mtime, size))
                total_size += size

            # Orphan sidecars (data file already gone) — prune by their own mtime.
            for entry in root.iterdir():
                name = entry.name
                if (name.startswith("guardian_capture_") and name.endswith(".sha256")
                        and name not in seen_sidecars and entry.is_file()):
                    stat = entry.stat()
                    units.append(([entry], stat.st_mtime, stat.st_size))
                    total_size += stat.st_size

            # Sort by modification time (oldest first)
            units.sort(key=lambda u: u[1])

            def _remove(paths: list[Path]) -> None:
                for path in paths:
                    try:
                        path.unlink()
                        self._metrics.files_retired += 1
                        logger.debug("Retention: removed %s", path.name)
                    except OSError:
                        pass

            # Remove old files first (data + sidecar together).  Entries
            # removed here are dropped from `units` so the byte-quota loop
            # below cannot pop stale entries and subtract their size twice
            # (which deflated the quota accounting and could leave real
            # files over the quota until the next sweep — review finding).
            survivors: list[tuple[list[Path], float, int]] = []
            for unit in units:
                paths, mtime, size = unit
                if mtime < cutoff:
                    _remove(paths)
                    total_size -= size
                else:
                    survivors.append(unit)
            units = survivors

            # If still over the byte limit, remove oldest until under quota.
            # max_capture_bytes < 0 = unlimited budget (matches infinite
            # retention, operator decision 2026-08-26).
            if cut_bytes >= 0:
                while total_size > cut_bytes and units:
                    paths, mtime, size = units.pop(0)
                    _remove(paths)
                    total_size -= size

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
            event_dict = dict(event.data)  # shallow copy
            line_no_auth = json.dumps(event_dict, separators=(",", ":"), sort_keys=False, default=str)
            record_auth = compute_record_auth(line_no_auth)
            if record_auth is not None:
                event_dict["record_auth"] = record_auth
                line = json.dumps(event_dict, separators=(",", ":"), sort_keys=False, default=str)
            else:
                line = line_no_auth
            line_bytes = (line + "\n").encode("utf-8")

            # Plain UTF-8 append: one complete JSON record per line.  The
            # active file stays readable line-by-line (tail -f, jq, plain
            # open) while the writer is mid-stream (feedback C3).
            self._active_fd.write(line_bytes)
            self._active_fd.flush()
            os.fsync(self._active_fd.fileno())
            # Rotation thresholds count the plain file's bytes directly.
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
        consecutive_errors = 0

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
                consecutive_errors = 0

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
                # Fail-open: log and continue — but never busy-spin on a
                # persistent error (e.g. a queue bound to a stale event loop).
                consecutive_errors += 1
                self._metrics.write_failures += 1
                logger.warning(
                    "Capture writer unexpected error (%d consecutive, continuing): %s",
                    consecutive_errors, exc,
                )
                if consecutive_errors >= 50:
                    logger.error(
                        "Capture writer stopping after %d consecutive errors (fail-open)",
                        consecutive_errors,
                    )
                    break
                await asyncio.sleep(0.5)

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

    def snapshot(self) -> dict[str, Any]:
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

        active_format = None
        if self._active_file is not None:
            active_format = "legacy_gzip" if self._active_file.suffix == ".gz" else "plain"

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
            "capture_active_file_format": active_format,
            "capture_queue_depth": self._sink.queue_depth,
        }
