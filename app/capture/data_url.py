"""Data-URL parsing, shared by the media writer and the redactor.

One implementation, linear by construction, so the two call sites cannot drift
apart and no regular expression is applied to a client-controlled string — see
issue #71: the previous pattern ``data:([^;]+);base64,(.*)`` was a polynomial
ReDoS shape (CodeQL ``py/polynomial-redos``, high).
"""

from __future__ import annotations

DATA_URL_PREFIX = "data:"
BASE64_SUFFIX = ";base64"


def parse_data_url(url: str) -> tuple[str, str] | None:
    """Split a base64 data URL into ``(media_type, base64_payload)``.

    Linear by construction: one prefix check, one ``find`` and two slices — no
    regular expression touches the (client-controlled) string. The previous
    pattern ``data:([^;]+);base64,(.*)`` was a polynomial-ReDoS shape: the
    unbounded negated class had to be retried against every ``;``-free run that
    was not followed by the ``;base64,`` literal (CodeQL ``py/polynomial-redos``,
    alert #3, issue #71), and the body-size bound limits the input, not the work.

    Returns ``None`` for anything that is not a base64 data URL. A media type may
    carry parameters (``text/plain;charset=utf-8;base64,...``): everything before
    the final ``;base64`` is the media type, so those parse correctly instead of
    being rejected as malformed.
    """
    if not isinstance(url, str) or not url.startswith(DATA_URL_PREFIX):
        return None
    comma = url.find(",", len(DATA_URL_PREFIX))
    if comma < 0:
        return None
    header = url[len(DATA_URL_PREFIX) : comma]
    if not header.lower().endswith(BASE64_SUFFIX):
        return None
    media_type = header[: -len(BASE64_SUFFIX)]
    if not media_type:
        return None
    return media_type, url[comma + 1 :]
