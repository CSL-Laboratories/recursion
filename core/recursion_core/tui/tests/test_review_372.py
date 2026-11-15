"""Round 2 of review on #372 (two Codex reads of a1f16a20: GPT-6 Astra C1 to C4, GPT-6.1 Sol 1 to 3), kept as
regressions with credit, together with the coverage probes both reads wrote for rules that already held.

The reviewers' probes waited with time bounds (a few seconds) as liveness guards; here every wait is on an
event that the code under test, or the test itself, sets, so no test fails or passes by the clock. PTYs are
opened by the tests and have no controlling foreground process.
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
from recursion_core.tui.keys import MAX_SEQ, KeyDecoder, Paste
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI
from recursion_core.tui.tests.test_inline import FakeTerminal

try:  # POSIX only
    import pty
    import termios
except ImportError:  # pragma: no cover - Windows
    pty = termios = None


def fresh() -> tuple[TuiApp, RecordingAPI]:
    driver = FakeDriver()
    driver.start_run()
    api = RecordingAPI(driver.api)
    app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
    app.open()
    api.calls.clear()
    return app, api


def tty_for(fd: int) -> term.Tty:
    tty = object.__new__(term.Tty)
    tty.fd = fd
    tty.size = lambda: os.terminal_size((80, 24))
    return tty


@unittest.skipIf(pty is None, "needs POSIX terminals")
class HangupAfterTheLastReadTest(unittest.TestCase):
    """Astra C1, Sol 2: a hangup after q was read, before any other read, is still a clean end."""

    def test_a_q_already_read_then_the_hangup(self) -> None:  # Sol's probe
        master, slave = pty.openpty()
        tty = tty_for(slave)
        real_cbreak = tty.cbreak

        @contextlib.contextmanager
        def provide_quit():
            with real_cbreak():
                os.write(master, b"q")
                yield

        tty.cbreak = provide_quit
        closed = threading.Event()

        def capture(app):
            actual = app.close

            def close():
                actual()
                closed.set()

            app.close = close

        class QuitThenHangup(term.Loop):
            def press(inner, key, **kwargs):
                nonlocal master
                super().press(key, **kwargs)
                closed.wait()  # the q ends the app; then the terminal goes, before the mode is restored
                os.close(master)
                master = -1

        try:
            with mock.patch.object(term, "Loop", QuitThenHangup):
                self.assertEqual(term.interactive(FakeDriver().api, io.StringIO(), {"NO_COLOR": "1"}, tty,
                                                  on_app=capture, stderr=io.StringIO()), 0)
        finally:
            if master >= 0:
                os.close(master)
            os.close(slave)

    def buffered_quit(self, disconnect: bool) -> None:  # Astra's probe, and its connected control
        master, slave = pty.openpty()
        tty = tty_for(slave)
        read, calls, apps = tty.read_key, [0], []
        closed = threading.Event()

        class SynchronizedLoop(Loop):
            def press(self, key, **kw):
                super().press(key, **kw)
                if key == "q":
                    closed.wait()

        original = tty.cbreak

        @contextlib.contextmanager
        def supply_keys():
            with original():
                os.write(master, b" q")
                yield

        tty.cbreak = supply_keys

        def disconnect_on_queued_key(timeout):
            nonlocal master
            calls[0] += 1
            if calls[0] == 2 and disconnect:
                os.close(master)
                master = -1
            return read(timeout)

        tty.read_key = disconnect_on_queued_key

        def capture(app):
            apps.append(app)
            close = app.close

            def wrapped():
                close()
                closed.set()

            app.close = wrapped

        try:
            with mock.patch.object(term, "Loop", SynchronizedLoop):
                code = term.interactive(FakeDriver().api, io.StringIO(), {"NO_COLOR": "1"}, tty,
                                        on_app=capture, stderr=io.StringIO())
            self.assertEqual(code, 0)
            self.assertTrue(apps[0].closed)
        finally:
            if master >= 0:
                os.close(master)
            os.close(slave)

    def test_a_buffered_q_then_the_hangup_before_another_read(self) -> None:
        self.buffered_quit(True)

    def test_a_buffered_q_on_a_terminal_that_stays(self) -> None:
        self.buffered_quit(False)

    def test_a_restore_error_that_is_not_eio_is_raised_even_after_eof(self) -> None:
        master, slave = pty.openpty()
        tty = tty_for(slave)
        real_set, calls = termios.tcsetattr, []

        def fail_restore(fd, when, mode):
            calls.append(when)
            if len(calls) == 2:
                raise termios.error(errno.EBADF, "unrelated restore failure")
            return real_set(fd, when, mode)

        try:
            with mock.patch.object(termios, "tcsetattr", side_effect=fail_restore):
                with self.assertRaises(termios.error):
                    with tty.cbreak():
                        tty.eof = True
        finally:
            os.close(master)
            os.close(slave)


@unittest.skipIf(pty is None, "needs POSIX terminals")
class InputTranslationTest(unittest.TestCase):
    """Astra C2, Sol 3: inherited IGNCR or INLCR cannot drop or rewrite a pasted CR or LF."""

    def test_inherited_cr_and_lf_translation_is_off_while_the_tui_runs(self) -> None:
        for flag in (termios.IGNCR, termios.INLCR, termios.IGNCR | termios.INLCR):
            with self.subTest(flag=flag):
                master, slave = pty.openpty()
                tty = tty_for(slave)
                try:
                    mode = termios.tcgetattr(slave)
                    mode[0] |= flag
                    termios.tcsetattr(slave, termios.TCSANOW, mode)
                    with tty.cbreak():
                        os.write(master, b"\x1b[200~a\rb\nc\x03\x1a\x13\x1b[201~\x1b[200~left\rright\nend\x1b[201~")
                        first, second = tty.read_key(0.5), tty.read_key(0.5)
                    self.assertEqual((first, second), (Paste("a\rb\nc\x03\x1a\x13"), Paste("left\rright\nend")))
                    self.assertEqual(termios.tcgetattr(slave), mode, "the inherited flags come back")
                    app, _ = fresh()
                    app.press(":")
                    app.press(first)
                    self.assertEqual(app.line, "a b c")
                finally:
                    os.close(master)
                    os.close(slave)

    def test_every_control_byte_stays_inside_a_paste(self) -> None:
        for data in (bytes(range(32)) + b"\x7f", b"a\x1c\x00\x11\x7fb"):
            with self.subTest(data=data):
                master, slave = pty.openpty()
                tty = tty_for(slave)
                try:
                    saved = termios.tcgetattr(slave)
                    with tty.cbreak():
                        os.write(master, b"\x1b[200~" + data + b"\x1b[201~")
                        self.assertEqual(tty.read_key(0.5), Paste(data.decode()))
                    self.assertEqual(termios.tcgetattr(slave), saved)
                finally:
                    os.close(master)
                    os.close(slave)


class UnfinishedInputTest(unittest.TestCase):
    """Astra C3: a sequence the wait drops still counts as other input in Enter's read."""

    def press_list(self) -> tuple[TuiApp, RecordingAPI]:
        app, api = fresh()
        for key in ":list":
            app.press(key)
        return app, api

    def test_enter_with_an_unfinished_sequence_is_a_burst(self) -> None:
        app, api = self.press_list()
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=[b"\n\x1b[12", None]):
            key = tty.read_key(0)
        app.press(key, burst=tty.burst)
        self.assertEqual((key, tty.burst, api.calls), ("enter", True, []))

    def test_the_same_through_a_real_pipe(self) -> None:
        app, api = self.press_list()
        read_fd, write_fd = os.pipe()
        tty = tty_for(read_fd)
        try:
            os.write(write_fd, b"\n\x1b[1;5")
            key = tty.read_key(0)
            app.press(key, burst=tty.burst)
            self.assertEqual((key, tty.burst, api.calls), ("enter", True, []))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_a_lone_esc_and_an_arrow_split_over_the_wait_are_one_key(self) -> None:
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=[b"\x1b", None]):
            self.assertEqual((tty.read_key(0), tty.burst), ("esc", False))
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=[b"\x1b", b"[C"]):
            self.assertEqual((tty.read_key(0), tty.burst), ("right", False))

    def test_utf8_held_before_enter_and_completed_alone(self) -> None:
        app, api = self.press_list()
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=[b"\xe2", b"\x82", b"\xac\n"]):
            self.assertIsNone(tty.read_key(0))
            self.assertIsNone(tty.read_key(0))
            for _ in range(2):
                key = tty.read_key(0)
                self.assertTrue(tty.burst)
                app.press(key, burst=tty.burst)
        self.assertEqual(api.calls, [])
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=[b"\xc3", b"\xa9"]):
            self.assertIsNone(tty.read_key(0))
            self.assertEqual((tty.read_key(0), tty.burst), (chr(0xE9), True))

    def test_holding_reports_an_escape_sequence(self) -> None:
        decoder = KeyDecoder()
        decoder.feed(b"\x1b[")
        self.assertTrue(decoder.holding)


