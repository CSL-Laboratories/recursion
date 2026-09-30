---
title: DS-34 — Decision journaling and calibration
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-34 — Decision journaling and calibration

This is an inert Deep Research handoff. It launches nothing, authorizes no provider call, write, spend or configuration change, and is inert until Lucas explicitly invokes this file in a new session. Run it as a public/synthetic research lane and include no private decision content.

## Purpose

Decide a decision-log schema and a review cadence that make forecasts falsifiable and actually reviewed, so that individual project decisions can be scored against their predicted outcomes instead of reconstructed by hindsight. This is blocking because the repo already keeps a D-entry log with falsifiers and a monthly review cadence in `state/DECISIONS.md`, and because the morning-brief and kill-criteria discipline depends on criteria written before outcomes. The research must reconcile with the existing D-entry convention rather than propose a parallel log, and must connect to `research/PROMPTS.md` R7.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed reconciliation, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. Do not reopen accepted Owner decisions or ANCHOR scope, and do not select a tool, store or format as a product decision. Existing D-entries are history and are not to be rewritten.

## Inputs to read

- `research/PROMPTS.md` (R7 decision journaling and calibration)
- `state/DECISIONS.md` (existing D-entry convention, falsifiers, review cadence)
- `ANCHOR.md` (kill criteria, review throughput)
- `CONTROL_PLANE.md` (decision precedence, decision log)
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/PLANNING.md`
- `docs/v3/EXECUTIVES.md`
- `docs/v3/PRODUCT.md`
- `state/BOARD.md`
- `state/INBOX.md`

## Research questions

### Calibration and forecasting evidence

1. What does the forecasting and calibration literature (Tetlock and successors) establish about measurable skill, and under what conditions does it transfer from a forecasting tournament to individual project decisions?
2. What accuracy, resolution and calibration measures are appropriate for a low-volume individual log, and which are misleading at small sample sizes?
3. How does continuous versus one-shot feedback change calibration, and what does the record show about how quickly individual judgment improves?
4. What do the studies report about domain expertise versus general forecasting skill for technical and project decisions?

### Decision-journal formats and reviewed fields

5. Which decision-journal formats are documented in practice, and which fields are actually revisited versus written once and abandoned?
6. What is the minimum field set that makes a decision reviewable: decision, options, choice, rationale, expected outcome, falsifier and review trigger?
7. How do you record a decision whose outcome is one of several gradations rather than a binary, and where do journals fail once they become a compliance ritual?

### Base rates and reference-class forecasting

8. How is a reference class chosen for a personal project decision, and what does the literature say about the failure to find an appropriate class and the outside-view correction?
9. What base rates are available or constructible for recurring decisions in a small operation (project length, rework, abandonment, review debt, estimate error)?
10. How should base rates be recorded, updated and reconciled with inside-view detail as local history accumulates, without overfitting to a handful of cases?

### Failure modes and biases

11. How are hindsight bias, outcome bias and motivated reconstruction detected and mitigated in a log that is reviewed after the outcome is known?
12. What makes success criteria vague, and how is vagueness caught at entry time rather than at review time?
13. What does the record show about journals written but never reviewed, and what trigger or cadence reliably drives the review?
14. How are survivorship, selection and small-sample effects kept from making a lucky call look like skill?

### Schema and review cadence

15. What decision-log schema should be used, with required versus optional fields, and how does it map onto the existing D-entry template?
16. What review cadence and trigger conditions are evidence-backed: time-based, event-based (falsifier fired, project closed, threshold crossed), or a combination?
17. What state transitions should a decision record carry (open, held, failed, unclear, superseded) and how is a fired falsifier treated?
18. How are superseded and amended decisions preserved with the original reasoning, as `CONTROL_PLANE.md` requires, without rewriting history?

### Reconciliation with Recursion's conventions

19. How does a candidate schema reconcile with `state/DECISIONS.md`, the monthly first-of-month review and the "every decision gets a falsifier" rule, and what does it add without duplicating?
20. How should the morning brief and kill-criteria discipline feed the decision log so predicted outcomes are recorded before the outcome is observed, and what machine-checkable condition flags a record missing a falsifier or past its review date without a model call?

## Deliverable

- A source-pinned claim table (claim, source, version/date, classification as fact/inference/hypothesis/proposal, confidence, expiry or falsifier trigger).
- A decision-log schema with required and optional fields, shown as a reconciliation with the existing `state/DECISIONS.md` D-entry template.
- A review cadence with specific trigger conditions, distinguishing time-based from event-based triggers and stating the evidence for each.
- A base-rate and reference-class recording approach with the small-sample caveat stated.
- One recommendation tied to a named competing option and a decisive falsifier, plus open Owner questions.

## Non-goals

- No runtime, tool, journaling application or automation implementation.
- No language, store, format or tool selection as a product decision.
- No rewriting, renumbering or editing of existing `state/DECISIONS.md` entries.
- No reopening accepted Owner decisions or ANCHOR scope.
- No private decision content, transcripts, local paths or credentials in any public packet.
- No provider or model calls, and no model-graded review performed.
- No invented base rate presented as a measured local value.
- No claim that a low-volume individual log yields statistically strong calibration.

## Completion gate

- Every calibration, bias and base-rate claim is pinned to a primary source with version or retrieval date, or marked unverified.
- The schema reconciles with the existing D-entry convention and states what it does not duplicate.
- Every required field has a stated reason it is reviewed later, not merely recorded.
- The review cadence names concrete trigger conditions and their evidence.
- Hindsight-bias and vague-criteria mitigations are stated as concrete entry-time or review-time rules.
- Small-sample limits are stated plainly and no calibration strength is overstated.
- Every recommendation names a competing option and a decisive falsifier.
- No existing D-entry is edited and no protected path is modified.
- Open Owner questions are listed separately from research findings.
