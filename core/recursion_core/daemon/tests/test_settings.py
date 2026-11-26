"""A person's own settings file (recursion_core/settings.py): one place, owner-only, every value with its default
and shown, never written, and on a system without an owner check no file is read.

Every path is made inside the test's own temporary folder. Nothing outside it is read for a value or written.
"""

from __future__ import annotations

import ast
import collections
import contextlib
import copy
import functools
import hashlib
import inspect
import io
import json
import logging
import os
from pathlib import Path
import pickle
import random
import re
import string
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from types import MappingProxyType
from unittest import mock

from recursion_core import settings

CORE = Path(__file__).resolve().parents[3]
REPO = CORE.parent
SCHEMA_DIR = REPO / "schemas" / "settings" / "v1"
POSIX_ONLY = "the owner check needs a uid; a system without one is the WindowsNoOwnerCheck class"
OFF = "version = 1\n\n[plugin_host]\ngil_check = false\n"
DIGITS, HEX = "9" * 5000, "0x" + "f" * 4000
# Each makes the parser raise a plain ValueError or a RecursionError, not a TOMLDecodeError.
NOT_READ_BY_THE_PARSER = {
    "a whole number past the digit limit": f"version = 1\nx = {DIGITS}\n",
    "a version past the digit limit": f"version = {DIGITS}\n",
    "3000 nested lists": "version = 1\nx = " + "[" * 3000 + "]" * 3000 + "\n",
    "3000 nested inline tables": "version = 1\nx = " + "{a=" * 3000 + "1" + "}" * 3000 + "\n",
}
# The parser reads these; a whole number of more than 4300 digits cannot be printed.
TOO_LONG_TO_SHOW = {
    "gil_check in hex": f"version = 1\n[plugin_host]\ngil_check = {HEX}\n",
    "version in hex": f"version = {HEX}\n",
}
# A RecursionError in the parser on 3.14; on 3.12 it is read and its one key is unknown.
DOTTED_3000 = "version = 1\n" + ".".join(["a"] * 3000) + " = 1\n"
# A key or a whole table this version does not know, and the close known name its note names (or None).
UNKNOWN = {
    "a mistyped key": ("version = 1\n[plugin_host]\ngil_chek = false\n", "plugin_host.gil_check"),
    "a mistyped table": ("version = 1\n[plugin_hosts]\ngil_check = false\nmore = 1\n", "plugin_host"),
    "a key with no close name": ("version = 1\n[plugin_host]\nlater_row = 3\n", None),
}
# A file that cannot be read as a whole, and what its warning must hold (None: the reader's line and column).
BROKEN = {
    "not TOML": ("version = 1\n[plugin_host\n", None),
    "a duplicate key": (OFF + "gil_check = true\n", None),
    "False with a capital": ("version = 1\n[plugin_host]\ngil_check = False\n", None),
    "no version": ("[plugin_host]\ngil_check = false\n", "has no version line; add `version = 1` at its top"),
    "group writable": (OFF, "its mode is 0620; run chmod 600 on it"),
}


