# Guardian loop — schedule and delivery handoff

The [loop procedure](GUARDIAN_LOOP.md) defines work and safety gates. This file
records operator-owned automation state and safe maintenance. The superseded
October procedure/spec is [archived verbatim](archive/guardian-loop-2026-11-18-before-strategy.md);
its cron/tool names, host paths and session-scoped inventory assumptions are not
current instructions.

## Verified snapshot — 2026-11-18

Source: live `schedule_view`, not an inferred cron file or old transcript.

| Field | Observed value |
| --- | --- |
| Task | `schedule-c4fa549a-1c4a-4c88-8e77-5e053ad972b8` |
| Title | Guardian loop — development run |
| Bound session | `session-290d375d-726c-4871-9820-5105c9b2d80e` |
| Workspace | `/home/flip/guardian-llmprovider-gateway` |
| Recurrence | `DTSTART:20261119T060000Z` followed by `RRULE:FREQ=DAILY;BYHOUR=6,18;BYMINUTE=0` |
| Cadence | Twice daily, 06:00 / 18:00 UTC; do not silently change it |
| Next occurrence at inspection | 2026-11-19 06:00 UTC |
| Last observed delivery | 2026-11-18 16:00 UTC, delivered 16:00:00.047 UTC under the previous cadence |
| Run reports | [Guardian tracking issue #30](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/30) |

The prior delivery proves historical delivery only. It does **not** prove that the
new recurrence or revised prompt has executed; verify the next natural occurrence
before claiming that. No new delivery-test schedule is authorized by a prompt edit.

## Safe prompt maintenance

1. Require explicit operator direction to edit automation. The 2026-11-18 request
   authorizes updating the existing loop **text**, not its cadence/target.
2. Read current tool schemas (`tool_help`) and `schedule_view(id)`. Available APIs
   currently include `schedule_list`, `schedule_view` and `schedule_edit` with
   RRULE recurrence; do not copy obsolete `cron`/`after_seconds` examples.
   `schedule_list` includes tasks created from other sessions; filter by exact ID.
3. Use `schedule_edit` with `action: update`, exact ID, revised `prompt` and the
   latest `expectedUpdatedAt`. **Omit** `rrule`, `target`, `active` and `title` to
   preserve them. On conflict reread, reconcile operator changes, never blindly retry.
4. Re-read the task and compare prompt, recurrence, target and next occurrence.
   Record the result on issue #30. Do not edit DSH storage files or service units.

## Prompt content and documentation delivery

The prompt must stand on its own for safety while pointing to the procedure:

- Mission spans both roadmaps: Guardian gateway/router/proxy/protocol bridge/safe
  traffic logging, Caretaker resource/service management/control, cross-platform
  quality, reproducible debugging and measured innovation.
- Preflight, existing PR remediation and high-risk issues precede new feature work;
  one new implementation and one deliberate merge maximum per run. Dependencies
  must be satisfied by evidence, not only merged. Bounded isolated experiments.
- Current-head completed independent review, all findings handled, passing checks,
  account **2-hour** waiting anchor, latest-activity/base/check revalidation and
  pinned-head squash. Closing comment normal, not a prerequisite. Authentication,
  secrets, migrations, deploy and dependency majors stay human-gated.
- No approvals, main/force pushes, production experiments/config mutations or
  service restarts. Exact restart batch goes to the operator. Only the two repos.
- Report implementation/test/deployment status, measurements, unavailable checks,
  next action and blockers honestly on issue #30; log AAL milestones fail-open.

Documentation PRs may not yet be merged or present in the production checkout.
The scheduled prompt therefore carries the new mission and global safety rules
**directly**, and explicitly supersedes conflicting old procedure text. At each
run, inspect the local procedure's revision and current-main documentation. Do
not checkout an unreviewed docs branch into production or deploy code just to
obtain updated instructions. Report doc-delivery lag; use reviewed/merged docs
when available without altering the production worktree. A docs-only process
update does not need a Guardian restart.

## Handoff / delivery troubleshooting

Do not recreate, retarget or delete the loop without a new explicit instruction.
Use current tool schemas rather than assumptions about presets, GUI forms, host
ports or storage locations. Never start/restart a DSH unit to repair presumed
schedule trouble without operator authorization.

For a missed run inspect the task's current status, recurrence, next occurrence,
last delivery and bound-session transcript, then issue #30. Distinguish scheduled,
delivered and procedure-completed; a delivery timestamp alone proves no work was
done. Delivery may repeat after a crash; inspect run/claim evidence to avoid
repeating side effects. After downtime only the latest missed occurrence may be
delivered; do not manufacture historical runs.

Completion of a prompt-only update means: exact intended prompt saved; existing
recurrence/target/status preserved; read-back verified and report linked. Natural
next-run delivery and documentation rollout remain separately evidenced states.
