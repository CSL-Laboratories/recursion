---
title: Recursion v3 — language-agnostic architecture outline (D-039)
status: candidate-outline-not-ratified
last-updated: 2026-10-02
parent-goal: goal-recursion-v3-harness-orchestrator
---
# Recursion v3 — language-agnostic architecture outline

**Candidate outline, not a ratified architecture.** It is design/research evidence for D-039
("plan a language-agnostic architecture so a change of implementation language is a bounded
reconstruction, not a re-derivation"), synthesized from the repository's accepted specs and research
intakes. It selects **no** language, store, framework, runtime, renderer, transport, sandbox or
vendor, and edits no protected path. Every behavior-defining contract below is stated
implementation-neutrally; anything marked *variable* may change with the implementation without
changing behavior.

Evidentiary basis (see `## Sources`): the accepted specs under `docs/v3/`, `state/DECISIONS.md`
D-017…D-039, and the research intakes (`shared-contracts-20261002`, `overnight-20261002`,
`dot-ra-rd-20261002`, `followups-20261002`, `capability-composition-20261002d`,
`ios-android-20261002`). Where this outline conflicts with an accepted spec, the spec wins; this
document is non-normative until the Owner ratifies it.

## 1. Purpose and method

- **Goal:** every facet is specified so that a later reimplementation reproduces the same semantic
  behavior and passes the same machine-checkable gates, verified independently.
- **"Language-agnostic" means:** the behavior-defining layer — schemas, wire profiles, state
  machines, invariants, refusal alphabets, identity/digest rules, test vectors and oracles — is
  stated without reference to a language. The implementation layer (memory model, concurrency
  primitives, libraries, packaging, rendering, transport) is explicitly *variable*.
- **Method:** facet by facet (A–F), record (i) what it owns, (ii) what it must never own, (iii) its
  language-neutral contracts, (iv) its artifacts, (v) reconstruction evidence, (vi) open unknowns.

## 2. Cross-cutting invariants (must never vary across implementations)

1. **One product, one authority chain.** Recursion is the v3 harness; the control-plane precedence
   (Owner's explicit decision → `CONTROL_PLANE.md` → `docs/v3/*` → `state/DECISIONS.md` →
   experiments/evidence → runtime state) is language-neutral; research is evidence, not decision.
2. **Deterministic kernel.** Four pure functions (`Decode → Admit → Reduce → DeriveWorkPacket`),
   one transition path, exactly **one fenced canonical writer** per aggregate, append-only event log,
   and projections rebuildable from durable events + immutable artifacts.
3. **Typed identity domains, never one scalar epoch.** Writer/work/pause/process/node/config/
   release/route/descriptor/context/enrollment/stream-cursor epochs are separate; only same-domain
   values are ordered. Authority is an **intersection** (never a union); unknown required dimension
   denies.
4. **Current authority gates every read, disclosure and effect.** A cache hit, `loaded`/`selected`
   state, transport ACK, lease, writer lock or provider listing never authorizes.
5. **Provenance is mandatory.** `ArtifactRef = (object_type, id, version, content_digest)` (4
   fields), `ContentEnvelope` (18 fields), exactly **one loss record**, and
   `authority_carried_none=true` on imported/model/plugin content; no 19th base field.
6. **`unknown` ≠ zero.** Missing counters are never zero; `null` means unknown/unavailable with a
   reason. Ambiguity is never converted to success, zero-effect or rollback.
7. **Evidence discipline.** Classes stay distinct (source-documented / locally observed / inferred /
   unmeasured); every consequential claim carries a conforming rival, a decisive falsifier and what a
   pass does not prove; an implementer's PASS is not acceptance.
8. **Canonical bytes under a declared profile.** M1 uses UTF-8 I-JSON + JSON Schema 2020-12 with
   RFC 8785 JCS and lowercase SHA-256; a digest omits only the structure's own self-digest. A
   different profile (e.g. the FU-03 test-only CBOR/RCV1) is a separately named profile with its own
   vectors.
9. **One pause/Stop vocabulary.** Aggressive / Full / Lazy; `checkpoint` / `interrupt` / `drain` are
   mechanisms, not modes. Detach, backgrounding, socket loss and process death are **not** Stop;
   quiescence is an observed conjunction, never asserted.
10. **No credential custody.** Recursion does not hold, copy or relay provider credentials it was not
    granted; a trusted-launch boundary (D-029) is required for a strong non-access claim, otherwise
    only "non-intermediation-by-policy".
11. **Clients are projections + typed commands.** Every surface (CLI/TUI/GUI/mobile/Telegram) shares
    one semantic command/query catalog, the ten controls, the three Stops and the receipt-fact
    vocabulary; it never owns canonical state, authority or actor lifetime.
12. **Reconstructability (D-039).** A port must reproduce the same normative bytes, refusals, state
    transitions and fixture outcomes under an independent oracle, and must not leak language details
    into the plugin contract (D-035).

## 3. Facets

### A. Deterministic kernel, state, persistence, concurrency
- **Owns:** command decode/admission, ordered event batch reduction, WorkPacket derivation, lease/
  fence issuance, effect-intent/receipt classification, replay, publication gating. **Must not own:**
  intent/tactics, budgets, provider wire semantics, external-effect truth, OS containment.
- **Language-neutral contracts:** atomic append (one admitted whole batch per visible mutation);
  `PrepareEffect` = one two-event atomic batch; commit revalidates ordinal/digest/writer; receipt+
  outbox+state+events in one transaction; projection stores processed receipt *identity* (not content
  digest); digest-chained log with fixed genesis; strict decode with closed error alphabet; the
  projection state machine and its terminal rules (gate passage necessary, `PlanVerifierDecision`
  required to complete); replay is pure/ordered/digest-verified with no best-effort on gap/conflict.
- **Artifacts:** `CommandReceipt@1`, `PlanningEvent@1`, `ProjectionState@1`, `WorkPacket`, `GateResult`.
- **Reconstruction evidence:** the FU-03 byte/refusal corpus, TLA+/TLC witnesses (`NoBlindEffectReplay`,
  `RFixed` clean), the 26-fixture Stage B crash-cut/fence matrix, and independent oracle/trace checker.
- **Status/unknowns:** kernel freeze is **conditional**; broker-inclusive vs split model undecided;
  store (JSONL+CAS vs SQLite-WAL+CAS) and language unselected; power-loss durability unproven.

### B. Authority, safety, security, privacy, Stop, sandbox, trusted launch
- **Owns:** admission authority; monotonic writer fence and revocation; per-adapter effect fencing;
  Stop/quiescence/quarantine; credential non-intermediation; trusted-launch obligations; sandbox/
  supply-chain admission. **Must not own:** product intent, provider protocol, external-effect truth.
- **Language-neutral contracts:** grant lattice with intersection/attenuation (six authority
  dimensions); receiver-installed durable fence at the irreversible boundary; separate facts
  (`revocation_accepted` / `installation_pending` / `fence_installed` / `preinstall_effect_unknown` /
  `quiescence_observed_or_unknown`); Stop modes + drain allowlist; `paused_with_ambiguity`;
  canonical command key `(admitted_authority_domain, command_id)` (digest is the conflict predicate,
  not a key); typed refusal taxonomy; non-intermediation property; direct + induced non-access.
- **Reconstruction evidence:** FU-03 crash-cut/receiver-fence fixtures (F20-A–G), R-E credential
  fixtures, R-U sandbox probes, negative controls (controller-only pause, signature≠authority).
- **Status/unknowns:** G-17 (per-adapter fence) unrun; G-19 containment open; strong non-access
  profiled but **unqualified** on every platform; refusal/loss taxonomy merge pending.

### C. Context, composition, compaction, plugins, provenance
- **Owns:** derived/compiled artifacts and their identity — the Execution Context Capsule, the
  compaction governor/fidelity contract, the capability graph and plugin lifecycle, the composition
  compiler, provenance/span maps, memory/media projections. **Must not own:** canonical authority,
  effects, credential custody, a second loss truth, a global prompt manager.
- **Language-neutral contracts:** non-droppable control kernel; one compaction governor per route
  epoch (native/plugin compactors return candidates only); fidelity contract (directives, refs,
  rehydratable evidence, preserved contradictions); capability graph with **separate dependency and
  authority edges** and seven independent capability states (installed/enabled/authorized/available/
  selected/loaded/executed); manifest-before-import and disabled inertness; `ArtifactRef`/
  `ContentEnvelope`/one loss record; the immutable `CompositionManifest` and its eight passes
  (capture cut → resolve graph → protect closure → select optional → admit disclosure → render →
  validate fit → install/dispatch).
- **Reconstruction evidence:** R-PC F01–F18, R-UI UI-01…UI-20, provenance round-trips, capability
  graph attenuation fixtures, disabled-trace zeros.
- **Status/unknowns:** no language/store/serialization selected; privacy/publication order (G-09)
  candidate-only; seven-state wording is a candidate, not a frozen enum.

### D. Interfaces and clients (CLI/TUI/GUI/terminal/mobile)
- **Owns:** authorized projections and typed commands; command/query meaning; the M1B wire profile;
  viewer/controller/input-lease semantics; accessibility; offline "last synchronized" honesty.
  **Must not own:** canonical state, authority, actor/process lifetime, credentials.
- **Language-neutral contracts:** one semantic catalog; stable command identity
  `(admitted_authority_domain, command_id)`; `TypedError` with retryability; ten controls and three
  Stops parity; seven capability states plus a separate client-support/render disposition; receipt
  facts as an independent vector; one input lease and one resize lease per target (separate domains);
  offline read states with source cut/cursor honesty; `GAP` as a visible marker and `RESET` as the
  only same-stream continuation after a gap; fail-closed disposition alphabet with a surviving
  host-owned Stop/continuity/attention shell; `recursion.ui-contract@candidate-2` supplier-qualified
  `ContributionKey`/`NodeKey` with deterministic occurrence indices.
- **Reconstruction evidence:** UI-01…UI-20 (+ C×V soak), MOB-F01…F24, MP01…MP11, lease/fence fixtures.
- **Status/unknowns:** unified interface platform is a candidate (`rlc-candidate/0`), not accepted;
  G-10 renderer/runtime and G-05 protocol-freeze blockers open; Swift/SwiftUI and Kotlin/Compose
  remain **advisory**.

### E. Execution fabric, node/remote execution, routes, delegation, accounting
- **Owns:** route resolution/admission, node enrollment/revocation, one-writer availability seam,
  harness delegation and headless fan-out, honest quota/accounting. **Must not own:** credentials,
  provider internals, canonical admission.
- **Language-neutral contracts:** five separable identities (user client / authority coordinator /
  execution host / engine / provider+pool); `RouteCapabilityDescriptor@1` and the route admission
  predicate (only `verified-current` selectable; unknown billing blocks); node enrollment state
  machine with atomic join-proof consumption and generation-based revocation; one fenced writer with
  optional opt-in failover (no default consensus); `DelegationRecord` binding identity/authority/
  continuity/capability-loss/reconciliation, with Recursion owning the headless JSON stream; typed
  epoch vector; operation/reservation lineage (command vs logical-operation vs attempt; one retry
  owner); requested/compiled/effective-value discipline.
- **Reconstruction evidence:** FU-02 three-way replay parity (O1) + stale-writer/duplicate/GC oracles,
  FU-04 platform fingerprint, R-O performance fixtures, R-X node fixtures RX-F01–F24, R-B/R-D handoffs.
- **Status/unknowns:** engines unselected (Codex App Server first priority only, D-023); transport/
  broker framing unselected; availability implementation unqualified; every fixture unexecuted.

### F. Governance, evidence/verification, observability, licensing
- **Owns:** who decides; scope; evidence/verification methodology; decision journaling; observability/
  replay; claim ledger; gaps/coverage; gates. **Must not own:** product mechanisms (it constrains
  them).
- **Language-neutral contracts:** decision entries (options/because/consequence/falsifier/review);
  four evidence classes and `unknown ≠ zero`; rival+falsifier+non-proof per claim; implementer PASS ≠
  acceptance; exact-candidate acceptance protocol (two clean passes + two independent reviews,
  attestations outside the candidate); observability as a deterministic query over an authenticated
  cut, never re-execution; the three replay labels (`replayed_recorded_input` /
  `rederived_deterministic_result` / `reexecuted_new_attempt`); decision vs claim classification.
- **Reconstruction evidence:** the decision log + claim ledger reproduce with the registered ranges;
  gate definitions yield the same pass/fail on the identical candidate; evidence classes preserved;
  `tools/check_v3_orientation.py --contracts` passes.
- **Status/unknowns:** G-32 licensing/counsel open; scientific-research mode contract-ready but unrun;
  no independent rerun of any research lane yet.

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
| F → E/C/D | claim/evidence rules, Owner decisions, gap/coverage status |

## 5. Language-neutral vs language-specific (variable)

| Must not vary | May vary |
|---|---|
| Contract/schema field names, bounds, unions, nullability | implementation language, libraries |
| Digest-omission rule; canonicalization profile version | serializer/crypto library (must pass KATs) |
| State-machine guards and transition order | concurrency primitives, memory model |
| Refusal alphabet semantics per schema epoch | error serialization beyond the alphabet |
| Identity/epoch domain separation | storage engine behind the contract |
| Plugin contract (D-035) | plugin host mechanism (process/WASM/FFI/IPC) |
| Wire message set and `GAP`/`RESET`/credit rules | transport binding, framing library |
| Ten controls / three Stops / receipt facts semantics | widget tree, gestures, key chords, renderer |
| Authority intersection and current-authority checks | route/adapter topology |
| Evidence classes and acceptance protocol | checker implementation, CI tooling |

## 6. Reconstruction acceptance criteria (the D-039 gate)

An implementation change (or a fresh port) is **reconstructable**, not a re-derivation, only when all
hold on an identical frozen candidate:

1. **Behavior corpus reproduces:** the same canonical bytes, typed refusals, state transitions and
   fixture outcomes (FU-03 vectors, M1A/B/C fixtures, R-PC/R-UI/R-MOB suites).
2. **Gates reproduce:** the same gate definitions pass/fail on the identical candidate, with the
   unchanged regression inventory and frozen predecessor gates; attestations stored outside the
   candidate.
3. **Independent oracle:** expectations derive from the spec, never from the candidate (no shared
   serializer/decoder/reducer/expected-output generator).
4. **Machine-checked invariants:** the kernel/authority model remains checkable (TLA+/TLC, Alloy) and
   the reduced model stays clean while a planted mutant is killed.
5. **No language leak:** the plugin contract (D-035) and the wire profiles are unchanged; variable
   details are confined to the marked layer.
6. **Independent verification:** a verifier on a different model family reruns the exact candidate.

**Falsifier:** a language change requires re-deriving the architecture, the behavior corpus cannot be
reproduced under an independent oracle, a required invariant cannot be expressed in the new language,
or a language detail leaks into the wire/plugin contract.

## 7. Open questions / Owner gates

- **Technology selection:** FU-01 language/runtime, FU-02 store, FU-03 full model/corpus, FU-04 native
  platform qualification, FU-05 broker chaos — all unrun; no language/store/framework selected.
- **Open gaps:** G-09 privacy/publication order, G-10 renderer/runtime, G-17 per-adapter fence,
  G-19 containment, G-20 configuration lifecycle, G-32 licensing/counsel.
- **Resolved-but-constraining:** D-028 platform floors; D-036 online-only private view; D-037 Python
  as disposable harness only; D-038 GUI/mobile built-but-opt-in; D-039 this outline.
- **Pending research:** R-LANG (core language + reconstructability) and R-CS (control-surface study)
  from packet `dot-research-20261002e` — this outline should be cross-checked against R-LANG when it
  returns.

## 8. Non-goals

- No technology selection, no implementation, no benchmark, no provider/live action, no spend.
- No ratification; no edit to a protected path; no reopening of accented Owner decisions.
- Not a substitute for the authoritative specs named in `## Sources`.

## 9. Sources

`CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md`, `state/DECISIONS.md` (D-017…D-039),
`docs/v3/{ARCHITECTURE,ARCHITECTURE-PROOF,STATE,CURRENT-AND-MIGRATION,ACCEPTANCE,VALIDATION,AUTONOMOUS-PLANNING,EXECUTIVES,PLANNING,SECURITY,CONTEXT,PLUGINS-AND-EDITIONS,ROUTE-CAPABILITIES,EXECUTION-FABRIC,PERFORMANCE-PLAN,CLI,TERMINAL-RUNTIME,INTERACTION-SURFACES,INTERFACE-STRATEGY,PRODUCT,TELEMETRY,CLAIM-LEDGER,DECISION-REGISTER,SCIENTIFIC-RESEARCH,M1A/B/C-*,GLOSSARY}.md`,
`docs/v3/research-lane/{RESEARCH-INDEX,SYNTHESIS,GAPS,ARCHITECTURE-COVERAGE,HARNESS-COVERAGE,NEXT-PHASE}.md`,
and the intakes `shared-contracts-20261002`, `dot-ra-rd-20261002`, `overnight-20261002`,
`followups-20261002`, `capability-composition-20261002d`, `ios-android-20261002`.

This outline is a synthesis; where it disagrees with a source, the source governs.
