"""Per-key cloud rate-limit handling for upstream HTTP 429 responses."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import inspect
import json
import logging
import math
import random
import re
import time
from threading import Lock
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Guardian.RateLimit")


@dataclass(frozen=True)
class RateLimitConfig:
    """Bounds and backoff policy for a cloud request retry window."""

    enabled: bool = True
    max_retries: int = 3
    max_hold_seconds: float = 90.0
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    jitter_factor: float = 0.25
    respect_retry_after: bool = True

    @classmethod
    def from_mapping(cls, raw: object) -> "RateLimitConfig":
        """Build a bounded configuration from YAML data."""
        values = raw if isinstance(raw, dict) else {}

        def non_negative_float(name: str, default: float) -> float:
            try:
                return max(float(values.get(name, default)), 0.0)
            except (TypeError, ValueError):
                return default

        try:
            max_retries = max(int(values.get("max_retries", cls.max_retries)), 0)
        except (TypeError, ValueError):
            max_retries = cls.max_retries
        max_hold = non_negative_float("max_hold_seconds", cls.max_hold_seconds)
        max_backoff = non_negative_float("max_backoff_seconds", cls.max_backoff_seconds)
        base_backoff = min(
            non_negative_float("base_backoff_seconds", cls.base_backoff_seconds),
            max_backoff,
        )
        jitter = min(non_negative_float("jitter_factor", cls.jitter_factor), 1.0)
        return cls(
            enabled=bool(values.get("enabled", cls.enabled)),
            max_retries=max_retries,
            max_hold_seconds=max_hold,
            base_backoff_seconds=base_backoff,
            max_backoff_seconds=max_backoff,
            jitter_factor=jitter,
            respect_retry_after=bool(values.get("respect_retry_after", cls.respect_retry_after)),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the effective policy without implementation details."""
        return {
            "enabled": self.enabled,
            "max_retries": self.max_retries,
            "max_hold_seconds": self.max_hold_seconds,
            "base_backoff_seconds": self.base_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
            "jitter_factor": self.jitter_factor,
            "respect_retry_after": self.respect_retry_after,
        }


@dataclass(frozen=True)
class RateLimitDecision:
    """Decision made after observing one upstream 429 response."""

    retry: bool
    wait_seconds: float
    reason: str


@dataclass
class _RateLimitState:
    """Mutable counters for one Guardian-key/provider pair."""

    total_429s: int = 0
    total_retries: int = 0
    retry_successes: int = 0
    retry_exhausted: int = 0
    consecutive_429s: int = 0
    total_wait_seconds: float = 0.0
    last_429_at: Optional[float] = None
    last_retry_after_seconds: Optional[float] = None
    last_wait_seconds: Optional[float] = None
    last_reset_at: Optional[float] = None
    remaining: Optional[int] = None
    limit: Optional[int] = None
    last_error_message: Optional[str] = None
    cooldown_until: float = 0.0
    active_requests: int = 0
    waiting_requests: int = 0


