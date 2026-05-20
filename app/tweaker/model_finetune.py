"""Guardian-native model finetuning helpers.

This module finds the highest stable runtime context for a configured model while
also exploring two-GPU tensor split candidates. It uses Guardian's own
`/admin/load` and `/v1/chat/completions` endpoints so the measured result matches
real `models.yaml` behavior.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import yaml

logger = logging.getLogger("model-finetune")

DEFAULT_SMOKE_PROMPT = "Reply with exactly: FIT OK"


@dataclass(slots=True)
class ProbeResult:
    """Outcome of a single Guardian load probe."""

    model: str
    context: int
    tensor_split: Optional[str]
    success: bool
    load_seconds: float
    smoke_seconds: float = 0.0
    status_code: Optional[int] = None
    error: Optional[str] = None
    response_excerpt: Optional[str] = None
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
    original_tensor_split: Optional[str]
    recommended_context: int
    recommended_tensor_split: Optional[str]
    benchmark_context_limit: Optional[int]
    attempts: List[ProbeResult] = field(default_factory=list)
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
            "original_tensor_split": self.original_tensor_split,
            "recommended_context": self.recommended_context,
            "recommended_tensor_split": self.recommended_tensor_split,
            "benchmark_context_limit": self.benchmark_context_limit,
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
        if key not in {"context", "tensor_split", "benchmark_context_limit"}
    }
    payload = {"model": model_name, "config": signature_config}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_smoke_signature(
    smoke_prompt: str,
    smoke_max_tokens: int,
    smoke_image_url: Optional[str],
) -> str:
    """Create a stable cache signature for the current smoke probe shape."""
    payload = {
        "prompt": smoke_prompt,
        "max_tokens": smoke_max_tokens,
        "image_url": smoke_image_url,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_probe_cache_key(
    model_name: str,
    context: int,
    tensor_split: Optional[str],
    model_signature: str,
    smoke_signature: str,
) -> Tuple[str, int, Optional[str], str, str]:
    """Build the durable cache key for one probe combination."""
    return (model_name, int(context), tensor_split, model_signature, smoke_signature)


def index_cached_probes(
    history: Sequence[Dict[str, object]],
    *,
    model_name: str,
    model_signature: str,
    smoke_signature: str,
) -> Dict[Tuple[str, int, Optional[str], str, str], ProbeResult]:
    """Index compatible historical probes from the finetune results file."""
    indexed: Dict[Tuple[str, int, Optional[str], str, str], ProbeResult] = {}
    for entry in history:
        if entry.get("model") != model_name:
            continue
        if entry.get("model_signature") != model_signature:
            continue
        if entry.get("smoke_signature") != smoke_signature:
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
            tensor_split = attempt.get("tensor_split")
            if tensor_split is not None:
                tensor_split = str(tensor_split)
            probe = ProbeResult(
                model=str(attempt.get("model") or model_name),
                context=context,
                tensor_split=tensor_split,
                success=bool(attempt.get("success")),
                load_seconds=float(attempt.get("load_seconds") or 0.0),
                smoke_seconds=float(attempt.get("smoke_seconds") or 0.0),
                status_code=int(attempt["status_code"]) if attempt.get("status_code") is not None else None,
                error=str(attempt["error"]) if attempt.get("error") is not None else None,
                response_excerpt=str(attempt["response_excerpt"]) if attempt.get("response_excerpt") is not None else None,
                model_signature=model_signature,
                smoke_signature=smoke_signature,
                cached=True,
            )
            indexed[build_probe_cache_key(model_name, context, tensor_split, model_signature, smoke_signature)] = probe
    return indexed


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
    """Build ordered two-GPU tensor split candidates around the anchor split."""
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

    ordered = sorted(values, key=lambda value: (abs(value - anchor_primary), value))
    candidates: List[Optional[str]] = [format_two_gpu_split(value) for value in ordered]
    if include_auto:
        return [None, *candidates]
    return candidates


def choose_better_result(
    current_best: Optional[ProbeResult],
    candidate: Optional[ProbeResult],
    anchor_split: Optional[str],
) -> Optional[ProbeResult]:
    """Return the stronger successful result, preferring higher stable context."""
    if candidate is None or not candidate.success:
        return current_best
    if current_best is None or not current_best.success:
        return candidate
    if candidate.context != current_best.context:
        return candidate if candidate.context > current_best.context else current_best
    if candidate.total_seconds != current_best.total_seconds:
        return candidate if candidate.total_seconds < current_best.total_seconds else current_best

    anchor_primary = parse_two_gpu_split(anchor_split)
    best_distance = abs((parse_two_gpu_split(current_best.tensor_split) or anchor_primary or 0.5) - (anchor_primary or 0.5))
    candidate_distance = abs((parse_two_gpu_split(candidate.tensor_split) or anchor_primary or 0.5) - (anchor_primary or 0.5))
    return candidate if candidate_distance < best_distance else current_best


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
    """Tune a configured Guardian model via fast context and split search."""

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
    ) -> None:
        self.guardian_url = guardian_url.rstrip("/")
        self.models_config_path = Path(models_config_path)
        self.results_file = Path(results_file)
        self.smoke_prompt = smoke_prompt
        self.smoke_max_tokens = smoke_max_tokens
        self.smoke_image_url = smoke_image_url
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(900.0, connect=10.0),
        )
        self.base_text = self.models_config_path.read_text()
        self.base_config = yaml.safe_load(self.base_text) or {}
        self.result_history = self._load_result_history()
        self.probe_cache: Dict[Tuple[str, int, Optional[str], str, str], ProbeResult] = {}
        self._attempt_log: List[ProbeResult] = []
        self._attempt_keys_seen: set[Tuple[str, int, Optional[str], str, str]] = set()
        self._active_model_signature: Optional[str] = None
        self._active_smoke_signature = build_smoke_signature(
            self.smoke_prompt,
            self.smoke_max_tokens,
            self.smoke_image_url,
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
        min_context: Optional[int] = None,
        max_context: Optional[int] = None,
        granularity: int = 2048,
        split_candidates: Optional[Sequence[Optional[str]]] = None,
        coarse_step: float = 0.05,
        refine_step: float = 0.02,
        split_min: float = 0.35,
        split_max: float = 0.65,
        include_auto_split: bool = False,
        apply: bool = False,
        restore_loaded_model: bool = True,
    ) -> TuneResult:
        """Search for the best context and tensor split for a model entry."""
        cleanup_needed = True
        try:
            canonical_model = self.resolve_model(model_name)
            original_model_config = copy.deepcopy(self.base_config.get("models", {}).get(canonical_model, {}))
            self._attempt_log = []
            self._attempt_keys_seen = set()
            self._active_model_signature = build_model_signature(canonical_model, original_model_config)
            self._seed_probe_cache(canonical_model)
            original_context = original_model_config.get("context")
            original_tensor_split = self._normalize_tensor_split(original_model_config.get("tensor_split"))
            benchmark_limit = original_model_config.get("benchmark_context_limit")
            upper_bound = int(max_context or benchmark_limit or original_context or 131072)
            lower_bound = int(min_context or max(granularity, granularity))
            anchor_context = int(original_context or upper_bound)

            if split_candidates:
                coarse_candidates = [self._normalize_tensor_split(candidate) for candidate in split_candidates]
            else:
                coarse_candidates = build_split_candidates(
                    original_tensor_split,
                    coarse_step,
                    split_min,
                    split_max,
                    include_auto=include_auto_split,
                )

            best_result: Optional[ProbeResult] = None
            for candidate in coarse_candidates:
                result = self._find_best_context_for_split(
                    model_name=canonical_model,
                    model_config=original_model_config,
                    tensor_split=candidate,
                    min_context=lower_bound,
                    max_context=upper_bound,
                    granularity=granularity,
                    anchor_context=anchor_context,
                )
                best_result = choose_better_result(best_result, result, original_tensor_split)

            if best_result is None:
                raise RuntimeError(f"No successful config found for '{canonical_model}' in range {lower_bound}-{upper_bound}")

            refined_candidates: List[Optional[str]] = []
            if not split_candidates and refine_step > 0:
                best_primary = parse_two_gpu_split(best_result.tensor_split)
                if best_primary is not None:
                    refined_candidates = build_split_candidates(
                        best_result.tensor_split,
                        refine_step,
                        max(split_min, best_primary - coarse_step),
                        min(split_max, best_primary + coarse_step),
                    )
                    for candidate in refined_candidates:
                        result = self._find_best_context_for_split(
                            model_name=canonical_model,
                            model_config=original_model_config,
                            tensor_split=candidate,
                            min_context=lower_bound,
                            max_context=upper_bound,
                            granularity=granularity,
                            anchor_context=best_result.context,
                        )
                        best_result = choose_better_result(best_result, result, original_tensor_split)

            if best_result is None:
                raise RuntimeError(f"No successful config found for '{canonical_model}'")

            result = TuneResult(
                model=canonical_model,
                original_context=original_context if isinstance(original_context, int) else None,
                original_tensor_split=original_tensor_split,
                recommended_context=best_result.context,
                recommended_tensor_split=best_result.tensor_split,
                benchmark_context_limit=benchmark_limit if isinstance(benchmark_limit, int) else None,
                attempts=list(self._attempt_log),
                coarse_candidates=list(coarse_candidates),
                refined_candidates=list(refined_candidates),
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
            return result
        finally:
            if cleanup_needed:
                try:
                    self._restore_original_config(restore_loaded_model=restore_loaded_model)
                except Exception as exc:
                    logger.warning("Failed to restore original finetune state: %s", exc)

    def _find_best_context_for_split(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        tensor_split: Optional[str],
        min_context: int,
        max_context: int,
        granularity: int,
        anchor_context: Optional[int],
    ) -> Optional[ProbeResult]:
        """Binary-search the highest successful context for one split candidate."""
        best_context, _ = binary_search_max_success(
            min_context=min_context,
            max_context=max_context,
            granularity=granularity,
            anchor_context=anchor_context,
            probe=lambda context: self._probe_candidate(
                model_name=model_name,
                model_config=model_config,
                context=context,
                tensor_split=tensor_split,
            ).success,
        )
        if best_context is None:
            return None
        return self._probe_candidate(
            model_name=model_name,
            model_config=model_config,
            context=best_context,
            tensor_split=tensor_split,
        )

    def _probe_candidate(
        self,
        *,
        model_name: str,
        model_config: Dict[str, object],
        context: int,
        tensor_split: Optional[str],
    ) -> ProbeResult:
        """Apply one temporary model config and probe it through Guardian."""
        if self._active_model_signature is None:
            raise RuntimeError("Active model signature not initialized")

        normalized_split = self._normalize_tensor_split(tensor_split)
        cache_key = build_probe_cache_key(
            model_name,
            int(context),
            normalized_split,
            self._active_model_signature,
            self._active_smoke_signature,
        )
        cached = self.probe_cache.get(cache_key)
        if cached is not None:
            self._record_attempt(cache_key, cached)
            return cached

        candidate_config = copy.deepcopy(model_config)
        candidate_config["context"] = int(context)
        if normalized_split:
            candidate_config["tensor_split"] = normalized_split
        else:
            candidate_config.pop("tensor_split", None)

        rendered = render_model_block(model_name, candidate_config)
        candidate_text = replace_model_block(self.base_text, model_name, rendered)
        self._atomic_write(self.models_config_path, candidate_text)

        load_started = time.perf_counter()
        try:
            load_response = self._request_with_retry(
                "POST",
                f"{self.guardian_url}/admin/load",
                json={"model": model_name},
            )
        except httpx.RequestError as exc:
            probe_result = ProbeResult(
                model=model_name,
                context=int(context),
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
                if smoke_response.status_code == 200:
                    message = smoke_response.json().get("choices", [{}])[0].get("message", {})
                    excerpt = (message.get("content") or message.get("reasoning_content") or "").strip()
                    probe_result = ProbeResult(
                        model=model_name,
                        context=int(context),
                        tensor_split=normalized_split,
                        success=True,
                        load_seconds=load_seconds,
                        smoke_seconds=smoke_seconds,
                        status_code=smoke_response.status_code,
                        response_excerpt=excerpt[:120] or None,
                        model_signature=self._active_model_signature,
                        smoke_signature=self._active_smoke_signature,
                    )
                else:
                    probe_result = ProbeResult(
                        model=model_name,
                        context=int(context),
                        tensor_split=normalized_split,
                        success=False,
                        load_seconds=load_seconds,
                        smoke_seconds=smoke_seconds,
                        status_code=smoke_response.status_code,
                        error=smoke_response.text,
                        model_signature=self._active_model_signature,
                        smoke_signature=self._active_smoke_signature,
                    )

        self.probe_cache[cache_key] = probe_result
        self._record_attempt(cache_key, probe_result)
        logger.info(
            "Probe %s ctx=%s split=%s success=%s",
            model_name,
            context,
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
        applied_config = copy.deepcopy(model_config)
        applied_config["context"] = best_result.context
        if best_result.tensor_split:
            applied_config["tensor_split"] = best_result.tensor_split
        else:
            applied_config.pop("tensor_split", None)
        rendered = render_model_block(model_name, applied_config)
        applied_text = replace_model_block(self.base_text, model_name, rendered)
        self._atomic_write(self.models_config_path, applied_text)
        response = self._request_with_retry(
            "POST",
            f"{self.guardian_url}/admin/load",
            json={"model": model_name},
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
        """Append the finetune run to the JSON history file."""
        self.results_file.parent.mkdir(parents=True, exist_ok=True)
        payload = list(self.result_history)
        payload.append(result.to_dict())
        self.results_file.write_text(json.dumps(payload, indent=2))
        self.result_history = payload

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
        )
        self.probe_cache.update(compatible)

    def _record_attempt(
        self,
        cache_key: Tuple[str, int, Optional[str], str, str],
        probe_result: ProbeResult,
    ) -> None:
        """Record one probe in the current run exactly once."""
        if cache_key in self._attempt_keys_seen:
            return
        self._attempt_keys_seen.add(cache_key)
        self._attempt_log.append(copy.deepcopy(probe_result))

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
    def _atomic_write(path: Path, text: str) -> None:
        """Atomically replace a text file in-place."""
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(text)
        temp_path.replace(path)
