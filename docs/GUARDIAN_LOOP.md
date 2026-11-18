# Guardian Loop — evidence-led development procedure

> Standing objective: continuously improve Guardian into an exceptional cross-platform
> policy gateway, router, proxy, protocol bridge and privacy-aware traffic logger;
> improve Caretaker into the complementary resource/service manager and controller.
> These are ambitions, not claims that the capabilities are complete or deployed.
> Work both canonical roadmaps: [Guardian](ROADMAP.md) and
> [Caretaker PLAN](https://github.com/m0nklabs/caretaker-llamacpp/blob/main/PLAN.md).
>
> Current operator-owned schedule: **06:00 and 18:00 UTC**, twice daily, verified
> 2026-11-18 for occurrences beginning 2026-11-19. Do not change cadence, target
> session or create replacement/test tasks without explicit operator direction.
> [Deployment handoff](GUARDIAN_LOOP_HANDOFF.md) records task identity and delivery.
> [Previous procedure and handoff](archive/guardian-loop-2026-11-18-before-strategy.md)
> are preserved verbatim as historical evidence, not active instructions.

## 1. Scope, ownership and hard stops

Only `m0nklabs/guardian-llmprovider-gateway` and `m0nklabs/caretaker-llamacpp`
are in scope. Read the target repository's `AGENTS.md` before writing there;
user-global instructions prevail. Other repositories receive attributed handoffs,
not fixes. No service or repository renaming is authorized.

- **Guardian owns** client identity/authorization, traffic admission, route/pool
  policy, protocol transformation, request telemetry and cost/privacy policy.
- **Caretaker owns** authorized lifecycle execution, observed readiness/health,
  host-resource admission/reservations and recovery through platform adapters.
  Guardian requests readiness/capability through the agreed contract, not direct
  process control. Caretaker validates feasibility, not client destination policy.
  One lifecycle/resource authority per resource; no competing watchdog/idle controller.
- Existing capabilities and pending contracts keep their evidence-labelled roadmap
  status. A merged change is not live verification or deployment. OS portability
  is not proof of backend/GPU parity.

Allowed: read issues/PRs/checks; create scoped issues and isolated branches/worktrees;
implement, test and push branches; request review; answer findings; merge only
through §3; close an issue only after its merged deliverable satisfies **all**
acceptance criteria, with evidence. An issue that requires live validation stays open
until that evidence exists. Do not close merely to tidy the board.

Hard stops:

1. Never approve a PR, dismiss a review, or merge unreviewed work. Authentication,
   secrets, migrations, deployment and dependency majors remain **human-gated**,
   regardless of elapsed time or green checks.
2. Never push to `main` or another long-lived branch, force-push, rewrite published
   history, delete branches, or overwrite another agent/operator's work.
3. **Never restart Guardian or any service/systemd unit**, deploy code or change
   live service configuration. The loop's model traffic passes through Guardian.
   For code changes run `scripts/pre_restart_check.py` in an appropriate isolated
   environment; report unavailable checks, never claim a pass. Put exact required
   restart/deployment actions and validation in the operator batch, never execute them.
4. Never commit secrets, weaken TLS/credential isolation, print key maps, or expose
   private payloads in logs, reports or fixtures. Existing raw capture is separate
   from metadata audit; do not silently change its operator-configured behavior.
5. No production fault injection or experiments. Use isolated fixtures/test services;
   preserve intentional platform policies such as Windows TTS no-auto-return.

## 2. Per-run budget and preflight

Each run starts fresh; unused budget does not accumulate:

- Remediate open PR findings in coherent tested batches within the run budget.
- At most **one new issue implementation** across the two repositories.
- At most **one deliberate PR merge** per run, never a merge sweep.
- At most **30 minutes of exploratory debugging/evaluation**; if more is needed,
  record the evidence and next scoped step. No speculative refactor/dependency churn.
- No mandatory code output: a reproduced defect, rejected hypothesis, measured
  comparison, documented blocker or honest no-op can be the correct outcome.

Preflight is read-only apart from fetching remote refs and recording evidence:

1. Record UTC start, both relevant checkout branches/SHAs and dirty paths. Fetch
   current remote state. Never stash/reset/clean/switch a production checkout.
   Use isolated worktrees from current `origin/main`; claim expected write scope
   and stage explicit paths only (never `git add -A`, `git add .`, `commit -a`).
2. Check Guardian service health without restarting: `systemctl is-active
   llama-guardian` plus an unauthenticated `/v1/models` probe (expected 401 on the
   configured local endpoint). Distinguish HTTP liveness from inference readiness.
   If unhealthy, report evidence and recovery need; no new code changes this run.
3. Read targeted current handoff and roadmap sections for the intended task, not
   entire archives. Identify existing claims, PRs and prerequisite evidence; avoid
   duplicate work and preserve outstanding auth/deployment findings.
4. Send `task_started` through the `agent-activity-log` skill (fail-open).

## 3. Open PRs first — review and merge gate

Inventory open PRs in **both** repositories. Read latest comments, reviews, inline
threads, commits and checks, not only a review-state badge. Give existing findings
priority over new features. Treat each finding as a hypothesis: reproduce/inspect,
fix correct in-scope findings; otherwise answer with evidence and an explicit
handoff/blocker where needed. Do not silently discard unresolved findings.

After a coherent change batch: run relevant tests/compile checks, inspect the diff,
commit with the implementing-model identity and push the branch. Post a PR comment
whose body is **exactly `/review` after the final push**, including every later
push. Use the repository's designated sender (currently a human account here;
bot senders are ignored). A requested, queued, failed, cancelled or incomplete
review is not a completed independent review. Follow up through the existing loop;
do not create a new scheduled follow-up without operator authorization.

