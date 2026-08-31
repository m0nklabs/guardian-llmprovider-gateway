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
- **Deploy:** systemd unit `guardian-llmprovider-gateway.service` (alias `llama-guardian.service`), productie-checkout `/home/flip/guardian-llmprovider-gateway` — de legacy-dir `/home/flip/llama_cpp_guardian` is frozen (archief). Nginx exposes the public API on `:11434`.
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
- **Findings worden same-session vastgelegd — in de cold files, niet in AGENTS.md zelf (2026-08-30).** Elke behavior change/les/opgegraven repository-fact gaat dezelfde sessie nog naar `docs/HANDOFF.md` (actuele status/open punten) of `docs/AGENT_JOURNAL.md` (append-only findings) — vóór de final commit/push. AGENTS.md zelf verandert alleen in **gebatchte promotie-passes** (journal-feiten → stabiele regels of docs-pointers; verouderde handoff → `docs/ARCHIVED_HANDOFFS.md`): elke byte-verandering hier breekt de prompt-cache, dus churn hoort in de cold files. Volatile hot-file-content zo ver mogelijk achteraan in het bestand. Werkwijze → `~/.dsh/AGENTS.md` ("AGENTS.md maintenance discipline"). Een stale handoff/journal is nog steeds een bug.
- **Maximize subagent usage with fresh context.** Delegate as much work as possible to subagents started with `context: "fresh"` (or default fresh-context workers). The lead session keeps the plan and stays in control; children do the mechanical/implementation/measurement work without inheriting the lead's accumulated chat history. Workflow: (1) the lead reads just enough to write a precise task (exact file paths, expected shape, the reference data to copy), (2) spawns a fresh-context worker for the implementation + verification, (3) synthesizes the child's output and applies any cross-file follow-up. Keep **one writer per cwd/worktree** — concurrent workers must not touch the same files; serialize edits to shared files (e.g. `config/models.yaml`) or have the lead own them. Subagent constraints on this host: model `guardian/openrouter/deepseek/deepseek-v4-flash-0731:high` (routes via Guardian, NOT bare `openrouter/...` — the direct OpenRouter key is disabled), max **3 simultaneous** (Guardian 429s above that; Novita upstream rate-limits). For read-only review use fresh-context reviewer children, then have the lead synthesize + apply fixes. Read `@~/.pi/agent/npm/node_modules/pi-subagents/skills/pi-subagents/SKILL.md` for the runs.run/runs.all API. **Operator-voorkeur (2026-08-26): gebruik worker subagents als standaard** — de lead blijft dan licht en direct aanspreekbaar voor de operator (geen lange eigen tool-runs). In DSH: `subagent`-tool draait standaard op de achtergrond (resultaat komt terug als melding; lead kan doorgaan met andere stappen en achteraf `send_message` sturen voor vervolg); alleen als de volgende stap ervan afhangt, `run_in_background: false`. Analyse- en classificatiewerk = background worker; implementatie- of meetwerk = background worker; de lead schrijft alleen zelf als het één klein, snel ding is of als meerdere workers hetzelfde bestand zouden raken.
- **DSH/MCP informatie-vergaring (live getest 2026-08-30):** de native `web_search` van de harness is sinds 2026-08-30 (middag) **weer ingeschakeld en werkt, maar is BETAALD** — operator heeft DEEPSEEK_API_KEY geconfigureerd (€2 budget); provider + `dsh-tool-web` staan weer aan (cordis.patch.yml) en een live-test gaf de correcte v0.3.0-releasepagina als #1. Kosten: ~10.6k in + ~0.8k out tokens per call (flash-tarief ≈ enkele tienden cent) → **€2 ≈ enkele honderden calls**; daarom staat hij als **#4 in de ranking: gratis engines eerst** (`local-search web_search_google` → `web_search`), pas gebruiken als die niets bruikbaars opleveren of de Google-bridge down is, `num_results` ≤ 3. De `~/.dsh/plugins/search-tool-ranking.js`-plugin injecteert de ranking in elke sessie. **Werkend en goed (gratis):** `local-search web_search` (SearXNG, beste gratis relevantie), `web_search_google` (Edge-relay, beste ranking), `fetch_page`/`fetch_meta`/`fetch_relay`/`site_headers`, Playwright, `legal-reference`, en de GitHub-suite (`get_latest_release` e.d. = betrouwbaarste bron voor actuele feiten). **Knelpunten:** `mcp github get_file_contents` geeft `"[resource: content discarded]"` (bridge-patch staat klaar in `dsh-mcp-client/lib/index.js`, actief na DSH-herstart) → tot die tijd altijd `github_file_read`; `search_code` kan 0 hits geven met `incomplete_results: true` (GitHub-index-achterstand — cross-check vóór conclusies, niet te fixen); kindly-web-search heeft zwakke relevantie (vond "Llama" het dier i.p.v. llama.cpp-releases). De local-search gateway-defects zijn **gefixt** (2026-08-30, `/home/flip/local-search` `gateway/app.py`, live geverifieerd): `fetch_rss` parseert Atom, `fetch_sitemap` geeft expliciete 404, `map_site` doet echte root-linkdiscovery, `fetch_meta` capt json_ld op 8 KB. **Vuistregel:** actuele versies eerst via GitHub API, dan `web_search_google`/`web_search` + `fetch_page`, `fetch_relay` als bot-wall-fallback, native `web_search` als betaalde fallback.
- **Dashboard UI auth — empty shell ≠ code bug.** Since commit `7472d61` (2026-07-30, auth on dashboard `:11437`, bind to 127.0.0.1 only) every `/api/*` on the dashboard requires a Bearer key, but the UI itself sent no auth header → the dashboard was functionally dead (empty shell) even locally, `curl 127.0.0.1:11437/api/stats` → 401. Fixed 2026-08-15: a fetch-wrapper (monkey-patch `window.fetch`) + key-input modal in `app/ui/index.html` store the key in localStorage (`guardian_dashboard_api_key`) and inject `Authorization: Bearer <key>` on every `/api/*` call; on 401 the key is cleared and the modal reopens. A dashboard that shows an empty shell is a missing key in the browser, not a code bug — check localStorage first before touching the code.
- **PR-afhandeling op guardian — `/review`-commando (werkwijze 2026-08-26/27).** PR's worden via de PR-Piet-reviewer behandeld; na elke laatste commit vóór merge post de agent altijd `/review` (`gh pr comment <n> --body "/review"`) zodat een verse review op de nieuwe head komt. Merge-criterium: pas mergen als er GEEN openstaande review-bevindingen zijn (met bewijs weerlegd én beantwoord telt niet als openstaand), de laatste review op de merge-head zit en CI groen is. Human merge is de standaard; de agent mag zelfstandig mergen zodra dit criterium volledig is behaald én een slot-comment op de PR het beaamt — meldt een nieuwe review-bevinding: eerst afhandelen, niet mergen. Geen auto-approve (nep-reviews blijven verboden). Bekende haken: concurrency-cancel telt als fail-check (`gh run rerun` fix), en de diepe `/review` kan op grote diffs 30-min timen → dan de auto-`pull_request`-run herrunnen. Volledige werkwijze + details → sectie `GitHub / Git / PR / Reviewers` en `docs/AGENT_CONTEXT_ARCHIVE.md` §3.1.
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
  - Deelplannen + voltekst (LAN_GPU_BACKENDS / GATEWAY_MANAGER_SPLIT / CONFIG_PROVIDER_FILES) → `docs/AGENT_CONTEXT_ARCHIVE.md` §2; F5-detail → `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`; **actuele status: zie `@docs/HANDOFF.md`.**

