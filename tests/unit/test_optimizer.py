"""Tests for app.proxy.optimizer — Benchmark-driven request optimization."""

import json
from pathlib import Path

import pytest

from tests.conftest import SAMPLE_BENCHMARK_STATE, SAMPLE_CONTEXT_RESULTS


# ── Helpers ────────────────────────────────────────────────────────────


def _make_optimizer(tmp_path: Path, state=None, context=None):
    """Create a RequestOptimizer with temp benchmark files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)

    state_file = str(data_dir / "benchmark_state.json")
    context_file = str(docs_dir / "benchmark_results.json")

    if state is not None:
        (data_dir / "benchmark_state.json").write_text(json.dumps(state))
    if context is not None:
        (docs_dir / "benchmark_results.json").write_text(json.dumps(context))

    from app.proxy.optimizer import RequestOptimizer

    return RequestOptimizer(
        legacy_state_file=state_file,
        context_results_file=context_file,
    )


# ── load_benchmarks ───────────────────────────────────────────────────


class TestLoadBenchmarks:
    def test_loads_legacy_state(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        assert "GLM-4.7-Flash" in opt.best_configs
        # Best TPS for GLM is 42.5 (ctx 8192) not 38.0 (ctx 16384)
        assert opt.best_configs["GLM-4.7-Flash"]["tps"] == 42.5
        assert opt.best_configs["GLM-4.7-Flash"]["num_ctx"] == 8192

    def test_skips_failed_results(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        assert "broken-model" not in opt.best_configs

    def test_loads_context_results(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, context=SAMPLE_CONTEXT_RESULTS)
        assert "Qwen3-30B-A3B" in opt.best_configs
        assert opt.best_configs["Qwen3-30B-A3B"]["tps"] == 28.0

    def test_skips_errored_context(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, context=SAMPLE_CONTEXT_RESULTS)
        assert "FailedModel" not in opt.best_configs

    def test_merges_both_sources(self, tmp_path: Path):
        opt = _make_optimizer(
            tmp_path, state=SAMPLE_BENCHMARK_STATE, context=SAMPLE_CONTEXT_RESULTS
        )
        # GLM-4.7-Flash: legacy says 42.5 TPS, context says 35.0 → legacy wins
        assert opt.best_configs["GLM-4.7-Flash"]["tps"] == 42.5
        # Qwen3-30B-A3B: only in context
        assert "Qwen3-30B-A3B" in opt.best_configs

    def test_handles_missing_files(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path)  # No files written
        assert opt.best_configs == {}

    def test_caches_by_mtime(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        first_load = opt.last_load

        # Load again without file change — should be cached
        opt.load_benchmarks()
        assert opt.last_load == first_load


# ── optimize_options ───────────────────────────────────────────────────


class TestOptimizeOptions:
    def test_injects_num_ctx(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        result = opt.optimize_options("GLM-4.7-Flash", {})
        assert result["num_ctx"] == 8192

    def test_respects_user_override(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        result = opt.optimize_options("GLM-4.7-Flash", {"num_ctx": 4096})
        # User-set value must not be overridden
        assert result["num_ctx"] == 4096

    def test_clamps_to_max_context(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        result = opt.optimize_options("GLM-4.7-Flash", {}, max_context=4096)
        assert result["num_ctx"] == 4096  # Clamped from 8192

    def test_no_clamp_when_within_limit(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        result = opt.optimize_options("GLM-4.7-Flash", {}, max_context=16384)
        assert result["num_ctx"] == 8192  # No clamping needed

    def test_unknown_model_returns_unchanged(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        original = {"temperature": 0.7}
        result = opt.optimize_options("nonexistent-model", original)
        assert result == original

    def test_partial_match(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        # "GLM-4.7" is a substring of "GLM-4.7-Flash"
        result = opt.optimize_options("GLM-4.7", {})
        assert "num_ctx" in result

    def test_does_not_mutate_original(self, tmp_path: Path):
        opt = _make_optimizer(tmp_path, state=SAMPLE_BENCHMARK_STATE)
        original = {"temperature": 0.5}
        result = opt.optimize_options("GLM-4.7-Flash", original)
        assert "num_ctx" not in original
        assert "num_ctx" in result
