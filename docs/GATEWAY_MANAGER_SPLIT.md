# Guardian opsplitsing — `guardian-llmprovider-gateway` + `guardian-llama-cpp-manager`

> Status: **architectuur-richting (operator-besluit 2026-08-26), NIET gebouwd.**
> Operator: "ik zit te denken om de boel op te splitsen" → twee componenten:
> **`guardian-llmprovider-gateway`** (de gateway/kieslaag) en
> **`guardian-llama-cpp-manager`** (de wrapper die llama-server beheert).

## Waarom

De operator wil de **`local`-levenscyclus uit Guardian halen** en als een
**wrapper naast llama-server** laten draaien. Dat maakt elk
model-serverend ding een *provider* (openrouter / nvidia / windows /
lokale-manager) en Guardian puur de **gateway naar providers** + logger
(raw capture, zie reeds 2026-08-26). Dit is de logische vervolgstap op de
provider-unificatie (`LAN_GPU_BACKENDS.md`) en sluit aan op het bestaande
patroon: `app/local_inference/ollama.py` is al een bridge naar een externe
(Ollama-)daemon.

## Ontleding — wat managed de huidige Guardian nu ECHT (2026-08-26)

Hard geteld (functie-lichamen): `engine/manager.py` ≈ 1637 regels,
`local_inference/models.py` ≈ 277, idle-unload-watcher ≈ 25.

| Regels | Categorie | Hoort waar? |
|---|---|---|
| ~1050 | **Echte lifecycle**: spawn/stop/restart, args-bouw, drift-detectie, health-check, crash-detectie + auto-restart, unload, ComfyUI VRAM vrijmaken, context-save/restore | Manager (de kern) |
| ~509 | **Registry/keuze/discovery**: `resolve_model`, `get_preferred_tool_model`, `get_advertised_context_window`, vision-capability-cache, `get_public_model_map` | Gateway (staat nu door elkaar in manager.py) |
| ~78 | **Settings lezen**: `_load_aliases`, `_load_switch_allowlist`, `_load_config` | Al gedeeld — leest dezelfde YAML als de gateway; geen aparte config |
| ~130 | **Resolutie/sizes/timeouts** (`local_inference/models.py`) | Gateway |
| ~40 | **VRAM-slot-acquisitie** (`VRAMScheduler.acquire/release`) | Manager |
| ~40 | **`reload_backend_after_connect_error`** | Manager (herstel-pad) |
| ~25 | **Idle-unload watcher** | Manager, MAAR verweven met verkeer: leest `active_requests` + `_inference_queue.active/waiting_count` van de gateway vóór unload |

**Conclusie van de ontleding:**
1. **Settings zijn geen manager-werk** (operator-gelijk): `models.local.settings.yaml`
   + `models.cloud.overrides.yaml` zijn al de enige bron; de manager leest ze
   alleen. Er is geen aparte models-registry nodig.
2. **De echte manager-kern is dun**: ~1050 regels subprocess-wrapping +
   watchdog (spawn/args/health/crash/unload/VRAM-slot). Dat is een *module*,
   geen *daemon* — en hij is **verweven met verkeer** (idle-unload leest
   queue/requests; auto-switch wordt door requests getriggerd; VRAM-slot
   wacht op vrijkomende modellen). Een aparte daemon verwijdert die
   verwevenheid niet — hij heeft er een nieuw contract voor nodig
   (gateway → manager: "3 requests in flight, niet unloaden").
3. **De split wordt pas echt waardevol voor andere hosts**: op de lokale
   GPU-host is de manager-kern klein en verkeersgebonden (module volstaat);
   een daemon is de enige manier om een GPU-host ZONDER Guardian (Windows)
   beheerd te krijgen. Dáár is geen verwevenheid — gewoon "draai model X".

## Wat verhuist naar `guardian-llama-cpp-manager` (alleen de lifecycle-kern)

| Bron (nu in Gateway-repo) | Functie |
|---|---|
| `app/engine/manager.py` (lifecycle-delen, ~1050 regels) | llama-server spawn/stop/reload, args-bouw, health/crash-detectie, auto-restart, unload, ComfyUI VRAM vrijmaken, context-save/restore |
| `app/local_inference/models.py` (VRAM-slot + reload-after-connect-error) | VRAMScheduler, `reload_backend_after_connect_error` |
| `app/proxy/lifespan.py` (idle-unload-watcher) | auto-unload na N minuten inactief — **verkeers-input nodig van de gateway** (contract!) |
| `config/models.local.settings.yaml` | **blijft de gedeelde settings-bron**; de manager leest hem alleen (geen kopie) |

**Manager = eigen proces** naast llama-server, met een dunne control-API:

```
GET  /status            → geladen model + gpu/vram-status
POST /ensure {model}    → laadt model (VRAM-slot, swap); idempotent
POST /unload            → unload (of zelf na N minuten — met verkeers-input)
(OpenAI-inferentie-API blijft rechtstreeks llama-server: http://127.0.0.1:11440/v1)
```

## Wat blijft in `guardian-llmprovider-gateway`

- Proxy/routing: auth, cloud+local herkenning, failover-groepen, queue,
  timeout-tiers, Anthropic-bridge, streaming keepalives.
