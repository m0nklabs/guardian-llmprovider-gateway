"""Tests for app.proxy.server._sanitize_session_filename — path-traversal guard
for /api/session/save and /api/session/load.

The sanitizer must reject anything that could escape the llama-server
--slot-save-path ($HOME/llama_slots) directory, and accept only safe basenames.
"""

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
