"""Unit tests for app.proxy.cloud_catalog — dynamic cloud model catalog.

Covers the cloud-access redesign (2026-08-21) catalog: brand normalization,
``resolve_cloud_target`` addressing, cold-start disk-cache restore, refresh
failure keeping the last successful list, and cloud_models.yaml overrides.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

import app.cloud_inference as _cloud_inf
from app.cloud_inference.routing import resolve_cloud_attempts
from app.proxy.cloud_catalog import CloudModelCatalog
from app.proxy.providers import ProviderRegistry


SAMPLE_SETTINGS = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    timeout_seconds: 300
    models:
      - openai/gpt-4o
      - deepseek/deepseek-v4-flash-0731
  google:
    enabled: true
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key: gk-test-key
    timeout_seconds: 300
    models:
      - gemini-3.5-flash
"""


def _write_settings(tmp_path: Path, content: str = SAMPLE_SETTINGS) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _make_catalog(tmp_path: Path, **kwargs) -> CloudModelCatalog:
    registry = ProviderRegistry(settings_path=_write_settings(tmp_path))
    catalog = CloudModelCatalog(
        provider_registry=registry,
        cache_file=tmp_path / "cache.json",
        overrides_file=tmp_path / "overrides.yaml",
        **kwargs,
    )
    return catalog


# ── Brand normalization ───────────────────────────────────────────────


class TestBrandNormalization:
    def test_bare_id_gets_brand_prefix(self):
        # google's bare gemini-x -> google/gemini-x
        assert CloudModelCatalog._normalize_upstream_id("gemini-3.5-flash", "google") == "google/gemini-3.5-flash"
        # openai's bare gpt-4o -> openai/gpt-4o
        assert CloudModelCatalog._normalize_upstream_id("gpt-4o", "openai") == "openai/gpt-4o"

    def test_namespaced_id_preserved(self):
        # nvidia upstream already carries the brand -> preserved unchanged
        assert CloudModelCatalog._normalize_upstream_id("minimaxai/minimax-m3", "nvidia") == "minimaxai/minimax-m3"

    def test_google_models_prefix_stripped(self):
        # google returns models/gemini-... ; the models/ prefix is stripped so it
        # normalizes to google/gemini-... (real 2026-08-21 bug: it was staying
        # as the 2-segment google/models/gemini-...).
        assert CloudModelCatalog._normalize_upstream_id("models/gemini-2.5-flash", "google") == "google/gemini-2.5-flash"
        assert CloudModelCatalog._normalize_upstream_id("models/gemini-3.5-flash", "google") == "google/gemini-3.5-flash"

    def test_empty_id(self):
        assert CloudModelCatalog._normalize_upstream_id("", "google") == ""
        assert CloudModelCatalog._normalize_upstream_id(None, "google") == ""


# ── resolve_cloud_target ─────────────────────────────────────────────


class TestResolveCloudTarget:
    def test_full_address_resolves_to_settings_provider(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        # Populate a catalog for openrouter
        catalog._catalogs["openrouter"] = {
            "fetched_at": 1.0,
            "models": {"deepseek/deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731"},
        }
        target = catalog.resolve_cloud_target("openrouter/deepseek/deepseek-v4-flash-0731")
        assert target == ("openrouter", "deepseek/deepseek-v4-flash-0731")

    def test_full_address_cold_start_falls_back_to_rest(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        # No catalog fetched yet -> upstream falls back to the non-provider segment
        target = catalog.resolve_cloud_target("google/google/gemini-3.5-flash")
        assert target == ("google", "google/gemini-3.5-flash")

    def test_bare_name_with_fallback_provider(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        provider = catalog._registry.get_provider_for_model("openai/gpt-4o")
        assert provider is not None and provider.name == "openrouter"
        catalog._catalogs["openrouter"] = {
            "fetched_at": 1.0,
            "models": {"openai/gpt-4o": "openai/gpt-4o"},
        }
        target = catalog.resolve_cloud_target("openai/gpt-4o", fallback=provider)
        assert target == ("openrouter", "openai/gpt-4o")

    def test_unknown_returns_none(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        assert catalog.resolve_cloud_target("no-such/model") is None


# ── Cold-start disk cache restore ────────────────────────────────────


class TestDiskCache:
    def test_restores_cached_catalog_at_construction(self, tmp_path: Path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "openrouter": {
                "fetched_at": 100.0,
                "models": {"openai/gpt-4o": "openai/gpt-4o"},
                # endpoint source must match the provider's base_url|catalog_url
                "source": "https://openrouter.ai/api/v1|/models",
            },
        }))
        registry = ProviderRegistry(settings_path=_write_settings(tmp_path))
        catalog = CloudModelCatalog(
            provider_registry=registry,
            cache_file=cache_file,
            overrides_file=tmp_path / "overrides.yaml",
        )
        assert catalog.get_models_for_provider("openrouter") == {"openai/gpt-4o": "openai/gpt-4o"}

    def test_stale_cached_catalog_dropped_on_endpoint_change(self, tmp_path: Path):
        # Cache written when openrouter pointed at /models; provider now points
        # at /models/user -> the stale entry must NOT be restored.
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "openrouter": {
                "fetched_at": 100.0,
                "models": {"openai/gpt-4o": "openai/gpt-4o"},
                "source": "https://openrouter.ai/api/v1|/models",
            },
        }))
        settings = _write_settings(
            tmp_path,
            SAMPLE_SETTINGS.replace(
                "base_url: https://openrouter.ai/api/v1\n",
                "base_url: https://openrouter.ai/api/v1\n    catalog_url: /models/user\n",
            ),
        )
        registry = ProviderRegistry(settings_path=settings)
        catalog = CloudModelCatalog(
            provider_registry=registry,
            cache_file=cache_file,
            overrides_file=tmp_path / "overrides.yaml",
        )
        assert catalog.get_models_for_provider("openrouter") == {}


