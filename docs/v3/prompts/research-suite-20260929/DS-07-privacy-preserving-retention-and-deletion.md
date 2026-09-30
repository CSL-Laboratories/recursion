---
title: DS-07 — Privacy-preserving retention and deletion
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-07 — Privacy-preserving retention and deletion

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Specify how raw private artifacts expire while durable lineage remains auditable, using scoped encryption, verified blob and key deletion, cache/backup propagation and a minimal signed non-sensitive tombstone. Agenda lane R9 blocks a defensible deletion contract until proof states for disconnected nodes, backup expiry and key granularity exist; deletion must never be reported complete while an unexpired recoverable copy remains.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. The owner-selected retention direction in `docs/v3/STATE.md` is fixed; refine mechanics, do not reopen it. No encryption, key management or deletion is performed.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md`
- `docs/v3/STATE.md`
- `docs/v3/SECURITY.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/CURRENT-AND-MIGRATION.md`

## Research questions

1. What key granularity (per project, per retention class, per artifact, per epoch) allows cryptographic erasure without breaking content-addressed deduplication or artifact identity?
2. How is key destruction verified when the key lives in an OS store, a KMS or a disconnected node, and what evidence distinguishes proven destruction from assertion?
3. Which recoverable copies must be enumerated (active store, CAS replicas, caches, backups, provider-side copies, disconnected spools), and what is the proof state for each?
4. How is a backup-expiry boundary recorded, audited and surfaced, and what happens when a copy outlives its recorded boundary?
5. What is the minimal signed tombstone that proves deletion without retaining raw content or an unnecessarily equality-revealing global digest?
6. How do content addressing, deduplication and shared keys create deletion-isolation failures, and which negative fixtures expose them?
7. What size, timing or access-pattern leakage lets a public observer infer private project membership, and how is that bounded?
8. How do legal hold and pin or retention overrides interact with the deletion proof-state machine without silently claiming completion?
9. What should the proof-state machine be, relative to `requested -> local-content-deleted -> key-destroyed -> propagation-pending -> verified-complete | partial | blocked | legal-hold | expiry-bound`?
10. What independent evidence is required before any node may report `verified-complete`, and what blocks that claim?
11. Where does NIST SP 800-88-style media sanitization guidance stop applying to logical and provider-held copies?
12. Which retention defaults (for example 30 days successful, 90 days failed or disputed) remain owner-selected and outside this lane's decisions?
13. What stop conditions prevent publishing a deletion claim while any recoverable copy lacks evidence or a recorded expiry boundary?

## Deliverable

A source-pinned report with: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a deletion proof-state machine; key-granularity and propagation options with tradeoffs; negative fixtures; a recommendation tied to a named competing option and decisive falsifier; and open Owner questions.

## Non-goals

- No key management, encryption or deletion implementation.
- No provider cache or backup deletion performed or requested.
- No claim that cryptographic erasure proves every copy is gone.
- No reopening of the ratified retention direction or default durations.
- No legal advice; legal hold remains an owner and legal decision.
- No secrets, keys, real digests or real artifact content in any output.
- No private project identity or content in public or synthetic briefs.

## Completion gate

- Every proof state maps to an observable predicate or is explicitly `unavailable`.
- No completion claim is allowed while a recoverable copy lacks evidence or a recorded expiry boundary.
- Key-granularity options each name a tradeoff and a falsifier.
- Content-address and deduplication leakage negative fixtures are specified.
- Owner retention direction is not reopened and no mechanism is promoted to a default.
- Disconnected-node and backup-expiry proof states have explicit stop conditions.
- Protected paths are untouched; privacy and secret scan are clean.
