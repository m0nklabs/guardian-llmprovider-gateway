"""Every error code the API reference promises must be produced by the code.

`409 queue_admission_rejected` was documented as live behaviour ("one running
request per key") while nothing raised it: the class, its import and its handler
existed and no path produced the error. These tests make that class of lie
fail the suite instead of surviving in prose (#66).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
API_REFERENCE = REPO_ROOT / "docs" / "API_REFERENCE.md"

#: Strict form: one code span holding both, e.g. `` `409 queue_admission_rejected` ``.
_CODE_PATTERN = re.compile(r"`(\d{3}) ([a-z][a-z0-9_]+)`")

#: Tolerant form: the status and the name backticked separately, or split over a
#: line break, e.g. `` `409` `queue_admission_rejected` `` or ``409\n`queue_full` ``.
#: The name must contain an underscore, which every real code does — that keeps
#: this from pairing a status with an ordinary following word.
_SPLIT_CODE_PATTERN = re.compile(r"\b(\d{3})\s+([a-z][a-z0-9]*_[a-z0-9_]+)\b")


def _documented_codes() -> dict[str, str]:
    """Return ``{code_name: status}`` for every code the reference documents.

    Both the strict and the split form are parsed: a stale claim that writes the
    status and the name as two code spans (or across a line break) must not slip
    past the reachability check (PR-Piet on #68).
    """
    text = API_REFERENCE.read_text(encoding="utf-8")
    codes = {match.group(2): match.group(1) for match in _CODE_PATTERN.finditer(text)}
    unbackticked = text.replace("`", "")
    for match in _SPLIT_CODE_PATTERN.finditer(unbackticked):
        codes.setdefault(match.group(2), match.group(1))
    return codes


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


@pytest.mark.parametrize(
    "line",
    [
        "| `409 queue_admission_rejected` | one slot |",
        "| `409` `queue_admission_rejected` | one slot |",
        "| 409\n`queue_admission_rejected` | one slot |",
        "`409`\n`queue_admission_rejected`",
    ],
    ids=["strict", "two-spans", "status-then-linebreak", "both-backticked-linebreak"],
)
def test_the_parser_sees_every_documented_format(line):
    """A stale claim must be parsed however it is formatted."""
    strict = {m.group(2): m.group(1) for m in _CODE_PATTERN.finditer(line)}
    unbackticked = line.replace("`", "")
    split = {m.group(2): m.group(1) for m in _SPLIT_CODE_PATTERN.finditer(unbackticked)}
    assert "queue_admission_rejected" in {**strict, **split}, (strict, split)
