"""The label rule of the person's settings (recursion_core/settings.py), a row of their file: with
``status.label_by_rule`` true, where nothing is declared, a label is read as a part of a name or the end of its
last part, on the text as written and folded, a key prefix in any case, and no length is asked after a label;
each family of shapes the earlier rule let through has its own generator. As shipped the row is false and the
earlier rule stands exactly; ``status.label_words`` holds the words the rule reads, and a file only adds to them.

Every value is a made-up stand-in drawn by the test. Every path is made inside the test's own temporary folder.
"""

from __future__ import annotations

import collections
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from recursion_core import settings
from recursion_core.daemon.tests import test_settings as earlier

from recursion_core.daemon.tests.test_settings_declared import FAMILY_DRAWS, _draws, _family_draws, _quoted

LINEAR_BYTES = 65536  # a test figure
LINEAR_MULTIPLE = 400  # a test figure: the label rule may take this many times a plain pass over the same text
ORDINARY_LABEL_LIKE = ("a token budget of 5", "bypass=on", "monkey=1")  # test values: ordinary texts


def _strict() -> tuple:
    """The label words in force with status.label_by_rule true and nothing added by a file."""
    return settings._label_words_in_force(True, settings._LABEL_WORDS_ROW.default, settings._HIDDEN_NAMES_ROW.default)


def _lines(text: str, words: object) -> tuple[str, str, str]:
    """The value whole, the value at the least cap, and the text as a key cut with its digest."""
    return (settings._shown(text, settings.NO_LIMIT, words=words), settings._shown(text, 40, words=words),
            settings._shown(text + "/" + "q" * 100, 80, key=8, words=words))


class TheRowsAsShipped(unittest.TestCase):
    """As shipped, status.label_by_rule is false: the label rule is never read, and the earlier rule stands."""

    def test_the_rows_and_their_shipped_values(self) -> None:
        by_rule = settings.DECLARED[settings.LABEL_BY_RULE]
        words = settings.DECLARED[settings.LABEL_WORDS]
        self.assertEqual((bool, False), (by_rule.type, by_rule.default))
        self.assertEqual((list, True), (words.type, words.adds))
        self.assertEqual(("password", "passwd", "secret", "apikey", "bearer", "token", "pwd", "pass", "passphrase",
                          "pw", "auth", "pin"), words.default)
        self.assertIsNone(settings._label_words_in_force(False, words.default, ()))

    def test_as_shipped_the_label_rule_is_never_read(self) -> None:
        def never(*_args, **_kwargs):
            raise AssertionError("the label rule was read while status.label_by_rule is false")

        with mock.patch.object(settings, "_labelled_by_rule", never):
            for family in _family_draws():
                for label, password in _draws(family):
                    _lines(label + password, None)
            for text in ORDINARY_LABEL_LIKE:
                _lines(text, None)

    def test_an_ordinary_text_is_shown_as_shipped_and_hidden_by_rule(self) -> None:
        for text in ORDINARY_LABEL_LIKE:
            with self.subTest(text=text):
                self.assertNotEqual(settings._HIDDEN_KEY, settings._shown(text, settings.NO_LIMIT))
                self.assertEqual(settings._HIDDEN_KEY, settings._shown(text, settings.NO_LIMIT, words=_strict()))


