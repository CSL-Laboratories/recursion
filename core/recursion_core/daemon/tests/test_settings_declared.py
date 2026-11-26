"""A row of the person's settings that declares it holds a secret, a sign-in or an address shows only whether it
is set (recursion_core/settings.py), by declaration and with no pattern; and a hidden key of the file never tells
what setting it resembles.

Every value is a made-up stand-in drawn by the test (the generators of the shapes that
the label rule has to read live here and are shared). Every path is made inside the test's own temporary folder.
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import string
import unittest
from types import MappingProxyType
from unittest import mock

from recursion_core import settings
from recursion_core.daemon.tests import test_settings as earlier

# Test figures, never the product's: the seed and the draws of the generators.
FAMILY_SEED = 8261
FAMILY_DRAWS = 120  # draws of each family
DECLARED_LONGEST = 30  # a declared row's stand-in values have every length from 0 to this
# The shipped key prefixes, written here and not read from the module, so that dropping one there goes red.
SHIPPED_PREFIXES = ("sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "glpat-",
                    "xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-", "AKIA", "ASIA", "AIza", "ya29.", "hf_", "npm_", "pypi-")
PASSWORD_ALPHABET = tuple(string.ascii_letters + string.digits) + ("/", "#", "?", "%", ",", ";", "!", "$", "*", "+")
MARKS = ("=", ":", ": ", " = ", "=>", " -> ", ":=", " is ", " ", "='", "\t")
LABELS = ("password", "passwd", "secret", "apikey", "bearer", "token")
COMPOUND_BEFORE = ("db", "client", "access", "x_api", "my", "app", "aws", "smtp", "redis", "SECRET")
COMPOUND_WORDS = ("password", "secret", "token", "key", "passwd", "apikey")
OTHER_SPELLINGS = ("pwd", "pass", "passphrase", "pw", "credential", "auth", "private_key", "api key", "pin", "key",
                   "apitoken", "accesskey", "dbpassword", "Credentials", "PassPhrase")
LOOK_ALIKE = {"a": "\u0430", "o": "\u043e", "e": "\u0435", "p": "\u0440", "c": "\u0441", "x": "\u0445",
              "s": "\u0455", "i": "\u0456"}
FORMAT_SPLITS = ("\u200b", "\u00ad", "\u2060", "%2D", "%2d")
# The ordinary sample: made-up texts with no secret; the label rule hides some of them on purpose.
ORDINARY = (
    "user01@example.org", "support@example.org", "noreply@example.com", "ops-team@example.net",
    "token@example.org", "secret@example.org", "password@example.com", "bearer@example.org", "api-key@example.org",
    "my.token@example.org", "secretary@example.com", "tokens@example.org",
    "https://h.example.com/path", "https://user@h.example.com/repo.git", "ssh://git@h.example.com/org/repo",
    "git@h.example.com:org/repo.git", "postgres://db.internal:5432/app", "https://h.example.com/token/refresh",
    "https://h.example.com/api/secret-scanning", "https://h.example.com/?token_type=bearer",
    "https://ci@h.example.com/token/list", "mailto:ops@example.org",
    "sk-learn-models", "sk-learn@cache-dir", "pk_index_main", "rk_cluster_east", "hf_cache_directory",
    "npm_config_cache", "pypi-mirror.example.org", "ghp_style_guide", "risk-level-high", "task_scheduler_v2",
    "token-bucket-size", "secret-santa-list", "password-reset-url", "bearer-auth-mode", "token budget 100000",
    "sk-team@example.org", "hf_user@example.org", "npm_ops@example.org",
    "/var/lib/recursion/state", "~/.config/recursion", "info", "warning", "UTC", "en_US.UTF-8", "on", "auto",
    "plugin_host.gil_check", "recursion-daemon.service", "C:\\Users\\Public", "192.0.2.10:8080",
    "2026-11-25T17:00:00Z", "a,b,c", "x=1;y=2", "release-1.4.2", "main", "origin/main", "*.log",
)


def _password(draw: random.Random, shortest: int = 1, longest: int = 24, at: bool = False) -> str:
    password = "".join(draw.choice(PASSWORD_ALPHABET) for _ in range(draw.randint(shortest, longest)))
    if at:
        place = draw.randint(0, len(password))
        password = password[:place] + "@" + password[place:]
    return password


def _cased(draw: random.Random, word: str) -> str:
    return draw.choice((word, word.upper(), word.capitalize(), word.swapcase()))


def _compound(draw):
    word, before = draw.choice(COMPOUND_WORDS), draw.choice(COMPOUND_BEFORE)
    join = draw.choice(("_", "-", ".", "step", "upper", "middle", "digit"))
    if join == "step":
        return before + word.capitalize()
    if join == "middle":  # the word as a part between two case steps: only the part rule reads it
        return before + word.capitalize() + draw.choice(("File", "Hint", "Id", "Path"))
    if join == "digit":  # the word as a part before a letter-digit step
        return before + "_" + word + str(draw.randint(1, 9))
    if join == "upper":
        return (before + "_" + word).upper()
    return before + join + word


def _percent(draw):
    word = draw.choice(LABELS)
    chosen = [draw.random() < 0.5 for _ in word]
    chosen[draw.randrange(len(word))] = True
    return "".join((f"%{ord(c):02X}" if draw.random() < 0.5 else f"%{ord(c):02x}") if pick else c
                   for c, pick in zip(word, chosen))


def _look_alike(draw):
    word = draw.choice(LABELS)
    if draw.random() < 0.5:  # fullwidth letters, any of them
        return "".join(chr(ord(c) + 0xFEE0) if draw.random() < 0.5 else c for c in word)
    places = [i for i, c in enumerate(word) if c in LOOK_ALIKE]
    chosen = set(draw.sample(places, draw.randint(1, max(1, len(places) - 1))))
    if len(chosen) == len(word):  # a word wholly in another script is not built; keep one plain letter
        chosen.pop()
    return "".join(LOOK_ALIKE[c] if i in chosen else c for i, c in enumerate(word))


def _prefix_case(draw):
    prefix = draw.choice(SHIPPED_PREFIXES)
    spelled = draw.choice([form for form in (prefix.upper(), prefix.lower(), prefix.swapcase(), prefix.title())
                           if form != prefix])
    return spelled


def _prefix_split(draw):
    prefix = draw.choice(SHIPPED_PREFIXES)
    split = draw.choice(FORMAT_SPLITS)
    if split.startswith("%"):
        if "-" in prefix:
            return prefix.replace("-", split)
        split = "\u200b"
    place = draw.randint(1, len(prefix))
    return prefix[:place] + split + prefix[place:]


# Each family: (its name, the read's count of 1000 draws shown with an @ / without, how a draw is made).
def _family_draws():
    def labelled(make_label, shortest=1, longest=24, space=False, marks=MARKS):
        def one(draw, at):
            password = _password(draw, shortest, longest, at)
            if space:
                place = draw.randint(1, len(password))
                password = password[:place] + draw.choice((" ", "\t", "  ")) + password[place:] + "x"
            return make_label(draw) + draw.choice(marks), password
        return one

    def prefixed(make_prefix):
        def one(draw, at):
            return make_prefix(draw), _password(draw, 8, 24, at)
        return one

    return (
        ("compound label", "920/971", labelled(_compound)),
        ("other spellings", "913/969", labelled(lambda draw: _cased(draw, draw.choice(OTHER_SPELLINGS)))),
        ("percent-written label", "938/959", labelled(_percent)),
        ("key prefix in another case", "931/963", prefixed(_prefix_case)),
        ("key prefix split by a format character or %2D", "818/852", prefixed(_prefix_split)),
        ("look-alike letters in a label", "670/699", labelled(_look_alike)),
        ("password shorter than 8 after a label", "458/833",
         labelled(lambda draw: _cased(draw, draw.choice(LABELS)), 1, 7)),
        ("white space inside the password", "394/384",
         labelled(lambda draw: _cased(draw, draw.choice(LABELS)), space=True)),
        ("other separators", "138/156",
         labelled(lambda draw: _cased(draw, draw.choice(LABELS)), marks=(" -> ", " is ", "=>", ":="))),
    )


def _draws(family):
    """(the label or prefix with its mark, the made-up password) of each draw of one family: half hold an @."""
    seed = FAMILY_SEED + [name for name, _, _ in _family_draws()].index(family[0])
    draw = random.Random(seed)
    return [family[2](draw, i % 2 == 0) for i in range(FAMILY_DRAWS)]


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unit_lines(text: str) -> tuple[str, str, str]:
    """The value whole, the value at the least cap, and the text as a key cut with its digest."""
    return (settings._shown(text, settings.NO_LIMIT), settings._shown(text, 40),
            settings._shown(text + "/" + "q" * 100, 80, key=8))


DECLARED_ROW = "plugin_host.standin_login"  # a name with no hidden word, declared by the test alone


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class ADeclaredRowShowsOnlyWhetherItIsSet(earlier._Folder):
    """A row that declares it holds a secret, a sign-in or an address: status, the start log and every note that
    names it show only whether it is set and its source, for its value and its default, whatever the value's shape or length,
    under any cap, and whatever status.shown_names says. The module's one table is extended for the test only."""

    @contextlib.contextmanager
    def declared(self, holds: str, default: str = ""):
        row = settings.Setting(DECLARED_ROW, str, default, "A stand-in row of this test.", holds=holds)
        rows = (*settings.SETTINGS, row)
        with mock.patch.object(settings, "SETTINGS", rows), \
                mock.patch.object(settings, "DECLARED", MappingProxyType({r.name: r for r in rows})):
            yield

    def values(self) -> list[str]:
        draw = random.Random(FAMILY_SEED)
        texts = ["".join(draw.choice(PASSWORD_ALPHABET) for _ in range(n)) for n in range(DECLARED_LONGEST + 1)]
        for family in _family_draws():
            texts.extend(label + password for label, password in _draws(family)[:12])
        return texts

    def status(self) -> tuple[str, list[str]]:
        out = io.StringIO()
        settings.main([], self.environ, out, io.StringIO())
        return out.getvalue(), settings.lines(settings.load(self.environ))

    def test_value_default_and_refused_value_show_nothing_of_it(self) -> None:
        for holds in settings.HOLDS:
            for cap in ('"none"', "40"):
                with self.subTest(holds=holds, cap=cap), self.declared(holds):
                    head = f"version = 1\n[status]\nvalue_max_chars = {cap}\nshown_names = [{_quoted(DECLARED_ROW)}]\n"
                    self.put(head + f'[plugin_host]\nstandin_login = "standin"\n')
                    reference = self.status()
                    self.put(head + "[plugin_host]\nstandin_login = 7\n")
                    refused = self.status()
                    for text in self.values():
                        if not text:
                            continue
                        self.put(head + f"[plugin_host]\nstandin_login = {_quoted(text)}\n")
                        self.assertEqual(reference, self.status(), repr(text))
                        self.put(head + f"[plugin_host]\nstandin_login = [{_quoted(text)}]\n")
                        self.assertEqual(refused, self.status(), repr(text))
                    line = [line for line in reference[1] if line.startswith(DECLARED_ROW)]
                    self.assertEqual([f"{DECLARED_ROW} = {settings._DECLARED_SET} (default "
                                      f"{settings._DECLARED_NOT_SET}; source: settings file)"], line)
                    self.assertIn(settings.STAYS_HIDDEN, " ".join(reference[1]))

    def test_a_declared_default_shows_nothing_of_it(self) -> None:
        self.put("version = 1\n")
        for holds in settings.HOLDS:
            seen = set()
            for text in self.values():
                with self.declared(holds, default=text):
                    out, shown = self.status()
                row = [line for line in shown if line.startswith(DECLARED_ROW)]
                word = settings._DECLARED_SET if text else settings._DECLARED_NOT_SET
                self.assertEqual([f"{DECLARED_ROW} = {word} (default {word}; source: default)"], row, repr(text))
                seen.add(out if text else "")
            self.assertEqual(2, len(seen))  # the empty default, and one same output for every other

    def test_the_start_log_shows_only_that_it_is_set(self) -> None:
        logs = set()
        for text in ("standin", "db_password=standin7", "SK-standin-key-0000", "u:standin7@h.example.org"):
            self.put(f"version = 1\n[plugin_host]\nstandin_login = {_quoted(text)}\n")
            with self.declared(settings.HOLDS_SIGN_IN):
                _, messages, _ = earlier.TheDaemonsStart.start_log(self)
            logged = [message for message in messages if message.startswith("settings: ")]
            self.assertIn(f"settings: {DECLARED_ROW} = {settings._DECLARED_SET} (default {settings._DECLARED_NOT_SET}; "
                          "source: settings file)", logged)
            logs.add(tuple(logged))
        self.assertEqual(1, len(logs))

    def test_a_kind_that_is_not_declared_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            settings.Setting("x.y", str, "", "a misspelt kind", holds="secrets")


@unittest.skipUnless(hasattr(os, "geteuid"), earlier.POSIX_ONLY)
class AHiddenKeyNamesNoCloseSetting(earlier._Folder):
    def test_a_hidden_key_that_resembles_a_setting_name_gets_no_hint(self) -> None:
        self.put('version = 1\n[status]\n"token=hidden_names" = 1\n')  # the hint once named status.hidden_names
        (note,) = settings.load(self.environ).notes
        self.assertTrue(note.detail.startswith(settings._HIDDEN_KEY_NAME.format(1)), note.detail)
        self.assertNotIn("did you mean", note.detail)



if __name__ == "__main__":
    unittest.main()
