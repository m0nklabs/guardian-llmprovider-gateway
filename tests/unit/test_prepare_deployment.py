"""Tests for ``scripts/prepare_deployment.py`` (issue #85).

The tool turns the operator's manual "checkout main + reconcile config" step into
one reviewable command.  Two kinds of test live here:

* **Reconciliation** — pure text/YAML work, tested directly against fixture
  configs shaped like the real pair (``main``'s file with the new ``admission``,
  ``audit``, ``auth`` and ``destination_policy`` blocks, and the live file with
  the operator's local delta).
* **Git-shaped work** — preflight, backup, stash, switch, install.  Every one of
  those builds a **real throwaway repository** in ``tmp_path`` (a working clone
  plus a bare ``origin``) and runs the tool as a subprocess with a hermetic
  ``GIT_CONFIG_GLOBAL``.  Nothing is mocked: the logic under test is git-shaped
  (ancestry, ahead/behind, porcelain status, stash refs), so a mock would only
  test the mock.

Nothing here touches ``/home/flip/guardian-llmprovider-gateway`` (the live
checkout) and nothing writes outside ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "prepare_deployment.py"

_SPEC = importlib.util.spec_from_file_location("prepare_deployment", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
tool = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("prepare_deployment", tool)
_SPEC.loader.exec_module(tool)


# ── Fixture configs, shaped like the real pair ─────────────────────────

#: ``origin/main``'s config: the new blocks plus the shipped values.
MAIN_TEXT = """\
proxy:
  port: 11434
cloud_retry:
  enabled: false
  max_retries: 10
queue:
  max_concurrent: 1
  # Enforced wait budget for one request in the queue, in seconds.
  # 0 or a negative value disables the timeout.
  queue_timeout_seconds: 1800
  history_ttl: 300
# Admission bounds (G1-03).
admission:
  max_body_bytes: 33554432
  rate_limit:
    requests_per_minute: 300
# Metadata-only audit (G1-03 part 2).
audit:
  log_decisions: true
services:
  comfyui_url: http://127.0.0.1:8188
# Remote-ensure window for PASSIVE failover candidates (windows).
# Hot-reloadable; bounded to 30-600 s.
failover:
  remote_ensure_timeout_seconds: 120
# Privilege scopes (G1-01).
auth:
  scopes_enforced_after: "2026-11-11T00:00:00Z"
# Outbound destination policy (G1-02).
destination_policy:
  schemes: [http, https]
  allow:
    - host: 127.0.0.1
      ports: [11440]
"""

#: The live file: the operator's local delta, and none of the new blocks.
LOCAL_TEXT = """\
proxy:
  port: 11434
cloud_retry:
  # 2026-10-24: reverted back to disabled (transient attempt to own 429s at the
  # gateway). Decision: the gateway forwards upstream 429s as-is with the 429
  # marker intact, and pi (via dsh's pi-ai adapter) owns 429 backoff/retry in
  # ~/.pi/agent/settings.json retry — widened there instead of here.
  enabled: false
  max_retries: 10
queue:
  max_concurrent: 1
  queue_timeout_seconds: 300
  history_ttl: 300
services:
  comfyui_url: http://127.0.0.1:8188
# Remote-ensure window for PASSIVE failover candidates (windows).
# ai-kvm2 cold loads the 125B Flash-Next MoE in ~210 s (2026-10-21), so this
# must cover that too; bounded to 30-600 s.
# Hot-reloadable; bounded to 30-600 s.
failover:
  remote_ensure_timeout_seconds: 420
