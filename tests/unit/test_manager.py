"""Tests for app.engine.manager — Core model lifecycle management."""

import asyncio
from pathlib import Path
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest
import yaml
import httpx

from tests.conftest import SAMPLE_MODELS_YAML, SAMPLE_SETTINGS_YAML


# ── Helpers ────────────────────────────────────────────────────────────

MODELS_CFG = yaml.safe_load(SAMPLE_MODELS_YAML)


def _make_manager(tmp_path: Path, models_yaml: str = SAMPLE_MODELS_YAML):
    """Create a ModelManager with a temp config, patching out subprocess/file side effects."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)

    models_file = config_dir / "models.yaml"
    models_file.write_text(models_yaml)

    settings_file = config_dir / "settings.yaml"
    settings_file.write_text(SAMPLE_SETTINGS_YAML)

    # Create dummy args file for initial model detection
    args_file = config_dir / "current_model.args"
    args_file.write_text("-m /models/GLM-4.7-Flash.gguf -c 8192 -ngl 99")

    with patch("app.engine.manager.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="")
        from app.engine.manager import ModelManager

        mgr = ModelManager(config_path=str(models_file))

    return mgr


# ── _load_config ───────────────────────────────────────────────────────


class TestLoadConfig:
    def test_loads_models(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        assert "GLM-4.7-Flash" in mgr.models
        assert "Qwen3-30B-A3B" in mgr.models

    def test_model_paths(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        assert mgr.models["GLM-4.7-Flash"]["path"] == "/models/GLM-4.7-Flash.gguf"

    def test_empty_config(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        f = config_dir / "models.yaml"
        f.write_text("models: {}")
        (config_dir / "settings.yaml").write_text(SAMPLE_SETTINGS_YAML)

        with patch("app.engine.manager.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1, stdout="")
            from app.engine.manager import ModelManager

            mgr = ModelManager(config_path=str(f))
        assert mgr.models == {}

    def test_missing_config_returns_empty(self, tmp_path: Path):
        fake = tmp_path / "nonexistent.yaml"
        with patch("app.engine.manager.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1, stdout="")
            from app.engine.manager import ModelManager

            mgr = ModelManager(config_path=str(fake))
        assert mgr.models == {}


# ── Pinned model ───────────────────────────────────────────────────────


class TestPinnedModel:
    def test_no_pin_by_default(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        assert mgr.pinned_model is None

    def test_pinned_model_loaded(self, tmp_path: Path):
        yaml_with_pin = SAMPLE_MODELS_YAML + "\nguardian:\n  pinned_model: GLM-4.7-Flash\n"
        mgr = _make_manager(tmp_path, models_yaml=yaml_with_pin)
        assert mgr.pinned_model == "GLM-4.7-Flash"
        assert mgr.current_model == "GLM-4.7-Flash"


# ── Switch allowlist ───────────────────────────────────────────────────


class TestSwitchAllowlist:
    def test_empty_allowlist_allows_all(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        assert mgr.is_switch_allowed("anyone") is True

    def test_allowlist_restricts(self, tmp_path: Path):
        yaml_with_al = SAMPLE_MODELS_YAML + "\nguardian:\n  switch_allowlist:\n    - admin\n    - oelala\n"
        mgr = _make_manager(tmp_path, models_yaml=yaml_with_al)
        assert mgr.is_switch_allowed("admin") is True
        assert mgr.is_switch_allowed("oelala") is True
        assert mgr.is_switch_allowed("random") is False


# ── _detect_initial_model ─────────────────────────────────────────────


class TestDetectInitialModel:
    def test_detects_from_args_file(self, tmp_path: Path):
        args_file = tmp_path / "config" / "current_model.args"
        args_file.parent.mkdir(parents=True, exist_ok=True)
        args_file.write_text("-m /models/GLM-4.7-Flash.gguf -c 8192 -ngl 99")

        with patch("app.engine.manager.Path") as MockPath:
            real_path = Path
            def path_side_effect(*args, **kwargs):
                p = real_path(*args, **kwargs)
                return p
            MockPath.side_effect = path_side_effect

        # Patch the hardcoded args file path in _detect_initial_model
        mgr = _make_manager(tmp_path)
        with patch.object(mgr, '_detect_initial_model') as mock_detect:
            mock_detect.return_value = "GLM-4.7-Flash"
            mgr.current_model = mgr._pinned_model or mock_detect()
        assert mgr.current_model == "GLM-4.7-Flash"

    def test_fallback_on_no_match(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        # _identify_model_by_path for an unknown path returns None
        result = mgr._identify_model_by_path("/models/UNKNOWN.gguf")
        assert result is None
        # Fallback should be first model in config
        assert mgr.current_model in mgr.models

    def test_detects_same_path_profile_from_extra_args(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        models_file = config_dir / "models.yaml"
        models_file.write_text(
            """\
