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
outline. Packet F (R-AUDIT) confirmed telemetry, update/release/DR, durable memory and editions are
**unowned**. It selects no tooling.

## Baked context

### Shared Recursion context

Recursion is a provider-neutral agent platform. The development target is v3: a small deterministic
kernel with typed adapters; native and upstream execution; goal-scoped assistants; configurable
context and compaction; plugins and editions; and Windows/Linux operation and recovery. Authority:
Lucas is the final owner; Hermes-led control retired (D-017). Evidence classes: source-documented /
locally observed / inferred / unknown; `unknown` is never zero. No language, store, framework,
transport or vendor selected.

### Lane context

- **Telemetry:** a read-only telemetry evidence-provider interface is a *hypothesis*: source,
  collector, schema/version, scope/role/attempt, route/pool, unit, cumulative-vs-delta, inclusion set,
  observed time, coverage, integrity; a validated value is separate from a raw report; adapters are
  candidates and unexercised; telemetry is local-by-default, metadata-minimal, and separates quotas,
  costs and human time.
- **Update/release/DR:** six update modes with observable landing; an update/release/DR matrix with no
  agreed RPO/RTO; cutover/restore/reverse-reader rehearsal unassigned.
- **Backup/restore:** a backup is a coherent canonical cut + CAS closure + replay that reacquires
  authority; rollback admits a prior configuration under current rights without replaying effects.
- **Durable memory:** a semantic evidence cache (provider-neutral, content-addressed) and the
  Execution Context Capsule are defined; multi-project memory is thin. Memory never refreshes
  authority or leaks across audiences.
- **Editions/distribution:** editions gate optional capabilities (e.g. a `remote`/`full` set) as
  opt-in; install/verify receipts; the product is distribution-neutral. D-019/D-038 keep capabilities
  built-but-opt-in; G-32 licensing/counsel stays open.
- Simulated observers are not OS enforcement; a synthetic observer cannot substitute for native
  qualification.

## Purpose

Produce the decision-ready **operations-and-lifecycle facet**: responsibilities, boundaries, neutral
contracts and state machines for telemetry, update/release/DR, backup/restore/cutover, durable memory
and editions; artifacts; interfaces; provider-free test/evidence obligations with planted mutants; the
neutral-vs-implementation split; and reconstruction acceptance evidence with falsifiers.

## Authority and boundaries

The constraints in `## Baked context` are operative. Propose amendments; never edit the constitution.
Public Deep Research receives only public or synthetic briefs. **DO NOT select a language, store,
framework, transport, telemetry vendor, backup system or distribution channel.** Do not decide an
Owner question recorded in D-019, D-027, D-028 or D-036. No destructive action, provider call,
install, spend or live-host action. Do not state a legal conclusion; licensing/distribution are
counsel questions (G-32). No measured performance, durability, RPO/RTO or eligibility claim.

## Inputs to read

None from this repository by necessity: the direction is summarized above. Retrieve and cite public
primary sources (telemetry/observability conventions, WAL durability and fsync, backup and
point-in-time recovery, release/rollback and canary methodology, content-addressed storage, packaging/
distribution documentation) with a URL and retrieval date. Treat every mechanism as prior art.

## Research questions

1. The neutral telemetry evidence-provider contract: fields, counter honesty (cumulative vs delta,
   inclusion set, `unknown` vs zero), adapter boundary, and how telemetry never becomes an authority.
2. The neutral update/release model: modes, atomic activation/landing, version/rollback rules, release
   identity and boot/recovery receipt.
3. The neutral DR model: failure domains, recovery cut, and reconstruction from a verified checkpoint
   plus committed suffix plus outstanding reservations without reanimating stale execution; state
   RPO/RTO as unmeasured.
4. The neutral backup/restore/cutover contract: coherent cut + CAS closure + replay + authority
   reacquisition; reverse-compatibility reader; rollback without replaying effects.
5. The neutral durable-memory contract: semantic evidence cache vs capsule vs working set;
   invalidation/rehydration; multi-project scoping and privacy; no authority refresh, no cross-audience
   leakage.
6. The neutral editions/distribution contract: opt-in gating, install/verify receipts, migration, and
   distribution neutrality.
7. What artifacts/facts must be language-neutral, and what may stay implementation-specific?
8. What provider-free vectors and planted mutants evidence a faithful reimplementation, and what
   falsifies reconstructability?

## Deliverable

An operations-and-lifecycle facet specification with: responsibilities/non-ownership; boundaries and
interfaces; neutral contracts and state machines for telemetry, update/release/DR,
backup/restore/cutover, durable memory and editions; artifacts; provider-free test/evidence
obligations with planted mutants; the neutral-vs-implementation split; and reconstruction acceptance
evidence with falsifiers. Each recommendation names a competing option, a decisive falsifier and what
it does not prove.

## Non-goals

- No selection of a language, store, framework, transport, telemetry vendor, backup system or
  distribution channel.
- No execution, destructive test, provider call, install, spend or live-host action; no repository
  edits.
- No reopening of D-019/D-027/D-028/D-036 or accepted decisions.
- No legal, licensing or distribution conclusion; G-32 is counsel-gated.
- No measured performance, durability, RPO/RTO or eligibility claim.
- No substitution of a synthetic observer for native qualification.
- No modification of a protected path or accepted Owner decision.

## Completion gate

- Neutral contracts are specified for telemetry, update/release/DR, backup/restore/cutover, durable
  memory and editions, each with state machine/invariants and refusal behavior.
- Telemetry preserves counter honesty and `unknown`-is-not-zero and never becomes an authority.
- Update/release and DR specify version identity, atomic landing, rollback, the recovery cut and
  checkpoint-plus-suffix reconstruction; RPO/RTO stated as unmeasured.
- Backup/restore specifies coherent cut + CAS closure + replay + authority reacquisition and
  reverse-compatibility reading.
- Memory distinguishes semantic cache, capsule and working set; forbids authority refresh and
  cross-audience leakage.
- Editions are opt-in, install/verify-receipted and distribution-neutral.
- Provider-free test/evidence obligations with planted mutants, and reconstruction acceptance evidence
  with a falsifier, are stated.
- Every recommendation names a competing option, a decisive falsifier and what it does not prove; no
  mechanism/vendor selected; no legal conclusion; privacy and secret scan clean.
