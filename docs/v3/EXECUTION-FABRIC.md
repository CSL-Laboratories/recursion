---
title: Recursion v3 execution fabric hypothesis
status: hypothesis
last-updated: 2026-09-07
---

# Recursion v3 execution fabric hypothesis

## Deferred candidates and recovery commands â€” 2026-09-07

RSM-001: the [RESET mapping packet](construction/reset-mapping-20260907/handoff.md) replaces
forced recovery after a locally suppressed candidate with deferred admission in its new narrow
model. Keep the candidate identity and payload until both cumulative credit and pending-queue
capacity permit emission. ACK releases queue capacity; CREDIT replenishes the separate allowance.
A candidate that never reached the wire does not itself create wire exhaustion or justify RESET.
The immutable oracle accepts both ACK-then-CREDIT and CREDIT-then-ACK continuation in the controls.

The prior c87 model and FMC-004 counterexample remain valid historical evidence of the rejected
mapping. The new finite mapper does not retroactively repair those models or prove full transport
refinement. Actual emitted exhaustion still requires RESET, and its rejected EVENT remains in
audit evidence. Candidate, admission, emission and audit facts remain separate.

RSM-003: unchanged M1B replay_broker returns requires_reset from historical recovery_needed_seen.
Consequently accepted-reset may still carry requires_reset=true. Do not turn that flag alone into
another RESET command: the recovered trace would refuse an unexpected RESET. A runtime adapter
needs separately established current recovery state; changing M1B is outside this packet.

## Preventive admission versus retained wire evidence â€” 2026-09-07

FMC-004: the paired immutable-oracle controls
expose an unresolved adapter seam. A preventive model can withhold an exhausted second EVENT,
retain admitted cursor 1, and request RESET. If the actual wire omits that EVENT, the M1B audit has
no observed recovery cause and refuses RESET as unexpected. If the rejected EVENT is included,
the audit cursor advances to 2. Deleting the candidate therefore does not establish a correct
mapping between admitted state and retained wire evidence.

Disposition: maintain distinct candidate/admission, emitted-message and audit facts. Before a
runtime adapter is admitted, specify how its supported resynchronization sequence creates a
valid recovery cause and snapshot cursor while preserving every rejected/unknown observation.
Check that sequence against unchanged M1B rules and both models. Do not invent a wire field,
forge a gap, erase a rejected event, or weaken the immutable validator. M1A/M1C joining, PAUSE
classification and OR-015's common-writer migration remain separately unresolved.

## Observed accounting boundaries â€” 2026-09-07

IPR-003: the installed Codex fixture emitted cumulative totals of 15, 30, 45, 60 and 75,
then a fork inherited 30 and the restarted original session replayed 75 without another
provider POST. These values were scripted. Their lineage and replay behavior demonstrates why
summing notifications or parent-plus-fork totals would duplicate usage. Bind each observation
to the canonical parent, upstream request, session lineage, meter segment and reset boundary.

IPR-006: the Hermes real-agent fixture made two scripted chat POST attempts, while its
post-interrupt view reported only one completed usage call and the first response's 18 tokens.
The second stream supplied no final usage. Retain an attempted-request record and an unknown
remainder; interruption cannot release the corresponding allowance by interpreting missing
counters as zero. These observations establish protocol behavior, not real subscription debit.
See [hash-bound receipts and applicability](construction/interface-probes-20260907/evidence.json).

Kernel admission, broker ownership and recipient settlement remain separate. A successful
harness interrupt, zero local owned processes or a restored transcript supplies one observation,
not an external-effect settlement receipt. Recovery allowance remains cumulative across all
of these boundaries, including failed attempts, helpers and verification.

## Mark 3 comparison: dispatch and ownership receipts - 2026-09-07

Each proposed dispatch envelope binds canonical operation/attempt, GoalSpec and packet digests,
logical actor/role lease, route/session generation, effective configuration revision, current
authority fence, parent reservation, host and owned process scope. These are adapter mapping
requirements; the immutable M1 schemas are not extended by this prose. See the
[formal mapping specification](foundation-research/mark2-research/mark3-comparison/formal-boundaries.md).

The broker owns local execution resources; the kernel owns admission and authoritative
transitions. A renderer, provider session ID or multiplexer pane cannot become canonical process
identity. Scope creation/association must precede execution where the OS supports it, and
association failure blocks dispatch. Windows Job membership and Linux delegated cgroup ownership
are explicit admissions. A systemd stop operation differs from killing a PID; all independent
relaunchers still need a current dispatch fence. CMP-002, CMP-003, CMP-004.

