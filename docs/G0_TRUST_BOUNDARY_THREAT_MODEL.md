# G0-02 — Trust-Boundary and Bypass Threat Model

Baseline required by `docs/ROADMAP.md` §4 G0 and backlog row G0-02
(`docs/ROADMAP.md:334`), reviewed together with the endpoint/policy matrix from
G0-01 ([`docs/G0_ENDPOINT_AUTH_MATRIX.md`](G0_ENDPOINT_AUTH_MATRIX.md)). It is
the reference G1 ("G1-01 authorization/admin isolation", "G1-02
destination/credential enforcement") is checked against, so a reviewer can tell
a new hole from a documented one.

**Scope:** documentation only. No `app/*.py`, config, nginx, systemd or TLS
change is proposed here; every gap is either linked to its owning issue or left
with an explicit `unverified:` note.

**Evidence bar.** Every row names an anchor (`file:line`, config key, nginx
directive) or a command with its observed output. Anchors were verified by
reading the file at `origin/main` (commit `52e55fe`) or the live deployment on
ai-kvm-2 on **2026-10-27 (UTC)** — §8 records what was verified how, and §9
lists what could not be verified. Where a statement is a judgement about
consequence rather than an observed fact, it is marked **[judgement]**.

**Privilege-class caveat carried over from G0-01.** The matrix's privilege
classes (`public` / `client` / `admin` / `operator`) are the *target*
classification, not enforcement: *"The code does not enforce these classes.
Every protected route uses the same flat `verify_api_key`, so one valid key
reaches `admin` and `operator` routes alike … This column is the target
classification for G1-01, not a claim about current enforcement"*
(`docs/G0_ENDPOINT_AUTH_MATRIX.md:28-31`). Nothing in this document claims
otherwise; §4 restates the flat reality and points at the matrix rows.

---

## 1. The deployment as observed

### 1.1 Listener map

`ss -ltn` on ai-kvm-2, 2026-10-27T22:58:24Z:

```text
LISTEN 127.0.0.1:11441          caretaker-llamacpp (python -m caretaker, pid 2032300)
LISTEN 127.0.0.1:11435          guardian (python3.14 -m app.main, pid 188579) — TLS
LISTEN 127.0.0.1:11437          guardian (same pid) — dashboard
LISTEN 127.0.0.1:11436          nginx — plain-HTTP ingress to Guardian
LISTEN 0.0.0.0:11434            nginx — public stream mux
LISTEN 192.168.1.35:11437       nginx — LAN dashboard reverse proxy
```

`:11440` (llama-server) is **not** listening at capture time: `systemctl
is-active llama-server` → `inactive` (backend unloaded). Its bind address is
therefore evidenced from configuration rather than from a live socket:
`--host 127.0.0.1 --port 11440` in both the live args file
(`config/current_model.args`, line 1, gitignored per `.gitignore:37`) and the
script default `scripts/start_llama.sh:40`.

Note the config/code split: `proxy.port: 11434` in
`config/global.settings.yaml:19` is **not** the port Guardian binds. With TLS
configured, the listen address comes from `GUARDIAN_TLS_HOST` /
`GUARDIAN_TLS_PORT` (`app/main.py:329-338`), which the drop-in sets to
`127.0.0.1` / `11435`
(`/etc/systemd/system/guardian-llmprovider-gateway.service.d/20-tls.conf:8-9`).
Giving `proxy.port` a security meaning would be wrong; `:11434` is nginx's.

### 1.2 Hop topology

```text
LAN client ──(:11434 TLS or PLAIN)──► nginx stream mux ──┬─ TLS passthrough ─► Guardian :11435
                                                         └─ plain HTTP :11436 ─► https://127.0.0.1:11435
Guardian ──plain HTTP, no auth──► llama-server :11440        (loopback only)
Guardian ──plain HTTP, Bearer CARETAKER_KEY──► caretaker :11441 (loopback only)
Guardian ──plain HTTP, Bearer management_key──► LAN host 192.168.1.245:11441 / :11440 / :11450 / :11451
Guardian ──HTTPS, Bearer provider key──► OpenRouter / NVIDIA / OpenAI / … (cloud)
Guardian ──plain HTTP, no auth──► dashboard :11437 (loopback, same process)
nginx :192.168.1.35:11437 ──plain HTTP──► dashboard :11437
```

---

## 2. Trust-boundary table

Per hop: what authenticates, what is plaintext, what the hop can reach if it is
compromised, and the anchor that proves the row.

### H1 — LAN client → nginx `:11434` (public ingress)

| Property | Value |
| --- | --- |
| Authenticates | **Nothing.** nginx performs no authentication; it is a TCP/TLS demultiplexer. |
| Plaintext | **Yes, by design and by default path.** The mux maps a connection *without* a TLS ClientHello (`""`) to the plain-HTTP backend, so `http://192.168.1.35:11434` is a supported, unencrypted ingress. Verified live: `curl http://192.168.1.35:11434/healthz` → `200`; `curl https://192.168.1.35:11434/healthz` → `200`. |
| Reachable if compromised | All of Guardian's proxy surface, i.e. all 41 proxy routes in the G0-01 matrix (matrix §"Proxy surface"), because the hop *is* the ingress. |
| Anchor | `deploy/nginx/guardian-llmprovider-gateway-protocol-mux.conf:1-4` (`map $ssl_preread_protocol` … `""` → `guardian_http_backend`), `:20-25` (`listen 0.0.0.0:11434; ssl_preread on;`). Rendered live as `/etc/nginx/stream-conf.d/llama-guardian-protocol-mux.conf:377-399` (`nginx -T`). |
| Perimeter | **No host firewall.** `ufw status` → `Status: inactive`; the only nftables content is Docker's own chains. `0.0.0.0:11434` is therefore reachable from every routed interface, including Docker bridges. **[judgement]** It is not restricted to `192.168.1.0/24` the way the dashboard proxy is. |

**What this means for G1-02.** A client that connects with `http://` sends its
Guardian Bearer key in cleartext across the LAN, and the nginx hop (below)
forwards it verbatim. This is the single largest plaintext exposure in the
deployment and is *not* a bug in nginx — it is the documented dual-protocol
design in `AGENTS.md` ("TLS: nginx stream TLS preread multiplexes both
`http://…:11434` and `https://…:11434`").

### H2 — nginx → Guardian `:11435` (TLS) and `:11436` → `:11435` (plain)

| Property | Value |
| --- | --- |
| Authenticates | Guardian authenticates the **client**, not nginx, via `verify_api_key` (`app/proxy/auth.py:411`). nginx presents no client identity: no mTLS, no `proxy_set_header X-…`-derived identity is consumed for auth. |
| Plaintext — TLS path | **No.** The mux `proxy_pass`es the TLS byte stream unchanged; nginx performs no TLS termination, so it cannot see the request or the `Authorization` header on this path. Anchor: `protocol-mux.conf:16-18,22` (`upstream guardian_https_backend` → `127.0.0.1:11435`, `proxy_pass $guardian_backend`) and the absence of `ssl_certificate` in that server block. |
| Plaintext — plain path | **Yes.** `:11436` receives the full cleartext request (including `Authorization: Bearer …`) and re-originates it as HTTPS to `:11435`. Anchor: `deploy/nginx/guardian-llmprovider-gateway-loopback-http.conf:2,7` (`listen 127.0.0.1:11436` → `proxy_pass https://127.0.0.1:11435`). |
| TLS trust in that re-origination | Verified: `proxy_ssl_verify on`, trusted cert pinned. Anchor: `loopback-http.conf:18-22`. The pinned file (`/home/flip/.config/llama-guardian/tls/guardian-192.168.1.35.crt`) is byte-identical to the unit's `GUARDIAN_TLS_CERTFILE` and to the system trust anchor (`md5sum` → one hash `d0d4230a…618e` for all three). |
| Reachable if compromised | A compromised nginx process sees every plaintext request on the `:11436` path (all client Bearer keys, all prompt bodies) and can rewrite or hold any request on both paths. **[judgement]** That is equivalent to full client impersonation. |
| Anchor (Guardian side) | `app/main.py:320-345` (TLS pair all-or-nothing, TLS host/port), live cert `openssl s_client` → `subject=CN=192.168.1.35, O=Llama-CPP Guardian`, `notAfter=Nov 10 21:07:30 2028 GMT`; key file mode `-rw-------`. |

### H3 — Guardian process (the authorization point)

| Property | Value |
| --- | --- |
| Authenticates | Every protected route: one flat `verify_api_key` dependency (`app/proxy/auth.py:411`), resolved against a single key store `config/guardian.keys.yaml` (first candidate in `app/paths.py:guardian_apikeys_file()`), file mode `-rw-------`. |
| Key sources accepted | `Authorization: Bearer`, `x-api-key`, `api-key` — `app/proxy/auth.py:221-234`. |
| Plaintext | Guardian terminates TLS for `:11435`; nothing leaves the process unencrypted except the loopback/LAN hops below. |
| Reachable if compromised | Everything: cloud provider credentials (`.env`), the caretaker key, all capture data, and — through `sudo systemctl` — backend lifecycle. See H9. |
| Anchor | `app/proxy/auth.py:411`; key store mode `-rw-------` observed on `config/guardian.keys.yaml`. |

### H4 — Guardian → local llama-server `:11440`

| Property | Value |
| --- | --- |
| Authenticates | **Nothing, in both directions.** Guardian sends no credential to the local backend, and `llama-server` was not started with `--api-key` (0 matches for `api-key` in `config/current_model.args`). |
| Why no credential | `app/proxy/providers.py:692-701`: the `Authorization` header is *removed* for `managed` providers — "Managed (local) providers are keyless: llama-server serves `/v1/models` without an upstream api_key". |
| Plaintext | Yes — plain HTTP on loopback (`base_url: http://127.0.0.1:11440/v1`, `config/providers/ai-kvm2-local.settings.yaml:8`; `OLLAMA_URL=http://127.0.0.1:11440` in the unit, and `proxy.target: http://localhost:11440` in `config/global.settings.yaml:20`). |
| Reachable if compromised | **[judgement]** Anything that can open a socket to `127.0.0.1:11440` — any process or user on the host — gets unauthenticated inference and llama-server's own admin endpoints, bypassing Guardian's queue, policy, capture and key model entirely. This is the top bypass (§3 B-01). |
| Anchor | `scripts/start_llama.sh:40` (`--host 127.0.0.1 --port 11440`); live `config/current_model.args` line 1. |

### H5 — Guardian → local caretaker `:11441` (control API)

| Property | Value |
| --- | --- |
| Authenticates | `Authorization: Bearer ${CARETAKER_KEY}` on all three control endpoints. Anchor: `app/gateway/caretaker_client.py:14-17` (`POST /ensure`, `POST /unload`, `GET /status`), key resolution `:449` (env-only, `CARETAKER_KEY`, **no YAML fallback**), verdict confirmed live: `curl http://127.0.0.1:11441/status` without a key → `401`. |
| Plaintext | Yes — plain HTTP on loopback. `management_url: http://127.0.0.1:11441` (`config/providers/ai-kvm2-local.settings.yaml:9`). The client *refuses* to send the key over cleartext to a non-loopback, non-own-host URL: `caretaker_client.py:455-459`. |
| Reachable if compromised | **[judgement]** Lifecycle control of the shared GPU: load/switch/unload any registered model, i.e. denial of service against every Guardian client and VRAM eviction of co-tenant workloads. It cannot read prompt data. |
| Anchor | `caretaker_client.py:449,455-459`; unit `caretaker-llamacpp.service` (`EnvironmentFile=/etc/caretaker/caretaker.env`, `User=flip`). |

### H6 — Guardian → LAN provider host (192.168.1.245)

| Property | Value |
| --- | --- |
| Authenticates | Provider-side Bearer only, and over **cleartext HTTP**: `api_key: ${WINDOWS_LAN_KEY}` for inference (`config/providers/14700k-local.settings.yaml:12`), `management_key: ${WINDOWS_LAN_KEY}` for the caretaker (`:22`). The header is attached at `app/proxy/providers.py:693-694` and is *not* stripped, because the provider declares `local: false` (`14700k-local.settings.yaml:9`) so `managed` resolves `False` (`app/proxy/providers.py:289-295`). |
| Plaintext | Yes, all four hops: inference `:11440`, caretaker `:11441`, TTS `:11450`, STT `:11451` — every one `http://` (`14700k-local.settings.yaml:11,21,24,26`). |
| Reachable if compromised | **[judgement]** Full use of the LAN GPU host and its LlamaIndex/caretaker control surface for anyone who reads the key off the wire or off any of the participating hosts. |
| Verified behaviour | `curl http://192.168.1.245:11441/status` (no key) → `401` — the remote caretaker does enforce its key. `curl http://192.168.1.245:11440/v1/models` → connection timed out (`000`), i.e. that host's llama-server was not running at capture time. `curl http://192.168.1.245:11450/` → `404` (an HTTP listener answers **without** authentication on an unrouted path). |
| Anchor | `app/cloud_inference/forwarding.py:199-212` documents this practice in-tree: "including the provider-file `management_key` over LAN http (firewall-restricted LAN, established F6 practice…)". The Bearer is actually attached at `forwarding.py:277-284` (`POST {mgmt_url}/ensure`). |

### H7 — Guardian → cloud providers

| Property | Value |
| --- | --- |
| Authenticates | Provider API key from `.env` (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `NVIDIA_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `POOLSIDE_API_KEY`, `GITHUB_API_KEY` — names only, values never read). Header: `app/proxy/providers.py:693-694`. |
| Plaintext | No — HTTPS base URLs; `httpx.AsyncClient` TLS verification is left at its default (on): no `verify=False` anywhere under `app/cloud_inference/` or `app/gateway/` (grep). |
| Reachable if compromised | **[judgement]** The provider credential *is* the trust boundary: Guardian is the only holder, so a Guardian compromise bills the operator's cloud accounts and can reach any destination those credentials allow — the reason G1-02 asks for destination allowlists and outbound credential isolation (`docs/ROADMAP.md:219-225`). |
| Anchor | `app/proxy/providers.py:693-694`; `app/cloud_inference/forwarding.py:857` (`httpx.AsyncClient(timeout=timeout)`, no `verify=` override). |

### H8 — Dashboard `:11437` (loopback app + LAN nginx proxy)

| Property | Value |
| --- | --- |
| Authenticates | Static shell: **nothing** (by design). `/api/*`: the same flat `verify_api_key` (`app/main.py:85` and the 16 dashboard rows of the matrix §"Dashboard surface"). |
| Bind | App: `host="127.0.0.1"`, `app/main.py:381` (hardcoded, not configurable). LAN: nginx `listen 192.168.1.35:11437` with `allow 192.168.1.0/24; deny all;` (`deploy/nginx/guardian-llmprovider-gateway-dashboard.conf:18,23-24`; rendered `/etc/nginx/sites-enabled/llama-guardian-dashboard.conf:18,23-24`). |
| Plaintext | Yes, end to end — LAN HTTP to nginx, then plain HTTP to loopback. A dashboard Bearer key is therefore cleartext on the LAN on every call. |
| Reachable if compromised | **[judgement]** The dashboard surface duplicates the proxy's admin surface (keys, catalog refresh, benchmark) with no additional role check, so a dashboard key is worth as much as a proxy key. |
| Verified behaviour | `curl http://127.0.0.1:11437/` (no key) → `200` (public shell); `curl http://127.0.0.1:11437/api/stats` (no key) → `401`. |
| Anchor | `app/main.py:381`; nginx dashboard conf lines above; the in-file security note at dashboard conf `:9-12` ("the static shell is public and the app has no rate limiting on auth failures"). |

### H9 — Host control plane (systemd / sudo) and data at rest

| Property | Value |
| --- | --- |
| Authenticates | Unix DAC only. Guardian runs as `flip`; the unit sets `UMask=0077` (`20-tls.conf:2`). |
| Backend control | Guardian still executes `sudo systemctl stop` / `start llama-server` directly (`app/engine/manager.py:1402,1410`) — the F5 "caretaker owns execution" split is **partial**, kept as a fallback (`app/proxy/server.py:1314-1321`). **[judgement]** Anyone who can run commands as `flip` owns the backend regardless of the caretaker. |
| Data at rest | `data/capture/` is `drwxr-x---` (0750) with the active WAL `data/capture/guardian_capture_current.jsonl` at `-rw-r-----` (0640), plain JSONL, 87 MB at capture time. Configured `file_mode: '0640'`, `directory_mode: '0750'`, `retention_days: -1`, `max_capture_bytes: -1` (`config/global.settings.yaml:161-167`). Media subdirectory `data/capture/media/` is **0755** and 305 of its files are `-rw-r--r--` — see §3 B-06. |
| Capture content | Raw request/response bodies including system prompts, reasoning and tool results; Authorization headers and API keys are never persisted (`app/capture/redactor.py:574-575`, `:261-274`; `app/capture/__init__.py:11`). Records carry an HMAC-SHA-256 `record_auth` when `GUARDIAN_CAPTURE_RECORD_AUTH_SECRET` is set (`app/capture/schema.py:208-231`) — integrity, **not** confidentiality. |
| Anchor | `config/global.settings.yaml:127-167`; observed `ls -ld`; `app/capture/schema.py:208-231`. |

---

## 3. Bypass inventory

"Bypass" = a way to reach a backend, or to act on Guardian's behalf, without
passing Guardian's authorization for that operation. Class is `approved`
(documented design) or `gap` (needs G1 work; tracker named).

| ID | Path | Bypasses | Class | Tracker |
| --- | --- | --- | --- | --- |
| B-01 | Direct `127.0.0.1:11440` (llama-server) | Guardian auth, queue, policy, capture | `approved` (loopback-only backend, no upstream key) — **but** see B-01 note | ROADMAP §G1 exit gate ("backend access is restricted from an ordinary client vantage", `docs/ROADMAP.md:237`) |
| B-02 | Caretaker control API `:11441` with `CARETAKER_KEY` | Guardian's operator routes (`/admin/load`, `/admin/unload`, lifecycle) | `approved` destination, `gap` for identity | ROADMAP §5 row G1-01 (`docs/ROADMAP.md:337`) → gap register [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-03 | LAN provider `http://192.168.1.245:11440` with `WINDOWS_LAN_KEY` | Guardian entirely (auth, queue, capture, cloud policy) | `gap` (documented F6 practice, cleartext) | G1-02 → [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-04 | LAN management hops `:11441` `/ensure`, `/tts/ensure` and speech `:11450`/`:11451` with a Bearer over HTTP | Guardian's credential-isolation goal; the key is cleartext on the LAN | `gap` (named in `docs/ROADMAP.md:221-225`) | G1-02 → [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-05 | Plain `http://…:11434` ingress (H1) | Transport confidentiality of *every* client Bearer key | `approved` design, `gap` for G1-02 | G1-02 → [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-06 | Capture/WAL data path on disk | Guardian auth (any `flip`-owned process, or any reader of the 0755 media dir) | `gap` | [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-07 | `sudo systemctl stop` / `start llama-server` from Guardian | Caretaker ownership (F5 split) | `approved` transition fallback | `docs/ROADMAP.md:125` — "Backend lifecycle delegation … Caretaker owns backend execution, Guardian owns traffic policy" |
| B-08 | Key minting `/api/keys` with **any** valid key | the admin/operator distinction; mints a key whose `cloud_gateway_access` is `True` | `gap` (verified gap in `docs/ROADMAP.md:215-218`) | G1-01 → [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-09 | Dashboard LAN proxy `192.168.1.35:11437` | nothing (same flat key), but doubles the reachable admin surface | `approved` (LAN-perimeter + Bearer), `gap` for role separation | G1-01 → [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38) |
| B-10 | `GET /metrics`, `GET /healthz`, dashboard `/` and `/static` | authentication (public by design) | `approved` | matrix rows proxy 8/27, dashboard 5-7 |
| B-11 | Default listen `0.0.0.0` when the TLS pair is absent | loopback confinement of the API | `approved` default, documented risk | `app/main.py:326-327` |

### B-01 — Direct `:11440` reachability

llama-server binds `127.0.0.1` only (`scripts/start_llama.sh:40`; live args file
line 1), so it is **not** reachable from the LAN. It is reachable from every
local process, and from any container sharing the host network namespace.
Guardian sends it no credential and the backend requires none
(`app/proxy/providers.py:692-701`, no `--api-key` in the args file).

- `approved`: a loopback-only, keyless backend is the documented design for the
  single-host deployment.
- Residual risk **[judgement]**: the backend is an unauthenticated inference
  endpoint for any local user. It is also the path the G1 exit gate names: a
  client AWS-style vantage must not reach it. `unverified:` whether any
  container on this host currently reaches `:11440` via host networking — that
  needs a container run, which this session did not perform.

### B-02 — Caretaker control API with `CARETAKER_KEY`

Three endpoints, one static shared secret, no per-caller identity and no
audit tie to a Guardian client id (`app/gateway/caretaker_client.py:14-17,449`).
Verified enforced (`401` without a key, live). It is a genuine bypass of
Guardian's *operator* routes: whoever holds the key loads and unloads models
without any Guardian key. `approved` as a destination (loopback, key-gated);
`gap` as an identity model — it is one more flat credential, which is exactly
what G1-01 exists to fix.

### B-03 / B-04 — LAN hops that carry Bearer keys over HTTP

Four cleartext hops to `192.168.1.245` (inference `:11440`, caretaker `:11441`,
TTS `:11450`, STT `:11451`; `config/providers/14700k-local.settings.yaml:11,21,
24,26`) plus the same key attached to the LAN caretaker's `/ensure`
(`app/cloud_inference/forwarding.py:277-284`). The speech path repeats it twice —
once for the lifecycle call and once for the workload:

- `app/gateway/tts.py:376-379` — `Authorization: Bearer {key}` to
  `{management_url}/tts/ensure`
- `app/gateway/tts.py:272-274` — `Authorization: Bearer {route['api_key']}` to
  `{base_url}/audio/speech`
- `app/gateway/stt.py:197-206` — same for transcriptions
- route construction, including the `management_key` ← `api_key` fallback:
  `app/gateway/speech_routing.py:93-103`

The in-tree comment names the compensating control precisely:
"firewall-restricted LAN, established F6 practice" (`forwarding.py:208-212`).
`docs/ROADMAP.md:221-225` rules that a LAN firewall restriction is not
encryption and that these hops must migrate to verified TLS or another reviewed
encrypted transport — that is G1-02 work, not a fix here. Note also that
`ufw` is inactive (§H1), so even the claimed restriction is not currently
enforced on this host. `unverified:` whether a switch/router ACL supplies it.

### B-05 — Plain HTTP ingress

`http://…:11434` is a first-class path (H1). A LAN sniffer, or any switch port
in between, sees client Bearer keys in cleartext. `approved` as dual-protocol
design; `gap` against the G1-02 goal that credentials not travel in the clear.

### B-06 — Capture / WAL data path

The WAL is unencrypted, retains forever, and holds full prompt/response bodies
(`config/global.settings.yaml:129-134,161-164`). Keys are excluded
(`app/capture/redactor.py:574-575`), and records are HMAC-signed
(`app/capture/schema.py:208-231`), so this is a confidentiality boundary, not a
credential-exposure one.

Observed permission drift: the writer creates the media directory and files with
no explicit mode — `target.parent.mkdir(parents=True, exist_ok=True)` and
`target.write_bytes(raw)` (`app/capture/media.py:68-74`) — so the effective mode
is the process umask. With `UMask=0077` (`20-tls.conf:2`) new files land `0600`
(the majority: 5054 files), but 305 files are `-rw-r--r--` and the
`data/capture/media/` directory itself is `drwxr-xr-x`. Prior to that umask, or
from a differently-umasked caller, capture media was group/world-readable.
**[judgement]** Any process that can read `data/capture/media/` reads user
images verbatim. This is not a Guardian-auth bypass (B-01 style) but it is a
data-path gap and is registered in [#38](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/38).

### B-07 — Guardian keeps direct backend lifecycle control

`app/engine/manager.py:1402,1410` (`sudo systemctl stop` / `start llama-server`) and
the fallback at `app/proxy/server.py:1314-1321` ("falling back to local unload")
mean the F5 split is a delegation, not an exclusive ownership move. `approved`:
both paths are documented as the transition fallback and the local stop is
idempotent. **[judgement]** Practically, this means two independent lifecycle
owners can act on one backend; G1 does not have to change it, but a future
review should not mistake it for a single-owner design.

### B-08 — Key minting is reachable with any valid key

`POST /api/keys` (`app/proxy/server.py:1395`) delegates to
`app/gateway/admin_api.py:113-129`, which validates only that `name` is
non-empty and calls `_generate_api_key`. `generate_api_key` writes
`"cloud_gateway_access": True` (`app/proxy/auth.py:405`). Consequences,
verified by reading the code:

1. Any valid key mints further keys — the verified gap already recorded in
   `docs/ROADMAP.md:215-218`.
2. **Escalation path:** a client whose own key has `cloud_gateway_access: false`
   can mint a key that has it `true`. `cloud_gateway_access` is the only per-key
   authorization bit that is actually honoured (§4), so this is a real
   privilege escalation, not just a missing role check. It belongs to G1-01.
3. `GET /api/keys` is safer than it looks: it returns fingerprints only, never
   raw keys (`app/gateway/admin_api.py:97-110`).

### B-09 / B-10 / B-11 — Lower-severity entries

- **B-09:** the LAN dashboard proxy adds a second reachable admin surface with
  the same flat key and a LAN CIDR perimeter (`guardian-…-dashboard.conf:23-24`).
  The static shell is intentionally public.
- **B-10:** `GET /healthz` and `GET /metrics` are unauthenticated (matrix proxy
  rows 8 and 27); verified live (`200` for both without a key). `/metrics`
  discloses the loaded model name and GPU/VRAM figures
  (`guardian_model_current_info{name="qwen3.8-27b-uncensored-ymq",…}`,
  `guardian_vram_used_mb`). **[judgement]** Minor information disclosure; keep
  it public for scrapers unless G1 decides otherwise.
- **B-11:** without the TLS pair, Guardian listens on `0.0.0.0:11434` by default
  (`app/main.py:326-327`). A mis-rendered drop-in would silently expose the API
  on the LAN without the nginx hop. `approved` default, worth a G1 config test.

---

## 4. Identity and role model as-is

### 4.1 Flat authentication, one attribute

There is **one** authentication mechanism for every protected route of both
apps: the `verify_api_key` dependency (`app/proxy/auth.py:411`). It appears on
45 dependency lines in `app/` — 36 in `app/proxy/server.py`, 9 in
`app/main.py` (`grep -rn 'Depends(verify_api_key)' app/`) — which is an
occurrence count, not a route count: the matrix's 57 rows are the route
authority, and the routes it lists as `none` are the ones without the
dependency. `verify_api_key` returns the key's `name` as `client_id` and puts
the whole key entry on `request.state`.

A key entry is normalised to exactly four fields — `name`, `created_at`,
`metadata`, `cloud_gateway_access` (`app/proxy/auth.py:_normalize_key_entry`,
`:318-336`). Only one of them is consulted for authorization:

| Field | Used for | Anchor |
| --- | --- | --- |
| `name` | identity string in logs/queue ownership | `app/proxy/auth.py:411` return value |
| `created_at` / `metadata` | presentation only | `app/gateway/admin_api.py:97-110` |
| `cloud_gateway_access` | **the only authorization bit**: `false` hides cloud models from discovery and rejects cloud routing | `app/gateway/model_discovery.py:111`, `app/cloud_inference/routing.py:100`, `app/gateway/model_endpoints.py:825-829` |

There is no scope, no role, no admin/operator distinction, and no per-destination
allowlist. Per privilege class: matrix rows 13, 14, 21 (`operator`), 15-20,
22-26, 28-31, 39-41 (`admin`) and 5-12, 32-38 (`client`) are **all** reachable
with the same key; the matrix carries the identical caveat
(`docs/G0_ENDPOINT_AUTH_MATRIX.md:28-31`) and this document does not restate the
rows — link to them.

### 4.2 What each client class can actually reach

| Client class | Can reach | Cannot reach |
| --- | --- | --- |
| **Unauthenticated** (matrix `public` rows) | proxy `GET /healthz`, `GET /metrics`, the four FastAPI docs routes; dashboard `/`, `/static`, `/favicon.ico`, its docs routes | everything else → `401` (verified live for `/api/status` and `/api/stats`) |
| **Any valid key** ("client" in intent) | all 41 proxy routes and all 16 dashboard routes except the `public` ones: inference (local *and* cloud, subject to `cloud_gateway_access`), model discovery, queue status/cancel, `/admin/load`, `/admin/unload`, `/api/config/reload`, `/api/keys` (mint), `/api/capture/*`, `/api/scaler/*`, `/api/session/*`, `/api/crashes`, `/api/status` | nothing on Guardian's surface is held back |
| **Key with `cloud_gateway_access: false`** | local inference and every local/admin/operator route | cloud models: hidden from `/v1/models` and rejected on routing (`routing.py:100`) — **unless** it mints a new key (B-08) |
| **Holder of `CARETAKER_KEY`** | caretaker `/ensure`, `/unload`, `/status` (`:11441`) | prompt data |
| **Host-local process** | `:11440` unauthenticated inference; `:11441` gated by the caretaker key; `.env`, `config/guardian.keys.yaml`, the capture WAL | — |

### 4.3 Where a compromise escalates

1. **Any client key → admin.** `/api/keys` mints new keys, `/api/config/reload`
   re-reads provider/capture config, `/api/capture/rotate` rotates data files,
   `/api/session/*` reads and writes shared state. Anchors: matrix rows 15/16,
   21, 25/26, 39-41.
2. **Any client key → operator.** `/admin/load` and `/admin/unload` change the
   single GPU backend for every tenant (`app/proxy/server.py:1286,1380`);
   `/api/config/reload` is `operator` in the matrix. This contradicts no
   enforcement claim — it is the flat reality the matrix documents.
3. **Client key → cloud spend.** With `cloud_gateway_access` true (the default,
   and mintable — B-08) a client reaches every cloud model the operator has
   credentials for.
4. **Unauthenticated LAN host → local backend.** `:11440` needs no key (B-01).
5. **nginx/host compromise → everything.** Plaintext on the `:11436` path
   (H2), `.env` and the key store readable as `flip` (H9).

---

## 5. Failure modes: fail-open vs fail-closed

Direction is relative to *security*: "closed" = the failure denies or narrows
access; "open" = the failure continues serving or widens access.

| # | Failure | Direction | Security-relevant | Anchor |
| --- | --- | --- | --- | --- |
| F-01 | Missing/invalid Bearer on a protected route | **closed** → `401` | yes (correct) | `app/proxy/auth.py:411-470`; verified live (`/api/status` → 401) |
| F-02 | Key store unreadable/corrupt YAML | **closed** → `{}` → every request `401` | yes (correct: denies all rather than allowing) | `app/proxy/auth.py:358-370` (`except Exception: raw = {}`) |
| F-03 | Key entry missing `cloud_gateway_access` | **open** → defaults `True` | **yes** — an old/partial key entry silently gets cloud access | `app/proxy/auth.py:332` (`setdefault`), `model_discovery.py:111`, `routing.py:100` (`get(..., True)`) |
| F-04 | Failed config reload (parse error) | **static** → previous config stays fully in place | yes — a stale config keeps its **old** permissions; a reload cannot be used to tighten access reactively | `app/config_loader.py:164-190` ("On any parse error the previous configuration stays fully intact") |
| F-05 | Caretaker unreachable on `/admin/unload` | **open** → falls back to local `systemctl stop` | yes (lifecycle action still executes; both owners act) | `app/proxy/server.py:1314-1321`, `:1332-1345`; `app/engine/manager.py:1402` |
| F-06 | Caretaker `/ensure` unreachable / times out | **closed** → request fails `503` (after a bounded re-probe) | yes (correct: no silent wrong-model serve) | `app/gateway/caretaker_runtime.py:27,632-640` ("Callers MUST fail the request closed (503 …)") |
| F-07 | Capture dispatch error | **open** → the inference request proceeds uncaptured | **yes** — the audit/completeness boundary silently loses a record | `app/gateway/capture_dispatch.py:1-6` ("They are all fail-open: any exception is swallowed") |
| F-08 | Cloud catalog / metadata fetch failure | **open** → discovery degrades, models keep serving | no for authorization (access is decided in routing, not discovery); availability only | `app/gateway/model_discovery.py:232`, `app/gateway/model_endpoints.py:227,278`, `app/gateway/metadata_enrichment.py:14` |
| F-09 | Provider catalog hides models from a key without cloud access | **closed** by routing | yes (correct) | `app/cloud_inference/routing.py:100` |
| F-10 | CORS policy | **open** → `allow_origins=["*"]` **with** `allow_credentials=True` | **yes** — any web origin a browser on the LAN visits may script the proxy; the Bearer requirement limits, but does not eliminate, the exposure | `app/proxy/server.py:781-788` |
| F-11 | nginx upstream down | **closed** → 502/connection reset | no | `protocol-mux.conf:20-25` |
| F-12 | Backend crash/OOM mid-session | **silently open** — Guardian learns only on the next `/ensure` | **yes** — see §6 and issue #34 | `app/gateway/caretaker_client.py:14-17`; issue [#34](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/34) |
| F-13 | Dashboard static shell | **open by design** | no (shell only; `/api/*` behind the key) | matrix dashboard rows 5-7; live `/` → `200` |

Security-relevant open failures to carry into G1: **F-03, F-07, F-10**, plus the
`static` (F-04) behaviour interacting with F-03 — a reload that fails after an
operator narrowed a key leaves the *narrowed* file unread until the next
successful reload, so the effective policy is "last successfully loaded".

---

## 6. Outstanding handoffs as inputs (not fixed here)

These two are **inputs** to this model: they are the known blind spots in what
Guardian can observe and guarantee. They are recorded here so G1 does not build
on an assumed-complete picture. Neither is addressed by this document.

### 6.1 Issue #34 — a backend crash/OOM during a live session is invisible

Guardian learns about a dead backend only through a `503 model_load_failed` on
the next `/ensure`. Verified on the Guardian side in this repository:
`caretaker_client.status()` has exactly one call site — the `already_unloaded`
check in `/admin/unload` (`app/proxy/server.py:1301`) — and an OOM-signal scan
finds one comment and no handling at all
(`grep -rnE '\boom\b|OOM' app/gateway/ app/proxy/` → `app/proxy/queue.py:142`
only). Tracked in
[#34](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/34). The
root causes are stated there to live in `m0nklabs/caretaker-llamacpp` (watchdog
never started, OOM reporting undecided); that repository is not in this
workspace and was not read here, so those two statements are `unverified:` in
this document and quoted from the issue.

**Why it matters for this model:** H5/H6 show the caretaker as the lifecycle
authority, and F-12 shows the failure is silent. Any G1 claim of the form "a
compromised or crashed backend is detected" would be unsupported until #34
lands. **[judgement]** A backend that dies without a report is
indistinguishable from one that was never loaded.

### 6.2 caretaker-llamacpp #17 — `vram_limit_mb: .inf` aborts the daemon at boot

Reported there: `_yaml_vram_limit_mb()` converts the YAML value with `int(raw)`
inside `except (TypeError, ValueError)`, so a non-finite float raises
`OverflowError` in the constructor and aborts the daemon at boot (same class as
the fixed `health_polls` knob). Tracked in
[m0nklabs/caretaker-llamacpp#17](https://github.com/m0nklabs/caretaker-llamacpp/issues/17);
it is a different repository, so it is a handoff, not a fix here.
`unverified:` the function name and the exception tuple are quoted from that
issue — `m0nklabs/caretaker-llamacpp` is not in this workspace and its code was
not read in this session. The failure *class* is verifiable independently in
Python: `int(float("inf"))` raises `OverflowError`.

**Why it matters for this model:** F-05/F-06 assume the caretaker is either up
or cleanly down. A daemon that cannot boot fails *before* serving, so Guardian
falls into the F-05 local-fallback path with no lifecycle owner — the
double-owner state in B-07 becomes the only state.

---

## 7. What G1 has to prove against this baseline

Short pointers, not new requirements (G1-01/G1-02 own the work; see
`docs/ROADMAP.md:212-238`):

- B-02 and B-08: a `client` key must stop reaching `admin`/`operator` routes,
  and key minting must stop granting `cloud_gateway_access` the caller does not
  hold.
- B-03/B-04/B-05: LAN Bearer keys must leave cleartext HTTP; a firewall is not
  the control.
- B-01: backend `:11440` must be demonstrated unreachable from an ordinary
  client vantage (roadmap exit gate wording).
- F-03/F-07/F-10: the remaining fail-open defaults need a decision, not
  silence.

---

## 8. Verification log

What was verified, and how. All reads are against `origin/main` @ `52e55fe`;
all live observations are from ai-kvm-2 on 2026-10-27 between 22:53 and 23:00
UTC. No daemon was POSTed to, nothing was restarted, no config was changed.

| Anchor | Method |
| --- | --- |
| nginx mux, loopback-http, dashboard blocks (repo + rendered) | `cat -n deploy/nginx/*.conf`; `nginx -T` (rendered config dump); direct read of `/etc/nginx/sites-enabled/*` |
| Listener map, `:11440` absent, `0.0.0.0:11434` | `ss -ltn` (+ `-p` for owner pids) |
| TLS host/port, cert/key/USMask | `systemctl cat` + drop-in read; `/proc/188579/environ`; `openssl s_client`; `ls -l`; three-way `md5sum` of the cert |
| Guardian bind logic, dashboard loopback, TLS pair | `app/main.py:320-345,381` read |
| `verify_api_key`, key sources, store, fail-closed load, `cloud_gateway_access` default | `app/proxy/auth.py` read (221-234, 323-336, 358-370, 405, 411) |
| Key minting / listing | `app/gateway/admin_api.py:97-129`, `app/proxy/server.py:1389-1399` |
| llama-server bind + keyless backend | live `config/current_model.args`; `scripts/start_llama.sh:40`; `app/proxy/providers.py:692-701`; `grep -c api-key` → 0 |
| Caretaker contracts + cleartext refusal + env-only key | `app/gateway/caretaker_client.py` read; live `curl :11441/status` → 401 |
| LAN hops and their Bearer headers | `config/providers/14700k-local.settings.yaml` read; `app/cloud_inference/forwarding.py:199-212,277-284`; `app/gateway/tts.py:272-274,374-379`; `app/gateway/stt.py:197-206`; `app/gateway/speech_routing.py:93-103`; live `curl 192.168.1.245:11441/status` → 401, `:11440` → timeout, `:11450/` → 404 |
| `managed` derivation (why LAN provider keeps its Bearer) | `app/proxy/providers.py:275-295` read |
| CORS | `app/proxy/server.py:781-788` read |
| Capture config, retention, modes, WAL/media permissions | `git show origin/main:config/global.settings.yaml` (127-167); `ls -ld`/`ls -l` on `data/capture`; `awk` mode histogram over `data/capture/media` |
| Capture excludes credentials; HMAC integrity | `app/capture/redactor.py:261-274,574-575`; `app/capture/__init__.py:11`; `app/capture/schema.py:208-231`; `app/capture/media.py:68-74` |
| Config reload fail-static | `app/config_loader.py:164-190` read; docs string quoted |
| Fail-open/closed inventory | a plain-text scan of `app/` for both spellings and per-site reads |
| Public surfaces live | `curl -o /dev/null -w '%{http_code}'` against `/healthz`, `/metrics`, `/api/status`, dashboard `/`, dashboard `/api/stats` |
| Handoffs | `gh api` for issue #34 and `m0nklabs/caretaker-llamacpp` issue #17 (bodies quoted); the Guardian-side half of #34 re-verified here: `grep -rnE '\boom\b|OOM' app/gateway/ app/proxy/` → one comment only; `int(float("inf"))` → `OverflowError` |
| No host firewall | `ufw status` → inactive; `nft list ruleset` → Docker chains only |
| Docs-only test impact | `pytest tests/unit -q` → **1863 passed in 56.74s** |

---

## 9. Unverified / judgement items

Everything below is either a judgement **[judgement]** already marked inline or
something this session could not check. Nothing here is stated as fact above.

1. `unverified:` **Firewall or ACL between ai-kvm-2 and 192.168.1.245.** `ufw`
   is inactive on this host; whether a switch/router ACL restricts the LAN hops
   (B-04) was not tested. The roadmap already treats "firewall-restricted" as an
   insufficient control regardless.
2. `unverified:` **Whether any container reaches `:11440`.** The bind is
   loopback-only, so only host-netns processes can; no container was run to
   confirm or deny.
3. `unverified:` **Which path produced the 305 world-readable media files.**
   The writer sets no mode (`app/capture/media.py:71-72`); the current
   `UMask=0077` yields `0600` for new files. The age/umask at creation time of
   those 305 files was not reconstructed — the *mechanism* is verified, the
   *history* is not.
4. `unverified:` **`windows-gpu-local` is a placeholder.** The committed
   provider file carries `http://192.168.1.W:{11440,11441,11450,11451}`
   (`config/providers/windows-gpu-local.settings.yaml:11,18,21,23`) with
   `enabled: true`. Whether a host named/aliased `192.168.1.W` exists (it is not
   a valid IPv4 literal) was not resolved; if the config is inert it is dead
   weight, if it resolves it is a second LAN bypass identical to B-03.
5. `unverified:` **Authenticated behaviour of the speech endpoints on
   `192.168.1.245`.** `:11450/` answered `404` without credentials, but the real
   endpoints (`/tts/ensure`, `/audio/speech`) were not called: that would be a
   POST to a live daemon, which this task forbids.
6. **[judgement]** All "reachable if this hop is compromised" statements are
   consequences reasoned from the verified configuration, not observed attacks.
7. **[judgement]** Severity statements in §3/§4 (e.g. "top bypass",
   "equivalent to full client impersonation") are assessments, not measurements.
8. `unverified:` **Behaviour of the public API under a LAN MITM.** The cleartext
   `http://:11434` path (B-05) is verified as *accepted*; no capture of a real
   client's key on the wire was performed.
9. `unverified:` **Caretaker-repository claims.** `m0nklabs/caretaker-llamacpp`
   is not in this workspace, so the `_yaml_vram_limit_mb()` / `OverflowError`
   detail (§6.2) and the "watchdog never started" / "OOM reporting undecided"
   statements (§6.1) are quoted from caretaker #17 and guardian #34, not read
   here. The failure class itself is verified independently:
   `int(float("inf"))` → `OverflowError: cannot convert float infinity to
   integer`.
10. `unverified:` **Exact route count behind the dependency count.** The 45
    `Depends(verify_api_key)` lines are counted directly; reconciling them
    one-to-one with the matrix's protected rows was not done (§4.1 treats the
    matrix as the route authority, which its guard
    `tests/unit/test_g0_endpoint_matrix.py` enforces).

## 10. Deliberately out of scope

- Duplicating the endpoint inventory — the G0-01 matrix is the inventory and is
  linked, not copied (`docs/G0_ENDPOINT_AUTH_MATRIX.md`).
- Fixing any gap: B-01…B-11 are registered, and the enforcement work belongs to
  G1-01/G1-02 (`docs/ROADMAP.md:212-238`).
- Any nginx, systemd, TLS-material or `app/` change.
- Cross-repository fixes for the caretaker (issue #34's root causes,
  caretaker #17) — recorded as inputs only (§6).
