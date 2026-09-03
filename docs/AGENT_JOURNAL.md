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

## 2026-09-02 — G3: bare-name routing hijack (pr-piet bugreport v3) — root cause + catalog-gestuurde fix

- **Rapport:** `scratch/pr-piet-guardian-bugreport-2026-09-02.json` (G1-G3) + `scratch/pr-piet-capture-feedback-2026-09-02.json` (C1-C11) van de pr-piet-maintainer-agent. G3 was de reviewer-blocker: alle tier-1 review-calls faalden (litellm stopt de openai/-prefix, pr-piet kan niet prefixen).
- **Root cause (code-geverifieerd):** `z-ai/` stond als namespace-prefix in ÓÓK nvidia's model_prefixes (relikwie uit de config-split, `996b900`/`8b642ba`-tijdperk; legacy v1 settings.yaml had het niet); de registry scant `config/providers/` directory-gewijs (nvidia vóór openrouter) en nam de eerste prefix-match → NVIDIA, die het model niet serveert (catalog_allowlist: géén z-ai) → upstream 404 "page not found", en failover probeerde geen openrouter. Eigen repro vóór fix: bare → 404, `openrouter/z-ai/glm-5.3-flash` → 200 Z.AI.
- **Fix (`7d5d32f`):** catalog-gestuurde disambiguatie in `ProviderRegistry` — `set_catalog_probe()` DI (closure over `CloudModelCatalog.get_models_for_provider`, die catalog_allowlist al toepast; fail-safe try/except → False); `_get_configured_provider_for_model` verzamelt nu ALLE prefix-claimants en kiest op positief catalog-bewijs; zonder bewijs → declaratievolgorde (back-compat, nooit smallen naar None; exact-entries winnen nog altijd). Wiring op de échte constructieplek (server.py:138, niet lifespan — die houdt alleen de referentie). Config: `z-ai/` uit nvidia-prefixes + subset-comment. 9 nieuwe tests; gate 1324/3 groen; push + herstart 16:13 UTC.
- **Live bewijs na fix:** bare `z-ai/glm-5.3-flash` → 200 `"provider":"Z.AI"` in 2,5 s via de publieke mux; prefixed pad onveranderd; startup adopteerde qwen schoon.
- **Les:** namespace-prefixes zijn claims, geen garanties — zodra twee providers dezelfde brand claimen beslist declaratievolgorde, en de winnaar kan het model helemaal niet dienen. Resolutie op gedeelde namespaces moet naar de levende catalog kijken (positief bewijs), niet naar de eerste statische match. En: de config-split verhuisde een prefix-mismatch mee van v1 naar v2 waar v1 hem nog niet had — bij het splitsen van config ook de semantiek van elke resolver-regel herchecken.
- **Uit de rapporten nog open:** G2 (orphan non-stream upstream calls na client-abort — disconnect-propagatie i.p.v. cap, see PR #18-verdict; pr-piet-zijde heeft intussen max_output_tokens-mitigatie) en de resterende C-feedback-items (C1/C4/C5/C6 deels al live via schema 1.1.0 — started_at/completed_at, finish_reason, cost, completion_tokens_details zijn in de records; C2/C7-C11 tooling/retentie nog open).

## 2026-09-01 (avond) — G2: de orphan-calls keten (3 lagen) + systemd unit-les

- **G2 root cause, laag 1 (live-repro + code):** cloud-routes returnen vóór `_begin_queued_request` (routing.py-commentaar: "cloud models bypass the inference queue entirely") — geen queue-entry, geen disconnect-watcher, en de non-stream branch await `cloud_rate_limiter.execute_with_retry(...)` direct. Live-repro's: client SIGKILL +8s → upstream rende 64,4s/41,9s/40,1s voluit (3000/2000/2000 tokens), 0 disconnect-regels. Nginx-keten vrijgesproken (repro direct-TLS gaf hetzelfde); `proxy_ignore_client_abort` staat nergens aan.
- **Laag 2 (mini-tests + productie-historie):** het starlette-primitief `request.is_disconnected()` werkt in een kale uvicorn-app (plain én TLS: disconnect gedetecteerd), maar **baseHTTPMiddleware breekt het**: met `@app.middleware("http")` (de gateway's usage-tracking) vuurt polling nooit. Productie-bevestiging: de queue-watcher had **0 fires sinds 25 aug** — het lokale non-stream pad had dus dezelfde latente bug. Les: in gateway's met BaseHTTPMiddleware `is_disconnected()` niet vertrouwen — **raw ASGI receive consumeren** (werkt door de middleware heen; bewezen met mini-test 3). Veilig na body-consumptie; watcher stoppen vóór response-send (response_complete zou anders direct http.disconnect teruggeven).
- **Laag 3 (asyncio-valkuil):** `Task.result()` is géén coroutine — op een pending task gooit hij direct `InvalidStateError` (mijn eerste patch viel daar door de mand in de suite). Fallback-pad moet `await upstream_task` zijn.
- **Test-les:** contract-test `test_begin_queued_request_cleans_up_waiter_on_disconnect` pinde het oude is_disconnected-contract — dat contract was in productie dode code; test overgezet naar het receive-contract. En: gate-exitcode checken via `${PIPESTATUS[0]}`, niet via `| tail &&`-ketens (mijn gate-FAIL lekte door naar een push).
- **systemd-les:** `llama-guardian.service` is geen alias maar een aparte unit-file met eigen drop-ins — een `systemctl restart llama-guardian` start een tweede gateway; bind-race won de stray en de echte unit crashte 28× op Errno 98. Vermijd de oude naam volledig; bij twijfel `ss -ltnp` op de poorten + `systemctl show -p MainPID` van de exacte unit.
- **Observatie:** caretaker-herstart → eerste /ensure doet een volledige garantie-cyclus (~30s); curl-timeouts onder de 30s zijn daar te kort voor (twee keer op getrapt).

## 2026-09-02 — test-isolatie: de gate raakte productie via integration-tests

- **Vondst:** `tests/integration/test_live_inference.py` + `test_finetune_v2_live_smoke.py` dragen al de `integration`/`finetune_v2_live`-markers, maar pyproject had **geen default-deselect** — een plain `pytest tests/` (exact wat de pre-restart-gate draait) stuurde dus live HTTP-calls naar de productie-gateway op :11434 (incl. `/admin/load`!). Onder load kan zo'n call hangen → de gate hangt → de restart-flow stagneert. Dit verklaart de bekende "volledige suite haalt 1009/1134 niet"-milieufactor-deels ook.
- **Fix (3 regels):** `addopts = '-m "not integration and not finetune_v2_live"'` in `[tool.pytest.ini_options]` + docstring-run-instructies bijgewerkt (opt-in via CLI `-m`, die addopts overridet). 1310 passed / 20 deselected in 47 s (was ~70 s met live-probe-overhead).
- **TOML-valkuil:** `addopts = -m "..."` (ongequote waarde) is ongeldige TOML — de waarde moet zelf een string zijn: `addopts = '-m "..."'`.
- **Verificatie:** deselected-telling in de plain run + `pytest -m integration --collect-only` toont de 20; gate groen.

## 2026-09-02 (laat) — gateway restart-race: de unit kan in een self-kill crash-loop belanden

- **Vondst (twee incidenten vandaag, 21:12 en 22:09):** na een gewone `systemctl restart guardian-llmprovider-gateway.service` kan de unit in een bind-race/crash-loop belanden (NRestarts=10 resp. 44): elke nieuwe generatie sterft op `[Errno 98] address already in use` terwijl een oudere generatie-orchestrator (PPID=1, buiten de leesbare cgroup) de poorten blijft vasthouden. Symptomen: TLS 200 blijft WERKEN (iemand serveert) terwijl `systemctl show` MainPID=0/auto-restart zegt.
- **Vermoede oorzaak (≥2 bewijzen, exacte mechaniek nog open):** de gateway's eigen pid-file "stale PID file … Overwriting"-logica (`app/proxy/process.py`) en systemd's Restart=always vechten om dezelfde poorten — de journal toont bij elke generatie de stale-pid-file-waarschuwing gevolgd door status=1/FAILURE ~14 s na start; nadat de rogue handmatig gestopt werd won de eerstvolgende systemd-generatie de bind en stabiliseerde alles (MainPID == :11435-listener).
- **Herstel-procedure (bewezen, 2× vandaag):** `ss -ltnp | grep :11435` → de listener-PID die NIET MainPID is → `sudo kill <pid>` → systemd's volgende spawn wint de bind → `systemctl reset-failed` (teller wassen) → verifieer MainPID == listener-PID én TLS 200.
- **Les voor elke toekomstige restart:** na `systemctl restart` ALTIJD verifiëren dat `systemctl show -p MainPID` gelijk is aan de :11435-listener-PID; zo niet → bovenstaande procedure. Een kandidaat-fix (stale-termination alleen toepassen bij koude start, niet binnen de eerste N seconden van een systemd-gestarte generatie) is een opvolg-issue — bewust niet in deze sessie gebouwd.

## 2026-09-02 (laat) — restart-race wortel: de stale-termination herkende de productie-gateway nooit

- **Wortel-oorzaak (code-gelezen + suite-pinned):** `is_guardian_uvicorn_listener` eiste `process_name == "uvicorn"` + `"app.proxy.server:app"` + `f"--port {_proxy_port}"` in de cmdline. De productie-unit draait `python3.14 -m app.main` (comm "python3.14", poort uit `GUARDIAN_TLS_PORT`): geen van de drie matcht. De stale-listener-termination was daarmee **dode code sinds de overstap naar de -m app.main-exec** — elk bewijs eerder ("vermoedelijke pid-file-wisselwerking") was speculatief; de pid-file-`Overwriting`-regels waren gewoon correct gedrag (dode generatie-PID overschrijven).
- **Gevolg:** bij de 22:09-restart hield een orphan (PPID=1, cmdline `-m app.main`, buiten de cgroup) :11435/:11437 vast; elke systemd-generatie stierf op Errno 98 ZONDER de poort-houder te ruimen → 44× loop. Het herstel was handmatig (rogue kill → volgende generatie wint de bind).
- **Fix:** herkenning = repo-root in cmdline + onze app-module (`-m app.main` óf `app.proxy.server:app`). De process_name- en cmdline-poort-eisen vervallen (ss wordt al poort-specifiek bevraagd — de `--port`-check was overbodig én misleidend; de eigen oude fixture-cmdline bevatte zelfs `--port 11434` terwijl `_proxy_port`=11435, dus de oude check was broser dan zijn eigen test). Zelf-kill-guard en de repo-root-uitsluiting (vreemde processen nooit killen) blijven.
- **Pins:** -m app.main-orphan → getermineerd; nginx-poort-houder → nooit; eigen pid → nooit. Post-restart-verificatie (MainPID == :11435-listener) toegepast bij de deploy zelf — schoon.
- **Open restant (eerlijk):** de HERKOMST van de orphan is niet hard vastgesteld (PPID=1, buiten cgroup, startte 3 s vóór de systemd-Started — CI-runner-hypothese: de push triggert de self-hosted runner op deze host; of een resterende generatie uit de 21:12-chaos met ps-drift). De kill-loop is ermee gedempt; als er opnieuw een orphan opduikt: `journalctl` van de runner-services meenemen in de analyse.

## 2026-09-02 (nachtdienst) — pi-models opgeschoond + orphan-forensica-uitslag

- **pi `models.json` cleanup:** 216 → 99 entries tegen de live `/v1/models` gekruist. 100 legacy `guardian/...`-entries (dode route sinds de cloud-redesign) → 12 geslaagd naar live full-addresses, 88 gedropt; 44 bare-name cloud-entries → herschreven naar hun live full-addresses (bij ambiguïteit de openrouter-route geprefereerd); 29 dode entries (gedecommissioneerde lokale aliassen: ornith/ministral/gemma4-12b*/step3-vl/laguna + cloud-modellen buiten de catalogus) gedropt. Validatie: 0 `guardian/`-prefixes, alle resterende ids resolvem live. Backup: `models.json.bak-20260902`.
- **Orphan-forensica (CI-hypothese VERZWAKT):** rond de rogue-start (22:08:47) startten Python CI + CodeQL pas om **22:08:51** (trigger: de gelijktijdige push) en de CI-checkout gebruikt een ander pad dan de rogue-cmdline (`/home/flip/guardian-llmprovider-gateway/venv/...`). De rogue blijft een smal onopgehelderd raadsel (2-3 s ps/journal-drift blijft over als spoor); geen herhaling in 2 deploys sinds de fix. Kill-loop gedempt — recidive raakt de beschikbaarheid niet meer.
- **Les (herhaald):** vóór het bouwen van de C-feedback-items de verdict-tabel van PR #17 checken — C2/C7 bleken daar al (deels) onterecht met refutatie, C8-C11 al live. Todo-lijsten kunnen stinken van oudheid; de repo-docs waren correct.

## 2026-09-02 (avond) — performance-onderzoek "thinking output happert": pijplijn gezond, geen gateway-bottleneck

- **Aanleiding:** operator meldde stutter/happering in de thinking-output. Gemeten (evidence): 4 streaming-runs via het volledige operator-pad (nginx TLS :11434 → gateway :11435 → OpenRouter), ~3700 chunks totaal — TTFT 0,9–1,7 s, medianGap 0–1 ms, p95 21–30 ms, maxGap 228–380 ms, **nul gaps > 0,5 s**; 2-min-observatierun met load-sampling: idem, nul gap-events.
- **Gateway zero-overhead bewezen (bisection):** via-gateway vs direct-naar-OpenRouter, zelfde model/prompt/key: TTFT 1,07 vs 1,00 s, p95 21 vs 30 ms, maxGap 380 vs 327 ms — binnen de meetruis identiek. nginx bufferet niet (`proxy_buffering off` op de guardian-paths). Host load 11–12,7 op 24 cores (≈50%, frigate+ffmpeg+fxp-racer) — tijdens diezelfde load glad; journal 0 errors/warnings.
- **Verklaring voor de perceptie (waarschijnlijk):** (a) reasoning-modellen sturen thinking-tokens in provider-side bursts met stilte daartussen — dat is modelgedrag, geen transport-delay; (b) achtergrond-tab-throttling van de browser laat de DSH-GUI-stream happeren en in bulk uitkomen bij terugkeer; (c) incidentele host-spikes konden niet worden vastgesteld in 3 metingen.
- **Neven-bewijs (gratis):** een door het onderzoek zelf getime-outte lokale probe werd live gecanceld door de G2-fix ("🚫 Cancelled while queued (client_disconnected)") — geen orphan, queue schoon. De capture-timestamps (C1) maken het mogelijk een toekomstig stutter-moment exact forensisch te matchen: noteer de tijd, dan `capture_query.py --since ... --until ...` op de zelfde request.
- **Baseline voor later:** TTFT ~1 s (upstream-netwerk, geen gateway), inter-chunk p95 < 30 ms, maxGap < 400 ms via het volledige pad. Afwijkingen daarvan zijn de actionable maatstaf.

## 2026-09-02 (diepe nacht) — gap-vrij architectureel: volledige audit + MUST-FIX geïmplementeerd (`97de6ea`)

- **Opdracht (operator):** "modulair zonder gaps — dat er al een gap is is structureel fout." Eerste fix-rondje (`f38af54`) pakte de subprocess-sites + bouwde de structural guard; een fresh-context audit-agent classificeerde daarna ALLE blocking-sites (read-only, ≥2 bewijzen per bevinding, call-chain-traced).
- **Grootste vondst (CRITICAL-2):** de capture-WAL-rotatie gzip't tot 256 MB **sync op de event loop** — 5–15 s full-loop stall voor ALLE clients, ≥ uurlijks onder belasting. De vermoedelijke echte bron van langzamere "happer"-momenten. Nu off-loop; writer-task behoudt event-volgorde.
- **Geïmplementeerd (MUST-FIX 1–8 compleet):** WAL write/rotate/retention via to_thread (C1/C2); /metrics nvidia-smi → async + 5s TTL + snapshot-rglob off-loop (C3/M2); get_server_status ss+ps → to_thread (H1); models.yaml re-parse per lokale request → mtime-cache (H2); usage-persist 2×/request → 10s-debounce + flush() (H3); scheduler systemctl 2×30s → to_thread (M1); comfyui-URL/idle_unload_minutes/switch-allowlist mtime-caches (M3/L2/L3); auth keys mtime-cache + ss-on-401 via to_thread (M4/L1).
- **Guard uitgebreid (gat 8):** MODULES 8→19 bestanden; FORBIDDEN += os.fsync + gzip.GzipFile; 6 ALLOWLIST-entries met audit-geverifieerde redenen; `*_sync`-conventie (7 helpers). 20/20 groen op productie.
- **Lessen onderweg:** monkeypatch-tests volgen hernoemingen niet automatisch (attribuut-calls met punt ontsnapten aan de eerste regex — lookbehind-corrigendum); de usage-debounce-pin vergde een mtime-tick-sleep (nanoseconde-ticks kunnen colliden); een flaky gate-tick bij de eerste timing-pin — tweede run groen, pin daarna robuust.
- **Status:** alle client-verkeer (lokaal én cloud) deelt één loop zonder bekende blokkades meer; de guard voorkomt regressie machinaal.
