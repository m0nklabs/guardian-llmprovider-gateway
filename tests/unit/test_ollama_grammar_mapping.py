"""FEAT-2: Ollama bridge maps ``options.format`` to llama-server GCD fields.

- dict (JSON schema) → ``response_format`` (OpenAI-native)
- string (GBNF grammar) → ``grammar`` (llama-server native)
- client's explicit top-level ``response_format``/``grammar`` wins.
- the global ``grammar.enabled`` kill-switch disables the mapping.
"""

import pytest

from app.local_inference import ollama as _ollama


def _apply(body, openai_body=None):
    result = openai_body if openai_body is not None else {}
    _ollama._apply_ollama_format_mapping(body, result)
    return result


class TestOllamaFormatMapping:
    def test_dict_format_maps_to_response_format(self):
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        body = {"options": {"format": schema}}
        result = _apply(body)
        assert result["response_format"] == schema
        assert "grammar" not in result

    def test_string_format_maps_to_grammar(self):
        body = {"options": {"format": 'root ::= "yes" | "no"'}}
        result = _apply(body)
        assert result["grammar"] == 'root ::= "yes" | "no"'
        assert "response_format" not in result

    def test_explicit_top_level_response_format_wins(self):
        top_level = {"type": "json_object"}
        schema = {"type": "object"}
        body = {"response_format": top_level, "options": {"format": schema}}
        result = _apply(body)
        assert result["response_format"] == top_level
        assert "grammar" not in result

    def test_explicit_top_level_grammar_wins(self):
        top_level = 'root ::= "explicit"'
        schema = {"type": "object"}
        body = {"grammar": top_level, "options": {"format": schema}}
        result = _apply(body)
        assert result["grammar"] == top_level
        assert "response_format" not in result

    def test_no_options_format_is_noop(self):
        result = _apply({"options": {"temperature": 0.5}})
        assert result == {}

    def test_kill_switch_disables_mapping(self):
        schema = {"type": "object"}
        body = {"options": {"format": schema}}
        original = _ollama._grammar_enabled
        try:
            _ollama._grammar_enabled = False
            result = _apply(body)
            assert result == {}
        finally:
            _ollama._grammar_enabled = original

    def test_json_sentinel_maps_to_response_format_not_grammar(self):
        # Ollama's documented JSON-mode convention is ``format: "json"`` — a
        # sentinel, NOT GBNF. Mapping it to ``grammar: "json"`` would cause a
        # llama-server GBNF parse error (undefined rule). Must translate to
        # OpenAI-native JSON mode instead.
        body = {"options": {"format": "json"}}
        result = _apply(body)
        assert result["response_format"] == {"type": "json_object"}
        assert "grammar" not in result
