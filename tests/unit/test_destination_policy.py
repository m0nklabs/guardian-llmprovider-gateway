"""G1-02: outbound destination allowlist, SSRF guard and credential isolation.

Source of record: ``docs/decisions/G1-02-destination-and-credentials.md``.

What is pinned here, in the order the issue lists it:

* the **negative** matrix — an unapproved destination, a redirect to an
  unapproved host, a DNS answer outside the allowlist, ``169.254.169.254``, a
  non-http(s) scheme, a URL with userinfo, a provider URL on an unapproved
  loopback port, and a malformed/absent allowlist (fail closed);
* the **positive** matrix — every destination the shipped configuration
  actually uses is still approved, and the caretaker's cleartext-key refusal
  still refuses;
* **credential isolation** — a provider's key cannot reach another provider's
  destination, no credential is forwarded on a redirect hop, and no secret
  reaches the log at DEBUG/INFO.

No test opens a socket: the guard sits in front of an ``httpx.MockTransport``,
and DNS is a patched ``destination_policy.resolve_addresses``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from app import destination_policy as dp
from app import log_safety
from app.config_loader import provider_settings_documents
from app.gateway.caretaker_client import build_caretaker_client
from app.paths import GLOBAL_SETTINGS_FILE

REPO_ROOT = Path(__file__).resolve().parents[2]

# Publicly-classified addresses used as DNS answers. Nothing is ever
# connected: the guard sits in front of an httpx.MockTransport.
PUBLIC_OPENROUTER = "8.8.8.8"
PUBLIC_GROQ = "1.1.1.1"


# ── Fixtures ────────────────────────────────────────────────────────────


def _policy_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "destination_policy": {
            "schemes": ["http", "https"],
            "allow": [
                {"host": "127.0.0.1", "ports": [4000, 11440, 11441, 11450]},
                {"host": "localhost", "ports": [11440], "addresses": ["127.0.0.1", "::1"]},
                {
                    "host": "192.168.1.245",
                    "ports": [11440, 11441, 11450, 11451],
                    "cleartext_credentials": True,
                },
                {"host": "openrouter.ai", "ports": [443]},
                {"host": "api.groq.com", "ports": [443]},
                {
                    "host": "internal-gpu.lan",
                    "ports": [11440],
                    "addresses": ["192.168.1.245"],
                },
            ],
        },
        "providers": {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "${OPENROUTER_API_KEY}",
            },
            "groq": {
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "${GROQ_API_KEY}",
            },
            "14700k-local": {
                "base_url": "http://192.168.1.245:11440/v1",
                "management_url": "http://192.168.1.245:11441",
                "tts_url": "http://192.168.1.245:11450",
                "stt_url": "http://192.168.1.245:11451",
                "api_key": "${WINDOWS_LAN_KEY}",
            },
        },
    }
    config.update(overrides)
    return config


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch):
    """Patch the resolver: public names answer test addresses, nothing else."""

    answers = {
        "openrouter.ai": [PUBLIC_OPENROUTER],
        "api.groq.com": [PUBLIC_GROQ],
        "internal-gpu.lan": ["192.168.1.245"],
    }
    calls: list[tuple[str, int]] = []

    async def fake_resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        if host in answers:
            return list(answers[host])
        raise OSError("name does not resolve")

    monkeypatch.setattr(dp, "resolve_addresses", fake_resolve)
    return answers, calls


@pytest.fixture
def policy(dns) -> dp.DestinationPolicy:
    return dp.DestinationPolicy.from_config(_policy_config())


class Recorder:
    """An inner transport that records requests and answers with one status."""

    def __init__(self, status_code: int = 200, headers: dict[str, str] | None = None):
        self.requests: list[httpx.Request] = []
        self.status_code = status_code
        self.headers = headers or {}
        self.fail_first = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_first:
            self.fail_first -= 1
            raise httpx.ConnectError("connect refused", request=request)
        return httpx.Response(self.status_code, headers=self.headers, content=b"{}")

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self.handler)


def _guard(policy: dp.DestinationPolicy, recorder: Recorder, **kwargs: Any):
    return dp.DestinationGuardTransport(
        policy, purpose="test", inner=recorder.transport(), **kwargs
    )


AUTH = {"Authorization": "Bearer sk-live-secret-value-123456"}


# ── Negative: destinations that must be refused ─────────────────────────


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        # Not in the allowlist at all.
        ("https://api.example.com/v1/models", dp.REASON_HOST_NOT_ALLOWED),
        # Cloud metadata, in every shape an SSRF reaches for.
        ("http://169.254.169.254/latest/meta-data/", dp.REASON_METADATA_HOST_BLOCKED),
        ("http://169.254.170.2/v2/credentials", dp.REASON_METADATA_HOST_BLOCKED),
        ("http://[fd00:ec2::254]/latest/meta-data/", dp.REASON_METADATA_HOST_BLOCKED),
        # Non-http(s) schemes.
        ("file:///etc/passwd", dp.REASON_SCHEME_NOT_ALLOWED),
        ("ftp://openrouter.ai/v1", dp.REASON_SCHEME_NOT_ALLOWED),
        ("gopher://127.0.0.1:11440/_x", dp.REASON_SCHEME_NOT_ALLOWED),
        # Userinfo in the URL (a classic allowlist-confusion trick).
        ("https://user:pass@openrouter.ai/api/v1", dp.REASON_USERINFO_NOT_ALLOWED),
        ("https://openrouter.ai@evil.example/api", dp.REASON_USERINFO_NOT_ALLOWED),
        # Loopback the operator did not approve.
        ("http://127.0.0.2:11440/v1", dp.REASON_LOOPBACK_NOT_APPROVED),
        ("http://[::1]:11440/v1", dp.REASON_LOOPBACK_NOT_APPROVED),
        # A port that is not approved for an approved host.
        ("http://127.0.0.1:9999/v1", dp.REASON_PORT_NOT_ALLOWED),
        ("https://openrouter.ai:8443/api/v1", dp.REASON_PORT_NOT_ALLOWED),
        # Link-local and RFC1918 targets are not implicitly trusted.
        ("http://169.254.1.1:11440/v1", dp.REASON_LINK_LOCAL_NOT_APPROVED),
        ("http://10.0.0.5:11440/v1", dp.REASON_PRIVATE_NOT_APPROVED),
        ("http://172.16.4.4:11441/ensure", dp.REASON_PRIVATE_NOT_APPROVED),
    ],
)
def test_destination_matrix_denies_with_stable_reason(policy, url, reason):
    with pytest.raises(dp.DestinationDenied) as excinfo:
        policy.check_structure(url, purpose="test")
    assert excinfo.value.code == "destination_denied"
    assert excinfo.value.reason == reason
    assert excinfo.value.to_detail()["error"] == "destination_denied"


def test_denied_destination_never_reaches_the_inner_transport(policy, dns):
    recorder = Recorder()
    transport = _guard(policy, recorder)

    async def deny():
        await transport.handle_async_request(
            httpx.Request("GET", "https://api.example.com/v1/models")
        )

    import asyncio

    with pytest.raises(dp.DestinationDenied):
        asyncio.run(deny())
    assert recorder.requests == [], "no socket may be opened for a denied destination"


def test_metadata_endpoint_is_never_approvable_even_when_allowlisted(dns):
    """"Approved" must not become a way to reach IMDS: the denylist wins."""
    config = _policy_config()
    config["destination_policy"]["allow"].append(
        {"host": "169.254.169.254", "ports": [80]}
    )
    policy = dp.DestinationPolicy.from_config(config)
    with pytest.raises(dp.DestinationDenied) as excinfo:
        policy.check_structure("http://169.254.169.254/latest/meta-data/")
    assert excinfo.value.reason == dp.REASON_METADATA_HOST_BLOCKED


def test_provider_url_on_unapproved_loopback_port_rejected_at_config_load(policy):
    """A provider pointing at an unapproved loopback port is a config-load error."""
    doc = {"enabled": True, "base_url": "http://127.0.0.1:9999/v1"}
    errors = dp.provider_destination_errors("rogue-local", doc, policy)
    assert [field for field, _ in errors] == ["base_url"]
    assert errors[0][1].reason == dp.REASON_PORT_NOT_ALLOWED

    approved = {"enabled": True, "base_url": "http://127.0.0.1:11440/v1"}
    assert dp.provider_destination_errors("ai-kvm2-local", approved, policy) == []


def test_provider_placeholder_host_rejected(policy):
    """The #48 class: a provider on a placeholder/undocumented destination."""
    doc = {
        "enabled": True,
        "base_url": "http://192.168.1.W:11440/v1",
        "management_url": "http://192.168.1.W:11441",
    }
    errors = dp.provider_destination_errors("windows-gpu-local", doc, policy)
    assert [field for field, _ in errors] == ["base_url", "management_url"]
    assert all(error.reason == dp.REASON_HOST_NOT_ALLOWED for _, error in errors)


