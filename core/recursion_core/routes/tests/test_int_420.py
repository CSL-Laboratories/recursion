"""Integration entry against #420: an integral JSON number is a count.

event.v1 calls a number an integer when its value is a whole number, so ``0.0`` and
``1e0`` are offsets and token counts. A bool and a fraction are not. A whole float
is stored as that int, the form a consumer can index or count with. A plain int,
including one past 2**53-1, stays that int. A subclass is not a JSON number here.
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
        checked = d.check_draft(draft)
        start = checked["payload"]["offset_start"]
        end = checked["payload"]["offset_end"]
        self.assertEqual(start, 0)
        self.assertEqual(end, 1)
        self.assertIs(type(start), int)
        self.assertIs(type(end), int)
        self.assertEqual("abcdef"[start:end], "a")

    def test_integer_valued_usage_count_is_a_token_count(self) -> None:
        measurement = json.loads('{"value":1e0,"quality":"known","provenance":"observed"}')
        d.check_measurement(measurement, integer=True, field="input_tokens")
        self.assertEqual(measurement["value"], 1)
        self.assertIs(type(measurement["value"]), int)
        self.assertEqual(list(range(measurement["value"] + 1)), [0, 1])

    def test_a_plain_int_past_the_reader_ceiling_stays_that_int(self) -> None:
        huge = 10 ** 400
        measurement = {"value": huge, "quality": "known", "provenance": "observed"}
        d.check_measurement(measurement, integer=True, field="input_tokens")
        self.assertIs(measurement["value"], huge)

    def test_a_number_subclass_is_not_a_json_number_here(self) -> None:
        class Whole(int):
            pass

        class Money(float):
            pass

        for value, integer, field in ((Whole(1), True, "input_tokens"), (Money(1.5), False, "cost_usd")):
            with self.subTest(field=field), self.assertRaises(d.DriverInputError):
                d.check_measurement(
                    {"value": value, "quality": "known", "provenance": "observed"},
                    integer=integer, field=field,
                )

    def test_a_dollar_amount_may_stay_a_fraction(self) -> None:
        measurement = {"value": 1.5, "quality": "known", "provenance": "observed"}
        d.check_measurement(measurement, integer=False, field="cost_usd")
        self.assertIs(type(measurement["value"]), float)
        self.assertEqual(measurement["value"], 1.5)

    def test_nonintegral_numbers_and_booleans_remain_invalid_counts(self) -> None:
        for value in (True, 1.5, -1.0, float("inf")):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                d.check_measurement(
                    {"value": value, "quality": "known", "provenance": "observed"},
                    integer=True, field="input_tokens",
                )


if __name__ == "__main__":
    unittest.main()
