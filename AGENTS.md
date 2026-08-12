# AGENTS.md — Llama-CPP Guardian

> Canonical AI-agent context for this repo. Read first.
> Claude Code: `CLAUDE.md` → here. Goose: `.goosehints` → here. Copilot: `.github/copilot-instructions.md` references this.

## Stack

- **Language:** Python 3.14 (venv at `./venv`)
- **Web:** FastAPI + uvicorn + httpx
- **Backend:** llama.cpp (`llama-server` on `:11440`, launched via `scripts/start_llama.sh`)
- **Frontend:** React/Vite/Tailwind dashboard on `:11437` (`dashboard/`), bound to `127.0.0.1`
- **Config:** `config/settings.yaml` (proxy, providers, queue, timeouts), `config/models.yaml` (model registry + aliases), `config/api_keys.json` (named API keys)
- **Secrets:** `.env` — `${VAR}` expansion in YAML; never commit secrets
- **Deploy:** systemd unit `llama-guardian.service`; nginx exposes the public API on `:11434`.
- **TLS:** nginx stream TLS preread multiplexes both `http://192.168.1.G:11434` and `https://192.168.1.G:11434`. It passes TLS unchanged to Guardian on `127.0.0.1:11435` and routes plain HTTP through nginx on `127.0.0.1:11436`. See `deploy/nginx/llama-guardian-protocol-mux.conf` and `deploy/nginx/llama-guardian-loopback-http.conf`.
- **TLS trust:** this host trusts the Guardian certificate through `/usr/local/share/ca-certificates/llama-guardian-192.168.1.G.crt`. Other LAN clients must trust that same certificate before connecting without a custom CA setting.
- **Tests:** pytest (`tests/`, `asyncio_mode=auto`)

## Critical rules

- **Test before claiming fixed:** `./venv/bin/python -m py_compile <file>` then run `./venv/bin/python -m pytest tests/ -x`. Never claim a fix works without verifying.
- **No hot reload.** The provider registry, queue, and server load at startup. Code changes require `sudo systemctl restart llama-guardian` to take effect. Config-file edits to `settings.yaml` also need a restart (the "hot reload" claim in older docs is incorrect).
- **The agent routes through Guardian — restarting cuts the agent's own model traffic.** This agent harness (Claude Code / goose / pi) reaches its model *through this very service* (nginx `:11434` → TLS `:11435` → app). A `sudo systemctl restart llama-guardian` therefore silences the current session until startup completes; a code/config error that prevents startup is **not self-healable** — the agent's model is unreachable, so it cannot fix its own mistake. Before any restart: (1) validate with `py_compile` + focused pytest, (2) tell the operator a restart is coming and the session will drop, (3) let the operator run the restart from outside the session, (4) if startup fails, the operator must revert (`git stash`/`git checkout` on `app/`, restore previous `settings.yaml`) — never promise in-session recovery. **Known recovery path (proven 2026-08-12):** the operator enables `gh copilot` (routes around Guardian) and uses it to inspect/repair/restart Guardian while the pi session is down.
- **TLS requires both paths.** `GUARDIAN_TLS_CERTFILE` and `GUARDIAN_TLS_KEYFILE` are an all-or-nothing pair. The production drop-in binds TLS to `127.0.0.1:11435` through `GUARDIAN_TLS_HOST` and `GUARDIAN_TLS_PORT`; nginx's `libnginx-mod-stream` module and a top-level `stream { include /etc/nginx/stream-conf.d/*.conf; }` block are required for the public protocol multiplexer. Keep the private key `0600`.
- **Secrets in `.env`.** API keys use `${ENV_VAR}` expansion. Never inline keys in YAML or Python. Use `scripts/generate_key.py` to mint new Guardian keys.
- **Model resolution is name-based and key-independent.** A model is cloud-hosted when it matches an explicit `models:` entry or a `model_prefixes:` namespace (e.g. `anthropic/`, `nvidia/`). Local models are aliases from `config/models.yaml`. Unknown models return `404 model_not_served`. See `@docs/LLM_ROUTER.md`.
- **Google AI Studio is per-key only.** Registering a `google` credential retrieves its current OpenAI-compatible catalog and publishes linked routes as `guardian/google/<model>`. Only the creating Guardian key can manage or share that credential. Use the credential refresh endpoint to update the stored catalog; a failed refresh preserves the last successful list.
- **Cloud vision fallback is capability-based.** Guardian uses a local vision model only when an image request targets a configured text-only cloud model with an `image_fallback`. Image-capable cloud candidates remain cloud-routed; failover groups filter image requests to image-capable candidates.
- **Model discovery always includes context metadata.** Every `/v1/models` entry and `/api/show` response reports a positive context size. Resolve `context_overrides` first, then cloud catalog or local `/props`, and log before using the `131072` fallback.
- **Streaming keepalives required.** All streaming paths (local + cloud) must pass `heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S` (15s default) to `_iter_sse_lines_with_watchdog`. Missing this causes client idle-timeout errors on reasoning models.
- **Don't duplicate docs.** Detailed architecture lives in `docs/`. `AGENTS.md` is the index — reference, don't re-explain.
- **No hardcoded vars.** Literals that depend on the deployment (paths, ports, file names, URLs, timeouts) belong in `config/settings.yaml` (`${VAR}`-expandable) or `app/paths.py` (env-var overridable). Never copy a literal into a new module "for convenience" — inject it via `init()` and keep one source of truth. When extracting code, check the moved bodies for literals (`/home/...`, `:11434`, `guardian.pid`, …) and re-route them through config/paths before committing. A hardcoded value in a helper module that bypasses config is a bug, not a shortcut.
- **Commit language:** Dutch is fine for operator-facing notes (internal project); English for code, API, and public docs.

