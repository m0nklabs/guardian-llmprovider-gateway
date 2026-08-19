# Free-Tier Pool — verification report (2026-08-19)

Read-only verification of `docs/free-tier-pool-request.md`. No config/code
changes were applied during verification. Answers 1–7 with live numbers.

---

## 1. Model existence — `minimax/minimax-m3:free` on OpenRouter?

**Does NOT exist** (operator confirms: the OpenRouter guardrail list has no
`minimax/…:free` entry; independently verified live on the public catalog):
`minimax/` models served are `minimax-m3`, `minimax-m3:batch`, `m2.7`, `m2.5`,
`m2-her`, `m2.1`, `m2`, `m1`, `minimax-01` — all paid. Pricing for
`minimax/minimax-m3`: prompt `0.0000003` $/tok, completion `0.0000012` $/tok.

**Operator-approved replacement (2026-08-19, applied to the *proposal* only —
nothing written to live config):**

- **`minimax-m3` group → NVIDIA NIM only**: `minimaxai/minimax-m3` (free card,
  catalog-verified). No OpenRouter mirror for this model.
- **OpenRouter `:free` groups (separate groups, per model family):**
  - `poolside/laguna-s-2.1:free`
  - `poolside/laguna-xs-2.1:free`
  - `nvidia/nemotron-3-nano-30b-a3b:free` (+ `nemotron-3-super-120b-a12b:free`,
    + `nemotron-3-nano-omni-30b-a3b-reasoning:free` if it exists — verify;
    the `-omni` variant is in the catalog but availability must be probed live)
- **Google Gemini runs via the direct `google` credential only** (per-key via
  `guardian/google/…`), never through an OpenRouter group.

Full verified `:free` set from the live catalog (no change vs earlier):
`cohere/north-mini-code:free`, `dots-studio/dots-3-note-preview:free`,
`google/gemma-4-26b-a4b-it:free`, `google/gemma-4-31b-it:free`,
`liquid/lfm-2.5-2.6b:free`, `nvidia/nemotron-3-nano-30b-a3b:free`,
`nemotron-3-nano-omni-30b-a3b-reasoning:free`, `nemotron-3-super-120b-a12b:free`,
`nemotron-3-ultra-550b-a55b:free`, `nemotron-3.5-content-safety:free`,
`nemotron-3.5-lightning:free`, `nemotron-nano-12b-v2-vl:free`,
`nemotron-nano-9b-v2:free`, `openai/gpt-oss-20b:free`,
`poolside/laguna-s-2.1:free`, `poolside/laguna-xs-2.1:free`, `z-ai/glm-5.2:free`.

## 2. NVIDIA free endpoint

- Host `https://integrate.api.nvidia.com/v1` is correct; catalog reachable
  (`GET /v1/models` → 200, ~0.1 s TTFB, 102 models). Entries relevant to the
  proposal: `minimaxai/minimax-m3` ✅, `z-ai/glm-5.2` ✅, `poolside/laguna-xs-2.1` ✅.
- **Live chat probe on `minimaxai/minimax-m3` (max_tokens=8, "Say OK"):**
  1. curl 600 s window → killed (no response, HTTP 000)
  2. curl 240 s window → HTTP 000 (connect OK to catalog, chat never answers)
  3. httpx 600 s window → **HTTP 504 after 302.3 s** (upstream gateway timeout)
- Control probe `nvidia/llama-3.1-nemotron-70b-instruct` → **404
  "Function … Not found for account …"** in 0.2 s → this NVIDIA account does not
  have every catalog model *activated*; free-tier availability is
  per-account/card, not per-catalog.

**Verdict (p2):** host & catalog correct; the free `minimaxai/minimax-m3`
card never answered a live request today (504/timeout). Treat NVIDIA minimax-m3
free as **degraded/unavailable** until a successful live inference is observed.

## 3. Failover schema correctness

