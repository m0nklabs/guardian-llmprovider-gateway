#!/usr/bin/env python3
"""Assert the real Copilot runtime contract without pytest or external I/O.

Run with the existing LiteLLM interpreter and ``-B``. All application requests
use ASGITransport, all upstream requests use MockTransport, and socket/DNS
operations fail closed. Credentials, prompts and responses are synthetic.
"""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Set before importing LiteLLM: importing must not fetch its remote price map.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
sys.dont_write_bytecode = True


def _deny_network(*args, **kwargs):
    raise AssertionError("Runtime checker forbids real socket/DNS operations")


# Install before dependency imports; do not monkeypatch production code or
# replace LiteLLM's HTTP handlers, which could hide missing client propagation.
socket.socket.connect = _deny_network
socket.socket.connect_ex = _deny_network
socket.socket.sendto = _deny_network
socket.create_connection = _deny_network
socket.getaddrinfo = _deny_network
socket.gethostbyname = _deny_network
socket.gethostbyname_ex = _deny_network

import httpx  # noqa: E402
from importlib.metadata import version  # noqa: E402

from app.copilot_bridge import Settings, create_app, normalize_model  # noqa: E402


HEADERS = {"Authorization": "Bearer synthetic-client-key"}
MESSAGES = [{"role": "user", "content": "Synthetic stable prefix"}]
TOOL = {"type": "function", "function": {
    "name": "probe", "description": "Tool café\nExact documentation",
    "parameters": {"type": "object", "properties": {
        "x": {"type": "string", "description": "Nested documentation"}},
        "required": ["x"], "additionalProperties": False}}}
CALL = {"type": "function_call", "id": "fc_synthetic", "call_id": "call_synthetic",
        "name": "probe", "arguments": '{"x":"value"}', "status": "completed"}
USAGE = {"input_tokens": 100, "output_tokens": 4, "total_tokens": 104,
         "input_tokens_details": {"cached_tokens": 90},
         "output_tokens_details": {"reasoning_tokens": 1}}
RESPONSE = {"id": "resp_synthetic", "object": "response", "created_at": 1700000000,
            "status": "completed", "model": "gpt-6.1-sol", "output": [CALL],
            "usage": USAGE, "parallel_tool_calls": True}


class EventBytes(httpx.AsyncByteStream):
    def __init__(self, events, *, stall=False):
        self.events, self.stall, self.closed = events, stall, False

    async def __aiter__(self):
        for event in self.events:
            yield ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def response_events():
    return [
        {"type": "response.created", "sequence_number": 0,
         "response": {**RESPONSE, "status": "in_progress", "output": [], "usage": None}},
        {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
         "item": {**CALL, "arguments": "", "status": "in_progress"}},
        {"type": "response.function_call_arguments.delta", "sequence_number": 2,
         "item_id": CALL["id"], "output_index": 0, "delta": '{"x":"'},
        {"type": "response.function_call_arguments.delta", "sequence_number": 3,
         "item_id": CALL["id"], "output_index": 0, "delta": 'value"}'},
        {"type": "response.function_call_arguments.done", "sequence_number": 4,
         "item_id": CALL["id"], "output_index": 0, "arguments": CALL["arguments"]},
        {"type": "response.output_item.done", "sequence_number": 5,
         "output_index": 0, "item": CALL},
        {"type": "response.completed", "sequence_number": 6, "response": RESPONSE},
    ]


