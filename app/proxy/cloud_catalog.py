"""Dynamic cloud model catalog — fetched from each provider's /v1/models.

Part of the Cloud Access Redesign (2026-08-21): replaces the hand-maintained
per-key ``guardian/{provider}/{model}`` routes / linked-credential model lists
with a single, consistent ``{provider}/{brand}/{model}`` cloud model catalog
built from each configured provider's own OpenAI-compatible ``/v1/models``
endpoint.

For every *enabled and configured* provider this module:

- Fetches ``{base_url}/models`` using the provider's settings API key
  (``providers.<name>.api_key`` → ``$ENV``).
- Normalizes each upstream model id to ``{brand}/{model}`` so the
  ``{provider}/{brand}/{model}`` address is structurally identical across
  providers.  A bare upstream id (no ``/``) is prefixed with the provider's
  declared ``brand`` (default: the provider name), so google's ``gemini-…``
  becomes ``google/gemini-…`` and openai's ``gpt-4o`` becomes ``openai/gpt-4o``.
- Caches the result in memory with a TTL, and persists it to a runtime cache
  file (``data/cloud_catalog_cache.json``) so Guardian can serve a usable
  catalog at startup *before* the first fetch completes (cold-start fallback,
  reviewer #2) and keeps the last successful list on a failed refresh (like
  today's google fallback).
- Curates the OpenRouter-parity metadata subset per model
  (``architecture`` / ``pricing`` / ``top_provider`` / ``supported_parameters``
  / ``reasoning`` / …, see :data:`METADATA_FIELDS` and
  ``docs/OPENROUTER_PARITY.md`` §3) into the ``metadata`` map, so a client
  asking Guardian about a model gets the information the upstream actually
  advertises instead of the four fields the pre-parity catalog kept.

The per-provider ``models:`` blocks in ``config/providers/*.settings.yaml``
supply per-model **overrides** (context window, thinking capability, tool
support, model sampling defaults, …) layered *above* the default template —
they are not a hand-maintained catalog, only exceptions from defaults.  (Before
F2 these lived in ``config/cloud_models.yaml`` / ``models.cloud.overrides.yaml``.)

This module is cheap to reconstruct and hot-reload aware: call
:meth:`CloudModelCatalog.reload` after a ``settings.yaml`` edit to pick up
provider/brand changes without a restart.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config_loader import provider_settings_documents
from app.paths import CLOUD_CATALOG_CACHE_FILE, is_local_provider_name
from app.proxy.providers import CloudProvider, ProviderRegistry

logger = logging.getLogger("Guardian.CloudCatalog")

#: Default in-memory/persisted-cache TTL before a background refresh is allowed.
DEFAULT_TTL_SECONDS = 3600.0

#: Curated OpenRouter-parity metadata keys captured per model from the
#: provider's own ``/v1/models`` entry (``docs/OPENROUTER_PARITY.md`` §3).
#: Every key is always present in a stored entry; ``None`` means the upstream
#: did not advertise it (or advertised a value of the wrong type).
METADATA_FIELDS: tuple[str, ...] = (
    "canonical_slug",
    "hugging_face_id",
    "name",
    "description",
    "created",
    "context_length",
    "architecture",
    "pricing",
    "top_provider",
    "per_request_limits",
    "supported_parameters",
    "default_parameters",
    "knowledge_cutoff",
    "expiration_date",
    "reasoning",
)

#: Sub-keys always present in the ``architecture`` metadata block (§3.1).
ARCHITECTURE_FIELDS: tuple[str, ...] = (
    "modality",
    "input_modalities",
    "output_modalities",
    "tokenizer",
    "instruct_type",
)

#: Sub-keys always present in the ``top_provider`` metadata block (§3.3).
TOP_PROVIDER_FIELDS: tuple[str, ...] = (
    "context_length",
    "max_completion_tokens",
    "is_moderated",
)


# ── Defensive type coercion for upstream JSON ─────────────────────────
# An upstream may send any JSON type for any key.  Only a value matching the
# expected type is stored; everything else becomes ``None``.  ``bool`` is an
# ``int`` subclass in Python, so every numeric coercion excludes it explicitly
# — this repository has been bitten by ``isinstance(x, int)`` matching bools.
# An empty dict/list counts as "not advertised": it carries no information and
# would otherwise block cross-provider gap filling in the presentation layer.


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return a shallow copy of a non-empty dict, else ``None``.

    Values inside the dict are passed through verbatim (OpenRouter pricing
    values are string decimals; Guardian never rewrites them).
    """
    return dict(value) if isinstance(value, dict) and value else None


