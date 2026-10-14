# R-AUDIT: executable contract corpus and reconstruction acceptance

Retrieval date: 2026-10-02 (UTC). Repository snapshot: **9f4122b50dc8117b185b2cd52244a10df3eb4285**. Scope: static inspection through the authorized private GitHub connector, plus arithmetic over retrieved manifests and file metadata. **No project program, import, test, gate, model, provider call or installation was run. No repository file was edited.** Source copies and this report are research artifacts outside the repository.

## Result for the architecture outline

**The strongest existing M1 behavior baseline is a 71-bundle executable-contract corpus: 23 M1A planning bundles, 24 M1B execution bundles and 24 M1C integrity bundles. It is a valuable regression and specification corpus, but it is not independently qualified reconstruction acceptance or production behavior evidence.** The M1A source still conflicts with its written WorkPacket rule on correlation identity and effect attenuation. The integrated FU-03 acceptance work, a complete independently reviewed literal-byte release, independent A/B reproduction, and native store/OS qualification remain distinct obligations. [AM] [BM] [CM] [KERNEL] [AWIRE] [QUAL] [FUEND]

Suggested replacement outline text, **inferred/proposed**:

> Reconstruction starts from the pinned M1 planning/execution/integrity schemas, pure contract programs and 71 literal fixture bundles, with exact manifest and semantic coverage gates. These historical synthetic/in-memory contracts are regression inputs, not a blanket statement that the current implementation or all current product policies are correct. Known M1A setup/derivation discrepancies must be reconciled through a reviewed amendment and independently valid controls. FU-03 Session A and Stage B provide bounded diagnostic evidence only. Reconstruction acceptance requires a reviewed neutral specification, complete literal bytes/refusals, independent implementations and monitors, integrated-bound/mutant evidence, unchanged plugin/client semantics, and scoped native store/OS/rebuild/migration evidence.

The governance explicitly separates the implementer’s pass from independent acceptance; the anchor requires executable kernel contracts before live code and an evidence-grounded specification. That establishes intended acceptance discipline, not the correctness of any source document or implementation. [GOV] [ANCHOR]

## Evidence classes used

- **SD:** source-documented at the immutable URL and retrieval date. Counts below are source inventory/manifest facts; totals are arithmetic on those declarations, not test observations.
- **SD-H:** a retrieved source records a historical execution or review at a named candidate. The execution was not repeated here.
- **I:** static inference, challenge or proposed correction/method; not a demonstrated runtime failure.
- **U:** unresolved or unmeasured acceptance result. “Not run here” is not failure, success or zero.
- An identifier absent from a bounded inspected source set is **unsupported in that set**; it is not a claim of global nonexistence.

All reference labels resolve to pinned URLs in §9; **each was retrieved 2026-10-02**. The companion source ledger gives full file and blob identities.

## 1. Exact corpus inventory and units

| Area | Literal fixture IDs | Bundles (files) | Internal unit | Supporting manifest |
|---|---|---:|---|---|
| M1A planning | F-M1A-001 through F-M1A-023 | 23 | 37 isolated cases; 267 command records and 267 expected receipts including setup; 253 expected events including setup; 11 expected decode failures; 10 replay expectations | [AM] |
| M1B execution | F-M1B-001 through F-M1B-024 | 24 | One typed input and one expected decision per bundle | [BM] [BOR] |
| M1C integrity | F-M1C-001 through F-M1C-024 | 24 | One typed input and one literal expected decision per bundle | [CM] [COR] |
| Total | Three disjoint families | **71** | Do not call this 71 tests or 71 executions | [AM] [BM] [CM] |

**SD:** M1A’s manifest declares 22 command types, 27 event types, 28 error codes, 25 domain record types, four projection terminal states and 133 schema definitions. Those are separate alphabets, not interchangeable coverage denominators. It declares 5,872,494 total literal-fixture bytes, matching the sum of Git tree size metadata for its 23 fixture blobs. Tree metadata gives 133,854 M1B and 87,474 M1C literal-fixture bytes. These byte-size counts exclude manifests and schemas and are not recomputed file-hash verification. [AM] [TREE]

A “twice” conformance loop does not double the bundle inventory. M1A’s 37 isolated cases are not 37 replay expectations: there are ten replay expectations nested inside those cases. In particular, the later M1B historical compatibility receipt calls the 37 “replay cases”; the current manifest gives the less ambiguous units above. [AC] [AM] [BREC]

