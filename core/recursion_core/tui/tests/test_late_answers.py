"""Stop, Start and stale state stay true across late answers. T1: a Start queued behind a read was sent
after q and ``Loop.shutdown``. T2: a ``run.list`` or ``run.status`` asked before a confirmed Stop showed the run
running again. T3: an answer to another method (a Stop) ended a stale status. T4: one clock sample that was no
number (NaN) at a list left the view fresh for ever. T23: run.start's ``start_unconfirmed`` was REFUSED, though
the run may be running. The sweeps' own tests are here as they were written (their names kept, from
``test_s7_app_sol``, ``test_s7_app_astra``, ``test_triage_front_extra`` and ``test_s7_protocol_sol``); the lane's
tests hold each rule's other side. The tests marked "holds" pass on e0733022 too: they keep what it already did,
so a mutant is caught; every other test fails on it. Every wait is bounded.
"""

from __future__ import annotations

import threading
import time
import unittest

from recursion_core.tui import protocol as p
from recursion_core.tui.app import Loop, TuiApp


def wait_for(predicate) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("bounded wait expired")


class API:  # the sweeps' (test_s7_app_sol)
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.state = "running"
        self.stale = False

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "run.list":
            return {"code": "ok", "runs": [{"run_id": "r", "state": self.state}]}
        if method == "run.status":
            answer = {"code": "ok", "run": {"run_id": "r", "state": self.state}}
            return {**answer, "stale": True} if self.stale else answer
        if method == "run.stop":
            self.state = "stopped"
            return {"code": "stopped", "run_id": params["run_id"]}
        return {"code": "started", "run": {"run_id": "new", "state": "running"}}


class PausedStatus(API):
    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def call(self, method, params):
        answer = super().call(method, params)
        if method == "run.status":
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError("status release missing")
        return answer


def new_app(api) -> TuiApp:
    return TuiApp(api, clock=lambda: 1000.0, environ={"NO_COLOR": "1"})


def held(**kwargs):
    """An app whose requests wait in ``pending`` until the test answers them, in any order."""
    pending: list = []
    app = TuiApp(None, clock=lambda: 1000.0, environ={"NO_COLOR": "1"},
                 dispatch=lambda *args: pending.append(args), **kwargs)
    return app, pending


def answer(pending, i, reply) -> None:
    method, params, done = pending[i]
    done(method, params, reply)


RUNNING = {"run_id": "run-1", "state": "running"}


class OnceNaN:  # the clock of test_triage_front_extra's D29: 100, or no number once when asked
    now = 100.0
    once = False

    def __call__(self) -> float:
        if self.once:
            self.once = False
            return float("nan")
        return self.now


class QueuedRequestAfterCloseTest(unittest.TestCase):  # T1
    def test_F01_unsent_start_is_cancelled_when_client_closes(self) -> None:  # test_s7_app_sol
        api = PausedStatus()
        app = new_app(api)
        loop = Loop(app, api)
        loop.join_timeout_s = 0.03
        loop.start()
        try:
            wait_for(lambda: app.snapshot_at is not None)
            loop.press("a")
            self.assertTrue(api.entered.wait(2))
            app.command("start --tenant t --engine codex --prompt hello")
            loop.press("q")
            wait_for(lambda: app.closed)
            loop.shutdown()
            self.assertNotIn("run.start", [m for m, _ in api.calls])
            api.release.set()
            for thread in loop._threads:
                thread.join(2)
            self.assertFalse(any(t.is_alive() for t in loop._threads))
            self.assertNotIn("run.start", [m for m, _ in api.calls], "an unsent start survived q")
        finally:
            api.release.set()
            app.close()
            loop.shutdown()

    def test_a_lane_begins_no_request_once_the_tui_ended_but_a_confirmed_stop(self) -> None:
        api = API()
        app = new_app(api)
        loop = Loop(app, api)
        app.close()
        for method, params in (("run.start", {"tenant": "t", "engine": "e", "prompt": "p"}),
                               ("run.stop", {"run_id": "r"}), ("run.list", {})):
            loop._dispatch(method, params, lambda *args: None)
        for lane in loop._lanes.values():
            lane.put(None)
            loop._lane(lane)
        self.assertEqual(api.calls, [("run.stop", {"run_id": "r"})])

    def test_a_lane_sends_what_is_queued_while_the_tui_is_open(self) -> None:  # holds
        api = API()
        app = new_app(api)
        loop = Loop(app, api)
        loop._dispatch("run.list", {}, lambda *args: None)
        lane = loop._lanes["other"]
        lane.put(None)
        loop._lane(lane)
        self.assertEqual([m for m, _ in api.calls], ["run.list"])


