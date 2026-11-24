"""The approval gate given the event store: approvals that last across a restart and expire, one effect each.

The store is the one shared fake of the event store (``control/tests/test_seams.py``), the store the approval
queue's own tests use; a restart is a new gate made on the same store, as a new daemon finds it. Time is a fake
clock moved by hand: no test waits on the wall clock, and none starts a thread or a process.

The three mutants at the end are run by the suite itself: each one is a gate with one rule taken out, and the
test it names must fail against it.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from recursion_core.approvals.model import principal_refusal
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.plugin_security import approval as gate_module
from recursion_core.plugin_security import ApprovalGate, Refused
from recursion_core.plugin_security.tests.support import (
    AGENT_TOKEN, OPERATOR_TOKEN, PLUGIN_TOKEN, Clock, authenticate, effect,
)

try:
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover - the seams are not in this tree
    FakeStore = None

needs_store = unittest.skipUnless(FakeStore is not None, "the shared fake of the event store is not in this tree")


def _note(**payload) -> dict:
    return effect(kind="note.post", amount=0, payload=payload or {"intent": "a"})


def _rows(store) -> dict:
    with store.transaction() as tx:
        return {approval_id: (state, shown) for approval_id, state, shown
                in tx.execute("SELECT approval_id, state, shown FROM approvals_records")}


@needs_store
class KeptGateTests(unittest.TestCase):
    Gate = ApprovalGate  # a mutant below replaces it

    def setUp(self) -> None:
        self.store = FakeStore()
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.drawn: list = []
        self.declared = False
        self.gate = self.restart()

    def restart(self, **figures) -> ApprovalGate:
        """A gate made on the same store, as a daemon that starts again finds it."""
        gate = self.Gate(lambda *drawn: self.drawn.append(drawn), authenticate, self.clock, store=self.store,
                         declare_tables=not self.declared, **figures)
        self.declared = True
        return gate

    def refusal(self, call, *args) -> str:
        with self.assertRaises(Refused) as caught:
            call(*args)
        return caught.exception.code

    def approved(self, gate, eff) -> str:
        approval_id, digest = gate.request(eff)
        self.assertEqual(gate.decide(approval_id, digest, OPERATOR_TOKEN, True), "approved")
        return approval_id

    def test_a_request_is_kept_before_it_is_drawn(self) -> None:
        seen = []
        gate = self.Gate(lambda approval_id, digest, raw: seen.append(_rows(self.store).get(approval_id)),
                         authenticate, self.clock, store=self.store, declare_tables=False)
        approval_id, _ = gate.request(_note())
        self.assertEqual(seen, [("pending", 0)])  # committed when drawn
        self.assertEqual(_rows(self.store)[approval_id], ("pending", 1))  # shown once the draw took it

    def test_restart_between_request_and_answer(self) -> None:
        approval_id, digest = self.gate.request(_note())
        after = self.restart()
        self.assertEqual(after.decide(approval_id, digest, OPERATOR_TOKEN, True), "approved")
        self.assertEqual(after.take(approval_id, _note()), digest)

    def test_restart_after_approve_before_take(self) -> None:
        approval_id = self.approved(self.gate, _note())
        after = self.restart()
        after.take(approval_id, _note())
        self.assertEqual(_rows(self.store)[approval_id][0], "consumed")
        self.assertEqual(self.refusal(self.restart().take, approval_id, _note()), "approval_replayed")

    def test_deadline_passed_while_the_daemon_was_down(self) -> None:
        approval_id, digest = self.gate.request(_note())
        self.clock.now += gate_module.APPROVAL_DEADLINE_SECONDS  # the deadline comes with no gate running
        after = self.restart()
        self.assertEqual(self.refusal(after.decide, approval_id, digest, OPERATOR_TOKEN, True), "approval_expired")
        self.assertEqual(self.refusal(after.take, approval_id, _note()), "approval_expired")
        self.assertEqual(_rows(self.store)[approval_id][0], "expired")
        answered = [event["payload"]["state"] for event in self.store.events if event["type"] == "approval.answered"]
        self.assertEqual(answered, ["expired"])

    def test_an_approval_unused_at_its_deadline_is_never_taken(self) -> None:
        approval_id = self.approved(self.gate, _note())
        self.clock.now += gate_module.APPROVAL_DEADLINE_SECONDS
        self.assertEqual(self.refusal(self.restart().take, approval_id, _note()), "approval_expired")

    def test_the_same_answer_twice(self) -> None:
        approval_id, digest = self.gate.request(_note())
        self.gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        self.assertEqual(self.refusal(self.gate.decide, approval_id, digest, OPERATOR_TOKEN, True),
                         "approval_already_decided")
        self.assertEqual(self.refusal(self.restart().decide, approval_id, digest, OPERATOR_TOKEN, False),
                         "approval_already_decided")
        self.gate.take(approval_id, _note())
        self.assertEqual(self.refusal(self.gate.take, approval_id, _note()), "approval_replayed")

    def test_a_second_effect_digest_with_the_first_approval(self) -> None:
        approval_id = self.approved(self.gate, _note(intent="a"))
        after = self.restart()
        self.assertEqual(self.refusal(after.take, approval_id, _note(intent="b")), "approval_digest_mismatch")
        after.take(approval_id, _note(intent="a"))  # the one effect it was drawn for is still covered, once

    def test_a_veto_then_a_restart_then_the_same_digest(self) -> None:
        approval_id, digest = self.gate.request(_note())
        sibling, _ = self.gate.request(_note())
        self.assertEqual(self.gate.decide(approval_id, digest, OPERATOR_TOKEN, False), "vetoed")
        after = self.restart()
        self.assertTrue(after.is_vetoed(digest))
        self.assertEqual(self.refusal(after.request, _note()), "approval_vetoed")
        self.assertEqual(self.refusal(after.decide, sibling, digest, OPERATOR_TOKEN, True), "approval_vetoed")
        after.request(_note(intent="another intent"))  # a new intent is a new digest

    def test_a_torn_write_of_the_store(self) -> None:
        approval_id, digest = self.gate.request(_note())
        with self.store.transaction() as tx:
            tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", ('{"approval_id": "', approval_id))
        after = self.restart()
        self.assertEqual(self.refusal(after.decide, approval_id, digest, OPERATOR_TOKEN, True), "record_invalid")
        self.assertEqual(self.refusal(after.take, approval_id, _note()), "record_invalid")
        self.assertEqual(_rows(self.store)[approval_id][0], "pending")  # never approved
        self.assertIsNone(after.run_state("run-1"))  # a record that cannot be read says nothing

    def test_a_run_waits_then_stalls_and_never_proceeds(self) -> None:
        self.assertIsNone(self.gate.run_state("run-1"))
        approval_id, _ = self.gate.request(_note())
        waiting = self.restart().run_state("run-1")
        self.assertEqual(waiting, {"state": "waiting_approval", "approval_id": approval_id,
                                   "says": gate_module.SAYS["waiting_approval"]})
        self.clock.now += gate_module.APPROVAL_DEADLINE_SECONDS
        after = self.restart()
        self.assertEqual(after.run_state("run-1")["state"], "stalled")
        self.assertEqual(self.refusal(after.take, approval_id, _note()), "approval_expired")
        self.assertEqual(after.run_state("run-1")["says"], gate_module.SAYS["stalled"])

    def test_an_answered_run_does_not_wait(self) -> None:
        approval_id = self.approved(self.gate, _note())
        self.assertIsNone(self.gate.run_state("run-1"))
        self.gate.take(approval_id, _note())
        self.assertIsNone(self.restart().run_state("run-1"))

    def test_the_figures_are_named_values_a_caller_can_replace(self) -> None:
        self.assertEqual((gate_module.APPROVAL_DEADLINE_SECONDS, gate_module.TOMBSTONES_KEPT,
                          gate_module.STALL_SECONDS), (300, 4096, None))
        short = self.restart(ttl_seconds=10, stall_seconds=4)
        approval_id, digest = short.request(_note())
        self.clock.now += 4
        self.assertEqual(short.run_state("run-1")["state"], "stalled")  # unanswered at its stall time
        self.clock.now += 6
        self.assertEqual(self.refusal(short.decide, approval_id, digest, OPERATOR_TOKEN, True), "approval_expired")
        with self.assertRaises(ValueError):
            self.Gate(lambda *drawn: None, authenticate, self.clock, stall_seconds=-1)

    def test_every_sentence_is_in_the_one_table(self) -> None:
        for code, words in gate_module.SAYS.items():
            with self.subTest(code=code):
                self.assertIs(gate_module.words_for(code), words)
                self.assertTrue(words.endswith("."))
                self.assertNotIn("_", words)
        for code in ("approval_expired", "approval_replayed", "approval_digest_mismatch", "approval_vetoed",
                     "approval_required", "record_invalid", "waiting_approval", "stalled"):
            self.assertIn(code, gate_module.SAYS)

    def test_the_queue_holds_the_gates_records_in_its_one_store(self) -> None:
        queue = ApprovalQueue(clock=lambda: float(self.clock.now))
        queue.attach(self.store, declare_tables=False)
        approval_id, _ = self.gate.request(_note())
        listed = queue.list_page({})["approvals"]
        self.assertEqual([(item["approval_id"], item["source"], item["state"]) for item in listed],
                         [(approval_id, "plugin_effect", "pending")])
        self.clock.now += gate_module.APPROVAL_DEADLINE_SECONDS
        self.assertEqual([record["approval_id"] for record in queue.sweep()], [approval_id])
        self.assertEqual(self.refusal(self.gate.take, approval_id, _note()), "approval_expired")

@needs_store
class AnswererPinTests(unittest.TestCase):
    """A pin, green before this change too: no principal outside the approver kinds may answer, so no later
    change can quietly make another kind an answerer without turning this red."""

    def test_only_an_owner_or_a_device_answers(self) -> None:
        clock = Clock()
        gate = ApprovalGate(lambda *drawn: None, authenticate, clock)
        approval_id, digest = gate.request(_note())
        for credential in (PLUGIN_TOKEN, AGENT_TOKEN, None):
            with self.subTest(credential=credential):
                with self.assertRaises(Refused) as caught:
                    gate.decide(approval_id, digest, credential, True)
                self.assertEqual(caught.exception.code, "approval_requires_operator")
        for principal in ({"kind": "agent", "id": "a-1"}, {"kind": "plugin", "id": "demo.plugin"}):
            with self.subTest(principal=principal):
                self.assertEqual(principal_refusal(principal), {"code": "scope_refused", "detail": "approve"})
        store = FakeStore()
        self.addCleanup(store.close)
        queue = ApprovalQueue(clock=lambda: float(clock.now), mint=lambda: "apr-pin-1")
        queue.attach(store)
        queued = queue.request("plugin_effect", _note(), run_id="run-1")
        with store.transaction() as tx:
            answer = queue.answer_in(tx, {"kind": "agent", "id": "a-1"}, {"approval_id": queued},
                                     lambda later: later())
        self.assertEqual(answer, {"code": "scope_refused", "detail": "approve"})
        self.assertEqual(_rows(store)[queued][0], "pending")


class RealStoreRestartTests(unittest.TestCase):
    """The event store itself, closed and opened again from its file in a folder the test makes."""

    def test_an_approval_and_a_veto_outlive_the_store_being_opened_again(self) -> None:
        try:
            import fcntl  # noqa: F401 - the event store takes its writer lock with it
        except ImportError:
            self.skipTest("the event store runs where fcntl does")
        from recursion_core.events.db import EventStore

        clock = Clock()
        with tempfile.TemporaryDirectory(prefix="gate-kept-") as folder:
            path = pathlib.Path(folder) / "events.db"

            def reopened():
                store = EventStore(path, clock=lambda: float(clock.now))
                self.addCleanup(store.close)
                return store, ApprovalGate(lambda *drawn: None, authenticate, clock, store=store)

            store, gate = reopened()
            approval_id, digest = gate.request(_note(intent="kept"))
            gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
            vetoed_id, vetoed = gate.request(_note(intent="vetoed"))
            gate.decide(vetoed_id, vetoed, OPERATOR_TOKEN, False)
            store.close()
            store, gate = reopened()
            self.assertEqual(gate.take(approval_id, _note(intent="kept")), digest)
            self.assertTrue(gate.is_vetoed(vetoed))
            store.close()
            store, gate = reopened()
            for call, args, code in ((gate.take, (approval_id, _note(intent="kept")), "approval_replayed"),
                                     (gate.request, (_note(intent="vetoed"),), "approval_vetoed")):
                with self.assertRaises(Refused) as caught:
                    call(*args)
                self.assertEqual(caught.exception.code, code)
            store.close()


class MemoryGateRunStateTests(unittest.TestCase):
    def test_a_gate_without_a_store_says_the_same(self) -> None:
        clock = Clock()
        gate = ApprovalGate(lambda *drawn: None, authenticate, clock)
        approval_id, digest = gate.request(_note())
        self.assertEqual(gate.run_state("run-1")["state"], "waiting_approval")
        clock.now += gate_module.APPROVAL_DEADLINE_SECONDS
        self.assertEqual(gate.run_state("run-1"), {"state": "stalled", "approval_id": approval_id,
                                                   "says": gate_module.SAYS["stalled"]})
        other, digest = gate.request(_note(intent="b"))
        gate.decide(other, digest, OPERATOR_TOKEN, True)
        self.assertIsNone(gate.run_state("run-1"))


class _ApproveExpiredOnReload(ApprovalGate):
    def _reloaded(self, tx, held, now):
        due = gate_module._durable()[1].expire_if_due(held, gate_module._durable()[2](now))
        if due is None:
            return held
        return dict(held, state="approved", answer="approve", answered_by=dict(gate_module.OPERATOR_PRINCIPAL),
                    answered_at=held["expires_at"])


class _ForgetVetoOnReload(ApprovalGate):
    def _vetoed_in_store(self):
        return set()


class _ApprovalCoversAnyDigest(ApprovalGate):
    def _covers(self, held, digest, raw):
        return True


MUTANTS = {
    "approve_expired_on_reload": (_ApproveExpiredOnReload, "test_deadline_passed_while_the_daemon_was_down"),
    "forget_veto_on_reload": (_ForgetVetoOnReload, "test_a_veto_then_a_restart_then_the_same_digest"),
    "approval_covers_any_digest": (_ApprovalCoversAnyDigest, "test_a_second_effect_digest_with_the_first_approval"),
}


@needs_store
class KeptGateMutantTests(unittest.TestCase):
    def test_each_mutant_turns_its_test_red(self) -> None:
        for name, (gate, test) in MUTANTS.items():
            with self.subTest(mutant=name):
                case = type("Mutant", (KeptGateTests,), {"Gate": gate})(test)
                result = unittest.TestResult()
                case.run(result)
                self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (1, 1, 0), name)
                clean = unittest.TestResult()
                KeptGateTests(test).run(clean)
                self.assertTrue(clean.wasSuccessful(), test)


if __name__ == "__main__":
    unittest.main()
