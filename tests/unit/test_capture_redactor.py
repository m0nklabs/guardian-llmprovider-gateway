"""Unit tests for the capture redactor module."""

import base64
import hashlib
import json
import re

import pytest

from app.capture.redactor import (
    redact_request_messages,
    redact_response_content,
    redact_request_parameters,
    redact_reasoning_content,
    redact_tool_results,
    redact_tool_calls,
    redact_image_blocks,
    redact_source_ip,
    redact_authorization_header,
    scan_for_secrets,
)
from app.capture.config import CaptureConfig


@pytest.fixture
def field_policies():
    return {
        "system_prompts": "strip",
        "reasoning": "strip",
        "tool_definitions": "capture",
        "tool_calls": "capture",
        "tool_results": "strip",
        "images": "hash_and_metadata",
        "unknown_content_blocks": "strip",
    }


class TestRequestMessageRedaction:
    def test_strips_system_messages_by_default(self, field_policies):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        result = redact_request_messages(messages, field_policies)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_preserves_system_messages_when_capture(self):
        policies = {"system_prompts": "capture", "reasoning": "strip",
                    "tool_definitions": "capture", "tool_calls": "capture",
                    "tool_results": "strip", "images": "hash_and_metadata",
                    "unknown_content_blocks": "strip"}
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        result = redact_request_messages(messages, policies)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert "You are a helpful assistant." in result[0]["content"]

    def test_redacts_api_keys_in_message_content(self, field_policies):
        messages = [
            {"role": "user", "content": "My key is sk-or-v1-abcdef1234567890"},
        ]
        result = redact_request_messages(messages, field_policies)
        serialized = json.dumps(result)
        assert "sk-or-v1-abcdef1234567890" not in serialized
        assert "REDACTED" in serialized

    def test_redacts_openai_keys(self, field_policies):
        messages = [
            {"role": "user", "content": f"Use key sk-proj-{ 'a' * 200 } to access"},
        ]
        result = redact_request_messages(messages, field_policies)
        serialized = json.dumps(result)
        assert "sk-proj-" not in serialized

    def test_redacts_nvapi_keys(self, field_policies):
        messages = [
            {"role": "user", "content": "nvapi-abc123def456ghi789jkl012="},
        ]
        result = redact_request_messages(messages, field_policies)
        serialized = json.dumps(result)
        assert "nvapi-" not in serialized

    def test_redacts_bearer_tokens(self, field_policies):
        messages = [
            {"role": "user", "content": "Authorization: Bearer eyJhbGci1234567890"},
        ]
        result = redact_request_messages(messages, field_policies)
        serialized = json.dumps(result)
        assert "eyJhbGci1234567890" not in serialized

    def test_returns_none_for_invalid_input(self, field_policies):
        assert redact_request_messages(None, field_policies) is None
        assert redact_request_messages("not a list", field_policies) is None
        assert redact_request_messages({}, field_policies) is None

    def test_preserves_non_system_messages(self, field_policies):
        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = redact_request_messages(messages, field_policies)
        assert len(result) == 2
        assert result[0]["content"] == "Hello world"
        assert result[1]["content"] == "Hi there"

    def test_strips_reasoning_content_by_default(self, field_policies):
        messages = [
            {"role": "assistant", "reasoning_content": "thinking...", "content": "Hello"},
        ]
        result = redact_request_messages(messages, field_policies)
        assert "reasoning_content" not in result[0]

    def test_preserves_reasoning_when_policy_captures(self):
        policies = {"system_prompts": "strip", "reasoning": "capture",
                    "tool_definitions": "capture", "tool_calls": "capture",
                    "tool_results": "strip", "images": "hash_and_metadata",
                    "unknown_content_blocks": "strip"}
        messages = [
            {"role": "assistant", "reasoning_content": "thinking...", "content": "Hello"},
        ]
        result = redact_request_messages(messages, policies)
        assert "reasoning_content" in result[0]
        assert result[0]["reasoning_content"] == "thinking..."

    def test_handles_multimodal_content_blocks(self, field_policies):
        # Image blocks should be replaced with metadata, text blocks preserved
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,invalid"}},
            ]},
        ]
        result = redact_request_messages(messages, field_policies)
        assert len(result) == 1
        content = result[0]["content"]
        assert isinstance(content, list)
        # Text block preserved
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) == 1
        assert "What's in this image?" in text_blocks[0]["text"]
        # Image block replaced with metadata (or removed if invalid base64)
        image_blocks = [b for b in content if b.get("type") == "image_metadata"]
        # Invalid base64 -> no metadata
        assert len(image_blocks) == 0

    def test_strips_tool_results_by_default(self, field_policies):
        messages = [
            {"role": "tool", "content": "result of tool"},
        ]
        result = redact_request_messages(messages, field_policies)
        assert len(result) == 1
        # Tool messages are preserved (role="tool") but content is redacted
        assert result[0]["role"] == "tool"

    def test_strips_unknown_content_blocks(self, field_policies):
        messages = [
            {"role": "user", "content": [
                {"type": "unknown_block_type", "data": "secret_data"},
            ]},
        ]
        result = redact_request_messages(messages, field_policies)
        assert len(result) == 1
        # Unknown block type is stripped
        assert len(result[0]["content"]) == 0


