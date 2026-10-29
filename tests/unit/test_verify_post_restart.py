"""Unit tests for scripts/verify_post_restart.py.

Every HTTP, systemd and journal surface is injected through
:class:`verify_post_restart.Context`, so no test here touches the network, the
live service or systemd.  The branches that matter most are the ones a
verifier can fake: a down service must FAIL, a shipped-default destination
policy must FAIL, and an audit record that carries a payload must FAIL.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_SPEC = importlib.util.spec_from_file_location(
    "verify_post_restart", REPO / "scripts" / "verify_post_restart.py"
)
assert _SPEC is not None and _SPEC.loader is not None
verify = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("verify_post_restart", verify)
_SPEC.loader.exec_module(verify)

UTC = timezone.utc


# ── Fakes ──────────────────────────────────────────────────────────────


class FakeHTTP:
    """Deterministic stand-in for the probe callable; records every call."""

    def __init__(self, routes: dict[tuple[str, str], object] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        headers=None,
        body=None,
        declared_content_length=None,
        timeout=None,
    ) -> "verify.Response":
        self.calls.append(
            {
                "method": method,
                "path": path,
                "key": key,
                "headers": dict(headers or {}),
                "body": body,
                "declared_content_length": declared_content_length,
            }
        )
        route = self.routes.get((method, path))
        if route is None:
            route = self.routes.get((method, path.split("?")[0]))
        if route is None:
            raise AssertionError(f"unexpected probe {method} {path}")
        if callable(route):
            return route()
        return route


def response(status: int = 200, payload=None, *, text: str | None = None, headers=None) -> "verify.Response":
    body = text.encode() if text is not None else json.dumps(payload if payload is not None else {}).encode()
    return verify.Response(status=status, headers=headers or {}, body=body)


def key_choice(name: str = "test-key", token: str = "token-value", declared=None, mode: str = "explicit"):
    return verify.KeyChoice(
        token=token,
        name=name,
        declared_scopes=declared,
        mode=mode,
        fingerprint="fp-" + name,
    )


BASE_CONFIG: dict = {
    "queue": {"max_concurrent": 1, "queue_timeout_seconds": 1800},
    "capture": {"enabled": True},
    "providers": {"ai-node-local": {"enabled": True}},
    "admission": {
        "max_body_bytes": 33554432,
        "queue_max_depth": 32,
        "queue_full_retry_after_seconds": 60,
        "rate_limit": {"requests_per_minute": 300, "burst": 300},
    },
    "destination_policy": {
        "schemes": ["http", "https"],
        "allow": [
            {"host": "127.0.0.1", "ports": [11440]},
            {"host": "192.168.1.245", "ports": [11440, 11441, 11450, 11451], "cleartext_credentials": True},
            {"host": "openrouter.ai", "ports": [443]},
        ],
    },
}


def make_ctx(**overrides) -> "verify.Context":
    """Build a Context whose host surfaces are all fakes."""
    defaults = dict(
        unit="guardian-llmprovider-gateway",
        scheme="https",
        host="127.0.0.1",
        port=11435,
        repo_root=REPO,
        ca_file="/tmp/fake-ca.pem",
        insecure=False,
        tls_server_name="localhost",
        timeout=5.0,
        journal_lines=1000,
        http=FakeHTTP(),
        systemctl=lambda unit, props, unix: {},
        journal=lambda since, lines: [],
        module_has_attr=lambda module, attr: False,
        config=json.loads(json.dumps(BASE_CONFIG)),
        admin_key=key_choice("admin"),
        client_key=key_choice("client"),
        capture_key=key_choice("capture"),
        legacy_key=key_choice("legacy", mode="grace"),
        service_start=1_700_000_000.0,
        service_reachable=True,
        service={},
    )
    defaults.update(overrides)
    return verify.Context(**defaults)


def audit_record(**fields) -> dict:
    """A schema-shaped audit record; callers add forbidden fields deliberately."""
    from app.proxy.audit import RECORD_FIELDS

    record = {name: None for name in RECORD_FIELDS}
    record.update(
        {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": "audit",
            "surface": "proxy",
            "decision": "allow",
            "reason": "scope_granted",
            "method": "GET",
            "route": "/api/tags",
            "required_scope": "client",
            "scopes": ["client"],
            "status": 200,
            "key_fingerprint": "fp-client",
        }
    )
    record.update(fields)
    return record


def audit_line(**fields) -> str:
    return "📋 audit " + json.dumps(audit_record(**fields), sort_keys=True, separators=(",", ":"))


def seed_media(
    media_dir: Path,
    *,
    file_mode: int | None = None,
    directory_mode: int | None = None,
    mtime: float | None = None,
    write_file: bool = True,
) -> Path:
    """Create the media tree exactly the way the service does.

    The modes are handed to :class:`CaptureConfig` and applied by
    ``app.capture.media`` itself, so the fixture exercises the real writer
    instead of a test-side chmod, and the test never states a permission mask
    of its own.
    """
    from app.capture.config import CaptureConfig
    from app.capture.media import _ensure_media_dir, _write_media_file

    overrides = {}
    if file_mode is not None:
        overrides["file_mode"] = file_mode
    if directory_mode is not None:
        overrides["directory_mode"] = directory_mode
    config = CaptureConfig(**overrides)
    media_dir.mkdir(parents=True, exist_ok=True)
    _ensure_media_dir(media_dir, config)
    if not write_file:
        return media_dir
    _write_media_file(media_dir, "probe", 1, b"payload", "image/png", config)
    path = media_dir / "probe_1.png"
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── service identity ───────────────────────────────────────────────────


SERVICE_FACTS = {
    "ActiveState": "active",
    "SubState": "running",
    "MainPID": "4242",
    "ActiveEnterTimestamp": "1700000000",
}


def service_ctx(**overrides):
    data = dict(service=dict(SERVICE_FACTS))
    data.update(overrides)
    return make_ctx(**data)


def test_service_identity_passes_when_mainpid_owns_listener(monkeypatch):
    monkeypatch.setattr(verify, "listener_pids", lambda host, port: ([4242], "ss"))
    result = verify.check_service_identity(service_ctx())
    assert result.status == verify.PASS
    assert "4242" in result.detail


def test_service_identity_fails_when_unit_inactive(monkeypatch):
    monkeypatch.setattr(verify, "listener_pids", lambda host, port: ([4242], "ss"))
    ctx = service_ctx(service={"ActiveState": "failed", "MainPID": "4242"})
    result = verify.check_service_identity(ctx)
    assert result.status == verify.FAIL
    assert "failed" in result.detail


def test_service_identity_fails_on_stale_process(monkeypatch):
    """Active + a listener owned by *another* pid is exactly the stale deploy."""
    monkeypatch.setattr(verify, "listener_pids", lambda host, port: ([999], "ss"))
    result = verify.check_service_identity(service_ctx())
    assert result.status == verify.FAIL
    assert "stale" in result.detail


def test_service_identity_fails_without_a_listener(monkeypatch):
    monkeypatch.setattr(verify, "listener_pids", lambda host, port: ([], "proc"))
    result = verify.check_service_identity(service_ctx())
    assert result.status == verify.FAIL
    assert "nobody listens" in result.detail


def test_service_identity_fails_when_systemd_is_silent():
    result = verify.check_service_identity(make_ctx(service={}))
    assert result.status == verify.FAIL


# ── queue wait budget ──────────────────────────────────────────────────


def test_queue_wait_budget_passes():
    http = FakeHTTP(
        {("GET", "/v1/queue/status"): response(200, {"queue_timeout_enforced": True, "queue_timeout_s": 1800})}
    )
    result = verify.check_queue_wait_budget(make_ctx(http=http))
    assert result.status == verify.PASS
    assert "1800" in result.detail


def test_queue_wait_budget_fails_when_not_enforced():
    http = FakeHTTP(
        {("GET", "/v1/queue/status"): response(200, {"queue_timeout_enforced": False, "queue_timeout_s": 1800})}
    )
    result = verify.check_queue_wait_budget(make_ctx(http=http))
    assert result.status == verify.FAIL
    assert "queue_timeout_enforced" in result.detail


def test_queue_wait_budget_fails_on_config_mismatch():
    http = FakeHTTP(
        {("GET", "/v1/queue/status"): response(200, {"queue_timeout_enforced": True, "queue_timeout_s": 300})}
    )
    result = verify.check_queue_wait_budget(make_ctx(http=http))
    assert result.status == verify.FAIL
    assert "1800" in result.detail


# ── capture modes ──────────────────────────────────────────────────────


def test_capture_modes_passes(tmp_path, monkeypatch):
    media = tmp_path / "media"
    seed_media(media, file_mode=0o640, directory_mode=0o750)
    monkeypatch.setattr(verify, "_capture_root", lambda ctx: (media, 0o750, 0o640))
    result = verify.check_capture_modes(make_ctx(http=capture_status_http()))
    assert result.status == verify.PASS


def test_capture_modes_fails_on_a_drifted_directory(tmp_path, monkeypatch):
    """The live media/ tree drifted to 0755 before #54; any non-0750 must fail.

    ``CaptureConfig`` refuses a world-accessible mode by design, so the fixture
    drifts to 0o640 (group-readable, no execute) — the point under test is that
    the observed mode must equal the configured one.
    """
    media = tmp_path / "media"
    seed_media(media, file_mode=0o640, directory_mode=0o640, write_file=False)
    monkeypatch.setattr(verify, "_capture_root", lambda ctx: (media, 0o750, 0o640))
    result = verify.check_capture_modes(make_ctx(http=capture_status_http()))
    assert result.status == verify.FAIL
    assert "0o640" in result.detail


def test_capture_modes_fails_on_loose_file_after_restart(tmp_path, monkeypatch):
    media = tmp_path / "media"
    seed_media(media, file_mode=0o600, directory_mode=0o750)
    monkeypatch.setattr(verify, "_capture_root", lambda ctx: (media, 0o750, 0o640))
    result = verify.check_capture_modes(make_ctx(http=capture_status_http()))
    assert result.status == verify.FAIL
    assert "0o600" in result.detail


def test_capture_modes_skips_file_mode_without_a_new_file(tmp_path, monkeypatch):
    media = tmp_path / "media"
    seed_media(media, file_mode=0o600, directory_mode=0o750, mtime=1_600_000_000.0)
    monkeypatch.setattr(verify, "_capture_root", lambda ctx: (media, 0o750, 0o640))
    result = verify.check_capture_modes(make_ctx(http=capture_status_http()))
    assert result.status == verify.SKIP
    assert "unverified" in result.detail


def test_capture_modes_fails_when_the_service_reports_wrong_modes(tmp_path, monkeypatch):
    media = tmp_path / "media"
    seed_media(media, file_mode=0o640, directory_mode=0o750)
    http = FakeHTTP(
        {("GET", "/api/capture/status"): response(200, {"config": {"file_mode": "0o600", "directory_mode": "0o750"}})}
    )
    monkeypatch.setattr(verify, "_capture_root", lambda ctx: (media, 0o750, 0o640))
    result = verify.check_capture_modes(make_ctx(http=http))
    assert result.status == verify.FAIL
    assert "0o600" in result.detail


# ── dead code ──────────────────────────────────────────────────────────


def test_dead_code_removed_passes():
    result = verify.check_dead_code_removed(make_ctx(module_has_attr=lambda module, attr: False))
    assert result.status == verify.PASS


def test_dead_code_removed_fails_when_get_version_survives():
    result = verify.check_dead_code_removed(make_ctx(module_has_attr=lambda module, attr: True))
    assert result.status == verify.FAIL
    assert "get_version" in result.detail


# ── privilege scopes ───────────────────────────────────────────────────


def test_scope_grace_passes_with_the_journal_line(monkeypatch):
    monkeypatch.setattr(verify, "_grace_deadline", lambda: "2026-11-11T00:00:00+00:00")
    line = (
        'WARNING ⏳ scope-grace {"event":"scope_grace","key":"legacy",'
        '"route":"GET /api/status","missing_scope":"admin"}'
    )
    ctx = make_ctx(
        http=FakeHTTP({("GET", "/api/status"): response(200, {"backend_state": "alive"})}),
        journal=lambda since, lines: [line],
    )
    result = verify.check_privilege_scope_grace(ctx)
    assert result.status == verify.PASS
    assert "client-only" in result.detail


def test_scope_grace_fails_without_the_journal_line(monkeypatch):
    monkeypatch.setattr(verify, "_grace_deadline", lambda: "2026-11-11T00:00:00+00:00")
    ctx = make_ctx(
        http=FakeHTTP({("GET", "/api/status"): response(200, {})}),
        journal=lambda since, lines: ["INFO unrelated line"],
    )
    result = verify.check_privilege_scope_grace(ctx)
    assert result.status == verify.FAIL
    assert "scope-grace" in result.detail


def test_scope_grace_skips_without_a_legacy_key():
    result = verify.check_privilege_scope_grace(make_ctx(legacy_key=None))
    assert result.status == verify.SKIP
    assert "legacy" in result.detail


def test_scope_grace_skips_after_the_window_closed(monkeypatch):
    monkeypatch.setattr(verify, "_grace_deadline", lambda: "2020-01-01T00:00:00+00:00")
    ctx = make_ctx(
        http=FakeHTTP({("GET", "/api/status"): response(403, {"detail": {"error": "insufficient_scope"}})})
    )
    result = verify.check_privilege_scope_grace(ctx)
    assert result.status == verify.SKIP
    assert "client-only" in result.detail


def test_scope_grace_fails_when_the_key_declares_the_scope(monkeypatch):
    monkeypatch.setattr(verify, "_grace_deadline", lambda: "2026-11-11T00:00:00+00:00")
    ctx = make_ctx(
        http=FakeHTTP({("GET", "/api/status"): response(200, {})}),
        legacy_key=key_choice("legacy", declared=("admin",), mode="explicit"),
    )
    result = verify.check_privilege_scope_grace(ctx)
    assert result.status == verify.FAIL
    assert "grant, not grace" in result.detail


# ── metrics gate ───────────────────────────────────────────────────────


class SplitAuthHTTP:
    """Answers ``/metrics`` differently with and without a key."""

    def __init__(self, anonymous_status: int = 401, client_status: int = 200) -> None:
        self.anonymous_status = anonymous_status
        self.client_status = client_status
        self.calls: list[bool] = []

    def __call__(self, method, path, **kwargs):
        self.calls.append(bool(kwargs.get("key")))
        if kwargs.get("key"):
            return response(self.client_status, text="guardian_queue_depth 0\n")
        return response(self.anonymous_status, {"detail": "unauthorized"})


def metrics_http(anonymous_status: int = 401, client_status: int = 200):
    http = SplitAuthHTTP(anonymous_status, client_status)
    return http, http.calls


def test_metrics_gate_passes():
    http, calls = metrics_http()
    result = verify.check_metrics_gate(make_ctx(http=http))
    assert result.status == verify.PASS
    assert calls == [False, True]


def test_metrics_gate_fails_when_anonymous_access_is_allowed():
    http, _ = metrics_http(anonymous_status=200)
    result = verify.check_metrics_gate(make_ctx(http=http))
    assert result.status == verify.FAIL
    assert "401" in result.detail


# ── destination allowlist ──────────────────────────────────────────────


def test_destination_allowlist_passes():
    result = verify.check_destination_allowlist(make_ctx())
    assert result.status == verify.PASS
    assert "cleartext" in result.detail


def test_destination_allowlist_fails_on_shipped_default():
    config = dict(BASE_CONFIG)
    config.pop("destination_policy")
    result = verify.check_destination_allowlist(make_ctx(config=config))
    assert result.status == verify.FAIL
    assert "shipped-default" in result.detail


def test_destination_allowlist_fails_when_the_hop_is_not_flagged():
    config = json.loads(json.dumps(BASE_CONFIG))
    config["destination_policy"]["allow"][1].pop("cleartext_credentials")
    result = verify.check_destination_allowlist(make_ctx(config=config))
    assert result.status == verify.FAIL
    assert "not flagged" in result.detail


def test_destination_allowlist_fails_when_a_port_is_missing():
    config = json.loads(json.dumps(BASE_CONFIG))
    config["destination_policy"]["allow"][1]["ports"] = [11440, 11441]
    result = verify.check_destination_allowlist(make_ctx(config=config))
    assert result.status == verify.FAIL
    assert "ports" in result.detail


# ── admission bounds ───────────────────────────────────────────────────


def admission_http(depth: int = 32, oversized_status: int = 413, limit_bytes: int | None = 33554432, metric: bool = True):
    metrics_text = (
        'guardian_admission_rejections_total{limit="admission.max_body_bytes"} 1.0\n' if metric else ""
    )
    return FakeHTTP(
        {
            ("GET", "/v1/queue/status"): response(200, {"max_queue_depth": depth}),
            ("POST", "/v1/chat/completions"): response(
                oversized_status,
                {"detail": {"error": "request_too_large", "limit_bytes": limit_bytes}}
                if oversized_status == 413
                else {"detail": {"error": "nope"}},
            ),
            ("GET", "/metrics"): response(200, text=metrics_text),
        }
    )


def test_admission_bounds_passes_and_uploads_no_bytes():
    http = admission_http()
    result = verify.check_admission_bounds(make_ctx(http=http))
    assert result.status == verify.PASS
    oversized = [call for call in http.calls if call["method"] == "POST"][0]
    assert oversized["declared_content_length"] == 33554432 + 1
    assert oversized["body"] is None  # the bound refuses a declared size; nothing is uploaded


def test_admission_bounds_fails_without_max_queue_depth():
    result = verify.check_admission_bounds(make_ctx(http=admission_http(depth=None)))
    assert result.status == verify.FAIL
    assert "max_queue_depth" in result.detail


def test_admission_bounds_fails_when_the_config_does_not_declare_the_bound():
    config = json.loads(json.dumps(BASE_CONFIG))
    config.pop("admission")
    result = verify.check_admission_bounds(make_ctx(http=admission_http(), config=config))
    assert result.status == verify.FAIL
    assert "shipped default" in result.detail


def test_admission_bounds_fails_without_the_rejection_metric():
    result = verify.check_admission_bounds(make_ctx(http=admission_http(metric=False)))
    assert result.status == verify.FAIL
    assert "guardian_admission_rejections_total" in result.detail


def test_admission_bounds_fails_when_the_body_is_accepted():
    result = verify.check_admission_bounds(make_ctx(http=admission_http(oversized_status=200)))
    assert result.status == verify.FAIL
    assert "413" in result.detail


# ── metadata audit ─────────────────────────────────────────────────────


def tags_http(status: int = 200):
    return FakeHTTP({("GET", "/api/tags"): response(status, {"models": []})})


def test_metadata_audit_passes_with_one_clean_record():
    ctx = make_ctx(http=tags_http(), journal=lambda since, lines: [audit_line()])
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.PASS
    assert "1 record" in result.detail


def test_metadata_audit_fails_when_the_record_carries_a_payload():
    ctx = make_ctx(
        http=tags_http(),
        journal=lambda since, lines: [audit_line(payload={"messages": ["secret prompt"]})],
    )
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.FAIL
    assert "payload" in result.detail


def test_metadata_audit_fails_when_the_record_has_extra_fields():
    ctx = make_ctx(http=tags_http(), journal=lambda since, lines: [audit_line(headers={"authorization": "x"})])
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.FAIL


def test_metadata_audit_fails_on_more_than_one_record():
    ctx = make_ctx(http=tags_http(), journal=lambda since, lines: [audit_line(), audit_line()])
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.FAIL
    assert "found 2" in result.detail


def test_metadata_audit_fails_when_the_marker_is_echoed():
    ctx = make_ctx(
        http=tags_http(),
        journal=lambda since, lines: [audit_line(detail="SHOULD-NEVER-BE-LOGGED")],
    )
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.FAIL
    assert "echoes" in result.detail


def test_metadata_audit_fails_on_a_refused_probe():
    ctx = make_ctx(http=tags_http(status=500), journal=lambda since, lines: [audit_line()])
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.FAIL


def test_metadata_audit_ignores_records_older_than_the_probe():
    stale = audit_line(ts="2020-01-01T00:00:00.000Z")
    ctx = make_ctx(http=tags_http(), journal=lambda since, lines: [stale])
    result = verify.check_metadata_audit(ctx)
    assert result.status == verify.FAIL
    assert "found 0" in result.detail


# ── log sanitizer ──────────────────────────────────────────────────────


def test_log_sanitizer_passes_on_clean_lines():
    ctx = make_ctx(journal=lambda since, lines: ["INFO Guardian: started", "INFO Auth: ok"])
    result = verify.check_log_sanitizer(ctx)
    assert result.status == verify.PASS


def test_log_sanitizer_fails_on_a_bearer_value():
    ctx = make_ctx(journal=lambda since, lines: ["INFO outbound Authorization: Bearer abcdef1234567890"])
    result = verify.check_log_sanitizer(ctx)
    assert result.status == verify.FAIL
    assert "credential shape" in result.detail
    assert "abcdef1234567890" not in result.detail  # the failure line itself is redacted


def test_log_sanitizer_skips_on_an_empty_journal():
    result = verify.check_log_sanitizer(make_ctx(journal=lambda since, lines: []))
    assert result.status == verify.SKIP


# ── endpoint confinement ───────────────────────────────────────────────


def confinement_http(dot_status: int = 400, encoded_status: int = 400, models_status: int = 200):
    return FakeHTTP(
        {
            ("GET", "/v1/../slots"): response(dot_status, {"detail": {"reason": "path_not_allowed"}}),
            ("GET", "/v1/%252e%252e/slots"): response(
                encoded_status, {"detail": {"reason": "path_not_allowed"}}
            ),
            ("GET", "/v1/models"): response(models_status, {"data": []}),
        }
    )


def test_endpoint_confinement_passes():
    result = verify.check_endpoint_confinement(make_ctx(http=confinement_http()))
    assert result.status == verify.PASS


def test_endpoint_confinement_fails_when_the_path_escapes():
    result = verify.check_endpoint_confinement(make_ctx(http=confinement_http(dot_status=200)))
    assert result.status == verify.FAIL
    assert "path_not_allowed" in result.detail


def test_endpoint_confinement_fails_when_the_encoded_variant_escapes():
    result = verify.check_endpoint_confinement(make_ctx(http=confinement_http(encoded_status=200)))
    assert result.status == verify.FAIL


def test_endpoint_confinement_fails_when_a_legitimate_route_breaks():
    result = verify.check_endpoint_confinement(make_ctx(http=confinement_http(models_status=500)))
    assert result.status == verify.FAIL
    assert "/v1/models" in result.detail


# ── backend-death seam ─────────────────────────────────────────────────


def status_http(**overrides):
    payload = {
        "backend_state": "alive",
        "backend_death": None,
        "caretaker": {"configured": True, "reachable": True, "death_signal": "unsupported"},
    }
    payload.update(overrides)
    return FakeHTTP({("GET", "/api/status"): response(200, payload)})


def test_backend_death_seam_passes_fail_open():
    result = verify.check_backend_death_seam(make_ctx(http=status_http()))
    assert result.status == verify.PASS
    assert "fail-open" in result.detail


def test_backend_death_seam_passes_with_a_reported_death():
    ctx = make_ctx(
        http=status_http(backend_state="dead", backend_death={"cause": "oom"}, caretaker={"reachable": False})
    )
    result = verify.check_backend_death_seam(ctx)
    assert result.status == verify.PASS
    assert "recorded" in result.detail


def test_backend_death_seam_fails_on_an_unknown_state():
    result = verify.check_backend_death_seam(make_ctx(http=status_http(backend_state="healthy")))
    assert result.status == verify.FAIL


def test_backend_death_seam_fails_without_backend_death():
    payload = {"backend_state": "alive", "caretaker": {"reachable": True}}
    ctx = make_ctx(http=FakeHTTP({("GET", "/api/status"): response(200, payload)}))
    result = verify.check_backend_death_seam(ctx)
    assert result.status == verify.FAIL
    assert "backend_death" in result.detail


def test_backend_death_seam_fails_without_caretaker():
    ctx = make_ctx(http=status_http(caretaker=None))
    result = verify.check_backend_death_seam(ctx)
    assert result.status == verify.FAIL
    assert "caretaker.reachable" in result.detail


def test_backend_death_seam_skips_when_the_scope_is_missing():
    ctx = make_ctx(http=FakeHTTP({("GET", "/api/status"): response(403, {"detail": "insufficient_scope"})}))
    result = verify.check_backend_death_seam(ctx)
    assert result.status == verify.SKIP


# ── retained checks ────────────────────────────────────────────────────


def capture_status_http(**overrides):
    config = {
        "file_mode": "0o640",
        "directory_mode": "0o750",
        "enabled": True,
        "active": True,
        "cloud_capture": True,
        "per_client_opt_in": False,
        "retention_days": -1,
        "max_capture_bytes": -1,
    }
    config.update(overrides)
    return FakeHTTP({("GET", "/api/capture/status"): response(200, {"config": config, "writer": {"running": True}})})


def test_capture_pipeline_passes():
    result = verify.check_capture_pipeline(make_ctx(http=capture_status_http()))
    assert result.status == verify.PASS


def test_capture_pipeline_fails_when_the_writer_is_down():
    http = FakeHTTP(
        {
            ("GET", "/api/capture/status"): response(
                200,
                {
                    "config": {"enabled": True, "active": True, "cloud_capture": True, "per_client_opt_in": False,
                               "retention_days": -1, "max_capture_bytes": -1},
                    "writer": {"running": False},
                },
            )
        }
    )
    result = verify.check_capture_pipeline(make_ctx(http=http))
    assert result.status == verify.FAIL
    assert "writer" in result.detail


def test_capture_pipeline_skips_without_the_capture_scope():
    result = verify.check_capture_pipeline(make_ctx(capture_key=None))
    assert result.status == verify.SKIP


def test_key_store_passes(tmp_path, monkeypatch):
    from app.proxy import auth

    path = tmp_path / "guardian.keys.yaml"
    path.write_text("token-one:\n  name: one\ntoken-two:\n  name: two\n")
    monkeypatch.setattr(auth, "API_KEYS_FILE", path)
    ctx = make_ctx(keys={"token-one": {"name": "one"}, "token-two": {"name": "two"}})
    result = verify.check_key_store(ctx)
    assert result.status == verify.PASS
    assert "2 keys" in result.detail


def test_key_store_fails_when_entries_are_missing(tmp_path, monkeypatch):
    from app.proxy import auth

    path = tmp_path / "guardian.keys.yaml"
    path.write_text("token-one:\n  name: one\ntoken-two:\n  name: two\n")
    monkeypatch.setattr(auth, "API_KEYS_FILE", path)
    result = verify.check_key_store(make_ctx(keys={"token-one": {"name": "one"}}))
    assert result.status == verify.FAIL
    assert "declares 2" in result.detail


def test_config_deep_merge_passes():
    result = verify.check_config_deep_merge(make_ctx())
    assert result.status == verify.PASS


def test_config_deep_merge_fails_without_the_queue_section():
    result = verify.check_config_deep_merge(make_ctx(config={"capture": {}}))
    assert result.status == verify.FAIL
    assert "queue.max_concurrent" in result.detail


def catalog_http(**entries):
    def _route():
        return response(200, {"catalog": list(entries.values())})

    return FakeHTTP({("GET", "/api/cloud/catalog"): _route})


def test_cloud_catalog_credentials_pass():
    http = catalog_http(openrouter={"name": "openrouter", "configured": True, "credential_status": "ok"})
    result = verify.check_cloud_catalog_credentials(make_ctx(http=http))
    assert result.status == verify.PASS


def test_cloud_catalog_credentials_fail_on_broken_credentials():
    http = catalog_http(nvidia={"name": "nvidia", "configured": True, "credential_status": "broken"})
    result = verify.check_cloud_catalog_credentials(make_ctx(http=http))
    assert result.status == verify.FAIL
    assert "nvidia" in result.detail


def test_cloud_catalog_credentials_skip_without_an_admin_key():
    result = verify.check_cloud_catalog_credentials(make_ctx(admin_key=None))
    assert result.status == verify.SKIP


# ── runner-level guarantees ────────────────────────────────────────────


def test_an_unreachable_service_fails_every_service_check(monkeypatch, tmp_path):
    """A verifier that passes on a down service is worse than none."""

    def _unreachable(*args, **kwargs):
        raise verify.ProbeError("connection refused")

    ctx = make_ctx(http=_unreachable, service_reachable=False)
    results = {result.check_id: result for result in verify.run_checks(ctx)}

    for check_id in (
        "queue_wait_budget",
        "privilege_scope_grace",
        "metrics_gate",
        "admission_bounds",
        "metadata_audit",
        "log_sanitizer",
        "endpoint_confinement",
        "backend_death_seam",
        "capture_pipeline",
        "cloud_catalog_credentials",
    ):
        assert results[check_id].status == verify.FAIL, check_id
        assert "unreachable" in results[check_id].detail
    assert verify.exit_code_for(list(results.values())) == verify.EXIT_FAIL


def test_run_checks_reports_every_check_once():
    results = verify.run_checks(make_ctx())
    ids = [result.check_id for result in results]
    assert ids == [check_id for check_id, _ in verify.CHECKS]
    assert len(ids) == len(set(ids))


def test_exit_code_maps_pass_fail_skip():
    ok = verify.Result("a", "a", verify.PASS)
    skipped = verify.Result("b", "b", verify.SKIP)
    failed = verify.Result("c", "c", verify.FAIL)
    assert verify.exit_code_for([ok]) == verify.EXIT_PASS
    assert verify.exit_code_for([ok, skipped]) == verify.EXIT_SKIP
    assert verify.exit_code_for([ok, skipped, failed]) == verify.EXIT_FAIL


def test_prepare_records_the_service_start_and_the_unreachable_reason(monkeypatch):
    def _unreachable(*args, **kwargs):
        raise verify.ProbeError("[Errno 111] Connection refused")

    ctx = make_ctx(
        http=_unreachable,
        systemctl=lambda unit, props, unix: {"ActiveState": "active", "MainPID": "1",
                                             "ActiveEnterTimestamp": "1700000000"},
    )
    monkeypatch.setattr("app.config_loader.load_config", lambda: dict(BASE_CONFIG))
    monkeypatch.setattr("app.proxy.auth.load_api_keys", lambda: {})
    verify.prepare(ctx)
    assert ctx.service_reachable is False
    assert "Connection refused" in ctx.unreachable_reason
    assert ctx.service_start == 1_700_000_000.0


def test_prepare_survives_a_broken_systemctl(monkeypatch):
    def _systemctl(*args, **kwargs):
        raise verify.ProbeError("systemctl show exited 1: no such unit")

    ctx = make_ctx(systemctl=_systemctl, http=FakeHTTP({("GET", "/healthz"): response(200, {"ok": True})}))
    monkeypatch.setattr("app.config_loader.load_config", lambda: dict(BASE_CONFIG))
    monkeypatch.setattr("app.proxy.auth.load_api_keys", lambda: {})
    verify.prepare(ctx)
    assert ctx.service == {}
    assert ctx.service_reachable is True
    assert any("systemd" in warning for warning in ctx.warnings)


def test_key_selection_prefers_scoped_keys_and_keeps_legacy_separate(monkeypatch):
    from app.proxy import scopes

    monkeypatch.setattr(scopes, "grace_window_open", lambda now=None: True)
    keys = {
        "token-admin": {"name": "admin", "metadata": {"scopes": ["admin"]}},
        "token-client": {"name": "client", "metadata": {"scopes": ["client"]}},
        "token-legacy": {"name": "legacy", "metadata": {}},
    }
    warnings: list[str] = []
    picked = verify._pick_keys(keys, warnings)

    assert picked["admin"] is not None and picked["admin"].name == "admin"
    assert picked["client"] is not None and picked["client"].name in {"admin", "client"}
    assert picked["legacy"] is not None and picked["legacy"].name == "legacy"
    assert picked["legacy"].is_legacy is True
    assert warnings == []


def test_json_document_carries_every_check(monkeypatch, capsys):
    """--json must expose the same check list the human output prints."""
    fake = make_ctx()
    monkeypatch.setattr(verify, "build_context", lambda args: fake)
    monkeypatch.setattr(verify, "prepare", lambda ctx: None)
    code = verify.main(["--json"])
    document = json.loads(capsys.readouterr().out)
    assert [entry["check"] for entry in document["checks"]] == [
        check_id for check_id, _ in verify.CHECKS
    ]
    assert document["exit_code"] == code
    assert document["ok"] is (code == verify.EXIT_PASS)


def test_human_output_prints_one_line_per_check(monkeypatch, capsys):
    fake = make_ctx()
    monkeypatch.setattr(verify, "build_context", lambda args: fake)
    monkeypatch.setattr(verify, "prepare", lambda ctx: None)
    verify.main([])
    out = capsys.readouterr().out
    for tag in ("[PASS]", "[FAIL]", "[SKIP]"):
        assert tag in out  # the fake mix exercises all three
    assert "passed" in out and "skipped" in out
