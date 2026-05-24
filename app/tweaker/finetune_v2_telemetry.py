"""Finetune v2 GPU telemetry helpers.

llama.cpp is started with CUDA devices ordered by PCI bus ID. These helpers
re-key `nvidia-smi` telemetry into that same llama/CUDA order, so tensor-split
decisions survive nvidia-smi index changes after a host reboot.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional

from app.paths import CURRENT_MODEL_ARGS_FILE, CURRENT_MODEL_ENV_FILE
from app.tweaker.finetune_v2_support import format_two_gpu_split, parse_two_gpu_split


DEFAULT_MIN_FREE_MIB_FOR_COARSE_SPLIT_SHIFT = 1024.0


@dataclass(frozen=True)
class GpuIdentity:
    """Stable identity for one physical GPU."""

    nvidia_index: str
    uuid: str
    pci_bus_id: str
    total_mib: float


def _run_nvidia_smi(query_args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", *query_args, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _normalize_pci_bus_id(value: str) -> str:
    return value.strip().lower()


def _strip_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _read_configured_cuda_visible_devices() -> list[str] | None:
    value = os.getenv("CUDA_VISIBLE_DEVICES")
    if value is None and CURRENT_MODEL_ENV_FILE.exists():
        try:
            for line in CURRENT_MODEL_ENV_FILE.read_text().splitlines():
                match = re.match(r"^\s*(?:export\s+)?CUDA_VISIBLE_DEVICES\s*=\s*(.+?)\s*$", line)
                if match:
                    value = _strip_env_value(match.group(1))
        except OSError:
            value = None
    if value is None:
        return None
    devices = [part.strip() for part in value.split(",") if part.strip()]
    return devices or []


def _read_configured_cuda_device_order() -> str:
    value = os.getenv("CUDA_DEVICE_ORDER")
    if value is None and CURRENT_MODEL_ENV_FILE.exists():
        try:
            for line in CURRENT_MODEL_ENV_FILE.read_text().splitlines():
                match = re.match(r"^\s*(?:export\s+)?CUDA_DEVICE_ORDER\s*=\s*(.+?)\s*$", line)
                if match:
                    value = _strip_env_value(match.group(1))
        except OSError:
            value = None
    return (value or "PCI_BUS_ID").strip().upper()


def _read_gpu_identities() -> list[GpuIdentity] | None:
    output = _run_nvidia_smi(["--query-gpu=index,uuid,pci.bus_id,memory.total"])
    if output is None:
        return None

    identities: list[GpuIdentity] = []
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        nvidia_index, uuid, pci_bus_id, total = parts
        try:
            total_mib = float(total)
        except ValueError:
            continue
        identities.append(
            GpuIdentity(
                nvidia_index=nvidia_index,
                uuid=uuid,
                pci_bus_id=_normalize_pci_bus_id(pci_bus_id),
                total_mib=total_mib,
            )
        )
    return identities or None


def _base_cuda_ordered_gpus(identities: list[GpuIdentity]) -> list[GpuIdentity]:
    if _read_configured_cuda_device_order() == "PCI_BUS_ID":
        return sorted(identities, key=lambda gpu: gpu.pci_bus_id)
    return sorted(identities, key=lambda gpu: int(gpu.nvidia_index))


def _match_visible_device(
    token: str,
    identities: list[GpuIdentity],
    base_cuda_order: list[GpuIdentity],
) -> GpuIdentity | None:
    normalized = _normalize_pci_bus_id(token)
    for gpu in identities:
        if token == gpu.uuid or normalized == gpu.pci_bus_id:
            return gpu
    if token.isdigit():
        visible_index = int(token)
        if 0 <= visible_index < len(base_cuda_order):
            return base_cuda_order[visible_index]
    return None


def _llama_ordered_gpus(identities: list[GpuIdentity]) -> list[GpuIdentity]:
    base_cuda_order = _base_cuda_ordered_gpus(identities)
    visible_devices = _read_configured_cuda_visible_devices()
    if visible_devices is not None:
        ordered: list[GpuIdentity] = []
        for token in visible_devices:
            match = _match_visible_device(token, identities, base_cuda_order)
            if match is not None and match not in ordered:
                ordered.append(match)
        return ordered
    return base_cuda_order


def _llama_index_by_uuid(identities: list[GpuIdentity]) -> dict[str, str]:
    return {gpu.uuid: str(index) for index, gpu in enumerate(_llama_ordered_gpus(identities))}


def _llama_index_by_nvidia_index(identities: list[GpuIdentity]) -> dict[str, str]:
    return {gpu.nvidia_index: str(index) for index, gpu in enumerate(_llama_ordered_gpus(identities))}


def read_gpu_vram_snapshot() -> Optional[dict[str, dict[str, float]]]:
    """Read host VRAM telemetry keyed by llama/CUDA device index."""
    identities = _read_gpu_identities()
    if identities is None:
        return None
    index_to_llama = _llama_index_by_nvidia_index(identities)

    output = _run_nvidia_smi(["--query-gpu=index,memory.used,memory.free,memory.total"])
    if output is None:
        return None

    snapshot: dict[str, dict[str, float]] = {}
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        nvidia_index, used, free, total = parts
        llama_index = index_to_llama.get(nvidia_index)
        if llama_index is None:
            continue
        try:
            used_value = float(used)
            free_value = float(free)
            total_value = float(total)
        except ValueError:
            continue
        snapshot[llama_index] = {
            "used": used_value,
            "free": free_value,
            "total": total_value,
            "free_pct": (free_value / total_value * 100.0) if total_value > 0 else 0.0,
            "nvidia_index": float(nvidia_index),
        }
    return dict(sorted(snapshot.items(), key=lambda item: int(item[0]))) or None


def read_backend_gpu_vram_snapshot(process_name: str = "llama-server") -> Optional[dict[str, dict[str, float]]]:
    """Read active llama-server VRAM keyed by llama/CUDA device index."""
    try:
        pid_result = subprocess.run(
            ["pgrep", "-x", process_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if pid_result.returncode != 0:
        return None

    pids = {line.strip() for line in pid_result.stdout.splitlines() if line.strip().isdigit()}
    if not pids:
        return None

    identities = _read_gpu_identities()
    if identities is None:
        return None
    uuid_to_llama = _llama_index_by_uuid(identities)
    total_by_llama = {uuid_to_llama[gpu.uuid]: gpu.total_mib for gpu in identities if gpu.uuid in uuid_to_llama}

    apps_output = _run_nvidia_smi(["--query-compute-apps=gpu_uuid,pid,used_gpu_memory"])
    if apps_output is None:
        return None

    used_by_llama: dict[str, float] = {}
    for line in apps_output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        gpu_uuid, pid, used = parts
        if pid not in pids:
            continue
        llama_index = uuid_to_llama.get(gpu_uuid)
        if llama_index is None:
            continue
        try:
            used_by_llama[llama_index] = used_by_llama.get(llama_index, 0.0) + float(used)
        except ValueError:
            continue

    if not used_by_llama:
        return None

    snapshot: dict[str, dict[str, float]] = {}
    for llama_index, used_value in sorted(used_by_llama.items(), key=lambda item: int(item[0])):
        total_value = total_by_llama.get(llama_index)
        if total_value is None:
            continue
        free_value = max(total_value - used_value, 0.0)
        snapshot[llama_index] = {
            "used": used_value,
            "free": free_value,
            "total": total_value,
            "used_pct": (used_value / total_value * 100.0) if total_value > 0 else 0.0,
            "free_pct": (free_value / total_value * 100.0) if total_value > 0 else 0.0,
        }
    return snapshot or None


def read_current_tensor_split_arg() -> Optional[str]:
    """Read the effective --tensor-split value from current_model.args when present."""
    try:
        args = CURRENT_MODEL_ARGS_FILE.read_text()
    except OSError:
        return None
    match = re.search(r"(?:^|\s)--tensor-split\s+([^\s]+)", args)
    if match is None:
        return None
    return match.group(1).strip() or None


def free_vram_delta_pct(gpu_vram: Optional[Mapping[str, Mapping[str, float]]]) -> Optional[float]:
    """Return the absolute free-VRAM percentage difference across llama CUDA0 and CUDA1."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    first = gpu_vram.get("0", {}).get("free_pct")
    second = gpu_vram.get("1", {}).get("free_pct")
    if first is None or second is None:
        return None
    return abs(float(first) - float(second))


