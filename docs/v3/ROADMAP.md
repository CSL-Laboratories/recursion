---
title: Recursion v3 research and construction roadmap
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
prior-status: evidence-reconciled-roadmap-proposal
---

# Recursion v3 research and construction roadmap hypothesis

> Current development entry point (2026-09-13): [START-HERE](START-HERE.md).
> The dated material below preserves its original campaign scope and evidence.
> Earlier next-step instructions, branch-specific gates and expired campaign windows
> do not define a new task or establish current runtime status.

## 2026-10-02 rescope — comprehensive build, opt-in install, community harness

Owner decisions D-018…D-027 change the direction. Recursion is repositioned as a community,
open-source harness that is both a first-class native harness and a unifying wrapper over the
harness ecosystem (D-018), built comprehensively and installed opt-in rather than cut down
(D-019). The research recommendation of a smaller initial release is rejected; its proposed
deferrals become built-but-opt-in plugins, not gate reductions.

Pipeline (development and test order):

1. Harden the deterministic core first: **FU-03** formal model/mutants/byte vectors, then
   **FU-04** OS platform conformance. These gate everything downstream; the toolchain is ready.
2. Qualify the first upstream profile (**Codex App Server**, D-023) and prototype a second,
   deliberately different adapter as an architecture stress test.
3. Then **FU-01** language/runtime, **FU-02** store crash/replay (default store + availability
   seam), **FU-05** broker protocol/chaos.
4. Build the plugin/portability layer from R-A, delegate-to-tool routing from R-B, legal posture
   from R-C, and quota measurement from R-D.
5. Independent acceptance of receipts before any selection. Language, store, renderer and plugin
   runtime remain Owner selections, decided after experiments.

The availability plugin (replicated store + coordinator election) ships in `remote`/`full`, never
in the default install (D-020). The interface baseline is
[INTERFACE-STRATEGY.md](INTERFACE-STRATEGY.md). Autonomous research is an optional, bounded,
off-by-default plugin (D-025). The 2026-10-02 scope-reframe handoff is intaken at
[research-lane/scope-reframe-20261002](research-lane/scope-reframe-20261002/).

## RESET mapping continuation — 2026-09-07

The [mapping packet](construction/reset-mapping-20260907/handoff.md) establishes a narrow
deferred-delivery route and a conditional genuine-gap route. The next construction step is to
integrate the deferred-candidate state and explicit current recovery state into both complete
broker-boundary models, preserving the old counterexample and all six obligations/27 corpus rows.
Prove the replay-store floor/snapshot provenance separately; the wire oracle cannot supply it.
Then extend typed epochs, process birth, receiver records, shared writer/pool conservation,
GoalSpec/consultation and M1A/M1C joins through the full preserved profiles. Physical control,
loss/recovery, configuration/plugin and remote probes continue alongside that work.
Formal freeze, language comparison and storage comparison remain gated as previously recorded.

## Formal construction continuation — 2026-09-07

The [first formal packet](construction/formal-comparison-20260907/handoff.md) provides an exhaustive
small flow/control graph, refinement and fairness checks, and preserved full-profile domains.
Its preventive/audit RESET counterexample defines the next bounded adapter request. Resolve that
mapping before extending the slice into the immutable wire contract; retain the existing source
and runtime observations as separately scoped evidence.

Then add typed epoch/recipient fencing, process birth/registration, receiver-record uncertainty,
writer/pool conservation, GoalSpec/consultation and literal M1A/M1C joins with all required corpus
witnesses. Execute the full core and cross-area configurations, and revisit abstract state or
proof technique when the coupling relation fails. An isomorphism failure alone does not establish
a semantic boundary fault. Physical control reserves and actual loss detection need interface
probes alongside this work. Language comparison and then complete storage comparison remain
behind the full formal gate; all integration, configuration and whole-workflow requirements remain.

## Interface campaign progression — 2026-09-07

The [bounded campaign](construction/interface-probes-20260907/campaign.md) has run installed
Codex controls with scripted inference, Hermes real-agent persistence/interruption with a
scripted provider, Windows launch-ownership faults, and finite fence/consultation vectors.
These satisfy a first discriminating probe step, not every admission or integration requirement.

Next, construct both formal boundaries after explicitly resolving the finite domains left open
in the preserved comparison contract. Use these observations to retain separate local/recipient
fences, process creation/registration, reply receipt/adoption and meter lineage/reset steps.
Require the original 27-schedule corpus, all shared obligations and a positive trigger for every
fault. Check initialization, step simulation/stutter and composition independently; retain
counterexample, inadequate mapping, exploration limit and conditional coverage as different
outcomes. No formal freeze follows from the small Python interface vectors.

