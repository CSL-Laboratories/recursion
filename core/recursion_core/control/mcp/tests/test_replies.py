"""The answers the reader owes go through ``replies.ReplyQueue`` (``server.py``, REPLIES): taken without
waiting, written in order by a thread of their own, bounded, and dropped, never waited for, when the client
has stopped reading stdout. Whatever stdout does, the reader goes on reading: a Stop behind a ``Server
busy`` answer is sent, and a cancellation is applied (round 2 of #395).

Every wait synchronises on an event (a call entering an API, a line on the wire, a write that stalls).
Time is only the hang guard: an ``offer`` that waited for stdout, or a reader that did, ends the test
there and fails it.
"""

from __future__ import annotations

import copy
import io
import threading
import unittest
from unittest import mock

from recursion_core.control.mcp import server
from recursion_core.control.mcp.framing import (NOT_JSON, NOT_UTF8, NUMBER_TOO_LONG, TOO_LARGE, Frame, FrameWriter,
                                                encode)
from recursion_core.control.mcp.replies import ReplyQueue
from recursion_core.control.mcp.tests.support import (HANG_GUARD_S, Capture, Events, GateAPI, QueueWriter, Served,
                                                      Stalling)
from recursion_core.control.mcp.tests.test_session import INIT, SHORT, START, STATUS, STOP, call, cancel, ping


def pong(request_id) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": {}}


SIZE = len(encode(pong(1)))  # the same for the ids 1 to 9


