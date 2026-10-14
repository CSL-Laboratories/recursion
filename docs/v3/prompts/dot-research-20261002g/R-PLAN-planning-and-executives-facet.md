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
architecture outline. Packet F (R-AUDIT) confirmed this layer is **unowned** and named only. It
extends accepted product direction (autonomous planning, Head → executives → bounded workers, no
separate AI "ARM manager") without reopening it.

## Baked context

### Shared Recursion context

Recursion is a provider-neutral agent platform. The development target is Recursion v3: a small
deterministic kernel with typed adapters; native and upstream execution; persistent, goal-scoped
assistants organized as a Head, goal-scoped executives, and bounded workers; configurable context and
compaction; plugins and editions; and Windows/Linux operation. Authority: Lucas is the final owner;
Hermes-led control was retired (D-017). An implementer's own PASS is never acceptance. Evidence
classes: source-documented / locally observed / inferred / unknown; `unknown` is not zero. No
language, store, framework, sandbox or vendor is selected.

### Lane context

- The gap: the deterministic-kernel facet must **not** own intent/tactics, and no facet owns the
  planning/executive behavior layer — goal decomposition, executive loops, consultation/adoption,
  persistent goal-scoped assistants, actor/model allocation, and D-025 (autonomous research as an
  optional bounded plugin).
- Relevant existing sources (summarized): a planning module with `GoalSpec`/`PlanRevision`/`TaskNode`
  and planning events; an executives module defining Head/executive/worker personas and lifecycle; an
  autonomous-planning contract whose gate states the kernel admits typed transitions while models
  only propose. Packet F's **M1A planning corpus** (`F-M1A-001…023`, 23 of 71 bundles) is the
  executable planning contract baseline and includes two recorded code/wire discrepancies (WorkPacket
  correlation identity must equal the `GoalSpec` ID; task effect fields must not exceed the goal's).
- Boundaries that must hold: models propose, the kernel admits; a model never mints authority; one
  fenced writer owns canonical state; no standing autonomous executive and no separate AI ARM manager;
  continuing autonomous research is optional, off by default and bounded (D-025).

## Purpose

Produce the decision-ready **planning-and-executives facet**: responsibilities, non-ownership,
boundaries and interfaces (including the missing planning→kernel row), neutral contracts, state
machines and invariants, artifacts, the consultation/adoption and allocation models, the D-025
boundary, provider-free test/evidence obligations with planted mutants, the neutral-vs-implementation
split, and reconstruction acceptance evidence with falsifiers.

## Authority and boundaries

The constraints in `## Baked context` are operative. Propose amendments; never edit the constitution.
Public Deep Research receives only public or synthetic briefs. **DO NOT select a language, store,
framework, runtime, sandbox, transport or vendor.** Do not decide an Owner question recorded in
D-017, D-019, D-022, D-024, D-025 or D-034, and do not reopen accepted planning/executive direction.
No provider, model, account or live-host call. Do not assert a planning/executive design improves
quality, latency or authority without a defined independent experiment.

## Inputs to read

None from this repository by necessity: the planning/executive direction and the M1A baseline are
summarized above. Retrieve and cite public primary sources (hierarchical planning, plan repair,
retry/backoff and quiescence, multi-agent delegation, human-in-the-loop adoption) with a URL and
retrieval date. Treat every framework as prior art, not a selection.

## Research questions

1. What is the exact ownership boundary between the planning/executive layer and the kernel — which
   decisions are proposals vs kernel-admitted transitions — and how does the planning→kernel
   interface look (the missing row)?
2. What is the neutral executive-loop state machine (states; wait/escalate/pause/terminate; interaction
   with Aggressive/Full/Lazy Stop and checkpoint/interrupt/drain)?
3. What are the neutral `GoalSpec`/`PlanRevision`/`WorkPacket` contracts, including identity,
   versioning, repair/rebase, and how a superseded plan is fenced (resolve the two M1A discrepancies
   conceptually without redefining frozen bytes)?
4. How are consultation, questions, approvals and adoption modeled so a reply is evidence with
   provenance and never silently becomes an accepted instruction?
5. How are actor/model allocation and bounded fan-out represented as proposals, and how are "no second
   portfolio executive" and "no separate AI ARM manager" enforced as invariants?
6. How is D-025 represented (optional, off by default, no idle inference, no standing executive)?
7. What artifacts/facts must be language-neutral, and what may stay implementation-specific?
8. What provider-free vectors and planted mutants evidence a faithful reimplementation, and what
   falsifies reconstructability?

## Deliverable

A planning-and-executives facet specification with: responsibilities/non-ownership; boundaries and
interfaces; neutral contracts and state machines; consultation/adoption and allocation models; the
D-025 boundary; a neutral-vs-implementation split; provider-free test/evidence obligations with
planted mutants; and reconstruction acceptance evidence with falsifiers. Each recommendation names a
competing option, a decisive falsifier and what it does not prove. No language/vendor selected.

## Non-goals

- No selection of a language, store, framework, runtime, sandbox, transport or vendor.
- No execution, provider/model call, install, spend or live-host action; no repository edits.
- No reopening of the accepted planning/executive direction or D-017/D-019/D-022/D-024/D-025/D-034.
- No measured performance/quality/authority claim; no qualification.
- No new executive tier beyond Head/executive/worker; no standing autonomous executive; no separate AI
  ARM manager.
- No modification of a protected path or accepted Owner decision.

## Completion gate

- Ownership and non-ownership are stated (kernel admission, canonical writes, authority minting).
- A neutral executive-loop state machine is specified with wait/escalate/pause/terminate and its Stop
  interaction.
- `GoalSpec`/`PlanRevision`/`WorkPacket` identity/versioning/repair/rebase contracts and supersession
  fencing are specified, and the two M1A discrepancies are addressed conceptually.
- Consultation/adoption, actor/model allocation, the "no second executive" invariant and the D-025
  boundary are specified.
- The planning→kernel (and execution-fabric) interfaces are enumerated.
- Provider-free test/evidence obligations with planted mutants, and reconstruction acceptance evidence
  with a falsifier, are stated.
- Every recommendation names a competing option, a decisive falsifier and what it does not prove; no
  language/vendor selected.
- What the candidate does not establish is stated; privacy and secret scan are clean.
