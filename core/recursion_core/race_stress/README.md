# race_stress: 3.14t race and stress tests (CSL-801)

Concurrency stress for core code that lives on stacked PRs. Mock only: the systemd manager
and the vendor clock are in-memory fakes; nothing launches and no provider or network is
touched.

| Driver | Measured code | Pinned head |
| --- | --- | --- |
| `drivers/daemon_324.py` | recursion #324 `core/recursion_core/daemon/__init__.py` (Registry, Daemon, UserLock) | `55a31508` |
| `drivers/breaker_326.py` | recursion #326 `core/recursion_core/routes/breaker.py` | `82247cbc` |
| `drivers/breaker_328.py` | recursion #328 `core/recursion_core/plugin_security/breaker.py` | `0264628b` |

Each driver prints one JSON line with the tree's git HEAD, the measured file's SHA-256, the
interpreter (free-threaded build, GIL state), the seed, counters and any violations. It runs as
its own script so it can put the measured tree first on `sys.path` without clashing with this
checkout's `recursion_core`.

## How races are detected

* **Linearizability (both breakers).** The target's lock is swapped for `StampLock`, which
  stamps each acquisition. Every operation takes that lock once, so sorting the concurrent
  history by stamp and replaying it on a fresh object must reproduce every result and the
  final state. The #326 replay also feeds each call the fake-clock values it read.
* **Invariants under load (daemon).** Snapshots taken while writers run plus final checks:
  intent before effect, one launch per run id, B14 (a stopped run never reads `running`),
  sticky terminal states, close racing in-flight calls, `UserLock` exclusivity.
* **Sensitivity.** `--mutant` breaks each target's guard (its lock excludes nothing). The
  tests require the mutant to be caught, so a clean run means something.

There is no ThreadSanitizer build of CPython 3.14t on the measuring host; these are
randomized stress runs, not a proof.

## Run

```
export RACE_STRESS_TREE_324=<checkout of recursion #324>  RACE_STRESS_TREE_326=<#326>
export RACE_STRESS_TREE_328=<#328>
PYTHONPATH=core python3.13 -B -m unittest discover -s core/recursion_core/race_stress/tests -t core -v
PYTHONPATH=core PYTHON_GIL=0 python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/race_stress/tests -t core -v
```

Unset tree variables skip their targets; the harness tests always run. `RACE_STRESS_ITERS`
(default 20) and `RACE_STRESS_SEED` tune the runs. A long run of one driver:

```
PYTHON_GIL=0 python3.14t -X gil=0 -B core/recursion_core/race_stress/drivers/breaker_326.py \
  --tree <checkout> --iters 1000 --seed 801001
```

## Findings

* **#328 breaker: a probe older than a trip clears it.** `reset_after_probe(connection, True)`
  takes no probe identity, lease or trip generation, so a probe that ended before a newer
  typed limit still erases that limit. `drivers/repro_328_stale_probe.py` is the minimal
  repro; `breaker_328.py` counts it as `lost_trips`. Reproduces on CPython 3.13.5 and on
  3.14t: it is a logic race, not a free-threading one. The two tests that state the wanted
  behaviour are `expectedFailure` until #328 changes. The #326 breaker does not have it: its
  probe tokens are dropped when a key reopens.
* **#324 registry lock is load-bearing for memory safety.** With the Registry and Daemon
  locks removed (`--mutant`), the shared `sqlite3` connection crashes the interpreter with
  SIGSEGV on both builds. The real code holds the lock on every connection use, and no
  crash or violation was seen without the mutant.

## Forced interleavings and the flake runner

`tests/test_interleave_*.py` complement the randomized drivers. Each test holds one window open
on purpose (a `Gate` inside a fake's method, or a barrier at an exact fake-clock instant) and
checks the invariant across it. They load the measured module in-process from the same
`RACE_STRESS_TREE_*` checkouts with the head's package `__init__` skipped, so the #324, #326 and
#328 copies of `recursion_core` coexist in one process. A test whose tree is unset is skipped.

| File | Target | Windows forced |
| --- | --- | --- |
| `test_interleave_registry.py` | #324 registry | Stop committed while a reconcile holds a stale `Run` and is paused in `observe` (B14); `Daemon.stop` behind a paused sweep; random writers against terminal stickiness |
| `test_interleave_breaker.py` | #326 routes breaker | half-open stampede at the exact cooldown end; lease handover stampede plus a late report from the expired probe; all 6 orders of reset / new limit / stale probe; a typed limit landing mid-probe (the #328 interleaving: #326 keeps the key open); untyped storms; randomized mix with a lock-held monitor |
| | #328 plugin breaker | concurrent `bind_run` of one run; fault and trip monotonic for every later `check`; report/reset flapping keeps `tripped` and `check` consistent |

Sensitivity (local, not committed): removing B14's `CASE` in `Registry.state`, or the lock in
#326 `admit`, makes these tests fail (B14 broken; 8 probes leased instead of 1).

`flake_runner.py` repeats the suite in fresh child processes, one seed per run, and reports
flaky tests (passed and failed across runs) and steady failures, each with a replay command:

```
PYTHON_GIL=0 python3.14t -X gil=0 -B core/recursion_core/race_stress/flake_runner.py \
  --runs 100 --iterations 20 --out /tmp/flakes.jsonl
```

It refuses a GIL build or an enabled GIL unless `--allow-gil` (a labelled control run).
`--cmd` repeats extra scripts with the same interpreter (paths relative to `core/`, `{seed}`
substituted), for example `--cmd "recursion_core/race_stress/drivers/breaker_326.py --tree
$RACE_STRESS_TREE_326 --iters 200 --seed {seed}"`. A driver exiting non-zero counts as a failure.
