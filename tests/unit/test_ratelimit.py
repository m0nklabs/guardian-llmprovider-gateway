"""Unit tests for intelligent per-key cloud 429 handling."""

import asyncio
from email.utils import formatdate
import time

import httpx
import pytest

from app.proxy.ratelimit import RateLimitConfig, RateLimitRetryManager


def _response(status_code: int, headers: dict[str, str] | None = None, body: str = "") -> httpx.Response:
    return httpx.Response(status_code, headers=headers, text=body)


class TestRetryAfterParsing:
    def test_parses_retry_after_seconds(self):
        manager = RateLimitRetryManager(RateLimitConfig(max_hold_seconds=60))

        delay = manager.parse_retry_after(_response(429, {"Retry-After": "7"}))

        assert delay == 7.0

    def test_parses_retry_after_http_date(self):
        manager = RateLimitRetryManager(RateLimitConfig(max_hold_seconds=60))
        retry_at = time.time() + 5

        delay = manager.parse_retry_after(
            _response(429, {"Retry-After": formatdate(retry_at, usegmt=True)})
        )

        assert 3.0 <= delay <= 6.0

    def test_uses_provider_reset_header(self):
        manager = RateLimitRetryManager(RateLimitConfig(max_hold_seconds=60))

        delay = manager.parse_retry_after(_response(429, {"X-RateLimit-Reset": "9"}))

        assert delay == 9.0

    def test_caps_provider_delay_to_hold_budget(self):
        manager = RateLimitRetryManager(RateLimitConfig(max_hold_seconds=10))

        delay = manager.parse_retry_after(_response(429, {"Retry-After": "90"}))

        assert delay == 10.0


