"""DeepSeek's round-10 read of #400 (``da6fbf63``; all four points still true of ``d190db02``) and two review
threads, each taken as tests that fail on ``d190db02`` and on ``da6fbf63`` (or pin what held there, as marked).
The integration sweep's F1 to F3 are tested in ``test_composition`` (``d190db02``).

* F10-1, F10-4: a ``stop`` that lands on a start's thread as the start takes its lock (a signal handler, say)
  waited for ever on a lock its own thread held; the lock is re-entrant again, and the test fails with a plain one.
* F10-2: a ``stop`` on a start's thread before the start read its stop count was not seen, and the start published
  a worker after it; the count is read first. A start that waited for another thread's stop still starts, even
  one called before that stop withdrew (both pinned: a stop counts on its own thread only).
* F10-3: the worker refusal is the queue's own: a worker of one queue may stop another queue (pinned).
* A ``start`` landing inside a stop on its thread is refused, and a stop inside a start keeps that start's own mark,
  so a start after it is refused too (both pinned: the plain lock held them, the re-entrant one must too).
* Devin's threads: the worker is ready again after a failed read on an empty queue, and ``approval list`` takes
  ``--after``.

Every start or stop that could wait for ever runs on a daemon thread with a bounded join.
"""

from __future__ import annotations

import argparse
import io
import json
import threading
import time
import types
import unittest
from unittest import mock

from recursion_core.approvals import model
from recursion_core.approvals import store as rows
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import FakeStore, needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base
from recursion_core.approvals.tests.test_review9 import _context, _in_thread, _settle

try:
    from recursion_core.control.commands import approval as cli
    from recursion_core.control.hooks import Hooks
except ImportError:  # pragma: no cover
    Hooks = None


class _HookAfterTaking:
    """The queue's lifecycle lock, running ``hook`` once, on the taking thread, just after the lock is taken: where
    a signal handler lands before the call has recorded its thread."""

    def __init__(self, lock, hook) -> None:
        self._lock, self._hook = lock, hook

    def __enter__(self):
        entered = self._lock.__enter__()
        hook, self._hook = self._hook, None
        if hook is not None:
            hook()
        return entered

    def __exit__(self, *exc_info):
        return self._lock.__exit__(*exc_info)


