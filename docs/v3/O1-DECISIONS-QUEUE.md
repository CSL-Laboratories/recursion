# O1 Decisions Queue and Evidence Manifest

**Version:** `o1-decisions-queue v0.1-draft`  
**Status:** DRAFT proposal under envelope G; nothing accepted until a rule-H receipt  
**Parent Contract:** [O1-CONTRACT.md](O1-CONTRACT.md)  
**Parent Issue:** [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205) (Outcome O1)  
**Author / Proposer:** Chaandan Saini (@chaandansaini101)  
**Target Decider:** Lucas Guerra (@lucassalvador987) under Rule H  
**Checkpoint Freeze Date:** October 23, 2026 (W2) · Month Checkpoint: November 12, 2026

---

## 1. Overview and Decision Framework

Under [CONTROL_PLANE.md](../../CONTROL_PLANE.md) and [AGENTS.md](../../AGENTS.md), agents and interactive sessions are bounded executors. They recommend and propose; founders decide. 

This document collects the open questions flagged in Chaandan's owner plan (Linear comment `99cd914d`, Oct 17) and status update (comment `d9068e29`, Oct 21) into structured, numbered decision packets (`DQ-01` through `DQ-11`). 

### Rule H Receipt Protocol
No proposed option is accepted into the normative product contract until Lucas Guerra (or an explicitly delegated human acting within recorded authority under [CONTROL_PLANE.md](../../CONTROL_PLANE.md) D-041) posts an authenticated human receipt on [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205). Per [O1-CONTRACT.md](O1-CONTRACT.md) §6.3, the receipt binds one unified manifest covering all four artifacts, pinned sources, and test fixtures, with strict rejection of stale or modified inputs:
- Linear Comment ID and author identity (`@lucassalvador987`).
- Exact candidate SHA-256 digests of `O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, and this queue (`O1-DECISIONS-QUEUE.md`).
- SHA-256 digests of pinned governance/specification sources and conformance test fixtures.
- Selected options and rejected alternatives with explicit rationale.
- Revisit triggers and calendar date (`YYYY-MM-DD`).

Until Rule-H acceptance, only the three specific defaults confirmed in comment `d9068e29` are active:
1. Target maturity tiers for all capability rows default to `null` (DQ-02).
2. Conformance fixtures reside beside documentation at `docs/v3/fixtures/O1/` (DQ-05).
3. Contract artifacts reside at `docs/v3/O1-*` (DQ-08).
All other decision packets carry **no default** and remain open, pending founder decisions.

---

## 2. Numbered Decision Packets

### Decision Packet DQ-01: Maturity Tier Vocabulary
- **Question:** Which maturity scale and graduation vocabulary should govern capability matrix promotion criteria across the month program?
- **Options Considered:**
  - **Option A (Kubernetes PRR / KEP-1194 Stage Model):** Adopt an `alpha` / `beta` / `GA` stage model with explicit gating criteria for each promotion step (observability, supportability, and rollback gates per KEP-1194/KEP-5241).
  - **Option B (8-Tier Delivery Progression - Proposal Only):** Propose the monotonic eight-tier scale from [MONTH-PROGRAM.md](program-control/MONTH-PROGRAM.md#2-capability-and-release-matrix): `researched` → `human_decided` → `contract_frozen` → `prototyped` → `implemented` → `provider_free_verified` → `locally_qualified` → `release_qualified`. This vocabulary remains a candidate proposal under Envelope G, distinct from accepted O1 semantics until ratified.
  - **Option C (Binary Status):** Simplify to `in-progress` / `verified` / `released`. *(Strong countercase: collapses vital distinctions between synthetic fixtures and live OS qualification).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` left vocabulary unanswered; Option B is a proposal under review, not an established default).
