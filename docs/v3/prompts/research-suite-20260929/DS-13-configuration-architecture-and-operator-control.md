---
title: DS-13 — Configuration architecture and operator control
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-13 — Configuration architecture and operator control

Run this as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide whether one typed schema can make deep customization predictable and safe across files, the interactive CLI, Telegram, editions, goals, tasks, plugins, routes, nodes, PAUSE, Full Access and terminal layouts. This blocks the configuration freeze and the promise that settings are transactional, portable, explainable and rollback-safe. It is blocking because merge semantics, provenance, stale-version conflicts and migration behavior are still unproven.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This prompt must not change provider, model or authentication configuration. Configuration is never permission to weaken silent integrity, authority, privacy or verification invariants.

## Inputs to read

- `docs/v3/RESEARCH-AGENDA.md` (lane R15)
- `docs/v3/CLI.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/EXECUTION-FABRIC.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/foundation-research/mark2-research/mark3-comparison/architectures.md`

## Research questions

1. Which configuration encoding gives a deterministic canonical form and safe round-trip (typed structural validation, a canonical serialization for digests, and a human syntax only when duplicate-key, type and normalization fixtures pass)?
2. What is the smallest orthogonal setting set covering engines, tools, context, memory, routing, delegation, verification, scheduling, placement, resources, nodes, PAUSE and Full Access without untestable interactions?
3. How should merge operators behave: keyed override for preferences, intersection for authority, egress and budget ceilings, stable-identifier merge for plugin sets, and explicit authority for credentials?
4. How are effective value plus provenance (requested value, effective value, source scope or preset, merge operator, reason, application boundary, blocked status) computed and explained with zero model tokens?
5. How are narrowing and widening distinguished, and what authenticated acknowledgment and receipt are required before widening authority, privacy exposure, network access, spending or publication rights?
6. How do transactional edits handle stale-version conflicts, concurrent clients, partial failure and idempotent replay without last-write-wins?
7. Which fields are live, checkpoint-applied, goal-frozen or restart-required, and how are propagation and per-session application receipts proven?
8. How are migrations and rollback designed so an unknown field or invalid combination fails before any side effect?
9. How are credential references kept opaque and validated for presence without exposing secret values?
10. How do presets and feature flags compose across editions so a lean edition defers but never silently erases an accepted capability?
11. How do cross-platform paths and environment differences stay portable across Windows and Linux?
12. How do novice setup and expert file editing stay consistent because both compile from the same schema, and which pairwise or three-way fixtures prove it?
13. Which setting combinations cannot be explained or tested and must be rejected rather than shipped?
14. How is it guaranteed that no configuration combination weakens a silent integrity, authority, privacy or verification invariant?

## Deliverable

- A candidate versioned schema and the smallest orthogonal field set, with named gaps where a surface cannot yet be modeled.
- Written merge-operator semantics, provenance output, lifecycle and propagation table, and migration/rollback design.
- A configuration-complexity vector (field count, scope levels, cross-field constraints, lifecycle transitions, authority and billing edges, invalid-combination classes, round-trip and migration coverage).
- Options with tradeoffs and a recommendation tied to a competing option and a decisive falsifier, plus open Owner questions.

## Non-goals

- Selecting a language, parser, store, framework or file format as final.
- Editing configuration, credentials, auth, model selection, cron or autonomy flags.
- Implementing Full Access, PAUSE or sovereign authority.
- Reopening accepted Owner decisions or treating configuration as permission to weaken invariants.
- Designing hosted tenancy, edition commerce or a graphical client.
- Promising hot reload or last-write-wins conflict handling as a default.

## Completion gate

- One versioned schema covers every named surface, or every gap is named explicitly.
- Every merge operator and lifecycle transition is reviewer-checkable and testable.
- No combination ships that cannot be explained or tested.
- Every recommendation names a competing option and a falsifier.
- Stale-version, partial-failure and concurrent-client behavior is specified.
- Credential references are opaque, the secret scan is clean, and no protected path is edited.
- No configuration path can override a silent integrity invariant.
- Open Owner questions are listed and no decision is promoted.