@needs_fakes
class CallsOnTheStartsThreadTests(_Base):
    def test_a_stop_landing_as_the_start_takes_its_lock_runs_and_stands(self) -> None:  # DeepSeek F10-1, F10-4
        queue = self.queue
        self.addCleanup(_settle, queue)
        outcome: list = []

        def handler() -> None:  # as a signal handler on the start's thread
            try:
                queue.stop()
                outcome.append("returned")
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                outcome.append(type(exc).__name__)

        queue._lifecycle = _HookAfterTaking(queue._lifecycle, handler)
        starter, box = _in_thread(lambda: queue.start(_context(self.store)), "start")
        starter.join(3)
        self.assertFalse(starter.is_alive(), "the stop waited for a lock its own thread holds")
        self.assertNotIn("raised", box)
        self.assertEqual(outcome, ["returned"])
        self.assertFalse(queue.ready)
        self.assertEqual(queue._workers, [])

    def test_a_stop_landing_before_the_start_reads_its_count_stands(self) -> None:  # DeepSeek F10-2
        queue = self.queue
        self.addCleanup(_settle, queue)
        real_start, outcome = queue._start, []

        def start_after_a_stop(*args):
            queue.stop()  # as a signal handler on the start's thread, once the start has recorded its thread
            outcome.append("stopped")
            return real_start(*args)

        with mock.patch.object(queue, "_start", side_effect=start_after_a_stop):
            starter, box = _in_thread(lambda: queue.start(_context(self.store)), "start")
            starter.join(3)
        self.assertFalse(starter.is_alive())
        self.assertNotIn("raised", box)
        self.assertEqual(outcome, ["stopped"])
        self.assertFalse(queue.ready, "the start published a worker after a stop on its own thread")
        self.assertEqual(queue._workers, [])

    def test_a_stop_landing_as_the_start_launches_its_worker_leaves_it_stopped(self) -> None:  # F10-2's class
        self.plugin_request()  # no surface takes it: a start that runs would offer it
        queue = self.restarted()
        self.addCleanup(_settle, queue)
        offered: list = []
        queue.subscribe_requested(lambda record: offered.append(record["approval_id"]))
        real_launch = threading.Thread.start

        def launch(thread) -> None:
            real_launch(thread)
            if thread.name == "approvals-deadlines" and threading.current_thread().name == "start":
                queue.stop()  # as a signal handler on the start's thread, just after the launch

        with mock.patch.object(threading.Thread, "start", launch):
            starter, box = _in_thread(lambda: queue.start(_context(self.store)), "start")
            starter.join(3)
        self.assertFalse(starter.is_alive())
        self.assertNotIn("raised", box)
        self.assertEqual(offered, [], "a start offered its backlog after a stop on its own thread")
        self.assertFalse(queue._running)
        self.assertFalse(queue.ready)
        self.assertEqual(queue._workers, [])

    def test_a_start_that_waited_for_another_threads_stop_starts(self) -> None:  # F10-2's other side (pinned)
        queue = self.queue
        self.addCleanup(_settle, queue)
        queue.start(_context(self.store))
        old = queue._worker
        in_join, release = threading.Event(), threading.Event()
        real_join = old.join

        def join(*args, **kwargs):
            in_join.set()
            if not release.wait(3):
                raise RuntimeError("bounded join gate")
            return real_join(*args, **kwargs)

        with mock.patch.object(old, "join", join):
            stopper, stop_box = _in_thread(queue.stop, "stop")
            self.assertTrue(in_join.wait(2))
            starter, start_box = _in_thread(lambda: queue.start(_context(self.store)), "start")
            starter.join(0.2)
            self.assertTrue(starter.is_alive(), "the start did not wait for the stop")
            release.set()
            stopper.join(3)
            starter.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(starter.is_alive())
        self.assertNotIn("raised", stop_box)
        self.assertNotIn("raised", start_box)
        self.assertTrue(queue.ready, "a start that waited for another thread's stop did not start")
        self.assertTrue(queue._worker.is_alive())

    def test_a_start_called_before_another_threads_stop_withdraws_starts(self) -> None:  # F10-2 (pinned)
        # The start reads its own thread's stop count before it takes the lock; a stop another thread makes after
        # that read is not this start's, and the start that waited for it still starts.
        queue = self.queue
        self.addCleanup(_settle, queue)
        queue.start(_context(self.store))
        in_withdraw, release = threading.Event(), threading.Event()
        real_withdraw = queue._withdraw

        def withdraw():
            in_withdraw.set()
            if not release.wait(3):
                raise RuntimeError("bounded withdraw gate")
            return real_withdraw()

        with mock.patch.object(queue, "_withdraw", withdraw):
            stopper, stop_box = _in_thread(queue.stop, "stop")
            self.assertTrue(in_withdraw.wait(2))
            starter, start_box = _in_thread(lambda: queue.start(_context(self.store)), "start")
            starter.join(0.2)
            self.assertTrue(starter.is_alive(), "the start did not wait for the stop")
            release.set()
            stopper.join(3)
            starter.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(starter.is_alive())
        self.assertNotIn("raised", stop_box)
        self.assertNotIn("raised", start_box)
        self.assertTrue(queue.ready, "another thread's stop, made after this start read its count, stopped it")
        self.assertTrue(queue._worker.is_alive())

    def test_a_start_landing_inside_a_stop_on_its_thread_is_refused(self) -> None:  # pinned
        queue = self.queue
        self.addCleanup(_settle, queue)
        queue.start(_context(self.store))
        worker, outcome = queue._worker, []
        real_join = worker.join

        def join(*args, **kwargs):  # as a signal handler while the stop joins
            try:
                queue.start(_context(self.store))
                outcome.append("returned")
            except RuntimeError:
                outcome.append("RuntimeError")
            return real_join(*args, **kwargs)

        with mock.patch.object(worker, "join", join):
            stopper, box = _in_thread(queue.stop, "stop")
            stopper.join(3)
        self.assertFalse(stopper.is_alive(), "the stop joined a worker that a start inside it published")
        self.assertNotIn("raised", box)
        self.assertEqual(outcome, ["RuntimeError"])
        self.assertFalse(queue.ready)
        self.assertEqual(queue._workers, [])

    def test_a_start_after_a_stop_inside_the_same_start_is_still_refused(self) -> None:  # pinned
        self.engine_request()  # its driver dies with this queue: the next start's recovery ends it, and tells
        queue = self.restarted()
        self.addCleanup(_settle, queue)
        outcome: list = []

        def stop_it(record, binding) -> None:
            queue.stop()
            outcome.append("stopped")

        def start_it(record, binding) -> None:
            try:
                queue.start(_context(self.store))
                outcome.append("started")
            except RuntimeError:
                outcome.append("RuntimeError")

        queue.subscribe_answered(stop_it)
        queue.subscribe_answered(start_it)
        starter, box = _in_thread(lambda: queue.start(_context(self.store)), "start")
        starter.join(3)
        self.assertFalse(starter.is_alive())
        self.assertNotIn("raised", box)
        self.assertEqual(outcome, ["stopped", "RuntimeError"])
        self.assertFalse(queue.ready)
        self.assertEqual(queue._workers, [])


