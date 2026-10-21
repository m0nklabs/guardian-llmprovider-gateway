"""G1 regression tests: non-stream capture keeps reasoning + finish_reason.

Root cause (2026-08-30, pr-piet evidence G1): non-streaming capture paths
dropped reasoning bodies entirely and never surfaced finish_reason — the
cloud path gated reasoning extraction on content presence (forwarding.py),
and the local dispatcher read finish_reason from the wrong JSON location
(message instead of choices[0]). A length-truncated reasoning-only response
was therefore captured as a body-less record (0/0), which hid ~300-500k
chars of upstream reasoning per runaway generation.
"""

import json
from types import SimpleNamespace

import pytest

from app.gateway import capture_dispatch


@pytest.fixture(autouse=True)
def _wire_di_slots(monkeypatch):
    """Wire the init()-injected helper slots the dispatcher resolves at call
    time (production wires them in server.py init(); unit tests get minimal
    stand-ins so a missing slot cannot be silently swallowed by fail-open)."""
    monkeypatch.setattr(
        capture_dispatch,
        "_coerce_usage_int",
        lambda value: int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None,
    )


class _FakeController:
    """Records capture_request_completed kwargs; fails loudly on misuse."""

    def __init__(self):
        self.completed = []

    def capture_request_completed(self, ctx, **kwargs):
        self.completed.append(kwargs)


def _patch_controller(monkeypatch):
    controller = _FakeController()
    monkeypatch.setattr(
        capture_dispatch, "get_capture_controller", lambda: controller
    )
    return controller


def _policy():
    return SimpleNamespace(should_capture=True)


def _ctx():
    return SimpleNamespace(request_id="req-g1")


def _request():
    return SimpleNamespace(headers={})


class TestDispatchCaptureNonstreamCompleted:
    def test_reasoning_only_length_response_is_captured(self, monkeypatch):
        """The G1 production signature: content null, reasoning at
        ``message.reasoning`` (OpenRouter), finish_reason on choices[0]."""
        controller = _patch_controller(monkeypatch)
        payload = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": None, "reasoning": "R" * 10},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 100},
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-g1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["reasoning_content"] == "R" * 10
        assert event["finish_reason"] == "length"
        assert event["response_content"] is None
        assert event["incomplete"] is False
        assert event["streamed"] is False

    def test_non_finite_usage_omitted_event_survives(self, monkeypatch):
        """A malformed usage object (1e999 -> inf) must not drop the whole
        request_completed event (the outer fail-open except would swallow
        the OverflowError); the non-finite mirror fields are omitted."""
        controller = _patch_controller(monkeypatch)
        payload = {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "OK"}},
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "native_tokens_reasoning": 1e999,
                "native_tokens_cached": 1e999,
                "cost": 1e999,
            },
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-inf-1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["response_content"] == "OK"
        assert not event.get("native_tokens_reasoning")
        assert not event.get("native_tokens_cached")
        assert not event.get("cost")
        line = json.dumps(event, separators=(",", ":"), default=str)
        assert "Infinity" not in line and "NaN" not in line

    def test_reasoning_content_key_captured_without_duplication(self, monkeypatch):
        controller = _patch_controller(monkeypatch)
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Answer", "reasoning_content": "Think"},
                }
            ]
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-g1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["reasoning_content"] == "Think"
        assert event["response_content"] == "Answer"
        assert event["finish_reason"] == "stop"
        assert event["incomplete"] is False

    def test_missing_finish_reason_marks_incomplete(self, monkeypatch):
        controller = _patch_controller(monkeypatch)
        payload = {"choices": [{"message": {"content": "partial"}}]}
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-g1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["finish_reason"] is None
        assert event["incomplete"] is True

    def test_anthropic_stop_reason_captured(self, monkeypatch):
        controller = _patch_controller(monkeypatch)
        payload = {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-g1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["finish_reason"] == "end_turn"
        assert event["response_content"] == "hi"


class TestDispatchCaptureStreamCompleted:
    def test_reasoning_passed_from_assembler(self, monkeypatch):
        """The local streaming dispatcher must pass the assembler's
        reasoning through (the cloud streaming path already did)."""
        controller = _patch_controller(monkeypatch)
        assembler = SimpleNamespace(
            assemble=lambda: {
                "content": "C",
                "finish_reason": "stop",
                "tool_calls": None,
                "reasoning_content": "R",
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "incomplete": False,
            }
        )
        capture_dispatch.dispatch_capture_stream_completed(
            _request(), "req-g1", "client", "model", _ctx(), _policy(),
            assembler, {"prompt_tokens": 1, "completion_tokens": 2},
            "chat/completions", 200,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["reasoning_content"] == "R"
        assert event["finish_reason"] == "stop"
        assert event["streamed"] is True


class TestNonstreamResponsesAndCompletions:
    """New endpoint coverage: Responses objects, legacy completions, embeddings."""

    def test_responses_payload_captured(self, monkeypatch):
        """Responses non-stream object: output items → content/tool_calls/
        reasoning; usage maps input/output_tokens; status → finish_reason."""
        controller = _patch_controller(monkeypatch)
        payload = {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "llama3.2-3b",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": "Think"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city":"Boston"}',
                    "status": "completed",
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello", "annotations": []}],
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-r1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        assert len(controller.completed) == 1
        event = controller.completed[0]
        assert event["response_content"] == "Hello"
        assert event["reasoning_content"] == "Think"
        assert event["finish_reason"] == "stop"
        assert event["prompt_tokens"] == 10
        assert event["completion_tokens"] == 5
        assert event["completion_tokens_details"] == {"reasoning_tokens": 2}
        assert event["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Boston"}'},
            }
        ]
        assert event["incomplete"] is False

    def test_responses_incomplete_maps_max_output_tokens(self, monkeypatch):
        controller = _patch_controller(monkeypatch)
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "partial"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-r2", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        event = controller.completed[0]
        assert event["finish_reason"] == "length"
        assert event["response_content"] == "partial"

    def test_legacy_completions_payload_text_captured(self, monkeypatch):
        """Legacy completions choices carry ``text`` (no message/delta)."""
        controller = _patch_controller(monkeypatch)
        payload = {
            "choices": [{"text": "Once upon a time", "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 7},
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-c1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        event = controller.completed[0]
        assert event["response_content"] == "Once upon a time"
        assert event["finish_reason"] == "stop"
        assert event["prompt_tokens"] == 4
        assert event["completion_tokens"] == 7

    def test_embeddings_payload_content_none_usage_extracted(self, monkeypatch):
        """Embeddings carry vectors (data list, no choices) — no content text,
        but the usage is still captured."""
        controller = _patch_controller(monkeypatch)
        payload = {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 12, "total_tokens": 12},
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-e1", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        event = controller.completed[0]
        assert event["response_content"] is None
        assert event["prompt_tokens"] == 12

    def test_responses_malformed_payload_does_not_break_dispatch(self, monkeypatch):
        """Fail-open: a malformed Responses payload must not raise; the
        event still reaches the controller with empty semantics."""
        controller = _patch_controller(monkeypatch)
        payload = {
            "output": [
                {"type": "message", "role": "assistant", "content": 42},
                "not-a-dict",
            ]
        }
        capture_dispatch.dispatch_capture_nonstream_completed(
            _request(), "req-r3", "client", "model",
            _ctx(), _policy(), payload, 200, 0.0,
        )
        event = controller.completed[0]
        assert event["response_content"] is None