"""

CLOUD_RETRY_COMMENT = (
    "  # 2026-10-24: reverted back to disabled (transient attempt to own 429s at the",
    "  # gateway). Decision: the gateway forwards upstream 429s as-is with the 429",
    "  # marker intact, and pi (via dsh's pi-ai adapter) owns 429 backoff/retry in",
    "  # ~/.pi/agent/settings.json retry — widened there instead of here.",
)

FAILOVER_COMMENT = (
    "# ai-kvm2 cold loads the 125B Flash-Next MoE in ~210 s (2026-10-21), so this",
    "# must cover that too; bounded to 30-600 s.",
)


# ── Throwaway repositories ─────────────────────────────────────────────

#: A hermetic git environment: the host's global/system config (commit signing,
#: default branch, hooks) must not leak into the fixtures.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Deploy Test",
    "GIT_AUTHOR_EMAIL": "deploy@example.invalid",
    "GIT_COMMITTER_NAME": "Deploy Test",
    "GIT_COMMITTER_EMAIL": "deploy@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd: pathlib.Path, *args: str) -> str:
    """Run git in *cwd* and return stdout; fail the test on a non-zero exit."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_GIT_ENV, check=False
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def commit(work: pathlib.Path, message: str) -> str:
    """Commit everything in *work* and return the new sha."""
    git(work, "add", "-A")
    git(work, "commit", "-m", message)
    return git(work, "rev-parse", "HEAD")


def make_repo(
    tmp_path: pathlib.Path,
    *,
    main_text: str = MAIN_TEXT,
    local_text: str = LOCAL_TEXT,
    advance: int = 1,
) -> tuple[pathlib.Path, pathlib.Path, str]:
    """The live shape: a feature branch behind ``origin/main``, with a local delta.

    Returns ``(work, origin, base)`` where *base* is the commit the feature branch
    sits on.  ``origin/main`` is *advance* commits ahead of it, so the switch is a
    fast-forward — exactly the situation the tool exists for.
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    git(tmp_path, "init", "-b", "main", str(seed))
    (seed / "app").mkdir()
    (seed / "app" / "main.py").write_text("VALUE = 1\n")
    (seed / "config").mkdir()
    (seed / "config" / "global.settings.yaml").write_text(main_text)
    (seed / "AGENTS.md").write_text("# charter\n")
    commit(seed, "initial")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main")
    base = git(seed, "rev-parse", "HEAD")

    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "switch", "-c", "feat/unified-provider-model-phase-a")

    for index in range(advance):
        (seed / "app" / f"merged_{index}.py").write_text(f"MERGED = {index}\n")
        commit(seed, f"merged {index}")
    git(seed, "push", "origin", "main")
    git(work, "fetch", "origin")

    # The operator's local delta, uncommitted — exactly like the live checkout.
    (work / "config" / "global.settings.yaml").write_text(local_text)
    (work / "AGENTS.md").write_text("# charter (local edit)\n")
    (work / "docs").mkdir(exist_ok=True)
    (work / "docs" / "GUARDIAN_LOOP.md").write_text("# untracked\n")
    return work, origin, base


def push_to_origin(tmp_path: pathlib.Path, origin: pathlib.Path, *, count: int = 1) -> str:
    """Push *count* new commits to ``origin/main`` from a second clone."""
    other = tmp_path / "other"
    if not other.exists():
        git(tmp_path, "clone", str(origin), str(other))
    for index in range(count):
        (other / "app" / f"later_{index}.py").write_text(f"LATER = {index}\n")
        commit(other, f"later {index}")
    git(other, "push", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def run_tool(
    repo: pathlib.Path,
    *args: str,
    path_prefix: pathlib.Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the tool against *repo* as a subprocess, with a hermetic environment."""
    # HOME is inherited on purpose: on the CI runner PyYAML lives in the *user*
    # site-packages, so overriding HOME would hide it from the subprocess.  Git
    # hermeticity comes from GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM, not from HOME.
    env = dict(_GIT_ENV)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo.parent),
        check=False,
    )


def snapshot(repo: pathlib.Path) -> tuple[str, str, str]:
    """``(branch, HEAD, porcelain status)`` — to prove a dry run changed nothing."""
    return (
        git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        git(repo, "rev-parse", "HEAD"),
        git(repo, "status", "--porcelain", "-uall"),
    )


