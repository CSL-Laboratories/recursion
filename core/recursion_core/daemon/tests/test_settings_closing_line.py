"""Status ends, when a line it shows hides a value, with closing lines that are true in every case
(recursion_core/settings.py, ``_closing_lines``): they count the lines that hide a value the settings file holds
and name that file by its path, and count the lines that hide only a shipped default and name the module where it
is written; where no file is read or the file is not used they say so. The start log never carries them. A
declared number row tells nothing of its value, and the note of a declared row named in status.shown_names says
what to do. No request of the command shows a hidden value.

Every value is a made-up stand-in. Every path is made inside the test's own temporary folder.
"""

from __future__ import annotations

import io
import os
import unittest
from types import MappingProxyType
from unittest import mock

from recursion_core import settings
from recursion_core.daemon.tests import test_settings as earlier
from recursion_core.daemon.tests.test_settings_declared import DECLARED_ROW

SIGN_ROW = "plugin_host.standin_mirror"  # a name with no hidden word, whose value carries a sign-in
NAMED_ROW = "plugin_host.standin_password"  # a name that holds a hidden word
SECRETS = ("standin-set5", "standin-default7", "standin7x", "standin8y", "standin-def9", "standin-ver3")


def _rows(*extra: settings.Setting) -> tuple:
    rows = (*settings.SETTINGS, *extra)
    return rows, MappingProxyType({row.name: row for row in rows})


def _of_file(n: int, path: str, unused: str = "") -> str:
    return (f"{n} line{'' if n == 1 else 's'} above hide{'s' if n == 1 else ''} a value the settings file holds"
            f"{unused}; open {path} yourself to read it as you wrote it")


