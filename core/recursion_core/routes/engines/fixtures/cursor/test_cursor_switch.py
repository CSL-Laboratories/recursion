# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The unattended switch (routes/engines/cursor.py): data whose one state is off, with no way to turn it on.

What "attended" means is Lucas's open question, so nothing here classifies a turn: these tests hold the
switch to being a read-only record, the refusal to its published shape, and the module to taking no origin
or caller flag anywhere.
"""

from __future__ import annotations

import inspect
import json
import re
import unittest
from pathlib import Path

from recursion_core.routes.engines import cursor

ROOT = Path(__file__).resolve().parents[6]
COMMON = ROOT / "schemas" / "control" / "v1" / "common.v1.json"
RUN_RECORD = ROOT / "schemas" / "control" / "v1" / "run.record.v1.json"


class SwitchTest(unittest.TestCase):
    def test_the_switch_is_off_and_names_its_refusal(self):
        self.assertEqual(cursor.unattended_switch(), {
            "engine": "cursor", "switch": "unattended", "state": "off",
            "refusal": "cursor_unattended_off", "waits_on": "counsel"})

    def test_the_module_record_cannot_be_changed(self):
        with self.assertRaises(TypeError):
            cursor._UNATTENDED["state"] = "on"
        self.assertEqual(cursor.unattended_switch()["state"], "off")

    def test_a_surface_gets_a_copy(self):
        shown = cursor.unattended_switch()
        shown["state"] = "on"
        self.assertEqual(cursor.unattended_switch()["state"], "off")

    def test_no_state_but_off_appears_anywhere_in_the_record(self):
        self.assertEqual([v for v in cursor.unattended_switch().values() if v in ("on", True)], [])

    def test_the_record_is_plain_json(self):
        record = cursor.unattended_switch()
        self.assertEqual(json.loads(json.dumps(record)), record)


class RefusalTest(unittest.TestCase):
    def test_the_refusal_is_a_start_error_with_a_published_code_word(self):
        refusal = cursor.unattended_refusal()
        self.assertEqual(set(refusal), {"code", "detail"})
        record = json.loads(RUN_RECORD.read_text(encoding="utf-8"))
        self.assertEqual(sorted(record["$defs"]["start_error"]["required"]), ["code", "detail"])
        pattern = json.loads(COMMON.read_text(encoding="utf-8"))["$defs"]["code_word"]["pattern"]
        self.assertRegex(refusal["code"], pattern)
        self.assertEqual(refusal["code"], cursor.UNATTENDED_OFF)
        self.assertIsInstance(refusal["detail"], str)
        self.assertNotIn("\n", refusal["detail"])

    def test_the_switch_and_the_refusal_name_one_code(self):
        self.assertEqual(cursor.unattended_switch()["refusal"], cursor.unattended_refusal()["code"])

    def test_each_call_gives_a_new_refusal(self):
        first = cursor.unattended_refusal()
        first["code"] = "changed"
        self.assertEqual(cursor.unattended_refusal()["code"], "cursor_unattended_off")


class NoOriginTest(unittest.TestCase):
    """No public function of the module takes a turn's origin or a caller flag (the start note's rule)."""

    WORDS = re.compile(r"attend|origin|caller|started_by|principal|unattended|trigger|delegat|human|person")

    def test_no_public_callable_takes_an_origin_or_caller_parameter(self):
        found = []
        for name, value in vars(cursor).items():
            if name.startswith("_") or not callable(value) or getattr(value, "__module__", None) != cursor.__name__:
                continue
            targets = [value] if not inspect.isclass(value) else [
                m for n, m in vars(value).items() if callable(m) and not n.startswith("__")]
            for target in targets:
                try:
                    params = inspect.signature(target).parameters
                except (TypeError, ValueError):
                    continue
                found += [f"{name}.{p}" for p in params if self.WORDS.search(p.lower())]
        self.assertEqual(found, [])

    def test_the_switch_functions_take_no_arguments(self):
        self.assertEqual(list(inspect.signature(cursor.unattended_switch).parameters), [])
        self.assertEqual(list(inspect.signature(cursor.unattended_refusal).parameters), [])


if __name__ == "__main__":
    unittest.main()
