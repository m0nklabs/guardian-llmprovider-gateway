"""Tests for app.tweaker.model_finetune."""

from app.tweaker.model_finetune import (
    align_context_ceil,
    align_context_floor,
    binary_search_max_success,
    build_ngl_candidates,
    build_model_signature,
    build_probe_cache_key,
    build_smoke_messages,
    build_smoke_signature,
    build_split_candidates,
    choose_better_result,
    ProbeResult,
    resolve_context_bounds,
    resolve_candidate_context_bounds,
    format_two_gpu_split,
    index_cached_probes,
    parse_two_gpu_split,
    render_model_block,
    replace_model_block,
    split_balance_distance,
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


class TestNglHelpers:
    def test_build_ngl_candidates_prefers_higher_values(self):
        candidates = build_ngl_candidates(36, 16, 36, 99)
        assert candidates == [99, 84, 68, 52, 36]


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
        }
        variant = {
            **base,
            "context": 196608,
            "ngl": 52,
            "tensor_split": "0.60,0.40",
        }
        assert build_model_signature("TestModel", base) == build_model_signature("TestModel", variant)

    def test_index_cached_probes_filters_by_model_and_smoke_signature(self):
        model_signature = build_model_signature("TestModel", {"path": "/tmp/model.gguf", "ngl": 36})
        smoke_signature = build_smoke_signature("Reply with exactly: FIT OK", 8, "https://example.com/test.png")
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
        assert len(indexed) == 1


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