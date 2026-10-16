# Candidate: Recursion month-long delivery control plane

**Answer:** the current foundation issues are the correct starting set, but they need a small program-control layer to remain coherent for the entire month. This is not another product architecture. It is the mechanism that versions decisions, admits work, limits parallelism, integrates slices, and proves what actually shipped.

**Status:** CSL-204 review candidate. It records proposed controls, not accepted product scope or release readiness.

Schema version 1 requires the program-level `status` to remain `candidate`.
Individual contract or capability receipts do not ratify the program. A future
program-ratification path requires a separate owner-approved schema/policy change;
this validator does not infer that authority.

Every work item explicitly declares `hard_dependencies` and `consumes_contracts`
as arrays and `consequential` as a boolean, including research/intake items.
Every checkpoint explicitly declares its `requires` contract array. Empty arrays
declare no such links; omitted declarations are invalid. Approved proposal packets
explicitly include `contract_bindings`, `unknown_bindings` and `checkpoint_bindings`
arrays, including empty arrays when that authorization has no corresponding links.
Proof fields remain conditional: completed work must provide verification evidence,
and qualification/release claims must provide their applicable profiles and proofs.

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

`month_target` is an aspiration, not an authority ceiling. Current maturity may exceed
it only when the actual tier's cumulative authority, contract, unknown and evidence
gates pass. This grants no new work, release, deployment or spending authority.

### 3. Human decision queue

Keep a short queue of consequential decisions with the exact packet digest, options, recommendation, blocked dependants and required human decision-maker. A receipt closes the decision; an agent-generated conclusion does not.

**Gate:** a changed packet digest invalidates stale approval and re-blocks affected dependants.

The queue is an exception path, not the default work queue. Agents proceed under bounded autonomy for reversible engineering choices, collaborate with the active claimant on material but reversible tradeoffs, and escalate only the smallest consequential decision. A pending decision blocks its affected contract lineage, not the entire program.

Product decisions, recommendation acceptance into a normative product contract, and continuation of a research lane beyond its approved budget always enter this queue. For research continuation, record the completed pass, remaining uncertainty, expected additional Parallel quota class, stop condition, and developer approval before another pass begins. Approval is a human-authored comment on the matching Linear issue, pinned by comment identity and proposal digest; labels and status changes only route the queue.

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

## Offline ledger bindings

The checker validates declarations and their consistency; it cannot authenticate a
Linear comment or independently establish that an external artifact passed. Reviewers
must inspect the pinned human comment and rerun the candidate. A passing ledger is
never a substitute for those actions.

All top-level record collections, including `research` and `risks`, must be arrays
of objects. `research_control` and `admission` must be objects. Malformed containers
and maturity selectors fail validation before lookup or traversal.

- Every capability explicitly declares `research_ids`. At `researched` and every
  higher tier it must link a current, successful structured research record for its
  exact capability ID and declared nonempty `research_question`. The record retains
  `source_class`, `source_commit`, `artifact`, `artifact_sha256`, `limitations`,
  current `contract_bindings`, and `continuation: false`. A run URL alone or a
  completed label is not sufficient. Research records have unique IDs and status
  `unverified`, `current`, or `superseded`; unverified provenance is retained but
  cannot support maturity. Capability approval packets pin `research_bindings`
  (`id`, `record_digest` of the complete research record), so changing the retained
  basis requires renewed review. These are declarations for corroboration, not
  proof that an external research service or source was inspected by the checker.

- `proposals` retains named approval packets. Each packet has `id`, `linear_issue`, `scope`,
  `contract_bindings` (`id`, `revision`, `sha256`) and `authorizations` (`action`,
  `target`, `target_digest`). The supported actions are `accept_contract`,
  `develop_work`, `decide_capability`, `admit_work`, and `accept_no_rollback`; the target is the exact
  contract, issue, capability, or program ID. Scope contains every authorized target.
  The receipt and proposal must name the matching work issue or the explicit
  `decision_issue` on the contract, capability or admission envelope.
- An approved decision pins `proposal_id` and `proposal_digest` to the complete
  current packet. Digests use SHA-256 of UTF-8 JSON with sorted keys, ASCII escaping,
  compact separators, and no NaN (`digest` in the checker). Array order is significant.
  The packet's contract bindings must exactly cover the contracts of its targets
  and their required checkpoints. Capability packets also pin `unknown_bindings`
  (the linked unknown definitions excluding status/resolution evidence) and
  `checkpoint_bindings` (`id`, `definition_digest`, hashing all checkpoint fields
  except status/evidence links).
  Packets explicitly include `research_bindings`, empty when no capability research
  is authorized by the packet. Freshness applies to packets referenced by approved decisions. Superseded/pending
  decisions and historical packets remain stored without granting authority.
