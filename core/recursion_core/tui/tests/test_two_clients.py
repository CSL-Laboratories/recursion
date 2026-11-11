"""Required test 5: two clients on one run. Either can Stop without holding input; a repeated stop is
not applied twice. The item 7/8 control API has no mid-run verb that carries an expected turn or
revision (only list, status, stop, start), so there is no stale mid-run request to refuse yet; the
test pins that the TUI exposes none (gap raised on CSL-209)."""

from __future__ import annotations

import io
import json
import threading
import unittest

from recursion_core.control import cli
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.tui import protocol
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver


class TwoClientsTest(unittest.TestCase):
    def test_second_tui_stops_while_first_is_typing_a_command(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        typing, other = TuiApp(driver.api, clock=Clock(), environ={}), TuiApp(driver.api, clock=Clock(), environ={})
        for app in (typing, other):
            app.open()
        for key in ":start --tenant":  # first client is mid-input; nothing is leased by that
            typing.press(key)
        other.press("s")
        other.press("y")
        self.assertEqual(other.last["code"], "stopped")
        typing.press("esc")
        typing.press("r")
        self.assertEqual(typing.runs[0]["state"], "stopped")
        self.assertEqual(len(driver.kill.requests), 1)

    def test_tui_and_cli_both_stop_one_kill_request(self) -> None:
        driver = FakeDriver(confirm_answers=[POPULATED, EMPTY])
        run_id = driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={})
        app.open()
        app.press("s")
        app.press("y")
        self.assertEqual(app.last["code"], "pending")
        out = io.StringIO()
        self.assertEqual(cli.main(["stop", run_id], driver.api, out, io.StringIO()), cli.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["repeat"], True)
        app.command(f"stop {run_id}")
        self.assertEqual((app.last["code"], app.last["repeat"]), ("stopped", True))
        self.assertEqual(len(driver.kill.requests), 1, "the second stop is not applied twice")

    def test_concurrent_stops_from_many_tuis(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        apps = [TuiApp(driver.api, clock=Clock(), environ={}) for _ in range(8)]
        for app in apps:
            app.open()
        barrier = threading.Barrier(len(apps))

        def go(app: TuiApp) -> None:
            barrier.wait()
            app.command(f"stop {run_id}")

        threads = [threading.Thread(target=go, args=(a,)) for a in apps]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual({a.last["code"] for a in apps}, {"stopped"})
        self.assertEqual(sum(1 for a in apps if not a.last["repeat"]), 1)
        self.assertEqual(len(driver.kill.requests), 1)

    def test_no_mid_run_verb_exists_to_apply_twice(self) -> None:
        self.assertEqual(set(protocol.METHODS.values()), {"run.list", "run.status", "run.stop", "run.start"})
        for verb in ("steer", "queue", "interrupt", "resume", "fork", "input", "approve", "deny"):
            with self.assertRaises(protocol.CommandError):
                protocol.parse_command(f"{verb} run-1")


if __name__ == "__main__":
    unittest.main()