## Directory map

```
app/
├─ main.py              # uvicorn entrypoint
├─ paths.py             # central path resolution (REPO_ROOT, CONFIG_DIR, MODELS_DIR, …)
├─ proxy/server.py      # FastAPI router: all endpoints, cloud forwarding, streaming
├─ proxy/auth.py        # API key verification
├─ proxy/providers.py   # ProviderRegistry: cloud model recognition (exact + prefix)
├─ proxy/cloud_keys.py   # CloudCredentialStore: per-key credential linking
├─ proxy/anthropic_bridge.py  # Anthropic↔OpenAI SSE translation + ping keepalives
├─ proxy/failover.py     # FailoverRegistry: health tracking, candidate ordering
├─ proxy/queue.py        # FIFO inference queue with lifecycle tracking
├─ proxy/ratelimit.py    # Cloud provider rate-limit retries
├─ proxy/metrics.py      # Prometheus /metrics
├─ proxy/usage.py        # persistent API usage tracking for dashboard
├─ engine/manager.py     # llama-server lifecycle (start/stop/reload)
├─ scheduler/manager.py  # Idle-unload + auto-switch scheduler
├─ tweaker/              # Finetune v2: context/ngl/tensor_split tuning
└─ capture/             # Privacy-aware capture subsystem (config, policy, redactor, schema, sink, WAL writer)
config/
├─ settings.yaml         # proxy, providers (OpenRouter/NVIDIA/Poolside), queue, timeout tiers
├─ models.yaml           # model registry (aliases, runtime, tensor_split, switch policy)
└─ api_keys.json         # named API keys (goose, oelala, hydroponics, …)
scripts/
├─ start_llama.sh        # launch llama-server backend
├─ update_guardian_config.py  # live config mutation helper
├─ generate_key.py       # mint Guardian API keys
└─ guardianctl.py        # capture subsystem CLI (status/config/files/rotate/enable/disable)
```

## Skills

When touching these areas, read the referenced detail docs:

- **Cloud routing / model resolution** → `@docs/LLM_ROUTER.md`
- **Anthropic API bridge** → `@docs/ANTHROPIC_BRIDGE.md`
- **System architecture** → `@docs/ARCHITECTURE.md`
- **API surface** → `@docs/API_REFERENCE.md`
- **Client setup** → `@docs/CLIENT_INTEGRATION.md`
- **GPU/hardware tuning** → `@docs/HARDWARE_TUNING.md`
- **Deployment & operations** → `@docs/skills/operator-runbook.md`

## References

- Cloud rate limiting: `@docs/skills/operator-runbook.md`
- Client list / keys: `config/api_keys.json` (named keys for goose, oelala, hydroponics, etc.)

