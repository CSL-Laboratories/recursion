"""The integration sweep of #400 against master (s6-int-400, confirmed by the triage s7): the queue composed with
master's real seams, and the two points of the queue it found beside that. Each test fails at ``da6fbf63``:

* F1 (P1): master's own ``compose()``, ``open_store()``, ``start_services()`` raised ``AttributeError`` (``None``
  has no ``register_tables``): master has no EVENTS registrant, so no event store is composed, and the approval
  service's start attached the ``None`` store. S13 allows that composition (``ServiceContext.store`` is "the event
  store's transaction helper, or None when no event store is composed") and fails the whole composition when a
  service's start raises, so the service starts with nothing to run and stays not ready; a request is refused
  ``store_absent``.
* F2 (P2): EVENTS' ``CommitUnknown`` after a COMMIT that was made durable came back as ``ApprovalRefused``
  ``store_unavailable`` ("nothing was written"); it passes as it is.
* F3 (P2): a surface that subscribed during a second start (the queue running from the first) was offered the
  backlog at once, before that start's recovery had run, and so even when the recovery failed.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import Clock, _vector_effect, needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base
from recursion_core.approvals.tests.test_review9 import _context, _settle

try:
    from recursion_core.approvals.hooks import queue_of, register
    from recursion_core.control import hooks as seams
except ImportError:  # pragma: no cover
    seams = None


@needs_fakes
@unittest.skipIf(seams is None, "SPINE's hooks are not in this tree")
class AbsentEventStoreTests(unittest.TestCase):
    def _composed_without_a_store(self, hooks) -> None:
        effect = _vector_effect("g03-a-plugin-effect.json")
        with tempfile.TemporaryDirectory(prefix="approvals-compose-") as folder:
            state_dir = pathlib.Path(folder)
            store = hooks.open_store(state_dir)
            if store is not None:
                self.skipTest("an event store is composed in this tree")
            context = seams.ServiceContext(store=store, state_dir=state_dir)
            try:
                hooks.start_services(context)  # raised AttributeError at da6fbf63
                queue = queue_of(hooks)
                self.assertIsNone(queue.store)
                self.assertFalse(queue.ready)
                self.assertEqual(queue._workers, [])
                with self.assertRaises(ApprovalRefused) as refused:
                    queue.request("plugin_effect", effect, run_id=effect["run_id"])
                self.assertEqual(refused.exception.code, "store_absent")
            finally:
                hooks.stop_services(context)
            self.assertFalse(queue.ready)

    def test_masters_own_composition_starts_its_services_with_no_event_store(self) -> None:  # F1
        self._composed_without_a_store(seams.compose())  # every installed registrant, as the daemon will

    def test_the_lanes_registrant_alone_starts_with_no_event_store(self) -> None:  # F1, the sweep's red test
        hooks = seams.Hooks()
        register(hooks)
        hooks.freeze()
        self._composed_without_a_store(hooks)

    def test_a_queue_started_with_no_store_starts_once_a_store_is_given(self) -> None:  # F1, no state is kept
        from recursion_core.control.tests.test_seams import FakeStore

        store = FakeStore()
        self.addCleanup(store.close)  # cleanups run last first: the queue's worker is gone before this
        queue = ApprovalQueue(clock=Clock())
        self.addCleanup(_settle, queue)
        queue.start(_context(None))
        self.assertFalse(queue.ready)
        queue.stop()
        queue.start(_context(store))
        self.assertIs(queue.store, store)
        self.assertTrue(queue.ready)
        queue.stop()
        self.assertFalse(queue.ready)
        self.assertEqual(queue._workers, [])


@needs_fakes
class CommitOutcomeUnknownTests(unittest.TestCase):
    def test_a_request_whose_commit_outcome_is_unknown_is_not_a_refusal(self) -> None:  # F2
        try:
            import fcntl  # noqa: F401 - the events store takes its writer lock with it
        except ImportError:
            self.skipTest("the events store runs where fcntl does")
        from recursion_core.events.db import CommitUnknown, EventStore

        clock = Clock()
        effect = _vector_effect("g03-a-plugin-effect.json")
        with tempfile.TemporaryDirectory(prefix="approvals-compose-") as folder:
            path = pathlib.Path(folder) / "events.db"
            store = EventStore(path, clock=clock)
            queue = ApprovalQueue(clock=clock, mint=lambda: "apr-unknown")
            queue.attach(store)
            real_commit = store._commit

            def durable_then_unknown(tx):  # the COMMIT is made, and its acknowledgement is lost
                real_commit(tx)
                store._fail(OSError("synthetic lost commit acknowledgement"), "commit_outcome_unknown")
                raise CommitUnknown("commit_outcome_unknown")

            raised = None
            try:
                with mock.patch.object(store, "_commit", durable_then_unknown):
                    try:
                        queue.request("plugin_effect", effect, run_id=effect["run_id"])
                    except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                        raised = exc
            finally:
                store.close()
            reopened = EventStore(path, clock=clock)
            try:
                reopened.register_tables(rows.LANE, rows.VERSION, rows.DDL)
                with reopened.transaction() as tx:
                    self.assertEqual(rows.load(tx, "apr-unknown")["state"], "pending")  # it was written
            finally:
                reopened.close()
            self.assertIsInstance(raised, CommitUnknown, "a request that may be written was refused: %r" % raised)

    def test_any_other_store_failure_is_still_store_unavailable(self) -> None:  # F2, what stays
        from recursion_core.control.tests.test_seams import FakeStore

        store = FakeStore()
        self.addCleanup(store.close)
        queue = ApprovalQueue(clock=Clock())
        queue.attach(store)
        effect = _vector_effect("g03-a-plugin-effect.json")
        with mock.patch.object(rows, "insert", side_effect=OSError("synthetic failed write")):
            with self.assertRaises(ApprovalRefused) as refused:
                queue.request("plugin_effect", effect, run_id=effect["run_id"])
        self.assertEqual(refused.exception.code, "store_unavailable")
        self.assertIsInstance(refused.exception.__cause__, OSError)


@needs_fakes
class SecondStartTests(_Base):
    def _second_start(self, recover_fails: bool) -> tuple:
        queue = self.queue
        self.addCleanup(_settle, queue)
        queue.start(_context(self.store))
        approval_id = self.plugin_request()  # no surface takes it: unshown
        drawn, during = [], []
        real_recover = queue.recover

        def recover():
            queue.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
            during.extend(drawn)
            if recover_fails:
                raise RuntimeError("synthetic failed second recovery")
            return real_recover()

        with mock.patch.object(queue, "recover", side_effect=recover):
            if recover_fails:
                with self.assertRaises(RuntimeError):
                    queue.start(_context(self.store))
            else:
                queue.start(_context(self.store))
        return approval_id, drawn, during

    def test_a_surface_subscribing_during_a_failed_second_start_is_offered_nothing(self) -> None:  # F3
        approval_id, drawn, during = self._second_start(recover_fails=True)
        self.assertEqual(during, [], "a surface was offered the backlog while a start was still recovering")
        self.assertEqual(drawn, [])
        with self.store.transaction() as tx:
            self.assertIn(approval_id, rows.unshown(tx))

    def test_a_surface_subscribing_during_a_second_start_is_offered_by_that_start(self) -> None:  # F3
        approval_id, drawn, during = self._second_start(recover_fails=False)
        self.assertEqual(during, [])
        self.assertEqual(drawn, [approval_id])

    def test_a_surface_subscribing_once_the_queue_runs_is_offered_at_once(self) -> None:  # F3, what stays
        queue = self.queue
        self.addCleanup(_settle, queue)
        queue.start(_context(self.store))
        approval_id = self.plugin_request()
        drawn: list = []
        queue.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        self.assertEqual(drawn, [approval_id])


if __name__ == "__main__":
    unittest.main()
