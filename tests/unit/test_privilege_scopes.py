"""G1-01 — privilege scopes, admin isolation and the migration grace window.

Decision record: ``docs/decisions/G1-01-privilege-scopes.md``.
Route declaration: ``app/proxy/scopes.py:ROUTE_SCOPES``.
Enforcement point: ``app/proxy/auth.py:verify_api_key``.

The class matrix is exercised end to end through both FastAPI apps with a
temporary key store (the repo's real ``config/guardian.keys.yaml`` is never
read or written). Both sides of the grace window use fixed timestamps — in the
past and in the future — so no test waits on wall-clock time.
"""

from __future__ import annotations

import json
import logging

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config_loader import CONFIG
from app.gateway import admin_api
from app.proxy import auth, metrics, scopes, server

# ── Key fixtures ───────────────────────────────────────────────────────

CLIENT_TOKEN = "client_0000000000000000000000000000c1"
OPERATOR_TOKEN = "operator_00000000000000000000000000c2"
ADMIN_TOKEN = "admin_00000000000000000000000000000c3"
CAPTURE_TOKEN = "capture_000000000000000000000000000c4"
LEGACY_TOKEN = "legacy_0000000000000000000000000000c5"
TYPO_TOKEN = "typo_000000000000000000000000000000c6"
MALFORMED_TOKEN = "malformed_0000000000000000000000000c7"
NO_CLOUD_ADMIN_TOKEN = "admincloud_00000000000000000000000c8"

KEYS: dict[str, dict] = {
    CLIENT_TOKEN: {
        "name": "plain-client",
        "created_at": 1,
        "metadata": {"scopes": ["client"]},
    },
    OPERATOR_TOKEN: {
        "name": "lifecycle-script",
        "created_at": 2,
        "metadata": {"scopes": ["operator"]},
    },
    ADMIN_TOKEN: {
        "name": "dashboard",
        "created_at": 3,
        "metadata": {"scopes": ["admin"]},
    },
    CAPTURE_TOKEN: {
        "name": "capture-exporter",
        "created_at": 4,
        "metadata": {"scopes": ["capture-export"]},
    },
    LEGACY_TOKEN: {"name": "legacy-unscoped", "created_at": 5, "metadata": {}},
    TYPO_TOKEN: {
        "name": "typo-key",
        "created_at": 6,
        "metadata": {"scopes": ["clinet"]},
    },
    MALFORMED_TOKEN: {
        "name": "malformed-key",
        "created_at": 7,
        "metadata": {"scopes": "admin"},
    },
    NO_CLOUD_ADMIN_TOKEN: {
        "name": "no-cloud-admin",
        "created_at": 8,
        "metadata": {"scopes": ["admin"]},
        "cloud_gateway_access": False,
    },
}

PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"
ABSENT = object()

#: Every key class against ``GET /metrics`` (issue #46): the payload names
#: models, queue depth and process state, so it is ``client`` data. ``operator``
#: and ``admin`` reach it through scope implication; ``capture-export`` alone
#: does not, exactly like ordinary inference.
METRICS_MATRIX = [
    ("no_key", None, 401),
    ("unknown_key", "fake_00000000000000000000000000000000", 401),
    ("capture-export", CAPTURE_TOKEN, 403),
    ("client", CLIENT_TOKEN, 200),
    ("operator", OPERATOR_TOKEN, 200),
    ("admin", ADMIN_TOKEN, 200),
]

#: Every privileged route a ``client`` key must be refused on (issue #52).
PRIVILEGED_CALLS = [
    ("get", "/api/keys", None),
    ("post", "/api/keys", {"name": "escalation-attempt"}),
    ("post", "/api/config/reload", None),
    ("post", "/admin/load", {"model": "test"}),
    ("post", "/admin/unload", None),
    ("post", "/api/capture/rotate", None),
    ("get", "/api/capture/status", None),
    ("get", "/api/status", None),
    ("get", "/api/session/list", None),
]


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    """Point the auth module at a temporary key store and clear its cache."""
    path = tmp_path / "guardian.keys.yaml"
    path.write_text(yaml.safe_dump(KEYS), encoding="utf-8")
    monkeypatch.setattr(auth, "API_KEYS_FILE", path)
    monkeypatch.setattr(auth, "LEGACY_APIKEYS_FILE", tmp_path / "no-legacy.json")
    auth._API_KEYS_CACHE.clear()
    yield path
    auth._API_KEYS_CACHE.clear()


