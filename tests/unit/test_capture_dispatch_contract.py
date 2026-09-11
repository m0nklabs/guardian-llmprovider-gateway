"""Dispatch→controller contract pins for the terminal capture event.

Regression guard for the 2026-09-11 silent-capture-loss defect (found by the
agent31 setup session, reported via docs/HANDOFF.md): the dispatcher passed
``degeneration_cutoff=...`` to ``CaptureController.capture_request_completed``
while the controller accepted neither that parameter nor ``**kwargs`` — the
fail-open ``except Exception`` swallowed the TypeError and ALL terminal
capture events silently disappeared (live proof: 48 request_received, 0
completed/failed in the current capture file).

These pins drive the REAL ``CaptureController`` (only the final ``_dispatch``
sink is captured), so any future dispatcher/controller signature drift fails
loudly here instead of silently in production.
"""

from types import SimpleNamespace

from app.capture import integration
from app.capture.integration import CaptureController
from app.gateway import capture_dispatch
from tests.unit.test_capture_schema import base_ctx as _schema_base_ctx_fixture


# Fixture re-export: requesting ``base_ctx`` in this module resolves to the
# schema-suite ctx fixture (pytest resolves by module-level binding).
base_ctx = _schema_base_ctx_fixture


def _policy(should_capture: bool = True):
    return SimpleNamespace(should_capture=should_capture)


class _EventSink:
    def __init__(self) -> None:
        self.events: list = []

    def _dispatch(self, event) -> None:
        self.events.append(event)


def _wired_controller(monkeypatch) -> tuple[_EventSink, CaptureController]:
    controller = CaptureController.__new__(CaptureController)
    sink = _EventSink()
    controller._dispatch = sink._dispatch
    controller._config = integration.load_capture_config()
    monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: controller)
    return sink, controller


def _dispatch(monkeypatch, sink, ctx, **kwargs):
    base = dict(
        policy_result=_policy(),
        response_content="answer",
        finish_reason="stop",
    )
    base.update(kwargs)
    capture_dispatch.dispatch_capture_request_completed(ctx, **base)


class TestDispatchToControllerContract:
    def test_cutoff_true_reaches_event(self, monkeypatch, base_ctx):
        sink, _ = _wired_controller(monkeypatch)
        _dispatch(monkeypatch, sink, base_ctx, degeneration_cutoff=True)
        assert len(sink.events) == 1
        assert sink.events[0]["degeneration_cutoff"] is True

    def test_cutoff_false_normal_completion_field_absent(self, monkeypatch, base_ctx):
        """Normal completion: the additive field must stay absent (schema 1.2.0
        only emits it when True)."""
        sink, _ = _wired_controller(monkeypatch)
        _dispatch(monkeypatch, sink, base_ctx)
        assert len(sink.events) == 1
        assert "degeneration_cutoff" not in sink.events[0]

    def test_cutoff_false_explicit_also_absent(self, monkeypatch, base_ctx):
        sink, _ = _wired_controller(monkeypatch)
        _dispatch(monkeypatch, sink, base_ctx, degeneration_cutoff=False)
        assert len(sink.events) == 1
        assert "degeneration_cutoff" not in sink.events[0]

    def test_no_capture_policy_skips_entirely(self, monkeypatch, base_ctx):
        sink, _ = _wired_controller(monkeypatch)
        _dispatch(monkeypatch, sink, base_ctx, policy_result=_policy(False), degeneration_cutoff=True)
        assert sink.events == []

    def test_signature_drift_is_not_silent(self, monkeypatch, caplog, base_ctx):
        """If a future kwarg is added to the dispatcher but not the controller
        (the exact 2026-09-11 defect shape), the fail-open swallow must log a
        content-free warning instead of disappearing."""
        controller = CaptureController.__new__(CaptureController)
        controller._config = integration.load_capture_config()
        calls: list = []

        def _broken_capture_request_completed(*args, **kwargs):
            calls.append(kwargs)
            if "degeneration_cutoff" in kwargs:
                # Faithful simulation of the old controller binding: explicit
                # parameter list without the kwarg → bind-time TypeError.
                raise TypeError(
                    "capture_request_completed() got an unexpected keyword argument 'degeneration_cutoff'"
                )

        controller.capture_request_completed = _broken_capture_request_completed
        monkeypatch.setattr(capture_dispatch, "get_capture_controller", lambda: controller)
        capture_dispatch.dispatch_capture_request_completed(
            base_ctx,
            policy_result=_policy(),
            response_content="answer",
            finish_reason="stop",
            degeneration_cutoff=True,
        )
        assert calls, "controller must be reached"
        assert any("capture dispatch failed" in r.message for r in caplog.records)
