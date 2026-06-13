# Changelog

## [Unreleased]

### Added
- The static monitoring UI on `:11437` now shows the current live Guardian request and a real queue contents table, including phase, client, model, elapsed time, queue wait, waiting positions, and in-flight token counters sourced from `queue_status` plus the active-request tracker in `api_usage`.
- Guardian queue lifecycle endpoints `GET /v1/queue/requests/{request_id}` and `DELETE /v1/queue/requests/{request_id}`, plus richer queue status payloads that expose request states, cancellation counts, and the current wait policy.
- Ground-up documentation suite for the live Guardian runtime: rewritten `README.md` and `ARCHITECTURE.md`, plus new `HARDWARE_TUNING.md` and `API_REFERENCE.md`, all aligned to the current queue, model lifecycle, systemd-backed backend control, ComfyUI `/free` integration, and finetune v2 behavior.

### Changed
- Replaced the redundant `gemma4-q8kv` / `gemma4-26b-q8kv` aliases with a single explicit `gemma4-26b` alias while keeping `gemma4` as the default 26B q8 route.
- Switched the `qwen3.6-35b-uncensored` and `gemma4-31b-uncensored` aliases to their q8-backed profiles so those uncensored entrypoints now default to quantized-KV runtimes.
- Switched the default `gemma4` alias to the tuned 26B `q8_0/q8_0` route and removed redundant `*-quality` aliases that only duplicated existing `*-q8kv` targets.
- Fixed the Huihui Gemma4 26B A4B runtime metadata to use the real `ngl: 30` layer count, added the short `gemma4-26b` alias, and tuned its full-context `q8_0/q8_0` production split to `0.36,0.64` after a Guardian-backed sweep showed the best combined headroom and smoke latency on this host.
- Documented the higher-token q8 batch follow-up: Qwen q8 stayed fastest with its implicit default batch even on a `~133k`-token prompt, while Gemma q8 at `160000` context disconnected under every tested higher-batch shape, so no additional q8 batch settings were applied to Guardian model profiles.
- Documented the Qwen batch-size follow-up probe: explicitly pinning `--batch-size 256 --ubatch-size 128` on the current Qwen q4 and q8 routes stayed stable but roughly halved prompt-ingestion throughput on this host, so the Qwen profiles intentionally keep batch sizes unset.
- Shortened the custom fork worktree path from `worktrees/fork-no-fit-draft-margin-1a7718b4` to `worktrees/fork`, updated the live systemd backend override plus operator docs to point at the shorter CUDA 13.3 `cu133-rel` binary path, and rewrote the moved build's embedded ELF `RUNPATH` entries so the service still resolves its local `libllama-*` shared libraries after the rename.
- Added a separate lower-context Gemma4 31B `q8_0/q8_0` quality profile at `context: 160000` after direct probes showed `200000` still died under long-prompt load while `160000` stayed stable with healthy recall and acceptable VRAM headroom; the default Gemma route remains the full-context `q4_0/q4_0` profile.
- Documented the focused full-context Gemma4 31B and Qwen3.6 35B batch/KV sweep, confirming Gemma should stay on `q4_0/q4_0` with `--batch-size 256 --ubatch-size 128`, while Qwen keeps `q4_0/q4_0` as the fast default and now exposes a separate full-context `q8_0/q8_0` quality profile because it fits but leaves much tighter VRAM headroom.
- Replaced the temporary upstream/reduced-context Gemma runtime with the CUDA 13.3 `cu133-rel` build of the custom fork branch `m0nk111/llama.cpp:no-fit-draft-margin`, carrying the draft-margin and quantized-KV startup-reserve reverts, and restored the current Gemma 31B runtime at `benchmark_context_limit/context: 262144`, `ngl: 60`, `tensor_split: 0.42,0.58`, plus `--main-gpu 1 --flash-attn on --parallel 1 --batch-size 256 --ubatch-size 128`.
- Promoted the live systemd backend override to the fresh upstream official llama.cpp build at `/home/flip/llama_cpp_official/worktrees/master-1a7718b4/build-cuda128-server-toolkit128-release/bin/llama-server` after isolated validation proved the new binary serves coherent Gemma4 output again.
- Retuned `gemma-4-31B-it-uncensored-heretic` for the fresh upstream runtime to `context: 196608` while keeping `benchmark_context_limit: 262144`, and added `--main-gpu 1 --batch-size 128 --ubatch-size 64` so the 31B route fits on the current RTX 3060 + RTX 5060 Ti pair without crashing during compute-buffer reservation.
- Rolled the live systemd backend override back from the newer upstream official llama.cpp HEAD to the old official `aa50b2c2a` CUDA 12.8 build at `/home/flip/llama_cpp_official/worktrees/cuda132-master/build-cu128/bin/llama-server`, because the newer `35c9b1f39` binary produced immediate gibberish on both Gemma4 31B and Qwen3.6 under the same Guardian runtime args while the rebuilt older commit restored coherent output.
- Guardian startup no longer crashes when `app/ui/static` is absent; `app.main` now skips the `/static` mount with a warning so proxy restarts still come back on hosts that only have the inline dashboard HTML checked out.
- Streaming proxy cancellation now observes queue cancellation events and bounds upstream response/client/background-task cleanup, preventing cancelled Hermes streams from sitting in `cancelling` indefinitely.
- Updated the Qwen3.6 agent runtime to bounded reasoning instead of `--reasoning-budget 0`, keeping reasoning as the normal agent default on current llama.cpp while preserving the proven model path, context, KV, and tensor split settings.
- Marked Nomic embedding profiles as dedicated non-thinking embedding runtimes with `--embedding --reasoning off`, so special non-chat routes do not inherit the new server-side thinking default.
- Guardian's OpenAI-compatible inference proxy now applies `reasoning_budget: 0` plus `chat_template_kwargs.enable_thinking=false` only for explicit no-thinking requests or special non-reasoning model profiles, leaving normal chat/agent requests untouched.
- The `/v1` proxy now runs the guarded model-switch path for `completions` and `embeddings` as well as chat endpoints, so special routes load their requested runtime instead of accidentally hitting whichever chat model is currently active.
- Model switch allowlist checks now hot-reload from `models.yaml`, and the live `hermes` client is allowed to switch models again so Hermes can select Guardian-served Qwen/Gemma routes.
- The live systemd backend override now points to the upstream official llama.cpp b1295 CUDA 13.2 build at `/home/flip/llama_cpp_official/worktrees/cuda132-master/build-cuda132/bin/llama-server`.
- Gemma4 31B keeps its proven full-context `262144 / ngl 60 / 0.42,0.58` runtime on b1295 by adding explicit `--flash-attn on --parallel 1 --batch-size 256 --ubatch-size 128`, reducing compute-buffer pressure without lowering the model profile.
- Relaxed same-key queue admission so one authenticated API key may own multiple waiting GPU requests while still receiving at most one running GPU slot at a time. This keeps helper/auxiliary calls from failing with duplicate-admission `409` responses while preserving per-key running-slot fairness.
- Restored explicit Qwen3.6 Hauhau reasoning runtime flags (`--reasoning on --reasoning-format deepseek`) and reintroduced the Qwen3.6 agent/reasoning aliases that share the current validated `0.36,0.64` split, so Guardian no longer relies on implicit GGUF/template defaults for thinking behavior.
- Retired the stale OpenClaw Guardian client path by removing its dedicated API key from `config/api_keys.json`; active OpenClaw config remnants were also pulled out of the live `~/.openclaw` path so dead local configs stop authenticating against Guardian.
- `scripts/sync_models.py` no longer runs `systemctl restart llama-guardian.service` after updating `config/models.yaml`. Guardian already hot-reloads the model registry, so the old restart path only risked killing active long-lived streams mid-response.
- Reworked Guardian's inference queue from a timeout-driven semaphore gate into an explicit request lifecycle state machine. Queued requests now wait safely until they run, disconnect, or are cancelled; downstream disconnects and explicit cancels now clean up waiting/running slots instead of orphaning the backend.
- Guardian queue admission is enforced per authenticated API key fingerprint instead of just the display name: one key may own multiple queued GPU requests, only one request per key may run at a time, and non-GPU `/v1/...` routes still bypass the queue entirely.
- GPU-backed inference routes now reject unknown or unserved model names before queue admission with a clear `404 model_not_served` payload, so bogus model requests never appear in the queue or operator telemetry.
- The dashboard live-request card now prefers queue-truth over telemetry-only truth for queue-managed work, so an active queued/running request still shows up even when `api_usage.active_requests` has not attached yet.
- OpenAI-compatible streaming cancel paths now translate `_GuardianRequestCancelled` into a normal client-facing cancellation response instead of leaking a proxy-side 500 when the downstream client disconnects mid-stream.
- Dashboard/API history rows for unauthenticated requests now fall back to request-derived attribution when auth never attached a validated client, so `recent_requests` still preserves source IP, agent, header, and non-secret key fingerprint details for missing/invalid-key failures and other unauthenticated API paths.
- Dashboard API usage aggregation is now keyed by authenticated `key_fingerprint` when available instead of only the display `client_id`, so shared client names no longer merge source attribution or token totals across different API keys; recent rows use the same identity-aware fallback when resolving operator-facing source labels.
- Refreshed the integration docs so client-maintainer agents have one canonical handoff path: `docs/CLIENT_INTEGRATION.md` now documents model discovery, per-key queue ownership, duplicate-admission rejects, unserved-model rejects, queue polling, and timeout guidance, with `README.md` and `docs/API_REFERENCE.md` aligned to the same contract.
- Replaced stale documentation claims that implied always-on benchmark/request optimization or broader runtime fencing than the current code actually enforces. The docs now distinguish the active queue and model-manager path from secondary or advisory surfaces such as the scaler, historical benchmark artifacts, and proxy-state VRAM scaffolding.
- Reorganized the new Guardian documentation under the top-level `docs/` directory so the repo root stays focused on the standard front-door files without introducing unnecessary nested documentation paths.

