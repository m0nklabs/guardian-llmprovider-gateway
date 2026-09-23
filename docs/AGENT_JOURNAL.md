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
- **G3 bare-name routing hijack (09-02, pr-piet v3):** root cause + catalog-gestuurde fix (`7d5d32f`).

## Batch 2 gearchiveerd (2026-09-15, onderhoudspass: 38,6 kB → dit) — voltekst in `docs/AGENT_JOURNAL_ARCHIVE.md`

Gedistilleerde lessen uit de gearchiveerde entries (les → waar hij nu leeft):
- **Namespace-prefixes zijn claims, geen garanties** (G3, `7d5d32f`): gedeelde brands tussen providers → resolutie op positief catalog-bewijs, niet op declaratievolgorde; bij config-splits de semantiek van elke resolver-regel herchecken.
- **BaseHTTPMiddleware breekt `is_disconnected()`** (G2): disconnect-detectie via raw ASGI receive (werkt door de middleware heen); watcher stoppen vóór response-send; `Task.result()` op een pending task gooit direct `InvalidStateError` — `await` de task.
- **Persist-functie met expliciete veld-lijst valt stilletjes nieuw-geadditieve velden weg** (trap-2 live-bug): persist→read roundtrip-pin is het contract.
- **Migreren vóór verwijderen** (legacy-cleanup, `038382b`): cloud_keys.json leek dood maar was de actieve failover-bron — blinde rm had de capaciteit gebroken.
- **Contract-drift-tests door de ÉCHTE keten** (capture-regressie 09-11): dispatch→controller→event, niet de monkeypatch-laag; todo-lijsten verouderen — check eerst de repo-docs/verdict-tabellen (C2/C7-les).
- **Gate-hygiëne:** gate-exitcode via `${PIPESTATUS[0]}`, niet `| tail`-ketens; TOML `addopts` = gequote string; default-deselect van live integration-tests voorkomt dat de gate productie raakt (09-02).
- **Degeneratie-guard (09-02):** altijd de fundamentele (kleinste) period q bepalen — een period-p-loop is óók een 2p/3p-loop; q < 6 vrijgesteld; letter-level (q=1) bewust vrijgesteld; marker-injectie + capture-veld `degeneration_cutoff` (schema 1.2.0).
- **Restart-baseline:** na `systemctl restart` altijd MainPID == :11435-listener verifiëren (restart-race, `ec1211e`); streaming-baseline (09-02-meting): TTFT ~1 s, inter-chunk p95 < 30 ms, maxGap < 400 ms — afwijkingen zijn de actionable maatstaf.
- **Nemotron-adapter (09-10, `3c0edb4`):** client-intent → provider-dialect (openrouter: unified `reasoning.enabled=false`; nvidia-direct: `chat_template_kwargs.enable_thinking=false`); "crap"-signatuur = length-cut in thinking → vLLM-parser dupliceert thinking in content. Open optimalisatie: metadata-gedreven verfijning via `supported_efforts` (lightning exposeert géén efforts; super/ultra wel).
- **Live-feiten verifiëren vóór adviseren** (09-11): PR #12/#13/#14 waren al gemerged terwijl de hot-file ze nog als open zette — GitHub-API-check vóór statusclaims; hot-file-regel gecorrigeerd in deze pass.

## 2026-09-11 — Wakeguard-alarmen gediagnosticeerd (advies uitgebracht, geen code)