- `FailoverRegistry` already parses a top-level `failover_groups` key from
  `config/cloud_keys.json` (`app/proxy/failover.py:248` `reload()`), including
  per-candidate `modalities` with a `("text",)` default.
- **Verified offline** against a temp file with the exact proposed JSON: all 4
  groups load cleanly — `minimax-m3`, `laguna`, `gemini-flash`, `kimi-k3`;
  candidates parse to `(provider, model)` and the registry logs
  "Loaded 4 failover group(s)". No schema error.
- **BUT** the live `config/cloud_keys.json` has **no `failover_groups` key**
  (only `credentials` + `links` + `model_defaults`) → a live
  `guardian/failover/minimax-m3` request today returns `404
  failover_group_not_found` (`app/cloud_inference/routing.py:111-119`).
- When the group is added (config-only) + credential reloaded, resolution goes
  through `failover_health.order_candidates` (healthy → rate-limited → tripped)
  and per-candidate per-key credential lookup (`routing.py:121-139`).
- Response shape: after a failover hit, the upstream model reports back as
  `{model_name}@{provider_name}` (`forwarding.py:304/583`), so usage logs show
  exactly which provider served.
- No RTT measured: requires writing the proposed config into the live file,
  which is a change (out of scope for this read-only pass). Schema → code is
  **ready, config-only**.

## 4. Key linkage (which keys link nvidia + openrouter + google)

Computed from `config/api_keys.json` vs `cloud_keys.json` `links`, and
verified live via `/api/cloud/links`:

| Key name | Fingerprint (sha256[:12]) | nvidia+openrouter+google |
|---|---|---|
| `claudekvm2` | `17aa6e789057` | ✅ |
| `goose` | `286cf1d8b6fc` | ✅ |
| `open-webui` | `3c423d5461bc` | ✅ |
| `hermes` | `94062b64e5d5` | ✅ |
| `pi` | `c1824126c6fb` | ✅ |
| `openai` | `59ede1411577` | ❌ (openai only) |
| **`keanu-factory`** | `7e573421cf2a` | ❌ **linked nowhere** |

Live proof that linked keys work end-to-end: `hermes` key →
`guardian/openrouter/moonshotai/kimi-k3` → **200** (chat completed, ~7 s).
Keanu should use one of the five linked keys (values live in
`config/api_keys.json`; operator hands the raw token to Keanu — do not print).

## 5. Capture — minimal change for cloud + `guardian/failover/…` capture

Current `config/settings.yaml` `capture:`:
`enabled: true`, `local_capture: true`, **`cloud_capture: false`**,
`cloud_allowlist_enabled: true`, `allowed_cloud_models: []`,
`cloud_model_prefixes: [openai/, anthropic/, google/, meta-llama/, deepseek/,
qwen/, mistralai/, z-ai/, minimax/, poolside/, moonshotai/, nvidia/]`,
`per_client_opt_in: true`, `allowed_client_refs: <33 refs>`.

Exact minimal change for `guardian/failover/*` (and `guardian/*` cloud routes):
1. `capture: cloud_capture: true`
2. add `"guardian/"` to `capture: cloud_model_prefixes`
3. nothing else: `allowed_cloud_models` can stay empty (prefix matching wins —
   `_matches_cloud_model`, `app/capture/policy.py:80-94`); `allowed_client_refs`
   already covers Keanu (HMAC of the keanu key is present in the 33-ref list,
   verified with the current `GUARDIAN_CAPTURE_CLIENT_REF_SECRET`);
   `per_client_opt_in: true` then captures only known client refs as intended.

Current blocker: a settings.yaml edit needs a restart (see op request — the
config-reload fix resolves this; until then changes only become active after a
coordinated restart; restart severs the agent's own model traffic (AGENTS.md
protocol).

## 6. 429 / Retry/After visibility on OpenRouter `:free`

