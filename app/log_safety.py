"""Keep untrusted diagnostics and secrets out of log lines.

Two independent jobs:

1. :func:`clean_log_value` bounds untrusted values to one printable log line
   (log-injection guard).
2. :func:`install_secret_redaction` makes a secret *unloggable*: every record
   that would print a registered credential, or a ``Bearer``/``api_key`` shaped
   value, is scrubbed before it reaches a handler.  G1-02 owns "no credential in
   the log at INFO/DEBUG"; the audit surface (G1-03) owns *what* must be logged,
   not this leak guard.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Mapping

#: C0/C1 control characters, minus CR and LF: line breaks are removed
#: explicitly in :func:`clean_log_value` so the removal is visible.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Values that are scrubbed wherever they appear in a log record.
_KNOWN_SECRETS: set[str] = set()

#: ``Bearer <token>`` shaped credentials.
_BEARER = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=\-]{6,})")
#: ``Authorization: …`` / ``api_key=…`` shaped credentials.
_HEADER_SECRET = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-goog-api-key)"
    r"(\s*[:=]\s*)([^\s,;\"'}]+)"
)
_ENV_REFERENCE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

REDACTED = "***redacted***"


def clean_log_value(value: Any, limit: int = 160) -> str:
    """Bound *value* to a single printable log line.

    Line breaks go first (``\\r``, then ``\\n``), then the rest of the C0/C1
    control range — NUL, TAB, ESC (which is what makes a terminal sequence
    executable), DEL and the C1 block.  The length is capped so one value cannot
    flood a log record.

    Line-break removal is deliberately a ``str.replace`` on the value itself:
    that is the operation CodeQL's ``py/log-injection`` model recognises as a
    sanitizer (``ReplaceLineBreaksSanitizer`` in
    ``python/ql/lib/semmle/python/security/dataflow/LogInjectionCustomizations.qll``).
    Because this helper is the only route a request value takes to a log call,
    the model and the runtime guard agree instead of drifting apart.

    Returned text is diagnostic only; request values and response payloads
    remain unchanged.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    return _CONTROL_CHARACTERS.sub(" ", text).strip()[:limit]


def register_secret(value: str | None) -> None:
    """Register a credential that must never appear in a log record."""
    if not isinstance(value, str):
        return
    candidate = value.strip()
    if len(candidate) < 6 or candidate.startswith("${"):
        return
    _KNOWN_SECRETS.add(candidate)


def register_secrets(values: Iterable[str | None]) -> None:
    for value in values:
        register_secret(value)


def register_configured_secrets(config: Mapping[str, Any] | None = None) -> int:
    """Register every provider credential the live configuration can send.

    Provider documents store ``${VAR}`` references rather than values, so the
    referenced environment variables are resolved and registered; literal values
    are registered as they are.  Returns the number of newly registered values.
    """
    if config is None:
        from app.config_loader import CONFIG  # local import: no import cycle

        config = CONFIG
    before = len(_KNOWN_SECRETS)
    register_secret(os.environ.get("CARETAKER_KEY", ""))
    providers = config.get("providers") if isinstance(config, Mapping) else None
    if isinstance(providers, Mapping):
        for doc in providers.values():
            if not isinstance(doc, Mapping):
                continue
            for field in ("api_key", "management_key"):
                raw = doc.get(field)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                names = _ENV_REFERENCE.findall(raw)
                for name in names:
                    register_secret(os.environ.get(name, ""))
                if "$" not in raw:
                    register_secret(raw)
    return len(_KNOWN_SECRETS) - before


def redact_secrets(text: str) -> str:
    """Return *text* with every known secret and credential shape scrubbed."""
    if not text:
        return text
    redacted = text
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, REDACTED)
    redacted = _BEARER.sub(lambda m: m.group(1) + REDACTED, redacted)
    redacted = _HEADER_SECRET.sub(lambda m: m.group(1) + m.group(2) + REDACTED, redacted)
    return redacted


def contains_credentials(text: str) -> bool:
    """True when *text* still carries a credential shape or a registered secret.

    The enforcement side of the G1-02 promise, and the primitive the G1-03
    auth-header invariant is written against: it reuses the same two patterns as
    :func:`redact_secrets`, so a test cannot drift away from the filter that
    makes a leak unloggable. Call it on what a handler *would* emit.
    """
    if not text:
        return False
    # A value that is already ``***redacted***`` is not a credential: the guard
    # must not fire on material the filter already scrubbed, or a correctly
    # redacted production log would look like a leak.
    if any(match.group(2) != REDACTED for match in _BEARER.finditer(text)):
        return True
    if any(match.group(3) != REDACTED for match in _HEADER_SECRET.finditer(text)):
        return True
    return any(secret and secret in text for secret in _KNOWN_SECRETS)


class SecretRedactionFilter(logging.Filter):
    """Scrub credentials from every log record it sees.

    Attached to the root handlers by :func:`install_secret_redaction`, so it
    covers records from every child logger (``Guardian.Providers``,
    ``uvicorn.error``, …) at every level, and it is a no-op for records that
    contain nothing secret.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib API
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken record must not be dropped
            return True
        cleaned = redact_secrets(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


_INSTALLED = False


def install_secret_redaction() -> None:
    """Install the redaction filter on the root logger and its handlers.

    Idempotent, and safe to call before handlers exist (the filter also lands on
    the root logger, which covers records logged directly on it).
    """
    global _INSTALLED
    if _INSTALLED:
        return
    redaction = SecretRedactionFilter()
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.addFilter(redaction)
    root.addFilter(redaction)
    _INSTALLED = True
