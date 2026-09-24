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
- **F7 cut-over (masterplan):** F6 (Windows/homelab-provider incl. TTS) is nu
  voltooid; F7 (formele cut-over) staat nog open in
  `docs/IMPLEMENTATION_PLAN.md` / issue #1.
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
