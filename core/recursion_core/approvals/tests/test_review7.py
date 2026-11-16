"""Round 6 of #400's Codex reads (astra and sol, of ``a819d068``) and DeepSeek's round-7 read of the same head; each
required change taken as the readers' own tests, with names and layout changed, every one failing on ``a819d068``:

* the start holds the surfaces back inside its guarded region, so a start interrupted even as it does lets them go
  again, and leaves a worker an earlier start left running as it was (astra 1, sol 1, DeepSeek F7-1);
* ``stop`` wakes and joins every worker a start launched, and no worker takes the wake another waits on: two
  workers that share the signal both return (astra 2, sol 2, DeepSeek F7-3), and a worker an interrupted launch
  kept is joined by ``stop`` after a retry (astra 2, DeepSeek F7-6).

Astra's retry check holds the old worker at a gate until after it expects ``stop`` to return; with ``stop`` joining
every worker it owns, as both reads require, ``stop`` waits for that worker. So here the gate opens while ``stop``
runs, and the test requires that ``stop`` waits for the old worker and then returns with both joined.

DeepSeek's F7-2 (a second interruption inside the start's own withdrawal), F7-4 (the drawer's comment for a value
that is not an object) and F7-5 (a redundant closing mark) are for later, as both Codex reads say; the module text
says what F7-2 leaves. Every thread a test starts is a daemon with a bounded join.
"""

from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


class _RaiseOnceAsLetGo:
    """The queue's lock, raising ``SystemExit`` once, in the starting thread, as it is first let go: the first
    lock a start takes is the one that holds the surfaces back."""

    def __init__(self, lock, starter) -> None:
        self._lock, self._starter, self.raised = lock, starter, False

    def __enter__(self):
        return self._lock.__enter__()

    def __exit__(self, *exc_info):
        let_go = self._lock.__exit__(*exc_info)
        if not self.raised and threading.current_thread() is self._starter:
            self.raised = True
            raise SystemExit("synthetic signal as the start holds the surfaces back")
        return let_go


@needs_fakes
class HeldBackSnapshotTests(_Base):
    def _interrupted_start(self, queue) -> None:
        real = queue._lock
        lock = _RaiseOnceAsLetGo(real, threading.current_thread())
        queue._lock = lock
        try:
            with self.assertRaises(SystemExit):
                queue.start(_context(self.store))
        finally:
            queue._lock = real
        self.assertTrue(lock.raised)

    def test_a_start_interrupted_as_it_holds_the_surfaces_back_lets_them_go(self) -> None:  # sol 1, DeepSeek F7-1
        for already_running in (False, True):
            with self.subTest(already_running=already_running):
                queue, shown, late = self.restarted(), [], []
                queue.subscribe_requested(lambda held, shown=shown: shown.append(held["approval_id"]))
                if already_running:
                    queue.start(_context(self.store))
                previous, generation = queue._worker, queue._generation
                self._interrupted_start(queue)
                first = self.plugin_request(queue=queue)
                queue.subscribe_requested(lambda held, late=late: late.append(held["approval_id"]))
                second = self.plugin_request(queue=queue)
                observed = (queue._late is None, first in shown, second in shown, second in late)
                if already_running:  # a worker an earlier start left running is not this start's to withdraw
                    self.assertIs(queue._worker, previous)
                    self.assertIs(queue._generation, generation)
                    self.assertTrue(queue.ready)
                queue.stop()
                self.assertEqual(observed, (True, True, True, True), "the interrupted start kept the surfaces back")

    def test_a_restart_of_a_stopped_queue_interrupted_as_it_holds_the_surfaces_back_lets_them_go(self) -> None:
        queue = self.queue  # astra 1
        queue.start(_context(self.store))
        queue.stop()
        offered: list = []
        queue.subscribe_requested(lambda held: offered.append(held["approval_id"]))
        self._interrupted_start(queue)
        approval_id = self.plugin_request()
        self.assertIn(approval_id, offered, "an interrupted restart kept the surfaces back")
        self.assertIsNone(queue._late)


