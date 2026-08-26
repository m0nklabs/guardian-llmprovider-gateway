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
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.capture.redactor import _try_extract_image_dimensions

logger = logging.getLogger("Guardian.Capture.Media")

# MIME → file extension map (kept minimal; unknown types get ".bin").
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

_DATA_URL_RE = re.compile(r"data:([^;]+);base64,(.*)", re.DOTALL)

MEDIA_SUBDIR = "media"


def _extension_for(mime_type: str) -> str:
    return _MIME_EXT.get((mime_type or "").lower(), ".bin")


def _write_media_file(
    media_root: Path,
    request_id: str,
    idx: int,
    raw: bytes,
    mime_type: str,
) -> Tuple[str, str]:
    """Write one media payload; returns (relative_path, sha256)."""
    ext = _extension_for(mime_type)
    filename = f"{request_id}_{idx}{ext}"
    rel = f"{MEDIA_SUBDIR}/{filename}"
    target = media_root / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(raw).hexdigest()
    target.write_bytes(raw)
    return rel, sha


def _image_metadata_for(
    raw: bytes, mime_type: str
) -> Dict[str, Any]:
    """Build the metadata dict (dimensions best-effort)."""
    dims = _try_extract_image_dimensions(raw, mime_type)
    meta: Dict[str, Any] = {"mime_type": mime_type, "size_bytes": len(raw)}
    if dims:
        meta["width"] = dims[0]
        meta["height"] = dims[1]
    return meta


def _handle_openai_image_url(
    block: Dict[str, Any],
    media_root: Path,
    request_id: str,
    idx: int,
) -> Dict[str, Any]:
    """Extract an OpenAI ``image_url`` block's data-URL payload."""
    image_url = block.get("image_url")
    if not isinstance(image_url, dict):
        image_url = {"url": str(image_url) if image_url else ""}
    url = str(image_url.get("url", ""))
    if not url.startswith("data:"):
        # Remote URL — no payload to extract; pass through unchanged.
        return block
    match = _DATA_URL_RE.match(url)
    if not match:
        return {"type": "image_media", "image_media": {"error": "malformed_data_url"}}
    mime_type = match.group(1)
    try:
        raw = base64.b64decode(match.group(2))
    except Exception:
        logger.debug("Failed to decode image base64 — error reference only")
        return {"type": "image_media", "image_media": {"error": "decode_failed"}}
    try:
        rel, sha = _write_media_file(media_root, request_id, idx, raw, mime_type)
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
    block: Dict[str, Any],
    media_root: Path,
    request_id: str,
    idx: int,
) -> Dict[str, Any]:
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
        rel, sha = _write_media_file(media_root, request_id, idx, raw, mime_type)
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
) -> Any:
    """Replace image content blocks in request messages with media references.

    Walks ``messages`` (OpenAI chat format, as captured) and replaces every
    data-bearing image block with an ``image_media`` reference.  Returns the
    (possibly unchanged) messages.  ``messages`` may be ``None`` or a list of
    dicts; non-list input is returned as-is.
    """
    if not isinstance(messages, list):
        return messages
    media_root = Path(capture_root) / MEDIA_SUBDIR
    result: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks: List[Any] = []
            for idx, block in enumerate(content):
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                block_type = block.get("type")
                if block_type == "image_url":
                    new_blocks.append(
                        _handle_openai_image_url(block, media_root, request_id, idx)
                    )
                elif block_type == "image":
                    new_blocks.append(
                        _handle_anthropic_image(block, media_root, request_id, idx)
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