@pytest.fixture
def deadline(monkeypatch):
    """Set ``auth.scopes_enforced_after`` for one test and restore it."""

    def _apply(value):
        if value is ABSENT:
            CONFIG.pop("auth", None)
        else:
            CONFIG["auth"] = {"scopes_enforced_after": value}
        scopes._TIMESTAMP_CACHE.clear()

    _apply(FUTURE)
    yield _apply
    CONFIG["auth"] = {"scopes_enforced_after": "2026-11-11T00:00:00Z"}
    scopes._TIMESTAMP_CACHE.clear()


@pytest.fixture
def stubbed_admin(monkeypatch):
    """Replace the privileged handlers so the tests assert authorization only."""

    async def _ok(*_args, **_kwargs):
        return {"ok": True}

    for name in ("reload_config", "admin_load", "get_capture_status", "rotate_capture_file"):
        monkeypatch.setattr(admin_api, name, _ok)
    return _ok


@pytest.fixture
def idle_backend(monkeypatch):
    """Make ``/admin/unload`` take its cheap already-unloaded path."""

    class _IdleManager:
        is_unloaded = True
        current_model = "test-model"

    monkeypatch.setattr(server, "caretaker_client", None)
    monkeypatch.setattr(server, "model_manager", _IdleManager())


@pytest.fixture
def proxy_client(keys_file, stubbed_admin, idle_backend):
    return TestClient(server.app)


@pytest.fixture
def stubbed_metrics(monkeypatch):
    """Keep ``/metrics`` hermetic: the gate is what this suite asserts.

    The gauges themselves are covered by ``tests/unit/test_metrics.py``; here
    the endpoint must only do the cheap work of rendering the registry.
    """

    async def _async_noop(*_args, **_kwargs):
        return None

    def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "update_queue_metrics", _noop)
    monkeypatch.setattr(server, "update_system_metrics", _noop)
    monkeypatch.setattr(server, "update_capture_metrics", _noop)
    monkeypatch.setattr(server, "update_gpu_metrics_cached", _async_noop)
    monkeypatch.setattr(server, "get_capture_sink_snapshot", _async_noop)


@pytest.fixture
def metrics_client(keys_file, stubbed_metrics):
    """Proxy app with a temporary key store and a cheap metrics handler."""
    return TestClient(server.app)


