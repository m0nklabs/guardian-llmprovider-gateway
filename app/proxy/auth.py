import hashlib
import json
import logging
import os
import asyncio
import re
import secrets
import subprocess
import time
from typing import Any

import yaml
from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.status import HTTP_401_UNAUTHORIZED

from app.log_safety import clean_log_value, register_secret, register_secrets
from app.paths import GUARDIAN_APIKEYS_FILE, LEGACY_APIKEYS_FILE
from app.proxy import admission as _admission
from app.proxy import audit as _audit
from app.proxy import scopes as _scopes

logger = logging.getLogger("Auth")

# New ``guardian_apikeys.yaml`` is the canonical write target; the legacy
# ``api_keys.json`` remains a read-only backward-compat alias that is migrated
# to the new file the first time keys are saved.
API_KEYS_FILE = GUARDIAN_APIKEYS_FILE
DEFAULT_API_KEY_PREFIX = "flip"
security_scheme = HTTPBearer(auto_error=False)


def _ensure_api_keys_file_permissions() -> None:
    """Restrict the plaintext Guardian API key store to its owner."""
    try:
        if API_KEYS_FILE.exists():
            API_KEYS_FILE.chmod(0o600)
    except OSError as exc:
        logger.error("Failed to secure Guardian API key file permissions: %s", exc)


def _normalize_api_key_prefix(prefix: str | None) -> str:
    """Return a safe API key prefix with exactly one trailing underscore."""
    normalized = (prefix or DEFAULT_API_KEY_PREFIX).strip().strip("_")
    if not normalized:
        normalized = DEFAULT_API_KEY_PREFIX
    return f"{normalized}_"


def _get_request_header(request: Request, header_name: str) -> str | None:
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


def _request_client_address(request: Request) -> tuple[str | None, int | None]:
    """Extract the immediate client host and port when FastAPI provides them."""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    port = getattr(client, "port", None)
    if not isinstance(host, str) or not host.strip():
        host = None
    if not isinstance(port, int):
        port = None
    return host, port


def _request_method(request: Request) -> str | None:
    """Extract the HTTP method when the request object exposes it."""
    method = getattr(request, "method", None)
    if not isinstance(method, str) or not method.strip():
        return None
    return method


def _request_path(request: Request) -> str | None:
    """Extract the request path without assuming a concrete FastAPI request type."""
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    if not isinstance(path, str) or not path.strip():
        return None
    return path


def _resolve_local_process_for_port(source_port: int | None) -> tuple[int | None, str | None]:
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
    token: str | None,
    header_name: str | None,
    user_data: dict | None,
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
        "cloud_gateway_access": bool((user_data or {}).get("cloud_gateway_access", True)),
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


def get_request_auth_context(request: Request) -> dict[str, Any] | None:
    """Return any auth context already stored on the request or its shared scope."""
    state_obj = getattr(request, "state", None)
    auth_context = getattr(state_obj, "auth_context", None)
    if isinstance(auth_context, dict):
        return auth_context

    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        auth_context = scope.get("guardian_auth_context")
        if isinstance(auth_context, dict):
            return auth_context

    return None


def set_request_auth_context(request: Request, auth_context: dict[str, Any]) -> dict[str, Any]:
    """Store auth context on both request.state and the shared ASGI scope."""
    state_obj = getattr(request, "state", None)
    if state_obj is not None:
        state_obj.auth_context = auth_context

    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        scope["guardian_auth_context"] = auth_context

    return auth_context


def build_request_auth_context(
    request: Request,
    *,
    token: str | None = None,
    header_name: str | None = None,
    user_data: dict | None = None,
) -> dict[str, Any]:
    """Build request attribution even when authentication has not succeeded yet."""
    resolved_token = token
    resolved_header_name = header_name
    if resolved_token is None and resolved_header_name is None:
        authorization = _get_request_header(request, "authorization")
        if authorization:
            scheme, _, credentials = authorization.partition(" ")
            if scheme.lower() == "bearer" and credentials.strip():
                resolved_token = credentials.strip()
                resolved_header_name = "authorization"
        if resolved_token is None and resolved_header_name is None:
            resolved_token, resolved_header_name = _extract_api_key(request, None)
    return _build_auth_context(request, resolved_token, resolved_header_name, user_data)


