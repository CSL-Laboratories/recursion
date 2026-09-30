# Build Recursion with a construction swarm

This tool applies that prior fleet PR86's method to **building Recursion**. It is not a
Recursion runtime integration, does not select v3's product language/store, and
does not operate the portfolio. Lucas owns objectives and merges; the current
authorized lead supplies packets, reviews results, and integrates changes.

For the native WSL tool-enabled route and Cursor/OpenCode coordinators, use
[the tool pipeline runbook](TOOL-PIPELINE.md) and [its qualification evidence](TOOL-VALIDATION.md).
The remainder of this page documents the original version-1 packet adapter.

## What runs

`python -B -m tools.construction_swarm` provides a durable queue, bounded parallel
dispatch, pinned Git inputs, separate attempts, dependency-based synthesis,
proposed-file staging, and Linux sandbox tests. It uses only Python's standard
library. Run it from the repository root on Python 3.11 or newer.

The implemented and smoke-tested adapter is **Windows Devin CLI 3000.10.21, SWE-2 Max**, with all
model tools denied and native subagents disabled in a fresh per-attempt config.
The worker receives a complete source packet in its prompt and returns JSON.
It can reason, review, and propose complete code files. It cannot inspect your
machine, execute its proposed tests, or directly modify the checkout through
the allowed tool profile. This is a restricted reasoning route, not a claim
that native Windows Devin has a filesystem sandbox. Its trusted CLI still
uses the existing user's authentication and stores its own session history.

The installed CLI must be pinned by executable SHA-256. Its exported model name
must be `SWE-2 Max`; a tool attempt, missing export, substituted model, malformed
filing or uncertain execution is held visibly. Native CLI process trees use a
Windows Job with membership at process creation, kill-on-close, 16-process and
2 GiB limits. `_windows.py` is copied from the existing interface probe so the
construction tool does not edit or load evolving historical experiments.

The fixture adapter exercises scheduling with **zero inference**. Linux model
launch, Cursor/OpenCode launch and automatic Terra/Sol routing are not qualified
in this increment. Coordinators using those tools in their existing authorized
sessions can prepare the same job JSON; this dispatcher executes the leaves.

## Team method

1. Lead freezes a parent goal, source commit, exact inputs, acceptance question,
   deadline and dispatch allowance. Start from a fresh fetched review worktree.
2. Domain coordinators prepare distinct job briefs. Suggested domains: formal
   counterexamples, independent checker review, process/recovery, interface
   compatibility, and evidence audit. An example is in `jobs.example.json`.
3. One dispatcher launches eligible leaves, with at most eight per domain and
   at most fifty model jobs. Dependencies release only after valid filings return.
   A dependent synthesis is explicitly processing unverified evidence; returned
   does not mean accepted. A failed/unknown/rejected dependency blocks downstream
   jobs. The full status manifest retains failures and uncovered work.
4. Separate synthesis jobs cite immutable filing IDs and original source hashes,
   preserve dissent, and identify missing evidence. The lead opens cited original
   artifacts instead of relying only on a coordinator's summary.
5. Development jobs return proposed files from an exact path allowlist. Stage them
   in a new external directory, run an independently chosen gate in Linux, review
   the diff and evidence, then integrate into the review branch. No worker output
   triggers Git commands, test commands, acceptance, publication or another launch.
6. Lead records `accept` or `reject` with an independent review/evidence reference.
   This is a construction disposition, never a formal proof or runtime gate PASS.
   Commit/publish through the repository's approved publication procedure. Lucas merges.

When a packet is complete, prepare a fresh plan pinned to the next reviewed source
with the next concrete questions. Continue within the current owner's campaign
scope/deadline. The tool does not invent new research, extend the deadline, redeem
quota, change models, or relaunch itself after a reset.

## Prepare and launch

Choose a new **external** run directory, e.g. `D:/RecursionSwarm/run-001`; do not
put it in a checkout, including its ignored directories. Keep plans/receipts private
unless deliberately sanitized for publication. Supply only owner-approved tracked
text files; hidden files, symlinks and submodules are rejected. Explicit input
selection is a disclosure grant, not an automatic secret-detection guarantee.

Example PowerShell, from the review checkout:

```powershell
python -B -m tools.construction_swarm.prepare --repo . --output D:/swarm-plan.json --run-id research-001 --authorization "Lucas: bounded research packet, <date and scope>" --job-file docs/v3/construction-swarm/jobs.example.json --devin C:/Users/Lucas/AppData/Local/devin/cli/bin/devin.exe --slots 2 --minutes 30
python -B -m tools.construction_swarm init --repo . --plan D:/swarm-plan.json --root D:/RecursionSwarm/run-001
python -B -m tools.construction_swarm run --root D:/RecursionSwarm/run-001
```

Inspect the generated plan before initialization. `prepare` defaults to fixtures
unless `--devin` is explicit. Validate current `devin models list` entitlement:
the observed Free label is not an unlimited-usage guarantee. `capacity_basis`
records the operator's evidence; it is not a provider quota API or proof.