class RateLimitRetryManager:
    """Serialize and retry rate-limited requests independently per API key."""

    def __init__(
        self,
        config: Optional[RateLimitConfig] = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.config = config or RateLimitConfig()
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._random_value = random_value
        self._states: Dict[Tuple[str, str], _RateLimitState] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._state_lock = Lock()

    def _state_for(self, key_fingerprint: str, provider: str) -> _RateLimitState:
        identity = (str(key_fingerprint), str(provider))
        with self._state_lock:
            return self._states.setdefault(identity, _RateLimitState())

    def _request_lock(self, key_fingerprint: str, provider: str) -> asyncio.Lock:
        identity = (str(key_fingerprint), str(provider))
        with self._state_lock:
            return self._locks.setdefault(identity, asyncio.Lock())

    @staticmethod
    def _header(response: Any, *names: str) -> Optional[str]:
        headers = getattr(response, "headers", {})
        for name in names:
            value = headers.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return None

    @staticmethod
    def _parse_numeric(value: object) -> Optional[float]:
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _parse_delay_value(self, value: object, now: Optional[float] = None) -> Optional[float]:
        numeric = self._parse_numeric(value)
        if numeric is not None:
            if numeric > 1_000_000_000:
                numeric -= self._wall_time() if now is None else now
            return max(numeric, 0.0)
        try:
            retry_at = parsedate_to_datetime(str(value)).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
        return max(retry_at - (self._wall_time() if now is None else now), 0.0)

    @staticmethod
    def _body_value(payload: object, wanted: set[str]) -> Optional[object]:
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in wanted:
                    return value
                nested = RateLimitRetryManager._body_value(value, wanted)
                if nested is not None:
                    return nested
        elif isinstance(payload, list):
            for item in payload:
                nested = RateLimitRetryManager._body_value(item, wanted)
                if nested is not None:
                    return nested
        return None

    @classmethod
    def _body_retry_after(cls, body_text: str) -> Optional[float]:
        if not body_text:
            return None
        try:
            payload = json.loads(body_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        value = cls._body_value(payload, {"retry_after", "retryafter", "retry_after_seconds"})
        return cls._parse_numeric(value)

    @classmethod
    def _body_message(cls, body_text: str) -> Optional[str]:
        if not body_text:
            return None
        try:
            payload = json.loads(body_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        value = cls._body_value(payload, {"message", "detail", "error_description"})
        if isinstance(value, str):
            message = re.sub(
                r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+",
                r"\1[redacted]",
                value,
            )
            message = re.sub(r"\b(?:sk|nvapi|nv)-[A-Za-z0-9_-]{12,}\b", "[redacted]", message)
            return message[:240]
        return None

    def parse_retry_after(self, response: Any, body_text: str = "") -> Optional[float]:
        """Read Retry-After or provider reset hints, capped to the hold budget."""
        value = self._header(response, "retry-after")
        if value is not None:
            delay = self._parse_delay_value(value)
        else:
            delay = self._body_retry_after(body_text)
            if delay is None:
                reset = self._header(response, "x-ratelimit-reset", "ratelimit-reset")
                delay = self._parse_delay_value(reset) if reset is not None else None
        if delay is None:
            return None
        return max(min(delay, self.config.max_hold_seconds), 0.0)

    def _backoff_seconds(self, retry_count: int) -> float:
        delay = min(
            self.config.base_backoff_seconds * (2**retry_count),
            self.config.max_backoff_seconds,
        )
        if delay and self.config.jitter_factor:
            spread = (self._random_value() * 2.0 - 1.0) * self.config.jitter_factor
            delay *= 1.0 + spread
        return max(min(delay, self.config.max_backoff_seconds), 0.0)

    def _provider_hint(self, response: Any) -> Tuple[Optional[int], Optional[int], Optional[float]]:
        limit = self._parse_numeric(self._header(response, "x-ratelimit-limit", "ratelimit-limit"))
        remaining = self._parse_numeric(
            self._header(response, "x-ratelimit-remaining", "ratelimit-remaining")
        )
        reset_value = self._header(response, "x-ratelimit-reset", "ratelimit-reset")
        reset_at = None
        if reset_value is not None:
            numeric = self._parse_numeric(reset_value)
            if numeric is not None:
                reset_at = numeric if numeric > 1_000_000_000 else self._wall_time() + max(numeric, 0.0)
        bounded_limit = min(max(int(limit), 0), 1_000_000_000) if limit is not None else None
        bounded_remaining = min(max(int(remaining), 0), 1_000_000_000) if remaining is not None else None
        return (
            bounded_limit,
            bounded_remaining,
            reset_at,
        )

    def record_429(
        self,
        key_fingerprint: str,
        provider: str,
        response: Any,
        *,
        body_text: str = "",
        retry_count: int = 0,
        elapsed_seconds: float = 0.0,
        allow_retry: bool = True,
    ) -> RateLimitDecision:
        """Record a 429 and decide whether the current request may retry."""
        state = self._state_for(key_fingerprint, provider)
        if not body_text:
            body_text = str(getattr(response, "text", "") or "")
        retry_after = self.parse_retry_after(response, body_text)
        limit, remaining, reset_at = self._provider_hint(response)
        delay = retry_after if self.config.respect_retry_after and retry_after is not None else self._backoff_seconds(retry_count)
        if not allow_retry:
            delay = 0.0
        delay = min(max(delay, 0.0), self.config.max_hold_seconds)
        with self._state_lock:
            state.total_429s += 1
            state.consecutive_429s += 1
            state.last_429_at = self._wall_time()
            state.last_retry_after_seconds = retry_after
            state.last_error_message = self._body_message(body_text)
            state.limit = limit if limit is not None else state.limit
            state.remaining = remaining if remaining is not None else state.remaining
            state.last_reset_at = reset_at if reset_at is not None else state.last_reset_at
            state.last_wait_seconds = delay
            state.cooldown_until = max(state.cooldown_until, self._monotonic() + delay)

            if not allow_retry:
                state.retry_exhausted += 1
                return RateLimitDecision(False, 0.0, "failover")
            if retry_count >= self.config.max_retries:
                state.retry_exhausted += 1
                return RateLimitDecision(False, delay, "max_retries")
            if elapsed_seconds + delay > self.config.max_hold_seconds:
                state.retry_exhausted += 1
                return RateLimitDecision(False, delay, "max_hold_seconds")

            state.total_retries += 1
            return RateLimitDecision(True, delay, "retry_after" if retry_after is not None else "exponential_backoff")

    def record_success(self, key_fingerprint: str, provider: str, *, retried: bool = False) -> None:
        """Clear the per-provider cooldown after a successful response."""
        state = self._state_for(key_fingerprint, provider)
        with self._state_lock:
            if retried:
                state.retry_successes += 1
            state.consecutive_429s = 0
            state.cooldown_until = 0.0

    async def _sleep_for(self, delay: float, state: _RateLimitState) -> None:
        if delay <= 0:
            return
        result = self._sleep(delay)
        if inspect.isawaitable(result):
            await result
        with self._state_lock:
            state.total_wait_seconds += delay

    @asynccontextmanager
    async def _request_scope(self, key_fingerprint: str, provider: str) -> AsyncIterator[_RateLimitState]:
        """Serialize one key/provider and honor a prior request's cooldown."""
        state = self._state_for(key_fingerprint, provider)
        with self._state_lock:
            state.waiting_requests += 1
        lock = self._request_lock(key_fingerprint, provider)
        async with lock:
            with self._state_lock:
                state.waiting_requests = max(state.waiting_requests - 1, 0)
                state.active_requests += 1
            try:
                with self._state_lock:
                    remaining = max(state.cooldown_until - self._monotonic(), 0.0)
                if remaining:
                    await self._sleep_for(remaining, state)
                    with self._state_lock:
                        state.cooldown_until = 0.0
                yield state
            finally:
                with self._state_lock:
                    state.active_requests = max(state.active_requests - 1, 0)

    async def execute_with_retry(
        self,
        key_fingerprint: str,
        provider: str,
        attempt: Callable[[], Awaitable[Any]],
        *,
        on_429: Optional[Callable[[Any], Awaitable[str]]] = None,
        retry_429: bool = True,
    ) -> Any:
        """Hold one request open while retrying upstream 429 responses."""
        if not self.config.enabled:
            return await attempt()

        retry_count = 0
        async with self._request_scope(key_fingerprint, provider):
            started = self._monotonic()
            while True:
                response = await attempt()
                if getattr(response, "status_code", 0) != 429:
                    self.record_success(key_fingerprint, provider, retried=retry_count > 0)
                    return response

                body_text = ""
                if on_429 is not None:
                    body_text = await on_429(response)
                else:
                    body_text = str(getattr(response, "text", "") or "")
                decision = self.record_429(
                    key_fingerprint,
                    provider,
                    response,
                    body_text=body_text,
                    retry_count=retry_count,
                    elapsed_seconds=max(self._monotonic() - started, 0.0),
                    allow_retry=retry_429,
                )
                if not decision.retry:
                    return response
                retry_count += 1
                state = self._state_for(key_fingerprint, provider)
                await self._sleep_for(decision.wait_seconds, state)
                with self._state_lock:
                    state.cooldown_until = 0.0

    def get_summary(self) -> Dict[str, Any]:
        """Return aggregate counters without exposing per-key activity."""
        with self._state_lock:
            states = list(self._states.values())
            totals = {
                "total_429s": sum(state.total_429s for state in states),
                "total_retries": sum(state.total_retries for state in states),
                "retry_successes": sum(state.retry_successes for state in states),
                "retry_exhausted": sum(state.retry_exhausted for state in states),
                "active_requests": sum(state.active_requests for state in states),
                "waiting_requests": sum(state.waiting_requests for state in states),
            }
        return {
            "generated_at": self._wall_time(),
            "config": self.config.to_dict(),
            "tracked_key_provider_pairs": len(states),
            **totals,
        }

    def _provider_snapshot(self, state: _RateLimitState) -> Dict[str, Any]:
        cooldown = max(state.cooldown_until - self._monotonic(), 0.0)
        return {
            "total_429s": state.total_429s,
            "total_retries": state.total_retries,
            "retry_successes": state.retry_successes,
            "retry_exhausted": state.retry_exhausted,
            "consecutive_429s": state.consecutive_429s,
            "total_wait_seconds": round(state.total_wait_seconds, 3),
            "last_429_at": state.last_429_at,
            "last_retry_after_seconds": state.last_retry_after_seconds,
            "last_wait_seconds": state.last_wait_seconds,
            "last_reset_at": state.last_reset_at,
            "remaining": state.remaining,
            "limit": state.limit,
            "last_error_message": state.last_error_message,
            "cooldown_remaining_seconds": round(cooldown, 3),
            "active_requests": state.active_requests,
            "waiting_requests": state.waiting_requests,
        }

    def get_stats(self, key_fingerprint: Optional[str] = None) -> Dict[str, Any]:
        """Return current safe counters, provider hints, and effective policy."""
        with self._state_lock:
            selected = {
                (key, provider): self._provider_snapshot(state)
                for (key, provider), state in self._states.items()
                if key_fingerprint is None or key == key_fingerprint
            }
        keys: Dict[str, Dict[str, Any]] = {}
        for (key, provider), provider_stats in selected.items():
            keys.setdefault(key, {"providers": {}})["providers"][provider] = provider_stats
        result: Dict[str, Any] = {
            "generated_at": self._wall_time(),
            "config": self.config.to_dict(),
            "keys": keys,
        }
        if key_fingerprint is not None:
            result["key_fingerprint"] = key_fingerprint
            result["providers"] = keys.get(key_fingerprint, {}).get("providers", {})
        return result