### M1A complete bundle inventory

Counts are manifest-declared, **SD**, with arithmetic checked independently. “Commands” includes initial setup and subject commands; the receipt count equals the command count in each row. “Events” includes initial setup and expected emitted events. These are stored records, not distinct primitive operations or executions observed by this audit. Purposes are abbreviated from the oracle blueprint. [AM] [AOR]

| ID | Purpose | Isolated cases | Commands = receipts | Events | Decode failures | Replay expectations |
|---|---|---:|---:|---:|---:|---:|
| F-M1A-001 | Verified completion | 1 | 10 | 10 | 0 | 0 |
| F-M1A-002 | Missing fields/authentication | 1 | 2 | 0 | 9 | 0 |
| F-M1A-003 | Authority/budget widening | 5 | 25 | 20 | 0 | 0 |
| F-M1A-004 | Cycle/missing dependency | 2 | 10 | 8 | 0 | 0 |
| F-M1A-005 | Atomic amendment/disposition | 1 | 16 | 27 | 0 | 1 |
| F-M1A-006 | Stale packet/lease | 3 | 25 | 22 | 0 | 0 |
| F-M1A-007 | Novel bounded retry | 1 | 10 | 11 | 0 | 0 |
| F-M1A-008 | Repeated non-novel retry | 1 | 11 | 11 | 0 | 0 |
| F-M1A-009 | Retry ceiling/escalation | 1 | 15 | 15 | 0 | 0 |
| F-M1A-010 | Missing verifier | 2 | 19 | 17 | 0 | 0 |
| F-M1A-011 | Explicit verifier downgrade | 1 | 11 | 11 | 0 | 0 |
| F-M1A-012 | Capacity exhaustion/route spend | 1 | 10 | 9 | 0 | 0 |
| F-M1A-013 | Checkpointed route replacement | 1 | 10 | 11 | 0 | 1 |
| F-M1A-014 | Current-immediate PAUSE | 1 | 12 | 8 | 0 | 0 |
| F-M1A-015 | Future PAUSE modes inert | 3 | 9 | 6 | 0 | 3 |
| F-M1A-016 | Ambiguous effect fences | 1 | 11 | 10 | 0 | 0 |
| F-M1A-017 | Duplicate/conflicting identity | 1 | 3 | 1 | 0 | 0 |
| F-M1A-018 | Genesis/checkpoint replay corruption | 1 | 8 | 8 | 0 | 5 |
| F-M1A-019 | Executive lease replacement | 1 | 6 | 5 | 0 | 0 |
| F-M1A-020 | Worker/verifier parent mutation | 1 | 6 | 4 | 0 | 0 |
| F-M1A-021 | Unknown field/version/policy | 1 | 3 | 2 | 2 | 0 |
| F-M1A-022 | Resource/effect conflict graphs | 5 | 27 | 30 | 0 | 0 |
| F-M1A-023 | Incomplete amendment | 1 | 8 | 7 | 0 | 0 |
| **Total** | | **37** | **267 each** | **253** | **11** | **10** |

### M1B complete family breakdown

The fixture test explicitly enforces six typed bundle families, **SD**. [BG]

| IDs | Typed family / count | Intended behavior and scope |
|---|---|---|
| 001–008 | RouteFixtureBundle@1: 8 | Eligible subscription route; no unapproved metered fallback; stale/private/unknown-billing/unavailable/capability/component failures |
| 009–014 | PluginFixtureBundle@1: 6 | Acyclic resolution; missing dependency/cycle/conflict/substitution failure; functional acknowledgement does not grant sensitive dimensions |
| 015 | DisabledPluginFixtureBundle@1: 1 | Metadata-only disabled trace with expected zero active counters |
| 016 | PluginReloadFixtureBundle@1: 1 | New epoch fences old packet/lease/subscription |
| 017–019 and 024 | ProcessFixtureBundle@1: 4 | Synthetic Windows admission; unknown/unsupported containment; immediate PAUSE/termination does not determine external effect |
| 020–023 | BrokerFixtureBundle@1: 4 | Attach/detach inertness; stale lease refusal; credit/reset; UTF-8 byte cursor |

Every row is a specification/fixture claim, **not actual plugin, process, containment, billing or broker observation**. Exact per-ID expected outcomes and adversarial obligations are in the blueprint. [BOR]

### M1C complete family breakdown

The fixture test explicitly enforces five typed bundle families, **SD**. [CG]

