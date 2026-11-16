"""The terminal entry point without a terminal: line mode for screen readers and pipes."""

from __future__ import annotations

import contextlib
import io
import os
import unittest

from recursion_core.tui import term
from recursion_core.tui.tests.fakes import FakeDriver

# select() on a pipe is POSIX only (Windows refuses it with WinError 10038): the tests that read a real
# pipe through the terminal driver skip there. The mocked reads beside them run everywhere.
posix_select = unittest.skipIf(os.name == "nt", "select() on a pipe is POSIX only")


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


class TtyInputTest(unittest.TestCase):
    """R2 finding 10 (Astra's fix-10.patch, split into one test per defect so each fails on its own).

    Astra's EOF test ran interactive() on a thread and failed if it was still alive after 2 s. The
    preamble allows no wall-clock limit in a test, so this one counts reads instead: a terminal at the
    end of input may be read once, and a read after it reported the end fails the test at once."""

    @posix_select
    def test_tty_buffers_pasted_bytes_and_reports_eof(self) -> None:
        r_fd, w_fd = os.pipe()
        try:
            tty_in = object.__new__(term.Tty)
            tty_in.fd = r_fd
            os.write(w_fd, b":stop run-1\n")
            os.close(w_fd)
            w_fd = -1
            got = []
            for _ in range(32):
                k = tty_in.read_key(0.05)
                if not k:
                    break
                got.append(k)
            self.assertEqual(got, [":", *"stop run-1", "enter"])
            self.assertTrue(tty_in.eof)
        finally:
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    @posix_select
    def test_interactive_closes_and_exits_on_terminal_eof(self) -> None:
        eof_r, eof_w = os.pipe()
        os.close(eof_w)
        try:
            tty_eof = object.__new__(term.Tty)
            tty_eof.fd = eof_r
            tty_eof.cbreak = contextlib.nullcontext  # type: ignore[assignment]
            tty_eof.size = lambda: os.terminal_size((80, 24))  # type: ignore[assignment]
            real_read, reads = tty_eof.read_key, []

            def read_key(timeout: float):
                if getattr(tty_eof, "eof", False):
                    raise AssertionError("interactive() read the terminal again after it ended input")
                reads.append(timeout)
                if len(reads) > 100:
                    raise AssertionError("interactive() kept reading a terminal at the end of input")
                return real_read(timeout)

            tty_eof.read_key = read_key  # type: ignore[assignment]
            apps: list = []
            code = term.interactive(FakeDriver().api, io.StringIO(), {"NO_COLOR": "1"}, tty_eof,
                                    on_app=apps.append, stderr=io.StringIO())
            self.assertEqual(code, 0)
            self.assertTrue(apps[0].closed)
            self.assertEqual(len(reads), 1)
        finally:
            os.close(eof_r)

    def test_main_reports_a_failing_api_factory_as_unavailable(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = term.main(["--plain"], lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                         stdin=io.StringIO("q\n"), stdout=out, stderr=err, environ={})
        self.assertEqual(code, 1)
        self.assertIn("status=control_api_unavailable", err.getvalue())
        self.assertIn("factory:RuntimeError", err.getvalue())
        self.assertNotIn("boom", err.getvalue(), "the exception's text is not shown")
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
