"""Cross-provider failover for Guardian's cloud LLM router.

Some logical models are available through more than one upstream cloud
provider (e.g. ``minimax/minimax-m3`` on both NVIDIA NIM and OpenRouter).
This module lets Guardian route requests through a *failover group* — an
ordered list of ``(provider, model)`` candidates for the same logical model —
so a degraded or erroring provider is skipped in favour of the next healthy
candidate, without the caller (Claude Code, etc.) needing to know or care
which upstream is currently serving the request.

Failover groups are configured under a top-level ``failover_groups`` key in
``config/settings.yaml`` (cloud-access redesign; the legacy
``config/cloud_keys.json`` key is still honoured as a backward-compat
fallback)::

    failover_groups:
      minimax-m3:
        candidates:
          - {provider: nvidia, model: minimaxai/minimax-m3}
          - {provider: openrouter, model: minimaxai/minimax-m3}

A client addresses the group with the ``failover/{group}`` route,
e.g. ``failover/minimax-m3``. Guardian tries each candidate in
priority order — skipping any that are currently tripped by the in-memory
circuit breaker (:class:`ProviderHealthTracker`) — and automatically retries
a tripped candidate once its cooldown expires, so it recovers back to the
preferred provider without manual intervention.

Every candidate is served with its provider's *settings* API key, gated on
the caller's ``cloud_gateway_access`` boolean; unconfigured or
poor-health candidates are skipped silently.

This is intentionally a simple, in-process circuit breaker — state resets on
Guardian restart and is not shared across workers. It is not a substitute for
proper upstream monitoring, just enough to keep a coding session running
when one provider has a bad day.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

import yaml

from app.paths import global_settings_file

logger = logging.getLogger("Guardian.Failover")

#: Path to the legacy on-disk store that used to hold failover group
#: definitions (shared with the removed per-key cloud credential store). Since
#: the cloud-access redesign (2026-08-21) groups come from settings.yaml
#: ``failover_groups:``; this file remains only as a backward-compat fallback.
FAILOVER_CONFIG_FILE: Path = Path(__file__).parent.parent.parent / "config" / "cloud_keys.json"

#: Consecutive failures before a (provider, model) candidate is tripped.
#: Overridden by settings.yaml ``failover_health.failure_threshold`` at startup.
FAILURE_THRESHOLD = 3

#: How long a tripped candidate is skipped before being retried (half-open).
#: Overridden by settings.yaml ``failover_health.cooldown_seconds`` at startup.
COOLDOWN_SECONDS = 60.0

#: How long a 429-rate-limited candidate is skipped before being probed again.
#: Overridden by settings.yaml ``failover_health.rate_limit_cooldown_seconds``.
RATE_LIMIT_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True)
class FailoverCandidate:
    """A single ``(provider, model)`` candidate within a failover group.

    ``modalities`` declares which input types the upstream model supports
    (e.g. ``("text", "image")`` for a vision-capable cloud model, or just
    ``("text",)`` for a text-only model). Defaults to text-only for
    backwards compatibility with existing configs that don't specify it.
    """

    provider: str
    model: str
    modalities: Tuple[str, ...] = ("text",)


@dataclass(frozen=True)
class FailoverGroup:
    """An ordered list of interchangeable provider candidates for one logical model.

    When all candidates are text-only (no ``"image"`` in their ``modalities``)
    and a request contains image inputs, Guardian transparently redirects to
    ``image_fallback_model`` — a local vision-capable model — instead of
    forwarding the image to a cloud model that cannot handle it.
    """

    name: str
    candidates: List[FailoverCandidate] = field(default_factory=list)
    image_fallback_model: Optional[str] = None

    def has_image_capable_candidate(self) -> bool:
        """Return True if any candidate declares image input support."""
        return any("image" in c.modalities for c in self.candidates)


class ProviderHealthTracker:
    """In-memory circuit breaker for cloud provider candidates.

    Tracks consecutive failures per ``(provider, model)`` pair. Once a
    candidate reaches :data:`FAILURE_THRESHOLD` consecutive failures it is
    "tripped" and skipped by :meth:`order_candidates` for
    :data:`COOLDOWN_SECONDS`, after which it gets a half-open retry.
    """

    def __init__(
        self,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        rate_limit_cooldown_seconds: float = RATE_LIMIT_COOLDOWN_SECONDS,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self._lock = Lock()
        self._consecutive_failures: Dict[Tuple[str, str], int] = {}
        self._tripped_until: Dict[Tuple[str, str], float] = {}
        self._rate_limited_until: Dict[Tuple[str, str], float] = {}

    def reconfigure(
        self,
        failure_threshold: Optional[int] = None,
        cooldown_seconds: Optional[float] = None,
        rate_limit_cooldown_seconds: Optional[float] = None,
    ) -> None:
        """Update health-tracker thresholds in place (config reload, no restart).

        Only the values that are not ``None`` are changed; existing in-memory
        tripped/rate-limited state is preserved so an in-flight cooldown keeps
        working across the reload.
        """
        with self._lock:
            if failure_threshold is not None:
                self._failure_threshold = max(int(failure_threshold), 1)
            if cooldown_seconds is not None:
                self._cooldown_seconds = max(float(cooldown_seconds), 0.0)
            if rate_limit_cooldown_seconds is not None:
                self._rate_limit_cooldown_seconds = max(float(rate_limit_cooldown_seconds), 0.0)
        logger.info(
            "🔧 Failover health tuned live: threshold=%d cooldown=%.0fs rl_cooldown=%.0fs",
            self._failure_threshold, self._cooldown_seconds, self._rate_limit_cooldown_seconds,
        )

    def record_success(self, provider: str, model: str) -> None:
        """Reset all failure and rate-limit state for *provider*/*model*."""
        key = (provider, model)
        with self._lock:
            self._consecutive_failures.pop(key, None)
            self._tripped_until.pop(key, None)
            self._rate_limited_until.pop(key, None)

    def record_failure(self, provider: str, model: str) -> None:
        """Record a failed request and trip the breaker past the threshold."""
        key = (provider, model)
        with self._lock:
            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self._failure_threshold:
                self._tripped_until[key] = time.time() + self._cooldown_seconds
                logger.warning(
                    "🔴 Failover: '%s/%s' tripped after %d consecutive failures; "
                    "skipping for %.0fs",
                    provider,
                    model,
                    count,
                    self._cooldown_seconds,
                )

    def is_tripped(self, provider: str, model: str) -> bool:
        """Return True if *provider*/*model* is currently within its cooldown window."""
        key = (provider, model)
        with self._lock:
            until = self._tripped_until.get(key)
            if until is None:
                return False
            if time.time() >= until:
                # Cooldown expired — clear the trip and allow a half-open retry.
                self._tripped_until.pop(key, None)
                self._consecutive_failures.pop(key, None)
                return False
            return True

    # ── 429 rate-limit tracking (separate from circuit breaker) ──────────
    # When a cloud provider returns HTTP 429 (Too Many Requests), it's not
    # broken — it's just busy.  Instead of retrying within the same request
    # (which blocks the caller), Guardian marks the provider as rate-limited
    # for a short cooldown.  Subsequent requests skip the provider and fall
    # through to the next candidate immediately, keeping latency low.  After
    # the cooldown, one request acts as a half-open probe: if the provider
    # has recovered, it becomes healthy again; if it's still 429, the
    # cooldown resets.

    def record_rate_limited(self, provider: str, model: str) -> None:
        """Mark *provider*/*model* as rate-limited (HTTP 429).

        The candidate is temporarily skipped by :meth:`order_candidates`,
        allowing concurrent requests to fall through to the next candidate
        without waiting.  After ``rate_limit_cooldown_seconds`` the provider
        gets a half-open probe.
        """
        key = (provider, model)
        with self._lock:
            self._rate_limited_until[key] = time.time() + self._rate_limit_cooldown_seconds
            logger.info(
                "🟡 Failover: '%s/%s' rate-limited (429); skipping for %.0fs "
                "— concurrent requests use next candidate",
                provider,
                model,
                self._rate_limit_cooldown_seconds,
            )

    def is_rate_limited(self, provider: str, model: str) -> bool:
        """Return True if *provider*/*model* is in 429 rate-limit cooldown."""
        key = (provider, model)
        with self._lock:
            until = self._rate_limited_until.get(key)
            if until is None:
                return False
            if time.time() >= until:
                # Cooldown expired — allow a half-open probe.
                self._rate_limited_until.pop(key, None)
                return False
            return True

    def clear_rate_limit(self, provider: str, model: str) -> None:
        """Clear 429 rate-limit state so a half-open probe can proceed."""
        key = (provider, model)
        with self._lock:
            self._rate_limited_until.pop(key, None)

    def order_candidates(self, candidates: List[FailoverCandidate]) -> List[FailoverCandidate]:
        """Return *candidates* healthy-first, then rate-limited, then tripped.

        Healthy candidates are tried first (in configured priority order).
        Rate-limited candidates (429 cooldown) come next — they're skipped
        normally but included so the failover loop can use them if they're
        the only option.  Tripped candidates (circuit breaker) come last.
        """
        healthy: List[FailoverCandidate] = []
        rate_limited: List[FailoverCandidate] = []
        tripped: List[FailoverCandidate] = []
        for c in candidates:
            if self.is_tripped(c.provider, c.model):
                tripped.append(c)
            elif self.is_rate_limited(c.provider, c.model):
                rate_limited.append(c)
            else:
                healthy.append(c)
        return healthy + rate_limited + tripped


class FailoverRegistry:
    """Loads ``failover_groups`` definitions.

    Since the cloud-access redesign (2026-08-21) groups come from the
    ``failover_groups:`` key in ``config/settings.yaml``.  For backward
    compatibility, when settings.yaml lacks it the legacy ``cloud_keys.json``
    key is used.

    Cheap to reconstruct; call :meth:`reload` after editing the config to
    pick up new/changed groups without restarting Guardian.
    """

    def __init__(self, path: Path = FAILOVER_CONFIG_FILE) -> None:
        self._path = path
        self._groups: Dict[str, FailoverGroup] = {}
        self.reload()

    def _load_raw_groups(self) -> dict:
        """Return the ``failover_groups`` map (global.settings.yaml, else legacy file)."""
        try:
            settings_path = global_settings_file()
            if settings_path.exists():
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = yaml.safe_load(f) or {}
                if isinstance(settings, dict):
                    fg = settings.get("failover_groups")
                    if isinstance(fg, dict):
                        return fg
        except Exception as e:
            logger.warning("⚠️  Failed to read failover_groups from settings.yaml: %s", e)

        try:
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                if isinstance(data, dict):
                    fg = data.get("failover_groups")
                    if isinstance(fg, dict):
                        return fg
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("⚠️  Failed to load failover groups from %s: %s", self._path, e)
        return {}

    def reload(self) -> None:
        """Re-read failover group definitions from disk."""
        self._groups.clear()
        raw_groups = self._load_raw_groups()

        for group_name, raw_group in raw_groups.items():
            if not isinstance(raw_group, dict):
                continue
            raw_candidates = raw_group.get("candidates")
            if not isinstance(raw_candidates, list):
                continue
            candidates: List[FailoverCandidate] = []
            for c in raw_candidates:
                if not isinstance(c, dict) or not c.get("provider") or not c.get("model"):
                    continue
                raw_mods = c.get("modalities")
                if isinstance(raw_mods, list):
                    modalities = tuple(str(m) for m in raw_mods)
                else:
                    modalities = ("text",)  # default: text-only (backwards compat)
                candidates.append(FailoverCandidate(
                    provider=str(c["provider"]),
                    model=str(c["model"]),
                    modalities=modalities,
                ))
            if not candidates:
                continue
            # Optional: local vision model to use when all candidates are
            # text-only and the request contains image inputs.
            raw_fallback = raw_group.get("image_fallback", {})
            image_fallback_model = None
            if isinstance(raw_fallback, dict):
                m = str(raw_fallback.get("local_model", "")).strip()
                if m:
                    image_fallback_model = m
            self._groups[str(group_name)] = FailoverGroup(
                name=str(group_name),
                candidates=candidates,
                image_fallback_model=image_fallback_model,
            )

        if self._groups:
            logger.info(
                "🔀 Loaded %d failover group(s): %s",
                len(self._groups),
                ", ".join(sorted(self._groups.keys())),
            )

    def get_group(self, name: str) -> Optional[FailoverGroup]:
        """Return the :class:`FailoverGroup` named *name*, or ``None``."""
        return self._groups.get(name)

    def get_image_fallback_for_model(self, model_name: str) -> Optional[str]:
        """Return the local image fallback configured for an upstream model."""
        for group in self._groups.values():
            for candidate in group.candidates:
                if candidate.model != model_name:
                    continue
                if "image" in candidate.modalities:
                    return None
                return group.image_fallback_model
        return None
