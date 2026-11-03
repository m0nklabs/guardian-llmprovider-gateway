#!/usr/bin/env python3
"""Prepare the production checkout for a deployment: switch to ``main``, reconcile config.

Issue #85: the systemd unit runs the operator's checkout, so ``systemctl
restart`` deploys whatever ``HEAD`` that checkout is on.  On 2026-10-29 that was
``b30e027`` — a feature branch 29 commits *behind* ``main`` — so a restart would
have deployed old code while every gate passed.  The operator's decision is that
the checkout tracks ``main``, with the live ``config/global.settings.yaml``
reconciled once and kept as the local delta.

This script turns that manual step into one reviewable command:

  1. preflight  — branch, HEAD, ``origin/main``, ahead/behind, fast-forward?
  2. preserve   — the exact dirty and untracked files, the backup name, the stash
  3. reconcile  — ``main``'s config plus the local deltas that must survive
  4. review     — the merged file and a diff against both inputs
  5. ``--apply`` — backup, stash, ``checkout main``, ``pull --ff-only``, install, stop

It never restarts the service, never pushes and never merges: the last thing it
does is print the commands the operator runs.  The default is a dry run that
writes nothing.

**YAML approach: text-level surgical edits, not a round-trip dump.**
``ruamel.yaml`` is not installed in this venv (``./venv/bin/python -c "import
ruamel.yaml"`` → ``ModuleNotFoundError``), and the operator's file is heavily
commented — a ``yaml.safe_dump`` round-trip would destroy every comment and
reflow the whole file.  So the merged file is built by taking **``main``'s text
verbatim as the base** and rewriting only the declared keys, line by line, plus
inserting the local comment lines ``main`` dropped.  Nothing else is touched,
which is also what makes the result verifiable: the merged text is re-parsed and
compared against the intended document, and a line diff against ``main`` may
contain only the declared value edits and the preserved comment lines (see
:func:`verify_merged`).  If ``ruamel.yaml`` ever lands in the venv this stays
correct — it is simply not needed.

Exit codes: ``0`` = plan produced / apply succeeded, ``1`` = preflight refused
(a human decision is needed), ``2`` = reconciliation, verification or apply
failed, or the tool cannot run (no PyYAML).

Usage:
    ./venv/bin/python scripts/prepare_deployment.py
    ./venv/bin/python scripts/prepare_deployment.py --queue-timeout 1800
    ./venv/bin/python scripts/prepare_deployment.py --write-reconciled
    ./venv/bin/python scripts/prepare_deployment.py --apply
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import difflib
import os
import pathlib
import re
import shutil
import stat
import sys
from typing import Any, Sequence

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - the venv always has it
    # A deployment tool must not greet the operator with a traceback.  PyYAML is
    # in requirements.txt, so this only fires when the wrong interpreter is used.
    print(
        "prepare_deployment.py needs PyYAML (it is in requirements.txt): run it with "
        "./venv/bin/python, not a bare python",
        file=sys.stderr,
    )
    raise SystemExit(2)

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# The deployment-head guard from #87 lives in the restart gate; this tool reuses
# it instead of re-deriving ancestry, ahead/behind counts and porcelain status.
import pre_restart_check as gate  # noqa: E402

#: The runtime input this tool reconciles, relative to the repository root.
CONFIG_REL = "config/global.settings.yaml"

#: Where the proposal is written for review (never installed from here).
RECONCILED_REL = CONFIG_REL + ".reconciled"

#: ``config/global.settings.yaml.<UTC timestamp>.bak``, next to the file.
BACKUP_SUFFIX = ".bak"

#: The stash message names the tool and the moment, so `git stash list` is readable.
STASH_PREFIX = "prepare_deployment: local state before main switch"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2

#: ``keep-local`` keeps the live value; ``operator-choice`` keeps it by default
#: and lets ``--queue-timeout`` override it.
KEEP_LOCAL = "keep-local"
OPERATOR_CHOICE = "operator-choice"

#: Git subcommands this tool must never run: the operator owns restart, push and
#: merge.  Enforced in :func:`run_git`, not just in the prose.
FORBIDDEN_GIT = ("push", "merge", "rebase", "reset", "clean", "commit", "tag", "remote")


# ── The declared local deltas ──────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ValueRule:
    """One key whose live value must survive the switch to ``main``."""

    key: str
    policy: str
    reason: str


@dataclasses.dataclass(frozen=True)
class CommentRule:
    """One comment block the operator wrote that ``main`` does not carry."""

    key: str
    #: ``leading`` = the comment lines directly above the key; ``block`` = the
    #: comment lines inside the key's mapping.
    where: str
    reason: str


#: The three local deltas from the #85 reconciliation comment.  The values are
#: *read from both files* — nothing here hardcodes 420 or 300.
VALUE_RULES = (
    ValueRule(
        key="failover.remote_ensure_timeout_seconds",
        policy=KEEP_LOCAL,
        reason=(
            "the live value covers the 125B Flash-Next cold load (~210 s on ai-kvm2); "
            "main's 120 s would abort that load"
        ),
    ),
    ValueRule(
        key="queue.queue_timeout_seconds",
        policy=OPERATOR_CHOICE,
        reason=(
            "policy choice, not a bug: 300 s = a queued request gives up after 5 min; "
            "1800 s = main's shipped default, sized for a 125B cold load behind "
            "max_concurrent: 1"
        ),
    ),
)

COMMENT_RULES = (
    CommentRule(
        key="cloud_retry",
        where="block",
        reason=(
            "the 2026-10-24 decision record (the gateway forwards upstream 429s as-is, "
            "pi owns backoff) — main dropped it"
        ),
    ),
    CommentRule(
        key="failover",
        where="leading",
        reason=(
            "the ai-kvm2 cold-load rationale for the 420 s value main would reset to 120 s"
        ),
    ),
)


# ── A small block-mapping reader (enough for this file, and no more) ───

_KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_][A-Za-z0-9_.\-]*):(?P<rest>.*)$")


def path_map(lines: Sequence[str]) -> dict[int, tuple[str, ...]]:
    """Map each block-mapping key line to its dotted path.

    A deliberately small YAML-subset reader: block mappings only, which is what
    ``config/global.settings.yaml`` is.  Sequence items (``- x``) and flow
    mappings (``- {host: x}``) never match, so they cannot be mistaken for keys.
    """
    stack: list[tuple[int, str]] = []
    out: dict[int, tuple[str, ...]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if match is None:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        out[index] = tuple(key for _indent, key in stack) + (match.group("key"),)
        if not match.group("rest").strip():
            stack.append((indent, match.group("key")))
    return out


def find_key(paths: dict[int, tuple[str, ...]], path: tuple[str, ...]) -> int | None:
    """Return the line index of *path*, or ``None`` when it is not a key line."""
    for index, found in paths.items():
        if found == path:
            return index
    return None


def leading_comments(lines: Sequence[str], index: int) -> list[str]:
    """The contiguous comment lines directly above ``lines[index]``."""
    out: list[str] = []
    cursor = index - 1
    while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
        out.append(lines[cursor])
        cursor -= 1
    return list(reversed(out))


def leading_block_comments(lines: Sequence[str], index: int) -> list[str]:
    """The comment lines at the *top* of the mapping that starts at ``lines[index]``.

    Only the contiguous run directly after the header counts.  A comment that sits
    further down, between mapping entries, is part of that entry's documentation
    and must never be moved or replaced — treating the whole block as one comment
    region would delete the entry it follows.
    """
    out: list[str] = []
    for line in lines[index + 1:]:
        if not line.strip() or not line.lstrip().startswith("#"):
            break
        out.append(line)
    return out


def merge_comment_block(main_block: Sequence[str], local_block: Sequence[str]) -> list[str]:
    """``main``'s comment block with the local lines it dropped put back in order.

    Each extra line is inserted before the first ``main`` line that follows it in
    the local block, so the operator's own ordering survives instead of the
    extras being appended at the end.
    """
    known = {line.strip() for line in main_block}
    result = list(main_block)
    for position, extra in enumerate(local_block):
        if extra.strip() in known:
            continue
        anchor = next(
            (line.strip() for line in local_block[position + 1:] if line.strip() in known),
            None,
        )
        if anchor is None:
            result.append(extra)
            continue
        index = next(i for i, line in enumerate(result) if line.strip() == anchor)
        result.insert(index, extra)
    return result


def format_scalar(value: Any) -> str:
    """Render *value* the way the surrounding file writes scalars."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    # A plain scalar may contain ":" as long as it is not followed by a space
    # (``2026-11-11T00:00:00Z`` is fine, ``a: b`` is not).
    needs_quotes = (
        not text
        or text.strip() != text
        or text.endswith(":")
        or ": " in text
        or any(ch in text for ch in "#{}[],&*!|>'\"%@`")
    )
    if needs_quotes:
        return "'" + text.replace("'", "''") + "'"
    return text


