#!/usr/bin/env python3
"""Pre-restart gate for guardian-llmprovider-gateway.

Runs every cheap check that has caught real bugs before:
  1. The deployment head: the tree this gate runs from must be the commit the
     operator intends to deploy (issue #85)
  2. py_compile on all app/**/*.py
  3. pyflakes over app/ (undefined names — caught the _-prefix injection
     bugs after the 2026-08-12 restart)
  4. The wrapper-vs-module signature regression test
  5. Full pytest suite

Exit code 0 = safe to `sudo systemctl restart guardian-llmprovider-gateway`.
Any failure = fix first; a startup-breaking error is NOT self-healable
because the agent's own model traffic routes through Guardian.

**Why the deployment head is checked first (issue #85).**  The unit runs the
operator's checkout, so `systemctl restart` deploys whatever `HEAD` that
checkout happens to be on.  On 2026-10-29 the checkout sat on
`feat/unified-provider-model-phase-a` at `b30e027` while `main` was at
`6a71cee`: every gate below passed, the restart succeeded, and the service came
back running code that predated the whole batch.  A green gate that does not
name the commit it graded is a false green light, so this gate now proves the
tree before it proves the code:

* **FAIL** when `HEAD` is not `origin/main`, in either direction.  A tree that
  carries commits `main` does not have deploys unreviewed work; a tree that is
  *behind* `main` deploys old code.  The second direction is the live case:
  `b30e027` **is** an ancestor of `bded1c8` (0 commits ahead, 29 behind), so an
  ancestor-only rule would have called the production checkout a PASS.  The
  line names the branch, `HEAD`, `origin/main` and how many commits would be
  deployed while absent from `main`.
* **FAIL** when `app/` or `scripts/` carries uncommitted changes: those files
  are the deployment, so a dirty tree deploys something no commit describes.
* **WARN** when `config/global.settings.yaml` is modified.  That file is
  *runtime input* read from the working directory, and on this host the live
  copy is deliberately the operator's local delta (it carries settings `main`
  does not), so a dirty copy is expected and must not block a restart.
* **SKIP** — never PASS — when the head cannot be proven: no git checkout, a
  detached `HEAD` (a CI checkout is not a deployment tree), or `origin/main`
  unreachable.  The fetch is best-effort with a short timeout so the gate can
  never hang on the network.
* ``--expect-head <sha>`` pins the intended commit (prefix match allowed) and
  FAILs on any other `HEAD`.  This is how an operator says "I intend to deploy
  exactly this", and it is the only form that works offline and from a
  detached checkout.

Usage:
    ./venv/bin/python scripts/pre_restart_check.py
    ./venv/bin/python scripts/pre_restart_check.py --expect-head bded1c8
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import importlib
import inspect
import pathlib
import py_compile
import re
import subprocess
import sys
from typing import Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "app"

#: Gate verdicts.  Only :data:`FAIL` blocks a restart; ``WARN`` and ``SKIP`` are
#: printed loudly and never turn into a silent pass.
PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

#: Worst-first, so a gate step that reports several lines can summarise itself.
_SEVERITY = {PASS: 0, WARN: 1, SKIP: 2, FAIL: 3}

#: Paths whose uncommitted changes *are* the deployment (issue #85).
DEPLOYMENT_CODE_PATHS = ("app/", "scripts/")

#: Runtime input that is expected to differ on this host: the live copy is the
#: operator's local delta, so a dirty copy is a WARN and never a FAIL.
DEPLOYMENT_CONFIG_PATHS = ("config/global.settings.yaml",)

#: Seconds allowed for the best-effort `git fetch`; the gate must never hang.
FETCH_TIMEOUT_S = 5.0

#: Seconds allowed for a local git query (no network involved).
GIT_TIMEOUT_S = 10.0

#: A commit sha as ``--expect-head`` accepts it: 4-64 hex characters.  Anything
#: else is rejected, because ``"".startswith("")`` is true and an empty pin would
#: pass every tree (PR-Piet on #85).
_PIN_RE = re.compile(r"^[0-9a-f]{4,64}$")


def step(name: str) -> None:
    print(f"── {name}")


def worst(*statuses: str) -> str:
    """Return the most severe of *statuses* (``FAIL`` beats ``SKIP`` beats ``WARN``)."""
    return max(statuses, key=lambda status: _SEVERITY.get(status, 0), default=PASS)


# ── The deployment head (issue #85) ────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class HeadState:
    """The git state of the tree this gate runs from.

    Every field is a fact read from git; nothing here is inferred.  ``head`` is
    empty when the tree is not a checkout or ``HEAD`` cannot be resolved, and
    ``origin_main`` is empty when the remote-tracking ref is unknown — the
    judge turns both into ``SKIP`` rather than a guess.
    """

    #: The tree these facts describe, so a verdict names the tree it judged.
    repo: pathlib.Path | None = None
    is_repo: bool = False
    head: str = ""
    branch: str = ""
    detached: bool = False
    origin_main: str = ""
    #: ``True``/``False`` when ``origin/main`` is known, ``None`` when it is not.
    ancestor_of_main: bool | None = None
    #: Commits in ``HEAD`` that ``origin/main`` does not have.
    ahead_of_main: int = 0
    #: Commits in ``origin/main`` that ``HEAD`` does not have.
    behind_main: int = 0
    #: Why the fetch failed; empty when it succeeded or was not attempted.
    fetch_error: str = ""
    #: Uncommitted paths under :data:`DEPLOYMENT_CODE_PATHS`.
    dirty_code: tuple[str, ...] = ()
    #: Uncommitted paths under :data:`DEPLOYMENT_CONFIG_PATHS`.
    dirty_config: tuple[str, ...] = ()
    #: Why ``git status`` failed; empty when it succeeded.  An unreadable status
    #: is *not* a clean tree, so the judge reports a SKIP instead of a PASS.
    status_error: str = ""

    @property
    def origin_main_known(self) -> bool:
        return bool(self.origin_main)


def _git(
    args: Sequence[str],
    *,
    repo: pathlib.Path = REPO,
    timeout: float = GIT_TIMEOUT_S,
) -> tuple[int, str, str]:
    """Run a read-only git command in *repo*; return ``(rc, stdout, stderr)``.

    Never raises: a missing ``git`` binary, a timeout or a non-zero exit all
    come back as a return code, so the gate reports a SKIP instead of a
    traceback.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    # `rstrip`, not `strip`: `git status --porcelain` lines are "XY path", and
    # stripping the leading space of the first line would shift every path by one
    # character (" M app/x.py" -> "M app/x.py" -> "pp/x.py").
    return proc.returncode, proc.stdout.rstrip(), proc.stderr.strip()