### Added
- Persistent dashboard API usage monitoring backed by `data/api_usage_state.json`, with request totals, token totals, top clients, recent activity, non-secret key fingerprints, source metadata, and restart-safe state restore on the served `:11437` UI.
- Root-level `./finetune_v2.py` operator entrypoint for Guardian finetune v2; running it without arguments now prints the usable options plus configured models and aliases before any Guardian API calls.
- Guardian API key generators now accept a custom normalized prefix, so service-specific keys like `hermes_...` can be minted without hand-editing `api_keys.json`.
- Added `docs/FINETUNE_V2_REQUIREMENTS.md`, a rewrite brief for a cleaner finetune v2 flow with explicit mode-aware ranking, layer ceilings, projector handling, split-balancing rules, acceptance criteria, and a Mermaid search-flow diagram.
- Public liveness probe `GET /healthz` (no auth) returning `{"ok": true}` for external monitors (monifuse, uptime checks). Does not reflect llama-server backend health; for that use the auth-gated `/api/status`.
- Added the tracked `llama_cpp_guardian.code-workspace` file so the intended multi-root VS Code workspace layout for Guardian, config, models, editor settings, and local llama.cpp sources is reproducible.
- Qwen3.6 Agent profile and `qwen3-35b-uncensored-agent` alias using normal llama.cpp reasoning-budget flags for low-latency tool-facing agents.
- Bounded Qwen3.6 reasoning agent profile and `qwen3-35b-reasoning-agent` alias with 65k context and a 2048-token reasoning budget for daily local-agent work.
- Gemma4 31B uncensored max-reasoning Agent Zero profile based on `TrevorJS/gemma-4-31B-it-uncensored`, with unrestricted reasoning and anti-repeat sampler settings under `gemma4-31b-uncensored-max-agent`.
- Explicit `gemma4-26b-agent` alias for the stable 26B Agent Zero route; the 31B uncensored route remains opt-in as `gemma4-31b-uncensored-max-agent`.
- Gemma4 Agent now uses the same multimodal projector as the proven OpenWebUI Gemma4 profile so Agent Zero can route image tasks through the bounded agent alias.
- Qwen3.6 Native-MTP multimodal profile plus `qwen3.6-35b-heretic-mtp`, `qwen3-35b-heretic-mtp`, and `qwen3-35b-mtp` aliases, wired to the preserved-MTP Heretic GGUF, its mmproj companion, and `--spec-type draft-mtp`.
- Guardian-native finetune suite in `app.tweaker.model_finetune` plus `scripts/finetune_model_config.py`, which binary-searches the highest stable runtime context and coarse-to-fine tests `ngl` plus two-GPU `tensor_split` candidates against live `/admin/load` probes.
- The finetune suite now persists compatible probe results in `data/model_finetune_results.json` and reuses them on later runs when the model signature and smoke-test signature still match, so already-tested `context`/`ngl`/`tensor_split` combinations are skipped instead of reloaded.
- Added `docs/HYDRO_CONTEXT_ARCHITECTURE.md`, describing a shared structured-memory and context-assembler design for Daily Grow Journal, Telegram grow assistance, root analysis, and future HydroCodo consumers.
- Added `docs/SERVER_UPGRADE_PLAN.md`, a normalized English planning document for the next Guardian host hardware upgrade based on the decoded server-upgrade note.

