---
title: Recursion v3 — language-agnostic architecture outline (D-039)
status: candidate-outline-not-ratified
last-updated: 2026-10-02
parent-goal: goal-recursion-v3-harness-orchestrator
---
# Recursion v3 — language-agnostic architecture outline

**Candidate outline, not a ratified architecture.** Design/research evidence for D-039 ("plan a
language-agnostic architecture so a change of implementation language is a bounded reconstruction,
not a re-derivation"). It selects **no** language, store, framework, runtime, renderer, transport,
sandbox or vendor, and edits no protected path. Behavior-defining contracts are stated
implementation-neutrally; anything marked *variable* may change with the implementation without
changing behavior.

**Revision 2 (2026-10-02).** Incorporates the packet-F R-AUDIT corrections and the independent
PR #161 review: the A1–A10 corrections, the R-LANG-informed checklist (C1–C6), the M1 executable
corpus with its two code/wire discrepancies, and the architecture revision guide's three blocker
classes. Sources: `docs/v3/research-lane/packet-f-raudit-20261002/` (see `## Sources`). Wording
adopted from that packet is *inferred/proposed*, not ratification. Where this outline conflicts with
an authoritative spec or an Owner decision, the source governs; this document is non-normative until
the Owner ratifies it.

## 1. Purpose and method

- **Goal:** each facet is specified so a later reimplementation reproduces the same semantic behavior
  and passes the same machine-checkable gates, verified independently.
- **"Language-agnostic" means:** the behavior-defining layer — input grammar, field
  presence/nullability, numeric domains, Unicode validity, key comparator, array semantics, canonical
  UTF-8 bytes, digest preimages, refusal precedence, state machines, identity/epoch rules, test
  vectors and oracles — is stated without reference to a language. The implementation layer (parsers,
  data structures, serializers, numeric representations, error mechanisms, concurrency primitives,
  memory model, packaging, rendering, transport) is *variable*, and may vary only behind adapters
  that preserve the neutral contract.
- **Method:** facet by facet (A–F), record what it owns, what it must never own, its neutral
  contracts, artifacts, reconstruction evidence and open unknowns; then state the acceptance stages
  and the behavioral-contract blockers.

## 2. Cross-cutting invariants

1. **One product, one authority chain.** Control-plane precedence (Owner decision → `CONTROL_PLANE.md`
   → `docs/v3/*` → `state/DECISIONS.md` → experiments/evidence → runtime state) is language-neutral;
   research is evidence, not decision. Authority is not correctness — a document's location does not
   establish that its contracts are right.
2. **Deterministic kernel.** Four pure functions (`Decode → Admit → Reduce → DeriveWorkPacket`), one
   transition path, exactly **one fenced canonical writer** per aggregate, append-only event log,
   projections rebuildable from durable events + immutable artifacts.
3. **Typed identity domains, never one scalar epoch.** Writer/work/pause/process/node/config/release/
   route/descriptor/context/enrollment/stream-cursor epochs are separate; only same-domain values are
   ordered. Authority is an **intersection** (never a union); an unknown required dimension denies.
4. **Current authority gates every read, disclosure and effect.** A cache hit, `loaded`/`selected`
   state, transport ACK, lease, writer lock or provider listing never authorizes.
5. **Provenance is mandatory.** `ArtifactRef = (object_type, id, version, content_digest)` (4
   fields), `ContentEnvelope` (18 fields), exactly **one loss record**, and
   `authority_carried_none=true` on imported/model/plugin content; no 19th base field. (These shared
   fields are *recommended integration candidates* where they preserve inherited material — see §8.)
6. **`unknown` ≠ zero.** Missing counters are never zero; `null` means unknown/unavailable with a
   reason. Ambiguity is never converted to success, zero-effect or rollback.
7. **Evidence discipline.** Classes stay distinct (source-documented / locally observed / inferred /
   unknown); every consequential claim carries a conforming rival, a decisive falsifier and what a
   pass does not prove; an implementer's PASS is not acceptance.
8. **Canonical bytes under a declared profile.** M1B is a *strict application profile* around JCS and
   is **not** unrestricted RFC 8785 input: it rejects duplicate keys, all floats and negative zero
   before schema validation, and fixes max decode depth 64 and canonical object size 262,144 UTF-8
   bytes. Generic JCS module conformance and the candidate FU-03 `RCV1` test interchange are
   **distinct profiles** and must not be retrofitted onto the frozen M1 contracts.
9. **One pause/Stop vocabulary.** Aggressive / Full / Lazy; `checkpoint` / `interrupt` / `drain` are
   mechanisms, not modes. Detach, backgrounding, socket loss and process death are **not** Stop;
   quiescence is an observed conjunction. Note: the frozen M1B PAUSE field is current-immediate; it
   does not by itself settle every newer Stop policy.
10. **No credential custody.** Recursion does not hold, copy or relay provider credentials it was not
    granted; a trusted-launch boundary (D-029) is required for a strong non-access claim, otherwise
    only "non-intermediation-by-policy".
11. **Clients are projections + typed commands.** Every surface shares one semantic command/query
    catalog, the ten controls, the three Stops and the receipt-fact vocabulary; it never owns
    canonical state, authority or actor lifetime.
12. **Reconstructability (D-039).** A port reproduces the same normative bytes, refusals, state
    transitions and fixture outcomes under an independent oracle, and does not leak language details
    into the plugin/edition contract.

## 3. Facets

### A. Deterministic kernel, state, persistence, concurrency
- **Owns:** command decode/admission, ordered event-batch reduction, WorkPacket derivation, lease/fence
  issuance, effect-intent/receipt classification, replay, publication gating. **Must not own:**
  intent/tactics, budgets, provider wire semantics, external-effect truth, OS containment.
- **Neutral contracts:** atomic append (one admitted whole batch per visible mutation); `PrepareEffect`
  = one two-event atomic batch; commit revalidates ordinal/digest/writer; receipt+outbox+state+events
  in one transaction; projection stores processed receipt *identity* (not content digest); digest-
  chained log with fixed genesis; strict decode with a closed error alphabet; projection state machine
  and terminal rules; pure/ordered/digest-verified replay with no best-effort on gap/conflict.
- **Artifacts:** `CommandReceipt@1`, `PlanningEvent@1`, `ProjectionState@1`, `WorkPacket`, `GateResult`.
- **Reconstruction evidence uses distinct namespaces:** R-SC **Q20 F20-A…F20-G** are proposed,
  **unexecuted** receiver-fence/permission vectors; FU-02 defines cutpoints **P00…P19** and fault
  cells **F01…F42**; FU-03 defines its named semantic prefixes and the byte/refusal catalog; Stage B
  defines **SB-01…SB-26** with separately recorded bounded synthetic outcomes. The M1A/B/C fixtures
  are the inventory (§7). None of these alone qualifies a production receiver or durability profile.
- **Status/unknowns:** kernel freeze **conditional**; broker-inclusive vs split model undecided;
  store (JS1 vs SQL1, §3.E) and language unselected; power-loss durability unproven.

### B. Authority, safety, security, privacy, Stop, sandbox, trusted launch
- **Owns:** admission authority; monotonic writer fence and revocation; per-adapter effect fencing;
  Stop/quiescence/quarantine; credential non-intermediation; trusted-launch obligations; sandbox/
  supply-chain admission. **Must not own:** product intent, provider protocol, external-effect truth.
- **Neutral contracts:** effective authority is an intersection of current, independently bounded
  grants. M1B represents functional capability and workspace scope plus **nine** separately
  represented permission dimensions — credentials, network, model launch, route/account, quota,
  spend, update, downgrade, grant administration — so `PluginGrantDimensions@1` has **eleven** grant
  dimensions (no true/nonempty dimension implies another). Receiver-installed durable fence at the
  irreversible boundary; separate facts (`revocation_accepted` / `installation_pending` /
  `fence_installed` / `preinstall_effect_unknown` / `quiescence_observed_or_unknown`); Stop modes +
  drain allowlist; `paused_with_ambiguity`; typed refusal taxonomy; non-intermediation; direct +
  induced non-access.
- **Proposed production command key:** `(admitted_authority_domain, command_id)` with the principal/
  auth reference and effect-relevant meaning bound inside the immutable content digest; `content_digest`
  is the equality/conflict predicate, not a third key component. This is a **proposal for Owner
  adoption**, distinct from the historical frozen M1A identity.
- **Reconstruction evidence:** the R-SC Q20 fence vectors (unexecuted), R-E credential fixtures, R-U
  sandbox probes, negative controls (controller-only pause, signature≠authority).
- **Status/unknowns:** G-17 (per-adapter fence) unrun; G-19 containment open; strong non-access
  profiled but **unqualified** on every platform; refusal/loss taxonomy merge pending.

### C. Context, composition, compaction, plugins, provenance
- **Owns:** derived/compiled artifacts and their identity — the Execution Context Capsule, the
  compaction governor/fidelity contract, the capability graph, the composition compiler, provenance/
  span maps, memory/media projections. **Must not own:** canonical authority, effects, credential
  custody, a second loss truth, a global prompt manager.
- **Neutral contracts:** non-droppable control kernel; one compaction governor per route epoch
  (native/plugin compactors return candidates only); fidelity contract; capability graph with separate
  dependency and authority edges; the capability facts (installed/enabled/authorized/available/
  selected/loaded/executed) are **independent scoped dimensions, not seven steps in one lifecycle**;
  manifest-before-import and disabled inertness; the immutable `CompositionManifest` and its eight
  passes.
- **Plugin/edition boundary source:** the semantic/schema contract is **`PluginManifest@1`** with
  `PluginGrantDimensions@1`, `PluginGrantEnvelope@1`, `PluginPolicyEnvelope@1`,
  `PluginLifecycleRecord@1` and resolver records in M1B. **D-035 is repository-topology context**
  (plugins external, referenced by digest), not the plugin semantic spec. R-A's
  `urn:example:recursion:plugin-manifest:proposal-0` is a research design, not an accepted schema.
- **Reconstruction evidence:** R-PC F01–F18, R-UI UI-01…UI-20, provenance round-trips, capability
  attenuation fixtures, disabled-trace zeros, host-boundary transcripts (§6).
- **Status/unknowns:** no production language/store/serializer selected; privacy/publication order
  (G-09) candidate-only.

### D. Interfaces and clients (CLI/TUI/GUI/terminal/mobile)
- **Owns:** authorized projections and typed commands; command/query meaning; the M1B wire profile;
  viewer/controller/input-lease semantics; accessibility; offline "last synchronized" honesty.
  **Must not own:** canonical state, authority, actor/process lifetime, credentials.
- **Neutral contracts:** one semantic catalog; stable command identity (proposed, §3.B); `TypedError`
  with retryability; ten controls and three Stops parity; capability facts plus a separate client
  support/render disposition; receipt facts as an independent vector; one input lease and one resize
  lease per target; offline read states with source-cut/cursor honesty; `GAP` as a marker and `RESET`
  as the only same-stream continuation after a gap; fail-closed disposition alphabet with a surviving
  host-owned Stop/continuity/attention shell; `recursion.ui-contract@candidate-2` supplier-qualified
  `ContributionKey`/`NodeKey` with deterministic occurrence indices.
- **Interface-platform direction:** the R-L shared command/query catalog and snapshot-bound
  projections with typed receipts/errors and surface-equivalent meaning — a **specification
  candidate**. No platform-wide schema identifier, renderer, toolkit or transport is selected.
- **Mobile evidence is a pending reconciled suite:** map R-MOB **MOB-F01…F24** onto the shared
  iOS/Android **MP01…MP11** obligations, preserve non-overlapping requirements and platform-specific
  stimuli, evaluate with **one** independently defined semantic oracle, and register concrete
  per-platform `ClientCapabilityProfile@candidate-2`/`UiMaterializationManifest@candidate-2`
  instances. Do not add 24+11 or claim 35 distinct executed cases.
- **Status/unknowns:** G-10 renderer/runtime and G-05 protocol-freeze blockers open; Swift/SwiftUI
  and Kotlin/Compose remain **advisory**.

### E. Execution fabric, node/remote execution, routes, delegation, accounting
- **Owns:** route resolution/admission, node enrollment/revocation, one-writer availability seam,
  harness delegation and headless fan-out, honest quota/accounting. **Must not own:** credentials,
  provider internals, canonical admission.
- **Neutral contracts:** five separable identities (user client / authority coordinator / execution
  host / engine / provider+pool); `RouteCapabilityDescriptor@1` and the route-admission predicate
  (only `verified-current` selectable; unknown billing blocks); node-enrollment state machine with
  atomic join-proof consumption and generation-based revocation; one fenced writer with optional
  opt-in failover (no default consensus); `DelegationRecord`; the typed epoch vector; operation/
  reservation lineage; requested/compiled/effective-value discipline.
- **Store/replay:** FU-02 proposes two canonical-store finalists — **JS1** (RFC 7464 segmented JSON
  text sequences + CAS) and **SQL1** (SQLite WAL + CAS) — both using the same CAS. **O1 compares each
  finalist with the other and an independently reviewed normative corpus** (the shared CAS is a
  common-mode risk component, not a third finalist); **O2–O5** cover stale writers, duplicate/
  conflicting commands, artifact closure and recovery integrity. These comparisons are **unrun**; no
  store is selected.
- **Reconstruction evidence:** FU-02 O1–O5 (unrun), FU-04 platform fingerprint, R-O performance
  fixtures, R-X node fixtures RX-F01–F24, R-B/R-D handoffs.
- **Status/unknowns:** engines unselected (Codex App Server first priority only, D-023); transport/
  broker framing unselected; availability implementation unqualified; fixtures unexecuted.

### F. Governance, evidence/verification, observability, licensing
- **Owns:** who decides; scope; evidence/verification methodology; decision journaling; observability/
  replay; claim ledger; gaps/coverage; gates. Governance is distinct from runtime responsibility (this
  facet does not own runtime explanation services, telemetry collectors or release machinery).
- **Neutral contracts:** decision entries (options/because/consequence/falsifier/review); four
  evidence classes and `unknown ≠ zero`; rival+falsifier+non-proof per claim; implementer PASS ≠
  acceptance; exact-candidate acceptance protocol; the three replay labels (`replayed_recorded_input`
  / `rederived_deterministic_result` / `reexecuted_new_attempt`); decision vs claim classification.
- **Observability is candidate research:** R-V proposes model-free explanation as a deterministic
  query over an identified, authenticated event/receipt/artifact cut and retained historical inputs
  (`SourceCut`, `ExplainResult`, `rv-candidate-contracts-1`). It is **not** an adopted contract;
  recorded facts, observations, hypotheses and unknowns stay distinct, and replay never re-executes
  effects.
- **Reconstruction evidence:** the decision log + claim ledger reproduce with registered ranges; gate
  definitions yield the same pass/fail on the identical candidate; the pinned M1 executable corpus
  (§7) with its semantic limits; scoped, dated independent-verifier receipts (Session A `74fb18d`;
  Stage B `d56cc44`). The 2026-09-13 orientation-repair gate is historical
  (`V3_ORIENTATION_PASS_NOT_RELEASE_ACCEPTANCE`); its fixed scope allow-list does not admit the present
  tree and is not a reconstruction or release gate.
- **Status/unknowns:** G-32 licensing/counsel open; scientific-research mode contract-ready but unrun.

### G. (Unowned, pending R-PLAN) planning and executives
The planning/executive behavior layer — goal decomposition, executive loops, consultation/adoption,
persistent goal-scoped assistants, actor/model allocation, D-025 research-as-plugin — is **not yet
owned by a facet**. Packet-F ran only R-AUDIT; R-PLAN and R-OPS were not run. Until designed, the
planning→kernel interface remains an explicit open seam.

## 4. Facet interface map (language-neutral)

| From → To | Interface (examples) |
|---|---|
| F → all | precedence, scope, gate/acceptance rules, evidence classes |
| A → B | admission decision; lease/fence; effect intent/receipt |
| B → A | grant/privacy/egress/budget checks at admission; fence installation |
| C → A | `CompositionManifest`/capsule as an admitted derived artifact; provenance/loss |
| A → C | canonical refs, current authority, config revision, route assignment |
| D → C/A | typed commands/queries; projections requested; no writes |
| C/A → D | authorized projections; receipt facts; continuity/GAP |
| E → A | route admission, node observations, delegated results, meters |
| A → E | WorkPacket, fences, reservations, assignment |
| **(pending) G → A** | plan/proposal refs; executive lifecycle (unowned until R-PLAN) |

## 5. Language-neutral vs language-specific (variable)

| Must not vary | May vary |
|---|---|
| Input grammar; presence/nullability; numeric domains; Unicode validity; key comparator; array semantics | parser, data structures, numeric representation |
| Canonical UTF-8 bytes; digest preimages; domain/omission rules | serializer/crypto library (must pass KATs) |
| State-machine guards and transition order | concurrency primitives, memory model |
| Refusal alphabet semantics per schema epoch and its precedence | error mechanism beyond the alphabet |
| Identity/epoch domain separation | storage engine behind the contract |
| Plugin/edition contract (`PluginManifest@1` + grant/lifecycle records) | plugin host mechanism (process/WASM/FFI/IPC); SDK signatures, pointers, type names, exceptions, callbacks |
| Wire message set and `GAP`/`RESET`/credit rules | transport binding, framing library |
| Ten controls / three Stops / receipt-facts semantics | widget tree, gestures, key chords, renderer |
| Authority intersection and current-authority checks | route/adapter topology |
| Evidence classes and acceptance protocol | checker implementation, CI tooling |

## 6. Reconstruction acceptance criteria (the D-039 gate)

A change is **reconstructable**, not a re-derivation, only when all hold on an identical frozen
candidate:

1. **Specification closure:** every consequential obligation is stated neutrally; each invariant is
   bound to an independent observable plus a valid positive control and an adverse mutation; no
   unsupported identifier is asserted.
2. **Corpus + exact behavior:** the pinned M1 executable corpus (§7) plus FU-03 vectors reproduce the
   same canonical bytes, typed refusals and state transitions, with **actual bytes retained** (not
   digests alone) and independent A/B stacks.
3. **Independent oracle:** expectations derive from the spec, never from the candidate (no shared
   serializer/decoder/reducer/expected-output generator; shared hash primitives disclosed with
   independent KATs).
4. **Machine-checked invariants:** the kernel/authority model stays checkable (TLA+/TLC, Alloy); the
   reduced model is clean while reachable non-equivalent mutants are killed (equivalent/unreachable
   mutants are not kills).
5. **KAT re-pinning per toolchain (four layers):** strict admission; canonical bytes; primitive digest
   (independently sourced vectors, e.g. SHA-256 of `abc`); integrated digest binding. One normative
   corpus across arms, one result manifest per executable identity; a compiler/runtime/dependency/
   feature change creates a new result identity.
6. **Host-qualified no-leak:** SDK signatures, pointers, object identities, runtime type names,
   exception classes and callbacks are adapter-local and confer no authority; each admitted host/
   topology preserves the same schema, bytes, grants, lifecycle, refusal and uncertainty semantics
   under independent observations; qualify an engine/configuration, never the label "Wasm".
7. **Integrated bar:** the invariant-checking bar is the reconciled **integrated** gate at admitted
   bounds (independent monitors, reachability/vacuity controls, retained mutation dispositions), not
   an enlarged diagnostic model.
8. **Independent verification:** a verifier on a different model family reruns the exact candidate;
   attestations live outside the candidate.

RC-01–RC-04 and RC-06 from the packet-F reconstruction checklist map into items 1–8 of **this**
gate. **RC-05 (native crash equivalence), RC-08 (bounded reconstruction cost) and RC-09 (safe
migration) are not this gate.** They belong to the separate native/release qualification bar in the
three blocking-condition kinds below. No numerical reconstruction-cost ceiling is established
(unmeasured until experiment admission).

**Inability claims are bounded witnesses, not language verdicts:** "language L cannot express
invariant I" identifies the exact language + runtime + toolchain + topology + allowed primitives +
host profile, the minimal counterexample, the independent expected behavior and the permitted
remedies considered. A default-library mismatch rejects that configuration only; genuine
language-level inability requires a constraint argument excluding compliant implementations.

**Falsifier:** a language change requires re-deriving the architecture; the corpus cannot be
reproduced under an independent oracle; a required invariant cannot be expressed in the new language
under a stated witness; or a language detail leaks into the wire/plugin contract.

## 7. M1 executable corpus and behavioral-contract blockers

- **Inventory:** 71 fixture bundles — M1A `F-M1A-001…023` (23), M1B `F-M1B-001…024` (24), M1C
  `F-M1C-001…024` (24). Do **not** call this "71 tests or 71 executions." Enforced by closed-inventory
  fixture tests plus semantic runners (`*.test.py`), test-only oracles and generators. Source-recorded
  same-family PASS receipts exist at M1A `fb0e045`, M1B `0f1bd5f`, M1C `8cd1c8e`; they are historical,
  not new evidence.
- **Two source-documented code/wire discrepancies (blockers before freezing the behavioral contract):**
  1. **WorkPacket correlation identity** — `scripts/v3/planning/replay/kernel.py` hardcodes
     `CORR-M1A` where the wire profile requires the derived packet correlation ID to equal its
     `GoalSpec` ID.
  2. **WorkPacket effect attenuation** — the constructor copies `goal.payload.allowed_effect_keys`
     where the wire profile requires TaskNode effect fields; a task that narrows effects can yield a
     broader set.
  Resolve by explicit, versioned amendment that preserves old bytes and historical receipts
  (re-sealing digests alone is insufficient).

**Three kinds of blocking condition (keep separate):**
1. *Before freezing the affected behavioral contract* — resolve the two discrepancies; reconcile
   candidate identity/authority/byte rules; remove unsupported current-pass claims.
2. *Before claiming conforming reconstruction or integrated acceptance* — exact corpus/profile
   identity, independently defined expected behavior, adequate positive/adverse controls, integrated
   predicate coverage, scoped receipts.
3. *Before native or release qualification* — resolve supported mobile floors and host/toolchain
   profiles, then obtain required native evidence. A desktop policy is not mobile qualification; a
   synthetic observer is not OS enforcement.

## 8. Candidate hedges and status corrections

- **Hedges (candidate/proposed, not settled):** the production command key (§3.B); the capability
  facts as independent dimensions (not a lifecycle); the R-SC shared fields as recommended
  integration candidates.
- **Status corrections:** scoped historical receipts exist (Session A `74fb18d` **ACCEPT**; Stage B
  `d56cc44` 16 bounded-consistent / 3 unknown / 5 unsupported / 2 blocked), so "everything unrun" is
  wrong; no production serializer is selected but frozen M1 byte profiles remain binding; the frozen
  PAUSE is immediate-only; governance and runtime are separate responsibilities.

## 9. Open questions / Owner gates

- **Technology selection:** FU-01 language/runtime, FU-02 store, FU-03 full model/corpus, FU-04 native
  platform qualification, FU-05 broker chaos — all unrun; no language/store/framework selected.
  R-LANG returned **advisory** (no winner; retains Rust, Go and both CPython arms, C#/.NET first
  expanded challenger) and listed **14** named neutral units in its §7 (the intake's "15" is an
  inventory defect).
- **Open gaps:** G-09 configuration conformance / human syntax / privacy-publication ordering;
  G-10 renderer/runtime; G-17 per-adapter fence; G-19 containment; **G-20** performance comparison /
  R17 ablation / disabled-plugin benchmark; G-32 licensing/counsel.
- **Mobile floors are open:** Android minimum runtime API and target API (separately) and supported
  device profiles; iOS minimum OS and supported iPhone/iPad profiles. D-028 resolves desktop/backend
  floors only. D-036 governs private offline content; D-038 keeps mobile/GUI built but opt-in.
- **Pending research:** R-PLAN (planning/executives) and R-OPS (telemetry / update-release-DR /
  durable memory / editions) were not run.

## 10. Non-goals

- No technology selection, no implementation, no benchmark, no provider/live action, no spend.
- No ratification; no edit to a protected path; no reopening of accepted Owner decisions.
- Not a substitute for the authoritative specs named in `## Sources`.

## 11. Sources

Authoritative: `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md`, `state/DECISIONS.md` (D-017…D-039),
`docs/v3/{ARCHITECTURE,ARCHITECTURE-PROOF,STATE,CURRENT-AND-MIGRATION,ACCEPTANCE,VALIDATION,AUTONOMOUS-PLANNING,EXECUTIVES,PLANNING,SECURITY,CONTEXT,PLUGINS-AND-EDITIONS,ROUTE-CAPABILITIES,EXECUTION-FABRIC,PERFORMANCE-PLAN,CLI,TERMINAL-RUNTIME,INTERACTION-SURFACES,INTERFACE-STRATEGY,PRODUCT,TELEMETRY,CLAIM-LEDGER,DECISION-REGISTER,SCIENTIFIC-RESEARCH,M1A/B/C-*,GLOSSARY}.md`,
`docs/v3/research-lane/{RESEARCH-INDEX,SYNTHESIS,GAPS,ARCHITECTURE-COVERAGE,HARNESS-COVERAGE,NEXT-PHASE}.md`.
Intakes: `shared-contracts-20261002`, `dot-ra-rd-20261002`, `overnight-20261002`,
`followups-20261002`, `capability-composition-20261002d`, `ios-android-20261002`,
`core-language-control-surfaces-20261002`, and `packet-f-raudit-20261002` (corrections, corpus,
language, architecture revision guide, PR #165 review).

This outline is a synthesis; where it disagrees with a source, the source governs.