def backups(repo: pathlib.Path) -> list[pathlib.Path]:
    return sorted((repo / "config").glob("global.settings.yaml.*.bak"))


# ── Reconciliation: the keep-local rule ────────────────────────────────


def test_keep_local_timeout_rule_keeps_the_live_value():
    """420 must survive: main's 120 s would abort the 125B cold load."""
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)

    assert rec.ok, rec.problems
    delta = next(d for d in rec.deltas if d.key == "failover.remote_ensure_timeout_seconds")
    assert delta.local == 420
    assert delta.main == 120
    assert delta.chosen == 420
    assert delta.action == tool.KEEP_LOCAL
    assert "cold load" in delta.reason
    assert "  remote_ensure_timeout_seconds: 420" in rec.merged_text
    assert "  remote_ensure_timeout_seconds: 120" not in rec.merged_text


def test_keep_local_rule_reads_both_files_instead_of_hardcoding_420():
    """The rule is "keep local when they differ", not "write 420"."""
    local = LOCAL_TEXT.replace("remote_ensure_timeout_seconds: 420",
                               "remote_ensure_timeout_seconds: 300")
    rec = tool.reconcile(MAIN_TEXT, local)

    delta = next(d for d in rec.deltas if d.key == "failover.remote_ensure_timeout_seconds")
    assert delta.chosen == 300
    assert "  remote_ensure_timeout_seconds: 300" in rec.merged_text


def test_identical_values_are_reported_as_same_and_not_rewritten():
    local = LOCAL_TEXT.replace("remote_ensure_timeout_seconds: 420",
                               "remote_ensure_timeout_seconds: 120")
    rec = tool.reconcile(MAIN_TEXT, local)

    delta = next(d for d in rec.deltas if d.key == "failover.remote_ensure_timeout_seconds")
    assert delta.action == "same"
    assert delta.rewrites_main is False
    assert rec.merged_text.count("remote_ensure_timeout_seconds: 120") == 1


def test_a_key_missing_from_the_live_file_takes_mains_value():
    local = LOCAL_TEXT.replace("  remote_ensure_timeout_seconds: 420\n", "")
    rec = tool.reconcile(MAIN_TEXT, local)

    delta = next(d for d in rec.deltas if d.key == "failover.remote_ensure_timeout_seconds")
    assert delta.action == "take-main"
    assert delta.chosen == 120
    assert "  remote_ensure_timeout_seconds: 120" in rec.merged_text


def test_a_key_missing_from_main_is_a_problem_not_an_invention():
    main = MAIN_TEXT.replace("failover:\n  remote_ensure_timeout_seconds: 120\n", "")
    rec = tool.reconcile(main, LOCAL_TEXT)

    assert not rec.ok
    assert any("no such key" in problem for problem in rec.problems)


# ── Reconciliation: the queue-timeout choice ───────────────────────────


def test_queue_timeout_defaults_to_the_live_value():
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)

    delta = next(d for d in rec.deltas if d.key == "queue.queue_timeout_seconds")
    assert delta.local == 300
    assert delta.main == 1800
    assert delta.chosen == 300
    assert delta.action == tool.OPERATOR_CHOICE
    assert "5 min" in delta.reason and "1800" in delta.reason
    assert "  queue_timeout_seconds: 300" in rec.merged_text


def test_queue_timeout_flag_takes_mains_shipped_default():
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT, queue_timeout=1800)

    delta = next(d for d in rec.deltas if d.key == "queue.queue_timeout_seconds")
    assert delta.chosen == 1800
    assert "--queue-timeout 1800" in delta.reason
    assert "  queue_timeout_seconds: 1800" in rec.merged_text
    assert "  queue_timeout_seconds: 300" not in rec.merged_text


