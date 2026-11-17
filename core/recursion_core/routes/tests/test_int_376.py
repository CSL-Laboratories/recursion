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
from collections.abc import Mapping
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


class _IterRaises(dict):
    """A dict whose walk raises. Nested in a plain dict, the canonicaliser meets it."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(a=1)
        self._exc = exc

    def __iter__(self):
        raise self._exc


class _MappingRaises(Mapping):
    """Not a dict, so the check builds one and meets the raise there."""

    def __init__(self, exc: BaseException, *, on_item: bool = False) -> None:
        self._exc = exc
        self._on_item = on_item

    def __iter__(self):
        if not self._on_item:
            raise self._exc
        yield "a"

    def __len__(self) -> int:
        return 1 if self._on_item else 0

    def __getitem__(self, key: object) -> object:
        raise self._exc


def _carried(value: object) -> dict:
    return {"type": "tool.call", "payload": {"call_id": "c1", "name": "Read",
            "input": value, "input_digest": "0" * 64}}


class CarriedInputFailureTest(unittest.TestCase):
    def test_an_ordinary_failure_while_reading_a_carried_input_is_refused(self) -> None:
        cases = (
            ("value_error", {"nested": _IterRaises(ValueError("boom"))}),
            ("key_error", {"nested": _IterRaises(KeyError("k"))}),
            ("memory", {"nested": _IterRaises(MemoryError())}),
            ("mapping_key", _MappingRaises(KeyError("k"))),
            ("mapping_value", _MappingRaises(ValueError("boom"), on_item=True)),
        )
        for label, carried in cases:
            with self.subTest(label=label):
                with self.assertRaises(d.DriverInputError) as caught:
                    d.check_draft(_carried(carried))
                self.assertEqual(caught.exception.field, "draft.tool.call.input")

    def test_an_interruption_while_reading_a_carried_input_still_leaves(self) -> None:
        for exc in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc.__name__):
                with self.assertRaises(exc):
                    d.check_draft(_carried({"nested": _IterRaises(exc())}))


if __name__ == "__main__":
    unittest.main()
