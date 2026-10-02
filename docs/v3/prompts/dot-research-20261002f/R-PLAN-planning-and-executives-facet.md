---
title: R-PLAN — Planning and executives facet
status: ready-for-explicit-invocation
last-updated: 2026-10-02
parent-goal: goal-recursion-v3-harness-orchestrator
invocation: self-contained; supplying this document launches the research
---

**Research directive.** You are the lead researcher. This document is your complete brief and your explicit authorization to research; it is not inert. Conduct the investigation described below and produce the deliverable, then check every item of the completion gate. Do not ask which topic to research, do not request further approval, and do not return a scoping plan, research plan, methodology, timeline, outline, or clarification request — those are not acceptable outputs. Execute the research now and return the final report with findings and citations; if you catch yourself planning, convert the plan into the research immediately.

# R-PLAN — Planning and executives facet

This handoff supplies the missing language-agnostic **planning and executives** facet for the D-039
architecture outline (independent review item **C1**). It extends accepted product direction
(autonomous planning, Head -> executives -> bounded workers, no separate AI "ARM manager") without
reopening it, and produces the neutral contracts a reimplementation must reproduce.

## Baked context

### Shared Recursion context

Recursion is a provider-neutral agent platform. The development target is Recursion v3: a small
deterministic kernel with typed adapters; native and upstream execution; persistent, goal-scoped
assistants organized as a Head, goal-scoped executives, and bounded workers; configurable context and
compaction; plugins and editions; and Windows/Linux operation. Authority: Lucas is the final owner.
Recursion is a standalone agent harness and a harness orchestrator that delegates through other
harnesses; Hermes-led control was retired on 2026-09-28 (D-017). An implementer's own PASS is never
acceptance. Evidence discipline: classify every claim as source-documented, locally observed,
inferred or unknown; `unknown` is not zero. No language, store, framework, sandbox or vendor is
selected by this research.

### Lane context

- The gap: the outline's deterministic-kernel facet says it must **not** own intent/tactics, but no
  facet owns the **planning/executive behavior layer** — goal decomposition, executive loops,
  consultation and adoption of replies, persistent goal-scoped assistants, and actor/model
  allocation. The review also found no planning->kernel row in the interface map and no representation
  of D-025 (continuing autonomous research as an optional bounded plugin).
- The relevant in-repo sources (summarized for this self-contained brief): a planning module
  describing planning events and a plan-revision DAG with `GoalSpec`/`PlanRevision`/`TaskNode`;
  an executives module describing Head/executive/worker personas and their lifecycle; and an
  autonomous-planning contract whose gate states the kernel admits typed transitions while models
  only propose. Treat these as binding constraints, not as text to restate.
- Boundaries that must hold: models propose, the kernel admits; a model never mints authority; a
  single fenced writer owns canonical state; a "no standing autonomous executive" and "no separate AI
  ARM manager" rule; continuing autonomous research is optional, off by default and bounded (D-025).

## Purpose

Produce the decision-ready, **language-agnostic planning-and-executives facet**: the owning
responsibilities, boundaries, neutral contracts, state machines and invariants, artifacts,
interfaces to the kernel and execution fabric, test/evidence obligations, what may stay
language-specific, and reconstruction acceptance evidence. It must be written so a reimplementation
can reproduce the behavior from the spec plus the frozen test corpus.

## Authority and boundaries

The authority, scope and privacy constraints in `## Baked context` are the operative limits. Propose
amendments to the operative constitution; never edit it. Public Deep Research receives only public or
synthetic briefs. **DO NOT select a language, store, framework, runtime, sandbox, transport or
vendor.** Do not decide an Owner question recorded in D-017, D-019, D-022, D-024, D-025 or D-034, and
do not reopen the accepted planning/executive direction. No provider, model, account or live-host
call is made. Do not assert that a planning/executive design improves quality, latency or authority
without a defined independent experiment. No claim of measured performance or qualification.

## Inputs to read

None from this repository: the planning, executive and autonomous-planning direction is summarized in
`## Baked context`. The session must, however, retrieve and cite the public primary sources it relies
on (for example peer-reviewed or official documentation on plan repair, hierarchical task networks,
retry/backoff and quiescence, multi-agent delegation, and human-in-the-loop adoption) and pin each
claim to a URL and retrieval date. Treat every framework or pattern as prior art, not a selection.

## Research questions

1. What is the precise ownership boundary between the planning/executive layer and the deterministic
   kernel — which decisions (goal decomposition, plan revision, delegation, consultation, adoption)
   are proposals, and which are kernel-admitted transitions?
2. What is the neutral model of a goal-scoped executive loop: its states, the conditions under which
   it waits, escalates, pauses or terminates, and how it interacts with the kernel's single writer
   and pause vocabulary (Aggressive/Full/Lazy, checkpoint/interrupt/drain)?
3. What are the neutral contracts for a plan revision (DAG or successor form) and a bounded
   `WorkPacket`, including identity, versioning, repair/rebase, and how a superseded plan is fenced?
4. How are consultation, questions, approvals and adoption modeled so a reply is evidence with
   provenance and never silently becomes an accepted instruction?
5. How are actor/model allocation and bounded fan-out represented as proposals, and how is "no second
   portfolio executive" and "no separate AI ARM manager" enforced as an invariant?
6. How is D-025 (continuing autonomous research as an optional bounded plugin) represented, off by
   default, with no idle inference and no standing executive?
7. What artifacts and facts must be language-neutral (state machines, events, refusals, interfaces to
   the kernel and execution fabric) and what may stay implementation-specific?
8. What provider-free test vectors and invariants (with planted mutants) would evidence a faithful
   reimplementation, and what would falsify a reconstructability claim?

## Deliverable

A planning-and-executives facet specification containing: responsibilities and non-ownership;
boundaries and interfaces (including the planning->kernel row the outline lacks); neutral contracts,
state machines and invariants; the consultation/adoption and allocation models; the D-025 boundary;
a language-neutral vs language-specific split; provider-free test/evidence obligations with planted
mutants; and reconstruction acceptance evidence with falsifiers. Every recommendation names a
competing option, a decisive falsifier and what it does not prove. No language or vendor is selected.

## Non-goals

- No selection of a language, store, framework, runtime, sandbox, transport or vendor.
- No execution, provider/model call, install, spend or live-host action; no repository edits.
- No reopening of the accepted planning/executive direction or of D-017/D-019/D-022/D-024/D-025/D-034.
- No measured performance, quality or authority claim; no qualification.
- No new executive tier beyond the accepted Head/executive/worker model; no standing autonomous
  executive and no separate AI "ARM manager".
- No modification of a protected path or accepted Owner decision.

## Completion gate

- The facet states its ownership and, explicitly, what it must not own (kernel admission, canonical
  writes, authority minting).
- A neutral executive-loop state machine is specified with wait/escalate/pause/terminate conditions
  and its interaction with the pause vocabulary.
- Plan-revision and WorkPacket identity/versioning/repair/rebase contracts are specified with fencing
  of superseded plans.
- Consultation/adoption, actor/model allocation, the "no second executive" invariant and the D-025
  boundary are each specified.
- The interfaces to the kernel and execution fabric are enumerated (including the planning->kernel
  row the outline lacks).
- Provider-free test/evidence obligations with planted mutants, and reconstruction acceptance
  evidence with a falsifier, are stated.
- Every recommendation names a competing option, a decisive falsifier and what it does not prove; no
  language or vendor is selected.
- The candidate states what it does not establish: no measured performance, no qualification, no
  legal conclusion. Privacy and secret scan are clean.
