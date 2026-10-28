# Guardian Config Schema — plan

> Status: **IMPLEMENTED** (2026-08-21, PR #9 op branch `config-schema-catalog-url`)
> Doel: `config/settings.yaml` strippen tot elke setting een eigen, duidelijk
> bestand heeft — geen "alles.yaml" meer. Naamgeving: domain-first, sorteerbaar,
> zelfbeschrijvend (operator: "bestandnamen moeten aan de buitenkant al duidelijk
> zijn, makkelijk sorteerbaar, dir-benamingen als een pro").

## 1. Naamgevingsconventie

Patroon: `<domain>.<kind>.<scope?>.yaml`, met een uitzondering voor models waar
de scope vóór het kind staat: `<domain>.<scope>.<kind>.yaml`.

- **domain** — waar het over gaat: `global` (global infra/subsystem), `providers`
  (cloud gateways), `models` (modellen), `guardian` (het product zelf: API keys).
- **kind** — `settings` (defaults) of `overrides` (afwijkingen). Naam zegt het.
- **scope** (bij models) — `local` of `cloud`, geplaatst ná domain en vóór kind:
  `models.cloud.settings.yaml`, `models.local.overrides.yaml`.
- **entity-vorm** (bij guardian): het bestand stelt geen "settings/overrides"
  voor maar een concrete entiteit (identiteiten/keys): `guardian.keys.yaml`.

Dus de twee schema's:
- generiek: `<domain>.<kind>.yaml` (bijv. `global.settings.yaml`, `providers.overrides.yaml`)
- models:  `<domain>.<scope>.<kind>.yaml` (bijv. `models.cloud.overrides.yaml`)
- entity:  `<domain>.<entity>.yaml`     (bijv. `guardian.keys.yaml`)

Sorteerbaar: per domain gegroepeerd (global → guardian → models → providers),
binnen models cloud vóór local. Je ziet in één oogopslag wat defaults vs afwijkingen is.

```
config/
├─ .env                            # SECRETS + machine-paden (${VAR}-expansie)
├─ global.settings.yaml            # GLOBAL: proxy/queue/timeouts/scaler/capture/grammar/cloud_retry/failover/benchmark/services
├─ guardian.keys.yaml              # Guardian API keys (36, cloud_gateway_access)
└─ providers/                      # EÉN bestand per provider (F2, docs/CONFIG_PROVIDER_FILES.md)
   ├─ ai-kvm2-local.settings.yaml  # lokale provider: base_url + models/aliases/guardian registry
   ├─ openrouter.settings.yaml     # base_url/api_key/timeout/prefixes + catalog_url + models:-overrides
   ├─ nvidia.settings.yaml         # idem + catalog_allowlist + models:-overrides
   ├─ google.settings.yaml
   ├─ openai.settings.yaml         # + models:-overrides (gpt-4o etc.)
   ├─ poolside.settings.yaml
   └─ groq.settings.yaml

# F2 (2026-08-26) vervangt: providers.settings.yaml + providers.overrides.yaml +
#   models.local.settings.yaml + models.cloud.overrides.yaml door de providers/
#   directory (één document per provider, incl. per-model overrides in `models:`).
# Gereserveerd (nog NIET meegeleverd — nog geen runtime-consumer):
#   models.cloud.settings.yaml  (cloud model defaults)
#   models.local.overrides.yaml (lokale model afwijkingen)
```

## 2. Volledig config-landschap (huidig → nieuw)

| Huidig | Nieuw | Rol |
|---|---|---|
| `.env` | `.env` (ongewijzigd) | secrets + machine |
| `guardian_apikeys.yaml` | `guardian.keys.yaml` | Guardian keys — compat-symlink verwijderd 2026-08-22 |
| `local_models.yaml` (+ symlink `models.yaml`) | `providers/ai-kvm2-local.settings.yaml` | lokale registry (`local_models.yaml` symlink wijst nu naar de provider-file) |
| `cloud_models.yaml` | *(per-provider `models:`-blok, F2)* | cloud model afwijkingen + sampling/context — compat-symlink verwijderd 2026-08-22 |
| `providers.settings.yaml` + `providers.overrides.yaml` | `providers/<naam>.settings.yaml` | één bestand per provider (F2, 2026-08-26) |
| `models.local.settings.yaml` | `providers/ai-kvm2-local.settings.yaml` | lokale registry (models/aliases/guardian in de lokale provider-file) |
| `models.cloud.overrides.yaml` | `providers/<naam>.settings.yaml` → `models:`-blok | per-model overrides bij de juiste provider (F2) |
| *(nieuw, gereserveerd)* | `models.cloud.settings.yaml` | cloud model defaults — **nog geen consumer, nog niet geleverd** |
| *(nieuw, gereserveerd)* | `models.local.overrides.yaml` | lokale model afwijkingen — **nog geen consumer, nog niet geleverd** |
| `providers.*` in settings.yaml | `providers/<naam>.settings.yaml` | provider config |
| `settings.yaml` | `global.settings.yaml` | global infra + subsystem |
| `api_keys.json`, `cloud_keys.json`, `models.yaml`, `benchmark_models.json` | *(verwijderd 2026-08-22)* | legacy — vervangen door `guardian.keys.yaml` resp. `providers/ai-kvm2-local.settings.yaml`; zie `.scratch/cleanup/legacy-config/` |
| `current_model.args/.sig`, `data/cloud_catalog_cache.json` | ongew. | runtime, niet hand-editeerbaar |

## 3. Mapping: waar elke settings.yaml-key heen gaat

| Huidige key | Nieuwe plek |
|---|---|
| `providers` | `providers/<naam>.settings.yaml` — één document per provider (F2) |
| `context_overrides` | per-provider `models:`-blok (context_window) — was `models.cloud.overrides.yaml` |
| `proxy`, `services`, `services_to_stop`, `queue`, `timeouts`, `scaler`, `benchmark` | `global.settings.yaml` |
| `capture`, `grammar`, `cloud_retry`, `failover_health` | `global.settings.yaml` |
| `failover_groups` (failover.py) | `global.settings.yaml` |

## 4. Catalog-endpoint als provider-default

Per provider optioneel `catalog_url` (default `/models`):

```yaml
# providers.settings.yaml
providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}
    timeout_seconds: 1200
    model_prefixes: [...]
# providers.overrides.yaml — afwijkingen
providers:
  openrouter:
    catalog_url: /models/user   # alleen echt toegankelijke (guardrails/privacy gefilterd)
```

`app/proxy/cloud_catalog.py`: `url = f"{provider.base_url}{provider.catalog_url or '/models'}"`.

### 4b. Reference catalog sources (OpenRouter-parity metadata)

A provider file may additionally declare cross-provider **reference** sources.
These are metadata-only: they are fetched independently of `catalog_url` and
never change which models are advertised, routed or allowlisted.

```yaml
# config/providers/openrouter.settings.yaml
reference_catalog:
  sources:
    - name: openrouter
      url: https://openrouter.ai/api/v1/models
      enabled: true
      ttl_seconds: 86400
      send_api_key: false
```

| Key | Default | Meaning |
|---|---|---|
| `name` | provider name | Source label reported in `metadata_sources` as `reference:<name>` |
| `url` | — | Reference catalog endpoint (required for the source to be used) |
| `enabled` | `true` | Set `false` to keep the block but stop fetching |
| `ttl_seconds` | `86400` | Refresh TTL; a fresh catalog costs zero network traffic |
| `send_api_key` | `false` | Send the declaring provider's API key with the request |

Reader: `app/proxy/openrouter_reference.py` (`ModelReferenceCatalog`). Sources
are re-read by `POST /api/config/reload`; the cache lives at
`data/model_reference_catalog.json` (`app/paths.py:model_reference_catalog_file`).
Full field contract: `@docs/OPENROUTER_PARITY.md` §2.

## 5. Wie leest de huidige settings.yaml (consumers-impact)

| Key | Reader |
|---|---|
| `providers` | `proxy/providers.py`, `proxy/ratelimit.py` |
| `proxy` | `config_loader.py`, `proxy/server.py` |
| `queue` | `main.py`, `config_loader.py`, `tweaker/legacy/benchmark_suite_v1.py` |
| `timeouts` | `config_loader.py`, `local_inference/models.py` |
| `scaler` | `proxy/scaler.py` |
| `capture` | `capture/config.py` |
| `grammar` | `gateway/normalization.py`, `config_loader.py`, `local_inference/ollama.py` |
| `cloud_retry` | `gateway/admin_api.py`, `config_loader.py`, `proxy/server.py` |
| `failover_health` | `gateway/admin_api.py`, `config_loader.py`, `proxy/server.py` |
| `context_overrides` | `proxy/providers.py` |
| `services` | `engine/manager.py` |
| `benchmark` | `scheduler/manager.py` |
| `services_to_stop` | `scheduler/manager.py` |
| `failover_groups` | `proxy/failover.py` |

`app/config_loader.py` = centrale leesswitch: merges de nieuwe files tot het
bestaande config-dict, zodat alle `.get("key")`-reads intact blijven.

## 6. Migratiestappen (operator: directe volledige cutoff)

1. **Backup**: `cp config/settings.yaml config/settings.yaml.bak` (rollback).
2. **Nieuwe files aanmaken** volgens §1, inhoud herverdeeld uit §3. Overrides
   en cloud-defaults starten als lege templates.
3. **Compat-symlinks** (kortstondig) zodat hardcoded `paths.py`-verwijzingen
   blijven werken: `models.yaml`, `local_models.yaml`, `cloud_models.yaml`,
   `settings.yaml`, `guardian_apikeys.yaml` → symlink naar nieuwe namen.
   (**Uiteindelijke stand 2026-08-22:** alleen `local_models.yaml` bestaat
   nog als compat-symlink; `models.yaml`, `settings.yaml`,
   `cloud_models.yaml`, `guardian_apikeys.yaml` zijn verwijderd — de code
   wijst via de `app/paths.py`-resolvers naar de canonieke namen.)
4. **`config_loader.py`**: lees + merge `global.settings.yaml` →
   `providers.settings.yaml` → `providers.overrides.yaml` (overrides winnen)
   tot hetzelfde config-dict. Alle consumers blijven werken.
5. **`providers.py`**: leest `providers.settings.yaml` + `providers.overrides.yaml`;
   `CloudProvider` krijgt `catalog_url`.
6. **`cloud_catalog.py`**: `catalog_url or "/models"`.
7. **Directe files-lezers/-schrijvers omzetten** (copilot-review pt3 — deze
   bypassen `config_loader.py` en openen `config/settings.yaml` rechtstreeks;
   ze breken als de compat-symlink wegvalt, en scaler/guardianctl kunnen naar de
   verkeerde laag schrijven):
   - `app/proxy/scaler.py` (regels 73/161/181) — leest én schrijft `:73`+profielen
   - `app/capture/config.py` (`:254`) — leest capture-blok
   - `app/scheduler/manager.py` (`:25`) — leest services/services_to_stop/benchmark
   - `app/proxy/failover.py` (`:283`) — leest failover_groups
   - `scripts/guardianctl.py` (`:34`) — leest/schrijft capture-cli
   Elke lezer moet naar de juiste nieuwe file wijzen (global vs providers), elke
   schrijver naar de juiste overrides-laag. Dit is een expliciete, geteste stap —
   niet weg te redeneren achter de compat-symlink.
8. **`paths.py`**: update naar nieuwe bestandsnamen; symlinks na succesvolle
   liveswitch ingekort tot de echte namen en `settings.yaml.bak` verwijderd.
9. **`pre_restart_check.py`** (4 gates) vóór restart; daarna restart.
10. Recovery bij falen: `cp settings.yaml.bak config/settings.yaml` terug +
    legacy paden herstellen (geen self-heal — zie AGENTS.md).

## 7. Tests

- `test_config_reload.py`: leest + merge van de nieuwe files (global, providers
  settings+overrides), overrides winnen.
- `test_providers.py`: `CloudProvider.catalog_url` default `/models`, override
  gerespecteerd.
- `test_cloud_catalog.py`: `refresh_provider` gebruikt `catalog_url` indien gezet.
- Bestaande suite 100% groen (merged dict houdt top-level keys compatibel).

## 8. Risico's & mitigatie

- **Crash bij restart door gemiste reference** → directe cutoff (operators keus).
  Mitigatie: backup (§6.1), compat-symlinks (§6.3), merged-dict (§6.4), gate
  (§6.8), recovery (§6.9).
- **`/models/user` filter** → eerste `providers.overrides.yaml` entry: `catalog_url`.
- **`model_defaults` uit cloud_keys.json** (temp/top_p/max_tokens/seed) →
  `models.cloud.overrides.yaml` vóór verwijdering van cloud_keys.json.

## 9. `auth` — privilege scopes (G1-01, 2026-10-28)

Source of record: `docs/decisions/G1-01-privilege-scopes.md`. Added to
`config/global.settings.yaml`; hot-reloadable through `POST /api/config/reload`
(no restart needed to move the window).

```yaml
auth:
  scopes_enforced_after: "2026-11-11T00:00:00Z"
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `auth.scopes_enforced_after` | ISO-8601 UTC timestamp (string) | `"2026-11-11T00:00:00Z"` | Instant from which a key **without** `metadata.scopes` is a plain `client` key. Before it, such a key keeps the pre-G1-01 flat access to privileged routes, with a structured grace log and `guardian_scope_grace_calls_total` on every privileged call. |

Rules:

- Keys carry their scopes in the key store, not here:
  `config/guardian.keys.yaml` -> `<token>.metadata.scopes: [client, operator]`.
  `admin` implies `operator`, `client` and `capture-export`; `operator` implies
  `client`; `capture-export` implies nothing else.
- **Absent/unparseable value enforces immediately** (fail-closed): a missing
  section, a typo'd timestamp or a non-ISO value never re-opens the permissive
  window. The same applies to an absent `auth` section.
- An unquoted YAML timestamp (`scopes_enforced_after: 2026-11-11T00:00:00Z`) is
  parsed by PyYAML into a `datetime`; both shapes are accepted. A naive value is
  interpreted as UTC.
- Moving the timestamp is itself an `admin` operation (it widens or narrows every
  legacy key's privilege), so it is not part of the `client`-reachable surface.
- Shipped default rationale: two weeks after the decision record, so the
  operator can scope the existing named keys (dashboard, monitoring, lifecycle
  scripts, export tooling) without an unattended break. Change the date in place
  when the migration is scheduled; the value is documented in the file's own
  comments as well.

## 10. `admission` — bounded ingress (G1-03, 2026-10-28)

Source of record: `docs/decisions/G1-03-admission-and-audit.md`. Added to
`config/global.settings.yaml`. `max_body_bytes`, `rate_limit.*` and
`queue_full_retry_after_seconds` are re-read per request and therefore
hot-reloadable through `POST /api/config/reload`; `queue_max_depth` is read when
the queue object is built and needs a restart (like the rest of `queue`).

```yaml
admission:
  max_body_bytes: 33554432                 # 32 MiB
  queue_max_depth: 32
  queue_full_retry_after_seconds: 60
  rate_limit:
    requests_per_minute: 300
    burst: 300
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `admission.max_body_bytes` | positive integer (bytes) | `33554432` (32 MiB) | Request body ceiling. Over it: `413 request_too_large` — a declared `Content-Length` is refused without reading, an unlabelled body is stopped at the byte. |
| `admission.queue_max_depth` | positive integer | `32` | Maximum number of requests *waiting* for a slot (`max_concurrent` still bounds execution). Over it: `503 queue_full`, refused before the entry is registered. **Restart-only.** |
| `admission.queue_full_retry_after_seconds` | positive integer (seconds) | `60` | `Retry-After` sent with that `503`. One local generation is tens of seconds, so a minute is the useful retry scale. |
| `admission.rate_limit.requests_per_minute` | positive integer | `300` | Sustained per-key budget of the token bucket (one token per request). Over it: `429 rate_limited` with `Retry-After`. |
| `admission.rate_limit.burst` | positive integer | `300` | Tokens available at rest, i.e. how much of a minute's allowance can be spent at once (reconnect storms, parallel tool calls). |

Rules:

- **Keyed on identity, not IP.** The bucket belongs to the authenticated key's
  G1-01 fingerprint (`sha256(token)[:12]`). Two keys behind one NAT get two
  buckets; one key shares its bucket across every route it calls.
- **Single process.** Buckets live in memory and reset with the service; two
  Guardian replicas would each enforce its own budget (multi-replica accounting
  is out of scope for G1-03).
- **Absent ≠ malformed** (same rule as §9 and G1-02): a missing section or key
  falls back to the shipped default with a WARNING, so a config file that
  predates this record does not take admission down on the next restart. A
  **malformed** value (wrong type, `null`, boolean, float, numeric string, `0`,
  negative, non-mapping `rate_limit`) refuses the requests that bound guards
  with `503 admission_misconfigured` naming the `limit` and the `reason`; the
  other bounds keep working.
- **No "off" value.** Unlike `queue.queue_timeout_seconds` (`0` = unbounded,
  kept as #45 shipped it), `0`/`-1` are malformed here: a bound that one digit
  can switch off is a bound nobody can rely on. Raise the number instead.
- A malformed `queue_full_retry_after_seconds` refuses the **queue-depth** bound
  (a broken hint never silently becomes "no hint").
- Shipped default rationale — **safe but not tuned**: 32 MiB admits large
  multimodal prompts and speech uploads with orders of magnitude of headroom;
  depth 32 is 32× the highest depth ever measured (1, with `max_concurrent: 1`);
  300 requests/minute is an order of magnitude above the heaviest measured
  minute (~9-20 requests) with a full minute spendable at once. These protect the
  process from unbounded bodies, unbounded waiting and unbounded arrival; they
  are not a capacity plan.

Related: `destination_policy` (G1-02) is documented in §11.

## 11. `destination_policy` — outbound allowlist and credential isolation (G1-02, 2026-10-28)

Source of record: `docs/decisions/G1-02-destination-and-credentials.md`;
implementation: `app/destination_policy.py`. A **config-file section** in
`config/global.settings.yaml`, not a per-provider file key. The policy is
rebuilt from the live configuration on every check (`current_policy()`), so a
change is live for the next outbound request after
`POST /api/config/reload` (admin scope, §9) — including clients that were
constructed once for the whole process; `log_startup_summary()` logs the
effective policy and any still-cleartext credential hop at startup and after
each reload.

```yaml
destination_policy:
  schemes: [http, https]
  allow:
    - host: 127.0.0.1
      ports: [4000, 4141, 8188, 11440, 11441, 11450]
      note: local llama-server, caretaker, TTS, Copilot bridge, ComfyUI
    - host: localhost
      ports: [11440]
      addresses: [127.0.0.1, "::1"]
      note: proxy.target (local llama-server via the localhost name)
    - host: 192.168.1.245
      ports: [11440, 11441, 11450, 11451]
      cleartext_credentials: true
      note: 14700k-local inference/caretaker/TTS/STT — cleartext Bearer (B-03/B-04)
    - {host: openrouter.ai, ports: [443]}
    - {host: integrate.api.nvidia.com, ports: [443]}
    - {host: api.groq.com, ports: [443]}
    - {host: api.openai.com, ports: [443]}
    - {host: generativelanguage.googleapis.com, ports: [443]}
    - {host: inference.poolside.ai, ports: [443]}
    - {host: api.fish.audio, ports: [443]}
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `destination_policy.schemes` | non-empty list (a bare string is accepted) of `http` / `https` | `[http, https]` | Schemes the guard accepts. Any other scheme is refused with `scheme_not_allowed`; an unsupported value *inside* this list closes the whole policy (`policy_unavailable`). |
| `destination_policy.allow` | non-empty list of entry mappings | the shipped allowlist (`SHIPPED_ALLOW`) — only when the section itself is absent | The complete approval set. There are no wildcards and no host-class shortcuts, so every reachable destination is spelled out here. |
| `allow[].host` | string: bare hostname or IP literal (no scheme, port or path) | required | The only host this entry approves, compared lowercased without a trailing dot. A host declared twice keeps its **first** entry: a later duplicate cannot widen the approved port set. |
| `allow[].ports` | integer or non-empty list of integers `1`-`65535` | required | The only ports reachable on that host. A URL without a port is checked at its scheme default (`80`/`443`). |
| `allow[].addresses` | list of IP literals (a bare string is accepted) | `[]` | Pinned resolve set. When set, every address the name resolves to must stay inside it. When empty, a *name* entry may only resolve to public addresses. A value that is not an IP literal drops the entry. |
| `allow[].cleartext_credentials` | boolean | `false` | Marks a hop that carries a Bearer key over plain `http` on the LAN (B-03/B-04). It documents and surfaces the hop; it weakens no check. |
| `allow[].note` | string | `""` | Free-text operator label; no enforcement effect. |

Rules:

- **One enforcement point, below the call sites.** Provider, catalog,
  management, speech and bridge traffic is built with `guarded_async_client(...)`,
  whose transport (`DestinationGuardTransport`) validates each request before a
  socket is opened; because the check sits in the transport, redirects, retries
  and future dynamic URLs pass the same code.
- **Only listed `(host, port)` pairs are reachable.** A URL is approved only
  when its scheme is listed, it carries no userinfo (`user:pass@` fails with
  `userinfo_not_allowed`), and its `(host, port)` matches an entry above.
- **Cloud-metadata addresses are never approvable.** `169.254.169.254`,
  `169.254.169.253`, `169.254.170.2`, `100.100.100.200` and `fd00:ec2::254`
  are refused with `metadata_host_blocked` — as a URL literal and as a resolved
  answer. An allowlist entry for one gains nothing.
- **Loopback, link-local and RFC1918 are not implicitly trusted.** They are
  usable only through an explicit entry; without one the refusal names the
  class (`loopback_not_approved`, `link_local_not_approved`,
  `private_network_not_approved`), so a misconfigured catalog URL cannot reach
  an internal admin port and an unlisted port on an approved host still fails
  with `port_not_allowed`.
- **A name entry must pin `addresses` or resolve to public addresses only.** The
  resolver is asked again at request time, every returned address is validated,
  and the connection is pinned to the validated address with `Host` and TLS SNI
  kept at the approved hostname. An answer outside a pin, or a public name
  answering with an internal address, fails with `dns_address_not_allowed`; a
  failed or empty resolution fails with `dns_resolution_failed` (never a
  fallback to connecting by name). An IP-literal entry is its own approval.
- **Redirects are never followed.** `follow_redirects=True` is refused at
  client construction (`ValueError`), the transport refuses a hop whose origin
  differs from the previous destination (`redirect_not_allowed`), and a `3xx`
  is returned to the caller untouched. A credential therefore cannot be carried
  to a new destination.
- **A credential header is bound to its own provider's destination.** A request
  carrying `authorization`, `proxy-authorization`, `x-api-key`, `api-key` or
  `x-goog-api-key` is refused unless the client was built for an owning
  provider (`credential_owner`) *and* the destination is one of that provider's
  own configured URLs (`base_url`, `management_url`, `tts_url`, `stt_url`, an
  absolute `catalog_url`) — otherwise `credential_foreign_destination`. A
  credentialed request without a declared owner fails closed
  (`credential_without_owner`). Provider A's key can never reach provider B.
- **TLS verification is mandatory.** The guard never sets `verify`, so httpx's
  default certificate and hostname verification stays in force, and SNI is the
  approved hostname even when the connection is pinned to an address. There is
  no skip-verification knob. `trust_env` defaults to `False`, so a proxy
  environment variable cannot route traffic around the guard.
- **Configuration load rejects, request time refuses.** Each enabled provider's
  URL fields are checked structurally at load (no DNS); an unapproved field
  flags the provider (`destination_error`, `destination_status` in
  `GET /api/cloud/providers`) with one error log line, and every request that
  would reach it still fails with the typed `502 destination_denied`.

Fail-closed rules:

- **Absent is not malformed.** A configuration without a `destination_policy`
  section falls back to the shipped allowlist (`SHIPPED_ALLOW` in
  `app/destination_policy.py`: the same hosts, ports and pins as the block
  above, without the informational `note` labels) and logs a WARNING, so a
  config file written before G1-02 does not turn the gateway into a closed
  relay on the next restart.
- **A broken policy denies everything.** A non-mapping section, a `schemes`
  value that is not a non-empty list of `http`/`https`, a missing or empty
  `allow`, or an `allow` in which no entry survives parsing yields a closed
  policy: every outbound request fails with `destination_denied` / reason
  `policy_unavailable`, and startup logs one error line naming the cause. A
  broken policy never degrades into "no policy".
- **A malformed entry is dropped loudly and only refuses its own host.** A
  missing `host`, a `ports` value that is not a non-empty integer list, an
  out-of-range port or a non-IP `addresses` value rejects that entry with one
  error log line; its host stays unreachable (a name as `host_not_allowed`, an
  address literal with its class reason) while every other entry keeps
  enforcing. A single typo neither approves what it cannot parse nor takes the
  rest of the deployment down.

Reason codes (`REASON_*` in `app/destination_policy.py`; the `reason` field of
the typed error is part of the contract, so values are added, never renamed):

| Reason | Meaning |
| --- | --- |
| `policy_unavailable` | No usable policy: the section is not a mapping, or `allow` is missing, empty or has no valid entry. Everything is refused. |
| `scheme_not_allowed` | The URL scheme is not in `destination_policy.schemes`. |
| `userinfo_not_allowed` | The URL carries `user:pass@` (allowlist-confusion trick). |
| `host_not_allowed` | The host is not an explicit entry (also used for an unparseable URL or a URL without a host). |
| `port_not_allowed` | The host is approved, the port is not (also used for an unparseable port). |
| `loopback_not_approved` | A loopback address without an entry of its own. |
| `link_local_not_approved` | A link-local address without an entry of its own. |
| `private_network_not_approved` | An RFC1918 or reserved address without an entry of its own. |
| `metadata_host_blocked` | A cloud-metadata address, as a literal or as a resolved answer. Never approvable. |
| `dns_resolution_failed` | The name did not resolve, or resolved to no address. No fallback to connecting by name. |
| `dns_address_not_allowed` | An answer outside the pinned `addresses` set, or a public name answering with a non-public address. |
| `credential_foreign_destination` | A credentialed request to a destination that is not its owning provider's own. |
| `credential_without_owner` | A credentialed request whose client declares no owner. |
| `redirect_not_allowed` | A hop to a different origin was attempted. |

## 12. `audit` — metadata-only decision audit (G1-03 part 2, 2026-10-28)

Source of record: `docs/decisions/G1-03-admission-and-audit.md` §11.
Implementation: `app/proxy/audit.py`. Added to `config/global.settings.yaml`; all
three keys are re-read from the shared configuration per decision, so
`POST /api/config/reload` applies them without a restart.

```yaml
audit:
  log_decisions: true
  buffer_size: 256
  include_key_name: true
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `audit.log_decisions` | boolean | `true` | Emit one structured JSON line per decision (allow `INFO`, deny `WARNING`) on logger `Guardian.Audit` with `"event":"audit"` — the sink that ships to the operator's log aggregator. `false` keeps the in-process buffer and the metric, and stops the lines. |
| `audit.buffer_size` | positive integer | `256` | Records kept in the in-process ring buffer (`audit.recent()`), **hard ceiling 1024** — a larger value is clamped, because the bound is the memory guarantee. 1024 records × ≤ 1 KiB = ≤ 1 MiB worst case. |
| `audit.include_key_name` | boolean | `true` | Include the operator-assigned key name next to the 12-hex fingerprint. Set `false` where key names are personal data (a person, a mailbox): the record then carries the fingerprint only. |

Rules — note that this section is **fail-safe, not fail-closed** (the one G1 row
that is, §11.2 of the record): a broken audit block must never refuse traffic.
Each key falls back toward its safe side, logs one `ERROR`/`WARNING`, and the
decision is still counted in `guardian_audit_records_total{decision,reason}`.

| Condition | Behaviour |
| --- | --- |
| section absent | shipped defaults, one `WARNING` |
| `buffer_size` not a positive integer | default `256`, one `ERROR` |
| `buffer_size` above `1024` | clamped to `1024`, one `ERROR` |
| `log_decisions` not a boolean | stays `true`, one `ERROR` |
| `include_key_name` not a boolean | **`false`** (omit names), one `ERROR` — a typo must never add a name |
| section present but not a mapping | shipped settings with key names omitted, one `ERROR` |

What a record contains, and what it can never contain, is stated in
`docs/API_REFERENCE.md` ("Audit records") and in the decision record §11.1. There
is no key to disable the audit itself: `log_decisions: false` only removes the
log line, and the bounded buffer plus the metric keep the decision observable.
