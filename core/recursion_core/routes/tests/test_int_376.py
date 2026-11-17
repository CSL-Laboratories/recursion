"""Integration entries against #376 that live in routes/driver.py.

The published refusal code of a mid-turn send is driver.v2's ``unsupported``. A tool.call that
carries an input keeps it only when ``input_digest`` is the SHA-256 of that input as RFC 8785
writes it. A registered engine that routing refuses is not covered here: that check is in
routing/config.py.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from recursion_core.routes import driver as d
from recursion_core.routes.tests.test_driver import StandIn, msg, session

_REPO = Path(d.__file__).resolve().parents[3]


class MidTurnRefusalCodeTest(unittest.TestCase):
    def test_f3_mid_turn_refusal_has_the_published_refusal_code(self) -> None:
        driver = StandIn(d.Flags(multi_turn=True))
        handle = driver.start(session(), None, {})
        driver.send(handle, msg())
        with self.assertRaises(d.DriverRefused) as caught:
            driver.send(handle, msg(delivery="dlv-2"))
        schema = json.loads((_REPO / "schemas/control/v1/driver.v2.json").read_text(encoding="utf-8"))
        expected = schema["$defs"]["refusal"]["properties"]["code"]["const"]
        self.assertEqual(caught.exception.as_record()["code"], expected)
        self.assertEqual(caught.exception.as_record()["flag"], "mid_turn_input")
        self.assertEqual(len(driver.written), 1)
        self.assertIsInstance(caught.exception, d.DriverBusy)


class CarriedToolInputTest(unittest.TestCase):
    def test_f4_carried_tool_input_and_digest_cannot_disagree(self) -> None:
        draft = {"type": "tool.call", "payload": {"call_id": "c1", "name": "Read",
                 "input": {}, "input_digest": "0" * 64}}
        with self.assertRaises(d.DriverInputError) as caught:
            d.check_draft(draft)
        self.assertEqual(caught.exception.field, "draft.tool.call.input_digest")

    def test_a_carried_empty_object_keeps_its_own_digest(self) -> None:
        digest = hashlib.sha256(b"{}").hexdigest()
        draft = {"type": "tool.call", "payload": {"call_id": "c1", "name": "Read",
                 "input": {}, "input_digest": digest}}
        checked = d.check_draft(draft)
        self.assertEqual(checked["payload"]["input"], {})
        self.assertEqual(checked["payload"]["input_digest"], digest)

    def test_key_order_does_not_change_the_digest(self) -> None:
        # RFC 8785 sorts these keys as a then b. The digest is of {"a":1,"b":2}, not of the order given.
        digest = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
        draft = {"type": "tool.call", "payload": {"call_id": "c1", "name": "Read",
                 "input": {"b": 2, "a": 1}, "input_digest": digest}}
        checked = d.check_draft(draft)
        self.assertEqual(checked["payload"]["input_digest"], digest)
        swapped = {"type": "tool.call", "payload": {"call_id": "c1", "name": "Read",
                   "input": {"b": 2, "a": 1}, "input_digest": "0" * 64}}
        with self.assertRaises(d.DriverInputError):
            d.check_draft(swapped)

    def test_an_input_that_is_not_carried_keeps_its_digest_unchecked(self) -> None:
        draft = {"type": "tool.call", "payload": {"call_id": "c1", "name": "Read",
                 "input": None, "input_digest": "0" * 64}}
        self.assertEqual(d.check_draft(draft)["payload"]["input"], None)


if __name__ == "__main__":
    unittest.main()
