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

## 2026-09-02 (nachtdienst-2) — operator-besluiten: NVIDIA-probe, orphan-forensica, consolidatie-plan

- **NVIDIA volledige probe (operator-keuze "2"):** 81 modellen, 53 s — 12 OK / 55×404 / 11×timeout / 3×5xx. De 68% dode entries zijn een NVIDIA-gefailing. Geen context-metadata in de catalog → "max context" wacht op model-card-keuzes voor de 12 werkende.
- **Orphan-forensica (gebonden pass, AFGEROND):** 22:08:47-venster = wekenlange host-chaos (vllm-bench 53.640 restarts, nervesplat 52.311 op exact dat tijdstip). Beste verklaring cgroup-escape: verzadigde systemd-wachtrij. Handoffs naar caramba + nervesplat; raadsel gesloten op beste verklaring.
- **Consolidatie:** twee-traps plan vastgelegd per operator — trap 1 consolidatie, **trap 2 modaliteiten verplicht daarna**.
- **Host-hygiene:** de 5 crash-loop-units spawneren nog steeds tientallen processen/min — hercheck over een week.

## 2026-09-02 — promotion/compaction-pass (budget-werkopdracht zelf uitgevoerd)
HANDOFF 22.7→11.9 kB: 24 afgeronde verhaal-blokken → docs/ARCHIVED_HANDOFFS.md (archive, never destroy); actuele status + open punten blijven. JOURNAL 18.0 kB (binnen budget). Geheugen-idee geshelved met operator-bezwaar (kruisbesmetting over projectgrenzen) vastgelegd in HANDOFF.

## 2026-09-02 — compaction-regel verfijnd (operator): archiveer eerst verbatim, componeer dan LICHT
De eerste pass verving 24 entries door één bulk-zin — te grof. Nieuwe regel (vastgelegd in dsh-config-private ): archiveer verbatim, daarna per-entry one-liner (essentie + refs) in de handoff. Retroactief toegepast: HANDOFF 13.3 kB met lichte lijst.

## 2026-09-02 — Degeneratie-guard gebouwd (operator-feature: herhalende tokenoutput afkappen)

- **Feature:** bij een repetition-loop in modeloutput kapt Guardian de stream server-side af met een gesynthetiseerde standaard `finish_reason: "length"` (OpenAI) / `done_reason: "length"` (Ollama) — de harness kan met ruime `max_tokens`-budgetten werken zonder dat degeneratie die verbrandt. Cloud (streaming via `_read_sse_lines()` — één detector voedt óók de Anthropic-vertaler) + lokaal (chat én generate) + non-stream (payload-truncatie, één loop-instantie blijft staan).
- **Kernles (harmonische matches):** een period-p-loop is óók een 2p/3p-loop; period-floors helpen niet tegen korte echo's ("ha "×200 matcht period 6). Fix: altijd de **fundamentele (kleinste) period q** bepalen; q < min_period (6) → vrijgesteld. Bewuste trade-off: letter-level loops (q=1, "aaaa...") zijn vrijgesteld (niet te onderscheiden van scheidingslijnen/fills); woord-/zin-level loops (het echte LLM-degeneratie-venster) worden gevangen. Deliberate, gedocumenteerd in tests.
- **Config:** `degeneration:` in global.settings.yaml (enabled/min_period/max_period/min_repeats/min_repeat_bytes=240/max_window_chars=4096), mtime-gated cache; kill-switch = `enabled: false`.
- **Capture:** additief veld `degeneration_cutoff: true` op request_completed, schema 1.2.0; streaming-dispatch krijgt de vlag via `_dispatch_capture_stream_completed(degeneration_cutoff=...)`.
- **Pins:** 17 in tests/unit/test_degeneration.py (detector-math, false-positives: ha/OK/listen/code, kill-switch, feed-midstream, config-loader, schema-veld, **full-wiring stream-cut**: degenererende SSE → `finish_reason:"length"`+`[DONE]`+capture-vlag). Gate-pak de versie-pin (1.1.0→1.2.0 bijgewerkt).

## 2026-09-02 — Degeneratie-guard: leesbare marker-injectie toegevoegd (operator-verzoek)

