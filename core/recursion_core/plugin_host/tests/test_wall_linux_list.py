"""The allow list in force is known by its rows: the shipped list is the one whose rows equal the rows pinned in the
wall's own code, and a list changed beside the wall, in place or given by path, is not the shipped list.

Every list a test changes is a copy of the shipped one in a temporary folder of the test's own; where a test needs
the list beside the wall changed, the module's ALLOW_LIST_FILE points at its copy for that test only (in a walled
CHILD process for the record's tests, with a write folder, a canary and a stand-in daemon of the test's own).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_family import run_child
from recursion_core.plugin_host.tests.test_wall_linux_proof import _schema_errors

PLANTED = {"call": "kcmp", "x86_64": 312, "aarch64": 272, "reason": "planted by the test: a row the shipped list does not hold",
           "recorded": [], "beyond_recording": "planted by the test"}


def _digests(data) -> dict:
    """The digest of each row, computed here apart from the wall's code: the row's JSON with its keys sorted, no
    spaces and ASCII only (prctl's row with the list's prctl options in it), SHA-256, its first 32 hex digits."""
    out = {}
    for row in data["rows"]:
        body = (dict(row, prctl_options=data.get("prctl_options", [])) if row["call"] == "prctl"
                else dict(row, futex_operations=data.get("futex_operations", [])) if row["call"] == "futex" else row)
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        out[row["call"]] = hashlib.sha256(text.encode("ascii")).hexdigest()[:32]
    return out


def _list_digest(digests: dict) -> str:
    text = "".join(f"{call}={digest}\n" for call, digest in sorted(digests.items()))
    return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


class PinnedListTests(unittest.TestCase):
    def setUp(self):
        with open(wall_linux.ALLOW_LIST_FILE, encoding="utf-8") as handle:
            self.shipped = json.load(handle)
        self.folder = tempfile.mkdtemp(prefix="wall-list-")
        self.addCleanup(shutil.rmtree, self.folder)

    def write(self, data, name: str = "wall_allow_linux.json") -> str:
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
        return path

    def beside_the_wall(self, data) -> None:
        """``data`` as the list beside the wall, for this test only."""
        patch = mock.patch.object(wall_linux, "ALLOW_LIST_FILE", self.write(data))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_pinned_rows_equal_the_shipped_file(self):
        # a change of the shipped list changes the pin in the same commit, where a diff shows it
        self.assertEqual(wall_linux._SHIPPED_ALLOW_ROWS, _digests(self.shipped))
        self.assertEqual(list(wall_linux._SHIPPED_ALLOW_ROWS), [row["call"] for row in self.shipped["rows"]])

    def test_the_shipped_file_is_the_shipped_list_with_its_digest(self):
        allow = wall_linux.load_allow_list()
        self.assertTrue(allow["shipped"])
        self.assertEqual((allow["added"], allow["removed"]), ([], []))
        self.assertEqual(allow["digest"], _list_digest(_digests(self.shipped)))
        self.assertEqual(len(allow["rows"]), len(self.shipped["rows"]))

    def test_a_row_added_in_place_is_not_the_shipped_list(self):
        widened = dict(self.shipped, rows=self.shipped["rows"] + [PLANTED])
        self.beside_the_wall(widened)
        allow = wall_linux.load_allow_list()
        self.assertFalse(allow["shipped"])
        self.assertEqual((allow["added"], allow["removed"]), (["kcmp"], []))
        self.assertEqual(allow["digest"], _list_digest(_digests(widened)))
        self.assertNotEqual(allow["digest"], _list_digest(_digests(self.shipped)))

    def test_a_row_changed_in_place_is_not_the_shipped_list(self):
        rows = [dict(row, reason=row["reason"] + " (changed in place)") if row["call"] == "read" else row
                for row in self.shipped["rows"]]
        self.beside_the_wall(dict(self.shipped, rows=rows))
        allow = wall_linux.load_allow_list()
        self.assertFalse(allow["shipped"])
        self.assertEqual((allow["added"], allow["removed"]), (["read"], []))

    def test_a_prctl_option_changed_in_place_is_not_the_shipped_list(self):
        options = [dict(entry, reason=entry["reason"] + " (changed in place)") for entry in self.shipped["prctl_options"]]
        self.beside_the_wall(dict(self.shipped, prctl_options=options))
        allow = wall_linux.load_allow_list()
        self.assertFalse(allow["shipped"])
        self.assertEqual(allow["added"], ["prctl"])

    def test_a_row_taken_out_in_place_is_named_removed(self):
        self.beside_the_wall(dict(self.shipped, rows=[row for row in self.shipped["rows"] if row["call"] != "mremap"]))
        allow = wall_linux.load_allow_list()
        self.assertFalse(allow["shipped"])
        self.assertEqual((allow["added"], allow["removed"]), ([], ["mremap"]))

    def test_a_list_given_by_path_is_compared_with_the_pin(self):
        given = self.write(dict(self.shipped, rows=self.shipped["rows"] + [PLANTED]), "given.json")
        allow = wall_linux.load_allow_list(given)
        self.assertFalse(allow["shipped"])
        self.assertEqual((allow["file"], allow["added"], allow["removed"]), (given, ["kcmp"], []))


LIST_CHILD = r'''
import json, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
if spec.get("beside"):
    w.ALLOW_LIST_FILE = spec["beside"]  # the list beside the wall, changed in place (a copy of the test's own)
w.apply_wall(spec["paths"], keep_descriptors=(1, 2), **({"allow_list": spec["given"]} if spec.get("given") else {}))
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
print(json.dumps({"record": record, "words": w.why_not_proven(record)}))
'''


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class TheRecordNamesTheListInForceTests(unittest.TestCase):
    """The record names the allow list the filter let through, and a row not in the shipped list keeps it from proven:
    it says over which list every other part held, in its fields and in the words a person reads."""

    def setUp(self):
        with open(wall_linux.ALLOW_LIST_FILE, encoding="utf-8") as handle:
            self.shipped = json.load(handle)
        self.folder = tempfile.mkdtemp(prefix="wall-list-")
        self.addCleanup(shutil.rmtree, self.folder)

    def write(self, data, name: str) -> str:
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_the_shipped_list_is_named_and_proven_over_it(self):
        result, _ = run_child(LIST_CHILD, "shipped")
        record = result["record"]
        self.assertIs(record["proven"], True, result["words"])
        self.assertEqual(record["proven_over"], "shipped_list")
        self.assertEqual(record["allow_list"], {"source": "beside_the_wall", "shipped": True,
                                                "digest": _list_digest(_digests(self.shipped)),
                                                "rows": len(self.shipped["rows"]), "rows_not_shipped": [],
                                                "rows_taken_out": []})
        self.assertEqual(_schema_errors(record), [])

    def test_a_list_widened_through_the_wall_s_argument_is_not_proven_and_names_its_row(self):
        widened = dict(self.shipped, rows=self.shipped["rows"] + [PLANTED])
        result, _ = run_child(LIST_CHILD, "given", given=self.write(widened, "given.json"))
        record = result["record"]
        self.assertIs(record["proven"], False, "proven speaks for the shipped list only")
        self.assertEqual(record["proven_over"], "widened_list", result["words"])
        self.assertEqual(record["allow_list"], {"source": "given_by_path", "shipped": False,
                                                "digest": _list_digest(_digests(widened)),
                                                "rows": len(widened["rows"]), "rows_not_shipped": ["kcmp"],
                                                "rows_taken_out": []})
        self.assertIn(wall_linux.WORDS["list_widened"].format(calls="kcmp"), result["words"])
        self.assertIn(wall_linux.WORDS["proven_with_widened_list"].format(calls="kcmp"), result["words"])
        self.assertEqual(_schema_errors(record), [])

    def test_the_list_beside_the_wall_widened_in_place_is_not_proven(self):
        beside = self.write(dict(self.shipped, rows=self.shipped["rows"] + [PLANTED]), "wall_allow_linux.json")
        result, _ = run_child(LIST_CHILD, "beside", beside=beside)
        record = result["record"]
        self.assertIs(record["proven"], False)
        self.assertEqual((record["proven_over"], record["allow_list"]["source"], record["allow_list"]["shipped"],
                          record["allow_list"]["rows_not_shipped"]), ("widened_list", "beside_the_wall", False, ["kcmp"]))
        self.assertIn(wall_linux.WORDS["list_widened"].format(calls="kcmp"), result["words"])

    def test_a_copy_of_the_shipped_list_given_by_path_is_proven_and_named_given(self):
        result, _ = run_child(LIST_CHILD, "copy", given=self.write(self.shipped, "copy.json"))
        record = result["record"]
        self.assertIs(record["proven"], True, result["words"])
        self.assertEqual((record["allow_list"]["source"], record["allow_list"]["shipped"]), ("given_by_path", True))


class TheWordsNameTheListTests(unittest.TestCase):

    def test_a_record_over_a_widened_list_says_so_in_words(self):
        record = {"proven": False, "proven_over": "widened_list",
                  "allow_list": {"rows_not_shipped": ["kcmp", "personality"]}}
        words = wall_linux.why_not_proven(record)
        self.assertIn(wall_linux.WORDS["list_widened"].format(calls="kcmp, personality"), words)
        self.assertIn(wall_linux.WORDS["proven_with_widened_list"].format(calls="kcmp, personality"), words)

    def test_a_widened_list_with_another_fault_names_the_rows_and_not_that_the_rest_held(self):
        words = wall_linux.why_not_proven({"proven": False, "proven_over": None,
                                           "allow_list": {"rows_not_shipped": ["kcmp"]}})
        self.assertIn(wall_linux.WORDS["list_widened"].format(calls="kcmp"), words)
        self.assertNotIn(wall_linux.WORDS["proven_with_widened_list"].format(calls="kcmp"), words)


if __name__ == "__main__":
    unittest.main()
