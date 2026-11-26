"""The gate and the queue on ONE event store.

Each test here is red on ``b0746805`` and green after this round, unless its docstring says it holds what must
stay. Waiters and racing writers meet on latches (``threading.Event``), never on timing alone; every wait is
bounded so that a broken build fails instead of hanging.

* The deadline and the veto are checked where a record is consumed, in the transaction that marks it
  consumed, in the one place the queue's ``consume`` and the gate's ``take`` both go through.
* A waiter of the queue wakes when the gate (or another queue on the same store) settles its approval.
* A second start while the queue runs ends no engine request and retires no binding, and says so.
"""

from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import _vector_effect, needs_fakes
from recursion_core.plugin_security import ApprovalGate, Refused
from recursion_core.plugin_security import approval as gate_module
from recursion_core.plugin_security.tests.support import OPERATOR_TOKEN, Clock, authenticate, effect

try:
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover - the seams are not in this tree
    FakeStore = None

BOUND_S = 10  # how long a latch or a join may take before the test fails instead of hanging
DEADLINE = gate_module.APPROVAL_DEADLINE_SECONDS


def _note(intent: str = "a", run_id: str = "run-1") -> dict:
    return effect(kind="note.post", amount=0, payload={"intent": intent}, run_id=run_id)


def _code(call, *args):
    """``("ok", value)`` or ``("refused", code)`` for a call of the gate or the queue."""
    try:
        return "ok", call(*args)
    except ApprovalRefused as refused:
        return "refused", refused.code
    except Refused as refused:
        return "refused", getattr(refused, "code", refused.args[0])


class _Shared(unittest.TestCase):
    """A gate and a queue on one store, as the gate's own text describes them."""

    serial = True

    def setUp(self) -> None:
        self.store = FakeStore(serial=self.serial)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.queue = ApprovalQueue(clock=lambda: float(self.clock.now))
        self.queue.attach(self.store)
        self.drawn: list = []
        self.gate = ApprovalGate(lambda *drawn: self.drawn.append(drawn), authenticate, self.clock,
                                 store=self.store, declare_tables=False)

    def held(self, approval_id: str) -> dict:
        with self.store.transaction() as tx:
            return rows.load(tx, approval_id)

    def approved(self, intent: str = "a") -> tuple:
        approval_id, digest = self.gate.request(_note(intent))
        self.gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        return approval_id, digest

    def consume_through(self, consumer: str, approval_id: str, intent: str = "a"):
        if consumer == "queue":
            held = self.held(approval_id)
            return _code(self.queue.consume, approval_id, held["revision"], held["digest"], None)
        return _code(self.gate.take, approval_id, _note(intent))

    def approved_and_vetoed(self) -> tuple:
        """The same effect asked for twice, before any veto: the first approved, the second vetoed."""
        first, first_digest = self.gate.request(_note())
        second, second_digest = self.gate.request(_note())
        self.gate.decide(first, first_digest, OPERATOR_TOKEN, True)
        self.gate.decide(second, second_digest, OPERATOR_TOKEN, False)
        return first, second


