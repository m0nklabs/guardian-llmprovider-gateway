"""Media extraction for raw capture — images stored out-of-band with WAL refs.

Raw capture (since 2026-08-26) stores *everything* Guardian sees, but binary
image payloads must not bloat the JSONL WAL: a single base64 image can be
larger than a rotation window.  Instead, image content blocks are extracted
from request messages, written as individual files beneath the capture root
(``media/``), and replaced in the event with a reference block:

.. code-block:: json

    {"type": "image_media", "image_media": {
        "path": "media/<request_id>_<idx>.<ext>",
        "sha256": "<hex>", "mime_type": "image/png", "size_bytes": 123,
        "width": 800, "height": 600
    }}

The ``path`` is relative to the capture root, so Keanu resolves it against
the same root that holds the WAL files.  This keeps the WAL compact while
remaining fully replayable — the reference carries the integrity hash.

Fail-open invariants:
- Base64 image bytes are NEVER written into the WAL.
- A failing decode/write produces a reference with an ``error`` marker
  instead of the raw data; capture never blocks inference.
- Non-data URLs (e.g. ``https://...`` image references) are left untouched —
  they contain no payload bytes and are already what the client sent.

Permission invariant (issue #49):
- The media directory and every media file get an explicit mode taken from the
  capture configuration (``CaptureConfig.directory_mode``/``file_mode``, the
  same pair the WAL writer uses), never from the process umask.  The mode is
  therefore read from the config object at the call site — this module contains
  no mode literal — and a caller that forgets to pass ``config`` fails loudly
  instead of silently inheriting the umask.
- Files are created with that mode through ``os.open(..., O_CREAT, mode)`` and
  the mode is re-applied with ``os.fchmod`` to the open descriptor *before* any
  payload byte is written: no window in which media exists with umask-derived
  permissions, no pathname race on the mode change, and a pre-existing looser
  file is tightened on rewrite.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from app.capture.config import CaptureConfig
from app.capture.data_url import parse_data_url
from app.capture.redactor import _try_extract_image_dimensions

logger = logging.getLogger("Guardian.Capture.Media")

# MIME → file extension map (kept minimal; unknown types get ".bin").
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

MEDIA_SUBDIR = "media"


def _extension_for(mime_type: str) -> str:
    return _MIME_EXT.get((mime_type or "").lower(), ".bin")


def _ensure_media_dir(media_root: Path, config: CaptureConfig) -> None:
    """Create ``media_root`` (if needed) at the configured directory mode.

    ``mkdir(mode=...)`` is masked by the process umask and never touches an
    existing directory, so the explicit ``chmod`` is what makes the configured
    mode authoritative — it also repairs a directory that drifted (the live
    ``data/capture/media`` was ``0755``).
    """
    media_root.mkdir(parents=True, exist_ok=True, mode=config.directory_mode)
    os.chmod(media_root, config.directory_mode)


def ensure_media_directory(capture_root: Path, config: CaptureConfig) -> bool:
    """Enforce the configured mode on ``<capture_root>/media`` (issue #49).

    Called when the capture writer starts, so a directory that drifted (the
    live tree was ``0755``) is corrected without waiting for the next image.
    Deliberately **directory-only**: existing media *files* are never
    bulk-chmoded by the service.  Rewriting already-captured user data is an
    operator-audited one-shot (see the operator runbook's capture section).

    Returns ``True`` when the media directory exists and was checked.
    """
    media_root = Path(capture_root) / MEDIA_SUBDIR
    if not media_root.is_dir():
        return False
    _ensure_media_dir(media_root, config)
    return True


def _write_media_file(
    media_root: Path,
    request_id: str,
    idx: int,
    raw: bytes,
    mime_type: str,
    config: CaptureConfig,
) -> tuple[str, str]:
    """Write one media payload; returns (relative_path, sha256).

    The file mode comes from configuration (``config.file_mode``), not from the
    umask: the descriptor is created with that mode and ``os.fchmod`` applies it
    to *that descriptor* before any payload byte is written.  ``O_CREAT``'s mode
    is umask-masked (it can only come out stricter) and ``fchmod`` cannot be
    redirected by a symlink swapped in after the open, so neither the create
    window nor a pre-existing looser file can leave wider permissions behind.
    ``O_NOFOLLOW`` refuses a symlinked target outright; the caller records a
    fail-open error reference instead of writing through it.
    """
    ext = _extension_for(mime_type)
    filename = f"{request_id}_{idx}{ext}"
    rel = f"{MEDIA_SUBDIR}/{filename}"
    target = media_root / filename
    _ensure_media_dir(target.parent, config)
    sha = hashlib.sha256(raw).hexdigest()
    fd = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        config.file_mode,
    )
    try:
        # Apply the mode to the descriptor that receives the payload: a
        # pathname-based chmod would let a group-writable directory swap the
        # path for a symlink between open() and the mode change, granting
        # access to an unrelated file instead.
        os.fchmod(fd, config.file_mode)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"short write to {target}")
            view = view[written:]
    finally:
        os.close(fd)
    return rel, sha


def _image_metadata_for(
    raw: bytes, mime_type: str
) -> dict[str, Any]:
    """Build the metadata dict (dimensions best-effort)."""
    dims = _try_extract_image_dimensions(raw, mime_type)
    meta: dict[str, Any] = {"mime_type": mime_type, "size_bytes": len(raw)}
    if dims:
        meta["width"] = dims[0]
        meta["height"] = dims[1]
    return meta


def _handle_openai_image_url(
    block: dict[str, Any],
    media_root: Path,
    request_id: str,
    idx: int,
    config: CaptureConfig,
) -> dict[str, Any]:
    """Extract an OpenAI ``image_url`` block's data-URL payload."""
    image_url = block.get("image_url")
    if not isinstance(image_url, dict):
        image_url = {"url": str(image_url) if image_url else ""}
    url = str(image_url.get("url", ""))
    if not url.startswith("data:"):
        # Remote URL — no payload to extract; pass through unchanged.
        return block
    parsed = parse_data_url(url)
    if parsed is None:
        return {"type": "image_media", "image_media": {"error": "malformed_data_url"}}
    mime_type, payload = parsed
    try:
        raw = base64.b64decode(payload)
    except Exception:
        logger.debug("Failed to decode image base64 — error reference only")
        return {"type": "image_media", "image_media": {"error": "decode_failed"}}
    try:
        rel, sha = _write_media_file(
            media_root, request_id, idx, raw, mime_type, config
        )
    except OSError as exc:
        logger.warning("Failed to write media file (fail-open): %s", exc)
        return {"type": "image_media", "image_media": {"error": "write_failed"}}
    meta = _image_metadata_for(raw, mime_type)
    ref = {
        "type": "image_media",
        "image_media": {
            "path": rel,
            "sha256": sha,
            **meta,
        },
    }
    return ref


def _handle_anthropic_image(
    block: dict[str, Any],
    media_root: Path,
    request_id: str,
    idx: int,
    config: CaptureConfig,
) -> dict[str, Any]:
    """Extract an Anthropic ``image`` block's base64 ``source.data`` payload."""
    source = block.get("source")
    if not isinstance(source, dict):
        return block
    if source.get("type") not in ("base64",):
        # URL-type sources carry no payload — pass through.
        return block
    data_str = source.get("data", "")
    mime_type = str(source.get("media_type", "image/png"))
    try:
        raw = base64.b64decode(data_str)
    except Exception:
        logger.debug("Failed to decode Anthropic image base64 — error reference only")
        return {"type": "image_media", "image_media": {"error": "decode_failed"}}
    try:
        rel, sha = _write_media_file(
            media_root, request_id, idx, raw, mime_type, config
        )
    except OSError as exc:
        logger.warning("Failed to write media file (fail-open): %s", exc)
        return {"type": "image_media", "image_media": {"error": "write_failed"}}
    meta = _image_metadata_for(raw, mime_type)
    return {
        "type": "image_media",
        "image_media": {
            "path": rel,
            "sha256": sha,
            **meta,
        },
    }


def extract_media_from_messages(
    messages: Any,
    capture_root: Path,
    request_id: str,
    config: CaptureConfig,
) -> Any:
    """Replace image content blocks in request messages with media references.

    Walks ``messages`` (OpenAI chat format, as captured) and replaces every
    data-bearing image block with an ``image_media`` reference.  Returns the
    (possibly unchanged) messages.  ``messages`` may be ``None`` or a list of
    dicts; non-list input is returned as-is.

    ``config`` supplies the capture permissions (``file_mode`` for the files,
    ``directory_mode`` for ``media/``) so the media tree never inherits the
    ambient umask.  It is required, not defaulted: a caller that cannot supply
    the configured modes must fail rather than silently fall back to the umask.
    """
    if not isinstance(messages, list):
        return messages
    media_root = Path(capture_root) / MEDIA_SUBDIR
    result: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks: list[Any] = []
            for idx, block in enumerate(content):
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                block_type = block.get("type")
                if block_type == "image_url":
                    new_blocks.append(
                        _handle_openai_image_url(
                            block, media_root, request_id, idx, config
                        )
                    )
                elif block_type == "image":
                    new_blocks.append(
                        _handle_anthropic_image(
                            block, media_root, request_id, idx, config
                        )
                    )
                else:
                    new_blocks.append(block)
            new_msg = dict(msg)
            new_msg["content"] = new_blocks
            result.append(new_msg)
        else:
            result.append(msg)
    return result


def is_media_reference(block: Any) -> bool:
    """True when a content block is an ``image_media`` reference."""
    return (
        isinstance(block, dict)
        and block.get("type") == "image_media"
        and isinstance(block.get("image_media"), dict)
    )
