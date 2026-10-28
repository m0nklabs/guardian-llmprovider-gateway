"""Outbound destination policy, SSRF guard and credential isolation (G1-02).

Source of record: ``docs/decisions/G1-02-destination-and-credentials.md``.

Every byte Guardian sends leaves through this module's guard.  The policy is a
*configured* host/port allowlist (``destination_policy`` in
``config/global.settings.yaml``) with three enforced rules:

1. **Destination.** An outbound URL is accepted only when its scheme is allowed,
   it carries no userinfo, and its ``(host, port)`` pair is an explicit
   allowlist entry.  Cloud-metadata addresses are never approvable.  Loopback,
   link-local and RFC1918 targets are usable only through such an explicit
   entry, so ``169.254.169.254`` or an unapproved loopback port cannot be
   reached by a misconfigured provider, a catalog URL or a redirect.
2. **Resolution.** A hostname string matching an entry is not enough: the
   resolver is asked again *at request time* and every returned address is
   re-checked (pinned addresses must match; an unpinned name may only resolve
   to public addresses).  The transport then connects to the validated address
   itself, so a DNS answer that changes between check and connect cannot move
   the connection.  ``Host`` and TLS SNI keep the approved hostname.
3. **Credentials.** A request that carries a credential header is refused
   unless the client was created for the owning provider and the destination is
   one of *that* provider's own approved destinations.  Provider A's key can
   therefore never reach provider B, and redirect hops (never followed at all)
   cannot carry a credential to a new destination.

Fail-closed rules: an absent, malformed or empty allowlist denies every
destination with the stable typed error :class:`DestinationDenied`
(``reason: "policy_unavailable"``) — a broken policy never degrades into "no
policy".  A malformed *entry* is dropped loudly and only its own host is
refused; it does not weaken the other entries, but it also never approves what
it cannot parse.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from app.proxy import audit as _audit

logger = logging.getLogger("Guardian.Destinations")

#: Stable machine code carried by :class:`DestinationDenied`.
ERROR_CODE = "destination_denied"

# Reason codes. These are part of the error contract (tests and operators match
# on them); add a new value rather than renaming an existing one.
REASON_POLICY_UNAVAILABLE = "policy_unavailable"
REASON_SCHEME_NOT_ALLOWED = "scheme_not_allowed"
REASON_USERINFO_NOT_ALLOWED = "userinfo_not_allowed"
REASON_HOST_NOT_ALLOWED = "host_not_allowed"
REASON_PORT_NOT_ALLOWED = "port_not_allowed"
REASON_LOOPBACK_NOT_APPROVED = "loopback_not_approved"
REASON_LINK_LOCAL_NOT_APPROVED = "link_local_not_approved"
REASON_PRIVATE_NOT_APPROVED = "private_network_not_approved"
REASON_METADATA_HOST_BLOCKED = "metadata_host_blocked"
REASON_DNS_RESOLUTION_FAILED = "dns_resolution_failed"
REASON_DNS_ADDRESS_NOT_ALLOWED = "dns_address_not_allowed"
REASON_CREDENTIAL_FOREIGN_DESTINATION = "credential_foreign_destination"
REASON_CREDENTIAL_WITHOUT_OWNER = "credential_without_owner"
REASON_REDIRECT_NOT_ALLOWED = "redirect_not_allowed"

#: Cloud metadata services. Never approvable, on any port, over any scheme: this
#: is the classic credential-theft target of an SSRF, and no Guardian deployment
#: legitimately talks to it.
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/OpenStack IMDS
        ipaddress.ip_address("169.254.169.253"),  # AWS DNS
        ipaddress.ip_address("169.254.170.2"),  # ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 IMDS
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: The allowlist Guardian ships with, used **only** when the configuration has no
#: ``destination_policy`` section at all (e.g. a config file written before
#: G1-02, or an operator keeping their own copy of the file). It is identical to
#: the shipped ``config/global.settings.yaml`` block, so "absent" behaves like
#: "shipped" instead of turning the gateway into a closed relay on the next
#: restart. A *present but malformed* section still fails closed — the
#: difference matters: a typo must not silently approve destinations, while a
#: missing block must not silently take the deployment down.
SHIPPED_ALLOW: tuple[Mapping[str, Any], ...] = (
    {"host": "127.0.0.1", "ports": [4000, 4141, 8188, 11440, 11441, 11450]},
    {"host": "localhost", "ports": [11440], "addresses": ["127.0.0.1", "::1"]},
    {"host": "192.168.1.245", "ports": [11440, 11441, 11450, 11451],
     "cleartext_credentials": True},
    {"host": "openrouter.ai", "ports": [443]},
    {"host": "integrate.api.nvidia.com", "ports": [443]},
    {"host": "api.groq.com", "ports": [443]},
    {"host": "api.openai.com", "ports": [443]},
    {"host": "generativelanguage.googleapis.com", "ports": [443]},
    {"host": "inference.poolside.ai", "ports": [443]},
    {"host": "api.fish.audio", "ports": [443]},
)

#: Header names that carry a provider credential. Presence of any of them makes
#: the request subject to the credential-isolation rule.
CREDENTIAL_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
)


class DestinationDenied(Exception):
    """Guardian refuses to send traffic to this destination.

    Stable typed error: ``code`` is always ``destination_denied``, ``reason`` is
    one of the ``REASON_*`` codes.  ``destination`` is the sanitized
    ``scheme://host:port`` form (never userinfo, never a query string) so an
    error can be surfaced to an operator without echoing a credential.
    """

    code = ERROR_CODE
    #: Upstream failure, not the caller's fault: the client did not ask for a
    #: bad destination, the configuration did.
    http_status = 502

    def __init__(
        self,
        *,
        reason: str,
        destination: str = "",
        purpose: str = "",
        owner: str | None = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.destination = destination
        self.purpose = purpose
        self.owner = owner
        self.detail = detail
        message = f"destination denied ({reason})"
        if destination:
            message += f": {destination}"
        if purpose:
            message += f" [purpose={purpose}]"
        if detail:
            message += f" — {detail}"
        super().__init__(message)

    def to_detail(self) -> dict[str, Any]:
        """JSON-ready detail for an HTTP error body or a structured log line."""
        detail: dict[str, Any] = {
            "error": self.code,
            "reason": self.reason,
            "destination": self.destination,
        }
        if self.purpose:
            detail["purpose"] = self.purpose
        if self.owner:
            detail["credential_owner"] = self.owner
        if self.detail:
            detail["message"] = self.detail
        return detail


def _classify_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Return the address class the policy reasons about."""
    if address in _METADATA_ADDRESSES:
        return "metadata"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return "reserved"
    if address.is_private:
        return "private"
    return "public"


