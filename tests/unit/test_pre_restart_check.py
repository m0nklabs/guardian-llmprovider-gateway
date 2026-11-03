"""Unit tests for the deployment-head guard in ``scripts/pre_restart_check.py``.

Issue #85: the unit runs the operator's checkout, so ``systemctl restart``
deploys whatever ``HEAD`` that checkout is on.  The gate must therefore prove
the *tree* before it proves the code, and it must never turn "I could not tell"
into a pass.

Every test builds a real throwaway git repository in ``tmp_path`` — a working
clone plus a bare ``origin`` — instead of mocking git.  The logic under test is
git-shaped (ancestry, ahead/behind counts, porcelain status), so a mock would
only test the mock.  Nothing here touches the network: the "offline" case points
``origin`` at a path that does not exist, and every other case uses a local
file remote.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_SPEC = importlib.util.spec_from_file_location(
    "pre_restart_check", REPO / "scripts" / "pre_restart_check.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
# The module uses `from __future__ import annotations` plus dataclasses, which
# resolves annotations through `sys.modules[cls.__module__]`; register it first.
sys.modules.setdefault("pre_restart_check", gate)
_SPEC.loader.exec_module(gate)


# ── Throwaway repositories ─────────────────────────────────────────────

#: A hermetic git environment: the host's global/system config (commit signing,
#: default branch, hooks) must not leak into the fixtures.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Guard Test",
    "GIT_AUTHOR_EMAIL": "guard@example.invalid",
    "GIT_COMMITTER_NAME": "Guard Test",
    "GIT_COMMITTER_EMAIL": "guard@example.invalid",
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


def make_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A working clone on ``main``, pushed to a bare ``origin``.

    The tree carries the two paths the guard classifies differently: ``app/``
    (code, a dirty copy is a FAIL) and ``config/global.settings.yaml`` (runtime
    input, a dirty copy is a WARN).
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "init", "-b", "main", str(work))
    (work / "app").mkdir()
    (work / "app" / "main.py").write_text("VALUE = 1\n")
    (work / "config").mkdir()
    (work / "config" / "global.settings.yaml").write_text("queue:\n  max_concurrent: 1\n")
    commit(work, "initial")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-u", "origin", "main")
    return work, origin


def advance_origin(tmp_path: pathlib.Path, origin: pathlib.Path, count: int = 3) -> str:
    """Push *count* new commits to ``origin/main`` from a second clone."""
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    for index in range(count):
        (other / "app" / f"merged_{index}.py").write_text(f"MERGED = {index}\n")
        commit(other, f"merged {index}")
    git(other, "push", "origin", "main")
    return git(other, "rev-parse", "HEAD")


# ── The head verdict ───────────────────────────────────────────────────


def test_feature_branch_with_commits_absent_from_main_fails(tmp_path):
    """The #85 shape: a branch carrying work ``main`` does not have."""
    work, _origin = make_repo(tmp_path)
    git(work, "switch", "-c", "feat/unified-provider-model-phase-a")
    for index in range(2):
        (work / "app" / f"feature_{index}.py").write_text(f"FEATURE = {index}\n")
        commit(work, f"feature {index}")
    head = git(work, "rev-parse", "HEAD")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state)

    assert status == gate.FAIL
    assert "feat/unified-provider-model-phase-a" in line  # the branch is named
    assert head[:12] in line  # HEAD is named
    assert state.origin_main[:12] in line  # origin/main is named
    assert "2 commits not in main" in line  # the count is named
    assert "2 commit(s) that main does not have" in line


def test_tree_behind_main_fails_even_though_it_is_an_ancestor(tmp_path):
    """The live #85 case: ``b30e027`` *is* an ancestor of ``bded1c8``.

    ``git merge-base --is-ancestor b30e027 bded1c8`` is true (0 commits ahead,
    29 behind), so an ancestor-only rule would call the production checkout a
    PASS while a restart deployed old code.  Being behind main is a FAIL.
    """
    work, origin = make_repo(tmp_path)
    advance_origin(tmp_path, origin, count=3)

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state)

    assert state.ancestor_of_main is True  # the trap: ancestry alone says "fine"
    assert state.ahead_of_main == 0
    assert state.behind_main == 3
    assert status == gate.FAIL
    assert "0 commits not in main" in line
    assert "3 commit(s) behind origin/main" in line
    assert "would deploy old code" in line


