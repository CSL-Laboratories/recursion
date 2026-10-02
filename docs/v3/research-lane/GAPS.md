# Recursion v3 research gap register

Status: research synthesis of syntheses. Not a decision, not independently verified, not rerun.
Every row traces to a named lane or domain synthesis under `docs/v3/research-lane/`; no gap is
invented here, and no experiment, benchmark, model run or benchmark fixture was executed to
produce this file.

This register consolidates the unrun experiments, unmeasured cells and open questions that the
four domain syntheses and their 28 lane syntheses record. It is the input list for the follow-up
research wave: each P1 gap has one self-contained follow-up prompt under
`docs/v3/prompts/followups-20261001/`.

## Conventions

- **Source lanes.** `DS-nn` identifies the lane synthesis; the parenthetical is the domain
  synthesis that digests it (`A` kernel/language/platform, `B` authority/safety/lifecycle,
  `C` operations/capacity/evaluation, `D` context/interaction/product).
- **Type.** `experiment` = a preregistered protocol that must be run before the gap closes;
  `research` = an open evidence question answerable by further study; `decision` = an Owner
  choice that research cannot close and must not pre-empt.
- **Priority.** P1 = blocks the architecture phase and is answerable by one bounded follow-up
  prompt; P2 = blocks a specific milestone or contract; P3 = needed eventually or gated behind
  another gap.
- Only P1 gaps receive follow-up prompts. Owner `decision` gaps are recorded for
  `state/DECISIONS.md` but are not prompt targets; a follow-up research session must never decide
  them.

## Domain A — kernel, language and platform

| ID | Gap | Source lanes | Why it matters for architecture | Type | Priority |
|---|---|---|---|---|---|
| G-01 | The decision-producing language/runtime experiment `EXP-FHAR-10` was never run; no implementation language is selected and every resource, tail, FFI, cold-start and dependency-closure cell is unmeasured. | DS-18 (A) | The ratified Option 2 target cannot be built and every later milestone bakes in concurrency, memory, packaging and fault semantics; a wrong base is expensive to unwind. | experiment | P1 |
| G-02 | No canonical crash/replay benchmark ran between segmented JSONL + CAS and SQLite WAL + CAS; no store is selected, so exact replay digest, stale-writer exclusion and power-loss durability have no measured basis. | DS-21 (A) | Blocks Milestone 2 (the canonical writer) and every durability, backup and recovery contract that consumes it. | experiment | P1 |
| G-03 | The TLA+/TLC finite model, the 36 classified reachable mutants, the language-neutral byte-level vector corpus and two independent implementations do not exist; the four-function kernel freeze is conditional. | DS-22 (A) | Without model, mutants and vectors the kernel cannot be frozen, and the language comparison of G-01 is not admissible because it needs the byte corpus first. | research | P1 |
| G-04 | No OS native-semantics conformance fixtures or machine-readable platform-profile manifest exist; there are zero measured cells for containment, PTY/ConPTY teardown, atomic-replace durability, lock release or supervisor races. | DS-19 (A) | Every process, terminal, filesystem and supervision contract the broker and canonical writer depend on is unqualified on the target Windows/Linux builds. | experiment | P1 |
| G-05 | The IPC/process-broker protocol is only conditionally adopted; four freeze blockers (version negotiation, snapshot materialization, cursor rollover, ACK-release wording) and the native Windows/Linux chaos matrix are unpassed. | DS-23 (A) | The v1 protocol cannot be frozen, and no detachable-terminal, slow-client isolation, containment or MCP-boundary claim is safe. | experiment | P1 |
| G-06 | The availability/authority contract is undecided: may the single fenced canonical writer be temporarily unavailable after coordinator loss? | DS-20, DS-21 (A) | Decides whether consensus, automatic failover and multi-node authority are in v3 scope at all; the domain synthesis calls it the shared pivot. | decision | P2 |
| G-07 | No single PAUSE/Stop model is settled across configuration, the formal model and the broker; `checkpoint`/`interrupt`/`drain`/emergency and the drain allowlist remain unresolved. | DS-01, DS-09, DS-13, DS-22, DS-23, DS-32 (A/B/C/D) | The pause/stop vocabulary is the substrate for revocation, disconnect and admission; three surfaces currently disagree. | decision | P2 |
| G-08 | The 2-vCPU/4-GiB coordinator-small profile is a target, not a demonstrated capacity point; preflight "seconds", service-topology SLOs, watchdog timeouts and recovery decomposition are unverified. | DS-20 (A) | Admission limits, recovery budgets and enterprise topology cannot be fixed without this capacity evidence. | experiment | P2 |
| G-09 | The configuration architecture is frozen in design but has no cross-platform conformance corpus, no chosen human syntax and no agreed authority lattices for privacy exposure and publication. | DS-13 (A) | Editors, edition gating, migration and the resolver cannot be built to a testable contract without it. | experiment | P2 |
| G-10 | The renderer and the plugin runtime remain unselected across every lane. | DS-04, DS-13, DS-18, DS-22, DS-23 (A/B) | Gates the terminal milestone and the plugin-isolation tier; DS-18 defers a WASI/WASM plugin-isolation evaluation. | research | P2 |
| G-11 | Exact Windows build floors, Linux kernel/distribution profiles, WSL2 production status, SMB/NFS scope and the definition of "survives power loss" are undecided. | DS-19 (A) | Sets the supported platform matrix every conformance, durability and containment test is judged against. | decision | P3 |