@needs_fakes
class TwoQueuesTests(_Base):
    def test_a_worker_of_one_queue_may_stop_another_queue(self) -> None:  # DeepSeek F10-3, its part A (pinned)
        other_store = FakeStore()
        self.addCleanup(other_store.close)
        other = ApprovalQueue(clock=self.clock, retry_seconds=0.0)
        other.attach(other_store)
        self.addCleanup(_settle, other)
        queue = self.queue
        self.addCleanup(_settle, queue)
        outcome, done = [], threading.Event()

        def answered(record, binding) -> None:
            if done.is_set():
                return
            try:
                other.stop()
                outcome.append("returned")
            except RuntimeError:
                outcome.append("RuntimeError")
            done.set()

        queue.subscribe_answered(answered)
        other.start(_context(other_store))
        other_worker = other._worker
        queue.start(_context(self.store))
        self.plugin_request()
        self.clock.now += 301  # its deadline has come: this queue's worker sweeps it and tells the subscriber
        queue._wake.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(outcome, ["returned"])
        self.assertFalse(other.ready)
        self.assertFalse(other_worker.is_alive())


@needs_fakes
class WorkerReadinessTests(_Base):
    def test_the_worker_is_ready_again_after_a_failed_read_on_an_empty_queue(self) -> None:  # Devin's thread
        queue = self.queue
        self.addCleanup(_settle, queue)
        real_next, real_sweep = rows.next_deadline, queue.sweep
        reads, sweeps = [], []

        def next_deadline(tx):
            reads.append(1)
            if len(reads) == 1:
                raise OSError("synthetic: one transient failure")
            return real_next(tx)

        def sweep():
            sweeps.append(1)
            return real_sweep()

        with mock.patch.object(rows, "next_deadline", side_effect=next_deadline), \
                mock.patch.object(queue, "sweep", side_effect=sweep):
            queue.start(_context(self.store))
            limit = time.monotonic() + 3
            while len(reads) < 2 and time.monotonic() < limit:
                time.sleep(0.01)
            while not queue.ready and time.monotonic() < limit:
                time.sleep(0.01)
            self.assertTrue(queue.ready, "the worker read the queue again but stays not ready")
            settled = len(sweeps)
            time.sleep(0.3)  # and it then waits, rather than sweeping without pause
            self.assertLessEqual(len(sweeps) - settled, 1)


@needs_fakes
@unittest.skipIf(Hooks is None, "SPINE's hooks are not in this tree")
class CommandLinePagingTests(_Base):
    def test_approval_list_takes_after_and_reaches_every_page(self) -> None:  # Devin's thread
        root = argparse.ArgumentParser()
        cli.parser(root.add_subparsers(dest="command"))
        made = {self.plugin_request() for _ in range(101)}
        sent: list = []

        def call(method, params):
            sent.append(dict(params))
            return model.list_params_refusal(params) or self.queue.list_page(params)

        client = types.SimpleNamespace(call=call)

        def run(*argv) -> tuple:
            args = root.parse_args(["approval", "list", *argv])
            args.out, args.err = io.StringIO(), io.StringIO()
            return cli.dispatch(args, client), json.loads(args.out.getvalue())

        status, first = run()
        self.assertEqual((status, len(first["approvals"])), (0, 100))
        status, second = run("--after", first["next_after"])
        self.assertEqual((status, len(second["approvals"])), (0, 1))
        self.assertNotIn("next_after", second)
        self.assertEqual(sent[1]["after"], first["next_after"])
        self.assertEqual({held["approval_id"] for held in first["approvals"] + second["approvals"]}, made)


if __name__ == "__main__":
    unittest.main()
