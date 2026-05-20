"""Guardian-native model finetuning helpers.

This module finds the highest stable runtime context for a configured model while
also exploring `ngl` and two-GPU tensor split candidates. It uses Guardian's own
`/admin/load` and `/v1/chat/completions` endpoints so the measured result matches
real `models.yaml` behavior.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import yaml

logger = logging.getLogger("model-finetune")

DEFAULT_SMOKE_PROMPT = "Reply with exactly: FIT OK"
DEFAULT_SPLIT_CALIBRATION_CONTEXT = 16384
DEFAULT_VRAM_BALANCE_THRESHOLD_PCT = 5.0
DEFAULT_MIN_FREE_MIB_FOR_COARSE_SPLIT_SHIFT = 1024.0
DEFAULT_LOW_HEADROOM_DUAL_GPU_FREE_MIB = 500.0
DEFAULT_CRITICAL_HEADROOM_SINGLE_GPU_FREE_MIB = 100.0
DEFAULT_OPTIMIZATION_MODE = "balanced"
VALID_OPTIMIZATION_MODES = {"speed", "context", "balanced"}


def detect_oom_gpu(error: Optional[str]) -> Optional[int]:
    """Infer which GPU hit OOM from a Guardian/llama.cpp error string."""
    if not error:
        return None
    for pattern in (r"CUDA([01])", r"device\s+([01])"):
        match = re.search(pattern, error, flags=re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
    return None


def next_split_candidates_after_oom(
    tensor_split: Optional[str],
    *,
    failed_gpu: Optional[int],
    step: float,
    split_min: float,
    split_max: float,
) -> List[str]:
    """Shift the primary split away from the GPU that failed and return the next candidates."""
    current_primary = parse_two_gpu_split(tensor_split) or 0.5
    directions: List[int]
    if failed_gpu == 1:
        directions = [1, -1]
    elif failed_gpu == 0:
        directions = [-1, 1]
    else:
        directions = [1, -1] if current_primary >= 0.5 else [-1, 1]

    candidates: List[str] = []
    for direction in directions:
        candidate_primary = round(current_primary + (direction * step), 2)
        if not split_min <= candidate_primary <= split_max:
            continue
        candidate_split = format_two_gpu_split(candidate_primary)
        if candidate_split == tensor_split or candidate_split in candidates:
            continue
        candidates.append(candidate_split)
    return candidates


def read_gpu_vram_snapshot() -> Optional[Dict[str, Dict[str, float]]]:
    """Read per-GPU used/free/total VRAM from nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None

    snapshot: Dict[str, Dict[str, float]] = {}
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        idx, used, free, total = parts
        try:
            used_value = float(used)
            free_value = float(free)
            total_value = float(total)
        except ValueError:
            continue
        free_pct = (free_value / total_value * 100.0) if total_value > 0 else 0.0
        snapshot[idx] = {
            "used": used_value,
            "free": free_value,
            "total": total_value,
            "free_pct": free_pct,
        }
    return snapshot or None


def free_vram_delta_pct(gpu_vram: Optional[Dict[str, Dict[str, float]]]) -> Optional[float]:
    """Return the absolute free-VRAM percentage difference across the first two GPUs."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    gpu_indices = sorted(gpu_vram.keys(), key=int)
    first = gpu_vram[gpu_indices[0]].get("free_pct")
    second = gpu_vram[gpu_indices[1]].get("free_pct")
    if first is None or second is None:
        return None
    return abs(float(first) - float(second))


def next_split_from_vram_balance(
    tensor_split: Optional[str],
    *,
    gpu_vram: Optional[Dict[str, Dict[str, float]]],
    step: float,
    split_min: float,
    split_max: float,
) -> Optional[str]:
    """Shift split toward the GPU with more free VRAM until free percentages converge."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    gpu_indices = sorted(gpu_vram.keys(), key=int)
    first_free = float(gpu_vram[gpu_indices[0]].get("free_pct", 0.0))
    second_free = float(gpu_vram[gpu_indices[1]].get("free_pct", 0.0))
    if math.isclose(first_free, second_free, abs_tol=0.01):
        return None

    current_primary = parse_two_gpu_split(tensor_split) or 0.5
    direction = 1 if first_free > second_free else -1
    candidate_primary = round(current_primary + (direction * step), 2)
    if not split_min <= candidate_primary <= split_max:
        return None
    candidate_split = format_two_gpu_split(candidate_primary)
    if candidate_split == tensor_split:
        return None
    return candidate_split


def smaller_split_step(step: float) -> Optional[float]:
    """Return the next smaller split step, down to a 1% minimum increment."""
    if step <= 0.01:
        return None
    halved = round(step / 2.0, 2)
    if halved >= step:
        return None
    return max(0.01, halved)


def target_gpu_free_mib_for_balance_shift(gpu_vram: Optional[Dict[str, Dict[str, float]]]) -> Optional[float]:
    """Return free MiB on the GPU that would receive more load from the next rebalance step."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    gpu_indices = sorted(gpu_vram.keys(), key=int)
    first = gpu_vram[gpu_indices[0]]
    second = gpu_vram[gpu_indices[1]]
    first_free_pct = first.get("free_pct")
    second_free_pct = second.get("free_pct")
    if first_free_pct is None or second_free_pct is None:
        return None
    target = first if float(first_free_pct) > float(second_free_pct) else second
    total_mib = target.get("total")
    if total_mib is None or float(total_mib) < 2048.0:
        return None
    free_mib = target.get("free")
    return float(free_mib) if free_mib is not None else None


def two_gpu_free_mib(gpu_vram: Optional[Dict[str, Dict[str, float]]]) -> Optional[Tuple[float, float]]:
    """Return real MiB headroom for the first two GPUs when telemetry is in MiB scale."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    values: List[float] = []
    for gpu_index in sorted(gpu_vram.keys(), key=int)[:2]:
        gpu_stats = gpu_vram[gpu_index]
        total_mib = gpu_stats.get("total")
        free_mib = gpu_stats.get("free")
        if total_mib is None or free_mib is None or float(total_mib) < 2048.0:
            return None
        values.append(float(free_mib))
    return values[0], values[1]


def resolve_headroom_context_granularity(
    gpu_vram: Optional[Dict[str, Dict[str, float]]],
    *,
    base_granularity: int,
) -> int:
    """Tighten local context bisection when headroom is critically low."""
    free_values = two_gpu_free_mib(gpu_vram)
    if free_values is None:
        return base_granularity
    if min(free_values) < DEFAULT_CRITICAL_HEADROOM_SINGLE_GPU_FREE_MIB:
        return max(512, base_granularity // 4)
    if all(free_mib < DEFAULT_LOW_HEADROOM_DUAL_GPU_FREE_MIB for free_mib in free_values):
        return max(1024, base_granularity // 2)
    return base_granularity


def should_limit_large_context_jumps(gpu_vram: Optional[Dict[str, Dict[str, float]]]) -> bool:
    """Return True when broad frontier jumps are unlikely to add signal at low VRAM headroom."""
    free_values = two_gpu_free_mib(gpu_vram)
    if free_values is None:
        return False
    if min(free_values) < DEFAULT_CRITICAL_HEADROOM_SINGLE_GPU_FREE_MIB:
        return True
    return all(free_mib < DEFAULT_LOW_HEADROOM_DUAL_GPU_FREE_MIB for free_mib in free_values)


def should_skip_coarse_split_shift(
    gpu_vram: Optional[Dict[str, Dict[str, float]]],
    *,
    step: float,
    min_free_mib: float = DEFAULT_MIN_FREE_MIB_FOR_COARSE_SPLIT_SHIFT,
) -> bool:
    """Return True when a 2% rebalance shift is too unlikely to fit to be worth probing."""
    if step < 0.02:
        return False
    target_free_mib = target_gpu_free_mib_for_balance_shift(gpu_vram)
    if target_free_mib is None:
        return False
    return target_free_mib < min_free_mib


@dataclass(slots=True)
class ProbeResult:
    """Outcome of a single Guardian load probe."""

    model: str
    context: int
    ngl: int
    tensor_split: Optional[str]
    success: bool
    load_seconds: float
    smoke_seconds: float = 0.0
    status_code: Optional[int] = None
    error: Optional[str] = None
    response_excerpt: Optional[str] = None
    gpu_vram: Optional[Dict[str, Dict[str, float]]] = None
    free_vram_delta_pct: Optional[float] = None
    model_signature: Optional[str] = None
    smoke_signature: Optional[str] = None
    cached: bool = False

    @property
    def total_seconds(self) -> float:
        """Return total wall-clock duration for the probe."""
        return self.load_seconds + self.smoke_seconds


@dataclass(slots=True)
class TuneResult:
    """Final recommendation from a finetune run."""

    model: str
    original_context: Optional[int]
    original_ngl: Optional[int]
    original_tensor_split: Optional[str]
    runtime_mode: str
    search_min_context: int
    search_max_context: int
    recommended_context: int
    recommended_ngl: int
    recommended_tensor_split: Optional[str]
    benchmark_context_limit: Optional[int]
    optimization: str = DEFAULT_OPTIMIZATION_MODE
    attempts: List[ProbeResult] = field(default_factory=list)
    coarse_ngl_candidates: List[int] = field(default_factory=list)
    refined_ngl_candidates: List[int] = field(default_factory=list)
    coarse_candidates: List[Optional[str]] = field(default_factory=list)
    refined_candidates: List[Optional[str]] = field(default_factory=list)
    applied: bool = False
    model_signature: Optional[str] = None
    smoke_signature: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        """Serialize the result for JSON output."""
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "model": self.model,
            "original_context": self.original_context,
            "original_ngl": self.original_ngl,
            "original_tensor_split": self.original_tensor_split,
            "runtime_mode": self.runtime_mode,
            "optimization": self.optimization,
            "search_min_context": self.search_min_context,
            "search_max_context": self.search_max_context,
            "recommended_context": self.recommended_context,
            "recommended_ngl": self.recommended_ngl,
            "recommended_tensor_split": self.recommended_tensor_split,
            "benchmark_context_limit": self.benchmark_context_limit,
            "coarse_ngl_candidates": self.coarse_ngl_candidates,
            "refined_ngl_candidates": self.refined_ngl_candidates,
            "coarse_candidates": self.coarse_candidates,
            "refined_candidates": self.refined_candidates,
            "applied": self.applied,
            "model_signature": self.model_signature,
            "smoke_signature": self.smoke_signature,
            "attempts": [asdict(attempt) for attempt in self.attempts],
        }


def build_smoke_messages(smoke_prompt: str, smoke_image_url: Optional[str] = None) -> List[Dict[str, object]]:
    """Build the minimal post-load smoke-test message list."""
    if smoke_image_url:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": smoke_image_url}},
                    {"type": "text", "text": smoke_prompt},
                ],
            }
        ]
    return [{"role": "user", "content": smoke_prompt}]


