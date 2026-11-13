"""CSL-801 race and stress tests. Each target runs as a driver subprocess on the same interpreter.

Measured trees are not part of this branch (the code lives on stacked PRs), so each target is
pointed at a checkout by environment variable and skipped when it is unset:

* ``RACE_STRESS_TREE_324``: recursion #324 (daemon registry)
* ``RACE_STRESS_TREE_326``: recursion #326 (routes breaker)
* ``RACE_STRESS_TREE_328``: recursion #328 (plugin_security breaker)

``RACE_STRESS_ITERS`` (default 20) and ``RACE_STRESS_SEED`` (default random, printed) tune the
runs. On a free-threaded build the child runs with ``-X gil=0`` and ``PYTHON_GIL=0``.
The child is started with ``-I`` and does not inherit ``PYTHONPATH``, ``PYTHONHOME``,
``PYTHONSTARTUP``, ``PYTHONUSERBASE``, or ``PYTHONSAFEPATH``. ``PYTHONNOUSERSITE=1``.
The driver directory is put on ``sys.path`` explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import threading
import time
import unittest
from pathlib import Path

DRIVERS = Path(__file__).resolve().parents[1] / "drivers"
sys.path.insert(0, str(DRIVERS))
import _harness as h

from recursion_core.race_stress.pins import require_pinned

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))

# Startup variables that choose import locations. The child gets none of them.
# PYTHONSAFEPATH is dropped with the path-related variables around it.
_PYTHON_IMPORT_ENV = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONSAFEPATH",
    "PYTHONPLATLIBDIR",
)

# -I does not put the script directory on sys.path. Insert the driver directory, then
# run the script so prepare_tree_import can put the measured core/ first.
_DRIVER_BOOTSTRAP = (
    "import sys, runpy\n"
    "drivers, script = sys.argv[1], sys.argv[2]\n"
    "sys.path.insert(0, drivers)\n"
    "sys.argv = [script, *sys.argv[3:]]\n"
    "runpy.run_path(script, run_name='__main__')\n"
)


def _driver_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _PYTHON_IMPORT_ENV}
    env["PYTHONNOUSERSITE"] = "1"
    if FREE_THREADED:
        env["PYTHON_GIL"] = "0"
    return env


def run_driver(script: str, tree: str, *, iters: int | None = None, mutant: bool = False) -> dict:
    argv = [sys.executable, "-I", "-B"] + (["-X", "gil=0"] if FREE_THREADED else [])
    argv += ["-c", _DRIVER_BOOTSTRAP, str(DRIVERS), str(DRIVERS / script), "--tree", tree]
    if iters is not None:
        argv += ["--iters", str(iters)]
    if mutant:
        argv.append("--mutant")
    out = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=1800, env=_driver_env())
    if mutant and out.returncode < 0:
        # An unguarded mutant may take the interpreter down (a signal) before it can report.
        print(f"\n{script} --mutant: killed by signal {-out.returncode}", file=sys.stderr)
        return {"violation_count": 1, "crashed_by_signal": -out.returncode}
    text = out.stdout.strip()
    try:
        result = json.loads(text.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        raise AssertionError(f"{script} exited {out.returncode}: {out.stderr[-2000:]}") from None
    # Violations are a result, not a crash: the driver exits 1 and still prints the JSON line.
    if out.returncode != 0 and not result.get("violation_count"):
        raise AssertionError(
            f"{script} exited {out.returncode} with no violations: {out.stderr[-2000:]}")
    print(f"\n{script}: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


def tree(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise unittest.SkipTest(f"{name} not set; see README.md")
    require_pinned(name, Path(value))  # skips, with a message, on a tree nobody pinned
    return value


class HarnessTests(unittest.TestCase):
    """The harness itself, with no measured tree."""

    def test_hammer_reports_exceptions(self) -> None:
        def boom():
            raise ValueError("boom")
        problems = h.hammer([boom, lambda: None])
        self.assertEqual(len(problems), 1)
        self.assertIn("ValueError: boom", problems[0])

    def test_hammer_reports_a_hang_instead_of_waiting(self) -> None:
        release = threading.Event()
        started = time.monotonic()
        problems = h.hammer([lambda: release.wait(30)], timeout=0.2)
        release.set()
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(any("still running" in p for p in problems))

    def test_stamp_lock_stamps_are_gap_free_under_contention(self) -> None:
        lock = h.StampLock()
        seen: list[int] = []
        guard = threading.Lock()

        def worker():
            for _ in range(500):
                with lock:
                    pass
                stamp = lock.take()
                with guard:
                    seen.append(stamp)

        self.assertEqual(h.hammer([worker] * 8), [])
        self.assertEqual(sorted(seen), list(range(1, 4001)))

    def test_violations_keep_count_beyond_the_sample(self) -> None:
        v = h.Violations()
        for i in range(h.MAX_VIOLATIONS + 5):
            v.add(str(i))
        self.assertEqual(v.count, h.MAX_VIOLATIONS + 5)
        self.assertEqual(len(v.first), h.MAX_VIOLATIONS)

    def test_child_runs_on_the_same_build(self) -> None:
        rt = h.runtime()
        self.assertEqual(rt["free_threaded_build"], FREE_THREADED)
        if FREE_THREADED:
            self.assertFalse(rt["gil_enabled"], "run the suite with -X gil=0")


class PinTests(unittest.TestCase):
    """The tree-HEAD pin itself (t1430u X1b): an unpinned tree is never measured silently."""

    def test_a_tree_that_is_not_a_checkout_is_refused_with_a_message(self) -> None:
        import tempfile
        from recursion_core.race_stress.pins import PINS, pin_status
        with tempfile.TemporaryDirectory() as tmp:
            for env in PINS:
                ok, message = pin_status(env, Path(tmp))
                self.assertFalse(ok)
                self.assertIn(PINS[env]["head"][:8], message)

    def test_wrong_head_is_refused_and_names_both_heads(self) -> None:
        from recursion_core.race_stress.pins import pin_status
        here = Path(__file__).resolve().parents[4]  # this checkout: not any measured head
        ok, message = pin_status("RACE_STRESS_TREE_328", here)
        if ok:
            self.skipTest("this checkout happens to carry the pinned #328 files")
        self.assertIn("54a2d771", message)
        self.assertIn("RACE_STRESS_UNPINNED=1", message)

    def test_set_trees_match_their_pins(self) -> None:
        from recursion_core.race_stress.pins import PINS, pin_status
        checked = 0
        for env in PINS:
            value = os.environ.get(env)
            if value:
                ok, message = pin_status(env, Path(value))
                self.assertTrue(ok or os.environ.get("RACE_STRESS_UNPINNED") == "1", message)
                checked += 1
        if not checked:
            self.skipTest("no RACE_STRESS_TREE_* set")

class DaemonRegistry324(unittest.TestCase):
    def test_invariants_hold_under_concurrency(self) -> None:
        result = run_driver("daemon_324.py", tree("RACE_STRESS_TREE_324"))
        self.assertEqual(result["violation_count"], 0, result["violations"])

    def test_sensitivity_unlocked_mutant_is_caught(self) -> None:
        result = run_driver("daemon_324.py", tree("RACE_STRESS_TREE_324"), iters=5, mutant=True)
        self.assertGreater(result["violation_count"], 0)


class RoutesBreaker326(unittest.TestCase):
    def test_linearizable_and_one_probe_per_key(self) -> None:
        result = run_driver("breaker_326.py", tree("RACE_STRESS_TREE_326"))
        self.assertEqual(result["violation_count"], 0, result["violations"])
        self.assertGreater(result["counters"]["probes"], 0)

    def test_sensitivity_unlocked_mutant_is_caught(self) -> None:
        result = run_driver("breaker_326.py", tree("RACE_STRESS_TREE_326"), iters=5, mutant=True)
        self.assertGreater(result["violation_count"], 0)


class PluginSecurityBreaker328(unittest.TestCase):
    def test_linearizable(self) -> None:
        result = run_driver("breaker_328.py", tree("RACE_STRESS_TREE_328"))
        self.assertEqual(result["violation_count"], 0, result["violations"])

    def test_sensitivity_unlocked_mutant_is_caught(self) -> None:
        result = run_driver("breaker_328.py", tree("RACE_STRESS_TREE_328"), iters=5, mutant=True)
        self.assertGreater(result["violation_count"], 0)

    # CSL-804: a probe older than a trip cleared it at #328 0264628b (these two tests were
    # expectedFailure). #328 afe38c1f drops the probe lease on a new trip; both now pass.
    def test_probe_older_than_a_trip_does_not_clear_it(self) -> None:
        result = run_driver("breaker_328.py", tree("RACE_STRESS_TREE_328"), iters=3)
        self.assertEqual(result["violation_count"], 0, result["violations"])
        self.assertGreater(result["counters"]["stale_probe_leases"], 0, result["counters"])
        self.assertEqual(result["counters"]["lost_trips"], 0, result["counters"])

    def test_minimal_repro_probe_older_than_a_trip(self) -> None:
        argv = [sys.executable, "-B", str(DRIVERS / "repro_328_stale_probe.py"), "--tree",
                tree("RACE_STRESS_TREE_328")]
        out = json.loads(subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True).stdout)
        self.assertFalse(out["erased"], out)
        self.assertFalse(out["reset_returned"], out)
        self.assertTrue(out["tripped_after"], out)
        self.assertEqual(out["check_after"], "connection_tripped", out)

if __name__ == "__main__":
    unittest.main()
