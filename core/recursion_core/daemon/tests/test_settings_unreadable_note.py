"""The note for a settings file that cannot be read as a whole (recursion_core/settings.py, settings_unreadable)
shows no character of a value on a line it cannot read and no word of the reader that refused it: a byte that is
not UTF-8 is placed by its line and not by its byte; a value the TOML reader cannot take gets the product's own
words; a line that is not TOML gets the product's own words and the reader's place, its line and its column.

What the place still tells: for a byte that is not UTF-8 and for a value the TOML reader cannot take, two values
of different lengths on a declared row's line give the same note, so the note tells nothing of their length. For
a line that is not TOML the column stays (an earlier test asks for the reader's line and column), and it counts
the characters before that place on its line: on a declared row's line written on one line it gives the length
of the value up to the first character the reader refuses. In every case the line counts the lines before it,
a declared value's own lines among them.

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
PLACE = re.compile(r"\((?:at line \d+, column \d+|at end of document)\)\Z")


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
    """On a declared row's line that is not TOML the note holds the product's words and the reader's place only:
    no character of the value, not the character the reader refused, and no word of the reader's message, in
    status, in lines() and so in the start log. Values that differ only in the refused character at one place
    give the same note."""

    def reader(self, text: str) -> tuple[str, str]:
        with self.assertRaises(settings.tomllib.TOMLDecodeError) as raised:
            settings.tomllib.loads(text)
        words = str(raised.exception)
        place = PLACE.search(words)
        self.assertIsNotNone(place, words)
        return place.group(0), words[:place.start()].strip()

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


if __name__ == "__main__":
    unittest.main()
