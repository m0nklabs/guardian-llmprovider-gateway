# G0-04 — First non-LLM HTTP service: decision, contract and fixture

Decision record for `docs/ROADMAP.md` §4 G0 ("Decide the first non-LLM/non-speech
service") and backlog row G0-04. It resolves the first item of §6 **Open
decisions → first HTTP target**, and fixes the read-only operation contract,
the fixture and the adapter boundary that G2-01 must build against.

- **Maintainer (upstream service):** `monifuse.service` — systemd **user** unit,
  `/home/flip/.config/systemd/user/monifuse.service`.
- **Drift guard:** `tests/unit/test_g0_first_http_service.py`.
- **Fixtures:** `tests/fixtures/monifuse/`.

**Verification status of this document.** Every response shape below was read
from the live service with `GET` only, on 2026-10-28 between 08:33 and 08:38 UTC.
No `POST`/`PUT`/`PATCH`/`DELETE` was sent to MoniFuse or to anything else, no
service was restarted and no configuration was changed. Claims that cannot be
established read-only are marked `unverified:` where they occur (§3.3, §3.4.5,
§6) rather than inferred.

## 1. Decision

**First registered non-LLM service: the MoniFuse read API at
`http://127.0.0.1:7994`, with exactly two operations in scope —
`GET /api/registry/ports` and `GET /api/agentlog`.**

The target is an operator decision, not an agent preference: the survey was
delivered as evidence and the operator confirmed this target in
[m0nk111's comment of 2026-10-28T08:32:48Z on #43](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/43#issuecomment-6464365573)
("Operator decision — target confirmed"). This document does not re-argue the
choice; it records the contract that follows from it.

### 1.1 Owner and reachability, as observed

| Evidence | Observation (2026-10-28) |
|---|---|
| Listening socket | `ss -ltnp` → `LISTEN 0 2048 0.0.0.0:7994` owned by `uvicorn`, pid 3655907 |
| Owning unit | `systemctl --user status monifuse.service` → `active (running) since Sat 2026-10-17 09:34:19 UTC`, `Main PID: 3655907 (uvicorn)` |
| Cgroup proof | `/proc/3655907/cgroup` → `app.slice/monifuse.service` |
| Unit declaration | `ExecStart=/home/flip/monifuse/backend/venv/bin/uvicorn main:app --host 0.0.0.0 --port 7994`, `Restart=always` |
| Live probe | `HTTP/1.1 200 OK`, `server: uvicorn`, `content-type: application/json`, `vary: Origin` |
| API identity | `GET /openapi.json` → `{"title": "MoniFuse", "version": "0.1.0"}`, 23 paths, 13 `GET`, 12 write operations |
| Authentication on reads | none — no key, no cookie, no mTLS. See §3.3 |
| GPU/model classification | `GET /api/config` → `{"id": "monifuse", "unit": "monifuse.service", "type": "user", "gpu_heavy": false, "safe_to_kill": true, "status": "active"}` |
| Public exposure | `~/.cloudflared/config.yml` lists 12 ingress hostnames; **none** targets port 7994 (it tunnels 5432, 7998, 9000, 5174, 8080, 8095, 8096, 3086, 3087, 3046 and a `*.oord.dev` wildcard) |

The service binds `0.0.0.0` (cleartext, no TLS). It is reachable from the LAN at
`192.168.1.2:7994`, but it is not published through the cloudflared ingress, so
"private" here means *loopback plus the operator's own LAN*, not
internet-private. Guardian's registered target is the loopback address.

### 1.2 How it satisfies the roadmap criteria

| §4 G0 criterion | How MoniFuse meets it |
|---|---|
| Private | Loopback (`127.0.0.1:7994`) for Guardian; absent from the cloudflared ingress list, so no public route |
| Read-only contract | Both operations in scope are `GET` and touch no upstream state; the same origin also carries writes, which is why the adapter is an allowlist (§4) |
| No model/GPU dependency | Plain FastAPI/uvicorn process; its own registry marks it `gpu_heavy: false`; no inference call in either operation |
| Small enough to specify completely | Two paths, six port fields (four required, two optional) and a two-field envelope, 6,195 B and 1,177 B responses; the whole contract fits in §3 |
| Stable contract | Versioned FastAPI app with published OpenAPI; shape changes are visible in `/openapi.json` (caveat: `0.1.0`, see §1.3) |
| Clear owner | `monifuse.service` is the operator's own control plane; breakage is noticed immediately |
| Fixture possibility | Deterministic JSON, small payloads, no secrets — captured verbatim in §3.1.2 / §3.2.4, provenance in §5 |
| Useful beyond the smoke test | The port registry is the service/port inventory G0/G1 need, and the agent log is the activity feed the same inventory is audited against |

### 1.3 What the decision trades away

These are accepted costs, recorded so G2-01 does not rediscover them:

- **No authentication boundary is exercised.** MoniFuse reads are
  unauthenticated, so this first slice proves routing, allowlisting, timeouts
  and fixture handling — but *not* upstream credential handling or the G1
  auth-scope work. A keyed upstream (Caretaker) would have exercised it and lost
  on the "no GPU dependency" criterion.
- **`0.1.0` is not semver-stable.** A minor bump may change a response shape;
  the drift guard only pins the shapes documented here, so a break shows up as
  a failing test rather than as silent adapter corruption.
- **`/api/registry` is slow.** `GET /api/registry` (the full cross-repository
  scan, *not* in scope) was measured at 3.65 s / 38,740 B on 2026-10-28; the G0-04
  survey recorded it exceeding a 6 s client timeout. `/api/registry/ports` is
  the cheap projection and is the operation in scope.
- **`/api/registry/ports` is not free either.** It re-reads the per-repository
  registry on each call: 0.48 s / 0.97 s / 1.27 s across three probes. G2-01
  needs a per-operation timeout that tolerates ~2 s, and no per-request fan-out.
- **Data quality is upstream's, not Guardian's.** The registry contains stale
  entries (six documented ports with no listener, per the survey's probe — not
  re-probed here) and duplicates (see §3.1.4);
  the agent log's `host` field is free-form. Guardian must pass this data
  through unaltered and must not treat the registry as a destination allowlist.

## 2. Rejected candidates

From the G0-04 survey
([survey comment, 2026-10-28T08:19:51Z](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/43#issuecomment-6464143354)).
Each row lost against the criteria in §1.2; nothing here was rejected on
documentation alone.

| Candidate | Reach / auth | Why it lost |
|---|---|---|
| Caretaker | `127.0.0.1:11441`, Bearer key mandatory (401 without) | Strong runner-up, but the unit exists to manage GPU model services — it loses "no model/GPU dependency" in spirit — and its mutating neighbours (`/ensure`, `/release`, `/unload`) are the same origin |
| Stepper Height API (.201) | `192.168.1.201:7996`, no auth | Viable contract, but a **third host**: no systemd unit visible from here, so the "clear owner" evidence is weaker; same base URL carries motion `POST`s and a config `PUT` |
| Guardian itself | `127.0.0.1:11434` | Circular: Guardian would proxy itself; `/api/*` needs a key while `/healthz` and `/metrics` are open |
| Caddy admin `:2019` | loopback, no auth | Config **control plane** of a public site — the read leaks the whole reverse-proxy configuration |
| Frigate | `127.0.0.1:5000` | GPU-backed (TensorRT) and camera imagery: model/GPU dependency, privacy cost |
| MinIO | `127.0.0.1:9000` | Public via `storage.oelala.xyz`; only `/minio/health/live` is readable without SigV4 |
| oelala backend | `127.0.0.1:7998` | Public via `api.oelala.xyz`; ComfyUI/GPU-dependent, `/health` only |
| monerod | `127.0.0.1:18080` | JSON-RPC over `POST`; `GET` is unsupported (`curl: (52)`), so the read-only contract cannot be met |
| cam3-viewer | `127.0.0.1:8090` | Personal camera imagery, static HTML |
| project-status | `127.0.0.1:3081` | Ad-hoc HTML, no JSON contract to specify |
| rustyfxp | `127.0.0.1:5001` | HTML UI with a stub `/api/status` |
| crewai watcher | `127.0.0.1:8509` | LLM-log adjacent, log viewer without a contract |
| litellm / open-webui / copilot-api / claude router | 4000 / 8081 / 4141 / 11442 | Model-backed, excluded by G0 |
| Six registry entries with no listener | `.2:7999`, `.6:8100`, `.62:7990`, `.35:8765`, `.35:9119`, `.35:50001` | `curl` → `(7) Failed to connect`; stale documentation, not services |

## 3. The contract

### 3.0 Operations in scope, and their common properties

Exactly two operations make up the registered contract. Every row is read-only
and names the fixture that pins it; the drift guard parses this table, so a new
row requires a fixture and a contract section (§3.1, §3.2).

| Operation | Fixture | Contract section | Read-only |
|---|---|---|---|
| `GET /api/registry/ports` | `tests/fixtures/monifuse/registry_ports.json` | §3.1 | yes |
| `GET /api/agentlog` | `tests/fixtures/monifuse/agentlog.json` | §3.2 | yes |

| Property | Value |
|---|---|
| Base URL | `http://127.0.0.1:7994` (cleartext HTTP; no TLS listener on 7994) |
| Authentication | **none** — no `Authorization`, cookie, mTLS or client certificate is required or evaluated |
| Method | `GET` only (see §3.4 for the `HEAD` behaviour) |
| Success status | `200` |
| Success `content-type` | `application/json` |
| Response framing | `content-length` present; no chunking observed; no `ETag`, no `Last-Modified`, no `Cache-Control` on either operation (the SPA fallback *does* send `Cache-Control: no-cache, no-store, must-revalidate`, which is a useful tell — §3.4) |
| Server header | `server: uvicorn` |
| Compression | not negotiated; no `content-encoding` observed |
| Versioning | none in the path; the app version is `0.1.0` and `info.version` is the only version signal |

The service exposes **23 paths: 13 `GET` and 12 write operations**, and it also
mounts a single-page-app catch-all (`GET /{catchall}`). The catch-all is why an
unknown `/api/...` path is *not* a `404` — see §3.4.3.

### 3.1 `GET /api/registry/ports`

- **Method / path:** `GET /api/registry/ports`
- **Summary in OpenAPI:** "Get Reserved Ports"; `operationId`
  `get_reserved_ports_api_registry_ports_get`.
- **Query parameters:** none. The endpoint declares no parameters and honours
  none: `?limit=2` returned a response byte-identical to the unfiltered one, and
  an unknown parameter (`?bogus=1`) is ignored rather than rejected.
- **Authentication:** none.
- **Response:** a **bare JSON array** (no envelope) of port-reservation objects.
  Live size on 2026-10-28: 50 entries, 6,195 B; two consecutive calls returned
  byte-identical bodies.

#### 3.1.1 Field-by-field meaning

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `port` | integer | always | Reserved TCP/UDP port number |
| `protocol` | string | always | `tcp` or `udp`; the only two values observed (48 × `tcp`, 2 × `udp`) |
| `ip` | string | always | The bind address the *reservation* claims (a loopback address or a LAN IP). **Not verified live by the endpoint** — a claimed address may have no listener |
| `host` | string | optional (9 of 50) | Logical host name when the reservation belongs to another machine than the API host; absent means "the MoniFuse host" |
| `description` | string | always | Short human label for the service on that port |
| `notes` | string | optional (19 of 50) | Free-form operational note: a sentence, a repository-relative hint or an explicit warning. Not machine-parseable |

#### 3.1.2 Captured example (trimmed to three entries)

Full capture is 50 entries / 6,195 B; the three entries below are copied
**verbatim** from it (only the count changed — trimming an array edits no field).
The `11440` entry is included because it is the only captured shape that carries
the optional `host` field. See `tests/fixtures/monifuse/registry_ports.json`.

```json
[
  {
    "port": 7994,
    "protocol": "tcp",
    "ip": "192.168.1.2",
    "description": "MoniFuse Backend",
    "notes": "Primary FastAPI control and registry API"
  },
  {
    "port": 11440,
    "protocol": "tcp",
    "ip": "127.0.0.1",
    "host": "ai-kvm2",
    "description": "llama-server backend",
    "notes": "Guardian-managed backend port; keep internal unless debugging Guardian itself"
  },
  {
    "port": 7993,
    "protocol": "tcp",
    "ip": "127.0.0.1",
    "description": "Dev Web Server",
    "notes": "Vite frontend paired with backend port 7994"
  }
]
```

#### 3.1.3 Pagination and limits

**None.** No `limit`, `offset`, `cursor` or `page` parameter exists and none is
honoured; the whole registry is returned in one response. A consumer that only
needs a subset must filter client-side. The array size is the number of
reservations in the upstream registry (50 at capture time) and grows with it.

#### 3.1.4 Documented data characteristics (not bugs of the contract)

- `(port, protocol)` is **not unique**: `8000/tcp` appears twice —
  `192.168.1.6` ("Trading REST API") and `127.0.0.1` on `windows-unreal`
  ("Incredibuild Manager"). A consumer keying on `(port, protocol)` must expect
  collisions; keying on `(host, ip, port, protocol)` is the honest key.
- Entries can be **stale**: the G0-04 survey found six entries whose hosts had no
  listener (not re-probed here). The endpoint reports intent, not liveness.

### 3.2 `GET /api/agentlog`

- **Method / path:** `GET /api/agentlog`
- **Summary in OpenAPI:** "Agentlog Get"; `operationId`
  `agentlog_get_api_agentlog_get`.
- **Authentication:** none.
- **Response:** a **JSON object envelope** with exactly two top-level members:
  `events` (array) and `total` (integer). Live sizes: unfiltered `?limit=2`
  1,140 B; the project-filtered capture in §3.2.4 1,177 B; the full store at
  capture time 1,478 events / ~795 KB.

#### 3.2.1 Query parameters actually honoured

All six are declared in OpenAPI **and** were verified live on 2026-10-28 (each
check was a `GET`, applying the filter and comparing `total`):

| Parameter | Type | Default | Honoured? | Semantics as observed |
|---|---|---|---|---|
| `limit` | integer | `200` | yes | Number of events returned. Values `<= 0` (tested `0`, `-1`) are clamped to **1**, not rejected and not "unlimited". Values above the stored count return everything (`limit=5000` and `limit=100000` both returned 1,478). No upper bound is enforced on the parameter itself |
| `agent_id` | string | none | yes | Exact, case-sensitive match (`agent_id=dsh-ai-kvm2-guardian` → `total` 8; a non-existent id → `0` events / `total` 0, still `200`) |
| `host` | string | none | yes | Exact, case-sensitive match on the event's `host` string |
| `event` | string | none | yes | Exact, case-sensitive match on `event`. **Not** an enum and **not** a substring match: `goal_achieved` → 36, `goal` → 0, `GOAL_ACHIEVED` → 0 |
| `project` | string | none | yes | Exact, case-sensitive match on `project` (`project=guardian-llmprovider-gateway` → `total` 158) |
| `since` | string | none | yes | Keeps events with `ts >= since`, compared **as strings**: works for `2026-10-28` and `2026-10-28T08:00:00+00:00`, silently returns 0 for an unparseable value (`since=nonsense` → `0`, `200`), and is **format-sensitive** — `2026-10-28T08:20:47Z` excluded an event whose `ts` is `2026-10-28T08:20:47.638941+00:00` while `2026-10-28T08:20:47+00:00` included it. Use the same `+00:00` form the service emits |

Unknown parameters are ignored, not rejected (`?limit=2&bogus=1` returned a
response byte-identical to `?limit=2`).

#### 3.2.2 Response shape — envelope

| Field | Type | Meaning |
|---|---|---|
| `events` | array of objects | The matching events, **oldest first** (ascending `ts`), truncated to the newest `limit` matches — i.e. the *tail* of the filtered store, not the head. Verified: `limit=1000` returned exactly the last 1,000 of 1,478 in the same order |
| `total` | integer | Number of events matching the filters **ignoring `limit`**. `?limit=200` → `total` 1,478 with 200 events; `?project=…&limit=2` → `total` 158 with 2 events. **`total` is not a page count and is not tied to `len(events)`** |

#### 3.2.3 Response shape — one event object

All fourteen members were present on 1,000 sampled events (no field is optional
in practice, even where it is empty):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Sortable event id, `<UTC yyyymmddHHMMSSmmmmmm>-<6 hex>`, e.g. `20261028082047638886-c0870b`. Unique in the sample |
| `ts` | string | Creation timestamp, ISO-8601 with microseconds and an explicit `+00:00` offset |
| `agent_id` | string | Reporting agent identity, e.g. `dsh-ai-kvm2-guardian` (free-form, not an enum) |
| `agent_type` | string | Agent class; observed `dsh`, `custom`, `watchdog`, `unknown` |
| `host` | string | Reporting host. **Free-form and messy**: observed both `192.168.1.35` and `ai-kvm2`, an empty string, and one entry with a space-separated list of every interface address. Do not parse it as an address |
| `workspace` | string | Absolute path of the agent's working directory |
| `project` | string | Project name (free-form; normally a repository name) |
| `event` | string | Lifecycle event kind; observed `task_started`, `task_achieved`, `task_failed`, `goal_achieved`, `blocked`, `note`. Not an enum |
| `title` | string | One-line summary (user-facing prose, any language) |
| `details` | string | Free-text detail; empty string when unused (empty on 874 of 1,000 sampled) |
| `artifacts` | array of strings | Pointers produced by the event: absolute paths and/or URLs; `[]` when none |
| `outcome` | string | Result classification; observed `success`, `partial`, `failed`, `blocked`, `started`, `in_progress`, and the empty string |
| `model` | string | Implementing model id when known, empty string otherwise. **Metadata only** — it must not be interpreted as a routing decision |
| `metrics` | object | Free-form numeric metrics; `{}` on every sampled event |

#### 3.2.4 Captured example

`GET /api/agentlog?limit=2&project=guardian-llmprovider-gateway` → `200`,
1,177 B, `content-type: application/json`, captured 2026-10-28T08:34:42Z. Copied
**verbatim** (no field edited; the two events both concern this repository, so
nothing needed stripping). See `tests/fixtures/monifuse/agentlog.json`.

```json
{
  "events": [
    {
      "agent_id": "dsh-ai-kvm2-guardian",
      "agent_type": "dsh",
      "host": "192.168.1.35",
      "workspace": "/home/flip/guardian-llmprovider-gateway",
      "project": "guardian-llmprovider-gateway",
      "event": "task_achieved",
      "title": "Roadmap G0-02 threat model merged (1f2d813); model has merge authority via gate; G0-03 baseline started",
      "details": "",
      "artifacts": [
        "https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/40"
      ],
      "outcome": "success",
      "model": "openrouter/deepseek/deepseek-v4.1-flash-1017",
      "metrics": {},
      "id": "20261027232037502623-bd78d5",
      "ts": "2026-10-27T23:20:37.502715+00:00"
    },
    {
      "agent_id": "dsh-ai-kvm2-guardian",
      "agent_type": "dsh",
      "host": "192.168.1.35",
      "workspace": "/home/flip/guardian-llmprovider-gateway",
      "project": "guardian-llmprovider-gateway",
      "event": "task_achieved",
      "title": "G0-03 merged (60c5632); loop ops rules merged; G0-04 candidate survey delivered, target decision pending with operator",
      "details": "",
      "artifacts": [
        "https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/43"
      ],
      "outcome": "success",
      "model": "openrouter/deepseek/deepseek-v4.1-flash-1017",
      "metrics": {},
      "id": "20261028082047638886-c0870b",
      "ts": "2026-10-28T08:20:47.638941+00:00"
    }
  ],
  "total": 158
}
```

Note how the example itself demonstrates the `total` semantics: 2 events,
`total` 158.

#### 3.2.5 Pagination and limits

There is **no cursor and no offset**. The only paging primitive is
`limit` + `total` + client-side `since`:

- To page backwards (older events), **omit `since` entirely and grow `limit`**.
  `since` is a *lower* bound (`ts >= since`), so pinning it to the oldest `ts`
  of the current page keeps exactly that page and its successors and excludes
  everything older — repeatedly raising `limit` with `since` set therefore never
  reaches earlier history. The response is the newest `limit` events of the
  filtered set in ascending order, so a larger `limit` means an earlier head.
- If a follow-up poll wants only *new* events, the opposite applies: pin
  `since` to the newest `ts` already consumed and leave `limit` small. Both
  directions are string comparisons on `ts` (see 3.2.4), so the pinned value
  must be copied verbatim from a response, never reformatted.
- `total` (ignoring `limit`) is the only count available; there is no
  "has more" flag. A consumer therefore cannot cheaply confirm it has seen
  everything except by fetching with a `limit` at or above `total`.
- The practical ceiling is the store size: on 2026-10-28, `limit=100000`
  returned all 1,478 events (~795 KB) in ~0.04–0.06 s. Treat this as
  "unbounded growth" rather than as a cap — the store is append-only, so a full
  fetch will not stay small.

### 3.3 Authentication: explicitly none

Both operations answered with **no credentials** at all — no `Authorization`
header, no cookie, no client certificate, no `X-*-token` header. This is a
property of the upstream, recorded so G1 does not mistake the first slice for an
authenticated integration:

- Guardian's adapter must **not** attach any upstream credential to these two
  operations; there is nothing to authenticate against, and sending a Guardian
  client key upstream would leak a Guardian credential to a third service.
- Because reads are open, the slice proves nothing about credential isolation,
  header stripping or the G1 auth-scope model.
- The *write* operations on the same origin declare `x-admin-secret`
  (`POST /api/registry/ports`) and `x-agent-token` (`POST /api/agentlog`)
  headers, both marked `required: false` in OpenAPI. `unverified:` whether those
  writes are actually enforced — establishing that would require a write, which
  is out of scope, and the answer must not be assumed from the OpenAPI schema.

### 3.4 Error shapes

Observed read-only on 2026-10-28. All of these were produced with `GET`, plus two
header-level probes (`HEAD` and `OPTIONS` — §3.4.2); no mutating verb was used
against any service.

#### 3.4.1 `422` — parameter type violation (only on `/api/agentlog`)

`GET /api/agentlog?limit=abc` → `HTTP/1.1 422 Unprocessable Content`,
`content-type: application/json`, body:

```json
{
  "detail": [
    {
      "type": "int_parsing",
      "loc": ["query", "limit"],
      "msg": "Input should be a valid integer, unable to parse string as an integer",
      "input": "abc"
    }
  ]
}
```

This is FastAPI's default validation envelope: `detail` is a **list** of error
objects with `type`, `loc`, `msg`, `input`. `GET /api/registry/ports` declares no
parameters, so it has no reachable `422` on that method.

#### 3.4.2 `405` — method not allowed

`HEAD /api/registry/ports` → `HTTP/1.1 405 Method Not Allowed`,
`allow: GET`, `content-length: 31`, `content-type: application/json`.
The status is `405` and the allowed-method hint is in the `allow` header. A
`HEAD` response carries no body, so the same rejection was re-read with
`OPTIONS /api/registry/ports` (read-only, no state change): also `405`,
`allow: GET`, `content-length: 31`, and the body is FastAPI's default

```json
{"detail": "Method Not Allowed"}
```

so a `405` is a **string** `detail`, unlike the list-shaped `422`.

#### 3.4.3 `200 text/html` — the catch-all trap (most important for the adapter)

There is **no `404` for unknown `/api/*` paths**. The app mounts
`GET /{catchall}` serving the single-page app, so:

| Request | Status | `content-type` | Body |
|---|---|---|---|
| `GET /api/nope` | `200` | `text/html; charset=utf-8` | 455 B Vite `index.html` |
| `GET /api/registry/no-such-thing` | `200` | `text/html; charset=utf-8` | 455 B Vite `index.html` |
| `GET /api/registry/ports/999` | `200` | `text/html; charset=utf-8` | 455 B Vite `index.html` |
| `GET /api/registry/ports/` (trailing slash) | `200` | `text/html; charset=utf-8` | 455 B Vite `index.html` |
| `GET /api/agentlog/` (trailing slash) | `200` | `text/html; charset=utf-8` | 455 B Vite `index.html` |

The fallback also sends `cache-control: no-cache, no-store, must-revalidate`,
which the two real operations do not. Consequences G2-01 must implement:

1. **A `2xx` is not success.** The adapter must require
   `content-type: application/json` *and* parseable JSON of the documented
   top-level shape; an HTML `200` is a failed upstream call.
2. **Paths must be matched exactly**, including no trailing slash — a trailing
   slash silently degrades to the SPA instead of redirecting or erroring.

#### 3.4.4 `404` — reachable only under the static mount

`GET /assets/missing.js` → `HTTP/1.1 404 Not Found`, `content-length: 22`,
`content-type: application/json`, body `{"detail":"Not Found"}`. That `404` comes
from the mounted static files, **not** from the API: every unknown `/api/*` path
still falls through to the SPA and returns `200 text/html` (§3.4.3). So a `404`
from this origin says nothing about the API surface; an adapter that survives on
`404` as "not found" will never see one for an API path.

#### 3.4.5 Not provoked (and why)

- `404` for an `/api/*` path — structurally impossible because of the catch-all
  (§3.4.3, §3.4.4).
- `5xx` and timeouts — `unverified:`. Provoking them read-only is not possible:
  the honest ways to force a `500`/`503` are a malformed upstream registry file
  or a stopped dependency, both of which require a change to the service or its
  state. No such attempt was made. The adapter must therefore treat `5xx` as
  "unknown failure" with a bounded timeout, not as a shape it can trust.
- Redirects (`3xx`) — `unverified:`; none was observed on either operation.

### 3.5 Exact capture commands

Every capture used the loopback address. The two fixture captures are `GET`;
the error-shape probes add `HEAD` and one `OPTIONS`, both read-only, and **no
write verb was sent to any service**:

```bash
# 1. Port registry — 50 entries, 6,195 B, HTTP 200
curl -sS -D- 'http://127.0.0.1:7994/api/registry/ports'

# 2. Agent log, two events of this project — 1,177 B, HTTP 200
#    (the fixture source; ?limit=2 alone returns the two newest events globally)
curl -sS -D- 'http://127.0.0.1:7994/api/agentlog?limit=2&project=guardian-llmprovider-gateway'

# 3. The app contract itself — used to enumerate paths and parameters
curl -sS 'http://127.0.0.1:7994/openapi.json'

# 4. Error shapes (read-only; no write verb)
curl -sS -D- -o /dev/null 'http://127.0.0.1:7994/api/nope'                 # 200 text/html
curl -sS -I  'http://127.0.0.1:7994/api/registry/ports'                    # 405, allow: GET
curl -sS -D- -X OPTIONS 'http://127.0.0.1:7994/api/registry/ports'         # 405 + JSON detail
curl -sS -D- 'http://127.0.0.1:7994/assets/missing.js'                     # 404 JSON detail
curl -sS -D- 'http://127.0.0.1:7994/api/agentlog?limit=abc'                # 422 JSON

# 5. Filter/limit semantics — each read-only, compared by `events` length + `total`
curl -sS 'http://127.0.0.1:7994/api/agentlog?limit=1'
curl -sS 'http://127.0.0.1:7994/api/agentlog?limit=0'          # clamps to 1 event
curl -sS 'http://127.0.0.1:7994/api/agentlog?event=goal_achieved&limit=3'
curl -sS 'http://127.0.0.1:7994/api/agentlog?agent_id=dsh-ai-kvm2-guardian&limit=2'
curl -sS 'http://127.0.0.1:7994/api/agentlog?host=192.168.1.35&limit=2'
curl -sS 'http://127.0.0.1:7994/api/agentlog?project=guardian-llmprovider-gateway&limit=2'
curl -sS 'http://127.0.0.1:7994/api/agentlog?since=2026-10-28&limit=5'
```

## 4. Adapter boundary (binding rule for G2-01)

**Rule: the adapter addresses exactly two operations by an internal path
allowlist, and never builds an upstream URL from caller input.**

```
allowed = {
    ("GET", "/api/registry/ports"),
    ("GET", "/api/agentlog"),
}
```

- The allowlist is keyed on **method + exact path**, not on a base URL, prefix or
  regex. The base URL (`http://127.0.0.1:7994`) may be configuration; the path
  may not come from the request.
- **No base-URL pass-through.** Forwarding a caller-supplied path below the
  MoniFuse origin is prohibited, because that origin also carries **12 write
  operations** — `POST /api/agentlog`, `POST /api/registry/ports`,
  `DELETE /api/registry/ports/{port}/{protocol}`, `POST /api/lifecycle/event`,
  `POST /api/llm-analysis/update`, `POST /api/portfolio/dod/run`,
  `POST /api/registry/project/{project_id}/{ci,dod,dod/run,status}`,
  `POST /api/service/{service_id}/free_vram` and
  `POST /api/service/{service_id}/{action}`. A prefix allowlist would expose
  all of them, including a service-level `{action}` endpoint and a VRAM free.
- **Query parameters:** `/api/agentlog` passes through only the six documented
  filters (`limit`, `agent_id`, `host`, `event`, `project`, `since`), each
  validated and bounded by the adapter (`limit` defaulted and clamped by
  Guardian too, since the upstream clamps `<= 0` to 1 and has no upper bound).
  `/api/registry/ports` passes **no** query parameters.
- **Response validation:** require `200` + `application/json` + the documented
  top-level shape (array of objects / object with `events` + `total`). An HTML
  `200` from the SPA catch-all is a failure (§3.4.3).
- **No credentials** are attached (§3.3), and no upstream response is cached
  across clients unless the adapter explicitly implements it — the upstream sends
  no cache validators.
- **Timeouts:** sized for the operations, not for a generic proxy — the policy
  path needs ≥ 2 s for `/api/registry/ports` (measured 0.48–1.27 s) while
  `/api/agentlog` answers in ~0.04–0.06 s. A single global 1 s timeout would
  make the first operation flaky.

G2-01 must implement the slice behind this rule, and
`tests/unit/test_g0_first_http_service.py` guards the documented half of it: the
paths above, their fixtures, and the absence of an advertised write path.

## 5. Fixture provenance

| Fixture | Operation | Trimmed from | Size as committed |
|---|---|---|---|
| `tests/fixtures/monifuse/registry_ports.json` | `GET /api/registry/ports` | 50 entries / 6,195 B → **3 entries** | 575 B (2-space JSON) |
| `tests/fixtures/monifuse/agentlog.json` | `GET /api/agentlog?limit=2&project=guardian-llmprovider-gateway` | 2 events / 1,177 B, **not trimmed** | 1,466 B (2-space JSON; the minified response body is 1,177 B) |

Both files are committed with 2-space indentation for reviewability. The values
are unchanged from the capture: re-serializing each committed file yields an
object identical to the captured response body (the only difference is
whitespace).

- **Captured:** 2026-10-28, between 08:33 and 08:35 UTC, from the live
  `monifuse.service` (pid 3655907) via loopback, `GET` only, commands in §3.5.
- **Trimming:** the registry fixture is the captured array reduced from 50 to 3
  entries — entries `7994` (this service, no `host`), `11440` (the only captured
  shape carrying the optional `host` field) and `7993`. **No field value was
  edited, and nothing is synthetic: all three entries are verbatim upstream
  data.** The agent-log fixture is the untouched 1,177 B response body.
- **Stripping:** nothing was stripped, because nothing needed stripping. The
  filter `project=guardian-llmprovider-gateway` was chosen precisely so the two
  captured events carry this repository's path and public GitHub issue URLs
  rather than another project's private paths. No credentials, tokens or
  personal paths are present. (Other events in the live store *do* carry
  personal workspace paths, which is why the capture is project-filtered rather
  than "last two events".)
- **Relationship to the live store:** `total: 158` is the captured upstream
  value (events matching the project filter at capture time), not the fixture's
  array length. The drift guard therefore asserts `total` is an integer at least
  as large as `len(events)`, never equality — equality would be a false promise
  about the contract (§3.2.2).
- **Refresh:** re-run the two capture commands in §3.5, apply the same trimming,
  and update this section's date plus the sizes. The drift guard fails when a
  documented path loses its fixture or when a fixture stops matching the
  documented top-level shape; it does *not* care that live data changed, so
  refreshing the fixture is only required when the *shape* changes.

## 6. Open items carried forward

1. **G2-01** implements the vertical slice behind the allowlist in §4 and needs a
   non-default timeout value exercised (per-operation, not one global value).
2. **G1:** reads are unauthenticated (§3.3), so this slice does not exercise the
   auth boundary — recorded as a traded-away criterion, not a defect of the
   target.
3. **Already on the register ([#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38)):**
   `GET :11434/metrics` is unauthenticated on the LAN port, and six entries in
   the MoniFuse port registry have no listener.
4. **`unverified:` list** — whether the write endpoints enforce
   `x-admin-secret` / `x-agent-token` (§3.3); `5xx` and timeout behaviour
   (§3.4.5); redirect (`3xx`) behaviour, none observed (§3.4.5). Each of these
   needs a mutation or a state change, which G0-04 excludes, so they stay open
   until G2-01 can be tested against a disposable instance.