## Maintenance

- This file is the source of truth. `CLAUDE.md` and `.goosehints` are relative symlinks to this file.
- Every behavior change goes here first; symlinks follow automatically.
- If this repo gains Windows CI, run `scripts/sync-agent-docs.sh` instead of symlinks.

## Active Handoff

### Pi session `20260812_4` (last updated 2026-08-12 ~22:40)

- Working directory: `/home/flip/llama_cpp_guardian`
- **RESTARTED 22:27 + 22:35 UTC — provider timeouts 600→1200 now LIVE**, `cloud_retry.enabled=false` live. Post-restart audit found and fixed 6 bugs:
  1. `admin_api.py`: extraction gave all 25 handlers the same `(request, client_id)` signature — 11 mismatched their wrappers (8 client_id-only, 3 with path params) → `/api/status` 500. Fixed + signature regression test.
  2. `ollama.py`: bare `get_model_timeout` instead of injected `_get_model_timeout` → 500 on local chat. Fixed.
  3. `ollama.py`: `generate_ollama` missing `_ollama_capture_assembler` init (pre-existing latent NameError on EVERY streaming /api/generate call, copied verbatim from old server.py). Fixed.
  4. `forwarding.py`: 7 call sites used `translate_openai_*` without injected `_`-prefix → NameError on Anthropic-translated cloud errors. Fixed.
  5. `routing.py`: vision-fallback path called `_resolve_inference_model`, never injected — added to init(). Fixed.
  6. Type-hint hygiene: `PolicyResult` from `app.capture.policy` (not schema), asyncio/List/Tuple/JSONResponse imports restored.
- **pyflakes now clean** for app/ (only 2 pre-existing lazy string annotations in capture/redactor.py + proxy/metrics.py). `pip install pyflakes` available in venv — run `./venv/bin/python -m pyflakes app/` as a pre-restart gate.
- Verified live: `/api/status` OK (llama3.2-3b, backend healthy, 0 crashes), local chat OK, ollama streaming /api/generate OK, cloud forwarding OK (openrouter 404 was an upstream account privacy setting, not Guardian). Integration suite: 17 passed, 3 skipped (FINETUNE_V2_LIVE gated). Full suite: 892 passed, 3 skipped.
- All fixes pushed (`de03ba9..5e6483e`). Working tree clean.
- **Lesson recorded:** signature mismatches and `_`-prefix injection bugs are the #1 extraction failure mode. Run pyflakes + the admin-signature test before every restart; consider a wrapper-vs-module signature check in CI.



- Working directory: `/home/flip/llama_cpp_guardian`
- **Phase 5 structural separation: server.py 5177 → 1667 lines (−68%), full suite 887 passed / 3 skipped.** All logic lives in modules; server.py is a thin shell (routes, wrappers, init() calls). 95 delegation markers, 41 routes.
- Modules (all with `init()` DI, wrappers keep `server._*` names so test patches survive):
  - `app/gateway/`: `context_metadata`, `streaming`, `queue_helpers`, `usage`, `normalization`, `routing` (proxy_v1_post), `capture_dispatch`, `model_discovery` (/api/tags, /v1/models, /api/show), `admin_api` (25 handlers: keys, credentials, status, capture, scaler, queue), `sessions`
  - `app/cloud_inference/`: `routing`, `forwarding`
  - `app/local_inference/`: `ollama`, `models` (resolution, sizes, timeouts, VRAM scheduler, backend-reload recovery)
  - `app/proxy/`: `process` (pid/listener/startup state), `lifespan`, `state`
  - `app/config_loader.py` (settings.yaml parsed once per process; typed accessors)