class BurstMarkTest(unittest.TestCase):
    """The burst marks of the first round, from more sides (both reads)."""

    def test_the_mark_survives_editing_and_a_burst_s_survives_rearming(self) -> None:
        app, api = fresh()
        app.press(":")
        for key in "stop run-1":
            app.press(key, burst=True)
        for key in ("left", "right", "home", "end"):
            app.press(key)
        app.press("enter")
        self.assertEqual(api.calls, [])
        app.press("esc")
        app.press("s", burst=True)
        app.press("Y")
        self.assertEqual(api.calls, [])
        app.press("s")
        app.press("Y")
        self.assertEqual(api.calls, [("run.stop", {"run_id": "run-1"})])

    def test_a_burst_edit_after_the_notice_needs_the_notice_again(self) -> None:
        app, api = fresh()
        app.press(":")
        for key in "stop run-1":
            app.press(key, burst=True)
        app.press("enter")
        app.press(" ", burst=True)
        app.press("enter")
        self.assertEqual(api.calls, [])
        app.press("enter")
        self.assertEqual(api.calls, [("run.stop", {"run_id": "run-1"})])

    def test_a_burst_colon_marks_the_line_alone_or_last_in_its_read(self) -> None:
        app, api = fresh()
        app.press(":", burst=True)
        for key in "list":
            app.press(key)
        app.press("enter")
        self.assertEqual(api.calls, [])
        app, api = fresh()
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", return_value=b" :"):
            for _ in range(2):
                key = tty.read_key(0)
                app.press(key, burst=tty.burst)
        for key in "list":
            app.press(key)
        app.press("enter")
        self.assertEqual((api.calls, app.mode), ([], "command"))


