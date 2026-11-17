"""The plugin-effect sink over #400's approval queue.

A plugin effect cannot be carried out through the queue alone: the queue records one and an owner or a
device can answer it, and nothing charges it before it is consumed. The sink records the effect bound to
its own run, charges it while the approval is still approved, consumes it only after that charge returns,
and refuses a second carry without charging again.

These tests are red on the queue by itself (there is no sink). They use SPINE's fake of the event store,
the same one the queue's own tests use, and a charge the test supplies: the authority ledger is not this
package's.
"""

from __future__ import annotations

import threading
import unittest

from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base


def _sink_type():
    from recursion_core.approvals.sink import PluginEffectSink
    return PluginEffectSink


@needs_fakes
class PluginEffectSinkTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.charges: list = []
        self.sink = _sink_type()(self.queue, self._charge)

    def _charge(self, effect: dict, approval_id: str):
        """The test's charge: it sees the approval still approved, and it spends once per call."""
        with self.store.transaction() as tx:
            held = rows.load(tx, approval_id)
        self.charges.append((approval_id, None if held is None else held["state"], effect["run_id"]))
        return {"approval_id": approval_id, "amount_minor": effect["amount_minor"]}

    def _state(self, approval_id: str) -> str:
        with self.store.transaction() as tx:
            held = rows.load(tx, approval_id)
        return None if held is None else held["state"]

    def _record(self, approval_id: str) -> dict:
        with self.store.transaction() as tx:
            return rows.load(tx, approval_id)

    def test_a_plugin_effect_is_recorded_bound_to_its_run_charged_then_consumed_and_refused_twice(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        record = self._record(approval_id)
        self.assertEqual(record["source"], "plugin_effect")
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["run_id"], effect["run_id"])
        self.assertEqual(record["effect"]["run_id"], effect["run_id"])
        self.assertEqual([event["type"] for event in self.store.events], ["approval.requested"])
        self.assertEqual(self.store.events[0]["payload"]["approval_id"], approval_id)

        self.approve(approval_id)
        self.assertEqual(self._state(approval_id), "approved")

        other_run = dict(effect, run_id="run-not-the-bound-one")
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry(approval_id, other_run)
        self.assertEqual(refused.exception.code, "approval_mismatch")
        self.assertEqual((self.charges, self._state(approval_id)), ([], "approved"))

        carried = self.sink.carry(approval_id, effect)
        self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual(carried["approval"]["run_id"], effect["run_id"])
        self.assertEqual(carried["charge"]["approval_id"], approval_id)
        self.assertEqual(self.charges, [(approval_id, "approved", effect["run_id"])])
        self.assertEqual(self._state(approval_id), "consumed")

        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry(approval_id, effect)
        self.assertEqual(refused.exception.code, "already_consumed")
        self.assertEqual(len(self.charges), 1)
        self.assertEqual(self._state(approval_id), "consumed")

    def test_a_refused_charge_leaves_the_approval_unconsumed_and_a_later_carry_spends_once(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        self.approve(approval_id)

        def refuse(effect, approval_id):
            self.charges.append("refused")
            raise ApprovalRefused("budget_total_exceeded")

        self.sink = _sink_type()(self.queue, refuse)
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry(approval_id, effect)
        self.assertEqual(refused.exception.code, "budget_total_exceeded")
        self.assertEqual(self._state(approval_id), "approved")

        self.sink = _sink_type()(self.queue, self._charge)
        carried = self.sink.carry(approval_id, effect)
        self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual(self.charges, ["refused", (approval_id, "approved", effect["run_id"])])

    def test_an_exception_from_the_charge_is_not_a_consumption(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        self.approve(approval_id)

        def boom(effect, approval_id):
            raise RuntimeError("ledger")

        self.sink = _sink_type()(self.queue, boom)
        with self.assertRaises(RuntimeError):
            self.sink.carry(approval_id, effect)
        self.assertEqual(self._state(approval_id), "approved")
        self.sink = _sink_type()(self.queue, self._charge)
        self.assertEqual(self.sink.carry(approval_id, effect)["approval"]["state"], "consumed")

    def test_an_interruption_during_the_charge_leaves_the_approval_unconsumed(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        self.approve(approval_id)

        def interrupt(effect, approval_id):
            raise KeyboardInterrupt("synthetic interruption during the charge")

        self.sink = _sink_type()(self.queue, interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.sink.carry(approval_id, effect)
        self.assertEqual(self._state(approval_id), "approved")
        self.assertEqual(len(self.store.events), 2)  # requested, answered; nothing for a charge that did not return

    def test_a_second_caller_is_refused_and_the_charge_runs_once(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        self.approve(approval_id)
        outcomes: list = []

        def carry() -> None:
            try:
                outcomes.append(self.sink.carry(approval_id, effect))
            except BaseException as exc:  # noqa: BLE001 - the second caller is recorded, not lost
                outcomes.append(exc)

        first, second = threading.Thread(target=carry), threading.Thread(target=carry)
        first.start()
        second.start()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        refused = [item for item in outcomes if isinstance(item, ApprovalRefused)]
        done = [item for item in outcomes if isinstance(item, dict)]
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].code, "already_consumed")
        self.assertEqual(done[0]["approval"]["state"], "consumed")
        self.assertEqual(len(self.charges), 1)
        self.assertEqual(self._state(approval_id), "consumed")

    def test_a_charge_that_lands_is_not_repeated_when_the_consumption_raises(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        self.approve(approval_id)
        real = self.queue.consume
        calls = {"n": 0}

        def consume_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("consumption raised after the charge")
            return real(*args, **kwargs)

        self.queue.consume = consume_once
        with self.assertRaises(RuntimeError):
            self.sink.carry(approval_id, effect)
        self.assertEqual(self._state(approval_id), "approved")
        self.assertEqual(len(self.charges), 1)
        carried = self.sink.carry(approval_id, effect)
        self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual(carried["charge"]["approval_id"], approval_id)
        self.assertEqual(len(self.charges), 1)

    def test_a_carry_before_an_answer_charges_nothing(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry(approval_id, effect)
        self.assertEqual(refused.exception.code, "not_approved")
        self.assertEqual((self.charges, self._state(approval_id)), ([], "pending"))

    def test_an_unknown_approval_and_a_malformed_effect_write_nothing(self) -> None:
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry("apr-missing", self.plugin)
        self.assertEqual(refused.exception.code, "approval_unknown")
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.request({"schema": "csl-effect/vNext"})
        self.assertEqual(refused.exception.code, "effect")
        self.assertEqual((self.store.events, self.charges), ([], []))

    def test_an_engine_approval_and_a_denied_plugin_effect_are_not_charged(self) -> None:
        engine_id = self.queue.request("engine_tool", self.engine, session_id=self.engine["session_id"],
                                       run_id=self.engine["run_id"], turn_id="turn-0001",
                                       engine_request_id="toolu_1")
        self.approve(engine_id)
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry(engine_id, self.plugin)
        self.assertEqual(refused.exception.code, "source")

        effect = dict(self.plugin, payload={"n": 9})
        plugin_id = self.sink.request(effect)
        shown = self.call(self.queue, "approval.open",
                          {"command_id": "o-" + plugin_id, "approval_id": plugin_id, "surface": "cli"})["approval"]
        denied = self.call(self.queue, "approval.answer",
                           {"command_id": "d-" + plugin_id, "approval_id": plugin_id, "surface": "cli",
                            "expected_revision": shown["revision"], "digest": shown["digest"], "answer": "deny"})
        self.assertEqual(denied["approval"]["state"], "denied")
        with self.assertRaises(ApprovalRefused) as refused:
            self.sink.carry(plugin_id, effect)
        self.assertEqual(refused.exception.code, "not_approved")
        self.assertEqual(self.charges, [])

    def test_a_carry_without_a_store_is_refused_and_the_charge_must_be_callable(self) -> None:
        bare = ApprovalQueue(clock=self.clock)
        sink = _sink_type()(bare, self._charge)
        with self.assertRaises(ApprovalRefused) as refused:
            sink.carry("apr-none", self.plugin)
        self.assertEqual(refused.exception.code, "store_absent")
        with self.assertRaises(TypeError):
            _sink_type()(self.queue, None)

    def test_the_charge_cannot_carry_the_sink_again(self) -> None:
        effect = dict(self.plugin)
        approval_id = self.sink.request(effect)
        self.approve(approval_id)

        def reenter(effect, approval_id):
            self.sink.carry(approval_id, effect)
            return {}

        self.sink = _sink_type()(self.queue, reenter)
        with self.assertRaises(RuntimeError):
            self.sink.carry(approval_id, effect)
        self.assertEqual(self._state(approval_id), "approved")


if __name__ == "__main__":
    unittest.main()
