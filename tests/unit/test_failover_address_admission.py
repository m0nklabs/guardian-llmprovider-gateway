"""Failover-address admission pins — chat must accept ``failover/{group}``.

Setup-agent handoff follow-up (2026-09-09): discovery lists failover groups
as ``failover/{name}`` synthetic entries, but chat admission
(``resolve_or_reject_inference_model``) rejected them with 404
``model_not_served`` — "failover" is not a configured provider, so the request
died before cloud routing could walk the group's candidates. Admission must
accept a failover address when the named group exists in the registry.
"""

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.local_inference import models as local_models


@pytest.fixture(autouse=True)
def _restore_local_models_init_state():
    """``local_models.init()`` writes module globals; restore them after each
    test so later tests (server-shell routing, grammar stripping) keep running
    against the real dependency wiring instead of this file's fakes."""
    names = (
        "_model_manager",
        "_provider_registry",
        "_failover_registry",
        "_config",
        "_safe_vram_limit_mb",
        "_model_switch_lock",
        "_reset_startup_check_status",
        "_run_guardian_operation",
        "_ModelLoadError",
    )
    saved = {n: getattr(local_models, n) for n in names if hasattr(local_models, n)}
    yield
    for n, v in saved.items():
        setattr(local_models, n, v)


class _FakeManager:
    models: dict = {}

    def resolve_model(self, name):
        raise ValueError(name)

    def get_preferred_tool_model(self, current):
        return None

    def resolve_reload_target(self, current):
        return current


class _FakeProviders:
    def is_cloud_model(self, name):
        return False

    def _provider_from_address(self, name):
        return None


def _init(registry):
    local_models.init(
        model_manager=_FakeManager(),
        provider_registry=_FakeProviders(),
        failover_registry=registry,
        config={},
        safe_vram_limit_mb=0,
        model_switch_lock=None,
        reset_startup_check_status=lambda: None,
        run_guardian_operation=None,
        model_load_error_cls=RuntimeError,
    )


def _registry():
    return SimpleNamespace(
        get_group=lambda name: SimpleNamespace(name=name) if name == "free" else None
    )


def test_failover_group_address_accepted():
    _init(_registry())
    assert local_models.resolve_or_reject_inference_model("failover/free", "x") == "failover/free"


def test_unknown_failover_group_rejected():
    _init(_registry())
    with pytest.raises(HTTPException) as exc:
        local_models.resolve_or_reject_inference_model("failover/nope", "x")
    assert exc.value.status_code == 404
    assert exc.value.detail["reason"] == "requested_model_not_served"


def test_failover_address_without_registry_rejected():
    _init(None)
    with pytest.raises(HTTPException) as exc:
        local_models.resolve_or_reject_inference_model("failover/free", "x")
    assert exc.value.status_code == 404


def test_regular_unknown_model_still_rejected():
    _init(_registry())
    with pytest.raises(HTTPException) as exc:
        local_models.resolve_or_reject_inference_model("totally/unknown-model", "x")
    assert exc.value.status_code == 404


def test_failover_address_is_cloud_routed(monkeypatch):
    """Route predicate: failover/{group} must be cloud-routed, not local.

    Without the failover branch in is_cloud_or_guardian_route, the chat
    dispatcher sent the group address to the LOCAL backend (observed live
    09-09: 'failover/free' answered by llama.cpp instead of the group's
    cloud candidates).
    """
    import app.cloud_inference as ci

    fake_registry = SimpleNamespace(
        is_cloud_model=lambda name: name.startswith("openrouter/"),
        _provider_from_address=lambda name: None,
    )
    monkeypatch.setattr(ci, "_provider_registry", fake_registry)
    assert ci.is_cloud_or_guardian_route("failover/free") is True
    assert ci.is_cloud_or_guardian_route("failover/any-group") is True
    assert ci.is_cloud_or_guardian_route("openrouter/deepseek/deepseek-chat") is True
    assert ci.is_cloud_or_guardian_route("totally/unknown-model") is False
