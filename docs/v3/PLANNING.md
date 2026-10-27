---
title: Recursion v3 planning and Grill Me projection
status: contract-projection-with-open-policy
last-updated: 2026-09-07
---

# Recursion v3 planning and Grill Me projection

## Mark 3 accepted answers and task-aligned initiative — 2026-09-07

[Mark 3 dispositions](foundation-research/mark2-research/mark3/directions.json) resolve both Mark 2 Owner choices.
OR-036 selects the small aggregate with independently checkable broker refinement and required
comparison against a broker-inclusive model. OR-037 retains Owner-only actual amendments with
substantial routine delegation. Historical pending snapshots are not the current Owner queue.

OR-038's accepted foundation supports extended independent research when requested and close
collaboration when desired, while preserving intent and consequential feedback. A proposed
conceptual separation is intended result, working relationship and authorized actions. Infer
these from explicit instructions and existing preferences; this is not three mandatory forms.
A progress question need not cancel the assignment, and discussion does not widen authority.

The questionnaire-assistant/Head exchange must retain exact Owner answers separately from
proposals and summaries. Head integrates accepted directions, rechecks consistency and derives
bounded follow-up packets. Model adequacy, resource-dependent probes and implementation remain
evidence work; approval of these two choices does not select a language, store or framework.

## Foundation research to accepted construction

The [foundation GRILLME packet](foundation-research/grillme.md) is a historical snapshot of ten then-unanswered decisions; current dispositions follow the Owner register and Mark 3 record.
The first concerns the bounded model integration contract; it does not reopen OR-018/OR-019.
Record exact later Owner answers through private intent references, apply publication-safe
dispositions to [decisions.json](foundation-research/decisions.json), reconcile cross-decision
dependencies, then compile an accepted versioned GoalSpec and construction DAG. Silence supplies
no answer. M1 contracts remain immutable inputs. FND-001/FND-002.

The illustrative Propose → Plan → Research → Synthesize → Develop lifecycle also requires
verification, integration, acceptance, checkpoint/recovery and stop transitions. Already bounded
tasks may skip unnecessary discovery ceremony; they cannot skip admission, required gates or
authority limits. Role/model/lease transitions remain distinct. FND-014.

> **Proposed contract projection.** References below to the Recursion Head and ARM-owned DAGs
> describe a future product boundary. The current Hermes-specific constitution remains operative
> until Lucas approves a recorded amendment; this document cannot perform that amendment.

## Purpose

Planning is a first-class, configurable phase of autonomous development. Its job is not to create an impressive document; it is to reduce semantic uncertainty, establish measurable success, expose trade-offs before implementation, and give ARM executives a stable contract they can execute without hallucinating product direction.

Planning may be disabled for already-bounded work. When enabled, it can range from seconds to hours.

[AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md) is the canonical owner of planning objects,
authority, lifecycle, DAG admission, WorkPacket derivation, gates, amendment, retry, replan,
escalation and replay. This document owns the human-facing planning and Grill Me product
projection only.

`off` removes the interactive planning phase; it does not remove provenance or admission. Execution still requires an authenticated, complete GoalSpec with parent goal, authority, exact source, allowed paths, non-goals, budget, privacy/egress policy, and machine-checkable gates. If any field is absent or ambiguous, admission stops.

## Grill Me protocol

Grill Me is Recursion's interactive goal-compilation protocol. Each cycle:

1. Identifies one consequential ambiguity.
2. Explains why it affects architecture, quality, cost, privacy, or operation.
3. Presents materially different options and their trade-offs.
4. Makes one reasoned recommendation.
5. Separates the recommended default from what remains configurable.
6. Names failure modes, evidence needs, and a falsifier where possible.
7. Records the owner's exact answer in the Owner Intent Ledger.
8. Updates the decision register and affected specification immediately.
9. Generates the next highest-value unresolved question.
10. Periodically performs a cross-decision consistency audit.

The protocol must not ask a hundred questions and leave the answers trapped in chat. Durability after each answer is part of the feature.

## Modes

| Mode | Behavior |
|---|---|
| `off` | Accept an already complete GoalSpec or bounded task without interactive planning |
| `quick` | Ask only questions that block safe execution |
| `standard` | Resolve important product, acceptance, route, privacy, and budget choices |
| `deep` | Add reference research, alternatives, experiments, and consistency checks |
| `exhaustive` | Hours-long collaborative design with scientific hypotheses and repeated hardening |

Each mode has configurable time, subscription-capacity, token, and metered-cost sub-budgets. Planning remains inside the parent goal's aggregate authority. Default planning consumes included/subscription capacity only. Exhaustion returns a resumable draft; it does not silently lower quality or begin execution.

Head and ARM planning compute may use any route inside the exact owner-approved
model/provider/account/capacity-pool envelope. The logical actor, GoalSpec and evidence remain
stable across routes. This owner direction requires reconciliation of the current
provider-specific constitutional clause before deployment behavior changes.

## Owner Intent Ledger

Every owner directive is stored losslessly with:

- exact text;
- source event and timestamp;
- intended scope;
- status: active, fulfilled, rejected, clarified, or superseded;
- link to any clarifying or superseding directive;
- affected GoalSpec and decision identifiers.

Summaries and compacted plans may reference this ledger but cannot replace it. When instructions conflict, the system surfaces the conflict and follows the established authority precedence; it never silently harmonizes them.

## GoalSpec projection

Owner intent is retained exactly. Head may compile a complete draft, but the authenticated Owner accepts or
amends the exact GoalSpec digest. Amendment proposed 2026-10-25 (acceptance is a human decision
by Lucas, CSL-524): **product-spec** (`docs/v3`) amendments by Samir or Dan are accepted by
another founder's GitHub `APPROVED` review on the exact head SHA with file digests
(`CONTROL_PLANE.md` product-spec acceptance). That rule is not GoalSpec digest acceptance.
GoalSpec remains Owner-authenticated; CSL's first-user Owner remains Lucas until a GoalSpec
names another founder. Accepted versions are immutable. Operative constitutional and
Owner restrictions fence new admission immediately; descendant plans may narrow without changing
the goal. Widening, relaxed privacy, new egress/account/spend/effect authority or weaker required
verification needs a new Owner-accepted version and safe checkpoint. The required fields,
amendment disposition and stale-work rules are canonical in
[AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md#goal-compilation-and-amendment).

## Planning ownership

The proposed post-amendment default boundary is:

- Lucas defines intent, values, acceptance, risk, spending, and final decisions.
- The Recursion Head compiles the strategic GoalSpec, assigns the goal, and monitors outcomes.
- The ARM executive creates and maintains the implementation DAG and tactical plan.
- Workers execute one bounded node.
- Independent verifiers judge evidence against the GoalSpec.

Models propose semantics. The deterministic kernel admits transitions and derives immutable
WorkPackets. Head may request an ARM replan or change ARM lifecycle but does not write the ARM's
DAG. Workers and verifiers have no parent-plan mutation authority.

Head-authored detailed implementation plans remain a configurable mode, not the only architecture.

## Plan verification

Plan verification is risk-scaled and resource-aware:

- low-risk bounded tasks: deterministic schema and repository checks;
- ordinary product work: an independent critique or targeted evidence review;
- security, authentication, control-plane, deployment, and migration work: multiple perspectives plus explicit human review where authority requires it.

Paid CI is never assumed. Local gates, remote-node gates, hosted CI, model review, and human validation are independent capabilities. A plan declares which evidence is required and which capabilities are unavailable. It must not report an unavailable gate as passed.

## Failure behavior

A task gate failure does not automatically PAUSE the entire system. It returns to its ARM with
exact evidence. The ARM may retry only with a valid closed-schema `NoveltyDelta` and a nonempty
canonical diff addressing the failed condition; the kernel does not judge semantic wisdom.
Repeated failure, semantic ambiguity, exhausted authorized recovery, or conflicting evidence
escalates to Head. If Lucas is the required actor, development under that affected
authority stops until direction arrives.

Global PAUSE remains a separate operator/control-plane mechanism.

The complete state transitions and typed capacity/provider/verifier/effect blockers are canonical
in [AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md#gates-retry-replan-and-escalation).

## Retry novelty contract

This is the user-facing projection of the canonical retry contract; the typed decision and
fail-before-model-turn rules live in [AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md).

Every retry uses exactly one canonical kind and its required digest/set fields:

- `evidence-added`;
- `hypothesis-replaced`;
- `strategy-changed`;
- `route-capability-changed`;
- `task-narrowed`;
- `dependency-repaired`; or
- `owner-amended`.

The exact finite field and admission grammar is canonical under
[Gates, retry, replan and escalation](AUTONOMOUS-PLANNING.md#gates-retry-replan-and-escalation).
Rephrasing with identical typed digests is not novelty. The scheduler stops repetitive quota burn
before another model turn is admitted.

## Planning outputs

A completed planning phase produces:

1. an accepted or explicitly draft GoalSpec;
2. an Owner Intent Ledger slice;
3. a decision register update;
4. a research/evidence appendix;
5. an initial ARM profile and execution policy;
6. a milestone DAG with machine-checkable gates;
7. unresolved questions and required human seams;
8. a construction prompt or task set that does not depend on the original chat.

## Scientific research mode

`deep` and `exhaustive` planning may create a bounded research ARM or research task graph. It
starts with a claim/source gap matrix, searches approved scholarly and technical sources,
records contradictions and applicability, and proposes a preregistered experiment where
external evidence cannot decide local fit. Search snippets and model consensus do not satisfy a
source gate. The product contract is in [SCIENTIFIC-RESEARCH.md](SCIENTIFIC-RESEARCH.md).

## Learning without rewriting intent

Recursion may learn which question ordering, task size, plan form, and review pattern correlate with better outcomes. It may recommend defaults and reorder unasked questions. It may not silently alter accepted requirements, gates, privacy, permissions, budgets, or authority.