Run further interface work alongside formal construction: fresh-connection questions/approvals,
fresh-process Hermes recovery, registration/relaunch faults, configuration/plugin activation and
remote uncertainty. Admit a live included-capacity route only after supported auth/helper closure
is established. Then preserve formal model -> CPython/Rust/Go comparison -> complete storage
comparison. The [next packets](construction/interface-probes-20260907/next-packets.json) retain
all eight families and six cross-area cases; no narrow quota test removes accepted capabilities.

## Mark 3 comparison dependencies and next requests - 2026-09-07

The [dossier](foundation-research/mark2-research/mark3-comparison/architectures.md) recommends this
bounded sequence under the accepted directions. Each future request needs its own exact
GoalSpec, allowed effects, fixture/build/host/capacity admission and gate; existing routine
research decisions do not need repeated Owner approval. CMP-018.

| Next bounded request | Dependency and output gate |
|---|---|
| Interface admission packet | Resolve existing build ownership/digests, supported schemas and all-role included-capacity eligibility for at most two route/host cases; no install or live call by inference |
| First interaction/configuration probes | Questionnaire-to-Head, pending waiter, queue/steer/Stop, disabled activation and revocation coalescing on public fixtures; preserve unknowns and inspect richer supported interfaces |
| Broker/process and remote probes | Exact Job/cgroup scope plus relaunch and lost-receipt faults; no unowned process, stale publication or false settlement |
| Formal adapter and two-boundary comparison | Incorporate probe counterexamples; freeze six M1 mappings, common observations/bounds/fairness and guard mutants; demonstrate split composition or move relevant broker state inward |
| Language comparison | Only after neutral formal vectors: identical CPython/Rust/Go semantics and fault schedules on Windows/Linux, with build and maintenance costs |
| Complete storage comparison | Only after language comparison: JSONL/CAS versus SQLite-WAL/CAS including metadata, artifacts, WAL, GC, replay, crash, backup and recovery |

Whole-system optimization continues alongside this sequence: A demand activation versus B
residency, and C durable scheduling only where a discriminating long-wait recovery case justifies
it. Run the preserved eight families and six cross-area cases with task completion, human burden
and fullworkflow consumption. Model, language, store, framework and production defaults remain
unselected. OR-015's common-writer migration and operational constitutional amendments remain
separate blocked work, not a deliverable silently completed by this research.

Before the formal construction request, resolve the standalone core/cross-area finite domains
in [comparison-contract.json](foundation-research/mark2-research/mark3-comparison/comparison-contract.json).
The twenty-seven-schedule corpus separates positive control13/negative partner14 from fault
schedules. Applicable faults need positive trigger witnesses and discriminating guard mutants.
Shared-pool trace21 is mandatory; rights trace22 is conditional on selecting the same extension
for both boundaries, otherwise explicitly unrun/not-covered without losing shared obligations.
Semantic or unenforceable-boundary failures motivate moving relevant state inward; inadequate
proof technique and checker limits leave the comparison incomplete without establishing a
product fault. Preserve M1A's E-PAUSED observation refusal and the authenticated supplemental
join required by M1C; neither pure classification nor a receiver replay cache authorizes a
runtime write or retransmission of an ambiguous effect.


## Mark 3 approved direction and deep comparison sequence — 2026-09-07

The [current handoff](foundation-research/mark2-research/mark3/handoff.md) supersedes the historical Owner queue.
Both decisions are accepted with qualifications (OR-036/037). Approval unblocks the direction
of comparative work, not model freeze or runtime construction.

The [twelve-area plan](foundation-research/mark2-research/mark3/research-plan.json) defines competing approaches,
source anchors, metrics, failures and disposition rules within the existing eight families.
Source comparison and whole-system optimization can proceed alongside interface/model work;
language and storage choices still depend on their earlier frozen contracts. Required
communication, delegation and interaction cannot become optional product scope because a
particular implementation costs more. Replace weak mechanisms while retaining capabilities.

Required outputs before architecture is ready to construct: competing complete designs;
capability-to-component/configuration maps; identical bounded traces across both model
boundaries; evidence-backed dispositions; explicit unresolved assumptions; and runnable,
resource-bounded construction packets. Topic lists, page counts and larger fleets do not
establish research depth or complete these outputs.

## Mark 2 continuation sequence — 2026-09-07

The historical [Mark 2 campaign record](foundation-research/mark2-research/campaign.md) and
[decision-first report](foundation-research/mark2-research/report-source.md) remain evidence
entrypoints; Mark 3 above supplies current dispositions. Historical research and completed Mark 2 answer capture retain their original
status. MKR-021/MKR-022.

