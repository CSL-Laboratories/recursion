---
title: Dot research packet — 2026-10-02g (planning + operations facets)
status: ready-for-explicit-invocation
last-updated: 2026-10-02
parent-goal: goal-recursion-v3-harness-orchestrator
invocation: each handoff is self-contained; supplying the document launches its session
---

# Dot research packet — 2026-10-02g (planning + operations facets)

This directory holds **two** self-contained research handoffs that supply the two facets the
packet-F return left unowned. Packet F (R-AUDIT) explicitly ran only the correction lane and stated
**R-PLAN and R-OPS have not been run**; its architecture revision guide names planning/executive
semantics, telemetry/accounting evidence, update/release/DR and editions as **unowned**. These
handoffs produce the missing language-agnostic facet specifications so the D-039 outline can be
completed on evidence.

- **R-PLAN** — owning facet for **autonomous planning and executives**: goal decomposition,
  executive loops, consultation/adoption, persistent goal-scoped assistants, actor/model allocation,
  and the D-025 research-as-plugin boundary.
- **R-OPS** — owning facet for **operations and lifecycle**: telemetry evidence-provider,
  update/release/DR and backup-restore/cutover, durable memory, and editions/distribution.

Both handoffs keep evidence classes distinct, carry a competing option and decisive falsifier for
every recommendation, and select no language, store, framework, transport, provider or vendor.

## Context from packet F (binding corrections to respect)

- The M1 executable corpus is **71 fixture bundles** (M1A 23 / M1B 24 / M1C 24), enforced by
  closed-inventory fixture tests plus semantic runners; it is a source inventory, **not 71 tests**.
- M1B represents **eleven** grant dimensions (functional capability + workspace scope + nine
  permissions); `PluginManifest@1` is the plugin/edition contract (D-035 is repository topology).
- Two source-documented code/wire discrepancies (WorkPacket correlation identity; effect attenuation)
  are blockers before freezing the behavioral contract.
- **Mobile floors are open** (Android min/target API; iOS OS/device); D-028 covers desktop only.
- G-09 = configuration conformance; G-20 = performance comparison / R11 ablation / disabled-plugin
  benchmark.

## Index

| Prompt | Domain | Title |
|---|---|---|
| [R-PLAN](R-PLAN-planning-and-executives-facet.md) | Autonomous planning and executives | Planning and executives facet |
| [R-OPS](R-OPS-operations-lifecycle-facet.md) | Telemetry, release/DR, memory, editions | Operations and lifecycle facet |

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
python -X utf8 -B docs/v3/prompts/dot-research-20261002g/check_research_stack.py
```

The check is deterministic and provider-free. It fails unless both `R-*.md` files exist, have the
required front matter and the eight required headings, contain the research directive, are
referenced by this README, and have only resolving relative links, with no obvious secret material
and no other-project identifier (reusing `tools.check_public_scope.PATTERN`). A passing check
certifies structure only.