@contextlib.asynccontextmanager
async def runtime(protocol, handler):
    endpoint = {"chat": "/chat/completions", "responses": "/responses",
                "messages": "/v1/messages", "embeddings": "/embeddings"}[protocol]
    raw_id = {"messages": "claude-sonnet-4-5", "embeddings": "text-embedding-3-small"}.get(protocol, "gpt-6.1-sol")
    vendor = "Anthropic" if protocol == "messages" else "OpenAI"

    def strict_handler(request):
        # Assert destination and credentials even if production SDK silently
        # retains its own default URL/key. Generic mocks would miss that bug.
        expected_path = "/v1/messages" if protocol == "messages" else "/v1" + endpoint
        assert str(request.url) == "http://synthetic.invalid" + expected_path, str(request.url)
        credential = request.headers.get("x-api-key") if protocol == "messages" else request.headers.get("authorization")
        expected = "synthetic-upstream-key" if protocol == "messages" else "Bearer synthetic-upstream-key"
        assert credential == expected, credential
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(strict_handler)) as upstream:
        settings = Settings("http://synthetic.invalid/v1", "synthetic-upstream-key",
                            "synthetic-client-key", heartbeat_seconds=0.01,
                            stream_idle_seconds=0.05, stream_cleanup_seconds=0.1)
        # Do not inject completion/embedding mocks: exercise real operations.
        app = create_app(settings, client=upstream)
        entry = {"id": raw_id, "vendor": vendor, "supported_endpoints": [endpoint]}
        if protocol == "embeddings":
            entry["capabilities"] = {"type": "embeddings"}
        model = normalize_model(entry)
        assert model is not None
        app.state.catalog.models = {model["id"]: model}
        app.state.catalog.last_success = time.time()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://synthetic-bridge") as client:
                yield client, model["id"]
        finally:
            # No lifespan is started: discovery is isolated from inference here.
            # Detach the bridge-owned hook before its borrowed mock pool closes.
            for hook in tuple(upstream.event_hooks.get("response", [])):
                owner = getattr(hook, "__self__", None)
                if hasattr(owner, "detach"):
                    owner.detach()


async def error_checks():
    outcomes = []
    for protocol in ("chat", "responses", "messages"):
        for mode in ("429", "remote_protocol", "connect", "429_overflow", "503_overflow"):
            calls = []

            def handle(request):
                calls.append(str(request.url))
                if mode == "remote_protocol":
                    raise httpx.RemoteProtocolError("Synthetic disconnect after send")
                if mode == "connect":
                    raise httpx.ConnectError("Synthetic connection failure")
                retry = "Fri, 31 Dec 9999 23:59:59 -0100" if mode.endswith("overflow") else "17"
                status = 503 if mode == "503_overflow" else 429
                return httpx.Response(status, headers={"Retry-After": retry},
                    json={"error": {"message": "Synthetic limit", "type": "rate_limit_error"}})

            async with runtime(protocol, handle) as (client, model):
                result = await client.post("/v1/chat/completions", headers=HEADERS,
                    json={"model": model, "messages": MESSAGES})
            assert len(calls) == 1, (protocol, mode, len(calls))
            if mode == "429":
                assert result.status_code == 429, (protocol, result.text)
                assert result.headers.get("Retry-After") == "17", protocol
                assert result.json()["error"]["code"] == "rate_limit_exceeded"
            elif mode.endswith("overflow"):
                assert result.status_code == (503 if mode == "503_overflow" else 429), (protocol, result.text)
                assert "Retry-After" not in result.headers, protocol
            else:
                assert 500 <= result.status_code <= 599, (protocol, result.status_code)
            assert "synthetic-upstream-key" not in result.text
            outcomes.append({"protocol": protocol, "failure": mode, "attempts": 1,
                             "status": result.status_code})
    return outcomes


def assert_usage(usage):
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 104
    assert usage["prompt_tokens_details"]["cached_tokens"] == 90
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 1