def _call(client: TestClient, method: str, path: str, body=None, token: str = CLIENT_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    if body is None:
        return getattr(client, method)(path, headers=headers)
    return getattr(client, method)(path, headers=headers, json=body)


def _assert_scope_denial(response, required: str) -> dict:
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["type"] == "insufficient_scope", detail
    assert detail["required_scope"] == required, detail
    assert detail["route"].endswith(response.request.url.path), detail
    assert "WWW-Authenticate" in response.headers
    assert 'error="insufficient_scope"' in response.headers["WWW-Authenticate"]
    return detail


# ── Declarations ───────────────────────────────────────────────────────


class TestRouteDeclaration:
    def test_head_folds_into_get(self):
        assert scopes.required_scope_for("HEAD", "/api/keys") is scopes.Scope.ADMIN
        assert scopes.required_scope_for("post", "/admin/load") is scopes.Scope.OPERATOR

    def test_undeclared_route_has_no_scope(self):
        assert scopes.required_scope_for("GET", "/not/a/declared/route") is None
        assert scopes.required_scope_for(None, "/api/keys") is None

    def test_public_routes_are_declared_public(self):
        for path in ("/healthz", "/"):
            assert scopes.ROUTE_SCOPES[("GET", path)] is scopes.Scope.PUBLIC
        assert scopes.ROUTE_SCOPES[("MOUNT", "/static")] is scopes.Scope.PUBLIC

    def test_metrics_requires_a_client_key(self):
        """Issue #46: the payload names models, queue depth and process state."""
        assert scopes.ROUTE_SCOPES[("GET", "/metrics")] is scopes.Scope.CLIENT


# ── Class matrix ───────────────────────────────────────────────────────


class TestExplicitScopes:
    def test_client_key_reaches_client_routes(self, proxy_client):
        assert _call(proxy_client, "get", "/v1/queue/status").status_code == 200
        assert _call(proxy_client, "get", "/api/tags").status_code == 200

    @pytest.mark.parametrize("method,path,body", PRIVILEGED_CALLS)
    def test_client_key_is_denied_on_privileged_routes(self, proxy_client, method, path, body):
        response = _call(proxy_client, method, path, body)
        _assert_scope_denial(
            response,
            scopes.ROUTE_SCOPES[(method.upper(), path)].value,
        )

    def test_operator_key_reaches_lifecycle_but_not_admin(self, proxy_client):
        assert _call(proxy_client, "post", "/admin/load", {"model": "test"}, token=OPERATOR_TOKEN).status_code == 200
        assert _call(proxy_client, "post", "/admin/unload", None, token=OPERATOR_TOKEN).status_code == 200
        assert _call(proxy_client, "get", "/v1/queue/status", token=OPERATOR_TOKEN).status_code == 200

        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=OPERATOR_TOKEN), "admin")
        _assert_scope_denial(
            _call(proxy_client, "post", "/api/config/reload", token=OPERATOR_TOKEN), "admin"
        )
        _assert_scope_denial(
            _call(proxy_client, "post", "/api/capture/rotate", token=OPERATOR_TOKEN),
            "capture-export",
        )

    @pytest.mark.parametrize("method,path,body", PRIVILEGED_CALLS)
    def test_admin_key_reaches_every_privileged_route(self, proxy_client, method, path, body):
        response = _call(proxy_client, method, path, body, token=ADMIN_TOKEN)
        assert response.status_code == 200, f"{method} {path}: {response.text}"

    def test_capture_export_key_reaches_capture_only(self, proxy_client):
        assert _call(proxy_client, "get", "/api/capture/status", token=CAPTURE_TOKEN).status_code == 200
        assert _call(proxy_client, "post", "/api/capture/rotate", token=CAPTURE_TOKEN).status_code == 200

        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=CAPTURE_TOKEN), "admin")
        _assert_scope_denial(
            _call(proxy_client, "get", "/v1/queue/status", token=CAPTURE_TOKEN), "client"
        )

    def test_dashboard_surface_requires_admin(self, keys_file):
        from app import main as dashboard_module

        dashboard = TestClient(dashboard_module.app)
        _assert_scope_denial(_call(dashboard, "get", "/api/keys"), "admin")
        assert _call(dashboard, "get", "/api/keys", token=ADMIN_TOKEN).status_code == 200

    def test_listed_keys_expose_their_scope_state(self, proxy_client):
        payload = _call(proxy_client, "get", "/api/keys", token=ADMIN_TOKEN).json()
        by_name = {entry["name"]: entry for entry in payload["keys"]}
        assert by_name["dashboard"]["scopes"] == ["admin"]
        assert by_name["dashboard"]["scope_mode"] == "explicit"
        assert by_name["dashboard"]["effective_scopes"] == [
            "admin",
            "capture-export",
            "client",
            "operator",
        ]
        assert by_name["legacy-unscoped"]["scopes"] is None
        assert by_name["typo-key"]["scope_mode"] == "invalid"


# ── /metrics gate (issue #46) ─────────────────────────────────────────


