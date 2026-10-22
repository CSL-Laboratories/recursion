# O1 Month-End Journeys and Capability/Release Evidence Contract

**Version:** `o1-contract v0.1-draft`  
**Status:** DRAFT proposal under envelope G; nothing accepted until a rule-H receipt  
**Parent Issue:** [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205) (Outcome O1, child of [CSL-45](https://linear.app/csl-laboratories/issue/CSL-45))  
**Milestone:** W1 Research (2026-10-16 to 2026-10-22) · Target Freeze: 2026-10-23 (W2)  
**Month Window Checkpoint:** 2026-11-12 (decided by Lucas Guerra, 2026-10-16, 09:42 PT, when the team's X subscriptions expire)  
**Governing Documents:** [CONTROL_PLANE.md](../../CONTROL_PLANE.md), [ANCHOR.md](../../ANCHOR.md), [AGENTS.md](../../AGENTS.md)

---

## 1. Scope and Target Fragment

### 1.1 Placement and Purpose
This contract establishes the observable, evidence-backed foundation for Recursion v3's month-long program, advancing [CSL-45](https://linear.app/csl-laboratories/issue/CSL-45) from an aspirational feature inventory into an empirical delivery framework. It defines the formal acceptance boundary for:
1. The **Journey Catalog** ([O1-JOURNEYS.md](O1-JOURNEYS.md)), preserving [D-024](../../state/DECISIONS.md#d-024--frontier-claim-productive-continuity)'s six release journeys and five crosswalk scenarios across native, upstream, and combined execution modes.
2. The **All-Pillar Capability Matrix** ([O1-CAPABILITY-MATRIX.json](O1-CAPABILITY-MATRIX.json)), evaluating 12 capability pillars from native harness kernel to release/rollback operations.
3. The **Decisions Queue and Evidence Manifest** ([O1-DECISIONS-QUEUE.md](O1-DECISIONS-QUEUE.md)), capturing pending founder decisions for Lucas Guerra under Rule H.
4. The **Verification Interface and Fixture Manifest** (Sections 6–9 below), specifying the machine-checkable acceptance gate for downstream qualification.

This contract refines existing scope without duplicating or preempting the program-control ledgers and receipt schemas developed under [CSL-204](https://linear.app/csl-laboratories/issue/CSL-204).

### 1.2 Boundary Conditions
- **Draft Status:** Everything in this document, the companion catalog, matrix, and queue is a draft proposal under Gate Envelope G.
- **Rule H Requirement:** No capability row, target tier, release scope, or architecture choice is accepted into the normative product contract until an authenticated human receipt under Rule H is recorded on Linear.
- **Checkpoint Date:** The month-end window ends on 2026-11-12. Target tiers for this checkpoint remain undecided pending Lucas's Rule H receipt.

---

## 2. Non-Goals (Verbatim Intent)

As specified in the [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205) contract:
- **No narrower first-release cut:** This contract does not define an unofficial subset or narrower scope cut. Downstream issue [CSL-237](https://linear.app/csl-laboratories/issue/CSL-237) proposes the R1 release boundary for founder decision; agents do not accept it.
- **No inferred deadline:** No deadlines beyond the stated 2026-11-12 subscription expiration checkpoint and intermediate freeze milestones are inferred or imposed.
- **No launch declaration:** Nothing in this contract authorizes public marketing claims, launch announcements, or documentation publication as a released product. Public launch page claims remain dependent on [CSL-242](https://linear.app/csl-laboratories/issue/CSL-242).
- **No universal savings claim:** Savings in quota, tokens, or cost are hypotheses to be measured per workload under [D-018](../../state/DECISIONS.md#d-018--thesis-recursion-is-a-native-harness-and-a-unifying-wrapper-no-ecosystem-lock-in) and [D-031](../../state/DECISIONS.md#d-031--product-positioning-no-vendor-sabotage-claim), never asserted as universal marketing facts.
- **No technology selection:** This contract selects no implementation language (A1b bake-off remains open), database/store (SQLite remains an unrun challenger), web framework, container sandbox, or transport.
- **Incomplete release protection:** A below-release checkpoint result is not permission to ship an incomplete accepted release.

---

## 3. Inherited-Decision Crosswalk

This contract strictly inherits and binds the decisions recorded in [state/DECISIONS.md](../../state/DECISIONS.md) and the accepted product directions in [PRODUCT.md](PRODUCT.md) and [DECISION-REGISTER.md](DECISION-REGISTER.md). Every referenced ID has been verified in the repository.

### 3.1 Repository Decision Log Crosswalk (state/DECISIONS.md)

| Decision ID | Summary Title | One-Line Constraint / Core Rule | Repository File Reference |
|---|---|---|---|
| **D-018** | Native Harness & Unifying Wrapper | Recursion must be both a native harness and an ecosystem wrapper; no vendor lock-in; quota savings are hypotheses requiring falsifiable proof. | [state/DECISIONS.md](../../state/DECISIONS.md#d-018--thesis-recursion-is-a-native-harness-and-a-unifying-wrapper-no-ecosystem-lock-in) |
| **D-019** | Comprehensive Build, Opt-In Install | Reject Dot Option B (smaller initial release); build comprehensive capability surface; ship lean default with opt-in plugins/profiles. | [state/DECISIONS.md](../../state/DECISIONS.md#d-019--release-model-build-comprehensively-ship-lean-install-opt-in-dot-option-b-rejected) |
| **D-020** | Availability & Monotonic Fencing | Core engine is single-node with kernel monotonic fencing; automatic coordinator takeover is an opt-in first-party plugin. | [state/DECISIONS.md](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06) |
| **D-021** | Credential Non-Intermediation | Recursion must not hold, copy, or relay ungranted credentials; delegate directly to user-authenticated local CLIs/tools. | [state/DECISIONS.md](../../state/DECISIONS.md#d-021--delegation-over-credential-intermediation) |
| **D-023** | First Upstream Qualification Target | Qualify Codex App Server (`codex app-server`) end-to-end first; all other upstream harnesses ship as plugins. | [state/DECISIONS.md](../../state/DECISIONS.md#d-023--first-qualified-upstream-profile) |
| **D-024** | Productive Continuity Moat | Headline measurable differentiator is productive continuity across harness change, compaction, fork, or host loss; adopt six journeys. | [state/DECISIONS.md](../../state/DECISIONS.md#d-024--frontier-claim-productive-continuity) |
| **D-025** | Bounded Autonomous Research | 24/7 autonomous research is an optional, off-by-default, budgeted plugin; no standing executive; no idle inference in lean base. | [state/DECISIONS.md](../../state/DECISIONS.md#d-025--continuing-autonomous-research-is-an-optional-bounded-plugin) |
| **D-028** | Platform Floors & OS Contracts | Windows 11 25H2/24H2, Linux 6.12/6.6 LTS cgroup v2, local NTFS/ext4; survive power loss via abrupt VM reset replay. | [state/DECISIONS.md](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11) |
| **D-029** | Trusted Launch Boundary | Operator-provisioned trusted launch boundary outside Recursion readable domain required for strong credential non-access. | [state/DECISIONS.md](../../state/DECISIONS.md#d-029--operator-provisioned-trusted-launch-boundary-strong-credential-non-intermediation) |
| **D-034** | Workflow Scaling Moat | Thesis is "the harness that scales with your workflow"; deterministic capability composition must be reproducible, cache-aware, injection-safe. | [state/DECISIONS.md](../../state/DECISIONS.md#d-034--product-thesis-the-harness-that-scales-with-your-workflow) |
| **D-036** | Mobile Online-Only Private View | Mobile clients operate online-only for private views; no offline protected redisplay; live authentication required before rehydration. | [state/DECISIONS.md](../../state/DECISIONS.md#d-036--mobile-offline-retention-online-only-private-view-o-01) |
| **D-037** | Disposable Test Harness | Authorize provider-free conformance fixtures; Python runtime serves strictly as a disposable test harness without selecting product language. | [state/DECISIONS.md](../../state/DECISIONS.md#d-037--conformance-fixtures-authorized-python-as-disposable-test-harness-only-o-02) |
| **D-038** | GUI & Mobile Supersession | D-034/D-019 supersede older first-release exclusions; GUI and mobile clients are in-scope built-but-opt-in components. | [state/DECISIONS.md](../../state/DECISIONS.md#d-038--d-034d-019-supersede-older-first-release-guimobile-exclusions-o-03) |
| **D-039** | Language-Agnostic Architecture | Mobile OS expansion advisory packet accepted; core and client architectures must remain language-agnostic and reconstructable. | [state/DECISIONS.md](../../state/DECISIONS.md#d-039--accept-the-oslanguage-expansion-as-advisory-plan-a-language-agnostic-architecture-o-04) |

### 3.2 Product Requirement Directions Crosswalk (PRODUCT.md & DECISION-REGISTER.md)

| Requirement ID | Direction Title | One-Line Constraint / Core Rule | Repository Mapping |
|---|---|---|---|
| **OR-021** | Usable Platform Scope | Standalone v3 architecture; terminal platform first, dedicated GUI built-opt-in per D-038; developer initial validation audience. | [PRODUCT.md](PRODUCT.md#accepted-first-usable-platform-release-â-2026-09-06), `CAP-014`, `CAP-015` |
| **OR-022** | Native & Upstream Parity | Native loop and upstream harness execution are equal release requirements; upstream-only must not require a hidden API key. | [PRODUCT.md](PRODUCT.md#accepted-first-usable-platform-release-â-2026-09-06), `CAP-002`, `CAP-005` |
| **OR-023** | Ten Platform Controls | All ten CLI controls required; maximize feature coverage across shared runtime, adapters, native loop, and plugins. | [PRODUCT.md](PRODUCT.md#accepted-first-usable-platform-release-â-2026-09-06), `CAP-007`, `CAP-014` |
| **OR-024** | Optimization Vector Ranking | Default optimization ranking is responsiveness, subscription efficiency, then task completion; report full Pareto vector without hidden scalar weights. | [PRODUCT.md](PRODUCT.md#optimization-target), `CAP-023` |
| **OR-025** | Remote Control & Recovery | Windows/Linux remote control, distributed stages, and recorded-work recovery are first-release requirements; macOS is a test opportunity. | [PRODUCT.md](PRODUCT.md#accepted-first-usable-platform-release-â-2026-09-06), `CAP-012` |
| **OR-026** | Launch-Critical Harnesses | Seven launch-critical names: Grok, Cursor, Gemini, Claude, Hermes, OpenCode, ChatGPT; detect existing installs; deterministic setup. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-004`, `CAP-005`, `CAP-005b` |
| **OR-027** | Persistent Assistants | Persistent assistant identity across goals/sessions; project profiles; routine delegation within authorized bounds; idle lifecycle. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-020`, `CAP-021` |
| **OR-028** | Ten Controls & Three Stops | Implement ten controls, three Stops (Aggressive, Full, Lazy per D-022), and preserved forks with preflight notice or post-work disclosure. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-017` |
| **OR-029** | Plugin & Browser Adaptation | Browser/MCP adaptation, local execution, inspectable incompatibility responses (four modes), and configuration revision propagation. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-009` |
| **OR-030** | Context Continuity & Ledgers | Context governor and accounting ledger retain handoff quality across quota, quality, tokens, and resources through completion. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-018`, `CAP-019` |
| **OR-031** | Verifier Service & Reporting | Report tests run, supported fix confidence, regressions, and demonstrated benefit; verifier reporting stays lean and configurable. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-027` |
| **OR-032** | Recovery Coordinator Bounds | Configure recovery destination, automation, and relative recovery budget allowance as a fraction of pre-shutdown usage with cumulative reservations. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-032` |
| **OR-033** | Six Update Modes & Rollback | Distinguish six update modes; require compatibility verification before rollback or resume per OR-033, preserving newest work/history, maintaining an explicit hold when compatibility is unknown, and offering configured repair-forward or separate-instance alternatives without promising automatic rollback. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-029`, `CAP-030` |
| **OR-034** | Quality Protection | Reduce repeated instructions, compaction loss, and cache inefficiency; protect landed-change quality from superficial token cuts. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-018`, `CAP-019` |
| **OR-035** | Resource Governor Profiles | Deterministic resource governor admits representative profiles (CPU/RAM/disk/network) and operator hard ceilings without workstation bias. | [PRODUCT.md](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), `CAP-024` |
| **OR-036** | Proof Boundary Refinement | Smallest aggregate plus explicit independently checkable bounded broker refinement; include necessary broker state if separate checking fails. | [ACCEPTANCE.md](ACCEPTANCE.md#mark-3-accepted-choices-and-additional-evidence-gates-â-2026-09-07), `CAP-033` |
| **OR-037** | GoalSpec Amendment Authority | Owner-only actual GoalSpec amendments; substantial routine delegation permitted; test avoidable interruptions before proposed delegation. | [ACCEPTANCE.md](ACCEPTANCE.md#mark-3-accepted-choices-and-additional-evidence-gates-â-2026-09-07), `CAP-034` |
| **OR-038** | Assistant/Head Collaboration | Task-aligned autonomy and initiative within agreed boundaries; questionnaire-assistant/Head interaction across sessions and nodes. | [PRODUCT.md](PRODUCT.md#accepted-working-foundation-and-required-collaboration-â-2026-09-07), `CAP-022` |
| **OR-039** | Whole-System Optimization | Configuration, compatibility, and whole-system optimization central; rich and lean configurations without deleting accepted capabilities. | [PRODUCT.md](PRODUCT.md#accepted-working-foundation-and-required-collaboration-â-2026-09-07), `CAP-007` |

---

## 4. Evidence Classes and Tiers

Per [CONTROL_PLANE.md](../../CONTROL_PLANE.md) Invariant 7 and [MONTH-PROGRAM.md](program-control/MONTH-PROGRAM.md#offline-ledger-bindings) §7, evidence classes and maturity tiers remain strictly segregated.

### 4.1 Evidence Classes
Evidence classes categorize the empirical basis of a record:
1. **Source-Documented:** Behavior, flag, schema, or limit extracted directly from public vendor documentation, specs, commit history, or static inspection of code and test files. Any evidence based only on reading code, test definitions, or schemas without a recorded execution command, full 40-hex candidate commit SHA, and observed exit/trace result must be classified as `source_documented`, never `locally_observed`.
2. **Locally-Observed:** Traces, exit codes, event streams, or logs recorded during deterministic execution on a clean local or CI workspace, binding the exact execution command, candidate commit SHA, and result.
3. **Inferred:** Deductions or interpolations from related mechanisms. Must be explicitly labeled and cannot satisfy an empirical gate.
4. **Unmeasured:** Capabilities, performance numbers, or quota claims lacking recorded instrumentation.

### 4.2 Proposed Maturity Tiers (Candidate Vocabulary)
The following eight-tier monotonic progression is an **Envelope G proposal** derived from [MONTH-PROGRAM.md](program-control/MONTH-PROGRAM.md#2-capability-and-release-matrix) §2; it remains a candidate schema vocabulary until ratified by founder receipt:
1. `researched` — Source-documented requirements, options, and tradeoffs filed.
2. `human_decided` — Human decision receipt recorded under Rule H selecting direction.
3. `contract_frozen` — Exact JSON/markdown schema frozen with SHA-256 digest.
4. `prototyped` — Exploratory implementation demonstrating core mechanics.
5. `implemented` — Production code integrated into repository structure.
6. `provider_free_verified` — Passes deterministic provider-free fixtures/mocks.
7. `locally_qualified` — Passes end-to-end trials on real OS environments (Windows/Linux).
8. `release_qualified` — Composed useful-outcome and fault traces verified across all platforms.

### 4.3 Promotion Rules ("No Promotion Without Evidence")
- **Synthetic-to-Live Prohibition:** Synthetic fixture passes and simulated traces (e.g., canned JSONL streams from [CSL-84](harness/CSL-84.md)) can never be promoted to or support `locally_qualified` or `release_qualified` live evidence tiers under any circumstances, regardless of whether a genuine or authentic execution record of the mock execution exists. An authentic execution record of a mock or synthetic test run remains synthetic evidence and strictly cannot qualify live or release tiers.
- **Composition Requirement:** Isolated component passes (e.g., unit test green) cannot stand in for composed, useful-outcome journey verification.
- **Candidate Immutability:** Any modification to candidate source invalidates prior verification passes; verification must re-execute on the exact 40-hex commit.

---

## 5. Value Conventions

To prevent semantic drift, silent failures, and unearned claims, all matrix entries and journey definitions adhere to the following value rules:

1. **Target Tiers:** All row target tiers remain `null` until explicitly set by Lucas Guerra in a Rule-H receipt.
2. **Pending Rows Fail Acceptance:** Rows with `target: null` and missing recognized disposition (`unmet`, `blocked`, `no-release-claim`) represent undecided work and **must fail** the acceptance gate (`O1E-003`). Draft status `disposition_status: "pending-H"` is permitted only in draft-only structural inspection, never in positive acceptance baselines.
3. **Unknown Counters:** Missing or unmeasured counters (tokens, latency, cache hits, cost) must be recorded as `null` with explicit `"unknown"` status. An unknown counter must **never** be coerced or defaulted to `0`.
4. **Control Point Availability:** Surface control coverage values are drawn strictly from `{offered-proposed, not-offered, unknown}`. Blank, missing, or omitted cells are strictly forbidden. Unverified remote controls (e.g. Mobile Full Stop) are marked `unknown`, not attributed to unrelated decisions like D-036.
5. **Withdrawn Claims:** If a capability claim is dropped, it remains visible in the matrix with `withdrawn: true` and an explanation. Silently deleting rows or claims is an admission violation.
6. **Implementation Deferrals:** Marking a capability as deferred is an operational scheduling choice, not an accepted product removal. The row remains present with disposition `unmet`, `blocked`, or `no-release-claim`.
7. **Claim Permissions:** In draft proposals, all capability rows must declare `"claim_permission": "none"`. No public capability claim is authorized prior to founder release sign-off.

---

## 6. Gate Specification: check_release_journey_contract.py

### 6.1 Proposed Command
```text
python -X utf8 -B tools/check_release_journey_contract.py --candidate <FULL_SHA>
```

### 6.2 Admission and Execution Status
- **Status:** This command is **proposed and specified now, NOT implemented**.
- **Lifecycle Placement:** It is a **proposed child task** that requires **separate implementation admission under Gate Envelope G after a Rule-H receipt** freezes this contract digest. Freezing this contract does not constitute admission to implement the checker; downstream checker implementation requires its own separately admitted child issue, finite acceptance contract, and machine-checkable gate.
- **Self-Gating Prohibition:** Under Gate Envelope G, this contract does not use its own future existence as its gate. Manual, cross-family, and structural verification precede automated checker construction.
- **Draft Inspection Mode:** When implemented, the checker may provide a distinct `--draft` flag for non-gating structural schema validation of in-progress drafts. Running in acceptance mode (the default) strictly rejects any undecided rows.

### 6.3 Gate Inputs and Allowed Paths
- **Allowed Paths:** Only non-protected documentation and contract files:
  - `docs/v3/O1-CONTRACT.md`
  - `docs/v3/O1-JOURNEYS.md`
  - `docs/v3/O1-CAPABILITY-MATRIX.json`
  - `docs/v3/O1-DECISIONS-QUEUE.md`
  - `docs/v3/fixtures/O1/**` (subject to placement decision DQ-05)
- **Input Manifest and Unified Binding Specification:**
  One unified manifest binds all four contract artifacts, pinned repository sources, and test fixtures:
  1. Candidate git commit SHA (`<FULL_SHA>`).
  2. Exact SHA-256 digests of all four O1 contract artifacts:
     - `docs/v3/O1-CONTRACT.md`
     - `docs/v3/O1-JOURNEYS.md`
     - `docs/v3/O1-CAPABILITY-MATRIX.json`
     - `docs/v3/O1-DECISIONS-QUEUE.md`
  3. Pinned repository governance, decision, and specification sources:
     - `CONTROL_PLANE.md`
     - `ANCHOR.md`
     - `AGENTS.md`
     - `state/DECISIONS.md`
     - `docs/v3/PRODUCT.md`
     - `docs/v3/ACCEPTANCE.md`
     - `docs/v3/DECISION-REGISTER.md`
     - `docs/v3/program-control/MONTH-PROGRAM.md`
  4. Conformance test fixture files and inline fixture definitions (`docs/v3/fixtures/O1/**`).
  5. Linear comment ID and author human identity (`@lucassalvador987`) for the Rule H receipt.
  - **Stale-Binding Rejection Rule:** Any modification, missing file, or SHA-256 digest mismatch across ANY of the four covered artifacts, pinned sources, or test fixtures invalidates the candidate and triggers immediate nonzero exit (`O1E-010`). Changes to any covered input without an updated matching human receipt are strictly rejected.

### 6.4 Nonzero-Exit Conditions and Error IDs
The checker script must exit nonzero if any of the following conditions occur:

| Error ID | Error Category | Precise Nonzero-Exit Condition |
|---|---|---|
| **O1E-001** | Missing Pillar or Journey | Matrix omits any of the 12 pillars or catalog omits any of D-024's six journeys (J1–J6). |
| **O1E-002** | Missing Surface Control Coverage | A journey lacks a cell in the Journey × Surface × Control matrix, or any cell value is outside the declared vocabulary `{offered-proposed, not-offered, unknown}`. |
| **O1E-003** | Missing Target or Disposition | A capability row has `target: null` and does not declare an explicit recognized disposition (`unmet`, `blocked`, or `no-release-claim`). Draft marker `pending-H` fails this gate. |
| **O1E-004** | Unsupported Evidence Promotion | Synthetic or fixture evidence (such as mock stream fixtures or simulated execution traces) is assigned to support locally_qualified, release_qualified, or live evidence qualification, regardless of execution-record presence or authenticity. |
| **O1E-005** | Unknown Counter Coerced to Zero | An unmeasured or missing usage/performance counter is recorded as `0` instead of `null` with explicit `"unknown"` status. |
| **O1E-006** | Unauthorized Scope Deferral/Removal | An implementation deferral is treated as an accepted product scope removal without a founder decision receipt under Rule H. |
| **O1E-007** | Opaque Upstream State Transfer | A row claims state moves between native and upstream sessions without naming the transferred artifact, its format/schema URI, and a round-trip evidence ID. |
| **O1E-008** | Component Pass as Composed Outcome | A component-level unit test pass is presented as satisfying composed end-to-end journey acceptance. |
| **O1E-009** | Stale or Mismatched Receipt | A Rule-H receipt binding references an outdated proposal digest, an unauthorized author, or an unrelated issue key. |
| **O1E-010** | Stale or Mismatched Input Binding | Any of the four contract artifacts (contract, journeys, matrix, decisions queue), pinned sources, or fixtures has a mismatched, stale, or missing cross-referencing SHA-256 digest against the manifest. |
| **O1E-011** | Schema or JSON Syntax Violation | `docs/v3/O1-CAPABILITY-MATRIX.json` fails JSON validation or uses undeclared values outside the schema enums. |
| **O1E-012** | Dangling Identity Reference | Matrix row, journey, or fixture references an entity ID (e.g. dependency, test ref, requirement) that does not exist in the repository or baseline. |
| **O1E-013** | Unresolved Freeze-Blocking Seam | Contract claims freeze or acceptance while an admitted freeze-blocking open decision or architecture seam has no recorded disposition. |
| **O1E-014** | Absent Independent Verification Evidence | Candidate presented in acceptance mode without an authentic candidate-level independent verification report from a different model family (fails unconditionally in acceptance mode regardless of whether any capability rows are promoted), or capability claim promoted without authentic independent evidence, or receipt exports are not compared against actual Linear comments. |
| **O1E-015** | Unauthorized Populated Target | Row declares a populated target tier (`target != null`) while an authentic Rule-H founder receipt is absent. |
| **O1E-016** | Missing Mobile Capability-Row Coverage | A journey (J1–J6) is not mapped in the `journeys` array of mobile client capability row `CAP-016` in `docs/v3/O1-CAPABILITY-MATRIX.json`. |

---

## 7. Frozen Fixture Manifest

The following manifest defines the test suite for `tools/check_release_journey_contract.py`. Every error condition (`O1E-001` through `O1E-016`) is matched with at least one distinct adverse mutation fixture testing exactly its condition against concrete, complete inline baseline inputs.

### 7.1 Reproducible Baseline Definition
To guarantee deterministic execution and full reproducibility without relying on external state, all fixtures evaluate against a concrete, reproducible nominal baseline. Physical test files under `docs/v3/fixtures/O1/` remain deferred pending DQ-05.

#### Deterministic Construction Rule
The **Nominal Valid Baseline** is defined by an exact deterministic construction:
> `baseline = docs/v3/O1-CAPABILITY-MATRIX.json and O1-JOURNEYS.md at the frozen candidate SHA, with every row's disposition replaced by no-release-claim, all DQ-01..DQ-11 seams marked dispositioned (fixture), and the inline receipt/verifier records below`

Specifically, under this deterministic construction:
1. Every row from `docs/v3/O1-CAPABILITY-MATRIX.json` has its disposition replaced with `disposition: "no-release-claim"`, `disposition_status: "accepted-H"`, and `target: null`, ensuring zero undecided rows and zero unapproved claims in the nominal baseline.
2. The exact declaration of all 12 pillars and 35 capability rows (`CAP-001` through `CAP-034`, including `CAP-005b`) is preserved directly from the real matrix.
3. The complete Journey × Surface × Control matrix covering all 6 journeys (J1–J6) across CLI, TUI, GUI, and Mobile surfaces is preserved directly from `docs/v3/O1-JOURNEYS.md`.
4. All mutation fixtures (`O1F-N01a` through `O1F-N14b`) operate deterministically against this constructed baseline, naming rows and journeys that exist in the real matrix (e.g., `O1F-N06` deletes `CAP-015`, which exists in the real matrix; `O1F-N02b` removes `J2` from `CAP-016`).

#### Baseline Seam Dispositions
In the nominal valid baseline, every decision packet `DQ-01` through `DQ-11` in the decisions queue record (`docs/v3/O1-DECISIONS-QUEUE.md`) is recorded with status `'dispositioned (fixture)'` in the fixture queue record:
- `DQ-01`: `dispositioned (fixture)`
- `DQ-02`: `dispositioned (fixture)`
- `DQ-03`: `dispositioned (fixture)`
- `DQ-04`: `dispositioned (fixture)`
- `DQ-05`: `dispositioned (fixture)`
- `DQ-06`: `dispositioned (fixture)`
- `DQ-07`: `dispositioned (fixture)`
- `DQ-08`: `dispositioned (fixture)`
- `DQ-09`: `dispositioned (fixture)`
- `DQ-10`: `dispositioned (fixture)`
- `DQ-11`: `dispositioned (fixture)`

Recording all 11 decision packets as `'dispositioned (fixture)'` in the baseline fixture queue record guarantees that all freeze-blocking seams are deterministically closed in the nominal baseline. Consequently, `O1E-013` (Unresolved Freeze-Blocking Seam) does not fire in the nominal baseline or during unrelated adverse mutations, and fires exclusively in adverse fixture `O1F-N13`.

#### Deterministic Rebinding Rule
When evaluating adverse mutation fixtures, the fixture test harness executes in two deterministic phases:
1. **Mutation Application:** The designated mutation for the fixture is applied to the baseline data (e.g., removing a required pillar, setting an unknown counter to 0, or promoting synthetic evidence to live).
2. **Digest Rebinding:** Immediately after applying the mutation, the fixture harness deterministically recomputes all SHA-256 digests across all covered artifacts, pinned sources, and test payloads, and updates the manifest bindings to match the mutated content.
- **Rebinding Exception for O1F-N10:** In adverse fixture `O1F-N10`, content is mutated deliberately **WITHOUT** rebinding manifest SHA-256 digests.

Under this rebinding rule, manifest digest mismatch error `O1E-010` fires **only** in `O1F-N10`. Every other adverse fixture deterministically isolates and tests its designated error condition without collateral `O1E-010` failures.

#### Expected Results Specification
The checker script's evaluation output for each fixture is tested against an exact set of reported error IDs:
- **Positive Fixtures (`O1F-P01`–`O1F-P04`):** Expect exit code `0` and an empty error set (`PASS (exit 0, set())`).
- **Adverse Fixtures (`O1F-N01a`–`O1F-N14b`):** Expect exit code `1` and an **exact set** of reported error IDs. Each adverse fixture in this suite expects **precisely its one declared error ID as a singleton set** (e.g., `FAIL {'O1E-001'}`).
- **Exact Expected Sets:** Deterministic rebinding and fully dispositioned baseline seams isolate every adverse condition, so each adverse fixture expects exactly one error ID. The two exceptions are `O1F-N01a`, which deletes three rows along with the pillar and expects `{O1E-001, O1E-006}`, and `O1F-N01b`: deleting journey J6 also removes it from `CAP-016`, so it expects `{O1E-001, O1E-016}`. No error suppression or precedence is applied, and the checker reports the full set.

#### Manifest Canonical Serialization Specification
The manifest is serialized to canonical JSON before computing its SHA-256 digest:
- **Encoding:** UTF-8
- **Key Order:** Lexicographically sorted keys (`sort_keys=True`)
- **Separators:** No whitespace between keys, colons, and values: `separators=(',', ':')`
- **Trailing Newline:** Strictly omitted (no trailing newline `\n` or `\r\n`)
- **Python Serialization Procedure:**
  ```python
  manifest_bytes = json.dumps(manifest_object, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
  manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
  ```

#### Fixture-Only Trust Input
To allow fully reproducible offline testing of receipt and verifier validation without connecting to external Linear APIs, the fixture harness provides a fixture-only trust input: a **fixture trust list** naming the fixture-only founder identity `'fixture-founder'`:
- **Scope & Validity:** The identity `'fixture-founder'` is valid **ONLY** in fixture mode (when evaluating test fixtures or executing with `--fixture`).
- **Strict Prohibition Against Rule H Satisfaction:** The fixture trust input can **NEVER** satisfy real Rule H. In real candidate acceptance mode (`python tools/check_release_journey_contract.py --candidate <FULL_SHA>`), the checker strictly disallows and rejects any fixture trust list. Real acceptance unconditionally requires automated or verifier direct comparison against actual, unedited Linear comments authored by founder Lucas Guerra (`@lucassalvador987`).

#### Literal Baseline Fixture Artifacts and Digest Construction
For every digest placeholder used across the contract schemas and baseline traces, literal fixture artifact content is defined inline below. For each artifact, its digest is computed at check time as the SHA-256 of the exact bytes shown (with no trailing newline). No literal hex hashes are hardcoded in contract schemas or fixture payloads:

1. **`goal_prompt`:**
   ```text
   Task: Fix routing capability filtering bug and order providers by user weights.
   ```
   Digest: `sha256` of those exact UTF-8 bytes (no trailing newline), computed by the checker.

2. **`goal_spec`:**
   ```json
   {"action":"refactor_routing","goal_id":"GOAL-J1-001","target_module":"scripts/v3/harness/routing.py"}
   ```
   Digest: `sha256` of those exact UTF-8 bytes (no trailing newline), computed by the checker.

3. **`j1_patch`:**
   ```diff
   --- a/scripts/v3/harness/routing.py
   +++ b/scripts/v3/harness/routing.py
   @@ -1,3 +1,3 @@
   -# routing draft
   +# routing verified
   ```
   Digest: `sha256` of those exact UTF-8 bytes (no trailing newline), computed by the checker.

4. **`stale_draft`:**
   ```text
   # Stale Unapproved O1 Candidate Draft v0.0.0-obsolete
   ```
   Digest: `sha256` of those exact UTF-8 bytes (no trailing newline), computed by the checker (used in adverse fixture `O1F-N09a`).

5. **`fixture_exec_stdout`:**
   ```text
   test_fixture_execution_pass (scripts.tests.v3.harness.harness.test) ... ok
   ```
   Digest: `sha256` of those exact UTF-8 bytes (no trailing newline), computed by the checker (used in `genuine_fixture_execution_record_synth_01`).

6. **`state_transfer_inbound` and `state_transfer_outbound`:**
   ```json
   {"active_constraints":["no-hidden-api-keys","preserve-dirty-work"],"capsule_id":"capsule-20261023-001","checkpoint_epoch":4,"goal_spec_digest":"<computed:sha256(goal_spec)>","modified_files":["docs/v3/O1-CONTRACT.md"],"owner_intent":"Preserve task context across upstream CLI boundary","task_id":"task-j1-transfer-demo"}
   ```
   Digest: `sha256` of those exact UTF-8 canonical JSON bytes (no trailing newline), computed by the checker.

7. **`manifest`:**
   The literal object below, serialized as canonical JSON (UTF-8, sorted keys, separators `(",", ":")`, no trailing newline). Each `<computed:…>` value is replaced with the lowercase hex sha256 of the named input before the manifest itself is serialized. `<candidate:FULL_SHA>` is the exact 40-hex SHA passed to `--candidate`, in fixture mode and in real mode alike, so the verifier report and the manifest bind the same candidate.
   ```json
   {"artifacts":{"O1-CAPABILITY-MATRIX.json":"<computed:sha256(matrix)>","O1-CONTRACT.md":"<computed:sha256(contract)>","O1-DECISIONS-QUEUE.md":"<computed:sha256(queue)>","O1-JOURNEYS.md":"<computed:sha256(journeys)>"},"candidate_commit":"<candidate:FULL_SHA>","fixtures":{"goal_prompt":"<computed:sha256(goal_prompt)>","goal_spec":"<computed:sha256(goal_spec)>","j1_patch":"<computed:sha256(j1_patch)>","state_transfer_inbound":"<computed:sha256(state_transfer_inbound)>","state_transfer_outbound":"<computed:sha256(state_transfer_outbound)>"},"manifest_version":"o1-manifest/v0.1-draft","pinned_sources":{}}
   ```
   `pinned_sources` stays empty in the baseline. The real candidate manifest lists each pinned repository source path with its sha256 at freeze.

8. **Covered Artifacts (`O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, `O1-DECISIONS-QUEUE.md`):**
   In real candidate mode, computed as sha256 of exact bytes of the files on disk; in baseline fixture mode, computed over the modified baseline artifacts. The matrix is re-serialized as canonical JSON (UTF-8, sorted keys, separators `(",", ":")`, no trailing newline) after the baseline modifications. Each Markdown artifact is its exact candidate bytes, with the baseline's textual replacements applied in document order and no other normalization.

#### Digest Computation Rule
All cryptographic digests binding artifacts, pinned sources, and test payloads are **computed at check/freeze time as sha256 of the exact bytes on disk or in-memory literal fixture bytes**, represented in schemas and fixtures using explicit computation placeholders (e.g., `<computed:sha256(docs/v3/O1-CONTRACT.md)>`), never literal invented or unverified hexadecimal strings.

#### Fixture-Only Record Schemas (Inline)

##### Fixture Receipt Record Schema
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "FixtureReceiptRecordSchema",
  "type": "object",
  "properties": {
    "comment_id": {
      "type": "string",
      "pattern": "^receipt-[a-zA-Z0-9_-]+$"
    },
    "author": {
      "type": "string",
      "enum": ["fixture-founder"],
      "description": "In fixture mode, author is fixture-founder valid under the fixture trust list; real candidate acceptance requires lucassalvador987"
    },
    "issue_key": {
      "type": "string",
      "enum": ["CSL-205"]
    },
    "contract_sha256": {
      "type": "string",
      "description": "Computed at freeze as sha256 of docs/v3/O1-CONTRACT.md exact file bytes"
    },
    "journeys_sha256": {
      "type": "string",
      "description": "Computed at freeze as sha256 of docs/v3/O1-JOURNEYS.md exact file bytes"
    },
    "matrix_sha256": {
      "type": "string",
      "description": "Computed at freeze as sha256 of docs/v3/O1-CAPABILITY-MATRIX.json exact file bytes"
    },
    "decisions_queue_sha256": {
      "type": "string",
      "description": "Computed at freeze as sha256 of docs/v3/O1-DECISIONS-QUEUE.md exact file bytes"
    },
    "manifest_sha256": {
      "type": "string",
      "description": "Computed at freeze as sha256 of manifest exact bytes"
    },
    "scope": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 4
    },
    "selected_options": {
      "type": "object"
    },
    "rejected_options": {
      "type": "array",
      "items": { "type": "string" }
    },
    "rationale": {
      "type": "string"
    },
    "conditions": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 1
    },
    "revisit_triggers": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 1
    },
    "decided_on": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    }
  },
  "required": [
    "comment_id",
    "author",
    "issue_key",
    "contract_sha256",
    "journeys_sha256",
    "matrix_sha256",
    "decisions_queue_sha256",
    "manifest_sha256",
    "scope",
    "selected_options",
    "rejected_options",
    "rationale",
    "conditions",
    "revisit_triggers",
    "decided_on"
  ]
}
```

##### Fixture Verifier Report Record Schema
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "FixtureVerifierReportRecordSchema",
  "type": "object",
  "properties": {
    "report_id": {
      "type": "string",
      "pattern": "^VR-[a-zA-Z0-9_-]+$"
    },
    "verifier_model_family": {
      "type": "string",
      "description": "Must differ from drafting_model_family per CONTROL_PLANE.md"
    },
    "drafting_model_family": {
      "type": "string"
    },
    "candidate_commit": {
      "type": "string",
      "description": "Full 40-hex Git commit SHA evaluated"
    },
    "receipt_validation": {
      "type": "object",
      "properties": {
        "comment_id": { "type": "string" },
        "fixture_trust_input": {
          "type": "object",
          "properties": {
            "trust_list_identity": { "type": "string", "enum": ["fixture-founder"] },
            "valid_only_in_fixture_mode": { "type": "boolean", "const": true }
          },
          "required": ["trust_list_identity", "valid_only_in_fixture_mode"]
        },
        "author_confirmed": { "type": "string" },
        "unmodified_body": { "type": "boolean" }
      },
      "required": ["comment_id", "fixture_trust_input", "author_confirmed", "unmodified_body"]
    },
    "manifest_check": {
      "type": "object",
      "properties": {
        "artifacts_verified": {
          "type": "array",
          "items": { "type": "string" }
        },
        "artifact_digests": {
          "type": "object",
          "description": "sha256 of each of the four O1 artifacts, keyed by basename exactly as in the manifest `artifacts` map; compared against the candidate manifest",
          "minProperties": 4,
          "additionalProperties": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
        },
        "pinned_source_digests": {
          "type": "object",
          "description": "sha256 of each pinned governance/specification source, keyed by path; empty only in fixture baseline",
          "additionalProperties": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
        },
        "fixture_digests": {
          "type": "object",
          "description": "sha256 of each conformance test fixture, keyed by payload name exactly as in the manifest `fixtures` map (goal_prompt, goal_spec, j1_patch, state_transfer_inbound, state_transfer_outbound)",
          "additionalProperties": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
        },
        "digests_matched": { "type": "boolean" },
        "stale_entries": { "type": "integer", "maximum": 0 }
      },
      "required": ["artifacts_verified", "artifact_digests", "pinned_source_digests", "fixture_digests", "digests_matched", "stale_entries"]
    },
    "coverage_diff": {
      "type": "object",
      "properties": {
        "unmapped_requirements": { "type": "array", "maxItems": 0 },
        "unmapped_decisions": { "type": "array", "maxItems": 0 },
        "diff_empty": { "type": "boolean", "const": true }
      },
      "required": ["unmapped_requirements", "unmapped_decisions", "diff_empty"]
    },
    "protected_paths_check": {
      "type": "object",
      "properties": {
        "modified_protected_paths": { "type": "array", "maxItems": 0 },
        "clean": { "type": "boolean", "const": true }
      },
      "required": ["modified_protected_paths", "clean"]
    },
    "status": {
      "type": "string",
      "enum": ["PASS", "FAIL"]
    }
  },
  "required": [
    "report_id",
    "verifier_model_family",
    "drafting_model_family",
    "candidate_commit",
    "receipt_validation",
    "manifest_check",
    "coverage_diff",
    "protected_paths_check",
    "status"
  ]
}
```

#### Complete Inline Baseline Inputs

```json
{
  "receipt_record_baseline": {
    "comment_id": "receipt-csl205-rule-h-baseline-sample",
    "author": "fixture-founder",
    "issue_key": "CSL-205",
    "contract_sha256": "<computed:sha256(docs/v3/O1-CONTRACT.md)>",
    "journeys_sha256": "<computed:sha256(docs/v3/O1-JOURNEYS.md)>",
    "matrix_sha256": "<computed:sha256(docs/v3/O1-CAPABILITY-MATRIX.json)>",
    "decisions_queue_sha256": "<computed:sha256(docs/v3/O1-DECISIONS-QUEUE.md)>",
    "manifest_sha256": "<computed:sha256(manifest)>",
    "scope": [
      "docs/v3/O1-CONTRACT.md",
      "docs/v3/O1-JOURNEYS.md",
      "docs/v3/O1-CAPABILITY-MATRIX.json",
      "docs/v3/O1-DECISIONS-QUEUE.md"
    ],
    "selected_options": {
      "DQ-01": "dispositioned (fixture)",
      "DQ-02": "dispositioned (fixture)",
      "DQ-03": "dispositioned (fixture)",
      "DQ-04": "dispositioned (fixture)",
      "DQ-05": "dispositioned (fixture)",
      "DQ-06": "dispositioned (fixture)",
      "DQ-07": "dispositioned (fixture)",
      "DQ-08": "dispositioned (fixture)",
      "DQ-09": "dispositioned (fixture)",
      "DQ-10": "dispositioned (fixture)",
      "DQ-11": "dispositioned (fixture)"
    },
    "rejected_options": [],
    "rationale": "Freezes candidate contract and nominal baseline for downstream verification under Gate Envelope G with all baseline seams dispositioned.",
    "conditions": [
      "Fixture-only record; valid under the fixture trust list and never in acceptance mode."
    ],
    "revisit_triggers": [
      "Any change to a bound artifact digest or to the pinned-source set."
    ],
    "decided_on": "2026-10-23"
  },
  "verifier_report_baseline": {
    "report_id": "VR-CSL205-20261023-01",
    "verifier_model_family": "fixture-family-B",
    "drafting_model_family": "fixture-family-A",
    "candidate_commit": "<candidate:FULL_SHA>",
    "receipt_validation": {
      "comment_id": "receipt-csl205-rule-h-baseline-sample",
      "fixture_trust_input": {
        "trust_list_identity": "fixture-founder",
        "valid_only_in_fixture_mode": true
      },
      "author_confirmed": "fixture-founder",
      "unmodified_body": true
    },
    "manifest_check": {
      "artifacts_verified": [
        "docs/v3/O1-CONTRACT.md",
        "docs/v3/O1-JOURNEYS.md",
        "docs/v3/O1-CAPABILITY-MATRIX.json",
        "docs/v3/O1-DECISIONS-QUEUE.md"
      ],
      "artifact_digests": {
        "O1-CONTRACT.md": "<computed:sha256(contract)>",
        "O1-JOURNEYS.md": "<computed:sha256(journeys)>",
        "O1-CAPABILITY-MATRIX.json": "<computed:sha256(matrix)>",
        "O1-DECISIONS-QUEUE.md": "<computed:sha256(queue)>"
      },
      "pinned_source_digests": {},
      "fixture_digests": {
        "goal_prompt": "<computed:sha256(goal_prompt)>",
        "goal_spec": "<computed:sha256(goal_spec)>",
        "j1_patch": "<computed:sha256(j1_patch)>",
        "state_transfer_inbound": "<computed:sha256(state_transfer_inbound)>",
        "state_transfer_outbound": "<computed:sha256(state_transfer_outbound)>"
      },
      "digests_matched": true,
      "stale_entries": 0
    },
    "coverage_diff": {
      "unmapped_requirements": [],
      "unmapped_decisions": [],
      "diff_empty": true
    },
    "protected_paths_check": {
      "modified_protected_paths": [],
      "clean": true
    },
    "status": "PASS"
  },
  "composed_journey_trace_p04": {
    "trace_id": "TRACE-J1-COMPOSED-NOMINAL-01",
    "journey": "J1",
    "mode": "combined",
    "stages": [
      {
        "stage_index": 1,
        "role": "native_head",
        "action": "goal_spec_compilation",
        "input_digest": "<computed:sha256(goal_prompt)>",
        "output_digest": "<computed:sha256(goal_spec)>",
        "status": "PASS"
      },
      {
        "stage_index": 2,
        "role": "native_arm",
        "action": "workspace_scaffolding_and_static_analysis",
        "worktree": "worktrees/j1-stage2",
        "status": "PASS"
      },
      {
        "stage_index": 3,
        "role": "upstream_claude",
        "action": "delegated_refactoring_turn",
        "argv": ["claude", "-p", "--stream-json"],
        "token_allowance_ceiling": 10000,
        "status": "PASS"
      },
      {
        "stage_index": 4,
        "role": "native_broker",
        "action": "stream_monitoring_and_token_redaction",
        "redacted_tokens_count": 0,
        "status": "PASS"
      },
      {
        "stage_index": 5,
        "role": "native_kernel",
        "action": "workspace_re_adoption_and_verification",
        "diff_digest": "<computed:sha256(j1_patch)>",
        "tests_executed": ["scripts/tests/v3/harness/harness.test.py"],
        "status": "PASS"
      }
    ],
    "verification_status": "PASS",
    "accounting": {
      "native_tokens": 1200,
      "upstream_tokens": 4850,
      "recovery_reservation_tokens": 605,
      "unmeasured_counters": null
    }
  },
  "synthetic_evidence_EV_SYNTH_01": {
    "id": "EV-SYNTH-01",
    "evidence_class": "source_documented",
    "type": "synthetic_mock_fixture",
    "source_ref": "docs/v3/harness/CSL-84.md",
    "notes": "Synthetic mock stream fixture for provider-free offline harness validation; cannot satisfy locally_qualified or release_qualified live evidence gates per Invariant 7 and Rule 6"
  },
  "genuine_fixture_execution_record_synth_01": {
    "evidence_id": "EV-SYNTH-01-EXEC",
    "evidence_class": "locally_observed",
    "execution_type": "offline_fixture_execution",
    "run_id": "run-fixture-synth-exec-001",
    "command": "python -X utf8 -B scripts/tests/v3/harness/harness.test.py",
    "exit_code": 0,
    "stdout_digest": "<computed:sha256(fixture_exec_stdout)>",
    "executed_at": "2026-10-21T16:00:00Z",
    "authenticity": "genuine_offline_mock_execution",
    "notes": "Authentic local execution record of an offline mock/fixture trial. While the execution record is authentic and genuine, it documents mock fixture execution and cannot support locally_qualified or release_qualified live tiers."
  },
  "usage_tracking_record_n05": {
    "task_id": "task-j6-metering-baseline",
    "provider": "anthropic",
    "route": "claude-code-stream",
    "tokens_consumed": null,
    "tokens_consumed_status": "unknown",
    "estimated_cost_usd": null,
    "estimated_cost_status": "unknown",
    "recovery_reservation_tokens": 1500,
    "cooldown_active": false
  },
  "state_transfer_p03": {
    "artifact_id": "transfer-capsule-j1-nominal",
    "schema_uri": "urn:recursion:schema:v3:context-capsule-transfer-v1",
    "schema": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "ContextCapsuleTransferPayload",
      "type": "object",
      "properties": {
        "active_constraints": {"type": "array", "items": {"type": "string"}},
        "capsule_id": {"type": "string"},
        "checkpoint_epoch": {"type": "integer", "minimum": 1},
        "goal_spec_digest": {"type": "string"},
        "modified_files": {"type": "array", "items": {"type": "string"}},
        "owner_intent": {"type": "string"},
        "task_id": {"type": "string"}
      },
      "required": ["active_constraints", "capsule_id", "checkpoint_epoch", "goal_spec_digest", "modified_files", "owner_intent", "task_id"]
    },
    "serialization_specification": {
      "format": "canonical JSON",
      "encoding": "UTF-8",
      "key_order": "lexicographically sorted",
      "whitespace": "none (separators=(',', ':'))",
      "python_serializer": "json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')",
      "digest_procedure": "sha256 of exact UTF-8 encoded canonical JSON bytes with sorted keys and no whitespace"
    },
    "inbound_payload": {
      "active_constraints": ["no-hidden-api-keys", "preserve-dirty-work"],
      "capsule_id": "capsule-20261023-001",
      "checkpoint_epoch": 4,
      "goal_spec_digest": "<computed:sha256(goal_spec)>",
      "modified_files": ["docs/v3/O1-CONTRACT.md"],
      "owner_intent": "Preserve task context across upstream CLI boundary",
      "task_id": "task-j1-transfer-demo"
    },
    "expected_returned_payload": {
      "active_constraints": ["no-hidden-api-keys", "preserve-dirty-work"],
      "capsule_id": "capsule-20261023-001",
      "checkpoint_epoch": 4,
      "goal_spec_digest": "<computed:sha256(goal_spec)>",
      "modified_files": ["docs/v3/O1-CONTRACT.md"],
      "owner_intent": "Preserve task context across upstream CLI boundary",
      "task_id": "task-j1-transfer-demo"
    },
    "round_trip_record": {
      "evidence_id": "EV-ROUNDTRIP-SYNTH-01",
      "evidence_class": "source_documented",
      "status": "expected synthetic result, not executed",
      "serialization_format": "application/json; canonical=true",
      "inbound_sha256": "<computed:sha256(state_transfer_inbound)>",
      "outbound_sha256": "<computed:sha256(state_transfer_outbound)>",
      "lossless": true,
      "notes": "Expected synthetic result, not executed. Synthetic serialization-deserialization round trip procedure defined against fixture-local Context Capsule payload with canonical UTF-8 JSON encoding; explicitly synthetic per O1E-007"
    }
  }
}
```

### 7.2 Manifest of Positive and Adverse Test Fixtures

| Fixture ID | Fixture Class | Description / Test Intent | Deterministic Mutation / Condition Against Baseline | Expected Result |
|---|---|---|---|---|
| **O1F-P01** | Positive | Nominal valid baseline | Complete baseline constructed deterministically from matrix and journeys at frozen candidate SHA with all rows dispositioned as no-release-claim, all DQ-01..DQ-11 seams marked 'dispositioned (fixture)', all artifact SHA-256 digests computed, and verified fixture Rule-H receipt record `receipt_record_baseline`. | **PASS** (exit 0, set()) |
| **O1F-P02** | Positive | Explicit non-offered / unknown cell | Nominal baseline setting offline redisplay to `"not-offered"` per D-036, and mobile Full Stop to `"unknown"`. | **PASS** (exit 0, set()) |
| **O1F-P03** | Positive | Transparent state transfer | Row specifies fixture-local payload `transfer-capsule-j1-nominal`, schema URI `urn:recursion:schema:v3:context-capsule-transfer-v1`, canonical JSON serialization, expected returned payload, and round-trip record `EV-ROUNDTRIP-SYNTH-01` (labeled 'expected synthetic result, not executed'). | **PASS** (exit 0, set()) |
| **O1F-P04** | Positive | Multi-stage composed journey | Journey J1 composed trace from native initiation to Claude execution to native resumption (trace `composed_journey_trace_p04`). | **PASS** (exit 0, set()) |
| **O1F-N01a** | Adverse | Missing required pillar | Delete the `"nodes"` pillar and all its associated rows (`CAP-011`, `CAP-012`, `CAP-013`) from constructed baseline matrix. | **FAIL** `{'O1E-001', 'O1E-006'}` (deleting CAP-011..CAP-013 also removes three rows without a `no-release-claim` disposition; both are expected, no suppression) |
| **O1F-N01b** | Adverse | Missing required journey | Delete journey `"J6"` from Journey Catalog and matrix row references. | **FAIL** `{'O1E-001', 'O1E-016'}` (removing J6 from CAP-016's `journeys` necessarily also triggers mobile-row coverage; both are expected, no suppression) |
| **O1F-N02a** | Adverse | Missing surface control cell | Omit mobile column or leave control cell blank in Journey × Surface × Control matrix for J1. | **FAIL** `{'O1E-002'}` |
| **O1F-N02b** | Adverse | Missing journey in mobile capability row | Remove journey `"J2"` from `CAP-016`'s journeys array in baseline matrix while retaining J2's cells in Journey × Surface × Control matrix. | **FAIL** `{'O1E-016'}` |
| **O1F-N03a** | Adverse | Undecided row (missing target & disposition) | Mutate row CAP-001 to `target: null` and `disposition: null` with `disposition_status: "pending-H"`. | **FAIL** `{'O1E-003'}` |
| **O1F-N03b** | Adverse | Target populated without Rule H | Set `target: "release_qualified"` on row CAP-001 while Rule-H receipt binding is absent or null. | **FAIL** `{'O1E-015'}` |
| **O1F-N04** | Adverse | Synthetic without execution record promoted to live | Assign synthetic evidence `EV-SYNTH-01` without an execution record to support a `locally_qualified` or `release_qualified` maturity tier on CAP-001. | **FAIL** `{'O1E-004'}` |
| **O1F-N04b** | Adverse | Synthetic with genuine execution record promoted to live | Assign synthetic evidence `EV-SYNTH-01` accompanied by genuine execution record `genuine_fixture_execution_record_synth_01` to support a `locally_qualified` or `release_qualified` maturity tier on CAP-001. | **FAIL** `{'O1E-004'}` |
| **O1F-N05** | Adverse | Unknown counter set to zero | In usage tracking record `usage_tracking_record_n05`, mutate `tokens_consumed: null` (status `"unknown"`) to `tokens_consumed: 0`. | **FAIL** `{'O1E-005'}` |
| **O1F-N06** | Adverse | Deferral treated as deletion | Silently delete `CAP-015` (Desktop GUI Client) from matrix rows in constructed baseline instead of recording disposition `"no-release-claim"`. | **FAIL** `{'O1E-006'}` |
| **O1F-N07** | Adverse | Opaque upstream state transfer | Mutate state transfer specification by setting `schema_uri: ""` and deleting `evidence_id` in `round_trip_record`. | **FAIL** `{'O1E-007'}` |
| **O1F-N08** | Adverse | Component pass as journey proof | Present unit test `scripts/tests/v3/harness/ops.test.py` as proof of Journey J1 completion. | **FAIL** `{'O1E-008'}` |
| **O1F-N09a** | Adverse | Stale proposal digest in receipt | Bind a receipt containing SHA-256 digest of literal `stale_draft` fixture content (`<computed:sha256(stale_draft)>`) instead of candidate contract content. | **FAIL** `{'O1E-009'}` |
| **O1F-N09b** | Adverse | Unauthorized receipt author | Bind a receipt authored by non-founder account or non-trusted identity (e.g. `"unauthorized-user"`) failing the fixture trust list. | **FAIL** `{'O1E-009'}` |
| **O1F-N09c** | Adverse | Wrong-scope receipt issue key | Bind a receipt comment from Linear issue `"CSL-117"` to the CSL-205 contract. | **FAIL** `{'O1E-009'}` |
| **O1F-N10** | Adverse | Stale or mismatched input binding | Mutate matrix JSON, journeys catalog, or decisions queue WITHOUT rebinding manifest SHA-256 digests (violates deterministic rebinding rule). | **FAIL** `{'O1E-010'}` |
| **O1F-N11** | Adverse | Undeclared enum value or syntax error | Inject undeclared maturity tier `"candidate-contract"` into row CAP-001 or a JSON trailing comma syntax error. | **FAIL** `{'O1E-011'}` |
| **O1F-N12** | Adverse | Dangling identity reference | Add dependency reference pointing to nonexistent ID `CAP-999` or nonexistent decision `D-999`. | **FAIL** `{'O1E-012'}` |
| **O1F-N13** | Adverse | Unresolved freeze-blocking seam | Mutate decision packet DQ-01 in fixture queue record to remove `'dispositioned (fixture)'`, asserting contract freeze while seam remains open without disposition. | **FAIL** `{'O1E-013'}` |
| **O1F-N14a** | Adverse | Absent candidate verifier report (no promotion) | Baseline candidate with all rows dispositioned (`disposition: "no-release-claim"`, zero promoted rows), but verifier report `verifier_report_baseline` is absent or null in acceptance mode. | **FAIL** `{'O1E-014'}` |
| **O1F-N14b** | Adverse | Absent independent evidence on promoted row | Capability claim promoted (`CAP-001` `target: "release_qualified"`) without an authentic independent verification report from a different model family or without receipt export comparison against actual Linear comments. | **FAIL** `{'O1E-014'}` |

---

## 8. Independent Verification Requirements

Under Gate Envelope G and [CONTROL_PLANE.md](../../CONTROL_PLANE.md) § "Independent verifiers", verification must satisfy the following criteria:

1. **Mandatory Cross-Family Verification & Unconditional Candidate Report:** The verifier report **must** be produced by an independent verifier from a **different model family** than the drafting agent (e.g., Anthropic Claude vs. OpenAI Codex or Google Gemini). A verification report produced by the same model family cannot satisfy this gate. Candidate-level independent verification is mandatory unconditionally in acceptance mode regardless of promotion: an otherwise complete candidate where all rows are dispositioned as `unmet`, `blocked`, or `no-release-claim` without any promoted claims must still fail acceptance (`O1E-014`) if an authentic independent verifier report is absent.
2. **Receipt Verification Against Linear:** The independent verifier must export and independently inspect candidate receipts, directly comparing them against actual Linear comments (verifying comment ID, author human identity, exact unedited comment body, and timestamp).
3. **Artifact and Manifest Binding:** The verifier report must bind:
   - The full 40-hex candidate commit SHA.
   - The exact SHA-256 digests of all four O1 artifacts (`O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, and `O1-DECISIONS-QUEUE.md`).
   - The SHA-256 digests of pinned governance/specification sources and conformance test fixtures per Section 6.3.
4. **Envelope G Verification Checks:** The verifier must explicitly inspect for:
   - Zero dangling identity references across rows, journeys, and decisions.
   - Zero unresolved freeze-blocking seams without recorded disposition.
   - Presence of authentic candidate-level independent verifier report unconditionally in acceptance mode.
   - Presence of authentic independent evidence for every promoted claim.
5. **Execution Ledger:** The report must list every manifest fixture (`O1F-P01`–`P04`, `O1F-N01a`–`N14b`), recording:
   - The exact command executed.
   - The expected exit code and error ID.
   - The observed exit code and error ID.
6. **Coverage Diff:** The verifier must independently reconstruct requirement coverage from the pinned inventory and perform a set diff against `O1-CAPABILITY-MATRIX.json`. The row-level inventory is OR-021–OR-039 plus the decisions in D-018–D-039 that bind capabilities. D-024, D-026, D-027, D-031, D-032, D-033 and D-034 bind no single matrix row (journey set, licence, counsel, positioning and similar scope-level decisions). They are excluded from the row-level diff and listed in the verifier report as `out_of_row_scope`, so they neither appear in `unmapped_decisions` nor block an empty diff.
7. **Pass Condition:** Verification passes **only** when:
   - All manifest fixtures match expected results exactly.
   - The coverage diff is completely empty.
   - No protected paths were touched.

---

## 9. Human Acceptance (Rule H)

As established in [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205) and [CSL-204](https://linear.app/csl-laboratories/issue/CSL-204):

1. **Authorship:** Only a human-authored comment on the matching Linear issue ([CSL-205](https://linear.app/csl-laboratories/issue/CSL-205)) can approve a product decision, research continuation, or acceptance into the normative product contract.
2. **Authorized Decider:** Lucas Guerra, or an explicitly delegated human acting within recorded authority under [CONTROL_PLANE.md](../../CONTROL_PLANE.md) D-041.
3. **Mandatory Comment Bindings:** The human comment must bind:
   - Comment ID and author human identity (`@lucassalvador987`).
   - Scoped delegation record (if not Lucas).
   - Exact candidate SHA-256 digests of all four artifacts (`O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, and `O1-DECISIONS-QUEUE.md`).
   - Explicit scope of acceptance.
   - Selected options and rejected options.
   - Substantive rationale.
   - Conditions and revisit triggers.
   - Calendar date (`YYYY-MM-DD`).
4. **Non-Qualifying Actions:** Labels, issue status changes (e.g., moving to "Done"), PR approvals, GitHub merges, bot relays, and green test runs do **not** qualify as acceptance under Rule H.
5. **Scope of Acceptance:** Human closure of CSL-205 accepts this contract, the journey catalog, the capability matrix, and the decisions queue. It does **not** constitute a release decision or authorize deployment.

---

## 10. Supplemental Structural Checks

In addition to this contract's proposed future journey checker, candidates maintain clean passes on existing repository program-control checks:
```text
python -X utf8 -B tools/check_program_control.py
python -X utf8 -B tools/test_program_control.py
```
> [!IMPORTANT]
> **Actual Scope of Program-Control Checks vs. O1 Structural Inspection:**  
> The existing candidate checks (`tools/check_program_control.py` and `tools/test_program_control.py`) validate the repository-level program-control ledger established under [CSL-204](https://linear.app/csl-laboratories/issue/CSL-204) (`docs/v3/program-control/MONTH-PROGRAM.json` and `MONTH-PROGRAM.md`). **They do not inspect or validate these O1 draft artifacts** (`O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, `O1-DECISIONS-QUEUE.md`).  
> 
> Inspection of O1 drafts is a distinct, separate validation scope:  
> 1. In-progress drafts undergo schema validation against the capability matrix JSON schema and cross-family review, during which draft marker `disposition_status: "pending-H"` is permitted.  
> 2. Acceptance qualification is enforced by the proposed release journey checker upon its separate admission after Rule H, which strictly rejects any undecided rows.  
> 
> Program-control checks verify ledger consistency across the repository; they neither authenticate human authorship nor inspect O1 internal tables, and they cannot substitute for the positive acceptance gate.
