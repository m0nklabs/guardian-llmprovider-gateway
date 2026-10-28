"""Issue #75: what the destination allowlist cannot see, and what bounds it.

The four ``py/partial-ssrf`` sites are all one shape: a client-supplied
``{path:path}`` route parameter is spliced onto a configured base URL
(``f"{_llama_server_url}/v1/{path}"``). ``DestinationGuardTransport`` decides
the *destination* of every hop — scheme, host, port, resolved address — and that
is the only control over where the request goes. It is blind to the rest of the
URL by design (§2.2: "it deliberately does not model HTTP method or path policy
beyond the URL"), and ``httpx`` normalizes dot segments before the request
leaves the process.

These tests pin both halves:

* the destination is not steerable from ``path`` at all, and when the *base* is
  not approved the guard refuses with its own reason (the positive control that
  the guard is really in the path);
* the path *is* steerable, the guard accepts the escape because the host is
  still approved, and :func:`app.upstream_url.build_upstream_url` is what
  refuses it — with the path-specific reason.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from app.destination_policy import (
    DestinationDenied,
    DestinationGuardTransport,
    DestinationPolicy,
)
from app.upstream_url import (
    REASON_ABSOLUTE_PATH,
    REASON_BACKSLASH,
    REASON_CONTROL_CHARACTER,
    REASON_DESTINATION_CHANGED,
    REASON_DOT_SEGMENT,
    REASON_EMPTY_PATH,
    REASON_INVALID_BASE_URL,
    REASON_PATH_ESCAPE,
    REASON_QUERY_IN_PATH,
    UpstreamPathRejected,
    backend_v1_url,
    build_upstream_url,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
BACKEND = "http://127.0.0.1:11440"


def _approved_policy() -> DestinationPolicy:
    """A policy that approves exactly the local backend used in these tests."""
    return DestinationPolicy.from_config(
        {
            "destination_policy": {
                "schemes": ["http"],
                "allow": [{"host": "127.0.0.1", "ports": [11440]}],
            }
        }
    )


# ── The builder: what it accepts ──────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("chat/completions", "http://127.0.0.1:11440/v1/chat/completions"),
        ("models", "http://127.0.0.1:11440/v1/models"),
        ("models/gpt-4.1", "http://127.0.0.1:11440/v1/models/gpt-4.1"),
        ("props", "http://127.0.0.1:11440/v1/props"),
        ("embeddings", "http://127.0.0.1:11440/v1/embeddings"),
        # A literal '%' is not an escape: it must not be refused as one.
        ("models/100%25", "http://127.0.0.1:11440/v1/models/100%25"),
    ],
)
def test_legitimate_v1_paths_still_build_the_same_url(path: str, expected: str) -> None:
    """Every approved endpoint keeps working, byte-for-byte (no behaviour change)."""
    assert str(build_upstream_url(BACKEND, path, prefix="/v1")) == expected
    assert backend_v1_url(BACKEND, path) == expected


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:11440/llama%20cpp",
        "http://127.0.0.1:11440/llama%20cpp/",
        "http://127.0.0.1:11440/v1/llama%20cpp",
    ],
)
def test_an_encoded_base_prefix_still_serves_legitimate_paths(base: str) -> None:
    """A percent-encoded segment in the *configured* base is a valid prefix.

    ``URL.path`` is decoded while ``URL.raw_path`` is not, so comparing the
    decoded base prefix against the encoded assembled path refused every
    request for such a base (PR-Piet, #75 round 1). Both sides are compared raw.
    """
    expected_tail = "/v1/models"
    built = build_upstream_url(base, "models", prefix="/v1")
    assert str(built).endswith(expected_tail)
    assert built.raw_path.endswith(b"/llama%20cpp" + expected_tail.encode())
    # The bound is unchanged for a hostile component on the same base.
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url(base, "../props", prefix="/v1")
    assert excinfo.value.reason == REASON_DOT_SEGMENT


def test_the_backend_url_is_text_the_transport_accepts() -> None:
    """``backend_v1_url`` returns exactly the URL string the call sites send."""
    url = backend_v1_url(BACKEND, "chat/completions")
    assert isinstance(url, str)
    assert httpx.URL(url).raw_path == b"/v1/chat/completions"


# ── The builder: what it refuses, and why ─────────────────────────────


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", REASON_EMPTY_PATH),
        # The escape httpx itself performs: `/v1/../props` *is* `/props`.
        ("../props", REASON_DOT_SEGMENT),
        ("a/../../slots", REASON_DOT_SEGMENT),
        # Starlette decodes once; these are what it hands the handler, and what
        # the backend decodes again.
        ("..%2fprops", REASON_DOT_SEGMENT),
        ("%2e%2e/props", REASON_DOT_SEGMENT),
        # One layer deeper than Starlette's single decode.
        ("%252e%252e/props", REASON_DOT_SEGMENT),
        ("..%5cprops", REASON_BACKSLASH),
        ("..\\props", REASON_BACKSLASH),
        ("\\@evil.example/x", REASON_BACKSLASH),
        # An absolute path would discard the `/v1` template segment.
        ("/props", REASON_ABSOLUTE_PATH),
        ("//evil.example/x", REASON_ABSOLUTE_PATH),
        # A query/fragment ends the path and changes what the backend receives.
        ("models?limit=999", REASON_QUERY_IN_PATH),
        ("models#@evil.example", REASON_QUERY_IN_PATH),
        ("models\x00", REASON_CONTROL_CHARACTER),
        ("models\tx", REASON_CONTROL_CHARACTER),
    ],
)
def test_hostile_components_are_refused_with_a_specific_reason(
    path: str, reason: str
) -> None:
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url(BACKEND, path, prefix="/v1")
    assert excinfo.value.reason == reason
    # The rejection is the client's mistake, not a server fault.
    assert excinfo.value.http_status == 400


def test_a_component_that_never_stops_decoding_is_refused() -> None:
    """Bounded decoding: an escape buried five layers deep is refused.

    ``%252525252e%252525252e`` decodes to ``.....`` one round at a time; the
    decoder gives each component four rounds and refuses what is still hidden
    after that, instead of judging the surface form.
    """
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url(BACKEND, "%252525252e%252525252e/props", prefix="/v1")
    assert excinfo.value.reason == REASON_PATH_ESCAPE


def test_the_rejected_value_is_not_echoed_raw() -> None:
    """A refusal is a bounded preview, not the client's string in a body/log."""
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url(BACKEND, "../props\nX-Injected: 1", prefix="/v1")
    detail = excinfo.value.to_detail()
    assert detail["reason"] == "path_not_allowed"
    # The control character is the first thing wrong with it, and it is what
    # the preview must not carry through.
    assert detail["path_reason"] == REASON_CONTROL_CHARACTER
    json.dumps(detail)  # JSON-safe, no exception object leaking into a response
    # The line break — the header-injection vector — is gone, and the preview is
    # bounded rather than an unbounded echo of client input.
    assert "\n" not in detail["path_preview"] and "\r" not in detail["path_preview"]
    assert len(detail["path_preview"]) <= 120


def test_the_second_layer_refuses_an_escape_the_first_layer_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-parsing the assembled URL is real defence, not decoration.

    With the component check bypassed, the normalized path still has to stay
    inside the template — the same invariant, applied to the value actually
    sent.
    """
    from app import upstream_url

    monkeypatch.setattr(upstream_url, "_check_component", lambda path: None)
    with pytest.raises(UpstreamPathRejected) as excinfo:
        upstream_url.build_upstream_url(BACKEND, "../props", prefix="/v1")
    assert excinfo.value.reason == REASON_PATH_ESCAPE
    assert "/v1/" in excinfo.value.detail


def test_the_invariant_catches_a_join_that_would_move_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the join is ever "improved" to ``urljoin``, the invariant trips.

    ``urljoin`` treats the component as a URL reference, so ``//host/x``
    replaces the authority. The formatter is one named function for exactly
    this reason: the test installs that mistake and the destination check
    catches it before anything is sent.
    """
    from urllib.parse import urljoin

    from app import upstream_url

    monkeypatch.setattr(upstream_url, "_check_component", lambda path: None)
    monkeypatch.setattr(
        upstream_url,
        "_assemble",
        lambda base_url, prefix, path: urljoin(f"{base_url.rstrip('/')}{prefix}/", path),
    )
    with pytest.raises(UpstreamPathRejected) as excinfo:
        upstream_url.build_upstream_url(BACKEND, "//evil.example/x", prefix="/v1")
    assert excinfo.value.reason == REASON_DESTINATION_CHANGED


def test_an_unparseable_base_is_a_configuration_error_not_a_client_error() -> None:
    """The other half of #75: a broken base URL cannot be blamed on the caller."""
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url(None, "models", prefix="/v1")  # type: ignore[arg-type]
    assert excinfo.value.reason == REASON_INVALID_BASE_URL
    assert excinfo.value.http_status == 502
    assert excinfo.value.to_detail()["error"] == "upstream_configuration"


def test_backend_v1_url_raises_the_http_error_the_handlers_return() -> None:
    """The call sites get one typed 400 instead of building a bad URL."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        backend_v1_url(BACKEND, "../slots")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error": "invalid_request",
        "reason": "path_not_allowed",
        "path_reason": REASON_DOT_SEGMENT,
        "message": (
            "The request path is not a valid backend endpoint path; it is "
            "refused before it is spliced into the upstream URL."
        ),
        "path_preview": "../slots",
        "detail": "segment '..' would leave the endpoint namespace",
    }


# ── What the attacker controls: the path, never the destination ───────


HOSTILE_COMPONENTS = [
    "../props",
    "../../etc/passwd",
    "..%2f..%2fprops",
    "@evil.example/x",
    "//evil.example/x",
    "\\\\evil.example\\x",
    "x#@evil.example",
    "a/../../../../slots",
    "%2e%2e/%2e%2e/props",
    "evil.example:8080/x",
]


def test_a_hostile_component_can_never_move_the_destination() -> None:
    """Either the builder refuses, or the authority is still the configured one.

    This is the exact claim the ``py/partial-ssrf`` alert leaves open ("part of
    the URL is influenced"): for these sites the influenced part is the path
    only. Both outcomes are safe, and neither is decided by the guard.
    """
    configured = httpx.URL(BACKEND)
    for component in HOSTILE_COMPONENTS:
        try:
            built = build_upstream_url(BACKEND, component, prefix="/v1")
        except UpstreamPathRejected:
            continue
        assert (built.scheme, built.host, built.port) == (
            configured.scheme,
            configured.host,
            configured.port,
        ), component
        assert built.raw_path.startswith(b"/v1/")


def test_a_base_with_userinfo_is_left_to_the_destination_policy() -> None:
    """The builder bounds the component; the authority stays the guard's job.

    A base URL carrying ``user@`` is a *configuration* destination question, not
    a path question: the builder keeps the authority it was given, and
    ``DestinationPolicy`` refuses the userinfo with ``userinfo_not_allowed``.
    """
    hijacked = "http://127.0.0.1:11440@evil.example"
    built = build_upstream_url(hijacked, "models", prefix="/v1")
    assert built.host == "evil.example"

    with pytest.raises(DestinationDenied) as excinfo:
        _approved_policy().check_structure(str(built))
    assert excinfo.value.reason == "userinfo_not_allowed"


# ── The guard is real, and blind to the path ──────────────────────────


@pytest.mark.asyncio
async def test_the_guard_refuses_an_unapproved_base_with_its_own_reason() -> None:
    """Positive control: the transport really is the destination control.

    ``path`` cannot produce this refusal — only the configuration can — which is
    why the guard, not the builder, owns the destination half of #75.
    """
    policy = _approved_policy()
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    transport = DestinationGuardTransport(
        policy, purpose="test", inner=httpx.MockTransport(handler)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DestinationDenied) as excinfo:
            await client.get("http://169.254.169.254/latest/meta-data/")
    assert excinfo.value.reason == "metadata_host_blocked"
    assert seen == [], "a refused hop must not reach the network"

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DestinationDenied) as excinfo:
            await client.get("http://127.0.0.1:11437/props")
    assert excinfo.value.reason == "port_not_allowed"
    assert seen == []


@pytest.mark.asyncio
async def test_the_guard_accepts_the_escape_the_builder_refuses() -> None:
    """The finding, stated as behaviour: `/v1/../props` is an approved hop.

    ``httpx`` normalizes the dot segment before the transport sees the request,
    so the transport validates ``/props`` on the approved host and allows it.
    Nothing below the call site can refuse this — the builder must, and does.
    """
    escaped = str(httpx.URL(f"{BACKEND}/v1/../props"))
    assert escaped.endswith("/props") and "/v1/" not in escaped

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"slots": []})

    transport = DestinationGuardTransport(
        _approved_policy(), purpose="test", inner=httpx.MockTransport(handler)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(escaped)

    assert response.status_code == 200
    assert seen == ["/props"], "the guard allowed the request outside /v1"

    # The same component, through the builder, never becomes a request.
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url(BACKEND, "../props", prefix="/v1")
    assert excinfo.value.reason == REASON_DOT_SEGMENT
    assert seen == ["/props"], "the builder must not send anything"


def test_the_provider_base_shape_is_bounded_by_the_same_builder() -> None:
    """``build_upstream_url`` also covers a base that already carries its prefix.

    The cloud forward path (``ProviderRegistry.build_forward_url``) has the same
    shape and is *not* one of the four #75 alerts; the primitive is reusable for
    it without a second implementation (see the issue's follow-up note).
    """
    url = build_upstream_url("https://openrouter.ai/api/v1", "chat/completions")
    assert str(url) == "https://openrouter.ai/api/v1/chat/completions"
    with pytest.raises(UpstreamPathRejected) as excinfo:
        build_upstream_url("https://openrouter.ai/api/v1", "../models")
    assert excinfo.value.reason == REASON_DOT_SEGMENT


# ── Recurrence guard: the four sites cannot come back by copy-paste ────


#: httpx client methods/shortcuts that take a URL, plus the request builders.
OUTBOUND_URL_CALLS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
        "send",
        "stream",
        "build_request",
    }
)

#: The one module allowed to compose an upstream URL from a component.
URL_BUILDER = "app/upstream_url.py"


def _interpolates_request_path(node: ast.JoinedStr) -> bool:
    """True when the f-string splices a request-derived path component in."""
    if not any(isinstance(value, ast.FormattedValue) for value in node.values):
        return False
    for value in node.values:
        if not isinstance(value, ast.FormattedValue):
            continue
        expression = value.value
        name = None
        if isinstance(expression, ast.Name):
            name = expression.id
        elif isinstance(expression, ast.Attribute):
            name = expression.attr
        if name is not None and (name == "path" or name.endswith("_path")):
            return True
    return False


def outbound_path_interpolations(source: str, path: str) -> list[tuple[int, str]]:
    """``(line, url expression)`` for every outbound call building its own URL."""
    tree = ast.parse(source, filename=path)
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in OUTBOUND_URL_CALLS:
            continue
        arguments = list(node.args) + [keyword.value for keyword in node.keywords]
        for argument in arguments:
            for candidate in ast.walk(argument):
                if isinstance(candidate, ast.JoinedStr) and _interpolates_request_path(
                    candidate
                ):
                    findings.append((candidate.lineno, ast.unparse(candidate)))
    return findings


def test_no_outbound_call_splices_a_request_path_into_its_url() -> None:
    """The class of bug behind #75 is a CI failure, not a review hope.

    Use ``app.upstream_url.backend_v1_url``/``build_upstream_url``: the URL is
    then built from a template plus a validated component, and the assembled
    value is re-parsed before it is sent. Configuration-derived interpolations
    (``self.server_url``, ``route['base_url']``, …) are not request input and
    stay out of this rule's scope.
    """
    offenders: list[str] = []
    for module in sorted(APP_ROOT.rglob("*.py")):
        rel = str(module.relative_to(REPO_ROOT))
        if rel == URL_BUILDER:
            continue
        for line, expression in outbound_path_interpolations(
            module.read_text(encoding="utf-8"), rel
        ):
            offenders.append(f"{rel}:{line} {expression}")
    assert not offenders, (
        "Outbound URL built by interpolating a request path (issue #75) — use "
        "app.upstream_url.backend_v1_url(...) or build_upstream_url(...):\n  "
        + "\n  ".join(offenders)
    )


PLANTED = '''
import httpx

async def passthrough(client, path):
    return await client.post(f"{BACKEND}/v1/{path}", content=b"")

async def streaming(client, path):
    return client.build_request("POST", f"{BACKEND}/v1/{path}")

async def aliased(client, request_path):
    return await client.get(f"{BACKEND}/{request_path}")

async def legitimate(client, path):
    from app.upstream_url import backend_v1_url
    return await client.post(backend_v1_url(BACKEND, path))

async def config_only(client):
    return await client.get(f"{self.server_url}/health")
'''


def test_the_scanner_detects_the_pre_fix_sites() -> None:
    """Not vacuous: the exact pre-#75 expressions are reported, by line."""
    found = {line for line, _ in outbound_path_interpolations(PLANTED, "planted.py")}
    assert found == {5, 8, 11}, sorted(found)


def test_the_scanner_leaves_the_guarded_and_config_shapes_alone() -> None:
    """The builder call and a configuration URL are not findings."""
    expressions = [
        expression for _, expression in outbound_path_interpolations(PLANTED, "x.py")
    ]
    assert all("path}" in expression for expression in expressions), expressions
    assert not any("server_url" in expression for expression in expressions)
