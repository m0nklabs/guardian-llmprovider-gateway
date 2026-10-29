# API Reference

For client-side integration patterns, queue ownership rules, and recommended
error handling, start with [CLIENT_INTEGRATION.md](CLIENT_INTEGRATION.md). This
document stays focused on the raw surface contract.

Per-route linkage (handler, auth dependency, enforced scope, covering test) for
every registered route is in [G0_ENDPOINT_AUTH_MATRIX.md](G0_ENDPOINT_AUTH_MATRIX.md);
`tests/unit/test_g0_endpoint_matrix.py` keeps the two in sync with the code
declaration in `app/proxy/scopes.py`. The scope model itself — vocabulary,
migration window, rotation design — is
[decisions/G1-01-privilege-scopes.md](decisions/G1-01-privilege-scopes.md).

## Ports and Auth Boundaries

| Port | Surface | Auth in current code |
| --- | --- | --- |
| `11434` | Guardian proxy and control plane | Required except `/healthz` and the FastAPI docs routes (`/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`); `/metrics` requires a key with the `client` scope |
| `11437` | Dashboard UI and stats API | UI assets are unauthenticated; every `/api/*` route requires a Bearer key with the route's scope (`admin`) |
| `11440` | Raw `llama-server` backend | Private Guardian-managed backend |

## Authentication

Guardian accepts any of these request styles on `:11434`:

- `Authorization: Bearer <token>`
- `x-api-key: <token>`
- `api-key: <token>`

Keys live in [../config/guardian.keys.yaml](../config/guardian.keys.yaml) (legacy
`api_keys.json` / `guardian_apikeys.yaml` are read as aliases).

Missing or invalid keys return `401 Unauthorized`.

### Privilege scopes (G1-01)

Every key carries zero or more scopes in its `metadata.scopes` list:

| Scope | Authorizes |
| --- | --- |
| `client` | inference, model discovery, own-request queue status/cancel |
| `operator` | `/admin/load`, `/admin/unload` (implies `client`) |
| `admin` | `/api/keys`, `/api/config/reload`, cloud catalog/scaler/session/status diagnostics (implies `operator`, `client`, `capture-export`) |
| `capture-export` | `/api/capture/status`, `/api/capture/rotate`, capture WAL export |

A request whose key lacks the route's scope returns a stable typed `403`
(see the error model). A key with **no** `metadata.scopes` list is a legacy key:
it keeps the old flat access until `auth.scopes_enforced_after`
(`config/global.settings.yaml`, shipped default `2026-11-11T00:00:00Z`,
hot-reloadable) and is a `client` key from that instant on. Each privileged call
it makes inside the window is logged (`scope-grace …`) and counted in
`guardian_scope_grace_calls_total`.

Example key entry:

```yaml
flip_0123456789abcdef0123456789abcdef:
  name: dashboard
  metadata:
    scopes: [admin]
```

## Common Queue Behavior

Queued inference responses include:

- `X-Request-Id`: Guardian UUID for the queued request
- `X-Queue-Wait-Ms`: time spent waiting before inference started

Guardian keeps a live queued request waiting until one of these things happens:

- the request reaches the front of the queue and runs
- the client disconnects and Guardian cancels it
- the client explicitly cancels it through `DELETE /v1/queue/requests/{request_id}`

Only GPU-backed inference routes are queued:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/completions`
- `POST /v1/embeddings`
- `POST /v1/messages`
- `POST /api/chat`
- `POST /api/generate`

Queue admission is rejected before waiting if either of these is true:

- the request is unauthenticated
- the requested model is not served by Guardian
- the same API key already owns a queued or running GPU request

The configured `queue.queue_timeout_seconds` (shipped default: `1800`) is an
**enforced** wait budget for a request sitting in the queue, connection or not.
When it elapses, Guardian cancels the queued request:

- the request finalizes as `expired` with
  `cancel_reason: "queue_timeout"` (distinct from `client_requested_cancel`
  and `client_disconnected`), and `stats.total_timeouts` /
  `stats.total_expired` increment;
- the waiting client receives the same typed cancellation as every other queue
  cancellation: HTTP `499`, `error: "request_cancelled"`,
  `message: "queue_timeout"`;
- the slot accounting stays intact: the entry leaves `waiting`, the next
  waiter is woken and promoted if a slot is free, and `your_status` /
  `your_cancel_reason` report the final state.

`queue_timeout_seconds: 0`, a negative value or a non-finite value (`.inf`)
means **no queue timeout**: the wait is unbounded, as before,
`queue_timeout_s` reports `0` and
`queue_timeout_enforced` reports `false`. An absent key falls back to the
built-in default (`1800`), so deleting the key does not silently disable the
guarantee. Both status fields are derived from the configured value at startup
— never assumed — so operators can trust them. Changing the queue section
requires a restart; it is not picked up by `POST /api/config/reload`.

Backpressure comes from the FIFO inference queue, the admission bounds below and
model-size-dependent backend timeouts. There is no history of per-route quotas
and no cloud-side rate limiting: cloud requests are limited by the provider.

## Admission Limits (G1-03)

Three bounds protect Guardian's ingress. Every one of them is configurable
(`admission` in `config/global.settings.yaml`, see
[CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) §10) and every one has a stable typed error
with an explicit retry contract. Source of record:
[decisions/G1-03-admission-and-audit.md](decisions/G1-03-admission-and-audit.md).

| Bound | Config key | Shipped default | Stable error | Retry-After |
| --- | --- | --- | --- | --- |
| Request body size | `admission.max_body_bytes` | `33554432` (32 MiB) | `413 request_too_large` | **no** — retrying the same body fails the same way |
| Waiting queue depth | `admission.queue_max_depth` | `32` | `503 queue_full` | **yes** — `admission.queue_full_retry_after_seconds` (60 s) |
| Per-key request rate | `admission.rate_limit.requests_per_minute` / `.burst` | `300` / `300` | `429 rate_limited` | **yes** — seconds until one token is available |

The defaults are **safe but not tuned**: they sit far above every measurement in
[G0_BASELINE.md](G0_BASELINE.md) (depth 1, ~9-20 requests/minute in the heaviest
probe minute, kilobyte chat bodies), not at a load-tested optimum.

**Which routes:**

| Bound | Applies to | Does not apply to |
| --- | --- | --- |
| Body size | every route on both surfaces | — |
| Per-key rate | every route that requires an API key ("every non-`public` row of the [G0-01 matrix](G0_ENDPOINT_AUTH_MATRIX.md)") | `/healthz`, `/`, `/favicon.ico`, `/static`, `/docs`, `/docs/oauth2-redirect`, `/openapi.json`, `/redoc` — no identity to key on |
| Queue depth | `POST /v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings`, `/v1/messages`, `POST /api/chat`, `POST /api/generate` | cloud-only routes (cloud traffic never enters the local queue) and every non-inference route |

The rate limiter is a **single-process** token bucket keyed on the authenticated
identity (the key fingerprint), never on the source IP: two keys behind one NAT
get two buckets, and a bucket resets with the service. Two Guardian replicas
would each enforce its own budget — multi-replica accounting is out of scope.

Queue depth and the wait budget answer different questions and never both charge
one request: the depth bound refuses a request *before* it is registered (no
request id, no wait budget involved), while an admitted request that waits too
long still ends as the usual `499` / `queue_timeout` described above.

## Audit records (G1-03 part 2)

Guardian writes **one metadata-only audit record per authorization/admission
decision** — allows *and* denials — on both surfaces (`:11434` → `surface:
"proxy"`, `:11437` → `surface: "dashboard"`). Source of record:
[decisions/G1-03-admission-and-audit.md](decisions/G1-03-admission-and-audit.md)
§11; implementation `app/proxy/audit.py`.

**Audited:** every request that presents a key (allowed, with its resolved
scopes) and every refusal on the way — `401` auth failure, `403` scope denial,
`429` rate limit, `413` body bound, `503` queue depth / misconfiguration, and a
refused outbound destination (`502 destination_denied`).

**Not audited:** `public` routes (`/healthz`, the docs routes, `/`, `/favicon.ico`,
`/static`) — they carry no key and make no authorization decision. Payload
capture is a separate, opt-in subsystem and is unchanged.

### The record

One JSON object per line on logger `Guardian.Audit` (`INFO` for allow, `WARNING`
for deny), one line per decision:

```json
{"decision":"deny","detail":"insufficient_scope","event":"audit","key_fingerprint":"4e99bf5e7dae","key_name":"agent-one","limit":null,"limit_value":null,"method":"GET","reason":"insufficient_scope","required_scope":"admin","route":"/api/keys","scopes":["client"],"status":403,"surface":"proxy","ts":"2026-10-28T13:54:17.256Z"}
```

| Field | Meaning |
| --- | --- |
| `ts` | UTC timestamp, milliseconds, `Z` |
| `event` | always `"audit"` |
| `surface` | `proxy` (`:11434`) or `dashboard` (`:11437`) |
| `decision` | `allow` or `deny` |
| `reason` | closed vocabulary: `scope_granted`, `scope_grace`, `missing_api_key`, `invalid_api_key`, `insufficient_scope`, `invalid_scope_declaration`, `scope_undeclared_route`, `rate_limited`, `queue_full`, `admission_misconfigured`, `request_too_large`, `destination_denied`, `other` |
| `detail` | machine sub-code where the reason has one (e.g. the destination reason like `host_not_allowed`), otherwise `null` |
| `method` | HTTP method |
| `route` | the FastAPI **route template** (`/v1/{path:path}`), never the concrete path; `null` when the request was refused before routing (`413`) |
| `required_scope` | the scope the route declares (`client`, `operator`, `admin`, `capture-export`) |
| `scopes` | the key's resolved scopes, sorted (`null` when the key was never resolved) |
| `key_fingerprint` | `sha256(token)[:12]` — the G1-01 identity of the **presented** key (validated or not) |
| `key_name` | operator-assigned key name (`null` when `audit.include_key_name: false`) |
| `status` | the HTTP status the client received |
| `limit` | config key of the admission bound that refused it, when one did |
| `limit_value` | that bound's configured number |

`decision` is the authorization/admission answer and `status` is what the client
got, so an allowed request that then failed (the `499 queue_timeout` wait budget,
a `5xx` from the backend) reads `"decision":"allow"` with that status.

**What a record never contains:** payload or prompt text, completion/model
output, any header value, the raw token, the query string, the concrete path,
the client IP or user agent. The field set is closed (`RECORD_FIELDS` in
`app/proxy/audit.py`) and every string is length-capped, so the line is safe to
ship to a shared log aggregator. The `key_name` can be omitted
(`audit.include_key_name: false`) where key names are personal data — see
[CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) §12.

**Auth-header invariant.** No `Bearer`/`api-key`/`Authorization` value can reach
a Guardian log line: the G1-02 redaction filter (`app/log_safety.py`) scrubs
registered secrets (provider credentials, the caretaker key and the Guardian key
store) and credential-shaped values, and
`tests/unit/test_audit_metadata.py` fails the build if any exercised path logs
one — `DEBUG` included.

### Querying it

The log line is the durable sink; the in-process buffer (bounded, default 256
records) is a post-incident tail, not an HTTP endpoint.

```bash
# every decision since the last restart
journalctl -u guardian-llmprovider-gateway --since today | grep '"event":"audit"'
# just the denials, newest last
journalctl -u guardian-llmprovider-gateway | grep '"event":"audit"' | grep '"decision":"deny"'
# one key's trail (fingerprint from the auth line or the record itself)
journalctl -u guardian-llmprovider-gateway | grep '"event":"audit"' | grep '"key_fingerprint":"4e99bf5e7dae"'
# rate/queue refusals, if jq is available
journalctl -u guardian-llmprovider-gateway -o cat \
  | grep '"event":"audit"' | jq -c 'select(.reason=="rate_limited" or .reason=="queue_full")'
