"""Cross-provider failover for Guardian's cloud LLM router.

Some logical models are available through more than one upstream cloud
provider (e.g. ``minimax/minimax-m3`` on both NVIDIA NIM and OpenRouter).
This module lets Guardian route requests through a *failover group* — an
ordered list of ``(provider, model)`` candidates for the same logical model —
so a degraded or erroring provider is skipped in favour of the next healthy
candidate, without the caller (Claude Code, etc.) needing to know or care
which upstream is currently serving the request.

Failover groups are configured in ``config/cloud_keys.json`` under a
top-level ``failover_groups`` key::

    {
      "failover_groups": {
        "minimax-m3": {
          "candidates": [
            {"provider": "nvidia", "model": "minimaxai/minimax-m3"},
            {"provider": "openrouter", "model": "minimax/minimax-m3"}
          ]
        }
      }
    }

A client addresses the group with the ``guardian/failover/{group}`` route,
e.g. ``guardian/failover/minimax-m3``. Guardian tries each candidate in
priority order — skipping any that are currently tripped by the in-memory
circuit breaker (:class:`ProviderHealthTracker`) — and automatically retries
a tripped candidate once its cooldown expires, so it recovers back to the
preferred provider without manual intervention.

Only providers with a credential linked to the caller's Guardian API key
(see :mod:`app.proxy.cloud_keys`) are attempted; candidates without a linked
credential are skipped silently.

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

logger = logging.getLogger("Guardian.Failover")

#: Path to the on-disk store that holds failover group definitions (shared
#: with the per-key cloud credential store).
FAILOVER_CONFIG_FILE: Path = Path(__file__).parent.parent.parent / "config" / "cloud_keys.json"

#: Consecutive failures before a (provider, model) candidate is tripped.
FAILURE_THRESHOLD = 3

#: How long a tripped candidate is skipped before being retried (half-open).
COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True)
class FailoverCandidate:
    """A single ``(provider, model)`` candidate within a failover group."""

    provider: str
    model: str


@dataclass(frozen=True)
class FailoverGroup:
    """An ordered list of interchangeable provider candidates for one logical model."""

    name: str
    candidates: List[FailoverCandidate] = field(default_factory=list)


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
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._lock = Lock()
        self._consecutive_failures: Dict[Tuple[str, str], int] = {}
        self._tripped_until: Dict[Tuple[str, str], float] = {}

    def record_success(self, provider: str, model: str) -> None:
        """Reset failure state for *provider*/*model* after a successful request."""
        key = (provider, model)
        with self._lock:
            self._consecutive_failures.pop(key, None)
            self._tripped_until.pop(key, None)

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

    def order_candidates(self, candidates: List[FailoverCandidate]) -> List[FailoverCandidate]:
        """Return *candidates* healthy-first, preserving configured priority within each bucket."""
        healthy = [c for c in candidates if not self.is_tripped(c.provider, c.model)]
        tripped = [c for c in candidates if self.is_tripped(c.provider, c.model)]
        return healthy + tripped


class FailoverRegistry:
    """Loads ``failover_groups`` definitions from ``cloud_keys.json``.

    Cheap to reconstruct; call :meth:`reload` after editing the config file
    to pick up new/changed groups without restarting Guardian.
    """

    def __init__(self, path: Path = FAILOVER_CONFIG_FILE) -> None:
        self._path = path
        self._groups: Dict[str, FailoverGroup] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read failover group definitions from disk."""
        self._groups.clear()
        try:
            if not self._path.exists():
                return
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("⚠️  Failed to load failover groups from %s: %s", self._path, e)
            return

        if not isinstance(data, dict):
            return

        raw_groups = data.get("failover_groups", {})
        if not isinstance(raw_groups, dict):
            logger.warning("⚠️  'failover_groups' in %s is not a dict; ignoring", self._path)
            return

        for group_name, raw_group in raw_groups.items():
            if not isinstance(raw_group, dict):
                continue
            raw_candidates = raw_group.get("candidates")
            if not isinstance(raw_candidates, list):
                continue
            candidates = [
                FailoverCandidate(provider=str(c["provider"]), model=str(c["model"]))
                for c in raw_candidates
                if isinstance(c, dict) and c.get("provider") and c.get("model")
            ]
            if candidates:
                self._groups[str(group_name)] = FailoverGroup(name=str(group_name), candidates=candidates)

        if self._groups:
            logger.info(
                "🔀 Loaded %d failover group(s): %s",
                len(self._groups),
                ", ".join(sorted(self._groups.keys())),
            )

    def get_group(self, name: str) -> Optional[FailoverGroup]:
        """Return the :class:`FailoverGroup` named *name*, or ``None``."""
        return self._groups.get(name)