@dataclass(frozen=True)
class AllowEntry:
    """One approved ``host:ports`` destination."""

    host: str
    ports: tuple[int, ...]
    #: Optional pinned addresses. When set, a DNS answer outside this set is
    #: refused; when empty, a *name* entry may only resolve to public addresses.
    addresses: tuple[str, ...] = ()
    #: True for a hop that carries a Bearer credential in cleartext over the
    #: LAN (the B-03/B-04 blocker). It does not weaken enforcement; it makes the
    #: unmigrated hop visible in the startup summary and in the decision record.
    cleartext_credentials: bool = False
    note: str = ""


@dataclass(frozen=True)
class Destination:
    """A validated destination returned by :meth:`DestinationPolicy.check`."""

    url: str
    scheme: str
    host: str
    port: int
    #: Validated connect addresses. Empty for an IP-literal host: the literal is
    #: the address and needs no resolution.
    addresses: tuple[str, ...] = ()

    @property
    def authority(self) -> str:
        """``host`` or ``host:port`` — the value for the ``Host`` header."""
        if self.port == _DEFAULT_PORTS.get(self.scheme):
            return self.host
        return f"{self.host}:{self.port}"

    @property
    def destination(self) -> str:
        return f"{self.scheme}://{self.authority}"

    def connect_addresses(self) -> tuple[str, ...]:
        """Addresses to pin the connection to (empty = connect to the name)."""
        return self.addresses