- **No hardcoded vars rule** added to Critical rules (2026-08-12): extraction bodies must be re-checked for literals (`/home/...`, `:11434`, `guardian.pid`, …) and re-routed through `config/settings.yaml` or `app/paths.py` before committing. Sessions dir now flows via `paths.LLAMA_SLOTS_DIR`; `PROXY_PORT`/`PID_FILE` derive from settings.yaml (`proxy.port`, `proxy.pid_file`).
- **Bug fixed during extraction:** `_resolve_auto_reload_model` had been an empty stub since the queue_helpers extraction (returned `None`); body restored in `app/local_inference/models.py` (`model_manager.resolve_reload_target`).
- **PUSHED to GitHub (2026-08-12 ~22:00 UTC):** all 36 Phase 5 commits (`f9bf3bb..60050ce`) are on `origin/main`. Note: `GH_TOKEN`/`GITHUB_TOKEN` env vars hold an invalid token — push required `unset GH_TOKEN GITHUB_TOKEN GITHUB_PERSONAL_ACCESS_TOKEN` + `gh auth setup-git` (uses the valid `gho_...` oauth_token from `~/.config/gh/hosts.yml`).
- **Provider timeouts 600→1200 still NOT live** (settings.yaml edited 2026-08-12 19:47; service last restarted 19:41). Needs `sudo systemctl restart llama-guardian` — session drops, operator runs it (see Critical rule). Verify after restart: `cloud_retry.enabled=false`, `providers.*.timeout_seconds=1200`.
- Takeover sequence: `git diff` review → `py_compile` on `app/` → `./venv/bin/python -m pytest tests/ -q` → restart with operator → push after review.

### Pi session `20260812_1` (last updated 2026-08-12 19:45)