def _extract_api_key(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
) -> tuple[str | None, str | None]:
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
    token: str | None,
    header_name: str | None,
) -> None:
    """Emit a searchable warning for every unauthorized auth failure."""
    source_ip, source_port = _request_client_address(request)
    local_pid = None
    local_process = None
    if source_ip in {"127.0.0.1", "::1"}:
        local_pid, local_process = _resolve_local_process_for_port(source_port)
    token_fingerprint = _token_fingerprint(token) if token else "-"

    logger.warning(
        "❌ Unauthorized API activity: reason=%s method=%s path=%s header=%s source_ip=%s source_port=%s token_fingerprint=%s local_pid=%s local_process=%s",
        reason,
        _request_method(request) or "-",
        _request_path(request) or "-",
        header_name or "-",
        source_ip or "-",
        source_port if source_port is not None else "-",
        token_fingerprint,
        local_pid if local_pid is not None else "-",
        local_process or "-",
    )


def _record_unauthorized_api_usage(request: Request, *, status_code: int = HTTP_401_UNAUTHORIZED) -> None:
    """Finalize usage tracking for auth failures before FastAPI returns the 401."""
    state_obj = getattr(request, "state", None)
    if state_obj is None or getattr(state_obj, "guardian_usage_finished", False):
        return

    request_id = getattr(state_obj, "guardian_usage_request_id", None)
    if not isinstance(request_id, str) or not request_id.strip():
        return

    endpoint = _request_path(request)
    method = _request_method(request)
    if endpoint is None or method is None:
        return

    try:
        from app.proxy.server import state as proxy_state
    except Exception:
        return

    auth_context = get_request_auth_context(request) or build_request_auth_context(request)
    proxy_state.api_usage.finish_request(
        request_id=request_id,
        client_id=None,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        response_bytes=0,
        streamed=False,
        attribution=auth_context,
    )
    state_obj.guardian_usage_finished = True


# ── Privilege scopes (G1-01, docs/decisions/G1-01-privilege-scopes.md) ──
#
# The route -> scope inventory lives in ``app.proxy.scopes.ROUTE_SCOPES``; this
# is the single enforcement point. Authorization is resolved from the matched
# FastAPI route template, so a parameterised or catch-all path can never pair
# with another route's scope. Both side effects of a wrong answer are
# deliberate: an undeclared protected route denies (fail-closed), and a
# malformed scope declaration denies privilege while leaving ordinary
# ``client`` inference working.


