"""Diagnostics go through ``diagnostics.DiagnosticQueue`` (``server.py``, DIAGNOSTICS): taken without
waiting, delivered in order by a thread of their own, bounded, dropped with a count when they do not fit.
Whatever stderr does, the reader goes on reading: with a ``log`` that has stopped taking events, a
cancellation is applied, a ping answered and a Stop sent (round 3 of #395). The reply queue's own drop
lines never make ``offer`` wait for ``log`` either.

Every wait synchronises on an event; time is only the hang guard.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from recursion_core.control.mcp.diagnostics import DiagnosticQueue
from recursion_core.control.mcp.framing import FrameWriter, encode
from recursion_core.control.mcp.replies import ReplyQueue
from recursion_core.control.mcp.tests.support import HANG_GUARD_S, Events, GateAPI, Served, Stalling
from recursion_core.control.mcp.tests.test_session import INIT, START, STATUS, STOP, call, cancel, ping


def event(n) -> dict:
    return {"event": "e", "n": n}


SIZE = len('{"event":"e","n":1}')  # the compact JSON the queue counts; the same for n from 1 to 9


class StalledLog:
    """A ``log`` that stops taking events at the ones named in ``stall_at``, until ``release`` is set."""

    def __init__(self, stall_at=(1,), *, raises: bool = False) -> None:
        self.stall_at = set(stall_at)
        self.raises = raises
        self.stalled = threading.Event()
        self.release = threading.Event()
        self.events = Events()

    def __call__(self, item: dict) -> None:
        if item.get("n") in self.stall_at:
            self.stalled.set()
            if not self.release.wait(HANG_GUARD_S):
                raise AssertionError("the log was never released")
            if self.raises:
                raise OSError("stderr went away")
        self.events(item)


class DiagnosticQueueTest(unittest.TestCase):
    def queue(self, log, **kwargs) -> DiagnosticQueue:
        made = DiagnosticQueue(log, **kwargs)
        made.start()
        return made

    def offers(self, queue: DiagnosticQueue, numbers) -> list[bool]:
        """``offer`` each event on a thread of its own; fails if any of them waits."""

        taken: list[bool] = []
        thread = threading.Thread(target=lambda: taken.extend(queue.offer(event(n)) for n in numbers), daemon=True)
        thread.start()
        thread.join(HANG_GUARD_S)
        self.assertFalse(thread.is_alive(), "taking an event waited for the log")
        return taken

    @staticmethod
    def idle(queue: DiagnosticQueue) -> bool:
        with queue._cond:
            return queue._cond.wait_for(lambda: not queue._pending, HANG_GUARD_S)

    def test_events_are_delivered_in_the_order_taken_and_close_waits_for_them(self) -> None:
        log = Events()
        queue = self.queue(log)
        self.assertEqual([True] * 5, self.offers(queue, range(1, 6)))
        queue.close()
        self.assertEqual([event(n) for n in range(1, 6)], log.all)
        self.assertEqual([False], self.offers(queue, [6]))  # closed

    def test_taking_an_event_never_waits_for_a_stalled_log(self) -> None:
        log = StalledLog()
        queue = self.queue(log)
        self.assertEqual([True], self.offers(queue, [1]))
        self.assertTrue(log.stalled.wait(HANG_GUARD_S))
        self.assertEqual([True, True], self.offers(queue, [2, 3]))
        self.assertEqual([], log.events.all)
        log.release.set()
        queue.close()
        self.assertEqual([event(1), event(2), event(3)], log.events.all)

    def test_a_full_queue_drops_and_the_next_event_taken_carries_the_count(self) -> None:
        log = StalledLog()
        queue = self.queue(log, max_bytes=2 * SIZE)
        self.assertEqual([True], self.offers(queue, [1]))
        self.assertTrue(log.stalled.wait(HANG_GUARD_S))
        self.assertEqual([True, False, False], self.offers(queue, [2, 3, 4]))  # event 1 counts until delivered
        log.release.set()
        self.assertEqual([event(1), event(2)], log.events.wait(2))
        self.assertTrue(self.idle(queue))
        self.assertEqual([True], self.offers(queue, [5]))
        queue.close()
        self.assertEqual([event(1), event(2), {"event": "diagnostics_dropped", "dropped": 2}, event(5)],
                         log.events.all)

    def test_a_count_not_yet_carried_is_delivered_at_the_end(self) -> None:
        log = StalledLog()
        queue = self.queue(log, max_bytes=SIZE)
        self.assertEqual([True, False, False], self.offers(queue, [1, 2, 3]))
        log.release.set()
        queue.close()
        self.assertEqual([event(1), {"event": "diagnostics_dropped", "dropped": 2}], log.events.all)

    def test_the_last_event_is_taken_whatever_the_bound(self) -> None:
        log = StalledLog()
        queue = self.queue(log, max_bytes=SIZE)
        self.assertEqual([True, False], self.offers(queue, [1, 2]))
        self.assertTrue(log.stalled.wait(HANG_GUARD_S))  # event 1 still fills the queue
        closing = threading.Thread(target=queue.close, kwargs={"last": event(9)}, daemon=True)
        closing.start()
        with queue._cond:  # close() has taken its last event although the queue is full
            self.assertTrue(queue._cond.wait_for(lambda: queue._closing, HANG_GUARD_S))
        log.release.set()
        closing.join(HANG_GUARD_S)
        self.assertFalse(closing.is_alive())
        self.assertEqual([event(1), {"event": "diagnostics_dropped", "dropped": 1}, event(9)], log.events.all)

    def test_an_event_over_the_bound_is_taken_into_an_empty_queue_only(self) -> None:
        log = StalledLog()
        queue = self.queue(log, max_bytes=1)
        self.assertEqual([True, False], self.offers(queue, [1, 2]))
        log.release.set()
        self.assertTrue(self.idle(queue))
        self.assertEqual([True], self.offers(queue, [3]))
        queue.close()
        self.assertEqual([event(1), {"event": "diagnostics_dropped", "dropped": 1}, event(3)], log.events.all)

    def test_a_log_that_raises_ends_delivery(self) -> None:
        log = StalledLog(raises=True)
        queue = self.queue(log)
        self.assertEqual([True, True], self.offers(queue, [1, 2]))
        self.assertTrue(log.stalled.wait(HANG_GUARD_S))
        log.release.set()  # the delivery of event 1 raises
        self.assertTrue(self.idle(queue))
        self.assertEqual([False], self.offers(queue, [3]))  # refused for the failure, before close()
        queue.close()
        self.assertEqual([], log.events.all)

    def test_the_bound_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            DiagnosticQueue(Events(), max_bytes=0)


def answer(n) -> dict:
    return {"jsonrpc": "2.0", "id": n, "result": {}}


class ReplyDiagnosticsTest(unittest.TestCase):
    """The reply queue's two lines per stretch of drops go through a diagnostics queue of its own."""

    def check_offer_returns_while_the_log_is_stalled(self, stalled_on: str) -> None:
        entered, release = threading.Event(), threading.Event()

        def log(item):
            if item["event"] == stalled_on:
                entered.set()
                if not release.wait(HANG_GUARD_S):
                    raise AssertionError("the log was never released")

        out = Stalling({1})
        replies = ReplyQueue(FrameWriter(out), max_bytes=len(encode(answer(1))), log=log)
        replies.start()
        taken: list[bool] = []
        try:
            self.assertTrue(replies.offer(answer(1)))
            self.assertTrue(out.stalled.wait(HANG_GUARD_S))
            if stalled_on == "replies_resumed":
                self.assertFalse(replies.offer(answer(2)))
                out.release.set()
                with replies._cond:
                    self.assertTrue(replies._cond.wait_for(lambda: not replies._pending, HANG_GUARD_S))
            offering = threading.Thread(target=lambda: taken.append(replies.offer(answer(3))), daemon=True)
            offering.start()
            offering.join(HANG_GUARD_S)
            self.assertFalse(offering.is_alive(), f"offer waited for the log's {stalled_on} line")
            self.assertTrue(entered.wait(HANG_GUARD_S))  # the line did reach the log, on another thread
        finally:
            release.set()
            out.release.set()
            replies.close()
        self.assertEqual([stalled_on == "replies_resumed"], taken)

    def test_the_drop_line_never_makes_offer_wait(self) -> None:
        self.check_offer_returns_while_the_log_is_stalled("replies_dropped")

    def test_the_resume_line_never_makes_offer_wait(self) -> None:
        self.check_offer_returns_while_the_log_is_stalled("replies_resumed")


