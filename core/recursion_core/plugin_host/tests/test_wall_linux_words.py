"""The words a person reads follow the cause: a record whose only fault is a widened allow list says what to do about
the list, and does not send the person to turn Landlock on.
"""
from __future__ import annotations

import json
import unittest

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import SCHEMA

WIDENED = "fixtures/good/wall.v1/h21-a-widened-list-is-not-proven-and-says-over-which-list.json"
PROVEN = "fixtures/good/wall.v1/h20-a-kernel-without-rings-is-proven-with-the-ring-not-tried.json"


def _fixture(name: str) -> dict:
    return json.loads((SCHEMA.parent / name).read_text(encoding="utf-8"))


class TheWordsFollowTheCauseTests(unittest.TestCase):

    def test_a_widened_list_is_told_what_to_do_about_the_list_only(self):
        record = _fixture(WIDENED)
        self.assertEqual(record["proven_over"], "widened_list")
        words = wall_linux.why_not_proven(record)
        calls = ", ".join(record["allow_list"]["rows_not_shipped"])
        self.assertIn(wall_linux.WORDS["list_widened"].format(calls=calls), words)
        self.assertNotIn(wall_linux.WORDS["what_to_do"], words, "Landlock is on and held: the list is the cause")

    def test_a_fault_of_the_wall_still_ends_with_the_advice_to_turn_landlock_on(self):
        record = dict(_fixture(PROVEN), proven=False, proven_over=None, landlock_abi=0)
        words = wall_linux.why_not_proven(record)
        self.assertIn(wall_linux.WORDS["no_landlock"], words)
        self.assertEqual(words[-1], wall_linux.WORDS["what_to_do"])


if __name__ == "__main__":
    unittest.main()