- **Raw capture** (WAL, `guardian_capture_v1`, gzip/multi-member, media,
  reasoning) — de gateway is de logger.
- Discovery (`/v1/models`, `/api/show`, context-metadata) en de
  `DynamicScaler` (adaptive reasoning budget & max_tokens — dit is
  verkeerslogica, blijft hier).
- De **509 regels registry/keuze/discovery** die nu in `manager.py` staan
  (resolve_model, preferred tool/reasoning model, vision-capability-cache,
  context-metadata, public model map) — dit is gateway-werk dat er nu
  doorheen staat; eerste stap van de split is dit verhuizen.
- `scheduler/manager.py` (maintenance-mode die services stopt in
  idle-window) blijft ook hier (host-beheer, of verhuist naar systemd/cron
  — operator-keuze).
- Api-key-registratie, admin-api, usage/metrics, config-schema
  (`providers.settings.yaml` blijft het register van ALLE providers).

**`local` wordt een passieve provider-entry** in de gateway:

```yaml
providers:
  local:
    base_url:       http://127.0.0.1:11440/v1
    management_url: http://127.0.0.1:11441   # optioneel ensure-call vóór forward
    managed: false                            # gateway is NIET meer eigenaar
```

## Contract gateway ↔ manager (de verwevenheid, expliciet)

- Gateway roept vóór een forward optioneel `POST /ensure {model}` aan
  (idempotent: "heb dit model, anders laad het"); daarna
  `POST /v1/chat/completions` op llama-server.
- Manager zelf kan een swap triggeren (idle-unload) → gateway moet 404
  `model_not_loaded` / 503 afvangen en met `ensure` retryen (zelfde
  herstelpaden als nu bij connect-error).
- **Verkeers-input**: gateway geeft actieve request/queue-aantallen door
  (of roept `/unload` alleen aan als het veilig is — de idle-beslissing
  blijft dan in de gateway, de uitvoering in de manager).
- `GET /status` voedt de discovery van de gateway: geladen model + context
  → geen parallele registry nodig, de manager is de bron van waarheid voor
  de lokale GPU.
- Replay/verificatie blijft WAL (`guardianctl export`), onafhankelijk van
  deze split.

## Voordelen / nadelen (eerlijk)

- **Voordelen:** backends worden generiek herbruikbaar per GPU-host (dezelfde
  manager draait straks op Windows — `windows` wordt identiek behandeld als
  `local`); restart van de gateway raakt het geladen model niet meer (en
  vice versa); één consistent verhaal per model-server.
- **Nadelen/kosten:** nieuw proces = nieuwe systemd-unit (bv.
  `llama-cpp-manager.service`) + eigen logging/monitoring. De manager-kern
  is klein (~1050 regels) en verkeersgebonden — voor de lokale host alleen
  is een aparte daemon NIET nodig (een module volstaat); de daemon wordt pas
  zinvol zodra er GPU-hosts zonder Guardian (Windows) zijn. Contract +
  swap-latentie + 404/503-herstelpaden moeten expliciet gemaakt worden.

## Volgorde (fasen, voorstel — herzien n.a.v. ontleding)

0. **Eerst: ontdraaien.** Verhuis de ~509 regels registry/keuze/discovery
   uit `engine/manager.py` naar de gateway-laag. Dit is puur gateway-werk
   dat er nu doorheen staat; het maakt de splitgrens schoon en verkleint de
   "manager" tot de echte kern. Geen gedragsverandering.
1. **Manager isoleren in dezelfde repo** als apart proces (control-API +
   systemd-unit), gedrag behouden; gateway blijft nog direct beheren maar
   kan al via `/ensure` sturen (dual-path). Alleen doen als Windows/andere
   hosts echt aan de deur staan — anders blijft het een module in de gateway.
2. **Gateway `local` passief maken** (`managed: false`, via het contract);
   oude directe lifecycle-code verwijderen uit de gateway.
3. **Windows-project**: zelfde manager-binary op Windows (wrapper + CUDA
   llama-server) → `windows`-provider-entry (config-only).
4. (Verhuis naar aparte git-repo's is optioneel en laatste; eerst in-monorepo
   stabiel laten draaien.)

## Openstaande vragen

- Eén monorepo met twee daemons, of direct twee aparte repos?
- Mag de manager zelf `POST /pick` doen (keuze verplaatsen) of blijft de
  keuze in de gateway (aanbevolen: gateway houdt de keuze, manager voert uit)?
- Waar gaat `scheduler/manager.py` (maintenance/services-stopper) naartoe?
- Naming: project-prefix `guardian-` en component-prefix
  `llmprovider-gateway` / `llama-cpp-manager` bevestigd (operator 2026-08-26).

## Cross-references

- `docs/LAN_GPU_BACKENDS.md` — provider-unificatie (Windows-PC-GPU) waar
  deze split de logische uitwerking van is.
- `docs/CONFIG_SCHEMA.md` — config-schema; `providers.settings.yaml` blijft
  het register.
- `AGENTS.md` „RAUW capture"-handoff — Guardian = RAW loggen + replay;
  Keanu = redactie + dataset (onveranderd door deze split).
