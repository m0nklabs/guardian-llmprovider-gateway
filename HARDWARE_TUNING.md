# Hardware Tuning

## Purpose

Guardian tuning is host-specific runtime calibration, not model training.

The goal is to find the best stable runtime shape for this exact machine:

- the highest safe `context`
- the highest useful `ngl`
- a `tensor_split` that keeps the bottleneck GPU alive instead of pretending
  the box is symmetric
- separate text and vision winners when `mmproj` changes the fit

All live tuning must go through Guardian itself. The point is to validate the
same queue, reload, VRAM fencing, and backend lifecycle that production uses.

## Current Runtime Assumptions

- `proxy.vram_limit_mb: 27000` in [config/settings.yaml](config/settings.yaml)
- official `llama-server` launched by [scripts/start_llama.sh](scripts/start_llama.sh)
- generated runtime args written to [config/current_model.args](config/current_model.args)
- GPU telemetry normalized to llama/CUDA order by PCI bus ID
- optional ComfyUI cooperation through `services.comfyui_url`

## Tuning Surfaces

| Surface | Role | Active today |
| --- | --- | --- |
| [finetune_v2.py](finetune_v2.py) | Operator entrypoint | Yes |
| [scripts/finetune_v2_model_config.py](scripts/finetune_v2_model_config.py) | Compatibility wrapper around the same CLI | Yes |
| [app/tweaker/finetune_v2_cli.py](app/tweaker/finetune_v2_cli.py) | CLI parsing and validation | Yes |
| [app/tweaker/finetune_v2_runner.py](app/tweaker/finetune_v2_runner.py) | Planner, probe loop, dry-run/apply behavior | Yes |
| [app/tweaker/finetune_v2_telemetry.py](app/tweaker/finetune_v2_telemetry.py) | GPU telemetry, split verification, backend VRAM snapshot | Yes |
| [app/tweaker/finetune_v2_support.py](app/tweaker/finetune_v2_support.py) | Split helpers, runtime mode helpers, YAML block replacement | Yes |
| [scripts/start_llama.sh](scripts/start_llama.sh) | Loads `current_model.args`, sources `current_model.env`, starts official backend | Yes |
| [data/model_finetune_v2_results.json](data/model_finetune_v2_results.json) | Canonical append-only result log | Yes |
| `data/model_finetune_v2_results.json.active` | In-progress run snapshot sidecar created during live runs | Ephemeral |
| [scripts/benchmark_context.py](scripts/benchmark_context.py) | Historical benchmark helper | Legacy |
| [scripts/analyze_benchmark.py](scripts/analyze_benchmark.py) | Historical benchmark analysis | Legacy |
| [scripts/recommend_context.py](scripts/recommend_context.py) | Historical benchmark recommendation helper | Legacy |
| [scripts/update_guardian_config.py](scripts/update_guardian_config.py) | Config editing helper | Secondary |

## Backend Control Files

Guardian tuning changes runtime state through generated files and live API
calls, not by hand-editing `llama-server` commands.

### [config/current_model.args](config/current_model.args)

This is the effective backend contract. `ModelManager._write_server_args()`
writes:

- `-m <gguf>`
- `-c <context>`
- `-ngl <ngl>`
- `-ctk` / `-ctv`
- `--tensor-split a,b`
- `--mmproj <path>` when vision mode is enabled
- any model-specific `extra_args`

### [scripts/start_llama.sh](scripts/start_llama.sh)

This script:

- resolves repo-relative defaults for the Guardian repo, models directory, and
  official llama.cpp checkout
- sources optional `config/current_model.env`
- forces `CUDA_DEVICE_ORDER=PCI_BUS_ID`
- starts the official `llama-server` binary with the generated arguments

That `CUDA_DEVICE_ORDER` detail matters because the finetune telemetry layer
maps `nvidia-smi` data into the same order. Split decisions are therefore made
against the same device order that the backend actually uses.

## How Finetune V2 Probes the Host

`GuardianV2ProbeRunner` executes one probe as a live Guardian cycle:

1. Capture pre-load VRAM telemetry.
2. Call `POST /admin/load` with `runtime_overrides` for `context`, `ngl`, and
   `tensor_split`.
3. If load succeeds, send a short smoke request to
   `POST /v1/chat/completions`.
4. Capture post-load or post-smoke telemetry:
   - per-GPU free and total VRAM
   - backend-only `llama-server` VRAM usage
   - effective `--tensor-split` read back from `current_model.args`
5. Append the probe immediately to the canonical results file and the `.active`
   sidecar.

That means every successful or failed probe leaves an auditable trail even when
the run is interrupted.

## CLI Entry Points

### Show help, models, and aliases

```bash
./finetune_v2.py
```

### Text-mode ceiling search

