# Guardian Config Schema — plan

> Status: **PLAN** (2026-08-21)
> Doel: `config/settings.yaml` strippen tot elke setting een eigen, duidelijk
> bestand heeft — geen "alles.yaml" meer. Naamgeving: domain-first, sorteerbaar,
> zelfbeschrijvend (operator: "bestandnamen moeten aan de buitenkant al duidelijk
> zijn, makkelijk sorteerbaar, dir-benamingen als een pro").

## 1. Naamgevingsconventie

Patroon: `<domain>.<kind>.<scope?>.yaml`

- **domain** — waar het over gaat: `global` (global infra/subsystem), `providers`
  (cloud gateways), `models` (modellen), `guardian` (het product zelf: API keys).
- **kind** — `settings` (defaults) of `overrides` (afwijkingen). Naam zegt het.
- **scope** (alleen bij models) — `local` of `cloud`.

Sorteerbaar: per domain gegroepeerd (global → guardian → models → providers),
binnen models cloud vóór local. Je ziet in één oogopslag wat defaults vs afwijkingen is.

```
config/
├─ .env                            # SECRETS + machine-paden (${VAR}-expansie)
├─ global.settings.yaml            # GLOBAL: proxy/queue/timeouts/scaler/capture/grammar/cloud_retry/failover/benchmark/services
├─ guardian.keys.yaml              # Guardian API keys (36, cloud_gateway_access)
├─ models.cloud.settings.yaml      # CLOUD MODEL DEFAULTS (context/thinking/sampling)
├─ models.cloud.overrides.yaml     # CLOUD MODEL AFWIJKINGEN (exceptions)
├─ models.local.settings.yaml      # LOKALE MODEL DEFAULTS (registry: models/aliases/guardian)
├─ models.local.overrides.yaml     # LOKALE MODEL AFWIJKINGEN
├─ providers.settings.yaml         # PROVIDER DEFAULTS (5: base_url/api_key/timeout/model_prefixes/catalog_url)
└─ providers.overrides.yaml        # PROVIDER AFWIJKINGEN (bv. openrouter catalog_url=/models/user)
```

## 2. Volledig config-landschap (huidig → nieuw)

| Huidig | Nieuw | Rol |
|---|---|---|
| `.env` | `.env` (ongewijzigd) | secrets + machine |
| `guardian_apikeys.yaml` | `guardian.keys.yaml` | Guardian keys |
| `local_models.yaml` (+ symlink `models.yaml`) | `models.local.settings.yaml` | lokale registry |
| `cloud_models.yaml` | `models.cloud.overrides.yaml` | cloud model afwijkingen |
| *(nieuw)* | `models.cloud.settings.yaml` | cloud model defaults |
| *(nieuw)* | `models.local.overrides.yaml` | lokale model afwijkingen |
| `providers.*` in settings.yaml | `providers.settings.yaml` + `providers.overrides.yaml` | provider config |
| `settings.yaml` | `global.settings.yaml` | global infra + subsystem |
| `api_keys.json`, `cloud_keys.json` | *(verwijderen na migratie)* | legacy |
| `current_model.args/.sig`, `benchmark_models.json`, `data/cloud_catalog_cache.json` | ongew. | runtime, niet hand-editeerbaar |

## 3. Mapping: waar elke settings.yaml-key heen gaat

| Huidige key | Nieuwe plek |
|---|---|
| `providers` | `providers.settings.yaml` (defaults) + `providers.overrides.yaml` (afwijking) |
| `context_overrides` | `models.cloud.overrides.yaml` (het is model-override) |
| `proxy`, `services`, `services_to_stop`, `queue`, `timeouts`, `scaler`, `benchmark` | `global.settings.yaml` |
| `capture`, `grammar`, `cloud_retry`, `failover_health` | `global.settings.yaml` |
| `failover_groups` (failover.py) | `global.settings.yaml` |

## 4. Catalog-endpoint als provider-default

Per provider optioneel `catalog_url` (default `/models`):

```yaml
# providers.settings.yaml
providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}
    timeout_seconds: 1200
    model_prefixes: [...]
# providers.overrides.yaml — afwijkingen
providers:
  openrouter:
    catalog_url: /models/user   # alleen echt toegankelijke (guardrails/privacy gefilterd)
```

`app/proxy/cloud_catalog.py`: `url = f"{provider.base_url}{provider.catalog_url or '/models'}"`.

## 5. Wie leest de huidige settings.yaml (consumers-impact)

| Key | Reader |
|---|---|
| `providers` | `proxy/providers.py`, `proxy/ratelimit.py` |
| `proxy` | `config_loader.py`, `proxy/server.py` |
| `queue` | `main.py`, `config_loader.py`, `tweaker/legacy/benchmark_suite_v1.py` |
| `timeouts` | `config_loader.py`, `local_inference/models.py` |
| `scaler` | `proxy/scaler.py` |
| `capture` | `capture/config.py` |
| `grammar` | `gateway/normalization.py`, `config_loader.py`, `local_inference/ollama.py` |
| `cloud_retry` | `gateway/admin_api.py`, `config_loader.py`, `proxy/server.py` |
| `failover_health` | `gateway/admin_api.py`, `config_loader.py`, `proxy/server.py` |
| `context_overrides` | `proxy/providers.py` |
| `services` | `engine/manager.py` |
| `benchmark` | `scheduler/manager.py` |
| `services_to_stop` | `scheduler/manager.py` |
| `failover_groups` | `proxy/failover.py` |

`app/config_loader.py` = centrale leesswitch: merges de nieuwe files tot het
bestaande config-dict, zodat alle `.get("key")`-reads intact blijven.

## 6. Migratiestappen (operator: directe volledige cutoff)

1. **Backup**: `cp config/settings.yaml config/settings.yaml.bak` (rollback).
2. **Nieuwe files aanmaken** volgens §1, inhoud herverdeeld uit §3. Overrides
   en cloud-defaults starten als lege templates.
3. **Compat-symlinks** (kortstondig) zodat hardcoded `paths.py`-verwijzingen
   blijven werken: `models.yaml`, `local_models.yaml`, `cloud_models.yaml`,
   `settings.yaml`, `guardian_apikeys.yaml` → symlink naar nieuwe namen.
4. **`config_loader.py`**: lees + merge `global.settings.yaml` →
   `providers.settings.yaml` → `providers.overrides.yaml` (overrides winnen)
   tot hetzelfde config-dict. Alle consumers blijven werken.
5. **`providers.py`**: leest `providers.settings.yaml` + `providers.overrides.yaml`;
   `CloudProvider` krijgt `catalog_url`.
6. **`cloud_catalog.py`**: `catalog_url or "/models"`.
7. **`paths.py`**: update naar nieuwe bestandsnamen; symlinks na succesvolle
   liveswitch ingekort tot de echte namen en `settings.yaml.bak` verwijderd.
8. **`pre_restart_check.py`** (4 gates) vóór restart; daarna restart.
9. Recovery bij falen: `cp settings.yaml.bak config/settings.yaml` terug +
   legacy paden herstellen (geen self-heal — zie AGENTS.md).

## 7. Tests

- `test_config_reload.py`: leest + merge van de nieuwe files (global, providers
  settings+overrides), overrides winnen.
- `test_providers.py`: `CloudProvider.catalog_url` default `/models`, override
  gerespecteerd.
- `test_cloud_catalog.py`: `refresh_provider` gebruikt `catalog_url` indien gezet.
- Bestaande suite 100% groen (merged dict houdt top-level keys compatibel).

## 8. Risico's & mitigatie

- **Crash bij restart door gemiste reference** → directe cutoff (operators keus).
  Mitigatie: backup (§6.1), compat-symlinks (§6.3), merged-dict (§6.4), gate
  (§6.8), recovery (§6.9).
- **`/models/user` filter** → eerste `providers.overrides.yaml` entry: `catalog_url`.
- **`model_defaults` uit cloud_keys.json** (temp/top_p/max_tokens/seed) →
  `models.cloud.overrides.yaml` vóór verwijdering van cloud_keys.json.
