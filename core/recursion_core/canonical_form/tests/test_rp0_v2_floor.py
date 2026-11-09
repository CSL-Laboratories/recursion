"""CSL-718 (F9): the RP0-v2 floor the Q13 schema redesign must keep, for Run@1's two fields.

PRODUCT.md 055cc7a5 (CSLR-D-011): section 1 and A27 (RP0-v2 section 5.2's RP2 verifier
names the vNext contract; grill Q13 redesigns every schema "with RP0-v2's accepted tests as
the floor"); section 4.2 (the fresh contract carries each proposal's run outcome and the
route's breaker state, B15); section 4.3 (Q13 "Redesign, starting from what exists"); O49
(canonical digest form). A later Run@ version may add fields; it must not drop or loosen
these two, and the committed vector digests must not move without a deliberate edit here.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/canonical_form/tests -t core -v``.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from recursion_core.canonical_form import RUN_OUTCOMES, RUN_REQUIRED, TWO_FIELDS, digest, loads_strict, run_field_errors
from recursion_core.canonical_form import vectors_build as vb

# Digest over (name, expect, sha256 | reason, run_errors) of every committed vector.
# Changing a vector's digest or verdict must change this constant in the same commit.
VECTOR_SET_SHA256 = "8038dce7d06ae87b16f579f80d62c3eb64f56ef1bf4087b97af7ab9053b6f375"

FLOOR_OUTCOMES = frozenset({
    "succeeded", "failed", "timed_out", "cancelled", "stopped", "route_breaker_tripped", "refused", "unknown",
})


def vector_set_digest(vectors: list[dict]) -> str:
    rows = [[v["name"], v["expect"], v.get("sha256", v.get("reason")), v.get("run_errors")] for v in vectors]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


class RP0V2FloorTests(unittest.TestCase):
    def setUp(self) -> None:
        with open(vb.VECTORS, encoding="utf-8") as f:
            self.vectors = json.load(f)["vectors"]

    def test_both_fields_stay_required(self) -> None:
        for field in TWO_FIELDS:
            self.assertIn(field, RUN_REQUIRED)
        for state in ("pending", "running", "stopping", *vb.OUTCOME):
            for field in TWO_FIELDS:
                with self.subTest(state=state, field=field):
                    run = vb.run(state)
                    self.assertEqual([], run_field_errors(run))
                    del run[field]
                    self.assertIn(f"{field}_required", run_field_errors(run))

    def test_outcome_enum_is_closed_and_not_narrowed(self) -> None:
        self.assertEqual(FLOOR_OUTCOMES, RUN_OUTCOMES)

    def test_breaker_tripped_stays_a_strict_boolean(self) -> None:
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                self.assertEqual(["breaker_tripped_not_boolean"], run_field_errors(vb.run("running", breaker_tripped=value)))

    def test_floor_refusals_hold(self) -> None:
        # PRODUCT.md 4.2 / B15: a run cannot claim the breaker ended it with the breaker untripped,
        # own timeouts are never breaker trips (F2), and unknown never pretends success.
        self.assertIn("breaker_outcome_without_breaker", run_field_errors(vb.run("limit_hit", breaker_tripped=False)))
        self.assertTrue(run_field_errors(vb.run("timed_out", outcome="route_breaker_tripped", breaker_tripped=True)))
        self.assertTrue(run_field_errors(vb.run("completed", outcome="unknown")))

    def test_committed_vector_digests_are_stable(self) -> None:
        self.assertEqual(VECTOR_SET_SHA256, vector_set_digest(self.vectors))
        for vec in self.vectors:
            if vec["expect"] == "ok":
                with self.subTest(vec["name"]):
                    self.assertEqual(vec["sha256"], digest(loads_strict(vec["input"])))

    def test_floor_digests_match_the_1100_contract(self) -> None:
        by = {v["name"]: v for v in self.vectors}
        for name, sha in vb.UPSTREAM_SHA256.items():
            with self.subTest(name):
                self.assertEqual(sha, by[name]["sha256"])


if __name__ == "__main__":
    unittest.main()
