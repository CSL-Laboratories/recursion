---
title: Recursion v3 ratified architecture target
status: owner-ratified-target-non-operative-until-amendment
last-updated: 2026-09-07
baseline-commit: 98113d6c6d3debe2a4d207c51bb1780f7356d714
---

# Recursion v3 ratified architecture target

## First formal construction slice — 2026-09-07

The [formal construction packet](construction/formal-comparison-20260907/handoff.md) implements
separate abstract, split-composition and inclusive transition definitions for a small preventive
broker flow/control model. It is refinement to that declared subsystem specification, not to the
complete M1B wire-audit reducer or the full Recursion model.

FMC-002: finite hidden-choice controls exhibit equal visible prefix languages while neither naive
single initial-state map works. Named weak-fairness controls reject infinite requeue starvation of
continuously enabled Stop, while coarse broker fairness and intermittently disabled Stop permit
it. These calibrations distinguish method/assumption limitations from a product counterexample.

FMC-003: exhaustive exploration reached 444,834 states and 2,644,215 transitions in each concrete
model, with empty frontiers and maximum discovered depth 35. Every concrete step matched the
separate abstract specification or preserved its visible state; the separately implemented
inclusive relation matched the split's reachable labeled graph. Twenty-two guard mutants were
rejected. All three implementations were authored by the same integrator; independent review and
immutable oracle controls caught shared initial mistakes. Agreement alone is insufficient.

The result is conditional on one declared small environment, a direct logical Owner-control path
and immediately signaled ingress loss. External-effect uncertainty is a preserved constant here.
Full core/cross-area profiles, receiver/process/pool/GoalSpec obligations and the remaining trace
corpus are explicitly unimplemented. The smaller slice is not a cutoff theorem or formal freeze.

## Interface evidence and formal construction boundary — 2026-09-07

The [interface campaign](construction/interface-probes-20260907/campaign.md) advances the
comparison from source inspection to bounded installed-harness and OS observations. It supports
adapting rich harness protocols behind an application-owned authority/accounting boundary.
It supplies no measured winner among demand activation, residency and durable scheduling.
The newer observations below are scoped deltas; earlier source-only statements remain dated
campaign records.

IPR-007: finite recipient-fence and consultation fixtures check 4,704 and 40,320 complete
schedules, respectively, with 16 broken-guard variants rejected. They preserve pre-install
permit exposure, stale receipt rejection, retained evidence, scoped Stop and fresh independent
consultation. Their separate event-fact oracles do not establish independent broker refinement
or fairness. See [exact vectors and limits](construction/interface-probes-20260907/contract-vectors.json).

The next formal campaign must implement both accepted boundaries against the same full corpus.
In the split, broker delivery/ownership steps either preserve all aggregate observations or map
to explicit aggregate transitions. In the inclusive model, those same steps remain distinct:
do not atomically combine local revoke with recipient installation, or answer receipt with
authoritative adoption. Probe observations become environment constraints and counterexample
vectors, not a reason to erase uncertain effects. No M1 change, formal freeze, language/store
selection or resolution of OR-015 follows from this fixture campaign.

## Mark 3 comparison: canonical state and scheduling boundary - 2026-09-07

The [comparative dossier](foundation-research/mark2-research/mark3-comparison/architectures.md)
compares demand-activated services, a resident session hub and durable workflow assistance
against the same authority contract. These are candidate arrangements, not selected languages,
stores or frameworks. Residency and durability are independent axes: compare bounded warm
pools within A/B, then hold residency constant when comparing receiver-owned operation records
against workflow assistance. Demand activation is the first probe target; no runtime winner
has been measured. CMP-001, CMP-005, CMP-006, CMP-014, CMP-029.

The proposed effect path is authenticated command -> current authority/identity/reservation
admission -> atomic canonical intent/receipt -> broker dispatch -> recipient observation ->
reconciliation. Commit and dispatch are separate failure boundaries. Persist a stable operation
identity before dispatch. A receipt of a command, queue drain, process exit and settled external
effect remain separate states; unknown effects retain their reservations. Only one component
owns retry for an operation. Timers and workflow journals may propose wakeups, but each wakeup
rechecks current authority, PAUSE, ownership and remaining allowance without model inference.

A service/actor journal is subordinate adapter state. It cannot accept GoalSpec amendments,
become a competing writer, renew budgets or decide that an external effect succeeded. Reconcile
it against the canonical ledger after restart. T3's atomic command pattern is useful, but its
runtime receipt bus is test-only and its inspected queues are unbounded. Temporal activity
cancellation is cooperative; Orleans reminders skip missed ticks; Restate's invoker fence and
journal acknowledgement distinguish attempt state from external completion. Those limits must
be preserved when adapting the mechanisms. No runtime reuse is selected here.

The [formal-boundary specification](foundation-research/mark2-research/mark3-comparison/formal-boundaries.md)
provides shared observations, six cross-contract obligations, original and cross-area bounds,
coupling assumptions, constructed traces and guard-removal criteria for OR-036. Neither
model has been implemented or checked. Include relevant broker state when independent
composition is inadequate; do not weaken an obligation to preserve a small state count.
OR-015's common-writer migration blocker and immutable M1 contracts remain unchanged. CMP-015.
The M1A observation cannot supply M1C's original key, target and complete admission by inverting
digests; require an authenticated supplemental join. M1A's PAUSE-sensitive observation refusal
remains operative even though pure M1C trace classification can inspect retained evidence.
Distinguish semantic counterexamples from inadequate auxiliary state or checker limits; neither
an unproved mapping nor a timeout is a passing model. CMP-027.


