"""Bound the request-derived part of an upstream URL (issue #75).

Source of record: ``docs/decisions/G1-02-destination-and-credentials.md`` and
its amendment of 2026-10-28 ("the allowlist cannot see a path").

The G1-02 destination policy decides **where** traffic goes: scheme, host and
port of every outbound hop. It deliberately models nothing below the authority,
so it cannot tell ``/v1/chat/completions`` from ``/props`` — and the two are not
the same request. The local backend routes ``/v1/{path}`` (GET *and* POST) by
splicing a client-supplied path onto a configured base URL:

    f"{_llama_server_url}/v1/{path}"

``path`` cannot move the destination (that only needs the configuration, which
the guard already checks), but it can leave the ``/v1`` namespace: ``httpx``
normalizes dot segments before the request leaves the process, so
``POST /v1/../slots`` reaches the backend's ``/slots`` endpoint — same approved
host, different API surface, and a refusal the allowlist will never produce.
That is the ``py/partial-ssrf`` shape at these four sites: part of the URL is
attacker-influenced, and the guard absorbs the part it can see.

One builder, one rule: a request-derived component is appended to a *template*
and the assembled URL is then re-parsed and re-checked (same scheme, host and
port as the base, still inside the expected path prefix). ``httpx.URL`` is the
parser on purpose — it is the same normalization the transport applies, so the
check cannot drift from what is actually sent.

What this module is not: a second allowlist. Endpoint *names* stay the
backend's business; only the shape of the path is bounded here, which is what
keeps every legitimate ``/v1/...`` endpoint working unchanged.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, unquote

import httpx
from fastapi import HTTPException

from app.log_safety import clean_log_value

logger = logging.getLogger("Guardian.UpstreamURL")

#: Stable machine codes. Part of the error contract; add rather than rename.
REASON_EMPTY_PATH = "empty_path"
REASON_CONTROL_CHARACTER = "control_character"
REASON_BACKSLASH = "backslash"
REASON_ABSOLUTE_PATH = "absolute_path"
REASON_QUERY_IN_PATH = "query_in_path"
REASON_DOT_SEGMENT = "dot_segment"
REASON_PATH_ESCAPE = "path_escape"
REASON_DESTINATION_CHANGED = "destination_changed"
REASON_INVALID_BASE_URL = "invalid_base_url"

#: How many times a percent-encoded component is decoded before it is judged.
#: Starlette already decodes the route parameter once; ``%252e%252e`` survives
#: that, so a single decode is not a bound. Four rounds is more than any real
#: client needs and still linear in the input length.
_DECODE_ROUNDS = 4

#: Characters that may not appear in a request-derived path component:
#: ``?``/``#`` would end the path and start a query/fragment (the backend gets a
#: different request than the one described), ``\\`` is treated as a separator
#: by some stacks, and a leading ``/`` produces ``//`` which no legitimate
#: caller needs.
_FORBIDDEN_LITERALS = frozenset("\\?#")

#: The fixed namespace the local backend's ``/v1`` routes live in.
BACKEND_V1_PREFIX = "/v1"


class UpstreamPathRejected(ValueError):
    """A request-derived value may not steer the upstream URL.

    ``reason`` is one of the ``REASON_*`` codes, ``http_status`` distinguishes
    the client's mistake (400) from a broken base URL in configuration (502),
    and the path is kept only as a bounded, sanitized preview — never as the
    raw value the client sent.
    """

    def __init__(self, reason: str, path: str = "", detail: str = "") -> None:
        self.reason = reason
        self.path = path
        self.detail = detail
        self.error = (
            "upstream_configuration"
            if reason == REASON_INVALID_BASE_URL
            else "invalid_request"
        )
        self.http_status = 502 if reason == REASON_INVALID_BASE_URL else 400
        message = f"path not allowed ({reason})"
        if detail:
            message += f" — {detail}"
        super().__init__(message)

    def to_detail(self) -> dict[str, str]:
        """JSON-ready error body for the caller's ``HTTPException``."""
        detail: dict[str, str] = {
            "error": self.error,
            "reason": "path_not_allowed",
            "path_reason": self.reason,
            "message": (
                "The request path is not a valid backend endpoint path; it is "
                "refused before it is spliced into the upstream URL."
                if self.error == "invalid_request"
                else "The configured backend URL cannot be parsed, so no path "
                "can be built from it."
            ),
        }
        if self.path:
            detail["path_preview"] = clean_log_value(self.path, limit=120)
        if self.detail:
            detail["detail"] = self.detail
        return detail


def _reject(reason: str, path: str, detail: str = "") -> "UpstreamPathRejected":
    return UpstreamPathRejected(reason=reason, path=path, detail=detail)


