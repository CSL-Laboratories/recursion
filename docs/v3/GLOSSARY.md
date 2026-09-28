---
title: Recursion v3 normalized glossary and ownership model
status: proposed-canonical-vocabulary
last-updated: 2026-09-03
---

# Recursion v3 normalized glossary and ownership model

These names remove the main ambiguities discovered in the source and design transcript. A
schema milestone may refine field names, but it should not create a second synonym set.

For planning objects this is a naming index; canonical fields, lifecycle, authority and replay
semantics live in [AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md).

## Actors and authority

| Term | Definition | Not the same as |
|---|---|---|
| `Owner` | Lucas, the principal and final authority | a model, administrator role or route account |
| `Head` / `Recursion Head` | Recursion-owned durable portfolio executive directly accountable to the Owner; `Main Assistant` is its UI role name | Hermes or another engine, one provider session, or the deterministic kernel |
| `Hermes adapter` | Optional version-pinned migration or execution-engine integration behind Recursion contracts | the canonical Head, Owner authority or permanent product base |
| `ARM executive` | Durable goal-scoped executive that plans and delegates inside one approved `GoalSpec` | a worker, Project Pilot script or AI ARM Manager |
| `Delegated worker` | Bounded executor of one `WorkPacket` | an executive with authority to redefine the goal |
| `Verifier` | Separately assigned evaluator of exact candidate/gate evidence | the implementer, vendor self-report or publisher |
| `Kernel` | Deterministic authority, event and action enforcement substrate | a semantic manager or planning agent |
| `Principal` | Identity whose signed grant is being exercised | whichever process happens to hold a session |
| `CapabilityGrant` | Versioned subset of parent authority | a tool merely being installed or discoverable |
| `grant-admin` | Explicit maximum-authority capability permitting receipt-backed mutation of the holder's grants within installation bounds | implicit self-escalation or permission to rewrite historical audit evidence |
| `PermissionProfile` | Named schema-validated bundle of capability defaults; `sovereign` is the explicit maximum profile | an edition manifest or automatic consequence of installing `full` |
| `VerifierPolicy` | Post-amendment risk-class mapping `V0-mechanical`/`V1-routine-reversible`/`V2-sensitive`/`V3-constitutional-release` to required deterministic, session, model/family/provider/host and owner-exception evidence | a missing verifier being treated as passed, or same-family review being called cross-family |
| `ActorEndpoint` | Addressable CLI/Telegram view of Head or an ARM | actor identity, authority, lease or canonical state |
| `TerminalView` | Detachable interactive client over actor/event/process state | the process owner or a durable executive |
| `ProcessBroker` | Platform adapter that owns and reconciles PTY/process groups for bounded work | the model, terminal window or canonical state writer |
| `PauseDirective` / `PausePolicy` | Epoch-bearing PAUSE command plus versioned behavior; `current-immediate` is operative, while checkpoint/interrupt/drain/emergency profiles are Owner-authorized but non-operative until the exact amendment is applied | a model choosing whether to honor PAUSE, or schema presence selecting future behavior |

The Recursion Head directly owns ARM lifecycle. An ARM directly owns its task DAG and delegates
workers. The kernel owns neither portfolio priorities nor ARM plans.

## Durable product objects