## Approved proof boundary and comparative obligation — 2026-09-07

OR-036 accepts the smallest aggregate plus explicit independently checkable bounded broker
refinement. Investigate both that split and a broker-inclusive model sufficiently to compare
coverage and complexity on identical required behavior, bounds, fairness assumptions and fault
traces. Ease or speed alone cannot remove necessary execution behavior.

Before freeze, the model and refinement together must cover the six M1 obligations and
process ownership, consultation, queue saturation, RESET/replay, relaunch, configuration and
update/Stop interactions. Include relevant broker state directly whenever separate checking is
inadequate or a bounded interleaving violates an aggregate obligation. Retain counterexamples
and mapping obligations. Whole-system optimization proceeds alongside this work; probes still
precede model freeze, then language comparison, then storage comparison. OR-015 is unchanged.

OR-039 requires comparing complete architectures, component placement and dependency costs,
including rich/lean and disabled configurations. A rejected framework, topology or default must
be replaced with an approach preserving the accepted capability. See [Mark 3 research plan](foundation-research/mark2-research/mark3/research-plan.json).

## Mark 2 model and recovery mechanics — updated by OR-036

The approved proof boundary is the smallest aggregate plus an explicit bounded broker
refinement; a bounded broker in the first model remains a required technical comparison.
Neither option selects language/storage/frameworks or changes M1. Preserve the six existing
obligations: M1A-to-M1C effect identity projection; separate work/writer/process/PAUSE fence
namespaces; one canonical writer; current-immediate PAUSE; partition/termination ambiguity;
and bounded broker flow/replay with RESET. Add assistant/consultation flow, revision propagation,
coherent forks, update admission and cumulative recovery reservations as proposed obligations.
MKR-018 is the historical recommendation, now accepted with qualifications by OR-036; [current decisions](foundation-research/mark2-research/mark3/directions.json).

| Alternative | Reuse/adaptation benefit | Limit and removal criterion |
|---|---|---|
| Small aggregate + broker refinement | Reuse M1 vectors; keep OS/protocol details in bounded adapters | Reject the split if an assumption cannot be mapped to executable counterexamples or admits stale effects |
| Bounded broker in first model | Queue/relaunch/update races visible in one state space | Reject unnecessary state if equivalent refinement detects the same failures with less complexity |
| Durable workflow/virtual actor framework | Reuse timers, activation and continuation mechanisms | Must not replace canonical identity/writer/grants; remove if runtime coupling or idle cost buys no measured requirement |
| Cordis/SDK/plugin composition | Reuse lifecycle and rich harness controls | Preserve authority outside fail-open hooks and destructive upstream lifecycle operations |
| New canonical integrity layer | Own missing admission, evidence, accounting and projection semantics | Implement only after probes/model/language/store comparisons; do not duplicate useful upstream loops |

A2A, Orleans and Cordis supply distinct mechanisms, not a selected runtime
(SRC-V3-183/SRC-V3-196/SRC-V3-197). The common-writer migration blocker OR-015 is unchanged.
The comparison sequence remains small probes → formal model → CPython/Rust/Go → complete
JSONL/CAS versus SQLite-WAL/CAS store comparison.

## Six update modes and observable landing — proposed 2026-09-07

MKR-015/MKR-017 retain compatible rollback as a candidate recovery strategy while separating
four outcomes: working executable, readable history, preserved newest work and resumable tasks.

`current tasks` is the nonterminal set with admitted packets before the canonical update-request
sequence, including waiting work, leased descendants and external attempts. Queued requests
without admission are outside that snapshot. `everything` is all admitted work/processes/monitors
under the selected scope and release epoch, including descendants and enrolled remote work;
it is not every process on the machine. Minimum deterministic Owner-control and maintenance
services remain separately identified. Only an explicit update-mode maintenance grant permits
the updater to run after work admission closes; it is not a general worker or model exception.

`lands` requires verified artifact identity/policy, activation or boot, and a typed per-host
receipt binding ReleaseId, protocol/schema epoch, state/checkpoint version and host. Downloaded,
staged or installed-but-not-activated is pending. Required offline hosts remain unknown; aggregate
landing requires all required receipts unless a separately explicit policy defines a reduced set.

| Mode | Admission and transition | After landing |
|---|---|---|
| interrupt_save_update_resume | Close old-epoch admission; request bounded checkpoints; stage/activate compatible candidate | Resume only validated checkpoints; ambiguous effects stay held |
| interrupt_update_stop_tasks | Close admission; interrupt current tasks and retain all history/artifacts even without runnable checkpoints | Tasks stay stopped; no implicit resume |
| wait_current_tasks | Freeze new old-epoch admission; drain the fixed current-task set, holding later requests | Activate only when snapshot set is terminal; timeout is waiting/escalation, not success |
| auto_update_off | No automatic staging/activation; candidate metadata may be visible | Existing release continues |
| notify_escalate_owner | Record verified candidate and notification; do not interrupt or update automatically | Existing release continues pending Owner decision |
| stop_everything_wait_update | Stop/fence all scoped work and relaunchers; use only the explicit maintenance lane to stage/activate | Hold work until the configured resume policy permits it; offline Stop remains unknown |

