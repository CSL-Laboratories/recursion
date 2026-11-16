"""The session's behaviour that fixtures cannot show: concurrent calls, cancellation, end of input, the
reserved Stop slot, back-pressure, and answers that cannot be written.

Every wait synchronises on an event (a call entering the gated API, an answer on the wire). Time is used
only to show that something has NOT happened yet (``join(SHORT)`` while a thread must still be blocked)
and as the hang guard in ``support``.
"""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import unittest
from unittest import mock

from recursion_core.control.mcp import server
from recursion_core.control.mcp.framing import Frame, encode
from recursion_core.control.mcp.tools import Tool
from recursion_core.control.mcp.tests.support import HANG_GUARD_S, GateAPI, QueueWriter, Served, assert_json_equal
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.control.tests.fakes import FakeKillTree, make_api

SHORT = 0.2  # only ever used to show a blocked thread is still blocked
INIT = {"jsonrpc": "2.0", "id": "init", "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def call(request_id, name, arguments):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def ping(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "method": "ping"}


def cancel(request_id):
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id, "reason": "t"}}


START = ("recursion_run_start", {"tenant": "t1", "engine": "codex", "prompt": "p"})
STOP = ("recursion_run_stop", {"run_id": "run-1"})
STATUS = ("recursion_run_status", {"run_id": "run-1"})


class SessionCase(unittest.TestCase):
    def session(self, api, **kwargs) -> tuple[server.LegacySession, QueueWriter]:
        writer = QueueWriter()
        session = server.LegacySession(api, writer, **kwargs)
        self.feed(session, INIT)
        self.assertEqual("2025-06-18", writer.next()["result"]["protocolVersion"])
        return session, writer

    @staticmethod
    def feed(session: server.LegacySession, message) -> None:
        session.receive(Frame(message=message))

    def feed_in_thread(self, session, message) -> threading.Thread:
        thread = threading.Thread(target=self.feed, args=(session, message), daemon=True)
        thread.start()
        return thread


