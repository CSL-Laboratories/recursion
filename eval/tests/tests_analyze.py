"""The frozen analysis: headline against valid-runs scoring, bounds, usage, timing, paired comparison."""
from __future__ import annotations

import unittest

from eval import analyze
from eval.tests import build_record, trial

USAGE = {"uncached_input": 100, "cache_write": 10, "cache_read": 1000, "output": 50, "cost": 0.5,
         "cost_unit": "USD", "cost_basis": "reported_not_billed", "source": "engine_output"}


class ScoringTests(unittest.TestCase):
    def test_infra_failure_counts_as_failure_in_headline_and_only_leaves_the_valid_score(self):
        # Task a: pass, pass, vendor limit (infra), then a passing replacement. k = 3, 3 planned attempts.
        rows = [trial("a", 1, "pass"), trial("a", 2, "pass"), trial("a", 3, "vendor_limit"),
                trial("a", 4, "pass", planned=False, replacement_for="r.a.3"),
                trial("b", 1, "pass"), trial("b", 2, "pass"), trial("b", 3, "pass")]
        record = build_record(rows, k=3, attempts=3)
        out = analyze.summarize(record, resamples=1000)
        # Headline: a has 2 of 3 -> pass^3 = 0; b 3 of 3 -> 1. Mean 0.5.
        self.assertEqual(out["counts"]["a"]["headline"], [3, 2])
        self.assertEqual(out["headline"]["pass_hat_k"], 0.5)
        # Valid runs: the infra trial is left out and the replacement counts: a 3 of 3 -> 1. Mean 1.0.
        self.assertEqual(out["counts"]["a"]["valid"], [3, 3])
        self.assertEqual(out["valid_runs_only"]["pass_hat_k"], 1.0)
        self.assertEqual(out["valid_runs_only"]["excluded"]["infra"], 1)
        self.assertEqual(out["bounds_pass_hat_k"], {"excluded_as_failed": 0.5, "excluded_as_passed": 1.0})

    def test_replacements_never_enter_the_headline(self):
        rows = [trial("a", 1, "setup_failed"), trial("a", 2, "pass", planned=False, replacement_for="r.a.1"),
                trial("b", 1, "pass")]
        record = build_record(rows, k=1, attempts=1)
        out = analyze.summarize(record, resamples=1000)
        self.assertEqual(out["counts"]["a"]["headline"], [1, 0])
        self.assertEqual(out["headline"]["pass_at_1"], 0.5)
        self.assertEqual(out["valid_runs_only"]["pass_at_1"], 1.0)

    def test_not_run_trials_are_failures_in_headline_and_unknown_in_valid(self):
        rows = [trial("a", 1, "pass"), trial("a", 2, "not_run"), trial("b", 1, "pass"), trial("b", 2, "pass")]
        out = analyze.summarize(build_record(rows, k=2, attempts=2), resamples=1000)
        self.assertEqual(out["headline"]["pass_hat_k"], 0.5)
        self.assertEqual(out["valid_runs_only"]["not_estimable"], ["a"])
        self.assertEqual(out["valid_runs_only"]["pass_hat_k"], 1.0)
        self.assertEqual(out["valid_runs_only"]["excluded"]["unknown"], 1)

    def test_grader_failures_are_excluded_from_valid(self):
        rows = [trial("a", 1, "no_tests_ran"), trial("a", 2, "pass"), trial("b", 1, "pass"), trial("b", 2, "fail")]
        out = analyze.summarize(build_record(rows, k=1, attempts=2), resamples=1000)
        self.assertEqual(out["counts"]["a"]["valid"], [1, 1])
        self.assertEqual(out["valid_runs_only"]["excluded"]["grader"], 1)

    def test_false_done(self):
        rows = [trial("a", 1, "fail", claimed=True), trial("a", 2, "pass"), trial("b", 1, "fail", claimed=False),
                trial("b", 2, "fail", claimed=None)]
        out = analyze.summarize(build_record(rows, k=1, attempts=2), resamples=1000)
        self.assertEqual(out["false_done"], {"claimed_done": 2, "false_done": 1, "rate": 0.5, "claim_unknown": 1})

    def test_unknown_usage_is_counted_unknown_never_zero(self):
        rows = [trial("a", 1, "pass", usage=dict(USAGE)), trial("a", 2, "pass"),
                trial("b", 1, "pass", usage=dict(USAGE, output=None)), trial("b", 2, "not_run")]
        out = analyze.summarize(build_record(rows, k=1, attempts=2), resamples=1000)
        self.assertEqual(out["usage"]["output"], {"sum_of_known": 50, "known": 1, "unknown": 2})
        self.assertEqual(out["usage"]["cache_read"], {"sum_of_known": 2000, "known": 2, "unknown": 1})
        self.assertIsNone(out["usage"]["cost_per_resolved"])  # one trial's cost is unknown
        all_unknown = analyze.summarize(build_record([trial("a", 1, "pass"), trial("b", 1, "pass")], k=1,
                                                     attempts=1), resamples=1000)
        self.assertIsNone(all_unknown["usage"]["output"]["sum_of_known"])

    def test_cost_per_resolved_counts_every_attempt(self):
        rows = [trial("a", 1, "pass", usage=dict(USAGE)), trial("a", 2, "fail", usage=dict(USAGE)),
                trial("b", 1, "pass", usage=dict(USAGE)), trial("b", 2, "pass", usage=dict(USAGE))]
        out = analyze.summarize(build_record(rows, k=1, attempts=2), resamples=1000)
        self.assertAlmostEqual(out["usage"]["cost_per_resolved"], 2.0 / 3)

    def test_wall_time_percentiles(self):
        rows = [trial("a", i, "pass" if i % 2 else "fail", wall=float(i)) for i in range(1, 11)]
        rows += [trial("b", i, "pass", wall=100.0) for i in range(1, 11)]
        out = analyze.summarize(build_record(rows, k=1, attempts=10), resamples=1000)
        self.assertEqual(out["wall_seconds"]["all"]["p50"], 10.0)
        self.assertEqual(out["wall_seconds"]["all"]["p95"], 100.0)
        self.assertEqual(analyze.percentile([1, 2, 3, 4], 50), 2)
        self.assertEqual(analyze.percentile([], 50), None)

    def test_first_attempt_rate(self):
        rows = [trial("a", 1, "fail"), trial("a", 2, "pass"), trial("b", 1, "pass"), trial("b", 2, "pass")]
        out = analyze.summarize(build_record(rows, k=1, attempts=2), resamples=1000)
        self.assertEqual(out["headline"]["first_attempt"], 0.5)
        self.assertEqual(out["headline"]["pass_at_1"], 0.75)


