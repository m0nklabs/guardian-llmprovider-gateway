# G1-03 — Bounded admission (part 1) and metadata audit (part 2)

| Field | Value |
| --- | --- |
| **Status** | Accepted (part 1). Part 2 reserves §11 and is not in this record yet. |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |
| **Issue** | [#57](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/57) — this record covers **the admission half only** |
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

## 11. Reserved for part 2 — metadata audit (not implemented here)

Part 2 of [#57](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/57)
extends **this same record**. Sections are append-only: part 2 fills §11 and may
add §12+, it must not renumber §1-§10.

- **§11.1 Audit record shape.** One metadata-only record per authorization
  decision (allowed *and* denied): identity fingerprint, route, required scope,
  outcome, reason, timestamp. No payload, no prompt text, no headers, no raw
  token.
- **§11.2 The auth-header log invariant.** An automated test asserting that a
  `Bearer`/`api-key` value never appears in a log line at INFO/DEBUG — the
  promise G1-02's redaction filter makes (`app/log_safety.py`) is the belt; part 2
  adds the braces and the test.
- **§11.3 Audit tests.** Allow and deny both produce a record; no payload/prompt
  content is included; a captured-log assertion for §11.2.

What part 1 already contributes to that work: the rejection log lines in §8 use
identity fingerprints (never tokens or headers), and the three admission
decisions are exactly the kind of event the audit record will carry — part 2 can
attach to the same call sites instead of re-deriving the decision.
