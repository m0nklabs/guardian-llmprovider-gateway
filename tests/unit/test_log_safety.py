"""Discovery diagnostics must not turn untrusted values into log lines."""

import inspect

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


# A request value an attacker controls completely: a forged log line, a terminal
# escape sequence and a payload bomb in one string.
HOSTILE = (
    'evil/model"\n'
    "2026-10-28 12:00:00 INFO event=deny user=admin action=read\n"
    "\x1b[2J\x1b[0m\x07\x00"
)


def test_a_forged_log_entry_cannot_start_a_new_line():
    cleaned = clean_log_value(HOSTILE)
    for character in ("\n", "\r", "\x1b", "\x00", "\x07"):
        assert character not in cleaned, f"control character {character!r} survived"
    # The text itself is still visible for diagnosis — only its line structure is gone.
    assert "evil/model" in cleaned and "event=deny" in cleaned


def test_ansi_escape_sequences_are_defused():
    assert clean_log_value("\x1b[31mred\x1b[0m") == "[31mred [0m"
    # A bare ESC is what a terminal needs; without it the rest is literal text.
    assert "\x1b" not in clean_log_value("\x1b]0;title\x07")


def test_a_megabyte_value_cannot_flood_the_log():
    assert clean_log_value("A" * 1_000_000) == "A" * 160
    assert clean_log_value(("line\n" * 10_000), limit=80) == ("line line " * 9).strip()[:80]


def test_the_line_break_removal_is_the_modelled_sanitizer():
    """CodeQL's py/log-injection model only sees an inline ``.replace("\\n", …)``.

    The helper therefore has to remove CR/LF through ``str.replace`` on the value
    itself (not just through the control-character regex), or the model and the
    runtime guard drift apart and every wrapped call site keeps its alert.
    """
    source = inspect.getsource(clean_log_value)
    assert '.replace("\\r", " ").replace("\\n", " ")' in source


@pytest.mark.parametrize(
    "separator",
    ["\u2028", "\u2029", "\u0085"],
    ids=["line-separator-U2028", "paragraph-separator-U2029", "next-line-U0085"],
)
def test_unicode_line_separators_are_removed(separator):
    """NEL/LS/PS are line breaks to a Unicode-aware reader (PR-Piet on #74).

    They are outside the C0/C1 range, so the control-character scrub alone let
    them through — while the helper this replaced used ``str.isprintable()`` and
    removed them. A reader that decodes before display (journald, a terminal, a
    log UI) would render the value on a second line.
    """
    cleaned = clean_log_value(f"model-a{separator}event=deny")
    for bad in ("\u2028", "\u2029", "\u0085", "\n", "\r"):
        assert bad not in cleaned
    assert cleaned == "model-a event=deny"


def test_a_forged_line_through_a_separator_cannot_survive():
    """End to end: the segmented value reaches one line, prefix intact."""
    value = "request-42\u2028🛑 forged deny record"
    cleaned = clean_log_value(value)
    assert "\u2028" not in cleaned
    assert cleaned.startswith("request-42 ")
    assert "🛑" in cleaned  # the content is preserved, only the break is defused
