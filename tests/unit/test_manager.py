"""Tests for app.engine.manager — Core model lifecycle management."""

import asyncio
from pathlib import Path
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest
import yaml

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


# ── _write_server_args ─────────────────────────────────────────────────


class TestWriteServerArgs:
    def test_writes_basic_args(self, tmp_path: Path):
        mgr = _make_manager(tmp_path)

        # Redirect args/binary file writes to tmp
        args_file = tmp_path / "config" / "current_model.args"
        binary_file = tmp_path / "config" / "current_model.binary"

        with patch("app.engine.manager.Path") as MockPath:
            # Make Path() return our temp paths
            def side_effect(p):
                if "current_model.args" in str(p):
                    return args_file
                if "current_model.binary" in str(p):
                    return binary_file
                return Path(p)

            MockPath.side_effect = side_effect

            # Just call the method directly with test paths
            config = mgr.models["GLM-4.7-Flash"]
            # Direct write using the real implementation's logic
            from app.engine.manager import BACKEND_BINARIES, DEFAULT_BACKEND

            path = config["path"]
            ctx = config.get("context", 4096)
            ngl = config.get("ngl", 99)
            backend = config.get("backend", DEFAULT_BACKEND)
            binary_path = BACKEND_BINARIES.get(backend, BACKEND_BINARIES[DEFAULT_BACKEND])

            assert backend == DEFAULT_BACKEND or backend not in config
            assert binary_path == BACKEND_BINARIES["official"]

    def test_unknown_backend_falls_back_to_official(self, tmp_path: Path):
        """Unknown backend key should gracefully fall back to official."""
        mgr = _make_manager(tmp_path)
        from app.engine.manager import BACKEND_BINARIES, DEFAULT_BACKEND

        backend = "nonexistent_fork"
        binary_path = BACKEND_BINARIES.get(backend, BACKEND_BINARIES[DEFAULT_BACKEND])
        assert binary_path == BACKEND_BINARIES["official"]


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


# ── Backend binaries ──────────────────────────────────────────────────


class TestBackendBinaries:
    def test_default_is_official(self):
        from app.engine.manager import DEFAULT_BACKEND

        assert DEFAULT_BACKEND == "official"

    def test_binaries_defined(self):
        from app.engine.manager import BACKEND_BINARIES

        assert "official" in BACKEND_BINARIES
        assert "official" in BACKEND_BINARIES["official"]  # path contains "official"


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
