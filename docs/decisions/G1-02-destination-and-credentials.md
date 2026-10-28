# G1-02 — Destination allowlist, credential isolation and SSRF resistance

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |
| **Issue** | [#56](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/56) |
| **Roadmap** | `docs/ROADMAP.md` §G1 row G1-02 |
| **Baseline** | `docs/G0_TRUST_BOUNDARY_THREAT_MODEL.md` B-03/B-04/B-05/B-07, §9 items 1/5/8; `docs/G0_BASELINE.md` §4 |
| **Follows** | [`G1-01-privilege-scopes.md`](G1-01-privilege-scopes.md) (same enforcement shape: one declaration, one enforcement point, typed refusal, fail closed) |
| **Operator decision** | Enforce a **configured allowlist** for every outbound destination, fail closed, and keep the four `192.168.1.245` cleartext hops **approved but flagged** until the Windows host serves TLS. The migration itself is a tracked blocker ([#59](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/59)), not a silent omission. |

## 1. Context

G1-01 fixed *who* may call *which operation*. This record fixes *where* Guardian
may send traffic and *what* it may send with it. The G0 baseline recorded the
gap concretely:

- **No provider URL is validated.** `base_url`, `management_url`, `tts_url`,
  `stt_url` and `catalog_url` are free-form YAML. A typo, a copy-pasted
  placeholder (`192.168.1.W`, the #48 class) or a catalog path that redirects
  decides where Guardian connects and where it attaches a Bearer key.
- **B-03/B-04 (four cleartext credential hops).** `192.168.1.245` `:11440`
  inference, `:11441` caretaker, `:11450` TTS and `:11451` STT all carry
  `${WINDOWS_LAN_KEY}` over `http://`. The in-tree justification
  ("firewall-restricted LAN, established F6 practice") is invalid: `ufw` is
  inactive on this host (G0-03 triage, `docs/G0_BASELINE.md` §4) and no switch
  ACL is observable. A LAN firewall is not encryption.
- **B-05 (plaintext ingress)** stays an approved dual-protocol design; G1-02
  owns *egress*, G1-03 owns admission.
- **B-07 (Guardian keeps `sudo systemctl`)** is unaffected by this record: it is
  a local lifecycle call, not an outbound destination.
- **Hostname checks were the only defence proposed so far** — which cannot
  survive DNS rebinding, and cannot survive a redirect that leaves the approved
  set.

The threat this closes: an SSRF-shaped bug (or a compromised upstream answer)
turns "Guardian calls a URL from configuration" into "Guardian fetches
`169.254.169.254` / an internal admin port and attaches a provider key to it".

## 2. Decision

1. **One policy, one enforcement point.** Every outbound HTTP request Guardian
   makes goes through `app/destination_policy.py:guarded_async_client`, which
   builds an `httpx.AsyncClient` whose transport is
   `DestinationGuardTransport`. The guard lives *below* the call sites, so
   redirects, retries and any future dynamic URL are all validated by the same
   code. All 24 outbound client constructions in `app/` were converted; the two
   in-process test doubles (`httpx.MockTransport`) bypass the guard by design
   because they never open a socket.
2. **The allowlist is configuration, and it is the only approval.** The source
   of truth is `destination_policy` in `config/global.settings.yaml`
   (documented in `docs/CONFIG_SCHEMA.md` §11). An outbound URL is approved only
   when its `(scheme, host, port)` matches an explicit entry. There are no
   wildcards and no host-class shortcuts: subdomain patterns and "private
   networks are fine" rules are exactly what an SSRF exploits.
3. **Host classes decide *how* a target must be approved, never whether.**
   - `169.254.169.254`, `169.254.169.253`, `169.254.170.2`, `100.100.100.200`
     and `fd00:ec2::254` (cloud metadata) are **never approvable** — an entry for
     them is ignored and every request is refused with
     `metadata_host_blocked`.
   - loopback, link-local and RFC1918 targets are usable **only** through an
     explicit entry, and a refusal names the class
     (`loopback_not_approved`, `link_local_not_approved`,
     `private_network_not_approved`) so the operator sees why.
   - public hosts must be listed by name (`host_not_allowed`).
4. **Authorization survives resolution (DNS rebinding).** The guard resolves the
   host again at request time, validates **every** returned address, and then
   connects to the validated address itself, preserving `Host` and TLS SNI
   (`sni_hostname` extension), with sequential fallback across the validated
   addresses. Two entry shapes exist:
   - **IP literal** (`192.168.1.245`): the literal is the approval; nothing is
     resolved, nothing can be re-pointed.
   - **Name without `addresses`**: every answer must be publicly routable. A
     provider name that suddenly answers `192.168.1.245` or `169.254.169.254`
     is refused (`dns_address_not_allowed` / `metadata_host_blocked`).
   - **Name with `addresses`** (pinned): every answer must be inside the pin, so
     an internal hostname keeps working and a changed answer is refused.
   Resolution failure is a refusal (`dns_resolution_failed`), never a fallback to
   connecting by name.
5. **Redirects are refused, not followed.** `guarded_async_client` rejects
   `follow_redirects=True` outright, and the transport refuses a hop whose
   origin differs from the previous destination
   (`redirect_not_allowed`). A `3xx` is returned to the caller untouched. This
   is the stronger of the two options the issue allows: a redirect cannot leave
   the approved set *and* cannot carry a credential to a new destination. The
   per-hop validation stays in place as belt-and-braces for any future caller
   that injects its own transport.
6. **Credentials are bound to their own destination.** A request that carries
   `authorization`/`x-api-key`/`api-key`/`x-goog-api-key` is refused unless the
   client was created for an owning provider (`credential_owner`) **and** the
   destination is one of *that provider's* configured destinations
   (`credential_foreign_destination`). A credentialed request with no owner fails
   closed (`credential_without_owner`), so a future call site that forgets the
   owner cannot silently widen the rule. No credential is forwarded on a
   redirect hop (there is no hop). The local caretaker's own cleartext-key
   refusal in `app/gateway/caretaker_client.py` is untouched and still runs
   before any request.
7. **Secrets are unloggable.** `app/log_safety.py` gains a redaction filter
   installed on the root handlers at startup (`app.main`) that scrubs registered
   credentials (provider `api_key`/`management_key` resolved from `${VAR}`,
   `CARETAKER_KEY`) and `Bearer`/`api-key` shaped values from every record at
   every level. The guard never logs request headers; a captured-log test pins
   both properties.
8. **Configuration load rejects, request time refuses.** At load
   (`app/proxy/providers.py:reload`) each enabled provider's URL fields are
   checked structurally (no DNS, so a transient resolver failure cannot reject a
   provider); a rejected provider gets `destination_error`/`destination_error_field`,
   one structured `🛑` log line, and is visible as `destination_status` in
   `GET /api/cloud/providers`. Every request that would reach it still fails
   with the typed `502 destination_denied` before a socket is opened — the
   request-time check is the enforcement, the load-time check is the visibility.
9. **TLS verification is mandatory and not configurable.** The guard never
   touches `verify`; `https` destinations keep httpx's default certificate and
   hostname verification, and SNI is the approved hostname even when the
   connection is pinned to an address. There is deliberately no
   "skip verification" knob — a self-signed internal endpoint needs a trusted
   certificate, not a bypass.

## 3. Destination inventory (derived from the live configuration)

Everything below is the shipped configuration this record was written against:
`config/global.settings.yaml` and `config/providers/*.settings.yaml`, in the
deployment checkout. The allowlist default in the same PR approves exactly this
set — nothing broader.

| Destination | Provider / owner | Field(s) | Transport | Credential |
| --- | --- | --- | --- | --- |
| `127.0.0.1:11440` | `ai-kvm2-local` | `base_url` (`/v1`) | http (loopback) | none (managed, keyless) |
| `localhost:11440` | proxy | `proxy.target` → `/props`, `/health`, slot state | http (loopback) | none |
| `127.0.0.1:11441` | `ai-kvm2-local` | `management_url` (`/ensure`, `/unload`, `/status`) | http (loopback) | `CARETAKER_KEY` |
| `127.0.0.1:11450` | `ai-kvm2-local` | `tts_url` (`/tts`), `management_url` (`/tts/ensure`) | http (loopback) | `CARETAKER_KEY` |
| `127.0.0.1:4000` | `github-copilot` | `base_url` (Copilot bridge) | http (loopback) | `${GITHUB_API_KEY}` |
| `127.0.0.1:4141` | Copilot bridge app | `github-copilot-bridge.settings.yaml: upstream_url` | http (loopback) | `COPILOT_GATEWAY_KEY` |
| `127.0.0.1:8188` | services | `services.comfyui_url` (`/free`) | http (loopback) | none |
| `192.168.1.245:11440` | `14700k-local` | `base_url` (`/v1`), `catalog_url: /models` | **http LAN — cleartext** | `${WINDOWS_LAN_KEY}` (**B-03**) |
| `192.168.1.245:11441` | `14700k-local` | `management_url` (`/ensure`) | **http LAN — cleartext** | `${WINDOWS_LAN_KEY}` (**B-04**) |
| `192.168.1.245:11450` | `14700k-local` | `tts_url` (`/tts`) + `/tts/ensure` | **http LAN — cleartext** | `${WINDOWS_LAN_KEY}` (**B-04**) |
| `192.168.1.245:11451` | `14700k-local` | `stt_url` (`/transcriptions`) + `/stt/ensure` | **http LAN — cleartext** | `${WINDOWS_LAN_KEY}` (**B-04**) |
| `openrouter.ai:443` | `openrouter` | `base_url`, `catalog_url: /models/user`, `reference_catalog.sources[openrouter].url` | https | `${OPENROUTER_API_KEY}` (catalog only; the reference source sends none) |
| `integrate.api.nvidia.com:443` | `nvidia` | `base_url`, `/models` | https | `${NVIDIA_API_KEY}` |
| `api.groq.com:443` | `groq` | `base_url`, `/models` | https | `${GROQ_API_KEY}` |
| `api.openai.com:443` | `openai` | `base_url`, `/models` | https | `${OPENAI_API_KEY}` |
| `generativelanguage.googleapis.com:443` | `google` | `base_url`, `/v1beta/models` discovery | https | `${GOOGLE_API_KEY}` |
| `inference.poolside.ai:443` | `poolside` | `base_url` | https | `${POOLSIDE_API_KEY}` |
| `api.fish.audio:443` | `fish.audio` | `base_url` (`/v1/tts`, `/v1/asr`, `/model`) | https | `${FISH_AUDIO_API_KEY}` |
| `192.168.1.W:11440/11441/11450/11451` | `windows-gpu-local` (disabled, placeholder) | `base_url`, `management_url`, … | — | — | **not allowlisted on purpose**: enabling it now fails at configuration load instead of bridging four cleartext hops to an undocumented host |

`integration` tests and operator scripts (`scripts/guardianctl.py`,
`scripts/verify_post_restart.py`) are *clients* of Guardian, not Guardian
egress, and are outside this policy.

## 4. Error contract

Stable typed error: `destination_denied` (HTTP `502` — the configuration is at
fault, not the caller), with a `reason` field.

| Reason | Meaning |
| --- | --- |
| `policy_unavailable` | The `destination_policy` section is absent, malformed, empty, or has no valid entry. Everything is refused. |
| `scheme_not_allowed` | Not `http`/`https` (`file:`, `ftp:`, `gopher:`, …). |
| `userinfo_not_allowed` | The URL carries `user:pass@` (allowlist-confusion trick). |
| `host_not_allowed` | Public/mixed host not in the allowlist. |
| `port_not_allowed` | Host approved, port not. |
| `loopback_not_approved` / `link_local_not_approved` / `private_network_not_approved` | Address-class target without an explicit entry. |
| `metadata_host_blocked` | Cloud-metadata address — never approvable. |
| `dns_resolution_failed` | The name did not resolve; fail closed. |
| `dns_address_not_allowed` | A resolved address left the pinned set, or a public name resolved to a non-public address. |
| `credential_foreign_destination` | This client's credential does not belong to that destination (or the owner is unknown). |
| `credential_without_owner` | A credentialed request without a declared provider owner. |
| `redirect_not_allowed` | A hop to a different origin was attempted. |

The error body is `{"error": "destination_denied", "reason", "destination",
"purpose", "credential_owner"?, "message"?}`; `destination` is always the
sanitized `scheme://host:port` — never userinfo, never a query string, never a
credential.

## 5. Fail-closed rules

| Condition | Behaviour | Why |
| --- | --- | --- |
| `destination_policy` absent, not a mapping, `allow` missing/empty, or every entry invalid | `policy_unavailable` on every outbound request; startup logs one `🛑` line | A broken policy must never degrade into "no policy". `/healthz` (no egress) still answers, so the outage is diagnosable |
| One entry malformed (missing `host`, `ports` not integers, out-of-range port, non-IP `addresses`) | That entry is **dropped** with a `🛑` log, its host stays unreachable, the remaining entries keep enforcing | A single typo must not silently approve what it cannot parse, and must not take every other destination down with it |
| Entry for a metadata address | Ignored; `metadata_host_blocked` | Never approvable |
| Host name without `addresses` resolves to a non-public address | `dns_address_not_allowed` | Stops a public provider name being re-pointed into the LAN |
| Pinned name resolves outside `addresses` | `dns_address_not_allowed` | DNS rebinding |
| Resolver error or empty answer | `dns_resolution_failed` | No fallback to connecting by name |
| Credential header without `credential_owner` | `credential_without_owner` | A forgotten owner must fail closed, not open |
| Provider URL unapproved at configuration load | Provider flagged (`destination_error`), `🛑` log, `destination_status` in `GET /api/cloud/providers`; requests refused | Visible where an operator looks, not buried in prose |
| `follow_redirects=True` requested by a caller | `ValueError` at client construction | A redirect is a new destination; the policy does not follow one |

## 6. TLS policy per hop

| Hop | Today | Verification | Migration target | Owner of the change |
| --- | --- | --- | --- | --- |
| loopback (`127.0.0.1`/`localhost`, ports `11440/11441/11450/4000/4141/8188`) | http on the loopback interface | n/a (no network exposure; the bytes never leave this host's stack) | Stays cleartext; loopback is not a LAN credential exposure | none |
| public cloud providers | https | mandatory, httpx default (system trust store + hostname check via SNI) | stays https | none |
| `192.168.1.245:11440/11441/11450/11451` | **http, Bearer in cleartext** | n/a | verified TLS on the Windows host (own certificate + trust anchor here), then drop `cleartext_credentials` from the entry and switch the four provider URLs to `https://` | **operator (Windows host)** — tracked as [#59](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/59) |

mTLS is **evaluated and not implemented**: both ends of every https hop are
either a public API (which does not ask for a client certificate) or, after
migration, a single-operator LAN daemon whose authentication is already a Bearer
key over verified TLS. A client certificate would add a second credential to
distribute and rotate on a host the operator reaches over RDP, without removing
the Bearer key. Revisit if a second tenant ever shares those LAN daemons.

## 7. LAN cleartext migration — per hop, and the blocker

The four hops are **approved but flagged**, not pretended away. The policy
entry carries `cleartext_credentials: true`, the startup log warns with the
exact URL list, and the blocker is a tracked issue
([#59](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/59),
label `Possible security concern`).

What migration requires, per hop — all of it on the Windows host, which this
loop does not control:

| Hop | What has to happen | Why it needs the Windows side |
| --- | --- | --- |
| `:11440` inference (`llama-server`) | Serve HTTPS (`--ssl-key-file`/`--ssl-cert-file`) or front it with a TLS proxy; the launcher command lives in the caretaker, not here | The daemon is started by NSSM/the caretaker on `teams-host`; Guardian only sends requests |
| `:11441` caretaker control API | Same: the caretaker must listen on TLS, and `/ensure`, `/unload`, `/status` must stay reachable over it | The caretaker process and its listener configuration are on the Windows host |
| `:11450` TTS engine | TLS on the speech daemon (`/tts`, `/tts/ensure`) | Third-party daemon started on that host |
| `:11451` STT engine | TLS on the transcription daemon (`/transcriptions`, `/stt/ensure`) | Same |
| **all four** | A certificate the Windows host can serve (private CA or ACME), the matching trust anchor installed on this host (`/usr/local/share/ca-certificates/`), client-side update in `config/providers/14700k-local.settings.yaml` to `https://…`, then the allowlist entry loses `cleartext_credentials` | Certificate issuance and trust distribution span both machines |

Operator action, in order: (1) issue/obtain a certificate the Windows host can
serve, (2) configure the four daemons for TLS, (3) install the trust anchor on
this host, (4) change the four URLs in `config/providers/14700k-local.settings.yaml`
to `https://` and reload, (5) remove `cleartext_credentials: true` from the
`192.168.1.245` entry. Steps 4–5 are a config change here; steps 1–3 are the
blocker.

Until then the honest statement is: **four Bearer-authenticated hops to a LAN
host are in cleartext on a host with no firewall**, accepted for the migration
window, visible in every startup log, tracked in #59.

## 8. Deliberately left to later rows

- **G1-03 (admission and audit):** request/body bounds, rate limits, and the
  *metadata* audit of outbound calls (which destination, which owner, how often).
  This record establishes that a secret cannot be logged; it does not add audit
  events.
- **G2 (registered adapters):** a first-class outbound adapter registry with
  per-service methods/paths. `app/destination_policy.py` is the enforcement
  primitive that adapter will reuse; it deliberately does not model HTTP method
  or path policy beyond the URL.
- **The Copilot bridge's LiteLLM call.** `app/copilot_bridge.py` uses LiteLLM for
  the completion call, which brings its own HTTP stack; the client cannot set
  `api_base`/`api_key` (pinned by `tests/unit/test_copilot_bridge.py`) but the
  call itself is not routed through the guard. Its *catalog* call is. Migrating
  LiteLLM onto the guard belongs with the G2 adapter work.
- **Legacy tooling** (`app/tweaker/legacy/*`, `app/local_inference/ollama.py`)
  is not on the gateway request path and was not converted.
- **B-05 (plaintext ingress)** and **B-07 (`sudo systemctl` fallback)** stay as
  the G0 baseline recorded them; neither is an egress destination.

## 9. Consequences

- **On the restart that ships this:** no approved destination changes behaviour.
  Every provider, catalog, management, speech and local-backend call keeps
  working; they are simply validated first. The startup log adds one `🛡️` line
  with the approval count and one `⚠️` line naming the four acknowledged
  cleartext hops.
- **A new provider, host or port now needs an allowlist entry.** That is the
  point of the record: adding a destination becomes a reviewed configuration
  change, not an incidental field edit. `POST /api/config/reload` applies it
  without a restart.
- **`windows-gpu-local` (the #48 placeholder) can no longer be enabled without
  an allowlist entry**, which is the class-level guard that issue asked for.
- **Failure mode:** a broken or deleted `destination_policy` takes all outbound
  traffic down with a typed `502` and a `🛑` log. That is intentional and is not
  softened by a fallback.
- **No effect** on client-facing request/response bodies of allowed calls, on
  routing semantics, on capture policy, on ports, or on the ingress TLS.

## 10. Tests

`tests/unit/test_destination_policy.py` (57 tests) pins the whole matrix:

- **Negative:** denied destination; redirect to an unapproved host; DNS answer
  outside the allowlist and outside a pin; `169.254.169.254` (also when
  allowlisted); non-http(s) schemes; URL with userinfo; unapproved loopback
  (`127.0.0.2`, `::1`), unapproved loopback port, link-local and RFC1918
  targets; provider URL on an unapproved loopback port; provider placeholder
  host; malformed and absent allowlist (every shape) → fail closed; a malformed
  entry that must not disable the rest; the closed-policy transport.
- **Positive:** every destination of the shipped configuration is approved and
  the shipped policy is valid; each known LAN/loopback/public hop still works;
  the four cleartext hops are flagged; an approved LAN destination is reachable
  through the guard; the caretaker cleartext-key refusal still refuses; pinning
  preserves `Host`/SNI; fallback across validated addresses.
- **Credential:** provider A's key is refused at provider B's destination; a
  credentialed request without an owner is refused; an unknown owner is
  refused; the own-key/own-destination case still works; no credential on a
  redirect hop; no secret in captured logs at `DEBUG`; the redaction filter
  scrubs registered and `Bearer`-shaped secrets.
- **Configuration load:** the provider registry flags an unapproved provider
  URL and leaves an approved one untouched.

## 9. Reboot safety: absent is not the same as malformed

Both G1 config blocks are new, so an operator can end up running code that
supports them against a `config/global.settings.yaml` that predates them (their
own copy of the file, or a config they did not reconcile). Two different failure
modes follow, and they must not be confused:

| Doelbewust? | `destination_policy` | `auth.scopes_enforced_after` |
| --- | --- | --- |
| **Absent** (key missing entirely) | the **shipped allowlist** is used, with a WARNING naming the reason | the **shipped deadline** (`2026-11-11T00:00:00Z`) is used, with a WARNING |
| **Malformed** (present but broken) | fail closed — every outbound request is denied with `destination_denied` / `policy_unavailable` | fail closed — scopes are enforced immediately, no grace |

Rationale: a missing block must not silently take the deployment down on the
next restart, while a typo must never silently approve a destination or extend a
migration window. "Absent" therefore means "behave like the shipped
configuration", and the warning makes it visible in the startup log either way.

This is the amended G1-01 rule as well (an absent timestamp used to enforce
immediately); the amendment is recorded in
`G1-01-privilege-scopes.md` §"Amendment 2026-10-28".

## Amendment 2026-10-28 — §2.1's completeness claim was false (#65)

§2.1 stated that *all* outbound client constructions in `app/` were converted.
That claim was false when it was written, and it stayed false until issue #65.

**What was true.** Every other outbound path — providers, catalog discovery,
management/caretaker, speech, sessions, context metadata, ComfyUI, the Copilot
bridge, cloud forwarding and the main local-inference router — really did go
through `guarded_async_client`. The mechanism, the enforcement point below the
call sites (redirects, retries, per-hop re-validation) and the pinning behaved
exactly as §2 describes; nothing in this amendment weakens §2.2–§2.9.

**What was missed.** Two production hops in
`app/local_inference/ollama.py` — `chat_ollama` (`/api/chat`) and
`generate_ollama` (`/api/generate`) — built a bare
`httpx.AsyncClient(timeout=request_timeout)` for
`{_llama_server_url}/v1/chat/completions` (then `build_request` + streaming
`send`). Those two hops never called `DestinationPolicy.check()`: no allowlist
check, no pinning to the validated address (a DNS answer that changed between
configuration and connect was never re-checked, and the connection followed the
name), no
`follow_redirects: False`, and no G1-03 audit record when the hop was — or
would have been — refused. The destination currently resolves inside the
shipped allowlist (`127.0.0.1:11440`), so this was a silent guard bypass and a
silent hole in the audit trail, not a live SSRF: `_llama_server_url` is
deployment configuration, not client input. It would become reachable the
moment that URL was misconfigured. The G1-03 part 2 work found it by noticing
that these inference hops produced no audit record; #65 records the finding.

The count in §2.1 was stale as well: at the time of the conversion there were
29 guarded constructions plus these 2 unguarded production ones (and 3 in the
offline `app/tweaker/**` tooling, which never runs inside the gateway). "24"
was never a verified figure; completeness was the claim that mattered, and it
was wrong.

**What changed.** Both sites share `_guarded_backend_client()` in
`app/local_inference/ollama.py`, which is
`guarded_async_client(timeout=..., purpose="local-inference")`. Streaming,
`build_request`/`send` semantics, the `_await_or_cancel_request(...,
cleanup=client.aclose)` contract and the caller's timeouts
(`_build_stream_timeout` when streaming, the plain timeout otherwise — the
guard must never override them) are unchanged. A refused hop now raises the
typed `destination_denied` with the policy reason, and leaves one G1-03 audit
record on the request.

**Where it is enforced now.** `tests/unit/test_outbound_client_guard.py` scans
every module under `app/` with `ast` and fails on any raw
`httpx.AsyncClient(…)`/`httpx.Client(…)` construction that is not the guard
factory itself or a named, reasoned allowlist entry (today: the three offline
`app/tweaker/**` sites); a stale entry fails too, the allowlisted modules must
stay unimportable from the gateway app, and a planted construction proves the
scan is not vacuous. `tests/unit/test_ollama_destination_guard.py` pins the two
converted paths: refusal for an unapproved destination, refusal for a resolver
answer outside a pin, the pinned-address connect on the allowed path, the
preserved streaming/plain timeouts, and the audit record for a refused hop.
This class of omission can no longer reach `main` unreviewed.

## Amendment 2026-10-28 — the allowlist cannot see a path (#75)

§2.2 says the policy "deliberately does not model HTTP method or path policy
beyond the URL". That boundary is correct and stays: the guard decides *where*
a request goes, not *which endpoint of an approved destination* it reaches. Four
`py/partial-ssrf` alerts sat exactly on that boundary, all the same shape — a
client-supplied `{path:path}` route parameter spliced onto a configured base
URL (`f"{_llama_server_url}/v1/{path}"`). This amendment records what each site
actually exposes, what the guard does and does not absorb, and the explicit
bound that now covers the part the guard cannot see.

**What was true.** All four sites already used
`destination_policy.guarded_async_client(...)`, so every hop was validated as a
destination: the assembled URL's scheme, host and port were checked against the
allowlist, the connection was pinned to a validated address, and a refused hop
wrote a G1-03 audit record. The client could not choose the host: `path` is
appended *after* the authority, and no tested component (`..`, `%2e`, `@`,
`//`, `\`, NUL, `#`) moves it — `tests/unit/test_upstream_url.py::test_a_hostile_component_can_never_move_the_destination`.
The destination half of #75 was genuinely absorbed.

**What was missed.** The client could still choose the *endpoint*.
`httpx` normalizes dot segments before the request leaves the process, so
`POST /v1/../slots` is sent as `POST /slots` — the configured backend's
slot-management endpoint, at the approved host and port, with an approved
`Host`. The guard validated that hop and allowed it
(`tests/unit/test_upstream_url.py::test_the_guard_accepts_the_escape_the_builder_refuses`).
Both `/v1/{path:path}` routes are `Scope.CLIENT` (`app/proxy/scopes.py`), so any
onboarded key could reach the backend endpoints Guardian uses *internally* —
`/props`, `/slots/0?action=save|restore` (`app/gateway/sessions.py`,
`app/gateway/context_metadata.py`), `/slots` (live prompt state of other
requests), `/metrics`, `/health`, `/tokenize`, `/apply-template`. That is
endpoint confusion and cross-client prompt exposure, not SSRF: no new host, no
new credential, no redirect. Same class at the GET passthrough, which also
forwarded arbitrary query parameters to whatever endpoint the path selected.

### The four sites, before and after

| Site (now) | Who controls what | What stops it | Test that proves it |
| --- | --- | --- | --- |
| `app/gateway/routing.py:216` (alert #13, `:255`) — POST, non-inference passthrough | `path` → the whole path after `/v1/`; the authority is `_llama_server_url` from `config/global.settings.yaml` and is not reachable from the request | `backend_v1_url()` before the body is read: dot segments (`..`, `%2e%2e`, `%252e…`), backslash, absolute path, `?`/`#`, control characters → `400 path_not_allowed` with a specific `path_reason`; the assembled URL is re-parsed and must stay under `/v1/`. Destination: the guard, unchanged | `test_v1_path_bound.py::test_post_passthrough_refuses_a_dot_segment_before_reading_the_body`, `::test_post_passthrough_forwards_a_legitimate_path_unchanged`, `::test_post_passthrough_refuses_an_unapproved_backend_port` |
| `app/gateway/routing.py:1057` (alert #14, `:1048`) — POST, non-streaming inference hop | same `path`; same base URL | the same single validated `upstream_url`, used for the hop (no second assembly); the guard still owns the destination, and its refusal reaches the client as this branch's 502 detail | `test_v1_path_bound.py::test_nonstreaming_inference_hop_sends_the_validated_v1_url`, `::test_inference_hop_surfaces_the_guards_refusal` |
| `app/gateway/routing.py:1072` (alert #15, `:1063`) — the retry after `httpx.ConnectError` | same `path` | the retry reuses that same validated URL object; a reload cannot re-introduce an unvalidated string | `test_v1_path_bound.py::test_the_reload_retry_reuses_the_same_validated_url` |
| `app/proxy/server.py:1555` (alert #12, `:1552`) — GET passthrough | `path` → the endpoint; `request.query_params` → the query of that same endpoint | `backend_v1_url()` before the client is built; the query is forwarded as typed parameters and can only select among results of the already-chosen `/v1/…` endpoint, never its path or destination | `test_v1_path_bound.py::test_get_passthrough_refuses_a_dot_segment`, `::test_get_passthrough_forwards_a_legitimate_path_and_its_query`, `::test_get_route_refuses_an_unapproved_backend_destination` |

Two sibling constructions with the same shape (`routing.py:803`, `:821`, the
streaming branch and its retry) were converted to the same builder; they never
carried their own alert.

### What was constrained, and how

`app/upstream_url.py` is the one builder for this shape:
`backend_v1_url(base_url, path)` for the local backend (`/v1` prefix) and
`build_upstream_url(base_url, path, prefix=...)` for a base that already
carries its own API prefix. It validates the component (no dot segment after
repeated percent-decoding, no `\`, `?`, `#`, control characters, no absolute
path), formats the URL in one named function, then **re-parses the result** and
refuses it unless scheme, host and port are the base's and the path is still
inside the template's prefix (`path_escape`, `destination_changed`,
`query_in_path`). The second layer is not decoration: it fires when the
component check is bypassed, and it is the check that would catch a "cleaner"
`urljoin`-based join, which treats `//host/x` as a new authority
(`test_the_second_layer_refuses_an_escape_the_first_layer_missed`,
`test_the_invariant_catches_a_join_that_would_move_the_destination`).

Deliberately **not** done: no endpoint-name allowlist (endpoint *names* are the
backend's contract; bounding the shape keeps every legitimate `/v1/…` call,
pinned by `test_legitimate_v1_paths_still_build_the_same_url`), and no change to
the destination allowlist — that is policy, not an SSRF fix.

### Where it is enforced now

`tests/unit/test_upstream_url.py` pins the builder, the guard's blindness to the
path, and a recurrence guard: no module under `app/` may hand an outbound httpx
call a URL built by interpolating a request path (`path`, `*_path`) — the class
of bug behind all four alerts is a CI failure now, with a planted pre-fix
snippet proving the scan is not vacuous. Configuration-derived interpolations
(`self.server_url`, `route['base_url']`) are out of that rule's scope.
`tests/unit/test_v1_path_bound.py` (12 tests) drives the four real handlers.

### Known limits, recorded

- **`ProviderRegistry.build_forward_url`** (`app/proxy/providers.py:774`,
  `f"{provider.base_url}/{path}"`) has the same shape and is *not* one of the
  four #75 alerts. It is left unchanged: the destination there is the provider
  the caller's own key already belongs to and the traversal can only reach that
  provider's own API surface. `build_upstream_url(provider.base_url, path)` is
  the drop-in bound if a follow-up wants it
  (`test_the_provider_base_shape_is_bounded_by_the_same_builder` shows the
  primitive already covers it).
- **Refusal surface unchanged.** The inference branch wraps upstream failures in
  its own 502, so a guard refusal reaches the client as that 502's detail
  (message carries `destination denied (<reason>)`) rather than as a typed
  `destination_denied` body; the passthrough branches raise the typed error
  unchanged. Shaping the client-facing error is G1-03 admission work, not part
  of this bound.