| IDs | Typed family / count | Intended behavior and scope |
|---|---|---|
| 001–005 | EvidenceFixtureBundle@1: 5 | Scoped structural support, metadata insufficiency, retraction, contradiction retention and export restriction |
| 006–010 | EffectFixtureBundle@1: 5 | Prepared/succeeded/ambiguous/refused classification of supplied bound records; no effect actuated |
| 011–015 | PublicationFixtureBundle@1: 5 | Eligible/blocked/ambiguous review-publication classification; no push or deployment |
| 016–020 | StateFixtureBundle@1: 5 | Synthetic fabric-only inventory, fencing, replay/recovery/ownership-transfer/migration conditions |
| 021–024 | RetentionFixtureBundle@1: 4 | Supplied deletion-proof graph completeness, unresolved copies, key/tombstone issues, unresolved Owner erasure seam |

“Supported”, “succeeded”, “eligible” and “complete” are outputs of typed record classifiers. They do not mean scientific truth, real effect completion, deployed migration or physical deletion occurred. The oracle contract states these limitations explicitly. [COR]

## 2. Programs and where the inventory is enforced

### Executable-contract surface

**SD, file inventory:** There are three pure M1A implementation files:
- scripts/v3/planning/validation/canonical.py: strict decoder, canonical serialization/digests and shape validation.
- scripts/v3/planning/validation/contracts.py: deterministic admission/attenuation/graph policy checks.
- scripts/v3/planning/replay/kernel.py: genesis, admission, reduction, WorkPacket derivation and replay.

M1B and M1C each instead have a test-only reference oracle, scripts/tests/v3/{execution,integrity}/reference_oracle.py. M1B’s static test explicitly forbids a production scripts/v3/execution directory. The tree contains no corresponding M1C production implementation. This is **five behavior-bearing Python files**, with different status, not five production components. [TREE] [AST] [BST] [CST]

There are **17 *.test.py runner files**: seven planning, five execution, five integrity. Each area also has one fixture materializer and one schema materializer, **six generator/check helpers total**, plus one schema.json and one fixture manifest per area. These counts describe files, not unittest methods or adversarial cases. [TREE]

| Area | Exact runner filenames |
|---|---|
| planning (7) | schema.test.py; red_fixture.test.py; validation.test.py; conformance.test.py; property.test.py; adversarial.test.py; static.test.py |
| execution (5) | schema.test.py; fixture.test.py; conformance.test.py; adversarial.test.py; static.test.py |
| integrity (5) | schema.test.py; fixture.test.py; conformance.test.py; adversarial.test.py; static.test.py |

The 17 runner files, two test-only oracles, six helpers and three pure M1A files total **28 Python program/helper files in this defined surface**. This is not the entire repository’s executable surface. [TREE]

### Exact enforcement locations

| Obligation | Static enforcement source and scope |
|---|---|
| M1A closed manifest shape and schema/candidate binding | red_fixture.test.py lines 622–668 checks exact keys, frozen schema candidate, schema SHA-256, fixture_count=23 and schema-alphabet equality. [AG] |
| M1A no missing/extra/reordered ID or JSON fixture file | lines 670–699 checks closed row keys, filename grammar, no symlink/root escape, actual JSON set = manifest file set, and exact ordered F-M1A-001–023 sequence. [AG] |
| M1A exact fixture bytes and quantities | lines 709–750 checks size cap, per-file byte length/SHA-256, bundle identity/schema/digests and declared case/command/receipt/event/decode/replay/final-digest counts. [AG] |
| M1A complete alphabet coverage | lines 752–779 unions observed commands/events/errors/domain arms, then compares exactly to schema registries. This is occurrence coverage, not exhaustive semantic guard coverage. [AG] |
| M1A conformance | conformance.test.py lines 19–75 compares receipts, event sequences, journal, unchanged refusal state, final state/digest, decode errors and replay dispositions/digests. Lines 78–89 runs each case twice from genesis. [AC] |
| M1B exact inventory | fixture.test.py lines 59–78 enforces IDs, bundle IDs, 24 manifest rows, schema digest and per-file bytes/digest/type. Lines 98–109 enforces the 8/6/1/1/4/4 split. Lines 111–116 checks byte-identical materialization. [BG] |
| M1B conformance | conformance.test.py lines 39–53 validates typed inputs/results and canonical expected bytes twice, plus aggregate decision digest equality; lines 55–57 enforces closed status coverage. The printed bundles=24 is not by itself the fixture-set gate. [BC] |
| M1C exact inventory | fixture.test.py lines 60–85 enforces IDs, 5/5/5/5/4 split, 24 manifest rows, schema digest and each fixture identity/path/type/digest. Lines 87–94 checks materialization and absence of direct oracle use. [CG] |
| M1C conformance | conformance.test.py lines 37–45 enforces 24 bundles and twice-reproduced expected canonical bytes; lines 59–78 binds every ID to its expected status. [CC] |
| Static isolation and independence checks | Planning static.test.py restricts imports; M1B forbids production runtime and dangerous imports/calls; M1C bounds oracle imports/surface and prohibits materializer-to-oracle imports. Such structural checks do not establish semantic independence or absence of shared specification error. [AST] [BST] [CST] |

