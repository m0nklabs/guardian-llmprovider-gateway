"""Every error code the API reference promises must be produced by the code.

`409 queue_admission_rejected` was documented as live behaviour ("one running
request per key") while nothing raised it: the class, its import and its handler
existed and no path produced the error. These tests make that class of lie
fail the suite instead of surviving in prose (#66).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
API_REFERENCE = REPO_ROOT / "docs" / "API_REFERENCE.md"

#: `NNN code_name` pairs in the reference's error model.
_CODE_PATTERN = re.compile(r"`(\d{3}) ([a-z][a-z0-9_]+)`")


def _documented_codes() -> dict[str, str]:
    text = API_REFERENCE.read_text(encoding="utf-8")
    return {match.group(2): match.group(1) for match in _CODE_PATTERN.finditer(text)}


def _app_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(APP_DIR.rglob("*.py"))
    )


def test_every_documented_error_code_is_produced_by_the_code():
    """A documented code with no `raise`/detail site is a promise, not a feature."""
    source = _app_source()
    missing = sorted(code for code in _documented_codes() if f'"{code}"' not in source)
    assert not missing, (
        "documented in docs/API_REFERENCE.md but never produced under app/: "
        f"{missing}"
    )


def test_the_dead_queue_admission_rejection_is_gone():
    """#66: the unreachable 409 must not creep back in as code or as prose."""
    source = _app_source()
    assert "QueueAdmissionRejected" not in source
    assert "queue_admission_rejected" not in source
    assert "queue_admission_rejected" not in _documented_codes()


def test_the_sweep_would_catch_a_fabricated_code(monkeypatch):
    """Prove the guard is not vacuous: a made-up documented code must fail it."""
    fabricated = dict(_documented_codes())
    fabricated["fabricated_error_code"] = "409"

    monkeypatch.setattr(
        "tests.unit.test_documented_error_codes._documented_codes", lambda: fabricated
    )
    source = _app_source()
    missing = sorted(code for code in fabricated if f'"{code}"' not in source)
    assert missing == ["fabricated_error_code"]