def next_split_from_vram_balance(
    tensor_split: Optional[str],
    *,
    gpu_vram: Optional[Mapping[str, Mapping[str, float]]],
    step: float,
    split_min: float,
    split_max: float,
) -> Optional[str]:
    """Shift split toward the llama/CUDA device with more free VRAM."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    first_free = float(gpu_vram.get("0", {}).get("free_pct", 0.0))
    second_free = float(gpu_vram.get("1", {}).get("free_pct", 0.0))
    if math.isclose(first_free, second_free, abs_tol=0.01):
        return None

    current_primary = parse_two_gpu_split(tensor_split) or 0.5
    direction = 1 if first_free > second_free else -1
    candidate_primary = round(current_primary + (direction * step), 2)
    if not split_min <= candidate_primary <= split_max:
        return None
    candidate_split = format_two_gpu_split(candidate_primary)
    if candidate_split == tensor_split:
        return None
    return candidate_split


def target_gpu_free_mib_for_balance_shift(gpu_vram: Optional[Mapping[str, Mapping[str, float]]]) -> Optional[float]:
    """Return free MiB on the llama/CUDA device that would receive more load."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    first = gpu_vram.get("0", {})
    second = gpu_vram.get("1", {})
    first_free_pct = first.get("free_pct")
    second_free_pct = second.get("free_pct")
    if first_free_pct is None or second_free_pct is None:
        return None
    target = first if float(first_free_pct) > float(second_free_pct) else second
    total_mib = target.get("total")
    if total_mib is None or float(total_mib) < 2048.0:
        return None
    free_mib = target.get("free")
    return float(free_mib) if free_mib is not None else None


def two_gpu_free_mib(gpu_vram: Optional[Mapping[str, Mapping[str, float]]]) -> Optional[tuple[float, float]]:
    """Return real MiB headroom for llama/CUDA0 and CUDA1."""
    if not gpu_vram or len(gpu_vram) < 2:
        return None
    values: list[float] = []
    for llama_index in ("0", "1"):
        gpu_stats = gpu_vram.get(llama_index)
        if gpu_stats is None:
            return None
        total_mib = gpu_stats.get("total")
        free_mib = gpu_stats.get("free")
        if total_mib is None or free_mib is None or float(total_mib) < 2048.0:
            return None
        values.append(float(free_mib))
    return values[0], values[1]


def should_skip_coarse_split_shift(
    gpu_vram: Optional[Mapping[str, Mapping[str, float]]],
    *,
    step: float,
    min_free_mib: float = DEFAULT_MIN_FREE_MIB_FOR_COARSE_SPLIT_SHIFT,
) -> bool:
    """Return True when the target llama/CUDA device has too little free VRAM."""
    if step < 0.02:
        return False
    target_free_mib = target_gpu_free_mib_for_balance_shift(gpu_vram)
    if target_free_mib is None:
        return False
    return target_free_mib < min_free_mib