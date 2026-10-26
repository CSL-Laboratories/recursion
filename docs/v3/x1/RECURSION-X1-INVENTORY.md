# Recursion X1 inventory: pre-x1 tag and test disposition table

Prepared by Grok Bot Liaison for Lucas. WP14, Recursion half. Related to CSL-495. Proposed
for Dan's review. Nothing in this change deletes, moves or edits legacy code or tests.

This is gate data for the X1 deletion, not research prose. It lives here because
`scripts/tests/v3/x1/inventory.test.py` reads the two tables below on every `linux` run. No write
route to the private research vault is granted to this task, so no vault record was written.

## 1. Decision and sources

Lucas decided (his words, Mon 2026-10-26 11:59 AM PT): "Decision 3: approve keep-paths and
boundary, push pre-x1 tags, delete after the Nov 10 rehearsal".

- Keep-paths: `scripts/v3/route_adapter/**` and the receipt tests.
- Recursion deletion boundary: `scripts/v3/harness/**` and `planning/**`.
- Deletion happens only after the Nov 10 rehearsal. Nothing is deleted now.
- Source plan: the attached step 4 synthesis, section 6 (WP14), section 7 (decision 3), and
  checks V-06 and V-07. That file is not in the repository. These are secondary citations
  and are not re-verified here.
- Base: `master` at `2f1fc17ec2ab6271f06f4d99a6d55f97cbf2efb0` (fetched 2026-10-26).

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

- Boundary code: `scripts/v3/harness/**` (20 files) and `scripts/v3/planning/**` (3 files).
  On `master` at the base SHA, `scripts/v3/` holds only `harness/` and `planning/`, and no
  directory named `planning/` exists at the repository root. So `planning/**` is read as
  `scripts/v3/planning/**`. This reading was an inference. Dan confirmed it in his tip review
  of PR #303 (review `PRR_kwDOTuH3ps8AAAABUTlc8w`, 2026-10-26) and Samir agreed in his review
  (`PRR_kwDOTuH3ps8AAAABUTzE6w`). Lucas's own words do not spell it out; his Decision 3 text
  says only `planning/**`.
- Legacy test files "under the boundary" are the ones whose subject is boundary code. By
  imports that is everything in `scripts/tests/v3/harness/` and `scripts/tests/v3/planning/`.
  `scripts/tests/v3/execution/` and `scripts/tests/v3/integrity/` do not import boundary code,
  so they are outside the boundary and are not rows below.
- Keep-paths: `scripts/v3/route_adapter/**`. It does not exist on `master` yet. R#301
  (CSL-537, open) adds it, with its test at `scripts/tests/v3/route_adapter/at1.test.py`. The
  boundary does not overlap it.
- "The receipt tests" means `tools/test_claude_fixer_*`. Lucas decided that on CSL-495
  (comment `611d6aa4`, Mon 2026-10-26 1:47 PM PT: "I agree with your recommendations").
  Those files sit outside the deletion boundary and are kept. The receipt checks inside
  P1 and P5 stay `port`.

## 4. Disposition table

Dispositions below are the ones Lucas accepted on CSL-495 (comment `611d6aa4`,
Mon 2026-10-26 1:47 PM PT). `port` means the contract survives and a
successor carrier must run it green before the legacy file is deleted. `drop` means the file
tests only legacy implementation detail. `keep` means the file stays as is. The test requires
each file under the two legacy test directories to match exactly one row.

<!-- x1-disposition-table -->
| Row | Path or glob | Files | Disposition | Oracle | Reason |
| --- | --- | --- | --- | --- | --- |
| H1 | `scripts/tests/v3/harness/harness.test.py` | 1 | port | O-H1, O-H2, O-H3, O-H5 | 38 tests of the legacy CLI harness. Stream parsing, secret scrubbing, live-provider gating and route fallback are contracts a replacement route layer still owes. The other tests cover the CLI shell and drop with it (section 6). |
| H2 | `scripts/tests/v3/harness/ops.test.py` | 1 | port | O-H2, O-H3, O-H4 | 17 tests. The land refusal rules (never master or main, never an unreviewed tip) and the allow-live adapter gate are safety rules to carry. Clean, logs and adapter-name tests drop with the CLI (section 6). |
| H3 | `scripts/tests/v3/harness/fixtures/claude-stream-json.txt` `scripts/tests/v3/harness/fixtures/codex-cumulative.txt` `scripts/tests/v3/harness/fixtures/codex-exec-jsonl.txt` `scripts/tests/v3/harness/fixtures/grok-review.txt` `scripts/tests/v3/harness/fixtures/grok-streaming-json.txt` | 5 | port | O-H1 | Recorded provider stream samples. They are oracle data for the parser contract and move with O-H1. |
| H4 | `scripts/tests/v3/harness/fixtures/claude-auth-status*.txt` | 3 | drop | none | Sign-in probe output used only by the doctor command. No successor oracle. |
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
| O-H1 | Provider stream and usage parsers against recorded samples | `scripts/tests/v3/harness/harness.test.py` | `scripts/tests/v3/route_adapter/` | none |
| O-H2 | Secret custody: credential fields refused, secrets scrubbed from saved state and logs | `scripts/tests/v3/harness/harness.test.py` | `scripts/tests/v3/route_adapter/at1.test.py` | none |
| O-H3 | Live-provider gate: no live start without explicit allow | `scripts/tests/v3/harness/ops.test.py` | `scripts/tests/v3/route_adapter/at1.test.py` | none |
| O-H4 | Land refusal rules: never master or main, never an unreviewed tip, never a dirty tree | `scripts/tests/v3/harness/ops.test.py` | `scripts/tests/v3/route_adapter/` | none |
| O-H5 | Route selection: weights, cooldown fallback, disabled-provider skip, pre-call budget refusal | `scripts/tests/v3/harness/harness.test.py` | `scripts/tests/v3/route_adapter/` | none |