@needs_fakes
class NoConsumerCarriesOutAnEndedApprovalTests(_Shared):
    def test_the_queue_refuses_an_approval_past_its_deadline(self) -> None:
        approval_id, _ = self.approved()
        self.clock.now += DEADLINE + 1
        self.assertEqual(self.consume_through("queue", approval_id), ("refused", "approval_expired"))
        self.assertEqual(self.held(approval_id)["state"], "expired")  # the expiry that was due is kept

    def test_the_queue_refuses_an_approval_after_a_veto_of_the_same_effect(self) -> None:
        first, _ = self.approved_and_vetoed()
        self.assertEqual(self.consume_through("queue", first), ("refused", "approval_vetoed"))
        self.assertEqual(self.held(first)["state"], "approved")  # nothing was carried out

    def test_the_gate_still_refuses_both(self) -> None:
        """What must stay: the gate's take refuses the same two cases, typed as before."""
        approval_id, _ = self.approved()
        self.clock.now += DEADLINE + 1
        self.assertEqual(self.consume_through("gate", approval_id), ("refused", "approval_expired"))
        first, _ = self.approved_and_vetoed()
        self.assertEqual(self.consume_through("gate", first), ("refused", "approval_vetoed"))

    def test_at_the_deadline_both_refuse_and_a_second_before_both_consume(self) -> None:
        for consumer in ("queue", "gate"):
            with self.subTest(consumer=consumer, at="the deadline"):
                approval_id, _ = self.approved("at-" + consumer)
                self.clock.now += DEADLINE
                self.assertEqual(self.consume_through(consumer, approval_id, "at-" + consumer),
                                 ("refused", "approval_expired"))
            with self.subTest(consumer=consumer, at="one second before"):
                approval_id, _ = self.approved("before-" + consumer)
                self.clock.now += DEADLINE - 1
                outcome = self.consume_through(consumer, approval_id, "before-" + consumer)
                self.assertEqual(outcome[0], "ok", outcome)
                self.assertEqual(self.held(approval_id)["state"], "consumed")

    def test_a_consumed_record_is_refused_by_the_other_consumer(self) -> None:
        """What must stay: one consumption, whichever consumer comes first."""
        approval_id, _ = self.approved()
        self.assertEqual(self.consume_through("queue", approval_id)[0], "ok")
        self.assertEqual(self.consume_through("gate", approval_id), ("refused", "approval_replayed"))


@needs_fakes
class AVetoBetweenTheReadAndTheWriteTests(_Shared):
    """A store that runs two transactions at once: the veto commits after the consume has read the record and
    before it writes. The consume must not carry it out."""

    serial = False

    def test_a_veto_that_lands_inside_a_consume_stops_it(self) -> None:
        first, first_digest = self.gate.request(_note())
        second, second_digest = self.gate.request(_note())
        self.gate.decide(first, first_digest, OPERATOR_TOKEN, True)
        held = self.held(first)
        read, vetoed = threading.Event(), threading.Event()
        consumer: list = []
        real_load = rows.load

        def load(tx, approval_id):
            found = real_load(tx, approval_id)
            if threading.current_thread() is consumer[0] and not read.is_set():
                read.set()  # the consume has read the approved record
                if not vetoed.wait(BOUND_S):
                    raise RuntimeError("the veto never came")
            return found

        outcome: list = []
        thread = threading.Thread(target=lambda: outcome.append(
            _code(self.queue.consume, first, held["revision"], held["digest"], None)), daemon=True)
        consumer.append(thread)
        with mock.patch.object(rows, "load", load):
            thread.start()
            self.assertTrue(read.wait(BOUND_S))
            self.assertEqual(self.gate.decide(second, second_digest, OPERATOR_TOKEN, False), "vetoed")
            vetoed.set()
            thread.join(BOUND_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [("refused", "approval_vetoed")])
        self.assertEqual(self.held(first)["state"], "approved")


