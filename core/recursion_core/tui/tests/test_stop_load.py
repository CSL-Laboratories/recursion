"""Required test 4: Stop under an output flood.

The fake driver emits engine output at a fixed 20,000 events/s (100-byte lines, batches of 200 every
10 ms) into a running Loop. The bound: the run.stop request leaves the TUI within STOP_BOUND_S = 250 ms
of the confirming key press, and the screen ends on the "stopped" or "pending" the API reported.
Measured 2026-11-07 on the Grok Bot box at 9e8e89cd, 10 trials each (5 stopped, 5 pending): worst 0.8 ms,
median 0.6 ms on CPython 3.13.5; worst 0.6 ms, median 0.4 ms on 3.14.10 free-threaded (-X gil=0). The
flood was checked separately at 20.2k events/s emitted, all drained, about 70 frames/s, none dropped.
The test prints its measured worst and median on every run.
"""

from __future__ import annotations

import statistics
import sys
import time
import unittest

from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.tui import protocol as p
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI

STOP_BOUND_S = 0.250
RATE_PER_S = 20_000
TRIALS = 10


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.002)
    return False


class StopUnderLoadTest(unittest.TestCase):
    def one_trial(self, confirm, want_code: str) -> float:
        driver = FakeDriver(confirm_answers=confirm)
        run_id = driver.start_run()
        api = RecordingAPI(driver.api)
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=100, height=30)
        loop = Loop(app, api)
        loop.start()
        try:
            self.assertTrue(wait_for(lambda: app.runs))
            loop.press("a")
            self.assertTrue(wait_for(lambda: app.attached == run_id))
            driver.emit(loop.post_event, run_id, RATE_PER_S)
            self.assertTrue(wait_for(lambda: app.outputs.get(run_id) and app.outputs[run_id].events > 2000))
            loop.press("s")
            self.assertTrue(wait_for(lambda: app.mode == "confirm_stop"))
            t0 = time.monotonic()
            loop.press("y")
            self.assertTrue(wait_for(lambda: any(m == "run.stop" for m, _ in api.times)))
            latency = next(t for m, t in api.times if m == "run.stop") - t0
            self.assertTrue(wait_for(lambda: loop.frame and loop.frame[1][0].startswith(f"status={want_code}")),
                            loop.frame[1] if loop.frame else None)
            self.assertGreater(driver.emitted, 2000, "the flood was running")
            screen = "\n".join(t for t, _ in loop.frame)
            self.assertIn(f"control API answered {want_code}", screen)
            if want_code == "pending":
                self.assertIn(f"{p.UNCERTAIN}: stop not confirmed", screen)
                self.assertNotIn("status=stopped", screen)
            return latency
        finally:
            driver.halt()
            loop.shutdown()

    def test_stop_leaves_within_bound_and_screen_shows_api_answer(self) -> None:
        latencies = [self.one_trial([EMPTY], "stopped") for _ in range(TRIALS // 2)]
        latencies += [self.one_trial([POPULATED], "pending") for _ in range(TRIALS - TRIALS // 2)]
        worst = max(latencies)
        print(f"\nstop-under-load {RATE_PER_S}/s: worst={worst * 1000:.1f}ms "
              f"median={statistics.median(latencies) * 1000:.1f}ms bound={STOP_BOUND_S * 1000:.0f}ms "
              f"python={sys.version.split()[0]}", file=sys.stderr)
        self.assertLess(worst, STOP_BOUND_S)

    def test_slow_read_does_not_delay_stop(self) -> None:
        """Stop has its own lane: a read stuck in the API cannot hold the stop request back."""

        driver = FakeDriver()
        run_id = driver.start_run()
        gate = __import__("threading").Event()

        class SlowReads(RecordingAPI):
            def call(self, method, params=None):
                if method == "run.status":
                    gate.wait(5)
                return super().call(method, params)

        api = SlowReads(driver.api)
        app = TuiApp(api, clock=Clock(), environ={})
        loop = Loop(app, api)
        loop.start()
        try:
            self.assertTrue(wait_for(lambda: app.runs))
            loop.press("a")  # status read now blocks in the API
            loop.press("s")
            t0 = time.monotonic()
            loop.press("y")
            self.assertTrue(wait_for(lambda: any(m == "run.stop" for m, _ in api.times), 1.0))
            self.assertLess(next(t for m, t in api.times if m == "run.stop") - t0, STOP_BOUND_S)
        finally:
            gate.set()
            loop.shutdown()

    def test_presentation_queue_is_bounded_and_drops_are_shown(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={})
        loop = Loop(app, driver.api, event_cap=100)
        app.open()
        app.attach(run_id)
        for i in range(250):
            loop.post_event({"run_id": run_id, "text": f"line {i}\n"})
        loop.tick()
        self.assertEqual(loop.dropped_events, 150)
        self.assertIn("skipped_events=150", "\n".join(t for t, _ in loop.frame))


if __name__ == "__main__":
    unittest.main()
