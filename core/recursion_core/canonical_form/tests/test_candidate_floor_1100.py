"""CSL-718 (F9): a *candidate* floor for Run@1's two fields, taken from csl-research #1100@114b48be.

Status: candidate, pending acceptance. These rules (the closed 8-value ``RunOutcome`` enum,
the ``StateOutcome`` pairings, ``breaker_tripped`` as a required strict boolean, and #1100's
A3 rule) come from csl-research #1100 at 114b48be2b4f062974b983ab2106fc2d25d53c38, which is
proposed and not accepted. No accepted RP0-v2 test requires them: RP0-v2's accepted material
(AMENDMENT-2026-10-26) names only "Route Contract v0 conformance against the fake driver".
They are NOT the RP0-v2 floor of PRODUCT.md 055cc7a5 (CSLR-D-011) section 1 / A27 / 4.3
(Q13 "with RP0-v2's accepted tests as the floor"). Acceptance goes through C5 / CSL-707.
Whether #1100's two-field rules become the Q13 floor is Lucas's decision; until then this
file only pins this PR's own vectors and their agreement with #1100, and a redesign that
changes these rules should edit this file deliberately rather than treat it as a binding guard.

Related PRODUCT.md 055cc7a5 context: section 4.2 (the fresh contract carries each proposal's
run outcome and the route's breaker state, B15); O49 (canonical digest form). The committed
vector digests must not move without a deliberate edit here.

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
# t1430u X1b: moved from 8038dce7... when the six stopped-run stop.reason vectors were added
# (no existing vector changed its digest or verdict).
VECTOR_SET_SHA256 = "32fb421404790ab8687dff3373b543ccd09107fc68839baafccaa61275b3a7fb"

# Candidate (from #1100@114b48be, not accepted): the RunOutcome enum as #1100 proposes it.
CANDIDATE_OUTCOMES_1100 = frozenset({
    "succeeded", "failed", "timed_out", "cancelled", "stopped", "route_breaker_tripped", "refused", "unknown",
})


def vector_set_digest(vectors: list[dict]) -> str:
    rows = [[v["name"], v["expect"], v.get("sha256", v.get("reason")), v.get("run_errors")] for v in vectors]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


class Candidate1100FloorTests(unittest.TestCase):
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

    def test_outcome_enum_matches_1100_candidate(self) -> None:
        self.assertEqual(CANDIDATE_OUTCOMES_1100, RUN_OUTCOMES)

    def test_breaker_tripped_stays_a_strict_boolean(self) -> None:
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                self.assertEqual(["breaker_tripped_not_boolean"], run_field_errors(vb.run("running", breaker_tripped=value)))

    def test_candidate_refusals_hold(self) -> None:
        # #1100 candidate rules (PRODUCT.md 4.2 / B15 context): a run cannot claim the breaker ended it with the breaker untripped,
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

    def test_digests_match_the_1100_contract(self) -> None:
        by = {v["name"]: v for v in self.vectors}
        for name, sha in vb.UPSTREAM_SHA256.items():
            with self.subTest(name):
                self.assertEqual(sha, by[name]["sha256"])


if __name__ == "__main__":
    unittest.main()
