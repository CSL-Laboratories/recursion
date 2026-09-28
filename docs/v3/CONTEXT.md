---
title: Recursion v3 context, cache, and compaction hypothesis
status: hypothesis
last-updated: 2026-09-07
---

# Recursion v3 context, cache, and compaction hypothesis

## Mark 3 comparison: cache validity and endpoint distinctions - 2026-09-07

Compare provider session/prefix affinity, deterministic content-addressed evidence capsules,
and coordinated hybrid compaction. Provider caches optimize processing; canonical context and
current authority determine validity. Cache hits cannot validate a grant, evidence freshness,
subscription debit or cross-provider reuse. An application capsule key binds authorized source,
goal/context/configuration revisions and current authority epoch; recheck authority before
hydration and effects. A preference coalescing stream cannot erase revocation. CMP-010, CMP-016.

Current OpenAI API documentation distinguishes GPT-5.6-and-later minimum-lifetime options from
older retention controls; routing keys do not guarantee a hit. Anthropic documents hierarchical
prefix invalidation and model-dependent effort/thinking changes. Google's current Interactions
endpoint supports implicit caching; explicit named caches belong to the separate generateContent
API. Do not copy a cache flag or billing rule between endpoints or into an OAuth harness without
route evidence. [Dated source bundles](foundation-research/mark2-research/mark3-comparison/evidence.json)
retain the inspected pages; no provider cache experiment ran.

The decisive continuity probe includes changed directives, stale tool/resource results,
compaction, session loss and no-future-work counterfactuals. Count cache writes/reads, every
compactor/refresh/retry/output, verification and human correction, using unknown for unavailable
counters. No model keepalive, paid explicit cache treatment or default TTL is authorized by this
research. Non-droppable references, privacy-scoped artifacts and one authoritative compaction
transition remain requirements independently of cache policy.

Separate reusable evidence bytes from their newly admitted capsule envelope. Record each
transformation's immediate inputs, transformation/schema version, relevant environment/tool
bindings and actual output digest. Content addressing alone cannot detect inconsistent cached
intermediate model outputs. Compare exact dependency-trace validation and Salsa-style unchanged
value backdating against full rebuild. Untracked filesystem/environment inputs, provisional
cycles and semantic-similarity substitutions are outside that guarantee. A cached verification
is historical evidence unless the current acceptance policy explicitly admits reuse. CMP-028.

Classify provider-cache treatments as documented disabled, observed first-write/reuse or unknown.
A new key/session or elapsed minimum TTL does not certify cold state; priming belongs in full
workflow cost. Current OpenAI pages disagree on 50 versus 80 breakpoint lookbacks, so no bound
is adopted. OpenAI cached tokens still count toward API TPM, Claude cache reads usually do not
count toward ITPM with a documented model exception, and Gemini generateContent counts cached
tokens in limits. None of those rules establishes subscription debit. CMP-034.


## Mark 3 continuity through collaborative work — 2026-09-07

OR-038/039 require intent and decision continuity across questionnaire assistance, Head
consultation, autonomous research and collaborative development. Exact accepted answers retain
their provenance outside summaries; proposed wording must not become an accepted instruction
through compaction, quotation or peer advice.

Compare provider prompt caches separately from Recursion-owned evidence/context reuse.
Cross-route reuse and savings need evidence. Stress revoked instructions, changed configuration,
consultation replies, warm-session reuse and compaction/replay gaps together. Count rediscovery,
refresh/write/miss costs, quality loss, repeated explanations and downstream corrections.

Claude's documented conversational classifier boundary may disappear when its source message
is compacted (SRC-V3-224). That is a bounded source-level reason to retain authority outside
conversation; it does not establish every tool or effect is mediated by a deny rule.

## Mark 2 continuity and value — research synthesis 2026-09-07

Keep the non-droppable capsule and one compaction governor per Recursion session, authoritative across the session's route epochs. Assistant memory
adds explicit source/audience/revision/expiry, including revoked instructions and pending
questions/approvals. A transform may preserve reference integrity while losing semantic quality;
deterministic validation and independent task-quality assessment are separate requirements.
A good summary never becomes the only retained copy of its evidence. MKR-005/MKR-019.

Compare native-only, deterministic-only and coordinated hybrid continuation rather than selecting
a universal compaction default. Stable rendered prefixes, tool schemas, model/settings and eligible
breakpoints matter; current OpenAI API docs include model-specific cache write charges as well
as read discounts. They do not establish ChatGPT subscription debit. Claude's query-pipeline
usage includes subagents/compaction but excludes some helper calls; cumulative streaming totals
must not be summed across results. SRC-V3-193/SRC-V3-220.

