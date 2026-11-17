"""Round 3 of #443: the Grok read of 46d006e0. R1 (P2): a run.status or run.list that was asked before a
Stop was confirmed still ended a start UNCERTAIN raised after that Stop. A status counts when it is
merged, and a list run counts when that read was not asked before the Stop. R2 (P3): the round-2 tests
send start_unconfirmed with run_id and no run. The retry body carries run and no top-level run_id, and
that answer stays UNCERTAIN while the run is on screen. The R2 test passes on 46d006e0; it goes red if
a run.start answer treats its run as naming the launch.
"""

from __future__ import annotations

import unittest

from recursion_core.tui import protocol as p
from recursion_core.tui.app import TuiApp

START = {"tenant": "t", "engine": "e", "prompt": "p", "client_key": "k-1"}
# The retry body control/api.py returns when a same-key start is still unconfirmed: run, no top-level run_id.
RETRY = {"code": "start_unconfirmed", "run": {"run_id": "run-1", "state": "starting"},
         "duplicate": True, "detail": "launch_unconfirmed"}


def held(clock=lambda: 1000.0):
    pending: list = []
    app = TuiApp(None, clock=clock, environ={"NO_COLOR": "1"}, dispatch=lambda *args: pending.append(args))
    return app, pending


def answer(pending, i, reply) -> None:
    method, params, done = pending[i]
    done(method, params, reply)


def uncertain(app) -> bool:
    return any(label == p.UNCERTAIN and "run-1" in text for label, text in app.conditions())


class RefusedReadDoesNotEndStartTest(unittest.TestCase):  # R1
    def test_a_status_asked_before_a_confirmed_stop_does_not_end_a_later_unconfirmed_start(self) -> None:
        app, pending = held()
        app.request("run.status", {"run_id": "run-1"})
        app.stop("run-1")
        answer(pending, 1, {"code": "stopped", "run_id": "run-1"})
        app.request("run.start", dict(START))
        answer(pending, 2, dict(RETRY))
        self.assertTrue(uncertain(app))
        answer(pending, 0, {"code": "ok", "run": {"run_id": "run-1", "state": "running"}})
        self.assertTrue(uncertain(app))
        self.assertEqual(app.runs, [])

    def test_a_list_asked_before_a_confirmed_stop_does_not_end_a_later_unconfirmed_start(self) -> None:
        app, pending = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "run-1", "state": "running"}]})
        app.request("run.list", {})
        app.stop("run-1")
        answer(pending, 1, {"code": "stopped", "run_id": "run-1"})
        app.request("run.start", dict(START))
        answer(pending, 2, dict(RETRY))
        self.assertTrue(uncertain(app))
        answer(pending, 0, {"code": "ok", "runs": [{"run_id": "run-1", "state": "running"}]})
        self.assertEqual(app.runs[0]["state"], "stopped")
        self.assertTrue(uncertain(app))


class RetryBodyStaysUncertainTest(unittest.TestCase):  # R2
    def test_the_retry_body_the_api_returns_stays_uncertain_while_the_run_is_on_screen(self) -> None:
        app, _ = held()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "run-1", "state": "starting"}]})
        app.on_answer("run.start", START, dict(RETRY))
        self.assertTrue(uncertain(app))
        self.assertTrue(any("UNCERTAIN" in text for text, _ in app.frame()))
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "run-1", "state": "starting"}]})
        self.assertFalse(uncertain(app))


if __name__ == "__main__":
    unittest.main()
