"""The person confirms each sensitive use of a plugin written by a model, by default.

Every case runs through the real ``ApprovalGate``; the approval area is a stand-in that
records what is drawn, and the person is a stand-in ``wait`` that approves, refuses, lets
the time pass or does nothing.
"""

from __future__ import annotations

import os
import random
import sys
import threading
import time
import unittest

from recursion_core.plugin_security import ApprovalGate, Refused, canonical
from recursion_core.plugin_security import confirm as confirm_module
from recursion_core.plugin_security.confirm import (
    ASK, GO, NetworkFirstUses, confirm, confirmation_effect, decide,
)

_OPERATOR = "operator-ui-token-stand-in"
_TTL = 300
_TARGETS = {"credential": "broker.example-key", "network": "api.example.com:443", "process": "/usr/bin/git"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


class _Area:
    """A gate, its approval area and a person who answers the way the test says."""

    def __init__(self, answer: str = "approve") -> None:
        self.clock = _Clock()
        self.drawn: list[tuple[str, str, bytes]] = []
        self.waited = 0
        self.answer = answer
        self.gate = ApprovalGate(lambda i, d, raw: self.drawn.append((i, d, raw)),
                                 lambda credential: "operator_ui" if credential == _OPERATOR else None,
                                 self.clock, ttl_seconds=_TTL)

    def wait(self, approval_id: str, digest: str) -> None:
        self.waited += 1
        if self.answer == "approve":
            self.gate.decide(approval_id, digest, _OPERATOR, True)
        elif self.answer == "refuse":
            self.gate.decide(approval_id, digest, _OPERATOR, False)
        elif self.answer == "time_out":
            self.clock.now += _TTL
        # "nothing": the person has not answered when the wait returns

    def use(self, handle: str, origin, *, lifted: bool = False, first_uses: NetworkFirstUses,
            target: str | None = None, plugin_id: str = "demo.plugin") -> str:
        return confirm(self.gate, self.wait, origin=origin, handle=handle,
                       target=target if target is not None else _TARGETS[handle],
                       plugin_id=plugin_id, run_id="run-1", connection="conn-a",
                       lifted=lifted, first_uses=first_uses)


class _TwinRefusedInsideTake(ApprovalGate):
    """A gate where the person refuses a twin approval of the same use (the same digest,
    drawn earlier and still pending) right after the real ``take`` returned: the refusal
    lands between the take and the record."""

    twin: tuple[str, str] | None = None

    def take(self, approval_id: str, effect: dict) -> str:
        digest = super().take(approval_id, effect)
        if self.twin is not None:
            twin, self.twin = self.twin, None
            self.decide(twin[0], twin[1], _OPERATOR, False)
        return digest


class EachCaseTests(unittest.TestCase):
    """One test per handle, origin and lift (filled in below the class)."""

    def _case(self, handle: str, origin: str, lifted: bool) -> None:
        first_uses = NetworkFirstUses()
        expected = GO if origin == "person" or lifted else ASK
        self.assertEqual(decide(origin, handle, _TARGETS[handle], lifted=lifted, first_uses=first_uses), expected)
        area = _Area()
        reason = area.use(handle, origin, lifted=lifted, first_uses=first_uses)
        if expected == ASK:
            self.assertEqual(len(area.drawn), 1)
            self.assertEqual(area.waited, 1)
            self.assertEqual(reason, "approved by the person")
        else:
            self.assertEqual(area.drawn, [])
            self.assertEqual(area.waited, 0)
            self.assertEqual(reason, "origin person" if origin == "person" else "lifted by the person")


def _make(handle: str, origin: str, lifted: bool):
    def test(self: EachCaseTests) -> None:
        self._case(handle, origin, lifted)
    return test


for _handle in ("credential", "network", "process"):
    for _origin in ("model", "unknown", "person"):
        for _lifted in (False, True):
            _name = f"test_{_handle}_origin_{_origin}_{'lifted' if _lifted else 'not_lifted'}"
            setattr(EachCaseTests, _name, _make(_handle, _origin, _lifted))


class RuleTests(unittest.TestCase):
    def test_no_origin_recorded_counts_as_unknown_and_asks(self) -> None:
        for handle in ("credential", "network", "process"):
            with self.subTest(handle=handle):
                self.assertEqual(decide(None, handle, _TARGETS[handle], first_uses=NetworkFirstUses()), ASK)
                area = _Area()
                self.assertEqual(area.use(handle, None, first_uses=NetworkFirstUses()), "approved by the person")
                self.assertEqual(len(area.drawn), 1)

    def test_not_lifted_is_the_default(self) -> None:
        for handle in ("credential", "network", "process"):
            with self.subTest(handle=handle):
                self.assertEqual(decide("model", handle, _TARGETS[handle], first_uses=NetworkFirstUses()), ASK)

    def test_network_first_use_asks_and_the_second_goes(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        self.assertEqual(area.use("network", "model", first_uses=first_uses), "approved by the person")
        self.assertEqual(decide("model", "network", _TARGETS["network"], first_uses=first_uses,
                                plugin_id="demo.plugin", run_id="run-1", connection="conn-a"), GO)
        self.assertEqual(area.use("network", "model", first_uses=first_uses),
                         "destination approved since the daemon started")
        self.assertEqual(len(area.drawn), 1)
        self.assertEqual(area.waited, 1)

    def test_network_asks_once_per_destination(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        area.use("network", "unknown", first_uses=first_uses)
        self.assertEqual(decide("unknown", "network", "other.example.com:443", first_uses=first_uses), ASK)
        self.assertEqual(area.use("network", "unknown", first_uses=first_uses, target="other.example.com:443"),
                         "approved by the person")
        self.assertEqual(len(area.drawn), 2)

    def test_a_destination_approved_for_one_plugin_is_asked_again_for_another(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        self.assertEqual(area.use("network", "model", first_uses=first_uses, plugin_id="plugin.a"),
                         "approved by the person")
        # Plugin b asks for the same destination: the person is asked, and the box names plugin b.
        self.assertEqual(area.use("network", "model", first_uses=first_uses, plugin_id="plugin.b"),
                         "approved by the person")
        self.assertEqual(len(area.drawn), 2)
        expected = confirmation_effect("network", _TARGETS["network"], plugin_id="plugin.b",
                                       run_id="run-1", connection="conn-a")
        self.assertEqual(area.drawn[1][2], canonical(expected))
        # Plugin a is not asked a second time.
        self.assertEqual(area.use("network", "model", first_uses=first_uses, plugin_id="plugin.a"),
                         "destination approved since the daemon started")
        self.assertEqual(len(area.drawn), 2)
        self.assertEqual(area.waited, 2)
        # decide says the same; a plugin never approved, or no plugin named, is asked.
        for plugin_id, expected_answer in (("plugin.a", GO), ("plugin.b", GO), ("plugin.c", ASK), (None, ASK)):
            with self.subTest(plugin_id=plugin_id):
                self.assertEqual(decide("model", "network", _TARGETS["network"], first_uses=first_uses,
                                        plugin_id=plugin_id, run_id="run-1", connection="conn-a"),
                                 expected_answer)

    def test_a_network_approval_never_lets_a_credential_or_a_program_of_the_same_name_go(self) -> None:
        shared = "shared.example-name"
        area, first_uses = _Area(), NetworkFirstUses()
        self.assertEqual(area.use("network", "model", first_uses=first_uses, target=shared),
                         "approved by the person")
        for handle in ("credential", "process"):
            with self.subTest(handle=handle):
                self.assertEqual(area.use(handle, "model", first_uses=first_uses, target=shared),
                                 "approved by the person")
                self.assertEqual(decide("model", handle, shared, first_uses=first_uses,
                                        plugin_id="demo.plugin"), ASK)
        self.assertEqual(len(area.drawn), 3)

    def test_a_daemon_restart_clears_the_record(self) -> None:
        area = _Area()
        area.use("network", "model", first_uses=NetworkFirstUses())
        restarted = NetworkFirstUses()  # the daemon makes a new, empty record at each start
        self.assertEqual(decide("model", "network", _TARGETS["network"], first_uses=restarted), ASK)
        self.assertEqual(area.use("network", "model", first_uses=restarted), "approved by the person")
        self.assertEqual(len(area.drawn), 2)

    def test_credential_asks_on_each_use(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        for _ in range(3):
            self.assertEqual(area.use("credential", "model", first_uses=first_uses), "approved by the person")
        self.assertEqual(len(area.drawn), 3)
        self.assertEqual(decide("model", "credential", _TARGETS["credential"], first_uses=first_uses), ASK)

    def test_process_asks_on_each_start(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        for _ in range(3):
            self.assertEqual(area.use("process", "unknown", first_uses=first_uses), "approved by the person")
        self.assertEqual(len(area.drawn), 3)
        self.assertEqual(decide("unknown", "process", _TARGETS["process"], first_uses=first_uses), ASK)

    def test_a_refusal_refuses_the_use_with_the_approvals_refusal(self) -> None:
        for handle in ("credential", "network", "process"):
            with self.subTest(handle=handle):
                area, first_uses = _Area("refuse"), NetworkFirstUses()
                with self.assertRaises(Refused) as caught:
                    area.use(handle, "model", first_uses=first_uses)
                # What the gate's take says for a vetoed approval, unchanged.
                self.assertEqual((caught.exception.code, caught.exception.detail), ("approval_not_granted", "vetoed"))
                self.assertEqual(decide("model", handle, _TARGETS[handle], first_uses=first_uses), ASK)
                # The gate's veto is final for that use: asked again, it is refused without drawing.
                with self.assertRaises(Refused) as again:
                    area.use(handle, "model", first_uses=first_uses)
                self.assertEqual(again.exception.code, "approval_vetoed")
                self.assertEqual(len(area.drawn), 1)

    def test_a_time_out_refuses_the_use_with_the_approvals_refusal(self) -> None:
        for handle in ("credential", "network", "process"):
            with self.subTest(handle=handle):
                area, first_uses = _Area("time_out"), NetworkFirstUses()
                with self.assertRaises(Refused) as caught:
                    area.use(handle, "unknown", first_uses=first_uses)
                # What the gate's take says for an approval left undecided past its time, unchanged.
                self.assertEqual((caught.exception.code, caught.exception.detail), ("approval_not_granted", "pending"))
                self.assertEqual(decide("unknown", handle, _TARGETS[handle], first_uses=first_uses), ASK)

    def test_no_answer_yet_refuses_the_use(self) -> None:
        area, first_uses = _Area("nothing"), NetworkFirstUses()
        with self.assertRaises(Refused) as caught:
            area.use("network", "model", first_uses=first_uses)
        self.assertEqual((caught.exception.code, caught.exception.detail), ("approval_not_granted", "pending"))
        self.assertNotIn(_TARGETS["network"], first_uses)

    def test_a_refusal_landing_after_the_take_refuses_the_use_and_records_nothing(self) -> None:
        for handle in ("credential", "network", "process"):
            with self.subTest(handle=handle):
                area, first_uses = _Area(), NetworkFirstUses()
                gate = _TwinRefusedInsideTake(lambda i, d, raw: area.drawn.append((i, d, raw)),
                                              lambda credential: "operator_ui" if credential == _OPERATOR else None,
                                              area.clock, ttl_seconds=_TTL)
                area.gate = gate
                twin = confirmation_effect(handle, _TARGETS[handle], plugin_id="demo.plugin",
                                           run_id="run-1", connection="conn-a")
                gate.twin = gate.request(twin)
                digest = gate.twin[1]
                with self.assertRaises(Refused) as caught:
                    area.use(handle, "model", first_uses=first_uses)
                self.assertEqual(caught.exception.code, "approval_vetoed")
                self.assertTrue(gate.is_vetoed(digest))
                self.assertEqual(len(area.drawn), 2)
                # Nothing is recorded, for that plugin or for any plugin.
                self.assertNotIn(("demo.plugin", _TARGETS[handle]), first_uses)
                self.assertNotIn(_TARGETS[handle], first_uses)
                self.assertEqual(decide("model", handle, _TARGETS[handle], first_uses=first_uses,
                                        plugin_id="demo.plugin"), ASK)
                # A later use of the same plugin and target asks again: the same use reaches the
                # gate, which refuses it without drawing; in another run the person is asked.
                with self.assertRaises(Refused) as again:
                    area.use(handle, "model", first_uses=first_uses)
                self.assertEqual(again.exception.code, "approval_vetoed")
                self.assertEqual(len(area.drawn), 2)
                self.assertEqual(confirm(gate, area.wait, origin="model", handle=handle, target=_TARGETS[handle],
                                         plugin_id="demo.plugin", run_id="run-2", connection="conn-a",
                                         first_uses=first_uses), "approved by the person")
                self.assertEqual(len(area.drawn), 3)

    def test_a_bare_destination_with_no_plugin_named_asks(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        target = _TARGETS["network"]
        self.assertEqual(area.use("network", "model", first_uses=first_uses, plugin_id="plugin.a"),
                         "approved by the person")
        # The record says a destination alone was approved for some plugin ...
        self.assertIn(target, first_uses)
        self.assertNotIn("other.example.com:443", first_uses)
        # ... yet a lookup that names no plugin asks, and so does a record of bare destinations.
        self.assertEqual(decide("model", "network", target, first_uses=first_uses), ASK)
        for plugin_id in (None, "plugin.a"):
            with self.subTest(plugin_id=plugin_id):
                self.assertEqual(decide("model", "network", target, first_uses={target},
                                        plugin_id=plugin_id), ASK)

    def test_what_is_drawn_names_the_handle_and_the_target(self) -> None:
        for handle in ("credential", "network", "process"):
            with self.subTest(handle=handle):
                area = _Area()
                area.use(handle, "model", first_uses=NetworkFirstUses())
                (_, _, raw), = area.drawn
                expected = confirmation_effect(handle, _TARGETS[handle], plugin_id="demo.plugin",
                                               run_id="run-1", connection="conn-a")
                self.assertEqual(raw, canonical(expected))
                self.assertEqual(expected["payload"], {"handle": handle, "target": _TARGETS[handle]})
                self.assertEqual(expected["amount_minor"], 0)

    def test_decide_changes_nothing(self) -> None:
        first_uses = NetworkFirstUses()
        for _ in range(2):
            self.assertEqual(decide("model", "network", _TARGETS["network"], first_uses=first_uses), ASK)
        self.assertNotIn(_TARGETS["network"], first_uses)

    def test_a_callers_mistake_is_never_an_answer(self) -> None:
        bad = [
            ({"origin": "robot"}, ValueError), ({"origin": ""}, ValueError),
            ({"handle": "file"}, ValueError), ({"target": ""}, TypeError), ({"target": None}, TypeError),
            ({"lifted": "off"}, TypeError), ({"lifted": 1}, TypeError), ({"lifted": None}, TypeError),
        ]
        for change, error in bad:
            args = {"origin": "model", "handle": "network", "target": "api.example.com:443", "lifted": False}
            args.update(change)
            with self.subTest(change=change):
                with self.assertRaises(error):
                    decide(args["origin"], args["handle"], args["target"], lifted=args["lifted"],
                           first_uses=NetworkFirstUses())
                area = _Area()
                with self.assertRaises(error):
                    confirm(area.gate, area.wait, origin=args["origin"], handle=args["handle"],
                            target=args["target"], plugin_id="demo.plugin", run_id="run-1",
                            connection="conn-a", lifted=args["lifted"], first_uses=NetworkFirstUses())
                self.assertEqual(area.drawn, [])


# The rounds of the thread run below: a test figure, not a product one. The shipped first
# value is 20000; CSL_CONFIRM_STRESS_ROUNDS sets another for one run.
_STRESS_ROUNDS = int(os.environ.get("CSL_CONFIRM_STRESS_ROUNDS", "20000"))
# The widest spin, in empty loop turns, before the refusing thread refuses: a test figure.
_STRESS_SPIN = int(os.environ.get("CSL_CONFIRM_STRESS_SPIN", "40"))
_NET = _TARGETS["network"]
_GIL = getattr(sys, "_is_gil_enabled", lambda: True)()


def _kept(first_uses: NetworkFirstUses, plugin_id: str = "demo.plugin") -> set:
    """The digests kept behind one allow, read as they are (a test's view of the record)."""
    return set(getattr(first_uses, "_allows", {}).get((plugin_id, _NET), ()))


class _VetoAtTheStore(ApprovalGate):
    """A gate where the person refuses a twin approval of the same use (the same digest,
    still pending) at the last moment before the allow is kept: right after the gate
    answered a veto check, or just before the one critical section that keeps it."""

    twin: tuple[str, str] | None = None

    def _refuse_the_twin(self) -> None:
        if self.twin is not None:
            twin, self.twin = self.twin, None
            self.decide(twin[0], twin[1], _OPERATOR, False)

    def is_vetoed(self, digest: str) -> bool:
        answer = super().is_vetoed(digest)
        self._refuse_the_twin()
        return answer

    def keep(self, approval_id, effect, store):
        self._refuse_the_twin()
        return super().keep(approval_id, effect, store)


class _SnapshotAtVeto(ApprovalGate):
    """A gate that notes which digests the record kept at the instant a veto lands (inside
    the gate's lock), and when a take has returned (so that a refusal can be timed at the
    store)."""

    first_uses: NetworkFirstUses | None = None
    kept_at_veto: set | None = None
    taken = False

    def take(self, approval_id, effect):
        digest = super().take(approval_id, effect)
        self.taken = True
        return digest

    def _retire(self, approval_id, item, state):
        # Runs under the gate's lock: the note is taken in the same instant the veto lands.
        if state == "vetoed" and self.first_uses is not None:
            self.kept_at_veto = _kept(self.first_uses)
        super()._retire(approval_id, item, state)


def _gate(area: _Area, cls=ApprovalGate) -> ApprovalGate:
    gate = cls(lambda i, d, raw: area.drawn.append((i, d, raw)),
               lambda credential: "operator_ui" if credential == _OPERATOR else None,
               area.clock, ttl_seconds=_TTL)
    area.gate = gate
    return gate


def _net(area: _Area, first_uses: NetworkFirstUses, run_id: str, **kw) -> str:
    return confirm(area.gate, area.wait, origin="model", handle="network", target=kw.pop("target", _NET),
                   plugin_id=kw.pop("plugin_id", "demo.plugin"), run_id=run_id, connection="conn-a",
                   first_uses=first_uses, **kw)


class VetoIsFinalTests(unittest.TestCase):
    """A veto is final for its use, whatever happens after it and on whatever thread."""

    def test_a_refused_use_stays_refused_after_another_run_is_approved(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        area.answer = "refuse"
        with self.assertRaises(Refused):
            _net(area, first_uses, "run-1")
        area.answer = "approve"
        self.assertEqual(_net(area, first_uses, "run-2"), "approved by the person")
        self.assertEqual(len(area.drawn), 2)
        # The retry of the refused use is refused without asking, though run-2 was allowed.
        with self.assertRaises(Refused) as again:
            _net(area, first_uses, "run-1")
        self.assertEqual((again.exception.code, again.exception.detail),
                         ("approval_vetoed", confirm_module.VETOED_WORDS))
        self.assertEqual(len(area.drawn), 2)
        self.assertEqual(decide("model", "network", _NET, first_uses=first_uses, plugin_id="demo.plugin",
                                run_id="run-1", connection="conn-a"), ASK)
        # A use no veto covers goes on run-2's allow.
        self.assertEqual(_net(area, first_uses, "run-3"), "destination approved since the daemon started")
        self.assertEqual(len(area.drawn), 2)

    def test_a_refusal_landing_at_the_store_keeps_nothing(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        gate = _gate(area, _VetoAtTheStore)
        effect = confirmation_effect("network", _NET, plugin_id="demo.plugin", run_id="run-1", connection="conn-a")
        gate.twin = gate.request(effect)
        digest = gate.twin[1]
        with self.assertRaises(Refused) as caught:
            _net(area, first_uses, "run-1")
        self.assertEqual(caught.exception.code, "approval_vetoed")
        self.assertIsNone(gate.twin)
        self.assertTrue(gate.is_vetoed(digest))
        self.assertNotIn(("demo.plugin", _NET), first_uses)
        self.assertEqual(_kept(first_uses), set())
        drawn = len(area.drawn)
        with self.assertRaises(Refused) as again:
            _net(area, first_uses, "run-1")
        self.assertEqual(again.exception.code, "approval_vetoed")
        self.assertEqual(len(area.drawn), drawn)

    def test_a_veto_after_the_allow_was_kept_takes_effect_at_once(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        effect = confirmation_effect("network", _NET, plugin_id="demo.plugin", run_id="run-1", connection="conn-a")
        twin = area.gate.request(effect)
        self.assertEqual(_net(area, first_uses, "run-1"), "approved by the person")
        self.assertIn(("demo.plugin", _NET), first_uses)
        area.gate.decide(twin[0], twin[1], _OPERATOR, False)  # the refusal comes after the allow was kept
        drawn = len(area.drawn)
        with self.assertRaises(Refused) as again:
            _net(area, first_uses, "run-1")
        self.assertEqual((again.exception.code, again.exception.detail),
                         ("approval_vetoed", confirm_module.VETOED_WORDS))
        self.assertEqual(len(area.drawn), drawn)
        # The only approval behind the allow is vetoed: the allow stands no more, so another
        # run of that plugin is asked again.
        self.assertNotIn(("demo.plugin", _NET), first_uses)
        self.assertNotIn(_NET, first_uses)
        self.assertEqual(_net(area, first_uses, "run-2"), "approved by the person")
        self.assertEqual(len(area.drawn), drawn + 1)

    def test_an_allow_is_kept_only_for_an_approval_the_gate_gave_and_that_was_taken(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        # The record of an earlier form, with no approval at all, keeps nothing.
        with self.assertRaises((TypeError, Refused)):
            first_uses.record("demo.plugin", _NET)
        self.assertNotIn(("demo.plugin", _NET), first_uses)
        self.assertEqual(decide("model", "network", _NET, first_uses=first_uses, plugin_id="demo.plugin",
                                run_id="run-1", connection="conn-a"), ASK)
        effect = confirmation_effect("network", _NET, plugin_id="demo.plugin", run_id="run-1", connection="conn-a")
        cases = []
        with self.assertRaises(Refused) as unknown:
            first_uses.record(area.gate, "apr_" + "0" * 32, effect)
        cases.append(unknown.exception.code)
        approval_id, digest = area.gate.request(effect)
        area.gate.decide(approval_id, digest, _OPERATOR, True)
        with self.assertRaises(Refused) as untaken:
            first_uses.record(area.gate, approval_id, effect)
        cases.append((untaken.exception.code, untaken.exception.detail))
        area.gate.take(approval_id, effect)
        other = confirmation_effect("network", _NET, plugin_id="other.plugin", run_id="run-1", connection="conn-a")
        with self.assertRaises(Refused) as mismatch:
            first_uses.record(area.gate, approval_id, other)
        cases.append(mismatch.exception.code)
        credential = confirmation_effect("credential", _NET, plugin_id="demo.plugin", run_id="run-1",
                                         connection="conn-a")
        with self.assertRaises(Refused) as not_network:
            first_uses.record(area.gate, approval_id, credential)
        cases.append(not_network.exception.code)
        self.assertEqual(cases, ["approval_required", ("approval_not_taken", "approved"),
                                 "approval_digest_mismatch", "allow_not_network"])
        self.assertNotIn(_NET, first_uses)
        first_uses.record(area.gate, approval_id, effect)  # the taken approval: kept
        self.assertIn(("demo.plugin", _NET), first_uses)
        self.assertNotIn(("other.plugin", _NET), first_uses)

    def test_a_record_answers_for_one_gate_only(self) -> None:
        area, first_uses = _Area(), NetworkFirstUses()
        self.assertEqual(_net(area, first_uses, "run-1"), "approved by the person")
        other = _Area()
        with self.assertRaises(Refused) as caught:
            _net(other, first_uses, "run-1")
        self.assertEqual(caught.exception.code, "first_uses_other_gate")
        self.assertEqual(other.drawn, [])

    def test_a_target_that_holds_a_secret_is_refused_before_anything_is_drawn(self) -> None:
        for target in ("https://user:secret-token@api.example.com:443", "user:secret-token@api.example.com:443",
                       "user@api.example.com", "https://api.example.com/v1?access_token=secret-token",
                       "api.example.com:443/v1?api_key=secret-token", "https://api.example.com/#token=secret-token",
                       "https://api.example.com/v1?page=2&Signature=secret-token"):
            with self.subTest(target=target):
                area, first_uses = _Area(), NetworkFirstUses()
                with self.assertRaises(Refused) as caught:
                    _net(area, first_uses, "run-1", target=target)
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 ("target_holds_secret", confirm_module.SECRET_TARGET_WORDS))
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(area.drawn, [])
                self.assertEqual(area.waited, 0)
                self.assertNotIn(target, first_uses)
        for target in ("api.example.com:443", "https://api.example.com:443/v1/items?page=2", "[::1]:8443"):
            with self.subTest(target=target):
                area = _Area()
                self.assertEqual(_net(area, NetworkFirstUses(), "run-1", target=target), "approved by the person")

    def test_thread_run_a_veto_is_final_whatever_the_order(self) -> None:
        """One thread confirms a use while another refuses its twin, round after round."""
        rnd = random.Random(603)
        stored_after_veto = standing = unasked = not_vetoed = 0
        for _ in range(_STRESS_ROUNDS):
            area, first_uses = _Area(), NetworkFirstUses()
            gate = _gate(area, _SnapshotAtVeto)
            gate.first_uses = first_uses
            effect = confirmation_effect("network", _NET, plugin_id="demo.plugin", run_id="run-1",
                                         connection="conn-a")
            twin = gate.request(effect)
            spin = rnd.randint(0, _STRESS_SPIN)
            # Half the rounds time the refusal at the store: it waits until the take returned.
            aimed = rnd.random() < 0.5
            done = []

            def confirming() -> None:
                try:
                    _net(area, first_uses, "run-1")
                except Refused:
                    pass
                finally:
                    done.append(True)

            def refusing() -> None:
                while aimed and not gate.taken and not done:
                    if _GIL:
                        time.sleep(0)  # with a GIL, a bare spin would hold the lock the other thread needs
                for _ in range(spin):
                    pass
                gate.decide(twin[0], twin[1], _OPERATOR, False)

            threads = [threading.Thread(target=confirming), threading.Thread(target=refusing)]
            rnd.shuffle(threads)
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if not gate.is_vetoed(twin[1]):
                not_vetoed += 1
            if twin[1] in _kept(first_uses) and twin[1] not in (gate.kept_at_veto or set()):
                stored_after_veto += 1
            if ("demo.plugin", _NET) in first_uses:
                standing += 1
            drawn = len(area.drawn)
            try:
                confirm(gate, lambda i, d: None, origin="model", handle="network", target=_NET,
                        plugin_id="demo.plugin", run_id="run-1", connection="conn-a", first_uses=first_uses)
                unasked += 1
            except Refused as refused:
                if refused.code != "approval_vetoed" or len(area.drawn) != drawn:
                    unasked += 1
        self.assertEqual({"rounds": _STRESS_ROUNDS, "not vetoed": not_vetoed,
                          "allow kept after the veto": stored_after_veto,
                          "allow standing for a vetoed digest": standing,
                          "later use let go unasked": unasked},
                         {"rounds": _STRESS_ROUNDS, "not vetoed": 0, "allow kept after the veto": 0,
                          "allow standing for a vetoed digest": 0, "later use let go unasked": 0})


class _WatchedLock:
    """A stand-in for the gate's lock. It says which thread holds it, counts how often it was
    taken, and can run one action just before the n-th next time it is taken: a refusal placed
    at an exact step of the code under test, with no thread and no luck."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder = None
        self.acquires = 0
        self._armed = None

    def held(self) -> bool:
        return self._holder == threading.get_ident()

    def arm(self, before_acquire: int, action) -> None:
        """Run ``action`` just before the lock is taken ``before_acquire`` takes from now (0: the next)."""
        self._armed = (self.acquires + before_acquire, action)

    def fired(self) -> bool:
        return self._armed is None

    def __enter__(self):
        number, self.acquires = self.acquires, self.acquires + 1
        if self._armed is not None and self._armed[0] == number:
            action, self._armed = self._armed[1], None
            action()
        self._lock.acquire()
        self._holder = threading.get_ident()
        return self

    def __exit__(self, *exc) -> None:
        self._holder = None
        self._lock.release()


class _Watch:
    """Puts the stand-in lock into a gate, and makes its veto set and the record's allows refuse
    to be read or written by a thread that does not hold that lock."""

    def __init__(self, gate: ApprovalGate, first_uses: NetworkFirstUses) -> None:
        self.gate = gate
        self.lock = _WatchedLock()
        self.violations: list[str] = []
        self.stores = 0
        gate._lock = self.lock
        gate._vetoed = _GuardedSet(self)
        first_uses._allows = _GuardedAllows(self)

    def need_the_lock(self, what: str) -> None:
        if not self.lock.held():
            self.violations.append(what + " ran without the gate's lock")
            raise AssertionError(self.violations[-1])


class _GuardedSet(set):
    def __init__(self, watch: _Watch) -> None:
        super().__init__()
        self._watch = watch

    def __contains__(self, item) -> bool:
        self._watch.need_the_lock("a veto check")
        return super().__contains__(item)

    def add(self, item) -> None:
        self._watch.need_the_lock("a veto")
        super().add(item)


class _GuardedDigests(set):
    def __init__(self, watch: _Watch) -> None:
        super().__init__()
        self._watch = watch

    def __iter__(self):
        self._watch.need_the_lock("a read of the allows")
        return super().__iter__()

    def add(self, digest) -> None:
        self._watch.need_the_lock("the store of an allow")
        if set.__contains__(self._watch.gate._vetoed, digest):
            self._watch.violations.append("an allow was stored for a digest already vetoed")
            raise AssertionError(self._watch.violations[-1])
        self._watch.stores += 1
        super().add(digest)


class _GuardedAllows(dict):
    def __init__(self, watch: _Watch) -> None:
        super().__init__()
        self._watch = watch

    def setdefault(self, key, default=None):
        self._watch.need_the_lock("the store of an allow")
        if not dict.__contains__(self, key):
            dict.__setitem__(self, key, _GuardedDigests(self._watch))
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._watch.need_the_lock("a read of the allows")
        return dict.get(self, key, default)

    def items(self):
        self._watch.need_the_lock("a read of the allows")
        return dict.items(self)

    def __getitem__(self, key):
        self._watch.need_the_lock("a read of the allows")
        return dict.__getitem__(self, key)

    def __contains__(self, key) -> bool:
        self._watch.need_the_lock("a read of the allows")
        return dict.__contains__(self, key)

    def __iter__(self):
        self._watch.need_the_lock("a read of the allows")
        return dict.__iter__(self)


def _peek(first_uses: NetworkFirstUses) -> dict:
    """The allows as they are, read without any guard (a test's view of the record)."""
    return {key: set(set.copy(value)) for key, value in dict.items(first_uses._allows)}


class OneStepUnderTheLockTests(unittest.TestCase):
    """The veto check and the store of an allow are one step under the gate's lock, and the
    go answer is given under it: placed at exact steps, with no thread and no luck."""

    @staticmethod
    def _watched():
        area, first_uses = _Area(), NetworkFirstUses()
        return area, first_uses, _Watch(area.gate, first_uses)

    @staticmethod
    def _effect(run_id: str) -> dict:
        return confirmation_effect("network", _NET, plugin_id="demo.plugin", run_id=run_id, connection="conn-a")

    def test_every_veto_check_and_every_store_and_every_go_answer_holds_the_gates_lock(self) -> None:
        area, first_uses, watch = self._watched()
        self.assertEqual(_net(area, first_uses, "run-1"), "approved by the person")
        self.assertGreaterEqual(watch.stores, 1)
        self.assertEqual(set(_peek(first_uses)), {("demo.plugin", _NET)})
        answers = {
            "confirm": lambda: _net(area, first_uses, "run-2") == "destination approved since the daemon started",
            "decide": lambda: decide("model", "network", _NET, first_uses=first_uses, plugin_id="demo.plugin",
                                     run_id="run-3", connection="conn-a") == GO,
            "stands": lambda: first_uses.stands("demo.plugin", _NET, run_id="run-4", connection="conn-a"),
            "pair": lambda: ("demo.plugin", _NET) in first_uses,
            "destination": lambda: _NET in first_uses,
        }
        for name, answer in answers.items():
            with self.subTest(answer=name):
                before = watch.lock.acquires
                self.assertTrue(answer())
                self.assertGreater(watch.lock.acquires, before)  # the go answer took the gate's lock
        self.assertEqual(watch.violations, [])

    def test_a_refusal_placed_at_each_step_of_the_keep_never_lands_between_its_check_and_its_store(self) -> None:
        for step in range(4):
            with self.subTest(step=step):
                area, first_uses, watch = self._watched()
                gate, effect = area.gate, self._effect("run-1")
                twin = gate.request(effect)  # a pending approval of this same use
                approval_id, digest = gate.request(effect)
                gate.decide(approval_id, digest, _OPERATOR, True)
                gate.take(approval_id, effect)
                # The person refuses the twin just before the lock is taken for the step-th time.
                watch.lock.arm(step, lambda: gate.decide(twin[0], twin[1], _OPERATOR, False))
                code = None
                try:
                    first_uses.record(gate, approval_id, effect)
                except Refused as refused:
                    code = refused.code
                self.assertEqual(watch.violations, [])
                if step == 0:
                    self.assertTrue(watch.lock.fired())
                    self.assertEqual(code, "approval_vetoed")
                if code is not None:
                    self.assertEqual(_peek(first_uses), {})

    def test_a_refusal_placed_before_the_go_answer_is_seen_by_it(self) -> None:
        for step in range(3):
            for name in ("stands", "decide"):
                with self.subTest(step=step, answer=name):
                    area, first_uses, watch = self._watched()
                    self.assertEqual(_net(area, first_uses, "run-2"), "approved by the person")
                    twin = area.gate.request(self._effect("run-1"))
                    watch.lock.arm(step, lambda: area.gate.decide(twin[0], twin[1], _OPERATOR, False))
                    if name == "stands":
                        answer = first_uses.stands("demo.plugin", _NET, run_id="run-1", connection="conn-a")
                    else:
                        answer = decide("model", "network", _NET, first_uses=first_uses, plugin_id="demo.plugin",
                                        run_id="run-1", connection="conn-a") == GO
                    self.assertEqual(watch.violations, [])
                    if step == 0:
                        self.assertTrue(watch.lock.fired())
                        self.assertFalse(answer)


if __name__ == "__main__":
    unittest.main()
