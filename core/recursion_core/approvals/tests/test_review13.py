"""The end every gate and every consumer of the store enforces, and what counts as a running queue, on the
REAL event store.

Each test here is red on ``f60c3c6b`` and green after this round, unless its docstring says it holds what must
stay. Every wait is bounded, so that a broken build fails instead of hanging.

* A gate made again on the same store with a shorter deadline or stall time ends a kept approval at that shorter
  end, and the one function that consumes a record ends it there too: the queue's ``consume`` and the gate's
  ``take`` refuse it alike, in either order. A longer figure made later never brings it back. With two gates
  alive on one store, the shorter end decides for both consumers; once that gate is gone, the remaining gate's
  end decides.
* Whether the queue runs is the queue's own record that it was started and not stopped: a deadline worker that
  ended without a stop is started again by the next start, which recovers nothing and says so.
"""

from __future__ import annotations

import gc
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import START_SAYS, ApprovalQueue
from recursion_core.approvals.tests.test_queue import _vector_effect, needs_fakes
from recursion_core.plugin_security import ApprovalGate, Refused
from recursion_core.plugin_security.tests.support import OPERATOR_TOKEN, Clock, authenticate, effect

try:
    from recursion_core.events.db import EventStore
except ImportError:  # pragma: no cover - a tree without EVENTS' store
    EventStore = None

BOUND_S = 10  # how long a join may take before the test fails instead of hanging
SHORTER = 60  # a figure below the gate's default deadline, in seconds
LONGER = 600  # a figure above it
LATER = 100  # how long after the approval the consumers come: past SHORTER, before the default deadline
MIDDLE = 200  # a second gate's figure: past LATER, before the default deadline

needs_store = unittest.skipUnless(EventStore is not None and os.name == "posix", "EVENTS' store needs a POSIX host")


def _note(intent: str) -> dict:
    return effect(kind="note.post", amount=0, payload={"intent": intent}, run_id="run-1")


def _code(call, *args):
    """``("ok", value)`` or ``("refused", code)`` for a call of the gate or the queue."""
    try:
        return "ok", call(*args)
    except ApprovalRefused as refused:
        return "refused", refused.code
    except Refused as refused:
        return "refused", getattr(refused, "code", refused.args[0])


class _OnTheRealStore(unittest.TestCase):
    """A gate and a queue on one EVENTS store over a real file in a temporary folder of the test's own."""

    def setUp(self) -> None:
        folder = tempfile.mkdtemp(prefix="approvals-review13-")
        self.addCleanup(shutil.rmtree, folder, True)
        self.store = EventStore(os.path.join(folder, "events.sqlite3"))
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.queue = ApprovalQueue(clock=lambda: float(self.clock.now))
        self.queue.attach(self.store)
        self.gate = None
        self.remake()

    def remake(self, **figures) -> None:
        """The gate made again on the same store, as a restart with other figures makes it: the old one is gone."""
        self.gate = None
        gc.collect()
        self.gate = ApprovalGate(lambda *drawn: None, authenticate, self.clock, store=self.store,
                                 declare_tables=False, **figures)

    def held(self, approval_id: str) -> dict:
        with self.store.transaction() as tx:
            return rows.load(tx, approval_id)

    def approved(self, intent: str) -> str:
        approval_id, digest = self.gate.request(_note(intent))
        self.gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        return approval_id

    def consume_through(self, consumer: str, approval_id: str, intent: str):
        if consumer == "queue":
            held = self.held(approval_id)
            return _code(self.queue.consume, approval_id, held["revision"], held["digest"], None)
        return _code(self.gate.take, approval_id, _note(intent))


@needs_fakes
@needs_store
class TheShorterEndReachesTheConsumeTests(_OnTheRealStore):
    def both_orders(self, **figure) -> None:
        queue_first, gate_first = self.approved("queue-first"), self.approved("gate-first")
        self.remake(**figure)
        self.clock.now += LATER
        self.assertEqual(self.consume_through("queue", queue_first, "queue-first"), ("refused", "approval_expired"))
        self.assertEqual(self.held(queue_first)["state"], "expired")
        self.assertEqual(self.consume_through("gate", queue_first, "queue-first"), ("refused", "approval_expired"))
        self.assertEqual(self.consume_through("gate", gate_first, "gate-first"), ("refused", "approval_expired"))
        self.assertEqual(self.consume_through("queue", gate_first, "gate-first")[0], "refused")
        self.assertEqual(self.held(gate_first)["state"], "expired")

    def test_a_shorter_deadline_ends_it_for_both_consumers_in_either_order(self) -> None:
        self.both_orders(ttl_seconds=SHORTER)

    def test_a_shorter_stall_time_ends_it_for_both_consumers_in_either_order(self) -> None:
        self.both_orders(stall_seconds=SHORTER)

    def test_a_longer_figure_never_revives_what_a_shorter_one_ended(self) -> None:
        for shorter, longer in (({"ttl_seconds": SHORTER}, {"ttl_seconds": LONGER}),
                                ({"stall_seconds": SHORTER}, {"ttl_seconds": LONGER, "stall_seconds": LONGER})):
            with self.subTest(shorter=shorter):
                self.remake()
                intent = "longer-" + "-".join(shorter)
                approval_id = self.approved(intent)
                self.remake(**shorter)
                self.clock.now += LATER
                self.assertEqual(self.consume_through("queue", approval_id, intent), ("refused", "approval_expired"))
                self.remake(**longer)
                self.assertEqual(self.consume_through("queue", approval_id, intent)[0], "refused")
                self.assertEqual(self.consume_through("gate", approval_id, intent), ("refused", "approval_expired"))
                self.assertEqual(self.held(approval_id)["state"], "expired")

    def test_inside_the_shorter_end_both_consumers_still_consume(self) -> None:
        """What must stay: before the shorter end comes, each consumer carries its approval out once."""
        through_queue, through_gate = self.approved("queue-in-time"), self.approved("gate-in-time")
        self.remake(ttl_seconds=SHORTER)
        self.clock.now += SHORTER - 1
        self.assertEqual(self.consume_through("queue", through_queue, "queue-in-time")[0], "ok")
        self.assertEqual(self.consume_through("gate", through_gate, "gate-in-time")[0], "ok")
        self.assertEqual([self.held(one)["state"] for one in (through_queue, through_gate)], ["consumed", "consumed"])


