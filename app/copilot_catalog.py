"""Validated Copilot discovery snapshots, independent of inference and the bridge.

Only explicit refreshes and the periodic loop perform discovery. Invalid rows and
ambiguous identities are excluded from each new snapshot; a failed discovery
request retains the previous snapshot without extending its freshness deadline.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

import httpx

logger = logging.getLogger("guardian.copilot_catalog")
BRANDS = {"azure openai": "openai", "xai": "x-ai", "moonshot ai": "moonshotai"}


class CatalogSettings(Protocol):
    """The settings fields discovery consumes; defaults belong to the bridge."""

    upstream_url: str
    upstream_key: str
    refresh_seconds: float
    catalog_timeout_seconds: float
    catalog_max_age_seconds: float


class _InvalidEntry(ValueError):
    """Internal validation failure; never contains upstream values."""


def _identity(entry: Mapping[str, Any]) -> tuple[str, str, str] | None:
    raw_id = entry.get("id")
    vendor = entry.get("vendor")
    if vendor is None or vendor == "":
        vendor = entry.get("owned_by")
    if (not isinstance(raw_id, str) or not raw_id or "/" in raw_id
            or any(character.isspace() or ord(character) < 32 for character in raw_id)):
        return None
    if not isinstance(vendor, str) or not vendor.strip():
        return None  # Never manufacture a maker from the serving provider.
    vendor = vendor.strip().lower()
    brand = BRANDS.get(vendor, re.sub(r"[^a-z0-9]+", "-", vendor).strip("-"))
    if not brand:
        return None
    return brand, raw_id, vendor


def _mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    if key not in parent:
        return {}
    value = parent[key]
    if not isinstance(value, Mapping):
        raise _InvalidEntry("Invalid nested mapping")
    return value


def _copy_metadata(value: Any) -> Any:
    try:
        return deepcopy(value)
    except RecursionError:
        # JSON decoding can accept deeper objects than deepcopy can traverse.
        # This is a bad row, not a failed snapshot that should retain revocations.
        raise _InvalidEntry("Catalog metadata exceeds projection depth") from None


def _project(entry: Mapping[str, Any], identity: tuple[str, str, str]) -> tuple[dict[str, Any] | None, str]:
    policy = _mapping(entry, "policy")
    if policy.get("state") == "disabled":
        # A revocation takes effect even if unrelated metadata is malformed.
        return None, "disabled"
    if "state" in policy and (not isinstance(policy["state"], str) or not policy["state"]):
        raise _InvalidEntry("Invalid policy state")
    capabilities = _mapping(entry, "capabilities")
    limits = _mapping(capabilities, "limits")
    supports = _mapping(capabilities, "supports")
    if "type" in capabilities and (not isinstance(capabilities["type"], str) or not capabilities["type"]):
        raise _InvalidEntry("Invalid capability type")
    endpoints = entry.get("supported_endpoints", [])
    if not isinstance(endpoints, list) or any(not isinstance(value, str) or not value.strip() for value in endpoints):
        raise _InvalidEntry("Invalid supported endpoints")
    if "model_picker_enabled" in entry and not isinstance(entry["model_picker_enabled"], bool):
        raise _InvalidEntry("Invalid picker state")
    if "name" in entry and (not isinstance(entry["name"], str) or not entry["name"].strip()):
        raise _InvalidEntry("Invalid model name")
    for key in ("max_context_window_tokens", "max_prompt_tokens", "max_output_tokens"):
        if key in limits and (not isinstance(limits[key], int) or isinstance(limits[key], bool) or limits[key] <= 0):
            raise _InvalidEntry("Invalid token limit")
    for key in ("vision", "tool_calls", "streaming"):
        if key in supports and not isinstance(supports[key], bool):
            raise _InvalidEntry("Invalid capability support")
    efforts = supports.get("reasoning_effort", [])
    if not isinstance(efforts, list) or any(not isinstance(value, str) or not value.strip() for value in efforts):
        raise _InvalidEntry("Invalid reasoning efforts")

    if capabilities.get("type") == "embeddings":
        protocol = "embeddings"
    elif "/responses" in endpoints:
        protocol = "responses"
    elif "/chat/completions" in endpoints:
        protocol = "chat"
    elif "/v1/messages" in endpoints:
        protocol = "messages"
    else:
        return None, "unsupported"
    if entry.get("model_picker_enabled") is False and protocol != "embeddings":
        return None, "disabled"

    brand, raw_id, vendor = identity
    public = {
        "id": f"{brand}/{raw_id}", "object": "model", "owned_by": brand,
        "name": entry.get("name", raw_id), "vendor": vendor,
        "upstream_id": raw_id, "protocol": protocol,
        "supported_endpoints": _copy_metadata(endpoints),
        # These are the adapter's protocol modalities, not invented token limits.
        "architecture": {"input_modalities": ["text"] + (["image"] if supports.get("vision") is True else []),
                         "output_modalities": ["embedding" if protocol == "embeddings" else "text"]},
    }
    if "capabilities" in entry:
        public["capabilities"] = _copy_metadata(dict(capabilities))
    context = limits.get("max_context_window_tokens", limits.get("max_prompt_tokens"))
    if context is not None:
        public["context_length"] = context
    if "max_output_tokens" in limits:
        public["max_output_tokens"] = limits["max_output_tokens"]
    if efforts:
        public["reasoning"] = {"supported": True, "supported_efforts": _copy_metadata(efforts)}
    return public, "accepted"


def normalize_model(entry: Any) -> dict[str, Any] | None:
    """Project one valid entry, or return None for disabled/unsupported/invalid rows.

    Missing optional metadata remains absent. Provided malformed metadata never
    becomes fabricated capabilities, limits or reasoning support.
    """
    if not isinstance(entry, Mapping):
        return None
    identity = _identity(entry)
    if identity is None:
        return None
    try:
        return _project(entry, identity)[0]
    except _InvalidEntry:
        return None


class Catalog:
    def __init__(self, settings: CatalogSettings, client: httpx.AsyncClient, *,
                 wall_clock: Callable[[], float] = time.time,
                 monotonic_clock: Callable[[], float] = time.monotonic):
        self.settings, self.client = settings, client
        self.models: dict[str, dict[str, Any]] = {}
        self.last_success: float | None = None  # Wall time for diagnostics only.
        self._last_success_monotonic: float | None = None
        self.last_error: str | None = None
        self.counts = self._empty_counts()
        self.lock = asyncio.Lock()
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        if (isinstance(settings.catalog_max_age_seconds, bool)
                or not math.isfinite(settings.catalog_max_age_seconds)
                or settings.catalog_max_age_seconds <= 0):
            raise ValueError("catalog_max_age_seconds must be positive and finite")

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {key: 0 for key in ("total", "accepted", "invalid", "disabled", "unsupported", "duplicate_identities")}

    @property
    def age_seconds(self) -> float | None:
        """Elapsed age since successful discovery, unaffected by wall-clock jumps."""
        if self._last_success_monotonic is None:
            return None
        return max(0.0, self._monotonic_clock() - self._last_success_monotonic)

    @property
    def is_stale(self) -> bool:
        """Discovery is degraded after failure, even during the usable grace period."""
        return self.last_error is not None or not self.usable

    @property
    def usable(self) -> bool:
        """A successful snapshot remains usable until its maximum age expires.

        Refresh failures mark discovery stale but do not cut short this grace
        period. A successful empty snapshot is also usable.
        """
        age = self.age_seconds
        return age is not None and age < self.settings.catalog_max_age_seconds

    @property
    def status(self) -> dict[str, Any]:
        """Safe diagnostics: no upstream rows, model names, credentials or bodies."""
        age = self.age_seconds
        usable = age is not None and age < self.settings.catalog_max_age_seconds
        stale = self.last_error is not None or not usable
        return {"usable": usable, "is_stale": stale, "age_seconds": age,
                "last_success": self.last_success, "last_error": self.last_error,
                "model_count": len(self.models), "counts": dict(self.counts)}

    async def refresh(self) -> None:
        async with self.lock:
            try:
                response = await self.client.get(
                    self.settings.upstream_url + "/models",
                    headers={"Authorization": "Bearer " + self.settings.upstream_key},
                    timeout=self.settings.catalog_timeout_seconds,
                )
                response.raise_for_status()
                root = response.json()
                if not isinstance(root, Mapping) or not isinstance(root.get("data"), list):
                    raise ValueError("Upstream catalog must contain a data list")
                counts = self._empty_counts()
                entries = root["data"]
                counts["total"] = len(entries)
                identities: Counter[str] = Counter()
                models: dict[str, dict[str, Any]] = {}
                for entry in entries:
                    identity = _identity(entry) if isinstance(entry, Mapping) else None
                    if identity is None:
                        counts["invalid"] += 1
                        continue
                    public_id = f"{identity[0]}/{identity[1]}"
                    identities[public_id] += 1
                    try:
                        model, category = _project(entry, identity)
                    except _InvalidEntry:
                        counts["invalid"] += 1
                        continue
                    if model is not None:
                        models[public_id] = model
                    else:
                        counts[category] += 1
                # Reject every ambiguous identity, including enabled/disabled or
                # enabled/malformed pairs. Never roll back and resurrect a model.
                for public_id, occurrences in identities.items():
                    if occurrences > 1:
                        counts["duplicate_identities"] += 1
                        models.pop(public_id, None)
                counts["accepted"] = len(models)
                succeeded_at = self._wall_clock()
                succeeded_monotonic = self._monotonic_clock()
                self.models = models  # Atomic replacement retires removed rows.
                self.counts = counts
                self.last_success, self.last_error = succeeded_at, None
                self._last_success_monotonic = succeeded_monotonic
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("Copilot catalog refresh failed (%s); retaining last successful snapshot", self.last_error)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.refresh_seconds)
            await self.refresh()
