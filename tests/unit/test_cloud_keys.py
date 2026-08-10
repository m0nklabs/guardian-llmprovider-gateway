"""Unit tests for app.proxy.cloud_keys — per-key cloud credential store."""

import json
import time
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch

import pytest

from app.proxy.cloud_keys import (
    CloudCredential,
    CloudCredentialStore,
    mask_api_key,
    parse_guardian_route,
)


# ── Helper fixtures ───────────────────────────────────────────────────


@pytest.fixture
def tmp_store(tmp_path: Path) -> CloudCredentialStore:
    """Create a CloudCredentialStore with a temp cloud_keys.json file."""
    return CloudCredentialStore(path=tmp_path / "cloud_keys.json")


# ── mask_api_key ───────────────────────────────────────────────────────


class TestMaskApiKey:
    def test_long_key(self):
        assert mask_api_key("nvapi-1234567890abcdef") == "nvapi-12****cdef"

    def test_short_key(self):
        assert mask_api_key("short") == "sh****rt"

    def test_empty_key(self):
        assert mask_api_key("") == ""

    def test_none_key(self):
        assert mask_api_key(None) == ""


# ── parse_guardian_route ──────────────────────────────────────────────


class TestParseGuardianRoute:
    def test_simple_route(self):
        assert parse_guardian_route("guardian/nvidia/minimax/minimax-m3") == ("nvidia", "minimax/minimax-m3")

    def test_openrouter_route(self):
        assert parse_guardian_route("guardian/openrouter/openai/gpt-4o") == ("openrouter", "openai/gpt-4o")

    def test_single_segment_model(self):
        assert parse_guardian_route("guardian/nvidia/llama-3.1") == ("nvidia", "llama-3.1")

    def test_non_guardian_route(self):
        assert parse_guardian_route("openai/gpt-4o") is None

    def test_empty_string(self):
        assert parse_guardian_route("") is None

    def test_bare_guardian(self):
        assert parse_guardian_route("guardian") is None

    def test_guardian_provider_only(self):
        assert parse_guardian_route("guardian/nvidia") is None


# ── CloudCredential dataclass ─────────────────────────────────────────


class TestCloudCredential:
    def test_from_dict(self):
        raw = {
            "provider": "nvidia",
            "name": "Test",
            "api_key": "nvapi-xxx",
            "created_at": 1234567890.0,
            "models": ["minimax/minimax-m3"],
        }
        cred = CloudCredential.from_dict("cred_001", raw)
        assert cred.id == "cred_001"
        assert cred.provider == "nvidia"
        assert cred.api_key == "nvapi-xxx"
        assert cred.models == ["minimax/minimax-m3"]

    def test_from_dict_defaults(self):
        cred = CloudCredential.from_dict("cred_002", {"provider": "openrouter", "name": "T", "api_key": "k"})
        assert cred.models == []
        assert cred.created_at == 0.0

    def test_to_dict(self):
        cred = CloudCredential(id="c1", provider="nvidia", name="T", api_key="k", created_at=1.0, models=["m1"])
        d = cred.to_dict()
        assert d["provider"] == "nvidia"
        assert d["models"] == ["m1"]

    def test_to_masked_dict(self):
        cred = CloudCredential(
            id="c1",
            provider="nvidia",
            name="T",
            api_key="nvapi-1234567890abcdef",
            created_at=1.0,
            models=[],
            owner_key_fingerprint="owner-key",
        )
        d = cred.to_masked_dict()
        assert "****" in d["api_key"]
        assert d["api_key"] != "nvapi-1234567890abcdef"
        assert "owner_key_fingerprint" not in d


# ── CloudCredentialStore lifecycle ────────────────────────────────────


