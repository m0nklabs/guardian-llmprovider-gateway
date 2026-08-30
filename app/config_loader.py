"""Configuration loading — single source of truth for the config schema.

Config-schema migration (2026-08-21, docs/CONFIG_SCHEMA.md): the monolith
``config/settings.yaml`` is split into domain files.  This module is the
central read switch: it deep-merges the full ``global.settings.yaml`` document
into the shared top-level config dict, then overlays the per-provider config
(F2, docs/CONFIG_PROVIDER_FILES.md) — a directory scan of
``config/providers/*.settings.yaml`` — as the canonical ``providers`` section.
It loads once and exposes typed accessors for the individual settings that used
to re-read YAML on every use.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.paths import (
    global_settings_file,
    provider_names,
    provider_settings_file,
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


def provider_settings_documents() -> Dict[str, Any]:
    """Read every ``config/providers/*.settings.yaml`` into ``{name: doc}``.

    F2 directory-scan (docs/CONFIG_PROVIDER_FILES.md): one document per
    provider, replacing the old defaults/overrides merge.  Each document
    carries the provider's own keys (enabled/base_url/api_key/… + a ``models:``
    block).  A missing/unparseable file yields ``{}`` for that provider rather
    than failing the whole scan.
    """
    documents: Dict[str, Any] = {}
    for name in provider_names():
        documents[name] = _load_yaml_map(provider_settings_file(name))
    return documents


def _merge_providers() -> Dict[str, Any]:
    """Scan the ``providers/`` directory into ``{provider_name: document}``."""
    return provider_settings_documents()


# Caretaker-runtime poll windows (F5 follow-up).  Defaults mirror the values
# that were hardcoded in the caretaker runtime pre-config; every accessor
# reads the LIVE CONFIG dict, so a POST /api/config/reload changes the
# effective windows without a restart.
_CARETAKER_DEFAULTS = {
    "adopt_poll_seconds": 120,
    "rebind_poll_attempts": 15,
    "rebind_poll_interval_seconds": 1.0,
    "client_timeout_seconds": 5.0,
}
# Safety bounds: a too-short adopt window would 503 legitimate cold loads
# (the in-flight /ensure is the only controller); an unbounded one holds the
# model-switch lock forever.  A client timeout of 0 disables the daemon path
# entirely in practice, so the floor keeps the remote-first design alive.
_CARETAKER_BOUNDS = {
    "adopt_poll_seconds": (1.0, 600.0),
    "rebind_poll_attempts": (0, 60),
    "rebind_poll_interval_seconds": (0.1, 30.0),
    "client_timeout_seconds": (0.5, 120.0),
}


def load_config() -> dict:
    """Load configuration from the config-schema files with sensible defaults.

    Merges, in order: built-in defaults → the full ``global.settings.yaml``
    document → the per-provider directory scan (``providers/*.settings.yaml``,
    one document per provider).  This keeps shared ``CONFIG.get("key")``
    consumers like the inference queue on the configured values while direct
    readers/writers of the domain files continue to work.
    """
    default_config: Dict[str, Any] = {
        "proxy": {
            "stream_heartbeat_seconds": 15,
            "stream_close_timeout_seconds": 5,
        },
        "cloud_retry": RateLimitConfig().to_dict(),
        "caretaker": dict(_CARETAKER_DEFAULTS),
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
    default_config = _deep_merge(default_config, global_cfg)

    # Provider section from the per-provider directory scan is the canonical
    # config (providers.py still reads its own files directly for cold reads;
    # this keeps the shared CONFIG dict carrying the providers as well).
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


def load_caretaker_runtime_config(config: Optional[Dict[str, Any]] = None) -> dict:
    """Return the caretaker-runtime poll windows (typed + bounded).

    Reads the ``caretaker`` section of global.settings.yaml; unknown/invalid
    values fall back to the default, and out-of-bounds values are clamped to
    the safety bounds (fail-safe: a config typo cannot disable the daemon
    path or hold the switch lock forever).
    """
    cfg = config if config is not None else CONFIG
    section = cfg.get("caretaker", {})
    if not isinstance(section, dict):
        section = {}
    result: Dict[str, Any] = {}
    for key, default in _CARETAKER_DEFAULTS.items():
        try:
            value = float(section.get(key, default))
        except (TypeError, ValueError):
            value = float(default)
        lo, hi = _CARETAKER_BOUNDS[key]
        result[key] = max(lo, min(hi, value))
    # attempts is a count: back to int after the shared float coercion.
    result["rebind_poll_attempts"] = int(result["rebind_poll_attempts"])
    return result


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
