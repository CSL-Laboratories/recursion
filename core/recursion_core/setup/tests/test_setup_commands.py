"""``recursiond install | upgrade | uninstall`` and ``setup containment``: what each prints, and the interpreter
checks."""

from __future__ import annotations

import contextlib
import errno
import inspect
import io
import json
import os
import socket
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
from recursion_core.setup import containment

PY = "/opt/recursion/venv/bin/python3.14t"
PY_NEXT = "/opt/recursion/venv-2/bin/python3.14t"
PACKAGE = recursion_core.__file__
FROM_CHECKOUT = bool(os.environ.get("PYTHONPATH"))  # how this suite is run in the repository


def _probe(**changes):
    info = {"implementation": "cpython", "version": [3, 14], "free_threaded_build": True, "package": PACKAGE}
    info.update(changes)
    return lambda interpreter: dict(info)


def _environ_without(*names):
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in names}, clear=True)


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

    def run_action(self, action, *, interpreter=PY, probe=None, package=PACKAGE, environ=None):
        out = io.StringIO()  # an empty environment unless one is given: XDG_* of the run's own are not read
        code = setup.run(action, out=out, roots=self.roots, interpreter=interpreter, package=package,
                         probe=probe or _probe(), environ={} if environ is None else environ)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0])

    def test_install_prints_ok_and_install_twice_changes_nothing(self):
        code, answer = self.run_action("install")
        self.assertEqual(code, 0)
        self.assertEqual(answer, {
            "code": "ok", "action": "install", "changed": True, "unit": self.unit, "interpreter": PY,
            "next": [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", UNIT_NAME]]})
        with open(self.unit, encoding="utf-8") as handle:
            first = handle.read()
        self.assertEqual(service_fields(first)["Service.ExecStart"], f"{PY} -I -X gil=0 -m recursion_core.daemon")
        code, answer = self.run_action("install")
        self.assertEqual((code, answer["changed"]), (0, False))
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

    def test_the_folder_named_is_under_the_resolved_home(self):
        # Review 4 of #382, DeepSeek (the named folder): a home reached through a link is named as every
        # other message names it, resolved.
        real_home = os.path.join(self._tmp.name, "real-home")
        os.rename(self.home, real_home)
        os.symlink(real_home, self.home)
        with mock.patch("os.fsync", side_effect=OSError(5, "Input/output error")):
            code, answer = self.run_action("install")
        self.assertEqual((code, answer["code"]), (1, "filesystem_error"))
        resolved = os.path.join(os.path.realpath(real_home), ".config", "systemd", "user")
        self.assertIn(f"(in {resolved})", answer["detail"])
        self.assertNotIn(self.home + "/", answer["detail"])

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
                _environ_without("XDG_CONFIG_HOME", "XDG_STATE_HOME"), \
                mock.patch.object(setup, "default_roots", return_value=self.roots), \
                mock.patch.object(setup, "probe_interpreter", _probe()), contextlib.redirect_stdout(out):
            code = entry.main(["install"])
        self.assertEqual(code, 0)
        answer = json.loads(out.getvalue())
        self.assertEqual((answer["code"], answer["interpreter"], answer["unit"]), ("ok", sys.executable, self.unit))

    # Review 4 of #382, Codex GPT-6.1 Sol (DeepSeek I7-2): an error opening the unit other than a link
    # answered not_regular, a refusal for a file's type, for a regular file the command could not open, and
    # lost the error. Sol's two regressions, and the socket case that stays a type refusal.
    def test_descriptors_exhausted_opening_the_unit_is_a_filesystem_error(self):
        import resource  # POSIX only, as this class is: at module level it fails the simulated-Windows import
        self.run_action("install")
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        held = []
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
            for _ in range(128):
                try:
                    held.append(os.open(os.devnull, os.O_RDONLY))
                except OSError as exc:
                    self.assertEqual(exc.errno, errno.EMFILE)
                    break
            else:
                self.fail("the lowered descriptor limit was not reached")
            code, answer = self.run_action("install")
        finally:
            for fd in held:
                os.close(fd)
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual((code, answer["code"]), (1, "filesystem_error"))
        self.assertIn("Too many open files", answer["detail"])

    def test_an_io_error_opening_the_unit_is_a_filesystem_error_for_every_command(self):
        self.run_action("install")
        with open(self.unit, "rb") as handle:
            before = handle.read()
        real_open = os.open

        def opening(path, flags, *args, **kwargs):
            if os.fspath(path) == self.unit:
                raise OSError(errno.EIO, "Input/output error")
            return real_open(path, flags, *args, **kwargs)
        for action in ("install", "upgrade", "uninstall"):
            with self.subTest(action=action):
                with mock.patch("os.open", side_effect=opening):
                    code, answer = self.run_action(action)
                self.assertEqual((code, answer["code"]), (1, "filesystem_error"))
                self.assertIn("Input/output error", answer["detail"])
        with open(self.unit, "rb") as handle:
            self.assertEqual(handle.read(), before)  # nothing rewritten or removed

    def test_a_socket_at_the_units_name_stays_not_regular(self):
        folder = self.home
        for name in (".config", "systemd", "user"):
            folder = os.path.join(folder, name)
            os.mkdir(folder, 0o700)
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        # Bound through its folder's descriptor: a short name whatever the folder's path (an absolute temporary
        # path can pass AF_UNIX's limit), and the working folder left alone (contextlib.chdir's os.getcwd()
        # raises where the process's working folder was removed). /proc/self/fd is Linux's; elsewhere (macOS)
        # the unit's own path is bound when it fits AF_UNIX's limit there, and the test is skipped when not.
        held = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, held)
        if os.path.isdir(f"/proc/self/fd/{held}"):
            server.bind(f"/proc/self/fd/{held}/{UNIT_NAME}")  # the non-blocking open takes a FIFO, but a socket is ENXIO
        elif len(os.fsencode(self.unit)) < 104:
            server.bind(self.unit)
        else:
            self.skipTest(f"no /proc/self/fd here, and the unit's path ({len(os.fsencode(self.unit))} bytes) is past "
                          "AF_UNIX's limit")
        for action in ("install", "upgrade", "uninstall"):
            with self.subTest(action=action):
                code, answer = self.run_action(action)
                self.assertEqual((code, answer["code"]), (1, "not_regular"))

    # The S6 integration sweep, F2 on #410 (P2): with XDG_CONFIG_HOME set, the user manager reads its units from
    # $XDG_CONFIG_HOME/systemd/user, and install wrote ~/.config/systemd/user and answered ok.
    def test_a_config_home_elsewhere_is_refused_before_anything_is_read(self):
        custom = os.path.join(self._tmp.name, "custom-config")
        for value in (custom, "relative/config", os.path.join(self.home, ".config", "x")):
            for action in ("install", "upgrade", "uninstall"):
                with self.subTest(value=value, action=action):
                    code, answer = self.run_action(action, probe=_refusing_probe, environ={"XDG_CONFIG_HOME": value})
                    self.assertEqual((code, answer["code"], answer["action"]), (1, "config_home_elsewhere", action))
                    self.assertIn(f"XDG_CONFIG_HOME is {value}", answer["detail"])
                    self.assertIn(f"`env -u XDG_CONFIG_HOME recursiond {action}`", answer["detail"])
                    self.assertEqual(os.listdir(self.home), [])
        # Read from this command's own environment when none is passed (the sweep's ready test).
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": custom}):
            out = io.StringIO()
            code = setup.run("install", out=out, roots=self.roots, interpreter=PY, package=PACKAGE, probe=_probe())
        self.assertEqual((code, json.loads(out.getvalue())["code"]), (1, "config_home_elsewhere"))
        self.assertEqual(os.listdir(self.home), [])
        # Unset, empty, or naming ~/.config itself: the default.
        config = os.path.join(self.home, ".config")
        for environ in ({}, {"XDG_CONFIG_HOME": ""}, {"XDG_CONFIG_HOME": config}, {"XDG_CONFIG_HOME": config + "/"}):
            with self.subTest(environ=environ):
                code, answer = self.run_action("install", environ=environ)
                self.assertEqual((code, answer["code"]), (0, "ok"))

    # The S6 integration sweep, F1 on #410 (P1), by this command's environment: a daemon started with
    # XDG_STATE_HOME keeps its state there, and the unit puts it in ~/.local/state/recursion.
    def test_a_state_home_elsewhere_is_refused_by_install_and_upgrade(self):
        custom = os.path.join(self._tmp.name, "custom-state")
        for value in (custom, "relative/state"):
            for action in ("install", "upgrade"):
                with self.subTest(value=value, action=action):
                    code, answer = self.run_action(action, probe=_refusing_probe, environ={"XDG_STATE_HOME": value})
                    self.assertEqual((code, answer["code"], answer["action"]), (1, "state_home_elsewhere", action))
                    self.assertIn(f"in {value}/recursion", answer["detail"])
                    self.assertIn(f"`env -u XDG_STATE_HOME recursiond {action}`", answer["detail"])
                    self.assertEqual(os.listdir(self.home), [])
        code, answer = self.run_action("uninstall", environ={"XDG_STATE_HOME": custom})  # it touches no state
        self.assertEqual((code, answer["code"], answer["changed"]), (0, "ok", False))
        state = os.path.join(self.home, ".local", "state")
        for environ in ({"XDG_STATE_HOME": ""}, {"XDG_STATE_HOME": state}):
            with self.subTest(environ=environ):
                code, answer = self.run_action("install", environ=environ)
                self.assertEqual((code, answer["code"]), (0, "ok"))

    def test_a_unit_that_keeps_the_state_elsewhere_is_printed_as_its_refusal(self):
        os.makedirs(os.path.dirname(self.unit), 0o700)
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write("[Service]\nEnvironment=XDG_STATE_HOME=/elsewhere\nExecStart=/old\n")
        code, answer = self.run_action("upgrade")
        self.assertEqual((code, answer["code"], answer["action"]), (1, "unit_state_elsewhere", "upgrade"))
        self.assertIn("sets XDG_STATE_HOME=/elsewhere", answer["detail"])
        # DeepSeek H9-1 on #382: a NUL byte in the value was a ValueError traceback; it is one JSON refusal.
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write("[Service]\nEnvironment=XDG_STATE_HOME=/a\0b\nExecStart=/old\n")
        code, answer = self.run_action("upgrade")
        self.assertEqual((code, answer["code"]), (1, "unit_state_elsewhere"))
        self.assertIn("sets XDG_STATE_HOME=/a:", answer["detail"])  # the NUL ends the line (Grok R1 of round 11)

    # The Liaison's item 4 (Nov 16): a password lookup that fails (CPython raises KeyError for no entry and for a
    # failed lookup alike) ended `recursiond install` with a traceback; it is the JSON refusal home_unknown.
    def test_a_password_lookup_that_fails_is_a_json_refusal(self):
        # DeepSeek F10-2 on #382: an OSError from the lookup is held to the same refusal (CPython raises KeyError).
        failures = (KeyError(f"getpwuid(): uid not found: {os.geteuid()}"), OSError(5, "Input/output error"))
        # setup answers before the lookup. install, upgrade, and uninstall still refuse home_unknown.
        home_actions = tuple(name for name in setup.NAMES if name != "setup")
        for failed, action in ((failed, action) for failed in failures for action in home_actions):
            with self.subTest(action=action, failed=type(failed).__name__):
                out = io.StringIO()
                with mock.patch.object(setup.sys, "platform", "linux"), mock.patch("pwd.getpwuid", side_effect=failed), \
                        mock.patch.object(setup, "run") as run:
                    self.assertEqual(setup.dispatch(mock.Mock(command=action, out=out), None), 1)
                answer = json.loads(out.getvalue())
                self.assertEqual((answer["code"], answer["action"]), ("home_unknown", action))
                self.assertIn("the password database gives no entry for this user", answer["detail"])
                run.assert_not_called()

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
            code = setup.run("install", out=out, roots=Roots(home=home), interpreter=sys.executable, package=PACKAGE,
                             environ={})
            answer = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertIn(answer["code"], {"not_installed", "interpreter_not_free_threaded"})
            self.assertEqual(os.listdir(home), [])


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "Linux file owners and modes, and a non-root uid")
class SetupContainmentTests(unittest.TestCase):
    """``recursiond setup containment [<value>]`` (Q17, Nov 15): the setting, set before any daemon runs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="setup-ct-")
        self.addCleanup(self._tmp.cleanup)
        base = os.path.realpath(self._tmp.name)
        os.chmod(base, 0o700)
        home = os.path.join(base, "home")  # the password database's home for this user, here (Q19)
        os.mkdir(home, 0o700)
        self.config = os.path.join(home, ".config")
        self.file = os.path.join(self.config, "recursion", "containment.json")
        passwd = mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=home))
        passwd.start()
        self.addCleanup(passwd.stop)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(base, "xdg")})  # not where it is read
        env.start()
        self.addCleanup(env.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def answer(self, *argv, code=0):
        got, out, _ = self.main(*argv)
        self.assertEqual(got, code, out)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def test_without_a_value_the_setting_is_shown_with_the_operators_words_and_nothing_is_made(self):
        self.assertEqual(self.answer("setup", "containment"), {
            "code": "ok", "action": "setup containment", "changed": False, "engine_homes": "inside_operator_home",
            "source": "default", "level": "same_user", "file": self.file, "notice": containment.OPERATOR_WORDS})
        self.assertFalse(os.path.lexists(self.config))

    def test_the_default_is_set_once_and_shown_from_the_file(self):
        first = self.answer("setup", "containment", "inside_operator_home")
        self.assertEqual((first["changed"], first["source"], first["notice"]),
                         (True, "file", containment.OPERATOR_WORDS))
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertFalse(self.answer("setup", "containment", "inside_operator_home")["changed"])
        self.assertEqual(self.answer("setup", "containment")["source"], "file")

    def test_outside_is_refused_without_an_engine_user_and_set_with_one(self):
        self.assertEqual(self.answer("setup", "containment", "outside_operator_home", code=1), {
            "code": "containment_needs_engine_user", "action": "setup containment",
            "detail": "outside_operator_home needs the engine user, which this machine does not have"})
        self.assertFalse(os.path.lexists(self.config))
        with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            answer = self.answer("setup", "containment", "outside_operator_home")
        self.assertEqual((answer["changed"], answer["engine_homes"], answer["level"]),
                         (True, "outside_operator_home", "engine_user"))
        self.assertNotIn("notice", answer)  # the words describe the default only

    def test_an_untrusted_file_is_refused_and_a_disk_error_is_named(self):
        os.makedirs(os.path.dirname(self.file), 0o700)
        os.symlink(os.path.join(self.config, "elsewhere.json"), self.file)
        for argv in (("setup", "containment"), ("setup", "containment", "inside_operator_home")):
            with self.subTest(argv=argv):
                answer = self.answer(*argv, code=1)
                self.assertEqual((answer["code"], answer["detail"]),
                                 ("containment_untrusted", f"{self.file} is a link"))
        self.assertTrue(os.path.islink(self.file))
        os.unlink(self.file)
        with mock.patch.object(containment, "write", side_effect=OSError(28, "No space left on device")):
            answer = self.answer("setup", "containment", "inside_operator_home", code=1)
        self.assertEqual(answer["code"], "filesystem_error")
        self.assertTrue(answer["detail"].endswith(f"(in {os.path.dirname(self.file)})"), answer["detail"])

    def test_a_missing_setting_or_an_unknown_value_is_a_usage_error(self):
        for argv in (("setup",), ("setup", "containment", "sideways"), ("setup", "sandbox"),
                     ("setup", "containment", "inside_operator_home", "again")):
            with self.subTest(argv=argv):
                code, out, err = self.main(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage error", err)
        self.assertFalse(os.path.lexists(self.config))

    def test_off_linux_the_setting_is_refused_before_anything_is_read(self):
        out = io.StringIO()
        with mock.patch.object(setup.sys, "platform", "win32"), mock.patch.object(containment, "read") as read:
            args = mock.Mock(command="setup", setting="containment", value=None, out=out)
            self.assertEqual(setup.dispatch(args, None), 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "platform_unsupported")
        read.assert_not_called()

    def test_setup_containment_answers_while_the_password_lookup_fails(self):
        # The setup branch returns before the lookup. install still answers the lookup's refusal.
        lookup = setup.InstallRefused(
            "home_unknown",
            "the password database gives no entry for this user (none, or the lookup failed), so the "
            "user manager's %h is not known")
        with mock.patch.object(setup, "default_roots", side_effect=lookup):
            shown = self.answer("setup", "containment")
            self.assertEqual((shown["code"], shown["action"]), ("ok", "setup containment"))
            refused = self.answer("install", code=1)
            self.assertEqual((refused["code"], refused["action"], refused["detail"]),
                             (lookup.kind, "install", lookup.detail))


class CommandFamilyShapeTests(unittest.TestCase):
    """The family has seam S2's shape (``control/commands/__init__.py`` on #369), so it can be registered as is."""

    def test_names_parser_and_dispatch(self):
        self.assertEqual(setup.NAMES, ("install", "upgrade", "uninstall", "setup"))
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
