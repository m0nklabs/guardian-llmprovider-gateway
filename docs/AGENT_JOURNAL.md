# AGENT_JOURNAL — append-only findings-log (cold file)

> **Werkwijze:** feiten/lessen die je had moeten opgraven (reverse-engineering,
> live tests, verborgen gedrag) → hier APPEN, zelfde sessie, met datum-kop.
> Dit bestand zit NIET in de DSH system prompt → appen kost geen prompt-cache.
> De **promotie-pass** (gebatcht, zie `~/.dsh/AGENTS.md` → "AGENTS.md maintenance
> discipline") distilleert periodiek: universele feiten → regels in `AGENTS.md`;
> detail → docs/-pagina's; verouderd → wissen of archiveren.
> Nederlands is prima (operator-facing, intern).

## Batch 1 gearchiveerd (2026-08-30) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

Gedistilleerd (les → waar hij nu leeft):
- **Zoek-tool kwaliteit** (kindly-web zwak, SearXNG/Google-relay goed) → ranking-plugin + `~/.dsh/AGENTS.md`.
- **Two-tier AGENTS.md-werkwijze** → hot/cold-regels in `~/.dsh/AGENTS.md` (bron van deze pass).
- **F5-tranche-2 + caretaker-contract** (exception-taxonomie, adoptie-poll, re-bind poll) → `@docs/F5_GATEWAY_WIRING_ANALYSIS.md`.
- **OpenRouter-catalogusvelden** (modaliteiten/pricing weggegooid; dubbele fetch) → open punt in HANDOFF.
- **F6/F7-bouw + take-over V2 merge-fase** → `@docs/IMPLEMENTATION_PLAN.md` + ARCHIVED_HANDOFFS Batch 3.
- **Capture-feedback C1-C11** (schema 1.1.0, reasoning/finish_reason) → live; detail in ARCHIVED_HANDOFFS Batch 3.

## September-01-batch → gearchiveerd (voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`)

- **model-mismatch contract (09-01):** nooit stil substitueren op het lokale pad — mismatch → expliciete fout/geplande switch, geen zwijgende vervanging.
- **launcher split-brain + fail-open verificatie (09-01):** operator-melding → 3 fixes; verificatie faalt open, niet stil.
- **G3 bare-name routing hijack (09-02, pr-piet v3):** root cause + catalog-gestuurde fix (`3f981e4`).

## Batch 2 gearchiveerd (2026-09-15, onderhoudspass: 38,6 kB → dit) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

Gedistilleerde lessen uit de gearchiveerde entries (les → waar hij nu leeft):
- **Namespace-prefixes zijn claims, geen garanties** (G3, `3f981e4`): gedeelde brands tussen providers → resolutie op positief catalog-bewijs, niet op declaratievolgorde; bij config-splits de semantiek van elke resolver-regel herchecken.
- **BaseHTTPMiddleware breekt `is_disconnected()`** (G2): disconnect-detectie via raw ASGI receive (werkt door de middleware heen); watcher stoppen vóór response-send; `Task.result()` op een pending task gooit direct `InvalidStateError` — `await` de task.
- **Persist-functie met expliciete veld-lijst valt stilletjes nieuw-geadditieve velden weg** (trap-2 live-bug): persist→read roundtrip-pin is het contract.
- **Migreren vóór verwijderen** (legacy-cleanup, `aec0f7d`): cloud_keys.json leek dood maar was de actieve failover-bron — blinde rm had de capaciteit gebroken.
- **Contract-drift-tests door de ÉCHTE keten** (capture-regressie 09-11): dispatch→controller→event, niet de monkeypatch-laag; todo-lijsten verouderen — check eerst de repo-docs/verdict-tabellen (C2/C7-les).
- **Gate-hygiëne:** gate-exitcode via `${PIPESTATUS[0]}`, niet `| tail`-ketens; TOML `addopts` = gequote string; default-deselect van live integration-tests voorkomt dat de gate productie raakt (09-02).
- **Degeneratie-guard (09-02):** altijd de fundamentele (kleinste) period q bepalen — een period-p-loop is óók een 2p/3p-loop; q < 6 vrijgesteld; letter-level (q=1) bewust vrijgesteld; marker-injectie + capture-veld `degeneration_cutoff` (schema 1.2.0).
- **Restart-baseline:** na `systemctl restart` altijd MainPID == :11435-listener verifiëren (restart-race, `29953e1`); streaming-baseline (09-02-meting): TTFT ~1 s, inter-chunk p95 < 30 ms, maxGap < 400 ms — afwijkingen zijn de actionable maatstaf.
- **Nemotron-adapter (09-10, `ed3344f`):** client-intent → provider-dialect (openrouter: unified `reasoning.enabled=false`; nvidia-direct: `chat_template_kwargs.enable_thinking=false`); "crap"-signatuur = length-cut in thinking → vLLM-parser dupliceert thinking in content. Open optimalisatie: metadata-gedreven verfijning via `supported_efforts` (lightning exposeert géén efforts; super/ultra wel).
- **Live-feiten verifiëren vóór adviseren** (09-11): PR #12/#13/#14 waren al gemerged terwijl de hot-file ze nog als open zette — GitHub-API-check vóór statusclaims; hot-file-regel gecorrigeerd in deze pass.

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

## 2026-09-23 — Speech-routing route-georiënteerd (operator-mandaat): opt-in switches geschrapt — DSH agent (glm-5.3-flash) — OPEN

- **Operator-oordeel:** de dubbele opt-in (`stt/tts.cloud_forwarding.enabled` + `cloud_stt/cloud_tts`) dwaalt af naar een local/cloud-verdeling en niet-uniform gedrag. Speech moet routegericht zijn, net als chat.
- **Nieuw contract** (`app/gateway/speech_routing.py`, gedeeld door stt.py + tts.py): `model` = adres `[guardian/]{provider}/{brand}/{model}`; het providerbestand beslist alleen — `tts_url`/`stt_url` + management = lokale engine (caretaker-lifecycle), `base_url` + `api_key` = OpenAI-compatibel cloud-endpoint; beide → LAN-first. Upstream-id = `{brand}/{model}` (de #22/#23-vorm met alleen het laatste segment was feitelijk fout voor echte model-ids als `canopylabs/orpheus-v1-english`).
- **Exact-routes:** een expliciet adres levert exact dat resultaat — falen = eerlijke 502 met de routenaam, géén stille fallback naar een andere provider (zelfde principe als de chat-routering). Zonder adres = de bestaande lokale failover-keten. Onbekend/malformed adres = `404 model_not_served` (chat-contract).
- **Switches weg:** `cloud_forwarding`-blokken uit global.settings.yaml, `cloud_stt/cloud_tts` uit de providerbestanden — capability IS configuratie.
- **Pin-val:** mijn eerste handler-versie gaf een kale modelnaam (`qwen3-asr`) ten onrechte 404 en liet de format-400 vóór de route-404 gaan — de pinnen vingen het; `address_intent()` maakt guardian/-voorvoegsels altijd adres-intentie (malformed → 404) terwijl korte kale namen naar de default-keten vallen.
- 21 routepinnen (10 STT + 11 TTS) + de bestaande routepinnen; gate 5/5.

## 2026-09-23 — Speech-adresvorm gecorrigeerd: upstream-id verbatim (2-segment minimum) — DSH agent (glm-5.3-flash) — OPEN

- **Live e2e-weerlegging:** `guardian/groq/groq/whisper-large-v3` → 502 "cloud HTTP 404" — Groq's API wil KAALE ids (`whisper-large-v3`), terwijl de eerstversie het adres als `{brand}/{model}` interpreteerde. Direct bewijs: dezelfde call rechtstreeks op api.groq.com met `whisper-large-v3` → 200 + transcript.
- **De oplossing stond al in het chat-contract:** `resolve_cloud_target` accepteert `{provider}/{upstream-id}` waar de rest het ÉCHTE id is (kaal óf namespaced). Speech nagevolgd: minimum 2 segmenten (provider + id), upstream-id = alles na de provider, verbatim.
- **Provider-e2e-bewijs (live geprobeerd):** groq `/audio/transcriptions` + `whisper-large-v3` → **200** ✓; groq `/audio/speech` + `canopylabs/orpheus-v1-english` → endpoint bestaat, **terms-acceptance vereist** (org-admin, console.groq.com); openrouter → **géén speech-service** (geen /audio/speech; catalog heeft geen whisper/orpheus/grok-stt; de transcriptions-probe routet wél maar blokkeert op workspace-guardrails).
- Consequentie voor de adreslijst van de operator: de windows-gpu-local- en groq-adressen zijn echt (groq-TTS na terms-acceptance); de openrouter-speech-adressen leveren eerlijke 502's tot openrouter speech endpoints ship't (of tot een providerbestand naar een wél-servend endpoint wijst).

## 2026-09-23 — CORRECTIE: OpenRouter speech werkt wél (operator had gelijk) — DSH agent (glm-5.3-flash)

- **Weerlegging van mijn eigen conclusie hierboven** ("openrouter → geen speech-service"): FOUT. De default `/models`-lijst toont speech-modellen niet — ontdekking via `?output_modalities=transcription` en `?output_modalities=speech`. De operator wees op de docs (openrouter.ai/docs/guides/overview/multimodal/tts + /stt).
- **Live bewijs (guardian-vorm, exact):** STT multipart `mistralai/voxtral-small-24b-2507-stt` + `language=nl` → **200** `{"text":"De lama wachtte nieuwsgierig."}`; TTS JSON `qwen/qwen-audio-3.0-tts-flash` + `voice=loongjohn` + `response_format=mp3` → **200**, 92 kB mp3; **roundtrip** die mp3 terug naar de STT → 200 `"Hallo, dit is de route gerientierde speech test van Guardian."`. De catalog bevat óók `x-ai/grok-stt-1.0` ✓ en `x-ai/grok-voice-tts-1.0` (voices eve/ara/rex/sal/leo) — de adreslijst van de operator is grotendeels echt.
- **Cloud-TTS-quirks** (provider-afhankelijk, verbatim doorgelaten): `voice` is verplicht bij sommige providers en de geldige waarden staan per model in `supported_voices` (models-API); `response_format` op openrouter = mp3/pcm (géén wav). Verkeerde waarden → eerlijke 502 met de provider-reden.
- Les (weer): de modality-gefilterde catalog is de waarheid, niet de default model-lijst; en een docs-URL van de operator weegt zwaarder dan een eigen conclusie uit één probe met foute ids.

## 2026-09-25 — Caretaker VRAM-beheer uniform live: windows llama-sleep + comfy idle/wake (caretaker PR #11 → 063d419) — DSH agent (glm-5.3-flash)

- **De operator-kwestie opgelost:** "na 5 min non-gebruik moeten comfy en llama shutdownen, tot er weer een verzoek komt" — nu live bewezen op teams-host. Llama: `--sleep-idle-seconds 300` (backend-eigen sleep, `is_sleeping` in /props; de VRAM 13,2→4,3 GB na 5 min idle; een request wekt in 7,5 s). Comfy: de caretaker-idle-watcher (300 s queue-leeg → de poort-kill op 8189) + de wake-proxy op 8188 (de connect → de GPU-first-start → de TCP-pomp → 200).
- **De windows-deploy-les (volledig in de caretaker-journal):** NSSM AppEnvironmentExtra = altijd de volledige lijst expliciet (de get-based read-modify-write verloor de env 1×); de service-python (LocalSystem) ziet de --user-site van onyou niet → de deps in `.deps` + PYTHONPATH; LLAMA_SERVER_BINARY + CARETAKER_LLAMA_SLOTS_DIR-overrides hersteld; de comfy-stop = de poort-kill (schtasks /End laat het comfy-child leven); de comfy-start = GPU-first (de llama + de engines wijken).
- **TTS/STT-integriteit na de env-reconstructie:** de ensure-cycli ✓ (de TTS already_running, de STT cold_start + pid) — de gereconstrueerde commands werken.
- De comfy-startup hangt SOMETIEMS bij de herstart onder VRAM-druk (de 119k-regel-spam, de python dood) — met de GPU-vrijheid start hij schoon. Volgup-kandidaat: de comfy-start-herhaaltjes monitoren; indien structureel → de comfy-versie/de bat-lus onderzoeken.

## 2026-09-23 — Fish Audio provider (fish.audio) via speech_adapter — DSH agent (glm-5.3-flash) — OPEN

- **Operator-vraag**: de Fish Audio API-key beschikbaar maken via guardian, "puur API proxyen", route `guardian/fish.audio/`.
- **Fish is géén OpenAI-vorm** — het OpenAPI-schema (api.fish.audio/openapi.json) definieert `/v1/tts` (JSON: `text`*, `reference_id`, `format` wav/pcm/mp3/opus) → audio-bytes en `/v1/asr` (multipart: `audio`, `language`-hint) → JSON `{text, duration, segments}`; Bearer-auth.
- **Adapter, geen vork**: `speech_adapter: fish` in het providerbestand; `speech_routing` geeft hem door en de cloud-forward vertaalt per dialect — client blijft OpenAI-vorm sturen (zoals de chat-routering al doet voor nemotron-dialecten). `voice` → `reference_id` (of het route-id als er geen voice is); `response_format` → `format` verbatim (fish ondersteunt wél wav, anders dan openrouter); STT: multipart-veld heet `audio` (niet `file`) en er is geen modelveld.
- **Key-hygiëne**: key in `.env` (`FISH_AUDIO_API_KEY`), providerbestand referenceert `${FISH_AUDIO_API_KEY}` — niets secrets in de repo.
- **Live probe**: auth OK, maar **402 Insufficient API credit** — Fish rekent API-credit apart van platform-credit; operator moet bijladen op fish.audio/app/developers. Vorm- en auth-bewijs op unit-niveau (23/23 routepinnen incl. 3 fish-pinnen).

## 2026-09-24 — Fish free-tier via model-header (operator had wederom gelijk) — DSH agent (glm-5.3-flash)

- **402-correctie:** de "Insufficient API credit"-probe gebruikte de betaalde default. Fish selecteert het model via de **`model` HTTP-header** — met `model: s2.1-pro-free` ($0.00/M bytes, het gratis model) → **HTTP 200, 62 kB mp3** met dezelfde key. Geen bijladen nodig.
- **Contract-implimentatie:** het route's upstream-id ÍS het model-header (client-`fish_model`-veld wint expliciet); `voice` → `reference_id` alleen als opgegeven. ASR heeft géén gratis variant (`transcribe-1` $0.36/uur) — fish-STT-routes leveren een eerlijke 502 tot er credit is.
- Pinnen bijgewerkt op het model-header-contract (24/24).

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
