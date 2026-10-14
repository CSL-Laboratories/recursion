---
title: Dot research packet — 2026-10-02f (D-039 outline revisions)
status: ready-for-explicit-invocation
last-updated: 2026-10-02
parent-goal: goal-recursion-v3-harness-orchestrator
invocation: each handoff is self-contained; supplying the document launches its session
---

# Dot research packet — 2026-10-02f (D-039 outline revisions)

This directory holds **three** self-contained research handoffs that respond to the independent
static review (GLM-5.3-Flash) of the D-039 language-agnostic architecture outline (PR #161). The
review verdict was **NEEDS REVISIONS**: the skeleton is sound, but there are unsupported
identifiers, a false gate-pass claim, a mislabeled gap, an unsupported count, and three
under-covered facets. These handoffs research the corrections and the missing content from primary
sources so the outline can be revised on evidence, not assertion.

- **R-PLAN** — owning facet for **autonomous planning and executives** (review C1): goal
  decomposition, executive loops, consultation/adoption, persistent goal-scoped assistants, actor
  and model allocation, and the research-as-plugin boundary (D-025).
- **R-OPS** — owning facet for **operations and lifecycle** (review C2–C5): telemetry
  evidence-provider, update/release/DR and backup-restore/cutover, durable memory, and
  editions/distribution.
- **R-AUDIT** — a **correction and baseline lane** (review A1–A10 and §4/§6): verify each disputed
  identifier/claim against primary sources; inventory the executable M1 contract corpus as the
  behavior baseline; and produce the R-LANG-informed revision checklist.

Supplying one document is its invocation; run one handoff per sitting. These handoffs keep evidence
classes distinct (source-documented / locally observed / inferred / unknown), carry a competing
option and decisive falsifier for every recommendation, and select no language, store, framework,
transport, provider or vendor. `unknown` is never zero.

## Index

| Prompt | Domain | Review items addressed | Title |
|---|---|---|---|
| [R-PLAN](R-PLAN-planning-and-executives-facet.md) | Autonomous planning and executives | C1, §7 | Planning and executives facet |
| [R-OPS](R-OPS-operations-lifecycle-facet.md) | Telemetry, release/DR, memory, editions | C2, C3, C4, C5 | Operations and lifecycle facet |
| [R-AUDIT](R-AUDIT-outline-correction-and-baseline.md) | Source verification, M1 corpus, R-LANG handoff | A1–A10, §4, §6 | Outline correction and baseline |

## Running a session

Supply the `R-*.md` document to the session. It opens with a research directive that names the task,
authorizes the investigation and forbids asking which topic to research. Nothing else has to be
typed; all context is baked in under `## Baked context`.

## Authority

Each handoff is self-contained: a Deep Research session needs no repository access because the shared
authority, scope, evidence and privacy context is baked into every file. The operator should read
[CONTROL_PLANE.md](../../../../CONTROL_PLANE.md), [ANCHOR.md](../../../../ANCHOR.md) and
[AGENTS.md](../../../../AGENTS.md) before invoking a session.

Recursion is a standalone agent harness and a harness orchestrator that delegates through other
harnesses. Hermes-led control was retired on 2026-09-28 (state/DECISIONS.md D-017). These prompts
propose no amendment and never edit a protected path. D-027 authorizes a research packet only: no
spend, no legal commitment and no counsel contact. Public sessions receive only public or synthetic
briefs. No language, store, framework, transport, provider or vendor is selected by any handoff.

## Gate

Run from the repository root:

```text
python -X utf8 -B docs/v3/prompts/dot-research-20261002f/check_research_stack.py
```

The check is deterministic and provider-free. It fails unless all three `R-*.md` files exist, have
the required front matter and the eight required headings, contain the research directive, are
referenced by this README, and have only resolving relative links, with no obvious secret material
and no other-project identifier (reusing `tools.check_public_scope.PATTERN`). A passing check
certifies structure only — not scientific content, source quality or Owner acceptance.
