"""Tests for the gateway's caretaker control-API client (F5 wiring, tranche 1).

Covers the CaretakerClient HTTP contract against the caretaker daemon
(``m0nklabs/caretaker-llamacpp``):
- ensure 200 / 404 / 503(crash) / 503(vram) / 422 + transport failure
- unload 200 (incl. idempotent second call) / 500
- status 200 / non-200
- no Authorization header when api_key is None
- the factory resolves management_url + CARETAKER_KEY correctly
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.gateway.caretaker_client import (
    CaretakerClient,
    CaretakerInvalidRequest,
    CaretakerModelLoadFailed,
    CaretakerModelNotFound,
    CaretakerUnavailable,
    CaretakerUnloadFailed,
    CaretakerVramExceeded,
    build_caretaker_client,
)


def _make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> CaretakerClient:
    transport = httpx.MockTransport(handler)
    return CaretakerClient(
        management_url="http://caretaker.test:11441",
        api_key="secret",
        _transport=transport,
        **kwargs,
    )


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


def _err(status: int, error: str, message: str = "", crash_details=None) -> httpx.Response:
    payload: dict = {"error": error, "message": message}
    if crash_details is not None:
        payload["crash_details"] = crash_details
    return httpx.Response(status, json=payload)


async def test_ensure_success() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = req.headers
        captured["body"] = req.content
        return _ok({"ok": True, "loaded_model": "minimal", "needs_reload": False})

    client = _make_client(handler)
    result = await client.ensure("minimal", enable_vision=False, context_hint=4096)
    assert result["loaded_model"] == "minimal"
    assert captured["url"].endswith("/ensure")
    assert captured["headers"]["authorization"] == "Bearer secret"
    import json

    assert json.loads(captured["body"]) == {
        "model": "minimal",
        "enable_vision": False,
        "context_hint": 4096,
    }


async def test_ensure_model_not_found() -> None:
    client = _make_client(lambda req: _err(404, "model_not_found", "unknown"))
    with pytest.raises(CaretakerModelNotFound):
        await client.ensure("unknown")


async def test_ensure_load_failed_with_crash() -> None:
    client = _make_client(
        lambda req: _err(
            503,
            "model_load_failed",
            "failed to load",
            crash_details={"error_message": "boom"},
        )
    )
    with pytest.raises(CaretakerModelLoadFailed) as excinfo:
        await client.ensure("broken")
    assert excinfo.value.crash_details == {"error_message": "boom"}


async def test_ensure_vram_exceeded() -> None:
    client = _make_client(lambda req: _err(503, "vram_limit_exceeded", "too big"))
    with pytest.raises(CaretakerVramExceeded):
        await client.ensure("big")


async def test_ensure_invalid_request() -> None:
    client = _make_client(lambda req: _err(422, "invalid_request"))
    with pytest.raises(CaretakerInvalidRequest):
        await client.ensure("bad")


async def test_ensure_transport_error_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conn refused")

    client = _make_client(handler)
    with pytest.raises(CaretakerUnavailable):
        await client.ensure("minimal")


async def test_ensure_unexpected_status_unavailable() -> None:
    client = _make_client(lambda req: httpx.Response(418, json={}))
    with pytest.raises(CaretakerUnavailable):
        await client.ensure("minimal")


async def test_unload_non_json_200_is_unavailable() -> None:
    """Review fix 1: a 200 whose body is NOT a JSON dict (empty body or an
    HTML error page from an intermediary) must raise CaretakerUnavailable —
    never a raw ValueError leaking as HTTP 500."""
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    client = _make_client(handler)
    with pytest.raises(CaretakerUnavailable):
        await client.unload()


async def test_status_empty_body_200_is_unavailable() -> None:
    """Review fix 1: same defensive parsing on GET /status."""
    import httpx

    client = _make_client(lambda req: httpx.Response(200, content=b""))
    with pytest.raises(CaretakerUnavailable):
        await client.status()


async def test_unload_success_and_idempotent() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return _ok({"ok": True, "is_unloaded": True})

    client = _make_client(handler)
    r1 = await client.unload()
    r2 = await client.unload()  # idempotent second call is still 200
    assert r1["is_unloaded"] is True
    assert r2["is_unloaded"] is True
    assert len(calls) == 2


async def test_unload_failure_500() -> None:
    client = _make_client(lambda req: _err(500, "unload_failed", "boom"))
    with pytest.raises(CaretakerUnloadFailed):
        await client.unload()


async def test_unload_401_is_availability_not_refusal() -> None:
    """Review fix (possible bug): a 401 (missing/mismatched CARETAKER_KEY —
    the daemon rejected and did NOT process the unload) must classify as
    availability so callers fall back to the idempotent local unload, instead
    of a definitive refusal that silently disables VRAM freeing."""
    import httpx

    client = _make_client(lambda req: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(CaretakerUnavailable):
        await client.unload()


async def test_unload_403_is_availability_not_refusal() -> None:
    """Same as 401: 403 forbidden also means the unload was not processed."""
    import httpx

    client = _make_client(lambda req: httpx.Response(403, json={"error": "forbidden"}))
    with pytest.raises(CaretakerUnavailable):
        await client.unload()


async def test_status_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "loaded_model": "minimal",
                "vision_enabled": False,
                "is_unloaded": False,
                "needs_reload": False,
                "loaded_at": 1234.5,
                "idle_since": 1234.5,
            }
        )

    client = _make_client(handler)
    status = await client.status()
    assert status["loaded_model"] == "minimal"
    assert status["is_unloaded"] is False


async def test_status_non_200_unavailable() -> None:
    client = _make_client(lambda req: httpx.Response(503, json={}))
    with pytest.raises(CaretakerUnavailable):
        await client.status()


async def test_no_auth_header_when_key_none() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = req.headers
        return _ok({"ok": True})

    transport = httpx.MockTransport(handler)
    client = CaretakerClient(
        management_url="http://caretaker.test:11441",
        api_key=None,
        _transport=transport,
    )
    await client.unload()
    assert "authorization" not in captured["headers"]


async def test_close_aclose() -> None:
    client = _make_client(lambda req: _ok({"ok": True}))
    await client.unload()  # works before close
    await client.close()
    # client is closed: further use raises (httpx's closed-client guard)
    with pytest.raises(RuntimeError):
        await client.unload()


def test_build_from_local_provider_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """management_url comes from the local provider doc; key from env."""
    monkeypatch.setenv("CARETAKER_KEY", "env-secret")
    config = {
        "providers": {
            "ai-kvm2-local": {
                "local": True,
                "management_url": "http://127.0.0.1:11441",
            }
        }
    }
    client = build_caretaker_client(config)
    assert client.management_url == "http://127.0.0.1:11441"
    assert client._api_key == "env-secret"
    import asyncio

    asyncio.run(client.close())


def test_build_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    """${VAR} expansion in management_url mirrors the repo pattern."""
    monkeypatch.setenv("CARETAKER_MGMT", "http://127.0.0.1:11441")
    monkeypatch.delenv("CARETAKER_KEY", raising=False)
    config = {
        "providers": {
            "ai-kvm2-local": {
                "local": True,
                "management_url": "${CARETAKER_MGMT}",
            }
        }
    }
    client = build_caretaker_client(config)
    assert client.management_url == "http://127.0.0.1:11441"
    assert client._api_key is None  # no key configured
    import asyncio

    asyncio.run(client.close())


def test_build_missing_management_url_raises() -> None:
    config = {"providers": {"openrouter": {"local": False, "base_url": "x"}}}
    with pytest.raises(ValueError):
        build_caretaker_client(config)


def test_error_classes_have_default_messages() -> None:
    """Review fix (error reporting gap): CaretakerInvalidRequest and
    CaretakerUnloadFailed carry a default message so str(e) is never empty
    (the /admin/unload detail and watcher log would otherwise be blank)."""
    assert str(CaretakerInvalidRequest()) == "Caretaker rejected the request (422 invalid_request)"
    assert str(CaretakerUnloadFailed()) == "Caretaker failed to unload (500 unload_failed)"


def test_build_prefers_loopback_local_over_other_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix 2: with multi-host locals present, the factory must pick the
    loopback (this-host) caretaker for lifecycle execution, not the first via
    dict ordering."""
    monkeypatch.delenv("CARETAKER_KEY", raising=False)
    config = {
        "providers": {
            # Intentionally ordered so the non-loopback one comes first.
            "14700k-local": {"local": True, "management_url": "http://192.168.1.99:11441"},
            "ai-kvm2-local": {"local": True, "management_url": "http://127.0.0.1:11441"},
        }
    }
    client = build_caretaker_client(config)
    assert client.management_url == "http://127.0.0.1:11441"
    import asyncio

    asyncio.run(client.close())


