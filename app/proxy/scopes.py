"""Guardian privilege scopes — G1-01.

One declaration per route, one enforcement point. This module owns the scope
vocabulary, the route -> scope inventory (the code form of
``docs/G0_ENDPOINT_AUTH_MATRIX.md``), the migration grace window, and the
key-minting rules that close B-08.

Decision record: ``docs/decisions/G1-01-privilege-scopes.md``.

Keys carry their scopes in the existing per-key ``metadata`` object of
``config/guardian.keys.yaml`` (``metadata.scopes: [client, operator]``), so the
key store needs no schema migration and no new secret. A key with **no**
``metadata.scopes`` list is a legacy key: until ``auth.scopes_enforced_after``
it keeps today's access to privileged routes (logged + counted on every such
call), and from that instant on it is a plain ``client`` key.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping

from app.log_safety import clean_log_value

logger = logging.getLogger("Auth")


class Scope(str, Enum):
    """Privilege class of an authenticated caller.

    ``public`` is not grantable to a key: it means the route needs no key at
    all and is listed here only so the route inventory stays complete.
    """

    PUBLIC = "public"
    CLIENT = "client"
    OPERATOR = "operator"
    ADMIN = "admin"
    CAPTURE_EXPORT = "capture-export"


#: Implied grants. ``admin`` is the superuser class and implies every other
#: class (a key that can mint keys can mint itself a capture-export key, so the
#: capture boundary is not a boundary against ``admin``). ``capture-export``
#: does NOT imply ``client``: an export-only tool must not run inference.
IMPLIED_SCOPES: Mapping[Scope, frozenset[Scope]] = {
    Scope.ADMIN: frozenset(
        {Scope.ADMIN, Scope.OPERATOR, Scope.CLIENT, Scope.CAPTURE_EXPORT}
    ),
    Scope.OPERATOR: frozenset({Scope.OPERATOR, Scope.CLIENT}),
    Scope.CAPTURE_EXPORT: frozenset({Scope.CAPTURE_EXPORT}),
    Scope.CLIENT: frozenset({Scope.CLIENT}),
    Scope.PUBLIC: frozenset({Scope.PUBLIC}),
}

#: Every scope a key can hold, i.e. the effective grant of a grandfathered key.
ALL_KEY_SCOPES: frozenset[Scope] = IMPLIED_SCOPES[Scope.ADMIN]

#: Scope strings accepted in ``metadata.scopes``.
GRANTABLE_SCOPES: frozenset[Scope] = frozenset(
    {Scope.CLIENT, Scope.OPERATOR, Scope.ADMIN, Scope.CAPTURE_EXPORT}
)

#: Scope string -> enum, for parsing ``metadata.scopes``.
_SCOPE_BY_VALUE: Mapping[str, Scope] = {scope.value: scope for scope in GRANTABLE_SCOPES}


def expand_scopes(scopes: frozenset[Scope] | set[Scope]) -> frozenset[Scope]:
    """Return *scopes* closed over :data:`IMPLIED_SCOPES`."""
    expanded: set[Scope] = set()
    for scope in scopes:
        expanded.update(IMPLIED_SCOPES[scope])
    return frozenset(expanded)


#: The single per-route declaration: ``(METHOD, FastAPI path template) -> Scope``.
#:
#: Keyed by the **route template** (``"/v1/{path:path}"``), not the concrete
#: request path, so parameterised and catch-all routes resolve exactly like the
#: documented matrix row. ``HEAD`` requests are folded into ``GET`` at lookup;
#: a method-less route (the dashboard ``/static`` mount) is declared by its
#: collected kind (``MOUNT``) so the inventory stays complete.
#:
#: Every route registered by ``app.proxy.server:app`` and ``app.main:app`` must
#: appear here exactly once. ``tests/unit/test_g0_endpoint_matrix.py`` proves
#: that, and enforces that the matrix column and this table agree. A protected
#: route that is missing here fails closed (typed ``403``), never open.
ROUTE_SCOPES: dict[tuple[str, str], Scope] = {
    # ── Proxy surface (`app.proxy.server:app`) ─────────────────────────
    # Framework documentation routes and the liveness probe. `/metrics` is not
    # here: its payload names models, queue depth and process state, so it is
    # key-holder data (`client`), not a public probe (issue #46, 2026-10-28).
    ("GET", "/openapi.json"): Scope.PUBLIC,
    ("GET", "/docs"): Scope.PUBLIC,
    ("GET", "/docs/oauth2-redirect"): Scope.PUBLIC,
    ("GET", "/redoc"): Scope.PUBLIC,
    ("GET", "/healthz"): Scope.PUBLIC,
    ("GET", "/metrics"): Scope.CLIENT,
    # Ordinary inference, discovery and own-request queue control.
    ("POST", "/api/chat"): Scope.CLIENT,
    ("POST", "/api/generate"): Scope.CLIENT,
    ("GET", "/api/tags"): Scope.CLIENT,
    ("GET", "/v1/models"): Scope.CLIENT,
    ("GET", "/v1/models/{model_id:path}/endpoints"): Scope.CLIENT,
    ("GET", "/v1/models/{model_id:path}"): Scope.CLIENT,
    ("POST", "/api/show"): Scope.CLIENT,
    ("GET", "/v1/queue/status"): Scope.CLIENT,
    ("GET", "/v1/queue/requests/{request_id}"): Scope.CLIENT,
    ("DELETE", "/v1/queue/requests/{request_id}"): Scope.CLIENT,
    ("GET", "/v1/{path:path}"): Scope.CLIENT,
    ("POST", "/v1/{path:path}"): Scope.CLIENT,
    ("POST", "/v1/audio/speech"): Scope.CLIENT,
    ("POST", "/v1/audio/transcriptions"): Scope.CLIENT,
    # Process/model lifecycle: restart-equivalent impact, still short of the
    # key store and of runtime configuration.
    ("POST", "/admin/load"): Scope.OPERATOR,
    ("POST", "/admin/unload"): Scope.OPERATOR,
    # Key store, runtime configuration and shared/control state.
    ("GET", "/api/keys"): Scope.ADMIN,
    ("POST", "/api/keys"): Scope.ADMIN,
    ("POST", "/api/config/reload"): Scope.ADMIN,
    ("GET", "/api/cloud/ratelimit-stats"): Scope.ADMIN,
    ("GET", "/api/cloud/providers"): Scope.ADMIN,
    ("GET", "/api/cloud/catalog"): Scope.ADMIN,
    ("POST", "/api/cloud/catalog/refresh"): Scope.ADMIN,
    ("GET", "/api/cloud/models"): Scope.ADMIN,
    ("GET", "/api/crashes"): Scope.ADMIN,
    ("GET", "/api/status"): Scope.ADMIN,
    ("GET", "/api/scaler"): Scope.ADMIN,
    ("PUT", "/api/scaler"): Scope.ADMIN,
    ("POST", "/api/scaler/reset"): Scope.ADMIN,
    ("POST", "/api/scaler/recommend"): Scope.ADMIN,
    ("POST", "/api/session/save"): Scope.ADMIN,
    ("POST", "/api/session/load"): Scope.ADMIN,
    ("GET", "/api/session/list"): Scope.ADMIN,
    # Capture data is user payload: exporting it is its own privilege.
    ("GET", "/api/capture/status"): Scope.CAPTURE_EXPORT,
    ("POST", "/api/capture/rotate"): Scope.CAPTURE_EXPORT,
    # ── Dashboard surface (`app.main:app`) ─────────────────────────────
    # Routes that exist on both surfaces (`/api/keys`, `/api/cloud/*
    # catalog`, the four docs routes) are declared ONCE above: the requirement
    # is a property of ``(method, path)``, and the matrix guard fails if the two
    # documented rows ever disagree. "One declaration per route" therefore means
    # "one requirement per route identity", not two copies that can drift.
    ("MOUNT", "/static"): Scope.PUBLIC,
    ("GET", "/"): Scope.PUBLIC,
    ("GET", "/favicon.ico"): Scope.PUBLIC,
    ("GET", "/api/stats"): Scope.ADMIN,
    ("GET", "/api/benchmark"): Scope.ADMIN,
    ("POST", "/api/benchmark/start"): Scope.ADMIN,
    ("POST", "/api/benchmark/stop"): Scope.ADMIN,
}


def required_scope_for(method: str | None, path_template: str | None) -> Scope | None:
    """Return the declared scope of one route, or ``None`` when undeclared.

    ``HEAD`` is folded into ``GET`` because Starlette adds ``HEAD`` to every
    ``GET`` route; the handler is the same, so the privilege is the same.
    """
    if not method or not path_template:
        return None
    normalized = "GET" if method.upper() == "HEAD" else method.upper()
    return ROUTE_SCOPES.get((normalized, path_template))


# ── Grace window ───────────────────────────────────────────────────────

#: Config path of the migration deadline (hot-reloadable global settings).
SCOPES_ENFORCED_AFTER_KEY = ("auth", "scopes_enforced_after")

#: Shipped in ``config/global.settings.yaml`` as ``2026-11-11T00:00:00Z``.
#: The code has no implicit fallback: an absent or unparseable value enforces
#: immediately (fail-closed), it never re-opens the window.
_TIMESTAMP_CACHE: dict[tuple[str, str], datetime | None] = {}
_LOGGED_BAD_TIMESTAMPS: set[str] = set()


def parse_enforced_after(raw: Any) -> datetime | None:
    """Parse the configured deadline; ``None`` means "no valid deadline".

    Accepts an ISO-8601 string (``Z`` or offset), a :class:`datetime` or a
    :class:`datetime.date` — PyYAML turns an unquoted ISO timestamp into a
    ``datetime``, so an operator editing the YAML by hand gets either shape.
    A naive value is interpreted as UTC. Anything else is malformed and the
    caller must enforce (fail-closed).
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def configured_enforced_after() -> datetime | None:
    """Return the live ``auth.scopes_enforced_after`` value, or ``None``.

    Reads the shared :data:`app.config_loader.CONFIG` dict on every call so a
    ``POST /api/config/reload`` closes (or moves) the window without a restart.
    A missing/unreadable config, a missing key and a malformed timestamp all
    return ``None``: the caller then treats the window as closed.
    """
    try:
        from app.config_loader import CONFIG
    except Exception as exc:  # pragma: no cover - import failure is fatal anyway
        logger.error("🛡️ Scope config unavailable (%s); enforcing scopes immediately", exc)
        return None

    section = CONFIG.get(SCOPES_ENFORCED_AFTER_KEY[0])
    raw = section.get(SCOPES_ENFORCED_AFTER_KEY[1]) if isinstance(section, dict) else None
    cache_key = (type(raw).__name__, repr(raw))
    if cache_key in _TIMESTAMP_CACHE:
        return _TIMESTAMP_CACHE[cache_key]

    parsed = parse_enforced_after(raw)
    _TIMESTAMP_CACHE[cache_key] = parsed
    if parsed is None:
        marker = clean_log_value(raw)
        if marker not in _LOGGED_BAD_TIMESTAMPS:
            _LOGGED_BAD_TIMESTAMPS.add(marker)
            logger.error(
                "🛡️ auth.scopes_enforced_after is %s; privilege scopes are enforced "
                "immediately (no grace window)",
                "absent" if raw is None else f"malformed ({marker!r})",
            )
    return parsed