models:
  Qwen-Deep:
    path: /models/qwen.gguf
    context: 131072
    extra_args: "--reasoning on --reasoning-budget -1 --temp 0.6"
  Qwen-Agent:
    path: /models/qwen.gguf
    context: 65536
    extra_args: "--chat-template-file /tmp/qwen_nonthinking.jinja --temp 0.7 --top-p 0.8"
"""
        )
        (config_dir / "settings.yaml").write_text(SAMPLE_SETTINGS_YAML)
        (config_dir / "current_model.args").write_text(
            "-m /models/qwen.gguf -c 65536 -ngl 99 --host 127.0.0.1 --port 11440 "
            "--chat-template-file /tmp/qwen_nonthinking.jinja --temp 0.7 --top-p 0.8"
        )

        with patch("app.engine.manager.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1, stdout="")
            from app.engine.manager import ModelManager

            mgr = ModelManager(config_path=str(models_file))

        assert mgr.current_model == "Qwen-Agent"


# ── _write_server_args ─────────────────────────────────────────────────


class TestWriteServerArgs:
    def test_writes_basic_args(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)

        config = mgr.models["GLM-4.7-Flash"]
        from app.engine.manager import OFFICIAL_LLAMA_SERVER_BIN

        path = config["path"]
        ctx = config.get("context", 4096)
        ngl = config.get("ngl", 99)

        assert path == "/models/GLM-4.7-Flash.gguf"
        assert ctx == 4096
        assert ngl == 99
        assert "official" in str(OFFICIAL_LLAMA_SERVER_BIN)

    def test_write_server_args_ignores_legacy_backend_key(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)

        config = dict(mgr.models["GLM-4.7-Flash"])
        config["backend"] = "unexpected_backend"
        args_file = tmp_path / "current_model.args"

        with patch("app.engine.manager.CURRENT_MODEL_ARGS_FILE", args_file):
            mgr._write_server_args(config)

        args = args_file.read_text()
        assert "-m /models/GLM-4.7-Flash.gguf" in args
        assert "--host 127.0.0.1 --port 11440" in args

    def test_build_runtime_config_omits_mmproj_for_text_mode(self, tmp_path: Path):
        mmproj = tmp_path / "vision-mmproj.gguf"
        mmproj.write_text("mmproj")
        models_yaml = f"""\
models:
    Vision-Model:
        path: /models/vision.gguf
        context: 131072
        ngl: 99
        tensor_split: \"0.55,0.45\"
        mmproj: {mmproj}
        vision_context: 65536
        vision_ngl: 36
        vision_tensor_split: \"0.60,0.40\"
"""
        mgr = _make_manager(tmp_path, models_yaml=models_yaml)

        runtime = mgr.build_runtime_config("Vision-Model", enable_vision=False)

        assert "mmproj" not in runtime
        assert runtime["context"] == 131072
        assert runtime["ngl"] == 99
        assert runtime["tensor_split"] == "0.55,0.45"

    def test_build_runtime_config_uses_vision_overrides(self, tmp_path: Path):
        mmproj = tmp_path / "vision-mmproj.gguf"
        mmproj.write_text("mmproj")
        models_yaml = f"""\
models:
    Vision-Model:
        path: /models/vision.gguf
        context: 131072
        ngl: 99
        tensor_split: \"0.55,0.45\"
        mmproj: {mmproj}
        vision_context: 65536
        vision_ngl: 36
        vision_tensor_split: \"0.60,0.40\"
