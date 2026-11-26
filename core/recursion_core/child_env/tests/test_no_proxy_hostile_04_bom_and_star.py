"""One hostile NO_PROXY reading shape, timed alone so one gate file cannot charge the other shapes.

The retry is the same as ReadingTimeTests._timed: a busy host may measure again, twice at most.
"""

from __future__ import annotations

import time
import unittest

from recursion_core.child_env import policy
from recursion_core.child_env.tests import test_no_proxy_readings as readings

LABEL = 'BOM and star'
SECONDS = readings.SECONDS
GROWTH = readings.GROWTH
GROWTH_FLOOR_S = readings.GROWTH_FLOOR_S


def _make(n: int) -> str:
    return "\ufeff*a" * (n // 3) + "q"


class HostileShapeTests(unittest.TestCase):
    LONG_HOST = "a" * 242 + ".example"

    def _timed(self, call):
        for _ in range(3):  # a slow run (a busy host) is retried twice at most
            started = time.perf_counter()
            result = call()
            elapsed = time.perf_counter() - started
            if elapsed < SECONDS:
                break
        return result, elapsed

    def _decide(self, value: str):
        lists = [("NO_PROXY", value)]
        refused = policy._no_proxy_bypass(lists, policy.ENGINE_PROFILES["claude_code"].hosts + (self.LONG_HOST,))
        return refused if refused is not None else policy._no_proxy_route_bypass(value, self.LONG_HOST)

    def test_hostile_value_is_decided_in_bounded_time(self) -> None:
        # Timed at n and at n / 2. Growth is the check; SECONDS is only a backstop against blow-ups.
        unit = 100_000
        value, half = _make(unit), _make(unit // 2)
        with self.subTest(label=LABEL, length=len(value)):
            for _ in range(3):  # a noisy pair is measured again, twice at most
                _, elapsed = self._timed(lambda: self._decide(value))
                _, half_elapsed = self._timed(lambda: self._decide(half))
                if elapsed < GROWTH * max(half_elapsed, GROWTH_FLOOR_S):
                    break
            self.assertLess(elapsed, SECONDS)
            self.assertLess(elapsed, GROWTH * max(half_elapsed, GROWTH_FLOOR_S))


if __name__ == "__main__":
    unittest.main()