def test_queue_timeout_flag_accepts_any_operator_value():
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT, queue_timeout=600)

    assert "  queue_timeout_seconds: 600" in rec.merged_text
    assert rec.ok, rec.problems


# ── Reconciliation: the preserved comment blocks ───────────────────────


def test_preserved_comment_block_survives_the_merge():
    """The 2026-10-24 cloud_retry decision record must not be lost."""
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)

    merged_lines = rec.merged_text.splitlines()
    for line in CLOUD_RETRY_COMMENT:
        assert line in merged_lines, line
    # ... and it sits inside cloud_retry, directly above `enabled`.
    header = merged_lines.index("cloud_retry:")
    assert merged_lines[header + 1:header + 5] == list(CLOUD_RETRY_COMMENT)
    assert merged_lines[header + 5] == "  enabled: false"
    preserved = next(item for item in rec.preserved if item.key == "cloud_retry")
    assert preserved.where == "block"
    assert preserved.lines == CLOUD_RETRY_COMMENT


def test_preserved_leading_comment_keeps_the_operators_order():
    """The ai-kvm2 rationale goes back where the operator wrote it."""
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)

    merged_lines = rec.merged_text.splitlines()
    for line in FAILOVER_COMMENT:
        assert line in merged_lines, line
    first = merged_lines.index(FAILOVER_COMMENT[0])
    assert merged_lines[first + 2] == "# Hot-reloadable; bounded to 30-600 s."
    assert merged_lines[first + 3] == "failover:"
    preserved = next(item for item in rec.preserved if item.key == "failover")
    assert preserved.where == "leading"
    assert preserved.lines == FAILOVER_COMMENT


def test_a_comment_main_already_has_is_not_duplicated():
    """Reconciling main against itself must not insert anything."""
    rec = tool.reconcile(MAIN_TEXT, MAIN_TEXT)

    assert rec.preserved == ()
    assert rec.merged_text == MAIN_TEXT


def test_merge_comment_block_keeps_local_order():
    main_block = ["# a", "# c"]
    local_block = ["# a", "# b", "# c"]

    assert tool.merge_comment_block(main_block, local_block) == ["# a", "# b", "# c"]


def test_merge_comment_block_appends_when_there_is_no_anchor():
    assert tool.merge_comment_block([], ["# x", "# y"]) == ["# x", "# y"]


# ── Reconciliation: main's new blocks are adopted verbatim ─────────────


def test_main_blocks_are_adopted_verbatim():
    """No invented values: admission/audit/auth/destination_policy come from main."""
    import yaml

    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)
    main_doc = yaml.safe_load(MAIN_TEXT)
    merged_doc = yaml.safe_load(rec.merged_text)

    for block in ("admission", "audit", "auth", "destination_policy"):
        assert merged_doc[block] == main_doc[block], block


def test_the_merged_document_is_main_plus_exactly_the_declared_deltas():
    import yaml

    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)
    main_doc = yaml.safe_load(MAIN_TEXT)
    merged_doc = yaml.safe_load(rec.merged_text)

    assert merged_doc["failover"]["remote_ensure_timeout_seconds"] == 420
    assert merged_doc["queue"]["queue_timeout_seconds"] == 300
    main_doc["failover"]["remote_ensure_timeout_seconds"] = 420
    main_doc["queue"]["queue_timeout_seconds"] = 300
    assert merged_doc == main_doc


def test_the_merged_text_differs_from_main_only_in_the_declared_lines():
    import difflib

    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)
    changed = [
        line
        for line in difflib.unified_diff(
            MAIN_TEXT.splitlines(), rec.merged_text.splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]

    # 6 preserved comment lines (4 cloud_retry + 2 failover) and 2 value rewrites,
    # each rewrite showing as a removed and an added line.
    assert len(changed) == 10
    assert sum(1 for line in changed if line.startswith("+  #")) == 4
    assert sum(1 for line in changed if line.startswith("+# ")) == 2
    assert sum(1 for line in changed if line.startswith("-")) == 2


# ── Reconciliation: the verification actually verifies ─────────────────


def test_verify_merged_catches_an_undeclared_edit():
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)
    tampered = rec.merged_text.replace("  port: 11434", "  port: 11435")

    problems = tool.verify_merged(tampered, MAIN_TEXT, rec.deltas, rec.preserved)

    assert any("unexpected edit" in problem for problem in problems)


