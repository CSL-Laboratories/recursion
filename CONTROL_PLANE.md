# Governance — Recursion

Adopted 2026-09-28. The highest-priority authority document in this repository, beneath
Lucas's direct instruction. If another file contradicts it, this file wins and that file is
stale.

Read `ANCHOR.md` for WHY the work exists and what is in scope. Read
[docs/v3/START-HERE.md](docs/v3/START-HERE.md) for how the product is organized.

Recursion is a **single product**: the v3 agent harness. This repository builds that product.
It does not supervise other repositories. The earlier Hermes-led control plane (v1/v2) was
retired on 2026-09-28; the complete prior tree is at tag `pre-v3-clean-cut-20260928`.

## Precedence

Conflicts resolve in this order, highest first:

1. Lucas's current explicit decision
2. `CONTROL_PLANE.md` (this file)
3. The v3 product specifications (`docs/v3/*`)
4. `state/DECISIONS.md` (the decision log)
5. Implementation docs, experiments and evidence
6. Runtime state

A lower rank never silently overrides a higher one. On conflict, stop and record a blocker in
`state/INBOX.md`.

## Roles

### Lucas — principal and final authority

Defines goals, priorities, risk tolerance and acceptance. Approves architecture, product and
policy changes, spending, credentials, deployment and destructive actions. Tests and merges
every review branch himself.

Nothing below this line may reinterpret an instruction from Lucas as advisory.

### Coding agents — bounded executors

An agent receives one task, one repository/worktree, explicit allowed paths, explicit
non-goals and one machine-checkable acceptance gate. It works inside that boundary. It does not
redefine the product, reprioritise work, change this document or the protected paths, or
promote itself into a supervisor role. On ambiguity it records a blocker and stops.

### Independent verifiers

Verify specification compliance, behaviour, tests and protected-path compliance. An
implementer's own PASS is never acceptance. Prefer a verifier from a different model family
than the implementer.

## Non-negotiable invariants

1. **No force pushes. No autonomous merge. No autonomous deployment.** Agents commit to
   a review branch named `csl-<N>-<slug>`, optionally prefixed with a GitHub username,
   and open that branch for review; Lucas merges and ships. pr-lint matches `CSL-[0-9]+`
   case-sensitively, so a lowercase branch name alone does not pass. The `CSL-N` key in
   the PR title satisfies the check.
2. Every task has a parent goal and a machine-checkable gate. No gate, no work — it escalates.
3. Generated output without independent verification is not progress.
4. No secrets in tracked files, logs or notifications.
5. Live provider/game/host actions, spending, quota resets and credential use are human-gated.
   Agents request them; they never perform them.
6. Protected paths change only in an explicitly approved authoring session, never as part of a
   product task.
7. Evidence classes stay distinct: source-documented, locally observed, inferred and unmeasured
   are never conflated. A paper or a model consensus never proves local fit.

## Protected paths

`CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `.env`, and any credential or
authentication configuration. A task that touches them fails its gate.

## Changing this document

Only in a reviewed authoring session Lucas approves, on a review branch, recorded in
`state/DECISIONS.md`. Superseding an entry preserves the reason it was believed — a ruled-out
approach without its reason is a dead-end waiting to be rediscovered.