# ── Negative: DNS answers that leave the allowlist ──────────────────────


def test_dns_answer_outside_pinned_allowlist_is_refused(policy, dns):
    """A pinned name that resolves elsewhere is a rebinding attempt, not a host."""
    answers, _ = dns
    answers["internal-gpu.lan"] = ["192.168.7.7"]
    import asyncio

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(policy.check("http://internal-gpu.lan:11440/v1"))
    assert excinfo.value.reason == dp.REASON_DNS_ADDRESS_NOT_ALLOWED
    assert "outside the pinned set" in excinfo.value.detail


def test_public_provider_name_resolving_into_the_lan_is_refused(policy, dns):
    answers, _ = dns
    answers["openrouter.ai"] = ["192.168.1.245"]
    import asyncio

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(policy.check("https://openrouter.ai/api/v1"))
    assert excinfo.value.reason == dp.REASON_DNS_ADDRESS_NOT_ALLOWED


def test_public_provider_name_resolving_to_metadata_is_refused(policy, dns):
    answers, _ = dns
    answers["openrouter.ai"] = ["169.254.169.254"]
    import asyncio

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(policy.check("https://openrouter.ai/api/v1"))
    assert excinfo.value.reason == dp.REASON_METADATA_HOST_BLOCKED


def test_resolution_failure_fails_closed(policy, dns):
    import asyncio

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(policy.check("https://unlisted.example/api"))
    assert excinfo.value.reason in {
        dp.REASON_HOST_NOT_ALLOWED,
        dp.REASON_DNS_RESOLUTION_FAILED,
    }
    # A listed-but-unresolvable entry fails closed rather than connecting by name.
    answers, _ = dns
    answers.pop("openrouter.ai")
    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(policy.check("https://openrouter.ai/api/v1"))
    assert excinfo.value.reason == dp.REASON_DNS_RESOLUTION_FAILED


