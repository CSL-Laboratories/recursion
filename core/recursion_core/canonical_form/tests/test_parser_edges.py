"""Parser edges for csl-canonical/1, including where it meets PR #231's wire decoder.

Defects here fail until canonical.py refuses them as the kind the profile already
names. The owner-question tests lock today's behaviour on purpose: they are not
a decision that the behaviour is the rule.
"""

from __future__ import annotations

import sys
import unittest

from recursion_core.canonical_form import vectors_build as vb
from recursion_core.canonical_form.canonical import (
    CanonicalError,
    canonical_bytes,
    digest_text,
    loads_strict,
)
from recursion_core.canonical_form.run_fields import run_field_errors


class CanonicalDefectTests(unittest.TestCase):
    def test_integer_past_the_converter_digit_limit_is_integer_range(self) -> None:
        # CPython's default int() limit is 4300 digits. A longer JSON integer is
        # still outside ±(2^53-1); reporting it as not_json disagrees with every
        # shorter out-of-range integer, and with a decoder whose limit is higher.
        old = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        try:
            for text in ("9" * 4301, "-" + "9" * 4301, "9" * 5000):
                with self.subTest(text=text[:8] + f"…({len(text)})"):
                    with self.assertRaises(CanonicalError) as cm:
                        loads_strict(text)
                    self.assertEqual("integer_range", cm.exception.kind)
                    self.assertEqual("canonical_form_invalid", cm.exception.code)
            # The length guard must not swallow a 16-digit in-range value, nor
            # let a 16-digit value past 2^53-1 through.
            self.assertEqual(9007199254740991, loads_strict("9007199254740991"))
            with self.assertRaises(CanonicalError) as cm:
                loads_strict("9007199254740992")
            self.assertEqual("integer_range", cm.exception.kind)
        finally:
            sys.set_int_max_str_digits(old)

    def test_decoder_recursion_on_deep_nesting_is_depth(self) -> None:
        # Nesting the C decoder cannot walk is still nesting past the profile's
        # depth rule. It must not surface as not_json, and it must not crash.
        text = "[" * 20000 + "]" * 20000
        with self.assertRaises(CanonicalError) as cm:
            loads_strict(text)
        self.assertEqual("depth", cm.exception.kind)

    def test_non_text_input_is_refused_not_a_crash(self) -> None:
        for bad in (None, 1, 1.0, bytearray(b"0"), memoryview(b"0"), object()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(CanonicalError) as cm:
                    loads_strict(bad)  # type: ignore[arg-type]
                self.assertEqual("not_json", cm.exception.kind)
        self.assertEqual(0, loads_strict(b"0"))
        self.assertEqual(0, loads_strict("0"))


class WireProfileAgreementTests(unittest.TestCase):
    """Lexical refusals both profiles already state. -0 is not in this list."""

    def test_shared_lexical_refusals(self) -> None:
        cases = {
            "1.0": "float",
            "1e2": "float",
            "2e0": "float",
            "-0.0": "float",
            "NaN": "non_finite",
            "Infinity": "non_finite",
            "-Infinity": "non_finite",
            '{"a":1,"\\u0061":2}': "duplicate_key",
            '"\\ud800"': "lone_surrogate",
            '"\\ud800\\ud800"': "lone_surrogate",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(CanonicalError) as cm:
                    loads_strict(text)
                self.assertEqual(kind, cm.exception.kind)
        with self.assertRaises(CanonicalError) as cm:
            loads_strict(b"\xff")
        self.assertEqual("not_utf8", cm.exception.kind)
        with self.assertRaises(CanonicalError) as cm:
            loads_strict(b"\xef\xbb\xbf{}")
        self.assertEqual("bom", cm.exception.kind)


class OwnerQuestionLocks(unittest.TestCase):
    def test_lexical_negative_zero_currently_matches_zero(self) -> None:
        # Owner question: PR #231's I-JSON decoder rejects lexical -0. RFC 8785
        # serialises minus zero as 0, and errata 7920 says a parser SHOULD error
        # on -0. This profile currently accepts -0 and folds it to 0, so the
        # digests match. Not changed here.
        self.assertEqual(0, loads_strict("-0"))
        self.assertEqual(b"0", canonical_bytes(loads_strict("-0")))
        self.assertEqual(digest_text("-0"), digest_text("0"))
        self.assertEqual(digest_text("0"), digest_text("  -0 \n"))

    def test_line_and_paragraph_separators_are_currently_unescaped(self) -> None:
        # Owner question: RFC 8785 points at ECMAScript JSON.stringify. ES6
        # leaves U+2028 and U+2029 raw; ES2019 and later escape them. CPython
        # 3.12 json.dumps and this profile both leave them raw. Not changed.
        self.assertEqual(b'"\xe2\x80\xa8"', canonical_bytes("\u2028"))
        self.assertEqual(b'"\xe2\x80\xa9"', canonical_bytes("\u2029"))
        self.assertEqual("\u2028", loads_strict(canonical_bytes("\u2028")))
        self.assertEqual(digest_text('"\u2028"'), digest_text('"\\u2028"'))

    def test_depth_counts_empty_containers_one_level_deeper_than_scalars(self) -> None:
        # Owner question: the root is depth 0 and the check is depth > 128, so
        # 129 nested empty arrays are in domain and 129 arrays around a scalar
        # are not. The prose says "nesting deeper than 128". Not changed.
        def innermost(text: str, levels: int):
            value = loads_strict(text)
            for _ in range(levels):
                self.assertIsInstance(value, list)
                value = value[0]
            return value

        self.assertEqual([], innermost("[" * 129 + "]" * 129, 128))
        with self.assertRaises(CanonicalError) as cm:
            loads_strict("[" * 130 + "]" * 130)
        self.assertEqual("depth", cm.exception.kind)
        self.assertEqual(0, innermost("[" * 128 + "0" + "]" * 128, 128))
        with self.assertRaises(CanonicalError) as cm:
            loads_strict("[" * 129 + "0" + "]" * 129)
        self.assertEqual("depth", cm.exception.kind)
        self.assertEqual({}, innermost("[" * 128 + "{}" + "]" * 128, 128))
        with self.assertRaises(CanonicalError) as cm:
            loads_strict("[" * 128 + '{"a":1}' + "]" * 128)
        self.assertEqual("depth", cm.exception.kind)

    def test_stopped_run_currently_ignores_who_requested_the_stop(self) -> None:
        # Owner question: the comment in run_fields.py cites #1100's rule that a
        # stopped run's stop.requested_by is "core". The checker never reads
        # requested_by or confirmed_at. A caller stop that copies a core reason
        # string is currently consistent, including a breaker trip. Not changed.
        for reason in ("kill_switch", "breaker"):
            base = vb.run("stopped", stop_reason=reason)
            self.assertEqual([], run_field_errors(base))
            for requester in ("caller", "plugin", "operator", "", None):
                doc = {**base, "stop": {**base["stop"], "requested_by": requester}}
                self.assertEqual([], run_field_errors(doc), (reason, requester))
            missing = {k: v for k, v in base["stop"].items() if k != "requested_by"}
            self.assertEqual([], run_field_errors({**base, "stop": missing}))
            unconfirmed = {**base["stop"], "confirmed_at": None}
            self.assertEqual([], run_field_errors({**base, "stop": unconfirmed}))


if __name__ == "__main__":
    unittest.main()
