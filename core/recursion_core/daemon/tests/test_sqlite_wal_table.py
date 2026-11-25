"""The table of SQLite releases that carry the WAL-reset repair: pinned version by version, said whole in the
refusal, and kept in a data file of its own shape whose every row carries its reason.

Each version is given as text to ``sqlite_wal_affected``, or STUBBED as ``sqlite3.sqlite_version`` for the refusal,
never the machine's own; every stand-in table file a test writes lives in its own temporary folder.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from types import MappingProxyType
import unittest
from unittest import mock

from recursion_core.events import db as D

# Every version the walk of the table reaches, and a few beside it, with its verdict written out by hand: True is
# refused (the bug is in it), False opens. A change of the table that moves any of them makes a test here red.
PINNED = {
    "3.6.23": False,  # before 3.7.0 there is no WAL and no bug
    "3.7.0": True,    # the first release with WAL
    "3.43.9": True,   # a line without a row
    "3.44.5": True, "3.44.6": False, "3.44.7": False, "3.44.8": False, "3.45.0": True,
    "3.50.6": True, "3.50.7": False, "3.50.8": False, "3.50.9": False, "3.51.0": True,
    "3.51.2": True, "3.51.3": False, "3.51.4": False, "3.51.5": False,
    "3.52.0": False, "3.52.1": False, "3.52.2": False, "3.52.99": False,
    "3.53.0": False, "3.53.1": False, "3.54.0": False,  # from 3.53.0 every release carries the repair
}
WILL_DO = ("Use a Python whose SQLite is 3.53.0 or later, or 3.52.0 or later on the 3.52 line, 3.51.3 or later on "
           "the 3.51 line, 3.50.7 or later on the 3.50 line, or 3.44.6 or later on the 3.44 line.")
LATER_ON_A_LINE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+) or later on the ([0-9]+)\.([0-9]+) line")
PUT_BACK = "Put back the file shipped with Recursion"


def text(version) -> str:
    return ".".join(str(part) for part in version)


def walk(repaired: dict, first: tuple, repaired_from: tuple) -> list[str]:
    """Around every row: the patch before its first repaired one (where there is one), that patch, the two after it,
    and the next line's first release; and the ends: the first release with WAL, the first release from which every
    release is repaired, and the patch after it."""
    versions = [text(first), text(repaired_from), text((*repaired_from[:2], repaired_from[2] + 1))]
    for (major, minor), patch in repaired.items():
        if patch > 0:
            versions.append(text((major, minor, patch - 1)))
        versions += [text((major, minor, patch + step)) for step in (0, 1, 2)]
        versions.append(text((major, minor + 1, 0)))
    return versions


def stub(version: str):
    """The SQLite version the refusal sees; the library itself is the machine's."""
    return mock.patch.object(sqlite3, "sqlite_version", version)


def mismatches() -> list[str]:
    return [version for version, refused in PINNED.items() if D.sqlite_wal_affected(version) is not refused]


class TheTableIsPinnedVersionByVersion(unittest.TestCase):
    def test_every_pinned_version_is_refused_or_opens_as_written(self) -> None:
        for version, refused in PINNED.items():
            with self.subTest(version=version):
                self.assertIs(refused, D.sqlite_wal_affected(version))
                with stub(version):
                    message = D.sqlite_wal_refusal()
                self.assertIs(refused, message is not None)

    def test_the_walk_around_every_row_reaches_only_pinned_versions(self) -> None:
        # a row added to the table without its versions written into PINNED makes this red
        walked = walk(D.SQLITE_WAL_RESET_REPAIRED, D.SQLITE_WAL_FIRST, D.SQLITE_WAL_REPAIRED_FROM)
        self.assertEqual([], [version for version in walked if version not in PINNED])

    def test_a_stand_in_table_that_refuses_3_52_1_is_caught_by_the_pins(self) -> None:
        self.assertEqual([], mismatches())
        refusing = MappingProxyType({**D.SQLITE_WAL_RESET_REPAIRED, (3, 52): 2})
        with mock.patch.object(D, "SQLITE_WAL_RESET_REPAIRED", refusing):
            self.assertEqual(["3.52.0", "3.52.1"], mismatches())


