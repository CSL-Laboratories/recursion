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
import itertools
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

# select() on a pipe is POSIX only (Windows refuses it with WinError 10038): the tests that read a real
# pipe through the terminal driver skip there. The mocked reads beside them run everywhere.
posix_select = unittest.skipIf(os.name == "nt", "select() on a pipe is POSIX only")


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

    @posix_select
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
            self.assertTrue(entered.wait(10.0), f"worker never entered append; errors={errors!r}")
            self.assertFalse(loop.shutdown())
            app.close()  # returns although the worker is still inside its append
        finally:
            release.set()
            worker.join(10.0)
        self.assertFalse(worker.is_alive(), "append worker did not end after release")
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


class SharedInputThatEndsWithoutAKeyTest(unittest.TestCase):
    """Round 3 (both reads of 46b8d7d8, Astra C3): an Enter or y that shares a read with an unfinished
    sequence stays a burst however that sequence ends: at the end of input, as an unknown sequence, by
    the wait, or broken by the Enter itself. A lone Esc and one split key stay one key."""

    def read_one(self, reads: list) -> tuple:
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=reads):
            key = tty.read_key(0)
        return key, tty.burst

    def test_every_way_the_sequence_can_end(self) -> None:
        for reads in ([b"\n\x1b[12", b""], [b"\n\x1b[12", b"z"], [b"\n\x1b[12", None], [b"\x1b[12\n"],
                      [b"\n\x1b[" + b"1" * (MAX_SEQ + 2) + b"A"], [b"\n\x1b[999~"], [b"\n\x1b" + chr(0x85).encode()]):
            with self.subTest(reads=reads):
                self.assertEqual(self.read_one(reads), ("enter", True))
        self.assertEqual(self.read_one([b"y\x1bO12", b""]), ("y", True))

    def test_a_lone_esc_and_one_split_key_stay_one_key(self) -> None:
        for reads, expected in (([b"\x1b[", b"1;5C"], "ctrl+right"), ([b"\x1b", None], "esc"),
                                ([b"\x1b", b""], "esc"), ([b"\x1bx"], "alt+x")):
            with self.subTest(reads=reads):
                self.assertEqual(self.read_one(reads), (expected, False))

    @posix_select
    def test_a_prepared_line_or_stop_is_not_sent_through_the_whole_client(self) -> None:
        # Astra's interactive probes (a pipe, interactive() and its threads), rounds 3 and 4: the unfinished
        # sequence ends at the end of input, as an unknown sequence, or (round 4) as a paste that begins.
        cases = [(key, prefix, tail, completion) for key, prefix in ((b"\n", ":stop run-1"), (b"y", "s"))
                 for tail, completion in ((b"\x1b[12", b""), (b"\x1b[12", b"z"), (b"\x1b[200", b"~"),
                                          (b"\x1b[200", b"~ignored body\x1b[201~"), (b"\x1b[20", b"0~payload"))]
        for keybytes, prefix, tail, completion in cases:
            with self.subTest(keybytes=keybytes, tail=tail, completion=completion):
                driver = FakeDriver()
                driver.start_run()
                api = RecordingAPI(driver.api)
                read_fd, write_fd = os.pipe()
                tty = tty_for(read_fd)
                tty.cbreak = contextlib.nullcontext
                real_read, reads = tty._read, [0]
                handled = threading.Event()

                def read(timeout):
                    nonlocal write_fd
                    reads[0] += 1
                    if reads[0] == 2:  # the wait after the unfinished sequence: its end, then end of input
                        if completion:
                            os.write(write_fd, completion)
                        os.close(write_fd)
                        write_fd = -1
                    return real_read(timeout)

                tty._read = read
                real_key, ended = tty.read_key, [False]

                def read_key(timeout):  # a client that ignores the end of input fails here, not hangs
                    if ended[0]:
                        raise AssertionError("interactive() read the terminal again after it ended input")
                    key = real_key(timeout)
                    ended[0] = key is None and tty.eof  # keys read with the end still come out first
                    return key

                tty.read_key = read_key

                def prepare(app):
                    app.open()
                    for key in prefix:
                        app.press(key)
                    api.calls.clear()
                    actual = app.press

                    def press(*args, **kwargs):
                        try:
                            actual(*args, **kwargs)
                        finally:
                            handled.set()

                    app.press = press

                class WaitUntilHandled(term.Loop):
                    def press(self, *args, **kwargs):
                        super().press(*args, **kwargs)
                        handled.wait()

                try:
                    os.write(write_fd, keybytes + tail)
                    with mock.patch.object(term, "Loop", WaitUntilHandled):
                        self.assertEqual(term.interactive(api, io.StringIO(), {"NO_COLOR": "1"}, tty,
                                                          on_app=prepare, stderr=io.StringIO()), 0)
                    self.assertEqual([c for c in api.calls if c[0] == "run.stop"], [])
                finally:
                    os.close(read_fd)
                    if write_fd >= 0:
                        os.close(write_fd)

    @unittest.skipIf(pty is None, "needs POSIX terminals")
    def test_a_real_hangup_cannot_erase_the_burst(self) -> None:  # Sol's PTY probe
        for keybytes, prefix in ((b"\n", ":stop run-1"), (b"y", "s")):
            with self.subTest(keybytes=keybytes):
                app, api = fresh()
                for key in prefix:
                    app.press(key)
                master, slave = pty.openpty()
                tty = tty_for(slave)
                actual, first = tty._read, [True]

                def read(timeout):
                    nonlocal master
                    data = actual(timeout)
                    if first[0] and data:
                        first[0] = False
                        os.close(master)
                        master = -1
                    return data

                tty._read = read
                try:
                    with tty.cbreak():
                        os.write(master, keybytes + b"\x1b[12")
                        key = tty.read_key(1)
                    app.press(key, burst=tty.burst)
                    self.assertTrue(tty.eof)
                    self.assertEqual((tty.burst, api.calls), (True, []))
                finally:
                    if master >= 0:
                        os.close(master)
                    os.close(slave)

    def test_the_decoder_counts_each_sequence_that_ends_without_a_key(self) -> None:
        for data, keys, discarded in ((b"\x1b[999~", [], 1), (b"\x1b[1;99A", [], 1), (b"\x1bOz", [], 1),
                                      (b"\x1b[12\n", ["enter"], 1), (b"\x1b[" + b"1" * (MAX_SEQ + 1) + b"A", [], 1),
                                      (b"\x1b" + chr(0x85).encode(), [], 1), (b"\x1b[C\x1bx\x1b[200~p\x1b[201~", ["right", "alt+x", Paste("p")], 0)):
            with self.subTest(data=data):
                d = KeyDecoder()
                self.assertEqual((d.feed(data), d.discarded), (keys, discarded))
        d = KeyDecoder()
        d.feed(b"\x1b[12")
        self.assertEqual((d.timeout(), d.discarded), ([], 1))
        d.feed(b"\x1b")
        self.assertEqual((d.timeout(), d.discarded), (["esc"], 1), "a lone Esc is a key, not a dropped sequence")
        d.feed(b"\x1b[3")
        self.assertEqual((d.end(), d.discarded), ([], 2))