- Working directory: `/home/flip/llama_cpp_guardian`
- Last user instruction: disable the 429 retry catcher and restart; then document that the agent's own model traffic routes through Guardian.
- **429 catcher disabled (live).** `config/settings.yaml` → `cloud_retry.enabled: false`; both failover 429 probes (streaming + non-streaming) in `app/proxy/server.py` are gated on `cloud_rate_limiter.config.enabled`. Upstream 429s now pass straight through to the client (agent harness owns backoff/retry). Cross-provider failover switching stays active. Service restarted 2026-08-12 ~19:36 UTC and again 19:41 UTC; the restart interrupted this agent's own session (see new Critical rule "The agent routes through Guardian"). The operator had to enable `gh copilot` to get the system responsive again — this is now recorded as the proven recovery path.
- **Cloud provider timeouts doubled (config only, not yet live).** `providers.*.timeout_seconds` 600 → 1200 (openrouter, nvidia, poolside, openai) — pi owns its own 429 backoff now, so per-request budgets must leave headroom for long reasoning generations. Local `timeouts.tiers` and `queue.queue_timeout_seconds` deliberately unchanged (recently tuned; local watchdog still bounds stalls). Needs a restart to take effect — warn the operator first: the session drops during restart.
- **Pi-side retry backoff stretched (live, no restart needed).** `~/.pi/agent/settings.json` → `retry.baseDelayMs` 2000 → 4000 (pi's own agent-level 429 retry: 4s, 8s, 16s instead of 2s, 4s, 8s), `retry.provider.maxRetryDelayMs` 60000 → 120000 (accept server Retry-After up to 2 min). `maxRetries: 3` and `provider.maxRetries: 0` unchanged. This lives outside the repo — pi reads it at startup of a new turn; backup at `/tmp/pi-settings.json.bak`.
- Validation: `py_compile` OK, `tests/unit/test_ratelimit.py` 13 passed, `tests/unit/test_server.py` 115 passed, and the full suite 887 passed / 3 skipped. A minimal pi-authenticated OpenRouter request returned HTTP 200 after the final restart.
- **Resolved Phase 5 regressions:** `app/cloud_inference/routing.py` imported `anthropic_messages_to_openai` from the wrong module, causing HTTP 500s during cloud capture setup. It now imports the existing helper from `app.capture.redactor`. Stale server tests were updated to patch the injected `_cloud_routing` dependencies.
- Still uncommitted from the previous takeover: structural extraction in `app/proxy/server.py`, `tests/unit/test_server.py`, and untracked `app/cloud_inference/routing.py`. The 429 change is part of the same working tree.

#### Takeover sequence (next session)

1. Inspect the uncommitted extraction and test changes:
   `git diff -- app/proxy/server.py tests/unit/test_server.py app/cloud_inference/routing.py`
2. Run syntax validation:
   `./venv/bin/python -m py_compile app/proxy/server.py app/cloud_inference/routing.py`
3. Run the focused server tests:
   `./venv/bin/python -m pytest tests/unit/test_server.py -q`
4. Re-run the focused and full suites after any further extraction changes:
   `./venv/bin/python -m pytest tests/ -x`
5. Restart Guardian only with the operator present and aware that the session drops (see Critical rules). Only push after the tests and the final `git diff` have been reviewed.

### Goose session `20260809_5` (last updated 2026-08-12 17:25)

- Working directory: `/home/flip/llama_cpp_guardian`
- Last user instruction: update the docs, make the progress clear in `AGENTS.md`, and push to GitHub.
- No Goose response or push confirmation was recorded after that instruction. Do not assume the push happened.
- Current takeover files: `app/proxy/server.py` and `tests/unit/test_server.py` are modified; `app/cloud_inference/routing.py` is untracked.
- The active work is the structural extraction from `app/proxy/server.py`. Context metadata patches in `tests/unit/test_server.py` were updated to use `server._ctx_meta`; routing extraction introduced `server._cloud_routing`, so tests that still patch old server-level routing or failover objects need review.

#### Last recorded validation

- Full unit suite: 870 tests passed.
- `tests/unit/test_server.py`: 115 tests passed after patch updates.
- Integration run: 9 passed, 3 skipped, 1 failed. `test_chat_completions_basic` returned `503 Loading model` instead of `200`.
- Two integration attempts timed out after 120 and 180 seconds without output.
- Vision fallback tests were still suspected to patch `server.failover_registry` while the extracted code reads `_cloud_routing._failover_registry`; this was not confirmed fixed before the session ended.

#### Takeover sequence

1. Inspect the uncommitted extraction and test changes:
   `git diff -- app/proxy/server.py tests/unit/test_server.py app/cloud_inference/routing.py`
2. Run syntax validation:
   `./venv/bin/python -m py_compile app/proxy/server.py app/cloud_inference/routing.py`
3. Run the focused server tests:
   `./venv/bin/python -m pytest tests/unit/test_server.py -q`
4. Fix remaining patch-target or model-loading failures, then run:
   `./venv/bin/python -m pytest tests/ -x`
5. Restart Guardian before integration checks; there is no hot reload. Only push after the tests and the final `git diff` have been reviewed.

## Capture Implementation Status

### Phase 0 — Foundation ✅ (2026-08-01)
- Capture configuration, schema, policy engine, redactor, stream assembler, sink, WAL writer
- Integration controller facade
- Secret canary tests
- `/api/capture/status` admin endpoint

### Phase 1 — Local OpenAI Chat Vertical Slice ✅ (2026-08-01)
- Capture hooks on `proxy_v1_post` for both streaming and non-streaming
- Disabled by default via `GUARDIAN_CAPTURE_ENABLED=false`

### Phase 2 — Capture Subsystem Complete ✅ (2026-08-01)
- All 9 capture modules implemented
- 147 unit tests passing
- No regressions: 757 Guardian tests pass

### Phase 3 — Keanu Factory Integration ✅ (2026-08-05)
- Added `Source.GUARDIAN_CAPTURE` to Keanu contracts
- Created `guardian_capture_parser.py` (603 lines) in Keanu Factory
- 47 parser unit tests, all passing
- All 833 Keanu tests pass (786 + 47 new)
- Documentation: `docs/SOURCE_GUARDIAN_CAPTURE.md` (contract) and `docs/PARSER_GUARDIAN_CAPTURE.md` (implementation)

### Phase 4 — Protocol/Route Coverage ✅ (2026-08-05)
- Anthropic Messages protocol capture support (translation + endpoint gate)
- Ollama protocol capture support (`/api/chat` + `/api/generate`, streaming + non-streaming)
- Tool call/result capture with field policies (`tool_calls: capture`, `tool_results: strip`)
- Cloud capture allowlists (config ready, `cloud_capture=false`, awaiting provider terms review)
- Cloud non-streaming response content extraction (`_extract_cloud_response_content`)
- Cloud streaming capture with `StreamResponseAssembler` (content + tool_calls assembled)
- Cloud stream cancellation capture (`_cloud_stream_cancelled` → `request_cancelled`)
- Failover attempt tracking in capture events
- 836 Guardian unit tests, 833 Keanu tests, 222 capture-specific tests

### Phase 5 — Guardian Structural Separation ✅ (server.py is a thin shell: 5177 → 1667 lines, −68%)
- ✅ Extract `app/gateway/context_metadata.py` (context window resolution + model metadata entry construction, 6 functions, dependency injection via `init()`)
- ✅ Extract `app/cloud_inference/` (provider URL resolution, Google model discovery, routing helpers, retry classification, response header sanitisation, OpenAI reasoning param adaptation — 14 functions, dependency injection via `init()`)
- ✅ Extract `app/gateway/capture_dispatch.py` (capture event dispatch, 11 functions, dependency injection via `init()`)
- ✅ Extract `app/gateway/streaming.py` (SSE watchdog, keepalives, Anthropic enrichment, 11 functions/class, dependency injection via `init()`)
- ✅ Extract `app/gateway/queue_helpers.py` (request lifecycle, disconnect watch, cancel cleanup, 11 functions/class, dependency injection via `init()`)
- ✅ Extract `app/cloud_inference/routing.py` (attempt resolution, candidate preparation, capture setup, 385 lines, `init()` DI) — plus test patches updated to `server._cloud_routing` targets
- ✅ Extract `app/cloud_inference/forwarding.py` (`forward_to_cloud_provider`: streaming/non-streaming cloud forwarding, failover + 429 handling, Anthropic translation, usage + capture hooks, 556 lines, 28 injected deps via `init()`); server.py keeps a thin wrapper
- ✅ Extract `app/local_inference/ollama.py` (`chat_ollama`/`generate_ollama`: Ollama-protocol bridges to local llama-server, queue admission, auto-reload/switch, SSE translation, usage + capture, 742 lines, 38 injected deps via `init()`); routes in server.py are thin wrappers, init() call at module end
- ✅ Extract `app/gateway/usage.py` (usage tracking: live request lifecycle, token accounting, middleware body, 15 funcs; single injected dep = server `State`)
- ✅ Extract `app/gateway/normalization.py` (multimodal normalization: vision probing/preflight, backend error mapping, thinking params, qwen sanitization, 15 funcs, 396 lines; injected: model_manager, llama_server_url, queue_headers)
- ✅ Extract `app/gateway/routing.py` (`route_v1_post`: the `/v1/{path}` dispatch node — count_tokens, cloud/local routing + vision fallback, queue admission, auto-reload/switch, multimodal preflight, llama-server transport, Anthropic enrichment, usage + capture, 845 lines, ~58 deps via `init()`); server.py keeps a thin route wrapper; tests patch `server._gw_routing.*` (and `_cloud_forwarding.*` for the cloud-path usage hooks)
- ✅ Extract `app/proxy/process.py` (pid file, listener inspection/stale termination, startup-check state machine, guarded model operations, background startup check — 15 funcs, ~260 lines; owns `_startup_check_status`/`_startup_check_task` with accessors)
- ✅ Extract `app/gateway/model_discovery.py` (Ollama /api/tags, /v1/models list+metadata, /api/show handler bodies — 4 async handlers, ~245 lines; routes stay thin wrappers in server.py)
- ✅ Extract `app/gateway/admin_api.py` (keys, cloud credentials CRUD/links/google refresh, crash history, server status, capture status/rotate, scaler, queue status/cancel — 25 async handlers, ~420 lines; routes stay thin wrappers)
- ✅ Extract `app/gateway/sessions.py` (session save/load/list + filename sanitizer — 4 funcs, ~85 lines)
- ✅ Extract `app/config_loader.py` (load_config + typed accessors vram/heartbeat/close-timeout/queue — YAML now parsed once per process)
- ✅ Extract `app/proxy/state.py` (State container with vram_limit_mb param)
- ✅ `app/gateway/` extraction complete (auth stays imported in server.py; `prometheus_metrics` is a thin wrapper over `app/proxy/metrics.py`)
- ✅ `app/local_inference/` extraction complete (ollama, models incl. VRAM scheduler + backend reload; queue stays in `app/proxy/queue.py` and is injected)
- ✅ `app/cloud_inference/` extraction complete (routing + forwarding)
- ✅ `app/proxy/` process/lifespan/state + `app/config_loader.py` done — **server.py is a thin shell: 5177 → 1667 lines (−68%), 95 delegation markers, 41 routes**
- 📋 Optional polish: `proxy_v1_get` passthrough, final import cleanup, push to GitHub

### Phase 6 — Operational Hardening 🔄 (In Progress)
- ✅ `guardianctl` CLI for capture control (`scripts/guardianctl.py`)
  - `status` — capture subsystem status via API
  - `config` — effective config from settings.yaml
  - `files` — list WAL files on disk
  - `rotate` — force WAL file rotation via API
  - `enable`/`disable` — toggle capture in settings.yaml
  - `test-event` — emit synthetic test event
- ✅ `/api/capture/rotate` admin endpoint
- ✅ `CaptureWALWriter.rotate()` public method (3 unit tests)
- ✅ Multi-secret client_ref rotation (Decision 1A) — `GUARDIAN_CAPTURE_CLIENT_REF_SECRET_PREVIOUS` overlap period (13 unit tests)
- ✅ Per-record HMAC authentication (Decision 2A) — `record_auth` field on WAL JSONL lines, `GUARDIAN_CAPTURE_RECORD_AUTH_SECRET` env var (10 unit tests)
- ✅ Crash-recovery tests (9 unit tests — restart, partial line, state persistence, corrupt state, deleted active file, multiple partials, empty file, disk-full simulation, HMAC across restart)
- ✅ Capture dashboard (React component `CapturePanel.jsx` — live status, disk usage bar, writer metrics, config summary, field policies, force-rotate button; view toggle in `App.jsx`; Vite proxy to Guardian :11434)
- 📋 72-hour soak test

### Resolved Decisions (2026-08-07)

1. **Rotation/migration for `GUARDIAN_CAPTURE_CLIENT_REF_SECRET`** → **Multi-secret overlap period (A)**
   - Guardian supports a comma-separated list of active secrets in `GUARDIAN_CAPTURE_CLIENT_REF_SECRET` (current) and `GUARDIAN_CAPTURE_CLIENT_REF_SECRET_PREVIOUS` (legacy).
   - `compute_client_ref()` tries the current secret first; `allowed_client_refs` matching accepts both current and legacy hashes during the rotation window.
   - This preserves existing opt-in continuity — no forced re-registration of all clients during key rotation.

2. **Keyed record authentication vs checksum-only** → **Per-record HMAC (A)**
   - Each WAL JSONL line gets a `record_auth` field: `{"alg": "hmac-sha256", "key_id": "<short hex of secret>", "mac": "<hex HMAC of the JSON line excluding record_auth>"}`
   - Keanu can verify per-record authenticity (not just file-level integrity) and detect individual line tampering.
   - Guardian holds the signing secret; Keanu holds a verification-only copy.

3. **Unix user/group sharing model between Guardian and Keanu** → **Same user (A)**
   - Both Guardian and Keanu run as the same Unix user (`flip`) on the same host.
   - Capture files use `0o640` (owner rw, group r) and directories `0o750` — no world access.
   - No dedicated shared group or cross-host transfer needed for the current deployment.

4. **Provider-by-provider cloud capture permissions** → **Global on/off sufficient (C)**
   - The existing `cloud_capture` boolean (default: false) controls all cloud capture.
   - The existing `cloud_allowlist_enabled` + `allowed_cloud_models` + `cloud_model_prefixes` namespace filter provides sufficient model-level granularity when enabled.
   - No per-provider fine-grained flags needed.

5. **Max message/response sizes before truncation** → **No truncation (B)**
   - Guardian delivers raw data without size limits on individual messages or responses.
   - Data processing (truncation, transformation) is Keanu's responsibility, not Guardian's.
   - File-level rotation (256 MB / 1 hour) and disk-level retention (10 GB total / 7 days) manage disk usage.

6. **Operator approval process for sensitive field capture** → **YAML-only, operator is responsible (C)**
   - The operator who edits `settings.yaml` is responsible for changing field policies from `strip` to `capture`.
   - No additional audit log, runtime confirmation, or approval workflow needed.
   - The conservative defaults (`system_prompts: strip`, `reasoning: strip`, `tool_results: strip`) protect against accidental disclosure.
