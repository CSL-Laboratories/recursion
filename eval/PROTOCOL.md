# Evaluation protocol (lane EVAL), version 5

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
the same permission mode and extra engine arguments. These shared settings are fixed in the suite
manifest (`arms`), hashed into the record (`arm.settings_sha256`, the same value for both arms: the arm's
name and how it reaches the engine, such as the binary path, passed variable names or route home, are
not part of it) and into the plan (`plan.plan_sha256`). The direct arm passes effort and permission mode
to Claude Code with Claude Code's own `--effort` and `--permission-mode` flags, and nothing else may set
them: extra engine arguments that would set effort, model, permission mode or settings are refused, and
only the variables that say where the sign-in lives and how the network is reached (`CLAUDE_CONFIG_DIR`,
proxy and certificate variables) may be passed to an engine, because an environment variable can
override a flag. For the same reason the direct arm runs Claude Code only with no settings file in
effect: a settings file can set the effort, the model, the permission mode or any environment variable
after launch, over the flags (Claude Code's documented precedence), so the record would not say what ran.
A file that sets anything (an empty object, or one holding only `$schema`, sets nothing; a file that
cannot be read counts as set) refuses the arm, naming it, in the engine's user configuration folder
(`CLAUDE_CONFIG_DIR` when passed, otherwise the route home's `.claude`), in the machine's managed policy
(`/etc/claude-code/managed-settings.json` and `managed-settings.d/`) and, before any attempt, in any
task's start tree (`.claude/settings.json`, `.claude/settings.local.json`); the same places are checked
again at each attempt, with the attempt's own environment and work tree, and a file found only then makes
that attempt an infra failure (`setup_failed`) without starting the engine. The configuration folder must
be an absolute path (`CLAUDE_CONFIG_DIR` when passed, even empty, and the route home), before the run and
at each attempt: the engine starts in the task's work tree and would read a relative folder, or a literal
`~`, from there rather than where it was checked. Engine arguments that set a
fallback model or a main-thread agent are refused as well. A paired comparison refuses two records that differ in these settings, the engine
version, the runner, the evaluated commit, the protocol or the analysis; and a record is scored only by
the protocol and analysis version it names: a record of any other version is refused, never scored by
this one.

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
  hidden tests, the rest of the commit (test files it deleted included) is the reference solution, and
  the start tree with the reference solution and the hidden tests must be exactly the commit's own tree
  (its tree id is pinned and checked, deletions and file modes included). The harvest script's SHA-256 and the
  repository list's SHA-256 (`python -m eval.harvest hashes`) are published before the freeze, and the
  list is never widened after it.
- Held-out tasks load only from a folder given at run time (`--tasks`), never from the repository, and
  only for scoring (`eval/heldout.py`); every input the loader reads (the manifest, each task file or
  folder) must lie inside that folder and outside the repository and be reached through no link, and a
  task folder holds no link at any depth. No dev-mode or optimizer-side code path can open them; a test
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
   process left in that session, whatever process group it moved to, is killed when the engine exits or
   runs out of time, rescanning until none is left, an empty scan confirmed by a fresh one; a cleanup
   that cannot empty the session within 10 seconds, or that leaves the engine's output pipes held open by
   a process outside the session, is reported at once, without waiting for the engine or its pipes, and
   the attempt is not graded; no wait is unbounded; variables passed to the engine can never replace the
   attempt's TMPDIR, XDG_RUNTIME_DIR, HOME or PATH;
4. the final diff taken against the start commit (new files included);
5. the hidden tests written over the work tree (over whatever the engine left at those paths) and run;
   pass means exit 0.

Times are monotonic. The engine's output, the diff and the test log are kept (and hashed into the
record) when an artifacts folder is given; a measurement (screen, pilot, full) must give one; for a
held-out suite it may not be inside the repository. A failure to store them never costs the trial its
row: the record says `retained: false` with the reason.

**Before any attempt.** The plan is checked (k and attempts positive integers, attempts at least k,
replacements a non-negative integer, the seed an integer), and so is the label (section 7). A frozen
suite must pin the version and the settings of every selected engine (for a real engine all of model,
route, effort and permission mode, none of them empty), and name as its evaluated commit the commit
actually checked out, with nothing changed under `core/`.

**Interrupted runs.** A run that is interrupted keeps every trial that ran; only trials that never ran
are written as `not_run`.

**Order.** Trial index outermost; the tasks shuffled for each index from the run's seed; the arms
alternating which goes first. No retry is hidden: a trial that ran is in the record.

**Attempts and k.** k is at least 5. The pilot runs 5 attempts per task (so a task's pass^5 is a 0-or-1
indicator); the full comparison runs 10 (O55), which estimates pass^5 far more stably.

**Replacement trials.** When a planned trial ends with an infra or grader failure, the runner adds one
replacement trial for that task and arm, at most 2 per task and arm. The planned trial stays in the
record and counts as a failure in the headline; the replacement counts only in the valid-runs score. A
record is valid only if each replacement replaces one earlier infra or grader failure of the same task,
once, within the plan's budget, at the next index after the planned attempts (a failed replacement may
itself be replaced, within the budget).

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
  planned and replacement, of tier none or agent. The trials it leaves out are reported per tier, failed
  replacements included; the bounds score the planned ones among them all failed (the headline) and all
  passed.
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

