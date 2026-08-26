#!/usr/bin/env python3
"""Pre-restart gate for llama-guardian.

Runs every cheap static check that has caught real bugs before:
  1. py_compile on all app/**/*.py
  2. pyflakes over app/ (undefined names — caught the _-prefix injection
     bugs after the 2026-08-12 restart)
  3. The wrapper-vs-module signature regression test
  4. Full pytest suite

Exit code 0 = safe to `sudo systemctl restart llama-guardian`.
Any failure = fix first; a startup-breaking error is NOT self-healable
because the agent's own model traffic routes through Guardian.

Usage:
    ./venv/bin/python scripts/pre_restart_check.py
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import py_compile
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "app"


def step(name: str) -> None:
    print(f"── {name}")


def check_compile() -> bool:
    step("1/4 py_compile app/**/*.py")
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
    step("2/4 pyflakes app/ (undefined names)")
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
    step("3/4 wrapper-vs-module signature check")
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
    step("3b/4 cross-module call-site signature check")
    sys.path.insert(0, str(REPO))

    import ast
    import importlib
    import inspect

    py_files = sorted(APP.rglob("*.py"))
    problems: list[str] = []

    def _resolve_import(module_name: str, attr: str | None = None):
        """Return the callee object or None."""
        try:
            mod = importlib.import_module(module_name)
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
                if parent is None:
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
                        if parent is None:
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
    step("4/4 pytest tests/")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(REPO / "tests"), "-q"],
        capture_output=True, text=True,
    )
    tail = proc.stdout.strip().splitlines()[-3:]
    for ln in tail:
        print(f"  {ln}")
    return proc.returncode == 0


def main() -> int:
    print(f"Pre-restart gate for llama-guardian ({REPO})")
    results = [
        ("py_compile", check_compile()),
        ("pyflakes", check_pyflakes()),
        ("signatures", check_wrapper_signatures()),
        ("call-sites", check_call_site_signatures()),
        ("pytest", check_pytest()),
    ]
    print("──")
    ok = all(r for _, r in results)
    for name, r in results:
        print(f"  {name}: {'PASS' if r else 'FAIL'}")
    if ok:
        print("✅ ALL GATES PASSED — safe to restart llama-guardian")
    else:
        print("❌ GATE FAILURES — fix before restarting (session drops on restart)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
