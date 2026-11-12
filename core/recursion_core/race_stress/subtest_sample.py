"""Not collected by discovery. The flake runner loads it by name to record a failed subtest."""

from __future__ import annotations

import unittest


class Sample(unittest.TestCase):
    def test_one(self) -> None:
        with self.subTest(i=1):
            self.fail("boom")
        with self.subTest(i=2):
            pass

    def test_ok(self) -> None:
        self.assertTrue(True)
