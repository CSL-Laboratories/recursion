---
title: Recursion v3 autonomous-planning contract
status: proposed-normative-contract-not-runtime
last-updated: 2026-09-03
---

# Recursion v3 autonomous-planning contract

This is the canonical specification candidate for autonomous-planning objects, ownership,
admission, transition and replay. Other v3 documents project this contract and link here; they
must not redefine it. `OR-018` ratifies its Option 2 architecture boundary, but the contract is
not an operative runtime, construction authorization, or evidence that a model can plan correctly.

Normative words (`MUST`, `MUST NOT`, `SHOULD`) describe the proposed v3 contract. Current
constitutional authority continues to prevail over it. In particular, the current PAUSE halts
planning and work immediately. `OR-020` authorizes the checkpoint/interrupt/drain/emergency
proposal, but those profiles remain non-operative until the exact amendment is separately
reviewed and applied.

## Authority and responsibility

| Actor | May propose | May authoritatively decide | Must never do |
|---|---|---|---|
| the authenticated Owner | any goal, amendment, exception or terminal disposition | accept/amend/close GoalSpec; change constitution; authorize privacy, spend, publication, deployment and exceptional verifier seams | be inferred from model prose or an unauthenticated transport message |
| Recursion Head | draft GoalSpec; create/replace/stop ARM run; request plan revision; resolve cross-ARM conflicts; escalate | ARM lifecycle and conflict decisions inside an accepted GoalSpec, subject to kernel admission | silently amend Owner intent, write an ARM's task DAG, widen authority or treat addressability as authority |
| ARM executive | immutable PlanRevision, TaskNodes, retry/replan/blocker/escalation proposals | semantic tactics inside its GoalSpec and live executive lease after kernel admission | widen GoalSpec, mutate an accepted revision, grant itself authority or decide its own verification |
| Worker | observations and one WorkPacket result | execute only an admitted immutable WorkPacket under its live work lease | amend goal/plan/task, mint packets or accept gates |
| Verifier | GateResult and PlanVerifierDecision proposals | judge the declared evidence/gate under a valid verifier assignment | rewrite goal/plan, repair implementation, manufacture missing independence or self-assign |
| Deterministic kernel | typed admission/refusal events and receipts | validate authority/policy/budget/privacy/route/source, order events, fence leases, enforce PAUSE, admit effects, project canonical state and gate publication | choose product intent, invent tactics, sample models or infer missing policy |
| External harness/adapter | samples, stream/tool observations and provider receipts | provider wire protocol only | own Recursion authority, canonical state, plan transitions or external-effect truth |

The logical actor identity is stable across permitted model or route replacement. A model route
is an implementation assignment, never the identity or authority source. Sol/Luna/Terra are one
GPT-5.6 family and do not create cross-family independence.

## Common immutable envelope and bindings

Every record uses a strict versioned schema and has:

`object_type`, `schema_version`, `id`, `version`, `created_by_actor_id`, `created_under_lease`,
`causation_id`, `correlation_id`, `created_logical_time`, `payload`, and `content_digest`.

`content_digest` is SHA-256 over RFC 8785 canonical JSON of all fields except the digest. Unknown
fields, unknown schema/policy versions, duplicate JSON keys, non-I-JSON values, malformed or
unbounded values and digest mismatches MUST fail closed. Wall clock, hash-map iteration, network
state and model output cannot participate in pure replay unless captured first as a validated
immutable observation.

Every authoritative proposal or transition binds the exact applicable tuple:

`(OwnerIntent, GoalSpec version/digest, PlanRevision version/digest, TaskNode digest, WorkPacket
digest, logical actor, lease id/epoch, authority/grant digest, privacy/egress policy, route
descriptor/capacity pool, budget reservation, source revision, verifier policy, constitution
policy, PAUSE epoch)`.

`null`, `unknown` and `not-applicable` are different typed values. A required `unknown` blocks;
it never selects a permissive default. Descendant scope is computed by intersection and
attenuation, never by union or last-write-wins:

- allowed capabilities, paths, routes, accounts, nodes and effects are intersections;
- limits and budgets use the most restrictive bound;
- required gates, non-goals, prohibitions and audit obligations accumulate;
- privacy/egress may stay equal or tighten but never broaden without a new Owner-accepted
  GoalSpec;
- a lower-scoped record cannot replace a higher-scoped policy reference.

## Canonical planning objects

Lifecycle is an event projection; no accepted object is updated in place.