```bash
./finetune_v2.py qwen3.6-35b-uncensored \
  --optimization context \
  --context 262144 \
  --start-ngl 37
```

### Vision-mode tuning

```bash
./finetune_v2.py qwen3.6-35b-heretic-mtp \
  --runtime-mode vision \
  --smoke-image-url data:image/png;base64,... \
  --optimization context
```

### Apply the winner to `models.yaml`

```bash
./finetune_v2.py qwen3.6-35b-uncensored --apply
```

## Validation Rules Enforced by the CLI

`app/tweaker/finetune_v2_cli.py` rejects:

- `--context <= 0`
- `--ngl < 0`
- `--start-ngl < 0`
- `--ngl` combined with `--start-ngl`
- `--ngl-step <= 0`
- invalid split bounds
- `--runtime-mode vision` without `--smoke-image-url`

That last rule is important: a vision tune is only valid if the probe actually
exercises the multimodal path.

## Calibration Phases

Guardian's current tuning flow is best understood as four phases.

### Phase 1: Seed the runtime

The runner resolves:

- the canonical model name
- the runtime mode (`text` or `vision`)
- the active and maximum context bounds
- the `total_layers` ceiling for `ngl`
- the seed split from config or explicit `--split`

The first probe is intentionally local to the current host and current runtime.

### Phase 2: Split balance calibration

Once a probe succeeds at the current `context` and `ngl`, Guardian calibrates
the split on that same rung.

Rules currently encoded in `app/tweaker/finetune_v2_runner.py`:

- `BALANCED_FREE_VRAM_THRESHOLD_PCT = 5.0`
- if the free-VRAM gap is large, step by 5%
- if the gap is medium, step by 2%
- otherwise refine by 1%
- if the target GPU is already too tight, skip the coarse move and try a
  smaller local step
- if a requested split changes the effective split but lands in the same
  backend VRAM bucket, keep stepping in that same direction until the bucket
  changes, a probe fails, or bounds are exhausted

**Critical rule:** after Phase 2, the process must include a split balance
check, and the split balance must be fully calibrated, not merely adjusted.

In practice that means do not move on just because a different split loaded
once. The rung is only ready when Guardian has checked the measured balance of
that successful state against:

- the post-smoke free-VRAM delta
- the effective `--tensor-split` written to `current_model.args`
- the backend allocation bucket reported by live `llama-server` telemetry
- any required same-rung follow-up splits that remain untried

If the split is still imbalanced, the runner stays on the same
`context` / `ngl` and continues local split follow-ups. It does not treat a
single adjustment as the final answer.

### Phase 3: Climb or search the frontier

Only after the current rung is balanced enough does the planner move outward.

Depending on the operator goal:

- `context` mode keeps chasing the highest stable context and only relaxes
  `ngl` when needed
- `speed` mode keeps context at the active floor and tries to maximize `ngl`
- `balanced` mode ranks the strongest combined runtime after invalid candidates
  are filtered out

For ladder runs, successful Phase 2 rebalancing can queue an upward `ngl`
retry. That is the explicit rule that prevents the first bad split from being
treated as the final word on a higher `ngl`.

### Phase 4: Convergence and optional apply

The run stops when one of these is true for the best successful state:

1. both GPUs are below `500 MiB` free VRAM
2. the state is already at the maximum allowed `context` and maximum allowed
   `ngl`

There is also a low-headroom budget:

- below `750 MiB` free on both GPUs, Guardian may spend at most 5 more
  follow-up probes trying to reach the final `<500 MiB` target

On completion:

- dry-run restores the on-disk runtime and leaves `models.yaml` unchanged
- `--apply` writes the winning runtime fields back to `models.yaml`

## What `--apply` Changes

The runner writes only the winning runtime fields for the selected mode.

Text mode updates:

- `context`
- `ngl`
- `tensor_split`

Vision mode updates:

- `vision_context`
- `vision_ngl`
- `vision_tensor_split`

What it does not rewrite:

- `benchmark_context_limit`
- aliases
- unrelated model entries
- `models.yaml` at all during a dry run

## Reading the Results Log

Each run stores:

- runtime mode
- optimization mode
- fixed context or fixed ngl constraints, if any
- every probe in order
- the winner
- a machine-readable winner explanation
- convergence reason
- whether the result was applied

That log is the canonical audit trail for host tuning.

## Legacy and Secondary Utilities

Historical helpers still exist, but they are not the live runtime path:

- `scripts/benchmark_context.py`
- `scripts/analyze_benchmark.py`
- `scripts/recommend_context.py`
- `app/tweaker/legacy/benchmark_suite_v1.py`

The UI still exposes `GET /api/benchmark` for historical results, but
`POST /api/benchmark/start` and `POST /api/benchmark/stop` return `410 Gone`.

Treat benchmark artifacts as historical evidence, not as the active scheduling
or tuning engine.