"""A confirmed Stop is said to be sent only when its request was written to the daemon, and never dropped silently.

After Ctrl-C the TUI waits, within the close's one bound, until the request of a Stop in its call is written (the
transport says so the moment its write returns: ``on_sent``); it does not take a time for that fact. The last line
says of each Stop only the step it reached: sent (written), answered, ended without being written, begun and not
yet written when the bound ran out (with what to do), or, for a transport that cannot say when it wrote, only that
its call began. Also: queued Stops of two runs are said in the plural, and a request of the other lane that is in
its call at the close is named. Begun Stops of two runs are said in the plural; a fault of the hook that is not an
Exception does not end the call.

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


class Door:
    """A stand-in daemon's door on a Unix socket in the test's own temporary folder. ``closes``: it accepts the
    connection and closes it before reading anything; ``refuses``: bound and not listening, so the connect is
    refused; ``answers``: it reads the whole request and answers that the run is stopped. ``got`` is the bytes it
    received, ``accepted`` the connections it took."""

    def __init__(self, path: str, kind: str) -> None:
        self.kind, self.got, self.accepted, self.closed = kind, 0, 0, threading.Event()
        self.path, self.release, self.replied = path, threading.Event(), threading.Event()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        if kind != "refuses":
            self.srv.listen(4)
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()

    def _serve(self) -> None:
        from recursion_core.control_transport.unix import write_frame

        self.srv.settimeout(WAIT_S)
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.accepted += 1
        with conn:
            if self.kind in ("answers", "late"):
                conn.settimeout(WAIT_S)
                buf = b""
                while len(buf) < 4 or len(buf) < 4 + int.from_bytes(buf[:4], "big"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                self.got += len(buf)
                if self.kind == "late":
                    self.release.wait(WAIT_S)
                try:
                    write_frame(conn, {"ok": True, "result": {"code": "stopped", "run_id": "r"}}, max_bytes=1 << 20)
                except OSError:
                    pass
                self.replied.set()
        self.closed.set()

    def end(self) -> None:
        self.srv.close()
        if self.kind != "refuses":
            self.thread.join(WAIT_S)


class StopThroughTheDoor(API):
    """The stand-in API for every read; each Stop goes through the real SocketAPI and UnixControlClient to the door.
    Its connect is let go only at Ctrl-C (``gate``), so its call ends after the close line; for the door that closes,
    the connection is made, the door closes it, and only then is the request written. ``hook``, when given, stands
    between the transport and the TUI's ``on_sent`` (a fault there)."""

    def __init__(self, path: str, door: Door, gate: threading.Event, hook=None) -> None:
        from recursion_core.control.client import SocketAPI
        from recursion_core.control_transport.unix import UnixControlClient

        super().__init__(runs=("r",))
        self.holds: dict = {}
        self.hook = hook

        def connect(p, *, timeout_s):
            gate.wait(WAIT_S)
            client = UnixControlClient(p, timeout_s=timeout_s)
            if door.kind == "closes":
                door.closed.wait(WAIT_S)
            return client

        self.sock_api = SocketAPI(path, timeout_s=WAIT_S, connect=connect)

    def call(self, method, params, on_sent=None):
        if method != "run.stop":
            return super().call(method, params)
        with self._lock:
            self.calls.append((method, dict(params)))
        self.entered.set()
        hook = on_sent if self.hook is None or on_sent is None else (lambda: self.hook(on_sent))
        return self.sock_api.call(method, params, on_sent=hook)


