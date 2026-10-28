"""G1-03 part 1 — bounded admission: body size, queue depth, per-key rate.

Decision record: ``docs/decisions/G1-03-admission-and-audit.md`` §2-§6.
Enforcement points: ``app/proxy/admission.py`` (body bound + rate limiter),
``app/proxy/queue.py`` (waiting-depth bound), ``app/proxy/auth.py`` (rate hook),
``app/gateway/queue_helpers.py`` (503 mapping).

Every bound is exercised with a **non-default** configured value: a 1 KiB body
limit, a queue depth of 1, a 6 requests/minute budget. The defaults are asserted
separately (and against the shipped ``config/global.settings.yaml``), because
"the default is safe but not tuned" is a claim the record makes and this file
has to keep honest.

No test here reads the real key store, waits on a real timer longer than a few
hundred milliseconds, or talks to a model: the queue is replaced with a
test-configured instance and model resolution is stubbed.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from app import main as dashboard_module
from app.config_loader import CONFIG
from app.gateway import model_discovery, queue_helpers, routing
from app.proxy import admission, auth, server
from app.proxy.admission import (
    DEFAULT_BURST,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_QUEUE_FULL_RETRY_AFTER_S,
    DEFAULT_QUEUE_MAX_DEPTH_BOUND,
    DEFAULT_REQUESTS_PER_MINUTE,
    LIMIT_BODY,
    LIMIT_QUEUE_DEPTH,
    LIMIT_RATE,
)
from app.proxy.queue import DEFAULT_QUEUE_MAX_DEPTH, InferenceQueue, QueueDepthExceeded

# ── Keys and fixtures ─────────────────────────────────────────────────

CLIENT_TOKEN = "client_00000000000000000000000000d1"
OTHER_TOKEN = "other_000000000000000000000000000d2"
ADMIN_TOKEN = "admin_000000000000000000000000000d3"

KEYS: dict[str, dict] = {
    CLIENT_TOKEN: {"name": "agent-one", "created_at": 1, "metadata": {"scopes": ["client"]}},
    OTHER_TOKEN: {"name": "agent-two", "created_at": 2, "metadata": {"scopes": ["client"]}},
    ADMIN_TOKEN: {"name": "dashboard", "created_at": 3, "metadata": {"scopes": ["admin"]}},
}


def _auth(token: str = CLIENT_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    path = tmp_path / "guardian.keys.yaml"
    path.write_text(yaml.safe_dump(KEYS), encoding="utf-8")
    monkeypatch.setattr(auth, "API_KEYS_FILE", path)
    monkeypatch.setattr(auth, "LEGACY_APIKEYS_FILE", tmp_path / "no-legacy.json")
    auth._API_KEYS_CACHE.clear()
    yield path
    auth._API_KEYS_CACHE.clear()


@pytest.fixture(autouse=True)
def fresh_admission_state():
    """Every test gets its own buckets and its own resolution cache."""
    admission.reset_cache()
    admission.RATE_LIMITER.reset()
    yield
    admission.reset_cache()
    admission.RATE_LIMITER.reset()


@pytest.fixture
def admission_config(monkeypatch):
    """Set the ``admission`` section for one test (never the defaults)."""

    def _apply(section: dict | None = None, **overrides):
        merged = dict(section or {})
        merged.update(overrides)
        monkeypatch.setitem(CONFIG, "admission", merged)
        admission.reset_cache()
        return merged

    yield _apply


@pytest.fixture
def proxy_client(keys_file):
    return TestClient(server.app)


@pytest.fixture
def dashboard_client(keys_file):
    return TestClient(dashboard_module.app)


@pytest.fixture
def test_queue(monkeypatch):
    """Install a test-configured queue where the request path will find it."""

    def _install(**kwargs):
        queue = InferenceQueue(**kwargs)
        monkeypatch.setattr(queue_helpers, "_inference_queue", queue)
        monkeypatch.setattr(server, "inference_queue", queue)
        return queue

    return _install


@pytest.fixture
def resolvable_models(monkeypatch):
    """Let any model name through so a test reaches queue admission."""

    def _resolve(requested_model, _current_model):
        return requested_model

    monkeypatch.setattr(routing, "_resolve_or_reject_inference_model", _resolve)


def _get(client: TestClient, path: str, token: str = CLIENT_TOKEN):
    return client.get(path, headers=_auth(token))


def _chat(client: TestClient, token: str = CLIENT_TOKEN, body: dict | None = None):
    payload = body if body is not None else {"model": "test-model", "messages": []}
    return client.post("/v1/chat/completions", headers=_auth(token), json=payload)


def _asgi_scope(headers=()) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def _drive_middleware(messages: list[dict], headers=()) -> tuple[int, bytes, list[dict]]:
    """Run BodySizeLimitMiddleware over a canned receive channel.

    Returns ``(status, payload, app_calls)`` where each app call records how the
    inner app saw the body (``messages`` = http.request messages it had to read).
    """
    pending = list(messages)
    app_calls: list[dict] = []
    sent: list[dict] = []

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    async def inner_app(scope, receive, send_) -> None:
        from starlette.responses import JSONResponse

        body = b""
        seen_messages = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            seen_messages += 1
            body += message.get("body") or b""
            if not message.get("more_body", False):
                break
        app_calls.append({"messages": seen_messages, "body": body})
        await JSONResponse({"ok": True})(scope, receive, send_)

    middleware = admission.BodySizeLimitMiddleware(inner_app)
    await middleware(_asgi_scope(headers), receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, payload, app_calls


# ── 1. Configuration resolution (fail-closed) ─────────────────────────


class TestLimitResolution:
    def test_shipped_config_matches_the_documented_defaults(self):
        """The defaults live in the config file *and* in the module, in sync."""
        limits = admission.resolve_limits()
        assert limits.invalid == {}
        assert limits.max_body_bytes == DEFAULT_MAX_BODY_BYTES == 32 * 1024 * 1024
        assert limits.queue_max_depth == DEFAULT_QUEUE_MAX_DEPTH_BOUND == 32
        assert limits.queue_full_retry_after_s == DEFAULT_QUEUE_FULL_RETRY_AFTER_S == 60
        assert limits.requests_per_minute == DEFAULT_REQUESTS_PER_MINUTE == 300
        assert limits.burst == DEFAULT_BURST == 300
        assert limits.source == "configured"

    def test_non_default_values_are_honoured(self, admission_config):
        admission_config(
            max_body_bytes=1024,
            queue_max_depth=2,
            queue_full_retry_after_seconds=7,
            rate_limit={"requests_per_minute": 6, "burst": 3},
        )
        limits = admission.resolve_limits()
        assert limits.invalid == {}
        assert (limits.max_body_bytes, limits.queue_max_depth) == (1024, 2)
        assert limits.queue_full_retry_after_s == 7
        assert (limits.requests_per_minute, limits.burst) == (6, 3)
        assert limits.source == "configured"

    def test_absent_section_uses_the_shipped_defaults_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.delitem(CONFIG, "admission", raising=False)
        admission.reset_cache()
        with caplog.at_level("WARNING", logger="Guardian.Admission"):
            limits = admission.resolve_limits()
        assert limits.invalid == {}
        assert limits.max_body_bytes == DEFAULT_MAX_BODY_BYTES
        assert limits.source == "shipped-default"
        assert "using the shipped default" in caplog.text

    @pytest.mark.parametrize(
        "value",
        ["32MB", 0, -1, True, 3.5, None, [], {}],
        ids=["string", "zero", "negative", "bool", "float", "null", "list", "mapping"],
    )
    def test_malformed_body_limit_is_refused_not_ignored(self, admission_config, value):
        admission_config(max_body_bytes=value)
        limits = admission.resolve_limits()
        assert limits.max_body_bytes is None
        assert LIMIT_BODY in limits.invalid
        assert limits.queue_max_depth == DEFAULT_QUEUE_MAX_DEPTH_BOUND  # others survive

    def test_zero_is_never_an_off_switch(self, admission_config):
        admission_config(queue_max_depth=0, rate_limit={"requests_per_minute": 0, "burst": 300})
        limits = admission.resolve_limits()
        assert LIMIT_QUEUE_DEPTH in limits.invalid
        assert LIMIT_RATE in limits.invalid
        assert "minimum" in limits.invalid[LIMIT_RATE]

    def test_non_mapping_section_refuses_every_bound(self, monkeypatch):
        monkeypatch.setitem(CONFIG, "admission", "on")
        admission.reset_cache()
        limits = admission.resolve_limits()
        assert limits.source == "invalid"
        assert set(limits.invalid) >= {LIMIT_BODY, LIMIT_QUEUE_DEPTH, LIMIT_RATE}

    def test_malformed_rate_section_refuses_the_rate_bound_only(self, admission_config):
        admission_config(max_body_bytes=2048, rate_limit="300/min")
        limits = admission.resolve_limits()
        assert limits.max_body_bytes == 2048
        assert LIMIT_RATE in limits.invalid
        assert LIMIT_QUEUE_DEPTH not in limits.invalid

    def test_broken_retry_hint_refuses_the_queue_bound(self, admission_config):
        """A broken Retry-After must not silently become 'no hint'."""
        admission_config(queue_max_depth=4, queue_full_retry_after_seconds="60")
        limits = admission.resolve_limits()
        assert LIMIT_QUEUE_DEPTH in limits.invalid
        assert "queue_full_retry_after_seconds" in limits.invalid[LIMIT_QUEUE_DEPTH]
        assert admission.queue_full_retry_after_s() is None

    def test_reload_changes_the_bounds_without_a_restart(self, admission_config):
        admission_config(max_body_bytes=1024)
        assert admission.resolve_limits().max_body_bytes == 1024
        admission_config(max_body_bytes=4096)
        assert admission.resolve_limits().max_body_bytes == 4096

    def test_resolution_is_cached_per_config_signature(self, admission_config):
        admission_config(max_body_bytes=1024)
        assert admission.resolve_limits() is admission.resolve_limits()


# ── 2. Request/body size -> 413 ───────────────────────────────────────


class TestBodySizeLimit:
    def test_oversized_declared_body_is_refused_with_413(self, proxy_client, admission_config):
        admission_config(max_body_bytes=1024)
        response = proxy_client.post(
            "/api/show",
            headers={**_auth(), "Content-Type": "application/json"},
            content=b'{"model": "' + b"x" * 4096 + b'"}',
        )
        assert response.status_code == 413, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "request_too_large"
        assert detail["limit"] == LIMIT_BODY
        assert detail["limit_bytes"] == 1024
        assert detail["declared_bytes"] > 1024
        assert "admission.max_body_bytes" in detail["message"]

    def test_chunked_body_is_stopped_at_the_bound(self, proxy_client, admission_config):
        """No Content-Length: the counting receive channel enforces the limit."""
        admission_config(max_body_bytes=1024)
        chunks = iter([b"x" * 700, b"x" * 700, b"x" * 700])
        response = proxy_client.post(
            "/api/show",
            headers={**_auth(), "Content-Type": "application/json"},
            content=chunks,
        )
        assert response.status_code == 413, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "request_too_large"
        assert detail["observed_bytes"] > 1024
        assert "declared_bytes" not in detail

    def test_rejection_never_echoes_the_payload(self, proxy_client, admission_config):
        admission_config(max_body_bytes=512)
        marker = "PROMPT-CONTENT-MUST-NOT-BE-ECHOED"
        response = proxy_client.post(
            "/api/show",
            headers={**_auth(), "Content-Type": "application/json"},
            content=json.dumps({"model": marker, "padding": "y" * 4096}).encode(),
        )
        assert response.status_code == 413
        assert marker not in response.text

    def test_body_under_the_limit_reaches_the_handler(self, proxy_client, admission_config, monkeypatch):
        admission_config(max_body_bytes=1024)

        async def _echo(request, client_id):
            body = await request.body()
            return {"received_bytes": len(body), "client": client_id}

        monkeypatch.setattr(model_discovery, "show_model", _echo)
        response = proxy_client.post(
            "/api/show", headers={**_auth(), "Content-Type": "application/json"}, content=b'{"a": 1}'
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"received_bytes": 8, "client": "agent-one"}

    def test_allowed_large_body_is_untouched(self, proxy_client, admission_config, monkeypatch):
        """8 MiB limit, 5 MiB body: admitted whole, not truncated."""
        admission_config(max_body_bytes=8 * 1024 * 1024)
        payload = b'{"pad": "' + b"a" * (5 * 1024 * 1024) + b'"}'

        async def _echo(request, client_id):
            body = await request.body()
            return {"received_bytes": len(body)}

        monkeypatch.setattr(model_discovery, "show_model", _echo)
        response = proxy_client.post(
            "/api/show", headers={**_auth(), "Content-Type": "application/json"}, content=payload
        )
        assert response.status_code == 200, response.text
        assert response.json()["received_bytes"] == len(payload)

    async def test_chunked_framing_wins_over_a_small_content_length(self, admission_config):
        """Review finding 1: both headers present => count the bytes.

        RFC 7230 says a message with ``Transfer-Encoding`` *and* ``Content-Length``
        is malformed and a server frames by the encoding (h11/uvicorn do), so a
        small declared length must not be trusted to skip counting.
        """
        admission_config(max_body_bytes=1024)
        headers = [
            (b"transfer-encoding", b"chunked"),
            (b"content-length", b"10"),
            (b"content-type", b"application/json"),
        ]
        messages = [
            {"type": "http.request", "body": b"x" * 700, "more_body": True},
            {"type": "http.request", "body": b"x" * 700, "more_body": True},
            {"type": "http.request", "body": b"x" * 700, "more_body": False},
        ]
        status, payload, app_calls = await _drive_middleware(messages, headers)
        assert status == 413, payload
        assert b"request_too_large" in payload
        assert b"observed_bytes" in payload
        assert app_calls == []  # the handler never ran

    async def test_a_declared_length_is_still_trusted_without_a_transfer_encoding_marker(
        self, admission_config
    ):
        admission_config(max_body_bytes=1024)
        headers = [(b"content-length", str(4096).encode()), (b"content-type", b"application/json")]
        status, payload, _app_calls = await _drive_middleware([], headers)
        assert status == 413, payload
        assert b"declared_bytes" in payload

    async def test_many_tiny_messages_do_not_escape_the_bound(self, admission_config):
        """Review finding 2: message count must not buy unbounded allocation."""
        admission_config(max_body_bytes=1024)
        messages = [{"type": "http.request", "body": b"x", "more_body": True} for _ in range(2000)]
        messages.append({"type": "http.request", "body": b"", "more_body": False})
        status, payload, app_calls = await _drive_middleware(messages)
        assert status == 413, payload
        assert b"observed_bytes" in payload
        assert app_calls == []

    async def test_an_allowed_chunked_body_is_coalesced_for_the_handler(self, admission_config):
        """Review finding 2: the replay is one message, not one per input message."""
        admission_config(max_body_bytes=1024)
        messages = [{"type": "http.request", "body": b"y", "more_body": True} for _ in range(500)]
        messages.append({"type": "http.request", "body": b"", "more_body": False})
        status, _payload, app_calls = await _drive_middleware(messages)
        assert status == 200
        assert app_calls == [{"messages": 1, "body": b"y" * 500}]

    async def test_a_disconnect_before_the_body_is_still_delivered(self, admission_config):
        admission_config(max_body_bytes=1024)
        status, _payload, app_calls = await _drive_middleware([{"type": "http.disconnect"}])
        assert status == 200
        assert app_calls == [{"messages": 1, "body": b""}]

    def test_dashboard_surface_has_the_same_bound(self, dashboard_client, admission_config):
        admission_config(max_body_bytes=1024)
        response = dashboard_client.post(
            "/api/keys",
            headers={**_auth(ADMIN_TOKEN), "Content-Type": "application/json"},
            content=b'{"name": "' + b"z" * 4096 + b'"}',
        )
        assert response.status_code == 413, response.text
        assert response.json()["detail"]["limit_bytes"] == 1024

    def test_malformed_body_bound_refuses_with_503(self, proxy_client, admission_config):
        admission_config(max_body_bytes="32MB")
        response = proxy_client.post(
            "/api/show",
            headers={**_auth(), "Content-Type": "application/json"},
            content=b'{"model": "x"}',
        )
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "admission_misconfigured"
        assert detail["limit"] == LIMIT_BODY
        assert "not a positive integer" in detail["reason"]

    def test_misconfigured_bound_refuses_even_a_tiny_body(self, proxy_client, admission_config):
        """Fail-closed: no size is admitted while the bound cannot be resolved."""
        admission_config(max_body_bytes=None)
        response = proxy_client.post("/api/show", headers=_auth(), content=b"")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "admission_misconfigured"


# ── 3. Queue depth -> 503 ─────────────────────────────────────────────


class TestQueueDepthBound:
    def test_default_depth_is_the_shipped_bound(self):
        assert InferenceQueue().max_queue_depth == DEFAULT_QUEUE_MAX_DEPTH == 32

    def test_next_submission_beyond_the_bound_is_refused(self):
        queue = InferenceQueue(max_concurrent=1, max_queue_depth=1)
        queue.submit("owner-a", "model", owner_id="owner-a")
        with pytest.raises(QueueDepthExceeded) as excinfo:
            queue.submit("owner-b", "model", owner_id="owner-b")
        assert excinfo.value.reason == "queue_full"
        assert excinfo.value.max_queue_depth == 1
        assert excinfo.value.queue_length == 1
        assert queue.waiting_count == 1  # nothing was registered

    def test_depth_counts_waiting_requests_only(self):
        queue = InferenceQueue(max_concurrent=2, max_queue_depth=2)
        queue.submit("a", "model", owner_id="a")
        queue.submit("b", "model", owner_id="b")
        with pytest.raises(QueueDepthExceeded):
            queue.submit("c", "model", owner_id="c")  # refused: 2 already waiting
        assert queue.waiting_count == 2

    def test_concurrency_bound_is_independent_of_the_depth_bound(self):
        """max_concurrent bounds running work; max_queue_depth bounds waiting work."""
        queue = InferenceQueue(max_concurrent=1, max_queue_depth=8)
        assert queue.get_status()["max_concurrent"] == 1
        assert queue.get_status()["max_queue_depth"] == 8

    def test_unresolvable_bound_refuses_instead_of_waiting_unbounded(self):
        queue = InferenceQueue(max_queue_depth=None)
        with pytest.raises(QueueDepthExceeded) as excinfo:
            queue.submit("a", "model", owner_id="a")
        assert excinfo.value.reason == "admission_misconfigured"
        assert excinfo.value.max_queue_depth is None

    @pytest.mark.parametrize("value", [0, -1, True, "8", 2.5])
    def test_queue_rejects_a_nonsense_depth(self, value):
        with pytest.raises(ValueError):
            InferenceQueue(max_queue_depth=value)

    def test_status_reports_the_bound(self, test_queue):
        queue = test_queue(max_queue_depth=3)
        assert queue.get_status()["max_queue_depth"] == 3

    async def test_full_queue_returns_503_with_retry_after(
        self, proxy_client, admission_config, test_queue, resolvable_models
    ):
        admission_config(queue_full_retry_after_seconds=7)
        queue = test_queue(max_concurrent=1, max_queue_depth=1, queue_timeout=1.0)
        queue.submit("holder", "model", owner_id="holder")  # one waiting => full

        response = _chat(proxy_client)
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "queue_full"
        assert detail["limit"] == LIMIT_QUEUE_DEPTH
        assert detail["max_queue_depth"] == 1
        assert detail["queue_length"] == 1
        assert detail["max_concurrent"] == 1
        assert detail["retry_after_s"] == 7
        assert response.headers["Retry-After"] == "7"
        assert queue.waiting_count == 1  # the refused request never entered

    async def test_request_within_the_bound_is_queued_not_refused(
        self, proxy_client, admission_config, test_queue, resolvable_models
    ):
        """Positive: depth 2 with one holder => the request waits (and expires)."""
        admission_config(queue_max_depth=2)
        queue = test_queue(max_concurrent=1, max_queue_depth=2, queue_timeout=0.2)
        holder_id = queue.submit("holder", "model", owner_id="holder")
        await queue.wait_for_turn(holder_id)  # the single slot is taken

        response = _chat(proxy_client)
        assert response.status_code == 499, response.text
        assert response.json()["detail"]["message"] == "queue_timeout"
        assert queue.get_status()["stats"]["total_timeouts"] >= 1

    async def test_expired_queued_request_keeps_the_shipped_499_behaviour(
        self, proxy_client, test_queue, resolvable_models
    ):
        """Interaction with #45: a queued request that expires is still a 499."""
        queue = test_queue(max_concurrent=1, max_queue_depth=4, queue_timeout=0.2)
        holder_id = queue.submit("holder", "model", owner_id="holder")
        await queue.wait_for_turn(holder_id)

        response = _chat(proxy_client)
        detail = response.json()["detail"]
        assert response.status_code == 499
        assert detail["error"] == "request_cancelled"
        assert detail["message"] == "queue_timeout"
        # The wait budget, not the depth bound, ended it: nothing was 503.
        assert queue.get_status()["queue_length"] == 0

    async def test_malformed_retry_hint_refuses_the_submission(
        self, proxy_client, admission_config, test_queue, resolvable_models
    ):
        """Review finding 3: a broken hint refuses, it does not just vanish."""
        admission_config(queue_max_depth=8, queue_full_retry_after_seconds="60")
        queue = test_queue(max_concurrent=1, max_queue_depth=8, queue_timeout=30.0)
        response = _chat(proxy_client)
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "admission_misconfigured"
        assert detail["limit"] == LIMIT_QUEUE_DEPTH
        assert "queue_full_retry_after_seconds" in detail["reason"]
        assert "Retry-After" not in response.headers
        assert queue.waiting_count == 0

    async def test_unresolvable_depth_refuses_over_http(self, proxy_client, test_queue, resolvable_models):
        test_queue(max_concurrent=1, max_queue_depth=None)
        response = _chat(proxy_client)
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "admission_misconfigured"
        assert detail["limit"] == LIMIT_QUEUE_DEPTH
        assert "Retry-After" not in response.headers


