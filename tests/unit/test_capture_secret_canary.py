"""Secret canary tests — verify no credentials or sensitive data leak into capture files.

These tests exercise the full redaction pipeline and verify that known secret
patterns do not survive into written capture files.  They write actual JSONL
files to a temporary directory and scan them with :func:`scan_for_secrets`.

Reference: security invariant R1 from the capture plan — "Credentials or secrets
enter raw capture data."
"""

import gzip
import json
from typing import Any, Dict, List

import pytest

from app.capture.config import CaptureConfig
from app.capture.redactor import (
    redact_request_messages,
    redact_response_content,
    redact_request_parameters,
    redact_reasoning_content,
    redact_tool_results,
    redact_tool_calls,
    scan_for_secrets,
)


# ── Known secret values used as canaries ────────────────────────────────

SECRET_PATTERNS = [
    ("openai_key", "sk-proj-abc123def456ghi789jkl012mno345"),
    ("openrouter_key", "sk-or-v1-d60f06e48d0fbc1496f7279e6911ca1f36588df305b"),
    ("nvidia_key", "nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI"),
    ("poolside_key", "pool-testkey_abc123def456ghi789"),
    ("bearer_token", "Bearer eyJhbGci1234567890abcdef"),
    ("api_key_field", "api_key=AKIAIOSFODNN7EXAMPLE"),
    ("password_field", "password=s3cr3tp@ss"),
    ("secret_env", "${GUARDIAN_CAPTURE_CLIENT_REF_SECRET}"),
    ("raw_env_var", "${OPENAI_API_KEY}"),
    ("ip_address", "192.168.1.100"),
    ("ip_address_2", "10.0.0.1"),
    ("ipv4_in_text", "Connect to 172.16.254.1 for more info"),
]


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


@pytest.fixture
def capture_config():
    return CaptureConfig(
        enabled=True,
        local_capture=True,
        cloud_capture=False,
        instance_id="canary-test-instance",
        policy_version="1.0.0",
    )


class TestCanaryInRequestMessages:
    """Verify no secrets survive in redacted request messages."""

    def test_openai_key_in_user_message_is_redacted(self, field_policies):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "My API key is sk-proj-abc123def456ghi789jkl012mno345, please help."},
        ]
        result = redact_request_messages(messages, field_policies)
        # System messages stripped
        assert len(result) == 1
        # API key should be redacted in content
        content = result[0]["content"]
        findings = scan_for_secrets(content)
        assert len(findings) == 0, f"Secret found in redacted content: {findings}"

    def test_bearer_token_in_user_message_is_redacted(self, field_policies):
        messages = [
            {"role": "user", "content": "Use Authorization: Bearer eyJhbGci1234567890abcdef to access."},
        ]
        result = redact_request_messages(messages, field_policies)
        content = result[0]["content"]
        findings = scan_for_secrets(content)
        assert len(findings) == 0

    def test_nvapi_key_in_user_message_is_redacted(self, field_policies):
        messages = [
            {"role": "user", "content": "Set nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI as env."},
        ]
        result = redact_request_messages(messages, field_policies)
        content = result[0]["content"]
        findings = scan_for_secrets(content)
        assert len(findings) == 0

    def test_ip_address_in_user_message_is_redacted(self, field_policies):
        messages = [
            {"role": "user", "content": "My server is at 192.168.1.100 and 10.0.0.1."},
        ]
        result = redact_request_messages(messages, field_policies)
        content = result[0]["content"]
        # IP addresses in text content are flagged by scan_for_secrets
        findings = scan_for_secrets(content)
        # _redact_ip_in_text should have replaced IPs
        assert len(findings) == 0, f"IP found in redacted content: {findings}"

    def test_multiple_secrets_in_conversation(self, field_policies):
        messages = [
            {"role": "system", "content": "API_KEY=sk-proj-abc123def456ghi789jkl012mno345"},
            {"role": "user", "content": "NVDA key: nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI"},
            {"role": "assistant", "content": "Bearer token: eyJhbGci1234567890abcdef"},
        ]
        result = redact_request_messages(messages, field_policies)
        # System messages stripped
        for msg in result:
            content = msg.get("content", "")
            if isinstance(content, str):
                findings = scan_for_secrets(content)
                assert len(findings) == 0, f"Secret found: {findings}"
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        findings = scan_for_secrets(str(block.get("text", "")))
                        assert len(findings) == 0, f"Secret found in block: {findings}"

    def test_env_var_references_not_leaked(self, field_policies):
        messages = [
            {"role": "user", "content": "Use ${OPENAI_API_KEY} for auth"},
        ]
        result = redact_request_messages(messages, field_policies)
        content = result[0]["content"]
        findings = scan_for_secrets(content)
        assert len(findings) == 0


