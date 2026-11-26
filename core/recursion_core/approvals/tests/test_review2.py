"""The second outside reads of #400 (two Codex reads of ``bcb11bc6..2fd180e4``), each change taken as a test that
fails on ``2fd180e4``: a start whose thread never launched, the readiness a start publishes, a surface that
subscribes after start, the waiter of a resolved record, a row that is not an approval's shape, and the
command id's grammar. Four are Astra's own round-2 checks taken as they were written (her C7), with a stub or
a name changed; each says so. No test waits on the wall clock except to check that something has NOT
happened yet, and every thread a test starts is a daemon with a bounded join.
"""

from __future__ import annotations

import contextlib
import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals import model
from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base

try:
    from recursion_core.approvals.tests.test_methods import MethodTests, needs_seams
except ImportError:  # pragma: no cover
    MethodTests, needs_seams = None, unittest.skip("SPINE's seam files (#369) are not in this tree")


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


@needs_fakes
class StartAndStopTests(_Base):
    def test_stop_signals_before_it_joins(self) -> None:  # Astra C7 (A04), her check with a stub that has an ident
        seen = []
        queue = self.queue

        class Worker:
            ident = 1

            def is_alive(self) -> bool:
                return True

            def join(self) -> None:
                seen.append((queue._closing.is_set(), queue._wake.is_set()))

        queue._worker = Worker()
        queue.stop()
        self.assertEqual(seen, [(True, True)])
        self.assertIsNone(queue._worker)

    def test_a_start_whose_thread_never_launched_can_be_stopped_and_started_again(self) -> None:  # Astra C1, Sol C3
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("synthetic start failure")):
            with self.assertRaises(RuntimeError):
                self.queue.start(_context(self.store))
        self.assertFalse(self.queue.ready)
        stopped = threading.Thread(target=lambda: (self.queue.stop(), self.queue.stop()), daemon=True)
        stopped.start()
        stopped.join(10)
        self.assertFalse(stopped.is_alive(), "stop waited for a worker that never launched")
        self.assertIsNone(self.queue._worker)
        self.queue.start(_context(self.store))  # the next start runs a worker as usual
        self.assertTrue(self.queue._worker.is_alive())
        self.queue.stop()
        self.assertIsNone(self.queue._worker)

    def test_a_worker_whose_generation_has_ended_returns_at_once(self) -> None:
        # A worker let go by stop (it never launched) that launches late must not run beside a new one.
        calls = []
        real = rows.next_deadline
        with mock.patch.object(rows, "next_deadline", lambda tx: (calls.append(1), real(tx))[1]):
            self.queue._generation = object()
            ended = threading.Thread(target=self.queue._run, args=(object(),), daemon=True)
            ended.start()
            ended.join(10)
        self.assertFalse(ended.is_alive())
        self.assertEqual(calls, [])

    def test_start_does_not_overwrite_a_failure_the_worker_reported(self) -> None:  # Astra C3, Sol C2 (her check)
        failed, release = threading.Event(), threading.Event()
        queue = self.queue
        real_start = threading.Thread.start
        real_next = rows.next_deadline

        def next_deadline(tx):
            if threading.current_thread().name == "approvals-deadlines":
                raise OSError("synthetic worker-only store failure")
            return real_next(tx)

        def retry_wait(timeout=None):
            failed.set()
            release.wait(3)
            return queue._closing.is_set()

        def start_and_observe_failure(worker):
            real_start(worker)
            self.assertTrue(failed.wait(3), "the worker's failure was not reached")

        with mock.patch.object(rows, "next_deadline", next_deadline), \
                mock.patch.object(queue._closing, "wait", retry_wait), \
                mock.patch.object(threading.Thread, "start", start_and_observe_failure):
            try:
                with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
                    queue.start(_context(self.store))
                self.assertFalse(queue.ready, "start overwrote the worker's failure with ready")
            finally:
                queue._closing.set()
                release.set()
                queue.stop()