## Domain B — authority, safety and lifecycle

| ID | Gap | Source lanes | Why it matters for architecture | Type | Priority |
|---|---|---|---|---|---|
| G-12 | The preregistered 480-candidate blinded verifier-independence matrix (120 per risk class, six defect families, treatments T0–T7) was never run; provider/account/host semantic value stays unverified. | DS-03 (B) | The V0–V3 tiers and false-green control are a hard floor; the tiers currently rest on unmeasured independence dimensions. | experiment | P2 |
| G-13 | The private canary has never executed: no route was exercised, no write-boundary/oracle path was tested end to end, and no independent authoritative oracle is shown to exist. | DS-08 (B) | The write-boundary and independent-rerun protocol is untested, so any later canary claim is unsupported. | experiment | P2 |
| G-14 | No cutover, restore or reverse-reader rehearsal ran for single-writer migration and effect rollback; no complete writer inventory or epoch-enforcing storage path is shown. | DS-05 (B) | The migration/rollback contract has no executed evidence on even the synthetic aggregate. | experiment | P2 |
| G-15 | The credential/egress broker has no adversarial threat-suite exercise and no measured zero-model preflight latency, resource use or bypass count; entitlement/distribution/ToS stay unknown. | DS-24 (B) | The sole credential-resolver and egress-decider boundary is unproven before it gates `E-FHAR2-104`/`105`. | experiment | P2 |
| G-16 | The deletion evidence contract has no negative-fixture run (copy ledger, key ledger, provider residual windows, disconnected spools); physical destruction on a lost node stays unknown. | DS-07 (B) | Retention/deletion is a hard privacy floor and `verified-complete` must be a derived, independently accepted state. | experiment | P2 |
| G-17 | The pause/effect-fencing crash-stage matrix and per-adapter fence conformance are unrun; no adapter is shown to place a fence near its irreversible boundary. | DS-01, DS-02 (B) | Receiver-side fencing is the substrate for revocation, disconnect and admission safety. | experiment | P2 |
| G-18 | The Full Access capability-vector model is not shown to be implemented in the kernel or adapters, and adapter fencing/idempotency is explicitly unverified. | DS-02, DS-04 (B) | Authority admission and plugin authority grants depend on the typed capability lattice being real. | research | P3 |
| G-19 | Native-plugin containment and cross-runtime capability enforcement are unresolved; the zero-effect disabled invariant has no tripwire run. | DS-04 (B) | Determines whether plugin isolation can be enforced at the claimed tier. | research | P3 |

## Domain C — operations, capacity and evaluation