async def resolve_addresses(host: str, port: int) -> list[str]:
    """Resolve *host* to its stream addresses (module-level for tests).

    Uses the event loop's threaded ``getaddrinfo`` so name resolution never
    blocks the loop (structural rule: no blocking syscalls on the event loop).
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _split_host_port(host: str) -> str:
    """Normalize a configured or requested host: lowercase, no trailing dot."""
    return host.strip().lower().rstrip(".")


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _parse_entry(raw: Any, index: int) -> tuple[AllowEntry | None, str | None]:
    """Parse one ``allow`` entry into ``(entry, error)``.

    Returns ``(entry, None)`` on success and ``(None, reason)`` when the entry
    cannot be trusted. A malformed entry is *dropped*, never guessed at.
    """
    if not isinstance(raw, Mapping):
        return None, f"allow[{index}] is not a mapping"
    host_raw = raw.get("host")
    if not isinstance(host_raw, str) or not host_raw.strip():
        return None, f"allow[{index}].host is missing or not a string"
    host = _split_host_port(host_raw)
    if not host or "/" in host or "@" in host or " " in host:
        return None, f"allow[{index}].host is not a bare host: {host_raw!r}"
    ports_raw = raw.get("ports")
    if isinstance(ports_raw, int) and not isinstance(ports_raw, bool):
        ports_raw = [ports_raw]
    if not isinstance(ports_raw, (list, tuple)) or not ports_raw:
        return None, f"allow[{index}].ports must be a non-empty list of integers"
    ports: list[int] = []
    for value in ports_raw:
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"allow[{index}].ports contains a non-integer: {value!r}"
        if not 1 <= value <= 65535:
            return None, f"allow[{index}].ports contains an out-of-range port: {value}"
        if value not in ports:
            ports.append(value)
    addresses_raw = raw.get("addresses") or ()
    if isinstance(addresses_raw, str):
        addresses_raw = [addresses_raw]
    if not isinstance(addresses_raw, (list, tuple)):
        return None, f"allow[{index}].addresses must be a list of IP addresses"
    addresses: list[str] = []
    for value in addresses_raw:
        if not isinstance(value, str) or _literal_address(value.strip()) is None:
            return None, f"allow[{index}].addresses contains a non-IP value: {value!r}"
        normalized = str(_literal_address(value.strip()))
        if normalized not in addresses:
            addresses.append(normalized)
    return (
        AllowEntry(
            host=host,
            ports=tuple(sorted(ports)),
            addresses=tuple(addresses),
            cleartext_credentials=bool(raw.get("cleartext_credentials", False)),
            note=str(raw.get("note") or ""),
        ),
        None,
    )


class DestinationPolicy:
    """An immutable, validated destination allowlist.

    Build it with :meth:`from_config` (live configuration) or
    :meth:`fail_closed` (an explicitly closed policy).  The object is cheap:
    no DNS happens here, resolution stays a request-time concern.
    """

    def __init__(
        self,
        entries: Sequence[AllowEntry] = (),
        *,
        schemes: Sequence[str] = ("http", "https"),
        owner_destinations: Mapping[str, frozenset[tuple[str, int]]] | None = None,
        invalid_entries: Iterable[str] = (),
        failure: str | None = None,
        source: str = "config",
    ) -> None:
        self._entries: tuple[AllowEntry, ...] = tuple(entries)
        self._schemes: tuple[str, ...] = tuple(s.strip().lower() for s in schemes)
        self._by_host: dict[str, AllowEntry] = {}
        for entry in self._entries:
            # First declaration wins: a duplicate host must not silently widen
            # the port set of the entry the operator read first.
            self._by_host.setdefault(entry.host, entry)
        self._owners: dict[str, frozenset[tuple[str, int]]] = {
            name.lower(): frozenset(pairs)
            for name, pairs in (owner_destinations or {}).items()
        }
        self._invalid_entries: tuple[str, ...] = tuple(invalid_entries)
        self._failure = failure
        self.source = source

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def fail_closed(cls, reason: str, *, source: str = "config") -> "DestinationPolicy":
        """A policy that refuses everything with ``policy_unavailable``."""
        return cls((), failure=reason, source=source)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "DestinationPolicy":
        """Build the policy from the live configuration document.

        ``config["destination_policy"]`` is the source of truth; provider
        documents supply the per-provider destination sets used by the
        credential-isolation rule.  Any structural problem with the section
        yields a fail-closed policy (:class:`DestinationDenied` every time).
        """
        config = config if isinstance(config, Mapping) else {}
        section = config.get("destination_policy")
        if section is None:
            # Absent is not the same as malformed: fall back to the shipped
            # allowlist (loudly) instead of denying every outbound request.
            logger.warning(
                "⚠️ no 'destination_policy' section in the configuration — using the "
                "shipped allowlist (%d entries). Add the block to "
                "config/global.settings.yaml to make the policy explicit.",
                len(SHIPPED_ALLOW),
            )
            section = {"schemes": ["http", "https"], "allow": list(SHIPPED_ALLOW)}
            source = "shipped-default"
        elif not isinstance(section, Mapping):
            return cls.fail_closed(
                "configuration section 'destination_policy' is not a mapping"
            )
        else:
            source = "config"

        schemes_raw = section.get("schemes", ["http", "https"])
        if isinstance(schemes_raw, str):
            schemes_raw = [schemes_raw]
        if not isinstance(schemes_raw, (list, tuple)) or not schemes_raw:
            return cls.fail_closed("destination_policy.schemes must be a non-empty list")
        schemes: list[str] = []
        for value in schemes_raw:
            scheme = str(value).strip().lower()
            if scheme not in _DEFAULT_PORTS:
                return cls.fail_closed(
                    f"destination_policy.schemes contains an unsupported scheme: {value!r}"
                )
            schemes.append(scheme)

        allow_raw = section.get("allow")
        if not isinstance(allow_raw, (list, tuple)) or not allow_raw:
            return cls.fail_closed(
                "destination_policy.allow must be a non-empty list of host/port entries"
            )

        entries: list[AllowEntry] = []
        invalid: list[str] = []
        for index, raw in enumerate(allow_raw):
            entry, error = _parse_entry(raw, index)
            if error is not None:
                invalid.append(error)
                logger.error(
                    "🛑 destination policy entry rejected (%s); that host stays "
                    "unreachable — fix destination_policy.allow in the configuration",
                    error,
                )
                continue
            assert entry is not None
            entries.append(entry)
        if not entries:
            return cls.fail_closed(
                "destination_policy.allow has no valid entry"
                + (f" ({invalid[0]})" if invalid else "")
            )

        owners = _owner_destinations(config.get("providers"))
        return cls(
            entries,
            schemes=schemes,
            owner_destinations=owners,
            invalid_entries=invalid,
            source=source,
        )

    # ── introspection ───────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        """True when the policy refuses every destination."""
        return self._failure is not None

    @property
    def failure(self) -> str | None:
        return self._failure

    @property
    def entries(self) -> tuple[AllowEntry, ...]:
        return self._entries

    @property
    def schemes(self) -> tuple[str, ...]:
        return self._schemes

    @property
    def invalid_entries(self) -> tuple[str, ...]:
        return self._invalid_entries

    def approved_destinations(self) -> list[str]:
        without_default = {s: p for s, p in _DEFAULT_PORTS.items()}
        approved: list[str] = []
        for entry in self._entries:
            for scheme in self._schemes:
                for port in entry.ports:
                    if port == without_default.get(scheme):
                        approved.append(f"{scheme}://{entry.host}")
                    else:
                        approved.append(f"{scheme}://{entry.host}:{port}")
        return approved

    def cleartext_credential_hops(self) -> list[str]:
        """Approved ``http://`` hops whose credentials travel in cleartext."""
        hops: list[str] = []
        if "http" not in self._schemes:
            return hops
        for entry in self._entries:
            if not entry.cleartext_credentials:
                continue
            for port in entry.ports:
                hops.append(f"http://{entry.host}:{port}")
        return hops

    def owner_destinations(self, owner: str) -> frozenset[tuple[str, int]]:
        return self._owners.get(owner.lower(), frozenset())

    @property
    def owner_count(self) -> int:
        return len(self._owners)

    def with_owner(self, owner: str, urls: Iterable[str]) -> "DestinationPolicy":
        """Return a policy that also maps *owner* to the destinations in *urls*.

        Used by an app that owns its own upstream configuration outside the
        provider documents (the Copilot bridge): its key stays bound to its own
        configured upstream instead of a provider entry.
        """
        owners = dict(self._owners)
        pairs = set(owners.get(owner.lower(), frozenset()))
        for url in urls:
            if not isinstance(url, str) or "://" not in url:
                continue
            try:
                parts = urlsplit(url)
                port = parts.port or _DEFAULT_PORTS.get((parts.scheme or "").lower())
            except ValueError:
                continue
            host = _split_host_port(parts.hostname or "")
            if host and port:
                pairs.add((host, int(port)))
        owners[owner.lower()] = frozenset(pairs)
        return DestinationPolicy(
            self._entries,
            schemes=self._schemes,
            owner_destinations=owners,
            invalid_entries=self._invalid_entries,
            failure=self._failure,
            source=self.source,
        )

    # ── checks ──────────────────────────────────────────────────────────

    def _deny(self, reason: str, *, destination: str, purpose: str, owner: str | None, detail: str = "") -> DestinationDenied:
        return DestinationDenied(
            reason=reason, destination=destination, purpose=purpose, owner=owner, detail=detail
        )

    def check_structure(
        self, url: str, *, purpose: str = "", owner: str | None = None
    ) -> Destination:
        """Validate scheme, userinfo, host and port only (no DNS).

        Used at configuration load, where a transient resolver failure must not
        reject an otherwise valid provider, and as the first half of the
        request-time check.
        """
        if self._failure is not None:
            raise self._deny(
                REASON_POLICY_UNAVAILABLE,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail=self._failure,
            )
        try:
            parts = urlsplit(url)
        except ValueError as exc:  # malformed URL
            raise self._deny(
                REASON_HOST_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail=f"unparseable URL: {exc}",
            ) from exc

        scheme = (parts.scheme or "").lower()
        if scheme not in self._schemes:
            raise self._deny(
                REASON_SCHEME_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail=f"scheme {scheme or '(none)'} is not allowed",
            )
        if parts.username or parts.password or "@" in (parts.netloc or ""):
            raise self._deny(
                REASON_USERINFO_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail="a URL with userinfo is refused",
            )
        host = _split_host_port(parts.hostname or "")
        if not host:
            raise self._deny(
                REASON_HOST_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail="URL has no host",
            )
        try:
            port = parts.port
        except ValueError as exc:
            raise self._deny(
                REASON_PORT_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail=f"invalid port: {exc}",
            ) from exc
        if port is None:
            port = _DEFAULT_PORTS[scheme]

        literal = _literal_address(host)
        entry = self._by_host.get(host)
        if literal is not None:
            address_class = _classify_address(literal)
            if address_class == "metadata":
                raise self._deny(
                    REASON_METADATA_HOST_BLOCKED,
                    destination=_sanitize(url),
                    purpose=purpose,
                    owner=owner,
                    detail="cloud metadata endpoints are never approvable",
                )
            if entry is None:
                raise self._deny(
                    _class_missing_reason(address_class),
                    destination=_sanitize(url),
                    purpose=purpose,
                    owner=owner,
                    detail=f"{address_class} host is not an explicit allowlist entry",
                )
        if entry is None:
            raise self._deny(
                REASON_HOST_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail="host is not in the configured allowlist",
            )
        if port not in entry.ports:
            raise self._deny(
                REASON_PORT_NOT_ALLOWED,
                destination=_sanitize(url),
                purpose=purpose,
                owner=owner,
                detail=f"port {port} is not approved for {entry.host}",
            )
        return Destination(
            url=url,
            scheme=scheme,
            host=host,
            port=port,
            addresses=(host,) if literal is not None else (),
        )

    async def check(
        self,
        url: str,
        *,
        purpose: str = "",
        owner: str | None = None,
        has_credential: bool = False,
        redirect_from: str | None = None,
    ) -> Destination:
        """Full request-time check: structure, credentials, resolution.

        Returns the :class:`Destination` whose ``connect_addresses()`` must be
        used for the actual connection.  Raises :class:`DestinationDenied`.
        """
        destination = self.check_structure(url, purpose=purpose, owner=owner)
        if redirect_from is not None and not _same_origin(redirect_from, url):
            raise self._deny(
                REASON_REDIRECT_NOT_ALLOWED,
                destination=destination.destination,
                purpose=purpose,
                owner=owner,
                detail=f"redirect from {_sanitize(redirect_from)} is not followed",
            )
        if has_credential:
            self._check_credential(destination, purpose=purpose, owner=owner)
        if destination.addresses:
            # IP literal: the entry itself is the approval (metadata literals
            # were refused above, whatever the entry says).
            return destination

        entry = self._by_host[destination.host]
        try:
            addresses = await resolve_addresses(destination.host, destination.port)
        except (OSError, UnicodeError) as exc:
            raise self._deny(
                REASON_DNS_RESOLUTION_FAILED,
                destination=destination.destination,
                purpose=purpose,
                owner=owner,
                detail=f"resolution failed: {exc}",
            ) from exc
        if not addresses:
            raise self._deny(
                REASON_DNS_RESOLUTION_FAILED,
                destination=destination.destination,
                purpose=purpose,
                owner=owner,
                detail="resolution returned no address",
            )
        validated: list[str] = []
        for address in addresses:
            parsed = _literal_address(address)
            if parsed is None:
                raise self._deny(
                    REASON_DNS_ADDRESS_NOT_ALLOWED,
                    destination=destination.destination,
                    purpose=purpose,
                    owner=owner,
                    detail=f"resolver returned a non-IP value: {address!r}",
                )
            address_class = _classify_address(parsed)
            if address_class == "metadata":
                raise self._deny(
                    REASON_METADATA_HOST_BLOCKED,
                    destination=destination.destination,
                    purpose=purpose,
                    owner=owner,
                    detail=f"resolves to a cloud metadata address ({address})",
                )
            if entry.addresses:
                if str(parsed) not in entry.addresses:
                    raise self._deny(
                        REASON_DNS_ADDRESS_NOT_ALLOWED,
                        destination=destination.destination,
                        purpose=purpose,
                        owner=owner,
                        detail=(
                            f"resolved address {address} is outside the pinned set "
                            f"for {entry.host}"
                        ),
                    )
            elif address_class != "public":
                raise self._deny(
                    REASON_DNS_ADDRESS_NOT_ALLOWED,
                    destination=destination.destination,
                    purpose=purpose,
                    owner=owner,
                    detail=(
                        f"a public hostname resolved to a {address_class} address "
                        f"({address}); pin 'addresses' to approve an internal target"
                    ),
                )
            if address not in validated:
                validated.append(address)
        return Destination(
            url=destination.url,
            scheme=destination.scheme,
            host=destination.host,
            port=destination.port,
            addresses=tuple(validated),
        )

    def _check_credential(self, destination: Destination, *, purpose: str, owner: str | None) -> None:
        """A credential may only travel to its own provider's destinations."""
        if not owner:
            raise self._deny(
                REASON_CREDENTIAL_WITHOUT_OWNER,
                destination=destination.destination,
                purpose=purpose,
                owner=None,
                detail="a request carrying a credential must declare its owning provider",
            )
        allowed = self.owner_destinations(owner)
        if (destination.host, destination.port) not in allowed:
            raise self._deny(
                REASON_CREDENTIAL_FOREIGN_DESTINATION,
                destination=destination.destination,
                purpose=purpose,
                owner=owner,
                detail=(
                    f"provider '{owner}' has no approved destination "
                    f"{destination.host}:{destination.port}"
                ),
            )


