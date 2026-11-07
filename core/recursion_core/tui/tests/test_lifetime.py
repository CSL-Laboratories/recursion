"""Required tests 2 and 3: closing never stops work, reopening reattaches, and opening or attaching
never starts a model call (the fake driver's call count stays 0)."""

from __future__ import annotations

import io
import unittest

from recursion_core.control import cli
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI
from recursion_core.tui.tests.test_stop_load import wait_for


class LifetimeTest(unittest.TestCase):
    def test_close_every_client_and_the_run_keeps_going_then_reattach(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        launched = driver.model_calls
        api = RecordingAPI(driver.api)
        first, second = (TuiApp(api, clock=Clock(), environ={}) for _ in range(2))
        for app in (first, second):
            app.open()
            app.attach(run_id)
        api.calls.clear()
        first.close()
        second.close()
        self.assertEqual(api.calls, [], "closing sends nothing")
        self.assertEqual(driver.registry.get(run_id)["state"], "running")
        self.assertEqual(driver.kill.requests, [])

        again = TuiApp(api, clock=Clock(), environ={})
        again.open()
        self.assertEqual([r["run_id"] for r in again.runs], [run_id])
        again.press("a")
        self.assertEqual(again.attached, run_id)
        self.assertEqual(again.last["run"]["run_id"], run_id)
        self.assertEqual(again.last["run"]["state"], "running")
        self.assertEqual(api.calls, [("run.list", {}), ("run.status", {"run_id": run_id})])
        self.assertEqual(driver.model_calls, launched)

    def test_threaded_loop_close_leaves_the_run_running(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={})
        loop = Loop(app, driver.api)
        loop.start()
        loop.press("q")
        self.assertTrue(wait_for(lambda: app.closed), "the loop thread handles q before shutdown")
        loop.shutdown()
        self.assertEqual(driver.registry.get(run_id)["state"], "running")
        self.assertEqual(driver.kill.requests, [])

    def test_cli_and_tui_see_the_same_run_after_tui_closes(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={})
        app.open()
        app.close()
        out = io.StringIO()
        self.assertEqual(cli.main(["status", run_id], driver.api, out, io.StringIO()), cli.EXIT_OK)
        self.assertIn('"state": "running"', out.getvalue())


class NoModelCallOnAttachTest(unittest.TestCase):
    def test_open_attach_refresh_select_and_close_start_nothing(self) -> None:
        driver = FakeDriver()
        api = RecordingAPI(driver.api)
        app = TuiApp(api, clock=Clock(), environ={})
        app.open()
        self.assertEqual(driver.model_calls, 0)
        driver.api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})  # someone else starts it
        before = driver.model_calls
        for key in ("r", "j", "k", "a", "enter", "r", "down", "up", "q"):
            app.press(key)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "start --tenant t --engine e --prompt p\n"})
        self.assertEqual(driver.model_calls, before)
        self.assertEqual({m for m, _ in api.calls}, {"run.list", "run.status"})

    def test_opening_with_no_runs_starts_nothing(self) -> None:
        driver = FakeDriver()
        app = TuiApp(driver.api, clock=Clock(), environ={})
        app.open()
        app.press("a")
        app.press("s")
        app.press("y")
        self.assertEqual(driver.model_calls, 0)
        self.assertEqual(driver.registry.list(), [])


if __name__ == "__main__":
    unittest.main()