class TestPerKeyRetryState:
    @pytest.mark.asyncio
    async def test_retries_429_then_returns_success(self):
        manager = RateLimitRetryManager(
            RateLimitConfig(
                max_retries=2,
                max_hold_seconds=30,
                base_backoff_seconds=0,
                max_backoff_seconds=0,
                jitter_factor=0,
            )
        )
        responses = iter([_response(429), _response(200, body='{"ok": true}')])

        async def attempt():
            return next(responses)

        result = await manager.execute_with_retry("key-a", "nvidia", attempt)

        assert result.status_code == 200
        stats = manager.get_stats("key-a")
        assert stats["providers"]["nvidia"]["total_429s"] == 1
        assert stats["providers"]["nvidia"]["total_retries"] == 1
        assert stats["providers"]["nvidia"]["retry_successes"] == 1

    @pytest.mark.asyncio
    async def test_returns_final_429_after_retry_budget(self):
        manager = RateLimitRetryManager(
            RateLimitConfig(
                max_retries=1,
                max_hold_seconds=30,
                base_backoff_seconds=0,
                max_backoff_seconds=0,
                jitter_factor=0,
            )
        )
        calls = 0

        async def attempt():
            nonlocal calls
            calls += 1
            return _response(429, {"Retry-After": "1"}, '{"error":{"message":"busy"}}')

        result = await manager.execute_with_retry("key-a", "nvidia", attempt)

        assert result.status_code == 429
        assert calls == 2
        provider_stats = manager.get_stats("key-a")["providers"]["nvidia"]
        assert provider_stats["total_429s"] == 2
        assert provider_stats["retry_exhausted"] == 1
        assert provider_stats["last_error_message"] == "busy"

    @pytest.mark.asyncio
    async def test_can_return_429_immediately_for_failover(self):
        sleeps: list[float] = []
        manager = RateLimitRetryManager(
            RateLimitConfig(
                max_retries=30,
                max_hold_seconds=600,
                base_backoff_seconds=2,
                max_backoff_seconds=60,
                jitter_factor=0,
            ),
            sleep=lambda delay: sleeps.append(delay),
        )
        calls = 0

        async def attempt():
            nonlocal calls
            calls += 1
            return _response(429, {"Retry-After": "60"}, '{"error":{"message":"busy"}}')

        result = await manager.execute_with_retry("key-a", "nvidia", attempt, retry_429=False)

        assert result.status_code == 429
        assert calls == 1
        assert sleeps == []
        provider_stats = manager.get_stats("key-a")["providers"]["nvidia"]
        assert provider_stats["retry_exhausted"] == 1
        assert provider_stats["cooldown_remaining_seconds"] == 0

    @pytest.mark.asyncio
    async def test_retry_state_isolated_per_guardian_key(self):
        manager = RateLimitRetryManager(
            RateLimitConfig(
                max_retries=0,
                max_hold_seconds=30,
                base_backoff_seconds=0,
                max_backoff_seconds=0,
                jitter_factor=0,
            )
        )

        async def attempt():
            return _response(429, {"X-RateLimit-Remaining": "0"})

        await manager.execute_with_retry("key-a", "nvidia", attempt)
        await manager.execute_with_retry("key-b", "nvidia", attempt)

        stats = manager.get_stats()
        assert set(stats["keys"]) == {"key-a", "key-b"}
        assert stats["keys"]["key-a"]["providers"]["nvidia"]["remaining"] == 0
        assert stats["keys"]["key-b"]["providers"]["nvidia"]["total_429s"] == 1

    @pytest.mark.asyncio
    async def test_waits_for_shared_key_cooldown_before_next_request(self):
        sleeps: list[float] = []
        manager = RateLimitRetryManager(
            RateLimitConfig(
                max_retries=1,
                max_hold_seconds=30,
                base_backoff_seconds=2,
                max_backoff_seconds=2,
                jitter_factor=0,
            ),
            sleep=lambda delay: sleeps.append(delay),
        )
        responses = iter([_response(429), _response(200)])

        async def attempt():
            return next(responses)

        await manager.execute_with_retry("key-a", "nvidia", attempt)

        assert sleeps == [2.0]

    def test_stats_include_current_cooldown_and_provider_hints(self):
        manager = RateLimitRetryManager(RateLimitConfig(max_retries=0, max_hold_seconds=30))
        response = _response(
            429,
            {
                "Retry-After": "4",
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "12",
            },
            '{"error":{"message":"rate limited"}}',
        )

        manager.record_429("key-a", "nvidia", response)

        stats = manager.get_stats("key-a")["providers"]["nvidia"]
        assert stats["limit"] == 100
        assert stats["remaining"] == 0
        assert stats["last_retry_after_seconds"] == 4.0
        assert stats["last_error_message"] == "rate limited"
        assert stats["cooldown_remaining_seconds"] > 0

    def test_bounds_provider_hints_and_redacts_secret_like_messages(self):
        manager = RateLimitRetryManager(RateLimitConfig(max_retries=0, max_hold_seconds=30))
        response = _response(
            429,
            {
                "X-RateLimit-Limit": "999999999999999999999",
                "X-RateLimit-Remaining": "-4",
            },
            '{"error":{"message":"token=sk-example-secret-value"}}',
        )

        manager.record_429("key-a", "nvidia", response)

        stats = manager.get_stats("key-a")["providers"]["nvidia"]
        assert stats["limit"] == 1_000_000_000
        assert stats["remaining"] == 0
        assert "sk-example-secret-value" not in stats["last_error_message"]

    @pytest.mark.asyncio
    async def test_serializes_concurrent_requests_for_one_key_provider(self):
        manager = RateLimitRetryManager(
            RateLimitConfig(max_retries=0, base_backoff_seconds=0, max_backoff_seconds=0, jitter_factor=0)
        )
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def attempt():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
            return _response(200)

        first = asyncio.create_task(
            manager.execute_with_retry("key-a", "nvidia", attempt)
        )
        await first_started.wait()
        second = asyncio.create_task(
            manager.execute_with_retry("key-a", "nvidia", attempt)
        )
        await asyncio.sleep(0)

        stats = manager.get_stats("key-a")["providers"]["nvidia"]
        assert stats["active_requests"] == 1
        assert stats["waiting_requests"] == 1

        release_first.set()
        await asyncio.gather(first, second)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_summary_aggregates_without_key_details(self):
        manager = RateLimitRetryManager(
            RateLimitConfig(max_retries=1, base_backoff_seconds=0, max_backoff_seconds=0, jitter_factor=0)
        )
        responses = iter([_response(429), _response(200)])

        async def retrying_attempt():
            return next(responses)

        await manager.execute_with_retry("key-a", "nvidia", retrying_attempt)
        manager.record_429("key-b", "openrouter", _response(429), retry_count=1)

        summary = manager.get_summary()
        assert summary["tracked_key_provider_pairs"] == 2
        assert summary["total_429s"] == 2
        assert summary["total_retries"] == 1
        assert summary["retry_successes"] == 1
        assert summary["retry_exhausted"] == 1
        assert "keys" not in summary
        assert "providers" not in summary