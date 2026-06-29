"""Per-key cloud LLM credential store for Guardian.

Guardian's :mod:`~app.proxy.providers` registry describes *which* cloud
providers exist (OpenRouter, NVIDIA, …) and the models they expose, but it
uses a single API key per provider loaded from ``settings.yaml``.  This module
adds a second, finer-grained layer: individual cloud credentials can be
registered, each with its own API key and model list, and then *linked* to one
or more Guardian API keys so that a single Guardian key can route to a
specific cloud backend::

    flip_aabbccdd...  ──link──▶  cred_001 (nvidia, nvapi-xxx)
                                 models: [minimax/minimax-m3, deepseek-ai/deepseek-r1]

A Guardian key with a linked NVIDIA credential can then be addressed using
the ``guardian/{provider}/{model_path}`` route convention::

    guardian/nvidia/minimax/minimax-m3   →  ("nvidia", "minimax/minimax-m3")
    guardian/openrouter/openai/gpt-4o   →  ("openrouter", "openai/gpt-4o")
    openai/gpt-4o                       →  None  (not a Guardian cloud route)

The credentials file lives at ``config/cloud_keys.json``::

    {
      "credentials": {
        "cred_001": {
          "provider": "nvidia",
          "name": "NVIDIA Default",
          "api_key": "nvapi-xxx",
          "created_at": 1234567890.0,
          "models": ["minimax/minimax-m3", "deepseek-ai/deepseek-r1"]
        }
      },
      "links": {
        "flip_aabbccdd...": {
          "nvidia": "cred_001",
          "openrouter": "cred_002"
        }
      }
    }

Reads operate on an in-memory snapshot loaded at construction (and refreshed
on demand via :meth:`CloudCredentialStore.reload`).  All mutating writes are
serialised through an :class:`asyncio.Lock` so concurrent FastAPI handlers
cannot corrupt the on-disk document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Guardian.CloudKeys")

#: Path to the on-disk credential store.
CLOUD_KEYS_FILE: Path = Path(__file__).parent.parent.parent / "config" / "cloud_keys.json"

#: Empty document used when creating the store file for the first time.
_EMPTY_DOC: Dict[str, Any] = {"credentials": {}, "links": {}}

#: Prefix that marks a Guardian cloud route.
_ROUTE_PREFIX = "guardian/"


# ── Helpers ────────────────────────────────────────────────────────────


def mask_api_key(key: str) -> str:
    """Return a human-safe display version of *key*.

    The first 8 and last 4 characters are preserved and the middle is
    replaced with four asterisks.  Short keys that do not leave room for
    both windows (``len(key) <= 12``) are masked more aggressively so the
    full secret is never leaked.

    Examples
    --------
    >>> mask_api_key("nvapi-1234567890abcdef")
    'nvapi-12****cdef'
    >>> mask_api_key("sk-short")
    'sk****rt'
    >>> mask_api_key("abc")
    '***'
    """
    if not key:
        return ""
    if len(key) <= 12:
        if len(key) <= 4:
            return "*" * len(key)
        return key[:2] + "****" + key[-2:]
    return key[:8] + "****" + key[-4:]


def parse_guardian_route(model_name: str) -> Optional[Tuple[str, str]]:
    """Parse a ``guardian/{provider}/{model_path}`` route string.

    Returns a ``(provider, model_path)`` tuple where *provider* is the first
    path segment after ``guardian/`` and *model_path* is everything that
    follows (it may itself contain slashes for namespaced models).

    Returns ``None`` when *model_name* does not start with the ``guardian/``
    prefix or lacks a non-empty provider and/or model segment.

    Examples
    --------
    >>> parse_guardian_route("guardian/nvidia/minimax/minimax-m3")
    ('nvidia', 'minimax/minimax-m3')
    >>> parse_guardian_route("guardian/openrouter/openai/gpt-4o")
    ('openrouter', 'openai/gpt-4o')
    >>> parse_guardian_route("openai/gpt-4o") is None
    True
    """
    if not isinstance(model_name, str) or not model_name:
        return None
    if not model_name.startswith(_ROUTE_PREFIX):
        return None
    rest = model_name[len(_ROUTE_PREFIX):]
    slash = rest.find("/")
    if slash <= 0:
        # No provider/model boundary, or an empty provider segment.
        return None
    provider = rest[:slash]
    model_path = rest[slash + 1:]
    if not model_path:
        return None
    return (provider, model_path)


# ── Data model ──────────────────────────────────────────────────────────


@dataclass
class CloudCredential:
    """A single cloud provider credential (API key + allowed models).

    Instances returned from the lookup path (``get_credential_for_key``)
    carry the **unmasked** API key so the proxy can forward it upstream.
    Dictionary representations produced for listing endpoints are masked via
    :func:`mask_api_key`.
    """

    id: str
    provider: str
    name: str
    api_key: str
    created_at: float
    models: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, cred_id: str, raw: Dict[str, Any]) -> "CloudCredential":
        """Build a :class:`CloudCredential` from its JSON representation."""
        return cls(
            id=cred_id,
            provider=str(raw.get("provider", "")),
            name=str(raw.get("name", "")),
            api_key=str(raw.get("api_key", "")),
            created_at=float(raw.get("created_at", 0.0) or 0.0),
            models=[str(m) for m in (raw.get("models") or []) if m],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the serialisable representation written to disk."""
        return {
            "provider": self.provider,
            "name": self.name,
            "api_key": self.api_key,
            "created_at": self.created_at,
            "models": list(self.models),
        }

    def to_masked_dict(self) -> Dict[str, Any]:
        """Return a display-safe dict with the API key masked.

        The credential ``id`` is included so callers can correlate entries.
        """
        result = self.to_dict()
        result["id"] = self.id
        result["api_key"] = mask_api_key(self.api_key)
        return result