@needs_fakes
class LateSurfaceTests(_Base):
    def test_a_surface_that_subscribes_after_start_is_offered_what_no_surface_took(self) -> None:  # Astra C2
        approval_id = self.plugin_request()  # no surface yet: nobody takes it
        restarted = self.restarted()
        restarted.start(_context(self.store))
        self.addCleanup(restarted.stop)
        drawn, told = [], threading.Event()
        restarted.subscribe_requested(lambda record: (drawn.append(record["approval_id"]), told.set()))
        self.assertTrue(told.wait(5), "a surface that subscribed after start was offered nothing")
        self.assertEqual(drawn, [approval_id])
        with self.store.transaction() as tx:
            self.assertEqual(rows.unshown(tx), [])  # taken now, so it counts as shown
        again = []
        restarted.subscribe_requested(lambda record: again.append(record["approval_id"]))
        self.assertEqual(again, [])  # a second surface: nothing is left that no surface took

    def test_a_surface_that_subscribes_before_start_is_not_offered_twice_by_the_subscription(self) -> None:
        self.plugin_request()
        restarted = self.restarted()
        drawn = []
        restarted.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        self.assertEqual(drawn, [])  # not started: recovery offers it, the subscription does not
        restarted.start(_context(self.store))
        self.addCleanup(restarted.stop)
        self.assertEqual(len(drawn), 1)

    def test_a_late_offer_goes_only_to_the_surface_that_subscribed(self) -> None:
        approval_id = self.plugin_request()
        restarted = self.restarted()
        earlier = []

        def failing(record) -> None:  # offered at start, takes nothing, so the request stays one nobody took
            earlier.append(record["approval_id"])
            raise ValueError("synthetic surface")

        restarted.subscribe_requested(failing)
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            restarted.start(_context(self.store))
        self.addCleanup(restarted.stop)
        self.assertEqual(earlier, [approval_id])
        late = []
        restarted.subscribe_requested(lambda record: late.append(record["approval_id"]))
        self.assertEqual(late, [approval_id])
        self.assertEqual(earlier, [approval_id])  # the earlier surface is not offered it again by this one

    def test_a_late_offer_that_cannot_read_the_store_keeps_the_subscription(self) -> None:
        self.plugin_request()
        restarted = self.restarted()
        restarted.start(_context(self.store))
        self.addCleanup(restarted.stop)
        drawn = []
        with mock.patch.object(rows, "unshown", side_effect=OSError("synthetic store failure")), \
                self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            restarted.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        approval_id = self.plugin_request(queue=restarted)
        self.assertEqual(drawn, [approval_id])


    def test_a_surface_that_subscribes_after_a_failed_start_is_offered_nothing(self) -> None:
        self.plugin_request()
        restarted = self.restarted()
        with mock.patch.object(rows, "unresolved_engine", side_effect=OSError("synthetic store failure")):
            with self.assertRaises(OSError):
                restarted.start(_context(self.store))
        drawn = []
        restarted.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        self.assertEqual(drawn, [])  # the start failed: nothing runs, so nothing is offered from here

    def test_a_surface_that_subscribes_after_stop_is_offered_nothing(self) -> None:
        self.plugin_request()  # no surface takes it, so it stays one that no surface took
        restarted = self.restarted()
        restarted.start(_context(self.store))
        restarted.stop()
        drawn = []
        restarted.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        self.assertEqual(drawn, [])

@needs_fakes
class WaiterTests(_Base):
    def test_waiting_on_finished_records_keeps_no_registration(self) -> None:  # Astra C6, Sol C4 (her check)
        ids = [self.plugin_request() for _ in range(12)]
        self.clock.now += 301
        self.queue.sweep()
        self.assertEqual(self.queue._done, {})
        for approval_id in ids:
            self.assertEqual(self.queue.wait(approval_id, timeout=0)["state"], "expired")
        self.assertEqual(self.queue._done, {}, "resolved records left waiters behind")

    def test_a_waiter_sharing_a_registration_is_woken_when_another_reads_it_finished(self) -> None:  # Sol C4
        approval_id = self.plugin_request()
        results, waiting = [], threading.Event()
        real_load = rows.load

        def load(tx, aid):
            held = real_load(tx, aid)
            if threading.current_thread().name == "first-waiter":
                waiting.set()
            return held

        with mock.patch.object(rows, "load", load):
            first = threading.Thread(name="first-waiter", daemon=True,
                                     target=lambda: results.append(self.queue.wait(approval_id, timeout=10)))
            first.start()
            self.assertTrue(waiting.wait(5))
        # Resolved in the store with no wake (as if its resolution's notice had not yet run): the second waiter
        # reads it resolved, lets the shared registration go and wakes the first.
        with self.store.transaction() as tx:
            rows.save(tx, dict(rows.load(tx, approval_id), state="expired"), answered=True)
        self.assertEqual(self.queue.wait(approval_id, timeout=0)["state"], "expired")
        first.join(10)
        self.assertFalse(first.is_alive(), "a waiter sharing the registration was not woken")
        self.assertEqual(results[0]["state"], "expired")
        self.assertEqual(self.queue._done, {})

    def test_a_resolution_between_the_waiters_read_and_its_wait_is_not_lost(self) -> None:  # Astra C7 (N27), hers
        approval_id = self.plugin_request()
        restarted = self.restarted()
        restarted.recover()
        snapshot_read, release = threading.Event(), threading.Event()
        original_tx = self.store.transaction
        results = []

        @contextlib.contextmanager
        def paused_transaction():
            with original_tx() as tx:
                yield tx
            if threading.current_thread().name == "snapshot-waiter" and not snapshot_read.is_set():
                snapshot_read.set()
                release.wait(3)

        self.store.transaction = paused_transaction
        waiter = threading.Thread(name="snapshot-waiter", daemon=True,
                                  target=lambda: results.append(restarted.wait(approval_id, timeout=0.3)))
        try:
            waiter.start()
            self.assertTrue(snapshot_read.wait(3))
            self.clock.now += 301
            restarted.sweep()
            release.set()
            waiter.join(4)
            self.assertFalse(waiter.is_alive())
            self.assertIsNotNone(results[0], "a resolution committed during the waiter's registration was lost")
            self.assertEqual(results[0]["state"], "expired")
        finally:
            release.set()
            waiter.join(4)
            self.store.transaction = original_tx


