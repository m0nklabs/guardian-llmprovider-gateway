# LAN GPU Backends — plan (2026-08-26, operator-idee, nog NIET geïmplementeerd)

> Status: **plan only** (operator-keuze 2026-08-26: "alleen plan vastleggen").
> Doel: de GPU van de Windows-PC inzetten naast de lokale GPU.

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

- **Nu: Optie A** (meerdere modellen parallel op LAN) — past bij het doel,
  minimale Guardian-impact, geen netwerkgevoeligheid.
- **Later optioneel: Optie B** voor één model dat nergens past — haalbaar
  op 1 Gbit voor chat, niet voor prefill-zware workloads.

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