# ── Refresh failure keeps last list ──────────────────────────────────


class TestRefreshFailure:
    @pytest.mark.asyncio
    async def test_failed_refresh_keeps_last_successful_list(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        catalog._catalogs["openrouter"] = {
            "fetched_at": 1.0,
            "models": {"openai/gpt-4o": "openai/gpt-4o"},
        }
        provider = catalog._registry.get_provider_for_model("openai/gpt-4o")

        class FailingClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers):
                raise RuntimeError("catalog outage")

        with patch("app.proxy.cloud_catalog.httpx.AsyncClient", FailingClient):
            result = await catalog.refresh_provider(provider)

        assert result == {"openai/gpt-4o": "openai/gpt-4o"}


# ── Catalog URL override ─────────────────────────────────────────────


class TestCatalogUrlOverride:
    @pytest.mark.asyncio
    async def test_refresh_uses_provider_catalog_url(self, tmp_path: Path):
        settings = tmp_path / "settings.yaml"
        settings.write_text(
            textwrap.dedent(
                """\
                providers:
                  openrouter:
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-or-test-key
                    timeout_seconds: 300
                    catalog_url: /models/user
                """
            )
        )
        registry = ProviderRegistry(settings_path=settings)
        catalog = CloudModelCatalog(
            provider_registry=registry,
            cache_file=tmp_path / "cache.json",
            overrides_file=tmp_path / "overrides.yaml",
        )
        provider = registry.get_provider_for_model("openrouter/deepseek/deepseek-chat")
        assert provider is not None
        assert provider.catalog_url == "/models/user"

        captured = {}

        class CapturingClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers):
                captured["url"] = url
                return _FakeResponse({"data": [{"id": "deepseek/deepseek-chat"}]})

        with patch("app.proxy.cloud_catalog.httpx.AsyncClient", CapturingClient):
            await catalog.refresh_provider(provider)

        assert captured["url"] == "https://openrouter.ai/api/v1/models/user"

    @pytest.mark.asyncio
    async def test_refresh_defaults_to_models(self, tmp_path: Path):
        catalog = _make_catalog(tmp_path)
        provider = catalog._registry.get_provider_for_model("openai/gpt-4o")
        assert provider is not None
        assert provider.catalog_url is None
        captured = {}

        class CapturingClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers):
                captured["url"] = url
                return _FakeResponse({"data": [{"id": "gpt-4o"}]})

        with patch("app.proxy.cloud_catalog.httpx.AsyncClient", CapturingClient):
            await catalog.refresh_provider(provider)
        assert captured["url"] == "https://openrouter.ai/api/v1/models"