class StalledStderrTest(unittest.TestCase):
    """``serve`` with a ``log`` that has stopped taking events (a client that left stderr unread)."""

    def test_the_reader_goes_on_a_cancellation_is_applied_and_a_stop_is_sent(self) -> None:
        stalled, release = threading.Event(), threading.Event()
        events = Events()

        def log(item):
            if item["event"] == "initialize":  # the first event: stderr stops taking lines here
                stalled.set()
                if not release.wait(HANG_GUARD_S):
                    raise AssertionError("stderr was never released")
            events(item)

        api = GateAPI()
        served = Served(api, log=log, max_in_flight=1, reserved_stop=1, max_pending_diagnostic_bytes=1)
        served.send(INIT)
        self.assertEqual("init", served.out.next()["id"])
        self.assertTrue(stalled.wait(HANG_GUARD_S))
        served.send(call(2, *START))
        start = api.next_entered()      # holds the one ordinary slot
        served.send(call(3, *STATUS))   # waits for it
        served.send(cancel(3))          # applied (its line is dropped: the queue is full)
        served.send(ping(4))
        self.assertEqual({"jsonrpc": "2.0", "id": 4, "result": {}}, served.out.next())
        served.send(call(5, *STOP))     # sent, on the reserved slot
        stop = api.next_entered()
        self.assertEqual("run.stop", stop[0])
        for gate in (stop, start):
            gate[2].set()
        self.assertEqual({2, 5}, {served.out.next()["id"], served.out.next()["id"]})
        release.set()
        self.assertEqual([], served.finish(self))
        self.assertEqual(["run.start", "run.stop"], [m for m, _ in api.calls])
        delivered = events.all
        self.assertEqual("initialize", delivered[0]["event"])
        self.assertEqual({"event": "diagnostics_dropped", "dropped": 1}, delivered[1])  # the cancellation's line
        self.assertEqual({"event": "end_of_input", "stdout_broken": False}, delivered[-1])


    def test_the_last_line_is_delivered_though_the_queue_is_full(self) -> None:
        stalled, release, closing = threading.Event(), threading.Event(), threading.Event()
        events = Events()

        def log(item):
            if item["event"] == "initialize":  # stderr stops here and stays stopped until serve ends
                stalled.set()
                if not release.wait(HANG_GUARD_S):
                    raise AssertionError("stderr was never released")
            events(item)

        real_close = DiagnosticQueue.close

        def close(queue, last=None):
            if queue._log is log:  # serve's own queue (the reply queue's queue delivers to it)
                closing.set()
            real_close(queue, last)

        with mock.patch.object(DiagnosticQueue, "close", close):
            served = Served(GateAPI(), log=log, max_pending_diagnostic_bytes=1)
            served.send(INIT)
            self.assertEqual("init", served.out.next()["id"])
            self.assertTrue(stalled.wait(HANG_GUARD_S))
            served.end_input()
            self.assertTrue(closing.wait(HANG_GUARD_S))  # serve is ending; its queue still holds initialize
            release.set()
            self.assertEqual([], served.finish(self))
        self.assertEqual(["initialize", "end_of_input"], [e["event"] for e in events.all])


if __name__ == "__main__":
    unittest.main()