class TestMetricsGate:
    """``GET /metrics`` is ``client`` data: any key may scrape, no key may not."""

    @pytest.mark.parametrize("label,token,expected", METRICS_MATRIX)
    def test_metrics_scope_matrix(self, metrics_client, label, token, expected):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        response = metrics_client.get("/metrics", headers=headers)
        assert response.status_code == expected, f"{label}: {response.text}"

    def test_unauthenticated_metrics_is_the_existing_typed_401(self, metrics_client):
        """No new error shape: the 401 is the one every protected route returns."""
        response = metrics_client.get("/metrics")
        assert response.status_code == 401
        assert response.json() == {"detail": "API Key required"}
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "guardian_" not in response.text

    def test_unknown_key_metrics_is_the_existing_typed_401(self, metrics_client):
        response = metrics_client.get(
            "/metrics",
            headers={"Authorization": "Bearer fake_00000000000000000000000000000000"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid API Key"}
        assert "guardian_" not in response.text

    def test_capture_export_key_is_denied_like_ordinary_inference(self, metrics_client):
        detail = _assert_scope_denial(
            _call(metrics_client, "get", "/metrics", token=CAPTURE_TOKEN), "client"
        )
        assert detail["granted_scopes"] == ["capture-export"]

    def test_client_key_gets_the_same_prometheus_payload(self, metrics_client):
        response = _call(metrics_client, "get", "/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "guardian_" in response.text
        assert "# HELP" in response.text

    def test_healthz_still_needs_no_key(self, metrics_client):
        response = metrics_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


# ── Grace window ───────────────────────────────────────────────────────


class TestGraceWindow:
    def test_unscoped_key_keeps_privilege_before_the_deadline(
        self, proxy_client, deadline, caplog
    ):
        deadline(FUTURE)
        before = metrics.SCOPE_GRACE_CALLS.labels(scope="admin", route="GET /api/keys")._value.get()
        with caplog.at_level(logging.WARNING, logger="Auth"):
            response = _call(proxy_client, "get", "/api/keys", token=LEGACY_TOKEN)
        assert response.status_code == 200

        grace_lines = [line for line in caplog.text.splitlines() if "scope-grace" in line]
        assert grace_lines, caplog.text
        event = json.loads(grace_lines[0].split("scope-grace", 1)[1].strip())
        assert event["event"] == "scope_grace"
        assert event["key"] == "legacy-unscoped"
        assert event["route"] == "GET /api/keys"
        assert event["missing_scope"] == "admin"

        after = metrics.SCOPE_GRACE_CALLS.labels(scope="admin", route="GET /api/keys")._value.get()
        assert after == before + 1

    def test_unscoped_key_is_denied_after_the_deadline(self, proxy_client, deadline):
        deadline(PAST)
        detail = _assert_scope_denial(
            _call(proxy_client, "get", "/api/keys", token=LEGACY_TOKEN), "admin"
        )
        assert detail["granted_scopes"] == ["client"]

    def test_unscoped_key_keeps_client_inference_on_both_sides(self, proxy_client, deadline):
        for value in (FUTURE, PAST):
            deadline(value)
            assert _call(proxy_client, "get", "/v1/queue/status", token=LEGACY_TOKEN).status_code == 200

    def test_explicit_client_key_is_denied_inside_the_window(self, proxy_client, deadline):
        deadline(FUTURE)
        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=CLIENT_TOKEN), "admin")

    def test_explicit_admin_key_works_after_the_window(self, proxy_client, deadline):
        deadline(PAST)
        assert _call(proxy_client, "get", "/api/keys", token=ADMIN_TOKEN).status_code == 200

    def test_moving_the_deadline_back_into_the_future_restores_grace(
        self, proxy_client, deadline
    ):
        deadline(PAST)
        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=LEGACY_TOKEN), "admin")
        deadline(FUTURE)
        assert _call(proxy_client, "get", "/api/keys", token=LEGACY_TOKEN).status_code == 200


# ── Fail-closed ────────────────────────────────────────────────────────


