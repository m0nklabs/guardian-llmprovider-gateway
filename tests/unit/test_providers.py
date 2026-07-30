"""Unit tests for app.proxy.providers — cloud LLM provider registry.

These tests verify that the ProviderRegistry correctly loads provider
configuration from settings.yaml, maps model names to providers, builds
forwarding headers/URLs, and handles edge cases like disabled providers,
missing API keys, and environment-variable expansion.
"""

import os
import textwrap
from pathlib import Path

import pytest

from app.proxy.providers import (
    CloudProvider,
    ProviderRegistry,
    _expand_env,
)


# ── Fixtures ───────────────────────────────────────────────────────────


SAMPLE_PROVIDERS_YAML = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-test-key
    timeout_seconds: 300
    models:
      - anthropic/claude-3.5-sonnet
      - openai/gpt-4o
      - google/gemini-2.0-flash-exp
  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: nvapi-test-key
    timeout_seconds: 600
    models:
      - nvidia/llama-3.1-nemotron-70b-instruct
      - deepseek-ai/deepseek-r1
"""

DISABLED_PROVIDER_YAML = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-key
    models:
      - openai/gpt-4o
  disabled_one:
    enabled: false
    base_url: https://example.com/v1
    api_key: some-key
    models:
      - example/disabled-model
"""

NO_KEY_PROVIDER_YAML = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: ""
    models:
      - openai/gpt-4o
"""

ENV_VAR_YAML = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: ${TEST_OPENROUTER_KEY}
    models:
      - openai/gpt-4o
"""

DUPLICATE_MODEL_YAML = """\
providers:
  openrouter:
    enabled: true
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-key
    models:
      - shared/model
      - openai/gpt-4o
  nvidia:
    enabled: true
    base_url: https://integrate.api.nvidia.com/v1
    api_key: nvapi-key
    models:
      - shared/model
      - nvidia/llama-3.1-nemotron-70b-instruct
"""


