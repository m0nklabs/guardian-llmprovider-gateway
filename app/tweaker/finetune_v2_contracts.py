"""Pure finetune v2 contract helpers.

These helpers intentionally avoid Guardian I/O so the v2 requirements can be
locked down by deterministic tests before the live rewrite is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


# Requirement thresholds: below 750 MiB starts the limited follow-up budget,
# while below 500 MiB on both GPUs is the final VRAM convergence target.
LOW_HEADROOM_MIB = 750.0
FINAL_HEADROOM_MIB = 500.0
LOW_HEADROOM_FOLLOWUP_LIMIT = 5


@dataclass(frozen=True)
class RuntimeLimits:
    total_layers: int
    max_context: int
    active_context: int


@dataclass(frozen=True)
class Candidate:
    context: int
    ngl: int
    tensor_split: str
    runtime_mode: str = "text"
    has_mmproj: bool = False


@dataclass(frozen=True)
class Probe:
    candidate: Candidate
    success: bool
    free_vram_mib: tuple[float, float] = (0.0, 0.0)
    total_seconds: float = 0.0
    order: int = 0
    telemetry_source: str = "post_smoke"
    cache_backed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class PlanAction:
    kind: str
    candidate: Candidate
    reason: str


def clamp_ngl(ngl: int, limits: RuntimeLimits) -> int:
    return max(0, min(ngl, limits.total_layers))


def clamp_candidate(candidate: Candidate, limits: RuntimeLimits) -> Candidate:
    return replace(candidate, ngl=clamp_ngl(candidate.ngl, limits))


def unique_explicit_ngls(ngls: Iterable[int], limits: RuntimeLimits) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for ngl in ngls:
        clamped = clamp_ngl(ngl, limits)
        if clamped not in seen:
            seen.add(clamped)
            result.append(clamped)
    return result


def initial_seed_candidates(
    limits: RuntimeLimits,
    *,
    optimization: str,
    seed_split: str,
    fixed_context: int | None = None,
    fixed_ngl: int | None = None,
    runtime_mode: str = "text",
    has_mmproj: bool = False,
) -> list[Candidate]:
    context = fixed_context if fixed_context is not None else limits.active_context
    if optimization == "context" and fixed_context is None:
        context = limits.max_context
    ngls = [fixed_ngl] if fixed_ngl is not None else [limits.total_layers]
    return [
        Candidate(
            context=context,
            ngl=clamp_ngl(ngl, limits),
            tensor_split=seed_split,
            runtime_mode=runtime_mode,
            has_mmproj=has_mmproj,
        )
        for ngl in ngls
    ]


def _successful(probes: Sequence[Probe]) -> list[Probe]:
    return [probe for probe in probes if probe.success]


def _ensure_single_runtime_pool(probes: Sequence[Probe]) -> None:
    runtime_modes = {probe.candidate.runtime_mode for probe in probes}
    if len(runtime_modes) > 1:
        raise ValueError("finetune v2 ranking pools must not mix text and vision probes")


def _bottleneck_headroom(probe: Probe) -> float:
    return min(probe.free_vram_mib)


def _ranking_key(probe: Probe, optimization: str) -> tuple[float, ...]:
    candidate = probe.candidate
    if optimization == "context":
        return (
            candidate.context,
            candidate.ngl,
            _bottleneck_headroom(probe),
            -probe.total_seconds,
            -probe.order,
        )
    if optimization == "speed":
        return (
            candidate.ngl,
            -probe.total_seconds,
            candidate.context,
            _bottleneck_headroom(probe),
            -probe.order,
        )
    if optimization == "balanced":
        # Scale ngl to one 1024-token context step so balanced mode uses an
        # explicit score instead of preferring splits merely for being close to 50/50.
        score = candidate.context + (candidate.ngl * 1024) + _bottleneck_headroom(probe)
        return (score, -probe.total_seconds, -probe.order)
    raise ValueError(f"unknown optimization mode: {optimization}")


def rank_successes(
    probes: Sequence[Probe],
    *,
    optimization: str,
    context_floor: int | None = None,
) -> tuple[Probe, dict[str, object]]:
    successes = _successful(probes)
    if not successes:
        raise ValueError("cannot rank without a successful probe")
    _ensure_single_runtime_pool(successes)
    if optimization == "speed" and context_floor is not None:
        successes = [probe for probe in successes if probe.candidate.context >= context_floor]
        if not successes:
            raise ValueError("no successful probe met the speed-mode context floor")

    winner = max(successes, key=lambda probe: _ranking_key(probe, optimization))
    explanation = {
        "comparator_mode": optimization,
        "runtime_mode": winner.candidate.runtime_mode,
        "winner_reason": {
            "code": f"{optimization}_lexicographic_winner",
            "key": _ranking_key(winner, optimization),
        },
        "losing_reasons": [
            {
                "order": probe.order,
                "code": "lower_comparator_key",
                "key": _ranking_key(probe, optimization),
            }
            for probe in successes
            if probe is not winner
        ],
        "winner": {
            "context": winner.candidate.context,
            "ngl": winner.candidate.ngl,
            "tensor_split": winner.candidate.tensor_split,
        },
    }
    return winner, explanation


def latest_successful_state(probes: Sequence[Probe]) -> Probe | None:
    successes = _successful(probes)
    if not successes:
        return None
    return max(successes, key=lambda probe: probe.order)


def split_rebalance_action(probes: Sequence[Probe], *, better_split: str) -> PlanAction | None:
    latest_success = latest_successful_state(probes)
    if latest_success is None:
        return None
    candidate = replace(latest_success.candidate, tensor_split=better_split)
    return PlanAction("split_rebalance", candidate, "latest_successful_runtime_state")


def next_after_seed_failure(probes: Sequence[Probe], limits: RuntimeLimits) -> PlanAction | None:
    if latest_successful_state(probes) is not None or not probes:
        return None
    last = max(probes, key=lambda probe: probe.order)
    next_ngl = last.candidate.ngl - 1
    if next_ngl < 0:
        return None
    candidate = replace(last.candidate, ngl=clamp_ngl(next_ngl, limits))
    return PlanAction("seed_ngl_step_down", candidate, "seed_failed_before_any_rebalance")


def upward_ngl_retry_actions(
    rebalance_probe: Probe,
    limits: RuntimeLimits,
    *,
    max_retries: int = 2,
) -> list[PlanAction]:
    if not rebalance_probe.success:
        return []
    start = rebalance_probe.candidate.ngl + 1
    stop = min(limits.total_layers, rebalance_probe.candidate.ngl + max_retries)
    return [
        PlanAction(
            "upward_ngl_retry",
            replace(rebalance_probe.candidate, ngl=ngl),
            "successful_rebalance_allows_upward_ngl_retry",
        )
        for ngl in range(start, stop + 1)
    ]


def convergence_status(
    best_success: Probe,
    limits: RuntimeLimits,
    *,
    low_headroom_followups_used: int = 0,
    allowed_context: int | None = None,
    allowed_ngl: int | None = None,
) -> dict[str, object]:
    candidate = best_success.candidate
    target_context = allowed_context if allowed_context is not None else limits.max_context
    target_ngl = allowed_ngl if allowed_ngl is not None else limits.total_layers
    both_under_final = all(value < FINAL_HEADROOM_MIB for value in best_success.free_vram_mib)
    at_max_shape = candidate.context >= target_context and candidate.ngl >= target_ngl
    if both_under_final:
        return {"should_continue": False, "reason": "both_gpus_below_500_mib"}
    if at_max_shape:
        return {"should_continue": False, "reason": "max_context_and_ngl"}
    both_under_low = all(value < LOW_HEADROOM_MIB for value in best_success.free_vram_mib)
    if both_under_low:
        remaining = LOW_HEADROOM_FOLLOWUP_LIMIT - low_headroom_followups_used
        if remaining <= 0:
            return {"should_continue": False, "reason": "low_headroom_budget_exhausted"}
        return {
            "should_continue": True,
            "reason": "low_headroom_followup",
            "remaining_followups": remaining,
        }
    return {"should_continue": True, "reason": "search_not_converged"}


def convergence_status_from_history(
    probes: Sequence[Probe],
    limits: RuntimeLimits,
    *,
    optimization: str,
    context_floor: int | None = None,
    low_headroom_followups_used: int = 0,
    allowed_context: int | None = None,
    allowed_ngl: int | None = None,
) -> dict[str, object]:
    best_success, _ = rank_successes(
        probes,
        optimization=optimization,
        context_floor=context_floor,
    )
    status = convergence_status(
        best_success,
        limits,
        low_headroom_followups_used=low_headroom_followups_used,
        allowed_context=allowed_context,
        allowed_ngl=allowed_ngl,
    )
    return {
        **status,
        "best_order": best_success.order,
        "best_context": best_success.candidate.context,
        "best_ngl": best_success.candidate.ngl,
    }


class FixtureProbeRunner:
    """Deterministic text/vision probe replay keyed by exact candidate shape."""

    def __init__(self, fixture_rows: Sequence[Mapping[str, object]]) -> None:
        self._fixtures = {
            (
                str(row["runtime_mode"]),
                int(row["context"]),
                int(row["ngl"]),
                str(row["tensor_split"]),
            ): row
            for row in fixture_rows
        }
        self.probes: list[Probe] = []

    def probe(self, candidate: Candidate) -> Probe:
        key = (
            candidate.runtime_mode,
            candidate.context,
            candidate.ngl,
            candidate.tensor_split,
        )
        if key not in self._fixtures:
            raise KeyError(f"missing finetune v2 fixture for {key}")
        row = self._fixtures[key]
        free_vram_mib = row["free_vram_mib"]
        if (
            not isinstance(free_vram_mib, (list, tuple))
            or len(free_vram_mib) != 2
            or any(not isinstance(value, int | float) for value in free_vram_mib)
        ):
            raise ValueError(f"fixture free_vram_mib must contain two values for {key}")
        probe = Probe(
            candidate=candidate,
            success=bool(row["success"]),
            free_vram_mib=(float(free_vram_mib[0]), float(free_vram_mib[1])),
            total_seconds=float(row["total_seconds"]),
            order=len(self.probes),
            telemetry_source=str(row.get("telemetry_source", "post_smoke")),
            cache_backed=bool(row.get("cache_backed", False)),
            error=row.get("error"),  # type: ignore[arg-type]
        )
        self.probes.append(probe)
        return probe


def dry_run_preserves_models_yaml(models_path: Path, operation: Callable[[], object]) -> None:
    before = models_path.read_bytes()
    try:
        operation()
    except BaseException as exc:
        after = models_path.read_bytes()
        if before != after:
            raise AssertionError("dry-run operation changed models.yaml bytes") from exc
        raise
    after = models_path.read_bytes()
    if before != after:
        raise AssertionError("dry-run operation changed models.yaml bytes")
