"""Dynamic, authenticated Copilot catalog and OpenAI protocol adapter.

Runs separately from Guardian using the existing LiteLLM environment. Model
names and protocol selection come from copilot-api, never a static alias list.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.destination_policy import current_policy, guarded_async_client
from app.copilot_catalog import Catalog, normalize_model as normalize_model  # Public projection helper.
from app.copilot_errors import error_headers, error_payload, error_status
from app.copilot_stream import CopilotStreamingResponse, await_inference, ClientDisconnected
from app.copilot_requests import validate_chat, validate_embedding

__all__ = ["Catalog", "Settings", "create_app", "normalize_model"]
# Client payloads must never override LiteLLM's credentials, endpoint or provider.
CHAT_FIELDS = frozenset({
    "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "stop", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "reasoning_effort", "seed", "presence_penalty",
    "frequency_penalty", "logprobs", "top_logprobs", "user", "n",
    "prompt_cache_key", "prompt_cache_retention",
})
# These fields must never become transport overrides, even on rejected input.
TRANSPORT_FIELDS = frozenset({"api_base", "api_key", "custom_llm_provider", "base_url", "extra_headers", "headers", "client", "num_retries", "max_retries", "fallbacks"})
EMBEDDING_FIELDS = frozenset({"input", "dimensions", "encoding_format", "user"})


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    upstream_key: str
    client_key: str
    refresh_seconds: float = 300
    timeout_seconds: float = 1200
    catalog_timeout_seconds: float = 30
    max_body_bytes: int = 16 * 1024 * 1024
    heartbeat_seconds: float = 15
    stream_idle_seconds: float = 1200
    stream_cleanup_seconds: float = 5
    catalog_max_age_seconds: float = 3600

    def __post_init__(self):
        if not isinstance(self.upstream_url, str) or not self.upstream_url.startswith(("http://", "https://")):
            raise ValueError("upstream_url must be HTTP(S)")
        for value in (self.refresh_seconds, self.timeout_seconds, self.catalog_timeout_seconds,
                      self.heartbeat_seconds, self.stream_idle_seconds, self.stream_cleanup_seconds,
                      self.catalog_max_age_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                raise ValueError("Bridge limits and intervals must be positive and finite")
        if isinstance(self.max_body_bytes, bool) or not isinstance(self.max_body_bytes, int) or self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer")

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ValueError(f"Bridge configuration is invalid YAML ({type(exc).__name__})") from None
        if not isinstance(raw, dict):
            raise ValueError("Bridge configuration must be a mapping")
        def key(name: str) -> str:
            value = os.environ.get(raw[name], "")
            if not value:
                raise ValueError(f"Missing environment variable named by {name}")
            return value
        result = cls(
            upstream_url=raw["upstream_url"].rstrip("/"),
            upstream_key=key("upstream_key_env"), client_key=key("client_key_env"),
            refresh_seconds=raw.get("refresh_seconds", 300),
            timeout_seconds=raw.get("timeout_seconds", 1200),
            catalog_timeout_seconds=raw.get("catalog_timeout_seconds", 30),
            max_body_bytes=raw.get("max_body_bytes", 16 * 1024 * 1024),
            heartbeat_seconds=raw.get("heartbeat_seconds", 15),
            stream_idle_seconds=raw.get("stream_idle_seconds", raw.get("timeout_seconds", 1200)),
            stream_cleanup_seconds=raw.get("stream_cleanup_seconds", 5),
            catalog_max_age_seconds=raw.get("catalog_max_age_seconds", 3600),
        )
        return result


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None,
               completion=None, embedding=None) -> FastAPI:
    settings = settings or Settings.load(Path(os.environ.get(
        "GUARDIAN_COPILOT_BRIDGE_CONFIG",
        str(Path(__file__).resolve().parents[1] / "config/github-copilot-bridge.settings.yaml"),
    )))
    upstream = client or guarded_async_client(
        purpose="copilot-upstream",
        credential_owner="copilot-bridge",
        policy=current_policy().with_owner("copilot-bridge", [settings.upstream_url]),
    )
    operations = None
    if completion is None or embedding is None:
        from app.copilot_transport import LitellmOperations
        operations = LitellmOperations(upstream, upstream_url=settings.upstream_url,
                                        upstream_key=settings.upstream_key,
                                        cleanup_seconds=settings.stream_cleanup_seconds)
        completion = completion or operations.completion
        embedding = embedding or operations.embedding
    catalog = Catalog(settings, upstream)

    @asynccontextmanager
    async def lifespan(app):
        await catalog.refresh()
        task = asyncio.create_task(catalog.loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if operations is not None:
                operations.detach()
            if client is None:
                await upstream.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.catalog = catalog

    def authorize(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", ""),
                                   "Bearer " + settings.client_key):
            raise HTTPException(401, "Invalid bridge credential")

    @app.get("/v1/models")
    async def models(request: Request):
        authorize(request)
        if catalog.last_success is None:
            raise HTTPException(503, "Copilot catalog not available yet")
        return {"object": "list", "data": list(catalog.models.values()),
                "last_success": catalog.last_success, "last_error": catalog.last_error,
                "catalog": catalog.status,
                "bridge": {"chat_fields": sorted(CHAT_FIELDS), "embedding_fields": sorted(EMBEDDING_FIELDS),
                           "automatic_retries": 0, "paid_fallback": False}}

    async def payload(request: Request, kind: str):
        authorize(request)
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > settings.max_body_bytes:
                raise HTTPException(413, "Bridge request too large")
        import json
        try:
            body = json.loads(chunks)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise HTTPException(400, "A model ID is required")
        allowed = CHAT_FIELDS if kind == "chat" else EMBEDDING_FIELDS
        if set(body) - allowed - TRANSPORT_FIELDS - {"model"}:
            raise HTTPException(400, {"error": {"type": "invalid_request_error", "code": "unsupported_parameter",
                                                "message": "A request parameter is not supported by this bridge; inspect /v1/models bridge fields."}})
        if not catalog.usable:
            raise HTTPException(503, "Copilot catalog is unavailable or stale")
        model = catalog.models.get(body["model"])
        if model is None:
            raise HTTPException(404, "model_not_served")
        if (model["protocol"] == "embeddings") != (kind == "embeddings"):
            raise HTTPException(400, "Model does not support this operation")
        return body, model

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body, model = await payload(request, "chat")
        protocol = model["protocol"]
        validate_chat(body, protocol)
        prefix = {"responses": "openai/responses/", "chat": "openai/", "messages": "anthropic/"}[protocol]
        kwargs = {k: v for k, v in body.items() if k in CHAT_FIELDS}
        count = body.get("n", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count != 1:
            raise HTTPException(400, "The economical bridge supports one completion per request (n=1)")
        if "stream" in body and not isinstance(body["stream"], bool):
            raise HTTPException(400, "stream must be a boolean")
        supports = model.get("capabilities", {}).get("supports", {})
        if body.get("tools") and supports.get("tool_calls") is False:
            raise HTTPException(400, "The selected model explicitly does not support tools")
        if body.get("stream") and supports.get("streaming") is False:
            raise HTTPException(400, "The selected model explicitly does not support streaming")
        if "reasoning_effort" in body and isinstance(supports.get("reasoning_effort"), list):
            if body["reasoning_effort"] not in supports["reasoning_effort"]:
                raise HTTPException(400, "reasoning_effort is not supported by the selected model")
        if "prompt_cache_retention" in kwargs:
            retention = kwargs.pop("prompt_cache_retention")
            if protocol == "messages" or retention not in ("in-memory", "24h"):
                raise HTTPException(400, "prompt_cache_retention requires an OpenAI protocol and in-memory or 24h")
            # LiteLLM 1.103.2 drops this normal kwarg. Promote only this validated
            # provider field; callers cannot supply arbitrary extra_body values.
            kwargs["extra_body"] = {"prompt_cache_retention": retention}
        if "prompt_cache_key" in kwargs:
            cache_key = kwargs["prompt_cache_key"]
            if protocol == "messages" or not isinstance(cache_key, str) or not cache_key or len(cache_key) > 512:
                raise HTTPException(400, "prompt_cache_key requires an OpenAI protocol and a nonempty string of at most 512 characters")
        try:
            result = await await_inference(request, completion(
                model=prefix + model["upstream_id"],
                api_base=settings.upstream_url if protocol != "messages" else settings.upstream_url.removesuffix("/v1"),
                api_key=settings.upstream_key, timeout=settings.timeout_seconds,
                num_retries=0, max_retries=0, **kwargs,
            ), cleanup_seconds=settings.stream_cleanup_seconds)
        except ClientDisconnected:
            return JSONResponse({"error": {"code": "client_disconnected", "message": "Client disconnected"}}, status_code=499)
        except Exception as exc:
            return JSONResponse(error_payload(exc), status_code=error_status(exc), headers=error_headers(exc))
        if body.get("stream"):
            return CopilotStreamingResponse(
                result, heartbeat_seconds=settings.heartbeat_seconds,
                idle_seconds=settings.stream_idle_seconds,
                cleanup_seconds=settings.stream_cleanup_seconds, error_payload=error_payload,
            )
        return result.model_dump(exclude_none=True)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body, model = await payload(request, "embeddings")
        validate_embedding(body)
        kwargs = {k: v for k, v in body.items() if k in EMBEDDING_FIELDS}
        try:
            result = await await_inference(request, embedding(model="openai/" + model["upstream_id"],
                                     api_base=settings.upstream_url, api_key=settings.upstream_key,
                                     timeout=settings.timeout_seconds, num_retries=0, max_retries=0, **kwargs), cleanup_seconds=settings.stream_cleanup_seconds)
        except ClientDisconnected:
            return JSONResponse({"error": {"code": "client_disconnected", "message": "Client disconnected"}}, status_code=499)
        except Exception as exc:
            return JSONResponse(error_payload(exc), status_code=error_status(exc), headers=error_headers(exc))
        return result.model_dump(exclude_none=True)

    return app
