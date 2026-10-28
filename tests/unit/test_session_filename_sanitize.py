"""Tests for app.proxy.server._sanitize_session_filename — path-traversal guard
for /api/session/save and /api/session/load.

The sanitizer must reject anything that could escape the llama-server
--slot-save-path ($HOME/llama_slots) directory, and accept only safe basenames.
"""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.proxy.server import _sanitize_session_filename


class TestSanitizeSessionFilenameRejectsTraversal:
    @pytest.mark.parametrize(
        "raw",
        [
            "../../etc/passwd",       # prompt's required case
            "../../../etc/shadow",
            "..\\..\\windows\\evil",
            "/etc/passwd",
        ],
    )
    def test_traversal_and_path_inputs_reject_when_basename_invalid(self, raw):
        # These resolve to a basename with no .bin extension -> rejected.
        with pytest.raises(HTTPException) as exc:
            _sanitize_session_filename(raw)
        assert exc.value.status_code == 400

    def test_missing_extension_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _sanitize_session_filename("mysession")
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("raw", ["shell;rm.bin", "a b.bin", "a|b.bin", "a&b.bin"])
    def test_shell_metachars_and_whitespace_rejected(self, raw):
        with pytest.raises(HTTPException) as exc:
            _sanitize_session_filename(raw)
        assert exc.value.status_code == 400

    def test_empty_and_non_string_rejected(self):
        for raw in ["", None, 123, "   "]:
            with pytest.raises(HTTPException) as exc:
                _sanitize_session_filename(raw)
            assert exc.value.status_code == 400


class TestSanitizeSessionFilenameAcceptsSafe:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("mysession.bin", "mysession.bin"),
            ("My-Session_1.bin", "My-Session_1.bin"),
            ("ABC123.bin", "ABC123.bin"),
        ],
    )
    def test_valid_basenames_accepted(self, raw, expected):
        assert _sanitize_session_filename(raw) == expected

    def test_path_prefix_stripped_to_safe_basename(self):
        # Directory components are dropped; only the safe basename is returned.
        assert _sanitize_session_filename("any/dir/mysession.bin") == "mysession.bin"

    @pytest.mark.parametrize("raw", ["../../etc/passwd.bin", "....//....//etc.bin", "subdir/inside.bin"])
    def test_traversal_prefix_with_valid_basename_is_neutralized(self, raw):
        """A traversal-shaped prefix whose final component is a valid .bin name
        is neutralized to the safe basename — the output never contains a path
        separator or '..' component, so it cannot escape the slots dir."""
        out = _sanitize_session_filename(raw)
        assert "/" not in out
        assert ".." not in out
        assert out.endswith(".bin")


# ── Normalised containment inside an injected slots directory ──────────
# CodeQL alert #11 (`py/path-injection`, app/gateway/sessions.py:46) closes on
# the realpath + startswith shape below, which is the shape the query's model
# recognises as a barrier. These tests drive the real helper against a real
# temp directory, so they fail if either half of the guard is removed.


class TestContainmentInsideTheSlotsDirectory:
    @pytest.fixture
    def slots(self, tmp_path: Path, monkeypatch):
        from app.gateway import sessions

        slots_dir = tmp_path / "llama_slots"
        slots_dir.mkdir()
        monkeypatch.setattr(sessions, "_SESSION_SLOTS_DIR", slots_dir)
        return slots_dir

    @pytest.fixture
    def sessions(self, slots):
        from app.gateway import sessions as sessions_mod

        return sessions_mod

    @pytest.mark.parametrize(
        "raw",
        [
            "../../etc/passwd.bin",
            "../../../root/.ssh/id_rsa.bin",
            "....//....//etc/cron.d/evil.bin",
            "..\\..\\windows\\system32\\evil.bin",
            "/etc/shadow.bin",
            "a\x00b.bin",
            "..bin",
            ".bin",
            "slot.bin/../other.bin",
        ],
    )
    def test_traversal_shaped_ids_can_never_escape(self, sessions, slots, raw):
        """Every input either raises 400 or names a file *inside* the slots dir."""
        try:
            name = sessions.sanitize_session_filename(raw)
        except HTTPException as exc:
            assert exc.status_code == 400
            return
        candidate = os.path.realpath(os.path.join(os.path.realpath(slots), name))
        assert candidate.startswith(os.path.realpath(slots) + os.sep), (
            f"{raw!r} escaped the slots directory as {candidate!r}"
        )
        assert os.path.basename(candidate) == name

    def test_symlink_planted_in_the_slots_directory_is_rejected(
        self, sessions, slots, tmp_path
    ):
        """``realpath`` resolves the link, so a link to /etc/passwd fails the
        containment check instead of being read or written outside the dir."""
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"not a session")
        (slots / "escape.bin").symlink_to(outside)

        with pytest.raises(HTTPException) as exc:
            sessions.sanitize_session_filename("escape.bin")
        assert exc.value.status_code == 400

    def test_a_real_slot_name_is_still_accepted(self, sessions):
        assert sessions.sanitize_session_filename("slot-1_A.bin") == "slot-1_A.bin"
        assert sessions.sanitize_session_filename("dir/slot-2.bin") == "slot-2.bin"

    def test_an_unconfigured_slots_directory_fails_closed(self, sessions, monkeypatch):
        monkeypatch.setattr(sessions, "_SESSION_SLOTS_DIR", None)
        with pytest.raises(HTTPException) as exc:
            sessions.sanitize_session_filename("slot.bin")
        assert exc.value.status_code == 500
