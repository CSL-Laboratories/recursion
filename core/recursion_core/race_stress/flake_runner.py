"""Repeat the race_stress suite N times under free-threaded CPython and record flakes (CSL-801).

Each repetition is a fresh child process with its own ``RACE_STRESS_SEED``, so a failure carries the
seed that reproduces it. The runner refuses to start on a GIL build or with the GIL enabled
unless ``--allow-gil`` is passed (a control run on 3.13 is still useful, but it is labelled).

    PYTHON_GIL=0 python3.14t -X gil=0 core/recursion_core/race_stress/flake_runner.py \\
        --runs 50 --out /tmp/flakes.jsonl
    # also repeat extra commands, e.g. a driver script; {seed} is substituted:
        --cmd "recursion_core/race_stress/drivers/breaker_326.py --tree $RACE_STRESS_TREE_326 --seed {seed}"

Each child records per-test status through a ``unittest.TestResult`` (not by parsing ``-v``
text). Writes one JSON line per (run, command) to ``--out`` and a summary to stdout. A test is a
flake when it both passed and failed across runs; a test that failed every run is a steady
failure. Exit status: 0 when nothing failed, 1 otherwise. Paper and mock only: it runs
the local suite and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from collections import defaultdict
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]  # .../core
def runtime() -> dict:
    check = getattr(sys, "_is_gil_enabled", None)
    return {"python": sys.version.split()[0], "executable": sys.executable,
            "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
            "gil_enabled": True if check is None else bool(check())}


_STATUS_RANK = {"skip": 0, "ok": 1, "fail": 2, "error": 3}


def _remember(statuses: dict, test, status: str) -> None:
    """Record ``status`` unless a more severe one is already stored for this test.

    ``addSuccess`` runs for a passing test and must not wipe a subtest failure: unittest
    reports those through ``addSubTest`` and does not call ``addFailure`` for the parent.
    """
    prev = statuses.get(test.id())
    if prev is None or _STATUS_RANK[status] >= _STATUS_RANK[prev]:
        statuses[test.id()] = status


def child(target: str, out_path: str) -> int:
    """Run one unittest target in this process and write ``{test_id: status}`` as JSON.

    Statuses come from a ``TestResult``, not from parsing ``-v`` text, so tests that write to
    stderr mid-line (the driver tests do) are still attributed.
    """
    sys.path.insert(0, str(CORE))
    package = CORE.joinpath(*target.split("."))
    loader = unittest.TestLoader()
    suite = (loader.discover(str(package), top_level_dir=str(CORE)) if package.is_dir()
             else loader.loadTestsFromName(target))
    statuses: dict[str, str] = {}

    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            _remember(statuses, test, "ok")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            _remember(statuses, test, "ok")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            _remember(statuses, test, "fail")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            _remember(statuses, test, "fail")

        def addError(self, test, err):
            super().addError(test, err)
            _remember(statuses, test, "error")

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            _remember(statuses, test, "skip")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                kind = "fail" if issubclass(err[0], test.failureException) else "error"
                _remember(statuses, test, kind)

    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    Path(out_path).write_text(json.dumps(statuses, sort_keys=True), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def _captured(value) -> str:
    """TimeoutExpired carries bytes even when the child was started with ``text=True``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_once(argv: list[str], env: dict, timeout: float) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=CORE, env=env, capture_output=True, text=True, timeout=timeout)
        rc, text = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, text = -999, f"TIMEOUT after {timeout}s (possible deadlock)\n{_captured(exc.stdout)}{_captured(exc.stderr)}"
    return rc, text, round(time.monotonic() - started, 3)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--pattern", default="recursion_core.race_stress.tests",
                    help="unittest target (module or package); '' to skip the unittest suite")
    ap.add_argument("--cmd", action="append", default=[],
                    help="extra script + args (path relative to core/) repeated with the same interpreter; {seed} is substituted")
    ap.add_argument("--iterations", type=int, default=None, help="RACE_STRESS_ITERS for each child")
    ap.add_argument("--timeout", type=float, default=600.0, help="per child, seconds")
    ap.add_argument("--seed", type=int, default=None, help="base seed; run k uses base + k")
    ap.add_argument("--out", default=None, help="JSON lines file (default: stdout summary only)")
    ap.add_argument("--allow-gil", action="store_true", help="permit a GIL build (control run)")
    ap.add_argument("--child", nargs=2, metavar=("TARGET", "OUT"), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        return child(*args.child)

    rt = runtime()
    if (rt["gil_enabled"] or not rt["free_threaded_build"]) and not args.allow_gil:
        print("refusing: run under python3.14t -X gil=0 (or pass --allow-gil for a control run)",
              file=sys.stderr)
        return 2
    base = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2**31 - args.runs)
    interp = [sys.executable] + ([] if rt["gil_enabled"] else ["-X", "gil=0"])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if not rt["gil_enabled"]:
        env["PYTHON_GIL"] = "0"
    if args.iterations is not None:
        env["RACE_STRESS_ITERS"] = str(args.iterations)

    jobs = []
    if args.pattern:
        jobs.append(("unittest", interp + [str(Path(__file__).resolve()), "--child", args.pattern]))
    for cmd in args.cmd:
        jobs.append((cmd, None))

    sink = open(args.out, "a", encoding="utf-8") if args.out else None
    tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    failures: list[dict] = []
    try:
        for k in range(args.runs):
            seed = base + k
            for label, fixed in jobs:
                with tempfile.TemporaryDirectory(prefix="flake-runner-") as tmp:
                    status_file = Path(tmp) / "statuses.json"
                    argv_k = (fixed + [str(status_file)] if fixed
                              else interp + shlex.split(label.replace("{seed}", str(seed))))
                    rc, text, secs = run_once(argv_k, dict(env, RACE_STRESS_SEED=str(seed)), args.timeout)
                    if fixed:
                        results = json.loads(status_file.read_text()) if status_file.is_file() else {}
                    else:
                        results = {label: "ok" if rc == 0 else "fail"}
                if rc != 0 and not any(v in ("fail", "error") for v in results.values()):
                    results[f"{label}#process"] = "error"  # crash, timeout or import error
                for test_id, status in results.items():
                    tally[test_id][status] += 1
                bad = sorted(t for t, s in results.items() if s in ("fail", "error"))
                record = {"run": k, "seed": seed, "job": label, "rc": rc, "seconds": secs,
                          "failed": bad, "runtime": rt}
                if bad:
                    record["tail"] = text[-4000:]
                    failures.append(record)
                if sink:
                    sink.write(json.dumps(record, sort_keys=True) + "\n")
                    sink.flush()
                print(f"run {k + 1}/{args.runs} seed={seed} {label[:60]} rc={rc} {secs}s"
                      + (f" FAILED {bad}" if bad else ""), flush=True)
    finally:
        if sink:
            sink.close()

    flaky = {t: dict(c) for t, c in tally.items() if c.get("ok") and (c.get("fail") or c.get("error"))}
    steady = {t: dict(c) for t, c in tally.items() if not c.get("ok") and (c.get("fail") or c.get("error"))}
    summary = {"runs": args.runs, "base_seed": base, "runtime": rt, "tests": len(tally),
               "flaky": flaky, "steady_failures": steady,
               "repro": [f"{shlex.join(interp)} -B core/recursion_core/race_stress/flake_runner.py --runs 1 --seed {f['seed']}"
                         f" --pattern {args.pattern}" + ("" if args.iterations is None else f" --iterations {args.iterations}")
                         if f["job"] == "unittest" else f"{shlex.join(interp)} {f['job'].replace('{seed}', str(f['seed']))}"
                         for f in failures[:10]]}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
