# AGENTS.md — Llama-CPP Guardian

> Canonical AI-agent context for this repo. Read first.
> Claude Code: `CLAUDE.md` → here. Goose: `.goosehints` → here. Copilot: `.github/copilot-instructions.md` references this.

## Stack

- **Language:** Python 3.14 (venv at `./venv`)
- **Web:** FastAPI + uvicorn + httpx
- **Backend:** llama.cpp (`llama-server` on `:11440`, launched via `scripts/start_llama.sh`)
- **Frontend:** React/Vite/Tailwind dashboard on `:11437` (`dashboard/`), bound to `127.0.0.1`
- **Config:** `config/global.settings.yaml` (proxy, providers, queue, timeouts), `config/models.local.settings.yaml` (model registry + aliases), `config/guardian.keys.yaml` (named API keys)
- **Secrets:** `.env` — `${VAR}` expansion in YAML; never commit secrets
- **Deploy:** systemd unit `llama-guardian.service`; nginx exposes the public API on `:11434`.
- **TLS:** nginx stream TLS preread multiplexes both `http://192.168.1.35:11434` and `https://192.168.1.35:11434`. It passes TLS unchanged to Guardian on `127.0.0.1:11435` and routes plain HTTP through nginx on `127.0.0.1:11436`. See `deploy/nginx/llama-guardian-protocol-mux.conf` and `deploy/nginx/llama-guardian-loopback-http.conf`.
- **TLS trust:** this host trusts the Guardian certificate through `/usr/local/share/ca-certificates/llama-guardian-192.168.1.35.crt`. Other LAN clients must trust that same certificate before connecting without a custom CA setting.
- **Tests:** pytest (`tests/`, `asyncio_mode=auto`)

## Critical rules

