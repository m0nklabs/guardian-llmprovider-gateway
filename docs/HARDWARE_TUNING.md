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

- `proxy.vram_limit_mb: 27000` in [../config/settings.yaml](../config/settings.yaml)
- official `llama-server` launched by [../scripts/start_llama.sh](../scripts/start_llama.sh)
- current pinned backend target:
  `/home/flip/llama_cpp_official/worktrees/fork/cu133-rel/bin/llama-server`
- the `cu133-rel` binaries embed an absolute local `RUNPATH`; if this worktree
  directory is renamed again, patch the ELF runpaths in `cu133-rel/bin` or the
  service will fail to resolve its bundled `libllama-*` shared libraries
- mixed RTX 3060 + RTX 5060 Ti builds must keep CUDA graphs disabled and peer
  copy disabled; the b1295 CUDA 13.2 build was compiled with
  `GGML_CUDA_GRAPHS=OFF`, `GGML_CUDA_NO_PEER_COPY=ON`, NCCL, and
  `86-real;120a-real`
- Gemma4 31B uses explicit b1295 compute-buffer controls at full context:
  `--flash-attn on --parallel 1 --batch-size 256 --ubatch-size 128`
- generated runtime args written to [../config/current_model.args](../config/current_model.args)
- GPU telemetry normalized to llama/CUDA order by PCI bus ID
- optional ComfyUI cooperation through `services.comfyui_url`

## Tuning Surfaces

| Surface | Role | Active today |
| --- | --- | --- |
| [../finetune_v2.py](../finetune_v2.py) | Operator entrypoint | Yes |
| [../scripts/finetune_v2_model_config.py](../scripts/finetune_v2_model_config.py) | Compatibility wrapper around the same CLI | Yes |
| [../app/tweaker/finetune_v2_cli.py](../app/tweaker/finetune_v2_cli.py) | CLI parsing and validation | Yes |
| [../app/tweaker/finetune_v2_runner.py](../app/tweaker/finetune_v2_runner.py) | Planner, probe loop, dry-run/apply behavior | Yes |
| [../app/tweaker/finetune_v2_telemetry.py](../app/tweaker/finetune_v2_telemetry.py) | GPU telemetry, split verification, backend VRAM snapshot | Yes |
| [../app/tweaker/finetune_v2_support.py](../app/tweaker/finetune_v2_support.py) | Split helpers, runtime mode helpers, YAML block replacement | Yes |
| [../scripts/start_llama.sh](../scripts/start_llama.sh) | Loads `current_model.args`, sources `current_model.env`, starts official backend | Yes |
| [../data/model_finetune_v2_results.json](../data/model_finetune_v2_results.json) | Canonical append-only result log | Yes |
| `data/model_finetune_v2_results.json.active` | In-progress run snapshot sidecar created during live runs | Ephemeral |
| [../scripts/recommend_context.py](../scripts/recommend_context.py) | Historical benchmark recommendation helper | Legacy |
| [../scripts/update_guardian_config.py](../scripts/update_guardian_config.py) | Config editing helper | Secondary |

## Backend Control Files

Guardian tuning changes runtime state through generated files and live API
calls, not by hand-editing `llama-server` commands.

### [../config/current_model.args](../config/current_model.args)

This is the effective backend contract. `ModelManager._write_server_args()`
writes:

- `-m <gguf>`
- `-c <context>`
- `-ngl <ngl>`
- `-ctk` / `-ctv`
- `--tensor-split a,b`
- `--mmproj <path>` when vision mode is enabled
- any model-specific `extra_args`

### [../scripts/start_llama.sh](../scripts/start_llama.sh)

This script:

- resolves repo-relative defaults for the Guardian repo, models directory, and
  official llama.cpp checkout
- honors `LLAMA_SERVER_BINARY` when a specific known-good official backend build
  must be pinned for runtime stability
- sources optional `config/current_model.env`
- forces `CUDA_DEVICE_ORDER=PCI_BUS_ID`
- starts the official `llama-server` binary with the generated arguments

That `CUDA_DEVICE_ORDER` detail matters because the finetune telemetry layer
maps `nvidia-smi` data into the same order. Split decisions are therefore made
against the same device order that the backend actually uses.

## Gemma4 31B: Why VRAM Jumped

The big Gemma4 regression was not that the model itself changed. The load path
changed.

On this host the failing combination was:

- `gemma-4-31B-it-uncensored-heretic-Q4_K_M.gguf`
- `context: 262144`
- quantized KV cache (`-ctk q4_0 -ctv q4_0`)
- flash attention enabled