| Order | Bounded output | Gate before proceeding |
|---|---|---|
| 1. Small interface probes | Exact builds and eligibility; richer SDK/server/ACP/extension traces for seven critical names and remaining requested entries | Authorized disposable scope, explicit all-role pool closure, truthful missing controls; source-only evidence cannot pass |
| 2. Formal model | Approved aggregate/refinement boundary compared with broker-inclusive model; six existing and Mark 2 obligations | Counterexamples resolved without silent M1 changes; explicit flow bounds/fairness and unchanged OR-015 |
| 3. Language comparison | Same frozen vectors in CPython, Rust and Go | Windows/Linux correctness/fault equivalence, resource/build/maintenance evidence; no language chosen in advance |
| 4. Storage comparison | Complete JSONL/CAS versus SQLite-WAL/CAS systems | Count all bytes, replay/GC/backup/crash/locking behavior; no store chosen in advance |
| 5. First usable construction | Native loop, upstream-only/combined role closure, persistent assistants, ten controls, config/plugins, setup/update/recovery | Complete accepted platform behavior and independent exact-candidate gates; small prototypes do not redefine release |
| 6. Value and expanded products | Measured continuity, lean verification, remote placement and optional advanced UI/enterprise work | Full-workflow benefit and removal criteria; no inherited performance claim |

Reuse protocol schemas, OS containment, source-grounded lifecycle patterns and maintained SDKs
where they preserve required semantics. Adapt their authority/usage/history gaps at the common
boundary. Build only missing canonical admission, evidence and reconciliation behavior.
Remove a framework/adapter layer that cannot preserve grants, imposes unbounded idle work, hides
consumption or adds no demonstrated contract/quality benefit. A2A/Orleans/Cordis and update
frameworks remain comparisons, not selected dependencies.

The earliest resource-dependent packet is a 30-minute, at-most-two-case integration probe with
actual host/build/fixture, no new metered spend, admitted included-pool reserve and human review
budget recorded before execution. It may begin with zero-inference capability discovery.
This campaign supplies the design, not permission to install, log in, enroll nodes or run providers.

## Mark 2 Owner direction — 2026-09-06

OR-026 through OR-035 add current product requirements and delegate mechanisms/defaults to research. Seven critical compatibility names are now selected; exact routes/builds and support windows remain unproved. The [Mark 2 fleet packets](foundation-research/mark2/handoff.md) keep probes before formal-model freeze, then language and storage comparisons, with current M1 and OR-015 boundaries intact. Persistent assistant, plugin, update/recovery and quality/resource work informs those contracts. All ten questionnaire answers are complete; a personal inventory is not a missing Owner answer.

## First usable platform milestone — 2026-09-06

OR-021–OR-025 require the new standalone architecture, a small native loop, independently
useful upstream execution and combined workflows. Native loop, ten terminal controls,
configuration/plugins, setup/repair, baseline compaction and Windows/Linux worker recovery
are first-release behavior, even when earlier internal prototypes are smaller.

Use the [platform roadmap](foundation-research/platform-round/architecture-roadmap.md):
small integration probes inform contract assumptions before formal-model freeze; formal model,
language comparison and storage comparison remain ordered. Advanced context/routing,
expanded engine/OS certification, dedicated GUI, enterprise infrastructure and distributed
model inference retain separate later milestones. Research completion does not mark a runtime
milestone complete or remove OR-015's migration blocker.

## Foundation priority update — 2026-09-05

Git evidence verifies merged M1A/M1B/M1C at
`a7cd9458d37f3090079930bf8d79ffd77c4f73e4`; this corrects earlier local-only status prose.
No runtime milestone is completed by this research. Next: resolve the model integration mapping,
run the bounded formal model, freeze language-neutral vectors and compare CPython/Rust/Go,
then compare JSONL/CAS and SQLite-WAL/CAS before canonical-state construction. FND-001/FND-002.

CLI capability extensions, native/upstream engine boundaries, setup and update support are
compatible proposals. Compaction/cache/fleet/resource benefits require later experiments.
Hosted tenancy, commercial tiers and graphical/media expansion remain deferred. OR-015 and
constitutional activation are not unblocked.

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas): D-038 already
put GUI in first-release scope (built but opt-in). "graphical/media expansion remain
deferred" does not defer GUI capability. This amendment does not reopen D-038.
**Beyond D-038** P6 names the daemon-served Tauri client, standalone app and "goat" bar.
Hosted tenancy remains research (F-4); commercial tiers stay deferred. See
[AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md). The [GRILLME queue](foundation-research/grillme.md)
contains pending choices without changing accepted priorities.

## Delivery model

V3 is developed through small, independently verified milestones and released as one complete v3 product promise. `core`, `standard`, `remote`, `full`, and custom manifests control installation weight; they do not create incompatible products or closed feature tiers.

