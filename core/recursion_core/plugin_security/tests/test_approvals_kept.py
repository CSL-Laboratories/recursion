"""The approval gate given the event store: approvals that last across a restart and expire, one effect each.

The store is the one shared fake of the event store (``control/tests/test_seams.py``), the store the approval
queue's own tests use; a restart is a new gate made on the same store, as a new daemon finds it. Time is a fake
clock moved by hand: no test waits on the wall clock, and none starts a thread or a process.

The three mutants at the end are run by the suite itself: each one is a gate with one rule taken out, and the
test it names must fail against it.
"""

from __future__ import annotations

import contextlib
import pathlib
import re
import tempfile
import unittest

from recursion_core.approvals.model import principal_refusal
from recursion_core.approvals import queue as queue_module
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.plugin_security import approval as gate_module
from recursion_core.plugin_security import ApprovalGate, Refused
from recursion_core.plugin_security.tests.support import (
    AGENT_TOKEN, OPERATOR_TOKEN, PLUGIN_TOKEN, Clock, authenticate, effect,
)

OWNER = {"kind": "owner", "id": "owner-1"}

try:
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover - the seams are not in this tree
    FakeStore = None

needs_store = unittest.skipUnless(FakeStore is not None, "the shared fake of the event store is not in this tree")


def _note(**payload) -> dict:
    return effect(kind="note.post", amount=0, payload=payload or {"intent": "a"})


def _run(run_id: str, intent: str) -> dict:
    return effect(kind="note.post", amount=0, run_id=run_id, payload={"intent": intent})


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


