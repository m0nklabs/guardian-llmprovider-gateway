# Historical loop documents before issue #113

Verbatim snapshots. Historical data only; not current instructions.

## docs/GUARDIAN_LOOP.md

````text
# Guardian Loop — development procedure (cold file)

> Executed by the cron reminder bound to the guardian-loop Session at
> **04:00, 10:00, 16:00 and 22:00 UTC** (four runs a day, six hours apart).
> This file is the authoritative procedure: the reminder only says "run the
> guardian loop", so a compacted or restored Session re-reads this document
> and continues from here.
>
> Operator decisions baked in (2026-10-25; cadence raised **and** merge
> authority granted 2026-10-27): autonomy **up to and including merge, but only
> through the merge gate below**; cadence **4×/day at 04:00, 10:00, 16:00,
> 22:00 UTC**; scope **guardian-llmprovider-gateway + caretaker-llamacpp**;
> standing objective **work the roadmap** (`docs/ROADMAP.md`) to completion.

## Scope and autonomy boundary

Deployment, preset requirements and the reminder spec live in
`docs/GUARDIAN_LOOP_HANDOFF.md` (moving the loop to another session,
delivery verification, known traps).

**In scope:** two repositories —

- `m0nklabs/guardian-llmprovider-gateway` — gateway core, cloud/local
  inference, capture, dashboard, config, and the caretaker wiring;
- `m0nklabs/caretaker-llamacpp` — the lifecycle daemon the roadmap depends on
  (its own `AGENTS.md`, review loop and conventions apply there).

Anything else is out of scope, even when it looks related.

**Autonomy granted:**

- read every issue, PR, review thread and CI result in both repos;
- create branches, commit, push branches, open and update PRs;
- apply correct review suggestions, reply on review threads with evidence;
- post `/review` from the human `gh` account (`m0nk111`) after each push;
- open issues that describe verified defects or the next roadmap step;
- **merge a PR once the merge gate below passes** (squash merge, `--squash`);
- **close an issue** whose merged PR satisfies its acceptance criteria, with a
  comment naming the merge commit and the evidence (operator rule, 2026-10-28);
- comment the run report on the tracking issue.

**Merge gate (all four, per PR, at the current head SHA):**

1. the latest PR-Piet review **at that SHA** reports no findings;
2. every CI check is green (skipped/neutral counts as green; a *failed* or
   cancelled review run is not — rerun it);
3. every review thread is resolved, and a thread was only resolved after its
   finding was fixed or answered with evidence;
4. a closing comment records what changed and the evidence.

Anything short of that stays a PR for the operator. When in doubt, do not
merge — ask in the report.

**A red CodeQL check is not yet a finding.** Before treating it as one, look at
*which lines* the alert annotations name: GitHub counts alerts on a PR against a
base analysis, and when the branch was cut before other merges landed the
comparison reports standing alerts as new. Observed on #69, where the check
claimed "32 new alerts including 1 high" for a PR whose four changed files
contained none of them — merging current `main` into the branch turned it green
with no change to the flagged files. So: if the flagged lines are outside your
diff, merge `main` in and re-run; only investigate the alerts when they are
actually yours. Never dismiss an alert to make a check go green.

**Not granted (hard stops):**

1. **Never merge outside the gate.** Never approve a PR (no self-approval),
   never dismiss a review, never resolve a thread that still has an unanswered
   finding, never merge your own unreviewed push.
2. **Never push to `main`** (or any long-lived branch), never force-push,
   never rewrite published history, never delete branches. Issues are closed
   only under the rule above — a completed deliverable, never to tidy the board,
   never with an acceptance criterion unmet. Merge and only merge — through the
   gate above.
3. **Never restart `llama-guardian` or any systemd unit.** The loop's own
   model traffic runs *through* Guardian: a restart kills the run mid-flight
   and is not self-healing. Run `scripts/pre_restart_check.py` as a validation
   gate for any code change and put the exact restart batch in the run report —
   the operator executes it. Waiting work that needs a restart is *merged and
   batched*, never deployed by the loop.
