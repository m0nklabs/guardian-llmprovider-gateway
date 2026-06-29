# Guardian TODO List

## 2026-06-28 LLM Router — Cloud Provider Support (OpenRouter + NVIDIA)

- [x] Add `app/proxy/providers.py` with `ProviderRegistry` that loads cloud provider config from `settings.yaml`.
- [x] Support `${ENV_VAR}` expansion for API keys so secrets stay out of the repo.
- [x] Add `providers` section to `config/settings.yaml` with OpenRouter and NVIDIA stub entries.
- [x] Cloud models appear in `GET /v1/models` with `served_by: cloud` and `owned_by: <provider>`.
- [x] `GET /v1/models/{model_id}` returns cloud metadata for cloud models (including slash-containing IDs).
- [x] `_resolve_or_reject_inference_model` accepts cloud models instead of rejecting them with 404.
- [x] `POST /v1/{path}` routes cloud models to the provider API, bypassing queue/VRAM/switch logic.
- [x] `POST /api/chat` (Ollama bridge) forwards cloud models to the provider with Ollama→OpenAI translation.
- [x] `POST /api/generate` (Ollama bridge) forwards cloud models to the provider.
- [x] Streaming SSE passthrough for cloud models with `StreamProgressWatchdog` timeout protection.
- [x] Usage tracking (token counts, live dashboard) for cloud requests via existing `_record_usage_from_payload` and `_update_live_request_usage`.
- [x] `503 provider_unavailable` error when a provider is enabled but has no API key.
- [x] `502` error mapping when a cloud provider request fails.
- [x] Hot-reload: provider registry re-reads `settings.yaml` on every model resolution.
- [x] Unit tests for `ProviderRegistry` (26 tests) and server-level cloud routing tests (6 tests).
- [x] Documentation in `docs/LLM_ROUTER.md`.
- [x] Populate `config/settings.yaml` with actual OpenRouter and NVIDIA model lists once API keys are provisioned.
- [ ] Add admin endpoint to reload/reload-check the provider registry on demand.

## 2026-06-28 Per-Key Cloud Credential Routing

- [x] Add `app/proxy/cloud_keys.py` with `CloudCredentialStore` for per-key cloud credentials.
- [x] Store credentials and key↔credential links in `config/cloud_keys.json`.
- [x] `parse_guardian_route()` parses `guardian/{provider}/{model}` routes.
- [x] `_forward_to_cloud_provider` resolves per-key credentials and rewrites model name to upstream model.
- [x] `_resolve_or_reject_inference_model` accepts `guardian/` routes.
- [x] `_is_cloud_or_guardian_route()` helper for both global and per-key cloud models.
- [x] `/v1/models` includes per-key cloud routes for the requesting client.
- [x] `GET/POST /api/keys` — generate and list Guardian API keys.
- [x] `GET/POST/DELETE /api/cloud/credentials` — manage cloud credentials (masked API keys).
- [x] `GET/POST/DELETE /api/cloud/links` — link/unlink credentials to Guardian keys.
- [x] `GET /api/cloud/providers` — list configured providers and status.
- [x] `GET /api/cloud/models` — list all cloud models available to the requesting client.
- [x] Dashboard UI: API key generation panel, cloud credential management, key↔credential linking, available cloud models table.
- [x] Unit tests for `CloudCredentialStore` (33 tests) covering lifecycle, model management, linking, per-key lookup, and persistence.
- [x] Full test suite passes (463 tests).

## 2026-05-27 llama.cpp b1295 Runtime Integration

- [x] Keep reasoning as the default for normal chat and agent model profiles.
- [x] Replace the Qwen3.6 agent profile's no-thinking `--reasoning-budget 0` runtime with bounded reasoning.
- [x] Add non-thinking defaults only for special embedding runtimes.
- [x] Make OpenAI-compatible `completions` and `embeddings` requests participate in guarded model switching.
- [x] Restore Hermes model-switch permission and hot-reload switch allowlist checks from `models.yaml`.
- [x] Switch the live systemd runtime from the b1176 fallback to the new CUDA 13.2 b1295 `llama-server` build.
- [x] Keep Gemma4 31B on full context by reducing b1295 compute batch sizes instead of lowering context, `ngl`, or tensor split.
- [x] Validate Qwen3.6, Gemma4 31B, and embeddings through Guardian after the live runtime switch.

