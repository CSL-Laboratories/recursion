"""Round 1 of #443: DeepSeek V4.1 Flash's read of f1f2242e. F1 (high, T4): a clock sample of +inf at a list left
the view fresh for ever and let a Start out. F2 (medium, T2): a second confirmed Stop of a run, or the same answer
again, moved the bar, so a read asked after the first confirmation was dropped. F3 (low, T23): a ``run`` with no
id hid the answer's own ``run_id``. F4 (low): a late ``started`` answer repainted a confirmed-stopped run. N4: an
``ok`` status with no run ended a stale status. The tests marked "holds" pass on f1f2242e too; every other test
fails on it.
"""

from __future__ import annotations

import unittest

from recursion_core.tui import protocol as p
from recursion_core.tui.app import TuiApp

RUNNING = {"run_id": "run-1", "state": "running"}
START = {"tenant": "t", "engine": "e", "prompt": "p"}


class OnceClock:  # 100, or ``value`` once when asked
    def __init__(self, value: float) -> None:
        self.now, self.value, self.once = 100.0, value, False

    def __call__(self) -> float:
        if self.once:
            self.once = False
            return self.value
        return self.now


def held(clock=lambda: 1000.0):
    pending: list = []
    app = TuiApp(None, clock=clock, environ={"NO_COLOR": "1"}, dispatch=lambda *args: pending.append(args))
    return app, pending


def answer(pending, i, reply) -> None:
    method, params, done = pending[i]
    done(method, params, reply)


class UsableAgeTest(unittest.TestCase):  # F1
    def test_a_snapshot_at_infinity_leaves_the_view_stale_and_start_refused(self) -> None:
        clock = OnceClock(float("inf"))
        app, pending = held(clock)
        clock.once = True
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})  # the snapshot at +inf: an age of -inf
        app.request("run.start", START)
        self.assertEqual(pending, [])
        self.assertTrue(app.watch_only)
        self.assertIn("snapshot time unknown", app._stale_text())

    def test_a_clock_that_goes_back_before_the_snapshot_leaves_the_view_stale(self) -> None:
        now = [1000.0]
        app, _ = held(lambda: now[0])
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        now[0] = 900.0
        self.assertTrue(app.watch_only)
        self.assertIn("snapshot time unknown", app._stale_text())

    def test_a_snapshot_at_minus_infinity_is_said_to_be_of_no_usable_time(self) -> None:
        clock = OnceClock(float("-inf"))
        app, _ = held(clock)
        clock.once = True
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        self.assertTrue(app.watch_only)
        self.assertIn("snapshot time unknown", app._stale_text())

    def test_an_age_from_zero_to_the_limit_is_fresh_and_past_it_older(self) -> None:  # holds
        now = [1000.0]
        app, _ = held(lambda: now[0])
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        for age, stale in ((0.0, False), (app.stale_after_s, False), (app.stale_after_s + 1, True)):
            with self.subTest(age=age):
                now[0] = 1000.0 + age
                self.assertEqual(app.watch_only, stale)
                self.assertEqual("snapshot older than" in app._stale_text(), stale)


class FirstConfirmationTest(unittest.TestCase):  # F2
    def test_a_second_confirmed_stop_does_not_hide_a_read_asked_after_the_first(self) -> None:
        for second in ("another Stop", "the same answer again"):
            with self.subTest(second=second):
                app, pending = held()
                app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
                app.stop("run-1")
                answer(pending, 0, {"code": "stopped", "run_id": "run-1"})
                app.request("run.status", {"run_id": "run-1"})  # asked after the first confirmation
                if second == "another Stop":
                    app.stop("run-1")
                    answer(pending, 2, {"code": "stopped", "run_id": "run-1"})
                else:
                    answer(pending, 0, {"code": "stopped", "run_id": "run-1"})
                now = {"run_id": "run-1", "state": "stopped", "stop": {"reason": "operator"}}
                answer(pending, 1, {"code": "ok", "run": now})
                self.assertEqual(app.runs[0], now)


class StartRunIdTest(unittest.TestCase):  # F3
    def test_a_run_with_no_id_does_not_hide_the_answer_s_run_id(self) -> None:
        reply = {"code": "start_unconfirmed", "run": {"state": "starting"}, "run_id": "r5"}
        self.assertEqual([(label, subject) for label, subject, _ in p.keyed_conditions(reply)],
                         [(p.UNCERTAIN, "start=r5")])
        app, _ = held()
        app.on_answer("run.start", START, reply)
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "r5", "state": "running"}]})
        self.assertNotIn(p.UNCERTAIN, {label for label, _ in app.conditions()})

    def test_a_run_with_an_id_names_it(self) -> None:  # holds
        reply = {"code": "start_unconfirmed", "run": {"run_id": "r6"}, "run_id": "r5"}
        self.assertEqual([subject for _, subject, _ in p.keyed_conditions(reply)], ["start=r6"])


class LateStartedTest(unittest.TestCase):  # F4
    def test_a_late_started_answer_does_not_repaint_a_stopped_run(self) -> None:
        app, pending = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
        app.request("run.start", {**START, "client_key": "k-1"})  # a retry of the launch of run-1
        app.stop("run-1")
        answer(pending, 1, {"code": "stopped", "run_id": "run-1"})
        answer(pending, 0, {"code": "started", "duplicate": True, "run": dict(RUNNING)})
        self.assertEqual(app.runs[0]["state"], "stopped")

    def test_a_started_answer_asked_after_the_confirmation_is_taken(self) -> None:  # holds
        app, pending = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
        app.request("run.start", START)
        answer(pending, 0, {"code": "started", "run": {"run_id": "run-2", "state": "running"}})
        self.assertEqual([r["run_id"] for r in app.runs], ["run-1", "run-2"])


class StatusWithNoRunTest(unittest.TestCase):  # N4
    def test_an_ok_status_with_no_run_does_not_end_a_stale_status(self) -> None:
        app, pending = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
        app.request("run.status", {"run_id": "run-1"})
        answer(pending, 0, {"code": "ok", "run": dict(RUNNING), "stale": True})
        app.request("run.status", {"run_id": "run-1"})
        answer(pending, 1, {"code": "ok"})
        self.assertTrue(app.watch_only)
        self.assertIn("run.status answer was stale", app._stale_text())


if __name__ == "__main__":
    unittest.main()
