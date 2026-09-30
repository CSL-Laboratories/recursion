---
title: DS-26 — Telemetry accounting and honest status
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-26 — Telemetry accounting and honest status

Run as a Deep Research route Lucas explicitly approves, with public-source and repository-reading tools. This file is inert until Lucas invokes it in a new session; creating, reading or listing it launches nothing and authorizes no provider call, write, effect, spend or configuration change.

## Purpose

Decide how v3 qualifies telemetry evidence providers, normalizes provider counters, separates cost classes, and renders status without fabrication. This blocks the dashboards, budget reservations, route learning and optimization claims in `docs/v3/ROADMAP.md` Milestone 8 and `docs/v3/TELEMETRY.md`: without a falsifiable accounting contract, a visible activity stream becomes a fabricated percent-complete, and a missing counter becomes a false zero.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs, never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. Do not reopen completed M1A/M1B/M1C contract work or recorded Owner decisions (OR-021 through OR-039), and do not select a product language, store or framework.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/STATE.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/EXECUTION-FABRIC.md`
- `docs/v3/EXECUTIVES.md`
- `docs/v3/CONTEXT.md`
- `docs/v3/prompts/research-suite-20260929/README.md`

## Research questions

### Evidence providers and qualification

1. Which provider usage APIs (OpenAI, Anthropic, Google, Cursor) currently expose read-only, account-scoped usage, and what exact fields, windows, granularity and retention do their public documents specify?
2. Which CLI-native usage events exist for the supported harnesses, and which of them are cumulative totals versus per-request deltas?
3. What is the `ccusage` project's exact repository, pinned version, license and data model, and what does its license permit for a bundled or optional adapter?
4. For local resource monitors, which host counters (CPU, RSS, disk, I/O, process, handle, wakeup) are reliably available on the coordinator-small and workstation profiles?
5. How should deterministic token estimators be labeled so they are never mistaken for provider-reported usage?
6. What qualification receipt should each evidence provider produce before its numbers may enter a dashboard, and how is applicability bound to a route, model, endpoint and date?

### Counter semantics and normalization

7. Under current public documentation, do cached tokens count toward API tokens-per-minute, and where does Anthropic exclude cache reads from input tokens-per-minute; how do these rules differ from any subscription debit?
8. When is a counter cumulative versus per-request, and how must cumulative `model_usage` totals be reconciled so a crash, reset, fork or session rotation neither double-charges nor erases consumption?
9. Which helper, classifier, compaction and retry calls are excluded from a provider's headline counter, and how are they counted exactly once at the parent segment?
10. How are subscription, metered, inferred and unavailable quantities represented as distinct classes, with throttles and resets recorded as availability changes rather than task-budget changes?
11. What normalization is required before summing cache reads and cache writes, and which edge cases (no-future-work, zero-miss, partial final total) must fail closed?

### Cost separation, latency and identity

12. How are human review minutes and host resource cost reported separately from inference cost unless an explicit Owner-accepted valuation weight exists?
13. How is bridge/control latency measured separately from inference latency, and how are input stability and resource attribution kept distinct?
14. Which correlation identifiers (`project_id`, `goal_id`, `arm_run_id`, `task_id`, `segment_id`, `route_id`, route digest, provider session, source/checkpoint/candidate/evidence/verifier) must every usage record carry?

### Dashboards, honest status, learning and privacy

15. Which operator views are required (running, waiting, budget, backlog, regressions, compaction health, headroom, exact actions), and how does each avoid converting absence into a healthy or complete status?
16. What rule guarantees visible activity is an observation, never percent-complete, and what wording and data model prevent an inferred progress bar?
17. What constraints must the adaptive route scorer obey (no new providers, no widened authority, no lowered verification), and how are its state versioned, explained, reset and exported with shadow/offline evaluation before use?
18. What metadata-minimal privacy default excludes prompts, raw tool output, source snippets and private artifact content, and how does aggregation resist revealing project membership?

### Measurement families

19. For each family (quality, capacity/cost, context quality, system performance, research quality, continuity/safety), which counters are required, which are optional, and which are explicitly `unavailable` rather than zero?
20. What falsifier, baseline and uncertainty statement is required before any telemetry-derived optimization default is promoted?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a provider-qualification matrix with explicit unknown cells; a counter-semantics table covering cached reads/writes, cumulative versus delta, helper exclusion, and subscription versus metered versus inferred versus unavailable; a normalization and exactly-once counting procedure; a cost-separation and latency-attribution model; a correlation-identifier schema; a dashboard and honest-status specification; a route-learning constraint list; a privacy-default statement; a measurement-family counter map; and a recommendation tied to a named competing approach and a decisive falsifier, plus open Owner questions.

## Non-goals

- No runtime, schema or dashboard implementation.
- No provider, model, network or paid API call.
- No provider/account credential or live usage query.
- No language, store, framework or dashboard-stack selection.
- No fabricated counter, percent-complete, confidence percentage or quota conversion.
- No reopening of M1A/M1B/M1C contracts or Owner decisions.
- No modification of protected paths or `docs/v3/TELEMETRY.md`.
- No private repository content, paths, transcripts or account identifiers in any brief.
- No optimization default promoted without baseline, uncertainty and falsifier.

## Completion gate

- Every evidence provider has a pinned source, version/date and qualification receipt, or is marked `unavailable`.
- Every counter is classified as observed, modeled, inferred or `unavailable`, and no missing value is reported as zero.
- Cache reads and writes are normalized before summation, with the OpenAI/Anthropic/Google distinctions stated per endpoint.
- Cumulative totals, helper/compaction calls and retries are each counted exactly once with a stated reconciliation rule.
- Human review cost and host resource cost are separated from inference cost unless an accepted valuation weight exists.
- Bridge/control latency is separated from inference latency.
- Every usage record carries the full correlation-identifier set.
- The honest-status rule forbids fabricated percent-complete and requires unknown-as-unknown.
- The route-learning constraints and privacy defaults are stated with a falsifier.
- Protected paths are untouched and the secret scan is clean.
