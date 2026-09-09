# HANDOFF — actuele status & open punten (cold file)

> **Dit is de cold file van deze repo:** agents appen hier vrij — het zit NIET
> in de DSH system prompt, dus churn hier kost geen prompt-cache. De hot file
> (`AGENTS.md`) verandert alleen in gebatchte promotie-passes (werkwijze:
> `~/.dsh/AGENTS.md` → "AGENTS.md maintenance discipline"). Afgeronde sessies
> → `docs/ARCHIVED_HANDOFFS.md`. Verplaatst uit AGENTS.md op 2026-08-30
> (two-tier werkwijze).

### Model-mismatch contract gefixt (2026-09-01, GEDPLOYD — gate groen, restart 13:46 UTC, live E2E bewezen; zie ook de caretaker/handoff-sectie hieronder)


### G3 bare-name routing hijack GEFIXT (2026-09-02, live bewezen) — uit pr-piet bugreport v3


### 2026-09-01 — Split-brain backend-launcher + /ensure-verificatie gefixt (caretaker-repo + host-unit; live bewezen)


> Afgeronde/gesloten DSH-sessies worden gearchiveerd in **`docs/ARCHIVED_HANDOFFS.md`**
> (volledige tekst blijft bewaard; de Active Handoff hieronder houdt alleen sessies
> met lopende relevantie). Gearchiveerd op 2026-08-26 (2 batches):
> **Batch 1:** `20260826_muse_catalog`, `20260826_raw_capture`, `20260824_reasoning_effort`.
> **Batch 2 (alles afgerond):** `20260822_cleanup`, `20260822_nvidia_free_filter`,
> `20260824_cloud_audit`, `20260821_config_schema`, `20260815_bench`, `20260815_gcd`,
> `20260815_1`, `20260816_1`, `20260816_2`, `20260819_1`, `20260820_1`,
> `20260820_cloud_refactor`, `20260813_1`, `20260812_1`, `20260809_5`.
> Alleen `20260826_rename` blijft actief (huidige workspace-transitie) — de écht
> openstaande zaken staan hieronder in **Open punten**.

### 2026-09-01 (avond) — G2 orphan-calls gefixt (3-lagige root cause) + adopt-only eraf

- Tests: 3 nieuwe cloud-forwarding pins + contract-test `test_begin_queued_request_cleans_up_waiter_on_disconnect` overgezet naar het receive-contract. Pre-restart gate: alle poorten PASS (eerste gate-run faalde op het oude polling-contract — exitcode checken met PIPESTATUS, niet via `| tail`).
- DSH-sessie-breuk 20:31 was NIET Guardian (gateway draaide door; de DSH-webserver op :3080 herstartte zelf).


## Open punten (actueel — alles wat hier niet staat is afgerond; details in `docs/ARCHIVED_HANDOFFS.md`)