The proposed ordinary behavior remains no periodic model keepalive. Warm affinity may improve
latency, but compare cold restart, reconstruction and no-future-work before adopting refresh.
Preserve fresh directives even when changing the prefix forfeits a cache hit. LongMemEval supplies
a sourced memory rubric, not a coding or authority benchmark; test knowledge updates, temporal
recall and abstention alongside exact control references (SRC-V3-189).

A handoff reports source/candidate/config/goal versions, last settled operation, uncertain effects,
pending interactions, dirty artifact manifest, adopted/rejected decisions, available evidence and
next bounded action. Count repeated instructions, failed transforms, hydration, verification,
recovery and human corrections in the full workflow. No context-size, token-saving or quality
advantage is established by this campaign.

## Foundation compaction and cache disposition — 2026-09-05

The existing non-droppable capsule and fidelity refusal remain the contract; configurable tiers
and coordinated hybrid compaction remain unproved optimizations. Declare one governor per route
epoch, detect native compaction, rehydrate/check bindings after session loss and reject summaries
that lose Owner directives, goal/plan/lease identity, gates, blockers or evidence. An upstream
session is acceleration state, not canonical recovery authority. FND-009.

Capacity, semantic memory, cache retention and pricing thresholds are distinct. Current API
snapshots and source IDs are in `analysis.foundation.cache-snapshots` of
[evidence.json](foundation-research/evidence.json). Stable full schemas and serialization can help
prefix matching; tool names alone cannot establish a hit. A universal discount or semantic decay
after thirty minutes is unsupported. Native compaction may occur at token thresholds; comparative
claims about Claude, Codex or Hermes remain unmeasured. FND-010.

The proposed default is event-driven continuation without periodic model keepalive. Deterministic
health checks are separate. Require the total-cost counterfactuals in
`experiment.foundation.cache` before selecting refresh/retention. Count every refresh, compactor,
retry and output exactly once; never preserve stale tool results merely for cache statistics.
Opaque reasoning state, exposed text, summaries and usage counters have distinct visibility
contracts. FND-011; `decision.foundation.context-policy` remains pending.

## Two different caches

Provider-side prompt/KV caches are normally scoped to provider, model family, account, endpoint, prefix, or session under rules Recursion may not control. V3 must not promise one physical cache shared by unrelated providers.

Recursion can own a provider-neutral **semantic evidence cache**: stable, content-addressed artifacts representing the goal, repository, decisions, plan, checkpoints, diff, evidence, and blockers. Adapters compile the same artifacts into each provider's preferred context shape. This creates cross-model reuse without pretending server-side caches are portable.

## Canonical context artifacts

Candidate artifact types:

- `OwnerIntentSlice`;
- `GoalBrief`;
- `PlanRevisionProjection`;
- `RepositoryMap`;
- `DecisionSlice`;
- `TaskBrief`;
- `CheckpointSummary`;
- `DiffSummary`;
- `EvidenceBundle`;
- `OpenBlockers`;
- `CapabilitySnapshot`.

Each artifact has a schema version, project/privacy classification, source identifiers, project-scoped content identity, dependencies, creation method, validity boundary, and rehydration locator. A context package references artifacts by an authorized opaque identifier rather than copying them repeatedly. Raw digests remain inside the project's trust domain; cross-domain handles are randomized or keyed so they do not expose equality or become membership oracles.

`PlanRevisionProjection` is a derived context view of the exact canonical `PlanRevision` digest in
[AUTONOMOUS-PLANNING.md](AUTONOMOUS-PLANNING.md), not a second plan object or writable snapshot.

## Execution Context Capsule

The capsule is the standard handoff unit between executives, engines, providers, and nodes. It is a manifest, not necessarily a monolithic prompt.

### Non-droppable control kernel

- logical actor and parent lineage;
- exact GoalSpec version and Owner Intent Ledger references;
- current task and acceptance gate;
- authority, permissions, privacy, egress, and budget bounds;
- exact repository/source/candidate identifiers;
- current checkpoint and open blockers;
- required output/evidence contract.

### Configurable context tiers

- hot working set;
- recent decisions and actions;
- relevant repository evidence;
- historical summaries;
- lazily rehydratable artifacts;
- optional raw session history.

Cross-node delivery sends the manifest and minimum control kernel first, then hydrates only required artifacts after authorization and digest validation.

## Compaction controller

V3 uses a dual-plane adaptive controller:

1. **Native plane:** allow Cursor, Codex, Claude Code, Gemini CLI, OpenCode, or another harness to use its own optimized compaction when the adapter can observe enough state and the owner enables it.
2. **Recursion plane:** maintain model-independent artifacts, non-droppable constraints, checkpoints, and deterministic validation outside the provider session.

