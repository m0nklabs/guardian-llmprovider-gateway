# G1-03 — Bounded admission (part 1) and metadata audit (part 2)

| Field | Value |
| --- | --- |
| **Status** | Accepted (parts 1 and 2). §1-§10 admission, §11 metadata audit. |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |
| **Issue** | [#57](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/57) — parts 1 (admission) and 2 (metadata audit) |
| **Roadmap** | `docs/ROADMAP.md` §G1, third enforcement block; backlog row G1-03 |
| **Baseline** | `docs/G0_BASELINE.md` §2.4/§2.5 (measured queue depth 1, `max_concurrent: 1`, 55.9 s cold start), `docs/G0_TRUST_BOUNDARY_THREAT_MODEL.md` |
| **Follows** | [`G1-01-privilege-scopes.md`](G1-01-privilege-scopes.md) (identity surface), [`G1-02-destination-and-credentials.md`](G1-02-destination-and-credentials.md) (same fail-closed shape: one enforcement point, typed refusal, absent ≠ malformed) |
| **Operator decision** | Enforce three explicit bounds now (body size, waiting queue depth, per-key rate) with generous defaults, validate overrides fail-closed, and keep the defaults honestly labelled **safe but not tuned**. The audit half (metadata-only records + the auth-header log invariant) follows in part 2 of the same issue. |

## 1. Context

`docs/ROADMAP.md` §G1 requires Guardian to "bound request/body sizes, queue
depth, concurrency, timeouts and per-client request rates" and to "return stable
403/413/429/503 errors with appropriate retry hints". The G0 baseline measured
what existed, and it was thin:

- **Queue depth and concurrency.** `queue.max_concurrent: 1` was the only bound.
  The backend has four slots (`docs/G0_BASELINE.md` §2.5) but Guardian serialises
  to one, and a *waiting* request had no ceiling at all beyond the enforced wait
  budget from [#45](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/45)
  — depth 1 was measured, not bounded.
- **Request/body size.** No documented maximum anywhere in
  `docs/API_REFERENCE.md`, and no G0-01 row naming a `413` path. Every handler
  that reads a body reads all of it (`await request.body()`), so an unbounded
  upload is an unbounded allocation.
- **Per-client rates.** `docs/API_REFERENCE.md` said it plainly: "There is no
  token-bucket or per-client rate limiter in current code." Backpressure was the
  FIFO queue and model-size timeout tiers, nothing else.
- **Error vocabulary.** `403` (scope), `404` (model_not_served), `409` (queue
  admission conflict) and `499` (cancellation) existed; there was no stable
  `413`/`429`/`503` shape and no retry hint of any kind.

G1-01 fixed *who* may call *which operation* and gave every request an identity
(`key_fingerprint`, `app/proxy/auth.py:_token_fingerprint`). G1-02 fixed *where*
Guardian may send traffic. This record fixes *how much* Guardian accepts — and
it is the first G1 row where the interesting question is not "may this caller do
this?" but "is this caller, or this payload, more than the deployment can hold?".

## 2. Decision

1. **Three bounds, three stable errors, one enforcement point each.**

   | Bound | Enforced in | Stable error |
   | --- | --- | --- |
   | request body size | `app/proxy/admission.py:BodySizeLimitMiddleware` (both FastAPI apps) | `413 request_too_large` |
   | waiting queue depth | `app/proxy/queue.py:InferenceQueue.submit` | `503 queue_full` |
   | per-client request rate | `app/proxy/admission.py:enforce_rate_limit`, called from `app/proxy/auth.py:verify_api_key` | `429 rate_limited` |

   There is no per-route quota, no per-model quota and no separate
   "burst/sustained" knob set: three bounds, each of which answers a question the
   others cannot. Depth caps *how many* wait; the wait budget caps *how long* one
   waits; the rate caps *how often* one arrives; the body bound caps *how much*
   one sends.
2. **Single process, deliberately.** All accounting is in-process memory: the
   token buckets reset with the service, and two Guardian replicas would each
   enforce its own budget. That is the documented scope ("multi-replica
   accounting — single-process limits only"), and it is why nothing here is
   presented as cluster-wide fairness. There is no Redis, no file, no shared
   counter.
3. **The rate is keyed on the authenticated identity, never on the IP.** The key
   is the G1-01 `key_fingerprint` (`sha256(token)[:12]`): a NAT, an office or a
   reverse proxy shares an IP, and two keys behind one IP are two clients, not
   one. A request is counted after authentication *and* authorization, so a
   bucket only ever belongs to an identified caller.
4. **Admission is ordered: authenticate → authorize → rate → body is already
   bounded → queue.** The body bound is a middleware, so it runs before routing;
   the rate check runs in the auth dependency; the depth check runs at queue
   submission. A request refused by an earlier bound therefore never reaches a
   later one — a `429` never consumes a queue slot, and a `413` never reaches the
   auth dependency at all.
5. **Configuration is one section, validated fail-closed** (§6): absent → shipped
   default with a warning; malformed → refuse, naming the limit. There is
   deliberately **no** `0`/`off` value for any bound.
6. **Defaults are safe but not tuned** (§9). They are chosen to be far above
   every measured value, not to be optimal; the record says so instead of
   implying a capacity plan.

## 3. The bounds and their shipped defaults

All three live in `config/global.settings.yaml` under `admission` (documented in
`docs/CONFIG_SCHEMA.md` §10).

### 3.1 Body size — `admission.max_body_bytes`, default **33 554 432 (32 MiB)** → `413`

What the code already accepts, and therefore what the bound must not break:

- Every inference handler buffers the whole body (`await request.body()`), so the
  bound is an abuse ceiling, not a streaming redesign. Chat bodies in the G0
  baseline are kilobytes.
- Inline images are base64 inside the JSON body, which inflates the original file
  by ~33 %; a two-image vision request with a long prompt is single-digit MiB.
- `/v1/audio/transcriptions` uploads a multipart audio file; the TTS/STT paths
  and the capture writer never approach tens of MiB.

32 MiB is "several tens of MiB" with room for a large multimodal prompt, and it
is 3-4 orders of magnitude above the measured payloads. Two enforcement paths,
one error body:

- a declared `Content-Length` over the bound is refused **without reading a
  byte** — but only when no `Transfer-Encoding` is in play. A message that
  carries both headers is malformed (RFC 7230) and servers frame by the encoding
  (h11/uvicorn do), so a small declared length next to a chunked body is not
  evidence of anything: those requests take the counting path;
- a chunked or unlabelled body is read up to the bound and refused the moment it
  crosses it, with the observed byte count in the error.

Neither path buffers more than the bound, and a rejection allocates nothing
beyond it: the chunk that crosses the bound is not appended, so a huge single
message is refused without a full-buffer copy. The body is coalesced into one
byte buffer, not kept as a list of ASGI messages — a sender delivering millions
of one-byte messages cannot allocate per message, and the replay to the handler
is a single message (linear, not quadratic).

A body cut short by a client disconnect is replayed as **unfinished**
(`more_body: True` followed by the disconnect), so a truncated body that happens
to look like valid JSON raises `ClientDisconnect` in the handler instead of being
served as a complete request.

The chunked path deliberately does **not** raise from the `receive` channel:
route handlers on this gateway parse bodies with broad `except Exception` blocks
(e.g. `app/gateway/model_discovery.py:show_model` → `400`), which would swallow
the signal and turn a clean `413` into a misleading client error. Responding from
the middleware cannot be swallowed.

### 3.2 Queue depth — `admission.queue_max_depth`, default **32** → `503 queue_full`

Measured basis: the highest waiting depth ever observed is **1**, with
`max_concurrent: 1` (`docs/G0_BASELINE.md` §2.4/§2.5). 32 is 32× that and does not
change the concurrency bound — `max_concurrent` still serialises execution; depth
only bounds the waiting list.

Why a depth bound at all, when the wait budget already exists? They bound
different failure modes: the budget stops one client from waiting forever, the
depth stops Guardian from accumulating waiting clients (each holding a connection,
an entry and a capture context). Without it, a client that keeps retrying while
`max_concurrent: 1` grows the waiting list without limit; with it, the 33rd waiter
gets a `503` it can act on.

The bound is **global per process, not per key**: one aggressive client can fill
all 32 slots, and its next request then gets the same `503 queue_full` as everyone
else's. That is deliberate — the point is that the waiting list cannot grow, not
that every caller is guaranteed a fair share; the rate bound is what limits one
client's arrival rate, and the per-key "one running request" rule (`409
queue_admission_rejected`) is what stops one key from occupying a slot twice.

The retry hint is `admission.queue_full_retry_after_seconds`, default **60 s**:
one local generation is tens of seconds (`docs/G0_BASELINE.md`: ~7 s for 256
tokens at ~33 tok/s, 55.9 s for a cold model load), so a minute is the scale at
which a client that honours `Retry-After` makes progress instead of hammering.

### 3.3 Per-client rate — `admission.rate_limit` (300 per minute, burst 300) → `429`

Token bucket: `burst` tokens at rest, one token per request,
`requests_per_minute` refill, per identity, in memory, no timer and no task — a
check is a dict lookup plus arithmetic under a short lock.

Basis for the default: the heaviest minute recorded in the baseline is a 20-sample
probe series (≈9 local samples/minute at ~7 s each, cloud samples faster), plus a
dashboard polling `/api/status` and a client polling `/v1/queue/status` while it
waits (1/s = 60/min). 300/min sustained with a whole minute's allowance spendable
at once leaves an order of magnitude of headroom, and the burst means a reconnect
storm or a batch of parallel tool calls is not mistaken for abuse. A *streaming*
reply is one request, so long generations do not consume the budget.

## 4. Error vocabulary and retry semantics

Stable, typed bodies (`detail`), one per class. `Retry-After` is present only
where retrying is the correct client action.

| HTTP | `error` | When | `Retry-After` | Body fields |
| --- | --- | --- | --- | --- |
| `413` | `request_too_large` | body exceeds `admission.max_body_bytes` (declared length, or the byte at which an unlabelled body crossed the bound) | **no** | `limit`, `limit_bytes`, `declared_bytes` *or* `observed_bytes`, `message` |
| `429` | `rate_limited` | the caller's identity has spent its token bucket | **yes** — whole seconds until one token is available (≥1) | `limit`, `requests_per_minute`, `burst`, `retry_after_s`, `message` |
| `503` | `queue_full` | the waiting queue is at `admission.queue_max_depth`; nothing was registered | **yes** — `admission.queue_full_retry_after_seconds` | `limit`, `max_queue_depth`, `queue_length`, `max_concurrent`, `retry_after_s`, `message` |
| `503` | `admission_misconfigured` | a bound cannot be resolved (malformed config, §6) | **no** | `limit`, `reason`, `message` |

Why each class gets the hint it gets:

- **`413` has no `Retry-After`** because a retry of the same request fails
  identically; the client has to change the request (smaller prompt, fewer
  images), and a time-based hint would invite a pointless retry loop. The error
  names the bound and the observed/declared size so the client can act.
- **`429` has an exact hint**, not an estimate: the bucket knows how many seconds
  one token needs.
- **`503 queue_full` has a configured hint**, not a prediction: Guardian cannot
  know how long the current slot holder will take (model size and generation
  length decide that), so the operator sets the scale rather than the code
  inventing a number.
- **`503 admission_misconfigured` has none** because no amount of waiting fixes a
  malformed value; the operator has to change the config.

Not part of the contract, on purpose: no `X-RateLimit-*` headers, no
`X-Queue-Depth` on every response. The queue-status endpoint already reports the
numbers an operator needs, and every extra header is a contract someone will
depend on. `Retry-After` is the only retry surface this record adds.

Existing codes are unchanged: `403 insufficient_scope` (G1-01), `404
model_not_served`, `409 queue_admission_rejected` (one running request per key),
`499 request_cancelled` (disconnect, explicit cancel, or the wait budget), `502
destination_denied` (G1-02).

## 5. Interaction with the enforced queue wait budget (#45)

The two queue bounds are separate mechanisms with separate errors, and the
boundary between them is explicit:

| Situation | Result | Queue state |
| --- | --- | --- |
| rate bucket exhausted | `429 rate_limited` | no submission at all: no request id, no `your_requests` entry, no `stats.total_queued` increment |
| waiting queue at the depth bound | `503 queue_full` | refused **before** registration (`submit` raises before the entry exists); the wait budget never applies to it |
| admitted, then a slot frees | normal inference | unchanged |
| admitted, then the wait budget elapses | `499 request_cancelled`, `message: queue_timeout` — exactly as #45 shipped it | entry finalizes `expired`, `stats.total_timeouts` / `total_expired` increment |

So: **the depth bound decides whether a request may wait at all; the wait budget
decides how long an admitted request may wait.** Neither is a substitute for the
other, and a request can never be charged both. Worst case at the defaults:
32 waiters × 1800 s budget, i.e. up to 16 hours of queued work in principle —
bounded, honest, and *not tuned*: the operator who runs longer generations should
lower `queue_max_depth` rather than raise the budget.

`GET /v1/queue/status` now reports `max_queue_depth` next to `max_concurrent`, so
the bound is visible rather than inferred (`null` means the configured bound could
not be resolved and every submission is refused — see §6).

## 6. Configuration and fail-closed rules

`admission` in `config/global.settings.yaml`:

```yaml
admission:
  max_body_bytes: 33554432                 # 32 MiB
  queue_max_depth: 32
  queue_full_retry_after_seconds: 60
  rate_limit:
    requests_per_minute: 300
    burst: 300
```

| Condition | Behaviour | Why |
| --- | --- | --- |
| the whole `admission` section **absent** | every bound takes its shipped default; one WARNING naming the key | a config file that predates this record must not take admission down on the next restart (same rule as the G1-01/G1-02 amendments) |
| one key **absent** | that bound takes its shipped default; one WARNING | same |
| a key **malformed** — wrong type, `null`, a boolean, a float, a string, `0`, a negative number, a non-mapping `rate_limit` | the bound **refuses** the requests it guards with `503 admission_misconfigured` (per limit; the other bounds keep working) | a typo must never silently disable a bound, and it must not be guessed into a neighbouring value either |
| `admission` **present but not a mapping** (`admission: "on"`) | every bound refuses | there is nothing to parse, and "no admission" is not a fallback |
| `queue_full_retry_after_seconds` malformed | the **queue-depth** bound refuses with `503 admission_misconfigured` and no `Retry-After`; the body and rate bounds are unaffected. Validated **at submission time**, not only at startup, so a hot reload that breaks the hint is caught before a request is registered | a broken hint must not silently become "no hint" — one bound, one refusal |
| numeric strings (`"33554432"`) | **malformed** | type-strict on purpose: an unquoted YAML number and a quoted string reach different code paths, and guessing which was meant is how limits end up wrong |

There is **no `0 = unlimited`** value for any bound, unlike
`queue.queue_timeout_seconds` (whose `0` predates this record and stays as #45
shipped it): a bound that a single digit can switch off is a bound nobody can
rely on. An operator who wants more headroom raises the number.

**Hot reload vs restart.** `max_body_bytes`, `rate_limit.*` and
`queue_full_retry_after_seconds` are re-read from the shared configuration on
every request, so `POST /api/config/reload` applies them. `queue_max_depth` is
read when the queue object is built at startup and therefore needs a restart,
exactly like the rest of the `queue` section; the startup line states which value
is in force.

## 7. Which routes are bounded

| Bound | Applies to | Not applied to |
| --- | --- | --- |
| body size | **every route on both surfaces** (`app.proxy.server:app` on `:11434`, `app.main:app` on `127.0.0.1:11437`), any method | nothing — a request body is a request body |
| per-key rate | every route that carries the `verify_api_key` dependency, i.e. **every non-`public` row of the G0-01 matrix** — 45 of its 57 rows (36 proxy, 9 dashboard; see `docs/G0_ENDPOINT_AUTH_MATRIX.md` "G1-03 changes") | the 12 `public` rows: the four FastAPI docs routes on each surface (`/docs`, `/docs/oauth2-redirect`, `/openapi.json`, `/redoc`) plus `/healthz`, and on the dashboard surface `/`, `/favicon.ico` and the `/static` mount — there is no identity to key a bucket on |
| queue depth | the three call sites that enqueue: `POST /v1/{path:path}` for the inference paths (`chat/completions`, `completions`, `embeddings`, `messages`, `responses`), `POST /api/chat`, `POST /api/generate` | every other route, including `/v1/{path:path}` for non-inference paths and all cloud-only routes (cloud requests never enter the local inference queue — `docs/G0_BASELINE.md` §2.5) |

Two consequences worth stating: an unauthenticated flood of a public route is
bounded only by nginx and the body middleware (rate limiting it would need a
different identity than a key), and a `403`-ing caller is not rate limited (the
rate check runs after authorization, so a key that is refused cannot burn a
bucket — it also cannot do work, which is the point).

## 8. Observability

- `guardian_admission_rejections_total{limit}` — one counter per bound, so an
  operator can see *which* bound is refusing traffic (prometheus text output on
  `GET /metrics`).
- Structured rejection log lines (WARNING, JSON payload, no payload content and
  no credential): `🛑 body-too-large`, `🛑 rate-limited` (identity fingerprint,
  limit, retry hint), `🛑 queue-full` (depths, hint), `🛑 admission configuration
  refuses traffic` (limit + reason).
- One startup line: `🛡️ admission bounds: body … bytes, queue depth …, rate …/min
  (burst …), queue-full Retry-After …s [configured|shipped-default|mixed]`.
- `GET /v1/queue/status` reports `max_queue_depth`.

None of these lines contains a request body, a prompt, an `Authorization` header
or a raw token; the identity is the 12-hex fingerprint that G1-01 already emits
in `scope_grace` / `scope_denied` events. The *automated* invariant that no auth
header ever reaches a log line at INFO/DEBUG is part 2 (§11.2), which is why this
record claims only "these lines carry no such value" and not "the invariant is
enforced".

## 9. Consequences — safe but not tuned

- **On the restart that ships this:** no allowed traffic changes. Every bound sits
  far above every measurement in `docs/G0_BASELINE.md`, so the live smoke facts
  stay true conceptually: 1 active request + 1 waiting (depth 1 ≪ 32), ~9-20
  requests per minute (≪ 300), kilobyte chat bodies (≪ 32 MiB).
- **"Safe but not tuned"** is the honest label: the defaults protect the process
  from unbounded bodies, unbounded waiting and unbounded arrival rates; they are
  not a capacity plan and they are not derived from a load test. Tuning them is
  an operator decision per deployment, and the mechanism (`admission.*`, hot
  reload for two of three bounds) exists for that.
- **A refused request is cheap and typed.** `413` costs no read, `429` costs a
  dict lookup, `503 queue_full` costs a length check; none of them touches a
  model, the GPU or the capture pipeline.
- **A broken `admission` block can refuse traffic** (`503
  admission_misconfigured`) while `/healthz` keeps answering, so the outage is
  diagnosable from outside. That is the G1-02 trade-off applied to ingress:
  fail closed, loudly, with a typed reason.
- **Single-process accounting** means a second replica doubles the effective
  budget. Stated, not solved.
- **No effect** on allowed request/response bodies, streaming, routing, capture
  policy, ports or TLS.

## 10. Tests

`tests/unit/test_admission_bounds.py` — **63 tests**, every bound exercised with a
**non-default** configured value (a 1 KiB body bound, a queue depth of 1, a
6 requests/minute budget), because "it works at the defaults" is what the
rejection tests would otherwise accidentally prove:

- **Body (negative):** a 4 KiB declared body → `413` naming the limit and the
  declared size; a chunked body of 3 × 700 bytes under a 1 KiB bound → `413` with
  `observed_bytes` and no `declared_bytes`; the response never echoes payload
  content; a malformed bound → `503 admission_misconfigured`, even for an empty
  body; the dashboard surface enforces the same bound.
- **Body (positive):** a body under the bound reaches the handler with its exact
  byte count; an **8 MiB bound admits a 5 MiB body whole** (byte count on the
  handler proves no truncation).
- **Body (framing and buffering, review follow-ups):** a request carrying both
  `Transfer-Encoding: chunked` and a 10-byte `Content-Length` with a 2.1 KiB body
  is still refused with `413` (the encoding wins, so the length is not trusted);
  2000 one-byte ASGI messages cannot escape a 1 KiB bound; an allowed 500-message
  body is delivered to the handler as **one** message; an 8 MiB single message
  against a 1 KiB bound is refused with **no buffered body** (the crossing chunk
  is never appended); a disconnect before the body and a disconnect mid-body both
  reach the handler as an **unfinished** body (`more_body: True`), never as a
  complete truncated request.
- **Queue depth:** a malformed `queue_full_retry_after_seconds` (with a valid
  depth) refuses the submission over HTTP with `503 admission_misconfigured` and
  registers nothing — the hint cannot silently vanish; depth 1 refuses the next
  submission with `QueueDepthExceeded`
  and over HTTP as `503 queue_full` + `Retry-After: 7` (non-default hint) with
  `max_queue_depth`/`queue_length`/`max_concurrent` in the body; the refused
  request never enters the queue; an unresolvable bound refuses with `503` and no
  `Retry-After`; `0`/`-1`/`true`/`"8"`/`2.5` raise at construction.
- **Queue (positive / interaction):** with depth 2 and one slot holder, a request
  **is** queued and still expires as `499 request_cancelled` /
  `message: queue_timeout` — #45 behaviour at the boundary, with
  `stats.total_timeouts` incremented and the depth bound never involved.
- **Rate:** a 6/min bucket with burst 1 → `429` + `Retry-After` (1..10 s) and the
  typed body; traffic under the limit untouched; two keys do not share a bucket;
  one key shares a bucket across routes; the bucket is not keyed on IP; public
  routes are not limited; a malformed bound → `503`; raising the limit on reload
  takes effect; a rate-limited request **never reaches the queue** (spy queue) and
  a queue refusal does not consume the rate budget.
- **Config:** absent section → shipped defaults + warning; the shipped
  `config/global.settings.yaml` values equal the module defaults; every malformed
  shape refuses; a broken retry hint refuses the depth bound; reload changes the
  bounds; resolution is cached per config signature.
- **Not touched by this file:** the auth-header log invariant and audit records —
  part 2 (§11).

Full suite: **2108 passed, 20 deselected** (baseline on `origin/main`: 2053
passed, same 20 deselected). `scripts/pre_restart_check.py` green.

## 11. Metadata audit and the auth-header log invariant (part 2)

Part 1 reserved this section and shipped no audit code. This is its fill:
§1-§10 above are unchanged. Implementation: `app/proxy/audit.py` (record schema,
sinks, bounds) plus one `mark_denial` call per enforcement point, and one
`AuditMiddleware` that emits exactly one record per request decision on both
surfaces (`app.proxy.server:app` → `surface="proxy"`, `app.main:app` →
`surface="dashboard"`).

### 11.1 The record — one per decision, allow and deny

**Who is audited, and who is not.** Every request that makes an authorization or
admission decision is audited: an authenticated request (allow), and every
refusal on the way to one (deny). A `public` route of the G0-01 matrix never
presents a key and never decides anything, so it produces **no** record — which
is what keeps `/healthz`, the four docs routes and the `/api/*` static mount out
of the trail and out of the buffer. A request that is refused before routing
(the `413` body bound) is audited with `route: null` rather than a concrete path:
the template is the only route form this record allows, and inventing a path
would smuggle request data in.

The record has exactly 15 fields — `RECORD_FIELDS` in `app/proxy/audit.py`, and a
test fails if the producer and that tuple ever disagree:

| Field | Value | Why it is metadata, not data |
| --- | --- | --- |
| `ts` | UTC ISO-8601, milliseconds, `Z` | when the decision happened; carries nothing from the request |
| `event` | always `"audit"` | routing marker for the aggregator/`grep` |
| `surface` | `proxy` \| `dashboard` | which listener answered — deployment fact |
| `decision` | `allow` \| `deny` | the outcome, from a two-value vocabulary |
| `reason` | one code from §11.1's table | *why*, from a closed vocabulary (never a free-form message) |
| `detail` | a bounded machine code (≤ 64 chars), e.g. the G1-02 `REASON_*` of a destination denial | the typed sub-reason, never prose and never a payload |
| `method` | `GET`, `POST`, … (validated shape) | part of the route identity |
| `route` | the FastAPI **template**, e.g. `/v1/{path:path}` | the route, not the request: no model name, no id, no query string |
| `required_scope` | `client`, `operator`, `admin`, `capture-export`, `public` | policy vocabulary (G1-01) |
| `scopes` | the key's resolved scopes, sorted | what the key may do — the key's *envelope*, not its secret |
| `key_fingerprint` | `sha256(token)[:12]`, the G1-01 identity | stable identity, one-way; the token itself is not recoverable from it |
| `key_name` | the operator-assigned key name, cleaned, ≤ 64 chars — `null` when `audit.include_key_name: false` | a label from the key store, not credential material; see below |
| `status` | the HTTP status the client received | the outcome the caller saw |
| `limit` | the config key of the bound that refused (`admission.max_body_bytes`, `admission.queue_max_depth`, `admission.rate_limit.requests_per_minute`) | names a configured limit, never a request value |
| `limit_value` | that bound's configured number | a deployment constant |

**`key_name`: included, with an off switch.** The name is *not* a secret — the
token is, and a name cannot be replayed as a credential — and it is the label the
operator, the dashboard and the G1-01 `scope_denied`/`scope_grace` lines already
use. An audit trail that only carried 12 hex characters would be unreadable for
attribution: "which client was refused" is the first question anyone asks of it.
But a key can be *named* after a person or a mailbox, and a record shipped to a
shared aggregator then carries personal data. So the inclusion is configurable
(`audit.include_key_name`, default `true`) and the failure direction is privacy:
an unreadable value omits the name instead of guessing (§11.2). The fingerprint
stays either way, so identity is never lost.

**`key_fingerprint` identifies the key that was *presented*.** For an unvalidated
denial (an unknown token, or a `413` refused before authentication) the record
carries the fingerprint of the token in the `Authorization`/`x-api-key` header —
that is what makes failed attempts correlatable — while `key_name` and `scopes`
stay `null` because the key store never resolved them. A validated, allowed
request carries all three.

**Decision vs status.** `decision` records the *authorization/admission* answer;
`status` records what the client then received. The two differ legitimately and
the record states both instead of pretending they are one thing:

| Situation | `decision` | `reason` | `status` |
| --- | --- | --- | --- |
| allowed, served | `allow` | `scope_granted` | `200` (or whatever the route produced) |
| allowed by the G1-01 grace window | `allow` | `scope_grace` | route status |
| allowed, wait budget expired (#45) | `allow` | `scope_granted` | `499` |
| allowed, handler failed | `allow` | `scope_granted` | `5xx` |
| refused by an outbound destination guard that the handler swallowed | `deny` | `destination_denied` | the status the handler still returned |

Reason vocabulary and the enforcement point that marks it (`mark_denial` is
called *before* the raise, so the middleware still emits exactly one record):

| `reason` | Enforcement point | HTTP |
| --- | --- | --- |
| `scope_granted` | `app/proxy/auth.py:_enforce_route_scope` (allow) | — |
| `scope_grace` | same, legacy key inside the G1-01 window (allow) | — |
| `insufficient_scope` | `_enforce_route_scope` | `403` |
| `invalid_scope_declaration` | `_enforce_route_scope` (malformed `metadata.scopes`) | `403` |
| `scope_undeclared_route` | `_enforce_route_scope` (fail-closed inventory gap) | `403` |
| `missing_api_key` | `app/proxy/auth.py:verify_api_key` | `401` |
| `invalid_api_key` | `verify_api_key` | `401` |
| `rate_limited` | `app/proxy/admission.py:enforce_rate_limit` | `429` |
| `queue_full` | `app/proxy/admission.py:queue_depth_error` | `503` |
| `admission_misconfigured` | `admission.py` (body, rate and queue bounds) | `503` |
| `request_too_large` | `admission.py:BodySizeLimitMiddleware` | `413` |
| `destination_denied` | `app/destination_policy.py:DestinationGuardTransport` | `502` (or the handler's status) |
| `other` | reserved: a caller passed a code outside the vocabulary | — |

**Absent by construction** — the field set has no room for them, and the
producer validates every value (`RECORD_FIELDS`, length caps, character classes):

- **Payload / prompt / model output / completion text** — never read by the audit
  path; it is only given the decision.
- **Headers** — a header *value* (including `Authorization`, `x-api-key`,
  `Cookie`) never reaches the producer; only the derived fingerprint does.
- **Query string** — the record carries the route template, so `?trace=…` has no
  field to land in.
- **Client IP, user agent, referer, forwarded-for** — deliberately excluded: an
  IP is not an identity behind a NAT (G1-03 keys the rate bound on the
  fingerprint for the same reason) and it is personal data in an aggregator.
  Authentication failures already keep the local-process attribution in
  `Auth`'s WARNING line, which is a different (operator-local) sink.
- **Model name / request id** — route-level audit; a model is routing data that
  the capture subsystem (a separate, opt-in surface) already covers.
- **Raw token, key prefix, header name** — the fingerprint replaces them.

### 11.2 Sinks, bounds, and the auth-header log invariant

**Three sinks, one source.** `_build_record` is the only producer; every sink
receives the same dict.

| Sink | What it is | Bound |
| --- | --- | --- |
| Log line | one JSON object per decision on logger `Guardian.Audit` (`INFO` for allow, `WARNING` for deny), `"event":"audit"` | one line per request, no accumulation in-process; the file/handler rotation is the existing logging configuration's business |
| Ring buffer | `collections.deque(maxlen=N)` of the newest records, `audit.recent()` | `audit.buffer_size`, default **256**, hard ceiling **1024** records |
| Metric | `guardian_audit_records_total{decision,reason}` on `GET /metrics` | 2 decisions × 14 reasons = **28** series, whatever a caller passes (`reason` is mapped onto the vocabulary; anything else becomes `other`) |

**The memory bound, stated and proved.** The buffer is a `deque` with a fixed
`maxlen`, so appending past it evicts — it cannot grow. A record is a dict of 15
fixed fields with every string capped (route ≤ 120, name ≤ 64, detail ≤ 64,
surface ≤ 16 chars, at most 5 validated scope tokens, integers bounded), which
gives a ceiling of **1024 bytes per serialized record** (`_MAX_RECORD_BYTES`) —
a test asserts that ceiling against pathological input (50 000-character route,
name, detail and limit, a 500-character "fingerprint", `limit_value=10**30`).
Worst case at the shipped default: 256 × 1 KiB = **256 KiB**; at the hard ceiling
1024 × 1 KiB = **1 MiB**. Nothing else in the module is keyed on request data: no
dict, set or list grows with traffic, only the `deque` (fixed) and two integers
(`emitted_count`, the counter). A million requests ⇒ a million log lines and the
same fixed buffer. Tests: `test_ring_buffer_stops_at_its_bound` (60 requests,
bound 8 → 8 retained, 60 emitted), `test_a_huge_configured_buffer_is_clamped`,
`test_metric_reason_labels_stay_bounded` (200 attacker-shaped reasons → only
`other`), `test_one_record_has_a_fixed_size_ceiling`,
`test_records_are_never_retained_from_a_public_flood` (25 unauthenticated
requests → nothing retained).

**Configuration is fail-safe, per key, toward the safe side.** A broken audit
block must not refuse traffic: the sink is observability, and refusing a request
because its *audit line* is misconfigured is an availability attack on the
operator. This is the one G1 row that does **not** fail closed, and the deviation
is deliberate:

| Condition | Behaviour | Why that direction |
| --- | --- | --- |
| section absent | shipped defaults, one WARNING | a config file that predates this record keeps auditing |
| `buffer_size` not a positive integer | default 256, one ERROR | a typo must not silently shrink the trail |
| `buffer_size` above 1024 | clamped to 1024, one ERROR | the bound is a memory guarantee and cannot be configured away |
| `log_decisions` not a boolean | stays `true`, one ERROR | losing the aggregator sink silently is worse than a stray line |
| `include_key_name` not a boolean | **`false`**, one ERROR | privacy: a typo must never *add* a name to a record |
| section present but not a mapping | shipped settings with names **off**, one ERROR | nothing parseable, so the conservative reading wins |

**The invariant: no credential in a log line.** The promise — "do not log auth
headers" (`docs/ROADMAP.md` §G1) — is enforced twice:

1. **Belt (G1-02, reused, not rebuilt).** `app/log_safety.py`'s
   `SecretRedactionFilter` is installed on the root handlers at startup; it
   scrubs every registered secret and every `Bearer`/`Authorization`/`x-api-key`
   shaped value. Part 2 **adds the Guardian key store to the registered secrets**
   (`auth._load_api_keys_uncached` → `register_secrets`, `generate_api_key` →
   `register_secret`): the tokens this service hands out were the one credential
   class the filter did not know, and a bare token has no `Bearer` prefix for a
   shape pattern to catch. The registered set is the size of the key store, so it
   costs a comparison per secret per line, not per-request memory.
2. **Braces (this part).** `log_safety.contains_credentials()` is the detector
   built on the *same* two patterns, and `tests/unit/test_audit_metadata.py`
   drives every decision path — auth success, invalid key, missing key, scope
   denial, rate denial, body denial, queue denial, destination denial — with
   `caplog` at `DEBUG` and fails if **any** captured line carries a credential.
   Because pytest attaches its capture handler *after* `install_secret_redaction()`
   ran, the records scanned are the pre-filter messages the gateway actually
   tried to write: the test fails on an attempted leak even when the filter would
   have saved the line in production.

**Not vacuous, proved.** `test_the_guard_is_not_vacuous` feeds the assertion
helper a line containing `Authorization: Bearer <token>` and requires the
`AssertionError`, plus `x-api-key:` / `Authorization=` / `bearer …` spellings.
`test_a_filterless_handler_would_leak` logs through a handler without the filter
and requires the scan to fire on what a handler really emitted.
`test_the_g1_02_filter_makes_a_leak_unloggable` puts the real
`SecretRedactionFilter` on a probe handler, logs the same token twice and requires
`***redacted***` with the credential gone — so the pair (detector + filter) is
demonstrated, not asserted. `contains_credentials` ignores values that are
already `***redacted***`, so a correctly scrubbed production log is not reported
as a leak.

**What the invariant does not claim.** `uvicorn.access` logs the request line
(path and query, no headers) on its own handlers; that is transport logging, not
this gateway's logging, and it is unchanged here. The invariant is stated for
Guardian's own log records at every level, `DEBUG` included.

### 11.3 The audit-without-payload demonstration, and the tests

`tests/unit/test_audit_metadata.py` — **39 tests**, in five groups:

- **`TestAllowRecords` (7).** The record carries exactly `RECORD_FIELDS`; allow on
  the inference catch-all route is audited by its template (`/v1/{path:path}`,
  `POST`, `499` from the wait budget); the grace window is marked `scope_grace`
  with the grandfathered envelope; the dashboard surface reports
  `surface="dashboard"`; `public` routes produce nothing; a mutating method is
  recorded as such.
- **`TestDenyRecords` (9).** One test per denial path named in the acceptance
  criteria — scope denial, invalid scope declaration, undeclared route, invalid
  key, missing key, **rate limit**, **queue depth**, queue `admission_misconfigured`,
  **body too large**, and **destination denied** (twice: once through a real
  guarded client on a probe route, once through a real Guardian request where the
  denial lands on that request's record).
- **`TestAuditWithoutPayload` (3).** The demonstration itself: one allowed and one
  denied request, each carrying a recognisable payload marker, a recognisable
  `X-Request-Marker` header, a recognisable `Authorization: Bearer …` value and a
  recognisable query marker. The record (as JSON) and **every** captured gateway
  log line are asserted to contain none of: the payload marker, the header
  marker, the token, the token's random part, `Bearer <token>`, the query marker,
  and a distinctive phrase from the prompt. A third test pins the schema closure
  (`body`, `headers`, `query`, `prompt`, `model` are not fields).
- **`TestAuthHeaderInvariant` (4).** The exercised-path invariant (§11.2), the
  non-vacuity proof (leaky line must fail), the filter proof, and the
  filterless-handler proof.
- **`TestBoundedStorage` (10) / `TestAuditConfiguration` (5).** The bounds of
  §11.2 and the fail-safe configuration directions, plus
  "shipped `config/global.settings.yaml` equals the module defaults".

Full suite: **2155 passed, 20 deselected** (measured baseline in a clean
`origin/main` (`f6b9bc0`) worktree: 2116 passed, same 20 deselected — the +39 is
exactly this file). `scripts/pre_restart_check.py` green.

### 11.4 Cost, consequences and what is not covered

- **Per decision:** one `deque` append, one `json.dumps` of a ≤ 1 KiB dict, one
  counter increment, one log line through the existing handler. No I/O of its own,
  no lock held across an `await`, no task. A refusal is audited on the way out, so
  an attacker who sends a million refused requests costs a million log lines —
  the same cost the existing rejection lines already had — and a bounded buffer.
- **The log is the durable sink.** The ring buffer is volatile by design: it
  resets with the process and it is bounded, so the operator's aggregator (or
  `guardian.log`) is the record of truth. `audit.recent()` exists for a
  post-incident look without log shipping and for the tests; there is deliberately
  **no HTTP endpoint** for it — that would add a 58th route to the pinned G0-01
  matrix to serve data the log already ships, and an audit surface with its own
  access-control question is a bigger decision than this record makes.
- **How the operator queries it:** `journalctl -u guardian-llmprovider-gateway |
  grep '"event":"audit"'` (or `grep '"event":"audit"' guardian.log`), filtered by
  `"decision":"deny"`, `"reason":"rate_limited"`, `"key_fingerprint":"…"`; the
  counter `guardian_audit_records_total{decision,reason}` on `GET /metrics` gives
  the rate. `docs/API_REFERENCE.md` "Audit records" documents the line format and
  the queries.
- **Single process, single sink.** No Redis, no queue, no file of its own: two
  replicas would each keep its own buffer (same scope statement as §2.2).
- **Not covered:** audit of *payload capture* decisions (capture stays as it is,
  §"Out of scope" of the issue); audit of the `/metrics` scrape itself beyond its
  ordinary allow record; retention/rotation of the log file (the existing
  logging configuration owns that); and any tamper-evidence (a local log is not
  append-only evidence — that would need an external sink).
- **Known adjacent gap, not fixed here:** `app/local_inference/ollama.py` builds
  two bare `httpx.AsyncClient(...)` clients, which bypass the G1-02 destination
  guard. It is a G1-02 scope defect (no audit record is produced for those hops
  either), and it belongs in its own issue rather than in this record's diff.
