"""A person's own settings file (recursion_core/settings.py): one place, owner-only, every value with its default
and shown, never written, and on a system without an owner check no file is read.

Every path is made inside the test's own temporary folder. Nothing outside it is read for a value or written.
"""

from __future__ import annotations

import ast
import collections
import contextlib
import functools
import inspect
import io
import json
import logging
import os
from pathlib import Path
import re
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
        self.assertEqual(["plugin_host.gil_check = false (default true; source: settings file)"],
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
                self.assertEqual([f"warning: note {loaded.notes[0]}"], settings.lines(loaded)[1:])

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
                self.assertEqual([f"warning: note {loaded.notes[0]}"], settings.lines(loaded)[1:])

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

    def test_a_number_outside_its_floor_and_ceiling_keeps_the_default_never_a_clamp(self) -> None:
        row = settings.Setting("test.limit", int, 5, "A test row.", floor=1, ceiling=10)
        rows = settings.SETTINGS + (row,)
        with mock.patch.object(settings, "SETTINGS", rows), \
                mock.patch.object(settings, "DECLARED", MappingProxyType({r.name: r for r in rows})):
            for found, expect, codes in (("11", 5, [settings.OUT_OF_RANGE]), ("0", 5, [settings.OUT_OF_RANGE]),
                                         ("7", 7, []), ("10", 10, []), ("1", 1, []),
                                         ("2.5", 5, [settings.OUT_OF_RANGE]), ("true", 5, [settings.OUT_OF_RANGE])):
                with self.subTest(found=found):
                    self.put(f"version = 1\n[test]\nlimit = {found}\n")
                    loaded = settings.load(self.environ)
                    self.assertEqual(expect, loaded["test.limit"])
                    self.assertEqual(codes, self.codes(loaded))
                    if codes:
                        self.assertIn("allowed: a whole number from 1 to 10", loaded.notes[0].detail)


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
        self.assertEqual({"const": settings.FILE_VERSION}, schema["properties"]["version"])
        kinds = {bool: "boolean", int: "integer", float: "number", str: "string"}
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
        expect = {r.name: (kinds[r.type], r.default, r.says, r.floor, r.ceiling) for r in settings.SETTINGS}
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
        self.assertEqual((1, 1), (len(good), len(bad)))
        self.put(good[0].read_text(encoding="utf-8"))
        loaded = settings.load(self.environ)
        self.assertEqual([], self.codes(loaded))
        self.assertEqual({settings.SOURCE_FILE}, {v.source for v in loaded.values.values()})
        self.put(bad[0].read_text(encoding="utf-8"))
        self.assert_default(settings.load(self.environ), settings.OUT_OF_RANGE)


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
                self.assertEqual("'engines' is not a setting this version knows; it is skipped", loaded.notes[0].detail)


if __name__ == "__main__":
    unittest.main()
