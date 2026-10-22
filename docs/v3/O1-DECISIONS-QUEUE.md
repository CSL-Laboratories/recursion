# O1 Decisions Queue and Evidence Manifest

**Version:** `o1-decisions-queue v0.1-draft`<br>
**Status:** DRAFT proposal under envelope G; nothing accepted until a rule-H receipt<br>
**Parent Contract:** [O1-CONTRACT.md](O1-CONTRACT.md)<br>
**Parent Issue:** [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205) (Outcome O1)<br>
**Author / Proposer:** Chaandan Saini (@chaandansaini101)<br>
**Target Decider:** Lucas Guerra (`@lucassalvador987`), or a human he explicitly delegates under Rule H<br>
**Checkpoint Freeze Date:** 2026-10-23 (W2) · Month Checkpoint: 2026-11-12

---

## 1. Overview and Decision Framework

Under [CONTROL_PLANE.md](../../CONTROL_PLANE.md) and [AGENTS.md](../../AGENTS.md), agents and interactive sessions are bounded executors. They recommend and propose; founders decide.

This document collects the open questions flagged in Chaandan's owner plan (Linear comment `99cd914d`, 2026-10-17) and status update (comment `d9068e29`, 2026-10-21) into structured, numbered decision packets (`DQ-01` through `DQ-11`).

### Rule H Receipt Protocol
No proposed option is accepted into the normative product contract until Lucas Guerra (`@lucassalvador987`), or a human he explicitly delegates under Rule H ([CONTROL_PLANE.md](../../CONTROL_PLANE.md) D-041), posts an authenticated human receipt on [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205). Per [O1-CONTRACT.md](O1-CONTRACT.md) §6.3 and §9, the receipt binds the candidate commit, one unified manifest, every pin, and every artifact and fixture digest. Matching only the four artifact digests is not a receipt. Stale or modified inputs are rejected:
- Linear comment ID and author identity. The author is Lucas Guerra (`@lucassalvador987`), or a human he explicitly delegates under Rule H.
- `candidate_commit`: the full 40-hex SHA of the candidate.
- `manifest_sha256`: the SHA-256 of the canonical unified manifest. The manifest carries the candidate, the artifact digests, the pin digests, and the fixture digests.
- Exact SHA-256 digests of `O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, and this queue (`O1-DECISIONS-QUEUE.md`).
- `pinned_source_sha256` for every §6.3 source, including `docs/v3/harness/CSL-27.md`, `CSL-82.md`, `CSL-83.md`, `CSL-84.md`, and `docs/v3/O1-CAPABILITY-MATRIX.schema.json`.
- `fixture_sha256` for every manifest fixture payload.
- Selected options and rejected alternatives with explicit rationale.
- Revisit triggers and calendar date (`YYYY-MM-DD`).

Until Rule-H acceptance, only the three specific defaults confirmed in comment `d9068e29` are active:
1. Target maturity tiers for all capability rows default to `null` (DQ-02).
2. Conformance fixtures reside beside documentation at `docs/v3/fixtures/O1/` (DQ-05).
3. Contract artifacts reside at `docs/v3/O1-*` (DQ-08).
All other decision packets carry **no default** and remain open, pending founder decisions.

Each packet's **Exact-Digest Binding** is the SHA-256 of the packet's own UTF-8 bytes. The hashed bytes run from the `### Decision Packet` heading through the Decision Deadline line, inclusive, with LF newlines. The Exact-Digest Binding line is not part of the hashed bytes. The digest is lowercase hex.


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
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: a13d760b08a79d814ffa1ce4dd19003ca2edec3d6cc214acb1a8cea19ab76836`

---

### Decision Packet DQ-02: Target Tiers per Row for 2026-11-12 Checkpoint
- **Question:** What are the accepted target maturity tiers for each capability row for the 2026-11-12 checkpoint?
- **Options Considered:**
  - **Option A (Comprehensive Ambition):** Target `release_qualified` for all 12 pillars. *(Countercase: high risk of unearned claims given zero-research budget and fixed month timeline).*
  - **Option B (Samir's Integrated Slice / CSL-237 Proposal):** Set `release_qualified` targets exclusively for the core integrated coding journey (J1 on native plus one upstream route, e.g. Claude Code/Codex), targeting `provider_free_verified` or `locally_qualified` for plugins/nodes/clients, and marking advanced distributed features `no-release-claim` for 2026-11-12.
  - **Option C (Defer All Targets):** Leave targets unassigned indefinitely. *(Countercase: fails Lucas's request in comment 05760f87 for an explicit output and acceptance gate before W2 freeze).*
- **Current Default (d9068e29):** All row `target` values remain `null` pending a Rule H receipt from Lucas Guerra, or from a human he explicitly delegates under Rule H. (Undecided rows with `target: null` and missing disposition fail acceptance gate `O1E-003`).
- **Who Decides:** Lucas Guerra (release scope and quality targets).
- **What It Blocks:** R1 release qualification planning ([CSL-237](https://linear.app/csl-laboratories/issue/CSL-237)) and W3/W4 execution prioritization.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: 05aa796f595b67190cc2ef950353b9fa3e24ecf6a12211f13a2506219e2031d7`