**Important correction, I:** Cite the inventory gate together with the semantic gate. A conformance runner that discovers whatever files happen to be present is not a closed inventory proof by itself. A printed “PASS bundles=24” string is not evidence that it was executed or that an independent reviewer accepted the current candidate. [AC] [BC] [BG] [CG]

## 3. Historical results versus this audit

| Family | Source-recorded historical candidate and result | What remains unproved |
|---|---|---|
| M1A | **SD-H:** fb0e04584a402b659560b64591706beb18082a72; two clean sweeps; 15 schema tests, 37 paths, 267 commands, 253 events, 11 decode failures, 15 adversarial probes, 15 attenuation variants, 10 graph permutations, two pure repeats, 46 DAG candidates; same-family independent review PASS. [AREC] | Current semantic correctness, new execution, cross-family independence, production authentication/durability/effects, reconstruction readiness |
| M1B | **SD-H:** 0f1bd5f9fd4f32acf9718930f67101983216c5a3; two sweeps; 13 schema, eight fixture, two conformance, 44 adversarial and three static tests; 48 cross-union mutations and nine grant dimensions reported; same-family review PASS. [BREC] | Production resolver/loader/broker/process behavior, containment or actual disabled inertness |
| M1C | **SD-H:** 8cd1c8ef574b0e375000d7c68d133404928308cf; two sweeps; 13 schema, nine fixture, four conformance, 21 adversarial and seven static tests; 40 structural mutations, 20 semantic groups, 79 output variants reported; same-family review PASS. [CREC] | Actual evidence retrieval, effect execution/reconciliation, publication, storage, migration or deletion |

These are source-documented historical receipts, **not locally observed results in this audit**. No count above is a newly measured pass count. Subsequent contrary source evidence must remain visible despite the older reviews. [QUAL]

## 4. Independent challenges that must accompany reuse

### C1. WorkPacket correlation identity conflicts with its wire rule

**SD:** kernel.py line 288 sets every helper-produced record correlation_id to CORR-M1A. derive_work_packet calls that helper at lines 347–349. The wire rule requires the derived packet correlation ID to equal its GoalSpec ID at lines 585–586. These are incompatible for a GoalSpec whose ID differs from CORR-M1A. [KERNEL] [AWIRE]

**I:** Do not treat exact equality to the frozen outputs as independent proof of the written packet contract. This is a directly inspectable source discrepancy; it is not a newly executed failing case. The existing Session A qualification memo and diagnostic verifier independently recorded the same limitation. [QUAL] [SAREC]

### C2. WorkPacket effects copy the parent instead of the narrowed task

**SD:** kernel.py line 327 copies goal.payload.allowed_effect_keys; the wire profile requires TaskNode effect fields at lines 571–574. [KERNEL] [AWIRE]

**I:** If a task narrows the goal’s allowed effects, the constructor can represent a broader set than the written derivation rule. This static counterexample family does not claim actual external execution. The qualification memo records an F-M1A-005 stored-data instance and warns that matching stored expected bytes can preserve the same mismatch. [QUAL]

**Amendment proposed, not applied:** independently adjudicate the intended correlation/effect rule, preserve the old bytes and historic receipts, and issue a versioned correction or reviewed PlanRevision with explicit invalidation and migration implications. A valid positive control must be checked against the entire written prefix by an independently authored oracle; its negative must differ in exactly the targeted field. Re-sealing only digests is insufficient if the producer still violates the rule. [QUAL] [AOR]

### C3. “Zero activity” is constructed in the oracle

**SD:** M1B evaluate_disabled sets fifteen active-work counters to zero unconditionally after seal verification. M1C classifiers similarly return fixed zero side-effect counters; its state classifier consumes supplied recovery/digest evidence rather than qualifying a real store. [DISABLED] [STATE] [CC]