### Changed
- Removed the custom `qwen3_nonthinking.jinja` Qwen chat-template injection from the agent profile so Guardian no longer writes a hand-rolled prompt template into `current_model.args` for normal Qwen agent loads.
- `scripts/start_llama.sh` now honors `LLAMA_SERVER_BINARY`, allowing Guardian to pin a known-good official llama.cpp backend binary while leaving model profiles, tensor splits, and queue behavior unchanged.
- Live-pinned the host systemd runtime to official llama.cpp `b1176` after direct clean-VRAM A/B testing showed Qwen3.6 35B q4 returns corrupted output on current `b1258` but answers correctly on `b1176` with the same model settings and tensor split.
- Guardian OpenAI-compatible streams now emit lightweight SSE keepalive comments during long upstream quiet periods, so local stream clients like Hermes do not hit idle read timeouts while Guardian still enforces its own real stall watchdog.
- Guardian stream-stall watchdog warnings now include request correlation context (`request_id`, route, client, and model) so Hermes-side stream drops can be matched back to the exact stalled proxy stream instead of relying on timestamp-only log correlation.
- Streaming proxy routes now use a dynamic Guardian-side stall watchdog instead of a flat read timeout: once a stream proves healthy with non-repeating token chunks, the allowed stall window expands in bounded steps, while obviously repeating chunk loops do not earn more time.
- `.gitignore` now also ignores repo-local `scratch/` alongside `.scratch/`, and a live open-file sweep confirmed nothing currently has handles open under the tracked repo scratch tree.
- Added a second operator-focused dashboard pass on `:11437`: p95 latency insight, endpoint-level recent error breakdown, and three live sparklines for request pace, latency trend, and error trend driven from the current filtered recent-activity view.
- Gave the served `:11437` dashboard a broader operator pass: last-refresh state, manual refresh + pause controls, free-text traffic search, recent-status filtering, client/recent sort controls, traffic-mix insight cards, strongest endpoint/client callouts, progress bars for hot endpoints, sticky table headers, and stronger visual highlighting for error/slow/heavy recent requests.
- Expanded the served `:11437` dashboard API usage panel with byte counters, average latency, streaming counts, top endpoints, and richer per-client / per-request usage rows; Guardian now tracks best-effort request and response byte totals from HTTP metadata alongside tokens.
- Localhost unauthorized auth warnings now also try to resolve the offending process from the client source port, logging `local_pid` and `local_process` when Guardian can still see the live loopback connection.
- Revoked the stray `name: "-h"` key entry from `config/api_keys.json` after an exact-key sweep showed no remaining references outside the key store.
- Unauthorized auth failures now emit a single searchable warning from `app.proxy.auth`, including method/path/source details and the full presented token when a stale or invalid API key is supplied, so dead-key hunts can be traced from Guardian logs instead of masked prefixes.
- Removed hardcoded `flip_` test keys from Guardian utility scripts by centralizing script auth resolution in `scripts/_auth.py`; `test_system.py`, `verify_prompts.py`, and `test_vision_models.py` now use `GUARDIAN_API_KEY` / `GUARDIAN_TEST_KEY` or fall back to the first configured key in `config/api_keys.json`.
- Realigned `Qwen3-30B-A3B-Thinking-2507` with the official HF-native profile: Guardian now uses `context: 262144`, `ngl: 40`, and `kv_type: q4_0` instead of the old 1M `q8_0` full-offload runtime that no longer fits this dual-GPU host.
- Swapped every configured `tensor_split` and `vision_tensor_split` where GPU0's value was larger than GPU1's, so Guardian's model configs now bias allocation toward the larger second GPU.
- Removed stale broad-sweep benchmark download entries whose local GGUF files are no longer present, leaving `config/benchmark_models.json` empty until new benchmark targets are explicitly added.
- Clarified public Qwen aliases: `qwen3` now resolves to the Qwen 3.0 30B Thinking runtime, while Qwen 3.6 routes use explicit `qwen3.6-*` aliases and the old misleading `qwen3-35b-*` aliases were removed.
- The Hauhau Qwen3.6 text runtime now also uses the live-validated `tensor_split: "0.36,0.64"`, so text-mode Guardian launches write an explicit `--tensor-split` instead of relying on llama.cpp auto placement.
- Live Gemma text finetuning now applies the proven `262144 / ngl 60 / 0.42,0.58` runtime for `gemma-4-31B-it-uncensored-heretic`, replacing the old overloaded `0.62,0.38` split that failed on GPU0; the smaller `gemma-4-E4B-it-uncensored` profile was also applied at `131072 / ngl 42 / 0.32,0.68`.
- `scripts/finetune_v2_model_config.py` is now a compatibility wrapper around the shared v2 CLI module so the root entrypoint and legacy script path cannot drift.
- Finetune v2 now flushes every appended probe to the configured main `--results-file` as well as the `.active` sidecar, and CLI runtime errors print the results-file path so operators can watch the right log during live runs.
- Explicit `--split` values now seed `start_ngl` ladder mode instead of being bypassed by the default split planner, so live ceiling checks start from the operator-provided GPU-order-aware split.
- The live Hauhau Qwen3.6 full-context vision run now applies the proven `262144 / ngl 40 / 0.36,0.64` runtime for `Qwen3.6-35B-A3B-HauhauCS-Aggressive`; the run confirmed that PCI-ordered GPU0 maps to the RTX 3060 and GPU1 maps to the RTX 5060 Ti, so the correct direction shifts load toward GPU1 rather than using the old `0.54,0.46` shape.
- `.gitignore` now ignores `.scratch/` so temporary synced-main worktrees and local probe scratch space stop polluting repo status during live finetune validation.
- Moved the outdated broad-sweep `BenchmarkSuite` out of the active runtime path to `app/tweaker/legacy/benchmark_suite_v1.py`, moved its quick-test script under `scripts/legacy/`, disabled the start/stop benchmark API actions, and left `/api/benchmark` as a read-only summary for historical result files.
- The finetune v2 contract helper now rejects malformed fixture booleans and non-string `error` payloads instead of silently coercing them, and the wrapper smoke test now has an explicit timeout so CI cannot hang indefinitely in the subprocess path.
- `docs/FINETUNE_V2_REQUIREMENTS.md` now explicitly defines Guardian finetune as
	host-specific runtime tuning (`context` / `ngl` / `tensor_split` and vision
	projector fit), not model-weight training, so the rewrite brief starts from
	the operator outcome it is meant to deliver.