class TestCanaryInResponseContent:
    """Verify no secrets survive in redacted response content."""

    def test_openai_key_in_response_is_redacted(self):
        content = "Here is your key: sk-proj-abc123def456ghi789jkl012mno345"
        redacted = redact_response_content(content, "auto")
        findings = scan_for_secrets(redacted)
        assert len(findings) == 0

    def test_bearer_token_in_response_is_redacted(self):
        content = "Token: Bearer eyJhbGci1234567890abcdef"
        redacted = redact_response_content(content, "auto")
        findings = scan_for_secrets(redacted)
        assert len(findings) == 0

    def test_nvapi_key_in_response_is_redacted(self):
        content = "Use nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI"
        redacted = redact_response_content(content, "auto")
        findings = scan_for_secrets(redacted)
        assert len(findings) == 0


class TestCanaryInToolCalls:
    """Verify no secrets survive in redacted tool calls."""

    def test_api_key_in_tool_call_arguments(self, field_policies):
        tool_calls = [
            {"id": "call_1", "type": "function",
             "function": {"name": "set_config", "arguments": '{"key": "sk-proj-abc123def456ghi789jkl012mno345"}'}}
        ]
        result = redact_tool_calls(tool_calls, "capture")
        for tc in result:
            for arg_str in [tc.get("function", {}).get("arguments", "")]:
                if isinstance(arg_str, str):
                    findings = scan_for_secrets(arg_str)
                    assert len(findings) == 0, f"Secret found in tool call: {findings}"

    def test_api_key_in_tool_call_name(self, field_policies):
        tool_calls = [
            {"id": "call_1", "type": "function",
             "function": {"name": "sk-proj-abc123def456ghi789jkl012mno345", "arguments": "{}"}}
        ]
        result = redact_tool_calls(tool_calls, "capture")
        for tc in result:
            name = tc.get("function", {}).get("name", "")
            findings = scan_for_secrets(name)
            assert len(findings) == 0, f"Secret found in tool call name: {findings}"


class TestCanaryInToolResults:
    """Verify no secrets survive in redacted tool results."""

    def test_api_key_in_tool_results_is_stripped(self, field_policies):
        tool_results = [
            {"tool_call_id": "call_1", "content": "API key is sk-proj-abc123def456ghi789jkl012mno345"}
        ]
        result = redact_tool_results(tool_results, "strip")
        # When stripped, tool results should be None or empty
        if result is not None:
            for tr in result:
                content = tr.get("content", "")
                if isinstance(content, str):
                    findings = scan_for_secrets(content)
                    assert len(findings) == 0


class TestCanaryInReasoning:
    """Verify no secrets survive in redacted reasoning content."""

    def test_api_key_in_reasoning_is_stripped(self):
        reasoning = "Let me think about sk-proj-abc123def456ghi789jkl012mno345"
        result = redact_reasoning_content(reasoning, "strip")
        assert result is None

    def test_api_key_in_reasoning_is_captured_and_redacted(self):
        reasoning = "Let me think about sk-proj-abc123def456ghi789jkl012mno345"
        result = redact_reasoning_content(reasoning, "capture")
        if result is not None:
            findings = scan_for_secrets(result)
            assert len(findings) == 0


