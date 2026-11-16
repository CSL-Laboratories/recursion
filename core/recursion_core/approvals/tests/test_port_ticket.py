"""The driver's ticket at the approvals port (DRIVER-CC's port withdraw, #432; the Liaison's request of Nov 15, 7:46
PM): the port takes the ticket a driver admitted a request with, calls ``publish()`` exactly once inside the commit
that makes the approval visible, and opens nothing for a ticket that was withdrawn, refusing ``approval_withdrawn``.

Every test here fails on ``e6deff65``, where the port took no ticket. Every thread a test starts is a daemon with a
bounded join.
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


if __name__ == "__main__":
    unittest.main()
