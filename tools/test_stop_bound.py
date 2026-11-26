#!/usr/bin/env python3
"""The performance gate's Stop bound. The workload names it; the gate enforces it."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import perf_ci  # noqa: E402


class StopBoundTest(unittest.TestCase):
    def test_workload_carries_the_stop_bound(self) -> None:
        payload = json.loads((ROOT / "tools" / "perf_workload.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("stop_bound_s"), 0.250)

    def test_a_stop_median_over_the_bound_fails_and_one_under_it_does_not(self) -> None:
        over = {"benchmarks": [{"name": "stop_decision", "median_s": 0.300}]}
        under = {"benchmarks": [{"name": "stop_decision", "median_s": 0.001}]}
        self.assertIsNotNone(perf_ci.stop_bound_failure(over))
        self.assertIsNone(perf_ci.stop_bound_failure(under))
        self.assertIsNotNone(perf_ci.stop_bound_failure({"benchmarks": []}))


if __name__ == "__main__":
    unittest.main()