def _route_template(request: Request) -> str | None:
    """Return the matched route's path template, or ``None`` without a route."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else None


def _scope_counters():
    """Return ``(grace, denials)`` counters, or ``(None, None)`` when unavailable."""
    try:
        from app.proxy.metrics import SCOPE_DENIALS, SCOPE_GRACE_CALLS

        return SCOPE_GRACE_CALLS, SCOPE_DENIALS
    except Exception:  # pragma: no cover - metrics are observability, not policy
        return None, None


def _bump_counter(counter, **labels: str) -> None:
    if counter is None:
        return
    try:
        counter.labels(**labels).inc()
    except Exception:  # pragma: no cover - never let metrics break a request
        pass


def _scope_error(
    *,
    error_type: str,
    message: str,
    required: str | None,
    granted: list[str],
    key_name: str | None,
    route: str | None,
) -> HTTPException:
    """Build the stable typed 403 body used for every scope denial."""
    detail: dict[str, Any] = {
        "type": error_type,
        "message": message,
        "required_scope": required,
        "granted_scopes": granted,
        "key": clean_log_value(key_name) if key_name else None,
        "route": route,
    }
    headers = {"WWW-Authenticate": 'Bearer error="insufficient_scope"'}
    if required:
        headers["WWW-Authenticate"] = (
            f'Bearer error="insufficient_scope", scope="{required}"'
        )
    return HTTPException(status_code=403, detail=detail, headers=headers)


def _enforce_route_scope(request: Request, user_data: dict[str, Any]) -> None:
    """Authorize one authenticated request against its route's required scope.

    A request that carries no route template (a direct ``verify_api_key`` call
    in a unit test) has no route to protect and is left to the caller.
    """
    template = _route_template(request)
    if template is None:
        return

    method = (_request_method(request) or "-").upper()
    route_label = f"{method} {template}"
    required = _scopes.required_scope_for(method, template)
    key_name = user_data.get("name") if isinstance(user_data, dict) else None
    grace_counter, denial_counter = _scope_counters()

    if required is None:
        logger.error(
            "🛡️ scope-denied %s",
            _scopes.structured_event(
                {
                    "event": "scope_undeclared_route",
                    "key": key_name,
                    "route": route_label,
                    "reason": "route has no declared scope; denying (fail-closed)",
                }
            ),
        )
        _bump_counter(denial_counter, scope="-", route=route_label)
        _audit.mark_denial(
            request=request,
            reason=_audit.REASON_UNDECLARED_ROUTE,
            status=403,
        )
        _record_unauthorized_api_usage(request, status_code=403)
        raise _scope_error(
            error_type="authorization_misconfigured",
            message=(
                f"route {route_label} has no declared privilege scope; "
                "access denied (fail-closed)"
            ),
            required=None,
            granted=[],
            key_name=key_name,
            route=route_label,
        )

    if required == _scopes.Scope.PUBLIC:
        return

    decision = _scopes.authorize_key(user_data, required)
    auth_context = get_request_auth_context(request)
    if isinstance(auth_context, dict):
        auth_context["scopes"] = sorted(
            scope.value for scope in decision.key_scopes.granted
        )
        auth_context["scope_mode"] = decision.key_scopes.mode

    if decision.allowed:
        _audit.mark_allow(
            request=request,
            reason=(
                _audit.REASON_SCOPE_GRACE
                if decision.grace_used
                else _audit.REASON_SCOPE_GRANTED
            ),
        )
        if decision.grace_used:
            deadline = _scopes.configured_enforced_after()
            logger.warning(
                "⏳ scope-grace %s",
                _scopes.structured_event(
                    {
                        "event": "scope_grace",
                        "key": key_name,
                        "route": route_label,
                        "missing_scope": required.value,
                        "granted_scopes": ",".join(
                            sorted(scope.value for scope in decision.key_scopes.granted)
                        ),
                        "enforced_after": deadline.isoformat() if deadline else "none",
                    }
                ),
            )
            _bump_counter(grace_counter, scope=required.value, route=route_label)
        return

    reason = "insufficient_scope"
    if decision.key_scopes.mode == "invalid":
        reason = "invalid_scope_declaration"
    logger.warning(
        "🛡️ scope-denied %s",
        _scopes.structured_event(
            {
                "event": "scope_denied",
                "key": key_name,
                "route": route_label,
                "required_scope": required.value,
                "granted_scopes": ",".join(
                    sorted(scope.value for scope in decision.key_scopes.granted)
                ),
                "mode": decision.key_scopes.mode,
                "reason": reason,
            }
        ),
    )
    _bump_counter(denial_counter, scope=required.value, route=route_label)
    _audit.mark_denial(
        request=request,
        reason=(
            _audit.REASON_INVALID_SCOPE_DECLARATION
            if reason == "invalid_scope_declaration"
            else _audit.REASON_INSUFFICIENT_SCOPE
        ),
        status=403,
        detail=reason,
    )
    _record_unauthorized_api_usage(request, status_code=403)
    raise _scope_error(
        error_type="insufficient_scope",
        message=(
            f"key '{clean_log_value(key_name) if key_name else 'unknown'}' lacks the "
            f"required scope '{required.value}' for {route_label}"
        ),
        required=required.value,
        granted=sorted(scope.value for scope in decision.key_scopes.granted),
        key_name=key_name,
        route=route_label,
    )


def _migrate_legacy_keys() -> dict[str, dict]:
    """Load keys from the legacy ``api_keys.json`` if the new file is absent.

    The legacy store is a JSON object mapping a token to its metadata dict.
    On a successful read the store is rewritten to the new YAML file so the
    whole codebase converges on the single ``guardian_apikeys.yaml`` source.
    """
    if not LEGACY_APIKEYS_FILE.exists():
        return {}
    try:
        with open(LEGACY_APIKEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load legacy API keys: {e}")
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _normalize_key_entry(token: str, raw: Any) -> dict[str, Any]:
    """Coerce a stored key entry to the canonical dict shape.

    Ensures every key carries ``name``, ``created_at``, ``metadata`` and the
    new ``cloud_gateway_access`` boolean (default ``True``).
    """
    if not isinstance(raw, dict):
        raw = {"name": token}
    entry = dict(raw)
    if not entry.get("name"):
        entry["name"] = token
    entry.setdefault("created_at", time.time())
    if not isinstance(entry.get("metadata"), dict):
        entry["metadata"] = {}
    entry.setdefault("cloud_gateway_access", True)
    return entry


#: mtime-gated cache — verify_api_key() runs load_api_keys() on EVERY
#: request; the yaml re-parse is pointless while the file is unchanged.
#: save_api_keys() writes the file (mtime changes), so edits invalidate
#: automatically.
_API_KEYS_CACHE: dict[str, object] = {}


def load_api_keys() -> dict[str, dict]:
    try:
        st = API_KEYS_FILE.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None and _API_KEYS_CACHE.get("key") == cache_key:
        return _API_KEYS_CACHE["value"]
    loaded = _load_api_keys_uncached()
    if cache_key is not None:
        _API_KEYS_CACHE["key"] = cache_key
        _API_KEYS_CACHE["value"] = loaded
    return loaded


def _load_api_keys_uncached() -> dict[str, dict]:
    if API_KEYS_FILE.exists():
        try:
            _ensure_api_keys_file_permissions()
            with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Failed to load API keys: {e}")
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
    else:
        raw = _migrate_legacy_keys()

    keys = {
        token: _normalize_key_entry(token, entry)
        for token, entry in raw.items()
    }
    # G1-03 §11.2: the credentials *this* service hands out are secrets too, so
    # the G1-02 redaction filter must know them. G1-02 registers provider
    # credentials and the caretaker key; the Guardian key store was the gap — a
    # bare token in a log line had no Bearer prefix to match on. The registered
    # set is bounded by key history (tens of values, never by traffic), so it
    # costs one comparison per secret per log line, not per-request memory.
    register_secrets(keys)
    return keys


def save_api_keys(keys: dict[str, dict]):
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = API_KEYS_FILE.with_suffix(API_KEYS_FILE.suffix + ".tmp")
    file_descriptor = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    os.fchmod(file_descriptor, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as f:
        yaml.safe_dump(keys, f, allow_unicode=True, sort_keys=False)
    tmp_path.replace(API_KEYS_FILE)


_ensure_api_keys_file_permissions()

def generate_api_key(
    name: str,
    metadata: dict = None,
    prefix: str | None = None,
    *,
    cloud_gateway_access: bool = True,
) -> str:
    """Generate a new API key with a normalized prefix.

    ``cloud_gateway_access`` is inherited from the minting key by the admin API
    (B-08: minting never upgrades cloud access). The CLI/offline default stays
    ``True`` — a host-local operator minting a key by hand is not constrained by
    another key's envelope.
    """
    prefix = _normalize_api_key_prefix(prefix)
    random_part = secrets.token_hex(16)
    api_key = f"{prefix}{random_part}"
    
    keys = load_api_keys()
    keys[api_key] = {
        "name": name,
        "created_at": time.time(),
        "metadata": metadata or {},
        "cloud_gateway_access": bool(cloud_gateway_access),
    }
    save_api_keys(keys)
    register_secret(api_key)
    logger.info("Generated new API key for '%s'", clean_log_value(name))
    return api_key

async def verify_api_key(request: Request, creds: HTTPAuthorizationCredentials | None = Security(security_scheme)):
    """
    Verify API key from Bearer token or Anthropic-style API key headers.
    Returns the metadata associated with the key (including name).
    """
    token, header_name = _extract_api_key(request, creds)
    set_request_auth_context(
        request,
        build_request_auth_context(
            request,
            token=token,
            header_name=header_name,
        ),
    )
    if not token:
        _audit.mark_denial(
            request=request,
            reason=_audit.REASON_MISSING_API_KEY,
            status=HTTP_401_UNAUTHORIZED,
        )
        await asyncio.to_thread(
            _log_unauthorized_attempt,
            request,
            reason="missing_api_key",
            token=None,
            header_name=header_name,
        )
        _record_unauthorized_api_usage(request)
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
    set_request_auth_context(
        request,
        build_request_auth_context(
            request,
            token=token,
            header_name=header_name,
            user_data=user_data,
        ),
    )
    if user_data:
        # Attach user info to request state for logging (and for the admin API's
        # minting check, which must know which key is minting).
        request.state.user = user_data
        _enforce_route_scope(request, user_data)
        # G1-03 admission: the per-key request rate is checked *after*
        # authentication and authorization (so the bucket belongs to an
        # identified, authorized caller) and *before* the handler runs (so a
        # rate-limited request never enters the inference queue). Keyed on the
        # G1-01 identity fingerprint, never on the source IP.
        _admission.enforce_rate_limit(_token_fingerprint(token))
        logger.info(f"🔑 Auth success: {user_data.get('name', 'Unknown')}")
        return user_data["name"]  # Return client_id/name as expected by endpoints

    _audit.mark_denial(
        request=request,
        reason=_audit.REASON_INVALID_API_KEY,
        status=HTTP_401_UNAUTHORIZED,
    )
    await asyncio.to_thread(
        _log_unauthorized_attempt,
        request,
        reason="invalid_api_key",
        token=token,
        header_name=header_name,
    )
    _record_unauthorized_api_usage(request)
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
