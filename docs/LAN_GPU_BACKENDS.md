# LAN GPU Backends — plan (2026-08-26, operator-idee, nog NIET geïmplementeerd)

> Status: **plan only** (operator-keuze 2026-08-26: "alleen plan vastleggen").
> Doel: de GPU van de Windows-PC inzetten naast de lokale GPU.
> **Architectuurprincipe (operator-correctie 2026-08-26):** alles wat
> modellen serveert hoort in `providers.settings.yaml` te leven — dus óók
> de lokale llama-server, niet alleen cloud/Windows. Zie §Unificatie.

## Aanleiding / doel

Guardian serveert lokale modellen via één llama-server-proces op
`127.0.0.1:11440` (`app/engine/manager.py`, model-switching via
`config/models.local.settings.yaml`). De operator wil de GPU van zijn
Windows-PC (NVIDIA/CUDA, zelfde LAN) kunnen inzetten. Operator-beslissing
(2026-08-26): het gaat **voornamelijk om meerdere modellen tegelijk op het
LAN** draaien — niet om één model te splitsen (netwerk-vrees), tenzij 1 Gbit
voor RPC tóch leuk genoeg is (antwoord: ja, zie §Optie B).

## Huidige architectuur (relevant)

- **Lokaal:** één llama-server op `127.0.0.1:11440`; per model GGUF-pad,
  context, ngl, kv_type, tensor_split; VRAM-scheduler + idle-unload +
  auto-switch (`app/scheduler/`). De URL is config-gedreven
  (`engine/manager.py`, default `http://127.0.0.1:11440`).
- **Cloud:** `CloudProvider` (name, base_url, api_key, models, catalog_url,
  catalog_allowlist) → dynamische catalogus (`/v1/models`), routing,
  streaming (SSE), capture en failover werken daar al generiek
  (`app/proxy/cloud_catalog.py`, `app/cloud_inference/`).

## Unificatie — alle model-servers in providers (operator-principe)

De operator stelt terecht: als de Windows-llama een provider-entry wordt,
moet **alles wat modellen serveert** door dezelfde providers-registratie
lopen. Doelbeeld:

```
providers.settings.yaml
  providers:
    local:      # ⭐ door Guardian beheerd (de enige managed entry)
      base_url: http://127.0.0.1:11440/v1
      managed: true        # engine/manager.py spawnt/schakelt deze
      catalog_url: /v1/models     # llama-server adverteert z'n model
    windows:    # extern, ongebeheerd — operator draait de server zelf
      base_url: http://192.168.1.x:11440/v1
      api_key:  ${WINDOWS_LAN_KEY}
      catalog_url: /v1/models
    openrouter: # bestaande cloud-entries, ongewijzigd
      ...
```

**Wat wél blijft verschillen (bewust):** `managed: true` op `local` = de
enige entry waarvan Guardian de levenscyclus bezit (spawn/stop, VRAM-
scheduler, idle-unload, auto-switch, tweaker-aanpassingen). Alle andere
entries (Windows, cloud) zijn passieve endpoints — Guardian stuurt er alleen
verkeer naartoe. `models.local.settings.yaml` blijft bestaan als de
runtime/args-metadata van de `local`-entry (GGUF-pad, context, ngl,
tensor_split, switch-policy) — niet als een apart serverregister.

**Implicaties / werk (geschat, NIET begonnen):**
1. `app/proxy/providers.py`: `CloudProvider` krijgt `managed: bool`
   (default False); `is_cloud_model`/`get_provider_for_model` en de
   route-dispatch moeten `local/...`-adressen als provider-resolutie zien.
2. `app/gateway/routing.py` + `app/local_inference/models.py`: lokale
   routing blijft via de bestaande aliases werken, maar de
   lokale-herkenning komt uit de providers-registratie i.p.v. een
   parallelle branch ("is cloud" = "provider entry zonder managed").
3. Catalogus: `local` krijgt een eigen catalog-refresh (llama-server
   `/v1/models` — geverifieerd: werkt, adverteert het geladen model).
4. Discovery (`/v1/models`): Windows-modellen verschijnen als
   `windows/<model>`; lokale als nu (aliases), maar consistent uit dezelfde
   registry.
5. Backwards-compat: alle bestaande lokale aliases + cloud-addressen
   blijven ongewijzigd werken (routing-herkenning is een superset).
6. Failover-groepen: `local` en `windows` kunnen straks in dezelfde
   failover-groep (lokaal → Windows-fallback of omgekeerd) — nieuw voordeel
   van de unificatie.

**Risico/grenzen:** dit is een routing-refactor (raakt providers, routing,
model_discovery, local_inference) — géén config-only wijziging, dus code +
gate + restart. De engine-manager-laag zelf (spawn/scheduler) verandert
niet van gedrag; alleen de registratie/herkenning wordt unificeerd.
Stap 1 = `local` als managed provider-entry; stap 2 = Windows-entry; cloud
blijft zoals het is.

## Optie A — Windows als extra model-server (KEUZE)

Windows draait zijn eigen llama-server-proces(sen) (CUDA-build). Guardian
ziet elke Windows-llama-server als een **provider op het LAN**: een entry in
`config/providers.settings.yaml`. Omdat llama-server OpenAI-compatibel is
(`/v1/models`, `/v1/chat/completions`, SSE) pikt de bestaande
provider-machinerie het gratis op.

