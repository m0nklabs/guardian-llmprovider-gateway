"""The data-URL parser must be linear and shared (issue #71).

CodeQL flagged ``data:([^;]+);base64,(.*)`` as a polynomial ReDoS on a
client-controlled string. These tests pin the replacement: no regex touches the
input, pathological lengths cost linear time, and both call sites use the one
parser so a fix cannot half-apply again.
"""

from __future__ import annotations

import time

import pytest

from app.capture import media, redactor
from app.capture.data_url import parse_data_url


class TestParsing:
    def test_a_plain_base64_data_url_splits_into_type_and_payload(self):
        assert parse_data_url("data:image/png;base64,AAAA") == ("image/png", "AAAA")

    def test_an_empty_payload_is_still_a_valid_data_url(self):
        assert parse_data_url("data:image/png;base64,") == ("image/png", "")

    def test_a_media_type_with_parameters_parses(self):
        """``text/plain;charset=utf-8`` is a valid type the old regex rejected."""
        assert parse_data_url("data:text/plain;charset=utf-8;base64,aGk=") == (
            "text/plain;charset=utf-8",
            "aGk=",
        )

    def test_a_payload_containing_commas_is_not_truncated(self):
        assert parse_data_url("data:image/png;base64,AA,BB,CC") == ("image/png", "AA,BB,CC")

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "https://example.com/cat.png",
            "data:image/png,AAAA",            # no ;base64
            "data:;base64,AAAA",              # no media type
            "data:image/png;base64",          # no comma
        ],
        ids=["empty", "remote-url", "not-base64", "no-type", "no-comma"],
    )
    def test_malformed_input_returns_none(self, url):
        assert parse_data_url(url) is None

    def test_non_string_input_returns_none(self):
        assert parse_data_url(None) is None
        assert parse_data_url(1234) is None


class TestLinearTime:
    """A pathological length must cost linear time, not exponential.

    The bound is generous (CI hosts are noisy) but the *shape* is what matters:
    the old pattern retried the negated class at every ``;``-free run that was
    not followed by the ``;base64,`` literal, so a few megabytes could occupy a
    worker for a very long time.
    """

    @staticmethod
    def _pathological(size: int) -> str:
        # No ';' at all and no ';base64,' terminator: every split point is tried
        # by the old pattern.
        return "data:" + "a" * size

    @staticmethod
    def _many_semicolons(size: int) -> str:
        return "data:" + ("a;" * (size // 2))

    @pytest.mark.parametrize("builder", ["_pathological", "_many_semicolons"])
    def test_two_megabytes_parse_in_well_under_a_second(self, builder):
        url = getattr(self, builder)(2 * 1024 * 1024)
        started = time.perf_counter()
        assert parse_data_url(url) is None
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"{builder} took {elapsed:.3f}s for 2 MiB"

    def test_cost_scales_linearly_with_length(self):
        def measure(size: int) -> float:
            url = self._many_semicolons(size)
            started = time.perf_counter()
            for _ in range(5):
                parse_data_url(url)
            return time.perf_counter() - started

        small = measure(64 * 1024)
        large = measure(512 * 1024)  # 8x the input
        # Linear would be ~8x; quadratic would be ~64x. Allow generous noise.
        assert large <= max(small, 1e-4) * 24, f"small={small:.4f}s large={large:.4f}s"

    def test_the_parser_never_compiles_a_regex(self, monkeypatch):
        """No regular expression may touch the client-controlled string."""
        import re as _re

        def boom(*args, **kwargs):  # pragma: no cover - only runs on failure
            raise AssertionError("parse_data_url must not use re")

        monkeypatch.setattr(_re, "compile", boom)
        monkeypatch.setattr(_re, "match", boom)
        monkeypatch.setattr(_re, "search", boom)
        assert parse_data_url("data:image/png;base64,AAAA") == ("image/png", "AAAA")
        assert parse_data_url("data:" + "a" * 4096) is None


class TestBothCallSitesUseTheOneParser:
    """A fix must not half-apply again: media and the redactor share the parser."""

    def test_media_uses_the_shared_parser(self, monkeypatch, tmp_path):
        calls: list[str] = []

        def spy(url):
            calls.append(url)
            return ("image/png", "AAAA")

        monkeypatch.setattr(media, "parse_data_url", spy)
        block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        media.extract_media_from_messages(
            [{"role": "user", "content": [block]}],
            tmp_path,
            "req-1",
            media.CaptureConfig(),
        )
        assert calls == ["data:image/png;base64,AAAA"]

    def test_the_redactor_uses_the_shared_parser(self, monkeypatch):
        calls: list[str] = []

        def spy(url):
            calls.append(url)
            return ("image/png", "AAAA")

        monkeypatch.setattr(redactor, "parse_data_url", spy)
        redactor._extract_image_metadata("data:image/png;base64,AAAA")
        assert calls == ["data:image/png;base64,AAAA"]

    def test_the_old_regex_is_gone_from_both_modules(self):
        import inspect

        assert "base64,(.*)" not in inspect.getsource(media)
        assert "base64,(.*)" not in inspect.getsource(redactor)