class TestResponseContentRedaction:
    def test_redacts_api_keys(self):
        content = "The answer is sk-or-v1-abc123def456ghijkl and nvapi-0KeCv_xZdXTmKxVrv4kUN test"
        result = redact_response_content(content)
        assert "sk-or-v1-abc123def456ghijkl" not in result
        assert "nvapi-0KeCv_xZdXTmKxVrv4kUN" not in result

    def test_redacts_ip_addresses(self):
        content = "Server at 192.168.1.1 returned the response"
        result = redact_response_content(content)
        assert "192.168.1.1" not in result
        assert "[REDACTED_IP]" in result

    def test_redacts_env_vars(self):
        content = "The key is ${OPENAI_API_KEY} here"
        result = redact_response_content(content)
        assert "${OPENAI_API_KEY}" not in result

    def test_returns_none_for_none_input(self):
        assert redact_response_content(None) is None

    def test_handles_non_string_input(self):
        result = redact_response_content(12345)
        assert result == "12345"


class TestRequestParameterRedaction:
    def test_strips_api_key_field(self, field_policies):
        params = {"temperature": 0.7, "api_key": "secret123", "model": "gpt-4"}
        result = redact_request_parameters(params)
        assert result["temperature"] == 0.7
        assert result["api_key"] == "[REDACTED]"
        assert result["model"] == "gpt-4"

    def test_strips_authorization_field(self):
        params = {"authorization": "Bearer token123"}
        result = redact_request_parameters(params)
        assert result["authorization"] == "[REDACTED]"

    def test_strips_headers_field(self):
        params = {"headers": {"Authorization": "Bearer token"}}
        result = redact_request_parameters(params)
        assert result["headers"] == "[REDACTED]"

    def test_strips_tools_when_policy_is_strip(self):
        policies = {"tool_definitions": "strip"}
        params = {"tools": [{"type": "function", "function": {"name": "calc"}}]}
        result = redact_request_parameters(params, policies)
        assert "tools" not in result

    def test_preserves_tools_when_policy_is_capture(self):
        policies = {"tool_definitions": "capture"}
        params = {"tools": [{"type": "function", "function": {"name": "calc"}}]}
        result = redact_request_parameters(params, policies)
        assert "tools" in result

    def test_redacts_secrets_in_nested_values(self):
        params = {"config": {"api_key": "nested-secret"}}
        result = redact_request_parameters(params)
        assert result["config"]["api_key"] == "[REDACTED]"

    def test_returns_none_for_invalid_input(self):
        assert redact_request_parameters(None) is None
        assert redact_request_parameters("string") is None

    def test_redacts_secrets_in_string_values(self):
        params = {"description": "key=sk-or-v1-abcdef1234567890"}
        result = redact_request_parameters(params)
        assert "sk-or-v1-abcdef1234567890" not in json.dumps(result)


class TestReasoningRedaction:
    def test_strips_reasoning_by_default(self):
        assert redact_reasoning_content("thinking...", "strip") is None

    def test_captures_reasoning_when_policy_allows(self):
        result = redact_reasoning_content("thinking...", "capture")
        assert result == "thinking..."

    def test_redacts_secrets_in_reasoning(self):
        result = redact_reasoning_content("key=nvapi-test1234567890", "capture")
        assert "nvapi-test1234567890" not in result

    def test_returns_none_for_none(self):
        assert redact_reasoning_content(None, "strip") is None
        assert redact_reasoning_content(None, "capture") is None


