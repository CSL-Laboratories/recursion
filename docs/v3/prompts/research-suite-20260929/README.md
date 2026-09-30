---
title: Deep Research prompt suite — proposed-inert, 2026-09-29
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: each prompt is inert until Lucas explicitly invokes that file in a new session
---

# Deep Research prompt suite

This directory collects bounded Deep Research handoffs that together cover the research bases
of Recursion. Every `DS-*` file is **inert**: creating, reading, or listing it launches
nothing and authorizes no provider call, write, effect, spend, or configuration change. Run
one prompt per sitting, not all at once.

This suite is a **candidate proposal**. It does not create a Hermes task, bind a goal, change
operational policy, or amend the constitution. Before any prompt is used, Hermes/Lucas must
bind it to a task with a parent goal, allowed paths, budget, route and an independent review.

## Authority

Read [CONTROL_PLANE](../../../../CONTROL_PLANE.md), `ANCHOR.md`, `AGENTS.md`, `docs/v3/START-HERE.md`,
[RESEARCH-AGENDA](../../RESEARCH-AGENDA.md) and the accepted OR-021 through OR-039 rows in
`DECISION-REGISTER.md` before using any prompt. The current Hermes constitution remains
operative; these prompts propose reconciliation text and never edit it. Public Deep Research
lanes receive only public or synthetic briefs — never unpublished v3 drafts, private project
content, transcripts, local paths, credentials, or account/infrastructure identifiers.

Existing research artifacts that remain authoritative and are not duplicated here:

- `docs/v3/prompts/architecture-proof-deep-research.md` — active architecture-proof campaign (agenda R1–R17).
- `docs/v3/prompts/standalone-harness-foundation-deep-research.md` — audiences, harness comparison, context/cache, delegation.
- `docs/v3/prompts/m1a-autonomous-planning-contract.md` — M1A construction, gated and inert.
- `docs/v3/prompts/overnight-architecture-research.md`, `docs/v3/prompts/overnight-adversarial-swarm-research.md` — superseded provenance only.
- `docs/v3/construction/next-work-20260914/` — 01 Sol service access, 02 Astra product frontier, 03 Sol recovery ledger.
- `research/PROMPTS.md` — earlier learning-loop suite (R1–R9).

## Coverage map

| Base | Lane | Prompt(s) |
|---|---|---|
| Headless native harness boundary | R1 | existing architecture-proof + standalone-harness prompts |
| ARM conversations and multi-agent rooms | R2 | [DS-15](DS-15-arm-conversations-and-multi-agent-rooms.md) |
| PAUSE, cancellation, effect fencing | R3 | [DS-01](DS-01-pause-cancellation-and-effect-fencing.md) |
| Calibrated Full Access and authority | R4 | [DS-02](DS-02-full-access-and-calibrated-authority.md) |
| Risk-scaled verifier independence | R5 | [DS-03](DS-03-risk-scaled-verifier-independence.md) |
| Plugin trust, signing, supply chain | R6 | [DS-04](DS-04-plugin-trust-and-supply-chain.md) |
| Single-writer migration, effect rollback | R7 | [DS-05](DS-05-single-writer-migration-and-effect-rollback.md) |
| Capacity admission and review debt | R8 | [DS-06](DS-06-capacity-admission-and-review-debt.md) |
| Privacy-preserving retention and deletion | R9 | [DS-07](DS-07-privacy-preserving-retention-and-deletion.md) |
| Private canary methodology | R10 | [DS-08](DS-08-private-canary-methodology.md) |
| Interactive terminal and communication views | R11 | [DS-09](DS-09-interactive-terminal-and-communication-views.md) |
| Whole-system performance and complexity budget | R12, R17 | [DS-10](DS-10-performance-and-complexity-budget.md) |
| Model routing and delegation science | R13 | [DS-11](DS-11-model-routing-and-delegation-science.md) |
| Context, compaction and cache economics | R14 | [DS-12](DS-12-context-compaction-and-cache-economics.md) |
| Configuration architecture and operator control | R15 | [DS-13](DS-13-configuration-architecture-and-operator-control.md) |
| Scientific Research ARM and scholarly evidence | R16 | [DS-14](DS-14-scientific-research-arm-and-scholarly-evidence.md) |

