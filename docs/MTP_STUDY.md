# MTP / Speculative-Decoding Study — Qwen Family (2026-08-15)

Benchmark session `20260815_bench` follow-up. Code-fix: commit `c67845b` (spec_type
without `draft_model_path`). All measurements via Guardian `/v1/chat/completions`
(streaming), bench script `scripts/bench_all_models.py`, single GPU pair
(3060 + 5060 Ti), model-switch queue, 3 runs per model, median gen t/s.

---

## 1. Setup — the three speculative-decoding methods

"Multi-Token Prediction (MTP)" is ambiguous in practice; this study covers all
three distinct mechanisms that llama-server exposes via `--spec-type`:

| Method | `--spec-type` | Draft source | Needs MTP layers? | Needs draft model? | Status |
|---|---|---|---|---|---|
| Native MTP | `draft-mtp` | MTP heads baked into the model architecture (DeepSeek-V3 style) | **yes** | no | ✅ works on qwen3.8 |
| DFlash | `draft-dflash` | external draft GGUF | no | **yes** | ❌ broken in current binary (b10555) |
| N-gram lookup | `ngram-simple` | lookup tables built from prompt/context | no | no | ✅ works on any model |

- **Native MTP is a Qwen3.8-only feature** in the local model set (verified by lead
  pre-work, re-confirmed in the journal during this bench):
  | Model | Arch | `--spec-type draft-mtp` | verdict |
  |---|---|---|---|
  | `qwen3.8-27b` | qwen3 | works — log line `creating MTP draft context against the target model` | ✅ |
  | `qwen3.6-35b` | qwen3 | fails — `[spec] failed to measure MTP context memory` → no MTP layers | ❌ |
  | `qwen3.5-9b` | qwen35 | fails — `model doesn't contain MTP layers` | ❌ |
- **DFlash is broken** in the current llama-server binary: the existing
  `Qwen3.6-…-DFlash-Turbo4` entry measures **15.77 t/s vs 81.48 t/s base = 5.2×
  SLOWER** (draft-model overhead with no acceptance benefit). Operator rule:
  "niet met dflash testen" — not part of this study's measurements.
- **N-gram lookup is the fallback** for models without MTP layers (qwen3.6, qwen3.5):
  no draft model, no architectural requirement, but its hits depend on the output
  repeating text from the prompt/context.
- **Code-fix (commit `c67845b`, `app/engine/manager.py` `_build_args_string`):**
  previously `--spec-type` was only emitted inside the `if draft_model_path:`
  block, so `draft-mtp` and `ngram-*` could not be used at all. The fix emits
  `--spec-type <type>` **without** `--model-draft` for these no-draft modes
  (placed before `extra_args` so user flags cannot override it), preserves the
  `draft-dflash` + draft-model path byte-identical, and warns on
  `draft-dflash` without a draft model. Launch-signature drift automatically
  reloads a model when `spec_type` changes.

Bench variants (all with `context` −20k from base per the operator VRAM rule;
samplers identical to their base entry — see confounds below):

| variant | spec_type | base model |
|---|---|---|
| `qwen3.8-27b-mtp` | `draft-mtp` | `qwen3.8-27b` (thinking, ctx 262144→242144) |
| `qwen3.8-27b-instruct-mtp` | `draft-mtp` | `qwen3.8-27b-instruct` (ctx 262144→242144) |
| `qwen3.6-35b-fast-ngram` | `ngram-simple` | `Qwen3.6-35B-A3B-HauhauCS-Aggressive-Turbo4` (ctx 262144→242144) |
| `qwen3.5-9b-ngram` | `ngram-simple` | `qwen3.5-9b` (ctx 262144→242144) |

---

## 2. Per-variant bench results

Bench prompt (fixed, one-shot, identical for all variants + bases):
*"Explain in three sentences how a lighthouse keeper would log tidal patterns,
lantern rotations, and shipping lanes in a coastal ledger. Be concise and
practical."*