def test_diverged_local_main_fails(tmp_path):
    """Local main with unpushed commits *and* missing merged ones is a FAIL.

    A WARN here would let a restart deploy a main nobody else has while silently
    dropping the merged changes (PR-Piet on #85).
    """
    work, origin = make_repo(tmp_path)
    (work / "app" / "unpushed.py").write_text("UNPUSHED = 1\n")
    commit(work, "unpushed local work")
    advance_origin(tmp_path, origin, count=2)

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state)

    assert state.branch == "main"
    assert state.ahead_of_main == 1
    assert state.behind_main == 2
    assert state.ancestor_of_main is False
    assert status == gate.FAIL
    assert "diverged from origin/main" in line
    assert "1 unpushed, 2 missing" in line


def test_tree_on_main_at_origin_main_passes(tmp_path):
    work, _origin = make_repo(tmp_path)

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state)

    assert status == gate.PASS
    assert "on main @" in line
    assert "0 commits not in main" in line
    assert state.head == state.origin_main


def test_expect_head_matching_passes(tmp_path):
    work, _origin = make_repo(tmp_path)
    head = git(work, "rev-parse", "HEAD")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state, head[:8])

    assert status == gate.PASS
    assert f"--expect-head {head[:8]} matches HEAD" in line


def test_expect_head_mismatching_fails(tmp_path):
    work, _origin = make_repo(tmp_path)

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state, "deadbeef")

    assert status == gate.FAIL
    assert "--expect-head deadbeef does not match HEAD" in line
    assert state.head in line


def test_expect_head_rejects_an_empty_or_non_hex_pin(tmp_path):
    """``"".startswith("")`` is true, so an empty pin would pass every tree."""
    work, _origin = make_repo(tmp_path)
    state = gate.read_head_state(repo=work)

    for pin in ("", "   ", "zzzz", "abc"):
        status, line = gate.judge_deployment_head(state, pin)
        assert status == gate.FAIL, pin
        assert "is not a commit sha" in line, pin


def test_expect_head_pins_a_detached_checkout(tmp_path):
    """A detached checkout is a SKIP by default, but a pin makes it provable."""
    work, _origin = make_repo(tmp_path)
    head = git(work, "rev-parse", "HEAD")
    git(work, "switch", "--detach", head)

    state = gate.read_head_state(repo=work)
    assert state.detached is True
    assert gate.judge_deployment_head(state)[0] == gate.SKIP
    assert gate.judge_deployment_head(state, head[:8])[0] == gate.PASS


def test_offline_origin_is_a_skip_never_a_pass(tmp_path):
    """No reachable ``origin/main`` ⇒ SKIP, even with a stale local ref."""
    work, _origin = make_repo(tmp_path)
    git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_head(state)

    assert state.fetch_error  # the fetch failure is recorded, not swallowed
    assert status == gate.SKIP
    assert status != gate.PASS
    assert "origin/main unknown" in line
    assert "not proven" in line


