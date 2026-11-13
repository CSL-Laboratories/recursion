"""Canonical form: malformed money and non-canonical bytes are refused, never coerced."""

from __future__ import annotations

import unittest

from recursion_core.plugin_security import Refused, canonical, parse


class CanonicalTests(unittest.TestCase):
    def test_sorted_compact_ascii(self) -> None:
        self.assertEqual(canonical({"b": [1, True, None], "a": "x"}), b'{"a":"x","b":[1,true,null]}')

    def test_refuses_malformed_values(self) -> None:
        for bad in (1.5, float("nan"), float("inf"), -float("inf"), 2**53, -(2**53), "é",
                    {1: "x"}, {"k": b"bytes"}, {"k": {1, 2}}, object()):
            with self.subTest(bad=repr(bad)), self.assertRaises(Refused) as ctx:
                canonical(bad)
            self.assertEqual(ctx.exception.code, "noncanonical_value")

    def test_parse_refuses_non_canonical_bytes(self) -> None:
        for raw in (b'{"a":1,"a":2}', b'{"a":1.0}', b'{"a":1e3}', b'{"a":NaN}', b'{"a":Infinity}',
                    b'{"a":-0}', b'{"b":1,"a":2}', b'{"a": 1}', b'{"a":9007199254740992}',
                    '{"a":"é"}'.encode(), b'{"a":"\\u00e9"}', b"\xff", b"", "str"):
            with self.subTest(raw=raw), self.assertRaises(Refused):
                parse(raw)

    def test_parse_round_trip(self) -> None:
        value = {"n": -(2**53 - 1), "t": True, "list": [{"x": None}]}
        self.assertEqual(parse(canonical(value)), value)

    def test_bool_is_not_an_int(self) -> None:
        self.assertEqual(canonical(True), b"true")
        self.assertNotEqual(canonical({"n": True}), canonical({"n": 1}))


if __name__ == "__main__":
    unittest.main()