**I:** The outline must say these are inert classifier contracts. Before claiming runtime inertness, separately instrument imports, processes, threads/timers/watchers, network, credentials, quota/spend, cache/context/tool/callback/subscription work. A supplied zero or a constant-zero result cannot be an independent observer.

### C4. Frozen PAUSE semantics are narrower than later product scope

**SD:** F-M1A-014 uses current-immediate; F-M1A-015 represents checkpoint/interrupt/drain but refuses selection with E-POLICY-UNKNOWN. M1B gate 13 and M1C gate 16 likewise preserve immediate-only operative behavior. [AOR] [BREQ] [CREQ]

**I:** The unchanged corpus cannot establish implementation of every newer Stop/PAUSE policy. Keep the old profile identified and add a reviewed semantic mapping/new profile and positive/negative cases for current accepted modes. This is a coverage/currency correction, not permission to weaken or silently rewrite frozen evidence.

### C5. Inventory and reviewer identity are not semantic completeness

**SD:** the red runner enforces occurrence of each command/event/error/domain type, and M1C uses source-string/AST checks to limit oracle coupling. [AG] [CG] [CST]

**I:** A guard may be absent or incorrect despite complete alphabets, a clean historical run and separate authors. Explicitly test whether a valid otherwise-identical control reaches the target guard, and preserve refusal precedence so an earlier setup error cannot masquerade as successful fault detection. These are acceptance blockers for the claimed invariant, not reasons to discard unrelated historical evidence.

## 5. FU-03 diagnostics must not replace the integrated bar

### Session A / PR160-era evidence

**SD-H:** The dated independent result for candidate 74fb18dea58361a2d0f51f8aafd194659014cadf records:
- 51 vectors: admission 20, canonical bytes 9, canonical decode 9, digest domains 1, log reduction 10, trace 2.
- 11 instantiated mutants, ten killed, M31 NOT_KILLED_WITHIN_BOUND; 24 NOT_YET_IMPLEMENTED and one NEEDS_LARGER_BOUND, across 36 dispositioned rows.
- The enlarged unfixed RBig exposes NoBlindEffectReplay; RFixed removes that recorded witness; M25 reintroduces it.
- ACCEPT explicitly limited to Session A diagnostic scope. [SAREC] [SAREAD]

**SD/I:** This is useful retained evidence and must not be rewritten as “no model/oracle/independent review exists.” Conversely, “36 dispositioned rows” is not “36 accepted classifications” or “36 kills.” NOT_YET_IMPLEMENTED and NEEDS_LARGER_BOUND do not satisfy the full acceptance vocabulary/obligations. The oracle documents candidate semantic completions and bounded trace assumptions. [FUMUT] [SAOR]

**SD:** Session A README line 67 still says no independent verifier rerun occurred, conflicting with its own dated verification section and the explicit result file. Prefer the exact dated receipt for its limited scope and flag the stale summary sentence for correction. Do not infer a new integrated pass from that correction. [SAREAD] [SAREC]

### Stage B

**SD-H:** Stage B records a 26-fixture synthetic run: 16 bounded_consistent, three unknown, five unsupported, two blocked. Native reset/durable-store SB-21 and real-process/incarnation SB-22 are blocked; takeover, reconnect and other mechanisms remain unsupported/unknown. The README excludes integrated-bound/kernel-freeze and IT-02 acceptance. [SB]

### Integrated acceptance contract and its unresolved status

The FU-03 source says the supplied handoff fixed the bounds, 17 invariant names, primary TLA/TLC and independent Alloy cross-check direction; the detailed equations, missing dimensions, refusal allocation and binary profile are **candidate completions pending reconciliation**. Do not silently upgrade every line of that candidate research to ratified product law. [FUSCOPE]

