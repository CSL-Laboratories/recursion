---
title: Recursion research and decision index (anti-drift map)
status: frozen-historical
last-updated: 2026-10-25
---

# Recursion research and decision index

**Frozen historical intake.** Do not add new waves here. All research output (notes,
briefs, findings, source tables, competitor or frontier writeups, research registers
or indexes) goes to
[CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research) as
`submissions/<record_id>/`, through a PR in that repo, with a digest on the Linear
issue. Never commit research content to this repo; specs, amendments and code only
link to the vault record (repo, 40-hex commit, path, claim, limitations). If
csl-research is not accessible, stop and post a blocker on the Linear issue; do not
fall back to this repo. `docs/v3/research-lane/`, `docs/v3/foundation-research/` and
`docs/v3/prompts/` are legacy and frozen; `experiments/v3/` holds only runnable
product checks.

A single map of every research wave, intake, packet, experiment, decision and gate that
landed in this tree through 2026-10-02, so no historical knowledge is lost between
sessions and stale artifacts are visible rather than silently reused.
When superseding a historical row, mark it `superseded` and keep the
path (a ruled-out approach without its reason is a dead end waiting to be rediscovered).

## Authority and identity

| Item | Path | Status |
|---|---|---|
| Control plane (governance) | `CONTROL_PLANE.md` | current |
| Anchor (mission/scope) | `ANCHOR.md` | current (active parent goal) |
| Active parent goal | `state/goals/goal-recursion-v3-harness-orchestrator.md` | active |
| Superseded parent goal | `state/goals/goal-recursion-v2-execution-fabric.md` | superseded (v2/Hermes retired, D-017) |
| Decision log | `state/DECISIONS.md` | current (D-001…D-039) |

## Decisions (state/DECISIONS.md)

