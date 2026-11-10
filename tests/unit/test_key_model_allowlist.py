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
from app.gateway import model_discovery
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
