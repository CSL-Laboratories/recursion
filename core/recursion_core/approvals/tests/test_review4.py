"""The fourth outside reads of #400 (two Codex reads of ``9c7c9c92..f60879d4``), each required change in this lane's
queue taken as a test that fails on ``f60879d4`` with #369's ``a95945ca`` merged:

* a surface that subscribes while a start runs, before recovery has offered the backlog, is offered nothing until
  the start has launched its worker, and nothing at all when the launch fails; a request committed meanwhile
  waits for it too (Astra R1, Sol M1);
* a launch interrupted once the worker's thread has launched can never publish ``ready``; the worker is kept for
  ``stop`` to join, and a start after that works (Astra R2).

The lock-discipline checks both reads offered as later coverage are taken too (Astra's ``readiness_check.py``,
Sol's ``coverage_probes.py``). The third required change of both reads (Astra R3, Sol M2: the command id checked
before the receipt lookup) is with the Liaison, since the lookup is in SPINE's dispatcher. Each reviewer's check is
taken as written, with names and layout changed. Every thread a test starts is a daemon with a bounded join.
"""

from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals import store as rows
from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


@needs_fakes
class StartupSubscriberTests(_Base):
    def _subscribe_during_start(self, *, pause: str, fail_launch: bool, meanwhile=None):
        """Restart the queue over one plugin request no surface took, in a thread that pauses at ``pause``
        (``recover``: before recovery; ``_offer_unshown``: after recovery's sweep, before its offering);
        subscribe a surface then and call ``meanwhile(queue)``; then let the start go on, with the worker's
        launch failing when ``fail_launch``. Returns the request's id, what the surface was shown while the start
        was paused and in all (each with whether the worker had launched and ``ready``), what ``meanwhile``
        returned, the start's errors, and the requests never shown at the end."""
        approval_id = self.plugin_request()
        queue = self.restarted()
        paused, release, errors, shown = threading.Event(), threading.Event(), [], []
        original = getattr(queue, pause)

        def pausing(*args, **kwargs):
            if threading.current_thread().name == "r4-starter" and not paused.is_set():
                paused.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            return original(*args, **kwargs)

        real_start = threading.Thread.start

        def launch(thread):
            if fail_launch and thread.name == "approvals-deadlines":
                raise RuntimeError("synthetic launch failure")
            return real_start(thread)

        def start() -> None:
            try:
                queue.start(_context(self.store))
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(type(exc).__name__)

        def surface(record) -> None:
            worker = queue._worker
            shown.append((record["approval_id"], worker is not None and worker.ident is not None, queue.ready))

        starter = threading.Thread(target=start, name="r4-starter", daemon=True)
        made = None
        with mock.patch.object(queue, pause, pausing), mock.patch.object(threading.Thread, "start", launch):
            starter.start()
            try:
                self.assertTrue(paused.wait(3))
                queue.subscribe_requested(surface)
                if meanwhile is not None:
                    made = meanwhile(queue)
                while_paused = list(shown)
            finally:
                release.set()
                starter.join(4)
                queue.stop()
        self.assertFalse(starter.is_alive())
        with self.store.transaction() as tx:
            unshown = rows.unshown(tx)
        return approval_id, while_paused, shown, made, errors, unshown

    def test_a_surface_that_subscribes_before_recovery_is_offered_only_once_the_worker_launched(self) -> None:
        # Astra R1 (her check)
        approval_id, while_paused, shown, _, errors, unshown = self._subscribe_during_start(
            pause="recover", fail_launch=False)
        self.assertEqual(errors, [])
        self.assertEqual(shown, [(approval_id, True, True)], "offered before the start had launched its worker")
        self.assertEqual(unshown, [])

    def test_a_surface_that_subscribes_before_recovery_offers_is_offered_only_once_the_worker_launched(self) -> None:
        # Sol M1 (his check)
        approval_id, while_paused, shown, _, errors, unshown = self._subscribe_during_start(
            pause="_offer_unshown", fail_launch=False)
        self.assertEqual(errors, [])
        self.assertEqual(shown, [(approval_id, True, True)], "recovery's own offering reached a surface early")
        self.assertEqual(unshown, [])

    def test_a_surface_that_subscribes_before_recovery_is_offered_nothing_when_the_launch_fails(self) -> None:
        # Astra R1 (her check)
        approval_id, _, shown, _, errors, unshown = self._subscribe_during_start(pause="recover", fail_launch=True)
        self.assertEqual(errors, ["RuntimeError"])
        self.assertEqual(shown, [], "a start whose launch failed offered the backlog")
        self.assertEqual(unshown, [approval_id], "a start whose launch failed marked the backlog shown")

    def test_a_surface_that_subscribes_before_recovery_offers_is_offered_nothing_when_the_launch_fails(self) -> None:
        # Sol M1 (his check)
        approval_id, _, shown, _, errors, unshown = self._subscribe_during_start(
            pause="_offer_unshown", fail_launch=True)
        self.assertEqual(errors, ["RuntimeError"])
        self.assertEqual(shown, [], "recovery's own offering reached a surface whose start then failed")
        self.assertEqual(unshown, [approval_id], "a start whose launch failed marked the backlog shown")

    def test_a_request_made_during_the_start_reaches_that_surface_only_once_the_worker_launched(self) -> None:
        approval_id, while_paused, shown, second, errors, unshown = self._subscribe_during_start(
            pause="_offer_unshown", fail_launch=False, meanwhile=lambda queue: self.plugin_request(queue=queue))
        self.assertEqual(errors, [])
        self.assertEqual(while_paused, [], "a request committed during the start was offered before the launch")
        self.assertEqual(sorted(shown), sorted([(approval_id, True, True), (second, True, True)]))
        self.assertEqual(unshown, [])

    def test_a_request_made_during_a_start_that_fails_stays_unshown(self) -> None:
        approval_id, _, shown, second, errors, unshown = self._subscribe_during_start(
            pause="_offer_unshown", fail_launch=True, meanwhile=lambda queue: self.plugin_request(queue=queue))
        self.assertEqual(errors, ["RuntimeError"])
        self.assertEqual(shown, [])
        self.assertEqual(sorted(unshown), sorted([approval_id, second]))


