"""A Stop the person confirmed is sent when the TUI closes, in every order of the keys within a tick and across
ticks, and the screen says at every moment what was and was not sent. A confirmed Stop that no lane had begun when
the TUI closed was dropped, while the transcript still said the Stop was requested and then that the TUI closed
with no request sent. Every other request not begun is still not sent, and the close line names it. Every wait is
bounded, and every test ends the threads it starts.
"""

from __future__ import annotations

import threading
import time
import unittest

from recursion_core.tui import protocol as p
from recursion_core.tui.app import Loop, TuiApp

WAIT_S = 3.0  # the longest any wait in this file lasts
# The real Loop thread races its lanes: one run of the keys could pass by luck, so the race is run this many times.
REPEATS = 30
# The orders in which s, y and the close key reach the Loop: all in one tick, and each split across ticks.
TICKS = (
    (("s", "y", "CLOSE"),),
    (("s", "y"), ("CLOSE",)),
    (("s",), ("y", "CLOSE")),
    (("s",), ("y",), ("CLOSE",)),
)
CLOSERS = ("q", "ctrl+c")
# When the lanes start: with the keys, or only after the quit key was handled, as when the Stop lane is still busy
# or slow to wake. The second makes "the lane had not begun the Stop when the TUI ended" certain, not a race.
LANES = ("after the quit key", "before the keys")
SAID_NOTHING_SENT = "closed: no request sent"


def wait_for(predicate) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("bounded wait expired")


