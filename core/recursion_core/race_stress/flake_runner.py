"""Repeat the race_stress suite N times under free-threaded CPython and record flakes (CSL-801).

Each repetition is a fresh child process with its own ``RACE_STRESS_SEED``, so a failure carries the
seed that reproduces it. The runner refuses to start on a GIL build or with the GIL enabled
unless ``--allow-gil`` is passed (a control run on 3.13 is still useful, but it is labelled).

    PYTHON_GIL=0 python3.14t -X gil=0 core/recursion_core/race_stress/flake_runner.py \\
        --runs 50 --out /tmp/flakes.jsonl
    # also repeat extra commands, e.g. a driver script; {seed} is substituted:
        --cmd "recursion_core/race_stress/drivers/breaker_326.py --tree $RACE_STRESS_TREE_326 --seed {seed}"

Writes one JSON line per (run, command) to ``--out`` and a summary to stdout. A test is a
flake when it both passed and failed across runs; a test that failed every run is a steady
failure. Exit status: 0 when nothing failed, 1 otherwise. Paper and mock only: it runs
the local suite and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import subprocess
import sys
import sysconfig
import time
from collections import defaultdict
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]  # .../core
RESULT = re.compile(r"^(test\w+) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure"
                    r"|unexpected success)$", re.M)


def runtime() -> dict:
    check = getattr(sys, "_is_gil_enabled", None)
    return {"python": sys.version.split()[0], "executable": sys.executable,
            "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
            "gil_enabled": True if check is None else bool(check())}


def parse_unittest(output: str) -> dict[str, str]:
    """Map ``module.Class.test`` to ok / fail / error / skip from ``unittest -v`` output."""
    out = {}
    for name, where, status in RESULT.findall(output):
        test_id = where if where.endswith("." + name) else f"{where}.{name}"
        out[test_id] = {"ok": "ok", "expected failure": "ok", "FAIL": "fail", "ERROR": "error",
                        "unexpected success": "fail"}.get(status, "skip")
    return out


def run_once(argv: list[str], env: dict, timeout: float) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=CORE, env=env, capture_output=True, text=True, timeout=timeout)
        rc, text = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, text = -999, f"TIMEOUT after {timeout}s (possible deadlock)\n{exc.stdout or ''}{exc.stderr or ''}"
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
    args = ap.parse_args(argv)

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
        package = CORE.joinpath(*args.pattern.split("."))
        target = (["discover", "-v", "-s", str(package), "-t", str(CORE)] if package.is_dir()
                  else ["-v", args.pattern])
        jobs.append(("unittest", interp + ["-m", "unittest"] + target))
    for cmd in args.cmd:
        jobs.append((cmd, None))

    sink = open(args.out, "a", encoding="utf-8") if args.out else None
    tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    failures: list[dict] = []
    try:
        for k in range(args.runs):
            seed = base + k
            for label, fixed in jobs:
                child = fixed or interp + shlex.split(label.replace("{seed}", str(seed)))
                rc, text, secs = run_once(child, dict(env, RACE_STRESS_SEED=str(seed)), args.timeout)
                results = parse_unittest(text) if fixed else {label: "ok" if rc == 0 else "fail"}
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
               "repro": [f"RACE_STRESS_SEED={f['seed']} {shlex.join(interp)} -m unittest {shlex.join(jobs[0][1][len(interp) + 2:])}"
                         if f["job"] == "unittest" else f"{shlex.join(interp)} {f['job'].replace('{seed}', str(f['seed']))}"
                         for f in failures[:10]]}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
