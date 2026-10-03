"""Unit tests for app.gateway.model_endpoints (OPENROUTER_PARITY.md §4).

The endpoint surface is exercised against the repository's real
``ProviderRegistry`` and ``CloudModelCatalog`` (built from a temporary provider
settings file) so catalog resolution, brand normalization and
``catalog_allowlist`` filtering are the production code paths, not mocks.

The metadata-presentation module of the parity feature is imported defensively by
the module under test; these tests attach a stand-in that implements the frozen
interface (``split_identity`` / ``resolve_model_metadata`` /
``endpoint_entry_fields``) so they stay green whichever track lands first.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.gateway import model_endpoints as me
from app.proxy.cloud_catalog import CloudModelCatalog
from app.proxy.failover import FailoverRegistry, ProviderHealthTracker
from app.proxy.providers import ProviderRegistry

# ── Config fixtures ──────────────────────────────────────────────────

SETTINGS = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    model_prefixes:
      - openai/
      - anthropic/
      - minimaxai/
  groq:
    enabled: true
    base_url: https://api.groq.com/openai/v1
    api_key: gsk-test-key
    model_prefixes:
      - openai/
  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: nvapi-test-key
    model_prefixes:
      - openai/
      - minimaxai/
    catalog_allowlist:
      - minimaxai/minimax-m3
  ai-node-local:
    enabled: true
    base_url: http://127.0.0.1:11440/v1
    local: true
    models:
      llama3.2-3b:
        grammar_decoding: true
        max_tokens: 4096
"""

#: Provider settings without any local (managed) provider.
SETTINGS_NO_LOCAL = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    model_prefixes:
      - openai/