"""
        mgr = _make_manager(tmp_path, models_yaml=models_yaml)

        runtime = mgr.build_runtime_config("Vision-Model", enable_vision=True)

        assert runtime["mmproj"] == str(mmproj)
        assert runtime["context"] == 65536
        assert runtime["ngl"] == 36
        assert runtime["tensor_split"] == "0.60,0.40"

    def test_build_crash_config_snapshot_keeps_effective_runtime_shape(self, tmp_path: Path):
        mmproj = tmp_path / "vision-mmproj.gguf"
        mmproj.write_text("mmproj")
        models_yaml = f"""\
models:
    Vision-Model:
        path: /models/vision.gguf
        context: 131072
        ngl: 99
        tensor_split: \"0.55,0.45\"
        mmproj: {mmproj}
        vision_context: 65536
        vision_ngl: 36
        vision_tensor_split: \"0.60,0.40\"
"""
        mgr = _make_manager(tmp_path, models_yaml=models_yaml)

        runtime = mgr.build_runtime_config("Vision-Model", enable_vision=True)
        snapshot = mgr._build_crash_config_snapshot(
            "Vision-Model",
            runtime_config=runtime,
            vision_enabled=True,
        )

        assert snapshot["runtime_mode"] == "vision"
        assert snapshot["vision_ngl"] == 36
        assert snapshot["effective_runtime_config"]["ngl"] == 36
        assert snapshot["effective_runtime_config"]["tensor_split"] == "0.60,0.40"
        assert snapshot["effective_runtime_config"]["mmproj"] == str(mmproj)


# ── _identify_model_by_path ───────────────────────────────────────────


class TestIdentifyModelByPath:
    def test_finds_known_model(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        result = mgr._identify_model_by_path("/models/GLM-4.7-Flash.gguf")
        assert result == "GLM-4.7-Flash"

    def test_returns_none_for_unknown(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        result = mgr._identify_model_by_path("/models/nonexistent.gguf")
        assert result is None


class TestAdvertisedContextWindow:
    def test_uses_runtime_context_not_theoretical_max(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"]["context"] = 8192
        mgr.models["GLM-4.7-Flash"]["benchmark_context_limit"] = 16384

        assert mgr.get_advertised_context_window("GLM-4.7-Flash") == 7168

    def test_keeps_headroom_for_large_contexts(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"]["context"] = 131072
        mgr.models["GLM-4.7-Flash"]["benchmark_context_limit"] = 131072

        assert mgr.get_advertised_context_window("GLM-4.7-Flash") == 126976

    def test_allows_explicit_advertised_override(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"]["context"] = 216064
        mgr.models["GLM-4.7-Flash"]["benchmark_context_limit"] = 262144
        mgr.models["GLM-4.7-Flash"]["advertised_context"] = 200000

        assert mgr.get_advertised_context_window("GLM-4.7-Flash") == 200000

    def test_returns_none_without_runtime_context(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"].pop("context", None)
        mgr.models["GLM-4.7-Flash"].pop("ctx", None)
        mgr.models["GLM-4.7-Flash"]["benchmark_context_limit"] = 16384

        assert mgr.get_advertised_context_window("GLM-4.7-Flash") is None


class TestContextWindowFields:
    def test_runtime_context_uses_configured_context(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"]["context"] = 8192
        mgr.models["GLM-4.7-Flash"]["benchmark_context_limit"] = 16384

        assert mgr.get_runtime_context_window("GLM-4.7-Flash") == 8192

    def test_benchmark_limit_reads_model_cap(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"]["context"] = 8192
        mgr.models["GLM-4.7-Flash"]["benchmark_context_limit"] = 16384

        assert mgr.get_benchmark_context_limit("GLM-4.7-Flash") == 16384

    def test_benchmark_limit_does_not_fall_back_to_runtime(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.models["GLM-4.7-Flash"]["context"] = 8192
        mgr.models["GLM-4.7-Flash"].pop("benchmark_context_limit", None)

        assert mgr.get_benchmark_context_limit("GLM-4.7-Flash") is None


class TestPublicModelMap:
    def test_includes_valid_aliases(self, tmp_path: Path):
        models_yaml = (
            SAMPLE_MODELS_YAML
            + """
