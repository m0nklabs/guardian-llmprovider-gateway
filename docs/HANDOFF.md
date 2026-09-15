# HANDOFF — actuele status & open punten (cold file)

> **Dit is de cold file van deze repo:** agents appen hier vrij — het zit NIET
> in de DSH system prompt, dus churn hier kost geen prompt-cache. De hot file
> (`AGENTS.md`) verandert alleen in gebatchte promotie-passes (werkwijze:
> `~/.dsh/AGENTS.md` → "AGENTS.md maintenance discipline"). Afgeronde sessies
> → `docs/ARCHIVED_HANDOFFS.md`. Verplaatst uit AGENTS.md op 2026-08-30
> (two-tier werkwijze). Laatste compaction-pass: **2026-09-15** (21,9 kB → dit;
> verbatim archief in ARCHIVED_HANDOFFS, sectie "gearchiveerd 2026-09-15").

## Open punten (actueel — alles wat hier niet staat is afgerond; details in `docs/ARCHIVED_HANDOFFS.md`)

- **Redactor precision audit (verzoek setup-agent 09-07, guardian-scope, NOG TE BOUWEN):** twee patronen in `app/capture/redactor.py` zijn categorisch te breed: `_IPV4_RE` op alle tekst (LAN-configs/codevoorbeelden → `[REDACTED_IP]`) en `_ENV_VAR_RE` = elke `$UPPERCASE` → `[REDACTED_ENV_VAR]` (`$HOME` is geen secret). Remedie-richting (operator-policy 09-07: "secrets out = good; non-secret data dropped = must fix"): config-gestuurde precisie — (a) gevoelige-IP-lijst (home WAN + LAN-subnet) i.p.v. all-IPv4; (b) env-var-redactie alleen bij bekende secret-namen/hoge entropie; (c) API-key/Bearer-patronen blijven; (d) golden tests: `$HOME` in een bash-snippet en default-gateway-voorbeelden passeren onredacted, echte keys worden nog steeds gevangen. Capture-gaten door guardian's eigen secret-filtering zijn BY DESIGN; over-filtering van non-secrets is de fix.
- **Caretaker generaliseren? (handoff cryptotrader 09-07, te evalueren door deze repo):** caretaker beheert nu llama.cpp/GGUF-processen met een OpenAI-style contract; TimesFM (torch, 1468 MiB resident voor ~1 forecast/uur) past daar niet in. Overweging: generieke model-lifecycle-supervisor (backend-adapters declareren start/health/unload; callers krijgen ensure/queue/idle-unload). Tot die tijd lost cryptotrader het in-process op (lazy load + idle unload, eigen PR) — geen afhankelijkheid van guardian/caretaker. Eerste kandidaat-customer: cryptotrader.
- **Degeneratie-guard open nasleep (live sinds 09-02, schema 1.2.0, guard + marker + capture-veld):** thresholds tunen op echte degeneratie-cases via capture-veld `degeneration_cutoff` (monitoring); letter-level-vrijstelling (q=1) her-evalueren zodra "aaaa"-cases opduiken. Deels open vraag (setup-agent 09-08): bredere OpenRouter `reasoning`-parameter-forwarding voor reasoning-modellen — deels beantwoord door de nemotron-adapter (journal 09-10); metadata-gedreven verfijning (alleen vertalen bij modellen zonder `supported_efforts`) staat open als catalog-optimalisatie.
- **OOM-rapportage (open vraag operator, op todo):** rapporteert de caretaker OOM-kills terug aan Guardian? (inter-repo: m0nklabs/caretaker-llamacpp; raadpleeg caretaker_client/caretaker_runtime-wiring + status-endpoints).
- **Test-nasleep legacy-removal (laatst gemeten 09-09):** 9 vision-fallback-failures op HEAD zijn pre-existing (stash-verified: falen met én zonder de failover-commits) — op te pakken bij de in-flight legacy-removal; `test_config_reload.py::test_failover_registry_loads_proposed_groups` pinde de verwijderde cloud_keys.json-fallback (by-design conflict; regel ~81 gebruikt nog tmp cloud_keys.json) → herschrijven/verwijderen bij diezelfde legacy-removal.
- **CI-adoptie (open sinds 20260813_1):** `scripts/pre_restart_check.py` als GitHub Action nog niet opgepakt.
- **Parked (operator-besluit 09-02, koelkast):** geheugen-idee (capture → agent-geheugen; stap 1 FTS/SQLite-index, stap 2 semantische embeddings). Bezwaar van de operator: kruisbesmetting over projectgrenzen — per-project scoping/key-isolation is eerste-klas eis in elke toekomstige uitwerking, niet een optie. Pas oppakken als de operator het weer op tafel legt.
- **Klein/deferred:** `input_modalities`-veld op /v1/models-cloud-entries (discovery-metadata, klein vervolgdeel); NVIDIA-bruikbaarheidskaart (09-02, `scratch/nvidia_probe_results.json`): 81 geprobed → 12 OK / 55×404 (68% dode catalogus-entries) / 11×timeout — context-metadata vereist model-card-werk voor de 12 werkende; m0nkdash-origin achter dashboard.oelala.xyz blijft dood (raakt Guardian niet); host-hygiene: crash-loop-units (vllm-bench/nervesplat/caramba-processor) herchecken — spawnerden tientallen processen/min op 09-02.

