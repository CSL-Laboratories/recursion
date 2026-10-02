---
title: R-OPS — Operations and lifecycle facet
status: ready-for-explicit-invocation
last-updated: 2026-10-02
parent-goal: goal-recursion-v3-harness-orchestrator
invocation: self-contained; supplying this document launches the research
---

**Research directive.** You are the lead researcher. This document is your complete brief and your explicit authorization to research; it is not inert. Conduct the investigation described below and produce the deliverable, then check every item of the completion gate. Do not ask which topic to research, do not request further approval, and do not return a scoping plan, research plan, methodology, timeline, outline, or clarification request — those are not acceptable outputs. Execute the research now and return the final report with findings and citations; if you catch yourself planning, convert the plan into the research immediately.

# R-OPS — Operations and lifecycle facet

This handoff supplies the missing language-agnostic **operations and lifecycle** facet for the D-039
outline (review items **C2, C3, C4, C5**): telemetry, update/release/disaster-recovery,
backup-restore/cutover, durable memory, and editions/distribution. It defines neutral contracts a
reimplementation must reproduce; it selects no tooling.

## Baked context

### Shared Recursion context

Recursion is a provider-neutral agent platform. The development target is Recursion v3: a small
deterministic kernel with typed adapters; native and upstream execution; goal-scoped assistants;
configurable context and compaction; plugins and editions; and Windows/Linux operation and recovery.
Authority: Lucas is the final owner; Hermes-led control was retired (D-017). Evidence discipline:
classify each claim as source-documented, locally observed, inferred or unknown; `unknown` is never
zero. No language, store, framework, transport or vendor is selected here. Recall the accepted
platform floor: Windows 11 25H2 x64 primary; Linux 6.12/6.6 with cgroup v2; WSL2 dev/test only;
local NTFS/ext4 only; power loss is an abrupt VM reset at the commit boundary.

### Lane context

- **Telemetry (C2):** a read-only telemetry evidence-provider interface exists in direction: source,
  collector, schema/version, scope/role/attempt, route/pool, unit, cumulative-vs-delta, inclusion
  set, observed time, coverage, integrity; a validated value is separate from a raw report; adapters
  are candidates and are unexercised. Telemetry is local-by-default, metadata-minimal, and separates
  quotas, costs and human time.
- **Update/release/DR (C3):** six update modes with observable landing, an update/release/DR matrix
  with no agreed RPO/RTO, and cutover/restore/reverse-reader rehearsal are unassigned to any facet in
  the outline.
- **Backup/restore (C3):** a backup must be a coherent canonical cut plus CAS closure plus a replay
  that reacquires authority; rollback admits a prior configuration under current rights without
  replaying effects.
- **Durable memory (C4):** a semantic evidence cache (provider-neutral, content-addressed artifacts)
  and the Execution Context Capsule are defined; multi-project memory and the capsule/memory
  distinction are thin.
- **Editions/distribution (C5):** editions gate optional capabilities (for example a `remote`/`full`
  set) as opt-in; configuration, edition gating and migration have no cross-platform conformance
  corpus or agreed human syntax.

## Purpose

Produce the decision-ready, **language-agnostic operations-and-lifecycle facet**: responsibilities,
boundaries, neutral contracts and state machines for telemetry, update/release/DR,
backup/restore/cutover, durable memory and editions/distribution; artifacts; interfaces; provider-free
test/evidence obligations with planted mutants; the language-neutral vs language-specific split; and
reconstruction acceptance evidence with falsifiers.

## Authority and boundaries

The authority, scope and privacy constraints in `## Baked context` are the operative limits. Propose
amendments to the operative constitution; never edit it. Public Deep Research receives only public or
synthetic briefs. **DO NOT select a language, store, framework, transport, telemetry vendor, backup
system or distribution channel.** Do not decide an Owner question recorded in D-019, D-027, D-028 or
D-036, and do not reopen accepted decisions. No destructive action, provider call, install, spend or
live-host action is performed. Do not state a legal conclusion; licensing and distribution are
counsel questions (G-32). No measured performance, durability, RPO/RTO or eligibility claim.