- The finetune rewrite plan is now explicit: v2 must treat split balance as a search heuristic instead of a global winner-selection override, compute balancing only from the latest successful probe, use documented lexicographic comparators for context/speed modes, reintroduce fixed `--context` / `--ngl` constraints under optimization-led defaults, and cap low-headroom follow-up search to 5 probes once both GPUs are below `750 MiB` free unless the runtime is already at max `context` and max `ngl`.
- The finetune CLI now exposes `--optimization {speed,context,balanced}` instead of manual `--min/max-context` and `--min/max-ngl` range flags, and result selection now applies the requested speed-vs-context policy only after the split has been rebalanced from measured per-GPU free-VRAM data.
- The finetune CLI no longer accepts explicit `--ngl` candidate overrides; `ngl` is now always auto-tuned via the search flow, and there is still no manual `--context` override in the current CLI.
- The Guardian finetune auto-search now runs as a strict 3-phase flow with proactive per-GPU VRAM balancing: safe-baseline split calibration first, `ngl` step-down with split rebalancing after each successful change second, and context bisection last with split rebalanced again for every context candidate.
- Guardian no longer exposes per-model backend selection; runtime launches now always target the official llama.cpp binary so stale fork plumbing does not linger in the config contract.
- Guardian no longer writes or reads `config/current_model.binary`, and the public vision metadata no longer pretends there is a selectable backend field when the runtime is official-only.
- A fresh Guardian-only text rerun for `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` proved that the text runtime can stay at the full native `262144` window with `ngl: 99` once the split is rebalanced to `0.61,0.39`; the previous lower-offload text assumption was stale and only the vision runtime still needs the separate `vision_ngl: 36` profile.
- A follow-up Guardian-native `--optimization context` vision rerun for `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` proved that the full `262144` vision window also fits on this host at `vision_ngl: 32`, and the applied winning split returned to the balanced `0.50,0.50` shape once higher-offload seed probes had been eliminated.
- Model entries can now declare `total_layers`, and Guardian's finetune search clamps `ngl` exploration to that backbone-layer ceiling instead of wasting probes above the GGUF's real block count.
- `data/model_finetune_results.json` now shows only the actually tested `ngl` and tensor-split values for the active run instead of dumping prebuilt candidate arrays before those probes happen.
- Centralized repo-sensitive filesystem paths in `app.paths` and `scripts/_paths.py`, so `ModelManager`, `start_llama.sh`, utility scripts, and tests now resolve from the checkout root or environment overrides instead of assuming `/home/flip/llama_cpp_guardian`.
- Simplified `config/models.yaml` to one runtime entry per remaining GGUF family, removed stale deleted-model paths, stripped `-nkvo`, and moved agent/deep/max-style behavior back to per-request API parameters instead of duplicate config profiles.
- Raised surviving runtime contexts to the highest repo-documented safe values where empirical benchmark evidence existed, while leaving unproven families on their existing runtime limits.
- Corrected the Qwen3.6 uncensored runtime rollback: the historical q4 benchmark at `131072` was valid, and the later `65536` result was a false negative caused by forcing an explicit tensor split during re-validation.
- Restored `qwen3-35b-uncensored` to `context: 131072`, restored `benchmark_context_limit` to the model's metadata ceiling `262144`, and removed the explicit tensor split from that runtime entry.
- Re-ran the Qwen3.6 context search through Guardian itself (`/admin/load` + live chat) instead of standalone backend launches, proved `262144` as the stable default runtime, measured `524288` as the highest stable Guardian load/runtime headroom on this host, and observed runtime instability by `540672` with load failures from `557056` upward.
- Retuned `gemma-4-31B-it-uncensored-heretic` for the current dual-GPU host after Guardian proved the old `context: 262144` / `tensor_split: "0.55,0.45"` profile could not fit on the RTX 3060. With the improved split `0.62,0.38`, the profile's tiny-request ceiling reached `196096` before failing at `196608`, while a heavier `~12k`-token prompt stayed stable at `190464` and failed by `191488`. The runtime config now uses the last practically proven value `context: 190464`.
- Kept `qwen3-35b-uncensored` as the unrestricted deep-reasoning alias while adding a bounded 65k-context agent variant for Agent Zero/OpenAI-compatible tool clients.
- Raised both Qwen3.6 CrewAI/agent-facing aliases (`qwen3-35b-uncensored-agent` and `qwen3-35b-reasoning-agent`) from 65k to their full 131072-token context so long CrewAI traces stop tripping Guardian with context-overflow 400s.
- Restored `gemma4-agent` to the stable 26B Agent Zero profile after the 31B uncensored route proved too slow for default AZ work.
- Synced the local official `llama.cpp` backend to upstream `master` and rebuilt it with `GGML_CUDA_GRAPHS=OFF` plus `GGML_CUDA_NO_PEER_COPY=ON`, which exposes upstream `draft-mtp` support without regressing the mixed 3060 + 5060 Ti host.
- Tuned the Native-MTP Qwen3.6 multimodal runtime for this host to `context: 196608`, `ngl: 36`, and `tensor_split: "0.55,0.45"` after full-GPU loads failed from extra MTP/mmproj buffer pressure.
- Vision-capable runtime entries can now keep separate text and `vision_*` tuning fields, Guardian only loads `mmproj` when the request actually contains image input, and the finetune CLI can target `--runtime-mode text|vision` while searching a wider default split range.
- Focused live vision reruns for `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` proved that this host can sustain the full `262144` window at `vision_ngl: 39`; the runtime default now moves to `vision_tensor_split: "0.54,0.46"`, which was the fastest measured success among the proven `0.54/0.55/0.56` neighbor splits.
- Restored a configured vision path for the non-MTP `Qwen3.6-35B-A3B-HauhauCS-Aggressive` profile using the shared Qwen projector plus the last proven full-context vision runtime `262144 / 38 / 0.48,0.52`; the older live vision evidence existed only in `data/model_finetune_results.json`, which is why this model had no dedicated `vision_*` config despite already being tested.
- Dedicated Hauhau vision v2 reruns now have their own finalized result log in `data/model_finetune_v2_results.live.hauhau.vision.main.json`; the clean three-candidate rerun reconfirmed `262144 / 38 / 0.48,0.52` as the configured default, while the wider interrupted sweep plus Guardian crash logs pinned the OOM cliff at `0.47,0.53` and `0.46,0.54`.
- Finetune v2 probe logs now persist the raw per-GPU `gpu_vram` snapshot alongside summarized `free_vram_mib`, so identical headroom rows can be recognized as a real `nvidia-smi` plateau instead of looking like a logging bug.
- Finetune v2 now prioritizes local split balancing before trying higher `ngl` retries, using per-GPU free-VRAM percentage deltas to keep rebalancing on the current successful runtime until the GPUs are close enough in free headroom.
- The finetune results log now writes an in-progress run entry immediately and flushes every individual probe to `data/model_finetune_results.json`, so long live searches can be monitored while they are still running or interrupted mid-run.