class TestCanaryInRequestParameters:
    """Verify no secrets survive in redacted request parameters."""

    def test_api_key_in_request_parameters(self, field_policies):
        parameters = {
            "model": "gpt-4",
            "api_key": "sk-proj-abc123def456ghi789jkl012mno345",
            "max_tokens": 100,
        }
        result = redact_request_parameters(parameters, field_policies)
        # api_key field is structurally redacted
        assert result.get("api_key") == "[REDACTED]"
        for value in str(result.values()):
            findings = scan_for_secrets(value)
            assert len(findings) == 0


class TestCanaryInWrittenFiles:
    """End-to-end canary: write redacted events to JSONL files and scan them.

    This is the most important canary test — it verifies that the complete
    pipeline (redaction → serialization → JSONL write → file read → scan)
    does not leak any secrets.
    """

    @pytest.fixture
    def field_policies_full(self):
        return {
            "system_prompts": "strip",
            "reasoning": "strip",
            "tool_definitions": "capture",
            "tool_calls": "capture",
            "tool_results": "strip",
            "images": "hash_and_metadata",
            "unknown_content_blocks": "strip",
        }

    def _build_and_redact_event(
        self,
        field_policies: Dict[str, str],
        secret_laden_messages: List[Dict[str, Any]],
        response_content: str,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        reasoning: str,
    ) -> Dict[str, Any]:
        """Build a capture event with redacted content."""
        redacted_messages = redact_request_messages(secret_laden_messages, field_policies)
        redacted_response = redact_response_content(response_content, "auto")
        redacted_tool_calls = redact_tool_calls(tool_calls, field_policies.get("tool_calls", "capture"))
        redacted_tool_results = redact_tool_results(tool_results, field_policies.get("tool_results", "strip"))
        redacted_reasoning = redact_reasoning_content(reasoning, field_policies.get("reasoning", "strip"))

        event = {
            "schema_name": "guardian_capture_v1",
            "schema_version": "1.0.0",
            "event_type": "request_completed",
            "request_id": "test-req-001",
            "sequence": 1,
            "timestamp_utc": "2026-08-05T00:00:00Z",
            "guardian_instance_id": "canary-test-instance",
            "client_ref": "a" * 64,
            "endpoint": "/v1/chat/completions",
            "ingress_protocol": "openai",
            "route_type": "local",
            "requested_model": "gpt-4",
            "capture_policy_version": "1.0.0",
            "request_messages": redacted_messages,
            "response_content": redacted_response,
            "tool_calls": redacted_tool_calls,
            "tool_results": redacted_tool_results,
            "reasoning_content": redacted_reasoning,
        }
        return event

    def test_no_secrets_in_written_jsonl(self, field_policies_full, tmp_path):
        """Write redacted events to a JSONL file and scan for secret leakage."""
        # Prepare secret-laden test data
        secret_messages = [
            {"role": "system", "content": "API_KEY=sk-proj-abc123def456ghi789jkl012mno345"},
            {"role": "user", "content": "Use nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI"},
            {"role": "assistant", "content": "Bearer: Bearer eyJhbGci1234567890abcdef"},
        ]
        secret_response = "Here is your sk-or-v1-d60f06e48d0fbc1496f7279e6911ca1f36588df305b key"
        secret_tool_calls = [
            {"id": "call_1", "type": "function",
             "function": {"name": "set_config", "arguments": '{"key": "pool-testkey_abc123def456ghi789"}'}}
        ]
        secret_tool_results = [
            {"tool_call_id": "call_1", "content": "sk-proj-abc123def456ghi789jkl012mno345"}
        ]
        secret_reasoning = "Thinking: nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI"

        event = self._build_and_redact_event(
            field_policies_full, secret_messages, secret_response,
            secret_tool_calls, secret_tool_results, secret_reasoning
        )

        # Write to JSONL file
        jsonl_path = tmp_path / "capture_test.jsonl"
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(event, separators=(",", ":"), sort_keys=False, default=str) + "\n")

        # Read back and scan
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                findings = scan_for_secrets(line)
                assert len(findings) == 0, (
                    f"Secret canary failed! Secrets found in JSONL line: {findings}\n"
                    f"Line: {line[:200]}"
                )

    def test_no_secrets_in_compressed_jsonl(self, field_policies_full, tmp_path):
        """Write redacted events to a gzipped JSONL file and scan for secret leakage."""
        secret_messages = [
            {"role": "user", "content": "sk-proj-abc123def456ghi789jkl012mno345 and BEARER=Bearer eyJhbGci1234567890abcdef"},
        ]
        secret_response = "Key: sk-or-v1-d60f06e48d0fbc1496f7279e6911ca1f36588df305b"
        secret_tool_calls = [
            {"id": "call_1", "type": "function",
             "function": {"name": "nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI", "arguments": "{}"}}
        ]
        event = self._build_and_redact_event(
            field_policies_full, secret_messages, secret_response,
            secret_tool_calls, [], "password=s3cr3tp@ss"
        )

        # Write to gzipped JSONL file (simulating rotation)
        gz_path = tmp_path / "capture_test.jsonl.gz"
        with gzip.open(gz_path, "wt") as f:
            f.write(json.dumps(event, separators=(",", ":"), sort_keys=False, default=str) + "\n")

        # Read back and scan
        with gzip.open(gz_path, "rt") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                findings = scan_for_secrets(line)
                assert len(findings) == 0, (
                    f"Secret canary failed in gzipped file! Secrets found: {findings}\n"
                    f"Line: {line[:200]}"
                )

    def test_no_secrets_in_checksum_file(self, field_policies_full, tmp_path):
        """Verify SHA-256 checksum files don't contain secrets."""
        secret_messages = [
            {"role": "user", "content": "My key is sk-proj-abc123def456ghi789jkl012mno345"},
        ]
        event = self._build_and_redact_event(
            field_policies_full, secret_messages, "Hello", [], [], ""
        )

        # Simulate checksum file containing event data (as some implementations do)
        jsonl_path = tmp_path / "capture_test.jsonl"
        content = json.dumps(event, separators=(",", ":"), sort_keys=False, default=str) + "\n"
        with open(jsonl_path, "w") as f:
            f.write(content)

        # Compute checksum (simulating what wal_writer does)
        import hashlib
        checksum = hashlib.sha256(content.encode()).hexdigest()

        # Write checksum file
        checksum_path = tmp_path / "capture_test.jsonl.sha256"
        with open(checksum_path, "w") as f:
            f.write(checksum)

        # Both files should be clean
        for path in [jsonl_path, checksum_path]:
            with open(path, "r") as f:
                content = f.read()
                findings = scan_for_secrets(content)
                assert len(findings) == 0, (
                    f"Secret canary failed in {path.name}! Secrets found: {findings}"
                )


