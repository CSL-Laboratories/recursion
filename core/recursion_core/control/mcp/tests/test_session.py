"""The session's behaviour that fixtures cannot show: concurrent calls, cancellation, end of input, the
reserved Stop slot, back-pressure, and answers that cannot be written.

Every wait synchronises on an event (a call entering the gated API, an answer on the wire). Time is used
only to show that something has NOT happened yet (``join(SHORT)`` while a thread must still be blocked)
and as the hang guard in ``support``.
"""

from __future__ import annotations

import io
import json
import threading
import unittest

from recursion_core.control.mcp import server
from recursion_core.control.mcp.framing import Frame, encode
from recursion_core.control.mcp.tests.support import GateAPI, QueueWriter, Served, assert_json_equal
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

    def test_a_call_over_the_limit_waits_for_a_slot_and_is_not_refused(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *START))
        first = api.next_entered()
        reader = self.feed_in_thread(session, call(3, *STATUS))  # a read gets no reserved slot
        reader.join(SHORT)
        self.assertTrue(reader.is_alive(), "the reader should wait for a slot")
        api.assert_nothing_entered(self)
        first[2].set()
        self.assertEqual(2, writer.next()["id"])
        second = api.next_entered()
        self.assertEqual("run.status", second[0])
        reader.join()
        second[2].set()
        self.assertEqual(3, writer.next()["id"])
        session.close()

    def test_a_stop_waits_when_the_reserved_slots_are_busy_too(self) -> None:
        api = GateAPI()
        session, writer = self.session(api, max_in_flight=1, reserved_stop=1)
        self.feed(session, call(2, *START))
        start = api.next_entered()
        self.feed(session, call(3, *STOP))
        stop = api.next_entered()
        reader = self.feed_in_thread(session, call(4, "recursion_run_stop", {"run_id": "run-2"}))
        reader.join(SHORT)
        self.assertTrue(reader.is_alive())
        stop[2].set()
        self.assertEqual(3, writer.next()["id"])
        reader.join()
        second_stop = api.next_entered()
        self.assertEqual(("run.stop", {"run_id": "run-2"}), second_stop[:2])
        second_stop[2].set()
        start[2].set()
        self.assertEqual({2, 4}, {writer.next()["id"], writer.next()["id"]})
        session.close()

    def test_limits_must_be_sane(self) -> None:
        for kwargs in ({"max_in_flight": 0}, {"reserved_stop": -1}):
            with self.assertRaises(ValueError):
                server.LegacySession(GateAPI(), QueueWriter(), **kwargs)


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

    def test_an_answer_that_is_not_an_object_is_an_outcome_not_known(self) -> None:
        for odd in ("stopped", ["stopped"], None, 3):
            class Odd:
                def call(self, method, params=None, odd=odd):
                    return odd

            with self.subTest(answer=odd):
                session, writer = self.session(Odd())
                self.feed(session, call(2, *STOP))
                result = writer.next()["result"]
                session.close()
                self.assertEqual(("control_api_unavailable", "unknown", True),
                                 (result["structuredContent"]["code"], result["structuredContent"]["delivered"],
                                  result["isError"]))

    def test_an_answer_json_cannot_hold_becomes_an_internal_error(self) -> None:
        class NotJson:
            def call(self, method, params=None):
                return {"code": "ok", "value": float("nan")}

        session, writer = self.session(NotJson())
        self.feed(session, call(2, *STATUS))
        self.assertEqual({"jsonrpc": "2.0", "id": 2, "error": {
            "code": -32603, "message": "Internal error: the answer is not JSON"}}, writer.next())
        session.close()


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

    def test_once_stdout_breaks_no_further_call_is_forwarded(self) -> None:
        class Broken(io.RawIOBase):
            def write(self, data):
                raise BrokenPipeError("client went away")

        api, _, _, starter = make_api()
        stdin = io.BytesIO(b"".join(encode(m) for m in (INIT, call(2, *START), call(3, *START))))
        self.assertEqual(0, server.serve(api, stdin, Broken()))
        self.assertEqual([], starter.calls)  # the initialize answer failed: nothing after it was forwarded

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
