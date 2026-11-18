"""The txtar reader finds its "-- name --" markers whether the archive's lines end in LF or CRLF."""
from __future__ import annotations

import unittest

from eval import txtar

LF_ARCHIVE = (
    "a comment\n"
    "-- task.json --\n"
    '{"id": "t"}\n'
    "-- prompt.md --\n"
    "Say hello.\n"
    "-- hidden/test_x.py --\n"
    "assert True\n"
)


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


class TxtarLineEndingTests(unittest.TestCase):
    def test_a_crlf_archive_has_its_sections(self):
        comment, files = txtar.parse(crlf(LF_ARCHIVE))
        self.assertEqual(list(files), ["task.json", "prompt.md", "hidden/test_x.py"])
        self.assertEqual(comment, "a comment\r\n")

    def test_crlf_section_content_keeps_its_own_line_endings(self):
        _, files = txtar.parse(crlf(LF_ARCHIVE))
        self.assertEqual(files["task.json"], '{"id": "t"}\r\n')
        self.assertEqual(files["prompt.md"], "Say hello.\r\n")
        self.assertEqual(files["hidden/test_x.py"], "assert True\r\n")

    def test_an_lf_archive_parses_as_before(self):
        comment, files = txtar.parse(LF_ARCHIVE)
        self.assertEqual(comment, "a comment\n")
        self.assertEqual(files, {"task.json": '{"id": "t"}\n', "prompt.md": "Say hello.\n",
                                 "hidden/test_x.py": "assert True\n"})

    def test_the_crlf_and_lf_archives_give_the_same_names(self):
        self.assertEqual(list(txtar.parse(crlf(LF_ARCHIVE))[1]), list(txtar.parse(LF_ARCHIVE)[1]))

    def test_a_mixed_archive_finds_every_marker(self):
        mixed = "-- a --\r\none\n-- b --\ntwo\r\n"
        self.assertEqual(txtar.parse(mixed), ("", {"a": "one\n", "b": "two\r\n"}))

    def test_a_marker_ending_in_a_bare_carriage_return_stays_content(self):
        # str.splitlines ends a line at a bare "\r", so only "\r\n" and "\n" are taken off before the
        # marker test; a marker line that ends in "\r" alone is content of the section before it.
        text = "-- a --\n-- b --\rafter\n"
        self.assertEqual(txtar.parse(text), ("", {"a": "-- b --\rafter\n"}))

    def test_a_carriage_return_inside_a_marker_name_names_no_section(self):
        # The "\r" ends the line, so neither half is a marker: no section named "b\r" or "b".
        text = "-- a --\n-- b\r --\nrest\n"
        self.assertEqual(txtar.parse(text), ("", {"a": "-- b\r --\nrest\n"}))

    def test_a_crlf_round_trip_through_format(self):
        expected = ("a comment\r\n", {"task.json": '{"id": "t"}\r\n', "prompt.md": "Say hello.\r\n",
                                      "hidden/test_x.py": "assert True\r\n"})
        parsed = txtar.parse(crlf(LF_ARCHIVE))
        self.assertEqual(parsed, expected)
        again = txtar.parse(txtar.format(*parsed))
        self.assertEqual(again, expected)
        self.assertEqual(list(again[1]), ["task.json", "prompt.md", "hidden/test_x.py"])


if __name__ == "__main__":
    unittest.main()
