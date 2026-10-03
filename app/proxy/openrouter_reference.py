"""Cross-provider model metadata reference catalog (OpenRouter parity, §2).

``docs/OPENROUTER_PARITY.md`` §2 defines a *reference* metadata source: a
provider settings file may declare ``reference_catalog.sources`` — one or more
read-only upstream catalogs (e.g. OpenRouter's public ``/api/v1/models``) whose
entries fill the gaps another provider's own ``/v1/models`` leaves.  OpenAI's
endpoint says nothing about tools or reasoning; OpenRouter's entry for the same
model does, and that is the headline cross-provider win.

Rules implemented here (spec §2):

- **Pure reference data.**  A reference source is fetched independently of
  ``catalog_url``.  It never affects discovery, routing, allowlists or which
  models Guardian advertises — it is only ever read by the presentation layer.
  Every failure mode is fail-open: a fetch error keeps the last successful
  catalog and only logs a warning, so reference data can never break
  discovery.
- **Identity key.**  Entries are keyed by the upstream's own model id — for
  OpenRouter that is already ``{brand}/{model}``, i.e. the identity key of §1
  directly, so two providers genuinely serving the same model share metadata.
- **Merging.**  Several sources merge in declaration order: the first source
  that provides a non-null value for a field wins, later sources only fill
  gaps (including the sub-keys of ``architecture`` / ``top_provider``), see
  :func:`merge_metadata_gaps`.
- **Uniform shape.**  The normalised subset returned by
  :meth:`ModelReferenceCatalog.metadata` is produced by
  :meth:`app.proxy.cloud_catalog.CloudModelCatalog.extract_model_metadata`, so
  the presentation layer can treat an upstream entry and a reference entry
  identically.
- **Persistence.**  ``data/model_reference_catalog.json`` (see
  :func:`app.paths.model_reference_catalog_file`) holds the fetched entries
  plus ``fetched_at`` and a ``source`` signature (the joined source urls), so a
  url change invalidates the cache — the same endpoint-signature trick
  :class:`app.proxy.cloud_catalog.CloudModelCatalog` uses.
- **Hot reload.**  :meth:`ModelReferenceCatalog.reload` re-reads the provider
  settings documents and the disk cache, so an authenticated
  ``POST /api/config/reload`` picks up a changed source list without a restart.

Sources are declared as::

    reference_catalog:
      sources:
        - name: openrouter
          url: https://openrouter.ai/api/v1/models
          enabled: true
          ttl_seconds: 86400
          send_api_key: false

``send_api_key: true`` sends the *declaring provider's* own credential through
:meth:`app.proxy.providers.ProviderRegistry.build_forward_headers` (for a
source that needs an account-scoped listing); the default is an anonymous
fetch.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.config_loader import provider_settings_documents
from app.paths import model_reference_catalog_file
from app.proxy.cloud_catalog import CloudModelCatalog
from app.proxy.providers import CloudProvider, ProviderRegistry

logger = logging.getLogger("Guardian.ModelReferenceCatalog")

#: Default TTL before a source is refetched (a source may override it).
DEFAULT_TTL_SECONDS = 86400.0

#: Reference fetches are cheap and must never stall discovery, so an upstream
#: provider timeout is clamped to this ceiling (and to a 1 s floor).
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0


def merge_metadata_gaps(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Return *base* with every null field filled in from *extra*.

    This is the merge rule of spec §2 — "the first source that provides a
    non-null value for a field wins; later sources only fill gaps" — and the
    same helper the presentation layer can use to complete a partial upstream
    entry from the reference catalog (§3.1: "a reference entry can complete a
    partial upstream entry").

    Nested blocks (``architecture``, ``top_provider``, ``reasoning``) merge per
    sub-key; a value that is ``False``/``0``/``""`` is a real value and is
    never replaced — only ``None`` counts as a gap.
    """
    if not isinstance(base, dict):
        return dict(extra) if isinstance(extra, dict) else {}
    if not isinstance(extra, dict):
        return dict(base)
    merged: dict[str, Any] = {}
    for key, value in base.items():
        other = extra.get(key)
        if isinstance(value, dict) and isinstance(other, dict):
            block = dict(value)
            for sub_key, sub_value in other.items():
                if block.get(sub_key) is None and sub_value is not None:
                    block[sub_key] = sub_value
            merged[key] = block
        elif value is None and other is not None:
            merged[key] = other
        else:
            merged[key] = value
    for key, value in extra.items():
        if key not in merged and value is not None:
            merged[key] = value
    return merged


