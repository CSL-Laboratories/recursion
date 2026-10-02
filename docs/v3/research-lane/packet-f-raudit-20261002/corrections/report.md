# R-AUDIT architecture correction register, A1–A10

Research retrieval date: **2026-10-02 UTC**. Report scope: static correction research for the candidate D-039 architecture outline; not RPLAN or ROPS.

## Executive finding

The outline needs correction before it can serve as a building-phase behavioral baseline. The important defects are attribution, overstatement of authority or verification, and missing boundary/status distinctions. Some premises of the correction brief are themselves too strong:

- **F20-A…F20-G exists**, in R-SC Q20, not FU-03. Correct the provenance and unexecuted-candidate status rather than delete a real vector family.
- **O1 really is three-way parity**: two store finalists and a reviewed normative corpus. The shared artifact store is not a third competing canonical store.
- **Nine is not the total number of M1B grant fields.** The prose names nine separately represented permission dimensions *in addition to functional capability and workspace scope*. The exact wire object has eleven independent grant dimensions, plus its type discriminator.
- R-V really proposes deterministic explanation over an authenticated identified cut. The mistake is presenting that research proposal as an already defined, adopted and implemented observability contract.
- Current source records contain limited independent reruns. Neither “everything is unrun” nor “the gates pass” is an adequate present-state summary.

These are **source-documented source-content findings**, with their exact supporting sources below. None proves implementation safety.

## 1. Pins, scope and evidence discipline

### Distinct source identities

