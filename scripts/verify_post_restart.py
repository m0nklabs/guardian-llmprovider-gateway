#!/usr/bin/env python3
"""Post-restart verification for Guardian — the current deployment contract.

**When to run it.** Immediately after
``sudo systemctl restart guardian-llmprovider-gateway``, and after every push
that is supposed to be live.  ``scripts/pre_restart_check.py`` proves the code
is *safe to deploy*; this script proves the running process *is* that code.  A
service that is merely ``active`` can still be an old process, so every check
below reads a fact that only the merged code produces.

**Which tree it grades (issue #84).**  A verifier that grades a checkout other
than the running deployment is worse than none: run from a detached worktree it
once reported ``PASS`` for code that was not running.  So the script resolves the
deployed root *from the service itself* — ``/proc/<MainPID>/cwd`` of the unit,
cross-checked with ``systemctl show -p WorkingDirectory`` (no MainPID, no
readable ``/proc`` entry, or two disagreeing answers ⇒ the root is **unknown**,
never guessed) — and compares it with its own root.  A different tree is refused
with exit ``3``, a message naming both paths, and **no check output at all**;
``--allow-foreign-tree`` proceeds for review/CI but every check whose source is
not the live API reports ``SKIP`` ("does not prove the deployment") instead of a
verdict.  When the service is down the tree is unknown, so the run refuses too.

**What it verifies** (one ``[PASS]``/``[FAIL]``/``[SKIP]`` line per item, each
naming its source: the running process, the live HTTP API, the deployed
code/config file, or journald):

=================================  ==============  ==============================================
check                              source          what it reads
=================================  ==============  ==============================================
1  ``service_identity``            process         ``systemctl show``: active + ``MainPID``, and the
                                                   process that owns the listener on the probe
                                                   address (``/proc``/``ss``), so a *stale*
                                                   process behind a healthy unit still fails.
2  ``queue_wait_budget``           deployed-config ``GET /v1/queue/status``:
                                                   ``queue_timeout_enforced: true`` and the
                                                   configured ``queue_timeout_s``.
3  ``capture_modes``               deployed-file   Live ``GET /api/capture/status`` modes plus the
                                                   real modes of ``<capture_root>/media`` (``0750``)
                                                   and of the media files written since the restart
                                                   (``0640``).  Read-only: nothing is chmod-ed.
4  ``dead_code_removed``           deployed-code   The module the process loaded
                                                   (``/proc/<MainPID>/cwd/app/proxy/server.py``,
                                                   accepted only when neither its mtime nor its
                                                   ctime is newer than the process start) must not
                                                   define ``get_version``, and the process must not
                                                   serve a ``/api/version`` route (404).
5  ``privilege_scope_grace``       journal         An unscoped *legacy* key calls a privileged route
                                                   (``/api/status``); the journal must carry a
                                                   ``scope-grace`` line for that route, and the key
                                                   entry must still declare no extra scope.  See
                                                   "How the log is found" below.
6  ``metrics_gate``                live-api        ``GET /metrics``: ``401`` without a key, ``200``
                                                   with a ``client`` key.
7  ``destination_allowlist``       live-api        ``GET /api/status`` → ``policy``: the policy the
                                                   running process holds — ``source == "config"``
                                                   (not ``shipped-default``) — and the four
                                                   ``192.168.1.245`` cleartext hops present *and*
                                                   flagged.  No YAML is re-parsed.
8  ``admission_bounds``            deployed-config ``/v1/queue/status`` carries ``max_queue_depth``;
                                                   the resolved body bound matches
                                                   ``config/global.settings.yaml``; a **header-only**
                                                   probe (declared ``Content-Length`` over the
                                                   bound, zero body bytes uploaded) gets the typed
                                                   ``413`` and makes
                                                   ``guardian_admission_rejections_total`` appear.
9  ``metadata_audit``              journal         One allowed request produces exactly one audit
                                                   record, and the record contains no payload,
                                                   header, query or credential (G1-03 §11 schema).
10 ``log_sanitizer``               journal         No ``Bearer <value>`` shape in the log lines
                                                   written since the restart (G1-04 invariant).
11 ``endpoint_confinement``        live-api        ``GET /v1/../slots`` and a percent-encoded
                                                   variant return ``400 path_not_allowed``; a
                                                   legitimate ``GET /v1/models`` still works.
12 ``backend_death_seam``          live-api        ``/api/status`` exposes ``backend_state`` in
                                                   ``{alive, dead, unloaded, unknown}``,
                                                   ``backend_death`` and ``caretaker.reachable``.
                                                   Fail-open: a daemon without ``last_death`` is
                                                   still a PASS, and the line says so.
13 ``capture_pipeline``            live-api        Retained from the previous verifier: capture
                                                   enabled for all clients, infinite retention,
                                                   writer running.
14 ``key_store``                   deployed-file   The auth store loads ``guardian.keys.yaml`` and
                                                   sees every key in the file.
15 ``config_deep_merge``           deployed-config The merged config carries the full global
                                                   document (``queue.max_concurrent`` …).
16 ``cloud_catalog_credentials``   live-api        Every configured cloud provider reports a
                                                   ``credential_status`` and none is ``broken``.
=================================  ==============  ==============================================

**Which commit it graded (issue #85).**  Before the checks, the run prints one
``[INFO] deployed commit — source=deployed-tree`` line naming the commit the
running process executes, read from the process's own root
(``/proc/<MainPID>/cwd`` -> ``git -C <root> rev-parse HEAD``) and carried in
``--json`` as ``deployed_commit``.  It is **reported, never graded**: deploying
an older commit on purpose is legitimate, so it is not a PASS/FAIL check and
never changes the exit code.  When the commit cannot be read — unknown root, a
foreign-tree run, or a tree without git metadata — the line is ``SKIP`` with the
reason instead of an invented sha.

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

**How keys are found.**  ``app.proxy.auth.load_api_keys()`` reads
``config/guardian.keys.yaml`` *of the script's tree* — which the tree guard has
already proven to be the deployed tree, or the run is refused; under
``--allow-foreign-tree`` a worktree without that untracked file simply yields no
key and the checks that need one report ``SKIP``.  The script picks one key that
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
read the reason) · ``3`` refused: the script's tree is not the deployed tree (or
the deployed root could not be determined), so no verdict was produced.
``--json`` prints one machine-readable document on stdout.

Usage::

    ./venv/bin/python scripts/verify_post_restart.py          # from the deployment
    ./venv/bin/python scripts/verify_post_restart.py --json | jq -r '.checks[] | ...'
    ./venv/bin/python scripts/verify_post_restart.py --url http://127.0.0.1:11436
    ./venv/bin/python scripts/verify_post_restart.py --allow-foreign-tree   # review only
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import logging
import os
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
#: Seconds allowed for the local ``git`` query that names the deployed commit.
GIT_TIMEOUT_S = 10.0

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
#: A reported fact, not a verdict: it never changes the exit code.  Used for the
#: deployed commit, because deploying an older commit on purpose is legitimate
#: (issue #85) — the point is that the verdict *names* what it graded.
INFO = "INFO"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 2
EXIT_REFUSED = 3

# ── Sources (issue #84) ────────────────────────────────────────────────
# Every check declares where its evidence comes from, and every line prints it:
# a reader must be able to tell a process fact from a file fact without reading
# the code.
#: The running service as systemd and ``/proc`` describe it (unit state, MainPID,
#: listener ownership).
SOURCE_PROCESS = "process"
#: Answers of the running process on its own listener — nothing else.
SOURCE_LIVE_API = "live-api"
#: The code file the process loaded, located through ``/proc/<MainPID>/cwd``.
SOURCE_DEPLOYED_CODE = "deployed-code"
#: A config file that is genuinely the deployed one (the tree guard proves it).
SOURCE_DEPLOYED_CONFIG = "deployed-config"
#: A data/key file that is genuinely the deployed one.
SOURCE_DEPLOYED_FILE = "deployed-file"
#: journald lines the running unit wrote.
SOURCE_JOURNAL = "journal"
#: The git commit the deployed tree is on, read from the process's own root
#: (``/proc/<MainPID>/cwd`` -> ``git -C <root> rev-parse HEAD``).  Reported, never
#: graded: it exists so a verdict names what it graded (issue #85).
SOURCE_DEPLOYED_TREE = "deployed-tree"

#: The only source that stands on its own in a foreign tree: it reads the live
#: service and nothing about the script's checkout.
SOURCES_TREE_INDEPENDENT = frozenset({SOURCE_LIVE_API})

#: Procfs mount point (injected through ``Context.read_proc_cwd`` in tests).
PROC_ROOT = Path("/proc")

#: The dead code the deploy batch removed (#47): an unscoped handler that was
#: never routed, plus the Ollama-shaped route the contract says is not served.
DEAD_CODE_MODULE = Path("app") / "proxy" / "server.py"
DEAD_CODE_SYMBOL = "get_version"
DEAD_CODE_ROUTE = "/api/version"

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
    """One check's outcome, including where its evidence came from."""

    check_id: str
    title: str
    status: str
    detail: str = ""
    observed: dict[str, Any] = field(default_factory=dict)
    #: One of the ``SOURCE_*`` kinds; stamped by the runner from ``CHECKS``.
    source: str = ""
    #: What that source concretely is (e.g. the path or the endpoint read).
    source_evidence: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "check": self.check_id,
            "title": self.title,
            "status": self.status,
            "source": self.source,
            "source_evidence": self.source_evidence,
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
    read_proc_cwd: Callable[[int], str | None]
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
    #: Where the running deployment lives, as the service reported it (issue #84).
    deployment: "Deployment | None" = None
    #: True when this run grades a tree that is not the deployment (flag given).
    foreign_tree: bool = False
    #: True when the flag was given at all (printed as a banner).
    allow_foreign_tree: bool = False
    #: Where the API keys came from (``auth`` or ``--keys-file``), for SKIP text.
    keys_file: str | None = None
    #: True when the key store this run read does not exist at all.
    keys_file_missing: bool = False
    #: True when the store exists but holds no entries.
    keys_file_empty: bool = False
    #: Why the store could not be loaded, when it could not (never a token).
    keys_load_error: str = ""

    def require_service(self) -> None:
        """Fail the calling check unless the probe listener answered."""
        if not self.service_reachable:
            raise ProbeError(
                f"service unreachable on {self.scheme}://{self.host}:{self.port}"
                + (f" ({self.unreachable_reason})" if self.unreachable_reason else "")
            )

    @property
    def deployed_root(self) -> Path | None:
        """The root the service runs in, or ``None`` when it is unknown."""
        return self.deployment.root if self.deployment is not None else None