This roadmap is not implementation authorization. It orders the work that a later accepted GoalSpec should convert into bounded tasks.

## Milestone 0 — evidence-grounded specification

Deliver:

- current source/runtime architecture map at the merged v2 baseline;
- exact gap map between Project Pilot/the legacy Hermes Head and the v2 MVP fabric;
- primary-source reference matrix with license/provenance review;
- provider/CLI/context capability facts with verification dates;
- benchmark and experiment plan;
- reconciled decision register and constitutional amendment list;
- complete v3 spec with normative/hypothesis boundaries.

Gate: another agent can explain the product and create bounded implementation tasks without reading the archived chat or inventing an actor.

Research status 2026-09-03: the current documentation baseline passed its corrected machine
gates and targeted same-family Terra recheck. It remains subject to unresolved owner decisions
and expiring route/reference evidence in [CLAIM-LEDGER.md](CLAIM-LEDGER.md). No live deployment
baseline or v3 performance measurement was claimed.

## Milestone 0B — focused architecture proof

Execute R1–R17 in [RESEARCH-AGENDA.md](RESEARCH-AGENDA.md): native/retained loop boundary,
direct and collaborative ARM surfaces, PAUSE, Full Access, verifier independence, plugin trust,
single-writer migration, adaptive branch capacity, retention/deletion, private-canary method and
interactive persistent terminal runtime, plus whole-system performance/leanness, model and
delegation quality, context/cache optimization, configuration architecture, scientific research
and comparative v2 benefit.

Gate: every consequential recommendation has primary evidence, contradiction and a decisive
experiment; constitutional amendment choices and remaining owner questions are exact; terminal
and multi-ARM protocols have fault/resource gates; no private canary or implementation has
started; the contract-readiness verdict is explicit.

Status 2026-09-03: research narrowed the construction candidate and the final corrected gates
and targeted adjudicator recheck passed. If separately authorized, M1A may build
only the planning schemas, validators, synthetic fixtures and in-memory replay model in
[CONSTRUCTION-RUNBOOK.md](CONSTRUCTION-RUNBOOK.md). No performance
improvement, provider capacity, cache behavior, runtime migration, terminal containment,
scientific-research benefit or whole-v3 advantage was proved. The broad OR-015 rehearsal is
blocked by its current multi-writer boundary; a fabric-only fixture may test vocabulary without
being described as that rehearsal.

### Milestone 0C — frontier harness source and experiment closure

Pinned code-path research now covers eight harnesses plus durable workflow, cache/context,
terminal/TUI, plugin/frontend, systems and autonomy/evaluation sources. The resulting minimum
kernel, adapter boundaries, contradictions and twelve preregistered comparisons are in
[FRONTIER-HARNESS-EVIDENCE.md](FRONTIER-HARNESS-EVIDENCE.md). Status remains documentation-only:
language, store, wire encoding, plugin isolation, terminal libraries, native loop, performance and
frontier standing are not selected or proved. M1A stays the first construction slice.

Status update: `OR-018` and `OR-019` now ratify Option 2 and sole Recursion canonical ownership.
The [second tranche](FRONTIER-HARNESS-RESEARCH-TRANCHE-2.md) adds a pre-code formal model,
language-neutral kernel experiment, persistence/IPC crash matrix, process-broker protocol,
plugin/frontend lifecycle, provider-neutral context capsule, corrected evaluation protocol and
adapter-free release/boot contract. The architecture is selected; its implementations and
performance remain unproved.

## Milestone 1A — autonomous-planning contract and replay baseline

Deliver only strict planning schemas, pure cross-record and DAG validation, deterministic
WorkPacket derivation, GateResult/retry/replan/escalation decisions and in-memory event replay over
sanitized fixtures.

Gate: every fixture and machine check in
[M1A-AUTONOMOUS-PLANNING.md](M1A-AUTONOMOUS-PLANNING.md) passes without provider/model calls,
processes, plugins, SQLite, Git, publication or effects. The contract is a fixture/replay result,
not a runtime or performance claim. The standalone future prompt is
[prompts/m1a-autonomous-planning-contract.md](prompts/m1a-autonomous-planning-contract.md).

Status 2026-09-04: complete and merged. The exact local implementation candidate and verifier
receipt are recorded in [M1A-CONSTRUCTION-CAMPAIGN.md](M1A-CONSTRUCTION-CAMPAIGN.md).

## Milestone 1B — route, plugin and process requirement contracts

After M1A, a separate GoalSpec may add inert schemas/fixtures for route, plugin and process
requirements. It still makes no provider call, launches no process and executes no plugin.