**Windows-kant (operator):**
1. llama.cpp **CUDA-release** installeren op de Windows-PC.
2. Per model een eigen proces (llama-server is single-model per proces):
   ```
   llama-server.exe -m <model>.gguf --host 0.0.0.0 --port 11440 --n-gpu-layers 99 --api-key <lan-key>
   ```
   Meerdere modellen = meerdere poorten (11440, 11441, …) = meerdere
   provider-entries.
3. Firewall: inbound-poorten openzetten. Optioneel als Windows-service
   (NSSM/scheduled task) zodat hij na reboot terugkomt.
4. Het GGUF moet lokaal op Windows staan (of via SMB-share gedeeld).

**Guardian-kant (nog te bouwen):**
```yaml
# config/providers.settings.yaml (+ overrides indien nodig)
providers:
  windows:
    base_url: http://192.168.1.x:11440/v1   # <-- IP/poort invullen
    api_key: ${WINDOWS_LAN_KEY}             # zelfde key als --api-key
    catalog_url: /models
```
- Model-adres wordt `windows/<model-id>` (catalogus normaliseert
  `{brand}/{model}`; llama-server adverteert model-bestandsnaam of
  `--alias`).
- **Nodige kleine plumbing (schatting):**
  1. `CloudProvider.is_configured` vereist nu een niet-lege api_key
     (`app/proxy/providers.py`). Ofwel altijd `--api-key` op llama-server
     zetten (aanbevolen), ofwel een `lan: true`-vlag die lege keys toestaat.
  2. **Context-metadata:** llama-server `/v1/models` rapporteert (waarschijn-
     lijk) geen `context_length` → `DEFAULT_CONTEXT_WINDOW = 131072`
     fallback, zelfde patroon als NVIDIA. Per-model `context_window`
     override in `models.cloud.overrides.yaml` corrigeert dat.
  3. Eventueel `per-key cloud_gateway_access`-semantiek voor de nieuwe
     provider (wil je hem aan alle Guardian-keys geven of beperken).
- **Wat gratis werkt:** catalogus-discovery, cloud-routing, streaming
  (llama-server SSE + heartbeat), raw capture (cloud-pad), failover.
- **Wat NIET geldt:** lifecycle-beheer (geen idle-unload/auto-switch op de
  Windows-GPU — die beheert de operator zelf), VRAM-scheduler ziet de
  Windows-GPU niet (bewust: aparte machine).
- Config-only → **hot-reload** (`POST /api/config/reload`), géén restart.

## Optie B — Eén model over beide GPU's (llama.cpp RPC, later optioneel)

llama.cpp's `llama-rpc-server`: Windows-host draait dat (zelfde GGUF), de
Linux llama-server wordt gestart met `--rpc <windows-pc>:50052` → lagen
worden naar de Windows-GPU geoffload; beide GPU's worden één device. De
enige manier om één model te draaien dat niet op één GPU past.

**1 Gbit-realiteit (onderzocht 2026-08-26):**
- Per token gaat over de grens alleen een activatie-tensor
  (~hidden_size × 2 bytes ≈ 7–15 KB) + ~0,2–0,5 ms LAN-latentie per
  crossing. GPU-compute is de bottleneck; overhead voor single-stream chat
  is in de praktijk **~5–15%** → 1 Gbit is voor chat-werk prima.
- Waar 1 Gbit wél pijn doet: prefill met lange prompts (MB's per batch) en
  meerdere concurrent streams; daarvoor pas 10 GbE.
- Vereisten: GGUF op beide hosts, CUDA-build op Windows, netwerk
  RPC-poort (50052 default) open.
- Guardian-integratie zou via `extra_args`/config-blok in
  `app/engine/manager.py` lopen (llama-server-start met `--rpc`).

## Beslissing

- **Principe (operator 2026-08-26): unificatie** — alle model-serverende
  endpoints in `providers.settings.yaml`; `local` wordt de enige `managed`
  entry (Guardian bezit de levenscyclus), Windows + cloud zijn externe
  entries. Zie §Unificatie.
- **Stap 1: `local` als managed provider-entry** (routing-refactor, code +
  gate + restart) — vereist operator-akkoord; hierna is het register
  uniform en is `windows` triviaal.
- **Stap 2: Windows-PC als externe provider-entry** (config-only,
  hot-reload) — Optie A, past bij het doel (meerdere modellen parallel op
  LAN), geen netwerkgevoeligheid.
- **Later optioneel: Optie B** (llama.cpp RPC) voor één model dat nergens
  past — haalbaar op 1 Gbit voor chat, niet voor prefill-zware workloads.
  B blijft buiten de providers-registratie (het is een engine-start-arg,
  geen endpoint).

## Openstaande vragen (voor implementatie)

- IP-adres + poort van de Windows-PC (placeholder `192.168.1.x:11440`).
- Welke modellen op Windows (welke GGUFs, hoeveel VRAM op de Windows-GPU).
- Wel of geen `--api-key` op de Windows-llama-server (aanbevolen: wel).
- Providernaam (`windows`? `lan`? per-box `win1`/`win2`?).

## Verificatieplan (als het ooit gebouwd wordt)

1. Windows-llama-server draait; vanaf deze server:
   `curl http://<win-ip>:11440/v1/models` → 200 met model-id.
2. `POST /api/cloud/catalog/refresh` → provider verschijnt in
   `/api/cloud/catalog` met `credential_status: ok`.
3. `windows/<model>` chat-completions (non-stream + stream) → 200, capture
   `request_completed` in de WAL (raw, cloud-pad).
4. Pre-restart gate + `verify_post_restart.py` blijven groen.