# ── Negative: redirects ────────────────────────────────────────────────


def test_redirect_to_unapproved_host_is_refused(policy, dns):
    """A hop is a new destination; the policy refuses to follow it."""
    import asyncio

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(
            policy.check(
                "https://api.example.com/v1/models",
                redirect_from="https://openrouter.ai/api/v1/models",
            )
        )
    assert excinfo.value.reason == dp.REASON_HOST_NOT_ALLOWED

    # Even an approved host is a different destination than the origin origin.
    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(
            policy.check(
                "https://api.groq.com/openai/v1",
                redirect_from="https://openrouter.ai/api/v1",
            )
        )
    assert excinfo.value.reason == dp.REASON_REDIRECT_NOT_ALLOWED


def test_redirect_target_never_contacts_the_unapproved_host(policy, dns):
    """The redirecting host answers 302; the guarded client must not follow it."""
    import asyncio

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    async def run() -> httpx.Response:
        client = dp.guarded_async_client(policy=policy, purpose="test")
        client._transport = dp.DestinationGuardTransport(  # type: ignore[attr-defined]
            policy, purpose="test", inner=httpx.MockTransport(handler)
        )
        try:
            return await client.get("https://openrouter.ai/api/v1/models")
        finally:
            await client.aclose()

    response = asyncio.run(run())
    assert response.status_code == 302
    assert seen == [f"https://{PUBLIC_OPENROUTER}/api/v1/models"]


def test_client_refuses_to_follow_redirects(policy):
    with pytest.raises(ValueError, match="redirects are not followed"):
        dp.guarded_async_client(policy=policy, follow_redirects=True, purpose="test")


# ── Negative: malformed / absent allowlist (fail closed) ───────────────


