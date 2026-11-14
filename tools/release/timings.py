#!/usr/bin/env python3
"""Per-test wall times of one test file, slowest first.

    python -X utf8 -B tools/release/timings.py tools/test_claude_fixer_publish.py [--top N]

Loads the file by path, as tools/ci_gate.py's unittest runner does, runs it with a result
that times each test, and prints the slowest tests and the total. Exits 1 if a test failed.
Lane A7 found tools/test_claude_fixer_publish.py timing out at the gate's 180 s limit on
hosted macOS, whose logs show only dots for that file; running this there names the tests
that take the time. Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.times: list[tuple[float, str]] = []
        self._started = 0.0

    def startTest(self, test) -> None:  # noqa: N802 (unittest's name)
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test) -> None:  # noqa: N802
        super().stopTest(test)
        self.times.append((time.perf_counter() - self._started, test.id()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="test file, relative to the current directory")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()
    path = Path(args.path)
    sys.path.insert(0, str(path.resolve().parent))
    spec = importlib.util.spec_from_file_location("timed_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    stream = io.StringIO()
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=TimedResult).run(suite)
    total = time.perf_counter() - started
    times = sorted(result.times, reverse=True)
    print(f"{path}: {result.testsRun} tests, {total:.2f} s, failures={len(result.failures)} "
          f"errors={len(result.errors)} skipped={len(result.skipped)}; python {sys.version.split()[0]}")
    for seconds, test_id in times[:args.top]:
        print(f"{seconds:9.3f} s  {test_id.removeprefix('timed_test_module.')}")
    covered = sum(seconds for seconds, _ in times[:args.top])
    print(f"top {min(args.top, len(times))}: {covered:.2f} s of {sum(s for s, _ in times):.2f} s in tests")
    if not result.wasSuccessful():
        sys.stderr.write(stream.getvalue())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