class PasteBesideAKeyTest(unittest.TestCase):
    """Round 4 (both reads of d69d18aa, C3): an Enter or y beside an unfinished sequence that turns into a
    paste opener, or beside a paste begun in the same read, stays a burst. One paste, however it is split,
    stays one event and not a burst."""

    def read_one(self, reads: list) -> tuple:
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=itertools.chain(reads, itertools.repeat(None))):
            key = tty.read_key(0)
        return key, tty.burst

    def test_a_key_beside_a_paste_opener_or_a_paste_begun_is_a_burst(self) -> None:
        for confirm in (b"\n", b"y"):
            name = "enter" if confirm == b"\n" else "y"
            for reads in ([confirm + b"\x1b[200", b"~"], [confirm + b"\x1b[200~body"],
                          [confirm + b"\x1b[20", b"0", b"~body\x1b[20"], [confirm + b"\x1b[20", b"0~"],
                          [confirm + b"\x1b[20", b"0~payload"]):
                with self.subTest(reads=reads):
                    self.assertEqual(self.read_one(reads), (name, True))

    def test_one_paste_however_split_is_one_event(self) -> None:
        for reads in ([b"\x1b[200~ab", b"c\x1b[201~"], [b"\x1b[200", b"~abc\x1b[201~"], [b"\x1b[20", b"0~abc\x1b[20", b"1~"]):
            with self.subTest(reads=reads):
                tty = tty_for(-1)
                with mock.patch.object(tty, "_read", side_effect=itertools.chain(reads, itertools.repeat(None))):
                    got = [k for k in (tty.read_key(0) for _ in range(len(reads) + 1)) if k is not None]
                self.assertEqual((got, tty.burst), ([Paste("abc")], False))
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=[b"\x1b[200~ab", b""]):
            self.assertIsNone(tty.read_key(0))
            self.assertEqual((tty.read_key(0), tty.burst), (Paste("ab"), False), "ended by the end of input")

    def test_a_key_the_wait_gives_beside_half_a_character_is_a_burst(self) -> None:
        self.assertEqual(self.read_one([b"\x1b\xc3", None]), ("esc", True))

    def test_a_sequence_dropped_in_an_earlier_read_does_not_mark_a_later_key(self) -> None:
        # The count is per read (both fourth reads' coverage note): an unknown sequence read alone, then
        # an Enter read alone, is one key.
        tty = tty_for(-1)
        with mock.patch.object(tty, "_read", side_effect=itertools.chain([b"\x1b[999~", b"\n"], itertools.repeat(None))):
            self.assertIsNone(tty.read_key(0))
            self.assertEqual((tty.read_key(0), tty.burst), ("enter", False))

    @unittest.skipIf(pty is None, "needs POSIX terminals")
    def test_a_paste_opener_split_over_a_real_pty_read_sends_no_stop(self) -> None:  # both reads' PTY probes
        for confirm, prefix in ((b"\n", ":stop run-1"), (b"y", "s")):
            with self.subTest(confirm=confirm):
                app, api = fresh()
                for key in prefix:
                    app.press(key)
                master, slave = pty.openpty()
                tty = tty_for(slave)
                actual, first = tty._read, [True]

                def read(timeout):  # the opener's last byte, and a paste left open, arrive in the wait
                    data = actual(timeout)
                    if first[0] and data:
                        first[0] = False
                        os.write(master, b"~body")
                    return data

                tty._read = read
                try:
                    with tty.cbreak():
                        os.write(master, confirm + b"\x1b[200")
                        key = tty.read_key(1)
                        marked = tty.burst
                    app.press(key, burst=marked)
                    self.assertEqual((marked, [c for c in api.calls if c[0] == "run.stop"]), (True, []))
                finally:
                    os.close(master)
                    os.close(slave)


