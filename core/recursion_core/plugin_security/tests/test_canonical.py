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


# RFC 8785 section 3.2.2.2: these five C0 controls take the two-character escape, every
# other C0 control the six-character escape in lowercase hex, and the solidus is written bare.
SHORT_ESCAPES = {0x08: b"\\b", 0x09: b"\\t", 0x0A: b"\\n", 0x0C: b"\\f", 0x0D: b"\\r"}


class ControlCharacterTests(unittest.TestCase):
    def assert_control_refusal(self, call, arg) -> None:
        with self.assertRaises(Refused) as ctx:
            call(arg)
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("noncanonical_value", "control character"))

    def test_canonical_refuses_a_string_holding_delete(self) -> None:
        for bad in ("\x7f", "a\x7fb", {"k": "x\x7f"}, ["\x7f"], {"k": ["ok", "\x7f"]}):
            with self.subTest(bad=repr(bad)):
                self.assert_control_refusal(canonical, bad)

    def test_canonical_refuses_a_key_holding_delete(self) -> None:
        for bad in ({"\x7f": 1}, {"a\x7fb": "x"}, {"k": {"\x7f": None}}):
            with self.subTest(bad=repr(bad)):
                self.assert_control_refusal(canonical, bad)

    def test_parse_refuses_delete_written_as_a_six_character_escape(self) -> None:
        for raw in (b'"\\u007f"', b'{"a":"x\\u007fy"}', b'["\\u007f"]'):
            with self.subTest(raw=raw):
                self.assert_control_refusal(parse, raw)

    def test_parse_refuses_a_key_holding_delete_as_a_six_character_escape(self) -> None:
        for raw in (b'{"\\u007f":1}', b'{"a\\u007fb":"x"}'):
            with self.subTest(raw=raw):
                self.assert_control_refusal(parse, raw)

    def test_parse_refuses_delete_as_the_raw_byte(self) -> None:
        # The raw byte 0x7F is how RFC 8785 itself writes the character.
        for raw in (b'"\x7f"', b'{"a":"x\x7fy"}', b'{"a\x7fb":1}'):
            with self.subTest(raw=raw):
                self.assert_control_refusal(parse, raw)

    def test_parse_refuses_delete_as_an_upper_case_escape(self) -> None:
        for raw in (b'"\\u007F"', b'{"a":"x\\u007Fy"}', b'{"\\u007F":1}'):
            with self.subTest(raw=raw):
                self.assert_control_refusal(parse, raw)

    def test_every_ascii_character_is_written_as_rfc_8785_writes_it_or_refused(self) -> None:
        # Every C0 control takes its RFC 8785 escape, the solidus stays bare, and DEL (which
        # RFC 8785 writes raw and json.dumps escapes) is refused rather than written differently.
        for code in range(0x80):
            char = chr(code)
            with self.subTest(code=code):
                if code == 0x7F:
                    self.assert_control_refusal(canonical, char)
                    self.assert_control_refusal(canonical, {char: char})
                    continue
                if code < 0x20:
                    body = SHORT_ESCAPES.get(code, b"\\u%04x" % code)
                else:
                    body = {0x22: b'\\"', 0x5C: b"\\\\"}.get(code, bytes([code]))
                expected = b'"' + body + b'"'
                self.assertEqual(canonical(char), expected)
                self.assertEqual(canonical({char: char}), b"{" + expected + b":" + expected + b"}")
                self.assertEqual(parse(expected), char)

    def test_the_solidus_is_never_escaped(self) -> None:
        self.assertEqual(canonical({"/": "a/b"}), b'{"/":"a/b"}')
        self.assertEqual(parse(b'"a/b"'), "a/b")
        with self.assertRaises(Refused):
            parse(b'"a\\/b"')
        self.assert_control_refusal(canonical, "a/\x7f")


if __name__ == "__main__":
    unittest.main()
