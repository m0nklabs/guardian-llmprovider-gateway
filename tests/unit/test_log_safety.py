"""Discovery diagnostics must not turn untrusted values into log lines."""

import pytest

from app.log_safety import clean_log_value


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\tb", "a\x00b", "a\x7fb", "a\x85b"])
def test_control_characters_are_removed(value):
    assert clean_log_value(value) == "a b"


def test_diagnostics_are_bounded_and_normal_text_is_preserved():
    assert clean_log_value("openai/gpt-4o") == "openai/gpt-4o"
    assert clean_log_value("x" * 1000) == "x" * 160
    assert clean_log_value(RuntimeError("bad\nvalue")) == "bad value"
    assert clean_log_value("abcdef", limit=3) == "abc"
