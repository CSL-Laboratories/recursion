"""The real terminal driver on private PTYs: raw input mode, hangup, and shutdown (the two Codex reviews of
#372 at 9a386914, GPT-6 Astra and GPT-6.1 Sol; their probes are kept here as regressions, with credit).

Each PTY is opened by the test and has no controlling foreground process, so no signal reaches any
process. No test waits on a clock: the shutdown case synchronises on events.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import threading
import unittest
from unittest import mock

from recursion_core.tui import term
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.keys import Paste
from recursion_core.tui.tests.fakes import Clock, FakeDriver
from recursion_core.tui.tests.test_inline import FakeTerminal

try:  # POSIX only; the Windows refusal is tested in test_windows_refusal
    import pty
    import termios
except ImportError:  # pragma: no cover - Windows
    pty = termios = None


def bare_tty(fd: int) -> term.Tty:
    tty = object.__new__(term.Tty)
    tty.fd = fd
    return tty


@unittest.skipIf(pty is None, "needs POSIX terminals")
class RawModeTest(unittest.TestCase):
    def test_raw_input_mode_and_the_saved_mode_back(self) -> None:
        master, slave = pty.openpty()
        try:
            before = termios.tcgetattr(slave)
            with bare_tty(slave).cbreak():
                now = termios.tcgetattr(slave)
                for flag in (termios.ISIG, termios.ICANON, termios.ECHO, termios.IEXTEN):
                    self.assertFalse(now[3] & flag, flag)
                for flag in (termios.IXON, termios.ICRNL):
                    self.assertFalse(now[0] & flag, flag)
                self.assertEqual(now[1], before[1], "output processing is left as it was")
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            os.close(master)
            os.close(slave)

    def test_a_control_character_stays_inside_a_bracketed_paste(self) -> None:
        # Astra R2: with signals on, Ctrl-C flushed the pending input, opener included.
        master, slave = pty.openpty()
        tty = bare_tty(slave)
        try:
            with tty.cbreak():
                os.write(master, b"\x1b[200~before\x03after\x1a\x13\x16end\x1b[201~")
                got = []
                for _ in range(16):
                    key = tty.read_key(0)
                    if key is None:
                        break
                    got.append(key)
            self.assertEqual(got, [Paste("before\x03after\x1a\x13\x16end")])
        finally:
            os.close(master)
            os.close(slave)

    def test_typed_ctrl_c_is_a_key_and_closes_from_any_mode(self) -> None:
        master, slave = pty.openpty()
        tty = bare_tty(slave)
        try:
            with tty.cbreak():
                os.write(master, b"\x03")
                self.assertEqual(tty.read_key(0), "ctrl+c")
        finally:
            os.close(master)
            os.close(slave)
        for keys in ([], [":", "l"], ["s"]):
            with self.subTest(keys=keys):
                driver = FakeDriver()
                driver.start_run()
                app = TuiApp(driver.api, clock=Clock(), environ={"NO_COLOR": "1"})
                app.open()
                for key in keys:
                    app.press(key)
                app.press("ctrl+c")
                self.assertTrue(app.closed)
                self.assertEqual(driver.registry.get("run-1")["state"], "running", "Ctrl-C stops nothing")

    def test_keys_and_a_paste_through_a_real_pty(self) -> None:
        # Sol's round trip and Astra's UTF-8 paste.
        master, slave = pty.openpty()
        tty = bare_tty(slave)
        try:
            with tty.cbreak():
                cafe = "caf" + chr(0xE9)
                os.write(master, b"\x1b[C\x1b[H\x1b[Z\x1b[200~stop run-1\x1b[201~" + f"\x1b[200~{cafe}\x1b[201~".encode())
                got = []
                for _ in range(10):
                    key = tty.read_key(0)
                    if key is None:
                        break
                    got.append(key)
            self.assertEqual(got, ["right", "home", "shift+tab", Paste("stop run-1"), Paste(cafe)])
        finally:
            os.close(master)
            os.close(slave)


@unittest.skipIf(pty is None, "needs POSIX terminals")
class HangupTest(unittest.TestCase):
    """Astra R1, Sol R2: a hangup is a clean end through restoring the mode and the last writes; only
    the hangup's EIO is taken as that end."""

    def test_restoring_the_mode_of_a_hung_up_pty_is_clean(self) -> None:
        master, slave = pty.openpty()
        tty = bare_tty(slave)
        try:
            with tty.cbreak():
                os.close(master)
                master = -1
                self.assertIsNone(tty.read_key(0))
                self.assertTrue(tty.eof)
        finally:
            if master >= 0:
                os.close(master)
            os.close(slave)

    def test_interactive_returns_zero_when_the_pty_hangs_up_under_it(self) -> None:
        for where in ("in_cbreak", "on_read"):
            with self.subTest(where=where):
                master, slave = pty.openpty()
                tty = bare_tty(slave)
                tty.size = lambda: os.terminal_size((80, 24))
                if where == "in_cbreak":
                    original = tty.cbreak

                    @contextlib.contextmanager
                    def hang_up_inside():
                        nonlocal master
                        with original():
                            os.close(master)
                            master = -1
                            yield

                    tty.cbreak = hang_up_inside
                else:
                    real_read = tty.read_key

                    def hang_up_then_read(timeout):
                        nonlocal master
                        if master >= 0:
                            os.close(master)
                            master = -1
                        return real_read(timeout)

                    tty.read_key = hang_up_then_read
                inner_read = tty.read_key

                def guarded_read(timeout):  # a client that ignores the end of input fails here, not hangs
                    if tty.eof:
                        raise AssertionError("interactive() read the terminal again after it ended input")
                    return inner_read(timeout)

                tty.read_key = guarded_read
                apps: list = []
                try:
                    code = term.interactive(FakeDriver().api, io.StringIO(), {"NO_COLOR": "1"}, tty,
                                            on_app=apps.append, stderr=io.StringIO())
                    self.assertEqual(code, 0)
                    self.assertTrue(tty.eof and apps[0].closed)
                finally:
                    if master >= 0:
                        os.close(master)
                    os.close(slave)

    def test_the_last_writes_to_a_hung_up_output_end_cleanly(self) -> None:
        master, slave = pty.openpty()
        tty = bare_tty(slave)
        tty.size = lambda: os.terminal_size((80, 24))
        real_read = tty.read_key

        def close_then_read(timeout):
            nonlocal master
            if tty.eof:  # a client that ignores the end of input fails here, not hangs
                raise AssertionError("interactive() read the terminal again after it ended input")
            if master >= 0:
                os.close(master)
                master = -1
            return real_read(timeout)

        tty.read_key = close_then_read

        class HungUpOut(io.StringIO):
            def write(self, text):
                if tty.eof:
                    raise OSError(errno.EIO, "hung-up terminal output")
                return super().write(text)

        class NoThreadLoop:  # isolates the final writes from the loop's own painting (Sol's probe)
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def shutdown(self):
                return True

        try:
            with mock.patch.object(term, "Loop", NoThreadLoop):
                self.assertEqual(term.interactive(FakeDriver().api, HungUpOut(), {"NO_COLOR": "1"}, tty,
                                                  stderr=io.StringIO()), 0)
        finally:
            if master >= 0:
                os.close(master)
            os.close(slave)

    def test_an_error_that_is_not_the_hangup_is_raised(self) -> None:
        tty = bare_tty(-1)
        with mock.patch.object(term.select, "select", return_value=([-1], [], [])), \
                mock.patch.object(term.os, "read", side_effect=OSError(errno.EBADF, "bad descriptor")):
            with self.assertRaises(OSError):
                tty.read_key(0)
        self.assertFalse(tty.eof)
        master, slave = pty.openpty()
        tty = bare_tty(slave)
        real_set, calls = termios.tcsetattr, []

        def restore_fails(fd, when, mode):  # entering works; restoring answers EIO with no hangup seen
            calls.append(when)
            if len(calls) == 2:
                raise termios.error(errno.EIO, "eio")
            return real_set(fd, when, mode)

        try:
            with mock.patch.object(termios, "tcsetattr", side_effect=restore_fails):
                with self.assertRaises(termios.error):
                    with tty.cbreak():
                        pass
            self.assertEqual(len(calls), 2)
        finally:
            os.close(master)
            os.close(slave)

        class BadOut(io.StringIO):
            def write(self, text):
                raise OSError(errno.EBADF, "bad descriptor")

        with self.assertRaises(OSError):
            term.interactive(FakeDriver().api, BadOut(), {"NO_COLOR": "1"}, FakeTerminal(["q"]),
                             stderr=io.StringIO())