Status 2026-09-04: local construction is complete; its exact candidate, SOL adjudication and
limitations are recorded in [M1B-CONSTRUCTION-CAMPAIGN.md](M1B-CONSTRUCTION-CAMPAIGN.md).
Publication and merge remain human-controlled. The exact semantics, wire profile and 24-case
closed oracle are [M1B-EXECUTION-CONTRACTS.md](M1B-EXECUTION-CONTRACTS.md),
[M1B-WIRE-PROFILE.md](M1B-WIRE-PROFILE.md) and
[M1B-FIXTURE-ORACLES.md](M1B-FIXTURE-ORACLES.md).

## Milestone 1C — source, effect and persistence requirement contracts

After M1A, a separate GoalSpec may add inert source/evidence, effect-reconciliation,
publication and persistence/migration requirements. Live scholarly/provider adapters, SQLite,
external effects, Git/publication and migration stay out of M1C implementation unless a later
explicit milestone authorizes each one.

Status 2026-09-04: local construction is complete; its exact candidate, SOL adjudication and
limitations are recorded in [M1C-CONSTRUCTION-CAMPAIGN.md](M1C-CONSTRUCTION-CAMPAIGN.md).
Publication and merge remain human-controlled. Its reviewed semantics, wire profile and 24-case
closed oracle are [M1C-INTEGRITY-CONTRACTS.md](M1C-INTEGRITY-CONTRACTS.md),
[M1C-WIRE-PROFILE.md](M1C-WIRE-PROFILE.md) and
[M1C-FIXTURE-ORACLES.md](M1C-FIXTURE-ORACLES.md).

The exact M1A task graph, allowed paths and non-goals are projected in
[CONSTRUCTION-RUNBOOK.md](CONSTRUCTION-RUNBOOK.md). This roadmap does not authorize any milestone.

## Milestone 2 — canonical state and compatibility projection

Deliver the selected canonical event writer, projections, migrations, artifact metadata,
action/outbox path, backup/restore and read-only imports/views for relevant v2 state. SQLite/WAL
is a candidate only after it beats the JSONL-plus-content-addressed-artifact baseline under the
preregistered store/replay/fault comparison.

Gate: crash, duplicate, stale lease, disk pressure, migration, backup, restore, and projection rebuild fixtures pass without two writable truths.

First rehearsal: the synthetic local execution-attempt/lease aggregate only, with no external
effects, exact replay parity, complete legacy-writer enumeration, fencing, crash recovery and
proven rollback. Its deterministic gate consumes zero model quota; optional analysis remains
configurable.

The 2026-09-03 audit found that task, assignment, run/evidence, accepted-result and fabric stores
do not yet form one fenced aggregate. Milestone 2 must not begin the named rehearsal until a
machine-readable writer allowlist and common epoch cover the exact chosen boundary. A
fabric-only state-contract requirement fixture in a later M1C is evidence preparation, not this cutover.

## Milestone 3 — plugin kernel and route fabric

Deliver manifest-first capability graph, trusted built-in registry, bounded isolated third-party
plugin lifecycle, typed event bus, v2 adapter compatibility, route resolver, model-aware pools,
billing boundary, credential references and deterministic explanations. Full artifact/dependency
digests and fenced reload precede activation; disabled paths load no implementation.

Gate: supported routes negotiate from fixtures; disabled plugins are inert; failures quarantine only affected capabilities; no unauthorized billing/credential/egress transition is possible.

## Milestone 4 — Head and ARM executive runtime

After the Owner-ratified Head/ARM target is applied constitutionally and Project Pilot compatibility
semantics pass their separate decision gate, deliver first-class logical actors, executive leases,
the approved Project Pilot migration, model escalation, ARM-owned task DAGs, bounded delegation,
checkpoint-driven route changes, quota-exhaustion behavior, and upward escalation.

Gate: Head directly manages multiple independent ARM runs; each survives model replacement and restart; the kernel never becomes a semantic ARM Manager; workers cannot redefine goals.

## Milestone 5 — context and evidence system

Deliver content-addressed repository evidence, context capsules, Owner Intent Ledger, Compaction Fidelity Contract, adapter capability descriptors, one compaction owner per route, rehydration, and proof-carrying verification.

Gate: repeated compaction/handoff benchmarks retain mandatory state and reduce measured repeated context without private cross-project reuse.

## Milestone 6 — planning and interactive operator CLI

Deliver Grill Me modes, GoalSpec compiler/amendments, schema-driven setup/configuration,
detachable multi-window terminal views, persistent daemon/process-broker attachment, non-flicker
TUI and plain-text modes, tiered doctor, route/queue/resource/context explanations, Observer
Interface, and safe branch inventory/pruning.

