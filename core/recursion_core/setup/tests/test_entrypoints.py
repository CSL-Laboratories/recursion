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


class HelpAndUsageTests(unittest.TestCase):
    def test_help_lists_every_command(self):
        code, out, _ = _main(["--help"])
        self.assertEqual(code, 0)
        for name in ("install", "upgrade", "uninstall", "serve", "Linux only"):
            self.assertIn(name, out)

    def test_a_command_has_its_own_help(self):
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
            for argv in (["install"], ["upgrade"], ["uninstall"], ["serve"], ["serve", "--gui-port", "1"], ["doctor"]):
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

    def test_serve_refuses_an_interpreter_without_a_path(self):
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(entry, "_daemon_build", return_value=True), \
                mock.patch.object(entry.sys, "executable", ""), mock.patch("os.execv") as execv:
            code, out, _ = _main(["serve"])
        self.assertEqual((code, json.loads(out)["code"]), (1, "interpreter_unknown"))
        execv.assert_not_called()

    def test_the_build_check_is_true_exactly_on_free_threaded_cpython_314(self):
        self.assertEqual(entry._daemon_build(), FREE_THREADED_314)
        with mock.patch("sysconfig.get_config_var", return_value=0):
            self.assertFalse(entry._daemon_build())
        with mock.patch.object(entry.sys, "version_info", (3, 13, 0, "final", 0)), \
                mock.patch("sysconfig.get_config_var", return_value=1):
            self.assertFalse(entry._daemon_build())

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
    def test_serve_refuses_this_build_for_real(self):
        with tempfile.TemporaryDirectory() as cwd:
            done = subprocess.run([sys.executable, "-B", "-m", "recursion_core._entrypoints", "serve"],
                                  cwd=cwd, env=_child_env(), capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stderr)
        expected = "build_not_free_threaded" if sys.platform.startswith("linux") else "platform_unsupported"
        self.assertEqual(json.loads(done.stdout)["code"], expected)


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


if __name__ == "__main__":
    unittest.main()
