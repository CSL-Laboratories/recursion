"""Witnesses for the terminal driver and for wrapping one wide glyph.

Bounded and in-memory: no real terminal, no child process.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import unittest
from unittest import mock

from recursion_core.tui import sanitize, term
from recursion_core.tui.app import TuiApp


class API:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"code": "ok", "runs": []}


class Terminal:
    eof = True
    burst = False

    def size(self):
        return os.terminal_size((80, 24))

    cbreak = staticmethod(contextlib.nullcontext)

    def read_key(self, timeout):
        return None


class QuietLoop:
    """Isolate driver lifetime from rendering threads and transports."""

    def __init__(self, app, api, **kwargs):
        self.app = app

    def start(self):
        pass

    def shutdown(self):
        return True


class SanitizerRegressionTests(unittest.TestCase):
    def test_f06_wide_character_fits_one_column(self):
        rows = sanitize.wrap("\u754c", 1)
        self.assertTrue(rows)
        self.assertTrue(all(sum(sanitize.cell_width(ch) for ch in row) <= 1 for row in rows), rows)
        self.assertLessEqual(sum(sanitize.cell_width(ch) for ch in sanitize.clip("\u754c", 1)), 1)


class DriverRegressionTests(unittest.TestCase):
    def test_f02_escape_continuations_share_a_deadline(self):
        tty = object.__new__(term.Tty)
        tty.fd = -1
        elapsed = 0.0
        calls = 0

        def read(timeout):
            nonlocal elapsed, calls
            calls += 1
            if calls == 1:
                elapsed += min(timeout, 0.001)
                return b"\x1b["
            if calls > 8:
                elapsed += timeout
                return None
            # Each ordinary parameter byte arrives 40 ms after the last.
            elapsed += min(timeout, 0.04)
            return b"1" if timeout >= 0.04 else None

        with mock.patch.object(tty, "_read", side_effect=read), \
                mock.patch.object(term.time, "monotonic", side_effect=lambda: elapsed):
            self.assertIsNone(tty.read_key(0.01))
        self.assertLessEqual(calls, 9)
        # Allow the documented extra ESC ambiguity wait, but never renew it.
        self.assertLessEqual(elapsed, 0.01 + tty.esc_wait_s + 1e-9)

    def test_f04_plain_ctrl_c_closes_and_returns_zero(self):
        api = API()
        app = TuiApp(api, clock=lambda: 0, environ={})

        def interrupted():
            raise KeyboardInterrupt
            yield "unreachable"

        caught = None
        result = None
        try:
            result = term.plain(api, interrupted(), io.StringIO(), {}, app=app)
        except KeyboardInterrupt as exc:
            caught = type(exc).__name__
        self.assertEqual((caught, result, app.closed), (None, 0, True))
        self.assertEqual(list(app.log)[-1].event_type, "close")
        self.assertEqual(api.calls, [("run.list", {})])

    def test_f04_plain_output_hangup_still_closes(self):
        class HungUp(io.StringIO):
            def write(self, text):
                raise OSError(errno.EIO, "synthetic terminal hangup")

        api = API()
        app = TuiApp(api, clock=lambda: 0, environ={})
        caught = None
        result = None
        try:
            result = term.plain(api, [], HungUp(), {}, app=app)
        except OSError as exc:
            caught = exc.errno
        self.assertEqual((caught, result, app.closed), (None, 0, True))

    def test_f05_initial_flush_failure_restores_screen_and_closes(self):
        class FailFirstFlush(io.StringIO):
            def __init__(self):
                super().__init__()
                self.failed = False

            def flush(self):
                if not self.failed:
                    self.failed = True
                    raise OSError(errno.EINTR, "synthetic interrupted flush")

        out, apps = FailFirstFlush(), []
        with mock.patch.object(term, "Loop", QuietLoop):
            with self.assertRaises(OSError):
                term.interactive(API(), out, {}, Terminal(), on_app=apps.append, stderr=io.StringIO())
        self.assertTrue(apps[0].closed, "app was left open after an initial flush failed")
        self.assertIn("\x1b[?25h\x1b[?1049l", out.getvalue())

    def test_f05_cleanup_failure_still_attempts_cursor_and_screen_restore(self):
        class FailPasteOffOnce(io.StringIO):
            def write(self, text):
                if text == term.PASTE_OFF:
                    raise OSError(errno.EINTR, "synthetic interrupted cleanup")
                return super().write(text)

        out, apps = FailPasteOffOnce(), []
        with mock.patch.object(term, "Loop", QuietLoop):
            with self.assertRaises(OSError):
                term.interactive(API(), out, {}, Terminal(), on_app=apps.append, stderr=io.StringIO())
        self.assertTrue(apps[0].closed)
        self.assertIn("\x1b[?25h\x1b[?1049l", out.getvalue())

    def test_f07_usage_diagnostic_omits_unrecognized_argument_values(self):
        # This is an ordinary placeholder, not a real credential or escape payload.
        out, err = io.StringIO(), io.StringIO()
        code = term.main(["--unknown=example-value"], None, stdin=io.StringIO(),
                         stdout=out, stderr=err, environ={})
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("example-value", err.getvalue())

    def test_D24_usage_diagnostics_escape_terminal_controls(self):
        out, err = io.StringIO(), io.StringIO()
        result = term.main(["--unknown=example\nFORGED\x1b[2J"], None, stdin=io.StringIO(),
                            stdout=out, stderr=err, environ={})
        self.assertEqual(result, 2)
        self.assertNotIn("\x1b", err.getvalue(), "usage diagnostic writes a live terminal escape")
        self.assertEqual(len(err.getvalue().splitlines()), 1, "argument forges a second diagnostic line")


if __name__ == "__main__":
    unittest.main()