def test_verify_merged_catches_a_missing_comment():
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)
    tampered = rec.merged_text.replace(CLOUD_RETRY_COMMENT[0] + "\n", "")

    problems = tool.verify_merged(tampered, MAIN_TEXT, rec.deltas, rec.preserved)

    assert any("preserved comment line missing" in problem for problem in problems)


def test_verify_merged_catches_a_wrong_value():
    rec = tool.reconcile(MAIN_TEXT, LOCAL_TEXT)
    tampered = rec.merged_text.replace(
        "  remote_ensure_timeout_seconds: 420", "  remote_ensure_timeout_seconds: 121"
    )

    problems = tool.verify_merged(tampered, MAIN_TEXT, rec.deltas, rec.preserved)

    assert problems


def test_reconcile_reports_invalid_yaml_instead_of_raising():
    rec = tool.reconcile(MAIN_TEXT, "queue: [1\n")

    assert not rec.ok
    assert any("not valid YAML" in problem for problem in rec.problems)


def test_reconcile_reports_a_non_mapping_document():
    rec = tool.reconcile(MAIN_TEXT, "- just\n- a list\n")

    assert not rec.ok
    assert any("not a mapping" in problem for problem in rec.problems)


# ── The small YAML reader ──────────────────────────────────────────────


def test_path_map_ignores_sequence_items_and_flow_mappings():
    lines = [
        "destination_policy:",
        "  allow:",
        "    - host: 127.0.0.1",
        "      ports: [11440]",
        "    - {host: openrouter.ai, ports: [443]}",
        "  schemes: [http, https]",
    ]
    paths = tool.path_map(lines)

    assert paths[0] == ("destination_policy",)
    assert paths[1] == ("destination_policy", "allow")
    assert paths[3] == ("destination_policy", "allow", "ports")
    assert paths[5] == ("destination_policy", "schemes")
    assert 2 not in paths and 4 not in paths


def test_replace_value_line_keeps_a_trailing_comment():
    line = "  queue_timeout_seconds: 1800  # five minutes"

    assert tool.replace_value_line(line, 300) == "  queue_timeout_seconds: 300  # five minutes"


def test_format_scalar_matches_the_files_style():
    assert tool.format_scalar(True) == "true"
    assert tool.format_scalar(False) == "false"
    assert tool.format_scalar(420) == "420"
    assert tool.format_scalar("2026-11-11T00:00:00Z") == "2026-11-11T00:00:00Z"
    assert tool.format_scalar("a: b") == "'a: b'"


# ── Preflight: the dry run ─────────────────────────────────────────────