class TheRefusalIsBuiltFromTheTable(unittest.TestCase):
    def refusal(self) -> str:
        with stub("3.45.1"):
            message = D.sqlite_wal_refusal()
        self.assertIsNotNone(message)
        return message

    def test_the_refusal_names_every_line_at_its_first_repaired_patch_in_the_tables_order(self) -> None:
        message = self.refusal()
        wanted = [f"{text(D.SQLITE_WAL_REPAIRED_FROM)} or later, "] + [
            f"{text((*line, patch))} or later on the {text(line)} line"
            for line, patch in D.SQLITE_WAL_RESET_REPAIRED.items()]
        for words in wanted:
            with self.subTest(words=words):
                self.assertIn(words, message)
        places = [message.find(words) for words in wanted]
        self.assertEqual(sorted(places), places)

    def test_the_refusal_names_no_line_the_table_lacks(self) -> None:
        found = LATER_ON_A_LINE.findall(self.refusal())
        self.assertEqual([], [named for named in found if named[:2] != named[3:]])
        self.assertEqual(len(D.SQLITE_WAL_RESET_REPAIRED), len(found))
        self.assertEqual(dict(D.SQLITE_WAL_RESET_REPAIRED),
                         {(int(major), int(minor)): int(patch) for major, minor, patch, _, _ in found})

    def test_the_shipped_words_say_every_sqlite_that_will_do(self) -> None:
        self.assertIn(" Recursion does not open its stores with it. " + WILL_DO + " Check the daemon's interpreter",
                      self.refusal())

    def test_one_function_builds_the_words_from_any_table(self) -> None:
        def table(rows):
            return {"first": (3, 7, 0), "repaired_from": (4, 2, 0), "rows": rows}

        self.assertEqual("Use a Python whose SQLite is 4.2.0 or later.", D.sqlite_wal_will_do(table(())))
        self.assertEqual("Use a Python whose SQLite is 4.2.0 or later, or 4.1.2 or later on the 4.1 line.",
                         D.sqlite_wal_will_do(table((((4, 1), 2, "a stand-in"),))))
        self.assertEqual("Use a Python whose SQLite is 4.2.0 or later, or 4.1.2 or later on the 4.1 line, or 3.9.0 "
                         "or later on the 3.9 line.",
                         D.sqlite_wal_will_do(table((((4, 1), 2, "a"), ((3, 9), 0, "b")))))
        self.assertEqual(WILL_DO, D.sqlite_wal_will_do())


class TheRuleAndTheWordsReadOneReadOnlyObject(unittest.TestCase):
    def test_the_mapping_is_read_only_and_is_the_tables_rows(self) -> None:
        mapping = D.SQLITE_WAL_RESET_REPAIRED
        self.assertIsInstance(mapping, MappingProxyType)
        self.assertEqual({line: patch for line, patch, _reason in D.SQLITE_WAL_TABLE["rows"]}, dict(mapping))
        with self.assertRaises(TypeError):
            mapping[(3, 52)] = 5
        with self.assertRaises(TypeError):
            mapping[(3, 99)] = 0
        with self.assertRaises(TypeError):
            del mapping[(3, 52)]
        self.assertEqual(0, mapping[(3, 52)])
        self.assertNotIn((3, 99), mapping)

    def test_one_change_of_the_object_moves_the_rule_and_the_words_together(self) -> None:
        stand_in = MappingProxyType({(3, 52): 2, (3, 51): 3})
        with mock.patch.object(D, "SQLITE_WAL_RESET_REPAIRED", stand_in):
            self.assertTrue(D.sqlite_wal_affected("3.52.1"))
            self.assertFalse(D.sqlite_wal_affected("3.52.2"))
            self.assertTrue(D.sqlite_wal_affected("3.50.7"))
            with stub("3.45.1"):
                message = D.sqlite_wal_refusal()
            self.assertIn("3.53.0 or later, or 3.52.2 or later on the 3.52 line, or 3.51.3 or later on the 3.51 line.",
                          message)
            self.assertNotIn("3.50.7", message)
            self.assertNotIn("3.44.6", message)
        with stub("3.45.1"):
            self.assertIn(WILL_DO, D.sqlite_wal_refusal())      # put back: the shipped words again
        self.assertFalse(D.sqlite_wal_affected("3.52.1"))