Gate: a fresh owner can configure the first deployment and run a bounded synthetic goal without
agent-assisted config editing or a model-powered setup call; terminal close/reopen, daemon
restart, process-tree termination, high-output rendering and simultaneous clients pass the
fault/resource/accessibility suite.

Amendment proposed 2026-10-24: M6 still delivers the operator CLI/TUI. D-038 already
includes GUI as built-but-opt-in. **Beyond D-038** the same daemon serves a thin Tauri
shell client and agent-operable Control MCP. Agent-assisted setup is an additional path
(bar 5), not a replacement of zero-model bootstrap.

## Milestone 7 — authenticated node fabric

Deliver enrollment, trust root/operator credential separation, node roles, signed envelopes, replay protection, manifest-first hydration, disconnect spooling/reconciliation, capability quarantine, and cached lightweight preflight.

Gate: cross-node fault/security scenarios pass; local operation remains available without remote-node enrollment; coordinator stays within measured resource budgets.

## Milestone 8 — adaptive optimization

Deliver explainable local routing calibration, quota shadow prices, reservations, adaptive concurrency, cache-domain affinity, resource shedding, telemetry evidence providers, and an optional usage reconciler; `ccusage` remains one unverified candidate pending a separate source/license review.

Gate: shadow-mode and controlled experiments show improvement for at least one declared profile without reducing quality floors or widening authority.

## Milestone 9 — integration, editions, and private soak

Deliver release manifests, dependency locks/SBOMs, official plugin combinations, migrations from v2, construction runbook, synthetic public conformance fixtures, private canary, long-session soak, and rollback. `CANARY-01` begins with test-only characterization of one stable deterministic non-safety-critical rule, tests/fixtures only, private-approved routes, an independent clean rerun and one local candidate commit.

Gate: [ACCEPTANCE.md](ACCEPTANCE.md) passes on the exact candidate; private evidence remains private; Lucas owns merge and production deployment.

## Parallelism rules

After a contract milestone freezes its interfaces, bounded agents may work concurrently on non-overlapping plugins and fixtures. Before that point, parallel research is encouraged but competing implementations of unstable core contracts are not.

- One integration owner per milestone.
- One exclusive worktree per implementation task.
- Machine-checkable gate and explicit allowed paths for every task.
- Different model family for meaningful independent review when practical.
- No more branch creation when measured machine/review admission or the owner hard ceiling is
  reached; the policy and evidence are visible.
- Failed work returns evidence; retry requires novelty.
- Capacity is reserved for integration, verification, and recovery.

## Construction-system separation

The temporary tools used to build v3—an overnight Codex lead, Cursor implementation swarms, a Grok-powered watcher, or particular subscription arrangements—belong in a construction runbook. The product exposes generic engines, nodes, and observers; it does not hard-code the construction campaign into the release.

## Remaining blockers before construction or release

1. Lucas has selected the planning, ARM/coordination, PAUSE default, self-administering maximum
   Full Access, verifier fallback, plugin, branch, retention, edition, terminal, first-migration
   and canary directions. Constitutional amendments and the narrower owner mechanics in
   [DECISION-REGISTER.md](DECISION-REGISTER.md) remain required before the conflicting behavior
   is operative.
2. M1A must prove the planning/replay contract first. A later separately authorized M1C must
   prove any canonical event/schema mapping across overlapping v1/v2/Project Pilot contracts;
   the map in [CURRENT-AND-MIGRATION.md](CURRENT-AND-MIGRATION.md) is not implementation evidence.
3. Real subscription, privacy and cache observability remains route-specific and expiring; no
   descriptor may infer entitlement from a model listing or successful prompt.
4. Numeric coordinator/workstation budgets require the preregistered experiments in
   [PERFORMANCE-PLAN.md](PERFORMANCE-PLAN.md).
5. Plugin quota/consent composition, interactive terminal/process implementation and supported-app
   matrix, multi-ARM message semantics, sovereign audit-retention authority and deletion proof
   still require fault tests or Owner decisions. Verifier tiers/receipts are specified but inert
   until the amendment is applied.
6. The small native loop and useful native-only workflows are required for the first standalone
   release (OR-022). Expansion beyond that required loop remains an optional spike until it wins
   a preregistered metric; it cannot replace Recursion's ratified canonical authority boundary
   without an Owner amendment.

## After the integrated flow/recovery packet

The integrated finite model resolves a narrow composition question: deferred candidates and
future queued IDs can survive a modeled replay gap without unnecessary RESET or ownership loss.
It leaves consequential physical and full-domain obligations open. Next bounded requests are:

1. A local replay-store evidence probe: authenticate retention floor and snapshot identity,
   distinguish notification loss from source loss, crash between snapshot publication and RESET,
   and replay both a real gap and a recoverable queued notification. Synthetic Boolean flags
   cannot admit this result; preserve zero provider calls and explicit cleanup.