### Fixed
- The OpenAI-compatible and Ollama-compatible queued inference routes now share the same disconnect-aware request cleanup and request outcome tracking, preventing dead clients from leaving zombie queue entries or holding the single-slot backend hostage.
- `/v1/models/{model_id}` now resolves Guardian public aliases locally instead of forwarding alias lookups to llama-server, so renamed aliases such as `qwen3.6-35b-uncensored` return metadata instead of backend errors.
- Timeout tiering now recognizes `35B` and `31B` model names as large runtimes instead of falling back to the tiny-model heuristic, so those streams start from a saner base timeout before the new dynamic watchdog expands it.
- Guardian startup and proxy recovery now treat `__MISMATCH__` as an internal sentinel only: startup adopts a known live backend when no model pin is active, failed forced switches restore a real target model, and auto-reload/connect-error recovery resolves to a configured model instead of trying to load `__MISMATCH__` and returning persistent 503s.
- Restored the missing served dashboard monitoring panels on `:11437`; the static UI now consumes `/api/stats` API usage snapshots instead of only showing the older VRAM/cache/benchmark cards.
- `/v1/models` now advertises `input_modalities: ["text", "image"]` for configured multimodal runtimes that are still `unverified`, so OpenCode and other clients do not incorrectly reject image attachments before the first live vision probe has marked the model `supported`.
- Finetune v2 same-bucket handling now follows llama.cpp's per-layer allocation behavior: when adjacent effective tensor splits land in the same backend VRAM bucket, the planner keeps stepping in the same measured direction (`0.39 -> 0.38 -> 0.37 -> ...`) until the bucket changes, a probe fails, or bounds are exhausted, instead of falling back to unrelated centered splits.
- Finetune v2 now automatically probes immediate `±1%` tensor-split neighbors after a critically low-headroom success, so per-model split refinement no longer stops at the first coarse rebalance hit.
- Finetune v2 now retries a failed local split-rebalance with a smaller step on the same `context/ngl` before falling back to a lower `ngl`, so balance-first searches do not abandon the local split path after one coarse OOM.
- Finetune v2 now retries a failed stepped-down seed on the same `context/ngl` with OOM-aware split candidates before falling back to a lower `ngl`, so `40 -> 39` searches do not immediately drop below a historically viable `ngl` just because the first `39` split was wrong.
- Finetune v2 now supports a `start_ngl` ladder mode for live ceiling checks: seed at a lower rung such as `37`, rebalance that rung until the GPUs are near-even, then climb to `38`, rebalance again, and continue upward without the old broad candidate grid cutting in front of the ladder.
- Finetune v2 ladder-mode rebalancing now keeps searching for the next smaller untried split when the obvious coarse rebalance target was already attempted, so a successful `same_ngl_failure_split_retry` does not prematurely exhaust the queue before the rung is actually balanced.
- Finetune v2 now falls back to the remaining untried splits on the current rung when the directed rebalance path is exhausted, so ladder runs do not exit with `search_not_converged` while valid same-`ngl` split candidates still exist.
- The extra same-rung fallback is now scoped to `start_ngl` ladder runs only, and finetune-v2 has regression coverage that the canonical results log appends completed runs instead of overwriting history.
- Live Hauhau ladder proof from `start_ngl=37` exhausted rung 37 until a balanced-enough `0.55,0.45`, then climbed to `ngl 38` and selected `vision_tensor_split: "0.54,0.46"` as the best proven Hauhau vision runtime at `context: 262144`.
- Finetune v2 now records `backend_gpu_vram` from the active `llama-server` process and `effective_tensor_split` from `current_model.args` for every live probe. The unchanged-split guard now aborts only when the effective split fails to change; when adjacent effective splits land in the same backend VRAM allocation bucket, the runner treats it as a real llama.cpp bucket plateau and keeps searching the rung for a split that changes allocation.
- Patched the local official llama.cpp layer split assignment to use midpoint layer placement instead of lower-edge placement, so adjacent Hauhau vision splits like `0.54,0.46` and `0.53,0.47` now produce different backend VRAM allocations at `262144 / ngl 38`.
- Finetune v2 now reverses split-search direction when an adjacent successful split makes VRAM balance worse, so a degrading step such as `0.54,0.46 -> 0.53,0.47` queues `0.55,0.45` next instead of continuing farther in the bad direction.
- Finetune v2 telemetry is now llama/CUDA-device aware: host and backend VRAM snapshots are keyed by stable llama ordinal order derived from PCI bus ID or `CUDA_VISIBLE_DEVICES`, numeric visible-device tokens are interpreted in PCI order, `start_llama.sh` defaults `CUDA_DEVICE_ORDER=PCI_BUS_ID`, and the deprecated v1 finetune module was moved under `app/tweaker/legacy/` so active v2 code no longer imports it.
- Finetune v2 now consumes local split rebalance/refinement follow-ups before trying a higher `ngl`, prunes stale lower-rung split follow-ups once a balanced rung climbs, and has a full-flow regression test that proves baseline success -> split rebalance -> fine split refinement -> higher `ngl` retry -> split rebalance without relying on artificial split bounds.
- Finetune v2 no longer stops immediately on a fixed-shape `max_context_and_ngl` success when that same success queued a split-rebalance follow-up; the runner now executes the queued probe first so worse asymmetric splits can be disqualified before convergence ends the run.
- Non-applying finetune v2 runs now restore the requested model's disk runtime after probing, including failure paths, so a late OOM candidate cannot leave `current_model.args` and the live `llama-server` stranded on the last attempted override.
- A post-restart live vision rerun for `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` confirmed that `0.50,0.50` remains the correct `262144 / 32` split on this host; `0.45,0.55` drops GPU0 headroom to `19 MiB`, and `0.40,0.60` fails to load with CUDA OOM.
- Fixed a merge-conflict regression in `app/tweaker/finetune_v2_runner.py` where a duplicated `start_run()` block made the merged finetune v2 path fail at import time with `SyntaxError: positional argument follows keyword argument`.
- Context-mode finetuning no longer keeps chasing alternate split candidates after a failed seed probe during either baseline calibration or later context evaluation; failed context-mode probes now hand control back to the broader ngl/context search, and split balancing only resumes after a successful probe.
- Restored the proven interim multimodal recovery baseline for `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` in `models.yaml` after drift had pushed the vision path back to `vision_ngl: 99` with `vision_tensor_split: "0.50,0.50"`, which caused Guardian 503 auto-reload failures and llama-server segfaults on image requests. That recovery baseline put the vision path back on a known-good `262144 / 36 / 0.55,0.45` shape before the final context rerun.
- The final context-mode rerun then replaced that recovery baseline with the better stable vision runtime `262144 / 32 / 0.50,0.50`; split rebalancing now resumes only after a successful seed probe instead of chasing alternate splits while the current `ngl` still fails to load.
- Multimodal `mmproj` files no longer influence the `ngl` ceiling logic by assumption; upstream llama.cpp handles projectors through separate `mmproj_use_gpu` loading rather than the main model's `n_gpu_layers` count.
- Guardian crash records now retain the resolved runtime mode plus the exact effective runtime config that was launched, so future load/reload failures immediately show the attempted `context`, `ngl`, `tensor_split`, and `mmproj` shape in `crash_details.config_snapshot`.
- Tensor-split rebalancing now skips a 2% move and goes straight to 1% when the GPU that would receive more load has under 1 GiB free, avoiding low-value coarse probes that are effectively dead on arrival for the current host/model shape.
- Speed-mode frontier search now also tightens its local context bisection when both GPUs are under 500 MiB free or any single GPU is under 100 MiB free, so low-headroom runs stop making broad post-frontier context jumps and probe with smaller local steps instead.
- `--optimization speed` now stops broad re-search once it reaches a narrow success/fail frontier and instead tries a local 1% split refinement near that edge, which cuts out low-value repeats like re-testing far-away `262144` / `172032` contexts for every alternate split.
- Finetune probe-cache reuse no longer depends on the exact short smoke success marker text, so reruns with the same runtime shape and image settings can reuse prior probes even if the operator changes `SPEED_OK_*` wording.
- Cached finetune probe reuse now preserves the original `gpu_vram` and `free_vram_delta_pct` telemetry too, so low-headroom split/context heuristics still have the VRAM evidence they need on reruns instead of going blind after a cache hit.
- Cached probe indexing now also merges duplicate history entries instead of letting a later cached replay with `gpu_vram: null` overwrite an older live probe with real telemetry, so reruns keep the richest VRAM data for identical `context` / `ngl` / `tensor_split` combinations.
- Finetune probe logs now tag VRAM snapshots with their capture phase. Non-200 `/admin/load` failures persist the `pre_load` snapshot instead of a misleading post-crash reading, while successful smoke checks keep the normal post-smoke VRAM telemetry.
- Tensor-split rebalancing now retries the 1% midpoint after a failed 2% move, so speed/context tuning records the intended `0.55 -> 0.53 -> 0.54` fallback path instead of stopping at the first failed coarse rebalance probe.
- `--optimization speed` no longer burns time recalibrating tensor splits on already-failed high-context probes; it now halves the context range first, then rebalances split only after a successful lower-context fit before trying upward again.
- Finetune result ranking now prefers measured VRAM-balance deltas over naive distance-to-50/50 when two successful tensor splits compete, which keeps asymmetric dual-GPU hosts from "winning" on the wrong split just because a ratio looks more centered.
- Removed the last tracked hardcoded underscore-checkout paths from Guardian scripts, tests, and helper utilities so a future rename toward the canonical `llama-cpp-guardian` style no longer requires code edits.
- Guardian crash parsing now scans a wider recent `llama-server` journal window and recognizes llama.cpp fit-target failures, compute-buffer initialization failures, and CUDA OOM signatures instead of collapsing them into `Unknown error (no recognizable error pattern in logs)`.
- Guardian now validates multimodal runtime support per model instead of assuming any `mmproj` config is vision-ready, exposes that status through `/v1/models`, and returns explicit 4xx/503 OpenAI-style errors for broken image paths instead of leaking raw 500s.
- Guardian now starts answering on `11434` immediately after restart by running startup model verification in the background instead of holding FastAPI startup open until `llama-server` on `11440` finishes warming up.
- Guardian no longer kills `systemctl --user restart llama-guardian-live.service` because of a momentarily live `guardian.pid`; the PID-file guard now overwrites old entries and relies on socket binding to reject real duplicate listeners.
- `/admin/load` now accepts public model aliases such as `qwen3-35b-uncensored` and serializes manual loads behind the shared model-switch lock so operator-triggered loads cannot race the background startup check.
- Guardian runtime status now uses generation-tracked operation snapshots, so an older background startup task cannot overwrite a newer manual load or auto-switch status in `/api/status`.
- `/api/status` now exposes richer proxy/routing diagnostics, including the live listener owner, pid-file state, preferred tool/reasoning models, and backend verification metadata for faster live debugging.
- `/api/status` now also exposes explicit switch-state diagnostics (`pending`/`checking`/`switching`/`ready`), queue state, current requested target, switch owner, and the last successful backend verification timestamp.
- Auto-routed inference requests now prefer a tool-friendly sibling profile when the current family is an unbounded reasoning model, which keeps `model: auto` practical for tool clients without changing explicit model requests.
- Ollama-compatible `/api/chat` and `/api/generate` responses now fall back to `reasoning_content` when a reasoning model emits no visible `content`, so tool clients no longer see a misleading empty answer.
- The live integration suite now includes a restart-race regression that restarts the active Guardian systemd unit, immediately issues `/admin/load` with an alias, checks `/api/status`, and runs a mini chat request.
- `/api/status` now exposes a `startup` object so authenticated clients can tell whether Guardian is still verifying the backend model, already ready, or ended startup with an error.
- Claude Code can now switch Guardian models with its dedicated `claudecode_*` API key instead of inheriting whichever sibling app model was already loaded, which prevented restarts from getting stuck on NerveSplat's lighter `gemma4-e4b` runtime.
- `ModelManager.resolve_model()` and the public model-map path now refresh the in-memory registry before resolving aliases, so freshly added `models.yaml` entries work through `/admin/load` and `/v1/models` without a Guardian restart.
- The finetune suite now restores the original `models.yaml` state on failure, retries transient Guardian transport errors, and correctly replaces only the targeted model block instead of swallowing top-level YAML sections such as `aliases:`.
- The finetune CLI now has an explicit auto-bounds mode for context search, derives sensible defaults from the active runtime config when bounds are omitted, and records the effective search range in the results log.
- The finetune objective is now `context > split balance > ngl`, and once a max-context combination is found the search stops retesting lower contexts for later combinations.
- A full Guardian-native multimodal finetune pass re-validated `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` at `context: 262144`; under the new objective the winning full-context config is `ngl: 36` with the more balanced `tensor_split: "0.55,0.45"`, and a repeat run confirmed the results-file cache returns `cached: true` for previously tested combinations.
- A follow-up full-context `ngl` sweep for `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved` confirmed that `ngl: 36` remains the correct `262144` runtime on this host; higher `ngl` values such as `52` and `68` only fit after the context drops into the `188k` range.
- Text-only requests to vision-capable models no longer force `--mmproj`, and the same canonical model can now hot-reload between text and vision runtime mode when Guardian sees image input appear or disappear.
- Anthropic-compatible clients such as Claude Code now authenticate successfully through Guardian because the proxy accepts `x-api-key` and `api-key` headers in addition to OpenAI-style `Authorization: Bearer` tokens.
- OpenAI-compatible inference requests now detect a stale stopped `llama-server` backend, reload the active model once, and retry instead of leaking an ASGI 500 traceback to Agent Zero/LiteLLM clients.
- Startup model detection now distinguishes profiles that share the same GGUF path by matching generated runtime args, preventing the non-thinking Qwen agent profile from being mistaken for the deep-reasoning profile after Guardian restarts.
- Forced a live unload/reload of the Qwen3.6 reasoning agent after the context bump and verified Guardian rewrote `current_model.args` to `-c 131072`, confirming the hotfix was actually live instead of only sitting in YAML.
- `/v1/models` now includes configured aliases alongside canonical model names, so clients that talk to Guardian through IDs such as `qwen3-35b-uncensored` can resolve metadata for the exact model string they send.
- Guardian runtime sizing now treats `context` as the only active runtime window; `benchmark_context_limit` is treated as a separate benchmark or paper ceiling instead of feeding the advertised runtime headroom calculation.
- `/v1/models` now exposes the benchmark ceiling under the clearer `benchmark_context_limit` field alongside the configured runtime `context` and the conservative `advertised_context` headroom field.
- Claude Code specifically still receives the conservative `advertised_context` value through the OpenAI-compatible `max_context` response field because this Claude build compacts against that field only; the response keeps the explicit runtime and benchmark fields visible next to that compatibility override.

