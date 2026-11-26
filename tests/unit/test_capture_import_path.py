"""Pin the import path that keeps PyYAML out of the capture CLI (#135).

The CLI has to reach argument parsing on a machine without PyYAML, so its own
"PyYAML not installed" message can be shown instead of a traceback. Two facts
together deliver that, and neither is obvious from any single file:

1. no module reachable from ``app/capture/__init__.py`` imports PyYAML at
   module scope — the YAML import belongs inside
   :func:`app.capture.config.load_capture_config`;
2. ``app.capture.errors`` imports nothing at all, so naming the configuration
   error never pulls anything in.

These are static checks on purpose: the behavioural side (``guardianctl --help``
in a subprocess without PyYAML) lives in ``test_capture_mode_validation.py``,
and a static test names the offending module directly when someone adds a
module-scope import, instead of only breaking a subprocess assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

CAPTURE_PACKAGE = Path(__file__).resolve().parents[2] / "app" / "capture"


def _module_scope_yaml_imports(path: Path) -> list[int]:
    """Line numbers of ``import yaml`` statements outside any function/class."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in tree.body:
        for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ():
            if isinstance(sub, ast.Import):
                if any(alias.name == "yaml" or alias.name.startswith("yaml.") for alias in sub.names):
                    hits.append(sub.lineno)
            elif isinstance(sub, ast.ImportFrom) and sub.module:
                if sub.module == "yaml" or sub.module.startswith("yaml."):
                    hits.append(sub.lineno)
    return hits


def test_no_capture_module_imports_pyyaml_at_module_scope() -> None:
    offenders = {
        path.name: lines
        for path in sorted(CAPTURE_PACKAGE.glob("*.py"))
        if (lines := _module_scope_yaml_imports(path))
    }
    assert not offenders, (
        "PyYAML must be imported inside the function that parses YAML, not at module "
        f"scope: a module-scope `import yaml` breaks `guardianctl --help` on a host "
        f"without PyYAML (#135). Offending modules: {offenders}"
    )


def test_the_errors_module_imports_nothing() -> None:
    path = CAPTURE_PACKAGE / "errors.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or ".")
    assert imports == ["__future__"], (
        f"{path.name} exists to be importable with no dependencies; it imports {imports}. "
        "Anything else belongs in the module that needs it."
    )


def test_the_config_module_still_reexports_the_one_error_class() -> None:
    """The re-export is what keeps the parser and the CLI sharing one class."""
    from app.capture.config import CaptureConfigError as from_config
    from app.capture.errors import CaptureConfigError as from_errors

    assert from_config is from_errors


def test_a_module_scope_yaml_import_would_be_caught(tmp_path: Path) -> None:
    """Guard the guard: the scanner must actually flag a module-scope import."""
    fake = tmp_path / "fake_module.py"
    fake.write_text("import yaml\n\n\nclass Thing:\n    def f(self):\n        import yaml  # noqa: F401\n", encoding="utf-8")
    assert _module_scope_yaml_imports(fake) == [1]
