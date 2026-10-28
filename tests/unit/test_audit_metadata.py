"""G1-03 part 2 — metadata-only audit records and the auth-header invariant.

Decision record: ``docs/decisions/G1-03-admission-and-audit.md`` §11.
Implementation: ``app/proxy/audit.py`` (records, sinks, bounds),
``app/proxy/auth.py`` / ``scopes.py`` / ``admission.py`` / ``queue.py`` /
``destination_policy.py`` (the decision points).

The exit gate this file carries:

* **one record per decision, allow and deny**, with the documented field set and
  the documented reason codes (``TestAllowRecords``, ``TestDenyRecords``);
* the **audit-without-payload demonstration** — a request that carries a
  recognisable payload and a recognisable ``Authorization`` header is driven
  through the audited path in *both* directions, and neither the record nor any
  captured log line may contain the payload, the header value, the bearer token
  or the query string (``TestAuditWithoutPayload``);
* the **auth-header log invariant**, automated and reused from the G1-02
  redaction filter (``TestAuthHeaderInvariant``) — including a deliberately
  leaky line, so the guard cannot be vacuous;
* the **storage bound**, filled and asserted (``TestBoundedStorage``).

Nothing here reads the real key store, waits on a real timer, or talks to a
model: the key file is a tmp fixture, the queue is a test instance and model
resolution is stubbed.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
import yaml
from fastapi.testclient import TestClient

from app import main as dashboard_module
from app.config_loader import CONFIG
from app.gateway import queue_helpers, routing
from app.log_safety import contains_credentials, install_secret_redaction
from app.proxy import admission, audit, auth, metrics, scopes, server
from app.proxy.admission import LIMIT_BODY, LIMIT_QUEUE_DEPTH, LIMIT_RATE
from app.proxy.queue import InferenceQueue

# ── Fixtures: keys, tokens, markers ───────────────────────────────────

CLIENT_TOKEN = "flip_9a71c4d2e8b6f0a3c5d7e9b1f3a5c7d9"
OTHER_TOKEN = "flip_0f1e2d3c4b5a69788796a5b4c3d2e1f0"
ADMIN_TOKEN = "flip_1234567890abcdef1234567890abcd13"
LEGACY_TOKEN = "flip_abcdefabcdefabcdefabcdefabcdef17"
UNKNOWN_TOKEN = "flip_deadbeefdeadbeefdeadbeefdeadbeef"

KEYS: dict[str, dict] = {
    CLIENT_TOKEN: {"name": "agent-one", "created_at": 1, "metadata": {"scopes": ["client"]}},
    OTHER_TOKEN: {"name": "agent-two", "created_at": 2, "metadata": {"scopes": ["client"]}},
    ADMIN_TOKEN: {"name": "dashboard", "created_at": 3, "metadata": {"scopes": ["admin"]}},
    LEGACY_TOKEN: {"name": "legacy-agent", "created_at": 4, "metadata": {}},
}

BROKEN_SCOPE_TOKEN = "flip_11112222333344445555666677778888"
BROKEN_KEYS = dict(KEYS)
BROKEN_KEYS[BROKEN_SCOPE_TOKEN] = {
    "name": "broken-scope",
    "created_at": 5,
    "metadata": {"scopes": ["not-a-scope"]},
}

#: Recognisable content that must never reach the audit sink or a log line.
PAYLOAD_MARKER = "PAYLOAD-MARKER-7c1e9f"
HEADER_MARKER = "HEADER-MARKER-4b8d2a"
QUERY_MARKER = "QUERY-MARKER-5e0c83"

#: Loggers owned by the test harness / ASGI plumbing, not by the gateway. The
#: invariant is about what the *gateway* emits (that is what reaches the log
#: aggregator); an HTTP client library's own debug line is not a Guardian log
#: line, and in production uvicorn's access log carries path+query only — never
#: a header.
_HARNESS_LOGGERS = ("httpx", "httpcore", "asyncio", "uvicorn.access", "urllib3")


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _auth(token: str = CLIENT_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    path = tmp_path / "guardian.keys.yaml"
    path.write_text(yaml.safe_dump(KEYS), encoding="utf-8")
    monkeypatch.setattr(auth, "API_KEYS_FILE", path)
    monkeypatch.setattr(auth, "LEGACY_API_KEYS_FILE", tmp_path / "no-legacy.json", raising=False)
    monkeypatch.setattr(auth, "LEGACY_APIKEYS_FILE", tmp_path / "no-legacy.json")
    auth._API_KEYS_CACHE.clear()
    yield path
    auth._API_KEYS_CACHE.clear()


@pytest.fixture
def broken_scope_keys(tmp_path, monkeypatch):
    path = tmp_path / "guardian.keys.yaml"
    path.write_text(yaml.safe_dump(BROKEN_KEYS), encoding="utf-8")
    monkeypatch.setattr(auth, "API_KEYS_FILE", path)
    monkeypatch.setattr(auth, "LEGACY_APIKEYS_FILE", tmp_path / "no-legacy.json")
    auth._API_KEYS_CACHE.clear()
    yield path
    auth._API_KEYS_CACHE.clear()


@pytest.fixture(autouse=True)
def fresh_audit_state():
    """Every test starts with an empty ring buffer and re-resolved settings."""
    audit.reset_cache()
    audit.clear()
    admission.reset_cache()
    admission.RATE_LIMITER.reset()
    install_secret_redaction()
    yield
    audit.reset_cache()
    audit.clear()
    admission.reset_cache()
    admission.RATE_LIMITER.reset()
    auth._API_KEYS_CACHE.clear()


@pytest.fixture
def audit_config(monkeypatch):
    """Set the ``audit`` section for one test."""

    def _apply(**overrides):
        section = dict(CONFIG.get("audit") or {})
        section.update(overrides)
        monkeypatch.setitem(CONFIG, "audit", section)
        audit.reset_cache()
        return section

    yield _apply


@pytest.fixture
def admission_config(monkeypatch):
    """Set the ``admission`` section for one test (never the defaults)."""

    def _apply(**overrides):
        section = dict(CONFIG.get("admission") or {})
        section.update(overrides)
        monkeypatch.setitem(CONFIG, "admission", section)
        admission.reset_cache()
        return section

    yield _apply


@pytest.fixture
def proxy_client(keys_file):
    return TestClient(server.app)


@pytest.fixture
def dashboard_client(keys_file):
    return TestClient(dashboard_module.app)


@pytest.fixture
def test_queue(monkeypatch):
    def _install(**kwargs):
        queue = InferenceQueue(**kwargs)
        monkeypatch.setattr(queue_helpers, "_inference_queue", queue)
        monkeypatch.setattr(server, "inference_queue", queue)
        return queue

    return _install


@pytest.fixture
def resolvable_models(monkeypatch):
    def _resolve(requested_model, _current_model):
        return requested_model

    monkeypatch.setattr(routing, "_resolve_or_reject_inference_model", _resolve)


def _records() -> list[dict]:
    return audit.recent()


def _only_record() -> dict:
    records = _records()
    assert len(records) == 1, f"expected exactly one audit record, got {records}"
    return records[0]


def _gateway_lines(caplog) -> list[str]:
    """Every message the gateway itself logged, at any level, control-free."""
    return [
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith(_HARNESS_LOGGERS)
    ]


def _assert_no_credentials(lines: list[str]) -> None:
    """The invariant helper: raise when any line still carries a credential.

    Shared by the exercised-path test *and* the non-vacuity test — the latter
    feeds it a deliberately leaky line and expects the assertion to fire, which
    is what stops this guard from being decorative.
    """
    offending = [line for line in lines if contains_credentials(line)]
    assert not offending, "credential material reached a log line: " + repr(offending[:3])


def _payload() -> dict:
    return {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": f"{PAYLOAD_MARKER} please read this whole prompt"}
        ],
        "metadata": {"trace": PAYLOAD_MARKER, "note": "the payload must stay in the body"},
    }


# ── 1. Records on allow ───────────────────────────────────────────────


class TestAllowRecords:
    def test_allow_record_carries_exactly_the_documented_fields(self, proxy_client):
        response = proxy_client.get("/v1/queue/status", headers=_auth())
        assert response.status_code == 200, response.text

        record = _only_record()
        assert tuple(record) == audit.RECORD_FIELDS
        assert record["event"] == "audit"
        assert record["surface"] == "proxy"
        assert record["decision"] == audit.ALLOW
        assert record["reason"] == audit.REASON_SCOPE_GRANTED
        assert record["detail"] is None
        assert record["method"] == "GET"
        assert record["route"] == "/v1/queue/status"
        assert record["required_scope"] == "client"
        assert record["scopes"] == ["client"]
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)
        assert record["key_name"] == "agent-one"
        assert record["status"] == 200
        assert record["limit"] is None
        assert record["limit_value"] is None
        assert record["ts"].endswith("Z")

    async def test_allow_record_on_the_inference_catch_all_route(
        self, proxy_client, test_queue, resolvable_models
    ):
        """The catch-all inference route is audited by its *template*."""
        queue = test_queue(max_concurrent=1, max_queue_depth=4, queue_timeout=0.2)
        holder = queue.submit("holder", "model", owner_id="holder")
        await queue.wait_for_turn(holder)  # the single slot is taken
        response = proxy_client.post(
            "/v1/chat/completions", headers=_auth(), json=_payload()
        )
        assert response.status_code == 499, response.text

        record = _only_record()
        assert record["decision"] == audit.ALLOW
        assert record["method"] == "POST"
        assert record["route"] == "/v1/{path:path}"
        assert record["required_scope"] == "client"
        # The authorization was granted; the wait budget ended the request, and
        # that is visible in the status rather than invented as a reason code.
        assert record["status"] == 499

    def test_grace_allow_is_marked_as_such(self, proxy_client, monkeypatch):
        monkeypatch.setitem(CONFIG, "auth", {"scopes_enforced_after": "2999-01-01T00:00:00Z"})
        scopes._TIMESTAMP_CACHE.clear()
        scopes._LOGGED_BAD_TIMESTAMPS.clear()
        try:
            response = proxy_client.get("/api/keys", headers=_auth(LEGACY_TOKEN))
            assert response.status_code == 200, response.text
            record = _only_record()
            assert record["decision"] == audit.ALLOW
            assert record["reason"] == audit.REASON_SCOPE_GRACE
            assert record["scopes"] == ["admin", "capture-export", "client", "operator"]
            assert record["key_name"] == "legacy-agent"
        finally:
            scopes._TIMESTAMP_CACHE.clear()
            scopes._LOGGED_BAD_TIMESTAMPS.clear()

    def test_dashboard_surface_records_its_own_surface(self, dashboard_client):
        response = dashboard_client.get("/api/keys", headers=_auth(ADMIN_TOKEN))
        assert response.status_code == 200, response.text
        record = _only_record()
        assert record["surface"] == "dashboard"
        assert record["route"] == "/api/keys"
        assert record["required_scope"] == "admin"

    def test_public_routes_make_no_authorization_decision(self, proxy_client):
        """`/healthz` decides nothing, so there is nothing to audit.

        Even with an ``Authorization`` header: presenting a key to a route that
        never checks one is not an authorization decision, and the record is
        driven by the decision point, not by the presence of a header.
        """
        assert proxy_client.get("/healthz").status_code == 200
        assert proxy_client.get("/healthz", headers=_auth()).status_code == 200
        assert _records() == []

    def test_mutating_request_method_is_recorded(self, proxy_client):
        response = proxy_client.post("/api/keys", headers=_auth(ADMIN_TOKEN), json={})
        assert response.status_code in (200, 400, 422), response.text
        record = _only_record()
        assert record["method"] == "POST"
        assert record["route"] == "/api/keys"
        assert record["required_scope"] == "admin"


# ── 2. Records on deny (every denial path) ────────────────────────────


class TestDenyRecords:
    def test_scope_denial_is_audited(self, proxy_client):
        response = proxy_client.get("/api/keys", headers=_auth())
        assert response.status_code == 403, response.text

        detail = response.json()["detail"]
        record = _only_record()
        assert record["decision"] == audit.DENY
        assert record["reason"] == audit.REASON_INSUFFICIENT_SCOPE
        assert record["detail"] == "insufficient_scope"
        assert record["status"] == 403
        assert record["required_scope"] == "admin"
        assert record["route"] == "/api/keys"
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)
        # The typed 403 body names the scope too — and never the token.
        assert detail["required_scope"] == "admin"
        assert CLIENT_TOKEN not in json.dumps(record)

    def test_invalid_scope_declaration_is_audited(self, broken_scope_keys):
        client = TestClient(server.app)
        response = client.get("/api/keys", headers=_auth(BROKEN_SCOPE_TOKEN))
        assert response.status_code == 403, response.text
        record = _only_record()
        assert record["reason"] == audit.REASON_INVALID_SCOPE_DECLARATION
        assert record["detail"] == "invalid_scope_declaration"
        assert record["scopes"] == ["client"]  # fail-closed to ordinary inference

    def test_undeclared_route_denial_is_audited(self, proxy_client, monkeypatch):
        monkeypatch.delitem(scopes.ROUTE_SCOPES, ("GET", "/v1/queue/status"))
        response = proxy_client.get("/v1/queue/status", headers=_auth())
        assert response.status_code == 403, response.text
        record = _only_record()
        assert record["reason"] == audit.REASON_UNDECLARED_ROUTE
        assert record["required_scope"] is None
        assert record["status"] == 403

    def test_invalid_key_is_audited_with_its_own_fingerprint(self, proxy_client):
        response = proxy_client.get("/v1/queue/status", headers=_auth(UNKNOWN_TOKEN))
        assert response.status_code == 401, response.text
        record = _only_record()
        assert record["decision"] == audit.DENY
        assert record["reason"] == audit.REASON_INVALID_API_KEY
        assert record["status"] == 401
        assert record["key_fingerprint"] == _fingerprint(UNKNOWN_TOKEN)
        assert record["key_name"] is None
        assert record["required_scope"] == "client"

    def test_missing_key_is_audited_without_an_identity(self, proxy_client):
        response = proxy_client.get("/v1/queue/status")
        assert response.status_code == 401, response.text
        record = _only_record()
        assert record["reason"] == audit.REASON_MISSING_API_KEY
        assert record["key_fingerprint"] is None
        assert record["status"] == 401

    def test_rate_limit_denial_is_audited(self, proxy_client, admission_config):
        admission_config(rate_limit={"requests_per_minute": 6, "burst": 1})
        assert proxy_client.get("/v1/queue/status", headers=_auth()).status_code == 200
        response = proxy_client.get("/v1/queue/status", headers=_auth())
        assert response.status_code == 429, response.text

        deny = [r for r in _records() if r["decision"] == audit.DENY]
        assert len(deny) == 1
        record = deny[0]
        assert record["reason"] == audit.REASON_RATE_LIMITED
        assert record["status"] == 429
        assert record["limit"] == LIMIT_RATE
        assert record["limit_value"] == 6
        assert record["route"] == "/v1/queue/status"
        assert "Retry-After" in response.headers

    async def test_queue_full_denial_is_audited(
        self, proxy_client, admission_config, test_queue, resolvable_models
    ):
        admission_config(queue_max_depth=1, queue_full_retry_after_seconds=7)
        queue = test_queue(max_concurrent=1, max_queue_depth=1, queue_timeout=5.0)
        queue.submit("holder", "model", owner_id="holder")

        response = proxy_client.post("/v1/chat/completions", headers=_auth(), json=_payload())
        assert response.status_code == 503, response.text

        deny = [r for r in _records() if r["decision"] == audit.DENY]
        assert len(deny) == 1
        record = deny[0]
        assert record["reason"] == audit.REASON_QUEUE_FULL
        assert record["limit"] == LIMIT_QUEUE_DEPTH
        assert record["limit_value"] == 1
        assert record["status"] == 503
        assert record["route"] == "/v1/{path:path}"
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)

    async def test_admission_misconfigured_is_audited(
        self, proxy_client, admission_config, test_queue, resolvable_models
    ):
        admission_config(queue_max_depth=8, queue_full_retry_after_seconds="60")
        test_queue(max_concurrent=1, max_queue_depth=8, queue_timeout=5.0)

        response = proxy_client.post("/v1/chat/completions", headers=_auth(), json=_payload())
        assert response.status_code == 503, response.text

        deny = [r for r in _records() if r["decision"] == audit.DENY]
        assert len(deny) == 1
        assert deny[0]["reason"] == audit.REASON_ADMISSION_MISCONFIGURED
        assert deny[0]["limit"] == LIMIT_QUEUE_DEPTH
        assert "queue_full_retry_after_seconds" in deny[0]["detail"]

    def test_body_too_large_is_audited_before_routing(self, proxy_client, admission_config):
        admission_config(max_body_bytes=1024)
        response = proxy_client.post(
            "/v1/chat/completions",
            headers=_auth(),
            content=json.dumps(_payload()).encode() + b"x" * 4096,
        )
        assert response.status_code == 413, response.text

        record = _only_record()
        assert record["decision"] == audit.DENY
        assert record["reason"] == audit.REASON_REQUEST_TOO_LARGE
        assert record["limit"] == LIMIT_BODY
        assert record["limit_value"] == 1024
        assert record["status"] == 413
        # The body bound is a middleware: routing never happened, so there is no
        # route template and no resolved scope. The fingerprint is the one the
        # usage middleware derived from the *presented* token (auditing a
        # refused request needs to correlate attempts); the name is absent
        # because the key was never resolved.
        assert record["route"] is None
        assert record["required_scope"] is None
        assert record["scopes"] is None
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)
        assert record["key_name"] is None

    def test_destination_denial_is_audited_on_the_probe_route(self):
        """A real guarded client, a real denied destination, a real record."""
        client = TestClient(_outbound_probe_app())
        response = client.get("/outbound-probe")
        assert response.status_code == 502, response.text

        record = _only_record()
        assert record["decision"] == audit.DENY
        assert record["reason"] == audit.REASON_DESTINATION_DENIED
        assert record["detail"] == "metadata_host_blocked"
        assert record["status"] == 502
        assert record["route"] == "/outbound-probe"
        assert "169.254.169.254" not in json.dumps(record)  # no destination echo

    def test_destination_denial_lands_on_a_real_guardian_request(
        self, keys_file, monkeypatch
    ):
        """The same denial, raised inside a real Guardian request, joins it.

        ``POST /api/show`` reads the backend's context window through the
        guarded client; pointing that hop at a cloud-metadata address (never
        approvable, on any port) makes the guard refuse it. The request then
        fails — ``show_model`` does not map ``DestinationDenied`` — and the
        audit record still carries the *decision* (deny, destination_denied)
        next to the status the response ended with.
        """
        from app.gateway import context_metadata, model_discovery

        monkeypatch.setattr(
            context_metadata, "LLAMA_SERVER_URL", "http://169.254.169.254:11440"
        )
        monkeypatch.setattr(
            model_discovery._model_manager, "resolve_model", lambda name: "test-model"
        )

        async def _current_model(*_args, **_kwargs):
            return "test-model"

        monkeypatch.setattr(
            context_metadata._model_manager, "get_current_model", _current_model
        )
        context_metadata._backend_context_cache.clear()

        client = TestClient(server.app, raise_server_exceptions=False)
        response = client.post(
            "/api/show", headers=_auth(), json={"model": "test-model"}
        )

        deny = [r for r in _records() if r["decision"] == audit.DENY]
        assert deny, f"no destination denial audited; records={_records()}"
        record = deny[0]
        assert record["reason"] == audit.REASON_DESTINATION_DENIED
        assert record["detail"] == "metadata_host_blocked"
        assert record["route"] == "/api/show"
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)
        assert record["status"] == response.status_code


def _outbound_probe_app():
    """A minimal app that performs one real guarded outbound call."""
    from fastapi import FastAPI, HTTPException

    from app.destination_policy import DestinationDenied, guarded_async_client

    probe = FastAPI()

    @probe.get("/outbound-probe")
    async def outbound_probe():
        try:
            async with guarded_async_client(purpose="test:audit-probe") as client:
                await client.get("http://169.254.169.254/latest/meta-data/")
        except DestinationDenied as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.to_detail()) from exc
        return {"ok": True}

    audit.install(probe, surface="proxy")
    return probe


# ── 3. The audit-without-payload demonstration ────────────────────────


class TestAuditWithoutPayload:
    """The roadmap's "audit without payload" demonstration, made explicit.

    One request per direction, each carrying a recognisable payload marker, a
    recognisable header marker and a recognisable query-string marker. The
    record and *every* captured gateway log line must contain none of: the
    payload, the ``Authorization`` header value, the bearer token, the query
    string.
    """

    def _needles(self) -> list[str]:
        return [
            PAYLOAD_MARKER,
            HEADER_MARKER,
            CLIENT_TOKEN,
            CLIENT_TOKEN.split("_", 1)[1],
            f"Bearer {CLIENT_TOKEN}",
            QUERY_MARKER,
            "please read this whole prompt",
        ]

    def _assert_clean(self, record: dict, caplog) -> None:
        serialized = json.dumps(record)
        lines = _gateway_lines(caplog)
        assert lines, "no log lines captured — the demonstration would be vacuous"
        for needle in self._needles():
            assert needle not in serialized, f"audit record leaked {needle!r}: {serialized}"
            for line in lines:
                assert needle not in line, f"log line leaked {needle!r}: {line}"

    def test_allow_direction(self, proxy_client, caplog):
        caplog.set_level(logging.DEBUG)
        # `/api/show` parses the body (so the payload really travels through the
        # audited path) and answers locally; the model is unknown here, which is
        # irrelevant — the authorization decision is what is audited.
        response = proxy_client.post(
            f"/api/show?trace={QUERY_MARKER}",
            headers={
                "Authorization": f"Bearer {CLIENT_TOKEN}",
                "X-Request-Marker": HEADER_MARKER,
            },
            json=_payload(),
        )
        assert response.status_code == 404, response.text
        record = _only_record()
        assert record["decision"] == audit.ALLOW
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)
        self._assert_clean(record, caplog)

    def test_deny_direction(self, proxy_client, caplog):
        caplog.set_level(logging.DEBUG)
        response = proxy_client.post(
            f"/api/config/reload?trace={QUERY_MARKER}",
            headers={
                "Authorization": f"Bearer {CLIENT_TOKEN}",
                "X-Request-Marker": HEADER_MARKER,
            },
            json=_payload(),
        )
        assert response.status_code == 403, response.text
        record = _only_record()
        assert record["decision"] == audit.DENY
        assert record["reason"] == audit.REASON_INSUFFICIENT_SCOPE
        self._assert_clean(record, caplog)

    def test_the_record_cannot_carry_a_payload_by_construction(self):
        """Schema closure: there is no field a payload could travel in."""
        assert "body" not in audit.RECORD_FIELDS
        assert "headers" not in audit.RECORD_FIELDS
        assert "query" not in audit.RECORD_FIELDS
        assert "prompt" not in audit.RECORD_FIELDS
        assert "model" not in audit.RECORD_FIELDS


# ── 4. The auth-header log invariant ──────────────────────────────────


class TestAuthHeaderInvariant:
    def test_no_bearer_reaches_a_log_line_on_any_exercised_path(
        self, proxy_client, admission_config, test_queue, resolvable_models, caplog
    ):
        """Auth, scopes, rate, queue, admission and the destination guard."""
        caplog.set_level(logging.DEBUG)
        admission_config(
            rate_limit={"requests_per_minute": 6, "burst": 2},
            max_body_bytes=1024,
        )

        # 1. auth success, 2. auth failure (invalid), 3. missing header
        proxy_client.get("/v1/queue/status", headers=_auth())
        proxy_client.get("/v1/queue/status", headers=_auth(UNKNOWN_TOKEN))
        proxy_client.get("/v1/queue/status")
        # 4. scope denial
        proxy_client.get("/api/keys", headers=_auth())
        # 5. rate-limit denial (burst 2 already spent on 1-2)
        proxy_client.get("/v1/queue/status", headers=_auth(OTHER_TOKEN))
        # 6. body-too-large denial
        proxy_client.post(
            "/v1/chat/completions",
            headers=_auth(ADMIN_TOKEN),
            content=b"y" * 4096,
        )
        # 7. queue-depth denial
        admission_config(
            rate_limit={"requests_per_minute": 600, "burst": 600},
            queue_max_depth=1,
        )
        queue = test_queue(max_concurrent=1, max_queue_depth=1, queue_timeout=5.0)
        queue.submit("holder", "model", owner_id="holder")
        proxy_client.post("/v1/chat/completions", headers=_auth(), json=_payload())
        # 8. destination denial
        client = TestClient(_outbound_probe_app())
        client.get("/outbound-probe")

        lines = _gateway_lines(caplog)
        assert len(lines) >= 8, f"path coverage too thin: {len(lines)} lines"
        _assert_no_credentials(lines)
        # Nothing anywhere, including the audit lines, carries a raw token.
        for token in (CLIENT_TOKEN, OTHER_TOKEN, ADMIN_TOKEN, UNKNOWN_TOKEN):
            assert all(token not in line for line in lines)

    def test_the_guard_is_not_vacuous(self):
        """A deliberately leaky line must fail the invariant helper."""
        leaky = [
            "request finished",
            f"Authorization: Bearer {CLIENT_TOKEN}",
            "ok",
        ]
        with pytest.raises(AssertionError) as excinfo:
            _assert_no_credentials(leaky)
        assert "credential material" in str(excinfo.value)
        assert contains_credentials(leaky[1])
        # And the shape detection is not tied to one spelling.
        for line in (
            f"x-api-key: {CLIENT_TOKEN}",
            f"Authorization={CLIENT_TOKEN}",
            f"bearer {CLIENT_TOKEN}",
        ):
            assert contains_credentials(line), line
        # ... and already-redacted material is not reported as a leak.
        assert not contains_credentials("header was Authorization: ***redacted***")
        assert not contains_credentials("token is ***redacted***")

    def test_the_g1_02_filter_makes_a_leak_unloggable(self, keys_file):
        """The mechanism is the existing G1-02 filter, not a second one.

        Note on why the exercised-path test above can still catch a leak: pytest
        attaches its capture handler after ``install_secret_redaction()`` ran, so
        the records the invariant scans are the *pre-filter* messages the
        application actually tried to log. The redaction filter is the belt; the
        invariant is the braces that fire first.
        """
        from app.log_safety import SecretRedactionFilter

        # The key store must be registered with the filter first: the Guardian
        # tokens this service hands out are secrets, and a bare one has no
        # `Bearer` prefix for the shape patterns to match on.
        auth.load_api_keys()

        handler = logging.Handler()
        handler.addFilter(SecretRedactionFilter())
        captured: list[str] = []
        handler.emit = lambda record: captured.append(record.getMessage())  # type: ignore[method-assign]

        probe = logging.getLogger("Audit.FilterProbe")
        probe.propagate = False
        probe.handlers.clear()
        probe.addHandler(handler)
        probe.setLevel(logging.DEBUG)
        try:
            probe.info("🔑 token is %s", CLIENT_TOKEN)
            probe.warning("header was Authorization: Bearer %s", CLIENT_TOKEN)
        finally:
            probe.removeHandler(handler)
            probe.propagate = True
            probe.handlers.clear()
            probe.setLevel(logging.NOTSET)

        assert captured
        assert all(CLIENT_TOKEN not in line for line in captured), captured
        assert any("***redacted***" in line for line in captured), captured
        _assert_no_credentials(captured)  # passes *because* of the filter

    def test_a_filterless_handler_would_leak(self):
        """Proof the assertion is about the filter, not about the test's luck."""
        leak_logger = logging.getLogger("Audit.LeakProbe")
        leak_logger.propagate = False
        leak_logger.handlers.clear()
        handler = logging.Handler()
        captured: list[str] = []
        handler.emit = lambda record: captured.append(record.getMessage())  # type: ignore[method-assign]
        leak_logger.addHandler(handler)
        leak_logger.setLevel(logging.DEBUG)
        try:
            leak_logger.info("token=%s", CLIENT_TOKEN)
        finally:
            leak_logger.removeHandler(handler)
            leak_logger.propagate = True
            leak_logger.handlers.clear()
            leak_logger.setLevel(logging.NOTSET)

        assert captured
        with pytest.raises(AssertionError):
            _assert_no_credentials(captured)


