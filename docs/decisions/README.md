# Guardian decision records

Versioned, dated records for decisions that change a contract this repository has
to live with: security boundaries, configuration schema, public API behaviour and
delivery-status claims. `docs/ROADMAP.md` §G1 requires them ("publish versioned
decision records"); this directory is where they live.

## Convention

- **File name:** `G<phase>-<nn>-<slug>.md` — phase from `docs/ROADMAP.md`
  (`G0`, `G1`, …), a two-digit sequence within the phase, and a short
  kebab-case slug. `G1-01-privilege-scopes.md` is the first record.
- **Header block:** every record opens with `Status`, `Decided` (date),
  `Supersedes` and `Superseded by`. Use `none` when the field does not apply.
- **Numbering:** sequential per phase in creation order. Numbers are never
  reused, and a deleted or withdrawn record keeps its number with status
  `Withdrawn`.
- **Immutable once merged.** A merged record is history: do not rewrite its
  decision, its rationale or its tables. To change a decision, merge a new
  record that states what it supersedes, and update the old record's header to
  point at the successor. Typo fixes and link repairs are the only in-place
  edits allowed.
- **One decision per record.** If a change needs two independent decisions, it
  needs two records; cross-link them.
- **Reviewed with the code.** The record lands in the same pull request as the
  implementation, and the implementation cites the record. A code change that
  moves an enforced boundary without a matching record is a review defect.

## Index

| Record | Title | Status | Decided |
| --- | --- | --- | --- |
| [G1-01](G1-01-privilege-scopes.md) | Privilege scopes and admin isolation | Accepted | 2026-10-28 |
| [G1-02](G1-02-destination-and-credentials.md) | Destination allowlist, credential isolation and SSRF resistance | Accepted | 2026-10-28 |
| [G1-03](G1-03-admission-and-audit.md) | Bounded admission and metadata audit | Accepted | 2026-10-28 |
| [G1-04](G1-04-log-injection.md) | Log-injection guard: one sanitizer, and a model that can see it | Accepted | 2026-10-28 |