class TestCredentialStoreLifecycle:
    @pytest.mark.asyncio
    async def test_add_credential(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential(
            provider="nvidia",
            name="NVIDIA Default",
            api_key="nvapi-test123",
            models=["minimax/minimax-m3"],
        )
        assert cred["provider"] == "nvidia"
        assert cred["name"] == "NVIDIA Default"
        assert "****" in cred["api_key"]  # masked
        assert cred["id"].startswith("cred_")

    @pytest.mark.asyncio
    async def test_list_credentials_masks_keys(self, tmp_store: CloudCredentialStore):
        await tmp_store.add_credential("nvidia", "Test", "nvapi-secretkey1234", ["m1"])
        creds = tmp_store.list_credentials()
        assert len(creds) == 1
        assert "****" in creds[0]["api_key"]
        assert "nvapi-secretkey1234" not in creds[0]["api_key"]

    @pytest.mark.asyncio
    async def test_delete_credential(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", [])
        deleted = await tmp_store.delete_credential(cred["id"])
        assert deleted is True
        assert len(tmp_store.list_credentials()) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_credential(self, tmp_store: CloudCredentialStore):
        deleted = await tmp_store.delete_credential("nonexistent")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_delete_credential_removes_links(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", ["m1"])
        await tmp_store.link_credential("key_fp_1", "nvidia", cred["id"])
        await tmp_store.delete_credential(cred["id"])
        links = tmp_store.list_links()
        assert "key_fp_1" not in links or "nvidia" not in links.get("key_fp_1", {})


# ── Model management ──────────────────────────────────────────────────


class TestModelManagement:
    @pytest.mark.asyncio
    async def test_add_model_to_credential(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", [])
        added = await tmp_store.add_model_to_credential(cred["id"], "minimax/minimax-m3")
        assert added is True
        creds = tmp_store.list_credentials()
        assert "minimax/minimax-m3" in creds[0]["models"]

    @pytest.mark.asyncio
    async def test_add_duplicate_model(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", ["m1"])
        added = await tmp_store.add_model_to_credential(cred["id"], "m1")
        assert added is False  # already present

    @pytest.mark.asyncio
    async def test_remove_model_from_credential(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", ["m1", "m2"])
        removed = await tmp_store.remove_model_from_credential(cred["id"], "m1")
        assert removed is True
        creds = tmp_store.list_credentials()
        assert "m1" not in creds[0]["models"]
        assert "m2" in creds[0]["models"]

    @pytest.mark.asyncio
    async def test_replace_models_for_credential_replaces_entire_catalog(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("google", "Google AI Studio", "google-key", ["old-model"])

        replaced = await tmp_store.replace_models_for_credential(
            cred["id"],
            ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash", "  "],
        )

        assert replaced is True
        stored = tmp_store.get_credential_by_id(cred["id"])
        assert stored is not None
        assert stored.models == ["gemini-2.5-flash", "gemini-2.5-pro"]

    @pytest.mark.asyncio
    async def test_replace_models_for_missing_credential_returns_false(self, tmp_store: CloudCredentialStore):
        replaced = await tmp_store.replace_models_for_credential("missing", ["gemini-2.5-flash"])

        assert replaced is False

    @pytest.mark.asyncio
    async def test_replace_models_restores_memory_when_persistence_fails(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("google", "Google AI Studio", "google-key", ["old-model"])

        with patch.object(tmp_store, "_save", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                await tmp_store.replace_models_for_credential(cred["id"], ["gemini-2.5-flash"])

        stored = tmp_store.get_credential_by_id(cred["id"])
        assert stored is not None
        assert stored.models == ["old-model"]

    @pytest.mark.asyncio
    async def test_save_secures_temporary_file_before_writing_credentials(self, tmp_store: CloudCredentialStore):
        original_dump = json.dump
        tmp_path = tmp_store._path.with_suffix(tmp_store._path.suffix + ".tmp")

        def assert_secure_dump(data, stream, **kwargs):
            assert S_IMODE(tmp_path.stat().st_mode) == 0o600
            return original_dump(data, stream, **kwargs)

        with patch("app.proxy.cloud_keys.json.dump", side_effect=assert_secure_dump):
            await tmp_store.add_credential("google", "Google AI Studio", "google-key", ["gemini-2.5-flash"])


# ── Linking ───────────────────────────────────────────────────────────


class TestLinking:
    @pytest.mark.asyncio
    async def test_link_credential(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", ["m1"])
        linked = await tmp_store.link_credential("key_fp_1", "nvidia", cred["id"])
        assert linked is True
        links = tmp_store.list_links()
        assert links["key_fp_1"]["nvidia"] == cred["id"]

    @pytest.mark.asyncio
    async def test_link_nonexistent_credential(self, tmp_store: CloudCredentialStore):
        linked = await tmp_store.link_credential("key_fp_1", "nvidia", "nonexistent")
        assert linked is False

    @pytest.mark.asyncio
    async def test_link_credential_rejects_provider_mismatch(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("google", "Google AI Studio", "google-key", ["gemini-2.5-flash"])

        linked = await tmp_store.link_credential("key_fp_1", "nvidia", cred["id"])

        assert linked is False
        assert tmp_store.list_links() == {}

    @pytest.mark.asyncio
    async def test_linking_legacy_credential_preserves_its_inferred_owner(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("google", "Google AI Studio", "google-key", ["gemini-2.5-flash"])
        tmp_store._data["links"] = {"legacy-owner": {"google": cred["id"]}}

        linked = await tmp_store.link_credential("shared-key", "google", cred["id"])

        assert linked is True
        assert tmp_store.is_credential_owned_by(cred["id"], "legacy-owner") is True
        assert tmp_store.is_credential_owned_by(cred["id"], "shared-key") is False

    @pytest.mark.asyncio
    async def test_unlink_credential(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-key", ["m1"])
        await tmp_store.link_credential("key_fp_1", "nvidia", cred["id"])
        unlinked = await tmp_store.unlink_credential("key_fp_1", "nvidia")
        assert unlinked is True
        links = tmp_store.list_links()
        assert "nvidia" not in links.get("key_fp_1", {})

    @pytest.mark.asyncio
    async def test_unlink_nonexistent(self, tmp_store: CloudCredentialStore):
        unlinked = await tmp_store.unlink_credential("nonexistent", "nvidia")
        assert unlinked is False


# ── Per-key model lookup ─────────────────────────────────────────────


class TestPerKeyLookup:
    @pytest.mark.asyncio
    async def test_get_credential_for_key(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "Test", "nvapi-secret", ["m1"])
        await tmp_store.link_credential("key_fp_1", "nvidia", cred["id"])
        result = tmp_store.get_credential_for_key("key_fp_1", "nvidia")
        assert result is not None
        assert result.api_key == "nvapi-secret"  # unmasked for forwarding

    @pytest.mark.asyncio
    async def test_get_credential_no_link(self, tmp_store: CloudCredentialStore):
        result = tmp_store.get_credential_for_key("nonexistent", "nvidia")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_credential_by_id(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("google", "Google AI Studio", "google-key", ["gemini-2.5-flash"])

        result = tmp_store.get_credential_by_id(cred["id"])

        assert result is not None
        assert result.provider == "google"
        assert result.api_key == "google-key"

    @pytest.mark.asyncio
    async def test_credential_owner_can_be_checked_without_exposing_it(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential(
            "google",
            "Google AI Studio",
            "google-key",
            ["gemini-2.5-flash"],
            owner_key_fingerprint="owner-key",
        )

        assert tmp_store.is_credential_owned_by(cred["id"], "owner-key") is True
        assert tmp_store.is_credential_owned_by(cred["id"], "other-key") is False
        assert [item["id"] for item in tmp_store.list_credentials_for_owner("owner-key")] == [cred["id"]]
        assert tmp_store.list_credentials_for_owner("other-key") == []

    @pytest.mark.asyncio
    async def test_get_linked_models_for_key(self, tmp_store: CloudCredentialStore):
        cred = await tmp_store.add_credential("nvidia", "NVIDIA", "nvapi-key", ["minimax/minimax-m3", "deepseek-ai/deepseek-r1"])
        cred2 = await tmp_store.add_credential("openrouter", "OpenRouter", "sk-or-key", ["openai/gpt-4o"])
        await tmp_store.link_credential("key_fp_1", "nvidia", cred["id"])
        await tmp_store.link_credential("key_fp_1", "openrouter", cred2["id"])

        models = tmp_store.get_linked_models_for_key("key_fp_1")
        assert len(models) == 3

        ids = [m["id"] for m in models]
        assert "guardian/nvidia/minimax/minimax-m3" in ids
        assert "guardian/nvidia/deepseek-ai/deepseek-r1" in ids
        assert "guardian/openrouter/openai/gpt-4o" in ids

    @pytest.mark.asyncio
    async def test_get_linked_models_no_links(self, tmp_store: CloudCredentialStore):
        models = tmp_store.get_linked_models_for_key("nonexistent")
        assert models == []


# ── Persistence ────────────────────────────────────────────────────────


class TestPersistence:
    @pytest.mark.asyncio
    async def test_reload_picks_up_changes(self, tmp_path: Path):
        store1 = CloudCredentialStore(path=tmp_path / "cloud_keys.json")
        await store1.add_credential("nvidia", "Test", "nvapi-key", ["m1"])

        store2 = CloudCredentialStore(path=tmp_path / "cloud_keys.json")
        creds = store2.list_credentials()
        assert len(creds) == 1
        assert creds[0]["name"] == "Test"

    @pytest.mark.asyncio
    async def test_creates_file_if_missing(self, tmp_path: Path):
        path = tmp_path / "cloud_keys.json"
        assert not path.exists()
        store = CloudCredentialStore(path=path)
        assert path.exists()
        assert S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert "credentials" in data
        assert "links" in data
