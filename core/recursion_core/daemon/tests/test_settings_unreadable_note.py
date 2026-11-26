"""The note for a settings file that cannot be read as a whole (recursion_core/settings.py, settings_unreadable)
tells nothing of a value on a line it cannot read, not even its length: a byte that is not UTF-8 is placed by
its line and not by its byte, and a value the TOML reader cannot take gets the product's own words and none of
the reader's, which count a number's digits.

Every value is a made-up stand-in. Every path is made inside the test's own temporary folder.
"""

from __future__ import annotations

import contextlib
import io
import os
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


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class TheUnreadableNoteTellsNoLength(earlier._Folder):
    """Two values of different lengths on a declared row's line that the file cannot be read past give the same
    note, in status, in lines() and so in the start log; the note still names the file and the line, and says
    what to do."""

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


if __name__ == "__main__":
    unittest.main()