```

`grep '"event":"audit"' guardian.log` works the same way when the service writes
its log file. Aggregated counts are on `GET /metrics`
(scope `client`): `guardian_audit_records_total{decision,reason}` — at most
`2 × 13` = 26 series, so the metric cannot grow with traffic or with caller input.

## Error Model

Common statuses:

- `200`: success
- `400`: bad admin load override, malformed JSON body, or missing required request fields
- `401`: missing or invalid API key
- `403`: authenticated, but not authorized — the key lacks the route's required
  scope. Body shape:
  ```json
  {"detail": {"type": "insufficient_scope",
              "message": "key 'dashboard' lacks the required scope 'admin' for GET /api/keys",
              "required_scope": "admin",
              "granted_scopes": ["client"],
              "key": "dashboard",
              "route": "GET /api/keys"}}
  ```
  `WWW-Authenticate: Bearer error="insufficient_scope", scope="admin"` is set.
  Other `type` values: `scope_escalation` (minting a key with more privilege than
  the minting key holds), `invalid_scope` (mint request naming an unknown scope,
  `400`), `authorization_misconfigured` (a protected route has no declared scope,
  or the key's own scope list is unreadable, `403`).
- `404`: model metadata lookup failed or inference requested an unserved model
- `409`: queue admission rejected because the same API key already has a queued or running GPU request
- `413`: the request body exceeds `admission.max_body_bytes`. No `Retry-After`.
  Body shape:
  ```json
  {"detail": {"error": "request_too_large",
              "limit": "admission.max_body_bytes",
              "limit_bytes": 33554432,
              "declared_bytes": 41943040,
              "message": "request body exceeds admission.max_body_bytes (33554432 bytes); ..."}}
  ```
  A chunked (or otherwise unlabelled) body reports `observed_bytes` instead of
  `declared_bytes`; a request that carries both `Transfer-Encoding: chunked` and a
  `Content-Length` is counted rather than trusted, because the encoding wins when
  both are present. The payload is never echoed. A body bound that cannot be
  resolved (malformed config) refuses *every* request body with the `503`
  `admission_misconfigured` below, even an empty one.
- `429`: the caller's key exhausted its per-key request budget. `Retry-After` is
  set to the whole seconds until one token is available. Body shape:
  ```json
  {"detail": {"error": "rate_limited",
              "limit": "admission.rate_limit.requests_per_minute",
              "requests_per_minute": 300, "burst": 300,
              "retry_after_s": 1,
              "message": "per-key request rate exceeded (300/min, burst 300); retry after 1s"}}
  ```
  A request refused here never enters the inference queue.
- `422`: vision runtime unavailable or backend image path rejected
- `499`: Guardian cancelled the request because the downstream client
  disconnected, the client explicitly cancelled its queue entry, or the queue
  wait budget (`queue_timeout_seconds`) expired. The body keeps the
  `{"error": "request_cancelled", "request_id": ..., "message": <reason>}`
  shape; `message` is the typed `cancel_reason` (`client_disconnected`,
  `client_requested_cancel`, `queue_timeout`, ...).
- `500`: unexpected switch or proxy failure
- `503`: model load, auto-reload, or backend recovery failure. Two admission
  variants are also `503` and are distinguishable by `detail.error`:
  - `queue_full` — the waiting queue is at `admission.queue_max_depth`; the
    request was **not** queued. `Retry-After` is set
    (`admission.queue_full_retry_after_seconds`). Body:
    ```json
    {"detail": {"error": "queue_full",
                "limit": "admission.queue_max_depth",
                "max_queue_depth": 32, "queue_length": 32, "max_concurrent": 1,
                "retry_after_s": 60,
                "message": "the inference queue is full (32 waiting, limit 32); the request was not queued; retry after 60s"}}
    ```
  - `admission_misconfigured` — an admission bound could not be resolved
    (malformed `admission` configuration, see
    [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) §10). **No** `Retry-After`: waiting
    cannot fix a configuration. Body reports `limit` and `reason`, and the
    refusal covers only the requests that bound guards.
- `410`: legacy benchmark start/stop endpoints are disabled

## Proxy Surface (`:11434`)

### Public endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | No | Liveness probe for the Guardian process only |

Notes:

- `/healthz` does not validate backend health. Use `/api/status` for that.

### Metrics endpoint

| Method | Path | Queued | Scope | Purpose |
| --- | --- | --- | --- | --- |
| `GET` | `/metrics` | No | `client` | Prometheus scrape target |

`/metrics` requires a key, like every other route except `/healthz` and the
FastAPI docs routes ([#46](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/46)):
its payload names the loaded models and exposes queue depth and process state, so
it is key-holder data rather than a public probe. Any valid Guardian key with the
`client` scope can scrape it (`operator` and `admin` imply `client`); an
unauthenticated request returns `401`, and a `capture-export`-only key returns the
typed `403`.

Scrape it like this:

```bash
curl -s -H "Authorization: Bearer $GUARDIAN_API_KEY" http://127.0.0.1:11434/metrics
```

The body, content type (`text/plain; version=0.0.4`) and metric families are
unchanged; only the credential requirement was added. Prometheus configuration
therefore needs a bearer token (or `x-api-key` header) on the scrape target:

```yaml
scrape_configs:
  - job_name: guardian
    scheme: http
    authorization:
      credentials_file: /etc/prometheus/guardian.key
    static_configs:
      - targets: ["127.0.0.1:11434"]
