# CI runbook

What to do when CI looks wrong on a PR or on `master`. This page is procedure only. What each
event does to each check is in the [event contract](ci-event-contract.md) (cited as "contract §N"
or by row, E1-E19); agent permissions are in [AGENTS.md](../AGENTS.md). Where they disagree, the
contract and the code at its base commit win. Tracked under CSL-255 exit criterion 5 (CSL-416).

**Who acts.** Founders may do every step below. An agent may read anything, push to its own PR
branch and comment, but marking ready, adding `ci:full`, and any dispatch, re-run or `/ci` command
start CI runs and follow AGENTS.md's proceed/escalate rules (AGENTS.md:82-84). Escalate on the
Linear issue with an @mention; a GitHub mention alone is not enough.

## 1. Reading the required checks

`master` requires **`linux`** and **`Devin Review`** (contract §2).

| You see | It means | Do |
|---|---|---|
| `linux` green on the current head | Preflight, settle and Linux tests passed for that head (E1, E3; contract §4) | Nothing. |
| `linux` red on a draft | Expected: drafts defer qualification until `ready_for_review` (E2, E4) | Mark ready when done. |
| `linux` red, non-draft | A test failed, or settle or Linux tests were cancelled or skipped: `linux` fails closed (contract §4) | Open the failing job; for a cancelled run see §3. |
| `linux` red on an older head after a push | Normal: the newer push cancelled that run, which still posts `linux` on its own superseded head (E5, E16) | Nothing; the current head decides. |
| No `linux` on the head | No run started for that head: a pending run replaced before it started (E5, E16), a skip annotation (E18), an unresolved conflict (E19), or a run waiting for approval (E17; contract gap 16) | §4 below. |
| `Devin Review` missing or pending | External app; not produced by this repository's workflows (contract §2) | Ask for a review through the Discord bot (`/devin review pr:<url>`); do not push to retrigger it. |
| Red `watch` (stuck-thread watch) with `login_skip=1` and "API rate limit exceeded" | Not a CI failure (AGENTS.md:320-324) | Nothing; the required checks decide. |

## 2. Re-running

- **Prefer "Re-run failed jobs" on the same head** over an empty push. A push to a non-draft PR
  waits 120 s in `settle` before Linux tests (E3) and dismisses approvals.
- **Flaky test:** re-run once. If it fails again on the same head, treat it as real and file a Bug.
  Re-run semantics are an accepted untested gap (contract §6, E15).
- **`ci:full`:** add it when Windows and macOS evidence is needed; remove it once that full run
  has passed on the current head (AGENTS.md:314-319). It persists otherwise and repeats Windows and
  macOS on every later non-draft PR event.

## 3. Cancellation

- Native `ci.yml` concurrency cancels any older run in the PR's group, including an identical-head
  twin, and keeps the newest (contract §5). There is no cancel controller in this repository.
- Never cancel the current head's run by hand; that leaves `linux` red or missing until a new push.

## 4. A required check is stuck or missing

1. Is the PR a draft, conflicted (E19), or is its head commit skip-annotated (E18)? Fix that; no
   run is created until then.
2. Actions tab shows **"Approve workflows to run"**: the push came from `GITHUB_TOKEN` automation or
   a fork (contract §7, gap 16; E17). A writer approves it there.
3. A run is queued for minutes: check the organization's runner capacity (Team plan: 60 concurrent
   jobs, 5 macOS, shared by every repository and bot workflow in the organization).
4. Otherwise push nothing yet; comment on the Linear issue with the run IDs and @mention the CI
   owner.

## 5. Stop or roll back automation

| Lever | Effect | Who |
|---|---|---|
| Label `claude:pause` or `hold` on a PR (any case) | The fixer and `@claude` jobs stop before checkout or reservation, and a pause added later stops the push (claude.yml:463-475, 1200-1210, 685-706); the conflict scan never selects a paused PR (conflict-scan.yml:79, 170) | Any writer; agents must never remove a pause |
| Repository variable `CLAUDE_FIXER_PAUSE=true` | Same, for every PR (claude.yml:467, 1204; conflict-scan.yml:336-337, 396-397) | Admin |
| Remove `ci:full` | Stops repeat Windows/macOS on later events | Human |
| Actions → workflow → **Disable workflow** | Stops new runs of that workflow | Admin; record it on Linear |
| Revert the PR that changed a workflow | Restores the previous behaviour | Founder merge |

## 6. Cost and capacity check

- Every job must use a standard GitHub-hosted label (`ubuntu-latest`, `ubuntu-24.04`,
  `windows-2025`, `macos-latest`; contract §1). Since CSL-408 these are free **because the
  repository is public**. Making it private would meter minutes (Team includes 3,000 per month);
  re-plan before that.
- `tools/test_ci_runner_cost.py` prices runner labels; `CrossLaneAdmissionTests` in `tools/test_ci_scheduling.py` fail
  if a Claude writer workflow leaves GitHub-hosted runners.
- Usage over time: the criterion 2 measurements on CSL-255 (queue wait, time to required check,
  concurrency).

## 7. Changing a workflow

- `.github/workflows/**` changes need a Linear issue explicitly assigned by a human (AGENTS.md
  Guardrails). `scripts/ci/**` and `tools/ci_gate.py` are protected paths: they need a separately
  recorded founder-approved authoring session (`CONTROL_PLANE.md`).
- **Contract rule (agreed by the CI and Reliability lanes, 2026-10-21; founders decide whether it
  becomes a repository rule):** a PR that changes a workflow, `tools/ci_gate.py`, a required check
  or the rulesets also updates [ci-event-contract.md](ci-event-contract.md) in the same PR, including
  re-pinned `file:line` citations when lines move, or says "no contract row changes" and why.
- The Claude writers (`claude.yml`, `conflict-scan.yml`) must keep the properties that
  `CrossLaneAdmissionTests` (`tools/test_ci_scheduling.py`) check: no CI dispatch, re-run or CI label write; one exact
  non-force `git push origin HEAD:refs/heads/<branch>` permission per writer job; one shared
  `claude-<PR>` lock without cancellation; the eight-commit guard on the fixer and `@claude` jobs,
  three attempts, and the pause. The conflict resolver has no eight-commit guard; it runs on
  conflicted PRs that the scan selects, every two hours and on `master` pushes (conflict-scan.yml:8-11).

## 8. Owners

| Area | Owner |
|---|---|
| CI workflows, required checks, this runbook, the contract | CI lane (Lucas's session), founders approve |
| `claude.yml`, `conflict-scan.yml`, fixer scripts | Reliability lane, founders approve |
| CI failure on `master` | Devin "Main CI failure fixer" automation opens a Linear Bug and a fix PR (CLAUDE.md Post-merge); a founder triages |
| Rulesets and repository settings | Founders (admins) |

Ownership changes over time; check the Linear assignee before acting.
