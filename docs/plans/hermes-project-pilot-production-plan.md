---
title: Hermes Project Pilot Production Plan
type: implementation-plan
status: proposed
last-verified: 2026-08-09
related:
  - CONTROL_PLANE.md
  - docs/architecture.md
  - docs/delegation-design.md
  - docs/autonomous-loops-architecture-and-roadmap.md
---
# Hermes project pilot production plan
## Objective

Build one production control plane with this hierarchy:

```text
Lucas (principal)
  -> Head Hermes (portfolio supervisor)
    -> Project Pilot / GPT Delegation Arm (one per project and Telegram topic)
      -> Claude implementation workers (Sonnet 5 or Opus 5)
      -> independent verifier worker and machine gates
    -> Head Hermes receives the terminal project report
  -> Lucas receives project-topic, head-channel, and direct-account notifications
```

Once Lucas approves a measurable goal and plan, the project pilot owns the autonomous
execution loop. It orients from the live repository and canonical state, decomposes the goal,
dispatches bounded workers, observes gates, retries within policy, escalates ambiguity, and
stops mechanically when the goal is achieved or a terminal blocker is reached.

This plan retains the existing rulings: the file/event task store is canonical; Kanban and
Telegram are views; Hermes decides in text; deterministic code actuates; project deploy keys
remain human-provisioned; workers publish review branches only; Lucas merges and deploys.
## Authority model

| Actor | Owns | Must not do |
|---|---|---|
| Lucas | goals, priorities, policy, spending, credentials, production, final acceptance | delegate irreversible authority implicitly |
| Head Hermes | portfolio, goal assignment, pilot lifecycle, capacity, cross-project priority, final reporting | implement product code or bypass policy |
| Project Pilot | one approved project goal, task DAG, worker selection, progress, retries, project-topic conversation | change portfolio priority, create projects, widen scope, merge, deploy, edit control policy |
| Claude Worker | one task, one worktree, allowed paths, exact gate, result report | plan the project, access other repos/secrets, publish, merge, deploy |
| Verifier | machine-gate evidence, diff review, task/goal verdict | modify the implementation it reviews |
| Actuator | execute schema-validated state transitions and runner verbs exactly once | reason, invent arguments, or execute arbitrary model-produced shell |

Direct communication with a project pilot is first-class. A message in a project's Telegram
topic becomes a project-scoped instruction for that pilot. Cross-project reprioritization,
scope expansion, spending, credentials, or policy changes are forwarded to Head Hermes or Lucas
rather than acted on locally.
## Canonical objects and state machines

Every object has a stable ID, version, timestamps, parent IDs, and an append-only event history.
All transitions use compare-and-swap semantics and an idempotency key.

| Object | Required fields |
|---|---|
| Project | id, repo, default branch, deploy-key reference, charter, topic, pilot, status |
| Goal | id, project, measurable outcome, acceptance suite, falsifier, budget, status |
| Task | id, goal_id, dependencies, allowed paths, non-goals, worker class, gate, status |
| Run | id, task_id, worker, worktree, base commit, PID, lease, deadline, usage, result |
| Decision | id, actor, verb, arguments, reason, goal_id, idempotency key, status |
| Escalation | id, goal/task/run, required actor, question, evidence, resolution |
| Delivery | event_id, destination, attempt, acknowledgement, status |
Goal states:

```text
draft -> approved -> active -> verifying -> achieved
                         |          |
                         +-> blocked+-> active
                         +-> abandoned
```
Task states:

```text
proposed -> ready -> leased -> running -> verifying -> passed
                       |         |          +-> failed -> ready (bounded retry)
                       |         +-> blocked
                       +-> stale -> ready
```

`achieved` is allowed only when the exact goal acceptance suite passes on the review-branch
commit, the independent verifier signs the evidence, all required tasks are terminal, and no
required escalation is open. That transition closes dispatch for the goal and emits one terminal
report. A model saying "done" is never itself the stop condition.
## Architectural rulings

1. **One input path.** The Telegram gateway authenticates, normalizes, and records an instruction.
   It does not execute terminal commands directly. Status queries may read through bounded APIs.
2. **One actuation path.** Every state-changing action enters a durable action queue and is executed
   once by the deterministic actuator. There is no `.decision-latest` singleton.
