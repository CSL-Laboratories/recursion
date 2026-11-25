"""Ctrl-C while the TUI waits for a confirmed Stop's answer, in every order of events with a stand-in daemon that
hangs: the prompt comes back as soon as nothing is left that the TUI must still send, and never later than the
close's one bound; a confirmed Stop is never dropped to come back sooner; the close line says why the TUI still
waits and how long, and one line after the wait says what became of each Stop that changed after the close line.

The orders of events (each with its test below):
  T1  one Stop in its call, begun less than the hand-over before Ctrl-C: back at once, its request being written
  T2  a hung Stop of r and a queued Stop of r2; r answers within the bound: r2 is sent; the line after says both
  T3  the same, r never answers: back at the bound; r2 not sent, and the line after says so and what to do
  T4  a hung Stop of r and another Stop of r queued (Stop confirmed twice), r never answers: back at the bound;
      the close line names the second Stop and the wait; the line after says it was not sent
  T5  the same, r answers within the bound: the second Stop of r is sent (as on master); the line after says so
  T6  the Stop of r answered during the wait, the Stop of r2 begun during the wait and hung: back once r2's request
      is written, not at the bound and not after the hand-over time
  T7  a queued Stop of r2 begins after Ctrl-C and hangs: back once its request is written
  T8  the call of a queued Stop fails after Ctrl-C (the connection drops): its failure is said, never retried

Stand-ins only: a stand-in API in this process and a scripted terminal; no daemon, socket or terminal of a person.
Every wait is bounded, and every test ends the threads it starts.
"""

from __future__ import annotations

import io
import re
import threading
import time
import unittest
from unittest import mock

from recursion_core.tui import term
from recursion_core.tui.tests.test_inline import FakeTerminal
from recursion_core.tui.tests.test_stop_at_close import API, WAIT_S
from recursion_core.tui.tests.test_stop_ctrl_c import BOUND_S, MARGIN_S, Stamped

HANDOVER_S = 0.6  # the Loop's hand-over in these tests: long enough that a wait for it is plain to see
SOON_S = 0.5  # how long after the Ctrl-C a hung Stop is let answer, well within the bound
WAITED_S = 1.0  # at least this long after the Ctrl-C when a queued Stop that cannot begin was waited for
TAIL = "; look with run status before you trust that {} stopped, and stop it again if still running"


class Held(API):
    """The stand-in API, but each Stop call of a run takes the next entry of ``holds[run]``: an Event the call waits
    for (the daemon hangs until it is set), None (it answers at once), or "fail" (the connection drops); past the
    list, at once. ``began`` keeps (run, when) for every Stop call; ``entered`` is set by a Stop call of ``signal``."""

    def __init__(self, runs, holds, signal=None) -> None:
        super().__init__(runs=runs)
        self.holds = {r: list(v) for r, v in holds.items()}
        self.began: list[tuple[str, float]] = []
        self.signal = signal

    def call(self, method, params, on_sent=None):
        if method != "run.stop":
            return super().call(method, params)
        run = params["run_id"]
        with self._lock:
            self.calls.append((method, dict(params)))
            self.began.append((run, time.monotonic()))
            hold = self.holds[run].pop(0) if self.holds.get(run) else None
        if on_sent is not None:  # a stand-in has no transport: its request is written when it is taken
            on_sent()
        if self.signal is None or run == self.signal:
            self.entered.set()
        if hold == "fail":
            raise ConnectionError("the daemon reset the connection")
        if hold is not None and not hold.wait(WAIT_S):
            raise AssertionError("stop release missing")
        self.state[run] = "stopped"
        return {"code": "stopped", "run_id": run}

    def last_began(self, run) -> float:
        with self._lock:
            return [t for r, t in self.began if r == run][-1]