class LateReadAfterStopTest(unittest.TestCase):  # T2
    def test_f01_late_read_cannot_revive_a_confirmed_stopped_run(self) -> None:  # test_s7_app_astra
        for read in ("run.status", "run.list"):
            with self.subTest(read=read):
                app, pending = held()
                app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
                app.request(read, {"run_id": "run-1"} if read == "run.status" else {})
                app.stop("run-1")
                answer(pending, 1, {"code": "stopped", "run_id": "run-1"})
                self.assertEqual(app.runs[0]["state"], "stopped")
                old = {"code": "ok", "run": RUNNING} if read == "run.status" else {"code": "ok", "runs": [RUNNING]}
                answer(pending, 0, old)
                self.assertEqual(app.runs[0]["state"], "stopped", "an older read must not undo the confirmed Stop")

    def test_F02_read_started_before_stop_cannot_restore_a_live_running_view(self) -> None:  # test_s7_app_sol
        api = PausedStatus()
        app = new_app(api)
        loop = Loop(app, api)
        loop.start()
        try:
            wait_for(lambda: app.snapshot_at is not None)
            loop.press("a")
            self.assertTrue(api.entered.wait(2))
            app.stop("r")
            wait_for(lambda: app.last_method == "run.stop")
            self.assertEqual(app.runs[0]["state"], "stopped")
            api.release.set()
            wait_for(lambda: app.in_flight["run.status"] == 0)
            with app._lock:  # wait until applying that answer is complete
                self.assertTrue(app.watch_only or app.runs[0]["state"] == "stopped",
                                "a pre-Stop read overwrote the attested stop as fresh running state")
        finally:
            api.release.set()
            app.close()
            loop.shutdown()

    def test_a_read_asked_after_the_stop_was_sent_but_before_it_was_confirmed_cannot_undo_it(self) -> None:
        for read in ("run.status", "run.list"):
            with self.subTest(read=read):
                app, pending = held()
                app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
                app.stop("run-1")
                app.request(read, {"run_id": "run-1"} if read == "run.status" else {})
                answer(pending, 0, {"code": "stopped", "run_id": "run-1"})
                old = {"code": "ok", "run": RUNNING} if read == "run.status" else {"code": "ok", "runs": [RUNNING]}
                answer(pending, 1, old)
                self.assertEqual(app.runs[0]["state"], "stopped")

    def test_a_read_asked_after_the_confirmation_is_taken(self) -> None:  # holds
        for read in ("run.status", "run.list"):
            with self.subTest(read=read):
                app, pending = held()
                app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
                app.stop("run-1")
                answer(pending, 0, {"code": "stopped", "run_id": "run-1"})
                app.request(read, {"run_id": "run-1"} if read == "run.status" else {})
                now = {"run_id": "run-1", "state": "stopped", "stop": {"reason": "operator"}}
                answer(pending, 1, {"code": "ok", "run": now} if read == "run.status" else {"code": "ok", "runs": [now]})
                self.assertEqual(app.runs[0], now)

    def test_a_late_read_keeps_the_other_runs_it_names(self) -> None:
        app, pending = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})
        app.request("run.list", {})
        app.stop("run-1")
        answer(pending, 1, {"code": "stopped", "run_id": "run-1"})
        answer(pending, 0, {"code": "ok", "runs": [RUNNING, {"run_id": "run-2", "state": "running"}]})
        self.assertEqual([(r["run_id"], r["state"]) for r in app.runs], [("run-1", "stopped"), ("run-2", "running")])


class StaleStatusTest(unittest.TestCase):  # T3
    def test_F03_stale_status_requires_a_fresh_status_before_start(self) -> None:  # test_s7_app_sol
        api = API()
        api.stale = True
        app = new_app(api)
        app.open()
        app.attach("r")
        self.assertTrue(app.watch_only)
        app.stop("r")
        self.assertTrue(app.watch_only, "Stop is not a refresh of the stale status snapshot")
        app.command("start --tenant t --engine codex --prompt hello")
        self.assertNotIn("run.start", [m for m, _ in api.calls])
        self.assertIn("run.status answer was stale", app._stale_text())

    def test_a_fresh_status_ends_it(self) -> None:  # holds
        api = API()
        api.stale = True
        app = new_app(api)
        app.open()
        app.attach("r")
        api.stale = False
        app.request("run.status", {"run_id": "r"})
        self.assertFalse(app.watch_only)
        app.command("start --tenant t --engine codex --prompt hello")
        self.assertIn("run.start", [m for m, _ in api.calls])


