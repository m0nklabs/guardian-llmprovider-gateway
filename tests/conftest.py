"""Shared fixtures for Guardian tests."""

import json
from pathlib import Path

import pytest

# ── Test isolation ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_model_manager_active_requests():
    """Stop the shared in-flight counter from crossing a test boundary.

    ``proxy_v1_post`` defers the release of its ``active_requests`` slot to the
    SSE generator once it returns a ``StreamingResponse``, so a streaming test
    that never consumes the body leaves the counter elevated.  #119: that leak
    from ``test_v1_path_bound``/``test_server`` made
    ``test_model_switch_verification``'s before/after accounting start from a
    stale value and fail only in that module order.  Runtime wiring untouched.
    """
    from app.proxy import server

    manager = getattr(server, "model_manager", None)
    before = getattr(manager, "active_requests", None)
    yield
    if isinstance(before, int):
        try:
            manager.active_requests = before
        except AttributeError:  # a test swapped in a stub manager
            pass


# ── Test data ──────────────────────────────────────────────────────────

SAMPLE_API_KEYS: dict[str, dict] = {
    "flip_aabbccdd11223344aabbccdd11223344": {
        "name": "test-user",
        "created_at": 1700000000.0,
        "metadata": {},
    },
    "oelala_eeff00112233445566778899aabbccdd": {
        "name": "oelala",
        "created_at": 1700000001.0,
        "metadata": {"client": "oelala"},
    },
}

SAMPLE_MODELS_YAML = """\
models:
    GLM-4.7-Flash:
        path: /models/GLM-4.7-Flash.gguf
        ngl: 99
        ctx: 8192
        benchmark_context_limit: 16384
        ts: "17,11"
    Qwen3-30B-A3B:
        path: /models/Qwen3-30B.gguf
        ngl: 99
        ctx: 4096
        benchmark_context_limit: 32768
        ts: "20,8"
"""

SAMPLE_SETTINGS_YAML = """\
server:
  host: 0.0.0.0
  port: 11434
  backend_port: 11440
  idle_timeout: 300

queue:
  max_concurrent: 1
  queue_timeout_seconds: 300

services:
  comfyui_url: http://127.0.0.1:8188

vram:
  safety_buffer_mb: 1500
"""

SAMPLE_BENCHMARK_STATE = {
    "completed": [
        {
            "success": True,
            "config": {"model": "GLM-4.7-Flash", "ctx": 8192},
            "metrics": {"tps": 42.5},
        },
        {
            "success": True,
            "config": {"model": "GLM-4.7-Flash", "ctx": 16384},
            "metrics": {"tps": 38.0},
        },
        {
            "success": False,
            "config": {"model": "broken-model", "ctx": 4096},
            "metrics": {"tps": 0},
        },
    ],
    "queue": [],
}

SAMPLE_CONTEXT_RESULTS = [
    {
        "model_name": "GLM-4.7-Flash",
        "context_size": 32768,
        "tokens_per_second": 35.0,
        "status": "success",
    },
    {
        "model_name": "Qwen3-30B-A3B",
        "context_size": 4096,
        "tokens_per_second": 28.0,
        "status": "success",
    },
    {
        "model_name": "FailedModel",
        "context_size": 4096,
        "tokens_per_second": 0,
        "status": "error",
    },
]


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with sample files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    (config_dir / "api_keys.json").write_text(json.dumps(SAMPLE_API_KEYS, indent=2))
    (config_dir / "models.yaml").write_text(SAMPLE_MODELS_YAML)
    (config_dir / "settings.yaml").write_text(SAMPLE_SETTINGS_YAML)

    return config_dir


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory with benchmark files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    (data_dir / "benchmark_state.json").write_text(
        json.dumps(SAMPLE_BENCHMARK_STATE, indent=2)
    )

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "benchmark_results.json").write_text(
        json.dumps(SAMPLE_CONTEXT_RESULTS, indent=2)
    )

    return data_dir


@pytest.fixture
def api_keys_file(tmp_config_dir: Path) -> Path:
    """Return the path to the temporary api_keys.json."""
    return tmp_config_dir / "api_keys.json"
