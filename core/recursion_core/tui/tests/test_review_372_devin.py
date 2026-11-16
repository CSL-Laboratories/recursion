"""The open Devin thread on #372 (``term.py``, the error lines after the alternate screen), answered on #404 at the
Liaison's word (Nov 15, 4:18 PM): after the screen output hung up, the error lines were not written at all, though
stderr may be another stream that still works (a log file). They now go to stderr whenever the TUI leaves the
alternate screen; an EIO on stderr itself (the same hung-up terminal) is still taken quietly, and any other error
on it is raised. The first test is red on 68c1d31e.
"""

from __future__ import annotations

import errno
import io
import unittest
from unittest import mock

from recursion_core.tui import term
from recursion_core.tui.tests.fakes import FakeDriver
from recursion_core.tui.tests.test_inline import FakeTerminal


class HungUpScreen(io.StringIO):
    """The terminal's output: every write is the hangup."""

    def write(self, text):
        raise OSError(errno.EIO, "hung-up output")


class FailingErr(io.StringIO):
    def __init__(self, number: int) -> None:
        super().__init__()
        self.number = number

    def write(self, text):
        raise OSError(self.number, "stderr failure")


class SyncLoop:
    """Saves one error and ends the TUI, on the calling thread."""

    def __init__(self, app, *args, **kwargs) -> None:
        self.app = app

    def start(self) -> None:
        self.app.errors.append("saved diagnostic")
        self.app.close()

    def shutdown(self) -> bool:
        return True


class StderrAfterHangupTest(unittest.TestCase):
    def run_tui(self, stderr) -> int:
        with mock.patch.object(term, "Loop", SyncLoop):
            return term.interactive(FakeDriver().api, HungUpScreen(), {"NO_COLOR": "1"}, FakeTerminal([]),
                                    stderr=stderr)

    def test_the_error_lines_reach_a_working_stderr_after_the_screen_hung_up(self) -> None:
        err = io.StringIO()
        self.assertEqual(self.run_tui(err), 0)
        self.assertIn("error: saved diagnostic", err.getvalue())

    def test_an_eio_on_stderr_too_is_taken_quietly(self) -> None:
        self.assertEqual(self.run_tui(FailingErr(errno.EIO)), 0)

    def test_another_error_on_stderr_is_raised(self) -> None:
        with self.assertRaises(OSError):
            self.run_tui(FailingErr(errno.EBADF))


if __name__ == "__main__":
    unittest.main()
