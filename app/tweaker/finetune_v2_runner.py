"""Guardian-backed finetune v2 runtime runner.

This module wires the pure v2 contracts to live Guardian probes without using
the v1 finetune engine. Dry-run probes pass runtime overrides to `/admin/load`
and keep `models.yaml` unchanged; only a final `apply=True` writes the winning
runtime fields back to disk.
"""

from __future__ import annotations

import copy
import json
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Mapping, Protocol, Sequence

import httpx
import yaml

from app.tweaker.finetune_v2_contracts import (
    Candidate,
    PlanAction,
    Probe,
    RuntimeLimits,
    convergence_status_from_history,
    initial_seed_candidates,
    next_after_seed_failure,
    rank_successes,
    split_rebalance_action,
    unique_explicit_ngls,
    upward_ngl_retry_actions,
)
from app.tweaker.model_finetune import (
    apply_runtime_search_values,
    build_smoke_messages,
    build_split_candidates,
    format_two_gpu_split,
    has_vision_runtime,
    parse_two_gpu_split,
    read_gpu_vram_snapshot,
    render_model_block,
    replace_model_block,
    resolve_runtime_config_value,
    resolve_runtime_mode,
    resolve_runtime_total_layers,
    runtime_mode_uses_vision,
    two_gpu_free_mib,
)


DEFAULT_V2_RESULTS_FILE = "data/model_finetune_v2_results.json"


@dataclass(frozen=True)
class FinetuneV2Result:
    """Completed v2 tuning outcome."""

    model: str
    runtime_mode: str
    optimization: str
    winner: Probe
    winner_explanation: dict[str, object]
    convergence: dict[str, object]
    probes: list[Probe] = field(default_factory=list)
    applied: bool = False
    results_file: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "runtime_mode": self.runtime_mode,
            "optimization": self.optimization,
            "winner": _probe_to_dict(self.winner),
            "winner_explanation": self.winner_explanation,
            "convergence": self.convergence,
            "probes": [_probe_to_dict(probe) for probe in self.probes],
            "applied": self.applied,
            "results_file": self.results_file,
        }


def _candidate_to_dict(candidate: Candidate) -> dict[str, object]:
    return asdict(candidate)


def _probe_to_dict(probe: Probe) -> dict[str, object]:
    payload = asdict(probe)
    payload["candidate"] = _candidate_to_dict(probe.candidate)
    return payload


