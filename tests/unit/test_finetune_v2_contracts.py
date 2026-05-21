"""Executable contracts derived from docs/FINETUNE_V2_REQUIREMENTS.md."""

import json
from pathlib import Path

import pytest

from app.tweaker.finetune_v2_contracts import (
    Candidate,
    FixtureProbeRunner,
    Probe,
    RuntimeLimits,
    clamp_candidate,
    convergence_status,
    convergence_status_from_history,
    dry_run_preserves_models_yaml,
    initial_seed_candidates,
    next_after_seed_failure,
    rank_successes,
    split_rebalance_action,
    unique_explicit_ngls,
    upward_ngl_retry_actions,
)


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "finetune_v2_probe_fixtures.json"
OVER_LIMIT_NGL_FOR_CLAMPING_TEST = 99


def load_fixture_runner() -> FixtureProbeRunner:
    return FixtureProbeRunner(json.loads(FIXTURE_PATH.read_text()))


def test_no_probe_candidate_exceeds_total_layers_after_clamping():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)

    clamped = clamp_candidate(Candidate(context=65536, ngl=99, tensor_split="0.55,0.45"), limits)

    assert clamped.ngl == 41
    assert unique_explicit_ngls([99, 42, 41, 17], limits) == [41, 17]


def test_mmproj_overhead_does_not_extend_main_model_ngl_ceiling():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)

    candidate = initial_seed_candidates(
        limits,
        optimization="context",
        seed_split="0.55,0.45",
        runtime_mode="vision",
        has_mmproj=True,
    )[0]

    assert candidate.runtime_mode == "vision"
    assert candidate.has_mmproj is True
    assert candidate.ngl == 41


def test_split_balancing_uses_latest_successful_state_not_failed_probe():
    runner = load_fixture_runner()
    success = runner.probe(Candidate(context=65536, ngl=40, tensor_split="0.55,0.45"))
    failed = runner.probe(Candidate(context=65536, ngl=41, tensor_split="0.55,0.45"))

    action = split_rebalance_action([success, failed], better_split="0.60,0.40")

    assert action is not None
    assert action.kind == "split_rebalance"
    assert action.candidate.ngl == 40
    assert action.candidate.tensor_split == "0.60,0.40"


def test_failed_seed_probe_steps_ngl_down_without_split_fanout():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)
    failed = Probe(
        candidate=Candidate(context=65536, ngl=41, tensor_split="0.55,0.45"),
        success=False,
        order=0,
    )

    assert split_rebalance_action([failed], better_split="0.60,0.40") is None
    action = next_after_seed_failure([failed], limits)

    assert action is not None
    assert action.kind == "seed_ngl_step_down"
    assert action.candidate.ngl == 40
    assert action.candidate.tensor_split == "0.55,0.45"


def test_successful_rebalance_triggers_upward_ngl_retries_around_better_split():
    limits = RuntimeLimits(total_layers=42, max_context=131072, active_context=65536)
    runner = load_fixture_runner()
    rebalance = runner.probe(Candidate(context=65536, ngl=40, tensor_split="0.60,0.40"))

    actions = upward_ngl_retry_actions(rebalance, limits, max_retries=2)

    assert [action.candidate.ngl for action in actions] == [41, 42]
    assert {action.candidate.tensor_split for action in actions} == {"0.60,0.40"}


def test_context_ranking_never_lets_split_balance_override_context_or_ngl():
    higher_context = Probe(
        candidate=Candidate(context=131072, ngl=39, tensor_split="0.65,0.35"),
        success=True,
        free_vram_mib=(900, 100),
        total_seconds=10.0,
        order=0,
    )
    prettier_split = Probe(
        candidate=Candidate(context=65536, ngl=41, tensor_split="0.50,0.50"),
        success=True,
        free_vram_mib=(500, 500),
        total_seconds=1.0,
        order=1,
    )
    same_context_higher_ngl = Probe(
        candidate=Candidate(context=131072, ngl=40, tensor_split="0.70,0.30"),
        success=True,
        free_vram_mib=(50, 600),
        total_seconds=12.0,
        order=2,
    )

    winner, explanation = rank_successes(
        [higher_context, prettier_split, same_context_higher_ngl],
        optimization="context",
    )

    assert winner is same_context_higher_ngl
    assert explanation["winner_reason"]["code"] == "context_lexicographic_winner"