- Every number is printed with its label: **dev**, **shakedown**, **screen**, **pilot** or **full**. The
  dev set is labelled dev, and only it; a shakedown (for example the stand-in engine on a held-out
  suite) checks the harness. Neither is ever a measurement. A screen, a pilot and a full comparison need
  k of at least 5, a held-out frozen suite and a real engine (never the stand-in, which is an oracle).
  The runner refuses, and the record checks, all of these.
- Nothing a run writes is published by default: no record, write-up or artifact is committed, pushed
  or posted unless the owner decides so (`eval/.gitignore` keeps `results/` and `artifacts/` out of git).
  Pilots stay labelled pilots until the full comparison meets O55, and no per-competitor score is
  published before it.
- If the paired interval contains 0: "no detectable difference", with the interval, and never
  "equivalent"; the most it rules out is a loss larger than the interval's lower end.
- A difference whose interval excludes 0 in a screen or a pilot is "an observed difference" with no
  superiority wording. Only the full comparison may say one arm is higher.
- The sized full comparison is about 200 tasks at 10 attempts, about 2,000 attempts per arm per engine; at
  about 25 minutes an attempt and 4 at a time that is about 200 hours per arm. It does not fit before
  the launch; what fits is the pilot, labelled as one.

## 8. Known limits

- Every engine runs as the same operator: a held-out folder the operator can read, an engine can read.
- A process an engine starts in a new session of its own is outside the session the runner kills (if it
  still holds the engine's output, the attempt reports a cleanup failure); so is a chain of processes
  each forking and exiting inside one scan of `/proc`.
- Settings that are not files on this machine (server-managed settings an organization delivers, or on
  WSL a Windows policy it inherits) are not checked; the operator runs on an account and machine with
  none.
- The grade is the hidden tests' exit code; tests that were wrong on the commit (grader defects) are
  caught only by the fail-on-parent, pass-on-commit check, not proven absent. A corrected grader is a new
  protocol version, and both scores are reported.

## Versions

- 1 (Nov 14, 2026): first version, before any measured run.
- 2 (Nov 14, 2026, answering the two first reads of #381; still before any measured run): the shakedown
  label and the label rules enforced; comparisons refuse different shared settings, engine version,
  runner, evaluated commit, protocol or analysis; the shared-settings hash leaves out the arm's name;
  effort and permission mode reach Claude Code; the valid-runs exclusions count failed replacements;
  replacement trials, frozen pins, the evaluated commit and held-out sources are checked; harvested
  tasks are checked against the commit's own tree; interrupted runs keep every trial that ran; storage
  failures keep the trial; numbers must be finite. The analysis changed (`analyze.py`); the calculator
  (`passk.py`) did not.
- 3 (Nov 14, 2026, answering the two confirming reads of #381; still before any measured run): a record
  of another protocol or analysis version is refused, not scored; effort, model and permission mode can
  be set only by the named settings (overriding engine arguments and passed variables are refused); the
  seed must be an integer; a frozen real-engine suite pins model, route, effort and permission mode; a
  screen needs a frozen suite too; held-out inputs are reached through no link and task folders hold
  none; session cleanup rescans and reports a failure; nothing is published by default. The analysis
  changed (`analyze.py`); the calculator did not.
- 4 (Nov 14, 2026, answering the two third-round reads of #381; still before any measured run): the direct arm
  runs only with no Claude Code settings file in effect (user folder, managed policy, task repository),
  and refuses fallback-model and agent arguments; session cleanup confirms an empty scan with a fresh
  one, reports a failure at once with no unbounded wait, and reports output pipes held open by a process
  outside the session. Neither the analysis nor the calculator changed.
- 5 (Nov 14, 2026, answering the two fourth-round reads of #381; still before any measured run): the
  engine's configuration folder (`CLAUDE_CONFIG_DIR` when passed, the route home) must be an absolute
  path, before the run and at each attempt. Neither the analysis nor the calculator changed.