- **Test before claiming fixed:** `./venv/bin/python -m py_compile <file>` then run `./venv/bin/python -m pytest tests/ -x`. Never claim a fix works without verifying.
- **Code needs a restart; config can hot-reload (since 2026-08-19).** `app/*.py` code changes require `sudo systemctl restart llama-guardian` — there is NO hot code reload. But `global.settings.yaml` (providers, failover_groups, capture `cloud_capture`/`cloud_model_prefixes`/policies, failover_health, cloud_retry) now hot-reload WITHOUT restart via `POST /api/config/reload` (admin, any valid key). Port/pid/TLS remain restart-only. Do NOT run the restart for config-only edits anymore; do run it (after the pre-restart gate) for code changes.
- **Run the pre-restart gate before every restart.** `./venv/bin/python scripts/pre_restart_check.py` runs py_compile + pyflakes (undefined names) + the wrapper-vs-module signature check + the full pytest suite. All four gates must pass before `sudo systemctl restart llama-guardian`; any failure means the restart may not come back up (agent traffic routes through Guardian). Added 2026-08-12 after the post-restart audit caught 6 injection/signature bugs the unit suite had missed.
- **The agent routes through Guardian — restarting cuts the agent's own model traffic.** This agent harness (Claude Code / goose / pi) reaches its model *through this very service* (nginx `:11434` → TLS `:11435` → app). A `sudo systemctl restart llama-guardian` therefore silences the current session until startup completes; a code/config error that prevents startup is **not self-healable** — the agent's model is unreachable, so it cannot fix its own mistake. Before any restart: (1) validate with `py_compile` + focused pytest, (2) tell the operator a restart is coming and the session will drop, (3) let the operator run the restart from outside the session, (4) if startup fails, the operator must revert (`git stash`/`git checkout` on `app/`, restore previous `settings.yaml`) — never promise in-session recovery. **Known recovery path (proven 2026-08-12):** the operator enables `gh copilot` (routes around Guardian) and uses it to inspect/repair/restart Guardian while the pi session is down.
- **TLS requires both paths.** `GUARDIAN_TLS_CERTFILE` and `GUARDIAN_TLS_KEYFILE` are an all-or-nothing pair. The production drop-in binds TLS to `127.0.0.1:11435` through `GUARDIAN_TLS_HOST` and `GUARDIAN_TLS_PORT`; nginx's `libnginx-mod-stream` module and a top-level `stream { include /etc/nginx/stream-conf.d/*.conf; }` block are required for the public protocol multiplexer. Keep the private key `0600`.
- **Secrets in `.env`.** API keys use `${ENV_VAR}` expansion. Never inline keys in YAML or Python. Use `scripts/generate_key.py` to mint new Guardian keys.
- **Model resolution is name-based and key-independent.** A model is cloud-hosted when it matches an explicit `models:` entry or a `model_prefixes:` namespace (e.g. `anthropic/`, `nvidia/`). Local models are aliases from `config/models.local.settings.yaml`. Unknown models return `404 model_not_served`. See `@docs/LLM_ROUTER.md`.
- **Cloud access redesign (2026-08-21) governs cloud routing.** Since commits `4329d7c`/`28e97ad` there is NO credential/link/ownership layer, NO `guardian/` prefix, and NO `cloud_keys.json` credential store (removed 2026-08-22). Cloud models are addressed `{provider}/{brand}/{model}` and resolved from each provider's settings API key via the dynamic `CloudModelCatalog` (`app/proxy/cloud_catalog.py`), which fetches `/v1/models`, normalizes to `{brand}/{model}`, and caches with TTL + cold-start disk cache. Per-key cloud access = `cloud_gateway_access: true|false` (default **true**) in `config/guardian.keys.yaml`; a key set `false` gets 403 on cloud routes and sees no cloud entries in `/v1/models`. Failover groups are `failover/{group}` and read `failover_groups:` from `global.settings.yaml`.
- **Per-provider `catalog_url` override (2026-08-21, PR #9).** `CloudProvider` has an optional `catalog_url` (default `/models`); `cloud_catalog.refresh_provider` fetches `base_url + catalog_url`. Use it so a provider advertises only the models genuinely reachable through its guardrails/privacy filters: e.g. openrouter is set to `catalog_url: /models/user`, so Guardian's `/v1/models` shows the 22 really-accessible OpenRouter models instead of all 422 (OpenRouter applies guardrails on inference only, not on the plain `/v1/models` listing — plain listing returns everything). The cold-start disk cache (`data/cloud_catalog_cache.json`) now stores a `source` = `base_url|catalog_url` per provider; a cached entry is dropped when the endpoint changes, so switching `catalog_url` does not keep advertising the old list until a manual `POST /api/cloud/catalog/refresh`. Changing `catalog_url`/`base_url` auto-invalidates the stale cache on the next reload/construction.
- **Config-schema split (2026-08-21, PR #9, `docs/CONFIG_SCHEMA.md`).** `config/settings.yaml` is split into domain files: `config/global.settings.yaml` (proxy/queue/timeouts/scaler/capture/grammar/cloud_retry/failover_health/services/services_to_stop/benchmark), `config/providers.settings.yaml` + `config/providers.overrides.yaml` (provider defaults + per-provider overrides win, e.g. `catalog_url`), `config/models.local.settings.yaml` (local registry), `config/models.cloud.overrides.yaml` (merged: catalog overrides + context_window + model sampling defaults), and `config/guardian.keys.yaml` (guardian API keys). `app/config_loader.py` is the central read switch: it deep-merges the full `global.settings.yaml` document into the shared CONFIG dict, then overlays merged providers from `providers.settings.yaml` + `providers.overrides.yaml`; `app/proxy/providers.py` production-default `ProviderRegistry()` (no `settings_path`) reads those files too and derives `context_overrides` from the `context_window` entries in `models.cloud.overrides.yaml`. Remaining legacy names still shipped compat-symlinks: `local_models.yaml` (→ `models.local.settings.yaml`); `models.yaml`, `api_keys.json`, `cloud_keys.json`, `benchmark_models.json` were **removed 2026-08-22** (no symlink kept — archived in `.scratch/cleanup/legacy-config/`). `cloud_models.yaml` (→ `models.cloud.overrides.yaml`), `guardian_apikeys.yaml` (→ `guardian.keys.yaml`) and `settings.yaml` (→ `global.settings.yaml`) compat-symlinks were **removed 2026-08-22** too (the `app/paths.py` resolvers already prefer the canonical names, and `scripts/generate_key.py` was rewired to `guardian.keys.yaml`). `.bak` backups are gitignored. All direct `settings.yaml` readers were rewired to the new names via `app/paths.py` helpers (`global_settings_file()`, `providers_defaults_file()`, `providers_overrides_file()`, `local_models_file()`, `guardian_apikeys_file()`, `models_cloud_overrides_file()`). `models.cloud.settings.yaml` and `models.local.overrides.yaml` are **reserved** (in the schema) but not shipped — no runtime consumer yet.
- **Cloud vision fallback is capability-based.** Guardian uses a local vision model only when an image request targets a configured text-only cloud model with an `image_fallback`. Image-capable cloud candidates remain cloud-routed; failover groups filter image requests to image-capable candidates.
- **Model discovery always includes context metadata.** Every `/v1/models` entry and `/api/show` response reports a positive context size. Resolve `context_overrides` first, then `cloud_models.yaml` overrides, then the cloud catalog or local `/props`, and log before using the `131072` fallback.
- **Streaming keepalives required.** All streaming paths (local + cloud) must pass `heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S` (15s default) to `_iter_sse_lines_with_watchdog`. Missing this causes client idle-timeout errors on reasoning models.
- **Don't duplicate docs.** Detailed architecture lives in `docs/`. `AGENTS.md` is the index — reference, don't re-explain.
- **GCD is a pass-through contract, cloud-stripped.** The local OpenAI path forwards `response_format`/`json_schema`/`grammar` (GBNF) byte-identical to llama-server (pinned by `tests/unit/test_grammar_passthrough.py`) — never whitelist body fields. Cloud routes strip GBNF/`json_schema` (providers reject them) and preserve OpenAI-native `response_format`; the `grammar` block in `settings.yaml` (`enabled` kill-switch, `cloud_auto_convert_json`, `cloud_strict_mode`, `validate_gbnf`) controls the optional behavior. Ollama `options.format` maps to `response_format`/`grammar` in the bridge. Capture stores only `grammar_present`/`response_format_present` flags — never the grammar content.
- **No hardcoded vars.** Literals that depend on the deployment (paths, ports, file names, URLs, timeouts) belong in `config/settings.yaml` (`${VAR}`-expandable) or `app/paths.py` (env-var overridable). Never copy a literal into a new module "for convenience" — inject it via `init()` and keep one source of truth. When extracting code, check the moved bodies for literals (`/home/...`, `:11434`, `guardian.pid`, …) and re-route them through config/paths before committing. A hardcoded value in a helper module that bypasses config is a bug, not a shortcut.
- **Commit language:** Dutch is fine for operator-facing notes (internal project); English for code, API, and public docs.
- **AGENTS.md is always updated — including fresh findings.** Every behavior change, bug fix, extraction, config change, lesson learned, AND any repository fact that had to be dug up or reverse-engineered goes into AGENTS.md (progress lists, handoff section, Critical rules) in the SAME working session — before the final commit/push, not after. If you finished a task and AGENTS.md does not reflect it, the task is not done. The handoff section is the primary continuity mechanism between agent sessions; a stale handoff is a bug. **Rule of thumb:** if you had to inspect code, config, or docs to learn *how something actually works*, write that understanding into AGENTS.md right then — otherwise the next session re-derives it from scratch. That is known investigation work, and it belongs in the file the moment you've confirmed it.
- **Maximize subagent usage with fresh context.** Delegate as much work as possible to subagents started with `context: "fresh"` (or default fresh-context workers). The lead session keeps the plan and stays in control; children do the mechanical/implementation/measurement work without inheriting the lead's accumulated chat history. Workflow: (1) the lead reads just enough to write a precise task (exact file paths, expected shape, the reference data to copy), (2) spawns a fresh-context worker for the implementation + verification, (3) synthesizes the child's output and applies any cross-file follow-up. Keep **one writer per cwd/worktree** — concurrent workers must not touch the same files; serialize edits to shared files (e.g. `config/models.yaml`) or have the lead own them. Subagent constraints on this host: model `guardian/openrouter/deepseek/deepseek-v4-flash-0731:high` (routes via Guardian, NOT bare `openrouter/...` — the direct OpenRouter key is disabled), max **3 simultaneous** (Guardian 429s above that; Novita upstream rate-limits). For read-only review use fresh-context reviewer children, then have the lead synthesize + apply fixes. Read `@~/.pi/agent/npm/node_modules/pi-subagents/skills/pi-subagents/SKILL.md` for the runs.run/runs.all API.
- **Dashboard UI auth — empty shell ≠ code bug.** Since commit `7472d61` (2026-07-30, auth on dashboard `:11437`, bind to 127.0.0.1 only) every `/api/*` on the dashboard requires a Bearer key, but the UI itself sent no auth header → the dashboard was functionally dead (empty shell) even locally, `curl 127.0.0.1:11437/api/stats` → 401. Fixed 2026-08-15: a fetch-wrapper (monkey-patch `window.fetch`) + key-input modal in `app/ui/index.html` store the key in localStorage (`guardian_dashboard_api_key`) and inject `Authorization: Bearer <key>` on every `/api/*` call; on 401 the key is cleared and the modal reopens. A dashboard that shows an empty shell is a missing key in the browser, not a code bug — check localStorage first before touching the code.

## Directory map

```
app/
├─ main.py              # uvicorn entrypoint
├─ paths.py             # central path resolution (REPO_ROOT, CONFIG_DIR, MODELS_DIR, …)
├─ config_loader.py     # settings.yaml parsing — loaded ONCE per process, typed accessors
├─ proxy/server.py      # thin shell: routes + init() wiring, 1643 lines (Phase 5: all logic extracted)
├─ proxy/auth.py        # API key verification
├─ proxy/providers.py   # ProviderRegistry: cloud model recognition (exact + prefix)
├─ proxy/cloud_keys.py   # CloudCredentialStore: per-key credential linking
├─ proxy/anthropic_bridge.py  # Anthropic↔OpenAI SSE translation + ping keepalives
├─ proxy/failover.py     # FailoverRegistry: health tracking, candidate ordering
├─ proxy/queue.py        # FIFO inference queue with lifecycle tracking
├─ proxy/ratelimit.py    # Cloud provider rate-limit retries
├─ proxy/metrics.py      # Prometheus /metrics
├─ proxy/usage.py        # persistent API usage tracking for dashboard
├─ proxy/process.py      # pid file, listener inspection/stale termination, startup-check state
├─ proxy/lifespan.py     # startup/shutdown orchestration + idle-unload watcher
├─ proxy/state.py        # runtime State container (VRAM scheduler, scaler, optimizer, usage)
├─ gateway/              # Phase 5 extracted logic, all with init() DI:
│  ├─ routing.py         #   /v1/{path} dispatch (cloud/local, queue, vision fallback)
│  ├─ normalization.py   #   multimodal preflight, error mapping, thinking params
│  ├─ streaming.py       #   SSE watchdog, keepalives, Anthropic enrichment
│  ├─ queue_helpers.py   #   request lifecycle, disconnect watch, cancel
│  ├─ usage.py           #   live usage tracking + middleware
│  ├─ capture_dispatch.py #   capture event dispatch hooks
│  ├─ model_discovery.py #   /api/tags, /v1/models, /api/show handler bodies
│  ├─ admin_api.py       #   25 admin/status/credential/scaler/queue handlers
│  ├─ sessions.py        #   session slot save/load/list
│  └─ context_metadata.py #  context window resolution + model metadata
├─ cloud_inference/      # Phase 5 extracted: routing.py (attempts/fallback/capture setup),
│                        #   forwarding.py (forward_to_cloud_provider, 28 deps)
├─ local_inference/      # Phase 5 extracted: ollama.py (chat/generate bridges),
│                        #   models.py (resolution, sizes, timeouts, VRAM scheduler, reload)
├─ engine/manager.py     # llama-server lifecycle (start/stop/reload)
├─ scheduler/manager.py  # Idle-unload + auto-switch scheduler
├─ tweaker/              # Finetune v2: context/ngl/tensor_split tuning
└─ capture/             # Privacy-aware capture subsystem (config, policy, redactor, schema, sink, WAL writer)
config/
├─ global.settings.yaml   # proxy (port/target/pid_file/vram), queue, timeout tiers, capture, etc.
├─ providers.settings.yaml + providers.overrides.yaml  # provider defaults + per-provider overrides
├─ models.local.settings.yaml   # model registry (aliases, runtime, tensor_split, switch policy)
├─ models.cloud.overrides.yaml   # cloud model overrides (context_window, model_defaults)
└─ guardian.keys.yaml     # named API keys (goose, oelala, hydroponics, …; gitignored secrets)
scripts/
├─ start_llama.sh        # launch llama-server backend
├─ update_guardian_config.py  # live config mutation helper
├─ generate_key.py       # mint Guardian API keys
├─ pre_restart_check.py  # restart gate: py_compile + pyflakes + signature check + pytest
└─ guardianctl.py        # capture subsystem CLI (status/config/files/rotate/enable/disable)
```

## Skills

When touching these areas, read the referenced detail docs:

- **Cloud routing / model resolution** → `@docs/LLM_ROUTER.md`
- **Cloud access redesign plan** → `@docs/CLOUD_ACCESS_REDESIGN.md` (IMPLEMENTED 2026-08-21: one config, one key source, dynamic catalog, consistent `{provider}/{brand}/{model}` cloud format — `guardian/` prefix dropped so bare-name clients keep working; per-key `cloud_gateway_access` boolean replaces credential linking)
- **Anthropic API bridge** → `@docs/ANTHROPIC_BRIDGE.md`
- **System architecture** → `@docs/ARCHITECTURE.md`
- **API surface** → `@docs/API_REFERENCE.md`
- **Client setup** → `@docs/CLIENT_INTEGRATION.md`
- **GPU/hardware tuning** → `@docs/HARDWARE_TUNING.md`
- **Deployment & operations** → `@docs/skills/operator-runbook.md`
- **LAN GPU backends + provider-unificatie (PLAN 2026-08-26, niet gebouwd)** → `@docs/LAN_GPU_BACKENDS.md` — operator-principe: **alles wat modellen serveert leeft in `providers.settings.yaml`**; `local` wordt de enige `managed` provider-entry (engine/manager.py behoudt spawn/VRAM/switch), Windows-PC + cloud zijn externe entries (`base_url` LAN + `catalog_url: /v1/models`; llama-server adverteert zelf `/v1/models` — geverifieerd). Stap 1 = local-als-provider (routing-refactor, code+gate+restart), stap 2 = Windows-entry (config-only, hot-reload). Optie B = llama.cpp `--rpc` (haalbaar op 1 Gbit voor chat, ~5–15% overhead; blijft engine-arg, geen provider). Operator: "alleen plan vastleggen".
- **Gateway/Manager-split (PLAN 2026-08-26, niet gebouwd)** → `@docs/GATEWAY_MANAGER_SPLIT.md` — operator-idee: opsplitsen in `guardian-llmprovider-gateway` (proxy/routing/capture/discovery) + **`caretaker-llama-cpp`** (de manager die llama-server beheert; naam gekozen 2026-08-26: fantasy-rol "caretaker" = verzorger/onderhouder, spiegelbeeld van guardian=gatekeeper). **Ontleding (hard geteld):** van de ~1637 regels `engine/manager.py` is ~1050 echte lifecycle (spawn/args/health/crash/unload), ~509 registry/keuze/discovery (hoort in de gateway) en ~78 settings-lezen (al gedeeld YAML — operator-gelijk: settings zijn geen manager-werk). Eerlijke conclusie: de manager-kern is dun en **verweven met verkeer** (idle-unload leest queue/requests; auto-switch door requests getriggerd) — voor de lokale host is een module genoeg, een daemon wordt pas zinvol voor GPU-hosts ZONDER Guardian (Windows). Fase 0 = de ~509 regels registry/keuze/discovery ontdraaien naar de gateway-laag. **Deployment-topologie (operator 2026-08-26): manager per GPU-host — één op ai-kvm-2 én één op de 14700K; gateway alleen op ai-kvm-2, praat met beide via `management_url` (http://192.168.1.35:11441 + http://192.168.1.x:11441). Windows: geen systemd (NSSM/service), elke manager leest zijn eigen `models.local.settings.yaml` (de GGUFs met Windows-paden), idle-unload via gateway-contract.**
- **Per-provider config-bestanden (PLAN 2026-08-26, niet gebouwd)** → `@docs/CONFIG_PROVIDER_FILES.md` — operator-richting: één configuratiebestand per provider i.p.v. de defaults/overrides-split. Nieuwe layout: `config/providers/<naam>.settings.yaml` — `ai-kvm2-local`, `14700k-local` (onderscheid local/cloud zit in de naam), `openrouter`, `nvidia`, `google`, enz. `providers.settings.yaml`+`providers.overrides.yaml`+`models.local.settings.yaml`+`models.cloud.overrides.yaml` verdwijnen; per-model overrides (context_window, model_defaults) komen in het `models:`-blok van de provider zelf. `global.settings.yaml` + `guardian.keys.yaml` blijven (cross-cutting). Code-impact: paths.py (PROVIDERS_DIR + scan), config_loader (directory-scan i.p.v. merge), providers.py, engine/local_inference (models uit ai-kvm2-local-bestand), context_metadata/routing (get_override per provider). Pay-off: nieuwe provider = één bestand + hot-reload. Tests/legacy single-file blijft werken via settings_path.

## References

- Cloud rate limiting: `@docs/skills/operator-runbook.md`
- Client list / keys: `config/guardian.keys.yaml` (named keys for goose, oelala, hydroponics, etc.)

## Maintenance

- This file is the source of truth. `CLAUDE.md` and `.goosehints` are relative symlinks to this file.
- Every behavior change goes here first; symlinks follow automatically.
- If this repo gains Windows CI, run `scripts/sync-agent-docs.sh` instead of symlinks.

## Active Handoff

### DSH session `20260826_rename` (repo-split: legacy + nieuwe bouwplaats, last updated 2026-08-26)

- Working directory (nieuw, bouwplaats): `/home/flip/guardian-llmprovider-gateway`
- **DRIE repos, definitief:**
  - `m0nklabs/llama-cpp-guardian` = **LEGACY/archief** (beschrijving gemarkeerd; teruggerenoemd). De **productie-installatie draait nog uit `/home/flip/llama_cpp_guardian`** (systemd `llama-guardian.service` + venv wijzen daarheen); die oude dir is aan deze legacy-repo gekoppeld.
  - `m0nklabs/guardian-llmprovider-gateway` = **NIEUW (bouwplaats)**. Schone herstart: lokale clone van de oude repo met **volledige git-history (326 commits)**; gitignored troep (venv, data/, .env, .scratch/, logs, caches) is NIET meegekomen. Remote + main staan.
  - `m0nklabs/caretaker-llama-cpp` = lege private repo voor de per-GPU-host manager (naamkeuze: fantasy-rol "caretaker" = verzorger, spiegelbeeld van guardian=gatekeeper; vorm `caretaker-llama-cpp` zonder "for").
- **History-oplossing (operator-vraag "gaat de commit history verloren?"):** nee — git-history zit in `.git`, niet in de bestanden. De nieuwe repo is een clone van de oude: alle 326 commits mee, boom alleen de 192 tracked bestanden.
- **OPERATIONEEL CRITIEK voor sessies in de nieuwe dir:** er is GEEN `venv`, GEEN `.env`, GEEN `data/`, GEEN `config/guardian.keys.yaml` (allemaal gitignored) in `/home/flip/guardian-llmprovider-gateway`. Dus: (a) geen restarts/deploys vanuit deze dir — productie draait uit de oude dir; (b) pytest/gate lopen via het OUDE venv: `/home/flip/llama_cpp_guardian/venv/bin/python -m pytest tests/` (of eerst een venv aanmaken); (c) secrets (`.env`, keys) niet committen — bij een draaiende nieuwe installatie kopiëren vanuit de oude dir. **Gedaan (16:02, deze sessie):** `venv` = symlink → legacy-venv (`import app` OK), `.env` + `config/guardian.keys.yaml` + `data/cloud_catalog_cache.json` + `data/capture/` gekopieerd (allemaal gitignored); `.gitignore` uitgebreid met `venv` zonder slash (symlink werd anders als untracked getoond — commit `cbcd3ca`).
- **DSH-sessie gekopieerd naar de nieuwe workspace (2026-08-26, deze sessie):** de hele `20260826_rename`-sessie (107.830 records, ~66 MB plain) is gekopieerd naar `~/.dsh/sessions/--home-flip-guardian-llmprovider-gateway--/session-a2b871fe-aefd-4d5a-8f7d-b2c048e58e38/` — **de operator kan deze sessie dus "meenemen" en in de GUI bij de nieuwe workspace terugzien/vervolgen**. Methode (reverse-engineered uit `dsh-session-persistence-jsonl`): sessies liggen per workspace onder `~/.dsh/sessions/<projectKey(cwd)>/<sessionId>/session.jsonl.zstd`; projectKey vervangt `/` door `-` (`/home/flip/guardian-llmprovider-gateway` → `--home-flip-guardian-llmprovider-gateway--`). Kopie = origineel decompressen (`zstd -dc`), eerste regel (header) herschrijven met nieuw `id` + `cwd`, rest byte-identiek doorgeven, hercompressen naar de nieuwe project-dir. **Verificatie:** sha256 van origineel (afgekapt op kopie-moment) == sha256 van kopie (minus header) `d2bf65b9…`; alle 107.830 records parsen als JSON (0 fouten; seq-gaten zijn normaal — compaction verwijdert records); de GUI scant project-dirs bij `list()` (geen index om bij te werken). Zie de oude sessie in `--home-flip-llama_cpp_guardian--/session-57198571-…` voor de rest van deze geschiedenis (de kopie bevat alles tot het kopieermoment).
- Docs-referenties (README/CLIENT_KEY_LINKING/JSON-specs/split-plan) zijn al naar `guardian-llmprovider-gateway` gefixt (commit `5e02c78`); die kloppen nu voor de nieuwe repo.

### DSH session `20260826_muse_catalog` (nieuw OpenRouter-model zichtbaar maken, last updated 2026-08-26)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Vraag operator:** "muse net aangezet op openrouter, maar ik zie hem nog niet bij /v1/models". Antwoord + fix: **een op OpenRouter nieuw ingeschakeld model verschijnt NIET automatisch in Guardian's `/v1/models` — er moet een catalog-refresh lopen** (`POST /api/cloud/catalog/refresh`, elke geldige key). De openrouter-catalogus wordt in memory + disk (`data/cloud_catalog_cache.json`, `source = base_url|catalog_url`) gecached met 1-uurs TTL; de cache stond op 24-aug en miste het model.
- **Uitgevoerd (live):** refresh → openrouter 22 modellen (was 21), `/v1/models` toont nu `openrouter/meta/muse-spark-1.2-contributor` (dit is het model dat de operator aanzette; de andere muse-varianten `muse-glimmer-30b`/`muse-spark-1.2`/`muse-spark-1.1` staan wél in OpenRouter's volledige catalogus maar NIET in `/models/user` van deze key). Entry heeft reasoning-metadata: `supported_efforts [xhigh,high,medium,low,minimal]`, default `medium`, `mandatory: true`. `nvidia/meta/muse-glimmer-30b` stond al in de NVIDIA-allowlist en is nu ook zichtbaar.
- **Tweede cache-laag (NIEUW inzicht, reverse-engineered):** naast de catalog-cache heeft `ProviderRegistry` een APARTE in-memory context-catalogus (`_context_catalogs`, `CONTEXT_CATALOG_TTL_SECONDS = 3600`, eigen fetch van `base_url + catalog_url` die alleen `context_length`/`max_input_tokens` extraheert). Een nieuw model kan dus wél in `/v1/models` staan maar nog de `131072`-fallback tonen (muse adverteert upstream 1.048.576) — de context-catalogus pakt dat pas op bij zijn eigen TTL-expiry of een restart. Er is GEEN admin-endpoint om de context-catalogus apart te forceren. Geen bug, geen restart nodig.
- **Terzijde (observatie, geen actie):** NVIDIA's live `/v1/models` is geslonken van 102 naar 83 modellen; 15 van de 40 `catalog_allowlist`-ids worden niet meer geadverteerd (o.a. `meta/llama-3.1-70b-instruct`, `meta/llama-3.1-8b-instruct`, `meta/llama-3.2-1b/3b-instruct`, `meta/llama-3.3-70b-instruct`, `nvidia/llama-3.1-nemotron-nano-8b-v1`, `nvidia/nemotron-mini-4b-instruct`, `nvidia/nv-embed-v1`, `thinkingmachines/inkling`). De allowlist kan met `scripts/sync_nvidia_free_models.js` worden opgeschoond zodra de operator dat wil.

### DSH session `20260826_raw_capture` (RAW capture + Keanu replay, last updated 2026-08-26)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Architectuur (operator-beslissing 2026-08-26, autoritatief):** *Guardian = RAW loggen + replay; Keanu = redactie + dataset.* De in-pipeline redactie is uit de capture-subsysteem VERWIJDERD — `app/capture/integration.py` roept geen `redact_*` meer aan; request messages, response content, reasoning en tool results gaan ongewijzigd de WAL in. De enige in-pipeline transformatie is **media-extractie**: binaire image-payloads worden als losse bestanden onder `data/capture/media/` geschreven en in het event vervangen door een referentieblok (`{"type":"image_media","image_media":{path, sha256, mime_type, size_bytes[, width,height]}}`) — base64-bytes komen NOOIT in de WAL. Redactie/dataset is nu de standalone tool `scripts/keanu_redact.py` (WAL-raw → geredacteerde dataset), en replay is `guardianctl export` (record_auth + sha256-verificatie + stream).
- **Gating = alles, alle clients (operator-beslissing):** `config/global.settings.yaml` → `cloud_capture: true`, `cloud_allowlist_enabled: false`, `per_client_opt_in: false`, `allowed_client_refs: []`. Alleen écht on-geauthenticeerde requests (geen geldige key → geen `client_ref`) worden nooit gecaptured. De veld-policies in de capture-sectie (`system_prompts: strip` enz.) zijn nu **Keanu-defaults** — Guardian negeert ze (documentatie-comment staat in de yaml).
- **Retentie oneindig + compressie op het log zelf (operator-beslissing):** `retention_days: -1` (= keep-all; `_validate()` accepteert nu -1, net als `max_capture_bytes: -1` = unlimited) en het ACTIEVE bestand wordt stream-gecomprimeerd gzip: `app/capture/wal_writer.py` schrijft elke record met `Z_SYNC_FLUSH` via een zlib-compressobj (gzip-wrapper), en alleen bij rotatie/close wordt de trailer (`Z_FINISH`) geschreven. Actieve filenaam is nu `guardian_capture_current.jsonl.gz`; geroteerde bestanden zijn al gz (géén recompressie meer in `_rotate_file`). Sidecar blijft `.sha256` via `with_suffix(".sha256")` → `...jsonl.sha256`.
- **Crash-tolerant multi-member gzip-lezer (NIEUW, `app/capture/gzip_reader.py`):** na een crash is het actieve bestand een gzip-stream zónder trailer; bij restart appende de writer een NIEUW gzip-member. Naïef `gzip.open().read()` faalt daarop. De lezer: (a) voert nooit voorbij een kandidaat-volgende-gzip-header (zodat een getrunceerde member de volgende niet opslokt), (b) bevestigt een membergrens door met een **gekopieerde** decompressor (`copy.copy(decompressobj)`) de eerste byte van de volgende header te proberen — op een `Z_SYNC_FLUSH`-grens geeft dat "invalid block type" (zlib gooit output weg bij error, vandaar de kopie-probe), (c) herstelt complete records uit getrunceerde members, dropt een partiële tail, en (d) raakt nooit in een loop (resync op volgende header). `eof=True` treedt pas op nadat de decompressobj de 8-byte trailer heeft geconsumeerd → geen handmatige trailer-consumptie. Gebruikt door `guardianctl export` en `keanu_redact`. Tests: `tests/unit/test_capture_gzip_reader.py` (12: clean, multi-member, crash+restart, double-crash, partial-tail, garbage-tussen-members, round-trip met writer).
- **Pre-existente bugs gevonden+gefix:**
  1. `integration._build_context()` gaf `duration_ms=` door aan `BuildContext` (dataclass zonder dat veld) → TypeError bij ELKE `request_received`-capture; `capture_dispatch` slikte het fail-open weg, dus live capture deed stilletjes niets. Fix: param+kwarg verwijderd.
  2. `wal_writer.rotate()` retourneerde ná rotatie de net herschapen (lege) actieve file i.p.v. het geroteerde bestand (glob `*.jsonl.gz` pakte nu ook de actieve `.gz`). Fix: `_rotate_file()` retourneert het voltooide pad.
  3. `_enforce_retention()` zou bij `max_capture_bytes: -1` álles wissen (`total_size > -1` altijd waar). Fix: guard `if cut_bytes >= 0`.
- **`scripts/keanu_redact.py` (NIEUW):** standalone Keanu-tool. `--input` (WAL-bestand of dir, gz via gzip_reader of plain jsonl), `--output` (default stdout), `--policy KEY=VALUE` (strip|capture; defaults = de veld-policies uit de yaml), `--images hash|strip|keep` (hash=verwijdert path, houdt sha256/mime/size; strip=weg; keep=referentie laten). Hergebruikt `app/capture/redactor.py` (dat bestand blijft bestaan als de redactie-engine van Keanu).
- **`guardianctl export` (NIEUW subcommando):** replay van raw WAL voor Keanu. Volgorde: geroteerde bestanden op naam (timestamp_seq), dan actieve. `--verify-checksums` (sha256 vs sidecar), record_auth-HMAC-verificatie (aan als `GUARDIAN_CAPTURE_RECORD_AUTH_SECRET` in env/.env staat), `--verify-only` (geen data, alleen controle), `--no-verify`. Verificatiefouten → waarschuwing op stderr, export gaat door.
- **Tests (+36):** `test_capture_raw_passthrough.py` (7: raw secret in WAL, reasoning/tool_results raw, image→media-file+referentie, anon-no-capture, retentie/-1 config), `test_capture_media.py` (8: OpenAI/Anthropic extractie, remote-url untouched, malformed→error-ref, text-only), `test_capture_keanu_tools.py` (7: export replay+auth+checksum, tamper-detectie, keanu_redact E2E, policy-override, images-hash, plain-jsonl), `test_capture_gzip_reader.py` (12). Alle 969 unit-tests passen (1 bekende pre-existing google-failure: `test_cloud_attempts_resolve_google_full_address`, bewust gedeferred).
- **Pre-restart gate 2026-08-26:** py_compile PASS, pyflakes PASS, signatures PASS, pytest FAIL enkel op de bekende pre-existing google-assertion. Code-change → `sudo systemctl restart llama-guardian` nodig.
- **LIVE-verificatie na restart (DONE 2026-08-26):** `guardianctl status` → Enabled/Active True, cloud=True, per_client_opt_in=False, retention=-1, budget=-1, writer_running=True. Actief bestand is `data/capture/guardian_capture_current.jsonl.gz` (6.8 MB — al het agentverkeer wordt raw gecaptured conform "alles, alle clients"). `guardianctl export` → 139 raw events, 137/137 met record_auth (HMAC OK), `--verify-only` groen. **Cloud-streaming capture live bevestigd:** na de assembler-reconcile-fix + restart bevatten cloud `request_completed`-events de geassembleerde response content (`FIELDCHECK`-test) i.p.v. None — de definitieve fix werkt live. Legacy-plain-bestand `guardian_capture_current.jsonl` (van vóór gzip, 16-aug) blijft staan, `files` labelt het nu "legacy (plain)", export negeert het. **Leerstuk uit 2026-08-24:** na een code-deploy die de cache-shape verandert moet een catalog-refresh (`POST /api/cloud/catalog/refresh`) lopen.
- **Reasoning-capture compleet (2026-08-26, follow-up op de assembler-fix):** de assembler las alleen `delta.reasoning_content`, maar OpenRouter proxyt DeepInfra-stijl reasoning als `delta.reasoning` — cloud-capture verloor dus álle reasoning-tekst (live bewezen: reasoning-deltas in de stream, `reasoning_content: None` in WAL-events). Fix: assembler accepteert `delta.reasoning` als fallback (`reasoning_content` heeft prioriteit als beide aanwezig), en `forwarding.py` geeft de geassembleerde reasoning nu door aan het capture-event op BEIDE paden (streaming + non-streaming; non-stream via nieuwe `extract_cloud_reasoning_content`-helper die `message.reasoning_content`/`message.reasoning` leest — alleen als de legacy-fallback hem niet al in content heeft gevouwen). Daarbij ook een wiring-bug gefixt: `_extract_cloud_reasoning_content` werd in `init()` toegewezen ZONDER `global`-declaratie → module-hook bleef stilletjes None (precies het dode-pad-patroon; de gate-call-site-check ving dit niet omdat het geen call-site is — het was een assignment-zonder-global in een init-functie). Live geverifieerd na restart: streaming request → WAL-event met `content: '81'` én `reasoning_content: 'We need answer only number...'`. Tests: assembler (OpenRouter-reasoning-accumulatie + precedence), server (non-stream reasoning-extractie, 4).
- **Cloud streaming 500 — root cause + definitieve fix (2026-08-26):** mijn raw-capture deploy (commit `810706e`) zette capture standaard AAN voor alle clients; daarmee werd een dode code-pad in `app/cloud_inference/forwarding.py` live: `StreamResponseAssembler(protocol=...)` (constructor accepteert geen `protocol`) + `.feed(data)` (methode bestaat niet; het is `add_sse_line(line)`, die een RAW `data:`-lijn verwacht) + ontbrekende `nonlocal _cloud_stream_cancelled` in de nested generator → HTTP 500 bij élke cloud-streaming request terwijl OpenRouter al 200 had gegeven. De unit-suite ving dit NIET omdat geen enkele test de cloud-streaming route met `capture_ctx` actief draaide en de gate alleen wrapper-calls uit `server.py` checkte.
  - **Eerste fix (operator liet het door een Copilot-agent doen, live geverifieerd: 200, 11 SSE data-lijnen, finish=stop):** assembler tijdelijk gebypast (clientstream/usage/lifecycle blijven, assembled content/tool_calls None) + `nonlocal`-fix + regression test `tests/unit/test_cloud_forwarding.py`. Restart + post-restart checks 6/6 door Copilot.
  - **Definitieve fix (deze sessie, daarna):** assembler correct aangesloten — `StreamResponseAssembler()` zonder kwargs, `add_sse_line(line)` met de raw SSE-lijn op BEIDE takken (pass-through + Anthropic-translatie; de translatie-tak voedt nu élke `data:`-lijn i.p.v. alleen `message_delta`), en `assemble()` + alle `add_sse_line`-calls **fail-open** (try/except) zodat capture de stream nooit meer kan breken. Regression test bijgewerkt: verwacht nu `response_content == "OK"` i.p.v. None.
  - **PREVENTIEVE MAATREGEL (nieuw, deze sessie):** `scripts/pre_restart_check.py` heeft een nieuwe gate **3b "cross-module call-site signature check"** die over álle `app/**/*.py` loopt: geïmporteerde namen worden geresolveerd en calls worden tegen de werkelijke callee-signature gecheckt (unexpected kwargs, missing required params, niet-bestaande methoden op klassen/modules, plus mini-dataflow `var = Klass(...)` → `var.meth(...)`). Deze check vangt de originele bug exact (`StreamResponseAssembler.__init__ unexpected kwarg 'protocol'` — gevalideerd tegen de gebroken forwarding.py). False-positive-veilig: alleen statisch resolveerbare geïmporteerde callees, `self`/`cls` gestript, functie-calls niet als klassen getrackt. Zonder deze check kan een dode-pad-API-drift opnieuw live gaan zodra een config-flag een pad activeert.
  - **Leerstuk (2026-08-26):** een config-only flip (capture aan) kan een latent fout code-pad activeren dat geen enkele test dekt. Regels: (1) elke code-deploy die een feature standaard AAN zet, moet tests hebben die dat pad met capture actief draaien; (2) de gate checkt nu call-sites tegen signatures (3b); (3) capture-gerelateerde integratiepunten zijn fail-open (assembler-aanroepen in try/except).
- **Risico bewust geaccepteerd (operator-keuze "alles, alle clients"):** secrets in client-message-bodies liggen nu RAUW in de WAL tot Keanu ze stript. `record_auth`-HMAC + `.sha256`-sidecars blijven de integriteitsbasis. Guard tegen symmetrische recursie: capture captureert zijn eigen capture-traffic niet (`EXCLUDED_PATH_PREFIXES` admin/health/metrics).

### DSH session `20260822_cleanup` (project-tree cleanup, last updated 2026-08-22)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Project tree cleaned (operator request).** Junk was moved to `.scratch/cleanup/{root-logs,data,config-backups,scripts,historical,legacy-docs}/` (gitignored) or deleted (regenerable caches). Root is now clean: logs/scripts/historical docs out.
- **Moved to scratch (content preserved, git rm'd where tracked):** root `benchmark.log`+`startup.log`; `data/bench-qwen36/` + `data/guardian_uvicorn.log`; `config/models.yaml.bak`+`config/settings.yaml.bak`; `.cp-images/` (one-off pasted png); `scripts/llama.log`; `docs/.scratch/running-laguna-with-limited-resources.md`; historical `patch_server.py` (SSE workaround — its target no longer exists in server post-refactor), `LOOP_PROGRESS.md`, `TODO_LIST.md` (superseded by AGENTS.md handoffs). **Deleted:** all `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, and the empty `integrations/` dir (source-less, pycache-only).
- **Legacy docs git rm'd → `.scratch/cleanup/legacy-docs/` (operator OK'd):** `docs/BENCHMARK_SUMMARY.md`, `CONTEXT_BENCHMARKS.md`, `REAL_BENCHMARK_RESULTS.md`, `unsloth_candidates.md`, `SERVER_UPGRADE_PLAN.md`, `HYDRO_CONTEXT_ARCHITECTURE.md`, `model_registry.json` (Feb-2026, unreferenced, superseded by `docs/MODEL_BENCHMARKS.md`). **Follow-up (operator OK'd):** the 3 scripts that pointed at those removed doc paths were also scratched → `.scratch/cleanup/legacy-scripts/` + git rm: `scripts/analyze_benchmark.py`, `benchmark_context.py`, `download_models_py.py`. Their `docs/HARDWARE_TUNING.md` references were removed (both the table rows and the "Legacy utilities" bullet list). `scripts/recommend_context.py` stays (not mentioned).
- **Operator consciously did NOT** remove the rest of the one-off script set (`download_new_models.sh`, `needle_test.py`, `stress_test.py`, `cleanup_invalid_benchmarks.py`, `bench_qwen36_variants.py`) — they stay in `scripts/`.
- **Deprecated config files removed (2026-08-22, operator OK'd):** all 4 of `config/models.yaml` (symlink), `config/cloud_keys.json`, `config/api_keys.json`, `config/benchmark_models.json` are removed → `.scratch/cleanup/legacy-config/`. `models.yaml` (symlink → `models.local.settings.yaml`) and `api_keys.json`/`cloud_keys.json`/`benchmark_models.json` were all replaced by the config-schema canonical files. **Code rewired so nothing breaks:** `scripts/_auth.py` + `app/tweaker/finetune_v2_cli.py` + `scripts/finetune_model_config.py` + `scripts/needle_test.py` now read the key store via `guardian_apikeys_file()` (YAML, was JSON api_keys.json); `scripts/sync_models.py`, `verify_prompts.py`, `update_guardian_config.py`, `recommend_context.py`, `bench_all_models.py`, `finetune_v2_cli.py` all use `local_models_file()` (was `CONFIG_DIR / "models.yaml"`). `scripts/link_keanu_key.py` scratched (dead — its credential claim+link API was removed in the cloud redesign). `README.md`/`operator-runbook` may still mention `config/models.yaml`/`api_keys.json` as historical docs. `docs/CONFIG_SCHEMA.md` migration table updated to note the removals.
- **Test suite:** 938 passed / 3 skipped / **1 failed** after the deprecations + rewiring. The 1 failure is the **pre-existing** `test_cloud_attempts_resolve_google_full_address` (confirmed on pristine HEAD; operator knowingly deferred it — NOT touched). The old pytest-9 collection error in `tests/integration/test_live_inference.py` ("pytest.skip … allow_module_level=True") was **FIXED** by rewiring `_resolve_api_key` to read `config/guardian.keys.yaml` via `yaml.safe_load` (no module-level skip; falls back to `SystemExit`); the module now collects 18 tests. Pre-restart gate: py_compile PASS, pyflakes PASS, signatures PASS, pytest FAIL only on the pre-existing google assertion. `data/bench-models/` (resumable state of `scripts/bench_all_models.py`) and `data/model_finetune_v2_results.json` (default finetune output) intentionally kept.

### DSH session `20260822_nvidia_free_filter` (NVIDIA free-tier discovery filter, last updated 2026-08-22)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Problem the operator reported:** `nvidia/moonshotai/kimi-k3` "werkt niet" via Guardian and doesn't appear on build.nvidia.com's catalog page. Root cause turned out to be **two separate things**, and the operator asked me to (a) investigate what is actually usable/free on NVIDIA, and (b) fix the bug — both done.
- **NVIDIA context-lengte not machine-readable (investigated, documented):** NVIDIA's hosted `integrate.api.nvidia.com/v1/models` returns only `{id, object, created, owned_by}` (no `context_length`, no `max_input_tokens`) — unlike OpenRouter (sends `context_length`) and Poolside (sends `context_length`). Google (`display_name` only) and OpenAI (`shutdown_date` extra) also send no context. So Guardian's `_extract_context_windows()` (provider catalog → `context_length`/`max_input_tokens`) only ever populates context for **OpenRouter + Poolside**; NVIDIA/Google/OpenAI models all fall back to `DEFAULT_CONTEXT_WINDOW = 131072` unless a `context_window` override exists in `models.cloud.overrides.yaml`. NVIDIA's free-tier per-model context exists ONLY in the build.nvidia.com web catalog (embedded `"contextLength":NNNN` in model detail pages). Verified the NIM management endpoints (`/v1/metadata`, `/v1/manifest`, `/v1/version`, `/v1/health/*`, `/openapi.json`, `/docs`) do NOT exist on the hosted gateway, only on a self-hosted NIM container.
- **Forwarding bug fixed (`app/cloud_inference/routing.py`):** the `400 Validation: Unsupported parameter(s): \`context_window\`` on `nvidia/moonshotai/kimi-k3` was NOT NVIDIA — Guardian was forwarding the whole overlay (`context_window: 1048576`) as an OpenAI request param (OpenRouter silently accepts it; NVIDIA rejects with 400). Fixed by adding a `metadata_keys` set (`context_window`/`context_length`/`max_input_tokens`/`max_context`/`benchmark_context_limit`/`advertised_context`) that `prepare_cloud_candidate_request` filters OUT of `model_defaults` before applying overrides as body params. Real OpenAI params (`max_tokens`/`temperature`/`top_p`/`seed`) still applied. Regression added: `test_prepare_cloud_candidate_strips_context_metadata_from_defaults`.
- **Free-allowlist for NVIDIA (`catalog_allowlist`):** NVIDIA's `/v1/models` lists all 102 models regardless of what the free token can reach and gives no free marker. The authoritative "what is free" source is build.nvidia.com's **"Free Endpoint" filter** (`?filters=nimType:nim_type_preview&pageSize=96&orderBy=weightPopular:DESC` ⇒ 58 models on one page — the pageSize=96 trick was the operator's tip, verified 2026-08-22). Of those 58, only **39** are also present in `integrate.api.nvidia.com/v1/models` (the other 19 — cosmos/voicechat/sparsedrive/glm-5.2/magpie-tts/etc — are free on the site but NOT servable via this API account).
  - Added `CloudProvider.catalog_allowlist` field (`app/proxy/providers.py`, parsed from `catalog_allowlist:` config) and a filter in `CloudModelCatalog.get_models_for_provider` (`app/proxy/cloud_catalog.py`) that restricts both discovery AND routing to the allowlisted ids.
  - `config/providers.overrides.yaml` now sets `providers.nvidia.catalog_allowlist` = **40** entries = the 39 free∩API models **plus `moonshotai/kimi-k3`** (operator's explicit choice: keep it since it genuinely works via the API even though the page doesn't mark it free).
  - 3 regression tests in `tests/unit/test_cloud_catalog.py` (`TestCatalogAllowlist`).
  - **NOTE: this is a CODE change → requires `sudo systemctl restart llama-guardian` (config hot-reload alone is NOT enough; the live process still advertises 102 until restart).** Hot-reload now only reflects the config but not the filter code.
- **Periodic refresh script `scripts/sync_nvidia_free_models.js`:** Node (≥22, global WebSocket) script that drives **google-chrome headless via CDP** to load the build.nvidia.com free-filter URL, run the WAF-challenge JS + cookie-accept, wait for the model list, extract the `{publisher}/{model}` free slugs, and rewrite `config/providers.overrides.yaml`'s `providers.nvidia.catalog_allowlist`. Preserves existing non-free extras (e.g. kimi-k3). `--dry-run` supported. **CAUTION:** the DSH execution sandbox kills `google-chrome` child processes (SIGKILL) after the initial probe runs — so on THIS harness the script fails to launch Chrome, and the refresh must be run outside the sandbox (operator/CI shell/real cron), or via the Playwright-MCP browser tool which does not fork Chrome itself. Verified logic works (extractor returns exactly 58) in an isolated CDP run before the sandbox blocked Chrome.
  - Build.nvidia.com boots: plain `curl`/`--dump-dom` get the AWS WAF challenge (HTTP 202/2.4KB, no models) — only a real JS browser that passes the challenge returns data. `api.nvcf.nvidia.com/v2/nvcf/functions` (205 functions) has NO free/price metadata.
  - The once-off probe utilities I wrote are `scripts/probe_nvidia_models.py` + `scripts/reprobe_nvidia_indeterminate.py` (both garbage-collect candidates; classify OK/404/403/429/400/timeout per model). Confirmed live: 21 models OK + 9 soft classification + 9 timeouts; `moonshotai/kimi-k2.6` → 404 "Function not found for account" (not free/servable with this key), `poolside/laguna-xs-2.1` → 503 ResourceExhausted (rate-limited, transient), most `writer/*`, `snowflake/arctic-embed-l`, `zyphra/*` etc → 404 (not servable with free key).
- **Current `/v1/models` context metadata gap (still open, NOT done):** NVIDIA models get no real context (131072 fallback) because NVIDIA's `/v1/models` lacks context and there is no machine-readable free-context source. Per-model `context_window` overrides for the actively-used NVIDIA models remain FUTURE work (operator said "first find what's usable, THEN max context") — the context question was explicitly deferred behind the free-filter work.
- **Pre-restart gate:** py_compile PASS, pyflakes PASS, signatures PASS, pytest **942 passed / 3 skipped / 1 failed** — the 1 failure is still the same pre-existing `test_cloud_attempts_resolve_google_full_address` (google cold-start routing, unrelated to this work, knowingly deferred).
- **Follow-up (same session): cleaned stale `models:` lists out of `config/providers.settings.yaml`** (operator request). The per-provider `models:` blocks were legacy/stale (contained EOL models like `z-ai/glm-5.2`, `nvidia/llama-3.1-nemotron-ultra-253b-v1` that 404 via the free key) and redundant now that the dynamic cloud catalog feeds discovery. Removed `models:` from openrouter/nvidia/poolside/google/openai; kept `model_prefixes` (they drive cloud recognition + routing of `{provider}/...` addresses). **Operator decision: NO bare-name aliases** — bare cloud names without a provider prefix (e.g. `gpt-4o`, `kimi-k3`, `glm-5.3`) now return 404; only full `{provider}/...` addresses (as the catalog advertises) are served. Config-only change → hot-reloaded (no restart). Verified live: openai 123 / nvidia 40 / openrouter 22 / google 51 / poolside 2; `openai/gpt-4o` → 200. If a bare-name alias is ever needed again, the correct home is a `models:` override in `providers.overrides.yaml` (verified the merge overlays it over the settings defaults), NOT `providers.settings.yaml`. **Heads-up:** `~/.pi/agent/models.json` still contains bare cloud names + legacy `guardian/...` entries in its `guardian` provider (`gpt-4o`, `gpt-5`, …) — pi may 404 on those if it actually issues them in bare form; left untouched (operator did not ask, and pi's active models use full addresses).


### DSH session `20260824_reasoning_effort` (DSH reasoning-effort config + empirisch bewijs, last updated 2026-08-24)

- Working directory: `/home/flip/llama_cpp_guardian` (het onderwerp is echter de DSH-userconfig, niet Guardian zelf)
- **Aanleiding:** de dsh-GUI had (initieel) geen reasoning-effort bediening op de relay-route (`guardian` provider, `api: openai-completions` via `http://192.168.1.35:11434/v1`). De operator koos **Pad A**: `reasoningEfforts` declareren in `~/.dsh/settings.yaml` i.p.v. een community-plugin. Ook onderzocht: de eerder geobserveerde "grep-loop"-degeneratie (model emitteert *tekst* "Grep." i.p.v. gestructureerde tool_call; dsh `agent/inbox/spliced` splices die gegenereerde tekst terug als `next-step` met `<system-reminder>Doorgaan` → self-prompting feedback). Remedie: hoger reasoning-effort reduceert dat soort degeneratie.
- **DSH gebruikt pi-agent onderdelen (bevestigd):** de provider-adapter heet `llm-pi-ai` (`@deepseek-ai/dsh-llm-pi-ai`), de model-directory én effort-mechanica zijn pi-ai (THINKING_LEVELS `off|minimal|low|medium|high|xhigh|max`, THINKING_FORMAT_GATE incl. `openrouter`). Het effort-niveauformaat en de wire-spelling komen dus uit pi's directory, niet uit een eigen dsh-systeem. De `guardian` provider in `~/.dsh/settings.yaml` (api `openai-completions`, baseURL Guardian `/v1`) is exact het pi-patroon; het `:high`-suffix in subagent-model-strings (`guardian/openrouter/deepseek/deepseek-v4-flash-0731:high`) is pi's/OpenRouter's native mechanisme.
- **Config die werkt (Pad A, live):** in `~/.dsh/settings.yaml` bij de model-entry `openrouter/deepseek/deepseek-v4-flash-0731` (contextWindow 250000):
  ```yaml
  reasoningEfforts: { "off": null, "high": "high", "max": "max" }
  compat: { supportsReasoningEffort: true, thinkingFormat: openrouter }
  ```
  - **YAML-valkuil die een bug was:** `off: null` ongemarkeerd wordt door YAML 1.1 geparsed als boolean `False`, wat door geen enkele THINKING_LEVEL komt ("off" als niveau-sleutel MISLUKT). **Sleutels moeten gequoted** (`"off"`). Na fix: `dsh --profile web --dump-config` accepteert foutloos (exit 0); pi-ai schema-validation gooit niet.
  - pi-ai `resolveModelReasoning` (catalog.ts): `reasoningEfforts` mag leeg `off` (wire `null`) maar moet ≥1 niveau voorbij `off` bieden, anders validatiefout. `compat.thinkingFormat: openrouter` stuurt de effort naar het OpenRouter-wire-dialect.
  - **Effect geldt per request zonder restart** (llm-pi-ai herlaadt knobs per request), maar de GUI-effortknop verschijnt pas na Web-Host-restart; de operator heeft `max` live ingesteld en bevestigd in de UI.
- **Empirisch bewijs dat effort werkt (via Guardian → OpenRouter → DeepInfra):** zelfde veeleisende prompt (fox/chicken/grain), géén `max_tokens`-cap, `finish=stop`, complete output:
  | effort | reasoning tokens | totale completion |
  |---|---|---|
  | `low` | 10.238 | 11.241 |
  | `max` | 20.612 | 22.267 |
  - **`max` ≈ 2× `low` op reasoning-tokens.** De keten geeft de param dus effectief door en het upstream-model respecteert hem.
  - **Leerstuk (mijn eerdere verkeerde conclusie gecorrigeerd):** lage reasoning-tokens (8-30) bij effort-tests zijn *geen* bewijs dat de param faalt — dat zijn artefacten van (a) een te simpele prompt (model hoeft niet diep te denken, ook niet op max) en (b) te kleine `max_tokens`-caps die reasoning vroeg afkappen (`finish=length`). Alleen met veeleisende prompt + ruim/geen budget zie je het echte effect.
  - Extra datapunt: `native_tokens_reasoning: 0` in de OpenRouter-generatielog is een **onbetrouwbaar** veld bij DeepInfra-flash (idem omgekeerd: varieert 0-24 willekeurig bij identieke runs), dus niet gebruiken als maat voor effort-effect. Objectief bewijs = generatie-tijd per token (langzamer bij zwaarder denken) + reasoning-tokens uit `completion_tokens_details`.
- **Hoe je de effort-stages van een model ontdekt (antwoord op operator-vraag):** OpenRouter stuurt wél machine-readable effort-info per model:
  ```
  GET https://openrouter.ai/api/v1/models/<author>/<slug>   (of de /api/v1/models lijst)
  → "reasoning": { "supported_efforts": [...], "default_effort": "..." }
  ```
  Concreet (aug 2026): `deepseek/deepseek-v4-flash-0731` → `["max","high","low"]` default `high`; `deepseek/deepseek-v4-flash` (latest) → `["xhigh","high"]`; `deepseek/deepseek-v4-pro` → `["xhigh","high"]`; `deepseek-v4-flash-vision-exp` → `["max","high","low"]`. **Gepinde versie (0731) heeft géén xhigh maar wél max; latest/pro hebben xhigh maar geen max.** Het kan dus per model-*versie* verschillen.
  - **Nuancering (empirisch bewezen):** xhigh wordt wél geaccepteerd (200 OK) op `0731` dat `[max,high,low]` rapporteert — OpenRouter valideert effort **niet strikt** per request; `supported_efforts` is de geadverteerde set, niet een harde whitelist. Wat upstream (DeepInfra e.d.) er daadwerkelijk mee doet is bepalend. Het `/endpoints` eindpunt (`/api/v1/models/<slug>/endpoints`) toont per-provider-pricing/context/max_completion_tokens maar géén per-provider effort-stages.
- **Status Groq/Cerebras (uit eerdere fase, dit werk pakte het erbij):** `.env` kreeg `GROQ_API_KEY` + `CEREBRAS_API_KEY` (gitignored); `config/providers.settings.yaml` kreeg `groq` (base_url `https://api.groq.com/openai/v1`) + `cerebras` (base_url `https://api.cerebras.ai/v1`) entries. **Nieuwe env-vars vereisen een `sudo systemctl restart llama-guardian`** (load_dotenv draait éénmaal bij startup; config-hot-reload herleest providers maar NIET `.env` — live toonde `configured:false` tot restart). **WORTEL-OORZAAK = AFGEKAPTE KEYS (2026-08-24, OPGELOST — met correctie op een eerdere foute conclusie):** de sleutels in `.env` waren de **eerste 40 karakters** van langere echte sleutels — bij het overzetten naar `.env` is afgekapt. Gevonden via de operator-tip "de geldige keys staan in `~/.secrets/keys`":
  - `~/.secrets/keys/groq.key` = 56 chars, `~/.secrets/keys/cerebras.key` = 52 chars (beide één doorlopende key-string, geen whitespace; mtime 23-aug 10:22/10:25, dus vóór het `.env`-schrijfmoment 10:28 van dezelfde sessie).
  - De oude `.env`-waarden (40 chars) waren een **exact voorvoegsel** van die bestanden → providers wezen de verkorte string terecht af (groq `invalid_api_key`, cerebras `wrong_api_key`). Mijn eerdere conclusie "nooit echte sleutels geweest / gegenereerd zonder verificatie" was **fout** — dat bleek uit het directe bewijs dat de VOLLEDIGE keys wél werken: groq → HTTP 200 (13 modellen), cerebras → HTTP 200 (2 modellen). Operator had dus gelijk.
  - **Herstel uitgevoerd (na `.env`-fix + restart PID 1325897 + catalog-refresh):** `groq` = **13 modellen** in `/v1/models`, chat/completions **200 "Hi! 👋"** (volledig werkend). `cerebras` = **2 modellen**, maar chat/completions → **402 `payment_required` (`param: quota`)** = het cerebras-account heeft geen actieve betaling; de key is wél geldig (catalogus 200; model-ids `gemma-4-31b`/`gpt-oss-120b` worden herkend). Operator-actie als cerebras-inferentie nodig is: billing activeren in cloud.cerebras.ai.
  - **Cerebras VERWIJDERD (operator-beslissing 2026-08-24, zie cloud_audit-vervolg):** operator: "ik vind cerebras niet belangrijk, het was een poging tot gratis llm gebruik" — en gratis is het niet (billing nodig). `cerebras`-entry uit `config/providers.settings.yaml` + `CEREBRAS_API_KEY` uit `.env` (groq géén verandering). Na restart live: **0 cerebras-entries** in `/v1/models`, groq nog 13 modellen + chat 200 ✅.
  - **Leerstuk:** `.secrets/keys/*.key`-sleutels kunnen langer zijn dan het "typische" 40-char formaat; valideer een key ALTIJD direct bij de provider (of vergelijk met het volledige bronbestand) vóór conclusies over "ongeldige keys".
- **Reasoning-effort metadata op `/v1/models` (IMPLEMENTED, This session, code-change → restart):** Guardian exposeert nu per cloud-model `reasoning`-metadata (`supported_efforts`, `default_effort`, plus optioneel `mandatory`/`default_enabled`) wanneer de provider-catalogus het adverteert (OpenRouter-stijl). Dit geeft dsh/pi/goose één betrouwbare bron voor "welke effort-trappen heeft dit model" — de operator-vraag die dit onderzoek begon. **Architectuur afspraak (operator-instuctie):** *Transport* (param `reasoning_effort` doorsturen) blijft Guardian's pass-through-taak; *metadata* (trappen verzamelen + blootleggen) is Guardian's taak; *selectie/UX* (welke trappen tonen, wire-mapping) is de harness (dsh/pi-ai `reasoningEfforts`).
  - **`app/proxy/cloud_catalog.py`:** `refresh_provider` extraheert per entry `reasoning` via `_extract_reasoning` (veilige subset: `supported_efforts`[lijst] + `default_effort`/`mandatory`/`default_enabled`[alleen als correct type]; providers zonder reasoning-blok → `{}`). Slaat het op in `self._catalogs[name]["reasoning"]` (naast `models`); disk-cache (`_persist_cache`/`_load_disk_cache`) schrijft/leest het mee, backward-compatibel met oude caches zonder het veld. Nieuwe accessor `get_model_reasoning(provider_name, normalized_id) -> Dict` (respecteert `catalog_allowlist`). **Opgelet:** de effort-stages zitten in de *OpenRouter `/v1/models/user`* catalogus (Guardian's `catalog_url` voor openrouter), wél in `reasoning:` per model.
  - **`app/gateway/model_discovery.py`:** `_build_cloud_entry` + het enkel-model-pad (`model_metadata` cloud-branch) roepen `_attach_reasoning_metadata(entry, provider_name)` — kijkt de `{brand}/{model}`-remainder op en zet `entry["reasoning"]` als die aanwezig is. Lokale/failover-entries krijgen het nooit.
  - **Tests:** `tests/unit/test_cloud_catalog.py` `TestReasoningMetadata` (5: extract subset, provider-zonder-reasoning, non-empty-efforts-guard, refresh+accessor, disk-cache-round-trip, legacy-cache-compat) + `tests/unit/test_server.py` 2 (lijst-annotatie + enkel-model-annotatie). Pre-restart gate: py_compile/pyflakes/signatures PASS, pytest **950 passed / 3 skipped / 1 failed** — de 1 failure blijft de pre-existing google-cold-start-assertion (bewezen ook op ongewijzigde HEAD te falen, niet gerelateerd).
  - **LIVE (2026-08-24, operator gaf toestemming om zelf te restarten):** `sudo systemctl restart llama-guardian` door de agent uitgevoerd (exit 0, startup actief, PID 877458). **Na restart MOET een catalog-refresh.** De disk-cache (`data/cloud_catalog_cache.json`, van vóór de restart) bevatte nog géén `reasoning`-velden → de nieuwe code serveerde de oude cache-data (`entries met reasoning-veld: 0`) tot `POST /api/cloud/catalog/refresh` liep. Na refresh: `openrouter/deepseek/deepseek-v4-flash-0731` → `reasoning: {supported_efforts: [max,high,low], default_effort: high, mandatory: false, default_enabled: true}`, 8 OpenRouter-entries met metadata, enkel-model-endpoint idem. **Leerstuk: code-deploy die cache-*shape* verandert vereist na restart een refresh** (of cache-TTL aflaten) — net als bij de eerdere NVIDIA-allowlist-fase. Regressie na restart: `reasoning_effort=max` via Guardian → 200 OK, finish=stop (14 reasoning-tokens bij "Say hi" = triviale prompt, geen effort-cap; conform eigen leerregel).
  - **Vervolg (operator openstaand):** nadat dit werkt, een **audit** naar andere OpenRouter-info die Guardian niet (goed) doorrouteert — o.a. per-provider pricing/context per endpoint, `max_completion_tokens`-verschillen per endpoint, en of de pass-through van andere params (bv. `logit_bias`, `top_logprobs`) correct is. Operator: "zodra we dit probleem hebben gefixed, moeten we toch ff verder kijken, of er andere relevante dingen niet worden door geroute door guardian."

### DSH session `20260824_cloud_audit` (doorroute-audit + reparatieplan, last updated 2026-08-24)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Opdracht operator:** "vervolg problemen kan je daarna verder onderzoeken, en een reparatie plan bedenken" — de audit naar andere info die Guardian niet (goed) doorrouteert, plus een reparatieplan.
- **Audit-resultaat — verrassend schoon:** de request-passthrough is grotendeels WEL correct. Gevonden:
  1. **Request-body passthrough:** in `app/cloud_inference/forwarding.py` + `routing.py` + `normalization.py` wordt **alleen** `grammar`, `json_schema` (correct, providers wijzen ze af) en de `metadata_keys`-set (`context_window`/`context_length`/`max_input_tokens`/`max_context`/`benchmark_context_limit`/`advertised_context`, correct — die waren de NVIDIA-400-oorzaak) gestript. Alles andere — `reasoning_effort`, `logit_bias`, `min_p`, `top_logprobs`, `temperature`, `max_tokens`, `response_format`, `seed` — gaat **ongewijzigd door** naar OpenRouter. Enige param-transformatie is `adapt_openai_reasoning_params` (alleen directe `openai` provider, o1/o3/o4/gpt-5: `max_tokens`→`max_completion_tokens`, temperature-beperkingen) — raakt openrouter niet.
  2. **Reasoning-metadata:** alleen OpenRouter adverteert een `reasoning:`-blok in zijn catalogus (8/21 modellen op deze key) — die worden nu correct geannoteerd. openai/google/nvidia/poolside leveren géén reasoning-blok → terecht zonder metadata (geen doorroute-gat).
  3. **GROQ/CEREBRAS: AFGEKAPTE KEYS (gevonden, OPGELOST deze sessie — zie reasoning_effort-handoff):** providers waren `configured:true` maar catalog-fetch faalde met 401; de oorzaak was dat de `.env`-keys de eerste 40 chars van langere echte sleutels waren (de volledige sleutels stonden in `~/.secrets/keys/groq.key`=56 chars en `~/.secrets/keys/cerebras.key`=52 chars). Na `.env`-fix + restart + refresh: groq 13 modellen ✓ chat 200; cerebras 2 modellen ✓ maar inferentie 402 `payment_required` (= billing op het cerebras-account, géén key/code-probleem). **Geen code-bug; Guardian stuurde `Bearer {key}` correct door.**
  4. **Niet-geadresseerde verbeterkans (geen bug):** OpenRouter's **per-endpoint** metadata (pricing/context/max_completion_tokens per upstream-provider via `/models/<slug>/endpoints`) wordt níet gesurfaced op `/v1/models` — Guardian toont één geaggregeerde `context_length` en geen per-endpoint nuance. Dit is een bewuste vereenvoudiging (één entry per `{provider}/{brand}/{model}`), geen dataverlies in routing. Loslaten of oppakken = operator-keuze.
- **Reparatieplan (status: 1-4 AFGEROND 2026-08-24, 5 losgelaten):**
  1. **Groq/Cerebras: AFGEROND.** Oorzaak was afgekapte keys (eerste 40 chars i.p.v. de volledige sleutels). `.env` hersteld met de volledige keys → **groq 13 modellen + chat 200 ✅**, **cerebras VERWIJDERD** (operator: "niet belangrijk, poging tot gratis llm gebruik" — gratis is het niet, billing nodig). Zie reasoning_effort-handoff hierboven voor details + leerstuk over `.secrets/keys/*.key`.
  2. **Credential-check: UITGEVOERD (2026-08-24).** `app/proxy/cloud_catalog.py`: `refresh_provider` onderscheidt nu 401/403-auth-fouten van transiënte fouten — `_set_auth_error(provider.name, True)` (in-memory, niet in disk-cache) en bij succes `auth_error: False`. Nieuwe accessor `is_auth_error(provider_name)`. `app/gateway/admin_api.py`: alle drie de provider-listing-endpoints (`/api/cloud/catalog`, `/api/cloud/catalog/refresh`, `/api/cloud/providers`) hebben nu per provider `credential_status` = `ok` | `broken` | `unconfigured`. Tests: `tests/unit/test_cloud_catalog.py` `TestRefreshFailure` +2 (`test_401_refresh_marks_broken_credentials`, `test_successful_refresh_clears_broken_credentials`). Live na restart: alle 6 providers `status=ok`, broken=none.
  3. **`reasoning`-veld gedocumenteerd in `docs/API_REFERENCE.md`** (nieuw representatief item blok + `reasoning`-notitie + gecorrigeerde verouderde "cloud modellen staan niet in de lijst"-noot — sinds de cloud-access-redesign staan ze er wél in). Geen restart nodig (doc-only).
  4. **`scripts/verify_post_restart.py` uitgebreid** (was config-schema-only): check #2 leest nu een echte Bearer key uit `guardian.keys.yaml` (het oude `body.get("openrouter")` was foutief — altijd None); nieuwe check #4 = "no broken credentials" (`credential_status` per geconfigureerde provider, faalt als `broken`); nieuwe check #5 = "openrouter catalog carries reasoning-effort metadata" (de cache-shape-lesson: na een code-deploy die de catalog-cache vorm verandert moet er een refresh lopen, anders faalt de check). Ook: `sys.path`-fix zodat `import app` werkt wanneer hij als `python scripts/verify_post_restart.py` wordt aangeroepen. Na restart live: **alle 5 checks PASS**.
  5. **Per-endpoint surfacing: LOSGELATEN (operator-keuze)** — zwaar, geen concrete behoefte.
- **Teststand:** 952 passed / 3 skipped / 1 failed (alweer de pre-existing google-assertion; de voorheen flaky live-inferentie-test passeerde deze run). Restart (PID 1461050) + catalog-refresh uitgevoerd, live geverifieerd: 6 providers `status=ok` (openrouter 21 / nvidia 40 / poolside 2 / google 51 / openai 123 / groq 13), cerebras 0, reasoning-metadata 8 openrouter, groq `groq/groq/compound-mini` → 200 "Hi there! 👋". **COMMIT: uitgevoerd op `main`** (groq/provider-config + veilige commit incl. reasoning-metadata + credential-check, zie git log) — na operator-beslissing "voer 1+2+4+5 uit" via keuzevraag.


### DSH session `20260821_config_schema` (config-schema split — PR #9, last updated 2026-08-21)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Config-schema migration EXECUTED on branch `config-schema-catalog-url` (PR #9)** — the plan in `docs/CONFIG_SCHEMA.md` is now IMPLEMENTED (not just planned/reviewed). The monolith `config/settings.yaml` is split into domain files (`global.settings.yaml`, `providers.settings.yaml` + `providers.overrides.yaml`, `models.local.settings.yaml`, `models.cloud.overrides.yaml`, `guardian.keys.yaml`), verified byte/data-preserving: all 11 global top-level sections identical, providers identical except `catalog_url` (now in overrides), the 4 `context_overrides` + 5 `model_defaults` merged into `models.cloud.overrides.yaml`. Legacy names are compat-symlinks; `.bak` backups gitignored. (`models.cloud.settings.yaml`/`models.local.overrides.yaml` are schema-reserved but not shipped — no runtime consumer yet.)
- **Central read switch:** `app/config_loader.py` deep-merges the full `global.settings.yaml` document into the shared CONFIG dict, then overlays merged `providers.settings.yaml` + `providers.overrides.yaml` (overrides win); this preserves `CONFIG` consumers like the server queue config while `app/proxy/providers.py` production-default `ProviderRegistry()` (no `settings_path`) reads those files + derives `context_overrides` from `context_window` entries in `models.cloud.overrides.yaml`. Explicit `settings_path=` (tests/legacy) keeps single-file compat so the 918-unit suite stays green.
- **Rewired direct readers** via `app/paths.py` helpers: scaler, capture/config, scheduler/manager, failover, engine comfyui URL, guardianctl, benchmark_suite_v1 (`local_models_file()`). `cloud_catalog`/`context_metadata`/`routing` pick up `models.cloud.overrides.yaml` through the `CLOUD_MODELS_OVERRIDES_FILE` alias — so `model_defaults` (gpt-4o temp/max_tokens etc.) now flow via `get_override`, and `context_window` via context_metadata.
- **New regression tests:** `tests/unit/test_config_schema.py` (3: load_config merge, prod-default ProviderRegistry reads merged files, path aliases resolve canonical names). Fixed 2 `test_server.py` context tests that leaked the now-non-empty overrides file (patch `cloud_catalog.get_override`). Full suite: **939 passed / 3 skipped; pre-restart gate all 4 PASS.**
- **Commits:** includes the earlier `catalog_url` work (`ef840a4`) + implement `6065635`+reviewer `1046275` (already merged in this branch history). This migration commit is pushed to `origin/config-schema-catalog-url` → PR #9 OP body = the plan doc. Reviewer-fix commits: `8c9a1ea` (recursive providers merge, guardianctl resolver, legacy-aware paths aliases, reserved files removed) + `305d41a` (reload() drops stale catalog cache on hot-reload) + **reviewer-pushed `4a4bfcc`/`2ac9be2`** (load_config deep-merges the full global doc; tightened regression coverage).
- **MERGED + LIVE (2026-08-22, merge commit `a5a030c`, restarted same night)** — PR #9 is merged (local branch deleted) and the service was restarted by the agent (operator consented). **Live verification (all green):** service active; auth loads `guardian.keys.yaml` (36 keys); `config_loader` deep-merges the full `global.settings.yaml` (queue present with correct values); 5 providers configured; `/v1/models` = 107 entries (43 local incl. `llama3.2-3b`, openrouter filtered to the 22 `/models/user` reachable models via `catalog_url`); local route `llama3.2-3b` → 200 "OK" (~142 t/s); cloud route `openrouter/moonshotai/kimi-k3` → 200 (DeepInfra); `POST /api/cloud/catalog/refresh` → openrouter 22, nvidia 102, poolside 2, google 51, openai 123. Note: the 18 openrouter ids in `/v1/models` pre-refresh come from the explicit `models:` fallback list in `providers.settings.yaml` (design, not a regression); the dynamic catalog populates on refresh/`ensure_fresh`. The cloud 404s for claude-3.5-sonnet/gemini are the known upstream account-privacy restriction, not a Guardian regression. Verification helper: `scripts/verify_post_restart.py` (service/auth/config/catalog checks). `config/guardian.keys.yaml` is gitignored (secrets).

### Benchmark session `20260815_bench` (last updated 2026-08-15)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Alle 18 benchmarkbare modellen (excl. embedding + laguna, op verzoek overgeslagen) gemeten via Guardian** → `docs/MODEL_BENCHMARKS.md`. Script: `scripts/bench_all_models.py` ( Guardian-native, leest `config/models.yaml`, streamt via `/v1/chat/completions`, meet load+switch/TTFT/gen tps/prompt eval tps, resumable via `data/bench-models/state.json`).
- **13 ✅ geslaagd / 4 ❌ OOM / 1 ❌ GGUF laad-fout:**
  - Snelste: `llama3.2-3b` 126 t/s, `Qwen3.6-…-Aggressive` 85 t/s, `Qwen3.6-…-Q8KV`/`Turbo4` 82 t/s
  - Nieuw geïnstalleerd + werkend: `granite-4.1-8b` 31 t/s (turbo4), `qwen3.8-27b` (thinking) 64 t/s, `qwen3.8-27b-instruct` 17 t/s
  - **OOM (KV-cache bij ctx=262144 + grote weights):** `Ministral-3-14B-Reasoning-2512` (Q8 ~15GB), `Qwen3-30B-A3B-Thinking-2507` (Q4_K_M ~18GB), `Step3-VL-10B` (F16 ~20GB), `google-gemma-4-12B-it-qat-q4_0-GPU1` — alle op ngl=99 kv=turbo4. Fix: `context` verlagen (naar 32768/131072) of ngl deels naar RAM. turbo4 alleen rekt het niet voor deze vier.
  - **GGUF laad-fout (geen OOM):** `Ornith-1.0-35B` — `llama_model_loader: failed to load model from .gguf` na een fit-params error. Bestand mogelijk corrupt/truncated → opnieuw downloaden.
- **Config-aanpassingen door gebruiker (turbo4 rollout, 2026-08-15):** `kv_type` f16/q4_0/q8_0 → `turbo4` voor `Ministral-3-14B`, `Qwen3-30B-A3B`, beide `Huihui-gemma-4-26B`/`unsloth-gemma-4-26B`, `Step3-VL-10B`, `Ornith-1.0-35B`, `google-gemma-4-12B` (naast de reeds-gecommitte turbo4 voor granite + qwen3.8). `ngl` overal naar 99 waar voorheen gedeeltelijk (40/30/48). Alles in `config/models.yaml`.
- **Bench-script verbeterd:** error-classificatie in `write_doc` (OOM/GGUF-laad-fout/load-failed/crash i.p.v. afgekapte JSON-fragmenten); failed modellen krijgen nu een per-model detail-blok met diagnose (voorheen overgeslagen). `--only` filter beperkt nu ook de tabel-regeneratie (volledige state via `write_doc(state, alle-entries-zonder-laguna)`).
- **Niet herstart na user-edits vr OOM-herretse:** de 4 OOM-modellen faalden met turbo4; oplossing is `context` verlagen (niet `kv_type` — turbo4 staat al aan). Operator-beslissing welke ctx-override per model.
- **Cleanup (2026-08-15 ~14:05):** 3 modellen verwijderd uit `config/models.yaml` + aliases + `~/.pi/agent/models.json` omdat de GGUF-bestanden van schijf zijn gehaald: `Ornith-1.0-35B` (corrupt), `laguna-s-2.1-ud-iq4_xs-160k-tq4` (te traag, op verzoek), `google-gemma-4-12B-it-qat-q4_0-GPU1` (verwijderd door operator). `docs/MODEL_BENCHMARKS.md` geregenereerd (16 modellen over). `/v1/models` sync via herstart → 106 entries.
- **Resterende 3 OOM-modellen (geïsoleerd herretse bij schone restart — faalden opnieuw, turbo4 alleen onvoldoend):** `Ministral-3-14B-Reasoning-2512` (Q8 ~15GB, cudaMalloc failed 5016 MiB op device 0), `Qwen3-30B-A3B-Thinking-2507` (Q4_K_M ~18GB, 3036 MiB), `Step3-VL-10B` (F16 ~20GB, 4488 MiB). De fout is telkens `common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort` — KV-cache bij `ctx=262144` ooverschrijdt vrije VRAM op GPU0 na weights-offload. Fix-richting: `context` verlagen (bv. 32768 of 131072) per model in `config/models.yaml`. Operator nog niet aangepast — wachten op beslissing.
- **Definitieve cleanup (2026-08-15 ~14:30):** operator besloot dat de 3 resterende OOM-modellen (Ministral, Qwen3-30B, Step3-VL) nutteloos zijn — alle 3 verwijderd uit `config/models.yaml` + aliases + `~/.pi/agent/models.json`. Focus operator ligt op `Huihui-gemma-4-26B-A4B-it-abliterated-Q8KV` (54 t/s) en `qwen3.8-27b` (64 t/s thinking / 17 t/s instruct) — beide live geverifieerd werkend (denk-branch + `OK` reply, `finish=stop`).
- **`docs/MODEL_BENCHMARKS.md` nu gesorteerd op snelheid** (snelst bovenaan, falende/pending onderaan) — `write_doc()` in `scripts/bench_all_models.py` gebruikt `_table_sort_key` (gen_tps desc → failed alpha → pending). Tabel-top: llama3.2-3b 126 t/s → Qwen3.6-Aggressive 85 t/s → … → Huihui-gemma-4-26B (niet-Q8KV) 5.5 t/s.
- **Qwen3.8 comment-correctie (feit-check via GGUF-metadata):** het comment "Qwen3.8 (IBM-dense 27B VLM, hybrid SSM+attention, qwen35 arch)" was **foutief**. Direct uit `general.*` metadata van `Qwen3.8-27B-UD-Q4_K_XL.gguf`: `general.architecture = qwen35` ✅ (dat deel klopte), `general.name = Qwen3.8-27B`, `general.organization = absent` (geen IBM — Qwen is Alibaba), `general.repo_url = huggingface.co/unsloth` (Unsloth = quantizer, niet maker). "IBM-dense" was een hallucinatie, vrijwel zeker SPF-confluence met IBM Granite-4.1 (dat wél IBM + hybrid SSM is, in dezelfde sessie geïnstalleerd). "hybrid SSM+attention" kon niet uit metadata bevestigd worden en is ook verwijderd. Comment nu: "Qwen3.8-27B (Alibaba Qwen, arch: qwen35; Unsloth UD-Q4_K_XL quant)".
- **Qwen3.8-27B-Q8KV variant toegevoegd (2026-08-15):** nieuwe entry `qwen3.8-27b-q8kv` in `config/models.yaml` + alias `qwen3.8-q8kv` + `~/.pi/agent/models.json` entry. Clone van `qwen3.8-27b` met **alleen geheugen-settings** overgenomen van `Qwen3.6-…-Q8KV`: `kv_type: turbo4→q8_0`, `tensor_split: "0.45,0.55"→"0.36,0.64"`. Alle andere settings (path, total_layers, context 262144, ngl 99, mmproj, samplers, `default_enable_thinking: true`) identiek. q8_0 KV = hogere quality dan turbo4 (~2× VRAM). Nu **16 lokale modellen** in models.yaml.
- **Nieuwe Critical rule: maximize subagent usage with fresh context.** Delegeer zoveel mogelijk werk aan fresh-context workers; de lead houdt het plan + doet de cross-file synthese. Constraint: subagent-model `guardian/openrouter/deepseek/deepseek-v4-flash-0731:high` routeert VIA Guardian → een Guardian-restart orphanet lopende subagents (doe restarts dus als lead met een rechtstreeks-openrouter model, niet via een worker). Max 3 simultane subagents.
- **Config-drift detectie geimplementeerd (2026-08-15, code-only, niet live):** `app/engine/manager.py` krijgt een **launch-signature**-mechanisme zodat een model altijd herladen wordt wanneer zijn `models.yaml`-entry verandert — zonder handmatige restart-trucs. Achtergrond: `llama-server` is een **aparte systemd-service** (`_start_server` doet `sudo systemctl start llama-server`), géén kind-proces van Guardian → een `systemctl restart llama-guardian` raakt llama-server niet en laat de oude config draaien. De oude `startup_check` vond de backend levend (verify op `.gguf`-pad) en heeft hem geadopteerd; de oude `switch_model` skip-reload bij `model_name == current_model`. Beide gaten zorgden ervoor dat aangepaste settings nóóit werden toegepast.
  - **Werkzaam:** nieuwe methodes `_build_args_string` (extractie uit `_write_server_args`, byte-identieke args+env — single source of truth), `_compute_launch_signature` (SHA-256 van args + gesorteerde JSON-env, getagd met model+vision), `_read/_write_persisted_signature` (`config/current_model.sig`, `.gitignore` bijgewerkt), `_config_drifted`.
  - **4 hooks:** (1) `_detect_initial_model` prefereert signature-boven fragiele arg-scoring; (2) `startup_check` forceert reload bij drift en adopteert géén stale backend; (3) `switch_model` skip-voorwaarde krijgt `and not drifted` + drift-forcering; (4) na een succesvolle launch wordt de signature gepersisteert.
  - **Validatie:** alleen unit-tests met gemockte backend (géén echte model-loads, dat is operator-taak) — `tests/unit/test_manager.py` +7 tests (drift-skip, reload bij kv_type/tensor_split/extra_argschange, startup-forcering, detect-prefers-signature, build_args_string byte-identical). 91 manager-tests pass. `py_compile` + `pyflakes` clean.
  - **Nog niet live:** code-change vereist `sudo systemctl restart llama-guardian` (sessie valt tijdens restart). Bij de eerste herstart na deze commit geldt direct: geen concern meer dat een models.yaml-edit genegeerd wordt — de drift-check herlaadt automatisch. Operator voert de restart uit.
- **Client context-hint feature (2026-08-15, code-only, niet live):** een client kan een KLEINERE context aanvragen dan geconfigureerd — via HTTP header `X-Guardian-Context: <int>` of body field `guardian_context` (header wint). De hint wordt geklemd op [4096, config.context] (nooit groter dan config — clients kunnen de KV niet vergroten) en door de bestaande launch-signature-driftdetectie gejaagd: `build_runtime_config(..., context_hint=N)` → `_compute_launch_signature` (bevat al `-c N`) verschilt van de gepersisteerde sig → `switch_model` herlaadt met de kleinere `-c`. **Geen nieuwe reload-logica** — de signature bevatte de ctx al. Zelfde hint twee keer = sig match = géén reload (gratis caching). Routing: `route_v1_post` leest de hint en geeft `context_hint` door aan alle `switch_model`/`load`-calls; een hint op het actieve model gaat via `switch_model` (drift-check + sig-persist) i.p.v. `load` (die altijd herlaadt).
- **Per-model `n_slots` config field:** `_build_args_string` appends ` --parallel {n_slots}` (alleen als >1) aan de args — en dus aan de signature (n_slots-verandering = drift = reload, correct). Zonder het veld géén `--parallel`-flag (llama-server default). Demo: `Qwen3.6-35B-A3B-HauhauCS-Aggressive-Turbo4` kreeg `n_slots: 8` (met client-hinted kleinere ctx 8 parallelle slots voor hogere throughput).
- **Limitation:** Guardian heeft ONE live backend per model — opeenvolgende hints met verschillende ctx herladen dus nog steeds (1-actief-per-keer; caching alleen over identieke-ctx-requests). Convenience accessor: `ModelManager.current_launch_context()` leest de actieve `-c` uit `current_model.args`.
- **Validatie:** `py_compile` clean; `tests/unit/test_manager.py` 100 pass (91 bestaand + 9 nieuw: clamp onder/boven/floor/None, drift-reload via hint, zelfde-hint-geen-reload, `--parallel 8` append, geen-`--parallel` backward compat, `current_launch_context` accessor). **Nog niet live:** vereist `sudo systemctl restart llama-guardian` (sessie valt tijdens restart — operator voert uit).

### GCD implementation session `20260815_gcd` (last updated 2026-08-15)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Grammar-Constrained Decoding (GCD) implemented as a first-class option** (spec: `docs/GCD_IMPLEMENTATION_SPEC.json`). All 7 FEATs implemented:
  - **FEAT-1** — local OpenAI passthrough contract pinned by regression tests (`tests/unit/test_grammar_passthrough.py`): `response_format`/`json_schema`/`grammar` reach llama-server byte-identical; no body allowlist strips unknown fields.
  - **FEAT-2** — Ollama bridge maps `options.format` (dict→`response_format`, string→`grammar`) in both `chat_ollama` and `generate_ollama` (`app/local_inference/ollama.py`); explicit top-level fields win; kill-switch gates the mapping.
  - **FEAT-3** — cloud routes strip GBNF/`json_schema`, preserve OpenAI-native `response_format`, optional JSON auto-conversion (`grammar.cloud_auto_convert_json`), optional strict 400 naming the provider (`grammar.cloud_strict_mode`) in `app/cloud_inference/forwarding.py`.
  - **FEAT-4** — `grammar` block in `config/settings.yaml` (`enabled`, `cloud_auto_convert_json`, `cloud_strict_mode`, `validate_gbnf`) + typed accessors in `app/config_loader.py` (`get_grammar_*`); per-model `grammar_decoding` hint in `config/models.yaml`. `grammar.enabled=false` is the process-wide kill-switch (strips on local + cloud).
  - **FEAT-5** — optional GBNF pre-validation in `app/gateway/normalization.py` (`validate_grammar_field`, structural checks, fail-open, off by default).
  - **FEAT-6** — capture events expose `grammar_present`/`response_format_present` booleans only; raw grammar/schema content is never stored (`structured_output: strip` policy in `app/capture/policy.py` + redactor).
  - **FEAT-7** — docs: `docs/API_REFERENCE.md` (new GCD section), `docs/LLM_ROUTER.md` (cloud GBNF note), this AGENTS.md entry.
- **Config keys added:** `grammar.enabled` (default true), `grammar.cloud_auto_convert_json` (default false), `grammar.cloud_strict_mode` (default false), `grammar.validate_gbnf` (default false) in settings.yaml; optional `grammar_decoding: true|false` per model in models.yaml.
- **Accessors added:** `config_loader.load_grammar_config()`, `get_grammar_enabled()`, `get_grammar_cloud_auto_convert_json()`, `get_grammar_cloud_strict_mode()`, `get_grammar_validate_gbnf()`.
- **Validation:** full suite 921 passed / 3 skipped; new GCD tests: 2 (passthrough) + 6 (ollama mapping) + 10 (cloud stripping) + 8 (validation/capture) = 26. `pre_restart_check.py` not yet run at last update — operator must run it before restart (config + code changes require `sudo systemctl restart llama-guardian`; session drops during restart).
- **Reviewer fixes applied (2026-08-15, post-review run d8bc1417):** read-only review of the full GCD diff surfaced 3 must-fix findings — all applied + regression-tested (full suite now **926 passed / 3 skipped**, +5 new regression tests, `pre_restart_check.py` all 4 gates PASS):
  1. **FEAT-2 Ollama `format: "json"` sentinel** — the mapping treated `options.format == "json"` (Ollama JSON-mode sentinel, NOT GBNF) as a grammar string, producing `grammar: "json"` → llama-server GBNF parse error (undefined rule). Fixed in `app/local_inference/ollama.py`: `"json"` now maps to `response_format: {"type": "json_object"}`. Regression: `test_json_sentinel_maps_to_response_format_not_grammar`.
  2. **FEAT-4 kill-switch precedence over strict-mode** — `app/cloud_inference/forwarding.py` checked `_grammar_cloud_strict_mode` BEFORE `_grammar_enabled`, so with the kill-switch OFF and strict-mode ON, a cloud request carrying GBNF would 400 instead of stripping. Kill-switch is the enforced control and must win. Fixed: `if not _grammar_enabled` now evaluated first (strips unconditionally), strict-mode only applies when grammar is enabled. Regression: `test_kill_switch_overrides_strict_mode`.
  3. **FEAT-6 `options.format` leak in redactor** — `app/capture/redactor.py` only stripped top-level `grammar`/`json_schema`/`response_format` keys (in `STRUCTURED_OUTPUT_KEYS`), but Ollama clients carry grammar/schema under `options.format`. The generic dict recursion preserved the raw grammar/schema content — a privacy contract violation. Fixed: explicit `options.format` redaction (content → `[REDACTED]`, rest of `options` preserved) when `structured_output: strip`. Also propagated to `app/gateway/capture_dispatch.py`: `grammar_present` flag now honors `options.format`. Regressions: 3 tests in `test_capture_redactor.py` (`test_strips_nested_options_format_schema`, `test_strips_nested_options_format_grammar_string`, `test_preserves_options_format_when_policy_is_capture`).
  4. **Docs (M3):** `grammar_decoding` per-model field in `docs/API_REFERENCE.md` corrected from “capability hint (default true/false)” to “advisory capability hint only; not consumed by runtime routing logic” — matches reality (the field is documentation-only; the kill-switch is the enforced control).
- **KV-cache tuned to `turbo4` for granite-4.1-8b + both qwen3.8 entries (post-restart, 2026-08-15):** initial `kv_type: f16` with `ngl: 99` caused OOM on all three models — granite (Q8_K_XL, 40 layers) and qwen3.8-27b (Q4_K_XL, 65 layers, large 262144 context) both failed with `cudaMalloc failed: out of memory` during KV-cache alloc because f16 KV over all layers at full context exceeds free VRAM. Switching `kv_type: f16 → turbo4` (Q4 KV cache, ~4× smaller) resolved OOM for all three; qwen3.8 `ngl: 40 → 99` (was under-fitting — only 40/65 layers offloaded). All 3 verified via Guardian `/v1/chat/completions`: granite 'OK', qwen3.8-27b (thinking) 'OK' (23 tok incl. thinking branch), qwen3.8-27b-instruct 'OK'. **Lesson: for large-context models (≥131072) with full-layer offload, `kv_type: turbo4` is the safe default — f16 KV only fits small-context or partial-offload configs.**
- **GCD + reviewer fixes + models committed (commit `15c5309`, pushed to origin/main).**

### Pi session `20260815_1` (last updated 2026-08-15 ~08:45)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Dashboard offline diagnose — volledig uitgekamd.** Gebruiker meldde `http://192.168.1.35:11437/` offline. Drie onafhankelijke lagen gevonden:
  - **Netwerklaag:** `app/main.py:448` bindt expliciet `host="127.0.0.1" port=11437` (by-design sinds commit `7472d61`, 2026-07-30 "security: add auth to dashboard :11437, bind to 127.0.0.1 only") → `192.168.1.35:11437` = connection refused.
  - **Tunnellaag:** `cloudflared-m0nkdash.service` routeerde `dashboard.oelala.xyz` → `http://127.0.0.1:18082` (m0nkdash-project `/home/flip/m0nkdash/`, handmatig via `serve.sh`, géén systemd-unit, al een maand dood) → tunnel healthy maar origin dood.
  - **Applicatielaag (verborgen hoofdoorzaak):** `app/ui/index.html` fetches stuurden GEEN `Authorization` header → sinds commit `7472d61` (die Bearer op `/api/*` plaatste) was het dashboard functioneel kapot (lege shell), zelfs lokaal. `curl 127.0.0.1:11437/api/stats` → 401.
- **Dashboard-UI-fix (uitgevoerd, GEEN Guardian-restart nodig):** `app/ui/index.html` bewerkt (4 edits, ~93 regels toegevoegd):
  - Header key-status indicator (klikbaar → modal), key-input modal (password + Save/Clear/Cancel), fetch-wrapper (monkey-patch `window.fetch` die automatisch `Authorization: Bearer <key>` uit localStorage injecteert op elke `/api/*` call; bij 401 cleart hij de key en heropent de modal), modal-wiring in `wireControls()` + auto-open bij page-load als geen key.
  - Key opslag: localStorage `guardian_dashboard_api_key`.
  - Geverifieerd: `GET /` 200 (104KB HTML), `GET /api/stats` 401 zonder key, `GET /api/stats` 200 met `pi_...` key. `FileResponse` serveert per-request van schijf → edit is live zonder restart. API-key = dict-key in `config/api_keys.json` (exacte string-match in `app/proxy/auth.py:333`, geen hashing).
- **nginx LAN reverse-proxy (uitgevoerd):** `deploy/nginx/llama-guardian-dashboard.conf` geschreven (worker subagent): `listen 192.168.1.35:11437; proxy_pass http://127.0.0.1:11437;` SSE/WS headers (Upgrade, Connection upgrade, `proxy_buffering off`), IP-allowlist `allow 192.168.1.0/24; deny all;` (defense-in-depth: subprocess-per-401 DoS-vector). Gekopieerd naar `/etc/nginx/sites-enabled/` + `nginx -t` + `systemctl reload nginx` (geen Guardian-restart). `curl http://192.168.1.35:11437/` → 200.
- **cloudflared tunnel ombouw (uitgevoerd):** `/etc/cloudflared/m0nkdash-config.yml` + `~/.cloudflared/m0nkdash-config.yml`: `service: http://127.0.0.1:18082` → `http://127.0.0.1:11437`. `cloudflared tunnel ingress validate` OK. `sudo systemctl restart cloudflared-m0nkdash`. `dashboard.oelala.xyz` → nu Guardian dashboard (achter Cloudflare Access, al provisioned). m0nkdash-origin blijft dood (apart te herstarten met `serve.sh` als ooit gewenst). Raakt Guardian niet.
- **Subagent-infra decode (belangrijk voor toekomstige sessies):** de default subagent-model-string in `~/.pi/agent/settings.json` was `guardian/openrouter/deepseek/deepseek-v4-flash-0731:high` — die faalde met "Unknown subagent model in active Pi model registry". Root cause: pi's model-registry laadt `models.json` alleen bij startup (NIET via `/reload` of `/reload-runtime`); én de `guardian/`-prefix-vorm moest expliciet toegevoegd worden aan de `providers.guardian.models` array in `~/.pi/agent/models.json`. Fix: entry toegevoegd (gemodelleerd op `guardian/openrouter/deepseek/deepseek-chat`), pi volledig herstart, daarna werkt `guardian/openrouter/deepseek/deepseek-v4-flash-0731:high` voor subagents. De bare vorm `openrouter/deepseek/deepseek-v4-flash-0731` faalt met 401 (pi routeert direct naar OpenRouter i.p.v. via Guardian). OpenRouter-key is disabled → ALLE subagent-traffic via Guardian. Constraint: max 3 simultaneous subagents (Guardian raakt 429-failover bij >3 concurrent reasoning-requests; Novita upstream rate-limits).
- **Constraints/preferences opgeslagen in project-memory:** subagent-modelconfig (context), max-3-simultaan (constraint).

### Pi session `20260816_1` (capture live, last updated 2026-08-16)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Capture subsystem ENABLED (operator decision, "schakel in"):** `config/settings.yaml` → `capture.enabled: true`, `local_capture: true` (cloud stays off — provider terms not accepted), `allowed_client_refs` now lists all 33 named API-key refs (HMAC-SHA-256 with the real `GUARDIAN_CAPTURE_CLIENT_REF_SECRET`). `per_client_opt_in` stays true — only known clients are captured.
- **SECRET FIX:** `.env` `GUARDIAN_CAPTURE_CLIENT_REF_SECRET` was a PLACEHOLDER (literally `generate import secrets; print(secrets.token_hex(32))`) — never a real secret. Generated real 64-hex secrets for both `GUARDIAN_CAPTURE_CLIENT_REF_SECRET` and the new `GUARDIAN_CAPTURE_RECORD_AUTH_SECRET`. Existing `allowed_client_refs` were therefore recomputed; the old placeholder refs were never deployed (list was empty).
- **Bug found during enablement — event-loop rebind:** with capture on, the WAL writer busy-spun at 100% CPU (2.8 GB RSS!) when a second lifespan start ran on a fresh event loop (pytest per-test loops): `CaptureSink`'s `asyncio.Queue` binds to the loop of its first blocking use, so `get()` raised `RuntimeError: ... is bound to a different event loop` and the writer's catch-all `continue` spun. Fixes in `app/capture/`:
  - `sink.py::CaptureSink._rebind_queue_if_needed()` — recreates the queue when the running loop changed (fail-open, pending items dropped)
  - `wal_writer.py::_run()` — consecutive-error counter + 0.5 s backoff; stops after 50 persistent errors instead of spinning forever
  - `wal_writer.py::_close_active_file()` — now resets `_active_file_size`/`_active_file_start`; a stale size made manual `rotate()` return None after an automatic rotation (real production bug, not just tests)
  - Tests: `TestSinkEventLoopRebind` (2 regression tests) + the 2 rotation tests were timing-dependent (10 events = 2070 B > 1024 B auto-rotate limit) and now use a roomy per-test `max_file_bytes`
- **Pre-restart gate: ALL 4 PASS** (950+ passed). Restart required to activate capture; agent traffic routes through Guardian — session drops during restart.
- To verify after restart: `curl -s localhost:11434/api/capture/status` shows enabled + writer active; `data/capture/guardian_capture_current.jsonl` appears after first opted-in request; Keanu can run the live contract test on the real WAL.

### DSH session `20260816_2` (model install, last updated 2026-08-16)

- Working directory: `/home/flip/llama_cpp_guardian`
- **New model installed: `qwen3.8-27b-uncensored-ymq`** (operator request: HF `zerodigest/Qwen3.8-27B-Uncensored-YMQ-MTP-GGUF`, `Qwen3.8-27B-Uncensored-YMQ-XL.gguf`). Source: YMQ-Compiler v2.0 mixed-precision quant of `JonathanColetti/Qwen3.8-27B-Uncensored` — an abliteration fine-tune of the same qwen35 base (65 layers, hybrid SSM+attention). XL preset ≈19.7 GB (larger than the 17.9 GB UD-Q4_K_XL base). MTP heads preserved natively → `spec_type: draft-mtp` like the sibling `qwen3.8-27b-mtp`.
  - **Download:** `hf download zerodigest/Qwen3.8-27B-Uncensored-YMQ-MTP-GGUF Qwen3.8-27B-Uncensored-YMQ-XL.gguf --local-dir /home/flip/models/qwen3.8-27b-uncensored-ymq/`.
  - **mmproj REUSED** from `/home/flip/models/qwen3.8-27b/mmproj-F16.gguf` — abliteration freezes the vision tower, so projector weights are shape-compatible. Created NO new mmproj dir.
  - **Config (zoals gecommitted op 2026-08-16):** `config/models.yaml` entry `qwen3.8-27b-uncensored-ymq` — turbo4 KV, `context: 150000` (NIET 242144 — de XL-weights zijn ~19.7 GB, dus een kleiner kv-venster om OOM te vermijden; benchmark_context_limit 262144), `ngl: 99`, `tensor_split: "0.42,0.58"`, mmproj van base-qwen3.8-27b, `default_enable_thinking: true`, `spec_type: draft-mtp`, `--jinja` samplers + aliases `qwen3.8-uncensored` / `qwen3.8-ymq`. `~/.pi/agent/models.json` entry added (reasoning true, text+image, ctxWindow 242144).
  - **Validatie:** YAML+JSON parse clean; `tests/unit/test_manager.py` 107 passed (config-drift detection unaffected). `py_compile` clean (no code touched).
  - **Nog niet live:** config edit vereist `sudo systemctl restart llama-guardian` om de nieuwe entry zichtbaar te maken in `/v1/models` (en `llama-server` zal de nieuwe GGUF bij eerste switch laden — launch-signature-driftdetectie herlaadt automatisch). Operator voert de restart uit (sessie valt tijdens restart). Verifieer na restart: `curl -s localhost:11434/v1/models | grep qwen3.8-27b-uncensored-ymq`; eerste request triggert ~30s laadtijd + MTP draft-context build.
  - **Also in this commit (2026-08-16/19, operator-driven config + CLI fix):** `config/settings.yaml` kreeg `z-ai/glm-5.3` als OpenRouter-only failover-mirror (NVIDIA NIM host nog alleen glm-5.2, gecheckt 2026-08-19) in de `providers.openrouter.models` failover-groep. `scripts/guardianctl.py` `test-event` was broken tegen de capture-refactor (`CaptureSink(config=cfg)` + `sink.write(event)` bestaan niet meer); gefwixt met de nieuwe async lifecycle (`CaptureSink(max_pending_events=…)` + `CaptureWALWriter.start()/stop()` + `sink.try_put()` + `build_request_received_event(cfg, …)`). `llama_cpp_guardian.code-workspace`: `python.analysis.exclude` (venv/models/.config) om Pylance-traffic te scheren.

### DSH session `20260819_1` (config hot-reload + ownership repair, last updated 2026-08-19)

- Working directory: `/home/flip/llama_cpp_guardian`
- **LIVE (restarted 2026-08-19, commit `c1c603c` + follow-up):** no-restart
  config reload (`POST /api/config/reload`) and legacy credential
  ownership repair (`POST /api/cloud/credentials/claim`) are deployed and
  verified. The old "No hot reload" critical rule is superseded: config
  edits to `settings.yaml`/`cloud_keys.json` now hot-reload via the
  endpoint (code still needs a restart).
- **keanu-factory key now linked (operator decision: clean way, no token
  swap):** hermes (`94062b64e5d5`) claimed the owner-less legacy nvidia +
  openrouter credentials via `/api/cloud/credentials/claim`, then linked
  keanu-factory (`7e573421cf2a`) to nvidia + openrouter; claudekvm2
  (`17aa6e789057`, google owner) linked keanu to google. Verified live:
  keanu token → `guardian/openrouter/moonshotai/kimi-k3` 200,
  `guardian/google/gemini-3.5-flash` + `gemini-flash-latest` 200 (the old
  `gemini-2.5-flash` is no longer available to new users upstream).
  `config/cloud_keys.json` is gitignored (secrets) — ownership/links live
  in the file only. Helper: `scripts/link_keanu_key.py`.
- **Verification report delivered**: `docs/free-tier-pool-verification.md`
  answers the 7 points of `docs/free-tier-pool-request.md` (Operator update:
  `minimax/minimax-m3:free` does NOT exist on OpenRouter — minimax-m3 group =
  NVIDIA NIM only; OpenRouter free groups separately; Google via direct
  google credential only). **Nothing of the pool config is implemented** —
  no `failover_groups`, no `cloud_capture` flip — until Keanu has run the
  probe (operator instruction). New tests: `tests/unit/test_config_reload.py`
  (7) + `tests/unit/test_cloud_keys_claim.py` (5); full suite green
  (964 passed / 3 skipped, pre-restart gate all 4 PASS).

### DSH session `20260820_1` (model-discovery architecture, last updated 2026-08-20)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Documented the `/v1/models` architecture that was previously only implied.**
  Added Critical rule "`/v1/models` has four sources and is per-key" — see
  Critical rules. Motivation: the pi/mindcraft list-difference (217 vs 118
  models) was not explained anywhere and had to be reverse-engineered from
  `app/gateway/model_discovery.py`. Root cause: only source (2) `settings.yaml`
  `providers.*.models` is key-independent-global; source (3) the per-key
  `guardian/{provider}/{model}` routes come from `cloud_keys.json`
  (`get_linked_models_for_key`), so an unlinked key (mindcraft, fp
  `43ed9e97823f`) sees 0 `guardian/` routes while a fully-linked key (pi, fp
  `c1824126c6fb`) sees all of them. Confirmed live with both keys.
- **Strengthened the "AGENTS.md is always updated" Critical rule** to
  explicitly include *fresh findings / reverse-engineered repository facts*,
  so a future session that has to dig into code writes that understanding down
  immediately instead of letting the next session re-derive it.
- Docs-only change: no code, no restart, no tests affected. Committed + pushed.

### DSH session `20260820_cloud_refactor` (cloud access redesign — **LIVE 2026-08-21**)

- Working directory: `/home/flip/llama_cpp_guardian`
- **MERGE + RESTART DONE.** PR #7 merged into main (`1f57f12`), working tree switched to main, service restarted 2026-08-21 19:37 UTC. The cloud-access redesign is **live**. `config/cloud_keys.json` still on disk as a backward-compat source for `FailoverRegistry` failover_groups (deletable once live, still present).
- **Post-restart bug found + fixed (google `models/` prefix, commit `85e0550`).** google's OpenAI-compatible `/v1/models` returns `models/gemini-2.5-flash` (with a literal `models/` prefix). `_normalize_upstream_id` treated that '/'-containing id as already-namespaced and kept it, producing the 2-segment `google/models/gemini-...` instead of the consistent `google/google/gemini-...`. Fixed by stripping a leading `models/` prefix (matching the removed `normalize_google_model_id` behavior). Regression test `test_google_models_prefix_stripped`. This is the only post-restart fix needed so far.
- **Live verification (all green, 2026-08-21):**
  - `/v1/models` = **743 models**, provider-global, consistent `{provider}/{brand}/{model}` (openrouter 422, nvidia 102, poolside 2, google 51, openai 123 + ~44 local). google models advertise `google/google/gemini-2.5-flash` etc.
  - `GET /api/cloud/catalog` + `POST /api/cloud/catalog/refresh` work (cold-start cache populated → live counts above).
  - Cloud route forwards: `google/google/gemini-3.5-flash` → 200, upstream model echoed as `models/gemini-3.5-flash` (the catalog maps `google/gemini-...` back to the `models/...` upstream id correctly).
  - Local route intact: `llama3.2-3b` → 200 'OK'.
  - `cloud_gateway_access`: all 36 keys default `true` (0 on false → everyone keeps cloud access).
  - Capture attribution intact: downstream of the credential-store removal, capture is keyed on the auth context HMAC `client_ref` — confirmed present in live records (reviewer point 5 closed).
  - NOTE: bare `openrouter/deepseek/deepseek-chat` → 404 "No endpoints available matching your guardrail restrictions" is the **upstream OpenRouter account privacy setting** (pre-existing, documented), not a Guardian regression — routing itself works.
- **Design decisions (operator-approved):**
  - All YAML (`guardian_apikeys.yaml`, `local_models.yaml`, `cloud_models.yaml`),
    cloud_keys.json removed (still on disk until the operator-run restart; now
    only a backward-compat source for `FailoverRegistry` failover_groups).
  - One key source: `settings.yaml` `providers.*.api_key` via `$ENV` (.env);
    google added as a first-class provider + `GOOGLE_API_KEY`/`POOLSIDE_API_KEY`
    migrated into `.env` from cloud_keys.json.
  - No credentials/links/ownership; per-key access = `cloud_gateway_access:
    true|false` (default true) in `guardian_apikeys.yaml`.
  - Dynamic cloud catalog (`app/proxy/cloud_catalog.py`): fetches each
    provider's `/v1/models`, normalizes to `{brand}/{model}`, TTL + cold-start
    disk cache (`data/cloud_catalog_cache.json`) + auto-refresh;
    `config/cloud_models.yaml` = per-model overrides only.
  - Model format: cloud `{provider}/{brand}/{model}` — `guardian/` prefix
    **dropped** (2026-08-21, after copilot review #7 point 1) so existing
    bare-name clients keep working; only the `{brand}` segment is the real
    change (openai/google previously omitted it). Local keeps its bare name.
    Example: `google/google/gemini-3.5-flash`, `openrouter/deepseek/deepseek-v4-flash-0731`.
- **Implementations made on `cloud-access-redesign` (commits `4329d7c`, `28e97ad`):**
  - Key store: `guardian_apikeys.yaml` (YAML) standard; `api_keys.json` is a
    read-only legacy alias migrated on first save. `models.yaml` is now a
    symlink to `local_models.yaml`.
  - `CloudCredentialStore` + `parse_guardian_route` deleted; all
    credential/link/claim/refresh admin + UI endpoints removed.
  - New admin endpoints: `GET /api/cloud/catalog`, `POST /api/cloud/catalog/refresh`.
  - `/v1/models` is provider-global from the dynamic catalog, gated per-key on
    `cloud_gateway_access`; failover groups are `failover/{group}` and now read
    `failover_groups:` from `settings.yaml` (cloud_keys.json fallback).
  - `cloud_gateway_access` default `True` when absent → existing keys keep cloud
    access; a key set `false` is local-only (cloud routes 403).
  - Context metadata resolves `cloud_models.yaml` context overrides first.
- **Reviewer (copilot-swe-agent) notes on PR #7:** (1) no breaking change for
  bare-name clients (addressed by dropping `guardian/`); (2) cold-start fallback
  catalog implemented (disk cache + keep-last-on-failure); (3) `cloud_models.yaml`
  scope example shipped; (4) poolside `/v1/models` — the dynamic catalog fetches
  all configured providers incl. poolside via the same `httpx` helper (verify a
  live refresh at `POST /api/cloud/catalog/refresh` after restart);
  (5) capture attribution — capture is keyed on auth context (client_fingerprint),
  not the credential store, so it is unaffected by credential removal (verify a
  live cloud request's capture event after restart).
- **Live verification (operator task after the restart):** confirm
  `/v1/models` shows the unified `{provider}/{brand}/{model}` list (~217 global),
  a linked key like pi still returns cloud entries, `GET /api/cloud/catalog`
  reports per-provider counts, and `POST /api/cloud/catalog/refresh` populates
  the catalog. `config/cloud_keys.json` can be deleted once live.
- **IMPORTANT:** implementation ends in a Guardian restart that drops the
  session. All code/config on `cloud-access-redesign` is committed; pre-restart
  gate PASSED (930 passed / 3 skipped). Operator runs `sudo systemctl restart
  llama-guardian`.

### Pi session `20260813_1` (session wrap-up, last updated 2026-08-13)

- Working directory: `/home/flip/llama_cpp_guardian`
- **Session conclusion (verbeterplan):** Phase 5 structural separation COMPLETE and maintained by other agent sessions (20 commits landed since: MTP/spec-type, Qwen3.5-9B variants, config-drift detection, per-app OpenRouter attribution, client context-hint + n_slots — all in the extracted modules, server.py +15 lines only). Pre-restart gate: all 4 PASS. Suite: 950 passed / 3 skipped.
- **Cleanup done:** committed pending `config/models.yaml` tuning (qwen3.8-27b context 220000, vision params) + `docs/CLIENT_KEY_LINKING.md`; removed untracked secret-bearing `config/cloud_keys.json.bak.*` (superseded by the committed attribution change). Commit `cea91ff`.
- **Keanu side (2026-08-13):** loop completed t0–t4; t4 unblocked in fixture form (real-writer WAL, shared vectors in both repos); new loop work ongoing (parallel distill, keanu-worker). Keanu `main` contains all capture work.
- **Still open (by design, operator decisions):** (1) live capture enablement (privacy decision — `capture.enabled` stays false; the 72 h soak test starts once enabled), (2) CI adoption of `scripts/pre_restart_check.py` as a GitHub Action, (3) first real dataset build on the Keanu side.
- **Known-good recovery:** `gh copilot` routes around Guardian if a restart breaks startup; `unset GH_TOKEN GITHUB_TOKEN GITHUB_PERSONAL_ACCESS_TOKEN` before `git push`.



- Working directory: `/home/flip/llama_cpp_guardian`
- **Keanu cross-repo contract work (2026-08-13, "tijd om te gaan bouwen"):** the Keanu agent (openhands loop) already completed handoff tasks t1 (record_auth HMAC verification, commit `6d6f2cc`), t2 (docs for Decisions 1A/2A, `0fdf51f`), t3 (capture→dataset pipeline with staging/dry-run/synthetic WAL, `3043efb`); t4 (live-WAL contract test) was marked blocked pending a Guardian-side privacy decision. This session closed the gap WITHOUT enabling live capture:
  - `scripts/generate_contract_wal.py` (Guardian): produces a realistic WAL with the REAL `CaptureSink`/`CaptureWALWriter`/event builders (not synthetic)
  - **Shared test vectors pinned in BOTH repos** (`TestSharedCrossRepoVector`): Guardian `tests/unit/test_capture_schema.py` regenerates, Keanu `tests/unit/parsers/test_guardian_capture_parser.py` verifies — any serialisation/HMAC drift breaks the other side
  - Keanu fixture `tests/fixtures/guardian_contract_wal/` + `tests/unit/pipeline/test_capture_contract_fixture.py`: authentic producer artifact flows through `capture_ingest` (2/2 record_auth verified, record accepted; tampered file quarantined)
  - Verified end-to-end manually: real writer → Keanu ingest → ChatML record accepted (3-turn history, ≥12-word replies pass the chatml quality gates: MIN_PAIRS=3, _PLACEHOLDER_MIN_WORDS=12)
  - Guardian suite: 895 passed / 3 skipped. Keanu parsers+pipeline: 832 passed.
- **Keanu handoff delivered:** `docs/KEANU_GUARDIAN_CAPTURE_HANDOFF.md` written into `/home/flip/keanu-factory/docs/` (NOT committed to the Keanu repo yet — the operator decides when). Verdict: Keanu side is NOT fully ready — the parser (2026-08-05) predates Decisions 1A/2A (2026-08-07): `record_auth` HMAC verification is missing, Keanu's SOURCE/PARSER docs don't document it, and the capture→dataset pipeline is not set up. The handoff lists 4 ordered tasks (record_auth verification → docs → pipeline → contract test) with the exact wire format, `compute_record_auth` reference, key_id rotation, and validation checklist.
- **New Critical rule: AGENTS.md must always be updated in the same session as the change** (commit `cf7a879`).
- **Pre-restart gate added:** `scripts/pre_restart_check.py` — py_compile + pyflakes + wrapper-vs-module signature check + pytest in one command. All 4 gates PASS. New Critical rule: run it before every restart.
- **Generic signature regression test** added to `tests/unit/test_server.py` (`test_all_wrapper_calls_match_module_signatures`) — covers every `_module.func(...)` delegation in server.py.
- Fixed the last 2 pyflakes findings: `Union` in `app/capture/redactor.py`, `Dict`/`Any` in `app/proxy/metrics.py` — `pyflakes app/` is now fully clean.
- Full suite: 893 passed, 3 skipped. Commits: `cf7a879`, `235045e`. Pushed.
- **Docs refresh (2026-08-12, commit `8ac206b`+):** all documentation updated for the Phase 5 structure:
  - `AGENTS.md` directory map rewritten (gateway/cloud_inference/local_inference/proxy modules, config_loader, server.py = thin shell 1643 lines)
  - `docs/ANTHROPIC_BRIDGE.md`: enrichment layer now `app/gateway/streaming.py`, cloud bridge `app/cloud_inference/forwarding.py` (was `app/proxy/server.py`)
  - `docs/LLM_ROUTER.md`: `_PROVIDER_BASE_URLS` + `_adapt_openai_reasoning_params` now `app/cloud_inference/routing.py`
  - `docs/API_REFERENCE.md`: added missing endpoint groups — API keys (`/api/keys`), cloud credentials (`/api/cloud/*`, 12 routes, owner-scoped), capture admin (`/api/capture/status`, `/api/capture/rotate`)
  - `docs/GUARDIAN_KEANU_CAPTURE_PLAN.json`: statuses corrected — phases 0–5 complete, phase 6 in_progress (was all `not_started`)
  - `docs/skills/operator-runbook.md`: code-change flow now runs `scripts/pre_restart_check.py` as the mandatory gate
  - Keanu-side docs (`SOURCE/PARSER_GUARDIAN_CAPTURE.md`) are still outdated — covered by `docs/KEANU_GUARDIAN_CAPTURE_HANDOFF.md` (task 2 for the Keanu agent)



- Working directory: `/home/flip/llama_cpp_guardian`
- **New Critical rule: AGENTS.md must always be updated in the same session as the change** (commit `cf7a879`).
- **Pre-restart gate added:** `scripts/pre_restart_check.py` — py_compile + pyflakes + wrapper-vs-module signature check + pytest in one command. All 4 gates PASS on the current tree. New Critical rule: run it before every restart.
- **Generic signature regression test** added to `tests/unit/test_server.py` (`test_all_wrapper_calls_match_module_signatures`) — covers every `_module.func(...)` delegation in server.py (the admin_api-only test caught 11 of 25 handlers; the generic one is the permanent net).
- Fixed the last 2 pyflakes findings (pre-existing lazy string annotations): `Union` in `app/capture/redactor.py`, `Dict`/`Any` in `app/proxy/metrics.py`. `pyflakes app/` is now fully clean.
- Full suite: 893 passed, 3 skipped. Commits: `cf7a879` (AGENTS.md rule), plus this session's gate work. Pushed.



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

### Phase 6 — Operational Hardening ✅ (complete; soak test recommended, not blocking)
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

## GitHub Actions runners (centrale pool van `m0nklabs`)

Dit project gebruikt de **centrale org-level self-hosted runners** van de
`m0nklabs`-organisatie, die OP DEZE SERVER draaien. Er zijn 4 runners
(`m0nklabs-runner-1` t/m `-4`) die voor álle projecten werken. **Voeg nooit een
aparte runner per project toe.**

- Host: `ai-kvm2`
- Labels (automatisch): `self-hosted`, `Linux`, `X64`
- Alle 4 runners zijn **GPU-capabel** (label `gpu`; 2× NVIDIA RTX op de host).
- Bron van waarheid & configuratie: de **publieke** repo
  `m0nklabs/github-action-runners` (zie README.md en AGENTS.md daar).

### Runners gebruiken in workflows

- Gewone job (geen GPU):
  ```yaml
  runs-on: [self-hosted, Linux]
  ```
- GPU-job:
  ```yaml
  runs-on: [self-hosted, Linux, gpu]
  ```

### GPU loopt SERIEEL (1 tegelijk)

Alle runners delen dezelfde GPU's. **Elke** GPU-job moet zijn zware commando's
wrappen met de centrale lock, zodat 2 jobs nooit op dezelfde kaarten concurreren:

```yaml
- name: Train
  run: /home/flip/github-action-runners/bin/gpu-run.sh <command>
```

### Generieke (reusable) workflows

Gebruik in plaats van kopiëren de generieke workflows uit
`m0nklabs/github-action-runners`: `python-ci`, `frontend-ci`, `go-ci`,
`rust-ci`, `gpu-ci`, `codeql-detect`. Roep alleen de workflows voor de talen die
dit project écht bevat.

```yaml
jobs:
  codeql:
    uses: m0nklabs/github-action-runners/.github/workflows/codeql-detect.yml@main
    secrets: inherit
```

### Regels

- **Geen runners per project toevoegen** — gebruik altijd de org-pool.
- **Geen GPU-werk zonder `gpu-run.sh`** — anders concurreren 2 jobs op dezelfde kaarten.
- **Geen secrets committen** in workflows.