3. **One pilot per project topic.** The pilot has a durable identity and resumable topic session,
   but boots from canonical files and git every turn. Model memory is a cache, never truth.
4. **Workers are ephemeral and sandboxed.** They run as an unprivileged identity in a disposable
   worktree/container, without deploy keys or control-plane write access.
5. **Verification is independent.** Machine gates run first; a verifier that did not implement the
   change then inspects the diff and evidence. Only the publisher can push a passed branch.
6. **Notifications use an outbox.** Project detail, head summary, and Lucas's configured accounts
   receive the same event ID with retry and deduplication.
7. **PAUSE is interruptive.** It cancels active workers, prevents gates and publication, and blocks
   branch-changing maintenance. Awareness remains read-only.
## Phase 0 - Safe execution baseline

Start the next session from a clean worktree at current `origin/master`; do not build on the stale,
dirty local `master`. Preserve the existing worktree as user-owned. Create a reviewed control-plane
authoring branch and keep the remote `state/PAUSE` present.
Deliverables:

- Re-run the current suites and capture the baseline commit and deployed commit.
- Add failing controls for every confirmed audit defect before implementation.
- Verify GitHub protection on every registered default branch using an authenticated read-only API.
- Inventory notification destinations meant by "all accounts" and record stable destination IDs.
- Define rollback and backup/restore procedures for runtime-only state.
Exit gate:

- Baseline is reproducible; every new control fails on pre-change code; PAUSE is present; no product
  repo or protected branch changed.
## Phase 1 - Privilege, kill switch, and publication boundary
Deliverables:

- Run the gateway as a dedicated non-root service with `NoNewPrivileges`, minimal capabilities,
  private temp/devices, protected system/home paths, and a restrictive umask.
- Run workers under a separate unprivileged runner. Mount only their worktree and required read-only
  seed data. Do not mount `.env`, SSH keys, other repos, or control-plane state.
- Split publisher credentials from workers. Accept only canonical `loop/work/<task-id>` refs and
  compare the resolved destination against the registered default branch.
- Make PAUSE cancel process groups, wait for confirmed termination, and re-check before every gate,
  commit, state transition, and push.
- Update vulnerable runtime dependencies or document a reviewed exception with reachability evidence.
Required controls:

- An in-flight sleeping worker is terminated by PAUSE and cannot run its gate or publisher.
- `main`, `master`, `HEAD`, `refs/heads/main`, `refs/heads/master`, symbolic refs, and crafted
  refspecs are refused.
- A worker cannot read a deploy key, write outside its worktree, or invoke the publisher.
- `systemd-analyze security hermes-gateway` no longer reports an unrestricted root service.
## Phase 2 - Durable, idempotent control state

Keep the existing file/event architecture and strengthen it rather than replacing it.
Deliverables:

- Versioned schemas for projects, pilots, goals, tasks, runs, decisions, escalations, and deliveries.
- Atomic temp-write-plus-rename updates and locks that fail closed instead of proceeding unlocked.
- An append-only action queue with `pending/running/succeeded/failed/cancelled`, leases, attempts,
  result evidence, and idempotency keys.
- Goal IDs and task dependency IDs enforced by validators, not prompt instructions.
- A consumed/acknowledged instruction transition coupled to the resulting action IDs.
- Event-log rotation plus tested backup and restore for ignored runtime state.
Required controls:

- Replaying one instruction or decision ten times executes one action.
- A crash after dispatch but before acknowledgement recovers without double-launching.
- Two simultaneous planners cannot allocate the same task ID or advance the same roadmap item.
- A task cannot start unless its parent goal is active and all dependencies passed.
## Phase 3 - Single Head Hermes decision and actuation path
Deliverables:

- Remove direct terminal execution from the Telegram gateway's normal control path.
- Route each authenticated message into one durable instruction with principal, topic, project,
  reply correlation, and deduplication metadata.
- Head Hermes emits schema-validated portfolio or delegation decisions only.
- A continuously supervised actuator consumes the durable queue. Do not overlap planner, sync, and
  git mutation through colliding cron entries.
- Cost checks fail closed when provider or usage evidence is absent. Tool-using work is admitted by
  an explicit budget and routed to a worker, never performed by the planning turn.
- Replace truncated Anchor/decision/project inputs with precedence-aware, current projections.
Required controls:

- A Telegram `plan` request cannot be acted on once by the gateway and again by intake.
- An unhandled instruction remains visible; a handled one links to the exact successful action.
- Planner output containing Markdown, shell text, traversal, unknown verbs, or oversized prose is inert.
- Restarting the gateway or actuator preserves every pending instruction and action.
## Phase 4 - Persistent project pilots in Telegram topics
Deliverables:

- Extend `portfolio.tsv` or its companion project file with pilot ID, topic ID, charter, model policy,
  concurrency/budget, notification targets, and goal source.
- Bind one logical GPT project pilot to each active topic and, where useful, map it to Hermes's native
  project abstraction. Recursion state remains canonical.
- Give pilots bounded read APIs for their repo, charter, goals, tasks, runs, evidence, and project logs.
- Give pilots verbs to propose a plan, create dependency-linked tasks under an approved goal, request
  workers/verifiers, block/escalate, and submit a goal-verification request.
- Send direct topic messages to that pilot. Forward portfolio and policy requests to Head Hermes.
- Project registration remains two-step: Hermes prepares a pending row; Lucas provisions the key;
  registration proves clone/auth/freshness before activation.
Required controls:

- Messages in two topics never share project state, tasks, worktrees, or pilot memory.
- A pilot refuses a task outside its charter and cannot modify another project's priority.
- A direct status question returns live task/run/git evidence, not remembered prose.
- A project with an absent key, stale clone, missing gate, or missing charter cannot activate.
## Phase 5 - GPT delegation pilots and Claude worker hierarchy
Deliverables:

- Project pilots use GPT for orientation, decomposition, monitoring, and replanning, not implementation.
- Configure model aliases rather than hardcoding versions: Sonnet 5 for routine bounded implementation;
  Opus 5 for complex implementation or independent review. A human controls aliases and budgets.
- Worker envelopes include goal/task IDs, exact base commit, allowed paths, non-goals, acceptance,
  evidence contract, deadline, network policy, and ambiguity-stop instructions.
- Capture worker PID/process group, heartbeats, logs, usage, result JSON, and changed files.
- Scope verification compares `base..HEAD`, staged, unstaged, untracked, renamed, and deleted paths.
  A worker-created commit must not hide a change.
- Reject `true`, empty, self-modified, or structurally vacuous gates. Run retry scope checks before
  the retry gate. Require a valid result report for generated work.
- Machine gate and independent verifier verdict are stored against the exact commit OID.
Required controls:

- A worker that commits a protected-file change is rejected.
- A worker that edits tests/fixtures, writes outside scope, omits its report, or returns a no-op against
  a non-vacuous task cannot pass.
- A hung worker times out, loses its lease, is terminated, and produces one actionable escalation.
- Implementer and verifier cannot be the same run or silently mutate each other's output.
## Phase 6 - Autonomous measurable-goal loop

Each pilot runs an event-driven reconcile loop:

```text
observe truth -> plan ready work -> capacity admission -> dispatch workers
-> collect -> scope gate -> acceptance gate -> independent verify
-> pass/retry/block -> unlock dependents -> verify goal -> stop and report
```
Deliverables:

- Per-goal concurrency, token/cost, wall-clock, retry, and failure budgets.
- Backpressure from Lucas's review queue; passed branches do not become unlimited new generation.
- Explicit terminal behavior for achieved, blocked, abandoned, budget-exhausted, and paused goals.
- Recovery reconciliation across task files, processes, worktrees, git refs, events, and deliveries.
- No task creation after `verifying` begins unless verification reopens the goal with evidence.
Required controls:

- Scenario tests cover success, gate failure, ambiguity/assist, retry exhaustion, PAUSE, process crash,
  host reboot, stale base, publisher failure, and verifier disagreement.
- Once a goal reaches `achieved`, repeated scheduler ticks launch zero workers.
## Phase 7 - Reporting and notification fan-out

Status: reviewed and merged in PR #28. The current production checkout descends from that merge,
but Phase 7 has not been exercised against a human-visible transport. See
`docs/plans/hermes-project-pilot-phase7-handoff.md`.

Deliverables:

- Durable outbox destinations: project topic for detail, Head Hermes channel for portfolio summary,
  Lucas direct message, and any additional explicitly configured account/channel.
