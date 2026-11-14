"""Gate 4b must skip uninstalled top-level packages instead of flagging drift.

The cross-module call-site check resolves callees by importing them. App code
may import a third-party SDK lazily inside a function (the Copilot bridge does),
and the deployment venv deliberately does not carry that SDK. An absent package
is not a renamed/removed callable: issue #107.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "pre_restart_check.py"

RUNNER = """\
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location("pre_restart_check_fixture", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module  # dataclass resolution needs the module registered
spec.loader.exec_module(module)
module.REPO = pathlib.Path(sys.argv[2])
module.APP = module.REPO / "app"
ok = module.check_call_site_signatures()
print("GATE4B", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
"""

LAZY_SDK = '''\
"""Calls an SDK that the deployment venv intentionally does not carry."""


def build():
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key="k", base_url="http://127.0.0.1:1/v1")
'''

MISSING_APP_MODULE = '''\
"""References an app submodule that does not exist - genuine drift."""


def load():
    from app.nope import Missing

    return Missing()
'''

WRONG_ATTRIBUTE = '''\
"""Calls an attribute that the installed module does not have."""

import json


def dump():
    return json.not_a_real_method({})
'''

WRONG_FROM_IMPORT = '''\
"""Imports a name that the installed module does not export."""

from os import not_a_real_name


def go():
    return not_a_real_name()
'''


def _write_tree(tmp_path: Path, *sources: tuple[str, str]) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    for name, text in sources:
        (tmp_path / "app" / name).write_text(text, encoding="utf-8")
    return tmp_path


def _run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", RUNNER, str(CHECKER), str(root)],
        cwd=root, capture_output=True, text=True, check=False, timeout=120,
    )


def test_uninstalled_top_level_package_is_skipped_not_failed(tmp_path):
    root = _write_tree(tmp_path, ("uses_lazy_sdk.py", LAZY_SDK))
    result = _run_gate(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "openai.AsyncOpenAI does not exist" not in result.stdout
    assert "OK (all resolved call sites match callee signatures)" in result.stdout


def test_missing_app_module_and_wrong_attributes_still_fail(tmp_path):
    root = _write_tree(
        tmp_path,
        ("uses_lazy_sdk.py", LAZY_SDK),
        ("missing_app_module.py", MISSING_APP_MODULE),
        ("wrong_attribute.py", WRONG_ATTRIBUTE),
        ("wrong_from_import.py", WRONG_FROM_IMPORT),
    )
    result = _run_gate(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "app.nope.Missing does not exist" in result.stdout
    assert "not_a_real_method does not exist on json" in result.stdout
    assert "os.not_a_real_name does not exist" in result.stdout
    # The absent-SDK site stays a skip even while other sites fail.
    assert "uses_lazy_sdk" not in result.stdout