@pytest.mark.parametrize(
    "section",
    [
        {},  # empty mapping
        {"schemes": ["http", "https"]},  # no allow list
        {"allow": []},  # empty allow list
        {"allow": "openrouter.ai"},  # wrong type
        {"allow": [{"host": "", "ports": [443]}]},  # no host
        {"allow": [{"host": "openrouter.ai"}]},  # no ports
        {"allow": [{"host": "openrouter.ai", "ports": ["443"]}]},  # wrong port type
        {"allow": [{"host": "openrouter.ai", "ports": [70000]}]},  # out of range
        {"allow": [{"host": "openrouter.ai", "ports": [443], "addresses": ["nope"]}]},
        {"schemes": ["http", "gopher"], "allow": [{"host": "a", "ports": [1]}]},
    ],
)
def test_malformed_allowlist_fails_closed(section):
    """A *present but broken* section denies everything — a typo never approves."""
    policy = dp.DestinationPolicy.from_config({"destination_policy": section})
    assert policy.closed is True
    with pytest.raises(dp.DestinationDenied) as excinfo:
        policy.check_structure("https://openrouter.ai/api/v1")
    assert excinfo.value.reason == dp.REASON_POLICY_UNAVAILABLE
    assert "destination_policy" in excinfo.value.detail or "allow" in excinfo.value.detail


def test_absent_section_uses_the_shipped_allowlist(caplog):
    """No section at all is not a config error: ship the same policy, loudly.

    This is the reboot-safety property: an operator whose config predates G1-02
    (or who kept their own copy of ``global.settings.yaml``) must not turn the
    gateway into a closed relay on the next restart, while destinations outside
    the shipped list stay denied.
    """
    with caplog.at_level("WARNING"):
        policy = dp.DestinationPolicy.from_config({})
    assert policy.closed is False
    assert policy.source == "shipped-default"
    assert any("shipped allowlist" in record.getMessage() for record in caplog.records)
    # Shipped, therefore approved …
    assert policy.check_structure("https://openrouter.ai/api/v1").host == "openrouter.ai"
    assert policy.check_structure("http://127.0.0.1:11440/v1/models").port == 11440
    # … and anything not shipped stays denied.
    with pytest.raises(dp.DestinationDenied) as excinfo:
        policy.check_structure("https://example.com/api")
    assert excinfo.value.reason == dp.REASON_HOST_NOT_ALLOWED


def test_one_malformed_entry_does_not_disable_the_rest(caplog):
    """Dropped entry = that host unreachable; the policy keeps enforcing."""
    config = _policy_config()
    config["destination_policy"]["allow"].insert(
        1, {"host": "broken.example", "ports": "not-a-list"}
    )
    with caplog.at_level(logging.ERROR, logger="Guardian.Destinations"):
        policy = dp.DestinationPolicy.from_config(config)
    assert policy.closed is False
    assert policy.invalid_entries, "the dropped entry must be reported"
    assert "🛑" in caplog.text
    # The valid entries still work…
    policy.check_structure("http://127.0.0.1:11440/v1")
    # …and the malformed host is not reachable through a guess.
    with pytest.raises(dp.DestinationDenied):
        policy.check_structure("https://broken.example/x")


def test_transport_with_closed_policy_refuses_everything():
    import asyncio

    recorder = Recorder()
    transport = dp.DestinationGuardTransport(
        dp.DestinationPolicy.fail_closed("absent"), purpose="test", inner=recorder.transport()
    )
    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(
            transport.handle_async_request(httpx.Request("GET", "http://127.0.0.1:11440/x"))
        )
    assert excinfo.value.reason == dp.REASON_POLICY_UNAVAILABLE
    assert recorder.requests == []


# ── Positive: the shipped destinations still work ───────────────────────


