"""``python -m recursion_core.control.tui``: the TUI wired to the daemon's control socket (CSL-799).

The daemon-up path (start, list, status and stop acting on a real composed daemon) is in
daemon/tests/test_composition.py. Here: the entry hands the TUI the same socket client as the CLI,
a daemon that is down reads ``control_api_unavailable`` with no traceback, and loading the entry
keeps the GIL off on a free-threaded build. No daemon, engine or network is started.
"""

from __future__ import annotations

import io
import os
import pathlib
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest import mock

from recursion_core.control import __main__ as cli_entry
from recursion_core.control import tui as tui_entry
from recursion_core.control.client import SocketAPI

CORE = pathlib.Path(__file__).resolve().parents[3]
POSIX_ONLY = ("POSIX-only: the daemon-down case needs a runtime directory to look for the socket in; on Windows the "
              "same entry answers platform_unsupported, covered by control/tests/test_windows_refusal.py")


class TuiEntryTests(unittest.TestCase):
    def test_the_tui_and_the_cli_use_the_same_api_factory(self) -> None:
        with mock.patch.object(tui_entry.term, "main", return_value=7) as term_main:
            self.assertEqual(tui_entry.main(["--plain"], stdout=io.StringIO()), 7)
        self.assertEqual(term_main.call_args.args[0], ["--plain"])
        self.assertIs(term_main.call_args.args[1], SocketAPI)
        with mock.patch.object(cli_entry.cli, "run", return_value=0) as cli_run:
            cli_entry.main(["list"])
        self.assertIs(cli_run.call_args.args[1], SocketAPI)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "getuid"), POSIX_ONLY)
    def test_daemon_down_shows_unavailable_and_raises_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rc-tui-") as runtime:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                code = tui_entry.main(["--plain"], stdin=io.StringIO("list\nstatus run-1\nstop run-1\n"
                                                                     "start --tenant t --engine codex --prompt p\nq\n"),
                                      stdout=out, stderr=err, environ={})
            self.assertEqual(os.listdir(runtime), [], "a client never creates the socket directory")
        text = out.getvalue()
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertIn("answer run.list status=control_api_unavailable", text)
        self.assertIn("answer run.stop status=control_api_unavailable", text)
        self.assertIn("UNAVAILABLE: control API unavailable", text)
        self.assertNotIn("status=stopped", text)  # a Stop with no answer is never shown as stopped
        self.assertNotIn("status=started", text)
        self.assertNotIn("Traceback", text)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "getuid"), POSIX_ONLY)
    def test_packaged_entry_point_with_the_daemon_down_has_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rc-tui-") as runtime:
            done = subprocess.run([sys.executable, "-B", "-m", "recursion_core.control.tui", "--plain"], input="list\nq\n",
                                  text=True, capture_output=True, timeout=60, check=False,
                                  env={"PYTHONPATH": str(CORE), "XDG_RUNTIME_DIR": runtime, "PATH": ""})
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("status=control_api_unavailable", done.stdout)
        self.assertNotIn("Traceback", done.stdout)

    @unittest.skipUnless(sysconfig.get_config_var("Py_GIL_DISABLED"), "free-threaded build only")
    def test_loading_the_entry_keeps_the_gil_off_without_forcing_it(self) -> None:
        # As tui/tests/test_imports.py does for the TUI itself: no -X gil=0 and no PYTHON_GIL, so an
        # extension module that turned the GIL back on would show (CPython warns with a RuntimeWarning).
        code = "import sys, recursion_core.control.tui; print(sys._is_gil_enabled())"
        env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"} | {"PYTHONPATH": str(CORE)}
        out = subprocess.run([sys.executable, "-B", "-W", "error::RuntimeWarning", "-c", code], cwd=CORE,
                             capture_output=True, text=True, env=env, check=True).stdout.strip()
        self.assertEqual(out, "False")


if __name__ == "__main__":
    unittest.main()