class ShutdownFenceTest(unittest.TestCase):
    """Astra R4: shutdown() returning does not prove the loop's thread stopped; close is still the last
    event and nothing is painted after it."""

    def test_close_is_last_even_when_a_tick_is_stalled_past_the_join(self) -> None:
        entered, release = threading.Event(), threading.Event()
        loops, apps, painted_after = [], [], []
        real_loop = term.Loop

        class StalledLoop(real_loop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.join_timeout_s = 0  # the join gives up at once: the thread is still in its tick
                loops.append(self)

            def tick(self):
                entered.set()
                release.wait()
                self.app.on_event({"run_id": "run-1", "text": "late output\n"})
                super().tick()

        class Interrupted(FakeTerminal):
            def read_key(self, timeout):
                entered.wait()
                raise KeyboardInterrupt

        class Out(io.StringIO):
            def write(self, text):
                if apps and apps[0].closed and release.is_set() and "\x1b[H" in text:
                    painted_after.append(text)
                return super().write(text)

        def capture(app):
            apps.append(app)
            close = app.close

            def close_then_release():
                close()
                release.set()

            app.close = close_then_release

        try:
            with mock.patch.object(term, "Loop", StalledLoop):
                code = term.interactive(FakeDriver().api, Out(), {"NO_COLOR": "1"}, Interrupted([]),
                                        on_app=capture, stderr=io.StringIO())
            self.assertEqual(code, 0)
        finally:
            release.set()
            for loop in loops:
                for thread in loop._threads:
                    thread.join()
        self.assertEqual(list(apps[0].log)[-1].event_type, "close")
        self.assertEqual(painted_after, [], "no frame is painted after shutdown")

    def test_a_tick_after_shutdown_paints_nothing_and_logs_nothing(self) -> None:
        driver = FakeDriver()
        app = TuiApp(driver.api, clock=Clock(), environ={"NO_COLOR": "1"})
        frames: list = []
        loop = Loop(app, driver.api, sink=frames.append)
        app.open()
        loop.tick()
        self.assertEqual(len(frames), 1)
        self.assertTrue(loop.shutdown())
        app.close()
        count = len(app.log)
        app.on_event({"run_id": "run-1", "text": "after\n"})
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        app.press("r")
        loop.tick()
        self.assertEqual((len(frames), len(app.log)), (1, count))

    def test_a_condition_that_appears_with_time_alone_is_not_logged_after_close(self) -> None:
        clock = Clock()
        app = TuiApp(FakeDriver().api, clock=clock, environ={"NO_COLOR": "1"}, stale_after_s=10.0)
        app.open()
        app.close()
        count = len(app.log)
        clock.now += 60  # the snapshot is stale now: a frame would say STALE, but the log has ended
        app.frame()
        app.live_rows()
        self.assertEqual(len(app.log), count)
        self.assertEqual(list(app.log)[-1].event_type, "close")


if __name__ == "__main__":
    unittest.main()
