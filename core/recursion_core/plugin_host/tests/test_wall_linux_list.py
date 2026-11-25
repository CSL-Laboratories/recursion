"""The allow list in force is known by its rows: the shipped list is the one whose rows equal the rows pinned in the
wall's own code, and a list changed beside the wall, in place or given by path, is not the shipped list.

Every list a test changes is a copy of the shipped one in a temporary folder of the test's own; where a test needs
the list beside the wall changed, the module's ALLOW_LIST_FILE points at its copy for that test only.
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

PLANTED = {"call": "kcmp", "x86_64": 312, "aarch64": 272, "reason": "planted by the test: a row the shipped list does not hold",
           "recorded": [], "beyond_recording": "planted by the test"}


def _digests(data) -> dict:
    """The digest of each row, computed here apart from the wall's code: the row's JSON with its keys sorted, no
    spaces and ASCII only (prctl's row with the list's prctl options in it), SHA-256, its first 32 hex digits."""
    out = {}
    for row in data["rows"]:
        body = dict(row, prctl_options=data.get("prctl_options", [])) if row["call"] == "prctl" else row
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


if __name__ == "__main__":
    unittest.main()
