"""Round 7 of #400's Codex reads (astra and sol, of ``e6deff65``) and DeepSeek's round-8 read of the same head: the
required change of astra's read, taken as astra's own tests with names and layout changed:

* ``start`` and ``stop`` run one at a time, so a start that comes during a stop neither publishes a worker the stop
  then joins unsignalled, nor launches one the stop has already passed over, nor clears the wake an older worker
  is still waiting on (astra 1; DeepSeek F8-1). Each of these three fails on ``e6deff65``.
* A worker whose generation a later start replaced passes a wake on without sweeping (astra's retired-worker
  check, which catches the removal of that generation check, DeepSeek's surviving M12; it passes on ``e6deff65``).

Sol's read (MERGE AS IS) holds the overlap outside the composition's serialized lifecycle; astra's requires the
queue to keep its ownership for every caller, and the stricter reading is taken. DeepSeek's F8-2 (a failed
recovery's backlog), F8-3 (the documented second interruption), F8-5 (a registry that only ``stop`` prunes) and
the redundant cleanup marks (F7-5) are for later. Every thread a test starts is a daemon with a bounded join.
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


@needs_fakes
class OneAtATimeTests(_Base):
    def test_a_start_during_a_stop_does_not_make_the_stop_join_an_unsignalled_worker(self) -> None:  # astra 1
        queue = self.queue
        queue.start(_context(self.store))
        old = queue._worker
        in_join, release_join, done = threading.Event(), threading.Event(), threading.Event()
        attempting, started = threading.Event(), threading.Event()
        new_workers: list = []
        errors: list = []
        real_join = old.join

        def join(*args, **kwargs):
            in_join.set()
            if not release_join.wait(3):
                raise RuntimeError("bounded join gate")
            return real_join(*args, **kwargs)

        def restart() -> None:
            attempting.set()
            try:
                queue.start(_context(self.store))
                new_workers.append(queue._worker)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(type(exc).__name__)
            finally:
                started.set()

        stopper = threading.Thread(target=lambda: (queue.stop(), done.set()), daemon=True)
        starter = threading.Thread(target=restart, daemon=True)
        with mock.patch.object(old, "join", join):
            try:
                stopper.start()
                self.assertTrue(in_join.wait(2))
                starter.start()
                self.assertTrue(attempting.wait(2))
                started.wait(0.2)  # a start that waits for the stop is what is asked for
                release_join.set()
                returned, start_returned = done.wait(2), started.wait(2)
                self.assertTrue(returned, "stop joined a newly started worker it never withdrew")
                self.assertTrue(start_returned)
                self.assertEqual(errors, [])
            finally:
                release_join.set()
                queue._withdraw()
                stopper.join(3)
                starter.join(3)
                queue._withdraw()
                old.join(2)
                for new in new_workers:
                    if new is not None:
                        new.join(2)

    def test_a_retry_during_a_stop_cannot_take_an_older_workers_shutdown_wake(self) -> None:  # astra 1
        queue = self.queue
        old: list = []
        workers: list = []
        entered, release, withdrawn = threading.Event(), threading.Event(), threading.Event()
        stopped, started, new_cleared = threading.Event(), threading.Event(), threading.Event()
        real_wait, real_clear, real_withdraw = queue._wake.wait, queue._wake.clear, queue._withdraw

        def wait(timeout=None):
            current = threading.current_thread()
            if current not in workers:
                workers.append(current)
            if not old:
                old.append(current)
            if current is old[0]:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("bounded old-worker entry gate")
            return real_wait(timeout)

        def clear() -> None:
            real_clear()
            if old and threading.current_thread() is not old[0]:
                new_cleared.set()

        def withdraw() -> None:
            real_withdraw()
            withdrawn.set()

        def restart() -> None:
            queue.start(_context(self.store))
            started.set()

        stopper = threading.Thread(target=lambda: (queue.stop(), stopped.set()), daemon=True)
        starter = threading.Thread(target=restart, daemon=True)
        with mock.patch.object(queue._wake, "wait", wait), mock.patch.object(queue._wake, "clear", clear), \
                mock.patch.object(queue, "_withdraw", withdraw):
            try:
                queue.start(_context(self.store))
                self.assertTrue(entered.wait(2))
                stopper.start()
                self.assertTrue(withdrawn.wait(2))
                starter.start()
                new_cleared.wait(0.3)  # a later worker that clears the wake now takes the older one's (astra)
                release.set()
                self.assertTrue(stopped.wait(2), "a later generation took the older worker's shutdown wake")
                self.assertTrue(started.wait(2))
            finally:
                release.set()
                real_withdraw()
                stopper.join(3)
                starter.join(3)
                real_withdraw()
                for worker in workers:
                    worker.join(2)

    def test_a_launch_pending_while_a_stop_runs_stays_owned_until_joined(self) -> None:  # astra 1, DeepSeek F8-1
        queue = self.queue
        withdrawn, read_pending, published = threading.Event(), threading.Event(), threading.Event()
        launch_allowed, in_worker, run_allowed = threading.Event(), threading.Event(), threading.Event()
        stopped, started = threading.Event(), threading.Event()
        workers: list = []
        errors: list = []
        real_withdraw, real_run, real_start = queue._withdraw, queue._run, threading.Thread.start

        def withdraw() -> None:
            real_withdraw()
            if threading.current_thread().name == "first-stop":
                withdrawn.set()
                if not read_pending.wait(4):
                    raise RuntimeError("bounded stop gate")

        def run(*args):
            in_worker.set()
            if not run_allowed.wait(4):
                raise RuntimeError("bounded worker entry gate")
            return real_run(*args)

        def launch(thread):
            if thread.name == "approvals-deadlines":
                workers.append(thread)
                published.set()
                if not launch_allowed.wait(4):
                    raise RuntimeError("bounded launch gate")
            return real_start(thread)

        def stop() -> None:
            try:
                queue.stop()
                stopped.set()
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(type(exc).__name__)

        def start() -> None:
            try:
                queue.start(_context(self.store))
                started.set()
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(type(exc).__name__)

        stopper = threading.Thread(target=stop, name="first-stop", daemon=True)
        starter = threading.Thread(target=start, name="restart", daemon=True)
        with mock.patch.object(queue, "_withdraw", withdraw), mock.patch.object(queue, "_run", run), \
                mock.patch.object(threading.Thread, "start", launch):
            try:
                stopper.start()
                self.assertTrue(withdrawn.wait(2))
                starter.start()
                published.wait(0.3)  # a publication that waits for the stop is what is asked for
                read_pending.set()
                stopped.wait(0.2)
                launch_allowed.set()
                self.assertTrue(in_worker.wait(2))
                worker = workers[0]
                self.assertTrue(worker in queue._workers or queue._worker is worker,
                                "a published worker was forgotten before it could launch")
            finally:
                read_pending.set()
                launch_allowed.set()
                run_allowed.set()
                real_withdraw()
                starter.join(3)
                stopper.join(3)
                real_withdraw()
                for worker in workers:
                    worker.join(2)
        self.assertEqual(errors, [])

    def test_a_replaced_worker_passes_a_wake_on_without_sweeping(self) -> None:  # astra; DeepSeek F8-4's M12
        queue = self.queue
        old: list = []
        new: list = []
        seen: list = []
        old_waiting, old_awake, release, new_waiting = (threading.Event() for _ in range(4))
        real_wait, real_sweep = queue._wake.wait, queue.sweep

        def wait(timeout=None):
            current = threading.current_thread()
            if not old or current is old[0]:
                if not old:
                    old.append(current)
                old_waiting.set()
                result = real_wait(timeout)
                old_awake.set()
                if not release.wait(3):
                    raise RuntimeError("bounded replaced-worker gate")
                return result
            if current not in new:
                new.append(current)
            new_waiting.set()
            return real_wait(timeout)

        def sweep():
            seen.append(threading.current_thread())
            return real_sweep()

        with mock.patch.object(queue._wake, "wait", wait), mock.patch.object(queue, "sweep", sweep):
            try:
                queue.start(_context(self.store))
                self.assertTrue(old_waiting.wait(2))
                queue._wake.set()
                self.assertTrue(old_awake.wait(2))
                queue.start(_context(self.store))  # a second start replaces the first worker's generation
                self.assertTrue(new_waiting.wait(2))
                seen.clear()
                release.set()
                old[0].join(2)
                self.assertFalse(old[0].is_alive())
                self.assertNotIn(old[0], seen, "a worker whose generation was replaced swept after it")
            finally:
                release.set()
                queue._withdraw()
                for worker in old + new:
                    worker.join(2)
                queue.stop()


if __name__ == "__main__":
    unittest.main()
