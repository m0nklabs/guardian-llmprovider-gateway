"""Dynamic, authenticated Copilot catalog and OpenAI protocol adapter.

Runs separately from Guardian using the existing LiteLLM environment. Model
names and protocol selection come from copilot-api, never a static alias list.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger("guardian.copilot_bridge")
BRANDS = {"azure openai": "openai", "xai": "x-ai", "moonshot ai": "moonshotai"}
# Client payloads must never override LiteLLM's credentials, endpoint or provider.
CHAT_FIELDS = frozenset({
    "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "stop", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "reasoning_effort", "seed", "presence_penalty",
    "frequency_penalty", "logprobs", "top_logprobs", "user", "n",
})


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    upstream_key: str
    client_key: str
    refresh_seconds: float = 300
    timeout_seconds: float = 1200
    catalog_timeout_seconds: float = 30
    max_body_bytes: int = 16 * 1024 * 1024

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = yaml.safe_load(path.read_text())
        def key(name: str) -> str:
            value = os.environ.get(raw[name], "")
            if not value:
                raise ValueError(f"Missing environment variable named by {name}")
            return value
        result = cls(
            upstream_url=raw["upstream_url"].rstrip("/"),
            upstream_key=key("upstream_key_env"), client_key=key("client_key_env"),
            refresh_seconds=float(raw.get("refresh_seconds", 300)),
            timeout_seconds=float(raw.get("timeout_seconds", 1200)),
            catalog_timeout_seconds=float(raw.get("catalog_timeout_seconds", 30)),
            max_body_bytes=int(raw.get("max_body_bytes", 16 * 1024 * 1024)),
        )
        if not result.upstream_url.startswith(("http://", "https://")):
            raise ValueError("upstream_url must be HTTP(S)")
        if any(not (0 < value < float("inf")) for value in (
            result.refresh_seconds, result.timeout_seconds,
            result.catalog_timeout_seconds, result.max_body_bytes,
        )):
            raise ValueError("Bridge limits and intervals must be positive and finite")
        return result


def normalize_model(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Project a catalog entry into a stable maker/model address and protocol."""
    raw_id, vendor = entry.get("id"), entry.get("vendor") or entry.get("owned_by")
    if not isinstance(raw_id, str) or not raw_id or "/" in raw_id:
        return None
    if not isinstance(vendor, str) or not vendor.strip():
        return None  # Never manufacture a maker from the serving provider.
    if (entry.get("policy") or {}).get("state") == "disabled":
        return None
    capabilities = entry.get("capabilities") or {}
    endpoints = entry.get("supported_endpoints") or []
    if capabilities.get("type") == "embeddings":
        protocol = "embeddings"
    elif "/responses" in endpoints:
        protocol = "responses"
    elif "/chat/completions" in endpoints:
        protocol = "chat"
    elif "/v1/messages" in endpoints:
        protocol = "messages"
    else:
        return None  # Unknown or WebSocket-only protocols require an adapter.
    if entry.get("model_picker_enabled") is False and protocol != "embeddings":
        return None
    vendor = vendor.strip().lower()
    brand = BRANDS.get(vendor, re.sub(r"[^a-z0-9]+", "-", vendor).strip("-"))
    if not brand:
        return None
    limits = capabilities.get("limits") or {}
    supports = capabilities.get("supports") or {}
    public = {
        "id": f"{brand}/{raw_id}", "object": "model", "owned_by": brand,
        "name": entry.get("name", raw_id), "vendor": vendor,
        "upstream_id": raw_id, "protocol": protocol,
        "capabilities": capabilities, "supported_endpoints": endpoints,
        "architecture": {"input_modalities": ["text"] + (["image"] if supports.get("vision") else []),
                         "output_modalities": ["embedding" if protocol == "embeddings" else "text"]},
    }
    context = limits.get("max_context_window_tokens") or limits.get("max_prompt_tokens")
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        public["context_length"] = context
    output = limits.get("max_output_tokens")
    if isinstance(output, int) and not isinstance(output, bool) and output > 0:
        public["max_output_tokens"] = output
    efforts = supports.get("reasoning_effort")
    if isinstance(efforts, list) and efforts:
        public["reasoning"] = {"supported": True, "supported_efforts": efforts}
    return public


class Catalog:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        self.models: dict[str, dict[str, Any]] = {}
        self.last_success: float | None = None
        self.last_error: str | None = None
        self.lock = asyncio.Lock()

    async def refresh(self) -> None:
        async with self.lock:
            try:
                response = await self.client.get(
                    self.settings.upstream_url + "/models",
                    headers={"Authorization": "Bearer " + self.settings.upstream_key},
                    timeout=self.settings.catalog_timeout_seconds,
                )
                response.raise_for_status()
                entries = response.json().get("data")
                if not isinstance(entries, list) or not entries:
                    raise ValueError("Upstream catalog is missing or empty")
                models = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("Invalid catalog entry")
                    model = normalize_model(entry)
                    if model:
                        if model["id"] in models:
                            raise ValueError("Duplicate public model identity")
                        models[model["id"]] = model
                self.models = models  # Atomic replace also retires removed models.
                self.last_success, self.last_error = time.time(), None
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("Copilot catalog refresh failed (%s); retaining last successful snapshot", self.last_error)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.refresh_seconds)
            await self.refresh()


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None,
               completion=None, embedding=None) -> FastAPI:
    settings = settings or Settings.load(Path(os.environ.get(
        "GUARDIAN_COPILOT_BRIDGE_CONFIG",
        str(Path(__file__).resolve().parents[1] / "config/github-copilot-bridge.settings.yaml"),
    )))
    if completion is None or embedding is None:
        import litellm
        completion = completion or litellm.acompletion
        embedding = embedding or litellm.aembedding
    upstream = client or httpx.AsyncClient()
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
                "last_success": catalog.last_success, "last_error": catalog.last_error}

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
        prefix = {"responses": "openai/responses/", "chat": "openai/", "messages": "anthropic/"}[protocol]
        kwargs = {k: v for k, v in body.items() if k in CHAT_FIELDS}
        try:
            result = await completion(
                model=prefix + model["upstream_id"],
                api_base=settings.upstream_url if protocol != "messages" else settings.upstream_url.removesuffix("/v1"),
                api_key=settings.upstream_key, timeout=settings.timeout_seconds, **kwargs,
            )
        except Exception as exc:
            code = getattr(exc, "status_code", 502)
            raise HTTPException(code if isinstance(code, int) and 400 <= code <= 599 else 502,
                                "Copilot upstream request failed") from None
        if body.get("stream"):
            async def events():
                try:
                    async for chunk in result:
                        yield "data: " + chunk.model_dump_json(exclude_none=True) + "\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    close = getattr(result, "aclose", None)
                    if close:
                        await close()
            return StreamingResponse(events(), media_type="text/event-stream")
        return result.model_dump(exclude_none=True)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body, model = await payload(request, "embeddings")
        kwargs = {k: v for k, v in body.items() if k in {"input", "dimensions", "encoding_format", "user"}}
        try:
            result = await embedding(model="openai/" + model["upstream_id"],
                                     api_base=settings.upstream_url, api_key=settings.upstream_key,
                                     timeout=settings.timeout_seconds, **kwargs)
        except Exception as exc:
            code = getattr(exc, "status_code", 502)
            raise HTTPException(code if isinstance(code, int) and 400 <= code <= 599 else 502,
                                "Copilot embedding request failed") from None
        return result.model_dump(exclude_none=True)

    return app
