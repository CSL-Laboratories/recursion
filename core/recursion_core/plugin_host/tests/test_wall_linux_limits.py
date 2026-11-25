"""The record names what 'proven' is not true over: the one wait that is not private the filter lets through, with its
forms, and what another process can still learn of the walled one, in the record's own fields and in the words a
person reads beside the word proven.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own.
"""
from __future__ import annotations

import unittest

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_family import run_child

LIMITS_CHILD = r'''
import json, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
w.apply_wall(spec["paths"], keep_descriptors=(1, 2))
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
print(json.dumps({"record": record, "words": w.why_not_proven(record), "limits": w.what_proven_does_not_cover(record)}))
'''


def _shared_waits() -> list:
    """The futex forms that are not private on the shipped list, read from the list itself."""
    rows = {row["call"]: row for row in wall_linux.load_allow_list()["rows"]}
    return sorted(operation for operation in rows["futex"]["operations"] if not operation & 128)


class TheLimitsReadAsWordsTests(unittest.TestCase):

    def test_a_record_with_its_limits_reads_with_a_sentence_for_each(self):
        shared = _shared_waits()
        self.assertTrue(shared, "the shipped list lets one wait that is not private through")
        record = {"proven": True, "shared_waits_allowed": shared, "limits_of_proven": wall_linux._limits_of_proven(shared)}
        self.assertEqual(set(record["limits_of_proven"]), set(wall_linux.LIMITS_OF_PROVEN))
        lines = wall_linux.what_proven_does_not_cover(record)
        self.assertEqual(lines[0], wall_linux.WORDS["limits_intro"])
        self.assertEqual(lines[-1], wall_linux.WORDS["limits_what_to_do"])
        self.assertEqual(len(lines), len(wall_linux.LIMITS_OF_PROVEN) + 2)
        for operation in shared:
            self.assertIn(f"(operation {operation})", lines[1])
            self.assertIn(f"(operation {operation})", record["limits_of_proven"]["shared_futex_wait"])

    def test_a_list_with_no_shared_wait_names_none(self):
        limits = wall_linux._limits_of_proven([])
        self.assertNotIn("shared_futex_wait", limits)
        self.assertEqual(set(limits), set(wall_linux.LIMITS_OF_PROVEN) - {"shared_futex_wait"})

    def test_a_limit_this_module_does_not_know_is_named_not_passed_over(self):
        lines = wall_linux.what_proven_does_not_cover({"proven": True, "limits_of_proven": {"made_up_limit": "x"}})
        self.assertIn("made_up_limit", lines[1])
        self.assertEqual(wall_linux.what_proven_does_not_cover({"proven": True}), [])


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class AProvenRecordNamesItsLimitsTests(unittest.TestCase):

    def test_the_record_names_the_shared_wait_and_what_another_process_can_learn(self):
        result, _ = run_child(LIMITS_CHILD, "limits")
        record, shared = result["record"], _shared_waits()
        self.assertIs(record["proven"], True, result["words"])
        self.assertEqual(record["shared_waits_allowed"], shared, "the record names the shared wait in force by its form")
        self.assertEqual(set(record["limits_of_proven"]), set(wall_linux.LIMITS_OF_PROVEN))
        for operation in shared:
            self.assertIn(f"(operation {operation})", record["limits_of_proven"]["shared_futex_wait"])
        self.assertIn("whether this process waits there", record["limits_of_proven"]["shared_futex_wait"])
        self.assertIn("command line", record["limits_of_proven"]["what_the_process_shows"])
        self.assertEqual(result["limits"], wall_linux.what_proven_does_not_cover(record))
        self.assertEqual(len(result["limits"]), len(wall_linux.LIMITS_OF_PROVEN) + 2, "a sentence for every limit")


CHANNELS = ("shared_futex_wait", "process_times", "page_cache_residency", "what_the_process_shows")


class EachChannelStandsInTheRecordTests(unittest.TestCase):

    def test_each_channel_to_another_process_is_named_with_its_reason_and_its_words(self):
        shared = _shared_waits()
        limits = wall_linux._limits_of_proven(shared)
        self.assertEqual(tuple(limits), CHANNELS)
        for name in CHANNELS[:3]:
            self.assertIn("a measurement", limits[name], name)
            self.assertIn("channel", limits[name], name)
        self.assertIn("both ways", limits["shared_futex_wait"])
        self.assertIn("end this process's waits", limits["shared_futex_wait"])
        lines = wall_linux.what_proven_does_not_cover({"proven": True, "shared_waits_allowed": shared,
                                                       "limits_of_proven": limits})
        self.assertIn("does NOT stop a plugin from signalling a cooperating process", lines[0])
        self.assertEqual(lines[1:-1], [wall_linux.WORDS["limit_shared_futex_wait"].format(
            forms=wall_linux._futex_forms(shared))] + [wall_linux.WORDS["limit_" + name] for name in CHANNELS[1:]])

    def test_the_shipped_list_says_the_record_names_the_shared_wait(self):
        with open(wall_linux.ALLOW_LIST_FILE, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("which the record states", text)
        self.assertIn("which the record names (limits_of_proven)", text)


if __name__ == "__main__":
    unittest.main()
