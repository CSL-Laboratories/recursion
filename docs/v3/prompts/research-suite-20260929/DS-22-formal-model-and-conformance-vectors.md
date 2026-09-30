---
title: DS-22 — Formal model and conformance vectors
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-22 — Formal model and conformance vectors

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide the exact finite transition system, invariant set, guard-removal mutant corpus and language-neutral conformance vectors that must exist before the privileged kernel API is frozen, and specify the evidence discipline that keeps a passing model from being misreported as implementation correctness. This is blocking because `OR-018` ratifies a small deterministic kernel while `E-FHAR2-101`/`102` and `WP-FHAR2-101`/`WP-FHAR2-102` remain unrun pre-code prerequisites to any language, storage or broker comparison; a model that hides the same race as the code would certify nothing, and an unstated vector format would let independent implementations diverge silently.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane must not select a language, storage engine, broker transport or renderer; `OR-018` selects the kernel boundary, not an implementation. Do not reopen `OR-018`/`OR-019`/`OR-020` or `OR-036`, and do not treat the amendment proposal as operative. A finite model is a design artifact: it neither proves the unbounded system nor refines to code, and no result here authorizes construction.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/STATE.md`
- `docs/v3/ARCHITECTURE.md`
- `docs/v3/AUTONOMOUS-PLANNING.md`
- `docs/v3/SECURITY.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`
- `docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md`
- `docs/v3/CLAIM-LEDGER.md`
- `docs/v3/construction/formal-comparison-20260907/results.json`
- `docs/v3/construction/formal-comparison-20260907/handoff.md`
- `docs/v3/foundation-research/platform-round/architecture-roadmap.md`

## Research questions

### Formal-method choice versus the goal

1. For the concrete obligations to falsify "no undeclared post-pause effect" and "no overlapping lease" over a bounded domain, which of TLA+/PlusCal with TLC, Alloy, SPIN/Promela, or Coq/Lean refinement can state each required invariant without an oracle the kernel is forbidden to hold, and what is the exact falsifier that eliminates a candidate — a required invariant unexpressible, or a deliberately fence-free mutant that still passes?
2. How does each tool represent unbounded logical time, digest-valued payloads, opaque byte commands and operation IDs, and where does the finite abstraction retag a genuinely infinite behavior as safe (state-space explosion that hides an omitted dimension)?
3. Which tool can mechanically tie its abstract transitions to the closed kernel signatures `decode(bytes, schema_epoch)`, `admit(immutable_state, expected_state_digest, command, fixtures)`, `reduce(state, ordered_event)` and `derive_work_packet(state, admitted_task_id)`, so that trace validity is checkable without adding filesystem, network, process, provider, credential or wall-clock access to the kernel?

### The authority/admission/fence/lease/PAUSE/effect transition system

4. What is the exact variable set and `Next` disjunction for one aggregate — goal epoch, accepted GoalSpec digest, plan revision/DAG, task states, packet digests, command IDs, append-only events, executive/worker/verifier leases and epochs, effect intents/receipts/ambiguity, PAUSE epoch/mode, retry novelty/attempts, verifier decisions, publication state and logical time — and which declared actions in `E-FHAR2-101` can be dropped without losing a required counterexample?
5. Can the eleven-plus required invariants (`TypeOK`, `ChainIntegrity`, `AtomicAppend`, `CommandIdempotence`, `AuthorityScope`, `LeaseFence`, `AmendmentFence`, `PauseSafety`, `RetrySafety`, `CancelNoResurrection`, `EffectSafety`, `VerifierSafety`, `ReplayDeterminism`, `ReplayFailClosed`, `ProjectionParity`, `FlowBound`, `NoBlindEffectReplay`) each be written as a pure state predicate, and which one forces an environment assumption or a monitor variable that must be exposed rather than hidden?
6. How is `AuthorityScope` ("a descendant never widens owner, paths, capabilities, privacy, routes or budgets") encoded over an abstract capability lattice so the mutant that removes one lattice edge produces a retained minimal counterexample?
7. What exact state predicate expresses "one writer per aggregate" and "atomic append with previous digest and transaction group", and how does the model distinguish a stale writer epoch from a merely duplicated command?
8. How is `PauseSafety` made falsifiable per PAUSE mode (`checkpoint`, `interrupt`, `drain`, `emergency`) given the operative constitution is immediate halt, and where can a checkpoint policy still leak an undeclared effect that the finite model cannot see?
9. How are `EffectSafety` and `NoBlindEffectReplay` modeled when the external effect truth is an unobservable environment: which pending/ambiguous/reconciled states are sufficient, and what negative trace proves blind repetition occurs?
10. What is the smallest bound for the first model (one aggregate, one task/effect/timer/verifier, two workers, two goal epochs, two plan revisions, three lease epochs, two attempts, event bound 20, small bounded queues) at which at least one required race remains reachable, and which expansion dimension is provably necessary rather than decorative?
11. How is `ProjectionParity` and `ReplayDeterminism` checked against a second independent decoder/registry that implements the same abstract semantics but shares no transition code with the first?

### Guard-removal mutants and the hidden-choice/fairness scope

12. For each fence, epoch, novelty, PAUSE, idempotence and effect guard, what is the exact guard-removal mutant, and what must a retained counterexample contain — model revision, tool version, configuration, seed, invariant name, full trace, final state, expected violation and digest?
13. Given `FMC-002`/`FMC-003` reported 444,834 reachable states and 2,644,215 transitions per concrete model with 22 rejected guard mutants, which additional mutants (not just guard deletion but guard weakening, reordering, and single-field hiding) are required, and how is mutant coverage measured rather than asserted?
14. How should hidden-choice and weak-fairness calibrations distinguish a mapping limitation from a visible failure, and under exactly which named weak/strong fairness assumptions is liveness checked, given provider response, owner action and network recovery are never assumed unconditionally?
15. What per-cursor pending-Stop or hidden-choice cyclic components (as seen in the `FRI-003` 32,500-component families) must be represented, and what negative trace proves eventual logical Stop fails without the named fairness of the apply action?

### Kernel-API freeze and language-neutral conformance vectors

16. What is the canonical, versioned, language-neutral encoding of conformance vectors (command bytes, `schema_epoch`, immutable-state fixture, `expected_state_digest`, authority/policy fixtures, expected event batch, expected typed refusal, expected derived packet) such that a CPython, Rust and Go implementation must produce bit-identical vectors, digests and refusals?
17. Which property-based strategies (canonicalization invariants, round-trip encode/decode, monotone sequence, no-widening generation, idempotence under replay, state-machine differential) complement the literal golden vectors, and how are shrunk counterexamples bound to a vector revision?
18. How is differential testing across independent implementations governed so a mismatch is classified as spec ambiguity, encoding divergence, or a real safety failure — and what is the refusal taxonomy (malformed, stale epoch, unknown schema, widening, duplicate command) that every implementation must reproduce exactly?
19. What evidence separates "the model's abstract `admit`" from "the code's `admit`", and what artifact (refinement map, trace-validity checker, or differential vector suite) must exist before a passing model can support any implementation or language claim?

### Model-to-implementation evidence gap

20. What is the smallest set of deliberately falsifiable claims in `WP-FHAR2-101`/`WP-FHAR2-102` that, if unrun or ambiguous, must block model freeze — and how should the report label every unproved seam (adapter seam, broker refinement, unbounded generalization, OS containment, external-effect truth)?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a formal-method comparison matrix with unknown cells explicitly `unknown`; the exact finite transition system (variables, `Next`, bounds, environment assumptions, named fairness); the invariant list with a predicate sketch per invariant; a guard-removal and guard-weakening mutant table with the required counterexample receipt schema; a conformance-vector specification at byte level with a refusal taxonomy; a differential-testing and property-based plan; a recommendation tied to a named competing method (for example TLA+/PlusCal/TLC versus Alloy versus SPIN versus Coq/Lean refinement) with a decisive falsifier; and an explicit "a model is not the implementation" evidence-gap section plus open Owner questions.

## Non-goals

- Do not select a construction language, storage engine, broker transport or renderer.
- Do not implement, run a runtime, or treat a model result as code correctness.
- Do not reopen `OR-018`/`OR-019`/`OR-020`/`OR-036` or apply the amendment proposal.
- Do not assume provider response, owner action or network recovery as unconditional fairness.
- Do not claim unbounded generalization, refinement to code, or OS containment from a finite model.
- Do not include private project content, transcripts, local paths or credentials in any public packet.
- Do not freeze the kernel API beyond the four closed functions without naming the forcing invariant.
- Do not reduce the mutant corpus to guard deletion alone.
- Do not present a proposed vector format as an Owner decision.

## Completion gate

- A formal-method recommendation names at least one competing method and a decisive falsifier.
- Every invariant has a written state predicate and either a passing check or an explicit environment assumption.
- The mutant corpus covers guard deletion, guard weakening and guard reordering, with a counterexample receipt schema.
- At least one retained minimal counterexample per removed guard is required, not asserted.
- Conformance vectors are byte-level, versioned, language-neutral and include an exact refusal taxonomy.
- The differential-testing plan covers at least two independent implementations with bit-identical vectors.
- The report states explicitly that a passing finite model does not prove the unbounded model or code refinement.
- Every unproved seam (adapter, broker, OS containment, external effect, unbounded generalization) is labeled.
- No protected path is edited and no runtime/provider/auth/cron/PAUSE/spending effect occurs.
- Open Owner questions are listed separately and no research recommendation is presented as an Owner decision.