- **Vraag:** "wordt er een bericht meegestuurd als injectie aan het einde van de output, zodat de lezer weet dat er iets mis is gegaan?" — antwoord was: nee, alleen machine-signalen (`finish_reason: length` + capture-veld). Nu wel: bij cutoff injecteert Guardian als **laatste content-delta** `\n\n[guardian: generation cut off - repetition loop detected]` vóór de finish-chunk.
- **Dekking:** cloud-streaming (marker-delta loopt via de bestaande Anthropic-vertaler automatisch mee als content_block_delta), ollama chat (`message.content`-chunk) en generate (`response`-chunk), en non-stream (append na truncatie). Capture-cloud neemt de marker mee in response_content; de machine-vlag blijft `degeneration_cutoff: true`.
- **Config:** `degeneration.marker_enabled` (default true) + `degeneration.marker_text` (overridebaar); lege text of false = uit. 21 pins (4 nieuw), gate groen.

## 2026-09-02 — Catalogus-consolidatie trap 1 afgerond (één bron, één TTL, dubbele fetch weg)

- **De dubbele fetch (archief-les §2026-08-30 gefixt):** `ProviderRegistry._get_context_catalog` (eigen TTL + eigen httpx-fetch van dezelfde `{base_url}{catalog_url}`-endpoint, alleen voor context_length/max_input_tokens) is **verwijderd**. `CloudModelCatalog.refresh_provider` bewaart nu per model óók de context-map (key-space onveranderd: `canonical_model_id(raw_id)` — exact de oude extractor-sleutelruimte), en de registry leest via `set_context_catalog_lookup(cloud_catalog.get_context_window)` (DI-binding in server.py, `set_catalog_probe`-patroon).
- **Contracts behouden:** overrides winnen boven upstream-data; per-key credential-headers op de (enkele) fetch; failed refresh houdt de vorige catalog incl. context; catalog_url-respect; backward-compat disk-cache ("context" ontbreekt → None, geen crash). Bewust GEEN allowlist-filter op context (de oude fetch filterde ook niet).
- **Gedragsnance:** vóór de eerste catalog-refresh (koude instantie) levert context-lookup None → bestaande fallback-keten (131072 + warn). Startup draait `ensure_all_fresh`, dus warm binnen enkele seconden.
- **Eén TTL over:** de catalog-TTL (3600s, ensure_fresh-gated). REGISTRY-side: ContextCatalog/locks/_extract_context_windows/asyncio/httpx-imports weg — providers.py doet geen HTTP meer.
- **Pins:** 44/44 test_providers (4 context-pins herleid naar catalog-niveau + 3 nieuwe: unbound-None, oude-cache-compat, key-space-equivalentie) + suite groen + gate 5/5.
- **Trap 2 (modaliteiten-extractie) blijft verplicht vervolg** — nu is de basis daarvoor gereed (één fetch die alle velden kan bewaren).

## 2026-09-02 — Consolidatie live-verificatie: 229 fallback-warnings zijn PRE-EXISTING (geen regressie, met bewijs)

- **Vraagstuk:** na deploy 229× "context could not be resolved" — nieuw of regressie?
- **Bewijs 1 (payload-probe, live):** openai /models → 124 entries, **0 met context_length/max_input_tokens** (entry-keys: created/id/object/owned_by/shutdown_date); google → 55 entries, 0 context-velden. De live upstreams adverteren géén context-metadata.
- **Bewijs 2 (keten-equivalentie):** de oude registry-fetch las dezelfde payload met dezelfde (lege) velden → zelfde fallback. De 18:00-20:30 "0 warnings" was geen apples-to-apples: geen discovery-polls in dat venster; de once-per-model-warnings vuurden bij de eerste poll na restart.
- **Conclusie:** consolidatie is gedragsgetrouw (strikt gelijk bij context-loze payloads, strikt beter bij payloads mét context — pins bewijzen het bewaren/leveren). De context die wél klopt komt uit overrides (openrouter kimi-k3/deepseek 1048576 live ✓). Les: "with_context=274" was een te zwak criterium (fallback-telling); het bewijs hoort op payload-niveau.
- **Restsignaal (optioneel, trap-2-gebied):** de once-warn-ruis bij discovery kan stiller via context_overrides in settings.yaml voor de modellen die er toe doen.

## 2026-09-02 — Trap 2: modaliteiten-extractie afgerond (capability-based vision-routing basis)

