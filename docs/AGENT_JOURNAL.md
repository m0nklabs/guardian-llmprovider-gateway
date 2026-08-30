# AGENT_JOURNAL — append-only findings-log (cold file)

> **Werkwijze:** feiten/lessen die je had moeten opgraven (reverse-engineering,
> live tests, verborgen gedrag) → hier APPEN, zelfde sessie, met datum-kop.
> Dit bestand zit NIET in de DSH system prompt → appen kost geen prompt-cache.
> De **promotie-pass** (gebatcht, zie `~/.dsh/AGENTS.md` → "AGENTS.md maintenance
> discipline") distilleert periodiek: universele feiten → regels in `AGENTS.md`;
> detail → docs/-pagina's; verouderd → wissen of archiveren.
> Nederlands is prima (operator-facing, intern).

## 2026-08-30 — DSH zoek-tool kwaliteitsonderzoek + fixes

- Native `web_search` (deepseek-official) was kapot (geen key); na operator-key weer aangezet door andere agent, maar **BETAALD**: ~10.6k in + ~0.8k out tokens per call → rankt **#4** in de zoek-ranking, gratis engines eerst (Google-relay → SearXNG). Kwaliteit is prima (live: v0.3.0-releasepagina als #1).
- Ranking verankerd op 3 plekken: `~/.dsh/plugins/search-tool-ranking.js` (system-prompt), `~/.dsh/AGENTS.md`, repo-`AGENTS.md`-bullet.
- Local-search gateway-defects gefixt (`/home/flip/local-search` `gateway/app.py` +296/−42): Atom-RSS (GitHub-feeds gaven stil 0 items), `fetch_sitemap` expliciete 404, `map_site` echte root-linkdiscovery (Firecrawl /v2/map volgt alleen robots/sitemap), `fetch_meta` json_ld-cap 8 KB. Live end-to-end geverifieerd.
- DSH-bridge patch klaar: `dsh-mcp-client/lib/index.js` resource-pass-through (fixt `get_file_contents` "[resource: content discarded]") — **actief na DSH-herstart**; node_modules-patch gaat verloren bij een dsh-update → opnieuw toepassen.
- **Les:** DSH-plugin `disabled: true` verwijdert een plugin niet altijd — `tool-web` stond disabled in de dump maar de tool bleef geregistreerd. Verifieer met `dsh --profile <p> --dump-config` én de live tool-lijst, niet alleen de dump.
- Klein restpuntje: `fetch_meta` negeert `include_links: false` (links worden altijd meegeleverd) — cosmetisch, op te pakken in `mcp-server/index.js`.

## 2026-08-30 — Two-tier AGENTS.md-werkwijze ingevoerd

- Active Handoff + Open punten verhuisd naar `docs/HANDOFF.md` (cold, 8.5 kB); AGENTS.md = stabiele regels + pointers (28.9 kB), verandert alleen in gebatchte promotie-passes.
- Reden: elke byte-verandering in de hot file maakt de prompt-cache-prefix ongeldig; churn hoort in cold files (gratis), optimalisatie in batches (1× cache-invalidation per pass).
- Guard-plugin `~/.dsh/plugins/agents-md-guard.js` bewaakt hot- én cold-drempels (config: `cordis.patch.yml`).

## 2026-08-30 — F5-tranche-2 merge + caretaker-contract-PR (sessie-fortzetting)

- **PR #12 gemerged** (operator, 10:39:01Z, squash `1402b10`); gateway main nu op 1134 tests. De remote-first hotpath is actief zodra de daemon het contract shipt — de gateway valideert `supports_fresh_load` op ELKE 200 (`"fresh_load" in value`), dus geen gateway-side versie-afhankelijkheid: het contract activeren = alleen de daemon upgraden.
- **Caretaker-contract-PR #7 (merge-klaar, wacht op human merge):** `switch_model` → bool `fresh_load`; `/ensure`-200 antwoordt `{ok, loaded_model, fresh_load, vision_enabled, needs_reload}`. Twee review-rondes, beide terecht-in-de-geest:
  - r1 (taerecht): de no-op fast-path rapporteerde "already active" zonder backend-health — een crash tussen watchdog-ticks laat `current_model` gezet achter met een DODE backend → /ensure lag ("ok: true, fresh_load: false" op een dode backend). Fix: no-op-gate eist geslaagde `server_process.health_ok(...)`; dode backend → geen no-op, healing-reload (fresh_load=True). Pin: `test_switch_model_noop_refused_when_backend_dead`.
  - r2 (deels terecht): de claim "TypeError bij health_ok() zonder URL" klopte NIET (de enige definitie heeft een default-arg; geen subklasse-overrides), maar de bare default gebruikte de IMPORT-tijd `SERVER_URL`-const terwijl de flow bewust call-time `self.server_url` gebruikt → gefixt naar `health_ok(self.server_url)`. De concrete review-suggestie (`health_ok(f"{server_url()}/health")`) zou een dubbel `/health`-pad bouwen — health_ok plakt zelf /health aan (manager.py:115); call-sites geven de basis-URL door. Les: bij review-suggesties die een URL construeren, check eerst wat de callee zelf met de parameter doet.
- **Caretaker-suite 84 passed (2× stabiel); 1 flaky geobserveerd** (`test_switch_model_swap_frees_old_slot_without_deadlock` faalde 1× in de volle suite, slaagde 3× daarna incl. isolatie) — waarschijnlijk machine-timing (wait_for timeout=2); volgen.
- **Oud stash-conflict in caretaker-AGENTS.md opgelost:** commit `089fa38` had de conflict-markers (`<<<<<<< Updated upstream` / `>>>>>>> Stashed changes`) vastgeschreven; de verouderde "Updated upstream"-variant (Phase A "niet gecommit") verwijderd, de actuele variant behouden.
- **Les (2× bevestigd deze sessie):** stash-pop na branch-switch: tracked en untracked bestanden apart stashen (`git stash push -- <file>` vs `git stash push -u`), anders vallen de untracked files buiten de eerste push.

- **Review-cyclus-les (2026-08-30, opgepikt door de operator):** de merge-klaar-check gebeurde vóór 11:31 terwijl de diepe rerun om 11:31:02 nog een incremental review MET een open thread postte (caretaker PR #7) — "0 threads" op t=11:26 was al stale bij het merge-signaal. Werkwijze voortaan: **thread-check herhalen vlak vóór het merge-signaal**, en de laatste review-body (key-observations/incremental) volledig lezen vóór het slot-comment — een "Possible Issue (Not Verified)" kan als review-thread landen óók als de merge-state CLEAN blijft (zonder branch-protection tellen open threads niet als BLOCKED). Slot-comment na de laatste review is verplicht onderdeel van het merge-signaal: de operator moet zien dat de laatste review verwerkt is (beantwoord/deferred met bewijs), niet alleen dat de checks groen zijn.
- **Caretaker PR #7 r3 (incremental 11:31:02) verwerkt als deferred trade-off:** de no-op-gate behandelt één gefaalde health-probe als dode backend. Weerlegging met ≥2 bewijzen: (1) `health_ok` abstraheert ConnectError (dood, direct refused) vs ReadTimeout (levend, >5s) weg naar bool — de retry raakt alleen het hypothetische levend-maar-traag-scenario; (2) kost-analyse: false-negative = één self-healing herstart + de gateway-restore is ná een échte herstart ontworpen gedrag ("stale context" klopt niet); false-positive (hangende backend als authoritative) = alle requests hangen tot client-timeout, watchdog ziet hangen niet. Fail-closed in de veilige richting bewust. Retry (2e probe na 0.5s) verzacht het zeldzame scenario maar verlengt de heal-detectie op de hangende path en voegt een magic delay toe. Follow-up alleen als productie-logs het scenario tonen.