class FinetuneV2ResultsLog:
    """Append-auditable JSON persistence for v2 runs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._active_path = self.path.with_suffix(f"{self.path.suffix}.active")
        self._write_lock = Lock()
        self.history = self._load()
        self.active_index: int | None = None

    def start_run(self, **fields: object) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "running",
            "version": 2,
            "probes": [],
            **fields,
        }
        self.history.append(entry)
        self.active_index = len(self.history) - 1
        self._persist()
        self._persist_active_entry()

    def append_probe(self, probe: Probe) -> None:
        entry = self._active_entry()
        probes = entry.setdefault("probes", [])
        if not isinstance(probes, list):
            probes = []
            entry["probes"] = probes
        probes.append(_probe_to_dict(probe))
        self._persist_active_entry()

    def complete_run(self, **fields: object) -> None:
        entry = self._active_entry()
        entry.update(fields)
        entry["status"] = "completed"
        entry["completed_at"] = datetime.now(UTC).isoformat()
        self._persist()
        self._clear_active_entry()
        self.active_index = None

    def fail_run(self, exc: BaseException) -> None:
        if self.active_index is None:
            return
        entry = self._active_entry()
        entry.update(
            {
                "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                "error": str(exc),
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )
        self._persist()
        self._clear_active_entry()
        self.active_index = None

    def _active_entry(self) -> dict[str, object]:
        if self.active_index is None:
            raise RuntimeError("finetune v2 result log has no active run")
        entry = self.history[self.active_index]
        if not isinstance(entry, dict):
            raise RuntimeError("finetune v2 result log active entry is invalid")
        return entry

    def _preserve_unreadable_file(self) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        suffix = 1
        while backup_path.exists():
            backup_path = self.path.with_name(
                f"{self.path.name}.corrupt-{timestamp}-{suffix}"
            )
            suffix += 1
        self.path.replace(backup_path)
        return backup_path

    def _load(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            preserved_path: str | None = None
            preservation_error: str | None = None
            try:
                preserved_path = str(self._preserve_unreadable_file())
            except OSError as rename_exc:
                preservation_error = str(rename_exc)
            return [
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "status": "error",
                    "version": 2,
                    "error": f"Unreadable finetune v2 results log: {exc}",
                    "corrupt_log_path": str(self.path),
                    "preserved_corrupt_log_path": preserved_path,
                    "preserve_error": preservation_error,
                    "probes": [],
                }
            ]
        return payload if isinstance(payload, list) else []

    def _persist(self) -> None:
        with self._write_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            tmp_path.write_text(json.dumps(self.history, indent=2))
            tmp_path.replace(self.path)

    def _persist_active_entry(self) -> None:
        if self.active_index is None:
            return
        with self._write_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._active_path.with_suffix(f"{self._active_path.suffix}.tmp")
            tmp_path.write_text(json.dumps(self._active_entry(), indent=2))
            tmp_path.replace(self._active_path)

    def _clear_active_entry(self) -> None:
        with self._write_lock:
            try:
                self._active_path.unlink()
            except FileNotFoundError:
                pass


class ProbeRunnerProtocol(Protocol):
    """Minimal interface required by FinetuneV2Runner."""

    def probe(self, model: str, candidate: Candidate) -> Probe: ...

    def verify_disk_load(self, model: str, *, enable_vision: bool = False) -> bool: ...


class GuardianV2ProbeRunner:
    """Execute one v2 probe through Guardian runtime overrides."""

    def __init__(
        self,
        *,
        guardian_url: str,
        api_key: str,
        smoke_prompt: str,
        smoke_max_tokens: int,
        smoke_image_url: str | None,
        client: httpx.Client | None = None,
    ) -> None:
        self.guardian_url = guardian_url.rstrip("/")
        self.smoke_prompt = smoke_prompt
        self.smoke_max_tokens = smoke_max_tokens
        self.smoke_image_url = smoke_image_url
        self.client = client or httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(900.0, connect=10.0),
        )
        self._owns_client = client is None
        self.probes: list[Probe] = []

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def verify_disk_load(self, model: str, *, enable_vision: bool = False) -> bool:
        """Load the model from disk without runtime overrides to verify the on-disk config."""
        load_payload: dict[str, object] = {"model": model, "enable_vision": enable_vision}
        response: httpx.Response | None = None
        try:
            response = self.client.post(
                f"{self.guardian_url}/admin/load",
                json=load_payload,
            )
            return response.status_code == 200
        except httpx.RequestError:
            return False
        finally:
            if response is not None:
                response.close()

    def probe(self, model: str, candidate: Candidate) -> Probe:
        started = time.perf_counter()
        load_payload = {
            "model": model,
            "enable_vision": runtime_mode_uses_vision(getattr(candidate, "runtime_mode", "text")),
            "runtime_overrides": {
                "context": candidate.context,
                "ngl": candidate.ngl,
                "tensor_split": candidate.tensor_split,
            },
        }
        pre_load_vram = read_gpu_vram_snapshot()
        load_response: httpx.Response | None = None
        try:
            load_response = self.client.post(f"{self.guardian_url}/admin/load", json=load_payload)
        except httpx.RequestError as exc:
            probe = self._build_probe(
                candidate,
                success=False,
                started=started,
                free_vram_mib=two_gpu_free_mib(pre_load_vram),
                telemetry_source="pre_load",
                error=str(exc),
            )
            self.probes.append(probe)
            return probe

        if load_response.status_code != 200:
            probe = self._build_probe(
                candidate,
                success=False,
                started=started,
                free_vram_mib=two_gpu_free_mib(pre_load_vram),
                telemetry_source="pre_load",
                error=load_response.text,
            )
            self.probes.append(probe)
            load_response.close()
            return probe

        load_response.close()
        if runtime_mode_uses_vision(getattr(candidate, "runtime_mode", "text")) and not self.smoke_image_url:
            gpu_vram = read_gpu_vram_snapshot()
            probe = self._build_probe(
                candidate,
                success=False,
                started=started,
                free_vram_mib=two_gpu_free_mib(gpu_vram),
                telemetry_source="post_load",
                error="vision finetune requires smoke_image_url to exercise the multimodal path",
            )
            self.probes.append(probe)
            return probe

        smoke_response: httpx.Response | None = None
        try:
            smoke_response = self.client.post(
                f"{self.guardian_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": build_smoke_messages(self.smoke_prompt, self.smoke_image_url),
                    "temperature": 0.0,
                    "max_tokens": self.smoke_max_tokens,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
        except httpx.RequestError as exc:
            gpu_vram = read_gpu_vram_snapshot()
            probe = self._build_probe(
                candidate,
                success=False,
                started=started,
                free_vram_mib=two_gpu_free_mib(gpu_vram),
                telemetry_source="post_load",
                error=str(exc),
            )
            self.probes.append(probe)
            return probe
        try:
            gpu_vram = read_gpu_vram_snapshot()
            probe = self._build_probe(
                candidate,
                success=smoke_response.status_code == 200,
                started=started,
                free_vram_mib=two_gpu_free_mib(gpu_vram),
                telemetry_source="post_smoke",
                error=None if smoke_response.status_code == 200 else smoke_response.text,
            )
            self.probes.append(probe)
            return probe
        finally:
            if smoke_response is not None:
                smoke_response.close()

    def _build_probe(
        self,
        candidate: Candidate,
        *,
        success: bool,
        started: float,
        free_vram_mib: tuple[float, float] | None,
        telemetry_source: str,
        error: str | None,
    ) -> Probe:
        return Probe(
            candidate=candidate,
            success=success,
            free_vram_mib=free_vram_mib,
            total_seconds=time.perf_counter() - started,
            order=len(self.probes),
            telemetry_source=telemetry_source,
            error=error,
        )


class FinetuneV2Runner:
    """Plan, probe, rank, persist, and optionally apply one v2 tuning run."""

    def __init__(
        self,
        *,
        models_config_path: str | Path,
        results_file: str | Path,
        probe_runner: ProbeRunnerProtocol,
        runtime_mode: str = "auto",
        smoke_image_url: str | None = None,
    ) -> None:
        self.models_config_path = Path(models_config_path)
        self.results = FinetuneV2ResultsLog(results_file)
        self.probe_runner = probe_runner
        effective_smoke_image_url = smoke_image_url
        if effective_smoke_image_url is None:
            effective_smoke_image_url = getattr(probe_runner, "smoke_image_url", None)
        self.runtime_mode = resolve_runtime_mode(runtime_mode, effective_smoke_image_url)
        self._base_text = ""
        self._base_config: Mapping[str, object] = {}
        self._base_mtime_ns: int | None = None
        self._reload_base_snapshot()

    def _reload_base_snapshot(self) -> None:
        self._base_text = self.models_config_path.read_text()
        self._base_config = self._load_models_config(self._base_text)
        self._base_mtime_ns = self.models_config_path.stat().st_mtime_ns

    def _refresh_base_snapshot_if_changed(self) -> None:
        current_mtime_ns = self.models_config_path.stat().st_mtime_ns
        if self._base_mtime_ns != current_mtime_ns:
            self._reload_base_snapshot()

    def _load_models_config(self, text: str) -> Mapping[str, object]:
        loaded = yaml.safe_load(text)
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(
                "models.yaml root must be a mapping/object; "
                f"found {type(loaded).__name__}"
            )
        return loaded

    def _normalize_split_candidates(
        self, split_candidates: Sequence[str] | None
    ) -> Sequence[str] | None:
        if split_candidates is None:
            return None
        normalized: list[str] = []
        for split in split_candidates:
            text = str(split).strip()
            if not text:
                raise ValueError("split_candidates entries must be non-empty")
            ratio = parse_two_gpu_split(text)
            if ratio is None or not math.isfinite(ratio):
                raise ValueError(
                    f"Invalid split candidate '{text}'; expected two finite numeric values like '0.55,0.45'"
                )
            candidate = format_two_gpu_split(ratio)
            if candidate not in normalized:
                normalized.append(candidate)
        if not normalized:
            raise ValueError("split_candidates must include at least one valid split")
        return normalized

    @property
    def base_text(self) -> str:
        self._refresh_base_snapshot_if_changed()
        return self._base_text

    @base_text.setter
    def base_text(self, value: str) -> None:
        self._base_text = value
        self._base_config = self._load_models_config(value)
        try:
            self._base_mtime_ns = self.models_config_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._base_mtime_ns = None

    @property
    def base_config(self) -> Mapping[str, object]:
        self._refresh_base_snapshot_if_changed()
        return self._base_config

    @base_config.setter
    def base_config(self, value: Mapping[str, object]) -> None:
        self._base_config = value
        try:
            self._base_mtime_ns = self.models_config_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._base_mtime_ns = None

    def tune_model(
        self,
        model_name: str,
        *,
        optimization: str,
        fixed_context: int | None = None,
        fixed_ngl: int | None = None,
        split_candidates: Sequence[str] | None = None,
        ngl_step: int = 1,
        split_min: float = 0.30,
        split_max: float = 0.70,
        apply: bool = False,
    ) -> FinetuneV2Result:
        canonical_model = self._resolve_model(model_name)
        split_candidates = self._normalize_split_candidates(split_candidates)
        model_config = copy.deepcopy(self.base_config.get("models", {}).get(canonical_model, {}))
        if self.runtime_mode == "vision" and not has_vision_runtime(model_config):
            raise ValueError(f"Model '{canonical_model}' does not have a configured vision runtime")
        limits = self._runtime_limits(model_config, fixed_context=fixed_context)
        seed_split = self._seed_split(model_config, split_min=split_min, split_max=split_max)
        has_mmproj = self.runtime_mode == "vision"
        speed_context_floor = None
        if optimization == "speed":
            speed_context_floor = fixed_context if fixed_context is not None else limits.active_context
        allowed_context = speed_context_floor if speed_context_floor is not None else (fixed_context or limits.max_context)
        allowed_ngl = fixed_ngl if fixed_ngl is not None else limits.total_layers
        probes: list[Probe] = []
        # Upward ngl retries are prepended so local follow-ups run before the wider candidate grid.
        seed_candidates = initial_seed_candidates(
            limits,
            optimization=optimization,
            seed_split=seed_split,
            fixed_context=fixed_context,
            fixed_ngl=fixed_ngl,
            runtime_mode=self.runtime_mode,
            has_mmproj=has_mmproj,
        )
        queued: deque[PlanAction] = deque(
            PlanAction(
                "seed",
                candidate,
                "initial_seed",
            )
            for candidate in seed_candidates
        )
        queued.extend(
            PlanAction("candidate_grid", candidate, "fixed_or_followup_candidate")
            for candidate in self._candidate_grid(
                limits=limits,
                optimization=optimization,
                seed_split=seed_split,
                has_mmproj=has_mmproj,
                fixed_context=fixed_context,
                fixed_ngl=fixed_ngl,
                split_candidates=split_candidates,
                ngl_step=ngl_step,
                split_min=split_min,
                split_max=split_max,
            )
        )
        seen_candidates: set[tuple[int, int, str, str, bool]] = set()
        low_headroom_followups_used = 0
        remaining_low_headroom_followups: int | None = None
        convergence: dict[str, object] = {"should_continue": True, "reason": "not_started"}

        self.results.start_run(
            model=canonical_model,
            runtime_mode=self.runtime_mode,
            optimization=optimization,
            fixed_context=fixed_context,
            fixed_ngl=fixed_ngl,
            applied=apply,
        )
        try:
            while queued:
                if remaining_low_headroom_followups is not None:
                    if remaining_low_headroom_followups <= 0:
                        break

                action = queued.popleft()
                candidate = self._clamp_candidate(action.candidate, limits, fixed_context, fixed_ngl)
                key = (
                    candidate.context,
                    candidate.ngl,
                    candidate.tensor_split,
                    candidate.runtime_mode,
                    candidate.has_mmproj,
                )
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                probe = self.probe_runner.probe(canonical_model, candidate)
                probes.append(probe)
                self.results.append_probe(probe)
                if remaining_low_headroom_followups is not None:
                    remaining_low_headroom_followups -= 1
                    low_headroom_followups_used += 1

                if probe.success:
                    convergence = convergence_status_from_history(
                        probes,
                        limits,
                        optimization=optimization,
                        context_floor=speed_context_floor,
                        low_headroom_followups_used=low_headroom_followups_used,
                        allowed_context=allowed_context,
                        allowed_ngl=allowed_ngl,
                    )
                    if convergence["should_continue"] is False:
                        break
                    if (
                        convergence["reason"] == "low_headroom_followup"
                        and remaining_low_headroom_followups is None
                    ):
                        remaining_low_headroom_followups = int(convergence["remaining_followups"])
                    elif remaining_low_headroom_followups is not None and convergence["reason"] != "low_headroom_followup":
                        break
                    if fixed_ngl is None:
                        retry_actions = upward_ngl_retry_actions(probe, limits, max_retries=1)
                        for retry_action in reversed(retry_actions):
                            queued.appendleft(retry_action)
                    rebalance = split_rebalance_action(probes, better_split=self._better_split(probe, split_min, split_max))
                    if rebalance is not None:
                        queued.append(rebalance)
                else:
                    if remaining_low_headroom_followups is not None:
                        convergence = convergence_status_from_history(
                            probes,
                            limits,
                            optimization=optimization,
                            context_floor=speed_context_floor,
                            low_headroom_followups_used=low_headroom_followups_used,
                            allowed_context=allowed_context,
                            allowed_ngl=allowed_ngl,
                        )
                        if convergence["should_continue"] is False:
                            break
                        if convergence["reason"] != "low_headroom_followup":
                            break
                    seed_retry = next_after_seed_failure(probes, limits)
                    if seed_retry is not None and fixed_ngl is None:
                        queued.append(seed_retry)

            winner, explanation = rank_successes(
                probes,
                optimization=optimization,
                context_floor=speed_context_floor,
            )
            convergence = convergence_status_from_history(
                probes,
                limits,
                optimization=optimization,
                context_floor=speed_context_floor,
                low_headroom_followups_used=low_headroom_followups_used,
                allowed_context=allowed_context,
                allowed_ngl=allowed_ngl,
            )
            if apply:
                self._apply_winner(canonical_model, model_config, winner)
            result = FinetuneV2Result(
                model=canonical_model,
                runtime_mode=self.runtime_mode,
                optimization=optimization,
                winner=winner,
                winner_explanation=explanation,
                convergence=convergence,
                probes=probes,
                applied=apply,
                results_file=str(self.results.path),
            )
            self.results.complete_run(
                winner=_probe_to_dict(winner),
                winner_explanation=explanation,
                convergence=convergence,
                applied=apply,
            )
            return result
        except BaseException as exc:
            self.results.fail_run(exc)
            raise

    def _candidate_grid(
        self,
        *,
        limits: RuntimeLimits,
        optimization: str,
        seed_split: str,
        has_mmproj: bool,
        fixed_context: int | None,
        fixed_ngl: int | None,
        split_candidates: Sequence[str] | None,
        ngl_step: int,
        split_min: float,
        split_max: float,
    ) -> list[Candidate]:
        contexts = [fixed_context] if fixed_context is not None else [limits.active_context, limits.max_context]
        if optimization == "speed" and fixed_context is None:
            contexts = [limits.active_context]
        if optimization == "context" and fixed_context is None:
            contexts = [limits.max_context, limits.active_context]
        ngl_stride = max(1, ngl_step)
        # Use -1 as the stop value, then append ngl=0 if stride alignment missed it.
        ngls = [fixed_ngl] if fixed_ngl is not None else list(range(limits.total_layers, -1, -ngl_stride))
        if fixed_ngl is None and 0 not in ngls:
            ngls.append(0)
        ngls = unique_explicit_ngls([ngl for ngl in ngls if ngl is not None], limits)
        splits = list(split_candidates or build_split_candidates(seed_split, 0.05, split_min, split_max))
        candidates: list[Candidate] = []
        for context in contexts:
            if context is None:
                continue
            for ngl in ngls:
                for split in splits:
                    if split is None:
                        continue
                    candidates.append(
                        Candidate(
                            context=int(context),
                            ngl=ngl,
                            tensor_split=split,
                            runtime_mode=self.runtime_mode,
                            has_mmproj=has_mmproj,
                        )
                    )
        return candidates

    def _runtime_limits(self, model_config: Mapping[str, object], *, fixed_context: int | None) -> RuntimeLimits:
        total_layers = resolve_runtime_total_layers(dict(model_config), self.runtime_mode)
        if total_layers is None:
            raise ValueError("finetune v2 requires total_layers for the selected runtime")
        active_context = resolve_runtime_config_value(dict(model_config), "context", self.runtime_mode)
        benchmark_context_limit = model_config.get("benchmark_context_limit")
        if benchmark_context_limit is not None:
            max_context = benchmark_context_limit
        elif active_context is not None:
            max_context = active_context
        else:
            max_context = fixed_context
        if active_context is None:
            active_context = max_context
        if max_context is None:
            raise ValueError("finetune v2 requires a configured context or benchmark_context_limit")
        return RuntimeLimits(total_layers=total_layers, max_context=int(max_context), active_context=int(active_context))

    def _seed_split(self, model_config: Mapping[str, object], *, split_min: float, split_max: float) -> str:
        split = resolve_runtime_config_value(dict(model_config), "tensor_split", self.runtime_mode)
        return str(split or format_two_gpu_split(min(max(0.5, split_min), split_max)))

    def _better_split(self, probe: Probe, split_min: float, split_max: float) -> str:
        values = probe.free_vram_mib
        if values is None or values[0] == values[1]:
            return probe.candidate.tensor_split
        primary = float(probe.candidate.tensor_split.split(",", 1)[0])
        direction = 0.05 if values[0] > values[1] else -0.05
        return format_two_gpu_split(min(max(primary + direction, split_min), split_max))

    def _clamp_candidate(
        self,
        candidate: Candidate,
        limits: RuntimeLimits,
        fixed_context: int | None,
        fixed_ngl: int | None,
    ) -> Candidate:
        clamped_context = fixed_context if fixed_context is not None else min(candidate.context, limits.max_context)
        clamped_ngl = fixed_ngl if fixed_ngl is not None else candidate.ngl
        clamped_ngl = min(int(clamped_ngl), limits.total_layers)

        return replace(
            candidate,
            context=int(clamped_context),
            ngl=clamped_ngl,
        )

    def _apply_winner(self, model_name: str, model_config: dict[str, object], winner: Probe) -> None:
        applied_config = apply_runtime_search_values(
            model_config,
            context=winner.candidate.context,
            ngl=winner.candidate.ngl,
            tensor_split=winner.candidate.tensor_split,
            runtime_mode=self.runtime_mode,
        )
        rendered = render_model_block(model_name, applied_config)
        applied_text = replace_model_block(self.base_text, model_name, rendered)
        previous_text = self.models_config_path.read_text()
        tmp_path = self.models_config_path.with_suffix(f"{self.models_config_path.suffix}.tmp")
        rollback_path = self.models_config_path.with_suffix(f"{self.models_config_path.suffix}.rollback")

        def restore_previous_config() -> None:
            rollback_path.write_text(previous_text)
            rollback_path.replace(self.models_config_path)
            self.probe_runner.verify_disk_load(
                model_name,
                enable_vision=runtime_mode_uses_vision(self.runtime_mode),
            )

        tmp_path.write_text(applied_text)
        tmp_path.replace(self.models_config_path)
        try:
            enable_vision = runtime_mode_uses_vision(self.runtime_mode)
            disk_ok = self.probe_runner.verify_disk_load(model_name, enable_vision=enable_vision)
            if not disk_ok:
                raise RuntimeError(
                    f"Applied finetune v2 winner failed no-override disk-load verification for '{model_name}'"
                )
            applied_probe = self.probe_runner.probe(model_name, winner.candidate)
            if not applied_probe.success:
                raise RuntimeError(
                    f"Applied finetune v2 winner failed to reload: {applied_probe.error or 'unknown error'}"
                )
        except Exception:
            restore_previous_config()
            raise

    def _resolve_model(self, requested_name: str) -> str:
        models = self.base_config.get("models", {})
        if requested_name in models:
            return requested_name
        aliases = self.base_config.get("aliases", {})
        target = aliases.get(requested_name)
        if target in models:
            return str(target)
        requested_lower = requested_name.lower()
        for model_name in models:
            if model_name.lower() == requested_lower:
                return str(model_name)
        raise ValueError(f"Model '{requested_name}' not found in models.yaml")
