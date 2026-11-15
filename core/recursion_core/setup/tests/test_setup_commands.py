"""``recursiond install | upgrade | uninstall``: what each prints, and the interpreter checks."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest import mock

import recursion_core
from recursion_core import _entrypoints as entry
from recursion_core.control.commands import setup
from recursion_core.daemon_install import UNIT_NAME, Roots, service_fields

PY = "/opt/recursion/venv/bin/python3.14t"
PY_NEXT = "/opt/recursion/venv-2/bin/python3.14t"
PACKAGE = recursion_core.__file__
FROM_CHECKOUT = bool(os.environ.get("PYTHONPATH"))  # how this suite is run in the repository


def _probe(**changes):
    info = {"implementation": "cpython", "version": [3, 14], "free_threaded_build": True, "package": PACKAGE}
    info.update(changes)
    return lambda interpreter: dict(info)


def _refusing_probe(interpreter):
    raise AssertionError("uninstall must not start the interpreter")


@unittest.skipUnless(os.name == "posix", "the installer is Linux-only")
class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="setup-cmd-")
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.mkdir(self.home, 0o700)
        self.roots = Roots(home=self.home)
        self.unit = os.path.join(self.home, ".config", "systemd", "user", UNIT_NAME)

    def run_action(self, action, *, interpreter=PY, probe=None, package=PACKAGE):
        out = io.StringIO()
        code = setup.run(action, out=out, roots=self.roots, interpreter=interpreter, package=package,
                         probe=probe or _probe())
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0])

    def test_install_prints_ok_and_install_twice_changes_nothing(self):
        code, answer = self.run_action("install")
        self.assertEqual(code, 0)
        store_dir = os.path.join(self.home, ".local", "state", "recursion", "plugin-security")
        stores = [{"kind": "breaker_store", "path": os.path.join(store_dir, "breaker.sqlite3"), "created": True},
                  {"kind": "revocation_store", "path": os.path.join(store_dir, "revoked.sqlite3"), "created": True}]
        self.assertEqual(answer, {
            "code": "ok", "action": "install", "changed": True, "unit": self.unit, "interpreter": PY,
            "stores": stores,
            "next": [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", UNIT_NAME]]})
        with open(self.unit, encoding="utf-8") as handle:
            first = handle.read()
        self.assertEqual(service_fields(first)["Service.ExecStart"], f"{PY} -I -X gil=0 -m recursion_core.daemon")
        code, answer = self.run_action("install")
        self.assertEqual((code, answer["changed"]), (0, False))
        self.assertEqual(answer["stores"], [dict(store, created=False) for store in stores])
        with open(self.unit, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), first)

    def test_install_and_upgrade_refuse_an_interpreter_the_daemon_cannot_run(self):
        for changes in ({"version": [3, 12], "free_threaded_build": False},
                        {"free_threaded_build": False},
                        {"version": [3, 13]},
                        {"implementation": "pypy"},
                        {"version": None}):
            for action in ("install", "upgrade"):
                with self.subTest(probe=changes, action=action):
                    code, answer = self.run_action(action, probe=_probe(**changes))
                    self.assertEqual((code, answer["code"]), (1, "interpreter_not_free_threaded"))
                    self.assertIn("python3.14t", answer["detail"])
                    self.assertIn(PY, answer["detail"])
        self.assertEqual(os.listdir(self.home), [])

    def test_install_refuses_an_interpreter_that_cannot_import_this_package(self):
        code, answer = self.run_action("install", probe=_probe(package=None))
        self.assertEqual((code, answer["code"]), (1, "not_installed"))
        self.assertIn("without PYTHONPATH", answer["detail"])
        elsewhere = "/somewhere/else/site-packages/recursion_core/__init__.py"
        code, answer = self.run_action("install", probe=_probe(package=elsewhere))
        self.assertEqual((code, answer["code"]), (1, "not_installed"))
        self.assertIn("/somewhere/else/site-packages/recursion_core", answer["detail"])
        self.assertEqual(os.listdir(self.home), [])

    def test_a_probe_that_fails_is_printed_as_its_refusal(self):
        def failing(interpreter):
            raise setup.InstallRefused("interpreter_unknown", "gone")
        code, answer = self.run_action("install", probe=failing)
        self.assertEqual((code, answer), (1, {"code": "interpreter_unknown", "action": "install", "detail": "gone"}))

    def test_upgrade_and_uninstall(self):
        self.run_action("install")
        code, answer = self.run_action("upgrade", interpreter=PY_NEXT)
        self.assertEqual((code, answer["changed"], answer["interpreter"]), (0, True, PY_NEXT))
        self.assertEqual(answer["next"], [["systemctl", "--user", "daemon-reload"],
                                          ["systemctl", "--user", "try-restart", UNIT_NAME]])
        code, answer = self.run_action("uninstall", probe=_refusing_probe)
        self.assertEqual(code, 0)
        self.assertEqual(answer, {"code": "ok", "action": "uninstall", "changed": True, "unit": self.unit,
                                  "next": [["systemctl", "--user", "stop", UNIT_NAME],
                                           ["systemctl", "--user", "daemon-reload"]]})
        self.assertFalse(os.path.lexists(self.unit))

    def test_installer_refusals_are_printed_with_exit_1(self):
        self.run_action("install")
        code, answer = self.run_action("install", interpreter=PY_NEXT)
        self.assertEqual((code, answer["code"]), (1, "unit_differs"))
        os.makedirs(os.path.join(os.path.dirname(self.unit), "default.target.wants"))
        os.symlink(self.unit, os.path.join(os.path.dirname(self.unit), "default.target.wants", UNIT_NAME))
        code, answer = self.run_action("uninstall", probe=_refusing_probe)
        self.assertEqual((code, answer["code"]), (1, "unit_enabled"))

    def test_an_unknown_action_is_refused(self):
        code, answer = self.run_action("purge")
        self.assertEqual((code, answer["code"]), (1, "action"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes through any mode")
    def test_a_file_system_error_is_a_json_refusal(self):
        # A folder this user can read but not write: the real PermissionError, as one JSON refusal
        # with exit 1, for each command (review 1: Astra R4, Sol R1). An existing unit stays as it was.
        unit_dir = os.path.dirname(self.unit)
        config = os.path.join(self.home, ".config")
        os.makedirs(config, 0o700)
        os.chmod(config, 0o500)
        code, answer = self.run_action("install")
        self.assertEqual((code, answer["code"], answer["action"]), (1, "filesystem_error", "install"))
        self.assertIn("PermissionError", answer["detail"])
        self.assertIn(config, answer["detail"])
        self.assertNotIn("changed", answer)
        os.chmod(config, 0o700)
        self.run_action("install")
        with open(self.unit, "rb") as handle:
            before = handle.read()
        os.chmod(unit_dir, 0o500)
        self.addCleanup(os.chmod, unit_dir, 0o700)
        for action, probe in (("upgrade", None), ("uninstall", _refusing_probe)):
            with self.subTest(action=action):
                code, answer = self.run_action(action, interpreter=PY_NEXT, probe=probe)
                self.assertEqual((code, answer["code"], answer["action"]), (1, "filesystem_error", action))
                self.assertIn(unit_dir, answer["detail"])
                with open(self.unit, "rb") as handle:
                    self.assertEqual(handle.read(), before)

    def test_a_file_system_error_that_names_no_path_names_the_unit_folder(self):
        # Review 3 of #382, DeepSeek L2: an fsync (or a read or write on an open file) fails with an
        # OSError that carries no file name; the detail still names where it happened.
        unit_dir = os.path.dirname(self.unit)
        # Slice 2: install makes the stores first, and their creation fsyncs too (a failure there is
        # store_not_created); with the stores in place, the unit's own write is what fails here.
        self.run_action("install")
        os.unlink(self.unit)
        with mock.patch("os.fsync", side_effect=OSError(5, "Input/output error")):
            code, answer = self.run_action("install")
        self.assertEqual((code, answer["code"], answer["action"]), (1, "filesystem_error", "install"))
        self.assertEqual(answer["detail"], f"OSError: [Errno 5] Input/output error (in {unit_dir})")
        self.run_action("install")
        for action, probe in (("upgrade", None), ("uninstall", _refusing_probe)):
            with self.subTest(action=action), mock.patch("os.fsync", side_effect=OSError(5, "Input/output error")):
                code, answer = self.run_action(action, interpreter=PY_NEXT, probe=probe)
                self.assertEqual((code, answer["code"], answer["action"]), (1, "filesystem_error", action))
                self.assertIn(f"(in {unit_dir})", answer["detail"])
        # An error that names its own path is printed as it is, with no folder added.
        with mock.patch("os.lstat", side_effect=PermissionError(13, "Permission denied", self.home)):
            code, answer = self.run_action("install")
        self.assertEqual(answer["detail"], f"PermissionError: [Errno 13] Permission denied: '{self.home}'")

    def test_an_existing_unit_another_user_could_change_is_printed_as_its_refusal(self):
        # Review 3 of #382, DeepSeek L1, as the command prints it: install refuses, upgrade writes it again.
        self.run_action("install")
        os.chmod(self.unit, 0o666)
        code, answer = self.run_action("install")
        self.assertEqual((code, answer["code"]), (1, "unit_untrusted"))
        self.assertIn(self.unit, answer["detail"])
        code, answer = self.run_action("upgrade")
        self.assertEqual((code, answer["code"], answer["changed"]), (0, "ok", True))
        self.assertEqual(os.stat(self.unit).st_mode & 0o777, 0o644)

    def test_recursiond_install_end_to_end(self):
        out = io.StringIO()
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(setup, "default_roots", return_value=self.roots), \
                mock.patch.object(setup, "probe_interpreter", _probe()), contextlib.redirect_stdout(out):
            code = entry.main(["install"])
        self.assertEqual(code, 0)
        answer = json.loads(out.getvalue())
        self.assertEqual((answer["code"], answer["interpreter"], answer["unit"]), ("ok", sys.executable, self.unit))

    def test_the_default_home_is_the_password_databases_not_HOME(self):
        import pwd
        with mock.patch.dict(os.environ, {"HOME": "/somewhere/else"}):
            self.assertEqual(setup.default_roots(), Roots(home=pwd.getpwuid(os.geteuid()).pw_dir))


@unittest.skipUnless(os.name == "posix", "the installer is Linux-only")
class RealProbeTests(unittest.TestCase):
    def test_the_probe_reads_this_interpreter_as_the_unit_would_start_it(self):
        info = setup.probe_interpreter(sys.executable)
        self.assertEqual(info["implementation"], sys.implementation.name)
        self.assertEqual(info["version"], list(sys.version_info[:2]))
        self.assertEqual(info["free_threaded_build"], bool(sysconfig.get_config_var("Py_GIL_DISABLED")))
        if FROM_CHECKOUT:
            self.assertIsNone(info["package"])  # -I: PYTHONPATH does not reach it

    def test_the_probe_has_a_finite_timeout(self):
        # A probe that never answers is ended and refused (review 1, Sol R5: M18).
        def blocked(*args, **kwargs):
            timeout = kwargs.get("timeout")
            self.assertIsInstance(timeout, (int, float))
            self.assertTrue(0 < timeout < 3600)
            raise subprocess.TimeoutExpired(args[0], timeout)
        with mock.patch.object(setup.subprocess, "run", side_effect=blocked):
            with self.assertRaises(setup.InstallRefused) as caught:
                setup.probe_interpreter("/not/run/python3.14t")
        self.assertEqual(caught.exception.kind, "interpreter_unknown")

    def test_a_probe_that_prints_the_right_answer_but_fails_is_unknown(self):
        # Its exit status counts, not only what it prints (review 1, DeepSeek's list).
        with tempfile.TemporaryDirectory() as folder:
            program = os.path.join(folder, "python3.14t")
            answer = json.dumps({"implementation": "cpython", "version": [3, 14], "free_threaded_build": True,
                                 "package": PACKAGE})
            with open(program, "w", encoding="utf-8") as handle:
                handle.write(f"#!/bin/sh\necho '{answer}'\nexit 3\n")
            os.chmod(program, 0o700)
            with self.assertRaises(setup.InstallRefused) as caught:
                setup.probe_interpreter(program)
        self.assertEqual(caught.exception.kind, "interpreter_unknown")
        self.assertIn("exit 3", caught.exception.detail)

    def test_a_program_that_is_not_python_or_not_there_is_unknown(self):
        for program in ("/bin/true", "/nonexistent/bin/python3"):
            with self.subTest(program=program):
                with self.assertRaises(setup.InstallRefused) as caught:
                    setup.probe_interpreter(program)
                self.assertEqual(caught.exception.kind, "interpreter_unknown")

    @unittest.skipUnless(FROM_CHECKOUT, "run from the repository with PYTHONPATH")
    def test_install_from_a_checkout_is_refused_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            code = setup.run("install", out=out, roots=Roots(home=home), interpreter=sys.executable, package=PACKAGE)
            answer = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertIn(answer["code"], {"not_installed", "interpreter_not_free_threaded"})
            self.assertEqual(os.listdir(home), [])


class CommandFamilyShapeTests(unittest.TestCase):
    """The family has seam S2's shape (``control/commands/__init__.py`` on #369), so it can be registered as is."""

    def test_names_parser_and_dispatch(self):
        self.assertEqual(setup.NAMES, ("install", "upgrade", "uninstall"))
        added = []

        class Family:
            def add_parser(self, name, **kwargs):
                added.append(name)
                return mock.Mock()
        setup.parser(Family())
        self.assertEqual(tuple(added), setup.NAMES)
        self.assertEqual(list(inspect.signature(setup.dispatch).parameters), ["args", "client"])

    def test_dispatch_runs_the_parsed_command_with_this_interpreter(self):
        with mock.patch.object(setup, "run", return_value=0) as run, mock.patch.object(setup.sys, "platform", "linux"), \
                mock.patch.object(setup, "default_roots", return_value=Roots(home="/h")):
            out = io.StringIO()
            args = mock.Mock(command="upgrade", out=out)
            self.assertEqual(setup.dispatch(args, None), 0)
        run.assert_called_once_with("upgrade", out=out, roots=Roots(home="/h"), interpreter=sys.executable,
                                    package=PACKAGE)

    def test_dispatch_refuses_off_linux_before_it_reads_anything(self):
        # Once the family is registered in the control CLI, recursiond's own platform check is not in
        # front of it (review 1, follow-on).
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                with mock.patch.object(setup, "run") as run, mock.patch.object(setup.sys, "platform", platform), \
                        mock.patch.object(setup, "default_roots") as roots:
                    out = io.StringIO()
                    self.assertEqual(setup.dispatch(mock.Mock(command="uninstall", out=out), None), 1)
                answer = json.loads(out.getvalue())
                self.assertEqual((answer["code"], answer["action"]), ("platform_unsupported", "uninstall"))
                self.assertIn(platform, answer["detail"])
                run.assert_not_called()
                roots.assert_not_called()


if __name__ == "__main__":
    unittest.main()