def parse_scalar(text: str) -> Any:
    """Parse a scalar the way YAML would, so a rewrite can be checked."""
    return yaml.safe_load(text)


def replace_value_line(line: str, value: Any) -> str:
    """Rewrite the scalar of one ``key: value`` line, keeping its comment."""
    head, sep, rest = line.partition(":")
    if not sep:  # pragma: no cover - callers only pass key lines
        raise ValueError(f"not a key line: {line!r}")
    comment = ""
    match = re.search(r"\s+#", rest)
    if match is not None:
        # Keep the original gap before the comment, so the line stays aligned.
        comment = rest[match.start():]
    return f"{head}: {format_scalar(value)}{comment}"


def get_path(doc: Any, path: Sequence[str]) -> tuple[bool, Any]:
    """Return ``(found, value)`` for a dotted path in a parsed document."""
    node = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def set_path(doc: Any, path: Sequence[str], value: Any) -> None:
    """Set a dotted path in a parsed document (the parents must exist)."""
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


# ── The reconciliation ─────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Delta:
    """One declared key, with the value each side carries and the choice made."""

    key: str
    local: Any
    main: Any
    chosen: Any
    action: str
    reason: str

    @property
    def rewrites_main(self) -> bool:
        """True when the merged file must carry a value ``main`` does not have."""
        return self.action in (KEEP_LOCAL, OPERATOR_CHOICE) and self.chosen != self.main


@dataclasses.dataclass(frozen=True)
class PreservedComment:
    """A local comment block that ``main`` dropped and the merge puts back."""

    key: str
    where: str
    lines: tuple[str, ...]
    reason: str


