---
title: DS-05 — Single-writer migration and effect rollback
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-05 — Single-writer migration and effect rollback

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

This research informs the exact checkpoint sufficient to fence a legacy writer and transfer one aggregate to v3, so the single-writer migration contract can be frozen. It is blocking because `OR-015` ratifies a first rehearsal on the synthetic local execution-attempt/lease aggregate with no external effects, and no common monotonic writer epoch currently fences the task, assignment, run/evidence, accepted-result and fabric paths. It must also define forward-only reconciliation for effects that cannot be rolled back once externally accepted.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane is design analysis over synthetic or copied fixtures only; it must not open a cutover, write canonical state, or run any external effect. It must not duplicate the bounded numeric reservation-ledger work assigned in `docs/v3/construction/next-work-20260914/03-sol-recovery-ledger.md`; that ledger concerns integer recovery-reservation arithmetic and is explicitly out of scope here.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md` (lane R7)
- `docs/v3/CURRENT-AND-MIGRATION.md`
- `docs/v3/STATE.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/construction/next-work-20260914/03-sol-recovery-ledger.md` (boundary only)

## Research questions

1. What exact import/replay progression and checkpoint condition is sufficient to fence every legacy writer for one aggregate without a second writable truth?
2. How do event-sourced strangler, expand/contract, saga/outbox, fencing-token and shadow-read patterns compare for the synthetic local execution-attempt/lease aggregate with zero external effects?
3. What machine-checkable enumeration and allowlist catches a new or renamed legacy writer rather than relying on prose inventory?
4. What proves import and projection parity — equal rendered state, equal event order, equal crash behavior, or all three — and how are they distinguished?
5. What fencing epoch, stale-epoch refusal and duplicate-command rule make competing or replayed writes fail closed at admission and execution?
6. What crash matrix must pass at every transition (before prepare, after prepare, during effect, after effect before receipt, after receipt before projection) with deterministic, zero-model reruns?
7. What backup, reverse-reader and rollback proof permits returning to the frozen legacy writer without merging two histories or repeating work?
8. For provider submissions, pushed refs, sent messages, credential changes and deployments, what forward-only reconciliation proves reality once an effect may have been externally accepted?
9. How are ambiguous or partially applied cross-aggregate saga steps blocked and reconciled without relabeling an external effect as rolled back?
10. What distinguishes a fabric-only state-contract fixture from the full `OR-015` rehearsal, and why does the fixture not satisfy the broad gate?
11. Which design choices (concurrency, retry, fixture volume, resource limits, optional model assistance) must stay configurable while the default gate remains deterministic and quota-free?
12. What competing cutover boundary (smallest aggregate plus bounded broker refinement versus broker-inclusive model) is supported, and what decisive test separates them?

## Deliverable

- A source-pinned per-aggregate migration/cutover/rollback table with classification, confidence and expiry.
- An exact import/replay/fence/rollback proof outline and a crash matrix for the synthetic rehearsal aggregate.
- A forward-only reconciliation taxonomy for externally accepted effects, each with a decisive falsifier.
- A recommendation tied to a competing option, plus an explicit statement of the reservation-ledger boundary and open Owner questions.

## Non-goals

- Do not perform a cutover, open a shadow writer, or write any canonical or operational state.
- Do not duplicate the numeric reservation-ledger work in `docs/v3/construction/next-work-20260914/03-sol-recovery-ledger.md`.
- Do not reopen `OR-015`/`OR-016` or promote a recommendation into an Owner decision.
- Do not claim rollback of an externally accepted effect or promise exactly-once semantics.
- Do not select a storage engine, language or persistence implementation.
- Do not edit constitution, auth, config or migration artifacts.
- Do not include private project identity, transcripts or local paths.

## Completion gate

- Every migration claim is pinned to a primary source or local read-only evidence, or marked unverified.
- The proof covers legacy-writer enumeration, fencing, replay parity, crash recovery, backup/restore and rollback.
- Forward-only reconciliation is defined for every externally accepted effect class.
- The reservation-ledger boundary is stated and no overlap with its assigned work is introduced.
- `OR-015`/`OR-016` are neither reopened nor promoted, and projection parity is not equated with replay parity.
- No cutover or protected path is touched and no runtime/provider/auth/spending effect occurs.
- The deliverable passes a privacy and secret scan; Owner questions are listed separately.