def _only_default(n: int, unused: str = "") -> str:
    return (f"{n} line{'' if n == 1 else 's'} above hide{'s' if n == 1 else ''} only a shipped default{unused}; it "
            f"is written in the product's settings module, {settings.__file__}")


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class TheClosingLinesOfStatusAreTrueInEveryCase(earlier._Folder):
    """No file; a file that is not used; a declared row set, unset with a default, unset with none; a sign-in
    hidden in a value or in a key; a refused value; nothing hidden: the closing lines count and name exactly."""

    def status(self, *extra: settings.Setting, environ: dict | None = None) -> tuple[list[str], list[str]]:
        rows, declared = _rows(*extra)
        out = io.StringIO()
        environ = self.environ if environ is None else environ
        with mock.patch.object(settings, "SETTINGS", rows), mock.patch.object(settings, "DECLARED", declared):
            settings.main([], environ, out, io.StringIO())
            logged = settings.lines(settings.load(environ))
        shown = out.getvalue().splitlines()
        said = "\n".join(shown)
        for secret in SECRETS:
            self.assertNotIn(secret, said)
        for line in logged:  # the start log never carries a closing line
            self.assertNotIn(" above hide", line)
        self.assertEqual(logged, shown[2:2 + len(logged)])
        return shown[2 + len(logged):], logged

    def declared(self, default: str) -> settings.Setting:
        return settings.Setting(DECLARED_ROW, str, default, "A stand-in row of this test.",
                                holds=settings.HOLDS_SECRET)

    def test_every_case_of_a_declared_row_and_a_sign_in(self) -> None:
        values = {"set": "standin-set5", "default": "standin-default7", "none": ""}
        for with_file in (True, False):
            for state in (("set", "default", "none") if with_file else ("default", "none")):
                for sign_in in (True, False):
                    with self.subTest(with_file=with_file, state=state, sign_in=sign_in):
                        if self.file.exists():
                            self.file.unlink()
                        rows = [self.declared(values["default"] if state == "default" else "")]
                        text, of_file, only_default = "version = 1\n[plugin_host]\n", 0, 0
                        if state == "set":
                            text += f'{DECLARED_ROW.split(".")[1]} = "{values["set"]}"\n'
                            of_file += 1
                        only_default += state == "default"
                        if sign_in:
                            rows.append(settings.Setting(SIGN_ROW, str, "admin:standin7x@host.example",
                                                         "A stand-in row of this test."))
                            if with_file:
                                text += f'{SIGN_ROW.split(".")[1]} = "root:standin8y@host.example"\n'
                                of_file += 1
                            else:
                                only_default += 1
                        if with_file:
                            self.put(text)
                        closing, _ = self.status(*rows)
                        want = ([_of_file(of_file, str(self.file))] if of_file else []) + \
                               ([_only_default(only_default)] if only_default else [])
                        self.assertEqual(want, closing)

    def test_nothing_hidden_gets_no_closing_line(self) -> None:
        self.put('version = 1\n[plugin_host]\ngil_check = false\n')
        self.assertEqual([], self.status()[0])
        self.file.unlink()
        self.assertEqual([], self.status()[0])

    def test_a_file_that_is_not_used_is_said_to_be_not_used(self) -> None:
        unused = f" (the settings file {self.file} is not used, as the note above says)"
        rows = (self.declared("standin-default7"), settings.Setting(NAMED_ROW, str, "standin-def9", "A stand-in."))
        self.put(f'version = 9\n[plugin_host]\n{DECLARED_ROW.split(".")[1]} = "standin-set5"\n')
        closing, logged = self.status(*rows)
        self.assertIn("version must be 1, found 9", "\n".join(logged))
        self.assertEqual([_only_default(2, unused)], closing)
        self.put('version = "password=standin-ver3"\n')
        self.assertEqual([_of_file(1, str(self.file), unused), _only_default(2, unused)], self.status(*rows)[0])

    def test_with_no_absolute_home_no_file_is_read_and_it_says_so(self) -> None:
        unused = " (no settings file is read: there is no absolute XDG_CONFIG_HOME or HOME)"
        closing, _ = self.status(self.declared("standin-default7"), environ={"HOME": "relative"})
        self.assertEqual([_only_default(1, unused)], closing)

    def test_a_key_of_the_file_hidden_whole_or_by_its_sign_in_is_a_value_of_the_file(self) -> None:
        self.put('version = 1\n"admin:standin7x@host.example" = 1\n"token=standin8y" = 2\n')
        self.assertEqual([_of_file(2, str(self.file))], self.status()[0])

    def test_a_value_found_and_refused_is_a_value_of_the_file_and_its_default_a_default(self) -> None:
        number = settings.Setting(DECLARED_ROW, int, 1000, "A stand-in row of this test.", floor=1000,
                                  holds=settings.HOLDS_SECRET)
        self.put(f'version = 1\n[plugin_host]\n{DECLARED_ROW.split(".")[1]} = 12\n')
        self.assertEqual([_of_file(1, str(self.file)), _only_default(1)], self.status(number)[0])
        named = settings.Setting(NAMED_ROW, str, "standin-def9", "A stand-in row of this test.")
        self.put(f'version = 1\n[plugin_host]\n{NAMED_ROW.split(".")[1]} = 12\n')
        self.assertEqual([_of_file(1, str(self.file)), _only_default(1)], self.status(named)[0])

    def test_a_note_that_hides_only_the_default_hides_no_value_of_the_file(self) -> None:
        row = settings.Setting(SIGN_ROW, str, "password=standin-def9", "A stand-in row of this test.")
        self.put(f'version = 1\n[plugin_host]\n{SIGN_ROW.split(".")[1]} = 12\n')
        closing, logged = self.status(row)
        self.assertIn("found 12", "\n".join(logged))
        self.assertEqual([_only_default(1)], closing)

    def test_a_declared_row_set_empty_hides_only_its_default(self) -> None:
        self.put(f'version = 1\n[plugin_host]\n{DECLARED_ROW.split(".")[1]} = ""\n')
        self.assertEqual([_only_default(1)], self.status(self.declared("standin-default7"))[0])


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class NoRequestShowsAHiddenValue(earlier._Folder):
    def test_an_argument_gets_the_usage_and_nothing_of_the_file(self) -> None:
        self.put(f'version = 1\n[plugin_host]\n{DECLARED_ROW.split(".")[1]} = "standin-set5"\n')
        rows, declared = _rows(settings.Setting(DECLARED_ROW, str, "", "A stand-in.", holds=settings.HOLDS_SECRET))
        for argv in (["--show", DECLARED_ROW], ["--value", DECLARED_ROW], [DECLARED_ROW], ["1"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(settings, "SETTINGS", rows), mock.patch.object(settings, "DECLARED", declared):
                self.assertEqual(2, settings.main(argv, self.environ, out, err))
            self.assertEqual("", out.getvalue())
            self.assertTrue(err.getvalue().startswith("usage: python -m recursion_core.settings   (no argument;"))
            self.assertNotIn("standin", err.getvalue())

    def test_the_note_of_a_declared_row_named_in_shown_names_says_what_to_do(self) -> None:
        rows, declared = _rows(settings.Setting(DECLARED_ROW, str, "", "A stand-in.", holds=settings.HOLDS_SECRET))
        self.put(f'version = 1\n[status]\nshown_names = ["{DECLARED_ROW}"]\n'
                 f'[plugin_host]\n{DECLARED_ROW.split(".")[1]} = "standin-set5"\n')
        with mock.patch.object(settings, "SETTINGS", rows), mock.patch.object(settings, "DECLARED", declared):
            shown = "\n".join(settings.lines(settings.load(self.environ)))
        self.assertIn(f"note {settings.STAYS_HIDDEN}: {DECLARED_ROW} is named in status.shown_names, but it holds a "
                      "secret, so status, the start log and every note that names it show only whether it is set; "
                      "naming it there changes nothing, so take it out", shown)
        self.assertNotIn("standin-set5", shown)


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class ADeclaredNumberRowTellsNothingOfItsValue(earlier._Folder):
    """Status, the start log and every note read the same for every value of a declared number row that is in
    force, and the same for every value of it that is refused: only set or not set, the source and the rule."""

    ROW = settings.Setting(DECLARED_ROW, int, 1000, "A stand-in row of this test.", floor=1000, advice=(1000, 9999),
                           holds=settings.HOLDS_SECRET)

    def lines_for(self, value: str) -> list[str]:
        table, key = DECLARED_ROW.split(".")
        self.put(f"version = 1\n[{table}]\n{key} = {value}\n")
        rows, declared = _rows(self.ROW)
        with mock.patch.object(settings, "SETTINGS", rows), mock.patch.object(settings, "DECLARED", declared):
            return settings.lines(settings.load(self.environ))

    def test_every_value_in_force_reads_the_same(self) -> None:
        inside, outside = self.lines_for("5000"), self.lines_for("123456")
        self.assertEqual(inside, outside)
        self.assertNotIn("outside the advice", "\n".join(outside))
        self.assertNotIn("123456", "\n".join(outside))

    def test_every_refused_value_reads_the_same_and_names_only_the_rule(self) -> None:
        below, wrong = self.lines_for("12"), self.lines_for('"standin"')
        self.assertEqual(below, wrong)
        self.assertNotIn("below", "\n".join(below))
        self.assertIn("allowed: a whole number of 1000 or more", "\n".join(below))


if __name__ == "__main__":
    unittest.main()