@dataclasses.dataclass(frozen=True)
class Reconciliation:
    """The merged config plus everything needed to review and verify it."""

    merged_text: str
    deltas: tuple[Delta, ...]
    preserved: tuple[PreservedComment, ...]
    problems: tuple[str, ...]
    main_text: str
    local_text: str

    @property
    def ok(self) -> bool:
        return not self.problems


def reconcile(
    main_text: str,
    local_text: str,
    *,
    queue_timeout: int | None = None,
) -> Reconciliation:
    """Build ``main``'s config plus the local deltas that must survive.

    ``main``'s text is the base, so every new block (``admission``, ``audit``,
    ``auth.scopes_enforced_after``, ``destination_policy``) is adopted exactly as
    shipped — this function never invents a value for them.  Only the keys named
    in :data:`VALUE_RULES` are rewritten, and only the comment lines named in
    :data:`COMMENT_RULES` are inserted.
    """
    problems: list[str] = []
    try:
        main_doc = yaml.safe_load(main_text)
    except yaml.YAMLError as exc:
        return Reconciliation("", (), (), (f"origin/main's config is not valid YAML: {exc}",),
                              main_text, local_text)
    try:
        local_doc = yaml.safe_load(local_text)
    except yaml.YAMLError as exc:
        return Reconciliation("", (), (), (f"the live config is not valid YAML: {exc}",),
                              main_text, local_text)
    if not isinstance(main_doc, dict):
        return Reconciliation("", (), (), ("origin/main's config is not a mapping",),
                              main_text, local_text)
    if not isinstance(local_doc, dict):
        return Reconciliation("", (), (), ("the live config is not a mapping",),
                              main_text, local_text)

    main_lines = main_text.splitlines()
    merged = list(main_lines)
    main_paths = path_map(main_lines)
    deltas: list[Delta] = []

    for rule in VALUE_RULES:
        path = tuple(rule.key.split("."))
        local_found, local_value = get_path(local_doc, path)
        main_found, main_value = get_path(main_doc, path)
        if not main_found:
            problems.append(
                f"{rule.key}: origin/main's config has no such key — refusing to invent it"
            )
            continue
        # An explicit operator choice is applied before the "nothing to do" cases:
        # `--queue-timeout 600` must win even when both files already say 1800, and
        # even when the live file has no such key at all.
        if rule.policy == OPERATOR_CHOICE and queue_timeout is not None:
            deltas.append(Delta(
                rule.key,
                local_value if local_found else None,
                main_value,
                queue_timeout,
                OPERATOR_CHOICE,
                rule.reason + f"; --queue-timeout {queue_timeout} overrides the live value",
            ))
            index = find_key(main_paths, path)
            if index is None:
                problems.append(f"{rule.key}: could not locate the key line in origin/main's config")
            elif queue_timeout != main_value:
                merged[index] = replace_value_line(merged[index], queue_timeout)
            continue
        if not local_found:
            deltas.append(Delta(rule.key, None, main_value, main_value, "take-main",
                                "the live file has no value for this key; main's value is used"))
            continue
        if local_value == main_value:
            deltas.append(Delta(rule.key, local_value, main_value, main_value, "same",
                                "identical in both files"))
            continue
        if rule.policy == KEEP_LOCAL:
            chosen, action = local_value, KEEP_LOCAL
            reason = rule.reason
        else:
            chosen, action = local_value, OPERATOR_CHOICE
            reason = rule.reason + "; keeping the live value (--queue-timeout overrides it)"
        deltas.append(Delta(rule.key, local_value, main_value, chosen, action, reason))
        if chosen == main_value:
            continue
        index = find_key(main_paths, path)
        if index is None:
            problems.append(f"{rule.key}: could not locate the key line in origin/main's config")
            continue
        merged[index] = replace_value_line(merged[index], chosen)

    # Comment preservation runs after the value edits so the line indices are
    # still the ones `path_map` reported for main's text.
    local_lines = local_text.splitlines()
    local_paths = path_map(local_lines)
    merged_paths = path_map(merged)
    preserved: list[PreservedComment] = []
    edits: list[tuple[int, int, list[str]]] = []
    for rule in COMMENT_RULES:
        path = (rule.key,)
        local_index = find_key(local_paths, path)
        merged_index = find_key(merged_paths, path)
        if local_index is None or merged_index is None:
            continue
        if rule.where == "leading":
            local_comments = leading_comments(local_lines, local_index)
            main_comments = leading_comments(merged, merged_index)
            start = merged_index - len(main_comments)
            end = merged_index
        else:
            local_comments = leading_block_comments(local_lines, local_index)
            main_comments = leading_block_comments(merged, merged_index)
            start = merged_index + 1
            end = start + len(main_comments)
        known = {line.strip() for line in main_comments}
        extra = [line for line in local_comments if line.strip() not in known]
        if not extra:
            continue
        preserved.append(PreservedComment(rule.key, rule.where, tuple(extra), rule.reason))
        edits.append((start, end, merge_comment_block(main_comments, local_comments)))

    # Apply from the bottom up so the earlier indices stay valid.
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        merged[start:end] = replacement

    merged_text = "\n".join(merged)
    if main_text.endswith("\n"):
        merged_text += "\n"

    problems.extend(verify_merged(merged_text, main_text, deltas, preserved))
    return Reconciliation(
        merged_text=merged_text,
        deltas=tuple(deltas),
        preserved=tuple(preserved),
        problems=tuple(problems),
        main_text=main_text,
        local_text=local_text,
    )


