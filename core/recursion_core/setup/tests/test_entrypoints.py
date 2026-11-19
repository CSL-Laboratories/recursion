"""The ``recursiond`` bootstrap: commands, refusals, and the re-exec that turns the GIL off."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest import mock

from recursion_core import _entrypoints as entry

CORE = os.path.dirname(os.path.dirname(os.path.abspath(entry.__file__)))
FREE_THREADED_314 = (sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14)
                     and bool(sysconfig.get_config_var("Py_GIL_DISABLED")))


def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


def _child_env(**extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["PYTHONPATH"] = CORE
    env.update(extra)
    return env


# ``recursiond doctor --json`` run as the module's own entry in a child process, with the
# doctor's account probes given argv[1] as their root (``doctor.run(root=)``). A call that
# asks this account (the password database, loginctl, systemctl) prints one line to stderr,
# which the test reads as empty. It covers what test_doctor.account_untouched covers
# (subprocess.run and subprocess.Popen with an argument list, check_operator_linger,
# pwd.getpwuid) and, like it, not a string command, os.system, os.posix_spawn or pwd.getpwnam.
_DOCTOR_AT_ROOT = """
import os, runpy, subprocess, sys
import recursion_core.daemon as daemon_module
import recursion_core.setup.doctor as doctor

root = sys.argv[1]


def refuse(name):
    def call(*_args, **_kwargs):
        print(name + " was called", file=sys.stderr)
        raise AssertionError(name + " was called")
    return call


real_subprocess_run = subprocess.run
real_popen = subprocess.Popen


def account_command(args, kwargs):
    command = args[0] if args else kwargs.get("args", ())
    if isinstance(command, (list, tuple)) and command:
        name = os.path.basename(str(command[0]))
        if name in ("loginctl", "systemctl"):
            return name
    return None


def guarded(*args, **kwargs):
    name = account_command(args, kwargs)
    if name is not None:
        return refuse(name)()
    return real_subprocess_run(*args, **kwargs)


class GuardedPopen(real_popen):
    def __init__(self, *args, **kwargs):
        name = account_command(args, kwargs)
        if name is not None:
            refuse(name)()
        super().__init__(*args, **kwargs)


subprocess.run = guarded
subprocess.Popen = GuardedPopen
daemon_module.check_operator_linger = refuse("check_operator_linger")
try:
    import pwd
except ImportError:
    pwd = None
if pwd is not None:
    pwd.getpwuid = refuse("pwd.getpwuid")