| ID | Gap | Source lanes | Why it matters for architecture | Type | Priority |
|---|---|---|---|---|---|
| G-20 | No v2-vs-Options benchmark, R17 ablation or PERF-003 0/10/100 disabled-plugin matrix ran; Option 2's performance benefit is unverified and the benchmark manifest is undefined. | DS-10 (C) | Every performance/complexity claim over v2 and Option 1 is unverified; the manifest is the top unresolved Owner input. | experiment | P2 |
| G-21 | The dual-domain adaptive admission controller has no fixed-vs-adaptive crossover, no per-slot marginal-cost profile on either hardware profile, and no Little's Law validity check. | DS-06 (C) | Water marks, reserves, dwell and concurrency limits have no measured basis. | experiment | P2 |
| G-22 | The routing/delegation confirmatory campaigns, pilot and ARM-count sweep have not run; no local corpus or acceptance margin exists. | DS-11 (C) | Route/delegation policy and the false-green/review-cost endpoints are unmeasured. | experiment | P2 |
| G-23 | The PERF-010 0/1/5/10/20-view matrix and adversarial containment tests have not run; whole-descendant-tree termination is unverifiable from schema. | DS-09 (C) | The terminal view-count SLA, slow-client isolation and containment guarantees are unmeasured. | experiment | P2 |
| G-24 | The update/release/DR six-mode by four-outcome matrix and the restore trial are `unknown` in every cell; no engine-specific snapshot mechanism is selected. | DS-25 (C) | Recovery, rollback and DR claims have no demonstrated cell and no numeric RPO/RTO. | experiment | P2 |
| G-25 | No telemetry adapter was exercised and no shadow evaluation ran; findings are documentation-qualified, not runtime-qualified. | DS-26 (C) | Counters cannot drive budgets, dashboards or route learning until adapters are runtime-qualified. | experiment | P2 |
| G-26 | No Windows PSI equivalent, route-level capacity metrics, portable wakeup count or host monetary cost are established. | DS-06, DS-10, DS-26 (C) | Admission, accounting and cost models lack their machine-pressure and monetary inputs. | research | P3 |

## Domain D — context, interaction and product-adjacent

| ID | Gap | Source lanes | Why it matters for architecture | Type | Priority |
|---|---|---|---|---|---|
| G-27 | The context/compaction/cache route-by-route synthetic suite was never run; Recursion's route inventory and all cache economics are unknown. | DS-12 (D) | The capsule/governor substrate blocks every interaction lane, but has no measured route or recovery data. | experiment | P2 |
| G-28 | The scientific-ARM hard-floor corpus and the Option A/B/C comparison were specified but not executed; rights operations remain open. | DS-14 (D) | The research/evidence capability's zero-tolerance false-acceptance floor is untested. | experiment | P2 |
| G-29 | The ARM-conversation F01–F24 fixtures and the matched direct-addressing evaluation were designed but not run. | DS-15 (D) | Direct addressing and bounded step leases have no executed fixture or coordination-benefit evidence. | experiment | P2 |
| G-30 | The structured-vs-prose resume-error comparison and scheduler/daemon crash-recovery semantics were not run. | DS-30 (D) | Multi-project supervision state has no measured accuracy advantage and no recovery semantics. | experiment | P3 |
| G-31 | HITL synthetic fixtures are acceptance targets only; the Lazy-Stop observable boundary and the ten terminal controls remain undefined. | DS-32 (D) | The pre-effect mediation boundary lacks its stop semantics and an authoritative control contract. | experiment | P3 |
| G-32 | No Recursion-specific licensing/IP analysis exists; counsel review is required before any distribution, publication or training decision. | DS-33 (D) | Legal clearance gates any release decision and is not a technical patch. | decision | P3 |
| G-33 | Decision-journaling R1-vs-R0 was not run; no optimal cadence or local calibration benefit is established. | DS-34 (D) | The acceptance-gate and review discipline has no measured reconstruction or timely-review benefit. | experiment | P3 |

## Priority P1 first actions

The five P1 gaps are the kernel/language/platform blockers; they are the only gaps that both
gate the architecture phase and are answerable by one bounded follow-up. Three of them form a
dependency chain: **G-03 supplies the vectors and refusal taxonomy that G-01 consumes**, and
**G-04/G-02 supply the platform facts the store and broker contracts assume**.