1. **Audited candidate:** [PR 161](https://github.com/NotSkittlz/recursion/pull/161), still open at retrieval, head **c7c5b1e87ff8de9e9e2c1d0aa3bb7026dfb938e5**, base recorded by PR metadata **050936e05f5523da0eb8dc2f2fa3a65e4798af26**. The [outline](https://github.com/NotSkittlz/recursion/blob/c7c5b1e87ff8de9e9e2c1d0aa3bb7026dfb938e5/docs/v3/architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md) has Git blob **a4b0bdbbd1813ecee8454059e38afa8468c21304**. This report does not pretend the outline is on current master.
2. **Correction-evidence baseline:** current master observed through the GitHub connector at **9f4122b50dc8117b185b2cd52244a10df3eb4285**, [merge PR 163 commit](https://github.com/NotSkittlz/recursion/commit/9f4122b50dc8117b185b2cd52244a10df3eb4285), committed 2026-10-02T21:19:40Z. Its recursive tree reports `truncated=false`. All repository citations below use this full immutable SHA unless explicitly identified as the PR head.
3. **Independent architecture-review limitation:** the supplied R-AUDIT brief summarizes A1–A10 and attributes NEEDS REVISIONS to GLM-5.3-Flash. At retrieval, [PR161 comments API](https://api.github.com/repos/NotSkittlz/recursion/issues/161/comments), [reviews API](https://api.github.com/repos/NotSkittlz/recursion/pulls/161/reviews), and the connector's merged timeline supplied no review text; the PR changed-file list contains only the outline and README. The full independent review was not located in the inspected source tree. Therefore its exact text and complete recommendations remain **unknown**, not independently checked. The brief is the source for what was disputed; repository sources decide whether each dispute holds.

**Execution boundary:** only authorized read-only connector acquisition, text inspection and self-authored research artifacts were used. No repository code, gate, test, model checker or provider was run. No install, store, account, native host, protected path, repository edit, technology selection or legal conclusion occurred. Source snapshots are private research copies outside the repository, not new accepted project files.

### Claim classification

- **SD / source-documented:** a retrieved source states the fact at the cited immutable URL. This is not proof that the contract is correct or the implementation satisfies it.
- **SD-H:** a special case of SD: the source records a historical execution or independent review. No such run was repeated here.
- **LO / locally observed:** acquisition and static artifact facts directly inspected here. It never means local runtime behavior unless explicitly tested; none was.
- **I / inferred:** reasoning, potential counterexample, recommendation or replacement wording. Proposed wording is an editorial proposal, not ratification.
- **U / unknown:** unresolved status or unavailable evidence. It is not zero.
- **Unsupported identifier:** the claimed identifier has no supporting definition in the stated inspected source set. This is narrower than proving global nonexistence.

Each suggested replacement below is **I/proposed**, supported by its SD references. Each includes a genuine conforming alternative, falsifier and explicit non-proof.

### Authority is not correctness

[AGENTS](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/AGENTS.md#L36-L41) says `docs/v3` is non-normative until ratified. [CONTROL_PLANE](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/CONTROL_PLANE.md#L16-L28) establishes current precedence and reserves Owner authority; it does not make all specifications mathematically or empirically correct. [D-039](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L480-L498) accepts the OS/language packet as *research evidence* and commissions an architecture plan. It does not ratify every research contract or select a stack. Thus the outline's blanket “accepted specs under docs/v3” should become “Owner decisions, frozen experimental/construction profiles, design baselines and candidate research, each labeled by its own authority and evidence status.” No authority is changed by this report.

## 2. Correction register

### A1 — Crash-cut, receiver-fence and permission artifact identifiers

**Disputed:** Facet B calls its evidence “FU-03 crash-cut/receiver-fence fixtures (F20-A–G).” The brief suggests no such identifier is known.

**Correct facts, SD:**
- R-SC **Q20** actually defines **F20-A through F20-G**, seven synthetic receiver-barrier vectors. They cover stale work after durable fence install; pre-install unknown exposure and lost ACK; unresolved external effect after installation; regressed floor after restart; unauthorized/wrong-scope installation; check-to-mutation race; and positive/current plus stale controls. The source explicitly says all synthetic vectors are unexecuted and “accepted candidate” means a recommendation inside the report, not Owner adoption. [R-SC status](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Shared_Contract_Reconciliation.md#L5-L9); [Q20 and F20-A–G](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Shared_Contract_Reconciliation.md#L791-L824)
- FU-02 specifies **P00–P19** cutpoints and **F01–F42** fault cells. Its **F20** is segment rotation, not the R-SC F20 letter family. F13 includes permission removal/read-only transition; F28–F29 writer-fence/takeover races; F30–F32 duplicate/conflict/lost-response cases. [FU-02 matrix](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-02-store-crash-replay.md#L189-L267)
- FU-03's mathematical prefix names are **G, A, B, F, T, P, Q, L, V, E, I, U, N**. G is genesis; A Start; B successful Submit; F failed Submit; T retry timer; P Pause; Q Resume; L lease replacement; V changed worker; E PrepareEffect; I MarkDispatch; U Unknown effect; N KnownNoEffect plus failed submit/timer. They are not a fixture family called F20-A–G. The proposed corpus has 88 named rows plus mandatory expansions. [FU-03 §10](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L438-L454)
- Stage B is **SB-01…SB-26**, a separate provider-free synthetic matrix. Its design file retains `unexecuted` because results live separately; its README and exact-candidate verifier record describe an executed bounded run. Those layers must not be collapsed. [matrix](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-stage-b/fixtures.json); [README](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-stage-b/README.md#L1-L16)

**Suggested replacement:**
> Reconstruction evidence names distinct namespaces: R-SC Q20 F20-A…F20-G are proposed, unexecuted receiver-fence/permission vectors; FU-02 defines P00…P19 cutpoints and F01…F42 store fault cells; FU-03 defines its named semantic prefixes and byte/refusal catalog; Stage B defines SB-01…SB-26 with separately recorded bounded synthetic outcomes. None of these alone qualifies a production receiver or durability profile.

**Build impact:** blocker for evidence traceability and for any fence/permission safety claim; correcting names requires no mechanism choice. Namespace aliases can be editorially deferred only if an explicit one-to-one source map remains.
**Conforming competitor:** preserve source-local IDs with a namespaced crosswalk rather than renumbering them into one universal family.
**Falsifier:** any referenced ID fails exact lookup, maps to different semantics, or an unexecuted vector is used as an observed pass.
**Non-proof:** identifier accuracy does not show adequate schedules, independent observations, physical durability or real receiver enforcement.

### A2 — Orientation gate versus current reconstruction evidence

**Disputed:** Facet F asserts `tools/check_v3_orientation.py --contracts` passes.

**Correct facts, SD + static I:**
- The program calls itself a provider-free gate **for the 2026-09-13 orientation repair, not v3 release**. It fixes baseline **797be4f18edf3f9e0f2dbac9432f190dbe1ed969** and a nine-path allow-list. Before optional contract runs, it refuses any changed/untracked path outside that list; checks document routing/links, claim/Owner-decision registries and exact fixture family IDs. [script](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/tools/check_v3_orientation.py#L1-L21); [scope/structure order](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/tools/check_v3_orientation.py#L110-L156)
- Its `--contracts` branch composes **23 invocations**: six schema/fixture-materializer checks plus 17 test runners, and hard-asserts that count. [run_contracts](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/tools/check_v3_orientation.py#L87-L107)
- The read-only [baseline-to-current compare](https://github.com/NotSkittlz/recursion/compare/797be4f18edf3f9e0f2dbac9432f190dbe1ed969...9f4122b50dc8117b185b2cd52244a10df3eb4285) reports current master 173 commits ahead and, among other changes, modified ANCHOR and CONTROL_PLANE, which are outside ALLOWED. Therefore **a clean checkout of this pinned master would fail the orientation scope predicate before reaching its contract checks**. This is static inference from code plus diff metadata, **not a newly observed failing run**.
- Its final label is literally `V3_ORIENTATION_PASS_NOT_RELEASE_ACCEPTANCE`. Even a historical green is not generic release or reconstruction acceptance.

**Suggested replacement:**
> Reconstruction evidence is the exact pinned M1 schema/fixture inventory and its separately identified semantic, adversarial and static checks, plus scoped FU-03 evidence and dated independent-verifier receipts. Cite candidate SHA, command/check identity, environment and receipt for each claimed historical result. The 2026-09-13 orientation-repair gate is historical; its fixed scope allow-list does not admit the present tree and is not a reconstruction or release gate.

**Build impact:** blocker for honest build-readiness reporting; the corpus can be inventoried now without running or weakening the old gate.
**Conforming competitor:** a new versioned aggregate verifier may call unchanged component checks, or each component can be independently attested and joined by an exact-candidate manifest.
**Falsifier:** aggregate success omits a required inventory/semantic check, reports an unrun current pass, or silently expands an old allow-list and cites it as unchanged historical verification.
**Non-proof:** a closed command inventory proves coverage membership, not test correctness, nonvacuity, current product-policy support or production readiness.

### A3 — G-20 is performance; configuration is G-09

**Disputed:** §7 calls G-20 “configuration lifecycle.”

**Correct facts, SD:** **G-20** is the unrun v2-vs-Options benchmark, R17 ablation and PERF-003 0/10/100 disabled-plugin matrix; Option 2's benefit and benchmark manifest are unverified. **G-09** covers configuration architecture: no cross-platform conformance corpus, no selected human syntax and no agreed privacy-exposure/publication authority lattices. [GAPS G-09](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/GAPS.md#L34-L42); [G-20](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/GAPS.md#L57-L66)

**Suggested replacement:**
> Open gaps include G-09 configuration conformance, human syntax and privacy/publication authority ordering; G-20 performance comparison, R17 ablation and disabled-plugin benchmark evidence. Configuration revision/lifecycle ownership must be explicit, but it is not the registered meaning of G-20.

**Build impact:** correct source mapping before task decomposition; conformance and authority ordering block the affected configuration claim. Human syntax and optimization ranking can remain bounded choices if the canonical semantics are explicit.
**Conforming competitor:** retain separate narrow G-09/G-20 cross-references, or add an architecture-owned configuration-seam entry linked to G-09 without repurposing the gap ID.
**Falsifier:** any claim labeled G-20 is closed by configuration evidence while its performance obligations are still missing.
**Non-proof:** a correct gap ID does not settle configuration design or demonstrate a performance gain.

### A4 — Authority dimensions: nine additional, eleven wire fields

**Disputed:** Facet B says “six authority dimensions”; brief/review asks for nine.

**Correct facts, SD:**
- M1B separates functional capability and workspace scope from **credential, network, model-launch, route/account, quota, spend, update, downgrade and grant-administration**, nine additional dimensions. A deny remains in its explanation; manifest/edition/signature/ack/dependency cannot widen GoalSpec or policy authority. [execution contract](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-EXECUTION-CONTRACTS.md#L51-L70)
- The exact `PluginGrantDimensions@1` wire fields are:
  1. functional_capabilities
  2. workspace_paths
  3. network_destinations
  4. credential_refs
  5. model_refs
  6. route_account_refs
  7. quota_units
  8. spend_microunits
  9. update
  10. downgrade
  11. grant_admin
  The `type` discriminator is not a twelfth grant dimension. Each field is explicitly independent. [M1B wire](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-WIRE-PROFILE.md#L123-L126); [machine schema, $defs.PluginGrantDimensions](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/schemas/control-plane/v3/execution/schema.json)
- PLUGINS repeats the nine permissions outside the limited functional/workspace acknowledgement. [permission separation](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/PLUGINS-AND-EDITIONS.md#L341-L345)
- The constitutional amendment packet is marked proposal-not-operative and contains now-stale Hermes/immediate-PAUSE framing. It is not a basis to reinstate retired governance or erase D-022. [proposal status](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md#L1-L14); [current control plane](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/CONTROL_PLANE.md#L9-L14)

**Suggested replacement:**
> Effective authority is an intersection of current, independently bounded grants. M1B represents functional capability and workspace scope plus nine separately represented permission dimensions: credentials, network, model launch, route/account, quota, spend, update, downgrade and grant administration. Its PluginGrantDimensions@1 wire record therefore has eleven grant dimensions; no true/nonempty dimension implies another.

**Build impact:** blocker for permission representation, policy projection and negative testing. Do not implement a six- or nine-field approximation while describing it as exact M1B parity.
**Conforming competitor:** a differently organized internal permission product can conform if its explicit bijection preserves all eleven independent denials/bounds and exact boundary bytes.
**Falsifier:** enabling one permission implicitly enables another, any dimension disappears in a round trip, or field/type counts are confused.
**Non-proof:** separate schema fields do not prove current checks at the irreversible boundary or runtime isolation. Nine is not an exhaustive count of every high-impact product permission outside this M1B profile.

### A5 — Unsupported `rlc-candidate/0`; real interface-platform direction

**Disputed:** Facet D identifies a unified interface candidate as `rlc-candidate/0`.

**Correct facts, SD/unsupported:** Exact connector search for that string returned no definition on the pinned default branch; none occurs in the retrieved R-L report, interface strategy or inspected declared schema identities. This is **unsupported in the inspected sources**, not proof it could never exist on another branch. The real direction is the **R-L Shared CLI, TUI and GUI interface platform**: one admitted command meaning, snapshot/audience-bound query meaning, semantic catalog, typed errors/receipts and projections; it is explicitly a specification candidate. Its examples and appendix include `CommandIntent`, `TypedError`, `interface-contract.schema.json`, and operation `core.message.enqueue@candidate-0`. Those do not define a universal platform identifier `rlc-candidate/0`. [R-L status/findings](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rl/report.md#L1-L32); [envelope/catalog](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rl/report.md#L49-L65); [interface strategy](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/INTERFACE-STRATEGY.md#L1-L28)

**Suggested replacement:**
> Unified interface-platform direction: the R-L shared command/query catalog and snapshot-bound projections, with typed receipts/errors and surface-equivalent meaning. This remains a specification candidate; no platform-wide schema identifier, renderer, toolkit or transport is selected here. Remove the unsupported identifier rlc-candidate/0.

**Build impact:** source/schema lookup blocker, not evidence that interface planning must wait for a renderer.
**Conforming competitor:** independently authored per-surface codecs against the same semantic oracle instead of generated codecs from one catalog.
**Falsifier:** equivalent commands differ in target/authority/arguments/refusal, or a schema identifier resolves to no versioned definition.
**Non-proof:** a catalog or schema cannot prove runtime parity, accessibility, trust or protocol qualification.

### A6 — Observability statement has a candidate source, not adopted-contract status

**Disputed:** Facet F presents “observability as a deterministic query over an authenticated cut” as a defined contract.

**Correct facts, SD:** R-V's first finding says explanation is a deterministic query over an **identified, authenticated canonical cut plus retained command/receipt/input evidence**. Its status is research candidate, and it explicitly labels all proposed product behavior inferred, unimplemented and unexecuted. The nearest named artifacts are `SourceCut`, `ExplainResult`, `DecisionRecord`, `ProjectionManifest` and `ReplayDescriptor` in `rv-candidate-contracts-1`. SourceCut joins event sequence/digest, separate receipt-journal revision/digest and artifact manifest, without inventing a global order for independent domains. [R-V findings/status](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rv/report.md#L1-L34); [candidate artifact set](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rv/report.md#L126-L178); [candidate-contracts](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rv/candidate-contracts.json)

The existing TELEMETRY document calls itself a hypothesis, requires provenance, missing-counter honesty and local/minimal telemetry; it is not an adopted general deterministic-query calculus. [TELEMETRY](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/TELEMETRY.md#L1-L53)

**Suggested replacement:**
> Proposed R-V observability boundary: model-free explanation is a deterministic query over an identified, authenticated event/receipt/artifact cut and retained historical inputs. Recorded facts, observations, hypotheses and unknowns remain distinct; current read rights gate disclosure. Replay and historical decision re-derivation do not execute effects. This is an inferred research contract awaiting adoption and qualification.

**Build impact:** blocker before claiming authenticated explanation/replay or exposing private history; implementation choices can wait while cut, authority and completeness obligations are fixed.
**Conforming competitor:** stored typed decision certificates independently re-derived versus complete immutable input snapshots evaluated at query time, both preserving the same operands/cuts and missing-evidence labels.
**Falsifier:** explanation depends on fresh model/tool execution, hides a missing input, treats a digest as authentication, invents receipt completeness, or discloses before current authorization.
**Non-proof:** deterministic reproduction can reproduce an originally wrong decision. Authentication of individual records does not prove inventory completeness or external truth.

### A7 — Two store finalists, shared CAS, five oracles; retain three-way O1

**Disputed:** “FU-02 three-way replay parity (O1).”

**Correct facts, SD:** FU-02 explicitly states O1 is **three-way**: finalist A = finalist B = independently reviewed normative corpus. Thus the phrase is not itself false. The ambiguity is what the three sides are and whether any test ran. The two finalists are **JS1 segmented RFC 7464 JSON Text Sequences plus CAS** and **SQL1 SQLite WAL plus the identical CAS**. CAS is a controlled shared component with common-mode risk, not a third finalist. “JSONL” is imprecise here: JS1 specifies record-separator + JSON text + LF framing. [finalists](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/synthesis/FU-02-store-crash-replay.md#L23-L37); [shared CAS](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-02-store-crash-replay.md#L85-L93); [JS1](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-02-store-crash-replay.md#L129-L155)

Exact oracle set:
- **O1:** ordered event identities, exact bytes/group boundaries, chain/schema/command/authority, replayed state at each committed group, and projection version/high water/digest, against independent normative expectations.
- **O2:** stale-writer exclusion including reacquired lock and takeover/mutation race.
- **O3:** exact duplicate receipt reuse, changed-byte COMMAND_CONFLICT, uncertainty reconciliation; no new mutation/effect.
- **O4:** artifact existence/length/digest at the same cut, with GC/backup pins.
- **O5:** engine/framing/metadata/recovery integrity and repeatability. Database integrity checks are diagnostic, not replacements for O1–O4.
[O1–O5](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-02-store-crash-replay.md#L159-L187)

**Suggested replacement:**
> FU-02 proposes two canonical-store finalists, JS1 RFC 7464 segmented JSON sequences and SQL1 SQLite WAL, both using the same CAS. O1 compares each finalist with the other and an independently reviewed normative corpus; O2–O5 cover stale writers, duplicate/conflicting commands, artifact closure and recovery integrity. These proposed comparisons are unrun/unqualified; no store is selected.

**Build impact:** blocks store qualification and ranking, not language-neutral semantic design. Storage mechanism is deferrable; commit/fence/ACK/artifact-closure semantics are not.
**Conforming competitor:** either finalist or another explicitly admitted candidate satisfying the same canonical contract; no candidate supplies its own expected output.
**Falsifier:** both implementations agree but differ from the normative corpus; acknowledged group/artifact is lost; stale mutation commits; receipt reuse creates a new effect.
**Non-proof:** parity does not prove the shared corpus, reducer or CAS is correct. A process-kill pass is not native reset/power-loss evidence.

### A8 — Plugin/edition source and stable schema, not D-035 alone

**Disputed:** cross-cutting #12, §5 and reconstruction criterion #5 cite “plugin contract (D-035).”

**Correct facts, SD:** D-035 is the repository topology principle: plugins external to core and referenced by digest; core remains in one R&D repository; actual split not authorized. It motivates cross-repository versionability, but is not the plugin semantic/schema specification. [D-035](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L384-L405)

The source artifact is **PLUGINS-AND-EDITIONS.md**, especially capability graph, lifecycle/distribution and editions; its profile/edition claims retain their own proposed/accepted statuses. The exact existing inert construction schema is **PluginManifest@1**, plus **PluginGrantDimensions@1**, **PluginGrantEnvelope@1**, **PluginPolicyEnvelope@1**, **PluginLifecycleRecord@1** and resolver records, specified in M1B-EXECUTION-CONTRACTS and M1B-WIRE-PROFILE and materialized at the execution schema. It includes protocol/artifact/dependency-lock identity, capability/dependency/conflict sets, subscriptions, configuration digest, isolation, requested grants, resource envelope, provenance/license/update source and self-digest omission. [plugin/edition artifact](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/PLUGINS-AND-EDITIONS.md#L196-L345); [M1B contract](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-EXECUTION-CONTRACTS.md#L51-L95); [wire fields](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-WIRE-PROFILE.md#L123-L178); [schema](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/schemas/control-plane/v3/execution/schema.json)

The R-A schema named `urn:example:recursion:plugin-manifest:proposal-0` explicitly says research design only and not accepted runtime schema. It must not be confused with M1B PluginManifest@1 or silently replace it. [proposal-0 schema](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/plugin-manifest.schema.json#L1-L6)

**Suggested replacement:**
> Preserve the semantic plugin/edition boundary documented in PLUGINS-AND-EDITIONS and the exact pinned M1B PluginManifest@1/grant/lifecycle/resolution profile. D-035 supplies the external-artifact repository-topology principle. R-A/P1 portability amendments remain separately versioned proposals; host mechanisms may vary only if they preserve the adopted boundary and pass its independent conformance tests.

**Build impact:** blocker before publishing an SDK/plugin host claimed stable; selecting a process/WASM/FFI/IPC host is deferrable here.
**Conforming competitor:** distinct host mechanisms behind an identical versioned boundary and loss/refusal contract.
**Falsifier:** language-native object layouts/exceptions/ownership or unversioned host callbacks become required portable plugin semantics, or an edition/manifest implies authority.
**Non-proof:** a stable schema does not prove sandboxing, cheap portability, safe loading or lifecycle settlement.

### A9 — Desktop floors are resolved policy; mobile floors remain open

**Disputed:** §7 calls D-028 “resolved platform floors” without a desktop limitation.

**Correct facts, SD:** D-028 fixes Windows and Linux/backend filesystem/VM-reset profiles and WSL development/test disposition. It does not supply an iOS or Android floor. It keeps each profile unqualified until traces pass. [D-028](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L240-L269)

Mobile intake explicitly leaves **Android minimum runtime API and target API separately**, supported profiles, and **iOS OS/device classes** open. iOS report calls for supported iPhone/iPad and OS floors; Android report separates runtime and target SDK and lists relevant behavior boundaries as test axes, not chosen minimums. [mobile intake](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ios-android-20261002/INTAKE-SYNTHESIS.md#L52-L87); [iOS decisions](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ios-android-20261002/ios/report.md#L310-L314); [Android floor](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ios-android-20261002/android/report.md#L295-L310)

D-036 already resolves online-only private view; older mobile statements that offline policy is undecided are superseded on that point, not on device/API floors. [D-036](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L410-L435)

**Suggested replacement:**
> D-028 resolves the declared desktop/backend Windows/Linux and storage-fault support policy, while native qualification remains profile-specific. Android minimum runtime API, target API and supported device profiles, and iOS minimum OS/supported iPhone/iPad profiles remain open. D-036 governs private offline content; D-038 keeps mobile/GUI built but opt-in.

**Build impact:** mobile qualification/release claims blocked until floors are named. Core semantic reconstruction need not wait; mobile-specific mechanism assumptions must remain conditional.
**Conforming competitor:** narrower or wider explicitly justified mobile support matrices, each carrying its own required behavior tests.
**Falsifier:** a required mechanism cannot work at the chosen floor, target-SDK behavior is omitted, or desktop support is cited as mobile qualification.
**Non-proof:** a policy floor or emulator pass is not native-device security, performance, energy or lifecycle qualification.

### A10 — Mobile fixture namespaces require reconciliation, not double counting

**Disputed:** Facet D lists MOB-F01…F24 and MP01…MP11 as if already one ready reconstruction corpus.

**Correct facts, SD:** D-039 explicitly requires those namespaces to reconcile into one suite. Intake clarifies one **semantic oracle**, while per-platform stimuli may differ; it does not establish one byte-identical event corpus. Android concrete ClientCapabilityProfile@candidate-2 / UiMaterializationManifest@candidate-2 instances are absent in that intake and need registration before implementation. [D-039](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L480-L498); [intake reconciliation](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ios-android-20261002/INTAKE-SYNTHESIS.md#L52-L61)

**Suggested replacement:**
> Mobile reconstruction evidence is a pending reconciled suite: map R-MOB MOB-F01…F24 onto the shared iOS/Android MP01…MP11 obligations, preserve non-overlapping requirements and platform-specific stimuli, and evaluate them with one independently defined semantic oracle. Do not add 24 and 11 to claim 35 distinct or executed cases. Register concrete per-platform candidate-2 profiles and explicit missing coverage.

**Build impact:** blocker before claiming cross-client parity or a complete mobile reconstruction corpus; namespace display choices are deferrable after exact traceability exists.
**Conforming competitor:** one canonical suite with aliases versus a federation of platform suites joined by a common obligation matrix and oracle.
**Falsifier:** an obligation disappears in the crosswalk, duplicate aliases inflate coverage, conflicting expected outcomes remain, or platform-specific setup supplies a different command meaning.
**Non-proof:** a reconciled synthetic suite does not prove OS scheduling, real storage, authentication, notifications, energy or backend enforcement.

## 3. Candidate/proposed hedges and supplemental corrections

### H1. Command key is a production integration candidate

**SD:** R-SC says `(admitted_authority_domain, command_id)`, with principal/auth reference and effect-relevant meaning inside immutable content/digest, is a proposal for Owner adoption. “Accepted candidate” does not mean ratified. [R-SC status](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Shared_Contract_Reconciliation.md#L5-L9); [command-identity proposal](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/synthesis/MODEL-ARCHITECTURE.md#L155-L161)

**Replacement, I:** “Proposed production command key: (admitted_authority_domain, command_id); immutable content_digest is its equality/conflict predicate. Historical M1A identity remains unchanged unless separately amended.”

**Competitor:** explicit principal-scoped ingress IDs with a durable injective map into canonical IDs.
**Falsifier:** same canonical ID with changed principal/meaning silently reuses acceptance, or revoked read rights disclose receipt existence.
**Non-proof:** deduplication does not give external exactly-once effects or fresh authority.

### H2. Seven capability facts are not a frozen enum or universal state machine

**SD:** the Prompt/Plugin/Skill report identifies itself as research candidate and says installed/enabled/authorized/available/selected/loaded/executed are independent scoped dimensions, not seven steps in one universal lifecycle. [status](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Prompt_Plugin_Skill_Architecture.md#L1-L9); [seven dimensions](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Prompt_Plugin_Skill_Architecture.md#L124-L142)

**Replacement, I:** “Candidate capability-state projection keeps seven independent facts, each with scope/evidence; field names and wire representation are not frozen here. It is distinct from M1B's PluginLifecycleRecord@1 lifecycle and from client support/render disposition.”

**Competitor:** separately typed per-capability facts rather than one product record.
**Falsifier:** rendering implies loaded/executed, enabled implies authorized, or one mutually exclusive enum cannot represent a valid combination.
**Non-proof:** describing independent facts does not establish discovery completeness, lifecycle control or disabled inertness.

### H3. Other synthesis invariants need source-status labels

**SD/I:** R-SC's exact four-field ArtifactRef, eighteen-field ContentEnvelope, one loss record, exposure-intent ordering, typed identity joins and resource algebra are recommended integration candidates, even where they preserve inherited fields. A heading “must never vary” cannot ratify all of them. Preserve the exact frozen profile where its construction contract requires it; separately identify the proposed broader production invariant and adoption dependency. [R-SC status](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Shared_Contract_Reconciliation.md#L5-L22)

**Competitor:** version-specific adapters with an explicit semantic join map.
**Falsifier:** field aliases silently merge different authority domains or a research recommendation is treated as current permission.
**Non-proof:** compatible candidate schemas are not independently correct implementations.

### S1. Replace blanket “all unrun” and “no independent rerun”

**SD-H:** an exact-candidate Session A verifier receipt for **74fb18dea58361a2d0f51f8aafd194659014cadf** records bounded diagnostic acceptance; a Stage B verifier receipt for **d56cc441b0efa964ed8eb515626a1c836192ca69** records 26 synthetic fixtures: **16 bounded_consistent, 3 unknown, 5 unsupported, 2 blocked**. These receipts refute a global no-rerun/no-execution sentence. They do not supply integrated acceptance, native store qualification or production behavior. [Session A receipt](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/verification/results/74fb18d-muse-spark-1-3.md); [Stage B receipt](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-stage-b/verification/results/d56cc44-muse-spark-1-3.md#L1-L54)

**Replacement, I:** “Execution status is artifact-specific. Bounded Session A and Stage B synthetic evidence and independent diagnostic reruns exist at named candidates; full integrated FU-03, two-store comparison, native platform qualification and unresolved route/runtime suites remain unqualified or unrun as individually recorded. No result from this static audit upgrades them.”

**Conforming competitor:** per-artifact evidence ledger versus exact-candidate receipt links embedded at each claim. **Falsifier:** any blanket status contradicts a scoped recorded result or promotes a diagnostic pass into integrated acceptance. **Non-proof:** a historical review receipt is not a new run or proof of its reviewers’ conclusions.

### S2. Serialization profile versus implementation serializer

**SD:** M1B fixes UTF-8 I-JSON, duplicate/invalid UTF-8/float/negative-zero rejection, JSON Schema 2020-12, RFC8785 canonicalization and exact digest syntax. [M1B encoding](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-WIRE-PROFILE.md#L9-L35)
**Replacement, I:** “No production language, store or serializer implementation is selected. Existing frozen M1 byte profiles remain binding within their scope; any new production profile must be separately named, versioned, independently vectored and reconciled.”
This replaces Facet C's overbroad “no serialization selected.”

**Conforming competitor:** independent serializers satisfying the same fixed byte profile, or an explicitly separately versioned new profile with reviewed mappings. **Falsifier:** a library’s default encoding changes bytes/types while being labeled the same profile. **Non-proof:** known-answer conformance does not prove compiler correctness or arbitrary-input coverage.

### S3. Frozen historical PAUSE cannot establish all current Stop policies

**SD:** M1B still fixes its operative PAUSE field to current-immediate, while D-022 adopts Aggressive/Full/Lazy plus mechanisms. [M1B process requirement](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-EXECUTION-CONTRACTS.md#L97-L120); [D-022](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L132-L147)
**Replacement, I:** “Retain frozen historical M1 semantics as a named regression profile; specify a reviewed mapping/new profile and independent fixtures for the current adopted Stop vocabulary. Do not relabel an old pass as current-policy coverage.”
**Competitor:** a boundary adapter versus a new explicit schema epoch.
**Falsifier:** accepted current mode is unrepresentable or silently mapped to a different scope/effect promise.
**Non-proof:** vocabulary reconciliation is not native process/receiver Stop evidence.

### S4. Separate governance from runtime responsibilities

**I from outline and source inventory:** Facet F can own evidence/acceptance governance but should not simultaneously serve as the unexamined owner of runtime explanation services, telemetry collectors, resource reconciliation and product release machinery. Naming a topic in F's title is not an interface/ownership contract. This report names the omissions below only; it does not conduct their design lanes.

## 4. Required additions: unowned facets and open floors, named only

These are **inferred architecture-coverage gaps**, not newly selected subsystems:

1. **Planning and executive semantics:** accepted goals/GoalSpec, decomposition/DAG proposal, role/lease ownership, worker/verifier boundaries and kernel admission interface. The outline says kernel must not own intent/tactics but gives no explicit owning facet. Relevant existing sources are [PLANNING](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/PLANNING.md), [AUTONOMOUS-PLANNING](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/AUTONOMOUS-PLANNING.md), [EXECUTIVES](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/EXECUTIVES.md) and [coverage rows](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ARCHITECTURE-COVERAGE.md#L117-L121). These historical product roles do not authorize a standing executive; current CONTROL_PLANE/ANCHOR and D-017/D-025 govern.
2. **Telemetry/accounting evidence:** ownership of meter ingestion, provenance, cumulative/delta/inclusion algebra, missing counters, observer overhead, privacy and export. [TELEMETRY](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/TELEMETRY.md); [G-25/G-26](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/GAPS.md#L65-L67)
3. **Update/release/disaster recovery:** release/config/schema identities, six update modes, restore/rollback/repair-forward promises, retained newest work and authority reacquisition. These are named unresolved boundary requirements, not an operations design performed here. [G-24](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/GAPS.md#L65-L65); [coverage DR/update](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ARCHITECTURE-COVERAGE.md#L73-L82); [release/packaging](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ARCHITECTURE-COVERAGE.md#L138-L147)
4. **Mobile floors:** Android minimum runtime API, target API, supported device/profile matrix; iOS minimum OS and supported iPhone/iPad profiles; qualified adaptive/multiwindow coverage. A9 sources govern.

**Editorial proposal:** add an explicit owner/consumers/contract-artifacts/evidence-status row for each. A separate facet is optional; silent ownership is not.
**Conforming competitor:** owned subfacets within A–F instead of adding new top-level letters.
**Falsifier:** a required behavior has no accountable contract owner or two conflicting canonical owners.
**Non-proof:** coverage rows do not prove complete requirements, safe implementation or reasonable scope.

## 5. Building-phase blocker versus deferrable-choice register

This is an **inferred readiness distinction**, not permission to reduce accepted release scope. D-019 and D-038 keep the comprehensive capability set built and opt-in. “Deferrable choice” here means the architecture can preserve alternatives while another bounded task proceeds, not that accepted features disappear from release. [D-019](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L62-L84); [D-038](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L461-L475)

| Must resolve for the named building/acceptance claim | Why it blocks | Can remain variable here |
|---|---|---|
| Correct IDs, pinned source closure and authority/evidence status | A false source or unratified invariant cannot be a reconstruction oracle | Citation presentation and namespaced alias format |
| Exact neutral bytes, refusal meanings, identity-domain/authority mapping | Independent implementations need an unambiguous expected outcome | Language, serializer implementation, memory/concurrency mechanisms |
| M1A known specification/fixture discrepancies and qualified positive controls | Regression equality can preserve a source defect or fail before the intended guard | Internal implementation organization |
| Integrated FU-03 acceptance and literal normative corpus for claims requiring them | Enlarging a diagnostic bound is not integrated acceptance | Diagnostic tools, logs and exploration aids |
| Commit/ACK/fence/artifact-closure/receipt semantics | A chosen store cannot supply missing authority or effect rules | Store backend until qualified comparison |
| Plugin semantic ABI/version/loss/refusal/authority contract | Host mechanism cannot determine portable semantics by accident | Process/WASM/FFI/IPC mechanism, renderer/toolkit |
| Current Stop vocabulary mapped to historical profiles | Historical immediate-only tests do not cover newly accepted modes | Per-platform mechanisms after obligations are fixed |
| Named facet owners and necessary interface joins | Intent, telemetry and release duties cannot remain ownerless | Top-level document layout/facet lettering |
| Mobile source/profile/fixture crosswalk and approved floors | Required before mobile compatibility or support claims | Concrete devices/toolkits until profile selection/qualification |
| Scope-specific native receiver/store/OS evidence | Required before claiming physical/runtime guarantees | Can be absent for explicitly inert provider-free construction claims |

The M1 discrepancy and integrated-gate evidence are detailed in the companion corpus report, not newly executed here. No technology ranking is performed. Safety/semantic blockers should be tied to the exact claim they block; they do not imply all useful inert contract authoring must stop.

## 6. M1 baseline and R-LANG joins for the final report

The companion **executable contract corpus report** at `../corpus/report.md` provides the full 71-bundle and program inventory, enforcement locations and historical/contrary evidence. Its primary-source facts should be joined into the corrected outline:

- 23 F-M1A, 24 F-M1B, 24 F-M1C bundles; do not call the sum 71 independent runtime tests.
- 17 test runners plus six materializer checks form the historical 23-invocation aggregate; the independent component inventory gates, rather than an obsolete allow-list, are the reusable evidence locations.
- Three pure planning implementation files plus two test-only reference oracles are not five production runtime components.
- The historical corpus includes known WorkPacket correlation/effect-attenuation discrepancies and immediate-only PAUSE. Preserve old identities/evidence and propose amendments separately; do not decree either implementation or prose correct merely because it is frozen.
- Integrated acceptance, literal-vector release, independent A/B reproduction and qualification of real store/OS/receiver boundaries remain distinct.

The R-LANG companion owns the detailed language facts and RC-01…RC-09 treatment. The required checklist to integrate is:
1. Record R-LANG advisory/no-selection outcome and explicit supersessions; remove “pending return” once referring to the current intake. Do not infer ratification from intake.
2. Separate immutable semantic/byte contracts from variable implementations; match numeric/null/Unicode/ordering rules to each candidate toolchain, including the stricter integer-only M1 profile.
3. Replace “cannot express an invariant in the new language” with a bounded witness obligation naming language/toolchain/profile/invariant/oracle and a smallest discriminating case.
4. Re-pin digest/canonicalization known-answer and rejection vectors per exact candidate serializer/toolchain; record independent expected-byte provenance.
5. Retest no-language-leak at the eventual FFI/plugin-host boundary, including ownership/lifetime/error/cancellation/version semantics; no host is selected now.
6. State that the integrated acceptance contract and independent normative corpus are the bar, not a larger diagnostic model or a green reduced mutant run.

**SD basis:** the current [GAPS packet-E update](https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/GAPS.md#L233-L251) records no language selection, Rust/Go/CPython shortlist, C#/.NET first expanded challenger, P1 language-neutral plugin direction and RC-01…RC-09 to author. The R-LANG companion analysis cites the full retrieved intake and report. This section deliberately does not turn shortlist membership into a recommendation or measure feasibility.

## 7. Provider-free verification method and decisive falsifier

**Proposed method, I; not run here:**
1. Freeze the revised outline's exact commit and a machine-readable claims manifest. Each behavior claim has source commit/blob/path/section, source status, evidence class, scope and a counterexample/non-proof.
2. Check A1–A10 as closed obligations, not keyword presence: resolve every identifier to the correct owner namespace; verify numerical denominators and profile identities; distinguish historical result versus current result versus unexecuted requirement.
3. Compare the source closure against current explicit Owner decisions and known supersessions, without granting ratification from file location or title. Preserve old bytes and reasons.
4. Crosswalk each reconstruction criterion to exact schema/fixture inventory, semantic gate, independent oracle and scope-specific receipt. Carry known source contradictions as blockers, not deletions.
5. Require a valid positive control and a minimal adverse mutation for each asserted invariant. A blanket-refusal implementation must fail.
6. Have an independent reviewer read the same frozen outline and source manifest, check the joins and challenge the conclusions. Under separately authorized execution, run unchanged component gates and the explicitly admitted integrated/qualification suites; no such execution is authorized or claimed by this report.
7. Scan the user-facing deliverable for secrets, raw auth material, accidental private-source publication and unsupported IDs. Private source citations remain private; no public research or third party receives repository content.

**Conforming competitor:** a versioned document/claim linter plus independently checked obligation ledger, instead of one monolithic checker. The same source closure and semantic requirements must be covered.

**Decisive falsifier:** the corrected outline fails if even one consequential invariant is untraceable/mis-scoped, a current-policy claim relies only on a stale profile, a candidate is mislabeled adopted, a required namespace/fixture disappears, a planted semantic defect survives due to a common expected-output path, or a conforming reimplementation must invent missing behavior rather than reconstruct it. Failure to substantiate the complete independent review must remain an explicit review-source gap.

**Non-proofs:** a perfect static correction is not correctness of its cited contracts, an implementation acceptance, a language/store choice, a release authorization, a security/legal clearance, physical durability or an unbounded proof.

## 8. Completion disposition

- A1–A10: corrected facts and exact replacement wording supplied, all source-anchored; A1/A4/A6/A7 brief premises independently corrected.
- Source pins/status: PR head and current master deliberately separated; exact independent architecture-review text remains unavailable in the inspected set.
- M1: companion inventory complete; full contents and enforcement sources in corpus deliverable.
- R-LANG: six integration requirements explicitly enumerated; language-specific factual detail belongs to the companion deliverable.
- Candidate hedges: command key, seven capability dimensions, broader shared-contract joins and observability named.
- Unowned facets/mobile floors: named only; no RPLAN/ROPS work performed.
- Readiness: semantic/qualification blockers separated from variable technology choices; accepted feature scope preserved.
- Verification: provider-free static method plus separately authorized later execution and clear falsifier.
- Repository/protected files unchanged; no target code run; no selection or legal conclusion.