def _live_config() -> dict[str, Any]:
    """The shipped configuration as the process loads it (no env expansion)."""
    with open(GLOBAL_SETTINGS_FILE, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    config["providers"] = provider_settings_documents()
    return config


def test_shipped_policy_is_valid_and_covers_every_configured_destination():
    """The shipped allowlist must approve every destination the config uses."""
    config = _live_config()
    policy = dp.DestinationPolicy.from_config(config)
    assert policy.closed is False, policy.failure
    assert policy.invalid_entries == ()

    rejected: list[str] = []
    for name, doc in (config.get("providers") or {}).items():
        if not isinstance(doc, dict) or not doc.get("enabled", True):
            continue
        for field, error in dp.provider_destination_errors(name, doc, policy):
            rejected.append(f"{name}.{field}: {error.reason} ({error.destination})")
    assert rejected == [], (
        "the shipped destination allowlist must cover the live configuration: "
        + "; ".join(rejected)
    )


def test_shipped_policy_covers_the_known_lan_and_loopback_hops():
    policy = dp.DestinationPolicy.from_config(_live_config())
    for url in (
        "http://127.0.0.1:11440/v1",
        "http://127.0.0.1:11441/ensure",
        "http://127.0.0.1:11450/tts",
        "http://localhost:11440/props",
        "http://192.168.1.245:11440/v1",
        "http://192.168.1.245:11441/ensure",
        "http://192.168.1.245:11450/tts",
        "http://192.168.1.245:11451/transcriptions",
        "https://openrouter.ai/api/v1/models",
        "https://api.groq.com/openai/v1/models",
        "https://integrate.api.nvidia.com/v1/models",
        "https://api.openai.com/v1/models",
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "https://inference.poolside.ai/v1/models",
        "https://api.fish.audio/model",
    ):
        policy.check_structure(url, purpose="inventory")


def test_shipped_policy_flags_the_unmigrated_cleartext_hops():
    """B-03/B-04 must stay visible, not buried in prose."""
    policy = dp.DestinationPolicy.from_config(_live_config())
    assert set(policy.cleartext_credential_hops()) == {
        "http://192.168.1.245:11440",
        "http://192.168.1.245:11441",
        "http://192.168.1.245:11450",
        "http://192.168.1.245:11451",
    }


def test_approved_lan_destination_is_reachable_through_the_guard(policy, dns):
    """The approved LAN target keeps working unchanged (no behaviour change)."""
    import asyncio

    recorder = Recorder()

    async def run():
        transport = _guard(policy, recorder, credential_owner="14700k-local")
        request = httpx.Request(
            "POST",
            "http://192.168.1.245:11440/v1/chat/completions",
            headers={"Authorization": "Bearer windows-lan-key"},
            json={"model": "qwen/qwen3.5-9b"},
        )
        return await transport.handle_async_request(request)

    response = asyncio.run(run())
    assert response.status_code == 200
    assert len(recorder.requests) == 1
    sent = recorder.requests[0]
    # IP-literal destinations are not re-resolved, and the Host header is kept.
    assert urlsplit(str(sent.url)).hostname == "192.168.1.245"
    assert sent.headers["host"] == "192.168.1.245:11440"


def test_caretaker_cleartext_refusal_still_refuses(monkeypatch: pytest.MonkeyPatch):
    """G1-02 must not weaken the caretaker's own cleartext-key refusal."""
    monkeypatch.setenv("CARETAKER_KEY", "supersecret")
    monkeypatch.setattr("socket.gethostname", lambda: "ai-node")
    monkeypatch.setattr(
        "socket.gethostbyname_ex", lambda *a: ("ai-node", ["ai-node"], ["127.0.0.1"])
    )
    config = {
        "providers": {
            "windows-gpu-local": {"local": True, "management_url": "http://192.168.1.K:11441"}
        }
    }
    with pytest.raises(ValueError, match="cleartext http"):
        build_caretaker_client(config)


# ── SSRF that survives resolution: pinning and fallback ─────────────────


def test_connect_is_pinned_to_the_validated_address(policy, dns):
    """The connection goes to the validated IP; Host and SNI keep the name."""
    import asyncio

    recorder = Recorder()

    async def run():
        transport = _guard(policy, recorder, credential_owner="openrouter")
        request = httpx.Request("GET", "https://openrouter.ai/api/v1/models", headers=AUTH)
        return await transport.handle_async_request(request)

    asyncio.run(run())
    sent = recorder.requests[0]
    assert urlsplit(str(sent.url)).hostname == PUBLIC_OPENROUTER
    assert sent.headers["host"] == "openrouter.ai"
    assert sent.extensions["sni_hostname"] == "openrouter.ai"


def test_connect_falls_back_across_validated_addresses(policy, dns):
    import asyncio

    answers, _ = dns
    answers["openrouter.ai"] = [PUBLIC_OPENROUTER, PUBLIC_GROQ]
    recorder = Recorder()
    recorder.fail_first = 1

    async def run():
        transport = _guard(policy, recorder, credential_owner="openrouter")
        request = httpx.Request("GET", "https://openrouter.ai/api/v1/models", headers=AUTH)
        return await transport.handle_async_request(request)

    assert asyncio.run(run()).status_code == 200
    assert len(recorder.requests) == 2
    assert [urlsplit(str(r.url)).hostname for r in recorder.requests] == [
        PUBLIC_OPENROUTER,
        PUBLIC_GROQ,
    ]


# ── Credential isolation ───────────────────────────────────────────────


def test_provider_key_cannot_reach_another_provider(policy, dns):
    """Provider A's secret must never be sent to provider B's destination."""
    import asyncio

    recorder = Recorder()

    async def run():
        transport = _guard(policy, recorder, credential_owner="openrouter")
        request = httpx.Request("GET", "https://api.groq.com/openai/v1/models", headers=AUTH)
        return await transport.handle_async_request(request)

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(run())
    assert excinfo.value.reason == dp.REASON_CREDENTIAL_FOREIGN_DESTINATION
    assert excinfo.value.owner == "openrouter"
    assert recorder.requests == []


def test_credential_without_declared_owner_is_refused(policy, dns):
    """A future call site that forgets the owner fails closed, not open."""
    import asyncio

    recorder = Recorder()

    async def run():
        transport = _guard(policy, recorder)  # no credential_owner
        request = httpx.Request("GET", "https://openrouter.ai/api/v1/models", headers=AUTH)
        return await transport.handle_async_request(request)

    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(run())
    assert excinfo.value.reason == dp.REASON_CREDENTIAL_WITHOUT_OWNER
    assert recorder.requests == []


def test_own_key_on_own_destination_is_allowed(policy, dns):
    import asyncio

    recorder = Recorder()

    async def run():
        transport = _guard(policy, recorder, credential_owner="groq")
        request = httpx.Request("GET", "https://api.groq.com/openai/v1/models", headers=AUTH)
        return await transport.handle_async_request(request)

    assert asyncio.run(run()).status_code == 200
    assert recorder.requests[0].headers["authorization"] == AUTH["Authorization"]


def test_unknown_owner_is_refused(policy, dns):
    import asyncio

    recorder = Recorder()
    transport = _guard(policy, recorder, credential_owner="not-a-provider")
    with pytest.raises(dp.DestinationDenied) as excinfo:
        asyncio.run(
            transport.handle_async_request(
                httpx.Request("GET", "https://openrouter.ai/api/v1/models", headers=AUTH)
            )
        )
    assert excinfo.value.reason == dp.REASON_CREDENTIAL_FOREIGN_DESTINATION
    assert recorder.requests == []


def test_no_credential_is_forwarded_on_a_redirect_hop(policy, dns):
    """Only the origin request is sent; the 302 body is returned un-followed."""
    import asyncio

    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            307, headers={"location": "https://api.groq.com/openai/v1/models"}
        )

    async def run():
        transport = dp.DestinationGuardTransport(
            policy,
            purpose="test",
            credential_owner="openrouter",
            inner=httpx.MockTransport(handler),
        )
        request = httpx.Request("GET", "https://openrouter.ai/api/v1/models", headers=AUTH)
        return await transport.handle_async_request(request)

    response = asyncio.run(run())
    assert response.status_code == 307
    assert [url for url, _ in seen] == [f"https://{PUBLIC_OPENROUTER}/api/v1/models"]


