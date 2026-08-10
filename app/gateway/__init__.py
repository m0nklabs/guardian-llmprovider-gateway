"""Gateway package — shared helpers extracted from the monolithic server.py.

Phase 5 (Structural Separation) extracts cross-cutting concerns into
focused modules so that the FastAPI router in ``app.proxy.server`` becomes
a thin presentation layer.
"""

from app.gateway.context_metadata import (
    apply_context_metadata,
    get_loaded_backend_context_window,
    resolve_context_window,
    warn_context_fallback,
    enrich_model_context_metadata,
    build_model_metadata_entry,
    init as init_context_metadata,
)

__all__ = [
    "apply_context_metadata",
    "get_loaded_backend_context_window",
    "resolve_context_window",
    "warn_context_fallback",
    "enrich_model_context_metadata",
    "build_model_metadata_entry",
    "init_context_metadata",
]
