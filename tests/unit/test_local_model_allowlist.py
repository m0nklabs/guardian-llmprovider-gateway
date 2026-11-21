"""Per-key model allowlist on the seven direct local inference paths (issue #117).

``allowed_models`` used to be enforced on cloud routing and model discovery
only: a key restricted to other public IDs could still reach queue admission
with a configured local model on every direct-local route. These tests pin the
guard that closes that gap:

* one shared decision point per handler family — ``route_v1_post`` for
  ``/v1/chat/completions``, ``/v1/completions``, ``/v1/embeddings``,
  ``/v1/messages`` and ``/v1/responses``; ``_enforce_local_model_allowed`` for
  ``/api/chat`` and ``/api/generate`` — taken on the public ID the client sent,
  before model resolution and before queue admission;
* the established ``403 {"error": "model_not_allowed"}`` body from the cloud
  resolver;
* exactly the behaviour that must be preserved: absent/``null``/empty stays
  unrestricted, matching is exact and case-sensitive (no aliases, prefixes or
  globbing), listed models still reach admission, cloud addresses keep the
  cloud resolver's own gate, and a request without ``model`` keeps its existing
  ``400`` because these endpoints resolve no implicit default.

Every dependency is an in-memory fake. The queue, lifecycle, manager and
transport hooks are fail-if-called sentinels, so a passing denial test proves
nothing below the decision ran. No backend, GPU, network, configuration file,
key store or live service is touched.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.gateway import routing as gw_routing
from app.local_inference import ollama as local_ollama
from app.proxy import auth as auth_mod
from app.proxy import server

LOCAL = "synthetic-local-model"
LOCAL_ALIAS = "synthetic-local-alias"
CLOUD_ONLY = "openrouter/z-ai/glm-5.3-flash"
V1_ROUTES = ("chat/completions", "completions", "embeddings", "messages", "responses")
OLLAMA_ROUTES = ("/api/chat", "/api/generate")

#: Distinguishes "no ``allowed_models`` field on the key" from "empty list".
_ABSENT = object()


# ── fakes and sentinels ────────────────────────────────────────────────


class QueueBoundaryReached(Exception):
    """Raised by the queue sentinel after it recorded the admission call."""


class _ForbiddenManager:
    """``_model_manager`` sentinel: any resolution call means denial came late."""

    def __getattr__(self, name):
        raise AssertionError(
            f"_model_manager.{name} must not run before the allowlist decision"
        )


class _ForbiddenAsyncClient:
    """Any HTTP client construction means the request reached the transport."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("the request must not open a backend transport")


def _forbidden(*args, **kwargs):
    raise AssertionError("the request must not pass the allowlist decision")


def _stub_manager():
    return SimpleNamespace(get_current_model=AsyncMock(return_value=LOCAL))


def _auth_ctx(allowed):
    ctx: dict = {"key_fingerprint": "test-key"}
    if allowed is not _ABSENT:
        ctx["allowed_models"] = allowed
    return ctx