| variant | spec_type | base model | base t/s | variant t/s | speedup | load (s) | verdict |
|---|---|---:|---:|---:|---:|---:|---|
| `qwen3.8-27b-mtp` | draft-mtp | qwen3.8-27b | 63.4 | **115.21** | **1.82×** | 28.3 (base 15.5) | **SPEEDUP** |
| `qwen3.8-27b-instruct-mtp` | draft-mtp | qwen3.8-27b-instruct | 17.64 | **18.83** | **1.07×** | 25.1 (base 13.9) | **SPEEDUP** |
| `qwen3.6-35b-fast-ngram` | ngram-simple | Qwen3.6-…-Turbo4 | 81.48 | 78.17 | 0.96× | 17.7 (base 13.3) | **NEUTRAL** |
| `qwen3.5-9b-ngram` | ngram-simple | qwen3.5-9b | 26.81 | 29.44 | 1.10× | 14.7 (base 12.8) | **SPEEDUP*** |

\* qwen3.5-9b-ngram is **confounded**: the variant also changed
`--presence-penalty 1.5 → 0.0` (per the phase-1 YAML spec, cloned from the
qwen3.8 thinking sampler set). Part of the gain may be sampler-driven, not ngram.

Thresholds: SPEEDUP >1.05× · NEUTRAL 0.95–1.05× · REGRESSION <0.95× · FAILED (OOM/load error).
No variant OOM'd or failed to load; `--spec-type` was accepted by llama-server on
all four (verified in the journal launch lines).

### Speculative acceptance stats (from `journalctl -u llama-server`)

| variant | log evidence | draft acceptance | mean draft len |
|---|---|---|---|
| `qwen3.8-27b-mtp` | `creating MTP draft context against the target model` ✅ | **0.65** (165/252) — warmup 0.92 (22/24) | 2.96 |
| `qwen3.8-27b-instruct-mtp` | `creating MTP draft context…` ✅ | **0.34** (44/129) — warmup 0.29 (14/49) | 2.02 |
| `qwen3.6-35b-fast-ngram` | no MTP line (expected — arch has no MTP heads) | **0.23** (19/82) | 10.50 |
| `qwen3.5-9b-ngram` | no MTP line (expected) | **no acceptance lines in journal** — llama-server printed none for this process | — |

---

## 3. Analysis — why each variant behaved the way it did

### qwen3.8-27b-mtp (thinking) — 1.82× — the headline result

The naive hypothesis ("reasoning tokens are unpredictable → low MTP acceptance")
is **refuted** by the journal: acceptance **0.65**, mean draft len **2.96** — each
verification step consumes ~3 tokens drafted in parallel by the MTP head. Two
reasons it works this well here:
1. The thinking entry's samplers (temp 1.0, top-p 0.95, min-p 0.0, pp 0.0,
   rp 1.0) sit very close to the model's training distribution, which is what the
   MTP head is trained to predict.
2. Reasoning traces are *formulaic* ("Wait,", "Therefore,", short arithmetic-ish
   bursts) — exactly the kind of token sequences an MTP head predicts well.

Costs: load time +13 s (28.3 vs 15.5 s, MTP draft-context build) and the extra
VRAM for the MTP draft context (hence the −20k ctx rule). At 115 t/s the payoff
dominates both.

### qwen3.8-27b-instruct-mtp — 1.07× — sampler drift kills acceptance

Acceptance drops to **0.34** (mean len 2.02). The instruct entry uses
`--presence-penalty 1.5` and `top-p 0.8` — the sampled distribution is pushed
away from what the MTP head predicts, so ~2/3 of drafts are rejected and the
verification overhead nearly cancels the parallel gain. It is still marginally
faster (parallel verification wins even at 34%), but the gap between the two
qwen3.8 variants (0.65 vs 0.34 acceptance on the same architecture) is a strong
signal that **samplers, not the model, drive MTP value**.

### qwen3.6-35b-fast-ngram — 0.96× — prompt repetition is the whole game

