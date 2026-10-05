# Unified Provider Model — no local/cloud class distinction

Status: design accepted by the operator (2026-10-04); implementation in phases
A + B via a PR series. This document is the contract for that work.

## Problem

Guardian's code distinguishes "local" from "cloud" providers as a *class*
(`local: true`, `managed: true`, the `-local` name-suffix heuristic). That
class conflates three independent concerns:

1. **Where the backend runs** (this host, LAN, internet) — irrelevant to
   Guardian's intermediary role; the roadmap already states this.
2. **What protocol the backend speaks** — a native llama.cpp server (grammar
   passthrough, `/props`, model switch) vs a generic OpenAI-compatible
   endpoint.
3. **Who owns the backend lifecycle** — Guardian (caretaker `/ensure`,
   auto-switch) or the operator.

The local llama-server is not a different *kind* of provider; it is a provider
whose backend happens to run on this box and whose lifecycle Guardian manages.
The managing application (caretaker) can equally run on a LAN host (Windows
NSSM, F6) or anywhere on the internet.

## Target model

Every provider is uniform: `{provider}/{brand}/{model}` addressable,
discoverable, health-tracked, failover-capable. Provider configuration carries
two capability attributes instead of a location class:

| Attribute | Values | Gates |
| --- | --- | --- |
| `backend` | `llama-server` \| `http` | Routing fork: native passthrough (grammar/GBNF, `response_format`, `/props`, SSE watchdog) vs generic OpenAI-compatible forwarding (failover, rate limiting, translation). |
| `lifecycle` | `guardian` \| `operator` | Backend lifecycle ownership: caretaker `/ensure` + auto-switch vs traffic-only. |

Location is not an attribute. `base_url` already expresses it
(`127.0.0.1:11440`, `192.168.1.245:11440`, `https://api.openrouter.ai/...`).

### Derived compatibility flag

`managed` stays as a *derived* property during migration:

```python
managed == (backend == "llama-server" and lifecycle == "guardian")
```

All internal `managed` reads migrate to the two attributes; the derived flag
keeps third-party/legacy call sites honest until phase B completes.

### Addressing

Local models gain namespaced addresses alongside their bare aliases (which are
preserved — existing aliases are never removed):

```
qwen3.8-27b                          # bare alias (compat, unchanged)
ai-kvm2-local/qwen/qwen3.8-27b       # namespaced form (new, additive)
```

The `brand` segment is the model-maker family (per-model `brand:` field in the
local registry: qwen, gemma, phi, llama, granite, nomic, …). Bare names
continue to route local by default (no provider segment → no cloud
recognition).

## Dispatch map (phase-B change points)

Current `managed`/local gate locations and their target attribute:

| Location | Today | Becomes |
| --- | --- | --- |
| `app/cloud_inference/__init__.py:140` (`is_cloud_or_guardian_route`) | `provider.managed` → local fork | `provider.backend == "llama-server" and provider.lifecycle == "guardian"` → native path; else forward path |
| `app/proxy/providers.py` (parse) | `local:`/`managed:`/`-local` suffix | `backend:`/`lifecycle:` keys; legacy keys map with deprecation warnings; suffix heuristic retired |
| `app/proxy/providers.py` (`is_configured`) | managed → keyless OK | `backend == "llama-server" and base_url is loopback/private` → keyless OK (native backends carry no upstream credential) |
| `app/gateway/model_discovery.py:222,289,344` | managed exclusion + bare advertisement | attribute-driven; namespaced local entries advertised additively |
| `app/gateway/model_endpoints.py:380,847,857,876,893,948,952` | route metadata split | same attribute conditions |
| `app/cloud_inference/forwarding.py:542-571` (failover ensure) | `managed` → local caretaker; `management_url` → remote ensure | already capability-shaped: `lifecycle == "guardian"` → local caretaker; `management_url` present → remote ensure — independent of location |
| `app/engine/manager.py:584` | managed llama-server acceptance | `lifecycle == "guardian" and backend == "llama-server"` |

## Phases

### Phase A — namespaced local addresses (additive)

1. Per-model `brand:` in the local registry documents (`ai-kvm2-local`,
   `ai-node-local`); inventory of all model entries with maker-family brands
   (done 2026-10-04: `docs/archive/brand-inventory-draft-2026-10-04.md` —
   25 model entries across qwen/gemma/nomic/llama/phi/granite, aliases
   inherit the target brand, one flagged name (`qwen3.8-27b-swift15`) is a
   fine-tune over Qwen3.8-27B, not a "swift" brand).
2. Discovery advertises `{local-provider}/{brand}/{model}` **alongside** the
   bare alias — same metadata, context, capabilities. The address prefix is
   the managed provider that owns the local registry document
   (`local_models_file()` stem, currently `ai-node-local`); generous input:
   any managed provider prefix resolves.
3. Local resolution accepts the namespaced form: strict brand match, strip to
   the registry entry, route through the native path exactly like the bare
   alias.
4. `is_cloud_model` must never claim a managed provider's namespaced form
   (already safe via `_provider_from_address` → managed → not cloud; pinned by
   tests).
5. No client-visible breaking change: bare aliases keep working everywhere.

Registry hygiene found during phase A (to consolidate in phase B): the two
local documents are near-identical drifted copies of the same backend registry
(`ai-node-local` = the app-read canonical; `ai-kvm2-local` = referenced by
`global.settings.yaml` failover groups and `tts.providers`); deltas: only
`ai-node-local` has `qwen3.8-27b-swift15`, only `ai-kvm2-local` has
`Huihui-Qwen3.5-9B-abliterated`. Phase B collapses them to one source of
truth.

### Phase B — attributes replace the class

1. Parse `backend:`/`lifecycle:`; legacy `local:`/`managed:`/`-local`-suffix
   map onto them with deprecation warnings (one release cycle).
2. Replace the dispatch-map gates above; `managed` becomes derived.
3. Config migration: `ai-kvm2-local`/`ai-node-local` →
   `backend: llama-server` + `lifecycle: guardian`; `windows-gpu-local`/
   `14700k-local` → `backend: llama-server` + `lifecycle: operator`.
4. Behavior preserved for both hosts: the local box stays on the native path;
   the LAN hosts stay on the forward path with remote ensure.
5. Suffix heuristic (`is_local_provider_name`) retired; name no longer implies
   a class.

### Phase C (later, own trajectory)

Failover groups treat every provider as a peer candidate (GPU hosts included)
and admission policy becomes an attribute (GPU-slot-per-key tied to
`backend: llama-server`), not a path property.

## Compatibility guarantees

- Bare local aliases remain valid indefinitely (naming contract).
- Existing clients see only additive discovery entries.
- Grammar/GBNF passthrough stays native-path-only; forwarding keeps stripping
  grammar for generic endpoints.
- Failover candidates keep the ensure-before-forward contract (no silent
  wrong-model serving).