def _through_a_door(test, kind, *, marked=True, hook=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "control.sock")
        door, gate = Door(path, kind), threading.Event()
        test.addCleanup(door.end)
        api = StopThroughTheDoor(path, door, gate, hook)
        lines, app, t_ctrl, t_end = drive(api if marked else Unmarked(api), ["s", "y", "q"], then=gate.set)
        test.assertIsNotNone(t_ctrl)
        test.assertLess(t_end - t_ctrl, BOUND_S + MARGIN_S, "the prompt came after the bound")
        # the Stop's call ends just after Ctrl-C: its end ends the wait, the bound is not waited out
        test.assertLess(t_end - t_ctrl, WAITED_S, "the TUI waited on after the Stop's call had ended")
        return lines, app, door


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "a Unix socket")
class CallEndedBeforeItsWriteTest(unittest.TestCase):
    NOT_SENT = ("after the close line: the Stop of r was not sent: its call ended before its request was written "
                "(control_api_unavailable); r may still be running" + TAIL.format("r"))

    def test_a_door_that_closes_before_the_write_is_said_not_sent_and_got_nothing(self) -> None:
        lines, app, door = _through_a_door(self, "closes")
        self.assertIn("the Stop of r was begun and is not yet written to the daemon", app.end_line)
        self.assertEqual(lines[-1], self.NOT_SENT)
        self.assertEqual((door.accepted, door.got), (1, 0))

    def test_a_door_that_refuses_the_connection_is_said_not_sent_and_got_nothing(self) -> None:
        lines, app, door = _through_a_door(self, "refuses")
        self.assertEqual(lines[-1], self.NOT_SENT)
        self.assertEqual((door.accepted, door.got), (0, 0))

    def test_a_transport_that_cannot_say_it_wrote_says_its_call_ended(self) -> None:
        lines, app, door = _through_a_door(self, "refuses", marked=False)
        self.assertEqual(lines[-1], "after the close line: the Stop of r: its call ended and did not confirm it "
                                    "(control_api_unavailable); r may still be running" + TAIL.format("r"))
        self.assertEqual((door.accepted, door.got), (0, 0))


class TheWaitEndsOnlyOnTheMarkTheEndOrTheBoundTest(unittest.TestCase):
    def test_a_stop_begun_while_the_close_looks_is_waited_for(self) -> None:
        """The window: the close looks whether every confirmed Stop is handed over while the Stop lane takes the
        queued Stop and begins its call. Forced with no sleep: the first time the look takes the app's lock, the
        lane is let take the Stop and begin its call (held before its write) before the lock is taken."""

        api = HeldBeforeTheWrite(("r",), {})
        app, loop = new_loop(api)
        app.request("run.stop", {"run_id": "r"})  # a confirmed Stop, queued: the lanes are not started yet
        real, looker = app._lock, threading.current_thread()

        class Window:
            armed = True

            def __enter__(self):
                if Window.armed and threading.current_thread() is looker:
                    Window.armed = False
                    lane = threading.Thread(target=loop._lane, args=(loop._lanes["stop"],), daemon=True)
                    loop._threads.append(lane)
                    lane.start()
                    if not api.entered.wait(WAIT_S):  # the Stop is begun and in its call, not yet written
                        raise AssertionError("the Stop was not begun")
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(real, name)

        app._lock = Window()
        try:
            self.assertFalse(loop._stops_handed_over(), "the wait ended on a Stop begun and not written")
            self.assertEqual(app._stop_step[next(iter(app._stops_out))], "begun")
        finally:
            app._lock = real
            api.release.set()
            loop._lanes["stop"].put(None)
            for thread in loop._threads:
                thread.join(WAIT_S)
        self.assertTrue(loop._stops_handed_over(), "a Stop whose call has ended is still waited for")


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "a Unix socket")
class AHookThatFailsTest(unittest.TestCase):
    def test_a_hook_that_raises_after_a_whole_write_does_not_end_the_call(self) -> None:
        from recursion_core.control.client import SocketAPI

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            door = Door(path, "answers")
            self.addCleanup(door.end)
            marks = []

            def on_sent() -> None:
                marks.append("written")
                raise RuntimeError("the hook itself fails")

            answer = SocketAPI(path, timeout_s=WAIT_S).call("run.stop", {"run_id": "r"}, on_sent=on_sent)
            self.assertEqual(marks, ["written"])
            self.assertEqual(answer, {"code": "stopped", "run_id": "r"})
            self.assertTrue(door.closed.wait(WAIT_S))
            self.assertGreater(door.got, 4)

    def test_a_hook_that_fails_before_the_tuis_mark_does_not_make_a_written_stop_unsent(self) -> None:
        def fails(on_sent) -> None:
            raise RuntimeError("the hook itself fails")

        lines, app, door = _through_a_door(self, "answers", hook=fails)
        self.assertGreater(door.got, 4)
        self.assertEqual(lines[-1], "after the close line: the Stop of r was sent and answered: r is stopped")


