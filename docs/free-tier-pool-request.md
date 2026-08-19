# Free-Tier Pool — verification request from the Keanu session (2026-08-19)

Status: REQUEST — read-only verification + report only. **Do not implement
anything yet.** Keanu is building a free-tier pool (Keanu-side controller,
Guardian stays the delivery/record layer). Before any change, the exact
constraints below must be verified against the live service. Answer in
writing; relay the reply to the Keanu agent via the operator.

## Context

- Roles: **Guardian delivers** (local + cloud LLM routes, holds all keys,
  capture → WAL → dataset), **Keanu processes** (data factory: distill,
  quality gates, pool/pacing, judge — all via Guardian's OpenAI-compatible
  endpoint, all traffic stays in Guardian so everything is captured).
- Goal: €0 model fleet by using free-tier entries already present in
  `config/cloud_keys.json` (nvidia, openrouter `:free`, google) — quality
  comes first; we only decide on code changes after seeing probe results.

## Proposed config (for review; NOT applied)

`config/cloud_keys.json` — add top-level `failover_groups`:

```json
"failover_groups": {
  "minimax-m3": {
    "candidates": [
      {"provider": "nvidia",     "model": "minimaxai/minimax-m3"},
      {"provider": "openrouter", "model": "minimax/minimax-m3:free"}
    ]
  },
  "laguna": {
    "candidates": [
      {"provider": "openrouter", "model": "poolside/laguna-s-2.1:free"},
      {"provider": "openrouter", "model": "poolside/laguna-xs-2.1:free"}
    ]
  },
  "gemini-flash": {
    "candidates": [
      {"provider": "google", "model": "gemini-2.5-flash"}
    ]
  },
  "kimi-k3": {
    "candidates": [
      {"provider": "openrouter", "model": "moonshotai/kimi-k3"}
    ]
  }
}
```

Plus: add `"minimax/minimax-m3:free"` to the openrouter credential `models` list
(it is currently absent — only the paid listing exists).

## Verification checklist (live, read-only)

1. **Model existence**: does `minimax/minimax-m3:free` exist on OpenRouter
   (`GET /api/v1/models` filter for `:free`, and `minimax/`)? If not, what is
   the best genuinely-free OpenRouter fallback for the same weight class?
2. **NVIDIA free endpoint**: is `integrate.api.nvidia.com/v1` the correct host
   for free-tier NIM (`minimaxai/minimax-m3` is a build.nvidia.com card) with
   the linked nvidia credential? Sampler one live chat completion (tiny).
3. **Failover schema correctness**: does `FailoverCandidate` config as above
   (provider name, model) load cleanly via `FailoverRegistry.reload` today,
   and does a `guardian/failover/minimax-m3` request resolve + which
   provider ultimately served (`model@provider` suffix) + how long the
   request took?
4. **Key linkage**: which bearer keys (names only) currently have links to
   nvidia AND openrouter AND google credentials? (The Keanu key needs all
   three for the groups above.)
5. **Capture**: with `cloud_capture` currently `false`, cloud routes are not
   captured. Confirm the exact minimal change (`settings.yaml` → `capture:
   cloud_capture: true` + `cloud_model_prefixes` add `"guardian/"`) and
   whether `allowed_cloud_models`/`per_client_opt_in` impose anything else
   for `guardian/failover/...` routes. NOTE: settings.yaml changes need a
   restart, which cuts your own model traffic — coordinate with the operator
   (their AGENTS.md restart protocol).
6. **429/Retry-After visibility**: for OpenRouter `:free` today, what happens
   on quota-exhausted 429 — headers (`retry-after`, `x-ratelimit-*`) and the
   current `cloud_retry.enabled=false` behavior? We need this to size the
   Keanu-side per-route daily budgets (Keanu will own pacing/budgets; you do
   NOT need to add budget code).
7. **Code-extension need (report only, no implementation)**: what would the
   smallest Guardian code change be (in which files/functions) IF we later
   want per-credential daily budgets / "prefer free" ordering Native to a
   group, per the earlier recon sketch (failover.py candidates + ratelimit.py
   budget counter + routing.py skip)? One paragraph, file:line pointers.

## Output

A short written report answering 1–7 with concrete numbers (model ids, RTT,
headers, which keys link what), plus a verdict: does the failover-group path
above work today (config-only), or does something need a code fix first?
Keep the report terse; no secrets; masked keys only.