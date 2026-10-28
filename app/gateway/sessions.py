"""Session slot management — save/load/list llama-server session files.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
The route decorators and thin wrappers stay in server.py; the handler logic
lives here.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from app.destination_policy import guarded_async_client
from app.log_safety import clean_log_value
import httpx  # noqa: F401 — kept as a monkeypatch target (tests patch <module>.httpx.AsyncClient)
from fastapi import HTTPException, Request

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_llama_server_url = None
_SESSION_SLOTS_DIR = None  # injected via init()
_SESSION_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.bin$")


def init(*, llama_server_url: str, session_slots_dir: Path) -> None:
    """Inject all dependencies. Called once at startup."""
    global _llama_server_url, _SESSION_SLOTS_DIR
    _llama_server_url = llama_server_url
    _SESSION_SLOTS_DIR = Path(session_slots_dir)


def sanitize_session_filename(raw: object) -> str:
    """Return a safe basename for a session slot, or raise HTTP 400.

    Two independent constraints, because llama-server joins whatever it is
    handed onto its own ``--slot-save-path``:

    1. the value is reduced to its final component and must be
       ``[A-Za-z0-9_-]+.bin`` — no separator, no ``..`` component and no NUL
       can survive that character class;
    2. the joined path is normalised with ``os.path.realpath`` (which resolves
       symlinks) and must still sit under the normalised slots directory, so a
       symlink planted in the slots directory cannot redirect the write
       outside it.

    The containment shape — normalise, then a ``startswith`` prefix check on
    the normalised form — is deliberate: it is the shape CodeQL's
    ``py/path-injection`` model recognises as a barrier, the same way
    ``clean_log_value``'s ``str.replace`` shape is what the log-injection model
    recognises (see ``docs/decisions/G1-05-sensitive-value-verdicts.md``).
    """
    if not isinstance(raw, str) or not raw:
        raise HTTPException(status_code=400, detail="Filename required")
    basename = Path(raw).name  # drop any directory components
    if not _SESSION_FILENAME_RE.fullmatch(basename):
        raise HTTPException(
            status_code=400,
            detail="Invalid filename: use letters, digits, '_' or '-' with a .bin suffix",
        )
    if _SESSION_SLOTS_DIR is None:
        raise HTTPException(
            status_code=500, detail="Session slots directory not configured"
        )
    slots_dir = os.path.realpath(str(_SESSION_SLOTS_DIR))
    candidate = os.path.realpath(os.path.join(slots_dir, basename))
    if not candidate.startswith(slots_dir + os.sep):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return basename

async def save_session(request: Request, client_id: str) -> Any:
    """Save the current llama-server slot state to a session file."""
    logger.info("💾 Session SAVE request from %s", clean_log_value(client_id))
    try:
        data = await request.json()
        filename = sanitize_session_filename(data.get("filename"))
        
        async with guarded_async_client(purpose="session-state") as client:
            resp = await client.post(
                f"{_llama_server_url}/slots/0?action=save",
                json={"filename": filename},
                timeout=60.0
            )  
            if resp.status_code != 200:
                logger.error(f"Llama save failed: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Llama save failed: {resp.text}")
                
            return resp.json()
    except HTTPException:
        # Let client-facing 4xx (e.g. filename-sanitization 400) propagate unchanged.
        raise
    except Exception as e:
        logger.error(f"Save session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def load_session(request: Request, client_id: str) -> Any:
    """Restore a llama-server slot state from a session file."""
    logger.info("📂 Session LOAD request from %s", clean_log_value(client_id))
    try:
        data = await request.json()
        filename = sanitize_session_filename(data.get("filename"))
            
        async with guarded_async_client(purpose="session-state") as client:
            resp = await client.post(
                f"{_llama_server_url}/slots/0?action=restore",
                json={"filename": filename},
                timeout=60.0 # Loading takes time
            )
            if resp.status_code != 200:
                logger.error(f"Llama load failed: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Llama load failed: {resp.text}")
                
            return resp.json()
    except HTTPException:
        # Let client-facing 4xx (e.g. filename-sanitization 400) propagate unchanged.
        raise
    except Exception as e:
        logger.error(f"Load session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def list_sessions(client_id: str) -> Any:
    """List available session files."""
    logger.debug("📜 Session LIST request from %s", clean_log_value(client_id))
    try:
        save_path = _SESSION_SLOTS_DIR
        if not save_path.exists():
            return {"sessions": []}
            
        files = [f.stem for f in save_path.glob("*.bin")]
        return {"sessions": sorted(files)}
    except Exception as e:
        logger.error(f"List sessions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
