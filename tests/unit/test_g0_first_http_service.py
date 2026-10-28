"""G0-04 guard: the first HTTP service contract and its read-only fixtures.

``docs/G0_FIRST_HTTP_SERVICE.md`` is the reviewed contract for
``docs/ROADMAP.md`` G0-04 (first non-LLM HTTP service: MoniFuse,
``GET /api/registry/ports`` and ``GET /api/agentlog``). A contract that only
lives in prose rots: the adapter in G2-01 builds against it, so the document,
the captured fixtures and the read-only claim must agree.

This module parses the document as text and the fixtures as JSON. Nothing
leaves the process — no HTTP request, no import of the upstream service, no
network access — so it stays fast and can run in the pre-restart gate. It is
stdlib-only on purpose (``json`` + ``pathlib``): a fixture/contract mismatch
must fail here even in an environment where the app's dependencies are broken.

Failure messages name the document section to fix, because the useful answer to
a red build is "the contract lost a section" or "this fixture stopped matching
what §3 promises", not "something changed".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "docs" / "G0_FIRST_HTTP_SERVICE.md"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "monifuse"

DOC = "docs/G0_FIRST_HTTP_SERVICE.md"

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READ_METHODS = {"GET", "HEAD"}

#: ``| `GET /api/registry/ports` | `tests/fixtures/monifuse/registry_ports.json` | §3.1 | yes |``
_OPERATION_ROW_RE = re.compile(
    r"^\|\s*`(?P<method>[A-Z]+)\s+(?P<path>/\S*)`\s*\|\s*`(?P<fixture>tests/fixtures/\S+)`\s*\|"
)
#: ``### 3.1 `GET /api/registry/ports``` — the contract section of one operation.
_SECTION_HEADING_RE = re.compile(r"^###\s+(?P<number>\d+\.\d+)\s+`(?P<method>[A-Z]+)\s+(?P<path>/\S*)`\s*$")
#: ``- **Method / path:** `GET /api/agentlog```
_METHOD_LINE_RE = re.compile(r"^\s*-\s+\*\*Method / path:\*\*\s+`(?P<method>[A-Z]+)\s+(?P<path>/\S+)`")
#: ``- **Authentication:** none``
_AUTH_LINE_RE = re.compile(r"^\s*-\s+\*\*Authentication:\*\*\s*(?P<value>.+?)\s*$")
#: A write verb followed by an API path, anywhere in a line.
_WRITE_PATH_RE = re.compile(
    r"\b(?P<method>POST|PUT|PATCH|DELETE)\s+`?(?P<path>/(?:api|v1|admin|registry|service)\S*)"
)
#: Words that flag a write path as *not* part of the read-only contract.
_WRITE_FLAG_RE = re.compile(
    r"write|mutat|out of scope|not in scope|prohibit|must not|never|reject|exclud|forbid|removed",
    re.IGNORECASE,
)

#: The contract's documented shape, per operation. Kept here rather than parsed
#: out of the field tables because a table can be reformatted without changing
#: the promise; the promise is what the adapter relies on.
REQUIRED_EVENT_FIELDS = {
    "id": str,
    "ts": str,
    "agent_id": str,
    "agent_type": str,
    "host": str,
    "workspace": str,
    "project": str,
    "event": str,
    "title": str,
    "details": str,
    "artifacts": list,
    "outcome": str,
    "model": str,
    "metrics": dict,
}
REQUIRED_PORT_FIELDS = {
    "port": int,
    "protocol": str,
    "ip": str,
    "description": str,
}
OPTIONAL_PORT_FIELDS = {"host": str, "notes": str}
PORT_PROTOCOLS = {"tcp", "udp"}

EXPECTED_OPERATIONS = {
    ("GET", "/api/registry/ports"),
    ("GET", "/api/agentlog"),
}


# ── Document parsing ───────────────────────────────────────────────────


def _contract_text() -> str:
    assert CONTRACT_PATH.is_file(), (
        f"the G0-04 contract is missing: {DOC}. Roadmap §4 G0 and backlog row "
        "G0-04 require it; restore the file before touching the fixtures."
    )
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _operation_rows(text: str) -> dict[tuple[str, str], str]:
    """``{(method, path): fixture path}`` from the §3.0 operations table."""
    rows: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        match = _OPERATION_ROW_RE.match(line.strip())
        if not match:
            continue
        key = (match.group("method"), match.group("path").rstrip("/"))
        rows[key] = match.group("fixture")
    return rows


def _contract_sections(text: str) -> dict[tuple[str, str], str]:
    """``{(method, path): section body}`` for the per-operation sections."""
    sections: dict[tuple[str, str], str] = {}
    current: tuple[str, str] | None = None
    body: list[str] = []
    for line in text.splitlines():
        heading = _SECTION_HEADING_RE.match(line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(body)
            current = (heading.group("method"), heading.group("path").rstrip("/"))
            body = []
            continue
        if line.startswith("## "):  # a new top-level section ends the current one
            if current is not None:
                sections[current] = "\n".join(body)
            current = None
            body = []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body)
    return sections


def _load_fixture(relative: str) -> object:
    path = REPO_ROOT / relative
    assert path.is_file(), (
        f"the contract names fixture {relative} but it does not exist. "
        f"Either add the fixture or fix the table in {DOC} §3.0."
    )
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:  # pragma: no cover - only on a broken fixture
        raise AssertionError(
            f"fixture {relative} is not valid JSON ({error}); re-capture it and "
            "strip the personal data, do not hand-edit it"
        ) from error


# ── Tests: the document ────────────────────────────────────────────────


def test_documented_operations_match_the_expected_contract():
    """§3.0 lists exactly the two confirmed operations, both `GET`."""
    rows = _operation_rows(_contract_text())
    documented = set(rows)

    missing = EXPECTED_OPERATIONS - documented
    unexpected = documented - EXPECTED_OPERATIONS
    problems: list[str] = []
    if missing:
        problems.append(
            "not in the §3.0 operations table (add the row, its fixture and a contract section):\n  "
            + "\n  ".join(f"{method} {path}" for method, path in sorted(missing))
        )
    if unexpected:
        problems.append(
            "documented but not part of the confirmed G0-04 target "
            "(remove it, or re-open the target decision with the operator):\n  "
            + "\n  ".join(f"{method} {path}" for method, path in sorted(unexpected))
        )
    assert not problems, (
        f"{DOC} §3.0 no longer matches the confirmed contract "
        f"({', '.join(f'{m} {p}' for m, p in sorted(EXPECTED_OPERATIONS))}):\n" + "\n".join(problems)
    )


def test_every_documented_operation_is_read_only_and_has_a_contract_section():
    """No operation row may use a write verb, and each row must have a section."""
    text = _contract_text()
    rows = _operation_rows(text)
    sections = _contract_sections(text)

    problems: list[str] = []
    for (method, path), fixture in sorted(rows.items()):
        if method not in READ_METHODS:
            problems.append(
                f"§3.0 lists `{method} {path}` as a contract operation, but the contract is "
                "read-only; move it to §2 (rejected) or §4 (write neighbours) instead"
            )
        if (method, path) not in sections:
            problems.append(
                f"§3.0 lists `{method} {path}` but no contract section of the form "
                f"'### 3.x `{method} {path}`' documents its method, parameters and response"
            )
        if not re.search(r"`?GET`?", method):  # pragma: no cover - defensive
            problems.append(f"§3.0 lists `{method} {path}`; the target is GET-only")

    assert not problems, (
        f"{DOC} advertises operations it does not document as read-only:\n  " + "\n  ".join(problems)
    )


def test_every_contract_section_is_listed_and_declares_no_authentication():
    """A documented section must be in the table and must state its auth stance."""
    text = _contract_text()
    rows = _operation_rows(text)
    sections = _contract_sections(text)

    problems: list[str] = []
    for key, body in sorted(sections.items()):
        method, path = key
        if key not in rows:
            problems.append(
                f"contract section '{method} {path}' is missing from the §3.0 operations table "
                "(add it there with its fixture)"
            )

        method_lines = [m for m in (_METHOD_LINE_RE.match(line) for line in body.splitlines()) if m]
        if not method_lines:
            problems.append(f"section {path} has no '- **Method / path:** `{method} {path}`' line")
        elif (method_lines[0].group("method"), method_lines[0].group("path").rstrip("/")) != key:
            problems.append(
                f"section {path} declares method/path "
                f"{method_lines[0].group('method')} {method_lines[0].group('path')}, not {method} {path}"
            )

        auth_values = [m.group("value").lower() for m in (_AUTH_LINE_RE.match(line) for line in body.splitlines()) if m]
        if not auth_values:
            problems.append(
                f"section {path} does not state its authentication stance "
                "('- **Authentication:** none'); G1 must not have to guess"
            )
        elif not any(value.startswith("none") for value in auth_values):
            problems.append(
                f"section {path} states authentication {auth_values[0]!r}; the captured upstream "
                "requires none — re-verify before changing this line"
            )

    assert not problems, f"{DOC} §3 has incomplete operation sections:\n  " + "\n  ".join(problems)


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """``[(first line number, block text)]`` split on blank lines.

    A markdown paragraph or list item wraps across lines, so the disqualifying
    word ("write", "prohibited", ...) legitimately lands on a different line
    than the endpoint it qualifies. Judging a whole block keeps the check about
    intent instead of about line wrapping.
    """
    blocks: list[tuple[int, str]] = []
    start = 1
    current: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not current:
                start = number
            current.append(line)
            continue
        if current:
            blocks.append((start, "\n".join(current)))
            current = []
    if current:
        blocks.append((start, "\n".join(current)))
    return blocks


def test_write_paths_are_never_advertised_as_read_only():
    """Every write verb in the contract is flagged as a write or as out of scope.

    The document *must* mention MoniFuse's write endpoints — they are the reason
    the adapter uses a path allowlist instead of a base-URL pass-through (§4).
    Mentioning is fine; mentioning one without a disqualifying word in the same
    paragraph is not, because a reader (or G2-01) could take it for part of the
    read-only contract.
    """
    problems: list[str] = []
    for first_line, block in _paragraphs(_contract_text()):
        if not _WRITE_PATH_RE.search(block):
            continue
        if _WRITE_FLAG_RE.search(block):
            continue
        excerpt = " / ".join(line.strip() for line in block.splitlines())[:200]
        problems.append(f"paragraph at line {first_line}: {excerpt}")
    assert not problems, (
        f"{DOC} mentions write endpoints without flagging them as write/mutating or out of scope "
        "— a reader (or G2-01) could take them for part of the read-only contract:\n  "
        + "\n  ".join(problems)
    )


def test_documented_paths_appear_beyond_the_operations_table():
    """The paths are stated in the prose too, not only in the §3.0 table."""
    text = _contract_text()
    problems = [
        f"{method} {path}"
        for method, path in sorted(EXPECTED_OPERATIONS)
        if text.count(f"{method} {path}") < 2
    ]
    assert not problems, (
        f"{DOC} names these operations fewer than twice (table + prose contract): {problems}"
    )


# ── Tests: the fixtures ────────────────────────────────────────────────


def test_every_documented_operation_has_a_fixture_that_parses():
    """Each §3.0 row resolves to a fixture file holding valid JSON."""
    problems: list[str] = []
    for (method, path), fixture in sorted(_operation_rows(_contract_text()).items()):
        target = REPO_ROOT / fixture
        if not target.is_file():
            problems.append(f"{method} {path}: fixture {fixture} is missing (§3.0)")
            continue
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            problems.append(f"{method} {path}: fixture {fixture} is not valid JSON ({error})")
    assert not problems, (
        f"{DOC} §3.0 promises a fixture per operation; these do not hold up:\n  " + "\n  ".join(problems)
    )


def test_fixture_directory_holds_no_undocumented_files():
    """The fixture directory is exactly the fixtures the contract names."""
    assert FIXTURE_DIR.is_dir(), (
        f"fixture directory {FIXTURE_DIR.relative_to(REPO_ROOT).as_posix()} is missing; "
        f"{DOC} §5 points at it"
    )
    named = {
        (REPO_ROOT / fixture).resolve()
        for fixture in _operation_rows(_contract_text()).values()
    }
    present = {path.resolve() for path in FIXTURE_DIR.glob("*.json")}
    problems: list[str] = []
    if present - named:
        problems.append(
            "fixtures exist that the §3.0 table does not name (add the row with its contract "
            "section, or delete the file):\n  "
            + "\n  ".join(sorted(path.name for path in present - named))
        )
    if named - present:
        problems.append(
            "the §3.0 table names fixtures that are not in "
            f"{FIXTURE_DIR.relative_to(REPO_ROOT).as_posix()}:\n  "
            + "\n  ".join(sorted(path.name for path in named - present))
        )
    assert not problems, f"{DOC} §3.0 and the fixture directory disagree:\n" + "\n".join(problems)


def test_registry_ports_fixture_matches_the_documented_shape():
    """§3.1 promises a bare array of port objects with the documented fields."""
    fixture = "tests/fixtures/monifuse/registry_ports.json"
    data = _load_fixture(fixture)
    where = f"{fixture} does not match {DOC} §3.1 (the response is a bare JSON array)"

    assert isinstance(data, list), f"{where}: top level is {type(data).__name__}, not a list"
    assert data, f"{where}: the captured array is empty, so it pins nothing"
    assert len(data) <= 10, (
        f"{where}: the fixture holds {len(data)} entries; §5 records a trimmed capture — "
        "keep it small (a couple of entries plus any optional-shape example)"
    )

    problems: list[str] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            problems.append(f"[{index}] is {type(entry).__name__}, not an object")
            continue
        for field, expected in REQUIRED_PORT_FIELDS.items():
            if field not in entry:
                problems.append(f"[{index}] is missing required field {field!r}")
                continue
            if not isinstance(entry[field], expected):
                problems.append(
                    f"[{index}].{field} is {type(entry[field]).__name__}, "
                    f"§3.1.1 documents {expected.__name__}"
                )
        for field, expected in OPTIONAL_PORT_FIELDS.items():
            if field in entry and not isinstance(entry[field], expected):
                problems.append(
                    f"[{index}].{field} is {type(entry[field]).__name__}, "
                    f"§3.1.1 documents {expected.__name__}"
                )
        extra = set(entry) - set(REQUIRED_PORT_FIELDS) - set(OPTIONAL_PORT_FIELDS)
        if extra:
            problems.append(
                f"[{index}] carries undocumented field(s) {sorted(extra)}; §3.1.1 must document them"
            )
        if entry.get("protocol") not in PORT_PROTOCOLS:
            problems.append(
                f"[{index}].protocol is {entry.get('protocol')!r}; §3.1.1 documents only "
                f"{sorted(PORT_PROTOCOLS)}"
            )

    assert not problems, where + ":\n  " + "\n  ".join(problems)


def test_agentlog_fixture_matches_the_documented_shape():
    """§3.2 promises `{events, total}` with the fourteen documented event fields."""
    fixture = "tests/fixtures/monifuse/agentlog.json"
    data = _load_fixture(fixture)
    where = f"{fixture} does not match {DOC} §3.2 (the response is an object with 'events' + 'total')"

    assert isinstance(data, dict), f"{where}: top level is {type(data).__name__}, not an object"
    assert set(data) == {"events", "total"}, (
        f"{where}: top-level keys are {sorted(data)}; §3.2.2 documents exactly "
        "['events', 'total']"
    )
    assert isinstance(data["events"], list), f"{where}: 'events' is not a list"
    assert isinstance(data["total"], int) and not isinstance(data["total"], bool), (
        f"{where}: 'total' is {data['total']!r}, §3.2.2 documents an integer"
    )
    assert data["total"] >= len(data["events"]), (
        f"{where}: total={data['total']} is smaller than len(events)={len(data['events'])}; "
        "§3.2.2 documents total as the match count ignoring 'limit'"
    )
    assert data["events"], f"{where}: 'events' is empty, so it pins no event shape"
    assert len(data["events"]) <= 5, (
        f"{where}: the fixture holds {len(data['events'])} events; §5 records a two-event capture"
    )

    problems: list[str] = []
    for index, event in enumerate(data["events"]):
        if not isinstance(event, dict):
            problems.append(f"events[{index}] is {type(event).__name__}, not an object")
            continue
        for field, expected in REQUIRED_EVENT_FIELDS.items():
            if field not in event:
                problems.append(f"events[{index}] is missing required field {field!r}")
            elif not isinstance(event[field], expected):
                problems.append(
                    f"events[{index}].{field} is {type(event[field]).__name__}, "
                    f"§3.2.3 documents {expected.__name__}"
                )
        extra = set(event) - set(REQUIRED_EVENT_FIELDS)
        if extra:
            problems.append(
                f"events[{index}] carries undocumented field(s) {sorted(extra)}; §3.2.3 must document them"
            )
        artifacts = event.get("artifacts")
        if isinstance(artifacts, list) and not all(isinstance(item, str) for item in artifacts):
            problems.append(f"events[{index}].artifacts must be a list of strings (§3.2.3)")

    assert not problems, where + ":\n  " + "\n  ".join(problems)


def test_documented_field_names_are_stated_in_the_contract():
    """Every field the guard requires is named in the document's field tables.

    The fixtures pin the shape; this keeps the human-readable tables honest, so
    the document alone stays usable for whoever writes the G2-01 adapter.
    """
    text = _contract_text()
    problems = [
        f"{name} (named in the fixture/guard but not in {DOC} §3.1.1/§3.2.3)"
        for name in sorted(
            set(REQUIRED_PORT_FIELDS) | set(OPTIONAL_PORT_FIELDS) | set(REQUIRED_EVENT_FIELDS)
        )
        if f"`{name}`" not in text
    ]
    assert not problems, (
        f"{DOC} no longer names these response fields, so the field-by-field meaning is lost:\n  "
        + "\n  ".join(problems)
    )