- **NVIDIA bruikbaarheidskaart GEMETEN (2026-09-02, volledige probe, `scratch/nvidia_probe_results.json`):** 81 catalogusmodellen geprobed: **12 OK** (minimaxai/minimax-m3, moonshotai/kimi-k3, nvidia/nemotron-3-super-120b-a12b, nemotron-3.5-lightning-30b-a3b, nemotron-3-nano-omni-30b-a3b-reasoning, llama-3.2-11b-vision-instruct, poolside/laguna-xs-2.1, 3 safety/guard, 2 riva-translate), **55×404** (68% dode catalogus-entries), 11×timeout (o.a. deepseek-v4-flash/pro), 2×500, 1×400. **Metadata-vondst:** NVIDIA's `/models` geeft géén context_length — overrides vereisen model-card-kennis en zijn alleen zinnig voor de 12 werkende.
- **Catalogus-consolidatie: twee-traps plan VASTGELEGD (operator-besluit):** trap 1 = consolidatie (één bron, één TTL, dubbele fetches weg) — volgende te bouwen, ~1-2 uur, geen routing-gedragsverandering; **trap 2 = modaliteiten-extractie is verplicht vervolg** (pas ná trap 1 — op de ongeconsolideerde basis bouwen verdubbelt tijdelijk de schuld).
- **Orphan-herkomst: gebonden forensica-pass AFGEROND (2026-09-02) — beste verklaring:** het journal-venster 22:08:44-50 toont een host in systemd-chaos: vllm-bench-cpu/gpu (53.640 herstarts!), vllm (27.098, 203/EXEC — binary ontbreekt), nervesplat (52.311, spawnde op exact 22:08:47), caramba-processor (27.065). Plausibele verklaring cgroup-escape: verzadigde systemd job-wachtrij tijdens de guardian-transitie. Handoffs aangemaakt in `/home/flip/caramba/docs/HANDOFF.md` + `/home/flip/nervesplat/docs/HANDOFF.md` (inter-repo). Raadsel gesloten op beste verklaring; risico blijft gedempt.
- **Geheugen-idee (capture → agent-geheugen) IN DE KOELKAST (operator-besluit 2026-09-02):** stap 1 FTS/SQLite-index, stap 2 semantische embeddings — ontwerp staat in deze journal (2026-09-02 nachtdienst). **Bezwaar van de operator dat het shelven veroorzaakte:** een gedeeld geheugen kan nadelig zijn zodra agents over projectgrenzen heen lezen (kruisbesmetting/isolatie) — een toekomstige uitwerking moet per-project scoping/key-isolation als eerste-klas eis ontwerpen, niet als optie. Pas oppakken als de operator dat weer op tafel legt.
- **OpenRouter modaliteiten + catalogusvelden (GEWENST, deferred — operator-besluit):** `/models`-payload bevat `architecture.*_modalities`, `pricing`, `supported_parameters` e.d. die Guardian weggooit — nodig voor capability-gebaseerde vision-routing; dezelfde catalog-URL wordt 2× onafhankelijk gefetcht (consolidatie hoort in die verbouwing). Analyse → JOURNAL-archief §2026-08-30 catalogusvelden.
- **CI-adoptie (open sinds 20260813_1):** `scripts/pre_restart_check.py` als GitHub Action nog niet opgepakt.
- **llama-guardian.service file (operator-beslissing):** aparte unit-file (géén alias!), nu disabled + failed + `Restart=no`; verwijderen/hernoemen of laten — hij is inactief en ongevaarlijk, maar de naam in AGENTS.md-documentatie ("alias") is misleidend.
- **m0nkdash-origin (optioneel):** origineel achter `dashboard.oelala.xyz` blijft dood — raakt Guardian niet.
- **72h soak AFGESLOTEN (2026-09-02, bewijs):** capture draait ~26 dagen live in productie; volledige inventarisatie: 172 bestanden, 41.044 events (20.667 received / 20.218 completed / 146 failed = 0,7% / 13 cancelled), 169/172 bestanden gezond, 0 parse-falen in gezonde bestanden. 3 beschadigde .gz-rotaties (1,7% — crash-slachtoffers van de restart-loops 09-01/02; door de restart-race-fix + kill-loop-demping niet meer reproduceerbaar; tolerant readers overslaan ze). Policy 1.1.0 actief.

## 2026-09-07 — setup-agent constatation: redactor precision audit needed (principal policy 09-07: "secrets out = good; non-secret data dropped = must fix")

**Observation (code-read, app/capture/redactor.py):** two patterns are categorically over-broad for capture content:
1. `_IPV4_RE` applied to ALL text (line ~90) — every IPv4-looking string becomes `[REDACTED_IP]`, including LAN configs, research content and code examples (agent31's traffic is full of these: 192.168.1.35 gateway address, monerod config, wireguard research).
2. `_ENV_VAR_RE` = `\$\{?[A-Z][A-Z0-9_]*\}?` (line ~92) — every `$UPPERCASE` reference in captured code/scripts becomes `[REDACTED_ENV_VAR]` (`$HOME`, `$VERSION` — not secrets).
Live WAL counts (current.jsonl): 25× IP, 19× ENV_VAR, 8× API_KEY, 8× AUTH_HEADER — partly the setup agent's own meta-discussion of the redactor (captured), so treat counts as upper bounds. API_KEY/Bearer patterns look correctly scoped.

**Suggested remediation (guardian agent decides/implements):** config-driven precision — (a) sensitive-IP LIST (home WAN + LAN subnet) instead of all-IPv4; (b) env-var redaction only for known secret-var NAMES or high-entropy values, not every `$UPPERCASE`; (c) keep API-key/Bearer patterns; (d) golden tests: benign payloads (`$HOME` in a bash snippet, default-gateway examples) must pass through unredacted, real keys must still be caught. Principal context: capture gaps created by guardian's own secret-filtering are BY DESIGN and must not be "filled"; over-filtering of non-secrets is the thing to fix. This is guardian-agent scope — setup agent will NOT implement.

## 2026-09-07 — Handoff from cryptotrader session: generalize caretaker beyond llama.cpp?

**Context**: the cryptotrader forecasting lane (TimesFM 3.0, PyTorch) was caught holding 1468 MiB of GPU 0 resident 24/7 while serving ~1 forecast/hour. The operator asked whether it could "run via guardian, to the caretaker, so it only works on-demand and waits in a queue".

**Finding**: caretaker's lifecycle management (spawn/stop/unload/health, idle-unload via the gateway's ensure/unload calls) is exactly the right *pattern* — but the current implementation manages **llama.cpp server processes (GGUF models)** with an OpenAI-style serving contract. TimesFM is a torch model with a time-series contract; it cannot run under caretaker today.

