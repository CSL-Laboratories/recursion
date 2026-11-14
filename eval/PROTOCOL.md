# Evaluation protocol (lane EVAL), version 1

Written before any measured run. This file, the analysis script `eval/analyze.py` and the calculator
`eval/passk.py` are hashed in `eval/FROZEN.sha256`; every record carries those hashes, and a test fails
when a file and its hash disagree. A change to any of the three is a new protocol version: it changes
the hash, it is listed at the end of this file, and it never applies to a run already made.

Sources: the product contract's rows N6 (decided: the full comparison first), O55 (decided: a
10-point pass^k difference; proposed: about 200 tasks at 10 attempts, sized by a pilot), 11.18
(proposed: the record, the calculator with per-task counts, k of at least 5, task-level intervals,
the held-out guard) and the infra-failure rule (proposed). The vault records this lane read are named
in the pull request with what was used and what was set aside.

## 1. What is compared

Two arms, one engine at a time:

- **direct**: the engine run as its own command-line program in the task repository.
- **recursion**: the same engine run under Recursion, through the control socket, working in the task
  repository through WORKSPACE's `workspace` field, its usage read from EVENTS' usage events.

Both arms use the **same model, the same route (the same signed-in account) and the same effort**, and
the same permission settings. These settings are fixed in the suite manifest (`arms`), hashed into the
record (`arm.settings_sha256`) and into the plan (`plan.plan_sha256`). A run whose arm settings differ
from the other arm's is not a paired comparison.

The recursion arm refuses, with a message naming what is missing, until WORKSPACE's `workspace` and
EVENTS' usage events are both on the evaluated base. A refused arm writes no record; the other arms run.

## 2. Tasks

A task is a repository state (the start tree), a prompt, and hidden tests. It is kept only if its hidden
tests **fail on the start tree and pass on the start tree with the reference solution**, on every one of
two runs (a task whose result changes between runs is flaky and is dropped). The start tree's git tree id
is pinned and checked again at every attempt.

- **Dev tasks** (`eval/devset/`, 12 tasks) live in the repository. They are for building and shaking down
  the runner, never for a reported number.
- **Held-out tasks** are harvested (`eval/harvest.py`) from merged commits of public repositories, after a
  not-before date: the parent's tree is the start tree, the linked issue's text (or else the commit
  message without its trailers) is the prompt, the commit's versions of the test files it changed are the
  hidden tests, the rest of the commit is the reference solution. The harvest script's SHA-256 and the
  repository list's SHA-256 (`python -m eval.harvest hashes`) are published before the freeze, and the
  list is never widened after it.
- Held-out tasks load only from a folder given at run time (`--tasks`), never from the repository, and
  only for scoring (`eval/heldout.py`). No dev-mode or optimizer-side code path can open them; a test
  shows it. Every engine runs as the same operator, so the operator keeps the held-out folder out of an
  engine's reach during a run (on a path no prompt, setting or work tree names); the guard covers the
  repository's code paths, not a curious engine.
- Wording: harvested tasks are "post-cutoff" and "held out from the optimizer". They are never called
  uncontaminated.

## 3. Running

One command runs a pinned suite through each arm (`eval/run`). For every attempt:

1. a new TMPDIR under `/var/tmp/csl-lanes-eval/<attempt>` and a new XDG_RUNTIME_DIR under
   `/dev/shm/csl-lanes-eval/<attempt>`, both created by the runner, both removed afterwards, and
   DBUS_SESSION_BUS_ADDRESS never passed on;
2. a fresh copy of the start tree, made into a one-commit repository whose tree must equal the pin
   (otherwise the attempt is an infra failure, `start_tree_mismatch`);
3. the engine run in it with the prompt, at most the task's time limit, in its own process session; every
   process left in that session is killed when the engine exits or runs out of time;
4. the final diff taken against the start commit (new files included);
5. the hidden tests written over the work tree (over whatever the engine left at those paths) and run;
   pass means exit 0.

Times are monotonic. The engine's output, the diff and the test log are kept (and hashed into the
record) when an artifacts folder is given; for a held-out suite that folder may not be inside the
repository.

**Order.** Trial index outermost; the tasks shuffled for each index from the run's seed; the arms
alternating which goes first. No retry is hidden: a trial that ran is in the record.

**Attempts and k.** k is at least 5. The pilot runs 5 attempts per task (so a task's pass^5 is a 0-or-1
indicator); the full comparison runs 10 (O55), which estimates pass^5 far more stably.

**Replacement trials.** When a planned trial ends with an infra or grader failure, the runner adds one
replacement trial for that task and arm, at most 2 per task and arm. The planned trial stays in the
record and counts as a failure in the headline; the replacement counts only in the valid-runs score.

## 4. The record

