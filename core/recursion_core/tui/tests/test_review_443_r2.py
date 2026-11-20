"""Round 2 of #443: the Grok read of d6bba735 (ruling 12). R1 (P2): a ``start_unconfirmed`` answer erased its own
UNCERTAIN warning when its run was already on screen, as a retry of the same key repeats the answer. R2 to R6 (P3),
rules no test held: a late list of a run the app did not hold shows it stopped; a lane begins nothing once
``Loop.shutdown`` ran, before the app ended; an event at an infinite clock sample keeps the last event's time; a
status naming the run ends a start's UNCERTAIN; a started answer asked after a confirmed Stop is taken. The tests
marked "holds" pass on d6bba735 too: they keep what it already did, so a mutant is caught; every other test fails
on it.
"""

from __future__ import annotations

import unittest

from recursion_core.tui import protocol as p
from recursion_core.tui.app import Loop, TuiApp

START = {"tenant": "t", "engine": "e", "prompt": "p", "client_key": "k-1"}
UNCONFIRMED = {"code": "start_unconfirmed", "run_id": "run-1", "detail": "launch_unconfirmed", "duplicate": True}
STARTING = {"run_id": "run-1", "state": "starting"}


def held(clock=lambda: 1000.0):
    pending: list = []
    app = TuiApp(None, clock=clock, environ={"NO_COLOR": "1"}, dispatch=lambda *args: pending.append(args))
    return app, pending


def answer(pending, i, reply) -> None:
    method, params, done = pending[i]
    done(method, params, reply)


def uncertain(app) -> bool:
    return any(label == p.UNCERTAIN and "run-1" in text for label, text in app.conditions())


class API:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, method, params):
        self.calls.append(method)
        return {"code": "ok", "runs": []}


class UnconfirmedStartOnScreenTest(unittest.TestCase):  # R1
    def test_an_unconfirmed_start_of_a_run_on_screen_is_uncertain_until_a_later_list_names_it(self) -> None:
        app, _ = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(STARTING)]})
        app.frame()
        app.on_answer("run.start", START, dict(UNCONFIRMED))
        self.assertTrue(uncertain(app))
        rows = [text for text, _ in app.frame()]
        self.assertTrue(any("UNCERTAIN" in row for row in rows), rows)
        self.assertTrue(any(e.event_type == "condition" and "UNCERTAIN" in e.text for e in app.log))
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(STARTING)]})
        self.assertFalse(uncertain(app))

    def test_an_answer_that_names_another_run_does_not_end_it(self) -> None:
        app, _ = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(STARTING)]})
        app.on_answer("run.start", START, dict(UNCONFIRMED))
        app.on_answer("run.status", {"run_id": "run-2"}, {"code": "ok", "run": {"run_id": "run-2", "state": "running"}})
        self.assertTrue(uncertain(app))  # run-1 is on screen, but this answer does not name it

    def test_a_retry_after_a_list_named_the_run_is_uncertain_again(self) -> None:
        app, _ = held()
        app.on_answer("run.start", START, dict(UNCONFIRMED))
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(STARTING)]})
        self.assertFalse(uncertain(app))
        app.on_answer("run.start", START, dict(UNCONFIRMED))  # the same key again: the same answer
        self.assertTrue(uncertain(app))


class RulesNoTestHeldTest(unittest.TestCase):  # R2 to R6
    def test_a_late_list_of_a_run_the_app_did_not_hold_shows_it_stopped(self) -> None:  # R2; holds
        app, pending = held()
        app.request("run.list", {})
        app.stop("run-9")  # the app holds no row for it
        answer(pending, 1, {"code": "stopped", "run_id": "run-9"})
        answer(pending, 0, {"code": "ok", "runs": [{"run_id": "run-9", "state": "running"}]})
        self.assertEqual([(r["run_id"], r["state"]) for r in app.runs], [("run-9", "stopped")])

    def test_a_lane_begins_nothing_once_shutdown_ran_before_the_app_ended(self) -> None:  # R3; holds
        api = API()
        app = TuiApp(api, clock=lambda: 1000.0, environ={"NO_COLOR": "1"})
        loop = Loop(app, api)
        loop._dispatch("run.start", dict(START), lambda *args: None)
        loop._dispatch("run.stop", {"run_id": "run-1"}, lambda *args: None)
        self.assertTrue(loop.shutdown())  # as term.py does on end of input: the Loop first, the app after
        self.assertFalse(app.closed)
        for lane in loop._lanes.values():
            loop._lane(lane)
        self.assertEqual(api.calls, [])

    def test_an_event_at_an_infinite_clock_sample_keeps_the_last_time(self) -> None:  # R4; holds
        for sample in (float("inf"), float("-inf")):
            with self.subTest(sample=sample):
                now = [100.0]
                app, _ = held(lambda now=now: now[0])
                app.on_answer("run.list", {}, {"code": "ok", "runs": []})
                now[0] = sample
                app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(STARTING)]})
                self.assertEqual([r["run_id"] for r in app.runs], ["run-1"])
                times = [e.time_rel_ms for e in app.log if e.event_type == "answer"]
                self.assertEqual(len(times), 2)
                self.assertEqual(times[1], times[0])

    def test_a_status_naming_the_run_ends_a_start_s_uncertain(self) -> None:  # R5; holds
        app, _ = held()
        app.on_answer("run.start", START, dict(UNCONFIRMED))
        self.assertTrue(uncertain(app))
        app.on_answer("run.status", {"run_id": "run-1"}, {"code": "ok", "run": dict(STARTING)})
        self.assertFalse(uncertain(app))
        self.assertEqual([r["run_id"] for r in app.runs], ["run-1"])

    def test_a_started_answer_asked_after_a_confirmed_stop_is_taken(self) -> None:  # R6; holds
        app, pending = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "run-1", "state": "running"}]})
        app.stop("run-1")
        answer(pending, 0, {"code": "stopped", "run_id": "run-1"})
        app.request("run.start", dict(START))  # asked after the confirmation
        answer(pending, 1, {"code": "started", "run": {"run_id": "run-1", "state": "running"}})
        self.assertEqual(app.runs[0]["state"], "running")


if __name__ == "__main__":
    unittest.main()
