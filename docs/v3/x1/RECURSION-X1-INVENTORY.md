# Recursion X1 inventory: pre-x1 tag and test disposition table

Prepared by Grok Bot Liaison for Lucas. WP14, Recursion half. Related to CSL-495. Proposed
for Dan's review. Nothing in this change deletes, moves or edits legacy code or tests.

This is gate data for the X1 deletion, not research prose. It lives here because
`scripts/tests/v3/x1/inventory.test.py` reads the two tables below on every `linux` run. No write
route to the private research vault is granted to this task, so no vault record was written.

## 1. Decision and sources

Lucas decided (his words, Mon 2026-10-26 11:59 AM PT): "Decision 3: approve keep-paths and
boundary, push pre-x1 tags, delete after the Nov 10 rehearsal".

- Keep-paths: `scripts/v3/route_adapter/**` and the receipt tests
  (`tools/test_claude_fixer_*`).
- Recursion deletion boundary: `scripts/v3/harness/**` and `planning/**`.
- Deletion happens only after the Nov 10 rehearsal. Nothing is deleted now.
- Source plan: the attached step 4 synthesis, section 6 (WP14), section 7 (decision 3), and
  checks V-06 and V-07. That file is not in the repository. These are secondary citations
  and are not re-verified here.
- Base: `master` at `2f1fc17ec2ab6271f06f4d99a6d55f97cbf2efb0` (fetched 2026-10-26).

Changed 2026-10-29 (CSL-578), for the harness half only. Asked whether to keep the harness until
CSL-460 lands or delete it, Lucas said: "I think we should just delete it. It served no
functionality when we are literally redesigning the entire thing". Told that this overrides
Decision 3's timing and the port calls in section 9, he answered: "Actually cancel that. Delete
and drop tests too, they aren't relevant". So `scripts/v3/harness/**`, its tests and fixtures,
and `config/harness*.json` are deleted, and oracles O-H1 to O-H5 are dropped with no successor.
`scripts/v3/planning/**` keeps the 2026-11-10 plan and everything below that concerns it.

## 2. Tag

| Item | Value |
| --- | --- |
| Name | `pre-x1-recursion-20261026` (annotated) |
| Target | `2f1fc17ec2ab6271f06f4d99a6d55f97cbf2efb0` (`master` when tagged) |
| Tag object | `53f221e1a2ed29289f2644eae936112740c9b9d9` |
| Tagged | 2026-10-26 12:05 PM PDT (19:05 UTC) |
| Precedent | `pre-v3-clean-cut-20260928` (annotated, tag object `407f4794`, target `acf0206e`) |
| Push | Pushed with the task token. `git ls-remote --tags origin 'pre-x1*'` returned the tag object above. |

Recreate command, if ever needed (do not move the tag once pushed):

```bash
git tag -a pre-x1-recursion-20261026 2f1fc17ec2ab6271f06f4d99a6d55f97cbf2efb0 -m "Archive: full pre-x1 state of recursion before X1 legacy deletion"
git push origin refs/tags/pre-x1-recursion-20261026
```

## 3. Boundary and keep-paths as the test reads them

- Boundary code: `scripts/v3/planning/**` (3 files). `scripts/v3/harness/**` (20 files) was
  part of the boundary until CSL-578 deleted it.
  On `master` at the base SHA, `scripts/v3/` holds only `harness/` and `planning/`, and no
  directory named `planning/` exists at the repository root. So `planning/**` is read as
  `scripts/v3/planning/**`. This reading was an inference. Dan confirmed it in his tip review
  of PR #303 (review `PRR_kwDOTuH3ps8AAAABUTlc8w`, 2026-10-26) and Samir agreed in his review
  (`PRR_kwDOTuH3ps8AAAABUTzE6w`). Lucas's own words do not spell it out; his Decision 3 text
  says only `planning/**`.
- Legacy test files "under the boundary" are the ones whose subject is boundary code. By
  imports that is everything in `scripts/tests/v3/planning/` (and, before CSL-578,
  `scripts/tests/v3/harness/`).
  `scripts/tests/v3/execution/` and `scripts/tests/v3/integrity/` do not import boundary code,
  so they are outside the boundary and are not rows below.
- Keep-paths: `scripts/v3/route_adapter/**`. It does not exist on `master` yet. R#301
  (CSL-537, open) adds it, with its test at `scripts/tests/v3/route_adapter/at1.test.py`. The
  boundary does not overlap it.
