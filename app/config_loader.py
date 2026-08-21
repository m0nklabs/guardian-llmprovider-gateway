"""Configuration loading — single source of truth for the config schema.

Config-schema migration (2026-08-21, docs/CONFIG_SCHEMA.md): the monolith
``config/settings.yaml`` is split into domain files.  This module is the
central read switch: it merges ``global.settings.yaml`` (proxy/queue/timeouts/
scaler/capture/grammar/cloud_retry/failover_health/services/benchmark/...),
``providers.settings.yaml`` + ``providers.overrides.yaml`` (provider defaults
and per-provider overrides, overrides win) into the *same* top-level config
dict, so every existing ``CONFIG.get("key")`` consumer stays intact.  It loads
once and exposes typed accessors for the individual settings that used to
re-read the YAML file on every use.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.paths import (
    global_settings_file,
    providers_defaults_file,
    providers_overrides_file,
)
from app.proxy.ratelimit import RateLimitConfig

logger = logging.getLogger("Guardian")

CONFIG_PATH = global_settings_file()


def _load_yaml_map(path: Path) -> dict:
    """Load a YAML file into a dict, or return {} on absence/parse error."""
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to load config from %s: %s", path, e)
        return {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *override* over *base* recursively (override wins)."""
    out = dict(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _merge_providers() -> Dict[str, Any]:
    """Merge providers defaults + overrides (overrides win per provider)."""
    defaults = _load_yaml_map(providers_defaults_file()).get("providers", {}) or {}
    overrides = _load_yaml_map(providers_overrides_file()).get("providers", {}) or {}
    if not isinstance(defaults, dict):
        defaults = {}
    if not isinstance(overrides, dict):
        overrides = {}
    return _deep_merge(defaults, overrides)


def load_config() -> dict:
    """Load configuration from the config-schema files with sensible defaults.

    Merges, in order: built-in defaults → ``global.settings.yaml`` → merged
    providers (from ``providers.settings.yaml`` + ``providers.overrides.yaml``,
    overrides win).  The returned dict keeps the same top-level keys as the
    legacy ``settings.yaml`` (proxy, cloud_retry, grammar, timeouts,
    failover_health, providers, queue, services, ...) so all consumers of the
    shared ``CONFIG`` dict continue to work unchanged.
    """
    default_config: Dict[str, Any] = {
        "proxy": {
            "stream_heartbeat_seconds": 15,
            "stream_close_timeout_seconds": 5,
        },
        "cloud_retry": RateLimitConfig().to_dict(),
        "grammar": {
            "enabled": True,
            "cloud_auto_convert_json": False,
            "cloud_strict_mode": False,
            "validate_gbnf": False,
        },
        "timeouts": {
            "tiers": {
                "tier_70b": {"min_size_mb": 40000, "timeout_seconds": 3600},
                "tier_32b": {"min_size_mb": 20000, "timeout_seconds": 600},
                "tier_13b": {"min_size_mb": 10000, "timeout_seconds": 300},
                "tier_8b": {"min_size_mb": 5000, "timeout_seconds": 180},
                "tier_small": {"min_size_mb": 0, "timeout_seconds": 120},
            },
            "default_timeout": 300
        }
    }

    global_cfg = _load_yaml_map(CONFIG_PATH)
    # Merge with defaults (file config takes precedence)
    for key in ("proxy", "cloud_retry", "grammar", "timeouts"):
        if key in global_cfg:
            default_config[key].update(global_cfg[key])
    if "failover_health" in global_cfg:
        default_config["failover_health"] = global_cfg["failover_health"]

    # Provider section merged from settings + overrides is the canonical config
    # (providers.py still reads its own files directly for cold reads; this
    # keeps the shared CONFIG dict carrying the merged providers as well).
    default_config["providers"] = _merge_providers()

    return default_config


# Loaded once at module level; every accessor below reads from this dict so
# the YAML file is parsed exactly one time per process.
CONFIG = load_config()


def reload_config() -> dict:
    """Atomically re-read settings.yaml into the module-global CONFIG dict.

    Keeps the *same* dict object (all existing references — e.g. the
    ``CONFIG = _config_loader.CONFIG`` alias in server.py and every accessor
    that reads ``CONFIG`` — keep pointing at it) but replaces its contents
    in place.  On any parse error the previous configuration stays fully
    intact (fail-safe: no partial swap, no half-loaded state).

    Returns the new configuration dict (may be the previous one when the
    reload failed).
    """
    try:
        new_config = load_config()
    except Exception as exc:  # defensive: never propagate a reload failure
        logger.warning("⚠️  Config reload failed (%s); keeping previous config", exc)
        return CONFIG
    if not isinstance(new_config, dict):
        logger.warning("⚠️  Config reload produced a non-dict; keeping previous config")
        return CONFIG
    CONFIG.clear()
    CONFIG.update(new_config)
    logger.info("🔄 config files reloaded (config generation bumped)")
    return CONFIG


def load_vram_limit(config: Optional[Dict[str, Any]] = None) -> int:
    """Return the VRAM budget (MB) from ``proxy.vram_limit_mb``."""
    cfg = config if config is not None else CONFIG
    try:
        return int(cfg.get("proxy", {}).get("vram_limit_mb", 27000))
    except (TypeError, ValueError):
        return 27000


def load_stream_heartbeat_interval_s(config: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Return the configured SSE heartbeat interval, or None when disabled."""
    cfg = config if config is not None else CONFIG
    try:
        interval = float(cfg.get("proxy", {}).get("stream_heartbeat_seconds", 15))
    except (TypeError, ValueError):
        interval = 15.0
    return interval if interval > 0 else None


def load_stream_close_timeout_s(config: Optional[Dict[str, Any]] = None) -> float:
    """Return the bounded timeout used for upstream stream cleanup."""
    cfg = config if config is not None else CONFIG
    try:
        timeout = float(cfg.get("proxy", {}).get("stream_close_timeout_seconds", 5))
    except (TypeError, ValueError):
        timeout = 5.0
    return max(timeout, 0.5)


def load_queue_config(config: Optional[Dict[str, Any]] = None) -> dict:
    """Return the ``queue`` section of the configuration."""
    cfg = config if config is not None else CONFIG
    return cfg.get("queue", {}) or {}


def load_grammar_config(config: Optional[Dict[str, Any]] = None) -> dict:
    """Return the ``grammar`` section of the configuration.

    Grammar-Constrained Decoding (GCD) controls. See docs/API_REFERENCE.md
    for the full field semantics.
    """
    cfg = config if config is not None else CONFIG
    return cfg.get("grammar", {}) or {}


def get_grammar_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether GCD is enabled process-wide (kill-switch)."""
    return bool(load_grammar_config(config).get("enabled", True))


def get_grammar_cloud_auto_convert_json(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether JSON-targeting grammars auto-convert to response_format on cloud."""
    return bool(load_grammar_config(config).get("cloud_auto_convert_json", False))


def get_grammar_cloud_strict_mode(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether cloud routes 400 on unsupported grammars instead of stripping."""
    return bool(load_grammar_config(config).get("cloud_strict_mode", False))


def get_grammar_validate_gbnf(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether GBNF grammars are pre-validated before local forwarding."""
    return bool(load_grammar_config(config).get("validate_gbnf", False))
