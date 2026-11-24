"""Tests for capture permission-mode validation (issue #135).

``capture.file_mode`` and ``capture.directory_mode`` are written as an octal
string (``'0640'``, ``'0o640'``) or as an integer.  A value that is neither must
fail as a *configuration* error that names the key, the value as written and the
accepted forms — not as an ``int()`` traceback that names neither the setting nor
the file — while the refusal of a world-accessible mode keeps its existing
message and behaviour.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from app.capture.config import (
    DEFAULT_DIRECTORY_MODE,
    DEFAULT_FILE_MODE,
    CaptureConfigError,
    capture_config_from_section,
    load_capture_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

MODE_KEYS = ("file_mode", "directory_mode")


def _load_guardianctl():
    """Import scripts/guardianctl.py as a module (scripts/ must be importable)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "guardianctl_mode_validation_module", SCRIPTS_DIR / "guardianctl.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gctl():
    return _load_guardianctl()


def _settings_file(path: Path, *, file_mode: str, directory_mode: str = "0750") -> Path:
    """A minimal settings document carrying the configured capture modes."""
    path.write_text(
        "capture:\n"
        "  enabled: false\n"
        f"  file_mode: '{file_mode}'\n"
        f"  directory_mode: '{directory_mode}'\n"
    )
    return path


# ---------------------------------------------------------------------------
# Accepted forms — unchanged behaviour
# ---------------------------------------------------------------------------


class TestValidModes:
    @pytest.mark.parametrize("key", MODE_KEYS)
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0640", 0o640),  # octal string
            ("0o640", 0o640),  # 0o-prefixed string
            ("640", 0o640),  # bare octal digits
            (0o640, 0o640),  # YAML 1.1 reads a bare 0o640 as the integer
            (416, 0o640),  # decimal integer for 0o640
            ("0700", 0o700),
            (0o600, 0o600),
        ],
    )
    def test_every_accepted_form_reads_as_the_mode(self, key, raw, expected):
        cfg = capture_config_from_section({key: raw})
        assert getattr(cfg, key) == expected

    def test_an_empty_section_keeps_the_documented_defaults(self):
        cfg = capture_config_from_section({})
        assert (cfg.file_mode, cfg.directory_mode) == (
            DEFAULT_FILE_MODE,
            DEFAULT_DIRECTORY_MODE,
        )
        assert capture_config_from_section(None).file_mode == DEFAULT_FILE_MODE

    def test_a_valid_section_changes_only_the_modes(self):
        cfg = capture_config_from_section(
            {"file_mode": "0600", "directory_mode": 0o700}
        )
        assert (cfg.file_mode, cfg.directory_mode) == (0o600, 0o700)


# ---------------------------------------------------------------------------
# Malformed modes — the error quality this issue is about
# ---------------------------------------------------------------------------


class TestMalformedModeIsAConfigurationError:
    @pytest.mark.parametrize("key", MODE_KEYS)
    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty string
            "999",  # digits beyond octal
            "0648",  # an 8 is not an octal digit
            "rg-r--r--",  # rwx notation, not supported
            "64O",  # letter O instead of digit zero
            "0o",  # prefix without digits
            "  ",  # whitespace only
        ],
    )
    def test_message_names_the_key_the_value_and_the_accepted_forms(self, key, raw):
        with pytest.raises(CaptureConfigError) as caught:
            capture_config_from_section({key: raw})

        message = str(caught.value)
        assert f"capture.{key}={raw!r}" in message, message
        assert "'0640'" in message, message
        assert "'0o640'" in message, message
        assert "integer" in message, message

    @pytest.mark.parametrize("key", MODE_KEYS)
    @pytest.mark.parametrize("raw", [True, False])
    def test_a_bool_is_refused_instead_of_meaning_zero_or_one(self, key, raw):
        # ``bool`` is an ``int`` subclass: read as 0o1/0o0 it would be either a
        # confusing world-access refusal (True) or a silently trusted 0o000
        # (False).  Neither is a mode anybody wrote.
        with pytest.raises(CaptureConfigError) as caught:
            capture_config_from_section({key: raw})

        message = str(caught.value)
        assert f"capture.{key}={raw!r}" in message, message
        assert "bool" in message, message
        assert f"{oct(int(raw))}" in message, message

    @pytest.mark.parametrize("key", MODE_KEYS)
    @pytest.mark.parametrize("raw", [6.5, None, ["0640"], {"mode": "0640"}])
    def test_a_non_scalar_value_is_a_configuration_error(self, key, raw):
        with pytest.raises(CaptureConfigError) as caught:
            capture_config_from_section({key: raw})
        assert f"capture.{key}=" in str(caught.value)

    def test_the_error_is_distinguishable_without_string_matching(self):
        # A caller that wants to treat "bad config" specially catches the type.
        assert issubclass(CaptureConfigError, ValueError)
        with pytest.raises(CaptureConfigError):
            capture_config_from_section({"file_mode": "rg-r--r--"})


# ---------------------------------------------------------------------------
# The world-access refusal is untouched
# ---------------------------------------------------------------------------


class TestWorldAccessRefusalIsUnchanged:
    @pytest.mark.parametrize(
        ("key", "raw", "octal"),
        [
            ("file_mode", 0o644, "0o644"),
            ("file_mode", 420, "0o644"),
            ("file_mode", "0644", "0o644"),
            ("file_mode", "0666", "0o666"),
            ("directory_mode", 0o755, "0o755"),
            ("directory_mode", "0777", "0o777"),
        ],
    )
    def test_refused_with_the_key_and_the_octal_value(self, key, raw, octal):
        with pytest.raises(ValueError) as caught:
            capture_config_from_section({key: raw})
        assert str(caught.value) == (
            f"capture.{key}={octal} must not grant world access"
        )


# ---------------------------------------------------------------------------
# The settings file is named where it is known; the permissions path reuses
# the same parser and error type
# ---------------------------------------------------------------------------


class TestBothCallPathsReportTheSameError:
    def test_the_loader_names_the_settings_file(self, tmp_path):
        settings = _settings_file(tmp_path / "global.settings.yaml", file_mode="rg-r--r--")
        with pytest.raises(CaptureConfigError) as caught:
            load_capture_config(settings)

        message = str(caught.value)
        assert str(settings) in message, message
        assert "capture.file_mode='rg-r--r--'" in message, message

    def test_the_permissions_audit_reuses_the_parser_and_the_error_type(self, gctl):
        assert gctl.CaptureConfigError is CaptureConfigError

    def test_the_permissions_command_exits_two_on_a_malformed_mode(
        self, gctl, monkeypatch, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path / "settings.yaml", file_mode="rg-r--r--")
        monkeypatch.setattr(gctl, "SETTINGS_YAML", settings)

        with pytest.raises(SystemExit) as excinfo:
            gctl.cmd_permissions(argparse.Namespace(apply=False, json=False))
        err = capsys.readouterr().err

        assert excinfo.value.code == gctl.PERMISSIONS_EXIT_ERROR == 2
        assert str(settings) in err, err
        assert "capture.file_mode='rg-r--r--'" in err, err
        assert "'0640'" in err and "'0o640'" in err, err