## References

- Cloud rate limiting: `@docs/skills/operator-runbook.md`
- Client list / keys: `config/guardian.keys.yaml` (named keys for goose, oelala, hydroponics, etc.)

## Maintenance

- This file is the source of truth. `CLAUDE.md` and `.goosehints` are relative symlinks to this file.
- Every behavior change goes here first; symlinks follow automatically.
- If this repo gains Windows CI, run `scripts/sync-agent-docs.sh` instead of symlinks.

## Current state & handoff (cold files — READ FIRST)

> De actuele status staat NIET in dit bestand (prompt-cache-stabiliteit):
> - **`@docs/HANDOFF.md`** — actuele status, Open punten, sessie-handoff → lees dit eerst.
> - **`@docs/AGENT_JOURNAL.md`** — append-only findings-log (zelfde sessie appen).
>
> Dit bestand verandert alleen in gebatchte promotie-passes (werkwijze:
> `~/.dsh/AGENTS.md` → "AGENTS.md maintenance discipline"). Afgeronde sessies
> → `docs/ARCHIVED_HANDOFFS.md`.

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
- **Merge-criterium:** pas mergen als er GEEN openstaande bevindingen/threads zijn (met bewijs weerlegd + beantwoord telt niet als openstaand), de laatste review op de merge-head zit en CI groen is. **Human merge is de standaard; de agent mag zelfstandig mergen** zodra dit criterium volledig is behaald én een slot-comment op de PR het beaamt — een nieuwe review-bevinding betekent eerst afhandelen, niet mergen. Geen auto-approve.
- **Slot-comment bij merge-gereed (operator, 2026-08-30):** een PR die als merge-gereed wordt aangemerkt krijgt minstens één slot-comment op de PR dat aangeeft dat de laatste review bewijst dat de issues/bevindingen met overwegingen zijn beoordeeld (resolved óf met bewijs beantwoord/weerlegd).

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