class ClosingTest(unittest.TestCase):
    """Astra C4, Sol 1: work already under way when the TUI ends cannot log, send or change anything after
    the final event; close() does not wait on it."""

    def test_an_append_already_begun_is_refused_after_the_final_event(self) -> None:  # Astra's probe
        app, api = fresh()
        entered, release = threading.Event(), threading.Event()
        real_append = app.log.append

        def gated_append(ms, event_type, *args):
            if event_type == "output":
                entered.set()
                release.wait()
            return real_append(ms, event_type, *args)

        app.log.append = gated_append
        loop = Loop(app, api, sink=lambda frame: None)
        loop.join_timeout_s = 0
        loop.post_event({"run_id": "run-1", "text": "in flight\n"})
        errors: list = []

        def tick():
            try:
                loop.tick()
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion below
                errors.append(exc)

        worker = threading.Thread(target=tick)
        loop._threads.append(worker)
        worker.start()
        try:
            entered.wait()
            self.assertFalse(loop.shutdown())
            app.close()  # returns although the worker is still inside its append
        finally:
            release.set()
            worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(list(app.log)[-1].event_type, "close", [e.event_type for e in app.log])

    def test_an_emit_already_past_its_check_appends_nothing_after_close(self) -> None:  # Sol's probe
        app, api = fresh()
        entered, release = threading.Event(), threading.Event()
        loop = Loop(app, api)
        loop.join_timeout_s = 0
        original_clock = app._clock

        def clock():
            if threading.current_thread().name == "review-tick":
                entered.set()
                release.wait()
            return original_clock()

        app._clock = clock
        loop.post_event({"run_id": "run-1", "text": "late output\n"})
        errors: list = []

        def tick():
            try:
                loop.tick()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=tick, name="review-tick")
        loop._threads.append(worker)
        worker.start()
        try:
            entered.wait()
            self.assertFalse(loop.shutdown())
            app.close()
            self.assertEqual(list(app.log)[-1].event_type, "close")
            before = len(app.log)
        finally:
            release.set()
            worker.join()
        self.assertEqual(errors, [])
        self.assertEqual([e.event_type for e in list(app.log)[before:]], [])

    def test_an_answer_being_read_changes_nothing_after_close(self) -> None:  # Astra's probe
        app, api = fresh()
        entered, release = threading.Event(), threading.Event()

        class PausedAnswer(dict):
            def get(self, key, *args):
                if key == "code":
                    entered.set()
                    release.wait()
                return super().get(key, *args)

        loop = Loop(app, api)
        loop.join_timeout_s = 0
        loop._answers.put((app.on_answer, "run.list", {}, PausedAnswer(code="ok", runs=[])))
        errors: list = []

        def tick():
            try:
                loop.tick()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=tick)
        loop._threads.append(worker)
        worker.start()
        try:
            entered.wait()
            loop.shutdown()
            app.close()
            before = list(app.runs)
        finally:
            release.set()
            worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(app.runs, before)

    def test_close_waits_for_a_state_change_under_way(self) -> None:
        # A handler blocked inside the state lock (here, reading the runs of a list answer) finishes
        # before close() goes on, so the final event comes after its answer line.
        app, api = fresh()
        inside, release, attempt, returned = (threading.Event() for _ in range(4))

        class SlowRuns(dict):
            def get(self, key, *args):
                if key == "runs":
                    inside.set()
                    release.wait()
                return super().get(key, *args)

        worker = threading.Thread(target=app.on_answer, args=("run.list", {}, SlowRuns(code="ok", runs=[])))

        class Watched:
            def __init__(self, lock):
                self.inner = lock

            def __enter__(self):
                if threading.current_thread().name == "closer":
                    attempt.set()
                return self.inner.__enter__()

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

        app._lock = Watched(app._lock)

        def closer():
            try:
                app.close()
            finally:
                returned.set()
                attempt.set()  # also when close() never took the lock, so a regression fails, not hangs

        stopper = threading.Thread(target=closer, name="closer")
        worker.start()
        try:
            inside.wait()
            stopper.start()
            attempt.wait()
            self.assertFalse(returned.is_set(), "close() went on while a state change was under way")
        finally:
            release.set()
            worker.join()
            stopper.join()
        self.assertEqual([e.event_type for e in app.log][-2:], ["answer", "close"])
        self.assertEqual(app.runs, [], "the answer that was under way was applied before the end")

    def test_output_whose_text_is_still_being_read_changes_nothing_after_close(self) -> None:
        app, api = fresh()
        entered, release = threading.Event(), threading.Event()

        class PausedEvent(dict):
            def get(self, key, *args):
                if key == "text":
                    entered.set()
                    release.wait()
                return super().get(key, *args)

        worker = threading.Thread(target=app.on_event, args=(PausedEvent(run_id="run-1", text="late\n"),))
        worker.start()
        try:
            entered.wait()
            app.close()
        finally:
            release.set()
            worker.join()
        self.assertEqual((app.outputs, list(app.log)[-1].event_type), ({}, "close"))

    def test_after_the_end_nothing_is_taken_and_ending_again_does_nothing(self) -> None:
        app, api = fresh()
        sent: list = []
        app.dispatch = lambda *args: sent.append(args)
        before = (list(app.runs), dict(app.outputs), dict(app.in_flight), app.mode, app.line, app.cursor)
        app.close()
        count = len(app.log)
        app.close()
        app.request("run.stop", {"run_id": "run-1"})
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        app.on_event({"run_id": "new-run", "text": "late\n"})
        app.press(":")
        self.assertEqual(sent, [])
        self.assertEqual((list(app.runs), dict(app.outputs), dict(app.in_flight), app.mode, app.line, app.cursor),
                         before)
        self.assertEqual(len(app.log), count)

    def test_shutdown_waits_for_a_paint_under_way(self) -> None:  # Astra's probe, with untimed waits
        app, api = fresh()
        inside, release, attempt, returned = (threading.Event() for _ in range(4))
        errors: list = []

        def sink(frame):
            inside.set()
            release.wait()

        loop = Loop(app, api, sink=sink)
        loop.join_timeout_s = 0

        class ObservableLock:
            lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == "shutdown-probe":
                    attempt.set()
                self.lock.acquire()

            def __exit__(self, *exc):
                self.lock.release()

        loop._paint_lock = ObservableLock()

        def tick():
            try:
                loop.tick()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def shutdown():
            try:
                loop.shutdown()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                returned.set()
                attempt.set()

        painter = threading.Thread(target=tick)
        stopper = threading.Thread(target=shutdown, name="shutdown-probe")
        painter.start()
        try:
            inside.wait()
            stopper.start()
            attempt.wait()
            self.assertFalse(returned.is_set(), "shutdown returned while a paint was still under way")
        finally:
            release.set()
            painter.join()
            if stopper.ident:
                stopper.join()
        self.assertEqual(errors, [])

    def test_shutdown_reports_a_thread_still_alive(self) -> None:
        app, api = fresh()
        loop = Loop(app, api)

        class Stalled:
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        loop._threads.append(Stalled())
        self.assertFalse(loop.shutdown())