class NotANumberClockTest(unittest.TestCase):  # T4
    def test_D29_nan_snapshot_remains_restricted(self) -> None:  # test_triage_front_extra
        class Clock:
            now = 100.0
            once = False

            def __call__(self):
                if self.once:
                    self.once = False
                    return float("nan")
                return self.now

        clock = Clock()
        sent = []
        app = TuiApp(None, clock=clock, dispatch=lambda method, params, done: sent.append(method))
        clock.once = True
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        clock.now = 1000.0
        app.request("run.start", {"tenant": "t", "engine": "inert", "prompt": "example"})
        self.assertNotIn("run.start", sent, "an invalid snapshot time disabled stale restriction")
        self.assertTrue(app.watch_only)
        self.assertIn("snapshot time unknown", app._stale_text())

    def test_a_clock_that_gives_no_number_now_leaves_the_view_stale(self) -> None:
        now = [1000.0]
        app = TuiApp(None, clock=lambda: now[0], dispatch=lambda *args: None)
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        self.assertFalse(app.watch_only)  # a list just now: fresh
        now[0] = float("nan")
        self.assertTrue(app.watch_only)

    def test_a_later_list_at_a_number_ends_it(self) -> None:
        clock = OnceNaN()
        app = TuiApp(None, clock=clock, dispatch=lambda *args: None)
        clock.once = True
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})  # the snapshot at no number
        self.assertTrue(app.watch_only)
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})  # the snapshot at 100
        self.assertFalse(app.watch_only)
        self.assertEqual(app._stale_text(), "")

    def test_an_event_at_a_clock_sample_that_is_no_number_loses_no_answer(self) -> None:
        now = [100.0]
        app = TuiApp(None, clock=lambda: now[0], dispatch=lambda *args: None)
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        now[0] = float("nan")
        app.on_answer("run.list", {}, {"code": "ok", "runs": [dict(RUNNING)]})  # raised ValueError at the event
        self.assertEqual([r["run_id"] for r in app.runs], ["run-1"])
        answers = [e for e in app.log if e.event_type == "answer"]
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[1].time_rel_ms, answers[0].time_rel_ms)  # the last event's time
        self.assertTrue(app.watch_only)


class UnconfirmedStartTest(unittest.TestCase):  # T23
    def test_f2_an_unconfirmed_start_is_uncertain_not_refused(self) -> None:  # test_s7_protocol_sol
        answer_ = {"code": "start_unconfirmed", "detail": "starter_handle_invalid",
                   "run": {"run_id": "run-1", "state": "starting"}}
        conditions = p.keyed_conditions(answer_)
        self.assertIn(p.UNCERTAIN, [label for label, _, _ in conditions])
        self.assertNotIn(p.REFUSED, [label for label, _, _ in conditions])
        text = " ".join(text for _, _, text in conditions).lower()
        self.assertIn("run-1", text)
        self.assertIn("start", text)
        self.assertNotIn("stop not confirmed", text)

    def test_the_schema_s_shape_names_its_run_too(self) -> None:  # methods.v1 fixture h52: run_id at the top
        conditions = p.keyed_conditions({"code": "start_unconfirmed", "run_id": "run-5",
                                         "detail": "registry_unavailable"})
        self.assertEqual([(label, subject) for label, subject, _ in conditions], [(p.UNCERTAIN, "start=run-5")])
        self.assertIn("registry_unavailable", conditions[0][2])

    def test_in_the_app_it_is_uncertain_with_no_error_until_a_list_names_the_run(self) -> None:
        app, _ = held()
        app.on_answer("run.start", {"tenant": "t", "engine": "e", "prompt": "p"},
                      {"code": "start_unconfirmed", "run_id": "run-5", "detail": "registry_unavailable"})
        self.assertEqual({label for label, text in app.conditions() if "run-5" in text}, {p.UNCERTAIN})
        self.assertIsNone(app.last_error)
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "run-4", "state": "running"}]})
        self.assertIn(p.UNCERTAIN, {label for label, text in app.conditions() if "run-5" in text})
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "run-5", "state": "running"}]})
        self.assertNotIn(p.UNCERTAIN, {label for label, text in app.conditions() if "run-5" in text})

    def test_a_refusal_of_a_start_is_still_refused(self) -> None:  # holds
        conditions = p.keyed_conditions({"code": "start_failed", "detail": "engine_unknown"})
        self.assertEqual([label for label, _, _ in conditions], [p.REFUSED])


if __name__ == "__main__":
    unittest.main()
