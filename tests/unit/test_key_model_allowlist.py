"""Per-key model allowlist (``allowed_models`` in guardian.keys.yaml).

A key without ``allowed_models`` keeps its full reach; a key with a non-empty
list may only name those exact addresses, both in discovery and on the request
path. These tests pin the three enforcement points:

* ``app.proxy.auth`` — normalisation and the shared predicate
* ``app.cloud_inference.routing`` — the request-path 403
* ``app.gateway.model_discovery`` — the discovery filter and the 404
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.cloud_inference import routing
from app.gateway import model_discovery, model_endpoints
from app.proxy import auth as auth_mod

ALLOWED = [
    "openrouter/z-ai/glm-5.3-flash",
    "openrouter/deepseek/deepseek-v4.1-flash-1017",
]


# ── auth: normalisation ────────────────────────────────────────────────


class TestNormalizeKeyEntry:
    def test_absent_allowlist_stays_absent(self):
        entry = auth_mod._normalize_key_entry("k_abc", {"name": "plain"})
        assert "allowed_models" not in entry

    def test_list_is_preserved(self):
        entry = auth_mod._normalize_key_entry("k_abc", {"name": "x", "allowed_models": ALLOWED})
        assert entry["allowed_models"] == ALLOWED

    def test_scalar_is_wrapped_into_a_list(self):
        entry = auth_mod._normalize_key_entry(
            "k_abc", {"name": "x", "allowed_models": "openrouter/z-ai/glm-5.3-flash"}
        )
        assert entry["allowed_models"] == ["openrouter/z-ai/glm-5.3-flash"]

    def test_blank_entries_are_dropped(self):
        entry = auth_mod._normalize_key_entry(
            "k_abc", {"name": "x", "allowed_models": ["  ", "openrouter/z-ai/glm-5.3-flash", ""]}
        )
        assert entry["allowed_models"] == ["openrouter/z-ai/glm-5.3-flash"]

    @pytest.mark.parametrize("allowed", [None, [], [" ", ""]])
    def test_none_empty_and_blank_only_remain_unrestricted(self, allowed):
        entry = auth_mod._normalize_key_entry("k_abc", {"allowed_models": allowed})
        assert not entry["allowed_models"]
        assert auth_mod.key_allows_model(entry, "any/model") is True

    def test_cloud_gateway_access_default_is_unchanged(self):
        entry = auth_mod._normalize_key_entry("k_abc", {"name": "x"})
        assert entry["cloud_gateway_access"] is True


# ── auth: the shared predicate ─────────────────────────────────────────


class TestKeyAllowsModel:
    @pytest.mark.parametrize("ctx", [None, {}, {"allowed_models": None}, {"allowed_models": []}])
    def test_unrestricted_key_allows_everything(self, ctx):
        assert auth_mod.key_allows_model(ctx, "openrouter/openai/gpt-6.1-sol") is True

    def test_restricted_key_allows_a_listed_model(self):
        ctx = {"allowed_models": ALLOWED}
        assert auth_mod.key_allows_model(ctx, ALLOWED[0]) is True

    def test_restricted_key_rejects_an_unlisted_model(self):
        ctx = {"allowed_models": ALLOWED}
        assert auth_mod.key_allows_model(ctx, "openrouter/openai/gpt-6.1-sol") is False


# ── auth: the context carries the allowlist ────────────────────────────


def test_auth_context_carries_allowed_models():
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1", port=1234),
        headers={},
        state=SimpleNamespace(),
    )
    ctx = auth_mod._build_auth_context(
        request, "gamedev_abc", "authorization", {"name": "gamedev", "allowed_models": ALLOWED}
    )
    assert ctx["allowed_models"] == ALLOWED


def test_auth_context_omits_allowlist_for_unrestricted_keys():
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1", port=1234),
        headers={},
        state=SimpleNamespace(),
    )
    ctx = auth_mod._build_auth_context(request, "flip_abc", "authorization", {"name": "flip"})
    assert ctx["allowed_models"] is None


# ── routing: the request-path gate ─────────────────────────────────────


def _request_with_ctx(ctx: dict):
    return SimpleNamespace(state=SimpleNamespace(auth_context=ctx))


class TestRoutingGate:
    def test_unlisted_model_is_rejected_before_any_registry_lookup(self, monkeypatch):
        monkeypatch.setattr(
            routing, "_get_request_auth_context", lambda request: {"allowed_models": ALLOWED}
        )
        with pytest.raises(HTTPException) as exc:
            routing.resolve_cloud_attempts(
                "openrouter/openai/gpt-6.1-sol", _request_with_ctx({}), "gamedev"
            )
        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "model_not_allowed"
        assert exc.value.detail["model"] == "openrouter/openai/gpt-6.1-sol"

    def test_unrestricted_key_is_not_blocked_by_the_allowlist(self, monkeypatch):
        """No allowlist → the gate must not fire; the next failure is the real one."""
        monkeypatch.setattr(routing, "_get_request_auth_context", lambda request: {})
        monkeypatch.setattr(routing, "_provider_registry", None)
        with pytest.raises(Exception) as exc:
            routing.resolve_cloud_attempts(
                "openrouter/openai/gpt-6.1-sol", _request_with_ctx({}), "flip"
            )
        # Anything but the allowlist 403 proves the gate stayed out of the way.
        assert not (
            isinstance(exc.value, HTTPException)
            and isinstance(exc.value.detail, dict)
            and exc.value.detail.get("error") == "model_not_allowed"
        )


# ── discovery: the filter and the 404 ──────────────────────────────────


class TestDiscoveryGate:
    def test_allowed_models_reader_returns_none_when_unrestricted(self, monkeypatch):
        monkeypatch.setattr(model_discovery, "_get_request_auth_context", lambda request: {})
        assert model_discovery._key_allowed_models(_request_with_ctx({})) is None

    def test_allowed_models_reader_returns_the_list(self, monkeypatch):
        monkeypatch.setattr(
            model_discovery, "_get_request_auth_context", lambda request: {"allowed_models": ALLOWED}
        )
        assert model_discovery._key_allowed_models(_request_with_ctx({})) == ALLOWED

    def test_assert_model_allowed_passes_a_listed_model(self, monkeypatch):
        monkeypatch.setattr(
            model_discovery, "_get_request_auth_context", lambda request: {"allowed_models": ALLOWED}
        )
        model_discovery._assert_model_allowed(_request_with_ctx({}), ALLOWED[0])

    def test_assert_model_allowed_404s_an_unlisted_model(self, monkeypatch):
        monkeypatch.setattr(
            model_discovery, "_get_request_auth_context", lambda request: {"allowed_models": ALLOWED}
        )
        with pytest.raises(HTTPException) as exc:
            model_discovery._assert_model_allowed(
                _request_with_ctx({}), "openrouter/openai/gpt-6.1-sol"
            )
        assert exc.value.status_code == 404

    def test_assert_model_allowed_is_a_noop_without_an_allowlist(self, monkeypatch):
        monkeypatch.setattr(model_discovery, "_get_request_auth_context", lambda request: {})
        model_discovery._assert_model_allowed(
            _request_with_ctx({}), "openrouter/openai/gpt-6.1-sol"
        )


# Exercise real route functions as well as the predicate helpers above. All
# dependencies are in-memory fakes: these tests never contact a provider.
@pytest.mark.parametrize("allowed", [None, [], ALLOWED])
def test_cloud_route_accepts_allowed_or_unrestricted_model(monkeypatch, allowed):
    provider = SimpleNamespace(enabled=True, is_configured=True)
    monkeypatch.setattr(
        routing, "_get_request_auth_context", lambda request: {"allowed_models": allowed}
    )
    monkeypatch.setattr(
        routing, "_provider_registry", SimpleNamespace(_providers={"openrouter": provider})
    )
    monkeypatch.setattr(routing, "_cloud_provider_for_request", lambda model: provider)
    monkeypatch.setattr(
        routing, "_cloud_catalog",
        SimpleNamespace(resolve_cloud_target=lambda model, fallback: ("openrouter", "z-ai/glm-5.3-flash")),
    )
    assert routing.resolve_cloud_attempts(ALLOWED[0], _request_with_ctx({}), "test") == (
        [(provider, "z-ai/glm-5.3-flash")], None
    )


def test_allowlisted_model_does_not_override_disabled_cloud_access(monkeypatch):
    monkeypatch.setattr(
        routing, "_get_request_auth_context",
        lambda request: {"allowed_models": ALLOWED, "cloud_gateway_access": False},
    )
    with pytest.raises(HTTPException) as exc:
        routing.resolve_cloud_attempts(ALLOWED[0], _request_with_ctx({}), "test")
    assert exc.value.status_code == 403
    assert exc.value.detail == "cloud access disabled for this Guardian key"


def test_unlisted_failover_group_is_rejected_before_expansion(monkeypatch):
    monkeypatch.setattr(
        routing, "_get_request_auth_context", lambda request: {"allowed_models": ALLOWED}
    )
    monkeypatch.setattr(routing, "_failover_registry", None)
    with pytest.raises(HTTPException) as exc:
        routing.resolve_cloud_attempts("failover/private", _request_with_ctx({}), "test")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "model_not_allowed"


@pytest.mark.parametrize("allowed", [None, [], [ALLOWED[0]]])
async def test_list_models_applies_exact_allowlist(monkeypatch, allowed):
    monkeypatch.setattr(model_discovery, "_schedule_catalog_self_heal", lambda: None)
    monkeypatch.setattr(model_discovery, "_local_namespaced_addresses", lambda: {})
    monkeypatch.setattr(
        model_discovery, "_model_manager",
        SimpleNamespace(get_public_model_map=lambda: {"local": "local"}),
    )
    monkeypatch.setattr(
        model_discovery, "_provider_registry",
        SimpleNamespace(get_enabled_providers=lambda: [
            SimpleNamespace(name="openrouter", is_configured=True, managed=False)
        ]),
    )
    monkeypatch.setattr(model_discovery, "_failover_registry", SimpleNamespace(_groups={}))
    monkeypatch.setattr(
        model_discovery, "_get_request_auth_context", lambda request: {"allowed_models": allowed}
    )
    monkeypatch.setattr(model_discovery, "_cloud_entries_for_provider", lambda provider: ALLOWED)

    async def local_entry(public_name, canonical_name, client_id):
        return {"id": public_name}

    async def cloud_entry(full_id, provider_name):
        return {"id": full_id}

    monkeypatch.setattr(model_discovery, "build_model_metadata_entry", local_entry)
    monkeypatch.setattr(model_discovery, "_build_cloud_entry", cloud_entry)
    result = await model_discovery.list_models(_request_with_ctx({}), "test")
    expected = [ALLOWED[0]] if allowed else ["local", *ALLOWED]
    assert [entry["id"] for entry in result["data"]] == expected


@pytest.mark.parametrize("route", ["metadata", "show", "endpoints"])
async def test_discovery_routes_hide_unlisted_models_before_lookup(monkeypatch, route):
    ctx = {"allowed_models": ALLOWED}
    request = _request_with_ctx(ctx)

    async def request_json():
        return {"model": "unlisted/local"}

    request.json = request_json
    monkeypatch.setattr(model_discovery, "_get_request_auth_context", lambda request: ctx)
    monkeypatch.setattr(model_endpoints, "_auth_context", lambda request: ctx)
    with pytest.raises(HTTPException) as exc:
        if route == "metadata":
            await model_discovery.model_metadata("unlisted/local", request, "test")
        elif route == "show":
            await model_discovery.show_model(request, "test")
        else:
            await model_endpoints.model_endpoints("unlisted/local", request, "test")
    assert exc.value.status_code == 404