def grace_window_open(now: datetime | None = None) -> bool:
    """True while an unscoped legacy key keeps its pre-G1-01 access."""
    deadline = configured_enforced_after()
    if deadline is None:
        return False
    return (now or datetime.now(timezone.utc)) < deadline


# ── Per-key resolution ─────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyScopes:
    """Resolved scope state of one key entry."""

    #: Literal scopes declared in ``metadata.scopes``; ``None`` when absent.
    declared: frozenset[Scope] | None
    #: Effective grant, implied scopes included.
    granted: frozenset[Scope]
    #: ``explicit``, ``grace`` (legacy key inside the window), ``unscoped``
    #: (legacy key after the window) or ``invalid``.
    mode: str
    invalid_tokens: tuple[str, ...] = field(default_factory=tuple)
    malformed: bool = False

    def describe(self) -> str:
        return ",".join(sorted(scope.value for scope in self.granted)) or "-"


def _metadata(entry: Mapping[str, Any] | None) -> Mapping[str, Any]:
    metadata = entry.get("metadata") if isinstance(entry, Mapping) else None
    return metadata if isinstance(metadata, Mapping) else {}


def _parse_declared(raw: Any) -> tuple[frozenset[Scope] | None, tuple[str, ...], bool]:
    """Return ``(declared, invalid_tokens, malformed)`` for ``metadata.scopes``."""
    if raw is None:
        return None, (), False
    if not isinstance(raw, (list, tuple)):
        return None, (), True
    tokens: list[str] = []
    invalid: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            invalid.append(clean_log_value(item))
            continue
        value = item.strip().lower()
        tokens.append(value)
        if value not in _SCOPE_BY_VALUE:
            invalid.append(clean_log_value(item))
    if invalid:
        return None, tuple(invalid), False
    return frozenset(_SCOPE_BY_VALUE[value] for value in tokens), (), False


