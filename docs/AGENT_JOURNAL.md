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
