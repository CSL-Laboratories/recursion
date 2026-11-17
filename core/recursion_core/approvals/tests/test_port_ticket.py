"""The driver's ticket at the approvals port (DRIVER-CC's port withdraw, #432; the Liaison's request of Nov 15, 7:46
PM): the port takes the ticket a driver admitted a request with, calls ``publish()`` inside the commit that makes
the approval visible, and opens nothing for a ticket that was withdrawn, refusing ``approval_withdrawn``. A ticket
that has answered True is not asked again; a later call is ``approval_published``.

The module is new in the ticket commit, so it does not import on ``e6deff65`` (the port took no ticket, and
``Ticket`` was not there). Every thread a test starts is a daemon with a bounded join.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.tests.test_queue import Ticket, needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base

RACE_ROUNDS = int(os.environ.get("APPROVALS_TICKET_ROUNDS", "200"))


class _OrderedTicket(Ticket):
    def __init__(self, order: list) -> None:
        super().__init__()
        self._order = order

    def publish(self) -> bool:
        self._order.append("publish")
        return super().publish()


@needs_fakes
class PortTicketTests(_Base):
    def _ask(self, ticket, request_id: str = "toolu_t", queue=None) -> str:
        return (queue or self.queue).ask(self.engine, {"engine_request_id": request_id}, ticket=ticket)

    def _nothing_written(self, approval_id: str) -> None:
        with self.store.transaction() as tx:
            self.assertIsNone(rows.load(tx, approval_id))
            self.assertIsNone(rows.binding_of(tx, approval_id))
            self.assertEqual(rows.unshown(tx), [])
        self.assertEqual(self.queue.list_page({})["approvals"], [])
        self.assertEqual(self.store.events, [])
        self.assertNotIn(approval_id, self.queue._done)

    def test_a_withdrawn_ticket_opens_nothing_and_is_refused_with_its_run(self) -> None:
        shown: list = []
        self.queue.subscribe_requested(lambda held: shown.append(held["approval_id"]))
        self.queue._mint = lambda: "apr-withdrawn-1"
        ticket = Ticket()
        self.assertTrue(ticket.withdraw())  # the driver's worker was closed first
        for attempt in ("first", "a retry with the same ticket"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ApprovalRefused) as refused:
                    self._ask(ticket)
                self.assertEqual((refused.exception.code, refused.exception.detail),
                                 ("approval_withdrawn", self.engine["run_id"]))
                self._nothing_written("apr-withdrawn-1")
        self.assertEqual((ticket.calls, ticket.state, shown), (2, "withdrawn", []))
        restarted = self.restarted()  # nothing for a restart to restore or offer
        restarted.subscribe_requested(lambda held: shown.append(held["approval_id"]))
        restarted.start(types.SimpleNamespace(store=self.store))
        self.addCleanup(restarted.stop)
        self.assertEqual(shown, [])
        self._nothing_written("apr-withdrawn-1")

    def test_a_published_ticket_opens_the_approval_and_cannot_be_withdrawn_after(self) -> None:
        shown: list = []
        self.queue.subscribe_requested(lambda held: shown.append(held["approval_id"]))
        ticket = Ticket()
        approval_id = self._ask(ticket)
        self.assertEqual((ticket.calls, ticket.state), (1, "published"))
        self.assertFalse(ticket.withdraw())
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id)["state"], "pending")
        self.assertEqual(shown, [approval_id])

    def test_publish_is_called_once_inside_the_commit_after_the_record_is_written(self) -> None:
        order: list = []
        real_transaction, real_insert = self.store.transaction, rows.insert

        @contextlib.contextmanager
        def transaction():
            with real_transaction() as tx:
                order.append("begin")
                yield tx
            order.append("committed")

        def insert(tx, record, binding) -> None:
            real_insert(tx, record, binding)
            order.append("insert")

        self.queue.subscribe_requested(lambda held: order.append("told"))
        ticket = _OrderedTicket(order)
        with mock.patch.object(self.store, "transaction", transaction), mock.patch.object(rows, "insert", insert):
            self._ask(ticket)
        self.assertEqual(order[:5], ["begin", "insert", "publish", "committed", "told"])
        self.assertEqual(ticket.calls, 1)

    def test_an_engine_request_without_a_ticket_and_a_plugin_effect_with_one_are_refused(self) -> None:
        self.queue._mint = lambda: "apr-no-ticket-1"

        class NotCallable:
            publish = 7

        for ticket in (None, object(), "ticket", NotCallable()):
            with self.subTest(ticket=repr(ticket)):
                for call in ("ask", "request"):
                    with self.assertRaises(ApprovalRefused) as refused:
                        if call == "ask":
                            self._ask(ticket)
                        else:
                            self.queue.request("engine_tool", self.engine, session_id=self.engine["session_id"],
                                               run_id=self.engine["run_id"], engine_request_id="toolu_t", ticket=ticket)
                    self.assertEqual(refused.exception.code, "ticket")
        self._nothing_written("apr-no-ticket-1")
        with self.assertRaises(ApprovalRefused) as refused:  # a plugin's effect has no driver ticket
            self.queue.request("plugin_effect", self.plugin, run_id=self.plugin["run_id"], ticket=Ticket())
        self.assertEqual(refused.exception.code, "ticket")
        self._nothing_written("apr-no-ticket-1")
        self.assertIsNotNone(self.plugin_request())  # and without one it opens as before

    def test_a_ticket_that_raises_or_answers_other_than_true_opens_nothing(self) -> None:
        self.queue._mint = lambda: "apr-odd-ticket-1"

        class Raising(Ticket):
            def publish(self) -> bool:
                raise RuntimeError("synthetic failure of the driver's ticket")

        class Truthy(Ticket):
            def publish(self):
                return 1

        with self.assertRaises(ApprovalRefused) as refused:
            self._ask(Raising())
        self.assertEqual(refused.exception.code, "ticket")
        self.assertIsInstance(refused.exception.__cause__, RuntimeError)
        self._nothing_written("apr-odd-ticket-1")
        with self.assertRaises(ApprovalRefused) as refused:
            self._ask(Truthy())
        self.assertEqual(refused.exception.code, "approval_withdrawn")
        self._nothing_written("apr-odd-ticket-1")

    def test_a_commit_that_fails_after_the_ticket_published_opens_nothing(self) -> None:
        self.queue._mint = lambda: "apr-failed-commit-1"
        real_transaction = self.store.transaction

        @contextlib.contextmanager
        def failing_commit():
            with real_transaction() as tx:
                yield tx
                raise sqlite3.OperationalError("synthetic commit failure")  # inside: the transaction rolls back

        ticket = Ticket()
        with mock.patch.object(self.store, "transaction", failing_commit):
            with self.assertRaises(ApprovalRefused) as refused:
                self._ask(ticket)
        self.assertEqual(refused.exception.code, "store_unavailable")
        self.assertEqual(ticket.state, "published")  # it stays published; nothing was opened
        self._nothing_written("apr-failed-commit-1")

    def test_a_withdraw_racing_the_request_ends_in_exactly_one_outcome(self) -> None:
        numbers = iter(range(RACE_ROUNDS))
        self.queue._mint = lambda: "apr-race-%d" % next(numbers)
        outcomes = {"published": 0, "withdrawn": 0}
        for number in range(RACE_ROUNDS):
            ticket, start, got = Ticket(), threading.Barrier(2), {}

            def ask(ticket=ticket, start=start, got=got, number=number) -> None:
                start.wait(5)
                try:
                    got["id"] = self._ask(ticket, "toolu_race_%d" % number)
                except ApprovalRefused as refused:
                    got["refused"] = (refused.code, refused.detail)

            def withdraw(ticket=ticket, start=start, got=got) -> None:
                start.wait(5)
                got["withdrew"] = ticket.withdraw()

            other, mine = (ask, withdraw) if number % 2 else (withdraw, ask)
            thread = threading.Thread(target=other, daemon=True)
            thread.start()
            mine()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            approval_id = "apr-race-%d" % number
            with self.store.transaction() as tx:
                held = rows.load(tx, approval_id)
            if ticket.state == "published":
                self.assertEqual((got.get("id"), got["withdrew"], held["state"]), (approval_id, False, "pending"))
            else:
                self.assertEqual(ticket.state, "withdrawn")
                self.assertEqual((got.get("refused"), got["withdrew"], held),
                                 (("approval_withdrawn", self.engine["run_id"]), True, None))
            self.assertEqual(ticket.calls, 1)
            outcomes[ticket.state] += 1
        self.assertEqual(sum(outcomes.values()), RACE_ROUNDS)

    def test_a_retry_after_a_failed_commit_does_not_publish_again(self) -> None:
        # DeepSeek F1: a retry after publish() answered True must not ask again, and must not be told the
        # request was withdrawn. The same ticket on a second queue is the same rule.
        self.queue._mint = lambda: "apr-retry-spent-1"
        real, ticket = self.store.transaction, Ticket()

        @contextlib.contextmanager
        def failing_commit():
            with real() as tx:
                yield tx
                raise sqlite3.OperationalError("synthetic commit failure")

        with mock.patch.object(self.store, "transaction", failing_commit):
            with self.assertRaises(ApprovalRefused) as refused:
                self._ask(ticket)
        self.assertEqual((refused.exception.code, refused.exception.detail),
                         ("store_unavailable", self.engine["run_id"]))
        self.assertIsInstance(refused.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual((ticket.calls, ticket.state), (1, "published"))
        self._nothing_written("apr-retry-spent-1")
        for queue, request_id in ((self.queue, "toolu_retry"), (self.restarted(), "toolu_other")):
            with self.subTest(request_id=request_id):
                with self.assertRaises(ApprovalRefused) as refused:
                    self._ask(ticket, request_id, queue=queue)
                self.assertEqual((refused.exception.code, refused.exception.detail),
                                 ("approval_published", self.engine["run_id"]))
                self.assertEqual(ticket.calls, 1)
                self._nothing_written("apr-retry-spent-1")

    def test_two_callers_on_one_ticket_publish_once(self) -> None:
        # DeepSeek F2: two requests, one ticket. One opens. The other is not told the request was withdrawn,
        # and publish() runs once.
        ticket, start, found, guard = Ticket(), threading.Barrier(2), [], threading.Lock()

        def one(number: int) -> None:
            start.wait(5)
            try:
                approval_id = self._ask(ticket, "toolu_two_%d" % number)
            except ApprovalRefused as refused:
                with guard:
                    found.append((refused.code, refused.detail))
            else:
                with guard:
                    found.append(("id", approval_id))

        threads = [threading.Thread(target=one, args=(number,), daemon=True) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual((ticket.calls, ticket.state), (1, "published"))
        opened = [item for item in found if item[0] == "id"]
        self.assertEqual([item for item in found if item[0] == "approval_published"],
                         [("approval_published", self.engine["run_id"])])
        self.assertEqual(len(opened), 1)
        self.assertFalse(any(item[0] == "approval_withdrawn" for item in found))
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, opened[0][1])["state"], "pending")

    def test_an_interruption_after_publish_answers_true_spends_the_ticket(self) -> None:
        # DeepSeek F3: an interruption after publish() answers True and before the commit leaves no record.
        # The interruption passes as itself. A retry is not told the request was withdrawn, and publish()
        # is not called again.
        self.queue._mint = lambda: "apr-spent-int-1"
        real, ticket = self.store.transaction, Ticket()

        @contextlib.contextmanager
        def interrupt_before_commit():
            with real() as tx:
                yield tx
                raise KeyboardInterrupt("synthetic interruption before the commit")

        with mock.patch.object(self.store, "transaction", interrupt_before_commit):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self._ask(ticket)
        self.assertEqual(type(raised.exception), KeyboardInterrupt)
        self.assertEqual((ticket.calls, ticket.state), (1, "published"))
        self._nothing_written("apr-spent-int-1")
        with self.assertRaises(ApprovalRefused) as refused:
            self._ask(ticket, "toolu_retry")
        self.assertEqual((refused.exception.code, refused.exception.detail),
                         ("approval_published", self.engine["run_id"]))
        self.assertEqual(ticket.calls, 1)

    def test_publish_holds_the_write_transaction_and_the_contract_says_so(self) -> None:
        # DeepSeek F4 and F6: publish() runs while the store's write transaction is held, and the port's text
        # says it must return without waiting, that a spent ticket is not read again, and that it ends once.
        from recursion_core.approvals import queue as approval_queue

        text = approval_queue.__doc__ or ""
        self.assertIn("must return without waiting", text)
        self.assertIn("does not read the ticket again", text)
        self.assertIn("ends exactly once", text)
        self.assertTrue(self.store._serial)
        held, release = threading.Event(), threading.Event()

        class Slow(Ticket):
            def publish(self) -> bool:
                held.set()
                if not release.wait(5):
                    raise RuntimeError("the test did not let publish return")
                return super().publish()

        outcome: dict = {}

        def ask() -> None:
            try:
                outcome["id"] = self._ask(Slow())
            except BaseException as exc:  # noqa: BLE001 - the thread reports it; the test asserts
                outcome["exc"] = exc

        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            acquired = self.store._write.acquire(False)
            if acquired:
                self.store._write.release()
            self.assertFalse(acquired)  # a second transaction cannot begin while publish() has not returned
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("exc", outcome)
        self.assertIn("id", outcome)

    def test_a_raising_publish_attribute_is_a_typed_ticket_refusal(self) -> None:
        # DeepSeek F5: reading publish must not escape as a raw error. An interruption from that read passes.
        self.queue._mint = lambda: "apr-hostile-1"

        class Hostile:
            @property
            def publish(self):
                raise RuntimeError("the driver's object is broken")

        class NoAttrs:
            def __getattribute__(self, name):
                raise RuntimeError("no attributes at all")

        class InterruptAttr:
            @property
            def publish(self):
                raise KeyboardInterrupt("synthetic interruption from the attribute")

        for ticket, cause in ((Hostile(), RuntimeError), (NoAttrs(), RuntimeError)):
            with self.subTest(ticket=type(ticket).__name__):
                with self.assertRaises(ApprovalRefused) as refused:
                    self._ask(ticket)
                self.assertEqual(refused.exception.code, "ticket")
                self.assertIsNone(refused.exception.detail)
                self.assertIsInstance(refused.exception.__cause__, cause)
                self._nothing_written("apr-hostile-1")
        with self.assertRaises(KeyboardInterrupt) as raised:
            self._ask(InterruptAttr())
        self.assertEqual(type(raised.exception), KeyboardInterrupt)
        self._nothing_written("apr-hostile-1")

    def test_an_interruption_from_publish_propagates_and_leaves_nothing(self) -> None:
        # Astra R3 (Sol M13): KeyboardInterrupt and SystemExit from publish() propagate as themselves, the
        # transaction rolls back, and the ticket is not treated as spent, so a later publish() can answer True.
        for kind in (KeyboardInterrupt, SystemExit):
            with self.subTest(kind=kind.__name__):
                self.queue._mint = lambda: "apr-interrupt-1"
                shown: list = []
                unsubscribe = self.queue.subscribe_requested(lambda held: shown.append(held["approval_id"]))
                self.addCleanup(unsubscribe)

                class Boom(Ticket):
                    def publish(self) -> bool:
                        raise kind("synthetic interruption from the driver's ticket")

                with self.assertRaises(kind) as raised:
                    self._ask(Boom())
                self.assertEqual(type(raised.exception), kind)
                self._nothing_written("apr-interrupt-1")
                self.assertEqual(shown, [])

        class ThenTrue(Ticket):
            def __init__(self) -> None:
                super().__init__()
                self.tries = 0

            def publish(self) -> bool:
                self.tries += 1
                if self.tries == 1:
                    raise KeyboardInterrupt("synthetic interruption before an answer")
                return super().publish()

        ticket = ThenTrue()
        with self.assertRaises(KeyboardInterrupt):
            self._ask(ticket)
        self.assertEqual(ticket.calls, 0)  # publish() raised before it answered
        approval_id = self._ask(ticket, "toolu_after")
        self.assertEqual((ticket.calls, ticket.state, ticket.tries), (1, "published", 2))
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id)["state"], "pending")

    def test_a_ticket_without_a_weak_reference_is_still_not_asked_again(self) -> None:
        # A ticket that cannot be referenced weakly is still spent after True: a retry does not ask again.
        self.queue._mint = lambda: "apr-slotted-1"

        class Slotted:
            __slots__ = ("_lock", "state", "calls")

            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.state = "admitted"
                self.calls = 0

            def publish(self) -> bool:
                with self._lock:
                    self.calls += 1
                    if self.state != "admitted":
                        return False
                    self.state = "published"
                    return True

        real, ticket = self.store.transaction, Slotted()

        @contextlib.contextmanager
        def failing_commit():
            with real() as tx:
                yield tx
                raise sqlite3.OperationalError("synthetic commit failure")

        with mock.patch.object(self.store, "transaction", failing_commit):
            with self.assertRaises(ApprovalRefused) as refused:
                self._ask(ticket)
        self.assertEqual(refused.exception.code, "store_unavailable")
        self.assertEqual((ticket.calls, ticket.state), (1, "published"))
        with self.assertRaises(ApprovalRefused) as refused:
            self._ask(ticket, "toolu_slotted_retry")
        self.assertEqual((refused.exception.code, refused.exception.detail),
                         ("approval_published", self.engine["run_id"]))
        self.assertEqual(ticket.calls, 1)


if __name__ == "__main__":
    unittest.main()