## 2026-05-27 Same-Key Queue Robustness

- [x] Allow one API key to submit multiple waiting GPU-backed requests without duplicate-admission `409` failures.
- [x] Keep per-key running-slot fairness by blocking a queued request until that key has no active running request.
- [x] Update queue contract documentation and regression tests for Hermes-style helper/auxiliary traffic.

## 2026-05-27 Qwen Thinking Runtime Repair

- [x] Restore explicit Qwen3.6 Hauhau `--reasoning on --reasoning-format deepseek` runtime flags.
- [x] Reintroduce Qwen3.6 tool-friendly and bounded-reasoning agent aliases using the current validated tensor split.
- [x] Remove the custom `qwen3_nonthinking.jinja` template injection from Qwen agent launches.
- [x] Prove Qwen3.6 35B q4 works again via Guardian on known-good official llama.cpp `b1176` with the validated `0.36,0.64` split and clean VRAM except Frigate.
- [x] Live-validate Gemma4 31B through Guardian after the b1258 regression is isolated upstream.

## 2026-05-27 Stream-Safe Config Sync

- [x] Stop `scripts/sync_models.py` from restarting `llama-guardian.service` after `models.yaml` edits now that Guardian hot-reloads registry state.
- [x] Keep long-lived client streams safe from config-sync induced Guardian restarts.

## 2026-05-27 Framework Key Attribution

- [x] Register dedicated Guardian API keys for the Kyber-managed framework runtimes.
- [x] Keep `config/api_keys.json` aligned with the live per-framework key split used by Kyber and ClaudeCode.

## 2026-05-24 Backend Reload Recovery

- [x] Restore live inference after the Step3 startup OOM left Guardian in the internal `__MISMATCH__` state.
- [x] Prove the active backend commandline contains the expected Hauhau text `--tensor-split 0.36,0.64` after a live reload.
- [x] Prevent startup and proxy auto-reload paths from using `__MISMATCH__` as a model name.
- [x] Persist the live-validated Hauhau text split in `config/models.yaml`.
- [x] Add regression coverage for safe reload-target resolution and startup adoption of a known live backend.

## 2026-05-24 Dashboard Usage Monitoring Restore

- [x] Recover the missing `:11437` monitoring work from the stashed index without re-enabling the old broad-sweep benchmark runtime.
- [x] Persist API usage counters and recent request history in `data/api_usage_state.json` so dashboard data survives Guardian restarts.
- [x] Expose API usage snapshots through `/api/stats` and render request/token totals, top clients, and recent activity in the served static dashboard.
- [x] Validate the restored monitoring path with focused tests and the full unit suite.

## 2026-05-24 Gemma4 31B Text Finetune

- [x] Live-test `gemma-4-31B-it-uncensored-heretic` through Guardian finetune v2 in text mode.
- [x] Apply the winning text runtime: `context: 262144`, `ngl: 60`, `tensor_split: "0.42,0.58"`.
- [x] Apply the companion `gemma-4-E4B-it-uncensored` text runtime: `context: 131072`, `ngl: 42`, `tensor_split: "0.32,0.68"`.
- [x] Confirm the full unit suite still passes after the applied runtime update and dashboard restore.

## 2026-05-24 Hauhau Finetune V2 Live Proof

