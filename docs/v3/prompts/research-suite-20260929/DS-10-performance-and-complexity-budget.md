---
title: DS-10 — Performance and complexity budget
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-10 — Performance and complexity budget

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Establish v2 and reference baselines across startup, idle resource use, install size, imported modules, process count, event/replay behavior, CLI latency and privileged versus optional code, then decide whether v3's added machinery is justified. Agenda lanes R12 and R17 block a lean core and any mandatory subsystem until every mechanism has one owner, a measured benefit and an ablation.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. Option 2 is owner-ratified and is not reopened; this lane measures whether it and its mechanisms earn their cost.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/ARCHITECTURE-OPTIONS.md`
- `docs/v3/CURRENT-AND-MIGRATION.md`
- `docs/v3/STATE.md`

## Research questions

1. What exact v2/reference baseline commit and command establish each dimension: startup, idle CPU/RSS/wakeups, install, binary and dependency size, imported modules, daemon and process count, event/replay/WAL behavior, CLI latency, and privileged versus optional lines or complexity?
2. Which counters are observable on the coordinator-small and workstation profiles, and which must be reported `unavailable` rather than zero?
3. How are the coordinate optimization dimensions reported separately so no scalar hides a failed quality, authority, privacy, resource or recovery floor?
4. How is every mandatory mechanism mapped to exactly one owner, and where do duplicate state, routing, process, context or plugin paths remain?
5. Which measured budgets may be set only after a baseline, and what falsifier invalidates each budget?
6. For monolith, microservice and small-kernel/plugin packaging, what matched end-to-end scenario, total resource cost and maintenance burden distinguish the options?
7. How does the ablation study work: disable each major subsystem and identify which accepted outcome degrades, with a named outcome and floor?
8. Which current mechanisms lack a user-visible, safety or measured efficiency benefit and therefore must not become mandatory core?
9. How do disabled paths (unselected engine, provider, node, plugin, indexer, contributor route) prove zero model or network call, zero plugin import, zero worker process and zero periodic work, per the `docs/v3/ARCHITECTURE-OPTIONS.md` commitments and PERF-003?
10. What is the per-mechanism and per-option disposition rule (adopt, simplify, defer, delete), and what evidence blocks promotion?
11. How are quality, owner review burden and maintenance cost included so an efficiency gain that shifts defects or review cost downstream is rejected?
12. Which Option 2 implementation-boundary falsifiers remain open, and which measurement would resolve each?
13. What is the minimum measurement set that could still change the Option 1, 2 or 3 choice, and what is already decisive without further spending?

## Deliverable

A source-pinned report with: a claim table (claim, source, version/date, classification, confidence, expiry trigger); the v2/reference baseline definition; an owner map for each mandatory mechanism; an ablation plan and results template; a monolith versus microservice versus small-kernel/plugin comparison; a recommendation tied to a named competing option and decisive falsifier; and open Owner questions.

## Non-goals

- No runtime implementation and no benchmark executed.
- No language, store, binary IPC or framework selection.
- No numeric product minimum, budget or threshold invented.
- No aggregate score hiding a failed hard floor.
- No reopening of the ratified Option 2 architecture.
- No personal hardware inventory treated as design data.
- No provider, model or route calls.
- No modification of `docs/v3/PERFORMANCE-PLAN.md` or any protected path.

## Completion gate

- The v2 baseline commit and command are pinned for each dimension.
- Every claimed improvement is compared against v2 or a named simpler baseline.
- Each mandatory mechanism has one owner, one ablation and a user-visible, safety or measured-efficiency justification.
- Budgets are set only after measurement and carry a falsifier.
- Disabled-path cost has a hard-failure test rather than a timing tolerance alone.
- Every recommendation names adopt, simplify, defer or delete plus a decisive falsifier.
- Hard floors are reported before any trade-off or aggregate summary.
- Protected paths are untouched; privacy and secret scan are clean.