- `target_digest` pins the target's current fields, excluding only lifecycle fields:
  contract status/receipt; work status/stage/receipts/candidate/verification links;
  capability maturity/target, evidence links, receipts and candidate commit;
  admission receipt/occupancy. Unknown links and checkpoint requirements remain bound.
  All other fields remain bound. Candidate changes require fresh verification;
  contract or scope changes require a new human receipt. Updating a receipt digest
  without a new human approval comment is forbidden, even if structurally valid.
- All issue references use exact `CSL-N` strings, including candidate routing and
  retained history. The syntax pass covers program `parent_issue`/`implementation_issue`,
  work IDs/parents/dependencies, contract/capability/admission `decision_issue`,
  proposal/receipt `linear_issue`, unknown `resolution_issue`, checkpoint `issue`
  and work inputs, admission occupancy, evidence `work_item_id`/`work_bindings`,
  proposal unknown bindings, and `develop_work` authorization targets. Malformed
  reference lists and binding objects fail validation before reference traversal.
  Candidate routing fields and proposal `linear_issue` are required; pending or
  historical receipts may omit an undecided `linear_issue`, but present values must
  be valid. Approved receipts always require the matching issue.
- Capability `contract_ids`, `unknown_ids`, `evidence_ids` and `research_ids` are required arrays
  of nonempty string IDs. The same array shape applies to recorded consumed-contract,
  verification/recovery/rollback evidence, and checkpoint contract/evidence references.
  Objects keyed by IDs, strings and null are not arrays; their implicit iteration
  never substitutes for the declared shape. Empty arrays remain subject to each
  maturity or completion gate's actual evidence/contract requirements.
- The implementation issue identifies an existing work row with the program's
  `parent_issue`. Hard dependencies, occupancy and checkpoint work inputs refer to
  local work rows; active proofs and grants must match their current work targets.
  External parent, decision, resolution and checkpoint-coordinator issues need not
  be work rows in this ledger. Valid historical references may retain obsolete
  targets/digests; they grant no current authority. Mixed proposal scopes and other
  resource IDs retain their own namespaces. Syntax and local cross-links do not
  prove that an issue exists in Linear; reviewers must corroborate the issue links.
- Accepted contracts reference their `decision_receipt`. Developing consumers pin
  the same receipt in `contract_receipts`, keyed by contract ID. Human-gated work
  also requires its own applicable `develop_work` receipt, regardless of the legacy
  `consequential` flag. At `human_decided` and above, a capability needs an applicable
  `decide_capability` receipt. At `contract_frozen` and above its nonempty linked
  contracts must all be accepted. These checks accumulate at higher tiers.
- `active_contract_consumers` and `active_reviews` list unique existing work IDs.
  In-progress development-or-later consumers and verification work cannot be omitted.
  Completed, inactive or wrong-lane list entries are rejected, not counted as capacity.
  Open admission, and occupied bounded admission, require positive consumer/review
  caps plus an `admit_work` receipt binding the actual mode, limits and policy.
  Each list must fit its cap. Empty bounded admission may retain null limits and a
  pending receipt; it admits no contract consumers/reviews until that envelope is
  approved. Routine non-consuming candidate work and unrelated research continue;
  no per-task receipt is added for routine work inside an approved envelope.
- Unknowns retain `status: open` or `resolved`; resolution requires
  `resolution_evidence`. Each linked open unknown blocks its named tier and every
  later tier. Removing an unknown link or weakening its blocking definition
  invalidates the prior capability approval; resolving the retained unknown does not.
- Every work item declares `independent_verification_required: true`. Completion
  (`done`, `accepted`, `verified`) requires retained `verification_evidence_ids`
  pointing to successful, current, independent `work_verification` records. Each
  record binds `work_item_id`, full `candidate_commit`, `work_digest`, the exact
  `acceptance_command`, current consumed `contract_bindings`, artifact SHA-256 and
  environment. `work_digest` hashes an object with `work` (all work fields except
  status, candidate commit and verification links) and `dependency_bindings` (the
  transitive hard-dependency closure, sorted by ID, with each dependency's `id`,
  `candidate_commit`, and `boundary_digest` of the same non-lifecycle fields).
  An upstream candidate or boundary change invalidates dependent and composed proof.
  Development and completed work require every hard
  dependency to be complete with this proof, including research prerequisites.
  Dependency cycles are invalid; a completion label alone never unblocks work.
