---
title: Recursion v3 spec amendment, v2 (drafted 2026-10-26)
status: amendment proposed (v2); acceptance is an exact-digest human receipt by Lucas
last-updated: 2026-10-26
authority: proposed; does not admit work, clear a hold, select a language/store/framework, authorize spend, or decide product goals
---

# Recursion v3 spec amendment, v2 (drafted 2026-10-26)

**Status: amendment proposed (v2); acceptance is an exact-digest human receipt by Lucas.**

This file is a **proposed amendment**. It supersedes
[AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md) (the v1 proposal) only where this
file says so. It does not rewrite Owner-approved wording. The OR-038 paragraph in
[PRODUCT.md](PRODUCT.md) is preserved verbatim. `state/DECISIONS.md`, `ANCHOR.md`,
`AGENTS.md` and `CONTROL_PLANE.md` are not edited. Publishing this proposal does not mark
any issue decided, does not clear a hold, and does not authorize implementation.

Owner issue: [CSL-542](https://linear.app/csl-laboratories/issue/CSL-542) (RP0-v2),
child of umbrella [CSL-493](https://linear.app/csl-laboratories/issue/CSL-493).
[CSL-494](https://linear.app/csl-laboratories/issue/CSL-494) (RP0) is Done and is not
reopened. This amendment is Lucas-authored, so Lucas's exact-digest receipt accepts it. A
GitHub approval, a merge, green CI, a Linear status or this file's own text is not a
receipt. Until that receipt exists, the 2026-10-24 amendment and the accepted v3 text
remain the governing proposals and decisions.

## RP0-v2 section 1. What this file changes

1. Keeps, unchanged, section 1 (north star), section 2 (seven-point bar), section 3
   pillars P1 to P17 and the F-1 to F-15 research questions of the 2026-10-24 amendment.
   That file stays in place as the v1 proposal, with one supersession pointer line.
2. Adds the four decided directions X1 to X4 (section 2), the Nov 11 framing and slip
   order (section 3), an **Execution substrate** section (section 4) and the Admission
   retargets for the Recursion parents (section 5).
3. Does not touch protected paths. `CONTROL_PLANE.md`, `ANCHOR.md` and
   `state/DECISIONS.md` need a founder authoring session
   (`CONTROL_PLANE.md` lines 126 to 150). Items that need that session are listed in
   section 8, not edited here.

## RP0-v2 section 2. Decided directions encoded

**X1 (Lucas, 2026-10-25 1:59 PM PT): rebuild means delete and recreate the old engine
code.** Existing documentation, and what the engine was supposed to accomplish, remain
the basis for research and development.

Path sets used by this amendment (the labels are names in this text, not new roots):

| Set | Paths | Role |
|---|---|---|
| `RX-LEGACY` | `scripts/v3/harness/**`, `scripts/v3/planning/**`, and the legacy tests that import or open those directories: `scripts/tests/v3/harness/**` and, under `scripts/tests/v3/planning/`, `adversarial`, `conformance`, `incremental`, `property`, `red_fixture`, `static` and `validation` (each `*.test.py`) plus `reduce_reference.py` | Frozen, then deleted by one cutover PR. Each test file gets one disposition row first. |
| `RX-KEEP` | `scripts/v3/route_adapter/**`, `scripts/tests/v3/route_adapter/**`, `scripts/v3/route_contract/**`, `scripts/tests/v3/route_contract/**`, `schemas/control-plane/v3/route-contract/**` | Survives the cutover. Binds once the pull request that adds each path merges. |
| `RX-NEW` | `scripts/v3/**` outside `RX-LEGACY`, in directories each child issue names; tests under `scripts/tests/v3/**` | The rebuilt engine. Reuses the existing layout, so no new root and no CI glob change. Do not create `scripts/v3/execution/`: `scripts/tests/v3/execution/static.test.py` line 30 asserts it does not exist. |

- **Freeze.** `RX-LEGACY` is frozen at tag `pre-x1-recursion-20261026`
  (commit `2f1fc17ec2ab6271f06f4d99a6d55f97cbf2efb0`). No agent edits it. CI and test
  tooling work stays allowed.
- **Keep.** Decision 12 (Lucas, relayed on CSL-522 comment `56b36b45`) adds the route
  contract paths to the keep list. `RX-KEEP` names them by directory so later files in
  those directories need no further amendment.
- **Timing.** Deletion happens only after the Nov 10 rehearsal, and only once conformance
  has gone from red to green under unchanged job names (decision 3, CSL-522 comment
  `9f3f7fc1`). Early deletion is not carried.
- **Gate retarget.** `tools/ci_gate.py` floors are retargeted by the cutover PR:
  `REQUIRED_SUITE_TESTS` (lines 81 to 88), `M1_MIN_PROGRAMS = 23` (line 89) and
  `M1_REQUIRED_TESTS` (lines 103 to 114). `tools/ci_gate.py` is a protected path and
  changing an existing assertion needs the named human reviewer, who submits their own
  GitHub review from their own account (`CONTROL_PLANE.md` lines 126 to 150).
- **Direct-CLI fallback.** A thin direct-CLI fallback is kept as a fallback only. It is
  recreated in `RX-NEW` (the current provider adapters in
  `scripts/v3/harness/providers.py` belong to `RX-LEGACY`) and it must not carry the
  auto-approve flags found there at lines 181, 200 and 279. **It does not satisfy X2.** A
  run through it is not an X2 integration and may not be labelled as one.
- **Language.** X1 does not select an implementation language. Language selection stays
  with the F-3 research question and its decision records.

**X2 (Lucas, 2026-10-25 2:01 PM PT): the first consuming product is built on Recursion by
default, with a pluggable harness layer.**

- Recursion implements the default harness adapter. Claude Code, Codex and other harnesses
  can implement the same contract. Native strengths are expressed as capability
  descriptors, never as a bypass of Recursion's authority rules.
- Recursion stays domain-neutral. It carries no consumer-specific code, identifier or
  string. `tools/check_public_scope.py` (lines 22 to 37) rejects named foreign-project
  identifiers and is not narrowed by this amendment.
- **Decided (decision 2, CSL-522 comment `9f3f7fc1`):** on Nov 11 Recursion delivers the
  route contract, fixture conformance and one recorded consumer bakeoff through an
  in-process adapter against the fake driver, labelled **X2 live integration deferred**. A
  live route is added only if CSL-460 lands by Nov 5. The live-by-Nov-6 option is not
  carried.

**X3 (Lucas, 2026-10-25 1:59 PM PT): the claim lease is 2 h plus 30 min grace; `AGENTS.md`
wins.** `AGENTS.md` line 84 is the source: the lease runs 2 hours from the claim comment's
last Linear `updatedAt`, and a renewal up to 2 hours 30 minutes restores it. The grace
period is a renewal and recovery window, not extra write time, and the writer renews its
own heartbeat. Any text in this repository or in an Admission that says "4h lease" reads
"2 h lease (+30 min grace)". [CSL-521](https://linear.app/csl-laboratories/issue/CSL-521)
is the known case (section 5.4).

**X4 (Lucas, 2026-10-25 1:59 PM PT): everything is documented in csl-research.**

- Research, specs, decisions, evidence and runbooks go to `CSL-Laboratories/csl-research`
  as records. This repository keeps the public README, knowledge base and implementation
  instructions, agent instruction files and current-state operator documentation.
- This supersedes the sentence in `docs/v3/START-HERE.md` (lines 26 to 28) that says
  user-facing and revision-dependent "operational docs and contracts stay in this
  repository". That sentence stays as history with a dated pointer.
- **Decision 7 (CSL-522 comment `56b36b45`):** only files that checkers or agents read
  offline stay as thin digest-pinned projections (`program.json`, the pinned documents,
  `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`). csl-research is the store for
  non-secret material only. CSL-536 runs inventory and classification now and the mass
  move comes after Nov 11. The exact file list is that inventory's output, which Lucas
  approves before anything moves.
- csl-research `README.md` and `decisions/README.md` still say product contracts remain
  authoritative in their owning product repository. A vault amendment follows X4 and is
  outside this file.

## RP0-v2 section 3. Nov 11 framing and slip order

**Decision 1 (CSL-522 comment `9f3f7fc1`).** Nov 11 is a recorded demo of a named spine.
Everything outside the spine is a labelled stretch or a named deferral. The ambition in D6
is unchanged. The spine, the stretch list and the slip order are recorded in the Nov 11
spine memo, a separate csl-research record, which becomes a pointer only after Lucas's
receipt. The slip order, invoked by Lucas on Nov 6, is:

1. the legacy-deletion PR moves behind the Nov 10 rehearsal;
2. operating-system client targets narrow to Linux-first;
3. venue and market scope on the consuming product narrows to its first demo venue plus one other;
4. benchmark-suite scope narrows;
5. pillars outside the spine become labelled stretch;
6. the category statement narrows to protocol only (nothing is called a verdict on Nov 11).

## RP0-v2 section 4. Execution substrate (Recursion side)

This section says who owns execution, so a consuming product does not grow a second
runtime beside Recursion's.

1. **Ownership.** Recursion owns session lifecycle, queue, lease and fencing, route
   selection, quota and capacity pools, sandbox and egress, health and cooldown, and the
   stop and kill path. Consumers own domain policy only.
2. **One contract, not three.** Three incompatible interface shapes exist today: the
   consumer's own CLI runner, Recursion's `scripts/v3/harness/providers.py`, and the
   route adapter of R#301 (methods `begin`, `send`, `observe`, `cancel`, `complete`;
   `F5_COUNSEL_GATE = "closed"`). The shared contract is a thin projection of the frozen
   M1B route shapes (`docs/v3/M1B-EXECUTION-CONTRACTS.md`, `docs/v3/ROUTE-CAPABILITIES.md`)
   plus execution and output fields. It is proved by fixture conformance against the fake
   driver (`scripts/v3/route_adapter/fake.py`), not by live adapters. The Route Contract
   v0 draft is R#302, in `scripts/v3/route_contract/**`.
3. **Conformance oracles.** The oracle is the consumer's current runner argv for: a
   zero-tool decision stage, secret refusal, health injection, a schema-repair loop and
   tree-kill. Recursion already has cooldown and availability code
   (`scripts/v3/harness/cooldowns.py`, `availability.py`); the list above is what it lacks.
4. **Live routes.** Every live adapter reports "unsupported" until the R2a permission
   profile (CSL-299) and CSL-460 land and a developer approval is recorded
   (`CONTROL_PLANE.md` lines 54 to 60 and 117 to 119). No live route may use auto-approve
   flags. CSL-460 is funded only if a live route is in scope (decision 12).
5. **Neutral identifiers.** Workload and route identifiers in Recursion are
   domain-neutral, and `tools/check_public_scope.py` is not narrowed this week.
6. **Out of this section.** The shared sandbox-profile spec (RP13) comes after Nov 11
   (decision 12).
7. **Where the work sits.** RP2 ([CSL-496](https://linear.app/csl-laboratories/issue/CSL-496))
   owns the execution contract, with CSL-207 as lineage (decision 12). Dan keeps RP2 and
   takes the Recursion spine (decision 11).

## RP0-v2 section 5. Admission retargets

Linear edits are made by Lucas; this file only states the text.

### 5.1 Rule RA (Recursion parents CSL-495 to CSL-510)

Replace the first part of "Allowed effects/files" with:

> recursion `scripts/v3/**` except `RX-LEGACY` (`scripts/v3/harness/**`,
> `scripts/v3/planning/**`), which is frozen at tag `pre-x1-recursion-20261026` and may be
> deleted only by the X1 cutover PR; new modules go in directories named by the child
> issue, never `scripts/v3/execution/`; `RX-KEEP` paths are writable only when the child
> issue names them; `scripts/tests/v3/**`, `schemas/**` and the named `docs/v3/*.md`
> sections as before.

Replace the verifier tail with:

> collected by `tools/ci_gate.py` (`TEST_GLOBS`, lines 53 to 64: `tools/test_*.py`,
> `scripts/tests/**/*.test.py`, `scripts/tests/**/test_*.py`, `tests/**` and
> `experiments/**` tests); the cutover PR retargets `REQUIRED_SUITE_TESTS`,
> `M1_REQUIRED_TESTS` and `M1_MIN_PROGRAMS`; numbers from `tools/perf_bench.py` or
> `tools/perf_compare.py` are recorded evidence, not a gate, unless a collected test fails
> on regression. `tools/ci_gate.py` does not reference `perf_bench.py`, `perf_compare.py`,
> `check_rescope.py` or `check_program_control.py`.

### 5.2 Per-parent retargets

"v1 verifier" states what the Linear text names. "Collected" means collected by `tools/ci_gate.py`.

| Parent | Owner, status | v1 Allowed (legacy token) | v1 verifier, collected? | v2 |
|---|---|---|---|---|
| CSL-494 RP0 | Lucas, Done | `docs/v3/PRODUCT.md`, `INTERFACE-STRATEGY.md`, `ROUTE-CAPABILITIES.md`, `CLAIM-REGISTRY.json` | orientation and public-scope checks on R#295 | No change. [CSL-542](https://linear.app/csl-laboratories/issue/CSL-542) is the home of this amendment. |
| CSL-495 RP1 | none, Backlog | `scripts/v3/**`, `scripts/tests/v3/**`, `schemas/**` | `ci_gate.py` "green on all three OSes" plus `perf_bench.py` numbers: `ci_gate.py` collected, the three-OS claim and perf not | RA. Linux is the gated OS; Windows and macOS cells are recorded sessions until a collected cell exists (slip step 2). |
| CSL-496 RP2 | Chaandan, In Progress | same, plus D-021, D-023, D-029, D-030 | route qualification matrix and credential-custody negative suite: collected | RA; owns `RX-KEEP`. Adds Route Contract v0 conformance against the fake driver in `scripts/tests/v3/route_adapter/` and `scripts/tests/v3/route_contract/`. |
| CSL-497 RP3 | none | same | paired A/B fixture: collected | RA |
| CSL-498 RP4 | none | same | Control API auth negative tests collected; `perf_bench.py` latency not | RA |
| CSL-499 RP5 | none | same, enterprise code only in the edition plugin directory | RBAC negative suite: collected | RA |
| CSL-500 RP6 | none | same, node config in `config/**` | journey 5 node-loss test: collected | RA. The "node agent on at least 3 Linux nodes by Nov 8" bullet is a slip-order item (decision 1). |
| CSL-501 RP7 | none | same | secret scan plus bring-up test: collected | RA |
| CSL-502 RP8 | none | same | ledger tests collected; `perf_compare.py` overhead not | RA |
| CSL-503 RP9 | none | same, plus root `AGENTS.md` install section and `llms.txt` | zero-keystroke install runs plus authority-receipt test: collected | RA. The `AGENTS.md` edit stays a protected-path authoring-session item and is outside this table. |
| CSL-504 RP10 | none | same | seeded-defect suite: collected | RA |
| CSL-505 RP11 | none | same | importer lossiness report plus benchmark: collected | RA |
| CSL-506 RP12 | none | same | reboot-survival test: collected | RA |
| CSL-507 RP13 | none | same | sandbox-escape and egress suites: collected | RA. The shared sandbox-profile spec is post-Nov 11 unless a live route is added (decision 12). |
| CSL-508 RP14 | none | same | replay determinism collected; `perf_bench.py` replay cost not | RA |
| CSL-509 RP15 | none | same | false-citation and contradiction suites: collected | RA |
| CSL-510 RP16 | none | same, `experiments/**` | independent rerun of the frozen SHA (human-reviewed procedure, not collected) and `check_public_scope.py` on published material | RA. `check_public_scope.py` rejects named foreign-project identifiers, so any published comparison that names the consuming product fails it; publish with neutral workload identifiers. |

### 5.3 Shared parents (Recursion side)

| Parent | v2 |
|---|---|
| CSL-520 SP1 | "Removals" from this repository become moves governed by X4 and the CSL-536 inventory, which Lucas approves first. `check_public_scope.py` is a Recursion tool and applies here. |
| CSL-521 SP2 | Both lease sites read "2 h" (section 5.4). Workflow edits need Lucas's own-words OK. `tools/test_program_control.py` is collected by `tools/test_*.py`. |
| CSL-522 SP3 | Walkthrough scripts are operator documentation under X4. `check_public_scope.py` applies to published material. The stop condition keeps "drop any demo segment that has no passing verifier". |
| CSL-536 | No Admission is drafted. It is unparented and unowned. Decision 7: inventory and classification now, mass move after Nov 11. |

### 5.4 CSL-521: "2 h" in both places

| Where | Current text | Replace with |
|---|---|---|
| `## Goal` | "Linear **4h leases**, merge queue, daily map." | "Linear **2 h leases (+30 min grace)**, merge queue, daily map." |
| `## Legacy issues/PRs mapped here as KEEP`, CSL-357 bullet | "Linear-only 4h lease (10:25 #7)." | "Linear-only 2 h lease (+30 min grace) (X3, Lucas 2026-10-25 1:59 PM PT)." |

Other "4h" sites outside this repository need the same correction from whoever owns them.

## RP0-v2 section 6. Receipt route

This amendment is Lucas-authored. It is accepted only by Lucas's exact-digest human
receipt over its bytes, posted as his own unprefixed Linear comment citing the digest
(decision 5, CSL-522 comment `56b36b45`). A GitHub approval, a merge, green CI, a Linear
status or this file's text is not a receipt. The founder-approval route proposed in
recursion R#296 (CSL-524) takes effect only once its pull request is merged and wired into
`CONTROL_PLANE.md` through an authoring session; until then it neither accepts nor rejects
this file. No agent lane holds an admin or bypass-capable token, and Lucas logs each
break-glass merge with a reason.

## RP0-v2 section 7. What this does not do

Select a product language, store, framework, kernel or default relay. Authorize live
spend, credentials or quota resets. Rewrite OR-038, OR-039 or any Owner-approved paragraph.
Admit RP1 to RP16 implementation work (each stays a separate admission). Touch workflow
files or CI. Keep a research register in this repository. Edit R#295, R#296, R#301 or R#302.

## RP0-v2 section 8. Needs a founder authoring session (not edited here)

`ANCHOR.md` lines 44 and 46, `AGENTS.md` line 62 and `CONTROL_PLANE.md` lines 12 to 13
bar cross-repository supervision and choosing a language or sandbox from research alone.
`tools/ci_gate.py` floors need the named human reviewer. Both are decision 10's session
(Thu Oct 29) and are not edited by this amendment.

## Research basis (candidate-only)

Vault draft record (candidate evidence; not human product acceptance):

- repository: `CSL-Laboratories/csl-research`
- commit: `541ee845cdcc0a479190ddd6f0736e4dc854dd1d`
- path: `submissions/20261026-csl493-wp7-rp0v2-cp0v2-amendment-drafts-01/`
- claim: the draft text, tree map, Admission retargets and evidence behind this file.
- limitation: candidate evidence on an unmerged pull request (csl-research#50); decisions
  1 to 12 are an agent's relay of Lucas's words, not his receipt.

The exact vault commit of the record that carries this file's text is stated in the
pull request that adds it. Lucas's decisions are relayed from CSL-522 comments
`9f3f7fc1-15ae-4621-83ff-d98192e902b1` and `56b36b45-0faf-493e-933e-82a2253862b1`.