@needs_fakes
class InterruptedLaunchTests(_Base):
    def test_a_launch_interrupted_once_the_worker_runs_never_publishes_ready(self) -> None:  # Astra R2 (her check)
        queue = self.queue
        self.plugin_request()  # the worker is woken at once and goes to its sweep
        entered, release, moved = threading.Event(), threading.Event(), threading.Event()
        real_sweep, real_next, real_run = queue.sweep, rows.next_deadline, queue._run
        reads = []

        def sweep():
            if threading.current_thread().name == "approvals-deadlines":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            return real_sweep()

        def next_deadline(tx):
            if threading.current_thread().name == "approvals-deadlines":
                reads.append(1)
                if len(reads) >= 2:
                    moved.set()  # it went round again: what it publishes after a sweep is published by now
            return real_next(tx)

        def run(generation=None) -> None:
            try:
                real_run(generation)
            finally:
                moved.set()  # it returned

        real_start = threading.Thread.start

        def start(thread):
            real_start(thread)
            if not entered.wait(3):
                raise RuntimeError("the worker did not reach its sweep")
            raise KeyboardInterrupt("synthetic interruption once the worker runs")

        with mock.patch.object(queue, "sweep", sweep), mock.patch.object(rows, "next_deadline", next_deadline), \
                mock.patch.object(queue, "_run", run):
            try:
                with mock.patch.object(threading.Thread, "start", start):
                    with self.assertRaises(KeyboardInterrupt):
                        queue.start(_context(self.store))
                self.assertFalse(queue.ready)
                release.set()
                self.assertTrue(moved.wait(3), "the worker neither went round again nor returned")
                observed = (queue.ready, queue._running)
            finally:
                release.set()
                queue.stop()
        self.assertEqual(observed, (False, False), "a start that failed became ready once its worker swept")

    def test_a_launch_interrupted_once_the_worker_runs_keeps_it_for_stop_and_a_start_after_works(self) -> None:
        # Astra R2 and Sol N2 (their checks: a KeyboardInterrupt and a SystemExit once the thread launched)
        real_start = threading.Thread.start
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                queue = self.restarted()

                def start(thread, interruption=interruption):
                    real_start(thread)
                    raise interruption("synthetic interruption once the worker launched")

                with mock.patch.object(threading.Thread, "start", start):
                    with self.assertRaises(interruption):
                        queue.start(_context(self.store))
                worker = queue._worker
                self.assertIsNotNone(worker.ident)
                self.assertEqual((queue.ready, queue._running, queue._late), (False, False, None))
                queue.stop()
                self.assertFalse(worker.is_alive())
                self.assertIsNone(queue._worker)
                queue.start(_context(self.store))
                self.assertTrue(queue.ready)
                self.assertTrue(queue._worker.is_alive())
                queue.stop()


