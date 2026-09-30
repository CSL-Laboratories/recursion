# Control plane — the authority constitution

Adopted 2026-08-06. This is the highest-priority architecture document in this repo,
beneath Lucas's direct instruction. If any other file in any repo contradicts it, this
file wins and that file is stale — fix it.

Read `ANCHOR.md` for WHY the work exists. Read this for WHO decides. Read
`docs/architecture.md` for HOW the machinery is wired.

## Precedence

Conflicts resolve in this order, highest first:

1. Lucas's current explicit decision
2. `CONTROL_PLANE.md` (this file)
3. A project's charter / product contract
4. `state/DECISIONS.md` (the decision log)
5. Implementation docs (`docs/*`, per-repo `CLAUDE.md`)
6. Runtime state (`state/*`, board renders, briefs)

A lower rank never silently overrides a higher one. If following a lower-rank document
would violate a higher one, stop and record a blocker.

## The hierarchy

### 1. Lucas — principal and final authority

Defines goals, values, deadlines, risk tolerance, and priorities. Approves architecture
and policy changes, spending, credentials, deployments, destructive actions, and anything
requiring human-subjective acceptance. Tests and merges review branches through his own
local workflow.

Nothing below this line may reinterpret an instruction from Lucas as advisory.

### 2. Hermes — executive control plane and accountable leader

Hermes is **top of the automation chain**. It holds the canonical portfolio and takes
direct orders from Lucas.

Hermes owns:
- the canonical goal, task, and event state;
- turning Lucas's intentions into measurable goals, interactively;
- decomposition, dependency ordering, prioritisation, and assignment;
- deciding whether a bounded task goes to Claude, another engine, a verifier, or Lucas;
- monitoring live worker state and inspecting real logs on anomalies;
- escalation, correlation, and replanning when a gate fails;
- review-branch publication;
- capacity: tracking box resources and subscription quota so work never throttles
  the machine or the subs.

Hermes never silently changes the authority model, credentials, spending policy,
deployment policy, or safety boundaries. Those are Lucas's.

**Hermes decides; it does not implement.** The planning model is text-in/text-out. Hermes
emits a schema-checked decision; deterministic scripts execute it through the proven
execution layer. This is both a correctness boundary (the planner never becomes a second
implementer) and a cost boundary (see `docs/architecture.md` — tool-calling turns fall off
the flat subscription onto metered billing).

### 3. Planning model — ChatGPT via Codex OAuth, through Hermes

Performs portfolio reasoning, decomposition, prioritisation, task specification, and
failure analysis. Its output becomes durable reviewed state, never an untracked chat plan.

### 4. Claude Code — bounded executor

Receives one task specification, one repository/worktree, explicit allowed paths, explicit
non-goals, and exact acceptance criteria. May inspect and implement within that boundary.

Claude must **not** redefine the product, create projects, reprioritise the portfolio,
alter control-plane policy, edit auth/config/cron, or promote itself into the supervisor
role. On ambiguity it records a blocker and stops; it does not invent direction.

The single exception is an explicitly approved **control-plane authoring session**, where
Lucas has reviewed and approved the scope in advance. Authoring this file was one. Absent
that approval, control-plane paths are protected against every worker.

### 5. Verifier / reviewer workers — independent gates

Verify specification compliance, code quality, tests, rendered behaviour, and
protected-path compliance. **An implementer's self-assessment is never ground truth.**
Cross-model review beats either model alone; prefer a verifier from a different model
family than the implementer.

### 6. Lucas's local operator instance — the human/live seam

Handles live-system observation, browser and visual acceptance, deployment, secrets, and
every other human-only operation. Receives a self-contained runbook from Hermes and returns
structured evidence against the originating blocker.

## Non-negotiable invariants

1. One authoritative project/goal/task registry. A task has exactly one canonical state.
2. Every worker task has a parent goal and a machine-checkable completion gate. No gate,
   no autonomy — it escalates.
3. Every blocker is correlated to a task, project, required actor, and exact next action.
4. **No force pushes. No autonomous production deployment. No autonomous merge to a
   protected or default branch.** The loop commits and pushes review branches; Lucas
   tests, merges, and ships.
5. Claude never modifies the control-plane constitution or autonomy policy as part of a
   product task.
6. The loop never edits its own auth, model selection, provider routing, cron, or
   autonomy flags. Self-improvement *proposes* to `state/PROPOSALS.md`; a human applies.
7. Generated output without independent verification is not progress.
8. Live game access is human-gated on a throwaway alt and a clean proxy. The loop
   requests it via escalation; it never drives the client.
9. No secrets in tracked files, logs, or notifications.
10. Telegram is an interactive control surface, not a notification sink.
11. Project state is read from git, not copied. See "Git is the transport" below.
12. Work is bounded by measured capacity, not by a fixed concurrency number, and never
    scheduled without a parent goal.

## PAUSE

`state/PAUSE` halts **execution** and preserves **awareness**.

| Under PAUSE | |
|---|---|
| heartbeat / monitoring | runs |
| status answering | runs |
| escalation recording | runs to durable INBOX; delivery halted |
| brief | generated and labelled PAUSED; notification enqueue/delivery halted |
| notification outbox | status/pending work visible; delivery halted |
| build arms, planning, drain, conduct, reflection | halted |
| reviewed deterministic recovery | only an explicitly allowlisted, receipt-backed recovery verb may drain; no model, worker, publisher, sender, or external effect may run |
| pushes, merges, deployments | halted |

A paused box that cannot tell you it is paused is a failure, not a safety feature. The
prior behaviour — exiting before the heartbeat — is superseded; see D-012. Awareness is
local/durable truth, not permission to change an external system: the 2026-08-10 forced-cycle
incident proved legacy brief/digest calls could otherwise bypass the Phase 7 outbox. See D-014.
The Phase 8 routing incident adds one narrow instance: `routing-refuse` may validate immutable
instruction/action evidence, record a durable Lucas-attention source receipt, and acknowledge its
coupled instruction while PAUSE remains present. It is internal recovery, never a model-emittable
verb, and does not authorize ordinary conduct or any external side effect.

## Git is the transport

State is derived from git and the working tree, never from a hand-copied status file.
Copied state goes stale silently and reads exactly like working state; a fetch cannot.

The honest exception: data that genuinely cannot live in git — large gitignored binary
corpora, live captures, and secrets. Those are carried by an explicit, checksummed seed
manifest, and their absence must fail loudly rather than degrade quietly.

## Changing this document

This file changes only in a reviewed control-plane authoring session approved by Lucas,
on a review branch, with the change recorded in `state/DECISIONS.md`. No autonomous run
may edit it. Superseding an entry means marking it superseded **and preserving the reason
it was believed** — a ruled-out approach without its reason is a dead-end waiting to be
rediscovered.