No universal mode is selected. Configured automatic actions use existing authorization without
adding repeated Owner confirmations. Every mode preserves newer history; omission of “save” is
not permission to delete work.

Canonical event order resolves admission order; Stop fences new permits. A previously issued
permit may activate after local Stop until the recipient fence is installed; cancellation is not rollback.
Persist the operation identity, mark uncertain activation, reconcile the boot receipt and reject
returning workers with stale release/lease/config epochs. Full Stop includes owned relaunchers;
Lazy uses a visible bounded operation boundary. Current operational PAUSE remains unchanged.

| Failure | Truthful guarantee / hold condition | Compared recovery strategies |
|---|---|---|
| Candidate unavailable, foreign install owner or offline host | No overwrite/landing claim; record deferred or unknown | Wait, requested repair or separately authorized isolated instance |
| Bad signature/hash/expiry/version mix | Reject candidate; preserve verified running release and reason | New authenticated release; never disable rollback/freeze checks |
| Staging failure before activation | Old active release remains; incomplete candidate quarantined | Idempotent restage only after effect identity is reconciled |
| Activation/reboot crash | Active version unknown until boot receipt; tasks held | Retained compatible instance, repair-forward or isolation |
| Schema contraction or incompatible checkpoint | Starting old code does not prove readable/newest state | Expand/contract compatibility, repair-forward or separate instance |
| Post-activation regression | New events/artifacts remain retained in lineage | Compatible rollback only when old code can read current state; otherwise isolate/repair |
| Remote acceptance then timeout | M1C ambiguous effect; no duplicate/dependent dispatch | Reconcile same operation; compensation is a separate authorized effect |
| Optional component failure | Keep canonical recovery and Owner control usable | Disable/isolate component; do not claim full feature recovery |
| Missing retained executable or exhausted recovery budget | Explicit hold; no invented fallback or new funds | Wait/escalate under configured policy |

TUF 1.0.36 secures metadata selection, not activation or migration. An intentional older binary
must be delivered through an authorized valid release selection; bypassing rollback protection
is not the recovery design. systemd-sysupdate supplies atomic retained instances but can prune
them; AWS redeployment creates a new deployment of an old revision. OpenStack's schema design
illustrates why destructive contraction changes compatibility, by inference. Pilot recovery
research motivates testing recovery itself. None proves rollback of arbitrary tool effects.
SRC-V3-184 through SRC-V3-188.

## Relative recovery ledger — proposed 2026-09-07

