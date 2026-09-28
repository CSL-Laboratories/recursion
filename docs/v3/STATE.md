---
title: Recursion v3 state and persistence hypothesis
status: hypothesis
last-updated: 2026-09-03
---

# Recursion v3 state and persistence hypothesis

## Canonical model and unselected persistence

V3 has one canonical event/projection boundary and content-addressed immutable artifacts. The
physical event store is deliberately unselected: append-only canonical JSON/JSON Sequence plus
CAS and single-writer SQLite WAL metadata plus the same CAS are the required competitors.
Human-readable YAML/JSON remains configuration and import/export; Markdown remains documentation
and rendered status. They do not compete as live task truth.

Persistence remains a hypothesis outside M1A. SQLite's official isolation contract permits
only one writer per database at a time; that concurrency primitive does not identify an
authorized Recursion writer, fence another store/process or reconcile an external effect. Those
are application contracts. M1A uses in-memory replay and performs no SQLite migration or write.

The design target is one daemon per authority domain/OS account, not one supervisor process per checked-out Recursion fork.

## Write path

```text
input
  -> authenticate principal or component
  -> validate schema and authority
  -> enter the selected store's atomic append boundary
  -> append canonical immutable event/frame
  -> update or checkpoint disposable projections
  -> enqueue idempotent action/outbox item
  -> flush/commit according to the pinned durability profile
```

Only deterministic controller code writes canonical state. For each aggregate, exactly one fenced writer admits an event at a monotonically increasing sequence; cross-aggregate operations use an explicit saga/outbox boundary rather than a second writer. Every accepted event records the authenticated principal or component, authority/GoalSpec version, command id, fencing token where applicable, causation, correlation, schema version, sequence, and integrity/authenticity material. Hash chaining alone is not authentication. Models propose typed decisions. Remote nodes submit signed results that the controller validates and records.

Planning command/event/lease/effect semantics have one canonical definition in
[AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md); this document owns the later persistence
projection only.

## Event and projection split

Durable lineage events include:

- intent recorded, clarified, or superseded;
- GoalSpec drafted, accepted, or amended;
- executive/ARM created, leased, checkpointed, replaced, blocked, or closed;
- task created, admitted, assigned, started, checkpointed, verified, integrated, or cancelled;
- route resolved, rejected, failed over, or exhausted;
- budget reserved, consumed, released, or denied;
- node enrolled, attested, quarantined, revoked, disconnected, or reconciled;
- evidence accepted or invalidated;
- publication proposed, admitted, published, or rejected.

Ephemeral operational events—progress ticks, streaming deltas, health samples, UI presence—use a bounded live channel or sampled tables and are not forced into the permanent lineage log unless they affect a decision.

Projections serve queues, status, budgets, lineage, routing history, artifact reachability, branch inventory, and observer interfaces. Every projection must be rebuildable from durable events plus immutable artifacts.

## Exactly-once effect posture

Distributed side effects cannot be assumed exactly once. V3 instead requires:

- idempotency keys;
- prepare/execute/reconcile states;
- leases with expiry and fencing tokens;
- authenticated result receipts;
- exact repository and source bindings;
- replay-safe actuators;
- ambiguity recovery that inspects external state before repeating an effect.

Each actuator must publish and test a crash matrix for failure before prepare, after prepare, during the external call, after the external effect but before receipt, and after receipt but before projection. "Exactly once" is not an acceptance claim; the required property is that duplicate, omitted, and ambiguous outcomes are detected, bounded, and reconciled without unauthorized repetition.

The event contract for the first rehearsal includes aggregate type/id, contiguous sequence,
event and command ids, idempotency key, causation/correlation ids, writer id, monotonic fence
epoch, logical/controller time, previous/current event digests, schema version and typed payload.
The lease projection carries task/assignment/lease/attempt identities, status, recovery state,
expiry, reservation, result digest, source digest and last event sequence/digest. Exact duplicate
commands return the prior outcome; conflicting duplicates and stale epochs fail without a new
event.

The required import/replay progression is:

```text
UNIMPORTED -> SNAPSHOTTED -> VALIDATED -> LEGACY_WRITERS_QUIESCED
 -> FENCE_ESTABLISHED -> IMPORTED -> REPLAYED -> SHADOW_READ_VERIFIED
 -> V3_OWNER_ACTIVE -> CHECKPOINTED
```

Malformed, conflicting, torn, unauthenticated or stale input becomes `REFUSED` or
`RECOVERY_REQUIRED`. Projection parity is not replay parity: equal rendered state does not prove
equal event order, crash behavior or writer exclusion.

## Artifact store

Large prompts, transcripts, repository maps, patches, logs, gate output, and context capsules live outside hot relational rows. The database stores project-scoped identity, digest, type, size, classification, owner project, encryption status, dependencies, lifecycle state, and locator. Raw digests and exact sizes are not exposed across project or public observer boundaries.

Storage classes:

1. active control/checkpoint state;
2. acceptance and audit evidence;
3. retained raw session material;
4. rebuildable repository indexes and caches;
5. temporary downloads and streams.

When free-space floors are crossed, v3 removes expired temporary and rebuildable data first, then freezes new admissions before threatening canonical state or acceptance evidence.

## Ratified retention direction with open proof mechanics

The retained defaults, still subject to per-project configuration and legal policy, are:

- successful raw sessions: 30 days;
- failed or disputed raw sessions: 90 days;
- compact metadata, lineage, decisions, receipts, and accepted evidence: indefinite or project-policy governed;
- explicit pinning for important sessions;
- project-specific shorter or longer policies within legal and storage constraints.

The ratified design direction separates raw content from durable proof:

1. Encrypt raw private artifacts with project- or retention-class-scoped data-encryption keys.
2. On expiry, delete the artifact and its scoped key, then propagate physical deletion through
   caches, replicas, backups and disconnected-node spools according to a bounded policy.
3. Append a minimal signed tombstone recording artifact class, governing policy/version,
   deletion request and completion time, responsible component and disposition. Do not retain
   recoverable content or unnecessary equality-revealing digests merely to prove deletion.
4. Retain the smallest lineage, decision, gate and publication receipts needed to explain what
   happened, subject to project policy and legal obligations.
5. Support explicit owner purge, pin/legal hold, deletion retry, incomplete-propagation status
   and evidence that a disconnected copy has not yet acknowledged deletion.

Cryptographic erasure is defense in depth, not proof that every copy disappeared. Content
addressing, deduplication and shared keys can defeat deletion isolation if scoped incorrectly.
Recursion must not report deletion complete until every recoverable copy is verified deleted or
has reached a recorded backup-expiry boundary. The next research campaign must specify proof
states for key granularity, backup expiry, tombstone disclosure, disconnected nodes and legal
holds; it may refine mechanics but must not reopen the owner-selected direction as model opinion.

Deletion is a proof-state machine rather than a boolean:

```text
requested -> local-content-deleted -> key-destroyed -> propagation-pending
 -> verified-complete | partial | blocked | legal-hold | expiry-bound
```

Completion requires either evidence from every recoverable copy or a recorded backup-expiry
boundary. A tombstone records policy/version, artifact class, responsible component, request and
disposition times and non-sensitive copy dispositions. It must not retain raw content or an
unnecessarily equality-revealing global digest. Shared keys, deduplication, delete markers,
soft-delete states, replicas, provider backups and disconnected spools are separate negative
fixtures. NIST SP 800-88 cryptographic erase guidance informs media sanitization but does not
prove that every logical/provider copy is gone.

## Performance posture

- One bounded writer path; many read-only projections.
- WAL checkpointing based on measured write volume and disk behavior. Every result pins the exact
  SQLite build/version, compile options, VFS, filesystem, `synchronous` and checkpoint settings;
  the upstream WAL-reset bug through 3.51.2 (fixed in 3.51.3 and named backports) is an explicit
  revalidation/fault-test trigger, not an inferred deployment fact.
- Prepared statements and batch event ingestion where ordering permits.
- No eager ORM object graph or plugin-specific columns in core tables.
- Bounded indexes justified by actual observer/routing queries.
- Incremental artifact garbage collection from reachability roots.
- Debounced telemetry writes and event-driven scheduling.
- Plugins communicate through typed interfaces rather than opening the database directly.

## Migration

V2 Markdown, JSON, and JSONL state should be imported through versioned adapters with:

- source file digest and commit;
- deterministic identity mapping;
- duplicate and conflict reports;
- dry-run projection comparison;
- reversible backup;
- replay and crash injection;
- refusal to fabricate missing or malformed state.

For a period, v3 may render legacy-compatible read-only views. Dual writable sources of truth are forbidden.

The evidence-backed v2 source map and single-writer strangler sequence are recorded in [CURRENT-AND-MIGRATION.md](CURRENT-AND-MIGRATION.md). Migration must prove at each stage that exactly one path owns assignment, state transition, publication, and each external effect.

Current source falsifies a broad “execution-attempt/lease is already one aggregate” claim.
Task lifecycle, assignment persistence, run evidence, accepted-result ledger and fabric journal
have separate writers, `work.sh` retains a direct task-state fallback, and no common monotonic
fence epoch spans them. The safe first construction target is therefore a **fabric-only state
contract** using `DurableFabricStore` with immutable assignment input and no task/run/result,
remote or publication writes. It is not a local cutover. The full owner-selected rehearsal
remains blocked until its aggregate boundary, complete writer allowlist and fencing protocol are
machine-checkable.

## Backup and recovery

Required proof includes:

- consistent selected-event-store plus artifact snapshot;
- encrypted offsite option;
- restore onto a fresh host;
- key and credential references excluded or separately provisioned;
- migration forward and rollback where schema permits;
- simulated torn write, disk full, process death, duplicate delivery, and stale lease;
- reconciliation of remote-node spools after controller recovery.

## Open questions

- Which v2 records belong in canonical event history versus import evidence?
- Does JSONL/JSON Sequence plus CAS or SQLite WAL metadata plus CAS best satisfy the complete
  replay, recovery, contention and maintenance profile for each authority domain?
- How should artifact encryption keys rotate without breaking content identity?
- Which projections must be transactionally synchronous and which may be eventually rebuilt?
- What measured event volume and database size should define the VPS acceptance envelope?

## Session aggregate - 2026-09-28

A `RecursionSession` (`SessionId`) is a projection over the canonical event ledger plus the
artifact index; there is **no separate session store and no second writer**. Session lifecycle is
represented as canonical events: created, checkpointed, compacted, route-switched, forked,
resumed, closed. Raw transcript remains a retention-class artifact, and the `ContextCapsule` is the
durable carrier across engines and nodes.