@needs_fakes
class AWaiterWakesWhenTheGateSettlesTests(_Shared):
    def waiters(self, approval_id: str) -> tuple:
        """Two waiters on the queue, one with a timeout and one with none, both past their read of the
        pending record before anything settles it."""
        results: dict = {}
        ready = {"bounded": threading.Event(), "forever": threading.Event()}
        names: dict = {}
        real_load = rows.load

        def load(tx, wanted):
            found = real_load(tx, wanted)
            name = names.get(threading.current_thread())
            if name is not None and found is not None and found["state"] == "pending":
                ready[name].set()
            return found

        patch = mock.patch.object(rows, "load", load)
        patch.start()
        self.addCleanup(patch.stop)
        threads = {
            "bounded": threading.Thread(target=lambda: results.__setitem__(
                "bounded", self.queue.wait(approval_id, timeout=BOUND_S)), daemon=True),
            "forever": threading.Thread(target=lambda: results.__setitem__(
                "forever", self.queue.wait(approval_id)), daemon=True),
        }
        for name, thread in threads.items():
            names[thread] = name
            thread.start()
        for event in ready.values():
            self.assertTrue(event.wait(BOUND_S))
        self.addCleanup(self._release, threads)
        return threads, results

    def _release(self, threads) -> None:
        """A waiter that a broken build left blocked is let go, so the test ends."""
        with self.queue._lock:
            events = list(self.queue._done.values())
        for event in events:
            event.set()
        for thread in threads.values():
            thread.join(BOUND_S)

    def settled(self, threads, results, state: str) -> None:
        for name, thread in threads.items():
            thread.join(BOUND_S if name == "forever" else BOUND_S + 1)
        self.assertEqual({name: thread.is_alive() for name, thread in threads.items()},
                         {"bounded": False, "forever": False})
        self.assertEqual({name: (record or {}).get("state") for name, record in results.items()},
                         {"bounded": state, "forever": state})

    def test_an_approval_through_the_gate_wakes_both_waiters(self) -> None:
        approval_id, digest = self.gate.request(_note())
        threads, results = self.waiters(approval_id)
        self.gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        self.settled(threads, results, "approved")

    def test_a_veto_through_the_gate_wakes_both_waiters(self) -> None:
        approval_id, digest = self.gate.request(_note())
        threads, results = self.waiters(approval_id)
        self.gate.decide(approval_id, digest, OPERATOR_TOKEN, False)
        self.settled(threads, results, "vetoed")

    def test_an_expiry_the_gate_writes_wakes_both_waiters(self) -> None:
        approval_id, digest = self.gate.request(_note())
        threads, results = self.waiters(approval_id)
        self.clock.now += DEADLINE
        self.assertEqual(_code(self.gate.decide, approval_id, digest, OPERATOR_TOKEN, True),
                         ("refused", "approval_expired"))
        self.settled(threads, results, "expired")

    def test_an_answer_through_another_queue_on_the_store_wakes_both_waiters(self) -> None:
        other = ApprovalQueue(clock=lambda: float(self.clock.now))
        other.attach(self.store, declare_tables=False)
        plugin = _vector_effect("g03-a-plugin-effect.json")
        approval_id = self.queue.request("plugin_effect", plugin, run_id=plugin["run_id"])
        threads, results = self.waiters(approval_id)
        owner = {"kind": "owner", "id": "local"}
        after: list = []
        with self.store.transaction() as tx:
            opened = other.open_in(tx, owner, {"approval_id": approval_id, "surface": "cli"})["approval"]
        with self.store.transaction() as tx:
            other.answer_in(tx, owner, {"command_id": "answer-0001", "approval_id": approval_id, "surface": "cli",
                                        "expected_revision": opened["revision"], "digest": opened["digest"],
                                        "answer": "deny"}, after.append)
        for fn in after:
            fn()
        self.settled(threads, results, "denied")


def _context(store):
    return types.SimpleNamespace(store=store)


@needs_fakes
class ASecondStartEndsNothingTests(_Shared):
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

    def test_a_second_start_while_running_ends_no_request_and_retires_no_binding(self) -> None:
        self.queue.start(_context(self.store))
        self.addCleanup(self.queue.stop)
        (approved, pending), binding = self.engine_requests()
        answer = self.queue.start(_context(self.store))
        self.assertEqual(self.held(pending)["state"], "pending")
        held = self.held(approved)
        self.assertEqual(self.queue.consume(approved, held["revision"], held["digest"], binding)["state"], "consumed")
        self.assertEqual(answer["code"], "already_running")
        self.assertIn("already running", answer["says"])

    def test_a_start_after_a_real_stop_still_recovers(self) -> None:
        """What must stay: a start with no worker of its own alive ends what the dead drivers left."""
        self.queue.start(_context(self.store))
        (approved, pending), binding = self.engine_requests()
        self.queue.stop()
        self.queue.start(_context(self.store))
        self.addCleanup(self.queue.stop)
        self.assertEqual(self.held(pending)["state"], "expired")
        held = self.held(approved)
        self.assertEqual(_code(self.queue.consume, approved, held["revision"], held["digest"], binding),
                         ("refused", "binding_mismatch"))


if __name__ == "__main__":
    unittest.main()
