"""Crash-tolerant multi-member gzip reader for the capture WAL.

Guardian writes the ACTIVE WAL as a gzip stream and flushes every record with
``Z_SYNC_FLUSH``, so each complete record forms an independently decodable
deflate block.  A crash can leave the stream without its final trailer (and
possibly a partial record at the tail); on restart the writer appends a NEW
gzip member to the same file.  Naive ``gzip.open().read()`` cannot read such
files — it chokes on the truncated tail or on the next member's header.

This module reads them robustly:

- decodes each member incrementally and yields every COMPLETE record;
- never feeds a member's decompressor past a candidate next-member gzip
  header, so decoding of a truncated member cannot swallow the next member;
- a boundary is confirmed by probing a *copy* of the decompressor with the
  next header's first byte (an invalid deflate block type at a ``Z_SYNC_FLUSH``
  boundary) — the real decoder is untouched;
- stops cleanly at a truncated tail (crash) — the partial record is dropped;
- never raises on malformed input (fail-open, like the rest of capture).

Used by ``scripts/guardianctl.py export`` and ``scripts/keanu_redact.py``;
completed/rotated files are clean single-member gzip and read equally well.
"""

from __future__ import annotations

import copy
import zlib
from pathlib import Path
from typing import Iterator, Union

_CHUNK = 65536
_GZIP_MAGIC = b"\x1f\x8b"
PathLike = Union[str, Path]


def _probe_boundary(decomp: zlib._Decompress) -> bool:
    """True when ``decomp`` sits at a Z_SYNC_FLUSH boundary (a gzip header
    follows), determined without mutating ``decomp``."""
    probe = copy.copy(decomp)
    try:
        probe.decompress(b"\x1f")
    except zlib.error:
        return True
    return False


def iter_records(path: PathLike) -> Iterator[bytes]:
    """Yield decompressed records (bytes, trailing newline stripped).

    A record is any text between two newlines in the decompressed stream.
    Complete records written before a crash are recovered; a truncated tail
    or a corrupt member is skipped without raising.
    """
    with open(path, "rb") as fh:
        pending = b""
        line_buf = b""
        decomp = None

        while True:
            # ---- (re)start a member at the next gzip header ----
            if decomp is None:
                while len(pending) < 2:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        return  # clean EOF
                    pending += chunk
                idx = pending.find(_GZIP_MAGIC)
                if idx == -1:
                    pending = b""
                    continue
                if idx > 0:
                    pending = pending[idx:]
                while len(pending) < 10:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        return
                    pending += chunk
                try:
                    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    decomp.decompress(pending[:10])  # gzip header
                except zlib.error:
                    decomp = None
                    pending = pending[2:]  # bad header — skip and rescan
                    continue
                pending = pending[10:]
                continue

            # ---- feed, never past a candidate next-member header ----
            if not pending:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    return  # truncated member simply ends here
                pending += chunk

            if pending.startswith(_GZIP_MAGIC) and _probe_boundary(decomp):
                # Current member ended (crash or clean finish); next header
                # starts here.  All complete records were already yielded;
                # drop any unterminated partial record (crash tail).
                line_buf = b""
                decomp = None
                continue

            feed = pending
            idx = pending.find(_GZIP_MAGIC, 2)
            if idx > 0:
                feed = pending[:idx]  # never feed past a candidate header

            try:
                out = decomp.decompress(feed)
            except zlib.error:
                # Corrupt/truncated stream — resync at the next header.
                idx = pending.find(_GZIP_MAGIC, 2)
                pending = pending[idx:] if idx > 0 else b""
                decomp = None
                continue
            consumed = len(feed) - len(decomp.unconsumed_tail)
            pending = pending[consumed:]

            parts = out.split(b"\n")
            line_buf += parts[0]
            for part in parts[1:]:
                yield line_buf
                line_buf = part

            if decomp.eof:
                # The gzip decompressobj sets eof=True only after it has seen
                # and consumed the member's 8-byte trailer (which is always in
                # ``feed`` — the feed is cut at the next candidate header,
                # after the trailer).  Nothing more to consume here.
                decomp = None


def iter_events(path: PathLike) -> Iterator[dict]:
    """Yield parsed JSON events; unparseable records are skipped."""
    import json

    for record in iter_records(path):
        if not record.strip():
            continue
        try:
            yield json.loads(record.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue


def read_all_text(path: PathLike) -> str:
    """Concatenate all recovered records with newlines (for tests/tools)."""
    return b"\n".join(iter_records(path)).decode("utf-8", errors="replace")
