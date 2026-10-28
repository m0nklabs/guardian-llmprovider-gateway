"""Unit tests for raw-capture media extraction (images out-of-band, WAL refs)."""

import base64
import contextlib
import hashlib
import os
import stat
from pathlib import Path

import pytest

from app.capture.config import CaptureConfig
from app.capture.media import (
    ensure_media_directory,
    extract_media_from_messages,
    is_media_reference,
)

# A tiny 1x1 PNG (valid enough for base64 round-trip + dimension sniffing).
PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _png_bytes() -> bytes:
    return base64.b64decode(PNG_1PX)


def _config(**overrides) -> CaptureConfig:
    """The capture config the writer takes its modes from (no literals there)."""
    return CaptureConfig(**overrides)


def _image_message() -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
        ],
    }]


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextlib.contextmanager
def umask(value: int):
    """Temporarily set the process umask; always restore it afterwards."""
    original = os.umask(value)
    try:
        yield
    finally:
        os.umask(original)


class TestOpenAIImageExtraction:
    def test_data_url_image_written_to_media_file(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
            ],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-1", _config())
        blocks = out[0]["content"]
        assert len(blocks) == 2
        ref = blocks[1]
        assert ref["type"] == "image_media"
        media = ref["image_media"]
        assert media["mime_type"] == "image/png"
        assert media["size_bytes"] == len(_png_bytes())
        assert "sha256" in media and len(media["sha256"]) == 64
        assert media["path"].startswith("media/req-1_")
        # File exists with matching hash
        target = tmp_path / media["path"]
        assert target.exists()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == media["sha256"]

    def test_remote_url_untouched(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            ],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-2", _config())
        block = out[0]["content"][0]
        assert block["type"] == "image_url"
        assert block["image_url"]["url"] == "https://example.com/x.png"

    def test_malformed_data_url_is_error_reference(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,@@not-base64@@"}}],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-3", _config())
        ref = out[0]["content"][0]
        assert ref["type"] == "image_media"
        assert "error" in ref["image_media"]

    def test_text_only_messages_unchanged(self, tmp_path):
        msg = [{"role": "user", "content": "hello"}]
        out = extract_media_from_messages(msg, tmp_path, "req-4", _config())
        assert out == msg

    def test_non_list_input_passthrough(self, tmp_path):
        assert extract_media_from_messages(None, tmp_path, "req-5", _config()) is None
        assert extract_media_from_messages("string", tmp_path, "req-5", _config()) == "string"