class TestFailClosed:
    def test_unknown_scope_token_denies_privilege_but_keeps_inference(self, proxy_client):
        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=TYPO_TOKEN), "admin")
        assert _call(proxy_client, "get", "/v1/queue/status", token=TYPO_TOKEN).status_code == 200

    def test_malformed_scope_declaration_denies_privilege(self, proxy_client):
        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=MALFORMED_TOKEN), "admin")
        assert (
            _call(proxy_client, "get", "/v1/queue/status", token=MALFORMED_TOKEN).status_code == 200
        )

    def test_malformed_timestamp_enforces_immediately(self, proxy_client, deadline):
        deadline("not-an-iso-timestamp")
        _assert_scope_denial(_call(proxy_client, "get", "/api/keys", token=LEGACY_TOKEN), "admin")
        assert _call(proxy_client, "get", "/v1/queue/status", token=LEGACY_TOKEN).status_code == 200

    def test_absent_auth_section_uses_the_shipped_deadline(self, proxy_client, deadline, caplog):
        """Absent ≠ malformed: an un-reconciled config keeps the shipped window.

        Reboot safety: a config file written before G1-01 (or an operator's own
        copy of it) must not turn every legacy key into a `client`-only key the
        instant the gateway restarts. A malformed value still enforces
        immediately — that case is covered separately.
        """
        with caplog.at_level("WARNING"):
            deadline(ABSENT)
        response = _call(proxy_client, "get", "/api/keys", token=LEGACY_TOKEN)
        assert response.status_code == 200, response.text
        assert any("shipped default" in record.getMessage() for record in caplog.records)

    def test_unreadable_key_store_rejects_every_request(self, tmp_path, monkeypatch, stubbed_admin):
        unreadable = tmp_path / "keys-as-directory.yaml"
        unreadable.mkdir()
        monkeypatch.setattr(auth, "API_KEYS_FILE", unreadable)
        monkeypatch.setattr(auth, "LEGACY_APIKEYS_FILE", tmp_path / "no-legacy.json")
        auth._API_KEYS_CACHE.clear()
        client = TestClient(server.app)
        try:
            assert _call(client, "get", "/v1/queue/status").status_code == 401
            assert _call(client, "get", "/api/keys", token=ADMIN_TOKEN).status_code == 401
        finally:
            auth._API_KEYS_CACHE.clear()

    def test_undeclared_protected_route_is_denied(self):
        """A protected route without a declaration must deny, never admit."""

        class _Route:
            path = "/not/declared"

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/not/declared",
                "headers": [],
                "route": _Route(),
            }
        )
        with pytest.raises(HTTPException) as excinfo:
            auth._enforce_route_scope(request, {"name": "any-key"})
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail["type"] == "authorization_misconfigured"

    def test_explicit_null_scope_declaration_denies_privilege(
        self, proxy_client, keys_file, deadline
    ):
        """``metadata.scopes: null`` is a declaration, not an absence."""

        deadline(FUTURE)  # even with the window wide open
        document = yaml.safe_load(keys_file.read_text(encoding="utf-8"))
        document["null_key"] = {
            "name": "null-scoped",
            "created_at": 9,
            "metadata": {"scopes": None},
        }
        keys_file.write_text(yaml.safe_dump(document), encoding="utf-8")
        auth._API_KEYS_CACHE.clear()

        _assert_scope_denial(
            _call(proxy_client, "get", "/api/keys", token="null_key"), "admin"
        )
        assert _call(proxy_client, "get", "/v1/queue/status", token="null_key").status_code == 200

    def test_client_routes_are_not_counted_as_grace(self, proxy_client, deadline, caplog):
        """Ordinary inference survives the deadline, so it is not a grace use."""

        deadline(FUTURE)
        before = metrics.SCOPE_GRACE_CALLS.labels(scope="client", route="GET /v1/queue/status")._value.get()
        with caplog.at_level(logging.WARNING, logger="Auth"):
            assert _call(proxy_client, "get", "/v1/queue/status", token=LEGACY_TOKEN).status_code == 200
        after = metrics.SCOPE_GRACE_CALLS.labels(scope="client", route="GET /v1/queue/status")._value.get()
        assert after == before
        assert "scope-grace" not in caplog.text

    def test_unparseable_scope_value_never_grants(self):
        for value in ("not-a-date", "", 12345, {"nested": "dict"}, None):
            assert scopes.parse_enforced_after(value) is None
        assert scopes.grace_window_open() is False or isinstance(scopes.grace_window_open(), bool)


# ── Minting (B-08) ─────────────────────────────────────────────────────