async def successful_responses_checks():
    outcomes = []
    for streaming in (False, True):
        calls, byte_streams = [], []

        def handle(request):
            body = json.loads(request.content)
            calls.append(body)
            if body.get("stream"):
                stream = EventBytes(response_events())
                byte_streams.append(stream)
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
            return httpx.Response(200, json=RESPONSE)

        async with runtime("responses", handle) as (client, model):
            payload = {"model": model, "messages": deepcopy(MESSAGES), "tools": [deepcopy(TOOL)],
                       "stream": streaming, "stream_options": {"include_usage": True},
                       "prompt_cache_key": "stable-synthetic-key", "prompt_cache_retention": "in-memory"}
            original = deepcopy(payload)
            result = await client.post("/v1/chat/completions", headers=HEADERS, json=payload)
            assert result.status_code == 200, result.text
            assert len(calls) == 1
            assert payload == original
            request = calls[0]
            assert request["prompt_cache_key"] == payload["prompt_cache_key"]
            assert request["prompt_cache_retention"] == payload["prompt_cache_retention"]
            assert request["tools"][0]["description"] == TOOL["function"]["description"]
            assert request["tools"][0]["parameters"] == TOOL["function"]["parameters"]
            if streaming:
                frames = [frame.removeprefix("data: ") for frame in result.text.split("\n\n")
                          if frame.startswith("data: ")]
                assert frames[-1] == "[DONE]", result.text
                chunks = [json.loads(frame) for frame in frames[:-1]]
                assert not any("error" in chunk for chunk in chunks), result.text
                usages = [chunk["usage"] for chunk in chunks if chunk.get("usage")]
                assert len(usages) == 1, result.text
                assert_usage(usages[0])
                tool_deltas = [tool for chunk in chunks for choice in chunk.get("choices", [])
                               for tool in choice.get("delta", {}).get("tool_calls", [])]
                returned_id = next(tool["id"] for tool in tool_deltas if tool.get("id"))
                name = next(tool["function"]["name"] for tool in tool_deltas if tool.get("function", {}).get("name"))
                arguments = "".join(tool.get("function", {}).get("arguments", "") for tool in tool_deltas)
                assert name == CALL["name"] and arguments == CALL["arguments"]
                assert any(choice.get("finish_reason") == "tool_calls" for chunk in chunks
                           for choice in chunk.get("choices", []))
                assert byte_streams[0].closed
                assistant = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": returned_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}
            else:
                output = result.json()
                assert_usage(output["usage"])
                assert output["choices"][0]["finish_reason"] == "tool_calls"
                assistant = output["choices"][0]["message"]
                returned_id = assistant["tool_calls"][0]["id"]
                assert assistant["tool_calls"][0]["function"]["arguments"] == CALL["arguments"]
            followup = {"model": model, "messages": [*MESSAGES, assistant,
                        {"role": "tool", "tool_call_id": returned_id, "content": "Synthetic tool result"}],
                        "tools": [TOOL]}
            next_result = await client.post("/v1/chat/completions", headers=HEADERS, json=followup)
            assert next_result.status_code == 200, next_result.text
            assert len(calls) == 2  # Two caller requests, not an implicit replay.
            items = calls[1]["input"]
            invoke = next(item for item in items if item["type"] == "function_call")
            answer = next(item for item in items if item["type"] == "function_call_output")
            # Stateless full-history replay may use the translator's public ID.
            # Both sides must use exactly the emitted ID; do not invent a mapping.
            assert invoke["call_id"] == answer["call_id"] == returned_id
            assert invoke["arguments"] == CALL["arguments"]
        outcomes.append({"stream": streaming, "cache_controls": True, "cached_usage": True,
                         "tool_schema": True, "tool_roundtrip_ids": True})
    return outcomes


async def messages_idle_check():
    calls, responses, streams = [], [], []

    def handle(request):
        calls.append(str(request.url))
        stream = EventBytes([{"type": "message_start", "message": {
            "id": "msg_synthetic", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 0}}}], stall=True)
        response = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        streams.append(stream)
        responses.append(response)
        return response

    async with runtime("messages", handle) as (client, model):
        result = await client.post("/v1/chat/completions", headers=HEADERS,
            json={"model": model, "messages": MESSAGES, "stream": True})
    assert len(calls) == 1
    assert "upstream_timeout" in result.text and "[DONE]" not in result.text
    assert responses[0].is_closed and streams[0].closed
    return {"attempts": 1, "idle_timeout": True, "http_response_closed": True}


