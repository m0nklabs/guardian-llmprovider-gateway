# Guardian — Policy and Traffic Gateway Roadmap

> Canonical product roadmap. Guardian is the intermediary for explicitly onboarded
> services, not only an LLM provider proxy. This is a documentation and planning
> update; proposed capabilities below are not implementation claims.
>
> The original [Guardian 2.0 implementation plan](IMPLEMENTATION_PLAN.md) remains
> unchanged as the historical F0–F7 migration specification. GitHub issue #1 is
> a historical snapshot, not the source of truth for this expanded roadmap.

## 1. Product mission

**Guardian is the policy-enforcing traffic gateway between clients and services.**
It authenticates callers, authorizes operations, selects eligible destinations,
controls capacity and cost, forwards requests safely, and makes decisions and
outcomes observable. LLM routing is one service capability alongside speech and,
in later phases, explicitly registered non-AI HTTP services and tool endpoints.

"Always the intermediary" means all supported client traffic to an onboarded
service crosses Guardian. It does not mean Guardian is an unrestricted forward
proxy, a universal network interceptor, or a replacement for the firewall.
Backends must be private or network-restricted; upstream credentials must stay
server-side. A cloud API that remains publicly accessible cannot be made
Guardian-only by gateway code alone: credential ownership and client egress
policy also matter. Privileged operator recovery remains a separate, audited
control path, not a normal client bypass.

### Success outcomes

- One client identity and policy surface across heterogeneous services.
- No unauthorized route, operation, credential access, or silent substitution.
- Capacity-aware routing that preserves exact-target intent and protocol semantics.
- Bounded resource use, understandable rejection reasons, and safe cancellation.
- An operator can explain who called what, why a destination was chosen, and what
  failed without needing to inspect private payloads by default.
- New supported services reuse the gateway core rather than fork authentication,
  routing, metrics, and error handling.

### Service naming and namespace (canonical conceptual contract)

The general conceptual address is **`{provider}/{brand}/{service}`**. It names
who serves a target, whose product namespace it belongs to, and which concrete
application or service is requested. This is a product vocabulary, not a new
runtime identifier format or a claim that a generic service resolver exists.

| Term | Meaning | Boundary |
| --- | --- | --- |
| `provider` | The specific serving operator/platform offering that makes the target available | May be local or cloud; distinguishes `github-copilot` from other GitHub APIs and need not be the product/model maker |
| `brand` | The product or model maker namespace | Identifies the product's origin, not the serving platform or protocol |
| `service` | The concrete application, API service or model being addressed | Names the requested target, not its interface or execution engine |

The existing cloud address **`{provider}/{brand}/{model}`** is the LLM
specialization: the model occupies the service position. For example,
`openrouter/anthropic/claude-sonnet-4.5` separates the serving platform
(`openrouter`), model maker (`anthropic`) and concrete model
(`claude-sonnet-4.5`). This illustrates the roles; actual availability remains
subject to the configured provider, catalog and access controls described in
[LLM routing](LLM_ROUTER.md).

The selected Copilot naming uses exactly three layers, with no separate channel:

| Canonical destination | Provider offering | Brand | Service/model |
| --- | --- | --- | --- |
| `github-copilot/openai/gpt-6-astra` | `github-copilot` | `openai` | `gpt-6-astra` |
| `github-copilot/openai/gpt-6.1-sol` | `github-copilot` | `openai` | `gpt-6.1-sol` |

