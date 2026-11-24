"""Issue #133 — one rule for "is this an ordinary SSE chunk?".

The rule had three implementations: ``_sse_data_payload`` in
``app.cloud_inference.forwarding``, its delegating ``_reported_model_from_line``
and an inline copy inside ``app.capture.stream_assembler``.  The copy existed
only because ``app.capture`` may not import ``app.gateway``
(``capture_dispatch`` imports the assembler, so the direction is circular), and
the copies drifted twice: #128's third review finding dropped the model of any
chunk whose content spelled ``[DONE]``, and #129 fed the degeneration detector
0 of 270 characters for the same reason.

The table below is the contract.  Every consumer is asserted against the *same*
rows, so the next divergence fails here instead of arriving as a bug report,
and the import guards at the bottom keep the neutral module neutral.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.capture.stream_assembler import StreamResponseAssembler
from app.cloud_inference import forwarding
from app.sse import SSE_DATA_PREFIX, SSE_DONE_SENTINEL, sse_data_payload

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Packages that must never appear in ``app.sse``'s import graph: importing one
#: of them from a neutral module is how the ``app.capture``/``app.gateway``
#: cycle comes back.
FORBIDDEN_PACKAGES = ("app.capture", "app.gateway", "app.cloud_inference")


@dataclass(frozen=True)
class ChunkLine:
    """One SSE line plus what every consumer must do with it."""

    line: str
    #: Verdict of the shared rule — the whole stripped payload, or ``None``.
    payload: str | None
    #: Text the capture assembler accumulates from this line alone.
    content: str
    #: Model the cloud sniffer reports (``None`` = nothing usable).
    model: str | None
    why: str


CHUNK_LINES: list[ChunkLine] = [
    ChunkLine(
        line='data: {"choices":[{"delta":{"content":"Hi"}}]}',
        payload='{"choices":[{"delta":{"content":"Hi"}}]}',
        content="Hi",
        model=None,
        why="ordinary-chunk",
    ),
    ChunkLine(
        line='data: {"model":"openai/gpt-4o-2024-11-20","choices":[{"delta":{"content":"Hi"}}]}',
        payload='{"model":"openai/gpt-4o-2024-11-20","choices":[{"delta":{"content":"Hi"}}]}',
        content="Hi",
        model="openai/gpt-4o-2024-11-20",
        why="ordinary-chunk-with-model",
    ),
    ChunkLine(
        line="data: [DONE]",
        payload=None,
        content="",
        model=None,
        why="sentinel",
    ),
    ChunkLine(
        line="data:   [DONE]  ",
        payload=None,
        content="",
        model=None,
        why="whitespace-padded-sentinel",
    ),
    # The two rows the substring tests broke: the marker is *content*, so the
    # line is an ordinary chunk and its model must survive (#128).
    ChunkLine(
        line='data: {"model":"openai/gpt-4o-2024-11-20","choices":[{"delta":{"content":"[DONE]"}}]}',
        payload='{"model":"openai/gpt-4o-2024-11-20","choices":[{"delta":{"content":"[DONE]"}}]}',
        content="[DONE]",
        model="openai/gpt-4o-2024-11-20",
        why="marker-inside-content",
    ),
    ChunkLine(
        line='data: {"choices":[{"delta":{"content":"x [DONE] y"}}]}',
        payload='{"choices":[{"delta":{"content":"x [DONE] y"}}]}',
        content="x [DONE] y",
        model=None,
        why="marker-inside-content-without-model",
    ),
    ChunkLine(
        line="event: content_block_delta",
        payload=None,
        content="",
        model=None,
        why="no-data-prefix",
    ),
    ChunkLine(line="", payload=None, content="", model=None, why="empty-line"),
    ChunkLine(
        line="event: [DONE]",
        payload=None,
        content="",
        model=None,
        why="marker-without-data-prefix",
    ),
    ChunkLine(line="data: ", payload=None, content="", model=None, why="empty-payload"),
    ChunkLine(line="data:     ", payload=None, content="", model=None, why="blank-payload"),
    # Framing only: the rule returns these payloads, the consumer rejects them.
    ChunkLine(
        line="data: {not-json",
        payload="{not-json",
        content="",
        model=None,
        why="malformed-json",
    ),
    ChunkLine(
        line="data: [1,2,3]",
        payload="[1,2,3]",
        content="",
        model=None,
        why="non-dict-payload",
    ),
]

_ROW_IDS = [row.why for row in CHUNK_LINES]


def _by_verbatim(chunk_lines: list[ChunkLine]) -> list[ChunkLine]:
    """Return rows whose expected payload is exactly the line minus its prefix.

    Used to prove the table itself is not tautological: a row only qualifies
    when the expectation was written out by hand, not derived from the rule.
    """
    return [row for row in chunk_lines if row.payload is not None]


@pytest.mark.parametrize("row", CHUNK_LINES, ids=_ROW_IDS)
def test_shared_rule_pins_the_verdict(row: ChunkLine) -> None:
    """The one implementation: payload of an ordinary ``data:`` chunk, else ``None``."""
    assert sse_data_payload(row.line) == row.payload


def test_expected_payloads_are_the_literal_line_remainder() -> None:
    """Anti-tautology check: a pinned payload is the line minus ``data: ``.

    Without this the table could drift into agreeing with a broken rule; with
    it, each non-``None`` expectation is checked against the line it belongs to.
    """
    for row in _by_verbatim(CHUNK_LINES):
        assert row.payload == row.line[len(SSE_DATA_PREFIX) :].strip()


@pytest.mark.parametrize("row", CHUNK_LINES, ids=_ROW_IDS)
def test_capture_assembler_acts_consistently_with_the_rule(row: ChunkLine) -> None:
    """Consumer 3 (the inline copy, #129's module) must agree row by row."""
    assembler = StreamResponseAssembler()
    assembler.add_sse_line(row.line)
    assembled = assembler.assemble()
    assert assembled["content"] == row.content
    if row.model is None:
        # Absent stays absent — no fabricated model on a non-chunk line.
        assert "upstream_model" not in assembled
    else:
        assert assembled["upstream_model"] == row.model


@pytest.mark.parametrize("row", CHUNK_LINES, ids=_ROW_IDS)
def test_cloud_sniffer_acts_consistently_with_the_rule(row: ChunkLine) -> None:
    """Consumer 2 (``_reported_model_from_line``) must agree row by row."""
    assert forwarding._reported_model_from_line(row.line) == row.model


@pytest.mark.parametrize("row", CHUNK_LINES, ids=_ROW_IDS)
def test_degeneration_guard_takes_its_payload_from_the_rule(row: ChunkLine) -> None:
    """Consumer 1's feed decision (what the guard hands the detector).

    The guard's own behavioural pin lives in
    ``tests/unit/test_cloud_forwarding.py``; this one proves the seam it uses
    *is* the shared rule rather than a second copy, row by row.
    """
    assert forwarding._sse_data_payload(row.line) == row.payload


def _code_string_literals(relative_path: str) -> list[str]:
    """Return every string literal in a module's code, docstrings excluded."""
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_capture_assembler_holds_no_copy_of_the_rule() -> None:
    """The assembler must not spell the prefix or the sentinel itself again."""
    literals = _code_string_literals("app/capture/stream_assembler.py")
    assert SSE_DATA_PREFIX not in literals
    assert SSE_DONE_SENTINEL not in literals


def test_forwarding_seam_delegates_and_holds_no_rule() -> None:
    """The forwarding seam is a delegation, not a second implementation."""
    source = inspect.getsource(forwarding._sse_data_payload)
    assert "sse_data_payload(line)" in source
    assert "startswith" not in source


def _app_modules_after_import(module: str) -> set[str]:
    """Import ``module`` in a fresh interpreter; return the ``app.*`` modules pulled in."""
    probe = (
        "import json\n"
        "import sys\n"
        "import {module}\n"
        'print(json.dumps(sorted(m for m in sys.modules if m == "app" or m.startswith("app."))))\n'
    ).format(module=module)
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return set(json.loads(completed.stdout))


def test_app_sse_source_imports_nothing_from_app() -> None:
    """Static half of the guard: no ``app.*`` import statement, not even lazy."""
    tree = ast.parse((REPO_ROOT / "app/sse.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import resolves inside ``app`` — the loophole to close.
            imported.append("<relative>" if node.level else (node.module or ""))
    assert [name for name in imported if name == "app" or name.startswith("app.")] == []
    assert "<relative>" not in imported


def test_importing_app_sse_pulls_in_no_consumer_package() -> None:
    """Dynamic half: a bare ``import app.sse`` imports no capture/gateway/cloud module."""
    pulled = _app_modules_after_import("app.sse")
    leaked = sorted(
        name for name in pulled if any(name == package or name.startswith(f"{package}.") for package in FORBIDDEN_PACKAGES)
    )
    assert leaked == []
    assert pulled == {"app", "app.sse"}


def test_capture_reaches_the_rule_without_the_gateway_cycle() -> None:
    """Why the third copy existed: ``app.capture`` may not pull ``app.gateway``.

    The assembler now reaches the shared rule through the neutral module, so
    the layering constraint that forced the duplicate is gone — and stays gone.
    """
    pulled = _app_modules_after_import("app.capture.stream_assembler")
    assert sorted(name for name in pulled if name == "app.gateway" or name.startswith("app.gateway.")) == []
    assert "app.sse" in pulled