### Removed
- Removed stale `Qwen3.6-35B-A3B` and `gemma-4-31B-it` registry entries whose GGUF files no longer exist locally.
- Removed duplicate `-Agent`, `-Deep`, and `-Max-Agent` model entries and the aliases that only existed to target those duplicate profiles.

## [2026-05-06] - Model Registry Cleanup, Qwen 3.6, Gemma Deep, and Load Guard

### Added
- **Qwen3.6 uncensored profile**: Registered `Qwen3.6-35B-A3B-HauhauCS-Aggressive` with 131k context, GPU KV offload enabled, unrestricted reasoning, and `qwen3-35b-uncensored` alias.
- **Gemma4 Heretic Deep profile**: Added `gemma-4-31B-it-uncensored-heretic-Deep` / `gemma4-heretic-deep` as a text-focused reasoning profile with 216k context, GPU KV offload enabled, unrestricted reasoning, and no multimodal projection overhead.
- **Gemma4 E4B profile**: Added `gemma-4-E4B-it-uncensored` / `gemma4-e4b` for a smaller text-only Gemma profile.

### Changed
- **Gemma Deep VRAM tuning**: Tuned the Deep profile to `context: 216064` and `tensor_split: "0.62,0.38"`, leaving measured runtime headroom on both GPUs while using substantially more context than the initial 131k load.
- **Model registry cleanup**: Removed obsolete GLM 4.7 entries and aliases after the GLM models were retired from the local Guardian registry.
- **Alias cleanup**: Removed orphaned aliases that pointed at deleted model entries.
- **README model examples**: Updated `config/models.yaml` documentation to reflect the current Qwen/Gemma registry instead of retired GLM examples.

