# OpenRouter-parity model metadata

Design contract for the model-discovery surface: `/v1/models`,
`/v1/models/{model_id}` and the new `/v1/models/{model_id}/endpoints`.

The goal is that a client asking Guardian about a model gets the same
*information* and the same *presentation shape* it would get from OpenRouter —
including for models whose own upstream (Google, OpenAI, NVIDIA, …) advertises
almost nothing, because a reference catalog fills the gaps.

This document is the frozen interface for the implementation. Field names and
semantics come from OpenRouter's public API; Guardian-native keys are preserved
so existing clients keep working.

## 1. Address model and identity key

- Guardian address: `{provider}/{brand}/{model}` (e.g.
  `openrouter/anthropic/claude-sonnet-4.6`, `openai/openai/gpt-4o`).
- **Identity key** for cross-provider joins: everything after the provider
  segment — `{brand}/{model}` (e.g. `openai/gpt-4o`).
- The identity key is what the reference catalog is keyed on. Two providers
  genuinely serving the same model therefore share metadata.

## 2. Reference catalog (cross-provider gap filling)

A provider may declare additional metadata sources in its provider settings
file (`config/providers/<name>.settings.yaml`):

```yaml
reference_catalog:
  # Optional list; each entry is one reference source.
  sources:
    - name: openrouter
      url: https://openrouter.ai/api/v1/models
      enabled: true
      ttl_seconds: 86400
      send_api_key: false
```

Rules:

- A reference source is fetched **independently** of `catalog_url`. It never
  affects discovery, routing, allowlists or which models are advertised. It is
  pure reference data.
- Fetched entries are stored keyed by their own upstream model id
  (OpenRouter ids are already `{brand}/{model}`, so the key is the identity key
  directly).
- Persisted to `data/model_reference_catalog.json` with `fetched_at` and a
  `source` signature (url), so a url change invalidates the cache.
- Multiple sources merge: the first source that provides a non-null value for a
  field wins; later sources only fill gaps.
- Fetch failures keep the last successful catalog (same fail-open policy as
  `CloudModelCatalog`). Reference data must never break discovery.

## 3. Per-model field contract

Every **model** entry returned by `/v1/models` and `/v1/models/{model_id}`
carries these keys. Keys are always present; unknown values are `null` (this is
OpenRouter's own convention — it returns `"knowledge_cutoff": null` rather than
omitting the key).