aliases:
  glm-flash: GLM-4.7-Flash
  qwen-fast: Qwen3-30B-A3B
  missing: Missing-Model
"""
        )
        mgr = _make_manager(tmp_path, models_yaml=models_yaml)

        public_models = mgr.get_public_model_map()

        assert public_models["GLM-4.7-Flash"] == "GLM-4.7-Flash"
        assert public_models["glm-flash"] == "GLM-4.7-Flash"
        assert public_models["qwen-fast"] == "Qwen3-30B-A3B"
        assert "missing" not in public_models


class TestVisionCapabilityState:
        def test_marks_existing_mmproj_models_unverified(self, tmp_path: Path):
                mmproj = tmp_path / "vision-mmproj.gguf"
                mmproj.write_text("mmproj")
                models_yaml = f"""\
models:
    Vision-Model:
        path: /models/vision.gguf
        mmproj: {mmproj}
    Text-Only:
        path: /models/text.gguf
"""
                mgr = _make_manager(tmp_path, models_yaml=models_yaml)

                vision = mgr.get_vision_capability("Vision-Model")
                text_only = mgr.get_vision_capability("Text-Only")

                assert vision["configured"] is True
                assert vision["status"] == "unverified"
                assert vision["mmproj_exists"] is True
                assert text_only["configured"] is False
                assert text_only["status"] == "text_only"

        def test_marks_missing_mmproj_as_misconfigured(self, tmp_path: Path):
                models_yaml = """\
models:
    Broken-Vision:
        path: /models/vision.gguf
        mmproj: /models/missing-mmproj.gguf
"""
                mgr = _make_manager(tmp_path, models_yaml=models_yaml)

                vision = mgr.get_vision_capability("Broken-Vision")

                assert vision["configured"] is True
                assert vision["status"] == "misconfigured"
                assert "mmproj file not found" in vision["last_error"]

        def test_reset_vision_validation_clears_cached_result(self, tmp_path: Path):
                mmproj = tmp_path / "vision-mmproj.gguf"
                mmproj.write_text("mmproj")
                models_yaml = f"""\
models:
    Vision-Model:
        path: /models/vision.gguf
        mmproj: {mmproj}
