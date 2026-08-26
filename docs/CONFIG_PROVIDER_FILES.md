# Per-provider configuratiebestanden — plan (2026-08-26, GEÏMPLEMENTEERD)

> Status: **geïmplementeerd** (2026-08-26, PR #3 / issue #1 fase F2).
> Doel bereikt: één configuratiebestand per provider in plaats van de huidige
> defaults/overrides-split tussen lokale en cloud-settings. Het
> local/cloud-onderscheid is dan aan de **providernaam** te zien
> (`ai-kvm2-local`, `14700k-local` vs `openrouter`, `google`, …).
> Zie `docs/CONFIG_SCHEMA.md` (migratietabel) voor de eindstand.

## Huidige situatie (geverifieerd 2026-08-26)

| Bestand | Inhoud | Probleem |
|---|---|---|
| `config/providers.settings.yaml` | provider defaults: base_url, api_key, timeout, model_prefixes, enabled | gescheiden van z'n overrides |
| `config/providers.overrides.yaml` | per-provider overrides: catalog_url, catalog_allowlist (nvidia 40) | overrides leven apart van de provider |
| `config/models.local.settings.yaml` | lokale registry: 23 modellen (path, context, ngl, kv_type, tensor_split, extra_args, switch policy) | apart van de lokale provider |
| `config/models.cloud.overrides.yaml` | per-model: context_window + model_defaults (max_tokens/temperature/top_p/seed) voor cloud | apart van de cloud-providers |
| `config/global.settings.yaml` | cross-cutting: proxy, queue, capture, failover, scaler, grammar, services, benchmark | blijft (geen provider-gebonden) |
| `config/guardian.keys.yaml` | named API keys | blijft (geen provider-gebonden) |

Lezers die op deze layout draaien: `app/paths.py` (pad-helpers),
`app/config_loader.py` (deep-merge providers.settings + overrides),
`app/proxy/providers.py` (`_load_settings_config`, derive context_overrides
uit `models.cloud.overrides.yaml`), `app/engine/manager.py` +
`app/local_inference/models.py` (models.local), `app/gateway/context_metadata.py`
+ `app/cloud_inference/routing.py` (context_window / model_defaults via
`get_override`), plus `scripts/*` via `local_models_file()` /
`models_cloud_overrides_file()`.

## Doelbeeld

```
config/
├─ global.settings.yaml          # cross-cutting (ongewijzigd)
├─ guardian.keys.yaml            # keys (ongewijzigd)
└─ providers/                    # NIEUW: één bestand per provider
   ├─ ai-kvm2-local.settings.yaml    # = vroegere models.local.settings.yaml
   │                                 #   + lokale provider-blok (base_url, management_url)
   ├─ 14700k-local.settings.yaml     # Windows-host (later, zelfde vorm)
   ├─ openrouter.settings.yaml       # base_url + api_key + prefixes + catalog_url
   │                                 #   + per-model overrides (was models.cloud.overrides.yaml)
   ├─ nvidia.settings.yaml
   ├─ google.settings.yaml
   ├─ openai.settings.yaml
   ├─ poolside.settings.yaml
   └─ groq.settings.yaml
```

**Vorm van een provider-bestand** (alles van die provider op één plek):

```yaml
# openrouter.settings.yaml
enabled: true
base_url: https://openrouter.ai/api/v1
api_key: ${OPENROUTER_API_KEY}
timeout_seconds: 1200
model_prefixes: [anthropic/, openai/, …]
catalog_url: /models/user
models:                       # per-model overrides (was models.cloud.overrides.yaml)
  deepseek/deepseek-v4-flash-0731:
    context_window: 1048576
  gpt-4o:
    max_tokens: 4096
    temperature: 0.7
```

```yaml
# ai-kvm2-local.settings.yaml
enabled: true
base_url: http://127.0.0.1:11440/v1
management_url: http://127.0.0.1:11441   # manager-contract (later; zie GATEWAY_MANAGER_SPLIT.md)
local: true                               # expliciete marker (naast de naam-suffix)
models:                       # was models.local.settings.yaml
  llama3.2-3b:
    path: /home/flip/models/llama3.2-3b.gguf
    total_layers: 28
    context: 131072
    ngl: 99
    kv_type: f16
    grammar_decoding: true
  …
```

**Overrides verdwijnen als apart bestand** — de defaults/overrides-merge
laag vervalt; één bestand is de waarheid. Secrets blijven `${VAR}` in het
bestand. Optioneel later: per provider een `*.overrides.yaml` ernaast voor
omgeving-specifieke deltas, maar niet nu.

## Wat verandert in de code (inschatting, NIET begonnen)

1. **`app/paths.py`**: `PROVIDERS_DIR = CONFIG_DIR / "providers"`,
   helpers `provider_settings_file(name)` + `provider_names()` (scan
   `*.settings.yaml`); `local_models_file()` blijft bestaan maar resolvert
   naar `providers/ai-kvm2-local.settings.yaml` (compat voor scripts/tests).
2. **`app/config_loader.py`**: deep-merge van twee bestanden → directory-scan
   (per provider één dict, geen merge-laag meer).
3. **`app/proxy/providers.py`**: `_load_settings_config` leest de directory;
   `context_overrides` / `model_defaults` komen uit het `models:`-blok van
   de betreffende provider i.p.v. uit `models.cloud.overrides.yaml`.
4. **`app/engine/manager.py` + `app/local_inference/models.py`**: models
   uit `providers/ai-kvm2-local.settings.yaml` (via helper; de manager blijft
   dezelfde lezer gebruiken).
5. **`app/gateway/context_metadata.py` + `app/cloud_inference/routing.py`**:
   `get_override` leest het provider-bestand.
6. **Tests**: `tests/legacy` single-file settings_path blijft werken (de
   explicit-path-constructor blijft); nieuwe unit-test voor de directory-scan
   + naam-herkenning (`*-local` → lokale provider).
7. **Migratie**: inhoud is bekend en klein (6 providers, 23 lokale modellen,
   10 cloud overrides) — handmatige split of één migrationscript, daarna de
   oude 4 bestanden verwijderen.

## Naamgeving / adressering (open punten)

- Providernamen worden `ai-kvm2-local` en `14700k-local` (operator 2026-08-26).
- **Blijven lokale aliases bare-name werken** (bv. `llama3.2-3b` zonder
  prefix)? Voorstel: ja — aliases blijven zoals nu; de provider-naam is de
  register-naam. Volledige adressering `ai-kvm2-local/llama3.2-3b` kan
  optioneel (zelfde patroon als `{provider}/{brand}/{model}` bij cloud).
- `local: true` als expliciete marker naast de naam-suffix (robust tegen
  hernoemen; routing gebruikt de marker, niet de string).

## Verhouding tot andere plannen

- Dit is de **config-laag** van de provider-unificatie
  (`docs/LAN_GPU_BACKENDS.md` + `docs/GATEWAY_MANAGER_SPLIT.md`): zodra
  `local` een passieve provider-entry is, past de per-provider config
  daarop; de `management_url`-velden hangen samen met de manager-split.
- `docs/CONFIG_SCHEMA.md` (migratietabel) moet worden bijgewerkt zodra dit
  gebouwd wordt: `providers.settings.yaml` / `providers.overrides.yaml` /
  `models.local.settings.yaml` / `models.cloud.overrides.yaml` → vervangen
  door `providers/*.settings.yaml`.

## Volgorde (voorstel)

0. (eventueel) Fase 0 van de manager-split: registry/keuze/discovery
   ontdraaien — dan is de config-split onafhankelijk bouwbaar.
1. **Config-split zelf**: `providers/`-directory + lezers ombouwen + tests
   + migratie van de 4 bestanden. Config-only voor de lezers? Nee — code
   (paths/config_loader/providers) → gate + restart.
2. Daarna pas de manager/Windows-stappen — de 14700k-local-provider kan dan
   puur als nieuw provider-bestand bijkomen.

## Verificatieplan (als het gebouwd wordt)

1. Full pytest + pre-restart gate groen.
2. Na restart: `/v1/models` identiek aan vóór de migratie (zelfde lokale
   aliases + zelfde cloud-entries + zelfde context-metadata).
3. `openrouter/…` en lokaal alias → 200 (zelfde routing als voorheen).
4. Catalog-refresh blijft werken; `credential_status` per provider klopt.
5. Nieuwe provider toevoegen = één bestand in `config/providers/` + hot-
   reload (geen restart) — de pay-off van de nieuwe layout.