Cancellation records requested mode and scope, admission fence, delivery, queue disposition,
observed process termination and unresolved external effects separately. An offline node remains
unknown. A stale returning process can supply evidence for reconciliation but cannot publish,
reacquire authority or renew a reservation. Reattachment does not relaunch work. Ordinary status,
lease expiry, timer delivery, Stop and reconciliation work without continuous model inference.

Record recipient-installed fence and in-flight launch permits separately from canonical Stop
acceptance. A disconnected recipient can retain earlier permits until its fence is installed;
report that exposure. Once installed, earlier permits must fail. Queue acceptance, cancellation
delivery, owned termination and external settlement are distinct observations. CMP-024.

For a recipient that supports it, compare a queryable operation record committed atomically
with its effect against workflow-assisted recovery. Stable identity, retry rendezvous after
relocation and safe record retirement are required. Without that recipient boundary preserve
unknown and prohibit blind replay. The canonical outbox alone supplies no exactly-once claim.
CMP-023. Both journal and receiver-record alternatives prohibit retransmitting an ambiguous
effect before authenticated same-operation resolution; a replay cache only supports query and
reconciliation, under the immutable M1C ambiguity guard.
Serialize shared-pool reservations across goals; per-goal writers cannot each reserve
the same remaining balance. Conserved offline rights are a separately proved comparator, not
permission to reclaim unknown use after timeout. CMP-026.

Persist meter-segment identity, reset boundary, observed lower bound, outstanding reservation
and unattributed remainder. Claude reset commands can reset totals and maxBudgetUsd; Grok totals
reset across process resume; Cursor billing can lag and aggregate usage exceed attributed turns.
Zero charged dollars or missing final totals never mean zero consumption. Resume, fork, provider
reset and session-ID rotation do not renew parent or cumulative recovery allowance. CMP-033.

The [route overlay](foundation-research/mark2-research/mark3-comparison/route-deltas.json) preserves
all 13 integrations and 23 paths. Its role inventory includes executives, workers, reviewers,
questionnaire assistants, consultants, summarizers, compactors, recovery, classifiers, title and
token helpers, and MCP sampling. Each role has native, upstream-only and combined paths with
explicit eligibility/accounting; design coverage is not proof of an available account or build.
No upstream-only helper may silently require a direct API key. Closure includes conditional
next-speaker/loop checks, model repair, fallback credentials and retry owners, not only the
configured primary model. Unknown internal helper closure is an adapter blocker; a separately
admitted eligible upstream session remains the functional alternative. CMP-011, CMP-017, CMP-032.


## Mark 3 communication and authority projection â€” 2026-09-07

OR-036/037 settle the proof-boundary and amendment choices while retaining current integrity
contracts. OR-038 requires direct assistant/Head consultation across compatible sessions and
configured nodes with one coherent goal/version, authority provenance and aggregate ledger.
A proposed message envelope binds request/causation, logical actors and role leases, decision
class, permitted context revision, route/model/harness/session/node, parent reservation and
delivery/adoption disposition. This is a proposed realization, not an M1 wire-schema change.

Transport, state, replay and visible status can be deterministic. Only admitted semantic
reasoning invokes models. Count consultation helpers and retries once with parent lineage;
an upstream-only workflow must not discover a hidden API dependency. Bound cycles, depth and
queues, and reject stale or revoked advice without deleting its evidence.

Model comparison must use identical queue/relaunch/RESET, lease/revocation, configuration/fork,
consultation, update/Stop, partition/reconnect and ambiguous-effect traces. Neither direct
messaging nor richer working relationships changes who can amend a GoalSpec or write canonical state.

## Role execution and accounting closure â€” proposed 2026-09-07

MKR-002/MKR-008 extend the existing Route/WorkPacket projection, not the M1 wire schema.
Admission resolves every model-using dependency before starting the workflow:

| Role | Native execution path | Upstream-only execution path | Accounting binding |
|---|---|---|---|
| Head/ARM planning | Admitted native model call | Eligible bounded harness session producing a validated proposal | Goal pool and planning reservation |
| Implementation | Native model/tool loop | Supported upstream session and scoped tools | Task attempt plus descendant reservations |
| Review | Separate eligible native verifier | Clean eligible harness verifier session | Review reserve and exact independence class |
| Summarization/memory | Deterministic extraction or explicitly bound model | Harness-native transform or eligible summarizer session | Context-service reservation, including failed transforms |
| Recovery analysis | Deterministic reconciliation or admitted model | Eligible recovery session only within the remaining cap | Cumulative recovery ledger |
| Setup assistance | Deterministic wizard first; optional admitted model | Optional eligible setup session | Explicit setup allowance; never necessary to boot |
| Consultation | Admitted role session | Eligible assistant/harness session | Parent consultation/depth budget |
| Learned routing/retrieval | Optional explicit model route | Eligible bound helper session | Router/retrieval reservation; deterministic alternative when equivalent |
| Hidden auxiliaries | Explicitly catalogued classifier/title/vision/helper | Harness feature enabled only with an eligible documented path | Same explicit pool ledger; unknown usage is not zero |

Each binding records role/run/goal version, provider/model, executing harness, interface/build,
credential reference, billing pool/unit, host, effective config, tools/grants, verification class,
observation source and conservative reservation rule. Included subscription capacity and API
spending are distinct units. Provider reset timestamps change availability, not task budgets.
Internal child usage must be counted once without losing parent lineage.

An upstream-only configuration must complete the required dependency closure with direct API
credentials absent. A helper that discovers a different provider, asks for a second key or
silently uses a metered pool fails admission. Unknown counters require a documented conservative
bound or a visible blocked configuration; they cannot become zero-cost calls. Same-provider
review is usable only under its permitted independence class. See the source-specific limits
in [the integration matrix](foundation-research/mark2-research/compatibility.json).

Ordinary persistence, scheduling, health observations, queueing, configuration compilation,
accounting and status are deterministic and event driven. Model inference is invoked for bounded
semantic work. A provider session can accelerate continuation; it is not the canonical writer,
authority source or scheduler. Native and upstream implementations share the same integrity path.

## Native and delegated engine accommodation â€” proposed

A provider adapter maps inference requests and billing fields; an engine adapter maps another
harness's lifecycle/tools/session; a Recursion plugin contributes optional admitted behavior.
Keep these identities separate. The common command bus owns canonical admission; engine events
are evidence inputs. Optional controls require explicit versioned capability negotiation.
The [foundation matrix](foundation-research/compatibility.json) distinguishes queued input,
active steering, question/approval exchanges, cancellation, native resume and recovery. FND-004.

Before any governed dispatch, bind packet/goal/plan/policy/grant/route and relevant lease/fence
identities. An upstream shell approval does not identify every descendant effect. Restrict or
refuse a route when a task-required fence cannot reach the protected resource. Process termination
remains `not-evaluated` for external effects; unknown outcomes stay ambiguous until reconciled.
M1A metered-spend/publication prohibitions cannot be widened by M1B route vocabulary or M1C
eligibility records. FND-002/FND-003/FND-005.

For future nodes, distinguish client host, authority coordinator, execution host, engine, inference
provider and account pool. Enrollment/revocation, locality and manifest-first transfer are adapter
concerns; one writer per aggregate and reconnect revalidation remain integrity obligations.
Disconnected nodes spool evidence and finish only previously permitted local work; no publication
or authority expansion. Single-user multi-node operation does not imply multi-tenant isolation.
`decision.foundation.remote-scope` and `experiment.foundation.remote` remain pending. FND-013.

## Separation of concerns

V3 resolves an execution route from independent declarations:

```text
task requirements
  + executive policy
  + model capability
  + engine/harness capability
  + provider and transport
  + capacity pool and billing basis
  + credential reference
  + host/node capability
  + sandbox and network profile
  + project privacy and publication policy
  -> immutable resolved route
```

A model is not a provider, a subscription is not a model, a CLI is not a billing pool, and a host is not authority. Keeping them separate lets the same GoalSpec survive subscription, provider, and machine changes.

## Route entities

- **Engine:** native Recursion loop, Codex CLI, Cursor CLI, Claude Code, Gemini CLI, OpenCode, Hermes, or another harness adapter.
- **Provider:** the service that performs inference or local endpoint contract.
- **Model:** a versioned capability description independent of delivery route.
- **Capacity pool:** one subscription, API account/budget, enterprise entitlement, local accelerator, or shared quota domain.
- **Route:** an eligible combination of engine, provider, model, pool, credential reference, and execution mode.
- **Host:** a registered execution node with measured resources and capabilities.
- **Profile:** sandbox, permission, network, context, verification, and resource rules.

