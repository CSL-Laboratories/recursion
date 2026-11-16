"""Part 2 of the simulated-Windows run of the other core suites (CSL-785, CSL-238).

The helpers, the partition (OTHER_SUITE_PARTS) and the test that the parts together hold every suite
exactly once live in test_simulated_windows.py. One gate file per part keeps each under the gate's
180 s per-file limit; inside a part the suites still run one after another.
"""

from __future__ import annotations

import os
import unittest

from recursion_core.child_env.tests import test_simulated_windows as simulated

PART = 2


@unittest.skipIf(os.name == "nt", "simulation for POSIX hosts; a Windows host runs the suites themselves")
class SimulatedWindowsOtherSuitesPart2Tests(unittest.TestCase):
    maxDiff = None

    def test_the_other_core_suites_have_no_error_or_failure_on_simulated_windows(self) -> None:
        simulated.assert_other_suites_clean(self, PART)


if __name__ == "__main__":
    unittest.main(verbosity=2)