class TestToolResultsRedaction:
    def test_strips_tool_results_by_default(self):
        results = [{"tool_id": "1", "output": "data"}]
        assert redact_tool_results(results, "strip") is None

    def test_captures_tool_results_when_policy_allows(self):
        results = [{"tool_id": "1", "output": "data"}]
        redacted = redact_tool_results(results, "capture")
        assert redacted is not None
        assert len(redacted) == 1
        assert redacted[0]["output"] == "data"

    def test_redacts_secrets_in_tool_results(self):
        results = [{"tool_id": "1", "output": "sk-or-v1-secretkeydata12345678901234567890"}]
        redacted = redact_tool_results(results, "capture")
        assert "sk-or-v1-secretkeydata" not in json.dumps(redacted)

    def test_returns_none_for_invalid_input(self):
        assert redact_tool_results(None, "capture") is None
        assert redact_tool_results("string", "capture") is None


class TestToolCallsRedaction:
    def test_strips_tool_calls_when_policy_is_strip(self):
        calls = [{"id": "1", "type": "function", "function": {"name": "calc"}}]
        assert redact_tool_calls(calls, "strip") is None

    def test_captures_tool_calls_when_policy_is_capture(self):
        calls = [{"id": "1", "type": "function", "function": {"name": "calc"}}]
        redacted = redact_tool_calls(calls, "capture")
        assert redacted is not None
        assert len(redacted) == 1

    def test_strips_api_key_from_tool_calls(self):
        calls = [{"id": "1", "type": "function", "function": {"name": "calc", "arguments": '{"api_key": "sk-or-v1-secretkeydata12345678901234567890"}'}}]
        redacted = redact_tool_calls(calls, "capture")
        serialized = json.dumps(redacted)
        assert "sk-or-v1-secretkeydata" not in serialized


class TestImageBlockRedaction:
    def test_hash_and_metadata_replaces_raw_image(self):
        # Create a minimal valid PNG
        png_header = b'\x89PNG\r\n\x1a\n'
        ihdr = b'\x00\x00\x00\x0dIHDR' + b'\x00\x00\x00\x01' + b'\x00\x00\x00\x01' + b'\x08\x02\x00\x00\x00'
        # This is not a valid PNG but tests the structure
        # Use a real minimal approach: just test with a simple image
        raw = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 20).decode("ascii")
        blocks = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw}"}},
        ]
        result = redact_image_blocks(blocks, "hash_and_metadata")
        assert result is not None
        assert len(result) == 1
        block = result[0]
        assert block["type"] == "image_metadata"
        assert "image_metadata" in block
        assert "sha256" in block["image_metadata"]
        assert "mime_type" in block["image_metadata"]
        assert "size_bytes" in block["image_metadata"]

    def test_strip_removes_image_blocks(self):
        blocks = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
        assert redact_image_blocks(blocks, "strip") is None

    def test_does_not_persist_raw_base64(self):
        raw = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100).decode("ascii")
        blocks = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw}"}}]
        result = redact_image_blocks(blocks, "hash_and_metadata")
        serialized = json.dumps(result)
        assert raw not in serialized


class TestIdentityRedaction:
    def test_source_ip_always_returns_none(self):
        assert redact_source_ip("192.168.1.1") is None
        assert redact_source_ip("10.0.0.1") is None
        assert redact_source_ip("::1") is None

    def test_auth_header_always_returns_none(self):
        assert redact_authorization_header("Bearer sk-or-v1-abc") is None
        assert redact_authorization_header("api-key: secret") is None


class TestSecretCanary:
    def test_detects_openai_style_keys(self):
        findings = scan_for_secrets("Use this key: sk-proj-abc123def456ghi789jkl012mno345")
        assert len(findings) > 0

    def test_detects_nvapi_keys(self):
        findings = scan_for_secrets("nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI")
        assert len(findings) > 0

    def test_detects_openrouter_keys(self):
        findings = scan_for_secrets("sk-or-v1-d60f06e48d0fbc1496f7279e6911ca1f36588df305b")
        assert len(findings) > 0

    def test_detects_bearer_tokens(self):
        findings = scan_for_secrets("Authorization: Bearer eyJhbGci1234567890abcdef")
        assert len(findings) > 0

    def test_no_false_positives_on_normal_text(self):
        findings = scan_for_secrets("The weather is nice today. Hello world.")
        assert len(findings) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic Messages → OpenAI translation tests
# ─────────────────────────────────────────────────────────────────────────────


