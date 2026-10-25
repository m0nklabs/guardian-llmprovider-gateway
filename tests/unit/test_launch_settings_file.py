"""Phase-B launch-settings file adoption (2026-10-25).

The caretaker owns model settings; the guardian registry keeps identity
(canonical names, ``brand``, aliases) and adopts launch/capability fields
from the caretaker's models file at read time via the registry document's
``launch_settings_file`` pointer. These tests pin the merge contract:
absent keys are adopted, explicit registry values win, a missing source
file is a no-op, and a source-file edit invalidates the parse cache.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.local_inference.model_registry import ModelRegistry

REGISTRY_YAML = """\
launch_settings_file: {source}
models:
  qwen3.8-27b:
    brand: qwen
  phi4:
    brand: phi
    context: 4096
aliases:
  phi: phi4
"""

LAUNCH_YAML = """\
models:
  qwen3.8-27b:
    path: /tmp/models/qwen3.8-27b.gguf
    context: 145000
    kv_type: turbo4
  phi4:
    path: /tmp/models/phi4.gguf
    context: 999999
  ghost-model:
    path: /tmp/models/ghost.gguf
"""


def test_launch_settings_merge_fills_absent_fields(tmp_path: Path):
    registry_file = tmp_path / "reg.settings.yaml"
    launch_file = tmp_path / "launch.settings.yaml"
    launch_file.write_text(LAUNCH_YAML)
    registry_file.write_text(REGISTRY_YAML.format(source=launch_file))

    reg = ModelRegistry(str(registry_file))

    qwen = reg.models["qwen3.8-27b"]
    assert qwen["path"] == "/tmp/models/qwen3.8-27b.gguf"
    assert qwen["context"] == 145000
    assert qwen["kv_type"] == "turbo4"
    assert qwen["brand"] == "qwen"
    # ghost-model lives only in the launch source: identity stays registry-owned.
    assert "ghost-model" not in reg.models


def test_launch_settings_merge_explicit_registry_value_wins(tmp_path: Path):
    registry_file = tmp_path / "reg.settings.yaml"
    launch_file = tmp_path / "launch.settings.yaml"
    launch_file.write_text(LAUNCH_YAML)
    registry_file.write_text(REGISTRY_YAML.format(source=launch_file))

    reg = ModelRegistry(str(registry_file))

    # The registry pins context: 4096; the launch source says 999999.
    assert reg.models["phi4"]["context"] == 4096
    # Still adopted: the fields the registry does not carry.
    assert reg.models["phi4"]["path"] == "/tmp/models/phi4.gguf"


def test_launch_settings_merge_missing_source_is_noop(tmp_path: Path):
    registry_file = tmp_path / "reg.settings.yaml"
    registry_file.write_text(REGISTRY_YAML.format(source=tmp_path / "absent.yaml"))

    reg = ModelRegistry(str(registry_file))

    assert reg.models["qwen3.8-27b"] == {"brand": "qwen"}


def test_launch_settings_cache_tracks_source_mtime(tmp_path: Path):
    registry_file = tmp_path / "reg.settings.yaml"
    launch_file = tmp_path / "launch.settings.yaml"
    launch_file.write_text(LAUNCH_YAML)
    registry_file.write_text(REGISTRY_YAML.format(source=launch_file))

    reg = ModelRegistry(str(registry_file))
    assert reg.models["qwen3.8-27b"]["context"] == 145000

    time.sleep(0.02)  # distinct mtime (ns clock still can jump: assert below)
    launch_file.write_text(
        LAUNCH_YAML.replace("context: 145000", "context: 200000")
    )

    reg._refresh_model_registry()
    assert reg.models["qwen3.8-27b"]["context"] == 200000
