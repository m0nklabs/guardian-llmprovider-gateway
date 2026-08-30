"""G1 regression tests: non-stream capture keeps reasoning + finish_reason.

Root cause (2026-08-30, pr-piet evidence G1): non-streaming capture paths
dropped reasoning bodies entirely and never surfaced finish_reason — the
cloud path gated reasoning extraction on content presence (forwarding.py),
and the local dispatcher read finish_reason from the wrong JSON location
(message instead of choices[0]). A length-truncated reasoning-only response
was therefore captured as a body-less record (0/0), which hid ~300-500k
chars of upstream reasoning per runaway generation.
"""

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
