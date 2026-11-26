"""The fifth outside read of #400 (a DeepSeek break-it read of ``77897d4c``), each change taken as a test; nine of
the eleven fail on ``77897d4c``. The other two pass there and hold what F1's change must keep: every surface
subscribed before the start is offered the backlog, and one that unsubscribes during the start is offered nothing.

* F2: a deadline worker whose generation has ended (kept after an interrupted launch) changes ``ready`` no more:
  its failure does not withdraw the next worker's ``ready``, and its sweep does not publish ``ready`` over the
  next worker's failure;
* F1: a surface subscribed before a start began is held back like one that subscribes during it: offered the
  backlog only once the worker has launched, and nothing when the launch fails;
* F4: a store failure in a request's insert is a typed refusal (``store_unavailable``), with no waiter kept;
* F5: the queue's own ``open_in`` and ``answer_in`` refuse a principal that may not approve;
* F6: a ``display`` or a ``timeout`` of the wrong kind is a typed refusal;
* F7: the drawer refuses an engine, session or run that is not a plain id, whoever calls it.

F3 (``wait`` has no deadline of its own) is answered in the text, not in code; F8 needs no change. Every thread
a test starts is a daemon with a bounded join.
"""

from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals import effects
from recursion_core.approvals import store as rows
from recursion_core.approvals.effects import EffectRefused
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import _vector_effect, needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


@needs_fakes
class StaleWorkerTests(_Base):
    def _interrupted_launch(self, queue, *, stale_sweep):
        """Start ``queue`` with a launch interrupted once its worker is inside its first sweep, which then runs
        ``stale_sweep`` when released. Returns (the kept worker, the release event, the worker's return event)."""
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        kept, real_sweep, real_run = [], queue.sweep, queue._run

        def sweep():
            if kept and threading.current_thread() is kept[0]:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
                return stale_sweep(real_sweep)
            return real_sweep()

        def run(generation=None) -> None:
            try:
                real_run(generation)
            finally:
                if kept and threading.current_thread() is kept[0]:
                    returned.set()

        real_start = threading.Thread.start

        def start(thread):
            kept.append(thread)
            real_start(thread)
            if not entered.wait(3):
                raise RuntimeError("the worker did not reach its sweep")
            raise KeyboardInterrupt("synthetic interruption once the worker runs")

        for patch in (mock.patch.object(queue, "sweep", sweep), mock.patch.object(queue, "_run", run)):
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                queue.start(_context(self.store))
        self.addCleanup(release.set)
        return kept[0], release, returned

    def test_a_worker_whose_generation_has_ended_does_not_withdraw_the_next_ones_ready(self) -> None:  # DS F2
        queue = self.queue
        self.plugin_request()  # the first worker is woken at once and goes to its sweep

        def failing(real_sweep):
            raise OSError("synthetic store failure in the stale worker's sweep")

        stale, release, returned = self._interrupted_launch(queue, stale_sweep=failing)
        self.addCleanup(queue.stop)
        queue.start(_context(self.store))  # a start after that works: a second worker, asleep until its deadline
        second = queue._worker
        self.assertTrue(queue.ready)
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            release.set()
            self.assertTrue(returned.wait(3), "the stale worker did not return")
        self.assertIsNot(stale, second)
        self.assertTrue(second.is_alive())
        self.assertTrue(queue.ready, "a worker whose generation had ended withdrew ready from the next one")

    def test_a_worker_whose_generation_has_ended_does_not_publish_ready_over_the_next_ones_failure(self) -> None:
        # DS F2, the other half: the stale worker's sweep succeeds after the next worker has reported a failure.
        queue = ApprovalQueue(clock=self.clock, retry_seconds=60.0)  # the second worker pauses after its failure
        queue.attach(self.store, declare_tables=False)
        self.addCleanup(self._stop_worker, queue)
        self.plugin_request(queue=queue)
        stale, release, returned = self._interrupted_launch(queue, stale_sweep=lambda real_sweep: real_sweep())
        self.addCleanup(queue.stop)
        withdrawn, real_wait = threading.Event(), queue._closing.wait

        def failing_sweep():
            raise OSError("synthetic failure of the second worker")

        def closing_wait(timeout=None):
            if threading.current_thread() is not stale and timeout == 60.0:
                withdrawn.set()  # the second worker has withdrawn ready and pauses before it tries again
            return real_wait(timeout)

        queue.start(_context(self.store))
        second = queue._worker
        with mock.patch.object(queue._closing, "wait", closing_wait), \
                self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            original_sweep = queue.sweep  # the patched one of _interrupted_launch

            def sweep():
                if threading.current_thread() is second:
                    return failing_sweep()
                return original_sweep()

            with mock.patch.object(queue, "sweep", sweep):
                queue._wake.set()
                self.assertTrue(withdrawn.wait(3), "the second worker did not report its failure")
                self.assertFalse(queue.ready)
                release.set()
                self.assertTrue(returned.wait(3), "the stale worker did not return")
        self.assertFalse(queue.ready, "a worker whose generation had ended published ready over a failure")