def _dirty_paths(status_output: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split ``git status --porcelain`` output into (code, config) dirty paths."""
    code: list[str] = []
    config: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        # A rename/copy reports "old -> new" and *both* sides matter:
        # `git mv app/main.py docs/main.py` removes a deployed file while its
        # destination is outside the deployment paths, so classifying only the
        # destination would report the tree clean (PR-Piet on #85).
        for path in line[3:].split(" -> "):
            path = path.strip().strip('"')
            if path.startswith(DEPLOYMENT_CODE_PATHS):
                code.append(path)
            elif path in DEPLOYMENT_CONFIG_PATHS:
                config.append(path)
    return tuple(code), tuple(config)


def read_head_state(
    *,
    repo: pathlib.Path = REPO,
    fetch: bool = True,
    timeout: float = FETCH_TIMEOUT_S,
) -> HeadState:
    """Read the git state of *repo*.  Never raises, never guesses.

    The fetch is the only network call and it is best-effort: it is bounded by
    *timeout* and its failure is recorded in ``fetch_error`` so the judge can
    report a SKIP instead of a false pass.
    """
    rc, _out, _err = _git(("rev-parse", "--git-dir"), repo=repo)
    if rc != 0:
        return HeadState(repo=repo, is_repo=False)

    rc, head, _err = _git(("rev-parse", "HEAD"), repo=repo)
    if rc != 0 or not head:
        return HeadState(repo=repo, is_repo=True)

    rc, branch, _err = _git(("rev-parse", "--abbrev-ref", "HEAD"), repo=repo)
    detached = rc != 0 or branch == "HEAD"

    fetch_error = ""
    if fetch:
        # Explicit refspec: the remote-tracking ref is updated deterministically
        # even when the local clone has no default refspec for `main`.
        rc, _out, err = _git(
            ("fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"),
            repo=repo,
            timeout=timeout,
        )
        if rc != 0:
            fetch_error = err or f"git fetch exited {rc}"

    rc, origin_main, _err = _git(("rev-parse", "--verify", "origin/main"), repo=repo)
    if rc != 0:
        origin_main = ""

    ancestor: bool | None = None
    ahead = 0
    behind = 0
    if origin_main:
        rc, _out, _err = _git(("merge-base", "--is-ancestor", "HEAD", "origin/main"), repo=repo)
        if rc == 0:
            ancestor = True
        elif rc == 1:
            ancestor = False
        rc, count, _err = _git(("rev-list", "--count", "origin/main..HEAD"), repo=repo)
        if rc == 0 and count.isdigit():
            ahead = int(count)
        rc, count, _err = _git(("rev-list", "--count", "HEAD..origin/main"), repo=repo)
        if rc == 0 and count.isdigit():
            behind = int(count)

    rc, status_output, status_err = _git(("status", "--porcelain"), repo=repo)
    status_error = "" if rc == 0 else (status_err or f"git status exited {rc}")
    dirty_code, dirty_config = _dirty_paths(status_output if rc == 0 else "")

    return HeadState(
        repo=repo,
        is_repo=True,
        head=head,
        branch="" if detached else branch,
        detached=detached,
        origin_main=origin_main,
        ancestor_of_main=ancestor,
        ahead_of_main=ahead,
        behind_main=behind,
        fetch_error=fetch_error,
        dirty_code=dirty_code,
        dirty_config=dirty_config,
        status_error=status_error,
    )


def _head_facts(state: HeadState) -> str:
    """The one-line fact block every deployment-head verdict carries."""
    branch = state.branch or "detached HEAD"
    main = state.origin_main[:12] if state.origin_main_known else "unknown"
    if state.fetch_error or not state.origin_main_known:
        # A stale remote-tracking ref is not evidence: say so instead of printing
        # a sha and a count that may describe a main nobody can see.
        return f"on {branch} @ {state.head[:12]}, origin/main unknown, commits not in main unknown"
    return (
        f"on {branch} @ {state.head[:12]}, origin/main {main}, "
        f"{state.ahead_of_main} commits not in main"
    )


def judge_deployment_head(
    state: HeadState, expect_head: str | None = None
) -> tuple[str, str]:
    """Decide whether *state* is the commit the operator intends to deploy.

    Returns ``(status, line)``; the line is the single verdict the operator
    reads, and it always names the branch, ``HEAD``, ``origin/main`` and the
    number of commits that would be deployed while absent from ``main``.
    """
    facts = _head_facts(state)
    if not state.is_repo:
        return SKIP, (
            f"deployment head — {state.repo or REPO} is not a git checkout, so the deployment head "
            "was not proven (pass --expect-head <sha> to pin the intended commit)"
        )
    if not state.head:
        return SKIP, (
            "deployment head — HEAD cannot be resolved (unborn branch?), so the "
            "deployment head was not proven"
        )
    if expect_head is not None:
        wanted = expect_head.strip().lower()
        if not _PIN_RE.match(wanted):
            return FAIL, (
                f"deployment head — {facts}; --expect-head {expect_head!r} is not a commit "
                "sha (4-64 hex characters), so the intended commit was not pinned"
            )
        if not state.head.lower().startswith(wanted):
            return FAIL, (
                f"deployment head — {facts}; --expect-head {expect_head} does not match "
                f"HEAD {state.head}"
            )
        return PASS, f"deployment head — {facts}; --expect-head {expect_head} matches HEAD"
    if state.detached:
        # A CI checkout is detached and is not a deployment tree: the unit runs a
        # branch checkout.  Never a PASS, never a FAIL that would break CI.
        return SKIP, (
            f"deployment head — {facts}; detached HEAD is not a deployment checkout "
            "(the unit runs a branch checkout) — pass --expect-head <sha> to pin the "
            "intended commit"
        )
    if state.fetch_error:
        # A failed fetch leaves a stale `origin/main` ref behind, and comparing
        # against it would be a false pass: the gate cannot see the main it is
        # supposed to prove, so it says so.
        return SKIP, (
            f"deployment head — {facts}; origin/main could not be fetched "
            f"({state.fetch_error}), so the deployment head was not proven"
        )
    if not state.origin_main_known:
        return SKIP, (
            f"deployment head — {facts}; there is no origin/main ref, so the deployment "
            "head was not proven"
        )
    if state.ancestor_of_main:
        if state.head == state.origin_main:
            return PASS, f"deployment head — {facts}"
        # An ancestor that is not equal is *behind* main, and that is the live
        # #85 case: the production checkout sat on `b30e027` while main was at
        # `bded1c8`, and `git merge-base --is-ancestor b30e027 bded1c8` is true
        # (0 commits ahead, 29 behind).  An ancestor-only rule would call that a
        # PASS and the restart would deploy old code while the gate looked
        # green — the exact false green light the issue is about.
        return FAIL, (
            f"deployment head — {facts}; HEAD is {state.behind_main} commit(s) behind "
            "origin/main, so a restart would deploy old code — check out main and pull, "
            "or pin the intent with --expect-head <sha>"
        )
    if state.branch == "main":
        if state.behind_main > 0:
            # Diverged: local main has unpushed commits *and* is missing merged
            # ones, so a restart would deploy a main nobody else has while
            # silently dropping the merged changes (PR-Piet on #85).
            return FAIL, (
                f"deployment head — {facts}; local main has diverged from origin/main "
                f"({state.ahead_of_main} unpushed, {state.behind_main} missing), so a "
                "restart would deploy a main nobody else has"
            )
        # Purely ahead: the commits are in main locally, so this is not the #85
        # case, but they are not pushed either.
        return WARN, (
            f"deployment head — {facts}; local main is ahead of origin/main, so these "
            "commits are not pushed"
        )
    return FAIL, (
        f"deployment head — {facts}; HEAD carries {state.ahead_of_main} commit(s) that "
        f"main does not have — a restart would deploy {state.branch or 'this commit'} "
        "instead of main"
    )


def judge_deployment_tree(state: HeadState) -> tuple[str, str]:
    """Decide whether the working tree is clean enough to deploy."""
    if not state.is_repo:
        return SKIP, "deployment tree — not a git checkout, so the working tree was not proven"
    if state.status_error:
        return SKIP, (
            f"deployment tree — git status failed ({state.status_error}), so the working "
            "tree was not proven clean"
        )
    if state.dirty_code:
        shown = ", ".join(state.dirty_code[:5])
        more = f" (+{len(state.dirty_code) - 5} more)" if len(state.dirty_code) > 5 else ""
        return FAIL, (
            f"deployment tree — {len(state.dirty_code)} uncommitted change(s) under "
            f"app/ or scripts/ would be deployed: {shown}{more}"
        )
    if state.dirty_config:
        return WARN, (
            "deployment tree — config/global.settings.yaml is modified; that is expected "
            "on this host (the live file is the operator's local delta and is runtime "
            "input, not code), so it does not block the restart"
        )
    return PASS, (
        "deployment tree — no uncommitted changes under app/, scripts/ or "
        "config/global.settings.yaml"
    )


def check_deployment_head(
    expect_head: str | None = None,
    *,
    repo: pathlib.Path = REPO,
    state: HeadState | None = None,
) -> str:
    """Print the deployment-head verdicts and return the gate status."""
    step("1/5 deployment head (issue #85)")
    state = state if state is not None else read_head_state(repo=repo)
    head_status, head_line = judge_deployment_head(state, expect_head)
    print(f"  [{head_status}] {head_line}")
    tree_status, tree_line = judge_deployment_tree(state)
    print(f"  [{tree_status}] {tree_line}")
    return worst(head_status, tree_status)


def check_compile() -> bool:
    step("2/5 py_compile app/**/*.py")
    files = sorted(APP.rglob("*.py"))
    ok = True
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"  FAIL {f}: {exc}")
            ok = False
    print(f"  {'OK' if ok else 'FAILED'} ({len(files)} files)")
    return ok


def check_pyflakes() -> bool:
    step("3/5 pyflakes app/ (undefined names)")
    try:
        import pyflakes.api  # noqa: F401
    except ImportError:
        print("  SKIP (pyflakes not installed: pip install pyflakes)")
        return True
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(APP)],
        capture_output=True, text=True,
    )
    lines = [ln for ln in proc.stdout.splitlines()
             if "imported but unused" not in ln
             and "assigned to but never used" not in ln
             and "unable to detect undefined names" not in ln]
    if lines:
        for ln in lines:
            print(f"  {ln}")
        return False
    print("  OK (no undefined names)")
    return True


def check_wrapper_signatures() -> bool:
    step("4/5 wrapper-vs-module signature check")
    sys.path.insert(0, str(REPO))
    src = (APP / "proxy" / "server.py").read_text()
    tree = ast.parse(src)

    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.asname and a.asname.startswith("_"):
                    alias_map[a.asname] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and a.asname.startswith("_"):
                    alias_map[a.asname] = a.name

    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id in alias_map):
            continue
        fn_name = node.func.attr
        if fn_name == "init":
            continue
        try:
            mod = importlib.import_module(alias_map[node.func.value.id])
        except Exception as exc:  # pragma: no cover
            problems.append(f"line {node.lineno}: cannot import {alias_map[node.func.value.id]}: {exc}")
            continue
        fn = getattr(mod, fn_name, None)
        if fn is None:
            problems.append(f"line {node.lineno}: {alias_map[node.func.value.id]}.{fn_name} does not exist")
            continue
        sig = inspect.signature(fn)
        params = list(sig.parameters)
        var_pos = any(v.kind == inspect.Parameter.VAR_POSITIONAL for v in sig.parameters.values())
        var_kw = any(v.kind == inspect.Parameter.VAR_KEYWORD for v in sig.parameters.values())
        pos = len(node.args)
        kw = {k.arg for k in node.keywords if k.arg}
        if not var_pos and pos > len(params):
            problems.append(f"line {node.lineno}: {fn_name} gets {pos} positional, only {len(params)} params")
            continue
        filled = set(params[:pos]) | kw
        for p, v in sig.parameters.items():
            if v.default is inspect.Parameter.empty and p not in filled and v.kind not in (
                inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
            ):
                problems.append(f"line {node.lineno}: {fn_name} missing required '{p}'")
        if not var_kw:
            for k in kw:
                if k not in sig.parameters:
                    problems.append(f"line {node.lineno}: {fn_name} unexpected kwarg '{k}'")

    if problems:
        for p in problems:
            print(f"  {p}")
        return False
    print("  OK (all delegation calls match module signatures)")
    return True


def check_call_site_signatures() -> bool:
    """Check cross-module call sites against the real callee signatures.

    Catches the 2026-08-26 class of bug: ``forwarding.py`` called
    ``StreamResponseAssembler(protocol=...)`` (constructor takes no args) and
    ``assembler.feed(...)`` (method does not exist; it is ``add_sse_line``).
    That drift was dead code while capture was disabled and went live —
    and crashed with HTTP 500 — the moment capture was enabled for all
    clients. The wrapper-only signature check (server.py) cannot see it.

    Static rules, conservative to avoid false positives:
    - Only calls whose callee resolves to an *imported* name are checked
      (locals/dynamics are skipped).
    - Constructors/classes and module attributes are resolved via the real
      import; missing attributes are only reported when the parent object
      resolves statically (class or module).
    - Mini dataflow: within one function, ``var = Klass(...)`` assignments
      are tracked so ``var.method(...)`` calls are checked against Klass.
    - Skip ``init`` (DI convention), ``**kwargs``/``*args`` callees, and
      calls that resolve to nothing importable.
    """
    step("4b/5 cross-module call-site signature check")
    sys.path.insert(0, str(REPO))

    import ast
    import importlib
    import inspect

    py_files = sorted(APP.rglob("*.py"))
    problems: list[str] = []

    # Sentinel: the *top-level* package is not installed in this venv, so the
    # call site cannot be verified here — not a drift finding. App code may
    # import an SDK lazily inside a function (e.g. the Copilot bridge imports
    # litellm/openai only where it talks to the external runtime), and the
    # deployment venv deliberately does not carry that SDK.
    _uninstalled = object()

    def _resolve_import(module_name: str, attr: str | None = None):
        """Return the callee object, ``_uninstalled``, or None.

        ``_uninstalled``: ``import_module`` failed because the top-level
        package itself is absent, so there is nothing to resolve here.
        ``None``: the module imports but the attribute is missing, a deeper
        module is absent, or the import failed another way — those stay
        drift findings.
        """
        try:
            mod = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name is not None and exc.name == module_name.split(".", 1)[0]:
                return _uninstalled
            return None
        except Exception:
            return None
        if attr is None:
            return mod
        try:
            return getattr(mod, attr)
        except AttributeError:
            return None

    for path in py_files:
        try:
            tree = ast.parse(path.read_text())
        except Exception as exc:  # pragma: no cover
            problems.append(f"{path.relative_to(REPO)}: parse error: {exc}")
            continue

        # Module-level import map: local name -> (module, attr or None)
        import_map: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    local = a.asname or a.name
                    import_map[local] = (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    import_map[local] = (a.name, None)

        def _check_call(call: ast.Call, callee_obj, label: str) -> None:
            if callee_obj is None:
                return
            if not (inspect.isfunction(callee_obj) or inspect.isclass(callee_obj)):
                return
            try:
                sig = inspect.signature(callee_obj)
            except (TypeError, ValueError):
                return
            params = list(sig.parameters)
            # Bound-style calls (Class(...) -> __init__) never pass self/cls.
            if params and params[0] in ("self", "cls"):
                params = params[1:]
            var_pos = any(v.kind == inspect.Parameter.VAR_POSITIONAL for v in sig.parameters.values())
            var_kw = any(v.kind == inspect.Parameter.VAR_KEYWORD for v in sig.parameters.values())
            pos = len(call.args)
            kw = {k.arg for k in call.keywords if k.arg}
            if not var_pos and pos > len(params):
                problems.append(
                    f"{path.relative_to(REPO)}:{call.lineno}: {label} gets {pos} positional, "
                    f"only {len(params)} params")
                return
            filled = set(params[:pos]) | kw
            for p, v in sig.parameters.items():
                if p in ("self", "cls"):
                    continue
                if v.default is inspect.Parameter.empty and p not in filled and v.kind not in (
                    inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
                ):
                    problems.append(
                        f"{path.relative_to(REPO)}:{call.lineno}: {label} missing required '{p}'")
            if not var_kw:
                for k in kw:
                    if k not in sig.parameters:
                        problems.append(
                            f"{path.relative_to(REPO)}:{call.lineno}: {label} unexpected kwarg '{k}'")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Case A: direct imported-name call: Klass(...) / func(...)
            if isinstance(node.func, ast.Name):
                local = node.func.id
                if local in import_map:
                    mod_name, attr = import_map[local]
                    if attr is None:
                        continue  # module alias itself, not a callable import
                    callee = _resolve_import(mod_name, attr)
                    if callee is _uninstalled:
                        continue
                    if callee is None:
                        problems.append(
                            f"{path.relative_to(REPO)}:{node.lineno}: {mod_name}.{attr} does not exist")
                        continue
                    if inspect.isclass(callee):
                        # compare against __init__ signature
                        init = getattr(callee, "__init__", None)
                        if init is not None and init is not object.__init__:
                            _check_call(node, init, f"{attr}.__init__")
                    else:
                        _check_call(node, callee, f"{attr}")
            # Case B: attribute call on imported module/class: mod.fn(...), Klass.method(...)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                base = node.func.value.id
                if base not in import_map:
                    continue
                mod_name, attr = import_map[base]
                parent = _resolve_import(mod_name, attr) if attr else _resolve_import(mod_name, None)
                if parent is _uninstalled or parent is None:
                    continue
                method = getattr(parent, node.func.attr, None)
                if method is None:
                    if inspect.isclass(parent) or inspect.ismodule(parent):
                        problems.append(
                            f"{path.relative_to(REPO)}:{node.lineno}: "
                            f"{node.func.attr} does not exist on {attr or mod_name}")
                    continue
                if node.func.attr == "init":
                    continue
                _check_call(node, method, f"{node.func.attr}")

        # Case C (mini dataflow): var = Klass(...) then var.method(...)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            klass_by_var: dict[str, tuple[str, str]] = {}
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    t = node.targets[0]
                    if isinstance(t, ast.Name) and isinstance(node.value, ast.Call) \
                            and isinstance(node.value.func, ast.Name):
                        local = node.value.func.id
                        if local in import_map:
                            mod_name, attr = import_map[local]
                            if attr and inspect.isclass(_resolve_import(mod_name, attr)):
                                klass_by_var[t.id] = (mod_name, attr)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and isinstance(node.func.value, ast.Name):
                    var = node.func.value.id
                    if var in klass_by_var:
                        mod_name, attr = klass_by_var[var]
                        parent = _resolve_import(mod_name, attr)
                        if parent is _uninstalled or parent is None:
                            continue
                        method = getattr(parent, node.func.attr, None)
                        if method is None:
                            problems.append(
                                f"{path.relative_to(REPO)}:{node.lineno}: {var}.{node.func.attr} "
                                f"does not exist on {attr}")
                            continue
                        _check_call(node, method, f"{var}.{node.func.attr}")

    if problems:
        for p in problems:
            print(f"  {p}")
        return False
    print("  OK (all resolved call sites match callee signatures)")
    return True


def check_pytest() -> bool:
    step("5/5 pytest tests/")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(REPO / "tests"), "-q"],
        capture_output=True, text=True,
    )
    tail = proc.stdout.strip().splitlines()[-3:]
    for ln in tail:
        print(f"  {ln}")
    return proc.returncode == 0


def _status(ok: bool) -> str:
    """Map a boolean check to a gate verdict."""
    return PASS if ok else FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-restart gate: prove the tree, then prove the code.",
    )
    parser.add_argument(
        "--expect-head",
        default=None,
        metavar="SHA",
        help=(
            "the commit this restart intends to deploy; HEAD must equal it (prefix "
            "match allowed) or the gate FAILs. Works offline and from a detached "
            "checkout, which is the only way to pin a tree git cannot compare to main"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"Pre-restart gate for guardian-llmprovider-gateway ({REPO})")
    results: list[tuple[str, str]] = [
        ("deployment-head", check_deployment_head(args.expect_head)),
        ("py_compile", _status(check_compile())),
        ("pyflakes", _status(check_pyflakes())),
        ("signatures", _status(check_wrapper_signatures())),
        ("call-sites", _status(check_call_site_signatures())),
        ("pytest", _status(check_pytest())),
    ]
    print("──")
    for name, status in results:
        print(f"  {name}: {status}")
    blocking = [name for name, status in results if status == FAIL]
    if blocking:
        print(
            "❌ GATE FAILURES — fix before restarting (session drops on restart): "
            + ", ".join(blocking)
        )
        return 1
    unproven = [name for name, status in results if status in (WARN, SKIP)]
    if unproven:
        print(
            "⚠️  ALL BLOCKING GATES PASSED — read the WARN/SKIP lines above: "
            + ", ".join(unproven)
        )
        return 0
    print("✅ ALL GATES PASSED — safe to restart guardian-llmprovider-gateway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