real_run = doctor.run
doctor.run = lambda probes=None: real_run(probes, root=root)
sys.argv = ["recursiond", "doctor", "--json"]
runpy.run_module("recursion_core._entrypoints", run_name="__main__", alter_sys=True)
"""


class HelpAndUsageTests(unittest.TestCase):
    def test_help_lists_every_command(self):
        code, out, _ = _main(["--help"])
        self.assertEqual(code, 0)
        for name in ("install", "upgrade", "uninstall", "serve", "Linux only"):
            self.assertIn(name, out)

    def test_a_command_has_its_own_help(self):
        # As on Linux: elsewhere ``install --help`` is refused platform_unsupported like any command
        # (PlatformTests), which child_env's simulated-Windows run of this file showed.
        with mock.patch.object(entry, "_linux", return_value=True):
            code, out, _ = _main(["install", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("per-user systemd unit", out)

    def test_no_command_and_an_unknown_command_are_usage_errors(self):
        with mock.patch.object(entry, "_linux", return_value=True):
            code, out, err = _main([])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("a command is required", err)
            code, out, err = _main(["frobnicate"])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("usage error", err)


class PlatformTests(unittest.TestCase):
    def test_every_command_refuses_off_linux_before_it_runs(self):
        with mock.patch.object(entry, "_linux", return_value=False), \
                mock.patch("recursion_core.control.commands.setup.dispatch") as dispatch, \
                mock.patch("os.execv") as execv:
            for argv in (["install"], ["upgrade"], ["uninstall"], ["serve"], ["serve", "--gui-port", "1"], ["doctor"],
                         ["install", "--help"]):
                with self.subTest(argv=argv):
                    code, out, _ = _main(argv)
                    self.assertEqual(code, 1)
                    answer = json.loads(out)
                    self.assertEqual(answer["code"], "platform_unsupported")
                    self.assertIn("Windows and macOS are not supported yet", answer["detail"])
            dispatch.assert_not_called()
            execv.assert_not_called()

    def test_help_still_prints_off_linux(self):
        with mock.patch.object(entry, "_linux", return_value=False):
            code, out, _ = _main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("install", out)

    def test_linux_is_this_system_here(self):
        self.assertEqual(entry._linux(), sys.platform.startswith("linux"))
        # The predicate reads the platform, not the host it happens to run on (review 1, Sol R5: M16).
        for platform, linux in (("win32", False), ("darwin", False), ("freebsd14", False), ("linux", True)):
            with self.subTest(platform=platform), mock.patch.object(entry.sys, "platform", platform):
                self.assertIs(entry._linux(), linux)


class ServeTests(unittest.TestCase):
    def test_serve_refuses_a_build_the_daemon_cannot_run_and_execs_nothing(self):
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(entry, "_daemon_build", return_value=False), mock.patch("os.execv") as execv:
            code, out, _ = _main(["serve"])
        self.assertEqual(code, 1)
        answer = json.loads(out)
        self.assertEqual(answer["code"], "build_not_free_threaded")
        self.assertIn("python3.14t", answer["detail"])
        execv.assert_not_called()

    def test_serve_replaces_the_process_with_the_daemon_module_and_the_gil_off(self):
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(entry, "_daemon_build", return_value=True), mock.patch("os.execv") as execv:
            entry.main(["serve", "--gui-port", "9000", "--plugin", "/p"])
        execv.assert_called_once_with(sys.executable, [sys.executable, "-X", "gil=0", "-m", "recursion_core.daemon",
                                                       "--gui-port", "9000", "--plugin", "/p"])

    def test_serve_refuses_an_interpreter_without_an_absolute_path(self):
        for executable in ("", "bin/python3.14t"):  # a relative one too (review 1, R6: M15)
            with self.subTest(executable=executable):
                with mock.patch.object(entry, "_linux", return_value=True), \
                        mock.patch.object(entry, "_daemon_build", return_value=True), \
                        mock.patch.object(entry.sys, "executable", executable), mock.patch("os.execv") as execv:
                    code, out, _ = _main(["serve"])
                self.assertEqual((code, json.loads(out)["code"]), (1, "interpreter_unknown"))
                execv.assert_not_called()

    def test_serve_turns_a_failed_exec_into_a_refusal(self):
        # execv returns only by raising (review 1, R4).
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(entry, "_daemon_build", return_value=True), \
                mock.patch("os.execv", side_effect=FileNotFoundError(2, "No such file or directory")):
            code, out, _ = _main(["serve"])
        answer = json.loads(out)
        self.assertEqual((code, answer["code"]), (1, "interpreter_unknown"))
        self.assertIn("FileNotFoundError", answer["detail"])
        self.assertIn(sys.executable, answer["detail"])

    def test_the_build_check_is_true_exactly_on_free_threaded_cpython_314(self):
        self.assertEqual(entry._daemon_build(), FREE_THREADED_314)
        with mock.patch("sysconfig.get_config_var", return_value=0):
            self.assertFalse(entry._daemon_build())
        with mock.patch.object(entry.sys, "version_info", (3, 13, 0, "final", 0)), \
                mock.patch("sysconfig.get_config_var", return_value=1):
            self.assertFalse(entry._daemon_build())
        # Another implementation of 3.14 with the same build flag (review 1, R6: M14).
        with mock.patch.object(entry.sys, "implementation", mock.Mock(name="impl")) as implementation, \
                mock.patch.object(entry.sys, "version_info", (3, 14, 0, "final", 0)), \
                mock.patch("sysconfig.get_config_var", return_value=1):
            implementation.name = "pypy"
            self.assertFalse(entry._daemon_build())
            implementation.name = "cpython"
            self.assertTrue(entry._daemon_build())

    @unittest.skipUnless(FREE_THREADED_314 and sys.platform.startswith("linux"), "needs free-threaded CPython 3.14 on Linux")
    def test_serve_really_turns_the_gil_off_in_the_daemon(self):
        # The daemon's own entry checks the GIL before it reads --plugin: a relative --plugin is a
        # usage error (exit 2) only once the GIL check has passed. PYTHON_GIL=1 asks for the GIL.
        with tempfile.TemporaryDirectory() as cwd:
            served = subprocess.run([sys.executable, "-B", "-m", "recursion_core._entrypoints", "serve",
                                     "--plugin", "relative/dir"], cwd=cwd, env=_child_env(PYTHON_GIL="1"),
                                    capture_output=True, text=True)
            direct = subprocess.run([sys.executable, "-B", "-m", "recursion_core.daemon", "--plugin", "relative/dir"],
                                    cwd=cwd, env=_child_env(PYTHON_GIL="1"), capture_output=True, text=True)
        self.assertEqual(served.returncode, 2, served.stderr)
        self.assertIn("--plugin must be an absolute directory path", served.stderr)
        self.assertEqual(direct.returncode, 1, direct.stderr)  # the same daemon, started without the bootstrap
        self.assertIn("Requires free-threaded CPython 3.14t with -X gil=0", direct.stderr)

    @unittest.skipIf(FREE_THREADED_314, "this build can run the daemon")
    @unittest.skipUnless(sys.platform.startswith("linux"), "the child runs on this host: Linux only")
    def test_serve_refuses_this_build_for_real(self):
        # The child is a real interpreter on this host, whatever this process's sys.platform says (the
        # simulated-Windows run sets it to win32); refusing off Linux is PlatformTests' (mocked).
        with tempfile.TemporaryDirectory() as cwd:
            done = subprocess.run([sys.executable, "-B", "-m", "recursion_core._entrypoints", "serve"],
                                  cwd=cwd, env=_child_env(), capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(json.loads(done.stdout)["code"], "build_not_free_threaded")


class BootstrapHygieneTests(unittest.TestCase):
    def test_the_bootstrap_imports_only_the_standard_library(self):
        with open(entry.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add(node.module)
        self.assertEqual(top, {"__future__", "argparse", "json", "os", "sys", "sysconfig"})
        nested = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} - top
        self.assertEqual(nested, {"recursion_core.control.commands"})  # only after the platform check

    def test_importing_the_bootstrap_loads_nothing_else_of_recursion_core(self):
        code = ("import json, sys; import recursion_core._entrypoints; "
                "print(json.dumps(sorted(m for m in sys.modules if m.startswith('recursion_core'))))")
        with tempfile.TemporaryDirectory() as cwd:
            done = subprocess.run([sys.executable, "-B", "-c", code], cwd=cwd, env=_child_env(),
                                  capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), ["recursion_core", "recursion_core._entrypoints"])

    def test_the_module_runs_as_a_script_entry(self):
        with tempfile.TemporaryDirectory() as cwd:
            done = subprocess.run([sys.executable, "-B", "-m", "recursion_core._entrypoints", "--help"],
                                  cwd=cwd, env=_child_env(), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("recursiond", done.stdout)


class DoctorCommandTests(unittest.TestCase):
    """``recursiond doctor`` prints one report. No daemon has to be running."""

    def _account_of_the_test(self):
        """``(home, env)``: a home, runtime, temp and empty PATH folders in one directory this test made.

        The doctor's account probes take that home as their root, so no run here reads this
        account's unit, linger state, user manager or home. PATH holds no engine binary, so the
        engine probe runs none.
        """

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = os.path.join(folder.name, "home")
        runtime = os.path.join(folder.name, "runtime")
        scratch = os.path.join(folder.name, "tmp")
        empty_path = os.path.join(folder.name, "bin")
        for path in (home, runtime, scratch, empty_path):
            os.mkdir(path, 0o700)
        return home, {
            "HOME": home,
            "PATH": empty_path,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
            "XDG_RUNTIME_DIR": runtime,
            "TMPDIR": scratch,
        }

    def _assert_refused_off_linux(self, code, out, err, asked):
        """Off Linux (the simulated-Windows step) ``doctor`` refuses, typed, before the doctor runs."""

        self.assertEqual((code, err), (1, ""))
        answer = json.loads(out)
        self.assertEqual(answer["code"], "platform_unsupported")
        self.assertIn("Windows and macOS are not supported yet", answer["detail"])
        self.assertEqual(asked, [])

    @contextlib.contextmanager
    def _at_root(self, home, env, asked):
        """The doctor in this process with ``doctor.run(root=home)``; a call that asks this account lands in ``asked``."""

        import recursion_core.setup.doctor as doctor
        from recursion_core.setup.tests.test_doctor import account_untouched

        real_run = doctor.run

        def run_at_root(probes=None):
            return real_run(probes, root=home)

        with mock.patch.dict(os.environ, env), mock.patch.object(tempfile, "tempdir", env["TMPDIR"]), \
                mock.patch.object(doctor, "run", run_at_root), account_untouched(asked):
            yield

    def test_doctor_json_prints_one_report_and_exits_1_when_a_check_is_fail(self):
        # Today this command is unknown: exit 2, and nothing is printed.
        home, env = self._account_of_the_test()
        asked = []
        with self._at_root(home, env, asked):
            code, out, err = _main(["doctor", "--json"])
        if not entry._linux():
            self._assert_refused_off_linux(code, out, err, asked)
            return
        self.assertNotEqual(code, 2, f"doctor is still an unknown command\n{err}\n{out}")
        self.assertEqual(err, "")
        self.assertTrue(out.endswith("\n") and not out.endswith("\n\n"), out)
        report = json.loads(out)
        self.assertEqual(out, json.dumps(report, sort_keys=True) + "\n")
        from recursion_core.setup.tests.test_doctor import _validate
        _validate(report)
        socket = next(check for check in report["checks"] if check["id"] == "daemon.socket")
        self.assertIn(socket["state"], ("fail", "unknown"))
        self.assertNotEqual(socket["state"], "ok")
        self.assertEqual(socket["state"], "fail")  # the runtime directory has no socket
        failed = any(check["state"] == "fail" for check in report["checks"])
        self.assertEqual(code, 1 if failed else 0)
        with self._at_root(home, env, asked):
            again, again_out, again_err = _main(["doctor", "--json"])
        self.assertEqual(again_err, "")
        again_report = json.loads(again_out)
        _validate(again_report)
        self.assertIsNot(again_report, report)
        again_failed = any(check["state"] == "fail" for check in again_report["checks"])
        self.assertEqual(again, 1 if again_failed else 0)
        self.assertEqual(asked, [])

    def test_doctor_without_json_prints_text(self):
        home, env = self._account_of_the_test()
        asked = []
        with self._at_root(home, env, asked):
            code, out, err = _main(["doctor"])
        if not entry._linux():
            self._assert_refused_off_linux(code, out, err, asked)
            return
        self.assertNotEqual(code, 2, err or out)
        self.assertEqual(err, "")
        self.assertFalse(out.lstrip().startswith("{"), out)
        self.assertIn("platform.linux", out)
        self.assertIn("daemon.socket", out)
        self.assertTrue(out.endswith("\n"))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)
        self.assertEqual(asked, [])

    def test_help_lists_doctor(self):
        code, out, _ = _main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("doctor", out)
        with mock.patch.object(entry, "_linux", return_value=True):
            code, out, _ = _main(["doctor", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--json", out)

    def test_an_interruption_propagates_and_a_probe_error_is_not_swallowed(self):
        import recursion_core.setup.doctor as doctor
        if not entry._linux():
            # The refusal comes first, so the doctor (here one that raises) is never called.
            with mock.patch.object(doctor, "run", side_effect=KeyboardInterrupt) as run:
                code, out, err = _main(["doctor", "--json"])
            run.assert_not_called()
            self._assert_refused_off_linux(code, out, err, [])
            return
        with mock.patch.object(doctor, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _main(["doctor", "--json"])
        with mock.patch.object(doctor, "run", side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit):
                _main(["doctor"])
        with mock.patch.object(doctor, "run", side_effect=RuntimeError("probe")):
            with self.assertRaises(RuntimeError):
                _main(["doctor", "--json"])

    def test_doctor_json_as_a_process_prints_one_object(self):
        home, env = self._account_of_the_test()
        with tempfile.TemporaryDirectory() as cwd:
            done = subprocess.run(
                [sys.executable, "-B", "-c", _DOCTOR_AT_ROOT, home],
                cwd=cwd, env=_child_env(**env), capture_output=True, text=True,
            )
            # The child is a real interpreter on this host, so the branch is entry._linux() read in
            # the child: under the simulated-Windows step this process says win32 while the child
            # runs as Linux; on a Windows or macOS host the child refuses, typed, before the doctor.
            linux = subprocess.run(
                [sys.executable, "-B", "-c", "from recursion_core import _entrypoints as e; print(e._linux())"],
                cwd=cwd, env=_child_env(**env), capture_output=True, text=True,
            )
        self.assertIn(linux.stdout, ("True\n", "False\n"), linux.stderr)
        if linux.stdout == "False\n":
            self._assert_refused_off_linux(done.returncode, done.stdout, done.stderr, [])
            return
        self.assertNotEqual(done.returncode, 2, done.stderr or done.stdout)
        self.assertEqual(done.stderr, "")
        report = json.loads(done.stdout)
        self.assertEqual(done.stdout, json.dumps(report, sort_keys=True) + "\n")
        from recursion_core.setup.tests.test_doctor import _validate
        _validate(report)
        socket = next(check for check in report["checks"] if check["id"] == "daemon.socket")
        self.assertIn(socket["state"], ("fail", "unknown"))
        self.assertNotEqual(socket["state"], "ok")
        failed = any(check["state"] == "fail" for check in report["checks"])
        self.assertEqual(done.returncode, 1 if failed else 0)


if __name__ == "__main__":
    unittest.main()