@needs_fakes
class ReadyLockTests(_Base):
    """The two lock rules no lane test pinned (both reads' mutation audits: removing either lock left every lane
    test green); later coverage in both reads, taken now."""

    def test_stop_signals_closing_while_it_holds_the_lock(self) -> None:  # Sol's coverage check
        seen = []
        original = self.queue._closing.set

        def signal() -> None:
            seen.append(self.queue._lock.locked())
            original()

        with mock.patch.object(self.queue._closing, "set", signal):
            self.queue.stop()
        self.assertEqual(seen, [True])

    def test_the_worker_reads_the_closing_signal_under_the_lock_before_it_publishes_ready(self) -> None:
        # Sol's coverage check
        queue = self.queue
        swept, checked, seen = threading.Event(), threading.Event(), []
        real_sweep, real_check = queue.sweep, queue._closing.is_set

        def sweep():
            result = real_sweep()
            if threading.current_thread().name == "approvals-deadlines":
                swept.set()
            return result

        def is_set() -> bool:
            if threading.current_thread().name == "approvals-deadlines" and swept.is_set():
                seen.append(queue._lock.locked())
                queue._closing.set()
                checked.set()
                return True
            return real_check()

        with mock.patch.object(queue, "sweep", sweep), mock.patch.object(queue._closing, "is_set", is_set):
            queue._wake.set()
            queue.start(_context(self.store))
            self.assertTrue(checked.wait(3))
            queue.stop()
        self.assertTrue(seen)
        self.assertTrue(seen[0], "the worker read the closing signal outside the lock before it published ready")

    def test_a_stop_waits_for_a_ready_publication_under_way(self) -> None:  # Astra's coverage check
        queue = self.queue
        queue.start(_context(self.store))
        checked, release, swept, attempted = threading.Event(), threading.Event(), threading.Event(), threading.Event()
        real_check, real_sweep, once = queue._closing.is_set, queue.sweep, []

        def sweep():
            result = real_sweep()
            swept.set()
            return result

        def is_set() -> bool:
            value = real_check()
            if threading.current_thread().name == "approvals-deadlines" and swept.is_set() and not once:
                once.append(1)
                checked.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            return value

        def stop() -> None:
            attempted.set()
            queue.stop()

        with mock.patch.object(queue, "sweep", sweep), mock.patch.object(queue._closing, "is_set", is_set):
            queue._wake.set()
            self.assertTrue(checked.wait(3))
            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            try:
                self.assertTrue(attempted.wait(3))
                # Time only to see that something has NOT happened: the stop has not returned while the worker
                # holds the lock between its closing check and its write of ready.
                queue._closing.wait(0.15)
                self.assertTrue(stopper.is_alive())
            finally:
                release.set()
                stopper.join(4)
        self.assertFalse(stopper.is_alive())
        self.assertIsNone(queue._worker)
        self.assertFalse(queue.ready, "a ready publication under way outlived the stop")


if __name__ == "__main__":
    unittest.main()