# ── 4. Per-client rate -> 429 ─────────────────────────────────────────


class TestRateLimit:
    def test_trip_returns_429_with_retry_after(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200

        response = _get(proxy_client, "/v1/queue/status")
        assert response.status_code == 429, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "rate_limited"
        assert detail["limit"] == LIMIT_RATE
        assert detail["requests_per_minute"] == 6
        assert detail["burst"] == 1
        retry_after = int(response.headers["Retry-After"])
        assert retry_after == detail["retry_after_s"]
        assert 1 <= retry_after <= 10  # 6/min => ~10 s per token

    def test_traffic_under_the_limit_is_untouched(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 3})
        for _ in range(3):
            assert _get(proxy_client, "/v1/queue/status").status_code == 200

    def test_two_keys_do_not_share_a_bucket(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status", CLIENT_TOKEN).status_code == 200
        assert _get(proxy_client, "/v1/queue/status", CLIENT_TOKEN).status_code == 429
        # A different identity buys its own token.
        assert _get(proxy_client, "/v1/queue/status", OTHER_TOKEN).status_code == 200

    def test_the_same_key_shares_one_bucket_across_routes(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200
        assert _get(proxy_client, "/api/tags").status_code == 429

    def test_rate_limit_is_not_keyed_on_ip(self, proxy_client, admission_config):
        """Both test clients come from the same 'testclient' IP."""
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status", CLIENT_TOKEN).status_code == 200
        assert _get(proxy_client, "/v1/queue/status", OTHER_TOKEN).status_code == 200

    def test_public_routes_are_not_rate_limited(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200
        assert _get(proxy_client, "/v1/queue/status").status_code == 429
        for _ in range(3):
            assert proxy_client.get("/healthz").status_code == 200

    def test_malformed_rate_bound_refuses_with_503(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": "fast", "burst": 300})
        response = _get(proxy_client, "/v1/queue/status")
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "admission_misconfigured"
        assert detail["limit"] == LIMIT_RATE

    def test_raising_the_limit_takes_effect_on_reload(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200
        assert _get(proxy_client, "/v1/queue/status").status_code == 429
        admission_config(rate_limit={"requests_per_minute": 600, "burst": 5})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200

    async def test_rate_limited_request_never_enters_the_queue(
        self, proxy_client, admission_config, monkeypatch, resolvable_models
    ):
        submitted: list[tuple] = []

        class _SpyQueue:
            def submit(self, *args, **kwargs):
                submitted.append((args, kwargs))
                raise AssertionError("a rate-limited request must not reach the queue")

        monkeypatch.setattr(queue_helpers, "_inference_queue", _SpyQueue())
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200

        response = _chat(proxy_client)
        assert response.status_code == 429, response.text
        assert submitted == []

    async def test_queue_full_does_not_consume_the_wait_budget(
        self, proxy_client, admission_config, test_queue, resolvable_models
    ):
        """Two different refusals: rate is checked before queue admission."""
        admission_config(rate_limit={"requests_per_minute": 600, "burst": 50})
        queue = test_queue(max_concurrent=1, max_queue_depth=1, queue_timeout=30.0)
        queue.submit("holder", "model", owner_id="holder")

        refused = _chat(proxy_client)
        assert refused.status_code == 503
        assert refused.json()["detail"]["error"] == "queue_full"

        # The same key is not throttled by a queue refusal, and the queue still
        # holds exactly the one pre-existing waiter (no entry leaked).
        assert _get(proxy_client, "/v1/queue/status").status_code == 200
        assert queue.waiting_count == 1


# ── 5. Rate limiter internals ─────────────────────────────────────────


class TestTokenBucket:
    def test_burst_is_spendable_then_refills_over_time(self):
        limiter = admission.TokenBucketRateLimiter()
        limiter.configure(60, 2)  # 1 token/second, burst 2
        assert limiter.check("key", now=0.0)[0] is True
        assert limiter.check("key", now=0.0)[0] is True
        allowed, retry_after = limiter.check("key", now=0.0)
        assert allowed is False
        assert retry_after == 1
        assert limiter.check("key", now=1.01)[0] is True

    def test_buckets_are_per_identity(self):
        limiter = admission.TokenBucketRateLimiter()
        limiter.configure(60, 1)
        assert limiter.check("a", now=0.0)[0] is True
        assert limiter.check("a", now=0.0)[0] is False
        assert limiter.check("b", now=0.0)[0] is True

    def test_reconfigure_resets_the_buckets(self):
        limiter = admission.TokenBucketRateLimiter()
        limiter.configure(60, 1)
        assert limiter.check("a", now=0.0)[0] is True
        assert limiter.check("a", now=0.0)[0] is False
        limiter.configure(60, 3)
        assert limiter.check("a", now=0.0)[0] is True

    def test_snapshot_names_no_identity(self):
        limiter = admission.TokenBucketRateLimiter()
        limiter.configure(60, 1)
        limiter.check("secret-looking-fingerprint", now=0.0)
        snapshot = limiter.snapshot()
        assert "secret-looking-fingerprint" not in json.dumps(snapshot)
        assert snapshot["identities"] == 1

    def test_identity_is_the_key_fingerprint(self, proxy_client, admission_config, monkeypatch):
        """The hook receives the G1-01 fingerprint, not an IP or a key name."""
        seen: list[str] = []
        real = admission.enforce_rate_limit

        def _spy(identity):
            seen.append(identity)
            return real(identity)

        monkeypatch.setattr(admission, "enforce_rate_limit", _spy)
        admission_config(rate_limit={"requests_per_minute": 60, "burst": 5})
        assert _get(proxy_client, "/v1/queue/status").status_code == 200
        fingerprint = auth._token_fingerprint(CLIENT_TOKEN)
        assert seen == [fingerprint]
        assert seen[0] != "testclient"  # never the source IP
        assert CLIENT_TOKEN not in seen[0]  # a fingerprint, not the secret
