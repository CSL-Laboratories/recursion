# Candidate: Recursion month-long delivery control plane

**Answer:** the current foundation issues are the correct starting set, but they need a small program-control layer to remain coherent for the entire month. This is not another product architecture. It is the mechanism that versions decisions, admits work, limits parallelism, integrates slices, and proves what actually shipped.

**Status:** CSL-204 review candidate. It records proposed controls, not accepted product scope or release readiness.

## Program objective

Advance every product pillar in parallel without allowing issue volume, prototype volume or model consensus to substitute for an integrated product result.

The program succeeds only if it can show, for each promised capability:

- the accepted product outcome and human decision receipt;
- the contract revision implemented;
- the exact candidate and qualified environments;
- the evidence tier reached;
- unresolved risks and deferrals;
- integration, recovery and rollback results.

## Program-level gaps to close

### 1. Versioned dependency graph

Every issue declares hard dependencies, soft inputs, contracts consumed and issues blocked by its output. The graph is versioned because a title-level dependency is insufficient: a child depends on an exact accepted contract digest.

**Gate:** no development issue starts with an unresolved hard dependency or an unpinned shared contract.

### 2. Capability and release matrix

Maintain one matrix across native harness, upstream routes, BYOK, local intelligence, configuration, plugins, node orchestration, CLI/TUI/GUI, performance, privacy and release operations. Each row records maturity independently:

1. researched;
2. human-decided;
3. contract-frozen;
4. prototyped;
5. implemented;
6. provider-free verified;
7. locally qualified;
8. release-qualified.

**Gate:** no lower evidence tier is described as a higher one; month-end scope names the required tier for each row.

### 3. Human decision queue

Keep a short queue of consequential decisions with the exact packet digest, options, recommendation, blocked dependants and required human decision-maker. A receipt closes the decision; an agent-generated conclusion does not.

**Gate:** a changed packet digest invalidates stale approval and re-blocks affected dependants.

The queue is an exception path, not the default work queue. Agents proceed under bounded autonomy for reversible engineering choices, collaborate with the active claimant on material but reversible tradeoffs, and escalate only the smallest consequential decision. A pending decision blocks its affected contract lineage, not the entire program.

### 4. Shared contract registry

Index accepted identities, lifecycle states, commands/events, refusals, wire schemas, capability rules, configuration precedence, engine boundary, adapter boundary and compatibility policy. Record owner, digest, consumers and supersession history.

**Gate:** two active issues may not privately redefine the same public or authority-bearing concept.

### 5. Work and review admission

Subscription capacity is not unlimited integration capacity. Admit bounded work when it inherits accepted contracts and has independent-review, CI, shared-file, and integration capacity. Human decisions are admission prerequisites only for work that crosses a consequential decision trigger. Preserve reserve for fixes and recovery.

**Gate:** stop opening implementation branches when active work exceeds review/integration capacity; research and bounded work may continue on non-overlapping scope. Do not close all admission merely because one decision lineage is waiting on a human.

### 6. Rolling integration checkpoints

Each contract family has a temporary integration issue and claimant. This is not permanent pillar ownership. It coordinates exact versions, composed fixtures, shared-file order and acceptance evidence until the checkpoint closes.

**Gate:** individually passing children do not advance capability maturity until composed behavior passes at the checkpoint.

### 7. Evidence and promotion ledger

For every candidate, retain source revision, inputs, environment, commands, raw outputs, expected results, independent rerun and limitations. Separate synthetic fixtures, local observations, live qualification and release evidence.

**Gate:** promotion is tied to an immutable candidate identity; a code or contract change requires a fresh gate.

### 8. Recovery, migration and rollback

Every stateful or compatibility-bearing change declares migration direction, interruption behavior, downgrade/rollback boundary, data preservation, and authority during mixed versions.

**Gate:** no release-qualified claim without tested recovery and rollback, or an explicit human-approved no-rollback boundary.

### 9. Risk and unknown ledger

Track unknowns separately from defects and decisions. Each entry names the issue that can resolve it, the claim it blocks, exposure if deferred, and owner trigger.

**Gate:** unknowns cannot silently become zero, unsupported, safe, passed or out of scope.

### 10. Stop and rescope conditions