@needs_fakes
class EarlySubscriberTests(_Base):
    def _subscribed(self, queue, shown: list):
        def surface(record) -> None:
            worker = queue._worker
            shown.append((record["approval_id"], worker is not None and worker.ident is not None, queue.ready))
        return queue.subscribe_requested(surface)

    def test_a_surface_subscribed_before_the_start_is_offered_the_backlog_only_once_the_worker_launched(self) -> None:
        approval_id = self.plugin_request()  # DS F1
        queue, shown = self.restarted(), []
        self._subscribed(queue, shown)
        queue.start(_context(self.store))
        self.addCleanup(queue.stop)
        self.assertEqual(shown, [(approval_id, True, True)], "offered before the start had launched its worker")

    def test_a_surface_subscribed_before_the_start_is_offered_nothing_when_the_launch_fails(self) -> None:  # DS F1
        approval_id = self.plugin_request()
        queue, shown = self.restarted(), []
        self._subscribed(queue, shown)
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("synthetic launch failure")):
            with self.assertRaises(RuntimeError):
                queue.start(_context(self.store))
        self.assertEqual(shown, [], "a start whose launch failed offered the backlog")
        with self.store.transaction() as tx:
            self.assertEqual(rows.unshown(tx), [approval_id], "a start whose launch failed marked the backlog shown")
        queue.stop()
        queue.start(_context(self.store))  # a start that works offers it, once
        self.addCleanup(queue.stop)
        self.assertEqual(shown, [(approval_id, True, True)])

    def test_every_surface_subscribed_before_the_start_is_offered_the_backlog(self) -> None:
        approval_id = self.plugin_request()
        queue, first, second = self.restarted(), [], []
        queue.subscribe_requested(lambda record: first.append(record["approval_id"]))
        queue.subscribe_requested(lambda record: second.append(record["approval_id"]))
        queue.start(_context(self.store))
        self.addCleanup(queue.stop)
        self.assertEqual((first, second), ([approval_id], [approval_id]))

    def test_a_surface_subscribed_before_the_start_that_unsubscribes_during_it_is_offered_nothing(self) -> None:
        self.plugin_request()
        queue, shown = self.restarted(), []
        unsubscribe = self._subscribed(queue, shown)
        original = queue.recover

        def recovery():
            unsubscribe()
            return original()

        with mock.patch.object(queue, "recover", recovery):
            queue.start(_context(self.store))
        self.addCleanup(queue.stop)
        self.assertEqual(shown, [])

    def test_a_request_made_during_the_start_reaches_an_earlier_surface_only_once_the_worker_launched(self) -> None:
        first = self.plugin_request()  # DS F1
        queue, shown = self.restarted(), []
        self._subscribed(queue, shown)
        paused, release, errors = threading.Event(), threading.Event(), []
        original = queue.recover

        def recovery():
            paused.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
            return original()

        def start() -> None:
            try:
                queue.start(_context(self.store))
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(type(exc).__name__)

        starter = threading.Thread(target=start, daemon=True)
        with mock.patch.object(queue, "recover", recovery):
            starter.start()
            try:
                self.assertTrue(paused.wait(3))
                second = self.plugin_request(queue=queue)
                while_paused = list(shown)
            finally:
                release.set()
                starter.join(4)
        self.addCleanup(queue.stop)
        self.assertFalse(starter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(while_paused, [], "a request committed during the start was offered before the launch")
        self.assertEqual(sorted(shown), sorted([(first, True, True), (second, True, True)]))


@needs_fakes
class TypedRefusalTests(_Base):
    def test_a_store_failure_in_the_insert_is_a_typed_refusal_and_keeps_no_waiter(self) -> None:  # DS F4
        self.queue._mint = lambda: "apr-store-1"
        with mock.patch.object(rows, "insert", side_effect=OSError("synthetic store failure")):
            with self.assertRaises(ApprovalRefused) as refused:
                self.plugin_request()
        self.assertEqual(refused.exception.code, "store_unavailable")
        self.assertIsInstance(refused.exception.__cause__, OSError)
        self.assertNotIn("apr-store-1", self.queue._done)
        with mock.patch.object(rows, "insert", side_effect=ApprovalRefused("record_invalid")):
            with self.assertRaises(ApprovalRefused) as refused:
                self.plugin_request()
        self.assertEqual(refused.exception.code, "record_invalid")  # a refusal of its own passes as it is
        with mock.patch.object(rows, "insert", side_effect=KeyboardInterrupt("synthetic interruption")):
            with self.assertRaises(KeyboardInterrupt):
                self.plugin_request()
        self.assertNotIn("apr-store-1", self.queue._done)
        with self.store.transaction() as tx:
            self.assertIsNone(rows.load(tx, "apr-store-1"))

    def test_a_display_or_a_timeout_of_the_wrong_kind_is_a_typed_refusal(self) -> None:  # DS F6
        for display in ("abc", [1, 2], [("label", "x")], {"label": "x"}, None):
            with self.subTest(display=repr(display)):
                with self.assertRaises(ApprovalRefused) as refused:
                    self.queue.request("plugin_effect", self.plugin, run_id=self.plugin["run_id"], display=display)
                self.assertEqual(refused.exception.code, "display")
        with self.store.transaction() as tx:
            self.assertEqual(rows.due(tx, 10**15), [])  # nothing was written
        approval_id = self.plugin_request()
        for timeout in ("x", True, float("nan"), float("inf"), [1]):
            with self.subTest(timeout=repr(timeout)):
                with self.assertRaises(ApprovalRefused) as refused:
                    self.queue.wait(approval_id, timeout=timeout)
                self.assertEqual(refused.exception.code, "timeout")
        self.assertIsNone(self.queue.wait(approval_id, timeout=0))
        self.assertIsNone(self.queue.wait(approval_id, timeout=0.0))

    def test_open_in_and_answer_in_refuse_a_principal_that_may_not_approve(self) -> None:  # DS F5
        approval_id = self.plugin_request()
        with self.store.transaction() as tx:
            before = rows.load(tx, approval_id)
        refused = {"code": "scope_refused", "detail": "approve"}
        for principal in ({}, None, {"kind": "plugin", "id": "p1"}, {"kind": "owner"}, {"kind": "owner", "id": ""}):
            with self.subTest(principal=repr(principal)):
                with self.store.transaction() as tx:
                    opened = self.queue.open_in(tx, principal, {"command_id": "o-00000001", "approval_id": approval_id,
                                                                "surface": "cli"})
                self.assertEqual(opened, refused)
                told: list = []
                with self.store.transaction() as tx:
                    answered = self.queue.answer_in(tx, principal, {
                        "command_id": "a-00000001", "approval_id": approval_id, "surface": "cli",
                        "expected_revision": before["revision"], "digest": before["digest"], "answer": "approve"},
                        told.append)
                self.assertEqual((answered, told), (refused, []))
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id), before)
        self.assertEqual(self.approve(approval_id)["state"], "approved")  # an owner still opens and answers it


class DrawerTests(unittest.TestCase):
    @needs_fakes
    def test_the_drawer_refuses_an_engine_session_or_run_that_is_not_a_plain_id(self) -> None:  # DS F7
        effect = _vector_effect("g01-a-tool-call.json")
        drawn = effects.lines_of("engine_tool", effect)
        self.assertIn({"text": "engine: " + effect["engine"], "trust": "trusted"}, drawn)
        for member, value in (("engine", "a" + chr(10) + "b"), ("engine", "a b"), ("engine", None), ("engine", ""),
                              ("session_id", "ses-1" + chr(10) + "x"), ("run_id", "r" + chr(13) + "1"),
                              ("run_id", "z" * 65)):
            with self.subTest(member=member, value=repr(value)):
                with self.assertRaises(EffectRefused) as refused:
                    effects.lines_of("engine_tool", dict(effect, **{member: value}))
                self.assertEqual(refused.exception.code, "effect")


if __name__ == "__main__":
    unittest.main()