- Live probes on 2026-08-19 (both `:free` poolside/laguna and paid kimi):
  OpenRouter responses carry **no `x-ratelimit-*` headers** — only
  `x-generation-id`, `cf-ray`, `set-cookie`. Rate-limit headers therefore
  cannot be sized from healthy responses; `Retry-After`/`x-ratelimit-*` appear
  only on the 429 response itself.
- Guardian behavior with current `cloud_retry.enabled=false`
  (`RateLimitRetryManager.config.enabled=False`):
  - `execute_with_retry()` becomes a pass-through — **no in-Guardian 429
    waits** (`app/proxy/ratelimit.py:357-358`);
  - failover 429 probe (60 s sleep) is gated on `config.enabled` as well →
    skipped (`app/cloud_inference/forwarding.py:501-521`);
  - a 429 on a **failover group** continues to the next candidate
    (`is_retryable_cloud_error(429)=True`);
  - a 429 on a **non-failover cloud route** is passed straight back to the
    client with sanitised headers — the client (Keanu/agent) owns
    backoff/retry (this is the 2026-08-12 deliberate design).
- For Keanu's budgets: it must count 429s + consume `Retry-After` itself.
  No extra Guardian code required for that (pacing stays Keanu-side).

## 7. Code-extension need (report only) — budgets / prefer-free

Smallest extension, 3 touch points (all local, no new endpoint):
1. `app/proxy/failover.py`
   - `FailoverCandidate` (line ~71): add `free: bool = False` +
   `daily_budget: Optional[int] = None` to the dataclass; parse from
   `candidates[].free / .daily_budget` in `reload()`.
   - `ProviderHealthTracker.order_candidates()` (line ~215): after health
   ordering, split healthy → `preferred`-first (healthy+free then healthy+paid)
   for "prefer free".
2. `app/proxy/ratelimit.py`
   - `_RateLimitState` (line ~88) + `RateLimitRetryManager` (line ~110):
     add a per `(key_fingerprint, provider, model)` rolling daily counter
     (`spent_daily`) + cap; expose `get_stats()` fields
     (`remaining_daily`, `reset_at`).
3. `app/cloud_inference/routing.py`
   - `resolve_cloud_attempts()` line ~121 f loop: after the credential check,
     `if cand.daily_budget and spent_daily >= budget: continue` (skip
     exhausted) — together with `forwarding.py` call sites that call
     `record_usage` to increment the counter.

Estimated delta: ~40-60 new lines total; purely config-driven after
implementation (groups stay in `cloud_keys.json`). Not implemented in this
session (per request).

---

## Verdict

- Failover-group **schema + route resolution already exist** (`FailoverRegistry`
  load verified; route verified to 403/404 cleanly today because the key/config
  are absent). Once `failover_groups` is in `cloud_keys.json` **and reloaded**,
  the `guardian/failover/{group}` path is **config-only — no code change**.
- The proposal as written would fail **on the `minimax-m3` group only**:
  `minimax/minimax-m3:free` does not exist on OpenRouter; the operator-approved
  replacement (§1) keeps MiniMax on NVIDIA NIM and splits the OpenRouter free
  models into separate groups. All other proposal points are config-valid.
- **Nothing is being implemented in live config until Keanu has run the
  probe** (operator instruction, 2026-08-19): no `failover_groups` in
  `cloud_keys.json`, no `capture.cloud_capture` flip, no model-list edits.
  The only code change in this session is the **no-restart config-reload
  infrastructure** (`POST /api/config/reload`), which is what will later apply
  the pool config without a Guardian restart.
- Item (c) alone explains the live Keanu-probe 403s: the `keanu-factory` key
  has **no credential links** — until one of the five linked keys is used (or
  the keanu key is linked) Keanu gets `403 cloud_credential_not_linked` on
  *every* cloud route (§4). **First fix, smallest:** point Keanu at one of the
  five linked keys (`hermes`/`goose`/`pi`/`claudekvm2`/`open-webui`) — the
  key-linking of the keanu-factory key itself is scheduled with the owner-scope
  repair (see the key-linking status in the operator notes, 2026-08-19).