A PR is **merge-ready** only when:

- an independent review of the **current head SHA** is finished;
- every outstanding review finding is fixed or answered with evidence, and review
  threads are resolved only after their findings have been handled;
- all required checks pass on that head; skipped/neutral results need an explicit
  policy justification and do not replace an absent required test/review;
- the change satisfies its acceptance criteria and human gates are satisfied.

A closing comment normally records change, evidence and next action; it is **not**
a precondition for readiness. Record the waiting anchor explicitly. For this
account the waiting window is **2 hours**, not 24. It starts after the last
outstanding review is answered; with no findings, at the later of completed
current-head review and green checks. Never before the latest push. Any new
activity needing a reply/fix/decision must be handled first and restarts the
clock. Quiet/old PRs without completed review never qualify by age.

Before the one deliberate merge:

1. Refresh against the current base. If refreshing creates a new head, request and
   finish its review and recalculate the anchor. **Then pin the candidate SHA before
   any SHA-sensitive verification**:
   `verified=$(gh pr view <N> --json headRefOid --jq .headRefOid)`.
2. For **that exact pinned SHA**, read all latest comments/reviews/commits, verify
   completed review, handled findings/threads and elapsed anchor, rerun relevant
   checks, and confirm clean merge state against the current base. Handle new
   information, not merely vetoes. No operator intervention or unmet human gate.
   Do not replace the pin with a newer head after this verification; if either
   head or base changes, stop and restart the complete gate for a newly pinned SHA.
3. Merge only the same verified pin:
   `gh pr merge <N> --squash --match-head-commit "$verified"`.
   A head mismatch means stop/revalidate, never blindly retry.
4. Record anchor, elapsed window, review/check/thread evidence and merge SHA.
   Fetch and check the resulting main in an isolated worktree. Report any failure
   as priority remediation; do not repair production or restart a service.

