#!/usr/bin/env python3
"""Run Guardian finetune v2 against live `/admin/load` runtime overrides."""

from __future__ import annotations

import argparse
import json
from typing import Optional, Sequence

from _paths import CONFIG_DIR, DATA_DIR
from app.tweaker.finetune_v2_runner import (
    DEFAULT_V2_RESULTS_FILE,
    FinetuneV2Runner,
    GuardianV2ProbeRunner,
)


def resolve_api_key(explicit_key: Optional[str]) -> str:
    if explicit_key:
        return explicit_key
    api_keys_path = CONFIG_DIR / "api_keys.json"
    if api_keys_path.exists():
        keys = json.loads(api_keys_path.read_text())
        if keys:
            return next(iter(keys))
        raise SystemExit("config/api_keys.json exists but contains no Guardian API keys. Use --api-key or add one.")
    raise SystemExit("No Guardian API key found. Use --api-key or create config/api_keys.json.")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="Canonical model name or configured alias from models.yaml")
    parser.add_argument("--guardian-url", default="http://127.0.0.1:11434", help="Guardian base URL")
    parser.add_argument("--api-key", default=None, help="Guardian bearer token")
    parser.add_argument("--models-config", default=str(CONFIG_DIR / "models.yaml"), help="Path to models.yaml")
    parser.add_argument(
        "--results-file",
        default=str(DATA_DIR / DEFAULT_V2_RESULTS_FILE.split("/", 1)[1]),
        help="Dedicated JSON history file for finetune v2 probes",
    )
    parser.add_argument(
        "--optimization",
        choices=["speed", "context", "balanced"],
        default="context",
        help="Mode-aware v2 winner comparator",
    )
    parser.add_argument("--context", type=int, default=None, help="Pin context and tune only ngl/split")
    parser.add_argument("--ngl", type=int, default=None, help="Pin ngl and tune only context/split")
    parser.add_argument("--ngl-step", type=int, default=1, help="Step size for v2 ngl follow-up candidates")
    parser.add_argument("--split-min", type=float, default=0.30, help="Minimum primary GPU share to test")
    parser.add_argument("--split-max", type=float, default=0.70, help="Maximum primary GPU share to test")
    parser.add_argument(
        "--split",
        action="append",
        dest="split_candidates",
        default=[],
        help="Explicit tensor split candidate such as 0.55,0.45. Repeat to test multiple values.",
    )
    parser.add_argument("--apply", action="store_true", help="Write the v2 winner back to models.yaml")
    parser.add_argument("--smoke-prompt", default="Reply with exactly: FIT OK", help="Short post-load smoke prompt")
    parser.add_argument("--smoke-image-url", default=None, help="Optional image URL to force multimodal smoke probes")
    parser.add_argument("--smoke-max-tokens", type=int, default=8, help="Max tokens for the smoke request")
    parser.add_argument(
        "--runtime-mode",
        choices=["auto", "text", "vision"],
        default="auto",
        help="Tune text, vision, or resolve automatically from --smoke-image-url",
    )
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    probe_runner = GuardianV2ProbeRunner(
        guardian_url=args.guardian_url,
        api_key=resolve_api_key(args.api_key),
        smoke_prompt=args.smoke_prompt,
        smoke_max_tokens=args.smoke_max_tokens,
        smoke_image_url=args.smoke_image_url,
    )
    runner = FinetuneV2Runner(
        models_config_path=args.models_config,
        results_file=args.results_file,
        probe_runner=probe_runner,
        runtime_mode=args.runtime_mode,
    )
    try:
        result = runner.tune_model(
            args.model,
            optimization=args.optimization,
            fixed_context=args.context,
            fixed_ngl=args.ngl,
            split_candidates=args.split_candidates or None,
            ngl_step=args.ngl_step,
            split_min=args.split_min,
            split_max=args.split_max,
            apply=args.apply,
        )
    finally:
        probe_runner.close()

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    winner = result.winner.candidate
    print(f"Model: {result.model}")
    print(f"Runtime mode: {result.runtime_mode}")
    print(f"Optimization: {result.optimization}")
    print(f"Winner context: {winner.context}")
    print(f"Winner ngl: {winner.ngl}")
    print(f"Winner tensor_split: {winner.tensor_split}")
    print(f"Convergence: {result.convergence['reason']}")
    print(f"Winner reason: {result.winner_explanation['winner_reason']['code']}")
    print(f"Applied to models.yaml: {'yes' if result.applied else 'no'}")
    print(f"Results file: {result.results_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
