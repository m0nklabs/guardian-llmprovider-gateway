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