- Vals-alarm-deel: de check proeft `:11440` DIRECT — by design vaak down (idle-unload na 5 min; 6× unload / 7× reload in 14h log-bewijs) → elke sweep in een idle-window = vals "llama-server DOWN"-alarm.
- Reëel deel: cold-start reload duurt ~40,6 s (gemeten) met 2× "Caretaker /ensure transport error (recovering via adopt-poll)" + 1× 503 op :11440/v1/models — een agent31-run met client-timeout < ~45 s die in een idle-window start hangt/sterft. Recovery werkt (model komt up).
- **Advies:** wakeguard proeft via Guardian :11434 (auto-reload) i.p.v. :11440; agent31 client-timeout ≥ 60s en via Guardian routen. Optioneel vervolg: ensure-timeout-tuning (config, PR #14) dempt de transport-error-warnings bij cold start.
- Zij-notitie: AAL toont "redacted" voor workspace/project door de agentlog-privacy-redaction — het project heet dus niet echt "redacted".

## 2026-09-11 — Terminal-capture regressie gefixt (agent31 setup-session vond hem) (5446949)

- **Defect (gemeld via docs/HANDOFF.md door de Copilot setup-session — correct kanaal, correcte bevinding):** `dispatch_capture_request_completed` gaf `degeneration_cutoff=...` door aan `CaptureController.capture_request_completed`, dat de parameter niet aannam → TypeError → stil ingeslikt door de fail-open except → **alle terminal-capture events weg sinds de degeneratie-deploy**. Hard bewijs: huidige capture = 48 request_received, 0 completed/failed.
- **Waarom de gate het miste:** de degeneratie-pinnen monkeypatchten de dispatch-laag; geen test ging door de ÉCHTE controller-signature. Les: contract-drift-tests moeten de echte keten nemen (dispatch → controller → event), niet de geschminkte laag.
- **Fix:** controller-signature + schema-door wiring (integration.py); de fail-open except logt nu een content-vrije warning (geen stil verzwelgen). Regression: `test_capture_dispatch_contract.py` (5 pinnen) — echte controller, cutoff true/false/afwezig, policy-skip, en een drift-simulatie die de warning aantoont.
- **Live na deploy:** request_completed-events verschijnen weer (7 received / 2 completed direct na herstart); cutoff-veld correct afwezig bij normale completies. Gate 5/5 (eerste gate-run had een transient pytest-fail; herhaal-run groen).

## 2026-09-15 — promotion/compaction-pass (deze)

JOURNAL 38,6 kB → dit (~8 kB); verbatim → `docs/AGENT_JOURNAL_ARCHIVE.md` Batch 2 (09-01 t/m 09-10). HANDOFF 21,9 → ~9 kB, verbatim → ARCHIVED_HANDOFFS "gearchiveerd 2026-09-15". Hot-file F5-statusregel (stale PR #12) gecorrigeerd — dit was de geplande promotie-pass.

## 2026-09-11 — Fase-status gecontroleerd: F7 is feitelijk AFGEROND (stale statusregel)

- Bewijs: systemd-unit `WorkingDirectory=/home/flip/guardian-llmprovider-gateway` + `ExecStart=<new-dir>/venv/bin/python3.14 -m app.main`; proces-cwd van de draaiende MainPID = new dir; legacy `/home/flip/llama_cpp_guardian` bevroren; nginx TLS-mux live; `llama-guardian.service` = alias op de nieuwe unit. Exact de F7-acceptatie-endstate uit IMPLEMENTATION_PLAN.md.
- **AGENTS.md-faseregel ("F6 Windows/14700K + F7 cut-over → open") is stale op F7** — corrigeren in de eerstvolgende promotion-pass. Alleen F6 is nog echt open.

## 2026-09-11 — F6 gedeployed: Windows/14700K-provider live end-to-end (goal-95e50473)

- **Windows-kant (teams-host, J:\\LLMSTUFF):** llama.cpp b10964 CUDA 13.3 x64 + cudart-DLL's (RTX 5060 Ti 16GB, driver 616.92); caretaker-checkout + venv (uv, py3.12.13 — trampoline-onder-servicetrap: NSSM wijst naar de basis-python + PYTHONPATH); NSSM-service `caretaker-llamacpp` (elke parameter apart zetten — een gecombineerde `nssm set` slokt de rest op als AppDirectory-waarde); host-own models.local.settings.yaml (qwen3.5-9b: ngl 24, 16k ctx — display-GPU: desktop/games houden ~5GB vast); GGUF Q8_K_XL (12.4GB) gekopieerd van ai-kvm2; firewall 11440+11441 (LAN-subnet).
- **Drie Windows-compat-valkuilen (deployment, geen code):** (1) `--slot-save-path` viel in het systemprofile → llama-server sterft direct ("not a directory") → fix `CARETAKER_LLAMA_SLOTS_DIR=J:\LLMSTUFF\llama_slots` (env wordt call-time gelezen, paths.py:52); (2) de caretaker's health-probe naar `CARETAKER_SERVER_URL=http://0.0.0.0:11440` faalt op Windows (0.0.0.0 is op Linux een loopback-alias, op Windows niet) → probe-URL 127.0.0.1 + `extra_args: --host 0.0.0.0` in de models-file (llama.cpp: dubbele --host = last-wins, live getest); (3) de zorgvuldige diagnosticering: het child draait wél (procs=1 tijdens de 120s health-window) — de probe mislukte, niet de load.
- **Gateway-kant:** de shipped `14700k-local.settings.yaml` (F6-placeholder, met vaste markers + F6-pin-tests) geactiveerd met de echte host — NIET clobber-en-hervatten: de eerste versie zonder `local: false`/`brand: windows` brak de marker-pins. Brand-fix: `DEFAULT_BRAND_BY_PROVIDER["14700k-local"] = "windows"` (anders valt _default_brand terug op de provider-stem en verdubbelt het geadverteerde adres). Context-override-key = het VOLLEDIG geroute adres (`14700k-local/windows/qwen3.5-9b`) — de brand-normalized key loste niet op (fallback 131072).
- **Acceptatie (IMPLEMENTATION_PLAN §F6):** catalog refresh 1 model, credential_status ok; `14700k-local/windows/qwen3.5-9b` geadverteerd (283 totaal); chat non-stream `finish=stop, content="F6 works"` (reasoning 634ch in reasoning_content ✓); stream 200 SSE; capture request_completed in de WAL ✓; gate 5/5 (twee transient pytest-fails ditmaal — reruns groen; patroon bekend).
- **Model-adres:** `14700k-local/windows/qwen3.5-9b` (provider/brand/model). Model nooit handmatig op :11440 starten als de caretaker hem moet ownen.

## 2026-09-11 — F6-opvolging: api-key-hardening uitgesteld (caretaker backend-auth gap), boot-ensure-gat gedicht

- **--api-key op de Windows llama-server getest en teruggedraaid:** key-auth zelf werkt (zonder key 401, met key 200), maar de caretaker's strikte /props-verificatie krijgt 401 (probes sturen geen Bearer; alleen /health is auth-vrij) → ensure 503 model_mismatch. De caretaker is keyless-gebouwd. Handoff geplaatst in caretaker-llamacpp docs/HANDOFF.md (backend-auth-knop-aanbeveling, hun OOM-taak ongemoeid gelaten). Firewall beperkt de keyless backend tot de LAN-subnet.
- **Boot-gat gedicht:** na een Windows-reboot start de NSSM-service wél maar laadt geen model (geen auto-load-feature in de caretaker; de idle-beslissing blijft gateway-zijde en de cloud-route doet geen remote-ensure) → scheduled task `caretaker-boot-ensure` (SYSTEM, onstart): wacht op :11441/status, POST /ensure qwen3.5-9b, log naar J:\LLMSTUFF\boot-ensure-last.log. Handmatige run geverifieerd (no-op fast-path ✓).
- **Failover `local → windows`:** de plan-capability is "prepared" maar een lokale (managed) candidate door de cloud-failover-machinerie raakt het managed-provider-ontwerp — ontwerpkeuze voor de operator, niet config-gokwerk. Adres-aliasing `failover/windows` (single candidate) is wél direct mogelijk.

## 2026-09-11 — Cross-host failover live (plan §F6 'local → windows'): managed providers als failover-candidates

- **Capability:** een failover-group-candidate wiens provider `managed` is (de eigen llama-server) doet vóór de forward `caretaker_runtime.ensure_backend(model=…, local_fallback=lambda: _model_manager.switch_model(…))` — anders serveert llama-server stilletjes het toevallig geladen model (hij negeert de modelnaam in het request; de 2026-09-01 mismatch-incidentenklasse). Ensure-faal = candidate overslaan zoals elke gefaalde attempt; als ALLE candidates lokaal zijn én de ensure faalt → 503.
- **Gate-catch (bewijs dat de gate werkt):** mijn eerste hook-aanroep `ensure_backend(upstream_model)` was dubbel fout — de functie is keyword-only mét verplichte `local_fallback: Callable` (caretaker_runtime.py:667). De call-sites-gate ving het vóór de restart ("forwarding.py:394: ensure_backend missing required 'local_fallback'"). Fix: `model_manager` als DI-dep in forwarding.init + server.py-wiring; de local_fallback = de gateway's eigen switch-lifecycle (hetzelfde patroon als de hotpath-calls in gateway/routing.py:508/597).
- **Test-pollutie-les:** mijn resolutie-pinnen (routing.init met stubs) lieten de routing-globals gebroken achter voor latere test-bestanden (19 faalende in test_server/test_grammar) → autouse-fixture die exact de init-globals save/restores. De suite: 1405 groen.
- **Live proof (qwen35-groep: windows eerst, lokaal als fallback):** primary `attempt 1/2 via '14700k-local'` → 200 "failover works"; Windows-backend getaskkilld → `attempt 2/2 via 'ai-kvm2-local'` met echte model-switch (qwen3.8-27b → qwen3.5-9b; de caretaker-ensure transport-timeout werd netjes opgevangen door de F5 adopt-poll: "backend now serves 'qwen3.5-9b' — adopting loaded state") → 200 "local fallback works"; daarna beide backends hersteld (lokaal qwen3.8-27b ✓ 14,5s, windows qwen3.5-9b ✓ 15,2s) en de primary-route opnieuw bewezen.
- **Operationele kanttekening:** een lokale fallback-candidate schakelt de eigen backend naar de candidate-model (disruptief voor de lokaal geladen workload) — daarom staat windows als primary in de qwen35-groep; de volgorde is config.

## 2026-09-16 — Nieuwe TTS-provider live op teams-host: `qwen3tts-http` (:11450) — instruct-emotie via Qwen3-TTS VoiceDesign (handoff, geen code gewijzigd)

- **Wat er staat:** NSSM-service `qwen3tts-http` op teams-host (`J:\LLMSTUFF\Qwen3-TTS-GGUF`): `POST /tts {"text","instruct","seed","sub_seed","temperature",...}` → `audio/wav`; `GET /health` → status. Poort 11450, firewallregel `qwen3tts-http 11450` (LAN /24) — zelfde keyless-patroon als de caretaker-backend (11440/11441). Engine = HaujetZhao/Qwen3-TTS-GGUF: talker (q5_k GGUF, 1,0 GB) + predictor (q8_0, 151 MB) via llama.cpp b10621 CUDA; encoder/decoder via ONNX Runtime DML; model Qwen3-TTS-12Hz-1.7B-VoiceDesign (Apache-2.0). Auto-start via NSSM.
- **Bewijs (live, 2026-09-16):** /health 200; POST EN → 372 KB WAV; POST NL ("Waarschuwing: de temperatuur in de kas is te hoog...") → 299 KB WAV; LAN-POST vanaf flip → byte-identieke 372 KB (deterministisch per seed). Luisterbestanden: `J:\LLMSTUFF\Qwen3-TTS-GGUF\output\goal-test\{design_en_happy,guardian_test,guardian_test_nl}.wav`.
- **Voor guardian-integratie:** dit is géén OpenAI `/v1`-endpoint maar een eigen `/tts`-contract — opname als provider vereist een tts-capability-type (vergelijk de transcriptions-route), niet een chat-model-entry. Keyless + firewall-dekking, zoals afgesproken voor de Windows-backend. RTF ~1,1 offline per verzoek; de engine kan streaming (RTF 0,35 op een 5050) zodra de wrapper die gebruikt.
- **Belangrijke engine-bevinding:** native `llama-tts` (b10964, staat op J:\LLMSTUFF\llama.cpp) ondersteunt géén instruct-emotie — `tools/mtmd/mtmd-helper-gen.cpp` heeft geen voice-design/instruct-template, en de community-VoiceDesign-GGUF (justmaier) is structureel incompatibel (aparte text/codec-vocab vs llama.cpp' unified 155.008-vocab; metadata-patch geprobeerd en doodgelopen — zie dossier §11). De HaujetZhao-engine is de werkende route; native llama.cpp instruct-TTS wacht op een upstream PR.
- **Niet aangepast:** geen code in guardian/caretaker gewijzigd; nieuw = service + firewallregel + bestanden onder `J:\LLMSTUFF\Qwen3-TTS-GGUF` en `J:\LLMSTUFF\models\qwen3tts-upstream`. Volledig dossier + recepten: `~/onderzoek/tts-llamacpp-emotie/docs/tts-emotie-llamacpp-onderzoek.md` (§11 live-test).

## 2026-09-16 — TTS via guardian/caretaker live: /v1/audio/speech + on-demand VRAM-lifecycle

- **Guardian-kant:** OpenAI-compatibele `POST /v1/audio/speech` (route vóór de /v1-catch-all geregistreerd) → mapping `input`→`text`, `voice`→`instruct` (OpenAI-stocknamen → VoiceDesign-instructies; onbekende namen verbatim), `seed`/`sub_seed`/`temperature` passthrough; alleen `wav` (16-bit PCM mono 24 kHz). Config `tts:` in global.settings.yaml (request-time gelezen = hot-reload). Pins: `tests/unit/test_tts_route.py` (16).
- **Caretaker-kant (m0nklabs/caretaker-llamacpp `349d2ef`, nieuw `caretaker/tts.py` + routes):** on-demand lifecycle — `POST /tts/ensure` (idempotent, health-first, ververst de idle-timer), `POST /tts/release`, `GET /tts/status`; idle-watcher stopt de service na `CARETAKER_TTS_IDLE_SECONDS` (600s default). NSSM-service `qwen3tts-http` staat nu op **manual** start. Inert op Linux.
- **Live proof:** engine gestopt → VRAM 5008 MiB (desktop-baseline) → guardian TTS-request → **koude start ~6s** → 200, 184364 bytes WAV in 10,9s → release → engine 000, VRAM terug naar 5008 MiB. De idle-watcher stopt de engine automatisch na 10 min ongebruik (pin bewijst de logica; de release-path is live bewezen).
- **Les (live 401):** de `tts:`-yaml-sectie wordt RAW gelezen — de provider-registry's `${VAR}`-expansie geldt daar niet; `caretaker_key: ${WINDOWS_LAN_KEY}` bleef letterlijk → Bearer-literal → 401. Fix: `_expand_env` uit providers.py (de canonieke helper) ook in de tts-handler. Gepind.
- **Caretaker-contract-detail:** `/tts/ensure` geeft 200 + `{ok: false, reason}` bij een faalende start (het endpoint raise nooit) — de guardian checkt daarom óók de body, niet alleen de HTTP-status.
- **Onafhankelijke verificatie vanuit onderzoek-workspace (16:38 UTC, operator-opdracht "test of het via de API werkt"):** `POST /v1/audio/speech` op :11434 mét geldige Bearer-key → HTTP 200, 222.764 B WAV (voice `nova`, 4,7 s) én HTTP 200, 234.284 B WAV (expliciete emotie-instruct, 5,6 s incl. cold start — `TTS ensure: True` in het journal). 401-path bevestigd (lege key → `API Key required`). WAV's bewaard: `~/onderzoek/tts-llamacpp-emotie/files/gateway_tts_{test,emotional}.wav`.
- **Vondst — transient accept-wedge na de 16:14:56-restart (jullie basis, hercheck de next restart):** tussen ~16:28–16:34 UTC kregen NIEUW-verbindingen direct naar 127.0.0.1:11435 een instant close (curl exit 52, ~30 ms, géén journal-spoor) terwijl MainPID == listener (bekende les) én bestaande verbindingen bleven dienen; auth uitgesloten (mét geldige key zelfde gedrag). Om 16:38 diende hetzelfde pid (2638320) de geslaagde :11434-requests wél — dus self-herstellend. Aanvulling op de restart-baseline-les: verifieer na een restart niet alleen MainPID == listener, maar óók dat een NIEUWE connectie (`curl -m5 http://127.0.0.1:11435/healthz`) echt antwoord krijgt.

## 2026-09-16 — Windows-backend uitval (17:10-17:35 UTC) veroorzaakt door eigen caretaker-restart; les vastgelegd

- **Wat gebeurde:** de DSH-agent-errors ("2x achter elkaar") kwamen van requests naar `14700k-local/windows/qwen3.5-9b` terwijl de Windows llama-server down was sinds ~16:05 UTC. Oorzaak: mijn TTS-deploy herstartte de NSSM-service `caretaker-llamacpp` — **NSSM's tree-kill neemt de door de caretaker gespawnde llama-server mee bij ELKE service-(re)start** (Application-log: "Killing process tree ... for service caretaker-llamacpp" vóór de nieuwe uvicorn-start; de caretaker zelf kwam schoon terug met loaded_model=null). De boot-ensure-task firet alleen bij boot — na een handmatige restart blijft de backend dus uit totdat iemand /ensure roept. Geen request_failed-events in de capture (de directe windows-route antwoordt 502 zonder capture-faal — de failover/qwen35-groep viel netjes terug op de lokale candidate en bleef werken).
- **Healing:** /ensure → fresh_load 200 in 13s → windows-route chat 200. Catalog-fetch-waarschuwingen (elke ~90s "fetch failed, keeping last successful list") stoppen vanzelf zodra de backend leeft.
- **Les (deploy-procedure):** na ELKE `Restart-Service caretaker-llamacpp` op teams-host moet de model-ensure opnieuw draaien. Concreet: `schtasks /run /tn caretaker-boot-ensure` na de restart (dezelfde task, zelfde contract). De tweede risico-post staat los: de TTS-engine + llama-server + desktop passen samen niet in 16GB — het TTS-ensure beleid neemt geen VRAM-beslissing (zie open punt).

## 2026-09-16 — TTS provider-pariteit: caretakers identiek op Linux/Windows/cloud, engine live op ai-kvm2

- **Operator-correctie (de kern):** guardian maakt GEEN onderscheid meer tussen cloud en local — de caretaker is een provider die toevallig op de LAN draait; op een RunPod moet hij alles kunnen wat hier draait. Consequentie: (1) de caretaker-tts-lifecycle herschreven — platform-agnostisch, de caretaker spawnt/stopt de engine via `CARETAKER_TTS_COMMAND` (spawn/terminate, health-first, idle-watcher), GEEN sys.platform-gate, GEEN sc/NSSM-koppeling; de NSSM qwen3tts-http-service is nu disabled op Windows (het proces is caretaker-eigendom). (2) de guardian-speech-route is provider-gedreven: `tts_url` + `management_url` + `management_key` in de provider-bestanden, `tts.providers` = failover-volgorde — een RunPod-provider met dezelfde declaratie doet mee zonder code.
- **Engine op ai-kvm2 (Linux-port):** de runtime-set (talker q5_k + predictor q8_0 + decoder/codec fp16 ONNX + tokenizer + embeddings/ 866MB) overgehaald van teams-host; venv (py3.14-brew) + onnxruntime-gpu 1.30 + nvidia-cudnn/cublas wheels; **llama.cpp b10621 zelfgebouwd met CUDA 12.8** (sm_86;120, BUILD_SHARED_LIBS) → de .so's in `qwen3_tts_gguf/inference/bin/` (ctypes-laadmechanisme verwacht platform-specifieke libs daar). Porting-fixes: de wrapper wacht op engine.ready (anders create_stream→None op cold start), /health is eerlijk (503 "loading" tot de modellen er zijn), LD_LIBRARY_PATH (cuDNN/cuBLAS uit de wheels) via `run_http_server.sh`, en een graceful-degrade-patch in workers/speaker.py (headless hosts: geen audio-device → playback uit, worker blijft READY; de HTTP-route speelt nooit lokaal af).
- **Live proof:** guardian `/v1/audio/speech` → ensure via de lokale caretaker (idempotent ✓, cold spawn 10,3s ✓) → lokaal 200, 176KB WAV in 4,9s; failover bewezen (lokaal OOM → windows unreachable → eerlijke 502 met per-provider redenen).
- **Fysieke grens (belangrijk):** de TTS past op een host alleen als het grote model daar idle/unloaded is — de 27b (tensor-split over de 3060+5060 Ti) laat geen ~2GB vrij; de guardian-speech-test slaagde pas na `/admin/unload` van de 27b (de hotpath herlaadt hem automatisch bij het eerstvolgende chat-verzoek). De failover-route maakt dit zichtbaar in plaats van het te verdoezelen. Open keuze: q4-kopieën/VRAM-budget of de remote idle-unload (eerder voorgesteld) voor betrouwbaardere TTS-windows.

## 2026-09-16 (avond) — TTS: Windows primary live; capability-routing is configuratie, geen technische mogelijkheid

- **Operator-principe (bindend):** "mn windows pc is primary op tts, maar zo kan het ook zijn dat een specifiek model alleen maar beschikbaar is op teams-host — dat betekent niet dat het daarom niet uniform moet werken. Het moet een configureerbaar ding zijn, niet een technisch mogelijk ding, want het moet uniform technisch altijd kunnen werken." Vertaald naar het ontwerp: een provider-bestand declareert CAPABILITEITEN (chat via base_url, TTS via tts_url, beheer via management_url+management_key); de guardian-routes kiezen op volgorde uit `tts.providers` / de failover-groups. Niets in de code weet of hardcodeert welke host "de TTS-doos" is — alleen config bepaalt dat, en elke host kan elke rol.
- **Live rolverdeling (config-only):** `tts.providers: [14700k-local, ai-kvm2-local]` — de Windows-host handelt de TTS af; ai-kvm2 (productie, 27b altijd druk) is alleen fallback. E2e bewezen: koud 17,8s (ensure → de VRAM-gate stopt de Windows-llama → DML-load → WAV), warm 3,5s, beide `served by '14700k-local'`.
- **Twee bugs onderweg gevonden en gefixt:**
  1. *Stille failover door een hardcoded timeout* — de ensure-call zat op 90s terwijl de caretaker's cold start tot 240s blokkeert (de guardian time-outte vóór de engine er was en viel stilletjes terug). Fix: `tts.ensure_timeout_seconds` (default 300, boven CARETAKER_TTS_START_TIMEOUT) + pin. Les: een primary die "niet werkt" kan een timeout-arithmetiek-bug zijn, geen host-probleem.
  2. *Windows cp1252-crash* — de engine-prints (emoji) crashten met `UnicodeEncodeError` op de geredirecte stdout → de model-load stierf stil ("engine exited during startup"). Fix uniform in de caretaker: kinderen worden altijd met `PYTHONIOENCODING=utf-8`+`PYTHONUTF8=1` gespawnt (no-op op Linux). Daarnaast bindt de engine-wrapper vóór de load (waarop `/health` eerlijk 503 "loading" toont) en is `get_engine()` thread-safe.
- **Pinnen:** 14 TTS-route-pinnen (incl. ensure-timeout-config + URL-filter-fix); guardian-suite 1419 groen; pre_restart_check 5/5.

## 2026-09-16 (laat) — VRAM vol: wachten óf opgeven (configureerbaar), ensure-lock

- **Operator-directief:** "als alle VRAM al in gebruik is moet een andere request wachten of opgeven, dat mag configureerbaar zijn." Geïmplementeerd in de caretaker's VRAM-gate: `CARETAKER_TTS_VRAM_WAIT_SECONDS` (0 = direct opgeven; ai-kvm2=120, Windows=0) pollt tot de grote model-idle-unload geheugen vrijmaakt en geeft daarna eerlijk op → de guardian valt over naar de volgende provider. Het totale geduld van de guardian staat op `tts.ensure_timeout_seconds` (420) en MOET boven start-timeout + wachttijd van de traagste host zitten (240+120=360) — anders valt een gezonde primary stil weg (zelfde les als de 90s-bug eerder vandaag).
- **Concurrency-correctie erbij:** `_ensure_lock` serialiseert ensures — een request tijdens een koude start wacht en pakt daarna de healthy fast-path; nooit twee engines op één GPU. De pinnen bewijzen allebei de paden (wacht→spawn, wacht→opgeven, concurrent→1 spawn).
- **Live bewijs:** de ensure op ai-kvm2 (27b host) kwam na ~2 min terug met `cold_start: true`; guardian-speech via Windows na idle-stop: 200 in 20,5s. Suite: caretaker 128 ✓, guardian 1419 ✓.

## 2026-09-19 — STT Route 1 live: Qwen3-ASR-1.7B via sherpa-onnx op teams-host, keten-gelijkaardig aan TTS

- **Opdracht (operator via dsh-flip-onderzoek, dossier `~/onderzoek/stt-lokaal/`):** de speech-chain krijgt een invoerkant. Model `Qwen3-ASR-1.7B` int8-ONNX (community-export `solavr/...`, manifest+SHA256) via **sherpa-onnx 1.13.8** (`OfflineRecognizer.from_qwen3_asr(conv_frontend, encoder, decoder, tokenizer, provider=cpu|cuda)`); engine-sidecar `stt_http_wrapper.py` op :11451 (raw audio-bytes in → `{"text","language","seconds","elapsed_s"}` uit), alle TTS-lessen ingebouwd (bind vóór load, thread-safe lazy, honest /health, caretaker spawnt met UTF-8 env).
- **Caretaker:** `caretaker/stt.py` = gespiegelde lifecycle (ensure/release/status, VRAM-gate met wacht-of-opgeven, ensure-lock, deterministische llama-yield, UTF-8-spawn), routes `/stt/*`; 20 pinnen (mirror van TTS), suite 152 groen. Bewuste keuze: parameterized copy i.p.v. refactor — de TTS-path is productie-hot; dedup als opvolgpunt voor de caretaker-agent.
- **Guardian:** `/v1/audio/transcriptions` (OpenAI multipart) → `stt:`-config + `stt_url`-declaraties (alleen 14700k-local heeft een engine; ai-kvm2-skips automatish), ISO-639-1 → Qwen-taalnamen (nl→Dutch, en→English), taal per verzoek, ensure+failover identiek aan TTS. 8 pinnen, suite 1419+ groen, gate 5/5.
- **DoD live:** NL `{"text":"De Lama wacht nu altijd.","language":"Dutch","elapsed_s":2.19}` (3s clip); EN `{"text":"Hello. This is a speech-to-text round-trip test.","language":"English","elapsed_s":3.44}` (5.3s clip) — beide via guardian → caretaker-ensure → engine, RTFx ~1.3-1.5 op CPU (int8, 8 threads).
- **Pitfalls (de moeite van het herlezen waard):**
  1. `OfflineStream.accept_waveform(sr, data)` wil een **1-D** array — `soundfile always_2d=True` → (N,1) gooit TypeError; `.mean(axis=1).reshape(-1)`.
  2. **Firewall:** de opdracht waarschuwde er al voor — :11451 had géén inbound-regel; externe probes hingen (SYN-drop) terwijl de server lokaal `{"status":"ok"}` antwoordde. Nieuwe regel `qwen3-stt-http` (Any-profile).
  3. **Nooit engine-processen spawnen via ssh-Start-Process op teams-host** — ze verdwijnen stilletjes na korte tijd; de caretaker (NSSM-service) spawnen werkt urenlang stabiel. Dat is het ontwerp; gebruik het.
  4. `sherpa_onnx.OfflineStream` heeft géén `accept_waveform_done()` — direct `decode_stream` na accept.
  5. Qwen3-ASR taalforcering = een decode-prompt met de TAALNAAM ("Dutch", niet "nl"); auto-detect is onbetrouwbaar op synthetische audio — forceer per verzoek vanaf de client.
  6. De `tail`-pipe in de gate-chain at de exit-code — de gate kan je restart NIET blokkeren als je `cmd | tail` koppelt aan `&&`. Gebruik PIPESTATUS of laat de gate direct lopen.

## 2026-09-21 — TTS clone passthrough (council v2 integration) — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Change**: `app/gateway/tts.py` `_build_engine_payload` now passes `ref_audio`, `ref_text`
  and `language` through to the qwen3-tts engine (clone mode: voice anchor from a reference
  sample in the engine's `voice_samples/` dir). Additive only; design-mode clients (RimTalk,
  council design voices) are unaffected. Empty-string values are dropped.
- **Companion change (Qwen3-TTS-GGUF repo)**: `tts_http_wrapper.py` gained clone mode —
  `ref_audio` (filename, resolved inside `TTS_SAMPLES_DIR`, traversal-safe), optional
  `ref_text`, `language` (clone default `english`); `set_voice(sample)` + `clone()` instead
  of `design()`. `resolve_ref_audio` logic unit-checked (6/6); wrapper py_compile OK.
- **Verification**: focused pytest `tests/unit/test_tts_engine_payload.py` (3 passed) +
  full `scripts/pre_restart_check.py` ALL GATES PASSED (first run had one flaky
  test_server.py::test_lifespan_does_not_wait_for_startup_check failure — passes in
  isolation and in the full rerun; timing-sensitive test, not related to this change).
- **PENDING**: `sudo systemctl restart llama-guardian` — operator must run it (agent
  traffic routes through Guardian). Until then, clone-mode requests via
  `/v1/audio/speech` are accepted by Guardian? NO — the passthrough is in the working
  tree but NOT live until restart; clone voices fall back… they don't: Guardian live
  code strips `ref_audio` (unknown field) → engine gets design-mode request → wrong
  voice until restart. Council app handles this gracefully (tts_error surfaced).
- Consumer: `councelofdicksv2` council app — voices library `config/voices/*.json`,
  samples upload endpoint, per-participant `voice_id`, celebrity presets.

## 2026-09-22 — STT cloud forwarding + 2026-09-21 WIP rescue — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Change**: `app/gateway/stt.py` — opt-in cloud forwarding op
  `/v1/audio/transcriptions`. Dubbele opt-in (`stt.cloud_forwarding.enabled` +
  per-provider `cloud_stt: true`); model-routing via eerste padsegment
  (`groq/groq/whisper-large-v3` → provider `groq`, upstream id = laatste
  segment); `language` verbatim door (ISO-639-1, géén Qwen-mapping — die is
  engine-specifiek); cloud vóór lokaal, failures vallen door. Default OFF.
- **Verification**: `tests/unit/test_stt_cloud_forwarding.py` 7 pinnen; hele
  STT-suite 16/16; `scripts/pre_restart_check.py` ALL GATES (1438 passed, 20
  deselected). Let op: de gate liep één keer vast op de bekende flaky
  `test_lifespan_does_not_wait_for_startup_check` (timing) — herstart van de
  gate zelf was groen, geen code-oorzaak.
- **Pitfall**: `_cloud_stt_target` leest de module-global `CONFIG` — unit-tests
  moeten `stt_mod.CONFIG`/`load_stt_config` patchen vóór een directe aanroep;
  de eerste run las de échte config en faalde op de lege
  `${GROQ_API_KEY}`-expansie (geen key in het pytest-proces).
- **Rescue**: de 2026-09-21 TTS clone passthrough (`tts.py` + pins + journal)
  draaide al in productie maar was nooit gecommit — verbatim als eigen commit
  vastgelegd vóór de feature-commit, zodat een clean checkout de clone-voices
  (Sjonnie/council) behoudt.
- **STT-kwaliteitscontext**: eigen benchmark (qwen3tts-NL audio) toonde dat
  qwen3-asr en Groq whisper-large-v3-turbo identieke fouten maken op
  samengestelde woorden → de test-audio was de confounder, niet de engine;
  echte mic-audio presteert beter dan de benchmark suggereerde. Cloud-forwarding
  maakt A/B-testen op echte Discord-clips nu zero-config mogelijk.

## 2026-09-22 (II) — TTS cloud forwarding (mirror of STT) — DSH agent (openrouter/z-ai/glm-5.3-flash)

- **Change**: `app/gateway/tts.py` — zelfde dubbele opt-in als STT
  (`tts.cloud_forwarding.enabled` + per-provider `cloud_tts: true`);
  `model=cloudtts/cloudtts/orpheus-v1-english` → provider's OpenAI-compatibele
  `/audio/speech` (upstream id = laatste segment). Doorgestuurde payload =
  canonieke OpenAI-shape (model/input/voice/response_format[/speed]); de
  lokale clone-passthroughs (ref_audio/ref_text/zero_shot) en `instruct`
  reizen NIET mee naar cloud. Cloud vóór lokaal; failures vallen door;
  default OFF.
- **Review-fixes gespiegeld** (van de gemergde STT-review): `_clean_log`
  log-injectie-guard, structured client-facing failure details (geen upstream
  bodies/hosts in 502-details; volledige tekst alleen in de guarded log),
  exact-host match in test-fixtures, geneutraliseerde fixture-naming
  (`cloudtts` / `tts.cloudtest.invalid`).
- **Micro-afwijking (gedocumenteerd)**: de backends-503 verschuift ná de
  body-parse/cloud-poging — cloud-only deployments kunnen; bij switch-uit
  blijft zichtbaar gedrag voor bestaande clients identiek.
- **Verification**: `tests/unit/test_tts_cloud_forwarding.py` 8 pinnen +
  hele speech-suite 45/45 + pre_restart_check ALL GATES.
- **Pitfall (cherry-pick)**: `git status --short | head -6` knipte de
  conflictenlijst af — twee conflicterende TTS-files losten "op" tot niets en
  de commit verloor de edits stilletjes (grep-check achteraf ving het).
  Daarna handmatig her-geappliceerd vanaf origin/main. Les: bij conflicten
  ALTIJD de volledige statuslijst lezen en de commit `--stat` verifiëren.

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
- Consequentie voor de adreslijst van de operator: de 14700k-local- en groq-adressen zijn echt (groq-TTS na terms-acceptance); de openrouter-speech-adressen leveren eerlijke 502's tot openrouter speech endpoints ship't (of tot een providerbestand naar een wél-servend endpoint wijst).