| Object | Canonical owner and identity | Immutable payload and bindings | Lifecycle and authority | Replay, retention and relationships |
|---|---|---|---|---|
| `OwnerIntent` | Owner; `OI-<scope>-<sequence>` | exact authenticated text/attachment digests, source event/transport, privacy, received logical time, supersession link | `received → clarified/compiled/fulfilled/superseded/rejected`; Owner creates/supersedes; Head may propose classifications only | exact input retained per Owner retention policy; compaction references but never replaces it; parent of GoalSpec draft |
| `GoalSpec` | Owner; stable goal id plus immutable version | outcome, acceptance, source, allowed paths/non-goals, authority, privacy/egress, routes/accounts/capacity, budgets, resources, verification, PAUSE/constitution refs, publication/effect bounds, terminal/Owner seams | `draft → pending-owner → accepted → superseded/closed/rejected`; Head compiles; only authenticated Owner acceptance/amendment makes it authoritative | all accepted versions and disposition events retained; parent of every ArmRun and PlanRevision |
| `GoalSpecAmendment` | Owner; unique command plus old/new GoalSpec digests | exact changed fields, rationale, authorization, effective fence epoch, safe-checkpoint rule, required old-plan/node/effect disposition | `proposed → accepted/refused → applied`; only Owner proposes/accepts; kernel validates | never edits old GoalSpec; emits new version and invalidation/fence events |
| `ArmRun` | Head; `AR-<goal>-<sequence>` | logical ARM id, GoalSpec binding, profile/route eligibility, budget, source, executive lease policy, verifier requirement | `proposed → admitted → planning/running/blocked → completed/failed/stopped/superseded`; Head controls lifecycle; kernel admits | route/model may change only through assignment events; logical ARM id and policy bindings persist |
| `PlanRevision` | ARM; stable plan id plus monotonically increasing revision | GoalSpec digest, full DAG, acceptance closure, budgets/reservations, path/effect ownership, source, policy bindings, prior revision, change rationale and complete old-node disposition map | `proposed → admitted → active → blocked/superseded → terminal-verified/terminal-verified-downgraded/terminal-failed`; ARM proposes; verifier judges; kernel admits/transitions | immutable aggregate snapshot; exactly one active revision per ArmRun; event replay reproduces its digest |
| `PlanTransition` | ARM proposal, kernel admission; unique command id | from/to plan digests, trigger evidence, retained/invalidated/quarantined/superseded/cancelled sets, lease and epoch, checkpoint | `proposed → admitted/refused → applied`; stale from-digest or lease refuses without mutation | atomic event group; supplies deterministic revision boundary and audit rationale |
| `TaskNode` | ARM; plan-scoped stable semantic id and version | dependencies, source, inputs/outputs, read/write paths, effect domains, authority/budget/route subset, gate/verifier refs, retry maximum and terminal contribution | states defined below; ARM proposes only inside a PlanRevision; kernel admits; worker cannot edit | immutable inside a PlanRevision; new version/revision supersedes; evidence may be retained only by compatibility proof |
| `WorkPacket` | deterministic kernel derivation; `WP-<task>-<attempt>-<digest>` | exact TaskNode plus attenuated goal/plan/policy/grant/privacy/route/budget/source/lease/gates, allowed actions, explicit prohibitions, output schema and deadline/checkpoint | `derived → admitted → leased → executing → result-submitted → gate-pending → consumed/stale/quarantined`; kernel alone derives/admits; worker executes | immutable/content-addressed; any binding or lease/epoch mismatch makes it stale; no packet survives GoalSpec amendment |
| `GateResult` | assigned verifier or deterministic gate; gate/run id | candidate/source/packet/evidence digests, procedure/version, observation, expected/actual, independence vector, disposition and limitations | `submitted → admitted/refused`; disposition is `pass/fail/error/missing/ambiguous`; kernel records but does not invent verdict | duplicate exact result idempotent; conflicting duplicate refused; all required results retained with candidate |
| `RetryDecision` | ARM proposal, kernel admission | failed attempt/gate, typed novelty and evidence/diff, unchanged bounds, next attempt, remaining budget and maximum | `proposed → admitted/refused → consumed`; admission returns task to ready for a new packet or stops | linked to failure and next packet; non-novel/refused decision cannot invoke a model or consume attempt budget |
| `ReplanDecision` | ARM proposal, Head involvement when conflict/goal seam requires, kernel admission | trigger, old/new revision, full disposition map, narrowed bounds, unresolved blocker/escalation | `proposed → admitted/refused → applied`; ARM may replan within goal; Head may request/accept lifecycle result, not author DAG | creates a new immutable PlanRevision; atomic replay boundary |
| `Blocker` | discovering actor proposes; kernel classifies/binds | type, exact blocked object, evidence, owner needed, allowed recovery, retry-after capacity observation if applicable | `open/deferred → resolved/superseded/escalated`; only a capacity blocker with a bound retry-after observation may enter `deferred`; only named authority supplies resolution | retained with failed/refused transitions; required blocker means node/plan cannot be complete |
| `Escalation` | ARM to Head or Head to Owner; unique id | blocker/failure history, attempts/novelty, requested decision, available options, recommendation, scope frozen pending answer | `raised → acknowledged → resolved/declined/superseded`; target authority answers | never widens authority by itself; resolution references an authenticated decision/amendment |
| `PlanVerifierDecision` | assigned plan verifier | exact GoalSpec/plan/event-log/gate/evidence digests, independence vector, `pass/downgraded-pass/fail/blocked`, assurance grade, optional Owner exception receipt, dissent and limitations | `submitted → admitted/refused`; missing verifier/evidence is `blocked`; `downgraded-pass` requires the exact policy-bound exception below | full `pass` is required for `terminal-verified`; a valid downgrade can produce only `terminal-verified-downgraded`; verifier cannot create a replacement plan |
| `CommandEnvelope` / `CommandReceipt` | authenticated caller / kernel | command id/type/schema, actor/lease, expected aggregate version/digest, payload digest, bindings / accepted-refused-duplicate-conflict and emitted event span | strict decode → valid envelope receives one receipt; malformed JSON/schema returns `DecodeFailure`; stale/unauthorized valid commands refuse before semantic execution | same id+digest returns original receipt; same id+different digest is conflict |
| `PlanningEvent` | kernel sequencer | stream id, sequence, previous/event digest, logical time, admitted command, type/version and payload | append-only after validation; no model writes event storage | replay source of truth; corrupt/gap/unknown event halts rebuild |
| `ExecutiveLease` / `WorkLease` | kernel grants to logical actor | subject, goal/plan/task scope, epoch, nonce, issued/expiry logical time, allowed commands, revocation | `offered → active → expired/revoked/replaced`; only kernel grants/replaces after authority checks | replacement increments epoch; stale holder proposals refuse; lease alone is not external-effect fencing |
| `Checkpoint` | kernel records from admitted evidence | aggregate versions/digests, open packets/effects, budget/route reservations, PAUSE/lease epochs and ambiguity set | `proposed → validated → durable`; required for widening/reassignment boundaries | replay optimization only; must reproduce event-derived digest or is ignored |
| `EffectIntent` / `EffectReceipt` | ARM/worker proposes under packet / kernel+adapter observe | exact idempotency/effect id, action, target, policy/lease/fence, prepare/dispatch/provider ids and observed status | `proposed → prepared → dispatched → observed-success/observed-failure/ambiguous → reconciled`; kernel gates every step | ambiguity cannot be success; external acceptance is never undone by state rollback |
| `RouteAssignment` / `BudgetReceipt` | Head/ARM request; kernel admits | logical actor, exact descriptor/account/capacity pool, privacy, zero/metered authority, reservation/debit observation and expiry | `proposed → reserved/assigned/refused/deferred → released/expired`; replacement at checkpoint/new packet only | route changes preserve actor and all policy bindings; missing quota defers, never enables spend |