# ── The deployed tree (issue #84) ──────────────────────────────────────


@dataclass(frozen=True)
class Deployment:
    """Where the running service's tree is, and how that was established.

    ``root`` is ``None`` — never a guess — whenever the service cannot say where
    it runs: no MainPID, no readable ``/proc`` entry *and* no declared
    ``WorkingDirectory``, or two contradicting answers.
    """

    root: Path | None = None
    main_pid: int | None = None
    proc_cwd: str | None = None
    working_directory: str | None = None
    #: How ``root`` was resolved: ``/proc/<pid>/cwd`` (the only accepted origin).
    origin: str = ""
    #: Why ``root`` is unknown; empty when it was resolved.
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.root is not None


def read_proc_cwd(pid: int) -> str | None:
    """Return ``readlink /proc/<pid>/cwd``, or ``None`` when it cannot be read."""
    try:
        return os.readlink(PROC_ROOT / str(pid) / "cwd")
    except OSError:
        return None


def _realpath(value: str | Path) -> Path:
    """Resolve *value* to a canonical absolute path (it need not exist)."""
    return Path(os.path.realpath(os.path.expanduser(str(value))))


def resolve_deployment(ctx: Context) -> Deployment:
    """Resolve the deployed root from the service itself.  Never guesses.

    ``/proc/<MainPID>/cwd`` is the process's own answer and the **only** accepted
    origin: ``WorkingDirectory`` is the unit's *declaration*, and a
    ``daemon-reload`` can change it without restarting the process.  It is used
    only as a cross-check; when the two disagree, or the cwd cannot be read, or
    there is no MainPID at all, the root is *unknown* with the reason, so the
    caller refuses instead of grading an arbitrary tree.
    """
    try:
        facts = ctx.systemctl(ctx.unit, ("ActiveState", "MainPID", "WorkingDirectory"), False)
    except ProbeError as exc:
        return Deployment(reason=f"systemd could not report the unit: {_clean(exc)}")
    active = str(facts.get("ActiveState") or "").strip()
    working_directory = str(facts.get("WorkingDirectory") or "").strip() or None
    try:
        main_pid = int(str(facts.get("MainPID") or "0").strip())
    except ValueError:
        return Deployment(
            main_pid=None,
            working_directory=working_directory,
            reason=f"systemd reports a non-numeric MainPID {facts.get('MainPID')!r}",
        )
    if main_pid <= 0:
        return Deployment(
            main_pid=None,
            working_directory=working_directory,
            reason=f"{ctx.unit} has no MainPID (ActiveState={active or 'unknown'}): "
            "the service is not running, so its tree cannot be determined",
        )
    proc_cwd = ctx.read_proc_cwd(main_pid)
    if not proc_cwd:
        # WorkingDirectory is *declaration*, not evidence: changing it and
        # running `daemon-reload` without a restart leaves the old process in its
        # old checkout, and a verifier started from the newly configured one
        # would grade the wrong files (PR-Piet, #84).  Only the process's own cwd
        # may anchor a verdict.
        declared = f"; the unit merely declares WorkingDirectory={working_directory}" if working_directory else ""
        return Deployment(
            main_pid=main_pid,
            working_directory=working_directory,
            reason=f"/proc/{main_pid}/cwd cannot be read{declared} — run the verifier as the "
            "service user (or as root) so the process's own cwd is available",
        )
    if working_directory and _realpath(working_directory) != _realpath(proc_cwd):
        return Deployment(
            main_pid=main_pid,
            proc_cwd=proc_cwd,
            working_directory=working_directory,
            reason=f"/proc/{main_pid}/cwd is {proc_cwd} but the unit declares "
            f"WorkingDirectory={working_directory} — refusing to guess which is deployed",
        )
    return Deployment(
        main_pid=main_pid,
        proc_cwd=proc_cwd,
        working_directory=working_directory,
        origin=f"/proc/{main_pid}/cwd",
        root=_realpath(proc_cwd),
    )