class ReplyQueueTest(unittest.TestCase):
    def queue(self, out, **kwargs) -> tuple[ReplyQueue, Events]:
        events = Events()  # the queue delivers its events from a diagnostics thread of its own
        replies = ReplyQueue(FrameWriter(out), log=events, **kwargs)
        replies.start()
        return replies, events

    def offers(self, replies: ReplyQueue, ids) -> list[bool]:
        """``offer`` each id's answer on a thread of its own; fails if any of them waits."""

        taken: list[bool] = []
        thread = threading.Thread(target=lambda: taken.extend(replies.offer(pong(n)) for n in ids), daemon=True)
        thread.start()
        thread.join(HANG_GUARD_S)
        self.assertFalse(thread.is_alive(), "taking an answer waited for stdout")
        return taken

    @staticmethod
    def idle(replies: ReplyQueue) -> bool:
        """Wait until the queue holds nothing (written or discarded); ``False`` at the hang guard."""

        with replies._cond:
            return replies._cond.wait_for(lambda: not replies._pending, HANG_GUARD_S)

    def test_answers_are_written_in_the_order_taken_and_close_waits_for_them(self) -> None:
        out = Capture()
        replies, events = self.queue(out)
        self.assertEqual([True] * 5, self.offers(replies, range(1, 6)))
        replies.close()
        self.assertEqual([1, 2, 3, 4, 5], [m["id"] for m in out.drain()])
        self.assertEqual([False], self.offers(replies, [6]))  # closed
        self.assertEqual([], events.all)

    def test_taking_an_answer_never_waits_for_a_stalled_stdout(self) -> None:
        out = Stalling({1})
        replies, _ = self.queue(out)
        self.assertEqual([True], self.offers(replies, [1]))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        self.assertEqual([True, True], self.offers(replies, [2, 3]))  # stdout holds answer 1
        self.assertEqual([], out.drain())
        out.release.set()
        replies.close()
        self.assertEqual([1, 2, 3], [m["id"] for m in out.drain()])

    def test_a_full_queue_drops_answers_and_logs_two_lines_per_stretch_of_drops(self) -> None:
        out = Stalling({1})
        replies, events = self.queue(out, max_bytes=2 * SIZE)
        self.assertEqual([True], self.offers(replies, [1]))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        self.assertEqual([True, False, False], self.offers(replies, [2, 3, 4]))  # answer 1 counts until written
        self.assertEqual([{"event": "replies_dropped", "pending_bytes": 2 * SIZE}], events.wait(1))
        out.release.set()
        self.assertEqual([1, 2], [out.next()["id"], out.next()["id"]])
        self.assertEqual([True], self.offers(replies, [5]))  # answer 1 is out: room for one more at least
        self.assertEqual({"event": "replies_resumed", "dropped": 2}, events.wait(2)[-1])
        self.assertEqual(5, out.next()["id"])
        self.assertEqual([True], self.offers(replies, [6]))  # the stretch is over: nothing more is logged
        replies.close()
        self.assertEqual([6], [m["id"] for m in out.drain()])
        self.assertEqual(2, len(events.all))  # close() has delivered every event

    def test_an_answer_over_the_bound_is_taken_into_an_empty_queue_only(self) -> None:
        out = Stalling({1})
        replies, events = self.queue(out, max_bytes=1)
        self.assertEqual([True], self.offers(replies, [1]))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        self.assertEqual([False], self.offers(replies, [2]))
        out.release.set()
        self.assertEqual(1, out.next()["id"])
        self.assertTrue(self.idle(replies))
        self.assertEqual([True], self.offers(replies, [3]))
        replies.close()
        self.assertEqual([3], [m["id"] for m in out.drain()])
        self.assertEqual([{"event": "replies_dropped", "pending_bytes": SIZE},
                          {"event": "replies_resumed", "dropped": 1}], events.all)

    def test_after_a_failed_write_what_is_queued_is_discarded_and_nothing_more_is_taken(self) -> None:
        out = Stalling({1}, fail=True)
        replies, events = self.queue(out)
        self.assertEqual([True], self.offers(replies, [1]))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        self.assertEqual([True], self.offers(replies, [2]))
        out.release.set()  # the write of answer 1 fails
        self.assertTrue(self.idle(replies))
        self.assertEqual([False], self.offers(replies, [3]))  # refused for the failure, before close()
        replies.close()    # returns: answer 2 was discarded
        self.assertEqual([], out.drain())
        self.assertEqual([], events.all)

    def test_a_writer_another_write_broke_discards_what_is_queued(self) -> None:
        # A call's answer, written by its worker through the same writer, found stdout failed: the queue's
        # next write returns False at once, and the queue discards and takes nothing more.
        out = Stalling({1})
        writer = FrameWriter(out)
        replies = ReplyQueue(writer)
        replies.start()
        self.assertEqual([True], self.offers(replies, [1]))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        self.assertEqual([True, True], self.offers(replies, [2, 3]))
        writer.broken = True  # as FrameWriter marks it when a write fails
        out.release.set()     # answer 1 was under way and goes out; 2 is refused by the writer
        self.assertTrue(self.idle(replies))
        self.assertEqual([False], self.offers(replies, [4]))  # refused for the failure, before close()
        replies.close()
        self.assertEqual([1], [m["id"] for m in out.drain()])

    def test_a_stream_that_raises_ends_writing_and_never_holds_the_shutdown(self) -> None:
        # Round 6 of #395: a reply thread that died on a raising stream left its answer pending for ever, so
        # the queue's shutdown, and serve with it, never returned.
        class RaisingPast(FrameWriter):
            def write(self, message):  # what no writer here does: raise past its own handling
                raise SystemExit("a writer that raises past its own handling")

        for writer in (FrameWriter(io.StringIO()), RaisingPast(Capture())):
            with self.subTest(writer=type(writer).__name__):
                hooks: list = []
                with mock.patch.object(threading, "excepthook", hooks.append):
                    replies = ReplyQueue(writer)
                    replies.start()
                    self.assertTrue(replies.offer(pong(1)))
                    self.assertTrue(self.idle(replies))  # discarded, not left pending
                    self.assertEqual([False], self.offers(replies, [2]))
                    shutting = threading.Thread(target=replies.close, daemon=True)
                    shutting.start()
                    shutting.join(HANG_GUARD_S)
                    self.assertFalse(shutting.is_alive(), "the shutdown waited for an answer that is never written")
                self.assertEqual([], hooks)  # no thread ended with a traceback

    def test_the_bound_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            ReplyQueue(FrameWriter(Capture()), max_bytes=0)


class TextStdoutTest(unittest.TestCase):
    def test_serve_over_a_text_stdout_returns_as_for_a_failed_stdout(self) -> None:
        # Round 6 of #395 (DeepSeek's F1): a consumer gives serve a text stream for stdout. The first answer
        # cannot be written, so stdout has failed; serve returns 0 instead of never returning.
        events = Events()
        status: list = []
        serving = threading.Thread(target=lambda: status.append(
            server.serve(GateAPI(), io.BytesIO(encode(INIT)), io.StringIO(), log=events)), daemon=True)
        serving.start()
        serving.join(HANG_GUARD_S)
        self.assertFalse(serving.is_alive(), "serve never returned")
        self.assertEqual([0], status)
        self.assertEqual({"event": "output_failed", "stdout_broken": True}, events.all[-1])