```

### Model metadata and compatibility endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/models` | No | List configured canonical models and aliases |
| `GET` | `/v1/models/{model_id}` | No | Return metadata for one model or alias |
| `GET` | `/v1/models/{model_id}/endpoints` | No | Return the serving routes for one model (OpenRouter-style) |
| `GET` | `/api/tags` | No | Ollama-compatible model list |
| `POST` | `/api/show` | No | Ollama-compatible metadata for one model |

#### `GET /v1/models`

Returns OpenAI-style model entries enriched with Guardian-specific metadata and
OpenRouter-parity model metadata.

Representative item:

```json
{
  "id": "openrouter/anthropic/claude-sonnet-4.6",
  "object": "model",
  "created": 1771342990,
  "owned_by": "openrouter",
  "permission": [],
  "served_by": "cloud",
  "provider": "openrouter",
  "max_context": 1000000,
  "benchmark_context_limit": 1000000,
  "context": 1000000,
  "context_length": 1000000,
  "max_input_tokens": 1000000,
  "meta": {"n_ctx": 1000000},
  "name": "Anthropic: Claude Sonnet 4.6",
  "description": "Sonnet 4.6 is Anthropic's most capable Sonnet-class model yet, ...",
  "canonical_slug": "anthropic/claude-4.6-sonnet-20260217",
  "hugging_face_id": null,
  "architecture": {
    "modality": "text+image+file->text",
    "input_modalities": ["text", "image", "file"],
    "output_modalities": ["text"],
    "tokenizer": "Claude",
    "instruct_type": null
  },
  "pricing": {
    "prompt": "0.000003",
    "completion": "0.000015",
    "input_cache_read": "0.0000003",
    "input_cache_write": "0.00000375"
  },
  "top_provider": {
    "context_length": 1000000,
    "max_completion_tokens": 128000,
    "is_moderated": true
  },
  "per_request_limits": null,
  "supported_parameters": [
    "include_reasoning", "max_tokens", "reasoning", "reasoning_effort",
    "response_format", "stop", "structured_outputs", "temperature",
    "tool_choice", "tools", "top_k", "top_p"
  ],
  "default_parameters": {},
  "knowledge_cutoff": null,
  "expiration_date": null,
  "links": {"details": "/v1/models/openrouter/anthropic/claude-sonnet-4.6/endpoints"},
  "reasoning": {
    "supported_efforts": ["max", "high", "medium", "low"],
    "default_effort": "medium",
    "mandatory": false,
    "default_enabled": true
  },
  "metadata_sources": {
    "architecture": "reference:openrouter"
  },
  "input_modalities": ["text", "image"],
  "configured_input_modalities": ["text", "image"],
  "vision": {"configured": true, "status": "supported", "validated": true}
}
```

Notes:

- `metadata_sources` lists only the sections whose value did **not** come from
  the route's own upstream advertisement — an upstream-served section is the
  default and deliberately carries no provenance entry, so `upstream` never
  appears as a value.
- `max_context` is normally the benchmark ceiling.
- for `client_id == claudecode`, Guardian may return the safer
  `advertised_context` value as `max_context` so Claude compacts earlier.
- `vision.status` comes from Guardian's runtime validation state, not just the
  presence of an `mmproj` path.
- Every entry has a positive `context_length`, `meta.n_ctx`, and
  `max_input_tokens`. Guardian resolves manual `context_overrides`, then a
  cached cloud catalog or the active local backend's `/props`, then the
  cross-provider reference catalog, before using a conservative `131072`
  fallback.
- `reasoning` (optional, cloud models only): present when the upstream provider
  catalog advertises reasoning-effort information (currently OpenRouter).
  `supported_efforts` lists the advertised effort levels, `default_effort` the
  provider default, and `mandatory`/`default_enabled` any required/opt-in
  flags. When absent, the model exposes no reasoning-effort metadata. Guardian
  strips the metadata fields down to this safe subset (never forwards unknown
  keys in the block). Effort *values* are passed through unchanged to the
  provider on every request; this field is the machine-readable listing that
  clients (dsh/pi/goose) can use to pick a valid `reasoning_effort`.
- Since the cloud-access redesign, `/v1/models` also lists cloud models from
  the dynamic catalog (`{provider}/{brand}/{model}` addresses); local models
  and aliases always appear. See `@docs/LLM_ROUTER.md`.

##### OpenRouter-parity metadata

`/v1/models` and `/v1/models/{model_id}` additionally carry the model metadata
OpenRouter publishes, using OpenRouter's own field names and conventions. The
full contract lives in `@docs/OPENROUTER_PARITY.md`; the essentials:

- **Unknown is `null`, never absent.** Every parity key is present on every
  entry, and `null` means "unknown". This is OpenRouter's own convention and it
  lets a client render a field without a presence check.
- **Precedence per field**: the operator's per-model config override, then this
  route's own upstream catalog advertisement, then the cross-provider
  *reference catalog*, then a grounded synthesis for locally served models.
- **Nothing is fabricated.** `pricing` is `null` for a cloud model whose
  upstream advertises no price — Guardian never borrows another provider's
  price. `tokenizer`, `instruct_type`, `is_moderated` and `knowledge_cutoff`
  stay `null` when unknown. Locally served models report grounded zero pricing
  (`"0"`), because local inference genuinely costs nothing.
- **`metadata_sources`** reports provenance per section: `upstream`,
  `override`, `reference:<name>`, `local` or `derived`. A section absent from
  the map came from the route's own upstream advertisement, so an operator can
  always tell an upstream fact from a cross-provider gap fill.
- **`supported_parameters`** is passed through verbatim when the upstream
  advertises it and filled from the reference catalog when it does not. For
  locally served models it lists only what Guardian's local path genuinely
  forwards, so a capability Guardian would drop is never advertised.
- **Additive contract.** No existing key changes name, type or meaning, and
  cloud access gating is unchanged.

##### Cross-provider reference catalog

Some providers advertise almost nothing in their own `/v1/models` — OpenAI,
Google and NVIDIA return little more than model ids — while another provider's
catalog describes the same model in detail. A provider may therefore declare
reference sources in its provider settings file:

```yaml
reference_catalog:
  sources:
    - name: openrouter
      url: https://openrouter.ai/api/v1/models
      enabled: true
      ttl_seconds: 86400
      send_api_key: false
```

Reference sources are keyed on the **identity key** — the `{brand}/{model}`
part of a `{provider}/{brand}/{model}` address — so two providers serving the
same model share metadata. A reference source is fetched independently of
`catalog_url` and **never** affects which models are advertised, routed or
allowlisted; it only fills gaps. Fetches are fail-open: on error the last
successful reference catalog is kept.

#### `GET /v1/models/{model_id}/endpoints`

Returns OpenRouter's endpoint shape for one model, filled with the routes
Guardian can actually use:

```json
{
  "data": {
    "id": "openrouter/anthropic/claude-sonnet-4.6",
    "name": "Anthropic: Claude Sonnet 4.6",
    "created": 1771342990,
    "description": "...",
    "architecture": {"modality": "text+image+file->text", "...": "..."},
    "endpoints": [
      {
        "name": "openrouter | anthropic/claude-sonnet-4.6",
        "model_id": "anthropic/claude-sonnet-4.6",
        "model_name": "Anthropic: Claude Sonnet 4.6",
        "context_length": 1000000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "provider_name": "openrouter",
        "tag": "openrouter",
        "quantization": null,
        "max_completion_tokens": 128000,
        "max_prompt_tokens": null,
        "supported_parameters": ["tools", "reasoning", "response_format"],
        "supports_tool_choice": {"none": true, "auto": true, "required": true, "function": true},
        "native_tools": null,
        "supports_implicit_caching": null,
        "supports_image_reference": null,
        "status": null,
        "uptime_last_30m": null,
        "uptime_last_5m": null,
        "uptime_last_1d": null,
        "latency_last_30m": null,
        "throughput_last_30m": null
      }
    ]
  }
}
```