class OutputHangupTest(unittest.TestCase):
    """Sol's and Astra's coverage of the hung-up output and stderr."""

    def test_an_output_hangup_alone_ends_the_loop_and_nothing_more_is_written(self) -> None:
        class Gone(io.StringIO):
            attempts = 0

            def write(self, text):
                Gone.attempts += 1
                raise OSError(errno.EIO, "hung-up output")

        class Idle(FakeTerminal):
            reads = 0

            def read_key(self, timeout):
                Idle.reads += 1
                if Idle.reads > 2:
                    raise AssertionError("kept reading after the output hung up")
                return None

        self.assertEqual(term.interactive(FakeDriver().api, Gone(), {"NO_COLOR": "1"}, Idle([]),
                                          stderr=io.StringIO()), 0)
        self.assertEqual(Gone.attempts, 1)

    def test_a_stderr_hangup_is_clean_and_another_stderr_error_is_raised(self) -> None:
        class ErrorOut(io.StringIO):
            def __init__(self, number):
                super().__init__()
                self.number = number

            def write(self, text):
                raise OSError(self.number, "stderr failure")

        class SyncLoop:
            def __init__(self, app, *args, **kwargs):
                self.app = app

            def start(self):
                self.app.errors.append("saved diagnostic")
                self.app.close()

            def shutdown(self):
                return True

        with mock.patch.object(term, "Loop", SyncLoop):
            self.assertEqual(term.interactive(FakeDriver().api, io.StringIO(), {"NO_COLOR": "1"}, FakeTerminal([]),
                                              stderr=ErrorOut(errno.EIO)), 0)
            with self.assertRaises(OSError):
                term.interactive(FakeDriver().api, io.StringIO(), {"NO_COLOR": "1"}, FakeTerminal([]),
                                 stderr=ErrorOut(errno.EBADF))


