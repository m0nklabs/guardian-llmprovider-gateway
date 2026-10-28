"""G0 guard: every auth-protected handler must actually be a route.

Issue #47: ``app/proxy/server.py`` carried

    async def get_version(client_id: str = Depends(verify_api_key)):
        # Mimic Ollama version endpoint
        return {"version": "0.1.27"}

with no route decorator and no other registration. The endpoint matrix then
counted 44 protected routes while a source search found 45
``Depends(verify_api_key)`` sites, so an audit read a protected endpoint that
does not exist. The handler was deleted rather than routed: nothing calls
Ollama's ``/api/version`` and an unrouted copy was never the contract.

The matrix guard (``tests/unit/test_g0_endpoint_matrix.py``) compares the
documented rows with the routes the two apps register, which cannot catch this
class of defect: a handler that is not a route never reaches ``app.routes``, so
there is nothing to compare it against. This module closes the blind spot from
the other side — it parses ``app/`` for handlers that declare the auth
dependency and requires each one to appear among the endpoints of the imported
apps.

Registration counts as routed when the app serves that same function object:
FastAPI keeps ``endpoint.__qualname__`` and its source file, so a handler
registered by decorator, by ``add_api_route``, or by importing it into an
``APIRouter`` that is later included all resolve to its defining module. A
decorator on a module nobody imports does not reach ``app.routes`` and stays
flagged, which is the behaviour we want.

Nothing leaves the process: both app objects are imported (the same two
``tests/unit/test_g0_endpoint_matrix.py`` imports), the source tree is parsed
with :mod:`ast`, and the matrix is parsed as text. No server is started, no
network or model call is made.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from pathlib import Path

from app import main as dashboard_module
from app.proxy import server as proxy_module
from tests.unit.test_g0_endpoint_matrix import _matrix_text, _parse_rows

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"

AUTH_DEPENDENCY = "verify_api_key"
HEAD_METHOD = "HEAD"
UNKNOWN_METHOD = "*"

FIX_HINT = (
    "A handler that declares the auth dependency but serves no route is dead "
    "code: it makes the auth surface look bigger than it is and hides the next "
    "real regression in a count that no longer reconciles.\n"
    "  Fix: delete the function (AGENTS.md anti-legacy protocol), or register it "
    "with a FastAPI route decorator and add its row to "
    "docs/G0_ENDPOINT_AUTH_MATRIX.md in the same change."
)


@dataclass(frozen=True)
class AuthHandler:
    """One ``def`` whose signature depends on ``verify_api_key``."""

    path: Path
    line: int
    qualname: str

    @property
    def key(self) -> tuple[Path, str]:
        return (self.path, self.qualname)

    def describe(self) -> str:
        try:
            location = self.path.relative_to(REPO_ROOT).as_posix()
        except ValueError:  # a synthetic file outside the repo, used by the tests below
            location = self.path.as_posix()
        return f"{location}:{self.line} {self.qualname}()"


# ── Source scan ────────────────────────────────────────────────────────


def _depended_names(node: ast.AST | None) -> set[str]:
    """Return the callables passed to ``Depends(...)`` inside one expression."""
    if node is None:
        return set()
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not child.args:
            continue
        callee = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
        if callee != "Depends":
            continue
        target = child.args[0]
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name:
            names.add(name)
    return names


def _signature_dependencies(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Dependencies declared by a function signature.

    Covers plain defaults (``client_id=Depends(verify_api_key)``), keyword-only
    defaults and ``Annotated[..., Depends(...)]`` annotations — the ways a
    FastAPI handler can declare a dependency without decorator arguments.
    """
    arguments = function.args
    expressions: list[ast.AST] = list(arguments.defaults)
    expressions += [default for default in arguments.kw_defaults if default is not None]
    expressions += [
        argument.annotation
        for argument in [*arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
        if argument is not None and argument.annotation is not None
    ]
    names: set[str] = set()
    for expression in expressions:
        names |= _depended_names(expression)
    return names


def _scan_module(path: Path) -> list[AuthHandler]:
    """Every module-level function or class method that depends on the auth key.

    Function-local ``def``s are skipped: they cannot be module endpoints, and
    counting them would blur "dead handler" into "locally used helper".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[AuthHandler] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if AUTH_DEPENDENCY in _signature_dependencies(child):
                    found.append(AuthHandler(path.resolve(), child.lineno, f"{prefix}{child.name}"))
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                walk(child, prefix)

    walk(tree, "")
    return found


def auth_handlers(paths: list[Path]) -> list[AuthHandler]:
    """All auth-dependent handlers under ``paths``, in file/line order."""
    found: list[AuthHandler] = []
    for path in paths:
        found.extend(_scan_module(path))
    return found


# ── Route registration ─────────────────────────────────────────────────


def _iter_routes(application):
    """Both the app's own routes and the routes of anything it mounts."""
    for route in application.routes:
        yield route
        mounted = getattr(route, "app", None)
        if mounted is not None and hasattr(mounted, "routes"):
            yield from _iter_routes(mounted)


def route_registry(applications) -> dict[tuple[Path, str], list[tuple[str, str, str]]]:
    """``{handler identity: [(surface, method, path), ...]}`` for the given apps.

    The identity is ``(resolved source file, ``__qualname__``)`` — the endpoint
    FastAPI stores and will call, independent of how the route got there.
    ``HEAD`` folds into ``GET`` exactly like the endpoint matrix does, so the
    collected rows line up with the documented ones.
    """
    registry: dict[tuple[Path, str], list[tuple[str, str, str]]] = {}
    for surface, application in applications:
        for route in _iter_routes(application):
            endpoint = getattr(route, "endpoint", None)
            source = inspect.getsourcefile(endpoint) if endpoint is not None else None
            if not source:
                continue
            identity = (Path(source).resolve(), endpoint.__qualname__)
            methods = set(getattr(route, "methods", None) or ())
            if "GET" in methods:
                methods.discard(HEAD_METHOD)
            path = getattr(route, "path", "")
            for method in sorted(methods) or [UNKNOWN_METHOD]:
                registry.setdefault(identity, []).append((surface, method, path))
    return registry


def registered_endpoints() -> dict[tuple[Path, str], list[tuple[str, str, str]]]:
    """The route registry of the two production apps."""
    return route_registry((("proxy", proxy_module.app), ("dashboard", dashboard_module.app)))


def unrouted_handlers(
    handlers: list[AuthHandler], registered: dict[tuple[Path, str], list[tuple[str, str, str]]]
) -> list[AuthHandler]:
    """Handlers no app serves — the defect this module exists for."""
    return [handler for handler in handlers if handler.key not in registered]


# ── Tests ──────────────────────────────────────────────────────────────


def test_every_auth_handler_is_registered_as_a_route():
    """No ``Depends(verify_api_key)`` handler may exist outside ``app.routes``."""
    sources = sorted(APP_DIR.rglob("*.py"))
    assert sources, f"no Python sources under {APP_DIR} — this guard would be blind"
    handlers = auth_handlers(sources)
    assert handlers, (
        f"the scan found no handler declaring {AUTH_DEPENDENCY}() at all; the "
        "source scan is broken, so a green result here would prove nothing"
    )

    unrouted = unrouted_handlers(handlers, registered_endpoints())
    assert not unrouted, (
        f"these handlers declare Depends({AUTH_DEPENDENCY}) but no registered route serves them:\n  "
        + "\n  ".join(handler.describe() for handler in unrouted)
        + "\n"
        + FIX_HINT
    )


def test_routed_auth_handlers_match_the_protected_matrix_rows():
    """44 ↔ 44: protected matrix rows and routed auth handlers are one set.

    This is the reconciliation ``docs/G0_TRUST_BOUNDARY_THREAT_MODEL.md`` §9
    item 10 states. The matrix guard owns the rows themselves; this side proves
    every protected row has a registered handler and that no registered auth
    handler is missing from the matrix.
    """
    protected = {
        (row["surface"], row["method"], row["path"])
        for row in _parse_rows(_matrix_text())
        if row["auth"].strip("`") == AUTH_DEPENDENCY
    }

    registered = registered_endpoints()
    routed: set[tuple[str, str, str]] = set()
    for handler in auth_handlers(sorted(APP_DIR.rglob("*.py"))):
        routed.update(registered.get(handler.key, []))

    problems: list[str] = []
    missing_rows = sorted(routed - protected)
    missing_handlers = sorted(protected - routed)
    if missing_rows:
        problems.append(
            "these routes declare the auth dependency but have no protected row in "
            "docs/G0_ENDPOINT_AUTH_MATRIX.md:\n  "
            + "\n  ".join(f"{surface} {method} {path}" for surface, method, path in missing_rows)
        )
    if missing_handlers:
        problems.append(
            "these matrix rows promise a protected route that no auth handler serves:\n  "
            + "\n  ".join(f"{surface} {method} {path}" for surface, method, path in missing_handlers)
        )
    assert not problems, (
        f"the protected-route reconciliation is off ({len(routed)} routed auth handlers, "
        f"{len(protected)} protected matrix rows)\n" + "\n".join(problems)
    )


def test_detector_flags_an_unrouted_handler(tmp_path: Path):
    """The detector itself: a dependency-bearing ``def`` without a route fails."""
    module = tmp_path / "synthetic_routes.py"
    module.write_text(
        textwrap.dedent(
            '''\
            from typing import Annotated

            from fastapi import Depends


            async def routed(client_id: str = Depends(verify_api_key)):
                return {}


            async def dead(client_id: str = Depends(verify_api_key)):
                """Exactly the shape issue #47 deleted."""


            async def annotated(client_id: Annotated[str, Depends(verify_api_key)]):
                return {}


            async def unrelated(client_id: str = Depends(other_dependency)):
                return {}
            '''
        ),
        encoding="utf-8",
    )

    handlers = auth_handlers([module])
    assert [handler.qualname for handler in handlers] == ["routed", "dead", "annotated"], (
        "the scan must see plain and Annotated dependencies, and must ignore a "
        "handler that depends on something else"
    )

    # Only ``routed`` is served, the way the live apps serve the real handlers.
    registered = {(module.resolve(), "routed"): [("proxy", "GET", "/synthetic")]}
    assert [handler.qualname for handler in unrouted_handlers(handlers, registered)] == ["dead", "annotated"]
    assert handlers[1].describe() == f"{module}:10 dead()"


def test_route_registry_keys_on_the_served_function_object():
    """A decorated handler is 'routed' because the app serves that function.

    Pins the positive half of the rule the guard relies on: the identity
    (source file + ``__qualname__``) and the ``HEAD``→``GET`` folding.
    """
    from fastapi import Depends, FastAPI

    application = FastAPI()

    @application.get("/synthetic")
    async def synthetic(client_id: str = Depends(lambda: "test")):
        return {}

    registry = route_registry([("synthetic", application)])
    identity = (Path(__file__).resolve(), synthetic.__qualname__)
    assert registry[identity] == [("synthetic", "GET", "/synthetic")]