## Inputs to read

None from this repository: the telemetry, update/release/DR, backup/restore, memory and editions
direction is summarized in `## Baked context`. The session must retrieve and cite the public primary
sources it relies on (official telemetry/OpenTelemetry-style conventions, database/WAL durability and
fsync documentation, backup and point-in-time-recovery practice, release/rollback and canary
methodology, content-addressed storage, and package/distribution documentation) and pin each claim to
a URL and retrieval date. Treat every mechanism as prior art, not a selection.

## Research questions

1. What is the neutral telemetry evidence-provider contract: fields, counter honesty (cumulative vs
   delta, inclusion set, `unknown` vs zero), adapter boundary, and how telemetry never becomes an
   authority or a fabricated counter?
2. What is the neutral update/release model: the update modes, atomic activation/landing, version and
   rollback rules, and how a release identity and boot/recovery receipt are represented?
3. What is the neutral DR model: the failure domains, the recovery-point/recovery-time concepts, the
   recovery cut, and how canonical state is reconstructed from a verified checkpoint plus the
   committed suffix plus outstanding reservations, without reanimating stale execution?
4. What is the neutral backup/restore/cutover contract: coherent canonical cut + CAS closure + replay
   + authority reacquisition; reverse-compatibility reader; rollback without replaying effects.
5. What is the neutral durable-memory contract: semantic evidence cache vs capsule vs working set;
   invalidation and rehydration; multi-project scoping and privacy; how memory never refreshes
   authority or leaks across audiences.
6. What is the neutral editions/distribution contract: opt-in edition gating, install/verify
   receipts, migration, and how the product stays distribution-neutral?
7. What artifacts and facts must be language-neutral, and what may stay implementation-specific?
8. What provider-free vectors and planted mutants would evidence a faithful reimplementation, and
   what would falsify reconstructability?

## Deliverable

An operations-and-lifecycle facet specification containing: responsibilities and non-ownership;
boundaries and interfaces; neutral contracts and state machines for telemetry, update/release/DR,
backup/restore/cutover, durable memory and editions; artifacts; provider-free test/evidence
obligations with planted mutants; the neutral vs language-specific split; and reconstruction
acceptance evidence with falsifiers. Every recommendation names a competing option, a decisive
falsifier and what it does not prove.

## Non-goals

- No selection of a language, store, framework, transport, telemetry vendor, backup system or
  distribution channel.
- No execution, destructive test, provider call, install, spend or live-host action; no repository
  edits.
- No reopening of D-019/D-027/D-028/D-036 or of accepted decisions.
- No legal, licensing or distribution conclusion; G-32 is counsel-gated.
- No measured performance, durability, RPO/RTO or eligibility claim.
- No modification of a protected path or accepted Owner decision.

## Completion gate

- Neutral contracts are specified for telemetry, update/release/DR, backup/restore/cutover, durable
  memory and editions, each with its state machine/invariants and refusal behavior.
- The telemetry contract preserves counter honesty and the `unknown`-is-not-zero rule and never
  becomes an authority.
- The update/release and DR contracts specify version identity, atomic landing, rollback, the
  recovery cut and checkpoint-plus-suffix reconstruction; they state RPO/RTO as unmeasured.
- The backup/restore contract specifies coherent cut + CAS closure + replay + authority
  reacquisition and reverse-compatibility reading.
- The memory contract distinguishes semantic cache, capsule and working set, and forbids authority
  refresh and cross-audience leakage.
- The editions contract is opt-in, install/verify-receipted and distribution-neutral.
- Provider-free test/evidence obligations with planted mutants, and reconstruction acceptance
  evidence with a falsifier, are stated.
- Every recommendation names a competing option, a decisive falsifier and what it does not prove; no
  mechanism or vendor is selected; no legal conclusion is stated. Privacy and secret scan are clean.
