"""Use the guarded HTTP pool once and preserve safe retry guidance for LiteLLM.

LiteLLM may rebuild errors without headers and retry low-level connection errors
independently of max_retries. This adapter records only safe Retry-After and uses
one guarded send. No prompts, credentials or raw error bodies are retained.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from app.copilot_errors import retry_after


@dataclass
class _CallState:
    retry_hint: str | None = None
    error_status: int | None = None
    responses: list[httpx.Response] = field(default_factory=list)


class _OwnedResult:
    """Close the original HTTP response even if a provider iterator cannot."""
    def __init__(self, result, responses):
        self.result, self.responses = result, responses

    def __aiter__(self):
        return self.result.__aiter__()

    async def aclose(self):
        # Close HTTP first: an upstream iterator may not implement aclose, or
        # may need the transport to close before its pending read can finish.
        for response in self.responses:
            await response.aclose()
        close = getattr(self.result, "aclose", None)
        if close:
            await close()


class LitellmOperations:
    """Bridge-owned callables sharing one caller-owned guarded HTTP pool."""
    def __init__(self, client: httpx.AsyncClient, *, cleanup_seconds: float = 5):
        import litellm
        from openai import AsyncOpenAI
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        self._litellm, self._client = litellm, client
        self._cleanup_seconds = cleanup_seconds
        self._state: ContextVar[_CallState | None] = ContextVar("copilot_http_state", default=None)

        class BorrowedHandler(AsyncHTTPHandler):
            def create_client(self, **_):
                return client

            async def post(self, url, data=None, json=None, params=None, headers=None,
                           timeout=None, stream=False, logging_obj=None, files=None, content=None):
                # Do not inherit AsyncHTTPHandler.post's unconditional replay on
                # RemoteProtocolError/ConnectError. These can occur after send.
                if isinstance(data, (str, bytes)) and content is None:
                    content, data = data, None
                request = client.build_request("POST", url, data=data, json=json,
                    params=params, headers=headers, timeout=timeout, files=files, content=content)
                response = await client.send(request, stream=stream, follow_redirects=False)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    exc.status_code = response.status_code
                    raise
                return response

        self._handler = BorrowedHandler(client_alias="guardian-copilot")
        self._handler.client = client
        self._openai = AsyncOpenAI(api_key="supplied-per-call", http_client=client, max_retries=0)
        client.event_hooks.setdefault("response", []).append(self._capture)

    async def _capture(self, response: httpx.Response):
        state = self._state.get()
        if state is not None:
            state.responses.append(response)
            if 400 <= response.status_code <= 599:
                state.error_status = response.status_code
            if response.status_code in (429, 503):
                state.retry_hint = retry_after(response.headers.get("Retry-After"))

    async def _invoke(self, operation, kwargs):
        if self._litellm.model_fallbacks:
            raise RuntimeError("The economical Copilot adapter refuses process-global model fallbacks")
        state = _CallState()
        token = self._state.set(state)
        streamed = False
        try:
            result = await operation(**kwargs, num_retries=0, max_retries=0, fallbacks=[])
            if kwargs.get("stream"):
                streamed = True
                return _OwnedResult(result, state.responses)
            return result
        except Exception as exc:
            if state.error_status is not None:
                exc.status_code = state.error_status
            if state.retry_hint is not None:
                exc.headers = {"Retry-After": state.retry_hint}
            raise
        finally:
            self._state.reset(token)
            if not streamed:
                import anyio
                with anyio.move_on_after(self._cleanup_seconds, shield=True):
                    for response in state.responses:
                        await response.aclose()

    async def _responses_completion(self, **kwargs):
        # LiteLLM 1.103.2's async Chat→Responses bridge omits its client kwarg.
        # Reuse its request/response translators with an explicit direct
        # aresponses call, not a global monkeypatch or a second inference route.
        from datetime import datetime, timezone
        from uuid import uuid4
        from litellm.completion_extras.litellm_responses_transformation.handler import ResponsesToCompletionBridgeHandler
        from litellm.litellm_core_utils.litellm_logging import Logging
        from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
        from litellm.types.utils import ModelResponse

        model = kwargs["model"].removeprefix("openai/responses/")
        messages = kwargs["messages"]
        stream = kwargs.get("stream", False)
        logging = Logging(model=model, messages=messages, stream=stream, call_type="completion",
                          start_time=datetime.now(timezone.utc), litellm_call_id=str(uuid4()), function_id=str(uuid4()))
        bridge = ResponsesToCompletionBridgeHandler()
        transform = bridge.transformation_handler
        parameters = {key: kwargs[key] for key in ("api_base", "api_key", "timeout", "num_retries", "max_retries", "fallbacks") if key in kwargs}
        options = {key: value for key, value in kwargs.items() if key not in parameters and key not in ("model", "messages", "client")}
        request = transform.transform_request(model=model, messages=messages, optional_params=options,
                    litellm_params=parameters, headers={}, litellm_logging_obj=logging, client=self._handler)
        request["model"] = "openai/" + model
        result = await self._litellm.aresponses(**request)
        if stream:
            iterator = transform.get_model_response_iterator(streaming_response=result, sync_stream=False)
            return CustomStreamWrapper(completion_stream=iterator, model=model, custom_llm_provider="openai", logging_obj=logging,
                                       stream_options=kwargs.get("stream_options"))
        return transform.transform_response(model=model, raw_response=result, model_response=ModelResponse(),
                    logging_obj=logging, request_data=request, messages=messages, optional_params=options,
                    litellm_params=parameters, encoding=None, api_key=kwargs.get("api_key"))

    async def completion(self, **kwargs):
        kwargs.pop("num_retries", None)
        kwargs.pop("max_retries", None)
        model = kwargs.get("model", "")
        kwargs["client"] = self._handler if model.startswith(("openai/responses/", "anthropic/")) else self._openai
        operation = self._responses_completion if model.startswith("openai/responses/") else self._litellm.acompletion
        return await self._invoke(operation, kwargs)

    async def embedding(self, **kwargs):
        kwargs.pop("num_retries", None)
        kwargs.pop("max_retries", None)
        kwargs["client"] = self._openai
        return await self._invoke(self._litellm.aembedding, kwargs)

    def detach(self):
        """Remove this hook, without closing the borrowed pool."""
        hooks = self._client.event_hooks.get("response", [])
        if self._capture in hooks:
            hooks.remove(self._capture)
