"""The terminal entry point without a terminal: line mode for screen readers and pipes."""

from __future__ import annotations

import contextlib
import io
import os
import pty
import tty
import unittest

from recursion_core.tui import term
from recursion_core.tui.tests.fakes import FakeDriver


class PlainModeTest(unittest.TestCase):
    def test_not_a_tty_runs_line_mode_and_closing_stops_nothing(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        out = io.StringIO()
        code = term.main([], lambda: driver.api, stdin=io.StringIO(f"status {run_id}\nq\nstop {run_id}\n"),
                         stdout=out, environ={})
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn(f"answer run.status status=ok", text)
        self.assertNotIn("run.stop", text, "lines after q are not read")
        self.assertNotIn("\x1b", text)
        self.assertEqual(driver.registry.get(run_id)["state"], "running")
        self.assertEqual(driver.model_calls, 1)

    def test_no_api_client_is_a_text_error(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(term.main(["--plain"], None, stdin=io.StringIO(""), stdout=out, environ={}, stderr=err), 1)
        self.assertIn("status=control_api_unavailable", err.getvalue())
        self.assertEqual(out.getvalue(), "", "diagnostics go to stderr (O7)")


class _Fd:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class TtyReadTest(unittest.TestCase):
    """Bytes the kernel has already delivered are keys, and a hangup is the end of input."""

    def setUp(self) -> None:
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.terminal = term.Tty(_Fd(self.slave))

    def tearDown(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_one_read_yields_every_key(self) -> None:
        os.write(self.master, b"sy")
        self.assertEqual(self.terminal.read_key(0.5), "s")
        self.assertEqual(self.terminal.read_key(0.5), "y")

    def test_an_arrow_key_does_not_swallow_the_next_key(self) -> None:
        os.write(self.master, b"\x1b[Aj")
        self.assertEqual(self.terminal.read_key(0.5), "up")
        self.assertEqual(self.terminal.read_key(0.5), "j")

    def test_a_utf8_character_split_across_reads_is_one_key(self) -> None:
        raw = "你".encode()
        os.write(self.master, raw[:1])
        self.assertIsNone(self.terminal.read_key(0.2))
        os.write(self.master, raw[1:])
        self.assertEqual(self.terminal.read_key(0.5), "你")

    def test_esc_after_a_utf8_lead_is_escape_not_payload(self) -> None:
        os.write(self.master, b"\xe4\x1b")
        self.assertEqual(self.terminal.read_key(0.5), "\ufffd")
        # The ESC still waits out the arrow-key window before it is delivered.
        self.assertEqual(self.terminal.read_key(0.3), "esc")

    def test_a_utf8_lead_does_not_swallow_the_following_bytes(self) -> None:
        os.write(self.master, b"\xe4\x1bA")
        self.assertEqual(self.terminal.read_key(0.5), "\ufffd")
        self.assertEqual(self.terminal.read_key(0.5), "esc")
        self.assertEqual(self.terminal.read_key(0.5), "A")
        os.write(self.master, b"\xe4Z")
        self.assertEqual(self.terminal.read_key(0.5), "\ufffd")
        self.assertEqual(self.terminal.read_key(0.5), "Z")
        os.write(self.master, b"\xe4\x1b[Aj")
        self.assertEqual(self.terminal.read_key(0.5), "\ufffd")
        self.assertEqual(self.terminal.read_key(0.5), "up")
        self.assertEqual(self.terminal.read_key(0.5), "j")

    def test_eof_is_reported_instead_of_a_ready_empty_read(self) -> None:
        os.close(self.master)
        self.master = -1
        with self.assertRaises(EOFError):
            self.terminal.read_key(0.5)


class EofClosesTest(unittest.TestCase):
    def test_stdin_eof_closes_and_leaves_the_alternate_screen(self) -> None:
        class Hangup:
            def size(self) -> os.terminal_size:
                return os.terminal_size((40, 12))

            @contextlib.contextmanager
            def cbreak(self):
                yield

            def read_key(self, timeout: float) -> str | None:
                raise EOFError

        driver = FakeDriver()
        out = io.StringIO()
        code = term.interactive(driver.api, out, {"NO_COLOR": "1"}, Hangup())
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("\x1b[?1049h"))
        self.assertTrue(text.endswith("\x1b[?1049l"))
        self.assertEqual(driver.model_calls, 0)


if __name__ == "__main__":
    unittest.main()