"""

GROUPS = {
    "free": {
        "candidates": [
            {"provider": "openrouter", "model": "minimaxai/minimax-m3"},
            {"provider": "groq", "model": "openai/gpt-4o"},
            {"provider": "ghost", "model": "ghost/model"},
        ],
        "image_fallback": {"local_model": "llama3.2-3b"},
    },
}

DEFAULT_CATALOGS = {
    "openrouter": {
        "fetched_at": 1.0,
        "models": {
            "openai/gpt-4o": "openai/gpt-4o",
            "anthropic/claude-sonnet-4.6": "anthropic/claude-sonnet-4.6",
        },
        "metadata": {
            "openai/gpt-4o": {
                "name": "OpenAI: GPT-4o",
                "description": "OpenAI's flagship multimodal model.",
                "created": 1715367049,
                "context_length": 128000,
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                "top_provider": {
                    "context_length": 128000,
                    "max_completion_tokens": 16384,
                    "is_moderated": False,
                },
                "supported_parameters": ["temperature", "tools", "tool_choice"],
                "architecture": {
                    "modality": "text+image->text",
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                    "tokenizer": None,
                    "instruct_type": None,
                },
            },
        },
        "reasoning": {},
        "context": {"openai/gpt-4o": 128000},
        "modalities": {"openai/gpt-4o": {"input": ["image", "text"], "output": ["text"]}},
    },
    "groq": {
        "fetched_at": 1.0,
        "models": {"openai/gpt-4o": "openai/gpt-4o"},
        # The route advertises metadata but no pricing at all.
        "metadata": {
            "openai/gpt-4o": {
                "name": "OpenAI GPT-4o on Groq",
                "description": None,
                "created": None,
                "context_length": None,
                "pricing": None,
                "top_provider": {
                    "context_length": None,
                    "max_completion_tokens": None,
                    "is_moderated": None,
                },
                "supported_parameters": None,
                "architecture": None,
            },
        },
        "reasoning": {},
        "context": {},
        "modalities": {},
    },
    "nvidia": {
        "fetched_at": 1.0,
        # Lists openai/gpt-4o upstream, but the provider's catalog_allowlist does
        # not contain it, so the free token cannot actually route it.
        "models": {
            "openai/gpt-4o": "openai/gpt-4o",
            "minimaxai/minimax-m3": "minimaxai/minimax-m3",
        },
        "metadata": {},
        "reasoning": {},
        "context": {},
        "modalities": {},
    },
    "ai-node-local": {
        "fetched_at": 1.0,
        "models": {"llama3.2-3b": "llama3.2-3b"},
        "metadata": {},
        "reasoning": {},
        "context": {"llama3.2-3b": 131072},
        "modalities": {},
    },
}


# ── Local stand-ins ──────────────────────────────────────────────────


class _FakeModelManager:
    """Minimal ModelManager stand-in for the local registry surface."""

    def __init__(self, models=None, public=None, aliases=None):
        self.models = dict(models or {})
        self._public = dict(public or {})
        self._aliases = dict(aliases or {})

    def get_public_model_map(self):
        return dict(self._public)

    def resolve_model(self, name):
        if name in self.models:
            return name
        if name in self._aliases:
            return self._aliases[name]
        raise ValueError(f"Model '{name}' not found in configuration (no alias match)")

    def get_vision_capability(self, name):
        return {"configured": False, "status": "text_only", "validated": False}


def _stub_split_identity(model_id: str) -> str:
    """Stand-in for the presentation contract: drop the leading provider segment.

    Only called by the module under test for a three-segment
    ``{provider}/{brand}/{model}`` address.
    """
    parts = (model_id or "").split("/")
    if len(parts) >= 3 and parts[0]:
        return "/".join(parts[1:])
    return model_id or ""


def _stub_resolve_model_metadata(
    model_id,
    *,
    upstream=None,
    reference=None,
    reference_source=None,
    overrides=None,
    local=None,
    context_length=None,
    created=None,
):
    """A faithful-ish stand-in for the frozen presentation contract (§3)."""
    upstream = upstream or {}
    reference = reference or {}
    overrides = overrides or {}
    local = local or {}
    sources: dict[str, str] = {}

    def first(*values):
        for value in values:
            if value is not None:
                return value
        return None

    identity = _stub_split_identity(model_id)
    brand, sep, model = identity.partition("/")
    name = first(
        upstream.get("name"),
        reference.get("name"),
        f"{brand.title()}: {model}" if sep else identity,
    )
    if not upstream.get("name") and reference.get("name"):
        sources["name"] = f"reference:{reference_source}"
    architecture = first(upstream.get("architecture"), reference.get("architecture"))
    if not isinstance(architecture, dict):
        architecture = {
            "modality": None,
            "input_modalities": None,
            "output_modalities": None,
            "tokenizer": None,
            "instruct_type": None,
        }
    if local.get("served_by") == "local":
        pricing = {"prompt": "0", "completion": "0", "request": "0", "local": "true"}
        sources["pricing"] = "local"
    else:
        pricing = first(upstream.get("pricing"), reference.get("pricing"))
        if pricing is not None and not upstream.get("pricing") and reference.get("pricing"):
            sources["pricing"] = f"reference:{reference_source}"
    supported = first(
        upstream.get("supported_parameters"),
        reference.get("supported_parameters"),
        ["max_tokens", "temperature"] if local.get("served_by") == "local" else None,
    )
    max_completion = first(
        overrides.get("max_tokens"),
        (upstream.get("top_provider") or {}).get("max_completion_tokens"),
        (reference.get("top_provider") or {}).get("max_completion_tokens"),
    )
    fields = {
        "id": model_id,
        "name": name,
        "created": first(upstream.get("created"), reference.get("created"), created, 1700000000),
        "description": first(upstream.get("description"), reference.get("description")),
        "architecture": architecture,
        "pricing": pricing,
        "top_provider": {
            "context_length": context_length,
            "max_completion_tokens": max_completion,
            "is_moderated": None,
        },
        "supported_parameters": supported,
        "context_length": context_length,
    }
    return fields, sources


def _stub_endpoint_entry_fields(**fields):
    return dict(fields)


def _make_presentation(**overrides):
    module = SimpleNamespace(
        split_identity=_stub_split_identity,
        resolve_model_metadata=_stub_resolve_model_metadata,
        endpoint_entry_fields=_stub_endpoint_entry_fields,
    )
    for key, value in overrides.items():
        setattr(module, key, value)
    return module


def _write_settings(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "providers.settings.yaml"
    path.write_text(textwrap.dedent(content))
    return path


class _World:
    """Everything a test needs to call the handler."""

    def __init__(self, **kwargs):
        self.request = kwargs["request"]
        self.registry = kwargs["registry"]
        self.catalog = kwargs["catalog"]
        self.manager = kwargs["manager"]
        self.failover = kwargs["failover"]
        self.calls: list[tuple[Any, ...]] = []
        self.cloud_denied = kwargs.get("cloud_denied", False)

    async def endpoints(self, model_id, client_id="tester"):
        return await me.model_endpoints(model_id, self.request, client_id)

    def names(self, payload):
        return [endpoint["provider_name"] for endpoint in payload["data"]["endpoints"]]


def _build_world(
    tmp_path: Path,
    *,
    settings: str = SETTINGS,
    catalogs: dict | None = None,
    groups: dict | None = GROUPS,
    models: dict | None = None,
    public: dict | None = None,
    aliases: dict | None = None,
    reference=None,
    cloud_gateway_access: bool = True,
    context_window: int | None = 131072,
    resolver_raises_403: bool = False,
):
    registry = ProviderRegistry(settings_path=_write_settings(tmp_path, settings))
    catalog = CloudModelCatalog(
        provider_registry=registry,
        cache_file=tmp_path / "cloud_catalog_cache.json",
        overrides_file=tmp_path / "overrides.yaml",
    )
    catalog._catalogs = catalogs if catalogs is not None else DEFAULT_CATALOGS
    failover = FailoverRegistry(groups=groups if groups is not None else {})
    manager = _FakeModelManager(models or {"llama3.2-3b": {"grammar_decoding": True, "max_tokens": 4096}},
                                public, aliases)
    request = SimpleNamespace(
        state=SimpleNamespace(auth_context={"cloud_gateway_access": cloud_gateway_access})
    )

    def resolve_attempts(model_id, request_arg, client_id):
        auth = getattr(getattr(request_arg, "state", None), "auth_context", None) or {}
        if not auth.get("cloud_gateway_access", True) or resolver_raises_403:
            raise HTTPException(status_code=403, detail="cloud access disabled for this Guardian key")
        provider = registry.get_provider_for_model(model_id)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"Model '{model_id}' is not a cloud model")
        return [(provider, model_id)], None

    async def resolve_ctx(model_id, canonical_name=None, cloud_attempts=None):
        return context_window

    me.init(
        _provider_registry=registry,
        _cloud_catalog=catalog,
        _reference_catalog=reference,
        _failover_registry=failover,
        _model_manager=manager,
        _resolve_cloud_attempts=resolve_attempts,
        _resolve_context_window=resolve_ctx,
    )
    return _World(
        request=request,
        registry=registry,
        catalog=catalog,
        manager=manager,
        failover=failover,
        cloud_denied=not cloud_gateway_access,
    )


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Keep the injected globals and the presentation cache per-test."""
    presentation = _make_presentation()
    monkeypatch.setattr(me, "_attached_presentation", presentation)
    monkeypatch.setattr(me, "_presentation_modules", (presentation,))
    monkeypatch.setattr(me, "_enrichment_modules", ())
    monkeypatch.setattr(me, "_route_health", None)
    monkeypatch.setattr(me, "_auth_context_reader", None)
    # No live application module unless a test installs one: without this, a test
    # session that already imported app.proxy.server would hand the module a real
    # health tracker and make these assertions order-dependent.
    monkeypatch.setitem(sys.modules, "app.proxy.server", None)
    yield presentation