class EveryFamilyIsHiddenByRule(unittest.TestCase):
    """With status.label_by_rule true, for each family of shapes the earlier label rule let through: no draw, as a
    value whole and cut and as a key cut, shows anything but the hidden-key words, and no run of 3 characters of
    its password stands in a line."""

    def test_each_family(self) -> None:
        for family in _family_draws():
            with self.subTest(family=family[0]):
                failed = []
                for label, password in _draws(family):
                    lines = _lines(label + password, _strict())
                    runs = {password[i:i + 3] for i in range(len(password) - 2)}
                    leaked = sorted(run for run in runs if any(run in line for line in lines)
                                    and run not in settings._HIDDEN_KEY)
                    if leaked or set(lines) != {settings._HIDDEN_KEY}:
                        failed.append(f"{label + password!r}: {lines[0]!r}")
                shipped = sum(set(_lines(label + password, None)) != {settings._HIDDEN_KEY}
                              for label, password in _draws(family))
                self.assertEqual([], failed[:3], f"{len(failed)} of {FAMILY_DRAWS} draws show a part by rule "
                                                 f"({shipped} as shipped; of 1000 the earlier rule showed {family[1]})")

    def test_a_word_of_the_hidden_names_or_the_label_words_is_a_label_too(self) -> None:
        text = "standinword=abc"
        self.assertNotEqual(settings._HIDDEN_KEY, settings._shown(text, settings.NO_LIMIT, words=_strict()))
        for added in ((*settings._LABEL_WORDS_ROW.default, "standinword"), settings._HIDDEN_NAMES_ROW.default):
            words = settings._label_words_in_force(True, added, ("standinword",))
            self.assertEqual(settings._HIDDEN_KEY, settings._shown(text, settings.NO_LIMIT, words=words))

    def test_the_label_rule_takes_linear_time(self) -> None:
        cases = {
            "one long name": "a_" * (LINEAR_BYTES // 2),
            "a label and white space": "pw" + " " * (LINEAR_BYTES - 2),
            "a label and quotes": "pw" + "'" * (LINEAR_BYTES - 2),
            "case and digit steps": "aB1" * (LINEAR_BYTES // 3),
            "short runs past a prefix": ("sk-" + "x" * 6 + " ") * (LINEAR_BYTES // 10),
            "percent signs": "%7" * (LINEAR_BYTES // 2),
            "format characters": "​" * LINEAR_BYTES,
            "one long word of two scripts": "аb" * (LINEAR_BYTES // 2),
        }

        def fastest(call, text):
            best = float("inf")
            for _ in range(3):
                begun = time.perf_counter()
                call(text)
                best = min(best, time.perf_counter() - begun)
            return best

        for name, text in cases.items():
            with self.subTest(case=name):
                plain = fastest(lambda t: [c.isalnum() for c in t], text)
                taken = fastest(settings._labelled_by_rule, text)
                self.assertLess(taken, LINEAR_MULTIPLE * plain, f"{taken:.4f}s against {plain:.4f}s")


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class TheRowsAreReadFromThePersonsFile(earlier._Folder):
    """The two rows come from the person's own file: false keeps the earlier rule, true reads labels by rule, and
    the file's label words are added to the shipped ones, never put in their place."""

    def status(self, by_rule: str, words: str = "[]") -> str:
        self.put('version = 1\n"db_password=standin7" = 1\n"my_pwd=standin8" = 1\n"standinword=standin9" = 1\n'
                 f'[status]\nlabel_by_rule = {by_rule}\nlabel_words = {words}\n')
        loaded = settings.load(self.environ)
        self.assertEqual(settings.SOURCE_FILE, loaded.values[settings.LABEL_BY_RULE].source)
        return "\n".join(settings.lines(loaded))

    def test_false_keeps_the_earlier_rule(self) -> None:
        shown = self.status("false", '["standinword"]')
        for text in ("db_password=standin7", "my_pwd=standin8", "standinword=standin9"):
            self.assertIn(text, shown)

    def test_true_reads_labels_by_rule_with_the_shipped_and_the_added_words(self) -> None:
        shown = self.status("true", '["standinword"]')
        for text in ("standin7", "standin8", "standin9"):
            self.assertNotIn(text, shown)
        self.assertIn("<hidden key-like name 3 of this file>", shown)

    def test_an_empty_list_keeps_the_shipped_words(self) -> None:
        shown = self.status("true")
        self.assertNotIn("standin7", shown)
        self.assertNotIn("standin8", shown)
        self.assertIn("standinword=standin9", shown)

    def test_a_value_that_is_not_true_or_false_keeps_the_shipped_rule(self) -> None:
        self.put('version = 1\n"my_pwd=standin8" = 1\n[status]\nlabel_by_rule = "yes"\n')
        loaded = settings.load(self.environ)
        self.assertIn(settings.OUT_OF_RANGE, self.codes(loaded))
        self.assertIs(False, loaded[settings.LABEL_BY_RULE])
        self.assertIn("my_pwd=standin8", "\n".join(settings.lines(loaded)))


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class EveryFamilyIsHiddenInStatusAndTheStartLog(earlier._Folder):
    """With status.label_by_rule true in the file, each family's draws as list values and as keys of one file:
    status and the start log read the same as for stand-in passwords of the same length."""

    def shown(self, text: str) -> str:
        self.put(text)
        env = dict(self.environ, PATH=os.environ.get("PATH", ""), PYTHONPATH=str(earlier.CORE), TMPDIR=str(self.tmp))
        proc = subprocess.run([sys.executable, "-B", "-m", "recursion_core.settings"], capture_output=True,
                              text=True, env=env, cwd=str(self.tmp), timeout=120, check=False)
        _, messages, _ = earlier.TheDaemonsStart.start_log(self)
        logged = [message for message in messages if message.startswith("settings: ")]
        return proc.stdout + proc.stderr + "\n".join(logged)

    def test_each_family(self) -> None:
        for family in _family_draws():
            with self.subTest(family=family[0]):
                kept = collections.OrderedDict()
                for label, password in _draws(family):
                    kept.setdefault(label + "\x00" * len(password), (label, password))  # one stand-in each
                real = [label + password for label, password in kept.values()]
                stand_in = [label + "\x00" * len(password) for label, password in kept.values()]

                def file(texts):
                    texts = [text.replace("\x00", "~") for text in texts]
                    return ("version = 1\n" + "".join(f"{_quoted(text)} = 1\n" for text in texts)
                            + '[status]\nvalue_max_chars = "none"\nlabel_by_rule = true\n'
                            f'shown_names = [{", ".join(_quoted(text) for text in texts)}]\n')
                out = self.shown(file(real))
                self.assertIn("<hidden", out)
                self.assertEqual(self.shown(file(stand_in)), out)


if __name__ == "__main__":
    unittest.main()
