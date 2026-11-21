# Per-key model allowlist: extracted scope

This documents the existing implementation extracted for
[issue #111](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/111),
not a new gateway-wide authorization design.

## Key field and matching

`allowed_models` is an optional **top-level field on a named Guardian key** in
`config/guardian.keys.yaml`, alongside `cloud_gateway_access` (not inside
`metadata`). No live key or deployment configuration is included here.

- Absent, `null`, or an empty list means **unrestricted by this field**, not
  deny-all. Existing cloud-access and privilege-scope checks still apply.
- Key loading wraps a non-list scalar in a list, stringifies list entries,
  trims surrounding whitespace and removes blank entries. A blank scalar or a
  blank-only list therefore also becomes unrestricted. Prefer a list of strings;
  this extraction does not introduce stricter schema validation.
- A non-empty normalized list uses exact, case-sensitive public-ID matching:
  no globbing, provider-prefix removal, or equivalence between aliases. It does
  not grant access to an unconfigured model or override disabled cloud access.
- The cloud resolver checks its incoming model ID before catalog target
  resolution and before failover expansion. An unlisted incoming ID returns
  `403` with `detail.error = "model_not_allowed"` (unless the earlier
  `cloud_gateway_access` check already denied cloud access).

## Covered discovery and virtual groups

`GET /v1/models` filters the advertised IDs by exact list membership.
`GET /v1/models/{model_id}`, `GET /v1/models/{model_id}/endpoints`, and
`POST /api/show` return `404` for an unlisted ID before model lookup. These
checks cover local metadata too; they are not proof of local inference denial.
The show handler trims surrounding model-name whitespace first; the endpoints
handler trims surrounding whitespace and trailing slashes before matching.

`failover/{group}` is itself an authorizable public ID. An explicitly granted
group expands to its configured, enabled, available candidates; those targets
are **not individually checked** against `allowed_models`. Conversely, granting
individual targets does not grant the group ID. This is public-group
authorization, not automatically an authorization bypass. A requirement to
intersect group candidates with individual target grants would change the
extracted behavior. Configured local image fallback similarly authorizes the
requested cloud ID, not a separate grant on the internal fallback target.

## Known limitations: not a complete isolation boundary

- **Direct local inference is gated in code, not yet deployed.** The local
  paths in `route_v1_post` (`/v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, `/v1/messages`, `/v1/responses`) and the Ollama
  `/api/chat` and `/api/generate` bridges now reject an unlisted public model
  ID with `403 model_not_allowed` before `_model_manager` resolution, queue
  admission, lifecycle actions and backend or cloud forwarding. The decision is
  taken once per handler family, on the ID the client sent, and streaming and
  non-streaming requests share it. A body without `model` keeps its existing
  `400` (`model_not_specified` / `Model not specified`) because these endpoints
  resolve no implicit default, so no unlisted default can be served; the
  gateway-resolved default sentinel `auto` is matched as the public ID it is.
  This is a source-level change: it is **not merged, deployed or live
  verified**, and it does not extend to the two limitations below (issue #117).
- **Separate speech routes do not enforce this field.** TTS/STT handlers use
  their own routing paths rather than the shared cloud inference resolver.
  When speech is enabled, explicit unlisted speech model addresses can reach
  those handlers' backend forwarding paths.
- **`GET /api/tags` is not filtered.** Its authenticated route invokes a
  request-independent handler, which lists local registry models without the
  key's allowlist. This is inconsistent with the filtered OpenAI discovery and
  the model-specific `404` behavior.
- The field does not introduce authorization on other administrative or
  service-specific APIs, nor inheritance rules for key minting. It must not be
  presented as a universal per-key service/model security boundary.

The speech and `/api/tags` limitations are retained deliberately from the
faithful extraction and still need separate fixes/contracts. The
local-inference gate above still needs a separately authorized deployment and a
live denied-request check before enforcement can be claimed as actual; this
code-only change does not claim it.

## Focused verification

`tests/unit/test_key_model_allowlist.py` covers normalization, auth-context
propagation, cloud allow/deny and cloud-access precedence, exact IDs, virtual
failover authorization, OpenAI listing, and covered metadata denials/allowance.
`tests/unit/test_local_model_allowlist.py` covers the direct local inference
gate on all seven paths: unlisted local IDs are denied before the manager,
queue, lifecycle and transport sentinels, listed and unrestricted keys still
reach admission, matching stays exact and case-sensitive, cloud addresses keep
the cloud resolver's gate, and a body without `model` keeps its `400`. The
pre-fix isolated handler probes that confirmed an unlisted local model reached
queue admission are recorded in the issue:
[#117](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/117).
They stopped before queue/backend/GPU side effects. No live inference,
deployment or production mutation was performed.