2. Complementary finite queue profiles: byte capacity4 with event capacity2, distinct event-limit
   mutation, and a fault-recovery product for actually emitted exhaustion. Keep the byte-capacity2
   profile and its witness; do not replace evidence to make a broader claim.
3. Physical control delivery, fencing, Stop escalation and reconnect mapping, then remaining
   obligations and traces in the original complete formal comparison gate.

Formal model completion precedes language comparison, then storage comparison. A temporary
file-backed probe fixture chooses no product language/store. Whole-system performance and all
eight experiment families continue alongside this dependency chain. No new Owner decision is
needed to prepare these already authorized bounded requests.

## After replay-store evidence, 2026-09-07

The [local named-cut campaign](construction/replay-store-evidence-20260907/dossier.md) advances retention-floor and snapshot-byte assumptions into observed fixture evidence. Next, specify the authoritative generation-selection seam and bounded concurrent-reader/publication tests, then integrate that evidence relation into both complete formal-model boundaries. Preserve the sequence: small integration probes → formal model → language comparison → storage comparison. Do not treat this synthetic layout comparison as a product storage decision.

A parallel configuration campaign can use the [pinned Owner-suggested management UI](construction/replay-store-evidence-20260907/source-findings.md) as inspiration for reviewed diffs, stale-response generation guards and explicit unknown quota states. Test expected-revision conflict rejection and separate accepted/applied configuration receipts before runtime propagation claims. All accepted integration routes and product interaction/delegation capabilities remain required.

## After generation authority evidence, 2026-09-07

The next bounded construction is a formal refinement of generation selection, durable pin lifetime, publisher permit and retirement intent, alongside receiver installation stages, in both accepted model boundaries. Require equal six-obligation and trace coverage and seek adverse interleavings before expanding the formal freeze claim. Keep formal model → language comparison → storage comparison; no store or framework is selected by a Python/NTFS fixture. Authentication, bootstrap corruption and power-loss probes require separate explicit admissions.

Whole-system optimization continues alongside that dependency chain. The Owner-suggested Headroom source is retained as an optional tool-output plugin candidate, with library-adapter, MCP and proxy/wrapper alternatives. [Pinned assessment and unrun probe admissions](construction/generation-authority-20260907/headroom-assessment.md) separate absent, installed, imported and activated costs, protect authoritative content and immutable originals, and retain all required native/upstream-only/combined execution paths. No plugin was installed and no vendor savings claim was reproduced.

The Owner-directed architecture challenge precedes further construction. Its [research gates](construction/generation-authority-20260907/architecture-challenge.md) prioritize composed authority/activation/child-incarnation traces, then exact provider-free adapter fixtures, then admitted whole-workflow measurements. The 75 abstract examples do not close any of the full model's 27 coverage rows. Keep historical-fork retention versus live pins and late valid effect evidence as explicit positive/negative composition cases. Four targeted research packets were sufficient for this bounded comparison; additional autonomous research should be tied to a discriminating unanswered question.

## Generation and effect composition, 2026-09-08

The generation/fork/recipient composition now has bounded exhaustive evidence and four complete ordered recovery examples. Next integrate literal effect-intent/attempt joins and typed authority boundaries into the formal coverage ledger, keeping PAUSE refusal and retained-evidence classification distinct. Then combine the checked generation/recipient relation with prior ingress/RESET/replay semantics under explicit shared state and budgets; separately passing subsystem graphs cannot certify that product. Use the full27-row ledger to select missing triggers and seek the smallest complete declared core/cross profiles rather than treating more narrow passes as a freeze.

In parallel, prepare provider-free own-code adapter fixtures for configuration closure, invocation quiescence and incarnation/evidence mapping. Keep native and all required upstream execution paths equally available. Headroom and other optional implementations remain competitors behind Recursion-owned interfaces; no upstream fork is required. Whole-workflow quality/resource comparisons follow exact route and included-capacity admission. Formal model → language comparison → storage comparison remains required. [Next bounded requests](construction/composition-model-20260908/handoff.md).

## After literal effect evidence, 2026-09-08

The literal effect seam now has an own-code restricted adapter and discriminating local evidence. Its strongest new constraint is an explicit settlement hold for an uncompleted attempt even when the unchanged M1C oracle reports an earlier success. Integrate that relation, authenticated-cut assumptions, historical prefixes and canonical quarantine persistence into both formal boundaries before expanding completion claims. Combine it with the checked generation/recipient and ingress/RESET/replay subsystems under shared domains; separate passes remain insufficient.

