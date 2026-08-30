# AGENTS.md — Guardian LLM Provider Gateway

> Canonical AI-agent context for this repo. Read first.
> Claude Code: `CLAUDE.md` → here. Goose: `.goosehints` → here. Copilot: `.github/copilot-instructions.md` references this.

## Stack

- **Language:** Python 3.14 (venv at `./venv`)
- **Web:** FastAPI + uvicorn + httpx
- **Backend:** llama.cpp (`llama-server` on `:11440`, launched via `scripts/start_llama.sh`)
- **Frontend:** React/Vite/Tailwind dashboard on `:11437` (`dashboard/`), bound to `127.0.0.1`
- **Config:** `config/global.settings.yaml` (proxy, providers, queue, timeouts), `config/providers/<naam>.settings.yaml` (één bestand per provider sinds F2; lokale registry/aliases/guardian in `ai-kvm2-local.settings.yaml`), `config/guardian.keys.yaml` (named API keys)
- **Secrets:** `.env` — `${VAR}` expansion in YAML; never commit secrets
- **Deploy:** systemd unit `llama-guardian.service`; nginx exposes the public API on `:11434`.
- **TLS:** nginx stream TLS preread multiplexes both `http://192.168.1.35:11434` and `https://192.168.1.35:11434`. It passes TLS unchanged to Guardian on `127.0.0.1:11435` and routes plain HTTP through nginx on `127.0.0.1:11436`. See `deploy/nginx/guardian-llmprovider-gateway-protocol-mux.conf` and `deploy/nginx/guardian-llmprovider-gateway-loopback-http.conf`.
- **TLS trust:** this host trusts the Guardian certificate through `/usr/local/share/ca-certificates/llama-guardian-192.168.1.35.crt`. Other LAN clients must trust that same certificate before connecting without a custom CA setting.
- **Tests:** pytest (`tests/`, `asyncio_mode=auto`)

## Critical rules

