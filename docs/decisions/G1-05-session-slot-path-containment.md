# G1-05 — Session-slot filenames are contained by normalisation plus a prefix check

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |
| **Issue** | [#76](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/76) |
| **Alert** | `py/path-injection` #11, `app/gateway/sessions.py:46` |

## Decision

A session-slot filename from `POST /api/session/save` / `/api/session/load`
reaches llama-server **only** after two independent constraints have both held,
in this order, inside `app/gateway/sessions.py:sanitize_session_filename`:

1. **Basename whitelist.** The value is reduced to its final path component
   (`Path(raw).name`) and must then fully match `^[A-Za-z0-9_-]+\.bin$`. That
   character class cannot contain a separator, a `..` component or a NUL, so
   the value llama-server joins onto its `--slot-save-path` names a file
   directly inside that directory.
2. **Normalised containment.** The joined path is put through
   `os.path.realpath` — both the slots directory and the candidate — and the
   candidate must `startswith` the normalised directory plus `os.sep`. This is
   the guard the whitelist cannot give: a symlink planted *inside* the slots
   directory resolves to its target, so a link pointing at `/etc/shadow` is
   rejected instead of read or written.

The function still returns the **basename**, not the resolved path: llama-server
performs its own join, so the contract with it is a name, and constraint 2 is a
proof about that name rather than a different value being passed on.

## Why the containment check has exactly this shape

The shape is chosen so that a reviewer can see the guard *and* the static
analyser can see it, which is the same reasoning G1-04 applied to
`clean_log_value`. CodeQL's `py/path-injection` query is a state machine, not a
taint filter (`python/ql/lib/semmle/python/security/dataflow/PathInjectionQuery.qll`):

- a source starts in state `NotNormalized`;
- only a `Path::PathNormalization` node (`os.path.normpath`, `abspath`,
  `realpath`) moves it to `NormalizedUnchecked`;
- only a `Path::SafeAccessCheck` barrier — in the standard library model, a
  `str.startswith` call used as a true-branch guard (`Stdlib.qll`,
  `StartswithCall`) — makes that state safe.

Two consequences follow, and both decided the implementation:

- **`pathlib.Path.resolve()` is not a normalisation in that model; it is a
  sink.** `Stdlib.qll` lists `resolve` in `PathlibFileAccess`'s file-system
  accesses, so `(slots / basename).resolve()` is exactly where the query
  reported the alert, and no amount of checking *afterwards* moves the flow out
  of `NotNormalized`. `os.path.realpath` is a normalisation and is not in the
  sink list, so the guarded value never flows into a file-system access at all.
- **A regex `fullmatch` is invisible to the model.** The whitelist in
  constraint 1 was — and still is — the actual primary guard, yet it could not
  close the alert. That is not a reason to weaken it: the record keeps both,
  with the model-visible one written in the form the model sees.

## Measured effect

| | before | after |
| --- | --- | --- |
| `py/path-injection` alerts, `app/gateway/sessions.py` | 1 (#11, line 46) | 0 in the PR's CodeQL analysis |
| accepted inputs | `[A-Za-z0-9_-]+.bin` | unchanged |
| rejected inputs | traversal, separators, metacharacters, missing suffix | + symlink escaping the slots directory, + unconfigured slots directory (fails closed, HTTP 500) |

## What this does not change

Session semantics, the llama-server protocol (`{"filename": …}` as a basename),
and the 400 responses are unchanged. `app/proxy/server.py:_sanitize_session_filename`
stays a thin delegate to this function.

## Guards

`tests/unit/test_session_filename_sanitize.py`:

- `TestContainmentInsideTheSlotsDirectory::test_traversal_shaped_ids_can_never_escape`
  — nine hostile shapes, each asserted to raise 400 or to resolve *inside* the
  injected directory (the assertion fails if the containment line is removed).
- `…::test_symlink_planted_in_the_slots_directory_is_rejected` — the case the
  whitelist alone cannot catch.
- `…::test_a_real_slot_name_is_still_accepted`,
  `…::test_an_unconfigured_slots_directory_fails_closed`.

## Evidence

- CodeQL model: `PathInjectionQuery.qll` (`NormalizationState`,
  `NormalizedUnchecked`), `PathInjectionCustomizations.qll`
  (`Sanitizer = ConstCompareAsSanitizerGuard | SanitizerFromModel`),
  `Concepts.qll` (`Path::PathNormalization`, `Path::SafeAccessCheck`),
  `Stdlib.qll` (`OsPathRealpathCall` / `OsPathNormpathCall` /
  `OsPathAbspathCall` normalisations; `StartswithCall` safe access check;
  `PathlibFileAccess` listing `resolve` as an access).
- [#76](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/76) —
  the worklist, the per-site verdicts and the dismissal comments.
- `docs/decisions/G1-04-log-injection.md` — the same "make the guard visible to
  the model" reasoning for the log sinks.