def test_dry_run_failure_leaves_models_yaml_byte_identical(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    models_path.write_bytes(b"models:\n  Test:\n    ngl: 41\n")

    def failing_dry_run() -> None:
        raise RuntimeError("probe failed")

    with pytest.raises(RuntimeError):
        dry_run_preserves_models_yaml(models_path, failing_dry_run)

    assert models_path.read_bytes() == b"models:\n  Test:\n    ngl: 41\n"


def test_dry_run_partial_write_failure_is_reported(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    models_path.write_bytes(b"models:\n  Test:\n    ngl: 41\n")

    def corrupting_dry_run() -> None:
        models_path.write_bytes(b"truncated")
        raise RuntimeError("probe failed after partial write")

    with pytest.raises(AssertionError, match="changed models.yaml bytes"):
        dry_run_preserves_models_yaml(models_path, corrupting_dry_run)


def test_dry_run_success_without_writes_is_allowed(tmp_path: Path):
    models_path = tmp_path / "models.yaml"
    models_path.write_bytes(b"models:\n  Test:\n    ngl: 41\n")

    dry_run_preserves_models_yaml(models_path, lambda: "validated")

    assert models_path.read_bytes() == b"models:\n  Test:\n    ngl: 41\n"


def test_convergence_uses_current_best_and_low_headroom_budget():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)
    best = Probe(
        candidate=Candidate(context=65536, ngl=40, tensor_split="0.60,0.40"),
        success=True,
        free_vram_mib=(700, 650),
        order=3,
    )

    assert convergence_status(best, limits, low_headroom_followups_used=0) == {
        "should_continue": True,
        "reason": "low_headroom_followup",
        "remaining_followups": 5,
    }
    assert convergence_status(best, limits, low_headroom_followups_used=5) == {
        "should_continue": False,
        "reason": "low_headroom_budget_exhausted",
    }


def test_convergence_from_history_ignores_failed_and_lower_ranked_latest_probe():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)
    best = Probe(
        candidate=Candidate(context=131072, ngl=41, tensor_split="0.55,0.45"),
        success=True,
        free_vram_mib=(900, 850),
        order=0,
    )
    failed_later = Probe(
        candidate=Candidate(context=131072, ngl=42, tensor_split="0.55,0.45"),
        success=False,
        free_vram_mib=(0, 0),
        order=1,
    )
    lower_ranked_later = Probe(
        candidate=Candidate(context=65536, ngl=41, tensor_split="0.60,0.40"),
        success=True,
        free_vram_mib=(100, 100),
        order=2,
    )

    status = convergence_status_from_history(
        [best, failed_later, lower_ranked_later],
        limits,
        optimization="context",
    )

    assert status == {
        "should_continue": False,
        "reason": "max_context_and_ngl",
        "best_order": 0,
        "best_context": 131072,
        "best_ngl": 41,
    }


def test_convergence_completes_only_below_500_or_at_max_shape():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)
    below_500 = Probe(
        candidate=Candidate(context=65536, ngl=40, tensor_split="0.60,0.40"),
        success=True,
        free_vram_mib=(499, 320),
    )
    max_shape = Probe(
        candidate=Candidate(context=131072, ngl=41, tensor_split="0.55,0.45"),
        success=True,
        free_vram_mib=(2000, 1800),
    )
    not_done = Probe(
        candidate=Candidate(context=65536, ngl=40, tensor_split="0.55,0.45"),
        success=True,
        free_vram_mib=(900, 320),
    )

    assert convergence_status(below_500, limits)["reason"] == "both_gpus_below_500_mib"
    assert convergence_status(max_shape, limits)["reason"] == "max_context_and_ngl"
    assert convergence_status(not_done, limits) == {
        "should_continue": True,
        "reason": "search_not_converged",
    }


def test_convergence_uses_fixed_shape_limits_when_present():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)
    fixed_shape = Probe(
        candidate=Candidate(context=32768, ngl=30, tensor_split="0.55,0.45"),
        success=True,
        free_vram_mib=(1400, 1300),
    )

    assert convergence_status(
        fixed_shape,
        limits,
        allowed_context=32768,
        allowed_ngl=30,
    ) == {
        "should_continue": False,
        "reason": "max_context_and_ngl",
    }


def test_speed_mode_history_keeps_context_floor_when_picking_best_success():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)
    below_floor_higher_ngl = Probe(
        candidate=Candidate(context=32768, ngl=41, tensor_split="0.55,0.45"),
        success=True,
        free_vram_mib=(1200, 1150),
        order=0,
    )
    floor_meeting_best = Probe(
        candidate=Candidate(context=65536, ngl=40, tensor_split="0.60,0.40"),
        success=True,
        free_vram_mib=(1400, 1300),
        order=1,
    )

    status = convergence_status_from_history(
        [below_floor_higher_ngl, floor_meeting_best],
        limits,
        optimization="speed",
        context_floor=65536,
        allowed_context=65536,
        allowed_ngl=40,
    )

    assert status == {
        "should_continue": False,
        "reason": "max_context_and_ngl",
        "best_order": 1,
        "best_context": 65536,
        "best_ngl": 40,
    }


def test_text_and_vision_probe_results_are_never_ranked_together():
    runner = load_fixture_runner()
    text_probe = runner.probe(Candidate(context=65536, ngl=40, tensor_split="0.55,0.45"))
    vision_probe = runner.probe(
        Candidate(context=65536, ngl=40, tensor_split="0.55,0.45", runtime_mode="vision", has_mmproj=True)
    )

    with pytest.raises(ValueError, match="must not mix text and vision"):
        rank_successes([text_probe, vision_probe], optimization="context")