## Terminal-capture regressie GEFIXT (2026-09-11 — gevonden door de agent31 setup-session via deze handoff: correct kanaal, correcte bevinding)

- Defect: `capture_request_completed(..., degeneration_cutoff=...)` → controller had de parameter niet → TypeError → fail-open swallow → **0 terminal-capture-events sinds de degeneratie-deploy** (hard bewijs: 48 request_received, 0 completed/failed).
- Fix: controller-signature + schema 1.2.0-wiring (`integration.py`); fail-open except logt nu een content-vrije warning. Regression: `tests/unit/test_capture_dispatch_contract.py` (5) over de ÉCHTE keten (dispatch→controller→event). Gate 5/5; live verschijnen events weer. Les: contract-drift-tests door de echte controller, niet door de geschminkte dispatch-laag. Fix-commit: `5446949`.

## Afgerond (carry-forward one-liners; voltekst → `docs/ARCHIVED_HANDOFFS.md`)

- **Terminal-capture contract-drift gefixt** (09-11, `5446949`) — zie sectie hierboven.
- **Nemotron "crap-outputs" verklaard + canonieke reasoning-adapter** (09-10, `3c0edb4`): length-cut in thinking → vLLM-parser dupliceert thinking in content; adapter vertaalt intent → provider-dialect (openrouter `reasoning.enabled=false` / nvidia-direct `chat_template_kwargs.enable_thinking=false`); 12 pins, end-to-end bewezen.
- **Legacy-config volledig opgeruimd + test-lekkage gefixt** (09-09, `038382b`): local_models.yaml-symlink weg; **cloud_keys.json bleek de actieve failover-bron** — eerst gemigreerd (failover_groups → global.settings.yaml), toen pas verwijderd; failover.py leest uitsluitend settings.yaml.
- **HTTP 200-garbage surfacet als 502** (09-09, `2e51140`): `_detect_upstream_invalid_response` — 200-body op chat-paths vereist `choices`; embedded `choices[0].error` (OpenRouter-vorm) = invalid; embeddings uitgezonderd.
- **Failover-groep `failover/free` live end-to-end** (09-09, operator-besluit: groups zijn globaal → settings.yaml): 2 admission/routing-gaps gefixt (chat-admissie + `is_cloud_or_guardian_route`); live `failover/free` → 200 in 0,85 s op nemotron-3-super terwijl lightning/lagunas degraded waren.
- **Degeneratie-guard + marker-injectie LIVE** (09-02, schema 1.2.0): server-side cutoff van repetition-loops (fundamentele period q; q<6 vrijgesteld; letter-level bewust vrijgesteld), leesbare slot-marker als laatste content-delta, kill-switch `degeneration.enabled: false`.
- **Catalogus-consolidatie trap 1 + trap 2 AFGEROND** (09-02): één bron/één TTL (dubbele /models-fetch weg); modaliteiten + context bewaard in de single fetch; persist-subset-bug gevonden+gefixt (`1b9493b`); beide traps gesloten.
- **Gap-vrij architectureel** (09-02, `97de6ea`/`f38af54`): WAL-rotatie gzip'd sync op de event loop (5–15 s stall!) → to_thread; MUST-FIX 1–8 compleet; structural guard 19 modules; via-gateway p95=2 ms, 0 gaps>0,5 s.
- **Restart-race gefixt** (09-02, `ec1211e`): listener-herkenning herkent `python3.14 -m app.main` weer; post-restart-verificatie (MainPID == listener) is standaard-procedure.
- **G2 orphan-calls gefixt** (09-01, `3fa1479`/`f2d4d9f`/`6db7f5b`): raw-ASGI receive-watchers (cloud + queue), 499-contract — live bewezen (0 tokens verbrand).
- **Model-mismatch contract gefixt** (09-01, `ba9467e`): 503 `model_switch_failed` op elk lokaal entry-pad; /ensure fail-closed + retry (PR #9/#10, `ba866ff`/`f0bdeb6`).
- **G3 bare-name routing hijack gefixt** (09-02, `7d5d32f`): catalog-gestuurde disambiguatie; `z-ai/` uit nvidia-prefixes — live bewezen.
- **Test-isolatie** (09-02, `7db5ba3`): 20 live integration-tests default gedeselecteerd; gate raakt productie nooit meer.
- **Streaming-teardown pin** (09-02, `f61c2f1`): client-disconnect tijdens write = `request_cancelled`/`client_disconnect`.
- **ensure_fresh gewired** (09-02, `7f777a5`): /v1/models triggert ensure_all_fresh; /ensure transport-error → WARNING; live 272 modellen.
- **pi-models opgeschoond** (09-02): 216→99 entries, alle resolvem live; backup `models.json.bak-20260902`.
- **UNIT-VALKUIL opgelost** (09-02): `llama-guardian.service` is nu een symlink op de echte unit (aparte file + drop-ins bewaard als `.disabled-20260902`) — restart via alias veilig.
- **`GUARDIAN_STARTUP_ADOPT_ONLY=1` verwijderd** uit beide unit-files; startup-heal weer volledig actief.
- **Nul-delta-meting + gap-vrij architectureel bewezen** (09-02): pijplijn gezond; baseline TTFT ~1 s, inter-chunk p95 < 30 ms, maxGap < 400 ms.
- **C-feedback dossiers volledig afgehandeld** (PR #17, `ef483dd`): C2/C7-refutaties, C8-C11 live; onafhankelijk herverifieerd.
- **72h soak AFGESLOTEN** (09-02, bewijs): capture ~26 dagen live; 41.044 events; 169/172 bestanden gezond, 0 parse-falen; 3 truncaties = crash-slachtoffers (niet meer reproduceerbaar). Policy 1.1.0 actief.
- **NVIDIA bruikbaarheidskaart GEMETEN** (09-02, volledige probe): 81 modellen → 12 OK / 55×404 / 11×timeout / 2×500 / 1×400; metadata-vondst: NVIDIA's /models geeft géén context_length.
- **Orphan-herkomst forensica AFGEROND op beste verklaring** (09-02): verzadigde systemd job-wachtrij tijdens de guardian-transitie (vllm-bench 53.640 herstarts, nervesplat 52.311); inter-repo-handoffs naar caramba + nervesplat; risico gedempt.
- **Geheugen-idee IN DE KOELKAST** (09-02, operator-besluit) — zie parked-punt hierboven.