- **Test before claiming fixed:** `./venv/bin/python -m py_compile <file>` then run `./venv/bin/python -m pytest tests/ -x`. Never claim a fix works without verifying.
- **Code needs a restart; config can hot-reload (since 2026-08-19).** `app/*.py` code changes require `sudo systemctl restart llama-guardian` — there is NO hot code reload. But `global.settings.yaml` (providers, failover_groups, capture `cloud_capture`/`cloud_model_prefixes`/policies, failover_health, cloud_retry) now hot-reload WITHOUT restart via `POST /api/config/reload` (admin, any valid key). Port/pid/TLS remain restart-only. Do NOT run the restart for config-only edits anymore; do run it (after the pre-restart gate) for code changes.
- **Run the pre-restart gate before every restart.** `./venv/bin/python scripts/pre_restart_check.py` runs py_compile + pyflakes (undefined names) + the wrapper-vs-module signature check + the full pytest suite. All four gates must pass before `sudo systemctl restart llama-guardian`; any failure means the restart may not come back up (agent traffic routes through Guardian). Added 2026-08-12 after the post-restart audit caught 6 injection/signature bugs the unit suite had missed.
- **The agent routes through Guardian — restarting cuts the agent's own model traffic.** This agent harness (Claude Code / goose / pi) reaches its model *through this very service* (nginx `:11434` → TLS `:11435` → app). A `sudo systemctl restart llama-guardian` therefore silences the current session until startup completes; a code/config error that prevents startup is **not self-healable** — the agent's model is unreachable, so it cannot fix its own mistake. Before any restart: (1) validate with `py_compile` + focused pytest, (2) tell the operator a restart is coming and the session will drop, (3) let the operator run the restart from outside the session, (4) if startup fails, the operator must revert (`git stash`/`git checkout` on `app/`, restore previous `settings.yaml`) — never promise in-session recovery. **Known recovery path (proven 2026-08-12):** the operator enables `gh copilot` (routes around Guardian) and uses it to inspect/repair/restart Guardian while the pi session is down.
- **TLS requires both paths.** `GUARDIAN_TLS_CERTFILE` and `GUARDIAN_TLS_KEYFILE` are an all-or-nothing pair. The production drop-in binds TLS to `127.0.0.1:11435` through `GUARDIAN_TLS_HOST` and `GUARDIAN_TLS_PORT`; nginx's `libnginx-mod-stream` module and a top-level `stream { include /etc/nginx/stream-conf.d/*.conf; }` block are required for the public protocol multiplexer. Keep the private key `0600`.
- **Secrets in `.env`.** API keys use `${ENV_VAR}` expansion. Never inline keys in YAML or Python. Use `scripts/generate_key.py` to mint new Guardian keys.
- **Model resolution is name-based and key-independent.** A model is cloud-hosted when it matches an explicit `models:` entry or a `model_prefixes:` namespace (e.g. `anthropic/`, `nvidia/`). Local models are aliases from `config/providers/ai-kvm2-local.settings.yaml` (the local provider file, F2). Unknown models return `404 model_not_served`. See `@docs/LLM_ROUTER.md`.
- **Cloud access redesign (2026-08-21) governs cloud routing.** Since commits `4329d7c`/`28e97ad` there is NO credential/link/ownership layer, NO `guardian/` prefix, and NO `cloud_keys.json` credential store (removed 2026-08-22). Cloud models are addressed `{provider}/{brand}/{model}` and resolved from each provider's settings API key via the dynamic `CloudModelCatalog` (`app/proxy/cloud_catalog.py`), which fetches `/v1/models`, normalizes to `{brand}/{model}`, and caches with TTL + cold-start disk cache. Per-key cloud access = `cloud_gateway_access: true|false` (default **true**) in `config/guardian.keys.yaml`; a key set `false` gets 403 on cloud routes and sees no cloud entries in `/v1/models`. Failover groups are `failover/{group}` and read `failover_groups:` from `global.settings.yaml`.
- **Per-provider `catalog_url` override (2026-08-21, PR #9).** `CloudProvider` has an optional `catalog_url` (default `/models`); `cloud_catalog.refresh_provider` fetches `base_url + catalog_url`. Use it so a provider advertises only the models genuinely reachable through its guardrails/privacy filters: e.g. openrouter is set to `catalog_url: /models/user`, so Guardian's `/v1/models` shows the 22 really-accessible OpenRouter models instead of all 422 (OpenRouter applies guardrails on inference only, not on the plain `/v1/models` listing — plain listing returns everything). The cold-start disk cache (`data/cloud_catalog_cache.json`) now stores a `source` = `base_url|catalog_url` per provider; a cached entry is dropped when the endpoint changes, so switching `catalog_url` does not keep advertising the old list until a manual `POST /api/cloud/catalog/refresh`. Changing `catalog_url`/`base_url` auto-invalidates the stale cache on the next reload/construction.
- **Config-schema split (2026-08-21, PR #9, `docs/CONFIG_SCHEMA.md`); per-provider files sinds F2 (2026-08-26, `docs/CONFIG_PROVIDER_FILES.md`, PR #7).** `config/settings.yaml` is opgesplitst: `config/global.settings.yaml` (cross-cutting) + één bestand per provider `config/providers/<naam>.settings.yaml` (openrouter, nvidia, google, openai, poolside, groq, ai-kvm2-local) + `config/guardian.keys.yaml`. De oude `providers.*.yaml`/`models.*.yaml` zijn **weg**; per provider zitten keys + `models:`-overrides in het eigen bestand. `app/config_loader.py` leest global en scant `config/providers/`; `ProviderRegistry()` scant ook de directory en houdt de lokale provider als **`managed: true`** entry (keyless maar `is_configured`, nooit cloud-gerouteerd). Legacy-constanten (bv. `PROVIDERS_SETTINGS_FILE`) zijn retained maar ongebruikt. Volledige voltekst → `docs/AGENT_CONTEXT_ARCHIVE.md` §4.
- **Cloud vision fallback is capability-based.** Guardian uses a local vision model only when an image request targets a configured text-only cloud model with an `image_fallback`. Image-capable cloud candidates remain cloud-routed; failover groups filter image requests to image-capable candidates.
- **Model discovery always includes context metadata.** Every `/v1/models` entry and `/api/show` response reports a positive context size. Resolve `context_overrides` first, then `cloud_models.yaml` overrides, then the cloud catalog or local `/props`, and log before using the `131072` fallback.
- **Streaming keepalives required.** All streaming paths (local + cloud) must pass `heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S` (15s default) to `_iter_sse_lines_with_watchdog`. Missing this causes client idle-timeout errors on reasoning models.
- **Don't duplicate docs.** Detailed architecture lives in `docs/`. `AGENTS.md` is the index — reference, don't re-explain.
- **GCD is a pass-through contract, cloud-stripped.** The local OpenAI path forwards `response_format`/`json_schema`/`grammar` (GBNF) byte-identical to llama-server (pinned by `tests/unit/test_grammar_passthrough.py`) — never whitelist body fields. Cloud routes strip GBNF/`json_schema` (providers reject them) and preserve OpenAI-native `response_format`; the `grammar` block in `settings.yaml` (`enabled` kill-switch, `cloud_auto_convert_json`, `cloud_strict_mode`, `validate_gbnf`) controls the optional behavior. Ollama `options.format` maps to `response_format`/`grammar` in the bridge. Capture stores only `grammar_present`/`response_format_present` flags — never the grammar content.
- **No hardcoded vars.** Literals that depend on the deployment (paths, ports, file names, URLs, timeouts) belong in `config/settings.yaml` (`${VAR}`-expandable) or `app/paths.py` (env-var overridable). Never copy a literal into a new module "for convenience" — inject it via `init()` and keep one source of truth. When extracting code, check the moved bodies for literals (`/home/...`, `:11434`, `guardian.pid`, …) and re-route them through config/paths before committing. A hardcoded value in a helper module that bypasses config is a bug, not a shortcut.
- **Commit language:** Dutch is fine for operator-facing notes (internal project); English for code, API, and public docs.
- **Commit-identity = de modelnaam van de agent (Optie B, 2026-08-27).** Commits in deze
  repo worden automatisch gestempeld met het model dat ze maakte: auteur én committer =
  `deepseek-v4-flash-0731 <deepseek-v4-flash-0731@m0nklabs.dev>` (of `glm-5.2` bij een
  tier-2/andere agent). Dit wordt mechanisch afgedwongen via de per-checkout git-config
  (geen PR-Piet meer voor implementatiewerk — PR-Piet blijft alleen de review-persona).
  Werkwijze: commit gewoon met `git commit` (de config regelt het); voor een expliciete
  andere modelnaam per commit: `AGENT_MODEL=glm-5.2 gc ...` (`gc` = de functie in
  `~/.bashrc` die de identity zet vóórdat git commit draait). Implementatie + terugdraaien:
  `pr-piet/bin/install-dynamic-commit-identity.sh` en `pr-piet/bin/git-agent-identity.sh`.
  Overschrijf de identity niet zelf naar `PR-Piet` — dat reserveren we voor de reviewer.
- **AGENTS.md is always updated — including fresh findings.** Every behavior change, bug fix, extraction, config change, lesson learned, AND any repository fact that had to be dug up or reverse-engineered goes into AGENTS.md (progress lists, handoff section, Critical rules) in the SAME working session — before the final commit/push, not after. If you finished a task and AGENTS.md does not reflect it, the task is not done. The handoff section is the primary continuity mechanism between agent sessions; a stale handoff is a bug. **Rule of thumb:** if you had to inspect code, config, or docs to learn *how something actually works*, write that understanding into AGENTS.md right then — otherwise the next session re-derives it from scratch. That is known investigation work, and it belongs in the file the moment you've confirmed it.
- **Maximize subagent usage with fresh context.** Delegate as much work as possible to subagents started with `context: "fresh"` (or default fresh-context workers). The lead session keeps the plan and stays in control; children do the mechanical/implementation/measurement work without inheriting the lead's accumulated chat history. Workflow: (1) the lead reads just enough to write a precise task (exact file paths, expected shape, the reference data to copy), (2) spawns a fresh-context worker for the implementation + verification, (3) synthesizes the child's output and applies any cross-file follow-up. Keep **one writer per cwd/worktree** — concurrent workers must not touch the same files; serialize edits to shared files (e.g. `config/models.yaml`) or have the lead own them. Subagent constraints on this host: model `guardian/openrouter/deepseek/deepseek-v4-flash-0731:high` (routes via Guardian, NOT bare `openrouter/...` — the direct OpenRouter key is disabled), max **3 simultaneous** (Guardian 429s above that; Novita upstream rate-limits). For read-only review use fresh-context reviewer children, then have the lead synthesize + apply fixes. Read `@~/.pi/agent/npm/node_modules/pi-subagents/skills/pi-subagents/SKILL.md` for the runs.run/runs.all API. **Operator-voorkeur (2026-08-26): gebruik worker subagents als standaard** — de lead blijft dan licht en direct aanspreekbaar voor de operator (geen lange eigen tool-runs). In DSH: `subagent`-tool draait standaard op de achtergrond (resultaat komt terug als melding; lead kan doorgaan met andere stappen en achteraf `send_message` sturen voor vervolg); alleen als de volgende stap ervan afhangt, `run_in_background: false`. Analyse- en classificatiewerk = background worker; implementatie- of meetwerk = background worker; de lead schrijft alleen zelf als het één klein, snel ding is of als meerdere workers hetzelfde bestand zouden raken.
- **DSH/MCP informatie-vergaring (live getest 2026-08-30):** de native `web_search` van de harness is kapot in dit milieu (`"web provider deepseek-official is not registered"`) — de MCP-suites zijn de enige werkende web-toegang. **Werkend en goed:** `local-search web_search` (SearXNG, beste relevantie), `web_search_google` (Edge-relay, beste ranking), `fetch_page`/`fetch_meta`/`fetch_relay`/`site_headers`, Playwright, `legal-reference`, en de GitHub-suite (`get_latest_release` e.d. = betrouwbaarste bron voor actuele feiten). **Knelpunten:** `mcp github get_file_contents` geeft `"[resource: content discarded]"` (DSH-bridge gooit de body weg) → altijd `github_file_read` gebruiken; `search_code` kan 0 hits geven met `incomplete_results: true` (index-achterstand — cross-check vóór conclusies); local-search `map_site` (502), `fetch_sitemap` (stil leeg) en `fetch_rss` (vindt feed, 0 items) zijn onbetrouwbaar; kindly-web-search heeft zwakke relevantie (vond "Llama" het dier i.p.v. llama.cpp-releases). **Vuistregel:** actuele versies eerst via GitHub API, dan `web_search_google`/`web_search` + `fetch_page`, `fetch_relay` als bot-wall-fallback.
- **Dashboard UI auth — empty shell ≠ code bug.** Since commit `7472d61` (2026-07-30, auth on dashboard `:11437`, bind to 127.0.0.1 only) every `/api/*` on the dashboard requires a Bearer key, but the UI itself sent no auth header → the dashboard was functionally dead (empty shell) even locally, `curl 127.0.0.1:11437/api/stats` → 401. Fixed 2026-08-15: a fetch-wrapper (monkey-patch `window.fetch`) + key-input modal in `app/ui/index.html` store the key in localStorage (`guardian_dashboard_api_key`) and inject `Authorization: Bearer <key>` on every `/api/*` call; on 401 the key is cleared and the modal reopens. A dashboard that shows an empty shell is a missing key in the browser, not a code bug — check localStorage first before touching the code.
- **PR-afhandeling op guardian — `/review`-commando (werkwijze 2026-08-26/27).** PR's worden via de PR-Piet-reviewer behandeld; na elke laatste commit vóór merge post de agent altijd `/review` (`gh pr comment <n> --body "/review"`) zodat een verse review op de nieuwe head komt. Merge-criterium (operator): pas mergen als er GEEN openstaande review-bevindingen zijn (met bewijs weerlegd én beantwoord telt niet als openstaand); altijd human merge, geen auto-approve/auto-merge. Bekende haken: concurrency-cancel telt als fail-check (`gh run rerun` fix), en de diepe `/review` kan op grote diffs 30-min timen → dan de auto-`pull_request`-run herrunnen. Volledige werkwijze + details → sectie `GitHub / Git / PR / Reviewers` en `docs/AGENT_CONTEXT_ARCHIVE.md` §3.1.
- **PR-Piet-bevindingen zijn speculatief — verifieer vóór je ze volgt (én check of ze terecht zijn).** PR-Piet's bevindingen kunnen terecht óf onterecht zijn; verifieer altijd met ≥ 2 onafhankelijke bewijzen (gedragstest + code-lezing, statische check + suite, API-feit + diff, call-site + test). Cases 1–4 (weerlegd / terecht, incl. de als-semantische lek-lessen over `is_configured`/`get_enabled_providers` en de control-flow-walrus-fix) → `docs/AGENT_CONTEXT_ARCHIVE.md` §3.2. De werkwijze rond PR-review-output staat in `GitHub / Git / PR / Reviewers`.

## Directory map

```
app/
├─ main.py              # uvicorn entrypoint
├─ paths.py             # central path resolution (REPO_ROOT, CONFIG_DIR, MODELS_DIR, …)
├─ config_loader.py     # settings.yaml parsing — loaded ONCE per process, typed accessors
├─ proxy/server.py      # thin shell: routes + init() wiring
├─ proxy/auth.py        # API key verification
├─ proxy/providers.py   # ProviderRegistry: cloud model recognition (exact + prefix)
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
│  ├─ context_metadata.py #  context window resolution + model metadata
│  ├─ caretaker_client.py #  caretaker-daemon client: idle-unload-watcher + /admin/unload (lokale fallback) — PR #11
│  └─ caretaker_runtime.py # ensure_backend remote-first (tranche 2 in review)
├─ cloud_inference/      # Phase 5 extracted: routing.py (attempts/fallback/capture setup),
│                        #   forwarding.py (forward_to_cloud_provider, 28 deps)
├─ local_inference/      # Phase 5 extracted: ollama.py (chat/generate bridges),
│                        #   models.py (resolution, sizes, timeouts, VRAM scheduler, reload),
│                        #   model_registry.py (ModelRegistry: choice/discovery-logica, F4/PR #10)
├─ engine/manager.py     # llama-server lifecycle (start/stop/reload)
├─ scheduler/manager.py  # Idle-unload + auto-switch scheduler
├─ tweaker/              # Finetune v2: context/ngl/tensor_split tuning
└─ capture/             # Privacy-aware capture subsystem (config, policy, redactor, schema, sink, WAL writer)
config/
├─ global.settings.yaml   # proxy (port/target/pid_file/vram), queue, timeout tiers, capture, etc.
├─ providers/             # EÉN bestand per provider (F2): ai-kvm2-local, openrouter, nvidia, google, openai, poolside, groq
│  ├─ ai-kvm2-local.settings.yaml   # lokale registry (models/aliases/guardian) + base_url/local
│  └─ openrouter.settings.yaml      # base_url/api_key/prefixes + catalog_url + models:-overrides
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
- **Guardian 2.0 masterplan (PLAN 2026-08-26, goedgekeurd)** → `@docs/IMPLEMENTATION_PLAN.md` (canoniek) + GitHub issue #1. Fases: F0 foundation + F1 file register → **gebouwd (PR #2)**; F2 → **gemerged (PR #7)**; F3 → **gemerged (PR #8)**; F4 → **gemerged (PR #10)**; F5 caretaker + local passief → **in uitvoering** (caretaker fases A–D + PR #6 gemerged in `m0nklabs/caretaker-llamacpp`, gateway-wiring tranche 1 PR #11 gemerged, tranche 2 `caretaker_runtime.py` merge-klaar — PR #12, wacht op human merge); F6 Windows/14700K + F7 cut-over → **open**.
  - Deelplannen + voltekst (LAN_GPU_BACKENDS / GATEWAY_MANAGER_SPLIT / CONFIG_PROVIDER_FILES) → `docs/AGENT_CONTEXT_ARCHIVE.md` §2; F5-detail → `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`; **actuele status: zie `### Open punten`.**

## References

- Cloud rate limiting: `@docs/skills/operator-runbook.md`
- Client list / keys: `config/guardian.keys.yaml` (named keys for goose, oelala, hydroponics, etc.)

## Maintenance

- This file is the source of truth. `CLAUDE.md` and `.goosehints` are relative symlinks to this file.
- Every behavior change goes here first; symlinks follow automatically.
- If this repo gains Windows CI, run `scripts/sync-agent-docs.sh` instead of symlinks.

## Active Handoff

> Afgeronde/gesloten DSH-sessies worden gearchiveerd in **`docs/ARCHIVED_HANDOFFS.md`**
> (volledige tekst blijft bewaard; de Active Handoff hieronder houdt alleen sessies
> met lopende relevantie). Gearchiveerd op 2026-08-26 (2 batches):
> **Batch 1:** `20260826_muse_catalog`, `20260826_raw_capture`, `20260824_reasoning_effort`.
> **Batch 2 (alles afgerond):** `20260822_cleanup`, `20260822_nvidia_free_filter`,
> `20260824_cloud_audit`, `20260821_config_schema`, `20260815_bench`, `20260815_gcd`,
> `20260815_1`, `20260816_1`, `20260816_2`, `20260819_1`, `20260820_1`,
> `20260820_cloud_refactor`, `20260813_1`, `20260812_1`, `20260809_5`.
> Alleen `20260826_rename` blijft actief (huidige workspace-transitie) — de écht
> openstaande zaken staan hieronder in **Open punten**.

### DSH session `20260826_rename` (repo-split: legacy + nieuwe bouwplaats)

- **DRIE repos, definitief:** `m0nklabs/llama-cpp-guardian` = LEGACY/archief; `m0nklabs/guardian-llmprovider-gateway` = NIEUW (bouwplaats, volle git-history); `m0nklabs/caretaker-llamacpp` = publieke per-GPU-host-manager-repo.
- **Productie draait nog uit `/home/flip/llama_cpp_guardian`** (systemd + venv) tot F7.
- **In de nieuwe dir GEEN restarts/deploys** — productie draait uit de oude dir.
- **pytest/gate via het OUDE venv:** `/home/flip/llama_cpp_guardian/venv/bin/python -m pytest tests/`.
- **Secrets/gitignored bestanden (`.env`, keys) niet committen.**
- **Klaargezet (deze sessie):** `venv`-symlink → legacy-venv + gekopieerde `.env`/`config/guardian.keys.yaml`/`data/`.
- **Overige details** (volledige sessiekopie-methode naar de nieuwe workspace, docs-referentie-fix `5e02c78`) → `docs/AGENT_CONTEXT_ARCHIVE.md` §5.

### Open punten (actueel — alles wat hier niet staat is afgerond; details in `docs/ARCHIVED_HANDOFFS.md`)

> Restructurering 2026-08-30 — AGENTS.md geslimd 57.979 → ~32 kB (25 kB-doel niet haalbaar zonder Critical rules te schenden; vloer ~26–27 kB), verplaatste voltekst → `docs/AGENT_CONTEXT_ARCHIVE.md`.

- **Bekende test-fout (pre-existing, bewust gedeferred):** `test_cloud_attempts_resolve_google_full_address` faalt al weken op ongewijzigde HEAD (google cold-start-assertie, niet gerelateerd aan enig werk) — gemeld in meerdere gearchiveerde handoffs, niet aangeraakt.
- **NVIDIA context-metadata gap (FUTURE work):** NVIDIA-modellen krijgen de 131072-fallback; per-model `context_window`-overrides voor actief gebruikte modellen zijn nog niet ingevuld (operator: "first find what's usable, THEN max context").
- **Heads-up pi-modellen:** `~/.pi/agent/models.json` bevat nog bare-name cloudnamen + legacy `guardian/...`-entries → 404-risico als pi die ooit bare uitzendt (actieve modellen gebruiken full addresses, dus latent).
- **CI-adoptie (open sinds 20260813_1):** `scripts/pre_restart_check.py` als GitHub Action is nog niet opgepakt.
- **m0nkdash-origin (optioneel):** origineel achter `dashboard.oelala.xyz` (m0nkdash via `serve.sh`) blijft dood — raakt Guardian niet.
- **F0+F1 gebouwd (PR #2, 2026-08-26, wacht op review/merge):** rename-sweep (`llama-guardian`/`LLAMA_CPP_GUARDIAN_*` → `guardian-llmprovider-gateway`/`GUARDIAN_LLMPROVIDER_GATEWAY_*` in deploy/scripts/docs/env + 5 git mv's incl. nginx/systemd/workspace), CHANGELOG-entry, CI-wiring (`python-ci.yml` + `ruff-autofix.yml` → nieuwe org-reusable `python-autofix.yml`; `AUTOFIX_PAT`-secret nodig want GITHUB_TOKEN-pushes triggeren geen workflow-runs), `docs/FILE_REGISTER.md` (draft, alle 194 tracked files). PR-Piet review gaf 2 bevindingen: `_paths.py`-re-export (F401 door autofix verwijderd → teruggezet met `# noqa: F401`) en env-var-backcompat — die is op operator-besluit **omgedraaid**: geen fallback, `LLAMA_CPP_GUARDIAN_*` raised nu `RuntimeError` bij import (`tests/unit/test_legacy_env_rejected.py` pinnt dit). Er is nog een `test/pr-piet-clean2`-branch op origin (PR-Piet-experiment, niet van ons).
- **F2 gebouwd (PR #7, 2026-08-26, branch `f2-provider-config`, CI-groen, klaar voor merge):** per-provider config-bestanden `config/providers/*.settings.yaml` (7: ai-kvm2-local, openrouter, nvidia, google, openai, poolside, groq); de 4 oude provider/modelfiles zijn verwijderd; `app/paths.py`/`config_loader.py`/`providers.py`/`cloud_catalog.py` scannen nu de directory (lokale provider uitgesloten uit de cloud-registry); `local_models_file()` → `ai-kvm2-local.settings.yaml`. Länder: LAN_GPU_BACKENDS + GATEWAY_MANAGER_SPLIT (de échte volgende stappen) staan in Skills → `@docs/LAN_GPU_BACKENDS.md`, `@docs/GATEWAY_MANAGER_SPLIT.md`; `m0nklabs/caretaker-llamacpp` heeft nu `PLAN.md` (gefaseerd plan, fases A–E) maar nog **geen code**.
- **F4 GEMERGED (PR #10, 2026-08-28, squash-commit `b734d3c`):** registry/keuze/discovery ontdraaid uit `engine/manager.py` naar `app/local_inference/model_registry.py` (nieuw, `ModelRegistry`); `ModelManager` componeert hem met dunne delegatoren; module-globals + externe imports intact. **Acceptatie op unit-bewijs (operator-besluit 2026-08-28, Optie A):** `tests/unit/` → 992 passed op de refactor én op originele code (apples-to-apples); de volledige suite haalt niet de beoogde "1009 passed / 3 skipped" omdat `tests/integration/test_live_inference.py` hangt op het draaiende proxy (waar de agent zelf doorheen routeert) — bewezen als milieufactor (hangt identiek op originele code), geen F4-regressie. Merge-criterium: /review zonder openstaande bevindingen; bereikt (3 threads resolved, 6 beantwoord/weerlegd met bewijs).
- **F5 gateway-wiring tranche 2 — IN REVIEW (PR #12, branch `f5-tranche2-hotpath`, 2026-08-30, merge-klaar zodra review 0 open threads op de laatste head geeft):** `app/gateway/caretaker_runtime.py` (`ensure_backend()`: remote-first `/ensure` + lokale fallback) + auto-reload/auto-switch/connect-error herstel remote-first in `routing.py`/`ollama.py`/`models.py` + `manager.mark_loaded_by_caretaker()`/`save_current_context()` spiegel. Volle suite 1134 passed. **Werking waar F6/F7 op vertrouwen:** (a) exception-taxonomie `/ensure`: `status_code` set = daemon alive + reject → fail-closed; Read/WriteTimeout + connection-established-set (ReadError/WriteError/RemoteProtocolError/PoolTimeout) = alive → fail-closed; alleen hard `ConnectError` (≠ ConnectTimeout) = refused; ConnectTimeout = DROP → fail-closed bij levende backend, lokale lifecycle bij backend down. (b) Adoptie-poll `_await_backend_serving` (120 s: wall-clock-deadline + iteratie-cap) na 2× timeout — drift-guard alleen bij `current_model == model` én géén parameter-delta: de persisted launch-sig is per constructie stale tijdens een in-flight switch (`mark_loaded_by_caretaker` herschrijft pas ná adoptie; r30–r32-lessen). (c) Re-bind poll 15×1 s bij hard-refused: draait bij backend-healthy ÓF (backend-down + `_ever_reached_caretaker` — KillMode=control-group doodt llama-server mee met de daemon), skipt bij nooit-geobserveerde daemon (roll-out; anders ~16 s lock-hold per switch). (d) `_ever_reached_caretaker` = proces-lifetime flag, gezet op ELKE daemon-response (succes óf status_code-rejectie) via `_remote_ensure()`; alleen pure transport-fouten laten hem ongerust. (e) fresh_load-restore-gate gedropt (r29): de pre-save is onvoorwaardelijk sinds r25, dus de response-level `fresh_load` is de enige restore-gate. **Review-lus-les (34 commits, ~33 rondes):** elke fix-push triggert een verse full review zonder geheugen → de opbrengst per ronde daalt en de code begint te oscilleren (r26 bailout → r28 terugdraai → r29 zelfde voorstel opnieuw); werkwijze voortaan: 1× `/review` per voltooide fix-batch, speculatieve "Possible, not verified"-bevindingen batchen als backlog tenzij blocking, en de WORTEL-fix van de gateway-heuristiek is contract-verrijking in de caretaker-daemon (`loaded_model`/`fresh_load`/`vision_enabled` betrouwbaar in het /ensure-ANTWOORD — aparte PR na merge; ook poll-vensters `_ADOPT_POLL_SECONDS`/re-bind naar config, hot-reload). Voltekst beslisboom → `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`.

## Capture Implementation Status

- **Phases 0–6 compleet** (laatste status 2026-08-07): capture-subsysteem (config/policy/redactor/sink/WAL), Keanu-integratie, protocol/route-coverage, Phase-5-structurele-separatie (server.py → thin shell −68%), Phase-6 operational hardening (guardianctl, rotate, HMAC, crash-recovery, dashboard).
- **72-hour soak test nog open** (niet blokkerend).
- Volledige voltekst per Phase (0–6) + **Resolved Decisions (2026-08-07)** → `docs/AGENT_CONTEXT_ARCHIVE.md` §1.

## GitHub Actions runners (org-pool m0nklabs)

- Org-level pool van 4 self-hosted runners (`m0nklabs-runner-1..4`) op host `ai-kvm2`, alle **GPU-capabel** (label `gpu`); **nooit een aparte runner per project**.
- GPU-job: `runs-on: [self-hosted, Linux, gpu]` en zware stappen wrappen met `/home/flip/github-action-runners/bin/gpu-run.sh` — **GPU loopt serieel** (centrale flock-lock, nooit 2 jobs op dezelfde kaarten).
- Gebruik de generieke reusable workflows uit de publieke repo `m0nklabs/github-action-runners` (`python-ci`, `frontend-ci`, `go-ci`, `rust-ci`, `gpu-ci`, `codeql-detect`, …) i.p.v. kopiëren — roep alleen de workflows voor talen die dit project écht bevat.
- **Geen secrets committen** in workflows.
- Bron van waarheid & configuratie: de publieke repo `m0nklabs/github-action-runners` (README + AGENTS.md daar); volledige voltekst van de oude sectie → `docs/AGENT_CONTEXT_ARCHIVE.md` §6.

## GitHub / Git / PR / Reviewers

> Review-werkstroom op guardian: wie de PR-reviewer is, hoe je review-output afhandelt. Kernafspraken in Critical rules; hieronder de bedieningshandleiding.

### Identity van de PR-reviewer (de "PR-Piet"-persona)

Geen menselijke reviewer; één automatisch mechanisme met drie namen:
1. **pr-agent fork** — `m0nklabs/pr-piet` (gepinde org-fork van [`the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent));
2. **GitHub Action workflow** — `.github/workflows/pr-piet.yml` (triggert op `pull_request` + `issue_comment`, roept org-reusable `m0nklabs/pr-piet/.github/workflows/reusable-pr-piet.yml@main` aan);
3. **PR reviewer** — alle `m0nklabs`-repos (Copilot-stijl, tier-1 `openai/deepseek/deepseek-v4-flash-0731` + optionele tier-2).

Kortom: **pr-agent fork = PR-Piet = workflow = review.** Slash-commando's (`/review`, `/describe`, `/improve`) via dezelfde `issue_comment`-trigger.

### Review aanvragen + merge-criterium (kort; uitgebreid in Critical rules)

- Post `/review` van een **mens-account** (`gh pr comment <n> --body "/review"`); bot-senders worden overgeslagen. Post het na **elke laatste commit vóór merge** (review draagt `head=<sha>`; na nieuwe pushes opnieuw `/review`).
- **Merge-criterium (operator):** pas mergen als er GEEN openstaande bevindingen/threads zijn (met bewijs weerlegd + beantwoord telt niet als openstaand). **Altijd human merge.**

### Review-output afhandelen (approven bespaart rondes)

PR-Piet presenteert bevindingen soms als **GitHub suggested code changes** — een suggestie die klopt direct toepassen:
- **UI:** "Files changed" → "Commit suggestion" / "Apply suggestion batch".
- **API/CLI:** suggested-change diffs committen via de GitHub REST API of de diff direct op de branch pushen.
- **Batchen:** zoveel mogelijk suggesties in één keer committen (elke push telt mee voor merge-criterium en `/review`-cyclus).

### Verificatie-discipline (altijd méér dan één bewijs)

PR-Piet-bevindingen zijn **speculatief** — terecht óf onterecht (zie Cases 1–4 in `docs/AGENT_CONTEXT_ARCHIVE.md` §3.2). Verifieer vóór je volgt of weerlegt, met ≥ 2 onafhankelijke bewijzen:
- **Gedragstest** (live `curl`/pytest die de verkeerde-tak-executie pinnt) **+** **code-lezing** van de control-flow (if/elif-chain, guards).
- **Statische check** (`py_compile`, ruff F-selectie, imports) **+** **geautomatiseerde suite** (unit-tests) op refactor én originele code (apples-to-apples via `git stash`).
- **API-feit** (bv. code-scanning alerts) **+** **PR-thread**/diff-bevestiging.
- **Call-site-analyse** (wie roept een functie aan) **+** **test die het openbare contract pint**.

Pas de gepaste fix toe óf weerleg met bewijs + antwoord op de thread — laat een bevinding nooit ongeadresseerd "open" hangen.

### Operationele details (de bekende haken)

- **Concurrency-cancel = gerade fail-check.** `/review` draait op main, de `pull_request`-trigger op de feature-branch; een gecancelde auto-`pull_request`-run telt als "fail"-check (`merge-state: UNSTABLE`). **Oplossing:** `gh run rerun <run-id> --failed` → CLEAN.
- **Deep-review op grote diffs.** De diepe `/review` (issue_comment) heeft 30-min job-timeout; op grote diffs kan die timen ("The operation was canceled", geen review). De `pull_request`-auto-review draait snel (~1 min) en post dezelfde template — **herrun die** als de diepe review getimed is.
