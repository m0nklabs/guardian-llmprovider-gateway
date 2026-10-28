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

## Addendum — 2026-10-28: the last `py/log-injection` site, and three text sinks next to it

[#76](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/76) took
the one `py/log-injection` site this record deliberately left behind (that file
was owned by the capture work, which has since merged) plus the text-sink alerts
in the same neighbourhood. This adds to the decision above; it does not change
it. The sanitizer, the "one guard, no local copies" rule and the per-site
judgement are untouched.

| Site | Verdict | Why |
| --- | --- | --- |
| `app/capture/redactor.py:663` — `logger.warning(f"Unknown Anthropic content block type: {block_type}")` | **Fixed** | `block_type` is the request's own `type` field on an Anthropic content block: caller-controlled, unvalidated, previously interpolated into an f-string. It now reaches the log as `clean_log_value(block_type)` in a lazy `%s` argument — rule 1 of "How to classify the next `py/log-injection` alert". |
| `app/proxy/auth.py:694` — `logger.info(f"🔑 Auth success: {user_data.get('name', 'Unknown')}")` | **Fixed (label removed)** | The value was a key-store *label*, not credential material (§11.2 of G1-03), so the `py/clear-text-logging-sensitive-data` alert on this line is a false positive in substance (`G1-06-credential-fingerprint-hashing.md`). It is nevertheless store data of unbounded shape flowing into an INFO line, and the PR's own CodeQL analysis re-reported it at the moved line, so the line now identifies the key the way every other auth line does — `key_fingerprint=<sha256(token)[:12]>` — and the label stays where G1-03 §11.2 puts it: the audit record's bounded, cleaned `key_name`, which every allowed request already emits. §11.3 therefore holds by construction, not by sanitising. |
| `app/proxy/auth.py:722` — `print(f"Generated API Key for {name}: {key}")` | **Removed** | The `if __name__ == "__main__"` helper minted a key and printed it: a second, undocumented minting CLI beside the documented `scripts/generate_key.py`. Deleting it removes a credential-print surface rather than arguing about it. |
| `scripts/test_system.py:32` — `print(f"🔑 Key: {API_KEY[:10]}...")` | **Fixed** | The smoke test printed ten characters of a live key merely to show that one was resolved. It now prints that a key is configured and its length; nothing about the credential is disclosed, and the alert closes because the value no longer flows into the print. |

Values that stay bare, for the same reason as before: `_token_fingerprint`
(12 derived hex characters), status codes, counters and configured labels.

Guards added with the fixes — each one fails if its guard is removed:

- `tests/unit/test_capture_redactor.py::TestAnthropicTranslation::test_unknown_block_type_cannot_forge_a_log_line`
- `tests/unit/test_auth.py::TestVerifyApiKey::test_auth_success_line_identifies_the_key_by_fingerprint`
- `tests/unit/test_auth.py::TestVerifyApiKey::test_a_hostile_key_label_cannot_reach_the_auth_log`
  (a label carrying a forged line, ANSI, a NUL and the token itself)

The remaining five alerts are adjudicated in
`G1-05-session-slot-path-containment.md` (the one real path sink) and
`G1-06-credential-fingerprint-hashing.md` (the identity hash and the
deliberate-output dismissals). Note that #76's table names eight alerts while
**nine** are open outside `py/partial-ssrf`: alert **#4**, the `Prefix: {prefix}`
print on `scripts/generate_key.py:82`, is the ninth and is adjudicated together
with its neighbour #5.
