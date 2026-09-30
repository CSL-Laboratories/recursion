---
title: DS-03 — Risk-scaled verifier independence
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-03 — Risk-scaled verifier independence

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

This research informs the verifier-independence vector, risk tiers, documented downgrade and release-blocking rules, so the verification contract can be frozen. It is blocking because `OR-006` ratifies risk-scaled independence whose exact dimensions and evidence are unresolved, and because a false green gate is the system's worst failure. Missing or weaker verification must never become a pass.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane may not select a product verifier, enable a route, or spend quota; it produces evidence and options only. The canonical independence vector and downgrade receipt are in `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md` (`CA-V3-005`/`CA-V3-006`) and this research must not define a second vector.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md` (lane R5)
- `docs/v3/EXECUTIVES.md`
- `docs/v3/SECURITY.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/STATE.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`

## Research questions

1. Which independence dimensions — separate task, context, session, model, family, provider, host/failure domain, and oracle — measurably improve defect detection for each risk class?
2. How do risk tiers `V0`–`V3` map to deterministic gates, same-family separation, cross-family review, cross-provider review and independent execution environment?
3. What documented downgrade shape satisfies routine reversible work without ever being reported as full independence?
4. What exactly must block for release, security-sensitive, control-plane and irreversible work when the required verifier is unavailable, and what must an Owner exception bind to be valid?
5. How is a blind, preregistered matrix designed so the implementer cannot persuade the verifier or leak candidate identity?
6. What fallback applies when independent routes are unavailable — human review, deterministic specialist/oracle, or cross-host rerun — and what are each one's limits?
7. How is verifier collision detected and refused when implementer and verifier share family, provider, account/pool, host, session or evidence channel?
8. What public software-assurance, reproducible-build, safety-case and multi-model evaluation evidence supports or contradicts each independence dimension?
9. Which metrics (first-review acceptance, escaped defects, disagreement rate, false-green gates) can distinguish dimensions without a hidden scalar score?
10. What evidence hierarchy prevents a stronger-model claim from replacing an exact deterministic gate execution?
11. What single-use receipt bindings, expiry and consume semantics prevent a downgrade exception from being reused or relabeled as `pass`?
12. What competing option (single universal strong verifier) fails, and by what decisive falsifier in a preregistered sample?

## Deliverable

- A source-pinned claim table classifying each independence dimension by risk class, confidence and expiry.
- A risk-tier table with minimum assurance, downgrade rule and blocking rule for each class.
- A preregistered blind matrix with exact candidates, treatments, metrics and deterministic gates.
- A recommendation tied to a competing option and a decisive falsifier, plus open Owner questions.

## Non-goals

- Do not issue, consume or simulate a downgrade receipt or Owner exception.
- Do not reopen `OR-006`, `OR-020` or the `CA-V3-005`/`CA-V3-006` vector.
- Do not claim any missing verification as a pass or a full independence result.
- Do not select a product verifier, route, provider or model, or spend any quota.
- Do not edit constitution, auth, config or acceptance state.
- Do not include private project identity, transcripts or local paths.

## Completion gate

- Every independence claim is pinned to a primary source or explicitly marked unverified.
- The tier table covers all four classes with downgrade and blocking rules and no silent gap.
- Every recommendation carries a competing option and a decisive falsifier.
- Missing or downgraded verification is never described as `pass` or full independence.
- `OR-006`/`OR-020` are neither reopened nor promoted, and no second vector is defined.
- No protected path is edited and no runtime/provider/auth/cron/PAUSE/spending effect occurs.
- The deliverable passes a privacy and secret scan; Owner questions are listed separately.