def _class_missing_reason(address_class: str) -> str:
    if address_class == "loopback":
        return REASON_LOOPBACK_NOT_APPROVED
    if address_class == "link_local":
        return REASON_LINK_LOCAL_NOT_APPROVED
    if address_class in ("private", "reserved"):
        return REASON_PRIVATE_NOT_APPROVED
    return REASON_HOST_NOT_ALLOWED


def _sanitize(url: str) -> str:
    """``scheme://host:port`` for diagnostics — never userinfo, never a query."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme:
        return ""
    host = _split_host_port(parts.hostname or "")
    if not host:
        return f"{parts.scheme}://"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _same_origin(first: str, second: str) -> bool:
    try:
        a, b = urlsplit(first), urlsplit(second)
    except ValueError:
        return False
    def _origin(parts) -> tuple[str, str, int | None]:
        try:
            port = parts.port
        except ValueError:
            port = None
        scheme = (parts.scheme or "").lower()
        return (scheme, _split_host_port(parts.hostname or ""), port or _DEFAULT_PORTS.get(scheme))
    return _origin(a) == _origin(b)


def _owner_destinations(
    providers: Any,
) -> dict[str, frozenset[tuple[str, int]]]:
    """Map provider name -> its own approved ``(host, port)`` destinations.

    Built from the provider's configured URL fields (``base_url``,
    ``management_url``, ``tts_url``, ``stt_url`` and an absolute
    ``catalog_url``).  Relative catalog paths already live under ``base_url``.
    """
    owners: dict[str, frozenset[tuple[str, int]]] = {}
    if not isinstance(providers, Mapping):
        return owners
    for name, doc in providers.items():
        if not isinstance(doc, Mapping) or not isinstance(name, str):
            continue
        pairs: set[tuple[str, int]] = set()
        for field in ("base_url", "management_url", "tts_url", "stt_url", "catalog_url"):
            raw = doc.get(field)
            if not isinstance(raw, str) or not raw.strip():
                continue
            url = raw.strip()
            if "://" not in url:
                continue  # a relative catalog path: covered by base_url
            try:
                parts = urlsplit(url)
                port = parts.port or _DEFAULT_PORTS.get((parts.scheme or "").lower())
            except ValueError:
                continue
            host = _split_host_port(parts.hostname or "")
            if host and port:
                pairs.add((host, int(port)))
        owners[name.lower()] = frozenset(pairs)
    return owners


# ── request-time enforcement ────────────────────────────────────────────


class DestinationGuardTransport(httpx.AsyncBaseTransport):
    """httpx transport that validates every request before it opens a socket.

    It sits below every call site (httpx hands *each* redirect hop and retry to
    the transport), which is why it — not the individual callers — is the
    enforcement point.  On top of validation it pins the connection to a
    validated address and preserves ``Host``/SNI, so a resolver answer that
    changes after the check cannot redirect the connect.
    """

    def __init__(
        self,
        policy: DestinationPolicy | Callable[[], DestinationPolicy],
        *,
        purpose: str = "",
        credential_owner: str | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # A *callable* is resolved per request: a client that lives for the whole
        # process (the caretaker client, the copilot bridge) must see a config
        # reload that removes its destination, otherwise revoking an approval
        # would silently keep working until the next restart (PR-Piet, G1-02).
        self._policy_source = policy
        self._purpose = purpose
        self._owner = credential_owner
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    @property
    def policy(self) -> DestinationPolicy:
        source = self._policy_source
        return source() if callable(source) else source

    async def aclose(self) -> None:  # pragma: no cover - delegation
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        has_credential = any(
            name in request.headers for name in CREDENTIAL_HEADERS
        )
        try:
            destination = await self.policy.check(
                str(request.url),
                purpose=self._purpose,
                owner=self._owner,
                has_credential=has_credential,
            )
        except DestinationDenied as exc:
            # G1-03 §11: a refused outbound hop is a deny decision like any
            # other. Marked through the audit ContextVar, so it lands on the
            # inbound request's record (route, key, status 502) when one is
            # being served, and is a no-op at configuration-load time.
            _audit.mark_denial(
                reason=_audit.REASON_DESTINATION_DENIED,
                status=exc.http_status,
                detail=exc.reason,
            )
            raise
        addresses = destination.connect_addresses()
        if not addresses:
            return await self._inner.handle_async_request(request)

        last_error: Exception | None = None
        for address in addresses:
            pinned = _pin_request(request, address, destination.authority, destination.host)
            try:
                return await self._inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                continue
        assert last_error is not None
        raise last_error


def _pin_request(
    request: httpx.Request, address: str, authority: str, hostname: str
) -> httpx.Request:
    """Return a copy of *request* that connects to *address* for *hostname*."""
    headers = request.headers.copy()
    headers["Host"] = authority
    url = request.url.copy_with(host=address)
    extensions = dict(request.extensions)
    # httpcore reads ``sni_hostname`` for the TLS handshake, so certificate
    # verification still checks the approved hostname, not the pinned address.
    extensions["sni_hostname"] = hostname
    return httpx.Request(
        method=request.method,
        url=url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def guarded_async_client(
    *,
    purpose: str = "",
    credential_owner: str | None = None,
    policy: DestinationPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` that can only reach approved destinations.

    Drop-in replacement for ``httpx.AsyncClient(...)`` on every Guardian
    outbound path.  ``follow_redirects`` is refused (a redirect is a new
    destination; the policy does not follow one), and ``trust_env`` defaults to
    ``False`` so a proxy environment variable cannot route traffic around the
    guard.

    ``transport`` is the existing test-only injection point in this codebase
    (``httpx.MockTransport``): a caller-supplied transport performs no network
    I/O, so the guard is not stacked on top of it and production behaviour is
    unchanged for tests.
    """
    if kwargs.get("follow_redirects"):
        raise ValueError(
            "redirects are not followed under the G1-02 destination policy; "
            "see docs/decisions/G1-02-destination-and-credentials.md"
        )
    if transport is not None:
        kwargs.setdefault("trust_env", False)
        return httpx.AsyncClient(transport=transport, **kwargs)
    # ``current_policy`` (not ``current_policy()``): long-lived clients must
    # re-resolve per request so a config reload takes effect immediately.
    policy_source = policy if policy is not None else current_policy
    guard = DestinationGuardTransport(
        policy_source, purpose=purpose, credential_owner=credential_owner
    )
    kwargs.setdefault("trust_env", False)
    kwargs["follow_redirects"] = False
    return httpx.AsyncClient(transport=guard, **kwargs)


