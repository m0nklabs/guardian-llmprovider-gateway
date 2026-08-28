# AGENTS.md — Llama-CPP Guardian

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
- **Model resolution is name-based and key-independent.** A model is cloud-hosted when it matches an explicit `models:` entry or a `model_prefixes:` namespace (e.g. `anthropic/`, `nvidia/`). Local models are aliases from `config/providers/ai-kvm2-local.settings.yaml` (the local provider file, F2). Unknown models return `404 model_not_served`. See `@docs/LLM_ROUTER.md`.
- **Cloud access redesign (2026-08-21) governs cloud routing.** Since commits `4329d7c`/`28e97ad` there is NO credential/link/ownership layer, NO `guardian/` prefix, and NO `cloud_keys.json` credential store (removed 2026-08-22). Cloud models are addressed `{provider}/{brand}/{model}` and resolved from each provider's settings API key via the dynamic `CloudModelCatalog` (`app/proxy/cloud_catalog.py`), which fetches `/v1/models`, normalizes to `{brand}/{model}`, and caches with TTL + cold-start disk cache. Per-key cloud access = `cloud_gateway_access: true|false` (default **true**) in `config/guardian.keys.yaml`; a key set `false` gets 403 on cloud routes and sees no cloud entries in `/v1/models`. Failover groups are `failover/{group}` and read `failover_groups:` from `global.settings.yaml`.
- **Per-provider `catalog_url` override (2026-08-21, PR #9).** `CloudProvider` has an optional `catalog_url` (default `/models`); `cloud_catalog.refresh_provider` fetches `base_url + catalog_url`. Use it so a provider advertises only the models genuinely reachable through its guardrails/privacy filters: e.g. openrouter is set to `catalog_url: /models/user`, so Guardian's `/v1/models` shows the 22 really-accessible OpenRouter models instead of all 422 (OpenRouter applies guardrails on inference only, not on the plain `/v1/models` listing — plain listing returns everything). The cold-start disk cache (`data/cloud_catalog_cache.json`) now stores a `source` = `base_url|catalog_url` per provider; a cached entry is dropped when the endpoint changes, so switching `catalog_url` does not keep advertising the old list until a manual `POST /api/cloud/catalog/refresh`. Changing `catalog_url`/`base_url` auto-invalidates the stale cache on the next reload/construction.
- **Config-schema split (2026-08-21, PR #9, `docs/CONFIG_SCHEMA.md`); per-provider files since F2 (2026-08-26, `docs/CONFIG_PROVIDER_FILES.md`, PR #7).** `config/settings.yaml` is split into domain files: `config/global.settings.yaml` (proxy/queue/timeouts/scaler/capture/grammar/cloud_retry/failover_health/services/services_to_stop/benchmark), a per-provider file `config/providers/<name>.settings.yaml` for each gateway (openrouter, nvidia, google, openai, poolside, groq + the local `ai-kvm2-local`), and `config/guardian.keys.yaml` (guardian API keys). Since F2 the old `providers.settings.yaml` + `providers.overrides.yaml` + `models.local.settings.yaml` + `models.cloud.overrides.yaml` are **gone** — each provider file holds its own keys (enabled/base_url/api_key/timeout/model_prefixes + catalog_url/catalog_allowlist + a `models:` block with per-model overrides). `app/config_loader.py` is the central read switch: it deep-merges the full `global.settings.yaml` document into the shared CONFIG dict, then scans `config/providers/` (one document per provider). `app/proxy/providers.py` production-default `ProviderRegistry()` (no `settings_path`) also scans the directory, and (since F3) keeps the **local provider** (`*-local` name / `local: true`) in the registry as a **`managed: true`** entry — addressable as `{local-provider}/{model}`, but **never cloud-routed** (`is_cloud_model` returns False for managed; `is_cloud_or_guardian_route` returns False for managed addresses); it derives `context_overrides` from the `context_window` entries in the providers' `models:` blocks; `CloudModelCatalog` builds its `get_override` map the same way (explicit `overrides_file=` keeps the legacy single-file shape for tests). `local_models_file()` now resolves to `config/providers/ai-kvm2-local.settings.yaml` (compat for engine/scripts/tests); the `local_models.yaml` compat-symlink points there too. Legacy compat constants (`PROVIDERS_SETTINGS_FILE`, `MODELS_CLOUD_OVERRIDES_FILE`, …) are retained but unused in production. `models.cloud.settings.yaml` and `models.local.overrides.yaml` are **reserved** (in the schema) but not shipped — no runtime consumer yet.
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
- **Dashboard UI auth — empty shell ≠ code bug.** Since commit `7472d61` (2026-07-30, auth on dashboard `:11437`, bind to 127.0.0.1 only) every `/api/*` on the dashboard requires a Bearer key, but the UI itself sent no auth header → the dashboard was functionally dead (empty shell) even locally, `curl 127.0.0.1:11437/api/stats` → 401. Fixed 2026-08-15: a fetch-wrapper (monkey-patch `window.fetch`) + key-input modal in `app/ui/index.html` store the key in localStorage (`guardian_dashboard_api_key`) and inject `Authorization: Bearer <key>` on every `/api/*` call; on 401 the key is cleared and the modal reopens. A dashboard that shows an empty shell is a missing key in the browser, not a code bug — check localStorage first before touching the code.
- **PR-afhandeling op guardian — `/review`-commando (werkwijze 2026-08-26/27).** PR's worden op guardian behandeld via de PR-Piet-reviewer: na **elke laatste commit** (laatste push vóór merge) post de agent **altijd het commando `/review`** op de PR (`gh pr comment <n> --body "/review"`) zodat er een verse reviewer wordt getriggerd op de nieuwe head — ook als de `pull_request`-auto-run al draaide. `.github/workflows/pr-piet.yml` triggert op `issue_comment` (created/edited) + `pull_request` en roept de org-reusable `m0nklabs/pr-piet/.github/workflows/reusable-pr-piet.yml@main` aan; de pr-agent (`the-pr-agent/pr-agent`, `auto_review: true`, tier1 deepseek-v4-flash-0731 + optionele tier2 z-ai/glm-5.2) reviewt dan de **nieuwste head** van de PR en post een formele GitHub-review (Copilot-stijl). Slash-commando's werken via dezelfde trigger (`/describe`, `/improve`). **Geen auto-approve/auto-merge — altijd human merge.** Bot-senders (`sender.type != 'Bot'`) worden overgeslagen, dus een `/review`-comment moet van een mens-account of expliciete user-token komen, niet van een bot-workflow. **Merge-criterium (operator 2026-08-27): een PR mag pas gemerged worden als er GEEN openstaande comments/suggesties uit de review zijn** — bevindingen die met bewijs zijn weerlegd + beantwoord tellen niet als openstaand. **Twee operationele details (2026-08-27, PR #7):** (a) het `issue_comment`-commando draait op **main**, de `pull_request`-trigger op de feature-branch; het `/review`-commando cancelt via de concurrency-groep de gelijktijdig lopende auto-`pull_request`-run → die **gecancelde run telt als "fail"-check** op de PR en maakt `merge-state: UNSTABLE` — oplossing: `gh run rerun <run-id>` van de gecancelde run (dan reviewt hij de huidige head + merge-state wordt CLEAN). (b) De postende review reviewt de head die op dat moment stond (footer `head=<sha>`); na nieuwe pushes een nieuwe `/review` posten. **Infra-bevinding (2026-08-27, doorgegeven aan pr-piet door operator):** de diepe `/review`-variant (issue_comment) heeft een job-timeout van **30 min** in de pr-piet reusable; op grotere PR-diffs haalt de pr-agent (litellm) dat niet meer → tier-1-job wordt gecancelled ("The operation was canceled") en er wordt GEEN review gepost. De `pull_request`-auto-review draait wél snel (~1 min) en post dezelfde review-template op de head. Werkwijze zolang dit niet is gefixt: bij een getimede `/review` de auto-`pull_request`-run (her)runnen op de head en die als de review laten tellen.
- **PR-Piet-bevindingen zijn speculatief — verifieer vóór je ze volgt (én check of ze terecht zijn).** **Case 1 — weerlegd (PR #7, 2026-08-27):** PR-Piet claimde een regressie ("openai/groq zonder `model_prefixes`/models-list → `{provider}/...`-requests zouden niet meer herkend worden"). **Weerlegd met live-test:** `ProviderRegistry._provider_from_address()` (providers.py:391-403) matcht het **eerste padsegment** van een `{provider}/{brand}/{model}`-adres tegen `self._providers` — de provider-naam is dus wél automatisch geregistreerd voor address-vorm (exact hoe openai/groq in productie bereikt worden, pre-F2 én nu). `openai/openai/gpt-4o → openai`, `groq/... → groq`. Bare-name (`gpt-4o`) was pre-F2 óók niet herkend voor openai/groq. Geen code-wijziging nodig. **Case 2 — TÉRECHT (PR #8, F3):** toen de managed-address-exclusie in `show_model` (model_discovery.py) toegevoegd werd, werd de `if _is_failover_address(...) / elif (cloud)`-chain per ongeluk een losse `if / if`, zodat failover-adressen ná het failover-blok óók door het lokale `else`-blok (`_model_manager.resolve_model`) vielen. PR-Piet ving dit op de `pull_request`-review. **Gefixt** door de `elif`-chain te herstellen met een walrus-operator voor de managed-check (`(_addr := _provider_from_address(m)) is not None and not _addr.managed`), gepind door `tests/unit/test_server.py::test_show_model_failover_address_stays_cloud_branch`. Les: PR-Piet's "Possible Issue"-markeringen kunnen echt regressies zijn — bij structuur-wijzigingen (if/elif, guards) ná een edit altijd controleren of de control-flow-chain intact bleef; verifieer óók met een test die de verkeerde-tak-executie pinnt. **Case 3 & 4 — TÉRECHT én als-semantische lekken (PR #8, F3, zelfde sessie):** nadat `CloudProvider.managed` er kwam en managed providers `is_configured=True` werden (`api_key`-loos), wees PR-Piet er op dat *alle* "filtert-op-`is_configured`"-punten nu de managed provider zouden meetellen: (a) de `/v1/models` **cloud-entry-loop** in `model_discovery.list_models` (filterde `get_enabled_providers()` op `is_configured`) zou lokale modellen als `ai-kvm2-local/<model>` cloud-entries tonen; (b) `get_all_cloud_models()` (filterde `_model_to_provider` op `is_configured`) zou bare lokale namen rapporteren. **Beide gefixt** door managed-exclusie (`not p.managed` / `not provider.managed`), gepind door `tests/unit/test_server.py::test_list_models_excludes_managed_provider_cloud_entries` + `tests/unit/test_f3_local_managed_provider.py::test_get_all_cloud_models_excludes_managed_provider`. **Deel-2-verificatie (get_provider_for_model/cloud_provider_for_request retourneren de managed provider voor bare lokale namen) = weerlegd als misclassificatie-risico:** de enige productie-caller van `cloud_provider_for_request` is `resolve_cloud_attempts` (cloud_inference/routing.py:146), die uitsluitend bereikt wordt ná de `is_cloud_or_guardian_route`-gate (gateway/routing.py:313) die voor managed-adressen en bare lokale namen `False` retourneert — er is geen pad dat een bare lokale naam naar de cloud-forwarding leidt. Les-2: na een wijziging die `is_configured`/`get_enabled_providers`-semantiek verbreedt, systematisch alle "filtert-op-`is_configured`"- en "cloud-provider-lookup"-punten nalopen (niet alleen de expliciete routing-gates `is_cloud_model`/`is_cloud_or_guardian_route`), want die zijn de echte lek-vectoren.

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
- **Guardian 2.0 masterplan (PLAN 2026-08-26, goedgekeurd)** → `@docs/IMPLEMENTATION_PLAN.md` (canoniek) + GitHub issue #1. Eén gefaseerd plan dat de drie architectuurplannen combineert: F0 foundation (rename `llama-guardian`→`guardian-llmprovider-gateway`, nieuwe dir, service, repo publiek), F1 file register (`docs/FILE_REGISTER.md`), F2 per-provider config-bestanden, F3 local als managed provider, F4 registry ontdraaien uit manager.py, F5 caretaker-llamacpp daemon + local passief, F6 Windows/14700K provider, F7 cut-over naar de nieuwe dir + legacy bevriezen. Details per deelplan: `@docs/LAN_GPU_BACKENDS.md`, `@docs/GATEWAY_MANAGER_SPLIT.md`, `@docs/CONFIG_PROVIDER_FILES.md`. **F0+F1 gebouwd (PR #2, 2026-08-26):** rename-sweep (`llama-guardian`/`LLAMA_CPP_GUARDIAN_*` → `guardian-llmprovider-gateway`/`GUARDIAN_LLMPROVIDER_GATEWAY_*` in deploy/scripts/docs/env), CI-wiring (`python-ci` + nieuwe org-reusable `python-autofix`; autofix-push gebruikt `AUTOFIX_PAT`-secret want GITHUB_TOKEN-pushes triggeren geen runs), `docs/FILE_REGISTER.md` (draft, alle 194 tracked files). **Legacy env-vars: GEEN fallback (operator-besluit 2026-08-26)** — `app/paths.py` raised `RuntimeError` bij import als `LLAMA_CPP_GUARDIAN_ROOT`/`LLAMA_CPP_GUARDIAN_SLOTS_DIR` nog gezet worden (met exacte nieuwe var-naam in de melding); gepind door `tests/unit/test_legacy_env_rejected.py`. De live productie draait nog uit de legacy-dir tot F7 (zie Active Handoff).
  - **LAN_GPU_BACKENDS** — operator-principe: **alles wat modellen serveert leeft in de providers-registratie**; `local` wordt de enige `managed` provider-entry (engine/manager.py behoudt spawn/VRAM/switch), Windows-PC + cloud zijn externe entries (`base_url` LAN + `catalog_url: /v1/models`; llama-server adverteert zelf `/v1/models` — geverifieerd). **Stap 1 = local-als-provider GEÏMPLEMENTEERD (F3, 2026-08-27, PR #8):** `CloudProvider.managed` (default False), de lokale provider blijft in de registry als `managed: true` entry (adresseerbaar `{provider}/<model>`, nooit cloud-gerouteerd), managed providers zijn keyless maar `is_configured` (catalog uit llama-server `/v1/models`), `build_forward_headers` stuurt geen Authorization-header voor managed, en `is_cloud_model`/`is_cloud_or_guardian_route` geven False voor managed-adressen. Stap 2 = Windows-entry (config-only, hot-reload). Optie B = llama.cpp `--rpc` (haalbaar op 1 Gbit voor chat, ~5–15% overhead; blijft engine-arg, geen provider).
  - **GATEWAY_MANAGER_SPLIT** — opsplitsen in gateway (proxy/routing/capture/discovery) + **`caretaker-llamacpp`** (manager die llama-server beheert; naam gekozen 2026-08-26: fantasy-rol "caretaker" = verzorger, spiegelbeeld van guardian=gatekeeper). **Ontleding (hard geteld):** van de ~1637 regels `engine/manager.py` is ~1050 echte lifecycle (spawn/args/health/crash/unload), ~509 registry/keuze/discovery (hoort in de gateway) en ~78 settings-lezen (al gedeeld YAML — operator-gelijk: settings zijn geen manager-werk). Manager-kern is dun en **verweven met verkeer** (idle-unload leest queue/requests) — voor de lokale host volstaat een module; een daemon wordt zinvol voor GPU-hosts ZONDER Guardian (Windows). Fase 0 = de ~509 regels registry/keuze/discovery ontdraaien naar de gateway-laag. **Fase 0 GEDAAN (F4, 2026-08-28, PR #10):** de registry/keuze/discovery-logica (~501 regels) is verhuisd van `app/engine/manager.py` naar een standalone `ModelRegistry` in `app/local_inference/model_registry.py` (nieuw); `ModelManager` componeert hem (`self.registry`) met dunne delegatoren (zelfde openbare namen/signatures). `ModelRegistry` is eigenaar van `models`/`config_path`/`_vision_capabilities`/aliases + alle choice/discovery-logica (resolve_model, resolve_reload_target, preferred tool/reasoning, context windows, build_runtime_config, vision-capability cache). Runtime-state leest de manager via `bind_runtime_state(owner)` (authoritatieve `current_model`/`current_vision_enabled`/pinned/verified/backend + `_read_launch_args_file()` zodat de per-test monkeypatch van `app.engine.manager.CURRENT_MODEL_ARGS_FILE` gehonoreerd blijft). Module-globals behouden: `manager = ModelManager()`, `MISMATCH_MODEL_NAME`, `CrashRecord`, `ModelLoadError`, `VisionCapability` (geïmporteerd uit registry). Externe imports (`ModelManager`/`ModelLoadError` via `app/gateway/routing.py`, `app/local_inference/ollama.py`, `app/proxy/server.py`) intact. **Gedrag-neutraal bewezen:** `tests/unit/` → 992 passed op de refactor én op originele code (apples-to-apples via stash). **Deployment-topologie (operator 2026-08-26): manager per GPU-host — één op ai-kvm-2 én één op de 14700K; gateway alleen op ai-kvm-2, praat met beide via `management_url` (http://192.168.1.35:11441 + http://192.168.1.x:11441). Windows: geen systemd (NSSM/service), elke manager leest zijn eigen `models.local.settings.yaml` (de GGUFs met Windows-paden), idle-unload via gateway-contract.**
  - **CONFIG_PROVIDER_FILES** — één configuratiebestand per provider i.p.v. de defaults/overrides-split. **GEÏMPLEMENTEERD (F2, 2026-08-26, PR #7).** Nieuwe layout: `config/providers/<naam>.settings.yaml` — `ai-kvm2-local`, `14700k-local` (onderscheid local/cloud zit in de naam), `openrouter`, `nvidia`, `google`, enz. `providers.settings.yaml`+`providers.overrides.yaml`+`models.local.settings.yaml`+`models.cloud.overrides.yaml` zijn **verwijderd**; per-model overrides (context_window, model_defaults) zitten in het `models:`-blok van de provider zelf. `global.settings.yaml` + `guardian.keys.yaml` blijven (cross-cutting). Code-impact gedaan: paths.py (PROVIDERS_DIR + scan), config_loader (directory-scan i.p.v. merge), providers.py (excl. lokale provider uit de cloud-registry), cloud_catalog.py (get_override per provider), engine/manager via `local_models_file()` → `ai-kvm2-local.settings.yaml`. Pay-off: nieuwe provider = één bestand + hot-reload. Tests/legacy single-file blijft werken via settings_path/overrides_file.

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

### DSH session `20260826_rename` (repo-split: legacy + nieuwe bouwplaats, last updated 2026-08-26)

- Working directory (nieuw, bouwplaats): `/home/flip/guardian-llmprovider-gateway`
- **DRIE repos, definitief:**
  - `m0nklabs/llama-cpp-guardian` = **LEGACY/archief** (beschrijving gemarkeerd; teruggerenoemd). De **productie-installatie draait nog uit `/home/flip/llama_cpp_guardian`** (systemd `llama-guardian.service` + venv wijzen daarheen); die oude dir is aan deze legacy-repo gekoppeld.
  - `m0nklabs/guardian-llmprovider-gateway` = **NIEUW (bouwplaats)**. Schone herstart: lokale clone van de oude repo met **volledige git-history (326 commits)**; gitignored troep (venv, data/, .env, .scratch/, logs, caches) is NIET meegekomen. Remote + main staan.
  - `m0nklabs/caretaker-llamacpp` = **publieke** repo (renamed 2026-08-26: eerst `caretaker-llama-cpp`, nu `caretaker-llamacpp` zonder hyphen, gelijk aan de lokale dir `/home/flip/caretaker-llamacpp`) voor de per-GPU-host manager (naamkeuze: fantasy-rol "caretaker" = verzorger, spiegelbeeld van guardian=gatekeeper; vorm `caretaker-llamacpp` zonder "for"). Inhoud: `AGENTS.md`-scaffold + **`PLAN.md` (gefaseerd implementatieplan, fases A–E + gateway-wiring, geschreven 2026-08-26)**. Nog leeg qua code.
- **History-oplossing (operator-vraag "gaat de commit history verloren?"):** nee — git-history zit in `.git`, niet in de bestanden. De nieuwe repo is een clone van de oude: alle 326 commits mee, boom alleen de 192 tracked bestanden.
- **OPERATIONEEL CRITIEK voor sessies in de nieuwe dir:** er is GEEN `venv`, GEEN `.env`, GEEN `data/`, GEEN `config/guardian.keys.yaml` (allemaal gitignored) in `/home/flip/guardian-llmprovider-gateway`. Dus: (a) geen restarts/deploys vanuit deze dir — productie draait uit de oude dir; (b) pytest/gate lopen via het OUDE venv: `/home/flip/llama_cpp_guardian/venv/bin/python -m pytest tests/` (of eerst een venv aanmaken); (c) secrets (`.env`, keys) niet committen — bij een draaiende nieuwe installatie kopiëren vanuit de oude dir. **Gedaan (16:02, deze sessie):** `venv` = symlink → legacy-venv (`import app` OK), `.env` + `config/guardian.keys.yaml` + `data/cloud_catalog_cache.json` + `data/capture/` gekopieerd (allemaal gitignored); `.gitignore` uitgebreid met `venv` zonder slash (symlink werd anders als untracked getoond — commit `cbcd3ca`).
- **DSH-sessie gekopieerd naar de nieuwe workspace (2026-08-26, deze sessie):** de hele `20260826_rename`-sessie (107.830 records, ~66 MB plain) is gekopieerd naar `~/.dsh/sessions/--home-flip-guardian-llmprovider-gateway--/session-a2b871fe-aefd-4d5a-8f7d-b2c048e58e38/` — **de operator kan deze sessie dus "meenemen" en in de GUI bij de nieuwe workspace terugzien/vervolgen**. Methode (reverse-engineered uit `dsh-session-persistence-jsonl`): sessies liggen per workspace onder `~/.dsh/sessions/<projectKey(cwd)>/<sessionId>/session.jsonl.zstd`; projectKey vervangt `/` door `-` (`/home/flip/guardian-llmprovider-gateway` → `--home-flip-guardian-llmprovider-gateway--`). Kopie = origineel decompressen (`zstd -dc`), eerste regel (header) herschrijven met nieuw `id` + `cwd`, rest byte-identiek doorgeven, hercompressen naar de nieuwe project-dir. **Verificatie:** sha256 van origineel (afgekapt op kopie-moment) == sha256 van kopie (minus header) `d2bf65b9…`; alle 107.830 records parsen als JSON (0 fouten; seq-gaten zijn normaal — compaction verwijdert records); de GUI scant project-dirs bij `list()` (geen index om bij te werken). Zie de oude sessie in `--home-flip-llama_cpp_guardian--/session-57198571-…` voor de rest van deze geschiedenis (de kopie bevat alles tot het kopieermoment).
- Docs-referenties (README/CLIENT_KEY_LINKING/JSON-specs/split-plan) zijn al naar `guardian-llmprovider-gateway` gefixt (commit `5e02c78`); die kloppen nu voor de nieuwe repo.

### Open punten (actueel — alles wat hier niet staat is afgerond; details in `docs/ARCHIVED_HANDOFFS.md`)

- **Bekende test-fout (pre-existing, bewust gedeferred):** `test_cloud_attempts_resolve_google_full_address` faalt al weken op ongewijzigde HEAD (google cold-start-assertie, niet gerelateerd aan enig werk) — gemeld in meerdere gearchiveerde handoffs, niet aangeraakt.
- **NVIDIA context-metadata gap (FUTURE work):** NVIDIA-modellen krijgen de 131072-fallback; per-model `context_window`-overrides voor actief gebruikte modellen zijn nog niet ingevuld (operator: "first find what's usable, THEN max context").
- **Heads-up pi-modellen:** `~/.pi/agent/models.json` bevat nog bare-name cloudnamen + legacy `guardian/...`-entries → 404-risico als pi die ooit bare uitzendt (actieve modellen gebruiken full addresses, dus latent).
- **CI-adoptie (open sinds 20260813_1):** `scripts/pre_restart_check.py` als GitHub Action is nog niet opgepakt.
- **m0nkdash-origin (optioneel):** origineel achter `dashboard.oelala.xyz` (m0nkdash via `serve.sh`) blijft dood — raakt Guardian niet.
- **F0+F1 gebouwd (PR #2, 2026-08-26, wacht op review/merge):** rename-sweep (`llama-guardian`/`LLAMA_CPP_GUARDIAN_*` → `guardian-llmprovider-gateway`/`GUARDIAN_LLMPROVIDER_GATEWAY_*` in deploy/scripts/docs/env + 5 git mv's incl. nginx/systemd/workspace), CHANGELOG-entry, CI-wiring (`python-ci.yml` + `ruff-autofix.yml` → nieuwe org-reusable `python-autofix.yml`; `AUTOFIX_PAT`-secret nodig want GITHUB_TOKEN-pushes triggeren geen workflow-runs), `docs/FILE_REGISTER.md` (draft, alle 194 tracked files). PR-Piet review gaf 2 bevindingen: `_paths.py`-re-export (F401 door autofix verwijderd → teruggezet met `# noqa: F401`) en env-var-backcompat — die is op operator-besluit **omgedraaid**: geen fallback, `LLAMA_CPP_GUARDIAN_*` raised nu `RuntimeError` bij import (`tests/unit/test_legacy_env_rejected.py` pinnt dit). Er is nog een `test/pr-piet-clean2`-branch op origin (PR-Piet-experiment, niet van ons).
- **F2 gebouwd (PR #7, 2026-08-26, branch `f2-provider-config`, CI-groen, klaar voor merge):** per-provider config-bestanden `config/providers/*.settings.yaml` (7: ai-kvm2-local, openrouter, nvidia, google, openai, poolside, groq); de 4 oude provider/modelfiles zijn verwijderd; `app/paths.py`/`config_loader.py`/`providers.py`/`cloud_catalog.py` scannen nu de directory (lokale provider uitgesloten uit de cloud-registry); `local_models_file()` → `ai-kvm2-local.settings.yaml`. Länder: LAN_GPU_BACKENDS + GATEWAY_MANAGER_SPLIT (de échte volgende stappen) staan in Skills → `@docs/LAN_GPU_BACKENDS.md`, `@docs/GATEWAY_MANAGER_SPLIT.md`; `m0nklabs/caretaker-llamacpp` heeft nu `PLAN.md` (gefaseerd plan, fases A–E) maar nog **geen code**.
- **F4 gebouwd (PR #10, 2026-08-28, branch `f4-registry-split`, commit `51c781a`, CI-groen, klaar voor merge):** registry/keuze/discovery ontdraaid uit `engine/manager.py` naar `app/local_inference/model_registry.py` (nieuw, `ModelRegistry`); `ModelManager` componeert hem met dunne delegatoren; module-globals + externe imports intact. **Acceptatie op unit-bewijs (operator-besluit 2026-08-28, Optie A):** `tests/unit/` → 992 passed op de refactor én op originele code (apples-to-apples); de volledige suite haalt niet de beoogde "1009 passed / 3 skipped" omdat `tests/integration/test_live_inference.py` hangt op het draaiende proxy (waar de agent zelf doorheen routeert) — bewezen als milieufactor (hangt identiek op originele code), geen F4-regressie. Merge-criterium: /review zonder openstaande bevindingen.

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

## GitHub / Git / PR / Reviewers

> **Doel van deze sectie.** Expliciet maken wie de PR-reviewer is, hoe de
> review-werkstroom op guardian draait, en hoe je review-output slim afhandelt.
> De kernafspraken (`/review`-commando, merge-criterium, human-merge) staan in
> Critical rules — deze sectie is de geconsolideerde bedieningshandleiding.

### Identity van de PR-reviewer (de "PR-Piet"-persona)

Er is **geen afzonderlijke menselijke reviewer**; review gebeurt automatisch door
één en hetzelfde mechanisme, dat drie namen draagt:

1. **pr-agent fork** — `m0nklabs/pr-piet` (gepinde org-fork van
   [`the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent));
2. **GitHub Action workflow** — `.github/workflows/pr-piet.yml`, die triggert op
   `pull_request` + `issue_comment` en de org-reusable
   `m0nklabs/pr-piet/.github/workflows/reusable-pr-piet.yml@main` aanroept;
3. **PR reviewer** — op alle `m0nklabs`-repos (Copilot-stijl review,
   tier-1 `openai/deepseek/deepseek-v4-flash-0731` + optionele tier-2).

Kortom: **pr-agent fork = PR-Piet = de GitHub Action workflow = de PR reviewer.**
Slash-commando's (`/review`, `/describe`, `/improve`) werken via dezelfde
`issue_comment`-trigger.

### Hoe je een review aanvraagt (`/review`)

- Post het commando van een **mens-account** (`gh pr comment <n> --body "/review"`);
  bot-senders (`sender.type != 'Bot'`) worden overgeslagen.
- Post het na **elke laatste commit vóór merge** zodat er een verse review op de
  nieuwe head komt; de geposte review draagt de footer `head=<sha>` en bekijkt de
  head die op dat moment actueel is — na nieuwe pushes dus opnieuw `/review`.
- **Merge-criterium (operator):** een PR mag pas gemerged worden als er GEEN
  openstaande bevindingen/threads uit de review zijn; bevindingen die met bewijs
  zijn weerlegd **én beantwoord** tellen niet als openstaand.
- **Geen auto-approve/auto-merge — altijd human merge** (zie Critical rules).

### Review-output afhandelen (approven bespaart rondes)

PR-Piet presenteert bevindingen soms als **GitHub suggested code changes**. Een
suggestie die klopt kun je rechtstreeks toepassen in plaats van handmatig heen-en-weer
te redigeren:

- **UI:** GitHub → tab "Files changed" → "Commit suggestion" of "Apply suggestion batch".
- **API/CLI:** review-comments met een suggested-change diff zijn te committen via de
  GitHub REST API (pull request review comments → Apply) of door de diff rechtstreeks
  op de branch toe te passen en te pushen.
- **Batchen:** zoveel mogelijk suggesties in één keer committen — elke push naar de
  feature-branch telt mee voor het merge-criterium en de `/review`-cyclus.

### Verificatie-discipline (altijd méér dan één bewijs)

PR-Piet-bevindingen zijn **speculatief** — ze kunnen terecht óf onterecht zijn (zie
Cases 1–4 in Critical rules). Handel ze daarom als een senior die niet op één bewijs
steunt: **verifieer vóór je volgt of weerlegt, en doe dat met ten minste twee
onafhankelijke bewijzen.** Concrete combinaties:

- **Gedragstest** (live `curl`/pytest die de verkeerde-tak-executie pinnt) **+**
  **code-lezing** van de betreffende control-flow (if/elif-chain, guards).
- **Statische check** (`py_compile`, ruff F-selectie, imports) **+**
  **geautomatiseerde suite** (unit-tests) op de refactor én op originele code
  (apples-to-apples via `git stash`).
- **API-feit** (bv. code-scanning alerts via de GitHub API) **+** **PR-thread**/
  diff-bevestiging dat de gemelde regel anders is geworden.
- **Call-site-analyse** (wie roept een functie aan) **+** **test die het openbare
  contract pint**.

Pas de gepaste fix toe óf weerleg met bewijs + antwoord op de thread — en laat
een bevinding nooit ongeadresseerd "open" hangen.

### Operationele details (de bekende haken)

- **Concurrency-cancel = gerade fail-check.** Het `/review`-commando draait op main;
  de `pull_request`-trigger op de feature-branch. Het commando cancelt via de
  concurrency-groep de gelijktijdig lopende auto-`pull_request`-run → die gecancelde
  run telt als "fail"-check en maakt `merge-state: UNSTABLE`.
  **Oplossing:** `gh run rerun <run-id> --failed` → merge-state wordt CLEAN.
- **Deep-review op grote diffs.** De diepe `/review`-variant (issue_comment) heeft een
  30-min job-timeout in de pr-piet reusable; op grotere diffs kan die timen
  ("The operation was canceled", geen review gepost). De `pull_request`-auto-review
  draait snel (~1 min) en post dezelfde template — **herrun die** als de diepe review
  getimed is.