class StopThroughTwoDoors(API):
    """The stand-in API for every read; a Stop of r goes through the real SocketAPI and UnixControlClient to the door
    in ``paths["r"]`` and a Stop of r2 to the door in ``paths["r2"]``. Every connect is let go only when ``gate`` is set."""

    def __init__(self, paths: dict, gate: threading.Event) -> None:
        from recursion_core.control.client import SocketAPI
        from recursion_core.control_transport.unix import UnixControlClient

        super().__init__(runs=("r", "r2"))
        self.holds: dict = {}

        def connect(p, *, timeout_s):
            gate.wait(WAIT_S)
            return UnixControlClient(p, timeout_s=timeout_s)

        self.apis = {run: SocketAPI(path, timeout_s=WAIT_S, connect=connect) for run, path in paths.items()}

    def call(self, method, params, on_sent=None):
        if method != "run.stop":
            return super().call(method, params)
        with self._lock:
            self.calls.append((method, dict(params)))
        self.entered.set()
        return self.apis[params["run_id"]].call(method, params, on_sent=on_sent)


class HookStops(BaseException):
    """A fault of a layer between the transport and the TUI that is not an Exception."""


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "a Unix socket")
class AHookThatIsNotAnExceptionTest(unittest.TestCase):
    def test_a_base_exception_of_the_hook_after_a_whole_write_does_not_end_the_call(self) -> None:
        from recursion_core.control.client import SocketAPI

        with tempfile.TemporaryDirectory() as tmp:
            door = Door(os.path.join(tmp, "control.sock"), "answers")
            self.addCleanup(door.end)
            marks = []

            def on_sent() -> None:
                marks.append("written")
                raise HookStops()

            answer = SocketAPI(door.path, timeout_s=WAIT_S).call("run.stop", {"run_id": "r"}, on_sent=on_sent)
            self.assertEqual(marks, ["written"])
            self.assertEqual(answer, {"code": "stopped", "run_id": "r"})
            self.assertTrue(door.closed.wait(WAIT_S))
            self.assertGreater(door.got, 4)

    def test_an_interrupt_of_the_hook_is_raised_again_only_after_the_reply_is_read(self) -> None:
        from recursion_core.control_transport.unix import UnixControlClient

        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt.__name__), tempfile.TemporaryDirectory() as tmp:
                door = Door(os.path.join(tmp, "control.sock"), "late")
                self.addCleanup(door.end)
                client = UnixControlClient(door.path, timeout_s=WAIT_S)
                self.addCleanup(client.close)
                threading.Timer(0.3, door.release.set).start()  # the daemon answers 0.3 s after the write
                t0 = time.monotonic()

                def on_sent() -> None:
                    raise interrupt()

                with self.assertRaises(interrupt):
                    client.request({"method": "run.stop", "params": {"run_id": "r"}}, on_sent=on_sent)
                self.assertGreaterEqual(time.monotonic() - t0, 0.3 - 0.05, "the interrupt came before the reply was read")
                self.assertTrue(door.replied.is_set(), "the interrupt came before the daemon's reply")
                self.assertGreater(door.got, 4)

    def test_a_base_exception_before_the_tuis_mark_does_not_make_a_written_stop_unsent(self) -> None:
        def fails(on_sent) -> None:
            raise HookStops()

        lines, app, door = _through_a_door(self, "answers", hook=fails)
        self.assertGreater(door.got, 4)
        self.assertEqual(lines[-1], "after the close line: the Stop of r was sent and answered: r is stopped")