Red CodeQL/CI is not waived because it appears pre-existing. Inspect annotations
and diff/base evidence, refresh/rerun appropriately and investigate or hand off;
never dismiss alerts or weaken tests to obtain green. If review infrastructure is
broken (including Caretaker's tracked infra handoff), report it; no self-review substitute.

## 4. Issues, roadmaps and evidence-led debugging

After PR remediation, select the highest-risk executable issue not already owned
or in a PR: security/correctness/reliability and operator incidents before feature
breadth. Search before creating issues. Every verified fixable defect needs its
own tracker with sanitized reproduction and acceptance criteria; grouped gap
registers do not replace those trackers. Do not duplicate an existing issue.

If no higher-priority issue consumes the budget, select one row from either
canonical roadmap whose dependencies are **satisfied by evidence**, not merely
merged. G1's live exit gate, for example, is not passed by a merged commit alone.
Record the choice and why it outranks the alternatives; do not starve Caretaker
or quality work by treating Guardian feature rows as the only standing objective.

Claim the issue with `loop-claim: <agent> <UTC timestamp>` and expected write scope
before building. A conflicting claim younger than 24 hours means coordinate or
choose another row; never race an active owner or edit a sibling's worktree.

For debugging: sanitized minimal reproduction → failing regression/contract test
where feasible → measured cause evidence → smallest fix → same-workload recheck
and broader regressions. Include non-default settings, cancellation/timeout,
error/denial paths and fault injection where relevant. Distinguish imported-env,
fixture, product and deployment failures. Change approach when evidence stops
improving; no repeated blind retries or unsupported causal claims.

Open a focused PR and request current-head review. Use bare `Closes #N` only when
merge fulfills every criterion; use `Refs #N` if operator restart/live verification
is still needed. Preserve **planned / implemented / tested / deployed** separately.

## 5. Bounded innovation and engineering quality

Innovation is a standing objective, not permission for churn. When no higher-risk
executable work consumes the run, choose one falsifiable hypothesis tied to a
roadmap outcome (latency, resource use, reliability, developer diagnosis or a
capability gap). An investigation can be the run's deliverable without new code.

- Reuse local helpers first. For a real external design choice consult dated
  primary docs/releases/standards; record sources and version/date. “Newest” or
  “state of the art” alone is not an adoption criterion.
- Compare the existing baseline with at most **two** alternatives on the **same**
  reproducible workload and pinned environment. Declare numeric acceptance/stop
  thresholds, time/resource limits, owner and cleanup before execution. First
  establish missing baselines; never invent a passing SLO after seeing results.
- Measure relevant p50/p95/p99 latency, throughput, errors, CPU/RSS/buffer/queue
  bounds, resource-release/recovery time and cost as appropriate. Preserve protocol,
  auth/privacy and exact-target invariants; performance does not excuse regressions.
- For platform work record OS/architecture/Python/backend/hardware and native
  evidence. Mocked portable tests are not proof of Windows/macOS/GPU support.
- Record **adopt / reject / defer** with evidence and next issue. Adoption still
  requires focused tests, PR review/checks, waiting window and human gates.

## 6. Report and failure handling

Post one factual run report on [tracking issue #30](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/30):

```markdown
## Run <YYYY-MM-DD HH:MM> UTC
- window / owner / both repository SHAs / preserved dirty paths:
- PR remediation / one merge or none / current-head review and CI evidence:
- waiting anchor / eligibility / latest activity and base revalidation:
- issue and roadmap row / dependency evidence / implemented-tested-deployed status:
- debugging: reproduction, cause evidence, regression result (or not applicable):
- innovation: hypothesis, baseline/candidates, thresholds, results, adopt/reject/defer:
- platform / protocol / security coverage, unavailable checks and known gaps:
- next intended action / blockers / operator decisions:
- restart batch: exact units/commands and validation, NEVER executed here:
```

Name unrun/failed checks honestly. Link commits, issues, PRs and measurements;
never include secrets or private raw captures. Send AAL `task_achieved`,
`task_failed` or `blocked` matching the actual outcome (fail-open).

Unclear authorization or uncertain safety → fail closed for that action and
continue only independent authorized work. Unhealthy Guardian or unavailable
GitHub/runners → report the concrete blocker; do not invent successful evidence.
A no-op is valid; do not manufacture tasks just to fill the slot.