| Required evidence dimension | Source-documented specification / present limitation |
|---|---|
| Full finite bound | One aggregate/task/effect/timer/verifier; two workers; goal and plan epochs {1,2}; lease epochs {1,2,3}; queue capacity 2; up to 20 committed events. Candidate adds command IDs 1..20, writer epochs and explicit pause/attempt interpretation. Diagnostic reductions have their own identity and cannot satisfy the integrated gate. [FUBOUND] |
| Independent invariant observation | Independent committed history, replay, projection and dispatch observations; the subject reducer cannot be its own monitor. The first authoritative safety run disables reductions; any later reduction needs a soundness argument and unreduced comparison. [FUSCOPE] [FUEND] |
| Mutant metric | Candidate gate: all 36 mandatory rows classified with retained evidence, K=RNE, no reachable non-equivalent survivor and no inconclusive row. Equivalent/unreachable count only in classification denominator. If historical intent requires 36 reachable non-equivalent mutants, substitutions require explicit acceptance; that interpretation is not resolved by relabeling. [FUMUT] |
| Literal byte corpus | Candidate catalog has 88 named rows plus mandatory finite expansions; literal reviewed bytes/preimages/refusals and coverage manifest are release obligations. The 51-vector bounded oracle is not this complete release. [FUVEC] [SAOR] |
| Genuine independent implementations | A bytes/results = B bytes/results = reviewed normative vector for every entry; retain bytes, not only hashes. No shared serializer/decoder registry/reducer/generated source/templates/expected-output generator. Shared hash primitive must be disclosed and independently known-answer tested. [FUAB] |
| Cross-check and trace controls | Scope-aligned independently authored Alloy cases, positive witnesses, seeded faults; trace validator rejects corrupted/missing/hidden/out-of-scope observations with truthful four-way verdicts. [FUEND] |
| Exact-candidate review and decision | Full evidence package and independent acceptance precede any separately authorized freeze/adoption. A baseline pass or source report cannot select a language/store or freeze the kernel. [FUEND] |

**U:** No complete integrated acceptance receipt for this pinned state was established by this audit. The retrieved current reports continue to limit Session A/Stage B to diagnostic/synthetic scope. This is a bounded evidence-status statement, not an assertion that every possible branch or unseen artifact was searched. [SAREAD] [SB] [RLANG]

## 6. Reconstruction criteria to preserve in the corrected outline

The R-LANG report’s RC-01–RC-09 are **I: a proposed operationalization**, not a passed suite or selected stack. Preserve all nine:
1. Immutable specification closure, with unresolved seams resolved or excluded from named claims.
2. Exact A=B=normative bytes/results/refusals.
3. Independent semantic implementation and expected-output provenance.
4. Model bridge, integrated bounds, mutant sensitivity and adversarial trace controls.
5. Native OS/store acknowledged-group, high-water, CAS and crash equivalence.
6. Unchanged external plugin/client artifacts and protocol/grant/disabled/reload behavior.
7. Rebuild and release identity, native/managed dependency closure, core-only boot.
8. Preregistered bounded implementation/review/rework cost, including failed work and clarifications.
9. Safe migration: compatible canonical history, one fenced cutover, constrained rollback and preserved uncertain effects. [RLANG]

No numerical reconstruction-cost ceiling is established in that source; it remains U until experiment admission. The decisive architectural failure is needing unrecorded author knowledge or re-deriving guards/order/bytes/recovery policy to implement the second core. Finite agreement still cannot prove all future ports, unbounded correctness or physical durability. [RLANG]

## 7. Provider-free verification recommendations, falsifiers and competitors

All rows are **I: proposed future work, unexecuted and not authorization to run**. They preserve current decisions and do not select technology.