# ── 5. Bounded storage ────────────────────────────────────────────────


class TestBoundedStorage:
    def test_ring_buffer_stops_at_its_bound(self, proxy_client, audit_config):
        audit_config(buffer_size=8)
        audit.reset_cache()
        for _ in range(60):
            assert proxy_client.get("/v1/queue/status", headers=_auth()).status_code == 200

        assert audit.buffer_size() == 8
        assert len(audit.recent()) == 8
        assert audit.emitted_count() == 60  # counted, not retained

    def test_a_huge_configured_buffer_is_clamped(self, proxy_client, audit_config):
        audit_config(buffer_size=10**9)
        audit.reset_cache()
        proxy_client.get("/v1/queue/status", headers=_auth())
        assert audit.buffer_size() == audit.MAX_BUFFER_SIZE
        assert len(audit.recent()) == 1

    def test_malformed_buffer_size_falls_back_to_the_shipped_default(self, proxy_client, audit_config, caplog):
        caplog.set_level(logging.ERROR, logger="Guardian.Audit")
        audit_config(buffer_size="lots")
        audit.reset_cache()
        proxy_client.get("/v1/queue/status", headers=_auth())
        assert audit.buffer_size() == audit.DEFAULT_BUFFER_SIZE
        assert "buffer_size" in caplog.text

    def test_malformed_include_key_name_omits_names(self, proxy_client, audit_config):
        """Privacy direction: a typo must never *add* a name to a record."""
        audit_config(include_key_name="yes")
        audit.reset_cache()
        proxy_client.get("/v1/queue/status", headers=_auth())
        assert _only_record()["key_name"] is None
        assert _only_record()["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)

    def test_include_key_name_false_keeps_the_fingerprint_only(self, proxy_client, audit_config):
        audit_config(include_key_name=False)
        audit.reset_cache()
        proxy_client.get("/v1/queue/status", headers=_auth())
        record = _only_record()
        assert record["key_name"] is None
        assert record["key_fingerprint"] == _fingerprint(CLIENT_TOKEN)

    def test_malformed_log_decisions_keeps_auditing(self, proxy_client, audit_config):
        audit_config(log_decisions="nope")
        audit.reset_cache()
        proxy_client.get("/v1/queue/status", headers=_auth())
        assert len(audit.recent()) == 1  # the buffer keeps the record

    def test_log_decisions_false_keeps_the_buffer_and_drops_the_line(self, proxy_client, audit_config, caplog):
        audit_config(log_decisions=False)
        audit.reset_cache()
        caplog.set_level(logging.DEBUG)
        proxy_client.get("/v1/queue/status", headers=_auth())
        assert len(audit.recent()) == 1
        assert not [line for line in _gateway_lines(caplog) if '"event":"audit"' in line]

    def test_metric_reason_labels_stay_bounded(self):
        """A caller cannot widen the metric with an arbitrary reason string."""
        config = audit.AuditConfig()
        for index in range(200):
            audit._emit(
                audit._build_record(
                    surface="proxy",
                    decision="deny",
                    reason=f"attacker-supplied-{index}",
                    status=403,
                ),
                config=config,
            )
        reasons = {
            sample.labels["reason"]
            for metric in metrics.AUDIT_RECORDS.collect()
            for sample in metric.samples
            if "reason" in sample.labels
        }
        assert reasons <= audit.REASONS
        assert "other" in reasons
        assert len(reasons) <= len(audit.REASONS)

    def test_one_record_has_a_fixed_size_ceiling(self):
        record = audit._build_record(
            surface="proxy",
            decision="deny",
            reason="insufficient_scope",
            status=403,
            method="POST",
            route="/" + "r" * 50_000,
            required_scope="admin",
            scopes=["admin"] * 100,
            key_fingerprint="a" * 500,
            key_name="n" * 50_000,
            detail="d" * 50_000,
            limit="l" * 50_000,
            limit_value=10**30,
        )
        assert len(json.dumps(record)) < audit._MAX_RECORD_BYTES
        assert len(record["route"]) <= 120
        assert len(record["key_name"]) <= 64
        assert record["key_fingerprint"] is None  # a 500-char "hex" is not a fingerprint

    def test_records_are_never_retained_from_a_public_flood(self, proxy_client, audit_config):
        """An unauthenticated flood makes no decisions, so it stores nothing."""
        audit_config(buffer_size=4)
        audit.reset_cache()
        for _ in range(25):
            assert proxy_client.get("/healthz").status_code == 200
        assert audit.recent() == []
        assert audit.emitted_count() == 0


# ── 6. Configuration and schema ───────────────────────────────────────


class TestAuditConfiguration:
    def test_shipped_config_matches_the_module_defaults(self):
        section = CONFIG.get("audit")
        assert isinstance(section, dict)
        assert section["buffer_size"] == audit.DEFAULT_BUFFER_SIZE
        assert section["log_decisions"] is audit.DEFAULT_LOG_DECISIONS
        assert section["include_key_name"] is audit.DEFAULT_INCLUDE_KEY_NAME

    def test_absent_section_uses_the_shipped_settings(self, monkeypatch, caplog):
        monkeypatch.delitem(CONFIG, "audit", raising=False)
        audit.reset_cache()
        caplog.set_level(logging.WARNING, logger="Guardian.Audit")
        config = audit.resolve_config()
        assert config.buffer_size == audit.DEFAULT_BUFFER_SIZE
        assert config.include_key_name is True
        assert config.source == "shipped-default"

    def test_non_mapping_section_keeps_auditing_without_names(self, monkeypatch):
        monkeypatch.setitem(CONFIG, "audit", "on")
        audit.reset_cache()
        config = audit.resolve_config()
        assert config.include_key_name is False
        assert config.buffer_size == audit.DEFAULT_BUFFER_SIZE
        assert config.log_decisions is True

    def test_config_is_cached_per_signature(self, monkeypatch):
        monkeypatch.setitem(CONFIG, "audit", {"buffer_size": 3})
        audit.reset_cache()
        first = audit.resolve_config()
        second = audit.resolve_config()
        assert first is second
        assert first.buffer_size == 3

    def test_record_fields_are_the_documented_schema(self):
        assert set(audit.RECORD_FIELDS) == {
            "ts", "event", "surface", "decision", "reason", "detail", "method",
            "route", "required_scope", "scopes", "key_fingerprint", "key_name",
            "status", "limit", "limit_value",
        }
        assert len(audit.RECORD_FIELDS) == 15

    def test_reason_vocabulary_size_is_pinned_by_the_docs(self):
        """The record and API_REFERENCE both claim 2 x 13 = 26 metric series."""
        assert len(audit.REASONS) == 13
        assert audit.REASON_OTHER in audit.REASONS