@needs_fakes
class UnreadableRowTests(_Base):
    def test_a_shape_invalid_due_row_holds_back_no_other_deadline(self) -> None:  # Astra C4 (her check)
        ids = iter(("apr-000-bad", "apr-111-good"))
        self.queue._mint = lambda: next(ids)
        bad, good = self.plugin_request(), self.plugin_request()
        with self.store.transaction() as tx:
            tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", ("{}", bad))
        self.clock.now += 301
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            self.queue.sweep()
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, good)["state"], "expired")
            self.assertEqual(rows.due(tx, 10**15), [])
            with self.assertRaises(ApprovalRefused) as refused:
                rows.load(tx, bad)
        self.assertEqual(refused.exception.code, "record_invalid")

    def test_a_row_that_fails_otherwise_holds_back_no_other_and_its_failure_is_raised(self) -> None:
        ids = iter(("apr-000-fails", "apr-111-good"))
        self.queue._mint = lambda: next(ids)
        failing, good = self.plugin_request(), self.plugin_request()
        real = model.expire_if_due

        def expire_if_due(record, at):
            if record["approval_id"] == failing:
                raise KeyError("synthetic")
            return real(record, at)

        self.clock.now += 301
        with mock.patch.object(model, "expire_if_due", expire_if_due), \
                self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            with self.assertRaises(KeyError):
                self.queue.sweep()
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, good)["state"], "expired")
            self.assertEqual(rows.load(tx, failing)["state"], "pending")  # not set aside: it can be tried again

    def test_non_object_rows_are_record_invalid_at_load(self) -> None:  # Astra C7 (N24), her check
        approval_id = self.plugin_request()
        for encoded in ("null", "[]", "42", '"plain string"', "{}", '{"approval_id": "apr-x"}'):
            with self.subTest(encoded=encoded):
                with self.store.transaction() as tx:
                    tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", (encoded, approval_id))
                    with self.assertRaises(ApprovalRefused) as refused:
                        rows.load(tx, approval_id)
                    self.assertEqual(refused.exception.code, "record_invalid")

    def test_pages_go_on_across_pages_of_unreadable_rows(self) -> None:  # Astra C7 (N31), her check
        ids = iter(("apr-000-bad", "apr-111-bad", "apr-222-good"))
        self.queue._mint = lambda: next(ids)
        bad1, bad2, good = self.plugin_request(), self.plugin_request(), self.plugin_request()
        with self.store.transaction() as tx:
            for approval_id in (bad1, bad2):
                tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", ("{", approval_id))
        with self.assertLogs("recursion_core.approvals.store", level="ERROR"):
            first = self.queue.list_page({"limit": 1})
            self.assertEqual(first, {"code": "ok", "approvals": [], "next_after": bad1})
            second = self.queue.list_page({"limit": 1, "after": first["next_after"]})
            self.assertEqual(second, {"code": "ok", "approvals": [], "next_after": bad2})
        third = self.queue.list_page({"limit": 1, "after": second["next_after"]})
        self.assertEqual([row["approval_id"] for row in third["approvals"]], [good])
        self.assertNotIn("next_after", third)


@needs_seams
class CommandIdTests(unittest.TestCase):
    if MethodTests is not None:
        setUp = MethodTests.setUp
        call = MethodTests.call
        _cid = MethodTests._cid
        open = MethodTests.open
        answer = MethodTests.answer

    BAD = ("a" * 129, "short", "bad id 1", "bad/id/1", "bad-id-1\n", "\u00e9tait-un-id")

    def test_open_holds_a_fresh_command_id_to_its_grammar(self) -> None:  # Astra C5, Sol C1
        for command_id in self.BAD:
            with self.subTest(command_id=repr(command_id)):
                before = dict(self.store.receipts)
                self.assertEqual(self.open(command_id=command_id), {"code": "request_invalid", "detail": "command_id"})
                self.assertEqual(dict(self.store.receipts), before)
        self.assertEqual(self.open(command_id="a" * 128)["code"], "ok")  # the longest it may be
        self.assertEqual(self.open(command_id="A.b_c:d-")["code"], "ok")  # the shortest, every allowed sign

    def test_answer_holds_a_fresh_command_id_to_its_grammar_and_changes_nothing(self) -> None:  # Sol C1
        shown = self.open()["approval"]
        events = len(self.store.events)
        for command_id in self.BAD:
            with self.subTest(command_id=repr(command_id)):
                before = dict(self.store.receipts)
                self.assertEqual(self.answer(shown, command_id=command_id),
                                 {"code": "request_invalid", "detail": "command_id"})
                self.assertEqual(dict(self.store.receipts), before)
        self.assertEqual(len(self.store.events), events)
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, self.approval_id)["state"], "pending")
        self.assertEqual(self.answer(shown)["approval"]["state"], "approved")


if __name__ == "__main__":
    unittest.main()