That shape used to fit, then newer upstream llama.cpp started reserving more GPU
memory earlier during startup. The main culprit for the extra VRAM pressure was
the CUDA-side change from `#23907` (`reserve space for quantize kv-cache at
startup`). With quantized KV plus flash attention, that reserve moved memory
pressure from "allocate later while running" to "reserve now while loading".
Because Gemma4 was already near the edge on GPU0, that extra reserve was enough
to push the old full-context profile into `graph_reserve` / CUDA OOM territory.

There was a second upstream change, `#23485` (`server: add margin for draft
model for fit`), which makes fit decisions more conservative. That one matters
more for draft/MTP paths than for the plain Gemma text route, but the fork keeps
both reverts so the host no longer carries either source of extra fit pressure.

The working recovery on this machine is:

- fork branch: `m0nk111/llama.cpp:no-fit-draft-margin`
- extra revert: `1cd97ea09` for `#23907`
- build/toolchain: CUDA 13.3 `cu133-rel`
- runtime: `262144 / ngl 60 / tensor_split 0.42,0.58 / --main-gpu 1 / --flash-attn on / --parallel 1 / --batch-size 256 / --ubatch-size 128`

CUDA 13.3 was the chosen toolchain for the rebuilt fork, but it was not the
root-cause fix by itself. The key fix was removing the newer startup-reserve and
fit-margin behavior that made Gemma4 load fatter than before.

## Why Gemma4 Is Slower Than Qwen3.6

This is mostly architecture, not a bad Guardian config.

`Qwen3.6-35B-A3B` is a sparse MoE-style route: total model size is large, but a
much smaller active subset does work per generated token. `Gemma4 31B` behaves
like a much denser workload, so more of the model participates every token. On
the same GPUs, that means lower decode throughput even when both models fit.

There are still a few knobs worth tuning, but none of them will turn Gemma4 into
Qwen-speed:

- Keep flash attention on. For the current Gemma KV setup it is required.
- `ngl 60` is already the right direction; less CPU fallback is better.
- `batch-size` and `ubatch-size` help prompt ingestion more than decode speed.
- Small `tensor_split` nudges around `0.42,0.58` can still be tested if GPU0
  becomes the bottleneck, but the gains are usually incremental.
- If you want lower latency over maximum context, the biggest practical tradeoff
  is reducing context, because the larger KV working set hurts both memory headroom
  and token speed.

So the short version is: Gemma4 is slower because it is doing more real work per
token than the Qwen A3B route, not because Guardian is leaving an obvious easy
win on the table.

## Focused Full-Context KV And Batch Findings

The latest direct full-context probe was run against the same backend shape that
Guardian currently manages, with a synthetic long code-style prompt plus an
exact needle recall check.

### Gemma4 26B A4B abliterated text route

- The configured runtime had an invalid `ngl: 99` even though the model only
  exposes `total_layers: 30`; Guardian's runtime-override validation catches
  that, so both the base and q8 profiles now use `ngl: 30`
- A dedicated full-context `q8_0/q8_0` quality route fits cleanly at `262144`
  context on this host

Focused q8 split sweep at `context 262144`, `ngl 30`:

- `0.36,0.64`: `45.427s`, post-smoke free VRAM `3390 MiB / 3191 MiB` — winner
- `0.38,0.62`: `46.712s`, post-smoke free VRAM `2864 MiB / 3719 MiB`
- `0.40,0.60`: `46.038s`, post-smoke free VRAM `2864 MiB / 3719 MiB`
- `0.42,0.58`: `47.609s`, post-smoke free VRAM `2334 MiB / 4245 MiB`
- `0.45,0.55`: `47.991s`, post-smoke free VRAM `1808 MiB / 4773 MiB`

Operational conclusion: the 26B q8 route is a valid full-context quality option
here, and its tuned production split is `0.36,0.64`. Guardian now exposes it
as the default `gemma4` alias and the explicit `gemma4-26b` alias.

### Gemma4 31B text route

- Stable winner: `q4_0/q4_0` KV with `--batch-size 256 --ubatch-size 128`
- Observed load time: `19.232s`
- Observed prompt ingestion: `23161` prompt tokens in `46321.241 ms`
  (`~500.0 tok/s`)
- Post-load free VRAM: `1156 MiB / 1661 MiB`
- Needle recall: correct (`7319`)

What lost:

- `1024/512` did not improve prompt throughput in any meaningful way
  (`~500.2 tok/s`) and only reduced free VRAM to `822 MiB / 1327 MiB`
- `q4_0/q8_0` loaded with only `76 MiB / 57 MiB` free and then died during
  prompt evaluation with a disconnected response
- `q8_0/q8_0` never reached healthy load at `262144` context
- `q8_0/q8_0` at `200000` context still loaded with only `26 MiB / 11 MiB`
  free and also died during prompt evaluation