One model may be reachable through several routes with different price, quota, privacy, cache, and health state. One CLI may expose several models or providers. Telemetry remains route-aware so it does not attribute Cursor subscription use to an abstract model or treat API and OAuth billing as interchangeable.

## Capacity pools

Capacity is model-aware and account-aware. A pool tracks:

- billing basis: subscription, metered, local, or unknown;
- models and reasoning modes actually exposed;
- concurrent-session and rate-limit observations;
- reset windows or uncertainty bounds;
- recent success, throttling, refusal, and outage signals;
- prompt/context cache evidence when observable;
- reservations and aggregate parent-goal consumption;
- privacy and provider-training constraints;
- health and credential-reference status without storing credentials.

Opaque flat subscriptions use confidence-aware shadow prices based on remaining capacity, resets, recent throttles, and opportunity cost. They must not be converted to fictional dollar prices. Metered pools use real configured prices and hard budget authority.

## Billing boundary

Automatic selection occurs only inside an owner-approved route envelope. Switching from a subscription/OAuth route to a metered API route is not ordinary failover; it is a billing-authority transition and requires existing parent-goal authorization.

The session's proposed default for the private deployment is a single aggregate `$10` metered budget for the parent goal when explicitly granted. It is not renewed per task. Public defaults should grant no metered fallback.

## Routing objective

The router does not simply choose the cheapest or strongest model. It predicts the least scarce eligible route likely to meet the task's quality floor, considering:

- task type, complexity, uncertainty, and risk;
- measured gate success and repair history by route;
- verifier disagreement and downstream review cost;
- context/session affinity and expected compaction cost;
- quota pressure, reset horizon, latency, and local resource load;
- privacy, egress, tool, and permission compatibility;
- reserved capacity for planning, integration, verification, and recovery.

Every route choice produces a deterministic explanation and a recorded feature snapshot. Online learning may reorder already approved routes but cannot add providers, widen authority, enable training use, or increase budgets.

Provider or plugin heuristics propose scores only over the kernel-produced eligible set. The deterministic admission layer validates the selected route again immediately before launch; a heuristic output is never an authorization.

## Checkpoint-driven adaptation

Routes remain stable between meaningful checkpoints. Re-resolution occurs after:

- task completion or decomposition;
- quota/throttle/outage event;
- verified capability mismatch;
- new failure evidence;
- material repository or plan change;
- owner override;
- scheduled capacity reset.

This avoids destroying cache/session affinity through constant micro-routing.

## Concurrency and admission

Admission is hierarchical and adaptive across node, pool, route, model, repository, goal, and review capacity. It:

- reserves configurable CPU/RAM/disk headroom for the OS and controller;
- uses measured task resource classes and foreground-machine activity;
- queues work when the appropriate quality route is unavailable;
- prevents overlapping writers and serializes integration per repository;
- protects capacity for verification and recovery;
- applies backpressure before throttling, lag, branch proliferation, or unreviewed-output growth;
- uses event-driven wakeups rather than polling;
- permits an audited owner override without concealing risk.

The private deployment historically carried an owner policy ceiling of 75 unique unreviewed
branches. Lucas has ruled that v3 uses measured, configurable branch/worktree admission instead
of treating 75 as universal. Signals include disk/inodes, Git/index latency, active process and
worktree cost, integration conflicts, stale age, unreviewed change volume and owner review
service rate. Policy uses hysteresis plus an explicit owner hard ceiling. Automatic pruning
remains limited to branches proven merged, superseded, or content-identical. More machine
headroom never justifies producing evidence faster than it can be reviewed.

## Scheduling

Goals receive priorities and optional reservations. Unused capacity may be borrowed. Interactive work may move ahead of queued autonomous work without killing safe active work. A goal consumes a whole node only when explicitly marked exclusive.

Configurable postures include:

- `quality-wait`;
- `quality-first-adaptive`;
- `throughput`;
- `cost-first`;
- `maximum`;
- owner-defined weighted policies.

## Node fabric

V3 hypothesizes a hub-and-spoke authority fabric:

- one authoritative controller per authority domain/OS account;
- authenticated enrolled nodes with separate controller, executor, observer, and verifier capabilities;
- signed, replay-protected parent-goal envelopes and work packets;
- least-privilege node roles and revocation;
- manifest-first, lazy transfer of content-addressed context and repository evidence;
- local spooling of signed events during disconnection;
- no autonomous authority expansion while disconnected;
- reconciliation through the controller before publication.

An owner authenticated on both devices may execute remote commands when the signed goal, node capability, permission profile, and risk-based step-up requirements permit it. Remote access is protected infrastructure, not a mechanism for locking an owner out of locally installed open-source software.

## Worker and integration flow

```text
ARM TaskNode
  -> exclusive worktree + immutable WorkPacket
  -> eligible native/external harness route
  -> normalized checkpoint/result
  -> controller-owned machine gate
  -> clean independent verification package
  -> evidence-based dispute resolution
  -> serialized integration candidate
  -> publication intent
  -> approved review branch
```

Verifiers receive GoalSpec, exact tree, diff, gates, and observable evidence, but not the implementer's persuasive narrative or hidden reasoning. Disagreement is resolved by collecting discriminating evidence, not model majority vote.

## Evidence hierarchy

From strongest to weakest:

1. exact source/candidate identity and reproducible machine evidence;
2. observed runtime/visual behavior with provenance;
3. independent verifier findings tied to exact evidence;
4. implementation reports and model explanations;
5. unsupported model assertion.

Lower levels may guide investigation but cannot overrule contradictory higher-level evidence.

## Integrated candidate ownership and recovery evidence

FRI-002 checks a combined deferred-delivery, loss, snapshot, recovery and continuation sequence
in both model boundaries. Candidate ownership includes retained source items and every pipeline
copy; delivered IDs and an explicitly lost ID account for all offered identities. Receiver ACK
entries reference delivered identities rather than becoming a second ownership bucket.

A lost notification does not establish retention loss. The finite eviction action explicitly
models source retention loss and removal of its sole live candidate; snapshot availability is
a separate action. Negotiation waits until earlier candidates have been installed. WELCOME
advances the literal audit cursor while the application's installed position still precedes
the loss. RESET installs only the matching snapshot disposition, clears receiver credit/ACK
state, and preserves every future candidate. Historical requires_reset stays true after accepted
recovery and cannot drive a new RESET. Source/store evidence must authenticate these assumptions.

The 32-action witnesses finish at event cursor4 or byte cursor6, with logical Stop interleaved
between WELCOME and RESET. The compact audit agrees with the unchanged M1B oracle on 45,240
nonempty finite controls, all prefixes of 12 historical controls, four special controls, and
each retained model witness. Actual emitted-exhaustion recovery remains in literal controls;
the normal-source product admits only funded emissions. No general adapter equivalence, storage
implementation or common-writer migration clearance follows.
See executed results.

## Replay evidence admission, 2026-09-07

RSE-001 establishes a local fixture path from selected namespace bytes to replay admission. The proposed adapter must bind the requested stream, epoch, cursor unit, generation and snapshot identity; derive a contiguous retained suffix; and refuse mixed or unverified evidence. A retained early event does not lower the floor past a later hole. A cursor inside a multibyte payload is not an event boundary. A selected-generation rollback must fail the controller's expected-generation pin even when the old files are intact.

This is a provisional construction refinement beneath unchanged M1B. A notification-queue miss alone is not replay loss. Complete replay from the client cursor advertises that replay baseline in WELCOME; advertising a future snapshot cursor would advance the immutable audit before replaying the intervening events. A true selected-namespace gap uses a verified snapshot at or beyond its derived floor, immediate RESET, and fresh CREDIT before further EVENTs. RSE-001 is confined to the [admitted local three-event experiment](construction/replay-store-evidence-20260907/dossier.md); production authentication, arbitrary concurrency and live upstream support are unknown.

## Generation selection and existing reader rights, 2026-09-07

GRA-001 distinguishes fresh admission from an existing pin. A new reader must atomically obtain a pin for the current generation/revision; an already admitted reader may finish its immutable old generation after a new selection or authority restart. No new old-generation pin is admitted after selection changes. Both tested policies preserve this rule: drain-before-publish closes new admission while existing pins drain; pinned-generation permits publication and retains old bytes until the last old pin is released. Two durable pins are tested, but two simultaneous reader child processes are not.

