# CI event-to-check contract

- **Contract version:** v2.1 (2026-10-21), a correction to v2. v2 records CSL-408 (#263): every job now runs on a free GitHub-hosted runner. `linux-tests` moved to `ubuntu-24.04` and windows to `windows-2025`. Event rows, conditions and the required check are unchanged from v1. v2.1 cites the first post-merge run (section 1), widens the [OBS] legend to dated runs, and corrects what a `GITHUB_TOKEN` push starts (section 7, gap 16).
- **Base commit:** master `0e48d54e875b66f1a757022b905de3f233e265dc` (merge of #263); v2.1 changes no cited line. v2 was traced at master `fa96b565` (merge of #262) plus the CSL-408 runner change in #263. That change keeps every cited line number in `ci.yml`, `ci-full-matrix.yml`, `tools/ci_runner_cost.py`, `AGENTS.md` and `CLAUDE.md`. #262 removed one line from `AGENTS.md`/`CLAUDE.md` above the cited CI section, so those two citations moved up by one; every other v1 citation still holds. v1 base: `b78b27b045f827849b88f3edf806200a8c591ba4` (master, merge of #247). The CI files cited below are unchanged since `2e369a6c747a4cc10ff6fd4da98f1272697bdc2e` (merge of #253). Since then, #259, #261 and #247 changed `AGENTS.md` and `CLAUDE.md` (#247 also added a skill and a `.gitignore` entry), and #260 changed only `tools/agent_protocol_sim*.py`. The event rows were traced at `c4cf7d8d` (#252, CSL-376); `ci.yml`, `tools/ci_gate.py` and the CI tests are unchanged since. #255 (CSL-383) and #253 (CSL-365) changed `claude.yml`, `conflict-scan.yml`, `codex-gate.yml` and `scripts/ci/`, which sections 5 and 7 cite. All `file:line` citations below are for this base.
- **Which workflow version runs:** a pull-request run uses the workflow file from the PR merge ref. `workflow_dispatch` and `push` use the file at the event's commit. A row applies only where that file matches this base; otherwise the behaviour depends on that version.
- **Scope:** describes current behaviour of the GitHub Actions workflows and repository rules. It is not a policy change and grants no authority. Where behaviour looks wrong, it is listed under [Known gaps](#8-known-gaps-and-follow-ups) for a separately admitted issue.
- **Evidence classes** (kept distinct throughout):
  - **[SRC]** source-documented: read from a file at the base commit; cited as `path:line`.
  - **[OBS]** locally observed: read from the GitHub REST API or job logs in the window below, or in a run cited with its ID and date.
  - **[INF]** inferred: reasoning from source plus GitHub platform semantics, not observed or tested here.
  - **[DEC]** recorded decision: a Linear record; the source and its authentication are named.

**Versioning.** Any change to a workflow, `tools/ci_gate.py`, a required check or the rulesets that changes a row here updates this file in the same PR. Bump the version for a semantic change (v1 → v2) and the patch for a correction (v1.1). Record the new base commit. Tracked under CSL-255 / CSL-385.

Observation window for [OBS]: the 64 `ci.yml` runs created 2026-10-20T03:27Z to 21:04Z, after `ci.yml` last changed (4c4fb763, #244). Each run's event action was read from the `linux` job log env (`EVENT_ACTION`, `PR_DRAFT`). Pull-request runs use the workflow file from the PR merge ref. Runs before c4cf7d8d used the pre-CSL-376 `tools/ci_gate.py`, which could still emit `run_full=false`.

## 1. Runners

- **Since CSL-408 (v2) every job is free.** `linux-tests` runs on `ubuntu-24.04` (`ci.yml:166`). windows runs on `windows-2025` (`ci.yml:305`, `ci-full-matrix.yml:20`). The remaining jobs run on `ubuntu-latest` or `macos-latest`. All are standard GitHub-hosted runners, and `tools/ci_runner_cost.py:18` prices them at $0 for this public repository. [SRC] The repository is public (`visibility: public`). [OBS] The new labels were observed only on this change's own PR runs, which are outside the v1 observation window above: `Linux tests` on `ubuntu-24.04` (run 39529052570, head `b671b67a`, 2026-10-21) and `ci-full-extra-os / windows` on `windows-2025` (run 39531217097, head `a87bbe6a`, 2026-10-21). [OBS, PR runs] The first post-merge run on master `0e48d54e` (push, 2026-10-21) ran `Linux tests` on `ubuntu-24.04` and windows on `windows-2025`, with `preflight` and `linux` on `ubuntu-latest`; `settle` and `macos` were skipped (run 39540049483). [OBS]
- **"Paid" in this contract** now means that the full-test runner (`linux-tests`) or windows was allocated. That is a cost in runner capacity and queue time, not money. The `paid` preflight output keeps its historical name (`ci.yml:2`). [SRC]
- **Before CSL-408 (v1):** those jobs ran on Blacksmith `blacksmith-2vcpu-ubuntu-2404` ($0.004/min) and `blacksmith-2vcpu-windows-2025` ($0.008/min). The rates stay in `tools/ci_runner_cost.py:13-17` for accounting of earlier runs, which are planning assumptions, "never billing proof" (line 1). Blacksmith billed each job that got a runner, per started minute (CSL-255 calibration). [SRC] [OBS]

## 2. Required checks

From `GET repos/CSL-Laboratories/recursion/rulesets`, `.../rulesets/25019234`, `.../rulesets/25015519` and `.../rules/branches/master` (2026-10-20) [OBS]:

| Ruleset | Source | Target | Rules |
|---|---|---|---|
| `required-ci` (25019234), active, updated 2026-10-20T06:04Z | repository | `~DEFAULT_BRANCH` (master) | `required_status_checks`: `linux` (integration 15368, GitHub Actions) and `Devin Review` (integration 811515); `strict_required_status_checks_policy: true`; `do_not_enforce_on_create: false`. `pull_request`: 1 approval, dismiss stale on push, `require_last_push_approval: true`, thread resolution required. Bypass: RepositoryRole 5, `pull_request` mode. |
| `protect-main` (25015519), active | organization (CSL-Laboratories) | `~DEFAULT_BRANCH` | `deletion`, `non_fast_forward`, `pull_request` (1 approval, dismiss stale, thread resolution, `require_last_push_approval: false`). Bypass: OrganizationAdmin, `pull_request` mode. |

- `gh api orgs/CSL-Laboratories/rulesets` is refused in this session ("sessions are bound to their configured repositories"). The org ruleset above was read through the repository-scoped endpoint. [OBS]
- Classic branch protection also exists on master (`enforce_admins`, `required_conversation_resolution`), with no status checks: `.../protection/required_status_checks` returns 404 "not enabled". Required checks come from `required-ci` only. [OBS]
- There is no merge-queue rule, and no workflow listens for `merge_group`. [OBS] [SRC]

**Who produces each required check**

- `linux`: job `linux` (`name: linux`) in `.github/workflows/ci.yml:207-289`, on `ubuntu-latest` (`ci.yml:211`). It is an aggregate with no checkout. The full suite is job `linux-tests` (`name: Linux tests`, `ci.yml:155-204`, `ubuntu-24.04` at `ci.yml:166`). No other workflow defines a job named `linux` (`ci-full-label.yml:2-4`; asserted by `tools/test_perf_bench.py:217`). [SRC]
- `Devin Review`: a **commit status**, not a check run, posted by `devin-ai-integration[bot]` as `pending` then `success`. On `11d2f700` it was posted at 20:44:20Z and 20:45:03Z; on `39c1e70e` at 21:04:14Z and 21:04:41Z. It is produced outside this repository's workflows, so this contract does not cover it further. [OBS]

## 3. Event matrix

Columns:

- **Jobs run:** jobs that start, other than skipped ones.
- **Paid:** the full-test runner (`linux-tests`, `ubuntu-24.04`) or windows (`windows-2025`) started. These runners are free since CSL-408, so the column tracks runner allocation, not spend.
- **Required `linux`:** what the run posts for that check on its head SHA.

`ci.yml` triggers: `pull_request` types `[opened, synchronize, reopened, ready_for_review]`, `push` to `master` and `workflow_dispatch` (`ci.yml:60-65`). The concurrency group is `ci-${{ github.workflow }}-${{ PR number || github.ref }}` with `cancel-in-progress: true` (`ci.yml:67-69`).

### 3.1 `ci.yml` (primary)

| # | Event | Jobs run | Paid | Required `linux` | Citation | Observed |
|---|---|---|---|---|---|---|
| E1 | `pull_request` opened, non-draft | preflight, Linux tests, linux; plus windows and macos if the PR already has `ci:full` | yes: ubuntu-24.04; windows-2025 only with `ci:full` | pass requires preflight success, `run_full=true`, `paid=true`, settle `skipped`, Linux tests `success` | `ci.yml:129-132`, `143-147`, `158-165`, `259-274`, `294-304` | 39399635090 (opened, settle skipped, tests success, linux success) |
| E2 | opened, draft | preflight, linux | no | **fails** ("draft pull request: linux qualification is deferred until ready_for_review") | `ci.yml:119-135`, `163`, `235-239` | 39376583206 |
| E3 | synchronize, non-draft | preflight, settle (`sleep 120`), Linux tests, linux; windows and macos with `ci:full` | yes, after settle | pass requires settle `success` and the rest as E1 | `ci.yml:140-152`, `164`, `259-260` | 39390206908; with `ci:full`: 39392986879 (windows on Blacksmith, macos on macos-latest) |
| E4 | synchronize, draft | preflight, linux | no; settle is skipped (`draft == false` clause) | **fails** (draft) | `ci.yml:147`, `163`, `235-239` | 39370999816 |
| E5 | burst of N non-draft synchronize pushes | each new run cancels the older in-progress or pending run in `ci-CI-<PR>`. A cancelled run that had started still runs `linux`. A run cancelled while still **pending** (replaced before it started) runs no job, so it posts no `linux` check; GitHub re-evaluates `always()` only for jobs that are already running (GitHub docs, workflow cancellation). [INF], not observed here. | runs cancelled during preflight or settle start no paid runner. **[INF]** A push that lands after an older run's Linux tests has started cancels it mid-flight, and the minutes used count. | each cancelled run that had started posts **failure** on its own (superseded) head; a pending run cancelled before starting posts nothing; the last run decides the live head | `ci.yml:67-69`, `140-152`, `207-210` | 39389995068: settle cancelled 2 min in, Linux tests `cancelled` with no runner, linux failure "settle result is 'cancelled'"; superseded by 39390206908 |
| E6 | reopened | as E1, or as E2 if draft | as E1, or none if draft (E2) | as E1/E2 | `ci.yml:62`, `232` | none in window |
| E7 | ready_for_review | as E1 (settle skipped; no 120 s wait) | yes | as E1 | `ci.yml:62`, `164-165`, `262` | 39495030669, 39391695628, 39374580228 |
| E8 | converted_to_draft | **no CI run** (not a trigger) | no | no new check; earlier `linux` results on the head stay. [INF] The PR cannot merge while a draft. | `ci.yml:62` | none |
| E9 | labeled / unlabeled (any label) | `ci.yml` does not run | no (see 3.2 for `ci:full`) | not produced | `ci.yml:53-57`, `62` | n/a |
| E10 | `edited` (title/body/base retarget) | `ci.yml` does not run; `pr-lint.yml` does (`pr-lint.yml:5`) | no | not re-produced. [INF] The `linux` already on the head stays and was computed against the old base (`CI_BASE_SHA`, `ci.yml:97`, `103`, `187`, `199`). The strict policy still requires the branch to be up to date, and updating it is a synchronize (E3). | `ci.yml:62`, `pr-lint.yml:5` | none |
| E11 | push to master | preflight, Linux tests, linux, windows (macos never runs on push) | yes: ubuntu-24.04 and windows-2025 | pass as E1 (settle `skipped`); `CI_BASE_SHA = github.event.before` | `ci.yml:63-64`, `97`, `302`, `328-329`, `341-342` | 39496738713 (c4cf7d8d), 39364410014 |
| E12 | merge_group | **not a trigger** | no | not produced. [INF] The shell would also reject the event (`ci.yml:246`), so enabling a merge queue without a workflow change leaves `linux` missing. | `ci.yml:60-65` | n/a |
| E13 | workflow_dispatch on a ref whose `ci.yml` matches this base (other refs: version-dependent) | preflight, Linux tests, linux, windows, macos | yes: ubuntu-24.04 and windows-2025 | a `linux` check run (pass as E1) on the dispatched ref's head SHA. Effect on the PR's required `linux`: not observed. GitHub's required-check troubleshooting doc says `workflow_dispatch` checks do not satisfy a pull request's required checks; only an eligible event such as `pull_request` does. `github.event.before` is empty, so the whitespace check is skipped (`tools/ci_gate.py:391-395`) and perf is skipped (`ci.yml:195`). The PR draft check (`ci.yml:235-241`) does not apply; `:242-245` only rejects a stray draft flag. A dispatch on a draft PR's branch can therefore create a passing `linux` check run on the draft head (gap 5). | `ci.yml:65`, `302-303`, `341` | no dispatch since 4c4fb763; the latest, 39073627392 (2026-10-17), ran a branch's different `ci.yml` |
| E14 | schedule | `ci.yml` has none (see 3.3) | n/a | n/a | `ci.yml:60-65` | n/a |
| E15 | rerun of a failed or cancelled run | [INF] reruns the selected jobs with the **original** event payload: a draft run reruns as a draft and fails again. A rerun joins the same concurrency group, so it can cancel the live run for that PR. Rerunning Linux tests starts its runner again. | yes, if Linux tests or windows reruns | [INF] a rerun of an older run on the current head posts a **newer** `linux` result on that SHA | `ci.yml:67-69`, `112`, `221` | none: all 300 recent runs are `run_attempt` 1 |
| E16 | run cancelled by concurrency | for a run that had started: `linux` still runs (`if: always()`), and unstarted jobs are marked cancelled without a runner. A pending run cancelled before starting runs no job (E5). | none, if cancelled before Linux tests started | **fails** on that run's head SHA if it had started; no check from a pending run cancelled before starting | `ci.yml:210`, `217`, `285-289` | 39495025672, 39391683862, 39383451090, 39389995068: all `linux` failure, paid jobs with no runner |
| E17 | `pull_request` from a fork (any of E1-E7) | as the matching same-repo row **once the run is allowed**. If the fork-approval setting applies (GitHub's default for public repos covers first-time contributors), the run waits for maintainer approval and no job starts until then. | once allowed, the same as the matching same-repo row: yes for E1, E3, E5 and E7 and for a non-draft E6; no for a draft fork (E2, E4 or a draft E6), because `linux-tests` also requires `draft == false` (`ci.yml:163`). [INF] `ci.yml` has no fork guard (no `head.repo.full_name` condition in `ci.yml:158-165` or `294-342`), so for non-draft forks, paid starts depend on the repository's fork-approval setting. Lucas reports setting it to require approval for all external contributors (2026-10-20); this session cannot read it. `codex-gate.yml:27` and `linear-pr-sync.yml:31` do guard. | as the matching same-repo row once allowed; pending (no `linux`) while awaiting approval | `ci.yml:158-165` | none |
| E18 | push or `pull_request` whose head commit carries a skip annotation (`[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]`, `[actions skip]`, or a `skip-checks: true` trailer) | none: GitHub creates no workflow run | no | not produced: the required check stays missing, which blocks merge (fail-closed) | GitHub docs, skipping workflow runs | none |
| E19 | `pull_request` while the PR has a merge conflict | none: GitHub creates no `pull_request` run until the conflict is resolved | no | not produced on that head; merge is blocked anyway | GitHub docs, events that trigger workflows (`pull_request`) | none |

Notes on E16 [OBS]: in all four cancelled runs, step-level `cancelled()` evaluated false inside `linux`. "Required status" ran and failed on the draft flag or a cancelled dependency, and "Reject workflow cancellation" was skipped (steps of run 39495025672). The `cancelled()` branch (`ci.yml:285-289`) was not exercised in the window. Both branches fail closed.

**Identical-head twins** [OBS]: three times in the window, a draft-time run (opened or synchronize) was followed within 5 to 9 s by a `ready_for_review` run on the same SHA. Native concurrency cancelled the older one:

- `3944c7d4`: 39383451090, then 39383463299
- `fba4e482`: 39391683862, then 39391695628
- `163d9db9`: 39495025672, then 39495046206

The SHA then carries a failed `linux` and a later result from the survivor. On `11d2f700` (#252) the check runs were failure 118876143589 (draft synchronize, 20:44Z) and success 118878785782 (ready_for_review, 20:49Z), and #252 merged at 21:00:00Z. [INF] That is consistent with GitHub using the latest `linux` check run on a SHA. Bypass merge is not excluded from REST data.

### 3.2 `ci-full-label.yml` + `ci-full-matrix.yml` (explicit full request)

| Event | Jobs run | Paid | Required `linux` | Citation | Observed |
|---|---|---|---|---|---|
| `labeled` `ci:full`, non-draft | `ci-full-extra-os / windows`, `ci-full-extra-os / macos`. No fork guard (`ci-full-label.yml:23-26`): on a fork PR whose author or event actor needs workflow approval, the run is held and starts nothing until a maintainer approves ([INF]; G14 setting, as E17). | yes: windows-2025 (macOS free), once the run is allowed | not produced: no job named `linux` | `ci-full-label.yml:9-11`, `23-26`; `ci-full-matrix.yml:6-7`, `18-20`, `43-45` | 39018368417 (2026-10-17) |
| `labeled` `ci:full`, draft | `extra-os` skipped; the matrix is not instantiated | no | not produced | `ci-full-label.yml:24` | none identified |
| `labeled`, other label | `extra-os` skipped | no | not produced | `ci-full-label.yml:25` | 38982719598 (`skipped`; label not recorded) |
| `unlabeled` | not a trigger | no | not produced | `ci-full-label.yml:11` | n/a |

- Concurrency is `ci-full-label-<PR>-<label>` with `cancel-in-progress: true` (`ci-full-label.yml:13-15`). This is a separate group from `ci.yml`. [SRC]
- `ci:full` also adds windows and macos to later `ci.yml` events that carry the label in the payload: non-draft PR events (`ci.yml:304`, `342`; E1/E3/E7 above). [SRC]
- [INF] Adding `ci:full`, for example through Codex READY (`ci-full-label.yml:1`), and then pushing runs windows twice on nearby heads: once from each workflow, in different groups.

### 3.3 `ci-macos-nightly.yml`

| Event | Jobs run | Paid | Required `linux` | Citation | Observed |
|---|---|---|---|---|---|
| schedule `17 7 * * *` | `recent-commits` (ubuntu); `macos` only if master has commits in the last 24 h | no (macos-latest is free here) | not produced | `ci-macos-nightly.yml:10-12`, `27-53`, `55-59` | 39424842738 (macos ran), 39125856501 (macos skipped) |

- Concurrency is `ci-macos-nightly` with `cancel-in-progress: true` (`ci-macos-nightly.yml:14-16`). [SRC]
- Scheduled runs started 4.5 to 7.5 h after 07:17 UTC: 11:39Z, 12:05Z, 12:46Z and 14:43Z (GitHub schedule delay). [OBS]

## 4. Aggregation semantics of `linux`

Job wiring: `needs: [preflight, settle, linux-tests]`, `if: always()`, ubuntu-latest, 3 min timeout, no checkout (`ci.yml:207-212`). The "Required status" step runs `if: !cancelled()` (`ci.yml:217`) under `set -eu` (`ci.yml:228`). Inputs come in through env, never `${{ }}` in `run:` (`ci.yml:218-226`; asserted at `tools/test_ci_scheduling.py:347-349`). [SRC]

Checks run in this order; the first failure exits 1:

| Step | Input | Pass condition | Otherwise | Lines |
|---|---|---|---|---|
| 1 | `EVENT_NAME`/`EVENT_ACTION` | `pull_request` with action opened, synchronize, reopened or ready_for_review; or `push`; or `workflow_dispatch` | fail: "unexpected pull_request action" or "unexpected event" | `ci.yml:229-234`, `246` |
| 2 | `PR_DRAFT` on `pull_request` | exactly `false` | `true`: fail (draft deferred); any other value: fail (unreadable) | `ci.yml:235-241` |
| 3 | `PR_DRAFT` on push or dispatch | empty | non-empty: fail | `ci.yml:242-245` |
| 4 | `needs.preflight.result` | `success` | `failure`, `cancelled`, `skipped` or empty: fail | `ci.yml:248-250` |
| 5 | `RUN_FULL` | exactly `true` or `false` | empty, `True`, etc.: fail | `ci.yml:251-254` |
| 6 | `PAID` | exactly `true` or `false` | otherwise fail | `ci.yml:255-258` |
| 7 | `needs.settle.result` | `success` for PR synchronize, `skipped` for every other event | any mismatch, including `cancelled`, `failure` or empty: fail | `ci.yml:259-266` |
| 8a | `RUN_FULL=true` | `PAID=true` and `needs.linux-tests.result == success` | Linux tests `skipped`, `cancelled`, `failure` or empty: fail | `ci.yml:267-274` |
| 8b | `RUN_FULL=false` (**unreachable since CSL-376**) | `PAID=false` and Linux tests `skipped` | otherwise fail | `ci.yml:275-283` |
| 9 | whole run cancelled at step time | n/a | "Reject workflow cancellation" exits 1 | `ci.yml:285-289` |

**Producer-side validation (preflight).** The "Scope outputs" shell turns preflight itself into a failure when:

- `run_full` is not exactly `true` or `false` (`ci.yml:115-118`);
- a PR draft flag is not exactly `true` or `false` (`ci.yml:120-125`);
- a non-PR event carries a draft flag (`ci.yml:126-128`).

It emits `paid=true` only for `run_full=true` on a non-draft event (`ci.yml:129-132`). A preflight failure reaches `linux` as step 4. [SRC]

**Paid job admission (`linux-tests`, `ci.yml:158-165`).** It runs only when all of these hold:

- `always() && !cancelled()`;
- preflight `success`;
- `paid == 'true'` and `run_full == 'true'`;
- non-draft (or a non-PR event);
- settle has its expected result.

GitHub `==` is case-insensitive. Exactness comes from the preflight shell (`ci.yml:27-29`; `tools/test_ci_scheduling.py:409-410`). windows and macos also require `needs.linux.result == 'success'` (`ci.yml:297`, `336`). [SRC]

**CSL-376 (merged in c4cf7d8d).**

- `tools/ci_gate.py` `decide_run_full` returns `True` on every path (`tools/ci_gate.py:178-198`), and `main` emits `run_full=true` (`tools/ci_gate.py:467-468`). [SRC]
- So `paid=true` on every non-draft event, row 8b is unreachable, and the docs-only comment at `ci.yml:22` and the `else` branch at `ci.yml:275-283` are stale. [SRC]
- Pruning is tracked in **CSL-382** (Triage, "prune ci.yml's docs-only branch and comments after CSL-376"). The test shell matrix still drives `run_full=false`, which stays valid as a test of the shell. [DEC: Linear CSL-382 description]
- Docs-only PRs and pushes now start the full-test runner. Before c4cf7d8d they did not: [OBS] 39383463299, 39384243800 and 39384747104 show "docs-only scope" with Linux tests skipped.
- [INF] A git failure inside `docs_only()` is not caught in `decide_run_full`. It reaches `main`'s `except (... SubprocessError)` and exits 1 (`tools/ci_gate.py:479-484`), so preflight fails and `linux` fails closed.

## 5. Duplicate and cancellation policy

Without `queue: max`, a concurrency group keeps at most one pending run, and a newer trigger cancels and replaces the older pending run (GitHub docs, controlling workflow concurrency). `cancel-in-progress: false` only protects the run that is already in progress. With `queue: max`, up to 100 pending runs wait in order.

| Workflow | Group | Cancel / queue | Lines |
|---|---|---|---|
| `ci.yml` | `ci-CI-<PR number>`; push `ci-CI-refs/heads/master`; dispatch `ci-CI-<github.ref>` (`refs/heads/<branch>` or `refs/tags/<tag>`) | `cancel-in-progress: true` | `ci.yml:67-69` |
| `ci-full-label.yml` | `ci-full-label-<PR>-<label>` | `cancel-in-progress: true` | `ci-full-label.yml:13-15` |
| `ci-full-matrix.yml` | none (reusable; runs in the caller's run) | n/a | `ci-full-matrix.yml:6-7` |
| `ci-macos-nightly.yml` | `ci-macos-nightly` | `cancel-in-progress: true` | `ci-macos-nightly.yml:14-16` |
| `pr-lint.yml` | `pr-lint-<PR>` | `cancel-in-progress: true` | `pr-lint.yml:10-12` |
| `linear-pr-sync.yml` | `linear-pr-sync-<PR or ref>` | `cancel-in-progress: true` | `linear-pr-sync.yml:18-20` |
| `codex-gate.yml` | `codex-gate-<PR>-request` or `-verdict-<review id>` | cancel in progress only for non-review events; no `queue`, so one pending run per group (each review has its own verdict group) | `codex-gate.yml:12-15` |
| `codex-gate.yml` `verdict` job | `claude-<PR>` | `queue: max`, no cancel | `codex-gate.yml:64-67` |
| `claude.yml` `claude-mention`, `devin-review-fix` | `claude-<PR>` | `queue: max`, no cancel | `claude.yml:438-442`, `1175-1179` |
| `conflict-scan.yml` | `claude-conflict-scan`; resolver `claude-<PR>` | scan: does not cancel the running scan, but a newer trigger replaces a pending one (no `queue`), so a pending event-scoped scan can be dropped; resolver: `queue: max`, no cancel | `conflict-scan.yml:30-32`, `364-369` |
| `stuck-thread-watch.yml` | `stuck-thread-watch` | does not cancel the running watch; a newer trigger replaces a pending one (no `queue`) | `stuck-thread-watch.yml:48-50` |

**Settle (120 s).**

- Settle is a fixed `sleep 120` on every non-draft PR `synchronize`, whatever the scope (`ci.yml:140-152`). It is not a timer that resets on each push.
- The debounce effect comes from the concurrency group: a newer push cancels the older run while it is still in preflight or settle, before Linux tests allocates its runner. [SRC]
- Observed timings: preflight about 9 to 10 s, then settle. So a run is exposed to paid allocation from about 130 s after it starts (39389995068: preflight 07:57:33 to 07:57:42Z, settle from 07:57:44Z). [OBS]
- Non-draft opened and reopened, ready_for_review, push and dispatch skip settle and allocate immediately (`ci.yml:164-165`). A draft opened or reopened event skips `linux-tests` and allocates nothing (E2, E6). [SRC]

**Rapid pushes.** On a non-draft PR, N pushes, each arriving before the previous run has allocated Linux tests (about 130 s after that run started), produce N-1 cancelled runs with no paid start and one paid run. Each cancelled run that had started posts a failed `linux` on its superseded head; a pending run replaced before starting posts nothing (E5, E16). A push after the window cancels a paid Linux tests job mid-run. Pushes to a draft PR start no paid run at all (E4). [SRC] [OBS]; the mid-run case is [INF]: none was observed.

**Identical-head duplicates.**

- [DEC] Founder decision **B2 = NO**: do not cancel an identical-head pending twin. Source: Linear CSL-356 comment 15995b76, 2026-10-20, "Relayed by the CI-lane session from Lucas in chat". It is an attributed relay, not a personal receipt, and it is scoped to the sibling repository's cancel controller.
- Recursion has no cancel controller. Its native `ci.yml` concurrency cancels **any** older run in the PR group, including identical-head twins, and keeps the newest (section 3.1, observed three times). [SRC] [OBS]
- Native concurrency never cancels the newest run in a group, so the head always gets a `linux` from a surviving run unless a later event supersedes it. B2's liveness concern (a cancelled twin leaving a head with no completing run) therefore does not arise in the same form. [INF]
- Whether B2 should constrain recursion is an open question for the founders; nothing here changes it.
- Duplicates that native concurrency does **not** collapse [INF]:
  - a `workflow_dispatch` on a PR branch (group `refs/heads/<branch>`) next to the PR run (group `<PR number>`);
  - `ci-full-label.yml` windows next to `ci.yml` windows on a `ci:full` PR.
- A dispatch on master shares `ci-CI-refs/heads/master` with push runs, so the two cancel each other. [INF]

## 6. Test evidence

Tests are discovered by the gate glob `tools/test_*.py` (`tools/ci_gate.py:53-54`). [SRC] At the base commit these ran locally under Python 3.13 [OBS]:

- `tools/test_ci_scheduling.py`: 12 OK
- `tools/test_ci_gate.py`: 25 OK
- `tools/test_perf_bench.py`: 17 OK
- `tools/test_ci_runner_cost.py`: 11 OK (v2 adds `test_github_hosted_test_runners_are_free`)

**How `tools/test_ci_scheduling.py` works.**

- It extracts each job's `if:` and shell from `ci.yml` by layout (`:45-107`).
- It evaluates the `if:` expressions with GitHub semantics: case-insensitive `==`, status functions and implicit `success()` (`:111-208`).
- It compares them with an independent oracle (`:236-268`) over the grid STATES × label × cancelled × 4³ results × 3² outputs (`:371-384`). STATES are the 4 PR actions × draft, plus push and dispatch (`:32-33`).
- It runs the real shells under bash (`:282-309`).

| Row / rule | Evidence | Status |
|---|---|---|
| Trigger list, concurrency string, permissions | `WorkflowShapeTests.test_triggers_concurrency_permissions_unchanged` (`test_ci_scheduling.py:313`) | PROVEN (shape) |
| Job layout, runners, timeouts, `sleep 120` | `WorkflowShapeTests.test_job_layout` (`:318`) | PROVEN (shape) |
| E1, E3, E6, E7, E11, E13 job admission | `ExpressionTests.test_if_expressions_match_oracle` (`:371`), `test_allocation_policy` (`:386-391`) | PROVEN (expression level) |
| E2, E4 drafts never allocate; `linux` fails | `test_allocation_policy` (`:396-399`), `AggregateTests.test_fail_closed_matrix` (`:493`) | PROVEN |
| E5 burst / settle cancellation | concurrency string only (`:313`); the cancellation itself is GitHub platform behaviour | PARTIAL ([OBS] 39389995068) |
| E8 converted_to_draft is not a trigger | `TRIGGERS` exact string (`:34-37`) | PARTIAL (absence only) |
| E9 labeled/unlabeled do not trigger `ci.yml` | `test_perf_bench.py` `test_unrelated_label_events_do_not_post_linux` (`:217`) | PROVEN (shape) |
| E10 base retarget / `edited` | none | GAP |
| E12 merge_group absent | `TRIGGERS` string, plus shell rejection of unknown events in `test_fail_closed_matrix` (`issue_comment` case, `:503`) | PARTIAL |
| E15 rerun | none | GAP |
| E17 non-draft fork PRs start paid runners once approved | none; no fork guard in `ci.yml`, and the approval setting is reported, not read | GAP |
| E18, E19 platform-suppressed runs | GitHub platform behaviour; nothing in this repository | GAP (source-documented by GitHub only) |
| E16 cancelled run fails `linux` | `test_fail_closed_matrix` (cancelled results, `:511-516`); `test_workflow_cancellation_after_successful_dependencies_fails` (`:524`) | PROVEN (shell); [OBS] the path taken differs (section 3.1) |
| Aggregation steps 1-8 | `test_fail_closed_matrix` (`:493-522`): valid and odd states × good and bad scopes × every dependency in `success/failure/cancelled/skipped/""/pending` | PROVEN |
| Preflight output validation | `PreflightTests.test_normalisation_matrix` (`:455`), `test_wiring` (`:433`) | PROVEN |
| Paid admission blocks incomplete dependencies and malformed outputs | `test_incomplete_dependencies_and_malformed_outputs_block_allocation` (`:415`) | PROVEN |
| CSL-376: always `run_full=true` | `test_ci_gate.py` `AlwaysRunSuiteTests` (`:144`, `154`, `161`, `185`, `191`); `test_ci_scheduling.py` `ClassifierTests` (`:565`, `570`) | PROVEN |
| Row 8b (`run_full=false`) still fail-closed | `test_fail_closed_matrix` scopes `("false","false")`; `test_ci_gate.py` `:209` | PROVEN (unreachable branch) |
| Perf step advisory only | `test_perf_bench.py` `test_linux_job_warns_on_pull_requests_without_a_hard_fail` (`:198`) | PROVEN (shape) |
| `ci-full-label.yml` admission (`ci:full`, non-draft) | string asserts only (`test_perf_bench.py:240-262`) | PARTIAL: the `if:` is not evaluated |
| `ci-full-matrix.yml` has no `linux` job | `test_perf_bench.py:263-269` | PROVEN (shape) |
| `ci-macos-nightly.yml` recent-commit gate | `recent` job is on ubuntu with no Blacksmith (`test_perf_bench.py:277-283`); gating logic untested | PARTIAL |
| Required checks and ruleset | none: repository settings | GAP ([OBS] only) |
| Latest `linux` check run wins on a SHA | none | GAP ([OBS] #252 is consistent with it) |

## 7. Paid workloads outside CI admission

None of these start CI test runners themselves. They spend model or tool usage through secrets or external apps. [SRC]

Their side effects do start CI (free runners since CSL-408):

- A fixer or conflict-resolver push to a **non-draft** PR branch is a `synchronize` (E3): one full Linux test run after settle, plus windows if `ci:full` is on the PR. The exceptions are pushes GitHub suppresses: a head commit with a skip annotation (E18) or a head that is still conflicted (E19) gets no run, so no test runner starts and the required check stays missing on that head. [OBS] Observed fixer pushes started `CI` with actor `claude[bot]` about 8-16 s after the commit: 39499885497 (#254 `8087e7f6`), 39386432470 (#249 `919f52e7`), 39352829266 (#241 `b1eb6ac8`), 39016885558 (#229 `0e1888fa`). [INF] A `synchronize` caused by the checkout's `GITHUB_TOKEN` creates runs in an approval-required state that start only when a writer approves them (GitHub docs, triggering a workflow). These runs started 8-16 s after the commit with no approval step observed, so the pushes most likely used the Claude app credential; the credential itself was not inspected. Earlier commits in a multi-commit push get no run of their own; only the pushed head does. A push to a draft PR is E4 and starts no paid runner (the conflict resolver has no draft exclusion).
- The eight-commit guard bounds both fixer paths: `devin-review-fix` (`cap` step, `claude.yml:1188-1189`) and, since CSL-365, `claude-mention` (step `live`, `claude.yml:498-529`). An `@claude` request on a PR at the cap is skipped instead of pushing.
- When Codex READY newly applies `ci:full`, the `labeled` event starts one windows run from `ci-full-label.yml`, plus windows on every later non-draft PR event until the label is removed. If the label is already on the PR, the gate returns `ready-present` without writing it (`scripts/ci/codex_gate.py:563-564`), so there is no `labeled` event and no extra windows run.
  - The gate removes its own label only when a fixer pause or unresolved findings block readiness on the current head (`scripts/ci/codex_gate.py:507-515`, `scripts/ci/claude_fixer_receipts.py:686`). It does not remove it on every push.
  - A `ci:full` added by a person is never removed automatically. `AGENTS.md:314-319` (mirrored in `CLAUDE.md`) asks to remove it once its full run has passed on the current head (CSL-395, #259), and says a manual removal is a human hold: the Codex gate skips READY on the head where it first sees the removal, and READY on a later head can add the label again (CSL-403, #261).

| Workflow / app | Trigger | Runner | Paid component | Lines |
|---|---|---|---|---|
| `claude.yml` `claude-mention` | `issue_comment` or `pull_request_review_comment` with a whole-word `@claude` from a human, on an open same-repository PR whose author and commenter both have write, maintain or admin permission. `resolve-pr` checks this first and step `live` re-checks it on the current PR. Forks, closed PRs, non-writers, a `claude:pause` or `hold` label, and `CLAUDE_FIXER_PAUSE=true` stop it before the paid action. | ubuntu-latest | `anthropics/claude-code-action@v1` via `CLAUDE_CODE_OAUTH_TOKEN`, `--max-turns 30`, 20 min step timeout | `claude.yml:3-7`, `58-85`, `241-262`, `433-443`, `459-497`, `945-951`, `1025` |
| `claude.yml` `devin-review-fix` | Devin review submitted (`commented` or `changes_requested`), `workflow_dispatch` or `repository_dispatch devin-review-fix`, on an open same-repository PR whose author has write, maintain or admin permission. `resolve-pr` checks this first and the job re-checks it on the live PR. A fork, a closed PR, a non-writer author, a `claude:pause` or `hold` label, or `CLAUDE_FIXER_PAUSE=true` stops it before the paid action. Once admitted, the paid action runs only if the `eligibility` step finds pending findings: unresolved, non-outdated Devin `bug` roots with valid metadata that are not already handled. A review with only analysis comments, malformed metadata, resolved or outdated roots, or handled roots ends at `no-pending-findings` with no model usage. | ubuntu-latest | same action | `claude.yml:8-27`, `58-85`, `241-247`, `264-278`, `1169-1180`, `1204-1230`, `1263-1265`, `1954-1965`, `2410-2417`, `2501` |
| `claude.yml` `event-paths` | `pull_request` touching fixer files | ubuntu-latest | none (tests + actionlint) | `claude.yml:28-48`, `406-432` |
| `conflict-scan.yml` `conflict-resolve` | schedule `11 */2 * * *` or `workflow_dispatch`. A push to master runs only the free scan; the resolver's `if` excludes push (`conflict-scan.yml:358`). The scan selects only open, conflicted, same-repository PRs that are not labelled `needs-decision`, `claude:pause` or `hold` and whose author has write, maintain or admin permission, up to a cap; `CLAUDE_FIXER_PAUSE=true` or a busy fixer selects none (`conflict-scan.yml:52-60`, `155-189`, `336-340`). | ubuntu-latest | claude-code-action via `CLAUDE_CODE_OAUTH_TOKEN` | `conflict-scan.yml:7-12`, `52-60`, `155-189`, `336-340`, `356-369`, `832-837` |
| `codex-gate.yml` `request` | non-draft same-repo PR opened, synchronize, reopened or ready_for_review | ubuntu-latest | posts `@codex review` once per head SHA (Codex usage) | `codex-gate.yml:6-8`, `23-59` |
| `stuck-thread-watch.yml` | schedule `23 */2 * * *`, dispatch, Claude or Codex comments and reviews | ubuntu-latest | may post `@codex review` | `stuck-thread-watch.yml:19-35`, `58-81` |
| Devin Review (external app) | PR creation and on demand; observed on draft heads too | Devin | Devin review; posts the required `Devin Review` status | [OBS] statuses on `11d2f700`, `39c1e70e` |

The Reliability lane owns `claude.yml` (and the fixer family). This contract only lists it.

## 8. Known gaps and follow-ups

1. **CSL-382 (open, Triage):** remove the unreachable docs-only branch `ci.yml:275-283` and the stale comment at `ci.yml:22`. Also stale: `tools/ci_gate.py:10` says the `linux` job runs the gate; `linux-tests` does.
2. **Required-check docs drift:** `CLAUDE.md:310` and `AGENTS.md:310` say "Required check: `linux` (GitHub-hosted)" (v2 wording), and `ci.yml:3` names only `linux`. `Devin Review` (a commit status) is also required, and neither file says so.
3. **Rerun semantics untested (E15):** a rerun replays the original payload (a draft run fails again). [INF] Rerunning an older run on the current head posts a newer failing `linux` on that SHA, and a rerun can cancel the live run in `ci-CI-<PR>`. No test, no observation.
4. **Base retarget (E10):** `edited` is not a `ci.yml` trigger. The `linux` on the head was computed against the old base. Behaviour under the strict policy is not tested.
5. **Dispatch path (E13):** no draft guard, the whitespace check is skipped (no `CI_BASE_SHA`), and it runs in a different concurrency group from PR runs (duplicate paid). It can create a `linux` check run on any branch head; per GitHub's docs that check does not satisfy a PR's required check (unverified here). No dispatch on the current `ci.yml` has been observed.
6. **Identical-head twins:** native concurrency cancels them in recursion, while B2 (CSL-356, sibling repository) says do not cancel. Whether B2 applies here is a founder question; no change is proposed.
7. **Duplicate paid windows:** the `ci:full` label workflow and `ci.yml` windows run in separate groups. This is a policy question; CSL-356 Phase A raised the analogous sibling-repository label re-runs.
8. **merge_group:** absent everywhere (E12). If a merge queue is enabled, `linux` is never produced, which fails closed by blocking merge. Needs a workflow change first.
9. **Untested admission logic:** the `ci-full-label.yml:23-25` `if:` and the `ci-macos-nightly.yml:46-58` 24 h gate are checked only as strings.
10. **Cost comment drift:** `ci-macos-nightly.yml:1` says macOS is about $0.08/min, while `tools/ci_runner_cost.py:18` treats `macos-latest` as free on this public repository. Nightly runs also start 4.5 to 7.5 h after the cron time.
11. **Mid-run cancellation cost:** a push more than about 130 s after the previous run started cancels a running `linux-tests` job, losing its runner time. That was billed before CSL-408 and is free since. Not observed in the window; not measured.
12. **Cancelled-run path:** in observed cancellations, `linux` failed through "Required status" (`cancelled()` false), not through "Reject workflow cancellation". Both fail closed. The test models only the `cancelled()=true` branch for the reject step.
13. **Error path:** a git failure in `docs_only()` makes preflight fail through `tools/ci_gate.py:479-484` (inferred, untested).
14. **Org ruleset listing:** `orgs/CSL-Laboratories/rulesets` is not readable from this session. Only rulesets that apply to recursion were read.
15. **Fork PRs (E17):** non-draft fork PRs can start the test runners once a maintainer approves the run. They're free since CSL-408, but they still use capacity. Lucas reports setting fork approval to "Require approval for all external contributors" (2026-10-20). An admin should confirm it, since this session cannot read it. A workflow guard would need a human-assigned workflow issue.
16. **Fixer push credential (section 7):** the fixer jobs check out with `actions/checkout@v4` and no `token:` (`claude.yml:532`, `1997`), so a plain `git push` would use `GITHUB_TOKEN`, whose `synchronize` creates approval-required runs (GitHub docs, triggering a workflow). The jobs also hold `id-token: write` (`claude.yml:449`, `1186`), which the Claude action can exchange for an app token. Observed fixer pushes did start `CI` as `claude[bot]` (section 7 [OBS]), so the app credential is the [INF] explanation. The push credential has not been read from a job log. If a future fixer push uses `GITHUB_TOKEN`, its runs would start no runner until a writer approves them, so the required check stays missing on that head until a writer approves the run or a human pushes.
