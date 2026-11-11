"""What the terminal driver does on Windows: a typed refusal before any screen output.

``Tty`` needs ``termios``, which Windows does not have. Line mode (``--plain``, a pipe) needs no
terminal and keeps working, so the rest of the TUI suite runs on Windows unchanged. On a POSIX host
this class runs under the simulation in child_env/tests/test_simulated_windows.py.
"""

from __future__ import annotations

import io
import os
import unittest

from recursion_core.tui import term
from recursion_core.tui.tests.fakes import FakeDriver

WINDOWS_ONLY = "Windows-only assertions; a POSIX host runs them simulated (child_env/tests/test_simulated_windows.py)"


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def test_the_real_terminal_refuses_without_termios(self) -> None:
        with self.assertRaisesRegex(term.TerminalUnsupported, "platform_unsupported"):
            term.Tty(_TtyStream())

    def test_full_screen_mode_is_a_text_refusal_and_paints_nothing(self) -> None:
        driver = FakeDriver()
        for argv in ([], ["--no-alt-screen"]):
            out, err = _TtyStream(), io.StringIO()
            with self.subTest(argv=argv):
                code = term.main(argv, lambda: driver.api, stdin=_TtyStream(), stdout=out, environ={}, stderr=err)
                self.assertEqual(code, 1)
                self.assertIn("status=platform_unsupported", err.getvalue())
                self.assertEqual(out.getvalue(), "", "no escape sequence reaches the screen")
        self.assertEqual(driver.model_calls, 0)

    def test_line_mode_needs_no_terminal(self) -> None:
        driver = FakeDriver()
        out = _TtyStream()
        code = term.main(["--plain"], lambda: driver.api, stdin=_TtyStream("q\n"), stdout=out, environ={})
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out.getvalue())


if __name__ == "__main__":
    unittest.main()
