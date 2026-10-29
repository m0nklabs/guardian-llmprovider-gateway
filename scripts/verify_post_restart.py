#!/usr/bin/env python3
"""Post-restart verification for Guardian — the current deployment contract.

**When to run it.** Immediately after
``sudo systemctl restart guardian-llmprovider-gateway``, and after every push
that is supposed to be live.  ``scripts/pre_restart_check.py`` proves the code
is *safe to deploy*; this script proves the running process *is* that code.  A
service that is merely ``active`` can still be an old process, so every check
below reads a fact that only the merged code produces.

**What it verifies** (one ``[PASS]``/``[FAIL]``/``[SKIP]`` line per item):

=================================  ==============================================
check                              what it reads
=================================  ==============================================
1  ``service_identity``            ``systemctl show``: active + ``MainPID``, and the
                                   process that owns the listener on the probe
                                   address (``/proc``/``ss``), so a *stale*
                                   process behind a healthy unit still fails.
2  ``queue_wait_budget``           ``GET /v1/queue/status``:
                                   ``queue_timeout_enforced: true`` and the
                                   configured ``queue_timeout_s``.
3  ``capture_modes``               Live ``GET /api/capture/status`` modes plus the
                                   real modes of ``<capture_root>/media`` (``0750``)
                                   and of the media files written since the restart
                                   (``0640``).  Read-only: nothing is chmod-ed.
4  ``dead_code_removed``           Imports ``app.proxy.server`` in a subprocess and
                                   asserts ``get_version`` is gone (import, not grep).
5  ``privilege_scope_grace``       An unscoped *legacy* key calls a privileged route
                                   (``/api/status``); the journal must carry a
                                   ``scope-grace`` line for that route, and the key
                                   entry must still declare no extra scope.  See
                                   "How the log is found" below.
6  ``metrics_gate``                ``GET /metrics``: ``401`` without a key, ``200``
                                   with a ``client`` key.
7  ``destination_allowlist``       In-process policy resolved from the live config:
                                   ``source == "config"`` (not ``shipped-default``)
                                   and the four ``192.168.1.245`` cleartext hops are
                                   present *and* flagged.
8  ``admission_bounds``            ``/v1/queue/status`` carries ``max_queue_depth``;
                                   the resolved body bound matches
                                   ``config/global.settings.yaml``; a **header-only**
                                   probe (declared ``Content-Length`` over the
                                   bound, zero body bytes uploaded) gets the typed
                                   ``413`` and makes
                                   ``guardian_admission_rejections_total`` appear.
9  ``metadata_audit``              One allowed request produces exactly one audit
                                   record, and the record contains no payload,
                                   header, query or credential (G1-03 §11 schema).
10 ``log_sanitizer``               No ``Bearer <value>`` shape in the log lines
                                   written since the restart (G1-04 invariant).
11 ``endpoint_confinement``        ``GET /v1/../slots`` and a percent-encoded
                                   variant return ``400 path_not_allowed``; a
                                   legitimate ``GET /v1/models`` still works.
12 ``backend_death_seam``          ``/api/status`` exposes ``backend_state`` in
                                   ``{alive, dead, unloaded, unknown}``,
                                   ``backend_death`` and ``caretaker.reachable``.
                                   Fail-open: a daemon without ``last_death`` is
                                   still a PASS, and the line says so.
13 ``capture_pipeline``            Retained from the previous verifier: capture
                                   enabled for all clients, infinite retention,
                                   writer running.
14 ``key_store``                   The auth store loads ``guardian.keys.yaml`` and
                                   sees every key in the file.
15 ``config_deep_merge``           The merged config carries the full global
                                   document (``queue.max_concurrent`` …).
16 ``cloud_catalog_credentials``   Every configured cloud provider reports a
                                   ``credential_status`` and none is ``broken``.
=================================  ==============================================

**How the probe reaches the process.**  The checks talk to the ``MainPID``
itself on ``https://127.0.0.1:11435`` (the TLS listener the unit binds), not to
nginx on ``:11434``: a proxy hop would let a dead Guardian look healthy.  The
certificate's SAN is ``DNS:localhost``, so the connection verifies against the
operator's CA with SNI ``localhost``; override with ``--url``/``--ca``/
``--tls-server-name`` for another layout.  ``--insecure`` skips verification and
prints a warning line — never silently.

**How the log is found.**  Guardian runs under systemd and logs to journald, so
the script reads ``journalctl -u <unit> --since <timestamp> --no-pager -o cat``
and filters in Python.  Windows are anchored on the *restart*
(``ActiveEnterTimestamp`` / ``ExecMainStartTimestamp`` from
``systemctl show --timestamp=unix``) or on the moment just before a probe
request, so lines from a previous run can neither satisfy nor fail a check.
Checks that need a written line poll the journal for a few seconds, because
journald is asynchronous.

**How keys are found.**  ``app.proxy.auth.load_api_keys()`` reads the same
``config/guardian.keys.yaml`` the service uses; the script picks one key that
grants ``admin``, one that grants ``client``, one that grants
``capture-export``, and one *unscoped legacy* key (no ``metadata.scopes``) for
the grace check.  Tokens are never printed: they are registered with
:mod:`app.log_safety` (so any handler scrubs them) and every observed value that
leaves the script goes through ``clean_log_value``/``redact_secrets``.  When a
required scope exists on no key, the check reports ``SKIP`` with the reason — it
never pretends to have verified something it could not.

For item 5, "the key itself stays ``client``-only" means the *declaration* is
unchanged: the entry still carries no ``metadata.scopes``, so privilege came
from the grace window and not from a grant, and after
``auth.scopes_enforced_after`` the same key is a plain client key.

**Read-only.**  No mutating endpoint is called: no ``POST /api/config/reload``,
no catalog refresh, no queue cancel, no capture rotation.  The body-bound probe
declares an oversized ``Content-Length`` and uploads **no** bytes (the bound
refuses a declared oversized body without reading it), so the "must not upload
32 MiB" rule holds by construction.

**Exit codes.**  ``0`` every check passed · ``1`` at least one check failed ·
``2`` no failures but at least one ``SKIP`` (something could not be verified —
read the reason).  ``--json`` prints one machine-readable document on stdout.

Usage::

    ./venv/bin/python scripts/verify_post_restart.py
    ./venv/bin/python scripts/verify_post_restart.py --json | jq -r '.checks[] | ...'
    ./venv/bin/python scripts/verify_post_restart.py --url http://127.0.0.1:11436
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import re
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

# Make `app` importable when run as `python scripts/verify_post_restart.py`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_UNIT = "guardian-llmprovider-gateway"
DEFAULT_URL = "https://127.0.0.1:11435"
DEFAULT_TLS_SERVER_NAME = "localhost"
DEFAULT_CA_GLOB = "/usr/local/share/ca-certificates/*guardian*.crt"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_JOURNAL_LINES = 20000
JOURNAL_SETTLE_TIMEOUT_S = 4.0

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 2

#: ``Bearer <value>`` — the shape the G1-04 invariant forbids in a log line.
_BEARER_SHAPE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{6,}")
_AUDIT_PREFIX = "audit "
_SCOPE_GRACE_MARKERS = ("scope-grace", '"event":"scope_grace"', '"event": "scope_grace"')
_BACKEND_STATES = frozenset({"alive", "dead", "unloaded", "unknown"})
_CLEARTEXT_HOST = "192.168.1.245"
_CLEARTEXT_PORTS = (11440, 11441, 11450, 11451)


# ── Result and context ─────────────────────────────────────────────────


@dataclass
class Result:
    """One check's outcome."""

    check_id: str
    title: str
    status: str
    detail: str = ""
    observed: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "check": self.check_id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "observed": self.observed,
        }


class ProbeError(RuntimeError):
    """The service could not be reached or answered nonsense.

    Raised instead of returning "no evidence", so a check that depends on the
    running service fails loudly when the service is down (a verifier that
    passes against nothing is worse than none).
    """


