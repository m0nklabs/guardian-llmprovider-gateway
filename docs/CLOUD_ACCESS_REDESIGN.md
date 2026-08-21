# Cloud Access Redesign — One Config, One Key Source, Dynamic Catalog

> Status: **IMPLEMENTED** (code+config merged on `cloud-access-redesign`, awaiting
> operator-run restart to go live). Design record + working plan for the
> cloud-access refactor. Created 2026-08-20, implemented 2026-08-21. This doc is
> the source of truth for the redesign. Referenced from `AGENTS.md`.

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
3. **The cloud model name is inconsistent** across providers: openrouter/nvidia
   store their models with the brand segment (`openai/gpt-4o`,
   `minimaxai/minimax-m3`) but openai/google store them without (bare `gpt-4o`,
   bare `gemini-3.5-flash`) → `openai/gpt-4o` (2 segments) vs
   `nvidia/minimaxai/minimax-m3` (3). The `{brand}` layer should always be
   present — so the target format is always `google/google/gemini-3.5-flash`,
   never the bare `gemini-3.5-flash` or the 2-segment `google/gemini-3.5-flash`.
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
  `{provider}/{brand}/{model}` (cloud) and `{local}` (local). Note: no
  `guardian/` prefix — it was only namespace-for-clarity, and dropping it means
  existing bare-name clients keep working unchanged (see Backward compatibility).
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

## Per-Key Access (replaces linking)

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
    `{provider}/{brand}/{model}` route is consistent for every provider.
  - Cache the result with a TTL + **auto-refresh** (background). On refresh
    failure, keep the last successful list (like today's google fallback).
- `config/cloud_models.yaml` supplies per-model **overrides** (e.g. context
  window, thinking cap, tool support) layered above the default template.
  This is not a hand-maintained catalog — only exceptions from defaults.

## Model Addressing (the norm)

> The `guardian/` prefix is dropped. It was only there for namespace clarity,
> and removing it keeps every existing bare-name client working (they already
> address cloud models as `openrouter/deepseek/deepseek-v4-flash-0731`, etc.).
> Cloud model access is gated by the per-key `cloud_gateway_access` boolean —
> not by the address — so the prefix provides no security.

| Model class | Format | Example |
|---|---|---|
| Cloud | `{provider}/{brand}/{model}` | `google/google/gemini-3.5-flash`, `openrouter/openai/gpt-4o`, `openai/openai/gpt-4o`, `nvidia/minimaxai/minimax-m3` |
| Local | `{local}` | `llama3.2-3b` |

- **Provider** = who serves the API (`openrouter`, `nvidia`, `openai`, `google`, …).
- **Brand** = the model's actual maker namespace (`openai`, `google`, `minimaxai`, …).
- Provider and brand are independent and may coincide (`google/google/…`,
  `openai/openai/…`).

### Backward compatibility (operator decision)

The addressing is **backward-compatible for the bare-name clients that are
actually in use**:

- **Cloud bare names that already carry the `{brand}` segment keep working**
  (`openrouter/deepseek/deepseek-v4-flash-0731`, `nvidia/minimaxai/minimax-m3`).
  The `guardian/` prefix is gone and the legacy per-key `guardian/{provider}/{model}`
  routes are removed with the credential layer, but the address these clients
  send does not change.
- **Google models are always written with the `google` brand.** Because google
  becomes a first-class provider, gemini models are consistently
  `google/google/gemini-3.5-flash` (never the bare `gemini-3.5-flash` or the
  2-segment `google/gemini-3.5-flash`).
- **Local:** models keep their bare name (`llama3.2-3b`) — unchanged.

If a client sends a name that no longer matches after the redesign, it is a
small, obvious fix on the client side (no migration tooling needed).

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
   `cloud_gateway_access` gate; consistent `{provider}/{brand}/{model}` cloud
   format; local bare names unchanged.
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

Active during planning. Refactor implementation is complete (2026-08-21,
commits `4329d7c` + `28e97ad` on `cloud-access-redesign`) — awaiting the
operator-run restart to go live. `AGENTS.md` handoff updated to
`20260820_cloud_refactor`.