- "The receipt tests" are `tools/test_claude_fixer_guard.py`,
  `tools/test_claude_fixer_publish.py` and `tools/test_claude_fixer_receipts.py`
  (`tools/test_claude_fixer_*`). Lucas decided that on CSL-495 comment `611d6aa4`
  (his words, Mon 2026-10-26 1:47 PM PT: "I agree with your recommendations.").
  Those files sit outside the deletion boundary and are kept. The receipt checks inside
  P1 and P5 stay `port`. The floor pins the three files and fails if one is missing.

## 4. Disposition table

Dispositions below are the ones Lucas accepted on CSL-495 (comment `611d6aa4`,
Mon 2026-10-26 1:47 PM PT). `port` means the contract survives and a
successor carrier must run it green before the legacy file is deleted. `drop` means the file
tests only legacy implementation detail. `keep` means the file stays as is. The test requires
each file under the legacy test directory to match exactly one row. Rows H1 to H4 (the harness
tests and fixtures) were removed with the harness in CSL-578.

<!-- x1-disposition-table -->
| Row | Path or glob | Files | Disposition | Oracle | Reason |
| --- | --- | --- | --- | --- | --- |
| P1 | `scripts/tests/v3/planning/conformance.test.py` | 1 | port | O-P1 | Executes every frozen planning command and replays the oracle twice. The 23-case corpus is the contract. |
| P2 | `scripts/tests/v3/planning/adversarial.test.py` | 1 | port | O-P2 | Regression probes for the TERRA-M1A-02 dissent. Refusal behavior must hold in any successor. |
| P3 | `scripts/tests/v3/planning/property.test.py` | 1 | port | O-P3 | Attenuation, graph and purity properties. |
| P4 | `scripts/tests/v3/planning/validation.test.py` | 1 | port | O-P4 | Strict decoding, canonicalization and DAG validation. |
| P5 | `scripts/tests/v3/planning/red_fixture.test.py` | 1 | port | O-P5 | Validates the literal corpus, then executes it against the reducer (it names `scripts/v3/planning/replay/kernel.py` directly). |
| P6 | `scripts/tests/v3/planning/incremental.test.py` | 1 | drop | O-P1 | Pins digest parity between the naive and incremental reducers (CSL-336). It is an implementation detail of the legacy kernel. Byte-identical digests are still checked by O-P1. |
| P7 | `scripts/tests/v3/planning/static.test.py` | 1 | drop | O-P3 | AST isolation scan whose target is the deleted package (`PACKAGE = scripts/v3/planning`). The purity property is carried by O-P3. |
| P8 | `scripts/tests/v3/planning/schema.test.py` | 1 | keep | O-P6 | Tests the wire schema and its materializer under `schemas/control-plane/v3/planning/`. It does not import boundary code. |
| P9 | `scripts/tests/v3/planning/materialize_fixtures.py` | 1 | keep | O-P7 | Fixture generator. `ci_gate.py` runs it with `--check`. It does not import boundary code. |
| P10 | `scripts/tests/v3/planning/reduce_reference.py` | 1 | drop | O-P1 | Naive reference reducer. Its only user is `incremental.test.py` (P6). |
| P11 | `scripts/tests/v3/planning/fixtures/*` | 24 | keep | O-P7 | The 23 `F-M1A-*.json` cases plus `manifest.json`. Frozen oracle data for every planning port. `tools/perf_workload.json` also reads `F-M1A-002.json`. |
| P12 | `scripts/tests/v3/planning/.gitattributes` | 1 | keep | O-P7 | Line-ending rule that keeps fixture bytes stable. |

## 5. Oracle register

The successor column names the file that must carry the oracle after deletion. In-place ports
keep their path and switch their import to the successor. The test computes status. An oracle
is `ported` only when its successor file exists, is run by `tools/ci_gate.py`, no longer
references the boundary, and defines every function or class named in `Successor tests`.
`pending` means it is not yet. A `Successor tests` cell of `none` keeps the oracle `pending`.
`none` in the successor column means no carrier path is named yet. A directory path names
the home and stays `pending` until a file and its test names are entered.