def test_a_non_git_tree_is_a_skip_never_a_pass(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    state = gate.read_head_state(repo=plain)
    status, line = gate.judge_deployment_head(state)

    assert state.is_repo is False
    assert status == gate.SKIP
    assert "not a git checkout" in line
    assert str(plain) in line  # names the tree it judged, not the module default


# ── The working-tree verdict ───────────────────────────────────────────


def test_dirty_app_fails(tmp_path):
    work, _origin = make_repo(tmp_path)
    (work / "app" / "main.py").write_text("VALUE = 2\n")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_tree(state)

    assert state.dirty_code == ("app/main.py",)
    assert status == gate.FAIL
    assert "app/main.py" in line
    assert "would be deployed" in line


def test_dirty_scripts_fails(tmp_path):
    """An untracked file under ``scripts/`` is deployed too, so it FAILs.

    ``git status --porcelain`` collapses a wholly untracked directory to
    ``scripts/``; the guard names what git reported rather than expanding the
    tree, which keeps the check cheap on a checkout with large ignored dirs.
    """
    work, _origin = make_repo(tmp_path)
    (work / "scripts").mkdir()
    (work / "scripts" / "new_tool.py").write_text("TOOL = 1\n")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_tree(state)

    assert status == gate.FAIL
    assert "scripts/" in line
    assert "would be deployed" in line


def test_dirty_config_is_a_warn_with_the_reason(tmp_path):
    """``config/global.settings.yaml`` is runtime input and a local delta here."""
    work, _origin = make_repo(tmp_path)
    (work / "config" / "global.settings.yaml").write_text("queue:\n  max_concurrent: 4\n")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_tree(state)

    assert state.dirty_code == ()
    assert state.dirty_config == ("config/global.settings.yaml",)
    assert status == gate.WARN
    assert "expected on this host" in line
    assert "does not block the restart" in line


def test_a_rename_out_of_app_fails(tmp_path):
    """``git mv app/x.py docs/x.py`` removes a deployed file.

    Classifying only the destination would report the tree clean (PR-Piet on #85).
    """
    work, _origin = make_repo(tmp_path)
    (work / "docs").mkdir()
    git(work, "mv", "app/main.py", "docs/main.py")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_tree(state)

    assert "app/main.py" in state.dirty_code
    assert status == gate.FAIL
    assert "app/main.py" in line


def test_an_unreadable_status_is_a_skip_never_a_pass(tmp_path):
    """A corrupt index must not be read as a clean tree (PR-Piet on #85)."""
    work, _origin = make_repo(tmp_path)
    (work / ".git" / "index").write_bytes(b"garbage-not-an-index")

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_tree(state)

    assert state.status_error
    assert status == gate.SKIP
    assert status != gate.PASS
    assert "git status failed" in line
    assert "not proven clean" in line


def test_clean_tree_passes(tmp_path):
    work, _origin = make_repo(tmp_path)

    state = gate.read_head_state(repo=work)
    status, line = gate.judge_deployment_tree(state)

    assert status == gate.PASS
    assert "no uncommitted changes" in line


# ── The gate step ──────────────────────────────────────────────────────


def test_gate_step_reports_the_worst_of_head_and_tree(tmp_path, capsys):
    """A dirty ``app/`` FAILs the step even when the head itself is fine."""
    work, _origin = make_repo(tmp_path)
    (work / "app" / "main.py").write_text("VALUE = 3\n")

    status = gate.check_deployment_head(repo=work)
    out = capsys.readouterr().out

    assert status == gate.FAIL
    assert "[PASS] deployment head — on main @" in out
    assert "[FAIL] deployment tree —" in out


def test_gate_step_prints_the_one_line_verdict(tmp_path, capsys):
    work, _origin = make_repo(tmp_path)

    status = gate.check_deployment_head(repo=work)
    out = capsys.readouterr().out

    assert status == gate.PASS
    assert "[PASS] deployment head — on main @" in out
    assert "origin/main" in out
    assert "0 commits not in main" in out


def test_worst_orders_fail_above_skip_above_warn():
    assert gate.worst(gate.PASS, gate.WARN) == gate.WARN
    assert gate.worst(gate.WARN, gate.SKIP) == gate.SKIP
    assert gate.worst(gate.SKIP, gate.FAIL) == gate.FAIL
    assert gate.worst() == gate.PASS


def test_main_blocks_on_a_failing_deployment_head(monkeypatch, capsys):
    """A FAIL in the new step must fail the gate, and only FAIL blocks."""
    monkeypatch.setattr(gate, "check_deployment_head", lambda expect_head=None: gate.FAIL)
    monkeypatch.setattr(gate, "check_compile", lambda: True)
    monkeypatch.setattr(gate, "check_pyflakes", lambda: True)
    monkeypatch.setattr(gate, "check_wrapper_signatures", lambda: True)
    monkeypatch.setattr(gate, "check_call_site_signatures", lambda: True)
    monkeypatch.setattr(gate, "check_pytest", lambda: True)

    assert gate.main([]) == 1
    assert "GATE FAILURES" in capsys.readouterr().out


def test_main_does_not_block_on_a_skip(monkeypatch, capsys):
    """An unproven head (offline, detached) is loud but never blocks a restart."""
    monkeypatch.setattr(gate, "check_deployment_head", lambda expect_head=None: gate.SKIP)
    monkeypatch.setattr(gate, "check_compile", lambda: True)
    monkeypatch.setattr(gate, "check_pyflakes", lambda: True)
    monkeypatch.setattr(gate, "check_wrapper_signatures", lambda: True)
    monkeypatch.setattr(gate, "check_call_site_signatures", lambda: True)
    monkeypatch.setattr(gate, "check_pytest", lambda: True)

    assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "deployment-head: SKIP" in out
    assert "ALL BLOCKING GATES PASSED" in out


def test_expect_head_reaches_the_gate_step(monkeypatch):
    """``--expect-head`` is parsed and handed to the check."""
    seen: list[str | None] = []

    def fake(expect_head=None):
        seen.append(expect_head)
        return gate.PASS

    monkeypatch.setattr(gate, "check_deployment_head", fake)
    monkeypatch.setattr(gate, "check_compile", lambda: True)
    monkeypatch.setattr(gate, "check_pyflakes", lambda: True)
    monkeypatch.setattr(gate, "check_wrapper_signatures", lambda: True)
    monkeypatch.setattr(gate, "check_call_site_signatures", lambda: True)
    monkeypatch.setattr(gate, "check_pytest", lambda: True)

    gate.main(["--expect-head", "bded1c8"])
    assert seen == ["bded1c8"]
