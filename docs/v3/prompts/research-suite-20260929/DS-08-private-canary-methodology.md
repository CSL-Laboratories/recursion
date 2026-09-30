---
title: DS-08 — Private canary methodology
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-08 — Private canary methodology

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Define what `CANARY-01` can prove without leaking its identity or letting one successful run substitute for fault coverage. Agenda lane R10 blocks the first private characterization of a stable deterministic non-safety-critical business rule until its alias-only reference, test/fixture write boundary, private-approved route, independent clean rerun, failure injection and stop conditions are specified. The canary must not begin during this research campaign.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. The canary is referenced by alias only; never record its real identity, path, content or digest in tracked or public artifacts. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This campaign authorizes no canary execution, no private route call and no candidate commit.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md`
- `docs/v3/CURRENT-AND-MIGRATION.md`
- `docs/v3/STATE.md`
- `docs/v3/SECURITY.md`

## Research questions

1. How is `CANARY-01` referenced by alias only, with no real project identity, path, content, hash or transcript in tracked docs?
2. What can one canary prove about the migration and verification pipeline, and what can it not prove?
3. How is one stable deterministic non-safety-critical business rule selected and characterized without touching production code?
4. What test/fixture-only write boundary is machine-checkable, and which paths must be refused?
5. Which private-approved routes are eligible, and how is route approval bound without recording private identifiers?
6. What defines an independent clean rerun: clean checkout, fresh state, frozen input and deterministic expected output?
7. What does "one local candidate commit" permit, and how does it avoid publication, push or merge?
8. Which failure-injection fixtures (malformed input, mismatch, crash, missing dependency, stale state) must the canary survive?
9. What baseline comparison distinguishes real verification from a tautological or overfit test?
10. What disclosure rules govern what may be shared, with whom, and at what classification?
11. What stop conditions abort the canary: privacy, secret exposure, protected-path contact, ambiguity or an unverified claim?
12. How does a single successful canary avoid substituting for fault coverage or broader conformance evidence?
13. What durable proof demonstrates the canary did not begin during this research campaign?

## Deliverable

A source-pinned report with: a claim table (claim, source, version/date, classification, confidence, expiry trigger); an alias-only runbook shape; the exact machine-checkable write boundary; private-route eligibility rules; failure-injection, baseline, disclosure and stop conditions; a recommendation tied to a named competing option and decisive falsifier; and open Owner questions.

## Non-goals

- The canary must not run during this research campaign.
- No real project identity, path, content, transcript or digest in tracked or public docs.
- No production writes; tests and fixtures only.
- No publication, push, merge or deployment.
- No provider call; route eligibility is reasoned, not exercised.
- No new route, model or auth selection.
- No claim of conformance, release readiness or migration completion from one canary.
- No reopening of the ratified first-canary scope.

## Completion gate

- Alias-only reference is verified; no real identity or content appears in tracked output.
- The write boundary is machine-checkable and limited to tests and fixtures.
- Independent clean rerun and baseline comparison are specified.
- Failure injection, disclosure and stop conditions are listed and falsifiable.
- The report states the canary did not start and no candidate commit was created here.
- Private-approved route eligibility is stated without recording private identifiers.
- What the canary proves is separated from what it cannot prove.
- Protected paths are untouched; privacy and secret scan are clean.
