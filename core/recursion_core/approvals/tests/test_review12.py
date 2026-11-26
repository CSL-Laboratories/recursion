"""Three smaller points of the gate and the queue. Each test here is red on ``b0746805`` and green after this
round, unless its docstring says it holds what must stay.

* ``run_state`` breaks a same-second tie by the order the requests were made, as the gate without a store does.
* ``stall_seconds`` below one second is refused where it is given, with words that say why.
* The cap on a call's size is measured on the raw input before it is parsed.
"""

from __future__ import annotations

import unittest
from unittest import mock

from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.plugin_security import ApprovalGate
from recursion_core.plugin_security import approval as gate_module
from recursion_core.plugin_security.tests.support import OPERATOR_TOKEN, Clock, authenticate, effect

try:
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover - the seams are not in this tree
    FakeStore = None


def _note(intent: str = "a", run_id: str = "run-1") -> dict:
    return effect(kind="note.post", amount=0, payload={"intent": intent}, run_id=run_id)


@needs_fakes
class RunStateTieTests(unittest.TestCase):
    def run_state(self, ids: list, with_store: bool):
        clock = Clock()
        store = None
        if with_store:
            store = FakeStore()
            self.addCleanup(store.close)
        gate = ApprovalGate(lambda *drawn: None, authenticate, clock, store=store)
        with mock.patch.object(gate_module.secrets, "token_hex", side_effect=ids):
            gate.request(_note("first", run_id="run-7"))
            second, digest = gate.request(_note("second", run_id="run-7"))
        gate.decide(second, digest, OPERATOR_TOKEN, True)  # only the newer request is approved
        return gate.run_state("run-7")

    def test_a_same_second_tie_goes_to_the_later_request_whatever_the_ids(self) -> None:
        for ids in (["f" * 32, "0" * 32], ["0" * 32, "f" * 32]):
            for with_store in (True, False):
                with self.subTest(ids=(ids[0][:2], ids[1][:2]), store=with_store):
                    self.assertIsNone(self.run_state(ids, with_store))  # the newest is approved: no stall


class StallSecondsTests(unittest.TestCase):
    def test_a_stall_below_one_second_is_refused_where_it_is_given(self) -> None:
        for stall in (0, -1):
            with self.subTest(stall=stall):
                with self.assertRaises(ValueError) as refused:
                    ApprovalGate(lambda *drawn: None, authenticate, Clock(), stall_seconds=stall)
                self.assertIn("one second", str(refused.exception))

    def test_one_second_and_none_are_taken(self) -> None:
        """What must stay."""
        for stall in (1, None):
            ApprovalGate(lambda *drawn: None, authenticate, Clock(), stall_seconds=stall)


class RawCallCapTests(unittest.TestCase):
    def test_the_cap_is_measured_on_the_raw_input_before_it_is_parsed(self) -> None:
        from recursion_core.approvals import effects
        with self.assertRaises(effects.EffectRefused) as refused:
            effects.read_call("Tool", " " * effects.CALL_MAX + "{}", None)
        self.assertEqual(refused.exception.code, "too_large")

    def test_an_input_of_exactly_the_cap_is_read(self) -> None:
        """What must stay: the raw measure refuses only what is over the cap."""
        from recursion_core.approvals import effects
        text = '{"x":"' + "a" * (effects.CALL_MAX - 64) + '"}'
        text = text + " " * (effects.CALL_MAX - len(text.encode("utf-8")))
        self.assertEqual(len(text.encode("utf-8")), effects.CALL_MAX)
        self.assertEqual(effects.read_call("Tool", text, None)["tool"], "Tool")


if __name__ == "__main__":
    unittest.main()
