"""Configuration loading — single source of truth for settings.yaml.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Pure module (no server dependencies): loads ``config/settings.yaml`` once,
merges it over the built-in defaults, and exposes typed accessors for the
individual settings that used to re-read the YAML file on every use.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.paths import CONFIG_DIR
from app.proxy.ratelimit import RateLimitConfig

logger = logging.getLogger("Guardian")

CONFIG_PATH = CONFIG_DIR / "settings.yaml"


def load_config() -> dict:
    """Load configuration from settings.yaml with sensible defaults."""
    default_config: Dict[str, Any] = {
        "proxy": {
            "stream_heartbeat_seconds": 15,
            "stream_close_timeout_seconds": 5,
        },
        "cloud_retry": RateLimitConfig().to_dict(),
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

    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'r') as f:
                file_config = yaml.safe_load(f) or {}
            # Merge with defaults (file config takes precedence)
            if "proxy" in file_config:
                default_config["proxy"].update(file_config["proxy"])
            if "cloud_retry" in file_config:
                default_config["cloud_retry"].update(file_config["cloud_retry"])
            if "timeouts" in file_config:
                default_config["timeouts"].update(file_config["timeouts"])
            if "failover_health" in file_config:
                default_config["failover_health"] = file_config["failover_health"]
            return default_config
    except Exception as e:
        logger.warning(f"Failed to load config from {CONFIG_PATH}: {e}. Using defaults.")

    return default_config


# Loaded once at module level; every accessor below reads from this dict so
# the YAML file is parsed exactly one time per process.
CONFIG = load_config()


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