Retention duration is policy-bound, but authority decisions, accepted GoalSpec/PlanRevision
digests, command receipts, transition events, gate/verifier decisions, grant/budget/effect receipts,
ambiguity/reconciliation and publication lineage cannot be silently compacted away. Raw private
payloads may expire under [STATE.md](STATE.md); durable records retain only the permitted
non-sensitive digests/tombstones and explicit proof state.

## Goal compilation and amendment

1. The kernel authenticates and appends raw `OwnerIntent`; no summary substitutes for it.
2. Head compiles a complete draft GoalSpec and records every interpretation, unresolved conflict
   and source intent. A model-generated draft has no execution authority.
3. Deterministic validation rejects missing parent/outcome/gates, ambiguous source/scope,
   unauthenticated authority, unbounded values, unknown policy, impermissible privacy/route/spend
   or internal contradiction.
4. The authenticated Owner accepts the exact GoalSpec digest. An authenticated direct Owner command may both supply
   a complete spec and accept its digest; otherwise explicit acceptance is required.
5. The kernel appends `GoalSpecAccepted`; only then may Head propose an ArmRun.

Only the authenticated Owner may amend GoalSpec intent, scope, acceptance, privacy exposure, authority, spending or
terminal seams. Head and ARM may narrow descendant assignments without changing the GoalSpec.
New operative law/constitution and authenticated Owner restrictions take effect immediately at
admission and effect recheck; they are not GoalSpec mutations. A GoalSpec restriction amendment
also fences new/undispatched work immediately. Widening, relaxed privacy, new egress/account,
metered spend, weaker verification, publication or effect power requires a new Owner-accepted
version and a validated safe checkpoint.

Amendment proposed 2026-10-25 (acceptance is a human decision by Lucas, CSL-524): **product-spec**
(`docs/v3`) amendments by Samir or Dan are accepted by another founder's GitHub `APPROVED` review
on the exact head SHA with file digests (`CONTROL_PLANE.md` product-spec acceptance). That rule
is not GoalSpec digest acceptance. GoalSpec remains Owner-authenticated; CSL's first-user Owner
remains Lucas until a GoalSpec names another founder.

Every accepted amendment increments the goal fence epoch and atomically classifies old state:

| Old item | Required deterministic disposition |
|---|---|
| active PlanRevision | superseded; a new revision must bind the new GoalSpec |
| proposed/admitted TaskNode | retain definition only if a field-by-field compatibility predicate proves source, inputs, outputs, bounds and gates still satisfy the new goal; otherwise invalidate/supersede/cancel |
| completed TaskNode/evidence | retain only if source/content digests match, no invalid authority/effect was used, and new/equal gates deterministically accept it; otherwise invalidate or quarantine |
| WorkPacket and work/executive lease | always stale/revoked across the GoalSpec epoch; compatible work is reissued with a new packet/lease |
| prepared but undispatched effect | cancel unless explicitly re-admitted under the new packet |
| dispatched or possibly accepted effect | quarantine as ambiguous or preserve observed receipt; reconcile forward, never relabel/undo through replay |
| route/budget reservation | release or revalidate under the new exact bounds; never carry a widened capacity/spend implication |

The new `PlanTransition` must list every prior node exactly once as retained, invalidated,
quarantined, superseded, cancelled or already-terminal-compatible. An incomplete disposition map
fails closed.

### Closed amendment-compatibility grammar

`compatible` is not a model judgment. `CompatibilityInput@1` is a closed record. It contains
exactly:

- `compatibility_schema_version="compatibility-input-1"`;
- `old_goal_ref` and `new_goal_ref`, each `{goal_id, version, content_digest, fence_epoch}`;
- `old_node` and `new_node`, each the complete `NodeCompatibilityTuple@1` below;
- `replacement_node_id` and `replacement_node_digest`, both `null` or both equal to the new
  node and to the PlanTransition's explicit old-to-new replacement mapping;
- `prior_result`, either `null` or `{state, candidate_digest, evidence_digests,
  gate_result_digests, plan_verifier_decision_digest, assurance_grade}`;
- `prior_effect`, always `{state, effect_id, intent_digest, receipt_digest}`; and
- `consumed_budget`, the same closed non-negative-integer budget map used by the old node.

`prior_result.state` is one of `result-submitted`, `gate-pending`, `verified`, `complete` or
`failed`, with assurance `full`, `downgraded` or `none`. `prior_effect.state` is one of
`none`, `prepared-undispatched`, `observed-success`, `observed-failure`, `ambiguous` or
`reconciled`; fields inapplicable to `none` are typed `null`, never omitted.

`NodeCompatibilityTuple@1` contains paired values rather than unnamed comparison inputs:

| Exact field | Canonical type |
|---|---|
| `node_id`, `node_version`, `node_digest` | bounded identifier, positive integer, digest |
| `semantic_definition_digest`, `output_schema_digest` | digest |
| `dependency_node_ids`, `input_digests`, `source_digests`, `read_resource_paths`, `write_resource_paths`, `effect_keys` | duplicate-free sorted arrays of exact strings/digests |
| `authority_ids`, `capability_ids`, `allowed_action_ids`, `prohibition_ids`, `route_ids`, `route_capability_ids`, `account_class_ids`, `capacity_pool_ids` | duplicate-free sorted identifier arrays |
| `privacy_class_id`, `egress_class_id`, `authority_policy_digest`, `privacy_policy_digest`, `egress_policy_digest` | identifiers plus exact GoalSpec-bound policy digests |
| `budget_limits` | closed map `{model_turns, attempts, tokens, money_minor, cpu_ms, memory_bytes, output_bytes}`, each a non-negative integer |
| `gate_requirements` | duplicate-free sorted `GateRequirement@1` array |
| `retry_policy_digest`, `verifier_policy_digest`, `constitution_policy_digest`, `terminal_contribution_digest` | digest |
| `retry_maximum` | non-negative integer |

