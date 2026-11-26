"""A queued Stop past the close's bound is never sent once a line has said it is not sent, and the close line is
printed again after full screen whenever it has something to tell.

The deadline of the close's bound was checked by the Stop lane without the app's lock, before begin() took the Stop
out of the queue under it: when the app's lock was held while the clock passed the deadline, the close line said the
queued Stop "is not sent: the bound has run out" and the lane then began it and sent it. Full screen printed the close
line again after leaving the alternate screen only when its words held "the Stop of", so a queued Start that was not
sent, or a Start still in its call, was never seen after full screen. Stand-ins only: a stand-in API in this process,
a scripted terminal, no daemon, no socket, no terminal of a person. Every wait is bounded, and every test ends the
threads it starts.
"""

from __future__ import annotations

import io
import threading
import time
import unittest
from unittest import mock

from recursion_core.tui import term
from recursion_core.tui.tests.test_inline import FakeTerminal
from recursion_core.tui.tests.test_stop_at_close import API, WAIT_S, close_line, new_loop, start_lanes

BOUND_S = 1.0  # the close's bound in the race tests: long enough that the lane reaches begin() well inside it
START = "start --tenant t --engine codex --prompt hello"
START_SAID = "run.start engine=codex prompt=hello tenant=t"


class LockedPastTheBound:
    """The race of the Stop lane against the app's lock: r's Stop is in its call, r2's waits behind it on the Stop
    lane; the close's wait begins (its deadline is set); r's call ends; the lane reaches begin() for r2 well before the
    deadline, and this harness holds the app's lock from then until the clock has passed the deadline."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.api = API(runs=("r", "r2"), slow=("r",))
        self.app, self.loop = new_loop(self.api, join_timeout_s=BOUND_S)
        self.reached, self.holding = threading.Event(), threading.Event()
        begin = self.app.begin

        def hooked(asked, method, params, **kwargs):  # the lane's call of begin(), before begin takes the lock
            if method == "run.stop" and params.get("run_id") == "r2":
                self.reached.set()
                if not self.holding.wait(WAIT_S):
                    raise AssertionError("the harness never took the app's lock")
            return begin(asked, method, params, **kwargs)

        self.app.begin = hooked

    def run(self, *, close_before: bool, close_after: bool, pass_the_deadline: bool = True) -> None:
        app, loop, api = self.app, self.loop, self.api
        start_lanes(loop)
        for key in ("s", "y"):
            loop.press(key)
        loop.tick()
        self.test.assertTrue(api.entered.wait(WAIT_S))
        for key in ("j", "s", "y", "q"):  # r2's Stop queues behind r's on the Stop lane; q begins the close's wait
            loop.press(key)
        loop.tick()
        self.test.assertTrue(app.awaiting_stop)
        deadline = loop._deadline
        self.test.assertIsNotNone(deadline)
        api.release.set()  # r's call ends; the lane takes r2's Stop
        self.test.assertTrue(self.reached.wait(WAIT_S), "the lane never reached begin() for r2")
        self.test.assertLess(time.monotonic(), deadline, "the lane reached begin() only after the deadline")
        with app._lock:
            self.holding.set()
            time.sleep(0.1)  # the lane is now inside begin(), waiting for the lock
            if close_before:
                app.close()  # the second close: the line is written while some of the bound is left
            if pass_the_deadline:
                while time.monotonic() < deadline + 0.02:
                    time.sleep(0.005)
            if close_after:
                app.close()  # the line is written after the bound has run out
        loop.shutdown()
        for thread in loop._threads:
            thread.join(WAIT_S)
        self.test.assertFalse(any(t.is_alive() for t in loop._threads))

    def end(self) -> None:
        self.api.release.set()
        self.holding.set()
        self.app.close()
        self.loop.shutdown()
        for thread in self.loop._threads:
            thread.join(WAIT_S)

    def r2_queued(self) -> bool:
        return any(m == "run.stop" and pa == {"run_id": "r2"} for m, pa in self.app._queued.values())


class StopPastTheBoundTest(unittest.TestCase):
    def test_a_stop_the_close_line_says_is_not_sent_is_never_sent(self) -> None:
        race = LockedPastTheBound(self)
        try:
            race.run(close_before=False, close_after=True)
            line = close_line(race.app)
            self.assertIn("the Stop of r2 was not begun (it waits behind the Stop of r) and is not sent: the 1 s "
                          "bound has run out", line)
            self.assertEqual(race.api.sent("run.stop"), [{"run_id": "r"}],
                             "the close line said the Stop of r2 is not sent: the bound has run out")
            self.assertTrue(race.r2_queued(), "a Stop past the bound stays queued")
            self.assertNotIn("Stop of r2 was sent", race.app.after_close_line())
        finally:
            race.end()

    def test_a_stop_said_to_be_sent_only_within_the_bound_is_not_sent_after_it_and_the_line_after_says_so(self):
        race = LockedPastTheBound(self)
        try:
            race.run(close_before=True, close_after=False)
            self.assertRegex(close_line(race.app), r"the Stop of r2 was not begun \(it waits behind the Stop of r\); "
                                                   r"it is sent only if the Stop of r ends within 0\.\d\d s, else it "
                                                   r"is not sent")
            self.assertEqual(race.api.sent("run.stop"), [{"run_id": "r"}],
                             "the Stop of r2 began after the bound the close line gave it")
            self.assertTrue(race.r2_queued(), "a Stop past the bound stays queued")
            self.assertIn("the queued Stop of r2 was not sent: the 1 s bound ran out", race.app.after_close_line())
        finally:
            race.end()

    def test_a_stop_that_begins_before_the_deadline_is_still_sent(self) -> None:
        race = LockedPastTheBound(self)
        try:
            race.run(close_before=False, close_after=False, pass_the_deadline=False)
            self.assertEqual(race.api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
            self.assertFalse(race.r2_queued())
        finally:
            race.end()


class HeldAPI(API):
    """A status read held on the other lane (so a later Start stays queued), or a Start held in its call."""

    def __init__(self, *, hold_start: bool = False) -> None:
        super().__init__()
        self.hold_start = hold_start
        self.start_entered, self.start_release = threading.Event(), threading.Event()

    def call(self, method, params, on_sent=None):
        if method == "run.start" and self.hold_start:
            with self._lock:
                self.calls.append((method, dict(params)))
            self.start_entered.set()
            if not self.start_release.wait(WAIT_S):
                raise AssertionError("start release missing")
            return {"code": "started", "run": {"run_id": "new", "state": "running"}}
        return super().call(method, params, on_sent)


class FullScreenTellsTest(unittest.TestCase):
    def interactive(self, api, keys, ready=lambda: True) -> tuple[object, list[str]]:
        loops, apps = [], []
        real_loop = term.Loop

        class ShortLoop(real_loop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.join_timeout_s = 0.3
                loops.append(self)

        class Keys(FakeTerminal):
            def read_key(self, timeout):  # each key only once the run list is in, and a held key once it holds
                if self.keys and not (apps and apps[0].runs):
                    time.sleep(0.01)
                    return None
                if self.keys and self.keys[0] == "WAIT":
                    if not ready():
                        time.sleep(0.01)
                        return None
                    self.keys.pop(0)
                return super().read_key(timeout)

        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.object(term, "Loop", ShortLoop):
                code = term.interactive(api, out, {"NO_COLOR": "1"}, Keys(keys), alt_screen=True,
                                        on_app=apps.append, stderr=err)
            self.assertEqual(code, 0)
            self.assertIn("\x1b[?1049l", out.getvalue())  # the alternate screen was left
        finally:
            api.status_release.set()
            if isinstance(api, HeldAPI):
                api.start_release.set()
            for loop in loops:
                for thread in loop._threads:
                    thread.join(WAIT_S)
        return apps[0], err.getvalue().splitlines()

    def test_a_queued_start_not_sent_is_printed_after_the_alternate_screen(self) -> None:
        api = HeldAPI()
        api.hold_status = True
        app, lines = self.interactive(api, ["a", "WAIT", ":", *START, "enter", "q"], api.status_entered.is_set)
        line = close_line(app)
        self.assertEqual(line, f"closed: 1 queued request not sent ({START_SAID}); runs keep going and can be "
                               f"reattached")
        self.assertEqual(api.sent("run.start"), [])
        self.assertEqual(lines, [line])

    def test_a_start_in_its_call_is_printed_after_the_alternate_screen(self) -> None:
        api = HeldAPI(hold_start=True)
        app, lines = self.interactive(api, [":", *START, "enter", "WAIT", "q"], api.start_entered.is_set)
        line = close_line(app)
        self.assertIn(f"the request {START_SAID} was in its call; whether it reached the daemon is not known; look "
                      f"with run list", line)
        self.assertEqual(lines, [line])

    def test_a_close_with_nothing_to_tell_prints_nothing_after_the_alternate_screen(self) -> None:
        api = HeldAPI()
        app, lines = self.interactive(api, ["q"])
        self.assertEqual(close_line(app), "closed: no request sent; runs keep going and can be reattached")
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