class ConcurrencyTest(SessionCase):
    def test_ping_is_answered_while_a_call_is_in_flight(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        self.feed(session, call(2, *START))
        method, _, release = api.next_entered()
        self.assertEqual("run.start", method)
        self.feed(session, ping(3))
        self.assertEqual({"jsonrpc": "2.0", "id": 3, "result": {}}, writer.next())
        release.set()
        answer = writer.next()
        self.assertEqual((2, False), (answer["id"], answer["result"]["isError"]))
        session.close()
        self.assertEqual([], writer.drain())

    def test_calls_in_flight_are_answered_by_their_own_ids(self) -> None:
        api = GateAPI(answer=lambda method, params: {"code": "ok", "run": {"run_id": params["run_id"]}})
        session, writer = self.session(api)
        for request_id, run_id in (("a", "run-a"), ("b", "run-b")):
            self.feed(session, call(request_id, "recursion_run_status", {"run_id": run_id}))
        first, second = api.next_entered(), api.next_entered()
        second[2].set()
        got = writer.next()
        self.assertEqual(("b", "run-b"), (got["id"], got["result"]["structuredContent"]["run"]["run_id"]))
        first[2].set()
        got = writer.next()
        self.assertEqual(("a", "run-a"), (got["id"], got["result"]["structuredContent"]["run"]["run_id"]))
        session.close()

    def test_an_id_still_in_flight_is_refused_and_free_again_once_answered(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        self.feed(session, call(5, *STATUS))
        _, _, release = api.next_entered()
        for duplicate in (ping(5), call(5, *STOP)):
            self.feed(session, duplicate)
            self.assertEqual({"jsonrpc": "2.0", "id": 5, "error": {
                "code": -32600, "message": "Invalid Request: this id belongs to a request still in flight"}},
                writer.next())
        api.assert_nothing_entered(self)  # the refused stop reached nothing
        release.set()
        self.assertEqual(5, writer.next()["id"])
        session.close()  # waits until the call has given its id back
        self.feed(session, ping(5))
        self.assertEqual({"jsonrpc": "2.0", "id": 5, "result": {}}, writer.next())
        self.assertEqual(["run.status"], [m for m, _ in api.calls])

    def test_a_string_id_and_a_number_id_of_one_spelling_are_two_requests(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        self.feed(session, call(1, *STATUS))
        _, _, release = api.next_entered()
        self.feed(session, ping("1"))
        self.assertEqual({"jsonrpc": "2.0", "id": "1", "result": {}}, writer.next())
        release.set()
        self.assertEqual(1, writer.next()["id"])
        session.close()


class CancellationTest(SessionCase):
    def test_a_cancelled_call_is_not_answered_and_sends_no_stop(self) -> None:
        api = GateAPI(answer=lambda method, params: {"code": "started", "run": {"run_id": "run-1"}})
        session, writer = self.session(api)
        self.feed(session, call(2, *START))
        _, _, release = api.next_entered()
        self.feed(session, cancel(2))
        release.set()
        session.close()
        self.assertEqual([], writer.drain())  # no answer for id 2
        self.assertEqual([("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})], api.calls)

    def test_cancelling_an_in_flight_stop_sends_nothing_more(self) -> None:
        api = GateAPI(answer=lambda method, params: {"code": "pending", "run_id": "run-1", "state": "pending"})
        session, writer = self.session(api)
        self.feed(session, call(7, *STOP))
        _, _, release = api.next_entered()
        self.feed(session, cancel(7))
        release.set()
        session.close()
        self.assertEqual([], writer.drain())
        self.assertEqual(["run.stop"], [m for m, _ in api.calls])

    def test_a_cancellation_names_exactly_one_request(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        self.feed(session, call(1, *STATUS))
        self.feed(session, call("x", *STATUS))
        entered = [api.next_entered(), api.next_entered()]
        # None of these is request 1 or "x": a string "1", a boolean, a different id, a missing id, a null.
        for request_id in ("1", True, 2, None, "X"):
            self.feed(session, cancel(request_id))
        self.feed(session, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}})
        self.feed(session, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": [1]})
        self.feed(session, cancel("x"))  # this one is
        for _, _, release in entered:
            release.set()
        session.close()
        self.assertEqual([1], [m["id"] for m in writer.drain()])

    def test_a_cancellation_after_the_answer_changes_nothing(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        self.feed(session, call(3, *STATUS))
        api.next_entered()[2].set()
        self.assertEqual(3, writer.next()["id"])
        session.close()
        self.feed(session, cancel(3))
        self.feed(session, ping(4))
        self.assertEqual([{"jsonrpc": "2.0", "id": 4, "result": {}}], [writer.next()])
        self.assertEqual(1, len(api.calls))


class IdentityTest(SessionCase):
    def test_a_fraction_never_names_an_integer_request(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        self.feed(session, call(1, *STATUS))
        _, _, release = api.next_entered()
        self.feed(session, cancel(1.0))  # not an id: ignored, request 1 keeps its answer
        self.feed(session, call(7.0, *STATUS))
        self.assertEqual({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "Invalid Request: id must be a string or an integer, never null"}}, writer.next())
        release.set()
        self.assertEqual(1, writer.next()["id"])
        session.close()
        self.assertEqual(1, len(api.calls))

    def test_a_schema_the_checker_cannot_apply_answers_an_internal_error_and_sends_nothing(self) -> None:
        broken = Tool("recursion_run_status", "run.status", {
            "name": "recursion_run_status", "inputSchema": {"type": "object", "properties": {
                "run_id": {"type": "string", "minLength": "x"}}}})
        api = GateAPI()
        session, writer = self.session(api, tools=[broken])
        self.feed(session, call(2, *STATUS))
        self.assertEqual({"jsonrpc": "2.0", "id": 2, "error": {
            "code": -32603, "message": "Internal error: the tool's input schema could not be applied",
            "data": {"tool": "recursion_run_status"}}}, writer.next())
        self.feed(session, ping(3))  # the reader goes on
        self.assertEqual({"jsonrpc": "2.0", "id": 3, "result": {}}, writer.next())
        api.assert_nothing_entered(self)
        session.close()


class OutputLostTest(SessionCase):
    def test_no_worker_is_started_for_a_call_after_output_failure(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)
        session.output_lost()
        with mock.patch.object(server.threading.Thread, "start") as start:
            self.feed(session, call(2, *STATUS))
        start.assert_not_called()
        self.assertEqual((0, []), (session.in_flight(), writer.drain()))
        api.assert_nothing_entered(self)

    def test_a_closed_output_is_seen_at_admission_too(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, output_open=lambda: False)
        with mock.patch.object(server.threading.Thread, "start") as start:
            self.feed(session, call(2, *STATUS))
        start.assert_not_called()
        self.assertEqual(0, session.in_flight())

    def test_a_call_admitted_before_output_failure_is_not_sent_after_it(self) -> None:
        api = GateAPI()
        open_ = [True]
        writer = QueueWriter()
        session = server.LegacySession(api, writer, output_open=lambda: open_[0])
        self.feed(session, INIT)
        writer.next()
        workers: list[threading.Thread] = []
        with mock.patch.object(server.threading.Thread, "start", autospec=True, side_effect=workers.append):
            self.feed(session, call(2, *STATUS))
        self.assertEqual(1, len(workers))
        open_[0] = False  # stdout fails after the call was admitted and before its worker sends it
        workers[0].run()  # the worker's body, on this thread
        api.assert_nothing_entered(self)
        self.assertEqual((0, []), (session.in_flight(), writer.drain()))


class CancellationWindowTest(SessionCase):
    """Whether a cancelled call's answer goes out is decided under the session's lock: never both."""

    def test_a_cancellation_while_the_answer_is_being_written_is_too_late_and_says_so(self) -> None:
        writing, release = threading.Event(), threading.Event()

        class SlowWriter(QueueWriter):
            def __call__(self, message):
                if message.get("id") == 2:
                    writing.set()
                    release.wait(HANG_GUARD_S)
                return super().__call__(message)

        events: list[dict] = []
        api = GateAPI()
        writer = SlowWriter()
        session = server.LegacySession(api, writer, log=events.append)
        self.feed(session, INIT)
        writer.next()
        self.feed(session, call(2, *STATUS))
        api.next_entered()[2].set()
        self.assertTrue(writing.wait(HANG_GUARD_S))
        self.feed(session, cancel(2))
        release.set()
        self.assertEqual(2, writer.next()["id"])  # it was committed before the cancellation came
        session.close()
        self.assertEqual({"event": "cancelled", "outcome": "answer_already_written"}, events[-1])

    def test_a_cancellation_while_the_call_runs_drops_the_answer_and_says_so(self) -> None:
        events: list[dict] = []
        api = GateAPI()
        session, writer = self.session(api, log=events.append)
        self.feed(session, call(2, *STATUS))
        _, _, release = api.next_entered()
        self.feed(session, cancel(2))
        release.set()
        session.close()
        self.assertEqual([], writer.drain())
        self.assertEqual({"event": "cancelled", "outcome": "answer_dropped"}, events[-1])

    def test_a_stop_cancelled_after_admission_and_before_its_send_is_never_sent(self) -> None:
        # The call holds its slot and its worker exists but has not run: the cancellation comes in between.
        # The worker then decides under the session's lock and does not send (round 4 of #395, DeepSeek F1).
        for tool in (STOP, STATUS):
            with self.subTest(tool=tool[0]):
                events: list[dict] = []
                api = GateAPI()
                session, writer = self.session(api, log=events.append)
                held: list[threading.Thread] = []
                real_start = threading.Thread.start
                with mock.patch.object(server.threading.Thread, "start", lambda thread, held=held: held.append(thread)):
                    self.feed(session, call(2, *tool))
                self.assertEqual(1, len(held))
                with session._cond:
                    self.assertEqual(server.ADMITTED, session._in_flight[2].state)
                self.feed(session, cancel(2))
                self.assertEqual({"event": "cancelled", "outcome": "never_sent"}, events[-1])
                real_start(held[0])
                held[0].join(HANG_GUARD_S)
                self.assertFalse(held[0].is_alive())
                session.close()
                api.assert_nothing_entered(self)
                self.assertEqual(([], [], 0), (api.calls, writer.drain(), session.in_flight()))

    def test_a_cancellation_cannot_come_between_the_send_decision_and_the_sent_state(self) -> None:
        # The worker's decision is held inside the session's lock (output_open() is called there); a
        # cancellation meanwhile waits for the lock, then finds the call SENT and drops only its answer.
        deciding, release, finished = threading.Event(), threading.Event(), threading.Event()

        def output_open():
            if threading.current_thread().name == "mcp-tools-call":
                deciding.set()
                release.wait(HANG_GUARD_S)
            return True

        api = GateAPI()
        writer = QueueWriter()
        events: list[dict] = []
        session = server.LegacySession(api, writer, log=events.append, output_open=output_open)
        self.feed(session, INIT)
        writer.next()
        self.feed(session, call(2, *STOP))
        self.assertTrue(deciding.wait(HANG_GUARD_S))
        cancelling = threading.Thread(target=lambda: (self.feed(session, cancel(2)), finished.set()), daemon=True)
        cancelling.start()
        self.assertFalse(finished.wait(SHORT), "the cancellation got in while the send was being decided")
        release.set()
        sent = api.next_entered()
        self.assertTrue(finished.wait(HANG_GUARD_S))
        sent[2].set()
        cancelling.join(HANG_GUARD_S)
        session.close()
        self.assertEqual({"event": "cancelled", "outcome": "answer_dropped"}, events[-1])
        self.assertEqual(([("run.stop", {"run_id": "run-1"})], []), (api.calls, writer.drain()))

    def test_a_promoted_waiting_stop_is_never_sent_when_cancelled_before_its_worker_sends(self) -> None:
        api = GateAPI()
        events: list[dict] = []
        session, writer = self.session(api, log=events.append, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *STATUS))
        first = api.next_entered()
        self.feed(session, call(3, *STOP))   # the reserved slot
        second = api.next_entered()
        self.feed(session, call(4, *STOP))   # waits in the stop queue
        held: list = []
        promoted = threading.Event()
        real_start = session._start

        def start(each):
            if each.request_id == 4:
                held.append(each)
                promoted.set()
            else:
                real_start(each)

        with mock.patch.object(session, "_start", start):
            second[2].set()  # the reserved slot frees: call 4 is promoted, its worker held
            self.assertTrue(promoted.wait(HANG_GUARD_S))
        with session._cond:
            self.assertEqual(server.ADMITTED, held[0].state)
        self.feed(session, cancel(4))
        self.assertEqual({"event": "cancelled", "outcome": "never_sent"}, events[-1])
        session._run(held.pop())
        first[2].set()
        session.close()
        self.assertEqual(["run.status", "run.stop"], [m for m, _ in api.calls])
        self.assertEqual({2, 3}, {m["id"] for m in writer.drain()})

    def test_a_late_cancellation_for_a_reused_id_cancels_the_newer_call(self) -> None:
        # MCP forbids reusing an id in a session; a client that does it anyway gets what the docstring says.
        api = GateAPI()
        events: list[dict] = []
        session, writer = self.session(api, log=events.append)
        self.feed(session, call(2, *STATUS))
        api.next_entered()[2].set()
        self.assertEqual(2, writer.next()["id"])
        session.close()
        self.feed(session, call(2, *STOP))  # the same id, a new call
        stop = api.next_entered()
        self.feed(session, cancel(2))       # meant for the first call, applied to the second
        stop[2].set()
        session.close()
        self.assertEqual({"event": "cancelled", "outcome": "answer_dropped"}, events[-1])
        self.assertEqual((["run.status", "run.stop"], []), ([m for m, _ in api.calls], writer.drain()))

    def test_a_cancelled_waiting_call_is_never_sent(self) -> None:
        for waiting in (STATUS, STOP):
            with self.subTest(waiting=waiting[0]):
                self.check_a_cancelled_waiting_call_is_never_sent(waiting)

    def check_a_cancelled_waiting_call_is_never_sent(self, waiting) -> None:
        # Every worker the session starts is recorded and joined, so a cancelled call that was left queued
        # and started later (after close() returned) is seen; nothing here depends on scheduling.
        first_entered, release_first = threading.Event(), threading.Event()
        calls: list[str] = []

        class FirstHeld:
            def call(self, method, params=None):
                calls.append(method)
                if len(calls) == 1:
                    first_entered.set()
                    release_first.wait(HANG_GUARD_S)
                return {"code": "ok"}

        workers: list[threading.Thread] = []
        real_start = threading.Thread.start

        def recording_start(thread):
            workers.append(thread)
            return real_start(thread)

        events: list[dict] = []
        session, writer = self.session(FirstHeld(), max_in_flight=1, reserved_stop=0, log=events.append)
        with mock.patch.object(server.threading.Thread, "start", recording_start):
            self.feed(session, call(2, *START))
            self.assertTrue(first_entered.wait(HANG_GUARD_S))
            self.feed(session, call(3, *waiting))
            self.feed(session, cancel(3))
            self.assertEqual({"event": "cancelled", "outcome": "never_sent"}, events[-1])
            release_first.set()
            self.assertEqual(2, writer.next()["id"])
            session.close()
            joined = 0
            while joined < len(workers):  # a worker that finishes may start another: join until none is left
                workers[joined].join(HANG_GUARD_S)
                self.assertFalse(workers[joined].is_alive())
                joined += 1
        self.assertEqual(1, len(workers))
        self.assertEqual([], writer.drain())
        self.assertEqual(["run.start"], calls)
        self.feed(session, cancel(99))
        self.assertEqual({"event": "cancelled", "outcome": "not_in_flight"}, events[-1])

    def test_a_waiting_call_is_started_only_once_its_arguments_are_attached(self) -> None:
        # The reader admits a call (a place in the queue) and then attaches its arguments. A slot that
        # frees in between must not start it: the call is not ready yet. Once attached, it is started with
        # its own arguments.
        first_entered, release_first, first_finished = threading.Event(), threading.Event(), threading.Event()
        admitted, attach = threading.Event(), threading.Event()
        calls: list[tuple[str, dict]] = []

        class FirstHeld:
            def call(self, method, params=None):
                calls.append((method, params))
                if len(calls) == 1:
                    first_entered.set()
                    release_first.wait(HANG_GUARD_S)
                return {"code": "ok"}

        session, writer = self.session(FirstHeld(), max_in_flight=1, reserved_stop=0)
        real_finish, real_admit = session._finish, session._admit

        def finish(each):
            real_finish(each)
            if each.request_id == 2:
                first_finished.set()

        def admit(request_id, tool):
            made = real_admit(request_id, tool)
            admitted.set()
            attach.wait(HANG_GUARD_S)
            return made

        with mock.patch.object(session, "_finish", finish):
            self.feed(session, call(2, *STATUS))
            self.assertTrue(first_entered.wait(HANG_GUARD_S))
            with mock.patch.object(session, "_admit", admit):
                reader = self.feed_in_thread(session, call(3, "recursion_run_status", {"run_id": "second"}))
                try:
                    self.assertTrue(admitted.wait(HANG_GUARD_S))
                    release_first.set()
                    self.assertTrue(first_finished.wait(HANG_GUARD_S))
                    with session._cond:
                        before = (session._in_flight[3].state, len(calls))
                finally:
                    attach.set()
                    reader.join(HANG_GUARD_S)
            session.close()
        self.assertEqual((server.WAITING, 1), before, "a slot was given before the arguments were attached")
        self.assertEqual([("run.status", {"run_id": "run-1"}), ("run.status", {"run_id": "second"})], calls)
        self.assertEqual({2, 3}, {writer.next()["id"], writer.next()["id"]})


class SlotTest(SessionCase):
    def test_stop_runs_on_a_reserved_slot_while_every_ordinary_slot_is_busy(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *START))
        start = api.next_entered()
        self.feed(session, call(3, *STOP))  # must not wait for the start
        stop = api.next_entered()
        self.assertEqual(("run.start", "run.stop"), (start[0], stop[0]))
        stop[2].set()
        self.assertEqual(3, writer.next()["id"])
        start[2].set()
        self.assertEqual(2, writer.next()["id"])
        session.close()

    def test_a_call_over_the_limit_waits_in_the_queue_and_the_reader_goes_on(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *START))
        first = api.next_entered()
        self.feed(session, call(3, *STATUS))  # returns at once: the call waits, the reader does not
        self.feed(session, ping(4))
        self.assertEqual({"jsonrpc": "2.0", "id": 4, "result": {}}, writer.next())
        api.assert_nothing_entered(self)
        first[2].set()
        self.assertEqual(2, writer.next()["id"])
        second = api.next_entered()
        self.assertEqual("run.status", second[0])
        second[2].set()
        self.assertEqual(3, writer.next()["id"])
        session.close()

    def test_a_stop_waits_when_the_reserved_slots_are_busy_too_and_goes_before_waiting_calls(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *START))
        start = api.next_entered()
        self.feed(session, call(3, *STOP))
        stop = api.next_entered()
        self.feed(session, call(4, *STATUS))                                       # waits (ordinary)
        self.feed(session, call(5, "recursion_run_stop", {"run_id": "run-2"}))    # waits (stop), sent first
        api.assert_nothing_entered(self)
        start[2].set()  # an ordinary slot frees: the waiting stop takes it before the earlier status
        self.assertEqual(2, writer.next()["id"])
        second_stop = api.next_entered()
        self.assertEqual(("run.stop", {"run_id": "run-2"}), second_stop[:2])
        api.assert_nothing_entered(self)
        stop[2].set()  # the reserved slot frees: a status cannot use it
        self.assertEqual(3, writer.next()["id"])
        api.assert_nothing_entered(self)
        second_stop[2].set()
        self.assertEqual(5, writer.next()["id"])
        status = api.next_entered()
        self.assertEqual("run.status", status[0])
        status[2].set()
        self.assertEqual(4, writer.next()["id"])
        session.close()

    def test_with_the_default_limits_a_stop_is_sent_behind_eight_busy_calls_and_a_waiting_one(self) -> None:
        api = GateAPI()
        session, writer = self.session(api)  # MAX_IN_FLIGHT 8, RESERVED_STOP 2
        for n in range(8):
            self.feed(session, call(f"s{n}", *START))
        busy = [api.next_entered() for _ in range(8)]
        self.feed(session, call("ninth", *START))   # an ordinary call over the limit, before the stop
        self.feed(session, call("stop", *STOP))
        stop = api.next_entered()
        self.assertEqual("run.stop", stop[0])  # sent while all eight starts are still held
        stop[2].set()
        self.assertEqual("stop", writer.next()["id"])
        for each in busy:
            each[2].set()
        ninth = api.next_entered()
        self.assertEqual("run.start", ninth[0])
        ninth[2].set()
        self.assertEqual(9, len([writer.next() for _ in range(9)]))
        session.close()
        self.assertEqual((server.MAX_IN_FLIGHT, server.RESERVED_STOP), (8, 2))

    def test_a_full_queue_refuses_at_once_and_sends_nothing(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1, max_waiting=1, max_waiting_stops=0)
        self.feed(session, call(2, *START))
        start = api.next_entered()
        self.feed(session, call(3, *STATUS))  # waits
        busy = {"code": -32000, "message": "Server busy: this call was not sent; it may be sent again",
                "data": {"delivered": False}}
        self.feed(session, call(4, *STATUS))
        self.assertEqual({"jsonrpc": "2.0", "id": 4, "error": busy}, writer.next())
        self.feed(session, call(5, *STOP))    # takes the reserved slot
        stop = api.next_entered()
        self.feed(session, call(6, "recursion_run_stop", {"run_id": "run-2"}))  # no stop place left
        self.assertEqual({"jsonrpc": "2.0", "id": 6, "error": busy}, writer.next())
        for gate in (start, stop):
            gate[2].set()
        self.assertEqual({2, 5}, {writer.next()["id"], writer.next()["id"]})
        api.next_entered()[2].set()
        self.assertEqual(3, writer.next()["id"])
        session.close()
        self.assertEqual(["run.start", "run.stop", "run.status"], [m for m, _ in api.calls])

    def test_a_slot_is_held_until_the_answer_is_written(self) -> None:
        written, release = threading.Event(), threading.Event()

        class SlowWriter(QueueWriter):
            def __call__(self, message):
                if message.get("id") == 2:
                    written.set()
                    release.wait(HANG_GUARD_S)  # stdout is full: the answer waits
                return super().__call__(message)

        api = GateAPI()
        writer = SlowWriter()
        session = server.LegacySession(api, writer, max_in_flight=1, reserved_stop=0)
        self.feed(session, INIT)
        writer.next()
        self.feed(session, call(2, *STATUS))
        api.next_entered()[2].set()
        self.assertTrue(written.wait(HANG_GUARD_S))
        self.feed(session, call(3, *STATUS))
        api.assert_nothing_entered(self)  # the first call still holds its slot while its answer waits
        release.set()
        self.assertEqual(2, writer.next()["id"])
        api.next_entered()[2].set()
        self.assertEqual(3, writer.next()["id"])
        session.close()

    def test_slow_stdout_bounds_api_calls_workers_and_waiting_calls_together(self) -> None:
        stalled, release = threading.Event(), threading.Event()

        class SlowWriter(QueueWriter):
            def __call__(self, message):
                if message.get("id") == 0:
                    stalled.set()
                    release.wait(HANG_GUARD_S)  # the client has stopped reading
                return super().__call__(message)

        class Counting:
            def __init__(self) -> None:
                self.calls = 0

            def call(self, method, params=None):
                self.calls += 1
                return {"code": "ok"}

        api, writer = Counting(), SlowWriter()
        session = server.LegacySession(api, writer, max_in_flight=1, reserved_stop=0, max_waiting=3, max_waiting_stops=0)
        self.feed(session, INIT)
        writer.next()
        for n in range(10):
            self.feed(session, call(n, *STATUS))
        self.assertTrue(stalled.wait(HANG_GUARD_S))
        # The bound is max_in_flight + max_waiting: one call answering, three waiting unsent, six refused unsent.
        self.assertEqual((1, 4), (api.calls, session.in_flight()))
        refused = [writer.next() for _ in range(6)]
        self.assertEqual(list(range(4, 10)), [m["id"] for m in refused])
        self.assertEqual({-32000}, {m["error"]["code"] for m in refused})
        release.set()
        self.assertEqual(list(range(4)), sorted(writer.next()["id"] for _ in range(4)))
        session.close()
        self.assertEqual(4, api.calls)

    def test_limits_must_be_sane(self) -> None:
        for kwargs in ({"max_in_flight": 0}, {"reserved_stop": -1}, {"max_waiting": -1}, {"max_waiting_stops": -1}):
            with self.assertRaises(ValueError):
                server.LegacySession(GateAPI(), QueueWriter(), **kwargs)


class WorkerStartFailureTest(SessionCase):
    """A worker thread that cannot be started: nothing is sent, the slot and the id are given back."""

    def test_an_ordinary_call(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=0)
        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no thread")):
            self.feed(session, call(2, *STATUS))
        self.assertEqual({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "Internal error: no worker for this call"}},
                         writer.next())
        api.assert_nothing_entered(self)
        self.assertEqual(0, session.in_flight())
        self.feed(session, call(2, *STATUS))  # the same id again, on the slot given back
        api.next_entered()[2].set()
        self.assertEqual(2, writer.next()["id"])
        session.close()

    def test_a_stop_on_a_reserved_slot(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *START))
        start = api.next_entered()
        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no thread")):
            self.feed(session, call("stop", *STOP))
        self.assertEqual(-32603, writer.next()["error"]["code"])
        self.feed(session, call("stop", *STOP))  # the reserved slot and the id were given back
        stop = api.next_entered()
        self.assertEqual("run.stop", stop[0])
        stop[2].set()
        self.assertEqual("stop", writer.next()["id"])
        start[2].set()
        self.assertEqual(2, writer.next()["id"])
        session.close()
        self.assertEqual(["run.start", "run.stop"], [m for m, _ in api.calls])

    def test_a_failed_stop_start_gives_back_its_id_and_slot_before_an_exit_from_its_answer_goes_on(self) -> None:
        # Round 7 of #395 (Sol's REQUIRED 1, Astra's R5): an in-process session writes the reader's own answers,
        # and an exit or an interrupt from such a write goes on to the caller. When it comes from the "no worker"
        # answer, the unsent call must be finished first, or its id and its slot stay taken and close waits for ever.
        for exc in (SystemExit, KeyboardInterrupt, GeneratorExit):
            for how in ("ordinary slot", "reserved slot", "promoted from waiting"):
                with self.subTest(exception=exc.__name__, how=how):
                    api = GateAPI()
                    session, _ = self.session(api, max_in_flight=1, reserved_stop=int(how == "reserved slot"))
                    held: list = []
                    if how != "ordinary slot":  # a status call takes the one ordinary slot; its worker is held back
                        with mock.patch.object(session, "_start", held.append):
                            self.feed(session, call(2, *STATUS))

                    def failing(message, exc=exc):
                        raise exc("the failure answer's write")

                    session._write = failing
                    if how == "promoted from waiting":
                        self.feed(session, call("stop", *STOP))  # no slot: it waits, and nothing is written
                    try:
                        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no thread")):
                            with self.assertRaises(exc):
                                if how == "promoted from waiting":
                                    session._finish(held.pop())  # the slot goes to the waiting stop
                                else:
                                    self.feed(session, call("stop", *STOP))
                        for each in held:
                            session._finish(each)
                        api.assert_nothing_entered(self)
                        self.assertEqual((0, 0, 0), (session.in_flight(), session._general, session._reserved))
                    finally:  # a call left in flight keeps close waiting: give it back, so a failure is not a hang
                        for each in list(session._in_flight.values()):
                            session._finish(each)
                        session.close()


class LogTest(SessionCase):
    def test_initialize_and_ignored_responses_are_logged(self) -> None:
        events: list[dict] = []
        session, writer = self.session(GateAPI(), log=events.append)
        self.feed(session, {"jsonrpc": "2.0", "id": 9, "result": {}})
        self.assertEqual([{"event": "initialize", "requested": "2025-06-18", "answered": "2025-06-18", "client": "t"},
                          {"event": "ignored_response"}], events)
        self.assertEqual([], writer.drain())


class AnswerTest(SessionCase):
    def test_a_call_that_raises_answers_delivery_unknown_never_stopped(self) -> None:
        class Raising:
            def call(self, method, params=None):
                raise RuntimeError("boom")

        session, writer = self.session(Raising())
        self.feed(session, call(2, *STOP))
        answer = writer.next()
        session.close()
        want = {"code": "control_api_unavailable", "delivered": "unknown", "detail": "mcp:RuntimeError"}
        assert_json_equal(self, {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": json.dumps(want, sort_keys=True)}],
            "structuredContent": want, "isError": True}}, answer)

    def test_an_answer_json_cannot_hold_becomes_an_internal_error(self) -> None:
        class NotJson:
            def call(self, method, params=None):
                return {"code": "ok", "value": float("nan")}

        session, writer = self.session(NotJson())
        self.feed(session, call(2, *STATUS))
        self.assertEqual({"jsonrpc": "2.0", "id": 2, "error": {
            "code": -32603, "message": "Internal error: the answer is not JSON"}}, writer.next())
        session.close()

    def test_an_api_that_raises_a_base_exception_answers_delivery_unknown(self) -> None:
        class Exiting:
            def call(self, method, params=None):
                raise SystemExit("bye")

        hooks: list = []
        session, writer = self.session(Exiting())
        with mock.patch.object(threading, "excepthook", hooks.append):
            self.feed(session, call(2, *STOP))
            answer = writer.next()
            session.close()
        self.assertEqual([], hooks)  # nothing escaped the worker
        self.assertEqual({"code": "control_api_unavailable", "delivered": "unknown", "detail": "mcp:SystemExit"},
                         answer["result"]["structuredContent"])

    def test_a_write_that_raises_is_a_failed_output(self) -> None:
        for raised in (OSError("stdout is gone"), SystemExit("bye")):
            with self.subTest(raised=type(raised).__name__):
                self.check_a_write_that_raises_is_a_failed_output(raised)

    def check_a_write_that_raises_is_a_failed_output(self, raised: BaseException) -> None:
        class RaisesOnTwo(QueueWriter):
            def __call__(self, message):
                if message.get("id") == 2:
                    raise raised
                return super().__call__(message)

        hooks: list = []
        events: list[dict] = []
        api = GateAPI()
        writer = RaisesOnTwo()
        session = server.LegacySession(api, writer, log=events.append)
        self.feed(session, INIT)
        writer.next()
        with mock.patch.object(threading, "excepthook", hooks.append):
            self.feed(session, call(2, *STATUS))
            api.next_entered()[2].set()
            session.close()
        self.assertEqual([], hooks)  # no traceback on the process's stderr
        self.assertIn({"event": "write_failed", "detail": type(raised).__name__}, events)
        self.assertEqual({"event": "output_lost", "dropped_unsent": 0}, events[-1])
        self.feed(session, call(3, *STATUS))  # stdout failed: nothing more is sent
        api.assert_nothing_entered(self)
        self.assertEqual(0, session.in_flight())

    def test_a_failed_fallback_write_also_discards_a_waiting_stop(self) -> None:
        # The answer is not JSON, so the fallback -32603 is written; that write raises too: the output has
        # failed, and the stop waiting behind the call is never sent.
        calls: list[str] = []
        events: list[dict] = []
        written = QueueWriter()

        class NotJson:
            def call(self, method, params=None):
                calls.append(method)
                return {"code": "ok", "value": float("nan")}

        def write(message):
            if message.get("error", {}).get("code") == -32603:
                raise OSError("stdout is gone")
            return written(message)

        session = server.LegacySession(NotJson(), write, log=events.append, max_in_flight=1, reserved_stop=0)
        self.feed(session, INIT)
        written.next()
        held: list = []
        with mock.patch.object(session, "_start", held.append):
            self.feed(session, call(2, *STATUS))
            self.feed(session, call(3, *STOP))  # waits for the one slot
        session._run(held.pop())
        session.close()
        self.assertEqual((["run.status"], []), (calls, written.drain()))
        self.assertEqual([{"event": "write_failed", "detail": "OSError"}, {"event": "output_lost", "dropped_unsent": 1}],
                         events[-2:])

    def test_a_reader_answer_whose_write_raises_is_a_failed_output(self) -> None:
        # Round 7 of #395 (DeepSeek's F4): a session given no reply writes the reader's own answers; a write
        # that raises there is a failed output too, and an exit then goes on to the caller of receive.
        for raised in (TypeError("a text stream"), SystemExit("bye")):
            with self.subTest(raised=type(raised).__name__):
                def write(message, raised=raised):
                    raise raised

                api = GateAPI()
                events: list[dict] = []
                session = server.LegacySession(api, write, log=events.append)
                if isinstance(raised, Exception):
                    self.feed(session, INIT)
                else:
                    with self.assertRaises(SystemExit):
                        self.feed(session, INIT)
                self.assertFalse(session.initialized)  # the answer was never written
                self.assertEqual([{"event": "write_failed", "detail": type(raised).__name__},
                                  {"event": "output_lost", "dropped_unsent": 0}], events)
                api.assert_nothing_entered(self)


