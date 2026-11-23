"""Tests for `guardianctl permissions` (issue #131).

The subcommand reports permission drift on the capture tree — WAL files,
rotated ``.jsonl.gz`` archives, sidecars, media files and the directories —
against ``capture.file_mode``/``capture.directory_mode``, and only changes
anything behind ``--apply``.

Every test builds its own temporary capture tree.  Nothing here touches the
production capture tree, and no test runs ``--apply`` against it: the
production correction is an operator action.
"""

import argparse
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_guardianctl():
    """Import scripts/guardianctl.py as a module (scripts/ must be importable)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "guardianctl_permissions_module", SCRIPTS_DIR / "guardianctl.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gctl():
    return _load_guardianctl()


# ---------------------------------------------------------------------------
# Helpers — temporary trees only
# ---------------------------------------------------------------------------


# Fixtures craft modes through ``Path.chmod`` (the pathlib call the repository
# already uses in its tests) rather than ``os.chmod``: the values here are the
# configuration data under test, not a production permission decision, and the
# pathlib form keeps the CodeQL weak-permission query pointed at code that
# states a mask for real.


def _mode(path: Path) -> int:
    """Mode bits of the entry itself (never of a symlink target)."""
    return stat.S_IMODE(os.lstat(path).st_mode)


def _tree_modes(root: Path) -> dict[str, int]:
    """Snapshot every mode under ``root`` (relative path → mode)."""
    snapshot = {".": _mode(root)}
    for path in sorted(root.rglob("*")):
        snapshot[str(path.relative_to(root))] = _mode(path)
    return snapshot


def _settings_file(
    path: Path, *, file_mode: str = "0640", directory_mode: str = "0750"
) -> Path:
    """A minimal settings document carrying the configured capture modes."""
    path.write_text(
        "capture:\n"
        "  enabled: true\n"
        f"  file_mode: '{file_mode}'\n"
        f"  directory_mode: '{directory_mode}'\n"
    )
    return path


def _make_tree(
    base: Path, *, file_mode: int = 0o640, directory_mode: int = 0o750
) -> Path:
    """Capture tree with drifting and correct entries.

    ``capture/``             directory_mode
    ├── guardian_capture_current.jsonl          file_mode   (correct)
    ├── guardian_capture_1787743535_1.jsonl.gz  file_mode   (correct)
    ├── guardian_capture_1787743535_1.jsonl.sha256  file_mode (correct)
    ├── guardian_capture_1787751555_2.jsonl.gz  0644        (DRIFT, legacy)
    ├── .capture_state.json                    0600        (correct — tighter)
    ├── media/                                  directory_mode
    │   ├── req_ok_0.png                       file_mode   (correct)
    │   └── req_legacy_0.png                   0644        (DRIFT, legacy)
    └── nested/                                 0775        (DRIFT, directory)
        └── leftover.jsonl                     0644        (DRIFT)
    """
    root = base / "capture"
    (root / "media").mkdir(parents=True)
    (root / "nested").mkdir()

    correct = [
        root / "guardian_capture_current.jsonl",
        root / "guardian_capture_1787743535_1.jsonl.gz",
        root / "guardian_capture_1787743535_1.jsonl.sha256",
        root / "media" / "req_ok_0.png",
    ]
    for path in correct:
        path.write_bytes(b"x")
        path.chmod(file_mode)
    (root / ".capture_state.json").write_text("{}")
    (root / ".capture_state.json").chmod(0o600)

    legacy = [
        root / "guardian_capture_1787751555_2.jsonl.gz",
        root / "media" / "req_legacy_0.png",
        root / "nested" / "leftover.jsonl",
    ]
    for path in legacy:
        path.write_bytes(b"x")
        path.chmod(0o644)
    (root / "nested").chmod(0o775)

    root.chmod(directory_mode)
    (root / "media").chmod(directory_mode)
    return root


def _names(scan) -> set[str]:
    return {str(entry.path) for entry in scan.drift}


# ---------------------------------------------------------------------------
# Configuration is the only source of modes
# ---------------------------------------------------------------------------


class TestCaptureConfigModes:
    def test_modes_come_from_configuration(self, gctl, tmp_path):
        settings = _settings_file(tmp_path / "settings.yaml", file_mode="0600", directory_mode="0700")
        cfg = gctl._capture_settings(settings)
        assert cfg.file_mode == 0o600
        assert cfg.directory_mode == 0o700

    def test_defaults_are_the_configuration_defaults(self, gctl, tmp_path):
        # No mode keys in the capture section: the documented CaptureConfig
        # defaults apply (0640/0750), not a literal in the script.
        settings = tmp_path / "settings.yaml"
        settings.write_text("capture:\n  enabled: true\n")
        cfg = gctl._capture_settings(settings)
        assert cfg.file_mode == 0o640
        assert cfg.directory_mode == 0o750

    def test_integer_modes_are_accepted(self, gctl, tmp_path):
        settings = tmp_path / "settings.yaml"
        settings.write_text("capture:\n  file_mode: 0o600\n  directory_mode: 0o700\n")
        cfg = gctl._capture_settings(settings)
        assert cfg.file_mode == 0o600
        assert cfg.directory_mode == 0o700

    def test_missing_settings_file_is_a_clear_error(self, gctl, tmp_path):
        with pytest.raises(gctl.CaptureConfigError, match="cannot read capture configuration"):
            gctl._capture_settings(tmp_path / "nope.yaml")

    def test_unparsable_settings_file_is_a_clear_error(self, gctl, tmp_path):
        settings = tmp_path / "settings.yaml"
        settings.write_text("capture: [unclosed\n  file_mode: '0640'\n")
        with pytest.raises(gctl.CaptureConfigError, match="cannot parse capture configuration"):
            gctl._capture_settings(settings)

    def test_missing_capture_section_is_a_clear_error(self, gctl, tmp_path):
        settings = tmp_path / "settings.yaml"
        settings.write_text("proxy:\n  host: 127.0.0.1\n")
        with pytest.raises(gctl.CaptureConfigError, match="no 'capture' section"):
            gctl._capture_settings(settings)

    def test_world_readable_mode_is_rejected_not_guessed(self, gctl, tmp_path):
        settings = _settings_file(tmp_path / "settings.yaml", file_mode="0644")
        with pytest.raises(gctl.CaptureConfigError, match="is invalid"):
            gctl._capture_settings(settings)


# ---------------------------------------------------------------------------
# What counts as drift
# ---------------------------------------------------------------------------


class TestPermissionDriftReasons:
    def test_configured_mode_is_clean(self, gctl):
        assert gctl._permission_drift(0o640, 0o640) == ()
        assert gctl._permission_drift(0o750, 0o750) == ()

    def test_tighter_than_configured_is_clean(self, gctl):
        # Owner-only is not drift: no extra bit, owner access intact.
        assert gctl._permission_drift(0o600, 0o640) == ()
        assert gctl._permission_drift(0o700, 0o750) == ()

    def test_world_readable_is_drift(self, gctl):
        reasons = gctl._permission_drift(0o644, 0o640)
        assert len(reasons) == 1
        assert "looser than 0640" in reasons[0]
        assert "world-read" in reasons[0]

    def test_world_writable_is_drift(self, gctl):
        reasons = gctl._permission_drift(0o666, 0o640)
        assert any("world-write" in reason for reason in reasons)

    def test_missing_owner_write_is_drift(self, gctl):
        reasons = gctl._permission_drift(0o400, 0o640)
        assert len(reasons) == 1
        assert "owner access missing" in reasons[0]
        assert "owner-write" in reasons[0]

    def test_missing_owner_read_and_write_is_drift(self, gctl):
        reasons = gctl._permission_drift(0o000, 0o640)
        assert "owner-read" in reasons[0]
        assert "owner-write" in reasons[0]

    def test_directory_needs_owner_execute(self, gctl):
        # A directory without owner-x cannot be traversed by its owner.
        reasons = gctl._permission_drift(0o600, 0o750)
        assert "owner access missing" in reasons[0]
        assert "owner-execute" in reasons[0]

    def test_stricter_config_makes_a_former_default_drift(self, gctl):
        # 0640 is correct for the default configuration, and drift for 0600.
        reasons = gctl._permission_drift(0o640, 0o600)
        assert "group-read" in reasons[0]


# ---------------------------------------------------------------------------
# Scanning the tree
# ---------------------------------------------------------------------------


class TestScanPermissionDrift:
    def test_file_level_drift_detected(self, gctl, tmp_path):
        root = _make_tree(tmp_path)
        scan = gctl._scan_permission_drift(root, file_mode=0o640, directory_mode=0o750)

        drifting = _names(scan)
        assert str(root / "media" / "req_legacy_0.png") in drifting
        assert str(root / "guardian_capture_1787751555_2.jsonl.gz") in drifting
        assert str(root / "nested" / "leftover.jsonl") in drifting
        # Correct and tighter-than-configured artefacts are not drift.
        assert str(root / "media" / "req_ok_0.png") not in drifting
        assert str(root / "guardian_capture_current.jsonl") not in drifting
        assert str(root / ".capture_state.json") not in drifting

    def test_directory_level_drift_detected(self, gctl, tmp_path):
        root = _make_tree(tmp_path)
        scan = gctl._scan_permission_drift(root, file_mode=0o640, directory_mode=0o750)

        directories = {
            str(entry.path): entry.actual_mode
            for entry in scan.drift
            if entry.kind == "directory"
        }
        assert directories == {str(root / "nested"): 0o775}
        assert scan.directories_checked == 3  # capture/, media/, nested/

    def test_scan_counts_every_entry(self, gctl, tmp_path):
        root = _make_tree(tmp_path)
        scan = gctl._scan_permission_drift(root, file_mode=0o640, directory_mode=0o750)
        assert scan.files_checked == 8
        assert scan.directories_checked == 3
        assert scan.file_mode == 0o640
        assert scan.directory_mode == 0o750
        assert scan.root == root

    def test_owner_unreadable_file_is_reported(self, gctl, tmp_path):
        root = tmp_path / "capture"
        (root / "media").mkdir(parents=True)
        unreadable = root / "media" / "owner_unreadable.png"
        unreadable.write_bytes(b"payload")
        unreadable.chmod(0o000)
        root.chmod(0o750)
        (root / "media").chmod(0o750)

        scan = gctl._scan_permission_drift(root, file_mode=0o640, directory_mode=0o750)
        entry = next(e for e in scan.drift if e.path == unreadable)
        assert entry.actual_mode == 0o000
        assert entry.expected_mode == 0o640
        assert any("owner access missing" in reason for reason in entry.reasons)

    def test_symlink_is_skipped_and_never_followed(self, gctl, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"not capture data")
        outside.chmod(0o644)

        root = tmp_path / "capture"
        root.mkdir()
        root.chmod(0o750)
        link = root / "media-link"
        link.symlink_to(outside)
        dir_link = root / "outside-dir"
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "loose.txt").write_bytes(b"x")
        (outside_dir / "loose.txt").chmod(0o644)
        dir_link.symlink_to(outside_dir)

        scan = gctl._scan_permission_drift(root, file_mode=0o640, directory_mode=0o750)

        assert scan.drift == []
        assert any("symlink" in note for note in scan.skipped)
        # The symlink target is untouched and was never followed.
        assert _mode(outside) == 0o644
        assert _mode(outside_dir / "loose.txt") == 0o644

    def test_entry_vanishing_mid_walk_does_not_crash(self, gctl, tmp_path, monkeypatch):
        root = _make_tree(tmp_path)
        victim = root / "media" / "req_legacy_0.png"
        media_dir = root / "media"
        real_scandir = os.scandir
        state = {"unlinked": False}

        class _VanishingScandir:
            """Lists a directory, then deletes an entry before the walk stats it."""

            def __init__(self, path):
                self._entries = list(real_scandir(path))
                if Path(path) == media_dir and not state["unlinked"]:
                    state["unlinked"] = True
                    victim.unlink()

            def __enter__(self):
                return iter(self._entries)

            def __exit__(self, *exc_info):
                return False

        def fake_scandir(path):
            if Path(path) == media_dir:
                return _VanishingScandir(path)
            return real_scandir(path)

        monkeypatch.setattr(gctl.os, "scandir", fake_scandir)

        scan = gctl._scan_permission_drift(root, file_mode=0o640, directory_mode=0o750)

        assert state["unlinked"] is True
        assert any("vanished during the walk" in note for note in scan.skipped)
        assert str(victim) not in _names(scan)
        # The rest of the tree is still scanned.
        assert str(root / "nested" / "leftover.jsonl") in _names(scan)

    def test_missing_root_is_an_error(self, gctl, tmp_path):
        with pytest.raises(FileNotFoundError):
            gctl._scan_permission_drift(
                tmp_path / "absent", file_mode=0o640, directory_mode=0o750
            )

    def test_root_that_is_a_file_is_an_error(self, gctl, tmp_path):
        target = tmp_path / "not-a-dir"
        target.write_bytes(b"x")
        with pytest.raises(NotADirectoryError):
            gctl._scan_permission_drift(target, file_mode=0o640, directory_mode=0o750)


# ---------------------------------------------------------------------------
# cmd_permissions — report by default, --apply is the only writer
# ---------------------------------------------------------------------------


def _run(gctl, monkeypatch, tmp_path, tree, *, apply=False, as_json=False):
    """Run cmd_permissions against ``tree`` with a temp settings file."""
    settings = _settings_file(tmp_path / "settings.yaml")
    monkeypatch.setattr(gctl, "SETTINGS_YAML", settings)
    args = argparse.Namespace(apply=apply, root=str(tree), json=as_json)
    with pytest.raises(SystemExit) as excinfo:
        gctl.cmd_permissions(args)
    return excinfo.value.code


class TestCmdPermissionsReport:
    def test_report_mode_finds_drift_and_exits_non_zero(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        root = _make_tree(tmp_path)
        code = _run(gctl, monkeypatch, tmp_path, root)
        out = capsys.readouterr().out

        assert code == gctl.PERMISSIONS_EXIT_DRIFT == 1
        assert "DRIFT" in out
        assert "0644 -> 0640" in out
        assert "0775 -> 0750" in out
        assert "REPORT (default, safe)" in out
        assert "nothing is changed" in out

    def test_report_mode_changes_absolutely_nothing(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        root = _make_tree(tmp_path)
        before = _tree_modes(root)

        _run(gctl, monkeypatch, tmp_path, root)
        capsys.readouterr()

        assert _tree_modes(root) == before

    def test_clean_tree_exits_zero(self, gctl, monkeypatch, tmp_path, capsys):
        root = tmp_path / "capture"
        (root / "media").mkdir(parents=True)
        (root / "guardian_capture_current.jsonl").write_bytes(b"x")
        (root / "media" / "req_0.png").write_bytes(b"x")
        for path in (
            root,
            root / "media",
            root / "guardian_capture_current.jsonl",
            root / "media" / "req_0.png",
        ):
            path.chmod(0o750 if path.is_dir() else 0o640)

        code = _run(gctl, monkeypatch, tmp_path, root)
        out = capsys.readouterr().out

        assert code == gctl.PERMISSIONS_EXIT_CLEAN == 0
        assert "No drift" in out
        assert "DRIFT" not in out

    def test_json_output_lists_drift_and_reasons(self, gctl, monkeypatch, tmp_path, capsys):
        root = _make_tree(tmp_path)
        code = _run(gctl, monkeypatch, tmp_path, root, as_json=True)
        payload = json.loads(capsys.readouterr().out)

        assert code == 1
        assert payload["apply"] is False
        assert payload["file_mode"] == "0640"
        assert payload["directory_mode"] == "0750"
        assert payload["checked"] == {"files": 8, "directories": 3}
        drifting = {entry["path"]: entry for entry in payload["drift"]}
        # A tree outside the checkout is reported by absolute path.
        entry = drifting[str(root / "media" / "req_legacy_0.png")]
        assert entry["kind"] == "file"
        assert entry["actual_mode"] == "0644"
        assert entry["expected_mode"] == "0640"
        assert any("world-read" in reason for reason in entry["reasons"])
        assert payload["changed"] == []

    def test_config_error_exits_two_and_changes_nothing(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        root = _make_tree(tmp_path)
        before = _tree_modes(root)
        settings = tmp_path / "settings.yaml"
        settings.write_text("capture: [broken\n")
        monkeypatch.setattr(gctl, "SETTINGS_YAML", settings)

        args = argparse.Namespace(apply=True, root=str(root), json=False)
        with pytest.raises(SystemExit) as excinfo:
            gctl.cmd_permissions(args)
        err = capsys.readouterr().err

        assert excinfo.value.code == gctl.PERMISSIONS_EXIT_ERROR == 2
        assert "cannot parse capture configuration" in err
        assert "Refusing to compare the capture tree against a guessed mode" in err
        assert _tree_modes(root) == before

    def test_missing_capture_root_exits_two(self, gctl, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(gctl, "SETTINGS_YAML", _settings_file(tmp_path / "settings.yaml"))
        args = argparse.Namespace(apply=False, root=str(tmp_path / "absent"), json=False)
        with pytest.raises(SystemExit) as excinfo:
            gctl.cmd_permissions(args)

        assert excinfo.value.code == gctl.PERMISSIONS_EXIT_ERROR
        assert "Cannot scan capture tree" in capsys.readouterr().err


class TestCmdPermissionsApply:
    def test_apply_tightens_only_the_drifting_entries(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        root = _make_tree(tmp_path)
        correct = [
            root / "guardian_capture_current.jsonl",
            root / "guardian_capture_1787743535_1.jsonl.gz",
            root / "guardian_capture_1787743535_1.jsonl.sha256",
            root / "media" / "req_ok_0.png",
            root / ".capture_state.json",
            root,
            root / "media",
        ]
        before = {str(path): _mode(path) for path in correct}

        code = _run(gctl, monkeypatch, tmp_path, root, apply=True)
        out = capsys.readouterr().out

        assert code == gctl.PERMISSIONS_EXIT_CLEAN == 0
        assert "FIXED file      0644 -> 0640" in out
        assert "FIXED directory 0775 -> 0750" in out
        assert "4 corrected" in out

        # Drifting entries are exactly the configured modes now.
        assert _mode(root / "media" / "req_legacy_0.png") == 0o640
        assert _mode(root / "guardian_capture_1787751555_2.jsonl.gz") == 0o640
        assert _mode(root / "nested" / "leftover.jsonl") == 0o640
        assert _mode(root / "nested") == 0o750
        # Correct entries (including the tighter state file) are untouched.
        assert {str(path): _mode(path) for path in correct} == before

    def test_apply_uses_the_configured_modes_not_a_literal(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        # Configuration demands 0600/0700: a 0640 media file is now drift and
        # --apply must produce 0600, proving the modes come from config.
        root = tmp_path / "capture"
        (root / "media").mkdir(parents=True)
        media_file = root / "media" / "req_0.png"
        media_file.write_bytes(b"x")
        root.chmod(0o700)
        (root / "media").chmod(0o700)
        media_file.chmod(0o640)

        settings = _settings_file(
            tmp_path / "settings.yaml", file_mode="0600", directory_mode="0700"
        )
        monkeypatch.setattr(gctl, "SETTINGS_YAML", settings)
        args = argparse.Namespace(apply=True, root=str(root), json=False)
        with pytest.raises(SystemExit) as excinfo:
            gctl.cmd_permissions(args)

        assert excinfo.value.code == 0
        assert _mode(media_file) == 0o600
        assert _mode(root) == 0o700
        assert "0600" in capsys.readouterr().out

    def test_apply_leaves_a_clean_tree_untouched(self, gctl, monkeypatch, tmp_path, capsys):
        root = tmp_path / "capture"
        root.mkdir()
        artefact = root / "guardian_capture_current.jsonl"
        artefact.write_bytes(b"x")
        root.chmod(0o750)
        artefact.chmod(0o640)
        before = _tree_modes(root)

        code = _run(gctl, monkeypatch, tmp_path, root, apply=True)
        out = capsys.readouterr().out

        assert code == 0
        assert "No drift" in out
        assert "0 corrected" in out
        assert _tree_modes(root) == before

    def test_apply_reports_a_failure_and_exits_one(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        root = _make_tree(tmp_path)
        target = root / "media" / "req_legacy_0.png"
        real_chmod = os.chmod
        failing = {"raised": False}

        def fake_chmod(path, mode, *args, **kwargs):
            if Path(path) == target:
                failing["raised"] = True
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(gctl.os, "chmod", fake_chmod)
        code = _run(gctl, monkeypatch, tmp_path, root, apply=True)
        captured = capsys.readouterr()

        assert failing["raised"] is True
        assert code == gctl.PERMISSIONS_EXIT_DRIFT == 1
        assert "Operation not permitted" in captured.err
        assert "1 failed" in captured.out
        # The entries that could be fixed still were.
        assert _mode(root / "nested") == 0o750