class TestMinting:
    def _mint(self, client, token, body):
        return client.post(
            "/api/keys", headers={"Authorization": f"Bearer {token}"}, json=body
        )

    def test_minted_key_inherits_the_minting_key_scopes(self, proxy_client, keys_file):
        response = self._mint(proxy_client, ADMIN_TOKEN, {"name": "child"})
        assert response.status_code == 200
        assert response.json()["scopes"] == ["admin"]
        assert response.json()["cloud_gateway_access"] is True

        stored = yaml.safe_load(keys_file.read_text(encoding="utf-8"))
        entry = stored[response.json()["api_key"]]
        assert entry["metadata"]["scopes"] == ["admin"]
        assert entry["cloud_gateway_access"] is True
        assert entry["metadata"]["minted_by"] == "dashboard"

    def test_minted_key_cannot_gain_cloud_access(self, proxy_client, keys_file):
        response = self._mint(
            proxy_client,
            NO_CLOUD_ADMIN_TOKEN,
            {"name": "cloud-upgrade", "cloud_gateway_access": True},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["type"] == "scope_escalation"

        inherited = self._mint(proxy_client, NO_CLOUD_ADMIN_TOKEN, {"name": "no-cloud-child"})
        assert inherited.status_code == 200
        assert inherited.json()["cloud_gateway_access"] is False
        stored = yaml.safe_load(keys_file.read_text(encoding="utf-8"))
        assert stored[inherited.json()["api_key"]]["cloud_gateway_access"] is False

    def test_minted_key_cannot_exceed_the_minting_key(self):
        operator_entry = {"name": "op", "metadata": {"scopes": ["operator"]}}
        with pytest.raises(scopes.ScopeError) as excinfo:
            scopes.mint_metadata(operator_entry, {"scopes": ["admin"]})
        assert excinfo.value.code == "scope_escalation"

        grant = scopes.mint_metadata(operator_entry, {"scopes": ["client"]})
        assert grant.metadata["scopes"] == ["client"]

    def test_grandfathered_key_cannot_launder_its_grace_grant(
        self, proxy_client, keys_file, deadline
    ):
        # Pinned to an explicitly open window: the shipped deadline must not
        # decide whether this test exercises minting or the 403 boundary.
        deadline(FUTURE)
        escalation = self._mint(
            proxy_client, LEGACY_TOKEN, {"name": "launder", "metadata": {"scopes": ["admin"]}}
        )
        assert escalation.status_code == 403
        assert escalation.json()["detail"]["type"] == "scope_escalation"

        plain = self._mint(proxy_client, LEGACY_TOKEN, {"name": "plain-child"})
        assert plain.status_code == 200
        assert plain.json()["scopes"] is None
        stored = yaml.safe_load(keys_file.read_text(encoding="utf-8"))
        assert "scopes" not in stored[plain.json()["api_key"]]["metadata"]

    def test_unknown_requested_scope_is_a_typed_400(self, proxy_client):
        response = self._mint(
            proxy_client, ADMIN_TOKEN, {"name": "typo", "metadata": {"scopes": ["admni"]}}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["type"] == "invalid_scope"

    def test_dashboard_mint_obeys_the_same_rule(self, keys_file):
        from app import main as dashboard_module

        dashboard = TestClient(dashboard_module.app)
        ok = dashboard.post(
            "/api/keys",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"name": "ui-child"},
        )
        assert ok.status_code == 200
        assert ok.json()["scopes"] == ["admin"]

        refused = dashboard.post(
            "/api/keys",
            headers={"Authorization": f"Bearer {NO_CLOUD_ADMIN_TOKEN}"},
            json={"name": "ui-cloud", "cloud_gateway_access": True},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"]["type"] == "scope_escalation"

    def test_key_minting_route_itself_requires_admin(self, proxy_client):
        _assert_scope_denial(
            self._mint(proxy_client, CLIENT_TOKEN, {"name": "nope"}), "admin"
        )


class TestMalformedVersusAbsentDeadline:
    """`auth: null` / `auth: []` / a null timestamp are broken, not un-reconciled.

    Absent keys fall back to the shipped deadline (reboot safety, PR-Piet G1-02
    follow-up), but a *present* invalid shape must enforce immediately — else a
    typo silently reopens the migration window for every legacy key.
    """

    @pytest.mark.parametrize(
        "auth_value",
        [None, [], {"scopes_enforced_after": None},
         {"scopes_enforced_after": "not-a-timestamp"},
         {"scopes_enforced_after": 12345}],
        ids=["auth-null", "auth-list", "timestamp-null", "timestamp-garbage", "timestamp-number"],
    )
    def test_malformed_auth_config_enforces_immediately(self, monkeypatch, auth_value):
        monkeypatch.setitem(CONFIG, "auth", auth_value)
        assert scopes.configured_enforced_after() is None

    def test_absent_auth_section_uses_the_shipped_deadline(self, monkeypatch):
        monkeypatch.delitem(CONFIG, "auth", raising=False)
        parsed = scopes.configured_enforced_after()
        assert parsed is not None
        assert parsed.isoformat().startswith("2026-11-11")

    def test_absent_key_inside_a_present_section_uses_the_shipped_deadline(self, monkeypatch):
        monkeypatch.setitem(CONFIG, "auth", {})
        parsed = scopes.configured_enforced_after()
        assert parsed is not None
        assert parsed.isoformat().startswith("2026-11-11")
