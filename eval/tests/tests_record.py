"""The recursion-eval-run/1 record: its rules, and agreement with the JSON Schema where jsonschema exists."""
from __future__ import annotations

import copy
import json
import unittest

from eval import PACKAGE_ROOT
from eval import record as record_mod
from eval.tests import build_record, trial

try:
    import jsonschema
except ImportError:  # the free-threaded interpreter has no jsonschema
    jsonschema = None

SCHEMA = json.loads((PACKAGE_ROOT / "schema" / "recursion-eval-run-1.json").read_text(encoding="utf-8"))


def good():
    rows = [trial("a", 1, "pass"), trial("a", 2, "fail"), trial("a", 3, "vendor_limit"),
            trial("a", 4, "pass", planned=False, replacement_for="r.a.3"),
            trial("b", 1, "pass"), trial("b", 2, "pass"), trial("b", 3, "not_run")]
    return build_record(rows, k=2, attempts=3)


def broken():
    """(name, mutate) pairs: each mutation breaks one rule."""
    def set_path(path, value):
        def mutate(record):
            target = record
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        return mutate

    return [
        ("resolved with an agent tier", set_path(["trials", 0, "error"], {"class": "tests_failed", "tier": "agent",
                                                                          "detail": None})),
        ("unresolved with tier none", set_path(["trials", 1, "error"], {"class": None, "tier": "none", "detail": None})),
        ("class in the wrong tier", set_path(["trials", 1, "error"], {"class": "vendor_limit", "tier": "agent",
                                                                      "detail": None})),
        ("not graded with an agent tier", set_path(["trials", 6, "error"], {"class": "timeout", "tier": "agent",
                                                                            "detail": None})),
        ("unknown token as a string", set_path(["trials", 0, "usage", "output"], "0")),
        ("negative token count", set_path(["trials", 0, "usage", "output"], -1)),
        ("cost without unit", set_path(["trials", 0, "usage", "cost"], 1.5)),
        ("planned trial that replaces one", set_path(["trials", 0, "attempt", "replacement_for"], "r.a.3")),
        ("replacement without its original", set_path(["trials", 3, "attempt", "replacement_for"], None)),
        ("unknown label", set_path(["label"], "final")),
        ("unknown field", set_path(["extra"], 1)),
        ("bad sha256", set_path(["suite", "manifest_sha256"], "abc")),
        ("wrong schema id", set_path(["schema"], "recursion-eval-run/2")),
    ]


class RecordRuleTests(unittest.TestCase):
    def test_good_record_is_valid(self):
        self.assertEqual(record_mod.validate(good()), [])

    def test_each_broken_rule_is_reported(self):
        for name, mutate in broken():
            with self.subTest(name):
                record = good()
                mutate(record)
                self.assertNotEqual(record_mod.validate(record), [], name)

    def test_every_planned_trial_has_a_row(self):
        record = good()
        record["trials"] = [t for t in record["trials"] if t["trial_id"] != "r.b.3"]
        problems = record_mod.validate(record)
        self.assertTrue(any("planned attempts" in p for p in problems), problems)

    def test_plan_hash_binds_the_plan(self):
        record = good()
        record["plan"]["k"] = 3
        self.assertTrue(any("plan_sha256" in p for p in record_mod.validate(record)))

    def test_unknown_usage_is_null_not_zero(self):
        empty = record_mod.empty_usage()
        for key in record_mod.TOKEN_FIELDS + ("cost",):
            self.assertIsNone(empty[key])
        self.assertEqual(empty["cost_basis"], "unknown")

    def test_trial_of_a_task_not_in_the_plan(self):
        record = good()
        record["trials"].append(trial("zzz", 1, "pass"))
        self.assertTrue(any("not in the plan" in p for p in record_mod.validate(record)))

    def test_attempts_below_k_refused(self):
        rows = [trial("a", 1, "pass")]
        record = build_record(rows, k=2, attempts=1)
        self.assertTrue(any("below k" in p for p in record_mod.validate(record)))


@unittest.skipIf(jsonschema is None, "jsonschema is not installed in this interpreter")
class SchemaAgreementTests(unittest.TestCase):
    def schema_errors(self, record):
        return list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(record))

    def test_schema_is_a_valid_schema(self):
        jsonschema.Draft202012Validator.check_schema(SCHEMA)

    def test_good_record_passes_the_schema(self):
        self.assertEqual(self.schema_errors(good()), [])

    def test_schema_refuses_what_the_checks_refuse(self):
        # Rules a schema can state; the cross-trial ones (plan hash, rows per plan) are record.py's alone.
        for name, mutate in broken():
            with self.subTest(name):
                record = copy.deepcopy(good())
                mutate(record)
                self.assertNotEqual(self.schema_errors(record), [], name)


if __name__ == "__main__":
    unittest.main()