class _FakeResponse:
    """Minimal stand-in for an httpx.Response used in tests."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# ── Overrides loading ────────────────────────────────────────────────


class TestOverrides:
    def test_loads_overrides_and_get_override(self, tmp_path: Path):
        overrides = tmp_path / "overrides.yaml"
        overrides.write_text(textwrap.dedent("""\
            openrouter/deepseek/deepseek-v4-flash-0731:
              context_window: 1048576
            gpt-4o:
              thinking: false
        """))
        registry = ProviderRegistry(settings_path=_write_settings(tmp_path))
        catalog = CloudModelCatalog(
            provider_registry=registry,
            cache_file=tmp_path / "cache.json",
            overrides_file=overrides,
        )
        full = catalog.get_override("openrouter/deepseek/deepseek-v4-flash-0731")
        assert full == {"context_window": 1048576}
        assert catalog.get_override("gpt-4o") == {"thinking": False}
        assert catalog.get_override("missing") is None

    def test_get_model_overrides_full_address(self, tmp_path: Path):
        overrides = tmp_path / "overrides.yaml"
        overrides.write_text(textwrap.dedent("""\
            openrouter/deepseek/deepseek-v4-flash-0731:
              context_window: 1048576
        """))
        registry = ProviderRegistry(settings_path=_write_settings(tmp_path))
        catalog = CloudModelCatalog(
            provider_registry=registry,
            cache_file=tmp_path / "cache.json",
            overrides_file=overrides,
        )
        assert catalog.get_model_overrides("openrouter/deepseek/deepseek-v4-flash-0731") == {
            "context_window": 1048576
        }


# ── cloud_gateway_access gating (routing) ────────────────────────────


@pytest.fixture
def restore_routing_globals():
    """Snapshot + restore the routing module's injected globals.

    The gating tests drive ``app.cloud_inference.routing`` via ``init()``,
    which permanently overwrites its module globals.  Restoring them keeps
    those mutations from leaking into other tests (e.g. test_server.py).
    """
    import app.cloud_inference.routing as _routing

    names = (
        "_provider_registry",
        "_cloud_catalog",
        "_failover_registry",
        "_failover_health",
        "_get_request_auth_context",
        "_cloud_provider_for_request",
        "_cloud_provider_unavailable_error",
        "_adapt_openai_reasoning_params",
    )
    saved = {name: getattr(_routing, name) for name in names}
    yield
    for name, val in saved.items():
        setattr(_routing, name, val)


def _init_routing(tmp_path: Path, *, cloud_gateway_access: bool):
    registry = ProviderRegistry(settings_path=_write_settings(tmp_path))
    catalog = CloudModelCatalog(
        provider_registry=registry,
        cache_file=tmp_path / "cache.json",
        overrides_file=tmp_path / "overrides.yaml",
    )
    from app.cloud_inference import routing as _routing
    _routing.init(
        registry,
        catalog,
        None,  # failover_registry
        None,  # failover_health
        lambda request: {"cloud_gateway_access": cloud_gateway_access},
        lambda req, cid: None,
        lambda req: "",
        lambda *a, **k: None,
        lambda: None,
        registry.get_provider_for_model,
        _cloud_inf.cloud_provider_unavailable_error,
        lambda provider, upstream, body: body,
    )
    return _routing


class _FakeRequest:
    pass


class TestCloudGatewayGate:
    def test_denied_key_raises_403(self, tmp_path: Path, restore_routing_globals):
        _init_routing(tmp_path, cloud_gateway_access=False)
        with pytest.raises(Exception) as exc_info:
            resolve_cloud_attempts("openrouter/deepseek/deepseek-v4-flash-0731", _FakeRequest(), "client")
        assert exc_info.value.status_code == 403

    def test_allowed_key_resolves_via_settings_provider(self, tmp_path: Path, restore_routing_globals):
        _init_routing(tmp_path, cloud_gateway_access=True)
        attempts, failover_group = resolve_cloud_attempts(
            "openrouter/deepseek/deepseek-v4-flash-0731", _FakeRequest(), "client"
        )
        assert failover_group is None
        assert len(attempts) == 1
        provider, upstream = attempts[0]
        assert provider.name == "openrouter"
        assert provider.base_url == "https://openrouter.ai/api/v1"
        assert upstream == "deepseek/deepseek-v4-flash-0731"

    def test_missing_flag_defaults_to_allowed(self, tmp_path: Path, restore_routing_globals):
        registry = ProviderRegistry(settings_path=_write_settings(tmp_path))
        catalog = CloudModelCatalog(
            provider_registry=registry,
            cache_file=tmp_path / "cache.json",
            overrides_file=tmp_path / "overrides.yaml",
        )
        from app.cloud_inference import routing as _routing
        _routing.init(
            registry,
            catalog,
            None, None,
            lambda request: {},  # no cloud_gateway_access key -> defaults True
            lambda req, cid: None,
            lambda req: "",
            lambda *a, **k: None,
            lambda: None,
            registry.get_provider_for_model,
            _cloud_inf.cloud_provider_unavailable_error,
            lambda provider, upstream, body: body,
        )
        attempts, _ = resolve_cloud_attempts(
            "openrouter/openai/gpt-4o", _FakeRequest(), "client"
        )
        assert attempts[0][0].name == "openrouter"
        assert attempts[0][1] == "openai/gpt-4o"
