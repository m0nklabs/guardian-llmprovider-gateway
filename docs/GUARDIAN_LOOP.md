# Guardian Loop — nightly development procedure (cold file)

> Executed by the daily reminder bound to the guardian-loop Session at
> **04:00 UTC**. This file is the authoritative procedure: the reminder only
> says "run the guardian loop", so a compacted or restored Session re-reads
> this document and continues from here.
>
> Operator decisions baked in (2026-10-25): autonomy **everything except
> merge**; cadence **04:00 UTC**; scope **this repository only**.

## Scope and autonomy boundary

Deployment, preset requirements and the reminder spec live in
`docs/GUARDIAN_LOOP_HANDOFF.md` (moving the loop to another session,
delivery verification, known traps).

**In scope:** `m0nklabs/guardian-llmprovider-gateway` only — gateway core,
cloud/local inference, capture, dashboard, config, and the caretaker
(including its TTS/STT lifecycle and management wiring, which is part of
Guardian). Anything else is out of scope, even when it looks related.

**Autonomy granted:**

- read every issue, PR, review thread and CI result in this repo;
- create branches, commit, push branches, open and update PRs;
- apply correct review suggestions, reply on review threads with evidence;
- post `/review` from the human `gh` account (`m0nk111`) after each push;
- open issues that describe verified defects or the next roadmap step;
- comment the nightly report on the tracking issue.

**Not granted (hard stops):**

1. **Never merge a PR.** Finish it and leave it ready for the human merge.
   The toolset does expose `mcp__github__merge_pull_request`; calling it is
   forbidden, as is any equivalent `gh pr merge`.
   Never approve a PR, never dismiss a review, never resolve a thread that
   still has an unanswered finding.
2. **Never push to `main`** (or any long-lived branch), never force-push,
   never rewrite published history, never delete branches or issues.
3. **Never restart `llama-guardian` or any systemd unit.** The loop's own
   model traffic runs *through* Guardian: a restart kills the run mid-flight
   and is not self-healing. Validate, then ask the operator in the report.
   `scripts/pre_restart_check.py` is used here as a *validation gate* only.
4. **Never touch another repository.** Cross-project findings are reported,
   not fixed.
5. **Never commit secrets** and never relax the TLS/keys boundaries: keep the
   cert/key pair rule, private keys `0600`, keys only in `.env` or
   `config/guardian.keys.yaml`.

## Budget per night

Bounded so a run stays reviewable and cheap:

- **all** open-PR remediation (batched: one coherent tested push per PR head);
- **at most one** new issue implementation, from a branch off `origin/main`;
- **no** speculative refactors, dependency bumps, or cleanup outside the
  issue/PR at hand.

Stop and report when the budget is spent. A finished short run is better than
a long speculative one.

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

When a PR is settled (CI green, latest review covers the merge head, every
finding resolved or answered), leave one closing comment: what changed, the
evidence, and `Ready for human merge`. **Do not merge.**

Draft PRs: keep them draft unless the work is complete and validated.

## Phase 2 — Open issues

```bash
gh issue list --state open --json number,title,labels,updatedAt
```

- Triage: does the issue still reproduce? Is it already covered by a PR or a
  commit on `main`? Stale → say so on the issue with evidence; never close it
  (only the operator closes issues, unless the issue explicitly delegates it).
- Pick the highest-value *executable* issue that is not already in a PR.
- Implement on `feat/<slug>-<issue>` or `fix/<slug>-<issue>` off
  `origin/main`, with tests pinning the changed public contract, then open the
  PR (`Closes #N`) and post `/review`.
- Review findings are hypotheses: verify before accepting or rejecting, and
  keep `if`/`elif` routing chains intact.

## Phase 3 — Advance the roadmap (only if phases 1–2 left budget)

Pick the next step from `docs/ROADMAP.md`, write a falsifiable issue (problem
statement, acceptance criteria, evidence links), then implement it in the same
run — one issue maximum. Skip when phases 1–2 already consumed the budget.

## Phase 4 — Report

Post one comment on the tracking issue **Guardian loop — nightly run log**
(tracking issue **#30**; `gh issue comment 30 --body-file <tmp>`):

```markdown
## Run <YYYY-MM-DD> 04:00 UTC
- window: <start> → <end> UTC
- PRs: #<n> <one line: what changed / settled / blocked> (or "none open")
- issues: #<n> <state after the run> (or "no executable issue")
- evidence: <commit SHAs, PR links, test result counts, CI state>
- next intended action: <one line>
- blockers / operator decisions needed: <one line or "none">
- restarts needed: <what must be restarted, and why — never performed here>
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
