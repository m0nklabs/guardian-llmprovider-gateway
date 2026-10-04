# AGENT_JOURNAL — append-only findings-log (cold file)

> **Werkwijze:** feiten/lessen die je had moeten opgraven (reverse-engineering,
> live tests, verborgen gedrag) → hier APPEN, zelfde sessie, met datum-kop.
> Dit bestand zit NIET in de DSH system prompt → appen kost geen prompt-cache.
> De **promotie-pass** (gebatcht, zie `~/.dsh/AGENTS.md` → "AGENTS.md maintenance
> discipline") distilleert periodiek: universele feiten → regels in `AGENTS.md`;
> detail → docs/-pagina's; verouderd → wissen of archiveren.
> Nederlands is prima (operator-facing, intern).

## Batch 1 gearchiveerd (2026-08-30) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

## September-01-batch → gearchiveerd (voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`)

## Batch 2 gearchiveerd (2026-09-15, onderhoudspass: 38,6 kB → dit) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

## Batch 3 gearchiveerd (2026-09-30, onderhoudspass: 42,8 kB → dit) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

Gedistilleerde lessen uit de gearchiveerde entries (les → waar hij nu leeft):
- **Managed provider als failover-candidate** (09-11): vóór de forward `caretaker_runtime.ensure_backend(model=…, local_fallback=…)` (keyword-only), anders serveert llama-server stil het toevallig geladen model (09-01-mismatchklasse); lokale fallback schakelt de eigen backend om: volgorde = config.
- **Deploy-les (Windows):** de NSSM tree-kill neemt de caretaker-gespawnde llama-server mee bij ELKE service-(re)start → daarna `schtasks /run /tn caretaker-boot-ensure`; nooit engines via ssh/Start-Process starten (ze verdwijnen stil).
- **Speech is capability-in-config, geen host-eigenschap** (09-16 t/m 09-19, operator-bindend): `tts_url`/`stt_url` + management = lokale engine; `base_url` + `api_key` = cloud. Poorten: TTS :11450, STT :11451, caretaker :11440/:11441.
- **Lifecycle/VRAM:** ensure/release/status + idle-watcher (600 s) + VRAM-gate met wacht-of-opgeven + `_ensure_lock` (nooit twee engines op één GPU); `tts.ensure_timeout_seconds` (420) MOET boven start-timeout + wachttijd zitten (240+120=360), anders valt een gezonde primary stil weg.
- **Restart-verificatie:** na een herstart óók een nieuwe connectie verifiëren (`curl -m5 http://127.0.0.1:11435/healthz`) — de transient accept-wedge van 09-16 (16:14:56-restart) was self-herstellend; MainPID == listener is niet genoeg.
- **`tts:`/`stt:`-yaml wordt RAW gelezen:** `${VAR}`-expansie via `_expand_env` (providers.py); zonder die helper bleef `${WINDOWS_LAN_KEY}` letterlijk → Bearer-literal → 401.
- **Route-georiënteerde speech-routing (09-23) verving de dubbele cloud-opt-in** (09-22): adres `[guardian/]{provider}/…`, upstream-id = alles ná de provider verbatim, min. 2 segmenten — de `{brand}/{model}`-vorm gaf Groq 404 op `groq/groq/whisper-large-v3`.

Carry-forward per gearchiveerd milestone (essentie + bewijs → voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md` Batch 3):
- **F6 + F7** (09-11, goal-95e50473): Windows/homelab-provider live e2e, catalog refresh 1 model, 283 totaal geadverteerd; unit-`WorkingDirectory` `/home/flip/guardian-llmprovider-gateway`, nginx TLS-mux live, legacy `/home/flip/llama_cpp_guardian` bevroren — F7 bleek al afgerond; modeladres `windows-gpu-local/windows/qwen3.5-9b`.
- **Cross-host failover** (09-11, §F6 'local → windows'): qwen35-groep primary `windows-gpu-local` → fallback `ai-node-local` met model-switch en F5 adopt-poll; de call-sites-gate ving een foute `ensure_backend`-aanroep vóór de restart.
- **TTS/STT-keten** (09-16 → 09-19): `/v1/audio/speech` + `/v1/audio/transcriptions`, engines `qwen3tts-http` (:11450) en Qwen3-ASR-1.7B via sherpa-onnx 1.13.8 (:11451); `tts.providers: [windows-gpu-local, ai-node-local]`; caretaker `stt.py` = parameterized copy van `tts.py` (dedup open).
- **TTS clone passthrough** (09-21/09-22): `tts.py` `_build_engine_payload` geeft `ref_audio`/`ref_text`/`language` door — nog live; op 09-22 verbatim als commit gered.
- **Cloud-forwarding met dubbele opt-in** (09-22): VERVANGEN door de route-georiënteerde routing van 09-23 — alleen historisch relevant.

## Batch 4 gearchiveerd (2026-10-04, onderhoudspass: 27,9 kB → dit) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

Gearchiveerd: de entries 09-23 t/m 09-25 (speech-routing, speech-adresvorm, openrouter-correctie, caretaker-VRAM-uniform, Fish Audio-provider, Fish model-header) én de oudere gedistilleerden van B1/09-01/B2 — alles vóór de ~2026-09-30-grens; de marker-koppen, de Batch 3-gedistilleerden en alle 10-02-entries blijven staan.

Lessen (B1/09-01/B2) staan nu verbatim in het archief; woonplaatsen/kernpunten: zoek-tool-ranking + hot/cold-werkwijze → `~/.dsh/AGENTS.md`; F5-contract → `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`; F6/F7 → `@docs/IMPLEMENTATION_PLAN.md` + ARCHIVED_HANDOFFS B3; namespace-claims zijn claims, geen garanties (resolutie op catalog-bewijs, `3f981e4`); BaseHTTPMiddleware breekt `is_disconnected()` (raw ASGI receive); persist-velden pinnen via persist→read-roundtrip; migreren vóór verwijderen (`aec0f7d`); contract-drift-tests door de échte keten; gate via `${PIPESTATUS[0]}`; degeneratie-guard op de fundamentele period q; restart-baseline (`29953e1`); model-mismatch nooit stil substitueren; live-feiten verifiëren vóór adviseren.

Carry-forward per nog-OPEN item (essentie + bewijs → voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md` Batch 4):
- **Speech-routing route-georiënteerd** (09-23, OPEN): live in `app/gateway/speech_routing.py` — exact adres = exact resultaat (502 met de routenaam, onbekend = 404 model_not_served); 21 routepinnen, gate 5/5; opvolging door de 09-23-correcties (zelfde archief-batch).
- **Speech-adresvorm** (09-23, OPEN): upstream-id = alles na de provider, verbatim, min. 2 segmenten; nóg open: groq-TTS wacht op terms-acceptance (console.groq.com, org-admin).
- **Fish Audio-provider** (09-23, OPEN; `speech_adapter: fish`): TTS live via `model`-header (09-24, gratis `s2.1-pro-free` → 200/62 kB); nóg open: fish-STT (`transcribe-1`, $0.36/uur) levert eerlijke 502 tot er API-credit is; pinnen 24/24.
- **Comfy-start-herhaaltjes** (09-25, caretaker PR #11 → 063d419): volgkandidaat — alleen bij structurele herhaaltjes de comfy-versie/bat-lus onderzoeken.
- **OpenRouter-catalogusvelden** (08-30, B1): open punt → `docs/HANDOFF.md` (in deze pass ongemoeid gelaten).
- **Nemotron `supported_efforts`-verfijning** (09-10, `ed3344f`, B2): open optimalisatie — lightning exposeert géén efforts, super/ultra wel.

## 2026-10-02 — Guardian product scope expanded to a policy and traffic gateway

- **Owner / goal:** DSH roadmap lead (`github/copilot/gpt-6.1-sol`); operator
  requested a substantial roadmap revision beyond LLM-only use. Checkpoint:
  2026-10-02T20:28:55Z deployment observation, same-session documentation update.
- **Delivered:** `docs/ROADMAP.md` is the canonical product roadmap: shared
  policy/traffic core plus capability adapters, G0–G5 priorities/dependencies,
  security-first non-AI HTTP vertical slice, load balancing, operational/cost/
  privacy controls, and later tool/MCP and multi-replica contracts. The original
  F0–F7 `docs/IMPLEMENTATION_PLAN.md` remains unchanged as historical evidence.
- **Verified finding:** the F7-open handoff item was stale. Journal Batch 3
  already retains F6/F7 closure; `systemctl show
  guardian-llmprovider-gateway.service -p WorkingDirectory -p ActiveState
  -p FragmentPath` independently returned the new checkout and `active`.
  The replaced item was archived verbatim before correction.
- **Constraints / hypothesis:** always-in-path enforcement requires network and
  credential controls as well as gateway code. Existing ordered failover is not
  generalized load balancing; LLM tool-call passthrough is not tool authorization.
  Existing raw capture and Keanu responsibilities remain unchanged. No runtime
  capability, config, service name, endpoint or external issue was changed.
- **Changed files owned here:** `docs/ROADMAP.md`, `README.md`, `AGENTS.md`,
  `docs/ARCHITECTURE.md` (legacy warning), `docs/FILE_REGISTER.md`,
  `docs/HANDOFF.md`, `docs/ARCHIVED_HANDOFFS.md`, and this journal.
  Pre-existing forwarding/config/router/test/provider-file changes were preserved.
- **Independent audit accepted with correction:** child confirmed no generic
  HTTP/MCP path, reactive upstream-429 handling rather than ingress rate limiting,
  and ordered failover rather than load balancing. Its missing-path findings were
  independently corrected and path-checked (`auth.py`, `ratelimit.py`). The key
  minting/admin isolation gap was checked directly: `server.py:1343–1361`,
  `admin_api.py:106–120`, `auth.py:411–459`; recorded as G1 P0, not fixed here.
  Historic F6 completion and current uncommitted failover enhancements are
  distinct; the latter do not erase historical deployment evidence.
- **Late audit claim rejected:** the child described capture as automatically
  redacting/stripping payloads based on `redactor.py`. The actual integration
  explicitly stores raw system prompts, reasoning and tool results and delegates
  redaction to Keanu (`app/capture/integration.py:9–17,384–385`). The roadmap's
  raw-capture warning is correct; existence of a redaction helper is not evidence
  it runs in the request pipeline. No automatic capture sanitization is claimed.
- **Late delta accepted narrowly:** G1 now explicitly calls for migrating LAN
  management/speech Bearer-over-HTTP hops to verified TLS or reviewed encrypted
  transport. Current remote-ensure evidence: working-tree
  `app/cloud_inference/forwarding.py:178–187`; this is not a fresh live-network
  observation. Missing-path findings were already resolved. No pre-existing
  changes were committed; roadmap work does not authorize committing concurrent
  implementation or provider configuration.
- **Checks:** `git diff --check` passed; Python documentation validation passed
  for roadmap local links/source paths, six phase exit gates and entry-point
  references. Runtime pytest/compile/restart gate not run: documentation-only
  changes, no restart. Baseline failure claims remain historical, not revalidated.
- **Next action:** G0 endpoint/policy matrix and threat model, measured baseline,
  then select one private read-only non-AI HTTP service before implementation.
  No implementation blocker; first target and policy decisions are phase gates.
- **Owned work:** read-only capability-audit child
  `dae05518-c0c2-4e55-a826-bfd7d3b4447a`; no background shell jobs or services started.

## 2026-10-02 — General service naming clarification

- **Checkpoint UTC:** 2026-10-02T20:43:15Z; owner: DSH
  `github/copilot/gpt-6.1-sol`; documentation-only operator request.
- **Decision:** Guardian is the policy and traffic gateway for explicitly
  onboarded services. General conceptual addressing is
  `{provider}/{brand}/{service}` (application/service); existing
  `{provider}/{brand}/{model}` addresses are its LLM specialization.
  Guardian mediates above the namespace. API protocols, capabilities,
  engines and bridges are distinct architecture concerns, not address layers.
- **Compatibility:** no runtime identifier migration, forced brand segment,
  request-field rename, new generic resolver or routing change. Existing local
  aliases/upstream IDs remain valid under their adapter contracts. Brand-less
  general service naming remains a schema decision before implementation.
- **Evidence:** `app/proxy/providers.py` and
  `app/gateway/model_discovery.py` use provider-prefixed cloud IDs;
  `app/gateway/speech_routing.py` preserves the upstream remainder and accepts
  an optional `guardian/` prefix. `docs/LLM_ROUTER.md` still contains historical
  credential-link and prefixed-route instructions; a prominent current-contract
  warning now prevents treating these as current onboarding guidance.
- **Scope/status:** general HTTP/tool mediation remains planned and generalized
  balancing/policy remains partial. Failover is not healthy-backend load
  distribution. Pre-existing dirty code, config and documentation are preserved;
  no commit, push, service restart or deployment is authorized by this task.
- **Outputs:** `AGENTS.md`, `README.md`, `docs/ROADMAP.md`,
  `docs/ARCHITECTURE.md`, `docs/LLM_ROUTER.md`, and this journal.
- **Verification:** scoped `git diff --check` passed. Python assertions passed
  for naming consistency across the four documentation entry sections, their
  local links/heading anchors, and durable agent scope guidance. Diffs reviewed
  against the pre-existing working tree. The first link-check attempt used the
  wrong historical-section delimiter and included legacy architecture links,
  revealing the already-broken `../config/settings.yaml` reference; corrected
  scoped check passed, unrelated historical cleanup is outside this task.
  Runtime tests/compile/restart gate were not run: documentation-only changes.
- **Owned work:** documentation child `ad5552b8-1bf8-45c5-aadf-68244b23b345`;
  no background shell jobs or services started. Next: future G0 reconciliation
  of historical docs and service-adapter schema policy before implementation;
  no blocker for this clarification.

## 2026-10-02 — Astra missing from GitHub discovery (read-only diagnosis)

- **Owner/goal:** DSH `github/copilot/gpt-6.1-sol`; trace the missing Astra
  card and check naming against the general provider/brand/service vocabulary.
- **Live evidence:** verified TLS GETs to Guardian `/v1/models`,
  `/api/cloud/models`, and `/api/cloud/catalog` show only three GitHub entries:
  `github/copilot/gpt-6.1-sol` and two `github/github/claude-*` aliases.
  Astra is advertised instead as `openai/openai/gpt-6-astra`. Discovery is not
  proof of a successful inference call or account-level model entitlement.
- **Cause:** `config/providers/github.settings.yaml` points to LiteLLM on
  loopback port 4000. Its `/v1/models` advertises three static aliases.
  Authenticated GET to the underlying copilot-api `/models` on loopback port
  4141 returns 31 models, including exact ID `gpt-6-astra` (not
  `gpt-6.0-astra`), vendor `OpenAI`, policy enabled, context 1050000,
  output limit 128000, and Responses-only endpoints. Astra has no LiteLLM
  alias in the inspected external bridge config, so cannot appear in the
  Guardian GitHub catalog through the current discovery chain.
- **Naming mismatch:** the existing alias yields brand `copilot`, not model
  maker `openai`. Bare IDs are normalized to the provider name by
  `app/proxy/cloud_catalog.py:231–257`, yielding `github/github/claude-*`;
  those external LiteLLM aliases actually forward to a local Qwen target, not
  Copilot Claude. This catalog mixes unrelated routes under GitHub.
- **Next action:** provide a reachable Responses bridge alias and a consistent
  public GitHub/OpenAI model identity while preserving the existing Sol alias.
  Filter unrelated bridge aliases. Simply changing catalog_url to advertise
  Copilot's entire catalog would claim targets that the static forwarding
  bridge does not currently expose. External bridge changes require explicit
  cross-project scope; none made during this diagnosis.
- **Checks/limitations:** unauthenticated Copilot discovery returned 401 and a
  repository GITHUB_API_KEY was not accepted by copilot-api; its declared
  service environment COPILOT_GATEWAY_KEY authenticated discovery successfully.
  Initial loopback HTTP hit the TLS socket; trusted public TLS endpoint then
  worked. No inference traffic, runtime writes, config reloads or restarts.
  Credentials were used internally and not reproduced in the report.
- **Dashboard audit:** `app/ui/index.html:1625–1646` builds cards from
  `/api/cloud/catalog` and badges the serving provider (`p.name`), not the
  brand. Independently read and confirmed; no UI re-grouping defect explains
  Astra. Admin catalog lacks the configured-model fallback present in
  `/v1/models`, but this is not the observed Astra cause: GitHub has a nonempty
  three-entry catalog and no configured Astra model. Catalog normalizer uses
  provider-name defaults and ignores per-entry vendor metadata/config brand.
- **Verification:** existing brand-normalization pytest selection passed
  (4 passed, 27 deselected); journal `git diff --check` passed. Child isolated
  checks also passed, but no full-suite or inference success is claimed.
- **Owned work:** read-only dashboard/code audit child
  `306dbb95-770b-40df-b491-6feb54c0c8ad` completed; no background shell jobs
  started. Diagnosis complete; runtime correction awaits operator scope.

## 2026-10-02 — github-copilot naming live (operator: clean, no legacy)

- **Decision:** provider `github-copilot`, brand `openai`, service/model
  `gpt-6-astra` / `gpt-6.1-sol` (exact id `gpt-6-astra`, never `gpt-6.0-astra`).
  Operator explicitly rejected legacy compatibility: old `github` provider file
  deleted, `copilot/gpt-6.1-sol` LiteLLM alias removed, no preserved aliases.
- **Changes:** `config/providers/github-copilot.settings.yaml` (allowlist
  + per-model overrides, 1m context / 128k output), LiteLLM aliases
  `openai/gpt-6.{1-sol,astra}` → `openai/responses/…` via copilot-api,
  DSH client refs in `~/.dsh/settings.yaml` updated, docs updated
  (README/ROADMAP/LLM_ROUTER, child c91338ad), new pin test
  `tests/unit/test_github_copilot_provider.py`.
- **Activation:** full suite green (1467 passed; one load-timing flake in
  `test_lifespan_does_not_wait_for_startup_check` passed 5/5 isolated);
  `litellm-proxy.service` restarted (idle verified; ~9 s startup, no crash);
  Guardian `POST /api/config/reload` ok; forced catalog refresh fetched 2
  models (claude bridge aliases correctly filtered by `catalog_allowlist`).
- **Live verification:** Guardian `/v1/models` + `/api/cloud/catalog` advertise
  exactly `github-copilot/openai/gpt-6.1-sol` and
  `github-copilot/openai/gpt-6-astra`; zero `github/*` entries remain.
  End-to-end chat through Guardian for both models returned `OK`
  (finish_reason stop, served_model `openai/gpt-6-astra` resp.
  `openai/gpt-6.1-sol`). Guardian itself was NOT restarted (hot reload only;
  no `app/*.py` changes), so no session traffic was cut.
- **Untracked note:** `docs/ROADMAP.md`, new provider file and test are
  uncommitted, consistent with the preserved pre-existing dirty tree; no
  commit/push authorized this task.

## 2026-10-03 — OpenRouter-parity model metadata for Guardian's discovery API — DSH agent (deepseek-v4.1-flash)

- **Goal (operator):** Guardian's `/v1/models` returned a thin entry (`id`,
  `object`, `created`, `owned_by`, `permission`, `served_by`, `provider` plus
  context fields) while OpenRouter publishes `architecture`, `pricing`,
  `top_provider`, `supported_parameters`, `name`/`description`,
  `knowledge_cutoff`, … Guardian should answer the same information in the
  same style, **including for providers whose own `/v1/models` advertises
  almost nothing** — filled in from another provider's catalog for the same
  model. Contract: `docs/OPENROUTER_PARITY.md` (new).
- **Delivered:** per-model metadata capture in `CloudModelCatalog` (trap 3:
  added to **both** `_persist_cache` and `_load_disk_cache`); new
  `ModelReferenceCatalog` (`app/proxy/openrouter_reference.py`) + path helper;
  new pure `app/gateway/model_metadata_presentation.py`; new
  `app/gateway/metadata_enrichment.py` merge layer; new
  `GET /v1/models/{model_id}/endpoints` (`app/gateway/model_endpoints.py`,
  registered before the greedy `{model_id:path}` catch-all); dashboard catalog
  view in `app/ui/index.html`; `reference_catalog.sources` config block on the
  openrouter provider file (`catalog_url: /models/user` deliberately untouched,
  so discovery still advertises only account-reachable models while the public
  466-model list is used purely as reference data).
- **Decision worth replaying — model-shaped vs route-shaped metadata.**
  `architecture`, `supported_parameters`, `name`, `description` and
  `top_provider.context_length` may be filled from the reference catalog because
  they describe the *model*. `pricing` and `top_provider.is_moderated` may
  **not**: they describe the *route*. OpenRouter charges $2.50/M for
  `openai/gpt-4o`; NVIDIA serves it free. A borrowed price would state something
  false in a gateway that bills real traffic, so a cloud route with no price of
  its own reports `"pricing": null`. Operator was told this is a deliberate
  choice, not an omission, and can override it.
- **Verified (observed, in-process ASGI app + real config, no service restart):**
  `/v1/models` → 288 entries / 242 cloud, 285 with `metadata_sources`;
  `openai/openai/gpt-4.1-nano` and `openai/openai/gpt-4-turbo` receive name,
  architecture, `supported_parameters` and the true `context_length`
  (1047576 / 128000) from `reference:openrouter`, while
  `openai/openai/gpt-5.4-mini-2026-03-17` — which OpenRouter also lacks — stays
  honestly all-`null`/`derived`; local models report grounded zero pricing, a
  vision model `text+image->text` and an embedding model `text->embedding`;
  `failover/free` → 7 endpoints, `failover/qwen35` → 2 local machines;
  `cloud_gateway_access: false` → 0 cloud entries, local models still visible,
  `/endpoints` returns 403. Suite: **1669 passed, 0 failed**.
- **Bug found and fixed in my own wiring — the dashboard was a second app.**
  `app/main.py` serves the dashboard (port 11437) and `app/proxy/server.py`
  serves the API (11436); they are two FastAPI apps, and `main.py` carried its
  **own copy** of `/api/cloud/catalog` that returned only
  `name/configured/model_count/addresses/last_fetch`. The enrichment went into
  `app.gateway.admin_api`, reachable only through the API app — so the
  dashboard's new catalog view could never have rendered in production, it would
  have stayed on the legacy address pills forever. Every in-process check I had
  run used `server.app`, which is exactly why it was missed; the dashboard's own
  `/api/*` surface had no coverage from my side. Fixed by delegating both
  dashboard catalog routes to `app.gateway.admin_api` (one implementation, one
  payload, both ports) and pinned with a test asserting the two surfaces return
  the **same document**, plus an explicit delegation test. `tests/unit/test_main.py`
  had pinned the *local* shape instead — which is what let the copy survive — so
  those two tests were rewritten to pin the delegation.
- **Dashboard verified in a browser against the real payload.** Served the new
  `app.main` on a spare port with a temporary harness that let the page seed its
  own key (the Guardian key never entered the transcript or a tool argument) and
  drove it with Playwright. Observed: toolbar and grid visible, legacy pill list
  hidden, 60 cards rendered with 182 provenance badges, 25 of them showing
  `⇄ ref:openrouter`. Card for `github-copilot/openai/gpt-6.1-sol`:
  `file image text` modality, `tools tool_choice reasoning reasoning_effort
  structured_outputs response_format` capabilities, `reasoning forced`, all
  `ref:openrouter`, with `ctx 1,050,000 ★ override`. Harness and spare port
  removed afterwards; production service never touched (`systemctl is-active`
  checked before and after).
  Note: the dashboard catalog covers *cloud providers* only — its
  `/api/cloud/catalog` never carried the local aliases that appear on
  `/v1/models`, so `free (local)` pricing is legitimately absent there.
- **Live HTTP verification achieved without touching the production process.**
  The operator restart had not happened, and it was not needed to obtain real
  HTTP evidence: `uvicorn app.proxy.server:app --port 11445 --lifespan off`
  serves the real API app over real HTTP while skipping startup entirely — no
  PID-file write, no caretaker calls, no catalog loop. A wrapper loaded `.env`
  first, because `app/proxy/server.py` does not (only `app/main.py` does for the
  production process) and without it every provider looks unconfigured, which
  silently yields "0 cloud models" and made the first attempt look like a bug.
  Verified against that instance: `/v1/models` → **273 entries / 227 cloud**,
  270 with `metadata_sources`, 126 with architecture; gap fill confirmed on
  `github-copilot/openai/gpt-6.1-sol` (`context_length: override` = the
  operator's 1050000, everything else `reference:openrouter`) and
  `google/google/gemini-2.5-flash` (all `reference:openrouter`);
  `/v1/models/{id}/endpoints` → 1 endpoint with `status=0` resolved from the
  live health tracker; unknown addresses → 404 on both routes. Production
  checked before and after (`guardian.pid` identical, service active, spare port
  released).
- **Full app audit after the round-3 miss.** With three FastAPI apps in the tree
  (`app/proxy/server.py`, `app/main.py`, `app/copilot_bridge.py`), every surface
  that emits an OpenAI-style model list was enumerated: only
  `model_discovery.list_models` and the copilot bridge. The bridge is a
  **deliberate non-goal** — it is the internal LiteLLM↔copilot-api protocol
  translator, its `/v1/models` is consumed by LiteLLM, and Guardian's own
  provider config explicitly says to advertise only an allowlisted subset rather
  than the bridge's whole catalog. Guardian's public surface covers those models
  richly (`github-copilot/openai/gpt-6.1-sol` carries `reference:openrouter`
  metadata). No other surface needs enrichment.
- **Contract narrowed for one honest exception.** Live output showed exactly 3 of
  288 `/v1/models` entries lacking parity keys, all `failover/*`. That was the
  deliberate round-1 choice, but spec §3 literally promised the keys on *every*
  entry, so the spec was wrong, not the code. A failover group is a route
  spanning providers: no single upstream advertisement, price or context window
  to report, and a fifteen-`null` skeleton would say nothing. §3 now states the
  carve-out and points at `/v1/models/failover/{group}/endpoints`, where each
  candidate is described properly.
- **POST-RESTART PRODUCTION CONFIRMATION (2026-10-03 18:50-18:52 UTC).** The
  operator restarted `llama-guardian`; the production process now serves the
  parity surface. Before the restart the live service was measured missing
  **every** parity field (`architecture` 0, `pricing` 0, `metadata_sources` 0)
  and `/v1/models/{id}/endpoints` did not exist. After it, against
  `http://127.0.0.1:11436`: `/v1/models` → **288 entries / 242 cloud**, **285 with
  `metadata_sources`**, **141 with architecture**; gap fill live on
  `github-copilot/openai/gpt-6.1-sol` (`context_length: override` = the
  operator's 1050000, everything else `reference:openrouter`) and
  `google/google/gemini-2.5-flash` (all `reference:openrouter`, ctx 1048576);
  `openai/openai/gpt-4o` carries the full architecture incl. `tokenizer: GPT` and
  `pricing: null`; `/v1/models/openai/openai/gpt-4o/endpoints` → 1 endpoint with
  `ctx=128000 status=0`; unknown addresses → 404 on both routes. The dashboard
  port `:11437` now returns the **same document** as the API port (12 providers,
  244 enriched models) — the round-3 duplication fix confirmed in production.
  Service health after the restart: log clean of errors, port listening,
  `/api/status` reports the loaded model, and a live 16-token completion returned
  `finish_reason: length` with usage accounting intact.
- **Performance (measured, because the payload grows a lot).** Direct handler
  timing, best-of-N, A/B against enrichment disabled by patching
  `attach_parity_metadata` to a no-op: `/v1/models` **97.6 ms / 381,914 bytes**
  enriched vs **94.5 ms** unenriched — the whole parity layer costs
  **+3.1 ms (~3%)**; the pre-existing ~95 ms is other work (local-model
  resolution, context). `/api/cloud/catalog` is **13.4 ms / ~284 KB** (10.5 KB of
  bare addresses before, so 27× the bytes for the dashboard's richer payload; the
  dashboard calls it on load and on the refresh button, not on a poll).
  Two measurement traps worth remembering: a `TestClient` request costs ~50-80 ms
  of anyio thread-portal overhead that swamps the real handler cost — an earlier
  HTTP-level comparison wrongly suggested an ~80 ms regression that does not
  exist. And 382 KB across 288 models is ~1.3 KB/model against OpenRouter's own
  ~2.8 KB/model for 466 entries, so the size is inherent to the feature and
  Guardian is still the leaner of the two.
- **Production damage found and repaired — cloud catalog disk cache.** A
  subagent's ad-hoc script built a `CloudModelCatalog` on the **default** cache
  path (its own scratch provider registry, no `cache_file` override), refreshed
  a single provider, and `_persist_cache` rewrote the whole document from that
  instance's nearly-empty `_catalogs`. It recurred during the session — ten
  providers / 308 models collapsed to one or two providers, and further rounds
  left an `openrouter` entry carrying a scratch registry's single model and a
  mismatched endpoint signature (`|/models` vs the configured `|/models/user`),
  which the reader drops — OpenRouter contributed 0 models until repaired. The
  running service was never affected (in-memory catalog
  intact); each time the disk cache was restored with a real
  `POST /api/cloud/catalog/refresh`. `_persist_cache` is now **purely additive**
  and never prunes: pruning is unnecessary because `_load_disk_cache` already
  ignores entries that are no longer enabled or whose stored `source` no longer
  matches the config. Regression coverage in
  `tests/unit/test_cloud_catalog_metadata.py`. Residual risk recorded below.
- **Residual risk (not fixed, deliberately):** the last writer still wins for a
  provider *both* processes know under the same name. A scratch process with
  scratch settings can therefore still overwrite that one provider's entry with
  a mismatched `source` (harmless beyond a failed cold start, since the reader
  drops it and the next real fetch repairs it). The alternative rule — never
  overwrite on a `source` mismatch — would let a stale entry be resurrected
  after the operator changes `catalog_url` back, which is the worse failure.
  Mitigation is process discipline: ad-hoc scripts must pass an explicit
  `cache_file` under `/tmp` and never write under the repo's `data/`.
- **Pre-existing flakes in the pre-restart gate (independent evidence, not
  caused by this work):**
  (a) `tests/unit/test_lifespan_does_not_wait_for_startup_check` fails on a clean
  `main` checkout and passes isolated;
  (b) `tests/unit/test_capture_wal_writer.py::TestWALWriterLegacyMigration::test_legacy_active_seq_persisted`
  leans on `asyncio.sleep(0.2)` as a flush deadline and failed once under load
  while passing 54/54 in isolation — worth converting to a bounded
  wait-until-flushed loop;
  (c) **order-dependent test isolation**: `app/main.py` calls `load_dotenv`
  (present at `HEAD`, untouched here), which puts real API keys into
  `os.environ`. Running `pytest tests/unit/test_main.py tests/unit/test_server.py`
  therefore fails `test_v1_post_cloud_model_without_api_key_returns_503` — that
  test expects an *unconfigured* provider, and the import of `app.main` has just
  configured it. Reversed order passes (142 passed), as does the test alone and
  as does the full suite. Only subset runs in that specific order are affected,
  but it is a trap worth knowing before blaming a change.

## 2026-10-04 — PR #28 review follow-up — DSH agent (gpt-6.1-sol)

- Scope: review on head `3b3a31c`, two blocking PR-Piet findings and one
  uncertain signature finding; six existing CodeQL log-injection threads.
- Confirmed and fixed: an empty health tracker labeled absent, disabled or
  unconfigured failover providers healthy. Only the unsupported healthy claim
  is now suppressed; candidates remain listed, and explicit degraded,
  rate-limited and authentication-error signals remain intact. Fifteen
  provider-state/health/authentication regression cases pin this distinction.
- Confirmed and fixed: display-name derivation stripped the brand from bare
  two-segment addresses. Bare and full OpenAI addresses now derive the same
  name, and `failover/free` retains its prefix. Existing `split_identity`
  behavior and advertised-name precedence are preserved.
- Refuted with real-class regression: `CloudModelCatalog.get_model_overrides`
  accepts `(identity, provider_name="")`, so both existing call forms work.
  No speculative TypeError retry was added; temporary test paths avoid live
  runtime caches.
- CodeQL findings addressed with a shared diagnostic-only control-character
  sanitizer and length limit. Regression tests cover identifiers, exceptions,
  HTTP error details and failed collaborator paths without mutating requests.
- Verification: 178 targeted tests passed; both child diffs were reviewed by
  the parent. First full gate: one known lifespan timing failure, 1712 passed.
  The failing test passed in isolation; the subsequent complete gate passed
  all checks with 1713 tests passed and 20 deselected. Ruff and diff checks
  passed. No production restart or deliberate live-cache mutation.
- Re-review on `4766567` (operator `/review` as `m0nk111`): two new findings,
  both reproduced red-first. The endpoints call passed its own request-time
  ``created`` into the presentation layer, whose caller-first priority buried
  the upstream timestamp forever (spec §3 row says upstream → reference →
  request time); the existing test missed it because the test stub orders
  upstream first. Fixed by omitting the argument; a second ``or
  int(time.time())`` in response assembly ate a legitimate ``created: 0``,
  replaced with an explicit ``is not None`` check. 186 parity-adjacent tests
  pass; full gate green. The `/v1/models` path is untouched: spec §6
  deliberately keeps the discovery entry's own ``created``.
- The first four deep `/review` runs on `5de2200` failed with no verdict:
  tier-1's model returned empty content with `finish_reason: length` on the
  ~9,800-line diff (documented on the PR). A later retry succeeded (0
  blocking). Its non-blocking finding was verified real and fixed: the
  reference-context join used the pure `split_identity`, which strips the
  brand from the two-segment `{provider}/{upstream_model}` addresses built in
  the cloud-attempt and failover-candidate paths — for providers whose own
  `/v1/models` advertises no context (openai, google, nvidia) the
  cross-provider fill silently missed and the route fell back to
  DEFAULT_CONTEXT_WINDOW. Now uses the shared `identity_key` with the known
  provider at all three call sites; red-first tests cover the seam and the
  end-to-end cloud-attempt path (128000 instead of the 131072 fallback).
- Review on `1fd56bd`: one blocking doc finding (API_REFERENCE example showed
  `metadata_sources` value `upstream`, which the presentation layer never
  records) — fixed doc-only, hunk-staged because the working tree carries
  unrelated in-progress edits from another stream. Next review round (still on
  the unreviewed `5de2200` diff, three duplicate thread instances of the same
  reference finding) led to two more verified fixes on `4df297c`'s predecessor:
  (a) the endpoints cold-start fallback could fabricate an endpoint for a model
  absent from every fetched catalog — guarded now with a new
  `CloudModelCatalog.is_provider_catalog_known` fetch-state signal; (b)
  `_as_model_data` omitted `metadata_sources` when empty, violating §5's
  always-present rule ({} never omitted) that `/v1/models` already follows —
  two tests re-pinned. CI green on the doc commit.
- Review on `791a11e`: one robustness finding verified real —
  `admin_api._build_catalog_entry` documented fail-open but left the first
  registry call unguarded, so one bad model 500'd the whole
  `/api/cloud/catalog` and the dashboard catalog stayed on "Loading…". The
  registry build is now wrapped fail-open into the documented fallback entry,
  with a red-first dashboard test asserting the other models still render.
- Next review round on `bad28ae`: one minor log-injection gap — the
  context-enrichment failure log in the same helper still passed `full_id` and
  the exception raw, inconsistent with the PR's own `clean_log_value`
  hardening. Fixed with the shared guard and a caplog regression test.
- Review via the operator's `/review2` (formal review posted on `aeec8f0`): one
  non-blocking XSS finding verified worse than UNCERTAIN — in a real browser
  the legacy address pill's inline `onclick="copyToClipboard('${escapeHtml(id)}')"`
  executed an injected id, and the copied value was even truncated at the
  injected quote: escapeHtml cannot protect a JS-string context because the
  HTML parser decodes entities in the attribute before the JS engine parses
  the string. Fixed by moving the id into `data-model-copy` on the pill and
  delegating clicks on the static `#model-cards` container to the same
  `handleModelCatalogClick` the catalog grid uses (guarded against
  double-binding). Browser red/green evidence captured with the QA hook and a
  canary `alert`; committed guard: `tests/unit/test_dashboard_ui_safety.py`
  (no `${` in any inline `onclick`; delegated copy wiring present).