- Every capability lists `checkpoint_requirements` as `{checkpoint_id,
  required_from_tier}` entries. An empty list is an explicit scope decision bound
  by the capability receipt. The contract does not prescribe the tier for each
  checkpoint: the seed's null threshold remains an owner decision and blocks
  `prototyped` or higher claims until resolved. It does not prevent research or
  contract review. At and above a configured threshold, the checkpoint must be
  closed, its required contracts accepted, and all its `work_item_ids` independently
  verified. Its `evidence_ids` must retain a successful, current, independent
  `composed_checkpoint` record binding `checkpoint_id`, `checkpoint_digest` (the
  definition digest above), capability, exact candidate and claimed tier, acceptance
  command, artifact SHA-256, environment, and the union of capability/checkpoint
  contract bindings. Exact `work_bindings` (`id`, `candidate_commit`, `work_digest`)
  pin the child inputs. Individually passing children and a closed label cannot
  substitute for the composed run. Changing the requirement or definition needs
  a new applicable approval; changing child candidates needs fresh composed proof.
- At `prototyped` and above, successful current evidence must name the exact
  `capability_id`, full `candidate_commit`, `tier`, current `contract_bindings`,
  artifact and `artifact_sha256`, and environment. Kinds are `prototype`,
  `implementation`, `provider_free`, `local_qualification`, `release_qualification`
  respectively. Evidence must match the claimed tier, not merely a lower tier.
  At `provider_free_verified` and above, `independent` must be boolean true.
  These records attest the claimed tier's cumulative checks; they do not authorize
  live runs. Historical/candidate evidence may stay linked but cannot promote.
  Every such proof also records a nonempty `acceptance_command` matching the claimed
  tier in the capability's `acceptance_commands` map. That map is bound by capability
  approval; changing both the command and evidence cannot inherit an old receipt.
  The seed leaves this map null pending selection of actual per-tier gates. Work,
  checkpoint and recovery/rollback proofs likewise match their declared commands.
- Qualified claims also require capability-approval-bound `qualification_requirements`
  keyed by tier. Each profile contains `evidence_class` and a structured `environment`
  with nonempty `id`, `execution_class`, and full `configuration_sha256`.
  `locally_qualified` requires `local_observation` / `local_runtime`;
  `release_qualified` requires `release_evidence` / `release_runtime`.
  Evidence must exactly match its approved class and environment profile. The same
  requirement applies to qualified composed checkpoint and recovery/rollback proof.
  A synthetic or provider-free fixture is not runtime qualification merely because
  its kind or environment label changes. Real local qualification may run without
  providers; these classes do not require or authorize paid/live calls. The seed's
  profiles remain null pending a decision about actual qualification environments.
- Release claims require approval-bound `release_requirements`: a nonempty
  `recovery_command`, plus `rollback_command` or a stated `no_rollback_boundary`.
  The release row binds their full `release_requirements_digest` and links
  `recovery_evidence_ids` and `rollback_evidence_ids`. Each successful current
  independent proof has kind `recovery` or `rollback` and binds the same capability,
  exact candidate, release tier, requirements digest, matching command, current
  contracts, qualified environment/class, and artifact digest. A command or plan
  alone is not a tested result. Requirements left null block release qualification.
- A nonempty `no_rollback_boundary` requires the capability's `no_rollback_receipt`
  to authorize the separate `accept_no_rollback` action on that capability. Its
  target digest pins capability ID, candidate commit, contract IDs, qualification
  profiles and release requirements; the packet also pins current contract revisions
  and digests. A generic capability approval cannot grant this exception. A changed
  candidate, profile, contract or boundary invalidates it. This waives rollback
  proof only; recovery proof remains mandatory. Any broader exemption needs an
  explicit owner policy and is not inferred here. No exception is present in the seed.
- Research requirements always include `question`, `source_class`, `quota_class`,
  `stop_condition`, and `evidence_artifact`. Any recorded `research_plan` must
  supply them as nonempty strings plus an explicit boolean `continuation`, and
  active/completed research work cannot omit its plan. Issue authors
  must use the budget and discretion prompts in the
  issue template. The checker does not read or authorize external research jobs.
- This candidate schema has no accepted research-continuation authorization action
  or mapping from its separate research receipt to an executable grant. Therefore
  `continuation: true`, a continuation packet/receipt, or any continuation-packet
  field on a work item or its plan blocks validation even with a generic work
  receipt. `continuation: false` declares only an initial pass; it does not itself
  approve research. A human must define/accept the exact pass, scope, quota, matching
  Linear comment and next-stop receipt mapping before continuation can be represented
  as authorized. The checker cannot infer undeclared continuation from free prose.

The seed stays candidate/unresearched: its historical research-run URL is retained
as unverified provenance because no linked result or content digest is retained here.
It has open unknowns, pending research/command/checkpoint/qualification/release requirements, pending acceptance and
empty admission occupancy. No accepted receipt or successful verification is
invented to make the example pass.
