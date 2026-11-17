"""Integration entry against #420: an integral JSON number is a count.

event.v1 calls a number an integer when its value is a whole number, so ``0.0`` and
``1e0`` are offsets and token counts. A bool and a fraction are not. The value is
kept as it was given.
"""

from __future__ import annotations

import json
import unittest

from recursion_core.routes import driver as d


class IntegralJsonNumberTest(unittest.TestCase):
    def test_integer_valued_json_numbers_fit_the_draft_reader(self) -> None:
        draft = json.loads(
            '{"type":"engine.output","payload":{"stream":"stdout","text":"x",'
            '"offset_start":0.0,"offset_end":1e0}}'
        )
        self.assertEqual(d.check_draft(draft), draft)

    def test_integer_valued_usage_count_is_a_token_count(self) -> None:
        measurement = json.loads('{"value":1e0,"quality":"known","provenance":"observed"}')
        d.check_measurement(measurement, integer=True, field="input_tokens")
        self.assertIs(type(measurement["value"]), float)

    def test_nonintegral_numbers_and_booleans_remain_invalid_counts(self) -> None:
        for value in (True, 1.5, -1.0, float("inf")):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                d.check_measurement(
                    {"value": value, "quality": "known", "provenance": "observed"},
                    integer=True, field="input_tokens",
                )


if __name__ == "__main__":
    unittest.main()