`GateRequirement@1` is exactly `{gate_id, procedure_digest, evidence_schema_digest,
candidate_binding_digest, verifier_policy_digest, assigned_verifier_actor_id}` and is sorted by
`gate_id`. M1A defines no
implicit gate-strength order. Any added, removed or unequal gate requirement, procedure,
evidence schema, candidate binding or verifier policy is a changed node contract and invalidates
prior completion. A future non-conservative strength order requires a new policy version and
Owner acceptance; it is not guessed by M1A.

The compatibility command supplies only refs/digests. The reducer loads the referenced canonical
GoalSpecs, nodes, gates, results and policy fixtures from the replay state, reconstructs both
tuples, and rejects the entire amendment with the applicable schema/digest/policy error if a
supplied value differs. The only node-disposition outputs are `retain-definition`,
`retain-terminal`, `invalidate`, `quarantine` or `supersede`; `cancel` belongs to the separate
effect disposition. Evaluation is ordered and total:

1. malformed/missing input, unknown schema/policy/ref, digest mismatch or an incomplete
   old-to-new mapping refuses the whole amendment without an event;
2. an ambiguous, contradictory or unauthorized prior effect produces `quarantine` for the node
   and effect; a merely prepared-undispatched effect receives the separate `cancel` effect
   disposition;
3. inequality in any old/new tuple field except `node_version` and `node_digest` produces
   `supersede` only when the non-null replacement fields match the new tuple and transition
   mapping; otherwise it produces `invalidate`;
4. the new tuple must be attenuated by the new GoalSpec: every authority/capability/allowed-action/
   route/path/effect set is a subset, prohibitions are a superset, privacy and egress are equal or
   more restrictive under the exact finite policy relation, every budget limit is at most the
   goal limit, and consumed budget is at most both the node and goal limit. Failure produces
   `invalidate`;
5. a nonterminal old node satisfying 1–4 produces `retain-definition`; every old packet and
   lease is nevertheless stale and compatible work requires a newly derived packet;
6. a terminal old node additionally requires the prior candidate/evidence/result digests to
   match the exact retained tuple, every required GateResult to be `pass`, and the exact same
   PlanVerifierDecision digest and assurance grade. It then produces `retain-terminal`;
   otherwise its evidence remains historical and the node produces `invalidate`; and
7. an observed immutable effect receipt remains evidence under its separate disposition but
   cannot retain task authority or convert ambiguity into success.

The M1A wire profile breaks content-digest cycles deliberately: a TaskNode binds its containing
plan by stable ID/version only, while the PlanRevision binds the complete TaskNode content
digests. Projection state similarly stores processed receipt identity plus command digest rather
than receipt content digest; the receipt binds the already-computed before/after state digests.
No other identity-only reference is implied.

Arrays compare byte-for-byte after duplicate rejection and required sorting; set inclusion is
over exact identifiers; numeric comparison is exact integer comparison; privacy classes use the
GoalSpec-bound finite partial order. No natural-language similarity, embedding, model score,
wall clock or provider observation participates. Unknown/incomparable relations fail closed.

## Plan and DAG admission

An ARM may create/revise a DAG only while holding the current ArmRun executive lease and only
inside the accepted GoalSpec. Kernel admission MUST:

1. validate all record/binding digests and descendant attenuation;
2. require unique task IDs and dependency references to existing nodes in the same revision;
3. topologically sort the complete graph and reject any cycle or unsatisfied/missing dependency;
4. normalize paths under a versioned platform policy and reject unknown/ambiguous paths;
5. reject potentially concurrent write/write or write/read overlaps unless a dependency orders
   the nodes and an exclusive ownership transfer is declared;
6. reject overlapping external-effect targets unless exact effect-ID partitioning and dependency
   ordering make one authoritative owner mechanically provable;
7. require bounded inputs/outputs, budget/attempt limits, source, policy, route eligibility,
   gates, verifier requirements and terminal contribution;
8. reject unknown policy versions, missing verifier capability, unauthorized fallback and
   unreserved aggregate budget; and
9. compute the canonical plan digest and append admission atomically.

A TaskNode is:

- `proposed` before graph admission;
- `admitted` when its containing PlanRevision passes;
- `blocked` when a typed dependency/resource/route/quota/verifier/policy/effect/Owner condition is
  unresolved;
- `ready` only when all dependencies are complete, the active goal/plan/fences match, no path or
  effect conflict exists, budget/route/resources/verifier are reserved, and PAUSE is not active;
- `running` only while one admitted WorkPacket has the live WorkLease;
- `gate-pending` after a bounded result is recorded and before all required gates decide;
- `verified` when all required GateResults pass against exact digests;
- `complete` only after the kernel consumes verified outputs and appends completion;
- `failed` on a gate failure, terminal worker error, retry exhaustion or invalid output;
- `stale` on any binding/lease/epoch mismatch;
- `superseded` when a newer admitted revision names its replacement;
- `cancelled` only before an unambiguous external effect remains; and
- `quarantined` when authority/evidence/effect status is ambiguous or later invalidated.

`complete`, `superseded`, `cancelled` and resolved `quarantined` are terminal for that node
version. `failed` may return to `ready` only through an admitted RetryDecision for a new attempt.
`blocked` may become `ready` only through a recorded resolution whose digest satisfies the named
condition. No state may be skipped by transcript text.