class BegunStopsOfTwoRunsTest(unittest.TestCase):
    """The Stops of two runs begun at the close are said in the plural, every run named, by the helper the queued
    Stops use; a Stop of one run is said as before."""

    def _app(self, steps: dict, *, marks: bool = True):
        api = Held(("r", "r2"), {})
        app, loop = new_loop(api)
        app.stop_marks = marks
        for run in ("r", "r2"):
            app.request("run.stop", {"run_id": run})
        number = {run: n for n, run in app._stops_out.items()}
        for run, n in number.items():
            self.assertTrue(app.begin(n, "run.stop", {"run_id": run}))
        for run, step in steps.items():
            app.stop_moved(number[run], step)
        return app

    UNWRITTEN = (r"the Stops of r and r2 were begun and are not yet written to the daemon; the TUI waits for that at "
                 r"most [0-9]\.[0-9]{2} s and then says what became of them; ")

    def test_two_begun_stops_not_yet_written_are_said_in_the_plural(self) -> None:
        self.assertRegex(self._app({})._end_line(), self.UNWRITTEN)

    def test_the_mixed_case_a_call_ended_and_a_stop_begun_is_said_in_the_plural(self) -> None:
        self.assertRegex(self._app({"r": "ended"})._end_line(), self.UNWRITTEN)

    def test_two_written_stops_are_said_in_the_plural(self) -> None:
        self.assertIn("the Stops of r and r2 were sent and their answers had not come",
                      self._app({"r": "written", "r2": "written"})._end_line())

    def test_two_stops_of_a_transport_that_cannot_say_it_wrote_are_said_in_the_plural(self) -> None:
        self.assertIn("the Stops of r and r2: their calls began and their answers had not come; whether they reached "
                      "the daemon is not known", self._app({}, marks=False)._end_line())

    def test_one_begun_stop_is_said_in_the_singular(self) -> None:
        line = self._app({"r2": "written"})._end_line()
        self.assertRegex(line, r"the Stop of r was begun and is not yet written to the daemon; the TUI waits for that "
                               r"at most [0-9]\.[0-9]{2} s and then says what became of it; ")
        self.assertIn("the Stop of r2 was sent and its answer had not come", line)


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "a Unix socket")
class TheEndOfAStopsCallEndsTheWaitTest(unittest.TestCase):
    def test_a_stop_refused_after_ctrl_c_and_the_next_one_written_to_a_late_door_return_the_prompt(self) -> None:
        """r's connect is refused 0.3 s after Ctrl-C (the TUI's tick has ended: its answer is not taken yet), then
        the queued Stop of r2 is written to a door that answers only after the bound. The prompt comes once r's
        call has ended and r2's request is written, not at the bound."""

        with tempfile.TemporaryDirectory() as tmp:
            refuses = Door(os.path.join(tmp, "a.sock"), "refuses")
            late = Door(os.path.join(tmp, "b.sock"), "late")
            self.addCleanup(refuses.end)
            self.addCleanup(late.end)
            gate = threading.Event()
            api = StopThroughTwoDoors({"r": refuses.path, "r2": late.path}, gate)
            late.release = api.release  # the drive sets it when it ends
            lines, app, t_ctrl, t_end = drive(api, ["s", "y", "j", "s", "y", "q"], then=later(gate, 0.3))
            self.assertIsNotNone(t_ctrl)
            self.assertGreaterEqual(t_end - t_ctrl, 0.3 - 0.05, "the prompt came before the refused Stop's call ended")
            self.assertLess(t_end - t_ctrl, WAITED_S, "the TUI waited on after r's call ended and r2 was written")
            self.assertEqual((refuses.accepted, late.got > 4), (0, True))
            self.assertTrue(lines[-1].startswith(
                "after the close line: the Stop of r was not sent: its call ended before its request was written "
                "(control_api_unavailable); r may still be running; the queued Stop of r2 was sent and its answer "
                "had not come"), lines[-1])


if __name__ == "__main__":
    unittest.main()