def verify_merged(
    merged_text: str,
    main_text: str,
    deltas: Sequence[Delta],
    preserved: Sequence[PreservedComment],
) -> list[str]:
    """Prove the merged text is ``main`` plus exactly the declared deltas.

    Three independent checks, because a text-level merge is only as good as its
    proof: the document must equal the intended one, every preserved comment
    line must be present, and a line diff against ``main`` may contain nothing
    but those comment insertions and the declared value rewrites.
    """
    problems: list[str] = []
    try:
        merged_doc = yaml.safe_load(merged_text)
    except yaml.YAMLError as exc:
        return [f"the merged file is not valid YAML: {exc}"]

    intended = yaml.safe_load(main_text)
    for delta in deltas:
        if delta.rewrites_main:
            set_path(intended, delta.key.split("."), delta.chosen)
    if merged_doc != intended:
        problems.append(
            "the merged document does not equal main's document plus the declared deltas"
        )

    merged_lines = merged_text.splitlines()
    for item in preserved:
        for line in item.lines:
            if line not in merged_lines:
                problems.append(f"preserved comment line missing from the merged file: {line!r}")

    allowed_inserts = {line for item in preserved for line in item.lines}
    changed = {delta.key: delta for delta in deltas if delta.rewrites_main}
    main_lines = main_text.splitlines()
    main_paths = path_map(main_lines)
    matcher = difflib.SequenceMatcher(a=main_lines, b=merged_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for line in merged_lines[j1:j2]:
                if line not in allowed_inserts:
                    problems.append(f"unexpected inserted line: {line!r}")
            continue
        if tag == "delete":
            problems.append(f"unexpected deleted line(s): {main_lines[i1:i2]!r}")
            continue
        if (i2 - i1) != (j2 - j1):
            problems.append(f"unexpected line-count change at main line {i1 + 1}")
            continue
        for offset, (old, new) in enumerate(zip(main_lines[i1:i2], merged_lines[j1:j2])):
            path = main_paths.get(i1 + offset)
            key = ".".join(path) if path else ""
            delta = changed.get(key)
            if delta is None:
                problems.append(
                    f"unexpected edit at main line {i1 + offset + 1}: {old!r} -> {new!r}"
                )
                continue
            if parse_scalar(new.partition(":")[2]) != delta.chosen:
                problems.append(f"{key}: the merged line does not carry the chosen value")
    return problems


# ── Reading the tree ───────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class WorktreeEntry:
    """One ``git status --porcelain`` line, with both sides of a rename."""

    code: str
    path: str

    @property
    def untracked(self) -> bool:
        return self.code == "??"


def read_worktree(repo: pathlib.Path) -> tuple[tuple[WorktreeEntry, ...], str]:
    """List every dirty and untracked path (``-uall``: no collapsed directories).

    ``-z`` is not cosmetic: the display form quotes a path with non-ASCII bytes or
    a space (``"docs/caf\303\251.md"``) and separates a rename with `` -> ``,
    which a filename may itself contain.  NUL-delimited records are the raw paths,
    so the pathspecs handed to ``git stash`` are the ones git reported.
    """
    rc, out, err = gate._git(("status", "--porcelain", "-z", "-uall"), repo=repo)
    if rc != 0:
        return (), err or f"git status exited {rc}"
    entries: list[WorktreeEntry] = []
    tokens = out.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code = token[:2]
        entries.append(WorktreeEntry(code, token[3:]))
        # With -z a rename/copy reports the destination first and the original
        # second; both sides matter, because the original is what disappears.
        if (code[0] in "RC" or code[1] in "RC") and index < len(tokens) and tokens[index]:
            entries.append(WorktreeEntry(code, tokens[index]))
            index += 1
    return tuple(entries), ""


def read_local_main(repo: pathlib.Path) -> str:
    """The sha of the local ``main`` branch, or ``""`` when it does not exist."""
    rc, out, _err = gate._git(("rev-parse", "--verify", "refs/heads/main"), repo=repo)
    return out if rc == 0 else ""


def read_main_config(repo: pathlib.Path) -> tuple[str, str]:
    """``(text, error)`` for ``origin/main:config/global.settings.yaml``."""
    rc, out, err = gate._git(("show", f"origin/main:{CONFIG_REL}"), repo=repo)
    if rc != 0:
        return "", err or f"git show origin/main:{CONFIG_REL} exited {rc}"
    return out + "\n", ""


def read_local_config(repo: pathlib.Path) -> tuple[str, str]:
    """``(text, error)`` for the live config in the working tree."""
    path = repo / CONFIG_REL
    try:
        return path.read_text(), ""
    except OSError as exc:
        return "", f"cannot read {path}: {exc}"


def utc_stamp() -> str:
    """``20261103T101500Z`` — the backup and stash timestamp."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


# ── The plan ───────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Plan:
    """Everything the dry run prints and ``--apply`` executes."""

    repo: pathlib.Path
    state: gate.HeadState
    entries: tuple[WorktreeEntry, ...]
    status_error: str
    local_main: str
    stamp: str
    backup_path: pathlib.Path
    stash_message: str
    reconciliation: Reconciliation
    refusals: tuple[str, ...]
    queue_timeout: int | None
    fetched: bool
    #: The live config's mode, captured before the switch so the installed file
    #: keeps it (git only stores 0644/0755, so a checkout would reset it).
    config_mode: int | None = None

    @property
    def on_main(self) -> bool:
        return self.state.branch == "main"

    @property
    def at_origin_main(self) -> bool:
        return bool(self.state.head) and self.state.head == self.state.origin_main

    @property
    def fast_forward(self) -> bool:
        return self.state.ancestor_of_main is True

    @property
    def ready(self) -> bool:
        return not self.refusals and self.reconciliation.ok


def preflight_refusals(
    state: gate.HeadState,
    local_main: str,
    status_error: str,
) -> list[str]:
    """Every condition that needs a human decision instead of a script.

    Being *behind* ``main`` is the normal case here — that is why the tool
    exists — so it is not a refusal.  Carrying commits ``main`` lacks, a dirty
    ``app/`` or ``scripts/``, an unprovable head and a local ``main`` that
    diverged from ``origin/main`` all are.
    """
    refusals: list[str] = []
    if not state.is_repo:
        return [f"{state.repo} is not a git checkout — nothing to prepare"]
    if not state.head:
        return [f"HEAD cannot be resolved in {state.repo} — nothing to prepare"]
    if state.detached:
        refusals.append(
            "detached HEAD — this checkout is not tracking a branch, so 'the checkout "
            "tracks main' cannot be established; check out a branch first (human decision)"
        )
    if not state.origin_main_known:
        refusals.append(
            "origin/main is unknown — fetch it first (or pass --fetch); without it the "
            "fast-forward cannot be proven"
        )
    elif state.ahead_of_main > 0:
        refusals.append(
            f"the tree carries {state.ahead_of_main} commit(s) main does not have — "
            "unreviewed work would be deployed; a human decides what happens to it"
        )
    elif state.ancestor_of_main is not True:
        refusals.append(
            "the switch to main is not a fast-forward — the histories diverged; "
            "a human decides how to reconcile them"
        )
    if local_main and state.origin_main_known and local_main != state.origin_main:
        rc, _out, _err = gate._git(
            ("merge-base", "--is-ancestor", "refs/heads/main", "origin/main"), repo=state.repo
        )
        if rc == 1:
            refusals.append(
                "the local main branch carries commits origin/main does not have — "
                "`git pull --ff-only` would refuse; a human decides what happens to them"
            )
    if status_error:
        refusals.append(f"git status failed ({status_error}) — the tree is not proven clean")
    if state.dirty_code:
        listed = ", ".join(state.dirty_code)
        refusals.append(
            f"app/ or scripts/ is dirty ({listed}) — those files are the deployment, so a "
            "human decides what happens to them"
        )
    return refusals


def build_plan(args: argparse.Namespace) -> Plan:
    """Read the tree and build the plan.  Never writes."""
    repo = pathlib.Path(args.repo).resolve()
    stamp = utc_stamp()
    state = gate.read_head_state(repo=repo, fetch=args.fetch)
    entries, status_error = read_worktree(repo)
    local_main = read_local_main(repo)
    main_text, main_error = read_main_config(repo)
    local_text, local_error = read_local_config(repo)

    if main_error or local_error:
        reconciliation = Reconciliation(
            "", (), (), (main_error or local_error,), main_text, local_text
        )
    else:
        reconciliation = reconcile(main_text, local_text, queue_timeout=args.queue_timeout)

    live_config = repo / CONFIG_REL
    config_mode = stat.S_IMODE(live_config.stat().st_mode) if live_config.exists() else None

    return Plan(
        repo=repo,
        state=state,
        entries=entries,
        status_error=status_error,
        local_main=local_main,
        stamp=stamp,
        backup_path=repo / f"{CONFIG_REL}.{stamp}{BACKUP_SUFFIX}",
        stash_message=f"{STASH_PREFIX} {stamp}",
        reconciliation=reconciliation,
        refusals=tuple(preflight_refusals(state, local_main, status_error)),
        queue_timeout=args.queue_timeout,
        fetched=args.fetch,
        config_mode=config_mode,
    )


# ── Rendering ──────────────────────────────────────────────────────────


def _short(sha: str) -> str:
    return sha[:12] if sha else "unknown"


def _diff(old: str, new: str, old_label: str, new_label: str) -> list[str]:
    return list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=old_label, tofile=new_label, lineterm=""
        )
    )


def _adopted_blocks(rec: Reconciliation) -> list[str]:
    """Top-level blocks ``main`` has and the live file does not (adopted verbatim)."""
    try:
        main_doc = yaml.safe_load(rec.main_text) or {}
        local_doc = yaml.safe_load(rec.local_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(main_doc, dict) or not isinstance(local_doc, dict):
        return []
    return sorted(key for key in main_doc if key not in local_doc)


def render_plan(plan: Plan, *, apply: bool) -> str:
    """The whole plan, as the operator reads it."""
    state = plan.state
    rec = plan.reconciliation
    out: list[str] = []
    add = out.append

    add(f"Deployment preparation — {plan.repo}")
    if apply:
        add("Mode: APPLY — the steps below are executed, then the tool stops.")
    else:
        add("Mode: DRY RUN — nothing is written. Pass --apply to execute.")
    add("")

    # 1. preflight
    add("── 1/5 preflight")
    if not state.is_repo:
        add("  branch:        n/a (not a git checkout)")
    else:
        add(f"  branch:        {state.branch or 'detached HEAD'}")
    add(f"  HEAD:          {_short(state.head)}")
    origin_note = "remote-tracking ref as last fetched" if not plan.fetched else "fetched just now"
    add(f"  origin/main:   {_short(state.origin_main)}  ({origin_note})")
    if state.origin_main_known:
        add(f"  ahead of main: {state.ahead_of_main} commit(s) main does not have")
        add(f"  behind main:   {state.behind_main} commit(s)")
        if plan.fast_forward:
            add("  fast-forward:  yes — HEAD is an ancestor of origin/main, so the switch is a "
                "fast-forward, not a merge")
        else:
            add("  fast-forward:  NO — the histories diverged")
    else:
        add("  ahead/behind:  unknown (origin/main is not available)")
    if plan.local_main:
        note = "" if plan.local_main == state.origin_main else "  (behind origin/main — the pull fast-forwards it)"
        add(f"  local main:    {_short(plan.local_main)}{note}")
    head_status, head_line = gate.judge_deployment_head(state)
    add(f"  pre_restart_check.py would say: [{head_status}] {head_line}")
    if plan.refusals:
        add("  verdict: REFUSED — a human decision is needed:")
        for refusal in plan.refusals:
            add(f"    ✗ {refusal}")
    elif not rec.ok:
        add("  verdict: REFUSED — the config reconciliation failed (see 3/5)")
    else:
        add("  verdict: READY — the switch is a fast-forward and app/ + scripts/ are clean")
    add("")

    # 2. preserve
    add("── 2/5 preserve")
    tracked = [entry for entry in plan.entries if not entry.untracked]
    untracked = [entry for entry in plan.entries if entry.untracked]
    add(f"  dirty (tracked): {len(tracked)} file(s)")
    for entry in tracked:
        add(f"    {entry.code} {entry.path}")
    add(f"  untracked:       {len(untracked)} file(s)")
    for entry in untracked:
        add(f"    {entry.code} {entry.path}")
    if not plan.entries:
        add("  (the tree is clean — nothing to stash)")
    add(f"  backup:          {plan.backup_path.relative_to(plan.repo)} "
        f"(mode preserved from {CONFIG_REL})")
    add(f"  stash:           git stash push -u -m \"{plan.stash_message}\" -- <the files above>")
    add("                   (explicit pathspecs, so this tool's own backup and proposal are")
    add("                   never stashed; the stash ref is printed after the push)")
    add("")

    # 3. reconciliation
    add("── 3/5 config reconciliation (proposal)")
    add(f"  base:     origin/main:{CONFIG_REL} ({_short(state.origin_main)})")
    add(f"  local:    {CONFIG_REL} (working tree)")
    add("  approach: text-level surgical edits — main's text is the base and only the declared")
    add("            keys are rewritten, so every comment and formatting choice survives verbatim")
    add("            (ruamel.yaml is not installed in this venv; a dump would destroy the comments)")
    for delta in rec.deltas:
        if delta.action == "same":
            add(f"  same        {delta.key}: {delta.chosen} in both files")
        elif delta.action == "take-main":
            add(f"  take-main   {delta.key}: {delta.chosen} — {delta.reason}")
        else:
            add(f"  {delta.action:<15} {delta.key}: {delta.chosen} (main: {delta.main}) — "
                f"{delta.reason}")
    for item in rec.preserved:
        add(f"  comment     {item.key} ({item.where}): {len(item.lines)} local line(s) main "
            f"dropped, preserved — {item.reason}")
    if plan.queue_timeout is not None and plan.queue_timeout <= 0:
        add("  warning     --queue-timeout <= 0 disables the queue timeout entirely "
            "(unbounded wait)")
    adopted = _adopted_blocks(rec)
    if adopted:
        add(f"  from main   {', '.join(adopted)} (adopted verbatim, no value invented)")
    if rec.problems:
        add("  VERIFICATION FAILED:")
        for problem in rec.problems:
            add(f"    ✗ {problem}")
    else:
        add("  verified:  the merged document equals main's document plus the declared deltas,")
        add("             and a line diff against main contains only those edits and the")
        add("             preserved comment lines")
    add("")

    # 4. the merged file
    add("── 4/5 the merged file")
    if apply:
        add(f"  path: {RECONCILED_REL} (written, then installed as {CONFIG_REL})")
    else:
        add(f"  path: {RECONCILED_REL} (not written in a dry run; --write-reconciled writes it)")
    add("")
    add(f"  diff vs origin/main:{CONFIG_REL} — what the local delta adds back:")
    for line in _diff(rec.main_text, rec.merged_text,
                      f"origin/main:{CONFIG_REL}", RECONCILED_REL):
        add(f"  {line}")
    add("")
    add(f"  diff vs {CONFIG_REL} (live) — what main adds:")
    for line in _diff(rec.local_text, rec.merged_text,
                      f"{CONFIG_REL} (live)", RECONCILED_REL):
        add(f"  {line}")
    add("")

    # 5. next steps
    add("── 5/5 next steps")
    if apply:
        add("  This tool stops here. It never restarts the service, never pushes and never merges.")
        add("  Run, in this order:")
    else:
        add("  Nothing has been written. To execute:")
        add("    ./venv/bin/python scripts/prepare_deployment.py --apply")
        add("  Then, in this order (this tool never runs them):")
    add("    1. ./venv/bin/python scripts/pre_restart_check.py")
    add("    2. sudo systemctl restart llama-guardian")
    add("    3. ./venv/bin/python scripts/verify_post_restart.py")
    return "\n".join(out)


# ── Applying ───────────────────────────────────────────────────────────


def run_git(repo: pathlib.Path, *args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run a git command, refusing the ones the operator owns."""
    subcommand = args[0] if args else ""
    if subcommand in FORBIDDEN_GIT:
        raise RuntimeError(
            f"prepare_deployment never runs `git {subcommand}` — restart, push and merge "
            "are the operator's call"
        )
    return gate._git(args, repo=repo, timeout=timeout)


def backup_config(repo: pathlib.Path, stamp: str) -> pathlib.Path:
    """Copy the live config next to itself, preserving its mode."""
    source = repo / CONFIG_REL
    target = source.with_name(source.name + f".{stamp}{BACKUP_SUFFIX}")
    shutil.copy2(source, target)
    source_mode = stat.S_IMODE(source.stat().st_mode)
    if stat.S_IMODE(target.stat().st_mode) != source_mode:
        os.chmod(target, source_mode)
    return target


def install_config(repo: pathlib.Path, text: str, mode: int | None = None) -> pathlib.Path:
    """Write *text* to the live config atomically, preserving its mode.

    *mode* is the mode the live file had before the switch: a checkout restores
    git's 0644, so reading the mode here would silently widen a 0600 config.
    """
    target = repo / CONFIG_REL
    if mode is None and target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text)
    if mode is not None:
        # Only ever the mode the live file already had; a new file keeps the
        # process umask rather than a permissive literal (CodeQL py/overly-permissive-file).
        os.chmod(temporary, mode)
    os.replace(temporary, target)
    return target