- **G-01 — language/runtime experiment.** First action: commission FU-01 to produce the
  decision-ready `EXP-FHAR-10` evidence pack and preregistered experiment specification. Do not
  select a runtime.
- **G-02 — canonical store crash/replay benchmark.** First action: commission FU-02 to produce
  the preregistered crash/replay/fault benchmark specification and candidate-neutral acceptance
  oracle. Do not select a store.
- **G-03 — formal model, mutants and byte vectors.** First action: commission FU-03 to produce
  the finite TLA+/TLC specification, the classified 36-mutant corpus and the language-neutral
  byte-vector corpus specification. This is a prerequisite for G-01.
- **G-04 — OS platform conformance fixtures.** First action: commission FU-04 to produce the
  machine-readable, platform-profile-keyed conformance manifest and preregistered negative-trace
  fixture suite.
- **G-05 — broker protocol freeze blockers and chaos matrix.** First action: commission FU-05 to
  produce the decision materials closing the four blockers and the preregistered native chaos
  matrix specification.

## Follow-up prompts

One self-contained prompt per P1 gap, at `docs/v3/prompts/followups-20261001/`:

| Gap | Domain | Source lane(s) | Prompt |
|---|---|---|---|
| G-01 | A | DS-18 | `FU-01-language-runtime-experiment.md` |
| G-02 | A | DS-21 | `FU-02-canonical-store-crash-replay.md` |
| G-03 | A | DS-22 | `FU-03-formal-model-and-conformance-vectors.md` |
| G-04 | A | DS-19 | `FU-04-os-platform-conformance-fixtures.md` |
| G-05 | A | DS-23 | `FU-05-broker-protocol-chaos.md` |

## Traceability and limits

- Every row's source is a recorded lane synthesis or its domain digest; no new claim, URL or
  measurement is added here. The syntax `[Unverified]`, `unknown` and `unavailable` used by the
  lanes is preserved rather than collapsed to zero.
- This register is itself a summary of summaries. It is not a decision, not authorization to
  implement, spend, deploy, publish, enroll, delete or cut over, and not evidence that any
  experiment has run.
- Owner `decision` gaps (G-06, G-07, G-11, G-32) must be resolved in `state/DECISIONS.md`; a
  follow-up research session may inform them but must never decide them.

## Status update — 2026-10-02 follow-up batch

The five P1 follow-up prompts were run through the Codex Dot. Their outputs are intaken at
`docs/v3/research-lane/followups-20261002/`. Outcome:

- **The G-01–G-05 specifications are now complete; the gaps are NOT closed.** The reports propose
  the experiments, contracts, fixtures, candidate definitions and acceptance oracles but did not
  run them — no benchmark, model-checker run, chaos case or platform qualification. They stay
  `experiment`/`research` gaps until the local experiments run and are independently accepted.
- Prerequisite ordering is confirmed: FU-03 (G-03) gates FU-01 (G-01); FU-04 (G-04) supplies the
  platform facts FU-02 (G-02) and FU-05 (G-05) assume.
- A read-only repository architecture audit (`followups-20261002/synthesis/architecture-audit.md`)
  adds a construction-stage finding: the tree is a contract-and-experiment foundation, not yet a
  usable v3 harness, and it flags authority/scope drift to repair before construction.
- Owner `decision` gaps (G-06, G-07, G-11, G-32) remain decisions; no follow-up research closes
  them.

## Status update — 2026-10-02 rescope and scope-reframe intake

Owner decisions recorded in `state/DECISIONS.md` D-018…D-027 change the register:

- **G-06 closed** by `D-020`: automatic node takeover is required for long-running autonomous
  work and is user-toggleable; the shipping core is single-node with a fenced writer and a
  store/availability seam; replicated failover is a first-party plugin.
- **G-07 closed** by `D-022`: one prompt/stop vocabulary (Aggressive/Full/Lazy + `checkpoint`/
  `interrupt`/`drain`) across configuration, formal model and broker.