def resolve_key_scopes(
    entry: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> KeyScopes:
    """Resolve one key entry to its effective scopes.

    Rules (decision record §Compatibility):

    * explicit, fully known ``metadata.scopes`` -> those scopes, closed over
      :data:`IMPLIED_SCOPES`, enforced from day one;
    * absent -> legacy key: every key scope while the window is open, plain
      ``client`` once it is closed;
    * present but malformed (not a list of strings, explicit ``null``) or
      carrying an unknown token -> ``client`` only. Privilege fails closed; ordinary inference keeps
      working instead of the key silently becoming unusable.
    """
    metadata = _metadata(entry)
    declared, invalid_tokens, malformed = _parse_declared(metadata.get("scopes"))
    if "scopes" in metadata and metadata["scopes"] is None:
        # Present-but-null is an explicit (broken) declaration, not an absence:
        # it must fail closed instead of inheriting the legacy grace grant.
        malformed = True

    if invalid_tokens or malformed:
        return KeyScopes(
            declared=None,
            granted=IMPLIED_SCOPES[Scope.CLIENT],
            mode="invalid",
            invalid_tokens=invalid_tokens,
            malformed=malformed,
        )
    if declared is not None:
        return KeyScopes(declared=declared, granted=expand_scopes(declared), mode="explicit")
    if grace_window_open(now):
        return KeyScopes(declared=None, granted=ALL_KEY_SCOPES, mode="grace")
    return KeyScopes(declared=None, granted=IMPLIED_SCOPES[Scope.CLIENT], mode="unscoped")


@dataclass(frozen=True)
class AuthorizationDecision:
    """Outcome of one route authorization check."""

    required: Scope
    key_scopes: KeyScopes
    allowed: bool

    @property
    def grace_used(self) -> bool:
        """True only when grace actually bought a *privileged* call.

        ``client`` access survives the deadline, so marking ordinary inference
        and discovery as a grace use would flood the log and the counter with
        requests that need no migration at all.
        """
        return (
            self.allowed
            and self.required not in {Scope.PUBLIC, Scope.CLIENT}
            and self.key_scopes.mode == "grace"
        )


def authorize_key(
    entry: Mapping[str, Any] | None,
    required: Scope,
    *,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Check one key against one route's required scope."""
    resolved = resolve_key_scopes(entry, now=now)
    return AuthorizationDecision(
        required=required,
        key_scopes=resolved,
        allowed=required in resolved.granted,
    )


def structured_event(payload: Mapping[str, Any]) -> str:
    """Render a structured (JSON) log payload with control characters removed."""
    safe = {key: clean_log_value(value) for key, value in payload.items()}
    return json.dumps(safe, sort_keys=True, separators=(",", ":"))


# ── Key minting (B-08) ─────────────────────────────────────────────────


class ScopeError(ValueError):
    """A rejected scope request; ``code`` maps to the API error type."""

    def __init__(self, code: str, message: str, *, scope: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.scope = scope


@dataclass(frozen=True)
class MintGrant:
    """Resolved grant for a key minted through the admin API."""

    metadata: dict[str, Any]
    cloud_gateway_access: bool


def mint_metadata(
    minter_entry: Mapping[str, Any] | None,
    requested_metadata: Any,
    *,
    requested_cloud_access: Any = None,
    now: datetime | None = None,
    source: str = "api",
) -> MintGrant:
    """Compute the grant for a key minted through the admin API.

    B-08 rule: a minted key never holds a scope the minting key does not
    declare, never upgrades ``cloud_gateway_access``, and never turns a grace
    grant into an explicit scope. Raises :class:`ScopeError` otherwise.
    """
    if not isinstance(minter_entry, Mapping):
        raise ScopeError(
            "authorization_misconfigured",
            "cannot resolve the minting key's scopes; refusing to mint",
        )

    minter = resolve_key_scopes(minter_entry, now=now)
    if minter.mode == "invalid":
        raise ScopeError(
            "authorization_misconfigured",
            "the minting key declares an invalid scope list; fix it before minting",
        )

    metadata: dict[str, Any] = dict(requested_metadata) if isinstance(requested_metadata, Mapping) else {}

    if requested_cloud_access is not None and bool(requested_cloud_access) and not bool(
        minter_entry.get("cloud_gateway_access", True)
    ):
        raise ScopeError(
            "scope_escalation",
            "a minting key without cloud_gateway_access cannot mint a key that has it",
            scope="cloud_gateway_access",
        )

    requested = metadata.get("scopes")
    if requested is not None:
        declared, invalid_tokens, malformed = _parse_declared(requested)
        if malformed or invalid_tokens:
            detail = ", ".join(invalid_tokens) if invalid_tokens else clean_log_value(requested)
            raise ScopeError(
                "invalid_scope",
                f"unknown or malformed scope(s) requested: {detail}",
                scope=detail,
            )
        assert declared is not None  # for type checkers
        # An explicitly scoped minter may hand out anything inside its own grant
        # (declared scopes plus their implications, e.g. operator -> client). A
        # grandfathered/unscoped minter holds no *declared* grant, so it cannot
        # mint an explicit scope at all: minting never makes grace permanent.
        allowed = minter.granted if minter.mode == "explicit" else frozenset()
        exceeding = sorted(scope.value for scope in declared - allowed)
        if exceeding:
            raise ScopeError(
                "scope_escalation",
                "minting key does not hold the requested scope(s): " + ", ".join(exceeding),
                scope=",".join(exceeding),
            )
        metadata["scopes"] = sorted(scope.value for scope in declared)
    elif minter.declared:
        # Inherit the minting key's declaration, so a scoped key's children stay
        # inside the same envelope without the caller repeating it. (An implied
        # scope is never promoted to a declaration: operator stays operator.)
        metadata["scopes"] = sorted(scope.value for scope in minter.declared)
    # A grandfathered (unscoped) minter yields an unscoped key: minting can
    # never make a grace grant permanent.

    metadata["minted_by"] = str(minter_entry.get("name") or "unknown")
    metadata["minted_via"] = source
    return MintGrant(
        metadata=metadata,
        cloud_gateway_access=bool(minter_entry.get("cloud_gateway_access", True)),
    )