### Fixed
- **Admin load idle-unload race**: `/admin/load` now increments `active_requests` and refreshes `last_request_time` during model loads so the idle-unload watcher does not terminate `llama-server` in the middle of heavy loads.

### Verified
- `models.yaml` parses successfully and all aliases resolve to existing model entries.
- `gemma-4-31B-it-uncensored-heretic-Deep` loads through Guardian at 216k context with backend health reporting `true`.
- Short `/v1/chat/completions` smoke test succeeds through Guardian on the Gemma Deep profile.

## [2026-04-17] - Backend Strategy Flip, Middleware Rebrand & Documentation Overhaul

### Changed
- **Backend strategy flipped**: Official llama.cpp is the documented and default backend. `DEFAULT_BACKEND` changed to `"official"` in `manager.py`.
- **Middleware rebrand**: Guardian is now positioned as middleware (not proxy). Logger renamed from `"Proxy"` to `"Guardian"` in `server.py`.
- **3rd-party GPU process awareness**: Replaced Frigate-specific language with generalized "3rd-party GPU process" awareness throughout configuration and documentation.
- **models.yaml cleanup**: Removed explicit `backend: official` from all 10 models that had it — they now use the default (official).
- **README.md**: Complete rewrite — middleware positioning, queue system documentation, dual backend strategy, 3rd-party GPU awareness, full API reference, directory structure.
- **ARCHITECTURE.md**: Complete rewrite — detailed queue architecture, cooperative VRAM management, backend selection, GPU strategy, timeout tiers, model lifecycle flows.
- **CLIENT_INTEGRATION.md**: Updated heading to reflect middleware terminology.

### Added
- GitHub issue #1: 5-phase roadmap for Guardian improvements (backend flip, middleware rebrand, 3rd-party awareness, docs, future roadmap).

## [2026-03-31] - Cooperative VRAM Management & Documentation Overhaul

### Added
- **Cooperative VRAM management**: Guardian now calls ComfyUI's `POST /free` API to request graceful VRAM release before loading models. ComfyUI stays alive and auto-reloads models on next workflow.
- **`_request_comfyui_free()`**: New method in `ModelManager` that sends `{"unload_models": true, "free_memory": true}` to `http://127.0.0.1:8188/free` with 10s timeout and graceful error handling.
- **`_free_gpu_memory()`**: Orchestrator method that coordinates VRAM cleanup from coexisting services before model loads.
- **Hydroponics API key**: Added `hydro_` prefixed key for Mycodo/Pi4 nutrient automation integration.