4. **Never touch a repository outside the two in scope.** Cross-project
   findings in any other repo are reported, not fixed.
5. **Never commit secrets** and never relax the TLS/keys boundaries: keep the
   cert/key pair rule, private keys `0600`, keys only in `.env` or
   `config/guardian.keys.yaml`.

## Budget per run

Bounded so a run stays reviewable and cheap (the budget is **per run**, and
runs do not accumulate it — each of the four daily runs starts fresh):

- **all** open-PR remediation (batched: one coherent tested push per PR head);
- **at most one** new issue implementation, from a branch off `origin/main`;
- **no** speculative refactors, dependency bumps, or cleanup outside the
  issue/PR at hand.

Stop and report when the budget is spent. A finished short run is better than
a long speculative one. When nothing needs doing, say so and stop: four runs a
day makes a no-op run the normal case on a quiet repository.

## Phase 0 — Preflight (evidence only, no writes)

```bash
cd /home/flip/guardian-llmprovider-gateway
git status --porcelain          # record dirty paths; they belong to the operator
git fetch origin --prune
git log --oneline -5
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:11434/v1/models   # 401 = alive (auth required)
systemctl is-active llama-guardian
```

- **Dirty working tree** → the operator is mid-work. Do not stop and do not
  clean: record the dirty paths, leave every one of them untouched, and skip
  any change that would need to touch one of them (report that skip). Never
  stash, checkout, reset, clean or commit someone else's uncommitted work.
- **Staging is explicit paths only.** Never `git add -A`, `git add .`,
  `git commit -a`, `git stash` or `git checkout -- <path>`; that is how
  uncommitted operator work ends up in a loop commit.
- **Guardian not active** → report blocked; do not change code that needs a
  restart to be validated.
- Read `docs/HANDOFF.md` (current open items) and `docs/ROADMAP.md` (next
  product step). Both are cold files: read on demand, not in full by habit.

## Phase 1 — Open PRs (highest priority)

For each open PR, oldest-updated first:

```bash
gh pr list --state open --json number,title,headRefName,isDraft,reviewDecision,statusCheckRollup
gh pr view <N> --json reviews,comments,files,commits,statusCheckRollup
gh api repos/m0nklabs/guardian-llmprovider-gateway/pulls/<N>/comments   # inline review threads
gh pr checks <N>
```

For every finding in the latest PR-Piet review **at the current head SHA**:

1. Treat it as a hypothesis. Verify against the code, the docs and the tests.
2. Correct and in scope → fix it in the same batch as the other findings for
   that head.
3. Wrong, already fixed, or out of scope → reply on the thread with the
   evidence (file:line, test output, or the commit that already covers it).
   Leave the thread open for the human; do not silently dismiss it.
4. Never leave a finding unaddressed and unanswered.

Then, per PR, one coherent batch:

```bash
./venv/bin/python -m py_compile <changed files>
./venv/bin/python -m pytest tests/ -x                 # or the targeted subset
./venv/bin/python scripts/pre_restart_check.py        # full gate for code changes
git commit ... && git push                            # branch only
gh pr comment <N> --body '/review'                    # human account, after each push
```

When a PR is settled, walk the **merge gate** (four conditions, at the current
head SHA) and record it in one closing comment: what changed, the evidence, and
`Merge gate: passed` (or which condition is unmet and why).

Gate passed → merge **the exact SHA you verified**, in this order:

```bash
# 1. pin the candidate FIRST
verified=$(gh pr view <N> --json headRefOid --jq .headRefOid)
# 2. verify THAT sha: the latest review must name it (`head \`$verified\``),
#    every check in `gh pr checks <N>` must pass for it, every thread resolved
# 3. merge that sha — never re-read the head in between
gh pr merge <N> --squash --match-head-commit "$verified" --subject "<what> (#<N>)"
```

The order matters: reading the head *after* the gate walk would pin whatever
arrived in between, and `--match-head-commit` would then happily accept an
unverified commit. Pin → verify → merge. If `--match-head-commit` rejects, the
head moved: walk the gate again from step 1 (fresh review, fresh CI) instead of
retrying blindly.

Then **verify `main`**: fetch, and run the relevant checks on the merged head
(the repo suite for code, the guard tests for docs). If the merged head is red,
fixing it is the next run's first task — report it as such.

Gate not passed → leave it open for the operator, with the failing condition
named. Never merge on the strength of an older review, never merge with an
unresolved thread, never merge with a red or failed check.

Draft PRs: keep them draft unless the work is complete and validated.

## Phase 2 — Open issues

```bash
gh issue list --state open --json number,title,labels,updatedAt
```

- Triage: does the issue still reproduce? Is it already covered by a PR or a
  commit on `main`? Stale → say so on the issue with evidence, and close it only
  under the rule below. An "you may close this" comment in an issue does not
  lift anything.
- **Every verified defect becomes an issue of its own** (operator rule,
  2026-10-28). A finding from a review, a probe, a triage or a stray read is not
  "noted" in a comment, a document or this session — it gets its own issue with
  reproduction evidence and acceptance criteria, so a later run can fix it with
  its own PR. A gap *register* (like #38) may group them, but each fixable
  defect still needs its own tracker to be closed by its own merge.
- **Closing an issue** (operator rule, 2026-10-28): allowed once the issue's
  merged PR satisfies its acceptance criteria — comment the merge commit and the
  evidence, then close. Never to tidy the board, never with an unmet criterion,
  never for an issue whose criteria need a live restart (those stay open until
  the restart is confirmed).
- Pick the highest-value *executable* issue that is not already in a PR.
- Implement on `feat/<slug>-<issue>` or `fix/<slug>-<issue>` off
  `origin/main`, with tests pinning the changed public contract, then open the
  PR (`Closes #N`) and post `/review`.
- Review findings are hypotheses: verify before accepting or rejecting, and
  keep `if`/`elif` routing chains intact.

## Phase 3 — Advance the roadmap (only if phases 1–2 left budget)

The standing objective is to **finish `docs/ROADMAP.md`** (G0 → G5, backlog rows
in §5). Pick the next row whose dependencies are merged, write a falsifiable
issue (problem statement, acceptance criteria, evidence links), then implement
it in the same run — one issue maximum per run.

**Claim before you build.** Other agents work in these repositories outside this
loop (observed 2026-10-27: a sibling session pushed to an open PR and deleted a
worktree mid-run). Before writing code for a row, comment on its issue:
`loop-claim: <who> <UTC timestamp>`; if a claim from someone else is younger
than 24 h, take the next row instead. Two sessions on one branch is how work
gets lost.

**Closing keyword: bare, and only when the merge really completes the issue.**
A PR body declares `Closes #N` **without backticks** — GitHub does not parse
closing keywords inside inline code, which is how #40 stayed open after its
deliverable merged.

But do **not** attach `Closes #N` to work whose acceptance criteria need a live
verification the loop cannot perform, i.e. anything sitting in the restart batch
(G1-class behaviour changes): the keyword fires on merge and would close the
issue before the operator has run the restart. Reference it as `Refs #N`
instead, merge, and close the issue only after the restart is confirmed in the
run report. Closing is earned by evidence, never by the merge event. A row that changes behaviour
needs a live restart to be proven: the loop merges it and puts it in the
restart batch, it never deploys it. Skip when phases 1–2 already consumed the
budget, and say in the report which row is next.

## Phase 4 — Report

