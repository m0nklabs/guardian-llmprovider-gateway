# Guardian TODO List

## 2026-05-20 Repo Path Decoupling

- [x] Centralize repo-sensitive filesystem paths in shared helpers for app runtime and standalone scripts.
- [x] Remove hardcoded `/home/flip/llama_cpp_guardian` assumptions from `ModelManager`, `start_llama.sh`, tracked script utilities, and unit tests.
- [x] Update docs to describe the checkout-root and env-override path resolution model.

## 2026-05-20 Fast Model Finetune Suite

- [x] Add a Guardian-native finetune helper that binary-searches the highest stable runtime context instead of sweeping linearly.
- [x] Explore two-GPU tensor split candidates coarse-to-fine around the current config so `models.yaml` tuning is fast enough for live use.
- [x] Extend the live finetune search to include `ngl`, so full-context profiles can prove whether a conservative GPU offload value is still necessary.
- [x] Provide a CLI wrapper that logs finetune runs and optionally writes the winning `context` and `tensor_split` back to `config/models.yaml`.
- [x] Add an explicit auto context-range mode so the CLI can derive sensible min/max bounds from the current runtime config.
- [x] Harden the suite against transient Guardian request resets and guarantee config restoration on failed dry-runs.
- [x] Persist compatible probe results in `data/model_finetune_results.json` so later runs can skip already tested `context`/`tensor_split` combinations.
- [x] Validate the suite with focused unit tests and a narrow live Guardian dry-run against the Native-MTP Qwen profile.
- [x] Re-run the Native-MTP Qwen profile with Guardian image smoke, apply the winning config, and confirm cache hits on the repeat search.
- [x] Re-run the Native-MTP Qwen profile with full-context `ngl` search and confirm that `ngl: 36` still wins at `262144` on this host.

## 2026-05-20 Qwen3.6 Native-MTP Multimodal Bring-up

- [x] Sync the local official `llama.cpp` checkout to an upstream revision that exposes `draft-mtp`, while preserving the known-safe mixed-GPU CUDA build flags.
- [x] Download `Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q4_K_M.gguf` and `Qwen3.6-35B-A3B-mmproj-BF16.gguf` into the shared models directory.
- [x] Register a Guardian profile and aliases for the preserved-MTP Heretic Qwen3.6 runtime, including `--spec-type draft-mtp` and the required mmproj path.
- [x] Fix hot model-registry reload so `/admin/load` and `/v1/models` see newly added `models.yaml` entries without restarting Guardian.
- [x] Tune the runtime fit for the current 3060 + 5060 Ti host until the profile loads stably through Guardian.
- [x] Validate text and image requests through Guardian and confirm live speculative metrics (`draft_n`, `draft_n_accepted`) appear on responses.

## 2026-05-20 Qwen3.6 Guardian Ceiling Follow-up

- [x] Re-run the Qwen3.6 context ceiling search through Guardian `/admin/load` and live proxy requests instead of standalone `llama-server` launches.
- [x] Confirm the official `262144` model ceiling works end-to-end through Guardian with a heavier runtime request.
- [x] Measure hardware headroom beyond the official ceiling: `524288` stays stable on this dual-GPU host, `540672` becomes runtime-unstable, and `557056`+ fails to load.
- [x] Teach Guardian's load-failure parser to classify the high-context Qwen aborts that currently surface as `Unknown error (no recognizable error pattern in logs)`.

## 2026-05-20 Qwen3.6 Runtime Re-baseline

- [x] Prove whether the current `Qwen3.6-35B-A3B-HauhauCS-Aggressive` split still loads on the current dual-GPU host.
- [x] Find the first loadable tensor split for `/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf`.
- [x] Reconcile the historical 131072 q4 benchmark with current live probes and reset the config to the matching runtime/paper values.

## 2026-05-20 Model Registry Cleanup

- [x] Remove deleted-model paths and duplicate `-agent` / `-deep` / `-max` profile entries from `config/models.yaml`.
- [x] Strip `-nkvo` and fixed reasoning-mode flags from surviving runtime entries so request behavior can be negotiated via the API.
- [x] Raise surviving runtime contexts to repo-documented safe maxima where empirical benchmark evidence exists.

## 2026-05-19 Vision Capability Hardening

- [x] Validate which configured model IDs are actually vision-ready at runtime instead of assuming `mmproj` implies working multimodal support.
- [x] Expose vision capability state through `/v1/models` so clients can distinguish verified image support from unverified or broken candidates.
- [x] Return explicit OpenAI-style 4xx/503 errors for image requests that target text-only, misconfigured, still-loading, or broken multimodal runtimes.

## 2026-05-16 Operational Hardening Sprint

- [x] Refactor `max_context` to `benchmark_context_limit` across config, code, tests, and docs while keeping OpenAI-compatible `max_context` output where clients still require it.
- [x] Make Guardian bind `11434` immediately after restart by moving startup verification out of blocking FastAPI startup.
- [x] Make PID-file handling restart-safe and wait briefly for an existing `11434` listener to release during systemd handoff.
- [x] Expand `/api/status` with generation-safe operation status, proxy listener diagnostics, backend verification timestamps, and routing recommendations.
- [x] Expand `/api/status` further with explicit switch ownership, current requested target, live queue state, and last successful backend verification timing for faster operator debugging.
- [x] Make `/admin/load` accept aliases and serialize with the shared model-switch lock.
- [x] Improve `model: auto` ergonomics by preferring tool-friendly sibling profiles for reasoning-heavy model families.
- [x] Improve Ollama bridge ergonomics by falling back to reasoning output when a reasoning model emits no visible content.
- [x] Add regression coverage for startup/status generation handling and preferred tool-model selection.
- [x] Add a live restart-race regression that restarts the active Guardian systemd service, performs an immediate alias load, checks status, and runs a mini chat.
- [x] Validate the changes with focused unit tests, live metadata probes, restart smoke checks, and practical end-to-end load/inference tests.