class EndOfInputTest(unittest.TestCase):
    def test_end_of_input_waits_for_calls_in_flight_and_writes_their_answers(self) -> None:
        api = GateAPI()
        served = Served(api)
        served.send(INIT)
        served.out.next()
        served.send(call(2, *START))
        _, _, release = api.next_entered()
        served.end_input()
        self.assertFalse(served.join(SHORT), "serve returned while a call was still in flight")
        release.set()
        self.assertTrue(served.join())
        self.assertEqual(0, served.status)
        self.assertEqual([2], [m["id"] for m in served.out.drain()])
        self.assertEqual("end_of_input", served.events[-1]["event"])

    def test_end_of_input_never_stops_a_run(self) -> None:
        api, _, kill, starter = make_api(FakeKillTree([POPULATED, EMPTY]))
        served = Served(api)
        served.send(INIT)
        served.out.next()
        served.send(call(2, *START))
        self.assertEqual("started", served.out.next()["result"]["structuredContent"]["code"])
        self.assertEqual([], served.finish(self))
        self.assertEqual([], kill.requests)  # detach, not stop
        self.assertEqual(1, len(starter.calls))
        self.assertEqual("running", api.call("run.status", {"run_id": "run-1"})["run"]["state"])

    def test_waiting_calls_are_still_run_and_answered_after_end_of_input(self) -> None:
        api = GateAPI()
        served = Served(api, max_in_flight=1, reserved_stop=0)
        served.send(INIT)
        served.out.next()
        served.send(call(2, *START))
        first = api.next_entered()
        served.send(call(3, *STATUS))
        served.end_input()
        first[2].set()
        self.assertEqual(2, served.out.next()["id"])
        api.next_entered()[2].set()
        self.assertEqual(3, served.out.next()["id"])
        self.assertTrue(served.join())
        self.assertEqual(0, served.status)

    def test_after_stdout_fails_nothing_more_is_sent_and_serve_ends_without_more_input(self) -> None:
        class FailsOnTwo(io.RawIOBase):
            def __init__(self) -> None:
                super().__init__()
                self.lines: queue.Queue = queue.Queue()

            def writable(self):
                return True

            def write(self, data):
                message = json.loads(data)
                if message.get("id") == 2:
                    raise BrokenPipeError("the client stopped reading")
                self.lines.put(message)
                return len(data)

        api = GateAPI()
        events: list[dict] = []
        read_fd, write_fd = os.pipe()
        instream = os.fdopen(read_fd, "rb")
        self.addCleanup(instream.close)
        out = FailsOnTwo()
        status: list[int] = []
        serving = threading.Thread(target=lambda: status.append(server.serve(
            api, instream, out, log=events.append, max_in_flight=1, reserved_stop=0)), daemon=True)
        serving.start()
        for message in (INIT, call(2, *STATUS), call(3, *START), ping(4)):
            os.write(write_fd, encode(message))
        entered = api.next_entered()
        self.assertEqual("run.status", entered[0])
        self.assertEqual("init", out.lines.get(timeout=HANG_GUARD_S)["id"])
        self.assertEqual(4, out.lines.get(timeout=HANG_GUARD_S)["id"])  # so call 3 has been read: it waits
        entered[2].set()  # its answer is the write that fails; call 3 is waiting and must never be sent
        serving.join(HANG_GUARD_S)
        self.assertFalse(serving.is_alive(), "serve should end when stdout fails, with stdin still open")
        self.assertEqual([0], status)
        os.write(write_fd, encode(call(4, *START)))
        os.close(write_fd)
        with self.assertRaises(queue.Empty):
            api.entered.get(timeout=SHORT)  # time only shows that nothing was sent after the failure
        self.assertEqual(["run.status"], [m for m, _ in api.calls])
        self.assertIn({"event": "output_lost", "dropped_unsent": 1}, events)
        self.assertEqual({"event": "output_failed", "stdout_broken": True}, events[-1])

    def test_once_stdout_breaks_no_further_call_is_forwarded(self) -> None:
        class Broken(io.RawIOBase):
            def write(self, data):
                raise BrokenPipeError("client went away")

        api = GateAPI()
        read_fd, write_fd = os.pipe()
        instream = os.fdopen(read_fd, "rb")
        self.addCleanup(instream.close)
        status: list[int] = []
        serving = threading.Thread(target=lambda: status.append(server.serve(api, instream, Broken())), daemon=True)
        serving.start()
        os.write(write_fd, encode(INIT))  # its answer, written by the reply queue, is the write that fails
        serving.join(HANG_GUARD_S)
        self.assertFalse(serving.is_alive(), "serve should end when stdout fails, with stdin still open")
        self.assertEqual([0], status)
        os.write(write_fd, encode(call(2, *START)) + encode(call(3, *START)))  # read, and refused at admission
        os.close(write_fd)
        with self.assertRaises(queue.Empty):
            api.entered.get(timeout=SHORT)  # time only shows that nothing was sent after the failure
        self.assertEqual([], api.calls)

    def test_output_is_one_ascii_line_per_message(self) -> None:
        odd = "caf\u00e9\nline\u2028two\x00"
        out = io.BytesIO()
        stdin = io.BytesIO(encode(ping(odd)) + encode(ping(2)))
        self.assertEqual(0, server.serve(GateAPI(), stdin, out))
        raw = out.getvalue()
        self.assertTrue(raw.isascii())
        lines = raw.split(b"\n")
        self.assertEqual(b"", lines[-1])
        self.assertEqual([odd, 2], [json.loads(line)["id"] for line in lines[:-1]])

    def test_an_over_long_integer_is_answered_as_such_and_serving_goes_on(self) -> None:
        served = Served(GateAPI())
        served.send_bytes(b'{"jsonrpc":"2.0","id":' + b"7" * 4301 + b',"method":"ping"}\n')
        self.assertEqual({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "Invalid Request: an integer longer than 4300 digits"}}, served.out.next())
        served.send(ping(2))
        self.assertEqual({"jsonrpc": "2.0", "id": 2, "result": {}}, served.out.next())
        self.assertEqual([], served.finish(self))

    def test_a_line_over_the_limit_is_refused_and_serving_goes_on(self) -> None:
        served = Served(GateAPI())
        served.send_bytes(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"a" * (2 * 1024 * 1024) + b'"}}\n')
        self.assertEqual({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "Invalid Request: message too large"}}, served.out.next())
        served.send(ping(2))
        self.assertEqual({"jsonrpc": "2.0", "id": 2, "result": {}}, served.out.next())
        self.assertEqual([], served.finish(self))


if __name__ == "__main__":
    unittest.main()
