"""Credential-safe OpenAI errors and narrowly forwarded retry guidance."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime


# Upstream exception messages and arbitrary response headers can contain tokens
# or prompts. Only a syntactically valid Retry-After value crosses this boundary.
def retry_after(value: object) -> str | None:
    """Return an HTTP delay/date, never arbitrary header content."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if "\r" in value or "\n" in value:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", value):
        return value
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        # Normalization can overflow even when parsing succeeds. HTTP date names
        # are locale-independent; do not use process-locale strftime names.
        expected = format_datetime(parsed.astimezone(timezone.utc), usegmt=True)
    except (TypeError, ValueError, OverflowError):
        return None
    # Only IMF-fixdate is forwarded; parsing a liberal mail date is insufficient.
    return value if value == expected else None


def error_status(error: BaseException) -> int:
    """Classify by structured status/type, never by credential-bearing text."""
    code = getattr(error, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
        return code
    if isinstance(error, TimeoutError):
        return 504
    # httpx timeouts do not inherit the built-in TimeoutError.
    import httpx

    return 504 if isinstance(error, httpx.TimeoutException) else 502


def error_payload(error: BaseException) -> dict:
    """Describe failure without quoting the provider's exception or body."""
    status = error_status(error)
    if status == 429:
        kind, code, message = "rate_limit_error", "rate_limit_exceeded", "Copilot rate limit reached; retry at the caller."
    elif status in (401, 403):
        kind, code, message = "authentication_error", "upstream_authentication_failed", "Copilot upstream authentication failed."
    elif status == 504:
        kind, code, message = "timeout_error", "upstream_timeout", "Copilot upstream timed out."
    elif status < 500:
        kind, code, message = "invalid_request_error", "upstream_request_rejected", "Copilot upstream rejected the request."
    else:
        kind, code, message = "api_error", "upstream_error", "Copilot upstream request failed."
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


def error_headers(error: BaseException) -> dict[str, str]:
    """Forward only validated Retry-After on a rate limit or unavailable service."""
    if error_status(error) not in (429, 503):
        return {}
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        headers = getattr(error, "headers", None)
    if not isinstance(headers, Mapping):
        return {}
    value = headers.get("retry-after", headers.get("Retry-After"))
    accepted = retry_after(value)
    return {"Retry-After": accepted} if accepted is not None else {}