No MTP layers → ngram-simple falls back to lookup tables. The journal shows the
typical ngram signature: **long drafts (mean len 10.5) but low acceptance (0.23)**.
The bench prompt is a fixed one-shot question; the generated answer barely repeats
phrases from the prompt, so most lookup hits are spurious and rejected. The verify
cost of ~10 drafted tokens to accept ~2 ≈ the drafting savings → neutral. On
repetition-heavy workloads (agentic loops, long context with repeated instructions
or tool schemas that the output echoes) ngram hits would rise and the method
should win; this bench cannot demonstrate that.

### qwen3.5-9b-ngram — 1.10× — small gain, confounded, no acceptance data

10 % faster than base, but the variant differs from base in TWO ways (ngram +
presence-penalty 1.5→0.0), and llama-server logged **no acceptance lines** for
this process, so the mechanism cannot be verified. The gain direction is
consistent with the qwen3.6 result (mild, repetition-dependent) plus a possible
sampler effect; treat as "likely real but unproven".

---

## 4. Recommendations per model family

| Family | Method | Verdict | Recommendation |
|---|---|---|---|
| **qwen3.8-27b (thinking)** | `draft-mtp` | **1.82×** | **Enable as the default** for the thinking entry. Pure speed win, same model/samplers. Keep the −20k ctx headroom (MTP draft context costs VRAM) and accept +13 s load time. |
| **qwen3.8-27b (instruct)** | `draft-mtp` | 1.07× | Optional. Safe to enable (still faster), but the low acceptance (0.34) is sampler-driven: if per-token speed matters more than the pp-1.5 output character, lower presence-penalty; otherwise keep pp 1.5 and take the ~7 %. |
| **qwen3.6-35b** | `ngram-simple` | 0.96× | **Keep OFF by default** (neutral on one-shot traffic). Candidate for per-session enablement on repetition-heavy/agentic workloads. |
| **qwen3.5-9b** | `ngram-simple` | 1.10× (confounded) | Same as qwen3.6: harmless, mildly positive on this prompt, no acceptance evidence. Off by default; enable per-session if repetition-heavy. |
| **Any** | `draft-dflash` | 5.2× slower | **Do not use** until the llama-server binary's DFlash path is fixed (measurement: 15.77 t/s vs 81.48 t/s base). |

Operator action if enabled: re-add the desired variant entry to
`config/models.yaml` (the temporary variants were removed after this study — see
§6) with `spec_type: draft-mtp` / `spec_type: ngram-simple`, keep
`context = base − 20000`, and restart Guardian.

---

## 5. Honest unknowns

- **Single fixed bench prompt.** Acceptance rates and speedups are workload-
  dependent; a reasoning-heavy or repetition-heavy workload may shift every
  verdict. Only a long-lived soak on real traffic would confirm the gains.
- **qwen3.5-9b-ngram acceptance numbers are missing** — llama-server printed no
  `draft acceptance` lines for that process (it did for qwen3.6), so the 1.10×
  mechanism is unverified, and the variant was confounded by a sampler change.
- **MTP output-quality equivalence was not measured.** MTP changes only speed
  (same model weights, greedy verification), but token-level output can differ
  from non-speculative decoding in edge cases; no side-by-side text comparison
  was done.
- **VRAM cost of the MTP draft context is inferred**, not measured (the −20k ctx
  rule is the operator's heuristic from earlier MTP bring-up work).
- **Baselines are from earlier bench sessions** (same script/prompt, recorded in
  `data/bench-models/state.json`), not re-measured today; GPU state and clocks can
  shift a few percent between sessions.
- No OOM or load failures occurred; the "FAILED" verdict category is untested.

---

## 6. Provenance / cleanup

- Code-fix: commit `c67845b` (manager.py `_build_args_string`), 7 regression tests
  in `tests/unit/test_manager.py`, full suite 949 passed / 3 skipped.
- The 4 temporary variant entries were removed from `config/models.yaml` and
  `data/bench-models/state.json` after this study (variants are invisible next
  launch; operator decides separately if any are keepers). Their measurements are
  preserved only here and in the regenerated benchmark table until the next
  bench run.
