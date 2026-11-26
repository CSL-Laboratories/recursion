"""The queue over the event store: commit before drawing, recovery, the deadline, the first answer, one use.

The store is SPINE's shared fake of EVENTS' helper (``control/tests/test_seams.py``, #369), as START.md's
ruling (ii) says; the queue uses only the calls EVENTS' own store also takes (tables named
``approvals_*``, full event envelopes). Time is a fake clock moved by hand: no test waits on the wall clock.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue

try:
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover - the seams (#369) are not in this tree
    FakeStore = None

V1 = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1"
needs_fakes = unittest.skipUnless(FakeStore is not None and (V1 / "vectors").is_dir(),
                                  "SPINE's seam files (#369) and schemas (#368) are not both in this tree")
OWNER = {"kind": "owner", "id": "local"}
PHONE = {"kind": "device", "id": "phone-1"}
START = 1_789_000_000.0  # a fixed moment, seconds since 1970


def _vector_effect(name: str) -> dict:
    return json.loads((V1 / "vectors" / "approval.binding" / name).read_text(encoding="utf-8"))["record"]["effect"]


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@needs_fakes
class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.counter = iter(range(1, 10_000))
        self.queue = self._queue()
        self.drawn: list = []
        self.queue.subscribe_requested(lambda record: self.drawn.append(record["approval_id"]))
        self.engine = _vector_effect("g01-a-tool-call.json")
        self.plugin = _vector_effect("g03-a-plugin-effect.json")

    def _queue(self) -> ApprovalQueue:
        queue = ApprovalQueue(clock=self.clock, mint=lambda: "apr-%04d" % next(self.counter))
        queue.attach(self.store)
        return queue

    def _restarted(self) -> ApprovalQueue:
        queue = ApprovalQueue(clock=self.clock)
        queue.attach(self.store, declare_tables=False)  # the same durable store, as a new daemon finds it
        return queue

    def _engine_request(self, request_id: str = "toolu_1") -> str:
        return self.queue.request("engine_tool", self.engine, session_id=self.engine["session_id"],
                                  run_id=self.engine["run_id"], turn_id="turn-0001", engine_request_id=request_id)

    def _call(self, queue, method: str, principal: dict, params: dict) -> dict:
        """The dispatcher's part: one transaction, the handler inside it, the callbacks after the commit."""
        callbacks: list = []
        with self.store.transaction() as tx:
            if method == "approval.open":
                answer = queue.open_in(tx, principal, params)
            else:
                answer = queue.answer_in(tx, principal, params, callbacks.append)
        for fn in callbacks:
            fn()
        return answer

    def _open(self, approval_id: str, principal=OWNER, surface="cli", queue=None) -> dict:
        return self._call(queue or self.queue, "approval.open", principal,
                          {"command_id": "open-" + approval_id, "approval_id": approval_id, "surface": surface})

    def _answer(self, approval_id: str, record: dict, answer="approve", principal=OWNER, surface="cli", queue=None):
        return self._call(queue or self.queue, "approval.answer", principal,
                          {"command_id": "ans-" + approval_id + principal["id"], "approval_id": approval_id,
                           "surface": surface, "expected_revision": record["revision"], "digest": record["digest"],
                           "answer": answer})

    def test_a_request_commits_its_record_and_event_and_is_drawn_after(self) -> None:
        approval_id = self._engine_request()
        self.assertEqual(self.drawn, [approval_id])
        (event,) = self.store.events
        self.assertEqual((event["type"], event["source"], event["trust"]), ("approval.requested", "core", "trusted"))
        self.assertEqual(event["payload"]["approval_id"], approval_id)
        self.assertEqual(event["payload"]["state"], "pending")
        self.assertEqual(self.queue.list_page({})["approvals"][0]["approval_id"], approval_id)

    def test_a_request_that_does_not_commit_leaves_nothing_and_draws_nothing(self) -> None:
        self.store.fail_commit = True
        with self.assertRaises(Exception):
            self._engine_request()
        self.store.fail_commit = False
        self.assertEqual((self.drawn, self.store.events), ([], []))
        self.assertEqual(self.queue.list_page({})["approvals"], [])

    def test_a_request_that_cannot_be_bound_whole_is_refused_and_writes_nothing(self) -> None:
        with self.assertRaises(ApprovalRefused) as refused:
            self.queue.request("engine_tool", dict(self.engine, input={"n": 2**60}), session_id=self.engine["session_id"],
                               run_id=self.engine["run_id"], engine_request_id="toolu_x")
        self.assertEqual(refused.exception.code, "integer_out_of_range")
        with self.assertRaises(ApprovalRefused) as refused:
            self.queue.request("engine_tool", self.engine, session_id=self.engine["session_id"], run_id=self.engine["run_id"])
        self.assertEqual(refused.exception.code, "binding_mismatch")  # an engine's request names its pending request
        self.assertEqual((self.drawn, self.store.events), ([], []))

    def test_open_then_answer_approves_once_and_the_effect_is_carried_out_once(self) -> None:
        approval_id = self._engine_request()
        opened = self._open(approval_id)["approval"]
        answer = self._answer(approval_id, opened)
        self.assertEqual((answer["code"], answer["approval"]["state"]), ("ok", "approved"))
        record = self.queue.wait(approval_id, timeout=0)
        self.assertEqual(record["state"], "approved")
        binding = {"approval_id": approval_id, "run_id": self.engine["run_id"], "engine": self.engine["engine"],
                   "engine_request_id": "toolu_1", "bound_at": record["requested_at"]}
        self.assertEqual(self.queue.consume(approval_id, record["revision"], record["digest"], binding)["state"], "consumed")
        with self.assertRaises(ApprovalRefused) as refused:
            self.queue.consume(approval_id, record["revision"], record["digest"], binding)
        self.assertEqual(refused.exception.code, "already_consumed")
        self.assertEqual([e["type"] for e in self.store.events], ["approval.requested", "approval.answered"])

    def test_list_alone_does_not_count_as_shown(self) -> None:
        approval_id = self._engine_request()
        record = self.queue.list_page({})["approvals"][0]
        with self.store.transaction() as tx:
            held = rows.load(tx, approval_id)
        answer = self._answer(approval_id, held)
        self.assertEqual(answer["code"], "prompt_not_shown")
        self.assertEqual(record["state"], "pending")

    def test_two_clients_answer_one_wins_the_other_is_answered_elsewhere(self) -> None:
        approval_id = self._engine_request()
        opened = self._open(approval_id)["approval"]
        self._open(approval_id, principal=PHONE, surface="phone")
        first = self._answer(approval_id, opened, "deny", principal=PHONE, surface="phone")
        second = self._answer(approval_id, opened, "approve")
        self.assertEqual(first["approval"]["state"], "denied")
        self.assertEqual((second["code"], second.get("detail")), ("conflict", "already_resolved"))
        self.assertEqual(self.queue.wait(approval_id, timeout=0)["state"], "denied")

    def test_an_unanswered_request_is_denied_at_its_deadline(self) -> None:
        approval_id = self._engine_request()
        opened = self._open(approval_id)["approval"]
        self.clock.now += 15 * 60 - 0.001
        self.assertEqual(self.queue.sweep(), [])
        self.clock.now += 0.001
        (expired,) = self.queue.sweep()
        self.assertEqual((expired["state"], expired["answer"], expired["answered_by"]), ("expired", None, None))
        self.assertEqual(self.queue.wait(approval_id, timeout=0)["state"], "expired")
        late = self._answer(approval_id, opened)
        self.assertEqual((late["code"], late.get("detail")), ("conflict", "already_resolved"))
        with self.assertRaises(ApprovalRefused) as refused:
            self.queue.consume(approval_id, opened["revision"], opened["digest"], None)
        self.assertEqual(refused.exception.code, "not_approved")
        self.assertEqual(self.store.events[-1]["payload"]["state"], "expired")

    def test_the_private_binding_is_never_on_the_wire(self) -> None:
        approval_id = self._engine_request("toolu_secret_request")
        opened = self._open(approval_id)
        answered = self._answer(approval_id, opened["approval"])
        seen = json.dumps([self.store.events, opened, answered, self.queue.list_page({})])
        self.assertNotIn("toolu_secret_request", seen)
        self.assertNotIn("engine_request_id", seen)

    def test_recovery_closes_an_engine_request_and_tells_an_untold_one(self) -> None:
        engine_id = self._engine_request()
        # The daemon died between a plugin request's commit and telling its surfaces: written, never told.
        from recursion_core.approvals import model
        plugin_record = model.new_request(approval_id="apr-untold", source="plugin_effect", effect=self.plugin,
                                          session_id=None, run_id=self.plugin["run_id"], turn_id=None,
                                          requested_at=self.queue.now(), expires_at="2099-01-01T00:00:00.000Z")
        with self.store.transaction() as tx:
            rows.insert(tx, plugin_record, None)
        restarted = self._restarted()
        told: list = []
        restarted.subscribe_requested(lambda record: told.append(record["approval_id"]))
        report = restarted.recover()
        self.assertEqual(report["closed"], [engine_id])  # never approved: its engine request died with the daemon
        self.assertEqual(told, ["apr-untold"])
        self.assertEqual(restarted.wait(engine_id, timeout=0)["state"], "expired")
        again = restarted.recover()
        self.assertEqual((again["closed"], again["told"]), ([], []))
        states = {s["approval_id"]: s["state"] for s in restarted.list_page({})["approvals"]}
        self.assertEqual(states, {engine_id: "expired", "apr-untold": "pending"})

    def test_an_answer_survives_a_restart_and_a_restart_approves_nothing(self) -> None:
        approval_id = self.queue.request("plugin_effect", self.plugin, run_id=self.plugin["run_id"])
        other = self.queue.request("plugin_effect", dict(self.plugin, payload={"n": 2}), run_id=self.plugin["run_id"])
        opened = self._open(approval_id)["approval"]
        self._answer(approval_id, opened, "deny")
        restarted = self._restarted()
        restarted.recover()
        states = {s["approval_id"]: s["state"] for s in restarted.list_page({})["approvals"]}
        self.assertEqual(states, {approval_id: "denied", other: "pending"})

    def test_a_request_is_drawn_only_once_its_transaction_has_ended(self) -> None:
        seen: list = []
        self.queue.subscribe_requested(lambda record: seen.append(
            (self.store.open_transactions, [event["payload"]["approval_id"] for event in self.store.events])))
        approval_id = self._engine_request()
        self.assertEqual(seen, [(0, [approval_id])])  # no transaction open, and the request already committed


if __name__ == "__main__":
    unittest.main()
