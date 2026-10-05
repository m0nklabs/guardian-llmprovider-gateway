# HANDOFF — actuele status & open punten (cold file)

> **Dit is de cold file van deze repo:** agents appen hier vrij — het zit NIET
> in de DSH system prompt, dus churn hier kost geen prompt-cache. De hot file
> (`AGENTS.md`) verandert alleen in gebatchte promotie-passes (werkwijze:
> `~/.dsh/AGENTS.md` → "AGENTS.md maintenance discipline"). Afgeronde sessies
> → `docs/ARCHIVED_HANDOFFS.md`. Verplaatst uit AGENTS.md op 2026-08-30
> (two-tier werkwijze). Laatste vernieuwing: **2026-09-19** (TTS-chain live;
> gesloten secties + oude one-liners verbatim gearchiveerd, sectie
> "gearchiveerd 2026-09-19").

## History-scrub 2026-09-24 (operator-mandate) — PROD-REDEPLOY NODIG

Volledige git-history herschreven (645 commits, filter-repo): echte LAN-IP's en
interne hostnamen → unieke letter-placeholders, repo blijft publiek. De oude
waarden staan bewust NERGENS meer in deze repo (ook niet in deze sectie).
Placeholder-rollen: `.W` = Windows-GPU-host, `.G` = gateway-host zelf, `.K`,
`.M`, `.N`, `.F` = overige LAN-machines. Providernamen: de op hardware gebaseerde
naam → `windows-gpu-local`, de op VM-naam gebaseerde naam → `ai-node-local`, de
SSH-alias van de windows-host → `windows-host`. Provider-bestanden hernoemd
(`windows-gpu-local.settings.yaml`, `ai-node-local.settings.yaml`). Bewust
behouden: `192.168.1.1` (generieke router), `10.0.0.x` + `172.16.254.1`
(testfixtures/Python-docs-voorbeeld).

**Prod-redeploy (operator, buiten agent-sessie):**
1. Bewaar EERST de echte waarden uit de huidige prod-config
   (`/home/flip/guardian-llmprovider-gateway/config/`) — die staan na de scrub
   nergens meer in git.
2. Re-clone (de history divergeert; force-push is uitgevoerd).
3. Zet de echte waarden terug op de placeholder-posities in
   `config/global.settings.yaml` + `config/providers/*.settings.yaml` — of
   breid `_expand_env` uit naar `base_url`/`*_url` en zet ze in `.env`.