- **D-017** v3 greenfield; v1/v2 retired.
- **D-018** native harness **and** unifying wrapper; no lock-in.
- **D-019** comprehensive build; lean opt-in install (Dot's "smaller release" rejected).
- **D-020** availability: auto node takeover required; single-node core + failover plugin (closes G-06).
- **D-021** delegation over credential intermediation.
- **D-022** one pause/stop vocabulary (Aggressive/Full/Lazy + checkpoint/interrupt/drain).
- **D-023** Codex App Server is the first qualification priority.
- **D-024** headline differentiator = productive continuity; six journeys as acceptance scenarios.
- **D-025** continuing autonomous research = optional bounded plugin.
- **D-026** Apache-2.0 direction; community/porter posture.
- **D-027** counsel packet authorized (no spend).
- **D-028** platform floors (closes G-11).
- **D-029** operator-provisioned trusted launch boundary.
- **D-030** harness-native driving + headless fan-out; Recursion owns the JSON stream (subject to counsel).
- **D-031** no vendor-sabotage claim.
- **D-032** parent goal is v3; sweep retired Hermes wording from prompts.
- **D-033** plugin import/port is a hypothesis to validate.
- **D-034** product thesis: "the harness that scales with your workflow."
- **D-035** plugins live outside the core (principle); no repo split during R&D (`state/PROPOSALS.md` P-001).
- **D-036** mobile: no private offline redisplay (online-only private view).
- **D-037** conformance fixtures authorized; Python disposable test harness only; product language open.
- **D-038** D-034/D-019 supersede older first-release GUI/mobile exclusions.
- **D-039** iOS/Android advisory accepted; plan a language-agnostic architecture.

## Research waves and intakes

| Wave | Path | Status |
|---|---|---|
| 28 Deep Research lanes (DS-01…DS-34) | `research-lane/wave-{a,b,b2,c}-20260930/`, `SYNTHESIS.md`, `GAPS.md`, `synthesis/domain-*.md` | complete at specification level; nothing executed |
| DS prompt suite (31) | `prompts/research-suite-20260929/` (+`check_suite.py`) | issued |
| FU-01…FU-05 prompts | `prompts/followups-20261001/` (+`check_followups.py`) | issued |
| FU-01…FU-05 return intake | `research-lane/followups-20261002/` (+`check_intake.py`, `machine/`) | intaken; specs only |
| R-A…R-D prompts | `prompts/dot-research-20261002/` (+`check_research_stack.py`) | issued |
| Scope-reframe handoff intake | `research-lane/scope-reframe-20261002/` (+`check_intake.py`) | intaken |
| R-A…R-D return (46 questions) | `research-lane/dot-ra-rd-20261002/` (+`check_intake.py`, `synthesis/SYNTHESIS.md`) | intaken; proposals only |
| R-A/R-B reconciliation | `research-lane/dot-ra-rd-20261002/synthesis/RECONCILIATION.md` | draft contract candidates |
| R-E…R-Y prompts (21 frontier handoffs) | `prompts/dot-research-20261002b/` (+`check_research_stack.py`) | issued (zip: `Recursion_Dot_Research_Packet_20261002b.zip`) |
| Overnight R-E…R-Y return (Checkpoint3) | `research-lane/overnight-20261002/` (+`check_intake.py`) | intaken; 247 files sha256-verified; all target tests unexecuted |
| FU-03 Session A (kernel reconciliation) | `experiments/v3/fu-03-kernel-session-a/` (+`check_session_a.py`) | witness confirmed; M25 killed; 36 rows dispositioned; not 36/36 |
| Shared-contract reconciliation prompt | `prompts/dot-reconciliation-20261002c/` (+`check_research_stack.py`) | issued (zip: `Recursion_Dot_Research_Packet_20261002c.zip`) |
| FU-03 Session A verification | `experiments/v3/fu-03-kernel-session-a/verification/` | **ACCEPT** (Muse Spark 1.3, candidate `74fb18d`); result on `loop/work/verify-fu03a-20261002` @ `a4c755c` |
| FU-03 Stage B execution (crash cuts/fences/Stop) | `experiments/v3/fu-03-stage-b/run/` (+`check_stage_b_run.py`) | executed at `d56cc44` (16/5/3/2/0); **independently ACCEPT** (Muse Spark; result `experiments/v3/fu-03-stage-b/verification/results/d56cc44-muse-spark-1-3.md`); bounded synthetic scope, not IT-02 |
| Shared-contracts + extensibility return | `research-lane/shared-contracts-20261002/` (+`check_intake.py`) | intaken; 5 files sha256-verified; unexecuted |
| Product thesis (D-034) | `state/DECISIONS.md`; `docs/v3/PRODUCT.md` thesis | accepted |
| Capability-driven composition prompts | `prompts/dot-research-20261002d/` (+`check_research_stack.py`) | issued (zip: `Recursion_Dot_Research_Packet_20261002d.zip`) |
| Capability-composition return (R-PC/R-UI/R-MOB, rev 2) | `research-lane/capability-composition-20261002d/` (+`check_intake.py`) | intaken; 5 files sha256-verified; R-UI `@candidate-2`; all target tests unexecuted |
| iOS/Android design + language expansion (O-04) | `research-lane/ios-android-20261002/` (+`check_intake.py`) | intaken; 3 files sha256-verified; advisory Swift/SwiftUI + Kotlin/Compose; all prototypes/tests unexecuted |
| Capability-composition conformance suite (D-037) | `experiments/v3/capability-composition/` (+`check_suite.py`) | R-PC F01–F16 implemented; **independently ACCEPT** at `050936e` (GLM-5.3-Flash); F17/F18 deferred; R-UI/R-MOB pending |
| Competitive note — T3 Code v0.0.45 (control surface) | `research-lane/competitive-notes/20261002-t3code-v0.0.45.md` | source-documented (public release notes); not installed/executed; no adoption |
| Packet-E return + PR #160 review | `research-lane/core-language-control-surfaces-20261002/` (+`check_intake.py`) | intaken; 7 files sha256-verified; R-LANG/R-CS advisory; PR #160 static review ISSUES/NEEDS-EVIDENCE; all target tests unexecuted |
| Packet-F R-AUDIT return + PR #165 review | `research-lane/packet-f-raudit-20261002/` (+`check_intake.py`) | intaken; 21 files sha256-verified; A1–A10 corrections + M1 corpus (71 bundles) + R-LANG C1–C6 + revision guide; PR #165 executed REJECT (repaired by Slice 5, re-verification pending) |
| R-PLAN + R-OPS prompts (packet 2026-10-02g) | `prompts/dot-research-20261002g/` (+`check_research_stack.py`) | issued; two handoffs (planning/executives; operations/lifecycle); M1A discrepancies remain open research questions |
| Architecture edge map | `research-lane/ARCHITECTURE-COVERAGE.md` | maintained |
| Per-harness coverage matrix | `research-lane/HARNESS-COVERAGE.md` | maintained |
| Next-phase runbook | `research-lane/NEXT-PHASE.md` | maintained |

## Specs and design baselines (docs/v3)

| Doc | Role |
|---|---|
| `PRODUCT.md` | thesis, promises, non-goals (D-018/D-031 revision pending reconciliation) |
| `INTERFACE-STRATEGY.md` | adopted CLI/TUI/GUI baseline (D-024) |
| `PLUGINS-AND-EDITIONS.md` | kernel/plugin boundary; opt-in install; availability backend (D-020) |
| `ROADMAP.md` | pipeline: FU-03 → FU-04 → FU-01/02/05 → selections |
| `ARCHITECTURE.md`, `STATE.md`, `EXECUTION-FABRIC.md`, `DECISION-REGISTER.md`, `CLAIM-LEDGER.md` | current specs; **contain deferred drift** (retired Hermes/v2 claims) — WS6 |

## Experiments (executed evidence)

| Experiment | Path | Result |
|---|---|---|
| FU-03 model | `experiments/v3/fu-03-formal-model/` | reduced-bound TLC baseline clean; 5/7 instantiated mutants killed (`check_fu03*.py`) |
| FU-04 OS conformance | `experiments/v3/fu-04-os-conformance/` | safe probes + bounded 20-trace harness; 13 run rows (3 host + 10 WSL): 12 pass / 0 fail / 1 unknown, **10 distinct PF IDs**, **10 not_run**; zero qualified profiles; floors pinned (D-028) |

## Gates and tools

`tools/check_public_scope.py`, `tools/check_next_phase.py`, `tools/check_rescope.py`,
`tools/check_v3_orientation.py` (scope-frozen to the 2026-09-13 repair), the research-lane
`check_intake.py`/`check_*` checkers, and the prompt-suite checkers.

## Review branches (nothing pushed; Lucas reviews/merges)

`loop/work/rescope-20261002` · `loop/work/fu-03-formal-model` · `loop/work/fu-04-os-conformance` ·
`loop/work/dot-ra-rd-20261002` · `loop/work/prompt-authority-sweep-20261002` ·
`loop/work/orchestrator-reconciliation-20261002`.

## Drift rules (why previous iterations drifted)

1. Every artifact gets an evidence class and a status; `unknown` is never zero.
2. Superseded artifacts stay in place with a reason; never delete the reasoning.
3. Prompts inherit **current** authority only — the retired Hermes-control wording was swept on
   2026-10-02 (D-032); do not reintroduce it.
4. Research output is evidence, not decision; only `state/DECISIONS.md` records Owner choices.
5. New waves are not added here. They land in csl-research `submissions/<record_id>/`.