async def remote_media_check():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        raise AssertionError("Rejected remote media must make zero HTTP requests")

    async with runtime("messages", handle) as (client, model):
        result = await client.post("/v1/chat/completions", headers=HEADERS,
            json={"model": model, "messages": [{"role": "user", "content": [{
                "type": "image_url", "image_url": {"url": "http://93.184.216.34/synthetic.png"}}]}]})
    assert result.status_code == 400 and not calls, (result.status_code, calls)
    return {"status": 400, "http_attempts": 0}


async def messages_success_checks():
    outcomes = []
    usage = {"input_tokens": 10, "output_tokens": 4,
             "cache_creation_input_tokens": 5, "cache_read_input_tokens": 90}
    tool_use = {"type": "tool_use", "id": "toolu_synthetic", "name": "probe", "input": {"x": "value"}}
    message = {"id": "msg_success", "type": "message", "role": "assistant",
               "model": "claude-sonnet-4-5", "content": [tool_use],
               "stop_reason": "tool_use", "stop_sequence": None, "usage": usage}
    for streaming in (False, True):
        calls, responses, streams = [], [], []

        def handle(request):
            body = json.loads(request.content)
            calls.append(body)
            if body.get("stream"):
                stream = EventBytes([
                    {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                          "usage": {**usage, "output_tokens": 0}}},
                    {"type": "content_block_start", "index": 0, "content_block": {**tool_use, "input": {}}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "input_json_delta", "partial_json": '{"x":"'}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "input_json_delta", "partial_json": 'value"}'}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                     "usage": {"output_tokens": 4}},
                    {"type": "message_stop"}])
                streams.append(stream)
                response = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
            else:
                response = httpx.Response(200, json=message)
            responses.append(response)
            return response

        async with runtime("messages", handle) as (client, model):
            result = await client.post("/v1/chat/completions", headers=HEADERS,
                json={"model": model, "messages": MESSAGES, "tools": [TOOL], "stream": streaming,
                      "stream_options": {"include_usage": True}})
            assert result.status_code == 200 and len(calls) == 1, result.text
            assert calls[0]["model"] == "claude-sonnet-4-5"
            assert calls[0]["tools"][0]["description"] == TOOL["function"]["description"]
            assert calls[0]["tools"][0]["input_schema"] == TOOL["function"]["parameters"]
            assert responses[0].is_closed
            if streaming:
                frames = [f.removeprefix("data: ") for f in result.text.split("\n\n") if f.startswith("data: ")]
                assert frames[-1] == "[DONE]", result.text
                chunks = [json.loads(f) for f in frames[:-1]]
                assert not any("error" in chunk for chunk in chunks), result.text
                usages = [chunk["usage"] for chunk in chunks if chunk.get("usage")]
                assert len(usages) == 1, result.text
                deltas = [tool for chunk in chunks for choice in chunk.get("choices", [])
                          for tool in choice.get("delta", {}).get("tool_calls", [])]
                returned_id = next(tool["id"] for tool in deltas if tool.get("id"))
                name = next(tool["function"]["name"] for tool in deltas if tool.get("function", {}).get("name"))
                arguments = "".join(tool.get("function", {}).get("arguments", "") for tool in deltas)
                assert any(choice.get("finish_reason") == "tool_calls" for chunk in chunks
                           for choice in chunk.get("choices", []))
                assert streams[0].closed
            else:
                output = result.json()
                assert output["model"] == "claude-sonnet-4-5"
                assert output["choices"][0]["finish_reason"] == "tool_calls"
                returned = output["choices"][0]["message"]["tool_calls"][0]
                returned_id, name, arguments = returned["id"], returned["function"]["name"], returned["function"]["arguments"]
                usages = [output["usage"]]
            assert returned_id == tool_use["id"] and name == tool_use["name"]
            assert json.loads(arguments) == tool_use["input"]  # Vendor JSON formatting is not guaranteed byte-identical.
            counted = usages[0]
            assert counted["prompt_tokens"] == 105 and counted["completion_tokens"] == 4 and counted["total_tokens"] == 109
            assert counted["prompt_tokens_details"]["cached_tokens"] == 90
            assert counted["cache_creation_input_tokens"] == 5 and counted["cache_read_input_tokens"] == 90
        outcomes.append({"stream": streaming, "attempts": 1, "tool_semantics": True,
                         "cached_usage": True, "cache_creation_usage": True, "http_closed": True})
    return outcomes