def _check_component(path: str) -> None:
    """Reject a component that is not a plain relative path remainder.

    Linear in ``len(path)``: two scans per decode round, no regular expression
    touching the client-controlled string (the #71/#72 lesson) and no recursion
    over it.
    """
    if not isinstance(path, str) or not path:
        raise _reject(REASON_EMPTY_PATH, "" if not isinstance(path, str) else path)

    if path.startswith("/"):
        raise _reject(REASON_ABSOLUTE_PATH, path, "an absolute path would replace the template path")

    for character in path:
        if character in _FORBIDDEN_LITERALS:
            raise _reject(
                REASON_QUERY_IN_PATH if character in "?#" else REASON_BACKSLASH,
                path,
                f"character {character!r} is not allowed in the path component",
            )
        if character < " " or character == "\x7f":
            raise _reject(
                REASON_CONTROL_CHARACTER,
                path,
                f"control character {character!r} is not allowed in the path component",
            )

    # Judge the value the *backend* may eventually decode, not only the one
    # Starlette handed us: `%2e%2e`, `%252e%252e` and `..%2f` all describe the
    # same traversal once the transport (and the server behind it) has decoded
    # them.
    candidate = path
    for _ in range(_DECODE_ROUNDS):
        _check_segments(candidate, path)
        if "%" not in candidate:
            break
        decoded = unquote(candidate)
        if decoded == candidate:  # a literal '%', not an escape
            break
        candidate = decoded
    else:
        # Still encoded after the allowed rounds: a component that keeps hiding
        # behind another layer of escaping cannot be bounded, so refuse it.
        raise _reject(
            REASON_PATH_ESCAPE,
            path,
            "the path component stays percent-encoded after repeated decoding",
        )


def _check_segments(candidate: str, original: str) -> None:
    """Reject dot segments, absolute paths and separators in *candidate*."""
    for character in candidate:
        if character in _FORBIDDEN_LITERALS:
            raise _reject(
                REASON_QUERY_IN_PATH if character in "?#" else REASON_BACKSLASH,
                original,
                f"decoded path contains {character!r}",
            )
    if candidate.startswith("/"):
        raise _reject(REASON_ABSOLUTE_PATH, original, "decoded path is absolute")
    for segment in candidate.split("/"):
        if segment in (".", ".."):
            raise _reject(
                REASON_DOT_SEGMENT,
                original,
                f"segment {segment!r} would leave the endpoint namespace",
            )


def _assemble(base_url: str, prefix: str, path: str) -> str:
    """The one place an upstream URL is formatted from its template.

    Deliberately not ``urllib.parse.urljoin``: ``urljoin`` resolves the
    component *as a URL*, so a component starting with ``//`` replaces the
    authority — the opposite of what a path remainder may do. Keeping the
    formatting in one named function is also what lets the invariant below be
    tested against exactly that mistake.
    """
    return f"{base_url.rstrip('/')}{prefix}/{path}"


def build_upstream_url(
    base_url: str, path: str, *, prefix: str = ""
) -> httpx.URL:
    """Build ``base_url + prefix + "/" + path`` and prove it stayed there.

    ``path`` is the request-derived component; ``prefix`` is a fixed path
    fragment inserted between the configured base and it (``"/v1"`` for the
    local backend, ``""`` for a base URL that already carries its API prefix).

    Raises :class:`UpstreamPathRejected`: the assembled URL must keep the
    base's scheme, host and port and must stay inside ``base.path + prefix``,
    otherwise it is refused. The returned URL is the exact object the caller
    sends, so the check and the request cannot disagree.
    """
    try:
        base = httpx.URL(base_url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise _reject(REASON_INVALID_BASE_URL, "", f"unparseable base URL ({exc})") from exc
    if not base.scheme or not base.host:
        raise _reject(
            REASON_INVALID_BASE_URL, "", "the configured base URL has no scheme/host"
        )

    _check_component(path)

    try:
        assembled = httpx.URL(_assemble(base_url, prefix, path))
    except httpx.InvalidURL as exc:  # pragma: no cover - component already checked
        raise _reject(REASON_PATH_ESCAPE, path, f"the assembled URL is invalid ({exc})") from exc

    if (assembled.scheme, assembled.host, assembled.port) != (
        base.scheme,
        base.host,
        base.port,
    ):
        raise _reject(
            REASON_DESTINATION_CHANGED,
            path,
            "the path component moved the destination",
        )

    # Compare *raw* paths on both sides. ``URL.path`` is percent-decoded, so a
    # configured base carrying an encoded segment (`http://host/llama%20cpp`)
    # would compare its decoded form against the assembled URL's encoded one and
    # refuse every legitimate request (PR-Piet, #75 round 1).
    expected_prefix = (
        base.raw_path.rstrip(b"/")
        + quote(prefix, safe="/").encode("ascii").rstrip(b"/")
        + b"/"
    )
    raw_path = assembled.raw_path
    if not raw_path.startswith(expected_prefix):
        raise _reject(
            REASON_PATH_ESCAPE,
            path,
            "the assembled path "
            f"{raw_path.decode('ascii', 'replace')!r} left "
            f"{expected_prefix.decode('ascii', 'replace')!r}",
        )
    if assembled.query or assembled.fragment:
        raise _reject(
            REASON_QUERY_IN_PATH,
            path,
            "the assembled URL carries a query or fragment",
        )
    return assembled


def backend_v1_url(base_url: str, path: str) -> str:
    """The local backend's ``/v1`` URL for *path*, or an ``HTTPException``.

    The single entry point for ``/v1/{path:path}`` handlers: a refused
    component becomes a typed ``400``/``502`` body instead of a request to a
    different endpoint, and the caller has no URL string of its own to get
    wrong. The URL is already normalized by :func:`build_upstream_url`, so the
    caller gets the validated absolute form as text — the same value the
    transport parses again, and the same shape the previous f-string produced
    for every approved endpoint.
    """
    try:
        return str(build_upstream_url(base_url, path, prefix=BACKEND_V1_PREFIX))
    except UpstreamPathRejected as exc:
        logger.warning("🛑 refused upstream path: %s", exc)
        raise HTTPException(status_code=exc.http_status, detail=exc.to_detail()) from exc
