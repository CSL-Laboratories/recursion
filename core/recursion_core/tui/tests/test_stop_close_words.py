"""What the screen says at the close is true, and no more than is known. Only a "stopped" answer for the run itself
confirms a Stop; a Stop still queued behind a hung one is said to be not begun, and is sent only while the close's
bound lasts; in full screen the close line is printed again after the screen is left; the whole close stays within
ONE bound; a second key while the close waits is answered once. Stand-ins only: a stand-in API in this process, a
scripted terminal, no daemon, no socket, no terminal of a person. Every wait is bounded, and every test ends the
threads it starts.
"""

from __future__ import annotations

import io
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.tui import app as app_module
from recursion_core.tui import term
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.tests.test_inline import FakeTerminal
from recursion_core.tui.tests.test_stop_at_close import API, WAIT_S, close_line, new_loop, start_lanes, tick_until

NOT_CONFIRMED = ("closed: the Stop of r was not confirmed ({said}, above); r may still be running; look with run "
                 "status before you trust that r stopped; every other run keeps going and can be reattached")


class AnsweringAPI(API):
    """A slow Stop of r that gets the given answer once released."""

    def __init__(self, answer, **kwargs) -> None:
        super().__init__(slow=("r",), **kwargs)
        self.answer = answer

    def call(self, method, params):
        if method != "run.stop":
            return super().call(method, params)
        with self._lock:
            self.calls.append((method, dict(params)))
        self.entered.set()
        if not self.release.wait(WAIT_S):
            raise AssertionError("stop release missing")
        return dict(self.answer)


def end_threads(api, app, loop) -> None:
    api.release.set()
    app.close()
    loop.shutdown()
    for thread in loop._threads:
        thread.join(WAIT_S)


class OnlyStoppedConfirmsTest(unittest.TestCase):
    def answered_while_closing(self, api) -> tuple[TuiApp, Loop]:
        app, loop = new_loop(api)
        start_lanes(loop)
        for key in ("s", "y", "q"):
            loop.press(key)
        loop.tick()
        self.assertTrue(api.entered.wait(WAIT_S))
        self.assertTrue(app.awaiting_stop)
        api.release.set()
        tick_until(loop, lambda: app.closed)
        return app, loop

    def test_a_failed_call_pending_or_another_run_is_not_said_as_a_stop_answered(self) -> None:
        cases = (
            ("the daemon reset the connection", API(slow=("r",), stop_raises=True), "control_api_unavailable"),
            ("the daemon answered pending", AnsweringAPI({"code": "pending", "run_id": "r"}), "pending"),
            ("an answer for another run", AnsweringAPI({"code": "stopped", "run_id": "r9"}),
             "stopped for another run"),
        )
        for name, api, said in cases:
            with self.subTest(name):
                app, loop = self.answered_while_closing(api)
                try:
                    line = close_line(app)
                    self.assertNotIn("answered (above)", line)
                    self.assertEqual(line, NOT_CONFIRMED.format(said=said))
                finally:
                    end_threads(api, app, loop)

    def test_a_stopped_answer_still_says_the_stop_was_answered(self) -> None:
        api = AnsweringAPI({"code": "stopped", "run_id": "r"})
        app, loop = self.answered_while_closing(api)
        try:
            self.assertEqual(close_line(app), "closed: the Stop of r was sent and answered (above); nothing else is "
                                              "sent at close; every other run keeps going and can be reattached")
            self.assertEqual(app._unconfirmed_at_close, {})
        finally:
            end_threads(api, app, loop)