Live runs serialize through a per-Windows-user provider-pool lock in
`%LOCALAPPDATA%/recursion-construction/pool`. Durable campaign registrations retain
unknown reservations across runs. A separately scoped campaign may use remaining
capacity only when its requested slots plus prior unknown/running reservations
fit the shared fifty-slot ceiling and measured host headroom. Prior dispatch
charges and unknown evidence remain intact; there is no automatic retry.
Fully returned/accepted/rejected campaigns retain a compact zero-hold completion
record, allowing their run directories to be archived. Unknown or retryable runs
must retain their ledger; missing evidence prevents admission.
This coordinates this tool's live runs only, not other applications,
accounts or machines. `model_slots` is a ceiling, never a promise of host capacity.
The dispatcher limits live slots by observed initial free RAM minus a 3 GiB reserve,
charging 2 GiB per admitted slot; it rechecks at least 5 GiB free RAM and 1 GiB
free evidence disk before each launch. This is conservative admission, not a
cross-application host governor. Start with two, measure, then increase deliberately.

`max_dispatches` is cumulative across attempts; failures are never refunded.
It bounds local CLI launches, not provider tokens, remote computation or dollars.
Provider usage receipts distinguish observed metrics from missing telemetry.
No metered fallback is implemented. Read-only global configuration may still be
loaded by Devin; global MCP servers cause refusal. Use a clean controller-owned
run parent with no `.devin` project configuration/hooks or unrelated rule files.
The lead must qualify any additional startup imports before using that host.

## Monitor, stop, retry and recover

```powershell
python -B -m tools.construction_swarm status --root D:/RecursionSwarm/run-001
New-Item D:/RecursionSwarm/run-001/STOP -ItemType File
python -B -m tools.construction_swarm manifest --root D:/RecursionSwarm/run-001 --output D:/run-001-manifest.json
```

Status uses a read-only SQLite snapshot while the dispatcher is active. Status
and manifest reports retain every job's status even when one filing is damaged;
that entry has an `artifact_error`, and its content is withheld. Independent
accept/reject still requires strict verification. STOP
prevents admission and cancels active local clients. Cancellation does not prove
that remote inference stopped; such attempts become `unknown`, retaining slots
and pool ownership. Deadlines have the same semantics. After a controller crash,
the next run marks recorded running jobs unknown and never auto-relaunches them.

Jobs that cannot be admitted become `blocked`, with a reason in the manifest.
Known failures before launch, failed fixture jobs and blocked jobs may use
`retry --job ID --reason "specific repair"`
within the original attempt/dispatch/deadline caps. Unknown provider completion
is never automatically retried. `reconcile --job ID` accepts only retained positive
evidence: normal zero-exit CLI completion without timeout/cancellation, observed
local quiescence, a matching packet, the exact requested model, and a schema-valid
export with no tool calls. A partial export from an interrupted client never suffices.
It preserves the original
unknown receipt and cumulative dispatch charge. Explicitly retry any blocked
dependents whose prerequisites are now satisfied. Then `run` can continue queued
work or release the pool if nothing remains. If that evidence is absent, preserve
the hold and investigate actual provider/process state; there is no force-clear.
Do not delete ledger rows, reset budgets, or clear a live lock.
Failures before entering the transport record `launch_entered: false` and remain
charged but do not retain a live slot. If writing the external receipt fails, the
ledger retains the error and packet identity. Crashes and uncertain submissions
still hold capacity; missing terminal evidence is not proof of non-execution.

## Proposed code and isolated tests

```powershell
python -B -m tools.construction_swarm stage --root D:/RecursionSwarm/run-001 --job implement-checker --destination D:/RecursionSwarm/staged-001 --output D:/stage-001.json
```

Staging exports only the job's frozen input files plus validated proposed files.
The controller registers the exact stage manifest and verifies its current filing
binding before testing; a caller-authored manifest cannot authorize a source mount.
It creates a fresh directory; it never applies a patch to an existing checkout.
Review the proposed file diff, including its tests. A generated test is not an
independent acceptance oracle. Do not select an unreviewed test as the sole gate.

On Linux/WSL, invoke `test --root ... --stage-manifest ... --entry path/to/gate.py`
using native Linux paths in the staging manifest. The gate must be a Python entry
file in the frozen stage. The current test runner requires Bubblewrap, an already
delegated writable cgroup v2 root with memory/pids/cpu controllers, and cgroup.kill.
It refuses when these are unavailable; it never enables controllers or installs
dependencies. Re-stage on Linux to obtain a native-path manifest when necessary.

Test containment: read-only source/system binds, no host home/drives/credentials,
isolated network and PID namespace, dropped capabilities, two 64 MiB temporary
filesystems, 2 GiB aggregate memory/no swap, 64 processes, two CPU cores, bounded
wall time/output, and verified empty cgroup cleanup. Tests are serialized, separate
from model dispatch, through a per-Linux-user test lock across run roots. Cleanup
failure retains its scope observation and prevents another test while any owned
test cgroup remains. Generic POSIX process-group termination alone is never
reported as tree-quiescence evidence. A missing gate dependency fails visibly; there is no network
installation inside the sandbox. This is a local test boundary, not protection
against kernel exploits or a complete multi-host scheduling platform.

Captured stdout/stderr are hard byte-capped. The provider export is polled against
a 16 MiB ceiling and checked after completion; a short overshoot is possible, and
CLI-managed session files are not covered by that cap. Do not claim a complete
provider disk quota. Raw exports remain external because they include CLI context.

## Validation

```text
python -B -m unittest tools.construction_swarm.tests.test_swarm -v
```

Run on native Windows and WSL Linux. The suite distinguishes scheduling fixtures,
actual OS containment tests, and live review evidence. See `VALIDATION.md` for the
observed candidate results and Devin findings. No historical formal-model result
is reinterpreted or marked complete by this tooling.