- [x] Add and validate the root `./finetune_v2.py` operator entrypoint with no-argument help plus model and alias listing.
- [x] Live-test `Qwen3.6-35B-A3B-HauhauCS-Aggressive` in vision mode through Guardian with full `262144` context and `start_ngl=37`.
- [x] Prove the GPU-order-aware split direction on the real dual-GPU host; `0.46,0.54` correctly applies to the effective runtime but overloads GPU0, while shifting toward GPU1 reaches stable splits.
- [x] Make finetune v2 continue through llama.cpp same-backend-bucket plateaus directionally, including the live `0.39 -> 0.38 -> 0.37` proof.
- [x] Apply the final Hauhau vision winner: `vision_context: 262144`, `vision_ngl: 40`, `vision_tensor_split: "0.36,0.64"`.

## 2026-05-21 Finetune V2 Rewrite

- [x] Write a requirements doc for finetune v2 before touching the implementation.
- [x] Rebuild finetune winner selection so `context` and `speed` use explicit mode-aware comparators instead of a hidden balance-first override.
- [x] Move v2 search state to in-memory working configs and only write `models.yaml` once on final `--apply`.
- [x] Split the finetune engine into smaller planner, probe, ranking, and persistence units.
- [x] Add a root-level `./finetune_v2.py` operator entrypoint that lists options, models, and aliases when run without arguments.

## 2026-05-21 Layer Ceiling Metadata

- [x] Record each configured model's GGUF backbone layer count in `config/models.yaml` as `total_layers`.
- [x] Clamp finetune `ngl` search to `total_layers` so Guardian stops probing synthetic offload values above the model's real layer count.
- [x] Verify the multimodal projector path separately and avoid incorrectly adding `mmproj` metadata to the main-model `ngl` ceiling.

## 2026-05-21 Qwen3.6 Vision Context Rebaseline

- [x] Re-run the Native-MTP Qwen vision finetune with `--optimization context`, auto `ngl` search, and split rebalancing only after a successful probe.
- [x] Use the earlier `262144 / 36 / 0.55,0.45` result only as the recovery baseline; the rerun supersedes it with the applied `262144 / 32 / 0.50,0.50` vision runtime.
- [x] Validate the applied winning vision config through Guardian after the rerun.

## 2026-05-20 Repo Path Decoupling

- [x] Centralize repo-sensitive filesystem paths in shared helpers for app runtime and standalone scripts.
- [x] Remove hardcoded `/home/flip/llama_cpp_guardian` assumptions from `ModelManager`, `start_llama.sh`, tracked script utilities, and unit tests.
- [x] Update docs to describe the checkout-root and env-override path resolution model.

## 2026-05-20 Fast Model Finetune Suite

- [x] Add a Guardian-native finetune helper that binary-searches the highest stable runtime context instead of sweeping linearly.
- [x] Explore two-GPU tensor split candidates coarse-to-fine around the current config so `models.yaml` tuning is fast enough for live use, with the objective ordered as `context > split balance > ngl`.
- [x] Extend the live finetune search to include `ngl`, so full-context profiles can prove whether a conservative GPU offload value is still necessary.
- [x] Provide a CLI wrapper that logs finetune runs and optionally writes the winning `context`, `ngl`, and `tensor_split` back to `config/models.yaml`.
- [x] Add an explicit auto context-range mode so the CLI can derive sensible min/max bounds from the current runtime config.
- [x] Harden the suite against transient Guardian request resets and guarantee config restoration on failed dry-runs.
- [x] Persist compatible probe results in `data/model_finetune_results.json` so later runs can skip already tested `context`/`ngl`/`tensor_split` combinations.
- [x] Replace sweep-style auto split/`ngl` candidate expansion with a strict 3-phase flow that monitors per-GPU free VRAM and rebalances the split on every successful context/ngl state change.
- [x] Replace manual finetune range flags with a single `--optimization` mode (`speed`, `context`, `balanced`) so operators steer the tradeoff without hardcoding min/max context or `ngl` bounds.
- [x] Validate the suite with focused unit tests and a narrow live Guardian dry-run against the Native-MTP Qwen profile.
- [x] Re-run the Native-MTP Qwen profile with Guardian image smoke, apply the winning config, and confirm cache hits on the repeat search.
- [x] Re-run the Native-MTP Qwen profile with full-context `ngl` search and confirm that `ngl: 36` still wins at `262144` on this host.
- [x] Re-run the Native-MTP Qwen profile in text-only Guardian mode and confirm the full native `262144` runtime can hold `ngl: 99` once the split is rebalanced to `0.61,0.39`.
- [x] Split text and vision finetune/application paths so `mmproj` only loads on image requests and `vision_*` tuning can coexist with a higher text runtime in one model entry.
- [x] Flush each individual finetune probe into `data/model_finetune_results.json` while a run is still in progress, instead of waiting for only the final summary.

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

