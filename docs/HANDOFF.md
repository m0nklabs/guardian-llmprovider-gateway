# HANDOFF — actuele status & open punten (cold file)

> **Dit is de cold file van deze repo:** agents appen hier vrij — het zit NIET
> in de DSH system prompt, dus churn hier kost geen prompt-cache. De hot file
> (`AGENTS.md`) verandert alleen in gebatchte promotie-passes (werkwijze:
> `~/.dsh/AGENTS.md` → "AGENTS.md maintenance discipline"). Afgeronde sessies
> → `docs/ARCHIVED_HANDOFFS.md`. Verplaatst uit AGENTS.md op 2026-08-30
> (two-tier werkwijze). Laatste vernieuwing: **2026-09-19** (TTS-chain live;
> gesloten secties + oude one-liners verbatim gearchiveerd, sectie
> "gearchiveerd 2026-09-19").

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
  guardian `ec1d4df`, caretaker `e1f9d48`.

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
  TTS-first** (09-16→19, guardian `ec1d4df`, caretaker `e1f9d48`) — zie
  "Actuele status" hierboven. Kernlessen in het journal: UTF-8 spawn-env
  (cp1252-crash), ensure-timeout-aritmetiek, ensure-lock, deterministische
  llama-yield, nooit handmatig llama-server killen (stale manager-state).
- **Terminal-capture contract-drift gefixt** (09-11, `5446949`) — verbatim
  gearchiveerd 09-19.
- **Nemotron "crap-outputs" verklaard + canonieke reasoning-adapter** (09-10,
  `3c0edb4`): adapter vertaalt reasoning-intent → provider-dialect (openrouter
  `reasoning.enabled=false` / nvidia-direct `enable_thinking=false`); 12 pins.
- **Legacy-config volledig opgeruimd + test-lekkage gefixt** (09-09,
  `038382b`): cloud_keys.json bleek de actieve failover-bron — gemigreerd
  (failover_groups → global.settings.yaml) toen pas verwijderd.
- **HTTP 200-garbage surfacet als 502** (09-09, `2e51140`): 200-body op
  chat-paths vereist `choices`; embedded `choices[0].error` = invalid.
- **Failover-groep `failover/free` live end-to-end** (09-09): 2
  admission/routing-gaps gefixt; groepen zijn globaal (settings.yaml).