---

### Decision Packet DQ-03: Capability Deferrals and Disposition
- **Question:** How should capabilities that will not reach `release_qualified` status by 2026-11-12 be formally classified in the matrix?
- **Options Considered:**
  - **Option A (Explicit Dispositions - Recommended):** Every non-target row explicitly carries one of three human-admitted dispositions: `unmet`, `blocked`, or `no-release-claim`. Deferrals remain visible and are never treated as silent product removals. Undecided rows fail acceptance gate `O1E-003`.
  - **Option B (Silent Deletion):** Remove out-of-scope rows from the matrix to present a smaller initial release. *(Strictly rejected by [state/DECISIONS.md](../../state/DECISIONS.md#d-019--release-model-build-comprehensively-ship-lean-install-opt-in-dot-option-b-rejected) D-019 and D-038).*
  - **Option C (Blank Dispositions):** Leave disposition blank/null. *(Fails gate check `O1E-003`).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` did not select a disposition policy; Option A proposed under Envelope G).
- **Who Decides:** Lucas Guerra (release boundary authority).
- **What It Blocks:** CSL-237 R1 release boundary ratification.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: 6e5a9386634ce9c0ffd1ff6080e577e80911ac27b7dbc3171668896d4d4241a6`

---

### Decision Packet DQ-04: Release and Launch Claims Wording
- **Question:** What public claims, tagline, and launch phrasing are authorized for the initial v3 platform release?
- **Options Considered:**
  - **Option A (Product Thesis Tagline - Recommended):** Authorize only the exact wording from [state/DECISIONS.md](../../state/DECISIONS.md#d-034--product-thesis-the-harness-that-scales-with-your-workflow) D-034: *"Recursion — the harness that scales with your workflow."* No public claims of universal quota savings or competitor superiority are permitted without workload-specific empirical evidence (D-018, D-031).
  - **Option B (Expanded Marketing Claims):** Claim automated cost reduction and provider parity. *(Violates Invariant 7 and D-031).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Draft proposals set `"claim_permission": "none"` across all rows until founder sign-off).
- **Who Decides:** Lucas Guerra (company public posture and release approval).
- **What It Blocks:** Launch page copy and external communication in [CSL-242](https://linear.app/csl-laboratories/issue/CSL-242).
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: 87b522ec88927821c46ead1558b8e0f0f7795006b2d3af22cd62be625fd9e361`

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
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: 809aac02e78b1811c61a9d9c029e29e7880997e65a521f88c90669ce5a64172b`

---

### Decision Packet DQ-06: CI Spend and Cross-Platform Dispatch Policy
- **Question:** When and how should pull requests in the O1 lane run full cross-platform matrix checks (`windows` and `macos` jobs) via the `ci:full` label?
- **Current CI Context:** Per CSL-408, GitHub Actions CI runs on free GitHub-hosted runners. The required check is `linux`. `windows` and `macos` runs are free but consume hosted runner queue capacity and add latency.
- **Options Considered:**
  - **Option A (Linux Required Only - Recommended Default):** Run the required free `linux` job on ordinary PRs. Reserve `ci:full` Windows/macOS runs for the W2 freeze milestone (2026-10-23) and final pre-merge qualification to conserve developer queue latency.
  - **Option B (ci:full on Every Push):** Apply `ci:full` label to all O1 PRs. *(Countercase: creates unnecessary queue backlogs and developer turnaround delays on documentation tranches).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` did not rule on CI dispatch; Option A proposed).
- **Who Decides:** Lucas Guerra (company resource reservation under D-041).
- **What It Blocks:** CI dispatch policy on CSL-205 and downstream qualification PRs.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: d3b4885c3352a974748ba3b39b8997b183272178a569e51bbc3ba13dc183ef8b`

---

### Decision Packet DQ-07: Recorded Research Deviation (Zero Additional Runs)
- **Question:** CSL-205 allows zero additional Parallel runs. Parallel Search (comment `99cd914d`), Run A (`trun_db705696fec649d88275fe040d179bd0`), and Run B (`trun_db705696fec649d8b8d783d0a437434d`, 2026-10-21) already happened. How is that deviation signed off?
- **Not a Dependency:** This contract, its rows, its gates, its fixtures, and its freeze do not depend on Parallel Search, Run A, or Run B. Normative sources are the pinned repository files and the Linear records in §3. Removing or disputing the runs changes no journey, row, or gate.
- **Deviation:** The three activities are a deviation from the zero-run budget. They require sign-off by Lucas Guerra (`@lucassalvador987`), or by a human he explicitly delegates under Rule H. Until that receipt they stay an unsigned deviation. They authorize no further run and no continuation.
- **Options Considered:**
  - **Option A:** Sign the deviation off as citation support that stays outside the normative contract.
  - **Option B:** Sign the deviation off as an exception to the zero-run budget, with no further runs.
  - **Option C:** Leave the deviation unsigned. The runs stay out of the normative evidence set. The contract still stands on the pinned repository files and Linear records.
- **Cost:** Unknown. No per-run cost has been verified. This packet requests no further spend.
- **Current Default:** None. The runs are an unsigned deviation, not a dependency and not an accepted classification.
- **Who Decides:** Lucas Guerra (`@lucassalvador987`), or a human he explicitly delegates under Rule H.
- **What It Blocks:** Only the accounting classification of this deviation. It blocks no catalog row, journey, fixture, or freeze.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** None under CSL-205. The budget remains zero additional runs. This packet does not request Parallel Search, Run A, Run B, or a new run.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: a713c147ac6f8d9379df74985ebfda4f2c64595ed2219f5549bcf3b20321f657`

---

### Decision Packet DQ-08: Target File Repository Location
- **Question:** Should the O1 artifacts reside permanently under `docs/v3/` or move into `docs/v3/program-control/`?
- **Options Considered:**
  - **Option A (docs/v3/ - Recommended Default):** Keep files at `docs/v3/O1-CONTRACT.md`, `O1-JOURNEYS.md`, `O1-CAPABILITY-MATRIX.json`, and `O1-DECISIONS-QUEUE.md`. Standard Git review rules apply.
  - **Option B (docs/v3/program-control/):** Place files under `docs/v3/program-control/`. That directory is SHA-256 pinned with strict `eol=lf` enforcement under CSL-204, requiring Lucas's direct maintenance as program-control owner.
- **Current Default (d9068e29):** Option A — files reside at `docs/v3/O1-*`.
- **Who Decides:** Lucas Guerra (program-control architecture).
- **What It Blocks:** Downstream file path stability for automated links.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: d5e783ae5be8126b569a956162a22d1bfdf1c37e55fb061cc004194b0a339b34`

---

### Decision Packet DQ-09: O1 / O2 / O6 Sequencing and Order
- **Question:** In what sequence should the foundational objective contracts freeze relative to one another?
- **Options Considered:**
  - **Option A (Concurrent Draft, Staged Freeze - Recommended):** Draft O1, O2 (CSL-206), and O6 (CSL-210) concurrently. Freeze O1 (scope and journeys) first on 2026-10-23; O2 inherits O1's journeys for common semantics; O6 inherits O1's matrix structure for evidence promotion rules.
  - **Option B (Strict Serial Order):** Halt O2 and O6 until O1 has a merged Rule-H receipt. *(Countercase: compresses remaining development time before 2026-11-12).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Status comment `d9068e29` did not settle sequencing; Option A proposed).
- **Who Decides:** Any founder (Chaandan, Samir, or Lucas under D-041).
- **What It Blocks:** Downstream milestone alignment for CSL-206 and CSL-210.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: e2dd20534fa9265d7001c565fed0391a1d37070917318a7223ea6d14267e4805`

---

### Decision Packet DQ-10: CSL-242 Launch Page Claim Permissions
- **Question:** How does the capability matrix claim column bind public marketing and documentation claims in [CSL-242](https://linear.app/csl-laboratories/issue/CSL-242)?
- **Options Considered:**
  - **Option A (Strict Claim Gating - Recommended):** CSL-242 may only publish claims for capabilities where `claim_permission` is explicitly set to `"authorized"` by a founder receipt and `current_maturity` is `release_qualified`. All draft rows stay `"claim_permission": "none"`.
  - **Option B (Aspirational Marketing):** Permit launch page copy to describe roadmap features as current capabilities. *(Rejected per [CONTROL_PLANE.md](../../CONTROL_PLANE.md) Invariant 7).*
- **Current Default:** **None / Pending founder decision under Rule H.** (Draft proposals set `"claim_permission": "none"` across all rows).
- **Who Decides:** Lucas Guerra.
- **What It Blocks:** Public launch copywriting in CSL-242.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: 0f836ebd8c60e25827ccd5b4fd0348c29a41c43c6580efbb158028c8eca10b41`

---

### Decision Packet DQ-11: CSL-237 R1 Release Boundary Proposal
- **Question:** Shall Samir's proposed R1 release boundary rule (Linear comment `37f33ff1` on [CSL-237](https://linear.app/csl-laboratories/issue/CSL-237)) be adopted as the release gate for the 2026-11-12 checkpoint?
- **Context & Correction:** Samir's recommendation is strictly his open proposal; it is **not** an established default. Lucas selected an "integrated end-to-end path" demonstration for the 2026-10-20 milestone; the specific journey/slice and the R1 release boundary remain unapproved and undecided.
- **Samir's Proposed Rule:**
  1. A row is inside R1 only if a **composed** journey has qualifying evidence on the 2026-11-12 candidate. Component passes and synthetic fixtures do not qualify.
  2. Every other row gets an explicit disposition: `unmet`, `blocked`, or `no-release-claim`. Nothing is dropped silently.
  3. Proposed core of R1: An integrated coding journey running natively plus on one upstream harness route. All other D-024 journeys default to `no-release-claim` unless supported by evidence by 2026-11-05.
  4. Provider, host, and spend access remain separately approved.
- **Options Considered:**
  - **Option A (Adopt Samir's Rule):** Ratify Samir's boundary rule as the official acceptance criterion for CSL-237.
  - **Option B (Amend Boundary Rule):** Expand or contract the core release journey requirement.
- **Current Default:** **None / Pending founder decision under Rule H (undecided).** Samir's recommendation is an open proposal; no default has been selected or approved.
- **Who Decides:** Lucas Guerra (Tier-1 release boundary decision).
- **What It Blocks:** Freezing the scope cut for R1 qualification in CSL-237.
- **Reversibility:** Reversible before the Rule-H receipt. After it, a change needs a new receipt and a new digest binding.
- **Further Research Cost:** Unknown; no further paid research is proposed for this packet.
- **Decision Deadline:** 2026-10-23 (W2 freeze) to be included in the frozen candidate. A later ruling moves the freeze. The 2026-11-12 checkpoint is the program window, not this deadline.
- **Exact-Digest Binding:** `sha256: d61ad74014913c0aea2f7b622e6ae184db2e8e484d31ec6bec882413c3942291`

---

## 3. Evidence Manifest

The following sources were consumed while drafting this specification. Normative sources are the pinned repository files and the Linear records below. Parallel Search and Runs A and B are not normative sources.

### Source pins

Every repository source below is pinned to the commit that last changed it and to the SHA-256 of its exact bytes on that revision. Mutable paths are not cited by branch tip alone. Parallel Search and Runs A and B are not in this table. They are the DQ-07 deviation in §3.4, not dependencies.

| Path | Revision | sha256 |
|---|---|---|
| `CONTROL_PLANE.md` | `cb9ca415c65bbba02f035cf40d074803d61f54fc` | `73ec2f799280e2167461e7f3de6172e6fe54456ea2e7955d7832bcf03dc8ab45` |
| `ANCHOR.md` | `dbb6b7a03f10ae8a119ca84aac38a805a3297ed1` | `0975e9151c1bafa3bd67d66fed061078b1eb75e6b808c65a7f4bfe851fc8b0e5` |
| `AGENTS.md` | `eb0cde2067463097eca147c960e2c44fed857584` | `f18a383410650d7c1370a88969d1177a25a4f23a7e07e467df46ae2e1b81b2f9` |
| `state/DECISIONS.md` | `cb9ca415c65bbba02f035cf40d074803d61f54fc` | `1a8c092085cbe1ecf5d69fc72dbde349686d7648562d7f62e2214ec41c6e1002` |
| `docs/v3/PRODUCT.md` | `3dce709e4afe6acd4c0e3755341a737a6846de0a` | `c794aa54e2bbe28746fe958fc690fbdda35512719f63fc7bd25c09a0419d708a` |
| `docs/v3/ACCEPTANCE.md` | `32c8a421c1476d50e430a6f40ef783f2be9ea478` | `1235beeb5bdecc6caadd07b777df22f58d4574ce70a2c1bfe2e3bab6ed7bc0a9` |
| `docs/v3/DECISION-REGISTER.md` | `e62a7bb97a4916eae90cbdc1e3e151bafb6181fd` | `3344cbd8aa603753d9f71f596a7ab773dba5201cec7ee41e9671e3331d08221f` |
| `docs/v3/program-control/MONTH-PROGRAM.md` | `64af2f3cbff7400faa2892c6fe3a278445e894dc` | `da91484250b4fff0193ac36a7d500588e1782c2bb4379206506757da47dc2bfb` |
| `docs/v3/harness/CSL-27.md` | `4a9dcef677380e40262ac49b435b37f6d2184cfc` | `145028de3613557b70492cdc0b27a65b2bfb226b03a592aa8f7c2c662f343fee` |
| `docs/v3/harness/CSL-82.md` | `46dd8326fc9d22953d28bd215db6ffbd5dd0fb75` | `514dee5226705678f9530eb0c040459eb7ca1c754ff9e41096165c822ae5b43a` |
| `docs/v3/harness/CSL-83.md` | `65a9c236b3c3f08ba74fb5d88b28b2b33024a5eb` | `e54536dd3d01906dc3d6296a63bb92d21b937103f9385bfd67453742c97ed8a7` |
| `docs/v3/harness/CSL-84.md` | `05e8657aecec5b85f1b9b66ee8b0fad5c2c73309` | `4657f483fb119f6b6e57a226fba65c7e0643241b35c41d5f5fb4c87bb92211ad` |
| `docs/v3/O1-CAPABILITY-MATRIX.schema.json` | `24da263d49ee167d6d1849f90de46e89af612451` | `bc55d6e7009fbc4cdfc3d1d03f1e583cf08b794c5a208eb173b506fe6ef9ee7e` |
| `docs/v3/research-lane/scope-reframe-20261002/raw/01-current-strategy/Recursion_Interface_and_Product_Strategy.md` | `0f89d356f6170164287211c746923a7b5824ccc1` | `ecbe910dd4070a4c59d85315bb42f67e7e405fb5ab12f6306a95de2b197b5b2b` |

The four harness rows and `docs/v3/O1-CAPABILITY-MATRIX.schema.json` are §6.3 real-mode manifest pins. The schema revision is `24da263d49ee167d6d1849f90de46e89af612451`, the commit that added that file. The gate hashes the candidate bytes. A later edit of the schema must move this revision and recompute the sha256.

### 3.1 Linear Issues and Owner Plan Records
- **CSL-205 Body:** Issue `75033598-1cec-475f-9cf3-0baecd75f9fc`, "Define month-end journeys and the capability/release evidence contract" (2026-10-16).
- **CSL-205 Comment 99cd914d:** Chaandan Saini owner plan for O1 (2026-10-17, 09:15 UTC).
- **CSL-205 Comment d9068e29:** Chaandan Saini status and defaults update (2026-10-21, 20:53 UTC).
- **CSL-205 Comment 05760f87:** Lucas Guerra request for explicit output and acceptance gate (2026-10-16, 19:21 UTC).
- **CSL-237 Body & Comments:** Issue `ff2492df-6c02-4111-924d-b09781fd8f84`, R1 release boundary definition; comment `37f33ff1` from Samir (2026-10-21, 07:27 UTC).
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
  - [D-026](../../state/DECISIONS.md#d-026--recursions-own-license-and-public-posture): Apache-2.0 posture and early plugin contract.
  - [D-027](../../state/DECISIONS.md#d-027--counsel-engagement-authorized-packet-only-no-spend): Counsel packet. No agent spend.
  - [D-028](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11): Platform floors and power-loss recovery.
  - [D-029](../../state/DECISIONS.md#d-029--operator-provisioned-trusted-launch-boundary-strong-credential-non-intermediation): Trusted launch boundary.
  - [D-031](../../state/DECISIONS.md#d-031--product-positioning-no-vendor-sabotage-claim): No vendor-sabotage claim.
  - [D-032](../../state/DECISIONS.md#d-032--parent-goal-is-v3-sweep-prompt-templates): Parent goal is v3.
  - [D-033](../../state/DECISIONS.md#d-033--plugin-importport-hypothesis-validate-do-not-assume): Plugin porting is a hypothesis.
  - [D-034](../../state/DECISIONS.md#d-034--product-thesis-the-harness-that-scales-with-your-workflow): Scaling thesis. Out of row scope. Composition obligations stay in force.
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

### 3.4 Recorded deviation: Parallel Search and Runs A and B

CSL-205 allows zero additional Parallel runs. The activities below already happened. DQ-07 records them as a deviation that needs sign-off from Lucas Guerra (`@lucassalvador987`), or from a human he explicitly delegates under Rule H. They are not a dependency of this contract, of any row, or of any gate. Normative sources are the pinned files in the table above and the Linear records in §3.1–§3.3 and §3.5.

**Parallel Search**, cited in comment `99cd914d`, overlaps the Run A URLs.

**Run A (`trun_db705696fec649d88275fe040d179bd0`)**, recorded in status comment `d9068e29`. The URLs below are the ones those comments list. They are a deviation log:
- Kubernetes PRR (KEP-1194): https://www.kubernetes.dev/resources/keps/1194
- Kubernetes feature gates (stage model): https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates
- KEP template: https://github.com/kubernetes/enhancements/blob/master/keps/NNNN-kep-template/README.md
- KEP-5241 (beta promotion) and enhancements #6203, from `99cd914d`: https://www.kubernetes.dev/resources/keps/5241/ , https://github.com/kubernetes/enhancements/issues/6203
- OPA policy language (fail closed): https://openpolicyagent.org/docs/policy-language
- SLSA provenance, verification summary and artifact verification: https://slsa.dev/spec/v1.2/provenance , https://slsa.dev/verification_summary , https://slsa.dev/spec/v1.2/verifying-artifacts
- in-toto release predicate: https://github.com/in-toto/attestation/blob/main/spec/predicates/release.md
- Atlan feature maturity matrix, from `99cd914d`. This is vendor documentation, cited only as an example: https://docs.atlan.com/get-started/references/feature-maturity-matrix

**Run B (`trun_db705696fec649d8b8d783d0a437434d`, 2026-10-21)** returned the URLs below. They are a deviation log, not a requirement and not a dependency. Sources it returned:
- https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates , https://www.kubernetes.dev/resources/keps/1194 , https://github.com/kubernetes/community/blob/main/sig-architecture/production-readiness.md , https://github.com/kubernetes/enhancements/blob/master/keps/NNNN-kep-template/README.md
- https://www.openpolicyagent.org/docs/operations , https://openpolicyagent.org/docs/cli , https://www.openpolicyagent.org/docs/policy-language , https://www.conftest.dev/options/
- https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md , https://slsa.dev/spec/v1.2/provenance , https://slsa.dev/spec/v1.2/verifying-artifacts , https://slsa.dev/spec/v1.0/verification_summary
- https://json-schema.org/understanding-json-schema/reference/null , https://json-schema.org/understanding-json-schema/reference/conditionals
- https://www.ftc.gov/news-events/news/press-releases/2025/04/ftc-order-requires-workado-back-artificial-intelligence-detection-claims , https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents , https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf

None of these sources sets a requirement for Recursion. The stages, error IDs and evidence tiers in this draft are proposals. The runs do not gate the freeze.

### 3.5 Scope-reframe strategy report §7

CSL-205 names §7 of the 2026-10-02 scope-reframe strategy report as the research basis for the six journeys. The file pin is in the table above. This subsection pins §7 itself.

- **Path:** [Recursion_Interface_and_Product_Strategy.md](research-lane/scope-reframe-20261002/raw/01-current-strategy/Recursion_Interface_and_Product_Strategy.md) §7, heading `## 7 Six annotated demonstration journeys`.
- **Revision:** `0f89d356f6170164287211c746923a7b5824ccc1` (last change of that file).
- **File sha256:** `ecbe910dd4070a4c59d85315bb42f67e7e405fb5ab12f6306a95de2b197b5b2b`.
- **§7 sha256:** `27a8c21f4d08cef2a5b3ad4307b81d0f3422971aee9e8adc4df77f93c3c9fe71`. The hashed bytes start at the §7 heading and stop before `## 8 Prioritized experiments and implementation handoffs`, LF newlines, UTF-8.
- **Gate pin:** The fixture baseline and the real manifest bind the whole file under `pinned_sources`. `O1F-N10` mutates that file without rebinding.
