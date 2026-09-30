---
title: DS-01 — Pause, cancellation, and effect fencing
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-01 — Pause, cancellation, and effect fencing

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

This research informs the exact per-effect-class semantics of the future `checkpoint`, `interrupt` and `drain` pause family plus an independent emergency stop, so the pause schema can be frozen. It is blocking because R3's transition/crash matrix and constitutional reconciliation options are prerequisites named in the agenda, and because a wrong disposition either strands in-flight work or admits an undeclared post-pause effect. It must also state where the desired v3 product policy differs from the operative constitution.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. In this lane the operative `CONTROL_PLANE.md` PAUSE is immediate halt; `checkpoint` is a selected future default that stays non-operative under `OR-020` in `docs/v3/DECISION-REGISTER.md` until the exact `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md` is applied.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md` (lane R3)
- `docs/v3/EXECUTIVES.md`
- `docs/v3/SECURITY.md`
- `docs/v3/STATE.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`

## Research questions

1. For each effect class — model turn, local transaction, subprocess, remote/disconnected action, read-only gate, publication, and ambiguous external effect — what exactly may finish, must be cancelled, or must be quarantined under `checkpoint`, `interrupt`, `drain` and emergency stop?
2. Which OS, workflow-engine, job-scheduler and agent-harness cancellation/shutdown contracts provide enforceable process-tree termination and quiescence, pinned by version and license?
3. What admission-epoch and fencing rule makes "no undeclared post-pause effect" falsifiable, and where can a checkpoint policy still leak one?
4. How is a model-turn boundary detected when a harness exposes only process/session boundaries, and what is the conservative fallback when it is unobservable?
5. For an already-dispatched effect without a receipt, what provider-identity/status evidence proves reality without blind retry or a false rollback claim?
6. Which transition/crash cells (before prepare, after prepare, during call, after effect before receipt, after receipt before projection) differ across the four modes?
7. How do disconnect and reconnect change permitted local completion and signed evidence spooling, and when must spooled evidence be revalidated before admission?
8. What precedence ordering (emergency, revocation, interrupt, checkpoint) holds across every effect class, and where does it observably break?
9. Which dispositions are implementable by a typed adapter versus the kernel, given an adapter may declare a narrower but never a wider disposition?
10. What acknowledgement and terminal labels (`ambiguous`, `quarantined`, `stale`) distinguish stopped, unknown and completed effects in owner-facing status?
11. What deadline miss should deterministically escalate `checkpoint` or `drain` into containment, and how is that outcome tested?
12. What are the competing constitutional reconciliation options — keep immediate halt, adopt the full future family, or stage adoption — and what decisive test separates them?
13. Under what conditions, if any, may a final bounded turn reach a checkpoint on capacity exhaustion without creating a new-effect escape hatch?

## Deliverable

- A source-pinned claim table with classification, confidence and expiry for every per-effect and per-mode claim.
- A transition and crash matrix covering all seven effect classes and all four modes with no silent gaps.
- Options with tradeoffs, each tied to a competing option and its decisive falsifier.
- An explicit statement of where desired v3 policy differs from the operative constitution, plus open Owner questions.

## Non-goals

- Do not decide the PAUSE default, apply the amendment, or treat future modes as operative.
- Do not reopen `OR-004` or `OR-020`, and do not promote a research recommendation into an Owner decision.
- Do not select a process-containment technology, language, or runtime implementation.
- Do not design or edit control-plane, auth, config, cron, or autonomy state.
- Do not include private project identity, transcripts, or local paths in any public packet.
- Do not recommend a mode without a competing option and a decisive falsifier.

## Completion gate

- Every per-effect claim is pinned to a primary source or explicitly marked unverified.
- The matrix covers every listed effect class and mode with no silent gap.
- Every recommendation carries a competing option and a decisive falsifier.
- The current immediate-halt PAUSE and `OR-004`/`OR-020` status are neither reopened nor promoted.
- No protected path is edited and no runtime/provider/auth/cron/PAUSE/spending effect occurs.
- The deliverable passes a privacy and secret scan with no local paths or identifiers.
- Owner questions are listed separately and no model opinion is presented as an Owner decision.