<!-- x1-oracle-register -->
| Oracle | Contract | Legacy carrier | Successor | Successor tests |
| --- | --- | --- | --- | --- |
| O-P1 | Replay of all 23 frozen planning cases, twice, with byte-identical digests | `scripts/tests/v3/planning/conformance.test.py` | `scripts/tests/v3/planning/conformance.test.py` | `run_conformance` |
| O-P2 | Adversarial refusal probes | `scripts/tests/v3/planning/adversarial.test.py` | `scripts/tests/v3/planning/adversarial.test.py` | `run_adversarial` |
| O-P3 | Attenuation, graph and purity properties | `scripts/tests/v3/planning/property.test.py` | `scripts/tests/v3/planning/property.test.py` | `main` |
| O-P4 | Strict decode, canonical form and DAG validation | `scripts/tests/v3/planning/validation.test.py` | `scripts/tests/v3/planning/validation.test.py` | `main` |
| O-P5 | Literal corpus validity and reducer execution | `scripts/tests/v3/planning/red_fixture.test.py` | `scripts/tests/v3/planning/red_fixture.test.py` | `validate_corpus` |
| O-P6 | Planning wire schema and materializer byte identity | `scripts/tests/v3/planning/schema.test.py` | `scripts/tests/v3/planning/schema.test.py` | `test_materializer_is_byte_identical` |
| O-P7 | Fixture corpus, manifest and generator `--check` | `scripts/tests/v3/planning/materialize_fixtures.py` | `scripts/tests/v3/planning/materialize_fixtures.py` | `main` |

State: O-P6 and O-P7 are satisfied because they stay in place and never touch the boundary.
O-P1 to O-P5 are carried only by legacy files, so they are `pending`. The five harness oracles
(O-H1 to O-H5) were dropped with the harness in CSL-578 and have no successor.

**What `ported` does and does not mean.** It means the successor file exists, is wired into the
gate, is free of boundary references, and contains the named tests. It does not mean anyone
verified that those tests cover the contract. Several names are generic (`main`). The deletion
PR's reviewer must confirm that each successor actually covers its oracle's contract.

## 6. Mixed files: which tests carry which oracle

This section mapped the harness test names to O-H1 to O-H5. It was removed with the harness in
CSL-578; the last copy is at `7c6ffdf69ad8` and under the `pre-x1-recursion-20261026` tag.

## 7. Floors the test pins

`scripts/tests/v3/x1/inventory.test.py` runs inside the existing `linux` job (and the Windows
and macOS jobs) through `tools/ci_gate.py`. It needs no workflow, job-name or gate edit,
because `ci_gate.py` discovers `scripts/tests/**/*.test.py`. It fails the job when:

1. A file under the legacy test directory matches no row or more than one row, or a row
   matches a different number of files than its `Files` column.
2. A row has an unknown disposition, an empty reason, or an unknown oracle ID. A `port` row has
   no oracle.
3. A floor drops: 12 rows, 8 legacy `*.test.py` files, 2 support modules, 23 planning
   fixtures, 7 oracles, 5 port oracles, 2 satisfied oracles. CSL-578 lowered the floors that
   counted the harness (16 rows, 10 test files, 5 stream fixtures, 12 oracles, 10 port
   oracles) when it deleted the harness.
4. Before `X1_DELETION_LANDED` is set, the boundary code differs from the pinned list
   `PINNED_BOUNDARY_FILES` (the 3 planning `.py` files at the pre-x1 tag), in either direction, so
   removing one module and adding another is also red. Legacy test files that disappear are
   red through item 1. This is the guard for "delete only after the Nov 10 rehearsal".
   Adding a boundary file before deletion requires editing the pin on purpose.
5. Any legacy test file under the boundary is not run by `ci_gate.py`.
6. Any keep-path overlaps the boundary.
7. The `X1_DELETION_LANDED` flag is set while any port oracle is still `pending`, while boundary
   code remains, or while a keep-path is missing. This is the red-to-green gate for the
   deletion PR. It stays red until every successor carrier is green in the same job.

`tools/test_ci_gate.py` gains one case that fails if the floor test stops being discovered and
run, so deleting the floor itself turns `linux` red.

## 8. Deletion PR preconditions and inbound dependencies

Checked by search on the base SHA. The deletion PR must handle each, after the Nov 10 rehearsal.