## Deterministic WorkPacket derivation

For an admitted ready node, the kernel derives a WorkPacket as a pure function of the common
binding tuple and current reservations. It copies exact source/input/output/gate definitions and
computes capabilities, paths, effects, privacy/egress, routes, budget and resource limits by the
attenuation rules above. Prohibitions and non-goals accumulate. The output includes exact
attempt, lease epoch, PAUSE epoch, deadline/checkpoint and result schema.

The exact assigned gate-verifier actor is part of each GateRequirement, and the exact assigned
plan-verifier actor is part of PlanRevision. A self-reported independence vector cannot create or
replace either assignment. GoalSpec binds authority, privacy, egress and route policies plus the
allowed route, account-class and capacity-pool sets; every descendant copies or narrows those
bindings.

Only the kernel may assign the resulting content digest and admit the packet. Any amendment,
new PlanRevision, grant/policy/route/budget/source change, PAUSE epoch, lease replacement or
deadline mismatch makes it stale before the next action. Adapter, worker, verifier or stale
executive attempts to rewrite/extend it are refused and audited.

## Gates, retry, replan and escalation

Failed gates return the exact GateResult and evidence digest to the ARM. Missing/error/ambiguous
required gates block; a risk-approved verifier downgrade is a distinct exception receipt and
must state the missing independence dimensions. It is never reported as full independence.

`PlanVerifierDecision@1.disposition` is exactly `pass`, `downgraded-pass`, `fail` or `blocked`.
`pass` requires every verifier-policy dimension and has `assurance_grade="full"` and
`exception_receipt=null`. `downgraded-pass` has `assurance_grade="downgraded"` and is admitted
only when an authenticated Owner exception receipt binds the exact GoalSpec, PlanRevision,
candidate, verifier-policy and operative constitution digests, missing dimensions, reason and
unexpired logical-time bound. `fail` and `blocked` have `assurance_grade="none"` and cannot close
a plan. A full pass projects `terminal-verified`; a valid downgrade projects the distinct
`terminal-verified-downgraded`. Policy, UI, receipts and reports MUST preserve that suffix and the
missing dimensions. No downgrade is cross-family/full independence, and no absent or mismatched
exception becomes a pass. This is an Owner-authorized representability proposal; the exact risk
classes and transition rules are in
[CONSTITUTIONAL-AMENDMENT-PROPOSAL.md](CONSTITUTIONAL-AMENDMENT-PROPOSAL.md). Those V0-V3 classes
remain non-operative until that amendment is applied; runtime policy must not infer them early.

A retry is admissible only with one `NoveltyDelta@1`. It has exactly the common fields
`novelty_schema_version="novelty-delta-1"`, `delta_id`, `kind`, `prior_attempt_digest`,
duplicate-free sorted nonempty `addresses_failure_ids`, `old_descriptor_digest`,
`new_descriptor_digest`, `canonical_diff`, `canonical_diff_digest` and `delta_digest`, plus
exactly one kind payload below. Every addressed ID must resolve to a failed GateResult or open
Blocker on the prior attempt. Unknown or extra fields fail schema admission.

M1A logical identifiers use these ASCII grammars: `evidence-[a-z0-9][a-z0-9-]{0,62}`,
`proposition-[a-z0-9][a-z0-9-]{0,62}`, `operation-[a-z0-9][a-z0-9-]{0,62}`,
`capability-[a-z0-9][a-z0-9-]{0,62}` and
`dependency-[a-z0-9][a-z0-9-]{0,62}`; `delta_id` uses
`delta-[a-z0-9][a-z0-9-]{0,62}`. `EvidenceRef@1` is exactly
`{evidence_id, evidence_type, locator, content_digest, schema_digest}`; `evidence_type` is one of
`gate-output`, `fixture-observation`, `synthetic-source` or `repair-receipt`, and `locator` is
exactly `fixture://evidence/<evidence_id>`. M1A never dereferences that locator or accepts a URL,
path or free-form locator.

`CanonicalDiff@1` is recomputed, never trusted from the proposer. Starting at the two referenced
descriptor values, the kernel recursively compares objects. If both values are objects, it walks
the union of property names in RFC 8785 UTF-16 code-unit order; a property absent on one side
emits one entry and is not descended into. If types differ, or either value is a scalar or array,
inequality emits one entry at the current path. An entry is exactly
`{path_segments, old_value, new_value}`, where `path_segments` is the array of object keys from
the root and each value is either `{state:"present", digest:D(value)}` or
`{state:"absent", digest:null}`. Entries sort lexicographically by `path_segments` under the same
ordering. `canonical_diff_digest=D(entries)`; `delta_digest=D(the complete delta without
delta_digest)`. A supplied list/digest unequal to the recomputation fails with
`E-DIGEST-MISMATCH`; an empty list is non-novel. Arrays in every descriptor have already passed
their schema's duplicate/sort rules and compare atomically.

For `evidence-added`, `hypothesis-replaced`, `strategy-changed`,
`route-capability-changed`, `task-narrowed` and `dependency-repaired`, the diff operands are
respectively the old/new evidence arrays, hypothesis descriptors, strategy descriptors, route
descriptors, scope descriptors and dependency inputs. For `owner-amended`, they are the complete
old/new canonical GoalSpec payloads loaded by the refs. `old_descriptor_digest` and
`new_descriptor_digest` must equal `D` of those exact operands. Every descriptor self-digest
(`hypothesis_digest`, `strategy_digest`, `descriptor_digest` or `scope_digest`) is likewise `D`
of that descriptor with its self-digest field omitted.

The GoalSpec's exact `novelty_policy_ref` resolves one `NoveltyPolicy@1` containing duplicate-free sorted arrays
`accepted_evidence_types[{failure_id,evidence_types}]` and
`failure_capability_requirements[{failure_id,capability_ids}]`, plus one route envelope
`{route_ids,account_class_ids,capacity_pool_ids,allowed_spend_classes}`. Each referenced failure
must exist in replay state and in the applicable map. M1A permits route spend classes only
`none` and `included`; `metered` is representable as a refused input, never in the allowed
envelope. Unknown IDs, maps, relations or versions fail closed.

