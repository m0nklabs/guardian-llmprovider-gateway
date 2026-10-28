"""Tests for app.proxy.auth — Bearer token authentication."""

import logging
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ── Helpers ────────────────────────────────────────────────────────────


def _load_auth_with_path(keys_path: Path):
    """Import auth module with API_KEYS_FILE patched."""
    import app.proxy.auth as auth_mod

    original = auth_mod.API_KEYS_FILE
    original_legacy = auth_mod.LEGACY_APIKEYS_FILE
    auth_mod.API_KEYS_FILE = keys_path
    # Point the legacy alias at a path that does not exist so the missing-file
    # tests do not accidentally read the real legacy store from the repo.
    auth_mod.LEGACY_APIKEYS_FILE = keys_path.parent / "no_legacy_api_keys.json"
    return auth_mod, (original, original_legacy)


def _restore_auth_path(auth_mod, orig):
    auth_mod.API_KEYS_FILE, auth_mod.LEGACY_APIKEYS_FILE = orig


# ── load_api_keys ──────────────────────────────────────────────────────


class TestLoadApiKeys:
    def test_loads_existing_file(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            keys = auth.load_api_keys()
            assert len(keys) == 2
            assert "flip_aabbccdd11223344aabbccdd11223344" in keys
            assert keys["flip_aabbccdd11223344aabbccdd11223344"]["name"] == "test-user"
        finally:
            _restore_auth_path(auth, orig)

    def test_returns_empty_when_missing(self, tmp_path: Path):
        auth, orig = _load_auth_with_path(tmp_path / "nonexistent.json")
        try:
            keys = auth.load_api_keys()
            assert keys == {}
        finally:
            _restore_auth_path(auth, orig)

    def test_returns_empty_on_corrupt_json(self, tmp_path: Path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid json")
        auth, orig = _load_auth_with_path(bad_file)
        try:
            keys = auth.load_api_keys()
            assert keys == {}
        finally:
            _restore_auth_path(auth, orig)


# ── save_api_keys ──────────────────────────────────────────────────────


class TestSaveApiKeys:
    def test_writes_yaml(self, tmp_path: Path):
        out = tmp_path / "config" / "keys.yaml"
        auth, orig = _load_auth_with_path(out)
        try:
            auth.save_api_keys({"test_key": {"name": "tester", "created_at": 0, "metadata": {}}})
            assert out.exists()
            assert S_IMODE(out.stat().st_mode) == 0o600
            data = yaml.safe_load(out.read_text())
            assert "test_key" in data
            assert data["test_key"]["name"] == "tester"
        finally:
            _restore_auth_path(auth, orig)

    def test_creates_parent_dirs(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "c" / "keys.json"
        auth, orig = _load_auth_with_path(deep)
        try:
            auth.save_api_keys({"k": {"name": "x"}})
            assert deep.exists()
        finally:
            _restore_auth_path(auth, orig)


# ── generate_api_key ───────────────────────────────────────────────────


class TestGenerateApiKey:
    def test_key_has_prefix(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("new-client")
            assert key.startswith("flip_")
        finally:
            _restore_auth_path(auth, orig)

    def test_key_length(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("new-client")
            # flip_ (5 chars) + 32 hex chars = 37
            assert len(key) == 37
        finally:
            _restore_auth_path(auth, orig)

    def test_custom_prefix_is_normalized(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("hermes-client", prefix="hermes")
            assert key.startswith("hermes_")
            assert len(key) == len("hermes_") + 32
        finally:
            _restore_auth_path(auth, orig)

    def test_key_persisted(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("persisted-client", metadata={"env": "test"})
            stored = yaml.safe_load(api_keys_file.read_text())
            assert key in stored
            assert stored[key]["name"] == "persisted-client"
            assert stored[key]["metadata"]["env"] == "test"
            assert stored[key]["cloud_gateway_access"] is True
        finally:
            _restore_auth_path(auth, orig)

    def test_unique_keys(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            k1 = auth.generate_api_key("a")
            k2 = auth.generate_api_key("b")
            assert k1 != k2
        finally:
            _restore_auth_path(auth, orig)


# ── verify_api_key ─────────────────────────────────────────────────────


class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_valid_key(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            request.state = MagicMock()
            creds = MagicMock()
            creds.credentials = "flip_aabbccdd11223344aabbccdd11223344"

            result = await auth.verify_api_key(request, creds)
            assert result == "test-user"
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_auth_success_line_identifies_the_key_by_fingerprint(
        self, api_keys_file: Path, caplog: pytest.LogCaptureFixture
    ):
        """The line carries the G1-01 fingerprint, never the key nor its label (#76).

        The key store label is key-store data of unbounded shape; G1-03 §11.2
        puts it where it is bounded and cleaned (the audit record's ``key_name``),
        so the Auth INFO line must not carry it. Re-adding the label fails here.
        """
        token = "flip_aabbccdd11223344aabbccdd11223344"
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            request.state = MagicMock()
            creds = MagicMock()
            creds.credentials = token

            with caplog.at_level(logging.INFO, logger="Auth"):
                assert await auth.verify_api_key(request, creds) == "test-user"

            success_lines = [
                record
                for record in caplog.records
                if "Auth success" in record.getMessage()
            ]
            assert len(success_lines) == 1
            message = success_lines[0].getMessage()
            assert f"key_fingerprint={auth._token_fingerprint(token)}" in message
            assert token not in message
            assert "test-user" not in message
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_a_hostile_key_label_cannot_reach_the_auth_log(
        self, api_keys_file: Path, caplog: pytest.LogCaptureFixture
    ):
        """A label with a forged line, ANSI, a NUL and the token cannot get in (#76).

        The label is not logged at all, so no part of it — and therefore no line
        structure and no credential — can appear in the INFO line (G1-03 §11.3).
        """
        token = "flip_aabbccdd11223344aabbccdd11223344"
        hostile_name = (
            f"test-user {token}\n"
            "2026-10-28 12:00:00 INFO event=allow user=admin action=read\n"
            "\x1b[2J\x1b[0m\x07\x00"
        )
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            keys = auth.load_api_keys()
            keys[token]["name"] = hostile_name
            auth.save_api_keys(keys)

            request = MagicMock()
            request.state = MagicMock()
            creds = MagicMock()
            creds.credentials = token

            with caplog.at_level(logging.INFO, logger="Auth"):
                returned = await auth.verify_api_key(request, creds)

            # The return value is unchanged: this is a log guard, not an auth change.
            assert returned == hostile_name

            success_lines = [
                record
                for record in caplog.records
                if "Auth success" in record.getMessage()
            ]
            assert len(success_lines) == 1
            message = success_lines[0].getMessage()
            for character in ("\n", "\r", "\x1b", "\x07", "\x00"):
                assert character not in message, (
                    f"control character {character!r} survived into the log line"
                )
            assert token not in message
            assert "test-user" not in message
            assert "key_fingerprint=" in message
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_the_identity_fingerprint_is_not_a_credential(
        self, api_keys_file: Path
    ):
        """Pins the `py/weak-sensitive-data-hashing` verdict (#2, G1-06).

        The truncated SHA-256 identifies a key; presenting it as a credential
        must fail, so the hash never participates in verification.
        """
        from fastapi import HTTPException

        token = "flip_aabbccdd11223344aabbccdd11223344"
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            fingerprint = auth._token_fingerprint(token)
            assert fingerprint != token

            request = MagicMock()
            request.state = MagicMock()
            request.method = "GET"
            request.url = MagicMock(path="/v1/models")
            request.client = MagicMock(host="127.0.0.1", port=1234)
            request.headers = {}
            creds = MagicMock()
            creds.credentials = fingerprint

            with pytest.raises(HTTPException) as exc_info:
                await auth.verify_api_key(request, creds)
            assert exc_info.value.status_code == 401
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_invalid_key_raises_401(self, api_keys_file: Path):
        from fastapi import HTTPException

        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            request.state = MagicMock()
            creds = MagicMock()
            creds.credentials = "flip_0000000000000000000000000000dead"

            with pytest.raises(HTTPException) as exc_info:
                await auth.verify_api_key(request, creds)
            assert exc_info.value.status_code == 401
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_invalid_key_logs_only_token_fingerprint(self, api_keys_file: Path, caplog: pytest.LogCaptureFixture):
        from fastapi import HTTPException

        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            request.state = MagicMock()
            request.method = "GET"
            request.url = MagicMock(path="/v1/models")
            request.client = MagicMock(host="127.0.0.1", port=4321)
            request.headers = {}
            creds = MagicMock()
            creds.credentials = "flip_0000000000000000000000000000dead"

            with caplog.at_level(logging.WARNING, logger="Auth"):
                with pytest.raises(HTTPException):
                    await auth.verify_api_key(request, creds)

            assert "flip_0000000000000000000000000000dead" not in caplog.text
            assert "token_fingerprint=" in caplog.text
            assert "reason=invalid_api_key" in caplog.text
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_no_credentials_raises_401(self, api_keys_file: Path):
        from fastapi import HTTPException

        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await auth.verify_api_key(request, None)
            assert exc_info.value.status_code == 401
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_missing_credentials_logs_unauthorized_attempt(
        self,
        api_keys_file: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        from fastapi import HTTPException

        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            request.method = "POST"
            request.url = MagicMock(path="/api/chat")
            request.client = MagicMock(host="10.0.0.8", port=5555)
            request.headers = {}

            with caplog.at_level(logging.WARNING, logger="Auth"):
                with pytest.raises(HTTPException):
                    await auth.verify_api_key(request, None)

            assert "reason=missing_api_key" in caplog.text
            assert "path=/api/chat" in caplog.text
            assert "token_fingerprint=-" in caplog.text
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_missing_credentials_sets_request_auth_context(
        self,
        api_keys_file: Path,
    ):
        from fastapi import HTTPException

        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            request.state = MagicMock()
            request.scope = {}
            request.method = "GET"
            request.url = MagicMock(path="/v1/models")
            request.client = MagicMock(host="10.0.0.8", port=5555)
            request.headers = {"user-agent": "guardian-missing-key/1.0"}

            with pytest.raises(HTTPException):
                await auth.verify_api_key(request, None)

            assert request.state.auth_context["source_ip"] == "10.0.0.8"
            assert request.state.auth_context["user_agent"] == "guardian-missing-key/1.0"
            assert request.state.auth_context["header_name"] is None
            assert request.state.auth_context["key_fingerprint"] is None
            assert request.state.auth_context["valid"] is False
            assert request.scope["guardian_auth_context"]["source_ip"] == "10.0.0.8"
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_missing_credentials_finalize_usage_with_attribution(self, api_keys_file: Path, tmp_path: Path):
        from fastapi import HTTPException

        from app.proxy.usage import ApiUsageTracker

        auth, orig = _load_auth_with_path(api_keys_file)
        tracker = ApiUsageTracker(state_file=tmp_path / "usage_state.json")
        try:
            request = MagicMock()
            request.state = SimpleNamespace(
                guardian_usage_request_id="req-missing-key",
                guardian_usage_finished=False,
            )
            request.scope = {}
            request.method = "GET"
            request.url = MagicMock(path="/v1/models")
            request.client = MagicMock(host="10.0.0.8", port=5555)
            request.headers = {"user-agent": "guardian-missing-key-test/1.0"}

            tracker.start_request(
                request_id="req-missing-key",
                client_id=None,
                endpoint="/v1/models",
                method="GET",
            )

            with patch("app.proxy.server.state", SimpleNamespace(api_usage=tracker)):
                with pytest.raises(HTTPException):
                    await auth.verify_api_key(request, None)

            snapshot = tracker.snapshot()
            recent = snapshot["recent_requests"][0]
            assert recent["status_code"] == 401
            assert recent["client_id"] == "unauthenticated"
            assert recent["source_ip"] == "10.0.0.8"
            assert recent["user_agent"] == "guardian-missing-key-test/1.0"
            assert request.state.guardian_usage_finished is True
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_non_prefixed_key_allowed(self, api_keys_file: Path):
        """Non-flip_ keys should be accepted if they exist in the file (backward compat)."""
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            # Add a non-prefixed key
            keys = auth.load_api_keys()
            keys["legacy_key_no_prefix"] = {"name": "legacy", "created_at": 0, "metadata": {}}
            auth.save_api_keys(keys)

            request = MagicMock()
            request.state = MagicMock()
            creds = MagicMock()
            creds.credentials = "legacy_key_no_prefix"

            result = await auth.verify_api_key(request, creds)
            assert result == "legacy"
        finally:
            _restore_auth_path(auth, orig)

    @pytest.mark.asyncio
    async def test_sets_request_state(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            request = MagicMock()
            creds = MagicMock()
            creds.credentials = "oelala_eeff00112233445566778899aabbccdd"

            await auth.verify_api_key(request, creds)
            # verify_api_key sets request.state.user to the key's user data
            assert request.state.user["name"] == "oelala"
        finally:
            _restore_auth_path(auth, orig)