- **Who Decides:** Lucas Guerra (Tier-1 architecture/contract foundation).
- **What It Blocks:** Freezing the schema for `docs/v3/O1-CAPABILITY-MATRIX.json` and the checker script validation rules (`tools/check_release_journey_contract.py`).
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-02: Target Tiers per Row for November 12 Checkpoint
- **Question:** What are the accepted target maturity tiers for each capability row for the Thursday, November 12, 2026 checkpoint?
- **Options Considered:**
  - **Option A (Comprehensive Ambition):** Target `release_qualified` for all 12 pillars. *(Countercase: high risk of unearned claims given zero-research budget and fixed month timeline).*
  - **Option B (Samir's Integrated Slice / CSL-237 Proposal):** Set `release_qualified` targets exclusively for the core integrated coding journey (J1 on native plus one upstream route, e.g. Claude Code/Codex), targeting `provider_free_verified` or `locally_qualified` for plugins/nodes/clients, and marking advanced distributed features `no-release-claim` for Nov 12.
  - **Option C (Defer All Targets):** Leave targets unassigned indefinitely. *(Countercase: fails Lucas's request in comment 05760f87 for an explicit output and acceptance gate before W2 freeze).*
- **Current Default (d9068e29):** All row `target` values remain `null` pending Lucas's Rule-H receipt. (Undecided rows with `target: null` and missing disposition fail acceptance gate `O1E-003`).
- **Who Decides:** Lucas Guerra (release scope and quality targets).
- **What It Blocks:** R1 release qualification planning ([CSL-237](https://linear.app/csl-laboratories/issue/CSL-237)) and W3/W4 execution prioritization.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-03: Capability Deferrals and Disposition
- **Question:** How should capabilities that will not reach `release_qualified` status by November 12 be formally classified in the matrix?
- **Options Considered:**
  - **Option A (Explicit Dispositions - Recommended):** Every non-target row explicitly carries one of three human-admitted dispositions: `unmet`, `blocked`, or `no-release-claim`. Deferrals remain visible and are never treated as silent product removals. Undecided rows fail acceptance gate `O1E-003`.
  - **Option B (Silent Deletion):** Remove out-of-scope rows from the matrix to present a smaller initial release. *(Strictly rejected by [state/DECISIONS.md](../../state/DECISIONS.md#d-019--release-model-build-comprehensively-ship-lean-install-opt-in-dot-option-b-rejected) D-019 and D-038).*
  - **Option C (Blank Dispositions):** Leave disposition blank/null. *(Fails gate check `O1E-003`).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` did not select a disposition policy; Option A proposed under Envelope G).
- **Who Decides:** Lucas Guerra (release boundary authority).
- **What It Blocks:** CSL-237 R1 release boundary ratification.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-04: Release and Launch Claims Wording
- **Question:** What public claims, tagline, and launch phrasing are authorized for the initial v3 platform release?
- **Options Considered:**
  - **Option A (Product Thesis Tagline - Recommended):** Authorize only the exact wording from [state/DECISIONS.md](../../state/DECISIONS.md#d-034--product-thesis-the-harness-that-scales-with-your-workflow) D-034: *"Recursion — the harness that scales with your workflow."* No public claims of universal quota savings or competitor superiority are permitted without workload-specific empirical evidence (D-018, D-031).
  - **Option B (Expanded Marketing Claims):** Claim automated cost reduction and provider parity. *(Violates Invariant 7 and D-031).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Draft proposals set `"claim_permission": "none"` across all rows until founder sign-off).
- **Who Decides:** Lucas Guerra (company public posture and release approval).
- **What It Blocks:** Launch page copy and external communication in [CSL-242](https://linear.app/csl-laboratories/issue/CSL-242).
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-05: Conformance Fixture Placement and CI Architecture
- **Question:** Where should physical contract and journey fixture files (positive and adverse) reside, given current repository test discovery rules?
- **Current CI Context:** Under current `tools/ci_gate.py` (lines 189–196, CSL-376), docs-only diffs do **not** skip test suites; the gate explicitly runs the full contract suite on every diff. Fixture placement is therefore evaluated on architectural cleanliness and path protection rather than skip workarounds.
- **Options Considered:**
  - **Option A (Beside Docs - Recommended Default):** Place physical fixtures at `docs/v3/fixtures/O1/`. Preserves clean architectural cohesion beside contract documents; keeps `tools/` and `scripts/` untouched.
  - **Option B (Under Tools Directory):** Place fixtures at `tools/contract_fixtures/O1/`. Co-locates fixtures with the future checker script, but mixes documentation contracts with harness code.
  - **Option C (Protected Path Amendment):** Open an authorized governance authoring session under D-042 to create a specialized test path.
- **Current Default (d9068e29):** Option A — fixtures beside docs (`docs/v3/fixtures/O1/`).
- **Who Decides:** Lucas Guerra (protected paths and CI configuration).
- **What It Blocks:** Placement of physical fixture files for `tools/check_release_journey_contract.py`.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-06: CI Spend and Cross-Platform Dispatch Policy
- **Question:** When and how should pull requests in the O1 lane run full cross-platform matrix checks (`windows` and `macos` jobs) via the `ci:full` label?
- **Current CI Context:** Per CSL-408, GitHub Actions CI runs on free GitHub-hosted runners. The required check is `linux`. `windows` and `macos` runs are free but consume hosted runner queue capacity and add latency.
- **Options Considered:**
  - **Option A (Linux Required Only - Recommended Default):** Run the required free `linux` job on ordinary PRs. Reserve `ci:full` Windows/macOS runs for the W2 freeze milestone (Oct 23) and final pre-merge qualification to conserve developer queue latency.
  - **Option B (ci:full on Every Push):** Apply `ci:full` label to all O1 PRs. *(Countercase: creates unnecessary queue backlogs and developer turnaround delays on documentation tranches).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` did not rule on CI dispatch; Option A proposed).
- **Who Decides:** Lucas Guerra (company resource reservation under D-041).
- **What It Blocks:** CI dispatch policy on CSL-205 and downstream qualification PRs.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-07: Research Budget Status for the Parallel Runs
- **Question:** CSL-205 sets the additional-research budget at zero Parallel runs. How should these be classified?
  - The Parallel Search citation pass for comment `99cd914d`.
  - Run A (`trun_db705696fec649d88275fe040d179bd0`, cited in `d9068e29`).
  - Run B (`trun_db705696fec649d8b8d783d0a437434d`, the Oct 21 deep-research run used for this draft).
- **Options Considered:**
  - **Option A (proposal):** Treat all three as citation support for synthesizing the pinned sources, under the team rule that research comes first, rather than as research continuations.
  - **Option B:** Record them as exceptions to the zero-run budget, with a human receipt.
  - **Option C:** Exclude Run B's sources from the contract and rely only on the pinned repository and Linear evidence.
- **Cost:** Unknown. No per-run cost has been verified.
- **Current Default:** None. Pending a human decision under Rule H. The drafts call these runs citation support, but that is a proposal, not an accepted classification.
- **Who Decides:** Lucas Guerra (spend and research budget), or a delegated human.
- **What It Blocks:** Whether the Run B sources can stay in the frozen contract, and CSL-205 research accounting.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-08: Target File Repository Location
- **Question:** Should the O1 artifacts reside permanently under `docs/v3/` or move into `docs/v3/program-control/`?
- **Options Considered:**
  - **Option A (docs/v3/ - Recommended Default):** Keep files at `docs/v3/O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, and `O1-DECISIONS-QUEUE.md`. Standard Git review rules apply.
  - **Option B (docs/v3/program-control/):** Place files under `docs/v3/program-control/`. That directory is SHA-256 pinned with strict `eol=lf` enforcement under CSL-204, requiring Lucas's direct maintenance as program-control owner.
- **Current Default (d9068e29):** Option A — files reside at `docs/v3/O1-*`.
- **Who Decides:** Lucas Guerra (program-control architecture).
- **What It Blocks:** Downstream file path stability for automated links.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-09: O1 / O2 / O6 Sequencing and Order
- **Question:** In what sequence should the foundational objective contracts freeze relative to one another?
- **Options Considered:**
  - **Option A (Concurrent Draft, Staged Freeze - Recommended):** Draft O1, O2 (CSL-206), and O6 (CSL-210) concurrently. Freeze O1 (scope and journeys) first on Oct 23; O2 inherits O1's journeys for common semantics; O6 inherits O1's matrix structure for evidence promotion rules.
  - **Option B (Strict Serial Order):** Halt O2 and O6 until O1 has a merged Rule-H receipt. *(Countercase: compresses remaining development time before Nov 12).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` did not settle sequencing; Option A proposed).
- **Who Decides:** Any founder (Chaandan, Samir, or Lucas under D-041).
- **What It Blocks:** Downstream milestone alignment for CSL-206 and CSL-210.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-10: CSL-242 Launch Page Claim Permissions
- **Question:** How does the capability matrix claim column bind public marketing and documentation claims in [CSL-242](https://linear.app/csl-laboratories/issue/CSL-242)?
- **Options Considered:**
  - **Option A (Strict Claim Gating - Recommended):** CSL-242 may only publish claims for capabilities where `claim_permission` is explicitly set to `"authorized"` by a founder receipt and `current_maturity` is `release_qualified`. All draft rows stay `"claim_permission": "none"`.
  - **Option B (Aspirational Marketing):** Permit launch page copy to describe roadmap features as current capabilities. *(Rejected per [CONTROL_PLANE.md](../../CONTROL_PLANE.md) Invariant 7).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Draft proposals set `"claim_permission": "none"` across all rows).
- **Who Decides:** Lucas Guerra.
- **What It Blocks:** Public launch copywriting in CSL-242.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

### Decision Packet DQ-11: CSL-237 R1 Release Boundary Proposal
- **Question:** Shall Samir's proposed R1 release boundary rule (Linear comment `37f33ff1` on [CSL-237](https://linear.app/csl-laboratories/issue/CSL-237)) be adopted as the release gate for the November 12 checkpoint?
- **Context & Correction:** Samir's recommendation is strictly his open proposal; it is **not** an established default. Lucas selected an "integrated end-to-end path" demonstration for the Oct 20 milestone; the specific journey/slice and the R1 release boundary remain unapproved and undecided.
- **Samir's Proposed Rule:**
  1. A row is inside R1 only if a **composed** journey has qualifying evidence on the Nov 12 candidate. Component passes and synthetic fixtures do not qualify.
  2. Every other row gets an explicit disposition: `unmet`, `blocked`, or `no-release-claim`. Nothing is dropped silently.
  3. Proposed core of R1: An integrated coding journey running natively plus on one upstream harness route. All other D-024 journeys default to `no-release-claim` unless supported by evidence by Nov 5.
  4. Provider, host, and spend access remain separately approved.
- **Options Considered:**
  - **Option A (Adopt Samir's Rule):** Ratify Samir's boundary rule as the official acceptance criterion for CSL-237.
  - **Option B (Amend Boundary Rule):** Expand or contract the core release journey requirement.
- **Current Default:** **None / Pending founder decision under Rule H (undecided).** Samir's recommendation is an open proposal; no default has been selected or approved.
- **Who Decides:** Lucas Guerra (Tier-1 release boundary decision).
- **What It Blocks:** Freezing the scope cut for R1 qualification in CSL-237.
- **Exact-Digest Binding:** `sha256: TBD at freeze under Rule H`

---

## 3. Evidence Manifest

The following sources were consumed and bound during the drafting of this specification. All citations are primary repository documents, verified Linear comments, or authenticated research traces:

### 3.1 Linear Issues and Owner Plan Records
- **CSL-205 Body:** Issue `75033598-1cec-475f-9cf3-0baecd75f9fc`, "Define month-end journeys and the capability/release evidence contract" (Oct 16, 2026).
- **CSL-205 Comment 99cd914d:** Chaandan Saini owner plan for O1 (Oct 17, 2026, 09:15 UTC).
- **CSL-205 Comment d9068e29:** Chaandan Saini status and defaults update (Oct 21, 2026, 20:53 UTC).
- **CSL-205 Comment 05760f87:** Lucas Guerra request for explicit output and acceptance gate (Oct 16, 2026, 19:21 UTC).
- **CSL-237 Body & Comments:** Issue `ff2492df-6c02-4111-924d-b09781fd8f84`, R1 release boundary definition; comment `37f33ff1` from Samir (Oct 21, 2026, 07:27 UTC).
- **Parent Issues:** [CSL-45](https://linear.app/csl-laboratories/issue/CSL-45) (Month Program parent), [CSL-204](https://linear.app/csl-laboratories/issue/CSL-204) (Program control layer), [CSL-81](https://linear.app/csl-laboratories/issue/CSL-81) / [CSL-27](https://linear.app/csl-laboratories/issue/CSL-27) (Harness slice).

### 3.2 Repository Governance and Architecture
- [CONTROL_PLANE.md](../../CONTROL_PLANE.md): Precedence rules, founder authority split (D-041), developer-approved comments (D-043), protected paths, non-negotiable invariants.
- [ANCHOR.md](../../ANCHOR.md): Single-product v3 harness orchestrator scope, anti-lock-in thesis.
- [AGENTS.md](../../AGENTS.md): Linear-first operating rules, proceed-or-escalate criteria.
- [state/DECISIONS.md](../../state/DECISIONS.md): Entries D-017 through D-043, specifically:
  - [D-018](../../state/DECISIONS.md#d-018--thesis-recursion-is-a-native-harness-and-a-unifying-wrapper-no-ecosystem-lock-in): Thesis and unproved savings.
  - [D-019](../../state/DECISIONS.md#d-019--release-model-build-comprehensively-ship-lean-install-opt-in-dot-option-b-rejected): Comprehensive release model (opt-in plugins).
  - [D-020](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06): Single-node core, monotonic fencing.
  - [D-021](../../state/DECISIONS.md#d-021--delegation-over-credential-intermediation): Credential non-intermediation.
  - [D-022](../../state/DECISIONS.md#d-022--one-pausestop-vocabulary-across-config-formal-model-and-broker-g-07): Pause/Stop vocabulary (Aggressive / Full / Lazy).
  - [D-023](../../state/DECISIONS.md#d-023--first-qualified-upstream-profile): Codex App Server first qualification.
  - [D-024](../../state/DECISIONS.md#d-024--frontier-claim-productive-continuity): Productive continuity moat / six journeys.
  - [D-025](../../state/DECISIONS.md#d-025--continuing-autonomous-research-is-an-optional-bounded-plugin): Bounded autonomous research plugin.
  - [D-028](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11): Platform floors and power-loss recovery.
  - [D-029](../../state/DECISIONS.md#d-029--operator-provisioned-trusted-launch-boundary-strong-credential-non-intermediation): Trusted launch boundary.
  - [D-034](../../state/DECISIONS.md#d-034--product-thesis-the-harness-that-scales-with-your-workflow): Scaling thesis.
  - [D-036](../../state/DECISIONS.md#d-036--mobile-offline-retention-online-only-private-view-o-01): Mobile online-only private view.
  - [D-037](../../state/DECISIONS.md#d-037--conformance-fixtures-authorized-python-as-disposable-test-harness-only-o-02): Disposable test harness in Python.
  - [D-038](../../state/DECISIONS.md#d-038--d-034d-019-supersede-older-first-release-guimobile-exclusions-o-03): GUI/mobile supersession.
  - [D-039](../../state/DECISIONS.md#d-039--accept-the-oslanguage-expansion-as-advisory-plan-a-language-agnostic-architecture-o-04): Language-agnostic architecture.

### 3.3 Product Specifications and Requirement Records
- [PRODUCT.md](PRODUCT.md): Requirements OR-021 through OR-039, capability map Q1–Q10.
- [ACCEPTANCE.md](ACCEPTANCE.md): Four platform scenarios (website preview, bug fix, interrupted refactor, upstream-only), candidate gate rules.
- [DECISION-REGISTER.md](DECISION-REGISTER.md): Direction rulings OR-001 through OR-039.
- [MONTH-PROGRAM.md](program-control/MONTH-PROGRAM.md): Program-level gaps, candidate maturity tiers, rolling-wave checkpoints C0–C5.
- [harness/CSL-27.md](harness/CSL-27.md), [CSL-82.md](harness/CSL-82.md), [CSL-83.md](harness/CSL-83.md), [CSL-84.md](harness/CSL-84.md): Executable harness slice, process broker, routing weights, cooldowns, stream parsers, synthetic fixtures.

### 3.4 Parallel Research Runs

Two Parallel runs back this draft. Whether they count as citation support or as research runs under the issue's zero-run budget is open in DQ-07. Calling them citation support is a proposal.

**Run A, cited in Linear (`trun_db705696fec649d88275fe040d179bd0`)**, recorded in status comment `d9068e29`. The owner plan `99cd914d` cites an earlier Parallel Search pass with overlapping sources. The URLs below are the ones those two comments list:
- Kubernetes PRR (KEP-1194): https://www.kubernetes.dev/resources/keps/1194
- Kubernetes feature gates (stage model): https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates
- KEP template: https://github.com/kubernetes/enhancements/blob/master/keps/NNNN-kep-template/README.md
- KEP-5241 (beta promotion) and enhancements #6203, from `99cd914d`: https://www.kubernetes.dev/resources/keps/5241/ , https://github.com/kubernetes/enhancements/issues/6203
- OPA policy language (fail closed): https://openpolicyagent.org/docs/policy-language
- SLSA provenance, verification summary and artifact verification: https://slsa.dev/spec/v1.2/provenance , https://slsa.dev/verification_summary , https://slsa.dev/spec/v1.2/verifying-artifacts
- in-toto release predicate: https://github.com/in-toto/attestation/blob/main/spec/predicates/release.md
- Atlan feature maturity matrix, from `99cd914d`. This is vendor documentation, cited only as an example: https://docs.atlan.com/get-started/references/feature-maturity-matrix

**Run B, used for this draft (`trun_db705696fec649d8b8d783d0a437434d`, Oct 21)** is deep research on stage vocabularies, fail-closed gates, frozen fixture manifests, digest binding, unknown vs zero vs not-offered values, and claim permission by evidence tier. The PR body's Research (Parallel) section cites only Run B URLs. Sources it returned:
- https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates , https://www.kubernetes.dev/resources/keps/1194 , https://github.com/kubernetes/community/blob/main/sig-architecture/production-readiness.md , https://github.com/kubernetes/enhancements/blob/master/keps/NNNN-kep-template/README.md
- https://www.openpolicyagent.org/docs/operations , https://openpolicyagent.org/docs/cli , https://www.openpolicyagent.org/docs/policy-language , https://www.conftest.dev/options/
- https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md , https://slsa.dev/spec/v1.2/provenance , https://slsa.dev/spec/v1.2/verifying-artifacts , https://slsa.dev/spec/v1.0/verification_summary
- https://json-schema.org/understanding-json-schema/reference/null , https://json-schema.org/understanding-json-schema/reference/conditionals
- https://www.ftc.gov/news-events/news/press-releases/2025/04/ftc-order-requires-workado-back-artificial-intelligence-detection-claims , https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents , https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf

None of these sources sets a requirement for Recursion. The stages, error IDs and evidence tiers in this draft are proposals.