class API:
    """A stand-in control API with running runs; a Stop of a run named in ``slow`` waits for ``release``."""

    def __init__(self, runs=("r",), slow=(), stop_raises=False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.state = {r: "running" for r in runs}
        self.slow, self.stop_raises = set(slow), stop_raises
        self.release, self.entered = threading.Event(), threading.Event()
        self.status_entered, self.status_release = threading.Event(), threading.Event()
        self.hold_status = False
        self._lock = threading.Lock()

    def call(self, method, params, on_sent=None):
        with self._lock:
            self.calls.append((method, dict(params)))
        if on_sent is not None:  # a stand-in has no transport: its request is written when it is taken
            on_sent()
        if method == "run.list":
            return {"code": "ok", "runs": [{"run_id": r, "state": s} for r, s in self.state.items()]}
        if method == "run.status":
            if self.hold_status:
                self.status_entered.set()
                if not self.status_release.wait(WAIT_S):
                    raise AssertionError("status release missing")
            r = params["run_id"]
            return {"code": "ok", "run": {"run_id": r, "state": self.state.get(r, "running")}}
        if method == "run.stop":
            if params["run_id"] in self.slow:
                self.entered.set()
                if not self.release.wait(WAIT_S):
                    raise AssertionError("stop release missing")
            if self.stop_raises:
                raise ConnectionError("the daemon went away")
            self.state[params["run_id"]] = "stopped"
            return {"code": "stopped", "run_id": params["run_id"]}
        return {"code": "started", "run": {"run_id": "new", "state": "running"}}

    def sent(self, method) -> list[dict]:
        with self._lock:
            return [pa for m, pa in self.calls if m == method]


def new_loop(api, join_timeout_s=WAIT_S):
    app = TuiApp(api, clock=lambda: 1000.0, environ={"NO_COLOR": "1"})
    loop = Loop(app, api)
    loop.join_timeout_s = join_timeout_s
    app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": r, "state": "running"} for r in api.state]})
    return app, loop


def start_lanes(loop) -> None:
    """The lanes only: the test drives the ticks itself, so the keys of one tick are handled in that tick."""
    for lane in loop._lanes.values():
        t = threading.Thread(target=loop._lane, args=(lane,), daemon=True)
        t.start()
        loop._threads.append(t)


def tick_until(loop, predicate) -> None:
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("bounded wait expired")
        loop.tick()
        time.sleep(0.001)


def close_line(app) -> str:
    return [e.text for e in app.log if e.event_type == "close"][-1]


class ConfirmedStopAtCloseTest(unittest.TestCase):
    def assert_sent_and_said(self, api, app, closer) -> None:
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}], "the confirmed Stop was not sent")
        text = app.transcript
        line = close_line(app)
        self.assertNotIn(SAID_NOTHING_SENT, line)
        self.assertIn("request run.stop run_id=r", text)
        # q and Ctrl-C alike: the close waited for the answer, and the transcript records it before the close line
        answers = [i for i, t in enumerate(text) if t.startswith("answer run.stop status=stopped")]
        self.assertTrue(answers, text)
        self.assertLess(answers[0], text.index(line))
        said = "closed: the Stop of r was sent and answered (above); "
        self.assertTrue(line.startswith(said), line)
        # Nothing else is sent; a read the Loop asked at its start and had not begun is named as not sent
        self.assertIn(line[len(said):], (
            "nothing else is sent at close; every other run keeps going and can be reattached",
            "1 queued request not sent (run.list); every other run keeps going and can be reattached"))

    def test_every_order_of_the_keys_sends_the_confirmed_stop_and_says_so(self) -> None:
        for ticks in TICKS:
            for closer in CLOSERS:
                for lanes in LANES:
                    with self.subTest(ticks=ticks, closer=closer, lanes=lanes):
                        self.one_order(ticks, closer, lanes)

    def one_order(self, ticks, closer, lanes) -> None:
        api = API()
        app, loop = new_loop(api)
        if lanes == "before the keys":
            start_lanes(loop)
        try:
            for keys in ticks:
                for key in keys:
                    loop.press(closer if key == "CLOSE" else key)
                loop.tick()
            if lanes == "after the quit key":
                start_lanes(loop)
            tick_until(loop, lambda: app.closed)
            self.assertTrue(loop.shutdown())
            self.assert_sent_and_said(api, app, closer)
        finally:
            app.close()
            loop.shutdown()

    def test_the_real_loop_sends_the_stop_when_the_keys_come_together(self) -> None:
        for trial in range(REPEATS):
            with self.subTest(trial=trial):
                api = API()
                app, loop = new_loop(api)
                loop.start()
                try:
                    for key in ("s", "y", "q"):
                        loop.press(key)
                    wait_for(lambda: app.closed)
                    self.assertTrue(loop.shutdown())
                    self.assert_sent_and_said(api, app, "q")
                finally:
                    app.close()
                    loop.shutdown()

    def test_a_stop_whose_call_fails_has_its_failure_recorded_before_the_close(self) -> None:
        api = API(stop_raises=True)
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            for key in ("s", "y", "q"):
                loop.press(key)
            tick_until(loop, lambda: app.closed)
            text = app.transcript
            failed = [t for t in text if t.startswith(f"answer run.stop status={p.UNAVAILABLE}")]
            self.assertTrue(failed, text)
            self.assertLess(text.index(failed[0]), text.index(close_line(app)))
            self.assertNotIn(SAID_NOTHING_SENT, close_line(app))
        finally:
            app.close()
            loop.shutdown()

    def test_a_close_does_not_wait_past_the_bound_and_says_the_answer_had_not_come(self) -> None:
        api = API(runs=("r", "r2"), slow=("r",))
        app, loop = new_loop(api, join_timeout_s=0.05)
        start_lanes(loop)
        try:
            for key in ("s", "y"):
                loop.press(key)
            loop.tick()
            self.assertTrue(api.entered.wait(WAIT_S))
            for key in ("j", "s", "y", "q"):  # a second Stop, of r2, waits behind the slow one on the Stop lane
                loop.press(key)
            began = time.monotonic()
            tick_until(loop, lambda: app.closed)
            self.assertLess(time.monotonic() - began, WAIT_S)
            self.assertEqual(close_line(app), (
                "closed: the Stop of r was sent and its answer had not come; the Stop of r2 was not begun (it waits "
                "behind the Stop of r) and is not sent: the 0.05 s bound has run out; look with run status "
                "before you trust that r, r2 stopped; every other run keeps going and can be reattached"))
            api.release.set()
            loop.shutdown()
            for thread in loop._threads:
                thread.join(WAIT_S)
            self.assertFalse(any(t.is_alive() for t in loop._threads))
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}], "the close line said the Stop of r2 is not sent")
        finally:
            api.release.set()
            app.close()
            loop.shutdown()

    def test_ctrl_c_ends_the_wait_and_the_stop_is_still_sent(self) -> None:
        api = API(slow=("r",))
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            for key in ("s", "y", "q"):
                loop.press(key)
            loop.tick()
            self.assertTrue(api.entered.wait(WAIT_S))
            self.assertTrue(app.awaiting_stop)
            self.assertFalse(app.closed)
            loop.press("ctrl+c")  # the second close: it ends the wait
            loop.tick()
            self.assertTrue(app.closed)
            self.assertIn("the Stop of r was sent and its answer had not come", close_line(app))
            api.release.set()
            self.assertTrue(loop.shutdown())
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
        finally:
            api.release.set()
            app.close()
            loop.shutdown()

    def test_the_end_of_input_sends_the_stop_and_records_its_answer(self) -> None:
        api = API()
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            for key in ("s", "y"):
                loop.press(key)
            loop.tick()
            self.assertTrue(loop.shutdown())  # as term.py does at the end of input: the Loop first, the app after
            app.close()
            self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
            line = close_line(app)
            self.assertTrue(any(t.startswith("answer run.stop status=stopped") for t in app.transcript))
            self.assertEqual(line, "closed: the Stop of r was sent and answered (above); nothing else is sent at "
                                   "close; every other run keeps going and can be reattached")
        finally:
            app.close()
            loop.shutdown()


class OtherRequestsAtCloseTest(unittest.TestCase):
    def test_a_queued_start_is_not_sent_and_the_end_line_names_it(self) -> None:
        api = API()
        api.hold_status = True
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            loop.press("a")  # a status read holds the other lane
            loop.tick()
            self.assertTrue(api.status_entered.wait(WAIT_S))
            app.command("start --tenant t --engine codex --prompt hello")
            loop.press("q")
            loop.tick()
            self.assertTrue(app.closed)
            self.assertEqual(close_line(app), "closed: 1 queued request not sent (run.start engine=codex "
                                              "prompt=hello tenant=t); runs keep going and can be reattached")
            api.status_release.set()
            self.assertTrue(loop.shutdown())
            self.assertEqual(api.sent("run.start"), [])
        finally:
            api.status_release.set()
            app.close()
            loop.shutdown()

    def test_a_close_with_nothing_queued_keeps_its_line(self) -> None:
        api = API()
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            loop.press("q")
            loop.tick()
            self.assertEqual(close_line(app), "closed: no request sent; runs keep going and can be reattached")
        finally:
            app.close()
            loop.shutdown()


if __name__ == "__main__":
    unittest.main()