- **Catalog bewaart nu ook modaliteiten** (zelfde single fetch): `architecture.input_modalities`/`output_modalities` (list-vorm) met fallback-parse van de `modality`-string ("text+image+video->text" → input-side). Key-space = catalog-norm (`{brand}/{model}` — matcht failover-candidate-configs). Disk-cache restore backward-compat. Lezer: `get_model_modalities` — bewust ZONDER allowlist (routing-beslissing op expliciete candidates; de oude config-only-weg filterde ook niet).
- **Failover-beslissing uitgebreid:** `FailoverRegistry.set_modality_lookup()` (DI, server.py-binding via `_catalog_input_modalities`) + `group_has_image_capable_candidate()`/`get_image_fallback_for_model` — resolutievolgorde: expliciete config-`modalities` wint ALTIJD (override-first, zoals context_overrides); zonder config beslist de catalog; onbekend = text-only (conservatief).
- **Live config:** geen failover_groups actief — de verandering is latent (actief zodra groups geconfigureerd worden). OpenRouter levert `architecture.input_modalities` per model (archief-analyse: 229/396 met image input).
- **Deferred:** `/v1/models`-cloud-entries krijgen nog géén `input_modalities`-veld (discovery-metadata; klein vervolgdelen, niet blokkerend voor routing).
- **Pins:** 10× tests/unit/test_modality_capability.py (list-form, string-form, onbekend, oude-cache-compat, catalog-capable, config-wint, legacy-gedrag, image-fallback-skip).
- **Pre-existing gevonden tijdens dit werk (niet gefixt, inter-repo-vrij):** server.py F401 `update_gpu_metrics` imported-but-unused (sinds de metrics-cache-refactor; pyflakes-gate-selectie vangt F821, niet F401 — bewijs via git stash).

## 2026-09-02 — Trap 2 live: persist-subset-bug gevonden en gefixt (belangrijke les)

- **Live-vondst na deploy:** disk-cache toonde 0 modalities terwijl de live payload ze WEL had (14 entries met architecture). **Root cause:** `_persist_cache` serialiseerde een expliciete subset (fetched_at/models/reasoning/source) — de geconsolideerde `context`- én `modalities`-mappen vielen STIL weg bij elke disk-write. Dit verklaart ook de eerdere "context_entries=0"-waarneming (toen fout-geïnterpreteerd als "payload heeft geen velden" — dáár apart van bewezen, maar de cache-0 had dit als tweede oorzaak).
- **Les:** een persist-functie die een expliciete veld-lijst serialiseert valt stilletjes nieuw-geadditieve velden weg. Contract-pin toegevoegd: persist→read roundtrip moet context+modalities overleven (tests/unit/test_modality_capability.py).
- **Tweede les (modalities_explicit):** de dataclass kon "expliciet text-only" niet van "default text-only" onderscheiden — een operator die `modalities: ["text"]` zet dwingt bewust text-only af (image-capable model niet voor vision). Fix: `modalities_explicit`-veld (builder zet hem op config-declaratie); expliciet wint ALTIJD, implicit defereert naar de catalog.
- **Live eindstaat (1b9493b):** 14 modality-entries, 7 met image-input, 30 context-entries — de single-fetch bewaart nu ALLE capability-velden en overleeft restarts.

## 2026-09-09 — Setup-agent handoff afgehandeld: HTTP 200-garbage surfacet nu als 502 (2e51140)

- **Handoff (07:45):** upstream-failure-vormen passeerden als HTTP 200 — nemotron: 200 + content:null + embedded `choices[].error {code:500}`; poolside: body zonder `choices`. Callers konden "leeg antwoord" niet van "provider down" onderscheiden.
- **Fix:** `_detect_upstream_invalid_response(path, payload)` in forwarding.py — op chat-vormige paths (chat/completions, completions, messages) vereist een 200-body `choices`; een embedded `choices[0].error` (OpenRouter-vorm) is óók invalid. Resultaat: 502 `upstream_invalid_response` + capture `request_failed` (error_code), met path-uitsluiting voor embeddings (data-contract).
- **Pins:** 4× tests/unit/test_upstream_invalid_response.py (embedded-error, no-choices, valid-payload-regressie, embeddings-uitzondering). Gate 5/5, live MainPID==listener.
- **Request 2 (failover bij :free-modellen) beoordeeld, niet gebouwd:** de machinerie bestaat al (failover/{group}-adressen + health-tracking + attempts-loop). Wat ontbreekt is een groep-config voor de free-modellen — dat is config-werk + eventueel een klein routing-kenmerk (groep-alias). Beoordelingsnotitie bij deze journal-entry; wacht op operator-keuze.

## 2026-09-09 — Legacy-config volledig opgeruimd (operator: "weg met legacy") + test-lekkage gefixt