Behaviour:

- A cloud model yields one endpoint per enabled and configured provider whose
  catalog genuinely contains the same identity key, plus a local backend
  endpoint when a local provider serves it.
- A `failover/{group}` address yields one endpoint per configured candidate,
  with the group name in `tag`.
- A locally served model yields its local backend endpoint.
- `status` is `0` when the route is known healthy and non-zero when Guardian
  knows it is degraded; it is `null` when Guardian has no health signal for that
  route. Uptime, latency and throughput are `null`: Guardian does not collect
  per-route time series, and reporting a plausible-looking number instead would
  be a fabrication.
- Unknown model → `404`. A key without `cloud_gateway_access` → `403` and no
  cloud endpoints.
- Route registration note: this route is declared *before* the greedy
  `/v1/models/{model_id:path}` catch-all. A model id that literally ends in
  `/endpoints` is therefore interpreted as an endpoints request, matching
  OpenRouter's own ambiguity.

#### `GET /api/tags`

Returns a synthetic Ollama-compatible `{"models": [...]}` list based on the
configured model registry.

Compatibility notes:

- `size` is derived from Guardian's `get_model_size()` heuristic
- `details.family`, `details.parameter_size`, and related fields are generic
  compatibility placeholders, not authoritative GGUF introspection

#### `POST /api/show`

Accepts `{"model": "<model-id>"}` (or Ollama's `name` alias) and returns
model metadata for local, cloud, per-key Guardian, and failover routes. The
response always includes a positive context window in
`model_info.general.context_length`, `model_info.guardian.context_length`, and
the `num_ctx` parameter.

#### `GET /api/version` — not served

Guardian does not implement Ollama's version endpoint: the path is not
registered, so a request returns `404`. An unregistered handler that would have
answered `{"version": "0.1.27"}` existed in `app/proxy/server.py` and was
removed in [#47](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/47);
routing it would be a new feature (route, test, and a matrix row), not a
correction. Ollama clients that require a version banner should probe
`GET /api/tags` or `GET /v1/models` instead.

### Inference endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/chat/completions` | Yes | OpenAI-compatible chat completions |
| `POST` | `/v1/responses` | Yes | OpenAI Responses API (native llama.cpp passthrough; cloud models via chat-completions translation) |
| `POST` | `/v1/completions` | Yes | OpenAI-compatible text completions |
| `POST` | `/v1/embeddings` | Yes | OpenAI-compatible embeddings |
| `POST` | `/v1/messages` | Yes | Anthropic-style message passthrough when supported by backend |
| `POST` | `/api/chat` | Yes | Ollama-compatible chat bridge |
| `POST` | `/api/generate` | Yes | Ollama-compatible prompt bridge |

#### `POST /v1/chat/completions`

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "messages": [
    {"role": "user", "content": "Reply with exactly: FIT OK"}
  ],
  "max_tokens": 16,
  "stream": false
}
```

Representative multimodal request:

```json
{
  "model": "qwen3.6-35b-heretic-mtp",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        {"type": "text", "text": "Describe the image in one sentence."}
      ]
    }
  ],
  "max_tokens": 64
}
```

Runtime behavior:

- the requested `model` is resolved against Guardian's configured registry before queue admission; unknown or unserved values fail with `404 model_not_served`
- cloud models are recognised by explicit `models:` entries or namespace prefixes (e.g. `anthropic/`, `nvidia/`, `poolside/`) — see `@docs/LLM_ROUTER.md`
- cloud requests bypass the local queue slots and are forwarded directly to the upstream provider
- requests from the same API key may queue behind each other, but Guardian only
  grants one running GPU slot per key at a time
- if the requested model differs from the active model and the client is
  allowed to switch, Guardian reloads inside the queue slot before proxying
- if the same model needs a text-to-vision or vision-to-text runtime flip,
  Guardian reloads that same model with the new runtime mode
- streaming holds the queue slot until the SSE stream closes
- all streaming paths (local + cloud) emit `: guardian-keepalive` SSE comments every 15s during upstream silence to prevent client idle timeouts; the Anthropic bridge path additionally emits `event: ping` events

#### `POST /v1/responses`

OpenAI Responses API (`POST /v1/responses`, the interface used by the OpenAI
SDK `client.responses.create` and Codex CLI). Behavior depends on the
resolved route:

- **Local models** — llama.cpp implements `/v1/responses` natively (it
  converts internally to chat completions and speaks the Responses SSE event
  protocol). Guardian routes the request through the standard inference
  pipeline (queue admission, model resolution/auto-switch, usage tracking,
  vision preflight) and streams backend events through unmodified.
- **Cloud models** — cloud providers speak chat completions only, so
  Guardian transparently translates both directions (request → chat
  completions; response / SSE stream / errors → Responses format), mirroring
  the Anthropic bridge. See `app/proxy/responses_bridge.py`.

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "instructions": "Answer tersely.",
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {"type": "input_text", "text": "Reply with exactly: FIT OK"}
      ]
    }
  ],
  "max_output_tokens": 16,
  "stream": false
}
```

`input` may also be a plain string (`"input": "Reply with exactly: FIT OK"`).
Supported input items: messages with `input_text`/`input_image` parts,
assistant output messages, `function_call` / `function_call_output` pairs
(function tool calling round-trips through the same items), and `reasoning`
items. Streaming uses the standard Responses event flow
(`response.created` → `response.output_item.added` →
`response.output_text.delta` → … → `response.completed`, the last event
carrying the full response object including usage). Cloud routes also accept
`service_tier` (OpenRouter service tiers: `default`/`flex`/`priority`/`fast`/
`ultrafast`) — it rides through the translation, and the terminal response
reports the tier that actually served the request.

Not supported (no chat-completions equivalent; `previous_response_id`
returns a 400): stateful `previous_response_id` conversations, `store`,
`background`, `include`, `item_reference`, `truncation`, and built-in tools
(web_search, file_search, computer_use — non-function tools are dropped).
Guardian does not persist responses, so `GET /v1/responses/{id}` is
backend-decided (llama.cpp returns 404 for unknown ids). Capture covers
`/v1/responses` like every other text inference endpoint: requests are
normalized from `input` items to OpenAI-style messages, and Responses SSE
streams are assembled into a single completed capture event.

#### `POST /api/chat`

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "messages": [
    {"role": "user", "content": "Say hello"}
  ],
  "stream": false,
  "options": {
    "temperature": 0.7
  }
}
```

Representative non-streaming response:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "created_at": "2026-05-25T12:00:00.000Z",
  "message": {
    "role": "assistant",
    "content": "hello"
  },
  "done": true,
  "total_duration": 0,
  "load_duration": 0,
  "prompt_eval_count": 8,
  "eval_count": 2
}
```

#### `POST /api/generate`

Representative request:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "prompt": "Reply with exactly: FIT OK",
  "stream": false
}
```

Representative response:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "created_at": "2026-05-25T12:00:00.000Z",
  "response": "FIT OK",
  "done": true,
  "context": [],
  "total_duration": 0,
  "load_duration": 0,
  "prompt_eval_count": 8,
  "eval_count": 2
}
```