async def embeddings_checks():
    import base64
    import struct

    outcomes = []
    vector = [0.25, -0.5, 1.0]
    encoded = base64.b64encode(struct.pack("<3f", *vector)).decode()
    for mode in ("float", "base64", "default", "429", "remote_protocol", "connect"):
        calls, responses = [], []

        def handle(request):
            body = json.loads(request.content)
            calls.append(body)
            if mode == "remote_protocol":
                raise httpx.RemoteProtocolError("Synthetic embedding disconnect after send")
            if mode == "connect":
                raise httpx.ConnectError("Synthetic embedding connection failure")
            if mode == "429":
                response = httpx.Response(429, headers={"Retry-After": "17"},
                    json={"error": {"message": "Synthetic limit", "type": "rate_limit_error"}})
            else:
                response = httpx.Response(200, json={"object": "list", "model": "text-embedding-3-small",
                    "data": [{"object": "embedding", "index": 0,
                              "embedding": encoded if body.get("encoding_format") == "base64" else vector}],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3}})
            responses.append(response)
            return response

        async with runtime("embeddings", handle) as (client, model):
            payload = {"model": model, "input": "Synthetic embedding", "dimensions": 3}
            if mode != "default":
                payload["encoding_format"] = mode if mode in ("float", "base64") else "float"
            result = await client.post("/v1/embeddings", headers=HEADERS, json=payload)
            assert len(calls) == 1, ("embeddings", mode, len(calls))
            assert calls[0]["model"] == "text-embedding-3-small" and calls[0]["dimensions"] == 3
            assert all(response.is_closed for response in responses)
            if mode in ("float", "base64", "default"):
                assert result.status_code == 200, result.text
                output = result.json()
                assert output["object"] == "list" and output["model"] == "text-embedding-3-small"
                assert output["data"][0]["index"] == 0
                assert output["data"][0]["embedding"] == (encoded if mode == "base64" else vector)
                assert output["usage"]["prompt_tokens"] == output["usage"]["total_tokens"] == 3
            elif mode == "429":
                assert result.status_code == 429 and result.headers.get("Retry-After") == "17", result.text
            else:
                assert 500 <= result.status_code <= 599, result.text
        outcomes.append({"mode": mode, "attempts": 1, "status": result.status_code, "http_closed": True})
    return outcomes


async def check():
    # Suppress dependency diagnostics, not errors/assertions or the final report.
    with contextlib.redirect_stdout(io.StringIO()):
        errors = await error_checks()
        responses = await successful_responses_checks()
        messages_success = await messages_success_checks()
        embeddings = await embeddings_checks()
        messages = await messages_idle_check()
        remote = await remote_media_check()
    return {"litellm_version": version("litellm"), "real_network_permitted": False,
            "exact_upstream_url_and_credentials": True,
            "errors": errors, "responses": responses, "messages_success": messages_success,
            "embeddings": embeddings, "messages_idle": messages, "remote_media": remote,
            "limits": ["Synthetic fixtures, not provider compatibility or billing evidence",
                       "No production activation, cache-hit-rate measurement or real model calls",
                       "Vendor-reported cost fields are not independently measured billing",
                       "Catalog startup/refresh and all cancellation races covered separately"]}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(check()), indent=2))
