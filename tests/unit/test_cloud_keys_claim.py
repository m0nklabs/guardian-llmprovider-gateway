"""Ownership-repair tests: claiming legacy (owner-less) cloud credentials.

A legacy credential linked to MORE THAN ONE key is unmanageable through the
API for every key (ambiguous ownership). Claiming lets a key that already
has a link adopt the credential as its permanent owner, after which the new
owner can manage the credential and link it to other Guardian keys.
"""

import pytest

from app.proxy.cloud_keys import CloudCredentialStore


@pytest.fixture
def tmp_store(tmp_path) -> CloudCredentialStore:
    """Create a CloudCredentialStore with a temp cloud_keys.json file."""
    return CloudCredentialStore(path=tmp_path / "cloud_keys.json")


@pytest.mark.asyncio
async def test_claim_adopts_legacy_credential(tmp_store: CloudCredentialStore):
    # Legacy credential as it exists in production data: no owner recorded,
    # but already linked to two keys (ambiguous → unmanageable for all).
    tmp_store._data["credentials"] = {
        "cred_legacy1": {
            "id": "cred_legacy1",
            "provider": "nvidia",
            "name": "Legacy NVIDIA",
            "api_key": "nvapi-legacy",
            "created_at": 1.0,
            "models": ["m1"],
        }
    }
    tmp_store._data["links"] = {
        "fp-alpha": {"nvidia": "cred_legacy1"},
        "fp-beta": {"nvidia": "cred_legacy1"},
    }
    assert tmp_store.is_credential_owned_by("cred_legacy1", "fp-alpha") is False
    assert tmp_store.is_credential_owned_by("cred_legacy1", "fp-beta") is False

    claimed = await tmp_store.claim_legacy_credential("fp-alpha", "nvidia", "cred_legacy1")
    assert claimed is True
    assert tmp_store.is_credential_owned_by("cred_legacy1", "fp-alpha") is True
    assert tmp_store.is_credential_owned_by("cred_legacy1", "fp-beta") is False
    raw = tmp_store._data["credentials"]["cred_legacy1"]
    assert raw["owner_key_fingerprint"] == "fp-alpha"


@pytest.mark.asyncio
async def test_claim_requires_existing_link(tmp_store: CloudCredentialStore):
    cred = await tmp_store.add_credential("openrouter", "OR", "sk-or", [])
    await tmp_store.link_credential("fp-owner", "openrouter", cred["id"])

    claimed = await tmp_store.claim_legacy_credential("fp-stranger", "openrouter", cred["id"])
    assert claimed is False  # no existing link → cannot claim
    assert tmp_store.is_credential_owned_by(cred["id"], "fp-stranger") is False


@pytest.mark.asyncio
async def test_claim_rejects_owned_credential(tmp_store: CloudCredentialStore):
    cred = await tmp_store.add_credential(
        "google",
        "G",
        "sk-g",
        [],
        owner_key_fingerprint="fp-owner",
    )
    await tmp_store.link_credential("fp-owner", "google", cred["id"])

    claimed = await tmp_store.claim_legacy_credential("fp-owner", "google", cred["id"])
    assert claimed is False  # already has an owner → nothing to claim


@pytest.mark.asyncio
async def test_claim_rejects_unknown_credential(tmp_store: CloudCredentialStore):
    claimed = await tmp_store.claim_legacy_credential("fp-alpha", "nvidia", "cred_missing")
    assert claimed is False


@pytest.mark.asyncio
async def test_claim_rejects_provider_mismatch(tmp_store: CloudCredentialStore):
    cred = await tmp_store.add_credential("nvidia", "NV", "sk-nv", [])
    await tmp_store.link_credential("fp-alpha", "nvidia", cred["id"])

    claimed = await tmp_store.claim_legacy_credential("fp-alpha", "google", cred["id"])
    assert claimed is False
    assert tmp_store._data["credentials"][cred["id"]].get("owner_key_fingerprint") is None