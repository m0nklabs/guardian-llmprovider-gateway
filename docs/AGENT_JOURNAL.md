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