| Object | Canonical owner | Meaning |
|---|---|---|
| `OwnerIntent` | Owner/authority plane | Exact non-lossy directive plus amendments and source |
| `GoalSpec` | Owner; Head compiles drafts | Immutable versioned goal, scope, non-goals, budget, privacy, gates and stop rules; only Owner acceptance/amendment makes it authoritative |
| `GoalSpecAmendment` | Owner proposes/accepts; kernel admits | New GoalSpec version plus exact old-state invalidation, retention, quarantine, supersession and fence map |
| `ExecutiveProfile` | reviewed configuration | Reusable defaults; never active state or authority by itself |
| `ArmRun` | Head creates; ARM advances | One leased execution of a `GoalSpec`, retaining identity across model/session swaps |
| `PlanRevision` | ARM proposes; kernel admits | Immutable version of the ARM-owned task DAG, bounds, source, gates and complete predecessor disposition |
| `PlanTransition` | ARM proposes; kernel admits | Atomic from/to revision command binding trigger evidence, current lease/fence and every affected item |
| `CoordinationLease` | Head issues; named ARM may hold | Bounded, expiring authority to coordinate one shared-room workflow step without owning the overall goal, membership, conflicts or ARM lifecycle |
| `TaskNode` | ARM | DAG node describing bounded work and dependencies |
| `WorkPacket` | kernel issues from approved task | Immutable executable authority for a worker or verifier |
| `GateResult` | assigned gate/verifier through kernel | Exact pass/fail/error/missing/ambiguous result bound to candidate, source, packet, procedure and evidence digests |
| `RetryDecision` | ARM proposes; kernel admits | Bounded retry request with typed material novelty; non-novel repetition refuses before another model turn |
| `ReplanDecision` | ARM proposes; Head handles cross-ARM/goal seams; kernel admits | Request for a new immutable PlanRevision and complete old-item disposition |
| `Blocker` | discovering actor proposes; kernel binds | Typed unresolved condition with evidence, affected scope, required authority and allowed recovery |
| `Escalation` | ARM to Head or Head to Owner | Exact blocker/decision packet that freezes affected scope and cannot grant authority itself |
| `PlanVerifierDecision` | assigned plan verifier | Exact-plan `pass/downgraded-pass/fail/blocked` judgment with assurance grade, independence vector, dissent and limitations; a policy-bound Owner exception may yield only `terminal-verified-downgraded`, never full verification, and cannot rewrite the plan |
| `CommandEnvelope` / `CommandReceipt` | authenticated caller / kernel | Compare-and-swap request and accepted/refused/duplicate/conflict result |
| `PlanningEvent` | kernel | Append-only sequenced transition record used for canonical replay |
| `ExecutiveLease` / `WorkLease` | kernel grants | Scoped epoch-bearing proposal/execution authority; replacement fences stale holders |
| `Route` | deterministic resolver | Complete engine/provider/model/account/pool/host/sandbox selection plus explanation |
| `RouteLease` | kernel/capacity pool | Time- and quota-bounded reservation; not entitlement ownership |
| `ContextCapsule` | context compiler | Content-addressed provider-neutral manifest; payload hydration remains policy-scoped |
| `Checkpoint` | kernel records; engine produces observations | Mechanically safe boundary with source, state and evidence bindings |
| `EvidenceBundle` | evidence plane | Candidate, source, gate, usage and independent-verifier bindings |
| `PublicationIntent` | Head/ARM may propose | Request for the sole publisher to create a review artifact |
| `PublicationReceipt` | publisher actuator | Durable result of the reviewed publication path; never a merge/deploy receipt |
| `ActionAdmission` | kernel | Fenced pre-effect authorization binding command, GoalSpec/grant/route/policy digests, epochs, effect class and idempotency identity |
| `EffectReceipt` | actuator/reconciler through kernel | Authenticated observed outcome or explicit ambiguity; never proof that an unseen remote effect did not occur |
| `GrantMutationReceipt` | kernel | Exact before/after grant digests, mutator, authorization, step-up, reason, epochs and result for one grant change |
| `Node` | installation trust domain | Registered execution host with identity, roles and capability evidence |
| `PluginManifest` | reviewed configuration | Data declaration of capability, dependencies, privileges, lifecycle and version |
| `ResearchClaim` | research/evidence plane | Exact falsifiable statement linked to sources, contradictions, experiments and confidence |
| `SourceRecord` | research/evidence plane | Versioned paper, standard, dataset, documentation or source revision with provenance and access policy |
| `ExperimentRecord` | evidence plane | Immutable experiment/workload identity, baseline/treatment, hard floors, measurements, exclusions, result and revalidation binding |
| `ReleaseId` | release builder/kernel | Product SemVer plus protocol, schema epoch, constitution-policy, core artifact/dependency-lock, provenance, SBOM and edition-manifest identities; SemVer alone is insufficient |
| `BootReceipt` | kernel | Exact ReleaseId/state/checkpoint/replay binding and status of every optional capability as absent, disabled, incompatible, unverified, quarantined or active |