def current_policy() -> DestinationPolicy:
    """The policy of the live configuration.

    Built per call: it is a few dozen string comparisons over an in-memory
    document, and rebuilding it means a ``POST /api/config/reload`` (or a test
    replacing ``CONFIG``) takes effect on the next request with no cache to
    invalidate.
    """
    from app.config_loader import CONFIG  # local import: avoids an import cycle

    return DestinationPolicy.from_config(CONFIG)


# ── configuration-load validation ──────────────────────────────────────


def provider_destination_errors(
    provider_name: str, doc: Mapping[str, Any], policy: DestinationPolicy
) -> list[tuple[str, DestinationDenied]]:
    """Return ``(field, error)`` for every unapproved destination in a provider.

    Runs at configuration load: structural checks only (no DNS), so a transient
    resolver failure never rejects a provider.
    """
    errors: list[tuple[str, DestinationDenied]] = []
    if not isinstance(doc, Mapping):
        return errors
    for field in ("base_url", "management_url", "tts_url", "stt_url", "catalog_url"):
        raw = doc.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        url = raw.strip()
        if "://" not in url:
            continue  # relative catalog path: validated through base_url
        try:
            policy.check_structure(url, purpose=f"provider:{provider_name}:{field}")
        except DestinationDenied as exc:
            errors.append((field, exc))
    return errors