def test_dry_run_plans_a_fast_forward_and_writes_nothing(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    before = snapshot(work)

    proc = run_tool(work)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Mode: DRY RUN" in proc.stdout
    assert "fast-forward:  yes" in proc.stdout
    assert "verdict: READY" in proc.stdout
    assert "behind main:   1 commit(s)" in proc.stdout
    assert snapshot(work) == before  # no write of any kind
    assert backups(work) == []
    assert not (work / tool.RECONCILED_REL).exists()


def test_dry_run_names_every_dirty_and_untracked_file(tmp_path):
    work, _origin, _base = make_repo(tmp_path)

    proc = run_tool(work)

    assert "dirty (tracked): 2 file(s)" in proc.stdout
    assert " M AGENTS.md" in proc.stdout
    assert " M config/global.settings.yaml" in proc.stdout
    assert "untracked:       1 file(s)" in proc.stdout
    assert "?? docs/GUARDIAN_LOOP.md" in proc.stdout


def test_dry_run_names_the_backup_and_the_stash(tmp_path):
    work, _origin, _base = make_repo(tmp_path)

    proc = run_tool(work)

    assert re.search(
        r"backup:\s+config/global\.settings\.yaml\.\d{8}T\d{6}Z\.bak", proc.stdout
    ), proc.stdout
    assert 'git stash push -u -m "prepare_deployment: local state before main switch ' in proc.stdout


def test_dry_run_prints_the_plan_and_both_diffs(tmp_path):
    work, _origin, _base = make_repo(tmp_path)

    proc = run_tool(work)

    assert "── 1/5 preflight" in proc.stdout
    assert "── 3/5 config reconciliation (proposal)" in proc.stdout
    assert "keep-local" in proc.stdout and "operator-choice" in proc.stdout
    assert "diff vs origin/main:config/global.settings.yaml" in proc.stdout
    assert "diff vs config/global.settings.yaml (live)" in proc.stdout
    assert "+  remote_ensure_timeout_seconds: 420" in proc.stdout
    assert "from main   admission, audit, auth, destination_policy" in proc.stdout


def test_write_reconciled_writes_the_proposal_without_touching_git(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    before = snapshot(work)

    proc = run_tool(work, "--write-reconciled")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    proposal = work / tool.RECONCILED_REL
    assert proposal.exists()
    assert proposal.read_text() == tool.reconcile(MAIN_TEXT, LOCAL_TEXT).merged_text
    # The proposal is the only new path; no tracked file and no git state changed.
    after = snapshot(work)
    assert after[:2] == before[:2]
    assert after[2].replace(f"?? {tool.RECONCILED_REL}\n", "") == before[2]
    assert backups(work) == []


# ── Preflight: every refusal ───────────────────────────────────────────


def test_refuses_when_the_tree_carries_commits_main_lacks(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    (work / "app" / "unreviewed.py").write_text("UNREVIEWED = 1\n")
    commit(work, "unreviewed work")
    before = snapshot(work)

    proc = run_tool(work)

    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout
    assert "carries 1 commit(s) main does not have" in proc.stdout
    assert snapshot(work) == before


def test_refuses_when_app_is_dirty(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    (work / "app" / "main.py").write_text("VALUE = 2\n")

    proc = run_tool(work)

    assert proc.returncode == 1
    assert "app/ or scripts/ is dirty" in proc.stdout
    assert "app/main.py" in proc.stdout


def test_refuses_when_scripts_is_dirty(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    (work / "scripts").mkdir()
    (work / "scripts" / "new_tool.py").write_text("TOOL = 1\n")

    proc = run_tool(work)

    assert proc.returncode == 1
    assert "app/ or scripts/ is dirty" in proc.stdout
    assert "scripts/new_tool.py" in proc.stdout


def test_refuses_a_detached_head(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    git(work, "switch", "--detach", "HEAD")

    proc = run_tool(work)

    assert proc.returncode == 1
    assert "detached HEAD" in proc.stdout


def test_refuses_when_origin_main_is_unknown(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    git(work, "update-ref", "-d", "refs/remotes/origin/main")

    proc = run_tool(work)

    assert proc.returncode == 1
    assert "origin/main is unknown" in proc.stdout


def test_refuses_when_the_local_main_branch_diverged(tmp_path):
    """A local main with unpushed commits would make `pull --ff-only` refuse."""
    work, _origin, _base = make_repo(tmp_path)
    git(work, "switch", "-c", "scratch")
    (work / "app" / "local_main_work.py").write_text("LOCAL = 1\n")
    commit(work, "local main work")
    git(work, "branch", "-f", "main", "HEAD")
    git(work, "switch", "feat/unified-provider-model-phase-a")

    proc = run_tool(work)

    assert proc.returncode == 1
    assert "local main branch carries commits origin/main does not have" in proc.stdout


def test_refuses_a_non_git_tree(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    proc = run_tool(plain)

    assert proc.returncode == 1
    assert "is not a git checkout" in proc.stdout


def test_apply_refuses_when_preflight_failed(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    (work / "app" / "unreviewed.py").write_text("UNREVIEWED = 1\n")
    commit(work, "unreviewed work")
    before = snapshot(work)

    proc = run_tool(work, "--apply")

    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout
    assert backups(work) == []
    assert git(work, "stash", "list") == ""
    assert snapshot(work) == before


# ── Apply ──────────────────────────────────────────────────────────────


def test_apply_backs_up_stashes_switches_and_installs(tmp_path):
    work, origin, _base = make_repo(tmp_path)
    live = work / "config" / "global.settings.yaml"
    os.chmod(live, 0o600)
    original = live.read_text()

    proc = run_tool(work, "--apply")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # (a) the backup holds the operator's file, with its mode
    found = backups(work)
    assert len(found) == 1
    assert found[0].read_text() == original
    assert stat.S_IMODE(found[0].stat().st_mode) == 0o600
    # (b) the local state is in a stash, recoverable
    assert "prepare_deployment: local state before main switch" in git(work, "stash", "list")
    # (c) the tree is on main at origin/main
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(work, "rev-parse", "HEAD") == git(origin, "rev-parse", "refs/heads/main")
    # (d) the reconciled config is installed, and the proposal is there to review
    expected = tool.reconcile(MAIN_TEXT, LOCAL_TEXT).merged_text
    assert live.read_text() == expected
    assert (work / tool.RECONCILED_REL).read_text() == expected
    assert stat.S_IMODE(live.stat().st_mode) == 0o600
    # (e) the next commands are printed, and the tool stops
    assert "1. ./venv/bin/python scripts/pre_restart_check.py" in proc.stdout
    assert "2. sudo systemctl restart llama-guardian" in proc.stdout
    assert "3. ./venv/bin/python scripts/verify_post_restart.py" in proc.stdout
    assert "never restarts the service, never pushes and never merges" in proc.stdout


def test_apply_never_restarts_and_never_pushes(tmp_path):
    """A fake `systemctl` on PATH proves the tool never calls it."""
    work, origin, _base = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "systemctl-was-called"
    fake = bin_dir / "systemctl"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    fake.chmod(0o755)
    origin_before = git(origin, "for-each-ref", "--format=%(refname) %(objectname)")

    proc = run_tool(work, "--apply", path_prefix=bin_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not marker.exists(), "the tool must never restart the service"
    assert git(origin, "for-each-ref", "--format=%(refname) %(objectname)") == origin_before


def test_apply_is_a_no_op_when_already_on_main(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    git(work, "switch", "main")
    git(work, "merge", "--ff-only", "origin/main")
    (work / "config" / "global.settings.yaml").write_text(LOCAL_TEXT)

    proc = run_tool(work, "--apply")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "already on main — the branch switch is skipped" in proc.stdout
    assert "stash: not needed" in proc.stdout
    assert git(work, "stash", "list") == ""
    assert (work / "config" / "global.settings.yaml").read_text() == (
        tool.reconcile(MAIN_TEXT, LOCAL_TEXT).merged_text
    )


def test_apply_aborts_before_any_write_when_the_backup_fails(tmp_path):
    """The backup is step (a): if it cannot be made, nothing else happens."""
    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
        pytest.skip("running as root: a read-only directory is not enforced")
    work, _origin, _base = make_repo(tmp_path)
    config_dir = work / "config"
    before = snapshot(work)
    os.chmod(config_dir, 0o555)
    try:
        proc = run_tool(work, "--apply")
    finally:
        os.chmod(config_dir, 0o755)

    assert proc.returncode == 2
    assert "could not back up" in proc.stdout
    assert "nothing was stashed, checked out or written" in proc.stdout
    assert git(work, "stash", "list") == ""
    assert snapshot(work) == before


def test_apply_leaves_the_backup_and_the_stash_when_the_pull_fails(tmp_path):
    """A failure after the stash must still leave both recovery paths intact."""
    work, origin, _base = make_repo(tmp_path)
    original = (work / "config" / "global.settings.yaml").read_text()
    shutil.rmtree(origin)  # the remote-tracking ref survives; the remote does not

    proc = run_tool(work, "--apply")

    assert proc.returncode == 2
    assert "git pull --ff-only origin main failed" in proc.stdout
    assert "git stash pop" in proc.stdout
    found = backups(work)
    assert len(found) == 1
    assert found[0].read_text() == original
    assert "prepare_deployment: local state before main switch" in git(work, "stash", "list")
    # the checkout happened, so the live file is main's; the delta is in the stash
    assert (work / "config" / "global.settings.yaml").read_text() == MAIN_TEXT


def test_apply_skips_the_install_when_the_config_already_matches(tmp_path):
    work, _origin, _base = make_repo(tmp_path)
    git(work, "switch", "main")
    git(work, "merge", "--ff-only", "origin/main")
    (work / "config" / "global.settings.yaml").write_text(
        tool.reconcile(MAIN_TEXT, LOCAL_TEXT).merged_text
    )

    proc = run_tool(work, "--apply")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "already matches the reconciled file" in proc.stdout


def test_apply_honours_the_queue_timeout_flag(tmp_path):
    work, _origin, _base = make_repo(tmp_path)

    proc = run_tool(work, "--apply", "--queue-timeout", "1800")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    installed = (work / "config" / "global.settings.yaml").read_text()
    assert "  queue_timeout_seconds: 1800" in installed
    assert "  remote_ensure_timeout_seconds: 420" in installed


# ── The guard rails ────────────────────────────────────────────────────


def test_run_git_refuses_push_merge_and_friends(tmp_path):
    for subcommand in ("push", "merge", "rebase", "reset", "clean"):
        with pytest.raises(RuntimeError, match="never runs"):
            tool.run_git(tmp_path, subcommand)


def test_a_missing_pyyaml_is_a_clear_message_not_a_traceback(tmp_path):
    """`python -S` hides site-packages, which is what a bare python looks like."""
    work, _origin, _base = make_repo(tmp_path)

    proc = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--repo", str(work)],
        capture_output=True,
        text=True,
        env=dict(_GIT_ENV),
        cwd=str(work.parent),
        check=False,
    )

    assert proc.returncode == 2
    assert "needs PyYAML" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_every_git_call_goes_through_the_guard():
    """No direct subprocess use: every git call passes `run_git`'s refusal list."""
    source = SCRIPT.read_text()
    assert "import subprocess" not in source
    assert "os.system" not in source
    assert "systemctl restart" in source  # printed for the operator, never run


def test_the_dry_run_does_not_fetch_by_default(tmp_path):
    """A dry run against a live checkout must stay read-only: no refs written."""
    work, origin, _base = make_repo(tmp_path, advance=0)
    push_to_origin(tmp_path, origin, count=1)
    stale = git(work, "rev-parse", "origin/main")

    proc = run_tool(work)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "behind main:   0 commit(s)" in proc.stdout
    assert "remote-tracking ref as last fetched" in proc.stdout
    assert git(work, "rev-parse", "origin/main") == stale


def test_fetch_refreshes_origin_main_before_planning(tmp_path):
    work, origin, _base = make_repo(tmp_path, advance=0)
    push_to_origin(tmp_path, origin, count=1)

    proc = run_tool(work, "--fetch")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "behind main:   1 commit(s)" in proc.stdout
    assert "fetched just now" in proc.stdout
    assert git(work, "rev-parse", "origin/main") == git(origin, "rev-parse", "refs/heads/main")
