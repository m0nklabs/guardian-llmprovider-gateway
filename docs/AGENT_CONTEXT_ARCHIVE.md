# AGENTS.md Archive — verplaatste details (2026-08-29)

> **Waarom dit bestand bestaat.** `AGENTS.md` moet een compacte index blijven binnen
> het DSH-instructiebudget (doel ≤ ~25 kB). De gedetailleerde voltekst hieronder is op
> 2026-08-29 verplaatst vanuit `AGENTS.md` — **zonder informatieverlies**. Alle
> verwijzingen in `AGENTS.md` (Critical rules, Skills, Active Handoff, Capture status)
> wijzen naar dit bestand voor de volledige details. Zie ook `AGENTS.md` zelf als index.

---

## 1. Capture Implementation Status (volledige voltekst, verplaatst uit `AGENTS.md`)

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

---

## 2. Skills-bullets over het Guardian 2.0 masterplan (volledige voltekst, verplaatst uit `AGENTS.md`)

### 2.1 Masterplan-megabullet (F0–F7 + F0/F1-detail)

- **Guardian 2.0 masterplan (PLAN 2026-08-26, goedgekeurd)** → `@docs/IMPLEMENTATION_PLAN.md` (canoniek) + GitHub issue #1. Eén gefaseerd plan dat de drie architectuurplannen combineert: F0 foundation (rename `llama-guardian`→`guardian-llmprovider-gateway`, nieuwe dir, service, repo publiek), F1 file register (`docs/FILE_REGISTER.md`), F2 per-provider config-bestanden, F3 local als managed provider, F4 registry ontdraaien uit manager.py (**✅ GEMERGED, PR #10, 2026-08-28**), F5 caretaker-llamacpp daemon + local passief (**IN UITVOERING — caretaker bootstrap + fases A–D GEMERGED in `m0nklabs/caretaker-llamacpp` (PR #1 skeleton, PR #3 lifecycle core: spawn/stop/reload/switch/unload/health/crash + ServerProcess-interface, 34 tests, guardian byte-equal args-cross-check; PR #6 fases A–D control-API: /ensure, /unload, /status, Bearer-auth). Gateway-wiring tranche 1 GEMERGED (PR #11, 2026-08-29: `app/gateway/caretaker_client.py` + idle-unload-watcher + `/admin/unload` via caretaker, lokale fallback bij onbereikbaar, fail-closed key-guard, shutdown-close). Tranche 2 IN REVIEW (request-hotpath: `app/gateway/caretaker_runtime.py` — `ensure_backend()` remote-first `/ensure` met lokale fallback + error-mapping; auto-reload/auto-switch in `routing.py`/`ollama.py` + connect-error herstelpad in `models.py` → remote-first; `manager.mark_loaded_by_caretaker()` + `save_current_context()` spiegel). Zie `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`**), F6 Windows/14700K provider, F7 cut-over naar de nieuwe dir + legacy bevriezen. Details per deelplan: `@docs/LAN_GPU_BACKENDS.md`, `@docs/GATEWAY_MANAGER_SPLIT.md`, `@docs/CONFIG_PROVIDER_FILES.md`, `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`. **F0+F1 gebouwd (PR #2, 2026-08-26):** rename-sweep (`llama-guardian`/`LLAMA_CPP_GUARDIAN_*` → `guardian-llmprovider-gateway`/`GUARDIAN_LLMPROVIDER_GATEWAY_*` in deploy/scripts/docs/env), CI-wiring (`python-ci` + nieuwe org-reusable `python-autofix`; autofix-push gebruikt `AUTOFIX_PAT`-secret want GITHUB_TOKEN-pushes triggeren geen runs), `docs/FILE_REGISTER.md` (draft, alle 194 tracked files). **Legacy env-vars: GEEN fallback (operator-besluit 2026-08-26)** — `app/paths.py` raised `RuntimeError` bij import als `LLAMA_CPP_GUARDIAN_ROOT`/`LLAMA_CPP_GUARDIAN_SLOTS_DIR` nog gezet worden (met exacte nieuwe var-naam in de melding); gepind door `tests/unit/test_legacy_env_rejected.py`. De live productie draait nog uit de legacy-dir tot F7 (zie Active Handoff).

### 2.2 Sub-bullet LAN_GPU_BACKENDS

