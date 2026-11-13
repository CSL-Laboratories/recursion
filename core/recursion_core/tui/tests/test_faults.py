"""Loop faults: a slow read must not paint over a newer Stop, and one bad event or answer
must not kill the thread that handles keys and Stop."""

from __future__ import annotations

import threading
import time
import unittest

from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.tui import protocol as p
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI, ScriptedAPI


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


class _BlockBeforeRead(RecordingAPI):
    """Holds one armed call before the inner API reads, so the payload is whatever landed while it waited."""

    def __init__(self, inner, method: str) -> None:
        super().__init__(inner)
        self.method, self.arm = method, False
        self.entered, self.release = threading.Event(), threading.Event()
        self.payload = None

    def call(self, method, params=None):
        if method == self.method and self.arm:
            self.arm = False
            self.entered.set()
            self.release.wait(5)
        result = super().call(method, params)
        if method == self.method:
            self.payload = result
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

    def test_an_early_list_that_reads_stopped_replaces_the_pending_row(self) -> None:
        # A list blocked before it reads still carries an early start time. Once a later Stop has
        # confirmed, that list's stopped row is the registry's word and must replace stop_pending.
        driver = FakeDriver(confirm_answers=[POPULATED, EMPTY])
        run_id = driver.start_run()
        api = _BlockBeforeRead(driver.api, "run.list")
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        loop = Loop(app, api)
        loop.start()
        self.addCleanup(driver.halt)
        self.addCleanup(api.release.set)
        self.addCleanup(loop.shutdown)
        self.assertTrue(wait_for(lambda: app.runs), "open did not list the run")
        api.arm = True
        loop.press("r")
        self.assertTrue(api.entered.wait(2), "the list was not held before the read")
        loop.press("s")
        self.assertTrue(wait_for(lambda: app.mode == "confirm_stop"))
        loop.press("y")
        self.assertTrue(wait_for(lambda: app.runs[0]["state"] == "stop_pending"), app.runs)
        self.assertIn(f"run={run_id}", app._uncertain)
        self.assertIn(p.UNCERTAIN, {label for label, _ in app.conditions()})
        second = driver.api.call("run.stop", {"run_id": run_id})
        self.assertEqual(second["code"], "stopped")
        self.assertEqual(driver.registry.get(run_id)["state"], "stopped")
        self.assertEqual(app.runs[0]["state"], "stop_pending", "the UI has not seen the second Stop yet")
        api.payload = None
        api.release.set()
        self.assertTrue(wait_for(lambda: api.payload is not None and app.last_method == "run.list"))
        self.assertEqual(api.payload["runs"][0]["state"], "stopped")
        self.assertEqual(app.runs[0]["state"], "stopped", app.runs[0])
        self.assertNotIn(f"run={run_id}", app._uncertain)
        self.assertNotIn(p.UNCERTAIN, {label for label, _ in app.conditions()})


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

    def test_a_non_dict_stop_does_not_leave_stop_pending_without_uncertain(self) -> None:
        # stop: 1 is not a record. Spreading it raises after state and the Stop mark are written,
        # and the answer handler then records only the exception class.
        class API:
            def call(self, method, params=None):
                if method == "run.list":
                    return {"code": "ok", "runs": [
                        {"run_id": "r", "state": "running", "engine": "e", "tenant": "t", "stop": 1}]}
                if method == "run.stop":
                    return {"code": "pending", "run_id": "r", "reason": "cgroup_populated"}
                return {"code": "ok", "runs": []}

        api = API()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=100, height=30)
        loop = Loop(app, api)
        loop.start()
        self.addCleanup(loop.shutdown)
        self.assertTrue(wait_for(lambda: app.runs))
        loop.press("s")
        self.assertTrue(wait_for(lambda: app.mode == "confirm_stop"))
        loop.press("y")
        self.assertTrue(wait_for(lambda: app.last_method == "run.stop"), app.errors)
        row = app.runs[0]
        self.assertEqual(row["state"], "stop_pending")
        self.assertIsInstance(row["stop"], dict)
        self.assertEqual(row["stop"].get("reason"), "cgroup_populated")
        self.assertIn("r", app._read_at)
        self.assertEqual(app._stop_mark["r"][1], "stop_pending")
        self.assertIn("run=r", app._uncertain)
        self.assertEqual({label for label, _ in app.conditions()}, {p.UNCERTAIN, p.UNRECONCILED})
        self.assertNotIn("TypeError", " ".join(app.errors))
        screen = "\n".join(text for text, _ in app.frame())
        self.assertIn("UNCERTAIN:", screen)
        self.assertIn("UNRECONCILED:", screen)

    def test_older_status_with_a_non_dict_stop_does_not_clear_watch_only(self) -> None:
        # Clearing status_refresh_failed before the merge, then spreading stop: 1, raised
        # TypeError. Loop dropped the answer with the latch already clear, so watch_only
        # turned off and run.start was dispatched against an empty list.
        api = ScriptedAPI({"run.list": {"code": "ok", "runs": []},
                           "run.start": {"code": "started", "run": {"run_id": "new", "state": "running"}}})
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        app.open()
        self.assertEqual(app.runs, [])
        app.status_refresh_failed = "run.status answered control_api_unavailable"
        self.assertTrue(app.watch_only)
        app.on_answer("run.stop", {"run_id": "r"},
                      {"code": "pending", "run_id": "r", "reason": "cgroup_populated"})
        loop = Loop(app, api)
        answer = {"code": "ok", "run": {"run_id": "r", "state": "running", "stop": 1}}
        loop._answers.put((app.on_answer, "run.status", {"run_id": "r"}, answer, app._read_at["r"] - 1.0))
        loop.tick()
        self.assertNotIn("TypeError", " ".join(app.errors))
        self.assertEqual(app.runs[0]["state"], "stop_pending")
        self.assertIsInstance(app.runs[0]["stop"], dict)
        self.assertEqual(app.runs[0]["stop"].get("reason"), "cgroup_populated")
        self.assertIsNone(app.status_refresh_failed)
        self.assertFalse(not app.watch_only and not app.runs)
        app.command("start --tenant t1 --engine codex --prompt p")
        if not app.runs or app.runs[0].get("state") != "stop_pending":
            self.assertNotIn("run.start", [method for method, _ in api.calls])

    def test_malformed_list_row_on_the_loop_keeps_the_snapshot(self) -> None:
        # A code-ok run.list of [null] was filtered to [] and committed, clearing the latch.
        run = {"run_id": "r", "state": "running", "engine": "e", "tenant": "t"}

        class API:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self._lock = threading.Lock()
                self.bad = False

            def call(self, method, params=None):
                with self._lock:
                    self.calls.append(method)
                    bad = self.bad
                if method == "run.list":
                    return {"code": "ok", "runs": [None] if bad else [dict(run)]}
                if method == "run.start":
                    return {"code": "started", "run": {**run, "run_id": "new"}}
                return {"code": "ok", "run": dict(run)}

            def count(self, method: str) -> int:
                with self._lock:
                    return self.calls.count(method)

            def saw(self, method: str) -> bool:
                with self._lock:
                    return method in self.calls

        api = API()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        loop = Loop(app, api)
        loop.start()
        self.addCleanup(loop.shutdown)
        self.assertTrue(wait_for(lambda: [r.get("run_id") for r in app.runs] == ["r"]))
        app.refresh_failed = "earlier read failed"
        self.assertTrue(app.watch_only)
        api.bad = True
        loop.press("r")
        self.assertTrue(wait_for(lambda: api.count("run.list") >= 2 and app.last_method == "run.list"
                                 and app.in_flight["run.list"] == 0))
        self.assertEqual([r.get("run_id") for r in app.runs], ["r"])
        self.assertIsNotNone(app.refresh_failed)
        self.assertTrue(app.watch_only)
        for key in ":start --tenant t1 --engine codex --prompt p":
            loop.press(key)
        loop.press("enter")
        self.assertTrue(wait_for(lambda: api.saw("run.start") or any("watch_only" in err for err in app.errors)))
        self.assertFalse(api.saw("run.start"))
        self.assertEqual([r.get("run_id") for r in app.runs], ["r"])

    def test_keyboard_interrupt_is_not_turned_into_a_dropped_answer(self) -> None:
        api = RecordingAPI(FakeDriver().api)
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        loop = Loop(app, api)

        def boom(method, params, answer):
            raise KeyboardInterrupt

        loop._answers.put((boom, "run.stop", {"run_id": "r"}, {"code": "pending", "run_id": "r"}, 0.0))
        with self.assertRaises(KeyboardInterrupt):
            loop.tick()
        self.assertFalse(any("KeyboardInterrupt" in err for err in app.errors))