Pause affected implementation intake when shared contracts churn, review debt grows, independently verified throughput falls, integration failures repeat without new evidence, or a required human decision cannot keep pace with its blocked lineage. Keep independent bounded lanes moving.

**Gate:** failed work returns evidence. A retry states what changed and why the same failure should not repeat.

## Starting issue set

These issues should begin the program. They remain active as versioned contracts and ledgers throughout the month rather than being discarded after initial closure.

### Start now, in parallel

1. **Program control and anti-drift contract:** ratify issue readiness, human decision receipts, promotion states and change invalidation.
2. **R1 native-harness responsibility boundary:** define what the core owns versus adapters and upstream engines.
3. **F2 month-one product/release contract:** assign a required evidence tier and explicit deferral to every pillar.
4. **Repository/Linear evidence reconciliation:** deduplicate existing work and link current implementation/research to the capability matrix.
5. **OS-specialization research:** complete public research while it cannot yet authorize product divergence.

### Begin when R1 has a candidate contract

6. **F3 public vocabulary and wire contracts:** freeze identity, lifecycle, command/event/refusal and compatibility candidates.
7. **F4 configuration, capability and authority composition:** prove non-widening precedence and authenticated changes.
8. **Engine/runtime comparison parent:** freeze the reconstruction corpus, host profiles, hard floors and comparator identities.
9. **A4 verification/evidence tiers:** make promotion and independent-verification rules executable.

Research and contract review may overlap. Production implementations do not fan out until the shared contract they consume is accepted.

### Fan out after the relevant foundations freeze

- **E1:** upstream/subscription route contracts.
- **E2:** BYOK and credential delegation.
- **E3:** local-intelligence roles and qualification.
- **D1:** enterprise node and remote execution protocol.
- **D2:** shared application/interaction contract for CLI, TUI and GUI.
- **A1:** whole-system performance, resource and specialization evidence.
- **A2:** plugin, supply-chain and edition trust.
- **A3:** privacy, retention, deletion and audit semantics.

Each lane opens matched research, prototype, implementation, testing and integration children as appropriate; it does not create one giant issue that remains permanently in progress.

## Rolling-wave execution

Use readiness checkpoints rather than pretending every lane is synchronized by calendar week.

### C0: Baseline accepted

Repository/Linear map, capability matrix, anti-drift contract and owner queue agree on current state.

### C1: Foundation candidate

R1, product scope, public vocabulary and configuration/authority have reviewable candidate contracts. Research lanes may fan out; production core contracts are not yet claimed as final.

### C2: Foundation receipt

Humans approve exact contract digests. Engine/runtime and OS experiments freeze matched inputs. Downstream implementation issues become ready selectively.

### C3: Vertical integration

Each pillar demonstrates its smallest useful slice against shared fixtures. Cross-pillar checkpoints test identity, authority, cancellation, evidence and recovery composition.

### C4: Qualification

Exact candidates run on declared environments, including failure, resource, security/privacy and long-running behavior. Unknowns remain visible.

### C5: Release decision

The capability matrix states what reached release qualification, what reached a lower tier, what was deferred, and what blocks the next release. Humans decide release/merge/deployment.

## Per-checkpoint controls

At every checkpoint:

1. reconcile changed parent decisions and invalidate stale children;
2. inspect WIP, review debt, CI queue and shared-file contention;
3. integrate exact child revisions and run composed falsifiers;
4. update capability, evidence, risk and decision ledgers;
5. admit the next bounded set of issues;
6. preserve capacity for failures before opening more work.

## Month-end completion criteria

- Every pillar has an explicit maturity tier supported by evidence, not a binary “worked on” status.
- Every consequential product/architecture direction has a human decision receipt or remains visibly blocked.
- Every implemented capability points to an accepted contract digest and exact-candidate verification.
- Native, upstream, local, node and client surfaces compose through shared identity, authority and evidence semantics.
- Performance claims are end-to-end and environment-bounded; OS specialization remains unselected unless its matched evidence and owner receipt justify it.
- State, compatibility and release-bearing changes have recovery/rollback evidence.
- Deferred work is represented by deduplicated dependency-linked issues with named blockers.

## What not to add

Do not create a second broad architecture rewrite, a permanent owner for every pillar, a Deep Research run for every mechanical subtask, or parallel product ports before contracts and experiment inputs freeze. The missing layer is execution control and integration evidence, not more unbounded specification.
