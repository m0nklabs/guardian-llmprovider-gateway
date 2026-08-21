# Cloud Access Redesign — One Config, One Key Source, Dynamic Catalog

> Status: **PLAN** (not yet implemented). Design record + working plan for the
> cloud-access refactor. Created 2026-08-20. This doc is the source of truth
> for the redesign; implementation follows it. Referenced from `AGENTS.md`.

## Why

The current cloud-access layer grew organically and is confusing and leaky:

1. **Cloud provider API keys live in more than one place.** `settings.yaml`
   (`providers.*.api_key` via `$ENV`) and `cloud_keys.json` (credentials). The
   `.env` is missing `POOLSIDE_API_KEY` and any `GOOGLE_API_KEY`, yet those
   providers *are* configured in `cloud_keys.json` — and only reachable when a
   Guardian API key is linked to the provider. Messy.
2. **Duplicate/inconsistent model listings.** `/v1/models` shows local models,
   bare cloud names from `settings.yaml`, `openrouter/` aliases, and per-key
   `guardian/{provider}/{model}` routes. Same model shows up 3–4 times
   (`openai/gpt-4o` → `openai/gpt-4o`, `openrouter/openai/gpt-4o`,
   `guardian/openrouter/openai/gpt-4o`, `guardian/openai/gpt-4o`).
3. **The `guardian/{provider}/{model}` route is inconsistent** across
   providers: openrouter/nvidia store their models with the brand segment
   (`openai/gpt-4o`, `minimaxai/minimax-m3`) but openai/google store them
   without (`gpt-4o`, `gemini-3.5-flash`) → `guardian/openai/gpt-4o` (3
   segments) vs `guardian/nvidia/minimaxai/minimax-m3` (4). The `{brand}` layer
   should always be present.
4. **The credential-linking layer is not a security boundary.** A Guardian
   API key with *no* linked credential can still call any bare cloud model from
   `settings.yaml` using the global provider key (`resolve_cloud_attempts` only
   checks the fingerprint for `guardian/...` routes). So the whole
   link/ownership machinery gives a false sense of per-key authorization.
5. **Mixed config formats** (`*.json` and `*.yaml`) and a
   hardcoded/hand-maintained model catalog that goes stale.

## Goals

- **One config format** (all YAML).
- **One source of truth for cloud provider API keys.**
- **No credentials/links/ownership layering** — replaced by one simple boolean
  per Guardian key.
- **Dynamic cloud model catalog** (fetched from each provider's own
  `/v1/models`, cached + auto-refresh) instead of hand-maintaining a list.
- **A single, consistent model-addressing format**:
  `guardian/{provider}/{brand}/{model}` (cloud) and `guardian/{local}` (local).
- **Google added as a first-class provider** (currently absent from
  `settings.yaml`, only a brand under openrouter).

## Target Config Layout

| Old | New | Role |
|---|---|---|
| `config/models.yaml` | `config/local_models.yaml` | Local GGUF models + aliases. Old `models.yaml` name still loads (backward-compat). |
| `config/api_keys.json` | `config/guardian_apikeys.yaml` | Named Guardian API keys, each with `cloud_gateway_access: true` (default). No secrets beyond the key itself. |
| `config/cloud_keys.json` | **removed** | No credentials/links/ownership. |
| — | `config/cloud_models.yaml` | Per-model **overrides** (context, capability, …) on top of the default template. NOT the catalog itself. |
| — | `data/cloud_catalog_cache.json` | Cached per-provider model lists (TTL + auto-refresh). Runtime data, gitignored. |
| `config/settings.yaml` | `config/settings.yaml` | **Only** cloud keys via `$ENV`. Provider defs (base_url, api_key, model_prefixes) stay here. Add `google` provider. |

## Key Source (one truth)

- All cloud provider API keys live in **`config/settings.yaml`** →
  `providers.<name>.api_key` → `${ENV_VAR}` → `.env`.
- Add missing `.env` vars: `POOLSIDE_API_KEY` and `GOOGLE_API_KEY` (and any
  others discovered missing during implementation).
- `cloud_keys.json` is deleted. Its per-key linking/ownership layer is gone.

## Per-Key Per-Key Access (replaces linking)

- Each Guardian API key (in `guardian_apikeys.yaml`) gets
  `cloud_gateway_access: true|false` (default `true`).
- `true` → the key may call any cloud model via the global provider keys.
- `false` → the key is local-only (cloud routes rejected).
- No per-provider linking, no ownership, no claim/repair.

## Dynamic Cloud Catalog

New module `app/proxy/cloud_catalog.py`:

- For each configured provider (openrouter, nvidia, openai, poolside, **google**):
  - Fetch its OpenAI-compatible `/v1/models` using the settings provider key.
  - Normalize each model id to `{brand}/{model}` so the
    `guardian/{provider}/{brand}/{model}` route is consistent for every provider.
  - Cache the result with a TTL + **auto-refresh** (background). On refresh
    failure, keep the last successful list (like today's google fallback).
- `config/cloud_models.yaml` supplies per-model **overrides** (e.g. context
  window, thinking cap, tool support) layered above the default template.
  This is not a hand-maintained catalog — only exceptions from defaults.

## Model Addressing (the norm)

| Model class | Format | Example |
|---|---|---|
| Cloud | `guardian/{provider}/{brand}/{model}` | `guardian/google/google/gemini-3.5-flash`, `guardian/openrouter/openai/gpt-4o`, `guardian/openai/openai/gpt-4o`, `guardian/nvidia/minimaxai/minimax-m3` |
| Local | `guardian/{local}` (new primary) | `guardian/llama3.2-3b` |

- **Provider** = who serves the API (`openrouter`, `nvidia`, `openai`, `google`, …).
- **Brand** = the model's actual maker namespace (`openai`, `google`, `minimaxai`, …).
- Provider and brand are independent and may coincide (`guardian/google/google/…`,
  `guardian/openai/openai/…`).

### Backward compatibility (operator decision)

- **Cloud:** strict new format only. Bare cloud names (`openai/gpt-4o`,
  `moonshotai/kimi-k3`) are **removed** from the listing and no longer accepted
  as router inputs. Clients must use `guardian/{provider}/{brand}/{model}`.
- **Local:** `guardian/{local}` is the new primary, but the old bare name
  (`llama3.2-3b`) stays accepted as an alias so existing local clients don't break.

## What Is Removed

- `CloudCredentialStore` (`app/proxy/cloud_keys.py` credentials/links/ownership).
- Credential CRUD + `link`/`unlink`/`claim`/`refresh` endpoints
  (admin_api, main.py, UI hooks in `app/ui/index.html`).
- `get_linked_models_for_key`, `get_credential_for_key`, owner-scoped access
  logic. Routing uses the settings provider key directly, gated by
  `cloud_gateway_access`.
- Tests: `tests/unit/test_cloud_keys.py` (full suite) and
  `tests/unit/test_cloud_keys_claim.py` are replaced by catalog + gating tests.

## Affected Modules

- `app/proxy/cloud_keys.py` — deleted.
- `app/proxy/providers.py` — cloud recognition stays; provider resolution reads
  the catalog (not per-key links).
- `app/proxy/cloud_catalog.py` — **new**.
- `app/cloud_inference/routing.py` — drop per-key credential lookup; route via
  settings key, gate on `cloud_gateway_access`.
- `app/cloud_inference/forwarding.py` — same (drop credential linkage).
- `app/gateway/model_discovery.py` — build `/v1/models` from catalog + local
  models in the unified format.
- `app/gateway/admin_api.py` — remove credential endpoints; add catalog refresh.
- `app/main.py` — wiring, remove cloud_keys store init + credential UI routes.
- `app/proxy/server.py` — route wiring.
- `app/gateway/context_metadata.py` — resolve context from `cloud_models.yaml`
  overrides + catalog.
- `app/ui/index.html` — remove credential UI, reflect new gating.
- `app/config_loader.py` — load new YAML config + accessor updates.

## Implementation Order (when approved)

1. **Config rename** (safe, non-routing): `models.yaml`→`local_models.yaml`,
   `api_keys.json`→`guardian_apikeys.yaml` (both with backward-compat loaders),
   add google provider def to `settings.yaml`, add missing `.env` vars.
2. **`cloud_catalog.py`** (new module): fetch/cache/auto-refresh, brand
   normalization, `cloud_models.yaml` overrides.
3. **Routing ombouw**: replace `get_credential_for_key` with settings-key +
   `cloud_gateway_access` gate; strict cloud format; local `guardian/{local}`.
4. **model_discovery/admin/UI opschoning**: unified listing, drop credential
   CRUD, add catalog refresh.
5. **Tests**: drop the two old cloud_keys suites, add catalog + gating tests.
6. **Pre-restart gate** + operator-run restart (session drops — agent routes
   through Guardian).

## Open Items / Decisions to Confirm

- Exact set of `.env` vars that are missing (POOLSIDE, GOOGLE, others?). Operator
  supplies values.
- Whether `cloud_models.yaml` overrides model-catalog TTL choices should be
  operator-configurable from the start (default: sane hardcoded TTL).

## AGENTS.md Status

Active during planning. Refactor implementation is NOT started until operator
approves this plan. Once approved, execute in the order above and then mark this
doc `IMPLEMENTED` and update `AGENTS.md` handoff to `20260820_cloud_refactor`.