| Priority / recommendation | Specific acceptance object | Strong competing option | Precise falsifier | Non-proof |
|---|---|---|---|---|
| B1, before outline sign-off: closed correction ledger | Each outline assertion maps to pinned source/range, evidence class, intended-rule versus behavior status; every fixture/decision/gap identifier resolves; all ten A-items have dispositions | Manual independent line-by-line review with the same closed checklist instead of a new validator | One retained assertion relies on a nonexistent identifier, stale superseded premise, unretrieved citation, or historical pass presented as current execution | Source/link completeness is not architectural correctness. Grounding: [GOV] [RLANG] |
| B2, before claiming M1A invariant qualification: whole-prefix semantic controls | Independent full-prefix validation; paired valid/one-fault tests for correlation identity, effect narrowing, binding digests, authority and refusal precedence; old fixtures retained | Manually derived event equations checked by a separate reviewer rather than a second executable oracle | A packet has parent-only effect authority or wrong goal correlation yet is accepted as a whole-contract-valid control; a negative “passes” by failing an earlier unrelated guard | Fixing these defects does not prove all guards or production security. Grounding: [KERNEL] [AWIRE] [QUAL] |
| B3, before corpus release: manifest + semantics + anti-vacuity join | Exact bundle IDs/counts/bytes/schema epochs; complete command/event/error mapping; known incorrect output or deleted bundle causes intended gate failure | One declarative coverage manifest with independently reviewed manual checks rather than multiple procedural runners | A missing/extra/reordered bundle, changed expected status, incorrect byte or targeted guard mutation leaves the claimed gate green | Closed inventories do not establish every reachable behavior. Grounding: [AG] [BG] [CG] |
| B4, before integrated freeze/ranking: reconcile full FU-03 identity | Fixed bounds, every invariant monitor, every registered mutation meaning/disposition, byte expansions, independent cross-check/trace receipts and exact review | Independent event-equation monitors versus independently authored reference-state interpreter, retaining identical required bounds and controls | Subject and monitor accept the same seeded non-equivalent defect; required lease-3/PAUSE/effect witness absent; timeout reduced to a smaller “pass”; undefined mutation counted as killed | Finite model checking is not native durability, effect truth or unbounded proof. Grounding: [FUMUT] [FUEND] |
| B5, before runtime-inertness or current-PAUSE claim: independent observations/new semantic cases | Actual disabled-path tripwires and current mode positive/negative cases tied to explicit profile/epoch; keep historical M1 immediate-only baseline separate | Separate minimal disabled-path boot and observable external supervisor counters instead of instrumentation inside the candidate | One forbidden initialization/side effect occurs, or a new accepted policy is declared covered only by an old refusal fixture/constant-zero oracle | Runtime observation within a window does not prove permanent absence; this requires separately authorized execution. Grounding: [DISABLED] [AOR] [BREQ] [CREQ] |
| B6, before claiming reconstruction success: independent second-team kit | RC-01–09 evidence, no original reducer consultation, unchanged external transcript/fixture corpus, preregistered cost ceiling | Ordinary regression suite plus migration checklist, explicitly compared for missed defects and review cost | Second implementation requires unstated architectural decisions, shares prohibited semantic code, changes plugin authority behavior, diverges on one required byte/refusal, or exceeds agreed cost ceiling | One successful pair is not universal cheap reconstruction or license/security clearance. Grounding: [RLANG] [FUAB] |

**Provider-free outline check method:** A new narrowly scoped static checker or independent manual equivalent should consume the corrected outline and a frozen evidence manifest, not the historical orientation repair allow-list. Its schema should include assertion ID, exact source URL/SHA/range, retrieval date, evidence class, supersession state, acceptance unit, required review, and blocker. It should reject absent IDs, unresolved required rows, duplicate/conflicting definitions, and PASS claims lacking the exact candidate receipt. A separate reviewer tests it using intentionally misspelled IDs, swapped gap names, stale source pins and promoted diagnostic results. No target execution is required to complete that documentation check. Semantic truth still needs independent reading; a regex/link checker cannot supply it. This is the operational form of B1/B3, not a claim a new checker was built or run. [GOV] [RLANG]

## 8. Blockers versus deferrable implementation choices

**Blockers to corrected-outline acceptance now, I:**
- Presenting the 71-bundle baseline, historical passes, 51 diagnostic vectors or 26 synthetic Stage B rows as full reconstruction/integrated qualification.
- Omitting the directly inspectable M1A code/wire mismatches and whole-prefix control limitation.
- Failing to distinguish candidate integrated details and RC-01–09 proposals from accepted outcomes.
- Mixing fixtures, isolated cases, commands, events, replay expectations, mutants and repeated sweeps into one “tests passed” number.
- Claiming current PAUSE/Stop coverage from an immediate-only historical profile. [AM] [QUAL] [SAREAD] [SB] [FUSCOPE] [RLANG]

**Blockers to later behavior/build-readiness claims, U until supplied:**
- Reviewed semantic reconciliation and valid targeted controls.
- Complete accepted byte/canonicalization/digest/refusal profile and literal corpus with exact identities.
- Full integrated-bound/mutant/independent-monitor/cross-check evidence and exact-candidate acceptance.
- Independent A/B equivalence and plugin/client no-language-leak evidence.
- Claimed native durability/containment/receiver-fence/recovery behavior qualified on the exact profile.
- Preregistered reconstruction cost and release/migration criteria. [QUAL] [FUEND] [RLANG]

**Deferrable choices, I:** private data structures, scheduler/lock/channel strategy, module layout, compiler/build/package tooling, parser and store implementation, renderer, plugin-host mechanism and transport may remain open while invariant semantics and required observation surfaces are written. A choice ceases to be deferrable for a specific accepted build/claim when it determines bytes, authority, crash acknowledgement, FFI leakage, current-mode semantics or the native qualification profile. This deferral selects none of those technologies. [RLANG]