- **`config/local_models.yaml` was een compat-symlink** uit de F2-migratie (21d35d1) — de resolver (`local_models_file()`) eindigt al bij de provider-file, dus verwijderd (git rm) + FILE_REGISTER.md bijgewerkt. `LEGACY_LOCAL_MODELS_FILE`-constante en de 4-naam-fallback-keten in paths.py weg (single canonieke naam).
- **KRITISCHE vondst bij de opruiming: `config/cloud_keys.json` was NIET dood** — het bevatte de enige `free`-failover-groep (7 candidates: nemotron/laguna/gemma :free-modellen — exact de modellen uit de setup-agent's handoff!) en was, omdat global.settings.yaml 0 groups had, de ACTIEVE bron via de legacy-fallback in FailoverRegistry._load_raw_groups. Blinde verwijdering had de failover-capaciteit gebroken.
- **Volgorde gerespecteerd:** eerst gemigreerd (failover_groups → global.settings.yaml), dan cloud_keys.json verwijderd + de legacy-fallback-tak, FAILOVER_CONFIG_FILE-constante en de path-__init__ weg (failover.py leest uitsluitend nog settings.yaml; in-memory `groups=`-injector voor tests). admin_api-docstrings opgeschoond.
- **Bonus-moment:** de gemigreerde `free`-groep is de gateway-zijdige infrastructuur voor setup-agent's Request 2 — callers kunnen nu `failover/free` gebruiken en de gateway cycled bij 429/5xx door de groep.
- **Suite-falen onderweg (7×) was NIET van de migratie:** tests/unit/test_failover_address_admission.py (setup-agent's bestand, vandaag toegevoegd) roept `local_models.init()` met fakes aan en lekt de module-globals — latere server-shell-tests draaiden tegen de nep-provider-registry (404 model_not_served op 'gpt-4o'). Gefixt met een autouse restore-fixture in dat bestand (globals-snapshot/herstel). Diagnoseweg: isoleer-groen/suite-rood → bisection per file.
- **Verificatie:** 64 gericht + gate 5/5 + live: free-groep 7 candidates via de nieuwe bron, resolver → provider-file, MainPID==listener (038382b).

## 2026-09-10 — Nemotron-diegonderzoek: de "crap-outputs" verklaard en de canonieke adapter gebouwd (3c0edb4)

- **Operator-terechttwijfel:** eerdere fix (chat_template_kwargs-toggle) was symptoombestrijding — de live A/B-drieër Bundel bewees dat de toggle via OpenRouter NOOIT vuurt (reasoning_tokens > 0 in álle calls, ook met de toggle). Adaptive-retry-met-markers (onverstuurde commit) volledig verwijderd — patchwork.
- **De echte mechaniek (primair: HF-modelkaart nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16):** "Reasoning Mode: Configurable on/off via chat template (enable_thinking=True/False)", thinking ON by default; de vLLM-validated deployment draait `--reasoning-parser nemotron_v3` — de thinking/content-scheiding is SERVING-SIDE parser-werk.
- **Levende bewijzen (14 geïnstrumenteerde calls, 2026-09-10):**
  - De "crap"-signatuur: `finish_reason=length` midden-in thinking → de parser ziet geen close-signal en dupliceert de thinking-tekst in `content` (smoking gun: content_len == reasoning_len == 1130 op mt=250). ALLE crap-cases = length; ALLE clean-cases = stop.
  - OpenAI-style `reasoning_effort: "low"` (wat agent31 stuurt) → genegeerd voor nvidia op OpenRouter (417 reasoning-tokens bij effort=low).
  - `chat_template_kwargs.enable_thinking=false` → niet geëerd via OpenRouter (B2 crap met toggle aan).
  - **OpenRouter unified dialect wél:** `reasoning: {"enabled": false}` → reasoning_tokens=0 (H1, deterministisch); `reasoning: {"max_tokens": 100}` → thinking gecapt (111 tok), clean antwoord (H3).
- **De canonieke adapter (3c0edb4):** `adapt_nemotron_reasoning_intent` in cloud_inference/routing.py — vertaalt client-intent naar het dialect dat de provider écht eert: openrouter → unified `reasoning.enabled=false`; nvidia-direct → `chat_template_kwargs.enable_thinking=false` (NVIDIA-documented). Client-explicit `reasoning.enabled=true` wint altijd; config-override `enable_thinking: false` in het models:-blok kan het per model vastpinnen. Zonder low/none-intent: untouched — nemotron redeneert gewoon (live: 402 reasoning-tokens netjes in het reasoning-veld, clean antwoord).
- **End-to-end bewezen via Guardian:** effort=low → reasoning_tok=0 + stop + clean; geen effort → 402 reasoning-tokens + clean. 12 pins + gate 5/5.
- **Research-corroboratie (2 children, primair):** (a) OpenRouter endpoints-API voor nemotron-3.5-lightning:free lijst als supported params ALLÉEN `reasoning, include_reasoning, temperature, max_tokens, seed, top_p, tools, tool_choice` — `chat_template_kwargs` ontbreekt → OpenRouter dropt hem (bevestigt waarom de toggle nooit vuurde); de unified `reasoning`-param ís supported. (b) Hermes-agent #75386 had exact hetzelfde probleem en merged exact dezelfde fix (reasoning.enabled=false per-model gescoped — vendor-breed `nvidia/` gaf OpenRouter 400's op unverified slugs; Guardian's nemotron-substring-scope matcht die les). (c) vLLM `parser/nemotron_v3.py`: bij lege content (o.a. na een length-cut in de thinking) **swapt** de parser reasoning↔content — verklaart de identieke tekst in beide velden én de per-response A/B-flips; correctie op mijn eerdere endpoint-rotatie-hypothese: :free is één NVIDIA NIM-endpoint (NVFP4), geen rotatie. (d) NVIDIA-dialect bevestigd: `chat_template_kwargs.enable_thinking` is de juiste vorm voor nvidia-direct (NIM 2.0.10 + build.nvidia.com); budget-knoppen: `thinking_token_budget` (NIM) / `reasoning_budget` (hosted); "Reasoning and visible output share one max_tokens budget" — verbatim NVIDIA-docs. (e) Guardian's `extract_cloud_reasoning_content` mapt al beide veldnamen (`reasoning_content` + `reasoning`, routing.py:338-340) ✓; LiteLLM heeft géén nemotron special-case — Guardian is hier voorop.
- **Child-1-corroboratie (OpenRouter-primair, onafhankelijke live-test):** `reasoning.enabled=false` → 0 reasoning-tokens ✅; beide effort-vormen (`reasoning.effort` én top-level `reasoning_effort`) → genegeerd door lightning ("accepted but does nothing"). Unified API-vorm bevestigd: `{effort, max_tokens, exclude, enabled}`; `include_reasoning` = deprecated alias van `reasoning.exclude`. **Metadata-nuance:** `GET /api/v1/models` per-model `reasoning:{supported_efforts,...}` — lightning exposeert GÉÉN efforts (daarom doet effort daar niets), maar nemotron-3-super ondersteunt `[medium, low]` + `supports_max_tokens` en ultra `[high, medium]`. Consequentie: de adapter's low→off is voor super/ultra strenger dan nodig (OpenRouter mapt low daar native naar een budget-percentage) — bewust gedrag behouden (deterministisch, geen budget-burn); metadata-gedreven verfijning (alleen vertalen bij models zonder supported_efforts) als open optimalisatie via de catalog.

## 2026-09-11 — Wakeguard-alarmen gediagnosticeerd + stale-status-correctie

- **PR #12 is al gemerged** (2026-08-30, plus opvolg #13 en #14) — de AGENTS.md F5-statusregel ("PR #12, wacht op human merge") is STALE; corrigeren in de eerstvolgende promotion-pass. Les: live-feiten verifiëren via GitHub-API vóór ze als advies gegeven worden.
- **Wakeguard-alarmen (`dsh-ai-kvm2-wakeguard`, 4× in 10h) diagnose:** de check proeft `:11440` DIRECT; :11440 is by design vaak down (idle-unload na 5 min — 6× unload / 7× reload in 14h log-bewijs) → elke sweep in een idle-window = vals "llama-server DOWN"-alarm. MAAR deels reëel: cold-start reload duurt ~40,6s (gemeten) met 2× "Caretaker /ensure transport error (recovering via adopt-poll)" + 1× 503 op :11440/v1/models tijdens het venster — een agent31-run met client-timeout < ~45s die in een idle-window start hangt/sterft. De ensure-errors zijn gedrag van de GEMERGED tranche-2-code (korte /ensure-timeout + 120s adopt-poll vangt koude loads; recovery werkt — model komt up).
- **Advies (wakeguard/agent31 = operator's DSH-agents, workspaces door AAL geredigeerd — niet lokaliseerbaar vanaf deze host):** wakeguard proeft via Guardian :11434 (auto-reload) i.p.v. :11440; agent31 client-timeout ≥ 60s en via Guardian routen. De AAL toont "redacted" voor workspace/project door de agentlog-privacy-redaction — het project heet dus niet echt "redacted".
- Optioneel vervolg: ensure-timeout-tuning (config, PR #14) om de transport-error-warnings bij cold start te dempen.