### Generic OpenAI passthrough

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/{path:path}` | No | Direct passthrough to backend `GET /v1/...` |
| `POST` | `/v1/{path:path}` | Sometimes | Direct passthrough to backend `POST /v1/...` |

Queue rule for generic `POST /v1/{path:path}`:

- queued only when `path` is `chat/completions`, `completions`, `embeddings`,
  `messages`, or `responses`
- direct passthrough for all other `POST /v1/*` paths
- for queued inference paths, Guardian requires a valid JSON object body with a served `model` field before queue admission

### Speech (TTS) endpoint

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/audio/speech` | No (blocks up to `tts.ensure_timeout_seconds`) | OpenAI-compatible speech synthesis via provider-declared TTS engines |

Provider-driven routing (2026-09-16): a host participates in speech routing
when its provider file declares `tts_url` (the qwen3tts-http engine) plus
`management_url` + `management_key` (the caretaker control API). `tts.providers`
in `global.settings.yaml` lists provider names in failover order; each attempt
is `POST {management_url}/tts/ensure` (the caretaker's uniform on-demand
lifecycle: VRAM gate, optional llama-stop, VRAM wait, spawn/idle-stop) followed
by `POST {tts_url}/tts`. There is no cloud/local distinction — any host (LAN or
cloud GPU box) serves speech purely by configuration.

Request mapping (OpenAI speech → engine contract):

| OpenAI field | Engine field | Notes |
| --- | --- | --- |
| `input` | `text` | required, non-empty |
| `voice` | `instruct` | stock names (`alloy`…`ballad`) map to VoiceDesign instructions; unknown strings pass through verbatim |
| `instruct` (extra) | `instruct` | explicit extra wins over `voice` |
| `seed`, `sub_seed`, `temperature` | same | passthrough when present |
| `response_format` | — | must be `wav` or `pcm` (engine serves 16-bit PCM mono 24 kHz WAV); others → 400 |

Config (`tts:` in `global.settings.yaml`, hot-reloadable): `enabled`,
`providers` (failover order), `timeout_seconds` (engine call),
`ensure_timeout_seconds` (must exceed `CARETAKER_TTS_START_TIMEOUT` +
`CARETAKER_TTS_VRAM_WAIT_SECONDS` of the slowest host), `default_instruct`.

Errors: `404` when disabled, `503` when no provider declares `tts_url`, `400`
for empty `input`/bad format, `502` when all configured providers fail (detail
lists the per-provider reasons, e.g. insufficient VRAM).

Speech model routing (2026-09-23, route-oriented — no opt-in switches): the
`model` field is an ADDRESS, `[guardian/]{provider}/{upstream-model-id}`, resolved
through the provider file alone (`app/gateway/speech_routing.py`). A provider
declaring `tts_url` (+ `management_url`) serves the request with its LOCAL
engine (caretaker-managed lifecycle); one declaring `base_url` + `api_key`
serves it from its OpenAI-compatible cloud endpoint; the upstream model id is
everything after the provider segment, VERBATIM — the provider's real id, bare
(`guardian/groq/whisper-large-v3` → `whisper-large-v3`) or namespaced
(`guardian/groq/canopylabs/orpheus-v1-english` → `canopylabs/orpheus-v1-english`)
alike. An explicit address is EXACT — a failure
surfaces honestly for that route (`502` with the route name) and never falls
back to a different provider. Without an addressable `model` field the default
local failover chain (`tts.providers`) serves. Unknown or malformed addresses
return `404 model_not_served` (the chat routing contract). The forwarded
cloud payload is the canonical OpenAI shape; the local engine's clone
passthroughs and `instruct` do not travel to cloud providers.

Validation split for routed requests: the local `wav`/`pcm`-only
`response_format` restriction does not apply to an address — the requested
format passes verbatim to the route's provider (e.g. `mp3`). `speed` is
validated early (number 0.25-4.0, OpenAI contract) for both routes so the
client gets a clear `400` instead of a failed upstream call.

Non-OpenAI-shaped speech APIs are bridged by a per-provider dialect adapter
(``speech_adapter`` in the provider file; default `openai`): `fish` translates
the OpenAI-shaped request to fish.audio's native `/v1/tts` (`voice` →
`reference_id`, falling back to the route's upstream id; `response_format` →
`format`: wav/pcm/mp3/opus) and `/v1/asr` (multipart field `audio`; `language`
is a hint — fish auto-detects and returns `language_code`).

Cloud TTS routes carry provider-specific requirements verbatim: `voice` is
mandatory for some providers and its valid values are the model's
`supported_voices` (OpenRouter models API), and the accepted `response_format`
values are provider-defined (OpenRouter: `mp3`/`pcm`; the local engine: `wav`).
A mismatch surfaces as an honest `502` with the upstream reason. Cloud STT
routes accept the same multipart shape as OpenAI clients (OpenRouter:
`model`/`file`/`language`), with `language` passed through verbatim.

### Speech-to-text (STT) endpoint

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/audio/transcriptions` | No (blocks up to `stt.ensure_timeout_seconds`) | OpenAI Whisper-compatible transcription via provider-declared STT engines |

Provider-driven routing (2026-09-19; route-oriented 2026-09-23): an
addressable `model` (`[guardian/]{provider}/{upstream-model-id}`) resolves
through the provider file alone — `stt_url` serves with that provider's local
engine (caretaker `POST {management_url}/stt/ensure`), `base_url` + `api_key`
serves from its OpenAI-compatible cloud endpoint (upstream id = everything
after the provider segment, verbatim — bare or namespaced ids alike).
`stt.providers` is the failover order for the DEFAULT path (no address).

Request: multipart/form-data — `file` (raw audio bytes; wav 16-bit PCM mono
preferred, sample rate arbitrary), optional `model`, optional `language` as an
ISO-639-1 code (`nl`/`en`) mapped to the Qwen3-ASR language names; without it
the host default applies. Response: `{"text", "language", "seconds",
"elapsed_s"}`.

Errors: `404` disabled, `503` no provider declares `stt_url`, `400` missing
`file`, `502` all providers failed (per-provider reasons).

Cloud forwarding (2026-09-22, opt-in twice): when `stt.cloud_forwarding.enabled`
is set in `global.settings.yaml` AND the requested `model` maps to a provider
that declares `cloud_stt: true` (plus `base_url` + `api_key`) in its provider
file, the upload is forwarded to that provider's OpenAI-compatible
`/audio/transcriptions` endpoint — e.g. `model=cloudstt/cloudstt/whisper-large-v3`
routes to the `cloudstt` provider (upstream model id = final path segment). `language` passes
through verbatim (ISO-639-1; the Qwen name mapping is engine-specific). A
cloud attempt runs before the local engine chain; any cloud failure falls
through to the local engines unchanged. With the switch off (default) or no
`model` field, behavior is identical to the pre-forwarding route.

### Queue and status endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/queue/status` | No | Return queue depth, active requests, and per-client status |
| `GET` | `/v1/queue/requests/{request_id}` | No | Return lifecycle state for one tracked queued request |
| `DELETE` | `/v1/queue/requests/{request_id}` | No | Cancel a queued request or request cancellation of a running one |
| `GET` | `/api/status` | No | Return backend, queue, startup, switch, proxy, and security state |
| `GET` | `/api/crashes` | No | Return crash history and the last crash |

#### `GET /v1/queue/status`

Representative response:

```json
{
  "queue_length": 1,
  "active_count": 1,
  "max_concurrent": 1,
  "max_queue_depth": 32,
  "queue_timeout_s": 1800,
  "queue_timeout_enforced": true,
  "wait_policy": "disconnect_or_cancel",
  "stats": {
    "total_queued": 12,
    "total_completed": 11,
    "total_timeouts": 0,
    "total_cancelled": 1,
    "total_failed": 0,
    "total_expired": 0
  },
  "your_position": 1,
  "your_status": "queued",
  "your_wait_s": 3.2,
  "your_request_id": "28b9fd72-8aed-4426-9645-156a43ec9074"
}
```

`max_queue_depth` is the configured waiting-depth bound
(`admission.queue_max_depth`, G1-03) and is `null` when that bound could not be
resolved — in which case every submission is refused with `503
admission_misconfigured` rather than waiting unbounded.

`queue_timeout_enforced` is derived from the configured
`queue.queue_timeout_seconds`: `true` for a positive finite value, `false` for
`0`, a negative value, `None` or any non-finite value (each of which disables
the timeout). An **absent** key is not a disable switch: it falls back to the
enforced `1800`-second default. A request that expires reports
`your_status: "expired"` with
`your_cancel_reason: "queue_timeout"` and increments
`stats.total_timeouts` / `stats.total_expired`.

#### `GET /v1/queue/requests/{request_id}`

Representative response:

```json
{
  "request_id": "28b9fd72-8aed-4426-9645-156a43ec9074",
  "client_id": "telegram-grow-bot",
  "model": "qwen3.6-35b-uncensored",
  "status": "queued",
  "position": 1,
  "enqueued_at": 1748262000.0,
  "waiting_s": 14.2
}
```

The request must belong to the same API key fingerprint that created it.
Guardian returns `404` for another key instead of leaking cross-key queue state.

#### `DELETE /v1/queue/requests/{request_id}`

Representative response for a running request:

```json
{
  "request_id": "28b9fd72-8aed-4426-9645-156a43ec9074",
  "client_id": "telegram-grow-bot",
  "model": "qwen3.6-35b-uncensored",
  "status": "cancelling",
  "position": 0,
  "cancel_reason": "client_requested_cancel"
}
```

#### `GET /api/status`

Representative top-level fields:

```json
{
  "current_model": "Qwen3.6-35B-A3B-HauhauCS-Aggressive",
  "backend_healthy": true,
  "backend_state": "alive",
  "backend_death": null,
  "caretaker": {
    "configured": true,
    "reachable": true,
    "death_signal": "empty",
    "backend_alive": true,
    "status_read_at": "2026-10-29T04:12:00.114275+00:00"
  },
  "is_unloaded": false,
  "idle_seconds": 17,
  "idle_unload_minutes": 5,
  "backend_url": "http://127.0.0.1:11440",
  "queue": {},
  "startup": {},
  "switch": {},
  "security": {},
  "proxy": {},
  "routing": {},
  "scaler": {}
}
```

Important subtrees:

- `queue`: live queue snapshot
- `startup`: generation-tracked background startup verification state
- `switch`: active switch owner, requested target, and lock state
- `security`: pinned model, allowlist, backend verification timestamps
- `proxy`: PID, listener ownership, PID file state
- `routing`: preferred tool model and reasoning model

Backend-death fields ([#34](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/34), additive):

- `backend_healthy`: unchanged — the result of a direct `/health` probe of
  the managed backend (`true`/`false`), independent of the caretaker.
- `backend_state`: the combined judgement, `alive` | `dead` | `unloaded` |
  `unknown`. See [the derivation rules](#backendstate-derivation) below.
- `backend_death`: the last backend death Guardian has observed, or `null`.
  Shape is the caretaker death record plus Guardian's provenance fields:
  `{cause, model, timestamp, exit_code, signal, detail, event_id, origin,
  observed_at}`. It is history: it survives a later successful reload, and it
  is `null` when no death has been observed (including every pre-#34 daemon).
- `caretaker`: how the death-signal read went — `configured` (a caretaker
  client exists), `reachable` (this read succeeded), `death_signal`
  (`reported` | `empty` | `unsupported` | `malformed` | `unavailable` |
  `not_configured`), `backend_alive` (the daemon's own tri-state view:
  `true`/`false`/`null`), and `status_read_at` (ISO-8601 of the last
  *successful* read; `null` if the daemon has never answered).

`/api/status` is the **only** place Guardian reads the caretaker death signal.
The read is coalesced for 5 s and uses `min(2 s, caretaker.client_timeout_seconds)`,
so a hung daemon cannot stall the endpoint. Inference requests never read it.

<a id="backendstate-derivation"></a>
##### `backend_state` derivation

Evaluated in order, first match wins:

| # | Condition | `backend_state` |
| --- | --- | --- |
| 1 | gateway `is_unloaded` or caretaker `is_unloaded: true` | `unloaded` |
| 2 | caretaker `backend_alive: false` | `dead` |
| 3 | direct `/health` probe returned 200 | `alive` |
| 4 | probe failed and the caretaker reported a death | `dead` |
| 5 | everything else (unreachable daemon, conflicting evidence, …) | `unknown` |

Consequences that matter for monitoring:

- An **unreachable** caretaker never produces `alive` on its own: with a
  failing probe the state is `unknown`, not healthy. The daemon's silence is
  not evidence of health.
- A **death record is not a liveness claim**: `backend_death` keeps the last
  death even after a successful reload (the record is history), while
  `backend_state` returns to `alive` as soon as the probe answers again.
- A daemon that reports `backend_alive: false` wins over a probe that still
  answers, and a daemon claiming `backend_alive: true` with a failing probe
  yields `unknown` — conflicting evidence fails closed.
- `backend_healthy` keeps its pre-#34 meaning for existing clients; use
  `backend_state` when you need the combined answer.

#### `GET /api/crashes`

Representative response (one load failure and one caretaker-reported death):

```json
{
  "total_crashes": 2,
  "last_crash": {
    "timestamp": "2026-10-28T21:14:03+00:00",
    "model": "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved",
    "error_message": "caretaker reported backend death: cause=oom, signal=SIGKILL, exit_code=137",
    "exit_code": 137,
    "config_snapshot": null,
    "origin": "reported_death",
    "cause": "oom"
  },
  "last_reported_death": {
    "timestamp": "2026-10-28T21:14:03+00:00",
    "model": "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved",
    "error_message": "caretaker reported backend death: cause=oom, signal=SIGKILL, exit_code=137",
    "exit_code": 137,
    "config_snapshot": null,
    "origin": "reported_death",
    "cause": "oom"
  },
  "history": [
    {
      "timestamp": "2026-10-28T20:00:00+00:00",
      "model": "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved",
      "error_message": "failed to fit params to free device memory",
      "exit_code": 1,
      "config_snapshot": {"ngl": 99},
      "origin": "load_failure",
      "cause": null
    },
    {
      "timestamp": "2026-10-28T21:14:03+00:00",
      "model": "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved",
      "error_message": "caretaker reported backend death: cause=oom, signal=SIGKILL, exit_code=137",
      "exit_code": 137,
      "config_snapshot": null,
      "origin": "reported_death",
      "cause": "oom"
    }
  ]
}
```

Provenance fields (added for [#34](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/34)):

- `origin`: `load_failure` — Guardian's own load/switch health wait, or the
  caretaker's `503 model_load_failed` `crash_details` on `/ensure`;
  `reported_death` — a death the caretaker reported outside `/ensure`
  (the `/status` death signal). Defaults to `load_failure`, so every
  pre-#34 record keeps its meaning.
- `cause`: the reported cause (`oom` | `signal` | `exit_code`, or any newer
  value the daemon sends, lower-cased) for a `reported_death`; always `null`
  for a `load_failure`, which Guardian never gives a cause it did not observe.
- `last_reported_death`: the newest `reported_death` entry, or `null` when the
  history holds only load failures.

So a crash record answers two different questions: `origin` says *where
Guardian learned about the death*, `cause` says *why the process stopped* when
the reporter knows it.

#### Caretaker death signal (the contract Guardian consumes)

Guardian used to learn about a backend death only through the **next**
`POST /ensure`. A llama-server crash or an OOM kill during a live-but-idle
session was invisible until then. The caretaker control API
(`GET {management_url}/status`, Bearer `CARETAKER_KEY`) therefore **MAY**
carry the following optional members. Nothing here is required, and no existing
field changes meaning.

Response members Guardian reads (all optional):

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `last_death` | object \| null | no | The most recent backend death the daemon observed that did **not** happen during an `/ensure` it served. `null` = the daemon implements the field and has nothing to report. Absent = the daemon predates this contract. |
| `last_death.cause` | string | yes, when `last_death` is an object | Why the backend stopped: `oom` (OOM-killed), `signal` (killed by a signal, name in `signal`), `exit_code` (exited on its own; code in `exit_code`). A newer daemon may send another cause; Guardian records it verbatim, lower-cased. |
| `last_death.model` | string | yes | The model the dead backend was serving. Guardian does not substitute its own `current_model`. |
| `last_death.timestamp` | string | yes | ISO-8601 when the daemon observed the death (`Z` or an explicit offset; naive values are accepted). Unparsable ⇒ the record is malformed. |
| `last_death.exit_code` | int \| numeric string | no | Process exit status when known. |
| `last_death.signal` | string | no | Signal name, e.g. `SIGKILL` (for `cause: "signal"`). |
| `last_death.detail` | string | no | One-line daemon reason, carried into `error_message`. |
| `last_death.event_id` | string | no | Stable identity for the death. When present, Guardian dedupes on it; when absent, Guardian dedupes on `(timestamp, cause, model, exit_code, signal)`. Supply it if two byte-identical deaths can share a timestamp and both must be visible. |
| `backend_alive` | bool | no | The daemon's own current view of the backend process. `false` means "not running and not deliberately stopped"; `true` means "running". Only a real JSON boolean counts — `"true"`/`"false"` strings read as unknown. |

Guardian's handling of every case:

| Situation | `caretaker.death_signal` | Crash record | `backend_state` contribution |
| --- | --- | --- | --- |
| `last_death` omitted (daemon predates #34) | `unsupported` | none | no contribution — exactly today's behaviour |
| `last_death: null` | `empty` | none | no death |
| valid `last_death` object | `reported` | one record per death (deduped) | `dead` while the probe fails |
| `last_death` present but unusable (not an object; missing `cause`/`model`/`timestamp`; unparsable timestamp) | `malformed` | none (logged, never a bogus record) | none — cannot claim alive |
| timeout, connection error, non-200, non-JSON/non-object body | `unavailable` | none | `unknown`, never `alive` |
| no caretaker configured | `not_configured` | none | probe only |

The reported death becomes a `CrashRecord` with `origin: "reported_death"`,
`cause` from the daemon, `timestamp`/`model`/`exit_code` from the record,
`config_snapshot: null` (Guardian did not launch that process), and an
`error_message` that summarises cause/signal/exit code/detail. It is visible
through `GET /api/crashes` and as `backend_death` in `GET /api/status`.

A caretaker that does not implement this contract is fully supported. A
caretaker that implements it but claims health while the backend is dead
cannot produce a false `alive`: `backend_state` requires the direct `/health`
probe, and an explicit `backend_alive: false` overrides a probe that still
answers.

### Model management endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/admin/load` | No | Reload current or specified model |
| `POST` | `/admin/unload` | No | Stop backend immediately and free VRAM |

### API key management endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/keys` | No | List all Guardian keys (tokens masked, fingerprints, declared and effective scopes) |
| `POST` | `/api/keys` | No | Generate a new Guardian API key (returns the full key once) |

Scope: **`admin`** for both. `POST /api/keys` body:
`{"name": "my-app", "prefix": "myapp", "metadata": {"client": "my-app", "scopes": ["client"]}}`.
The full key is returned only once — store it immediately.

Minting rule (B-08, decision record §7): the new key inherits the minting key's
`cloud_gateway_access` and its declared `metadata.scopes`; requested scopes must
be a subset of the minting key's declared scopes (`403 scope_escalation`
otherwise), unknown scopes are `400 invalid_scope`, and a request that tries to
upgrade `cloud_gateway_access` is refused. `GET /api/keys` additionally reports
`scopes` (declared, `null` when absent), `effective_scopes` and `scope_mode`
(`explicit`, `grace`, `unscoped`, `invalid`) so the migration window is visible.

### Cloud credential endpoints (per-key cloud routing)

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/cloud/credentials` | No | List credentials owned by the calling key |
| `POST` | `/api/cloud/credentials` | No | Add a provider credential (`provider`, `name`, `api_key`, `models`) |
| `POST` | `/api/cloud/credentials/{cred_id}/refresh-models` | No | Refresh the stored Google model catalog |
| `DELETE` | `/api/cloud/credentials/{cred_id}` | No | Delete a credential and all its links |
| `POST` | `/api/cloud/credentials/{cred_id}/models` | No | Add a model to a credential |
| `DELETE` | `/api/cloud/credentials/{cred_id}/models/{model_name:path}` | No | Remove a model from a credential |
| `GET` | `/api/cloud/links` | No | List credential links for the calling key |
| `POST` | `/api/cloud/links` | No | Link a credential to a Guardian key |
| `DELETE` | `/api/cloud/links` | No | Unlink a credential from a Guardian key |
| `GET` | `/api/cloud/providers` | No | List configured providers and their status |
| `GET` | `/api/cloud/models` | No | List global + per-key cloud models for the caller |
| `GET` | `/api/cloud/ratelimit-stats` | No | Per-key 429 counters and provider hints |
| `POST` | `/api/cloud/credentials/claim` | No | Adopt an owner-less (legacy) credential — caller must already hold a link |
| `POST` | `/api/config/reload` | No | Hot-reload settings.yaml + cloud_keys.json without restart |

Scope: **`admin`** for the live `/api/cloud/*` routes in this section and for
`POST /api/config/reload` (G1-01). A `client` or `operator` key receives the
typed `403 insufficient_scope`.

Credentials and links are **owner-scoped**: only the Guardian key that created
a credential can manage or share it (403/404 otherwise). Google credentials
automatically fetch their model catalog on create/refresh.

**Legacy ownership repair:** credentials created before ownership was recorded
and linked to more than one key are unmanageable for every key. A key that
already holds a link can claim one via `POST /api/cloud/credentials/claim`
(`{"provider": ..., "credential_id": ...}`); after the claim it becomes the
permanent owner and can link the credential to other keys.

**Hot config reload (`POST /api/config/reload`):** re-reads settings.yaml +
cloud_keys.json (providers, failover_groups, credential links, capture
cloud_capture/prefixes/policies, failover_health, cloud_retry) and swaps them
live without a restart. Port/pid/TLS remain restart-only. Only a key with scope
`admin` can trigger it (G1-01); a `client` or `operator` key gets the typed
`403 insufficient_scope`. Response lists what was reloaded and what could not be.

### Capture management endpoints (admin)

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/capture/status` | No | Capture subsystem status, config summary, writer metrics, disk usage |
| `POST` | `/api/capture/rotate` | No | Force rotation of the active WAL file |

Scope: **`capture-export`** for both. `admin` reaches them through scope
implication; a `client` or `operator` key gets the typed `403`.

The capture subsystem stores **raw request/response events** in a JSONL WAL under
`data/capture/` (the active file `guardian_capture_current.jsonl` is plain JSONL so
`tail -f`/`jq` streaming works and readers never hit a truncated gzip member
mid-write; on rotation it is gzip-compressed to
`guardian_capture_<timestamp>_<seq>.jsonl.gz`, which gets a `.sha256` sidecar). Since the 2026-08-26 raw-capture redesign the only
in-pipeline transformation is **media extraction**: inline image payloads are
written as separate files under `data/capture/media/` and replaced in the event
by a reference block (base64 never lands in the WAL). Redaction/dataset work is
done offline by `scripts/keanu_redact.py`; replay with integrity verification is
`guardianctl export`. Capture is enabled by default for all authenticated
clients (`cloud_capture: true`, `per_client_opt_in: false`); `retention_days: -1`
means keep everything indefinitely and `max_capture_bytes: -1` is an unlimited
byte budget.

**Coverage — all text inference endpoints.** Every queued text inference route
is captureable under its native protocol: OpenAI
(`/v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings`),
Anthropic (`/v1/messages`), and Ollama (`/api/chat`, `/api/generate`).
Requests are normalized to OpenAI-style messages for the record (Responses
`input` items, legacy `prompt`, and embeddings `input` are all converted
fail-open; grammar/schema parameters are stripped, presence flags only).
Streaming requests are accumulated from the SSE stream into a single
completed event — the assembler understands OpenAI chat deltas, Anthropic
blocks, and Responses events (`response.output_text.delta`,
`response.reasoning_text.delta`, function-call items,
`response.completed`/`response.incomplete`). Embeddings records carry the
input text with no response content (vectors are not stored). Audio
endpoints (`/v1/audio/*`) are outside capture coverage (binary media, no
message model).

`/api/capture/status` shows the effective config (enabled/active, local/cloud,
opt-in mode, retention, byte budget) plus the sink/writer metrics and disk usage
— the `field_policies` block it reports are **Keanu defaults** for the offline
redaction tool, not per-request Guardian filtering. `/api/capture/rotate` closes
the active WAL file and opens a new one (returns the rotated + active paths). See
`scripts/guardianctl.py` for the operator CLI mirroring these endpoints
(`status`, `rotate`, `files`, plus the `export` replay/verify subcommand).

#### `POST /admin/load`

Scope: **`operator`** (G1-01) — process/model lifecycle with restart-equivalent
impact. `client` keys are refused with the typed `403`.

Request body:

```json
{
  "model": "qwen3.6-35b-uncensored",
  "enable_vision": false,
  "runtime_overrides": {
    "context": 262144,
    "ngl": 40,
    "tensor_split": "0.36,0.64"
  }
}
```

Rules:

- `model` may be a canonical model name or an alias
- `enable_vision` is optional
- `runtime_overrides` must be an object and may contain only:
  - `context`
  - `ngl`
  - `tensor_split`
- `tensor_split` must be exactly two finite non-negative comma-separated values
- invalid override contracts return `400`
- load failures return `503`

Representative success response:

```json
{
  "status": "loaded",
  "model": "Qwen3.6-35B-A3B-HauhauCS-Aggressive"
}
```

#### `POST /admin/unload`

Scope: **`operator`** (G1-01), like `/admin/load`.

Representative response when backend was active:

```json
{
  "status": "unloaded",
  "message": "Model 'Qwen3.6-35B-A3B-HauhauCS-Aggressive' unloaded - VRAM is free"
}
```

Representative response when already unloaded:

```json
{
  "status": "already_unloaded",
  "message": "llama-server is already stopped"
}
```

### Scaler endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/scaler` | No | Return current scaler config |
| `PUT` | `/api/scaler` | No | Partially update scaler config |
| `POST` | `/api/scaler/reset` | No | Reset scaler config to defaults |
| `POST` | `/api/scaler/recommend` | No | Return advisory `thinking_budget_tokens` and `max_tokens` |

Important note: in current code the scaler is an advisory API and config store.
It is not automatically rewriting every inference request body in the hot path.

#### `PUT /api/scaler`

Representative request:

```json
{
  "enabled": true,
  "queue_pressure": {
    "heavy_threshold": 6
  }
}
```

Representative response:

```json
{
  "status": "updated",
  "config": {}
}
```

#### `POST /api/scaler/recommend`

Representative request:

```json
{
  "messages": [
    {"role": "user", "content": "Explain the runtime tradeoffs."}
  ]
}
```

Representative response:

```json
{
  "profile": "moderate",
  "complexity": {
    "total_chars": 36,
    "num_messages": 1,
    "has_system": false,
    "has_images": false
  },
  "pressure": "none",
  "recommended": {
    "thinking_budget_tokens": 4096,
    "max_tokens": 8192
  }
}
```

### Session endpoints

| Method | Path | Queued | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/session/save` | No | Save llama.cpp slot 0 state to disk |
| `POST` | `/api/session/load` | No | Restore llama.cpp slot 0 state from disk |
| `GET` | `/api/session/list` | No | List saved slot files |

#### `POST /api/session/save`

Request body:

```json
{
  "filename": "my-session"
}
```

Guardian forwards to llama.cpp slot API:

- `POST /slots/0?action=save`

#### `POST /api/session/load`

Request body:

```json
{
  "filename": "my-session"
}
```

Guardian forwards to llama.cpp slot API:

- `POST /slots/0?action=restore`

#### `GET /api/session/list`

Representative response:

```json
{
  "sessions": ["my-session", "other-session"]
}
```

Current implementation scans `~/llama_slots` for `*.bin` files.

## Grammar-Constrained Decoding (GCD)

Guardian supports three structured-output fields, mapped to llama-server on the
local path and to OpenAI-provider conventions on cloud routes.

### Request fields

| Field | Type | Local | Cloud (OpenRouter/NVIDIA/OpenAI/Google) |
| --- | --- | --- | --- |
| `response_format` | `{type, json_schema}` | pass-through (OpenAI-native) | pass-through (provider-native) |
| `json_schema` | dict / GBNF schema | pass-through | stripped, optional JSON conversion |
| `grammar` | string (GBNF) | pass-through | stripped, optional JSON conversion |

### Ollama protocols (/api/chat, /api/generate)

Ollama clients send `options.format`. Guardian maps a dict value to
`response_format` and a string value to `grammar` before forwarding to
llama-server. Client's explicit top-level `response_format`/`grammar` in the
request body wins over `options.format`.

### Config

- `config/settings.yaml` → `grammar`:
  - `enabled` (default `true`) — kill-switch: when `false`, `grammar` and
    `json_schema` are stripped on BOTH local and cloud paths.
  - `cloud_auto_convert_json` (default `false`) — convert a JSON-targeting
    grammar/schema to OpenAI `response_format` on cloud routes.
  - `cloud_strict_mode` (default `false`) — return HTTP 400 (naming the
    provider) instead of silently stripping an unsupported grammar on cloud.
  - `validate_gbnf` (default `false`) — pre-validate GBNF syntax before
    forwarding to llama-server (fail-open; structural checks only).
- `config/models.yaml` → optional per-model `grammar_decoding: true|false`
  (advisory capability hint only; not consumed by runtime routing logic. The
  global `grammar.enabled` kill-switch is the enforced control.)

### Cloud limitation

Cloud providers (OpenRouter, NVIDIA, OpenAI, Google) do NOT accept GBNF
grammar strings. By default they are stripped silently. Enable
`grammar.cloud_auto_convert_json: true` to auto-convert a JSON-targeting
grammar to a `response_format` JSON-schema; enable `grammar.cloud_strict_mode:
true` to return 400 instead of stripping for non-JSON grammars on cloud models.

### Example

```json
POST /v1/chat/completions
{
  "model": "llama3.2-3b",
  "messages": [],
  "response_format": {"type": "json_schema", "json_schema": {"name": "distill_record", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}}
}
```

### Capture

Capture events include `grammar_present` and `response_format_present` boolean
flags; the raw grammar/schema content is never stored (`structured_output`
field policy defaults to `strip`).

## Dashboard and Monitoring Surface (`:11437`)

These endpoints are served by the separate UI app in `app/main.py`.

| Method | Path | Auth | Scope | Purpose |
| --- | --- | --- | --- | --- |
| `GET` | `/` | No | `public` | Serve static dashboard UI |
| `GET` | `/api/stats` | Bearer | `admin` | Aggregate VRAM, cached models, and API usage snapshot |
| `GET` | `/api/benchmark` | Bearer | `admin` | Read-only benchmark summary |
| `POST` | `/api/benchmark/start` | Bearer | `admin` | Returns `410 Gone` |
| `POST` | `/api/benchmark/stop` | Bearer | `admin` | Returns `410 Gone` |
| `GET` `POST` | `/api/keys` | Bearer | `admin` | List / mint keys (same rule as the proxy surface) |
| `GET` | `/api/cloud/providers` `catalog` | Bearer | `admin` | Cloud provider/catalog state |

### `GET /api/stats`

Representative top-level fields:

```json
{
  "vram": {
    "used": 12000,
    "free": 14000,
    "total": 26000
  },
  "active_models": [],
  "queue_size": 0,
  "optimized_count": 0,
  "cached_models": [],
  "records": [],
  "api_usage": {}
}
```

Important note: `api_usage` is backed by
[../data/api_usage_state.json](../data/api_usage_state.json), so request
totals and top-client summaries survive Guardian restarts.

### `GET /api/benchmark`

Representative fields:

- `completed_count`
- `queue_count`
- `last_completed`
- `best_by_model`

This endpoint is historical and read-only. The old benchmark runner is no
longer a live control path.

## Security and Backpressure Summary

- Auth is enforced on `:11434` except for `/healthz` and the FastAPI docs routes;
  `/metrics` requires a key with the `client` scope since
  [#46](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/46).
- Authorization is enforced per route (G1-01): the caller's key must hold the
  route's declared scope; `client` keys are refused on `/api/keys`,
  `/api/config/reload`, `/admin/*` and the capture routes. See
  [decisions/G1-01-privilege-scopes.md](decisions/G1-01-privilege-scopes.md).
- The dashboard UI assets on `:11437` are unauthenticated; every `/api/*` route
  on that surface requires a Bearer key with the route's scope (all of them
  `admin`).
- Model changes are governed by `guardian.pinned_model` and
  `guardian.switch_allowlist`.
- Admission bounds (G1-03) cap the request body (32 MiB -> `413`), the waiting
  queue depth (32 -> `503 queue_full`), and the per-key request rate (300/min ->
  `429`) with retry hints; the rate limiter is keyed on the key fingerprint, is
  single-process and is documented as such. See
  [decisions/G1-03-admission-and-audit.md](decisions/G1-03-admission-and-audit.md).
- Metadata-only audit records (G1-03 part 2) are written for every allow and
  deny decision on both surfaces; no payload, header value, token or query
  string can reach them, and no `Bearer` value can reach a log line at any
  level. See [decisions/G1-03-admission-and-audit.md](decisions/G1-03-admission-and-audit.md)
  §11.
- Backpressure comes from the FIFO inference queue, the per-key token bucket
  (G1-03), the waiting-depth bound and the model-size timeout tiers.