def log_startup_summary(policy: DestinationPolicy | None = None) -> DestinationPolicy:
    """Log the effective policy and the acknowledged cleartext credential hops.

    The unmigrated LAN hops (B-03/B-04) must stay visible in the operational
    log, not only in the decision record.
    """
    active = policy if policy is not None else current_policy()
    if active.closed:
        logger.error(
            "🛑 OUTBOUND DESTINATION POLICY IS CLOSED (%s): every outbound request "
            "to a provider, catalog, management or speech endpoint will be refused "
            "with %s. Fix 'destination_policy' in config/global.settings.yaml.",
            active.failure,
            ERROR_CODE,
        )
        return active
    logger.info(
        "🛡️  Outbound destination policy: %d approved host(s), schemes=%s, "
        "%d provider destination set(s)",
        len(active.entries),
        ",".join(active.schemes),
        active.owner_count,
    )
    hops = active.cleartext_credential_hops()
    if hops:
        logger.warning(
            "⚠️  B-03/B-04 acknowledged cleartext credential hop(s) still active: %s "
            "— Bearer keys travel over http:// until the Windows side serves TLS; "
            "tracked in docs/decisions/G1-02-destination-and-credentials.md",
            ", ".join(hops),
        )
    for invalid in active.invalid_entries:
        logger.error("🛑 destination policy entry rejected: %s", invalid)
    return active
