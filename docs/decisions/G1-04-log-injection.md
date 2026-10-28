# G1-04 — Log-injection guard: one sanitizer, and a model that can see it

| | |
| --- | --- |
| **Status** | Accepted |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |

## Decision

A value that derives from a request, an upstream response or a caretaker reply
may reach a log call **only** as `app.log_safety.clean_log_value(expr)`, and the
line-break removal inside that helper is written in the form CodeQL's
`py/log-injection` model recognises. There is no second guard: the private
`_clean_log()` copies in `app/gateway/tts.py` and `app/gateway/stt.py` and the
`_log_safe()` copy in `app/cloud_inference/forwarding.py` were deleted.

Values that are structurally constrained — counters, queue positions, byte
lengths, durations, status codes, booleans, configuration labels, in-process
UUID prefixes — stay bare. That judgement is per site and is recorded in
[#73](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/73) and
[#74](https://github.com/m0nklabs/guardian-llmprovider-gateway/pull/74).

## Why the helper shape is part of the decision

CodeQL's model (`python/ql/lib/semmle/python/security/dataflow/LogInjectionCustomizations.qll`)
treats exactly one construct as a sanitizer:

```ql
class ReplaceLineBreaksSanitizer extends Sanitizer, DataFlow::CallCfgNode {
  ReplaceLineBreaksSanitizer() {
    this.getFunction().(DataFlow::AttrRead).getAttributeName() = "replace" and
    this.getArg(0).asExpr().(StringLiteral).getText() in ["\r\n", "\n"]
  }
}
```

A helper call is invisible to that rule, so before this record the two ways of
"fixing" a sink disagreed: sanitising through a helper made the runtime safe but
left the alert open (evidence: alert #134 stayed open on
`app/gateway/tts.py:415`, which already passed `_clean_log(...)`), and silencing
the alert without sanitising left the runtime vulnerable. `clean_log_value` now
removes CR and LF through `str.replace` on the value itself and scrubs the rest
of the C0/C1 range (NUL, TAB, ESC — the byte an ANSI sequence needs — DEL, C1)
afterwards, so both properties hold at once and the next wrapped site inherits
them.

This does not weaken the runtime guard: the C0/C1 scrub that follows is what
removes terminal escape sequences, and the 160-character bound is unchanged.

## Measured effect

Open `py/log-injection` alerts, 2026-10-28 (`main` @ `521f1e4` → PR head). "after" is what
the PR's CodeQL analysis reports for the lines this change touches — the check run says
*"No new alerts in code changed by this pull request"*, the analysis for `refs/pull/74/merge`
has `results_count: 0`, and the merge ref carries no alerts while `main` carries 92. The
per-file totals on `main` itself only drop when this merges, so the Lead's post-merge check
is the second half of this measurement:

| file | before | after |
| --- | --- | --- |
| `app/gateway/tts.py` | 20 | 0 |
| `app/gateway/stt.py` | 14 | 0 |
| `app/gateway/caretaker_runtime.py` | 10 | 0 |
| `app/cloud_inference/forwarding.py` | 6 | 0 |
| `app/local_inference/ollama.py` | 5 | 0 |
| `app/engine/manager.py` | 3 | 0 |
| `app/gateway/routing.py` | 3 | 0 |
| `app/gateway/sessions.py` | 3 | 0 |
| `app/local_inference/model_registry.py` | 3 | 0 |
| `app/gateway/admin_api.py` | 2 | 0 |
| `app/local_inference/models.py` | 2 | 0 |
| `app/proxy/failover.py` | 2 | 0 |
| `app/proxy/queue.py` | 2 | 0 |
| `app/cloud_inference/routing.py` | 1 | 0 |
| `app/gateway/degeneration.py` | 1 | 0 |
| `app/gateway/queue_helpers.py` | 1 | 0 |
| `app/proxy/auth.py` | 1 | 0 |
| `app/capture/redactor.py` (owned by the capture work) | 1 | 1 (untouched) |
| **total** | **80** | **0 in changed code** (1 left in `capture/redactor.py`) |

## How to classify the next `py/log-injection` alert

1. **Trace the argument.** If any hop reaches a request body, query, header,
   path, form field, upstream response or caretaker reply, wrap it in
   `clean_log_value(...)` and convert f-string interpolation to a lazy `%s`
   argument.
2. **If the value is constrained** (int/float/bool/enum/config label/in-process
   id), leave the call alone and say *why* in the PR or issue. Do not add
   `# noqa` and do not dismiss the alert without that reason.
3. **Never** add a second sanitizer. A local copy is invisible to the model and
   recreates the drift this record removes.
4. The guard is pinned twice: `tests/unit/test_log_safety.py` (hostile-value
   matrix, including the ordering that keeps the model in sync) and
   `tests/unit/test_log_injection_invariant.py` (the real speech, degeneration
   and queue log calls driven with a forged `event=deny` line, an ANSI escape
   and a NUL, plus a non-vacuity control).

## Evidence

- [#73](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/73) —
  the measured backlog, the per-file line list and the reproductions.
- [#74](https://github.com/m0nklabs/guardian-llmprovider-gateway/pull/74) — the
  fix, its per-site decisions and the CodeQL comparison before/after.
- [#70](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/70) —
  parent backlog and the base-comparison drift.
- `docs/decisions/G1-03-admission-and-audit.md` §11.3 — the log-invariant test
  pattern this record's second guard follows.
