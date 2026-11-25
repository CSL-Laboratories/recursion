"""A confirmed Stop is said to be sent only when its request was written to the daemon, and never dropped silently.

After Ctrl-C the TUI waits, within the close's one bound, until the request of a Stop in its call is written (the
transport says so the moment its write returns: ``on_sent``); it does not take a time for that fact. The last line
says of each Stop only the step it reached: sent (written), answered, ended without being written, begun and not
yet written when the bound ran out (with what to do), or, for a transport that cannot say when it wrote, only that
its call began. Also: queued Stops of two runs are said in the plural, and a request of the other lane that is in
its call at the close is named.

Stand-ins only: a stand-in API in this process, a scripted terminal, and one socket in a temporary folder of the
test's own with a stand-in daemon on a thread; no daemon, socket or terminal of a person. Every wait is bounded,
and every test ends the threads it starts.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest

from recursion_core.tui import app as tui_app
from recursion_core.tui.tests.test_stop_at_close import API, WAIT_S, new_loop, start_lanes
from recursion_core.tui.tests.test_stop_ctrl_c import BOUND_S, MARGIN_S
from recursion_core.tui.tests.test_stop_orders import HANDOVER_S, TAIL, WAITED_S, Held, drive, later


class HeldBeforeTheWrite(Held):
    """Held, but the Stop's thread is held back between the start of its call and the write of its request (a
    loaded or swapping machine): its request is written, and ``on_sent`` called, only once ``release`` is set."""

    def call(self, method, params, on_sent=None):
        if method != "run.stop":
            return super().call(method, params)
        self.entered.set()
        if not self.release.wait(WAIT_S):
            raise AssertionError("write release missing")
        if on_sent is not None:
            on_sent()
        return super().call(method, params)  # then as Held: taken, and answered or held by ``holds``


class Unmarked:
    """An api whose ``call`` takes no ``on_sent``: it cannot say when a request is written (another transport)."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def call(self, method, params):
        return self.inner.call(method, params)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class WrittenOrNotTest(unittest.TestCase):
    def test_a_stop_held_back_before_its_write_is_never_said_sent(self) -> None:
        api = HeldBeforeTheWrite(("r",), {})
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "q"])
        self.assertIsNotNone(t_ctrl)
        self.assertNotIn("was sent", app.end_line)
        self.assertIn("the Stop of r was begun and is not yet written to the daemon; the TUI waits for that at most ",
                      app.end_line)
        self.assertGreaterEqual(t_end - t_ctrl, WAITED_S, "a Stop not yet written was dropped to come back sooner")
        self.assertLess(t_end - t_ctrl, BOUND_S + MARGIN_S, "the prompt came after the bound")
        self.assertEqual(lines[-1], "after the close line: the Stop of r was begun and its request was not written to "
                                    f"the daemon when the {BOUND_S:g} s bound ran out; it may not have reached the "
                                    "daemon" + TAIL.format("r"))

    def test_a_stop_written_within_the_bound_is_said_sent_and_the_prompt_comes_then(self) -> None:
        api = HeldBeforeTheWrite(("r",), {"r": [threading.Event()]})  # written 0.3 s after Ctrl-C, then hangs
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "q"], then=later(api.release, 0.3))
        self.assertIn("the Stop of r was begun and is not yet written to the daemon", app.end_line)
        self.assertGreaterEqual(t_end - t_ctrl, 0.3 - 0.05, "the prompt came before the request was written")
        self.assertLess(t_end - t_ctrl, 0.3 + MARGIN_S, "the TUI waited on after the request was written")
        self.assertEqual(api.sent("run.stop"), [{"run_id": "r"}])
        self.assertEqual(lines[-1], "after the close line: the Stop of r was sent and its answer had not come"
                         + TAIL.format("r"))

    def test_a_transport_that_cannot_say_it_wrote_is_said_only_to_have_begun(self) -> None:
        inner = Held(("r",), {})
        inner.holds["r"] = [inner.release]
        api = Unmarked(inner)
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "q"])
        self.assertIsNotNone(t_ctrl)
        self.assertNotIn("was sent", app.end_line)
        self.assertIn("the Stop of r: its call began and its answer had not come; whether it reached the daemon is "
                      "not known; look with run status before you trust that r stopped", app.end_line)
        self.assertGreaterEqual(t_end - inner.last_began("r"), HANDOVER_S - 0.05, "a Stop just begun was cut off")
        self.assertLess(t_end - t_ctrl, HANDOVER_S + MARGIN_S, "the TUI waited past the hand-over")
        self.assertEqual(lines, [app.end_line])  # nothing more is known after the close line