class _Folder(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.config = self.tmp / "config"
        (self.config / "recursion").mkdir(parents=True, mode=0o700)
        (self.tmp / "home").mkdir(mode=0o700)
        self.environ = {"XDG_CONFIG_HOME": str(self.config), "HOME": str(self.tmp / "home")}
        self.file = self.config / "recursion" / "settings.toml"

    def put(self, text: str, path: Path | None = None, mode: int = 0o600) -> Path:
        path = path or self.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def codes(self, loaded) -> list[str]:
        return [note.code for note in loaded.notes]

    def assert_default(self, loaded, *codes: str) -> None:
        self.assertIs(loaded["plugin_host.gil_check"], True)
        self.assertEqual(settings.SOURCE_DEFAULT, loaded.values["plugin_host.gil_check"].source)
        self.assertEqual(list(codes), self.codes(loaded))


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class TheFileAndItsDefaults(_Folder):
    def test_with_no_file_every_setting_has_its_default_and_no_note(self) -> None:
        loaded = settings.load(self.environ)
        self.assert_default(loaded)
        self.assertEqual(str(self.file), loaded.path)
        self.assertEqual({row.name for row in settings.SETTINGS}, set(loaded.values))

    def test_the_first_row_is_the_gil_check_on_by_default(self) -> None:
        row = settings.DECLARED["plugin_host.gil_check"]
        self.assertEqual((bool, True), (row.type, row.default))
        self.assertTrue(row.says)

    def test_a_value_in_the_file_is_in_force_with_its_source(self) -> None:
        self.put(OFF)
        loaded = settings.load(self.environ)
        self.assertIs(loaded["plugin_host.gil_check"], False)
        self.assertEqual(settings.SOURCE_FILE, loaded.values["plugin_host.gil_check"].source)
        self.assertEqual([], self.codes(loaded))
        self.assertEqual(["plugin_host.gil_check = false (default true; source: settings file)",
                          "status.value_max_chars = 80 (default 80; source: default)",
                          "status.hidden_names = ['key', 'keys', 'token', 'tokens', 'secret', 'secrets', 'password'], "
                          "and 4 more (default ['key', 'keys', 'token', 'tokens', 'secret', 'secrets', 'password'], "
                          "and 4 more; source: default)",
                          "status.shown_names = [] (default []; source: default)",
                          "status.digest_chars = 8 (default 8; source: default; advised: at most 64)"],
                         settings.lines(loaded))

    def test_a_dotted_key_is_the_same_setting(self) -> None:
        self.put("version = 1\nplugin_host.gil_check = false\n")
        self.assertIs(settings.load(self.environ)["plugin_host.gil_check"], False)

    def test_home_config_is_the_folder_when_xdg_config_home_is_unset(self) -> None:
        self.put(OFF, self.tmp / "home" / ".config" / "recursion" / "settings.toml")
        loaded = settings.load({"HOME": str(self.tmp / "home")})
        self.assertIs(loaded["plugin_host.gil_check"], False)

    def test_calling_load_again_is_a_reload_and_the_value_is_immutable(self) -> None:
        self.assertIs(settings.load(self.environ)["plugin_host.gil_check"], True)
        self.put(OFF)
        loaded = settings.load(self.environ)
        self.assertIs(loaded["plugin_host.gil_check"], False)
        self.assertIsInstance(loaded.values, MappingProxyType)
        with self.assertRaises(TypeError):
            loaded.values["plugin_host.gil_check"] = True
        with self.assertRaises(AttributeError):
            loaded.notes = ()

    def test_the_docstrings_keep_a_later_reload_possible(self) -> None:
        text = (settings.__doc__ or "") + (settings.load.__doc__ or "")
        self.assertIn("Read at daemon start in this build; a later reload may call ``load`` again", text)
        self.assertNotIn("only once", inspect.getsource(settings))


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class OnlyTheOnePlace(_Folder):
    """A settings.toml anywhere but $XDG_CONFIG_HOME/recursion of the daemon's own environment is ignored."""

    def setUp(self) -> None:
        super().setUp()
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)

    def planted(self, folder: Path) -> Path:
        for rel in ("settings.toml", "recursion/settings.toml", ".recursion/settings.toml",
                    ".config/recursion/settings.toml"):
            self.put(OFF, folder / rel)
        return folder

    def test_the_current_folder_is_never_read(self) -> None:
        os.chdir(self.planted(self.tmp / "here"))
        self.assert_default(settings.load(self.environ))
        for relative in (".", "here", "./.config", ""):
            with self.subTest(xdg_config_home=relative):
                loaded = settings.load({"XDG_CONFIG_HOME": relative, "HOME": str(self.tmp / "home")})
                self.assert_default(loaded)
                self.assertEqual(str(self.tmp / "home" / ".config" / "recursion" / "settings.toml"), loaded.path)

    def test_a_relative_home_reads_no_file(self) -> None:
        os.chdir(self.planted(self.tmp / "here"))
        loaded = settings.load({"HOME": "."})
        self.assert_default(loaded, settings.UNREADABLE)
        self.assertIsNone(loaded.path)

    def test_a_project_folder_is_never_read(self) -> None:
        project = self.planted(self.tmp / "project")
        (project / ".git").mkdir()
        self.put('version = 1\n[settings]\n"plugin_host.gil_check" = false\n', project / "recursion.toml")
        os.chdir(project)
        environ = dict(self.environ, PWD=str(project), RECURSION_PROJECT=str(project))
        self.assert_default(settings.load(environ))
        for relative in (".", ".config"):  # as a project's own environment file might set it
            with self.subTest(xdg_config_home=relative):
                self.assert_default(settings.load(dict(environ, XDG_CONFIG_HOME=relative)))

    def test_a_runs_workspace_and_environment_are_never_read(self) -> None:
        workspace = self.planted(self.tmp / "workspace")
        os.chdir(workspace)  # a run works in its workspace
        planted = str(workspace / "settings.toml")
        run_env = dict(self.environ, RECURSION_SETTINGS=planted, RECURSION_SETTINGS_FILE=planted,
                       RECURSION_WORKSPACE=str(workspace), RECURSION_CONFIG=planted)
        self.assert_default(settings.load(run_env))
        self.assertEqual(["environ"], list(inspect.signature(settings.load).parameters))
        main_source = (CORE / "recursion_core" / "daemon" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("settings.load(os.environ)", main_source)  # the daemon passes its own environment

    def test_a_plugins_folder_and_manifest_are_never_read(self) -> None:
        plugin = self.planted(self.config / "recursion" / "plugins" / "p")
        os.chdir(plugin)  # as a plugin host started in the plugin's folder would be
        manifest = {"name": "p", "settings": {"plugin_host.gil_check": False}, "plugin_host": {"gil_check": False}}
        self.put(json.dumps(manifest), plugin / "manifest.json")
        self.assert_default(settings.load(self.environ))

    def test_a_command_line_value_is_refused_and_nothing_is_read(self) -> None:
        planted = self.put(OFF, self.tmp / "elsewhere" / "settings.toml")
        for argv in (["--file", str(planted)], [str(planted)], ["--set", "plugin_host.gil_check=false"]):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(settings, "load", side_effect=AssertionError("read")):
                    self.assertEqual(2, settings.main(argv, self.environ, out, err))
                self.assertEqual("", out.getvalue())
                self.assertIn("no argument", err.getvalue())


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class OwnerOnlyAndWrongFiles(_Folder):
    def test_a_group_or_other_writable_file_is_not_used(self) -> None:
        for mode in (0o620, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                self.put(OFF, mode=mode)
                loaded = settings.load(self.environ)
                self.assert_default(loaded, settings.UNREADABLE)
                self.assertIn("not group or other writable", loaded.notes[0].detail)

    def test_a_link_is_not_used_even_to_an_owner_only_file(self) -> None:
        target = self.put(OFF, self.tmp / "target.toml")
        self.file.symlink_to(target)
        self.assert_default(settings.load(self.environ), settings.UNREADABLE)

    def test_a_file_of_another_user_is_not_used(self) -> None:
        self.put(OFF)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_default(settings.load(self.environ), settings.UNREADABLE)

    def test_a_folder_or_a_fifo_is_not_used_and_nothing_blocks(self) -> None:
        self.file.mkdir()
        self.assert_default(settings.load(self.environ), settings.UNREADABLE)
        self.file.rmdir()
        os.mkfifo(self.file, 0o600)
        self.assert_default(settings.load(self.environ), settings.UNREADABLE)

    def _swap_at_open(self, swap):
        """``os.open`` of the settings file first runs ``swap``: the file changes after the look, before the open."""
        real_open, done = os.open, []

        def opening(path, *args, **kwargs):
            if not done and os.fspath(path) == str(self.file):
                done.append(swap())
            return real_open(path, *args, **kwargs)
        return mock.patch.object(os, "open", side_effect=opening)

    def test_a_file_renamed_over_it_between_the_look_and_the_open_is_refused(self) -> None:
        self.put("version = 1\n")
        other = self.put(OFF, self.tmp / "other.toml")
        with self._swap_at_open(lambda: os.replace(other, self.file)):
            loaded = settings.load(self.environ)
        self.assert_default(loaded, settings.UNREADABLE)
        self.assertIn("changed while it was opened", loaded.notes[0].detail)

    def test_a_fifo_put_there_between_the_look_and_the_open_does_not_block(self) -> None:
        self.put("version = 1\n")
        real_open, result = os.open, []

        def to_fifo() -> None:
            os.unlink(self.file)
            os.mkfifo(self.file, 0o600)
        with self._swap_at_open(to_fifo):
            reader = threading.Thread(target=lambda: result.append(settings.load(self.environ)), daemon=True)
            reader.start()
            reader.join(10)
            blocked = reader.is_alive()
            if blocked:  # a writer lets the blocked open return, so no thread is left behind
                os.close(real_open(self.file, os.O_WRONLY))
                reader.join(10)
        self.assertFalse(blocked, "opening the settings file blocked on a FIFO")
        self.assert_default(result[0], settings.UNREADABLE)
        self.assertIn("changed while it was opened", result[0].notes[0].detail)

    def test_load_routes_makes_the_one_owner_check(self) -> None:
        from recursion_core.daemon import control_adapter
        routes = self.put('{"routes": {}}', self.tmp / "routes.json")
        self.assertEqual({}, control_adapter.load_routes(routes))
        check, kept = settings.owner_only_file, settings.owner_only_file.__code__
        check.__code__ = (lambda info: False).__code__  # the one check refuses, under every name it has
        try:
            with self.assertRaises(control_adapter.RoutesUnreadable):
                control_adapter.load_routes(routes)
        finally:
            check.__code__ = kept

    def test_a_file_that_cannot_be_used_leaves_every_default_with_the_reason(self) -> None:
        cases = {
            "not TOML": "version = 1\n[plugin_host\n",
            "version 2": "version = 2\n[plugin_host]\ngil_check = false\n",
            "no version": "[plugin_host]\ngil_check = false\n",
            "version true": "version = true\n[plugin_host]\ngil_check = false\n",
            "version 1.0": "version = 1.0\n[plugin_host]\ngil_check = false\n",
            "too large": OFF + "# " + "x" * settings.MAX_BYTES + "\n",
            **NOT_READ_BY_THE_PARSER,
            "version in hex": TOO_LONG_TO_SHOW["version in hex"],
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                self.put(text)
                loaded = settings.load(self.environ)  # never raises: the daemon starts
                self.assert_default(loaded, settings.UNREADABLE)
                self.assertIn(str(self.file), loaded.notes[0].detail)
        self.file.write_bytes(b"version = 1\n# \xff\n")
        self.assert_default(settings.load(self.environ), settings.UNREADABLE)

    def test_a_dotted_key_of_3000_parts_never_raises(self) -> None:
        self.put(DOTTED_3000)
        loaded = settings.load(self.environ)
        self.assertIs(loaded["plugin_host.gil_check"], True)
        self.assertEqual(1, len(loaded.notes))
        self.assertIn(loaded.notes[0].code, (settings.UNREADABLE, settings.UNKNOWN_KEY))

    def test_a_whole_number_too_long_to_show_is_named_and_never_raises(self) -> None:
        for case, code, end in (("gil_check in hex", settings.OUT_OF_RANGE, 0),
                                ("version in hex", settings.UNREADABLE, settings.EXIT_WARNED)):
            with self.subTest(case=case):
                self.put(TOO_LONG_TO_SHOW[case])
                loaded = settings.load(self.environ)
                self.assert_default(loaded, code)
                self.assertIn("a whole number too long to show", loaded.notes[0].detail)
                out = io.StringIO()
                self.assertEqual(end, settings.main([], self.environ, out, io.StringIO()))
                self.assertIn("a whole number too long to show", out.getvalue())

    def test_a_quoted_key_that_holds_a_dot_is_never_the_setting_and_is_reported_by_name(self) -> None:
        quoted = '"plugin_host.gil_check"'
        for text, expect in ((f"version = 1\n{quoted} = true\n[plugin_host]\ngil_check = false\n", False),
                             (f"version = 1\nplugin_host.gil_check = false\n{quoted} = true\n", False),
                             (f'version = 1\n{quoted} = "no"\n[plugin_host]\ngil_check = false\n', False),
                             (f"version = 1\n{quoted} = false\n", True)):
            with self.subTest(text=text):
                self.put(text)
                loaded = settings.load(self.environ)
                self.assertIs(expect, loaded["plugin_host.gil_check"])
                source = settings.SOURCE_DEFAULT if expect else settings.SOURCE_FILE
                self.assertEqual(source, loaded.values["plugin_host.gil_check"].source)
                self.assertEqual([settings.UNKNOWN_KEY], self.codes(loaded))
                self.assertIn("'plugin_host.gil_check'", loaded.notes[0].detail)
                self.assertIn("without the quotes", loaded.notes[0].detail)
                self.assertNotIn("stays in force", "\n".join(settings.lines(loaded)))

    def test_a_skipped_key_or_table_is_never_applied_and_its_note_is_a_warning(self) -> None:
        for case, (text, _) in UNKNOWN.items():
            with self.subTest(case=case):
                self.put(text)
                loaded = settings.load(self.environ)
                self.assert_default(loaded, settings.UNKNOWN_KEY)  # one note, for a whole table too
                self.assertEqual([f"warning: note {loaded.notes[0]}"], settings.lines(loaded)[len(loaded.values):])

    def test_the_command_ends_non_zero_when_a_key_or_table_was_skipped(self) -> None:
        for case, (text, _) in UNKNOWN.items():
            with self.subTest(case=case):
                self.put(text)
                out = io.StringIO()
                code = settings.main([], self.environ, out, io.StringIO())
                self.assertNotIn(code, (0, 2))  # 2 is the usage error
                self.assertEqual(settings.EXIT_WARNED, code)
                self.assertIn("note settings_unknown_key", out.getvalue())
        self.put(OFF)
        self.assertEqual(0, settings.main([], self.environ, io.StringIO(), io.StringIO()))

    def put_broken(self, case: str) -> str:
        self.put(BROKEN[case][0], mode=0o620 if case == "group writable" else 0o600)
        return case

    def test_a_file_that_cannot_be_read_as_a_whole_applies_nothing_and_its_note_is_a_warning(self) -> None:
        for case in BROKEN:
            with self.subTest(case=self.put_broken(case)):
                loaded = settings.load(self.environ)
                self.assert_default(loaded, settings.UNREADABLE)
                self.assertEqual([f"warning: note {loaded.notes[0]}"], settings.lines(loaded)[len(loaded.values):])

    def test_the_command_ends_non_zero_for_a_file_that_cannot_be_read_as_a_whole(self) -> None:
        for case in BROKEN:
            with self.subTest(case=self.put_broken(case)):
                out = io.StringIO()
                self.assertEqual(settings.EXIT_WARNED, settings.main([], self.environ, out, io.StringIO()))
                self.assertIn("warning: note settings_unreadable", out.getvalue())

    def test_the_warning_names_the_fault_and_the_way_out(self) -> None:
        for case, (text, expect) in BROKEN.items():
            with self.subTest(case=self.put_broken(case)):
                detail = settings.load(self.environ).notes[0].detail
                if expect is None:  # the line and the column exactly as the TOML reader reports them
                    with self.assertRaises(settings.tomllib.TOMLDecodeError) as raised:
                        settings.tomllib.loads(text)
                    place = re.search(r"\(at line \d+, column \d+\)", str(raised.exception))
                    self.assertIsNotNone(place, str(raised.exception))
                    self.assertIn(place.group(0), detail)
                    self.assertIn("correct the file there", detail)
                else:
                    self.assertIn(expect, detail)

    def test_a_close_known_name_is_suggested_as_text_and_no_other(self) -> None:
        for case, (text, close) in UNKNOWN.items():
            with self.subTest(case=case):
                self.put(text)
                detail = settings.load(self.environ).notes[0].detail
                if close is None:
                    self.assertNotIn("did you mean", detail)
                else:
                    self.assertTrue(detail.endswith(f"; did you mean {close}?"), detail)

    def test_an_unknown_key_is_reported_by_name_and_the_known_keys_are_read(self) -> None:
        self.put(OFF + "later_row = 3\n\n[future]\nanything = 'x'\n")
        loaded = settings.load(self.environ)
        self.assertIs(loaded["plugin_host.gil_check"], False)
        self.assertEqual([settings.UNKNOWN_KEY, settings.UNKNOWN_KEY], self.codes(loaded))
        details = " ".join(note.detail for note in loaded.notes)
        self.assertIn("'plugin_host.later_row'", details)
        self.assertIn("'future'", details)
        self.assertTrue(any("note settings_unknown_key: 'future'" in line for line in settings.lines(loaded)))

    def test_a_value_of_the_wrong_type_keeps_the_default_and_names_key_value_and_what_is_allowed(self) -> None:
        for found, shown in (('"no"', "'no'"), ("0", "0"), ("1", "1"), ("[]", "a list")):
            with self.subTest(found=found):
                self.put(f"version = 1\n[plugin_host]\ngil_check = {found}\n")
                loaded = settings.load(self.environ)
                self.assert_default(loaded, settings.OUT_OF_RANGE)
                detail = loaded.notes[0].detail
                self.assertIn("plugin_host.gil_check", detail)
                self.assertIn(f"found {shown}", detail)
                self.assertIn("allowed: true or false", detail)

    def test_a_whole_number_has_no_ceiling_and_must_be_meaningful(self) -> None:
        row = settings.Setting("test.limit", int, 5, "A one-based position.", floor=1)
        rows = settings.SETTINGS + (row,)
        with mock.patch.object(settings, "SETTINGS", rows), \
                mock.patch.object(settings, "DECLARED", MappingProxyType({r.name: r for r in rows})):
            for found, expect, codes in (("11", 11, []), ("0", 5, [settings.OUT_OF_RANGE]),
                                         ("7", 7, []), ("10", 10, []), ("1", 1, []),
                                         ("2.5", 5, [settings.OUT_OF_RANGE]), ("true", 5, [settings.OUT_OF_RANGE])):
                with self.subTest(found=found):
                    self.put(f"version = 1\n[test]\nlimit = {found}\n")
                    loaded = settings.load(self.environ)
                    self.assertEqual(expect, loaded["test.limit"])
                    self.assertEqual(codes, self.codes(loaded))
                    if codes:
                        reason = ("a value below 1 has no meaning; " if found == "0" else "") + \
                            "allowed: a whole number of 1 or more"
                        self.assertEqual(f"test.limit: found {found}; {reason}; the default 5 stays in force",
                                         loaded.notes[0].detail)


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class LimitsNamesAndAdvice(_Folder):
    def declared(self, *rows: settings.Setting):
        return mock.patch.multiple(settings, SETTINGS=rows,
                                   DECLARED=MappingProxyType({row.name: row for row in rows}))

    def read(self, row: settings.Setting, text: str) -> settings.Loaded:
        self.put(f"version = 1\n{row.name} = {text}\n")
        with self.declared(row):
            return settings.load(self.environ)

    def test_no_limit_is_a_distinct_value(self) -> None:
        for kind, default, ordinary, expected in ((int, 5, "4", 4), (float, 5.0, "1.25", 1.25)):
            with self.subTest(kind=kind):
                row = settings.Setting("test.limit", kind, default, "A limit.", floor=0,
                                       allow_no_limit=True)
                loaded = self.read(row, '"none"')
                value = loaded[row.name]
                self.assertIs(settings.NO_LIMIT, value)
                self.assertEqual("NO_LIMIT", repr(value))
                for number in (default, 0, -1, float("inf")):
                    self.assertNotEqual(number, value)
                self.assertEqual([], self.codes(loaded))
                self.assertEqual(settings.SOURCE_FILE, loaded.values[row.name].source)
                loaded = self.read(row, ordinary)
                self.assertEqual(expected, loaded[row.name])
                self.assertEqual([], self.codes(loaded))

    def test_a_row_must_opt_in_to_no_limit(self) -> None:
        for kind, default, allowed in ((int, 5, "a whole number"), (float, 5.0, "a finite number"),
                                       (bool, True, "true or false")):
            with self.subTest(kind=kind):
                row = settings.Setting("test.limit", kind, default, "A test row.")
                loaded = self.read(row, '"none"')
                self.assertEqual(default, loaded[row.name])
                self.assertEqual(settings.SOURCE_DEFAULT, loaded.values[row.name].source)
                self.assertEqual([settings.OUT_OF_RANGE], self.codes(loaded))
                self.assertIn(f"found 'none'; allowed: {allowed};", loaded.notes[0].detail)

    def test_a_text_row_keeps_the_word_none_as_text(self) -> None:
        row = settings.Setting("test.word", str, "usual", "A word.")
        loaded = self.read(row, '"none"')
        self.assertEqual("none", loaded[row.name])
        self.assertEqual(settings.SOURCE_FILE, loaded.values[row.name].source)
        self.assertEqual([], self.codes(loaded))
        self.assertEqual(["test.word = 'none' (default 'usual'; source: settings file)"], settings.lines(loaded))
        names = settings.Setting("test.names", list, ("a",), "Names.")
        loaded = self.read(names, '"none"')
        self.assertEqual(("a",), loaded[names.name])
        self.assertEqual([settings.OUT_OF_RANGE], self.codes(loaded))

    def test_only_the_reserved_string_means_no_limit(self) -> None:
        row = settings.Setting("test.limit", float, 5.0, "A limit.", allow_no_limit=True)
        for text in ('"None"', '"unlimited"', "inf", "-inf", "nan"):
            with self.subTest(text=text):
                loaded = self.read(row, text)
                self.assertEqual(5.0, loaded[row.name])
                self.assertEqual([settings.OUT_OF_RANGE], self.codes(loaded))
                self.assertIn('allowed: a finite number or "none" for no limit;', loaded.notes[0].detail)

    def test_a_string_array_is_in_force(self) -> None:
        row = settings.Setting("test.names", list, ("fallback",), "Names.")
        loaded = self.read(row, '["Ada", "none", "Lin"]')
        self.assertEqual(("Ada", "none", "Lin"), loaded[row.name])
        self.assertEqual(settings.SOURCE_FILE, loaded.values[row.name].source)
        self.assertEqual([], self.codes(loaded))

    def test_a_non_string_item_rejects_the_whole_array(self) -> None:
        row = settings.Setting("test.names", list, ("fallback",), "Names.")
        for member in ("1", "false", "{name = 'Lin'}", '["Lin"]', HEX):
            with self.subTest(kind=member[:12]):
                loaded = self.read(row, f'["Ada", {member}]')
                self.assertEqual(("fallback",), loaded[row.name])
                self.assertEqual(settings.SOURCE_DEFAULT, loaded.values[row.name].source)
                self.assertEqual([settings.OUT_OF_RANGE], self.codes(loaded))
                self.assertEqual(
                    "test.names: found a list; allowed: a list of strings; the default ['fallback'] stays in force",
                    loaded.notes[0].detail,
                )

    def test_an_empty_array_is_a_value(self) -> None:
        row = settings.Setting("test.names", list, ("fallback",), "Names.")
        loaded = self.read(row, "[]")
        self.assertEqual((), loaded[row.name])
        self.assertEqual(settings.SOURCE_FILE, loaded.values[row.name].source)
        self.assertEqual([], self.codes(loaded))
        self.assertEqual(["test.names = [] (default ['fallback']; source: settings file)"],
                         settings.lines(loaded))

    def test_repeated_names_keep_the_order_of_first_occurrence(self) -> None:
        row = settings.Setting("test.names", list, (), "Names.")
        loaded = self.read(row, '["Lin", "Ada", "Lin", "lin", "Ada"]')
        self.assertEqual(("Lin", "Ada", "lin"), loaded[row.name])
        self.assertEqual([], self.codes(loaded))

    def test_a_declaration_has_no_ceiling(self) -> None:
        self.assertNotIn("ceiling", inspect.signature(settings.Setting).parameters)

    def test_numbers_on_either_side_of_advice_stay_in_force(self) -> None:
        row = settings.Setting("test.limit", int, 5, "A limit.", advice=(2, 10))
        for value in (-1, 2, 10, 11):
            with self.subTest(value=value):
                loaded = self.read(row, str(value))
                self.assertEqual(value, loaded[row.name])
                self.assertEqual(settings.SOURCE_FILE, loaded.values[row.name].source)
                self.assertEqual([], self.codes(loaded))
                outside = "; outside the advice" if value in (-1, 11) else ""
                self.assertEqual(
                    [f"test.limit = {value} (default 5; source: settings file; advised: at least 2, "
                     f"at most 10{outside})"],
                    settings.lines(loaded),
                )

    def test_advice_can_be_one_sided_and_is_shown_with_a_default(self) -> None:
        for bounds, shown in (((0, None), "at least 0"), ((None, 10), "at most 10")):
            with self.subTest(bounds=bounds):
                row = settings.Setting("test.limit", int, 5, "A limit.", advice=bounds)
                with self.declared(row):
                    loaded = settings.load(self.environ)
                self.assertEqual(
                    [f"test.limit = 5 (default 5; source: default; advised: {shown})"],
                    settings.lines(loaded),
                )

    def test_below_a_meaningful_floor_keeps_the_default_with_the_reason(self) -> None:
        row = settings.Setting("test.length", int, 5, "A length.", floor=0)
        loaded = self.read(row, "-1")
        self.assertEqual(5, loaded[row.name])
        self.assertEqual(settings.SOURCE_DEFAULT, loaded.values[row.name].source)
        self.assertEqual([settings.OUT_OF_RANGE], self.codes(loaded))
        self.assertEqual("test.length: found -1; a value below 0 has no meaning; allowed: a whole number of 0 or more; "
                         "the default 5 stays in force",
                         loaded.notes[0].detail)
        loaded = self.read(row, "0")
        self.assertEqual(0, loaded[row.name])
        self.assertEqual([], self.codes(loaded))

    def test_no_limit_is_outside_an_at_most_advice_and_not_an_at_least_one(self) -> None:
        for kind, default, advice, outside in ((int, 5, (None, 10), True), (int, 5, (1, None), False),
                                               (float, 5.0, (None, 10.0), True), (float, 5.0, (1.0, None), False)):
            with self.subTest(kind=kind, advice=advice):
                row = settings.Setting("test.limit", kind, default, "A limit.", allow_no_limit=True, advice=advice)
                line = settings.lines(self.read(row, '"none"'))[0]
                self.assertEqual(outside, line.endswith("; outside the advice)"), line)

    def test_no_limit_stays_the_one_value_through_a_copy_and_a_pickle(self) -> None:
        one = settings.NO_LIMIT
        self.assertIs(one, copy.copy(one))
        self.assertIs(one, copy.deepcopy(one))
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocol=protocol):
                self.assertIs(one, pickle.loads(pickle.dumps(one, protocol)))
        row = settings.Setting("test.limit", int, 5, "A limit.", allow_no_limit=True)
        held = copy.deepcopy(dict(self.read(row, '"none"').values))
        self.assertIs(one, held[row.name].value)
        self.assertEqual("no limit", settings._shown(held[row.name].value))

    def test_a_float_below_its_floor_keeps_the_default_with_the_reason(self) -> None:
        row = settings.Setting("test.span", float, 1.5, "A span.", floor=0)
        loaded = self.read(row, "-0.5")
        self.assertEqual(1.5, loaded[row.name])
        self.assertEqual(settings.SOURCE_DEFAULT, loaded.values[row.name].source)
        self.assertEqual([settings.OUT_OF_RANGE], self.codes(loaded))
        self.assertEqual("test.span: found -0.5; a value below 0 has no meaning; allowed: a finite number of 0 or "
                         "more; the default 1.5 stays in force",
                         loaded.notes[0].detail)
        loaded = self.read(row, "0.0")
        self.assertEqual(0.0, loaded[row.name])
        self.assertEqual([], self.codes(loaded))

    def test_a_float_beyond_its_advice_is_in_force_and_marked(self) -> None:
        row = settings.Setting("test.span", float, 1.5, "A span.", advice=(None, 10.0))
        loaded = self.read(row, "11.5")
        self.assertEqual(11.5, loaded[row.name])
        self.assertEqual([], self.codes(loaded))
        self.assertEqual(["test.span = 11.5 (default 1.5; source: settings file; advised: at most 10.0; "
                          "outside the advice)"], settings.lines(loaded))

    def test_status_shows_no_limit_and_the_names(self) -> None:
        rows = (
            settings.Setting("test.limit", int, 5, "A limit.", allow_no_limit=True, advice=(None, 10)),
            settings.Setting("test.names", list, (), "Names."),
        )
        self.put('version = 1\n[test]\nlimit = "none"\nnames = ["Lin", "Ada"]\n')
        out, err = io.StringIO(), io.StringIO()
        with self.declared(*rows):
            self.assertEqual(0, settings.main([], self.environ, out, err))
        self.assertEqual("", err.getvalue())
        shown = out.getvalue().splitlines()
        self.assertEqual(4, len(shown))
        self.assertEqual(f"settings file: {self.file}", shown[0])
        self.assertTrue(shown[1].startswith("These are the values a daemon started from this environment"))
        self.assertEqual(
            ["test.limit = no limit (default 5; source: settings file; advised: at most 10; outside the advice)",
             "test.names = ['Lin', 'Ada'] (default []; source: settings file)"],
            shown[2:],
        )


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class ShownAndNeverWritten(_Folder):
    def test_the_command_prints_each_setting_its_default_source_and_the_notes(self) -> None:
        self.put(OFF + "unknown = 1\n")
        out = io.StringIO()
        self.assertEqual(settings.EXIT_WARNED, settings.main([], self.environ, out, io.StringIO()))
        text = out.getvalue()
        self.assertIn(f"settings file: {self.file}", text)
        self.assertIn("plugin_host.gil_check = false (default true; source: settings file)", text)
        self.assertIn("note settings_unknown_key: 'plugin_host.unknown'", text)

    def test_python_dash_m_runs_the_read_only_command(self) -> None:
        env = dict(self.environ, PATH=os.environ.get("PATH", ""), PYTHONPATH=str(CORE), TMPDIR=str(self.tmp))
        proc = subprocess.run([sys.executable, "-B", "-m", "recursion_core.settings"], capture_output=True,
                              text=True, env=env, cwd=str(self.tmp), timeout=60, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        self.assertIn("plugin_host.gil_check = true (default true; source: default)", proc.stdout)

    def test_no_public_name_writes_the_file_or_its_folder(self) -> None:
        self.put(OFF + "unknown = 1\n")
        folder = self.file.parent
        before = (self.file.read_bytes(), os.stat(self.file).st_mtime_ns, os.stat(self.file).st_mode,
                  sorted(os.listdir(folder)), os.stat(folder).st_mtime_ns)
        loaded = settings.load(self.environ)
        settings.lines(loaded)
        settings.settings_path(self.environ)
        settings.owner_only_file(os.lstat(self.file))
        settings.main([], self.environ, io.StringIO(), io.StringIO())
        settings.main(["--file", str(self.file)], self.environ, io.StringIO(), io.StringIO())
        after = (self.file.read_bytes(), os.stat(self.file).st_mtime_ns, os.stat(self.file).st_mode,
                 sorted(os.listdir(folder)), os.stat(folder).st_mtime_ns)
        self.assertEqual(before, after)

    def test_the_module_has_no_writing_call_and_imports_no_toml_writer(self) -> None:
        tree = ast.parse(inspect.getsource(settings))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(tree)
                                                 if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imported & {"tomli_w", "tomlkit", "toml", "json", "shutil", "tempfile"}, imported)
        writes = {"write", "write_text", "write_bytes", "writelines", "dump", "dumps", "replace", "rename",
                  "unlink", "remove", "rmdir", "mkdir", "makedirs", "chmod", "chown", "truncate", "ftruncate",
                  "symlink", "link", "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}
        used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(set(), used & writes)
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("open", calls)  # the one read is os.open with O_RDONLY

    def test_no_core_op_plugin_api_name_or_method_refers_to_settings(self) -> None:
        protocol = json.loads((CORE / "recursion_core" / "plugin_security" / "protocol.json").read_text(encoding="utf-8"))
        self.assertTrue(protocol["ops"])
        self.assertEqual([], [op for op in protocol["ops"] if "setting" in op.lower()])
        api = (REPO / "schemas" / "plugin" / "v1" / "api.v1.json").read_text(encoding="utf-8")
        self.assertNotIn("setting", api.lower())
        table = json.loads((REPO / "schemas" / "control" / "v1" / "methods.table.v1.json").read_text(encoding="utf-8"))
        names = json.dumps(table["methods"]).lower()
        self.assertTrue(table["methods"])
        self.assertNotIn("setting", names)
        # The core's protocol module, the one a plugin's client is built from, says nothing of settings.
        # The plugin host module may read its own row: what a plugin is handed is searched in WhatAPluginIsHanded.
        source = (CORE / "recursion_core" / "plugin_security" / "protocol.py").read_text(encoding="utf-8")
        self.assertNotIn("settings", source)


class _ReachedTheLock(Exception):
    pass


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class TheDaemonsStart(_Folder):
    """The daemon's ``main()`` up to its registry: the start checks are passed, the lock holds nothing and the
    registry is replaced by a stop."""

    def start_log(self) -> tuple[list, list[str], list[tuple[str, str]]]:
        from recursion_core.daemon import __main__ as daemon_main
        loads, real = [], settings.load

        def counted(environ):
            loads.append(real(environ))
            return loads[-1]
        with mock.patch.dict(os.environ, self.environ), \
                mock.patch.object(settings, "load", side_effect=counted), \
                mock.patch.object(daemon_main, "require_posix"), \
                mock.patch.object(daemon_main, "require_free_threaded"), \
                mock.patch.object(daemon_main, "refuse_engine_user"), \
                mock.patch.object(daemon_main, "check_operator_linger"), \
                mock.patch.object(daemon_main.logging, "basicConfig") as basic_config, \
                mock.patch.object(daemon_main.signal, "signal"), \
                mock.patch.object(daemon_main, "UserLock"), \
                mock.patch.object(daemon_main, "Registry", side_effect=_ReachedTheLock), \
                self.assertLogs("recursion_core.daemon", "INFO") as logs, \
                self.assertRaises(_ReachedTheLock):
            self.basic_config = basic_config
            daemon_main.main()
        records = [(record.levelname, record.getMessage()) for record in logs.records]
        return loads, [message for _, message in records], records

    def test_the_start_log_holds_the_lines_of_one_load(self) -> None:
        self.put(OFF + "unknown = 1\n")
        loads, messages, _ = self.start_log()
        self.assertEqual(1, len(loads))
        logged = [message for message in messages if message.startswith("settings: ")]
        self.assertEqual(["settings: " + line for line in settings.lines(loads[0])], logged)
        self.assertIn("settings: plugin_host.gil_check = false (default true; source: settings file)", logged)
        self.assertIn("note settings_unknown_key: 'plugin_host.unknown'", " ".join(logged))

    def test_status_and_start_log_name_the_file_from_each_environment(self) -> None:
        self.put(OFF)
        shell_config = self.tmp / "shell-config"
        shell_file = self.put(OFF.replace("false", "true"),
                              shell_config / "recursion" / "settings.toml")
        sentence = ('These are the values a daemon started from this environment would take; a daemon already running keeps the values it read at its own start, which it wrote to its log in the lines beginning `settings file:` and `settings:` (for the shipped user unit, `journalctl --user -u recursion-daemon` shows them only where the system journal is stored on disk; where there is no per-user journal, someone allowed to read the system journal can match the unit and its owner with `journalctl _SYSTEMD_USER_UNIT=recursion-daemon.service _SYSTEMD_OWNER_UID=@UID@` (@UID@ is the user id of the account that ran this command; the unit has this same name for every account, so the unit match alone also shows the daemons of other accounts on a shared host); a daemon started another way writes them to its standard error, wherever that start sent it).').replace("@UID@", str(os.geteuid()))
        for config, path, value in ((self.config, self.file, "false"),
                                    (shell_config, shell_file, "true")):
            with self.subTest(config=config), mock.patch.dict(os.environ, XDG_CONFIG_HOME=str(config)):
                out = io.StringIO()
                self.assertEqual(0, settings.main([], out=out, err=io.StringIO()))
                shown = out.getvalue().splitlines()
                self.assertEqual(f"settings file: {path}", shown[0])
                self.assertIn(sentence, shown)
                self.assertIn(f"plugin_host.gil_check = {value} (default true; source: settings file)", shown)

        _, _, records = self.start_log()
        self.assertIn(("INFO", f"settings file: {self.file}"), records)
        self.assertIn(("INFO", "settings: plugin_host.gil_check = false (default true; source: settings file)"),
                      records)

    def test_a_start_the_lock_refuses_writes_no_settings_record(self) -> None:
        from recursion_core.daemon import AlreadyRunning
        from recursion_core.daemon import UserLock
        from recursion_core.daemon import __main__ as daemon_main
        self.put(OFF)
        other = self.tmp / "other-config"
        self.put(OFF.replace("false", "true"), other / "recursion" / "settings.toml")
        runtime = self.tmp / "runtime"
        environ = {**self.environ, "XDG_CONFIG_HOME": str(other), "XDG_RUNTIME_DIR": str(runtime)}
        with UserLock(runtime):  # a daemon of the first configuration holds the lock
            with mock.patch.dict(os.environ, environ), \
                    mock.patch.object(daemon_main, "require_posix"), \
                    mock.patch.object(daemon_main, "require_free_threaded"), \
                    mock.patch.object(daemon_main, "refuse_engine_user"), \
                    mock.patch.object(daemon_main, "check_operator_linger"), \
                    mock.patch.object(daemon_main.logging, "basicConfig"), \
                    mock.patch.object(daemon_main.signal, "signal"), \
                    self.assertNoLogs("recursion_core.daemon", "INFO"), \
                    self.assertRaises(AlreadyRunning):
                daemon_main.main()

    def test_every_start_line_carries_the_process_id_and_a_time(self) -> None:
        self.put(OFF)
        self.start_log()
        fmt = self.basic_config.call_args.kwargs["format"]
        record = logging.LogRecord("recursion_core.daemon", logging.INFO, __file__, 1, "settings file: x", None, None)
        shown = logging.Formatter(fmt).format(record)
        self.assertIn(f"[{os.getpid()}]", shown)
        self.assertRegex(shown, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")

    def test_the_status_sentence_names_the_line_and_the_place_the_unit_file_gives(self) -> None:
        unit = (CORE / "systemd" / "recursion-daemon.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=%h/.local/lib/recursion/venv/bin/python3.14t -X gil=0 -m recursion_core.daemon", unit)
        self.assertNotIn("StandardOutput", unit)  # so the daemon's log is the user journal's, by the unit's name
        self.assertNotIn("StandardError", unit)
        out = io.StringIO()
        settings.main([], environ=self.environ, out=out, err=io.StringIO())
        shown = out.getvalue()
        self.assertIn("`journalctl --user -u recursion-daemon` shows them only where the system journal is stored on disk", shown)
        self.assertIn(f"`journalctl _SYSTEMD_USER_UNIT=recursion-daemon.service _SYSTEMD_OWNER_UID={os.geteuid()}`", shown)
        self.assertIn("lines beginning `settings file:` and `settings:`", shown)
        self.assertIn("standard error", shown)
        self.put(OFF)
        self.assertIn("settings file: " + str(self.file), [m for m in self.start_log()[1]])

    def test_the_status_sentence_adds_the_owner_filter_beside_the_unit_match(self) -> None:
        out = io.StringIO()
        settings.main([], environ=self.environ, out=out, err=io.StringIO())
        shown = out.getvalue()
        uid = os.geteuid()
        self.assertEqual(1, shown.count("_SYSTEMD_OWNER_UID="))
        self.assertIn(f"_SYSTEMD_OWNER_UID={uid}", shown)
        self.assertIn(f"`journalctl _SYSTEMD_USER_UNIT=recursion-daemon.service _SYSTEMD_OWNER_UID={uid}`", shown)
        self.assertIn(f"{uid} is the user id of the account that ran this command", shown)
        self.assertNotIn("<your user id>", shown)

    def test_with_every_file_the_parser_or_the_notes_cannot_take_the_daemon_reaches_its_lock(self) -> None:
        cases = {**NOT_READ_BY_THE_PARSER, **TOO_LONG_TO_SHOW, "a dotted key of 3000 parts": DOTTED_3000}
        for case, text in cases.items():
            with self.subTest(case=case):
                self.put(text)
                loads, messages, _ = self.start_log()
                self.assertEqual(1, len(loads))
                self.assertIn("settings: plugin_host.gil_check = true (default true; source: default)", messages)
                self.assertTrue(any("note settings_" in message for message in messages))
                warned = any(note.warns for note in loads[0].notes)
                self.assertEqual(settings.EXIT_WARNED if warned else 0,
                                 settings.main([], self.environ, io.StringIO(), io.StringIO()))

    def test_a_skipped_key_or_table_or_file_is_a_warning_in_the_start_log(self) -> None:
        for case, (text, _) in {**UNKNOWN, **BROKEN}.items():
            with self.subTest(case=case):
                self.put(text, mode=0o620 if case == "group writable" else 0o600)
                _, _, records = self.start_log()
                levels = {message: level for level, message in records if message.startswith("settings: ")}
                warned = [message for message, level in levels.items() if level == "WARNING"]
                self.assertEqual(1, len(warned), levels)
                self.assertIn("settings_unknown_key" if case in UNKNOWN else "settings_unreadable", warned[0])
                self.assertEqual("INFO", levels["settings: plugin_host.gil_check = true (default true; source: default)"])


def _held_of_settings(root) -> list[str]:
    """The paths by which ``root``, an object handed to a plugin, carries a reference to the settings module.

    WHAT A GREEN RESULT PROVES: nothing the plugin is handed carries a reference to the settings module, or
    to an object made by it, by any of these walks, each named: a function's closure cells, defaults and
    keyword defaults; a function's own attributes (its ``__dict__``, where ``functools.wraps`` stores
    ``__wrapped__``); a bound method's instance and function; a built-in method's object; a partial's
    function, arguments and keywords; the items of dicts, lists, tuples, sets and deques; an instance's
    ``__dict__`` and ``__slots__`` (including a single-string ``__slots__`` and private double-underscore
    slots under their mangled name); the attributes of the classes of an object (all but the built-in
    ones); a property's getter, setter and deleter; a static or class method's function; and the local
    variables of a paused generator, coroutine or asynchronous generator.

    WHAT IT CANNOT PROVE: anything by a walk not listed, and anything reached through the interpreter
    itself: a function's module namespace (``__globals__``, ``__builtins__``), ``sys.modules``, a module's
    attributes, the garbage collector, the frames of running threads. A plugin in the daemon's process can
    always import a module for itself; this search says what is handed, and no more."""

    held = settings.__name__
    found, seen, todo = [], set(), [("handed", root)]

    def class_attributes(path, klass):
        return [(f"{path}.class[{k.__name__}].{name}", value) for k in klass.__mro__ if k.__module__ != "builtins"
                for name, value in list(vars(k).items())]

    while todo:
        path, obj = todo.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if obj is settings or type(obj).__module__ == held or getattr(obj, "__module__", None) == held:
            found.append(path)
            continue
        if isinstance(obj, (types.ModuleType, str, bytes, int, float, complex, type(None))):
            continue
        kids: list[tuple[str, object]] = []
        if isinstance(obj, type):
            kids += class_attributes(path, obj)
        elif isinstance(obj, types.FunctionType):
            for i, cell in enumerate(obj.__closure__ or ()):
                try:
                    kids.append((f"{path}.closure[{i}]", cell.cell_contents))
                except ValueError:  # an empty cell
                    pass
            kids += [(f"{path}.default", d) for d in obj.__defaults__ or ()]
            kids += [(f"{path}.kwdefault", d) for d in (obj.__kwdefaults__ or {}).values()]
            kids.append((f"{path}.__dict__", vars(obj)))  # carrier: a function's own attributes
        elif isinstance(obj, types.MethodType):
            kids += [(f"{path}.__self__", obj.__self__), (f"{path}.__func__", obj.__func__)]
        elif isinstance(obj, types.BuiltinMethodType):
            kids.append((f"{path}.__self__", obj.__self__))  # carrier: a built-in method's object
        elif isinstance(obj, functools.partial):
            kids += [(f"{path}.func", obj.func), (f"{path}.args", obj.args), (f"{path}.keywords", obj.keywords)]
        elif isinstance(obj, property):
            kids += [(f"{path}.{name}", fn) for name, fn in (("fget", obj.fget), ("fset", obj.fset),  # carrier: a property
                                                              ("fdel", obj.fdel))]
        elif isinstance(obj, (staticmethod, classmethod)):
            kids.append((f"{path}.__func__", obj.__func__))
        elif isinstance(obj, (types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType)):
            frame = getattr(obj, "gi_frame", None) or getattr(obj, "cr_frame", None) or getattr(obj, "ag_frame", None)
            if frame is not None:
                kids.append((f"{path}.locals", dict(frame.f_locals)))  # carrier: a paused generator's local variables
        elif isinstance(obj, dict):
            for key, value in list(obj.items()):
                kids += [(f"{path}.key", key), (f"{path}[{key!r}]", value)]
        elif isinstance(obj, (list, tuple, set, frozenset, collections.deque)):
            kids += [(f"{path}[]", item) for item in list(obj)]
        else:
            if hasattr(obj, "__dict__"):
                kids.append((f"{path}.__dict__", vars(obj)))
            for klass in type(obj).__mro__:
                slots = vars(klass).get("__slots__", ())
                for slot in (slots,) if isinstance(slots, str) else slots:
                    if slot.startswith("__") and not slot.endswith("__"):
                        owner = klass.__name__.lstrip("_")
                        if owner:
                            slot = f"_{owner}{slot}"
                    if hasattr(obj, slot):
                        kids.append((f"{path}.{slot}", getattr(obj, slot)))
            kids += class_attributes(path, type(obj))  # carrier: the class attributes of the handed object
        todo += kids
    return found


def _handed_over(kind: str, held):
    """Stand-ins for what a core that hands a plugin the settings could return, one carrier for each kind."""

    if kind == "closure":
        return lambda request: held
    if kind == "built-in method":
        return {"in force": held}.get
    if kind == "function attribute":
        def with_attribute(request):
            return None
        with_attribute.in_force = held
        return with_attribute
    if kind == "wraps":
        @functools.wraps(lambda request: held)
        def wrapper(request):
            return None
        return wrapper
    if kind == "class attribute":
        class WithClassAttribute:
            in_force = held

            def __call__(self, request):
                return None
        return WithClassAttribute()
    if kind == "property":
        class WithProperty:
            @property
            def in_force(self):
                return held
        return WithProperty()
    if kind == "generator":
        def paused():
            in_force = held  # noqa: F841 - the local variable is what the stand-in carries
            yield
        gen = paused()
        next(gen)
        return gen
    if kind == "single-string slot":
        class WithStringSlot:
            __slots__ = "held"

            def __init__(self, value):
                self.held = value
        return WithStringSlot(held)
    if kind == "private slot":
        class __WithPrivateSlot:
            __slots__ = ("__priv",)

            def __init__(self, value):
                self.__priv = value
        return __WithPrivateSlot(held)
    if kind == "dunder slot":
        class WithDunderSlot:
            __slots__ = ("__held__",)  # begins and ends with two underscores: Python does not mangle it

            def __init__(self, value):
                self.__held__ = value
        return WithDunderSlot(held)
    if kind == "slot of an underscore class":
        class ___:  # a class name made only of underscores: Python does not mangle a private name in it
            __slots__ = ("__priv",)

            def __init__(self, value):
                self.__priv = value
        return ___(held)
    raise AssertionError(kind)


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class WhatAPluginIsHanded(_Folder):
    """A stand-in plugin is loaded by the real plugin host; what it was handed is searched for the settings.

    The search is ``_held_of_settings``: its docstring says what a green result proves and what it cannot."""

    def hosted(self, host_class=None, leak=False):
        from recursion_core.daemon import breaker as daemon_breaker
        from recursion_core.daemon import plugins
        from recursion_core.daemon.tests import plugin_fixture as fx
        from recursion_core.plugin_security.tests.support import FakeGilProbe
        config, state = self.config / "recursion", self.tmp / "state"
        store, _ = daemon_breaker.open_breaker_store(state, clock=lambda: 0.0)
        self.addCleanup(store.close)
        fx.write_install(config, grants=[fx.grant_for(fx.OPERATOR_SOURCE)])
        fx.write_plugin(config / "plugins" / "one", fx.OPERATOR_SOURCE)

        def leaky(core, caller):  # a core that hands the plugin the settings in force, by the carrier named
            return _handed_over("closure" if leak is True else leak, settings.load(self.environ))

        patched = mock.patch.object(plugins.CoreService, "client_for", leaky) if leak else contextlib.nullcontext()
        with patched:
            host = (host_class or plugins.PluginHost)(config, state, store, gil_probe=FakeGilProbe(),
                                                      verification_available=lambda: True)
        self.addCleanup(host.close)
        kept = host._operators["fixture.operator"].__globals__["CORE"]  # what the stand-in's bind_core was handed
        self.assertEqual(1, len(kept))
        return host, kept[0]

    def test_nothing_of_the_settings_module_is_reachable_from_what_a_plugin_is_handed(self) -> None:
        self.put(OFF)
        _, handed = self.hosted()
        self.assertEqual([], _held_of_settings(handed))
        self.assertEqual([], _held_of_settings(((lambda: 1),)))  # the search finds nothing where there is nothing

    def test_the_search_finds_a_plugin_that_is_handed_the_settings(self) -> None:
        self.put(OFF)
        _, handed = self.hosted(leak=True)
        found = _held_of_settings(handed)
        self.assertTrue(found)
        self.assertEqual(["handed.closure[0]"], found)

    def test_the_search_finds_each_carrier_through_the_real_host(self) -> None:
        self.put(OFF)
        for kind in ("closure", "built-in method", "function attribute", "wraps", "class attribute", "property",
                     "generator", "single-string slot", "private slot", "dunder slot",
                     "slot of an underscore class"):
            with self.subTest(carrier=kind):
                _, handed = self.hosted(leak=kind)
                self.assertTrue(_held_of_settings(handed))

    def test_the_search_finds_each_carrier_in_a_plain_stand_in(self) -> None:
        held = settings.load(self.environ)
        for kind in ("closure", "built-in method", "function attribute", "wraps", "class attribute", "property",
                     "generator", "single-string slot", "private slot", "dunder slot",
                     "slot of an underscore class"):
            with self.subTest(carrier=kind):
                self.assertTrue(_held_of_settings(_handed_over(kind, held)))
        self.assertEqual([], _held_of_settings(_handed_over("closure", {"copy": "plain values"})))

    def test_the_search_names_the_slot_it_finds_where_python_does_not_mangle_the_name(self) -> None:
        held = settings.load(self.environ)
        # a slot spelled with two underscores at both ends is looked up under its own name
        self.assertEqual(["handed.__held__"], _held_of_settings(_handed_over("dunder slot", held)))
        # a private slot in a class whose name is only underscores is looked up under its own name too
        self.assertEqual(["handed.__priv"], _held_of_settings(_handed_over("slot of an underscore class", held)))
        # control: an ordinary private slot is found under the class's mangled name
        self.assertEqual(["handed._WithPrivateSlot__priv"], _held_of_settings(_handed_over("private slot", held)))

    def test_the_host_may_read_one_row_and_a_plugin_still_cannot_reach_the_settings(self) -> None:
        from recursion_core.daemon import plugins
        self.put(OFF)

        class ReadsItsRow(plugins.PluginHost):
            def __init__(host, *args, **kwargs):
                super().__init__(*args, **kwargs)
                host.gil_check = settings.load(os.environ)["plugin_host.gil_check"]

        with mock.patch.dict(os.environ, self.environ):
            host, handed = self.hosted(ReadsItsRow)
        self.assertIs(False, host.gil_check)
        self.assertEqual([], _held_of_settings(handed))

    def test_a_host_that_keeps_the_whole_settings_where_the_core_can_reach_them_is_found(self) -> None:
        from recursion_core.daemon import plugins
        self.put(OFF)

        class KeepsTheFile(plugins.PluginHost):
            def __init__(host, *args, **kwargs):
                super().__init__(*args, **kwargs)
                host.in_force = settings.load(os.environ)

        with mock.patch.dict(os.environ, self.environ):
            _, handed = self.hosted(KeepsTheFile)
        self.assertTrue(_held_of_settings(handed))


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class PublishedShape(_Folder):
    def test_the_schema_declares_exactly_the_table(self) -> None:
        schema = json.loads((SCHEMA_DIR / "settings.v1.json").read_text(encoding="utf-8"))
        version = schema["properties"]["version"]  # its description says what the schema cannot decide
        self.assertEqual({"const": settings.FILE_VERSION, "description": version.get("description")}, version)
        kinds = {bool: "boolean", int: "integer", float: "number", str: "string", list: "array"}
        found = {}

        def walk(props: dict, prefix: str) -> None:
            for key, part in props.items():
                if prefix == "" and key == "version":
                    continue
                if part.get("type") == "object":
                    walk(part["properties"], prefix + key + ".")
                else:
                    found[prefix + key] = (part["type"], part["default"], part["description"],
                                           part.get("minimum"), part.get("maximum"))

        walk(schema["properties"], "")
        expect = {r.name: (kinds[r.type], list(r.default) if r.type is list else r.default, r.says, r.floor, None)
                  for r in settings.SETTINGS}
        self.assertEqual(expect, found)

    def test_the_schema_describes_the_readers_version_type(self) -> None:
        schema = json.loads((SCHEMA_DIR / "settings.v1.json").read_text(encoding="utf-8"))
        self.assertIn("The settings file reader accepts version only as a TOML integer "
                      "(a whole number such as 1) and rejects a float such as 1.0.",
                      schema["description"])
        self.put(OFF.replace("version = 1", "version = 1.0"))
        loaded = settings.load(self.environ)
        self.assert_default(loaded, settings.UNREADABLE)
        self.assertIn("version must be 1, found 1.0", loaded.notes[0].detail)
        for wrong in ('"1"', "2", "true"):
            with self.subTest(version=wrong):
                self.put(OFF.replace("version = 1", f"version = {wrong}"))
                loaded = settings.load(self.environ)
                self.assert_default(loaded, settings.UNREADABLE)
                self.assertIn("version must be 1, found", loaded.notes[0].detail)
        self.put(OFF)
        self.assertEqual([], self.codes(settings.load(self.environ)))

    def test_the_readme_names_each_setting(self) -> None:
        readme = (SCHEMA_DIR / "README.md").read_text(encoding="utf-8")
        for row in settings.SETTINGS:
            self.assertIn(f"`{row.name}`", readme)

    def test_the_good_fixture_is_read_whole_and_the_bad_one_keeps_the_default(self) -> None:
        good = sorted((SCHEMA_DIR / "fixtures" / "good").glob("*.toml"))
        bad = sorted((SCHEMA_DIR / "fixtures" / "bad").glob("*.toml"))
        self.assertEqual((1, 2), (len(good), len(bad)))
        self.put(good[0].read_text(encoding="utf-8"))
        loaded = settings.load(self.environ)
        self.assertEqual([], self.codes(loaded))
        self.assertEqual({settings.SOURCE_FILE}, {v.source for v in loaded.values.values()})
        self.put(bad[0].read_text(encoding="utf-8"))
        self.assert_default(settings.load(self.environ), settings.OUT_OF_RANGE)
        self.put(bad[1].read_text(encoding="utf-8"))  # version = 1.0: the schema accepts it, the reader refuses it
        self.assert_default(settings.load(self.environ), settings.UNREADABLE)


CAP_KEY = "status.value_max_chars"
# Stand-ins built at run time, so that no text of this file has the shape of a key. (the text, its part that
# must never be shown)
KEY_LIKE = (
    ("s" + "k-proj-" + "Zq7Lm2Xc9Vb4Nn8Kk1Jj6Hh3", "Zq7Lm2Xc9Vb4Nn8Kk1Jj6Hh3"),
    ("gh" + "p_" + "a1b2c3d4e5f6g7h8", "a1b2c3d4e5f6g7h8"),
    ("AK" + "IA" + "QWERTYUIOP123456", "QWERTYUIOP123456"),
    ("-----" + "BEGIN OPENSSH PRIVATE KEY----- " + "b3BlbnNzaC1rZXktdjE", "b3BlbnNzaC1rZXktdjE"),
    ("pass" + "word = " + "hunter2hunter2", "hunter2hunter2"),
    ("Bear" + "er " + "eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ("Q2hhbmdl" + "IHRoaXMgYmVmb3JlIHVzZQ", "Q2hhbmdlIHRoaXMgYmVmb3JlIHVzZQ"),
)
ORDINARY = ("Ada", "plugin_host.gil_check", "csl-238-settings-no-limit-list-20261121", "/home/user2/projects/foo",
            "123e4567-e89b-12d3-a456-426614174000", "internationalization", "a token budget of 5", "")
class OneCapOnEveryShownValue(_Folder):
    """No value status, the start log or a note shows escapes the one cap, and no key-like text is shown."""

    def declared(self, *rows: settings.Setting):
        return mock.patch.multiple(settings, SETTINGS=rows,
                                   DECLARED=MappingProxyType({row.name: row for row in rows}))

    def test_the_two_lists_of_the_reproduction_stay_within_the_cap(self) -> None:
        three = settings._shown(("x" * 30,) * 3)
        name = "'" + "x" * 30 + "'"
        self.assertEqual(f"[{name}, {name}], and 1 more", three)
        sixty = settings._shown(tuple("y" * 30 for _ in range(60)))
        name = "'" + "y" * 30 + "'"
        self.assertEqual(f"[{name}], and 59 more", sixty)
        self.assertEqual((80, 47), (len(three), len(sixty)))

    def test_a_text_a_number_and_a_list_at_the_cap_and_one_past(self) -> None:
        self.assertEqual(repr("a" * 78), settings._shown("a" * 78))  # 80 characters with its quotes
        cut = settings._shown("a" * 79)
        self.assertEqual(repr("a" * 79)[:46] + "... (35 more characters not shown)", cut)
        self.assertEqual(80, len(cut))
        self.assertEqual(repr("a" * 500)[:45] + "... (457 more characters not shown)", settings._shown("a" * 500))
        self.assertEqual("9" * 80, settings._shown(int("9" * 80)))
        self.assertEqual("9" * 46 + "... (35 more characters not shown)", settings._shown(int("9" * 81)))
        b, c = "'" + "b" * 36 + "'", "'" + "c" * 36 + "'"
        self.assertEqual(f"[{b}, {c}]", settings._shown(("b" * 36, "c" * 36)))  # 2 + 38 + 2 + 38 = 80
        self.assertEqual(f"[{b}], and 1 more", settings._shown(("b" * 36, "c" * 37)))
        self.assertEqual("['" + "d" * 76 + "']", settings._shown(("d" * 76,)))
        self.assertEqual("a list of 1, too long to show", settings._shown(("d" * 77,)))
        self.assertEqual("... (1 more character not shown)", settings._shown(7, 0))

    def test_no_limit_shows_every_value_whole(self) -> None:
        for value in ("a" * 500, int("9" * 300), tuple("y" * 30 for _ in range(60))):
            with self.subTest(value=type(value).__name__):
                self.assertNotIn(" more", settings._shown(value, settings.NO_LIMIT))
        self.assertEqual(repr("a" * 500), settings._shown("a" * 500, settings.NO_LIMIT))
        self.assertEqual(2040, len(settings._shown(tuple("y" * 30 for _ in range(60)), settings.NO_LIMIT)))

    def test_every_kind_and_every_branch_passes_the_one_cap(self) -> None:
        import datetime
        values = (settings.NO_LIMIT, True, False, 7, -2.5, float("inf"), 10 ** 200, int("f" * 4000, 16),
                  "z" * 500, KEY_LIKE[0][0], (), ("Ada",) * 40, tuple("y" * 30 for _ in range(60)),
                  (("a" * 40, "b" * 40), ("c" * 40,)), ["x"] * 50, {"k": "v" * 100}, datetime.date(2026, 11, 24),
                  object())
        for cap in (0, 1, 5, 40, 80, 200):
            for value in values:
                with self.subTest(cap=cap, value=type(value).__name__):
                    shown = settings._shown(value, cap)
                    whole = settings._shown(value, settings.NO_LIMIT)
                    # within the cap; under a cap shorter than the count itself, only the count is shown
                    self.assertLessEqual(len(shown), max(cap, 40), shown)
                    self.assertEqual(len(whole) > cap, shown != whole)
                    if shown != whole:
                        self.assertRegex(shown, r"\(\d+ more characters? not shown\)$|, and \d+ more$|"
                                                r"^a list of \d+, too long to show$")

    def test_every_row_type_meets_the_cap_in_status(self) -> None:
        rows = (settings.DECLARED[CAP_KEY],
                settings.Setting("test.flag", bool, True, "A flag."),
                settings.Setting("test.limit", int, 10 ** 100, "A limit.", allow_no_limit=True),
                settings.Setting("test.span", float, 1.5, "A span."),
                settings.Setting("test.word", str, "w" * 300, "A word."),
                settings.Setting("test.names", list, tuple("n" * 30 for _ in range(60)), "Names."))
        for cap, cut in (("40", True), ("80", True), ('"none"', False)):
            with self.subTest(cap=cap):
                self.put(f"version = 1\n[status]\nvalue_max_chars = {cap}\n")
                with self.declared(*rows):
                    shown = settings.lines(settings.load(self.environ))
                self.assertEqual(len(rows), len(shown))
                for line in shown[2:3] + shown[4:]:  # the long whole number, text and list
                    self.assertEqual(cut, " more" in line or "too long to show" in line, line)
                if cut:
                    self.assertTrue(all(len(line) < 2 * 80 + 80 for line in shown), shown)
                else:
                    self.assertIn("w" * 300, shown[4])

    def test_the_one_cap_is_the_only_way_out_of_shown(self) -> None:
        tree = ast.parse(inspect.getsource(settings))
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        returns = [node for node in ast.walk(funcs["_shown"]) if isinstance(node, ast.Return)]
        self.assertEqual(1, len(returns))
        self.assertEqual("_cut", returns[0].value.func.id)
        users = {name for name, func in funcs.items() for node in ast.walk(func)
                 if isinstance(node, ast.Name) and node.id in ("_piece", "_cut")}
        self.assertEqual({"_shown", "_piece"}, users)  # _piece calls itself for a list inside a list
        outside = [sub.id for node in tree.body if not isinstance(node, ast.FunctionDef)
                   for sub in ast.walk(node) if isinstance(sub, ast.Name) and sub.id in ("_piece", "_cut", "repr")]
        self.assertEqual([], outside)
        texts = {name for name, func in funcs.items() for node in ast.walk(func)
                 if isinstance(node, ast.Name) and node.id == "repr"}
        self.assertEqual({"_piece"}, texts)  # no other function turns a value into text of its own

    def test_the_cap_is_a_row_of_the_one_file_with_80_and_no_limit(self) -> None:
        row = settings.DECLARED[CAP_KEY]
        self.assertIn(row, settings.SETTINGS)
        self.assertEqual((int, 80, 40, True), (row.type, row.default, row.floor, row.allow_no_limit))
        self.assertIn("none", row.says)

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_the_file_sets_the_cap_of_every_line_and_note(self) -> None:
        rows = (settings.DECLARED[CAP_KEY], settings.Setting("test.word", str, "w" * 50, "A word."),
                settings.Setting("test.limit", int, 5, "A limit."))
        word = "'wwwww... (46 more characters not shown)"
        self.put('version = 1\n[status]\nvalue_max_chars = 40\n[test]\nlimit = "' + "y" * 50 + '"\n')
        with self.declared(*rows):
            shown = settings.lines(settings.load(self.environ))
        self.assertEqual([f"{CAP_KEY} = 40 (default 80; source: settings file)",
                          f"test.word = {word} (default {word}; source: default)",
                          "test.limit = 5 (default 5; source: default)",
                          "note settings_value_out_of_range: test.limit: found 'yyyyy... (46 more characters not "
                          "shown); allowed: a whole number; the default 5 stays in force"], shown)
        self.put('version = 1\n[status]\nvalue_max_chars = "none"\n[test]\nlimit = "' + "y" * 50 + '"\n')
        with self.declared(*rows):
            shown = settings.lines(settings.load(self.environ))
        self.assertIn(repr("w" * 50), shown[1])
        self.assertIn(repr("y" * 50), shown[3])
        self.assertNotIn("not shown", "\n".join(shown))

    def test_a_text_that_looks_like_a_key_is_never_shown_even_in_part(self) -> None:
        for text, secret in KEY_LIKE:
            for value in (text, ("Ada", text), (("Ada", text),), "a note: " + text):
                for cap in (80, 10, settings.NO_LIMIT):
                    with self.subTest(text=text[:6], value=type(value).__name__, cap=cap):
                        shown = settings._shown(value, cap)
                        if cap is settings.NO_LIMIT:
                            self.assertIn("<hidden: looks like a key or password>", shown)
                        for i in range(len(secret) - 5):
                            self.assertNotIn(secret[i:i + 6], shown)

    def test_an_ordinary_text_is_shown(self) -> None:
        for text in ORDINARY:
            with self.subTest(text=text):
                self.assertEqual(repr(text), settings._shown(text, settings.NO_LIMIT))
                self.assertEqual(repr(text), settings._shown((text,), settings.NO_LIMIT)[1:-1])

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_key_in_the_file_is_in_no_line_of_status_or_a_note(self) -> None:
        text, secret = KEY_LIKE[0]
        rows = (settings.DECLARED[CAP_KEY], settings.Setting("test.word", str, "usual", "A word."),
                settings.Setting("test.limit", int, 5, "A limit."), settings.Setting("test.names", list, (), "Names."))
        self.put(f'version = 1\n[status]\nvalue_max_chars = "none"\n[test]\nword = "{text}"\nlimit = "{text}"\n'
                 f'names = ["Ada", "{text}"]\n')
        out = io.StringIO()
        with self.declared(*rows):
            self.assertEqual(0, settings.main([], self.environ, out, io.StringIO()))
        printed = out.getvalue()
        for i in range(len(secret) - 5):
            self.assertNotIn(secret[i:i + 6], printed)
        self.assertIn("test.names = ['Ada', <hidden: looks like a key or password>]", printed)
        self.assertEqual(3, printed.count("<hidden: looks like a key or password>"))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_note_says_the_floor_and_no_limit_in_what_is_allowed(self) -> None:
        row = settings.Setting("test.limit", int, 5, "A limit.", floor=1, allow_no_limit=True)
        for found, reason in (("0", "a value below 1 has no meaning; "), ("2.5", ""), ("true", "")):
            with self.subTest(found=found):
                self.put(f"version = 1\n[test]\nlimit = {found}\n")
                with self.declared(row):
                    loaded = settings.load(self.environ)
                self.assertEqual(f'test.limit: found {found}; {reason}allowed: a whole number of 1 or more, or "none" '
                                 "for no limit; the default 5 stays in force", loaded.notes[0].detail)


HIDDEN_KEY = "status.hidden_names"
SHOWN_NAMES_KEY = "status.shown_names"
SHOWN_KEY = "<hidden: looks like a key or password>"
SHOWN_NAME = "<hidden: name in status.hidden_names>"
# Made-up addresses, built at run time: (the text, its part that must never be shown, what a person reads)
SIGNED_IN = (
    ("postgres://" + "admin:" + "S3cr3tP4ss" + "@db/x", "S3cr3tP4ss", "'postgres://<sign-in hidden>@db/x'"),
    ("https://user:" + "hunter2pass" + "@example.com/", "hunter2pass", "'https://<sign-in hidden>@example.com/'"),
    ("connect to https://ada:" + "Pw9xQ" + "@example.com:8443/a and wait", "Pw9xQ",
     "'connect to https://<sign-in hidden>@example.com:8443/a and wait'"),
    ("admin:" + "S3cr3tP4ss" + "@db.example.com:5432/x", "S3cr3tP4ss", "'<sign-in hidden>@db.example.com:5432/x'"),
    ("https://" + "adalogin" + "@example.com", "adalogin", "'https://<sign-in hidden>@example.com'"),
    ("redis://u:" + "p@ss@w0rd" + "@cache:6379", "p@ss@w0rd", "'redis://<sign-in hidden>@cache:6379'"),
    ("https://u:" + "pa/ss" + "@host/x", "pa/ss", "'https://<sign-in hidden>@host/x'"),
    ("ftp://ada%3A" + "S3cr3t" + "@files", "S3cr3t", "'ftp://<sign-in hidden>@files'"),
)
NO_SIGN_IN = ("https://example.com:8443/path",)
# Everything before the last @ of a run is hidden: of a mail address only the part after the @ is shown.
BEFORE_THE_AT_SIGN = (("ada@example.com", "'<sign-in hidden>@example.com'"), ("a:b c@d", "'a:b <sign-in hidden>@d'"))
# A path that holds an @ after a host: from the scheme mark to the last @ is hidden, the host with it (the safe side).
PAST_THE_HOST = (("https://example.com/u@mail", "'https://<sign-in hidden>@mail'"),)
class NoSecretAndOneLineInEveryShownValue(_Folder):
    """A start line never shows a secret and never becomes two lines."""

    def declared(self, *rows: settings.Setting):
        return mock.patch.multiple(settings, SETTINGS=rows,
                                   DECLARED=MappingProxyType({row.name: row for row in rows}))

    def test_the_sign_in_of_an_address_is_never_shown_and_the_rest_is(self) -> None:
        for text, secret, whole in SIGNED_IN:
            for cap in (40, 80, settings.NO_LIMIT):
                for value in (text, ("Ada", text), (("Ada", text),)):
                    with self.subTest(text=whole, cap=cap, value=type(value).__name__):
                        shown = settings._shown(value, cap)
                        self.assertNotIn(secret, shown)
                        self.assertNotIn(secret[:4], shown)
            self.assertEqual(whole, settings._shown(text, settings.NO_LIMIT))
            self.assertEqual(f"['Ada', {whole}]", settings._shown(("Ada", text), settings.NO_LIMIT))
        for text in NO_SIGN_IN:
            with self.subTest(text=text):
                self.assertEqual(repr(text), settings._shown(text, settings.NO_LIMIT))
        for text, whole in PAST_THE_HOST + BEFORE_THE_AT_SIGN:
            with self.subTest(text=text):
                self.assertEqual(whole, settings._shown(text, settings.NO_LIMIT))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_an_address_in_the_file_is_shown_without_its_sign_in_in_every_line(self) -> None:
        text, secret, whole = SIGNED_IN[0]
        rows = (settings.DECLARED[CAP_KEY], settings.Setting("test.word", str, "usual", "A word."),
                settings.Setting("test.limit", int, 5, "A limit."), settings.Setting("test.names", list, (), "Names."))
        for cap in ("80", '"none"'):
            with self.subTest(cap=cap):
                self.put(f'version = 1\n[status]\nvalue_max_chars = {cap}\n[test]\nword = "{text}"\nlimit = "{text}"\n'
                         f'names = ["Ada", "{text}"]\n')
                out = io.StringIO()
                with self.declared(*rows):
                    self.assertEqual(0, settings.main([], self.environ, out, io.StringIO()))
                printed = out.getvalue()
                self.assertNotIn(secret, printed)
                self.assertIn(f"test.word = {whole} (default 'usual'; source: settings file)", printed)
                self.assertIn(f"test.names = ['Ada', {whole}]", printed)
                self.assertIn(f"test.limit: found {whole}; allowed: a whole number;", printed)

    def test_the_hidden_names_are_a_row_with_the_shipped_words(self) -> None:
        row = settings.DECLARED[HIDDEN_KEY]
        self.assertIn(row, settings.SETTINGS)
        self.assertEqual((list, None), (row.type, row.floor))
        for word in ("key", "keys", "token", "tokens", "secret", "secrets", "password", "passwords", "passwd",
                     "credential", "credentials"):
            self.assertIn(word, row.default)
        for name, hidden in (("a.api_key", True), ("a.sessionToken", True), ("db.PASSWORD", True),
                             ("a.secret-path", True), ("a.monkey", False), ("a.keyring", False),
                             ("a.tokenizer", False), (CAP_KEY, False), (HIDDEN_KEY, False)):
            with self.subTest(name=name):
                self.assertEqual(hidden, settings._hidden_name(name, row.default))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_value_under_a_hidden_name_is_never_shown_whatever_it_looks_like(self) -> None:
        rows = (settings.DECLARED[CAP_KEY], settings.DECLARED[HIDDEN_KEY],
                settings.Setting("test.api_key", str, "plain default", "A key."),
                settings.Setting("test.sessionToken", int, 7, "A number."),
                settings.Setting("test.tokens", list, ("ada",), "Names."),
                settings.Setting("test.monkey", str, "plain monkey", "A word."))
        self.put('version = 1\n[status]\nvalue_max_chars = "none"\n[test]\napi_key = "plainword"\n'
                 'sessionToken = "eleven"\ntokens = ["lin"]\nmonkey = "plainword too"\n')
        with self.declared(*rows):
            shown = settings.lines(settings.load(self.environ))
        self.assertEqual([f"test.api_key = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)",
                          f"test.sessionToken = {SHOWN_NAME} (default {SHOWN_NAME}; source: default)",
                          f"test.tokens = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)",
                          "test.monkey = 'plainword too' (default 'plain monkey'; source: settings file)",
                          f"note settings_value_out_of_range: test.sessionToken: found {SHOWN_NAME}; allowed: a "
                          f"whole number; the default {SHOWN_NAME} stays in force"], shown[2:])
        for word in ("'plainword'", "plain default", "eleven", "'lin'", "'ada'"):
            self.assertNotIn(word, "\n".join(shown))
        # the file's words are added to the shipped ones, and are read before any note is written
        self.put('version = 1\n[status]\nhidden_names = ["monkey"]\n[test]\napi_key = 7\nmonkey = 8\n')
        with self.declared(*rows):
            shown = settings.lines(settings.load(self.environ))
        self.assertIn(f"test.api_key = {SHOWN_NAME} (default {SHOWN_NAME}; source: default)", shown)
        self.assertIn(f"note settings_value_out_of_range: test.monkey: found {SHOWN_NAME}; allowed: a text; the "
                      f"default {SHOWN_NAME} stays in force", shown)
        self.assertIn(f"note settings_value_out_of_range: test.api_key: found {SHOWN_NAME}; allowed: a text; "
                      f"the default {SHOWN_NAME} stays in force", shown)

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_persons_words_are_added_to_the_shipped_ones_which_stay(self) -> None:
        rows = (settings.DECLARED[CAP_KEY], settings.DECLARED[HIDDEN_KEY],
                settings.Setting("test.api_key", str, "plain default", "A key."),
                settings.Setting("test.passphrase", str, "plain phrase", "A phrase."),
                settings.Setting("test.monkey", str, "plain monkey", "A word."))
        shipped = settings.DECLARED[HIDDEN_KEY].default
        self.put('version = 1\n[status]\nvalue_max_chars = "none"\nhidden_names = ["passphrase", "key"]\n[test]\n'
                 'api_key = "plainword"\npassphrase = "plainphrase"\nmonkey = "plainmonkey"\n')
        with self.declared(*rows):
            loaded = settings.load(self.environ)
            shown = settings.lines(loaded)
        self.assertEqual(shipped + ("passphrase",), loaded[HIDDEN_KEY])
        self.assertEqual(settings.SOURCE_FILE, loaded.values[HIDDEN_KEY].source)
        self.assertEqual([], self.codes(loaded))
        self.assertIn(f"test.api_key = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)", shown)
        self.assertIn(f"test.passphrase = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)", shown)
        self.assertIn("test.monkey = 'plainmonkey' (default 'plain monkey'; source: settings file)", shown)
        for word in ("plainword", "plain default", "plainphrase", "plain phrase"):
            self.assertNotIn(word, "\n".join(shown))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_an_empty_or_missing_list_keeps_the_shipped_words_alone(self) -> None:
        rows = (settings.DECLARED[CAP_KEY], settings.DECLARED[HIDDEN_KEY],
                settings.Setting("test.api_key", str, "plain default", "A key."),
                settings.Setting("test.apiToken", str, "plain token", "A token."))
        shipped = settings.DECLARED[HIDDEN_KEY].default
        for hidden, source in (("hidden_names = []\n", settings.SOURCE_FILE), ("", settings.SOURCE_DEFAULT)):
            with self.subTest(hidden=hidden):
                self.put(f'version = 1\n[status]\n{hidden}[test]\napi_key = "plainword"\napiToken = "plainword"\n')
                with self.declared(*rows):
                    loaded = settings.load(self.environ)
                    shown = "\n".join(settings.lines(loaded))
                self.assertEqual((shipped, source), (loaded[HIDDEN_KEY], loaded.values[HIDDEN_KEY].source))
                self.assertEqual([], self.codes(loaded))
                for word in ("plainword", "plain default", "plain token"):
                    self.assertNotIn(word, shown)
                self.assertEqual(2, shown.count(f" = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)"))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_name_in_shown_names_is_shown_and_every_start_says_so(self) -> None:
        hidden_row, shown_row = settings.DECLARED[HIDDEN_KEY], settings.DECLARED[SHOWN_NAMES_KEY]
        self.assertIn(shown_row, settings.SETTINGS)
        self.assertEqual((list, ()), (shown_row.type, shown_row.default))
        for said in ("status.hidden_names", "empty as shipped", "says so in a note", "stay hidden"):
            self.assertIn(said, shown_row.says)
        for said in ("added to these shipped words", "no settings file can take away", "an empty list keeps",
                     "status.shown_names"):
            self.assertIn(said, hidden_row.says)
        rows = (settings.DECLARED[CAP_KEY], hidden_row, shown_row,
                settings.Setting("test.api_key", str, "plain default", "A key."),
                settings.Setting("test.apiToken", str, "plain token", "A token."))
        said = ("note settings_shown_anyway: the value of each setting named in status.shown_names is shown even "
                "where its name holds a word of status.hidden_names: ['test.api_key']; a text that looks like a key "
                "or password and the sign-in of an address stay hidden; take a name out of status.shown_names to "
                "hide its value again")
        self.put('version = 1\n[status]\nvalue_max_chars = "none"\nshown_names = ["test.api_key"]\n[test]\n'
                 'api_key = "plainword"\napiToken = "plainword too"\n')
        out = io.StringIO()
        with self.declared(*rows):
            loaded = settings.load(self.environ)
            shown = settings.lines(loaded)
            self.assertEqual(0, settings.main([], self.environ, out, io.StringIO()))
        self.assertEqual([settings.SHOWN_ANYWAY], self.codes(loaded))
        self.assertIn("test.api_key = 'plainword' (default 'plain default'; source: settings file)", shown)
        self.assertIn(f"test.apiToken = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)", shown)
        self.assertNotIn("plainword too", "\n".join(shown))
        self.assertEqual(said, shown[-1])
        self.assertIn(said, out.getvalue().splitlines())
        # under a name shown anyway, the sign-in of an address stays hidden
        text, secret, whole = SIGNED_IN[0]
        self.put('version = 1\n[status]\nshown_names = ["test.api_key"]\n[test]\n'
                 f'api_key = "{text}"\n')
        with self.declared(*rows):
            shown = settings.lines(settings.load(self.environ))
        self.assertIn(f"test.api_key = {whole} (default 'plain default'; source: settings file)", shown)
        self.assertNotIn(secret, "\n".join(shown))
        self.assertEqual(said, shown[-1])

    def test_a_hidden_text_does_not_say_its_length(self) -> None:
        for text, _ in KEY_LIKE:
            for value in (text, text * 3, ("Ada", text)):
                with self.subTest(text=text[:6], value=type(value).__name__):
                    shown = settings._shown(value, settings.NO_LIMIT)
                    self.assertIn(SHOWN_KEY, shown)
                    self.assertNotIn(str(len(text)), shown)

    def test_the_look_alike_rule_is_a_second_net_and_the_words_say_so(self) -> None:
        for text in ("x." + "sk-" + "abcdefgh12", "(" + "sk-" + "abcdefgh)", "a.b." + "gh" + "p_" + "a1b2c3d4e5"):
            with self.subTest(text=text):
                self.assertEqual(SHOWN_KEY, settings._shown(text, settings.NO_LIMIT))
        readme = (SCHEMA_DIR / "README.md").read_text(encoding="utf-8")
        for words in (settings.__doc__, readme):
            for said in ("second net", "a short key", "status.hidden_names", "<sign-in hidden>"):
                self.assertIn(said, words)

    def test_no_value_can_break_or_disguise_a_line(self) -> None:
        class Text(str):
            def __repr__(self):
                return "1\nplugin_host.gil_check = true"

            def __str__(self):
                return "2\nplugin_host.gil_check = true"

        class Number(int):
            def __repr__(self):
                return "3\nplugin_host.gil_check = true"

        odd = type("Odd\nplugin_host.gil_check = true", (), {})
        values = (Text("ada"), Number(5), odd(), "a\nb", "a\rb", "abc\u202edcba", "a\u2066b\u2069", "a\x1b[2Jb",
                  "a\u2028b", "a\x85b", "a\x00b", "a\tb", "\ufeffa", ("Ada", "a\nb"))
        for i, value in enumerate(values):
            for cap in (40, settings.NO_LIMIT):
                with self.subTest(i=i, cap=cap):
                    shown = settings._shown(value, cap)
                    self.assertTrue(shown.isprintable(), ascii(shown))
        self.assertEqual("'ada'", settings._shown(Text("ada"), settings.NO_LIMIT))
        self.assertEqual("5", settings._shown(Number(5), settings.NO_LIMIT))
        self.assertEqual("'abc\\u202edcba'", settings._shown("abc\u202edcba", settings.NO_LIMIT))
        word = settings.Value("test.word", Text("ada"), odd(), settings.SOURCE_DEFAULT)
        loaded = settings.Loaded("/x", MappingProxyType({"test.word": word}), (settings.Note("x", "a\nb\u202ec"),))
        self.assertEqual(["test.word = 'ada' (default a Odd\\nplugin_host.gil_check = true; source: default)",
                          "note x: a\\nb\\u202ec"], settings.lines(loaded))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_the_settings_file_line_is_one_line(self) -> None:
        environ = dict(self.environ, XDG_CONFIG_HOME=self.environ["XDG_CONFIG_HOME"] + "/a\nb")
        out = io.StringIO()
        self.assertEqual(0, settings.main([], environ, out, io.StringIO()))
        first = out.getvalue().splitlines()[0]
        self.assertTrue(first.startswith("settings file: "), first)
        self.assertTrue(first.endswith("/a\\nb/recursion/settings.toml"), first)

    def test_at_its_least_the_cap_holds_every_word_that_says_what_is_cut_or_hidden(self) -> None:
        row = settings.DECLARED[CAP_KEY]
        self.assertEqual(40, row.floor)
        self.assertIn("The least is 40", row.says)
        values = ("a" * 300000, "\x00" * 70000, int("9" * 4000), tuple(str(i) for i in range(100000)),
                  tuple("n" * 60 for _ in range(100000)), KEY_LIKE[0][0], SIGNED_IN[0][0], settings.NO_LIMIT)
        for i, value in enumerate(values):
            with self.subTest(i=i):
                shown = settings._shown(value, row.floor)
                self.assertLessEqual(len(shown), row.floor, shown)
        for words in (SHOWN_KEY, SHOWN_NAME, settings._shown("x", row.floor, True)):
            self.assertLessEqual(len(words), row.floor)
        self.assertEqual(SHOWN_NAME, settings._shown("x", row.floor, True))

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_cap_below_its_least_keeps_the_default_and_says_why(self) -> None:
        self.put("version = 1\n[status]\nvalue_max_chars = 39\n")
        loaded = settings.load(self.environ)
        self.assertEqual(80, loaded[CAP_KEY])
        self.assertEqual('status.value_max_chars: found 39; a value below 40 has no meaning; allowed: a whole '
                         'number of 40 or more, or "none" for no limit; the default 80 stays in force',
                         loaded.notes[0].detail)

    @unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
    def test_a_cut_key_carries_a_digest_of_its_whole_text(self) -> None:
        one, two = "k" * 99 + "a", "k" * 99 + "b"
        details = []
        for name in (one, two, f'"x.{one}"', f'"x.{two}"'):
            with self.subTest(name=name[-4:]):
                self.put(f"version = 1\n{name} = 1\n")
                details.append(settings.load(self.environ).notes[0].detail)
                self.assertRegex(details[-1], r"^'(x\.)?k+\.\.\. \(\d+ more, digest [0-9a-f]{8}\) is ")
        self.assertEqual(4, len(set(details)))
        big = "k" * 60000
        self.put(f"version = 1\n{big} = 1\n")
        out = io.StringIO()
        self.assertEqual(settings.EXIT_WARNED, settings.main([], self.environ, out, io.StringIO()))
        notes = [line for line in out.getvalue().splitlines() if "not a setting" in line]
        self.assertEqual(1, len(notes))
        words = " is not a setting this version knows; it is skipped; remove it or check its spelling"
        self.assertLessEqual(len(notes[0]), len("warning: note settings_unknown_key: ") + 80 + len(words), notes[0])
        self.assertLessEqual(len(settings._shown(big * 5, 40, key=True)), 40)
        self.put("version = 1\nkkk = 1\n")
        detail = settings.load(self.environ).notes[0].detail
        self.assertTrue(detail.startswith("'kkk' is not a setting this version knows; it is skipped"), detail)
        self.assertNotIn("digest", detail)
        self.put(f'version = 1\n{one} = 1\n[status]\nvalue_max_chars = "none"\n')
        detail = settings.load(self.environ).notes[0].detail
        self.assertTrue(detail.startswith(f"'{one}' is not a setting this version knows"), detail)
        self.assertNotIn("digest", detail)


class WindowsNoOwnerCheck(_Folder):
    """(7) Where ``os`` has no ``geteuid`` (Windows), ``load`` reads no file and every default is in force."""

    def test_no_uid_means_no_file_is_read_and_the_note_says_so(self) -> None:
        self.file.write_text(OFF, encoding="utf-8")
        saved = getattr(os, "geteuid", None)
        if saved is not None:
            del os.geteuid
            self.addCleanup(setattr, os, "geteuid", saved)
        with mock.patch.object(os, "lstat", side_effect=AssertionError("a file was looked at")), \
                mock.patch.object(os, "open", side_effect=AssertionError("a file was opened")):
            loaded = settings.load(self.environ)
        self.assertIsNone(loaded.path)
        self.assert_default(loaded, settings.NOT_READ_HERE)
        self.assertIn("note settings_not_read_on_this_system", "\n".join(settings.lines(loaded)))

    def test_no_uid_prints_the_placeholder_sentence_and_the_exit_is_unchanged(self) -> None:
        self.file.write_text(OFF, encoding="utf-8")
        saved = getattr(os, "geteuid", None)
        if saved is not None:
            del os.geteuid
            self.addCleanup(setattr, os, "geteuid", saved)
        out = io.StringIO()
        self.assertEqual(0, settings.main([], environ=self.environ, out=out, err=io.StringIO()))
        shown = out.getvalue()
        self.assertIn("`journalctl _SYSTEMD_USER_UNIT=recursion-daemon.service _SYSTEMD_OWNER_UID=<your user id>`", shown)
        self.assertIn("put in place of <your user id> the number `id -u` prints", shown)
        self.assertNotRegex(shown, r"_SYSTEMD_OWNER_UID=[0-9]")
        self.assertIn("note settings_not_read_on_this_system", shown)


# The spelling the pinned-copy plan prints for a person: a table and a key under it.
PLAN_SPELLING = 'version = 1\n\n[engines.claude_code]\npinned_version = "2.1.325"\n'
FULL_NAME = "engines.claude_code.pinned_version"
OTHER_SPELLINGS = {
    "the dotted key at the top level": f'version = 1\n{FULL_NAME} = "2.1.325"\n',
    "[engines] and a dotted key under it": 'version = 1\n\n[engines]\nclaude_code.pinned_version = "2.1.325"\n',
}


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class ATableAndAKeyReadUnderTheFullDottedName(_Folder):
    """A setting written as a table and a key is read under its full dotted name. The row of that name is made
    known to the reader for each test alone; the product's table is put back after it. A known table written as a
    table and applied with its source is test_a_value_in_the_file_is_in_force_with_its_source (OFF)."""

    def with_the_row(self) -> None:
        row = settings.Setting(FULL_NAME, str, "", "A row known to the reader for this test only.")
        rows = (*settings.SETTINGS, row)
        for name, value in (("SETTINGS", rows), ("DECLARED", MappingProxyType({r.name: r for r in rows}))):
            patcher = mock.patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self, text: str):
        self.put(text)
        return settings.load(self.environ)

    def test_the_plans_printed_spelling_and_the_two_others_give_the_value_under_the_full_name(self) -> None:
        self.with_the_row()
        for case, text in {"the plan's printed spelling": PLAN_SPELLING, **OTHER_SPELLINGS}.items():
            with self.subTest(case=case):
                loaded = self.read(text)
                self.assertEqual("2.1.325", loaded[FULL_NAME])
                self.assertEqual(settings.SOURCE_FILE, loaded.values[FULL_NAME].source)
                self.assertEqual([], self.codes(loaded))
                self.assertIn(f"{FULL_NAME} = '2.1.325' (default ''; source: settings file)", settings.lines(loaded))

    def test_the_quoted_spelling_is_not_read_as_that_name(self) -> None:
        self.with_the_row()
        loaded = self.read('version = 1\n"engines.claude_code".pinned_version = "2.1.325"\n')
        self.assertEqual("", loaded[FULL_NAME])
        self.assertEqual(settings.SOURCE_DEFAULT, loaded.values[FULL_NAME].source)
        self.assertEqual([settings.UNKNOWN_KEY], self.codes(loaded))
        self.assertIn("'engines.claude_code' is a quoted key that holds a dot, not a setting", loaded.notes[0].detail)

    def test_without_the_row_each_spelling_is_reported_by_the_table_name_and_nothing_is_applied(self) -> None:
        self.assertNotIn(FULL_NAME, settings.DECLARED)  # the product's table is back as it was
        for case, text in {"the plan's printed spelling": PLAN_SPELLING, **OTHER_SPELLINGS}.items():
            with self.subTest(case=case):
                loaded = self.read(text)
                self.assert_default(loaded, settings.UNKNOWN_KEY)
                self.assertNotIn(FULL_NAME, loaded.values)
                self.assertEqual("'engines' is not a setting this version knows; it is skipped; remove it or check "
                                 "its spelling", loaded.notes[0].detail)



@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class TheStartLinesWhateverLoggingHeldBefore(_Folder):
    """A root logger an embedding caller or a test host set up first: the daemon's start lines still carry the
    time and the process id and are written at INFO, and the caller's own handler keeps receiving them."""

    def start(self, err: io.StringIO) -> None:
        from recursion_core.daemon import __main__ as daemon_main
        with mock.patch.dict(os.environ, self.environ), \
                mock.patch.object(sys, "stderr", err), \
                mock.patch.object(daemon_main, "require_posix"), \
                mock.patch.object(daemon_main, "require_free_threaded"), \
                mock.patch.object(daemon_main, "refuse_engine_user"), \
                mock.patch.object(daemon_main, "check_operator_linger"), \
                mock.patch.object(daemon_main.signal, "signal"), \
                mock.patch.object(daemon_main, "UserLock"), \
                mock.patch.object(daemon_main, "Registry", side_effect=_ReachedTheLock), \
                self.assertRaises(_ReachedTheLock):
            daemon_main.main()

    def test_a_root_handler_set_up_first_at_info_or_warning_leaves_both_lines_with_time_and_process_id(self) -> None:
        self.put(OFF)
        root, own = logging.getLogger(), logging.getLogger("recursion_core.daemon")
        kept = (root.handlers[:], root.level, own.handlers[:], own.level)

        def restore() -> None:
            root.handlers[:], own.handlers[:] = kept[0], kept[2]
            root.setLevel(kept[1])
            own.setLevel(kept[3])
        self.addCleanup(restore)
        form = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[" + str(os.getpid()) + r"\] INFO recursion_core\.daemon: "
        starts = (f"settings file: {self.file}",
                  "settings: plugin_host.gil_check = false (default true; source: settings file)")
        for level in (logging.INFO, logging.WARNING):
            caller = io.StringIO()
            bare = logging.StreamHandler(caller)
            bare.setFormatter(logging.Formatter("%(message)s"))
            root.handlers[:] = [bare]
            root.setLevel(level)
            for attempt in ("a first start", "a second start in the same process"):
                with self.subTest(root=logging.getLevelName(level), attempt=attempt):
                    err = io.StringIO()
                    self.start(err)
                    shown = err.getvalue().splitlines()
                    for start in starts:
                        found = [line for line in shown if line.endswith(start)]
                        self.assertEqual(1, len(found), shown)
                        self.assertRegex(found[0], form)
            self.assertIn(starts[0], caller.getvalue().splitlines())  # the caller's own handler is kept



@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class AKeyIsShownWhole(_Folder):
    """A key is capped in a note like every shown value, and a cut key carries a short digest of its whole text,
    so two long keys that differ late read apart; under "none" a key is named whole."""

    def test_two_unknown_keys_of_100_characters_that_differ_after_character_90_give_two_notes(self) -> None:
        first, second = "a" * 100, "a" * 90 + "b" * 10
        self.put(f"version = 1\n[plugin_host]\n{first} = false\n{second} = false\n")
        details = [note.detail for note in settings.load(self.environ).notes]
        self.assertEqual(2, len(set(details)), details)
        for detail in details:  # cut at the cap, a digest of the whole key, and the words that say what to do
            self.assertRegex(detail, r"^'plugin_host\.a+\.\.\. \(\d+ more, digest [0-9a-f]+\) is not a setting this "
                                     r"version knows; it is skipped; remove it or check its spelling$")
        self.put(f'version = 1\n[plugin_host]\n{first} = false\n{second} = false\n[status]\nvalue_max_chars = "none"\n')
        details = [note.detail for note in settings.load(self.environ).notes]
        for key, detail in zip((first, second), details):  # under "none" the whole key
            self.assertTrue(detail.startswith(f"'plugin_host.{key}' is not a setting this version knows; it is "
                                              "skipped; remove it or check its spelling"), detail)

    def test_two_long_quoted_keys_that_differ_late_give_two_notes_with_the_whole_key(self) -> None:
        first, second = "a" * 90 + ".b", "a" * 90 + ".c"
        self.put(f'version = 1\n"{first}" = 1\n"{second}" = 1\n')
        details = [note.detail for note in settings.load(self.environ).notes]
        self.assertEqual(2, len(set(details)), details)
        for detail in details:  # cut at the cap, with a digest of the whole key
            self.assertRegex(detail, r"^'a+\.\.\. \(\d+ more, digest [0-9a-f]+\) is a quoted key that holds a dot")
        self.put(f'version = 1\n"{first}" = 1\n"{second}" = 1\n[status]\nvalue_max_chars = "none"\n')
        details = [note.detail for note in settings.load(self.environ).notes]
        for key, detail in zip((first, second), details):  # under "none" the whole key
            self.assertTrue(detail.startswith(f"'{key}' is a quoted key that holds a dot"), detail)

    def test_the_note_for_an_unknown_key_says_what_to_do_and_keeps_the_nearest_known_key(self) -> None:
        self.put("version = 1\n[plugin_host]\ngil_chek = false\n")
        self.assertEqual("'plugin_host.gil_chek' is not a setting this version knows; it is skipped; remove it or "
                         "check its spelling; did you mean plugin_host.gil_check?",
                         settings.load(self.environ).notes[0].detail)

    def test_a_value_is_still_cut_in_its_note(self) -> None:
        self.put('version = 1\n[plugin_host]\ngil_check = "' + "x" * 200 + '"\n')
        detail = settings.load(self.environ).notes[0].detail
        self.assertNotIn("x" * 200, detail)
        self.assertIn("...", detail)



@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class TheVersionTheSchemaCannotTell(_Folder):
    """JSON Schema compares numbers by value, so the schema cannot refuse version 1.0; it says so, and the reader
    (which takes only the whole number 1) is what refuses it, with its own words, in a published bad vector."""

    SAYS = "JSON Schema cannot tell 1.0 from 1, so this schema accepts version 1.0; the reader takes only the whole number 1"

    def test_the_schema_accepts_1_0_on_purpose_and_its_version_says_the_reader_takes_only_1(self) -> None:
        schema = json.loads((SCHEMA_DIR / "settings.v1.json").read_text(encoding="utf-8"))
        version = schema["properties"]["version"]
        self.assertEqual(version["const"], json.loads("1.0"))  # a parsed 1.0 meets const 1: accepted on purpose
        self.assertIn(self.SAYS, version.get("description", ""))
        readme = " ".join((SCHEMA_DIR / "README.md").read_text(encoding="utf-8").split())
        self.assertIn(self.SAYS, readme)

    def test_the_bad_vector_version_1_0_is_refused_by_the_reader_with_its_words(self) -> None:
        vector = SCHEMA_DIR / "fixtures" / "bad" / "b02-version-written-as-a-float.toml"
        self.assertTrue(vector.is_file(), vector)
        self.put(vector.read_text(encoding="utf-8"))
        loaded = settings.load(self.environ)
        self.assert_default(loaded, settings.UNREADABLE)
        self.assertIn("version must be 1, found 1.0; write `version = 1`", loaded.notes[0].detail)


def _sha(text: str, chars: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:chars]


START_FORM = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[" + str(os.getpid()) + r"\] (INFO|WARNING) recursion_core\.daemon: "
UNKNOWN_WORDS = " is not a setting this version knows; it is skipped; remove it or check its spelling"


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class AKeyIsCappedAndACutKeyCarriesItsDigest(_Folder):
    """The two rounds joined: a key is capped like every shown value and a cut key carries the first characters
    of the SHA-256 of its whole text (the row status.digest_chars); a key that looks like a key carries its
    ordinal only; each start line keeps its time and process id, once per start, on the stderr of the moment."""

    def start(self, err: io.StringIO) -> None:
        TheStartLinesWhateverLoggingHeldBefore.start(self, err)

    def bare_logging(self, handlers=()) -> None:
        root, own = logging.getLogger(), logging.getLogger("recursion_core.daemon")
        kept = (root.handlers[:], root.level, own.handlers[:], own.level)

        def restore() -> None:
            root.handlers[:], own.handlers[:] = kept[0], kept[2]
            root.setLevel(kept[1])
            own.setLevel(kept[3])
        self.addCleanup(restore)
        root.handlers[:] = list(handlers)
        own.handlers[:] = []

    def test_the_join_two_long_keys_read_apart_a_hidden_value_shows_nothing_and_each_start_line_keeps_time_and_pid(self) -> None:
        # One test that is red when either half is cut: the start lines' time and process id, or the cap's
        # digest of a cut key and its hidden value.
        first, second = "a" * 100, "a" * 90 + "b" * 10
        secret = "plainsecret14c"
        row = settings.Setting("test.api_key", str, "plain default", "A key.")
        rows = settings.SETTINGS + (row,)
        self.put(f'version = 1\n{first} = 1\n{second} = 1\n[test]\napi_key = "{secret}"\n')
        self.bare_logging()
        err = io.StringIO()
        with mock.patch.multiple(settings, SETTINGS=rows, DECLARED=MappingProxyType({r.name: r for r in rows})):
            self.start(err)
        shown = err.getvalue().splitlines()
        mine = [line for line in shown if "settings file: " in line or "settings: " in line]
        self.assertTrue(mine, shown)
        for line in mine:
            self.assertRegex(line, START_FORM)
        notes = [line for line in mine if UNKNOWN_WORDS in line]
        self.assertEqual(2, len(notes), mine)
        self.assertEqual(2, len(set(notes)))
        for key, line in zip((first, second), notes):
            self.assertIn(f"digest {_sha(key)})", line)
            self.assertNotIn(key, line)
        valued = [line for line in mine if "test.api_key = " in line]
        self.assertEqual(1, len(valued), mine)
        self.assertIn(f"test.api_key = {SHOWN_NAME} (default {SHOWN_NAME}; source: settings file)", valued[0])
        self.assertNotIn(secret, err.getvalue())
        self.assertNotIn(str(len(secret)), valued[0].split("recursion_core.daemon: ", 1)[1])

    def test_two_long_keys_that_differ_past_the_cap_read_apart_under_every_cap_and_whole_under_none(self) -> None:
        first, second = "a" * 100, "a" * 90 + "b" * 10
        for cap in [*range(40, 131), '"none"']:
            with self.subTest(cap=cap):
                self.put(f"version = 1\n{first} = 1\n{second} = 1\n[status]\nvalue_max_chars = {cap}\n")
                details = [note.detail for note in settings.load(self.environ).notes]
                self.assertEqual(2, len(set(details)), details)
                for key, detail in zip((first, second), details):
                    head = detail[:-len(UNKNOWN_WORDS)]
                    self.assertTrue(detail.endswith(UNKNOWN_WORDS), detail)
                    if cap == '"none"' or len(repr(key)) <= cap:
                        self.assertEqual(repr(key), head)
                    else:
                        self.assertLessEqual(len(head), cap)
                        self.assertTrue(head.endswith(f" more, digest {_sha(key)})"), head)
        big = "k" * 60000
        self.put(f"version = 1\n{big} = 1\n")
        detail = settings.load(self.environ).notes[0].detail
        self.assertTrue(detail.endswith(UNKNOWN_WORDS), detail)
        self.assertLessEqual(len(detail) - len(UNKNOWN_WORDS), 80)  # the cap plus the note's own words
        self.assertIn(f"digest {_sha(big)})", detail)

    def test_the_digest_length_is_the_row_status_digest_chars(self) -> None:
        key = "k" * 199 + "a"  # long enough to be cut under a cap of 160, which leaves room for 64 hex characters
        self.put(f"version = 1\n{key} = 1\n")
        loaded = settings.load(self.environ)
        self.assertIn("status.digest_chars = 8 (default 8; source: default; advised: at most 64)",
                      settings.lines(loaded))
        self.assertIn(f"digest {_sha(key)})", loaded.notes[0].detail)
        for chars, shown in ((6, 6), (12, 12), (64, 64), (65, 64), (1000, 64)):
            with self.subTest(chars=chars):
                self.put(f"version = 1\n{key} = 1\n[status]\ndigest_chars = {chars}\nvalue_max_chars = 160\n")
                loaded = settings.load(self.environ)
                self.assertEqual(chars, loaded["status.digest_chars"])
                self.assertRegex(loaded.notes[0].detail, r"digest " + _sha(key, shown) + r"\)")
                outside = "; outside the advice" if chars > 64 else ""
                self.assertIn(f"status.digest_chars = {chars} (default 8; source: settings file; advised: at most 64"
                              f"{outside})", settings.lines(loaded))
        for found, why in (("5", "found 5; a value below 6 has no meaning; "), ('"none"', "found 'none'; ")):
            with self.subTest(found=found):
                self.put(f"version = 1\n{key} = 1\n[status]\ndigest_chars = {found}\n")
                loaded = settings.load(self.environ)
                self.assertEqual(8, loaded["status.digest_chars"])
                self.assertIn(f"status.digest_chars: {why}allowed: a whole number of 6 or more; the default 8 "
                              "stays in force", [note.detail for note in loaded.notes])
                self.assertIn(f"digest {_sha(key)})", loaded.notes[0].detail)

    def test_a_key_that_looks_like_a_key_carries_its_ordinal_only_equal_between_two_starts(self) -> None:
        short, long_ = "ghp_" + "A1b2" * 10, "ghp_" + "C3d4" * 30
        text = f"version = 1\n{short} = 1\nplain = 1\n{long_} = 1\n"
        self.put(text)
        once = [note.detail for note in settings.load(self.environ).notes]
        twice = [note.detail for note in settings.load(self.environ).notes]
        self.assertEqual(once, twice)  # two starts of the same file show the same lines
        hidden = [d for d in once if d.startswith("<hidden key-like name")]
        self.assertEqual(2, len(hidden), once)
        self.assertNotEqual(hidden[0], hidden[1])  # two hidden names read apart
        self.assertEqual(hidden[0].replace(" 1 of this file", " N of this file"),
                         hidden[1].replace(" 2 of this file", " N of this file"))  # only the ordinal differs
        self.assertTrue(hidden[0].startswith("<hidden key-like name 1 of this file> is not a setting"), hidden[0])
        for detail in hidden:
            for piece in (short, long_, "ghp_", "A1b2", "C3d4", str(len(short)), str(len(long_)), "digest"):
                self.assertNotIn(piece, detail)

    def test_the_digest_of_a_cut_key_is_made_without_the_sign_in_of_an_address(self) -> None:
        # Two keys that differ only in the password of an address show the same line, and the digest is that of
        # the text with the sign-in taken out (the same removal the shown form uses): no trace of the password.
        for head, tail in (("https://admin:", "@dbhost/" + "p" * 100), ("p" * 100 + " admin:", "@dbhost")):
            with self.subTest(scheme=head.startswith("https")):
                keys = [head + word + tail for word in ("S3cretpass", "Hunter2pass")]
                details = []
                for key in keys:
                    self.put(f'version = 1\n"{key}" = 1\n')
                    details.append(settings.load(self.environ).notes[0].detail)
                self.assertEqual(details[0], details[1])
                free = keys[0].replace("admin:S3cretpass", "<sign-in hidden>")
                self.assertIn(f" more, digest {_sha(free)})", details[0])
                for key, word in zip(keys, ("S3cretpass", "Hunter2pass")):
                    self.assertNotIn(_sha(key), details[0])
                    self.assertNotIn(word, details[0])

    def test_a_key_like_password_inside_the_sign_in_never_enters_the_digest(self) -> None:
        # The raw text holds a run that looks like a key, but only inside the sign-in: the name is shown cut,
        # and its digest is made of the text without that sign-in.
        word = "A1b2" * 6
        key = "https://admin:" + word + "@dbhost/" + "p" * 100
        self.put(f'version = 1\n"{key}" = 1\n')
        detail = settings.load(self.environ).notes[0].detail
        self.assertIn(f" more, digest {_sha(key.replace('admin:' + word, '<sign-in hidden>'))})", detail)
        self.assertNotIn(_sha(key), detail)
        self.assertNotIn("A1b2", detail)

    def test_a_long_name_whose_tail_looks_like_a_key_is_hidden_whole_with_no_digest(self) -> None:
        # What the look-alike rule hides is never hashed: the whole name is hidden and carries its ordinal only,
        # also where the part that looks like a key stands past the cap.
        for tail in ("A1b2" * 6, " password hunter2pass"):
            with self.subTest(tail=tail[:5]):
                key = "a" * 100 + tail
                self.put(f'version = 1\n"{key}" = 1\n')
                detail = settings.load(self.environ).notes[0].detail
                self.assertTrue(detail.startswith("<hidden key-like name 1 of this file> "), detail)
                for piece in ("digest", "aaaa", tail.strip()[:4], str(len(key))):
                    self.assertNotIn(piece, detail)

    def test_a_value_under_any_name_never_enters_the_digest_of_its_key(self) -> None:
        # The words of status.hidden_names hide values, never names: an unknown key that holds such a word is
        # shown cut with the digest of the key alone, and its value is neither shown nor hashed.
        key, secret = "a" * 100 + "_password", "s3cretvalue1"
        self.put(f'version = 1\n{key} = "{secret}"\n')
        detail = settings.load(self.environ).notes[0].detail
        self.assertIn(f" more, digest {_sha(key)})", detail)
        self.assertNotIn(secret, detail)
        self.assertNotIn(_sha(f"{key} = {secret}"), detail)

    def test_a_cut_key_holds_the_cap_and_part_of_the_key_for_every_pair_of_cap_and_digest_length(self) -> None:
        # The case of the read: caps 40 and 80, digest lengths 16, 32 and 64, keys of 100 and 60000.
        for size in (100, 60000):
            key = "k" * (size - 1) + "a"
            for cap in (40, 80):
                for chars in (16, 32, 64):
                    with self.subTest(size=size, cap=cap, chars=chars):
                        self.put(f"version = 1\n{key} = 1\n[status]\nvalue_max_chars = {cap}\n"
                                 f"digest_chars = {chars}\n")
                        detail = settings.load(self.environ).notes[0].detail
                        head = detail[:-len(UNKNOWN_WORDS)]
                        self.assertTrue(detail.endswith(UNKNOWN_WORDS), detail)
                        self.assertLessEqual(len(head), cap)
                        self.assertTrue(head.startswith("'kkkkkkkk"), head)
        # Every pair the two rows allow, walked: each cap from its least 40 to 200, each digest length from its
        # least 6 to past the whole digest; keys of either side of the cap, long, and of the file's most bytes.
        pattern = re.compile(r"^(.*)\.\.\. \((\d+) more, digest ([0-9a-f]+)\)$")
        for cap in range(40, 201):
            for chars in (*range(6, 73), 10 ** 18):
                sizes = (cap - 3, cap - 2, cap - 1, cap + 40, 1000)
                if chars in (6, 8, 16, 64, 10 ** 18):
                    sizes += (60000, settings.MAX_BYTES)
                for size in sizes:
                    key = "k" * (size - 1) + "a"
                    shown = settings._shown(key, cap, key=chars)
                    if len(repr(key)) <= cap:
                        self.assertEqual(repr(key), shown)
                        continue
                    found = pattern.match(shown)
                    self.assertIsNotNone(found, (cap, chars, size, shown))
                    kept, more, digest = found.group(1), int(found.group(2)), found.group(3)
                    self.assertLessEqual(len(shown), cap, (cap, chars, size))
                    self.assertTrue(kept.startswith("'kkkkkkkk"), (cap, chars, size, shown))
                    self.assertEqual(len(repr(key)), len(kept) + more, (cap, chars, size))
                    self.assertGreaterEqual(len(digest), 6)
                    self.assertEqual(_sha(key, len(digest)), digest)
                    if len(digest) < min(chars, 64):  # shortened only where the cap leaves no more room
                        self.assertLessEqual(len(kept), 10, (cap, chars, size, shown))

    def test_a_second_start_in_one_process_with_no_earlier_handler_writes_each_start_line_once(self) -> None:
        self.put(OFF)
        self.bare_logging()
        err = io.StringIO()
        self.start(err)
        self.start(err)
        shown = err.getvalue().splitlines()
        for start in (f"settings file: {self.file}",
                      "settings: plugin_host.gil_check = false (default true; source: settings file)"):
            found = [line for line in shown if line.endswith(start)]
            self.assertEqual(2, len(found), shown)  # one line on each of the two starts
            for line in found:
                self.assertRegex(line, START_FORM)

    def test_the_daemons_handler_writes_to_the_stderr_of_the_moment(self) -> None:
        self.put(OFF)
        for earlier in (False, True):
            with self.subTest(earlier=earlier):
                caller = logging.StreamHandler(io.StringIO())
                self.bare_logging([caller] if earlier else [])
                first = io.StringIO()
                self.start(first)
                first.close()  # the host shuts the stderr it had at start
                later = io.StringIO()
                with mock.patch.object(sys, "stderr", later):
                    logging.getLogger("recursion_core.daemon").info("a line after the start")
                self.assertIn("a line after the start", later.getvalue())
                self.assertNotIn("Logging error", later.getvalue())
                self.assertRegex(later.getvalue().splitlines()[0], START_FORM)


# Two made-up passwords; a line shown for one must equal the line shown for the other.
PASSWORDS = ("S3cretpass", "Hunter2pass")
# Two addresses in one run of text, joined in every way a person may write them: (the form with {} for the
# secret, the secret's own text around {} where it holds more). Each secret stands in the SECOND address or later.
JOINED = (
    ("https://a:x@h1/,https://b:{}@h2/", "{}"),  # a comma
    ("https://a:x@h1;https://b:{}@h2", "{}"),  # a semicolon, no path
    ("https://a:x@h1/|ftp://b:{}@h2/", "{}"),  # a pipe
    ('"https://a:x@h1/","https://b:{}@h2/"', "{}"),  # double quotes
    ("'https://h1/'+'https://b:{}@h2/'", "{}"),  # single quotes, the first with no sign-in
    ("[https://a:x@h1/][https://b:{}@h2/]", "{}"),  # brackets
    ("https://a:x@h1/\nhttps://b:{}@h2/", "{}"),  # a line break
    ("https://a:x@h1/https://b:{}@h2/", "{}"),  # nothing at all, after a path
    ("https://a:x@h1https://b:{}@h2", "{}"),  # nothing at all, after the host
    ("HTTPS://A:X@H1/,POSTGRES://B:{}@H2/DB", "{}"),  # upper-case schemes
    ("https://a:x@h1/,https://{}@h2/", "{}"),  # a sign-in with no password
    ("https://a:x@h1/,https://b:{}@q@h2/", "{}@q"),  # an at-sign in the password
    ("https://a:x@h1/,https://b:{}:q@h2/", "{}:q"),  # a colon in the password
    ("https://a:x@h1/,https://b:{}%40q%3Ar@h2/", "{}%40q%3Ar"),  # both, percent-escaped
    ("https://h1/,https://b:{}/q@h2/", "{}/q"),  # a slash in the password
    ("https://a:x@h1/,https://b:q@{}/r@h2/", "q@{}/r"),  # an at-sign and then a slash in the password
    ("https://a:x@h1/,https://b:{}%40q%2Fr@h2/x", "{}%40q%2Fr"),  # both, percent-escaped, then a path
    ("https://h1:8443/,b:{}@h2/", "{}"),  # a sign-in without a scheme after an address
    ("b:{}@h0,https://a:x@h1/", "{}"),  # a sign-in without a scheme before an address
    ("a://u:x@h1,b://u:y@h2,c://u:{}@h3", "{}"),  # three addresses, the secret in the third
)


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class EverySignInOfATextIsTakenOut(_Folder):
    """The sign-in of every address in a text is taken out, however two addresses are joined, as a value and as
    a key, shown whole or cut; the digest of a cut key does not move when only a password changes."""

    def test_the_second_of_two_addresses_joined_by_a_comma_or_a_semicolon_shows_no_password(self) -> None:
        # The case of draft 631's sign-in rule: only the first sign-in of a run was taken out.
        # Everything before the last @ of the run is hidden but a scheme at its very start: the first host sat
        # before it, and so did the label 'x=', which stands before the scheme.
        cases = (("https://a:x@h1/,https://b:{}@h2/", "'https://<sign-in hidden>@h2/'"),
                 ("x=https://a:x@h1/;y=postgres://b:{}@h2/db", "'<sign-in hidden>@h2/db'"))
        for form, whole in cases:
            for word in PASSWORDS:
                text = form.format(word)
                with self.subTest(form=form, word=word):
                    for cap in (40, 80, settings.NO_LIMIT):  # as a value
                        self.assertNotIn(word, settings._shown(text, cap))
                    self.assertEqual(whole, settings._shown(text, settings.NO_LIMIT))
                    details = []
                    for key in (text, text + "p" * 100):  # as a key of the file: shown whole, then cut
                        self.put(f'version = 1\n"{key}" = 1\n')
                        details.append(settings.load(self.environ).notes[0].detail)
                    self.assertTrue(details[0].startswith(whole), details[0])
                    free = settings._without_sign_in(text + "p" * 100)
                    self.assertNotIn(word, free)
                    self.assertIn(f" more, digest {_sha(free)})", details[1])
                    for detail in details:
                        self.assertNotIn(word, detail)
                        self.assertNotIn(_sha(text + "p" * 100), detail)
            lines = []
            for word in PASSWORDS:
                self.put(f'version = 1\n"{form.format(word)}{"p" * 100}" = 1\n')
                lines.append(settings.load(self.environ).notes[0].detail)
            self.assertEqual(lines[0], lines[1])  # the digest does not move when only the password changes

    def test_every_sign_in_is_taken_out_however_the_addresses_are_joined(self) -> None:
        for form, secret in JOINED:
            with self.subTest(form=form):
                shown, cut, free = [], [], []
                for word in PASSWORDS:
                    text = form.format(word)
                    for value in (text, ("Ada", text), (("Ada", text),)):
                        for cap in (40, 80, settings.NO_LIMIT):
                            line = settings._shown(value, cap)
                            self.assertNotIn(word, line)
                            self.assertNotIn(secret.format(word), line)
                            self.assertNotIn(word[:4], line)
                    shown.append(settings._shown(text, settings.NO_LIMIT))
                    key = text + "/" + "q" * 100
                    cut.append(settings._shown(key, 80, key=8))
                    self.assertNotIn(_sha(key), cut[-1])
                    self.assertNotIn(word, cut[-1])
                    free.append(settings._without_sign_in(key))
                    self.assertNotIn(word, free[-1])
                self.assertIn("<sign-in hidden>", shown[0])
                self.assertEqual(shown[0], shown[1])  # only the password changed: the same line
                self.assertEqual(cut[0], cut[1])  # and the same digest
                self.assertEqual(free[0], free[1])

    def test_an_address_with_no_sign_in_keeps_every_character(self) -> None:
        # A run with no @ stays whole: hosts and their ports.
        for text in ("https://h1/,https://h2:8443/x",):
            with self.subTest(text=text):
                self.assertEqual(repr(text), settings._shown(text, settings.NO_LIMIT))

    def test_a_host_or_a_path_before_the_last_at_sign_is_hidden_with_it(self) -> None:
        # The safe side: everything before the last @ of the run is hidden but a scheme at its very start.
        for text, whole in (("https://h1/u@mail,ada@example.com", "'https://<sign-in hidden>@example.com'"),
                            ("a@b,https://h2/", "'<sign-in hidden>@b,https://h2/'"),
                            ("https://h1/?to=ada@example.com#u@x", "'https://<sign-in hidden>@x'"),
                            ("https://[2001:db8::1]:5432/x,https://h2/@x", "'https://<sign-in hidden>@x'"),
                            ("https://h:8080/u@mail", "'https://<sign-in hidden>@mail'")):
            with self.subTest(text=text):
                self.assertEqual(whole, settings._shown(text, settings.NO_LIMIT))


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class ACutKeyNeverCarriesFewerThanTheLeastDigest(_Folder):
    """A digest is shortened to fit the cap, but never below the least of status.digest_chars."""

    def test_a_key_whose_count_has_six_digits_keeps_the_least_digest_within_the_least_cap(self) -> None:
        # Each U+200E is shown as its escape of 6 characters, so the count of what is not shown has 6 digits and
        # the room the cap of 40 leaves beside the key's first characters is 5: the least, 6, stands.
        key = "\u200e" * 21832
        least = settings.DECLARED[settings.DIGEST_CHARS].floor
        pattern = re.compile(r"^(.*)\.\.\. \((\d{6}) more, digest ([0-9a-f]+)\)$")
        for chars in (6, 8, 64):
            with self.subTest(chars=chars):
                shown = settings._shown(key, 40, key=chars)
                self.assertLessEqual(len(shown), 40, shown)
                found = pattern.match(shown)
                self.assertIsNotNone(found, shown)
                self.assertEqual(least, len(found.group(3)), shown)
                self.assertEqual(_sha(key, least), found.group(3))
                self.assertGreater(len(found.group(1)), 1, shown)  # the quote and part of the key

# The forms of a password that a reading of a host and its port, or a cut at each scheme mark, let through: (the
# form with {} for the made-up password, the secret's own text around {}, the line shown whole as a value).
PAST_A_PORT_OR_A_SCHEME_MARK = (
    ("https://UsrQ:1234/{}@dbhost/x", "1234/{}", "'https://<sign-in hidden>@dbhost/x'"),  # digits, then a slash
    ("https://UsrQ:1234#{}@dbhost/x", "1234#{}", "'https://<sign-in hidden>@dbhost/x'"),  # digits, then a hash
    ("https://UsrQ:1234?{}@dbhost/x", "1234?{}", "'https://<sign-in hidden>@dbhost/x'"),  # digits, then a ?
    ("https://UsrQ:QZJa://{}@dbhost/x", "QZJa://{}", "'https://<sign-in hidden>@dbhost/x'"),  # a scheme mark
    ("https://UsrQ:QZJahttps://{}@dbhost/x", "QZJahttps://{}", "'https://<sign-in hidden>@dbhost/x'"),  # a scheme
    ("UsrQ:QZJa://{}@dbhost/x", "QZJa://{}", "'<sign-in hidden>@dbhost/x'"),  # a scheme mark, no scheme before
    ("https://a:x@h1/,https://UsrQ:1234/{}@h2/", "1234/{}", "'https://<sign-in hidden>@h2/'"),  # the second of two
)


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class FromTheFirstSchemeMarkToTheLastAtSignIsHidden(_Folder):
    """A password that holds digits and then a slash, a hash or a question mark, or that holds a scheme mark, is
    hidden as a value and as a key, shown whole or cut; the digest of a cut key does not move with it."""

    def assert_hidden(self, line: str, form: str, secret: str, word: str) -> None:
        self.assertNotIn(word, line)
        self.assertNotIn(secret.format(word), line)
        self.assertNotIn("UsrQ", line)
        self.assertNotIn("QZJa", line)

    def test_as_a_value(self) -> None:
        for form, secret, whole in PAST_A_PORT_OR_A_SCHEME_MARK:
            for word in PASSWORDS:
                text = form.format(word)
                with self.subTest(form=form, word=word):
                    for value in (text, ("Ada", text), (("Ada", text),)):
                        for cap in (40, 80, settings.NO_LIMIT):
                            self.assert_hidden(settings._shown(value, cap), form, secret, word)
                    self.assertEqual(whole, settings._shown(text, settings.NO_LIMIT))

    def test_as_a_key_shown_whole(self) -> None:
        for form, secret, whole in PAST_A_PORT_OR_A_SCHEME_MARK:
            details = []
            for word in PASSWORDS:
                with self.subTest(form=form, word=word):
                    self.put(f'version = 1\n"{form.format(word)}" = 1\n')
                    details.append(settings.load(self.environ).notes[0].detail)
                    self.assertTrue(details[-1].startswith(whole), details[-1])
                    self.assert_hidden(details[-1], form, secret, word)
            with self.subTest(form=form):
                self.assertEqual(details[0], details[1])  # only the password changed: the same line

    def test_as_a_key_cut_the_digest_holds_still_while_only_the_password_changes(self) -> None:
        for form, secret, whole in PAST_A_PORT_OR_A_SCHEME_MARK:
            details = []
            for word in PASSWORDS:
                key = form.format(word) + "p" * 100
                with self.subTest(form=form, word=word):
                    self.put(f'version = 1\n"{key}" = 1\n')
                    details.append(settings.load(self.environ).notes[0].detail)
                    self.assert_hidden(details[-1], form, secret, word)
                    self.assertIn(f" more, digest {_sha(settings._without_sign_in(key))})", details[-1])
                    self.assertNotIn(_sha(key), details[-1])
            with self.subTest(form=form):
                self.assertEqual(details[0], details[1])  # the digest does not move when only the password changes


# The generated test: made-up passwords from this alphabet (letters, digits and the marks a password may hold, the
# scheme mark drawn as one), a set count of draws from one seed, in one address and in two joined addresses.
GENERATED_ALPHABET = tuple(string.ascii_letters + string.digits) + ("/", ":", "#", "?", "@", "%", ",", ";", "://")
GENERATED_DRAWS = 5000
GENERATED_SEED = 729
GENERATED_LONGEST = 24
GENERATED_FORMS = (
    "https://u:{}@dbhost/x",  # one address
    "https://a:x@h1/,https://u:{}@h2/x",  # the second of two joined by a comma
    "https://u:{}@h1/;postgres://b:x@h2/db",  # the first of two joined by a semicolon
    "https://u:{}@h1https://b:x@h2",  # the first of two joined by nothing
)


# The second generator: a password label of the look-alike rule (each spelling, in lower, upper and mixed case,
# then each mark or none) or a known key prefix, then a made-up password from GENERATED_ALPHABET that holds an @:
# the last @ at any place and the tail after it of every length from 0 to GENERATED_LONGEST, for each form in turn.
# The reproduced cases come first, set by hand.
LABELLED_SEED = 2311
LABELLED_DRAWS = 50  # for each label, case and mark, and for each key prefix: two of each tail length
LABELLED_WORDS = ("password", "passwd", "secret", "apikey", "api_key", "api-key", "bearer", "token")
LABELLED_MARKS = ("=", ":", ": ", " = ", "")
LABELLED_BY_HAND = (("password=", "hunter2@x7Gq9zLm"), ("token:", "abc@defghijk"), ("password=", "hunter2@x7Gq"))


def _labelled_forms() -> list[tuple[str, bool]]:
    """(the label and its mark, or a key prefix; whether it is a label with no mark)."""
    forms = []
    for word in LABELLED_WORDS:
        mixed = "".join(c.upper() if i % 2 else c for i, c in enumerate(word))
        for spelled in (word, word.upper(), mixed):
            forms.extend((spelled + mark, not mark) for mark in LABELLED_MARKS)
    return forms + [(prefix, False) for prefix in settings._KEY_PREFIXES]


def _labelled_draws():
    """(the form, the password) of each draw: the reproduced cases, then LABELLED_DRAWS for each form."""
    yield from LABELLED_BY_HAND
    draw = random.Random(LABELLED_SEED)
    no_at = tuple(c for c in GENERATED_ALPHABET if c != "@")
    marks = tuple(c for c in GENERATED_ALPHABET if not c[0].isalnum())
    for form, bare in _labelled_forms():
        for i in range(LABELLED_DRAWS):
            tail = "".join(draw.choice(no_at) for _ in range(i % (GENERATED_LONGEST + 1)))
            before = "".join(draw.choice(GENERATED_ALPHABET) for _ in range(draw.randint(0, GENERATED_LONGEST)))
            while len(before) + 1 + len(tail) < settings._KEY_TAIL:  # the label rule reads a password this long
                before += draw.choice(GENERATED_ALPHABET)
            password = before + "@" + tail
            if bare and (password[0].isalnum() or password[0] == "_"):
                # a label and a password with no mark between them that read as one longer word ('tokenab@c')
                # are no label to the look-alike rule; a mark begins the password instead
                password = draw.choice(marks) + password
            yield form, password


class AGeneratedPasswordNeverReachesALine(unittest.TestCase):
    """In every draw no run of 3 characters of the password (so no longer run either) stands in a line shown for
    it, as a value whole and cut and as a key cut, and every such line, the cut key's digest with it, is the line
    shown for a stand-in password that shares no character with the alphabet."""

    @staticmethod
    def lines(text: str) -> tuple[str, str, str]:
        return (settings._shown(text, settings.NO_LIMIT), settings._shown(text, 40),
                settings._shown(text + "/" + "q" * 100, 80, key=8))

    def test_no_run_of_a_generated_password_reaches_a_line_and_no_digest_moves(self) -> None:
        draw = random.Random(GENERATED_SEED)
        drawn = 0
        for form in GENERATED_FORMS:
            reference = self.lines(form.format("~"))
            key = form.format("~") + "/" + "q" * 100
            self.assertIn(f"digest {_sha(settings._without_sign_in(key))})", reference[2])
            for _ in range(GENERATED_DRAWS):
                password = "".join(draw.choice(GENERATED_ALPHABET)
                                   for _ in range(draw.randint(1, GENERATED_LONGEST)))
                drawn += 1
                runs = {password[i:i + 3] for i in range(len(password) - 2)}
                for line, before in zip(self.lines(form.format(password)), reference):
                    # a run the stand-in's line holds too (a scheme mark, a word of the note) is no trace
                    leaked = sorted(run for run in runs if run in line and run not in before)
                    if leaked or line != before:
                        self.fail(f"form {form!r}, password {password!r}: {leaked} in {line!r}")
        self.assertEqual(len(GENERATED_FORMS) * GENERATED_DRAWS, drawn)

    def test_a_labelled_password_that_holds_an_at_sign_is_hidden_whole(self) -> None:
        # The label or prefix and the password as one value: whole, cut and as a key cut, the line is the
        # hidden-key words and no run of 3 characters of the password stands in it.
        drawn, failed = 0, []
        for form, password in _labelled_draws():
            drawn += 1
            runs = {password[i:i + 3] for i in range(len(password) - 2)}
            lines = self.lines(form + password)
            leaked = sorted(run for run in runs if any(run in line for line in lines) and run not in settings._HIDDEN_KEY)
            if leaked or set(lines) != {settings._HIDDEN_KEY}:
                failed.append(f"{form + password!r}: {leaked} in {lines!r}")
        self.assertEqual(len(LABELLED_BY_HAND) + len(_labelled_forms()) * LABELLED_DRAWS, drawn)
        self.assertEqual([], failed[:4], f"{len(failed)} of {drawn} generated values show a part of the password")


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class ALabelledPasswordIsHiddenWhole(_Folder):
    """A value or a key whose own text carries a password label or a known key prefix is never shown, not in
    part, wherever the @ stands in it: in status (python -m recursion_core.settings) and in the start log, under
    names that hold no hidden word."""

    def shown(self, text: str) -> str:
        self.put(text)
        env = dict(self.environ, PATH=os.environ.get("PATH", ""), PYTHONPATH=str(CORE), TMPDIR=str(self.tmp))
        proc = subprocess.run([sys.executable, "-B", "-m", "recursion_core.settings"], capture_output=True,
                              text=True, env=env, cwd=str(self.tmp), timeout=60, check=False)
        _, messages, _ = TheDaemonsStart.start_log(self)
        logged = [message for message in messages if message.startswith("settings: ")]
        self.assertTrue(logged)
        return proc.stdout + proc.stderr + "\n".join(logged)

    def assert_hidden_whole(self, file) -> None:
        real = [form + password for form, password in LABELLED_BY_HAND]
        stand_in = [form + "~" * len(password) for form, password in LABELLED_BY_HAND]
        out = self.shown(file(real))
        self.assertIn("<hidden", out)
        for form, password in LABELLED_BY_HAND:
            self.assertNotIn(password[3:], out)
            self.assertNotIn(password[password.rfind("@"):], out)  # the tail after the last @
        self.assertEqual(self.shown(file(stand_in)), out)  # only the password changed: every line the same

    def test_as_values_under_names_that_hold_no_hidden_word(self) -> None:
        def file(texts: list[str]) -> str:
            return (f'version = 1\n[plugin_host]\ngil_check = "{texts[0]}"\n[status]\nvalue_max_chars = "none"\n'
                    f'shown_names = [{", ".join(chr(34) + text + chr(34) for text in texts)}]\n')
        self.assert_hidden_whole(file)

    def test_as_keys_of_the_file(self) -> None:
        self.assert_hidden_whole(lambda texts: "version = 1\n" + "".join(f'"{text}" = 1\n' for text in texts))

    def test_every_labelled_draw_as_a_key_of_a_file_is_hidden_whole(self) -> None:
        self.put('version = 1\n"zzzz" = 1\n"zz.zz" = 1\n')  # the words of the two notes a key can meet
        tails = {note.detail[len(repr(key)):] for key, note in zip(("zzzz", "zz.zz"), settings.load(self.environ).notes)}
        batches = collections.defaultdict(dict)
        for form, password in _labelled_draws():
            batches[form].setdefault(form + password, password)
        drawn, failed = 0, []
        for keys in batches.values():
            self.put("version = 1\n" + "".join(f'"{key}" = 1\n' for key in keys))
            notes = settings.load(self.environ).notes
            self.assertEqual(len(keys), len(notes))
            for number, (key, note) in enumerate(zip(keys, notes), 1):
                drawn += 1
                name = settings._HIDDEN_KEY_NAME.format(number)
                rest = re.sub(r"; did you mean [a-z_.]+\?$", "", note.detail[len(name):])
                if not note.detail.startswith(name) or rest not in tails:
                    failed.append(f"{key!r}: {note.detail!r}")
        self.assertEqual(sum(len(keys) for keys in batches.values()), drawn)
        self.assertEqual([], failed[:4], f"{len(failed)} of {drawn} generated keys show a part of the password")


# Sign-ins with no scheme at the very start of their run: (the form with {} for the made-up secret, the parts of
# the form that must never be shown, the line shown whole as a value). Everything before the last @ is hidden.
BEFORE_THE_LAST_AT_SIGN = (
    ("Us!rQ:{}@h/x", ("Us!", "rQ"), "'<sign-in hidden>@h/x'"),  # a mark in the user name, then a password
    ("Us{}!rQ:QZJa@h/x", ("Us", "rQ", "QZJa"), "'<sign-in hidden>@h/x'"),  # the secret in the user name
    ("ab/CD+{}=:QZJa@h/x", ("ab/CD", "QZJa"), "'<sign-in hidden>@h/x'"),  # a slash, a plus, an equals sign
    ("x=Us${}:QZJa@h/x", ("x=Us", "QZJa"), "'<sign-in hidden>@h/x'"),  # a label, then a dollar sign
    ("{}@github.com/org/repo", (), "'<sign-in hidden>@github.com/org/repo'"),  # a token as the user, no colon
    ("{}@dbhost:5432", (), "'<sign-in hidden>@dbhost:5432'"),  # the colon only after the last @, a port
    ("{}@github.com:org/repo.git", (), "'<sign-in hidden>@github.com:org/repo.git'"),  # the scp form
    ("{}@h1,https://UsrQ:QZJa@h2/", ("UsrQ", "QZJa", "h1,"), "'<sign-in hidden>@h2/'"),  # a token before a scheme
    ("x;{}@h0.example.com", ("x;",), "'<sign-in hidden>@h0.example.com'"),  # a label and a token
    ("h0.example.com/db?x=1*{}@h1.example.com", ("h0.", "db?x"), "'<sign-in hidden>@h1.example.com'"),  # a host
    ("url=https://UsrQ:{}@h/x", ("url=", "UsrQ"), "'<sign-in hidden>@h/x'"),  # a label before a scheme
)


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class EverythingBeforeTheLastAtSignIsHidden(_Folder):
    """With no scheme at the very start of a run, nothing before its last @ is shown: not a user name or a part
    of it, not a token used as the user name, not a label or an earlier host; as a value and as a key, shown
    whole or cut, and the digest of a cut key does not move with what is hidden."""

    def assert_hidden(self, line: str, form: str, parts: tuple[str, ...], word: str) -> None:
        self.assertNotIn(word, line)
        self.assertNotIn(word[:3], line)
        for part in parts:
            self.assertNotIn(part, line)

    def test_as_a_value(self) -> None:
        for form, parts, whole in BEFORE_THE_LAST_AT_SIGN:
            for word in PASSWORDS:
                text = form.format(word)
                with self.subTest(form=form, word=word):
                    for value in (text, ("Ada", text), (("Ada", text),)):
                        for cap in (40, 80, settings.NO_LIMIT):
                            self.assert_hidden(settings._shown(value, cap), form, parts, word)
                    self.assertEqual(whole, settings._shown(text, settings.NO_LIMIT))

    def test_as_a_key_shown_whole(self) -> None:
        for form, parts, whole in BEFORE_THE_LAST_AT_SIGN:
            details = []
            for word in PASSWORDS:
                with self.subTest(form=form, word=word):
                    self.put(f'version = 1\n"{form.format(word)}" = 1\n')
                    details.append(settings.load(self.environ).notes[0].detail)
                    self.assertTrue(details[-1].startswith(whole), details[-1])
                    self.assert_hidden(details[-1], form, parts, word)
            with self.subTest(form=form):
                self.assertEqual(details[0], details[1])  # only the secret changed: the same line

    def test_as_a_key_cut_the_digest_holds_still_while_only_the_hidden_part_changes(self) -> None:
        for form, parts, whole in BEFORE_THE_LAST_AT_SIGN:
            details = []
            for word in PASSWORDS:
                key = form.format(word) + "/" + "p" * 100  # a slash: '5432' and the p's are no key-like run
                with self.subTest(form=form, word=word):
                    self.put(f'version = 1\n"{key}" = 1\n')
                    details.append(settings.load(self.environ).notes[0].detail)
                    self.assert_hidden(details[-1], form, parts, word)
                    self.assertIn(f" more, digest {_sha(settings._without_sign_in(key))})", details[-1])
                    self.assertNotIn(_sha(key), details[-1])
            with self.subTest(form=form):
                self.assertEqual(details[0], details[1])  # the digest does not move when only the secret changes


class WhatEndsARun(unittest.TestCase):
    """A run ends at every white-space character as Python counts them, and at nothing else: a zero-width
    character inside a sign-in does not end its run, so the sign-in stays hidden whole."""

    def test_every_white_space_character_ends_a_run_and_nothing_else_does(self) -> None:
        for code in range(0x110000):
            char = chr(code)
            if bool(settings._SPACE.fullmatch(char)) != char.isspace():
                self.fail(f"U+{code:04X}: ends a run {bool(settings._SPACE.fullmatch(char))}, white space "
                          f"{char.isspace()}")

    def test_a_zero_width_character_does_not_end_a_run(self) -> None:
        for mark in tuple(chr(code) for code in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)):
            with self.subTest(mark=f"U+{ord(mark):04X}"):
                self.assertEqual("'https://<sign-in hidden>@h/x'",
                                 settings._shown(f"https://UsrQ:QZ{mark}Ja@h/x", settings.NO_LIMIT))
                self.assertEqual("'<sign-in hidden>@h/x'", settings._shown(f"Us{mark}rQ@h/x", settings.NO_LIMIT))

    def test_white_space_ends_a_run_and_the_run_after_it_is_read_alone(self) -> None:
        for mark in tuple(chr(code) for code in (0x20, 0x09, 0x0A, 0xA0, 0x2028, 0x1F)):
            with self.subTest(mark=f"U+{ord(mark):04X}"):
                line = settings._shown(f"Us{mark}rQ@h/x", settings.NO_LIMIT)
                self.assertTrue(line.endswith("<sign-in hidden>@h/x'"), line)
                self.assertNotIn("rQ", line)


# The generated test of the one rule: made-up secrets from every ASCII letter, digit and mark (the scheme mark drawn
# as one), as user names with and without a colon and as passwords, after a scheme, a label or nothing, in 1, 2 or 5
# addresses joined by any ASCII mark or by nothing; a set count of draws from one seed.
GENERATED_RUN_ALPHABET = tuple(string.ascii_letters + string.digits + string.punctuation) + ("://",)
GENERATED_RUN_DRAWS = 50000
GENERATED_RUN_SEED = 7295
GENERATED_RUN_LONGEST = 40
GENERATED_RUN_SCHEMES = ("", "", "https", "postgres", "postgresql+psycopg", "jdbc:postgresql", "redis", "git+ssh",
                         "mongodb+srv", "amqps")
GENERATED_RUN_LABELS = ("", "", "url=", "dsn:", "x;")
GENERATED_RUN_FORMS = ("UsrQ:{p}@", "{s}:{p}@", "{s}@", ":{p}@", "")  # the last: no sign-in
GENERATED_RUN_PATHS = ("", "/db", ":5432/x", "/x?y=1")
# The scheme and its mark a run may begin with, as the module's words say it, written here again.
A_SCHEME_AT_THE_START = re.compile(r"[A-Za-z0-9+.-]+://")


class NothingBeforeTheLastAtSignReachesALine(unittest.TestCase):
    """In every generated draw no run of 3 characters of what stood before the last @ (the scheme at the very start
    aside) stands in a line shown for it (a value whole and at the cap of 40, a key cut at 80 and at 40) that the
    line of a stand-in lacks, where the stand-in is the same text with all of that part written as one '~'; and
    every such line, a cut key's digest with it, is the stand-in's line."""

    @staticmethod
    def lines(text: str) -> tuple[str, ...]:
        key = text + "/" + "q" * 100
        return (settings._shown(text, settings.NO_LIMIT), settings._shown(text, 40),
                settings._shown(key, 80, key=8), settings._shown(key, 40, key=6))

    def test_no_run_of_what_stood_before_the_last_at_sign_reaches_a_line_and_no_digest_moves(self) -> None:
        draw = random.Random(GENERATED_RUN_SEED)

        def secret() -> str:
            return "".join(draw.choice(GENERATED_RUN_ALPHABET) for _ in range(draw.randint(1, GENERATED_RUN_LONGEST)))

        seen = collections.Counter()
        for _ in range(GENERATED_RUN_DRAWS):
            parts = [draw.choice(GENERATED_RUN_LABELS)]
            for i in range(draw.choice((1, 2, 5))):
                if i:
                    parts.append(draw.choice(("",) + tuple(string.punctuation)))
                scheme, form = draw.choice(GENERATED_RUN_SCHEMES), draw.choice(GENERATED_RUN_FORMS)
                seen[f"form {form!r}"] += 1
                parts.append((scheme + "://" if scheme else "") + form.format(s=secret(), p=secret())
                             + f"h{i}.example.com" + draw.choice(GENERATED_RUN_PATHS))
            text = "".join(parts)
            last = text.rfind("@")
            start = A_SCHEME_AT_THE_START.match(text)
            begin = start.end() if start else 0
            if last <= begin:
                seen["nothing before an @"] += 1
                continue
            seen["a scheme at the start" if start else "no scheme at the start"] += 1
            hidden = text[begin:last]
            runs = {hidden[i:i + 3] for i in range(len(hidden) - 2)}
            stand_in = self.lines(text[:begin] + "~" + text[last:])
            if settings._key_shape_as_written(text) and not settings._key_shape_as_written(text[:begin] + "~"
                                                                                         + text[last:]):
                # The one exception: the hidden part carries a password label or a known key prefix, as the
                # module's own predicate reads it, so every line is the words for a hidden key or password.
                seen["the hidden part carries a key shape"] += 1
                stand_in = (settings._HIDDEN_KEY,) * len(stand_in)
            for line, before in zip(self.lines(text), stand_in):
                leaked = sorted(run for run in runs if run in line and run not in before)
                if leaked or line != before:
                    self.fail(f"text {text!r}: {leaked} in {line!r}, the stand-in's line {before!r}")
        self.assertEqual(GENERATED_RUN_DRAWS, seen["nothing before an @"] + seen["a scheme at the start"]
                         + seen["no scheme at the start"])
        kinds = ["a scheme at the start", "no scheme at the start"] + [f"form {f!r}" for f in GENERATED_RUN_FORMS]
        for drawn in kinds:
            self.assertGreater(seen[drawn], 0, drawn)


if __name__ == "__main__":
    unittest.main()
