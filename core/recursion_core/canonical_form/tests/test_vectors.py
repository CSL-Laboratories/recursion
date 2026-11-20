"""CSL-718 (F9): canonical form and the Run@1 two-field vectors (candidate tests).

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/canonical_form/tests -t core -v``.
Standard library only; no network, engine, provider or venue.
"""

from __future__ import annotations

import hashlib
import json
import math
import unittest

from recursion_core.canonical_form import (
    CanonicalError,
    canonical_bytes,
    digest,
    digest_text,
    is_full_digest,
    loads_strict,
    run_field_errors,
)
from recursion_core.canonical_form import vectors_build as vb
from recursion_core.canonical_form.canonical import KINDS
from recursion_core.canonical_form.run_fields import RULES


def load_vectors() -> dict:
    with open(vb.VECTORS, encoding="utf-8") as f:
        return json.load(f)


def vector_input(vec: dict) -> str | bytes:
    return vec["input"] if "input" in vec else bytes.fromhex(vec["input_utf8_hex"])


class VectorFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = load_vectors()
        self.by = {v["name"]: v for v in self.doc["vectors"]}

    def test_file_is_what_the_builder_writes(self) -> None:
        with open(vb.VECTORS, encoding="utf-8") as f:
            self.assertEqual(vb.render(vb.build()), f.read(), "rebuild with vectors_build --write")

    def test_file_is_itself_in_the_canonical_domain(self) -> None:
        with open(vb.VECTORS, "rb") as f:
            loads_strict(f.read())

    def test_format_header(self) -> None:
        self.assertEqual("csl-canonical-vectors/1", self.doc["format"])
        self.assertEqual("csl-canonical/1", self.doc["canonical_form"])
        self.assertIn("#1100@114b48be", self.doc["source"])
        names = [v["name"] for v in self.doc["vectors"]]
        self.assertEqual(len(names), len(set(names)), "vector names are unique")

    def test_every_vector_has_a_known_shape(self) -> None:
        for vec in self.doc["vectors"]:
            with self.subTest(vec["name"]):
                self.assertEqual(1, ("input" in vec) + ("input_utf8_hex" in vec))
                self.assertTrue(vec["note"])
                if vec["expect"] == "ok":
                    self.assertTrue(is_full_digest(vec["sha256"]))
                    self.assertEqual(vec["canonical"].encode("utf-8").hex(), vec["canonical_utf8_hex"])
                    self.assertEqual(hashlib.sha256(bytes.fromhex(vec["canonical_utf8_hex"])).hexdigest(), vec["sha256"])
                    self.assertTrue(set(vec["run_errors"]) <= set(RULES), vec["run_errors"])
                    self.assertEqual(sorted(vec["run_errors"]), vec["run_errors"])
                else:
                    self.assertEqual("reject", vec["expect"])
                    self.assertIn(vec["reason"], KINDS)

    def test_accepted_vectors_reproduce(self) -> None:
        for vec in self.doc["vectors"]:
            if vec["expect"] != "ok":
                continue
            with self.subTest(vec["name"]):
                value = loads_strict(vector_input(vec))
                self.assertEqual(vec["canonical"].encode("utf-8"), canonical_bytes(value))
                self.assertEqual(vec["sha256"], digest(value))
                self.assertEqual(vec["sha256"], digest_text(vector_input(vec)))
                self.assertEqual(vec["run_errors"], run_field_errors(value))
                # canonical text is a fixed point
                self.assertEqual(vec["sha256"], digest_text(vec["canonical"]))

    def test_refused_vectors_refuse_with_their_kind(self) -> None:
        for vec in self.doc["vectors"]:
            if vec["expect"] != "reject":
                continue
            with self.subTest(vec["name"]):
                with self.assertRaises(CanonicalError) as cm:
                    loads_strict(vector_input(vec))
                self.assertEqual(vec["reason"], cm.exception.kind)
                self.assertEqual("canonical_form_invalid", cm.exception.code)

    def test_upstream_vectors_keep_their_1100_digests(self) -> None:
        for name, sha in vb.UPSTREAM_SHA256.items():
            with self.subTest(name):
                self.assertEqual(name, self.by[name]["upstream"])
                self.assertEqual(sha, self.by[name]["sha256"])
        self.assertEqual(("reject", "duplicate_key"),
                         (self.by["doc_run_outcome_duplicate"]["expect"], self.by["doc_run_outcome_duplicate"]["reason"]))


class TwoFieldInteractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.by = {v["name"]: v for v in load_vectors()["vectors"]}

    def test_key_order_whitespace_and_escapes_do_not_change_the_digest(self) -> None:
        base = self.by["doc_run_outcome_breaker"]["sha256"]
        for name in ("doc_run_outcome_breaker_reordered", "doc_run_outcome_escaped_key"):
            with self.subTest(name):
                self.assertEqual(base, self.by[name]["sha256"])
                self.assertEqual(self.by["doc_run_outcome_breaker"]["canonical"], self.by[name]["canonical"])

    def test_each_field_changes_the_digest(self) -> None:
        self.assertNotEqual(self.by["doc_run_outcome_breaker"]["sha256"],
                            self.by["doc_run_outcome_breaker_not_tripped"]["sha256"])
        # 1, "true" and true are three different documents
        shas = {self.by[n]["sha256"] for n in ("run_running_breaker_tripped", "run_breaker_tripped_int",
                                                 "run_breaker_tripped_string", "run_breaker_tripped_null")}
        self.assertEqual(4, len(shas))
        # an absent outcome is not the same document as a null one
        self.assertNotEqual(self.by["run_running_null_outcome"]["sha256"], self.by["run_outcome_absent"]["sha256"])

    def test_breaker_outcome_needs_breaker_tripped(self) -> None:
        run = vb.run("limit_hit")
        self.assertEqual([], run_field_errors(run))
        run["breaker_tripped"] = False
        self.assertEqual(["breaker_outcome_without_breaker"], run_field_errors(run))
        run = vb.run("stopped", stop_reason="breaker")
        self.assertEqual([], run_field_errors(run))
        del run["breaker_tripped"]
        self.assertIn("breaker_outcome_without_breaker", run_field_errors(run))
        self.assertIn("breaker_tripped_required", run_field_errors(run))

    def test_breaker_tripped_does_not_force_the_outcome(self) -> None:
        for state in ("running", "completed", "failed", "cancelled", "timed_out", "refused"):
            with self.subTest(state=state):
                self.assertEqual([], run_field_errors(vb.run(state, breaker_tripped=True)))

    def test_null_outcome_exactly_while_not_terminal(self) -> None:
        for state in ("pending", "running", "stopping"):
            with self.subTest(state=state):
                self.assertEqual([], run_field_errors(vb.run(state)))
                for outcome in sorted(vb.OUTCOME.values()) + ["unknown"]:
                    self.assertEqual(["outcome_not_null"],
                                     run_field_errors(vb.run(state, outcome=outcome, breaker_tripped=True)))
        for state in vb.OUTCOME:
            with self.subTest(state=state):
                self.assertEqual(["outcome_null_terminal"], run_field_errors(vb.run(state, outcome=None)))

    def test_state_outside_the_enum_is_never_consistent(self) -> None:
        # Red-team #339 finding 1: a state outside RUN_STATES used to skip both outcome checks.
        for state in ("Completed", "COMPLETED", " completed", "done", "", None, 1, 0, True, 1.5, [], ["completed"],
                      {}, {"state": "completed"}):
            for outcome in (None, "succeeded", "route_breaker_tripped", "unknown"):
                with self.subTest(state=state, outcome=outcome):
                    run = {**vb.run("limit_hit"), "state": state, "outcome": outcome}
                    self.assertIn("state_not_in_enum", run_field_errors(run))
        run = vb.run("completed")
        del run["state"]
        self.assertEqual(["missing_required", "state_not_in_enum"], run_field_errors(run))
        for name in ("run_state_wrong_case", "run_state_null", "run_state_int_breaker", "run_state_array",
                     "run_state_null_outcome_null", "run_state_absent"):
            with self.subTest(name):
                self.assertIn("state_not_in_enum", self.by[name]["run_errors"])

    # t1430u X1b: a stopped run with a missing or unknown stop.reason used to pass with any
    # outcome stopped allows; the outcome cannot follow a reason that is not there.
    def test_stopped_without_a_core_stop_reason_is_refused(self) -> None:
        base = vb.run("stopped")
        stops = [None, {}, {k: v for k, v in base["stop"].items() if k != "reason"}]
        stops += [{**base["stop"], "reason": r} for r in (None, "", "Breaker", "operator_whim", "caller_stop", 1, ["breaker"])]
        for stop in stops:
            for outcome in ("stopped", "unknown", "route_breaker_tripped"):
                with self.subTest(stop=stop, outcome=outcome):
                    doc = {**base, "stop": stop, "outcome": outcome, "breaker_tripped": True}
                    self.assertEqual(["stop_reason_not_core"], run_field_errors(doc))
        for reason in ("kill_switch", "daemon_shutdown", "budget_cap", "breaker", "orphan_sweep"):
            with self.subTest(reason=reason):
                self.assertEqual([], run_field_errors(vb.run("stopped", stop_reason=reason)))
        for name in ("run_stopped_reason_absent", "run_stopped_reason_unknown", "run_stopped_reason_null",
                     "run_stopped_reason_caller_stop", "run_stopped_stop_null",
                     "run_stopped_reason_unknown_as_breaker"):
            with self.subTest(name):
                self.assertEqual(["stop_reason_not_core"], self.by[name]["run_errors"])

    def test_every_run_state_is_checked(self) -> None:
        for state in sorted(vb.OUTCOME) + ["pending", "running", "stopping"]:
            with self.subTest(state=state):
                self.assertNotIn("state_not_in_enum", run_field_errors(vb.run(state)))

    def test_duplicate_two_field_keys_never_last_win(self) -> None:
        for name in ("doc_run_outcome_duplicate", "dup_breaker_tripped", "dup_outcome_via_escape"):
            with self.subTest(name):
                self.assertEqual("duplicate_key", self.by[name]["reason"])
        # last-wins would turn the A3-violating false into an accepted true
        last_wins = json.loads(self.by["dup_breaker_tripped"]["input"])
        self.assertEqual([], run_field_errors(last_wins))

    def test_in_memory_non_finite_and_float_refused(self) -> None:
        for bad, kind in ((math.nan, "non_finite"), (math.inf, "non_finite"), (-math.inf, "non_finite"),
                          (1.0, "float"), (2**53, "integer_range")):
            with self.subTest(bad=bad):
                run = vb.run("running", breaker_tripped=bad)
                with self.assertRaises(CanonicalError) as cm:
                    digest(run)
                self.assertEqual(kind, cm.exception.kind)

    def test_utf16_key_order(self) -> None:
        # RFC 8785 3.2.3: U+1F600 (D83D DE00) sorts before U+FB33 in UTF-16 code units.
        self.assertEqual('{"\U0001f600":1,"דּ":2}'.encode("utf-8"),
                         canonical_bytes({"דּ": 2, "\U0001f600": 1}))


if __name__ == "__main__":
    unittest.main()