class TheTableIsADataFileWithAReasonOnEveryRow(unittest.TestCase):
    def raw(self) -> dict:
        return json.loads(D.SQLITE_WAL_TABLE_FILE.read_text(encoding="utf-8"))

    def test_the_file_is_of_the_shape_this_code_reads_and_every_row_has_its_reason(self) -> None:
        raw = self.raw()
        self.assertEqual(1, D.SQLITE_WAL_TABLE_SHAPE)
        self.assertEqual(D.SQLITE_WAL_TABLE_SHAPE, raw["shape"])
        for key in ("first_reason", "repaired_from_reason"):
            with self.subTest(key=key):
                self.assertIs(str, type(raw.get(key)))
                self.assertTrue(raw[key].strip())
        for row in raw["lines"]:
            with self.subTest(line=row["line"]):
                self.assertIs(str, type(row.get("reason")))
                self.assertTrue(row["reason"].strip())
        self.assertIn("news page", next(row["reason"] for row in raw["lines"] if row["line"] == "3.52"))

    def test_the_constants_are_the_files_rows_in_its_order(self) -> None:
        raw = self.raw()
        rows = [(tuple(int(part) for part in row["line"].split(".")), row["first_repaired_patch"])
                for row in raw["lines"]]
        self.assertEqual(rows, list(D.SQLITE_WAL_RESET_REPAIRED.items()))
        self.assertEqual(rows, [(line, patch) for line, patch, _reason in D.SQLITE_WAL_TABLE["rows"]])
        self.assertEqual(raw["first"], text(D.SQLITE_WAL_FIRST))
        self.assertEqual(raw["repaired_from"], text(D.SQLITE_WAL_REPAIRED_FROM))
        self.assertEqual(D.SQLITE_WAL_TABLE, D.load_sqlite_wal_table())

    def test_the_loader_refuses_a_bad_file_and_names_the_file_the_place_and_what_to_write(self) -> None:
        def row(index, key, value):
            def change(table):
                if value is None:
                    table["lines"][index].pop(key)
                else:
                    table["lines"][index][key] = value
            return change

        def top(key, value):
            def change(table):
                if value is None:
                    table.pop(key)
                else:
                    table[key] = value
            return change

        def twice(table):
            table["lines"].append(copy.deepcopy(table["lines"][1]))

        def add(line, patch):
            def change(table):
                table["lines"].append({"line": line, "first_repaired_patch": patch, "reason": "a stand-in"})
            return change

        def below_first(table):
            table["first"] = "3.7.4"
            add("3.7", 2)(table)

        cases = {
            "a row without its reason": (row(1, "reason", None), "the row of line 3.51 has no reason"),
            "a row with an empty reason": (row(1, "reason", "  "), "the row of line 3.51 has no reason"),
            "the first release without its reason": (top("first_reason", None), '"first" has no reason'),
            "the repaired end without its reason": (top("repaired_from_reason", ""), '"repaired_from" has no reason'),
            "another shape": (top("shape", 2), '"shape" is 2, and this Recursion reads shape 1'),
            "a shape that is not a number": (top("shape", True), '"shape" is True'),
            "a key the shape does not have": (top("extra", 1), "holds keys its shape does not have: ['extra']"),
            "a row key the shape does not have": (row(0, "note", "x"), "holds keys its shape does not have"),
            "a line given twice": (twice, "the row of line 3.51 is given twice"),
            "a patch as text": (row(1, "first_repaired_patch", "3"), "not a whole number of 0 or more"),
            "a patch that is true": (row(1, "first_repaired_patch", True), "not a whole number of 0 or more"),
            "a negative patch": (row(1, "first_repaired_patch", -1), "not a whole number of 0 or more"),
            "a line that is a version": (row(0, "line", "3.52.0"), 'not a line such as "3.52"'),
            "a line outside the range": (row(0, "line", "3.60"), "lies outside the lines from 3.7.0 to 3.53.0"),
            "a row at the repaired end": (add("3.53", 0), 'the row of line 3.53 starts at 3.53.0, at or past '
                                          '"repaired_from" 3.53.0'),
            "a row past the repaired end": (add("3.53", 2), 'the row of line 3.53 starts at 3.53.2, at or past '
                                            '"repaired_from" 3.53.0'),
            "a row below the first release": (below_first, 'the row of line 3.7 starts at 3.7.2, below "first" 3.7.4'),
            "a first release that is not a version": (top("first", "3.7"), 'not a version such as "3.53.0"'),
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sqlite_wal_repaired.json"
            path.write_text(json.dumps(self.raw()), encoding="utf-8")
            self.assertEqual(D.SQLITE_WAL_TABLE, D.load_sqlite_wal_table(path))
            for name, (change, words) in cases.items():
                with self.subTest(case=name):
                    table = self.raw()
                    change(table)
                    path.write_text(json.dumps(table), encoding="utf-8")
                    with self.assertRaises(ValueError) as caught:
                        D.load_sqlite_wal_table(path)
                    message = str(caught.exception)
                    self.assertIn(str(path), message)
                    self.assertIn(words, message)
                    self.assertIn(PUT_BACK, message)
            with self.subTest(case="a file that is not JSON"):
                path.write_text("{ not json", encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    D.load_sqlite_wal_table(path)
                self.assertIn("is not a JSON file that can be read", str(caught.exception))
                self.assertIn(PUT_BACK, str(caught.exception))

    def test_the_loader_takes_a_row_just_inside_both_bounds(self) -> None:
        table = self.raw()
        table["first"], table["repaired_from"] = "3.7.4", "3.53.2"
        table["lines"] += [{"line": "3.53", "first_repaired_patch": 1, "reason": "a stand-in"},
                           {"line": "3.7", "first_repaired_patch": 4, "reason": "a stand-in"}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sqlite_wal_repaired.json"
            path.write_text(json.dumps(table), encoding="utf-8")
            loaded = D.load_sqlite_wal_table(path)
        self.assertEqual([((3, 53), 1), ((3, 7), 4)], [(line, patch) for line, patch, _ in loaded["rows"]][-2:])


if __name__ == "__main__":
    unittest.main()