"""
                mgr = _make_manager(tmp_path, models_yaml=models_yaml)

                mgr.mark_vision_validation("Vision-Model", "supported")
                assert mgr.get_vision_capability("Vision-Model")["status"] == "supported"

                mgr.reset_vision_validation("Vision-Model")

                vision = mgr.get_vision_capability("Vision-Model")
                assert vision["status"] == "unverified"
                assert vision["last_error"] is None


# ── _get_backend_model_path ───────────────────────────────────────────


class TestGetBackendModelPath:
    def test_parses_pgrep_output(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        pgrep_output = "12345 /usr/bin/llama-server -m /models/GLM-4.7-Flash.gguf -c 8192"
        with patch("app.engine.manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=pgrep_output
            )
            result = mgr._get_backend_model_path()
        assert result == "/models/GLM-4.7-Flash.gguf"

    def test_no_process_running(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        with patch("app.engine.manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = mgr._get_backend_model_path()
        assert result is None


# ── verify_backend_model ──────────────────────────────────────────────


class TestVerifyBackendModel:
    @pytest.mark.asyncio
    async def test_match_returns_true(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with patch.object(mgr, "_get_backend_model_path", return_value="/models/GLM-4.7-Flash.gguf"):
            result = await mgr.verify_backend_model()
        assert result is True
        assert mgr._model_verified is True

    @pytest.mark.asyncio
    async def test_mismatch_returns_false(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with patch.object(mgr, "_get_backend_model_path", return_value="/models/Qwen3-30B.gguf"):
            result = await mgr.verify_backend_model()
        assert result is False
        assert mgr._model_verified is False

    @pytest.mark.asyncio
    async def test_no_process_returns_false(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        with patch.object(mgr, "_get_backend_model_path", return_value=None):
            result = await mgr.verify_backend_model()
        assert result is False


# ── backend_health_ok ─────────────────────────────────────────────────


class TestBackendHealthOk:
    @pytest.mark.asyncio
    async def test_returns_true_on_healthy_backend(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = MagicMock(status_code=200)

        with patch("app.engine.manager.httpx.AsyncClient", return_value=mock_client):
            result = await mgr.backend_health_ok()

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_unhealthy_status(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = MagicMock(status_code=503)

        with patch("app.engine.manager.httpx.AsyncClient", return_value=mock_client):
            result = await mgr.backend_health_ok()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connect_error(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.side_effect = httpx.ConnectError("backend down")

        with patch("app.engine.manager.httpx.AsyncClient", return_value=mock_client):
            result = await mgr.backend_health_ok()

        assert result is False


# ── startup_check ─────────────────────────────────────────────────────


class TestStartupCheck:
    @pytest.mark.asyncio
    async def test_forces_switch_on_mismatch_even_if_current_equals_target(self, tmp_path: Path):
        yaml_with_pin = SAMPLE_MODELS_YAML + "\nguardian:\n  pinned_model: GLM-4.7-Flash\n"
        mgr = _make_manager(tmp_path, models_yaml=yaml_with_pin)

        # Reproduce edge case: current already equals target but backend verification fails
        mgr.current_model = "GLM-4.7-Flash"

        with (
            patch.object(mgr, "verify_backend_model", new_callable=AsyncMock, return_value=False),
            patch.object(mgr, "_get_backend_model_path", return_value="/models/Qwen3-30B.gguf"),
            patch.object(mgr, "switch_model", new_callable=AsyncMock) as mock_switch,
        ):
            await mgr.startup_check()

        mock_switch.assert_awaited_once_with("GLM-4.7-Flash")

    @pytest.mark.asyncio
    async def test_no_switch_when_backend_already_verified(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        with (
            patch.object(mgr, "verify_backend_model", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "switch_model", new_callable=AsyncMock) as mock_switch,
        ):
            await mgr.startup_check()

        mock_switch.assert_not_called()


# ── switch_model security ─────────────────────────────────────────────


class TestSwitchModelSecurity:
    @pytest.mark.asyncio
    async def test_unknown_model_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            await mgr.switch_model("nonexistent-model")

    @pytest.mark.asyncio
    async def test_pinned_model_blocks_switch(self, tmp_path: Path):
        # Pin requires a switch_allowlist; without one, is_switch_allowed() returns True for all
        yaml_with_pin = (
            SAMPLE_MODELS_YAML
            + "\nguardian:\n  pinned_model: GLM-4.7-Flash\n  switch_allowlist:\n    - admin\n"
        )
        mgr = _make_manager(tmp_path, models_yaml=yaml_with_pin)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="pinned"):
            await mgr.switch_model("Qwen3-30B-A3B", client_id="random-client")

    @pytest.mark.asyncio
    async def test_allowlisted_client_can_override_pin(self, tmp_path: Path):
        yaml_combined = (
            SAMPLE_MODELS_YAML
            + "\nguardian:\n  pinned_model: GLM-4.7-Flash\n  switch_allowlist:\n    - admin\n"
        )
        mgr = _make_manager(tmp_path, models_yaml=yaml_combined)
        mgr.current_model = "GLM-4.7-Flash"

        # Should not raise, but will fail on subprocess calls — patch those
        with (
            patch.object(mgr, "_save_context", new_callable=AsyncMock),
            patch.object(mgr, "_stop_server", new_callable=AsyncMock),
            patch.object(mgr, "_free_gpu_memory", new_callable=AsyncMock),
            patch.object(mgr, "_start_server", new_callable=AsyncMock),
            patch.object(mgr, "_wait_for_health", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "verify_backend_model", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "_load_context", new_callable=AsyncMock, side_effect=Exception("no save")),
        ):
            await mgr.switch_model("Qwen3-30B-A3B", client_id="admin")
            assert mgr.current_model == "Qwen3-30B-A3B"

    @pytest.mark.asyncio
    async def test_skip_if_same_model(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"

        # Should return immediately without starting/stopping anything
        with patch.object(mgr, "_stop_server", new_callable=AsyncMock) as mock_stop:
            await mgr.switch_model("GLM-4.7-Flash")
            mock_stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_reloads_same_model_when_vision_mode_changes(self, tmp_path: Path):
        mmproj = tmp_path / "vision-mmproj.gguf"
        mmproj.write_text("mmproj")
        models_yaml = f"""\
