"""Unit tests for raw-capture media extraction (images out-of-band, WAL refs)."""

import base64
import hashlib
import json
from pathlib import Path

from app.capture.media import extract_media_from_messages, is_media_reference

# A tiny 1x1 PNG (valid enough for base64 round-trip + dimension sniffing).
PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _png_bytes() -> bytes:
    return base64.b64decode(PNG_1PX)


class TestOpenAIImageExtraction:
    def test_data_url_image_written_to_media_file(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
            ],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-1")
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
        out = extract_media_from_messages(msg, tmp_path, "req-2")
        block = out[0]["content"][0]
        assert block["type"] == "image_url"
        assert block["image_url"]["url"] == "https://example.com/x.png"

    def test_malformed_data_url_is_error_reference(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,@@not-base64@@"}}],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-3")
        ref = out[0]["content"][0]
        assert ref["type"] == "image_media"
        assert "error" in ref["image_media"]

    def test_text_only_messages_unchanged(self, tmp_path):
        msg = [{"role": "user", "content": "hello"}]
        out = extract_media_from_messages(msg, tmp_path, "req-4")
        assert out == msg

    def test_non_list_input_passthrough(self, tmp_path):
        assert extract_media_from_messages(None, tmp_path, "req-5") is None
        assert extract_media_from_messages("string", tmp_path, "req-5") == "string"


class TestAnthropicImageExtraction:
    def test_base64_source_written_to_media_file(self, tmp_path):
        msg = [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_1PX}},
            ],
        }]
        out = extract_media_from_messages(msg, tmp_path, "req-a")
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
        out = extract_media_from_messages(msg, tmp_path, "req-b")
        block = out[0]["content"][0]
        assert block["source"]["type"] == "url"


class TestMediaReferenceHelper:
    def test_is_media_reference(self):
        assert is_media_reference({"type": "image_media", "image_media": {"path": "media/x"}})
        assert not is_media_reference({"type": "text", "text": "hi"})
        assert not is_media_reference(None)
        assert not is_media_reference({"type": "image_media"})  # missing payload