class SessionReplyTest(unittest.TestCase):
    def test_answers_the_reader_owes_go_to_reply_and_the_calls_answers_to_write(self) -> None:
        replies, answers = QueueWriter(), QueueWriter()
        api = GateAPI()
        session = server.LegacySession(api, answers, reply=replies, max_in_flight=1, reserved_stop=0,
                                       max_waiting=0, max_waiting_stops=0)
        for message in (INIT, ping(2), call(3, *STATUS), call(4, *STATUS),  # 4 finds no place: busy
                        {"jsonrpc": "2.0", "id": 5, "method": "no/such"}, call(6, "no_such_tool", {})):
            session.receive(Frame(message=message))
        session.receive(Frame(error=NOT_JSON))
        api.next_entered()[2].set()
        session.close()
        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no thread")):
            session.receive(Frame(message=call(7, *STATUS)))  # the worker cannot start: an internal error
        session.close()
        self.assertEqual(["init", 2, 4, 5, 6, None, 7], [m["id"] for m in replies.drain()])
        self.assertEqual([3], [m["id"] for m in answers.drain()])

    def test_every_kind_of_answer_the_reader_owes_goes_to_reply(self) -> None:
        def request(request_id, method, params=None) -> Frame:
            return Frame(message={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

        no_capabilities = copy.deepcopy(INIT)
        del no_capabilities["params"]["capabilities"]
        cases = [  # (name, initialized before, frame)
            ("too large", True, Frame(error=TOO_LARGE)),
            ("integer too long", True, Frame(error=NUMBER_TOO_LONG)),
            ("not UTF-8", True, Frame(error=NOT_UTF8)),
            ("invalid request", True, Frame(message={"id": True})),
            ("second initialize", True, Frame(message=INIT)),
            ("initialize refused", False, Frame(message=no_capabilities)),
            ("not initialized", False, request(2, "tools/list")),
            ("unknown cursor", True, request(3, "tools/list", {"cursor": "c"})),
            ("tools/list", True, request(4, "tools/list")),
            ("name not a string", True, request(5, "tools/call", {"name": 42})),
            ("arguments refused", True, Frame(message=call(6, STATUS[0], {}))),
            ("unknown method", True, request(7, "no/such")),
        ]
        for name, initialized, frame in cases:
            with self.subTest(case=name):
                replies, answers = QueueWriter(), QueueWriter()
                session = server.LegacySession(GateAPI(), answers, reply=replies)
                session.initialized = initialized
                session.receive(frame)
                self.assertEqual((1, []), (len(replies.drain()), answers.drain()))
        with self.subTest(case="schema check raises"):
            replies, answers = QueueWriter(), QueueWriter()
            session = server.LegacySession(GateAPI(), answers, reply=replies)
            session.initialized = True
            with mock.patch.object(server, "check_arguments", side_effect=ValueError("the checker broke")):
                session.receive(Frame(message=call(8, *STATUS)))
            self.assertEqual((1, []), (len(replies.drain()), answers.drain()))
        with self.subTest(case="id still in flight"):
            api, replies, answers = GateAPI(), QueueWriter(), QueueWriter()
            session = server.LegacySession(api, answers, reply=replies)
            session.initialized = True
            session.receive(Frame(message=call(9, *STATUS)))
            gate = api.next_entered()
            try:
                session.receive(Frame(message=ping(9)))
                self.assertEqual((1, []), (len(replies.drain()), answers.drain()))
            finally:
                gate[2].set()
                session.close()

    def test_a_dropped_initialize_answer_leaves_the_session_uninitialized(self) -> None:
        take = [False]
        seen: list[dict] = []
        events: list[dict] = []

        def reply(message):
            seen.append(message)
            return take[0]

        session = server.LegacySession(GateAPI(), QueueWriter(), reply=reply, log=events.append)
        session.receive(Frame(message=INIT))
        self.assertFalse(session.initialized)
        self.assertEqual([], events)
        take[0] = True
        session.receive(Frame(message={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        self.assertEqual("Invalid Request: not initialized; send initialize first", seen[-1]["error"]["message"])
        session.receive(Frame(message=INIT))
        self.assertTrue(session.initialized)
        self.assertEqual("2025-06-18", seen[-1]["result"]["protocolVersion"])
        self.assertEqual(["initialize"], [e["event"] for e in events])


class StalledStdoutTest(unittest.TestCase):
    """``serve`` with a stdout that stops taking writes: the reader goes on."""

    def test_a_stop_is_sent_behind_a_busy_answer_while_stdout_is_stalled(self) -> None:
        stop_sent = threading.Event()
        calls: list[str] = []

        class API:
            def call(self, method, params=None):
                calls.append(method)
                if method == "run.stop":
                    stop_sent.set()
                return {"code": "ok"}

        out = Stalling({2})
        served = Served(API(), out=out, max_in_flight=1, reserved_stop=1, max_waiting=1, max_waiting_stops=1)
        served.send(INIT)
        self.assertEqual("init", out.next()["id"])
        served.send(call(2, *STATUS))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))  # call 2's answer holds stdout and the one slot
        served.send(call(3, *STATUS))  # waits for the slot
        served.send(call(4, *STATUS))  # its queue is full: Server busy, an answer the reader owes
        served.send(call(5, *STOP))    # read behind it: the reserved slot
        self.assertTrue(stop_sent.wait(HANG_GUARD_S), "the stop was not sent while stdout was stalled")
        self.assertEqual(["run.status", "run.stop"], calls)
        self.assertEqual([], out.drain())  # nothing more was written meanwhile, the busy answer included
        out.release.set()
        rest = served.finish(self)
        by_id = {m["id"]: m for m in rest}
        self.assertEqual((4, {2, 3, 4, 5}), (len(rest), set(by_id)))
        self.assertEqual(server.SERVER_BUSY, by_id[4]["error"]["code"])
        self.assertEqual(["run.status", "run.stop", "run.status"], calls)

    def test_with_the_reply_queue_full_a_cancellation_is_applied_and_a_stop_is_sent(self) -> None:
        api = GateAPI()
        out = Stalling({"p1"})
        size = len(encode(pong("p1")))
        served = Served(api, out=out, max_in_flight=1, reserved_stop=1, max_pending_reply_bytes=2 * size)
        served.send(INIT)
        self.assertEqual("init", out.next()["id"])
        served.send(call(2, *START))
        start = api.next_entered()     # holds the one ordinary slot
        served.send(call(3, *STATUS))  # waits for it
        served.send(ping("p1"))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))  # stdout stalls on the answer to p1
        served.send(ping("p2"))        # taken: the queue holds p1 and p2
        served.send(ping("p3"))        # no room: dropped
        served.send(cancel(3))         # applied: call 3 is never sent
        served.send(call(4, *STOP))    # sent, on the reserved slot
        stop = api.next_entered()
        self.assertEqual("run.stop", stop[0])
        for gate in (stop, start):
            gate[2].set()
        out.release.set()
        seen = []
        while not {"p1", "p2"} <= set(seen):
            seen.append(out.next()["id"])
        served.send(ping("p4"))        # room again: taken
        seen += [m["id"] for m in served.finish(self)]
        self.assertEqual((5, {"p1", "p2", 2, 4, "p4"}), (len(seen), set(seen)))
        self.assertEqual(["run.start", "run.stop"], [m for m, _ in api.calls])
        self.assertIn({"event": "cancelled", "outcome": "never_sent"}, served.events)
        self.assertIn({"event": "replies_dropped", "pending_bytes": 2 * size}, served.events)
        self.assertIn({"event": "replies_resumed", "dropped": 1}, served.events)

    def test_end_of_input_waits_until_the_reader_answers_are_written(self) -> None:
        out = Stalling({"p"})
        served = Served(GateAPI(), out=out)
        served.send(ping("p"))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        served.end_input()
        self.assertFalse(served.join(SHORT), "serve returned before the answer to p was written")
        out.release.set()
        self.assertEqual(["p"], [m["id"] for m in served.finish(self)])

    def test_a_reader_answer_still_queued_when_stdout_fails_is_never_written(self) -> None:
        api = GateAPI()
        out = Stalling({"p1"}, fail=True)
        served = Served(api, out=out)
        served.send(INIT)
        self.assertEqual("init", out.next()["id"])
        served.send(ping("p1"))
        self.assertTrue(out.stalled.wait(HANG_GUARD_S))
        served.send(ping("p2"))
        served.send(call(3, *STATUS))
        api.next_entered()[2].set()  # its answer waits behind p1 for stdout, or finds it failed
        out.release.set()            # the write of p1 fails
        self.assertTrue(served.join(), "serve should end when stdout fails, with stdin still open")
        self.assertEqual(0, served.status)
        self.assertEqual({"event": "output_failed", "stdout_broken": True}, served.events[-1])
        self.assertEqual([], served.finish(self))  # neither p2 nor call 3's answer was written


if __name__ == "__main__":
    unittest.main()