def test_no_secret_appears_in_captured_logs(policy, dns, caplog):
    """Neither a denied nor an accepted request may log the credential."""
    import asyncio

    secret = "sk-live-secret-value-123456"
    recorder = Recorder()

    async def run():
        transport = _guard(policy, recorder, credential_owner="openrouter")
        ok = httpx.Request(
            "GET", "https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {secret}"}
        )
        await transport.handle_async_request(ok)
        try:
            await transport.handle_async_request(
                httpx.Request(
                    "GET",
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {secret}"},
                )
            )
        except dp.DestinationDenied:
            pass
        logging.getLogger("Guardian.Destinations").debug(
            "request headers were %s", ok.headers
        )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert secret not in caplog.text
    assert caplog.records, "the capture must actually contain records"


def test_log_redaction_filter_scrubs_registered_and_bearer_secrets():
    log_safety.register_secret("windows-lan-key-value")
    record = logging.LogRecord(
        "Guardian.Test",
        logging.INFO,
        __file__,
        1,
        "key=%s header=Bearer abcdef123456789",
        ("windows-lan-key-value",),
        None,
    )
    assert log_safety.SecretRedactionFilter().filter(record) is True
    assert "windows-lan-key-value" not in record.getMessage()
    assert "abcdef123456789" not in record.getMessage()
    assert record.getMessage().count(log_safety.REDACTED) == 2