class ModelReferenceCatalog:
    """Read-only cross-provider metadata reference (spec §2).

    Constructed once per process and refreshed on a schedule; the catalog is
    cheap to reconstruct and hot-reload aware.
    """

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        *,
        cache_file: Path | None = None,
        sources: list[Any] | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._registry = provider_registry
        self._cache_file = Path(cache_file) if cache_file is not None else model_reference_catalog_file()
        self._ttl_seconds = float(ttl_seconds)
        # An explicit ``sources`` list (tests/injection) replaces the provider
        # settings scan entirely, mirroring CloudModelCatalog's explicit
        # ``overrides_file`` escape hatch.
        self._explicit_sources = sources is not None
        self._declared_sources: list[Any] = list(sources) if sources is not None else []

        self._sources: list[dict[str, Any]] = []
        # source name -> {identity_key: raw upstream entry}
        self._source_entries: dict[str, dict[str, dict[str, Any]]] = {}
        # source name -> epoch seconds of that source's last successful fetch
        self._source_fetched_at: dict[str, float] = {}
        # merged views over all sources (declaration order = precedence order)
        self._entries: dict[str, dict[str, Any]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._origin: dict[str, str] = {}

        self._load_sources()
        self._load_disk_cache()

    # ── Configuration ─────────────────────────────────────────────────

    def _load_sources(self) -> None:
        """(Re)read ``reference_catalog.sources`` from the provider settings.

        A provider without a ``reference_catalog`` block contributes nothing.
        Sources are deduplicated by url (one fetch per url, first declaration
        wins) and keep provider-file order, which is the precedence order for
        gap filling within a single file.
        """
        if self._explicit_sources:
            declared = [("", raw) for raw in self._declared_sources]
            self._sources = self._build_sources(declared)
            return
        declared: list[tuple[str, Any]] = []
        try:
            for provider_name, document in provider_settings_documents().items():
                if not isinstance(document, dict):
                    continue
                block = document.get("reference_catalog")
                if not isinstance(block, dict):
                    continue
                raw_sources = block.get("sources")
                if not isinstance(raw_sources, list):
                    continue
                for raw in raw_sources:
                    if isinstance(raw, dict):
                        declared.append((provider_name, raw))
        except Exception as exc:  # config trouble must never break discovery
            logger.warning("⚠️  Failed to load reference catalog sources: %s", exc)
            declared = []
        self._sources = self._build_sources(declared)

    def _build_sources(self, declared: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for provider_name, raw in declared:
            if not isinstance(raw, dict):
                continue
            url = raw.get("url")
            if not isinstance(url, str) or not url.strip():
                logger.warning(
                    "⚠️  Reference source without a url is ignored (provider '%s')",
                    provider_name or "-",
                )
                continue
            url = url.strip()
            # Only an explicit ``enabled: false`` disables a source; a missing
            # key means enabled (spec example and least-surprise default).
            if raw.get("enabled") is False:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = raw.get("name")
            if not (isinstance(name, str) and name.strip()):
                name = provider_name or url
            ttl = raw.get("ttl_seconds")
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or float(ttl) <= 0:
                ttl = self._ttl_seconds
            sources.append(
                {
                    "name": name.strip() if isinstance(name, str) else str(name),
                    "url": url,
                    "provider": provider_name,
                    "ttl_seconds": float(ttl),
                    "send_api_key": raw.get("send_api_key") is True,
                }
            )
        return sources

    def _source_signature(self) -> str:
        """Stable signature of the configured sources (the joined urls).

        Persisted in the cache document; a url change — or a source being added
        or removed — changes the signature and invalidates the stale cache.
        """
        return "|".join(str(source.get("url") or "") for source in self._sources)

    def _provider(self, name: str | None) -> CloudProvider | None:
        providers = getattr(self._registry, "_providers", None)
        if not isinstance(providers, dict) or not name:
            return None
        return providers.get(name)

    # ── Disk cache ────────────────────────────────────────────────────

    def _load_disk_cache(self) -> None:
        """Restore the last successful reference catalog (cold start).

        Entries are restored only when the persisted ``source`` signature still
        matches the currently configured sources, so changing a url drops the
        stale catalog (exactly like the cloud catalog's endpoint-signature
        check).  The merged views are recomputed from the restored raw entries
        so the in-memory shape always matches this revision's field contract.
        """
        self._source_entries = {}
        self._source_fetched_at = {}
        self._entries = {}
        self._metadata = {}
        self._origin = {}
        try:
            if not self._cache_file.exists():
                return
            raw = json.loads(self._cache_file.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                return
            if raw.get("source") != self._source_signature():
                logger.info(
                    "📚 Reference catalog cache %s is stale (source urls changed); dropping",
                    self._cache_file,
                )
                return
            by_source = raw.get("source_entries")
            if isinstance(by_source, dict) and by_source:
                # The signature only covers urls, so a source that was *renamed*
                # would restore nothing and silently serve an empty reference
                # catalog until the TTL elapsed.  Keep only names that still
                # exist; if none do, the restored maps stay empty and the next
                # ensure_all_fresh refetches instead.
                current_names = {str(source.get("name")) for source in self._sources}
                for name, entries in by_source.items():
                    if not isinstance(name, str) or name not in current_names:
                        continue
                    if not isinstance(entries, dict):
                        continue
                    clean = {str(k): v for k, v in entries.items() if isinstance(v, dict)}
                    if clean:
                        self._source_entries[name] = clean
            if not self._source_entries:
                return
            fetched = raw.get("source_fetched_at")
            if isinstance(fetched, dict):
                for name, value in fetched.items():
                    if isinstance(name, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                        self._source_fetched_at[name] = float(value)
            # Normalised metadata is recomputed from the raw entries so the
            # in-memory shape always matches this revision's field contract.
            self._merge_sources()
            logger.info(
                "📚 Restored reference catalog from %s (%d model(s))",
                self._cache_file,
                len(self._entries),
            )
        except Exception as exc:
            logger.debug("Reference catalog disk cache not restored: %s", exc)

    def _persist_cache(self) -> None:
        if not self._entries:
            return
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": max(self._source_fetched_at.values(), default=0.0),
                # Joined source urls: the cache is only valid for this set.
                "source": self._source_signature(),
                # Per-source raw entries + their own fetch times.  The merged
                # entries/metadata/origin views are DERIVED from these on load
                # (`_merge_sources`), so persisting them too would only add a
                # second copy that can drift.
                "source_entries": self._source_entries,
                "source_fetched_at": self._source_fetched_at,
            }
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            logger.warning("⚠️  Failed to persist reference catalog cache: %s", exc)

    def reload(self) -> None:
        """Re-read the configured sources and the disk cache (hot-reload aware).

        Called from the config-reload path: a changed ``reference_catalog``
        block is picked up without a restart.  When the source urls changed, the
        signature check drops both the disk cache and the in-memory entries, so
        the next :meth:`ensure_all_fresh` refetches them.
        """
        self._load_sources()
        self._load_disk_cache()

    # ── Fetching ──────────────────────────────────────────────────────

    def _source_timeout(self, source: dict[str, Any]) -> float:
        provider = self._provider(source.get("provider"))
        raw = getattr(provider, "timeout_seconds", None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) <= 0:
            return DEFAULT_FETCH_TIMEOUT_SECONDS
        return min(max(float(raw), 1.0), DEFAULT_FETCH_TIMEOUT_SECONDS)

    def _headers_for(self, source: dict[str, Any]) -> dict[str, str]:
        """Headers for one source fetch; anonymous unless ``send_api_key``."""
        headers: dict[str, str] = {"Accept": "application/json"}
        if not source.get("send_api_key"):
            return headers
        provider = self._provider(source.get("provider"))
        if provider is None or not getattr(provider, "is_configured", False):
            logger.warning(
                "⚠️  Reference source '%s' requests send_api_key but provider '%s' has no credential; fetching anonymously",
                source.get("name"),
                source.get("provider") or "-",
            )
            return headers
        headers.update(ProviderRegistry.build_forward_headers(provider))
        return headers

    async def _fetch_source(self, source: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
        """Fetch one source; ``None`` on any failure (fail-open, never raises)."""
        name = str(source.get("name"))
        try:
            async with httpx.AsyncClient(timeout=self._source_timeout(source)) as client:
                response = await client.get(str(source.get("url")), headers=self._headers_for(source))
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning(
                "⚠️  Reference catalog fetch failed for source '%s' (%s); keeping last successful catalog",
                name,
                exc,
            )
            return None
        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            logger.warning(
                "⚠️  Reference source '%s' returned no 'data' list; keeping last successful catalog",
                name,
            )
            return None
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identity_key = entry.get("id")
            if not isinstance(identity_key, str) or not identity_key.strip():
                continue
            result[identity_key.strip()] = entry
        if not result:
            logger.warning(
                "⚠️  Reference source '%s' returned an empty catalog; keeping last successful catalog",
                name,
            )
            return None
        return result

    async def _refresh(self, *, force: bool) -> None:
        if not self._sources:
            return
        refreshed = False
        for source in self._sources:
            name = str(source.get("name"))
            if not force and not self._is_source_stale(source):
                continue
            try:
                entries = await self._fetch_source(source)
            except Exception as exc:  # defence in depth: never break the caller
                logger.warning("⚠️  Reference catalog refresh for '%s' failed: %s", name, exc)
                continue
            if entries is None:
                # Fail-open: this source keeps its previous entries (or stays
                # absent) and the other sources stay untouched.
                continue
            self._source_entries[name] = entries
            self._source_fetched_at[name] = time.time()
            refreshed = True
        if not refreshed:
            logger.debug("📚 Reference catalog: no source refreshed (%d configured)", len(self._sources))
            return
        self._merge_sources()
        self._persist_cache()
        logger.info(
            "📚 Reference catalog refreshed: %d model(s) from %d source(s)",
            len(self._entries),
            len(self._source_entries),
        )

    async def ensure_all_fresh(self) -> None:
        """TTL-gated refresh: refetch only the sources whose TTL elapsed."""
        try:
            await self._refresh(force=False)
        except Exception as exc:  # pragma: no cover - defensive fail-open
            logger.warning("⚠️  Reference catalog ensure_all_fresh failed: %s", exc)

    async def refresh_all(self) -> None:
        """Refetch every enabled source now (manual/administrative refresh)."""
        try:
            await self._refresh(force=True)
        except Exception as exc:  # pragma: no cover - defensive fail-open
            logger.warning("⚠️  Reference catalog refresh_all failed: %s", exc)

    # ── Merging / queries ─────────────────────────────────────────────

    def _merge_sources(self) -> None:
        """Rebuild the merged views in source declaration order."""
        entries: dict[str, dict[str, Any]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        origin: dict[str, str] = {}
        for source in self._sources:
            name = str(source.get("name"))
            for identity_key, entry in (self._source_entries.get(name) or {}).items():
                normalized = CloudModelCatalog.extract_model_metadata(entry)
                if identity_key in entries:
                    # Later sources only fill the gaps the earlier ones left.
                    entries[identity_key] = merge_metadata_gaps(entries[identity_key], entry)
                    metadata[identity_key] = merge_metadata_gaps(metadata[identity_key], normalized)
                else:
                    entries[identity_key] = entry
                    metadata[identity_key] = normalized
                    origin[identity_key] = name
        self._entries = entries
        self._metadata = metadata
        self._origin = origin

    def _is_source_stale(self, source: dict[str, Any]) -> bool:
        fetched_at = self._source_fetched_at.get(str(source.get("name")))
        if fetched_at is None:
            return True
        ttl = source.get("ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or float(ttl) <= 0:
            ttl = self._ttl_seconds
        return (time.time() - fetched_at) > float(ttl)

    def is_stale(self) -> bool:
        """True when at least one configured source needs a (re)fetch."""
        if not self._sources:
            return False
        return any(self._is_source_stale(source) for source in self._sources)

    def lookup(self, identity_key: str) -> dict[str, Any] | None:
        """Return the raw (merged) upstream entry for *identity_key*, else None."""
        entry = self._entries.get(identity_key)
        return dict(entry) if isinstance(entry, dict) else None

    def metadata(self, identity_key: str) -> dict[str, Any]:
        """Return the normalised parity subset for *identity_key* (spec §3 shape).

        Empty dict when the identity key is unknown.  Non-empty results always
        carry every key of
        :data:`app.proxy.cloud_catalog.METADATA_FIELDS`.
        """
        entry = self._metadata.get(identity_key)
        return dict(entry) if isinstance(entry, dict) else {}

    def source_name(self, identity_key: str) -> str | None:
        """Return the name of the source that supplied *identity_key*, else None.

        Used for the ``reference:<source-name>`` provenance values of §5.
        """
        name = self._origin.get(identity_key)
        return name if isinstance(name, str) else None

    def count(self) -> int:
        """Return the number of merged reference entries."""
        return len(self._entries)
