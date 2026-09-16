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
