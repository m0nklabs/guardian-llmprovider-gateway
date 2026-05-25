import json
import hashlib
import secrets
import time
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import HTTPException, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.status import HTTP_401_UNAUTHORIZED

logger = logging.getLogger("Auth")

API_KEYS_FILE = Path(__file__).parent.parent.parent / "config" / "api_keys.json"
DEFAULT_API_KEY_PREFIX = "flip"
security_scheme = HTTPBearer(auto_error=False)


def _normalize_api_key_prefix(prefix: Optional[str]) -> str:
    """Return a safe API key prefix with exactly one trailing underscore."""
    normalized = (prefix or DEFAULT_API_KEY_PREFIX).strip().strip("_")
    if not normalized:
        normalized = DEFAULT_API_KEY_PREFIX
    return f"{normalized}_"


def _get_request_header(request: Request, header_name: str) -> Optional[str]:
    """Read a request header safely without trusting mock objects."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(header_name)
    except Exception:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _request_client_address(request: Request) -> tuple[Optional[str], Optional[int]]:
    """Extract the immediate client host and port when FastAPI provides them."""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    port = getattr(client, "port", None)
    if not isinstance(host, str) or not host.strip():
        host = None
    if not isinstance(port, int):
        port = None
    return host, port


def _request_method(request: Request) -> Optional[str]:
    """Extract the HTTP method when the request object exposes it."""
    method = getattr(request, "method", None)
    if not isinstance(method, str) or not method.strip():
        return None
    return method


def _request_path(request: Request) -> Optional[str]:
    """Extract the request path without assuming a concrete FastAPI request type."""
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    if not isinstance(path, str) or not path.strip():
        return None
    return path


def _resolve_local_process_for_port(source_port: Optional[int]) -> tuple[Optional[int], Optional[str]]:
    """Best-effort mapping from a localhost client port to a live process."""
    if source_port is None:
        return None, None

    try:
        result = subprocess.run(
            ["ss", "-tnp"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None, None

    peer_suffix = f":{source_port}"
    for line in result.stdout.splitlines():
        if ":11434" not in line or peer_suffix not in line:
            continue

        pid_match = re.search(r"pid=(\d+)", line)
        process_match = re.search(r'users:\(\("([^\"]+)"', line)
        pid = int(pid_match.group(1)) if pid_match else None
        process_name = process_match.group(1) if process_match else None
        if pid is not None or process_name is not None:
            return pid, process_name

    return None, None


def _token_prefix(token: str) -> str:
    """Return the visible prefix segment for a stored API token."""
    prefix, separator, _ = token.partition("_")
    if separator and prefix:
        return prefix
    return "legacy"


def _token_fingerprint(token: str) -> str:
    """Create a non-secret stable identifier for dashboard attribution."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _build_auth_context(
    request: Request,
    token: Optional[str],
    header_name: Optional[str],
    user_data: Optional[dict],
) -> dict[str, Any]:
    """Build request attribution details for usage monitoring and debugging."""
    metadata = user_data.get("metadata") if isinstance(user_data, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}

    source_ip, source_port = _request_client_address(request)
    name = user_data.get("name") if isinstance(user_data, dict) else None
    project_prefix = metadata.get("project_prefix") or metadata.get("project") or name

    return {
        "client_name": name,
        "project_prefix": project_prefix,
        "key_prefix": _token_prefix(token) if token else None,
        "key_fingerprint": _token_fingerprint(token) if token else None,
        "header_name": header_name,
        "source_ip": source_ip,
        "source_port": source_port,
        "forwarded_for": _get_request_header(request, "x-forwarded-for"),
        "forwarded_proto": _get_request_header(request, "x-forwarded-proto"),
        "host": _get_request_header(request, "host"),
        "origin": _get_request_header(request, "origin"),
        "referer": _get_request_header(request, "referer"),
        "user_agent": _get_request_header(request, "user-agent"),
        "metadata_client": metadata.get("client"),
        "metadata_note": metadata.get("note"),
        "valid": isinstance(user_data, dict),
    }


def _extract_api_key(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials],
) -> tuple[Optional[str], Optional[str]]:
    """Accept both OpenAI-style Bearer tokens and Anthropic-style x-api-key headers."""
    if creds and creds.credentials:
        return creds.credentials, "authorization"

    for header_name in ("x-api-key", "api-key"):
        header_value = _get_request_header(request, header_name)
        if header_value:
            return header_value, header_name

    return None, None


def _log_unauthorized_attempt(
    request: Request,
    reason: str,
    token: Optional[str],
    header_name: Optional[str],
) -> None:
    """Emit a searchable warning for every unauthorized auth failure."""
    source_ip, source_port = _request_client_address(request)
    local_pid = None
    local_process = None
    if source_ip in {"127.0.0.1", "::1"}:
        local_pid, local_process = _resolve_local_process_for_port(source_port)

    logger.warning(
        "❌ Unauthorized API activity: reason=%s method=%s path=%s header=%s source_ip=%s source_port=%s token=%s local_pid=%s local_process=%s",
        reason,
        _request_method(request) or "-",
        _request_path(request) or "-",
        header_name or "-",
        source_ip or "-",
        source_port if source_port is not None else "-",
        token or "-",
        local_pid if local_pid is not None else "-",
        local_process or "-",
    )

def load_api_keys() -> Dict[str, dict]:
    if not API_KEYS_FILE.exists():
        return {}
    try:
        with open(API_KEYS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load API keys: {e}")
        return {}

def save_api_keys(keys: Dict[str, dict]):
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)

def generate_api_key(name: str, metadata: dict = None, prefix: Optional[str] = None) -> str:
    """Generate a new API key with a normalized prefix."""
    prefix = _normalize_api_key_prefix(prefix)
    random_part = secrets.token_hex(16)
    api_key = f"{prefix}{random_part}"
    
    keys = load_api_keys()
    keys[api_key] = {
        "name": name,
        "created_at": time.time(),
        "metadata": metadata or {}
    }
    save_api_keys(keys)
    logger.info(f"Generated new API key for '{name}'")
    return api_key

async def verify_api_key(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Security(security_scheme)):
    """
    Verify API key from Bearer token or Anthropic-style API key headers.
    Returns the metadata associated with the key (including name).
    """
    token, header_name = _extract_api_key(request, creds)
    if not token:
        _log_unauthorized_attempt(
            request,
            reason="missing_api_key",
            token=None,
            header_name=header_name,
        )
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="API Key required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not token.startswith("flip_"):
        # Allow non-prefixed keys if they exist in file (backward compat or manual keys)
        pass

    keys = load_api_keys()
    user_data = keys.get(token)
    request.state.auth_context = _build_auth_context(request, token, header_name, user_data)
    if user_data:
        # Attach user info to request state for logging
        request.state.user = user_data
        logger.info(f"🔑 Auth success: {user_data.get('name', 'Unknown')}")
        return user_data["name"]  # Return client_id/name as expected by endpoints

    _log_unauthorized_attempt(
        request,
        reason="invalid_api_key",
        token=token,
        header_name=header_name,
    )
    raise HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Invalid API Key",
        headers={"WWW-Authenticate": "Bearer"},
    )

if __name__ == "__main__":
    # Helper CLI to generate key
    import sys
    if len(sys.argv) > 1:
        name = sys.argv[1]
        key = generate_api_key(name)
        print(f"Generated API Key for {name}: {key}")
    else:
        print("Usage: python3 -m app.proxy.auth <name>")
