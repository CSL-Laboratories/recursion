"""The calculator: per-task counts, not-estimable tasks, task-level and paired intervals."""
from __future__ import annotations

import math
import unittest
from fractions import Fraction

from eval import passk
from eval.passk import Counts


class PassHatKTests(unittest.TestCase):
    def test_hand_computed_on_unequal_trial_counts(self):
        # Task a: 3 of 5 passed; task b: 6 of 7; task c: 2 of 2. k = 2.
        # a: C(3,2)/C(5,2) = 3/10. b: C(6,2)/C(7,2) = 15/21 = 5/7. c: C(2,2)/C(2,2) = 1.
        counts = {"a": Counts(5, 3), "b": Counts(7, 6), "c": Counts(2, 2)}
        value = passk.suite_metric(counts, lambda c: passk.pass_hat_k(c, 2), allow_not_estimable=False)
        self.assertEqual(value.per_task, {"a": Fraction(3, 10), "b": Fraction(5, 7), "c": Fraction(1)})
        self.assertEqual(value.value, (Fraction(3, 10) + Fraction(5, 7) + 1) / 3)
        self.assertEqual(value.value, Fraction(141, 210))
        # The shared-denominator error the contract warns about gives a different number.
        shared_n = max(c.n for c in counts.values())
        wrong = sum(Fraction(math.comb(c.c, 2), math.comb(shared_n, 2)) for c in counts.values()) / 3
        self.assertNotEqual(value.value, wrong)

    def test_product_form_agrees(self):
        # pass^k = prod_{i<k} (c-i)/(n-i)
        for n, c, k in [(10, 7, 5), (5, 5, 5), (6, 2, 3), (12, 11, 4)]:
            product = Fraction(1)
            for i in range(k):
                product *= Fraction(c - i, n - i)
            self.assertEqual(passk.pass_hat_k(Counts(n, c), k), max(product, Fraction(0)))

    def test_n_below_k_is_not_estimable(self):
        with self.assertRaises(passk.NotEstimable):
            passk.pass_hat_k(Counts(4, 4), 5)

    def test_headline_with_a_short_task_is_none_and_names_it(self):
        counts = {"a": Counts(5, 5), "short": Counts(3, 3)}
        value = passk.suite_metric(counts, lambda c: passk.pass_hat_k(c, 5), allow_not_estimable=False)
        self.assertIsNone(value.value)
        self.assertEqual(value.not_estimable, ("short",))

    def test_valid_score_averages_estimable_tasks_and_names_the_rest(self):
        counts = {"a": Counts(5, 5), "b": Counts(6, 0), "short": Counts(3, 3)}
        value = passk.suite_metric(counts, lambda c: passk.pass_hat_k(c, 5), allow_not_estimable=True)
        self.assertEqual(value.value, Fraction(1, 2))
        self.assertEqual(value.not_estimable, ("short",))
        self.assertEqual(value.tasks_counted, 2)

    def test_pass_at_1_and_pass_at_k(self):
        self.assertEqual(passk.pass_at_1(Counts(5, 2)), Fraction(2, 5))
        # pass@2 of 5 with 2 passes: 1 - C(3,2)/C(5,2) = 1 - 3/10
        self.assertEqual(passk.pass_at_k(Counts(5, 2), 2), Fraction(7, 10))

    def test_counts_are_checked(self):
        for n, c in [(3, 4), (-1, 0), (2, -1)]:
            with self.assertRaises(ValueError):
                Counts(n, c)
        with self.assertRaises(ValueError):
            passk.pass_hat_k(Counts(5, 3), 0)
        with self.assertRaises(ValueError):
            Counts(True, 0)


class IntervalTests(unittest.TestCase):
    def test_bootstrap_is_seeded_and_brackets_the_mean(self):
        values = [1, 0, 1, 1, 0, 1, 0, 1, 1, 1]
        a = passk.bootstrap_interval(values, resamples=2000, seed=3)
        b = passk.bootstrap_interval(values, resamples=2000, seed=3)
        self.assertEqual(a, b)
        self.assertLessEqual(a[0], 0.7)
        self.assertGreaterEqual(a[1], 0.7)
        self.assertGreaterEqual(a[0], 0.0)
        self.assertLessEqual(a[1], 1.0)

    def test_bootstrap_refuses_few_resamples_and_one_task(self):
        with self.assertRaises(ValueError):
            passk.bootstrap_interval([1, 0], resamples=999)
        with self.assertRaises(passk.NotEstimable):
            passk.bootstrap_interval([1], resamples=1000)

    def test_identical_tasks_give_a_zero_width_interval(self):
        self.assertEqual(passk.bootstrap_interval([0.5, 0.5, 0.5], resamples=1000), (0.5, 0.5))
        self.assertTrue(passk.clt_interval([0.5, 0.5, 0.5])["se_is_zero"])

    def test_clt_interval_raw_and_clipped(self):
        out = passk.clt_interval([1, 1, 1, 0])
        self.assertAlmostEqual(out["mean"], 0.75)
        self.assertGreater(out["raw"][1], 1.0)
        self.assertEqual(out["clipped"][1], 1.0)

    def test_paired_aligns_by_task_and_never_clips(self):
        a = {"t1": 1.0, "t2": 1.0, "t3": 1.0}
        b = {"t3": 0.0, "t1": 0.0, "t2": 0.0}
        out = passk.paired(a, b, resamples=1000, seed=1)
        self.assertEqual(out["mean_difference"], -1.0)
        self.assertEqual(out["bootstrap"], (-1.0, -1.0))
        self.assertEqual(out["per_task_difference"], {"t1": -1.0, "t2": -1.0, "t3": -1.0})

    def test_paired_draws_the_same_tasks_for_both_arms(self):
        # Equal arms on every task: a paired interval is exactly zero, whatever the tasks' spread.
        values = {f"t{i}": (i % 3) / 2 for i in range(12)}
        out = passk.paired(values, dict(values), resamples=1000, seed=5)
        self.assertEqual(out["bootstrap"], (0.0, 0.0))

    def test_paired_refuses_different_task_sets(self):
        with self.assertRaises(ValueError):
            passk.paired({"a": 1, "b": 0}, {"a": 1, "c": 0}, resamples=1000)

    def test_clustered_needs_two_clusters_and_is_seeded(self):
        values = {"a1": 1.0, "a2": 0.0, "b1": 1.0, "b2": 1.0, "c1": 0.0}
        clusters = {"a1": "A", "a2": "A", "b1": "B", "b2": "B", "c1": "C"}
        one = passk.clustered_bootstrap_interval(values, clusters, resamples=1000, seed=2)
        self.assertEqual(one, passk.clustered_bootstrap_interval(values, clusters, resamples=1000, seed=2))
        self.assertLessEqual(one[0], one[1])
        with self.assertRaises(passk.NotEstimable):
            passk.clustered_bootstrap_interval({"a": 1, "b": 0}, {"a": "A", "b": "A"}, resamples=1000)
        loo = passk.leave_one_cluster_out(values, clusters)
        self.assertEqual(loo["C"], 0.75)

    def test_tasks_for_difference(self):
        # ((1.95996 + 0.84162) * 0.3 / 0.1)^2 = 70.6 -> 71
        self.assertEqual(passk.tasks_for_difference(0.3), 71)
        self.assertEqual(passk.tasks_for_difference(0.3, design_effect=2.0), 142)


if __name__ == "__main__":
    unittest.main()