## 2026-06-29 Anthropic Messages API Bridge — Claude Code Compatibility

### Cloud Bridge (`anthropic_bridge.py`)
- [x] Compare OpenRouter OpenAPI spec against our bridge implementation — identified 10 gaps.
- [x] Fix HIGH: Thinking blocks not translated (reasoning_content/reasoning → thinking blocks in response + streaming).
- [x] Fix HIGH: Error responses not translated (added `translate_openai_error_to_anthropic()` with HTTP status → error type mapping).
- [x] Fix MED: Image URL source missing (`source.type == "url"`).
- [x] Fix MED: Cache usage fields missing (`cache_creation_input_tokens`, `cache_read_input_tokens` in streaming + non-streaming).
- [x] Fix MED: `stop_sequence` value always null (added `request_stop_sequences` parameter + best-effort detection).
- [x] Fix MED: PDF/document content blocks not handled (base64 + URL → data URLs).
- [x] Fix MED: Streaming `thinking_delta` missing.
- [x] Fix MED: Interleaved blocks in streaming (text after tool_use gets new block index).
- [x] Fix LOW: Stop sequence detection in streaming.
- [x] Fix HIGH: Ping SSE events during idle upstream (prevents Claude Code 5-min idle timeout).
- [x] Fix MED: `signature_delta` event before thinking block `content_block_stop`.
- [x] Fix MED: `content_filter` → `refusal` stop_reason mapping (was `end_turn`).
- [x] Fix MED: `is_error` field in `tool_result` not passed through.
- [x] Fix MED: `disable_parallel_tool_use` not translated to `parallel_tool_calls: false`.
- [x] Fix: `_convert_tool_choice()` didn't handle dict-form `tool_choice` like `{"type": "auto"}`.
- [x] E2E verified against live NVIDIA NIM with minimax-m3 model (non-streaming, streaming, errors, cache usage).
- [x] 63 unit tests in `tests/unit/test_anthropic_bridge.py`.

### Local Model Enrichment (`server.py`)
- [x] Fix HIGH: Prefill workaround broke on Anthropic content blocks (`str(content)` → `_stringify_message_content()`).
- [x] Fix MED: `message_delta` usage missing `input_tokens` (Claude Code shows 0 tokens in status bar).
- [x] Fix MED: `cache_creation_input_tokens` missing from llama-server responses (added in streaming + non-streaming).
- [x] Fix MED: Keepalive comments → Anthropic `ping` events for `/v1/messages` streams.
- [x] Fix: Non-streaming Response headers — `content-length` from llama-server caused truncated responses when enriched content had different size.
- [x] Fix HIGH: Anthropic `thinking: {type: "disabled"}` not converted to llama-server params (added `_apply_anthropic_thinking_to_llama_params()`).
- [x] Fix HIGH: Anthropic `thinking: {type: "enabled", budget_tokens: N}` → `reasoning_budget: N`.
- [x] Fix MED: `stop_reason` incorrect when stop_sequence matched (llama-server returns `"end_turn"`, corrected to `"stop_sequence"`).
- [x] E2E verified with local Qwen3.6 model (thinking disabled/enabled/adaptive, tool use, stop_sequences, streaming, non-streaming).
- [x] Documentation in `docs/ANTHROPIC_BRIDGE.md`.
- [x] 526 total unit tests passing.