## Route vocabulary

| Term | Definition |
|---|---|
| `Engine` | Harness or execution protocol, such as a native loop or external CLI |
| `Provider` | Service that serves a model or remote execution API |
| `Model` | Named model capability selected through a provider |
| `Account` | Credential/entitlement identity; durable records contain only an opaque reference |
| `CapacityPool` | Bounded subscription, metered, local or remote capacity with billing and fallback policy |
| `Host` | Machine on which the engine/process executes |
| `SandboxProfile` | Filesystem, process, network and resource boundary applied to execution |
| `ProviderSession` | Disposable route-local continuity/cache affinity handle. Zero or more map to exactly one `RecursionSession`; never canonical actor identity |
| `RouteCapabilityDescriptor` | Dated evidence about supported context, tools, controls, billing/privacy unknowns and expiry |

A model listing does not create a route. A route exists only when all required objects resolve
inside current authority. Subscription exhaustion never turns into metered usage implicitly.

## Event and effect ownership

| Category | Examples | Persistence/ordering | Owner |
|---|---|---|---|
| Authority event | goal approved, grant narrowed, pause epoch advanced | durable, globally ordered within authority domain | kernel after authenticated command |
| Executive event | ARM created, plan amended, task proposed, escalation raised | durable per aggregate | Head or ARM proposes; kernel validates/appends |
| Execution event | lease acquired, dispatch admitted, checkpoint reached, result observed | durable and idempotency-bound | kernel plus normalized engine observation |
| Evidence event | gate result, verifier receipt, candidate binding | durable, append-only | gate/verifier identity through kernel |
| Publication event | review intent admitted, review ref published | durable, separate actuator | kernel/publisher |
| Ephemeral signal | token delta, progress frame, sampled CPU, UI cursor | bounded/loss-tolerant unless promoted | engine/observer |
| External effect | model request, process launch, Git ref update, notification | action record before effect; receipt/reconciliation after | one deterministic actuator |

Ephemeral data becomes durable only through an explicit typed promotion. Model prose and
provider self-reports are observations until a trusted gate or reconciler binds them.

## State ownership rule

One aggregate has one writable authority at a time. JSONL/JSON Sequence plus CAS and SQLite WAL
metadata plus the same CAS remain competing future stores; Git/Markdown/JSON views
remain configuration, evidence exports or compatibility projections. None may silently become
a second runtime writer. See [CURRENT-AND-MIGRATION.md](CURRENT-AND-MIGRATION.md).

## Recursion-native session and endpoint kinds - 2026-09-28

| Object | Definition |
|---|---|
| `RecursionSession` / `SessionId` | Kernel-minted, project-scoped canonical session. Spans turns, route/provider epochs, engine switches, detach/resume and compaction transitions; bound to one logical actor and `GoalSpec`. Zero or more `ProviderSession`s map to exactly one. A projection over the canonical event ledger plus the artifact index - not a second writer. |
| `TurnStream` / `StreamEvent` | The canonical model-output stream for one turn, emitted by Recursion in both native and upstream lanes. Adapters normalize upstream deltas into `StreamEvent`s; they never own cursors or credits. |
| `ViewSubscription` | A view's attachment to the canonical stream (attach/detach); one canonical append fans out to N views. |
| `EndpointKind` | `local` / `byok` / `subscription` / `upstream-harness` - orthogonal to `BillingBasis` and to `Engine`/`Provider`. |