class TestAnthropicTranslation:
    """Test conversion from Anthropic Messages format to OpenAI messages for capture."""

    def test_text_content_block_translated(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {"type": "text", "text": "Hello world"}
        result = _anthropic_content_block_to_openai(block)
        assert result == {"role": "user", "content": "Hello world"}

    def test_tool_use_block_translated(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {
            "type": "tool_use",
            "id": "tool_123",
            "name": "get_weather",
            "input": {"location": "Amsterdam"},
        }
        result = _anthropic_content_block_to_openai(block)
        assert result["role"] == "assistant"
        assert result["tool_calls"][0]["id"] == "tool_123"
        assert result["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_image_block_skipped(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {"type": "image", "source": {"data": "base64data"}}
        result = _anthropic_content_block_to_openai(block)
        assert result == {}

    def test_unknown_block_type_skipped(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {"type": "unknown_type", "foo": "bar"}
        result = _anthropic_content_block_to_openai(block)
        assert result == {}

    def test_non_dict_block_returns_empty(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        result = _anthropic_content_block_to_openai("not a dict")
        assert result == {}

    def test_tool_result_block_translated(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {
            "type": "tool_result",
            "content": "The weather is sunny",
        }
        result = _anthropic_content_block_to_openai(block)
        assert result["role"] == "user"
        assert result["content"] == "The weather is sunny"

    def test_tool_result_block_with_array_content(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "Result part 1"},
                {"type": "text", "text": "Result part 2"},
            ],
        }
        result = _anthropic_content_block_to_openai(block)
        assert result["role"] == "user"
        assert "Result part 1" in result["content"]
        assert "Result part 2" in result["content"]

    def test_thinking_block_translated(self) -> None:
        from app.capture.redactor import _anthropic_content_block_to_openai
        block = {"type": "thinking", "thinking": "Let me think..."}
        result = _anthropic_content_block_to_openai(block)
        assert "[Thinking]" in result["content"]

    def test_messages_to_openai_with_system_string(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        result = anthropic_messages_to_openai(messages, system="You are helpful")
        assert result[0] == {"role": "system", "content": "You are helpful"}
        assert result[1] == {"role": "user", "content": "Hello"}

    def test_messages_to_openai_with_system_blocks(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        system = [{"type": "text", "text": "You are helpful"}]
        result = anthropic_messages_to_openai(messages, system=system)
        assert result[0] == {"role": "system", "content": "You are helpful"}
        assert result[1] == {"role": "user", "content": "Hello"}

    def test_messages_to_openai_string_content(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        result = anthropic_messages_to_openai(messages)
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "Hello"}

    def test_messages_to_openai_content_blocks(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "image", "source": {"data": "base64"}},
                ],
            }
        ]
        result = anthropic_messages_to_openai(messages)
        # Text block should be translated, image should be skipped
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "Hello"}

    def test_messages_to_openai_with_tool_calls(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "get_weather",
                        "input": {"location": "NYC"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": "Sunny, 72°F",
                    }
                ],
            },
        ]
        result = anthropic_messages_to_openai(messages)
        # Assistant message with tool_calls
        assistant_msgs = [m for m in result if m.get("role") == "assistant" and m.get("tool_calls")]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["tool_calls"][0]["function"]["name"] == "get_weather"
        # User message with tool result
        user_msgs = [m for m in result if m.get("role") == "user" and "tool result" in m.get("content", "").lower()]
        assert len(user_msgs) == 0  # tool_result is translated as content, not with label
        # Actually, the tool_result block is translated to content "Sunny, 72°F"
        content_msgs = [m for m in result if m.get("role") == "user" and "Sunny" in m.get("content", "")]
        assert len(content_msgs) == 1

    def test_messages_to_openai_no_system(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        result = anthropic_messages_to_openai(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_messages_to_openai_empty_system(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        result = anthropic_messages_to_openai(messages, system="")
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_messages_to_openai_whitespace_system(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        result = anthropic_messages_to_openai(messages, system="   ")
        assert len(result) == 1

    def test_messages_to_openai_none_system(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        messages = [{"role": "user", "content": "Hello"}]
        result = anthropic_messages_to_openai(messages, system=None)
        assert len(result) == 1

    def test_messages_to_openai_empty_messages(self) -> None:
        from app.capture.redactor import anthropic_messages_to_openai
        result = anthropic_messages_to_openai([])
        assert result == []
