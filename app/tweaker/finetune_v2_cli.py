"""Command-line entrypoint for Guardian finetune v2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

import yaml

from app.paths import DATA_DIR, guardian_apikeys_file, local_models_file
from app.tweaker.finetune_v2_runner import (
    DEFAULT_V2_RESULTS_FILE,
    FinetuneV2Runner,
    GuardianV2ProbeRunner,
)


def resolve_api_key(explicit_key: Optional[str]) -> str:
    """Resolve the Guardian bearer token from CLI input or the key store."""
    if explicit_key:
        return explicit_key
    keys_path = guardian_apikeys_file()
    if keys_path.exists():
        keys = yaml.safe_load(keys_path.read_text()) or {}
        if isinstance(keys, dict) and keys:
            return next(iter(keys))
        raise SystemExit(f"{keys_path.name} exists but contains no Guardian API keys. Use --api-key or add one.")
    raise SystemExit("No Guardian API key found. Use --api-key or create the Guardian key file.")


def _load_model_catalog(models_config: str | Path) -> tuple[list[str], dict[str, str]]:
    try:
        config = yaml.safe_load(Path(models_config).read_text()) or {}
    except OSError:
        return [], {}
    if not isinstance(config, dict):
        return [], {}
    models = config.get("models", {})
    aliases = config.get("aliases", {})
    model_names = sorted(models.keys()) if isinstance(models, dict) else []
    alias_map = {str(key): str(value) for key, value in aliases.items()} if isinstance(aliases, dict) else {}
    return model_names, dict(sorted(alias_map.items()))


def _print_catalog(models_config: str | Path, *, limit: int = 40) -> None:
    models, aliases = _load_model_catalog(models_config)
    print("\nAvailable models:")
    if not models:
        print(f"  (none found in {models_config})")
    else:
        for model in models[:limit]:
            print(f"  {model}")
        if len(models) > limit:
            print(f"  ... {len(models) - limit} more")

    print("\nAliases:")
    if not aliases:
        print("  (none)")
    else:
        for alias, target in list(aliases.items())[:limit]:
            print(f"  {alias} -> {target}")
        if len(aliases) > limit:
            print(f"  ... {len(aliases) - limit} more")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse finetune v2 CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run Guardian finetune v2 against live /admin/load runtime overrides.",
        epilog=(
            "Examples:\n"
            "  ./finetune_v2.py qwen3.6-35b-uncensored --context 262144 --start-ngl 37\n"
            "  ./finetune_v2.py Qwen3.6-35B-A3B-HauhauCS-Aggressive --runtime-mode vision --smoke-image-url data:image/png;base64,...\n"
            "  ./finetune_v2.py qwen3.6-35b-uncensored --apply"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("model", nargs="?", help="Canonical model name or configured alias from models.yaml")
    parser.add_argument("--list-models", action="store_true", help="Show configured models and aliases, then exit")
    parser.add_argument("--guardian-url", default="http://127.0.0.1:11434", help="Guardian base URL")
    parser.add_argument("--api-key", default=None, help="Guardian bearer token")
    parser.add_argument("--models-config", default=str(local_models_file()), help="Path to the local model registry")
    parser.add_argument(
        "--results-file",
        default=str(DATA_DIR / Path(DEFAULT_V2_RESULTS_FILE).name),
        help="Canonical JSON history file for finetune v2 probes; avoid ad-hoc per-run files unless debugging a specific case",
    )
    parser.add_argument(
        "--optimization",
        choices=["speed", "context", "balanced"],
        default="context",
        help="Mode-aware v2 winner comparator",
    )
    parser.add_argument("--context", type=int, default=None, help="Pin context and tune only ngl/split")
    parser.add_argument("--ngl", type=int, default=None, help="Pin ngl and tune only context/split")
    parser.add_argument("--start-ngl", type=int, default=None, help="Seed the ladder search at this ngl and climb upward after each balanced success")
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
    args = parser.parse_args(argv)
    setattr(args, "_parser", parser)
    return args


def validate_args(args: argparse.Namespace) -> None:
    """Validate finetune v2 CLI arguments before any Guardian calls."""
    if args.context is not None and args.context <= 0:
        raise SystemExit("--context must be > 0")
    if args.ngl is not None and args.ngl < 0:
        raise SystemExit("--ngl must be >= 0")
    if args.start_ngl is not None and args.start_ngl < 0:
        raise SystemExit("--start-ngl must be >= 0")
    if args.ngl is not None and args.start_ngl is not None:
        raise SystemExit("--ngl and --start-ngl are mutually exclusive")
    if args.ngl_step <= 0:
        raise SystemExit("--ngl-step must be > 0")
    if not 0 < args.split_min <= args.split_max < 1:
        raise SystemExit("--split-min/--split-max must satisfy 0 < split_min <= split_max < 1")
    if args.runtime_mode == "vision" and not args.smoke_image_url:
        raise SystemExit("--runtime-mode vision requires --smoke-image-url to exercise the multimodal path")


def _print_result(result) -> None:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the finetune v2 CLI."""
    args = parse_args(argv)
    parser = args._parser
    if args.list_models or not args.model:
        parser.print_help()
        _print_catalog(args.models_config)
        return 0

    validate_args(args)
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
            start_ngl=args.start_ngl,
            split_candidates=args.split_candidates or None,
            ngl_step=args.ngl_step,
            split_min=args.split_min,
            split_max=args.split_max,
            apply=args.apply,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Results file: {args.results_file}", file=sys.stderr)
        return 1
    finally:
        probe_runner.close()

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_result(result)
    return 0
