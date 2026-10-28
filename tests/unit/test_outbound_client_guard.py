"""Structural guard: every outbound httpx client in ``app/`` goes through G1-02.

Source of record: ``docs/decisions/G1-02-destination-and-credentials.md`` §2.1
and its amendment of 2026-10-28.

A bare ``httpx.AsyncClient(...)``/``httpx.Client(...)`` inside ``app/`` is an
unguarded egress path: no allowlist check, no pinned validated address, no
redirect refusal, and — since G1-03 part 2 — no audit record when the hop is
refused (it is not refused at all). Two such constructions survived the G1-02
conversion in ``app/local_inference/ollama.py`` (issue #65) and were only found
later by the audit work, so the class of omission is a CI failure now instead
of a review hope.

The scan is AST-based on purpose: a regex over raw text also matches the
argument lists, docstrings and comments that *describe* the pattern, and it
cannot see through ``import httpx as h`` / ``from httpx import AsyncClient``.
Each finding is reported as ``file:line`` with its enclosing class/function, so
"allowed" is a decision about a *site*, not about a file.

Legitimate exception sites are named in :data:`ALLOWLIST` with a concrete
reason. An entry is only accepted while it still matches real code (a stale
exemption fails), and the allowlisted modules must stay unreachable from the
gateway app (a site that becomes an in-process egress path fails here even
before someone "converts" it).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"

#: The one module allowed to construct the raw httpx client: the guard factory
#: itself. Everything else in ``app/`` must call
#: ``destination_policy.guarded_async_client(...)``.
GUARD_FACTORY = "app/destination_policy.py"

#: Canonical ``module.attribute`` names of the raw client constructors.
FORBIDDEN = frozenset({"httpx.AsyncClient", "httpx.Client"})

#: Sites that may construct a raw client, keyed by ``(repo-relative path,
#: dotted class/function name)``, each with the reason it is outside the policy.
#: Keep the reason concrete: "it is legacy" is not a reason.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "app/tweaker/finetune_v2_runner.py",
        "GuardianV2ProbeRunner.__init__",
    ): (
        "operator-run maintenance CLI: talks to Guardian's own public endpoint "
        "with the caller's key, outside the gateway process. Not importable "
        "from the gateway app (pinned by the reachability test below)."
    ),
    (
        "app/tweaker/legacy/benchmark_suite_v1.py",
        "BenchmarkSuite.run_single_test",
    ): (
        "operator-run legacy benchmark harness, standalone and synchronous; "
        "invoked via scripts/legacy/test_benchmark_v1.py only, never imported "
        "by the gateway app."
    ),
    (
        "app/tweaker/legacy/model_finetune_v1.py",
        "GuardianModelFinetuner.__init__",
    ): (
        "operator-run legacy finetune harness (synchronous httpx.Client) "
        "invoked via scripts/finetune_model_config.py only, never imported by "
        "the gateway app."
    ),
}


@dataclass(frozen=True)
class ClientConstruction:
    """One raw ``httpx`` client construction found in a source file."""

    path: str
    line: int
    qualname: str
    constructor: str

    @property
    def site(self) -> tuple[str, str]:
        return (self.path, self.qualname)

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.qualname} -> {self.constructor}("


def _httpx_import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local names to canonical ``httpx`` members imported into a module."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    aliases[alias.asname or "httpx"] = "httpx"
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"httpx.{alias.name}"
    return aliases


def _canonical_constructor(
    func: ast.expr, aliases: dict[str, str]
) -> str | None:
    """Return ``httpx.AsyncClient``/``httpx.Client`` for a call target, else None."""
    if isinstance(func, ast.Name):
        canonical = aliases.get(func.id)
        return canonical if canonical in FORBIDDEN else None
    if isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Name):
            module = aliases.get(owner.id)
            if module == "httpx":
                canonical = f"httpx.{func.attr}"
                return canonical if canonical in FORBIDDEN else None
        # ``httpx.AsyncClient`` used without importing the name ``httpx`` at all
        # (a stale import the compiler would reject) still counts.
        if isinstance(owner, ast.Name) and owner.id == "httpx":
            canonical = f"httpx.{func.attr}"
            return canonical if canonical in FORBIDDEN else None
    return None


def scan_constructions(source: str, path: str) -> list[ClientConstruction]:
    """Return every raw httpx client construction in *source*."""
    tree = ast.parse(source, filename=path)
    aliases = _httpx_import_aliases(tree)
    found: list[ClientConstruction] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _visit_container(self, node: ast.AST) -> None:
            self.stack.append(node.name)  # type: ignore[attr-defined]
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_container(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_container(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_container(node)

        def visit_Call(self, node: ast.Call) -> None:
            constructor = _canonical_constructor(node.func, aliases)
            if constructor is not None:
                found.append(
                    ClientConstruction(
                        path=path,
                        line=node.lineno,
                        qualname=".".join(self.stack) or "<module>",
                        constructor=constructor,
                    )
                )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def _app_sources() -> list[tuple[str, Path]]:
    """Every Python module under ``app/`` as ``(repo-relative path, path)``."""
    return sorted(
        (str(path.relative_to(REPO_ROOT)), path)
        for path in APP_ROOT.rglob("*.py")
    )


def _module_constructions() -> list[ClientConstruction]:
    findings: list[ClientConstruction] = []
    for rel, path in _app_sources():
        findings.extend(scan_constructions(path.read_text(encoding="utf-8"), rel))
    return findings


# ── The guard itself ──────────────────────────────────────────────────


def test_no_unguarded_httpx_client_in_app() -> None:
    """No module under ``app/`` may build a raw httpx client (see #65).

    Use ``guarded_async_client(...)`` from ``app.destination_policy``; if the
    site genuinely cannot (it runs outside the gateway process), add an
    ``ALLOWLIST`` entry naming *why*.
    """
    offenders = [
        finding
        for finding in _module_constructions()
        if finding.path != GUARD_FACTORY and finding.site not in ALLOWLIST
    ]
    assert not offenders, (
        "Raw httpx client construction(s) bypass the G1-02 destination guard "
        "(issue #65) — use destination_policy.guarded_async_client(...) or add "
        "an ALLOWLIST entry with a reason:\n  "
        + "\n  ".join(str(finding) for finding in offenders)
    )


def test_the_guard_factory_still_builds_the_client() -> None:
    """The exemption for ``destination_policy.py`` is real, not a stale hole."""
    factory_findings = [
        finding
        for finding in _module_constructions()
        if finding.path == GUARD_FACTORY
    ]
    assert factory_findings, (
        f"{GUARD_FACTORY} no longer constructs an httpx client — the exemption is "
        "stale and the guard factory moved somewhere this test does not cover"
    )


def test_allowlist_entries_match_real_code() -> None:
    """Every exemption must still name a construction that exists."""
    sites = {finding.site for finding in _module_constructions()}
    stale = [entry for entry in ALLOWLIST if entry not in sites]
    assert not stale, (
        "Stale ALLOWLIST entries (code moved or was converted — drop the "
        f"exemption): {stale}"
    )


def test_allowlist_reasons_are_substantive() -> None:
    """An exemption is a reviewed decision; it needs a usable reason."""
    thin = [
        entry
        for entry, reason in ALLOWLIST.items()
        if len(reason.strip()) < 40 or "legacy" == reason.strip().lower()
    ]
    assert not thin, f"ALLOWLIST entries without a concrete reason: {thin}"


def test_allowlisted_modules_are_not_reachable_from_the_gateway_app() -> None:
    """``app/tweaker/**`` must stay outside the gateway: scripts only.

    The allowlist says these clients run in operator-invoked tooling. If the
    gateway ever imports them, the exemption is wrong and the raw client is back
    on a request path.
    """
    offending_importers: list[str] = []
    for rel, path in _app_sources():
        if rel.startswith("app/tweaker/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(module == "app.tweaker" or module.startswith("app.tweaker.")
                   for module in modules):
                offending_importers.append(f"{rel}:{node.lineno} imports {node.module}")
    assert not offending_importers, (
        "The gateway app imports an allowlisted outbound-client module; those "
        "clients are only exempt because they never run inside the gateway:\n  "
        + "\n  ".join(offending_importers)
    )


# ── Proof the guard is not vacuous ────────────────────────────────────


#: The exact pre-fix lines from issue #65, plus the two import spellings the
#: scanner must see through. The scanner runs on source text, so proving it on
#: a planted snippet needs no file on disk and cannot be defeated by the fix.
PLANTED = '''
import httpx
import httpx as h
from httpx import AsyncClient
from httpx import Client as SyncClient


async def _pre_fix_chat_hop(request_timeout):
    client = httpx.AsyncClient(timeout=request_timeout)          # issue #65
    return client


def _aliased(url):
    return h.AsyncClient(base_url=url)


def _imported_name(url):
    return AsyncClient(timeout=5.0)


class Bridge:
    def hop(self, url):
        return SyncClient(base_url=url)


def _guarded(url):
    from app.destination_policy import guarded_async_client
    return guarded_async_client(purpose="test")
'''


def test_scanner_detects_planted_bare_constructions() -> None:
    """A deliberately planted bare construction must be reported."""
    found = scan_constructions(PLANTED, "planted.py")
    sites = {(finding.qualname, finding.constructor) for finding in found}

    assert ("_pre_fix_chat_hop", "httpx.AsyncClient") in sites, sites
    assert ("_aliased", "httpx.AsyncClient") in sites, sites
    assert ("_imported_name", "httpx.AsyncClient") in sites, sites
    assert ("Bridge.hop", "httpx.Client") in sites, sites
    # The guarded call is not a finding: only raw constructions are.
    assert len(found) == 4, [str(finding) for finding in found]


def test_scanner_is_not_fooled_by_comments_or_docstrings() -> None:
    """Text that merely *mentions* the pattern is not a construction."""
    source = '''
"""Uses httpx.AsyncClient(url) nowhere near a call site."""

# httpx.Client(timeout=5) in a comment is not a construction.
CONSTRUCTOR_NAME = "httpx.AsyncClient("
MODULE = object
'''
    assert scan_constructions(source, "mentions.py") == []


@pytest.mark.parametrize(
    ("rel", "qualname", "constructor"),
    [
        ("planted.py", "_pre_fix_chat_hop", "httpx.AsyncClient"),
        ("planted.py", "Bridge.hop", "httpx.Client"),
    ],
)
def test_planted_findings_are_reported_as_file_line(
    rel: str, qualname: str, constructor: str
) -> None:
    """Findings must be actionable (``path:line``), not a bare count."""
    rendered = [
        str(finding)
        for finding in scan_constructions(PLANTED, rel)
        if finding.qualname == qualname
    ]
    assert rendered, f"{qualname} not found"
    assert rendered[0].startswith(f"{rel}:")
    assert constructor + "(" in rendered[0]
