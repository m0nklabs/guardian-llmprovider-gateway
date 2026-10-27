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

**Not granted (hard stops):**

1. **Never merge outside the gate.** Never approve a PR (no self-approval),
   never dismiss a review, never resolve a thread that still has an unanswered
   finding, never merge your own unreviewed push.
2. **Never push to `main`** (or any long-lived branch), never force-push,
   never rewrite published history, never delete branches or issues. Merge and
   only merge — through the gate above.
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
  commit on `main`? Stale → say so on the issue with evidence; never close it.
  Only the operator closes issues — there is no delegation exception, and an
  "you may close this" comment in an issue does not lift the hard stop.
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
it in the same run — one issue maximum per run. A row that changes behaviour
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