Next bounded construction should target deterministic evidence-cut ownership and settlement projection with corruption/omission/restart and duplicate receipts, plus the shared formal composition. Preserve M1 immutability: any necessary extension is a separately specified bridge, not a retrospective contract edit. Continue configuration/consultation/context adapter research where it answers a distinct missing trace. Full formal gate → language comparison → storage comparison remains the dependency chain. All required upstream/native/combined routes and whole-workflow experiment families remain intact. [Concrete next admissions](construction/effect-evidence-20260908/handoff.md).

## Shared composition progress — 2026-09-08

The campaign following literal effect evidence connects generation lifetime,
evidence cuts and internal replay installation in two independently implemented
finite boundaries. Its free shared core and targeted profiles advance the formal
comparison stage; they do not finish it. See
[results and limits](construction/shared-composition-20260908/findings.md).

The next construction gaps are the original shared-pool/reservation schedule and
literal M1B correspondence, followed by typed goal/lease/consultation composition.
[SC-WR-01, SC-WIRE-02 and SC-EPOCH-03](construction/shared-composition-20260908/next-probes.md)
name dependencies, admissions, failure counters and decisive gates. Review the
first two independently, then couple them before promoting an aggregate obligation.
Formal freeze still precedes language comparison, then storage comparison; whole
system optimization continues alongside. Existing rich/lean capabilities, all
integration routes and full-workflow experiment families remain required.

## After shared-reservation construction — 2026-09-08

SC-WR-01 supplies scoped central admission and one-transfer rights evidence while
preserving the original6+6-from10 schedule and uncertain-effect reservation. Next
construct SC-WIRE-02 literal M1B correspondence independently, then couple it to
these reservation and replay boundaries. SC-EPOCH-03 keeps the typed consultation
work. Do not replace full core/cross coverage with accumulated independent slices.
Formal freeze still precedes language then storage comparison. Native/upstream-only/
combined integrations, eight experiment families and six cross-area cases remain
required. No new Owner direction or product framework selection follows.
[Next bounded request](construction/shared-reservations-20260908/next-probes.md).

## SC-WIRE-02 — 2026-09-08

SC-WIRE-02 constructs the bounded literal correspondence packet. SC-EPOCH-03 is the next separately authorized typed consultation campaign. A coupled shared-reservation/wire/epoch construction must precede aggregate promotion; preserve the original six obligations,27 traces, eight experiment families, six cross-area cases and native/upstream-only/combined routes. Language and storage comparison remain after full formal comparison.
[Evidence and limits](construction/wire-correspondence-20260908/evidence.json).

## SC-EPOCH-03 — 2026-09-08

SC-EPOCH-03 is the second Owner-authorized successor campaign. The next bounded request is the coupled shared-reservation/wire/typed-consultation construction, preserving original6+6-from10 and full flow/byte/framing obligations. Keep all6 obligations,27 traces, eight experiment families, six cross-area cases and native/upstream-only/combined routes. Full formal comparison precedes language then storage comparison.
[Evidence and residuals](construction/typed-consultation-20260908/evidence.json).

## SC-COUPLED-01 — 2026-09-08

SC-COUPLED-01 adds scoped central reservation/wire/consultation composition. Next propose coupled effect-evidence-cut and PAUSE admission for original10/13/14/15 crossings, preserving current bounds. General rights, full core/cross comparison, authenticated physical persistence and common-writer migration remain residuals; product language/store/framework selection remains deferred.
[Evidence and limits](construction/coupled-construction-20260908/handoff.md).

## Coupled evidence cut and PAUSE — 2026-09-08

SC-CUT-PAUSE-01 adds scoped coupled cut/PAUSE support. Next proposed bounded work is retained-cut ownership and stale publication; full comparison precedes language/storage selection and common-writer migration remains blocked.
See [scoped findings and residuals](construction/coupled-cut-pause-20260908/findings.md). No runtime integration or full formal freeze follows.

## Retained-cut publication — 2026-09-08

SC-CUT-PUB-01 adds bounded retained-cut publication and reader ownership correspondence;
all six original obligations,27 traces,experiment families and integration requirements
remain. Proposed next bounded request: complete-source attestation and append admission,
which must construct the current trusted completeness premise. Orphan-reader recovery,
physical persistence, full original comparison and common-writer migration remain separate gaps.

See [SC-CUT-PUB-01](construction/cut-publication-20260908/handoff.md) for evidence and limits.

## Complete-source construction — 2026-09-08

SC-CUT-SOURCE-01 narrows complete-source attestation to a constructed registration/append
protocol with explicit bounded adversarial and scaling evidence. It does not close source
truth outside admitted events, physical recovery/reclamation, orphan readers, full original
formal comparison or common-writer migration. No successor is activated by this result.
See [remaining gaps](construction/cut-source-overnight-20260908/next-probes.md).