Publication validates the actual staged namespace/content under the complete current permit. A corrupt snapshot or missing event under an unchanged valid permit refuses selection changes. Commit-before-reply determines recovery; a lost reply cannot roll back durable publication. These named local observations do not prove arbitrary interleavings or end-to-end recovery. Preserve the accepted six obligations, 27-row coverage status and both formal boundaries. [Evidence and limits](construction/generation-authority-20260907/dossier.md).

The cross-arm research adds concrete adapter discriminators: friendly-ID child replacement must retain prior meter segments; isolated histories may still share mutable framework state; rejected steering may be absent from workflow history; durable promises and late transport ACKs do not renew authority. Require immutable proposals/current-revision adoption and explicit incarnation/operation bindings. Valid late authenticated evidence for the same stable effect may still resolve uncertainty; rejecting stale dispatch authority is not blanket rejection of historical evidence. [Comparative evidence and unrun adapter probes](construction/generation-authority-20260907/architecture-challenge.md).

## Generation and effect composition, 2026-09-08

GCM-002 separates immutable fork history, resident generation capacity and a live invocation pin in a checked finite composition. Under the declared two-slot/one-capsule bound, G3 can activate after G1 local quiescence and staged reclamation even while a detached remote operation is unresolved. Its uncertainty and separate attempt accounting remain. A resident-only G1 fork instead produces visible G3 hold. This is existential recovery under explicit finite assumptions, not unconditional liveness or measured capsule fidelity/cost. A capsule never permits unloading an in-process invocation still using that generation. The remote operation in this model has no remaining local runtime dependency after owned wait; other host-call cases need separate evidence. Composed ordered witness.

## Restricted literal effect join, 2026-09-08

EEJ-001 adds local fixture support for the M1Aâ†’M1C seam. Require original key, target, payload, dispatch command, intent/admission and ordered attempts; retain exact packet, GoalSpec, plan, lease, route and budget references and internal cross-record relationships. Keep collector, dispatcher and observer roles separate. M1A command acceptance records the supplied aggregate label; it does not authenticate an external result. Missing or contradictory supplemental evidence blocks the join rather than creating replacement observations.

Bind the original observation to its historical evidence prefix and classify the full trusted cut separately. A valid late same-attempt receipt or reconciliation may settle M1C while the original ambiguous record stays byte-identical. Preserve original receipt and operation digests with explicit codecs; neither effect capability keys nor packet attempt numbers identify provider operations or effect attempts. Current epoch assessment does not rewrite historical admission. The adapter's separate unsettled-attempt diagnostic can retain a hold even when literal M1C reports an earlier success. This supplies no runtime write/retry authority and does not close the common-writer migration blocker. [Bounded evidence](construction/effect-evidence-20260908/evidence.json).

## Evidence cuts and independent ownership â€” 2026-09-08

SCM-002 adds a bounded shared-model obligation: presentation recovery installs a
particular historical evidence cut without replacing the current inventory,
clearing quarantine or releasing a newer unresolved attempt. Literal success can
coexist with a later uncompleted start; a conflict can hold settlement even with
no pending IDs. The separately rerun M1A/M1C controls preserve classification as
evidence, never runtime permission. External-effect reservation ownership remains
a distinct, as-yet uncomposed variable.

An invocation pin and snapshot reader pin have different owners. Reclamation waits
for both applicable pins and retained history. A single ordered model trace
publishes G2 while a reader owns G1, refuses reclamation after invocation release,
then copies/waits the reader, reclaims G1 and installs the independent copied
snapshot. Neither receipt classification nor copy completion alone releases that
reader. Publication survives writer restart before acknowledgment; literal lost
reply/relocation recovery and the full goal/lease namespace remain unproved here.

[Bounded shared composition](construction/shared-composition-20260908/findings.md)
retains local Stop versus recipient-installed fencing and lifetime-spend versus
quota-reset distinctions. It does not complete original trace03/21/27, grant new
operational authority or resolve common-writer migration.

## Uncertain-effect reservations â€” 2026-09-08

SWR-002 stores a fixed pool/goal/operation/attempt/payload/owner/amount binding for
each admitted reservation. A matching duplicate retains its terminal disposition;
conflicting inputs do not gain another grant. Reservation commit, intent append,
send, environmental outcome, retained receipt, classification and reconciliation
are separate modeled transitions. Receipt loss/restart/replay preserves six held
and blocks blind resend. A successful reconciliation converts hold to consumed
allowance; exact terminal nonexecution releases unused allowance once.