**Recommendation (for this repo's agent to evaluate)**: consider generalizing caretaker (or a sibling service) into a **generic model-lifecycle supervisor** — backend adapters declare how to start/health-check/unload a model host, callers get ensure/queue/idle-unload semantics regardless of model type. That would let every LAN service with an occasionally-used local model (cryptotrader's TimesFM is the first known candidate) drop resident memory and reuse one proven queue/idle-unload implementation. Until then, cryptotrader implements the pattern in-process (lazy load + idle unload in its own service — PR in flight there, no dependency on guardian/caretaker).

**Evidence**: cryptotrader issue thread 2026-09-07 (GPU residency); `nvidia-smi` showed the uvicorn process at 1468 MiB beside ComfyUI (port 8188) and Frigate; caretaker scope verified via this repo's F5 notes (spawn/stop/reload/switch/unload/health/crash for llama.cpp).

## 2026-09-08 — FEATURE REQUEST (from setup agent, redacted project): degeneration watchdog (streaming circuit breaker)

**Context (evidence, live-measured 09-08):** agent31 consumes `:free` OpenRouter models through this gateway. Live probes showed: nemotron-3.5-lightning healthy (5/6 checks), gemma-4-26b 6/6 HTTP 429, poolside 2/6 intermittent — and one generation record (12:10:23Z, origin agent31.guardian.local) with finish_reason "length": a reasoning model burned 361 thinking tokens on a trivial probe question, then the caller's 400-token answer-cap truncated the result → HTTP 200 with a useless payload. Callers currently defend themselves with STATIC max_tokens caps, which fight reasoning models (agent31's own documented rule: reasoning models need generous budgets).

**Request:** add a degeneration watchdog in the gateway's streaming path (the recent event-loop audit shows the stream hook points exist):
- Detection: sliding-window k-gram repetition on the outbound token stream — e.g., any 6-gram repeating ≥3× within the last ~200 generated tokens → degeneration loop. Rolling hash, no LLM scoring needed; also flag zero-EOS over N tokens for trivially small prompts.
- Action: cancel the upstream request and return `finish_reason: "degeneration_detected"` (+ repeat stats) to the caller instead of letting the loop run to the provider cap.
- Why: with a watchdog at the gateway, consumers can DROP static max_tokens caps on free models (model stops naturally via EOS, pathology killed early by the gateway, provider cap as last resort). One implementation protects all consumers (agent31, setup sessions, future agents) instead of every caller rolling its own cap.
- Secondary: does the gateway forward OpenRouter `reasoning` parameters (effort low/high, exclude) for reasoning models? If yes, callers can control thinking depth per intent (probe = low, deep work = high) — the cleanest lever against the "reasoning ate my budget" class.