def drive(api, keys, *, when=None, then=None, handover=HANDOVER_S):
    """term.interactive in full screen with the keys, then Ctrl-C once the close waits and ``when(app)`` holds (by
    default: a Stop call of the API's ``signal`` has begun); returns (err lines, app, t_ctrl, t_end)."""

    loops, apps, stamp = [], [], {}
    real_loop = term.Loop

    class BoundLoop(real_loop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.join_timeout_s = BOUND_S
            self.stop_handover_s = handover
            loops.append(self)

    class Keys(FakeTerminal):
        def read_key(self, timeout):
            app = apps[0] if apps else None
            if self.keys and not (app and app.runs):
                time.sleep(0.01)
                return None
            if not self.keys and "ctrl" not in stamp:
                if app and app.awaiting_stop and (when(app) if when else api.entered.is_set()):
                    time.sleep(0.1)  # some ticks of the wait go by first
                    stamp["ctrl"] = time.monotonic()
                    if then is not None:
                        then()
                    return "ctrl+c"
                time.sleep(0.01)
                return None
            return super().read_key(timeout)

    out, err = io.StringIO(), Stamped()
    try:
        with mock.patch.object(term, "Loop", BoundLoop):
            code = term.interactive(api, out, {"NO_COLOR": "1"}, Keys(list(keys)), alt_screen=True,
                                    on_app=apps.append, stderr=err)
        stamp["end"] = time.monotonic()
        assert code == 0
    finally:
        api.release.set()
        for hold in [h for v in api.holds.values() for h in v if isinstance(h, threading.Event)]:
            hold.set()
        for loop in loops:
            for thread in loop._threads:
                thread.join(WAIT_S)
    return err.getvalue().splitlines(), apps[0], stamp.get("ctrl"), stamp["end"]


def later(event, seconds=SOON_S):
    return lambda: threading.Timer(seconds, event.set).start()


class HandOverTest(unittest.TestCase):
    def test_t1_a_stop_just_begun_is_waited_for_until_its_request_is_written_or_its_call_has_ended(self) -> None:
        api = Held(("r",), {})
        api.holds["r"] = [api.release]
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "q"])
        self.assertIsNotNone(t_ctrl)
        self.assertLess(t_end - api.last_began("r"), HANDOVER_S, "a Stop whose request was written was held on")
        self.assertLess(t_end - t_ctrl, HANDOVER_S + MARGIN_S)
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
        self.assertEqual(lines, [app.end_line])  # nothing changed after the close line: no second line

    def test_t6_a_stop_begun_during_the_wait_is_not_held_to_the_bound(self) -> None:
        r_answers = threading.Event()
        api = Held(("r", "r2"), {"r": [r_answers]}, signal="r2")
        api.holds["r2"] = [api.release]

        def when(app):
            r_answers.set()  # the Stop of r is answered after the wait began; the Stop of r2 then begins and hangs
            return api.entered.is_set()

        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "q"], when=when)
        self.assertIsNotNone(t_ctrl)
        self.assertLess(t_end - api.last_began("r2"), HANDOVER_S, "a Stop whose request was written was held on")
        self.assertLess(t_end - t_ctrl, HANDOVER_S + MARGIN_S, "the TUI waited past the hand-over")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
        self.assertIn("the Stop of r2 was sent and its answer had not come", app.end_line)

    def test_t7_a_queued_stop_begun_after_ctrl_c_is_waited_for_until_its_request_is_written(self) -> None:
        r_answers = threading.Event()
        api = Held(("r", "r2"), {"r": [r_answers]}, signal="r")
        api.holds["r2"] = [api.release]
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "q"], then=later(r_answers, 0.3))
        self.assertIsNotNone(t_ctrl)
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
        self.assertLess(t_end - api.last_began("r2"), HANDOVER_S, "a Stop whose request was written was held on")
        self.assertLess(t_end - api.last_began("r2"), HANDOVER_S + MARGIN_S, "the TUI waited past the hand-over")
        self.assertEqual(lines[-1], "after the close line: the Stop of r was sent and answered: r is stopped; the "
                                    "queued Stop of r2 was sent and its answer had not come" + TAIL.format("r2"))