class _V1Request:
    """The request parts ``route_v1_post`` touches before queue admission."""

    def __init__(self, route: str, payload: dict, allowed) -> None:
        self.headers = {"Content-Type": "application/json"}
        self.state = SimpleNamespace(auth_context=_auth_ctx(allowed))
        self.url = SimpleNamespace(path=f"/v1/{route}")
        self.method = "POST"
        self.payload = payload

    async def body(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _OllamaRequest:
    """The request parts both Ollama bridges touch before queue admission."""

    def __init__(self, path: str, payload: dict, allowed) -> None:
        self.headers = {"Content-Type": "application/json"}
        self.state = SimpleNamespace(auth_context=_auth_ctx(allowed))
        self.url = SimpleNamespace(path=path)
        self.method = "POST"
        self.payload = payload

    async def json(self):
        return self.payload


def _payload(route: str, model: str, *, stream: bool = False) -> dict:
    """A minimal valid body per endpoint; only ``model`` drives the guard."""
    body: dict = {"model": model}
    if route in ("chat/completions", "messages"):
        body["messages"] = [{"role": "user", "content": "synthetic"}]
        body["stream"] = stream
    elif route == "completions":
        body["prompt"] = "synthetic"
        body["stream"] = stream
    elif route == "responses":
        body["input"] = [{"role": "user", "content": "synthetic"}]
        body["stream"] = stream
    else:  # embeddings
        body["input"] = "synthetic"
    return body


# ── post-decision sentinels: denial must happen first ──────────────────


def _v1_denial_patches():
    """Everything after the decision is a sentinel; the guard is the real one."""
    return (
        patch.object(gw_routing, "_is_cloud_or_guardian_route", lambda name: False),
        patch.object(gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(gw_routing, "_model_manager", _ForbiddenManager()),
        patch.object(gw_routing, "_resolve_or_reject_inference_model", _forbidden),
        patch.object(gw_routing, "_resolve_inference_model", _forbidden),
        patch.object(gw_routing, "_begin_queued_request", _forbidden),
        patch.object(gw_routing, "_run_guardian_operation", _forbidden),
        patch.object(gw_routing, "_preflight_multimodal_request", _forbidden),
        patch.object(gw_routing, "_setup_cloud_capture", _forbidden),
        patch.object(gw_routing, "_forward_to_cloud_provider", _forbidden),
        patch("httpx.AsyncClient", _ForbiddenAsyncClient),
    )


def _v1_admission_patches(reached: list[str]):
    """Everything up to (and including) a recording queue sentinel."""

    async def stop_before_admission(request, client_id, model):
        reached.append(model)
        raise QueueBoundaryReached("stopped at queue admission")

    return (
        patch.object(gw_routing, "_is_cloud_or_guardian_route", lambda name: False),
        patch.object(gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
        patch.object(gw_routing, "_model_manager", _stub_manager()),
        patch.object(gw_routing, "_resolve_or_reject_inference_model", lambda raw, current: raw),
        patch.object(gw_routing, "_messages_contain_image_input", lambda messages: False),
        patch.object(gw_routing, "responses_contains_image_input", lambda value: False),
        patch.object(gw_routing, "_apply_anthropic_thinking_to_llama_params", lambda body: None),
        patch.object(gw_routing, "_apply_request_reasoning_defaults", lambda *a: None),
        patch.object(gw_routing, "_validate_grammar_field", None),
        patch.object(
            gw_routing, "_sanitize_messages_for_qwen_chat_template", lambda messages: messages
        ),
        patch.object(gw_routing, "_begin_queued_request", stop_before_admission),
        patch("httpx.AsyncClient", _ForbiddenAsyncClient),
    )


def _ollama_denial_patches():
    return (
        patch.object(local_ollama, "_is_cloud_or_guardian_route", lambda name: False),
        patch.object(local_ollama, "_model_manager", _ForbiddenManager()),
        patch.object(local_ollama, "_resolve_or_reject_inference_model", _forbidden),
        patch.object(local_ollama, "_begin_queued_request", _forbidden),
        patch.object(local_ollama, "_run_guardian_operation", _forbidden),
        patch.object(local_ollama, "_forward_to_cloud_provider", _forbidden),
        patch("httpx.AsyncClient", _ForbiddenAsyncClient),
    )


def _ollama_admission_patches(reached: list[str]):
    async def stop_before_admission(request, client_id, model):
        reached.append(model)
        raise QueueBoundaryReached("stopped at queue admission")

    return (
        patch.object(local_ollama, "_is_cloud_or_guardian_route", lambda name: False),
        patch.object(local_ollama, "_model_manager", _stub_manager()),
        patch.object(local_ollama, "_resolve_or_reject_inference_model", lambda raw, current: raw),
        patch.object(local_ollama, "_begin_queued_request", stop_before_admission),
        patch("httpx.AsyncClient", _ForbiddenAsyncClient),
    )


# ── entry points (real route wrappers, real handlers) ──────────────────


async def _call_v1(route: str, request: _V1Request):
    return await server.proxy_v1_post(route, request, client_id="test")


async def _call_ollama(path: str, request: _OllamaRequest):
    if path == "/api/chat":
        return await server.proxy_chat_ollama(request, client_id="test")
    return await server.proxy_generate_ollama(request, client_id="test")


def _assert_model_not_allowed(exc: HTTPException, model: str) -> None:
    """Pin the established cloud-resolver shape exactly."""
    assert exc.status_code == 403
    assert exc.detail == {
        "error": "model_not_allowed",
        "model": model,
        "message": "this Guardian key is restricted to a fixed model list",
    }


# ── /v1: unlisted local model is denied before anything runs ───────────


class TestV1DirectLocalPaths:
    @pytest.mark.parametrize("route", V1_ROUTES)
    @pytest.mark.parametrize("stream", [False, True])
    async def test_unlisted_local_model_is_denied_before_resolution_and_admission(
        self, route, stream
    ):
        request = _V1Request(route, _payload(route, LOCAL, stream=stream), [CLOUD_ONLY])
        with ExitStack() as stack:
            for cm in _v1_denial_patches():
                stack.enter_context(cm)
            with pytest.raises(HTTPException) as excinfo:
                await _call_v1(route, request)
        _assert_model_not_allowed(excinfo.value, LOCAL)

    @pytest.mark.parametrize("route", V1_ROUTES)
    async def test_listed_local_model_reaches_queue_admission(self, route):
        reached: list[str] = []
        request = _V1Request(route, _payload(route, LOCAL), [LOCAL])
        with ExitStack() as stack:
            for cm in _v1_admission_patches(reached):
                stack.enter_context(cm)
            with pytest.raises(QueueBoundaryReached):
                await _call_v1(route, request)
        assert reached == [LOCAL]

    @pytest.mark.parametrize("route", V1_ROUTES)
    @pytest.mark.parametrize("allowed", [None, [], _ABSENT], ids=["null", "empty", "absent"])
    async def test_unrestricted_key_still_reaches_queue_admission(self, route, allowed):
        reached: list[str] = []
        request = _V1Request(route, _payload(route, LOCAL), allowed)
        with ExitStack() as stack:
            for cm in _v1_admission_patches(reached):
                stack.enter_context(cm)
            with pytest.raises(QueueBoundaryReached):
                await _call_v1(route, request)
        assert reached == [LOCAL]


# ── /api/chat and /api/generate ────────────────────────────────────────


class TestOllamaDirectLocalPaths:
    @pytest.mark.parametrize("path", OLLAMA_ROUTES)
    @pytest.mark.parametrize("stream", [False, True])
    async def test_unlisted_local_model_is_denied_before_resolution_and_admission(
        self, path, stream
    ):
        payload = {"model": LOCAL, "stream": stream}
        payload.update(
            {"messages": [{"role": "user", "content": "synthetic"}]}
            if path == "/api/chat"
            else {"prompt": "synthetic"}
        )
        request = _OllamaRequest(path, payload, [CLOUD_ONLY])
        with ExitStack() as stack:
            for cm in _ollama_denial_patches():
                stack.enter_context(cm)
            with pytest.raises(HTTPException) as excinfo:
                await _call_ollama(path, request)
        _assert_model_not_allowed(excinfo.value, LOCAL)

    @pytest.mark.parametrize("path", OLLAMA_ROUTES)
    async def test_listed_local_model_reaches_queue_admission(self, path):
        reached: list[str] = []
        payload = {"model": LOCAL}
        payload.update(
            {"messages": [{"role": "user", "content": "synthetic"}]}
            if path == "/api/chat"
            else {"prompt": "synthetic"}
        )
        request = _OllamaRequest(path, payload, [LOCAL])
        with ExitStack() as stack:
            for cm in _ollama_admission_patches(reached):
                stack.enter_context(cm)
            with pytest.raises(QueueBoundaryReached):
                await _call_ollama(path, request)
        assert reached == [LOCAL]

    @pytest.mark.parametrize("path", OLLAMA_ROUTES)
    @pytest.mark.parametrize("allowed", [None, [], _ABSENT], ids=["null", "empty", "absent"])
    async def test_unrestricted_key_still_reaches_queue_admission(self, path, allowed):
        reached: list[str] = []
        payload = {"model": LOCAL}
        payload.update(
            {"messages": [{"role": "user", "content": "synthetic"}]}
            if path == "/api/chat"
            else {"prompt": "synthetic"}
        )
        request = _OllamaRequest(path, payload, allowed)
        with ExitStack() as stack:
            for cm in _ollama_admission_patches(reached):
                stack.enter_context(cm)
            with pytest.raises(QueueBoundaryReached):
                await _call_ollama(path, request)
        assert reached == [LOCAL]


# ── preserved matching semantics ───────────────────────────────────────


@pytest.mark.parametrize(
    "requested,grants",
    [
        (LOCAL.upper(), [LOCAL]),  # case-sensitive
        (LOCAL, [LOCAL.upper()]),  # grants are compared verbatim
        (LOCAL_ALIAS, [LOCAL]),  # alias is not the canonical public ID
        (LOCAL, ["synthetic-local-*"]),  # no globbing
        (LOCAL, [f"local/{LOCAL}"]),  # no provider prefix stripping
    ],
)
async def test_local_gate_matches_the_exact_public_id(requested, grants):
    request = _V1Request("chat/completions", _payload("chat/completions", requested), grants)
    with ExitStack() as stack:
        for cm in _v1_denial_patches():
            stack.enter_context(cm)
        with pytest.raises(HTTPException) as excinfo:
            await _call_v1("chat/completions", request)
    _assert_model_not_allowed(excinfo.value, requested)


async def test_granted_alias_is_matched_as_sent_and_resolved_afterwards():
    """The decision is on the sent ID; alias -> canonical happens afterwards."""
    reached: list[str] = []
    request = _V1Request("chat/completions", _payload("chat/completions", LOCAL_ALIAS), [LOCAL_ALIAS])
    with ExitStack() as stack:
        for cm in _v1_admission_patches(reached):
            stack.enter_context(cm)
        stack.enter_context(
            patch.object(
                gw_routing, "_resolve_or_reject_inference_model", lambda raw, current: LOCAL
            )
        )
        with pytest.raises(QueueBoundaryReached):
            await _call_v1("chat/completions", request)
    assert reached == [LOCAL]


async def test_cloud_address_keeps_the_cloud_resolver_gate():
    """A cloud address is never denied by the local guard.

    The cloud resolver owns that decision and checks ``cloud_gateway_access``
    before ``allowed_models``; this guard must not preempt it (documented
    precedence in docs/KEY_MODEL_ALLOWLIST.md).
    """
    forwarded: list[str] = []

    async def forward(**kwargs):
        forwarded.append(kwargs["model_name"])
        return "cloud-response"

    request = _V1Request("chat/completions", _payload("chat/completions", CLOUD_ONLY), [LOCAL])
    request.state.auth_context["cloud_gateway_access"] = False
    with ExitStack() as stack:
        for cm in (
            patch.object(gw_routing, "_is_cloud_or_guardian_route", lambda name: True),
            patch.object(gw_routing, "_set_request_usage_metadata", lambda *a, **k: None),
            patch.object(gw_routing, "_enforce_key_model_allowed", _forbidden),
            patch.object(gw_routing, "_model_manager", _stub_manager()),
            patch.object(gw_routing, "_resolve_or_reject_inference_model", lambda raw, current: raw),
            patch.object(gw_routing, "_messages_contain_image_input", lambda messages: False),
            patch.object(
                gw_routing,
                "_setup_cloud_capture",
                lambda request, client_id, **kwargs: (None, None, "req-cloud", 0.0),
            ),
            patch.object(gw_routing, "_forward_to_cloud_provider", forward),
            patch("httpx.AsyncClient", _ForbiddenAsyncClient),
        ):
            stack.enter_context(cm)
        assert await _call_v1("chat/completions", request) == "cloud-response"
    assert forwarded == [CLOUD_ONLY]


# ── implicit/default model resolution (absent model, ``auto``) ─────────


class TestImplicitDefaultModel:
    """Contract chosen for a request that names no model.

    ``route_v1_post``, ``chat_ollama`` and ``generate_ollama`` resolve no
    implicit default: a body without ``model`` keeps its pre-existing ``400``
    before any model selection, so no unlisted default can be served and the
    allowlist decision never has to fabricate an ID. The gateway-resolved
    default sentinel ``auto`` *is* a public ID and is matched exactly, so a
    restricted key that does not list it is denied before the resolver picks
    the model it would actually serve.
    """

    async def test_v1_request_without_model_keeps_the_existing_400(self):
        request = _V1Request("chat/completions", {"messages": []}, [CLOUD_ONLY])
        with ExitStack() as stack:
            for cm in _v1_denial_patches():
                stack.enter_context(cm)
            with pytest.raises(HTTPException) as excinfo:
                await _call_v1("chat/completions", request)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail["reason"] == "model_not_specified"

    @pytest.mark.parametrize("path", OLLAMA_ROUTES)
    async def test_ollama_request_without_model_keeps_the_existing_400(self, path):
        request = _OllamaRequest(path, {"prompt": "synthetic"}, [CLOUD_ONLY])
        with ExitStack() as stack:
            for cm in _ollama_denial_patches():
                stack.enter_context(cm)
            with pytest.raises(HTTPException) as excinfo:
                await _call_ollama(path, request)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "Model not specified"

    async def test_auto_default_is_denied_unless_its_public_id_is_listed(self):
        request = _V1Request("chat/completions", _payload("chat/completions", "auto"), [LOCAL])
        with ExitStack() as stack:
            for cm in _v1_denial_patches():
                stack.enter_context(cm)
            with pytest.raises(HTTPException) as excinfo:
                await _call_v1("chat/completions", request)
        _assert_model_not_allowed(excinfo.value, "auto")

    async def test_granted_auto_default_reaches_queue_admission(self):
        reached: list[str] = []
        request = _V1Request("chat/completions", _payload("chat/completions", "auto"), ["auto"])
        with ExitStack() as stack:
            for cm in _v1_admission_patches(reached):
                stack.enter_context(cm)
            with pytest.raises(QueueBoundaryReached):
                await _call_v1("chat/completions", request)
        assert reached == ["auto"]


# ── the shared predicate and its error shape ───────────────────────────


class TestEnforceKeyModelAllowed:
    def test_unlisted_model_raises_the_established_shape(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_context={"allowed_models": [CLOUD_ONLY]}))
        with pytest.raises(HTTPException) as excinfo:
            auth_mod.enforce_key_model_allowed(request, LOCAL)
        _assert_model_not_allowed(excinfo.value, LOCAL)

    def test_listed_model_passes(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_context={"allowed_models": [LOCAL]}))
        auth_mod.enforce_key_model_allowed(request, LOCAL)

    @pytest.mark.parametrize("ctx", [None, {}, {"allowed_models": None}, {"allowed_models": []}])
    def test_unrestricted_context_passes(self, ctx):
        request = SimpleNamespace(state=SimpleNamespace(auth_context=ctx))
        auth_mod.enforce_key_model_allowed(request, LOCAL)

    def test_missing_auth_context_is_unrestricted(self):
        request = SimpleNamespace(state=SimpleNamespace())
        auth_mod.enforce_key_model_allowed(request, LOCAL)