def write_reconciled(repo: pathlib.Path, text: str) -> pathlib.Path:
    """Write the proposal next to the live config, for review."""
    target = repo / RECONCILED_REL
    target.write_text(text)
    return target


def apply_plan(plan: Plan, *, out=print) -> int:
    """Execute the plan: backup, stash, switch, install, print.  Never restarts."""
    repo = plan.repo
    rec = plan.reconciliation

    # (0) Prove the target before anything is written.  The plan was built from
    # the remote-tracking ref, which may be stale: `git pull` talks to the remote
    # and can land on a commit the reconciled config was never built against,
    # silently dropping settings merged in the meantime.  Refresh the ref and
    # refuse when it moved — nothing has been written at this point.
    rc, _stdout, err = run_git(
        repo, "fetch", "--quiet", "--no-tags", "origin",
        "+refs/heads/main:refs/remotes/origin/main", timeout=120.0,
    )
    if rc != 0:
        out(f"  ABORT — cannot refresh origin/main: {err}")
        out("  the target cannot be proven, so nothing was written")
        return EXIT_ERROR
    rc, target, _err = run_git(repo, "rev-parse", "origin/main")
    if rc != 0 or not target:
        out("  ABORT — origin/main cannot be resolved after the fetch; nothing was written")
        return EXIT_ERROR
    if target != plan.state.origin_main:
        out(f"  ABORT — origin/main moved from {_short(plan.state.origin_main)} to "
            f"{_short(target)} since the plan was built")
        out("  the reconciled config describes the older commit; re-run the tool to plan "
            "against the new one")
        out("  nothing was written")
        return EXIT_ERROR
    out(f"  target:  origin/main {_short(target)} (refreshed, unchanged since the plan)")

    # (a) The backup comes first: it must exist before anything writes.
    try:
        backup = backup_config(repo, plan.stamp)
    except OSError as exc:
        out(f"  ABORT — could not back up {CONFIG_REL}: {exc}")
        out("  nothing was stashed, checked out or written")
        return EXIT_ERROR
    out(f"  backup: {backup.relative_to(repo)}")

    # (b) Stash the local state, so the switch cannot lose it.  A checkout or a
    # pull can overwrite a dirty file; when neither happens there is nothing to
    # protect, so the stash is skipped and the tree stays as the operator had it.
    stash_sha = ""
    # This tool's own artifacts (the backup and the proposal) are never stashed:
    # a bare `git stash push -u` would take the one file the operator needs for
    # recovery out of the working tree.
    stashable = [
        entry for entry in plan.entries if not entry.path.startswith(f"{CONFIG_REL}.")
    ]
    needs_stash = bool(stashable) and (not plan.on_main or not plan.at_origin_main)
    if needs_stash:
        # Explicit pathspecs, not a bare `-u`, so exactly the listed files move.
        pathspecs = [f":(literal){entry.path}" for entry in stashable]
        rc, _stdout, err = run_git(
            repo, "stash", "push", "-u", "-m", plan.stash_message, "--", *pathspecs
        )
        if rc != 0:
            out(f"  ABORT — git stash failed: {err}")
            out(f"  the backup is at {backup.relative_to(repo)}; nothing was checked out")
            return EXIT_ERROR
        rc, sha, _err = run_git(repo, "rev-parse", "stash@{0}")
        stash_sha = sha if rc == 0 else ""
        out(f"  stashed: {_short(stash_sha)} — recover with `git stash pop`")
    elif plan.entries and not stashable:
        out("  stash: nothing to stash (only this tool's own backup/proposal files are "
            "untracked)")
    elif plan.entries:
        out("  stash: not needed — already on main at origin/main, so no checkout or pull "
            "can overwrite the local files")
    else:
        out("  stash: nothing to stash (the tree is clean)")

    def abort(step: str, detail: str) -> int:
        out(f"  ABORT — {step} failed: {detail}")
        if stash_sha:
            out(f"  your local state is in stash@{{0}} ({_short(stash_sha)}) — `git stash pop`")
        out(f"  the config backup is {backup.relative_to(repo)}")
        out(f"  the tree is on {gate._git(('rev-parse', '--abbrev-ref', 'HEAD'), repo=repo)[1]}")
        return EXIT_ERROR

    # (c) Switch to the merge target and fast-forward it.
    if plan.on_main:
        out("  already on main — the branch switch is skipped")
    else:
        rc, _stdout, err = run_git(repo, "checkout", "main")
        if rc != 0:
            return abort("git checkout main", err)
        out("  switched: checkout main")
    # Always pull: skipping it when the *tracking ref* already matches would miss
    # a remote that moved on, and the reconciled config would then describe a
    # commit that is no longer the target.
    rc, _stdout, err = run_git(repo, "pull", "--ff-only", "origin", "main", timeout=120.0)
    if rc != 0:
        return abort("git pull --ff-only origin main", err)
    rc, head, _err = run_git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return abort("git rev-parse HEAD", "HEAD cannot be resolved")
    if head != target:
        out(f"  ABORT — the target moved again during the apply: HEAD is {_short(head)}, "
            f"the plan was built against {_short(target)}")
        out("  the reconciled config was NOT installed; re-run the tool to plan against the "
            "new commit")
        if stash_sha:
            out(f"  your local state is in stash@{{0}} ({_short(stash_sha)}) — `git stash pop`")
        out(f"  the config backup is {backup.relative_to(repo)}")
        return EXIT_ERROR
    out(f"  pulled:   fast-forward to {_short(head)}")

    # (d) Install the reconciled config.
    try:
        reconciled = write_reconciled(repo, rec.merged_text)
    except OSError as exc:
        out(f"  ABORT — could not write the proposal: {exc}")
        out("  the config was NOT installed")
        if stash_sha:
            out(f"  your local state is in stash@{{0}} ({_short(stash_sha)}) — `git stash pop`")
        out(f"  the config backup is {backup.relative_to(repo)}")
        return EXIT_ERROR
    out(f"  proposal: {reconciled.relative_to(repo)}")
    live = repo / CONFIG_REL
    if live.exists() and live.read_text() == rec.merged_text:
        # The content is already right, but a checkout restores git's 0644: put the
        # operator's mode back rather than silently widening a 0600 config.
        if plan.config_mode is not None and stat.S_IMODE(live.stat().st_mode) != plan.config_mode:
            os.chmod(live, plan.config_mode)
            out(f"  config:   {CONFIG_REL} already matches the reconciled file — mode restored "
                f"to {oct(plan.config_mode)}")
        else:
            out(f"  config:   {CONFIG_REL} already matches the reconciled file — nothing to install")
    else:
        install_config(repo, rec.merged_text, plan.config_mode)
        out(f"  config:   {CONFIG_REL} installed from the reconciled file")
    out("")
    out("  This tool stops here. It never restarts the service, never pushes and never merges.")
    out("  Run, in this order:")
    out("    1. ./venv/bin/python scripts/pre_restart_check.py")
    out("    2. sudo systemctl restart llama-guardian")
    out("    3. ./venv/bin/python scripts/verify_post_restart.py")
    return EXIT_OK