@dataclass(frozen=True)
class Response:
    """One HTTP response from the probed listener."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", "replace") or "null")
        except ValueError as exc:
            raise ProbeError(f"response is not JSON: {exc}") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass(frozen=True)
class KeyChoice:
    """A key the script is allowed to use, without ever exposing the token."""

    token: str | None
    name: str | None
    declared_scopes: tuple[str, ...] | None
    mode: str
    fingerprint: str | None = None

    @property
    def is_legacy(self) -> bool:
        """True for an entry that declares no ``metadata.scopes`` at all."""
        return self.declared_scopes is None and self.mode in {"grace", "unscoped"}


@dataclass
class Context:
    """Everything a check may read, injected so tests never touch the host."""

    unit: str
    scheme: str
    host: str
    port: int
    repo_root: Path
    ca_file: str | None
    insecure: bool
    tls_server_name: str
    timeout: float
    journal_lines: int
    # injected host surfaces
    http: Callable[..., Response]
    systemctl: Callable[[str, Sequence[str], bool], dict[str, str]]
    journal: Callable[[float, int], list[str]]
    module_has_attr: Callable[[str, str], bool]
    # filled by `prepare`
    service: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    keys: dict[str, Any] = field(default_factory=dict)
    admin_key: KeyChoice | None = None
    client_key: KeyChoice | None = None
    capture_key: KeyChoice | None = None
    legacy_key: KeyChoice | None = None
    service_start: float | None = None
    service_reachable: bool = False
    unreachable_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def require_service(self) -> None:
        """Fail the calling check unless the probe listener answered."""
        if not self.service_reachable:
            raise ProbeError(
                f"service unreachable on {self.scheme}://{self.host}:{self.port}"
                + (f" ({self.unreachable_reason})" if self.unreachable_reason else "")
            )


# ── Host surfaces (all read-only) ──────────────────────────────────────


def _run(cmd: Sequence[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run a read-only host command and return the completed process."""
    return subprocess.run(  # noqa: S603 - fixed argv, never a shell
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False
    )


def systemctl_show(unit: str, props: Sequence[str], unix_timestamps: bool = False) -> dict[str, str]:
    """Return ``systemctl show`` properties for *unit* as a dict.

    Fails loudly (:class:`ProbeError`) when systemd cannot answer: without it no
    service claim below is verifiable.
    """
    cmd = ["systemctl", "show", "--no-pager"]
    if unix_timestamps:
        cmd.append("--timestamp=unix")
    cmd.append(f"--property={','.join(props)}")
    cmd.append(unit)
    try:
        proc = _run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"systemctl show failed: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(
            "systemctl show exited "
            f"{proc.returncode}: {proc.stderr.strip() or proc.stdout.strip() or 'no output'}"
        )
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _proc_address_matches(hex_address: str, host: str) -> bool:
    """True when a ``/proc/net/tcp`` hex address is *host*."""
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return False
    if len(raw) == 4:
        decoded = ".".join(str(part) for part in reversed(raw))
    elif len(raw) == 16:
        # /proc/net/tcp6 stores four 32-bit words, each little-endian.
        words = [raw[index : index + 4][::-1] for index in range(0, 16, 4)]
        decoded = socket.inet_ntop(socket.AF_INET6, b"".join(words))
        if decoded.startswith("::ffff:"):
            decoded = decoded[len("::ffff:") :]
    else:
        return False
    return decoded == host


def _socket_inodes_for_port(port: int, host: str) -> set[str]:
    """Return the listening socket inodes for ``host:port`` from ``/proc/net``."""
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":  # 0A = LISTEN
                continue
            address, _, local_port = parts[1].partition(":")
            try:
                port_value = int(local_port, 16)
            except ValueError:
                continue
            if port_value != port:
                continue
            if not _proc_address_matches(address, host):
                continue
            inodes.add(parts[9])
    return inodes


def _pids_owning_inodes(inodes: Iterable[str]) -> list[int]:
    """Return the PIDs holding any of *inodes* (read-only ``/proc`` scan)."""
    wanted = {f"socket:[{inode}]" for inode in inodes}
    if not wanted:
        return []
    owners: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = str(fd.readlink())
            except OSError:
                continue
            if target in wanted:
                owners.append(int(entry.name))
                break
    return sorted(owners)


def listener_pids(host: str, port: int) -> tuple[list[int], str]:
    """Return ``(pids, method)`` for the process listening on ``host:port``.

    ``ss`` is asked first because it names the owner directly; when it reports
    no owner (permissions, or no ``ss`` at all) the script falls back to
    ``/proc/net/tcp`` plus a scan of ``/proc/<pid>/fd``.  An empty list means
    "nobody is listening", which the identity check treats as a failure.
    """
    try:
        proc = _run(["ss", "-H", "-ltnp", f"sport = :{port}"])
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        pids = sorted({int(match) for match in re.findall(r"pid=(\d+)", proc.stdout)})
        if pids:
            return pids, "ss"
    inodes = _socket_inodes_for_port(port, host)
    return _pids_owning_inodes(inodes), "proc"


def read_journal(unit: str, since: float, lines: int = DEFAULT_JOURNAL_LINES) -> list[str]:
    """Return journal lines for *unit* written since the UNIX time *since*."""
    stamp = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cmd = [
        "journalctl",
        "-u",
        unit,
        "--since",
        stamp,
        "--no-pager",
        "-o",
        "cat",
        "-n",
        str(int(lines)),
    ]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"journalctl failed: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(
            f"journalctl exited {proc.returncode}: "
            f"{proc.stderr.strip() or 'no output'} (is the unit name right?)"
        )
    return proc.stdout.splitlines()


