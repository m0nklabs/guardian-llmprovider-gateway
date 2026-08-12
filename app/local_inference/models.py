"""Local model helpers — resolution, size heuristics, timeouts, VRAM scheduler.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Owns inference-model resolution (auto/alias/cloud/per-key routes, unserved
rejection), model size heuristics, config-tier timeouts, and the VRAM
acquisition scheduler shared by the local inference path.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections import defaultdict
from typing import Any, Dict, Optional

from fastapi import HTTPException

logger = logging.getLogger("Guardian")


# ── Injected (set once at startup by init()) ─────────────────────────
_model_manager = None
_provider_registry = None
_parse_guardian_route = None
_config: Dict[str, Any] = {}
_safe_vram_limit_mb = 0
_model_switch_lock = None
_reset_startup_check_status = None
_run_guardian_operation = None
_ModelLoadError = None


def init(
    *,
    model_manager,
    provider_registry,
    parse_guardian_route,
    config: Dict[str, Any],
    safe_vram_limit_mb: int,
    model_switch_lock,
    reset_startup_check_status,
    run_guardian_operation,
    model_load_error_cls,
) -> None:
    """Inject all dependencies. Called once at startup."""
    global _model_manager, _provider_registry, _parse_guardian_route, _config, _safe_vram_limit_mb
    global _model_switch_lock, _reset_startup_check_status, _run_guardian_operation, _ModelLoadError
    _model_manager = model_manager
    _provider_registry = provider_registry
    _parse_guardian_route = parse_guardian_route
    _config = config
    _safe_vram_limit_mb = safe_vram_limit_mb
    _model_switch_lock = model_switch_lock
    _reset_startup_check_status = reset_startup_check_status
    _run_guardian_operation = run_guardian_operation
    _ModelLoadError = model_load_error_cls


def resolve_inference_model(raw_model: Optional[str], current_model: str) -> Optional[str]:
    if not raw_model:
        return raw_model
    if raw_model == "auto":
        preferred = _model_manager.get_preferred_tool_model(current_model)
        if preferred and preferred != "__MISMATCH__":
            return preferred
        return _model_manager.resolve_reload_target(current_model)
    try:
        return _model_manager.resolve_model(raw_model)
    except ValueError:
        return raw_model


def reject_unserved_inference_model(raw_model: Optional[str]) -> None:
    """Raise a client-facing error for a model Guardian does not serve."""
    requested_model = str(raw_model or "").strip() or "(missing)"
    raise HTTPException(
        status_code=404,
        detail={
            "error": "model_not_served",
            "reason": "requested_model_not_served",
            "message": f"Model '{requested_model}' is not configured in Guardian and cannot be served.",
            "requested_model": requested_model,
            "hint": "Use /v1/models to discover the models currently served by Guardian.",
        },
    )


def resolve_or_reject_inference_model(raw_model: Optional[str], current_model: str) -> str:
    """Resolve an inference model name and reject unknown or unserved values.

    Cloud-provider models (OpenRouter, NVIDIA, …) are accepted as-is so they
    can be forwarded to their upstream API instead of the local backend.

    Per-key cloud routes using the ``guardian/{provider}/{model}`` convention
    are also accepted — the actual upstream model name is extracted at
    forwarding time once the requesting client's linked credential is known.
    """
    resolved_model = resolve_inference_model(raw_model, current_model)
    if not resolved_model or resolved_model == "__MISMATCH__":
        reject_unserved_inference_model(raw_model)
    if resolved_model in _model_manager.models:
        return resolved_model
    if _provider_registry.is_cloud_model(resolved_model):
        return resolved_model
    # Per-key cloud route: guardian/{provider}/{model_path}
    if _parse_guardian_route(resolved_model) is not None:
        return resolved_model
    reject_unserved_inference_model(raw_model)


def resolve_auto_reload_model(requested_model: Optional[str] = None) -> str:
    """Resolve the model Guardian should load when the backend is absent."""
    return _model_manager.resolve_reload_target(requested_model)


def get_gpu_metrics():
    try:
        result = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=memory.used,memory.free,memory.total', '--format=csv,nounits,noheader'],
            encoding='utf-8'
        )
        lines = result.strip().split('\n')
        total_used = 0
        total_free = 0
        total_cap = 0
        for line in lines:
            u, f, t = map(int, line.split(','))
            total_used += u
            total_free += f
            total_cap += t
        return {'used': total_used, 'free': total_free, 'total': total_cap}
    except Exception as e:
        logger.error(f"Failed to get GPU metrics: {e}")
        return {'used': 0, 'free': _safe_vram_limit_mb, 'total': _safe_vram_limit_mb}

def get_model_size(model_name: str) -> int:
    if not model_name: return 0
    model_lower = model_name.lower()
    # Specific overrides for new models
    if "glm-4" in model_lower: return 26000  # ~24GB
    if "35b" in model_lower: return 22000
    if "31b" in model_lower: return 20000
    if "qwen3" in model_lower and "30b" in model_lower: return 20000 # ~18GB
    if "deepseek-r1" in model_lower and "32b" in model_lower: return 22000 # ~19GB
    
    # Generic heuristics
    if "70b" in model_lower: return 40000
    if "32b" in model_lower: return 20000
    if "30b" in model_lower: return 20000
    if "27b" in model_lower: return 18000
    if "13b" in model_lower: return 10000
    if "14b" in model_lower: return 11000
    if "8b" in model_lower: return 6000
    if "7b" in model_lower: return 5000
    if "1.5b" in model_lower: return 1500
    
    # Small models
    if "0.5b" in model_lower: return 600
    if "embed" in model_lower: return 500
    
    # Default fallback
    return 4000

def get_model_timeout(model_name: str) -> int:
    """Calculate timeout based on model size using config tiers.
    
    Tiers are configurable in config/settings.yaml under 'timeouts.tiers'.
    Each tier has min_size_mb and timeout_seconds.
    """
    size = get_model_size(model_name)
    timeout_config = _config.get("timeouts", {})
    tiers = timeout_config.get("tiers", {})
    default_timeout = timeout_config.get("default_timeout", 300)
    
    # Sort tiers by min_size_mb descending to match largest first
    sorted_tiers = sorted(
        tiers.items(),
        key=lambda x: x[1].get("min_size_mb", 0),
        reverse=True
    )
    
    for tier_name, tier_config in sorted_tiers:
        min_size = tier_config.get("min_size_mb", 0)
        timeout = tier_config.get("timeout_seconds", default_timeout)
        
        if size >= min_size:
            logger.debug(f"Model {model_name} ({size}MB) matched tier '{tier_name}' -> {timeout}s timeout")
            return timeout
    
    # Fallback to default
    logger.debug(f"Model {model_name} ({size}MB) using default timeout -> {default_timeout}s")
    return default_timeout


# VramScheduler
class VramScheduler:

    def __init__(self, limit_mb):
        self.limit_mb = limit_mb
        self.active_counts = defaultdict(int) # model -> count
        self.condition = asyncio.Condition()

    async def acquire(self, model_name, model_size_mb):
        async with self.condition:
            while True:
                # Calculate what VRAM would be if we proceed
                current_active_models = [m for m, c in self.active_counts.items() if c > 0]
                
                needed_vram = 0
                for m in current_active_models:
                    needed_vram += get_model_size(m)
                
                # If this model is NOT already active, we need to add its size
                if model_name not in current_active_models:
                    needed_vram += model_size_mb
                
                if needed_vram <= self.limit_mb:
                    self.active_counts[model_name] += 1
                    logger.info(f"VRAM Acquired for {model_name}. Active: {current_active_models + [model_name] if model_name not in current_active_models else current_active_models}")
                    return # Success
                
                # Wait
                logger.info(f"Wait: {model_name} ({model_size_mb}MB) needs space. Active: {current_active_models} (Total: {needed_vram}MB > {self.limit_mb}MB)")
                await self.condition.wait()

    async def release(self, model_name):
        async with self.condition:
            self.active_counts[model_name] -= 1
            if self.active_counts[model_name] <= 0:
                del self.active_counts[model_name]
            self.condition.notify_all()
            logger.info(f"VRAM Released for {model_name}.")

async def reload_backend_after_connect_error(path: str, error: Exception) -> None:
    """Reload llama-server once after Guardian detects stale backend state."""
    current_model = await _model_manager.get_current_model()
    reload_model = resolve_auto_reload_model(current_model)
    logger.warning(
        f"⚠️ Backend unreachable while proxying /v1/{path}; "
        f"reloading '{reload_model}' once before retry: {error}"
    )

    async with _model_switch_lock:
        if await _model_manager.backend_health_ok():
            _model_manager.is_unloaded = False
            logger.info("✅ Backend became healthy before retry")
            return

        _model_manager.is_unloaded = True
        try:
            generation = _reset_startup_check_status(
                source="proxy",
                phase="backend_reload",
                target_model=reload_model,
                requested_model=current_model,
                owner="backend_recovery",
            )
            await _run_guardian_operation(
                source="proxy",
                phase="backend_reload",
                target_model=reload_model,
                requested_model=current_model,
                owner="backend_recovery",
                operation=lambda: _model_manager.load(reload_model),
                generation=generation,
            )
        except _ModelLoadError as e:
            crash = e.crash_record
            detail = {
                "error": f"Backend reload failed for '{reload_model}'",
                "message": str(e),
                "crash_details": crash.to_dict() if crash else None,
            }
            logger.error(f"💥 Backend reload crash: {detail}")
            raise HTTPException(status_code=503, detail=detail)
        except Exception as e:
            logger.error(f"❌ Backend reload failed after connect error: {e}")
            raise HTTPException(status_code=503, detail=f"Backend reload failed: {e}")