## Deep tranche 2 — implementation, platform and operations

These cover the bases the first tranche left partial. The technology decisions (language, store, renderer, plugin runtime, native loop) remain **unselected** by Owner direction; each prompt produces evidence and a recommendation with a falsifier, never a selection.

| Base | Anchor | Prompt |
|---|---|---|
| Implementation language and runtime | EXP-FHAR-10, E-FHAR2-102 | [DS-18](DS-18-implementation-language-and-runtime-selection.md) |
| Operating-system platform behavior (Windows + POSIX) | R11, TERMINAL-RUNTIME | [DS-19](DS-19-operating-system-platform-behavior.md) |
| Linux service topology and enterprise scaling | Milestones 7–8, SECURITY | [DS-20](DS-20-linux-service-topology-and-enterprise-scaling.md) |
| Canonical state store and persistence faults | PERF-001, H-012, E-FHAR2-103 | [DS-21](DS-21-canonical-state-store-and-persistence-faults.md) |
| Formal model and language-neutral vectors | E-FHAR2-101/102, WP-101/102 | [DS-22](DS-22-formal-model-and-conformance-vectors.md) |
| IPC, process broker, terminal wire protocol | E-FHAR2-104, WP-104 | [DS-23](DS-23-ipc-process-broker-and-terminal-protocol.md) |
| Credentials, enrollment, egress, secret lifecycle | FND-005/006, SECURITY | [DS-24](DS-24-credentials-enrollment-egress-and-secret-lifecycle.md) |
| Update, release, rollback, disaster recovery | OR-033, MKR-015, CMP-017 | [DS-25](DS-25-update-release-rollback-and-disaster-recovery.md) |
| Telemetry, accounting, honest status | TELEMETRY, Milestone 8 | [DS-26](DS-26-telemetry-accounting-and-honest-status.md) |
| Post-M1 construction: state and projection | ROADMAP Milestone 2 | [DS-27](DS-27-post-m1-construction-state-and-projection.md) |
| Post-M1 construction: plugin and route fabric | ROADMAP Milestone 3 | [DS-28](DS-28-post-m1-construction-plugin-and-route-fabric.md) |
| Construction fleet separation and continuous operation | CONTINUOUS-OPERATION CW-01…18, P1–P8 | [DS-29](DS-29-construction-fleet-separation-and-continuous-operation.md) |
| Multi-project supervision state and memory | research/PROMPTS R4/R5 | [DS-30](DS-30-multi-project-supervision-state-and-memory.md) |
| HITL approvals, forwarding, consultation | OR-027/028, INTERACTION-SURFACES | [DS-32](DS-32-human-in-the-loop-approvals-forwarding-and-consultation.md) |
| Licensing, distribution, provenance, IP | E-FHAR2-108, WP-108 | [DS-33](DS-33-licensing-distribution-provenance-and-ip-clearance.md) |
| Decision journaling and calibration | research/PROMPTS R7 | [DS-34](DS-34-decision-journaling-and-calibration.md) |

Bases that are operational or human-gated, and therefore deliberately have **no** research
prompt: the v2-era integration escalations (`esc-0002`…`esc-0014`, resolved or superseded)
and the ANCHOR out-of-scope list. These are not research questions.

## Deliberately excluded

- Market, investment and alpha research — near-term tooling spend converts via shipped
  work, not returns.
- "Best AI tools" surveys and universal model rankings — these age out and answer nothing.
- Any research that would require provider calls, paid fallback, a PAUSE change, credentials,
  deployment, publication or merge.

## Non-goals

- No runtime, schema or product implementation.
- No provider/model/auth/config/cron/spending change.
- No task creation in the Hermes registry; binding a task is a separate Owner/Hermes action.
- No launch of any prompt by this file.

## Completion gate

Run from the repository root:

```text
python -X utf8 -B docs/v3/prompts/research-suite-20260929/check_suite.py
```

The check is deterministic and provider-free. It fails unless every `DS-*` file exists, carries
the required front matter and headings, uses an inert status, has a unique id, is referenced by
this README, and has only resolving relative links, with no obvious secret material. A passing
check certifies structure only — not scientific content, source quality, or Owner acceptance.