def _context(store):
    return types.SimpleNamespace(store=store)


@needs_fakes
@needs_store
class AWorkerThatEndedWithoutAStopTests(_OnTheRealStore):
    def engine_requests(self) -> tuple:
        engine = _vector_effect("g01-a-tool-call.json")
        made = [self.queue.request("engine_tool", engine, session_id=engine["session_id"], run_id=engine["run_id"],
                                   engine_request_id=name) for name in ("toolu_1", "toolu_2")]
        owner = {"kind": "owner", "id": "local"}
        after: list = []
        with self.store.transaction() as tx:
            opened = self.queue.open_in(tx, owner, {"approval_id": made[0], "surface": "cli"})["approval"]
        with self.store.transaction() as tx:
            self.queue.answer_in(tx, owner, {"command_id": "answer-0001", "approval_id": made[0], "surface": "cli",
                                             "expected_revision": opened["revision"], "digest": opened["digest"],
                                             "answer": "approve"}, after.append)
        for fn in after:
            fn()
        with self.store.transaction() as tx:
            binding = rows.binding_of(tx, made[0])
        return made, binding

    def test_a_start_after_the_worker_died_recovers_nothing_and_says_so(self) -> None:
        self.queue.start(_context(self.store))
        self.addCleanup(self.queue.stop)
        (approved, pending), binding = self.engine_requests()
        worker = self.queue._worker

        def interrupted():
            raise SystemExit  # not an ordinary error: the worker's loop lets it through and its thread ends

        with mock.patch.object(self.queue, "sweep", interrupted):
            self.queue._wake.set()
            worker.join(BOUND_S)
        self.assertFalse(worker.is_alive())
        with self.assertLogs("recursion_core.approvals.queue", "WARNING") as logged:
            answer = self.queue.start(_context(self.store))
        self.assertEqual(self.held(pending)["state"], "pending")
        held = self.held(approved)
        self.assertEqual(self.queue.consume(approved, held["revision"], held["digest"], binding)["state"], "consumed")
        self.assertEqual(answer, {"code": "worker_restarted", "says": START_SAYS["worker_restarted"]})
        self.assertTrue(any("without a stop" in line for line in logged.output), logged.output)
        self.assertTrue(self.queue._worker.is_alive())

    def test_a_start_after_a_real_stop_still_recovers(self) -> None:
        """What must stay: a start after a stop ends what the dead drivers left."""
        self.queue.start(_context(self.store))
        (approved, pending), binding = self.engine_requests()
        self.queue.stop()
        answer = self.queue.start(_context(self.store))
        self.addCleanup(self.queue.stop)
        self.assertEqual(answer["code"], "started")
        self.assertEqual(self.held(pending)["state"], "expired")
        held = self.held(approved)
        self.assertEqual(_code(self.queue.consume, approved, held["revision"], held["digest"], binding),
                         ("refused", "binding_mismatch"))


@needs_fakes
@needs_store
class TwoLivingGatesTests(_OnTheRealStore):
    def another_gate(self, **figures):
        return ApprovalGate(lambda *drawn: None, authenticate, self.clock, store=self.store, declare_tables=False,
                            **figures)

    def test_the_shorter_of_two_living_gates_decides_then_the_remaining_one(self) -> None:
        names = ("queue-first", "gate-first", "queue-in-time", "gate-in-time", "queue-late", "gate-late")
        made = {name: self.approved("two-" + name) for name in names}
        self.remake(ttl_seconds=MIDDLE)
        shorter = self.another_gate(ttl_seconds=SHORTER)
        self.clock.now += LATER
        self.assertEqual(self.consume_through("queue", made["queue-first"], "two-queue-first"),
                         ("refused", "approval_expired"))
        self.assertEqual(self.held(made["queue-first"])["state"], "expired")
        self.assertEqual(self.consume_through("gate", made["queue-first"], "two-queue-first"),
                         ("refused", "approval_expired"))
        self.assertEqual(self.consume_through("gate", made["gate-first"], "two-gate-first"),
                         ("refused", "approval_expired"))
        self.assertEqual(self.consume_through("queue", made["gate-first"], "two-gate-first")[0], "refused")
        self.assertEqual(self.held(made["gate-first"])["state"], "expired")
        del shorter
        gc.collect()
        self.assertEqual(self.consume_through("queue", made["queue-in-time"], "two-queue-in-time")[0], "ok")
        self.assertEqual(self.consume_through("gate", made["gate-in-time"], "two-gate-in-time")[0], "ok")
        self.clock.now += MIDDLE - LATER
        self.assertEqual(self.consume_through("queue", made["queue-late"], "two-queue-late"),
                         ("refused", "approval_expired"))
        self.assertEqual(self.consume_through("gate", made["gate-late"], "two-gate-late"),
                         ("refused", "approval_expired"))
        self.assertEqual([self.held(made[name])["state"] for name in names],
                         ["expired", "expired", "consumed", "consumed", "expired", "expired"])


if __name__ == "__main__":
    unittest.main()
