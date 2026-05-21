"""Tests for app.tweaker.model_finetune."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from app.tweaker.model_finetune import (
    balance_metric,
    balanced_tradeoff_score,
    free_vram_delta_pct,
    detect_oom_gpu,
    GuardianModelFinetuner,
    next_split_from_vram_balance,
    smaller_split_step,
    apply_runtime_search_values,
    align_context_ceil,
    align_context_floor,
    binary_search_max_success,
    binary_search_max_int_success,
    build_ngl_candidates,
    build_model_signature,
    build_probe_cache_key,
    build_smoke_messages,
    build_smoke_signature,
    build_split_candidates,
    choose_better_result,
    ProbeResult,
    TuneResult,
    resolve_context_bounds,
    resolve_candidate_context_bounds,
    format_two_gpu_split,
    index_cached_probes,
    parse_two_gpu_split,
    render_model_block,
    replace_model_block,
    resolve_headroom_context_granularity,
    resolve_optimization_mode,
    resolve_runtime_config_value,
    resolve_runtime_mode,
    should_skip_coarse_split_shift,
    should_limit_large_context_jumps,
    split_candidates_for_distance,
    split_balance_distance,
    target_gpu_free_mib_for_balance_shift,
    two_gpu_free_mib,
    unique_attempt_ngls,
    unique_attempt_splits,
)


def _make_finetuner(tmp_path: Path) -> GuardianModelFinetuner:
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        """\
models:
  TestModel:
    path: /tmp/test-model.gguf
    context: 262144
    ngl: 36
    tensor_split: \"0.55,0.45\"