class PluralAndOtherLaneTest(unittest.TestCase):
    def test_the_queued_stops_of_two_runs_are_said_in_the_plural(self) -> None:
        api = Held(("r", "r2", "r3"), {}, signal="r")
        api.holds["r"] = [api.release]
        lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "j", "s", "y", "q"])
        self.assertIn("the Stops of r2 and r3 were not begun (they wait behind the Stop of r); they are sent only if",
                      app.end_line)
        self.assertLess(t_end - t_ctrl, BOUND_S + MARGIN_S)

    def test_each_form_of_the_queued_stops(self) -> None:
        said = tui_app._queued_said
        self.assertEqual(said([], ["r2"]), ("the Stop of r2", False))
        self.assertEqual(said(["r"], ["r2", "r3"]), ("the Stops of r2 and r3", True))
        self.assertEqual(said(["r"], ["r"]), ("another Stop of r", False))
        self.assertEqual(said(["r"], ["r2", "r"]), ("the Stop of r2 and another Stop of r", True))

    def test_a_start_in_its_call_at_the_close_is_named(self) -> None:
        class StartHangs(API):
            def __init__(self) -> None:
                super().__init__()
                self.start_entered = threading.Event()

            def call(self, method, params, on_sent=None):
                if method == "run.start":
                    self.start_entered.set()
                    if not self.release.wait(WAIT_S):
                        raise AssertionError("start release missing")
                return super().call(method, params)

        api = StartHangs()
        app, loop = new_loop(api)
        start_lanes(loop)
        try:
            app.request("run.start", {"engine": "e1"})
            self.assertTrue(api.start_entered.wait(WAIT_S))
            app.close()
            self.assertRegex(app.end_line, r"the request run\.start \S*e1\S* was in its call; whether it reached the "
                                           r"daemon is not known; look with run list")
        finally:
            api.release.set()
            loop.shutdown()
            for thread in loop._threads:
                thread.join(WAIT_S)


class OneStepForwardTest(unittest.TestCase):
    def test_a_stops_step_never_moves_back(self) -> None:
        app, loop = new_loop(API())
        app._stop_step.update({7: "answered", 8: "written", 9: "begun"})
        app.stop_moved(7, "written")
        app.stop_moved(8, "begun")
        app.stop_moved(9, "queued")
        self.assertEqual([app._stop_step[n] for n in (7, 8, 9)], ["answered", "written", "begun"])
        app.stop_moved(9, "written")
        app.stop_moved(8, "ended")
        self.assertEqual([app._stop_step[8], app._stop_step[9]], ["ended", "written"])


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "a Unix socket")
class SocketTransportMarksTheWriteTest(unittest.TestCase):
    def test_on_sent_comes_once_the_frame_is_written_and_before_the_reply(self) -> None:
        from recursion_core.control.client import SocketAPI
        from recursion_core.control_transport.unix import read_frame, write_frame

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(path)
            server.listen(1)
            got, may_answer, seen = threading.Event(), threading.Event(), []

            def daemon() -> None:
                conn, _ = server.accept()
                with conn:
                    seen.append(read_frame(conn, max_bytes=1 << 20, deadline=time.monotonic() + WAIT_S))
                    got.set()
                    may_answer.wait(WAIT_S)
                    write_frame(conn, {"ok": True, "result": {"code": "stopped", "run_id": "r"}}, max_bytes=1 << 20)

            t = threading.Thread(target=daemon, daemon=True)
            t.start()
            marks = []

            def on_sent() -> None:
                marks.append(("written", got.wait(WAIT_S)))  # the daemon has the request, and has not answered
                may_answer.set()

            answer = SocketAPI(path, timeout_s=WAIT_S).call("run.stop", {"run_id": "r"}, on_sent=on_sent)
            t.join(WAIT_S)
            self.assertEqual(marks, [("written", True)])
            self.assertEqual(answer, {"code": "stopped", "run_id": "r"})
            self.assertEqual(seen[0]["method"], "run.stop")

    def test_on_sent_never_comes_when_the_connect_fails(self) -> None:
        from recursion_core.control.client import SocketAPI

        with tempfile.TemporaryDirectory() as tmp:
            marks = []
            answer = SocketAPI(os.path.join(tmp, "none.sock"), timeout_s=WAIT_S).call(
                "run.stop", {"run_id": "r"}, on_sent=lambda: marks.append("written"))
            self.assertEqual(marks, [])
            self.assertIs(answer.get("delivered"), False)


if __name__ == "__main__":
    unittest.main()
