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
    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "completed"
    assert history[-1]["version"] == 2
    assert history[-1]["probes"][0]["candidate"]["context"] == 65536
    assert history[-1]["winner_explanation"]["winner_reason"]["code"] == "context_lexicographic_winner"


def test_v2_results_log_persists_active_run_incrementally(tmp_path: Path):
    results_path = tmp_path / "v2_results.json"
    log = FinetuneV2ResultsLog(results_path)
    candidate = Candidate(context=65536, ngl=40, tensor_split="0.55,0.45")
    probe = Probe(candidate=candidate, success=True, free_vram_mib=(300.0, 280.0), total_seconds=1.0, order=0)

    log.start_run(model="TestModel", runtime_mode="text", optimization="speed", applied=False)
    history_after_start = results_path.read_text()
    active_path = results_path.with_suffix(f"{results_path.suffix}.active")

    log.append_probe(probe)

    assert results_path.read_text() == history_after_start
    assert active_path.exists()

    log.complete_run(applied=False)

    assert not active_path.exists()
    history = json.loads(results_path.read_text())
    assert history[-1]["status"] == "completed"
    assert history[-1]["probes"][0]["candidate"]["tensor_split"] == "0.55,0.45"


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
        "scripts/finetune_v2_model_config.py",
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

    client = httpx.Client(transport=httpx.MockTransport(handler))
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
