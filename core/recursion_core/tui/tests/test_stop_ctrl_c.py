"""Ctrl-C while the TUI waits for a confirmed Stop's answer ends it at once, as the notice says: the screen is left
and the true last line is on it within MARGIN_S, in full screen and inline, and with a lone hung Stop the TUI's
function returns then too. A Stop still queued behind the hung one is waited for within the bound and sent if it
can begin. Stand-ins only: a stand-in API in this process and a scripted terminal; every wait is bounded, and every
test ends the threads it starts.
"""

from __future__ import annotations

import io
import threading
import time
import unittest
from unittest import mock

from recursion_core.tui import term
from recursion_core.tui.tests.test_inline import FakeTerminal
from recursion_core.tui.tests.test_stop_at_close import API, WAIT_S

BOUND_S = 2.0  # the Loop's bound in these tests: long enough that a wait for the rest of it is plain to see
MARGIN_S = 0.5  # how soon after the Ctrl-C the last line must be on the screen (a tick, a key read, a paint)
HUNG_LINE = ("closed: the Stop of r was sent and its answer had not come; look with run status before you trust "
             "that r stopped; every other run keeps going and can be reattached")


class Stamped(io.StringIO):
    """A stream that keeps when each write came."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[float, str]] = []

    def write(self, text):
        self.writes.append((time.monotonic(), text))
        return super().write(text)

    def first(self, part: str) -> float | None:
        return next((t for t, text in self.writes if part in text), None)


def run(api, keys_then_ctrl_c, *, alt_screen: bool, then=None):
    """term.interactive with the keys, then Ctrl-C once the close waits; returns (out, err, apps, t_ctrl, t_end)."""

    loops, apps, stamp = [], [], {}
    real_loop = term.Loop

    class BoundLoop(real_loop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.join_timeout_s = BOUND_S
            loops.append(self)

    class Keys(FakeTerminal):
        def read_key(self, timeout):
            app = apps[0] if apps else None
            if self.keys and not (app and app.runs):
                time.sleep(0.01)
                return None
            if not self.keys and "ctrl" not in stamp:
                if app and app.awaiting_stop and api.entered.is_set():
                    time.sleep(0.1)  # some ticks of the wait go by first
                    stamp["ctrl"] = time.monotonic()
                    if then is not None:
                        then()
                    return "ctrl+c"
                time.sleep(0.01)
                return None
            return super().read_key(timeout)

    out, err = Stamped(), Stamped()
    try:
        with mock.patch.object(term, "Loop", BoundLoop):
            code = term.interactive(api, out, {"NO_COLOR": "1"}, Keys(list(keys_then_ctrl_c)), alt_screen=alt_screen,
                                    on_app=apps.append, stderr=err)
        stamp["end"] = time.monotonic()
        assert code == 0
    finally:
        api.release.set()
        for loop in loops:
            for thread in loop._threads:
                thread.join(WAIT_S)
    return out, err, apps, stamp.get("ctrl"), stamp["end"]


class CtrlCWithAHungStopTest(unittest.TestCase):
    def test_full_screen_the_line_is_on_the_screen_at_once(self) -> None:
        api = API(slow=("r",))
        out, err, apps, t_ctrl, t_end = run(api, ["s", "y", "q"], alt_screen=True)
        self.assertIsNotNone(t_ctrl)
        left, said = out.first("\x1b[?1049l"), err.first(HUNG_LINE)
        self.assertIsNotNone(said, err.getvalue())
        self.assertLess(left - t_ctrl, MARGIN_S, "the alternate screen was left only after the wait")
        self.assertLess(said - t_ctrl, MARGIN_S, "the last line came only after the wait")
        self.assertLess(t_end - t_ctrl, MARGIN_S, "the TUI waited for a Stop already sent and hung")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])

    def test_inline_the_line_is_written_and_the_tui_returns_at_once(self) -> None:
        api = API(slow=("r",))
        out, err, apps, t_ctrl, t_end = run(api, ["s", "y", "q"], alt_screen=False)
        self.assertIsNotNone(t_ctrl)
        said = out.first("the Stop of r was sent and its answer had not come")
        self.assertIsNotNone(said, out.getvalue()[-400:])
        self.assertLess(said - t_ctrl, MARGIN_S)
        self.assertLess(t_end - t_ctrl, MARGIN_S, "the TUI waited for a Stop already sent and hung")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])


class CtrlCWithAQueuedStopTest(unittest.TestCase):
    def test_a_queued_stop_is_still_waited_for_and_sent_when_it_can_begin(self) -> None:
        api = API(runs=("r", "r2"), slow=("r",))

        def release_soon():  # the hung Stop ends soon after the Ctrl-C, well within the bound
            threading.Timer(0.8, api.release.set).start()

        out, err, apps, t_ctrl, t_end = run(api, ["s", "y", "j", "s", "y", "q"], alt_screen=True, then=release_soon)
        self.assertIsNotNone(t_ctrl)
        said = err.first("the Stop of r2 was not begun (it waits behind the Stop of r); it is sent only if the Stop "
                         "of r ends within ")
        self.assertIsNotNone(said, err.getvalue())
        self.assertLess(said - t_ctrl, MARGIN_S, "the last line came only after the wait")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}, {"run_id": "r2"}])
        self.assertGreaterEqual(t_end - t_ctrl, 0.75, "the queued Stop was not waited for")


if __name__ == "__main__":
    unittest.main()
