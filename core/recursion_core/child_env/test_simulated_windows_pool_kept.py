"""The other-suites run stays four at a time after master is merged (CSL-238, #520).

Master runs every other core suite from
``test_the_other_core_suites_have_no_error_or_failure_on_simulated_windows``
with a ``ThreadPoolExecutor`` of at most four workers. This draft also splits
that run across part files. The pooled method has to stay: dropping it, or
rewriting the four-worker call, turns this test red. No suite is started here.
"""

from __future__ import annotations

import inspect
import unittest

from recursion_core.child_env.tests import test_simulated_windows as simulated


class SimulatedWindowsPoolKeptTests(unittest.TestCase):
    def test_other_core_suites_still_run_four_at_a_time(self) -> None:
        method = getattr(
            simulated.SimulatedWindowsTests,
            "test_the_other_core_suites_have_no_error_or_failure_on_simulated_windows",
            None,
        )
        self.assertIsNotNone(
            method,
            "the pooled other-suites method is missing",
        )
        source = inspect.getsource(method)
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn("max_workers=min(4, os.cpu_count() or 1)", source)
        self.assertIn("pool.submit(simulate, str(path))", source)
        self.assertIn("futures[path].result()", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