def test_build_prefers_own_host_lan_ip_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix (possible issue): F5 reaches this host's caretaker via its
    LAN IP (192.168.1.35), which is NOT loopback.  With a second local provider
    present, the factory must still pick THIS host's caretaker via hostname/IP
    resolution — not dict order."""
    monkeypatch.delenv("CARETAKER_KEY", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "ai-kvm2")
    monkeypatch.setattr(
        "socket.gethostbyname_ex",
        lambda *a: ("ai-kvm2", ["ai-kvm2"], ["192.168.1.35", "127.0.0.1"]),
    )
    config = {
        "providers": {
            # Intentionally ordered so the non-this-host one comes first.
            "14700k-local": {"local": True, "management_url": "http://192.168.1.99:11441"},
            "ai-kvm2-local": {"local": True, "management_url": "http://192.168.1.35:11441"},
        }
    }
    client = build_caretaker_client(config)
    assert client.management_url == "http://192.168.1.35:11441"
    import asyncio

    asyncio.run(client.close())


def test_build_fails_closed_sending_key_to_foreign_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (sensitive key): CARETAKER_KEY must never go over cleartext
    HTTP to a FOREIGN host's management_url — build fails closed with ValueError."""
    monkeypatch.setenv("CARETAKER_KEY", "supersecret")
    monkeypatch.setattr("socket.gethostname", lambda: "ai-kvm2")
    monkeypatch.setattr(
        "socket.gethostbyname_ex",
        lambda *a: ("ai-kvm2", ["ai-kvm2"], ["127.0.0.1"]),
    )
    config = {
        "providers": {
            "14700k-local": {"local": True, "management_url": "http://192.168.1.99:11441"},
        }
    }
    with pytest.raises(ValueError, match="cleartext http"):
        build_caretaker_client(config)