class QueuedStopTest(unittest.TestCase):
    def queue_r2_behind_a_hung_r(self, bound: float):
        api = API(runs=("r", "r2"), slow=("r",))
        app, loop = new_loop(api, join_timeout_s=bound)
        start_lanes(loop)
        for key in ("s", "y"):
            loop.press(key)
        loop.tick()
        self.assertTrue(api.entered.wait(WAIT_S))
        for key in ("j", "s", "y", "q"):
            loop.press(key)
        loop.tick()
        return api, app, loop

    def test_a_stop_queued_when_the_bound_runs_out_is_said_not_begun_and_is_not_sent(self) -> None:
        api, app, loop = self.queue_r2_behind_a_hung_r(0.2)
        try:
            self.assertIn("closing: the Stop of r is sent and the Stop of r2 is not yet begun (it waits behind the "
                          "Stop of r); the TUI closes when their answers come, within 0.2 s; Ctrl-C closes now",
                          app.transcript)
            tick_until(loop, lambda: app.closed)
            self.assertEqual(close_line(app), (
                "closed: the Stop of r was sent and its answer had not come; the Stop of r2 was not begun (it waits "
                "behind the Stop of r) and is not sent: the 0.2 s bound has run out; look with run status "
                "before you trust that r, r2 stopped; every other run keeps going and can be reattached"))
            self.assertFalse(loop.shutdown())  # the Stop of r is still in its call
            api.release.set()  # the Stop of r ends now, after the bound: the Stop of r2 behind it is not begun
            for thread in loop._threads:
                thread.join(WAIT_S)
            self.assertFalse(any(t.is_alive() for t in loop._threads))
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
        finally:
            end_threads(api, app, loop)

    def test_a_stop_queued_when_ctrl_c_ends_the_wait_is_sent_if_the_one_ahead_ends_in_time(self) -> None:
        api, app, loop = self.queue_r2_behind_a_hung_r(WAIT_S)
        try:
            loop.press("ctrl+c")
            loop.tick()
            self.assertTrue(app.closed)
            line = close_line(app)
            self.assertRegex(line, r"^closed: the Stop of r was sent and its answer had not come; the Stop of r2 was "
                                   r"not begun \(it waits behind the Stop of r\); it is sent only if the Stop of r "
                                   r"ends within [0-9]\.[0-9]{2} s, else it is not sent; look with run status before "
                                   r"you trust that r, r2 stopped; every other run keeps going and can be reattached$")
            api.release.set()  # well within what is left of the bound
            self.assertTrue(loop.shutdown())
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
        finally:
            end_threads(api, app, loop)


class OneBoundTest(unittest.TestCase):
    def test_the_bound_is_one_named_value(self) -> None:
        self.assertEqual(app_module.CLOSE_BOUND_S, 5.0)
        app = TuiApp(API(), clock=lambda: 1000.0, environ={"NO_COLOR": "1"})
        loop = Loop(app, API())
        self.assertEqual((loop.join_timeout_s, app.stop_wait_s, app.close_bound_s), (5.0, 5.0, 5.0))

    def test_the_whole_close_with_a_hung_daemon_stays_within_one_bound(self) -> None:
        bound = 0.4
        api = API(slow=("r",))
        app, loop = new_loop(api, join_timeout_s=bound)
        start_lanes(loop)
        try:
            for key in ("s", "y"):
                loop.press(key)
            loop.tick()
            self.assertTrue(api.entered.wait(WAIT_S))
            began = time.monotonic()
            loop.press("q")
            tick_until(loop, lambda: app.closed)
            self.assertFalse(loop.shutdown())  # the Stop's call is still hung: the join gives up with the bound
            took = time.monotonic() - began
            self.assertGreaterEqual(took, bound - 0.02)
            self.assertLess(took, bound * 1.5, "the wait and the join each took the bound")
        finally:
            end_threads(api, app, loop)


