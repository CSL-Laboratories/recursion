---
title: DS-06 — Capacity admission and review debt
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-06 — Capacity admission and review debt

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide which measured signals bound new branches, worktrees and concurrent ARM output, and how machine pressure is separated from human review debt so admission can adapt without inventing a constant. Agenda lane R8 blocks a construction contract for branch/worktree admission and safe pruning until this is evidence-backed.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane is measurement and policy research only; the owner-set hard ceiling and any numeric threshold remain Lucas's decision.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/CLI.md`
- `docs/v3/STATE.md`
- `docs/v3/CURRENT-AND-MIGRATION.md`

## Research questions

1. Which measured Git and worktree signals (status/index latency, changed bytes, disk and inode growth, process and handle counts) best bound concurrent branches and worktrees on the coordinator-small and workstation profiles?
2. What unreviewed-change volume and owner review service rate can be measured without exporting private code content, and how do they relate to branch age and integration-conflict frequency?
3. Which signal separates machine pressure (CPU, RAM, disk, I/O, pool quota) from human review debt, and what is the correct behavior when the two disagree?
4. How should admission combine machine headroom, pool headroom, goal budget, review credit and an owner hard ceiling, per the `admission = min(...)` shape in `docs/v3/PERFORMANCE-PLAN.md`?
5. What hysteresis, low/high-water marks and one-slot-at-a-time expansion or contraction prevent oscillation and reviewer whiplash?
6. How do stale-branch age, repeated integration conflicts and abandoned worktrees feed safe pruning rules that never delete unpushed, referenced or review-only work?
7. What is a safe pruning protocol for merged, superseded, stale or orphaned worktrees and branches, including remote refs and published review branches?
8. Which counters are observable per host and per route, and which must remain `unavailable` rather than assumed zero?
9. How can a review service-rate trace be modeled or controlled without exposing private content or transcripts?
10. How does the rule "a large machine must not generate work faster than evidence can be reviewed" become specific, testable refusal conditions?
11. How should refusal, queue age and inferred limits surface through CLI concepts such as `recursion branch inventory|explain|prune-safe` without writing canonical state from a view?
12. What falsifier shows adaptive admission genuinely outperforms a fixed limit, and what would show it merely hides a growing backlog?
13. What evidence would justify deleting the adaptive policy in favor of a simpler owner-set hard ceiling?

## Deliverable

A source-pinned report with: a claim table (claim, source, version/date, classification, confidence, expiry trigger); measurable machine and review signals with units and observability; candidate admission functions with hysteresis and refusal conditions; options with tradeoffs; a recommendation tied to a named competing option and decisive falsifier; and open Owner questions. No threshold is presented as a default.

## Non-goals

- No branch, worktree or Git implementation; no `git` command is run.
- No change to current capacity policy or the constitution.
- No owner hard-ceiling number selected; 75 is historical input, not a v3 constant.
- No private repository content, paths, transcripts, host names or account identifiers in any public or synthetic brief.
- No provider, model or route selection.
- No pruning, deletion or mutation of any real branch or worktree.
- No schema or CLI change.
- No claim that machine headroom alone protects review capacity.

## Completion gate

- Primary sources pinned by version/date; local structural claims separated from public evidence.
- Every machine and review signal traces to an observable measurement or is explicitly `unavailable`.
- Every recommendation names a competing option and a decisive falsifier.
- The adaptive-versus-fixed comparison has a baseline, workload and stopping rule.
- Owner decisions are not reopened and no number is promoted to a default.
- Protected paths are untouched; privacy and secret scan are clean.
- The report states which signals were measurable locally versus modeled.