def test_build_allows_key_to_own_host_lan_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (sensitive key): the own host's caretaker via LAN-IP is
    THIS host (not foreign) — CARETAKER_KEY is allowed and used."""
    monkeypatch.setenv("CARETAKER_KEY", "supersecret")
    monkeypatch.setattr("socket.gethostname", lambda: "ai-kvm2")
    monkeypatch.setattr(
        "socket.gethostbyname_ex",
        lambda *a: ("ai-kvm2", ["ai-kvm2"], ["192.168.1.35", "127.0.0.1"]),
    )
    config = {
        "providers": {
            "ai-kvm2-local": {"local": True, "management_url": "http://192.168.1.35:11441"},
        }
    }
    client = build_caretaker_client(config)
    assert client.management_url == "http://192.168.1.35:11441"
    assert client._api_key == "supersecret"
    import asyncio

    asyncio.run(client.close())


def test_build_ignores_caretaker_key_in_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix (sensitive key over cleartext): the Bearer secret must never
    be read from a tracked provider YAML — CARETAKER_KEY is env-only."""
    monkeypatch.delenv("CARETAKER_KEY", raising=False)
    config = {
        "providers": {
            "ai-kvm2-local": {
                "local": True,
                "management_url": "http://127.0.0.1:11441",
                "caretaker_key": "would-leak-over-lan",
            }
        }
    }
    client = build_caretaker_client(config)
    # The YAML caretaker_key is deliberately IGNORED; without CARETAKER_KEY the
    # client sends no Authorization header at all.
    assert client._api_key is None
    import asyncio

    asyncio.run(client.close())


def test_build_fails_closed_on_multiple_locals_no_this_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (possible issue): with TWO local providers and none binding
    THIS host, dict order must never decide which GPU host's caretaker receives
    lifecycle commands — fail closed with ValueError."""
    monkeypatch.delenv("CARETAKER_KEY", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "ai-kvm2")
    monkeypatch.setattr(
        "socket.gethostbyname_ex",
        lambda *a: ("ai-kvm2", ["ai-kvm2"], ["127.0.0.1"]),  # no LAN IP
    )
    config = {
        "providers": {
            "14700k-local": {"local": True, "management_url": "http://192.168.1.99:11441"},
            "ai-kvm2-local": {"local": True, "management_url": "http://192.168.1.35:11441"},
        }
    }
    with pytest.raises(ValueError, match="Multiple local providers"):
        build_caretaker_client(config)


def test_build_falls_back_to_first_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a loopback entry the first local provider still wins."""
    monkeypatch.delenv("CARETAKER_KEY", raising=False)
    config = {
        "providers": {
            "myhost-local": {"local": True, "management_url": "http://10.0.0.5:11441"},
        }
    }
    client = build_caretaker_client(config)
    assert client.management_url == "http://10.0.0.5:11441"
    import asyncio

    asyncio.run(client.close())