For each task/goal scope and separately metered unit, record normal consumption `N` before the
shutdown boundary, configured exact fraction `p/q`, cumulative committed recovery `C` and
outstanding recovery reservations `R`. Use integer counters in a declared quantum (tokens,
micro-units of one allowance, or one currency's minor unit), not floating-point percentages.

`B = floor(p * N / q)`; a new credible upper-bound reservation `x` requires
`C + R + x <= B` and every parent/pool/resource ceiling. Charge actual usage once and release
only reconciled unused reservation. An ambiguous outcome retains its reservation. A provider
whose usage cannot be bounded before dispatch cannot satisfy a strict cap merely by reporting
an estimate afterward. MKR-016.

Repeated shutdowns, worker/model changes, daemon restart and provider allowance reset never reset
`C` or `R`. Additional verified normal work after resume may increase the next boundary's
cumulative `N`; recovery consumption itself does not enlarge the baseline. Zero prior use gives
zero automatic allowance. Unknown prior use/counter resets block new consumption reservations in that unit until
reconciled; they are not zero. Deterministic boot, replay, status and evidence preservation remain
available under their independent authority/resource limits without model consumption. Cross-goal borrowing needs an explicit transfer and shared pools
also enforce aggregate reservations. Combined caps are componentwise; no silent conversion among
tokens, quota and currencies.

The accepted illustrations remain 100,000 prior tokens at 10% → 10,000 extra tokens, and 10% of
one allowance at 10% → one additional percentage point of that same allowance. No universal
fraction or promise that provider counters expose this precision is selected. The local campaign
gate tests arithmetic fixtures only; [the update/recovery family](foundation-research/mark2-research/experiments.json)
preregisters actual counter and failure tests, all unrun.

## Mark 2 Owner direction — 2026-09-06

Persistent assistants spanning sessions/goals and configurable inter-assistant decision rights require an explicit mapping to Head, ARM, profiles and current GoalSpec amendment ownership. Compare the proposed identities, consultation/lifecycle, coherent forks, configuration propagation, Stop scopes and update/recovery accounting before formal-model freeze. OR-018/019 and immutable M1 remain unchanged; see the [Mark 2 contract obligations](foundation-research/mark2/handoff.md). No new manager tier, live process migration or universal memory-sharing default is selected.

## Platform execution requirement — 2026-09-06

OR-021/OR-022 require independently useful native and upstream execution in the first usable
release, plus combined workflows. A small native loop is required behind the existing typed
boundary; the privileged kernel remains small under OR-018. Reusing libraries is permitted.
An embedded upstream SDK that owns the agent loop is upstream execution in this taxonomy.

Every reasoning role resolves an explicit eligible execution/accounting path. The deterministic
runtime must boot, store, schedule, supervise processes, account and expose status without
continuous inference; an upstream-only workflow must not implicitly require a native planner,
reviewer, summarizer or recovery model. See the proposed
[ownership and interaction contract](foundation-research/platform-round/interaction-contract.md).
The current constitution and immutable M1 boundaries are unchanged.

## Foundation integration boundary — proposed 2026-09-05

OR-018/OR-019 remain accepted. Standalone recovery and a complete native sampling loop are
different commitments. Recursion owns canonical authority/recovery in both native and delegated
execution. An upstream engine may own sampling, native tools and session state through an optional
typed adapter. The [ownership matrix](foundation-research/compatibility.json) records each layer;
neither terminal attachment nor an SDK transfers canonical ownership. FND-004–FND-006.

Before formal-model freeze, make the M1A-to-M1C effect identity projection explicit; distinguish
work lease, writer fence, process/daemon and PAUSE/admission epochs; preserve one canonical writer,
current-immediate PAUSE and unresolved effects after partition/termination. Broker flow/replay is
either bounded model state or an explicit adapter obligation. See
`analysis.foundation.model` and `decision.foundation.model-boundary` in the
[foundation evidence](foundation-research/evidence.json) and [decisions](foundation-research/decisions.json).
These model-level mappings preserve immutable M1 contracts and the blocked OR-015 boundary.
A failed mapping requires a separately scoped repair proposal, not a silent schema change. FND-002/FND-003.

> **Ratified target, not operative authority.** `OR-018` and `OR-019` select the hierarchy and
> Recursion-owned Option 2 boundary below. `CONTROL_PLANE.md` records that the earlier
> Hermes-led repository control plane was retired on 2026-09-28; it remains the controlling
> authority document. The docs-only [amendment proposal](CONSTITUTIONAL-AMENDMENT-PROPOSAL.md)
> does not activate a replacement Head or transfer authority.

## Correct hierarchy

```text
Lucas — principal and final authority
  |
  v
Main Assistant / Recursion Head — portfolio executive
  |  model escalation, GoalSpecs, ARM lifecycle, monitoring, escalation
  |
  +--> ARM Executive — one approved parent goal
  |      implementation DAG, delegation, tactical replanning, checkpoints
  |        |
  |        +--> research / implementation / test / review / integration agents
  |                    |
  |                    +--> gates + independent verifier
  |
  +--> ARM Executive — another approved parent goal
  |
  v
Deterministic Recursion kernel
  authority | state | budgets | routing | leases | evidence | PAUSE | publication
  |
  v
Review candidate — Lucas reviews, merges, provisions, and deploys
```

The Recursion Head directly manages ARMs. An ARM is itself a bounded executive that carries an approved goal by delegating work. A worker implements one bounded task. A verifier independently evaluates evidence. The deterministic kernel validates and executes declared transitions but does not make semantic product decisions.

## Four planes

### Authority plane

Answers who may decide and how far authority extends. It contains owner identity, GoalSpecs, capability grants, budgets, amendments, approval receipts, project privacy policy, and publication authority. Child authority must be a subset of parent authority.

### Executive plane

Contains the durable logical actors: the Recursion-owned `Head` and `ArmRun` instances. Models are replaceable compute assigned to an actor. The actor's identity, goal, authority, and event history do not change merely because its model or provider changes.

### Execution plane

Contains native or external harness sessions, delegated agents, worktrees, nodes, sandbox scopes, gates, and integration. It consumes immutable work packets and returns normalized checkpoints, candidates, usage, and evidence.

### Evidence plane

Contains append-only events, receipts, exact source and candidate identifiers, gate results, independent verification, context artifact hashes, and publication provenance. Model narratives may explain evidence but cannot substitute for it.

## Interaction surfaces

The v3 product is a headless harness. The interactive CLI is its canonical local operator
surface, and Telegram is an optional interactive transport. Several terminal clients may attach
to Head, ARMs, workers, events or system state; closing a client does not own or end the
underlying process. The persistent daemon, canonical state and managed process broker provide
lifetime and recovery. The conceptual `#general` conversation belongs to the Recursion Head; a
dedicated topic may address an ARM directly. These are views over canonical actor/event state,
not additional supervisors or task registries. See
[TERMINAL-RUNTIME.md](TERMINAL-RUNTIME.md).

Shared multi-ARM conversations use a Head-routed room or an event-mediated workflow. A named ARM
may hold one bounded workflow-step coordination lease; Head retains overall goal, membership,
authority, conflicts, lifecycle and strategic escalation. A `supervisor` is a temporary role
held by Head or that named ARM inside a GoalSpec, never an additional AI ARM Manager. Transport
normalization and the bounded step lease now have requirements contracts; usefulness, conflict
resolution and delayed-message behavior remain experimental. See
[INTERACTION-SURFACES.md](INTERACTION-SURFACES.md).

## Core object model

| Object | Purpose |
|---|---|
| `OwnerIntent` | Exact owner directive plus scope, source, status, and superseding amendment |
| `GoalSpec` | Versioned execution contract: outcome, scope, non-goals, budget, privacy, routes, gates, falsifier |
| `ExecutiveProfile` | Reusable defaults for a Head or ARM without carrying active-goal state |
| `ArmRun` | Durable goal-scoped executive instance with lease, plan, policy, checkpoints, and status |
| `CoordinationLease` | Bounded Head-issued authority for one named ARM to coordinate one shared-room workflow step |
| `TaskNode` | Bounded unit in an ARM-owned DAG |
| `WorkPacket` | Immutable executable grant for a delegated worker or verifier |
| `Route` | Engine + provider + model + capacity pool + credential reference + host + sandbox |
| `CapacityPool` | Subscription, metered API, or local compute entitlement and observed availability |
| `PausePolicy` | Versioned `current-immediate` operative behavior plus separately labeled future/non-operative `checkpoint`, `interrupt`, `drain` or emergency behavior by effect class |
| `PermissionProfile` / `CapabilityGrant` | Configurable authority, including explicit maximum `sovereign` and optional `grant-admin` |
| `ActionAdmission` / `EffectReceipt` | Fenced before/after boundary for a typed effect, including explicit ambiguity and reconciliation |
| `GrantMutationReceipt` | Attributable before/after authority change with step-up and policy/epoch bindings |
| `ContextCapsule` | Model-independent, content-addressed handoff manifest |
| `SourceRecord` / `ResearchClaim` | Versioned source identity plus falsifiable claim, contradiction, applicability and revalidation edges |
| `ExperimentRecord` | Immutable workload, baseline/treatment, hard floors, measurements and result binding |
| `EvidenceBundle` | Exact source, diff, gates, observed behavior, and verifier receipt |
| `PublicationIntent` | Passed candidate proposed to the only approved publishing path |

The glossary normalizes names. Planning-object owners, fields, lifecycle and transitions are
canonical in [AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md); this table is only an architecture
projection. Schema field names remain provisional until M1A, and new documents must not create
competing owners or synonyms.

## One transition path

```text
authenticated command or model proposal
  -> schema validation
  -> authority and budget validation
  -> append durable event
  -> update transactionally derived projections
  -> enqueue an idempotent action
  -> deterministic actuator executes a fixed verb
  -> authenticated result/evidence event
  -> reconcile next state
```

The system should not maintain independent operational truths in Markdown, SQLite, JSONL, process tables, and chat sessions. Human-readable views and configuration may remain files, but active state needs one canonical event/projection boundary.

## Headless native harness and external integrations

Recursion v3 is its own harness at the orchestration and product-contract level. The
ratified construction architecture is a Recursion-owned deterministic contract/event kernel
with compatibility adapters around existing loops. Codex, Cursor, Claude Code, Gemini CLI,
OpenCode, Hermes, and future harnesses remain optional peer engines behind the same contracts.
A small Recursion-owned native agent turn loop is required for the first usable release under
OR-022. It need not precede the first bounded kernel prototype; its implementation and performance
must be measured independently from the upstream adapters.

Every engine adapter negotiates a versioned capability descriptor for thread/turn identities,
stream ordering, tool-intent and settlement boundaries, approval bridging, interrupt semantics,
process ownership, checkpoint/resume, native compaction, usage evidence and stale-notification
filtering. Unsupported invariants fail closed for that route. External engines may own provider
sampling, provider-specific wire/tool syntax and native retry/stream parsing; they do not own
Recursion actor/goal/task identity, authority, budgets, PAUSE epochs, process lifetime, evidence,
canonical events or publication.

The privileged kernel is intentionally smaller than the complete object catalog. Its ratified
implementation responsibilities are strict schema/canonicalization, authenticated command
admission, a pure reducer and event sequence, authority/budget/privacy/fence checks, deterministic
WorkPacket derivation, effect-intent/receipt classification, replay and publication gating.
Provider selection/sampling, context construction, plugin dependency resolution, source retrieval,
terminal rendering, telemetry and process implementation remain adapters that submit typed
proposals. The exact code-informed evidence and falsifiers are in
[FRONTIER-HARNESS-EVIDENCE.md](FRONTIER-HARNESS-EVIDENCE.md) and the second-tranche
[evidence packet](FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md).

Expansion beyond the required small native loop must earn its scope by a required capability or
measured quality, continuity, security or resource improvement. See the compared options and falsifiers in
[ARCHITECTURE-OPTIONS.md](ARCHITECTURE-OPTIONS.md).

“Optimized” remains an objective, not a measured property. Implementing Option 2 is conditional
on the typed adapter contract and on a complete writer/migration boundary; a generic wrapper around
arbitrary opaque CLIs would not satisfy it. Matched neutral-API and minimal-native-loop spikes must show
a declared quality, continuity, security or resource benefit before the native loop expands.

## Scientific research capability

A research ARM is the same goal-scoped executive protocol applied to source discovery, claim
reconciliation and experiment design. Scholarly search, standards/source retrieval, PDF parsing,
citation checks and notebook execution are plugins; the kernel retains generic claim, source,
artifact, provenance, privacy and verification contracts. Research conclusions distinguish
external mechanism evidence, local structural applicability and empirical Recursion fit. See
[SCIENTIFIC-RESEARCH.md](SCIENTIFIC-RESEARCH.md).

## V2 inheritance

At `98113d6`, v2 already provides valuable substrate:

- immutable bounded task packets;
- explicit engine/provider/model/pool/host/sandbox separation;
- deterministic placement and rejection traces;
- durable leases, retries, restart recovery, and idempotency;
- aggregate child budgets;
- candidate/source/scope binding;
- independent authenticated verification;
- review intent separated from publication;
- provider egress and remote-execution contracts;
- PAUSE and protected publication boundaries.

V3 must preserve those invariants or deliberately supersede them with stronger tested equivalents.
The source trace verified that the MVP orchestrator has no non-test runtime caller in the
baseline tree, its supervisor schema fixes the executive identity to Hermes, and it does not
model an ARM as a first-class goal-scoped executive. Deployment state remains unknown. The
full trace and migration matrix are in
[CURRENT-AND-MIGRATION.md](CURRENT-AND-MIGRATION.md).

## Migration posture

The destination may be a re-foundation; construction should still use a strangler sequence:

1. Specify v3 contracts and prove adapters against fixtures.
2. Introduce the canonical event/state boundary alongside read-only projections of v2 state.
3. After Lucas ratifies ARM identity and Project Pilot compatibility semantics, represent the
   approved subset as first-class ARM runs without changing publication authority.
4. Rehearse the synthetic local execution-attempt/lease aggregate with no external effects,
   exact replay, complete writer fencing, crash recovery and rollback.
5. Compare results and rollback behavior against v2.
6. Move additional transitions only when their equivalence or improvement is proven.
7. Delete duplicated paths only after replay and migration gates pass.

The 2026-09-03 writer audit blocks step 4 at the broad aggregate boundary: task lifecycle,
assignment, run/evidence, accepted-result and fabric stores do not share a common fence epoch.
M1A uses planning-only in-memory fixtures and no fabric store. A later M1C may use an isolated
fabric-only synthetic requirement fixture, but that would be neither a cutover nor completion of
the owner-selected rehearsal. See
[ARCHITECTURE-PROOF.md](ARCHITECTURE-PROOF.md).

## Coordinator fit

The historical target is a 2-vCPU/4-GB-class coordinator; its current deployment was not
measured in this campaign. V3 is expected to fit only if it uses:

- one daemon per authority domain/OS account;
- event-driven scheduling instead of busy polling;
- one serialized canonical admission path; SQLite WAL is only an optional bounded metadata/
  projection candidate after a JSONL-plus-artifact comparison;
- content-addressed artifacts outside hot database pages;
- lazy plugin loading and isolated short-lived workers;
- adaptive admission with reserved OS/control-plane headroom;
- remote placement for heavy builds and model work;
- summaries and manifests rather than copying full transcripts between actors.

It does not fit if the coordinator becomes a universal build host, runs many resident CLI
trees, eagerly indexes every repository, or loads every plugin into one process. These are
hypotheses with workloads and failure rules in [PERFORMANCE-PLAN.md](PERFORMANCE-PLAN.md),
not measured conclusions.

No language is selected. CPython, Rust and Go are equal-entry post-M1A comparators on the same
reducer/replay/fault workload. JSON/JSONL is the inspectable initial local
protocol encoding; binary framing, SQLite and proposed native-loop optimizations must each
demonstrate a non-dominated improvement without a hard-floor regression before adoption.

## Constitutional reconciliation required

Lucas selected provider-neutral planning inside an explicit approved
model/provider/account/capacity-pool envelope. The current constitution still names ChatGPT via
Codex OAuth, so the product ruling requires a separately reviewed constitutional amendment
before private deployment configuration changes. OR-016 establishes the product principle that
the Recursion-owned `Head` remains the accountable logical actor, models and external harnesses
are authorized replaceable compute, deterministic code actuates, and Lucas retains authority.
Current Hermes-specific operative wording remains in force until the separate constitutional
amendment is applied. The exact proposed clauses and migration seam are recorded in
[CONSTITUTIONAL-AMENDMENT-PROPOSAL.md](CONSTITUTIONAL-AMENDMENT-PROPOSAL.md).

The proposed future `checkpoint` PAUSE default and a maximum `sovereign` profile capable of receiving
control-plane, credential, spending, publication, deployment and `grant-admin` authority
also differ from current constitutional defaults. V3 research must produce exact amendment
choices and preserve explicit receipts/audit boundaries; these docs do not authorize a live
policy or configuration change. See [DECISION-REGISTER.md](DECISION-REGISTER.md).

The first construction candidate is now the narrower planning-only
[M1A](M1A-AUTONOMOUS-PLANNING.md). It excludes SQLite, providers, terminals, plugins, Git,
publication and effects; the broader object table above remains a later architecture projection.

## Conditional flow and recovery composition

FRI-001 extends the earlier finite subsystem without replacing its retained counterexample.
The queue-independent specification, component split (kernel/source/broker), and flat inclusive
model now contain four candidate identities, mixed payload costs 1/2/1/2 bytes, four grants,
three single-slot pipeline stages, two total pipeline slots, and one modeled retention loss.
Both event and cumulative UTF-8-byte cursor profiles close at 97,500 split/audit states and
603,827 transitions each. Every reachable transition satisfies the independent specification
and exact local labeled correspondence to the separately defined inclusive model. This method
requires unique transition labels and complete visible/physical signatures; it is stronger than
the trace inclusion needed here and may cease to apply to future hidden nondeterminism.

Retain the small aggregate plus independently checked bounded broker as the leading candidate.
The inclusive model remains a required competing boundary, not an implementation cost measurement.
Shared domain and wire encoding, finite identity/grant bounds, atomic admission/emission, direct
logical Stop and modeled store facts limit this evidence. The original six obligations and
27-row comparison contract remain authoritative; full freeze is false. Next discriminators are
independent source evidence, physical control transport, and broader fault-recovery products.
See [packet evidence](construction/flow-recovery-integration-20260907/evidence.json).

## Local replay-store evidence, 2026-09-07

RSE-002 adds observed support for a staged-generation construction candidate: write and verify an isolated generation, publish one selected-generation pointer, and keep the preceding generation fenced from current replay but available for recovery analysis. Across the five named process cuts, the tested staged layout retains old coherent replay before publication; the tested in-place snapshot/prune layout blocks. This compares these two concrete mechanisms, not every possible in-place transaction or product store. A transactional in-place design remains a competitor for later storage comparison.

The [campaign dossier](construction/replay-store-evidence-20260907/dossier.md) binds source, host, raw observations and limits. Controller-pinned hashes are a synthetic trust anchor. The construction still needs a production authority for generation selection, concurrent-reader and publisher-death handling, and platform-specific durability evidence. No language/store/framework is selected; the full formal-model freeze and common-writer migration blocker remain open.

## Generation authority construction evidence, 2026-09-07

GRA-002 adds a locally observed candidate boundary: one deterministic authority owns selection, writer permit, durable reader pins and reclamation intent in a single envelope. A stable offset-zero exclusive lock excludes another cooperating authority in the tested Windows/NTFS schedules; a different-offset control does not. A boot change revokes publisher permits while preserving reader rights. A same-boot revocation needs a distinct permit serial and unique staging directory. Adapt these relations into the formal comparison; do not select this JSON/Python storage mechanism as the product architecture.

The trusted fixture controller mediates IPC, reader launch and owned-handle death. The authority trusts caller release reasons; this does not establish production caller authentication, protection from malicious filesystem writers or power-loss durability. Common-writer migration remains blocked. [Dossier and next construction requests](construction/generation-authority-20260907/dossier.md).

## Cross-arm architecture challenge, 2026-09-07

GRA-004 records 75 finite executable requirement examples across nine discriminator groups, including explicit recipient fence installation. The selected shortcuts produce counterexamples; guarded positive outcomes are consistency checks in the declared abstraction, not independent proof of implementations or a ranking of complete architectures. The full formal coverage ledger remains unchanged.

The [four-packet architecture challenge](construction/generation-authority-20260907/architecture-challenge.md) retains demand-activated services, a resident specialist hub and durable coordination as complete competitors. Refine activation receipts to bind the exact dependency closure; separate durable ingress/rejection, current decision adoption and external settlement. A persistent actor's display identity must not replace execution incarnation or meter epoch. Historical evidence/branch identity, applied dependency counts, transform identity and current disclosure authority are separate facts; runtime cache identity is another binding. These are candidate contract refinements requiring composition tests, not framework selections. Preserve retained logical forks without assuming every fork keeps a live worker or index generation indefinitely.

## Generation and effect composition, 2026-09-08

GCM-001 adds a conditional generation/effect composition result. Independent split and inclusive reducers each close 36,552 capsule-policy states/447,844 transitions and 11,008 resident-policy states/129,696 transitions, with zero frontier. Each edge satisfies an independent aggregate relation and a separate staging/deletion relation. Twenty-one guard mutants per boundary and four ordered recovery traces challenge the result. These graphs are a new three-generation subsystem, not the full original core/cross-area profiles or a composition with the earlier flow graph. No full formal freeze or common-writer migration follows. [Specification, evidence and residual coverage](construction/composition-model-20260908/handoff.md).

Recursion owns the common authority, continuity and accounting semantics and their eventual implementation. Studying an upstream mechanism does not require a fork or product dependency. Keep supported execution adapters and optional algorithms replaceable behind those contracts; select reuse, adaptation or new implementation on the same requirement and measurement evidence. The current experimental Python reducers select no product language, store or framework.

## Literal evidence and completion boundary, 2026-09-08

EEJ-003 adds a necessary distinction between historical effect classification and outstanding execution. In a literal M1C fixture, start1 → accepted1 → receipt1 → start2 reports `succeeded` even though attempt2 has no settlement. The new restricted adapter preserves that result and independently reports attempt2 with a settlement hold. Keep original classification, per-attempt settlement and permission to act as separate contract fields. An aggregate success label must not release another attempt's hold or authorize retry.

Prefer an evidence-cut adapter over direct label translation. A separately authenticated original dispatch, ordered inventory and historical prefix permit literal classification without synthesizing receipts. Individual record authentication cannot establish inventory completeness. Recursion owns this adapter contract; the experimental HMAC mechanism is replaceable and selects no product cryptography, language or store. Authoritative cut ownership and canonical quarantine resolution remain construction gaps. [Method, results and continuation](construction/effect-evidence-20260908/handoff.md).

## Shared generation, evidence and recovery boundary — 2026-09-08

SCM-001 retains the authority aggregate plus explicit broker refinement as the
modular candidate, with the inclusive record as a coverage reference and fallback.
The new finite model pairs the complete authority/broker state with the inclusive
record and checks independent postconditions on authority and broker-only edges.
Both representations close the same15 declared environments, including a free
shared core. This does not establish a smaller state space, runtime speed advantage
or unrestricted composition theorem. No language, store or framework is selected.

Authority owns publication, writer term/serial, admission, evidence inventory and
lifetime accounting. Broker ownership covers undelivered source entries, reader
pins, copied snapshots, gap/credit and installed presentation. Cross-component
reclamation predicates are required contracts. A term-only ticket is inadequate
for same-term replacement; the complete prior runtime issuer/owner/nonce bindings
remain required beyond the modeled term/serial tuple.

[Shared evidence and limitations](construction/shared-composition-20260908/evidence.json)
and [construction requests](construction/shared-composition-20260908/next-probes.md)
advance formal comparison. Full original profiles, authoritative shared pool and
wire mapping remain open; the common-writer migration blocker is unchanged.

## Shared-pool admission construction — 2026-09-08

SWR-001 preserves the original two goals requesting six each from ten. The central
construction serializes current-balance check and durable reservation across goals;
removing that guard admits twelve. Both split and inclusive representations close
the same declared graphs under a third complete relation. This is scoped finite
evidence, not closure of the original full core/cross configurations.

SWR-003 constructs a two-holder comparator with one durable one-unit transfer:
source debit precedes destination credit with a retained applied marker. Duplicate
credit, lost-ACK restart, timeout and stale snapshot restoration cannot manufacture
rights in the guarded relation. Free+in-flight+reserved+consumed stays ten. Initial
rights(5,5) deny six while central admission serves it; safety correspondence does
not imply equal availability. Keep central admission as mandatory baseline and
rights as optional under explicit persistent-memory/serialized-holder premises.

No product store/language/framework, physical durability, arbitrary transfer, full
formal freeze or common-writer migration is established. [Evidence and limits](construction/shared-reservations-20260908/findings.md).

## SC-WIRE-02 — 2026-09-08

WIR-003 adds scoped literal recovery correspondence with independent split/inclusive reducers, a complete separate relation and all distinct wire histories in16 finite configurations. Preserve synthetic versus observed support. Full shared-reservation/wire composition, original core/cross comparison, authenticated snapshots and runtime integration remain unproved.
[Evidence and limits](construction/wire-correspondence-20260908/evidence.json).

## SC-EPOCH-03 — 2026-09-08

EPC-003 checks16 typed-consultation environments, including an unordered12-action core, in independent split/inclusive representations with a third complete relation. Independent review found and repaired a shared alias-generation flaw that graph equality had missed. Preserve structural namespace checks versus actual workflow and literal M1A evidence. The coupled reservation/wire/epoch product, full original comparison and common-writer migration remain incomplete.
[Evidence and residuals](construction/typed-consultation-20260908/evidence.json).

## SC-COUPLED-01 — 2026-09-08

CPL-001 closes the declared central reservation/transport/typed-consultation product across100 finite environments,74,332 states and279,976 edges per representation, with a third complete relation. The original two six-unit requests against ten remain actual admission. This scoped result does not close the original full Cartesian comparison, general rights coupling or common-writer migration.
[Evidence and limits](construction/coupled-construction-20260908/handoff.md).

## Coupled evidence cut and PAUSE — 2026-09-08

CCP-001 preserves actual original-cut and typed attempt mapping under independent complete comparison. Valid signatures cannot substitute for complete inventory or current cut ownership.
See [scoped findings and residuals](construction/coupled-cut-pause-20260908/findings.md). No runtime integration or full formal freeze follows.

## Retained-cut publication — 2026-09-08

CPB-001: Fresh retained-cut publication checks current publisher boot/serial, exact parent,
full cut content/inventory and original intent/admission/reservation against actual admitted
A ownership and current source head/incarnation. Equal record heads do not establish equal
scope or cut identity. Durable exact retries return old receipts without selecting old state.
Synthetic source truth and atomic durability remain premises; this is not common-writer migration.

See [SC-CUT-PUB-01](construction/cut-publication-20260908/handoff.md) for evidence and limits.

## Complete-source admission — 2026-09-08

SC-CUT-SOURCE-01 constructs registration/append revision, exact ordinal collection and
current source admission in two representations with a third complete relation. A new
pending attempt changes authority even when the record head is unchanged. Fresh versus
retained duplicate publication depends on durable request identity, not call spelling.
Atomic registration/start and pending registration are bounded alternatives, not product
architecture selection. Physical source truth, recovery and common-writer migration remain
premises or residuals. See [evidence](construction/cut-source-overnight-20260908/findings.md).