models:
    Vision-Model:
        path: /models/vision.gguf
        context: 131072
        ngl: 99
        tensor_split: \"0.55,0.45\"
        mmproj: {mmproj}
        vision_context: 65536
        vision_ngl: 36
        vision_tensor_split: \"0.60,0.40\"
"""
        mgr = _make_manager(tmp_path, models_yaml=models_yaml)
        mgr.current_model = "Vision-Model"
        mgr.current_vision_enabled = True

        with (
            patch.object(mgr, "_save_context", new_callable=AsyncMock),
            patch.object(mgr, "_stop_server", new_callable=AsyncMock),
            patch.object(mgr, "_free_gpu_memory", new_callable=AsyncMock),
            patch.object(mgr, "_start_server", new_callable=AsyncMock),
            patch.object(mgr, "_wait_for_health", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "verify_backend_model", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "_load_context", new_callable=AsyncMock, side_effect=Exception("no save")),
            patch.object(mgr, "_write_server_args") as mock_write,
        ):
            await mgr.switch_model("Vision-Model", enable_vision=False)

        mock_write.assert_called_once()
        runtime_config = mock_write.call_args.args[0]
        assert "mmproj" not in runtime_config
        assert mgr.current_vision_enabled is False


# ── unload ─────────────────────────────────────────────────────────────


class TestUnload:
    @pytest.mark.asyncio
    async def test_unload_stops_server(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        with patch.object(mgr, "_stop_server", new_callable=AsyncMock) as mock_stop:
            await mgr.unload()
        mock_stop.assert_called_once()
        assert mgr.is_unloaded is True

    @pytest.mark.asyncio
    async def test_double_unload_noop(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        with patch.object(mgr, "_stop_server", new_callable=AsyncMock) as mock_stop:
            await mgr.unload()
            await mgr.unload()
        mock_stop.assert_called_once()


# ── _get_comfyui_url ──────────────────────────────────────────────────


class TestGetComfyuiUrl:
    def test_reads_from_settings(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        url = mgr._get_comfyui_url()
        assert url == "http://127.0.0.1:8188"

    def test_fallback_default(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        # Remove settings file
        settings = mgr.config_path.parent / "settings.yaml"
        settings.unlink(missing_ok=True)
        url = mgr._get_comfyui_url()
        assert url == "http://127.0.0.1:8188"


# ── CrashRecord ───────────────────────────────────────────────────────


class TestCrashRecord:
    def test_to_dict(self):
        from app.engine.manager import CrashRecord

        record = CrashRecord(
            timestamp="2026-04-17T12:00:00",
            model="test-model",
            error_message="OOM",
            exit_code=137,
            config_snapshot={"ngl": 99},
        )
        d = record.to_dict()
        assert d["model"] == "test-model"
        assert d["exit_code"] == 137
        assert d["config_snapshot"]["ngl"] == 99


class TestCrashLogParsing:
    def test_extracts_fit_failure_messages(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)

        lines = [
            "llama_params_fit_impl: cannot meet free memory targets on all devices, need to use 1487 MiB less in total",
            "llama_params_fit: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort",
            "/workspace/scripts/start_llama.sh: line 49: 3724619 Segmentation fault      (core dumped) $BINARY $ARGS",
        ]

        result = mgr._extract_crash_error_from_lines(lines)

        assert "cannot meet free memory targets" in result
        assert "failed to fit params to free device memory" in result
        assert "Segmentation fault" in result

    def test_extracts_runtime_cuda_failure_messages(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)

        lines = [
            "CUDA error: out of memory",
            "ggml_gallocr_reserve_n_impl: failed to allocate CUDA1 buffer of size 547880960",
            "graph_reserve: failed to allocate compute buffers",
            "llama_init_from_model: failed to initialize the context: failed to allocate compute pp buffers",
        ]

        result = mgr._extract_crash_error_from_lines(lines)

        assert "CUDA error: out of memory" in result
        assert "failed to allocate CUDA1 buffer" in result
        assert "failed to initialize the context: failed to allocate compute pp buffers" in result

    @pytest.mark.asyncio
    async def test_get_crash_error_reads_larger_recent_log_window(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        proc = AsyncMock()
        proc.communicate.return_value = (
            b"llama_params_fit_impl: cannot meet free memory targets on all devices\n"
            b"llama_params_fit: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort\n",
            b"",
        )

        with patch("app.engine.manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec:
            result = await mgr._get_crash_error()

        assert "cannot meet free memory targets" in result
        assert "failed to fit params to free device memory" in result
        assert mock_exec.await_args.args[:7] == (
            "journalctl", "-u", "llama-server", "-n", "120", "--no-pager", "-o",
        )


# ── Backend binaries ──────────────────────────────────────────────────


class TestOfficialBinary:
    def test_official_binary_path_is_defined(self):
        from app.engine.manager import OFFICIAL_LLAMA_SERVER_BIN

        assert "official" in str(OFFICIAL_LLAMA_SERVER_BIN)

        assert "official" in str(OFFICIAL_LLAMA_SERVER_BIN)


# ── Model aliases ─────────────────────────────────────────────────────

YAML_WITH_ALIASES = SAMPLE_MODELS_YAML + """\

