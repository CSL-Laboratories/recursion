"""The person confirms each sensitive use of a plugin written by a model, by default.

Every case runs through the real ``ApprovalGate``; the approval area is a stand-in that
records what is drawn, and the person is a stand-in ``wait`` that approves, refuses, lets
the time pass or does nothing.
"""

from __future__ import annotations

import unittest

from recursion_core.plugin_security import ApprovalGate, Refused, canonical
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
            target: str | None = None) -> str:
        return confirm(self.gate, self.wait, origin=origin, handle=handle,
                       target=target if target is not None else _TARGETS[handle],
                       plugin_id="demo.plugin", run_id="run-1", connection="conn-a",
                       lifted=lifted, first_uses=first_uses)


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
        self.assertEqual(decide("model", "network", _TARGETS["network"], first_uses=first_uses), GO)
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


if __name__ == "__main__":
    unittest.main()
