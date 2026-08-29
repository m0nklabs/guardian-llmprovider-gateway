"""Caretaker control-API client.

F5 (GATEWAY_MANAGER_SPLIT): the local llama-server lifecycle *execution*
(load/switch/unload) moves to the caretaker daemon exposed at
``{management_url}`` (http://127.0.0.1:11441), authenticated with a Bearer
``CARETAKER_KEY``.  The gateway keeps the *decisions* (idle-unload, switch
permission, connect-error retry-once) and only talks to the caretaker over
HTTP.

This module is a thin, dependency-light HTTP client for the three caretaker
endpoints (m0nklabs/caretaker-llamacpp contract):

- ``POST {management_url}/ensure``  body ``{model, enable_vision?, context_hint?}``
- ``POST {management_url}/unload``
- ``GET  {management_url}/status``

All three require ``Authorization: Bearer <CARETAKER_KEY>`` when a key is set;
``api_key=None`` clients send no header and log a warning on construction.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from typing import Any, Self

import httpx

logger = logging.getLogger("Guardian")

# Matches ``${ENV_VAR}`` in config strings (mirrors app/proxy/providers._expand_env
# so this module stays free of proxy-service imports and thus of import cycles).
_ENV_VAR_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")

CONTEXT_HINT_FALLBACK_CTX = 131072


class CaretakerError(Exception):
    """Base class for all caretaker control-API failures."""


class CaretakerUnavailable(CaretakerError):
    """Transport/timeout failure reaching the caretaker daemon."""

    def __init__(self, management_url: str) -> None:
        super().__init__(f"Caretaker unreachable at {management_url}")
        self.management_url = management_url


class CaretakerModelNotFound(CaretakerError):
    """Caretaker returned 404 model_not_found for the requested model."""

    def __init__(self, model: str) -> None:
        super().__init__(f"Caretaker does not know model '{model}'")
        self.model = model


class CaretakerModelLoadFailed(CaretakerError):
    """Caretaker returned 503 model_load_failed (optionally with crash details)."""

    def __init__(self, model: str, crash_details: Any | None = None) -> None:
        detail = "" if crash_details is None else f" — crash_details={crash_details}"
        super().__init__(f"Caretaker failed to load model '{model}'{detail}")
        self.model = model
        self.crash_details = crash_details


class CaretakerVramExceeded(CaretakerError):
    """Caretaker returned 503 vram_limit_exceeded for the requested model."""

    def __init__(self, model: str) -> None:
        super().__init__(f"Caretaker refused model '{model}': VRAM limit exceeded")
        self.model = model


class CaretakerInvalidRequest(CaretakerError):
    """Caretaker returned 422 invalid_request for a malformed ensure payload."""

    def __init__(self, message: str = "Caretaker rejected the request (422 invalid_request)") -> None:
        super().__init__(message)


class CaretakerUnloadFailed(CaretakerError):
    """Caretaker returned 500 unload_failed during an unload."""

    def __init__(self, message: str = "Caretaker failed to unload (500 unload_failed)") -> None:
        super().__init__(message)


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` references in a string using process environment.

    Unknown variables are replaced with an empty string so misconfiguration
    fails loudly at request time rather than leaking the literal placeholder
    (mirrors ``app/proxy/providers._expand_env`` semantics).
    """

    def _replace(match: re.Match) -> str:
        return os.environ.get(match.group("name"), "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


class CaretakerClient:
    """Async HTTP client for the caretaker control-API (management_url).

    None of the methods are request-hotpath (the blueprint keeps those for the
    real manager in tranche 2); this client is used for background idle-unload
    and the admin routes.
    """

    def __init__(
        self,
        management_url: str,
        api_key: str | None = None,
        timeout: float = 5.0,
        logger: logging.Logger | None = None,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._management_url = management_url.rstrip("/")
        self._api_key = api_key or None
        self._timeout = timeout
        self._log = logger or logging.getLogger("Guardian.CaretakerClient")
        # _transport is an internal override used only by tests (MockTransport);
        # it is not part of the public constructor contract.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            transport=_transport,
        )
        if self._api_key is None:
            self._log.warning(
                "Caretaker api_key is not set; requests to %s will be sent without "
                "an Authorization header (set CARETAKER_KEY in the gateway env/config).",
                self._management_url,
            )

    @property
    def management_url(self) -> str:
        return self._management_url

    def _headers(self) -> dict:
        headers: dict = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def ensure(
        self,
        model: str,
        *,
        enable_vision: bool | None = None,
        context_hint: int | None = None,
    ) -> dict:
        """POST /ensure — idempotently load (or switch to) ``model``.

        Success returns the caretaker response dict (``{ok, loaded_model, ...}``).
        HTTP error bodies are parsed into the specific :class:`CaretakerError`
        subclasses; transport/timeout failures become :class:`CaretakerUnavailable`.
        """
        payload: dict[str, Any] = {"model": model}
        if enable_vision is not None:
            payload["enable_vision"] = bool(enable_vision)
        if context_hint is not None:
            payload["context_hint"] = int(context_hint)
        try:
            resp = await self._client.post(
                f"{self._management_url}/ensure",
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            logger.error("Caretaker /ensure transport error: %s", exc)
            raise CaretakerUnavailable(self._management_url) from exc

        if resp.status_code == 200:
            return await self._ok_json(resp, "/ensure")

        body = _safe_json(resp)
        if resp.status_code == 404:
            raise CaretakerModelNotFound(model)
        if resp.status_code == 503:
            # The caretaker error body is top-level:
            #   {"error": "model_load_failed"|"vram_limit_exceeded",
            #    "message": ..., "crash_details": ...}
            error_code = body.get("error")
            if error_code == "vram_limit_exceeded":
                raise CaretakerVramExceeded(model)
            crash_details = body.get("crash_details") if isinstance(body, dict) else None
            raise CaretakerModelLoadFailed(model, crash_details)
        if resp.status_code == 422:
            raise CaretakerInvalidRequest()
        # Unexpected status → treat as unavailable so the gateway never claims
        # success from an unknown caretaker response.
        logger.error(
            "Caretaker /ensure unexpected status %s: %s", resp.status_code, _safe_body_text(resp)
        )
        raise CaretakerUnavailable(self._management_url)

    async def unload(self) -> dict:
        """POST /unload — idempotent-safe unload.

        ``200`` is always ok (including the caretaker-side "already unloaded"
        no-op second call).  ``500 unload_failed`` raises :class:`CaretakerUnloadFailed`.
        """
        try:
            resp = await self._client.post(
                f"{self._management_url}/unload",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            logger.error("Caretaker /unload transport error: %s", exc)
            raise CaretakerUnavailable(self._management_url) from exc

        if resp.status_code == 200:
            return await self._ok_json(resp, "/unload")
        if resp.status_code in (401, 403, 404):
            # The daemon rejected our credentials (missing/mismatched
            # CARETAKER_KEY — e.g. multi-host/non-loopback where the daemon
            # requires Bearer auth) or the endpoint is missing (older daemon /
            # wrong management_url path).  The unload was NOT processed.
            # Classify as availability (not a definitive refusal) so callers
            # fall back to the idempotent local unload — otherwise a key
            # misconfiguration or deployment mismatch silently disables VRAM
            # freeing (review: possible bug).
            logger.error(
                "Caretaker /unload not processed (status %s): %s — check CARETAKER_KEY/management_url",
                resp.status_code,
                _safe_body_text(resp),
            )
            raise CaretakerUnavailable(self._management_url)
        logger.error("Caretaker /unload failed (status %s): %s", resp.status_code, _safe_body_text(resp))
        raise CaretakerUnloadFailed()

    async def status(self) -> dict:
        """GET /status — return the caretaker runtime status dict."""
        try:
            resp = await self._client.get(
                f"{self._management_url}/status",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            logger.error("Caretaker /status transport error: %s", exc)
            raise CaretakerUnavailable(self._management_url) from exc
        if resp.status_code != 200:
            logger.error("Caretaker /status failed (status %s): %s", resp.status_code, _safe_body_text(resp))
            raise CaretakerUnavailable(self._management_url)
        return await self._ok_json(resp, "/status")

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _ok_json(self, resp: httpx.Response, endpoint: str) -> dict:
        """Parse a 200 response defensively.

        A caretaker 200 whose body is not a JSON dict (empty body, or an
        HTML/error page returned by an intermediary) must not surface as
        ``ValueError`` — callers only catch :class:`CaretakerError`, so this
        would leak as an HTTP 500 instead of the intended 503 mapping.  Any
        non-dict body is treated as :class:`CaretakerUnavailable`.
        """
        try:
            value = resp.json()
        except ValueError:
            logger.error("Caretaker %s returned non-JSON 200 body", endpoint)
            raise CaretakerUnavailable(self._management_url) from None
        if not isinstance(value, dict):
            logger.error("Caretaker %s returned non-dict 200 body: %r", endpoint, value)
            raise CaretakerUnavailable(self._management_url)
        return value


def _safe_json(resp: httpx.Response) -> dict:
    try:
        value = resp.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _safe_body_text(resp: httpx.Response) -> str:
    try:
        return (resp.text or "")[:300]
    except Exception:  # noqa: BLE001 - defensive: never fail on body reading
        return ""


def build_caretaker_client(config: dict) -> CaretakerClient:
    """Build a :class:`CaretakerClient` from the gateway provider config.

    Resolution order:
    - ``management_url``: from the local provider document
      (``config/providers/ai-kvm2-local.settings.yaml``; ``CONFIG["providers"][...]``),
      ``${VAR}``-expanded.  Mandatory — a missing value raises ``ValueError``.
    - ``api_key``: ``CARETAKER_KEY`` env-only (never from YAML).  ``None`` when
      absent (the client then sends no Authorization header and logs a warning).

    The key must come from the gateway's own secret source (``.env`` loaded
    early by ``app.main.load_dotenv``); it is never committed to
    ``config/providers/*.settings.yaml`` — those are tracked and a Bearer key
    read from them would leak over the LAN in cleartext (review: sensitive
    key over cleartext).  A non-loopback ``management_url`` (multi-host F5)
    therefore also requires ``CARETAKER_KEY`` to be set explicitly; without it
    the client simply sends no Authorization header and the caretaker 401s.
    """
    providers = config.get("providers") or {}
    local_doc = None
    local_name = None

    def _is_local(name: str, doc: dict) -> bool:
        return name.endswith("-local") or bool(doc.get("local"))

    def _is_this_host(mgmt: str) -> bool:
        """True when the management_url binds THIS host: loopback, or the
        host's own hostname / resolved LAN IP (F5 deploys reach the local
        caretaker via e.g. http://192.168.1.35:11441, which is NOT loopback).
        This prevents dict order from sending lifecycle commands (/unload) to
        a different GPU host's caretaker once a second local provider exists."""
        if mgmt.startswith(
            ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")
        ):
            return True
        host = mgmt.split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0].lower()
        if not host:
            return False
        try:
            hostname, aliases, ips = socket.gethostbyname_ex(socket.gethostname())
        except OSError:
            return False
        own = {hostname.lower(), *(a.lower() for a in aliases), *(ip.lower() for ip in ips)}
        return host in own

    # Pass 1: prefer a local provider whose management_url binds THIS host —
    # this is the gateway's own caretaker (ai-kvm-2 talks to its own daemon,
    # never to a Windows/remote GPU host, for lifecycle execution).  Loopback
    # OR this host's hostname/LAN-IP.  Dict ordering must not decide which
    # caretaker the gateway unloads (F5 multi-host: ai-kvm2-local vs a future
    # 14700k-local).
    for name, doc in providers.items():
        doc = doc or {}
        if not _is_local(name, doc):
            continue
        mgmt = _expand_env(str(doc.get("management_url", "")))
        if _is_this_host(mgmt):
            local_doc, local_name = doc, name
            break
    # Pass 2 (single-provider fallback): when none of the local providers binds
    # THIS host (e.g. /etc/hosts maps the hostname to 127.0.1.1 instead of the
    # LAN IP), a single local provider is unambiguous.  But with MULTIPLE local
    # providers none binding this host, dict order must never decide which GPU
    # host's caretaker receives lifecycle commands (and possibly CARETAKER_KEY)
    # — fail closed instead (review: possible issue).
    if local_doc is None:
        local_candidates = [
            (name, doc or {}) for name, doc in providers.items() if _is_local(name, doc or {})
        ]
        if len(local_candidates) == 1:
            local_name, local_doc = local_candidates[0]
        elif len(local_candidates) > 1:
            raise ValueError(
                "Multiple local providers configured and none binds this host; "
                "refusing to pick one by dict order (check management_url and "
                "host/IP resolution)."
            )
    if local_name is not None:
        logger.info(
            "Caretaker client uses local provider %s (%s)",
            local_name,
            _expand_env(str((local_doc or {}).get("management_url", ""))),
        )

    management_url = _expand_env(str((local_doc or {}).get("management_url") or "")) if local_doc else ""
    if not management_url:
        raise ValueError(
            "management_url is missing for the local provider; add it to "
            "config/providers/*-local.settings.yaml so the gateway can reach the caretaker."
        )

    # Secret resolution: env-only (CARETAKER_KEY).  No YAML fallback — a
    # caretaker_key in a tracked provider file would leak the Bearer secret
    # over the LAN and violate the secrets rule (review: sensitive key over
    # cleartext).
    api_key = _expand_env(os.environ.get("CARETAKER_KEY", "")).strip() or None

    # Fail closed (review: sensitive key): never send the Bearer secret over
    # cleartext HTTP to a foreign host (F5 multi-host LAN addresses like
    # http://192.168.1.x:11441).  THIS host's caretaker — loopback or the own
    # hostname/LAN-IP (per _is_this_host) — stays allowed; anything else with a
    # key configured raises instead of leaking the key on the wire.
    if api_key and management_url.lower().startswith("http://") and not _is_this_host(management_url):
        raise ValueError(
            "Refusing to send CARETAKER_KEY over cleartext http to non-loopback "
            f"management_url {management_url}; use https:// or a loopback address."
        )

    return CaretakerClient(management_url=management_url, api_key=api_key)