def build_model_signature(model_name: str, model_config: Dict[str, object]) -> str:
    """Create a stable cache signature for a model independent of tuned values."""
    signature_config = {
        key: value
        for key, value in model_config.items()
        if key not in {
            "context",
            "ngl",
            "tensor_split",
            "benchmark_context_limit",
            "text_context",
            "text_ngl",
            "text_tensor_split",
            "vision_context",
            "vision_ngl",
            "vision_tensor_split",
        }
    }
    payload = {"model": model_name, "config": signature_config}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_smoke_signature(
    smoke_prompt: str,
    smoke_max_tokens: int,
    smoke_image_url: Optional[str],
    runtime_mode: str,
) -> str:
    """Create a stable cache signature for the current smoke probe shape.

    Exact marker text should not invalidate a load-fit cache entry. For fit reuse,
    the rough prompt footprint matters more than the literal success token string.
    """
    prompt_length_bucket = max(64, min(1024, ((len(smoke_prompt.strip()) + 63) // 64) * 64))
    payload = {
        "prompt_length_bucket": prompt_length_bucket,
        "max_tokens": smoke_max_tokens,
        "image_url": smoke_image_url,
        "runtime_mode": runtime_mode,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def resolve_optimization_mode(optimization: str) -> str:
    """Normalize and validate the requested finetune optimization mode."""
    normalized = optimization.strip().lower()
    if normalized not in VALID_OPTIMIZATION_MODES:
        valid = ", ".join(sorted(VALID_OPTIMIZATION_MODES))
        raise ValueError(f"optimization must be one of: {valid}")
    return normalized


def balance_metric(result: ProbeResult) -> float:
    """Return the measured split-balance quality, preferring live VRAM data over 50/50 distance."""
    if result.free_vram_delta_pct is not None:
        return float(result.free_vram_delta_pct)
    return split_balance_distance(result.tensor_split)


def normalized_ratio(value: int, ceiling: Optional[int]) -> float:
    """Normalize a non-negative integer against its search ceiling."""
    if ceiling is None or ceiling <= 0:
        return 0.0
    return min(max(float(value) / float(ceiling), 0.0), 1.0)


def balanced_tradeoff_score(
    candidate: ProbeResult,
    *,
    max_context: Optional[int],
    max_ngl: Optional[int],
) -> float:
    """Return a harmonic-mean tradeoff score for context and ngl equilibrium."""
    context_ratio = normalized_ratio(candidate.context, max_context)
    ngl_ratio = normalized_ratio(candidate.ngl, max_ngl)
    if context_ratio <= 0.0 or ngl_ratio <= 0.0:
        return 0.0
    return (2.0 * context_ratio * ngl_ratio) / (context_ratio + ngl_ratio)


def resolve_optimization_defaults(
    *,
    original_context: Optional[int],
    benchmark_context_limit: Optional[int],
    granularity: int,
    auto_context_floor_ratio: float,
    optimization: str,
) -> Tuple[int, int, int, int]:
    """Resolve automatic context and ngl search bounds for the requested optimization mode."""
    lower_bound, upper_bound = resolve_context_bounds(
        original_context=original_context,
        benchmark_context_limit=benchmark_context_limit,
        min_context=None,
        max_context=None,
        granularity=granularity,
        auto_context_range=True,
        auto_context_floor_ratio=auto_context_floor_ratio,
    )
    if optimization == "speed":
        return lower_bound, upper_bound, 0, 99
    if optimization == "context":
        return lower_bound, upper_bound, 0, 99
    return lower_bound, upper_bound, 0, 99


def resolve_runtime_mode(runtime_mode: str, smoke_image_url: Optional[str]) -> str:
    """Resolve `auto` finetune mode to the effective text or vision runtime."""
    normalized = runtime_mode.strip().lower()
    if normalized == "auto":
        return "vision" if smoke_image_url else "text"
    if normalized not in {"text", "vision"}:
        raise ValueError("runtime_mode must be one of: auto, text, vision")
    return normalized


def runtime_mode_uses_vision(runtime_mode: str) -> bool:
    """Return whether the finetune run targets Guardian's vision runtime."""
    return runtime_mode == "vision"


def has_vision_runtime(model_config: Dict[str, object]) -> bool:
    """Return whether the model has an mmproj-backed vision path."""
    mmproj = str(model_config.get("vision_mmproj") or model_config.get("mmproj") or "").strip()
    return bool(mmproj)


def resolve_runtime_config_value(model_config: Dict[str, object], key: str, runtime_mode: str) -> object:
    """Return the effective config value for the requested finetune runtime."""
    override_key = f"{runtime_mode}_{key}"
    override_value = model_config.get(override_key)
    if override_value not in (None, ""):
        return override_value
    return model_config.get(key)


def apply_runtime_search_values(
    model_config: Dict[str, object],
    *,
    context: int,
    ngl: int,
    tensor_split: Optional[str],
    runtime_mode: str,
) -> Dict[str, object]:
    """Apply tuned fields to the correct text or vision config keys."""
    target = copy.deepcopy(model_config)
    if runtime_mode_uses_vision(runtime_mode) and has_vision_runtime(target):
        prefix = "vision"
    elif runtime_mode == "text" and any(
        target.get(f"text_{field}") not in (None, "") for field in ("context", "ngl", "tensor_split")
    ):
        prefix = "text"
    else:
        prefix = None

    if prefix is None:
        target["context"] = int(context)
        target["ngl"] = int(ngl)
        if tensor_split:
            target["tensor_split"] = tensor_split
        else:
            target.pop("tensor_split", None)
        return target

    target[f"{prefix}_context"] = int(context)
    target[f"{prefix}_ngl"] = int(ngl)
    if tensor_split:
        target[f"{prefix}_tensor_split"] = tensor_split
    else:
        target.pop(f"{prefix}_tensor_split", None)
    return target


def build_probe_cache_key(
    model_name: str,
    context: int,
    ngl: int,
    tensor_split: Optional[str],
    model_signature: str,
    smoke_signature: str,
) -> Tuple[str, int, int, Optional[str], str, str]:
    """Build the durable cache key for one probe combination."""
    return (model_name, int(context), int(ngl), tensor_split, model_signature, smoke_signature)


def index_cached_probes(
    history: Sequence[Dict[str, object]],
    *,
    model_name: str,
    model_signature: str,
    smoke_signature: str,
    runtime_mode: Optional[str] = None,
) -> Dict[Tuple[str, int, int, Optional[str], str, str], ProbeResult]:
    """Index compatible historical probes from the finetune results file."""
    indexed: Dict[Tuple[str, int, int, Optional[str], str, str], ProbeResult] = {}

    def merge_cached_probe(existing: ProbeResult, candidate: ProbeResult) -> ProbeResult:
        """Keep the most informative cached probe when history contains duplicates."""
        merged = copy.deepcopy(candidate)
        if merged.gpu_vram is None and existing.gpu_vram is not None:
            merged.gpu_vram = copy.deepcopy(existing.gpu_vram)
        if merged.free_vram_delta_pct is None and existing.free_vram_delta_pct is not None:
            merged.free_vram_delta_pct = existing.free_vram_delta_pct
        if merged.response_excerpt is None and existing.response_excerpt is not None:
            merged.response_excerpt = existing.response_excerpt
        if merged.error is None and existing.error is not None:
            merged.error = existing.error
        if merged.status_code is None and existing.status_code is not None:
            merged.status_code = existing.status_code
        if merged.gpu_vram is not None and merged.free_vram_delta_pct is None:
            merged.free_vram_delta_pct = free_vram_delta_pct(merged.gpu_vram)
        return merged

    for entry in history:
        if entry.get("model") != model_name:
            continue
        if entry.get("model_signature") != model_signature:
            continue
        smoke_matches = entry.get("smoke_signature") == smoke_signature
        runtime_matches = runtime_mode is not None and entry.get("runtime_mode") == runtime_mode
        if not smoke_matches and not runtime_matches:
            continue
        attempts = entry.get("attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            context = attempt.get("context")
            if not isinstance(context, int):
                continue
            ngl = attempt.get("ngl")
            if not isinstance(ngl, int):
                ngl = entry.get("original_ngl") if isinstance(entry.get("original_ngl"), int) else None
            if not isinstance(ngl, int):
                continue
            gpu_vram = copy.deepcopy(attempt.get("gpu_vram")) if isinstance(attempt.get("gpu_vram"), dict) else None
            cached_free_vram_delta_pct = attempt.get("free_vram_delta_pct")
            if cached_free_vram_delta_pct is None and gpu_vram is not None:
                cached_free_vram_delta_pct = free_vram_delta_pct(gpu_vram)
            tensor_split = attempt.get("tensor_split")
            if tensor_split is not None:
                tensor_split = str(tensor_split)
            probe = ProbeResult(
                model=str(attempt.get("model") or model_name),
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=bool(attempt.get("success")),
                load_seconds=float(attempt.get("load_seconds") or 0.0),
                smoke_seconds=float(attempt.get("smoke_seconds") or 0.0),
                status_code=int(attempt["status_code"]) if attempt.get("status_code") is not None else None,
                error=str(attempt["error"]) if attempt.get("error") is not None else None,
                response_excerpt=str(attempt["response_excerpt"]) if attempt.get("response_excerpt") is not None else None,
                gpu_vram=gpu_vram,
                free_vram_delta_pct=(
                    float(cached_free_vram_delta_pct) if cached_free_vram_delta_pct is not None else None
                ),
                model_signature=model_signature,
                smoke_signature=smoke_signature,
                cached=True,
            )
            cache_key = build_probe_cache_key(model_name, context, ngl, tensor_split, model_signature, smoke_signature)
            existing_probe = indexed.get(cache_key)
            indexed[cache_key] = merge_cached_probe(existing_probe, probe) if existing_probe is not None else probe
    return indexed


def build_ngl_candidates(anchor_ngl: Optional[int], step: int, min_ngl: int, max_ngl: int) -> List[int]:
    """Build ordered `ngl` candidates, favoring higher GPU offload first."""
    if step <= 0:
        raise ValueError("ngl step must be > 0")
    if min_ngl < 0 or max_ngl < min_ngl:
        raise ValueError("ngl bounds must satisfy 0 <= min <= max")

    anchor = anchor_ngl if isinstance(anchor_ngl, int) else max_ngl
    anchor = min(max(anchor, min_ngl), max_ngl)

    values: set[int] = {anchor, min_ngl, max_ngl}
    current = min_ngl
    while current <= max_ngl:
        values.add(current)
        current += step

    return sorted(values, reverse=True)


def split_balance_distance(tensor_split: Optional[str]) -> float:
    """Return how far a split is from a perfectly balanced 50/50 split."""
    primary = parse_two_gpu_split(tensor_split)
    if primary is None:
        return 1.0
    return abs(primary - 0.5)


def resolve_context_bounds(
    *,
    original_context: Optional[int],
    benchmark_context_limit: Optional[int],
    min_context: Optional[int],
    max_context: Optional[int],
    granularity: int,
    auto_context_range: bool,
    auto_context_floor_ratio: float,
) -> Tuple[int, int]:
    """Resolve effective context bounds, optionally deriving them automatically."""
    if not 0 < auto_context_floor_ratio <= 1.0:
        raise ValueError("auto_context_floor_ratio must satisfy 0 < ratio <= 1")

    upper_candidate = int(max_context or benchmark_context_limit or original_context or 131072)
    upper_bound = align_context_floor(upper_candidate, granularity)

    if min_context is not None:
        lower_candidate = int(min_context)
    elif auto_context_range:
        anchor_context = int(original_context or upper_bound)
        lower_candidate = max(granularity, int(min(anchor_context, upper_bound) * auto_context_floor_ratio))
    else:
        lower_candidate = granularity

    lower_bound = align_context_ceil(lower_candidate, granularity)
    if lower_bound > upper_bound:
        raise ValueError("resolved min_context is greater than resolved max_context")
    return lower_bound, upper_bound


def align_context_floor(value: int, granularity: int) -> int:
    """Round a context value down to the configured search granularity."""
    if granularity <= 0:
        raise ValueError("granularity must be > 0")
    return max(granularity, (value // granularity) * granularity)


def align_context_ceil(value: int, granularity: int) -> int:
    """Round a context value up to the configured search granularity."""
    if granularity <= 0:
        raise ValueError("granularity must be > 0")
    return max(granularity, math.ceil(value / granularity) * granularity)


def parse_two_gpu_split(tensor_split: Optional[str]) -> Optional[float]:
    """Return the primary-GPU ratio from a two-GPU tensor split string."""
    if not tensor_split:
        return None
    parts = [part.strip() for part in tensor_split.split(",") if part.strip()]
    if len(parts) != 2:
        return None
    try:
        primary = float(parts[0])
        secondary = float(parts[1])
    except ValueError:
        return None
    total = primary + secondary
    if total <= 0:
        return None
    return round(primary / total, 4)


def format_two_gpu_split(primary_ratio: float, decimals: int = 2) -> str:
    """Format a normalized two-GPU tensor split string."""
    bounded_primary = min(max(primary_ratio, 0.0), 1.0)
    rounded_primary = round(bounded_primary, decimals)
    rounded_secondary = round(max(0.0, 1.0 - rounded_primary), decimals)
    return f"{rounded_primary:.{decimals}f},{rounded_secondary:.{decimals}f}"


def build_split_candidates(
    anchor_split: Optional[str],
    step: float,
    min_primary: float,
    max_primary: float,
    *,
    include_auto: bool = False,
) -> List[Optional[str]]:
    """Build ordered two-GPU tensor split candidates, preferring balanced splits first."""
    if step <= 0:
        raise ValueError("step must be > 0")
    if min_primary <= 0 or max_primary >= 1 or min_primary > max_primary:
        raise ValueError("split bounds must satisfy 0 < min <= max < 1")

    anchor_primary = parse_two_gpu_split(anchor_split)
    if anchor_primary is None:
        anchor_primary = 0.55

    values: set[float] = {round(anchor_primary, 2)}
    current = min_primary
    while current <= max_primary + 1e-9:
        values.add(round(current, 2))
        current += step

    ordered = sorted(
        values,
        key=lambda value: (
            round(abs(value - 0.5), 4),
            round(abs(value - anchor_primary), 4),
            value,
        ),
    )
    candidates: List[Optional[str]] = [format_two_gpu_split(value) for value in ordered]
    if include_auto:
        return [*candidates, None]
    return candidates


def resolve_candidate_context_bounds(
    *,
    best_context: Optional[int],
    lower_bound: int,
    upper_bound: int,
    granularity: int,
) -> Tuple[int, int]:
    """Return the only context range worth testing for a new combination."""
    if best_context is None:
        return lower_bound, upper_bound

    aligned_best = align_context_floor(best_context, granularity)
    if aligned_best >= upper_bound:
        return upper_bound, upper_bound

    next_context = align_context_ceil(aligned_best + granularity, granularity)
    if next_context > upper_bound:
        return upper_bound, upper_bound
    return next_context, upper_bound


def choose_better_result(
    current_best: Optional[ProbeResult],
    candidate: Optional[ProbeResult],
    *,
    optimization: str = DEFAULT_OPTIMIZATION_MODE,
    max_context: Optional[int] = None,
    max_ngl: Optional[int] = None,
) -> Optional[ProbeResult]:
    """Return the stronger successful result for the requested optimization mode."""
    if candidate is None or not candidate.success:
        return current_best
    if current_best is None or not current_best.success:
        return candidate
    normalized_optimization = resolve_optimization_mode(optimization)
    candidate_balance = balance_metric(candidate)
    current_balance = balance_metric(current_best)
    if candidate_balance != current_balance:
        return candidate if candidate_balance < current_balance else current_best
    if normalized_optimization == "speed":
        if candidate.ngl != current_best.ngl:
            return candidate if candidate.ngl > current_best.ngl else current_best
        if candidate.context != current_best.context:
            return candidate if candidate.context > current_best.context else current_best
    elif normalized_optimization == "context":
        if candidate.context != current_best.context:
            return candidate if candidate.context > current_best.context else current_best
        if candidate.ngl != current_best.ngl:
            return candidate if candidate.ngl > current_best.ngl else current_best
    else:
        candidate_score = balanced_tradeoff_score(candidate, max_context=max_context, max_ngl=max_ngl)
        current_score = balanced_tradeoff_score(current_best, max_context=max_context, max_ngl=max_ngl)
        if candidate_score != current_score:
            return candidate if candidate_score > current_score else current_best
        if candidate.context != current_best.context:
            return candidate if candidate.context > current_best.context else current_best
        if candidate.ngl != current_best.ngl:
            return candidate if candidate.ngl > current_best.ngl else current_best
    if candidate.total_seconds != current_best.total_seconds:
        return candidate if candidate.total_seconds < current_best.total_seconds else current_best
    return candidate if (candidate.tensor_split or "") < (current_best.tensor_split or "") else current_best


def binary_search_max_success(
    *,
    min_context: int,
    max_context: int,
    granularity: int,
    probe: Callable[[int], bool],
    anchor_context: Optional[int] = None,
) -> Tuple[Optional[int], List[int]]:
    """Find the highest successful context using bounded binary search."""
    low_bound = align_context_ceil(min_context, granularity)
    high_bound = align_context_floor(max_context, granularity)
    if low_bound > high_bound:
        raise ValueError("min_context must be <= max_context after alignment")

    attempts: List[int] = []
    cache: Dict[int, bool] = {}

    def cached_probe(context: int) -> bool:
        if context not in cache:
            cache[context] = probe(context)
            attempts.append(context)
        return cache[context]

    if anchor_context is None:
        seed = low_bound
    else:
        seed = align_context_floor(anchor_context, granularity)
        seed = min(max(seed, low_bound), high_bound)

    if cached_probe(seed):
        if seed == high_bound:
            return seed, attempts
        if cached_probe(high_bound):
            return high_bound, attempts
        low = seed
        high = high_bound
        best = seed
    else:
        if seed == low_bound:
            return None, attempts
        if not cached_probe(low_bound):
            return None, attempts
        low = low_bound
        high = seed
        best = low_bound

    while high - low > granularity:
        mid = align_context_floor((low + high) // 2, granularity)
        if mid <= low:
            mid = low + granularity
        if mid >= high:
            mid = high - granularity
        if cached_probe(mid):
            low = mid
            best = mid
        else:
            high = mid

    return best, attempts


def binary_search_max_int_success(
    *,
    min_value: int,
    max_value: int,
    probe: Callable[[int], bool],
    anchor_value: Optional[int] = None,
) -> Tuple[Optional[int], List[int]]:
    """Find the highest successful integer using bounded binary search."""
    if min_value > max_value:
        raise ValueError("min_value must be <= max_value")

    attempts: List[int] = []
    cache: Dict[int, bool] = {}

    def cached_probe(value: int) -> bool:
        if value not in cache:
            cache[value] = probe(value)
            attempts.append(value)
        return cache[value]

    if anchor_value is None:
        seed = max_value
    else:
        seed = min(max(anchor_value, min_value), max_value)

    if cached_probe(seed):
        if seed == max_value:
            return seed, attempts
        if cached_probe(max_value):
            return max_value, attempts
        low = seed
        high = max_value
        best = seed
    else:
        if seed == min_value:
            return None, attempts
        if not cached_probe(min_value):
            return None, attempts
        low = min_value
        high = seed
        best = min_value

    while high - low > 1:
        mid = (low + high) // 2
        if mid <= low:
            mid = low + 1
        if mid >= high:
            mid = high - 1
        if cached_probe(mid):
            low = mid
            best = mid
        else:
            high = mid

    return best, attempts


def split_candidates_for_distance(
    distance: float,
    *,
    min_primary: float,
    max_primary: float,
    anchor_split: Optional[str],
) -> List[str]:
    """Return split candidates at one balance distance, preferring the anchor side first."""
    anchor_primary = parse_two_gpu_split(anchor_split) or 0.55
    if distance <= 0:
        return [format_two_gpu_split(0.5)]

    candidates: List[float] = []
    upper = round(0.5 + distance, 2)
    lower = round(0.5 - distance, 2)
    if min_primary <= upper <= max_primary:
        candidates.append(upper)
    if min_primary <= lower <= max_primary and lower not in candidates:
        candidates.append(lower)

    if anchor_primary < 0.5:
        candidates.sort()
    else:
        candidates.sort(reverse=True)

    return [format_two_gpu_split(candidate) for candidate in candidates]


def unique_attempt_ngls(attempts: Sequence[ProbeResult]) -> List[int]:
    """Return tested ngl values in first-seen order."""
    ordered: List[int] = []
    for attempt in attempts:
        if attempt.ngl not in ordered:
            ordered.append(attempt.ngl)
    return ordered


def unique_attempt_splits(attempts: Sequence[ProbeResult]) -> List[Optional[str]]:
    """Return tested tensor splits in first-seen order."""
    ordered: List[Optional[str]] = []
    for attempt in attempts:
        if attempt.tensor_split not in ordered:
            ordered.append(attempt.tensor_split)
    return ordered


def _format_yaml_scalar(value: object) -> str:
    """Format a scalar for the hand-written model block renderer."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_model_block(model_name: str, model_config: Dict[str, object]) -> str:
    """Render a single `models.yaml` model block while preserving key order."""
    lines = [f"  {model_name}:"]
    for key, value in model_config.items():
        lines.append(f"    {key}: {_format_yaml_scalar(value)}")
    return "\n".join(lines)


def replace_model_block(config_text: str, model_name: str, replacement_block: str) -> str:
    """Replace exactly one model block inside `models.yaml`."""
    lines = config_text.splitlines()
    start: Optional[int] = None
    end: Optional[int] = None
    header = f"  {model_name}:"
    for index, line in enumerate(lines):
        if line == header:
            start = index
            break
    if start is None:
        raise ValueError(f"Model block '{model_name}' not found")
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line and not line.startswith("    "):
            break
        end += 1
    new_lines = lines[:start] + replacement_block.splitlines() + lines[end:]
    suffix = "\n" if config_text.endswith("\n") else ""
    return "\n".join(new_lines) + suffix


class GuardianModelFinetuner:
    """Tune a configured Guardian model via fast context, split, and ngl search."""

    def __init__(
        self,
        *,
        guardian_url: str,
        api_key: str,
        models_config_path: str,
        results_file: str,
        smoke_prompt: str = DEFAULT_SMOKE_PROMPT,
        smoke_max_tokens: int = 8,
        smoke_image_url: Optional[str] = None,
        runtime_mode: str = "auto",
    ) -> None:
        self.guardian_url = guardian_url.rstrip("/")
        self.models_config_path = Path(models_config_path)
        self.results_file = Path(results_file)
        self.smoke_prompt = smoke_prompt
        self.smoke_max_tokens = smoke_max_tokens
        self.smoke_image_url = smoke_image_url
        self.runtime_mode = resolve_runtime_mode(runtime_mode, smoke_image_url)
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(900.0, connect=10.0),
        )
        self.base_text = self.models_config_path.read_text()
        self.base_config = yaml.safe_load(self.base_text) or {}
        self.result_history = self._load_result_history()
        self.probe_cache: Dict[Tuple[str, int, int, Optional[str], str, str], ProbeResult] = {}
        self._attempt_log: List[ProbeResult] = []
        self._attempt_keys_seen: set[Tuple[str, int, int, Optional[str], str, str]] = set()
        self._active_model_signature: Optional[str] = None
        self._active_result_index: Optional[int] = None
        self._active_smoke_signature = build_smoke_signature(
            self.smoke_prompt,
            self.smoke_max_tokens,
            self.smoke_image_url,
            self.runtime_mode,
        )
        self.original_loaded_model = self._get_current_model()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self.client.close()

    def resolve_model(self, requested_name: str) -> str:
        """Resolve a canonical model name or configured alias."""
        models = self.base_config.get("models", {})
        if requested_name in models:
            return requested_name
        aliases = self.base_config.get("aliases", {})
        if requested_name in aliases:
            target = aliases[requested_name]
            if target in models:
                return target
        requested_lower = requested_name.lower()
        for model_name in models:
            if model_name.lower() == requested_lower:
                return model_name
        raise ValueError(f"Model '{requested_name}' not found in models.yaml")

    def tune_model(
        self,
        model_name: str,
        *,
        granularity: int = 2048,
        auto_context_floor_ratio: float = 0.5,
        optimization: str = DEFAULT_OPTIMIZATION_MODE,
        ngl_candidates: Optional[Sequence[int]] = None,
        ngl_step: int = 16,
        ngl_refine_step: int = 8,
        split_candidates: Optional[Sequence[Optional[str]]] = None,
        coarse_step: float = 0.05,
        refine_step: float = 0.02,
        split_min: float = 0.30,
        split_max: float = 0.70,
        include_auto_split: bool = False,
        apply: bool = False,
        restore_loaded_model: bool = True,
    ) -> TuneResult:
        """Search for the best context, `ngl`, and tensor split for a model entry."""
        cleanup_needed = True
        run_completed = False
        try:
            canonical_model = self.resolve_model(model_name)
            original_model_config = copy.deepcopy(self.base_config.get("models", {}).get(canonical_model, {}))
            self._attempt_log = []
            self._attempt_keys_seen = set()
            self._active_model_signature = build_model_signature(canonical_model, original_model_config)
            self._seed_probe_cache(canonical_model)
            original_context = resolve_runtime_config_value(original_model_config, "context", self.runtime_mode)
            original_ngl = self._normalize_ngl(resolve_runtime_config_value(original_model_config, "ngl", self.runtime_mode))
            original_tensor_split = self._normalize_tensor_split(
                resolve_runtime_config_value(original_model_config, "tensor_split", self.runtime_mode)
            )
            benchmark_limit = original_model_config.get("benchmark_context_limit")
            normalized_optimization = resolve_optimization_mode(optimization)
            lower_bound, upper_bound, lower_ngl, upper_ngl = resolve_optimization_defaults(
                original_context=int(original_context) if isinstance(original_context, int) else None,
                benchmark_context_limit=int(benchmark_limit) if isinstance(benchmark_limit, int) else None,
                granularity=granularity,
                auto_context_floor_ratio=auto_context_floor_ratio,
                optimization=normalized_optimization,
            )

            self._start_live_result_log(
                model=canonical_model,
                original_context=original_context if isinstance(original_context, int) else None,
                original_ngl=original_ngl,
                original_tensor_split=original_tensor_split,
                optimization=normalized_optimization,
                search_min_context=lower_bound,
                search_max_context=upper_bound,
                benchmark_context_limit=benchmark_limit if isinstance(benchmark_limit, int) else None,
                coarse_ngl_candidates=[],
                refined_ngl_candidates=[],
                coarse_candidates=[],
                refined_candidates=[],
                applied=False,
                runtime_mode=self.runtime_mode,
            )

            explicit_ngl_candidates = None
            if ngl_candidates:
                normalized_ngls = [
                    normalized
                    for candidate in ngl_candidates
                    for normalized in [self._normalize_ngl(candidate)]
                    if normalized is not None
                ]
                explicit_ngl_candidates = sorted(set(normalized_ngls), reverse=True)
            explicit_split_candidates = None
            if split_candidates:
                explicit_split_candidates = [self._normalize_tensor_split(candidate) for candidate in split_candidates]

            if explicit_ngl_candidates or explicit_split_candidates:
                best_result = self._search_explicit_candidate_grid(
                    model_name=canonical_model,
                    model_config=original_model_config,
                    lower_bound=lower_bound,
                    upper_bound=upper_bound,
                    granularity=granularity,
                    upper_ngl=upper_ngl,
                    lower_ngl=lower_ngl,
                    ngl_step=ngl_step,
                    explicit_ngl_candidates=explicit_ngl_candidates,
                    explicit_split_candidates=explicit_split_candidates,
                    original_ngl=original_ngl,
                    original_tensor_split=original_tensor_split,
                    split_step=coarse_step,
                    split_min=split_min,
                    split_max=split_max,
                    include_auto_split=include_auto_split,
                    optimization=normalized_optimization,
                )
            else:
                best_result = self._search_best_auto_combination(
                    model_name=canonical_model,
                    model_config=original_model_config,
                    lower_bound=lower_bound,
                    upper_bound=upper_bound,
                    granularity=granularity,
                    original_ngl=original_ngl,
                    lower_ngl=lower_ngl,
                    upper_ngl=upper_ngl,
                    ngl_refine_step=ngl_refine_step,
                    coarse_step=coarse_step,
                    refine_step=refine_step,
                    split_min=split_min,
                    split_max=split_max,
                    original_tensor_split=original_tensor_split,
                    optimization=normalized_optimization,
                )

            if best_result is None:
                raise RuntimeError(f"No successful config found for '{canonical_model}' in range {lower_bound}-{upper_bound}")

            tested_ngls = unique_attempt_ngls(self._attempt_log)
            tested_splits = unique_attempt_splits(self._attempt_log)

            result = TuneResult(
                model=canonical_model,
                original_context=original_context if isinstance(original_context, int) else None,
                original_ngl=original_ngl,
                original_tensor_split=original_tensor_split,
                runtime_mode=self.runtime_mode,
                optimization=normalized_optimization,
                search_min_context=lower_bound,
                search_max_context=upper_bound,
                recommended_context=best_result.context,
                recommended_ngl=best_result.ngl,
                recommended_tensor_split=best_result.tensor_split,
                benchmark_context_limit=benchmark_limit if isinstance(benchmark_limit, int) else None,
                attempts=list(self._attempt_log),
                coarse_ngl_candidates=tested_ngls,
                refined_ngl_candidates=[],
                coarse_candidates=tested_splits,
                refined_candidates=[],
                applied=apply,
                model_signature=self._active_model_signature,
                smoke_signature=self._active_smoke_signature,
            )

            if apply:
                self._apply_recommendation(canonical_model, original_model_config, best_result)
            else:
                self._restore_original_config(restore_loaded_model=restore_loaded_model)
            cleanup_needed = False

            self._append_result_log(result)
            run_completed = True
            return result
        except BaseException as exc:
            self._mark_live_result_failed(exc)
            raise
        finally:
            if cleanup_needed:
                try:
                    self._restore_original_config(restore_loaded_model=restore_loaded_model)
                except Exception as exc:
                    logger.warning("Failed to restore original finetune state: %s", exc)
            if run_completed or self._active_result_index is not None:
                self._active_result_index = None

    def _find_best_context_for_combination(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        ngl: int,
        tensor_split: Optional[str],
        min_context: int,
        max_context: int,
        granularity: int,
        anchor_context: Optional[int],
    ) -> Optional[ProbeResult]:
        """Binary-search the highest successful context for one `ngl`/split combination."""
        best_context, _ = binary_search_max_success(
            min_context=min_context,
            max_context=max_context,
            granularity=granularity,
            anchor_context=anchor_context,
            probe=lambda context: self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
            ).success,
        )
        if best_context is None:
            return None
        return self._probe_candidate(
            model_name=model_name,
            model_config=model_config,
            context=best_context,
            ngl=ngl,
            tensor_split=tensor_split,
        )

    def _search_explicit_candidate_grid(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        lower_bound: int,
        upper_bound: int,
        granularity: int,
        upper_ngl: int,
        lower_ngl: int,
        ngl_step: int,
        explicit_ngl_candidates: Optional[Sequence[int]],
        explicit_split_candidates: Optional[Sequence[Optional[str]]],
        original_ngl: Optional[int],
        original_tensor_split: Optional[str],
        split_step: float,
        split_min: float,
        split_max: float,
        include_auto_split: bool,
        optimization: str,
    ) -> Optional[ProbeResult]:
        """Evaluate explicit ngl/split candidates without auto bisection."""
        ngl_values = list(explicit_ngl_candidates or build_ngl_candidates(original_ngl, ngl_step, lower_ngl, upper_ngl))
        split_values = list(
            explicit_split_candidates
            or build_split_candidates(
                original_tensor_split,
                split_step,
                split_min,
                split_max,
                include_auto=include_auto_split,
            )
        )

        best_result: Optional[ProbeResult] = None
        for candidate in split_values:
            for ngl_candidate in ngl_values:
                candidate_min_context, candidate_max_context = resolve_candidate_context_bounds(
                    best_context=best_result.context if best_result is not None else None,
                    lower_bound=lower_bound,
                    upper_bound=upper_bound,
                    granularity=granularity,
                )
                result = self._find_best_context_for_combination(
                    model_name=model_name,
                    model_config=model_config,
                    ngl=ngl_candidate,
                    tensor_split=candidate,
                    min_context=candidate_min_context,
                    max_context=candidate_max_context,
                    granularity=granularity,
                    anchor_context=upper_bound,
                )
                best_result = choose_better_result(
                    best_result,
                    result,
                    optimization=optimization,
                    max_context=upper_bound,
                    max_ngl=upper_ngl,
                )
                if result is not None and result.success and result.context >= upper_bound:
                    break
        return best_result

    def _search_best_auto_combination(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        lower_bound: int,
        upper_bound: int,
        granularity: int,
        original_ngl: Optional[int],
        lower_ngl: int,
        upper_ngl: int,
        ngl_refine_step: int,
        coarse_step: float,
        refine_step: float,
        split_min: float,
        split_max: float,
        original_tensor_split: Optional[str],
        optimization: str = DEFAULT_OPTIMIZATION_MODE,
    ) -> Optional[ProbeResult]:
        """Run an optimization-aware search with balanced split calibration on every successful state."""
        calibration_context = min(align_context_ceil(DEFAULT_SPLIT_CALIBRATION_CONTEXT, granularity), upper_bound)
        seed_split = original_tensor_split or format_two_gpu_split(min(max(0.5, split_min), split_max))
        ngl_values = build_ngl_candidates(original_ngl, ngl_refine_step, lower_ngl, upper_ngl)
        best_result: Optional[ProbeResult] = None
        best_seed_split = seed_split

        for ngl_candidate in ngl_values:
            split_result = self._calibrate_tensor_split(
                model_name=model_name,
                model_config=model_config,
                context=calibration_context,
                ngl=ngl_candidate,
                starting_split=best_seed_split,
                coarse_step=coarse_step,
                balance_threshold_pct=DEFAULT_VRAM_BALANCE_THRESHOLD_PCT,
                split_min=split_min,
                split_max=split_max,
            )
            if split_result is None or split_result.tensor_split is None:
                continue

            if optimization == "speed":
                candidate_result = self._maximize_context_for_speed_mode(
                    model_name=model_name,
                    model_config=model_config,
                    min_context=lower_bound,
                    max_context=upper_bound,
                    granularity=granularity,
                    ngl=ngl_candidate,
                    starting_split=split_result.tensor_split,
                    refine_step=refine_step,
                    split_min=split_min,
                    split_max=split_max,
                )
            else:
                candidate_result = self._maximize_context_with_balanced_runtime(
                    model_name=model_name,
                    model_config=model_config,
                    min_context=lower_bound,
                    max_context=upper_bound,
                    granularity=granularity,
                    ngl=ngl_candidate,
                    starting_split=split_result.tensor_split,
                    refine_step=refine_step,
                    split_min=split_min,
                    split_max=split_max,
                )
            if candidate_result is None:
                continue

            best_result = choose_better_result(
                best_result,
                candidate_result,
                optimization=optimization,
                max_context=upper_bound,
                max_ngl=upper_ngl,
            )
            best_seed_split = candidate_result.tensor_split or best_seed_split

            if optimization == "speed":
                return best_result
            if optimization == "context" and best_result is not None and best_result.context >= upper_bound:
                return best_result

        return best_result

    def _stabilize_split_for_state(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        starting_split: Optional[str],
        step: float,
        balance_threshold_pct: float,
        split_min: float,
        split_max: float,
    ) -> Optional[ProbeResult]:
        """Find a stable split for one context/ngl state and proactively rebalance it by VRAM."""
        current_split = starting_split or format_two_gpu_split(min(max(0.5, split_min), split_max))
        attempted_splits: set[str] = set()
        best_success: Optional[ProbeResult] = None

        while True:
            attempted_splits.add(current_split)
            result = self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=ngl,
                tensor_split=current_split,
            )
            if result.success:
                best_success = self._rebalance_split_by_vram(
                    model_name=model_name,
                    model_config=model_config,
                    starting_result=result,
                    step=step,
                    balance_threshold_pct=balance_threshold_pct,
                    split_min=split_min,
                    split_max=split_max,
                )
                return best_success

            next_candidates = next_split_candidates_after_oom(
                current_split,
                failed_gpu=detect_oom_gpu(result.error),
                step=step,
                split_min=split_min,
                split_max=split_max,
            )
            next_split = next((candidate for candidate in next_candidates if candidate not in attempted_splits), None)
            if next_split is None:
                return best_success
            current_split = next_split

    def _rebalance_split_by_vram(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        starting_result: ProbeResult,
        step: float,
        balance_threshold_pct: float,
        split_min: float,
        split_max: float,
    ) -> ProbeResult:
        """Keep nudging the split until free VRAM percentages converge or stop improving."""
        current_result = starting_result
        attempted_splits: set[Optional[str]] = {current_result.tensor_split}

        while (
            current_result.success
            and current_result.free_vram_delta_pct is not None
            and current_result.free_vram_delta_pct > balance_threshold_pct
        ):
            probe_step = step
            if should_skip_coarse_split_shift(current_result.gpu_vram, step=probe_step):
                probe_step = smaller_split_step(probe_step) or probe_step
            next_split = next_split_from_vram_balance(
                current_result.tensor_split,
                gpu_vram=current_result.gpu_vram,
                step=probe_step,
                split_min=split_min,
                split_max=split_max,
            )
            if next_split is None or next_split in attempted_splits:
                break
            attempted_splits.add(next_split)
            candidate_result = self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=current_result.context,
                ngl=current_result.ngl,
                tensor_split=next_split,
            )
            if not candidate_result.success:
                retry_step = smaller_split_step(probe_step)
                if retry_step is None:
                    break
                retry_split = next_split_from_vram_balance(
                    current_result.tensor_split,
                    gpu_vram=current_result.gpu_vram,
                    step=retry_step,
                    split_min=split_min,
                    split_max=split_max,
                )
                if retry_split is None or retry_split in attempted_splits:
                    break
                attempted_splits.add(retry_split)
                candidate_result = self._probe_candidate(
                    model_name=model_name,
                    model_config=model_config,
                    context=current_result.context,
                    ngl=current_result.ngl,
                    tensor_split=retry_split,
                )
                if not candidate_result.success:
                    break
            if (
                candidate_result.free_vram_delta_pct is None
                or current_result.free_vram_delta_pct is None
                or candidate_result.free_vram_delta_pct >= current_result.free_vram_delta_pct
            ):
                break
            current_result = candidate_result

        return current_result

    def _calibrate_tensor_split(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        starting_split: Optional[str],
        coarse_step: float,
        balance_threshold_pct: float,
        split_min: float,
        split_max: float,
    ) -> Optional[ProbeResult]:
        """Calibrate the baseline split first, then proactively rebalance it by free VRAM."""
        return self._stabilize_split_for_state(
            model_name=model_name,
            model_config=model_config,
            context=context,
            ngl=ngl,
            starting_split=starting_split,
            step=coarse_step,
            balance_threshold_pct=balance_threshold_pct,
            split_min=split_min,
            split_max=split_max,
        )

    def _optimize_ngl_for_baseline(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        tensor_split: str,
        min_ngl: int,
        max_ngl: int,
        ngl_step: int,
        refine_step: float,
        split_min: float,
        split_max: float,
    ) -> Optional[ProbeResult]:
        """Lower ngl stepwise on the safe baseline and rebalance split after every ngl change."""
        current_ngl = max_ngl
        current_split = tensor_split
        while current_ngl >= min_ngl:
            result = self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=current_ngl,
                tensor_split=current_split,
            )
            if result.success:
                rebalanced = self._rebalance_split_by_vram(
                    model_name=model_name,
                    model_config=model_config,
                    starting_result=result,
                    step=refine_step,
                    balance_threshold_pct=DEFAULT_VRAM_BALANCE_THRESHOLD_PCT,
                    split_min=split_min,
                    split_max=split_max,
                )
                current_split = rebalanced.tensor_split or current_split
                return rebalanced
            if current_ngl == min_ngl:
                return None
            current_ngl = max(min_ngl, current_ngl - ngl_step)
        return None

    def _maximize_context_with_balanced_runtime(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        min_context: int,
        max_context: int,
        granularity: int,
        ngl: int,
        starting_split: str,
        refine_step: float,
        split_min: float,
        split_max: float,
    ) -> Optional[ProbeResult]:
        """At every new context, restabilize the split first and then apply context bisection."""
        context_results: Dict[int, Optional[ProbeResult]] = {}

        def evaluate_context(context: int) -> Optional[ProbeResult]:
            if context not in context_results:
                context_results[context] = self._stabilize_split_for_state(
                    model_name=model_name,
                    model_config=model_config,
                    context=context,
                    ngl=ngl,
                    starting_split=starting_split,
                    step=refine_step,
                    balance_threshold_pct=DEFAULT_VRAM_BALANCE_THRESHOLD_PCT,
                    split_min=split_min,
                    split_max=split_max,
                )
            return context_results[context]

        best_context, _ = binary_search_max_success(
            min_context=min_context,
            max_context=max_context,
            granularity=granularity,
            anchor_context=max_context,
            probe=lambda context: (evaluate_context(context) or ProbeResult("", 0, 0, None, False, 0.0)).success,
        )
        if best_context is None:
            return None
        return evaluate_context(best_context)

    def _maximize_context_for_speed_mode(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        min_context: int,
        max_context: int,
        granularity: int,
        ngl: int,
        starting_split: str,
        refine_step: float,
        split_min: float,
        split_max: float,
    ) -> Optional[ProbeResult]:
        """Binary-search context on the current split, then rebalance once at the winning frontier."""
        probed_results: Dict[int, ProbeResult] = {}

        def probe(context: int) -> bool:
            result = self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=ngl,
                tensor_split=starting_split,
            )
            probed_results[context] = result
            return result.success

        best_context, _ = binary_search_max_success(
            min_context=min_context,
            max_context=max_context,
            granularity=granularity,
            anchor_context=max_context,
            probe=probe,
        )
        if best_context is None:
            return None
        best_result = probed_results[best_context]
        higher_failures = sorted(
            context
            for context, result in probed_results.items()
            if context > best_context and not result.success
        )
        frontier_failure = probed_results[higher_failures[0]] if higher_failures else None
        return self._refine_speed_frontier_with_split(
            model_name=model_name,
            model_config=model_config,
            best_success=best_result,
            frontier_failure=frontier_failure,
            max_context=max_context,
            granularity=granularity,
            refine_step=refine_step,
            split_min=split_min,
            split_max=split_max,
        )

    def _refine_speed_frontier_with_split(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        best_success: ProbeResult,
        frontier_failure: Optional[ProbeResult],
        max_context: int,
        granularity: int,
        refine_step: float,
        split_min: float,
        split_max: float,
    ) -> ProbeResult:
        """Use a tiny local split refinement near a narrow speed frontier instead of restarting a broad search."""
        baseline = self._rebalance_split_by_vram(
            model_name=model_name,
            model_config=model_config,
            starting_result=best_success,
            step=refine_step,
            balance_threshold_pct=DEFAULT_VRAM_BALANCE_THRESHOLD_PCT,
            split_min=split_min,
            split_max=split_max,
        )
        if frontier_failure is None or frontier_failure.context <= baseline.context:
            return baseline

        frontier_gap = frontier_failure.context - baseline.context
        imbalance = max(
            float(baseline.free_vram_delta_pct or 0.0),
            float(frontier_failure.free_vram_delta_pct or 0.0),
        )
        fine_step = smaller_split_step(refine_step) or refine_step
        if frontier_gap > (granularity * 3) or imbalance <= DEFAULT_VRAM_BALANCE_THRESHOLD_PCT:
            return baseline

        split_source = frontier_failure if frontier_failure.gpu_vram else baseline
        frontier_split = next_split_from_vram_balance(
            baseline.tensor_split,
            gpu_vram=split_source.gpu_vram,
            step=fine_step,
            split_min=split_min,
            split_max=split_max,
        )
        if frontier_split is None or frontier_split == baseline.tensor_split:
            return baseline

        frontier_probe = self._probe_candidate(
            model_name=model_name,
            model_config=model_config,
            context=frontier_failure.context,
            ngl=baseline.ngl,
            tensor_split=frontier_split,
        )
        if not frontier_probe.success:
            return baseline

        local_results: Dict[int, ProbeResult] = {frontier_probe.context: frontier_probe}
        local_split = frontier_probe.tensor_split or frontier_split
        headroom_source = frontier_probe.gpu_vram or baseline.gpu_vram or frontier_failure.gpu_vram
        local_granularity = resolve_headroom_context_granularity(
            headroom_source,
            base_granularity=granularity,
        )
        local_jump = granularity if should_limit_large_context_jumps(headroom_source) else max(granularity, frontier_gap)
        local_upper = min(max_context, frontier_failure.context + local_jump)

        if local_upper > frontier_probe.context:
            def local_probe(context: int) -> bool:
                result = self._probe_candidate(
                    model_name=model_name,
                    model_config=model_config,
                    context=context,
                    ngl=baseline.ngl,
                    tensor_split=local_split,
                )
                local_results[context] = result
                return result.success

            local_best_context, _ = binary_search_max_success(
                min_context=frontier_probe.context,
                max_context=local_upper,
                granularity=local_granularity,
                anchor_context=local_upper,
                probe=local_probe,
            )
            if local_best_context is not None:
                frontier_probe = local_results[local_best_context]

        frontier_best = self._rebalance_split_by_vram(
            model_name=model_name,
            model_config=model_config,
            starting_result=frontier_probe,
            step=fine_step,
            balance_threshold_pct=DEFAULT_VRAM_BALANCE_THRESHOLD_PCT,
            split_min=split_min,
            split_max=split_max,
        )
        return choose_better_result(
            baseline,
            frontier_best,
            optimization="speed",
            max_context=max_context,
            max_ngl=baseline.ngl,
        )

    def _ensure_balanced_split_for_values(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        split_min: float,
        split_max: float,
        starting_split: Optional[str],
        include_auto_split: bool,
    ) -> Optional[ProbeResult]:
        """Probe the current split first, then rebalance it before changing other dimensions again."""
        best_result: Optional[ProbeResult] = None
        if starting_split is not None:
            seed_result = self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=ngl,
                tensor_split=starting_split,
            )
            best_result = choose_better_result(best_result, seed_result)
            if seed_result.success:
                rebalanced_result = self._rebalance_successful_split(
                    model_name=model_name,
                    model_config=model_config,
                    context=context,
                    ngl=ngl,
                    successful_result=seed_result,
                    split_min=split_min,
                    split_max=split_max,
                )
                return choose_better_result(best_result, rebalanced_result)

        balanced_result = self._search_most_balanced_split_for_values(
            model_name=model_name,
            model_config=model_config,
            context=context,
            ngl=ngl,
            split_min=split_min,
            split_max=split_max,
            anchor_split=starting_split,
        )
        best_result = choose_better_result(best_result, balanced_result)
        if best_result is not None:
            return best_result
        if not include_auto_split or starting_split is None:
            return best_result

        auto_result = self._probe_candidate(
            model_name=model_name,
            model_config=model_config,
            context=context,
            ngl=ngl,
            tensor_split=None,
        )
        return choose_better_result(best_result, auto_result)

    def _rebalance_successful_split(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        successful_result: ProbeResult,
        split_min: float,
        split_max: float,
    ) -> Optional[ProbeResult]:
        """Move a known-good split one halving step toward 50/50."""
        current_primary = parse_two_gpu_split(successful_result.tensor_split)
        target_primary = min(max(0.5, split_min), split_max)
        if current_primary is None or math.isclose(current_primary, target_primary, abs_tol=0.005):
            return successful_result

        candidate_primary = round((current_primary + target_primary) / 2, 2)
        if math.isclose(candidate_primary, current_primary, abs_tol=1e-9):
            return successful_result

        candidate_result = self._probe_candidate(
            model_name=model_name,
            model_config=model_config,
            context=context,
            ngl=ngl,
            tensor_split=format_two_gpu_split(candidate_primary),
        )
        return choose_better_result(successful_result, candidate_result)

    def _search_most_balanced_split_for_values(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        split_min: float,
        split_max: float,
        anchor_split: Optional[str],
    ) -> Optional[ProbeResult]:
        """Use halving search to find the closest-to-balanced explicit split for fixed context/ngl."""
        max_distance = max(0.5 - split_min, split_max - 0.5)
        precision = 0.01

        balanced_result = self._evaluate_split_distance_for_values(
            model_name=model_name,
            model_config=model_config,
            context=context,
            ngl=ngl,
            distance=0.0,
            split_min=split_min,
            split_max=split_max,
            anchor_split=anchor_split,
        )
        if balanced_result is not None:
            return balanced_result

        low = 0.0
        high = max_distance
        best_result: Optional[ProbeResult] = None
        while high - low > precision:
            mid = round((low + high) / 2, 4)
            candidate_result = self._evaluate_split_distance_for_values(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=ngl,
                distance=mid,
                split_min=split_min,
                split_max=split_max,
                anchor_split=anchor_split,
            )
            if candidate_result is not None:
                best_result = choose_better_result(best_result, candidate_result)
                high = mid
            else:
                low = mid

        final_result = self._evaluate_split_distance_for_values(
            model_name=model_name,
            model_config=model_config,
            context=context,
            ngl=ngl,
            distance=round(high, 4),
            split_min=split_min,
            split_max=split_max,
            anchor_split=anchor_split,
        )
        return choose_better_result(best_result, final_result)

    def _evaluate_split_distance_for_values(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        distance: float,
        split_min: float,
        split_max: float,
        anchor_split: Optional[str],
    ) -> Optional[ProbeResult]:
        """Evaluate one balance distance and stop on the first successful side."""
        best_result: Optional[ProbeResult] = None
        for candidate in split_candidates_for_distance(
            distance,
            min_primary=split_min,
            max_primary=split_max,
            anchor_split=anchor_split,
        ):
            result = self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                ngl=ngl,
                tensor_split=candidate,
            )
            best_result = choose_better_result(best_result, result)
            if result.success:
                return best_result
        return best_result

    def _probe_candidate(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        ngl: int,
        tensor_split: Optional[str],
    ) -> ProbeResult:
        """Apply one temporary model config and probe it through Guardian."""
        if self._active_model_signature is None:
            raise RuntimeError("Active model signature not initialized")

        normalized_split = self._normalize_tensor_split(tensor_split)
        cache_key = build_probe_cache_key(
            model_name,
            int(context),
            int(ngl),
            normalized_split,
            self._active_model_signature,
            self._active_smoke_signature,
        )
        cached = self.probe_cache.get(cache_key)
        if cached is not None:
            self._record_attempt(cache_key, cached)
            return cached

        candidate_config = apply_runtime_search_values(
            model_config,
            context=int(context),
            ngl=int(ngl),
            tensor_split=normalized_split,
            runtime_mode=self.runtime_mode,
        )

        rendered = render_model_block(model_name, candidate_config)
        candidate_text = replace_model_block(self.base_text, model_name, rendered)
        self._atomic_write(self.models_config_path, candidate_text)

        load_started = time.perf_counter()
        try:
            load_response = self._request_with_retry(
                "POST",
                f"{self.guardian_url}/admin/load",
                json={
                    "model": model_name,
                    "enable_vision": runtime_mode_uses_vision(self.runtime_mode),
                },
            )
        except httpx.RequestError as exc:
            probe_result = ProbeResult(
                model=model_name,
                context=int(context),
                ngl=int(ngl),
                tensor_split=normalized_split,
                success=False,
                load_seconds=time.perf_counter() - load_started,
                error=str(exc),
                model_signature=self._active_model_signature,
                smoke_signature=self._active_smoke_signature,
            )
            self.probe_cache[cache_key] = probe_result
            self._record_attempt(cache_key, probe_result)
            return probe_result
        load_seconds = time.perf_counter() - load_started

        if load_response.status_code != 200:
            probe_result = ProbeResult(
                model=model_name,
                context=int(context),
                ngl=int(ngl),
                tensor_split=normalized_split,
                success=False,
                load_seconds=load_seconds,
                status_code=load_response.status_code,
                error=load_response.text,
                model_signature=self._active_model_signature,
                smoke_signature=self._active_smoke_signature,
            )
        else:
            smoke_started = time.perf_counter()
            try:
                smoke_response = self._request_with_retry(
                    "POST",
                    f"{self.guardian_url}/v1/chat/completions",
                    json={
                        "model": model_name,
                        "messages": build_smoke_messages(self.smoke_prompt, self.smoke_image_url),
                        "temperature": 0.0,
                        "max_tokens": self.smoke_max_tokens,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
            except httpx.RequestError as exc:
                probe_result = ProbeResult(
                    model=model_name,
                    context=int(context),
                    ngl=int(ngl),
                    tensor_split=normalized_split,
                    success=False,
                    load_seconds=load_seconds,
                    smoke_seconds=time.perf_counter() - smoke_started,
                    error=str(exc),
                    model_signature=self._active_model_signature,
                    smoke_signature=self._active_smoke_signature,
                )
            else:
                smoke_seconds = time.perf_counter() - smoke_started
                gpu_vram = read_gpu_vram_snapshot()
                delta_pct = free_vram_delta_pct(gpu_vram)
                if smoke_response.status_code == 200:
                    message = smoke_response.json().get("choices", [{}])[0].get("message", {})
                    excerpt = (message.get("content") or message.get("reasoning_content") or "").strip()
                    probe_result = ProbeResult(
                        model=model_name,
                        context=int(context),
                        ngl=int(ngl),
                        tensor_split=normalized_split,
                        success=True,
                        load_seconds=load_seconds,
                        smoke_seconds=smoke_seconds,
                        status_code=smoke_response.status_code,
                        response_excerpt=excerpt[:120] or None,
                        gpu_vram=gpu_vram,
                        free_vram_delta_pct=delta_pct,
                        model_signature=self._active_model_signature,
                        smoke_signature=self._active_smoke_signature,
                    )
                else:
                    probe_result = ProbeResult(
                        model=model_name,
                        context=int(context),
                        ngl=int(ngl),
                        tensor_split=normalized_split,
                        success=False,
                        load_seconds=load_seconds,
                        smoke_seconds=smoke_seconds,
                        status_code=smoke_response.status_code,
                        error=smoke_response.text,
                        gpu_vram=gpu_vram,
                        free_vram_delta_pct=delta_pct,
                        model_signature=self._active_model_signature,
                        smoke_signature=self._active_smoke_signature,
                    )

        self.probe_cache[cache_key] = probe_result
        self._record_attempt(cache_key, probe_result)
        logger.info(
            "Probe %s ctx=%s ngl=%s split=%s success=%s",
            model_name,
            context,
            ngl,
            normalized_split or "auto",
            probe_result.success,
        )
        return probe_result

    def _request_with_retry(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Retry transient Guardian transport failures a small number of times."""
        last_error: Optional[httpx.RequestError] = None
        for attempt in range(3):
            try:
                return self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if last_error is None:
            raise RuntimeError("request retry loop exited without a response or error")
        raise last_error

    def _apply_recommendation(
        self,
        model_name: str,
        model_config: Dict[str, object],
        best_result: ProbeResult,
    ) -> None:
        """Persist the winning config to models.yaml and reload it through Guardian."""
        applied_config = apply_runtime_search_values(
            model_config,
            context=best_result.context,
            ngl=best_result.ngl,
            tensor_split=best_result.tensor_split,
            runtime_mode=self.runtime_mode,
        )
        rendered = render_model_block(model_name, applied_config)
        applied_text = replace_model_block(self.base_text, model_name, rendered)
        self._atomic_write(self.models_config_path, applied_text)
        response = self._request_with_retry(
            "POST",
            f"{self.guardian_url}/admin/load",
            json={
                "model": model_name,
                "enable_vision": runtime_mode_uses_vision(self.runtime_mode),
            },
        )
        response.raise_for_status()

    def _restore_original_config(self, *, restore_loaded_model: bool) -> None:
        """Restore the original config file and optionally the original live model."""
        self._atomic_write(self.models_config_path, self.base_text)
        if restore_loaded_model and self.original_loaded_model:
            response = self._request_with_retry(
                "POST",
                f"{self.guardian_url}/admin/load",
                json={"model": self.original_loaded_model},
            )
            response.raise_for_status()

    def _append_result_log(self, result: TuneResult) -> None:
        """Persist the final finetune run state to the JSON history file."""
        entry = result.to_dict()
        entry["status"] = "completed"
        entry["completed_at"] = datetime.now(UTC).isoformat()
        if self._active_result_index is None:
            self.result_history.append(entry)
        else:
            existing = self.result_history[self._active_result_index]
            if isinstance(existing, dict) and existing.get("timestamp"):
                entry["timestamp"] = existing["timestamp"]
            self.result_history[self._active_result_index] = entry
        self._persist_result_history()

    def _load_result_history(self) -> List[Dict[str, object]]:
        """Load previous finetune runs from the durable results file."""
        if not self.results_file.exists():
            return []
        try:
            payload = json.loads(self.results_file.read_text())
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def _seed_probe_cache(self, model_name: str) -> None:
        """Load compatible probe results from the durable history file."""
        if self._active_model_signature is None:
            return
        compatible = index_cached_probes(
            self.result_history,
            model_name=model_name,
            model_signature=self._active_model_signature,
            smoke_signature=self._active_smoke_signature,
            runtime_mode=self.runtime_mode,
        )
        self.probe_cache.update(compatible)

    def _record_attempt(
        self,
        cache_key: Tuple[str, int, int, Optional[str], str, str],
        probe_result: ProbeResult,
    ) -> None:
        """Record one probe in the current run exactly once."""
        if cache_key in self._attempt_keys_seen:
            return
        self._attempt_keys_seen.add(cache_key)
        self._attempt_log.append(copy.deepcopy(probe_result))
        self._update_live_result_log(
            attempts=[asdict(attempt) for attempt in self._attempt_log],
            coarse_ngl_candidates=unique_attempt_ngls(self._attempt_log),
            coarse_candidates=unique_attempt_splits(self._attempt_log),
        )

    def _start_live_result_log(self, **entry: object) -> None:
        """Create an in-progress finetune entry before the first probe runs."""
        live_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "running",
            "model_signature": self._active_model_signature,
            "smoke_signature": self._active_smoke_signature,
            "attempts": [],
            **entry,
        }
        self.result_history.append(live_entry)
        self._active_result_index = len(self.result_history) - 1
        self._persist_result_history()

    def _update_live_result_log(self, **fields: object) -> None:
        """Persist incremental finetune state so operators can monitor a live run."""
        if self._active_result_index is None:
            return
        existing = self.result_history[self._active_result_index]
        if not isinstance(existing, dict):
            return
        updated = dict(existing)
        updated.update(fields)
        self.result_history[self._active_result_index] = updated
        self._persist_result_history()

    def _mark_live_result_failed(self, exc: BaseException) -> None:
        """Mark the active finetune entry as failed or interrupted."""
        if self._active_result_index is None:
            return
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        self._update_live_result_log(
            status=status,
            error=str(exc),
            completed_at=datetime.now(UTC).isoformat(),
        )

    def _persist_result_history(self) -> None:
        """Write the current in-memory result history to disk."""
        self.results_file.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.results_file, json.dumps(self.result_history, indent=2))

    def _get_current_model(self) -> Optional[str]:
        """Return the currently loaded canonical Guardian model, if any."""
        try:
            response = self.client.get(f"{self.guardian_url}/api/status")
            response.raise_for_status()
            current_model = response.json().get("current_model")
            if current_model and current_model != "__MISMATCH__":
                return current_model
        except Exception as exc:
            logger.warning("Could not fetch current Guardian model: %s", exc)
        return None

    @staticmethod
    def _normalize_tensor_split(tensor_split: Optional[object]) -> Optional[str]:
        """Normalize optional tensor split strings to a stable CLI format."""
        if tensor_split is None:
            return None
        ratio = parse_two_gpu_split(str(tensor_split))
        if ratio is None:
            text = str(tensor_split).strip()
            return text or None
        return format_two_gpu_split(ratio)

    @staticmethod
    def _normalize_ngl(ngl: Optional[object]) -> Optional[int]:
        """Normalize optional `ngl` values to integers."""
        if ngl is None:
            return None
        if not isinstance(ngl, (int, float, str)):
            return None
        try:
            value = int(ngl)
        except (TypeError, ValueError):
            return None
        return max(0, value)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Atomically replace a text file in-place."""
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(text)
        temp_path.replace(path)