- Events for goal accepted, plan approved, worker launched, task passed/failed/blocked, human answer
  received, review branch published, goal verifying, goal achieved, and goal stopped.
- Notification deduplication by `(event_id, destination)`, retry with backoff, delivery acknowledgement,
  and a dead-letter escalation.
- Project pilots report terminal evidence to Head Hermes; Head Hermes emits the portfolio-level result.
Required controls:

- Simulated Telegram failure eventually delivers once without duplicate human-visible reports.
- Every terminal goal can be traced from Lucas's instruction to plan, workers, commits, gates, verifier,
  review branch, pilot report, head report, and all delivery acknowledgements.
## Phase 8 - Observed pilots and promotion

Status: the Phase 8 implementation was reviewed, merged through PR #29 as
`9422b9cc5bf4270b7184b9af6fa31cae8e6a6876`, and its review branch was pruned. The separate Phase 1
compatibility remediation was reviewed and merged through PR #30 as
`fa0263587479b3c43b0ba6bb61f5b176383f7cec`, then successfully deployed to `hermes-gateway` under
PAUSE using frozen Hermes `498061d8f1dcd855419b30e8729c869596a89ca6`. Baseline 17/17, every
Phase 1-7 control and positive audit, live Phase 1, branch protection, backup/restore, rollback,
dependency/OSV, and independent closure gates pass. Phase 8 execution remains deliberately inert:
its supervisor, pilot, actuator, reconciler, outbox, and worker units are inactive; model routing is
unset; capacity is zero; PAUSE is asserted; and no observed pilot, notification, or soak has
started. See `docs/plans/hermes-project-pilot-phase8-handoff.md`.

Run pilots through the real hierarchy, not by manually driving worker commands.

1. **Success pilot:** one small, reversible, build-verifiable goal from Telegram instruction to
   project pilot, Claude worker, independent verifier, review branch, terminal reports, and stop.
2. **Assist pilot:** force a real ambiguity; verify pilot -> Head Hermes -> Lucas -> answer ->
   same task resumes with the answer and does not double-run.
3. **Offline pilot:** one existing offline machine-gated task only. No live game action. Preserve
   the existing conformance/native-diff stack and prove no test, fixture, oracle, or baseline changed.
4. **Parallel pilot:** two project topics, one goal each, proving isolation, capacity control, and
   correct reporting without shared-state corruption.
5. **Soak:** 24 hours including scheduler ticks, gateway restart, host reboot, PAUSE during a worker,
   notification failure, and usage/cost reconciliation.

Production promotion requires:

- All required controls fail before their fix and pass after it.
- No default-branch push or autonomous deployment path exists.
- Branch protection is verified, not assumed.
- Backup/restore and crash recovery are demonstrated.
- The four pilots complete with no duplicate action or lost instruction/event.
- Review backlog stays within the configured limit.
- Lucas explicitly lifts PAUSE after reviewing the evidence bundle.
## Next-session execution order

1. Re-orient from current remote `CONTROL_PLANE.md`, this plan, and relevant commit messages.
2. Create a clean authoring worktree/branch from current `origin/master`; leave the existing dirty
   local worktree untouched.
3. Implement Phase 0 controls first and demonstrate every red result.
4. Complete Phases 1-3 before enabling any live project pilot or execution cadence.
5. Review and merge the security/control-plane branch through Lucas's normal GitHub workflow.
6. Implement Phases 4-7 in reviewable slices, with one design/schema change per branch.
7. Run Phase 8 while observed. Keep PAUSE set between pilot windows.
## Assumptions requiring confirmation

- V1 uses one hardened Telegram gateway and forum topic per project pilot, with isolated logical
  sessions. Separate bot identities are deferred unless Lucas requires visually distinct accounts.
- "All accounts" means project topic + Head Hermes topic/channel + Lucas direct message, with an
  extensible destination list for additional accounts.
- Sonnet 5 and Opus 5 are human-configured model aliases. The control plane never edits model routing.
- Workers create review branches only. Lucas remains the only merge/deploy authority.
## Explicit non-goals

- No live game automation in this plan.
- No autonomous protected-branch merge or production deployment.
- No replacement of the canonical file/event store with Hermes Kanban or model memory.
- No permanently burning model process per project; pilots are durable identities invoked by events.
- No widening autonomy before its acceptance and recovery controls have demonstrated both directions.