class QueuedBehindAHungStopTest(unittest.TestCase):
    def test_t2_a_queued_stop_of_another_run_is_sent_and_the_line_after_says_so(self) -> None:
        r_answers = threading.Event()
        api = Held(("r", "r2"), {"r": [r_answers]}, signal="r")
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "q"], then=later(r_answers))
        self.assertGreaterEqual(t_end - t_ctrl, SOON_S - 0.05, "the queued Stop was not waited for")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
        self.assertIn("the TUI waits for it at most", app.end_line)
        self.assertEqual(lines[-1], "after the close line: the Stop of r was sent and answered: r is stopped; the "
                                    "queued Stop of r2 was sent and answered: r2 is stopped")

    def test_t3_a_queued_stop_that_cannot_begin_is_waited_for_then_said_not_sent(self) -> None:
        api = Held(("r", "r2"), {}, signal="r")
        api.holds["r"] = [api.release]
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "q"])
        self.assertGreaterEqual(t_end - t_ctrl, WAITED_S, "a confirmed Stop was dropped to come back sooner")
        self.assertLess(t_end - t_ctrl, BOUND_S + MARGIN_S)
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
        self.assertEqual(lines[-1], f"after the close line: the queued Stop of r2 was not sent: the {BOUND_S:g} s "
                                    f"bound ran out" + TAIL.format("r2"))

    def test_t8_a_queued_stop_whose_call_fails_is_said_not_confirmed_and_sent_once(self) -> None:
        r_answers = threading.Event()
        api = Held(("r", "r2"), {"r": [r_answers], "r2": ["fail"]}, signal="r")
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "q"], then=later(r_answers))
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
        self.assertRegex(lines[-1], r"^after the close line: the Stop of r was sent and answered: r is stopped; the "
                                    r"queued Stop of r2 was sent and not confirmed \([^)]+\); r2 may still be running"
                                    + re.escape(TAIL.format("r2")) + "$")


class StopConfirmedTwiceTest(unittest.TestCase):
    KEYS = ["s", "y", "s", "y", "q"]

    def test_t4_the_second_stop_is_named_waited_for_and_said_not_sent(self) -> None:
        api = Held(("r",), {})
        api.holds["r"] = [api.release]
        lines, app, t_ctrl, t_end = drive(api, self.KEYS)
        self.assertIsNotNone(t_ctrl)
        self.assertRegex(app.end_line, r"^closed: the Stop of r was sent and its answer had not come; another Stop of "
                                       r"r was not begun \(it waits behind the Stop of r\); it is sent only if the "
                                       r"Stop of r ends within [0-9]\.[0-9]{2} s, else it is not sent; the TUI waits "
                                       r"for it at most [0-9]\.[0-9]{2} s and then says what became of it; ")
        self.assertGreaterEqual(t_end - t_ctrl, WAITED_S, "a confirmed Stop was dropped to come back sooner")
        self.assertLess(t_end - t_ctrl, BOUND_S + MARGIN_S)
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
        self.assertEqual(lines[-1], f"after the close line: the queued Stop of r was not sent: the {BOUND_S:g} s "
                                    f"bound ran out" + TAIL.format("r"))
        said = ("closing: the Stop of r is sent and another Stop of r is not yet begun (it waits behind the Stop of "
                "r); the TUI closes when their answers come")
        self.assertTrue(any(said in e.text for e in app.log), [e.text for e in app.log][-6:])

    def test_t5_the_second_stop_is_sent_when_the_first_answers_in_time(self) -> None:
        r_answers = threading.Event()
        api = Held(("r",), {"r": [r_answers, None]})
        lines, app, t_ctrl, t_end = drive(api, self.KEYS, then=later(r_answers))
        self.assertGreaterEqual(t_end - t_ctrl, SOON_S - 0.05, "the second Stop was not waited for")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r"}])
        self.assertEqual(lines[-1], "after the close line: the Stop of r was sent and answered: r is stopped; the "
                                    "queued Stop of r was sent and answered: r is stopped")


if __name__ == "__main__":
    unittest.main()
