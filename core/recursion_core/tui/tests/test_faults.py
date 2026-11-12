"""Loop faults: a slow read must not paint over a newer Stop, and one bad event or answer
must not kill the thread that handles keys and Stop."""

from __future__ import annotations

import threading
import time
import unittest

from recursion_core.control.registry import POPULATED
from recursion_core.tui import protocol as p
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI


def wait_for(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class _HoldReads(RecordingAPI):
    """Completes one armed read, then holds that answer so a Stop can land before it is applied."""

    def __init__(self, inner, method: str) -> None:
        super().__init__(inner)
        self.method, self.arm = method, False
        self.held, self.release = threading.Event(), threading.Event()

    def call(self, method, params=None):
        result = super().call(method, params)
        if method == self.method and self.arm:
            self.held.set()
            self.release.wait(5)
        return result


class StaleReadTest(unittest.TestCase):
    def _run(self, held_method: str):
        driver = FakeDriver(confirm_answers=[POPULATED])
        run_id = driver.start_run()
        api = _HoldReads(driver.api, held_method)
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        loop = Loop(app, api)
        loop.start()
        self.addCleanup(driver.halt)
        self.addCleanup(api.release.set)
        self.addCleanup(loop.shutdown)
        self.assertTrue(wait_for(lambda: app.runs), "open did not list the run")
        api.arm = True
        loop.press("a" if held_method == "run.status" else "r")
        self.assertTrue(api.held.wait(2), "the read never reached the API")
        loop.press("s")
        self.assertTrue(wait_for(lambda: app.mode == "confirm_stop"))
        loop.press("y")
        self.assertTrue(wait_for(lambda: app.runs[0]["state"] == "stop_pending"), app.runs)
        api.release.set()
        self.assertTrue(wait_for(lambda: app.last_method == held_method), app.last_method)
        # The held answer was computed before the Stop. Applying it must not unwind that Stop.
        self.assertEqual(app.runs[0]["state"], "stop_pending", app.runs[0])
        self.assertEqual(driver.registry.get(run_id)["state"], "stop_pending")
        self.assertIn(f"run={run_id}", app._uncertain)
        self.assertIn(p.UNCERTAIN, {label for label, _ in app.conditions()})
        return driver, api, app, loop, run_id

    def test_a_list_started_before_stop_does_not_hide_the_unconfirmed_stop(self) -> None:
        driver, api, app, loop, run_id = self._run("run.list")
        # A list that starts after the Stop is the API's current word, not another stale snapshot.
        api.arm = False
        record = driver.registry.get(run_id)
        record["state"] = "stopped"
        driver.registry.put(record)
        loop.press("r")
        self.assertTrue(wait_for(lambda: app.runs and app.runs[0]["state"] == "stopped"), app.runs)
        self.assertNotIn(f"run={run_id}", app._uncertain)

    def test_a_status_started_before_stop_does_not_restore_the_old_run(self) -> None:
        self._run("run.status")


class LoopSurvivesBadInputTest(unittest.TestCase):
    def test_null_output_does_not_kill_the_loop(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        loop = Loop(app, driver.api)
        loop.start()
        self.addCleanup(loop.shutdown)
        self.assertTrue(wait_for(lambda: app.runs))
        loop.press("a")
        self.assertTrue(wait_for(lambda: app.attached == run_id))
        loop.post_event({"run_id": run_id, "text": None})
        loop.post_event({"run_id": run_id, "text": "still here\n"})
        self.assertTrue(wait_for(lambda: "still here" in "\n".join(t for t, _ in loop.frame)), loop.frame)
        loop.press("q")
        self.assertTrue(wait_for(lambda: app.closed), "q never closed the TUI")

    def test_unserializable_stop_answer_does_not_kill_the_stop_lane(self) -> None:
        class Circular:
            def __init__(self) -> None:
                self.stops = 0

            def call(self, method, params=None):
                if method == "run.stop":
                    self.stops += 1
                    if self.stops == 1:
                        answer = {"code": "stopped", "run_id": "r", "reason": "stopped"}
                        answer["loop"] = answer
                        return answer
                    return {"code": "stopped", "run_id": "r", "reason": "stopped", "repeat": True}
                return {"code": "ok", "runs": [{"run_id": "r", "state": "running", "engine": "e", "tenant": "t"}]}

        api = Circular()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        loop = Loop(app, api)
        loop.start()
        self.addCleanup(loop.shutdown)
        self.assertTrue(wait_for(lambda: app.runs))
        loop.press("s")
        self.assertTrue(wait_for(lambda: app.mode == "confirm_stop"))
        loop.press("y")
        self.assertTrue(wait_for(lambda: app.last_method == "run.stop"), "the first Stop answer never arrived")
        self.assertEqual(app.last["code"], "stopped")
        self.assertEqual(app.runs[0]["state"], "stopped")
        loop.press("s")
        self.assertTrue(wait_for(lambda: app.mode == "confirm_stop"))
        loop.press("y")
        self.assertTrue(wait_for(lambda: api.stops == 2), api.stops)
        self.assertEqual(app.last.get("repeat"), True)