class Round3CoverageTest(unittest.TestCase):
    """The third reads' coverage cases for rules that held (green on 46b8d7d8), with untimed waits."""

    def test_only_pollhup_is_a_hangup_and_no_poll_is_none(self) -> None:
        tty = tty_for(-1)
        with mock.patch.object(term.select, "poll", None, create=True):
            self.assertFalse(tty._hung_up())
        if not hasattr(term.select, "poll"):
            return
        for flags in (term.select.POLLIN, term.select.POLLERR, term.select.POLLNVAL, term.select.POLLHUP):
            with self.subTest(flags=flags):
                poller = mock.Mock()
                poller.poll.return_value = [(-1, flags)]
                with mock.patch.object(term.select, "poll", return_value=poller):
                    self.assertEqual(tty._hung_up(), flags == term.select.POLLHUP)

    @unittest.skipIf(pty is None, "needs POSIX terminals")
    def test_every_inherited_input_flag_and_high_bytes(self) -> None:
        master, slave = pty.openpty()
        tty = tty_for(slave)
        mask = (termios.IGNBRK | termios.BRKINT | termios.IGNPAR | termios.PARMRK | termios.INPCK | termios.ISTRIP
                | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON | termios.IXANY | termios.IXOFF)
        saved = termios.tcgetattr(slave)
        saved[0] |= mask
        termios.tcsetattr(slave, termios.TCSANOW, saved)
        payload = b"\x00\r\n\x03\x11\x13\xff\xc3\xa9\x80"
        try:
            with tty.cbreak():
                mode = termios.tcgetattr(slave)
                self.assertEqual(mode[0] & mask, 0)
                self.assertEqual(mode[1], saved[1])
                os.write(master, payload)
                self.assertEqual(os.read(slave, 128), payload)
            self.assertEqual(termios.tcgetattr(slave), saved)
        finally:
            os.close(master)
            os.close(slave)

    def test_many_writers_and_two_final_events_keep_one_last_event(self) -> None:
        from recursion_core.tui.events import EventLog

        log = EventLog(10000)
        ready = threading.Barrier(10)
        errors: list = []

        def writer(final: bool) -> None:
            try:
                ready.wait()
                if final:
                    log.append_last(0, "close", "operator", "closed", "closed", "", "")
                else:
                    for i in range(100):
                        log.append(i, "output", "engine", "live", str(i), "", "")
            except BaseException as error:  # noqa: BLE001
                errors.append(repr(error))

        workers = [threading.Thread(target=writer, args=(i < 2,)) for i in range(10)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        events = list(log)
        self.assertEqual(errors, [])
        self.assertEqual([e.event_type for e in events].count("close"), 1)
        self.assertEqual(events[-1].event_type, "close")
        self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)))

    def test_ending_waits_for_a_request_under_way_and_then_refuses_new_ones(self) -> None:
        app, api = fresh()
        inside, release, attempt, returned = (threading.Event() for _ in range(4))
        sent: list = []

        class Watched:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                if threading.current_thread().name == "ender":
                    attempt.set()
                return self.inner.__enter__()

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

        app._lock = Watched(app._lock)

        def dispatch(*args):
            inside.set()
            release.wait()
            sent.append(args[:2])

        app.dispatch = dispatch

        def end():
            try:
                app.close()
            finally:
                returned.set()
                attempt.set()

        worker = threading.Thread(target=app.request, args=("run.status", {"run_id": "run-1"}))
        ender = threading.Thread(target=end, name="ender")
        worker.start()
        try:
            inside.wait()
            ender.start()
            attempt.wait()
            self.assertFalse(returned.is_set(), "ending went on while a request was under way")
        finally:
            release.set()
            worker.join()
            ender.join()
        app.request("run.list", {})
        self.assertEqual(sent, [("run.status", {"run_id": "run-1"})])


if __name__ == "__main__":
    unittest.main()
