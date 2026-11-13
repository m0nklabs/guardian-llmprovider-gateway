"""Snapshot validation, revocation and discovery freshness without real I/O."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.copilot_catalog import Catalog, normalize_model


def settings(max_age=37):
    return SimpleNamespace(upstream_url="http://copilot.test/v1", upstream_key="upstream-secret",
                           refresh_seconds=17, catalog_timeout_seconds=5,
                           catalog_max_age_seconds=max_age)


def entry(raw_id="model", vendor="OpenAI", endpoints=None, **extra):
    return {"id": raw_id, "vendor": vendor, "name": "Public model name",
            "supported_endpoints": endpoints if endpoints is not None else ["/chat/completions"],
            "model_picker_enabled": True, "capabilities": {"type": "chat", "limits": {
                "max_context_window_tokens": 200000, "max_prompt_tokens": 180000,
                "max_output_tokens": 32000}, "supports": {
                "vision": True, "tool_calls": True, "reasoning_effort": ["low", "high"]}}, **extra}


@pytest.mark.parametrize("vendor,brand", [("OpenAI", "openai"), ("Azure OpenAI", "openai"),
    ("Anthropic", "anthropic"), ("xAI", "x-ai"), ("Moonshot AI", "moonshotai"),
    ("Future Maker", "future-maker")])
def test_identity_and_metadata_preserved(vendor, brand):
    raw = entry(vendor=vendor)
    model = normalize_model(raw)
    assert model["id"] == f"{brand}/model"
    assert model["upstream_id"] == "model"
    assert model["vendor"] == vendor.lower()
    assert model["owned_by"] == brand
    assert model["name"] == raw["name"]
    assert model["capabilities"] == raw["capabilities"]
    assert model["context_length"] == 200000
    assert model["max_output_tokens"] == 32000
    assert model["reasoning"] == {"supported": True, "supported_efforts": ["low", "high"]}
    assert model["architecture"] == {"input_modalities": ["text", "image"], "output_modalities": ["text"]}


@pytest.mark.parametrize("endpoints,protocol", [
    (["/v1/messages", "/chat/completions", "/responses"], "responses"),
    (["/v1/messages", "/chat/completions"], "chat"),
    (["/v1/messages"], "messages"),
])
def test_protocol_preference(endpoints, protocol):
    assert normalize_model(entry(endpoints=endpoints))["protocol"] == protocol


def test_embedding_picker_exception_and_protocol():
    raw = entry(endpoints=["/responses", "/chat/completions"], model_picker_enabled=False)
    raw["capabilities"]["type"] = "embeddings"
    model = normalize_model(raw)
    assert model["protocol"] == "embeddings"
    assert model["architecture"]["output_modalities"] == ["embedding"]
    assert normalize_model({**raw, "policy": {"state": "disabled"}}) is None


def test_owned_by_fallback_and_optional_metadata_not_fabricated():
    model = normalize_model({"id": "model", "owned_by": "OpenAI", "supported_endpoints": ["/responses"]})
    assert model["id"] == "openai/model"
    assert model["name"] == "model"
    assert "capabilities" not in model
    assert "context_length" not in model
    assert "max_output_tokens" not in model
    assert "reasoning" not in model
    assert model["supported_endpoints"] == ["/responses"]
    raw = entry(capabilities={"type": "chat", "limits": {"max_prompt_tokens": 123}})
    assert normalize_model(raw)["context_length"] == 123


def test_projection_does_not_alias_input_metadata():
    raw = entry()
    model = normalize_model(raw)
    raw["capabilities"]["supports"]["reasoning_effort"].append("mutated")
    raw["supported_endpoints"].append("mutated")
    assert model["reasoning"]["supported_efforts"] == ["low", "high"]
    assert model["capabilities"]["supports"]["reasoning_effort"] == ["low", "high"]
    assert model["supported_endpoints"] == ["/chat/completions"]


@pytest.mark.parametrize("raw", [None, [], "sensitive-payload", 12, entry(raw_id=""),
    entry(raw_id="namespace/model"), entry(raw_id="bad\nmodel"), entry(raw_id="bad model"),
    entry(vendor=""), entry(vendor=42), entry(vendor="!"),
    entry(vendor=[], owned_by="OpenAI"), entry(vendor=False, owned_by="OpenAI"),
    entry(policy=[]), entry(policy=None), entry(policy={"state": {}}),
    entry(capabilities=[]), entry(capabilities=None),
    entry(capabilities={"limits": "sensitive-payload"}), entry(capabilities={"supports": []}),
    entry(capabilities={"type": {}}), entry(capabilities={"supports": {"vision": "true"}}),
    entry(capabilities={"supports": {"tool_calls": 1}}),
    entry(capabilities={"supports": {"reasoning_effort": "high"}}),
    entry(capabilities={"supports": {"reasoning_effort": ["high", {}]}}),
    entry(capabilities={"limits": {"max_prompt_tokens": True}}),
    entry(capabilities={"limits": {"max_context_window_tokens": "200000"}}),
    entry(capabilities={"limits": {"max_output_tokens": -1}}),
    entry(capabilities={"limits": {"max_output_tokens": None}}),
    entry(supported_endpoints="/responses"), entry(supported_endpoints=["/responses", {}]),
    entry(supported_endpoints=None), entry(model_picker_enabled="false"), entry(name={}),
])
def test_malformed_entries_are_skipped_without_attribute_errors(raw):
    assert normalize_model(raw) is None


@pytest.mark.parametrize("streaming", ["false", "true", 0, 1, None, [], {}])
def test_streaming_support_must_be_boolean(streaming):
    assert normalize_model(entry(capabilities={"supports": {"streaming": streaming}})) is None


@pytest.mark.parametrize("streaming", [False, True])
def test_boolean_streaming_support_preserved(streaming):
    model = normalize_model(entry(capabilities={"supports": {"streaming": streaming}}))
    assert model["capabilities"]["supports"]["streaming"] is streaming


@pytest.mark.asyncio
async def test_invalid_streaming_support_skips_row_and_records_safe_counter():
    rows = [entry("invalid", capabilities={"supports": {"streaming": "false"}}),
            entry("nonstreaming", capabilities={"supports": {"streaming": False}}), entry("valid")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": rows}))) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        await catalog.refresh()
        assert set(catalog.models) == {"openai/nonstreaming", "openai/valid"}
        assert catalog.models["openai/nonstreaming"]["capabilities"]["supports"]["streaming"] is False
        assert catalog.counts["invalid"] == 1
        assert catalog.counts["accepted"] == 2
        assert catalog.last_error is None


@pytest.mark.parametrize("raw", [entry(policy={"state": "disabled"}),
    entry(model_picker_enabled=False), entry(endpoints=[]), entry(endpoints=["ws:/responses"])])
def test_disabled_and_unsupported_entries_not_served(raw):
    assert normalize_model(raw) is None


@pytest.mark.parametrize("entries", [[], [entry(policy={"state": "disabled"})]])
@pytest.mark.asyncio
async def test_first_valid_snapshot_can_be_empty_or_all_disabled(entries):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": entries}))) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        assert not catalog.usable
        await catalog.refresh()
        assert catalog.models == {}
        assert catalog.last_success == 100
        assert catalog.last_error is None
        assert catalog.usable
        assert not catalog.is_stale
        assert catalog.status["model_count"] == 0


@pytest.mark.asyncio
async def test_invalid_rows_do_not_discard_valid_rows_or_keep_retired_models(caplog):
    state = {"data": [entry("retired"), entry("retained")]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=state))) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        await catalog.refresh()
        state["data"] = [entry("retained"), entry("added", "Anthropic"),
                         entry("retired", policy={"state": "disabled"}, capabilities="sensitive-payload"),
                         "sensitive-payload", entry("invalid", capabilities={"limits": "sensitive-payload"})]
        await catalog.refresh()
        assert set(catalog.models) == {"openai/retained", "anthropic/added"}
        assert catalog.last_error is None
        assert catalog.counts == {"total": 5, "accepted": 2, "invalid": 2,
                                  "disabled": 1, "unsupported": 0, "duplicate_identities": 0}
        assert "sensitive-payload" not in repr(catalog.status)
        assert "sensitive-payload" not in caplog.text
        assert "upstream-secret" not in repr(catalog.status)


def deeply_nested_entry():
    raw = entry("deep")
    nested = {"leaf": "sensitive-payload"}
    for _ in range(600):
        nested = {"nested": nested}
    raw["capabilities"]["unknown_metadata"] = nested
    return raw


def test_deep_metadata_projection_is_invalid_instead_of_raising_recursion_error():
    raw = json.loads(json.dumps(deeply_nested_entry()))
    assert normalize_model(raw) is None


@pytest.mark.asyncio
async def test_deep_metadata_row_does_not_block_revocation_and_valid_snapshot(caplog):
    state = {"data": [entry("revoked")]}
    now = [100.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps(state)))) as client:
        catalog = Catalog(settings(), client, clock=lambda: now[0])
        await catalog.refresh()
        assert set(catalog.models) == {"openai/revoked"}
        state["data"] = [entry("revoked", policy={"state": "disabled"}),
                         deeply_nested_entry(), entry("valid")]
        now[0] = 101
        await catalog.refresh()
        assert set(catalog.models) == {"openai/valid"}
        assert catalog.last_success == 101
        assert catalog.last_error is None
        assert catalog.counts == {"total": 3, "accepted": 1, "invalid": 1,
                                  "disabled": 1, "unsupported": 0, "duplicate_identities": 0}
        assert "sensitive-payload" not in caplog.text


@pytest.mark.parametrize("failure,error", [("http", "HTTPStatusError"), ("root", "ValueError")])
@pytest.mark.asyncio
async def test_failed_refresh_is_stale_but_usable_during_grace_then_success_clears_error(failure, error):
    state = {"failure": None}
    now = [100.0]
    def handle(request):
        if state["failure"] == "http":
            return httpx.Response(503)
        if state["failure"] == "root":
            return httpx.Response(200, json={"data": None})
        return httpx.Response(200, json={"data": [entry()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = Catalog(settings(max_age=37), client, clock=lambda: now[0])
        await catalog.refresh()
        assert not catalog.is_stale
        previous_models = catalog.models
        state["failure"] = failure
        now[0] = 101
        await catalog.refresh()
        assert catalog.last_error == error
        assert catalog.is_stale
        assert catalog.usable
        assert catalog.status["is_stale"] is True
        assert catalog.status["usable"] is True
        assert catalog.models is previous_models
        assert catalog.last_success == 100
        now[0] = 137
        assert catalog.is_stale
        assert not catalog.usable
        assert catalog.status["is_stale"] is True
        assert catalog.status["usable"] is False
        state["failure"] = None
        await catalog.refresh()
        assert catalog.last_error is None
        assert not catalog.is_stale
        assert catalog.usable
        assert catalog.status["is_stale"] is False
        assert catalog.status["usable"] is True
        assert catalog.last_success == 137


@pytest.mark.parametrize("pair", [
    [entry(), entry()],
    [entry(), entry(vendor="Azure OpenAI")],
    [entry(), entry(policy={"state": "disabled"}, capabilities="invalid")],
    [entry(policy={"state": "disabled"}, capabilities="invalid"), entry()],
    [entry(), entry(capabilities={"supports": "invalid"})],
    [entry(), entry(model_picker_enabled=False)],
])
@pytest.mark.asyncio
async def test_every_duplicate_identity_is_conservatively_excluded(pair):
    state = {"data": [entry()]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=state))) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        await catalog.refresh()
        state["data"] = [*pair, entry("unrelated")]
        await catalog.refresh()
        assert set(catalog.models) == {"openai/unrelated"}
        assert catalog.last_error is None
        assert catalog.counts["duplicate_identities"] == 1
        assert catalog.counts["accepted"] == 1


@pytest.mark.parametrize("root", [None, [], 123, "sensitive-payload", {}, {"data": None},
    {"data": "sensitive-payload"}, {"data": {"id": "model"}}])
@pytest.mark.asyncio
async def test_malformed_root_preserves_previous_snapshot_with_value_error(root, caplog):
    state = {"root": {"data": [entry()]}}
    now = [100.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps(state["root"])))) as client:
        catalog = Catalog(settings(), client, clock=lambda: now[0])
        await catalog.refresh()
        previous_models, previous_counts = catalog.models, deepcopy(catalog.counts)
        now[0] = 130
        state["root"] = root
        await catalog.refresh()
        assert catalog.models is previous_models
        assert catalog.counts == previous_counts
        assert catalog.last_success == 100
        assert catalog.last_error == "ValueError"
        assert catalog.usable
        assert "sensitive-payload" not in caplog.text
        assert "upstream-secret" not in caplog.text


@pytest.mark.parametrize("failure,error", [("timeout", "ReadTimeout"), ("http", "HTTPStatusError"),
                                           ("json", "JSONDecodeError")])
@pytest.mark.asyncio
async def test_transport_and_json_errors_preserve_snapshot(failure, error, caplog):
    state = {"failure": None}
    def handle(request):
        assert str(request.url) == "http://copilot.test/v1/models"
        assert request.headers["authorization"] == "Bearer upstream-secret"
        if state["failure"] == "timeout":
            raise httpx.ReadTimeout("sensitive-payload upstream-secret", request=request)
        if state["failure"] == "http":
            return httpx.Response(503, text="sensitive-payload upstream-secret")
        if state["failure"] == "json":
            return httpx.Response(200, text="sensitive-payload upstream-secret")
        return httpx.Response(200, json={"data": [entry()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        await catalog.refresh()
        previous_models = catalog.models
        state["failure"] = failure
        await catalog.refresh()
        assert catalog.models is previous_models
        assert catalog.last_success == 100
        assert catalog.last_error == error
        assert "sensitive-payload" not in caplog.text
        assert "upstream-secret" not in caplog.text
        state["failure"] = None
        await catalog.refresh()
        assert catalog.last_error is None


@pytest.mark.asyncio
async def test_freshness_exact_deadline_errors_do_not_extend_age_and_reads_do_not_discover():
    now = [0.0]
    state = {"data": [entry()]}
    calls = 0
    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = Catalog(settings(max_age=37), client, clock=lambda: now[0])
        assert catalog.age_seconds is None
        assert catalog.is_stale
        assert catalog.status["usable"] is False
        await catalog.refresh()
        assert catalog.usable  # Timestamp zero is a successful snapshot.
        now[0] = 36.999
        assert not catalog.is_stale
        state["data"] = None
        await catalog.refresh()
        assert catalog.last_error == "ValueError"
        now[0] = 37
        assert catalog.age_seconds == 37
        assert catalog.is_stale
        assert not catalog.usable
        for _ in range(10):
            assert catalog.models["openai/model"]["protocol"] == "chat"
            assert catalog.status["is_stale"]
            assert not catalog.usable
        assert calls == 2  # Cached checks/lookups cannot cause discovery.
        state["data"] = []
        await catalog.refresh()
        assert catalog.usable
        assert catalog.last_success == 37
        assert catalog.last_error is None
        assert catalog.models == {}


def test_status_is_detached_from_counters_and_clock_rollback_clamps_age():
    now = [99.0]
    catalog = Catalog(settings(), AsyncMock(), clock=lambda: now[0])
    catalog.last_success = 100
    assert catalog.age_seconds == 0
    status = catalog.status
    status["counts"]["accepted"] = 900
    assert catalog.counts["accepted"] == 0


@pytest.mark.parametrize("max_age", [0, -1, True, float("inf"), float("nan")])
def test_invalid_freshness_configuration_rejected(max_age):
    with pytest.raises(ValueError, match="positive and finite"):
        Catalog(settings(max_age), AsyncMock())


@pytest.mark.asyncio
async def test_failed_first_snapshot_stays_unavailable():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": None}))) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        await catalog.refresh()
        assert catalog.last_error == "ValueError"
        assert catalog.last_success is None
        assert catalog.models == {}
        assert catalog.is_stale
        assert not catalog.usable


@pytest.mark.asyncio
async def test_refreshes_serialize_and_keep_old_snapshot_until_request_finishes():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    active, maximum_active = 0, 0
    async def handle(request):
        nonlocal calls, active, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        started.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={"data": [entry(str(calls))]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        catalog.models = {"old": {"id": "old"}}
        first = asyncio.create_task(catalog.refresh())
        second = asyncio.create_task(catalog.refresh())
        try:
            await started.wait()
            assert catalog.models == {"old": {"id": "old"}}
            assert calls == 1
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert maximum_active == 1
        assert calls == 2
        assert set(catalog.models) == {"openai/2"}


@pytest.mark.asyncio
async def test_cancelled_refresh_does_not_replace_snapshot_or_swallow_cancellation():
    async def handle(request):
        raise asyncio.CancelledError
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = Catalog(settings(), client, clock=lambda: 100)
        catalog.models = {"old": {"id": "old"}}
        with pytest.raises(asyncio.CancelledError):
            await catalog.refresh()
        assert catalog.models == {"old": {"id": "old"}}
        assert catalog.last_success is None
        assert catalog.last_error is None
        assert not catalog.lock.locked()


@pytest.mark.asyncio
async def test_loop_refreshes_on_schedule_only(monkeypatch):
    catalog = Catalog(settings(), AsyncMock())
    catalog.refresh = AsyncMock()
    waits = []
    async def sleep(delay):
        waits.append(delay)
        if len(waits) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr("app.copilot_catalog.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await catalog.loop()
    assert waits == [17, 17, 17]
    assert catalog.refresh.await_count == 2
