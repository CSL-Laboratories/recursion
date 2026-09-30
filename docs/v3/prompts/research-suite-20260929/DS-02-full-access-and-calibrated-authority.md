---
title: DS-02 — Full Access and calibrated authority
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-02 — Full Access and calibrated authority

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

This research informs the capability-vector contract behind Full Access, grant administration, break-glass elevation and auditability, so the grant schema can be frozen. It is blocking because `OR-005` ratifies a maximum `sovereign` preset that may explicitly include `grant-admin` and modify its own grants attributably, while the operative constitution forbids self-modification until a reviewed reconciliation exists. Audit-retention and audit-erasure authority remain separate unresolved capabilities that must not be bundled into grant administration.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane is analysis only and grants no authority; a model recommendation is never an Owner decision. Audit-retention/erasure stays a separate unresolved Owner question in `docs/v3/DECISION-REGISTER.md`, and the `full` edition must never be treated as implying `sovereign`.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md` (lane R4)
- `docs/v3/SECURITY.md`
- `docs/v3/EXECUTIVES.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`

## Research questions

1. What is the minimal orthogonal capability vector that expresses expansive Full Access without a single ambient boolean, covering control-plane writes, credential administration, billing authority, publication, merge, deployment, service control and audit-policy change?
2. How do grant/revoke inheritance, delegation depth and non-widening subset rules compose across project, host, account, pool and endpoint scope?
3. What not-before/expiry, single-use step-up and revocation-epoch semantics prevent stale or replayed authority from admitting an effect?
4. What authentication strength, reason capture and automatic expiry make break-glass elevation attributable, bounded and recoverable rather than ambient?
5. How can explicit `grant-admin` permit a `sovereign` actor to modify its own grants while keeping model prose incapable of creating authority, with a schema-checked kernel action and before/after receipt?
6. What threat model covers confused-deputy, privilege accumulation, authority bundling, replay and stale-parent admission, and which threats are untested by signatures alone?
7. Which prior capability-OS, just-in-time-elevation, break-glass-administration and dangerous-mode designs demonstrate enforceable, auditable maximum authority, and what are their documented limits?
8. How is audit retention distinguished from grant administration, and what proof states make historical receipts non-concealable while permitting scoped raw-artifact deletion?
9. What acknowledgement and display of blast radius makes the effective capability set visible without reducing Full Access to a warning-only rubber stamp?
10. What recovery exists when a sovereign grant mutation was mistaken, given that receipts cannot be silently rewritten and states after an effect reconcile forward?
11. What deterministic test proves that installing `full` never enables maximum `sovereign`, and that a child cannot widen parent scope, limits, lifetime or delegation?
12. Which competing option — a confirmed single boolean or a preset-by-edition — fails, and by what decisive falsifier?

## Deliverable

- A source-pinned capability/grant matrix with classification, confidence and expiry for each high-impact verb.
- A threat model with per-threat falsifiers and the option tradeoffs for one-step versus staged elevation.
- A recommendation tied to a competing option and a decisive falsifier, plus the grant-mutation receipt shape.
- Open Owner questions, explicitly including audit-retention/erasure and step-up cadence.

## Non-goals

- Do not grant, widen, revoke or simulate any real authority or credential.
- Do not reopen `OR-005`/`OR-014`, and do not promote a recommendation into an Owner decision.
- Do not decide audit-retention or audit-erasure policy.
- Do not edit constitution, auth, config, cron, spending or deployment state.
- Do not include private project identity, credentials, account identifiers or local paths.
- Do not equate a signature, edition name or preset with a capability grant.

## Completion gate

- Every capability claim is pinned to a primary source or explicitly marked unverified.
- The matrix covers each control-plane, credential, billing, publication, merge, deployment, service and grant-administration verb.
- Every recommendation names a competing option and a decisive falsifier.
- `OR-005`/`OR-014` questions are neither reopened nor promoted, and audit retention stays separate.
- No protected path is edited and no runtime/provider/auth/cron/PAUSE/spending effect occurs.
- The deliverable passes a privacy and secret scan with no identifiers or local paths.
- Owner questions are listed separately from model recommendations.
