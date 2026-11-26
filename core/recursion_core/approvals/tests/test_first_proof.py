"""The brief's first proof (CONTRACT 1710): a tool call waits for an approval and is refused without one.

A fake driver stands for an engine's permission bridge: it raises a tool permission request
(``queue.request``), its call blocks (``queue.wait``), and it answers its engine only from the queue's
after-commit answer (``subscribe_answered``, standing for driver.v2's ``approval_answer``). With no answer by
the deadline the call is denied and the tool never runs; an approve, given after ``approval.open`` and bound
to that digest, lets exactly that call run, once, even when the answer is replayed. The event sequence of
each case is printed.
"""

from __future__ import annotations

import json
import threading
import types
import unittest
from pathlib import Path

from recursion_core.approvals.hooks import install
from recursion_core.approvals.queue import ApprovalQueue

try:
    from recursion_core.control.hooks import Hooks, Principal
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover
    Hooks = None

V1 = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1"
needs_seams = unittest.skipUnless(Hooks is not None and (V1 / "vectors").is_dir(),
                                  "SPINE's seam files (#369) and schemas (#368) are not both in this tree")


class Clock:
    def __init__(self) -> None:
        self.now = 1_789_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeDriver:
    """One engine's permission bridge: ask, block, and run the tool only on an approve it can consume."""

    def __init__(self, queue: ApprovalQueue, effect: dict) -> None:
        self.queue, self.effect = queue, effect
        self.ran: list = []
        self.answers: list = []
        queue.subscribe_answered(self._on_answer)

    def _on_answer(self, record: dict, binding) -> None:
        # driver.v2 approval_answer, after the commit: only for this driver's own pending request
        if binding is None or binding["engine_request_id"] != "toolu_proof":
            return
        self.answers.append(record["state"])

    def tool_call(self, started: threading.Event) -> None:
        approval_id = self.queue.request("engine_tool", self.effect, session_id=self.effect["session_id"],
                                         run_id=self.effect["run_id"], turn_id="turn-0001",
                                         engine_request_id="toolu_proof")
        self.approval_id = approval_id
        started.set()
        record = self.queue.wait(approval_id, timeout=30)  # a bound on a broken test, not a deadline of the queue
        if record is None or record["state"] != "approved":
            return  # denied or expired: the tool does not run
        binding = {"approval_id": approval_id, "run_id": self.effect["run_id"], "engine": self.effect["engine"],
                   "engine_request_id": "toolu_proof", "bound_at": record["requested_at"]}
        consumed = self.queue.consume(approval_id, record["revision"], record["digest"], binding)
        self.ran.append(consumed["effect"]["input"]["command"])


@needs_seams
class FirstProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.queue = ApprovalQueue(clock=self.clock)
        self.hooks = Hooks()
        install(self.hooks, self.queue)
        self.hooks.freeze()
        self.store = FakeStore()
        self.addCleanup(self.store.close)
        self.queue.attach(self.store)
        self.hooks.methods.store = self.store
        self.effect = json.loads((V1 / "vectors" / "approval.binding" / "g01-a-tool-call.json").read_text(
            encoding="utf-8"))["record"]["effect"]
        self.driver = FakeDriver(self.queue, self.effect)
        self.started = threading.Event()
        self.call = threading.Thread(target=self.driver.tool_call, args=(self.started,))
        self.call.start()
        self.addCleanup(self.call.join, 30)
        self.assertTrue(self.started.wait(30))

    def _dispatch(self, method: str, params: dict) -> dict:
        return self.hooks.methods.dispatch(method, params, Principal("owner", "local"), lambda m, p: None)

    def _sequence(self, name: str) -> list:
        sequence = [(e["type"], e["payload"].get("state")) for e in self.store.events]
        print("\nfirst proof, %s: %s" % (name, sequence))
        return sequence

    def test_no_answer_by_the_deadline_denies_and_the_tool_does_not_run(self) -> None:
        self.assertTrue(self.call.is_alive(), "the call did not wait")
        self.clock.now += 15 * 60  # the deadline of an engine's tool call
        self.queue.sweep()
        self.call.join(30)
        self.assertFalse(self.call.is_alive())
        self.assertEqual((self.driver.ran, self.driver.answers), ([], ["expired"]))
        self.assertEqual(self._sequence("unanswered"), [("approval.requested", "pending"), ("approval.answered", "expired")])

    def test_an_approve_after_open_runs_exactly_that_call_once(self) -> None:
        self.assertTrue(self.call.is_alive(), "the call did not wait")
        shown = self._dispatch("approval.open", {"command_id": "cmd-open-0001", "approval_id": self.driver.approval_id,
                                                 "surface": "cli"})["approval"]
        params = {"command_id": "cmd-answer-01", "approval_id": self.driver.approval_id, "surface": "cli",
                  "expected_revision": shown["revision"], "digest": shown["digest"], "answer": "approve"}
        self.assertEqual(self._dispatch("approval.answer", params)["approval"]["state"], "approved")
        self.call.join(30)
        self.assertFalse(self.call.is_alive())
        self.assertEqual(self._dispatch("approval.answer", params)["approval"]["state"], "approved")  # a replay
        self.assertEqual(self.driver.ran, ["rm -rf build && make"])
        self.assertEqual(self.driver.answers, ["approved"])
        self.assertEqual(self.queue.wait(self.driver.approval_id, timeout=0)["state"], "consumed")
        self.assertEqual(self._sequence("approved"), [("approval.requested", "pending"), ("approval.answered", "approved")])

    def test_no_answer_by_the_deadline_the_started_worker_denies_and_the_tool_does_not_run(self) -> None:
        """The deadline case with the queue's own deadline worker started: nothing sweeps by hand here."""
        self.clock.now += 15 * 60  # the call setUp made is ended by hand first, as the case above does
        self.queue.sweep()
        self.call.join(30)
        queue = ApprovalQueue(clock=self.clock, deadlines={"engine_tool": 1, "plugin_effect": 1})
        queue.attach(self.store, declare_tables=False)
        queue.start(types.SimpleNamespace(store=self.store))  # started before the request: recovery ends nothing
        self.addCleanup(queue.stop)
        driver = FakeDriver(queue, self.effect)
        started = threading.Event()
        call = threading.Thread(target=driver.tool_call, args=(started,))
        call.start()
        self.addCleanup(call.join, 30)
        self.assertTrue(started.wait(30))
        self.clock.now += 1  # its deadline: the started worker wakes within a second and denies it
        call.join(30)
        self.assertFalse(call.is_alive())
        self.assertEqual((driver.ran, driver.answers), ([], ["expired"]))


if __name__ == "__main__":
    unittest.main()
