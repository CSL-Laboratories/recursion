---
title: DS-27 — Post-M1 construction state and projection
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-27 — Post-M1 construction state and projection

This is a bounded construction handoff, not a public Deep Research brief. It is inert until Lucas explicitly invokes it in a new session; creating, reading or listing it launches nothing and authorizes no provider call, write, effect, spend or configuration change. It prepares the exact packet that would build `docs/v3/ROADMAP.md` Milestone 2.

## Purpose

Specify the first bounded construction packet for canonical state and compatibility projection: one fenced single-writer store, rebuildable projections, versioned migrations, an action/outbox path, backup/restore, and read-only legacy imports. This is blocking because `docs/v3/STATE.md` records that task, assignment, run/evidence, accepted-result and fabric stores do not yet form one fenced aggregate, so the owner-selected rehearsal cannot begin until its aggregate boundary, complete writer allowlist and fencing protocol are machine-checkable.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. M1A, M1B and M1C are already merged inert at the recorded baseline and must not be rebuilt, reopened or edited; this packet extends them through a separately specified bridge if a mapping is needed. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication, push or merge is authorized. Do not select a product language, store or framework. Do not reopen recorded Owner decisions (OR-021 through OR-039). The public Deep Research lane receives only public or synthetic briefs; this construction packet may reference repository paths but must never export private content, transcripts, credentials or account identifiers.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/STATE.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `docs/v3/CURRENT-AND-MIGRATION.md`
- `docs/v3/M1C-INTEGRITY-CONTRACTS.md`
- `docs/v3/M1A-WIRE-PROFILE.md`
- `docs/v3/construction/next-work-20260914/README.md`
- `docs/v3/prompts/research-suite-20260929/README.md`

## Research questions

### Branch, worktree and task-binding discipline

1. What exact reviewed predecessor commit is the `TASK_BASE`, and what isolated worktree path and branch name does the packet freeze before any edit?
2. What is the full preflight sequence (ownership, cleanliness, remote state, PAUSE, capacity, source-commit verification) that must pass before inference or writes?
3. How is the parent goal, v3 objective, source commit, allowed paths, non-goals and acceptance commands bound into one machine-checkable GoalSpec before launch?

### Allowed paths and protected boundaries

4. Which exact new paths may this packet create, and how is every protected path (`CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `scripts/`, `.env`) excluded by construction?
5. How does the packet prove it did not modify M1A/M1B/M1C fixtures, schemas, oracles or wire profiles?
6. Which existing read-only inputs are consumed as dependencies without being rewritten?

### Synthetic execution-attempt/lease aggregate rehearsal

7. What is the minimal synthetic execution-attempt/lease aggregate, with no task/run/result, remote or publication writes, and exactly what identities, statuses, expiry, reservation and result/source digests does its lease projection carry?
8. What aggregate type/id, contiguous sequence, event/command id, idempotency key, causation/correlation id, writer id, monotonic fence epoch, logical/controller time, previous/current event digest, schema version and typed payload does each event carry?
9. How does the rehearsal produce exact replay parity, and how is projection parity kept distinct from replay parity?
10. How is the complete legacy-writer enumeration and common fence epoch demonstrated before the rehearsal is admitted?

### Writer, projections, migrations and outbox

11. How does exactly one fenced writer admit an event per aggregate at a monotonically increasing sequence, and how are cross-aggregate operations expressed as an explicit saga/outbox boundary?
12. Which projections are transactionally synchronous and which are eventually rebuildable, and how is each rebuilt from durable events plus immutable artifacts?
13. What versioned migration and read-only legacy import adapters are specified, and how do they refuse to fabricate missing or malformed state?

### Backup, restore and failure gates

14. What consistent selected-event-store plus artifact snapshot, restore-onto-fresh-host, key/credential exclusion and forward/rollback migration proof is required?
15. How are the crash, duplicate, stale-lease, disk-full, migration, backup, restore and projection-rebuild gates each injected and observed?
16. Which malformed, conflicting, torn, unauthenticated or stale inputs must produce `REFUSED` or `RECOVERY_REQUIRED`, and how is the import/replay state machine preserved?
17. How does forward-only effect reconciliation handle failure before prepare, after prepare, during the external call, after the effect but before receipt, and after receipt but before projection?

### Independent review and publication boundary

18. What independent review is required for the exact candidate, and what evidence must the reviewer reproduce rather than accept from the implementer?
19. How does the packet end at a local candidate commit with no push, publication or merge, and what external attestation records the result?

## Deliverable

A machine-checkable construction packet specification: the frozen predecessor commit, worktree and branch; the exact allowed new paths and protected-path exclusions; the aggregate/event/lease field contract; the writer allowlist and fence protocol; the projection, migration, outbox, backup/restore and legacy-import requirements; the full failure-gate inventory with expected `REFUSED`/`RECOVERY_REQUIRED` outcomes; the forward-only reconciliation procedure; the acceptance command inventory; and the independent-review and no-push/no-merge boundary. It is a proposed packet, not an executed result.

## Non-goals

- No push, publication or merge to any branch.
- No rebuilding, editing or reopening M1A/M1B/M1C.
- No product language, store, framework or wire-format selection.
- No live provider, model, network or paid call.
- No real external effect, migration cutover or OR-015 rehearsal.
- No modification of protected paths or existing specifications.
- No SQLite migration or write outside the synthetic rehearsal boundary.
- No fabricated replay parity or projection parity claim.
- No self-review presented as independent verification.

## Completion gate

- The predecessor commit, worktree, branch and allowed paths are frozen and machine-checkable.
- The writer allowlist and common fence epoch cover the exact chosen aggregate boundary.
- The synthetic execution-attempt/lease aggregate is defined with no external effects.
- Event, lease-projection and idempotency field contracts are explicit and closed.
- Crash, duplicate, stale-lease, disk-full, migration, backup, restore and projection-rebuild gates each have an expected outcome.
- `REFUSED` and `RECOVERY_REQUIRED` semantics are stated for every malformed or stale input class.
- Forward-only effect reconciliation covers all five crash positions.
- The packet ends at a local candidate commit with no push, publication or merge.
- Independent review reproduces the exact-candidate gates and records any missing independence.
- Protected paths and M1 fixtures are untouched and the secret scan is clean.
