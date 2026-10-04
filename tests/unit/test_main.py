"""Tests for app.main dashboard stats API."""

import logging
from collections import defaultdict
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app import main
from app.proxy.usage import ApiUsageTracker


class JsonRequest:
    """Minimal authenticated request fixture for dashboard endpoint tests."""

    def __init__(self, payload: dict, key_fingerprint: str = "owner-key") -> None:
        self._payload = payload
        self.state = SimpleNamespace(auth_context={"key_fingerprint": key_fingerprint})

    async def json(self) -> dict:
        return self._payload


def test_configure_static_mount_skips_missing_dir(tmp_path, caplog):
    """Missing built dashboard assets should not block Guardian startup."""
    application = FastAPI()

    with caplog.at_level(logging.WARNING):
        main._configure_static_mount(application, tmp_path / "static")

    assert all(getattr(route, "path", None) != "/static" for route in application.routes)
    assert "skipping /static mount" in caplog.text


@pytest.mark.asyncio
async def test_get_stats_includes_api_usage(monkeypatch, tmp_path):
    """Dashboard stats include the persisted API usage snapshot."""
    tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")
    tracker.start_request(
        request_id="live-req-1",
        client_id="test-user",
        endpoint="/v1/chat/completions",
        method="POST",
        model="GLM-4.7-Flash",
        streamed=True,
    )
    tracker.update_active_request(
        request_id="live-req-1",
        phase="running",
        queue_request_id="queue-req-1",
        prompt_tokens=8,
        completion_tokens=5,
    )
    tracker.record_request(
        client_id="test-user",
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        model="GLM-4.7-Flash",
    )
    tracker.record_tokens(
        client_id="test-user",
        endpoint="/v1/chat/completions",
        model="GLM-4.7-Flash",
        prompt_tokens=8,
        completion_tokens=5,
    )

    monkeypatch.setattr(main, "get_gpu_metrics", lambda: {"used": 1024, "free": 2048, "total": 3072})
    monkeypatch.setattr(main, "get_model_size", lambda model: 4096)
    monkeypatch.setattr(main.proxy_state, "last_used", defaultdict(float, {"GLM-4.7-Flash": 1000.0}))
    monkeypatch.setattr(main.proxy_state.scheduler, "active_counts", {"GLM-4.7-Flash": 1}, raising=False)
    monkeypatch.setattr(main.proxy_state, "api_usage", tracker, raising=False)
    monkeypatch.setattr(
        main.inference_queue,
        "get_status",
        lambda: {
            "queue_length": 2,
            "active_count": 1,
            "wait_policy": "disconnect_or_cancel",
            "active_requests": [{"client_id": "test-user", "status": "running"}],
            "waiting": [{"client_id": "hydroponics", "position": 1}],
        },
    )

    stats = await main.get_stats()

    assert stats["api_usage"]["summary"]["total_requests"] == 1
    assert stats["api_usage"]["summary"]["total_tokens"] == 13
    assert stats["api_usage"]["summary"]["active_requests_count"] == 1
    assert stats["api_usage"]["active_requests"][0]["queue_request_id"] == "queue-req-1"
    assert stats["api_usage"]["top_clients"][0]["client_id"] == "test-user"
    assert stats["cached_models"][0]["name"] == "GLM-4.7-Flash"
    assert stats["queue_size"] == 2
    assert stats["queue_status"]["wait_policy"] == "disconnect_or_cancel"
    assert stats["queue_status"]["active_requests"][0]["client_id"] == "test-user"
    assert stats["queue_status"]["waiting"][0]["client_id"] == "hydroponics"


@pytest.mark.asyncio
async def test_dashboard_cloud_catalog_delegates_to_the_shared_implementation(monkeypatch):
    """The dashboard app (port 11437) and the API app (11436) are two separate
    FastAPI apps, and this route used to hold its own hand-built copy of the
    payload.  That copy silently drifted: it never returned the enriched
    ``models`` array, so the dashboard's model catalog view could only ever show
    the legacy address pills.  Pin the delegation instead of a local shape."""
    enriched = [{"id": "openrouter/openai/gpt-4o", "name": "OpenAI: GPT-4o"}]
    seen: list[str] = []

    async def fake_list(client_id):
        seen.append(client_id)
        return {"catalog": [{"name": "openrouter", "addresses": [], "models": enriched}]}

    monkeypatch.setattr(main._admin_api, "list_cloud_catalog", fake_list)

    result = await main.list_cloud_catalog_ui("client")

    assert seen == ["client"], "the dashboard must call the shared implementation"
    assert result["catalog"][0]["models"] == enriched


@pytest.mark.asyncio
async def test_dashboard_cloud_catalog_refresh_delegates(monkeypatch):
    """Refresh delegates too, so it also refreshes the reference catalog and
    returns the refreshed payload rather than a bare status string."""
    seen: list[str] = []

    async def fake_refresh(client_id):
        seen.append(client_id)
        return {"status": "refreshed", "catalog": []}

    monkeypatch.setattr(main._admin_api, "refresh_cloud_catalog", fake_refresh)

    result = await main.refresh_cloud_catalog_ui("client")

    assert seen == ["client"]
    assert result["status"] == "refreshed"