Contact: setup agent, redacted project (this note is informational — implementation scope/timing is the gateway project's call).

## Afgerond deze sessie (licht gecompacteerd; voltekst → `docs/ARCHIVED_HANDOFFS.md`)

- **Model-mismatch contract:** 503 `model_switch_failed` op elk lokaal entry-pad, live probe `backend_serving_model_name()` — live bewezen, `ba9467e`.
- **Caretaker split-brain + /ensure strict:** legacy-launcher weg, /ensure fail-closed + retry — PR #9/#10 gemerged (`ba866ff`/`f0bdeb6`).
- **G3 bare-name routing hijack:** catalog-gestuurde disambiguatie + `z-ai/` uit nvidia-prefixes — `7d5d32f`, live bewezen.
- **G2 orphan non-stream calls:** raw-ASGI receive-watchers (cloud + queue), 499-contract — `3fa1479`/`f2d4d9f`/`6db7f5b`, live bewezen (0 tokens verbrand).
- **`GUARDIAN_STARTUP_ADOPT_ONLY=1` verwijderd** uit beide unit-files; startup-heal weer volledig actief.
- **UNIT-VALKUIL opgelost:** `llama-guardian.service` is nu een **symlink** op de echte unit (aparte file + drop-ins bewaard als `.disabled-20260902`) — restart via alias veilig.
- **Test-isolatie:** 20 live integration-tests default gedeselecteerd (`7db5ba3`); de verouderde google-test-note gecorrigeerd (niet reproduceerbaar op HEAD).
- **Streaming-teardown pin:** client-disconnect tijdens write = `request_cancelled`/`client_disconnect` — `f61c2f1`.
- **Gateway restart-race gefixt:** listener-herkenning herkent `python3.14 -m app.main` weer (`ec1211e`); post-restart-verificatie (MainPID == listener) standaard.
- **ensure_fresh gewired** aan `/v1/models` + `/ensure` ERROR→WARNING (`7f777a5`); live 272 modellen, geen fouten.
- **pi-models opgeschoond:** 216→99 entries, alle resolvem live; backup `models.json.bak-20260902`.
- **72h-soak afgesloten:** 26 dagen live, 41.044 events, 169/172 bestanden gezond, 0 parse-falen; 3 truncaties = crash-slachtoffers (niet meer reproduceerbaar).
- **Nul-delta-meting + gap-vrij architectureel:** alle event-loop-blokkades uit de audit verwijderd (`f38af54`/`97de6ea`); structural guard 19 modules; via-gateway p95=2ms, 0 gaps>0.5s.
- **C-feedback dossiers:** volledig afgehandeld door PR #17 (`ef483dd`) — C2/C7 refutaties, C8-C11 live; onafhankelijk herverifieerd.

- **Degeneratie-guard LIVE-bouw afgerond (`app/gateway/degeneration.py`, schema 1.2.0):** server-side cutoff van repetition-loops (operator-feature, koelkast-noot hieronder blijft staan), thresholds in `config/global.settings.yaml` `degeneration:`, kill-switch `enabled: false`. Pins 17×, gate groen; deploy + live-sanity volgt in deze sessie. Open: thresholds tunen op echte degeneratie-cases via capture-veld `degeneration_cutoff` (monitoring), en de letter-level-vrijstelling (q=1) her-evalueren als er "aaaa"-cases opduiken.

- **Degeneratie-marker-injectie (operator-verzoek, zelfde sessie):** bij cutoff krijgt de lezer nu een zichtbare slot-marker `\n\n[guardian: generation cut off - repetition loop detected]` als laatste content-delta (alle paden, `marker_enabled`/`marker_text` configureerbaar). Machine-signaal onveranderd (`finish_reason: "length"` + capture-vlag).

- **Catalogus-consolidatie trap 1 AFGEROND (2026-09-02):** dubbele /models-fetch weg — CloudModelCatalog is nu de enige fetch-bron (bewaart models + reasoning + context); registry leest via DI-reader, geen HTTP meer in providers.py. Pins herleid + 3 nieuwe, gate groen. **Trap 2 (modaliteiten-extractie) is het verplichte vervolg** — basis gereed.

- **OPEN VRAAG (operator, op todo):** rapporteert de caretaker OOM's terug aan Guardian? (OOM-kill detectie + rapportage-oppervlak naar Guardian — inter-repo: m0nklabs/caretaker-llamacpp; raadpleeg caretaker_client/caretaker_runtime-wiring + caretaker-repo status-endpoints).

- **Trap 2 AFGEROND (2026-09-02):** modaliteiten-extractie live — catalog bewaart input/output-modalities (single fetch), failover image-capability beslist config-first, dan catalog. Deferred: `input_modalities`-veld op /v1/models-cloud-entries. **Beide traps van het twee-traps plan zijn nu gesloten.**

## 2026-09-08/09 — FOLLOW-UP on the degeneration handoff: upstream failures surface as HTTP 200 + null content (capture-log evidence)

**Second live finding (setup agent, redacted project) — a DIFFERENT failure class than repetition loops, observed in the same window:**
- 09-09 ~07:17-07:40 UTC, data/capture/guardian_capture_current.jsonl: agent31 probe battery (nemotron mt=2000 effort=low) produced no usable answer; laguna-s + laguna-xs probes = `request_failed` within ~1s (x4). Setup's own live calls reproduced it: nemotron-3.5-lightning returned **HTTP 200 with `content: null`, partial reasoning ("Here") and an embedded upstream `error: {code: 500, error_type: "server"}`** (Nvidia); poolside returned a body with **no `choices` key at all**. Meanwhile nemotron-3-super-120b-a12b:free answered correctly in the same minute. agent31 correctly flagged "primary DEGRADED" in run 3 and cycled models manually (visible in capture + OpenRouter activity).
- **Request 1 — surface upstream failures as real HTTP errors:** when the upstream response carries `choices[].error` (OpenRouter's embedded-error shape) or a body without `choices`, the gateway should return a proper 502/503 to the caller instead of passing 200-with-garbage through. Callers (python urllib, dsh tooling) currently can't distinguish "empty answer" from "provider down" without bespoke parsing.
- **Request 2 — gateway-side failover:** the recent modalities work added failover machinery; consider auto-retry within a failover group when the upstream 500s/429s on a `:free` model — callers ask for "a free model", the gateway picks a healthy one. This removes the caller-side model-cycling visible in today's capture.
- Same-minute evidence that capacity windows rotate: nemotron-3-super answered fine while lightning + both lagunas failed. Detection/fallback at the caller works (agent31's DEGRADED flag); the gateway could own it centrally.
