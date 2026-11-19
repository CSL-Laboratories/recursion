"""A person's own settings file (recursion_core/settings.py): one place, owner-only, every value with its default
and shown, never written, and on a system without an owner check no file is read.

Every path is made inside the test's own temporary folder. Nothing outside it is read for a value or written.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import MappingProxyType
from unittest import mock

from recursion_core import settings

CORE = Path(__file__).resolve().parents[3]
REPO = CORE.parent
SCHEMA_DIR = REPO / "schemas" / "settings" / "v1"
POSIX_ONLY = "the owner check needs a uid; a system without one is the WindowsNoOwnerCheck class"
OFF = "version = 1\n\n[plugin_host]\ngil_check = false\n"


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

    def test_a_file_that_cannot_be_used_leaves_every_default_with_the_reason(self) -> None:
        cases = {
            "not TOML": "version = 1\n[plugin_host\n",
            "version 2": "version = 2\n[plugin_host]\ngil_check = false\n",
            "no version": "[plugin_host]\ngil_check = false\n",
            "version true": "version = true\n[plugin_host]\ngil_check = false\n",
            "version 1.0": "version = 1.0\n[plugin_host]\ngil_check = false\n",
            "too large": OFF + "# " + "x" * settings.MAX_BYTES + "\n",
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                self.put(text)
                loaded = settings.load(self.environ)  # never raises: the daemon starts
                self.assert_default(loaded, settings.UNREADABLE)
                self.assertIn(str(self.file), loaded.notes[0].detail)
        self.file.write_bytes(b"version = 1\n# \xff\n")
        self.assert_default(settings.load(self.environ), settings.UNREADABLE)

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
        self.assertEqual(0, settings.main([], self.environ, out, io.StringIO()))
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
        for module in ("daemon/plugins.py", "plugin_security/protocol.py"):
            with self.subTest(module=module):
                source = (CORE / "recursion_core" / module).read_text(encoding="utf-8")
                self.assertNotIn("settings", source)


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


if __name__ == "__main__":
    unittest.main()