## 9. Source register

All sources below: retrieved **2026-10-02**, evidence class **SD** for source content. Historical runs are explicitly labeled **SD-H** only where discussed. The source itself may contain candidate or historical statements; citation does not promote their authority.

Readable source groups: [governance][GOV]; [planning manifest][AM], [execution manifest][BM], [integrity manifest][CM]; [planning inventory gate][AG], [execution inventory gate][BG], [integrity inventory gate][CG]; [M1A derivation source][KERNEL] and [wire requirement][AWIRE]; [fixture qualification][QUAL]; [historical M1A][AREC], [M1B][BREC], [M1C][CREC] receipts; [Session A dated verifier result][SAREC]; [Stage B bounded results][SB]; [integrated obligations][FUEND]; [reconstruction proposal][RLANG]. Full metadata is in source-ledger.json. The complete Git tree response was nontruncated (1,703 entries); inventory claims are confined to the named paths.

[GOV]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/CONTROL_PLANE.md#L1-L77
[ANCHOR]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/ANCHOR.md#L24-L58
[AOR]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1A-FIXTURE-ORACLES.md#L23-L223
[BOR]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-FIXTURE-ORACLES.md#L9-L77
[COR]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1C-FIXTURE-ORACLES.md#L9-L104
[AM]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/planning/fixtures/manifest.json#L1-L690
[BM]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/execution/fixtures/manifest.json#L1-L174
[CM]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/integrity/fixtures/manifest.json#L1-L174
[AG]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/planning/red_fixture.test.py#L622-L805
[BG]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/execution/fixture.test.py#L52-L142
[CG]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/integrity/fixture.test.py#L47-L141
[AC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/planning/conformance.test.py#L19-L97
[BC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/execution/conformance.test.py#L34-L63
[CC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/integrity/conformance.test.py#L31-L100
[AST]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/planning/static.test.py#L52-L75
[BST]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/execution/static.test.py#L27-L62
[CST]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/integrity/static.test.py#L34-L122
[KERNEL]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/v3/planning/replay/kernel.py#L282-L350
[AWIRE]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1A-WIRE-PROFILE.md#L567-L594
[QUAL]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/FIXTURE-QUALIFICATION.md#L1-L22
[DISABLED]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/execution/reference_oracle.py#L385-L390
[STATE]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/integrity/reference_oracle.py#L664-L794
[AREC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1A-CONSTRUCTION-CAMPAIGN.md#L605-L630
[BREC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-CONSTRUCTION-CAMPAIGN.md#L228-L260
[CREC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1C-CONSTRUCTION-CAMPAIGN.md#L242-L278
[AREQ]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1A-CONSTRUCTION-CAMPAIGN.md#L68-L86
[BREQ]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-CONSTRUCTION-CAMPAIGN.md#L88-L105
[CREQ]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1C-CONSTRUCTION-CAMPAIGN.md#L189-L207
[SAREAD]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/README.md#L1-L69
[SAREC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/verification/results/74fb18d-muse-spark-1-3.md#L6-L67
[SAOR]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/oracle/README.md#L34-L100
[SB]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-stage-b/README.md#L88-L156
[FUSCOPE]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L16-L35
[FUBOUND]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L76-L105
[FUMUT]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L251-L310
[FUVEC]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L438-L448
[FUAB]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L655-L663
[FUEND]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L695-L722
[RLANG]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/core-language-control-surfaces-20261002/rlang/report.md#L203-L244
[PAUSE]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1A-FIXTURE-ORACLES.md#L199-L200
[TREE]: https://api.github.com/repos/NotSkittlz/recursion/git/trees/9f4122b50dc8117b185b2cd52244a10df3eb4285?recursive=1

## Completion and scope

The requested executable-program/bundle inventory, exact units, enforcement locations, known correctness challenges, reconstruction criteria, diagnostic/integrated distinction, acceptance blockers and provider-free method are present. A1–A10 and language-specific feasibility are handled by companion audit lanes; this report does not silently claim their completion.

No new repository behavior, test pass/fail, provider result or build readiness was observed. No technology was selected, protected file altered, live-host action performed or legal conclusion drawn. Report content uses source links and synthetic identifiers; it does not reproduce private credentials or local personal filesystem paths. Source copies remain private research inputs.
