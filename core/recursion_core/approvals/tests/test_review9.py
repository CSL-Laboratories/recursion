"""DeepSeek's round-9 read of #400 (``553de2cb``): ``start`` and ``stop`` called back by a subscriber.

* F9-1: a ``stop`` or ``start`` that the deadline worker's answered subscriber calls while another thread's stop
  is joining that worker waited for ever on ``_lifecycle``, and so did that stop. Both are now refused at once
  on a worker's thread, the current worker's or a replaced one's (``RuntimeError``, nothing changed), whoever
  else is in a start or stop; so is a stop there with nobody else in one (round 8's F8-6 withdrew the queue and
  then raised).
* F9-2: a ``stop`` that an answered subscriber calls inside a start's recovery, on the start's thread, stands:
  the start publishes no worker after it and leaves the queue stopped. A ``start`` called there is refused.
* F9-3: the stop that a requested subscriber calls from a start's own offering is pinned: it runs at once and
  joins the worker that start launched.

The tests for F9-1 and F9-2 fail on ``553de2cb``; the F9-3 pin passes there (with ``RLock`` weakened to
``Lock`` it fails). Every start or stop that could wait for ever runs on a daemon thread with a bounded join,
so a regression fails the test instead of hanging the run. Sol's round-8 read of the same head asks the same
change as F9-1 (its required 1) with its callback case over the events store's SQLite file as well as the fake;
both are here.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


def _in_thread(fn, name: str):
    """Run ``fn`` on a daemon thread; the box gets what it returned or raised."""
    box: dict = {}

    def run() -> None:
        try:
            box["returned"] = fn()
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            box["raised"] = exc

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, box


def _settle(queue) -> None:
    """Leave no worker of ``queue`` running, without taking its lifecycle lock (a failed test may hold it)."""
    queue._closing.set()
    queue._wake.set()
    for worker in list(queue._workers):
        if worker.ident is not None and worker is not threading.current_thread():
            worker.join(3)


@needs_fakes
class CallsFromTheWorkersThreadTests(_Base):
    def _worker_calls_back_during_a_stop(self, call: str, *, replaced: bool = False, sqlite: bool = False) -> None:
        if sqlite:  # Sol's round-8 case: the events store over a scratch SQLite file, not the fake
            try:
                import fcntl  # noqa: F401 - the events store takes its writer lock with it
            except ImportError:
                self.skipTest("the events store runs where fcntl does")
            from recursion_core.events.db import EventStore

            folder = tempfile.mkdtemp(prefix="approvals-review9-")
            self.addCleanup(shutil.rmtree, folder, True)
            store = EventStore(pathlib.Path(folder) / "events.db", clock=self.clock)
            self.addCleanup(store.close)
            queue = ApprovalQueue(clock=self.clock, retry_seconds=0.0)
            queue.attach(store)
        else:
            store, queue = self.store, self.restarted()
        self.addCleanup(_settle, queue)
        in_subscriber, withdrawn = threading.Event(), threading.Event()
        outcome: list = []
        real_withdraw = queue._withdraw

        def withdraw() -> None:
            real_withdraw()
            if threading.current_thread().name == "outer-stop":
                withdrawn.set()

        def answered(record, binding) -> None:
            if in_subscriber.is_set():
                return
            in_subscriber.set()
            if not withdrawn.wait(3):  # the outer stop has withdrawn the queue and is joining this worker
                outcome.append("the outer stop never withdrew")
                return
            try:
                if call == "start":
                    queue.start(_context(store))
                else:
                    queue.stop()
                outcome.append("returned")
            except RuntimeError:
                outcome.append("RuntimeError")

        queue.subscribe_answered(answered)
        with mock.patch.object(queue, "_withdraw", withdraw):
            queue.start(_context(store))
            self.plugin_request(queue=queue)
            self.clock.now += 301  # its deadline has come: the worker's sweep expires it and tells the subscriber
            queue._wake.set()
            self.assertTrue(in_subscriber.wait(3))
            if replaced:  # a later start replaces the worker that is inside its subscriber; the stop joins both
                queue.start(_context(store))
            stopper, box = _in_thread(queue.stop, "outer-stop")
            stopper.join(3)
            self.assertFalse(stopper.is_alive(), "the stop waited for ever on a worker that waited for it")
        self.assertNotIn("raised", box)
        self.assertEqual(outcome, ["RuntimeError"])
        self.assertEqual(queue._workers, [])
        self.assertFalse(queue.ready)

    def test_a_stop_from_the_workers_subscriber_while_a_stop_joins_that_worker(self) -> None:  # DeepSeek F9-1
        self._worker_calls_back_during_a_stop("stop")

    def test_a_stop_from_the_workers_subscriber_over_sqlite_while_a_stop_joins_it(self) -> None:  # Sol r8 1
        self._worker_calls_back_during_a_stop("stop", sqlite=True)

    def test_a_start_from_the_workers_subscriber_while_a_stop_joins_that_worker(self) -> None:  # DeepSeek F9-1
        self._worker_calls_back_during_a_stop("start")

    def test_a_stop_from_a_replaced_workers_subscriber_while_a_stop_joins_it(self) -> None:  # DeepSeek F9-1
        self._worker_calls_back_during_a_stop("stop", replaced=True)

    def test_a_stop_on_the_workers_own_thread_is_refused_and_changes_nothing(self) -> None:  # F9-1, round 8's F8-6
        queue = self.queue
        self.addCleanup(_settle, queue)
        outcome: list = []
        done = threading.Event()

        def answered(record, binding) -> None:
            if done.is_set():
                return
            try:
                queue.stop()
                outcome.append("returned")
            except RuntimeError:
                outcome.append("RuntimeError")
            outcome.append((queue.ready, queue._closing.is_set(), queue._generation is not None))
            done.set()

        queue.subscribe_answered(answered)
        queue.start(_context(self.store))
        worker = queue._worker
        self.plugin_request()
        self.clock.now += 301
        queue._wake.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(outcome, ["RuntimeError", (True, False, True)])
        self.assertIs(queue._worker, worker)
        stopper, box = _in_thread(queue.stop, "outer-stop")  # from outside, the worker is joined as before
        stopper.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertNotIn("raised", box)
        self.assertFalse(worker.is_alive())
        self.assertEqual(queue._workers, [])


@needs_fakes
class CallsInsideAStartTests(_Base):
    def _start_in_thread(self, queue) -> dict:
        starter, box = _in_thread(lambda: queue.start(_context(self.store)), "start")
        starter.join(3)
        self.assertFalse(starter.is_alive(), "the start never returned")
        self.assertNotIn("raised", box)
        return box

    def test_a_stop_from_an_answered_subscriber_inside_recovery_stands(self) -> None:  # DeepSeek F9-2
        self.engine_request()  # its driver dies with this queue: the next start's recovery ends it as expired
        queue = self.restarted()
        self.addCleanup(_settle, queue)
        seen: list = []

        def answered(record, binding) -> None:
            seen.append(record["state"])
            queue.stop()
            seen.append("stopped")

        queue.subscribe_answered(answered)
        self._start_in_thread(queue)
        self.assertEqual(seen, ["expired", "stopped"])
        self.assertFalse(queue.ready)
        self.assertFalse(queue._running)
        self.assertTrue(queue._closing.is_set())
        self.assertIsNone(queue._worker)
        self.assertEqual(queue._workers, [])
        self.assertIsNone(queue._late)  # the surfaces are let through: a request now is offered at once
        offered: list = []
        queue.subscribe_requested(lambda record: offered.append(record["approval_id"]))
        self.assertEqual(offered, [self.plugin_request(queue=queue)])

    def test_a_start_from_an_answered_subscriber_inside_recovery_is_refused(self) -> None:  # DeepSeek F9-2
        self.engine_request()
        queue = self.restarted()
        self.addCleanup(_settle, queue)
        outcome: list = []

        def answered(record, binding) -> None:
            try:
                queue.start(_context(self.store))
                outcome.append("returned")
            except RuntimeError:
                outcome.append("RuntimeError")

        queue.subscribe_answered(answered)
        self._start_in_thread(queue)
        self.assertEqual(outcome, ["RuntimeError"])
        self.assertTrue(queue.ready)
        self.assertEqual(len(queue._workers), 1)  # the outer start's worker only
        self.assertTrue(queue._worker.is_alive())

    def test_a_stop_from_a_requested_subscriber_in_the_starts_offering_joins_its_worker(self) -> None:  # F9-3
        self.plugin_request()  # no surface took it: the next start offers it once its worker runs
        queue = self.restarted()
        self.addCleanup(_settle, queue)
        workers: list = []

        def requested(record) -> None:
            workers.append(queue._worker)
            queue.stop()

        queue.subscribe_requested(requested)
        self._start_in_thread(queue)
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0])
        self.assertFalse(workers[0].is_alive(), "the stop returned before the start's worker was joined")
        self.assertFalse(queue.ready)
        self.assertEqual(queue._workers, [])


if __name__ == "__main__":
    unittest.main()