Worker lifetime, provider quota epoch, generation/reader pin, classification hold,
canonical quarantine and presentation/replay cursors cannot release that effect
reservation. This is the fixed-identity finite relation, not a literal M1C runtime
bridge or proof of provider authentication. Existing dispatch envelopes, integration
roles/routes, eight experiment families and full workflow requirements remain.
[Scoped sources and results](construction/shared-reservations-20260908/evidence.json).

## SC-WIRE-02 â€” 2026-09-08

WIR-002 checks that an old zero-reservation presentation snapshot cannot release a current six-unit uncertain reservation. Install, receiver restart, quota and control preserve its fixed owner binding. The literal wire oracle cannot inspect this authority; independent local guards reject corruption. This is a held-reservation boundary, while original6+6-from10 admission remains in SC-WR-01. The full coupled product and common-writer migration remain unresolved.
[Evidence and limits](construction/wire-correspondence-20260908/evidence.json).

## SC-EPOCH-03 â€” 2026-09-08

EPC-001 binds consultation to typed goal, lease, issuer, requester incarnation, delivery generation, context/payload and existing operation grant. Received stale advice and valid historical adoption remain evidence; adoption and continuation each recheck current authority. Actual request payload conflicts remain distinct from matching duplicates. Owner-only GoalSpec amendment is unchanged; literal M1A stale-authority checks remain separate from this proposed application relation.
[Evidence and residuals](construction/typed-consultation-20260908/evidence.json).

## SC-COUPLED-01 â€” 2026-09-08

CPL-002 separates transported advice, current adoption/wake and bound terminal effect reconciliation. Old snapshots, ACKs, requester restart, quota reset and Stop cannot release uncertainty. Success consumes six permanently; only proven nonexecution plus reconciliation permits the competing six-unit request. Atomic durable commits, truthful authenticated receipts and same-run catalogue availability remain premises.
[Evidence and limits](construction/coupled-construction-20260908/handoff.md).

## Coupled evidence cut and PAUSE â€” 2026-09-08

CCP-002 checks the actual joined observation through literal M1A: fresh PAUSE refusal, exact duplicate receipt and awareness-journal growth remain distinct from runtime events and pure classification.
See [scoped findings and residuals](construction/coupled-cut-pause-20260908/findings.md). No runtime integration or full formal freeze follows.

## Retained-cut publication â€” 2026-09-08

CPB-002: Existing retained-cut reader pins bind exact generation and actor nonce through new
selection/restart. Fresh pin admission checks current selection; owned wait/release precedes
that generation's reclaim. Independent copied bytes remain inspectable after retirement,
while current-cut/source reconciliation authority and A6 reservation remain separate.
A restarted reader cannot steal an old live pin; physical orphan recovery is unconstructed.

See [SC-CUT-PUB-01](construction/cut-publication-20260908/handoff.md) for evidence and limits.

## Complete-source reservation boundary â€” 2026-09-08

The bounded source protocol preserves actual A6/B6 pool10. Registration-only source
change invalidates fresh publication and selected-cut reconciliation even with identical
record bytes. Pending registry entries absent from original attempt-start mapping retain
A6. Existing publication receipts, reader pins/copies, worker lifetime, quota/reset, replay
and classification remain distinct. Pending registry scaling to4 does not authorize
additional original literal attempts or external execution.
See [scope and results](construction/cut-source-overnight-20260908/findings.md).

## Endpoint kinds and adapter session mapping - 2026-09-28

Add `EndpointKind` (`local` / `byok` / `subscription` / `upstream-harness`) as an explicit route
resolution axis, orthogonal to `BillingBasis` and to `Engine`/`Provider`. An owner authorizes a
set as a route suite inside `RoutePolicyEnvelope@1`; unknown/expired eligibility blocks rather than
falls back.

Engine-adapter contract additions: (a) accept the Recursion `SessionId` + capsule; (b) report
native session ids only as affinity evidence (N:1 mapping); (c) bind accounting
`SessionId -> parent goal/pool`; (d) expose resume/fork/compaction as typed **candidate** events
that never advance the session epoch. A subscription route is admitted only inside the envelope; a
subscription-to-metered switch remains a billing-authority transition.