"""
    )
    results_path = tmp_path / "results.json"
    with patch.object(GuardianModelFinetuner, "_get_current_model", return_value=None):
        return GuardianModelFinetuner(
            guardian_url="http://127.0.0.1:11434",
            api_key="test-key",
            models_config_path=str(models_path),
            results_file=str(results_path),
            runtime_mode="text",
        )


class TestContextAlignment:
    def test_align_floor(self):
        assert align_context_floor(196700, 2048) == 196608

    def test_align_ceil(self):
        assert align_context_ceil(196700, 2048) == 198656


class TestTensorSplitHelpers:
    def test_parse_and_format_round_trip(self):
        ratio = parse_two_gpu_split("0.62,0.38")
        assert ratio == 0.62
        assert format_two_gpu_split(ratio) == "0.62,0.38"

    def test_build_split_candidates_prefers_balanced_splits(self):
        candidates = build_split_candidates("0.55,0.45", 0.05, 0.45, 0.65)
        assert candidates[0] == "0.50,0.50"
        assert candidates[1] == "0.55,0.45"
        assert "0.50,0.50" in candidates
        assert "0.60,0.40" in candidates

    def test_build_split_candidates_can_include_auto(self):
        candidates = build_split_candidates(None, 0.05, 0.45, 0.55, include_auto=True)
        assert candidates[-1] is None
        assert "0.55,0.45" in candidates

    def test_split_balance_distance_prefers_balanced_values(self):
        assert split_balance_distance("0.50,0.50") < split_balance_distance("0.60,0.40")

    def test_split_candidates_for_distance_prefers_anchor_side_first(self):
        candidates = split_candidates_for_distance(
            0.05,
            min_primary=0.30,
            max_primary=0.70,
            anchor_split="0.55,0.45",
        )
        assert candidates == ["0.55,0.45", "0.45,0.55"]

    def test_detect_oom_gpu_parses_cuda_device_number(self):
        error = "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory"
        assert detect_oom_gpu(error) == 1

    def test_next_split_from_vram_balance_moves_toward_fuller_gpu(self):
        gpu_vram = {
            "0": {"free_pct": 20.0},
            "1": {"free_pct": 10.0},
        }
        assert next_split_from_vram_balance(
            "0.50,0.50",
            gpu_vram=gpu_vram,
            step=0.05,
            split_min=0.30,
            split_max=0.70,
        ) == "0.55,0.45"

    def test_free_vram_delta_pct_uses_first_two_gpus(self):
        gpu_vram = {
            "0": {"free_pct": 21.0},
            "1": {"free_pct": 13.5},
        }
        assert free_vram_delta_pct(gpu_vram) == 7.5

    def test_smaller_split_step_halves_down_to_one_percent(self):
        assert smaller_split_step(0.05) == 0.03
        assert smaller_split_step(0.02) == 0.01
        assert smaller_split_step(0.01) is None


class TestProbeCapture:
    def test_probe_candidate_captures_vram_on_load_failure(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        finetuner._active_model_signature = build_model_signature(
            "TestModel",
            {"path": "/tmp/test-model.gguf", "context": 262144, "ngl": 36, "tensor_split": "0.55,0.45"},
        )
        gpu_vram = {
            "0": {"used": 11880.0, "free": 40.0, "total": 12288.0, "free_pct": 0.33},
            "1": {"used": 16260.0, "free": 51.0, "total": 16311.0, "free_pct": 0.31},
        }
        load_failure = httpx.Response(503, text="load failed")

        with (
            patch.object(finetuner, "_request_with_retry", return_value=load_failure),
            patch("app.tweaker.model_finetune.read_gpu_vram_snapshot", return_value=gpu_vram),
        ):
            result = finetuner._probe_candidate(
                model_name="TestModel",
                model_config={
                    "path": "/tmp/test-model.gguf",
                    "context": 262144,
                    "ngl": 36,
                    "tensor_split": "0.55,0.45",
                },
                context=196608,
                ngl=99,
                tensor_split="0.60,0.40",
            )

        assert result.success is False
        assert result.status_code == 503
        assert result.gpu_vram == gpu_vram
        assert result.gpu_vram_phase == "pre_load"
        assert result.free_vram_delta_pct == free_vram_delta_pct(gpu_vram)
        assert finetuner._attempt_log[-1].gpu_vram == gpu_vram

    def test_target_gpu_free_mib_for_balance_shift_uses_receiver_gpu(self):
        gpu_vram = {
            "0": {"free": 991.0, "total": 12288.0, "free_pct": 8.06},
            "1": {"free": 39.0, "total": 16311.0, "free_pct": 0.24},
        }
        assert target_gpu_free_mib_for_balance_shift(gpu_vram) == 991.0

    def test_should_skip_coarse_split_shift_when_receiver_has_under_one_gib_free(self):
        gpu_vram = {
            "0": {"free": 991.0, "total": 12288.0, "free_pct": 8.06},
            "1": {"free": 39.0, "total": 16311.0, "free_pct": 0.24},
        }
        assert should_skip_coarse_split_shift(gpu_vram, step=0.02) is True
        assert should_skip_coarse_split_shift(gpu_vram, step=0.01) is False

    def test_two_gpu_free_mib_requires_real_mib_scale(self):
        assert two_gpu_free_mib({"0": {"free": 10.0, "total": 100.0}, "1": {"free": 5.0, "total": 100.0}}) is None

    def test_headroom_context_policy_shrinks_when_both_gpus_are_under_five_hundred_mib(self):
        gpu_vram = {
            "0": {"free": 480.0, "total": 12288.0, "free_pct": 3.9},
            "1": {"free": 320.0, "total": 16311.0, "free_pct": 2.0},
        }
        assert resolve_headroom_context_granularity(gpu_vram, base_granularity=2048) == 1024
        assert should_limit_large_context_jumps(gpu_vram) is True

    def test_headroom_context_policy_shrinks_more_when_one_gpu_is_under_one_hundred_mib(self):
        gpu_vram = {
            "0": {"free": 991.0, "total": 12288.0, "free_pct": 8.06},
            "1": {"free": 39.0, "total": 16311.0, "free_pct": 0.24},
        }
        assert resolve_headroom_context_granularity(gpu_vram, base_granularity=2048) == 512
        assert should_limit_large_context_jumps(gpu_vram) is True


class TestNglHelpers:
    def test_build_ngl_candidates_prefers_higher_values(self):
        candidates = build_ngl_candidates(36, 16, 36, 99)
        assert candidates == [99, 84, 68, 52, 36]

    def test_binary_search_max_int_success_halves_to_highest_fit(self):
        result, attempts = binary_search_max_int_success(
            min_value=36,
            max_value=99,
            anchor_value=99,
            probe=lambda ngl: ngl <= 52,
        )
        assert result == 52
        assert attempts[0] == 99
        assert len(attempts) <= 8


class TestAutoContextBounds:
    def test_resolve_context_bounds_auto_mode_uses_half_of_current_context(self):
        lower, upper = resolve_context_bounds(
            original_context=262144,
            benchmark_context_limit=262144,
            min_context=None,
            max_context=None,
            granularity=2048,
            auto_context_range=True,
            auto_context_floor_ratio=0.5,
        )
        assert lower == 131072
        assert upper == 262144

    def test_resolve_candidate_context_bounds_skips_lower_contexts_after_best(self):
        lower, upper = resolve_candidate_context_bounds(
            best_context=262144,
            lower_bound=131072,
            upper_bound=262144,
            granularity=2048,
        )
        assert lower == 262144
        assert upper == 262144

    def test_resolve_candidate_context_bounds_only_searches_above_current_best(self):
        lower, upper = resolve_candidate_context_bounds(
            best_context=196608,
            lower_bound=131072,
            upper_bound=262144,
            granularity=2048,
        )
        assert lower == 198656
        assert upper == 262144


class TestSmokeMessages:
    def test_build_text_smoke_messages(self):
        messages = build_smoke_messages("Reply with exactly: FIT OK")
        assert messages == [{"role": "user", "content": "Reply with exactly: FIT OK"}]

    def test_build_multimodal_smoke_messages(self):
        messages = build_smoke_messages("Reply with exactly: FIT OK", "https://example.com/test.png")
        assert messages[0]["role"] == "user"
        content = messages[0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"] == "https://example.com/test.png"
        assert content[1]["text"] == "Reply with exactly: FIT OK"


class TestPersistentProbeCache:
    def test_model_signature_ignores_tuned_fields(self):
        base = {
            "path": "/tmp/model.gguf",
            "ngl": 36,
            "extra_args": "--spec-type draft-mtp",
            "context": 131072,
            "tensor_split": "0.55,0.45",
            "vision_context": 65536,
            "vision_ngl": 28,
            "vision_tensor_split": "0.60,0.40",
        }
        variant = {
            **base,
            "context": 196608,
            "ngl": 52,
            "tensor_split": "0.60,0.40",
            "vision_context": 81920,
            "vision_ngl": 36,
            "vision_tensor_split": "0.55,0.45",
        }
        assert build_model_signature("TestModel", base) == build_model_signature("TestModel", variant)

    def test_index_cached_probes_filters_by_model_and_smoke_signature(self):
        model_signature = build_model_signature("TestModel", {"path": "/tmp/model.gguf", "ngl": 36})
        smoke_signature = build_smoke_signature("Reply with exactly: FIT OK", 8, "https://example.com/test.png", "vision")
        history = [
            {
                "model": "TestModel",
                "model_signature": model_signature,
                "smoke_signature": smoke_signature,
                "attempts": [
                    {
                        "model": "TestModel",
                        "context": 196608,
                        "ngl": 52,
                        "tensor_split": "0.55,0.45",
                        "success": True,
                        "load_seconds": 10.0,
                        "smoke_seconds": 1.0,
                        "status_code": 200,
                        "gpu_vram": {
                            "0": {"free": 480.0, "total": 12288.0, "free_pct": 3.9},
                            "1": {"free": 320.0, "total": 16311.0, "free_pct": 2.0},
                        },
                    }
                ],
            },
            {
                "model": "TestModel",
                "model_signature": model_signature,
                "smoke_signature": "other-smoke",
                "attempts": [
                    {
                        "model": "TestModel",
                        "context": 229376,
                        "ngl": 60,
                        "tensor_split": "0.60,0.40",
                        "success": True,
                        "load_seconds": 11.0,
                        "smoke_seconds": 1.1,
                        "status_code": 200,
                    }
                ],
            },
        ]
        indexed = index_cached_probes(
            history,
            model_name="TestModel",
            model_signature=model_signature,
            smoke_signature=smoke_signature,
        )
        key = build_probe_cache_key("TestModel", 196608, 52, "0.55,0.45", model_signature, smoke_signature)
        assert key in indexed
        assert indexed[key].cached is True
        assert indexed[key].success is True
        assert indexed[key].gpu_vram == {
            "0": {"free": 480.0, "total": 12288.0, "free_pct": 3.9},
            "1": {"free": 320.0, "total": 16311.0, "free_pct": 2.0},
        }
        assert indexed[key].free_vram_delta_pct == 1.9
        assert len(indexed) == 1

    def test_index_cached_probes_reuses_legacy_runtime_mode_history(self):
        model_signature = build_model_signature("TestModel", {"path": "/tmp/model.gguf", "ngl": 36})
        current_smoke_signature = build_smoke_signature(
            "Reply with exactly: SPEED_OK_55_53_54_V2",
            8,
            "https://example.com/test.png",
            "vision",
        )
        history = [
            {
                "model": "TestModel",
                "model_signature": model_signature,
                "smoke_signature": "legacy-exact-prompt-hash",
                "runtime_mode": "vision",
                "attempts": [
                    {
                        "model": "TestModel",
                        "context": 188416,
                        "ngl": 99,
                        "tensor_split": "0.60,0.40",
                        "success": True,
                        "load_seconds": 10.0,
                        "smoke_seconds": 1.0,
                        "status_code": 200,
                    }
                ],
            }
        ]

        indexed = index_cached_probes(
            history,
            model_name="TestModel",
            model_signature=model_signature,
            smoke_signature=current_smoke_signature,
            runtime_mode="vision",
        )

        key = build_probe_cache_key(
            "TestModel",
            188416,
            99,
            "0.60,0.40",
            model_signature,
            current_smoke_signature,
        )
        assert key in indexed
        assert indexed[key].cached is True

    def test_index_cached_probes_keeps_richer_vram_telemetry_from_older_live_probe(self):
        model_signature = build_model_signature("TestModel", {"path": "/tmp/model.gguf", "ngl": 36})
        smoke_signature = build_smoke_signature(
            "Reply with exactly: SPEED_OK_CACHE_TELEMETRY_CHECK",
            8,
            "https://example.com/test.png",
            "vision",
        )
        history = [
            {
                "model": "TestModel",
                "model_signature": model_signature,
                "smoke_signature": "older-live-smoke",
                "runtime_mode": "vision",
                "attempts": [
                    {
                        "model": "TestModel",
                        "context": 186368,
                        "ngl": 99,
                        "tensor_split": "0.60,0.40",
                        "success": True,
                        "load_seconds": 19.8,
                        "smoke_seconds": 1.8,
                        "status_code": 200,
                        "response_excerpt": "SPEED_OK_55_5",
                        "gpu_vram": {
                            "0": {"free": 969.0, "total": 12288.0, "free_pct": 7.88},
                            "1": {"free": 13.0, "total": 16311.0, "free_pct": 0.08},
                        },
                        "gpu_vram_phase": "pre_load",
                        "free_vram_delta_pct": 7.8,
                    }
                ],
            },
            {
                "model": "TestModel",
                "model_signature": model_signature,
                "smoke_signature": smoke_signature,
                "attempts": [
                    {
                        "model": "TestModel",
                        "context": 186368,
                        "ngl": 99,
                        "tensor_split": "0.60,0.40",
                        "success": True,
                        "load_seconds": 19.86407330899965,
                        "smoke_seconds": 1.8133850639860611,
                        "status_code": 200,
                        "response_excerpt": "SPEED_OK_55_5",
                        "gpu_vram": None,
                        "free_vram_delta_pct": None,
                    }
                ],
            },
        ]

        indexed = index_cached_probes(
            history,
            model_name="TestModel",
            model_signature=model_signature,
            smoke_signature=smoke_signature,
            runtime_mode="vision",
        )

        key = build_probe_cache_key(
            "TestModel",
            186368,
            99,
            "0.60,0.40",
            model_signature,
            smoke_signature,
        )
        assert key in indexed
        assert indexed[key].cached is True
        assert indexed[key].success is True
        assert indexed[key].gpu_vram == {
            "0": {"free": 969.0, "total": 12288.0, "free_pct": 7.88},
            "1": {"free": 13.0, "total": 16311.0, "free_pct": 0.08},
        }
        assert indexed[key].gpu_vram_phase == "pre_load"
        assert indexed[key].free_vram_delta_pct == 7.8

    def test_build_smoke_signature_ignores_short_marker_text_changes(self):
        first = build_smoke_signature(
            "Reply with exactly: SPEED_OK_55_53_54",
            8,
            "https://example.com/test.png",
            "vision",
        )
        second = build_smoke_signature(
            "Reply with exactly: SPEED_OK_55_53_54_V2",
            8,
            "https://example.com/test.png",
            "vision",
        )
        assert first == second

    def test_live_result_log_updates_per_probe(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        finetuner._active_model_signature = "model-sig"
        finetuner._start_live_result_log(
            model="TestModel",
            original_context=262144,
            original_ngl=36,
            original_tensor_split="0.55,0.45",
            search_min_context=131072,
            search_max_context=262144,
            benchmark_context_limit=262144,
            coarse_ngl_candidates=[99, 84, 68, 52, 36],
            refined_ngl_candidates=[],
            coarse_candidates=["0.50,0.50", "0.55,0.45"],
            refined_candidates=[],
            applied=False,
            runtime_mode="text",
        )

        probe = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=36,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=1.0,
            smoke_seconds=0.5,
            status_code=200,
            model_signature="model-sig",
            smoke_signature=finetuner._active_smoke_signature,
        )
        key = build_probe_cache_key(
            "TestModel",
            262144,
            36,
            "0.55,0.45",
            "model-sig",
            finetuner._active_smoke_signature,
        )

        finetuner._record_attempt(key, probe)

        payload = json.loads(finetuner.results_file.read_text())
        assert payload[-1]["status"] == "running"
        assert len(payload[-1]["attempts"]) == 1
        assert payload[-1]["attempts"][0]["context"] == 262144
        assert payload[-1]["coarse_ngl_candidates"] == [36]
        assert payload[-1]["coarse_candidates"] == ["0.55,0.45"]

        result = TuneResult(
            model="TestModel",
            original_context=262144,
            original_ngl=36,
            original_tensor_split="0.55,0.45",
            runtime_mode="text",
            search_min_context=131072,
            search_max_context=262144,
            recommended_context=262144,
            recommended_ngl=36,
            recommended_tensor_split="0.55,0.45",
            benchmark_context_limit=262144,
            attempts=[probe],
        )
        finetuner._append_result_log(result)

        payload = json.loads(finetuner.results_file.read_text())
        assert payload[-1]["status"] == "completed"
        assert payload[-1]["recommended_context"] == 262144
        assert payload[-1]["attempts"][0]["ngl"] == 36
        finetuner.close()

    def test_unique_attempt_helpers_preserve_first_seen_order(self):
        attempts = [
            ProbeResult("TestModel", 262144, 99, "0.50,0.50", False, 1.0),
            ProbeResult("TestModel", 131072, 99, "0.50,0.50", False, 1.0),
            ProbeResult("TestModel", 262144, 84, "0.55,0.45", False, 1.0),
        ]

        assert unique_attempt_ngls(attempts) == [99, 84]
        assert unique_attempt_splits(attempts) == ["0.50,0.50", "0.55,0.45"]

    def test_auto_search_rebalances_split_on_baseline_then_context(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (16384, 52, "0.50,0.50"),
            (16384, 52, "0.55,0.45"),
            (262144, 52, "0.55,0.45"),
        }

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.50,0.50":
                return {
                    "0": {"used": 80.0, "free": 20.0, "total": 100.0, "free_pct": 20.0},
                    "1": {"used": 90.0, "free": 10.0, "total": 100.0, "free_pct": 10.0},
                }
            return {
                "0": {"used": 83.0, "free": 17.0, "total": 100.0, "free_pct": 17.0},
                "1": {"used": 20.0, "free": 15.0, "total": 100.0, "free_pct": 15.0},
            }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory",
                gpu_vram=build_vram(tensor_split or "0.50,0.50") if success else None,
                free_vram_delta_pct=free_vram_delta_pct(build_vram(tensor_split or "0.50,0.50")) if success else None,
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._search_best_auto_combination(
                model_name="TestModel",
                model_config={},
                lower_bound=131072,
                upper_bound=262144,
                granularity=2048,
                original_ngl=36,
                lower_ngl=36,
                upper_ngl=52,
                ngl_refine_step=8,
                coarse_step=0.05,
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
                original_tensor_split="0.50,0.50",
                optimization="context",
            )

        assert result is not None
        assert result.context == 262144
        assert result.ngl == 52
        assert result.tensor_split == "0.55,0.45"
        assert observed == [
            (16384, 52, "0.50,0.50"),
            (16384, 52, "0.55,0.45"),
            (262144, 52, "0.55,0.45"),
        ]

    def test_find_best_context_for_combination_returns_successful_probe(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            observed.append((context, ngl, tensor_split))
            return ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=context <= 262144,
                load_seconds=1.0,
                smoke_seconds=0.1,
                status_code=200,
            )

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._find_best_context_for_combination(
                model_name="TestModel",
                model_config={},
                ngl=36,
                tensor_split="0.55,0.45",
                min_context=131072,
                max_context=262144,
                granularity=2048,
                anchor_context=262144,
            )

        assert result is not None
        assert result.context == 262144
        assert result.ngl == 36
        assert result.tensor_split == "0.55,0.45"

    def test_speed_mode_rebalances_only_after_winning_context(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (16384, 52, "0.55,0.45"),
            (16384, 52, "0.60,0.40"),
            (131072, 52, "0.60,0.40"),
            (131072, 52, "0.62,0.38"),
        }

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.55,0.45":
                return {
                    "0": {"used": 70.0, "free": 30.0, "total": 100.0, "free_pct": 30.0},
                    "1": {"used": 85.0, "free": 15.0, "total": 100.0, "free_pct": 15.0},
                }
            if split == "0.60,0.40":
                return {
                    "0": {"used": 74.0, "free": 26.0, "total": 100.0, "free_pct": 26.0},
                    "1": {"used": 82.0, "free": 18.0, "total": 100.0, "free_pct": 18.0},
                }
            return {
                "0": {"used": 80.0, "free": 20.0, "total": 100.0, "free_pct": 20.0},
                "1": {"used": 82.0, "free": 18.0, "total": 100.0, "free_pct": 18.0},
            }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory",
                gpu_vram=build_vram(tensor_split or "0.55,0.45") if success else None,
                free_vram_delta_pct=free_vram_delta_pct(build_vram(tensor_split or "0.55,0.45")) if success else None,
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._search_best_auto_combination(
                model_name="TestModel",
                model_config={},
                lower_bound=131072,
                upper_bound=262144,
                granularity=2048,
                original_ngl=52,
                lower_ngl=52,
                upper_ngl=52,
                ngl_refine_step=8,
                coarse_step=0.05,
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
                original_tensor_split="0.55,0.45",
                optimization="speed",
            )

        assert result is not None
        assert result.context == 131072
        assert result.ngl == 52
        assert result.tensor_split == "0.62,0.38"
        first_high_failure = observed.index((262144, 52, "0.60,0.40"))
        assert observed[first_high_failure + 1] == (131072, 52, "0.60,0.40")
        assert observed.count((262144, 52, "0.60,0.40")) == 1
        assert (262144, 52, "0.62,0.38") not in observed
        assert (196608, 52, "0.62,0.38") not in observed
        assert observed[-1] == (131072, 52, "0.62,0.38")

    def test_speed_mode_returns_highest_successful_context_even_if_balance_is_slightly_worse(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (262144, 99, "0.55,0.45"): False,
            (131072, 99, "0.55,0.45"): True,
            (196608, 99, "0.55,0.45"): False,
            (163840, 99, "0.55,0.45"): True,
            (180224, 99, "0.55,0.45"): False,
            (172032, 99, "0.55,0.45"): False,
            (167936, 99, "0.55,0.45"): True,
            (169984, 99, "0.55,0.45"): True,
        }
        free_delta_by_context = {
            131072: 7.30,
            163840: 6.98,
            167936: 7.04,
            169984: 7.15,
        }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            success = successful_probes.get(key, False)
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory",
                gpu_vram={
                    "0": {"free_pct": 10.0},
                    "1": {"free_pct": 10.0},
                }
                if success
                else None,
                free_vram_delta_pct=free_delta_by_context.get(context) if success else None,
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._maximize_context_for_speed_mode(
                model_name="TestModel",
                model_config={},
                min_context=131072,
                max_context=262144,
                granularity=2048,
                ngl=99,
                starting_split="0.55,0.45",
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
            )

        assert result is not None
        assert result.context == 169984
        assert result.ngl == 99

    def test_rebalance_split_tries_one_percent_fallback_after_two_percent_failure(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.55,0.45":
                return {
                    "0": {"used": 85.0, "free": 15.0, "total": 100.0, "free_pct": 15.0},
                    "1": {"used": 75.0, "free": 25.0, "total": 100.0, "free_pct": 25.0},
                }
            return {
                "0": {"used": 81.0, "free": 19.0, "total": 100.0, "free_pct": 19.0},
                "1": {"used": 77.0, "free": 23.0, "total": 100.0, "free_pct": 23.0},
            }

        successful_probes = {
            (167936, 99, "0.55,0.45"),
            (167936, 99, "0.54,0.46"),
        }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory",
                gpu_vram=build_vram(tensor_split or "0.55,0.45") if success else None,
                free_vram_delta_pct=free_vram_delta_pct(build_vram(tensor_split or "0.55,0.45")) if success else None,
            )
            cache[key] = result
            return result

        starting_result = ProbeResult(
            model="TestModel",
            context=167936,
            ngl=99,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=1.0,
            smoke_seconds=0.1,
            status_code=200,
            gpu_vram=build_vram("0.55,0.45"),
            free_vram_delta_pct=free_vram_delta_pct(build_vram("0.55,0.45")),
        )

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._rebalance_split_by_vram(
                model_name="TestModel",
                model_config={},
                starting_result=starting_result,
                step=0.02,
                balance_threshold_pct=5.0,
                split_min=0.30,
                split_max=0.70,
            )

        assert result.tensor_split == "0.54,0.46"
        assert observed == [
            (167936, 99, "0.53,0.47"),
            (167936, 99, "0.54,0.46"),
        ]

    def test_speed_mode_uses_local_one_percent_split_near_frontier(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (131072, 99, "0.60,0.40"),
            (163840, 99, "0.60,0.40"),
            (180224, 99, "0.60,0.40"),
            (184320, 99, "0.60,0.40"),
            (186368, 99, "0.60,0.40"),
            (188416, 99, "0.61,0.39"),
        }

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.61,0.39":
                return {
                    "0": {"used": 11880.0, "free": 40.0, "total": 12288.0, "free_pct": 3.20},
                    "1": {"used": 15820.0, "free": 35.0, "total": 16311.0, "free_pct": 2.90},
                }
            return {
                "0": {"used": 10930.0, "free": 980.0, "total": 12288.0, "free_pct": 7.98},
                "1": {"used": 15822.0, "free": 27.0, "total": 16311.0, "free_pct": 0.17},
            }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            vram = build_vram(tensor_split or "0.60,0.40")
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 500,
                error=None if success else "Internal Server Error",
                gpu_vram=vram,
                free_vram_delta_pct=free_vram_delta_pct(vram),
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._maximize_context_for_speed_mode(
                model_name="TestModel",
                model_config={},
                min_context=131072,
                max_context=262144,
                granularity=2048,
                ngl=99,
                starting_split="0.60,0.40",
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
            )

        assert result is not None
        assert result.context == 188416
        assert result.tensor_split == "0.61,0.39"
        assert (188416, 99, "0.61,0.39") in observed
        assert (190464, 99, "0.61,0.39") in observed
        assert (262144, 99, "0.61,0.39") not in observed
        assert (196608, 99, "0.61,0.39") not in observed

    def test_speed_mode_uses_smaller_context_bisection_when_headroom_is_critical(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (131072, 99, "0.60,0.40"),
            (163840, 99, "0.60,0.40"),
            (180224, 99, "0.60,0.40"),
            (182272, 99, "0.60,0.40"),
            (184320, 99, "0.60,0.40"),
            (186368, 99, "0.60,0.40"),
            (188416, 99, "0.61,0.39"),
            (189440, 99, "0.61,0.39"),
        }

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.61,0.39":
                return {
                    "0": {"used": 11870.0, "free": 41.0, "total": 12288.0, "free_pct": 0.33},
                    "1": {"used": 16260.0, "free": 51.0, "total": 16311.0, "free_pct": 0.31},
                }
            return {
                "0": {"used": 10920.0, "free": 991.0, "total": 12288.0, "free_pct": 8.06},
                "1": {"used": 15810.0, "free": 39.0, "total": 16311.0, "free_pct": 0.24},
            }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            vram = build_vram(tensor_split or "0.60,0.40") if success else None
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 500,
                error=None if success else "Internal Server Error",
                gpu_vram=vram,
                free_vram_delta_pct=free_vram_delta_pct(vram) if success else None,
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._maximize_context_for_speed_mode(
                model_name="TestModel",
                model_config={},
                min_context=131072,
                max_context=262144,
                granularity=2048,
                ngl=99,
                starting_split="0.60,0.40",
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
            )

        assert result is not None
        assert result.context == 189440
        assert result.tensor_split == "0.61,0.39"
        assert (194560, 99, "0.61,0.39") not in observed
        assert (189440, 99, "0.61,0.39") in observed

    def test_rebalance_split_skips_two_percent_shift_below_one_gib_receiver_headroom(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.61,0.39":
                return {
                    "0": {"used": 11588.0, "free": 700.0, "total": 12288.0, "free_pct": 5.696614583333333},
                    "1": {"used": 15838.0, "free": 473.0, "total": 16311.0, "free_pct": 2.900496597387652},
                }
            return {
                "0": {"used": 11297.0, "free": 991.0, "total": 12288.0, "free_pct": 8.064778645833332},
                "1": {"used": 16272.0, "free": 39.0, "total": 16311.0, "free_pct": 0.2391024462019496},
            }

        successful_probes = {
            (182272, 99, "0.60,0.40"),
            (182272, 99, "0.61,0.39"),
        }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            vram = build_vram(tensor_split or "0.60,0.40") if success else None
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 860.98 MiB on device 0: cudaMalloc failed: out of memory",
                gpu_vram=vram,
                free_vram_delta_pct=free_vram_delta_pct(vram) if success else None,
            )
            cache[key] = result
            return result

        starting_result = ProbeResult(
            model="TestModel",
            context=182272,
            ngl=99,
            tensor_split="0.60,0.40",
            success=True,
            load_seconds=1.0,
            smoke_seconds=0.1,
            status_code=200,
            gpu_vram=build_vram("0.60,0.40"),
            free_vram_delta_pct=free_vram_delta_pct(build_vram("0.60,0.40")),
        )

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._rebalance_split_by_vram(
                model_name="TestModel",
                model_config={},
                starting_result=starting_result,
                step=0.02,
                balance_threshold_pct=5.0,
                split_min=0.30,
                split_max=0.70,
            )

        assert result.tensor_split == "0.61,0.39"
        assert observed == [
            (182272, 99, "0.61,0.39"),
        ]

    def test_optimize_ngl_for_baseline_rebalances_after_ngl_drop(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (16384, 44, "0.55,0.45"),
            (16384, 44, "0.57,0.43"),
        }

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.55,0.45":
                return {
                    "0": {"used": 75.0, "free": 25.0, "total": 100.0, "free_pct": 25.0},
                    "1": {"used": 85.0, "free": 15.0, "total": 100.0, "free_pct": 15.0},
                }
            return {
                "0": {"used": 76.0, "free": 24.0, "total": 100.0, "free_pct": 24.0},
                "1": {"used": 78.0, "free": 22.0, "total": 100.0, "free_pct": 22.0},
            }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory",
                gpu_vram=build_vram(tensor_split or "0.55,0.45") if success else None,
                free_vram_delta_pct=free_vram_delta_pct(build_vram(tensor_split or "0.55,0.45")) if success else None,
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._optimize_ngl_for_baseline(
                model_name="TestModel",
                model_config={},
                context=16384,
                tensor_split="0.55,0.45",
                min_ngl=36,
                max_ngl=52,
                ngl_step=8,
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
            )

        assert result is not None
        assert result.ngl == 44
        assert result.tensor_split == "0.57,0.43"
        assert observed == [
            (16384, 52, "0.55,0.45"),
            (16384, 44, "0.55,0.45"),
            (16384, 44, "0.57,0.43"),
        ]

    def test_context_bisection_rebalances_split_per_candidate(self, tmp_path: Path):
        finetuner = _make_finetuner(tmp_path)
        observed: list[tuple[int, int, str | None]] = []
        cache: dict[tuple[int, int, str | None], ProbeResult] = {}
        successful_probes = {
            (262144, 44, "0.57,0.43"),
            (262144, 44, "0.59,0.41"),
        }

        def build_vram(split: str) -> dict[str, dict[str, float]]:
            if split == "0.57,0.43":
                return {
                    "0": {"used": 74.0, "free": 26.0, "total": 100.0, "free_pct": 26.0},
                    "1": {"used": 84.0, "free": 16.0, "total": 100.0, "free_pct": 16.0},
                }
            return {
                "0": {"used": 79.0, "free": 21.0, "total": 100.0, "free_pct": 21.0},
                "1": {"used": 82.0, "free": 19.0, "total": 100.0, "free_pct": 19.0},
            }

        def fake_probe(*, model_name, model_config, context, ngl, tensor_split):
            key = (context, ngl, tensor_split)
            if key in cache:
                return cache[key]
            observed.append(key)
            success = key in successful_probes
            result = ProbeResult(
                model=model_name,
                context=context,
                ngl=ngl,
                tensor_split=tensor_split,
                success=success,
                load_seconds=1.0,
                smoke_seconds=0.1 if success else 0.0,
                status_code=200 if success else 503,
                error=None if success else "allocating 469.00 MiB on device 1: cudaMalloc failed: out of memory",
                gpu_vram=build_vram(tensor_split or "0.57,0.43") if success else None,
                free_vram_delta_pct=free_vram_delta_pct(build_vram(tensor_split or "0.57,0.43")) if success else None,
            )
            cache[key] = result
            return result

        with patch.object(finetuner, "_probe_candidate", side_effect=fake_probe):
            result = finetuner._maximize_context_with_balanced_runtime(
                model_name="TestModel",
                model_config={},
                min_context=131072,
                max_context=262144,
                granularity=2048,
                ngl=44,
                starting_split="0.57,0.43",
                refine_step=0.02,
                split_min=0.30,
                split_max=0.70,
            )

        assert result is not None
        assert result.context == 262144
        assert result.tensor_split == "0.59,0.41"
        assert observed == [
            (262144, 44, "0.57,0.43"),
            (262144, 44, "0.59,0.41"),
        ]


class TestRuntimeModeHelpers:
    def test_resolve_runtime_mode_uses_smoke_image_for_auto(self):
        assert resolve_runtime_mode("auto", None) == "text"
        assert resolve_runtime_mode("auto", "https://example.com/test.png") == "vision"

    def test_resolve_runtime_config_value_prefers_runtime_override(self):
        config = {
            "context": 262144,
            "ngl": 68,
            "tensor_split": "0.62,0.38",
            "vision_context": 131072,
            "vision_ngl": 36,
            "vision_tensor_split": "0.55,0.45",
        }

        assert resolve_runtime_config_value(config, "context", "vision") == 131072
        assert resolve_runtime_config_value(config, "ngl", "vision") == 36
        assert resolve_runtime_config_value(config, "tensor_split", "vision") == "0.55,0.45"
        assert resolve_runtime_config_value(config, "context", "text") == 262144

    def test_apply_runtime_search_values_targets_vision_fields(self):
        config = {
            "context": 262144,
            "ngl": 68,
            "tensor_split": "0.62,0.38",
            "mmproj": "/models/mmproj.gguf",
        }

        updated = apply_runtime_search_values(
            config,
            context=131072,
            ngl=36,
            tensor_split="0.55,0.45",
            runtime_mode="vision",
        )

        assert updated["context"] == 262144
        assert updated["ngl"] == 68
        assert updated["vision_context"] == 131072
        assert updated["vision_ngl"] == 36
        assert updated["vision_tensor_split"] == "0.55,0.45"


class TestOptimizationHelpers:
    def test_resolve_optimization_mode_validates_known_modes(self):
        assert resolve_optimization_mode("speed") == "speed"
        assert resolve_optimization_mode("CONTEXT") == "context"
        assert resolve_optimization_mode("balanced") == "balanced"

    def test_balanced_tradeoff_score_penalizes_lopsided_results(self):
        context_heavy = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=36,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=10.0,
        )
        equilibrium = ProbeResult(
            model="TestModel",
            context=196608,
            ngl=68,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=11.0,
        )

        assert balanced_tradeoff_score(equilibrium, max_context=262144, max_ngl=99) > balanced_tradeoff_score(
            context_heavy,
            max_context=262144,
            max_ngl=99,
        )

    def test_balance_metric_prefers_measured_vram_delta(self):
        result = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=52,
            tensor_split="0.50,0.50",
            success=True,
            load_seconds=10.0,
            free_vram_delta_pct=3.5,
        )

        assert balance_metric(result) == 3.5


class TestResultSelection:
    def test_choose_better_result_prioritizes_balanced_split_over_higher_ngl_at_same_context(self):
        current = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=68,
            tensor_split="0.60,0.40",
            success=True,
            load_seconds=20.0,
        )
        candidate = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=52,
            tensor_split="0.50,0.50",
            success=True,
            load_seconds=25.0,
        )
        assert choose_better_result(current, candidate) is candidate

    def test_choose_better_result_prioritizes_higher_ngl_when_context_and_split_match(self):
        current = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=44,
            tensor_split="0.50,0.50",
            success=True,
            load_seconds=20.0,
        )
        candidate = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=52,
            tensor_split="0.50,0.50",
            success=True,
            load_seconds=25.0,
        )
        assert choose_better_result(current, candidate) is candidate

    def test_choose_better_result_speed_mode_prefers_higher_ngl_over_context(self):
        current = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=44,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=20.0,
        )
        candidate = ProbeResult(
            model="TestModel",
            context=196608,
            ngl=52,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=25.0,
        )

        assert (
            choose_better_result(
                current,
                candidate,
                optimization="speed",
                max_context=262144,
                max_ngl=99,
            )
            is candidate
        )

    def test_choose_better_result_balanced_mode_prefers_equilibrium(self):
        current = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=36,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=20.0,
        )
        candidate = ProbeResult(
            model="TestModel",
            context=196608,
            ngl=68,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=25.0,
        )

        assert (
            choose_better_result(
                current,
                candidate,
                optimization="balanced",
                max_context=262144,
                max_ngl=99,
            )
            is candidate
        )

    def test_choose_better_result_prefers_lower_vram_delta_over_closer_50_50(self):
        current = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=52,
            tensor_split="0.50,0.50",
            success=True,
            load_seconds=20.0,
            free_vram_delta_pct=12.0,
        )
        candidate = ProbeResult(
            model="TestModel",
            context=262144,
            ngl=52,
            tensor_split="0.55,0.45",
            success=True,
            load_seconds=25.0,
            free_vram_delta_pct=4.0,
        )

        assert choose_better_result(current, candidate, optimization="context") is candidate


class TestBinarySearch:
    def test_binary_search_finds_highest_successful_context(self):
        threshold = 196608
        result, attempts = binary_search_max_success(
            min_context=131072,
            max_context=262144,
            granularity=2048,
            anchor_context=196608,
            probe=lambda context: context <= threshold,
        )
        assert result == threshold
        assert threshold in attempts
        assert len(attempts) < 10

    def test_binary_search_returns_none_when_lower_bound_fails(self):
        result, attempts = binary_search_max_success(
            min_context=32768,
            max_context=65536,
            granularity=2048,
            anchor_context=65536,
            probe=lambda context: False,
        )
        assert result is None
        assert attempts[0] == 65536


class TestModelBlockReplacement:
    def test_replace_model_block_only_changes_target_model(self):
        original = (
            "models:\n"
            "  Foo:\n"
            "    context: 32768\n"
            "    tensor_split: \"0.55,0.45\"\n"
            "  Bar:\n"
            "    context: 65536\n"
        )
        replacement = render_model_block(
            "Foo",
            {"context": 196608, "tensor_split": "0.62,0.38"},
        )
        updated = replace_model_block(original, "Foo", replacement)
        assert '  Foo:\n    context: 196608\n    tensor_split: "0.62,0.38"' in updated
        assert '  Bar:\n    context: 65536' in updated

    def test_replace_model_block_stops_before_top_level_aliases(self):
        original = (
            "models:\n"
            "  Foo:\n"
            "    context: 32768\n"
            "aliases:\n"
            "  foo: \"Foo\"\n"
        )
        replacement = render_model_block("Foo", {"context": 65536})
        updated = replace_model_block(original, "Foo", replacement)
        assert "aliases:\n  foo: \"Foo\"" in updated
        assert "    context: 65536" in updated