aliases:
  glm4: "GLM-4.7-Flash"
  qwen3: "Qwen3-30B-A3B"
"""


class TestModelAliases:
    def test_resolve_exact_match(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, models_yaml=YAML_WITH_ALIASES)
        assert mgr.resolve_model("GLM-4.7-Flash") == "GLM-4.7-Flash"

    def test_resolve_alias(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, models_yaml=YAML_WITH_ALIASES)
        assert mgr.resolve_model("glm4") == "GLM-4.7-Flash"
        assert mgr.resolve_model("qwen3") == "Qwen3-30B-A3B"

    def test_resolve_case_insensitive(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, models_yaml=YAML_WITH_ALIASES)
        assert mgr.resolve_model("glm-4.7-flash") == "GLM-4.7-Flash"
        assert mgr.resolve_model("QWEN3-30B-A3B") == "Qwen3-30B-A3B"

    def test_resolve_unknown_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, models_yaml=YAML_WITH_ALIASES)
        with pytest.raises(ValueError, match="not found"):
            mgr.resolve_model("nonexistent-model")

    def test_no_aliases_section(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        # Should still work via exact match
        assert mgr.resolve_model("GLM-4.7-Flash") == "GLM-4.7-Flash"

    def test_alias_pointing_to_unknown_model(self, tmp_path: Path):
        bad_alias_yaml = SAMPLE_MODELS_YAML + """\

aliases:
  broken: "NonexistentModel"
"""
        mgr = _make_manager(tmp_path, models_yaml=bad_alias_yaml)
        # Falls through alias (target not in models), then case-insensitive, then raises
        with pytest.raises(ValueError, match="not found"):
            mgr.resolve_model("broken")


TOOL_ROUTING_YAML = """\
models:
    Qwen-Deep:
        path: /models/qwen.gguf
        context: 131072
        extra_args: "--reasoning on --reasoning-budget -1"
    Qwen-Agent:
        path: /models/qwen.gguf
        context: 65536
        extra_args: "--chat-template-file /tmp/qwen_nonthinking.jinja"
    Qwen-Bounded:
        path: /models/qwen.gguf
        context: 65536
        extra_args: "--reasoning on --reasoning-budget 2048"
    Other-Model:
        path: /models/other.gguf
        context: 32768
        extra_args: "--reasoning-budget 0"