@needs_store
class RoundOnTheKeptGateTests(unittest.TestCase):
    """Cases each red before this change."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.declared = False
        self.drawn: list = []

    def gate(self, **figures) -> ApprovalGate:
        gate = ApprovalGate(lambda *drawn: self.drawn.append(drawn), authenticate, self.clock, store=self.store,
                            declare_tables=not self.declared, **figures)
        self.declared = True
        return gate

    def outcome(self, call, *args) -> tuple:
        try:
            return ("ok", call(*args))
        except Refused as refused:
            return ("refused", refused.code)

    def test_a_stall_shorter_than_the_deadline_ends_the_approval(self) -> None:
        for kept in (True, False):
            with self.subTest(kept=kept):
                clock = self.clock if kept else Clock()
                gate = self.gate(ttl_seconds=10, stall_seconds=4) if kept else \
                    ApprovalGate(lambda *drawn: None, authenticate, clock, ttl_seconds=10, stall_seconds=4)
                unanswered, digest = gate.request(_note(intent="unanswered"))
                answered, answered_digest = gate.request(_note(intent="answered"))
                clock.now += 1
                gate.decide(answered, answered_digest, OPERATOR_TOKEN, True)
                clock.now += 4  # past the stall time, still inside the deadline
                self.assertEqual(gate.run_state("run-1")["state"], "stalled")
                self.assertEqual(self.outcome(gate.decide, unanswered, digest, OPERATOR_TOKEN, True),
                                 ("refused", "approval_expired"))
                self.assertEqual(self.outcome(gate.take, unanswered, _note(intent="unanswered")),
                                 ("refused", "approval_expired"))
                self.assertEqual(self.outcome(gate.take, answered, _note(intent="answered")),
                                 ("refused", "approval_expired"))
                self.assertIn("not done", gate_module.words_for("stalled"))

    def test_an_approval_found_past_its_deadline_is_ended_for_good(self) -> None:
        gate = self.gate()
        approval_id, digest = gate.request(_note())
        gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        start = self.clock.now
        self.clock.now = start + gate_module.APPROVAL_DEADLINE_SECONDS + 5
        self.assertEqual(self.outcome(gate.take, approval_id, _note()), ("refused", "approval_expired"))
        self.assertEqual(_rows(self.store)[approval_id][0], "expired")
        self.clock.now = start + 1  # the clock steps back
        for after in (gate, self.gate()):
            self.assertEqual(self.outcome(after.take, approval_id, _note()), ("refused", "approval_expired"))
            self.assertEqual(self.outcome(after.decide, approval_id, digest, OPERATOR_TOKEN, True),
                             ("refused", "approval_expired"))
        answered = [(event["payload"]["state"], event["payload"]["answer"]) for event in self.store.events
                    if event["type"] == "approval.answered"]
        self.assertEqual(answered, [("approved", "approve"), ("expired", None)])  # who approved stays in the log

    def test_a_veto_through_one_gate_is_seen_by_every_gate_on_the_store(self) -> None:
        first, second = self.gate(), self.gate()  # the second is made before the veto
        vetoed, digest = first.request(_note())
        sibling, _ = first.request(_note())
        self.assertEqual(first.decide(vetoed, digest, OPERATOR_TOKEN, False), "vetoed")
        self.assertTrue(second.is_vetoed(digest))
        self.assertEqual(self.outcome(second.decide, sibling, digest, OPERATOR_TOKEN, True),
                         ("refused", "approval_vetoed"))
        self.assertEqual(self.outcome(second.request, _note()), ("refused", "approval_vetoed"))
        approved, digest = first.request(_note(intent="b"))
        first.decide(approved, digest, OPERATOR_TOKEN, True)
        other, _ = first.request(_note(intent="b"))
        second.decide(other, digest, OPERATOR_TOKEN, False)  # vetoed after the first was approved
        self.assertEqual(self.outcome(first.take, approved, _note(intent="b")), ("refused", "approval_vetoed"))

    def test_a_denial_through_the_queue_is_a_veto_for_every_gate(self) -> None:
        gate = self.gate()
        queue = ApprovalQueue(clock=lambda: float(self.clock.now))
        queue.attach(self.store, declare_tables=False)
        denied, digest = gate.request(_note())
        sibling, _ = gate.request(_note())
        with self.store.transaction() as tx:
            queue.open_in(tx, OWNER, {"approval_id": denied, "surface": "cli"})
            answer = queue.answer_in(tx, OWNER, {"command_id": "command-0001", "approval_id": denied, "surface": "cli",
                                                 "expected_revision": 1, "digest": digest, "answer": "deny"},
                                     lambda later: later())
        self.assertEqual(answer["code"], "ok")
        self.assertTrue(gate.is_vetoed(digest))
        self.assertEqual(self.outcome(gate.decide, sibling, digest, OPERATOR_TOKEN, True),
                         ("refused", "approval_vetoed"))
        self.assertEqual(self.outcome(gate.request, _note()), ("refused", "approval_vetoed"))

    def test_a_store_that_cannot_commit_is_a_typed_refusal(self) -> None:
        gate = self.gate()
        approval_id, digest = gate.request(_note())
        gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        self.store.fail_commit = True
        self.assertEqual(self.outcome(gate.request, _note(intent="x")), ("refused", "store_unavailable"))
        self.assertEqual(len(self.drawn), 1)  # a request whose commit failed is never drawn
        self.assertEqual(self.outcome(gate.take, approval_id, _note()), ("refused", "store_unavailable"))
        self.store.fail_commit = False
        self.assertEqual(self.outcome(gate.take, approval_id, _note()), ("ok", digest))
        self.assertEqual(self.outcome(gate.take, approval_id, _note()), ("refused", "approval_replayed"))

    def test_every_code_the_gate_answers_has_its_sentence(self) -> None:
        text = pathlib.Path(gate_module.__file__).read_text(encoding="utf-8")
        codes = set(re.findall(r'(?:Refused\(|return \(?)"([a-z_]+)"', text))
        self.assertGreater(len(codes), 15)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertIsNotNone(gate_module.words_for(code))
        gate = self.gate()
        crowded = _note(**{"k%03d" % number: "v" for number in range(500)})
        ApprovalGate(lambda *drawn: None, authenticate, Clock()).request(crowded)  # a gate without a store takes it
        for refused_effect in (crowded, effect(kind="note.post", amount=0, run_id="run 1", payload={"intent": "a"})):
            with self.subTest(effect=refused_effect["run_id"]):
                with self.assertRaises(Refused) as caught:
                    gate.request(refused_effect)
                self.assertEqual(caught.exception.code, "record_invalid")
                self.assertIsNotNone(gate_module.words_for(caught.exception.code))

    def test_the_words_are_true_in_every_case_that_reaches_them(self) -> None:
        for kept in (True, False):
            with self.subTest(kept=kept):
                gate = self.gate() if kept else ApprovalGate(lambda *drawn: None, authenticate, self.clock)
                waiting, _ = gate.request(_note(intent="waiting"))
                vetoed, digest = gate.request(_note(intent="vetoed"))
                gate.decide(vetoed, digest, OPERATOR_TOKEN, False)
                for approval_id, intent in ((waiting, "waiting"), (vetoed, "vetoed")):
                    self.assertEqual(self.outcome(gate.take, approval_id, _note(intent=intent)),
                                     ("refused", "approval_not_granted"))
                words = gate_module.words_for("approval_not_granted")
                self.assertIn("waiting", words)
                self.assertIn("refused", words)
                self.assertNotIn("Answer it first", words)
        words = gate_module.words_for("record_invalid")
        self.assertIn("could not be kept", words)  # a request refused before anything was saved
        self.assertIn("could not be read", words)  # a saved record torn

    def test_the_deadline_is_one_value_the_queue_reads_too(self) -> None:
        self.assertIs(queue_module.DEADLINE_SECONDS["plugin_effect"], gate_module.APPROVAL_DEADLINE_SECONDS)


class _FailsAfterTheDraw:
    """A stand-in store: the shared fake, whose commits can be made to end with an unknown outcome, as the event
    store's COMMIT does when it raises; from then on every transaction is refused ``store_failed`` until the
    store is opened again, as the event store's are."""

    def __init__(self, store) -> None:
        self.store = store
        self.fail_next = False
        self.failed = False

    def register_tables(self, *declared) -> None:
        self.store.register_tables(*declared)

    @contextlib.contextmanager
    def transaction(self):
        from recursion_core.events.db import CommitUnknown, StoreError
        if self.failed:
            raise StoreError("store_failed")
        with self.store.transaction() as tx:
            yield tx
        if self.fail_next:
            self.fail_next, self.failed = False, True
            raise CommitUnknown("commit_outcome_unknown")