**One deliberate exception: `failover/{group}` entries.** A failover group is a
*route*, not a model — it is a synthetic address spanning several providers, so
it has no single upstream advertisement, no single price and no single context
window to report. Those three entries keep their own documented shape
(`{id, object, created, owned_by, permission, served_by, provider,
failover_group}` plus Guardian's context aliases) and carry **no** parity keys,
rather than a skeleton of fifteen `null`s that says nothing. A client that wants
failover metadata asks the endpoints surface, where each candidate is described
on its own:
`GET /v1/models/failover/{group}/endpoints`. This was verified to be the only
exception: of 288 live `/v1/models` entries, exactly the 3 `failover/*` ones
lack the parity keys.

| Field | Type | Source precedence |
| --- | --- | --- |
| `id` | str | Guardian address (existing) |
| `object` | `"model"` | existing |
| `created` | int (epoch) | upstream → reference → request time |
| `owned_by` | str | existing (provider name) |
| `permission` | list | existing (`[]`) |
| `served_by` | str | existing (`cloud` / `local` / `failover`) |
| `provider` | str | existing |
| `canonical_slug` | str\|null | upstream → reference |
| `hugging_face_id` | str\|null | upstream → reference |
| `name` | str | upstream → reference → derived from identity key |
| `description` | str\|null | upstream → reference |
| `context_length` | int | provider config override → upstream → reference → resolved context |
| `architecture` | object | see §3.1 |
| `pricing` | object\|null | see §3.2 |
| `top_provider` | object | see §3.3 |
| `per_request_limits` | object\|null | upstream → reference |
| `supported_parameters` | list[str] | see §3.4 |
| `default_parameters` | object | upstream → reference → `{}` |
| `knowledge_cutoff` | str\|null | upstream → reference |
| `expiration_date` | str\|null | upstream → reference |
| `links` | object | `{"details": "/v1/models/{id}/endpoints"}` — always Guardian's own route |
| `reasoning` | object | **Optional** — see §3.5 |
| `metadata_sources` | object | Guardian-native provenance map, always present (`{}` when nothing was filled in), see §5 |

Existing Guardian-native keys stay untouched and orthogonal:
`context`, `max_context`, `benchmark_context_limit`, `max_input_tokens`,
`advertised_context`, `meta.n_ctx`, `input_modalities` (top-level, Guardian's
own), `configured_input_modalities`, `vision`, `failover_group`.

### 3.1 `architecture`

```json
{
  "modality": "text+image->text",
  "input_modalities": ["text", "image"],
  "output_modalities": ["text"],
  "tokenizer": "Claude",
  "instruct_type": null
}
```

- Precedence: upstream advertisement → reference catalog → Guardian local
  capability (vision/`mmproj` config, `model_type`, and llama-server's
  `--embedding` flag in `extra_args`).
- A local route whose capability is unknown must not default to `text->text`:
  that claims the route returns text, when an embedding model returns a vector.
  Guardian therefore treats `--embedding` as an explicit embedding-model signal
  even when the config omits `model_type`.
- `modality` is synthesized from the modality lists when only the lists are
  known, and the lists are parsed out of a `text+image->text` string when only
  the string is known (both directions, so a reference entry can complete a
  partial upstream entry).
- `tokenizer` / `instruct_type` are `null` when unknown. Never guessed.
- An entry with no modality information at all gets
  `{"modality": null, "input_modalities": null, "output_modalities": null,
  "tokenizer": null, "instruct_type": null}` — not an omitted key.
- **Blank is not a value.** The catalog boundary already normalises an empty or
  whitespace-only string to `null` (`CloudModelCatalog.extract_model_metadata`),
  which is how OpenRouter's `"hugging_face_id": ""` becomes `null` here. The
  presentation layer additionally treats an empty string *inside* a nested block
  (`modality`, `tokenizer`, …) as a gap that the reference may fill, because
  `""` would otherwise block the list-derived modality. Both paths therefore
  report `null` for "nothing advertised"; the difference is only reachable by a
  caller handing in an un-normalised dict.

### 3.2 `pricing`

OpenRouter's shape: a flat map of **string** decimal values per token
(`"prompt": "0.000003"`). Guardian passes the route's own advertised values
through verbatim, and synthesizes only where it is factually grounded:

- **Local models** (`served_by: "local"`, or the local/managed providers): all
  known keys `"0"`, plus `"local": "true"`. Local inference costs nothing —
  this is a fact, not a guess.
- **Cloud models whose own provider advertises pricing**: passthrough of the
  advertised keys, values untouched.
- **Everything else**: `null`.

**The reference catalog is deliberately NOT a pricing source.** This is the one
field where the cross-provider fill rule of §2 does not apply, and the
distinction is principled:

- `architecture`, `supported_parameters`, `top_provider`, `description` and
  friends are properties of the **model**. OpenRouter's entry for
  `openai/gpt-4o` describes the same model Google or NVIDIA would serve, so
  filling those across providers is sound.
- `pricing` is a property of the **route**. OpenRouter charges $2.50/M tokens
  for `openai/gpt-4o`; NVIDIA serves it on a free tier for nothing. Copying
  OpenRouter's price onto an `nvidia/openai/gpt-4o` entry would state something
  false about what that route costs.

Guardian must therefore **never** borrow another provider's price for a model
it does not serve, and must never invent a number. `null` is the correct answer
for "unknown".

Note the asymmetry with `top_provider`: its `context_length` is model-shaped and
may be filled from the reference, while `is_moderated` describes the *route* and
must not be.

### 3.3 `top_provider`

```json
{"context_length": 1048576, "max_completion_tokens": 128000, "is_moderated": null}
```

- `context_length`: the context Guardian itself resolved for the model.
- `max_completion_tokens`: provider config override (`max_tokens`) → upstream
  `top_provider.max_completion_tokens` → reference → `null`.
- `is_moderated`: the route's **own** upstream advertisement only → `null`.
  Never the reference catalog — moderation is a property of the route, not of
  the model (see §3.2).

### 3.4 `supported_parameters`

- Upstream advertisement → passthrough verbatim.
- Reference catalog → used to fill when the upstream advertises nothing (this
  is the headline cross-provider win: OpenAI's `/v1/models` says nothing about
  tools or reasoning, OpenRouter's entry for the same model does).
- Local models → derived from what Guardian's local path genuinely forwards:
  `max_tokens`, `temperature`, `top_p`, `top_k`, `stop`, `seed`,
  `frequency_penalty`, `presence_penalty`, `response_format`,
  `structured_outputs`. `grammar` is added only when the model's config enables
  grammar decoding; `tools` / `tool_choice` only when the model config declares
  tool support. Never advertise a capability Guardian would drop.
- Unknown → `null`.

### 3.5 `reasoning`

`reasoning` is the **only optional key** in this contract, and it stays that way
for backwards compatibility: it is *absent* when no source advertises reasoning
metadata, because providers that expose no reasoning-effort information should
carry no such field at all. When present it keeps the documented reduced shape —
`supported_efforts`, `default_effort`, `mandatory`, `default_enabled` — and any
other key an upstream block happens to carry is dropped, never forwarded.

Precedence: the route's own upstream advertisement wins; a reference-derived
block is used only when the route advertises none. `supported_efforts` must be a
non-empty list of strings, otherwise the field is treated as absent.

## 4. Endpoints surface

`GET /v1/models/{model_id}/endpoints` mirrors OpenRouter's
`/api/v1/models/{author}/{slug}/endpoints`:

```json
{
  "data": {
    "id": "openrouter/anthropic/claude-sonnet-4.6",
    "name": "Anthropic: Claude Sonnet 4.6",
    "created": 1771342990,
    "description": "...",
    "architecture": { ... },
    "endpoints": [ ... ]
  }
}
```

Guardian's `endpoints` are the concrete routes Guardian can actually use for
that model — the honest analogue of OpenRouter's upstream provider list:

- a **cloud model** yields one endpoint per provider whose catalog contains the
  same identity key (so a model served by both `openrouter` and `nvidia` shows
  both routes), plus the local backend when a local provider serves it;
- a **`failover/{group}`** address yields one endpoint per configured
  candidate;
- a **local model** yields its local backend endpoint.

Per-endpoint keys (OpenRouter parity, `null` where Guardian has no grounded
value):

`name`, `model_id`, `model_name`, `context_length`, `pricing`, `provider_name`,
`tag`, `quantization`, `max_completion_tokens`, `max_prompt_tokens`,
`supported_parameters`, `supports_tool_choice`, `native_tools`,
`supports_implicit_caching`, `supports_image_reference`, `status`,
`uptime_last_30m`, `uptime_last_5m`, `uptime_last_1d`, `latency_last_30m`,
`throughput_last_30m`.

- `status`: `0` when healthy, non-zero when the provider is known-degraded
  (Guardian's failover health tracker / rate-limit state). `null` when Guardian
  has no health signal for that route.
- `supports_tool_choice`: OpenRouter uses
  `{"none": bool, "auto": bool, "required": bool, "function": bool}`.
  Guardian derives it from whether the route genuinely supports tools.
- `native_tools`: OpenRouter's provider-native tool map. Guardian has no such
  concept for most routes → `null`, unless a provider config declares it.
- `model_id` is the **route's own upstream model id** — what Guardian actually
  sends to that provider (`models/gemma-4-31b-it` for google,
  `anthropic/claude-sonnet-4.6` for openrouter). This is a deliberate divergence
  from OpenRouter, which repeats the logical model id in every endpoint: for a
  gateway, "what will be sent upstream on this route" is the operationally
  useful answer, and the display name is still carried in `model_name` and
  `name`. The logical model id is the top-level `data.id`.
- uptime/latency/throughput: Guardian does not currently collect per-provider
  time series. Emit `null` rather than fabricating.
- `tag`: the provider's route tag (e.g. `azure/global`); Guardian uses the
  provider name plus, for failover candidates, the group name.

Errors: unknown model → `404` with the same body shape the other model routes
use. A model the requesting key may not use (cloud access off) → `403`.

## 5. Provenance

Guardian additionally reports **where each synthesized section came from**, in a
Guardian-native key, so an operator can tell an upstream fact from a
cross-provider fill:

```json
"metadata_sources": {
  "architecture": "reference:openrouter",
  "pricing": "local",
  "supported_parameters": "reference:openrouter",
  "name": "derived"
}
```

Values: `upstream`, `override`, `reference:<source-name>`, `local`, `derived`.
Only sections whose value did not come from the route's own upstream are listed;
a section absent from the map came from the upstream itself.

The key itself follows the same always-present rule as the rest of the contract:
a route whose every section came from its own upstream advertisement reports
`"metadata_sources": {}`, never an omitted key. `{}` is a statement — "nothing
here was filled in from elsewhere" — and a client should not need a presence
check to read it.

## 6. Backwards compatibility

- Purely additive. Every key that exists today keeps its name, type and
  meaning.
- `object`, `permission`, `owned_by`, `created` keep their current values.
- The failover synthetic entry shape is preserved.
- Cloud access gating is unchanged: `cloud_gateway_access: false` hides cloud
  entries and returns `403`.
- The reference catalog must not change which models are advertised — the
  OpenRouter provider keeps `catalog_url: /models/user` (account-reachable
  models only) while the reference source uses the public `/models`.