State on the base SHA: O-P6 and O-P7 are satisfied because they stay in place and never touch
the boundary. The other ten oracles are carried only by legacy files, so they are `pending`.
O-H1, O-H4 and O-H5 name the directory `scripts/tests/v3/route_adapter/` as their successor
home. O-H2 and O-H3 name R#301's test. `Successor tests` is `none` on all five, so none of
them can become `ported` until the file names are entered after R#301 merges. The provider
stream fixtures (row H3) move with O-H1.

**What `ported` does and does not mean.** It means the successor file exists, is wired into the
gate, is free of boundary references, and contains the named tests. It does not mean anyone
verified that those tests cover the contract. Several names are generic (`main`), and O-H2 and
O-H3 share one file. The deletion PR's reviewer must confirm that each successor actually covers
its oracle's contract, most of all O-H2 and O-H3 against R#301's `at1.test.py`, which no one has
verified yet.

## 6. Mixed files: which tests carry which oracle

Informational. The test only checks that each `test_` name below exists in its file.

`harness.test.py`:

- O-H1: `test_codex_jsonl_agent_message_is_the_assistant_text`,
  `test_codex_cumulative_usage_is_not_summed_and_clears_the_default_ceiling`,
  `test_checked_in_stream_fixtures_match_the_parsers`,
  `test_stubbed_rate_limit_is_classified_without_a_live_process`
- O-H2: `test_config_rejects_credential_fields_without_echoing_them`,
  `test_saved_run_scrubs_a_secret_from_the_environment`,
  `test_assistant_text_and_provider_errors_are_both_scrubbed`
- O-H3: `test_pinned_live_provider_does_not_start_without_allow_live`,
  `test_offline_failure_with_allow_live_does_not_pick_a_live_provider`,
  `test_quota_and_auth_fall_back_without_a_live_provider`
- O-H5: `test_capability_route_skips_disabled_providers`,
  `test_weights_prefer_the_higher_weight_and_reject_a_bad_weight`,
  `test_rate_limit_cooldown_persists_and_falls_back`,
  `test_pinned_fixture_rate_limit_falls_back_to_fixture_b`,
  `test_live_failure_falls_back_to_another_live_provider`,
  `test_large_diff_is_clipped_before_the_adapter`,
  `test_pre_call_budget_refuses_before_the_adapter`
- Drop with the CLI shell: every other test in the file (config shipping, doctor, windows
  shims, state folder, run ids, review verdicts, fixture review loop, workspace handling).

`ops.test.py`:

- O-H2: `test_logs_prints_events_and_redacts_token_shaped_text`
- O-H3: `test_new_adapters_start_only_with_allow_live`,
  `test_a_fixture_run_writes_events_without_a_provider_process`
- O-H4: `test_land_refuses_master_and_main`, `test_land_refuses_a_tip_the_reviewer_did_not_approve`,
  `test_land_refuses_when_acceptance_did_not_record_a_commit`, `test_land_refuses_a_dirty_tree`,
  `test_land_refuses_a_run_that_is_not_accepted`, `test_land_refuses_a_conflict_and_aborts`,
  `test_land_refuses_to_overwrite_an_ignored_file`
- Drop with the CLI shell: the remaining land, clean and adapter-name tests.

## 7. Floors the test pins

`scripts/tests/v3/x1/inventory.test.py` runs inside the existing `linux` job (and the Windows
and macOS jobs) through `tools/ci_gate.py`. It needs no workflow, job-name or gate edit,
because `ci_gate.py` discovers `scripts/tests/**/*.test.py`. It fails the job when:

1. A file under the two legacy test directories matches no row or more than one row, or a row
   matches a different number of files than its `Files` column.
2. A row has an unknown disposition, an empty reason, or an unknown oracle ID. A `port` row has
   no oracle.
