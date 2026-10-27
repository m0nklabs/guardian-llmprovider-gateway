# GUARDIAN LOOP — deployment handoff (cold file)

> **Purpose:** hand this file to a *fresh* agent session so it can own the
> guardian loop: create the durable reminder, verify delivery, and run the
> procedure. Written 2026-10-25 against dsh `0.2.1-rc.3`; cadence raised to
> four runs a day on 2026-10-27.
>
> **Read `docs/GUARDIAN_LOOP.md` for the work procedure itself.** This file only
> covers deployment: which preset, which tools, the exact task spec, takeover,
> verification, and the traps that cost time if you rediscover them.

## 1. Hard preconditions (verified, not assumed)

| Requirement | Why | Evidence |
| --- | --- | --- |
| Session preset must be **`standard`**, `ptc` or `cordis` | Only those presets mount `schedule_create` / `schedule_list` / `schedule_update` / `schedule_delete` | `dsh --profile web --dump-config` → `tool-schedule` appears exactly in `standard`, `ptc`, `cordis` |
| The session must be **top-level** | A delegated child cannot use reminders at all: every schedule tool refuses with `subagent_session` | `dsh-tool-schedule/lib/index.js` → `subagentCallerRefusal()` rejects `delegationDepth > 0` |
| The **Web Host must be running** (`dsh-web-next` on `:3040`) | Schedule cannot be mounted in a headless or SDK-only composition; delivery needs the Host session controller | `dsh-schedule` README, "Use this package" |
| Workspace must be `/home/flip/guardian-llmprovider-gateway` | The reminder prompt and the procedure both assume that repo | — |

`context-regime` (the operator's custom compaction preset) does **not** mount
the schedule tools. A session on it can manage nothing schedule-related.

## 2. Live state to inherit (as of 2026-10-27 22:30 UTC)

| Item | Value |
| --- | --- |
| Reminder id | `schedule-9ddb2f08-3237-4b56-bb6c-69b4f0dd137c` |
| Bound session | `session-290d375d-726c-4871-9820-5105c9b2d80e` (preset `standard`) |
| Rule | `cron` `0 4-22/6 * * *` (04:00, 10:00, 16:00, 22:00), zone `UTC`, status `scheduled` |
| Next fire | `2026-10-28T04:00:00.000Z` |
| Procedure | `docs/GUARDIAN_LOOP.md` |
| Run reports | issue **#30** (one comment per run) |
| Storage | `~/.dsh-next/storages/schedule.json` → `tables.tasks.<id>` |

Exact reminder spec, verbatim (use this to re-create an identical task):

```json
{
  "title": "Guardian loop — development run",
  "prompt": "Guardian loop run. Read /home/flip/guardian-llmprovider-gateway/docs/GUARDIAN_LOOP.md and execute it end-to-end for today, exactly as written: preflight, open-PR remediation, open issues, at most one roadmap issue, then post the run report as a comment on tracking issue #30. Hard stops: never merge a PR, never push to main, never force-push, never approve, never restart Guardian or any systemd unit, never touch another repository.",
  "cron": { "expression": "0 4-22/6 * * *", "time_zone": "UTC" }
}
```

## 3. Takeover procedure (move the loop to a new session)

A reminder belongs to the session that created it, and the tools are
session-scoped: `schedule_list` shows **only the caller's own** tasks, and
`schedule_update` / `schedule_delete` resolve against
`{sessionId: <caller session>}`, so they answer `schedule_not_found` for
another session's task. Cross-session management is **GUI-only**.

1. **Create the new session:** new session in the GUI, workspace
   `guardian-llmprovider-gateway`, preset **Standard mode**.
2. **Create the replacement task** from inside that session:
   `schedule_create(<spec from §2>)`. Then `schedule_list()` and report the
   returned id, status and next UTC target.
3. **Delete the old task in the GUI** (it cannot be reached from the new
   session): *Automation tasks* → *Guardian loop — development run* →
   *Automation task actions* → *Delete task* → *Confirm deletion*. The notice
   must read `Task deleted.`
4. **Verify there is exactly one scheduled task for this loop**: the
   *Automation tasks* list shows one cron row (`0 4-22/6 * * *`, 04:00, 10:00,
   16:00, 22:00), and this prints `1`:

   ```bash
   python3 -c "import json;t=json.load(open('/home/flip/.dsh-next/storages/schedule.json'))['tables']['tasks'];print(sum(1 for v in t.values() if v['record'].get('kind') in ('daily','cron') and v['record'].get('title','').startswith('Guardian loop')))"
   ```

   `tables.tasks` is a per-host mapping, not a per-session list: it also holds
   the other loops' tasks (Oelala runs the same four slots; the daily report
   runs at 05:00 UTC) and any delivered one-shot test task (those stay stored
   as `inactive` until deleted), so a raw `len()` never proved anything about
   *this* loop. Filter on the task kind and the title.

