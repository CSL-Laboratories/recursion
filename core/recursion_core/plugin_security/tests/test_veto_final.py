"""CSL-743: a veto is final for that intent; a retry must be a new intent.

Lucas's decision on csl-research #1161 item 5. Once the operator vetoes an approval,
the gate refuses the same effect digest (``approval_vetoed``) at ``request``, at
``decide`` approve for a still-pending sibling, and at ``take`` for a sibling approved
earlier. Expired, used and never-decided approvals do not block a re-request, and the
vetoed digests outlive tombstone eviction. They are in memory only: a restart forgets
them (known limit).
"""

from __future__ import annotations

import unittest

from recursion_core.plugin_security import ApprovalGate, Refused
from recursion_core.plugin_security.approval import effect_digest
from recursion_core.plugin_security.tests.support import (
    OPERATOR_TOKEN, PLUGIN_TOKEN, Clock, World, authenticate, effect, needs_crypto, profile,
)


def _code(response: dict) -> str | None:
    return None if response["ok"] else response["refusal"]["code"]


def _note(**payload) -> dict:
    return effect(kind="note.post", amount=0, payload=payload or {"intent": "a"})


class VetoFinalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.drawn: list = []
        self.gate = self._gate()

    def _gate(self, cap: int = 4096) -> ApprovalGate:
        return ApprovalGate(lambda *drawn: self.drawn.append(drawn), authenticate, self.clock,
                            ttl_seconds=300, max_retained=cap)

    def _refusal(self, call, *args) -> str:
        with self.assertRaises(Refused) as ctx:
            call(*args)
        return ctx.exception.code

    def _veto(self, gate: ApprovalGate, eff: dict) -> str:
        approval_id, digest = gate.request(eff)
        self.assertEqual(gate.decide(approval_id, digest, OPERATOR_TOKEN, False), "vetoed")
        return approval_id

    def _state(self, gate: ApprovalGate):
        return (dict(gate._approvals), list(gate._tombstones), list(gate._expiry), gate._live,
                set(gate._vetoed))

    def test_vetoed_effect_cannot_be_requested_again(self) -> None:
        eff = _note()
        self._veto(self.gate, eff)
        drawn, before = list(self.drawn), self._state(self.gate)
        for _ in range(3):
            self.assertEqual(self._refusal(self.gate.request, eff), "approval_vetoed")
        self.assertEqual(self.drawn, drawn)  # the refused request was never drawn
        self.assertEqual(self._state(self.gate), before)  # and created no state
        # Still refused after the vetoed approval's own TTL has passed.
        self.clock.now += 301
        self.assertEqual(self._refusal(self.gate.request, eff), "approval_vetoed")
        self.assertEqual(self.drawn, drawn)

    def test_only_the_digest_is_kept(self) -> None:
        eff = _note()
        approval_id = self._veto(self.gate, eff)
        self.assertEqual(self.gate._vetoed, {effect_digest(eff)})
        self.assertIsNone(self.gate._approvals[approval_id].raw)

    def test_a_different_digest_is_still_requestable(self) -> None:
        self._veto(self.gate, _note(intent="a"))
        for other in (_note(intent="b"), effect(kind="note.post", amount=1, payload={"intent": "a"}),
                      effect(kind="note.post", amount=0, payload={"intent": "a"}, run_id="run-2")):
            with self.subTest(other=other):
                approval_id, digest = self.gate.request(other)
                self.assertEqual(self.gate.decide(approval_id, digest, OPERATOR_TOKEN, True), "approved")
                self.assertEqual(self.gate.take(approval_id, other), digest)

    def test_only_the_operator_can_veto(self) -> None:
        eff = _note()
        approval_id, digest = self.gate.request(eff)
        for token in (PLUGIN_TOKEN, None):
            self.assertEqual(self._refusal(self.gate.decide, approval_id, digest, token, False),
                             "approval_requires_operator")
        # A veto naming the wrong digest, or an unknown approval, vetoes nothing.
        self.assertEqual(self._refusal(self.gate.decide, approval_id, "0" * 64, OPERATOR_TOKEN, False),
                         "approval_digest_mismatch")
        self.assertEqual(self._refusal(self.gate.decide, "apr_x", digest, OPERATOR_TOKEN, False),
                         "approval_unknown")
        self.assertEqual(self.gate._vetoed, set())
        self.gate.request(eff)

    def test_expired_pending_is_requestable_again(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        self.clock.now += 301
        second, _ = self.gate.request(eff)
        self.assertEqual(self.gate._approvals[first].state, "expired")
        # Deciding the expired one (veto included) refuses and does not veto the digest.
        self.assertEqual(self._refusal(self.gate.decide, first, digest, OPERATOR_TOKEN, False),
                         "approval_expired")
        self.assertEqual(self.gate.decide(second, digest, OPERATOR_TOKEN, True), "approved")
        self.assertEqual(self.gate.take(second, eff), digest)

    def test_pending_that_expires_at_the_veto_does_not_veto(self) -> None:
        # Not swept yet: decide itself finds the TTL passed, so the veto never lands.
        eff = _note()
        first, digest = self.gate.request(eff)
        self.clock.now += 300
        self.assertEqual(self._refusal(self.gate.decide, first, digest, OPERATOR_TOKEN, False),
                         "approval_expired")
        self.assertEqual(self.gate._vetoed, set())
        self.gate.request(eff)

    def test_approved_then_expired_is_requestable_again(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        self.gate.decide(first, digest, OPERATOR_TOKEN, True)
        self.clock.now += 301
        self.assertEqual(self._refusal(self.gate.take, first, eff), "approval_expired")
        second, _ = self.gate.request(eff)
        self.assertEqual(self.gate.decide(second, digest, OPERATOR_TOKEN, True), "approved")
        self.assertEqual(self.gate.take(second, eff), digest)

    def test_used_approval_is_requestable_again(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        self.gate.decide(first, digest, OPERATOR_TOKEN, True)
        self.assertEqual(self.gate.take(first, eff), digest)
        second, _ = self.gate.request(eff)
        self.assertEqual(self._refusal(self.gate.take, first, eff), "approval_replayed")
        self.assertEqual(self.gate.decide(second, digest, OPERATOR_TOKEN, True), "approved")
        self.assertEqual(self.gate.take(second, eff), digest)

    def test_never_decided_pending_is_requestable_again(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        second, _ = self.gate.request(eff)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.drawn), 2)
        self.assertEqual([self.gate._approvals[k].state for k in (first, second)], ["pending"] * 2)
        self.assertEqual(self.gate.decide(second, digest, OPERATOR_TOKEN, True), "approved")
        self.assertEqual(self.gate.take(second, eff), digest)

    def test_veto_survives_tombstone_eviction(self) -> None:
        gate = self._gate(cap=3)
        eff = _note(intent="vetoed")
        vetoed_id = self._veto(gate, eff)
        for i in range(50):  # used, vetoed and expired tombstones push the first one out
            other = _note(n=i)
            approval_id, digest = gate.request(other)
            if i % 3 == 0:
                gate.decide(approval_id, digest, OPERATOR_TOKEN, False)
            elif i % 3 == 1:
                gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
                gate.take(approval_id, other)
            else:
                self.clock.now += 301
        self.assertNotIn(vetoed_id, gate._approvals)  # its tombstone is gone
        self.assertLessEqual(len(gate._tombstones), 3)
        drawn = list(self.drawn)
        self.assertEqual(self._refusal(gate.request, eff), "approval_vetoed")
        self.assertEqual(self.drawn, drawn)
        # One digest per operator veto: 1 + the 17 vetoed in the loop, no effect bytes.
        self.assertEqual(len(gate._vetoed), 18)
        self.assertTrue(all(type(d) is str and len(d) == 64 for d in gate._vetoed))

    def test_refused_requests_do_not_grow_the_vetoed_set(self) -> None:
        eff = _note()
        self._veto(self.gate, eff)
        for i in range(100):
            self.assertEqual(self._refusal(self.gate.request, eff), "approval_vetoed")
            self.gate.request(_note(n=i))  # undecided plugin requests add nothing either
        self.assertEqual(len(self.gate._vetoed), 1)

    def test_pending_sibling_cannot_be_approved_after_a_veto(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        second, _ = self.gate.request(eff)
        self.gate.decide(first, digest, OPERATOR_TOKEN, False)
        for _ in range(2):
            self.assertEqual(self._refusal(self.gate.decide, second, digest, OPERATOR_TOKEN, True),
                             "approval_vetoed")
        self.assertEqual(self.gate._approvals[second].state, "pending")  # nothing retired
        self.assertEqual(self._refusal(self.gate.take, second, eff), "approval_not_granted")
        # Vetoing the sibling is still allowed.
        self.assertEqual(self.gate.decide(second, digest, OPERATOR_TOKEN, False), "vetoed")
        self.assertEqual(self.gate._vetoed, {digest})

    def test_sibling_decide_keeps_the_earlier_checks(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        second, _ = self.gate.request(eff)
        self.gate.decide(first, digest, OPERATOR_TOKEN, False)
        self.assertEqual(self._refusal(self.gate.decide, second, digest, PLUGIN_TOKEN, True),
                         "approval_requires_operator")
        self.assertEqual(self._refusal(self.gate.decide, second, "0" * 64, OPERATOR_TOKEN, True),
                         "approval_digest_mismatch")
        self.assertEqual(self._refusal(self.gate.decide, first, digest, OPERATOR_TOKEN, True),
                         "approval_already_decided")
        self.clock.now += 300
        self.assertEqual(self._refusal(self.gate.decide, second, digest, OPERATOR_TOKEN, True),
                         "approval_expired")

    def test_approved_sibling_cannot_be_taken_after_a_veto(self) -> None:
        eff = _note()
        first, digest = self.gate.request(eff)
        second, _ = self.gate.request(eff)
        self.assertEqual(self.gate.decide(first, digest, OPERATOR_TOKEN, True), "approved")
        self.gate.decide(second, digest, OPERATOR_TOKEN, False)
        for _ in range(2):
            self.assertEqual(self._refusal(self.gate.take, first, eff), "approval_vetoed")
        self.assertEqual(self.gate._approvals[first].state, "approved")  # nothing retired
        self.assertEqual(self._refusal(self.gate.take, second, eff), "approval_not_granted")
        # The earlier take checks keep their order and codes.
        self.assertEqual(self._refusal(self.gate.take, first, _note(intent="b")), "approval_digest_mismatch")
        self.assertEqual(self._refusal(self.gate.take, "apr_x", eff), "approval_required")
        self.clock.now += 300
        self.assertEqual(self._refusal(self.gate.take, first, eff), "approval_expired")

    def test_a_restart_forgets_vetoes(self) -> None:
        # Known limit: the gate is in memory only, so a new gate can ask again.
        eff = _note()
        self._veto(self.gate, eff)
        restarted = self._gate()
        approval_id, digest = restarted.request(eff)
        self.assertEqual(restarted.decide(approval_id, digest, OPERATOR_TOKEN, True), "approved")


@needs_crypto
class VetoFinalProtocolTests(unittest.TestCase):
    """The same rule through the ``csl-core/v0`` bytes."""

    def setUp(self) -> None:
        self.w = World(self)
        self.w.load()
        self.w.bind()
        self.env = profile(self.w.installer)

    def _decide(self, req: dict, decision: str) -> dict:
        return self.w.call(self.w.host, "approval.decide", approval_id=req["approval_id"],
                           digest=req["digest"], credential=OPERATOR_TOKEN, decision=decision)

    def test_wire_refusal_carries_approval_vetoed(self) -> None:
        eff = effect()
        req = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        self.assertEqual(self._decide(req, "veto")["result"]["state"], "vetoed")
        drawn = list(self.w.drawn)
        out = self.w.call(self.w.plugin, "effect.request", effect=eff)
        self.assertEqual(out, {"ok": False, "protocol": "csl-core/v0", "refusal": {"code": "approval_vetoed"}})
        self.assertEqual(self.w.drawn, drawn)
        # A new intent (different payload, so a different digest) goes through.
        retry = effect(payload={"note": "fake", "intent": 2})
        self.assertTrue(self.w.authorize(retry, self.w.approve(retry), self.env)["ok"])

    def test_wire_refusals_for_siblings(self) -> None:
        eff = effect()
        approved = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        pending = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        vetoed = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        self.assertEqual(self._decide(approved, "approve")["result"]["state"], "approved")
        self.assertEqual(self._decide(vetoed, "veto")["result"]["state"], "vetoed")
        self.assertEqual(_code(self._decide(pending, "approve")), "approval_vetoed")
        self.assertEqual(_code(self.w.authorize(eff, approved["approval_id"], self.env)), "approval_vetoed")
        self.assertEqual(self._decide(pending, "veto")["result"]["state"], "vetoed")
        # Nothing was charged for the vetoed intent.
        other = effect(payload={"note": "other"})
        out = self.w.authorize(other, self.w.approve(other), self.env)
        self.assertEqual(out["result"]["charge"]["spent_minor"], 1_000)


if __name__ == "__main__":
    unittest.main()
