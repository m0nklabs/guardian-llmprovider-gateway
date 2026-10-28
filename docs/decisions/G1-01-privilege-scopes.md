# G1-01 — Privilege scopes and admin isolation

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |
| **Issue** | [#52](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/52) |
| **Roadmap** | `docs/ROADMAP.md` §G1 (verified priority gap) row G1-01 |
| **Baseline** | `docs/G0_ENDPOINT_AUTH_MATRIX.md`, `docs/G0_TRUST_BOUNDARY_THREAT_MODEL.md` §4.2/§4.3, B-08 |
| **Operator decision** | Migration **option C** — enforce, with a bounded grace window driven by a config timestamp |

## 1. Context

G0-01 recorded the flat reality: **57 routes, 44 protected, one dependency**
(`verify_api_key`). Any valid key reached every privileged route — key minting
(`POST /api/keys`), runtime configuration (`POST /api/config/reload`), capture
export/rotation, process lifecycle (`/admin/load`, `/admin/unload`), shared state
(`/api/status`, `/api/session/*`, `/api/scaler/*`). G0-02 §4.3 recorded the
consequence (any client key escalates to admin/operator) and B-08 recorded the
minting loop: because `cloud_gateway_access` defaults to `true` and a minted key
inherits it, a key that was denied cloud access could recover it by minting a new
key. Authentication alone authorized privilege; it must not.

G1-01 introduces the distinction **without breaking the named-key clients that
exist today**. The operator chose the two-phase migration: enforce, but give
legacy unscoped keys a bounded, configured grace window instead of an immediate
break.

## 2. Decision

1. **Scopes are carried by the existing key store.** A key's privilege lives in
   its `metadata.scopes` list in `config/guardian.keys.yaml` —
   `metadata.scopes: [client, operator]`. No new secret, no key-store schema
   migration: `metadata` already exists per key and is already hot-reloaded from
   the file (mtime-gated).
2. **One declaration per route, one enforcement point.** Every route registered
   by `app.proxy.server:app` and `app.main:app` is declared exactly once in
   `app/proxy/scopes.py:ROUTE_SCOPES`, keyed by `(HTTP method, FastAPI path
   template)` — the route *template*, so a parameterised or catch-all path
   cannot borrow another route's scope. The single auth dependency
   (`app/proxy/auth.py:verify_api_key`) resolves the matched route's declaration
   and authorizes the caller's key against it.
3. **Five classes, one implication chain.**

   | Scope | Authorizes | Implies |
   | --- | --- | --- |
   | `public` | nothing — the route needs no key (not grantable to a key) | — |
   | `client` | ordinary inference, model discovery, own-request queue status/cancel | — |
   | `operator` | process/model lifecycle: `/admin/load`, `/admin/unload` | `client` |
   | `admin` | the key store, runtime configuration, shared control state and everything below | `operator`, `client`, `capture-export` |
   | `capture-export` | capture WAL status/rotation/export — user payload data | nothing (deliberately **not** `client`) |

   `admin` implies `capture-export` because an `admin` key can mint itself a
   capture key; keeping the capture class out of `admin` would be paperwork, not
   a boundary. `capture-export` does not imply `client`: an export tool must not
   gain an inference plane by asking to read capture data.
4. **Enforcement from day one for scoped keys.** A key with an explicit
   `metadata.scopes` list is enforced from the moment the code runs, grace
   window or not.
5. **Legacy keys get a bounded window.** A key with **no** `metadata.scopes`
   keeps today's flat access to privileged routes until
   `auth.scopes_enforced_after` and is a plain `client` key from that instant
   on. Every privileged call it makes inside the window is logged (structured,
   naming key, route and would-be missing scope) and counted
   (`guardian_scope_grace_calls_total`).
6. **Minting can never escalate (B-08).** `POST /api/keys` on both surfaces
   requires `admin`; the minted key inherits the minting key's *declared* scopes
   and its `cloud_gateway_access` value, may not request a scope the minting key
   does not declare, and may not upgrade cloud access (§7).
7. **Fail-closed on every ambiguity** (§6): an unknown scope string, a malformed
   scope declaration, an unreadable key store, a missing protected-route
   declaration, or an absent/malformed timestamp denies privilege. Ordinary
   `client` inference is kept alive where that is possible without guessing a
   privilege.

## 3. Route → scope mapping

This is the enforced mapping; it encodes the G0-01 inventory and is duplicated in
`app/proxy/scopes.py:ROUTE_SCOPES` (code is the source of truth, the guard keeps
this table honest). `HEAD` is folded into `GET`; the dashboard `/static` mount is
declared as `MOUNT`.


| Method | Path | Scope | Rationale |
| --- | --- | --- | --- |
| `GET` | `/` | `public` | No key by design (probe/docs/static asset). |
| `POST` | `/admin/load` | `operator` | Restart-equivalent lifecycle: loads/unloads the shared GPU backend. |
| `POST` | `/admin/unload` | `operator` | Restart-equivalent lifecycle: loads/unloads the shared GPU backend. |
| `GET` | `/api/benchmark` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/benchmark/start` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/benchmark/stop` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/capture/rotate` | `capture-export` | Capture payload data (prompt content) - its own privilege. |
| `GET` | `/api/capture/status` | `capture-export` | Capture payload data (prompt content) - its own privilege. |
| `POST` | `/api/chat` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/api/cloud/catalog` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/cloud/catalog/refresh` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/cloud/models` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/cloud/providers` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/cloud/ratelimit-stats` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/config/reload` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/crashes` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/generate` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/api/keys` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/keys` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/scaler` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `PUT` | `/api/scaler` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/scaler/recommend` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/scaler/reset` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/session/list` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/session/load` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/session/save` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `POST` | `/api/show` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/api/stats` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/status` | `admin` | Key store, runtime config, shared control state, diagnostics. |
| `GET` | `/api/tags` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/docs` | `public` | No key by design (probe/docs/static asset). |
| `GET` | `/docs/oauth2-redirect` | `public` | No key by design (probe/docs/static asset). |
| `GET` | `/favicon.ico` | `public` | No key by design (probe/docs/static asset). |
| `GET` | `/healthz` | `public` | No key by design (probe/docs/static asset). |
| `GET` | `/metrics` | `public` | No key by design (probe/docs/static asset). |
| `GET` | `/openapi.json` | `public` | No key by design (probe/docs/static asset). |
| `GET` | `/redoc` | `public` | No key by design (probe/docs/static asset). |
| `MOUNT` | `/static` | `public` | No key by design (probe/docs/static asset). |
| `POST` | `/v1/audio/speech` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `POST` | `/v1/audio/transcriptions` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/v1/models` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/v1/models/{model_id:path}` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/v1/models/{model_id:path}/endpoints` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `DELETE` | `/v1/queue/requests/{request_id}` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/v1/queue/requests/{request_id}` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/v1/queue/status` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `GET` | `/v1/{path:path}` | `client` | Own-request plane: inference, discovery, queue status/cancel. |
| `POST` | `/v1/{path:path}` | `client` | Own-request plane: inference, discovery, queue status/cancel. |

## 4. Compatibility rules and the grace window

- **Explicit declaration wins.** `metadata.scopes: [...]` is enforced
  immediately, both directions: a `client` key is refused on `/admin/load` on the
  first restart even though the window is open, and an `operator` key reaches
  `/admin/load` without waiting for anything.
- **Absent declaration = legacy key.** Before `auth.scopes_enforced_after` it
  holds every key scope (the old behaviour) and every privileged call it makes
  emits one structured log line plus a metrics increment; at/after the timestamp
  it holds `client` only and a privileged call returns a stable typed `403`
  naming the missing scope. There is no fallback to permissive at any point.
- **Config key:** `auth.scopes_enforced_after` in
  `config/global.settings.yaml`, an ISO-8601 UTC timestamp. Hot-reloadable
  through `POST /api/config/reload`. **Shipped default:
  `2026-11-11T00:00:00Z`** — two weeks after this record — documented in the file
  itself and in `docs/CONFIG_SCHEMA.md` §9. The operator can move it earlier
  (tighten) or later (extend) by editing that value and reloading; moving it is a
  privileged `admin` operation by definition.
- **Unparseable/absent value = window closed.** Enforcement applies immediately;
  a broken config can never re-open the permissive path.
- **Migration path for an operator:** add `metadata.scopes` to the keys that need
  privilege (dashboard/control key `[admin]`, lifecycle scripts `[operator]`,
  export tooling `[capture-export]`, inference clients `[client]` or nothing at
  all — `client` is the post-window default). Key entries are read from the file
  on mtime change, so no restart is needed for scoping; only the code needs the
  restart that ships this record.
- **Reading and writing the list:** stored verbatim in
  `metadata.scopes`; the effective grant is the implication closure (§2.3).
  `POST /api/keys` mints under the same rule (§7).

## 5. Rotation and revocation design

This record does not implement rotation/revocation tooling; it fixes the design
the tooling must follow.

- **Rotation without breaking a client:** the key store is a token -> entry map,
  so rotation is "add new token, deploy it to the client, remove old token". The
  recommended operator flow is: mint the replacement with the same declared
  scopes (`POST /api/keys` with `metadata.scopes` copied from
  `GET /api/keys`), update the client's secret, then delete the old entry from
  `config/guardian.keys.yaml`. Because the store is read per request (mtime-
  gated), both tokens work during the overlap and the old one stops working the
  instant the file is saved — no restart.
- **Revocation is visible to callers:** a removed token returns the existing
  `401` (`Invalid API Key`); a key whose scope was narrowed returns the typed
  `403` (`insufficient_scope`, with `required_scope` and `granted_scopes`) on the
  routes it lost. Clients must treat `401` as "stop, credential is gone" and
  `403 insufficient_scope` as "this credential is valid but not for this route",
  which is the distinction G1-01 adds to the error model.
- **Narrowing is safe:** removing a scope from a key takes effect on the next
  request; there is no cached authorization decision beyond the mtime-gated key
  store.
- **Audit:** `GET /api/keys` reports each key's declared `scopes`, their
  `effective_scopes` and the `scope_mode` (`explicit`, `grace`, `unscoped`,
  `invalid`), so an operator can see which keys the window still covers.

## 6. Fail-closed rules

| Condition | Behaviour | Why |
| --- | --- | --- |
| Unknown/typo'd scope token in `metadata.scopes` | The whole declaration is treated as invalid: `client` only, `403` on every privileged route, one structured log naming the offending tokens | A typo must never silently grant the stronger neighbour; the key keeps working for inference so a live integration does not die of a spelling mistake |
| `metadata.scopes` not a list of strings (including an explicit `null`) | Same as unknown token | Present-but-null is a broken declaration, not an absence; it must not inherit the legacy grace grant |
| `auth.scopes_enforced_after` absent or malformed | Window closed; unscoped keys are `client` | A broken config must not extend privilege |
| Key store unreadable/unparseable | `401` on every route (existing `load_api_keys` behaviour) | No token can be verified, so no request is authorized; there is no permissive fallback to fall back to |
| Protected route missing from `ROUTE_SCOPES` | `403` `authorization_misconfigured` | A new route must not become reachable just because nobody declared its scope; the matrix guard also fails the build |
| Explicitly scoped key declared `capture-export` only | Client routes denied | Least privilege; documented in the class table |

## 7. Key minting (B-08)

Rule: **a key minted through the API never holds more than the key that minted
it.**

- `POST /api/keys` (both `:11434` and `:11437`) requires scope `admin`.
- The minted key inherits the minting key's `cloud_gateway_access` value. A
  request that tries to set `cloud_gateway_access: true` from a key that does
  not have it is refused with `403 scope_escalation`.
- Requested `metadata.scopes` must be a subset of the minting key's **grant**
  (its declared scopes plus their implications), otherwise `403
  scope_escalation`. Unknown scopes are `400 invalid_scope`.
- Omitting `metadata.scopes` copies the minting key's declared scopes (an
  implied scope is never promoted to a declaration: an `operator` key mints an
  `operator` key). A grandfathered (unscoped) key holds no declared grant, so it
  can only mint unscoped keys — minting can never turn a grace grant into a
  permanent explicit scope.
- An invalid-scoped minting key cannot mint at all (`403
  authorization_misconfigured`).
- The CLI (`scripts/generate_key.py`) runs as a host-local operator action and is
  intentionally not constrained by a key envelope; it is documented as such.

## 8. OIDC / service identity later

The scope model is deliberately identity-agnostic: authorization consumes
`(key entry -> scopes)` in exactly one function
(`app/proxy/scopes.py:authorize_key`). A later decision can add an OIDC/service
token verifier that produces the same normalized scope set from token claims
(`scope`/`roles` -> class mapping) without touching any route: the route
declarations stay in `ROUTE_SCOPES`, and `verify_api_key` keeps being the single
enforcement point with a second, earlier credential branch. Nothing in this
record assumes the credential is a static string in a YAML file; the key store
just happens to be today's only issuer.

## 9. Consequences

- **On the restart that ships this:** nothing breaks for unscoped legacy keys
  (they hold the old access through the window), but every privileged call by
  such a key logs a grace line and increments a counter. Explicitly scoped keys
  (none exist in the operator's store today) are enforced immediately.
- **After `2026-11-11T00:00:00Z`:** any key without `metadata.scopes` is a
  `client` key. Inference/discovery/queue keep working; `/api/keys`,
  `/api/config/reload`, `/api/capture/*`, `/admin/*`, `/api/status`,
  `/api/crashes`, `/api/session/*`, `/api/scaler/*` and the dashboard `/api/*`
  control routes return the typed `403`. The PR that ships this record lists the
  affected key names and the scopes each needs.
- **Operator tooling that needs a scope before the deadline** (audited in this
  change): the dashboard's stored `guardian_dashboard_api_key` (`:11437`
  `/api/*`, including capture panel status/rotate) needs `admin`;
  `scripts/guardianctl.py` (`status`, `rotate`, `export`) and
  `scripts/verify_post_restart.py` (`GET /api/capture/status`) need
  `capture-export` (or `admin`); anything driving `/admin/load`,
  `/admin/unload` needs `operator`; anything driving `/api/config/reload` or
  `/api/keys` needs `admin`. Which existing named keys those are is visible in
  the grace log — every privileged call an unscoped key makes during the window
  is logged with the key name, route and missing scope, so the window itself
  produces the exact scoping list:
  `grep 'scope-grace' guardian.log`.
- **No effect** on request/response bodies of allowed calls, on cloud routing
  semantics, on capture policy, on ports, or on TLS.

## 10. Tests

`tests/unit/test_privilege_scopes.py` covers the positive and negative class
matrix, both sides of the grace timestamp (fixed timestamps, never a wall-clock
wait), minting escalation, and every fail-closed row above.
`tests/unit/test_g0_endpoint_matrix.py` keeps the matrix, the registered routes
and the code declaration in sync (including "public iff no auth dependency").