def tree_matches(deployment: Deployment, script_root: Path) -> bool | None:
    """``True``/``False`` when the deployed root is known, ``None`` when it is not."""
    if deployment.root is None:
        return None
    return _realpath(deployment.root) == _realpath(script_root)


def refusal_message(deployment: Deployment, script_root: Path) -> str:
    """The text printed when this checkout is not the deployed tree."""
    lines = [
        f"REFUSED: {os.path.basename(__file__)} would grade the wrong tree",
        f"  deployed root : {deployment.root if deployment.known else 'UNKNOWN'}"
        + (f"   ({deployment.origin})" if deployment.origin else ""),
        f"  script root   : {_realpath(script_root)}",
    ]
    if not deployment.known:
        lines.append(f"  why unknown   : {deployment.reason}")
    else:
        lines.append(
            "  The running service does not run the checkout this script lives in, so no "
            "check was run and no verdict was printed."
        )
    lines.extend(
        [
            "",
            "Run it from the deployed checkout, as the service user:",
            f"  cd {deployment.root if deployment.known else '<deployed checkout>'} "
            "&& ./venv/bin/python scripts/verify_post_restart.py",
            "For a review run that never proves the deployment, pass --allow-foreign-tree "
            "(process-level checks then SKIP).",
        ]
    )
    return "\n".join(lines)


# ── The deployed commit (issue #85) ────────────────────────────────────


@dataclass(frozen=True)
class DeployedCommit:
    """The commit the running process executes, and how that was established.

    Reported, never graded.  Deploying an older commit on purpose is legitimate,
    so this is not a PASS/FAIL check; it exists so that a green verdict *names*
    what it graded instead of leaving the reader to guess which commit is live.
    ``commit`` is empty — never a guess — whenever the root is unknown, the run
    is a foreign-tree run, or the tree carries no git metadata.
    """

    root: Path | None = None
    commit: str = ""
    branch: str = ""
    status: str = SKIP
    reason: str = ""
    #: How the commit was read, e.g. ``/proc/4242/cwd -> git -C <root> rev-parse HEAD``.
    origin: str = ""
    #: True only when the checkout is proven not to have moved since the process
    #: started, i.e. when the sha is startup-bound evidence and not merely "what
    #: is on disk now".  Always False for a SKIP.
    bound_to_process: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": SOURCE_DEPLOYED_TREE,
            "root": str(self.root) if self.root is not None else None,
            "commit": self.commit or None,
            "branch": self.branch or None,
            "origin": self.origin,
            "reason": self.reason,
            "bound_to_process": self.bound_to_process,
        }


