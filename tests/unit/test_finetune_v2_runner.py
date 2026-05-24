"""Tests for the Guardian-backed finetune v2 runner."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.tweaker.finetune_v2_contracts import Candidate, Probe
from app.tweaker.finetune_v2_runner import (
    FinetuneV2ResultsLog,
    FinetuneV2Runner,
    GuardianV2ProbeRunner,
)


def _write_models(path: Path) -> None:
    path.write_text(
        """\
models:
  TestModel:
    path: /tmp/test-model.gguf
    context: 65536
    benchmark_context_limit: 131072
    ngl: 40
    total_layers: 41
    tensor_split: "0.55,0.45"
    vision_mmproj: /tmp/mmproj.gguf
    vision_context: 32768
    vision_ngl: 38
    vision_total_layers: 41
    vision_tensor_split: "0.60,0.40"
aliases:
  test: TestModel
"""
    )


def _write_text_only_models(path: Path) -> None:
    path.write_text(
        """\
models:
  TestModel:
    path: /tmp/test-model.gguf
    context: 65536
    benchmark_context_limit: 131072
    ngl: 40
    total_layers: 41
    tensor_split: "0.55,0.45"
aliases:
  test: TestModel
"""
    )


class FakeProbeRunner:
    def __init__(self, free_vram_mib=(300.0, 280.0)) -> None:
        self.free_vram_mib = free_vram_mib
        self.calls: list[tuple[str, Candidate]] = []
        self.disk_load_calls: list[tuple[str, bool]] = []

    def verify_disk_load(self, model: str, *, enable_vision: bool = False) -> bool:
        self.disk_load_calls.append((model, enable_vision))
        return True

    def probe(self, model: str, candidate: Candidate) -> Probe:
        self.calls.append((model, candidate))
        return Probe(
            candidate=candidate,
            success=True,
            free_vram_mib=self.free_vram_mib,
            total_seconds=float(len(self.calls)),
            order=len(self.calls) - 1,
        )


class SequencedProbeRunner:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, Candidate]] = []
        self.disk_load_calls: list[tuple[str, bool]] = []

    def verify_disk_load(self, model: str, *, enable_vision: bool = False) -> bool:
        self.disk_load_calls.append((model, enable_vision))
        return True

    def probe(self, model: str, candidate: Candidate) -> Probe:
        self.calls.append((model, candidate))
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        success = self.outcomes[index]
        return Probe(
            candidate=candidate,
            success=success,
            free_vram_mib=(300.0, 280.0),
            total_seconds=float(len(self.calls)),
            order=len(self.calls) - 1,
            error=None if success else "probe failed",
        )


class RestoreFailingProbeRunner(SequencedProbeRunner):
    def verify_disk_load(self, model: str, *, enable_vision: bool = False) -> bool:
        self.disk_load_calls.append((model, enable_vision))
        return False


class CandidateMapProbeRunner:
    def __init__(self, outcomes: dict[tuple[int, int, str, str, bool], Probe]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, Candidate]] = []
        self.disk_load_calls: list[tuple[str, bool]] = []

    def verify_disk_load(self, model: str, *, enable_vision: bool = False) -> bool:
        self.disk_load_calls.append((model, enable_vision))
        return True

    def probe(self, model: str, candidate: Candidate) -> Probe:
        self.calls.append((model, candidate))
        key = (
            candidate.context,
            candidate.ngl,
            candidate.tensor_split,
            candidate.runtime_mode,
            candidate.has_mmproj,
        )
        template = self.outcomes[key]
        return Probe(
            candidate=candidate,
            success=template.success,
            free_vram_mib=template.free_vram_mib,
            gpu_vram=template.gpu_vram,
            backend_gpu_vram=template.backend_gpu_vram,
            effective_tensor_split=template.effective_tensor_split,
            total_seconds=template.total_seconds,
            order=len(self.calls) - 1,
            telemetry_source=template.telemetry_source,
            cache_backed=template.cache_backed,
            error=template.error,
        )


def test_v2_no_successful_probes_fails_with_operator_error(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    probe_runner = SequencedProbeRunner([False])
    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="text",
    )

    with pytest.raises(RuntimeError, match="no successful probes") as excinfo:
        runner.tune_model(
            "TestModel",
            optimization="context",
            fixed_context=32768,
            fixed_ngl=38,
            split_candidates=["0.55,0.45"],
        )

    assert "context=32768" in str(excinfo.value)
    assert "ngl=38" in str(excinfo.value)
    assert probe_runner.disk_load_calls == [("TestModel", False)]
    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "failed"
    assert "no successful probes" in history[-1]["error"]


def test_v2_no_successful_probes_reports_restore_failure_without_masking_primary(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    probe_runner = RestoreFailingProbeRunner([False])
    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="text",
    )

    with pytest.raises(RuntimeError) as excinfo:
        runner.tune_model(
            "TestModel",
            optimization="context",
            fixed_context=32768,
            fixed_ngl=38,
            split_candidates=["0.55,0.45"],
        )

    message = str(excinfo.value)
    assert "no successful probes" in message
    assert "dry-run restore also failed" in message
    assert "failed to restore disk runtime" in message
    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "failed"
    assert "dry-run restore also failed" in history[-1]["error"]


def test_v2_balances_before_retrying_higher_ngl(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 40')
        .replace('total_layers: 41', 'total_layers: 40', 1)
    )
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 40, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=40,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=False,
                free_vram_mib=(3000.0, 200.0),
                gpu_vram={
                    "0": {"free": 3000.0, "total": 12000.0, "free_pct": 25.0},
                    "1": {"free": 200.0, "total": 16000.0, "free_pct": 1.25},
                },
                total_seconds=1.0,
                order=0,
                telemetry_source="pre_load",
                error="oom",
            ),
            (32768, 39, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=39,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(3000.0, 200.0),
                gpu_vram={
                    "0": {"free": 3000.0, "total": 12000.0, "free_pct": 25.0},
                    "1": {"free": 200.0, "total": 16000.0, "free_pct": 1.25},
                },
                total_seconds=2.0,
                order=1,
            ),
            (32768, 39, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=39,
                    tensor_split="0.53,0.47",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(1400.0, 1800.0),
                gpu_vram={
                    "0": {"free": 1400.0, "total": 12000.0, "free_pct": 11.67},
                    "1": {"free": 1800.0, "total": 16000.0, "free_pct": 11.25},
                },
                total_seconds=3.0,
                order=2,
            ),
            (32768, 40, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=40,
                    tensor_split="0.53,0.47",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(1200.0, 1500.0),
                gpu_vram={
                    "0": {"free": 1200.0, "total": 12000.0, "free_pct": 10.0},
                    "1": {"free": 1500.0, "total": 16000.0, "free_pct": 9.375},
                },
                total_seconds=4.0,
                order=3,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        split_candidates=["0.48,0.52", "0.53,0.47"],
    )

    assert [call[1].ngl for call in probe_runner.calls] == [40, 39, 39, 40]
    assert [call[1].tensor_split for call in probe_runner.calls] == [
        "0.48,0.52",
        "0.48,0.52",
        "0.53,0.47",
        "0.53,0.47",
    ]
    assert result.winner.candidate.ngl == 40
    assert result.winner.candidate.tensor_split == "0.53,0.47"


def test_v2_failed_rebalance_retries_smaller_local_step_before_lower_ngl(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
    )
    imbalanced_gpu_vram = {
        "0": {"free": 211.0, "total": 12288.0, "free_pct": 1.72},
        "1": {"free": 3813.0, "total": 16311.0, "free_pct": 23.38},
    }
    balanced_gpu_vram = {
        "0": {"free": 900.0, "total": 12288.0, "free_pct": 7.32},
        "1": {"free": 1200.0, "total": 16311.0, "free_pct": 7.36},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(211.0, 3813.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.45,0.55", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.45,0.55",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=False,
                free_vram_mib=(211.0, 3813.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=2.0,
                order=1,
                telemetry_source="pre_load",
                error="oom",
            ),
            (32768, 38, "0.47,0.53", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.47,0.53",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(900.0, 1200.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=1.5,
                order=2,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="speed",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.48,0.52"],
        split_min=0.45,
        split_max=0.55,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == [
        "0.48,0.52",
        "0.45,0.55",
        "0.47,0.53",
    ]
    assert [call[1].ngl for call in probe_runner.calls] == [38, 38, 38]
    assert result.probes[-1].candidate.tensor_split == "0.47,0.53"


def test_v2_seed_step_down_failure_retries_same_ngl_split_before_lower_ngl(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 40')
        .replace('total_layers: 41', 'total_layers: 40', 1)
    )
    failed_gpu_vram = {
        "0": {"free": 10815.0, "total": 12288.0, "free_pct": 88.01},
        "1": {"free": 15709.0, "total": 16311.0, "free_pct": 96.31},
    }
    balanced_gpu_vram = {
        "0": {"free": 1200.0, "total": 12288.0, "free_pct": 9.77},
        "1": {"free": 1300.0, "total": 16311.0, "free_pct": 7.97},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 40, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=40,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=False,
                free_vram_mib=(1739.0, 1687.0),
                gpu_vram=failed_gpu_vram,
                total_seconds=1.0,
                order=0,
                telemetry_source="pre_load",
                error="allocating 497.00 MiB on device 1: cudaMalloc failed: out of memory",
            ),
            (32768, 39, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=39,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=False,
                free_vram_mib=(10815.0, 15709.0),
                gpu_vram=failed_gpu_vram,
                total_seconds=2.0,
                order=1,
                telemetry_source="pre_load",
                error="allocating 497.00 MiB on device 1: cudaMalloc failed: out of memory",
            ),
            (32768, 39, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=39,
                    tensor_split="0.53,0.47",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(1200.0, 1300.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=1.5,
                order=2,
            ),
            (32768, 40, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=40,
                    tensor_split="0.53,0.47",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(900.0, 1000.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=1.4,
                order=3,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        split_candidates=["0.48,0.52"],
        split_min=0.45,
        split_max=0.55,
    )

    assert [call[1].ngl for call in probe_runner.calls] == [40, 39, 39, 40]
    assert [call[1].tensor_split for call in probe_runner.calls] == [
        "0.48,0.52",
        "0.48,0.52",
        "0.53,0.47",
        "0.53,0.47",
    ]
    assert result.probes[2].candidate.ngl == 39
    assert result.probes[2].candidate.tensor_split == "0.53,0.47"


def test_v2_start_ngl_ladder_balances_before_each_upward_retry(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 38')
        .replace('total_layers: 41', 'total_layers: 38', 1)
    )
    imbalanced_37 = {
        "0": {"free": 2900.0, "total": 12000.0, "free_pct": 24.17},
        "1": {"free": 200.0, "total": 16000.0, "free_pct": 1.25},
    }
    balanced_37 = {
        "0": {"free": 1400.0, "total": 12000.0, "free_pct": 11.67},
        "1": {"free": 1500.0, "total": 16000.0, "free_pct": 9.38},
    }
    imbalanced_38 = {
        "0": {"free": 1800.0, "total": 12000.0, "free_pct": 15.0},
        "1": {"free": 320.0, "total": 16000.0, "free_pct": 2.0},
    }
    balanced_38 = {
        "0": {"free": 900.0, "total": 12000.0, "free_pct": 7.5},
        "1": {"free": 950.0, "total": 16000.0, "free_pct": 5.94},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 37, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=37,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(2900.0, 200.0),
                gpu_vram=imbalanced_37,
                total_seconds=1.0,
                order=0,
            ),
            (32768, 37, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=37,
                    tensor_split="0.53,0.47",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(1400.0, 1500.0),
                gpu_vram=balanced_37,
                total_seconds=1.5,
                order=1,
            ),
            (32768, 38, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.53,0.47",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(1800.0, 320.0),
                gpu_vram=imbalanced_38,
                total_seconds=2.0,
                order=2,
            ),
            (32768, 38, "0.55,0.45", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.55,0.45",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(900.0, 950.0),
                gpu_vram=balanced_38,
                total_seconds=2.5,
                order=3,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        start_ngl=37,
        split_candidates=["0.48,0.52"],
        split_min=0.45,
        split_max=0.55,
    )

    assert [call[1].ngl for call in probe_runner.calls] == [37, 37, 38, 38]
    assert [call[1].tensor_split for call in probe_runner.calls] == [
        "0.48,0.52",
        "0.53,0.47",
        "0.53,0.47",
        "0.55,0.45",
    ]
    assert result.start_ngl == 37
    assert result.winner.candidate.ngl == 38
    assert result.winner.candidate.tensor_split == "0.55,0.45"


def test_v2_start_ngl_ladder_uses_explicit_split_as_seed(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.54,0.46"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 38')
        .replace('total_layers: 41', 'total_layers: 38', 1)
    )
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 37, "0.46,0.54", "vision", True): Probe(
                candidate=Candidate(32768, 37, "0.46,0.54", "vision", True),
                success=True,
                free_vram_mib=(1200.0, 1300.0),
                gpu_vram={
                    "0": {"free": 1200.0, "total": 12000.0, "free_pct": 10.0},
                    "1": {"free": 1300.0, "total": 16000.0, "free_pct": 8.12},
                },
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.46,0.54", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.46,0.54", "vision", True),
                success=True,
                free_vram_mib=(900.0, 950.0),
                gpu_vram={
                    "0": {"free": 900.0, "total": 12000.0, "free_pct": 7.5},
                    "1": {"free": 950.0, "total": 16000.0, "free_pct": 5.94},
                },
                total_seconds=1.5,
                order=1,
            ),
        }
    )
    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        start_ngl=37,
        split_candidates=["0.46,0.54"],
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.46,0.54", "0.46,0.54"]
    assert result.winner.candidate.tensor_split == "0.46,0.54"


def test_v2_full_ladder_refines_split_before_higher_ngl_and_rebalances_again(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 38')
        .replace('total_layers: 41', 'total_layers: 38', 1)
    )

    def successful_probe(
        ngl: int,
        split: str,
        free_vram_mib: tuple[float, float],
        free_pct: tuple[float, float],
        total_seconds: float,
    ) -> Probe:
        return Probe(
            candidate=Candidate(32768, ngl, split, "vision", True),
            success=True,
            free_vram_mib=free_vram_mib,
            gpu_vram={
                "0": {"free": free_vram_mib[0], "total": 12000.0, "free_pct": free_pct[0]},
                "1": {"free": free_vram_mib[1], "total": 16000.0, "free_pct": free_pct[1]},
            },
            total_seconds=total_seconds,
            order=0,
        )

    probes = [
        successful_probe(37, "0.48,0.52", (3000.0, 200.0), (25.0, 1.25), 1.0),
        successful_probe(37, "0.53,0.47", (90.0, 95.0), (0.75, 0.59), 2.0),
        successful_probe(37, "0.52,0.48", (800.0, 820.0), (6.67, 5.13), 3.0),
        successful_probe(38, "0.52,0.48", (1100.0, 500.0), (9.0, 3.0), 4.0),
        successful_probe(38, "0.53,0.47", (720.0, 820.0), (6.0, 5.13), 5.0),
    ]
    probe_runner = CandidateMapProbeRunner(
        {
            (
                probe.candidate.context,
                probe.candidate.ngl,
                probe.candidate.tensor_split,
                probe.candidate.runtime_mode,
                probe.candidate.has_mmproj,
            ): probe
            for probe in probes
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        start_ngl=37,
        split_candidates=["0.48,0.52"],
    )

    assert [(call[1].ngl, call[1].tensor_split) for call in probe_runner.calls] == [
        (37, "0.48,0.52"),
        (37, "0.53,0.47"),
        (37, "0.52,0.48"),
        (38, "0.52,0.48"),
        (38, "0.53,0.47"),
    ]
    assert result.start_ngl == 37
    assert result.winner.candidate.ngl == 38
    assert result.winner.candidate.tensor_split == "0.53,0.47"
    history = json.loads(results_path.read_text())
    reasons = [probe["candidate"]["tensor_split"] for probe in history[-1]["probes"]]
    assert reasons == ["0.48,0.52", "0.53,0.47", "0.52,0.48", "0.52,0.48", "0.53,0.47"]
    assert not results_path.with_suffix(f"{results_path.suffix}.active").exists()


def test_v2_start_ngl_keeps_rebalancing_after_smaller_retry_success(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 38')
        .replace('total_layers: 41', 'total_layers: 38', 1)
    )
    imbalanced_gpu_vram = {
        "0": {"free": 143.0, "total": 12288.0, "free_pct": 1.16},
        "1": {"free": 4331.0, "total": 16311.0, "free_pct": 26.55},
    }
    balanced_gpu_vram = {
        "0": {"free": 800.0, "total": 12288.0, "free_pct": 6.51},
        "1": {"free": 850.0, "total": 16311.0, "free_pct": 5.21},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 37, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=37,
                    tensor_split="0.48,0.52",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(143.0, 4331.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=1.0,
                order=0,
            ),
            (32768, 37, "0.45,0.55", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=37,
                    tensor_split="0.45,0.55",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=False,
                free_vram_mib=(143.0, 4331.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=2.0,
                order=1,
                telemetry_source="pre_load",
                error="allocating 497.00 MiB on device 1: cudaMalloc failed: out of memory",
            ),
            (32768, 37, "0.47,0.53", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=37,
                    tensor_split="0.47,0.53",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(143.0, 4331.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=3.0,
                order=2,
            ),
            (32768, 37, "0.46,0.54", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=37,
                    tensor_split="0.46,0.54",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(800.0, 850.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=4.0,
                order=3,
            ),
            (32768, 38, "0.46,0.54", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.46,0.54",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(700.0, 720.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=5.0,
                order=4,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        start_ngl=37,
        split_candidates=["0.48,0.52"],
        split_min=0.45,
        split_max=0.55,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == [
        "0.48,0.52",
        "0.45,0.55",
        "0.47,0.53",
        "0.46,0.54",
        "0.46,0.54",
    ]
    assert [call[1].ngl for call in probe_runner.calls] == [37, 37, 37, 37, 38]
    assert result.winner.candidate.ngl == 38
    assert result.winner.candidate.tensor_split == "0.46,0.54"


def test_v2_start_ngl_exhausts_remaining_rung_splits_before_stopping(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text()
        .replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.48,0.52"')
        .replace('vision_total_layers: 41', 'vision_total_layers: 38')
        .replace('total_layers: 41', 'total_layers: 38', 1)
    )
    imbalanced_gpu_vram = {
        "0": {"free": 143.0, "total": 12288.0, "free_pct": 1.16},
        "1": {"free": 4331.0, "total": 16311.0, "free_pct": 26.55},
    }
    imbalanced_gpu_vram_mid = {
        "0": {"free": 675.0, "total": 12288.0, "free_pct": 5.49},
        "1": {"free": 3799.0, "total": 16311.0, "free_pct": 23.29},
    }
    balanced_gpu_vram = {
        "0": {"free": 900.0, "total": 12288.0, "free_pct": 7.32},
        "1": {"free": 940.0, "total": 16311.0, "free_pct": 5.76},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 37, "0.48,0.52", "vision", True): Probe(
                candidate=Candidate(32768, 37, "0.48,0.52", "vision", True),
                success=True,
                free_vram_mib=(143.0, 4331.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=1.0,
                order=0,
            ),
            (32768, 37, "0.45,0.55", "vision", True): Probe(
                candidate=Candidate(32768, 37, "0.45,0.55", "vision", True),
                success=False,
                free_vram_mib=(143.0, 4331.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=2.0,
                order=1,
                telemetry_source="pre_load",
                error="allocating 497.00 MiB on device 1: cudaMalloc failed: out of memory",
            ),
            (32768, 37, "0.47,0.53", "vision", True): Probe(
                candidate=Candidate(32768, 37, "0.47,0.53", "vision", True),
                success=True,
                free_vram_mib=(143.0, 4331.0),
                gpu_vram=imbalanced_gpu_vram,
                total_seconds=3.0,
                order=2,
            ),
            (32768, 37, "0.46,0.54", "vision", True): Probe(
                candidate=Candidate(32768, 37, "0.46,0.54", "vision", True),
                success=True,
                free_vram_mib=(675.0, 3799.0),
                gpu_vram=imbalanced_gpu_vram_mid,
                total_seconds=4.0,
                order=3,
            ),
            (32768, 37, "0.50,0.50", "vision", True): Probe(
                candidate=Candidate(32768, 37, "0.50,0.50", "vision", True),
                success=True,
                free_vram_mib=(900.0, 940.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=5.0,
                order=4,
            ),
            (32768, 38, "0.50,0.50", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.50,0.50", "vision", True),
                success=True,
                free_vram_mib=(900.0, 940.0),
                gpu_vram=balanced_gpu_vram,
                total_seconds=6.0,
                order=5,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        start_ngl=37,
        split_candidates=["0.48,0.52"],
        split_min=0.45,
        split_max=0.55,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == [
        "0.48,0.52",
        "0.45,0.55",
        "0.47,0.53",
        "0.46,0.54",
        "0.50,0.50",
        "0.50,0.50",
    ]
    assert [call[1].ngl for call in probe_runner.calls] == [37, 37, 37, 37, 37, 38]
    assert result.winner.candidate.ngl == 38
    assert result.winner.candidate.tensor_split == "0.50,0.50"


def test_v2_aborts_when_effective_tensor_split_does_not_change(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.55,0.45"')
    )
    repeated_gpu_vram = {
        "0": {"free": 1273.0, "total": 12288.0, "free_pct": 10.36},
        "1": {"free": 2689.0, "total": 16311.0, "free_pct": 16.49},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.55,0.45", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.55,0.45", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram=repeated_gpu_vram,
                effective_tensor_split="0.55,0.45",
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.54,0.46", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.54,0.46", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram=repeated_gpu_vram,
                effective_tensor_split="0.55,0.45",
                total_seconds=2.0,
                order=1,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    with pytest.raises(
        RuntimeError,
        match="effective tensor split did NOT change with a different requested split",
    ):
        runner.tune_model(
            "TestModel",
            optimization="context",
            fixed_context=32768,
            fixed_ngl=38,
            split_candidates=["0.55,0.45"],
            split_min=0.50,
            split_max=0.55,
        )

    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "failed"
    assert "effective tensor split did NOT change with a different requested split" in history[-1]["error"]
    assert not results_path.with_suffix(f"{results_path.suffix}.active").exists()
    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.55,0.45", "0.54,0.46"]


def test_v2_continues_when_neighbor_splits_share_backend_vram_bucket(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.54,0.46"')
    )
    repeated_host_gpu_vram = {
        "0": {"free": 1273.0, "total": 12288.0, "free_pct": 10.36},
        "1": {"free": 2689.0, "total": 16311.0, "free_pct": 16.49},
    }
    plateau_backend_gpu_vram = {
        "0": {"used": 9574.0, "total": 12288.0, "used_pct": 77.91},
        "1": {"used": 13146.0, "total": 16311.0, "used_pct": 80.60},
    }
    shifted_backend_gpu_vram = {
        "0": {"used": 10170.0, "total": 12288.0, "used_pct": 82.76},
        "1": {"used": 12550.0, "total": 16311.0, "used_pct": 76.94},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.54,0.46", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.54,0.46", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram=repeated_host_gpu_vram,
                backend_gpu_vram=plateau_backend_gpu_vram,
                effective_tensor_split="0.54,0.46",
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.53,0.47", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram=repeated_host_gpu_vram,
                backend_gpu_vram=plateau_backend_gpu_vram,
                effective_tensor_split="0.53,0.47",
                total_seconds=2.0,
                order=1,
            ),
            (32768, 38, "0.52,0.48", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.52,0.48", "vision", True),
                success=True,
                free_vram_mib=(677.0, 3285.0),
                gpu_vram={
                    "0": {"free": 677.0, "total": 12288.0, "free_pct": 5.51},
                    "1": {"free": 3285.0, "total": 16311.0, "free_pct": 20.14},
                },
                backend_gpu_vram=shifted_backend_gpu_vram,
                effective_tensor_split="0.52,0.48",
                total_seconds=3.0,
                order=2,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.54,0.46"],
        split_min=0.52,
        split_max=0.54,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.54,0.46", "0.53,0.47", "0.52,0.48"]
    assert result.probes[0].backend_gpu_vram == result.probes[1].backend_gpu_vram
    assert result.probes[1].backend_gpu_vram != result.probes[2].backend_gpu_vram
    assert not results_path.with_suffix(f"{results_path.suffix}.active").exists()


def test_v2_same_bucket_plateau_steps_in_same_split_direction(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.39,0.61"')
    )
    repeated_gpu_vram = {
        "0": {"free": 1409.0, "total": 12288.0, "free_pct": 11.47},
        "1": {"free": 3059.0, "total": 16311.0, "free_pct": 18.75},
    }
    plateau_backend_gpu_vram = {
        "0": {"used": 9436.0, "total": 12288.0, "used_pct": 76.79},
        "1": {"used": 12776.0, "total": 16311.0, "used_pct": 78.33},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.39,0.61", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.39,0.61", "vision", True),
                success=True,
                free_vram_mib=(1409.0, 3059.0),
                gpu_vram=repeated_gpu_vram,
                backend_gpu_vram=plateau_backend_gpu_vram,
                effective_tensor_split="0.39,0.61",
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.38,0.62", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.38,0.62", "vision", True),
                success=True,
                free_vram_mib=(1409.0, 3059.0),
                gpu_vram=repeated_gpu_vram,
                backend_gpu_vram=plateau_backend_gpu_vram,
                effective_tensor_split="0.38,0.62",
                total_seconds=2.0,
                order=1,
            ),
            (32768, 38, "0.37,0.63", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.37,0.63", "vision", True),
                success=True,
                free_vram_mib=(2250.0, 2300.0),
                gpu_vram={
                    "0": {"free": 2250.0, "total": 12288.0, "free_pct": 18.31},
                    "1": {"free": 2300.0, "total": 16311.0, "free_pct": 14.10},
                },
                backend_gpu_vram={
                    "0": {"used": 8578.0, "total": 12288.0, "used_pct": 69.81},
                    "1": {"used": 13634.0, "total": 16311.0, "used_pct": 83.59},
                },
                effective_tensor_split="0.37,0.63",
                total_seconds=3.0,
                order=2,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.39,0.61"],
        split_min=0.35,
        split_max=0.50,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.39,0.61", "0.38,0.62", "0.37,0.63"]
    assert result.probes[0].backend_gpu_vram == result.probes[1].backend_gpu_vram
    assert result.probes[1].backend_gpu_vram != result.probes[2].backend_gpu_vram
    assert not results_path.with_suffix(f"{results_path.suffix}.active").exists()


def test_v2_uses_backend_gpu_telemetry_before_host_free_for_plateau_detection(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.55,0.45"')
    )
    repeated_host_gpu_vram = {
        "0": {"free": 1273.0, "total": 12288.0, "free_pct": 10.36},
        "1": {"free": 2689.0, "total": 16311.0, "free_pct": 16.49},
    }
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.55,0.45", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.55,0.45", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram=repeated_host_gpu_vram,
                backend_gpu_vram={
                    "0": {"used": 10638.0, "total": 12288.0, "used_pct": 86.6},
                    "1": {"used": 13160.0, "total": 16311.0, "used_pct": 80.7},
                },
                effective_tensor_split="0.55,0.45",
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.54,0.46", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.54,0.46", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram=repeated_host_gpu_vram,
                backend_gpu_vram={
                    "0": {"used": 10702.0, "total": 12288.0, "used_pct": 87.1},
                    "1": {"used": 13096.0, "total": 16311.0, "used_pct": 80.3},
                },
                effective_tensor_split="0.54,0.46",
                total_seconds=2.0,
                order=1,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.55,0.45", "0.54,0.46"],
        split_min=0.54,
        split_max=0.55,
    )

    assert result.winner.candidate.tensor_split == "0.55,0.45"
    assert result.probes[0].backend_gpu_vram is not None
    assert result.probes[1].backend_gpu_vram is not None
    assert result.probes[0].backend_gpu_vram != result.probes[1].backend_gpu_vram
    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.55,0.45", "0.54,0.46"]
    assert not results_path.with_suffix(f"{results_path.suffix}.active").exists()


def test_v2_reverses_split_direction_when_neighbor_worsens_balance(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(
        models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.54,0.46"')
    )
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.54,0.46", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.54,0.46", "vision", True),
                success=True,
                free_vram_mib=(1273.0, 2689.0),
                gpu_vram={
                    "0": {"free": 1273.0, "total": 12288.0, "free_pct": 10.36},
                    "1": {"free": 2689.0, "total": 16311.0, "free_pct": 16.49},
                },
                total_seconds=1.0,
                order=0,
            ),
            (32768, 38, "0.53,0.47", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.53,0.47", "vision", True),
                success=True,
                free_vram_mib=(677.0, 3285.0),
                gpu_vram={
                    "0": {"free": 677.0, "total": 12288.0, "free_pct": 5.51},
                    "1": {"free": 3285.0, "total": 16311.0, "free_pct": 20.14},
                },
                total_seconds=2.0,
                order=1,
            ),
            (32768, 38, "0.55,0.45", "vision", True): Probe(
                candidate=Candidate(32768, 38, "0.55,0.45", "vision", True),
                success=True,
                free_vram_mib=(1600.0, 2400.0),
                gpu_vram={
                    "0": {"free": 1600.0, "total": 12288.0, "free_pct": 13.02},
                    "1": {"free": 2400.0, "total": 16311.0, "free_pct": 14.71},
                },
                total_seconds=3.0,
                order=2,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="context",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.54,0.46"],
        split_min=0.52,
        split_max=0.55,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.54,0.46", "0.53,0.47", "0.55,0.45"]
    assert result.probes[1].gpu_vram is not None
    assert result.probes[2].gpu_vram is not None
    assert result.winner.candidate.tensor_split == "0.55,0.45"
    assert not results_path.with_suffix(f"{results_path.suffix}.active").exists()


def test_v2_dry_run_keeps_models_yaml_unchanged_and_logs_probe_history(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    before = models_path.read_bytes()
    fake_runner = FakeProbeRunner()

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )
    result = runner.tune_model(
        "test",
        optimization="context",
        fixed_context=65536,
        fixed_ngl=40,
        split_candidates=["0.55,0.45"],
    )

    assert models_path.read_bytes() == before
    assert result.applied is False
    assert result.winner.candidate.context == 65536
    assert result.winner_explanation["comparator_mode"] == "context"
    assert fake_runner.disk_load_calls == [("TestModel", False)]
    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "completed"
    assert history[-1]["version"] == 2
    assert history[-1]["probes"][0]["candidate"]["context"] == 65536
    assert history[-1]["winner_explanation"]["winner_reason"]["code"] == "context_lexicographic_winner"


def test_v2_results_log_persists_active_run_incrementally(tmp_path: Path):
    results_path = tmp_path / "v2_results.json"
    log = FinetuneV2ResultsLog(results_path)
    candidate = Candidate(context=65536, ngl=40, tensor_split="0.55,0.45")
    probe = Probe(
        candidate=candidate,
        success=True,
        free_vram_mib=(300.0, 280.0),
        gpu_vram={
            "0": {"free": 300.0, "total": 12000.0, "free_pct": 2.5},
            "1": {"free": 280.0, "total": 16000.0, "free_pct": 1.75},
        },
        total_seconds=1.0,
        order=0,
    )

    log.start_run(model="TestModel", runtime_mode="text", optimization="speed", applied=False)
    history_after_start = results_path.read_text()
    active_path = results_path.with_suffix(f"{results_path.suffix}.active")

    log.append_probe(probe)

    assert results_path.read_text() != history_after_start
    live_history = json.loads(results_path.read_text())
    assert live_history[-1]["probes"][0]["gpu_vram"]["0"]["free"] == 300.0
    assert active_path.exists()
    active_history = json.loads(active_path.read_text())
    assert active_history["probes"][0]["gpu_vram"]["0"]["free"] == 300.0

    log.complete_run(applied=False)

    assert not active_path.exists()
    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "completed"
    assert history[-1]["probes"][0]["candidate"]["tensor_split"] == "0.55,0.45"


def test_v2_results_log_appends_completed_runs(tmp_path: Path):
    results_path = tmp_path / "v2_results.json"
    log = FinetuneV2ResultsLog(results_path)

    log.start_run(model="FirstModel", runtime_mode="text", optimization="speed", applied=False)
    log.complete_run(
        winner={"candidate": {"context": 65536, "ngl": 40, "tensor_split": "0.55,0.45"}},
        winner_explanation={"winner_reason": {"code": "speed_lexicographic_winner"}},
        convergence={"should_continue": False, "reason": "max_context_and_ngl"},
        applied=False,
    )

    log.start_run(model="SecondModel", runtime_mode="vision", optimization="context", applied=False)
    log.complete_run(
        winner={"candidate": {"context": 32768, "ngl": 37, "tensor_split": "0.48,0.52"}},
        winner_explanation={"winner_reason": {"code": "context_lexicographic_winner"}},
        convergence={"should_continue": False, "reason": "candidate_queue_exhausted"},
        applied=False,
    )

    history = json.loads(results_path.read_text())
    assert len(history) == 2
    assert history[0]["model"] == "FirstModel"
    assert history[1]["model"] == "SecondModel"


def test_v2_apply_writes_winner_once_to_runtime_specific_models_yaml_keys(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    fake_runner = FakeProbeRunner()

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="vision",
    )
    result = runner.tune_model(
        "TestModel",
        optimization="speed",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.60,0.40"],
        apply=True,
    )

    assert result.applied is True
    rendered = models_path.read_text()
    assert 'vision_context: 32768' in rendered
    assert 'vision_ngl: 38' in rendered
    assert 'vision_tensor_split: "0.60,0.40"' in rendered
    assert len(result.probes) == 1
    # Applying: 1 probe during tuning + 1 disk-load verification + 1 override probe to confirm reload.
    assert len(fake_runner.disk_load_calls) == 1
    assert fake_runner.disk_load_calls[0] == ("TestModel", True)
    assert len(fake_runner.calls) == 2


def test_v2_runner_rejects_non_mapping_models_yaml_root(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    models_path.write_text("- TestModel\n")

    with pytest.raises(ValueError, match="models.yaml root must be a mapping/object"):
        FinetuneV2Runner(
            models_config_path=models_path,
            results_file=results_path,
            probe_runner=FakeProbeRunner(),
            runtime_mode="text",
        )


@pytest.mark.parametrize(
    ("yaml_text", "expected_section"),
    [
        ("models: []\n", "models"),
        ("models: {}\naliases: []\n", "aliases"),
    ],
)
def test_v2_runner_rejects_non_mapping_models_or_aliases_sections(
    tmp_path: Path, yaml_text: str, expected_section: str
):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    models_path.write_text(yaml_text)

    with pytest.raises(ValueError, match=rf"models.yaml '{expected_section}' must be a mapping/object"):
        FinetuneV2Runner(
            models_config_path=models_path,
            results_file=results_path,
            probe_runner=FakeProbeRunner(),
            runtime_mode="text",
        )


def test_v2_fixed_context_and_ngl_pin_all_probes(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    fake_runner = FakeProbeRunner(free_vram_mib=(900.0, 850.0))

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )
    runner.tune_model(
        "TestModel",
        optimization="speed",
        fixed_context=65536,
        fixed_ngl=39,
        split_candidates=["0.55,0.45", "0.60,0.40"],
    )

    assert fake_runner.calls
    assert {call[1].context for call in fake_runner.calls} == {65536}
    assert {call[1].ngl for call in fake_runner.calls} == {39}


def test_v2_fixed_ngl_is_capped_to_total_layers(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    fake_runner = FakeProbeRunner()

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )
    result = runner.tune_model(
        "TestModel",
        optimization="speed",
        fixed_context=65536,
        fixed_ngl=99,
        split_candidates=["0.55,0.45"],
    )

    assert result.winner.candidate.ngl == 41
    assert {call[1].ngl for call in fake_runner.calls} == {41}


def test_v2_normalizes_explicit_split_candidates(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    fake_runner = FakeProbeRunner()
    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )

    normalized = runner._normalize_split_candidates(["1,3"])

    assert normalized == ["0.25,0.75"]


@pytest.mark.parametrize("split", ["0.55", "abc,def", "nan,1", "inf,1"])
def test_v2_rejects_invalid_explicit_split_candidates(tmp_path: Path, split: str):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=FakeProbeRunner(),
        runtime_mode="text",
    )

    with pytest.raises(ValueError, match="Invalid split candidate"):
        runner.tune_model(
            "TestModel",
            optimization="speed",
            fixed_context=65536,
            fixed_ngl=40,
            split_candidates=[split],
        )


def test_v2_low_headroom_followup_budget_counts_all_followup_attempts(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    fake_runner = SequencedProbeRunner([True, False, False, False, False, False])

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )

    with patch(
        "app.tweaker.finetune_v2_runner.convergence_status_from_history",
        side_effect=lambda probes, limits, **kwargs: {
            "should_continue": True,
            "reason": "low_headroom_followup" if len(probes) == 1 else "not_started",
            "remaining_followups": 2,
        },
    ):
        result = runner.tune_model(
            "TestModel",
            optimization="speed",
            fixed_context=65536,
            split_candidates=["0.55,0.45"],
        )

    assert len(result.probes) == 2
    assert len(fake_runner.calls) == 2
    assert result.convergence["reason"] == "not_started"


def test_v2_speed_mode_uses_active_context_as_default_floor(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    fake_runner = FakeProbeRunner(free_vram_mib=(1200.0, 1200.0))

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )
    result = runner.tune_model(
        "TestModel",
        optimization="speed",
        fixed_ngl=41,
        split_candidates=["0.55,0.45"],
    )

    assert len(result.probes) == 1
    assert result.winner.candidate.context == 65536
    assert result.convergence["reason"] == "max_context_and_ngl"


def test_v2_fixed_shape_runs_split_rebalance_before_stopping_at_max_shape(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.60,0.40", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.60,0.40",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(579.0, 4917.0),
                total_seconds=33.0,
                order=0,
            ),
            (32768, 38, "0.55,0.45", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.55,0.45",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(940.0, 3600.0),
                total_seconds=34.0,
                order=1,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )
    with patch.object(runner, "_better_split", side_effect=["0.55,0.45", "0.55,0.45"]):
        result = runner.tune_model(
            "TestModel",
            optimization="speed",
            fixed_context=32768,
            fixed_ngl=38,
            split_candidates=["0.60,0.40"],
            split_min=0.30,
            split_max=0.70,
        )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.60,0.40", "0.55,0.45"]
    assert result.winner.candidate.tensor_split == "0.60,0.40"
    assert result.convergence["reason"] == "max_context_and_ngl"
    assert probe_runner.disk_load_calls == [("TestModel", True)]


def test_v2_fixed_shape_critically_low_headroom_refines_neighbor_splits(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    models_path.write_text(models_path.read_text().replace('vision_tensor_split: "0.60,0.40"', 'vision_tensor_split: "0.55,0.45"'))
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.55,0.45", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.55,0.45",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(25.0, 1793.0),
                total_seconds=24.5,
                order=0,
            ),
            (32768, 38, "0.54,0.46", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.54,0.46",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(25.0, 1793.0),
                total_seconds=24.0,
                order=1,
            ),
            (32768, 38, "0.56,0.44", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.56,0.44",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(25.0, 1793.0),
                total_seconds=24.8,
                order=2,
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )

    result = runner.tune_model(
        "TestModel",
        optimization="speed",
        fixed_context=32768,
        fixed_ngl=38,
        split_candidates=["0.55,0.45"],
        split_min=0.30,
        split_max=0.70,
    )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.55,0.45", "0.54,0.46", "0.56,0.44"]
    assert result.winner.candidate.tensor_split == "0.54,0.46"
    assert result.convergence["reason"] == "max_context_and_ngl"
    assert probe_runner.disk_load_calls == [("TestModel", True)]


def test_v2_dry_run_restores_disk_runtime_after_failed_followup_probe(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    probe_runner = CandidateMapProbeRunner(
        {
            (32768, 38, "0.60,0.40", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.60,0.40",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=True,
                free_vram_mib=(579.0, 4917.0),
                total_seconds=33.0,
                order=0,
            ),
            (32768, 38, "0.40,0.60", "vision", True): Probe(
                candidate=Candidate(
                    context=32768,
                    ngl=38,
                    tensor_split="0.40,0.60",
                    runtime_mode="vision",
                    has_mmproj=True,
                ),
                success=False,
                free_vram_mib=(19.0, 5477.0),
                total_seconds=72.0,
                order=1,
                telemetry_source="pre_load",
                error="cuda out of memory",
            ),
        }
    )

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=probe_runner,
        runtime_mode="vision",
    )
    with patch.object(runner, "_better_split", side_effect=["0.40,0.60", "0.40,0.60"]):
        result = runner.tune_model(
            "TestModel",
            optimization="speed",
            fixed_context=32768,
            fixed_ngl=38,
            split_candidates=["0.60,0.40"],
            split_min=0.30,
            split_max=0.70,
        )

    assert [call[1].tensor_split for call in probe_runner.calls] == ["0.60,0.40", "0.40,0.60"]
    assert result.winner.candidate.tensor_split == "0.60,0.40"
    assert result.probes[-1].success is False
    assert probe_runner.disk_load_calls == [("TestModel", True)]


def test_v2_apply_failure_restores_yaml_and_reloads_previous_config(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_models(models_path)
    before = models_path.read_text()
    fake_runner = SequencedProbeRunner([True, False])

    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="text",
    )

    with pytest.raises(RuntimeError, match="failed to reload"):
        runner.tune_model(
            "TestModel",
            optimization="speed",
            fixed_context=65536,
            fixed_ngl=40,
            split_candidates=["0.55,0.45"],
            apply=True,
        )

    assert models_path.read_text() == before
    assert len(fake_runner.disk_load_calls) == 2


def test_v2_vision_mode_requires_configured_vision_runtime(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    results_path = tmp_path / "v2_results.json"
    _write_text_only_models(models_path)
    fake_runner = FakeProbeRunner()
    runner = FinetuneV2Runner(
        models_config_path=models_path,
        results_file=results_path,
        probe_runner=fake_runner,
        runtime_mode="vision",
    )

    with patch.object(runner, "_runtime_limits") as mock_runtime_limits:
        with pytest.raises(ValueError, match="does not have a configured vision runtime"):
            runner.tune_model("TestModel", optimization="context", fixed_context=65536, fixed_ngl=40)
        mock_runtime_limits.assert_not_called()


@pytest.mark.parametrize(
    "split_args",
    [("--split-min", "0"), ("--split-max", "1"), ("--split-min", "0.8", "--split-max", "0.7")],
)
def test_finetune_v2_cli_rejects_invalid_split_range(split_args: tuple[str, ...]):
    command = [
        sys.executable,
        "finetune_v2.py",
        "TestModel",
        *split_args,
    ]

    result = subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "0 < split_min <= split_max < 1" in result.stderr


def test_finetune_v2_root_cli_without_args_prints_help_and_models():
    command = [sys.executable, "finetune_v2.py"]

    result = subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "--optimization" in result.stdout
    assert "Available models:" in result.stdout
    assert "Aliases:" in result.stdout


def test_finetune_v2_compat_script_without_args_prints_help_and_models():
    command = [sys.executable, "scripts/finetune_v2_model_config.py"]

    result = subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "Available models:" in result.stdout


def test_guardian_v2_probe_runner_uses_admin_load_runtime_overrides():
    load_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/load":
            load_payloads.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "loaded"})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "FIT OK"}}]})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runner = GuardianV2ProbeRunner(
            guardian_url="http://guardian.test",
            api_key="test-key",
            smoke_prompt="FIT?",
            smoke_max_tokens=4,
            smoke_image_url=None,
            client=client,
        )
        candidate = Candidate(context=65536, ngl=40, tensor_split="0.55,0.45")

        with patch(
            "app.tweaker.finetune_v2_runner.read_gpu_vram_snapshot",
            return_value={
                "0": {"free": 300.0, "total": 12000.0, "free_pct": 2.5},
                "1": {"free": 280.0, "total": 16000.0, "free_pct": 1.75},
            },
        ):
            probe = runner.probe("TestModel", candidate)

        assert probe.success is True
        assert probe.free_vram_mib == (300.0, 280.0)
        assert probe.gpu_vram == {
            "0": {"free": 300.0, "total": 12000.0, "free_pct": 2.5},
            "1": {"free": 280.0, "total": 16000.0, "free_pct": 1.75},
        }
        assert load_payloads == [
            {
                "model": "TestModel",
                "enable_vision": False,
                "runtime_overrides": {
                    "context": 65536,
                    "ngl": 40,
                    "tensor_split": "0.55,0.45",
                },
            }
        ]


def test_guardian_v2_probe_runner_verify_disk_load_sends_no_overrides():
    load_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/load":
            load_payloads.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "loaded"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runner = GuardianV2ProbeRunner(
            guardian_url="http://guardian.test",
            api_key="test-key",
            smoke_prompt="FIT?",
            smoke_max_tokens=4,
            smoke_image_url=None,
            client=client,
        )

        result = runner.verify_disk_load("TestModel", enable_vision=True)

    assert result is True
    assert load_payloads == [{"model": "TestModel", "enable_vision": True}]


def test_guardian_v2_probe_runner_verify_disk_load_closes_response():
    response = MagicMock(status_code=200)
    client = MagicMock()
    client.post.return_value = response
    runner = GuardianV2ProbeRunner(
        guardian_url="http://guardian.test",
        api_key="test-key",
        smoke_prompt="FIT?",
        smoke_max_tokens=4,
        smoke_image_url=None,
        client=client,
    )

    result = runner.verify_disk_load("TestModel", enable_vision=False)

    assert result is True
    response.close.assert_called_once()


def test_guardian_v2_probe_runner_probe_closes_load_and_smoke_responses():
    load_response = MagicMock(status_code=200)
    smoke_response = MagicMock(status_code=200)
    client = MagicMock()
    client.post.side_effect = [load_response, smoke_response]
    runner = GuardianV2ProbeRunner(
        guardian_url="http://guardian.test",
        api_key="test-key",
        smoke_prompt="FIT?",
        smoke_max_tokens=4,
        smoke_image_url=None,
        client=client,
    )
    candidate = Candidate(context=65536, ngl=40, tensor_split="0.55,0.45")

    with patch(
        "app.tweaker.finetune_v2_runner.read_gpu_vram_snapshot",
        return_value={
            "0": {"free": 300.0, "total": 12000.0, "free_pct": 2.5},
            "1": {"free": 280.0, "total": 16000.0, "free_pct": 1.75},
        },
    ):
        probe = runner.probe("TestModel", candidate)

    assert probe.success is True
    load_response.close.assert_called_once()
    smoke_response.close.assert_called_once()


def test_guardian_v2_probe_runner_requires_image_for_vision_smoke():
    load_payloads = []
    chat_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/load":
            load_payloads.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "loaded"})
        if request.url.path == "/v1/chat/completions":
            chat_payloads.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"choices": [{"message": {"content": "FIT OK"}}]})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runner = GuardianV2ProbeRunner(
            guardian_url="http://guardian.test",
            api_key="test-key",
            smoke_prompt="FIT?",
            smoke_max_tokens=4,
            smoke_image_url=None,
            client=client,
        )
        candidate = Candidate(
            context=32768,
            ngl=38,
            tensor_split="0.60,0.40",
            runtime_mode="vision",
            has_mmproj=True,
        )

        with patch(
            "app.tweaker.finetune_v2_runner.read_gpu_vram_snapshot",
            return_value={
                "0": {"free": 300.0, "total": 12000.0, "free_pct": 2.5},
                "1": {"free": 280.0, "total": 16000.0, "free_pct": 1.75},
            },
        ):
            probe = runner.probe("TestModel", candidate)

    assert probe.success is False
    assert probe.telemetry_source == "post_load"
    assert probe.error == "vision finetune requires smoke_image_url to exercise the multimodal path"
    assert chat_payloads == []
    assert load_payloads == [
        {
            "model": "TestModel",
            "enable_vision": True,
            "runtime_overrides": {
                "context": 32768,
                "ngl": 38,
                "tensor_split": "0.60,0.40",
            },
        }
    ]