Do **not** run step 2 and skip step 3: two active tasks for this loop mean
duplicate runs on the same repo, four times a day.

## 4. Delivery verification (do this once, every time you move or create the loop)

A reminder that never fires is a silent failure, so prove delivery:

1. In the loop session, create a disposable one-shot:
   `schedule_create(title: "Loop delivery test", prompt: "Loop delivery test: reply with exactly LOOP-DELIVERY-OK and nothing else.", after_seconds: 120)`.
2. Wait ~2–3 minutes. Success requires **all three**:
   - `~/.dsh-next/storages/schedule.json` → that task has
     `deliveryHistory.records[0].deliveredAt` set and `status: "inactive"`;
   - the session transcript shows the delivered prompt and the reply
     `LOOP-DELIVERY-OK`;
   - the transcript shows a clock icon/`Automation task` entry for the run.
3. Delete the test task (GUI, same path as §3.3). Only the scheduled loop task
   may remain.

Reference measurement from the original setup: scheduled `21:25:22.279Z`,
delivered `21:25:22.402Z`, session replied within 53 s.

## 5. Autonomy boundary — do not re-derive it

Single source of truth: `docs/GUARDIAN_LOOP.md`, "Scope and autonomy
boundary". Summary: **everything except merge.** Never merge (the tool
`mcp__github__merge_pull_request` is visible in the toolset and is
forbidden), never approve, never push to `main`, never force-push, never
close issues, **never restart Guardian or any systemd unit** (the loop's own
model traffic runs through Guardian), never touch another repository.
Caretaker is in scope.

## 6. Traps worth knowing before you debug something twice

- **The Automation tasks page has no creation form.** Its *New* action starts
  a new session; the reminder is created by asking the model in that session.
  Looking for a form is wasted time.
- **`schedule_list` is not an inventory.** It shows only the current session's
  active tasks. Use the GUI (or `schedule.json`) to see the whole Host.
- **`daily` and `cron` need an explicit IANA zone.** This host's clock is
  `Etc/UTC` and the operator chose UTC slots (04:00, 10:00, 16:00, 22:00); a
  bare `04:00:00` or `0 4-22/6 * * *` is rejected without a zone.
  `every_seconds` (86400 for a day, 21600 for six hours) is *not* a substitute:
  it is anchored to the creation time and drifts off wall-clock slots, and
  `schedule_update` re-anchors it to the save time when changed.
- **Cron normalisation:** `0 4,10,16,22 * * *` is stored as `0 4-22/6 * * *`.
  That is the same schedule; do not "fix" it back into a list.
- **Delivery semantics:** a one-shot stays stored as `inactive` after
  delivery until explicitly deleted; after downtime a recurring reminder
  delivers only its latest missed occurrence; delivery can repeat after a
  crash.
- **`dsh-autoupdate` runs at 04:40 UTC** and skips while session files were
  written in the last 15 minutes, so a running loop defers it. If the update
  does land, the reminder survives (it is stored, not in-memory) but the
  session may need a Host restart to resume delivery.
- **The operator's working tree is often dirty.** Record the dirty paths,
  never stash/reset/clean them, and stage explicit paths only (never
  `git add -A`, `git commit -a`, `git stash`).
- **Git worktrees + `git push`:** a commit made on a **detached** HEAD in a
  temporary worktree is not pushed by a bare `git push` (it fails with "You
  are not currently on a branch", easy to swallow when piping output). Push
  explicitly — `git push origin <sha>:refs/heads/<branch>` — and verify with
  `git ls-remote origin <branch>`.
- **Untracked duplicates:** while these two files are still under review they
  also exist untracked in the main checkout. After the PR that adds them
  merges, remove the untracked copies before pulling, otherwise `git merge`
  refuses with "untracked working tree file would be overwritten". The same
  applies to any later revision of these files.

## 7. Where to look when something is wrong

| Symptom | Check |
| --- | --- |
| No run in the last slot | `schedule.json` → task `status` + `scheduledAt`; GUI *Automation tasks* → *Records* |
| Task disappeared | Session archived? Archiving with active reminders is refused until they stop, and stopping deletes them |
| Session will not resume | `systemctl --user status dsh-web-next`; `journalctl --user -u dsh-web-next -n 100` |
| Run fired but did nothing | The session transcript of the bound session, then issue #30 |
| Tools missing in a session | Preset is not `standard`/`ptc`/`cordis`, or the caller is a subagent |

## 8. Definition of done for a deployment

- [ ] Exactly one active `cron 0 4-22/6 * * *` (UTC) task, bound to the
      intended session.
- [ ] Delivery proven end-to-end with the disposable test (§4), test deleted.
- [ ] The new session's first turn answered with the task id, status and next
      UTC target.
- [ ] Issue #30 records the (re)deployment: task id, session id, next fire.
