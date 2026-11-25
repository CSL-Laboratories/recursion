"""The plugin host's own reading of a wall record: a refusal counts only from the part of the wall that holds it.

Where Landlock refuses a kind of write by itself (every kind from Landlock ABI 1 on, truncation from ABI 3 on),
a record that credits the filter with the refusal shows the filter and not the wall, so the host loads nothing,
names each such kind and says why; the schema wall.v1 refuses such a record too. Data only: no wall is put up
and no process is started.
"""
import json
import unittest
from pathlib import Path

from recursion_core import plugin_host
from recursion_core.plugin_host import wall_linux

try:
    import jsonschema
except ImportError:  # some interpreters have none: the schema's own cases are then skipped
    jsonschema = None

FOLDER = Path(__file__).resolve().parents[3].parent / "schemas" / "plugin" / "v1"
GOOD_ABI_2 = "good/wall.v1/h09-below-abi-3-the-filter-refuses-truncation.json"
GOOD_ABI_3 = "good/wall.v1/h01-a-proven-wall.json"
GOOD_ABI_6 = "good/wall.v1/h07-a-proven-wall-with-both-scopes.json"
BAD_TRUNCATION = "bad/wall.v1/b12-proven-crediting-truncation-to-the-filter-at-abi-3.json"
BAD_ALL_FILTER = "bad/wall.v1/b13-proven-at-abi-6-crediting-every-write-to-the-filter.json"


def fixture(name):
    return json.loads((FOLDER / "fixtures" / name).read_text(encoding="utf-8"))


class AFilterOnlyRecordIsNotProven(unittest.TestCase):
    def test_the_good_records_stay_proven(self):
        for name in (GOOD_ABI_2, GOOD_ABI_3, GOOD_ABI_6):
            with self.subTest(name):
                self.assertEqual(plugin_host._wall_missing(fixture(name), ""), [])

    def test_every_write_credited_to_the_filter_at_abi_6_is_missing(self):
        # The record of the claim: ABI 6, both scopes, every kind refused_by_filter.
        record = fixture(BAD_ALL_FILTER)
        self.assertEqual(record["landlock_abi"], 6)
        self.assertEqual(plugin_host._wall_missing(record, ""), ["writes." + kind for kind in wall_linux.WRITE_KINDS])

    def test_truncation_credited_to_the_filter_from_abi_3_on_is_missing(self):
        for abi in (3, 4, 6):
            with self.subTest(abi=abi):
                record = fixture(BAD_TRUNCATION)
                record["landlock_abi"] = abi
                self.assertEqual(plugin_host._wall_missing(record, ""), ["writes.truncate"])

    def test_below_abi_3_the_filter_may_hold_truncation_and_nothing_else(self):
        record = fixture(GOOD_ABI_2)
        record["writes"]["open_for_write"] = "refused_by_filter"
        self.assertEqual(plugin_host._wall_missing(record, ""), ["writes.open_for_write"])

    def test_an_abi_that_is_not_a_whole_number_reads_as_one_that_handles_truncation(self):
        for abi in ("2", None, 2.0, True):
            with self.subTest(abi=abi):
                record = fixture(GOOD_ABI_2)
                record["landlock_abi"] = abi
                self.assertEqual(plugin_host._wall_missing(record, ""), ["writes.truncate"])

    def test_the_words_say_which_part_is_missing_and_what_to_do(self):
        for name, what in ((BAD_ALL_FILTER, "12 kinds of write, each named at the end"),
                           (BAD_TRUNCATION, wall_linux.WORDS["truncate"])):
            with self.subTest(name):
                record = fixture(name)
                missing = plugin_host._wall_missing(record, "")
                detail = plugin_host._unproven_detail(record, missing)
                self.assertIn("its filter, not Landlock, refused " + what, detail)
                self.assertIn("(ABI %d)" % record["landlock_abi"], detail)
                self.assertIn("What you can do:", detail)
                self.assertLessEqual(len(detail), plugin_host._DETAIL_MAX)
                self.assertTrue(detail.endswith(" Missing: " + ", ".join(missing)), detail)

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_the_schema_holds_the_same(self):
        validator = jsonschema.Draft202012Validator(json.loads((FOLDER / "wall.v1.json").read_text(encoding="utf-8")))
        for name in (GOOD_ABI_2, GOOD_ABI_3, GOOD_ABI_6):
            with self.subTest(name):
                self.assertEqual([error.message for error in validator.iter_errors(fixture(name))], [])
        for name, at in ((BAD_TRUNCATION, "/writes/truncate"), (BAD_ALL_FILTER, "/writes/open_for_write")):
            with self.subTest(name):
                seen = {(error.validator, "/" + "/".join(map(str, error.absolute_path)))
                        for error in validator.iter_errors(fixture(name))}
                self.assertIn(("enum", at), seen)


if __name__ == "__main__":
    unittest.main()