### Changed
- **README.md**: Complete rewrite with full API reference table, directory structure, cooperative VRAM management docs, GPU configuration details, and all current features.
- **ARCHITECTURE.md**: Complete rewrite reflecting cooperative VRAM management (ComfyUI /free integration), VramScheduler, timeout tiers, backend verification flow, model switch sequence diagram, and implementation notes.
- **Model load flow**: `load()` and `switch_model()` now call `_free_gpu_memory()` before `_start_server()` to ensure VRAM availability.

### Design Decision
- **Cooperative over destructive**: Instead of killing GPU processes (ComfyUI, etc.), Guardian politely requests VRAM release via API calls. This preserves service uptime and lets ComfyUI auto-recover its models on the next workflow execution.

## [2026-02-16] - Comprehensive Code Review & Multi-GPU Fixes

### Fixed (CRITICAL)
- **Unreachable code in `get_model_size()`**: `return 8000` was placed before embed/0.5b checks, causing embed models (e.g., nomic-embed) to report 8000MB instead of 500MB.
- **Default model `"glm-4"` didn't exist**: Changed to `"GLM-4.7-Flash"` to match actual `models.yaml` key.
- **Benchmark suite non-functional**: Was using Ollama `/api/generate` endpoint (404 on llama-server). Migrated to `/v1/chat/completions` with OpenAI-format response parsing.
- **Benchmark model names**: Were Ollama-style (`deepseek-r1:32b`). Now loaded dynamically from `models.yaml`.
- **Model switch race condition**: Added `asyncio.Lock()` to prevent concurrent model switches from colliding.

### Fixed (IMPORTANT)
- **Dead config `vram_limit_mb`**: `settings.yaml` value (27000) was never read — `server.py` hardcoded 26000. Now properly loaded from config.
- **Dead config `proxy.port` and `proxy.target`**: Documented as config-driven but were hardcoded. `vram_limit_mb` now wired; port/target remain hardcoded (intentional).
- **Scheduler ignored `settings.yaml`**: Hours, days, and services were hardcoded. Now reads `benchmark.schedule` and `services_to_stop` from config.
- **`manage_service()` was a no-op**: `subprocess.run()` was commented out. Re-enabled with timeout protection.
- **Unauthenticated endpoints**: `/api/tags` and `/api/version` bypassed API key auth. Fixed.
- **Benchmark blocked event loop**: Sync `requests.post()` inside async `run_suite()`. Fixed via `asyncio.to_thread()` + migrated from `requests` to `httpx`.

### Added
- **`tensor_split` for all >12GB models**: 16 models configured with multi-GPU weight distribution (`0.55,0.45` for ≤19GB, `0.45,0.55` for >20GB). Enables coexistence with Frigate NVR on GPU 1.
- **`_model_switch_lock`**: Global asyncio lock prevents concurrent model switches across `/api/chat` and `/v1/chat/completions`.

### Removed
- Unused imports: `secrets`, `base64`, `BackgroundTask`, `HTTPBasic`, `HTTPBasicCredentials`
- Dead constants: `DEFAULT_CONTEXT_SIZE`, `MAX_CONCURRENT_REQUESTS`, `MAX_REQUEST_TIMEOUT`, `STATS_FILE`, `CLIENTS_FILE`
- Dead functions: `unload_model()` (used Ollama API), `update_model_stats()` (no-op), `check_and_free_vram()` (no-op)
- Stale `# ...existing code...` placeholder comments

### Changed
- **`start_llama.sh`**: Fixed default model filename from `GLM-4.7-Flash-Q4_K_M-latest.gguf` to `GLM-4.7-Flash-Q4_K_M.gguf`.
- **`settings.yaml`**: Cleaned dead `benchmark.models` list (now loaded from `models.yaml`), added VRAM documentation comments.
- **README.md**: Complete rewrite reflecting current architecture, dual-backend system, multi-GPU setup, and all features.

## [2026-02-14] - Refactor to Llama Server

### Changed
- **Ollama to Llama Server**: Renamed all component references from "Ollama" to "Llama Server" to reflect the backend change.
- **Port standardization**: Default internal Llama Server port updated to 11440.
- **Environment Variables**: Renamed `OLLAMA_URL` and similar vars to `LLAMA_SERVER_URL`.
- **Legacy Cleanup**: Removed deprecated `configure_ollama.sh` and `modelfile_template.txt`.
- **VRAM Logic**: Disabled legacy `check_and_free_vram` in favor of new manager.

## [Unreleased] - 2025-12-21

### Added
- **Configurable Timeout Tiers**: Timeout values per model tier are now configurable in `config/settings.yaml` under `timeouts.tiers`. Each tier has `min_size_mb` and `timeout_seconds` settings.
- **Benchmark Visualization in UI**: Dashboard now visualizes benchmark results (best TPS per model + last-run metadata) via a new `/api/benchmark` endpoint.
- **Manual Benchmark Control**: Added `/api/benchmark/start` and `/api/benchmark/stop` to run benchmarks on-demand.

### Changed
- **Dynamic Timeouts**: Refactored `get_model_timeout()` to read from config file instead of hardcoded values. Supports hot-reload via config file changes.
- **Benchmark Resuming Behavior**: Benchmark queue is regenerated from current settings and filtered by completed tests to avoid no-op runs when the persisted queue is empty/stale.

---

## [2025-12-03]

### Added
- **Feedback Loop**: Implemented `RequestOptimizer` which injects the best `num_ctx` and `num_batch` settings from `benchmark_results.json` into incoming requests.
- **Smart Combo Caching**: Implemented LRU (Least Recently Used) eviction policy. Models are only unloaded if VRAM is actually needed.
- **Multi-GPU Support**: Updated VRAM monitoring to sum memory across all available GPUs.
- **Triple Hit Verification**: Added `scripts/test_combo.py` to verify concurrent model loading.
- **Dashboard UI**: Real-time monitoring dashboard on port 11437 (Dark Mode, Tailwind).
- **Record Alerts**: Benchmark suite now logs "🏆 NEW RECORD" when TPS improves.
- **API Stats**: Added `/api/stats` endpoint for frontend integration.
- **Architecture Docs**: Updated `ARCHITECTURE.md` with port mappings and flow diagrams.

### Fixed
- **Service Architecture**: Moved Guardian to port 11435 to avoid conflict with Nginx (which proxies 11434 -> 11435).
- **Crash Loop**: Fixed missing imports and initialization errors in `app/proxy/server.py`.
- **VRAM Monitoring**: Replaced static estimates with real-time `nvidia-smi` queries.

### Changed
- **Port Migration**: Guardian now listens on port 11434 (replacing Nginx/Ollama default).
- **Nginx**: Disabled Nginx Ollama config to allow Guardian to take over the entry port.
- **Architecture**: Simplified flow: Client -> Guardian (11434) -> Ollama (11436).
