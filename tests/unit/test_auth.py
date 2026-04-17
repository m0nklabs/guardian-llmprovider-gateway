"""Tests for app.proxy.auth — Bearer token authentication."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import SAMPLE_API_KEYS


# ── Helpers ────────────────────────────────────────────────────────────


def _load_auth_with_path(keys_path: Path):
    """Import auth module with API_KEYS_FILE patched."""
    import app.proxy.auth as auth_mod

    original = auth_mod.API_KEYS_FILE
    auth_mod.API_KEYS_FILE = keys_path
    return auth_mod, original


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
            auth.API_KEYS_FILE = orig

    def test_returns_empty_when_missing(self, tmp_path: Path):
        auth, orig = _load_auth_with_path(tmp_path / "nonexistent.json")
        try:
            keys = auth.load_api_keys()
            assert keys == {}
        finally:
            auth.API_KEYS_FILE = orig

    def test_returns_empty_on_corrupt_json(self, tmp_path: Path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid json")
        auth, orig = _load_auth_with_path(bad_file)
        try:
            keys = auth.load_api_keys()
            assert keys == {}
        finally:
            auth.API_KEYS_FILE = orig


# ── save_api_keys ──────────────────────────────────────────────────────


class TestSaveApiKeys:
    def test_writes_json(self, tmp_path: Path):
        out = tmp_path / "config" / "keys.json"
        auth, orig = _load_auth_with_path(out)
        try:
            auth.save_api_keys({"test_key": {"name": "tester", "created_at": 0, "metadata": {}}})
            assert out.exists()
            data = json.loads(out.read_text())
            assert "test_key" in data
            assert data["test_key"]["name"] == "tester"
        finally:
            auth.API_KEYS_FILE = orig

    def test_creates_parent_dirs(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "c" / "keys.json"
        auth, orig = _load_auth_with_path(deep)
        try:
            auth.save_api_keys({"k": {"name": "x"}})
            assert deep.exists()
        finally:
            auth.API_KEYS_FILE = orig


# ── generate_api_key ───────────────────────────────────────────────────


class TestGenerateApiKey:
    def test_key_has_prefix(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("new-client")
            assert key.startswith("flip_")
        finally:
            auth.API_KEYS_FILE = orig

    def test_key_length(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("new-client")
            # flip_ (5 chars) + 32 hex chars = 37
            assert len(key) == 37
        finally:
            auth.API_KEYS_FILE = orig

    def test_key_persisted(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            key = auth.generate_api_key("persisted-client", metadata={"env": "test"})
            stored = json.loads(api_keys_file.read_text())
            assert key in stored
            assert stored[key]["name"] == "persisted-client"
            assert stored[key]["metadata"]["env"] == "test"
        finally:
            auth.API_KEYS_FILE = orig

    def test_unique_keys(self, api_keys_file: Path):
        auth, orig = _load_auth_with_path(api_keys_file)
        try:
            k1 = auth.generate_api_key("a")
            k2 = auth.generate_api_key("b")
            assert k1 != k2
        finally:
            auth.API_KEYS_FILE = orig


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
            auth.API_KEYS_FILE = orig

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
            auth.API_KEYS_FILE = orig

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
            auth.API_KEYS_FILE = orig

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
            auth.API_KEYS_FILE = orig

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
            auth.API_KEYS_FILE = orig