The provider identifies the specific GitHub Copilot offering, not GitHub as an
umbrella for unrelated APIs. `copilot` is not an additional address layer or the
model brand. This provider migration removes `github/copilot/gpt-6.1-sol`;
only the two canonical destinations above are supported by the new contract,
with no legacy Copilot aliases. The new aliases target
an external LiteLLM bridge to adapt Chat Completions to Responses-only GPT models;
bridge protocol and upstream identifiers remain separate from the public name.
See the [migration contract](LLM_ROUTER.md#github-copilot-provider-and-external-bridge-migration).
Runtime activation is pending a bridge restart outside the active session.
Provider/bridge configuration work does not establish live deployment or test
success, nor change the generic resolver and non-LLM delivery status below.

**Guardian sits above this namespace as the gateway.** It is not an extra
address segment and does not require a `guardian/` prefix. Whether the target
is local or cloud-hosted does not change Guardian's intermediary role for
supported client traffic to explicitly onboarded services.

The following concepts are orthogonal to the address, not competing address
layers:

- **API:** the interface or protocol through which a service is called.
- **LLM:** a capability offered by a service; speech and other application
  capabilities do not have to be labeled as LLMs.
- **Engine:** the execution implementation, such as `llama.cpp`, behind a service.
- **Bridge:** a protocol adapter, such as Anthropic-to-OpenAI translation, rather
  than a provider, brand or service identity.

**Compatibility and unresolved policy:** unrelated existing bare aliases,
upstream IDs and adapter-specific address forms remain intact; the explicitly
selected Copilot migration above is the exception. Do not expand other names
into three segments, rename runtime fields, alter provider matching, or change
discovery endpoints merely to match this conceptual notation. For services without a
natural brand namespace, the representation is an unresolved service-adapter
and schema policy decision. No required default brand, omitted-segment syntax,
or universal normalization/resolution rule is established here. Resolve and
validate that policy in the relevant service specification with a compatibility
plan before implementation.

This vocabulary does not advance delivery status: general HTTP and tool/MCP
adapters remain planned; service-wide policy and generalized load balancing
remain partial as recorded below. Ordered failover recovers from failed or
unavailable destinations; it is not load distribution among healthy instances.

## 2. Baseline and evidence

Status vocabulary: **present** = code/documented historical evidence exists;
**partial** = some mechanisms exist, but not a general product contract;
**planned** = no completed milestone is claimed. None of this table asserts a
fresh full-suite pass or live validation of every capability.

| Capability | Baseline | Evidence / limitation |
| --- | --- | --- |
| Local and cloud LLM routing, discovery, protocol translation | Present | `app/gateway/routing.py`, `app/proxy/providers.py`, `docs/LLM_ROUTER.md`; existing aliases and explicit provider routes remain compatibility contracts |
| Speech services beyond text LLMs | Present | `app/gateway/speech_routing.py`, `app/gateway/tts.py`, `app/gateway/stt.py`; September journal records TTS/STT routing and adapter work |
| Named client keys and cloud-access controls | Present, service-wide policy partial | `app/proxy/auth.py`; not evidence of generalized tenant RBAC |
| TLS and deployment separation | Present | `deploy/nginx/`, `docs/skills/operator-runbook.md`; encryption does not itself prevent backend bypass |
| Queueing, provider health and ordered failover | Present, generalized balancing partial | `app/proxy/queue.py`, `app/proxy/failover.py`, `app/proxy/ratelimit.py`; failover is not equivalent to load balancing |
| Streaming, cancellation, capture, metrics | Present, cross-service coverage partial | `app/cloud_inference/forwarding.py`, `app/capture/`, `app/proxy/metrics.py`; existing raw capture is not safe metadata-only audit |
| Backend lifecycle delegation | Present, mixed compatibility paths | `app/gateway/caretaker_client.py`, `app/gateway/caretaker_runtime.py`; Caretaker owns backend execution, Guardian owns traffic policy |
| Registered general HTTP services and uniform tool/MCP mediation | Planned | Not implied by speech adapters or forwarding of LLM tool-call JSON |
| Distributed quotas, multi-replica HA and general policy engine | Planned | Require explicit consistency, storage and failure-mode decisions |

### Historical migration closure

- F0–F5 implementation evidence is recorded in
  `archive/agent-instructions-2026-09-22.md` and the linked PR history.
- F6 and F7 completion evidence is retained in `AGENT_JOURNAL.md`, Batch 3
  carry-forward, with details in `AGENT_JOURNAL_ARCHIVE.md`.
- The active service's `WorkingDirectory` was verified as the new checkout during
  this roadmap update. The older handoff entry calling F7 open was stale.
- Do not rerun the historical migration or treat old proposed configuration
  shapes as today's schema. Optional RPC was not established by F7 closure.

## 3. Architecture: shared core, capability adapters

```text
Clients -> private/public ingress -> Guardian data plane
                                  | identity + authorization + admission
                                  | route selection + resilience + telemetry
                                  +-> LLM adapter -> local/cloud inference
                                  +-> speech adapter -> TTS/STT service
                                  +-> registered HTTP adapter -> service [planned]
                                  +-> tool/MCP adapter -> tool endpoint [planned]

Operators -> authenticated control plane -> validated config + policy revisions
Guardian -> authenticated Caretaker control API -> backend lifecycle
```

### Boundary rules

1. **Guardian owns traffic and policy**, not model execution, training, dataset
   building, or host-wide process supervision. Caretaker owns backend lifecycle;
   Keanu owns downstream redaction/dataset transformations. Existing raw capture
   remains explicit and access-restricted, never relabeled as sanitized audit.
2. **Capability is declared, not inferred from geography.** A LAN and a cloud
   service can expose the same capability; actual compatibility must be checked.
3. **Shared core, specific adapters.** Keep auth, admission, health, telemetry and
   transport common; protocol translation and operation semantics stay explicit.
   Reuse `httpx`, current registry/config helpers and tested forwarding paths.
   Consider mature ingress/policy components before implementing equivalent
   low-level machinery; avoid a speculative plugin framework in the first slice.
4. **Exact target stays exact.** Existing explicit LLM/speech routes must not
   silently become pooled routes. Pooling is opt-in through a documented policy
   or logical service address. Candidate compatibility includes model identity,
   modality, streaming format, privacy/residency and operation semantics.
5. **Config grows by migration, not duplication.** Preserve global settings,
   per-provider files and Guardian keys. Reuse them first; introduce a service
   schema only with validation and a compatibility plan, not a second config
   system. Configuration examples in future specifications must be marked as
   proposed until supported by the loader.
6. **Data plane and control plane have distinct privileges.** Client keys do
   not imply policy-edit, lifecycle, capture-export or credential privileges.
7. **Enforcement fails closed; ancillary telemetry has a declared failure mode.**
   No hidden allow-on-policy-error path. Audit/capture write failure must follow
   explicit per-route policy; never leak credentials to make diagnostics easier.

## 4. Delivery roadmap

Order: **G0 -> G1 -> G2 -> G3 -> G4 -> G5**. Basic limits and measurements start
in G1, before broadening exposure. Performance tuning and HA follow measured use.
No calendar promises: effort estimates require each phase's scoped technical spec.

### G0 — Reconcile contracts and establish a measurable baseline

**Priority:** P0. **Status:** planning document delivered; runtime baseline open.

- Inventory public endpoints, identities, privileges, routing rules, health and
  capture behavior. Pin chat/speech exact-target semantics with regression tests.
- Record actual deployment trust boundaries, backend bypass paths, outstanding
  Caretaker watchdog/OOM-reporting handoffs, and current test failures separately.
- Reconcile the old README/architecture references and issue #1 before claiming
  documentation-wide freshness. Record baseline latency, throughput, queue depth,
  cancellation behavior and current capacity on reproducible workloads.
- Decide the first non-LLM/non-speech service: recommended default is one private,
  read-only HTTP API with no model or GPU dependency. Confirm target and operation
  contract before implementation; no unrelated project changes are authorized.

**Exit gate:** endpoint/policy matrix and threat model reviewed; current green
checks and known failures recorded; first service and smoke-test fixtures defined.
**Deployment:** none for docs; operator-run restart only if code is changed.

### G1 — Security and admission foundation

**Priority:** P0. **Depends on:** G0.

- Extend identities with explicit service, route and operation scopes; separate
  admin, lifecycle and capture-export permissions. Plan key rotation/revocation
  and optional OIDC/service identity without breaking named-key clients.
  **Verified priority gap:** `app/proxy/server.py` protects `/admin/load` and
  `POST /api/keys` with `verify_api_key`, without a distinct admin scope;
  `app/gateway/admin_api.py` creates keys without an additional role check.
  Authentication alone must not authorize key minting or lifecycle operations.
- Enforce upstream destination allowlists, outbound credential isolation, TLS
  verification and authenticated management calls. Evaluate mTLS where warranted.
  Explicitly migrate existing LAN management/speech hops that send Bearer keys
  over HTTP to verified TLS or a reviewed encrypted transport; a LAN firewall
  restriction is not encryption. The current working-tree remote-ensure path
  documents this legacy practice in `app/cloud_inference/forwarding.py`.
  Preserve the separate local Caretaker cleartext-key refusal during migration.
- Block SSRF through dynamic URLs, redirects, DNS rebinding and unapproved
  loopback/link-local/metadata destinations. Explicitly approved LAN targets
  remain usable; authorization must survive DNS and redirect resolution.
- Bound request/body sizes, queue depth, concurrency, timeouts and per-client
  request rates. Return stable 403/413/429/503 errors with appropriate retry hints.
- Publish versioned decision records and safe metadata audit. Do not enable
  payload capture for new service types implicitly; do not log auth headers.

**Exit gate:** negative tests cover denied scopes, credential/header leakage,
admin isolation, invalid upstreams/redirects, oversize requests, quota rejection
and policy/config error fail-closed behavior. Non-default configured limits are
exercised. Verify backend access is restricted from an ordinary client vantage.
**Deployment:** schema migration plus code; operator-run restart after gate.

### G2 — First general-purpose service vertical slice

**Priority:** P1. **Depends on:** G1.

- Add an explicitly registered HTTP service with allowed methods/paths, upstream
  auth, timeout/body limits, health contract and operation/retry classification.
- Reuse the core for auth -> policy -> admission -> forwarding -> audit. Preserve
  response status, content type and binary/streamed bodies unless an adapter
  explicitly translates them. Strip hop-by-hop and untrusted identity headers.
- Keep cancellation and backpressure correct; bounded buffers prevent arbitrary
  uploads/downloads from consuming unbounded memory. No caller-selected URL proxy.
- Publish service discovery filtered by caller permissions; preserve `/v1/models`
  as model discovery, not a generic service listing.

**Exit gate:** one real non-AI service is called through Guardian using only a
Guardian credential; unauthorized direct/mediated calls fail as designed.
Contract tests cover errors, binary content, streaming, timeout, client disconnect,
upstream credential isolation and denied methods/paths. Existing chat/speech pass.
**Deployment:** code plus one service registration; no repo/service rename.

### G3 — Capacity-aware routing and resilient service pools

**Priority:** P1. **Depends on:** G2 and compatibility contracts from G0.

- Introduce opt-in pools of equivalent service instances; select among healthy
  candidates by configured strategy (weighted round-robin first, then measured
  least-in-flight/queue-aware selection when useful).
- Separate per-instance health, service capability, backend warmup and caller
  errors; generalize the existing in-process circuit breaker, cooldowns and
  half-open probes into a bounded per-service contract.
- Apply per-pool concurrency and fair admission rather than one global queue for
  unrelated services. Integrate Caretaker readiness without moving lifecycle
  execution into Guardian. Report selected candidate and routing reason.
- Bound retries with deadline/backoff/jitter and honor upstream retry hints.
  Retry side-effecting operations only with a defined idempotency contract;
  never replay committed streaming output. Client cancellation is not failure.
- Add an administrative drain mode that rejects new work but lets active work
  finish within a configured budget; cancellation releases all capacity leases.

**Exit gate:** deterministic selection/fairness tests plus injected failure,
429, cold-start, open-circuit, disconnect and drain scenarios. Prove no duplicate
side effect, no mid-stream failover and no exact-target substitution. Measure
load distribution and p95 latency against G0, not only successful requests.
**Deployment:** code and pool schema; operator-run restart after gate.

### G4 — Operator control, cost and privacy governance

**Priority:** P1/P2. **Depends on:** G3; safe audit starts in G1.

- Dashboard: services, routes, pools, health, capacity, queue wait, latency,
  errors, policy denies and routing explanations; identifiers avoid payload PII.
- Versioned config validation, dry-run, atomic reload and rollback; retain last
  known-good config on invalid reload. Audit control-plane actions.
- Per-project/client usage and configurable budgets where usage can be measured;
  distinguish estimates, reservations and reconciled upstream cost. Do not
  promise exact spending ceilings without atomic admission and accounting.
- Privacy/residency routing, retention, capture/export permissions and explicit
  cache policy. No shared response cache for private data without tenant-safe
  keys and operation semantics. Preserve intentional current raw-capture policy
  until a separate approved migration changes it.
- Trace/request IDs, route-specific SLOs, alerting and runbooks. Bound metric
  cardinality; secret-bearing request bodies are not metric labels.

**Exit gate:** an operator can explain an injected incident from metadata alone;
invalid reload preserves behavior; cross-client access/cache tests fail closed;
budget race/reconciliation tests and capture/retention compatibility pass.
**Deployment:** backend and dashboard artifacts plus controlled config migration.

### G5 — Tools, protocols and multi-instance operations

**Priority:** P2. **Depends on:** G4; split into independently reviewed milestones.

- Tool/MCP mediation: registered servers, operation scopes, credential isolation,
  session ownership, read/write/destructive classifications and explicit approval
  contracts. Proxying LLM tool-call JSON is not executing or authorizing tools.
- WebSocket/session-aware protocols only for named use cases; define session
  affinity, disconnect, idle timeout and bounded message sizes first.
- Multi-replica Guardian: define shared quota/accounting consistency, queue and
  cancellation ownership, health convergence and capture behavior. A second
  process alone is not HA. Graceful shutdown, rollback and disaster recovery.
- Reuse suitable ingress/service discovery systems; introduce shared storage or
  a distributed broker only when the consistency requirement justifies it.

**Exit gate:** tenant/session isolation and hostile-tool tests; replica-loss
experiments prove documented behavior without duplicated jobs, exceeded shared
quotas or unaudited fail-open routes. Recovery and rollback rehearsed.
**Deployment:** explicit topology change and operator-run rollout; no automatic
production expansion or tool execution from this roadmap alone.

## 5. First implementation backlog

| ID | Deliverable | Priority | Dependency | Completion evidence |
| --- | --- | --- | --- | --- |
| G0-01 | Current endpoint/auth/route matrix | P0 | None | Code-linked matrix and regression-test inventory |
| G0-02 | Trust-boundary and bypass threat model | P0 | G0-01 | Approved destinations, client/admin roles and failure modes |
| G0-03 | Baseline and existing failure triage | P0 | G0-01 | Reproducible measurements; known failures separately attributed |
| G0-04 | First general HTTP service contract | P0 | G0-02 | Selected service, methods/paths, read-only fixture and owner |
| G1-01 | Service/operation authorization and admin isolation | P0 | G0-02 | Positive/negative compatibility and privilege tests |
| G1-02 | Destination/credential/header enforcement | P0 | G0-02 | SSRF, redirect, DNS and credential-leak tests |
| G1-03 | Bounded admission and metadata audit | P0 | G0-03 | Non-default limit tests and audit-without-payload demonstration |
| G2-01 | Registered HTTP adapter vertical slice | P1 | G0-04, G1 exit | Real non-AI call and existing API regression suite |
| G3-01 | Explicit pools and weighted selection | P1 | G2 exit | Distribution/compatibility tests and measured comparison |

## 6. Risks, exclusions and decision gates

- **Central bottleneck/blast radius:** measure overhead and isolate control-plane
  work; drain/recovery first, replicas only with coherent state ownership.
- **False security confidence:** authentication is not authorization; TLS is not
  network isolation; request content and model output are untrusted data.
  Optional prompt/content filtering is defense-in-depth, not a security boundary.
- **Unsafe retries and fallback:** duplicated writes, paid inference, changed
  models or stream corruption are worse than an honest failure.
- **Sensitive capture:** raw prompts, audio and tool payloads can contain secrets.
  Keep capture distinct from metadata audit and downstream Keanu processing.
- **Over-generalization:** no arbitrary URL forwarding, universal TCP/UDP proxy,
  whole service mesh, workflow/agent execution engine, or built-in dataset builder.
- **Naming:** Guardian is the product name. Keep current repository/package,
  service aliases, ports and public routes until a dedicated migration is justified.
- **Open decisions:** first HTTP target; identity/OIDC needs; residency rules;
  acceptable audit-loss policy; measured SLOs; pool equivalence definition;
  multi-replica consistency/storage. Resolve at their phase gate, not by silently
  adding defaults or changing existing behavior.

## 7. Definition of done and roadmap maintenance

Each implementation milestone must provide a scoped spec, configuration and
compatibility migration, positive/negative tests, measured operational evidence,
rollback/restart impact and updated docs/file register. Run relevant compile and
pytest checks; every production restart requires `scripts/pre_restart_check.py`
and is performed by the operator outside the agent session.

Track **planned / in progress / verified / deployed** separately. Record evidence
and unresolved findings in `AGENT_JOURNAL.md` or the declared handoff; preserve
replaced history verbatim before revising active status. Keep this roadmap as the
product source of truth, the historical plan as migration history, and GitHub
issues as scoped execution work. Do not mark a phase complete because this file
exists, a feature has a name, or a happy-path mock returned 200.