- **G-11 closed** by `D-028`: Windows 11 25H2 (build 26200) minimum (24H2 build 26100 Enterprise
  also qualified), x64 primary; Linux 6.12 LTS primary / 6.6 LTS minimum with cgroup v2; WSL2
  dev/test only; canonical state on local NTFS/ext4 only (SMB/NFS out of scope); "survives power
  loss" = application-consistent after an abrupt VM reset at the commit boundary.
- **G-32 remains open** and is counsel-gated; research packet R-C prepares a counsel-ready
  question packet (no spend).
- **Release-cut recommendation rejected** (`D-019`): the 2026-10-02 research recommended a smaller
  initial release with named deferrals; the Owner chose the comprehensive build with opt-in
  install instead. The proposed deferrals become built-but-opt-in plugins, not gate reductions.
- New research gaps commissioned as packets R-A (plugin portability), R-B (cross-harness
  delegation / productive continuity), R-C (route legality and licensing), R-D (quota-efficiency
  measurement) under `docs/v3/prompts/dot-research-20261002/`.
- The 2026-10-02 scope-reframe handoff is intaken at
  `docs/v3/research-lane/scope-reframe-20261002/` (source-documented, nothing executed).

## Status update — 2026-10-02 R-A…R-D research return

The R-A…R-D research return (46 questions) is intaken at
`docs/v3/research-lane/dot-ra-rd-20261002/` with a synthesis at
`.../synthesis/SYNTHESIS.md`. Outcome:

- **Proposals, not closures.** R-A (plugin portability contract + manifest schema), R-B
  (delegation record + HonestAccounting + the H1 productive-continuity experiment), R-C
  (counsel-ready packet, Q01–Q12), R-D (quota/lock-in measurement spec + preregistration
  templates). Nothing was executed; no legal clearance; porting savings and continuity remain
  unmeasured.
- **G-32 remains open**; R-C prepares the counsel packet only. No route is cleared.
- **Reinforces** D-019/D-021/D-023/D-024/D-026; no conflict found.
- New reviewable work items (no IDs assigned yet): adopt/reconcile the R-A plugin contract;
  reconcile R-A/R-B shared semantics (capability/grant/attenuation/evidence/refusal/accounting);
  draft the Codex App Server qualification spec; draft the R-D measurement preregistration; hand
  R-C to counsel with Lucas's deployment/tier/entity facts.
- Three-phase local handoff: (1) reconcile evidence + Owner acceptance of any normative contract;
  (2) first qualification spec under separate authorization; (3) freeze measurement + counsel.

## Status update — 2026-10-02 overnight research + audit

The overnight checkpoint (pin `73d70505`) is intaken at
`docs/v3/research-lane/overnight-20261002/` (247 files, sha256-verified). It is research coverage
only; **every target test is unexecuted**. Corrections to this register:

- **G-03 is not "does not exist".** A reduced-bound TLA+/TLC model and a 7-mutant battery now
  exist (`experiments/v3/fu-03-formal-model/`), but they are `researched-not-run` for the
  acceptance gate: the 4-command-ID bound cannot exceed four transitions, 5/7 is not 5/36, and the
  proposed 7-step `NoBlindEffectReplay` witness is source-level inference outside the executed
  bound. The kernel freeze stays conditional.
- **G-04 is not "zero measured cells".** A bounded host+WSL trace harness exists
  (`experiments/v3/fu-04-os-conformance/`), but **zero platform profiles are qualified**; WSL is
  dev/test only (D-028). Native Windows and native Linux remain unqualified.
- **G-31:** the ten controls and the Lazy-Stop boundary are now defined at design level
  (`INTERFACE-STRATEGY.md` + D-022); the fixtures remain unrun.
- New candidate kernel issues (static/inferred, unexecuted) are recorded in the intake's `gaps/`
  and `additional-kernel-research/` (e.g. SK03 second-attempt unreachability, SG03 effect-key
  widening, and the `CORR/GOAL` fixture setup qualification). None changes a decided gap status.
- Owner interpretation still needed: GUI release timing (D-019 built opt-in GUI vs "GUI later").

This supersedes the older wording in the G-03/G-04/G-31 rows above; those rows are retained as the
historical register.