- **LAN_GPU_BACKENDS** — operator-principe: **alles wat modellen serveert leeft in de providers-registratie**; `local` wordt de enige `managed` provider-entry (engine/manager.py behoudt spawn/VRAM/switch), Windows-PC + cloud zijn externe entries (`base_url` LAN + `catalog_url: /v1/models`; llama-server adverteert zelf `/v1/models` — geverifieerd). **Stap 1 = local-als-provider GEÏMPLEMENTEERD (F3, 2026-08-27, PR #8):** `CloudProvider.managed` (default False), de lokale provider blijft in de registry als `managed: true` entry (adresseerbaar `{provider}/<model>`, nooit cloud-gerouteerd), managed providers zijn keyless maar `is_configured` (catalog uit llama-server `/v1/models`), `build_forward_headers` stuurt geen Authorization-header voor managed, en `is_cloud_model`/`is_cloud_or_guardian_route` geven False voor managed-adressen. Stap 2 = Windows-entry (config-only, hot-reload). Optie B = llama.cpp `--rpc` (haalbaar op 1 Gbit voor chat, ~5–15% overhead; blijft engine-arg, geen provider).

### 2.3 Sub-bullet GATEWAY_MANAGER_SPLIT

- **GATEWAY_MANAGER_SPLIT** — opsplitsen in gateway (proxy/routing/capture/discovery) + **`caretaker-llamacpp`** (manager die llama-server beheert; naam gekozen 2026-08-26: fantasy-rol "caretaker" = verzorger, spiegelbeeld van guardian=gatekeeper). **Ontleding (hard geteld):** van de ~1637 regels `engine/manager.py` is ~1050 echte lifecycle (spawn/args/health/crash/unload), ~509 registry/keuze/discovery (hoort in de gateway) en ~78 settings-lezen (al gedeeld YAML — operator-gelijk: settings zijn geen manager-werk). Manager-kern is dun en **verweven met verkeer** (idle-unload leest queue/requests) — voor de lokale host volstaat een module; een daemon wordt zinvol voor GPU-hosts ZONDER Guardian (Windows). Fase 0 = de ~509 regels registry/keuze/discovery ontdraaien naar de gateway-laag. **Fase 0 GEDAAN + GEMERGED (F4, 2026-08-28, PR #10 → main, squash-commit `b734d3c`):** de registry/keuze/discovery-logica (~501 regels) is verhuisd van `app/engine/manager.py` naar een standalone `ModelRegistry` in `app/local_inference/model_registry.py` (nieuw); `ModelManager` componeert hem (`self.registry`) met dunne delegatoren (zelfde openbare namen/signatures). `ModelRegistry` is eigenaar van `models`/`config_path`/`_vision_capabilities`/aliases + alle choice/discovery-logica (resolve_model, resolve_reload_target, preferred tool/reasoning, context windows, build_runtime_config, vision-capability cache). Runtime-state leest de manager via `bind_runtime_state(owner)` (authoritatieve `current_model`/`current_vision_enabled`/pinned/verified/backend + `_read_launch_args_file()` zodat de per-test monkeypatch van `app.engine.manager.CURRENT_MODEL_ARGS_FILE` gehonoreerd blijft). Module-globals behouden: `manager = ModelManager()`, `MISMATCH_MODEL_NAME`, `CrashRecord`, `ModelLoadError`, `VisionCapability` (geïmporteerd uit registry). Externe imports (`ModelManager`/`ModelLoadError` via `app/gateway/routing.py`, `app/local_inference/ollama.py`, `app/proxy/server.py`) intact. **Gedrag-neutraal bewezen:** `tests/unit/` → 992 passed op de refactor én op originele code (apples-to-apples via stash). **Deployment-topologie (operator 2026-08-26): manager per GPU-host — één op ai-kvm-2 én één op de 14700K; gateway alleen op ai-kvm-2, praat met beide via `management_url` (http://192.168.1.35:11441 + http://192.168.1.x:11441). Windows: geen systemd (NSSM/service), elke manager leest zijn eigen `models.local.settings.yaml` (de GGUFs met Windows-paden), idle-unload via gateway-contract.**

### 2.4 Sub-bullet CONFIG_PROVIDER_FILES

- **CONFIG_PROVIDER_FILES** — één configuratiebestand per provider i.p.v. de defaults/overrides-split. **GEÏMPLEMENTEERD (F2, 2026-08-26, PR #7).** Nieuwe layout: `config/providers/<naam>.settings.yaml` — `ai-kvm2-local`, `14700k-local` (onderscheid local/cloud zit in de naam), `openrouter`, `nvidia`, `google`, enz. `providers.settings.yaml`+`providers.overrides.yaml`+`models.local.settings.yaml`+`models.cloud.overrides.yaml` zijn **verwijderd**; per-model overrides (context_window, model_defaults) zitten in het `models:`-blok van de provider zelf. `global.settings.yaml` + `guardian.keys.yaml` blijven (cross-cutting). Code-impact gedaan: paths.py (PROVIDERS_DIR + scan), config_loader (directory-scan i.p.v. merge), providers.py (excl. lokale provider uit de cloud-registry), cloud_catalog.py (get_override per provider), engine/manager via `local_models_file()` → `ai-kvm2-local.settings.yaml`. Pay-off: nieuwe provider = één bestand + hot-reload. Tests/legacy single-file blijft werken via settings_path/overrides_file.

---

## 3. De twee langste Critical-rules-bullets (volledige voltekst, verplaatst uit `AGENTS.md`)

### 3.1 PR-afhandeling op guardian — `/review`-commando

- **PR-afhandeling op guardian — `/review`-commando (werkwijze 2026-08-26/27).** PR's worden op guardian behandeld via de PR-Piet-reviewer: na **elke laatste commit** (laatste push vóór merge) post de agent **altijd het commando `/review`** op de PR (`gh pr comment <n> --body "/review"`) zodat er een verse reviewer wordt getriggerd op de nieuwe head — ook als de `pull_request`-auto-run al draaide. `.github/workflows/pr-piet.yml` triggert op `issue_comment` (created/edited) + `pull_request` en roept de org-reusable `m0nklabs/pr-piet/.github/workflows/reusable-pr-piet.yml@main` aan; de pr-agent (`the-pr-agent/pr-agent`, `auto_review: true`, tier1 deepseek-v4-flash-0731 + optionele tier2 z-ai/glm-5.2) reviewt dan de **nieuwste head** van de PR en post een formele GitHub-review (Copilot-stijl). Slash-commando's werken via dezelfde trigger (`/describe`, `/improve`). **Geen auto-approve/auto-merge — altijd human merge.** Bot-senders (`sender.type != 'Bot'`) worden overgeslagen, dus een `/review`-comment moet van een mens-account of expliciete user-token komen, niet van een bot-workflow. **Merge-criterium (operator 2026-08-27): een PR mag pas gemerged worden als er GEEN openstaande comments/suggesties uit de review zijn** — bevindingen die met bewijs zijn weerlegd + beantwoord tellen niet als openstaand. **Twee operationele details (2026-08-27, PR #7):** (a) het `issue_comment`-commando draait op **main**, de `pull_request`-trigger op de feature-branch; het `/review`-commando cancelt via de concurrency-groep de gelijktijdig lopende auto-`pull_request`-run → die **gecancelde run telt als "fail"-check** op de PR en maakt `merge-state: UNSTABLE` — oplossing: `gh run rerun <run-id>` van de gecancelde run (dan reviewt hij de huidige head + merge-state wordt CLEAN). (b) De postende review reviewt de head die op dat moment stond (footer `head=<sha>`); na nieuwe pushes een nieuwe `/review` posten. **Infra-bevinding (2026-08-27, doorgegeven aan pr-piet door operator):** de diepe `/review`-variant (issue_comment) heeft een job-timeout van **30 min** in de pr-piet reusable; op grotere PR-diffs haalt de pr-agent (litellm) dat niet meer → tier-1-job wordt gecancelled ("The operation was canceled") en er wordt GEEN review gepost. De `pull_request`-auto-review draait wél snel (~1 min) en post dezelfde review-template op de head. Werkwijze zolang dit niet is gefixt: bij een getimede `/review` de auto-`pull_request`-run (her)runnen op de head en die als de review laten tellen.

### 3.2 PR-Piet-bevindingen zijn speculatief — verifieer vóór je ze volgt (Cases 1–4)

- **PR-Piet-bevindingen zijn speculatief — verifieer vóór je ze volgt (én check of ze terecht zijn).** **Case 1 — weerlegd (PR #7, 2026-08-27):** PR-Piet claimde een regressie ("openai/groq zonder `model_prefixes`/models-list → `{provider}/...`-requests zouden niet meer herkend worden"). **Weerlegd met live-test:** `ProviderRegistry._provider_from_address()` (providers.py:391-403) matcht het **eerste padsegment** van een `{provider}/{brand}/{model}`-adres tegen `self._providers` — de provider-naam is dus wél automatisch geregistreerd voor address-vorm (exact hoe openai/groq in productie bereikt worden, pre-F2 én nu). `openai/openai/gpt-4o → openai`, `groq/... → groq`. Bare-name (`gpt-4o`) was pre-F2 óók niet herkend voor openai/groq. Geen code-wijziging nodig. **Case 2 — TÉRECHT (PR #8, F3):** toen de managed-address-exclusie in `show_model` (model_discovery.py) toegevoegd werd, werd de `if _is_failover_address(...) / elif (cloud)`-chain per ongeluk een losse `if / if`, zodat failover-adressen ná het failover-blok óók door het lokale `else`-blok (`_model_manager.resolve_model`) vielen. PR-Piet ving dit op de `pull_request`-review. **Gefixt** door de `elif`-chain te herstellen met een walrus-operator voor de managed-check (`(_addr := _provider_from_address(m)) is not None and not _addr.managed`), gepind door `tests/unit/test_server.py::test_show_model_failover_address_stays_cloud_branch`. Les: PR-Piet's "Possible Issue"-markeringen kunnen echt regressies zijn — bij structuur-wijzigingen (if/elif, guards) ná een edit altijd controleren of de control-flow-chain intact bleef; verifieer óók met een test die de verkeerde-tak-executie pinnt. **Case 3 & 4 — TÉRECHT én als-semantische lekken (PR #8, F3, zelfde sessie):** nadat `CloudProvider.managed` er kwam en managed providers `is_configured=True` werden (`api_key`-loos), wees PR-Piet er op dat *alle* "filtert-op-`is_configured`"-punten nu de managed provider zouden meetellen: (a) de `/v1/models` **cloud-entry-loop** in `model_discovery.list_models` (filterde `get_enabled_providers()` op `is_configured`) zou lokale modellen als `ai-kvm2-local/<model>` cloud-entries tonen; (b) `get_all_cloud_models()` (filterde `_model_to_provider` op `is_configured`) zou bare lokale namen rapporteren. **Beide gefixt** door managed-exclusie (`not p.managed` / `not provider.managed`), gepind door `tests/unit/test_server.py::test_list_models_excludes_managed_provider_cloud_entries` + `tests/unit/test_f3_local_managed_provider.py::test_get_all_cloud_models_excludes_managed_provider`. **Deel-2-verificatie (get_provider_for_model/cloud_provider_for_request retourneren de managed provider voor bare lokale namen) = weerlegd als misclassificatie-risico:** de enige productie-caller van `cloud_provider_for_request` is `resolve_cloud_attempts` (cloud_inference/routing.py:146), die uitsluitend bereikt wordt ná de `is_cloud_or_guardian_route`-gate (gateway/routing.py:313) die voor managed-adressen en bare lokale namen `False` retourneert — er is geen pad dat een bare lokale naam naar de cloud-forwarding leidt. Les-2: na een wijziging die `is_configured`/`get_enabled_providers`-semantiek verbreedt, systematisch alle "filtert-op-`is_configured`"- en "cloud-provider-lookup"-punten nalopen (niet alleen de expliciete routing-gates `is_cloud_model`/`is_cloud_or_guardian_route`), want die zijn de echte lek-vectoren.

---

## 4. Critical rule: Config-schema split / per-provider files (volledige voltekst, verplaatst uit `AGENTS.md`)

- **Config-schema split (2026-08-21, PR #9, `docs/CONFIG_SCHEMA.md`); per-provider files sinds F2 (2026-08-26, `docs/CONFIG_PROVIDER_FILES.md`, PR #7).** `config/settings.yaml` is split into domain files: `config/global.settings.yaml` (proxy/queue/timeouts/scaler/capture/grammar/cloud_retry/failover_health/services/services_to_stop/benchmark), a per-provider file `config/providers/<name>.settings.yaml` for each gateway (openrouter, nvidia, google, openai, poolside, groq + the local `ai-kvm2-local`), and `config/guardian.keys.yaml` (guardian API keys). Since F2 the old `providers.settings.yaml` + `providers.overrides.yaml` + `models.local.settings.yaml` + `models.cloud.overrides.yaml` are **gone** — each provider file holds its own keys (enabled/base_url/api_key/timeout/model_prefixes + catalog_url/catalog_allowlist + a `models:` block with per-model overrides). `app/config_loader.py` is the central read switch: it deep-merges the full `global.settings.yaml` document into the shared CONFIG dict, then scans `config/providers/` (one document per provider). `app/proxy/providers.py` production-default `ProviderRegistry()` (no `settings_path`) also scans the directory, and (since F3) keeps the **local provider** (`*-local` name / `local: true`) in the registry as a **`managed: true`** entry — addressable as `{local-provider}/{model}`, but **never cloud-routed** (`is_cloud_model` returns False for managed; `is_cloud_or_guardian_route` returns False for managed addresses); it derives `context_overrides` from the `context_window` entries in the providers' `models:` blocks; `CloudModelCatalog` builds its `get_override` map the same way (explicit `overrides_file=` keeps the legacy single-file shape for tests). `local_models_file()` now resolves to `config/providers/ai-kvm2-local.settings.yaml` (compat for engine/scripts/tests); the `local_models.yaml` compat-symlink points there too. Legacy compat constants (`PROVIDERS_SETTINGS_FILE`, `MODELS_CLOUD_OVERRIDES_FILE`, …) are retained but unused in production. `models.cloud.settings.yaml` and `models.local.overrides.yaml` are **reserved** (in the schema) but not shipped — no runtime consumer yet.

---

## 5. Active Handoff — DSH session `20260826_rename` (volledige voltekst, verplaatst uit `AGENTS.md`)

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

---

## 6. GitHub Actions runners — org-pool (volledige voltekst, verplaatst uit `AGENTS.md`)

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