def module_has_attr(module: str, attribute: str, repo_root: Path | None = None) -> bool:
    """Import *module* in a subprocess and report whether it defines *attribute*.

    A subprocess keeps the verifier free of the heavy import side effects (the
    server module builds the app, the queue and the caretaker client at import
    time).  An import failure is a :class:`ProbeError`: "cannot import" is not
    "the name is gone".
    """
    code = (
        "import importlib, json, logging\n"
        "logging.disable(logging.CRITICAL)\n"
        f"module = importlib.import_module({module!r})\n"
        f"print(json.dumps({{'has': hasattr(module, {attribute!r})}}))\n"
    )
    try:
        proc = _run([sys.executable, "-c", code], timeout=120.0)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"import probe failed: {exc}") from exc
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines() or ["no output"]
        raise ProbeError(f"cannot import {module}: {lines[-1]}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return bool(payload["has"])


# ── TLS + HTTP probe ───────────────────────────────────────────────────


def discover_ca(pattern: str = DEFAULT_CA_GLOB) -> str | None:
    """Return the first Guardian CA certificate the host trusts, if any."""
    for candidate in sorted(glob.glob(pattern)):
        if Path(candidate).is_file():
            return candidate
    return None


def build_tls_context(*, ca_file: str | None, insecure: bool) -> ssl.SSLContext:
    """Build the verification context for the loopback TLS listener."""
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if not ca_file:
        raise ProbeError(
            "no Guardian CA certificate found (looked at "
            f"{DEFAULT_CA_GLOB}); pass --ca <cert.pem> or --insecure"
        )
    try:
        return ssl.create_default_context(cafile=ca_file)
    except (OSError, ssl.SSLError) as exc:
        raise ProbeError(f"cannot load CA file {ca_file}: {exc}") from exc


class _SNIConnection(HTTPSConnection):
    """``HTTPSConnection`` that verifies the certificate against a fixed SNI.

    The listener binds ``127.0.0.1`` while the certificate's SAN is
    ``DNS:localhost``, so the socket is connected by address and the TLS
    handshake is given the documented server name — verification stays on.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        context: ssl.SSLContext,
        server_name: str,
        timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._server_name = server_name

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self._server_name)
        except BaseException:
            sock.close()
            raise


def make_http(ctx: "Context") -> Callable[..., Response]:
    """Return the probe callable bound to *ctx* (raw paths, no normalization)."""

    def _request(
        method: str,
        path: str,
        *,
        key: str | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        declared_content_length: int | None = None,
        timeout: float | None = None,
    ) -> Response:
        timeout_s = ctx.timeout if timeout is None else timeout
        if ctx.scheme == "https":
            context = build_tls_context(ca_file=ctx.ca_file, insecure=ctx.insecure)
            conn: HTTPConnection = _SNIConnection(
                ctx.host,
                ctx.port,
                context=context,
                server_name=ctx.tls_server_name,
                timeout=timeout_s,
            )
        else:
            conn = HTTPConnection(ctx.host, ctx.port, timeout=timeout_s)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            conn.putheader("Host", ctx.tls_server_name if ctx.scheme == "https" else ctx.host)
            conn.putheader("Connection", "close")
            if key:
                conn.putheader("Authorization", f"Bearer {key}")
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            if declared_content_length is not None:
                conn.putheader("Content-Length", str(declared_content_length))
            elif body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
            raw = conn.getresponse()
            return Response(
                status=raw.status,
                headers={name.lower(): value for name, value in raw.getheaders()},
                body=raw.read(),
            )
        except (OSError, ssl.SSLError, ConnectionError) as exc:
            raise ProbeError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must never mask the result
                pass

    return _request


# ── Value hygiene ──────────────────────────────────────────────────────


def _clean(value: Any, limit: int = 200) -> str:
    """Bound *value* to one redacted log line (never prints a credential)."""
    from app.log_safety import clean_log_value, redact_secrets

    return redact_secrets(clean_log_value(value, limit=limit))


def _contains_credentials(text: str) -> bool:
    from app.log_safety import contains_credentials

    return contains_credentials(text)


# ── Preparation ────────────────────────────────────────────────────────


def _fingerprint(token: str) -> str | None:
    try:
        from app.proxy import auth as _auth

        return _auth._token_fingerprint(token)
    except Exception:  # noqa: BLE001 - attribution is best-effort
        return None


def _pick_keys(keys: Mapping[str, Any], warnings: list[str]) -> dict[str, KeyChoice | None]:
    """Resolve the keys the checks need, without ever exposing a token."""
    from app.proxy import scopes as _scopes

    picked: dict[str, KeyChoice | None] = {
        "admin": None,
        "client": None,
        "capture-export": None,
        "legacy": None,
    }
    for token, meta in keys.items():
        entry = meta if isinstance(meta, Mapping) else {}
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), Mapping) else {}
        try:
            resolved = _scopes.resolve_key_scopes(entry)
        except Exception as exc:  # noqa: BLE001 - a broken entry must not stop the run
            warnings.append(f"key entry could not be resolved: {_clean(exc)}")
            continue
        raw_scopes = metadata.get("scopes") if isinstance(metadata, Mapping) else None
        declared = tuple(str(item) for item in raw_scopes) if isinstance(raw_scopes, (list, tuple)) else None
        choice = KeyChoice(
            token=str(token),
            name=str(entry.get("name")) if entry.get("name") is not None else None,
            declared_scopes=declared,
            mode=resolved.mode,
            fingerprint=_fingerprint(str(token)),
        )
        if resolved.mode in {"grace", "unscoped"} and "scopes" not in metadata:
            picked["legacy"] = picked["legacy"] or choice
        for scope_name, scope in (
            ("admin", _scopes.Scope.ADMIN),
            ("client", _scopes.Scope.CLIENT),
            ("capture-export", _scopes.Scope.CAPTURE_EXPORT),
        ):
            if scope in resolved.granted and picked[scope_name] is None:
                picked[scope_name] = choice
    return picked


def prepare(ctx: Context) -> None:
    """Collect the shared facts every check reads.  Never raises."""
    try:
        ctx.service = ctx.systemctl(
            ctx.unit,
            (
                "ActiveState",
                "SubState",
                "MainPID",
                "ActiveEnterTimestamp",
                "ExecMainStartTimestamp",
            ),
            True,
        )
    except ProbeError as exc:
        ctx.service = {}
        ctx.warnings.append(f"systemd facts unavailable: {_clean(exc)}")

    for prop in ("ExecMainStartTimestamp", "ActiveEnterTimestamp"):
        raw = str(ctx.service.get(prop) or "").strip()
        if raw.isdigit():
            ctx.service_start = float(raw)
            break

    # The listener is the authority on "is the service there", not systemd.
    try:
        response = ctx.http("GET", "/healthz", timeout=min(ctx.timeout, 10.0))
        ctx.service_reachable = response.status == 200
        if not ctx.service_reachable:
            ctx.unreachable_reason = f"/healthz returned {response.status}"
    except ProbeError as exc:
        ctx.service_reachable = False
        ctx.unreachable_reason = _clean(exc)

    try:
        from app import config_loader as _config_loader

        ctx.config = _config_loader.load_config()
    except Exception as exc:  # noqa: BLE001 - a broken config is reported per check
        ctx.config = {}
        ctx.warnings.append(f"config could not be loaded: {_clean(exc)}")

    try:
        from app.proxy import auth as _auth

        ctx.keys = _auth.load_api_keys()
        picked = _pick_keys(ctx.keys, ctx.warnings)
        ctx.admin_key = picked["admin"]
        ctx.client_key = picked["client"]
        ctx.capture_key = picked["capture-export"]
        ctx.legacy_key = picked["legacy"]
        tokens = [choice.token for choice in picked.values() if choice and choice.token]
        if tokens:
            from app.log_safety import register_secrets

            register_secrets(tokens)
    except Exception as exc:  # noqa: BLE001 - reported by the key_store check
        ctx.keys = {}
        ctx.warnings.append(f"key store could not be loaded: {_clean(exc)}")


# ── Checks ─────────────────────────────────────────────────────────────


def check_service_identity(ctx: Context) -> Result:
    """The unit is active *and* its MainPID is the process behind the listener."""
    title = "service active and MainPID owns the listener"
    if not ctx.service:
        return Result(
            "service_identity",
            title,
            FAIL,
            f"systemd reports nothing for {ctx.unit}: {ctx.unreachable_reason or 'no properties'}",
            {"unit": ctx.unit},
        )
    active = ctx.service.get("ActiveState", "")
    main_pid_raw = str(ctx.service.get("MainPID") or "0")
    observed: dict[str, Any] = {
        "unit": ctx.unit,
        "active": active,
        "sub_state": ctx.service.get("SubState"),
        "main_pid": main_pid_raw,
        "listener": f"{ctx.host}:{ctx.port}",
    }
    if active != "active":
        return Result(
            "service_identity", title, FAIL, f"{ctx.unit} is {active or 'unknown'}", observed
        )
    try:
        main_pid = int(main_pid_raw)
    except ValueError:
        return Result(
            "service_identity", title, FAIL, f"MainPID is not a number: {main_pid_raw}", observed
        )
    if main_pid <= 0:
        return Result("service_identity", title, FAIL, "MainPID is 0 (no main process)", observed)
    pids, method = listener_pids(ctx.host, ctx.port)
    observed["listener_pids"] = pids
    observed["listener_owner_probe"] = method
    if not pids:
        return Result(
            "service_identity",
            title,
            FAIL,
            f"nobody listens on {ctx.host}:{ctx.port} (probe: {method})",
            observed,
        )
    if main_pid not in pids:
        return Result(
            "service_identity",
            title,
            FAIL,
            f"{ctx.host}:{ctx.port} is owned by pid(s) {pids}, MainPID is {main_pid} (stale process)",
            observed,
        )
    return Result(
        "service_identity",
        title,
        PASS,
        f"{ctx.unit} active, MainPID {main_pid} owns {ctx.host}:{ctx.port} ({method})",
        observed,
    )


def _configured_queue_timeout(config: Mapping[str, Any]) -> Any:
    section = config.get("queue") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        return section.get("queue_timeout_seconds")
    return None


def check_queue_wait_budget(ctx: Context) -> Result:
    """``/v1/queue/status`` proves the configured wait budget is enforced."""
    title = "queue wait budget enforced"
    ctx.require_service()
    key = ctx.client_key
    if key is None:
        return Result("queue_wait_budget", title, SKIP, "no key grants the client scope")
    response = ctx.http("GET", "/v1/queue/status", key=key.token)
    body = response.json() if response.status == 200 else {}
    body = body if isinstance(body, Mapping) else {}
    configured = _configured_queue_timeout(ctx.config)
    observed = {
        "status": response.status,
        "queue_timeout_enforced": body.get("queue_timeout_enforced"),
        "queue_timeout_s": body.get("queue_timeout_s"),
        "configured_queue_timeout_s": configured,
    }
    if response.status != 200:
        return Result(
            "queue_wait_budget",
            title,
            FAIL,
            f"GET /v1/queue/status returned {response.status}",
            observed,
        )
    if body.get("queue_timeout_enforced") is not True:
        return Result(
            "queue_wait_budget",
            title,
            FAIL,
            f"queue_timeout_enforced is {body.get('queue_timeout_enforced')!r} (expected True)",
            observed,
        )
    if configured is not None and body.get("queue_timeout_s") != configured:
        return Result(
            "queue_wait_budget",
            title,
            FAIL,
            f"queue_timeout_s={body.get('queue_timeout_s')!r} but the config says {configured!r}",
            observed,
        )
    return Result(
        "queue_wait_budget",
        title,
        PASS,
        f"enforced with queue_timeout_s={body.get('queue_timeout_s')!r}",
        observed,
    )


def _capture_root(ctx: Context) -> tuple[Path, int, int]:
    """Return ``(media_dir, expected_dir_mode, expected_file_mode)``.

    A relative ``capture.capture_root`` is resolved against the *deployment*
    root the service runs in (``app.paths.REPO_ROOT``, i.e. the unit's
    ``WorkingDirectory``), not against the script's own directory.
    """
    from app.capture.config import load_capture_config
    from app.paths import REPO_ROOT as APP_ROOT

    capture = load_capture_config()
    root = Path(capture.capture_root)
    if not root.is_absolute():
        root = Path(APP_ROOT) / root
    return root / "media", int(capture.directory_mode), int(capture.file_mode)


def _live_capture_modes(ctx: Context) -> Mapping[str, Any] | None:
    """Read the running service's effective capture modes, or ``None``.

    ``None`` means "the endpoint could not be used" (service down, key without
    ``capture-export``, or a build without the fields); the filesystem half of
    the check then stands alone instead of failing on a permission the verifier
    simply does not hold.
    """
    if not ctx.service_reachable:
        return None
    key = ctx.capture_key
    if key is None:
        return None
    try:
        response = ctx.http("GET", "/api/capture/status", key=key.token)
    except ProbeError:
        return None
    if response.status != 200:
        return None
    body = response.json()
    config = body.get("config") if isinstance(body, Mapping) else None
    return config if isinstance(config, Mapping) else None


def _media_files(media_dir: Path, since: float | None) -> tuple[list[tuple[Path, int]], int]:
    """Split media files into ``(post-restart files with modes, legacy count)``."""
    recent: list[tuple[Path, int]] = []
    legacy = 0
    for path in media_dir.iterdir():
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if since is not None and info.st_mtime >= since:
            recent.append((path, stat.S_IMODE(info.st_mode)))
        else:
            legacy += 1
    return recent, legacy


def check_capture_modes(ctx: Context) -> Result:
    """The media tree carries the configured modes — read, never written."""
    title = "capture media modes"
    media_dir, expected_dir, expected_file = _capture_root(ctx)
    observed: dict[str, Any] = {
        "media_dir": str(media_dir),
        "expected_directory_mode": oct(expected_dir),
        "expected_file_mode": oct(expected_file),
    }
    if not media_dir.is_dir():
        return Result("capture_modes", title, FAIL, f"{media_dir} does not exist", observed)
    directory_mode = stat.S_IMODE(media_dir.stat().st_mode)
    observed["directory_mode"] = oct(directory_mode)
    live = _live_capture_modes(ctx)
    if live is not None:
        observed["live_file_mode"] = live.get("file_mode")
        observed["live_directory_mode"] = live.get("directory_mode")
        if live.get("file_mode") != oct(expected_file) or live.get("directory_mode") != oct(expected_dir):
            return Result(
                "capture_modes",
                title,
                FAIL,
                "the running service reports "
                f"file_mode={live.get('file_mode')} directory_mode={live.get('directory_mode')}",
                observed,
            )
    if directory_mode != expected_dir:
        return Result(
            "capture_modes",
            title,
            FAIL,
            f"{media_dir} is {oct(directory_mode)} (expected {oct(expected_dir)})",
            observed,
        )

    recent, legacy = _media_files(media_dir, ctx.service_start)
    observed["files_since_restart"] = len(recent)
    observed["legacy_files_out_of_scope"] = legacy
    wrong = [item for item in recent if item[1] != expected_file]
    if wrong:
        exemplar = wrong[0]
        return Result(
            "capture_modes",
            title,
            FAIL,
            f"{len(wrong)} media file(s) written since the restart are not {oct(expected_file)} "
            f"(e.g. {exemplar[0].name} is {oct(exemplar[1])})",
            observed,
        )
    if not recent:
        return Result(
            "capture_modes",
            title,
            SKIP,
            f"{media_dir} is {oct(expected_dir)}; no media file was written since the restart yet, "
            f"so the {oct(expected_file)} file mode is unverified ({legacy} older file(s) are out of "
            "scope — the service never re-modes captured data)",
            observed,
        )
    return Result(
        "capture_modes",
        title,
        PASS,
        f"{media_dir} is {oct(expected_dir)}; {len(recent)} file(s) since the restart are "
        f"{oct(expected_file)}",
        observed,
    )


def check_dead_code_removed(ctx: Context) -> Result:
    """``app.proxy.server.get_version`` is gone (import check, not a grep)."""
    title = "dead get_version removed"
    observed = {"module": "app.proxy.server", "attribute": "get_version"}
    if ctx.module_has_attr("app.proxy.server", "get_version"):
        observed["present"] = True
        return Result(
            "dead_code_removed",
            title,
            FAIL,
            "app.proxy.server still defines get_version — the deployed code predates the removal",
            observed,
        )
    observed["present"] = False
    return Result(
        "dead_code_removed",
        title,
        PASS,
        "imported app.proxy.server in a subprocess: get_version is absent",
        observed,
    )


def _grace_lines(lines: Sequence[str], route: str) -> list[str]:
    """Journal lines that are scope-grace records for *route*."""
    return [
        line
        for line in lines
        if any(marker in line for marker in _SCOPE_GRACE_MARKERS) and route in line
    ]


def _grace_deadline() -> str | None:
    from app.proxy import scopes as _scopes

    try:
        deadline = _scopes.configured_enforced_after()
    except Exception:  # noqa: BLE001 - config trouble is reported by other checks
        return None
    return deadline.isoformat() if deadline else None


def _parse_deadline(value: str) -> datetime | None:
    from app.proxy.scopes import parse_enforced_after

    return parse_enforced_after(value)


def check_privilege_scope_grace(ctx: Context) -> Result:
    """A legacy key reaches a privileged route through the grace window only."""
    title = "privilege scopes: legacy grace, key stays client-only"
    ctx.require_service()
    key = ctx.legacy_key
    if key is None:
        return Result(
            "privilege_scope_grace",
            title,
            SKIP,
            "no unscoped legacy key in the key store; the grace path cannot be exercised",
            {"legacy_key": None},
        )
    route = "/api/status"
    window_start = time.time() - 2.0
    response = ctx.http("GET", route, key=key.token)
    observed: dict[str, Any] = {
        "key_name": key.name,
        "key_mode": key.mode,
        "declared_scopes": list(key.declared_scopes) if key.declared_scopes is not None else None,
        "privileged_route": f"GET {route}",
        "status": response.status,
    }
    if response.status == 403:
        deadline = _grace_deadline()
        observed["grace_deadline"] = deadline
        parsed = _parse_deadline(deadline) if deadline else None
        if parsed is not None and parsed < datetime.now(timezone.utc):
            return Result(
                "privilege_scope_grace",
                title,
                SKIP,
                f"grace window closed at {deadline}: the legacy key is client-only now (403), "
                "so no scope-grace line can be emitted",
                observed,
            )
        return Result(
            "privilege_scope_grace",
            title,
            FAIL,
            f"GET {route} with the legacy key returned 403 before the configured grace deadline "
            f"({deadline}): {_clean(response.text(), 120)}",
            observed,
        )
    if response.status != 200:
        return Result(
            "privilege_scope_grace",
            title,
            FAIL,
            f"GET {route} with the legacy key returned {response.status}: "
            f"{_clean(response.text(), 120)}",
            observed,
        )
    if key.declared_scopes not in (None, ("client",)):
        return Result(
            "privilege_scope_grace",
            title,
            FAIL,
            f"key {key.name!r} declares {list(key.declared_scopes)} — that is a grant, not grace",
            observed,
        )
    lines = _journal_wait(ctx, window_start, lambda entries: _grace_lines(entries, route))
    grace = _grace_lines(lines, route)
    observed["scope_grace_lines"] = len(grace)
    if not grace:
        return Result(
            "privilege_scope_grace",
            title,
            FAIL,
            f"the privileged call succeeded but no scope-grace line for {route} reached the journal",
            observed,
        )
    return Result(
        "privilege_scope_grace",
        title,
        PASS,
        f"key {key.name!r} is unscoped (client-only once the window closes) and the call was "
        f"logged as scope-grace: {_clean(grace[0], limit=150)}",
        observed,
    )


def check_metrics_gate(ctx: Context) -> Result:
    """``/metrics`` needs a client-scope key (issue #46)."""
    title = "metrics gate: 401 without a key, 200 with a client key"
    ctx.require_service()
    anonymous = ctx.http("GET", "/metrics")
    observed: dict[str, Any] = {"anonymous_status": anonymous.status}
    if anonymous.status != 401:
        return Result(
            "metrics_gate",
            title,
            FAIL,
            f"GET /metrics without a key returned {anonymous.status} (expected 401)",
            observed,
        )
    key = ctx.client_key
    if key is None:
        return Result("metrics_gate", title, SKIP, "no key grants the client scope", observed)
    authorized = ctx.http("GET", "/metrics", key=key.token)
    observed["client_status"] = authorized.status
    observed["content_type"] = authorized.headers.get("content-type", "")
    if authorized.status != 200:
        return Result(
            "metrics_gate",
            title,
            FAIL,
            f"GET /metrics with the client key returned {authorized.status}",
            observed,
        )
    if "guardian_" not in authorized.text():
        return Result(
            "metrics_gate",
            title,
            FAIL,
            "GET /metrics returned 200 but carries no guardian_* series",
            observed,
        )
    return Result(
        "metrics_gate",
        title,
        PASS,
        f"anonymous 401, client key 200 ({len(authorized.body)} bytes)",
        observed,
    )


def _policy_entries(policy: Any) -> list[dict[str, Any]]:
    """Read ``AllowEntry`` objects without depending on a private attribute name."""
    raw = getattr(policy, "_entries", None)
    if raw is None:
        raw = getattr(policy, "entries", None)
    entries: list[dict[str, Any]] = []
    for entry in raw or ():
        host = getattr(entry, "host", None)
        ports = getattr(entry, "ports", None)
        flagged = getattr(entry, "cleartext_credentials", None)
        if isinstance(entry, Mapping):
            host = host if host is not None else entry.get("host")
            ports = ports if ports is not None else entry.get("ports")
            flagged = flagged if flagged is not None else entry.get("cleartext_credentials")
        if host is None:
            continue
        entries.append(
            {
                "host": str(host),
                "ports": list(ports or ()),
                "cleartext_credentials": bool(flagged),
            }
        )
    return entries


def check_destination_allowlist(ctx: Context) -> Result:
    """The loaded policy comes from config and still flags the cleartext hops."""
    title = "destination allowlist from config, cleartext hops flagged"
    from app.destination_policy import DestinationPolicy

    policy = DestinationPolicy.from_config(ctx.config)
    source = getattr(policy, "source", "unknown")
    entries = _policy_entries(policy)
    observed: dict[str, Any] = {
        "source": source,
        "entries": len(entries),
        "cleartext_hosts": sorted(
            entry["host"] for entry in entries if entry["cleartext_credentials"]
        ),
    }
    if source != "config":
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            f"the loaded policy source is {source!r} (expected 'config'): "
            "config/global.settings.yaml has no destination_policy block",
            observed,
        )
    if not entries:
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            "the loaded policy is fail-closed (no usable allow entry)",
            observed,
        )
    hops = [entry for entry in entries if entry["host"] == _CLEARTEXT_HOST]
    observed["cleartext_hops"] = [
        {"ports": hop["ports"], "flagged": hop["cleartext_credentials"]} for hop in hops
    ]
    if len(hops) != 1:
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            f"expected one {_CLEARTEXT_HOST} entry, found {len(hops)}",
            observed,
        )
    hop = hops[0]
    if tuple(hop["ports"]) != _CLEARTEXT_PORTS:
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            f"{_CLEARTEXT_HOST} ports are {hop['ports']}, expected {list(_CLEARTEXT_PORTS)}",
            observed,
        )
    if not hop["cleartext_credentials"]:
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            f"the {_CLEARTEXT_HOST} hop is not flagged cleartext_credentials — the tracking flag is off",
            observed,
        )
    return Result(
        "destination_allowlist",
        title,
        PASS,
        f"source=config; {_CLEARTEXT_HOST} ports {list(_CLEARTEXT_PORTS)} present and flagged cleartext",
        observed,
    )