class TestCanaryNoFalseNegatives:
    """Verify that scan_for_secrets actually detects the secrets we use as canaries.

    If this test fails, it means our canary secrets aren't being detected
    by the scanner, which would make the canary test worthless.
    """

    def test_scanner_detects_openai_key(self):
        findings = scan_for_secrets("sk-proj-abc123def456ghi789jkl012mno345")
        assert len(findings) > 0, "Scanner should detect OpenAI key"

    def test_scanner_detects_openrouter_key(self):
        findings = scan_for_secrets("sk-or-v1-d60f06e48d0fbc1496f7279e6911ca1f36588df305b")
        assert len(findings) > 0

    def test_scanner_detects_nvapi_key(self):
        findings = scan_for_secrets("nvapi-0KeCv_xZdXTmKxVrv4kUN9OVaSX6TapUGclFUtS2wcI")
        assert len(findings) > 0

    def test_scanner_detects_bearer_token(self):
        findings = scan_for_secrets("Bearer eyJhbGci1234567890abcdef")
        assert len(findings) > 0

    def test_scanner_detects_api_key_header(self):
        findings = scan_for_secrets("api_key=AKIAIOSFODNN7EXAMPLE")
        assert len(findings) > 0

    def test_scanner_detects_password(self):
        findings = scan_for_secrets("password=s3cr3tp@ss")
        assert len(findings) > 0

    def test_scanner_detects_raw_ip(self):
        findings = scan_for_secrets("192.168.1.100")
        assert len(findings) > 0