"""Paired intervals, clustering, resampling, power, and pass^k."""

from __future__ import annotations

import math
import unittest

from helpers import manifest

from curbgate.stats import (
    aggregate_key,
    clustered_paired_standard_error,
    compare_scores,
    item_mean_stderr,
    item_means,
    majority,
    mean_pass_hat_k,
    minimum_detectable_effect,
    pass_at_k,
    pass_hat_k,
    power_check,
    probability_summary,
    required_n,
    resolve_headline,
    sample_variance,
    significance_label,
    stderr_of_mean,
    student_t_cdf,
    student_t_quantile,
)


class StatsTests(unittest.TestCase):
    def test_student_t_matches_table(self) -> None:
        self.assertAlmostEqual(student_t_quantile(1, 0.975), 12.7062047364, places=6)
        self.assertAlmostEqual(student_t_quantile(19, 0.975), 2.0930240544, places=6)
        self.assertAlmostEqual(student_t_cdf(student_t_quantile(9, 0.975), 9), 0.975, places=6)

    def test_paired_interval_matches_formula(self) -> None:
        baseline = [0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0]
        candidate = [1, 1, 0, 1, 1, 1, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 1, 0, 0, 1]
        diffs = [c - b for b, c in zip(baseline, candidate)]
        n = len(diffs)
        mean = sum(diffs) / n
        se = math.sqrt(sample_variance(diffs) / n)
        result = compare_scores(baseline, candidate, delta=0.5)
        self.assertEqual(result["n"], 20)
        self.assertAlmostEqual(result["mean_diff"], mean, places=12)
        self.assertAlmostEqual(result["se"], se, places=12)
        self.assertFalse(result["clustered"])
        self.assertIn("correlation", result)
        self.assertEqual(result["fail_count"], sum(1 for diff in diffs if diff < 0))

    def test_higher_mean_covering_zero_is_inconclusive(self) -> None:
        baseline = [0] * 20
        candidate = [1, 1] + [0] * 18
        result = compare_scores(baseline, candidate, delta=0.5)
        self.assertGreater(result["mean_diff"], 0)
        self.assertEqual(result["side"], "covers")
        self.assertFalse(result["underpowered"])
        self.assertEqual(result["label"], "inconclusive")
        self.assertNotIn("significant", result["label"])

    def test_cluster_inflation(self) -> None:
        diffs = []
        clusters = []
        for cluster in range(10):
            value = 1.0 if cluster < 5 else 0.0
            diffs.extend([value, value])
            clusters.extend([f"passage-{cluster}", f"passage-{cluster}"])
        clustered = clustered_paired_standard_error(diffs, clusters)
        naive = math.sqrt(sample_variance(diffs) / len(diffs))
        self.assertGreater(clustered, naive)
        pooled_as_independent = stderr_of_mean(diffs)
        self.assertGreater(clustered, pooled_as_independent)
        result = compare_scores([0] * 20, diffs, clusters, delta=0.5)
        self.assertTrue(result["clustered"])
        self.assertEqual(result["cluster_count"], 10)
        self.assertAlmostEqual(result["se"], clustered, places=12)
        self.assertGreater(result["se"], result["naive_se"])

    def test_resample_uses_item_means_not_epoch_rows(self) -> None:
        rows = [[1, 1, 1, 1], [0, 0, 0, 0], [1, 1, 1, 1], [1, 1, 1, 1], [0, 0, 0, 0]]
        means = item_means(rows)
        expected = stderr_of_mean(means)
        self.assertAlmostEqual(item_mean_stderr(rows), expected, places=12)
        pooled = [value for row in rows for value in row]
        pooled_se = stderr_of_mean(pooled)
        self.assertNotAlmostEqual(item_mean_stderr(rows), pooled_se, places=6)
        self.assertGreater(expected, pooled_se)

    def test_miller_sample_size_and_power_gate(self) -> None:
        n_star = required_n(1 / 9, 0, 0, 1, 1, 0.03)
        self.assertAlmostEqual(n_star, 969, delta=0.01)
        small = power_check(30, 0.03, 1 / 9)
        self.assertTrue(small["underpowered"])
        self.assertEqual(significance_label("above", True), "underpowered")
        self.assertNotIn("significant", significance_label("above", True))
        large_n = math.ceil(float(small["required_n"]))
        large = power_check(large_n, 0.03, 1 / 9)
        self.assertFalse(large["underpowered"])
        self.assertEqual(significance_label("above", False), "significant_improvement")
        looser = required_n(1 / 9, 0, 0, 1, 1, 0.03, alpha=0.05, power=0.80)
        stricter = required_n(1 / 9, 0, 0, 1, 1, 0.03, alpha=0.01, power=0.90)
        self.assertGreater(stricter, looser)
        pinned = manifest(alpha=0.01, power=0.9)
        from_manifest = required_n(1 / 9, 0, 0, 1, 1, 0.03, alpha=pinned.alpha, power=pinned.power)
        self.assertAlmostEqual(from_manifest, stricter, places=6)
        mde = minimum_detectable_effect(30, 1 / 9, 0, 0, 1, 1)
        self.assertGreater(mde, 0.03)

    def test_pass_k_headline_is_not_pass_at(self) -> None:
        self.assertEqual(pass_hat_k(8, 8, 8), 1.0)
        self.assertEqual(pass_hat_k(4, 8, 8), 0.0)
        self.assertAlmostEqual(mean_pass_hat_k([(8, 8), (4, 8)], 8), 0.5)
        self.assertEqual(pass_at_k(4, 8, 8), 1.0)
        name, value = resolve_headline([("stderr", 0.2), ("pass_at", 1.0), ("pass_k", 0.5)], "pass_k")
        self.assertEqual(name, "pass_k")
        self.assertEqual(value, 0.5)

    def test_headline_ignores_a_leading_stderr(self) -> None:
        name, value = resolve_headline([("stderr", 0.2), ("task_correct", 0.75)], "task_correct")
        self.assertEqual((name, value), ("task_correct", 0.75))

    def test_aggregate_skips_nan_and_does_not_zero_fill(self) -> None:
        rows = [{"task_correct": 1.0}, {"task_correct": None}, {"task_correct": float("nan")}, {"task_correct": 1.0}]
        self.assertEqual(aggregate_key(rows, "task_correct", on_missing="skip"), 1.0)
        # None becomes 0; NaN stays unscored, so the denominator is three.
        self.assertAlmostEqual(aggregate_key(rows, "task_correct", on_missing="zero"), 2.0 / 3.0)
        self.assertIsNone(majority([1, 0, 1, 0]))
        self.assertEqual(majority([1, 1, 0]), 1.0)

    def test_probability_axis_summary(self) -> None:
        probs = [0.2, 0.4, 0.6, 0.8]
        summary = probability_summary(probs)
        self.assertAlmostEqual(summary["mean"], 0.5)
        self.assertAlmostEqual(summary["se"], stderr_of_mean(probs))


if __name__ == "__main__":
    unittest.main()