The seven `kind` payloads and their complete mechanical rules are:

| `kind` | Exact payload and admission rule |
|---|---|
| `evidence-added` | `old_evidence_refs`, `new_evidence_refs`, each sorted and unique by `EvidenceRef@1.evidence_id`. Every old record must occur byte-identically in new; new must add at least one ID and may not remove or replace one. Every addition's type must appear in the exact GoalSpec-bound `accepted_evidence_types[failure_id]`, and each addressed failure must receive at least one allowed addition. |
| `hypothesis-replaced` | `old_hypothesis`, `new_hypothesis` and `changed_proposition_ids`; each descriptor is `{hypothesis_digest, propositions}` with a sorted nonempty array of `{proposition_id, proposition_digest}`. The kernel recomputes each hypothesis digest and the changed set as the sorted IDs added, removed or mapped to a different digest; that set must be nonempty and equal the supplied set. |
| `strategy-changed` | `old_strategy`, `new_strategy` and `changed_operation_ids`; each descriptor is `{strategy_digest, operations, edges, goal_ceiling_digest, resource_ceiling_digest, effect_ceiling_digest}`. Operations are sorted `{operation_id, operation_digest}`; edges are sorted `{from_operation_id,to_operation_id}` over known operations. The kernel recomputes strategy digests and the changed set from added/removed/changed operations or incident edge changes. That set must be nonempty/equal the supplied set, while all three ceiling digests remain equal. |
| `route-capability-changed` | `old_route_descriptor`, `new_route_descriptor` and `newly_observed_capability_ids`; each descriptor is `{descriptor_digest, route_id, account_class_id, capacity_pool_id, spend_class, capability_ids, observation_digest, allowed_envelope_digest}`. Descriptor digests are recomputed; the envelope digest and spend class are exactly equal, the spend class is `none` or `included`, both routes/accounts/pools are members of the bound envelope, and the supplied set must equal `new.capability_ids - old.capability_ids`. The finite failure-capability map must give every addressed failure a nonempty required set that was not satisfied by old capabilities and is satisfied by new capabilities; every added capability must occur in at least one addressed requirement. |
| `task-narrowed` | `old_scope`, `new_scope`, each `{scope_digest, authority_ids, capability_ids, allowed_action_ids, prohibition_ids, route_ids, route_capability_ids, account_class_ids, capacity_pool_ids, read_resource_paths, write_resource_paths, effect_keys, privacy_class_id, egress_class_id, authority_policy_digest, privacy_policy_digest, egress_policy_digest, budget_limits, required_output_schema_digest, gate_requirements}`. Digests are recomputed. New allowed sets must be subsets, prohibitions must be a superset, all policy digests must remain exact, privacy/egress must be equal or stricter under the bound finite order, each new integer limit must be `<=` old, output schema and `GateRequirement@1` arrays must be identical, and at least one allowed set is proper, a prohibition is added, a class is stricter or an integer is lower. |
| `dependency-repaired` | `{predicate_id, predicate_version, predicate_input_schema_digest, old_input, new_input, old_result_digest, repair_receipt_digest, new_result_digest}`. M1A's only registered predicate is `dependency-artifact-present` version 1 with input `{required_artifact_digest, observed_artifact_digests}`; it returns pass exactly when the required digest is in the duplicate-free sorted observed array. A result digest is `D({predicate_id,predicate_version,input_digest,verdict})`. The kernel recomputes the input/result digests and requires old=false, new=true, unequal inputs and the repair receipt digest to be one of the new observed digests. Unknown predicate/version/schema refuses. |
| `owner-amended` | `{old_goal_ref, new_goal_ref, accepted_owner_command_receipt_digest, amendment_disposition_digest, retained_node_id, compatibility_input_digest}`. Replayed state must contain the exact accepted Owner receipt and amendment, the new goal must supersede the old, and the referenced `CompatibilityInput@1` must deterministically output `retain-definition` for that node. |

The kernel admits a delta only when its exact kind/schema, descriptors, recomputed diff/proofs and
failure bindings pass; the same delta digest has not been consumed; and attempt, budget, route,
PAUSE and effect conditions still pass. It does **not** decide that evidence is credible, a new
hypothesis true or a strategy wise; the next declared gate tests that semantic proposal.
Rephrasing without a typed descriptor change, a route label change without the required finite
capability proof, or previously consumed evidence is non-novel and is refused before another
model turn.

Exhaustion creates a Blocker and Escalation rather than an unbounded loop.

The ARM MUST propose a new PlanRevision instead of retrying when the DAG/dependency/path/effect
ownership changes, an amendment or stale source invalidates the plan, the planned verifier/route
is unavailable beyond its bound, repeated failures falsify the strategy, or new evidence changes
more than one node contract. It may narrow inside GoalSpec. It escalates to Head for cross-ARM
conflict, ARM replacement or goal-level tactical failure. Head escalates to Lucas for GoalSpec or
constitution change, new privacy/egress/spend/effect authority, an unavailable high-risk verifier
exception, an irreversible ambiguous effect or a semantic choice not recorded by the Owner.

Quota exhaustion is `Blocker(kind=capacity, state=deferred)`. It releases or preserves only
explicit reservations and resumes after a new capacity observation; it cannot enable metered
spending. Provider refusal is a typed blocker. A permitted replacement route is assigned only at
a checkpoint/new WorkPacket, preserving logical actor identity, goal/policy bindings and exact
budget/privacy bounds.

## PAUSE and ambiguous effects