def _as_str_list(value: Any) -> list[str] | None:
    """Return the non-empty strings of a list, else ``None``."""
    if not isinstance(value, list):
        return None
    items = [item for item in value if isinstance(item, str) and item.strip()]
    return items or None


#: Default ``{brand}`` used when a provider's upstream model ids are bare.
DEFAULT_BRAND_BY_PROVIDER: dict[str, str] = {
    "openai": "openai",
    "google": "google",
    "nvidia": "nvidia",
    # F6 (2026-09-11): the Windows LAN provider declares ``brand: windows`` in
    # its provider file; without this entry the bare llama-server ids would
    # fall back to the provider stem as brand and the advertised address would
    # double to ``windows-gpu-local/windows-gpu-local/<model>``.
    "windows-gpu-local": "windows",
}


class CloudModelCatalog:
    """Fetches, normalizes, and caches the cloud model catalog per provider."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        cache_file: Path | None = None,
        overrides_file: Path | None = None,
    ) -> None:
        self._registry = provider_registry
        self._ttl_seconds = float(ttl_seconds)
        self._cache_file = cache_file or CLOUD_CATALOG_CACHE_FILE
        # An explicit ``overrides_file`` (tests/legacy single-file) loads that
        # flat map directly.  In production (None) the overrides come from the
        # per-provider ``models:`` blocks in config/providers/ (F2).
        self._explicit_overrides_file = overrides_file is not None
        self._overrides_file = overrides_file

        # provider name -> {"fetched_at": float, "models": {normalized_id: upstream_id}}
        self._catalogs: dict[str, dict[str, Any]] = {}
        self._overrides: dict[str, dict[str, Any]] = {}

        self._load_overrides()
        self._load_disk_cache()
        self.reload()

    # ── Overrides / disk cache ────────────────────────────────────────

    def _load_overrides(self) -> None:
        """Load per-model overrides into ``self._overrides``.

        Production (no explicit ``overrides_file``): merge the ``models:``
        blocks of every *cloud* provider file in ``config/providers/`` into a
        flat ``{model_id: overrides}`` map (F2).  Local providers (``*-local``
        name / ``local: true``) are skipped — their ``models:`` block is the
        local GGUF registry, not cloud overrides.

        Tests/legacy (explicit ``overrides_file``): load that single flat file
        (the old ``models.cloud.overrides.yaml`` shape).
        """
        if not self._explicit_overrides_file:
            merged: dict[str, Any] = {}
            try:
                for name, doc in provider_settings_documents().items():
                    if not isinstance(doc, dict):
                        continue
                    if is_local_provider_name(name) or bool(doc.get("local")):
                        continue
                    model_overrides = doc.get("models")
                    if isinstance(model_overrides, dict):
                        for model, entry in model_overrides.items():
                            if isinstance(entry, dict):
                                merged[str(model)] = dict(entry)
                self._overrides = merged
            except Exception as e:
                logger.warning("⚠️  Failed to load per-provider overrides: %s", e)
                self._overrides = {}
            return
        try:
            if not self._overrides_file.exists():
                self._overrides = {}
                return
            raw = yaml.safe_load(self._overrides_file.read_text(encoding="utf-8")) or {}
            self._overrides = raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning("⚠️  Failed to load cloud overrides file: %s", e)
            self._overrides = {}

    def _load_disk_cache(self) -> None:
        """Restore a previously persisted catalog for cold-start resilience.

        Entries are only restored when their *endpoint signature* (base_url +
        catalog_url) still matches the current provider config.  A change to
        either invalidates the stale cache, so e.g. switching openrouter to
        ``catalog_url=/models/user`` does not keep advertising the old 422-model
        list until a manual refresh.
        """
        try:
            if not self._cache_file.exists():
                return
            raw = json.loads(self._cache_file.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                return
            for provider in self._registry.get_enabled_providers():
                stored = raw.get(provider.name)
                if not (isinstance(stored, dict) and isinstance(stored.get("models"), dict)):
                    continue
                if stored.get("source") != self._provider_endpoint_key(provider):
                    logger.info(
                        "☁️  Cloud catalog cache for '%s' is stale (endpoint changed); dropping",
                        provider.name,
                    )
                    continue
                # Restore reasoning metadata when present (older caches lack it).
                if not isinstance(stored.get("reasoning"), dict):
                    stored["reasoning"] = {}
                # Restore consolidated context map when present (older caches
                # lack it; the next refresh fills it).
                if not isinstance(stored.get("context"), dict):
                    stored["context"] = {}
                # Restore modality capability when present (older caches lack
                # it; the next refresh fills it).
                if not isinstance(stored.get("modalities"), dict):
                    stored["modalities"] = {}
                # Trap 3 (OpenRouter parity, 2026-09-22): the per-model
                # OpenRouter-parity metadata map must be restored here too — a
                # cold start that silently lost it would answer /v1/models with
                # nulls for every model until the next refresh.  Older caches
                # lack it, so normalise to {} exactly like the maps above.
                if not isinstance(stored.get("metadata"), dict):
                    stored["metadata"] = {}
                self._catalogs[provider.name] = stored
            if self._catalogs:
                logger.info(
                    "☁️  Restored cold-start cloud catalog from %s (%d provider(s))",
                    self._cache_file,
                    len(self._catalogs),
                )
        except Exception as e:
            logger.debug("Cloud catalog disk cache not restored: %s", e)

    @staticmethod
    def _provider_endpoint_key(provider: CloudProvider | None) -> str:
        """Stable key identifying which catalog endpoint a provider points at."""
        if provider is None:
            return ""
        return f"{provider.base_url}|{provider.catalog_url or '/models'}"

    def _persist_cache(self) -> None:
        """Rewrite the runtime catalog cache, additively.

        This file is *shared runtime state*: more than one process (the service,
        a config reload, a maintenance script) can touch it, and they do not
        necessarily share a provider registry.  Rewriting the whole document
        from this instance's ``_catalogs`` was destructive: a scratch process
        built on the default cache path refreshed a single provider and replaced
        the file with that one provider, silently degrading cold-start discovery
        for every other provider until its next successful fetch.  Observed in
        production 2026-10-03, twice: ten providers / 308 models collapsed to
        one or two providers.

        The write is therefore purely additive — every entry already on the file
        is preserved, and only providers this instance actually holds catalog
        data for are (over)written.  Nothing is pruned here, and nothing needs
        to be: ``_load_disk_cache`` restores a provider only when it is still
        enabled *and* its stored endpoint signature matches the current config,
        so a stale or removed provider's entry is simply never read, and the
        next successful fetch for that provider overwrites it in place.
        """
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            by_name = {p.name: p for p in self._registry.get_enabled_providers()}

            payload: dict[str, Any] = {}
            if self._cache_file.exists():
                try:
                    existing = json.loads(self._cache_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    existing = {}
                if isinstance(existing, dict):
                    payload = {
                        name: stored
                        for name, stored in existing.items()
                        if isinstance(stored, dict)
                    }

            for provider_name, data in self._catalogs.items():
                if not (isinstance(data, dict) and data.get("models")):
                    continue
                payload[provider_name] = {
                    "fetched_at": data["fetched_at"],
                    "models": data["models"],
                    "reasoning": data.get("reasoning") or {},
                    # Persist the consolidated maps too (context since trap 1,
                    # modalities since trap 2, per-model OpenRouter-parity
                    # metadata since trap 3) — a subset here would silently
                    # drop them on every restart.
                    "context": data.get("context") or {},
                    "modalities": data.get("modalities") or {},
                    "metadata": data.get("metadata") or {},
                    "source": self._provider_endpoint_key(by_name.get(provider_name)),
                }

            if not payload:
                return
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning("⚠️  Failed to persist cloud catalog cache: %s", e)

    def reload(self) -> None:
        """Re-read overrides and align cached entries with current providers.

        Also re-loads the persisted disk cache: its per-provider ``source``
        (``base_url|catalog_url``) check drops any entry whose endpoint changed
        (e.g. switching ``catalog_url``), so a hot ``/api/config/reload`` that
        changes the catalog endpoint stops advertising the stale list without
        needing a restart or a manual ``/api/cloud/catalog/refresh``.
        """
        self._load_overrides()
        self._load_disk_cache()
        enabled_names = {p.name for p in self._registry.get_enabled_providers()}
        for stale in [p for p in self._catalogs if p not in enabled_names]:
            self._catalogs.pop(stale, None)

    # ── Brand normalization ───────────────────────────────────────────

    def _default_brand(self, provider: CloudProvider) -> str:
        return DEFAULT_BRAND_BY_PROVIDER.get(provider.name, provider.name)

    @staticmethod
    def _normalize_upstream_id(raw_id: str, brand: str) -> str:
        """Return ``{brand}/{model}`` for an upstream model id.

        A bare id (no ``/``) gets the *brand* prefix; a namespaced id is kept
        as-is so an already-branded upstream id (e.g. nvidia's
        ``minimaxai/minimax-m3``) is preserved.

        A leading ``models/`` prefix — the format google's OpenAI-compatible
        /v1/models returns (``models/gemini-2.5-flash``) — is stripped before
        the brand logic so it normalizes to ``google/gemini-2.5-flash``
        (→ ``google/google/gemini-2.5-flash``), consistent with how the old
        ``normalize_google_model_id`` behaved.
        """
        raw_id = (raw_id or "").strip()
        if not raw_id:
            return ""
        if raw_id.lower().startswith("models/"):
            raw_id = raw_id[len("models/") :]
        if not raw_id:
            return ""
        if "/" in raw_id:
            return raw_id
        return f"{brand}/{raw_id}"

    @staticmethod
    def _extract_reasoning(entry: dict[str, Any]) -> dict[str, Any]:
        """Extract reasoning-effort metadata from one catalog entry.

        OpenRouter (and providers that mirror its catalog shape) advertise per
        model ``reasoning: {mandatory, default_enabled, supported_efforts,
        default_effort}`` in ``/v1/models``.  Guardian only forwards a safe
        subset — the effort stages and defaults a client needs to render a
        reasoning-effort selector — and ignores everything else.  Providers
        without a ``reasoning`` block (google, openai, nvidia, …) yield ``{}``
        so their entries stay unannotated.
        """
        if not isinstance(entry, dict):
            return {}
        raw = entry.get("reasoning")
        if not isinstance(raw, dict):
            return {}
        supported = raw.get("supported_efforts")
        if not isinstance(supported, list):
            return {}
        efforts = [s for s in supported if isinstance(s, str) and s]
        if not efforts:
            return {}
        result: dict[str, Any] = {"supported_efforts": efforts}
        if isinstance(raw.get("default_effort"), str) and raw.get("default_effort"):
            result["default_effort"] = raw["default_effort"]
        if isinstance(raw.get("mandatory"), bool):
            result["mandatory"] = raw["mandatory"]
        if isinstance(raw.get("default_enabled"), bool):
            result["default_enabled"] = raw["default_enabled"]
        return result

    @staticmethod
    def extract_model_metadata(entry: dict[str, Any]) -> dict[str, Any]:
        """Curate the OpenRouter-parity metadata subset for one catalog entry.

        Pre-parity the catalog kept only ``models`` / ``reasoning`` /
        ``context`` / ``modalities`` and threw away everything else the
        upstream advertised.  This extraction captures the full curated subset
        defined by ``docs/OPENROUTER_PARITY.md`` §3 so ``/v1/models`` can
        present the same information OpenRouter does.

        Shape contract (also used by the reference catalog, so both sources can
        be treated uniformly by the presentation layer):

        - every key in :data:`METADATA_FIELDS` is always present;
        - ``None`` means "the upstream did not advertise this" (or advertised a
          wrongly-typed value — see the coercion helpers above);
        - ``architecture`` and ``top_provider`` are always dicts carrying every
          sub-key from :data:`ARCHITECTURE_FIELDS` /
          :data:`TOP_PROVIDER_FIELDS`, ``None`` where unknown (§3.1 says an
          entry with no modality information gets explicit nulls, never an
          omitted key);
        - ``reasoning`` is the *full* upstream block, unlike the reduced subset
          :meth:`_extract_reasoning` produces for the legacy accessor.
        """
        if not isinstance(entry, dict):
            entry = {}
        architecture = entry.get("architecture")
        if not isinstance(architecture, dict):
            architecture = {}
        top_provider = entry.get("top_provider")
        if not isinstance(top_provider, dict):
            top_provider = {}
        return {
            "canonical_slug": _as_str(entry.get("canonical_slug")),
            "hugging_face_id": _as_str(entry.get("hugging_face_id")),
            "name": _as_str(entry.get("name")),
            "description": _as_str(entry.get("description")),
            "created": _as_int(entry.get("created")),
            "context_length": _as_int(entry.get("context_length")),
            "architecture": {
                "modality": _as_str(architecture.get("modality")),
                "input_modalities": _as_str_list(architecture.get("input_modalities")),
                "output_modalities": _as_str_list(architecture.get("output_modalities")),
                "tokenizer": _as_str(architecture.get("tokenizer")),
                "instruct_type": _as_str(architecture.get("instruct_type")),
            },
            "pricing": _as_dict(entry.get("pricing")),
            "top_provider": {
                "context_length": _as_int(top_provider.get("context_length")),
                "max_completion_tokens": _as_int(top_provider.get("max_completion_tokens")),
                "is_moderated": _as_bool(top_provider.get("is_moderated")),
            },
            "per_request_limits": _as_dict(entry.get("per_request_limits")),
            "supported_parameters": _as_str_list(entry.get("supported_parameters")),
            "default_parameters": _as_dict(entry.get("default_parameters")),
            "knowledge_cutoff": _as_str(entry.get("knowledge_cutoff")),
            "expiration_date": _as_str(entry.get("expiration_date")),
            "reasoning": _as_dict(entry.get("reasoning")),
        }

    # ── Fetching ──────────────────────────────────────────────────────

    def _set_auth_error(self, provider_name: str, value: bool) -> None:
        """Record whether a provider's credentials are broken (401/403).

        In-memory only (not persisted to the disk cache): the flag is cleared
        on the next successful fetch, and a fresh process re-detects it.
        """
        data = self._catalogs.setdefault(provider_name, {})
        if isinstance(data, dict):
            data["auth_error"] = bool(value)

    def is_auth_error(self, provider_name: str) -> bool:
        """Return True when the provider's last catalog fetch failed with 401/403.

        Used by the admin surface to surface ``broken-credentials`` loudly
        instead of a silent ``model_count: 0``.
        """
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return False
        return bool(data.get("auth_error"))

    async def refresh_provider(self, provider: CloudProvider) -> dict[str, str]:
        """Fetch and normalize one provider's catalog.

        Returns ``{normalized_id: upstream_id}``.  On failure the previously
        cached list is kept (persisted from last successful run).
        """
        headers = ProviderRegistry.build_forward_headers(provider)
        catalog_path = provider.catalog_url or "/models"
        url = f"{provider.base_url}{catalog_path}"
        timeout = min(max(float(provider.timeout_seconds), 1.0), 30.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            # Distinguish auth failures (401/403 = broken credentials) from
            # transient errors so the admin surface can flag them loudly
            # instead of silently showing `model_count:0`.
            auth_error: bool = False
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", None) in (401, 403):
                auth_error = True
            self._set_auth_error(provider.name, auth_error)
            logger.warning(
                "⚠️  Cloud catalog fetch failed for provider '%s' (%s)%s; keeping last successful list",
                provider.name,
                exc,
                " — BROKEN CREDENTIALS (401/403)" if auth_error else "",
            )
            return dict(self._catalogs.get(provider.name, {}).get("models", {}))

        brand = self._default_brand(provider)
        normalized: dict[str, str] = {}
        reasoning_by_model: dict[str, dict[str, Any]] = {}
        context_by_model: dict[str, int] = {}
        modalities_by_model: dict[str, dict[str, Any]] = {}
        metadata_by_model: dict[str, dict[str, Any]] = {}
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            self._set_auth_error(provider.name, False)
            logger.warning("⚠️  Provider '%s' /v1/models returned no 'data' list", provider.name)
            return dict(self._catalogs.get(provider.name, {}).get("models", {}))

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id") or entry.get("name")
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            norm = self._normalize_upstream_id(raw_id, brand)
            if not norm:
                continue
            normalized[norm] = raw_id.strip()
            reasoning = self._extract_reasoning(entry)
            if reasoning:
                reasoning_by_model[norm] = reasoning
            # Consolidated (2026-09-02): the provider /models endpoint is now
            # fetched ONCE — context sizes ride along here instead of a second
            # independent fetch in ProviderRegistry._get_context_catalog.
            # Key space matches the old extractor: canonical_model_id(raw_id).
            raw_context = entry.get("context_length")
            if raw_context is None:
                raw_context = entry.get("max_input_tokens")
            if isinstance(raw_context, int) and not isinstance(raw_context, bool) and raw_context > 0:
                ctx_key = self._registry.canonical_model_id(raw_id.strip())
                if ctx_key:
                    context_by_model[ctx_key] = raw_context
            # Trap 2 (2026-09-02): modality capability rides along on the
            # same single fetch (OpenRouter exposes architecture.modalities).
            mods = self._extract_modalities(entry)
            if mods:
                modalities_by_model[norm] = mods
            # OpenRouter parity (2026-09-22): capture the full curated metadata
            # subset (architecture/pricing/top_provider/supported_parameters/…)
            # keyed by the normalized id, alongside the reduced maps above.
            metadata_by_model[norm] = self.extract_model_metadata(entry)
        if not normalized:
            self._set_auth_error(provider.name, False)
            logger.warning("⚠️  Provider '%s' /v1/models returned an empty catalog", provider.name)
            return dict(self._catalogs.get(provider.name, {}).get("models", {}))

        self._catalogs[provider.name] = {
            "fetched_at": time.time(),
            "models": normalized,
            "reasoning": reasoning_by_model,
            "context": context_by_model,
            "modalities": modalities_by_model,
            "metadata": metadata_by_model,
            "auth_error": False,
        }
        self._persist_cache()
        logger.info(
            "☁️  Cloud catalog refreshed for provider '%s': %d model(s), %d with reasoning metadata",
            provider.name,
            len(normalized),
            len(reasoning_by_model),
        )
        return normalized

    async def refresh_all(self) -> None:
        """Fetch every enabled+configured provider catalog concurrently-ish."""
        for provider in self._registry.get_enabled_providers():
            if not provider.is_configured:
                logger.info("☁️  Provider '%s' has no API key; skipping catalog fetch", provider.name)
                continue
            try:
                await self.refresh_provider(provider)
            except Exception as e:
                logger.warning("☁️  Catalog refresh for '%s' failed: %s", provider.name, e)

    async def ensure_all_fresh(self) -> None:
        """TTL-gated counterpart of refresh_all: refresh only stale providers.

        Safe to call on a schedule — each provider's :meth:`ensure_fresh`
        checks its own staleness first, so a healthy deployment costs zero
        network traffic while a cold cache (fresh install, disk wipe) or an
        expired TTL self-heals without operator action.
        """
        for provider in self._registry.get_enabled_providers():
            await self.ensure_fresh(provider.name)

    def is_stale(self, provider_name: str) -> bool:
        data = self._catalogs.get(provider_name)
        if data is None:
            return True
        return (time.time() - float(data.get("fetched_at", 0))) > self._ttl_seconds

    def is_provider_catalog_known(self, provider_name: str) -> bool:
        """True when the provider has catalog state: fetched, or restored from
        the disk cache at cold start.

        This is the 'do we know what this provider serves at all' signal — it
        distinguishes 'not fetched yet' (the window in which a prefix match may
        still be the only routing evidence) from 'fetched and the model is
        absent', where absence is evidence the model is not served. An empty
        models map with a fetch timestamp still counts as known: the fetch
        happened and answered (possibly with nothing).
        """
        data = self._catalogs.get(provider_name)
        return isinstance(data, dict) and bool(data.get("fetched_at"))

    async def ensure_fresh(self, provider_name: str) -> None:
        """Refresh a provider's catalog only when its TTL has elapsed."""
        provider = self._registry._providers.get(provider_name)
        if provider is None or not provider.is_configured:
            return
        if not self.is_stale(provider_name):
            return
        try:
            await self.refresh_provider(provider)
        except Exception as e:
            logger.warning("☁️  ensure_fresh failed for '%s': %s", provider_name, e)

    # ── Queries ───────────────────────────────────────────────────────

    def get_models_for_provider(self, provider_name: str) -> dict[str, str]:
        """Return ``{normalized_id: upstream_id}`` for a provider (cached view).

        When the provider declares a ``catalog_allowlist`` (e.g. NVIDIA's free
        tier), only the advertised ids are returned — both for discovery and for
        routing — so modellen that the token cannot actually reach stay hidden.
        """
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return {}
        models = dict(data.get("models", {}))

        provider = self._registry._providers.get(provider_name)
        allowlist = getattr(provider, "catalog_allowlist", None)
        if allowlist:
            models = {k: v for k, v in models.items() if k in allowlist}
        return models

    @staticmethod
    def _extract_modalities(entry: dict[str, Any]) -> dict[str, list[str]] | None:
        """Extract input/output modality capability from a catalog entry.

        Preferred shape: ``architecture.input_modalities`` / ``output_modalities``
        lists.  Fallback: parse the ``architecture.modality`` string form
        ``"text+image+video->text"`` (input side before the arrow).  Returns
        ``None`` when the entry advertises no modality data at all.
        """
        architecture = entry.get("architecture")
        if not isinstance(architecture, dict):
            return None
        input_mods = architecture.get("input_modalities")
        if not isinstance(input_mods, list):
            modality = architecture.get("modality")
            if isinstance(modality, str) and "->" in modality:
                input_side = modality.split("->", 1)[0]
                input_mods = [m for m in input_side.split("+") if m]
            else:
                return None
        clean_input = sorted(
            {m.strip().lower() for m in input_mods if isinstance(m, str) and m.strip()}
        )
        if not clean_input:
            return None
        output_mods = architecture.get("output_modalities")
        clean_output: list[str] = []
        if isinstance(output_mods, list):
            clean_output = sorted(
                {m.strip().lower() for m in output_mods if isinstance(m, str) and m.strip()}
            )
        result: dict[str, list[str]] = {"input": clean_input}
        if clean_output:
            result["output"] = clean_output
        return result

    def get_model_modalities(self, provider_name: str, normalized_id: str) -> dict[str, list[str]] | None:
        """Return upstream-advertised modality capability for a model id.

        ``normalized_id`` is the catalog key space (``{brand}/{model}``, the
        same shape as failover-candidate configs).  Returns ``None`` when the
        provider catalog has no modality data (cold cache, unadvertised model,
        or an upstream that does not advertise it) — callers then fall back to
        configured behavior.  Deliberately does NOT apply ``catalog_allowlist``:
        routing decisions on explicitly configured candidates need the real
        capability, and the pre-trap-2 config-only path never filtered either.
        """
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return None
        mods_map = data.get("modalities")
        if not isinstance(mods_map, dict):
            return None
        mods = mods_map.get(normalized_id)
        if isinstance(mods, dict) and isinstance(mods.get("input"), list) and mods["input"]:
            return mods
        return None

    def get_context_window(self, provider_name: str, normalized_id: str) -> int | None:
        """Return the upstream-advertised context window for a model id.

        ``normalized_id`` is in the registry-canonical key space (the same the
        pre-consolidation registry extractor used).  Returns ``None`` when the
        provider catalog has no context data (cold cache or unadvertised
        model) — callers fall back to the configured default.  Deliberately
        does NOT apply ``catalog_allowlist``: the pre-consolidation context
        fetch never filtered either, and context is needed for models that
        routing already resolved.
        """
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return None
        context_map = data.get("context")
        if not isinstance(context_map, dict):
            return None
        value = context_map.get(normalized_id)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    def get_model_reasoning(self, provider_name: str, normalized_id: str) -> dict[str, Any]:
        """Return reasoning-effort metadata for a ``{brand}/{model}`` id.

        Returns an empty dict when the provider does not advertise reasoning
        info (or the model is not in the provider's catalog).  Respects the
        provider's ``catalog_allowlist`` so models hidden from discovery are
        also hidden here.
        """
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return {}
        models = data.get("models", {})
        if not isinstance(models, dict) or normalized_id not in models:
            return {}
        provider = self._registry._providers.get(provider_name)
        allowlist = getattr(provider, "catalog_allowlist", None)
        if allowlist and normalized_id not in allowlist:
            return {}
        reasoning = data.get("reasoning") or {}
        raw = reasoning.get(normalized_id) if isinstance(reasoning, dict) else None
        return dict(raw) if isinstance(raw, dict) else {}

    def get_model_metadata(self, provider_name: str, normalized_id: str) -> dict[str, Any]:
        """Return OpenRouter-parity metadata captured from this provider's own
        /v1/models entry. Empty dict when absent or filtered by catalog_allowlist.

        ``normalized_id`` is the catalog key space (``{brand}/{model}``).  The
        returned dict has the shape produced by
        :meth:`extract_model_metadata` — every key of :data:`METADATA_FIELDS`
        present, ``None``/nulls where the upstream advertised nothing.
        """
        data = self._catalogs.get(provider_name)
        if not isinstance(data, dict):
            return {}
        models = data.get("models", {})
        if not isinstance(models, dict) or normalized_id not in models:
            return {}
        provider = self._registry._providers.get(provider_name)
        allowlist = getattr(provider, "catalog_allowlist", None)
        if allowlist and normalized_id not in allowlist:
            return {}
        metadata = data.get("metadata") or {}
        raw = metadata.get(normalized_id) if isinstance(metadata, dict) else None
        return dict(raw) if isinstance(raw, dict) else {}

    def get_model_overrides(self, normalized_id: str, provider_name: str = "") -> dict[str, Any]:
        """Return per-model overrides layered from cloud_models.yaml.

        Keys may be the full ``{provider}/{brand}/{model}``, ``{brand}/{model}``,
        or the bare upstream id.  Precedence: full address > namespaced > bare.
        """
        return dict(self._overrides.get(normalized_id, {}) or {})

    def get_override(self, key: str) -> dict[str, Any] | None:
        raw = self._overrides.get(key)
        if isinstance(raw, dict):
            return dict(raw)
        return None

    def addresses(self, provider_name: str) -> list[str]:
        """Return the full ``{provider}/{brand}/{model}`` addresses for a provider."""
        provider = provider_name
        return [
            f"{provider}/{norm}"
            for norm in self.get_models_for_provider(provider_name)
        ]

    # ── Addressing / resolution ───────────────────────────────────────

    def resolve_cloud_target(
        self,
        model_name: str,
        fallback: CloudProvider | None = None,
    ) -> tuple[str, str] | None:
        """Resolve a cloud model address to ``(provider_name, upstream_model)``.

        Accepts either the full ``{provider}/{brand}/{model}`` address (the
        ``{provider}`` segment names a configured provider) or a bare upstream
        name that matches a configured provider (``model_prefixes``/``models``).

        The upstream id is looked up in the fetched per-provider catalog so a
        provider that answers with bare ids (openai ``gpt-4o``, google
        ``gemini-…``) maps to the bare id the upstream API actually expects.
        When the catalog has not been fetched yet (cold-start) it falls back
        to stripping the ``{provider}/`` segment from the address.
        """
        # Full {provider}/{brand}/{model}: first segment is a known provider.
        first, sep, rest = model_name.partition("/")
        if sep and first and first in self._registry._providers:
            provider = self._registry._providers[first]
            catalog = self.get_models_for_provider(first)
            upstream = catalog.get(rest) if rest else None
            if upstream is None:
                upstream = rest or None
            if upstream is None:
                return None
            return first, upstream

        # Bare upstream name: resolve via the existing provider registry.
        provider = fallback or self._registry.get_provider_for_model(model_name)
        if provider is None:
            return None
        canonical = ProviderRegistry.canonical_model_id(model_name)
        catalog = self.get_models_for_provider(provider.name)
        return provider.name, catalog.get(canonical, canonical)