class LastTickTest(unittest.TestCase):
    def test_a_stop_confirmed_in_the_tick_under_way_at_shutdown_is_sent(self) -> None:
        """The end of input right after y: shutdown() begins while the Loop's tick has the y still to handle."""

        entered, release = threading.Event(), threading.Event()

        class GatedLoop(Loop):
            gated = False

            def tick(self):
                if self.gated:
                    entered.set()
                    if not release.wait(WAIT_S):
                        raise AssertionError("release missing")
                super().tick()

        api = API()
        app = TuiApp(api, clock=lambda: 1000.0, environ={"NO_COLOR": "1"})
        loop = GatedLoop(app, api)
        loop.join_timeout_s = WAIT_S
        loop.start()
        ender = threading.Thread(target=loop.shutdown, daemon=True)
        try:
            deadline = time.monotonic() + WAIT_S
            while not app.runs:  # the Loop's first read is answered
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            loop.press("s")
            while app.mode != "confirm_stop":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            loop.gated = True
            loop.press("y")
            self.assertTrue(entered.wait(WAIT_S))  # the tick that handles y is under way and held
            ender.start()
            lanes = loop._threads[:2]
            waited = time.monotonic() + 0.3  # long enough for the lanes to end if shutdown() ended them first
            while any(t.is_alive() for t in lanes) and time.monotonic() < waited:
                time.sleep(0.005)
            release.set()
            ender.join(WAIT_S)
            self.assertFalse(ender.is_alive())
            app.close()
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
            self.assertEqual(close_line(app), "closed: the Stop of r was sent and answered (above); nothing else is "
                                              "sent at close; every other run keeps going and can be reattached")
        finally:
            release.set()
            app.close()
            loop.shutdown()
            ender.join(WAIT_S)
            for thread in loop._threads:
                thread.join(WAIT_S)


class SecondKeyWhileWaitingTest(unittest.TestCase):
    def test_a_second_q_says_the_notice_again_once(self) -> None:
        api = API(slow=("r",))
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            for key in ("s", "y", "q"):
                loop.press(key)
            loop.tick()
            self.assertTrue(api.entered.wait(WAIT_S))
            for _ in range(3):
                loop.press("q")
                loop.tick()
            again = [t for t in app.transcript if t.startswith("still waiting:")]
            self.assertEqual(len(again), 1, app.transcript)
            self.assertRegex(again[0], r"^still waiting: the Stop of r is sent; the TUI closes when its answer comes, "
                                       r"at most [0-9]\.[0-9] s from now; other keys do nothing while it waits; "
                                       r"Ctrl-C closes now$")
            self.assertFalse(app.closed)
        finally:
            end_threads(api, app, loop)


class FullScreenCloseLineTest(unittest.TestCase):
    def test_the_close_line_naming_a_stop_is_printed_after_the_alternate_screen(self) -> None:
        api = API(slow=("r",))
        loops, apps = [], []
        real_loop = term.Loop

        class ShortLoop(real_loop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.join_timeout_s = 0.3
                loops.append(self)

        class Keys(FakeTerminal):
            def read_key(self, timeout):  # the keys only once the run list is in, so s has a run to stop
                if self.keys and not (apps and apps[0].runs):
                    time.sleep(0.01)
                    return None
                return super().read_key(timeout)

        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.object(term, "Loop", ShortLoop):
                code = term.interactive(api, out, {"NO_COLOR": "1"}, Keys(["s", "y", "q"]), alt_screen=True,
                                        on_app=apps.append, stderr=err)
            self.assertEqual(code, 0)
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
            line = ("closed: the Stop of r was sent and its answer had not come; look with run status before you "
                    "trust that r stopped; every other run keeps going and can be reattached")
            self.assertEqual(close_line(apps[0]), line)
            self.assertIn("\x1b[?1049l", out.getvalue())  # the alternate screen was left
            self.assertEqual(err.getvalue().splitlines(), [line])
        finally:
            api.release.set()
            for loop in loops:
                for thread in loop._threads:
                    thread.join(WAIT_S)


class ReadmeTest(unittest.TestCase):
    def test_the_readme_says_q_or_ctrl_c_waits(self) -> None:
        readme = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("When `q` or Ctrl-C comes while a confirmed Stop has no answer yet, the TUI waits", readme)
        self.assertIn("ONE bound (`CLOSE_BOUND_S`", readme)


if __name__ == "__main__":
    unittest.main()