def test_fixture_probe_rejects_string_free_vram_payload():
    runner = FixtureProbeRunner(
        [
            {
                "runtime_mode": "text",
                "context": 65536,
                "ngl": 40,
                "tensor_split": "0.55,0.45",
                "success": True,
                "free_vram_mib": "1200,1100",
                "total_seconds": 1.0,
            }
        ]
    )

    with pytest.raises(ValueError, match="free_vram_mib must contain two values"):
        runner.probe(Candidate(context=65536, ngl=40, tensor_split="0.55,0.45"))


def test_fixture_probe_key_distinguishes_mmproj_state():
    runner = FixtureProbeRunner(
        [
            {
                "runtime_mode": "vision",
                "context": 65536,
                "ngl": 40,
                "tensor_split": "0.55,0.45",
                "has_mmproj": False,
                "success": True,
                "free_vram_mib": [1400, 1300],
                "total_seconds": 1.0,
            },
            {
                "runtime_mode": "vision",
                "context": 65536,
                "ngl": 40,
                "tensor_split": "0.55,0.45",
                "has_mmproj": True,
                "success": True,
                "free_vram_mib": [900, 800],
                "total_seconds": 1.2,
            },
        ]
    )

    no_mmproj = runner.probe(
        Candidate(context=65536, ngl=40, tensor_split="0.55,0.45", runtime_mode="vision", has_mmproj=False)
    )
    with_mmproj = runner.probe(
        Candidate(context=65536, ngl=40, tensor_split="0.55,0.45", runtime_mode="vision", has_mmproj=True)
    )

    assert no_mmproj.free_vram_mib == (1400.0, 1300.0)
    assert with_mmproj.free_vram_mib == (900.0, 800.0)


def test_final_winner_explanation_is_machine_readable_and_recorded():
    runner = load_fixture_runner()
    text_probe = runner.probe(Candidate(context=65536, ngl=40, tensor_split="0.55,0.45"))
    rebalance_probe = runner.probe(Candidate(context=65536, ngl=40, tensor_split="0.60,0.40"))

    winner, explanation = rank_successes([text_probe, rebalance_probe], optimization="speed", context_floor=65536)

    assert winner is rebalance_probe
    assert explanation["comparator_mode"] == "speed"
    assert explanation["runtime_mode"] == "text"
    assert explanation["winner"] == {"context": 65536, "ngl": 40, "tensor_split": "0.60,0.40"}
    assert explanation["losing_reasons"][0]["code"] == "lower_comparator_key"


def test_context_mode_starts_at_highest_allowed_ngl_and_max_context():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)

    seeds = initial_seed_candidates(limits, optimization="context", seed_split="0.55,0.45")

    assert seeds == [Candidate(context=131072, ngl=41, tensor_split="0.55,0.45")]


def test_speed_mode_keeps_active_or_fixed_context_and_searches_highest_ngl():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)

    active_seed = initial_seed_candidates(limits, optimization="speed", seed_split="0.55,0.45")[0]
    fixed_seed = initial_seed_candidates(
        limits,
        optimization="speed",
        seed_split="0.55,0.45",
        fixed_context=32768,
    )[0]

    assert active_seed == Candidate(context=65536, ngl=41, tensor_split="0.55,0.45")
    assert fixed_seed == Candidate(context=32768, ngl=41, tensor_split="0.55,0.45")


def test_fixed_context_and_fixed_ngl_pin_those_dimensions():
    limits = RuntimeLimits(total_layers=41, max_context=131072, active_context=65536)

    seed = initial_seed_candidates(
        limits,
        optimization="context",
        seed_split="0.55,0.45",
        fixed_context=32768,
        fixed_ngl=OVER_LIMIT_NGL_FOR_CLAMPING_TEST,
    )[0]

    assert seed == Candidate(context=32768, ngl=41, tensor_split="0.55,0.45")


def test_mixed_mode_probes_rejected_even_when_one_mode_has_only_failures():
    text_success = Probe(
        candidate=Candidate(context=65536, ngl=40, tensor_split="0.55,0.45", runtime_mode="text"),
        success=True,
        free_vram_mib=(900, 850),
        order=0,
    )
    vision_failure = Probe(
        candidate=Candidate(context=65536, ngl=40, tensor_split="0.55,0.45", runtime_mode="vision", has_mmproj=True),
        success=False,
        free_vram_mib=(0, 0),
        order=1,
    )

    with pytest.raises(ValueError, match="must not mix text and vision"):
        rank_successes([text_success, vision_failure], optimization="context")


def test_fixture_probe_runner_rejects_duplicate_keys():
    duplicate_rows = [
        {
            "runtime_mode": "text",
            "context": 65536,
            "ngl": 40,
            "tensor_split": "0.55,0.45",
            "success": True,
            "free_vram_mib": [900, 620],
            "total_seconds": 4.1,
        },
        {
            "runtime_mode": "text",
            "context": 65536,
            "ngl": 40,
            "tensor_split": "0.55,0.45",
            "success": False,
            "free_vram_mib": [0, 0],
            "total_seconds": 2.0,
        },
    ]

    with pytest.raises(ValueError, match="duplicate finetune v2 fixture key"):
        FixtureProbeRunner(duplicate_rows)