@pytest.mark.asyncio
async def test_both_surfaces_serve_the_same_catalog_payload():
    """Regression guard for the drift above: whatever the API app returns, the
    dashboard app returns the same document.

    ``created`` is ``int(time.time())`` at build time, so two calls that straddle
    a second boundary legitimately differ; it is excluded rather than compared.
    The delegation is what this test pins, not the clock. Everything else must be
    byte-identical, which is the property that breaks if the two apps ever grow
    separate implementations again.
    """
    from app.gateway import admin_api

    def without_created(node):
        if isinstance(node, dict):
            return {k: without_created(v) for k, v in node.items() if k != "created"}
        if isinstance(node, list):
            return [without_created(v) for v in node]
        return node

    dashboard = await main.list_cloud_catalog_ui("client")
    api = await admin_api.list_cloud_catalog("client")

    assert without_created(dashboard) == without_created(api)


@pytest.mark.asyncio
async def test_dashboard_catalog_carries_enriched_models(monkeypatch):
    """The dashboard payload must carry ``models``, not just bare addresses —
    that is what the catalogue view renders. Uses fakes so the result does not
    depend on runtime catalogues or credentials."""
    from app.gateway import admin_api

    provider = SimpleNamespace(name="openrouter", is_configured=True, managed=False)
    captured = {
        "openai/gpt-4o": {
            "name": "OpenAI: GPT-4o",
            "architecture": {
                "modality": "text+image->text",
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "tokenizer": "GPT",
                "instruct_type": None,
            },
            "pricing": {"prompt": "0.0000025"},
            "supported_parameters": ["tools"],
        }
    }

    class _Catalog:
        _catalogs = {"openrouter": {"fetched_at": 1.0}}

        def get_models_for_provider(self, name):
            return {"openai/gpt-4o": "gpt-4o"}

        def get_model_metadata(self, provider_name, identity):
            return dict(captured.get(identity, {}))

        def get_model_overrides(self, identity, provider_name=""):
            return {}

        def is_auth_error(self, name):
            return False

    monkeypatch.setattr(
        admin_api,
        "_provider_registry",
        SimpleNamespace(
            get_enabled_providers=lambda: [provider],
            build_model_metadata_entry=lambda full_id: {
                "id": full_id,
                "object": "model",
                "created": 1,
                "owned_by": "openrouter",
                "permission": [],
                "served_by": "cloud",
                "provider": "openrouter",
            },
        ),
    )
    monkeypatch.setattr(admin_api, "_cloud_catalog", _Catalog())
    monkeypatch.setattr(admin_api, "_reference_catalog", None)

    result = await main.list_cloud_catalog_ui("client")
    entry = result["catalog"][0]

    assert entry["addresses"] == ["openrouter/openai/gpt-4o"]
    assert len(entry["models"]) == 1
    model = entry["models"][0]
    assert model["id"] == "openrouter/openai/gpt-4o"
    assert model["architecture"]["input_modalities"] == ["text", "image"]
    assert model["supported_parameters"] == ["tools"]
    assert "metadata_sources" in model



@pytest.mark.asyncio
async def test_dashboard_catalog_survives_a_raising_registry_entry_builder(monkeypatch):
    """``_build_catalog_entry`` is documented fail-open: one model whose
    registry entry builder raises must degrade to its renderable fallback
    entry, not 500 the whole dashboard catalog into a silent "Loading…"."""
    from app.gateway import admin_api

    provider = SimpleNamespace(name="openrouter", is_configured=True, managed=False)

    class _Registry:
        def get_enabled_providers(self):
            return [provider]

        def build_model_metadata_entry(self, full_id):
            if full_id == "openrouter/openai/broken":
                raise RuntimeError("inconsistent registry state")
            return {
                "id": full_id,
                "object": "model",
                "created": 1,
                "owned_by": "openrouter",
                "permission": [],
                "served_by": "cloud",
                "provider": "openrouter",
            }

    class _Catalog:
        _catalogs = {"openrouter": {"fetched_at": 1.0}}

        def get_models_for_provider(self, name):
            return {"openai/gpt-4o": "gpt-4o", "openai/broken": "broken"}

        def get_model_metadata(self, provider_name, identity):
            return {}

        def get_model_overrides(self, identity, provider_name=""):
            return {}

        def is_auth_error(self, name):
            return False

    monkeypatch.setattr(admin_api, "_provider_registry", _Registry())
    monkeypatch.setattr(admin_api, "_cloud_catalog", _Catalog())
    monkeypatch.setattr(admin_api, "_reference_catalog", None)

    result = await main.list_cloud_catalog_ui("client")

    models = {m["id"]: m for p in result["catalog"] for m in p["models"]}
    assert set(models) == {"openrouter/openai/gpt-4o", "openrouter/openai/broken"}
    # The broken model degrades to the minimal renderable entry.
    assert models["openrouter/openai/broken"]["served_by"] == "cloud"
