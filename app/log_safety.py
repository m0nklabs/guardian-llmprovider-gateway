"""Bound untrusted diagnostic values to one printable log line."""

from __future__ import annotations

import re
from typing import Any

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_log_value(value: Any, limit: int = 160) -> str:
    """Remove line/control characters and truncate identifiers and exceptions.

    Mirrors the existing speech handlers' log-injection guard without importing
    an inference handler into discovery code. Returned text is diagnostic only;
    request values and response payloads remain unchanged.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    return _CONTROL_CHARACTERS.sub(" ", text).strip()[:limit]
