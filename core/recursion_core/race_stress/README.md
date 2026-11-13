# race_stress: 3.14t race and stress tests (CSL-801)

Concurrency stress for core code that lives on stacked PRs. Mock only: the systemd manager
and the vendor clock are in-memory fakes; nothing launches and no provider or network is
touched.

| Driver | Measured code | Pinned head (`pins.py`) |
| --- | --- | --- |
| `drivers/daemon_324.py` | recursion #324 `core/recursion_core/daemon/` (Registry, Daemon, UserLock) | `7c729db9` |
| `drivers/breaker_326.py` | recursion #326 `core/recursion_core/routes/breaker.py` | `3147f979` |
| `drivers/breaker_328.py` | recursion #328 `core/recursion_core/plugin_security/breaker.py` (SQLite store, probe leases) | `54a2d771` |

`pins.py` holds each pinned head and the git object ids of the measured paths. A test uses a
`RACE_STRESS_TREE_*` checkout only when every measured path's committed object is that id and
every tracked file under it has those bytes in the worktree. Those bytes are what the driver
imports, so a skip-worktree or assume-unchanged edit is refused and a staged blob whose worktree
was restored is not. Untracked and ignored files are refused when Python could import them in
place of that source: a bytecode cache (a timestamp or unchecked-hash `.pyc` runs even under
`-B`), an extension module, a package directory sitting on a module, any other importable file
under a directory pin, and an untracked top-level name on the `core/` path the driver inserts.
The same shadow check covers every hashed module, including one imported from outside the
import-search directories: a same-stem package directory, a same-stem extension, and that
directory's `__pycache__`. A package shadow is `__init__` plus one suffix from
`importlib.machinery.all_suffixes()`; `__init__extra.py` is not a package init. Any
untracked `.pyc` under `core/` is refused.
A symlink anywhere under `core/` is refused, including one whose link text matches `HEAD`
(the pinned heads have none). A dangling Emacs lock named `.#*` is left alone. A nested
`.git` file or directory under `core/` is refused. An untracked directory in `core/` or in
a measured package directory is refused when it contains a file, except an empty directory
that lists cleanly, a dot-directory other than `.git`, and a `*.egg-info` directory that
holds no `.py`, `.pyc`, `.so`, or `.pth`. A directory that cannot be listed (mode `0111`
hides names from `readdir`, `os.walk`, and `git ls-files --others`) fails closed, and each
hashed source module's `importlib.util.cache_from_source` path is statted directly so an
untracked unchecked-hash `.pyc` there is refused. Python opens that path by name under
`-B`. Each tracked file's parents must be real directories (`lstat`, not a followed link).
A file pin hashes every ancestor package `__init__.py` and the tracked modules those files
import. A directory pin seeds that walk with every tracked `.py` in the directory,
including its own `__init__.py`, then hashes the transitive imports and applies the same
shadow checks. A namespace portion (no `__init__` file) is refused when an untracked
sibling of the same stem has any loader suffix, or when an untracked `__init__` plus any
loader suffix sits inside it. A sourceless `.pyc` the import resolves to is hashed against
`HEAD` and not executed; its code object is read and `IMPORT_NAME` targets join the closure,
and a dynamic import in that code is refused. An extension file is hashed the same way and
its imports are not walked. A changed, missing, or untracked one is refused. A tracked
`.pyc` beside a same-stem `.py` is not the loaded module and stays allowed, for a file pin
and a directory pin. A hashed source module that calls `importlib.import_module`,
`importlib.__import__`, `__import__`, `importlib.util.spec_from_file_location`, `exec` or
`compile` adds a string-literal target to the closure and refuses any other argument.
`from X import *` follows a literal `__all__` and refuses a non-literal one. An assignment
or method call that mutates `__path__`, `sys.path`, `sys.meta_path` or `sys.path_hooks`
is refused. On the pinned heads that refuses `7c729db9` (`daemon/__main__.py` calls
`importlib.import_module` with a non-literal name, and `daemon/tests/crash_child.py`
inserts on `sys.path`; the driver imports `recursion_core.daemon` and does not run those
files, but the directory pin hashes every `.py`) and `54a2d771` (`plugin_security/__init__.py`
imports `loader.py`, which calls `compile` and `exec` with non-literals). `3147f979` still
pins. A file such as `scratch.txt` that cannot be a module and does not create a
directory is left alone, and so is `helper.py` beside a file pin or outside a directory pin.