class CoverageTest(unittest.TestCase):
    """Rules that held, from more sides (both reads' coverage cases)."""

    def test_sequence_bounds_modified_f6_and_split_keypad_enter(self) -> None:
        d = KeyDecoder()
        for part in (b"\x1bO", b"1" * (MAX_SEQ // 2), b"1" * (MAX_SEQ + 1)):
            self.assertEqual(d.feed(part), [])
            self.assertLessEqual(len(d._seq), MAX_SEQ)
        self.assertEqual(d.feed(b"Mx"), ["x"])
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1bO" + b"0" * (MAX_SEQ + 1)), [])
        self.assertEqual(d.feed(b"Pz"), ["z"])
        self.assertEqual(KeyDecoder().feed(b"\x1b[17;7~\x1b[17;8~\x1bOM"), ["ctrl+alt+f6", "ctrl+alt+shift+f6", "enter"])
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1bO"), [])
        self.assertEqual(d.feed(b"M"), ["enter"])

    def test_pasted_line_breaks_and_the_cursor_bound(self) -> None:
        app, _ = fresh()
        app.press(":")
        app.press(Paste("a\r\n\r\nb"))
        app.press("home")
        for _ in range(10):
            app.press("left")
        self.assertEqual((app.line, app.cursor), ("a  b", 0))
        app, _ = fresh()
        app.press(":")
        app.press("left")
        app.press(Paste("a\r\nb\r\nc"))
        self.assertEqual((app.cursor, app.line), (5, "a b c"))

    def test_uncertainty_survives_a_status_answer_and_an_absent_run(self) -> None:
        app, _ = fresh()
        app.on_answer("run.stop", {"run_id": "run-1"}, {"code": "pending", "run_id": "run-1", "recorded": False})
        for state in ("ended", "lost_reboot"):
            app.on_answer("run.status", {"run_id": "run-1"}, {"code": "ok", "run": {"run_id": "run-1", "state": state}})
            self.assertIn("run=run-1", app._uncertain)
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        self.assertIn("run=run-1", app._uncertain)


if __name__ == "__main__":
    unittest.main()
