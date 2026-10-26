"""G0-01 guard: the endpoint/auth matrix must match the registered routes.

``docs/G0_ENDPOINT_AUTH_MATRIX.md`` is the reviewed baseline for
``docs/ROADMAP.md`` G0-01. A matrix is only worth having if it cannot drift away
from the code, so this module collects the routes of both FastAPI apps the way
the application registers them and compares that set with the documented rows.

Nothing leaves the process: the two app objects are imported (the same ones
``tests/unit/test_main.py`` and ``tests/unit/test_server.py`` already import),
routes are read from ``app.routes``, and the matrix is parsed as text. No server
is started, no network or model call is made.

Failure messages name the offending routes, because the useful answer to a red
build here is "add or delete these rows", not "something changed".
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path

import pytest
import starlette.routing

from app import main as dashboard_module
from app.proxy import server as proxy_module

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "docs" / "G0_ENDPOINT_AUTH_MATRIX.md"
TESTS_DIR = REPO_ROOT / "tests"

PRIVILEGE_CLASSES = {"public", "client", "admin", "operator"}
MOUNT_METHOD = "MOUNT"
WEBSOCKET_METHOD = "WEBSOCKET"
OTHER_METHOD = "OTHER"
HEAD_METHOD = "HEAD"

#: ``| 1 | proxy | GET | `/healthz` | `app/proxy/server.py:1200` | none | public | none |``
#: The line is optional only for framework-owned handlers; repo code stays pinned
#: (enforced in ``test_every_row_is_classified_and_code_linked``).
_HANDLER_RE = re.compile(r"^`?(?P<file>[A-Za-z0-9_./-]+\.py)(?::(?P<line>\d+))?`?$")
_BACKTICKED_RE = re.compile(r"`([^`]+)`")
_TOTAL_RE = re.compile(
    r"Route count:\s*(?P<total>\d+)\s*—\s*(?P<proxy>\d+)\s*proxy,\s*(?P<dashboard>\d+)\s*dashboard"
)
_UNCOVERED_HEADER_RE = re.compile(r"(?P<count>\d+) of the (?P<total>\d+) routes")


# ── Route collection ───────────────────────────────────────────────────


def _method_less_kind(route) -> str:
    """Classify a route object that has no ``methods`` set.

    Only a real ``Mount`` is a mount. Anything else (``WebSocketRoute`` today,
    a future route type tomorrow) gets its own kind so that it surfaces as an
    undocumented route instead of being silently filed as ``MOUNT``.
    """
    if isinstance(route, starlette.routing.Mount):
        return MOUNT_METHOD
    if isinstance(route, starlette.routing.WebSocketRoute):
        return WEBSOCKET_METHOD
    return OTHER_METHOD


def _collect_routes(application) -> dict[tuple[str, str], object]:
    """Return ``{(METHOD, path): route}`` for one app.

    ``HEAD`` is folded into ``GET`` because Starlette adds it to every ``GET``
    route. Method-less routes are classified by ``_method_less_kind``: a
    ``Mount`` becomes one ``MOUNT`` entry, a WebSocket route becomes
    ``WEBSOCKET`` and an unknown method-less route becomes ``OTHER``.
    """
    collected: dict[tuple[str, str], object] = {}
    for route in application.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if not methods:
            collected[(_method_less_kind(route), path)] = route
            continue
        for method in set(methods) - {HEAD_METHOD}:
            collected[(method, path)] = route
    return collected


@pytest.fixture(scope="module")
def dashboard_app():
    """The dashboard app with its production route set.

    ``app/main.py`` mounts ``/static`` only when the working directory is the
    repo root. The mount is part of the production surface, so restore it
    through the app's own helper instead of letting the guard depend on the
    invocation directory.
    """
    application = dashboard_module.app
    static_dir = REPO_ROOT / "app" / "ui" / "static"
    if not any(getattr(route, "path", None) == "/static" for route in application.routes):
        dashboard_module._configure_static_mount(application, static_dir)
    return application


@pytest.fixture(scope="module")
def collected(dashboard_app) -> dict[tuple[str, str], object]:
    """``{(surface, method, path): route}`` for both apps."""
    routes: dict[tuple[str, str, str], object] = {}
    for surface, application in (
        ("proxy", proxy_module.app),
        ("dashboard", dashboard_app),
    ):
        for (method, path), route in _collect_routes(application).items():
            routes[(surface, method, path)] = route
    return routes


# ── Matrix parsing ─────────────────────────────────────────────────────


def _matrix_text() -> str:
    assert MATRIX_PATH.is_file(), f"endpoint matrix is missing: {MATRIX_PATH}"
    return MATRIX_PATH.read_text(encoding="utf-8")


def _parse_rows(text: str) -> list[dict[str, str]]:
    """Parse the numbered table rows of the matrix.

    The header row (``#``) and the ``|---|`` separator are skipped; every data
    row carries exactly the eight documented columns.
    """
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 8 or not cells[0].isdigit():
            continue
        rows.append(
            {
                "index": cells[0],
                "surface": cells[1],
                "method": cells[2],
                "path": cells[3].strip("`"),
                "handler": cells[4],
                "auth": cells[5],
                "privilege": cells[6],
                "tests": cells[7],
            }
        )
    return rows


def _documented_keys(rows: list[dict[str, str]]) -> dict[tuple[str, str, str], str]:
    """``{(surface, method, path): row index}``, asserting no duplicate rows."""
    documented: dict[tuple[str, str, str], str] = {}
    duplicates: list[str] = []
    for row in rows:
        key = (row["surface"], row["method"], row["path"])
        if key in documented:
            duplicates.append(f"{row['method']} {row['path']} (rows {documented[key]} and {row['index']})")
        documented[key] = row["index"]
    assert not duplicates, "the endpoint matrix documents these routes more than once:\n  " + "\n  ".join(
        sorted(duplicates)
    )
    return documented


def _format_keys(keys) -> str:
    return "\n  ".join(f"{surface:9s} {method:6s} {path}" for surface, method, path in sorted(keys))


def _test_refs(cell: str) -> list[str]:
    """Return the test node ids referenced in a covering-tests cell."""
    return [token for token in _BACKTICKED_RE.findall(cell) if token.startswith("tests/")]


def _known_test_nodes() -> set[str]:
    """Every ``file::Class::test`` path defined under ``tests/``."""
    nodes: set[str] = set()
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        module = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node: ast.AST, prefix: str = "") -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes.add(f"{module}::{prefix}{child.name}")
                    walk(child, f"{prefix}{child.name}.")
                elif isinstance(child, ast.ClassDef):
                    walk(child, f"{prefix}{child.name}::")

        walk(tree)
    return nodes


# ── Handler references ─────────────────────────────────────────────────


def _doc_relative(path: str) -> str:
    """Normalize a source path the way the matrix writes it."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        parts = resolved.parts
        if "site-packages" in parts:
            return "/".join(parts[parts.index("site-packages") + 1 :])
        return resolved.as_posix()


def _handler_span(endpoint) -> tuple[str, int, int]:
    """Return ``(documented file, first line, last line)`` of an endpoint."""
    source_file = inspect.getsourcefile(endpoint)
    assert source_file, f"cannot resolve the source file of {endpoint!r}"
    source_lines, first_line = inspect.getsourcelines(endpoint)
    return _doc_relative(source_file), first_line, first_line + len(source_lines) - 1


def _installed_module_file(relative: str) -> bool:
    """True when ``<package>/<file>.py`` resolves inside an installed package.

    Framework handlers are documented without a line number, so the row stays
    valid across a dependency bump; this check keeps the module reference real.
    """
    parts = Path(relative).parts
    if len(parts) < 2:
        return False
    try:
        package = importlib.import_module(parts[0])
    except ImportError:
        return False
    origin = getattr(package, "__file__", None)
    if not origin:
        return False
    return Path(origin).parent.joinpath(*parts[1:]).is_file()


# ── Tests ──────────────────────────────────────────────────────────────


def test_documented_routes_match_registered_routes(collected):
    """The matrix documents exactly the routes the two apps register."""
    rows = _parse_rows(_matrix_text())
    documented = _documented_keys(rows)

    undocumented = set(collected) - set(documented)
    stale = set(documented) - set(collected)

    problems: list[str] = []
    if undocumented:
        problems.append(
            f"{len(undocumented)} registered route(s) are missing from "
            f"docs/G0_ENDPOINT_AUTH_MATRIX.md:\n  {_format_keys(undocumented)}"
        )
    if stale:
        problems.append(
            f"{len(stale)} documented route(s) are no longer registered and must be "
            f"removed from docs/G0_ENDPOINT_AUTH_MATRIX.md:\n  {_format_keys(stale)}"
        )
    assert not problems, "endpoint matrix out of date\n" + "\n".join(problems)


def test_documented_route_counts_match(collected):
    """The stated per-app route counts match the collected routes."""
    match = _TOTAL_RE.search(_matrix_text())
    assert match, (
        "docs/G0_ENDPOINT_AUTH_MATRIX.md must state the route count as "
        "'Route count: <total> — <n> proxy, <n> dashboard'"
    )
    rows = _parse_rows(_matrix_text())
    for surface in ("proxy", "dashboard"):
        collected_count = sum(1 for key in collected if key[0] == surface)
        documented_count = sum(1 for row in rows if row["surface"] == surface)
        assert documented_count == collected_count, (
            f"{surface}: matrix has {documented_count} row(s) but the app registers "
            f"{collected_count} route(s)"
        )
        assert int(match.group(surface)) == collected_count, (
            f"{surface}: the stated count ({match.group(surface)}) does not match the "
            f"{collected_count} registered route(s)"
        )
    assert int(match.group("total")) == len(collected), (
        f"stated total {match.group('total')} != {len(collected)} collected route(s)"
    )


def test_every_row_is_classified_and_code_linked(collected):
    """Every row carries a privilege class, an auth value and a real handler."""
    problems: list[str] = []
    for row in _parse_rows(_matrix_text()):
        key = (row["surface"], row["method"], row["path"])
        label = f"{row['method']} {row['path']} ({row['surface']})"

        if row["privilege"] not in PRIVILEGE_CLASSES:
            problems.append(f"{label}: privilege {row['privilege']!r} is not one of {sorted(PRIVILEGE_CLASSES)}")
        if (row["auth"] == "none") != (row["privilege"] == "public"):
            problems.append(
                f"{label}: auth {row['auth']!r} and privilege {row['privilege']!r} disagree — "
                "only public routes may skip the auth dependency"
            )
        if key not in collected:
            continue  # reported by the set comparison above

        match = _HANDLER_RE.match(row["handler"])
        if not match:
            problems.append(f"{label}: handler {row['handler']!r} is not a 'file.py[:line]' reference")
            continue

        documented_file = match.group("file")
        documented_line = match.group("line")

        if documented_line is None:
            # Module-only linkage is reserved for framework code, so a dependency
            # bump cannot turn this guard red over upstream line churn.
            if (REPO_ROOT / documented_file).exists():
                problems.append(
                    f"{label}: repo handler {documented_file} must be pinned to a line "
                    "('file.py:line'); module-only references are for framework code"
                )
            elif not _installed_module_file(documented_file):
                problems.append(
                    f"{label}: handler module {documented_file} does not resolve in any installed package"
                )
            continue

        route = collected[key]
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            target = REPO_ROOT / documented_file
            if not target.is_file():
                problems.append(f"{label}: handler file {documented_file} does not exist")
            continue

        expected_file, first_line, last_line = _handler_span(endpoint)
        documented_line = int(documented_line)
        if documented_file != expected_file:
            problems.append(
                f"{label}: matrix points at {documented_file} but the registered "
                f"handler lives in {expected_file}"
            )
        elif not first_line <= documented_line <= last_line:
            problems.append(
                f"{label}: matrix points at line {documented_line} but the registered "
                f"handler spans {expected_file}:{first_line}-{last_line} — update the row"
            )

    assert not problems, "endpoint matrix rows are incomplete or stale:\n  " + "\n  ".join(problems)


def test_referenced_tests_exist():
    """Every covering-test node id resolves to a test that exists in the tree."""
    known = _known_test_nodes()
    problems: list[str] = []
    for row in _parse_rows(_matrix_text()):
        label = f"{row['method']} {row['path']} ({row['surface']})"
        for ref in _test_refs(row["tests"]):
            if ref not in known:
                problems.append(f"{label}: {ref} does not resolve to a test in the tree")
    assert not problems, "the endpoint matrix cites tests that do not exist:\n  " + "\n  ".join(problems)


def test_uncovered_section_matches_none_rows():
    """The 'Uncovered routes' list is exactly the set of rows without a test."""
    text = _matrix_text()
    rows = _parse_rows(text)
    untested = {
        f"{row['method']} {row['path']} ({row['surface']})" for row in rows if not _test_refs(row["tests"])
    }

    section = text.split("## Uncovered routes", 1)
    assert len(section) == 2, "docs/G0_ENDPOINT_AUTH_MATRIX.md lost its '## Uncovered routes' section"
    body = section[1].split("\n## ", 1)[0]

    listed: set[str] = set()
    for line in body.splitlines():
        if not line.startswith("- "):
            continue
        tokens = _BACKTICKED_RE.findall(line)
        if len(tokens) < 2:
            continue
        listed.add(f"{tokens[0]} {tokens[1]} ({line.rsplit('— ', 1)[-1].strip()})")

    assert listed == untested, (
        "the 'Uncovered routes' list and the 'none' cells disagree\n"
        f"  only listed: {sorted(listed - untested)}\n"
        f"  only 'none':  {sorted(untested - listed)}"
    )

    header = _UNCOVERED_HEADER_RE.search(body)
    assert header, "the 'Uncovered routes' section must open with '<count> of the <total> routes'"
    assert int(header.group("count")) == len(untested), (
        f"section states {header.group('count')} uncovered route(s) but {len(untested)} rows have no test"
    )
    assert int(header.group("total")) == len(rows), (
        f"section states {header.group('total')} routes but the matrix has {len(rows)} rows"
    )


def test_collect_routes_classifies_method_less_routes():
    """A ``Mount`` is ``MOUNT``; a WebSocket route must not pose as one."""
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route, WebSocketRoute

    async def home(request):  # pragma: no cover - never invoked
        return None

    async def socket(websocket):  # pragma: no cover - never invoked
        return None

    synthetic = Starlette(
        routes=[
            Route("/home", home),
            WebSocketRoute("/ws", socket),
            Mount("/static", app=Starlette(routes=[])),
        ]
    )

    assert set(_collect_routes(synthetic)) == {
        ("GET", "/home"),
        (WEBSOCKET_METHOD, "/ws"),
        (MOUNT_METHOD, "/static"),
    }