Not checked, on purpose: `sitecustomize` reached through `PYTHONPATH` when the process is
not isolated (`-B` does not skip it); the bytes of `/usr/bin/git` and the `PATH` fallback
when that binary is missing; `GIT_EXEC_PATH`; a `__spec__` whose paths still name the
pinned file, which `verify_loaded` accepts; launching a driver as a plain script instead
of the isolated runner; imports reached through `getattr` or another alias of those
functions; imports inside an extension module; a string-literal
`spec_from_file_location` path that is not the hashed file.
The driver directory is appended after the standard library, and a stdlib module loaded
from `drivers/` is refused. `git` is `/usr/bin/git` when that binary exists, not whatever
`PATH` names, and `git` variables that select another repository (`GIT_DIR` and the rest)
are ignored. Any other tree
skips with a message naming both heads; `RACE_STRESS_UNPINNED=1` measures it anyway. Earlier
heads of this PR measured #324 `55a31508`/`9aa03185`, #326 `82247cbc` and #328 `0264628b`.

Each driver prints one JSON line with the tree's git HEAD, the measured file's SHA-256, the
interpreter (free-threaded build, GIL state), the seed, counters and any violations, and exits
1 when `violation_count` is not zero. It runs as its own script so it can put the measured
tree first on `sys.path` without clashing with this checkout's `recursion_core`.

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

Status at the pinned heads (Nov 9, t1430u): the two #328 findings and the #326 finding
below are **fixed**. Their tests are now plain tests, not `expectedFailure`.

* **#328 breaker: a probe older than a trip clears it (CSL-804). Fixed at #328 `afe38c1f`.**
  At `0264628b`, `reset_after_probe(connection, True)` took no probe identity, lease or trip
  generation, so a probe that ended before a newer typed limit still erased it (about 190k
  lost trips per 1000 iterations on both builds). Now `begin_probe` hands out one lease per
  tripped connection, `report` drops it on every new trip, and `reset_after_probe(connection,
  lease, ok)` clears only for that live lease. `drivers/repro_328_stale_probe.py` (rewritten
  for the lease API) and `breaker_328.py`'s `lost_trips` both show 0. With the lease drop
  removed in a local mutant of `afe38c1f`, the repro, the stress test and
  `test_lease_dropped_by_a_newer_trip_cannot_clear_it` all fail again.
* **#328 breaker: a reset with no leased probe clears a trip. Fixed at `afe38c1f`.** No lease,
  a made-up lease or a non-string lease now leaves the trip in place
  (`test_reset_without_a_leased_probe_does_not_clear_a_trip`).
* **#326 breaker: a model-scoped limit from a connection probe fenced every model. Fixed at
  #326 `09d0c36f`.** `_open` now releases the probe on whichever key held it. This is pinned by
  `test_model_limit_from_a_connection_probe_leaves_other_models_available`, which was an
  unexpected success at the new head and is now a plain test. `breaker_326.py`'s
  one-probe-per-key check models that release, so an opened result ends the reporting
  probe's lease. Without that modelling, the driver reported 9 false "second live probe"
  violations at `09d0c36f`.
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
| `test_interleave_breaker.py` | #326 routes breaker | half-open stampede at the exact cooldown end; lease handover stampede plus a late report from the expired probe; all 6 orders of reset / new limit / stale probe; a typed limit landing mid-probe (the #328 interleaving: #326 keeps the key open); a model-scoped limit from a connection probe leaves other models available (fixed at `09d0c36f`); untyped storms; randomized mix with a lock-held monitor |
| | #328 plugin breaker | concurrent `bind_run` of one run; fault and trip monotonic for every later `check`; report / leased-probe-reset flapping keeps `tripped` and `check` consistent; a reset with no lease, a made-up lease or a non-string lease is refused; a lease dropped by a newer trip cannot clear it (CSL-804); one live lease per connection under a stampede, and an expired lease cannot reset |

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