Post one comment on the tracking issue **Guardian loop — run log**
(tracking issue **#30**; `gh issue comment 30 --body-file <tmp>`):

```markdown
## Run <YYYY-MM-DD HH:MM> UTC
- window: <start> → <end> UTC
- PRs: #<n> <what changed / settled / merged / blocked> (or "none open")
- merged: #<n> <merge-gate evidence: review head, CI, threads> (or "none")
- issues: #<n> <state after the run> (or "no executable issue")
- roadmap: <row delivered / next row> (or "unchanged")
- evidence: <commit SHAs, PR links, test result counts, CI state>
- next intended action: <one line>
- blockers / operator decisions needed: <one line or "none">
- restart batch: <exact units/commands the merged code needs, and the
  validation already run — never executed here>
```

Report facts, not intentions: failed or unrun checks are named as such. Also
send the MoniFuse agent-activity events (`task_started`, then
`task_achieved` / `task_failed` / `blocked`) per the `agent-activity-log`
skill.

## Failure handling

- Uncertain finding or unclear intent → fail closed: leave it for the human
  with the evidence, never guess in a merge-bound change.
- Red CI caused by the loop's own push → fix it in the same run when in
  scope, otherwise report it.
- Red CI or failing tests that predate the run → report; do not "fix" the
  gate by weakening tests.
- Guardian unhealthy, GitHub or the runner pool down → report blocked, make
  no code changes.
- A run that produces nothing is a valid outcome: say so plainly instead of
  inventing work.

````

## docs/GUARDIAN_LOOP_HANDOFF.md

````text
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
  "prompt": "Guardian loop run. Read /home/flip/guardian-llmprovider-gateway/docs/GUARDIAN_LOOP.md and execute it end-to-end for this slot, exactly as written: preflight, open-PR remediation (merge only through the merge gate), open issues, at most one roadmap row, then post the run report as a comment on tracking issue #30. Standing objective: finish docs/ROADMAP.md. Hard stops: never approve a PR, never push to main, never force-push, never restart Guardian or any systemd unit (put needed restarts in the run report), never touch a repository other than guardian-llmprovider-gateway and caretaker-llamacpp.",
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
boundary". Summary as of 2026-10-27 (merge authority granted): the loop may
**merge a PR, but only through the gate** — latest review at the merge head
with no findings, CI green, every thread resolved, closing comment with the
evidence. Squash only. It never approves a PR, never dismisses a review, never
pushes to `main`, never force-pushes, never closes issues, **never restarts
Guardian or any systemd unit** (its own model traffic runs through Guardian —
it merges, gates and batches restarts, the operator executes them), and
touches only two repositories: `guardian-llmprovider-gateway` and
`caretaker-llamacpp`.

The standing objective is to finish `docs/ROADMAP.md`; each run may deliver one
roadmap row (issue → branch → PR → review → merge gate).

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

````

## AGENTS.md

````text
# AGENTS.md — Guardian Policy and Traffic Gateway

> Canonical durable AI-agent context for this repo. Read targeted cold context only when the task requires it.
> Claude Code: `CLAUDE.md` → here. Goose: `.goosehints` → here. Copilot: `.github/copilot-instructions.md` references this.

## Product scope and naming

Guardian is the policy and traffic gateway for explicitly onboarded services,
not only an LLM proxy. It mediates client access, security, routing and capacity
policy; generalized load balancing and non-LLM adapters retain the delivery
status documented in `docs/ROADMAP.md`.

The general conceptual address is `{provider}/{brand}/{service}` (application
or service). Existing `{provider}/{brand}/{model}` cloud addresses are its
LLM specialization, not a separate product hierarchy. Guardian sits above the
namespace; API protocols, capabilities, engines and bridges are separate
architecture concerns. Preserve existing aliases and adapter-specific address
forms; this naming convention does not introduce a generic runtime resolver.
See `docs/ROADMAP.md` for definitions and compatibility boundaries.

## Stack

- **Language:** Python 3.14 (venv at `./venv`)
- **Web:** FastAPI + uvicorn + httpx
- **Backend:** llama.cpp (`llama-server` on `:11440`, launched via `scripts/start_llama.sh`)
- **Frontend:** React/Vite/Tailwind dashboard on `:11437` (`dashboard/`), bound to `127.0.0.1`
- **Config:** `config/global.settings.yaml` (proxy, providers, queue, timeouts), `config/providers/<naam>.settings.yaml` (één bestand per provider sinds F2; lokale registry/aliases/guardian in `ai-node-local.settings.yaml`), `config/guardian.keys.yaml` (named API keys)
- **Secrets:** `.env` — `${VAR}` expansion in YAML; never commit secrets
- **Deploy:** systemd unit `guardian-llmprovider-gateway.service` (alias `llama-guardian.service`), productie-checkout `/home/flip/guardian-llmprovider-gateway` — de legacy-dir `/home/flip/llama_cpp_guardian` is frozen (archief). Nginx exposes the public API on `:11434`.
- **TLS:** nginx stream TLS preread multiplexes both `http://192.168.1.G:11434` and `https://192.168.1.G:11434`. It passes TLS unchanged to Guardian on `127.0.0.1:11435` and routes plain HTTP through nginx on `127.0.0.1:11436`. See `deploy/nginx/guardian-llmprovider-gateway-protocol-mux.conf` and `deploy/nginx/guardian-llmprovider-gateway-loopback-http.conf`.
- **TLS trust:** this host trusts the Guardian certificate through `/usr/local/share/ca-certificates/llama-guardian-192.168.1.G.crt`. Other LAN clients must trust that same certificate before connecting without a custom CA setting.
- **Tests:** pytest (`tests/`, `asyncio_mode=auto`)

## Critical rules

- **Verify before claiming a fix.** Run `./venv/bin/python -m py_compile <file>`
  and relevant pytest coverage; use `./venv/bin/python -m pytest tests/ -x`
  when the full suite is warranted. Do not claim success without evidence.
- **Code restarts; configuration hot-reloads.** `app/*.py` changes require
  `sudo systemctl restart llama-guardian`; there is no hot code reload.
  `global.settings.yaml` changes hot-reload through authenticated
  `POST /api/config/reload`. Ports, PIDs, and TLS remain restart-only.
- **Run the pre-restart gate before every restart.**
  `./venv/bin/python scripts/pre_restart_check.py` runs py_compile, pyflakes,
  signature validation, and the full pytest suite. Every gate must pass first.
- **Restarting cuts the agent's own model traffic.** Validate first, notify the
  operator that the session will drop, and have the operator restart outside the
  session. A failed start is not self-healable; recover from outside Guardian.
- **TLS and secrets are strict boundaries.** `GUARDIAN_TLS_CERTFILE` and
  `GUARDIAN_TLS_KEYFILE` are an all-or-nothing pair; keep private keys `0600`
  and preserve the nginx stream multiplexer. Keep keys in `.env` or
  `config/guardian.keys.yaml`, never inline or commit them, and use
  `scripts/generate_key.py` to mint Guardian keys.
- **Keep the split config and routing contract.** Global settings, per-provider
  settings, and Guardian keys remain separate. Cloud models match `models:` or
  `model_prefixes:`; local aliases live in `ai-node-local.settings.yaml`; unknown
  models return `404 model_not_served`. The local `managed: true` provider is
  addressable but never cloud-routed.
- **Preserve cloud authorization and discovery.** Cloud addresses are
  `{provider}/{brand}/{model}` with no `guardian/` prefix or credential-link
  store. `cloud_gateway_access: false` must hide cloud models and return `403`.
  `catalog_url` limits a provider to reachable models; changing its endpoint
  invalidates the corresponding cold-start catalog cache.
- **Treat capability metadata as routing data.** Use local image fallback only
  for configured text-only cloud models with `image_fallback`. Resolve context
  metadata in order: `context_overrides`, cloud overrides, catalog or `/props`,
  then the `131072` fallback.
- **Preserve streaming and grammar contracts.** Every SSE path passes
  `heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S` to
  `_iter_sse_lines_with_watchdog`. Local OpenAI passes `response_format`,
  `json_schema`, and GBNF unchanged; cloud strips GBNF and `json_schema`, keeps
  native `response_format`, and capture records only presence flags.
- **Do not hardcode deployment values.** Keep paths, ports, URLs, filenames, and
  timeouts in configuration or `app/paths.py`; inject them when extracting code.
- **Diagnose dashboard empty shells as auth first.** Dashboard `/api/*` requests
  require a Bearer key; verify `guardian_dashboard_api_key` in localStorage
  before treating an empty dashboard as a code failure.

## Task-directed documentation

- **Cloud routing, model resolution, or cloud access:** read `docs/LLM_ROUTER.md`.
- **Configuration schema or provider files:** read `docs/CONFIG_SCHEMA.md` and
  `docs/CONFIG_PROVIDER_FILES.md`.
- **Anthropic bridge, API, clients, or architecture:** read the applicable
  `docs/ANTHROPIC_BRIDGE.md`, `docs/API_REFERENCE.md`,
  `docs/CLIENT_INTEGRATION.md`, or `docs/ARCHITECTURE.md`.
- **Deployment, TLS, or host operations:** read `docs/skills/operator-runbook.md`.
- **Product scope or roadmap:** read `docs/ROADMAP.md` (policy and traffic
  gateway, including non-LLM services). `docs/IMPLEMENTATION_PLAN.md` is the
  historical Guardian 2.0 F0–F7 migration specification, not the active roadmap.
- **Hardware tuning:** read `docs/HARDWARE_TUNING.md`; consult
  `docs/HANDOFF.md` for current operational state.

## Repository guide

- `app/proxy/` owns authentication, provider discovery, failover, queues,
  streaming, process state, metrics, and service lifespan.
- `app/gateway/` owns extracted routing, normalization, streaming, model
  discovery, admin API, sessions, context metadata, and caretaker wiring.
- `app/cloud_inference/`, `app/local_inference/`, and `app/capture/` own cloud
  forwarding, local model execution, and privacy-aware capture respectively.
- `config/` contains global, per-provider, and Guardian-key configuration.
- `scripts/pre_restart_check.py` is the restart gate;
  `scripts/update_guardian_config.py` mutates live configuration; and
  `scripts/guardianctl.py` manages capture operations.

## State, maintenance, and runners

- Read `docs/HANDOFF.md` only to continue active work, report current status, or
  address a named open item. Append same-session behavior changes and repository
  findings to the handoff or `docs/AGENT_JOURNAL.md`; completed handoffs go to
  `docs/ARCHIVED_HANDOFFS.md`.
- Historical detail, including prior capture phases and resolved decisions, lives
  in `docs/AGENT_CONTEXT_ARCHIVE.md`. The exact pre-condensation source is
  `docs/archive/agent-instructions-2026-09-22.md`; neither archive is startup
  context.
- `CLAUDE.md` and `.goosehints` are relative symlinks to this file. Keep hot
  instructions durable; place dated status and tool details in cold documents.
- The **guardian loop** runs **four times a day** (cron `0 4-22/6 * * *`:
  04:00, 10:00, 16:00, 22:00 UTC) and follows `docs/GUARDIAN_LOOP.md`, reporting
  on issue #30; its standing objective is finishing `docs/ROADMAP.md`. Autonomy
  runs **up to and including merge, but only through the merge gate**: latest
  review at the merge head with no findings, CI green, every thread resolved,
  a closing comment with the evidence, squash only. It never approves a PR,
  never pushes to `main`, never force-pushes, and never restarts Guardian or
  any systemd unit — merged work that needs a restart goes into a batch the
  operator executes. In scope: this repo plus `caretaker-llamacpp`.
- Reuse the organization-level self-hosted runner pool; never add a project
  runner. GPU jobs use `[self-hosted, Linux, gpu]` and
  `/home/flip/github-action-runners/bin/gpu-run.sh` so GPU work stays serial.
  Reuse suitable organization workflows and never commit workflow secrets.

## Git, PR, and review

- Operator-facing notes may be Dutch; code, APIs, and public documentation are
  English. Use the configured implementing-model commit identity; never replace
  it with the reviewer identity.
- PR-Piet is the repository PR-review workflow. Post `/review` from a human
  account after the final commit and after every later push; bot senders are
  ignored. Apply correct suggested changes in sensible batches.
- Human merge is the default. An agent may merge only when the latest review
  covers the merge head, CI is green, no findings or threads remain open, and a
  closing PR comment confirms each finding was resolved or answered with
  evidence. Never auto-approve.
- Treat review findings as hypotheses. Verify before accepting or rejecting;
  use independent evidence for uncertain or high-impact conclusions. Preserve
  `if`/`elif` routing chains and pin changed public contracts with tests.
- A cancelled review run is a failed check: rerun it. If a deep `/review` times
  out on a large diff, rerun the automatic `pull_request` review.

````