Named policies:

- `native`;
- `deterministic-only`;
- `hybrid` (candidate for comparison; no universal default selected);
- `aggressive`;
- `manual`;
- `off`.

This avoids double-summarizing blindly while ensuring provider-native compaction never becomes the sole copy of owner intent or execution state.

## When to compact

Compaction is adaptive. Signals may include:

- provider-reported context usage or warning;
- token estimates with explicit uncertainty;
- semantic phase transition;
- checkpoint boundary;
- declining retrieval relevance;
- repeated context segments;
- expected route/provider switch;
- memory or latency pressure.

The deterministic context governor owns the one authoritative compaction transition for a route
epoch. It chooses whether to reuse the native session, request a route-native transform, start a
fresh session with a capsule, or call a specialist compactor. Native/session and plugin
compactors return candidate artifacts only; they cannot advance the authoritative epoch. The
governor must avoid needless churn that forfeits provider cache affinity.

## Compaction Fidelity Contract

No compacted epoch becomes authoritative unless deterministic validation confirms:

- every active owner directive remains represented by exact reference;
- GoalSpec, permissions, privacy, budgets, gates, source state, decisions, blockers, and next action are present;
- identifiers and digests resolve;
- omitted evidence remains rehydratable;
- contradictions and uncertainty are preserved rather than smoothed away;
- the old epoch remains available according to retention policy until the new one is accepted.

Independent model auditing is configurable for high-risk goals. Hidden chain of thought is neither required nor persisted; recoverable state consists of decisions, evidence, actions, uncertainties, and next steps.

## RouteCapabilityDescriptor

`RouteCapabilityDescriptor` is the canonical schema name for the maintainer-owned context
capability descriptor named in the owner ruling. Other prose labels are descriptive only.

Every engine/provider adapter publishes a maintained, versioned descriptor covering:

- supported context limits and known server-side restrictions;
- native compaction and summarization behavior;
- resumable session semantics;
- cache observability and controllable cache fields;
- stable-prefix constraints;
- tool-output handling;
- attachment/artifact behavior;
- token-accounting fidelity;
- discovery method and evidence timestamp.

Maintainers own signed descriptor updates. Local overrides are allowed and visible. Runtime discovery that conflicts with a descriptor degrades to the conservative bound and alerts the executive. An owner may override a performance or compatibility bound, but no descriptor override can widen GoalSpec authority, privacy, egress, credential visibility, billing, publication, or another constitutional boundary. Descriptor resolution must be zero-token.

## Cache-aware compilation

Provider adapters should maximize same-provider hits where documented by:

- keeping invariant instructions and schemas in stable prefixes;
- ordering stable artifacts before volatile task state when provider semantics reward prefix reuse;
- using canonical serialization and deterministic whitespace;
- referencing unchanged repository artifacts by stable digest;
- isolating volatile timestamps, counters, and route explanations from reusable prefixes;
- preserving provider session affinity when quality and capacity policy permit;
- recording observed cache evidence instead of estimating it as fact.

Exact strategies must be provider-specific and continuously revalidated against official documentation and empirical probes.

## Privacy

Context artifacts inherit the strictest classification of their inputs. Cross-project deduplication is forbidden for private content even when underlying bytes match. Public or cross-project interfaces expose neither raw content digests nor stable size/timing patterns that make equality or membership practical to test. Raw prompts and tool outputs are locally encrypted when retained, never automatically exported, and excluded from public telemetry.

## Required experiments

- Decision/constraint recall after repeated compaction cycles.
- Rehydration completeness after provider and node switches.
- Token and latency savings from stable artifact serialization.
- Native-only versus deterministic-only versus hybrid compaction.
- Session affinity benefits versus fresh-session capsule quality.
- Repository-map invalidation accuracy by commit and configuration digest.
- Cache-hit observability and billing behavior for every supported route.

## Canonical prompt assembly ownership - 2026-09-28

Recursion assembles the canonical prompt from the `ContextCapsule` (invariant instruction/schema
prefix + volatile task suffix). Provider adapters only serialize it into a route's shape; provider
prompt caches remain adapter-local. One compaction governor is authoritative **per Recursion
session** (spanning that session's route epochs), not merely per route epoch; native/session and
plugin compactors return candidate artifacts and never advance the session's authoritative epoch.

## Cross-session prompt-consistency contract - 2026-09-28

Define a content-addressed **compiled-prompt manifest** derived from the capsule and a per-adapter
**compile receipt**. The same `GoalSpec` revision + capsule digest must compile to an equivalent
control prefix on every engine; a divergence is a blocker, not a best-effort conversion. This is
what keeps prompt/compaction clean when the owner switches engine, provider or session.