class CompareTests(unittest.TestCase):
    def pair(self):
        tasks = [f"t{i}" for i in range(8)]
        rows_a, rows_b = [], []
        for n, t in enumerate(tasks):
            for i in (1, 2):
                rows_a.append(trial(t, i, "pass" if n < 6 else "fail", record_id="A"))
                rows_b.append(trial(t, i, "pass" if n < 4 else "fail", record_id="B"))
        a = build_record(rows_a, k=2, attempts=2, record_id="A")
        b = build_record(rows_b, k=2, attempts=2, record_id="B", arm="recursion")
        return a, b

    def test_two_runs_of_one_suite_give_a_paired_task_level_interval(self):
        a, b = self.pair()
        out = analyze.compare(a, b, resamples=2000, seed=11)
        result = out["pass_hat_k"]
        self.assertEqual(result["tasks"], 8)
        self.assertEqual(result["mean_difference"], -0.25)
        low, high = result["bootstrap"]
        self.assertLessEqual(low, -0.25)
        self.assertGreaterEqual(high, -0.25)
        self.assertIn("DEV RUN", result["claim"])
        self.assertIsNotNone(out["full_run_size_estimate"]["tasks_for_10_points"])

    def test_different_suites_refused(self):
        a, b = self.pair()
        rows = b["trials"]
        other = build_record(rows, k=2, attempts=2, record_id="B", manifest_sha="1" * 64)
        with self.assertRaises(ValueError):
            analyze.compare(a, other, resamples=1000)

    def test_different_k_refused(self):
        a, b = self.pair()
        rows = b["trials"]
        other = build_record(rows, k=1, attempts=2, record_id="B")
        with self.assertRaises(ValueError):
            analyze.compare(a, other, resamples=1000)


class ClaimTests(unittest.TestCase):
    def test_interval_across_zero_is_no_detectable_difference(self):
        text = analyze.claim("pilot", (-0.12, 0.05))
        self.assertIn("no detectable difference", text)
        self.assertIn("not equivalence", text)
        self.assertIn("12 points", text)
        self.assertTrue(text.startswith("PILOT"))

    def test_pilot_never_claims_superiority(self):
        text = analyze.claim("pilot", (0.02, 0.2))
        self.assertIn("no superiority claim", text)
        self.assertNotIn("higher", text)

    def test_full_comparison_may_say_higher(self):
        self.assertIn("higher", analyze.claim("full", (0.02, 0.2)))


if __name__ == "__main__":
    unittest.main()
