"""What the control CLI's socket client does on Windows: a typed refusal, asserted on a real Windows host.

The daemon's control socket is AF_UNIX under the user's runtime directory, which Windows does not
have (F40 follow-on). The CLI must answer ``control_api_unavailable`` with ``delivered: false`` and
exit 1, never a traceback, and must not try to connect. On a POSIX host the same class runs under
the simulation in child_env/tests/test_simulated_windows.py. Nothing is started.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from recursion_core.control import __main__ as entry
from recursion_core.control import cli
from recursion_core.control import tui as tui_entry
from recursion_core.control.client import UNAVAILABLE, SocketAPI, socket_directory, socket_path
from recursion_core.control_transport import PlatformNotSupported, TransportRefused, UnixControlClient

WINDOWS_ONLY = ("Windows-only assertions; a POSIX host runs them simulated "
                "(child_env/tests/test_simulated_windows.py)")
REFUSAL = {"code": UNAVAILABLE, "delivered": False, "detail": "platform_unsupported"}


def never_connect(*args, **kwargs):
    raise AssertionError("the socket client tried to connect on a platform without the Unix transport")


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def test_socket_location_is_a_typed_refusal(self) -> None:
        self.assertTrue(issubclass(PlatformNotSupported, TransportRefused))
        for locate in (socket_directory, socket_path):
            for environ in ({}, {"XDG_RUNTIME_DIR": "C:\\runtime"}):
                with self.assertRaisesRegex(PlatformNotSupported, "platform_unsupported"):
                    locate(environ)

    def test_default_socket_api_answers_unavailable_without_connecting(self) -> None:
        api = SocketAPI(connect=never_connect)
        self.assertIsNone(api.path)
        for method, params in (("run.list", {}), ("run.status", {"run_id": "r1"}), ("run.stop", {"run_id": "r1"}),
                               ("run.start", {"tenant": "t", "engine": "codex", "prompt": "p"})):
            self.assertEqual(REFUSAL, api.call(method, params))

    def test_explicit_path_gets_the_transport_refusal_as_unavailable(self) -> None:
        # The real client refuses in its constructor (PlatformNotSupported); a Stop is never reported stopped.
        api = SocketAPI("/run/user/1000/recursion/control.sock", connect=UnixControlClient)
        self.assertEqual(REFUSAL, api.call("run.stop", {"run_id": "r1"}))

    def test_cli_prints_the_refusal_and_exits_1_for_every_command(self) -> None:
        for argv in (["list"], ["status", "r1"], ["stop", "r1"], ["conditions"],
                     ["start", "--tenant", "t", "--engine", "codex", "--prompt", "p"]):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = entry.main(argv)
                self.assertEqual(cli.EXIT_ERROR, code)
                self.assertEqual("", err.getvalue())  # no traceback, no partial output
                answer = json.loads(out.getvalue())
                self.assertEqual(UNAVAILABLE, answer["code"])
                self.assertNotIn("Traceback", out.getvalue())
                if argv != ["conditions"]:  # conditions prints the code and its condition rows only
                    self.assertEqual(REFUSAL, answer)

    def test_tui_entry_shows_the_refusal_in_line_mode_and_connects_nowhere(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = tui_entry.main(["--plain"], stdin=io.StringIO("list\nstop r1\nq\n"), stdout=out, stderr=err, environ={})
        self.assertEqual((0, ""), (code, err.getvalue()))
        text = out.getvalue()
        self.assertIn("answer run.list status=control_api_unavailable", text)
        self.assertIn("detail=platform_unsupported", text)
        self.assertNotIn("status=stopped", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
