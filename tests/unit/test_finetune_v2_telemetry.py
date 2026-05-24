"""Tests for finetune v2 GPU telemetry mapping."""

from types import SimpleNamespace

from app.tweaker import finetune_v2_telemetry as telemetry


GPU_IDENTITIES = """\
1, GPU-B, 00000000:01:00.0, 12288
0, GPU-A, 00000000:02:00.0, 16384
"""

GPU_MEMORY = """\
0, 4096, 12288, 16384
1, 2048, 10240, 12288
"""

GPU_APPS = """\
GPU-A, 1234, 9000
GPU-B, 1234, 3000
GPU-B, 7777, 2000
"""


def _completed(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_read_gpu_vram_snapshot_maps_smi_indices_to_llama_pci_order(monkeypatch, tmp_path):
    """nvidia-smi index changes must not change llama/CUDA telemetry order."""
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.setattr(telemetry, "CURRENT_MODEL_ENV_FILE", tmp_path / "missing.env")

    def fake_run(command, **_kwargs):
        if "--query-gpu=index,uuid,pci.bus_id,memory.total" in command:
            return _completed(GPU_IDENTITIES)
        if "--query-gpu=index,memory.used,memory.free,memory.total" in command:
            return _completed(GPU_MEMORY)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    snapshot = telemetry.read_gpu_vram_snapshot()

    assert snapshot is not None
    assert snapshot["0"]["nvidia_index"] == 1.0
    assert snapshot["0"]["used"] == 2048.0
    assert snapshot["1"]["nvidia_index"] == 0.0
    assert snapshot["1"]["used"] == 4096.0


def test_backend_vram_snapshot_maps_compute_apps_by_uuid_to_llama_order(monkeypatch, tmp_path):
    """llama-server process telemetry should follow UUID/PCI identity, not smi index."""
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.setattr(telemetry, "CURRENT_MODEL_ENV_FILE", tmp_path / "missing.env")

    def fake_run(command, **_kwargs):
        if command[:2] == ["pgrep", "-x"]:
            return _completed("1234\n")
        if "--query-gpu=index,uuid,pci.bus_id,memory.total" in command:
            return _completed(GPU_IDENTITIES)
        if "--query-compute-apps=gpu_uuid,pid,used_gpu_memory" in command:
            return _completed(GPU_APPS)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    snapshot = telemetry.read_backend_gpu_vram_snapshot()

    assert snapshot is not None
    assert snapshot["0"]["used"] == 3000.0
    assert snapshot["0"]["total"] == 12288.0
    assert snapshot["1"]["used"] == 9000.0
    assert snapshot["1"]["total"] == 16384.0


def test_cuda_visible_devices_overrides_pci_order(monkeypatch, tmp_path):
    """Explicit CUDA_VISIBLE_DEVICES order should define llama ordinal order."""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-A,GPU-B")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.setattr(telemetry, "CURRENT_MODEL_ENV_FILE", tmp_path / "missing.env")

    def fake_run(command, **_kwargs):
        if "--query-gpu=index,uuid,pci.bus_id,memory.total" in command:
            return _completed(GPU_IDENTITIES)
        if "--query-gpu=index,memory.used,memory.free,memory.total" in command:
            return _completed(GPU_MEMORY)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    snapshot = telemetry.read_gpu_vram_snapshot()

    assert snapshot is not None
    assert snapshot["0"]["nvidia_index"] == 0.0
    assert snapshot["1"]["nvidia_index"] == 1.0


def test_numeric_cuda_visible_devices_uses_pci_order(monkeypatch, tmp_path):
    """Numeric CUDA_VISIBLE_DEVICES values follow CUDA PCI order, not smi index order."""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.setattr(telemetry, "CURRENT_MODEL_ENV_FILE", tmp_path / "missing.env")

    def fake_run(command, **_kwargs):
        if "--query-gpu=index,uuid,pci.bus_id,memory.total" in command:
            return _completed(GPU_IDENTITIES)
        if "--query-gpu=index,memory.used,memory.free,memory.total" in command:
            return _completed(GPU_MEMORY)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    snapshot = telemetry.read_gpu_vram_snapshot()

    assert snapshot is not None
    assert snapshot["0"]["nvidia_index"] == 1.0
    assert snapshot["1"]["nvidia_index"] == 0.0