def _write_settings(tmp_path: Path, content: str) -> Path:
    """Write a settings.yaml snippet to a temp file and return its path."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(textwrap.dedent(content))
    return settings


@pytest.fixture
def settings_with_providers(tmp_path: Path) -> Path:
    return _write_settings(tmp_path, SAMPLE_PROVIDERS_YAML)


@pytest.fixture
def settings_disabled(tmp_path: Path) -> Path:
    return _write_settings(tmp_path, DISABLED_PROVIDER_YAML)


@pytest.fixture
def settings_no_key(tmp_path: Path) -> Path:
    return _write_settings(tmp_path, NO_KEY_PROVIDER_YAML)


@pytest.fixture
def settings_env_var(tmp_path: Path) -> Path:
    return _write_settings(tmp_path, ENV_VAR_YAML)


@pytest.fixture
def settings_duplicate(tmp_path: Path) -> Path:
    return _write_settings(tmp_path, DUPLICATE_MODEL_YAML)


# ── _expand_env ────────────────────────────────────────────────────────


class TestExpandEnv:
    def test_expands_known_var(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "hello")
        assert _expand_env("${MY_TEST_VAR}") == "hello"

    def test_unknown_var_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR_12345", raising=False)
        assert _expand_env("${NONEXISTENT_VAR_12345}") == ""

    def test_plain_string_unchanged(self):
        assert _expand_env("sk-or-plain-key") == "sk-or-plain-key"

    def test_embedded_var(self, monkeypatch):
        monkeypatch.setenv("PREFIX", "sk")
        assert _expand_env("${PREFIX}-or-key") == "sk-or-key"


# ── ProviderRegistry loading ───────────────────────────────────────────


class TestRegistryLoading:
    def test_loads_two_providers(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        assert len(reg.get_enabled_providers()) == 2

    def test_cloud_models_detected(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        assert reg.is_cloud_model("anthropic/claude-3.5-sonnet")
        assert reg.is_cloud_model("openai/gpt-4o")
        assert reg.is_cloud_model("nvidia/llama-3.1-nemotron-70b-instruct")
        assert reg.is_cloud_model("deepseek-ai/deepseek-r1")

    def test_local_model_not_cloud(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        assert not reg.is_cloud_model("Qwen3-30B-A3B")
        assert not reg.is_cloud_model("local-model")
        assert not reg.is_cloud_model("")

    def test_get_all_cloud_models(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        models = set(reg.get_all_cloud_models())
        assert models == {
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "google/gemini-2.0-flash-exp",
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "deepseek-ai/deepseek-r1",
        }

    def test_get_provider_for_model(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        p = reg.get_provider_for_model("openai/gpt-4o")
        assert p is not None
        assert p.name == "openrouter"
        assert p.base_url == "https://openrouter.ai/api/v1"
        assert p.api_key == "sk-or-test-key"
        assert p.is_configured

    def test_get_provider_returns_none_for_unknown(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        assert reg.get_provider_for_model("unknown/model") is None


# ── Disabled providers ─────────────────────────────────────────────────


class TestDisabledProviders:
    def test_disabled_provider_models_not_served(self, settings_disabled: Path):
        reg = ProviderRegistry(settings_path=settings_disabled)
        assert reg.is_cloud_model("openai/gpt-4o")  # from enabled openrouter
        assert not reg.is_cloud_model("example/disabled-model")  # from disabled

    def test_disabled_provider_not_in_enabled_list(self, settings_disabled: Path):
        reg = ProviderRegistry(settings_path=settings_disabled)
        enabled = reg.get_enabled_providers()
        names = [p.name for p in enabled]
        assert "openrouter" in names
        assert "disabled_one" not in names


# ── Missing API key ────────────────────────────────────────────────────


class TestMissingApiKey:
    def test_provider_with_empty_key_not_configured(self, settings_no_key: Path):
        reg = ProviderRegistry(settings_path=settings_no_key)
        p = reg.get_provider_for_model("openai/gpt-4o")
        assert p is not None
        assert not p.is_configured
        assert p.api_key == ""


# ── Environment variable expansion ─────────────────────────────────────


class TestEnvVarExpansion:
    def test_api_key_expanded_from_env(self, settings_env_var: Path, monkeypatch):
        monkeypatch.setenv("TEST_OPENROUTER_KEY", "sk-or-from-env")
        reg = ProviderRegistry(settings_path=settings_env_var)
        p = reg.get_provider_for_model("openai/gpt-4o")
        assert p.api_key == "sk-or-from-env"
        assert p.is_configured

    def test_missing_env_var_results_in_empty_key(self, settings_env_var: Path, monkeypatch):
        monkeypatch.delenv("TEST_OPENROUTER_KEY", raising=False)
        reg = ProviderRegistry(settings_path=settings_env_var)
        p = reg.get_provider_for_model("openai/gpt-4o")
        assert p.api_key == ""
        assert not p.is_configured


# ── Duplicate model handling ───────────────────────────────────────────


class TestDuplicateModels:
    def test_first_provider_wins_for_duplicate_model(self, settings_duplicate: Path):
        reg = ProviderRegistry(settings_path=settings_duplicate)
        p = reg.get_provider_for_model("shared/model")
        assert p is not None
        assert p.name == "openrouter"  # first provider wins

    def test_both_providers_other_models_loaded(self, settings_duplicate: Path):
        reg = ProviderRegistry(settings_path=settings_duplicate)
        assert reg.is_cloud_model("openai/gpt-4o")
        assert reg.is_cloud_model("nvidia/llama-3.1-nemotron-70b-instruct")


# ── Model metadata ─────────────────────────────────────────────────────


class TestModelMetadata:
    def test_build_metadata_entry(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        entry = reg.build_model_metadata_entry("openai/gpt-4o")
        assert entry is not None
        assert entry["id"] == "openai/gpt-4o"
        assert entry["object"] == "model"
        assert entry["owned_by"] == "openrouter"
        assert entry["served_by"] == "cloud"
        assert entry["provider"] == "openrouter"

    def test_build_metadata_returns_none_for_unknown(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        assert reg.build_model_metadata_entry("unknown/model") is None


# ── Forwarding helpers ─────────────────────────────────────────────────


class TestForwardingHelpers:
    def test_build_forward_headers_openrouter(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        p = reg.get_provider_for_model("openai/gpt-4o")
        headers = ProviderRegistry.build_forward_headers(p)
        assert headers["Authorization"] == "Bearer sk-or-test-key"
        assert headers["Content-Type"] == "application/json"
        # OpenRouter-specific attribution headers
        assert headers["HTTP-Referer"] == "https://guardian.local"
        assert headers["X-Title"] == "Guardian"
        # Response caching is enabled by default for OpenRouter
        assert headers["X-OpenRouter-Cache"] == "true"

    def test_build_forward_headers_nvidia(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        p = reg.get_provider_for_model("nvidia/llama-3.1-nemotron-70b-instruct")
        headers = ProviderRegistry.build_forward_headers(p)
        assert headers["Authorization"] == "Bearer nvapi-test-key"
        assert headers["Content-Type"] == "application/json"
        # NVIDIA doesn't get OpenRouter-specific headers
        assert "HTTP-Referer" not in headers
        assert "X-Title" not in headers
        assert "X-OpenRouter-Cache" not in headers

    def test_build_forward_headers_accepts_client_user_id(self, settings_with_providers: Path):
        """The client_user_id parameter is accepted but not sent as a header
        for OpenRouter — it goes in the request body ``user`` field instead."""
        reg = ProviderRegistry(settings_path=settings_with_providers)
        p = reg.get_provider_for_model("openai/gpt-4o")
        headers = ProviderRegistry.build_forward_headers(p, client_user_id="fp_abc123")
        assert headers["Authorization"] == "Bearer sk-or-test-key"
        # No X-User-Id header — OpenRouter uses the body `user` field
        assert "X-User-Id" not in headers

    def test_build_forward_headers_cache_overridable_via_extra_headers(self, tmp_path: Path):
        """Provider extra_headers can override the default cache setting."""
        settings = _write_settings(
            tmp_path,
            """\
            providers:
              openrouter:
                enabled: true
                base_url: https://openrouter.ai/api/v1
                api_key: sk-or-key
                models:
                  - openai/gpt-4o
                extra_headers:
                  X-OpenRouter-Cache: "false"
            """,
        )
        reg = ProviderRegistry(settings_path=settings)
        p = reg.get_provider_for_model("openai/gpt-4o")
        headers = ProviderRegistry.build_forward_headers(p)
        assert headers["X-OpenRouter-Cache"] == "false"

    def test_build_forward_url(self, settings_with_providers: Path):
        reg = ProviderRegistry(settings_path=settings_with_providers)
        p = reg.get_provider_for_model("openai/gpt-4o")
        url = ProviderRegistry.build_forward_url(p, "chat/completions")
        assert url == "https://openrouter.ai/api/v1/chat/completions"

    def test_build_forward_url_strips_trailing_slash(self, tmp_path: Path):
        settings = _write_settings(
            tmp_path,
            """\
            providers:
              test_provider:
                enabled: true
                base_url: https://example.com/v1/
                api_key: test
                models:
                  - test/model
            """,
        )
        reg = ProviderRegistry(settings_path=settings)
        p = reg.get_provider_for_model("test/model")
        url = ProviderRegistry.build_forward_url(p, "completions")
        assert url == "https://example.com/v1/completions"


# ── Hot reload ─────────────────────────────────────────────────────────


class TestHotReload:
    def test_reload_picks_up_new_models(self, tmp_path: Path):
        settings = _write_settings(
            tmp_path,
            """\
            providers:
              openrouter:
                enabled: true
                base_url: https://openrouter.ai/api/v1
                api_key: sk-or-key
                models:
                  - openai/gpt-4o
            """,
        )
        reg = ProviderRegistry(settings_path=settings)
        assert reg.is_cloud_model("openai/gpt-4o")
        assert not reg.is_cloud_model("anthropic/claude-3.5-sonnet")

        # Rewrite the file with an additional model
        settings.write_text(
            textwrap.dedent(
                """\
                providers:
                  openrouter:
                    enabled: true
                    base_url: https://openrouter.ai/api/v1
                    api_key: sk-or-key
                    models:
                      - openai/gpt-4o
                      - anthropic/claude-3.5-sonnet
                """
            )
        )
        reg.reload()
        assert reg.is_cloud_model("openai/gpt-4o")
        assert reg.is_cloud_model("anthropic/claude-3.5-sonnet")


# ── Empty / missing config ─────────────────────────────────────────────


class TestEmptyConfig:
    def test_no_providers_section(self, tmp_path: Path):
        settings = _write_settings(tmp_path, "proxy:\n  port: 11434\n")
        reg = ProviderRegistry(settings_path=settings)
        assert reg.get_all_cloud_models() == []
        assert not reg.is_cloud_model("any/model")

    def test_missing_settings_file(self, tmp_path: Path):
        reg = ProviderRegistry(settings_path=tmp_path / "nonexistent.yaml")
        assert reg.get_all_cloud_models() == []
        assert not reg.is_cloud_model("any/model")