One `recursion-eval-run/1` record (`eval/schema/recursion-eval-run-1.json`) per suite run of one arm. It
binds the suite (manifest SHA-256), the runner (version and SHA-256 of its files), the arm and its
settings, k, the plan and every trial. Every planned trial has a row, a trial that never ran included
(class `not_run`). An unknown value is null and is counted as unknown, never as 0. No aggregate is
stored: every number is recomputed from the trials.

Each trial has a verdict (`resolved`, `unresolved`, `not_graded`) and an error tier saying whose failure
it is:

| tier | classes | meaning |
|---|---|---|
| none | | resolved |
| agent | tests_failed, timeout, engine_crash, no_result, engine_error | the arm's failure, a timeout included |
| infra | setup_failed, start_tree_mismatch, engine_missing, vendor_limit, auth_failed | the harness or the vendor; an unresolved trial is infra only for a typed vendor limit or sign-in failure |
| grader | grader_failed, no_tests_ran | the hidden tests could not give a verdict |
| unknown | not_run, unclassified | not attributable |

A hidden-test run that passes its time limit counts as `tests_failed` (the reference solution was
validated within that limit). "False done" is a valid trial (tier none or agent) whose engine claimed to
be done and whose hidden tests did not pass.

## 5. Scoring

- **Headline**: the plan is the denominator. A task's n is the planned attempts; c counts its planned
  trials that resolved. Infra, grader, unknown and not-run trials are failures here, as the contract's
  infra rule says.
- **Valid runs only**, reported beside the headline, declared here before any run: a task's trials,
  planned and replacement, of tier none or agent. The number left out is reported per tier, with the
  bounds of scoring them all failed (the headline) and all passed.
- **pass^k** per task is C(c, k) / C(n, k) with that task's own n; the suite value is the unweighted mean
  over tasks. A task with n < k is "not estimable": the headline is then not given at all (the run is
  incomplete), and the valid-runs score is the mean over the estimable tasks with the others named.
- **pass@1** per task is c / n, averaged over tasks; the first-attempt rate (trial 1 alone) is reported
  beside it.
- **Tokens**, split as uncached input, cache write, cache read and output, and **cost** with its unit and
  basis (`billed`, `reported_not_billed`, `unknown`): sums over the trials that report them, with the
  count that do not. Cached input is never folded into uncached input. Cost per resolved trial counts
  every attempt and is given only when no trial's cost is unknown.
- **Wall time**: p50 and p95 (nearest rank) over all trials that ran and over the resolved ones.

## 6. Intervals and comparisons

- Intervals resample **tasks**, never trials: a percentile bootstrap over per-task values with 10,000
  resamples (at least 1,000) and a fixed seed, and a normal interval over the same values (raw, and
  clipped to [0, 1] for display). Resampling trials inside a task counts within-task variance about twice
  and, for k above 1, centres on the wrong value.
- When the tasks come from two or more source repositories, a clustered bootstrap (repositories, then
  tasks inside each drawn repository) and the leave-one-repository-out means are reported too.
- **Paired difference**: two records of one pinned suite (same manifest SHA-256, same k, same planned
  attempts) are aligned by task id; d = B - A per task (B the recursion arm); the same task indexes are
  drawn for both; the difference lies in [-1, 1] and is never clipped.
- **Sizing**: the paired standard deviation of a pilot gives an estimate of the tasks a full run needs to
  see a 10-point difference (two-sided 0.05, power 0.8). It is an estimate, not a power claim.

## 7. Claims

- Every number is printed with its label: **dev**, **screen**, **pilot** or **full**. A run on the dev set
  is never a measurement. Pilots stay labelled pilots until the full comparison meets O55, and no
  per-competitor score is published before it.
- If the paired interval contains 0: "no detectable difference", with the interval, and never
  "equivalent"; the most it rules out is a loss larger than the interval's lower end.
- A difference whose interval excludes 0 in a screen or a pilot is "an observed difference" with no
  superiority wording. Only the full comparison may say one arm is higher.
- The sized full comparison is about 200 tasks at 10 attempts, about 2,000 attempts per arm per engine; at
  about 25 minutes an attempt and 4 at a time that is about 200 hours per arm. It does not fit before
  the launch; what fits is the pilot, labelled as one.

## 8. Known limits

- Every engine runs as the same operator: a held-out folder the operator can read, an engine can read.
- A process an engine starts in a new session of its own is outside the session the runner kills.
- The grade is the hidden tests' exit code; tests that were wrong on the commit (grader defects) are
  caught only by the fail-on-parent, pass-on-commit check, not proven absent. A corrected grader is a new
  protocol version, and both scores are reported.

## Versions

- 1 (Nov 14, 2026): first version, before any measured run.
