"""FEAT-5 + FEAT-6: GBNF pre-validation and capture presence flags.

FEAT-5: ``grammar.validate_gbnf`` pre-validates GBNF structure before
forwarding to llama-server (fail-open, off by default).
FEAT-6: capture events expose ``grammar_present``/``response_format_present``
boolean flags and never leak raw grammar/schema content.
"""

import pytest

from app.capture import schema as capture_schema
from app.capture import redactor as redactor_mod
from app.gateway import normalization as _normalization


class TestGbnfValidation:
    def _set_flag(self, value):
        original = _normalization._grammar_validate_gbnf
        _normalization._grammar_validate_gbnf = value
        return original

    def test_valid_gbnf_passes_through_unchanged(self):
        original = self._set_flag(True)
        try:
            result = _normalization.validate_grammar_field({"grammar": 'root ::= "hello"'})
            assert result is None
        finally:
            _normalization._grammar_validate_gbnf = original

    def test_invalid_gbnf_returns_400_with_detail_when_enabled(self):
        original = self._set_flag(True)
        try:
            result = _normalization.validate_grammar_field({"grammar": 'root ::= {'})
            assert result is not None
            assert result.status_code == 400
            body = result.body
            assert b"Invalid GBNF grammar" in body
        finally:
            _normalization._grammar_validate_gbnf = original

    def test_missing_rule_definition_returns_400(self):
        original = self._set_flag(True)
        try:
            result = _normalization.validate_grammar_field({"grammar": "no rule here"})
            assert result is not None
            assert result.status_code == 400
        finally:
            _normalization._grammar_validate_gbnf = original

    def test_noop_when_flag_false(self):
        original = self._set_flag(False)
        try:
            result = _normalization.validate_grammar_field({"grammar": 'root ::= {'})
            assert result is None
        finally:
            _normalization._grammar_validate_gbnf = original

    def test_no_grammar_field_is_noop(self):
        original = self._set_flag(True)
        try:
            result = _normalization.validate_grammar_field({"model": "x"})
            assert result is None
        finally:
            _normalization._grammar_validate_gbnf = original


class TestCaptureGrammarFlags:
    def _build_ctx(self, **kwargs):
        from app.capture.config import CaptureConfig

        ctx = capture_schema.BuildContext(
            request_id="req-1",
            endpoint="/v1/chat/completions",
            ingress_protocol="openai",
            route_type="local",
            requested_model="llama3.2-3b",
            capture_policy_version="1.0.0",
            instance_id="test",
            client_fingerprint="fp",
            grammar_present=kwargs.get("grammar_present", False),
            response_format_present=kwargs.get("response_format_present", False),
        )
        config = CaptureConfig(instance_id="test", policy_version="1.0.0")
        return ctx, config

    def test_event_includes_presence_flags(self):
        ctx, config = self._build_ctx(grammar_present=True, response_format_present=True)
        event = capture_schema.build_request_received_event(
            config, ctx,
            request_messages=[],
            request_parameters={"temperature": 0.7},
        )
        assert event["grammar_present"] is True
        assert event["response_format_present"] is True

    def test_event_flags_false_when_absent(self):
        ctx, config = self._build_ctx()
        event = capture_schema.build_request_received_event(config, ctx)
        assert event["grammar_present"] is False
        assert event["response_format_present"] is False

    def test_redactor_strips_grammar_content(self):
        params = {
            "temperature": 0.7,
            "grammar": 'root ::= "yes" | "no"',
            "json_schema": {"type": "object"},
            "response_format": {"type": "json_object"},
        }
        result = redactor_mod.redact_request_parameters(params)
        assert result["grammar"] == "[REDACTED]"
        assert result["json_schema"] == "[REDACTED]"
        assert result["response_format"] == "[REDACTED]"
        assert "yes" not in json_dumps(result)
        assert result["temperature"] == 0.7


import json


def json_dumps(obj):
    return json.dumps(obj)