class TestAnthropicImageExtraction:
    def test_base64_source_written_to_media_file(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_1PX}},
            ],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-a", _config())
        ref = out[0]["content"][0]
        assert ref["type"] == "image_media"
        media = ref["image_media"]
        assert media["mime_type"] == "image/png"
        assert media["path"].startswith("media/req-a_")
        assert (tmp_path / media["path"]).exists()

    def test_url_source_untouched(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [{"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-b", _config())
        block = out[0]["content"][0]
        assert block["source"]["type"] == "url"


class TestMediaReferenceHelper:
    def test_is_media_reference(self):
        assert is_media_reference({"type": "image_media", "image_media": {"path": "media/x"}})
        assert not is_media_reference({"type": "text", "text": "hi"})
        assert not is_media_reference(None)
        assert not is_media_reference({"type": "image_media"})  # missing payload


class TestMediaPermissions:
    """Issue #49: media modes come from config, never from the process umask."""

    def test_defaults_are_the_wal_pair_and_never_world_accessible(self):
        cfg = CaptureConfig()
        assert cfg.file_mode == 0o640  # group read is deliberate (WAL pair)
        assert cfg.directory_mode == 0o750
        assert not cfg.file_mode & 0o007
        assert not cfg.directory_mode & 0o007

    def test_writer_requires_the_config(self):
        """No config-supplied modes must not silently mean "use the umask"."""
        with pytest.raises(TypeError):
            extract_media_from_messages(_image_message(), Path("."), "req-x")

    @pytest.mark.parametrize("umask_value", [0o000, 0o022, 0o002, 0o077])
    def test_file_and_directory_modes_are_umask_independent(
        self, tmp_path, umask_value
    ):
        cfg = _config()
        with umask(umask_value):
            out = extract_media_from_messages(
                _image_message(), tmp_path, "req-mode", cfg
            )

        ref = out[0]["content"][0]["image_media"]
        target = tmp_path / ref["path"]
        # Exactly the configured modes, not merely "no world bits".
        assert _mode(tmp_path / "media") == cfg.directory_mode
        assert _mode(target) == cfg.file_mode
        assert not _mode(target) & 0o007
        assert target.read_bytes() == _png_bytes()

    def test_configured_non_default_modes_are_applied(self, tmp_path):
        cfg = _config(file_mode=0o600, directory_mode=0o700)
        with umask(0o000):
            out = extract_media_from_messages(
                _image_message(), tmp_path, "req-custom", cfg
            )

        ref = out[0]["content"][0]["image_media"]
        assert _mode(tmp_path / "media") == 0o700
        assert _mode(tmp_path / ref["path"]) == 0o600

    def test_anthropic_source_files_get_the_configured_mode(self, tmp_path):
        cfg = _config()
        with umask(0o000):
            out = extract_media_from_messages(
                [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_1PX}},
                    ],
                }],
                tmp_path,
                "req-ant-mode",
                cfg,
            )
        ref = out[0]["content"][0]["image_media"]
        assert _mode(tmp_path / ref["path"]) == cfg.file_mode

    def test_existing_looser_file_is_tightened(self, tmp_path):
        """A rewrite tightens an existing file instead of leaving it alone.

        Chosen behaviour: the configured mode is authoritative for Guardian's
        own capture artefacts, so a repeated ``<request_id>_<idx>`` path (retry,
        replay) repairs a file that predates the explicit-mode fix instead of
        preserving world-readable permissions — exactly the state issue #49
        reports for the 305 historical files.
        """
        media_dir = tmp_path / "media"
        # Drift created the way it actually happened: default creation modes
        # under a permissive umask (a 0022 shell), no explicit chmod.
        with umask(0o022):
            media_dir.mkdir()
        existing = media_dir / "req-tighten_0.png"
        with umask(0o022):
            existing.write_bytes(b"stale drift")
        assert _mode(media_dir) == 0o755
        assert _mode(existing) == 0o644

        cfg = _config()
        with umask(0o000):
            out = extract_media_from_messages(
                _image_message(), tmp_path, "req-tighten", cfg
            )

        ref = out[0]["content"][0]["image_media"]
        assert ref["path"] == "media/req-tighten_0.png"
        assert _mode(existing) == cfg.file_mode
        assert _mode(media_dir) == cfg.directory_mode
        assert existing.read_bytes() == _png_bytes()

    def test_existing_tighter_file_is_relaxed_to_the_configured_mode(self, tmp_path):
        """0600 file, configured 0640 -> 0640 (config wins, matching the WAL).

        The WAL writer behaves the same way (``os.open(..., file_mode)`` then
        ``chmod``), so a stricter ambient umask cannot make the two writers
        disagree about the same capture tree.
        """
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        existing = media_dir / "req-relax_0.png"
        with umask(0o077):
            existing.write_bytes(b"old")
        assert _mode(existing) == 0o600

        cfg = _config()
        out = extract_media_from_messages(_image_message(), tmp_path, "req-relax", cfg)
        ref = out[0]["content"][0]["image_media"]
        assert ref["path"] == "media/req-relax_0.png"
        assert _mode(existing) == cfg.file_mode

    def test_symlinked_target_is_refused_and_never_followed(self, tmp_path):
        """O_NOFOLLOW: a writable media dir cannot redirect the write."""
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"must not change")
        (media_dir / "req-sym_0.png").symlink_to(outside)

        out = extract_media_from_messages(
            _image_message(), tmp_path, "req-sym", _config()
        )
        ref = out[0]["content"][0]["image_media"]
        assert ref["error"] == "write_failed"  # fail-open, no payload written
        assert outside.read_bytes() == b"must not change"

    def test_startup_hardening_fixes_directory_but_never_touches_files(self, tmp_path):
        """Directory drift is repaired at writer start; files are not.

        Historical files are operator data: correcting them is the documented
        one-shot (`find ... -perm -004 -exec chmod`), never a service startup
        side effect.
        """
        media_dir = tmp_path / "media"
        with umask(0o022):
            media_dir.mkdir()
        legacy = media_dir / "legacy_0.png"
        with umask(0o022):
            legacy.write_bytes(b"historical drift")
        assert _mode(media_dir) == 0o755
        assert _mode(legacy) == 0o644

        cfg = _config()
        with umask(0o000):
            assert ensure_media_directory(tmp_path, cfg) is True

        assert _mode(media_dir) == cfg.directory_mode
        assert _mode(legacy) == 0o644  # untouched

    def test_startup_hardening_is_a_noop_without_a_media_dir(self, tmp_path):
        assert ensure_media_directory(tmp_path, _config()) is False
        assert not (tmp_path / "media").exists()
