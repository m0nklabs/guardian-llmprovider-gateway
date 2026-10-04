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