3. A floor drops: 16 rows, 10 legacy `*.test.py` files, 2 support modules, 23 planning
   fixtures, 5 harness stream fixtures, 12 oracles, 10 port oracles, 2 satisfied oracles.
4. Before `X1_DELETION_LANDED` is set, the boundary code differs from the pinned list
   `PINNED_BOUNDARY_FILES` (the 23 `.py` files at the pre-x1 tag), in either direction, so
   removing one module and adding another is also red. Legacy test files that disappear are
   red through item 1. This is the guard for "delete only after the Nov 10 rehearsal".
   Adding a boundary file before deletion requires editing the pin on purpose.
5. Any legacy test file under the boundary is not run by `ci_gate.py`.
6. Any keep-path overlaps the boundary.
7. The `X1_DELETION_LANDED` flag is set while any port oracle is still `pending`, while boundary
   code remains, or while a keep-path is missing. This is the red-to-green gate for the
   deletion PR. It stays red until every successor carrier is green in the same job. The check
   that section 6 names real legacy tests runs only before deletion; afterwards the legacy files
   are expected to be gone and the register's successor tests carry the check.

`tools/test_ci_gate.py` gains one case that fails if the floor test stops being discovered and
run, so deleting the floor itself turns `linux` red.

## 8. Deletion PR preconditions and inbound dependencies

Checked by search on the base SHA. The deletion PR must handle each, after the Nov 10 rehearsal.

| Dependent | What breaks if the boundary is deleted | Evidence |
| --- | --- | --- |
| `tools/ci_gate.py` (protected path) | `REQUIRED_SUITE_TESTS` pins `harness.test.py`. `M1_REQUIRED_TESTS` pins 7 planning tests (17 total). `M1_MIN_PROGRAMS = 23` against 24 programs today (6 materialize checks plus 18 tests). Under the proposed table, dropping P6 and P7 leaves 22 programs and 16 pinned tests, so the floor must be re-pinned to 22 and `static` removed from the pins. | `tools/ci_gate.py` lines 81 to 113 and 363 to 368 |
| `tools/test_ci_gate.py` | Asserts `len(M1_REQUIRED_TESTS) == 17`. | line 341 |
| `tools/perf_bench.py`, `tools/perf_workload.json` | `perf_bench.py` imports the planning kernel and validators. The workload reads `F-M1A-002.json`. The perf step in `ci.yml` is `continue-on-error`, so it would only warn. | `tools/perf_bench.py` lines 99 to 103; `.github/workflows/ci.yml` lines 195 to 204 |
| `experiments/v3/effect-evidence/` (`ee_common.py`) | Imports the planning kernel and validators. Not part of general CI (packet-scoped). | `ee_common.py` lines 5 to 7 |
| `experiments/v3/swarm-research-20260911-155142/research_entry.py` | References `scripts/v3/planning/replay/kernel.py` by path. | line 23 |
| `README.md`, `docs/v3/harness/CSL-27.md`, `docs/v3/M1A-*.md` | Document `python -m scripts.v3.harness` and the planning kernel. | grep at base SHA |

If a thin direct-CLI fallback is kept (the plan says "labeled as not satisfying X2"), the
boundary shrinks and rows H1, H2 and H4 must be re-reviewed. No such keep-path is approved.

Workflow wiring for Lucas: none is needed for this change. The `linux`, `windows` and `macos`
job names are unchanged and this test runs in each. Optional, in a founder authoring session
for a protected path: add `scripts/tests/v3/x1/inventory.test.py` to `REQUIRED_SUITE_TESTS` in
`tools/ci_gate.py`, so the discovery pin lives in the gate itself as well.

## 9. Decisions recorded 2026-10-26

Lucas decided these on CSL-495, comment `611d6aa4-100f-4dc5-b8eb-88a710049287`
(Mon 2026-10-26 1:47 PM PT). His words: "I agree with your recommendations."
The same comment says none of these block this PR. They bind the post-Nov 10 deletion PR.
Dan was asked there to confirm the port and drop calls.

1. Decided: `planning/**` means `scripts/v3/planning/**` only (section 3, Dan and Samir).
2. Decided: "the receipt tests" means `tools/test_claude_fixer_*`. Those files are outside
   the deletion boundary and are kept. The receipt checks in P1 and P5 are ported anyway.
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

Execution record: no Linear claim was posted, and none will be added retroactively.
Lucas's comment `611d6aa4` says the PR and its review request are the record. Commits
carry no `Agent-Claim` trailer for that reason.

## 10. Verification and unverified items

- Verified locally on the base SHA plus this change: `python -X utf8 -B tools/ci_gate.py` and
  the new test, with mutation checks listed in the PR body.
- Unverified: the synthesis file contents beyond the sections cited; whether R#301 covers
  O-H2 and O-H3 (test names are not entered until that PR merges); GitHub CI on this PR head.