def _configured_admission(config: Mapping[str, Any]) -> dict[str, Any]:
    section = config.get("admission") if isinstance(config, Mapping) else None
    return dict(section) if isinstance(section, Mapping) else {}


def _error_detail(response: Response) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ProbeError:
        return {}
    if not isinstance(body, Mapping):
        return {}
    detail = body.get("detail", body)
    return detail if isinstance(detail, Mapping) else {}


def _metric_line(text: str, metric: str, label_value: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(metric) and label_value in line:
            return line
    return None


def check_admission_bounds(ctx: Context) -> Result:
    """Queue depth, resolved body bound and the rejection counter."""
    title = "admission bounds resolved and counted"
    ctx.require_service()
    from app.proxy.admission import LIMIT_BODY, resolve_limits

    limits = resolve_limits(ctx.config)
    configured = _configured_admission(ctx.config)
    observed: dict[str, Any] = {
        "resolved_body_bound": limits.max_body_bytes,
        "resolved_queue_depth": limits.queue_max_depth,
        "admission_source": limits.source,
        "configured_body_bound": configured.get("max_body_bytes"),
    }
    if limits.invalid:
        return Result(
            "admission_bounds",
            title,
            FAIL,
            "admission refuses traffic (invalid configuration): "
            + ", ".join(f"{name} ({reason})" for name, reason in sorted(limits.invalid.items())),
            observed,
        )
    if configured.get("max_body_bytes") is None:
        return Result(
            "admission_bounds",
            title,
            FAIL,
            "config/global.settings.yaml has no admission.max_body_bytes, so the resolved bound "
            f"{limits.max_body_bytes} is the shipped default and not a configured value "
            f"(source={limits.source!r})",
            observed,
        )
    if limits.max_body_bytes != configured["max_body_bytes"]:
        return Result(
            "admission_bounds",
            title,
            FAIL,
            f"resolved body bound {limits.max_body_bytes} != config {configured['max_body_bytes']}",
            observed,
        )
    if limits.max_body_bytes is None:
        return Result("admission_bounds", title, FAIL, "no body bound could be resolved", observed)

    key = ctx.client_key
    if key is None:
        return Result("admission_bounds", title, SKIP, "no key grants the client scope", observed)
    queue = ctx.http("GET", "/v1/queue/status", key=key.token)
    queue_body = queue.json() if queue.status == 200 else {}
    queue_body = queue_body if isinstance(queue_body, Mapping) else {}
    observed["queue_status"] = queue.status
    observed["max_queue_depth"] = queue_body.get("max_queue_depth")
    if queue.status != 200:
        return Result(
            "admission_bounds", title, FAIL, f"GET /v1/queue/status returned {queue.status}", observed
        )
    depth = queue_body.get("max_queue_depth")
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        return Result(
            "admission_bounds",
            title,
            FAIL,
            f"/v1/queue/status max_queue_depth={depth!r} (expected a positive int)",
            observed,
        )

    oversized = int(limits.max_body_bytes) + 1
    rejection = ctx.http(
        "POST",
        "/v1/chat/completions",
        key=key.token,
        declared_content_length=oversized,
        headers={"Content-Type": "application/json"},
    )
    detail = _error_detail(rejection)
    observed["oversized_status"] = rejection.status
    observed["oversized_probe_uploaded_bytes"] = 0
    observed["oversized_detail"] = _clean(detail.get("error")) if detail else None
    if rejection.status != 413:
        return Result(
            "admission_bounds",
            title,
            FAIL,
            f"a declared body of {oversized} bytes returned {rejection.status} (expected 413)",
            observed,
        )
    if detail.get("limit_bytes") not in (None, int(limits.max_body_bytes)):
        return Result(
            "admission_bounds",
            title,
            FAIL,
            f"413 limit_bytes={detail.get('limit_bytes')} != resolved bound {limits.max_body_bytes}",
            observed,
        )

    metrics = ctx.http("GET", "/metrics", key=key.token)
    observed["metrics_status"] = metrics.status
    metric_line = _metric_line(metrics.text(), "guardian_admission_rejections_total", LIMIT_BODY)
    observed["admission_metric"] = metric_line
    if metrics.status != 200:
        return Result("admission_bounds", title, FAIL, f"GET /metrics returned {metrics.status}", observed)
    if metric_line is None:
        return Result(
            "admission_bounds",
            title,
            FAIL,
            "guardian_admission_rejections_total is missing from /metrics after a refused request",
            observed,
        )
    return Result(
        "admission_bounds",
        title,
        PASS,
        f"body bound {limits.max_body_bytes} (config), max_queue_depth {depth}, "
        f"413 on a declared {oversized}-byte body with 0 bytes uploaded, counter present",
        observed,
    )


def _record_epoch(payload: Mapping[str, Any]) -> float | None:
    """Return the record's own ``ts`` as a UNIX timestamp, or ``None``."""
    raw = payload.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _audit_lines(
    lines: Sequence[str],
    route: str,
    fingerprint: str | None,
    after: float | None = None,
) -> list[tuple[Mapping[str, Any], str]]:
    """Audit records for *route* written after *after* by *fingerprint*'s key.

    The journal window is deliberately wider than the probe (journald stamps
    seconds, not milliseconds); the record's own ``ts`` is what makes "exactly
    one record for THIS request" a real assertion instead of a count of whatever
    else used the route.
    """
    found: list[tuple[Mapping[str, Any], str]] = []
    for line in lines:
        index = line.find(_AUDIT_PREFIX)
        if index < 0:
            continue
        try:
            payload = json.loads(line[index + len(_AUDIT_PREFIX) :])
        except ValueError:
            continue
        if not isinstance(payload, Mapping) or payload.get("event") != "audit":
            continue
        if payload.get("route") != route:
            continue
        if fingerprint and payload.get("key_fingerprint") not in (None, fingerprint):
            continue
        if after is not None:
            record_time = _record_epoch(payload)
            if record_time is not None and record_time < after:
                continue
        found.append((payload, line))
    return found


def _journal_wait(
    ctx: Context,
    since: float,
    predicate: Callable[[Sequence[str]], Sequence[Any]],
    *,
    timeout: float = JOURNAL_SETTLE_TIMEOUT_S,
) -> list[str]:
    """Poll the journal until *predicate* finds something (journald is async)."""
    deadline = time.monotonic() + timeout
    while True:
        lines = ctx.journal(since, ctx.journal_lines)
        if predicate(lines) or time.monotonic() >= deadline:
            return lines
        time.sleep(0.25)


def check_metadata_audit(ctx: Context) -> Result:
    """One allowed request, exactly one record, no payload/header/query/credential."""
    title = "metadata audit: one record, no payload"
    ctx.require_service()
    key = ctx.client_key
    if key is None:
        return Result("metadata_audit", title, SKIP, "no key grants the client scope")
    from app.proxy.audit import RECORD_FIELDS

    nonce = uuid.uuid4().hex
    route = "/api/tags"
    window_start = time.time() - 2.0
    # The record must be *this* request: capture the boundary before it is sent
    # and compare against the record's own millisecond timestamp.  The 5 ms
    # tolerance absorbs that timestamp's truncation to milliseconds — without it
    # a record from the very same millisecond could be filtered out.
    request_started_at = time.time() - 0.005
    response = ctx.http(
        "GET",
        f"{route}?probe={nonce}",
        key=key.token,
        headers={"X-Probe-Marker": nonce, "Content-Type": "application/json"},
        body=json.dumps({"probe_marker": nonce, "payload": "SHOULD-NEVER-BE-LOGGED"}).encode(),
    )
    observed: dict[str, Any] = {
        "status": response.status,
        "route": f"GET {route}",
        "key_fingerprint": key.fingerprint,
    }
    if response.status != 200:
        return Result(
            "metadata_audit",
            title,
            FAIL,
            f"the allowed probe GET {route} returned {response.status}",
            observed,
        )
    def _records(entries: Sequence[str]) -> list[tuple[Mapping[str, Any], str]]:
        return _audit_lines(entries, route, key.fingerprint, request_started_at)

    lines = _journal_wait(ctx, window_start, _records)
    records = _records(lines)
    observed["records"] = len(records)
    if len(records) != 1:
        return Result(
            "metadata_audit",
            title,
            FAIL,
            f"expected exactly 1 audit record for GET {route}, found {len(records)}",
            observed,
        )
    record, raw_line = records[0]
    observed["record_fields"] = sorted(record)
    extra = sorted(set(record) - set(RECORD_FIELDS))
    if extra:
        return Result(
            "metadata_audit",
            title,
            FAIL,
            f"the audit record carries fields outside the G1-03 schema: {extra}",
            observed,
        )
    leaked = [name for name in ("payload", "headers", "query", "credential") if name in record]
    if leaked:
        return Result(
            "metadata_audit",
            title,
            FAIL,
            f"the audit record contains forbidden field(s): {leaked}",
            observed,
        )
    if nonce in raw_line or "SHOULD-NEVER-BE-LOGGED" in raw_line:
        return Result(
            "metadata_audit",
            title,
            FAIL,
            "the audit record echoes request payload/header/query content",
            observed,
        )
    if _contains_credentials(raw_line):
        return Result(
            "metadata_audit",
            title,
            FAIL,
            "the audit record carries a credential shape",
            observed,
        )
    return Result(
        "metadata_audit",
        title,
        PASS,
        f"1 record for GET {route}, {len(record)} schema fields, no payload/header/query/credential",
        observed,
    )


def check_log_sanitizer(ctx: Context) -> Result:
    """No ``Bearer <value>`` shape in the log lines since the restart (G1-04)."""
    title = "log sanitizer: no Bearer value in the log"
    ctx.require_service()
    since = ctx.service_start if ctx.service_start is not None else time.time() - 300.0
    lines = ctx.journal(since, ctx.journal_lines)
    observed: dict[str, Any] = {"since": since, "lines_read": len(lines)}
    offenders = [line for line in lines if _BEARER_SHAPE.search(line) or _contains_credentials(line)]
    observed["offending_lines"] = len(offenders)
    if offenders:
        return Result(
            "log_sanitizer",
            title,
            FAIL,
            f"{len(offenders)} log line(s) carry a credential shape, e.g. "
            f"{_clean(offenders[0], limit=150)}",
            observed,
        )
    if not lines:
        return Result(
            "log_sanitizer",
            title,
            SKIP,
            f"no log lines since {datetime.fromtimestamp(since, timezone.utc).isoformat()} — "
            "nothing to inspect (journald empty or wrong unit)",
            observed,
        )
    return Result(
        "log_sanitizer",
        title,
        PASS,
        f"{len(lines)} line(s) since the restart carry no Bearer/credential shape",
        observed,
    )


def check_endpoint_confinement(ctx: Context) -> Result:
    """A path cannot leave ``/v1``; a legitimate endpoint still answers."""
    title = "endpoint confinement"
    ctx.require_service()
    key = ctx.client_key
    if key is None:
        return Result("endpoint_confinement", title, SKIP, "no key grants the client scope")
    observed: dict[str, Any] = {}
    probes = {
        "dot_segment": "/v1/../slots",
        "percent_encoded": "/v1/%252e%252e/slots",
    }
    for label, path in probes.items():
        response = ctx.http("GET", path, key=key.token)
        detail = _error_detail(response)
        observed[label] = {"status": response.status, "reason": detail.get("reason")}
        if response.status != 400 or detail.get("reason") != "path_not_allowed":
            return Result(
                "endpoint_confinement",
                title,
                FAIL,
                f"GET {path} returned {response.status} "
                f"(reason={detail.get('reason')!r}), expected 400 path_not_allowed",
                observed,
            )
    legit = ctx.http("GET", "/v1/models", key=key.token)
    observed["legitimate_status"] = legit.status
    if legit.status != 200:
        return Result(
            "endpoint_confinement",
            title,
            FAIL,
            f"the legitimate GET /v1/models returned {legit.status}",
            observed,
        )
    return Result(
        "endpoint_confinement",
        title,
        PASS,
        "both escape attempts are 400 path_not_allowed; GET /v1/models is 200",
        observed,
    )


def check_backend_death_seam(ctx: Context) -> Result:
    """``/api/status`` carries the combined backend state and the death signal."""
    title = "backend-death seam on /api/status"
    ctx.require_service()
    key = ctx.admin_key or ctx.client_key
    if key is None:
        return Result("backend_death_seam", title, SKIP, "no key usable for /api/status")
    response = ctx.http("GET", "/api/status", key=key.token)
    if response.status == 403:
        return Result(
            "backend_death_seam",
            title,
            SKIP,
            "GET /api/status requires the admin scope and no key holds it: "
            f"{_clean(response.text(), 120)}",
            {"status": 403},
        )
    if response.status != 200:
        return Result(
            "backend_death_seam",
            title,
            FAIL,
            f"GET /api/status returned {response.status}",
            {"status": response.status},
        )
    body = response.json()
    body = body if isinstance(body, Mapping) else {}
    state = body.get("backend_state")
    caretaker = body.get("caretaker")
    observed: dict[str, Any] = {
        "backend_state": state,
        "backend_death_present": "backend_death" in body,
        "caretaker": caretaker,
    }
    if state not in _BACKEND_STATES:
        return Result(
            "backend_death_seam",
            title,
            FAIL,
            f"backend_state={state!r} (expected one of {sorted(_BACKEND_STATES)})",
            observed,
        )
    if "backend_death" not in body:
        return Result(
            "backend_death_seam",
            title,
            FAIL,
            "the payload has no backend_death field (the contract requires it; null is fine)",
            observed,
        )
    if not isinstance(caretaker, Mapping) or "reachable" not in caretaker:
        return Result(
            "backend_death_seam",
            title,
            FAIL,
            "the payload has no caretaker.reachable field",
            observed,
        )
    death_signal = caretaker.get("death_signal")
    observed["death_signal"] = death_signal
    detail = (
        f"backend_state={state}, "
        f"backend_death={'null' if body.get('backend_death') is None else 'recorded'}, "
        f"caretaker.reachable={caretaker.get('reachable')}"
    )
    if death_signal in {"unsupported", "empty", "not_configured", None}:
        detail += (
            f"; the daemon reports no last_death (death_signal={death_signal!r}) and that is "
            "fail-open, still a PASS"
        )
    return Result("backend_death_seam", title, PASS, detail, observed)


def check_capture_pipeline(ctx: Context) -> Result:
    """Retained: capture enabled for all clients, infinite retention, writer up."""
    title = "capture pipeline enabled and writing"
    ctx.require_service()
    key = ctx.capture_key
    if key is None:
        return Result(
            "capture_pipeline",
            title,
            SKIP,
            "no key grants the capture-export scope required by /api/capture/status",
        )
    response = ctx.http("GET", "/api/capture/status", key=key.token)
    if response.status == 403:
        return Result(
            "capture_pipeline",
            title,
            SKIP,
            f"/api/capture/status requires the capture-export scope: {_clean(response.text(), 120)}",
            {"status": 403},
        )
    if response.status != 200:
        return Result(
            "capture_pipeline", title, FAIL, f"GET /api/capture/status returned {response.status}"
        )
    body = response.json()
    body = body if isinstance(body, Mapping) else {}
    config = body.get("config") if isinstance(body.get("config"), Mapping) else {}
    writer = body.get("writer") if isinstance(body.get("writer"), Mapping) else {}
    observed = {
        "enabled": config.get("enabled"),
        "active": config.get("active"),
        "cloud_capture": config.get("cloud_capture"),
        "per_client_opt_in": config.get("per_client_opt_in"),
        "retention_days": config.get("retention_days"),
        "max_capture_bytes": config.get("max_capture_bytes"),
        "writer_running": writer.get("running"),
    }
    problems: list[str] = []
    if config.get("enabled") is not True or config.get("active") is not True:
        problems.append("capture is not enabled/active")
    if config.get("cloud_capture") is not True:
        problems.append("cloud capture is off")
    if config.get("per_client_opt_in") is not False:
        problems.append("per-client opt-in is on")
    if config.get("retention_days") != -1:
        problems.append(f"retention_days={config.get('retention_days')!r} (expected -1)")
    if config.get("max_capture_bytes") != -1:
        problems.append(f"max_capture_bytes={config.get('max_capture_bytes')!r} (expected -1)")
    if writer.get("running") is not True:
        problems.append("the capture writer is not running")
    if problems:
        return Result("capture_pipeline", title, FAIL, "; ".join(problems), observed)
    return Result(
        "capture_pipeline",
        title,
        PASS,
        "enabled for all clients, infinite retention, writer running",
        observed,
    )


def check_key_store(ctx: Context) -> Result:
    """Retained: the auth store loads the deployment's key file completely."""
    title = "key store loads guardian.keys.yaml"
    from app.proxy import auth as _auth

    observed: dict[str, Any] = {"file": _auth.API_KEYS_FILE.name}
    loaded = ctx.keys if isinstance(ctx.keys, Mapping) else {}
    observed["loaded"] = len(loaded)
    if not loaded:
        return Result("key_store", title, FAIL, "the auth store returned no keys", observed)
    try:
        import yaml

        raw = yaml.safe_load(_auth.API_KEYS_FILE.read_text()) or {}
    except Exception as exc:  # noqa: BLE001 - an unreadable file is a failure
        return Result(
            "key_store", title, FAIL, f"cannot read {_auth.API_KEYS_FILE}: {_clean(exc)}", observed
        )
    declared = len(raw) if isinstance(raw, Mapping) else 0
    observed["declared"] = declared
    if declared != len(loaded):
        return Result(
            "key_store",
            title,
            FAIL,
            f"the file declares {declared} keys but the store loaded {len(loaded)}",
            observed,
        )
    return Result("key_store", title, PASS, f"{declared} keys from guardian.keys.yaml", observed)


def check_config_deep_merge(ctx: Context) -> Result:
    """Retained: the merged config still carries the full global document."""
    title = "config deep-merges global.settings.yaml"
    observed: dict[str, Any] = {}
    if not ctx.config:
        return Result("config_deep_merge", title, FAIL, "no configuration could be loaded", observed)
    queue = ctx.config.get("queue") if isinstance(ctx.config, Mapping) else None
    observed["sections"] = {
        "queue": isinstance(queue, Mapping),
        "capture": isinstance(ctx.config.get("capture"), Mapping),
        "providers": isinstance(ctx.config.get("providers"), Mapping),
    }
    if not isinstance(queue, Mapping) or "max_concurrent" not in queue:
        return Result(
            "config_deep_merge",
            title,
            FAIL,
            f"the merged config has no queue.max_concurrent (queue={type(queue).__name__})",
            observed,
        )
    return Result(
        "config_deep_merge",
        title,
        PASS,
        f"queue.max_concurrent={queue.get('max_concurrent')!r}, capture and providers present",
        observed,
    )


def check_cloud_catalog_credentials(ctx: Context) -> Result:
    """Retained: every configured cloud provider reports a usable credential."""
    title = "cloud catalog: no broken credentials"
    ctx.require_service()
    key = ctx.admin_key
    if key is None:
        return Result("cloud_catalog_credentials", title, SKIP, "no key grants the admin scope")
    response = ctx.http("GET", "/api/cloud/catalog", key=key.token)
    if response.status == 403:
        return Result(
            "cloud_catalog_credentials",
            title,
            SKIP,
            f"/api/cloud/catalog requires the admin scope: {_clean(response.text(), 120)}",
            {"status": 403},
        )
    if response.status != 200:
        return Result(
            "cloud_catalog_credentials",
            title,
            FAIL,
            f"GET /api/cloud/catalog returned {response.status}",
            {"status": response.status},
        )
    body = response.json()
    body = body if isinstance(body, Mapping) else {}
    items = body.get("catalog") if isinstance(body.get("catalog"), list) else []
    configured = [item for item in items if isinstance(item, Mapping) and item.get("configured")]
    observed: dict[str, Any] = {
        "catalog_entries": len(items),
        "configured": len(configured),
        "credential_status": {
            str(item.get("name")): item.get("credential_status") for item in configured
        },
    }
    missing = [str(item.get("name")) for item in configured if "credential_status" not in item]
    broken = [str(item.get("name")) for item in configured if item.get("credential_status") == "broken"]
    observed["missing_status"] = missing
    observed["broken"] = broken
    if missing:
        return Result(
            "cloud_catalog_credentials",
            title,
            FAIL,
            f"provider(s) without a credential_status: {missing}",
            observed,
        )
    if broken:
        return Result("cloud_catalog_credentials", title, FAIL, f"broken credential(s): {broken}", observed)
    return Result(
        "cloud_catalog_credentials",
        title,
        PASS,
        f"{len(configured)} configured provider(s), none broken",
        observed,
    )


CHECKS: tuple[tuple[str, Callable[[Context], Result]], ...] = (
    ("service_identity", check_service_identity),
    ("queue_wait_budget", check_queue_wait_budget),
    ("capture_modes", check_capture_modes),
    ("dead_code_removed", check_dead_code_removed),
    ("privilege_scope_grace", check_privilege_scope_grace),
    ("metrics_gate", check_metrics_gate),
    ("destination_allowlist", check_destination_allowlist),
    ("admission_bounds", check_admission_bounds),
    ("metadata_audit", check_metadata_audit),
    ("log_sanitizer", check_log_sanitizer),
    ("endpoint_confinement", check_endpoint_confinement),
    ("backend_death_seam", check_backend_death_seam),
    ("capture_pipeline", check_capture_pipeline),
    ("key_store", check_key_store),
    ("config_deep_merge", check_config_deep_merge),
    ("cloud_catalog_credentials", check_cloud_catalog_credentials),
)


# ── Runner ─────────────────────────────────────────────────────────────


def run_checks(ctx: Context) -> list[Result]:
    """Run every check; an unexpected exception is a loud FAIL, never a skip."""
    results: list[Result] = []
    for check_id, check in CHECKS:
        try:
            results.append(check(ctx))
        except ProbeError as exc:
            results.append(Result(check_id, check_id, FAIL, _clean(exc)))
        except Exception as exc:  # noqa: BLE001 - one broken check must not stop the run
            results.append(Result(check_id, check_id, FAIL, f"{type(exc).__name__}: {_clean(exc)}"))
    return results


def exit_code_for(results: Sequence[Result]) -> int:
    """``0`` all passed, ``1`` any failure, ``2`` skips without a failure."""
    if any(result.status == FAIL for result in results):
        return EXIT_FAIL
    if any(result.status == SKIP for result in results):
        return EXIT_SKIP
    return EXIT_PASS


def _print_result(result: Result) -> None:
    line = f"[{result.status}] {result.title}"
    if result.detail:
        line += f" — {result.detail}"
    print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the running Guardian deployment against the current contract.",
    )
    parser.add_argument("--json", action="store_true", help="print one machine-readable document")
    parser.add_argument("--unit", default=DEFAULT_UNIT, help=f"systemd unit (default {DEFAULT_UNIT})")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"listener to probe (default {DEFAULT_URL})")
    parser.add_argument("--ca", default=None, help="CA certificate for the TLS listener")
    parser.add_argument(
        "--tls-server-name",
        default=DEFAULT_TLS_SERVER_NAME,
        help=f"SNI name to verify the certificate against (default {DEFAULT_TLS_SERVER_NAME})",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (prints a warning; loopback debugging only)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-request timeout")
    parser.add_argument(
        "--journal-lines",
        type=int,
        default=DEFAULT_JOURNAL_LINES,
        help="maximum journal lines to read per check",
    )
    return parser


def build_context(args: argparse.Namespace) -> Context:
    """Build the probe context from parsed arguments."""
    parts = urlsplit(args.url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SystemExit(f"--url must be an http(s) URL, got {args.url!r}")
    ca_file = args.ca or (discover_ca() if parts.scheme == "https" and not args.insecure else None)
    ctx = Context(
        unit=args.unit,
        scheme=parts.scheme,
        host=parts.hostname,
        port=parts.port or (443 if parts.scheme == "https" else 80),
        repo_root=REPO_ROOT,
        ca_file=ca_file,
        insecure=bool(args.insecure),
        tls_server_name=args.tls_server_name,
        timeout=float(args.timeout),
        journal_lines=int(args.journal_lines),
        http=lambda *a, **kw: (_ for _ in ()).throw(ProbeError("http surface not built")),
        systemctl=systemctl_show,
        journal=lambda since, lines: read_journal(args.unit, since, lines),
        module_has_attr=lambda module, attribute: module_has_attr(module, attribute, REPO_ROOT),
    )
    ctx.http = make_http(ctx)
    return ctx


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check and return the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        ctx = build_context(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken invocation must not traceback
        print(f"[{FAIL}] could not start: {_clean(exc)}")
        return EXIT_FAIL
    prepare(ctx)
    results = run_checks(ctx)
    code = exit_code_for(results)
    if args.json:
        document = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "unit": ctx.unit,
            "base_url": f"{ctx.scheme}://{ctx.host}:{ctx.port}",
            "tls_verified": not ctx.insecure and ctx.scheme == "https",
            "service_reachable": ctx.service_reachable,
            "exit_code": code,
            "ok": code == EXIT_PASS,
            "warnings": [_clean(warning) for warning in ctx.warnings],
            "checks": [result.to_json() for result in results],
        }
        print(json.dumps(document, indent=2, sort_keys=True))
        return code
    if args.insecure:
        print("⚠️  --insecure: TLS verification is OFF for this run")
    for warning in ctx.warnings:
        print(f"⚠️  {_clean(warning)}")
    print(
        f"Verifying {ctx.unit} on {ctx.scheme}://{ctx.host}:{ctx.port}"
        + (
            " (service unreachable — service checks will fail)"
            if not ctx.service_reachable
            else ""
        )
    )
    for result in results:
        _print_result(result)
    passed = sum(1 for result in results if result.status == PASS)
    failed = sum(1 for result in results if result.status == FAIL)
    skipped = sum(1 for result in results if result.status == SKIP)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if code == EXIT_PASS:
        print("✅ the running deployment matches the contract")
    elif code == EXIT_SKIP:
        print("⚠️  no failures, but something could not be verified — read the SKIP reasons")
    else:
        print("❌ the running deployment does NOT match the contract")
    return code


if __name__ == "__main__":
    sys.exit(main())