# ── (a) two providers serving the same identity key ──────────────────


async def test_cloud_model_served_by_two_providers_yields_two_endpoints(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    assert payload["data"]["id"] == "openai/gpt-4o"
    assert world.names(payload) == ["openrouter", "groq"]
    assert {endpoint["model_id"] for endpoint in payload["data"]["endpoints"]} == {"openai/gpt-4o"}


async def test_provider_address_and_bare_name_join_on_the_identity_key(tmp_path):
    """`openrouter/openai/gpt-4o` sees the same routes as the bare address."""
    world = _build_world(tmp_path)

    bare = await world.endpoints("openai/gpt-4o")
    addressed = await world.endpoints("openrouter/openai/gpt-4o")

    assert world.names(addressed) == world.names(bare)


# ── (b) allowlisted providers are filtered consistently ─────────────


async def test_allowlisted_provider_without_the_model_is_not_advertised(tmp_path):
    world = _build_world(tmp_path)

    # The catalog itself already filters the allowlist out...
    assert "openai/gpt-4o" not in world.catalog.get_models_for_provider("nvidia")
    assert "minimaxai/minimax-m3" in world.catalog.get_models_for_provider("nvidia")

    payload = await world.endpoints("openai/gpt-4o")

    assert "nvidia" not in world.names(payload)


async def test_allowlisted_provider_is_advertised_for_a_contained_model(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("minimaxai/minimax-m3")

    assert "nvidia" in world.names(payload)


async def test_allowlisted_provider_gets_no_cold_start_fallback_route(tmp_path):
    """An empty catalog may fall back to the registry — but never past an allowlist."""
    catalogs = {"nvidia": {"fetched_at": 1.0, "models": {}, "metadata": {}, "reasoning": {}, "context": {}, "modalities": {}}}
    world = _build_world(tmp_path, catalogs=catalogs)

    payload = await world.endpoints("nvidia/openai/gpt-4o")

    assert payload["data"]["endpoints"] == []


async def test_cloud_route_falls_back_to_the_registry_when_no_catalog_exists(tmp_path):
    """Cold start: no catalog evidence at all yet, so the routable provider is reported."""
    world = _build_world(tmp_path, catalogs={})

    payload = await world.endpoints("openrouter/openai/gpt-4o")

    assert world.names(payload) == ["openrouter"]
    assert payload["data"]["endpoints"][0]["model_id"] == "openai/gpt-4o"


# ── (c) failover groups ─────────────────────────────────────────────


async def test_failover_group_yields_one_endpoint_per_candidate(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("failover/free")

    endpoints = payload["data"]["endpoints"]
    assert len(endpoints) == len(GROUPS["free"]["candidates"])
    assert [(e["provider_name"], e["model_id"]) for e in endpoints] == [
        ("openrouter", "minimaxai/minimax-m3"),
        ("groq", "openai/gpt-4o"),
        ("ghost", "ghost/model"),
    ]
    assert all("free" in endpoint["tag"] for endpoint in endpoints)
    assert payload["data"]["id"] == "failover/free"
    # The local image fallback of the group is not a route of the group.
    assert "llama3.2-3b" not in {endpoint["model_id"] for endpoint in endpoints}


async def test_unknown_failover_group_returns_404(tmp_path):
    world = _build_world(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        await world.endpoints("failover/nope")

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Failover group 'nope' not found"


# ── (d) local models ────────────────────────────────────────────────


async def test_local_model_yields_its_local_backend_endpoint(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("llama3.2-3b")

    endpoints = payload["data"]["endpoints"]
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint["provider_name"] == "ai-node-local"
    assert endpoint["model_id"] == "llama3.2-3b"
    assert endpoint["tag"] == "ai-node-local"
    assert endpoint["pricing"] == {
        "prompt": "0",
        "completion": "0",
        "request": "0",
        "local": "true",
    }
    # No model in this repository declares a quantization -> null, not a guess.
    assert endpoint["quantization"] is None
    # A local backend route has no health signal -> null, never "healthy".
    assert endpoint["status"] is None


async def test_public_alias_resolves_to_the_local_canonical_model(tmp_path):
    world = _build_world(tmp_path, public={"llama3": "llama3.2-3b"})

    payload = await world.endpoints("llama3")

    assert payload["data"]["endpoints"][0]["model_id"] == "llama3.2-3b"
    assert payload["data"]["id"] == "llama3"


async def test_local_model_without_a_configured_local_provider(tmp_path):
    world = _build_world(tmp_path, settings=SETTINGS_NO_LOCAL)

    payload = await world.endpoints("llama3.2-3b")

    endpoint = payload["data"]["endpoints"][0]
    assert endpoint["provider_name"] is None
    assert endpoint["model_id"] == "llama3.2-3b"
    assert endpoint["pricing"]["local"] == "true"


async def test_local_route_keeps_local_pricing_even_for_a_cloud_sibling(tmp_path):
    """A cloud passthrough price is never inherited by the local route."""
    catalogs = dict(DEFAULT_CATALOGS)
    catalogs["ai-node-local"] = {
        "fetched_at": 1.0,
        "models": {"openai/gpt-4o": "openai/gpt-4o"},
        "metadata": {},
        "reasoning": {},
        "context": {},
        "modalities": {},
    }
    world = _build_world(tmp_path, catalogs=catalogs)

    payload = await world.endpoints("openai/gpt-4o")

    local_endpoints = [e for e in payload["data"]["endpoints"] if e["provider_name"] == "ai-node-local"]
    assert len(local_endpoints) == 1
    assert local_endpoints[0]["pricing"]["local"] == "true"
    assert local_endpoints[0]["pricing"]["prompt"] == "0"


def test_local_pricing_rejects_a_cloud_passthrough_block():
    assert me._local_pricing({"prompt": "0.0000025", "completion": "0.00001"}) == {
        "prompt": "0",
        "completion": "0",
        "request": "0",
        "image": "0",
        "input_cache_read": "0",
        "local": "true",
    }
    assert me._local_pricing({"prompt": "0", "local": "true"}) == {"prompt": "0", "local": "true"}


# ── (e) the frozen §4 key list, with nulls where ungrounded ─────────


@pytest.mark.parametrize(
    "model_id",
    ["openai/gpt-4o", "openrouter/openai/gpt-4o", "failover/free", "llama3.2-3b"],
)
async def test_every_endpoint_carries_the_full_section4_key_list(tmp_path, model_id):
    world = _build_world(tmp_path)

    payload = await world.endpoints(model_id)

    endpoints = payload["data"]["endpoints"]
    assert endpoints, f"no endpoints for {model_id}"
    for endpoint in endpoints:
        missing = set(me.ENDPOINT_KEYS) - set(endpoint)
        assert not missing, f"{model_id}: missing {sorted(missing)}"
        assert endpoint["supports_implicit_caching"] is None
        assert endpoint["supports_image_reference"] is None
        assert endpoint["max_prompt_tokens"] is None


@pytest.mark.parametrize(
    "model_id",
    ["openai/gpt-4o", "failover/free", "llama3.2-3b"],
)
async def test_no_fabricated_uptime_latency_or_throughput(tmp_path, model_id):
    """No per-provider time series exists in this codebase -> always null."""
    world = _build_world(tmp_path)

    payload = await world.endpoints(model_id)

    for endpoint in payload["data"]["endpoints"]:
        assert endpoint["uptime_last_30m"] is None
        assert endpoint["uptime_last_5m"] is None
        assert endpoint["uptime_last_1d"] is None
        assert endpoint["latency_last_30m"] is None
        assert endpoint["throughput_last_30m"] is None


async def test_unadvertised_health_fields_never_leak_from_metadata(tmp_path):
    """Even if a catalog entry carried such keys, the endpoint must not report them."""
    catalogs = dict(DEFAULT_CATALOGS)
    catalogs["openrouter"] = {
        **DEFAULT_CATALOGS["openrouter"],
        "metadata": {
            "openai/gpt-4o": {
                "name": "OpenAI: GPT-4o",
                "pricing": None,
                "top_provider": {"context_length": None, "max_completion_tokens": None, "is_moderated": None},
                "supported_parameters": None,
                "architecture": None,
                "uptime_last_30m": 99.9968,
                "latency_last_30m": 0.42,
                "throughput_last_30m": 120.5,
                "native_tools": {"openrouter:web_search": {"type": "web_search_20260209"}},
            }
        },
    }
    world = _build_world(tmp_path, catalogs=catalogs)

    payload = await world.endpoints("openai/gpt-4o")

    endpoint = next(e for e in payload["data"]["endpoints"] if e["provider_name"] == "openrouter")
    assert endpoint["uptime_last_30m"] is None
    assert endpoint["latency_last_30m"] is None
    assert endpoint["throughput_last_30m"] is None
    # native_tools comes from a provider config declaration, not from the catalog.
    assert endpoint["native_tools"] is None


async def test_native_tools_is_reported_when_the_provider_declares_it(tmp_path):
    world = _build_world(tmp_path)
    world.registry._providers["openrouter"].native_tools = {
        "openrouter:web_search": {"type": "web_search_20260209"}
    }

    payload = await world.endpoints("openai/gpt-4o")

    endpoint = next(e for e in payload["data"]["endpoints"] if e["provider_name"] == "openrouter")
    assert endpoint["native_tools"] == {"openrouter:web_search": {"type": "web_search_20260209"}}


def test_supports_tool_choice_derivation():
    assert me._supports_tool_choice(None) is None
    assert me._supports_tool_choice(["tools", "tool_choice"]) == {
        "none": True,
        "auto": True,
        "required": True,
        "function": True,
    }
    assert me._supports_tool_choice(["temperature"]) == {
        "none": True,
        "auto": False,
        "required": False,
        "function": False,
    }


# ── (f) unknown model -> 404 ────────────────────────────────────────


@pytest.mark.parametrize("model_id", ["does-not-exist", "nope/does-not-exist", ""])
async def test_unknown_model_returns_404(tmp_path, model_id):
    world = _build_world(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        await world.endpoints(model_id)

    assert excinfo.value.status_code == 404
    assert "not found in configuration" in str(excinfo.value.detail)


# ── (g) cloud access gating ─────────────────────────────────────────


async def test_cloud_access_disabled_returns_403(tmp_path):
    world = _build_world(tmp_path, cloud_gateway_access=False)

    with pytest.raises(HTTPException) as excinfo:
        await world.endpoints("openai/gpt-4o")

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "cloud access disabled for this Guardian key"


async def test_cloud_access_disabled_returns_403_for_failover(tmp_path):
    world = _build_world(tmp_path, cloud_gateway_access=False)

    with pytest.raises(HTTPException) as excinfo:
        await world.endpoints("failover/free")

    assert excinfo.value.status_code == 403


async def test_cloud_access_disabled_still_serves_local_models(tmp_path):
    world = _build_world(tmp_path, cloud_gateway_access=False)

    payload = await world.endpoints("llama3.2-3b")

    assert world.names(payload) == ["ai-node-local"]


async def test_cloud_access_disabled_hides_cloud_endpoints_of_a_local_model(tmp_path):
    """A locally served model keeps working; a cloud provider's route for the same
    identity is hidden without turning the request into a 403 (the model is
    servable locally, so access is not denied)."""
    catalogs = dict(DEFAULT_CATALOGS)
    catalogs["openrouter"] = {
        **DEFAULT_CATALOGS["openrouter"],
        "models": {"llama3.2-3b": "llama3.2-3b"},
        "metadata": {},
        "context": {},
        "modalities": {},
    }
    world = _build_world(tmp_path, catalogs=catalogs, cloud_gateway_access=False)

    payload = await world.endpoints("llama3.2-3b")

    assert world.names(payload) == ["ai-node-local"]

    # With cloud access the very same request also reports the cloud route.
    allowed = _build_world(tmp_path, catalogs=catalogs)
    assert allowed.names(await allowed.endpoints("llama3.2-3b")) == ["openrouter", "ai-node-local"]


async def test_router_403_is_propagated_when_the_reader_disagrees(tmp_path):
    """The shared cloud router stays authoritative for authorization."""
    world = _build_world(tmp_path, resolver_raises_403=True)

    with pytest.raises(HTTPException) as excinfo:
        await world.endpoints("openai/gpt-4o")

    assert excinfo.value.status_code == 403


def test_auth_context_reader_prefers_the_attached_reader(monkeypatch):
    calls = []

    def attached(request):
        calls.append("attached")
        return {"cloud_gateway_access": False}

    sibling = SimpleNamespace(
        _get_request_auth_context=lambda request: {"cloud_gateway_access": True}
    )
    monkeypatch.setitem(sys.modules, "app.gateway.model_discovery", sibling)
    monkeypatch.setattr("app.gateway.model_discovery", sibling, raising=False)
    me.attach(auth_context_reader=attached)

    request = SimpleNamespace(state=SimpleNamespace(auth_context={"cloud_gateway_access": True}))

    assert me._key_can_access_cloud(request, "tester") is False
    assert calls == ["attached"]


def test_auth_context_falls_back_to_the_sibling_helper(monkeypatch):
    sibling = SimpleNamespace(
        _get_request_auth_context=lambda request: {"cloud_gateway_access": False}
    )
    monkeypatch.setitem(sys.modules, "app.gateway.model_discovery", sibling)
    monkeypatch.setattr("app.gateway.model_discovery", sibling, raising=False)

    request = SimpleNamespace(state=SimpleNamespace(auth_context={"cloud_gateway_access": True}))

    assert me._key_can_access_cloud(request, "tester") is False


def test_auth_context_fallback_reads_the_request_context(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.gateway.model_discovery", None)

    request = SimpleNamespace(state=SimpleNamespace(auth_context={"cloud_gateway_access": False}))
    assert me._key_can_access_cloud(request, "tester") is False

    scope_request = SimpleNamespace(scope={"guardian_auth_context": {"cloud_gateway_access": False}})
    assert me._key_can_access_cloud(scope_request, "tester") is False

    # Absent key means allowed (the redesign default).
    assert me._key_can_access_cloud(SimpleNamespace(state=SimpleNamespace()), "tester") is True


# ── (h) pricing honesty ─────────────────────────────────────────────


async def test_cloud_route_without_pricing_reports_null_not_another_providers_price(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    # Verbatim passthrough of the route's own advertised pricing.
    assert by_provider["openrouter"]["pricing"] == {
        "prompt": "0.0000025",
        "completion": "0.00001",
    }
    # No advertised pricing for this route and no reference entry -> null.
    assert by_provider["groq"]["pricing"] is None


async def test_reference_pricing_is_never_borrowed_by_another_route(tmp_path):
    """§3.2: pricing describes the ROUTE, so the reference catalog must not fill
    it, even when it advertises a price for the same identity key.

    OpenRouter charges for ``openai/gpt-4o`` while ``groq`` may serve it for
    nothing; copying the price across would state something false about what the
    groq route costs. Model-shaped fields may still be filled from the reference.
    """
    reference = SimpleNamespace(
        metadata=lambda identity: {"pricing": {"prompt": "0.000001"}},
        source_name=lambda identity: "openrouter",
    )
    world = _build_world(tmp_path, reference=reference)

    payload = await world.endpoints("openai/gpt-4o")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    # No price of its own -> null, never the reference's price.
    assert by_provider["groq"]["pricing"] is None
    # The route's own advertisement still passes through verbatim.
    assert by_provider["openrouter"]["pricing"] == {
        "prompt": "0.0000025",
        "completion": "0.00001",
    }


async def test_failover_candidate_without_pricing_reports_null(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("failover/free")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    assert by_provider["groq"]["pricing"] is None
    assert by_provider["ghost"]["pricing"] is None


async def test_failover_reference_lookup_is_per_candidate(tmp_path):
    """One candidate never inherits another candidate's reference entry."""
    seen: list[str] = []

    def metadata(identity):
        seen.append(identity)
        if identity == "minimaxai/minimax-m3":
            return {"pricing": {"prompt": "0.0000003"}, "context_length": 1000000}
        return None

    reference = SimpleNamespace(metadata=metadata, source_name=lambda identity: "openrouter")
    world = _build_world(tmp_path, reference=reference)
    seen.clear()

    payload = await world.endpoints("failover/free")

    by_model = {e["model_id"]: e for e in payload["data"]["endpoints"]}
    # §3.2: no candidate borrows the reference's price — pricing is route-shaped.
    assert by_model["minimaxai/minimax-m3"]["pricing"] is None
    assert by_model["openai/gpt-4o"]["pricing"] is None
    assert by_model["ghost/model"]["pricing"] is None
    # The reference is still consulted per candidate (model-shaped fields), and
    # the lookup covers every candidate rather than one shared entry.
    assert set(seen) >= {"minimaxai/minimax-m3", "openai/gpt-4o", "ghost/model"}


# ── status: what live health state actually exists ──────────────────


async def test_status_is_null_without_a_health_signal(tmp_path, monkeypatch):
    # No live application module and no attached reader: nothing tracks the route.
    monkeypatch.setitem(sys.modules, "app.proxy.server", None)
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    assert [e["status"] for e in payload["data"]["endpoints"]] == [None, None]


async def test_status_uses_the_live_health_tracker_of_the_application(tmp_path, monkeypatch):
    """The tracker the running app builds is used when one is attached nowhere."""
    tracker = ProviderHealthTracker(failure_threshold=2, cooldown_seconds=60.0)
    tracker.record_failure("groq", "openai/gpt-4o")
    tracker.record_failure("groq", "openai/gpt-4o")
    monkeypatch.setitem(
        sys.modules, "app.proxy.server", SimpleNamespace(failover_health=tracker)
    )
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    assert by_provider["groq"]["status"] == me.STATUS_DEGRADED
    assert by_provider["openrouter"]["status"] == me.STATUS_HEALTHY


async def test_status_from_the_failover_health_tracker(tmp_path):
    world = _build_world(tmp_path)
    tracker = ProviderHealthTracker(failure_threshold=2, cooldown_seconds=60.0)
    tracker.record_failure("openrouter", "openai/gpt-4o")
    tracker.record_failure("openrouter", "openai/gpt-4o")
    tracker.record_rate_limited("groq", "openai/gpt-4o")
    me.attach(route_health=me.failover_route_health(tracker))

    payload = await world.endpoints("openai/gpt-4o")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    assert by_provider["openrouter"]["status"] == me.STATUS_DEGRADED
    assert by_provider["groq"]["status"] == me.STATUS_RATE_LIMITED

    tracker.record_success("openrouter", "openai/gpt-4o")
    tracker.clear_rate_limit("groq", "openai/gpt-4o")
    payload = await world.endpoints("openai/gpt-4o")
    assert [e["status"] for e in payload["data"]["endpoints"]] == [
        me.STATUS_HEALTHY,
        me.STATUS_HEALTHY,
    ]


async def test_status_reports_broken_credentials_without_a_tracker(tmp_path):
    catalogs = dict(DEFAULT_CATALOGS)
    catalogs["openrouter"] = {**DEFAULT_CATALOGS["openrouter"], "auth_error": True}
    world = _build_world(tmp_path, catalogs=catalogs)

    payload = await world.endpoints("openai/gpt-4o")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    assert by_provider["openrouter"]["status"] == me.STATUS_DEGRADED
    assert by_provider["groq"]["status"] is None


async def test_local_route_never_reports_a_status(tmp_path):
    world = _build_world(tmp_path)
    me.attach(route_health=lambda provider_name, model_id: me.STATUS_DEGRADED)

    payload = await world.endpoints("llama3.2-3b")

    assert payload["data"]["endpoints"][0]["status"] is None


# ── model-level response shape ──────────────────────────────────────


async def test_response_shape_and_metadata_sources(tmp_path):
    """The reference catalog fills the model-level gaps and names its provenance."""
    reference = SimpleNamespace(
        metadata=lambda identity: {"name": "Anthropic: Claude Sonnet 4.6"},
        source_name=lambda identity: "openrouter",
    )
    world = _build_world(tmp_path, reference=reference)

    payload = await world.endpoints("anthropic/claude-sonnet-4.6")

    data = payload["data"]
    assert set(me.MODEL_KEYS) <= set(data)
    assert data["name"] == "Anthropic: Claude Sonnet 4.6"
    assert data["description"] is None
    assert data["architecture"] == {
        "modality": None,
        "input_modalities": None,
        "output_modalities": None,
        "tokenizer": None,
        "instruct_type": None,
    }
    assert isinstance(data["endpoints"], list)
    assert data["metadata_sources"] == {"name": "reference:openrouter"}
    assert set(payload) == {"data"}


async def test_model_level_metadata_comes_from_the_routes_own_upstream(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    data = payload["data"]
    assert data["name"] == "OpenAI: GPT-4o"
    assert data["created"] == 1715367049
    assert data["description"] == "OpenAI's flagship multimodal model."
    assert data["architecture"]["modality"] == "text+image->text"


async def test_metadata_sources_is_omitted_when_empty(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    assert "metadata_sources" not in payload["data"]


async def test_route_metadata_is_used_per_route(tmp_path):
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    by_provider = {e["provider_name"]: e for e in payload["data"]["endpoints"]}
    assert by_provider["openrouter"]["context_length"] == 128000
    assert by_provider["openrouter"]["supported_parameters"] == [
        "temperature",
        "tools",
        "tool_choice",
    ]
    assert by_provider["openrouter"]["supports_tool_choice"]["required"] is True
    assert by_provider["openrouter"]["max_completion_tokens"] == 16384
    # groq advertises no parameters and no context of its own: nulls here, never
    # the other route's advertisement (and never the model-level merge of it).
    assert by_provider["groq"]["supported_parameters"] is None
    assert by_provider["groq"]["supports_tool_choice"] is None
    assert by_provider["groq"]["max_completion_tokens"] is None
    assert by_provider["groq"]["context_length"] == 131072  # Guardian's resolved value


# ── presentation-layer integration ──────────────────────────────────


async def test_endpoint_entry_fields_result_is_used_and_completed(tmp_path, monkeypatch):
    presentation = _make_presentation(
        endpoint_entry_fields=lambda **fields: {"name": "custom-label", "tag": "custom-tag"}
    )
    monkeypatch.setattr(me, "_presentation_modules", (presentation,))
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    endpoint = payload["data"]["endpoints"][0]
    assert endpoint["name"] == "custom-label"
    assert endpoint["tag"] == "custom-tag"
    assert set(me.ENDPOINT_KEYS) <= set(endpoint)
    assert endpoint["uptime_last_30m"] is None


async def test_endpoint_entry_fields_with_an_incompatible_signature_is_tolerated(tmp_path, monkeypatch):
    def incompatible(provider_name, upstream_model):  # noqa: ARG001
        return {"name": "never-used"}

    presentation = _make_presentation(endpoint_entry_fields=incompatible)
    monkeypatch.setattr(me, "_presentation_modules", (presentation,))
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    endpoint = payload["data"]["endpoints"][0]
    assert endpoint["name"] == "openrouter | openai/gpt-4o"
    assert set(me.ENDPOINT_KEYS) <= set(endpoint)


async def test_endpoint_entry_fields_raising_is_fail_open(tmp_path, monkeypatch):
    def broken(**fields):
        raise RuntimeError("boom")

    presentation = _make_presentation(endpoint_entry_fields=broken)
    monkeypatch.setattr(me, "_presentation_modules", (presentation,))
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    assert set(me.ENDPOINT_KEYS) <= set(payload["data"]["endpoints"][0])


async def test_resolve_model_metadata_failure_falls_back(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    presentation = _make_presentation(resolve_model_metadata=broken)
    monkeypatch.setattr(me, "_presentation_modules", (presentation,))
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    assert payload["data"]["name"] == "Openai: gpt-4o"
    assert payload["data"]["architecture"] == {
        "modality": None,
        "input_modalities": None,
        "output_modalities": None,
        "tokenizer": None,
        "instruct_type": None,
    }
    assert "metadata_sources" not in payload["data"]


async def test_missing_presentation_module_still_serves_the_route(tmp_path, monkeypatch):
    monkeypatch.setattr(me, "_presentation_modules", ())
    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    data = payload["data"]
    assert set(me.MODEL_KEYS) <= set(data)
    assert data["name"] == "Openai: gpt-4o"
    assert isinstance(data["created"], int)
    assert world.names(payload) == ["openrouter", "groq"]


def test_attach_registers_the_presentation_module():
    presentation = _make_presentation()
    me.attach(presentation=presentation)

    assert me._presentation_fn("split_identity") is presentation.split_identity
    assert me._presentation_fn("does_not_exist") is None


async def test_end_to_end_with_the_real_presentation_module(tmp_path, monkeypatch):
    """Integration guard against the sibling track's frozen module, when present."""
    pytest.importorskip("app.gateway.model_metadata_presentation")
    monkeypatch.setattr(me, "_attached_presentation", None)
    monkeypatch.setattr(me, "_presentation_modules", None)
    for name in ("split_identity", "resolve_model_metadata", "endpoint_entry_fields"):
        assert me._presentation_fn(name) is not None

    world = _build_world(tmp_path)

    payload = await world.endpoints("openai/gpt-4o")

    data = payload["data"]
    assert set(me.MODEL_KEYS) <= set(data)
    assert isinstance(data["created"], int)
    by_provider = {e["provider_name"]: e for e in data["endpoints"]}
    for endpoint in data["endpoints"]:
        assert set(me.ENDPOINT_KEYS) <= set(endpoint)
        assert endpoint["uptime_last_30m"] is None
        assert endpoint["latency_last_30m"] is None
        assert endpoint["throughput_last_30m"] is None
        assert endpoint["native_tools"] is None
    # openrouter advertises pricing; groq does not (and no reference entry exists).
    assert by_provider["openrouter"]["pricing"] == {
        "prompt": "0.0000025",
        "completion": "0.00001",
    }
    assert by_provider["groq"]["pricing"] is None


def test_presentation_module_is_imported_automatically(tmp_path, monkeypatch):
    """The frozen module name is picked up without an explicit attach()."""
    presentation = _make_presentation()
    monkeypatch.setitem(sys.modules, "app.gateway.model_metadata_presentation", presentation)
    monkeypatch.setattr(me, "_attached_presentation", None)
    monkeypatch.setattr(me, "_presentation_modules", None)

    assert me._presentation_fn("split_identity") is presentation.split_identity


# ── injected-dependency robustness ──────────────────────────────────


async def test_async_injected_callables_are_awaited(tmp_path):
    world = _build_world(tmp_path)

    async def async_attempts(model_id, request, client_id):
        return ([], None)

    async def async_context(model_id, canonical_name=None, cloud_attempts=None):
        return 4242

    me.init(
        _provider_registry=world.registry,
        _cloud_catalog=world.catalog,
        _reference_catalog=None,
        _failover_registry=world.failover,
        _model_manager=world.manager,
        _resolve_cloud_attempts=async_attempts,
        _resolve_context_window=async_context,
    )

    payload = await world.endpoints("openai/gpt-4o")

    assert payload["data"]["endpoints"][0]["context_length"] == 128000
    # A route with no advertisement of its own takes Guardian's resolved value.
    assert payload["data"]["endpoints"][1]["context_length"] == 4242


async def test_failing_cloud_attempt_resolution_does_not_hide_catalog_routes(tmp_path):
    world = _build_world(tmp_path)

    def exploding(model_id, request, client_id):
        raise RuntimeError("router down")

    me.init(
        _provider_registry=world.registry,
        _cloud_catalog=world.catalog,
        _reference_catalog=None,
        _failover_registry=world.failover,
        _model_manager=world.manager,
        _resolve_cloud_attempts=exploding,
        _resolve_context_window=lambda *args, **kwargs: 131072,
    )

    payload = await world.endpoints("openai/gpt-4o")

    assert world.names(payload) == ["openrouter", "groq"]