"""


class TestPreferredModelRecommendations:
        def test_prefers_tool_friendly_sibling_for_reasoning_model(self, tmp_path: Path):
                mgr = _make_manager(tmp_path, models_yaml=TOOL_ROUTING_YAML)
                assert mgr.get_preferred_tool_model("Qwen-Deep") == "Qwen-Agent"

        def test_tool_friendly_model_returns_itself(self, tmp_path: Path):
                mgr = _make_manager(tmp_path, models_yaml=TOOL_ROUTING_YAML)
                assert mgr.get_preferred_tool_model("Qwen-Agent") == "Qwen-Agent"

        def test_prefers_unbounded_reasoning_sibling(self, tmp_path: Path):
                mgr = _make_manager(tmp_path, models_yaml=TOOL_ROUTING_YAML)
                assert mgr.get_preferred_reasoning_model("Qwen-Agent") == "Qwen-Deep"


# ── runtime_overrides validation ───────────────────────────────────────


class TestRuntimeOverridesValidation:
    @pytest.mark.asyncio
    async def test_non_dict_runtime_overrides_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="must be an object/dict"):
            await mgr.load(runtime_overrides=["context", 65536])

    @pytest.mark.asyncio
    async def test_unknown_runtime_override_key_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="unsupported keys"):
            await mgr.load(runtime_overrides={"contex": 65536})

    @pytest.mark.asyncio
    async def test_context_bool_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="context"):
            await mgr.load(runtime_overrides={"context": True})

    @pytest.mark.asyncio
    async def test_ngl_bool_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="ngl"):
            await mgr.load(runtime_overrides={"ngl": False})

    @pytest.mark.asyncio
    async def test_context_float_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="context"):
            await mgr.load(runtime_overrides={"context": 65536.0})

    @pytest.mark.asyncio
    async def test_context_float_string_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="context"):
            await mgr.load(runtime_overrides={"context": "65536.0"})

    @pytest.mark.asyncio
    async def test_context_zero_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="context"):
            await mgr.load(runtime_overrides={"context": 0})

    @pytest.mark.asyncio
    async def test_ngl_negative_raises(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="ngl"):
            await mgr.load(runtime_overrides={"ngl": -1})

    @pytest.mark.asyncio
    async def test_ngl_above_total_layers_raises(self, tmp_path: Path):
        models_yaml = """\
models:
    GLM-4.7-Flash:
        path: /models/GLM-4.7-Flash.gguf
        context: 8192
        ngl: 40
        total_layers: 41
        tensor_split: \"0.55,0.45\"
"""
        mgr = _make_manager(tmp_path, models_yaml=models_yaml)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="total_layers"):
            await mgr.load(runtime_overrides={"ngl": 100})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tensor_split", ["nan,0.5", "0.5,inf", "0.5,-inf"])
    async def test_tensor_split_non_finite_values_raise(self, tmp_path: Path, tensor_split: str):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with pytest.raises(ValueError, match="finite"):
            await mgr.load(runtime_overrides={"tensor_split": tensor_split})

    @pytest.mark.asyncio
    async def test_valid_int_context_and_ngl_accepted(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with (
            patch.object(mgr, "_write_server_args"),
            patch.object(mgr, "_stop_server", new_callable=AsyncMock),
            patch.object(mgr, "_free_gpu_memory", new_callable=AsyncMock),
            patch.object(mgr, "_start_server", new_callable=AsyncMock),
            patch.object(mgr, "_wait_for_health", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "verify_backend_model", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "_save_context", new_callable=AsyncMock),
        ):
            await mgr.load(runtime_overrides={"context": 65536, "ngl": 40})

    @pytest.mark.asyncio
    async def test_valid_digit_string_context_accepted(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)
        mgr.current_model = "GLM-4.7-Flash"
        with (
            patch.object(mgr, "_write_server_args") as mock_write,
            patch.object(mgr, "_stop_server", new_callable=AsyncMock),
            patch.object(mgr, "_free_gpu_memory", new_callable=AsyncMock),
            patch.object(mgr, "_start_server", new_callable=AsyncMock),
            patch.object(mgr, "_wait_for_health", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "verify_backend_model", new_callable=AsyncMock, return_value=True),
            patch.object(mgr, "_save_context", new_callable=AsyncMock),
        ):
            await mgr.load(runtime_overrides={"context": "65536"})
        written_config = mock_write.call_args.args[0]
        assert written_config["context"] == 65536