The broader proposed schema family can encode `current-immediate`, `checkpoint`, `interrupt`,
`drain` and `emergency-stop`, each tied to a constitution-policy version. M1A represents only
`current-immediate` plus the inert `checkpoint`, `interrupt` and `drain` values; emergency policy
is outside this construction slice. Only `current-immediate` is
operative under the present constitution. On current PAUSE, the kernel refuses new planning,
sampling, packet admission, worker execution and effect/publication transitions; it may record
minimal awareness/status evidence allowed by the constitution. Representing a future policy does
not select it.

An effect is successful only with a verified provider/local acceptance receipt under the exact
effect id and fence. Loss of response, crash after dispatch, timeout without authoritative status
or conflicting receipts produces `ambiguous` and quarantine. The only next actions are
provider-specific status reconciliation, Owner-authorized remediation or terminal unresolved
disposition. Replay, cancellation, rollback or retry cannot convert ambiguity to success.

## Deterministic replay and idempotency

Replay starts from genesis or a digest-verified checkpoint and consumes events ordered by stream
sequence. For each event it verifies schema/policy version, previous/event digest, command
receipt, authority/lease/fence and legal state transition. The reducer is pure and emits the
canonical projection and plan digest. Unknown event/policy, a gap, conflicting duplicate,
out-of-order transition or checkpoint mismatch halts replay; there is no best-effort recovery.

Two replays of the same accepted events and schema/policy bundle MUST produce byte-identical RFC
8785 canonical state and the same digest. An exact duplicate command returns its prior receipt
without another event/effect. The same command ID with another digest is a conflict. A stale
executive lease cannot author an authoritative proposal; a stale WorkPacket cannot execute; a
worker/verifier/plugin/harness has no command type that amends GoalSpec or PlanRevision.

## Research anchors and limits

These sources informed the proposed mechanics; none supplies Recursion authority or proves the
combined design:

| Source, version and locator | Adopted or adapted point | Contradiction/limit and revalidation |
|---|---|---|
| [RFC 8785, June 2020](https://www.rfc-editor.org/rfc/rfc8785.html), §§3–3.2 | canonical JSON bytes for content digests | informational canonicalization does not define schema validity, authority or semantic equivalence; immutable RFC |
| [RFC 9110, June 2022](https://www.rfc-editor.org/rfc/rfc9110.html), §13.1.1 `If-Match` | compare an expected representation version before mutation | an HTTP precondition is not actor authority, lease fencing or replay; immutable RFC |
| [RFC 6902, April 2013](https://www.rfc-editor.org/rfc/rfc6902.html), §§4–5 | explicit ordered change operations and all-or-error patch application | a patch can still express an unauthorized or semantically invalid widening; immutable RFC |
| [CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md), §§3.1–3.2 | required event identity, source, type and time/context vocabulary | an event envelope does not establish trust, ordering, causation or admissibility; revalidate on adopted spec version change |
| Kubernetes [Controllers](https://kubernetes.io/docs/concepts/architecture/controller/) and [Leases](https://kubernetes.io/docs/concepts/architecture/leases/), retrieved 2026-09-03 | desired/current reconciliation and explicit holder/renewal records | controller convergence is not plan correctness; a lease record without resource-version/epoch enforcement is not a complete fence; revalidate on Kubernetes/docs version change or before implementation |
| Temporal [Workflow execution](https://docs.temporal.io/workflow-execution) and [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies), retrieved 2026-09-03 | history-driven deterministic workflow replay and separately bounded retry policy | Temporal's runtime contract is not Recursion goal authority, retry novelty or effect reconciliation; revalidate on adopted server/SDK/docs version change |
| A. B. Kahn, [“Topological sorting of large networks,” 1962](https://doi.org/10.1145/368996.369025) | deterministic whole-graph acyclicity/topological-order check with a specified tie-break | acyclicity alone does not establish satisfied dependencies, path ownership or effect commutativity; immutable paper |
| R. Tarjan, [“Depth-First Search and Linear Graph Algorithms,” 1972](https://doi.org/10.1137/0201010) | strongly connected components as an independent cycle-analysis reference | graph structure does not decide semantic conflicts or authority; immutable paper |

The Recursion-specific rules—Owner acceptance, Head/ARM ownership, attenuation, PAUSE,
privacy/egress/spend bindings, stale-work disposition, novelty categories, effect ambiguity and
publication gates—are repository-local proposals constrained by current authority. They are not
properties inferred from these external systems.

## Required-question trace APQ-01–APQ-18

This trace makes the 18 requested planning questions independently auditable. `proposal` means
the answer is canonical for this design baseline but not current runtime/constitutional authority.

| ID | Exact question | Canonical answer/status | Controlling locator | Fixture/falsifier | Lucas seam |
|---|---|---|---|---|---|
| APQ-01 | How does raw owner intent become a draft and then accepted GoalSpec? | authenticate/append exact OwnerIntent; Head compiles a non-authoritative draft; deterministic validation; the authenticated Owner accepts the exact digest (`proposal`) | [Goal compilation](#goal-compilation-and-amendment) | F-M1A-001/002 | ratify acceptance protocol/authentication later[^cslr-d-001] |
| APQ-02 | Who may amend a GoalSpec? | the authenticated Owner; constitution changes also require its own authority path (`proposal`) | [Goal compilation](#goal-compilation-and-amendment) | F-M1A-003/005/020 | Lucas may choose a narrower delegated restriction-only role[^cslr-d-001] |
| APQ-03 | Which restrictions may tighten immediately? | operative constitutional/Owner restrictions and accepted Owner amendment fence new admission/effects immediately; Head/ARM may narrow descendants without editing GoalSpec (`proposal`) | [Goal compilation](#goal-compilation-and-amendment) | F-M1A-003/005/014 | non-Owner GoalSpec amendment authority remains unratified |
| APQ-04 | Which changes require a safe checkpoint or new authorization? | any widening, relaxed privacy, new egress/account/spend/effect, weaker verifier or route power needs new Owner acceptance and checkpoint (`proposal`) | [Goal compilation](#goal-compilation-and-amendment) | F-M1A-003/011/013 | exact future high-impact authorization mechanism |
| APQ-05 | How does an amendment affect plans, nodes, packets, leases, effects and evidence? | new goal epoch; old plan superseded; complete disposition; packets/leases always stale; compatible nodes/evidence need exact proof/reissue; uncertain effects quarantine (`proposal`) | [amendment disposition table](#goal-compilation-and-amendment) | F-M1A-005/006/016/023 | treatment of irreversible effect remains Owner seam |
| APQ-06 | How does an ARM create/revise a DAG without widening the goal? | current leased ARM proposes an immutable full DAG; kernel validates attenuation/graph; revision appends, never edits (`proposal`) | [Plan and DAG admission](#plan-and-dag-admission) | F-M1A-003/004/019/020 | ARM DAG authorship still conflicts with current Hermes-specific decomposition language until constitutional migration |
| APQ-07 | What defines each TaskNode state? | exact proposed/admitted/blocked/ready/running/gate-pending/verified/complete/failed/stale/superseded/cancelled/quarantined guards (`proposal`) | [TaskNode state list](#plan-and-dag-admission) | F-M1A-001/005–010 | none for pure semantics; runtime mapping later |
| APQ-08 | How are cycles, missing dependencies and ownership conflicts rejected? | whole-revision unique-reference/topological/path/effect validation; unordered/unknown conflict refuses atomically (`proposal`) | [DAG admission rules](#plan-and-dag-admission) | F-M1A-004/022 | effect commutativity taxonomy later |
| APQ-09 | How is a WorkPacket deterministically derived? | pure intersection/attenuation over exact goal/plan/task/policy/lease/source/reservation tuple; kernel assigns digest (`proposal`) | [WorkPacket derivation](#deterministic-workpacket-derivation) | F-M1A-003/006/013 | runtime adapters later |
| APQ-10 | How are failed gates returned to the ARM? | immutable exact GateResult/evidence digest; fail/error/missing/ambiguous blocks and becomes retry/replan input (`proposal`) | [Gates](#gates-retry-replan-and-escalation) | F-M1A-007/010/011 | verifier exception tiers require Lucas |
| APQ-11 | What constitutes retry novelty? | one typed, content-diffed new evidence/hypothesis/strategy/capability/narrowing/repair/amendment; rephrase/duplicate refuses (`proposal`) | [retry novelty list](#gates-retry-replan-and-escalation) | F-M1A-007/008 | semantic materiality stays conservative until measured |
| APQ-12 | When must ARM replan, narrow, escalate or stop? | structural/stale/multi-node change replans; within-goal change may narrow; cross-ARM/lifecycle goes Head; authority/policy goes Lucas; exhaustion stops (`proposal`) | [replan/escalation rules](#gates-retry-replan-and-escalation) | F-M1A-008/009/023 | disputed semantic choice goes Lucas |
| APQ-13 | How are route changes admitted without identity/authority loss? | checkpoint/new assignment and packet only; actor/goal/policy/privacy/budget remain exact; no implicit spend (`proposal`) | [quota/route paragraph](#gates-retry-replan-and-escalation) | F-M1A-012/013 | provider-neutral route policy needs constitutional amendment |
| APQ-14 | How are missing verifier, quota, refusal, ambiguity and PAUSE represented? | typed blocking/deferred/refused/quarantined/paused states; none is pass; current PAUSE immediate (`proposal plus current authority`) | [Gates](#gates-retry-replan-and-escalation), [PAUSE](#pause-and-ambiguous-effects) | F-M1A-010–016 | verifier exception and future PAUSE need Lucas |
| APQ-15 | How does replay reproduce the exact accepted plan state? | validate contiguous digest-chained known-version events through a pure reducer; duplicate receipt idempotency; byte-identical canonical digest (`proposal`) | [Replay](#deterministic-replay-and-idempotency) | F-M1A-017/018/021 | durable/cryptographic store later |
| APQ-16 | Which decisions are semantic proposals versus kernel enforced? | Owner/Head/ARM/worker/verifier propose/decide only their semantic roles; kernel enforces every admission/transition/fence/budget/privacy/PAUSE/effect gate (`proposal`) | [Authority table](#authority-and-responsibility) | F-M1A-002/003/019/020 | Head/ARM decomposition seam requires Lucas |
| APQ-17 | What prevents worker/verifier/plugin/harness/stale lease rewriting plan? | closed command types, role/lease/epoch/digest validation and immutable plans/packets; unauthorized command refuses/audits (`proposal`) | [common bindings](#common-immutable-envelope-and-bindings), [Replay](#deterministic-replay-and-idempotency) | F-M1A-006/019/020/021 | runtime authentication implementation later |
| APQ-18 | What is the minimum complete lean baseline? | strict planning schemas/canonicalization, pure validation/DAG/packet/gate/retry/replan/escalation/decider/reducer and sanitized fixtures; no external subsystem (`proposal`) | [Minimum baseline](#minimum-complete-baseline-and-construction-boundary) | all F-M1A-001–023 plus static no-I/O gate | separately authorize M1A construction |

[^cslr-d-001]: product-spec acceptance is CSLR-D-001, not these seams.

## Minimum complete baseline and construction boundary

The minimum privileged planning baseline is strict schemas, canonicalization, cross-record
validation, a pure command decider/event reducer, deterministic WorkPacket derivation, typed gate
and retry/replan/escalation decisions, leases/fences as records, and sanitized fixtures. Semantic
goal/plan/task proposals remain model-authored; every state transition above remains
kernel-enforced.

Provider clients, model sampling, terminals/processes, plugins, SQLite, Git mutation,
publication, scientific adapters, performance optimization and external effects add no proof to
that first slice and are deferred. [M1A-AUTONOMOUS-PLANNING.md](M1A-AUTONOMOUS-PLANNING.md)
defines the exact construction candidate and fixtures; it is not authorized by this document.