4. `sudo systemctl restart llama-guardian` — knipt agent-verkeer; herstel is
   niet self-healing. Prod draait nu de fish-audio-branch (PR#27): na re-clone
   eerst #27 landen of die branch opnieuw uitchecken.

**Residu:** GitHub houdt pre-rewrite SHAs bereikbaar via PR-commitlijsten (incl.
de PR#26-dump) tot GitHub GC of support-purge — dat kan niet zelf. Alle
SHA-verwijzingen in `docs/*.md` zijn geremapt naar de nieuwe historie (65 refs,
0 oude SHAs resterend).


## Actuele status — speech (TTS) chain LIVE (2026-09-16→19, operator-directed)

De volledige spraakketen draait in productie; **capaciteiten zijn puur
configuratie, geen technische mogelijkheid** (operator-principe 09-16: een
provider die toevallig op de LAN draait moet exact hetzelfde kunnen als één op
een cloud-GPU-box):

- **Provider-declaratie:** een host doet mee aan speech-routing via drie regels
  in `config/providers/<naam>.settings.yaml`: `tts_url` (de
  qwen3tts-http-engine), `management_url` + `management_key` (de caretaker
  control API). Volgorde = failover: `tts.providers: [windows-gpu-local,
  ai-node-local]` — **Windows is TTS-first**, ai-node (productie, 27b altijd
  druk) is fallback. Guardian-docs: `docs/API_REFERENCE.md` § Speech.
- **Caretaker-lifecycle (uniform, geen platform-gate):** `CARETAKER_TTS_COMMAND`
  spawnt de engine op elke host; health-first ensure; idle-stop na 600 s;
  ensure-lock (geen dubbele spawns). Knobs: `CARETAKER_TTS_STOP_LLAMA`
  (Windows=1 → **deterministisch TTS-first**: de eigen llama-server unloadt
  vóór élke engine-start; ai-node=0 → productie-27b wordt nooit weggestopt),
  `CARETAKER_TTS_MIN_FREE_MB`, `CARETAKER_TTS_VRAM_WAIT_SECONDS` (ai-node=120:
  wachten op de idle-unload of eerlijk opgeven → failover; Windows=0),
  `CARETAKER_TTS_START_TIMEOUT`, `CARETAKER_TTS_LOG`,
  `CARETAKER_TTS_CUDA_DEVICE`. Implementatie: `caretaker/tts.py` (+19 pins).
- **Timeout-aritmetiek (valkuil):** guardian `tts.ensure_timeout_seconds: 420`
  moet ≥ `CARETAKER_TTS_START_TIMEOUT` + `CARETAKER_TTS_VRAM_WAIT_SECONDS`
  van de traagste host (240+120=360); een kortere timeout liet eerder een
  werkende primary stil wegvallen naar failover.
- **Engine (qwen3tts-http, VoiceDesign 1.7B):** op Windows via caretaker
  (NSSM qwen3tts-http = disabled), op ai-node met een zelfgebouwde
  llama.cpp-b10621-CUDA-toolchain (libs in `inference/bin/`) + wrapper-fixes:
  bind vóór de load (`/health` 503 "loading" tijdens koude start),
  thread-safe `get_engine()`, UTF-8 IO. Les die universeel geldt: de caretaker
  spawnt engine-kinderen altijd met `PYTHONIOENCODING=utf-8`+
  `PYTHONUTF8=1` (cp1252-redirect crashte de model-load middenin).
- **Bewijs:** koude start 18–20 s (incl. llama-yield), warm 3,5 s; pinnen
  guardian 14 (suite 1419) / caretaker 20 (suite 132); gates 5/5. Commits:
  guardian `1614c89`, caretaker `e1f9d48`.

### ACTUEEL (2026-09-23): speech-routing route-georiënteerd herontworpen (operator-mandaat) — opt-in switches geschrapt; adres = `[guardian/]{provider}/{brand}/{model}`, providerbestand beslist (tts_url/stt_url = lokaal, base_url+api_key = cloud), upstream-id = brand/model, expliciete route = exact (geen fallback). Zie journal + `app/gateway/speech_routing.py`.

### OPDRACHT AFGEROND (2026-09-19) — Route 1: STT live (voltekst hieronder bewaard tot de volgende compaction)

> Operator-verzoek: "bericht achterlaten voor de guardian agent dat hij route 1
> moet implementeren". Doel: de speech-chain krijgt een invoerkant — STT op de
> caretaker (windows-host), gespiegeld aan de live TTS-chain (zelfde
> provider-declaratie/caretaker-lifecycle-patroon: analoog aan `tts_url` een
> `stt_url`-declaratie en een `CARETAKER_STT_COMMAND`-lifecycle — de
> ontwerpkeuzes zijn aan de guardian). Volledige onderzoeksgrondslag, alle
> claims met bron: `~/onderzoek/stt-lokaal/docs/stt-lokaal-onderzoek.md`
> (§7 runtime-matrix, §10 aanbeveling) + journal-entry 2026-09-19 in
> `~/.dsh/docs/IDEAS_JOURNAL.md`.

**Kernfeiten (geverifieerd 2026-09-19):**
- Model: `Qwen/Qwen3-ASR-1.7B-hf` — Apache-2.0, 30 talen incl. NL, #1
  open-weights Engels (WER 4.31), NL MCV 5.43; streaming+offline in één
  checkpoint; ~2 GB VRAM int8 → past naast de TTS-engine op de RTX 5050.
- Runtime: sherpa-onnx v1.13.8+ heeft native Qwen3-ASR-**offline**-recognizers
  (VAD-gechunkt; geen continue streaming — voice-assistent-patroon); Windows
  x64/arm64-builds zijn first-class.
- Pakketten: 0.6B int8 = `csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25`
  (officiële maintainer); 1.7B int8 = community (`solavr/sherpa-onnx-qwen3-asr-1.7B-int8`,
  2026-09-13, of `thieunv-asilla/...`; beide Apache-2.0) — zelf exporteren via
  sherpa-onnx export-scripts is het fallback-pad.
- Voorgestelde poort **:11451** (symmetrisch aan :11450), firewall- /24-regel
  erbij; wrapper-patroon: `~/onderzoek/tts-llamacpp-emotie/files/tts_http_wrapper.py`
  (omgekeerde richting: WAV erin, JSON-transcript eruit).
- GGUF/llama.cpp = no-go voor Qwen3-ASR (geen ggml-implementatie; whisper.cpp
  = alleen Whisper-familie + Parakeet TDT via zelf-convert).
- Back-upmodel op dezelfde service: `parakeet-tdt-0.6b-v3` int8
  (`csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`, CC-BY-4.0,
  NL-capable, RTFx-koning).
- DSH-context: er bestaat al een lokaal-STT-plugin (`dsh-voxtype`,
  faster-whisper, PTT/hotmic) — de nieuwe engine kan daar later aan worden
  geknoopt.

**Definition of done — ALLE DRIE BEHAALD (2026-09-19, dit journal):**
1. Engine aanroepbaar via de chain (user → guardian → caretaker → engine):
   WAV → JSON {transcript, taal}; NL- én EN-test-WAV correct binnen ~2 s —
   live bewezen (NL 2,19 s / EN 3,44 s, tekst correct).
2. Recept + valkuilen → `docs/AGENT_JOURNAL.md`; status van deze sectie
   bijwerken (afgerond → archiveren per conventie).
3. Operator op de hoogte.

## Open punten (actueel — alles wat hier niet staat is afgerond; details in `docs/ARCHIVED_HANDOFFS.md`)

- **Watchdog draait nergens (handoff caretaker-agent, 09-19):** `start_watchdog()`
  wordt nergens aangeroepen — op Windows blijft een echte llama-crash
  onopgemerkt tot de volgende ensure. De bestaande `is_unloaded`-guard
  respecteert TTS-unloads, dus starten is veilig. Voelt verwant aan het
  OOM-rapportage-punt hieronder.
- **OOM-rapportage (open vraag operator, op todo):** rapporteert de caretaker
  OOM-kills terug aan Guardian? (inter-repo: m0nklabs/caretaker-llamacpp;
  raadpleeg caretaker_client/caretaker_runtime-wiring + status-endpoints).
- **Windows-llama auto-return na TTS-idle — bewust NIET gebouwd** (operator
  geïnformeerd 09-16): na een TTS-sessie komt de Windows-llama alleen via de
  boot-ensure of een handmatige ensure terug; chat valt via
  `failover/qwen35` op de lokale 27b terug. Auto-return zou een
  start/stop-cyclus worden zolang TTS prioriteit heeft — pas bouwen als de
  operator er anders over beslist.
- **Speech dagelijkse validatie (operator):** de keten is synthetisch bewezen
  (curl-cycles + pinnen); de realistische check is Open WebUI → Settings →
  Audio → TTS: engine `OpenAI`, base `http://192.168.1.G:11434/v1`, model
  `qwen3-tts`, voice `nova` → 🔊-knop per chatbericht.
- **Redactor precision audit (verzoek setup-agent 09-07, NOG TE BOUWEN):**
  `_IPV4_RE` en `_ENV_VAR_RE` in `app/capture/redactor.py` zijn categorisch te
  breed (non-secrets verdwijnen). Remedie-richting (operator-policy 09-07):
  config-gestuurde precisie — gevoelige-IP-lijst i.p.v. all-IPv4;
  env-var-redactie alleen bij bekende secret-namen/hoge entropie;
  API-key/Bearer-patronen blijven; golden tests (`$HOME` en
  default-gateway-voorbeelden passeren onredacted).
- **Caretaker generaliseren? (handoff cryptotrader 09-07, te evalueren):**
  generieke model-lifecycle-supervisor (backend-adapters) vs in-process
  oplossing van cryptotrader; her-evalueren als er een tweede non-llama
  klant is.
- **Degeneratie-guard nasleep (live sinds 09-02):** thresholds tunen via
  capture-veld `degeneration_cutoff`; letter-level-vrijstelling (q=1)
  her-evalueren zodra "aaaa"-cases opduiken.
- **Test-nasleep legacy-removal (laatst gemeten 09-09):** 9 vision-fallback-
  failures op HEAD zijn pre-existing (stash-verified);
  `test_config_reload.py::test_failover_registry_loads_proposed_groups`
  pinde de verwijderde cloud_keys.json-fallback → herschrijven/verwijderen
  bij de legacy-removal.
- **CI-adoptie (open sinds 20260813_1):** `scripts/pre_restart_check.py` als
  GitHub Action nog niet opgepakt.
- **Product roadmap:** `docs/ROADMAP.md` is the active policy/traffic-gateway
  roadmap, expanding beyond LLMs. G0 runtime-baseline reconciliation and selection
  of the first non-AI HTTP service remain open; no new runtime feature was deployed.
  Historical F6/F7 are complete (journal Batch 3; active service WorkingDirectory
  verified as the new checkout). The superseded F7-open entry is preserved
  verbatim in `docs/ARCHIVED_HANDOFFS.md`, section 2026-10-02.
- **Parked (operator-besluit 09-02, koelkast):** geheugen-idee (capture →
  agent-geheugen). Kruisbesmetting-over-projectgrenzen is dé eerste-klas eis
  in elke toekomstige uitwerking. Pas oppakken als de operator het weer op
  tafel legt.
- **Klein/deferred:** `input_modalities`-veld op /v1/models-cloud-entries;
  NVIDIA context-metadata voor de 12 werkende catalogus-entries (09-02-probe:
  81 → 12 OK / 55×404); m0nkdash-origin achter dashboard.oelala.xyz blijft
  dood; host-hygiene: crash-loop-units (vllm-bench/nervesplat/
  caramba-processor) herchecken.

## Afgerond (carry-forward one-liners; voltekst → `docs/ARCHIVED_HANDOFFS.md`)

- **Speech/TTS-chain live: provider-driven, platform-pariteit, Windows
  TTS-first** (09-16→19, guardian `1614c89`, caretaker `e1f9d48`) — zie
  "Actuele status" hierboven. Kernlessen in het journal: UTF-8 spawn-env
  (cp1252-crash), ensure-timeout-aritmetiek, ensure-lock, deterministische
  llama-yield, nooit handmatig llama-server killen (stale manager-state).
- **Terminal-capture contract-drift gefixt** (09-11, `58db7a8`) — verbatim
  gearchiveerd 09-19.
- **Nemotron "crap-outputs" verklaard + canonieke reasoning-adapter** (09-10,
  `ed3344f`): adapter vertaalt reasoning-intent → provider-dialect (openrouter
  `reasoning.enabled=false` / nvidia-direct `enable_thinking=false`); 12 pins.
- **Legacy-config volledig opgeruimd + test-lekkage gefixt** (09-09,
  `aec0f7d`): cloud_keys.json bleek de actieve failover-bron — gemigreerd
  (failover_groups → global.settings.yaml) toen pas verwijderd.
- **HTTP 200-garbage surfacet als 502** (09-09, `a09c4e3`): 200-body op
  chat-paths vereist `choices`; embedded `choices[0].error` = invalid.
- **Failover-groep `failover/free` live end-to-end** (09-09): 2
  admission/routing-gaps gefixt; groepen zijn globaal (settings.yaml).

## 2026-09-22 — STT cloud forwarding (feat/cloud-stt-forwarding) — DSH agent (openrouter/z-ai/glm-5.3-flash) — OPEN

- **Wat**: `/v1/audio/transcriptions` kan nu (opt-in) forwarden naar cloud-STT.
  Dubbele opt-in: `stt.cloud_forwarding.enabled` (global.settings.yaml) +
  `cloud_stt: true` in het providerbestand.
  `model=cloudstt/cloudstt/whisper-large-v3` → de provider's OpenAI-compatibele
  `/audio/transcriptions` (upstream model-id =
  laatste padsegment; `language` verbatim ISO-639-1 — de Qwen-naammapping is
  engine-specifiek). Cloud loopt vóór de lokale keten; elke cloud-fout valt
  terug op lokaal, ongewijzigd. Default OFF = gedrag byte-identiek aan voor.
- **Motivatie**: Sjonnie (discord_ai_person) wil whisper-large-v3 voor
  Nederlands; guardian blijft het enige controlepunt (provider-switch zonder
  de bot aan te raken).
- **Bewijs**: `tests/unit/test_stt_cloud_forwarding.py` 7 pinnen + oude
  STT-pinnen 9/9 + pre_restart_check ALL GATES (1438 passed; één flaky
  lifespan-run, bekende timing-gevoeligheid). API_REFERENCE § STT bijgewerkt.
- **Tevens gecommit**: de 2026-09-21 TTS clone passthrough WIP (verbatim, met
  pins + journal) — draaide al in productie maar was nog niet vastgelegd.
- **OPEN / operator**: (1) PR `feat/cloud-stt-forwarding` → /review-cyclus;
  (2) `sudo systemctl restart llama-guardian` (operator, snijdt agent-traffic);
  (3) enablement via hot-reload: `stt.cloud_forwarding.enabled: true` +
  `cloud_stt: true` in `config/providers/groq.settings.yaml` (GEEN restart
  nodig); (4) Sjonnie-client zet `STT_MODEL=groq/groq/whisper-large-v3`.

## 2026-09-23 — TTS cloud forwarding MERGED + beide cloud-routes live — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **PR #23 gemerged** (6799f5b) + operator-restart. Live-geverifieerd (5/5):
  STT `model=groq/groq/whisper-large-v3` → Groq (0,4 s); TTS
  `model=groq/canopylabs/orpheus-v1-english` (voice `tara`) → 223 kB 24 kHz
  WAV via Groq; geen-model → lokale engines (beide); ongeldige modelnaam →
  automatische fallback naar lokaal.
- **Enablement is LIVE maar bewust NIET gecommit**: de review heeft beide
  repo-defaults op OFF gezet ("enablement is per-deployment operator
  decision, hot-reloadable"). De productie-checkout draait met
  `cloud_forwarding.enabled: true` (stt + tts) en `cloud_stt: true` +
  `cloud_tts: true` op de groq-provider als lokale, niet-gecommitte
  deploy-state — bewust, zodat de repo de veilige default bewaart.
- **Sjonnie** (discord_ai_person): STT via guardian → Groq whisper-large-v3
  (`STT_MODEL=groq/groq/whisper-large-v3`); TTS default lokaal Trump-clone.
  Orpheus-voice `tara` geverifieerd; opt-in via
  `TTS_MODEL=groq/canopylabs/orpheus-v1-english` + preset voice.

## 2026-10-04 — OpenAI Responses API (/v1/responses) geïmplementeerd, gate groen, wacht op operator-restart — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Wat**: `POST /v1/responses` (OpenAI Responses API, o.a. Codex CLI + OpenAI SDK `client.responses.create`) nu een volwaardig inference-pad. **Lokaal**: llama.cpp (b10200) heeft het endpoint native — Guardian laat het door de gewone pipeline lopen (queue, modelresolutie/auto-switch, vision-preflight via nieuwe `input_image`-detectie, usage; usage uit geneste `response.completed`-payload; text-deltas tellen in live output-chars). **Cloud**: volledige Responses⇄chat/completions-vertaling (request, non-stream response, SSE-eventflow, errors) in `app/proxy/responses_bridge.py`, aangehaakt op dezelfde 4 punten als de Anthropic-bridge (`prepare_cloud_candidate_request`, forwarding non-stream/stream/error, `_cloud_forwarding.init`-injection). Vertaling is bewust onconditioneel voor alle cloudproviders (OpenRouter heeft wel een native route, probed 2026-10-04, maar feature-diepte ongeverifieerd en failover-groepen mixen providers) — gate: `provider_needs_responses_translation()`.
- **Semantiek**: officiële docs (developers.openai.com `.md`-referentie) + llama.cpp-converter als normatieve bron, cross-checked tegen LiteLLM/vLLM. `finish_reason` length/content_filter/refusal → `status: "incomplete"` + `incomplete_details` (streams eindigen met `response.incomplete`); tools/tool_choice flat↔nested; `text.format`→`response_format`; `max_output_tokens`→`max_tokens`; `previous_response_id`/`item_reference`/`input_file` → 400 (ResponsesRequestError, nooit upstream).
- **Bewijs**: pre_restart_check ALL GATES (1763 passed; py_compile/pyflakes/signature/call-sites PASS). Nieuw: tests/unit/test_responses_bridge.py (48 tests, incl. wiring-pin + response.incomplete-pinnen). Docs: API_REFERENCE.md (`/v1/responses`-sectie + queue-lijsten), LLM_ROUTER.md (Responses-ingress-sectie).
- **Bewust niet**: capture voor /v1/responses blijft policy-gated uit (endpoint-allowlist chat-only) — opvolgitem; statefulness (`store`/`previous_response_id`) unsupported, gedocumenteerd; OpenRouter-native-passthrough later per provider via de gate.
- **OPEN / operator**: (1) `sudo systemctl restart llama-guardian` (knipt agent-verkeer; live checklist staat in de sessie-report: non-stream + stream via lokaal, cloud-model via `/v1/responses` → verwacht vertaald antwoord, `previous_response_id` → 400); (2) commit/PR-topologie bepalen (werkboom = `feat/openrouter-model-metadata-parity` + oncommitte Responses-wijzigingen — suggestie: nieuwe branch na live-verificatie).

## 2026-10-04 (2) — Capture-dekking uitgebreid naar ALLE tekst-inference-endpoints — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Wat**: operator-mandaat "capture moet alles capteren, niet alleen chat". De OpenAI-endpoint-allowlist in `app/capture/policy.py` dekt nu `/v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings` (Anthropic/Ollama onveranderd); `/v1/embeddings` is uit `EXCLUDED_PATH_PREFIXES` gehaald (die liep vóór de allowlist en blokkeerde de dekking — rationale in de diff). Request-normalisatie naar OpenAI-messages: Responses `input`-items (`responses_capture_request`, responses_bridge), legacy `prompt` + embeddings `input` (`completions_capture_request`/`embeddings_capture_request`, redactor) — allemaal fail-open. Non-stream parsing: Responses-branch (`app/capture/responses_format.py`, nieuw: status→finish_reason-mapper + `extract_responses_semantics`) + legacy `choices[0].text`. Stream-assembler begrijpt Responses-events (text/reasoning-deltas, function_call-items, `response.completed`/`response.incomplete` → usage+finish) én completions `delta.text`. Presence-flags: Responses `text.format` telt als grammar-vector; `redact_request_parameters` stript de Responses `text`-param onder de structured_output-policy (zelfde regel als `response_format`). Cloud-capture (`setup_cloud_capture`) brancht op endpoint voor dezelfde drie. Audio (`/v1/audio/*`) bewust buiten capture (binary, geen message-model).
- **Belangrijk**: productie heeft capture op "Log EVERYTHING" (enabled, beide routes, geen allowlists) — voor deze fix verdween `/v1/responses`-verkeer dus geruisloos uit de capture. Na de restart wordt het gewoon meegepakt; geen enablement-wijziging.
- **Bewijs**: pre_restart_check ALL GATES (**1799 passed**, +36 nieuwe tests; py_compile/pyflakes/signature/call-sites PASS). Deviaties van de child-brief gedocumenteerd en gereviewd (exclusielijst, top-level imports).
- **Operator**: zelfde restart als de Responses-slice dekt ook dit; daarna kun je live controleren met `tail -f data/capture/guardian_capture_current.jsonl | jq 'select(.endpoint=="/v1/responses")'` tijdens een Responses-call.

## 2026-10-04 (3) — OpenRouter service tiers doorgelaten op alle ingress-vormen — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Wat**: operator-verzoek (openrouter.ai/docs/guides/features/service-tiers). `service_tier` (`default|flex|priority|fast|ultrafast`) rijdt nu overal mee: chat-completions en Anthropic-Messages-passthrough deden dat al (candidate-prep stript het niet); de Responses-bridge liet het vallen door de whitelist — nu doorgegeven door `translate_responses_request_to_chat`. Served-tier-rapportage: de Responses-bridge zet `service_tier` in het response-object op de tier die het verzoek écht bediende (uit de upstream chat-payload), met fallback op de gevraagde tier. `:nitro`/`:floor` model-varianten overleven modelresolutie/failover onveranderd (gepin met test). Per-model default tier kan via `service_tier` in het provider-`models:`-blok (fill-missing-mechanisme, hot-reload, expliciete clientwaarde wint). `speed` (Anthropic) heeft geen gateway-afhandeling nodig op OpenRouter-native Messages.
- **Bewijs**: pre_restart_check ALL GATES (**1809 passed**, +10: 5 bridge service-tier-tests, 3 served/echo-tests, 1 candidate-prep passthrough-pin, 1 afwezigheidstest). Docs: LLM_ROUTER.md "Service Tiers (OpenRouter)" + API_REFERENCE /v1/responses-veldnotitie.
- **Operator**: geen extra restart nodig boven de reeds geplande (deze wijzigingen zitten in dezelfde oncommitte working tree); live-check: chat-call naar een OpenRouter-model met `"service_tier":"flex"` en daarna een Responses-call idem — antwoord moet `service_tier` echoën zoals OpenRouter rapporteert.

## 2026-10-04 (4) — Standaard service tier = flex (per-provider config) — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Wat**: operator: "default moet hij op flex staan". Nieuwe provider-instelling `service_tier` (validatie: default/flex/priority/fast/ultrafast, lowercase-normalisatie, ongeldige waarde → warning + genegeerd). Injectie in `prepare_cloud_candidate_request` wanneer het verzoek geen tier heeft — precedence: client > model-default (`models:`-blok) > provider-default. Productieconfig: `service_tier: flex` op `config/providers/openrouter.settings.yaml` (kosten eerst). Andere providers krijgen GEEN tier geïnjecteerd (opt-in per provider; NVIDIA/Gemini tolerantie voor de param is niet geverifieerd). Hot-reloadbaar.
- **Bewijs**: pre_restart_check ALL GATES (**1818 passed**, +9: 4 parsing-tests, 3 injectie/precedence-tests, 1 passthrough-pin uit de vorige slice behouden). Docs: LLM_ROUTER.md (default-tier-bullet herzien), CONFIG_PROVIDER_FILES.md (provider-sleutel gedocumenteerd).
- **Operator**: config-deel is hot-reload (`POST /api/config/reload`), het code-deel zit in dezelfde geplande restart. Live-check: chat-call zonder `service_tier` naar een OpenRouter-model → antwoord moet `service_tier: flex` rapporteren; call met expliciete `service_tier: priority` → priority wint.
- **LIVE GEVERIFIEERD (2026-10-04, na operator-goedgekeurde restart door de agent)**: service terug na 3s; /v1/responses lokaal non-stream (geldig Response-object) + stream (volledige event-ladder) + `previous_response_id` → 400; /v1/responses via openrouter vertaalt correct (status/output/usage/echo); capture WAL bevat /v1/responses-events (protocol openai, model correct); flex-default: offline-beslissend (injectie + precedence tegen de echte config) én live end-to-end — `openrouter/openai/gpt-6.1-sol` zonder client-tier bediend op `service_tier: "flex"`. deepseek-v4-flash rapporteert `default` zonder client-tier: dat model heeft geen flex-endpoint, OpenRouter bedient standaard en rapporteert de geserveerde tier — correct gedrag, geen gateway-bug.

## 2026-10-04 (5) — SSE-emissiepad /v1/responses volledig live geverifieerd — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Aanleiding**: operator wees op het SSE-emissiepad als restpunt — mijn eerdere stream-check was zwak (`head -8` sloot de pipe met SIGPIPE, dus nooit terminatie gezien) en de cloud-stream was alleen unit-getest.
- **Onderzoek**: eerste schrik was een vermeende hang — directe llama.cpp-stream leek na 8 reasoning-deltas te blijven open. Ontkracht: met `chat_template_kwargs: {enable_thinking: false}` termineert de stream in 1,1s met nette `response.completed`. De "hang" was een denkende Qwen3.8 op een belaste GPU (>60s denken bij 16-token output) — modelsnelheid, geen protocolbug. Ook chat/completions deed er 21,9s over onder dezelfde last.
- **Live bewijs (alle combinaties)**: lokaal door Guardian — terminatie 9,9s, exacte officiële ladder (created→in_progress→output_item.added→content_part.added→output_text.delta×4→output_text.done→content_part.done→output_item.done→response.completed), usage 40/5/45 uit de geneste terminal-payload (dashboards krijgen tokens), capture-record compleet (streamed, tokens, finish stop, content). Cloud via openrouter — zelfde officiële ladder (10 events), terminal completed met usage 12/6/18; mét reasoning: 7× reasoning_text.delta, terminal [reasoning, message], usage 90/34, tekst correct. Client-disconnect → correct request_cancelled-record met cancel_reason.
- **Conclusie**: geen code-wijzigingen nodig; het pad was correct, de dekking van het bewijs niet. Alle /v1/responses-combinaties nu live geverifieerd.

## 2026-10-04 (6) — README.md gereconcilieerd met de running system — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Verwijderd/vervangen (stale)**: (1) copilot-bridge "pending a bridge restart" → bridge draait (LiteLLM-proces, :4142 reject unauthenticated — observed); (2) `config/settings.yaml` + `config/models.local.settings.yaml` verwijzingen (bestaan niet meer) → `config/global.settings.yaml` + `config/providers/` + `config/providers/ai-node-local.settings.yaml`; (3) backend-binaire claim b1295/cuda132-master → live build `cuda128-laguna-tq-full/build-cuda128-full` via drop-in `20-turboquant-backend.conf`; (4) backend-lifecycle via `sudo systemctl start|stop llama-server` → caretaker-llamacpp (:11441, remote-first /ensure) — `llama-server.service` observed inactive/dead terwijl het proces draait; (5) dashboard "not auth-gated" → `/api/*` vereist Bearer (`Depends(verify_api_key)` in app/main.py), localStorage `guardian_dashboard_api_key`; (6) `context_overrides` map in settings.yaml bestaat niet meer → context-window komt nu uit `models:`-blokken (context_metadata-resolutie: models-blok → cloud overrides → catalog//props → 131072 fallback); (7) queue-bullet "Single-slot FIFO" → neutrale formulering.
- **Toegevoegd**: `/v1/responses` in de featurelijst + quickstart (Responses + /v1/messages-verwijzing), capture (raw WAL + media extraction), service-tier bullet (incl. openrouter flex-default), nginx TLS-mux in topology + security, caretaker in het topologiediagram, docs-map met LLM_ROUTER/CONFIG_SCHEMA/CONFIG_PROVIDER_FILES/ANTHROPIC_BRIDGE, data/capture/ in Key Files.
- **Geverifieerd**: alle README-linkdoelen bestaan (linkcheck; `data/model_finetune_v2_results.json` is de code-default uit finetune_v2_runner.py:66 en ontstaat bij eerste run — geen fout); quickstart-alias `qwen3.6-35b-uncensored` bestaat nog in de registry (23 aliases).
- Geen code/config-gedrag gewijzigd — docs-only.

## 2026-10-04 (7) — `brand:`-key als echt configveld; 14700k-local adres-fix — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Melding operator**: geadverteerd adres `14700k-local/14700k-local/qwen3.5-9b` moet `14700k-local/qwen/qwen3.5-9b` heten. Diagnose: de catalog normaliseert bare upstream-ids via een hardcoded `DEFAULT_BRAND_BY_PROVIDER` in cloud_catalog.py; `14700k-local` stond er niet in → brand viel terug op de providernaam → verdubbeling. De `brand: windows`-key in de provider-file werd **nooit gelezen** (dode config), en de `models:`-override-keys (`14700k-local/windows/...`) matchten de lookup-vorm nooit (dode context-overrides).
- **Fix**: (1) `CloudProvider.brand`-veld + loader-parse (lowercase, één segment, invalide → warning + None) in providers.py; (2) `_default_brand` geeft de file-brand voorrang boven de map (configuratie boven code; windows-gpu-local `brand: windows` = zelfde waarde als de dict → geen gedragswijziging); (3) 14700k-local.settings.yaml: `brand: qwen` (modelmaker per naming-contract) + override-keys → `qwen/qwen3.5-9b`, `qwen/Huihui-Qwen3.5-9B-abliterated`; (4) stale genormaliseerde cache-entry `14700k-local` uit data/cloud_catalog_cache.json verwijderd (cache slaat genormaliseerde ids op; raw id `qwen3.5-9b` blijft de forwarding-vorm).
- **Geverifieerd**: offline tegen de echte productieconfig (brand parsed `qwen`, genormaliseerd `qwen/qwen3.5-9b`), 3 nieuwe tests in TestProviderBrandConfig, gate 1826 tests ALL PASSED. Live verificatie (/v1/models + context 16384) na de restart.
- Open: herstart van guardian-llmprovider-gateway vereist (code-wijziging); daarna live check `14700k-local/qwen/qwen3.5-9b` in /v1/models + 404 op de oude verdubbelde vorm.