@needs_fakes
class SharedWakeTests(_Base):
    def _two_workers_and_a_stop(self, delayed: int) -> None:  # astra 2, DeepSeek F7-3
        queue = self.queue
        entered = [threading.Event(), threading.Event()]
        release, consumed, stopped = threading.Event(), threading.Event(), threading.Event()
        workers: list = []
        real_wait, real_clear = queue._wake.wait, queue._wake.clear

        def wait(timeout=None):
            current = threading.current_thread()
            if current not in workers:
                workers.append(current)
            index = workers.index(current)
            entered[index].set()
            if index == delayed and not release.wait(2):
                raise RuntimeError("bounded scheduling gate")
            return real_wait(timeout)

        def clear() -> None:
            real_clear()
            if threading.current_thread() is workers[1 - delayed]:
                consumed.set()

        def stop() -> None:
            queue.stop()
            stopped.set()

        stopper = None
        with mock.patch.object(queue._wake, "wait", wait), mock.patch.object(queue._wake, "clear", clear):
            try:
                queue.start(_context(self.store))
                self.assertTrue(entered[0].wait(1))
                queue.start(_context(self.store))  # a second start without a stop: two workers share the wake
                self.assertTrue(entered[1].wait(1))
                stopper = threading.Thread(target=stop, daemon=True)
                stopper.start()
                self.assertTrue(consumed.wait(1))
                release.set()
                returned = stopped.wait(2)
                alive = [worker.is_alive() for worker in workers]
                self.assertTrue(returned, "stop waited for a worker that lost the shared shutdown wake")
                self.assertEqual(alive, [False, False], "stop returned while a replaced worker still ran")
            finally:
                release.set()
                queue._closing.set()
                queue._wake.set()
                for worker in workers:
                    worker.join(1)
                if stopper is not None:
                    stopper.join(1)

    def test_stop_returns_when_the_older_worker_takes_the_shutdown_wake(self) -> None:
        self._two_workers_and_a_stop(1)

    def test_stop_returns_when_the_newer_worker_takes_the_shutdown_wake(self) -> None:
        self._two_workers_and_a_stop(0)

    def test_two_workers_cannot_lose_the_stop_wake(self) -> None:  # sol 2, DeepSeek F7-3
        queue, real = self.queue, self.queue._wake
        first_waiting, second_waiting = threading.Event(), threading.Event()
        allow_second, first_cleared, done = threading.Event(), threading.Event(), threading.Event()
        first: list = []

        class Wake:
            def set(self) -> None:
                real.set()

            def is_set(self) -> bool:
                return real.is_set()

            def clear(self) -> None:
                real.clear()
                if queue._closing.is_set():
                    first_cleared.set()

            def wait(self, timeout=None):
                if first and threading.current_thread() is first[0]:
                    first_waiting.set()
                else:
                    second_waiting.set()
                    if not allow_second.wait(2):
                        raise RuntimeError("bounded second-worker gate")
                return real.wait(timeout)

        queue._wake = Wake()
        launch = threading.Thread.start

        def start(thread):
            if not first:
                first.append(thread)
            return launch(thread)

        stopper = None
        try:
            with mock.patch.object(threading.Thread, "start", start):
                queue.start(_context(self.store))
            self.assertTrue(first_waiting.wait(2))
            queue.start(_context(self.store))
            self.assertTrue(second_waiting.wait(2))
            stopper = threading.Thread(target=lambda: (queue.stop(), done.set()), daemon=True)
            stopper.start()
            self.assertTrue(first_cleared.wait(2))
            allow_second.set()
            returned = done.wait(2)
        finally:
            allow_second.set()
            real.set()
            if stopper is not None:
                stopper.join(2)
            for worker in first + [queue._worker]:
                if worker is not None and worker.ident is not None:
                    worker.join(2)
        self.assertTrue(returned, "stop joined a worker that missed the shared wake")
        self.assertFalse(first[0].is_alive())

    def test_a_retry_after_an_interrupted_launch_leaves_the_kept_worker_for_stop_to_join(self) -> None:
        queue = self.queue  # astra 2, DeepSeek F7-6; the gate opens while stop runs (see the module text)
        old_entered, release_old, new_idle, stopped = (threading.Event() for _ in range(4))
        old: list = []
        new: list = []
        count = [0]
        real_wait, launch = queue._wake.wait, threading.Thread.start

        def wait(timeout=None):
            current = threading.current_thread()
            if not old or current is old[0]:
                if not old:
                    old.append(current)
                old_entered.set()
                if not release_old.wait(2):
                    raise RuntimeError("bounded old-worker gate")
            else:
                if not new:
                    new.append(current)
                count[0] += 1
                if count[0] >= 2:
                    new_idle.set()
            return real_wait(timeout)

        def start(thread):
            launch(thread)
            if thread.name == "approvals-deadlines":
                if not old_entered.wait(1):
                    raise RuntimeError("the old worker did not reach its wait")
                raise KeyboardInterrupt("synthetic interrupted launch")

        def stop() -> None:
            queue.stop()
            stopped.set()

        stopper = None
        with mock.patch.object(queue._wake, "wait", wait):
            try:
                with mock.patch.object(threading.Thread, "start", start), self.assertRaises(KeyboardInterrupt):
                    queue.start(_context(self.store))
                queue.start(_context(self.store))  # the retry
                self.assertTrue(new_idle.wait(1))
                stopper = threading.Thread(target=stop, daemon=True)
                stopper.start()
                self.assertFalse(stopped.wait(0.3), "stop returned without the worker the interrupted launch kept")
                release_old.set()
                self.assertTrue(stopped.wait(2))
                self.assertEqual((old[0].is_alive(), new[0].is_alive()), (False, False))
            finally:
                release_old.set()
                queue._closing.set()
                queue._wake.set()
                for worker in old + new:
                    worker.join(1)
                if stopper is not None:
                    stopper.join(1)


if __name__ == "__main__":
    unittest.main()
