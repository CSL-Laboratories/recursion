"""The note for a settings file that cannot be read as a whole (recursion_core/settings.py, settings_unreadable)
shows no character of a value on a line it cannot read and no word of the reader that refused it: a byte that is
not UTF-8 is placed by its line and not by its byte; a value the TOML reader cannot take gets the product's own
words; a line that is not TOML gets the product's own words and the line of the reader's place, never its column.

What the note still tells: for a byte that is not UTF-8, for a value the TOML reader cannot take and for a line
that is not TOML, values of different lengths on a declared row's line give the same note, byte for byte, so the
note tells nothing of their length, and nothing of which character the reader refused or where on its line it
stands. It still tells its line, the number of the line that holds the first place the file cannot be read, so it
counts the lines before that place: for a declared value written over several lines, how many of its lines came
before that place. And its words say which kind of fault it met (not UTF-8; not TOML; a value or a nesting the
reader cannot take), and for a line that is not TOML whether the reader met the fault at the end of the document.

Every value is a made-up stand-in. Every path is made inside the test's own temporary folder.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from types import MappingProxyType
from unittest import mock

from recursion_core import settings
from recursion_core.daemon.tests import test_settings as earlier

# Test figures, never the product's: lengths of a stand-in value, and counts of digits past the interpreter's limit.
VALUE_LENGTHS = (5, 14, 30)
DIGIT_COUNTS = (4400, 5000)
# Test figures, never the product's: lengths of an unterminated stand-in text (the file's cap is 65,536 bytes), the
# lengths of a value before a word after its closing quote, the places of a refused character inside one value of
# LONG characters, and the lengths of a key without a value.
UNTERMINATED_LENGTHS = (5, 14, 30, 30000, 60000)
WORD_AFTER_LENGTHS = (5, 30000)
LONG = 30000
REFUSED_PLACES = (1, 17, LONG - 1)
KEY_LENGTHS = (11, 3000)
DECLARED_TEXT_ROW = "plugin_host.standin_login"
DECLARED_NUMBER_ROW = "plugin_host.standin_pin"
# The two halves of a stand-in value, around the character the TOML reader refuses.
BEFORE, AFTER = "StandinQx", "ZwStandin"
# Lines that are not TOML on a declared row, each family a list of lines whose fault stands at one place of the
# value: the reader's words for each name the character it met, or say what it expected.
NOT_TOML = {
    "a control character": [f'standin_login = "{BEFORE}{c}{AFTER}"' for c in ("\x01", "\x07", "\x1f", "\x7f")],
    "an escape the reader refuses": [f'standin_login = "{BEFORE}{e}{AFTER}"' for e in ("\\q", "\\uD800", "\\x41")],
    "an unescaped quote": [f'standin_login = "{BEFORE}"{AFTER}"', f'standin_login = "{BEFORE}" {AFTER}"'],
    "a bare newline inside a basic string": [f'standin_login = "{BEFORE}\n{AFTER}"'],
    "a key without a value": ["standin_login =", "standin_login = ", "standin_pin ="],
}
PLACE = re.compile(r"\((?:at line (\d+), column \d+|at end of document)\)\Z")


def stand_in(length: int) -> str:
    return ("Standin" * (length // 7 + 1))[:length]


# Lines that are not TOML on one and the same line of a declared row, each family a list of lines that differ only
# in the length of what stands before the fault, or in the place of the refused character inside the value.
SAME_LINE = {
    "an unterminated basic text": [f'standin_login = "{stand_in(n)}' for n in UNTERMINATED_LENGTHS],
    "a word after the closing quote": [f'standin_login = "{stand_in(n)}" Standinword' for n in WORD_AFTER_LENGTHS],
    "a refused character at another place": [
        f'standin_login = "{stand_in(LONG)[:at]}\x01{stand_in(LONG)[at:]}"' for at in REFUSED_PLACES],
    "a key without a value": [f"{stand_in(n).lower()} =" for n in KEY_LENGTHS],
    "each control character the reader refuses": [
        f'standin_login = "{BEFORE}{chr(c)}{AFTER}"' for c in (*range(0x00, 0x09), *range(0x0b, 0x20), 0x7f)],
}
# A line that is not TOML at the first line, at a middle line and at the last line of a file, inside a declared
# value written over several lines, and at the end of the document: the file, and the line its note names.
WHERE = {
    "the first line": ("version = 1 Standinword\n[plugin_host]\nstandin_login = \"StandinQx\"\n", 1),
    "a middle line": ("version = 1\n# a\n[plugin_host]\nstandin_login = \"StandinQx\n# b\n# c\n", 4),
    "the last line": ("version = 1\n[plugin_host]\n# a\n# b\nstandin_login = \"Stand\x01inQx\"\n", 5),
    "the third line of a value over several lines": (
        'version = 1\n[plugin_host]\nstandin_login = """\nStandinQx\nZw\x01Standin\n"""\n', 5),
    "the end of the document": ("version = 1\n[plugin_host]\nstandin_login = \"StandinQx", None),
}


class _Declared(earlier._Folder):
    @contextlib.contextmanager
    def declared(self):
        rows = (*settings.SETTINGS,
                settings.Setting(DECLARED_TEXT_ROW, str, "", "A stand-in row of this test.", holds=settings.HOLDS_SECRET),
                settings.Setting(DECLARED_NUMBER_ROW, int, 0, "A stand-in row of this test.", holds=settings.HOLDS_SECRET))
        with mock.patch.object(settings, "SETTINGS", rows), \
                mock.patch.object(settings, "DECLARED", MappingProxyType({r.name: r for r in rows})):
            yield

    def seen(self, data: bytes) -> tuple[str, str, tuple[str, ...]]:
        self.put("version = 1\n")
        self.file.write_bytes(data)
        with self.declared():
            loaded = settings.load(self.environ)
            self.assertEqual([settings.UNREADABLE], [note.code for note in loaded.notes])
            out = io.StringIO()
            self.assertEqual(settings.EXIT_WARNED, settings.main([], self.environ, out, io.StringIO()))
            return loaded.notes[0].detail, out.getvalue(), tuple(settings.lines(loaded))

    def reader(self, text: str) -> tuple[str, str]:
        """The line of the TOML reader's own place as the note must name it, and the reader's words before it."""

        with self.assertRaises(settings.tomllib.TOMLDecodeError) as raised:
            settings.tomllib.loads(text)
        words = str(raised.exception)
        place = PLACE.search(words)
        self.assertIsNotNone(place, words)
        where = "(at end of document)" if place.group(1) is None else f"(at line {place.group(1)})"
        return where, words[:place.start()].strip()

    def note_texts(self, detail: str, out: str, lines: tuple[str, ...]) -> list[str]:
        """The note as it reaches a person or a log (load's detail, status, lines() and so the start log), with
        the file's own path taken out."""

        path = str(self.file)
        texts = [detail, *[line for line in out.splitlines() if settings.UNREADABLE in line],
                 *[line for line in lines if settings.UNREADABLE in line]]
        self.assertEqual(3, len(texts), texts)
        return ["".join(text.split(path)) for text in texts]


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class TheUnreadableNoteTellsNoLength(_Declared):
    """For a byte that is not UTF-8 and for a value the TOML reader cannot take, two values of different lengths
    on a declared row's line give the same note, in status, in lines() and so in the start log; the note still
    names the file and the line, and says what to do."""

    def test_a_byte_that_is_not_utf8_after_a_declared_value_is_placed_by_its_line(self) -> None:
        seen = set()
        for length in VALUE_LENGTHS:
            value = ("standin" * 5)[:length].encode("ascii")
            detail, out, lines = self.seen(b'version = 1\n[plugin_host]\nstandin_login = "' + value + b'\xff"\n')
            self.assertEqual(f"{self.file} is not UTF-8 (at line 3); save it as UTF-8; every default is in force",
                             detail)
            seen.add((detail, out, lines))
        self.assertEqual(1, len(seen))

    def test_a_number_past_the_digit_limit_on_a_declared_row_tells_nothing_of_its_digits(self) -> None:
        seen = set()
        for count in DIGIT_COUNTS:
            detail, out, lines = self.seen(f"version = 1\n[plugin_host]\nstandin_pin = {'7' * count}\n".encode())
            self.assertTrue(detail.startswith(f"{self.file} holds a value or a nesting the TOML reader cannot take"),
                            detail)
            self.assertIn("shorten it", detail)
            self.assertNotIn(str(count), detail)
            for word in ("Exceeds the limit", "set_int_max_str_digits", "digits;"):
                self.assertNotIn(word, out)
            seen.add((detail, out, lines))
        self.assertEqual(1, len(seen))


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class TheNotTomlNoteGivesOnlyThePlace(_Declared):
    """On a declared row's line that is not TOML the note holds the product's words and the line of the reader's
    place only: no character of the value, not the character the reader refused, and no word of the reader's
    message, in status, in lines() and so in the start log. Values that differ only in the refused character at
    one place give the same note."""

    def test_the_note_is_the_place_and_the_way_out_and_nothing_of_the_line(self) -> None:
        for family, rows in NOT_TOML.items():
            notes = set()
            for row in rows:
                text = f"version = 1\n[plugin_host]\n{row}\n"
                with self.subTest(family=family, row=ascii(row)):
                    place, message = self.reader(text)
                    detail, out, lines = self.seen(text.encode("utf-8"))
                    notes.add((detail, out, lines))
                    self.assertEqual(
                        f"{self.file} is not TOML {place}; correct the file there; every default is in force", detail)
                    shown = "\n".join((out, *lines))
                    self.assertNotIn(message, shown)
                    for piece in (BEFORE, AFTER, BEFORE[3:], AFTER[:-3]):
                        self.assertNotIn(piece, shown)
                    refused = row[len('standin_login = "') + len(BEFORE):][:6]
                    for char in refused:  # the refused character as the reader names it, and as itself
                        if char in "\"\\" or not char.isprintable():
                            self.assertNotIn(repr(char), shown)
                        if not char.isprintable():
                            self.assertNotIn(ascii(char)[1:-1], shown)
                            if char != "\n":
                                self.assertNotIn(char, shown)
            if family == "a control character":
                with self.subTest(family=family, one_note=True):
                    self.assertEqual(1, len(notes), family)

    def test_reader_words_with_no_place_give_no_word_of_them(self) -> None:
        refusal = settings.tomllib.TOMLDecodeError.__new__(settings.tomllib.TOMLDecodeError)
        refusal.args = (f"Standin words of the reader about {BEFORE}",)
        with mock.patch.object(settings.tomllib, "loads", side_effect=refusal):
            detail, out, lines = self.seen(b"version = 1\n")
        self.assertEqual(f"{self.file} is not TOML; correct the file; every default is in force", detail)
        for word in ("Standin words", "reader about", BEFORE):
            self.assertNotIn(word, "\n".join((out, *lines)))


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class TheNotTomlNoteNamesTheLineOnly(_Declared):
    """The note for a line that is not TOML names the line and never the column: on one and the same line, values
    of different lengths, a refused character at different places and keys of different lengths give the same
    note, byte for byte, in load's note, in status, in lines() and so in the start log; no such note holds the
    word column or any number but its line's; and the line is the reader's own, on the first, a middle and the
    last line of a file."""

    def test_on_one_line_values_of_different_lengths_give_the_same_note(self) -> None:
        every = set()
        for family, rows in SAME_LINE.items():
            notes = set()
            for row in rows:
                text = f"version = 1\n[plugin_host]\n{row}\n"
                with self.subTest(family=family, length=len(row)):
                    self.assertEqual("(at line 3)", self.reader(text)[0])
                    detail, out, lines = self.seen(text.encode("utf-8"))
                    self.assertEqual(
                        f"{self.file} is not TOML (at line 3); correct the file there; every default is in force",
                        detail)
                    notes.add((detail, out, lines))
            with self.subTest(family=family, one_note=True):
                self.assertEqual(1, len(notes), family)
            every |= notes
        self.assertEqual(1, len(every))

    def test_no_note_holds_the_word_column_or_a_number_but_its_line(self) -> None:
        cases = [(f"version = 1\n[plugin_host]\n{row}\n", 3) for rows in SAME_LINE.values() for row in rows]
        cases += [(text, line) for text, line in WHERE.values() if line is not None]
        for text, line in cases:
            with self.subTest(length=len(text), line=line):
                for note in self.note_texts(*self.seen(text.encode("utf-8"))):
                    self.assertNotIn("column", note.lower())
                    self.assertEqual([str(line)], re.findall(r"\d+", note), note)

    def test_the_line_is_the_readers_own_on_the_first_a_middle_and_the_last_line(self) -> None:
        for case, (text, line) in WHERE.items():
            with self.subTest(case=case):
                where = "(at end of document)" if line is None else f"(at line {line})"
                self.assertEqual(where, self.reader(text)[0])
                detail, _, _ = self.seen(text.encode("utf-8"))
                self.assertEqual(f"{self.file} is not TOML {where}; correct the file there; every default is in force",
                                 detail)


if __name__ == "__main__":
    unittest.main()
