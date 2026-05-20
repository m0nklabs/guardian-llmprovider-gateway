# Guardian TODO List

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