| Dependent | What breaks if the boundary is deleted | Evidence |
| --- | --- | --- |
| `tools/ci_gate.py` (protected path) | `M1_REQUIRED_TESTS` pins 7 planning tests (17 total). `M1_MIN_PROGRAMS = 23` against 24 programs today (6 materialize checks plus 18 tests). Under the proposed table, dropping P6 and P7 leaves 22 programs and 16 pinned tests, so the floor must be re-pinned to 22 and `static` removed from the pins. | `tools/ci_gate.py` lines 81 to 113 and 363 to 368 |
| `tools/test_ci_gate.py` | Asserts `len(M1_REQUIRED_TESTS) == 17`. | line 341 |
| `tools/perf_bench.py`, `tools/perf_workload.json` | `perf_bench.py` imports the planning kernel and validators. The workload reads `F-M1A-002.json`. The perf step in `ci.yml` is `continue-on-error`, so it would only warn. | `tools/perf_bench.py` lines 99 to 103; `.github/workflows/ci.yml` lines 195 to 204 |
| `experiments/v3/effect-evidence/` (`ee_common.py`) | Imports the planning kernel and validators. Not part of general CI (packet-scoped). | `ee_common.py` lines 5 to 7 |
| `docs/v3/M1A-*.md` | Document the planning kernel. | grep at base SHA |

Lucas's comment `611d6aa4` says the thin direct-Claude fallback is new code under
`route_adapter/`, not a keep of `scripts/v3/harness/`. It is labeled as not satisfying X2.

CSL-578 handled the harness rows of this table: the `harness.test.py` pin in
`REQUIRED_SUITE_TESTS` became `scripts/tests/v3/x1/inventory.test.py`, which keeps a dotted
`*.test.py` file pinned so a discovery miss still fails the gate, and the README harness section
was removed. `docs/v3/harness/` stays as history under CSL-536.

Workflow wiring for Lucas: none is needed. The `linux`, `windows` and `macos` job names are
unchanged and this test runs in each.

## 9. Decisions recorded 2026-10-26

Lucas decided these on CSL-495, comment `611d6aa4-100f-4dc5-b8eb-88a710049287`
(Mon 2026-10-26 1:47 PM PT). His words: "I agree with your recommendations."
The same comment says none of these block this PR. They bind the post-Nov 10 deletion PR.
Dan was asked there to confirm the port and drop calls.

1. Decided: `planning/**` means `scripts/v3/planning/**` only (section 3, Dan and Samir).
2. Decided: "the receipt tests" means `tools/test_claude_fixer_*`, the three files named in
   section 3. They are outside the deletion boundary and are kept. The receipt checks in P1
   and P5 are ported anyway.
3. Decided: the disposition table stands. O-H4 (land refusal) is **port**. O-H5 (route
   selection: weights, cooldown fallback, skipping disabled providers, pre-call budget
   refusal) is **port** and becomes part of the X2 route-contract conformance suite.
   P6 `incremental.test.py` is **drop**. O-P1 still checks byte-identical digests. The
   deletion PR still has to fix or retire `tools/perf_bench.py`.
4. Decided: the successor home for O-H1, O-H4 and O-H5 is `scripts/tests/v3/route_adapter/`,
   the keep-path. The provider stream fixtures move with O-H1. Test names are entered after
   R#301 merges, along with O-H2 and O-H3. Until those names are in `Successor tests`, the
   cell stays `none` and the oracle stays `pending`. The approved thin direct-Claude
   fallback is built as new code under `route_adapter/`, not kept in `scripts/v3/harness/`.

Items 3 and 4 no longer apply to the harness oracles: on 2026-10-29 (CSL-578) Lucas had the
harness deleted and O-H1 to O-H5 dropped without a port (section 1). The P6 and `perf_bench.py`
parts of item 3 still stand.

Execution record: no Linear claim was posted, and none will be added retroactively.
Lucas's comment `611d6aa4` says the PR and its review request are the record. Commits
carry no `Agent-Claim` trailer for that reason.

## 10. Verification and unverified items

- Verified locally on the base SHA plus this change: `python -X utf8 -B tools/ci_gate.py` and
  the new test, with mutation checks listed in the PR body.
- Unverified: the synthesis file contents beyond the sections cited; GitHub CI on this PR head.
  The receipt-test names, the port and drop calls, the successor home, and the claim record
  are settled by CSL-495 comment `611d6aa4`.