def test_live_config_secrets_are_registered_for_redaction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-registered-secret")
    config = {"providers": {"groq": {"api_key": "${GROQ_API_KEY}"}}}
    log_safety.register_configured_secrets(config)
    assert "gsk-test-registered-secret" not in log_safety.redact_secrets(
        "header Bearer gsk-test-registered-secret"
    )


# ── Configuration load: the provider registry rejects unapproved URLs ────


def test_provider_registry_flags_an_unapproved_destination(tmp_path, monkeypatch):
    """A provider URL outside the allowlist is refused at configuration load."""
    from app.proxy.providers import ProviderRegistry

    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "rogue-local.settings.yaml").write_text(
        "local: false\nenabled: true\nbase_url: http://192.168.1.99:11440/v1\n"
        "api_key: rogue-key\ncatalog_url: /models\n"
    )
    (providers_dir / "ai-kvm2-local.settings.yaml").write_text(
        "local: true\nenabled: true\nbase_url: http://127.0.0.1:11440/v1\n"
        "management_url: http://127.0.0.1:11441\n"
    )
    monkeypatch.setattr("app.paths.PROVIDERS_DIR", providers_dir)
    registry = ProviderRegistry()  # production directory scan

    rogue = registry._providers["rogue-local"]
    assert rogue.destination_error == dp.REASON_PRIVATE_NOT_APPROVED
    assert rogue.destination_error_field == "base_url"

    local = registry._providers["ai-kvm2-local"]
    assert local.destination_error is None
    assert local.destination_error_field is None


def test_guarded_client_disables_environment_proxies(policy):
    """A proxy env var must not become a way around the destination guard."""
    client = dp.guarded_async_client(policy=policy, purpose="test")
    assert client.trust_env is False


def test_long_lived_client_sees_a_reload_that_revokes_its_destination(dns):
    """A reload must revoke an approval for an already-constructed client.

    The caretaker client (and the copilot bridge) build one httpx client for the
    process lifetime; resolving the policy once at construction would keep a
    removed destination usable until the next restart (PR-Piet, G1-02).
    """
    old = dp.DestinationPolicy.from_config(
        {"destination_policy": {"allow": [{"host": "caretaker.internal", "ports": [11441]}]}}
    )
    new = dp.DestinationPolicy.from_config(
        {"destination_policy": {"allow": [{"host": "elsewhere.internal", "ports": [11441]}]}}
    )
    live = {"policy": old}
    client = dp.guarded_async_client(purpose="t", policy=lambda: live["policy"])

    transport = client._transport
    assert isinstance(transport, dp.DestinationGuardTransport)
    assert transport.policy is old

    live["policy"] = new  # simulates POST /api/config/reload
    assert transport.policy is new
    with pytest.raises(dp.DestinationDenied) as excinfo:
        transport.policy.check_structure("http://caretaker.internal:11441/ensure")
    assert excinfo.value.reason == dp.REASON_HOST_NOT_ALLOWED


def test_userinfo_password_never_reaches_the_log(caplog):
    """A rejected URL with userinfo must be logged sanitized, not verbatim."""
    policy = dp.DestinationPolicy.from_config(
        {"destination_policy": {"allow": [{"host": "openrouter.ai", "ports": [443]}]}}
    )
    with pytest.raises(dp.DestinationDenied) as excinfo:
        policy.check_structure("https://user:s3cr3t-pw@evil.example/api")
    assert "s3cr3t-pw" not in caplog.text
    assert "s3cr3t-pw" not in str(excinfo.value)
    assert "s3cr3t-pw" not in repr(excinfo.value.to_detail())