@needs_store
class StallAndStoreRoundTests(unittest.TestCase):
    """Cases each red before this change: a deadline or stall time made shorter between a request and its
    answer, and a store whose commit ends with an unknown outcome after the prompt was drawn."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.declared = False

    def gate(self, store=None, draw=None, **figures) -> ApprovalGate:
        gate = ApprovalGate(draw or (lambda *drawn: None), authenticate, self.clock, store=store or self.store,
                            declare_tables=not self.declared, **figures)
        self.declared = True
        return gate

    def outcome(self, call, *args) -> tuple:
        try:
            return ("ok", call(*args))
        except Refused as refused:
            return ("refused", refused.code)

    def test_stall_changed_between_request_and_answer(self) -> None:
        for shorter in ({"stall_seconds": 4}, {"ttl_seconds": 4}):
            with self.subTest(shorter=shorter):
                self.setUp()
                first = self.gate(ttl_seconds=10)  # the default stall: the deadline
                unanswered, digest = first.request(_run("run-a", "a"))
                answered, answered_digest = first.request(_run("run-b", "b"))
                first.decide(answered, answered_digest, OPERATOR_TOKEN, True)
                self.clock.now += 5  # past the shorter figure, inside the old deadline
                after = self.gate(**dict({"ttl_seconds": 10}, **shorter))
                self.assertEqual(after.run_state("run-a")["state"], "stalled")
                self.assertEqual(self.outcome(after.decide, unanswered, digest, OPERATOR_TOKEN, True),
                                 ("refused", "approval_expired"))
                self.assertEqual(self.outcome(after.take, answered, _run("run-b", "b")), ("refused", "approval_expired"))
                self.assertEqual(after.run_state("run-b")["state"], "stalled")
                self.assertEqual({state for state, _ in _rows(self.store).values()}, {"expired"})
                longer = self.gate(ttl_seconds=10)  # the longer figure again: an ended approval stays ended
                self.assertEqual(self.outcome(longer.decide, unanswered, digest, OPERATOR_TOKEN, True),
                                 ("refused", "approval_expired"))
                self.assertEqual(longer.run_state("run-a")["state"], "stalled")

    def test_a_longer_stall_after_the_request_never_makes_an_ended_approval_live(self) -> None:
        first = self.gate(ttl_seconds=10, stall_seconds=4)
        unanswered, digest = first.request(_note(intent="unanswered"))
        answered, answered_digest = first.request(_note(intent="answered"))
        first.decide(answered, answered_digest, OPERATOR_TOKEN, True)
        self.clock.now += 5
        for after in (self.gate(ttl_seconds=10), self.gate(ttl_seconds=60, stall_seconds=60)):
            self.assertEqual(after.run_state("run-1")["state"], "stalled")
            self.assertEqual(self.outcome(after.decide, unanswered, digest, OPERATOR_TOKEN, True),
                             ("refused", "approval_expired"))
            self.assertEqual(self.outcome(after.take, answered, _note(intent="answered")),
                             ("refused", "approval_expired"))

    def test_an_unknown_commit_after_the_draw_is_caught_and_a_failed_store_says_so(self) -> None:
        store = _FailsAfterTheDraw(self.store)
        drawn: list = []

        def draw(*shown) -> None:
            drawn.append(shown)
            store.fail_next = True  # the commit that marks it shown ends with an unknown outcome

        gate = self.gate(store, draw)
        with self.assertLogs(gate_module.log, "ERROR"):
            approval_id, digest = gate.request(_note())
        self.assertEqual(len(drawn), 1)
        self.assertEqual(_rows(self.store)[approval_id][0], "pending")  # committed before the draw
        for call, args in ((gate.request, (_note(intent="b"),)),
                           (gate.decide, (approval_id, digest, OPERATOR_TOKEN, True)),
                           (gate.take, (approval_id, _note())), (gate.run_state, ("run-1",)),
                           (gate.is_vetoed, (digest,))):
            with self.subTest(call=call.__name__):
                self.assertEqual(self.outcome(call, *args), ("refused", "store_failed"))
        self.assertEqual(len(drawn), 1)  # nothing more is drawn
        words = gate_module.words_for("store_failed")
        self.assertIn("opened again", words)
        self.assertNotIn("later", words)
        self.assertEqual(gate_module.words_for("store_unavailable"),
                         "The approval store could not be written, so nothing was done. Try again later.")


class _ApproveExpiredOnReload(ApprovalGate):
    def _reloaded(self, tx, held, now):
        due = gate_module._durable()[1].expire_if_due(held, gate_module._durable()[2](now))
        if due is None:
            return held
        return dict(held, state="approved", answer="approve", answered_by=dict(gate_module.OPERATOR_PRINCIPAL),
                    answered_at=held["expires_at"])


class _IgnoreVetoesInTheStore(ApprovalGate):
    def _vetoed_in(self, tx, digest):
        return False


class _ApprovalCoversAnyDigest(ApprovalGate):
    def _covers(self, held, digest, raw):
        return True


MUTANTS = {
    "approve_expired_on_reload": (_ApproveExpiredOnReload, "test_deadline_passed_while_the_daemon_was_down"),
    "ignore_vetoes_in_the_store": (_IgnoreVetoesInTheStore, "test_a_veto_then_a_restart_then_the_same_digest"),
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
