"""Finetune v2 support helpers.

These helpers intentionally live outside the deprecated v1 finetune module so
the v2 runner can evolve without keeping legacy code on its hot path.
"""

from __future__ import annotations

import copy
import re
from typing import Mapping, Optional


def detect_oom_gpu(error: Optional[str]) -> Optional[int]:
    """Infer which GPU hit OOM from a Guardian/llama.cpp error string."""
    if not error:
        return None
    for pattern in (r"CUDA([01])", r"device\s+([01])"):
        match = re.search(pattern, error, flags=re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
    return None


def build_smoke_messages(smoke_prompt: str, smoke_image_url: Optional[str] = None) -> list[dict[str, object]]:
    """Build the minimal post-load smoke-test message list."""
    if smoke_image_url:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": smoke_image_url}},
                    {"type": "text", "text": smoke_prompt},
                ],
            }
        ]
    return [{"role": "user", "content": smoke_prompt}]


def resolve_runtime_mode(runtime_mode: str, smoke_image_url: Optional[str]) -> str:
    """Resolve `auto` finetune mode to the effective text or vision runtime."""
    normalized = runtime_mode.strip().lower()
    if normalized == "auto":
        return "vision" if smoke_image_url else "text"
    if normalized not in {"text", "vision"}:
        raise ValueError("runtime_mode must be one of: auto, text, vision")
    return normalized


def runtime_mode_uses_vision(runtime_mode: str) -> bool:
    """Return whether the finetune run targets Guardian's vision runtime."""
    return runtime_mode == "vision"


def has_vision_runtime(model_config: Mapping[str, object]) -> bool:
    """Return whether the model has an mmproj-backed vision path."""
    mmproj = str(model_config.get("vision_mmproj") or model_config.get("mmproj") or "").strip()
    return bool(mmproj)


def resolve_runtime_config_value(model_config: Mapping[str, object], key: str, runtime_mode: str) -> object:
    """Return the effective config value for the requested finetune runtime."""
    override_key = f"{runtime_mode}_{key}"
    override_value = model_config.get(override_key)
    if override_value not in (None, ""):
        return override_value
    return model_config.get(key)


def resolve_runtime_total_layers(model_config: Mapping[str, object], runtime_mode: str) -> Optional[int]:
    """Return the configured main-model layer ceiling for `ngl` search."""
    value = resolve_runtime_config_value(model_config, "total_layers", runtime_mode)
    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def apply_runtime_search_values(
    model_config: Mapping[str, object],
    *,
    context: int,
    ngl: int,
    tensor_split: Optional[str],
    runtime_mode: str,
) -> dict[str, object]:
    """Apply tuned fields to the correct text or vision config keys."""
    target = copy.deepcopy(dict(model_config))
    if runtime_mode_uses_vision(runtime_mode) and has_vision_runtime(target):
        prefix = "vision"
    elif runtime_mode == "text" and any(
        target.get(f"text_{field}") not in (None, "") for field in ("context", "ngl", "tensor_split")
    ):
        prefix = "text"
    else:
        prefix = None

    if prefix is None:
        target["context"] = int(context)
        target["ngl"] = int(ngl)
        if tensor_split:
            target["tensor_split"] = tensor_split
        else:
            target.pop("tensor_split", None)
        return target

    target[f"{prefix}_context"] = int(context)
    target[f"{prefix}_ngl"] = int(ngl)
    if tensor_split:
        target[f"{prefix}_tensor_split"] = tensor_split
    else:
        target.pop(f"{prefix}_tensor_split", None)
    return target


def parse_two_gpu_split(tensor_split: Optional[str]) -> Optional[float]:
    """Return the primary-GPU ratio from a two-GPU tensor split string."""
    if not tensor_split:
        return None
    parts = [part.strip() for part in tensor_split.split(",") if part.strip()]
    if len(parts) != 2:
        return None
    try:
        primary = float(parts[0])
        secondary = float(parts[1])
    except ValueError:
        return None
    total = primary + secondary
    if total <= 0:
        return None
    return round(primary / total, 4)


def format_two_gpu_split(primary_ratio: float, decimals: int = 2) -> str:
    """Format a normalized two-GPU tensor split string."""
    bounded_primary = min(max(primary_ratio, 0.0), 1.0)
    rounded_primary = round(bounded_primary, decimals)
    rounded_secondary = round(max(0.0, 1.0 - rounded_primary), decimals)
    return f"{rounded_primary:.{decimals}f},{rounded_secondary:.{decimals}f}"


def build_split_candidates(
    anchor_split: Optional[str],
    step: float,
    min_primary: float,
    max_primary: float,
    *,
    include_auto: bool = False,
) -> list[Optional[str]]:
    """Build ordered two-GPU tensor split candidates, preferring balanced splits first."""
    if step <= 0:
        raise ValueError("step must be > 0")
    if min_primary <= 0 or max_primary >= 1 or min_primary > max_primary:
        raise ValueError("split bounds must satisfy 0 < min <= max < 1")

    anchor_primary = parse_two_gpu_split(anchor_split)
    if anchor_primary is None:
        anchor_primary = 0.55

    values: set[float] = {round(anchor_primary, 2)}
    current = min_primary
    while current <= max_primary + 1e-9:
        values.add(round(current, 2))
        current += step

    ordered = sorted(
        values,
        key=lambda value: (
            round(abs(value - 0.5), 4),
            round(abs(value - anchor_primary), 4),
            value,
        ),
    )
    candidates: list[Optional[str]] = [format_two_gpu_split(value) for value in ordered]
    if include_auto:
        return [*candidates, None]
    return candidates


def smaller_split_step(step: float) -> Optional[float]:
    """Return the next smaller split step, down to a 1% minimum increment."""
    if step <= 0.01:
        return None
    halved = round(step / 2.0, 2)
    if halved >= step:
        return None
    return max(0.01, halved)


def _format_yaml_scalar(value: object) -> str:
    """Format a scalar for the hand-written model block renderer."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_model_block(model_name: str, model_config: Mapping[str, object]) -> str:
    """Render a single `models.yaml` model block while preserving key order."""
    lines = [f"  {model_name}:"]
    for key, value in model_config.items():
        lines.append(f"    {key}: {_format_yaml_scalar(value)}")
    return "\n".join(lines)


def replace_model_block(config_text: str, model_name: str, replacement_block: str) -> str:
    """Replace exactly one model block inside `models.yaml`."""
    lines = config_text.splitlines()
    start: Optional[int] = None
    end: Optional[int] = None
    header = f"  {model_name}:"
    for index, line in enumerate(lines):
        if line == header:
            start = index
            break
    if start is None:
        raise ValueError(f"Model block '{model_name}' not found")
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line and not line.startswith("    "):
            break
        end += 1
    new_lines = lines[:start] + replacement_block.splitlines() + lines[end:]
    suffix = "\n" if config_text.endswith("\n") else ""
    return "\n".join(new_lines) + suffix

