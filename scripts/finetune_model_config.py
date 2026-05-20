#!/usr/bin/env python3
"""Fast Guardian-native model finetuning CLI.

This script binary-searches the highest stable `context` and explores `ngl`
plus two-GPU `tensor_split` candidates around the current model config. It
talks to Guardian directly, so the recommendation matches real `models.yaml`
behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from _paths import CONFIG_DIR, DATA_DIR
from app.tweaker.model_finetune import GuardianModelFinetuner


def resolve_api_key(explicit_key: Optional[str]) -> str:
    """Resolve the Guardian API key from CLI or config/api_keys.json."""
    if explicit_key:
        return explicit_key
    api_keys_path = CONFIG_DIR / "api_keys.json"
    if api_keys_path.exists():
        keys = json.loads(api_keys_path.read_text())
        if keys:
            return next(iter(keys))
    raise SystemExit("No Guardian API key found. Use --api-key or populate config/api_keys.json.")


def parse_args() -> argparse.Namespace:
    """Parse finetune CLI arguments."""
    parser = argparse.ArgumentParser(description="Find the best context, ngl, and tensor split for a Guardian model.")
    parser.add_argument("model", help="Canonical model name or configured alias from models.yaml")
    parser.add_argument("--guardian-url", default="http://127.0.0.1:11434", help="Guardian base URL")
    parser.add_argument("--api-key", default=None, help="Guardian bearer token")
    parser.add_argument("--models-config", default=str(CONFIG_DIR / "models.yaml"), help="Path to models.yaml")
    parser.add_argument(
        "--results-file",
        default=str(DATA_DIR / "model_finetune_results.json"),
        help="JSON file that stores finetune history",
    )
    parser.add_argument("--min-context", type=int, default=None, help="Lower search bound for runtime context")
    parser.add_argument("--max-context", type=int, default=None, help="Upper search bound for runtime context")
    parser.add_argument("--granularity", type=int, default=2048, help="Context search step size")
    parser.add_argument(
        "--auto-context-range",
        action="store_true",
        help="Derive effective context bounds automatically from the current runtime config and benchmark ceiling",
    )
    parser.add_argument(
        "--auto-context-floor-ratio",
        type=float,
        default=0.5,
        help="When auto context range is active, start from this fraction of the current runtime context",
    )
    parser.add_argument("--min-ngl", type=int, default=None, help="Lower bound for auto ngl search (default: current ngl)")
    parser.add_argument("--max-ngl", type=int, default=None, help="Upper bound for auto ngl search (default: 99)")
    parser.add_argument("--ngl-step", type=int, default=16, help="Primary coarse ngl step")
    parser.add_argument("--ngl-refine-step", type=int, default=8, help="Refine ngl step around the best coarse result")
    parser.add_argument(
        "--ngl",
        action="append",
        dest="ngl_candidates",
        type=int,
        default=[],
        help="Explicit ngl candidate. Repeat to test multiple values.",
    )
    parser.add_argument("--coarse-step", type=float, default=0.05, help="Primary coarse tensor-split step")
    parser.add_argument("--refine-step", type=float, default=0.02, help="Primary refine tensor-split step")
    parser.add_argument("--split-min", type=float, default=0.35, help="Minimum primary GPU share to test")
    parser.add_argument("--split-max", type=float, default=0.65, help="Maximum primary GPU share to test")
    parser.add_argument(
        "--split",
        action="append",
        dest="split_candidates",
        default=[],
        help="Explicit tensor split candidate such as 0.55,0.45. Repeat to test multiple values.",
    )
    parser.add_argument("--include-auto-split", action="store_true", help="Also test removing tensor_split entirely")
    parser.add_argument("--apply", action="store_true", help="Write the winning context/tensor_split back to models.yaml")
    parser.add_argument(
        "--keep-loaded-model",
        action="store_true",
        help="Do not restore the previously loaded Guardian model after a dry run",
    )
    parser.add_argument("--smoke-prompt", default="Reply with exactly: FIT OK", help="Short post-load smoke prompt")
    parser.add_argument("--smoke-image-url", default=None, help="Optional image URL to force multimodal smoke probes")
    parser.add_argument("--smoke-max-tokens", type=int, default=8, help="Max tokens for the smoke request")
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")
    return parser.parse_args()


def main() -> int:
    """Run the finetune search and print the recommendation."""
    args = parse_args()
    api_key = resolve_api_key(args.api_key)
    finetuner = GuardianModelFinetuner(
        guardian_url=args.guardian_url,
        api_key=api_key,
        models_config_path=args.models_config,
        results_file=args.results_file,
        smoke_prompt=args.smoke_prompt,
        smoke_max_tokens=args.smoke_max_tokens,
        smoke_image_url=args.smoke_image_url,
    )
    try:
        result = finetuner.tune_model(
            args.model,
            min_context=args.min_context,
            max_context=args.max_context,
            granularity=args.granularity,
            auto_context_range=args.auto_context_range,
            auto_context_floor_ratio=args.auto_context_floor_ratio,
            ngl_candidates=args.ngl_candidates or None,
            min_ngl=args.min_ngl,
            max_ngl=args.max_ngl,
            ngl_step=args.ngl_step,
            ngl_refine_step=args.ngl_refine_step,
            split_candidates=args.split_candidates or None,
            coarse_step=args.coarse_step,
            refine_step=args.refine_step,
            split_min=args.split_min,
            split_max=args.split_max,
            include_auto_split=args.include_auto_split,
            apply=args.apply,
            restore_loaded_model=not args.keep_loaded_model,
        )
    finally:
        finetuner.close()

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print(f"Model: {result.model}")
    print(f"Original context: {result.original_context}")
    print(f"Original ngl: {result.original_ngl}")
    print(f"Original tensor_split: {result.original_tensor_split or 'auto'}")
    print(f"Effective context range: {result.search_min_context}-{result.search_max_context}")
    print(f"Recommended context: {result.recommended_context}")
    print(f"Recommended ngl: {result.recommended_ngl}")
    print(f"Recommended tensor_split: {result.recommended_tensor_split or 'auto'}")
    print(f"Applied to models.yaml: {'yes' if result.applied else 'no'}")
    print(f"Attempts: {len(result.attempts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())