# ── Store ──────────────────────────────────────────────────────────────


class CloudCredentialStore:
    """Persistent, per-key cloud credential store.

    The store keeps an in-memory snapshot of ``cloud_keys.json`` (loaded in
    :meth:`__init__` and refreshed on demand via :meth:`reload`).  Read
    methods consult this snapshot and never touch disk, so they are cheap and
    safe to call concurrently.  All mutating methods are coroutines that
    serialise their read-modify-write cycle through an :class:`asyncio.Lock`
    and persist atomically (temp-file + ``rename``) so concurrent FastAPI
    handlers cannot corrupt the on-disk document.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path: Path = path if path is not None else CLOUD_KEYS_FILE
        self._write_lock: asyncio.Lock = asyncio.Lock()
        self.reload()

    # ── Persistence ───────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read the credential store from disk into memory.

        Creates an empty store file when one does not yet exist.  A corrupt
        or partially-written file is logged and treated as an empty document
        so the proxy stays responsive.
        """
        self._ensure_file()
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "⚠️  Failed to parse %s (%s); starting with an empty store",
                self._path,
                e,
            )
            data = {"credentials": {}, "links": {}}

        if not isinstance(data, dict):
            logger.warning("⚠️  %s root is not a dict; using empty store", self._path)
            data = {"credentials": {}, "links": {}}

        creds = data.get("credentials", {})
        links = data.get("links", {})
        if not isinstance(creds, dict):
            logger.warning("⚠️  'credentials' in %s is not a dict; ignoring", self._path)
            creds = {}
        if not isinstance(links, dict):
            logger.warning("⚠️  'links' in %s is not a dict; ignoring", self._path)
            links = {}

        self._data: Dict[str, Any] = {"credentials": creds, "links": links}
        logger.debug(
            "☁️  Loaded %d credential(s) and %d link(s) from %s",
            len(creds),
            len(links),
            self._path,
        )

    def _ensure_file(self) -> None:
        """Create the store file with an empty structure if it is absent."""
        try:
            if not self._path.exists():
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(_EMPTY_DOC, f, indent=2)
                logger.info("☁️  Created empty cloud credential store at %s", self._path)
        except OSError as e:
            logger.error("❌  Could not create cloud credential store %s: %s", self._path, e)

    def _save(self) -> None:
        """Write the current in-memory document to disk atomically.

        Callers must already hold ``self._write_lock``.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        tmp_path.replace(self._path)

    # ── Credentials ───────────────────────────────────────────────────

    def list_credentials(self) -> List[Dict[str, Any]]:
        """Return every credential as a list of masked dicts."""
        creds = self._data.get("credentials", {})
        return [
            CloudCredential.from_dict(cred_id, raw).to_masked_dict()
            for cred_id, raw in sorted(creds.items())
            if isinstance(raw, dict)
        ]

    async def add_credential(
        self,
        provider: str,
        name: str,
        api_key: str,
        models: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Register a new cloud credential and return its masked dict.

        Parameters
        ----------
        provider:
            Upstream provider identifier (``"nvidia"``, ``"openrouter"`` …).
        name:
            Human-readable label for the credential.
        api_key:
            The raw upstream API key.  Stored in plaintext on disk so the
            proxy can forward it; only masked in the returned dict.
        models:
            Optional list of model identifiers this credential may serve.
        """
        async with self._write_lock:
            cred_id = f"cred_{uuid.uuid4().hex[:8]}"
            credential = CloudCredential(
                id=cred_id,
                provider=str(provider),
                name=str(name),
                api_key=str(api_key),
                created_at=time.time(),
                models=[str(m) for m in (models or []) if m],
            )
            self._data.setdefault("credentials", {})[cred_id] = credential.to_dict()
            self._save()
            logger.info(
                "☁️  Added cloud credential '%s' (%s) id=%s",
                name,
                provider,
                cred_id,
            )
            return credential.to_masked_dict()

    async def delete_credential(self, cred_id: str) -> bool:
        """Delete *cred_id* and every link that references it.

        Returns ``True`` when the credential existed and was removed.
        """
        async with self._write_lock:
            creds = self._data.get("credentials", {})
            if cred_id not in creds:
                logger.warning("⚠️  delete_credential: '%s' not found", cred_id)
                return False

            creds.pop(cred_id)

            # Purge any links pointing at the deleted credential.
            removed_links = 0
            links = self._data.get("links", {})
            for fingerprint, provider_map in list(links.items()):
                if not isinstance(provider_map, dict):
                    continue
                for prov, cid in list(provider_map.items()):
                    if cid == cred_id:
                        provider_map.pop(prov, None)
                        removed_links += 1
                if not provider_map:
                    links.pop(fingerprint, None)

            self._save()
            logger.info(
                "☁️  Deleted credential '%s' and %d link(s)",
                cred_id,
                removed_links,
            )
            return True

    async def add_model_to_credential(self, cred_id: str, model_name: str) -> bool:
        """Append *model_name* to a credential's model list.

        Returns ``False`` if the credential does not exist or the model was
        already present.
        """
        async with self._write_lock:
            creds = self._data.get("credentials", {})
            raw = creds.get(cred_id)
            if not isinstance(raw, dict):
                logger.warning("⚠️  add_model_to_credential: '%s' not found", cred_id)
                return False
            models = raw.get("models")
            if not isinstance(models, list):
                models = []
                raw["models"] = models
            if model_name in models:
                return False
            models.append(model_name)
            self._save()
            logger.info("☁️  Added model '%s' to credential '%s'", model_name, cred_id)
            return True

    async def remove_model_from_credential(self, cred_id: str, model_name: str) -> bool:
        """Remove *model_name* from a credential's model list.

        Returns ``False`` if the credential or model was not present.
        """
        async with self._write_lock:
            creds = self._data.get("credentials", {})
            raw = creds.get(cred_id)
            if not isinstance(raw, dict):
                logger.warning(
                    "⚠️  remove_model_from_credential: '%s' not found", cred_id
                )
                return False
            models = raw.get("models")
            if not isinstance(models, list) or model_name not in models:
                return False
            models.remove(model_name)
            self._save()
            logger.info(
                "☁️  Removed model '%s' from credential '%s'", model_name, cred_id
            )
            return True

    # ── Links ─────────────────────────────────────────────────────────

    async def link_credential(
        self,
        guardian_key_fingerprint: str,
        provider: str,
        cred_id: str,
    ) -> bool:
        """Link a cloud credential to a Guardian API key fingerprint.

        Returns ``False`` if the referenced credential does not exist.
        """
        async with self._write_lock:
            creds = self._data.get("credentials", {})
            if cred_id not in creds:
                logger.warning(
                    "⚠️  link_credential: credential '%s' not found", cred_id
                )
                return False
            links = self._data.setdefault("links", {})
            provider_map = links.setdefault(guardian_key_fingerprint, {})
            if not isinstance(provider_map, dict):
                provider_map = {}
                links[guardian_key_fingerprint] = provider_map
            provider_map[provider] = cred_id
            self._save()
            logger.info(
                "☁️  Linked Guardian key '%s' → %s/%s",
                guardian_key_fingerprint,
                provider,
                cred_id,
            )
            return True

    async def unlink_credential(
        self,
        guardian_key_fingerprint: str,
        provider: str,
    ) -> bool:
        """Remove a provider link from a Guardian key.

        Returns ``False`` if no link existed for that provider.
        """
        async with self._write_lock:
            links = self._data.get("links", {})
            provider_map = links.get(guardian_key_fingerprint)
            if not isinstance(provider_map, dict) or provider not in provider_map:
                return False
            provider_map.pop(provider, None)
            if not provider_map:
                links.pop(guardian_key_fingerprint, None)
            self._save()
            logger.info(
                "☁️  Unlinked provider '%s' from Guardian key '%s'",
                provider,
                guardian_key_fingerprint,
            )
            return True

    def list_links(self) -> Dict[str, Dict[str, str]]:
        """Return the full link map ``{fingerprint: {provider: cred_id}}``."""
        return {
            str(fp): {str(p): str(c) for p, c in pm.items()}
            for fp, pm in self._data.get("links", {}).items()
            if isinstance(pm, dict)
        }

    # ── Lookups ───────────────────────────────────────────────────────

    def get_credential_for_key(
        self,
        guardian_key_fingerprint: str,
        provider: str,
    ) -> Optional[CloudCredential]:
        """Return the credential linked to *guardian_key_fingerprint*.

        The returned :class:`CloudCredential` carries the **unmasked** API
        key so the proxy can forward it to the upstream provider.  Returns
        ``None`` when no link exists for the given provider.
        """
        links = self._data.get("links", {})
        provider_map = links.get(guardian_key_fingerprint)
        if not isinstance(provider_map, dict):
            return None
        cred_id = provider_map.get(provider)
        if not cred_id:
            return None
        creds = self._data.get("credentials", {})
        raw = creds.get(cred_id)
        if not isinstance(raw, dict):
            logger.warning(
                "⚠️  Link for '%s/%s' points at missing credential '%s'",
                guardian_key_fingerprint,
                provider,
                cred_id,
            )
            return None
        return CloudCredential.from_dict(cred_id, raw)

    def get_linked_models_for_key(
        self,
        guardian_key_fingerprint: str,
    ) -> List[Dict[str, Any]]:
        """Return every cloud model available to a Guardian key.

        Each entry is shaped as::

            {
              "id": "guardian/nvidia/minimax/minimax-m3",
              "provider": "nvidia",
              "model": "minimax/minimax-m3",
              "credential_id": "cred_001"
            }
        """
        result: List[Dict[str, Any]] = []
        links = self._data.get("links", {})
        provider_map = links.get(guardian_key_fingerprint)
        if not isinstance(provider_map, dict):
            return result
        creds = self._data.get("credentials", {})
        for provider, cred_id in provider_map.items():
            raw = creds.get(cred_id)
            if not isinstance(raw, dict):
                logger.warning(
                    "⚠️  Linked credential '%s' for provider '%s' not found",
                    cred_id,
                    provider,
                )
                continue
            for model in raw.get("models") or []:
                if not model:
                    continue
                result.append(
                    {
                        "id": f"guardian/{provider}/{model}",
                        "provider": str(provider),
                        "model": str(model),
                        "credential_id": str(cred_id),
                    }
                )
        return result