## Status update — 2026-10-02 capability-composition research return (packet d, revision 2)

The capability-composition return is intaken at
`docs/v3/research-lane/capability-composition-20261002d/` (5 files sha256-verified; source pin
`b696f6e…`). Research coverage only; **every target runtime/performance fixture is unexecuted**.
Outcome:

- **Proposals, not closures.** R-PC (composition manifest + eight-pass compiler + F01–F18), R-UI
  (`recursion.ui-contract@candidate-2` typed contribution points + UI-01…UI-20 + C×V soak), R-MOB
  (mobile session/read-cache/notification candidates + MOB-F01…F24 + provider-free path simulation).
- **Revision-2 correction.** The first R-UI delivery used bare local IDs in resolved records
  (ambiguous across suppliers sharing a local name); candidate-2 adds `ContributionKey`/`NodeKey`
  and supplier-qualified resolution. A specification defect, not an observed runtime failure. R-PC
  is byte-unchanged; R-MOB changes one reference line.
- **New unresolved Owner policy:** private offline-content retention / offline protected redisplay
  (R-MOB Q11) — the literal never-serves-an-unseen-revocation requirement is unsatisfiable offline.
- **G-09/G-10/G-19 remain open** (privacy/publication order, renderer/runtime, containment) and
  **G-32 (licensing/counsel, distribution) remains open and counsel-gated**; no language, store,
  framework, transport, authentication mechanism, push provider or renderer selected.
- **Reinforces** D-019/D-021/D-022/D-024/D-029/D-031/D-033/D-034; no conflict found.

## Status update — 2026-10-02 packet-E return + PR #160 review

The packet-E return and the static R-PC suite review are intaken at
`docs/v3/research-lane/core-language-control-surfaces-20261002/` (7 files sha256-verified).

- **R-LANG** (advisory): no language selected; Rust/Go/CPython shortlist, C#/.NET first expanded
  challenger; JS1-vs-SQL1 store unmeasured; language-neutral plugin contract **P1**; reconstruction
  **RC-01…RC-09** and an unresolved-seam register to author. Feeds D-039.
- **R-CS** (advisory): T3 Code v0.0.45 and peers; D-034's moat is the combined composition+authority+
  continuity+accounting+portability contract; strong-baseline falsifier; **G-32 open**.
- **R-PC suite review:** rerun/reproducibility **ACCEPT** (GLM-5.3-Flash) but **full F01–F16
  specification fidelity ISSUES FOUND / NEEDS EVIDENCE** (Astra Q01–Q10). The suite is a scaffold;
  repairs are ordered in `experiments/v3/capability-composition/REVIEWS.md`. Any change to the tested
  files restarts verification.
- **Queued:** revise the D-039 outline against R-LANG; strengthen R-PC; start R-UI UI-05 with
  independent qualification asserts; correct the UI-01 wording.
- No technology selected; no protected path edited; all target tests unexecuted.

## Status update — 2026-10-02 iOS/Android design + language expansion (O-04)

The separately requested OS/language expansion is intaken at
`docs/v3/research-lane/ios-android-20261002/` (3 files sha256-verified; pin `b696f6e…`, consuming
`recursion.ui-contract@candidate-2`). Research coverage only; **all prototypes/tests/benchmarks
unexecuted**.

- Advisory stacks: iOS **Swift + SwiftUI** (small UIKit boundary); Android **Kotlin + Compose**
  (Views and RN New Architecture are genuine rivals). No stack, toolkit, renderer, store, auth,
  transport or push provider selected.
- **Reconciliation flags:** R-MOB `MOB-F01…F24` vs the shared **`MP01…MP11`** matrix must become one
  suite; Android does not yet register a concrete `ClientCapabilityProfile@candidate-2` /
  `UiMaterializationManifest@candidate-2`.
- **D-028 does not set an Android floor** — Android min/target API and supported profiles remain open.
- Offline retention: iOS reports it undecided (vs R-MOB's conservative hold); Owner decision O-01
  governs. **G-32 (counsel/distribution) remains open.**
- **Reinforces** D-018/D-019/D-022/D-024/D-034/D-035; no conflict found.