# ── CLI ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prepare_deployment.py",
        description=(
            "Prepare the production checkout for a deployment: prove the switch to main is a "
            "fast-forward, reconcile config/global.settings.yaml, and (with --apply) execute it. "
            "The default is a dry run that writes nothing."
        ),
    )
    parser.add_argument(
        "--repo",
        default=str(_SCRIPTS_DIR.parent),
        help="the checkout to prepare (default: the repository this script lives in)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute the plan: backup, stash, checkout main, pull --ff-only, install the config",
    )
    parser.add_argument(
        "--write-reconciled",
        action="store_true",
        help=f"write the proposal to {RECONCILED_REL} without touching git",
    )
    parser.add_argument(
        "--queue-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "queue.queue_timeout_seconds for the merged config (default: keep the live value; "
            "1800 is main's shipped default, 300 makes a queued request give up after 5 min)"
        ),
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help=(
            "refresh origin/main first (writes .git/refs/remotes — off by default so a dry run "
            "against a live checkout stays read-only)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = build_plan(args)
    print(render_plan(plan, apply=args.apply))

    if not plan.ready:
        print("")
        print("❌ REFUSED — nothing was written. Resolve the items above first.")
        return EXIT_REFUSED

    if args.write_reconciled and not args.apply:
        try:
            target = write_reconciled(plan.repo, plan.reconciliation.merged_text)
        except OSError as exc:
            print("")
            print(f"❌ could not write the proposal: {exc}")
            return EXIT_ERROR
        print("")
        print(f"✅ proposal written to {target} — review it, then re-run with --apply")

    if not args.apply:
        return EXIT_OK

    print("")
    print("── applying")
    return apply_plan(plan)


if __name__ == "__main__":
    raise SystemExit(main())