def _git_in(root: Path, *args: str) -> str:
    """Run a read-only git command in *root*; return stdout, or ``""`` on failure."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _checkout_moved_after(root: Path, since: float | None) -> tuple[bool | None, str]:
    """Did the checkout's ``HEAD`` move after *since*?  ``None`` when unknowable.

    ``git -C <root> rev-parse HEAD`` answers "what is checked out *now*", not
    "what did the process load".  After the documented ``checkout main && pull``
    step an unrestarted process still runs its old code while its cwd holds the
    new ``HEAD``, so the raw sha would name a deployment that never happened
    (PR-Piet on #85).

    The reflog is the only accepted evidence: it records when ``HEAD`` last moved
    in *this* checkout, which is the fact that binds the sha to the process
    start.  A commit date is deliberately **not** used as a fallback — it says
    when the commit was written, not when it was checked out, so it would turn
    "cannot tell" into a false "verified".  No reflog, no process start, or an
    unparseable timestamp all return ``None``, and the caller reports the
    checkout ``HEAD`` as unverified.
    """
    if since is None:
        return None, ""
    raw = _git_in(root, "reflog", "show", "-1", "--format=%ct", "HEAD")
    if not raw.isdigit():
        return None, ""
    moved_at = float(raw)
    evidence = f"reflog {moved_at:.0f} vs process start {since:.0f}"
    return moved_at > since, evidence


def resolve_deployed_commit(ctx: Context) -> DeployedCommit:
    """Read the commit the running process executes, from its own tree.

    The chain is ``/proc/<MainPID>/cwd`` -> ``git -C <root> rev-parse HEAD``: the
    process's own root, never the script's checkout.  Never a guess — an unknown
    root, a foreign-tree run, or a tree without git metadata reports ``SKIP``
    with the reason instead of inventing a commit.
    """
    deployment = ctx.deployment
    if deployment is None or not deployment.known:
        why = deployment.reason if deployment is not None else "no deployment facts"
        return DeployedCommit(
            reason=f"the deployed root could not be determined ({why})",
        )
    root = deployment.root
    origin = f"{deployment.origin} -> git -C {root} rev-parse HEAD"
    if ctx.foreign_tree:
        return DeployedCommit(
            root=root,
            status=SKIP,
            origin=origin,
            reason=(
                "does not prove the deployment: --allow-foreign-tree was given and this "
                f"run graded {_realpath(ctx.repo_root)}, not the deployed tree {root}"
            ),
        )
    commit = _git_in(root, "rev-parse", "HEAD")
    if not commit:
        return DeployedCommit(
            root=root,
            status=SKIP,
            origin=origin,
            reason=(
                f"{root} carries no readable git HEAD (not a git checkout, or git is "
                "unavailable), so the deployed commit was not reported"
            ),
        )
    branch = _git_in(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = "" if branch == "HEAD" else branch
    moved, evidence = _checkout_moved_after(root, ctx.service_start)
    if moved is True:
        # The checkout moved after the process started: the sha is what is on
        # disk, not what is running.  Report it, but never as the deployment.
        return DeployedCommit(
            root=root,
            commit=commit,
            branch=branch,
            status=SKIP,
            origin=origin,
            reason=(
                f"the checkout moved to {commit[:12]} after the process started "
                f"({evidence}), so the running code is not this commit — restart, then "
                "re-run this verifier"
            ),
        )
    if moved is None:
        # The sha is what is on disk; nothing binds it to the process, so it is
        # reported as unverified rather than as the deployment.
        return DeployedCommit(
            root=root,
            commit=commit,
            branch=branch,
            status=SKIP,
            origin=origin,
            reason=(
                "the checkout HEAD could not be bound to the process start (no reflog, or "
                "the service start time is unknown), so this is the checkout's HEAD and not "
                "proof of the running code"
            ),
        )
    return DeployedCommit(
        root=root,
        commit=commit,
        branch=branch,
        status=INFO,
        origin=origin,
        bound_to_process=True,
        reason=f"checkout unchanged since the process started ({evidence})",
    )


def deployed_commit_result(ctx: Context, deployed: DeployedCommit | None = None) -> Result:
    """The deployed-commit report line: same shape as a check, never a verdict."""
    deployed = deployed if deployed is not None else resolve_deployed_commit(ctx)
    if deployed.status == INFO:
        detail = deployed.commit[:12]
        detail += f" on {deployed.branch}" if deployed.branch else " (detached HEAD)"
        if deployed.reason:
            detail += f"; {deployed.reason}"
    else:
        detail = deployed.reason
        if deployed.commit:
            detail = f"checkout HEAD {deployed.commit[:12]} is NOT the running code — {detail}"
    return Result(
        "deployed_commit",
        "deployed commit",
        deployed.status,
        detail,
        deployed.to_json(),
        source=SOURCE_DEPLOYED_TREE,
        source_evidence=deployed.origin or "/proc/<MainPID>/cwd -> git -C <root> rev-parse HEAD",
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


#: Addresses that answer for every address on the host.
_WILDCARD_ADDRESSES = frozenset({"*", "0.0.0.0", "::", "[::]", ""})


def _listener_line_matches_host(local_address: str, host: str) -> bool:
    """True when an ``ss`` ``Local Address:Port`` value serves *host*.

    ``ss`` filters by port only, so a stale process on ``[::1]:11435`` would
    otherwise be reported as an owner of the ``127.0.0.1:11435`` listener the
    verifier probes.  A wildcard bind answers for every address and therefore
    matches; a different concrete address does not.
    """
    address = local_address.rsplit(":", 1)[0].strip()
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    if address in _WILDCARD_ADDRESSES:
        return True
    return address == host


def listener_pids(host: str, port: int) -> tuple[list[int], str]:
    """Return ``(pids, method)`` for the process listening on ``host:port``.

    ``ss`` is asked first because it names the owner directly, and its lines are
    filtered by the local address as well as the port (a listener on another
    address of the same port is a different listener).  When ``ss`` reports no
    owner (permissions, or no ``ss`` at all) the script falls back to
    ``/proc/net/tcp`` plus a scan of ``/proc/<pid>/fd``.  An empty list means
    "nobody is listening", which the identity check treats as a failure.
    """
    try:
        proc = _run(["ss", "-H", "-ltnp", f"sport = :{port}"])
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        pids: set[int] = set()
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4 or not _listener_line_matches_host(fields[3], host):
                continue
            pids.update(int(match) for match in re.findall(r"pid=(\d+)", line))
        if pids:
            return sorted(pids), "ss"
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


def request_headers(
    ctx: "Context",
    *,
    key: str | None = None,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    declared_content_length: int | None = None,
) -> list[tuple[str, str]]:
    """Build the request headers, with exactly one ``Host``.

    ``http.client`` would add a ``Host`` of its own (hence ``skip_host=True`` at
    the call site), and adding ours on top of that emitted *two* ``Host``
    headers — invalid per RFC 7230 §5.4 and accepted only because this
    uvicorn/h11 tolerates it.  A caller-supplied ``Host`` wins; nothing else is
    ever duplicated.
    """
    supplied = {name.lower() for name in (headers or {})}
    built: list[tuple[str, str]] = []
    if "host" not in supplied:
        built.append(("Host", ctx.tls_server_name if ctx.scheme == "https" else ctx.host))
    built.append(("Connection", "close"))
    if key:
        built.append(("Authorization", f"Bearer {key}"))
    built.extend((name, value) for name, value in (headers or {}).items())
    if declared_content_length is not None:
        built.append(("Content-Length", str(declared_content_length)))
    elif body is not None:
        built.append(("Content-Length", str(len(body))))
    return built


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
            # skip_host: request_headers() owns the single Host header.
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in request_headers(
                ctx,
                key=key,
                headers=headers,
                body=body,
                declared_content_length=declared_content_length,
            ):
                conn.putheader(name, value)
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


def load_keys_file(path: Path) -> dict[str, Any]:
    """Load a Guardian key store from an explicit path.

    A fresh worktree has no key store: ``config/guardian.keys.yaml`` is a
    gitignored secret (``.gitignore:68``), so a run that grades the *live* API
    from a review worktree sees an empty mapping and cannot observe anything
    (#92).  ``--keys-file`` points at the deployment's store instead.

    Returns the same token -> entry mapping ``auth.load_api_keys()`` returns; an
    unreadable or non-mapping document raises so ``prepare`` can report it.
    """
    import yaml

    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, Mapping):
        raise TypeError(f"{path} is not a token -> entry mapping")
    return {str(token): entry for token, entry in loaded.items()}


def _no_key_reason(ctx: "Context", needed: str) -> str:
    """Explain *why* a check has no usable key, without guessing at scopes.

    "no key grants the client scope" was true in wording and wrong in meaning:
    on a foreign tree the store is a gitignored secret that simply is not there,
    so the reader went looking for a scope problem that did not exist (#92).
    The three states need three different operator actions.
    """
    if ctx.keys_load_error:
        return f"the key store could not be loaded: {ctx.keys_load_error}"
    where = ctx.keys_file or "the key store"
    if ctx.keys_file_missing:
        return (
            f"no key store at {where} (it is a gitignored secret, so a fresh worktree has none; "
            "pass --keys-file with the deployment's config/guardian.keys.yaml to observe the live API)"
        )
    if ctx.keys_file_empty:
        return f"the key store at {where} holds no keys"
    return f"no key grants the {needed} scope ({len(ctx.keys)} key(s) in the store, none usable for it)"


def _parse_unix_timestamp(raw: Any) -> float | None:
    """Parse a ``systemctl show --timestamp=unix`` value.

    systemd prints UNIX timestamps with an ``@`` prefix (``@1792938421``), so a
    bare ``str.isdigit()`` check would silently lose the restart boundary and
    turn the post-restart file window into "no files since" — a quiet SKIP where
    a real check belongs.  ``systemd`` also prints ``n/a`` for a unit that never
    started; that returns ``None``.
    """
    text = str(raw or "").strip().lstrip("@").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


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
        parsed = _parse_unix_timestamp(ctx.service.get(prop))
        if parsed is not None:
            ctx.service_start = parsed
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

        if ctx.keys_file:
            ctx.keys = load_keys_file(Path(ctx.keys_file))
        else:
            ctx.keys = _auth.load_api_keys()
            ctx.keys_file = str(getattr(_auth, "API_KEYS_FILE", "")) or None
        if ctx.keys_file:
            ctx.keys_file_missing = not Path(ctx.keys_file).exists()
        ctx.keys_file_empty = not ctx.keys and not ctx.keys_file_missing
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
        ctx.keys_load_error = _clean(exc)
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
        return Result("queue_wait_budget", title, SKIP, _no_key_reason(ctx, "client"))
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


def _module_function_names(path: Path) -> set[str]:
    """Top-level function names defined in *path* (parsed, never imported)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def check_dead_code_removed(ctx: Context) -> Result:
    """The code the process loaded carries no dead handler and no dead route.

    Two prongs, both anchored on the running deployment:

    * the module file *the process loaded* — ``<deployed root>/app/proxy/server.py``
      where the deployed root comes from ``/proc/<MainPID>/cwd``, accepted only
      when **neither** its mtime **nor** its ctime is newer than the process
      start: a file replaced after the process started is not the code that is
      running, and mtime alone is preserved by ``rsync -a``/``install -p``
      (ctime cannot be preserved by any userspace tool, so it catches a
      timestamp-preserving replacement).  It is parsed with :mod:`ast`; nothing
      is imported, so the script's own sys.path is irrelevant.
    * the running process must not serve ``/api/version`` (the Ollama-shaped route
      the contract says is gone): a 404 from the probed listener.

    The script's own tree is never read, so a checkout that already dropped the
    dead symbol cannot make a stale process pass.
    """
    title = "dead get_version removed from the running code"
    ctx.require_service()
    observed: dict[str, Any] = {
        "attribute": DEAD_CODE_SYMBOL,
        "route": DEAD_CODE_ROUTE,
        "process_start": ctx.service_start,
    }
    key = ctx.client_key
    route = ctx.http("GET", DEAD_CODE_ROUTE, key=key.token if key else None)
    observed["route_status"] = route.status
    if route.status != 404:
        return Result(
            "dead_code_removed",
            title,
            FAIL,
            f"the running process answers GET {DEAD_CODE_ROUTE} with {route.status}; "
            "the contract removed that route (#47)",
            observed,
        )

    deployed = ctx.deployed_root
    if deployed is None:
        reason = ctx.deployment.reason if ctx.deployment is not None else "no deployment facts"
        return Result(
            "dead_code_removed",
            title,
            SKIP,
            f"the deployed root is unknown ({reason}), so the loaded module cannot be read",
            observed,
        )
    module_path = Path(deployed) / DEAD_CODE_MODULE
    observed["module_path"] = str(module_path)
    try:
        info = module_path.stat()
    except OSError as exc:
        return Result(
            "dead_code_removed",
            title,
            SKIP,
            f"{module_path} is unreadable ({_clean(exc)}): this root is not where the "
            "process loads its code",
            observed,
        )
    observed["module_mtime"] = info.st_mtime
    observed["module_ctime"] = info.st_ctime
    if ctx.service_start is None:
        return Result(
            "dead_code_removed",
            title,
            SKIP,
            f"the process start time is unknown, so {module_path} cannot be proven to be "
            "the code the process loaded",
            observed,
        )
    # Both stamps, because either alone is forgeable: `rsync -a`/`install -p`
    # preserve mtime but always bump ctime, and a plain `touch -d` sets mtime but
    # also bumps ctime.  A file whose ctime is newer than the process start was
    # (re)placed after the process read it, whatever its mtime claims
    # (PR-Piet, #84).
    for label, stamp in (("mtime", info.st_mtime), ("ctime", info.st_ctime)):
        if stamp > float(ctx.service_start):
            return Result(
                "dead_code_removed",
                title,
                FAIL,
                f"{module_path} was modified after the process started "
                f"({label} {datetime.fromtimestamp(stamp, timezone.utc).isoformat()} > "
                f"process start "
                f"{datetime.fromtimestamp(float(ctx.service_start), timezone.utc).isoformat()}), "
                "so the running process is not that code",
                observed,
            )
    try:
        names = _module_function_names(module_path)
    except (OSError, SyntaxError) as exc:
        return Result(
            "dead_code_removed",
            title,
            FAIL,
            f"cannot read {module_path} ({_clean(exc)}); the loaded code cannot be judged",
            observed,
        )
    observed["symbol_present"] = DEAD_CODE_SYMBOL in names
    if DEAD_CODE_SYMBOL in names:
        return Result(
            "dead_code_removed",
            title,
            FAIL,
            f"the code the process loaded ({module_path}) still defines {DEAD_CODE_SYMBOL} — "
            "the running deployment predates the removal",
            observed,
        )
    return Result(
        "dead_code_removed",
        title,
        PASS,
        f"the loaded module {module_path} (mtime and ctime older than the process start) does "
        f"not define {DEAD_CODE_SYMBOL}, and the process returns 404 for GET {DEAD_CODE_ROUTE}",
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
        return Result("metrics_gate", title, SKIP, _no_key_reason(ctx, "client"), observed)
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


def check_destination_allowlist(ctx: Context) -> Result:
    """The policy the running process holds comes from config, hops flagged.

    Read from ``GET /api/status`` → ``policy``, i.e. the live policy object's own
    report.  The old implementation rebuilt the policy from a YAML file next to
    the script, which reported ``PASS`` while the process ran the shipped
    default — the false green light of issue #84.
    """
    title = "destination allowlist from the live policy, cleartext hops flagged"
    ctx.require_service()
    key = ctx.admin_key or ctx.client_key
    if key is None:
        return Result(
            "destination_allowlist",
            title,
            SKIP,
            _no_key_reason(ctx, "admin or client (for /api/status)"),
        )
    response = ctx.http("GET", "/api/status", key=key.token)
    if response.status == 403:
        return Result(
            "destination_allowlist",
            title,
            SKIP,
            f"GET /api/status requires the admin scope: {_clean(response.text(), 120)}",
            {"status": 403},
        )
    if response.status != 200:
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            f"GET /api/status returned {response.status}",
            {"status": response.status},
        )
    body = response.json()
    policy = body.get("policy") if isinstance(body, Mapping) else None
    if not isinstance(policy, Mapping):
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            "the running process reports no 'policy' block on /api/status, so the live "
            "policy cannot be judged (the deployed code predates this verifier; a YAML "
            "file next to the script is deliberately not used instead)",
            {"status": 200, "policy": None},
        )
    source = str(policy.get("source") or "unknown")
    entries = [
        {
            "host": str(entry.get("host")),
            "ports": [
                port for port in entry.get("ports") or () if isinstance(port, int) and not isinstance(port, bool)
            ],
            "cleartext_credentials": bool(entry.get("cleartext_credentials")),
        }
        for entry in policy.get("entries") or ()
        if isinstance(entry, Mapping) and entry.get("host") is not None
    ]
    observed: dict[str, Any] = {
        "source": source,
        "entries": len(entries),
        "cleartext_hops": policy.get("cleartext_hops"),
        "cleartext_hosts": sorted(
            entry["host"] for entry in entries if entry["cleartext_credentials"]
        ),
    }
    if source != "config":
        return Result(
            "destination_allowlist",
            title,
            FAIL,
            f"the live policy source is {source!r} (expected 'config'): "
            "the running process has no destination_policy block in its config",
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
        return Result("admission_bounds", title, SKIP, _no_key_reason(ctx, "client"), observed)
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
        return Result("metadata_audit", title, SKIP, _no_key_reason(ctx, "client"))
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
        return Result("endpoint_confinement", title, SKIP, _no_key_reason(ctx, "client"))
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
        return Result("backend_death_seam", title, SKIP, _no_key_reason(ctx, "admin or client (for /api/status)"))
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
            _no_key_reason(ctx, "capture-export (for /api/capture/status)"),
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
        return Result("cloud_catalog_credentials", title, SKIP, _no_key_reason(ctx, "admin"))
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


@dataclass(frozen=True)
class CheckSpec:
    """One check, its title, and where its evidence comes from (issue #84).

    The source is the single place that decides what a check is allowed to claim
    in a foreign tree: only :data:`SOURCE_LIVE_API` checks stand on their own
    there, because they read the live service and nothing about the checkout this
    script lives in.
    """

    check_id: str
    title: str
    source: str
    evidence: str
    run: Callable[[Context], Result]


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "service_identity",
        "service active and MainPID owns the listener",
        SOURCE_PROCESS,
        f"systemctl show {DEFAULT_UNIT} (ActiveState, MainPID) + ss//proc listener owners",
        check_service_identity,
    ),
    CheckSpec(
        "queue_wait_budget",
        "queue wait budget enforced",
        SOURCE_DEPLOYED_CONFIG,
        "GET /v1/queue/status vs the deployed config/global.settings.yaml",
        check_queue_wait_budget,
    ),
    CheckSpec(
        "capture_modes",
        "capture media modes",
        SOURCE_DEPLOYED_FILE,
        "GET /api/capture/status + <capture_root>/media modes under the deployed root",
        check_capture_modes,
    ),
    CheckSpec(
        "dead_code_removed",
        "dead get_version removed from the running code",
        SOURCE_DEPLOYED_CODE,
        "/proc/<MainPID>/cwd/app/proxy/server.py (AST) + GET /api/version on the process",
        check_dead_code_removed,
    ),
    CheckSpec(
        "privilege_scope_grace",
        "privilege scopes: legacy grace, key stays client-only",
        SOURCE_JOURNAL,
        "journalctl -u <unit> scope-grace lines + the deployed key store declaration",
        check_privilege_scope_grace,
    ),
    CheckSpec(
        "metrics_gate",
        "metrics gate: 401 without a key, 200 with a client key",
        SOURCE_LIVE_API,
        "GET /metrics (anonymous and with a client key)",
        check_metrics_gate,
    ),
    CheckSpec(
        "destination_allowlist",
        "destination allowlist from the live policy, cleartext hops flagged",
        SOURCE_LIVE_API,
        "GET /api/status -> policy (the running process's own policy report)",
        check_destination_allowlist,
    ),
    CheckSpec(
        "admission_bounds",
        "admission bounds resolved and counted",
        SOURCE_DEPLOYED_CONFIG,
        "GET /v1/queue/status, POST /v1/chat/completions, GET /metrics vs the deployed config",
        check_admission_bounds,
    ),
    CheckSpec(
        "metadata_audit",
        "metadata audit: one record, no payload",
        SOURCE_JOURNAL,
        "one allowed GET /api/tags + its audit record in journald",
        check_metadata_audit,
    ),
    CheckSpec(
        "log_sanitizer",
        "log sanitizer: no Bearer value in the log",
        SOURCE_JOURNAL,
        "journalctl -u <unit> since the restart",
        check_log_sanitizer,
    ),
    CheckSpec(
        "endpoint_confinement",
        "endpoint confinement",
        SOURCE_LIVE_API,
        "GET /v1/../slots, GET /v1/%252e%252e/slots, GET /v1/models",
        check_endpoint_confinement,
    ),
    CheckSpec(
        "backend_death_seam",
        "backend-death seam on /api/status",
        SOURCE_LIVE_API,
        "GET /api/status (backend_state, backend_death, caretaker)",
        check_backend_death_seam,
    ),
    CheckSpec(
        "capture_pipeline",
        "capture pipeline enabled and writing",
        SOURCE_LIVE_API,
        "GET /api/capture/status",
        check_capture_pipeline,
    ),
    CheckSpec(
        "key_store",
        "key store loads guardian.keys.yaml",
        SOURCE_DEPLOYED_FILE,
        "the deployed config/guardian.keys.yaml as the auth store loads it",
        check_key_store,
    ),
    CheckSpec(
        "config_deep_merge",
        "config deep-merges global.settings.yaml",
        SOURCE_DEPLOYED_CONFIG,
        "the deployed config/global.settings.yaml as the loader merges it",
        check_config_deep_merge,
    ),
    CheckSpec(
        "cloud_catalog_credentials",
        "cloud catalog: no broken credentials",
        SOURCE_LIVE_API,
        "GET /api/cloud/catalog",
        check_cloud_catalog_credentials,
    ),
)


# ── Runner ─────────────────────────────────────────────────────────────


def foreign_tree_skip(ctx: Context, spec: CheckSpec) -> Result:
    """The SKIP a tree-dependent check reports when the tree is foreign.

    Never a verdict: in a foreign tree the evidence this check grades is not the
    deployment's, so "does not prove the deployment" is the only honest answer.
    """
    deployment = ctx.deployment
    if deployment is None or not deployment.known:
        why = deployment.reason if deployment is not None else "no deployment facts"
        where = f"the deployed root could not be determined ({why})"
    else:
        where = (
            f"the script's tree {_realpath(ctx.repo_root)} is not the deployed tree "
            f"{deployment.root}"
        )
    return Result(
        spec.check_id,
        spec.title,
        SKIP,
        f"does not prove the deployment: --allow-foreign-tree was given and {where}",
        {"source": spec.source, "foreign_tree": True},
    )


def run_checks(ctx: Context) -> list[Result]:
    """Run every check; an unexpected exception is a loud FAIL, never a skip."""
    results: list[Result] = []
    for spec in CHECKS:
        if ctx.foreign_tree and spec.source not in SOURCES_TREE_INDEPENDENT:
            result = foreign_tree_skip(ctx, spec)
        else:
            try:
                result = spec.run(ctx)
            except ProbeError as exc:
                result = Result(spec.check_id, spec.title, FAIL, _clean(exc))
            except Exception as exc:  # noqa: BLE001 - one broken check must not stop the run
                result = Result(spec.check_id, spec.title, FAIL, f"{type(exc).__name__}: {_clean(exc)}")
        result.source = spec.source
        result.source_evidence = spec.evidence
        results.append(result)
    return results


def exit_code_for(results: Sequence[Result]) -> int:
    """``0`` all passed, ``1`` any failure, ``2`` skips without a failure.

    ``INFO`` results (the deployed-commit report) are deliberately ignored: they
    are facts, not verdicts, so they can never change the exit code.
    """
    if any(result.status == FAIL for result in results):
        return EXIT_FAIL
    if any(result.status == SKIP for result in results):
        return EXIT_SKIP
    return EXIT_PASS


def _print_result(result: Result) -> None:
    """One line per check, naming the source before the verdict's detail."""
    line = f"[{result.status}] {result.title}"
    if result.source:
        line += f" — source={result.source}"
        if result.source_evidence:
            line += f" ({result.source_evidence})"
    if result.detail:
        line += f"; {result.detail}"
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
    parser.add_argument(
        "--keys-file",
        default=None,
        help=(
            "read API keys from this store instead of the checkout's (default: the "
            "checkout's own store). Use the deployment's config/guardian.keys.yaml when "
            "this run grades the live API from another tree; the store is a gitignored "
            "secret, so a fresh worktree has none"
        ),
    )
    parser.add_argument(
        "--allow-foreign-tree",
        action="store_true",
        help=(
            "run even when this checkout is not the deployed tree (review/CI). The run "
            "then does NOT prove the deployment: every check whose source is not the live "
            "API reports SKIP instead of a verdict"
        ),
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
        keys_file=args.keys_file,
        timeout=float(args.timeout),
        journal_lines=int(args.journal_lines),
        http=lambda *a, **kw: (_ for _ in ()).throw(ProbeError("http surface not built")),
        systemctl=systemctl_show,
        journal=lambda since, lines: read_journal(args.unit, since, lines),
        read_proc_cwd=read_proc_cwd,
    )
    ctx.http = make_http(ctx)
    return ctx


def _refuse(ctx: Context, args: argparse.Namespace, text: str) -> int:
    """Print the refusal (no verdict) and return the refused exit code."""
    print(text, file=sys.stderr)
    deployment = ctx.deployment
    if args.json:
        document = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "unit": ctx.unit,
            "base_url": f"{ctx.scheme}://{ctx.host}:{ctx.port}",
            "refused": True,
            "reason": (deployment.reason if deployment is not None else ""),
            "deployed_root": str(deployment.root) if deployment is not None and deployment.known else None,
            "deployment_origin": deployment.origin if deployment is not None else "",
            "script_root": str(_realpath(ctx.repo_root)),
            "allow_foreign_tree": False,
            "exit_code": EXIT_REFUSED,
            "ok": False,
            "warnings": [],
            "checks": [],
        }
        print(json.dumps(document, indent=2, sort_keys=True))
    return EXIT_REFUSED


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
    # Issue #84: never grade a tree that is not the running deployment.
    deployment = resolve_deployment(ctx)
    ctx.deployment = deployment
    ctx.allow_foreign_tree = bool(args.allow_foreign_tree)
    if tree_matches(deployment, ctx.repo_root) is not True:
        if not ctx.allow_foreign_tree:
            return _refuse(ctx, args, refusal_message(deployment, ctx.repo_root))
        ctx.foreign_tree = True
        if deployment.known:
            ctx.warnings.append(
                "--allow-foreign-tree: this checkout "
                f"({_realpath(ctx.repo_root)}) is not the deployed tree ({deployment.root}); "
                "the run does NOT prove the deployment"
            )
        else:
            ctx.warnings.append(
                "--allow-foreign-tree: the deployed root could not be determined "
                f"({deployment.reason}); no check can prove the deployment"
            )
    prepare(ctx)
    # Issue #85: the verdict must name the commit it graded.  Reported, never
    # graded — an old commit deployed on purpose is legitimate.
    deployed = resolve_deployed_commit(ctx)
    deployed_line = deployed_commit_result(ctx, deployed)
    results = run_checks(ctx)
    code = exit_code_for(results)
    if args.json:
        document = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "unit": ctx.unit,
            "base_url": f"{ctx.scheme}://{ctx.host}:{ctx.port}",
            "tls_verified": not ctx.insecure and ctx.scheme == "https",
            "service_reachable": ctx.service_reachable,
            "deployed_root": str(ctx.deployed_root) if ctx.deployed_root is not None else None,
            "deployment_origin": ctx.deployment.origin if ctx.deployment is not None else "",
            "deployed_commit": deployed.to_json(),
            "script_root": str(_realpath(ctx.repo_root)),
            "allow_foreign_tree": ctx.allow_foreign_tree,
            "proves_the_deployment": not ctx.foreign_tree,
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
            f" (deployed root {ctx.deployed_root}, from {ctx.deployment.origin})"
            if ctx.deployed_root is not None and not ctx.foreign_tree
            else ""
        )
        + (
            " (service unreachable — service checks will fail)"
            if not ctx.service_reachable
            else ""
        )
    )
    _print_result(deployed_line)
    for result in results:
        _print_result(result)
    passed = sum(1 for result in results if result.status == PASS)
    failed = sum(1 for result in results if result.status == FAIL)
    skipped = sum(1 for result in results if result.status == SKIP)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if ctx.foreign_tree:
        print(
            "⚠️  --allow-foreign-tree: this run graded "
            f"{_realpath(ctx.repo_root)}, not the deployment"
            + (f" ({ctx.deployed_root})" if ctx.deployed_root is not None else "")
            + " — it proves nothing about what is running"
        )
    elif code == EXIT_PASS:
        print("✅ the running deployment matches the contract")
    elif code == EXIT_SKIP:
        print("⚠️  no failures, but something could not be verified — read the SKIP reasons")
    else:
        print("❌ the running deployment does NOT match the contract")
    return code


if __name__ == "__main__":
    sys.exit(main())