Operational conclusion: keep Gemma4 31B on symmetric `q4_0` KV plus
`256/128`. There is no credible full-context `q8` production profile for this
host right now.

Lower-context quality fallback:

- `q8_0/q8_0` at `160000` context stayed healthy with the same `ngl 60`,
  `tensor_split 0.42,0.58`, and `256/128` batch settings
- Observed load time: `19.136s`
- Observed prompt ingestion: `23161` prompt tokens in `46486.831 ms`
  (`~498.2 tok/s`)
- Post-load free VRAM: `692 MiB / 1021 MiB`
- Needle recall: correct (`7319`)

That means Gemma now has two meaningful operating points on this host:

- default speed/context profile: `262144` context on `q4_0/q4_0`
- opt-in quality profile: `160000` context on `q8_0/q8_0`

The `160000` q8 route is not faster. It exists only as a deliberate context-for-
KV-quality tradeoff.

High-input q8 batch follow-up:

- on a much larger `~133k`-token prompt, every tested Gemma `q8_0/q8_0` batch
  shape disconnected during prompt processing: `256/128`, `512/256`, and
  `1024/512`
- those runs all loaded successfully, but post-load free VRAM kept shrinking as
  batch size rose: about `692/1021 MiB`, `644/945 MiB`, then `436/733 MiB`

Operational conclusion: do not raise Gemma q8 batch sizes in Guardian. For very
large prompt ingestion, the `160000` Gemma q8 quality route should still be
treated as fragile, and the stable high-context route remains the `262144`
`q4_0/q4_0` profile.

### Qwen3.6 35B text route

- Fast default winner: `q4_0/q4_0`
- Observed load time: `17.103s`
- Observed prompt ingestion: `22661` prompt tokens in `9892.948 ms`
  (`~2290.6 tok/s`)
- Post-load free VRAM: `2682 MiB / 1659 MiB`
- Needle recall: correct (`7319`)

Batch-size follow-up:

- forcing `--batch-size 256 --ubatch-size 128` on the same `q4_0/q4_0` route
  cut prompt throughput to `~1150.9 tok/s` while only improving free VRAM to
  `2914 MiB / 1891 MiB`
- forcing the same `256/128` batch pair on the `q8_0/q8_0` route also cut
  prompt throughput to `~1153.4 tok/s`

Operational conclusion: the current Qwen speed routes should keep batch sizes
unset. On this host, explicit `256/128` is stable but materially slower than the
implicit llama.cpp default.

Alternative full-context quality profile:

- `q8_0/q8_0` stayed healthy at `262144` context
- Observed load time: `17.126s`
- Observed prompt ingestion: `22661` prompt tokens in `9929.196 ms`
  (`~2282.3 tok/s`)
- Post-load free VRAM: `2298 MiB / 763 MiB`
- Needle recall: correct (`7319`)

Why it does not replace the default:

- Throughput difference versus `q4_0/q4_0` was negligible in this probe
- The smaller GPU headroom on the second card (`763 MiB`) is much tighter for a
  shared host that still has to coexist with other GPU consumers

So Guardian keeps `q4_0/q4_0` as the default Qwen speed profile and exposes a
separate opt-in `q8_0/q8_0` quality route instead of silently making the live
default more fragile.

High-input q8 batch follow-up:

- on a much larger `~133k`-token prompt, the `q8_0/q8_0` Qwen route stayed best
  with its implicit batch default at `~1606-1611 tok/s`
- forcing `--batch-size 512 --ubatch-size 256` dropped that to `~1274.6 tok/s`
- forcing `--batch-size 1024 --ubatch-size 512` landed at `~1602.8-1612.5 tok/s`
  across two runs, which is effectively a wash against the implicit default and
  not a durable win

Operational conclusion: even for very large prompt ingestion, the Qwen q8 route
should keep batch sizes unset on this host.

### Important config limitation

Guardian's current model config schema only exposes one `kv_type`, and
`ModelManager._write_server_args()` maps it to both `-ctk` and `-ctv`.
That means asymmetric K/V profiles such as `q4_0/q8_0` are benchmarkable via
direct `llama-server` launches, but they are not yet representable as durable
`models.yaml` profiles without widening Guardian's config/runtime model first.

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

- `scripts/recommend_context.py`
- `app/tweaker/legacy/benchmark_suite_v1.py`

The UI still exposes `GET /api/benchmark` for historical results, but
`POST /api/benchmark/start` and `POST /api/benchmark/stop` return `410 Gone`.

Treat benchmark artifacts as historical evidence, not as the active scheduling
or tuning engine.