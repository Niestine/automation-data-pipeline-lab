"""CheckList cells, prompt bundles, and the release gate."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401  — puts src/ on sys.path

from curbgate.gate import SAFETY_AXES, decide, validate_report
from curbgate.models import NIST_RISK_TAGS
from curbgate.scoring import (
    SplitError,
    assert_held_out,
    bundle_metrics,
    cell_failure_rates,
    checklist_failure,
    expand_negation,
    negative_template_fraction,
    rank_systems,
)
from curbgate.stats import compare_scores


def _green(**overrides):
    headline = {
        "available": True,
        "underpowered": False,
        "side": "above",
        "label": "significant_improvement",
        "baseline_mean": 0.4,
        "candidate_mean": 0.9,
    }
    safety = {name: (1.0, 1.0) for name in SAFETY_AXES}
    tag_cells = {
        "confabulation": {"available": True, "baseline_mean": 0.4, "candidate_mean": 0.9},
        "information security": {"available": True, "baseline_mean": 1.0, "candidate_mean": 1.0},
    }
    raw = dict(
        manifest_ok=True,
        pin_reasons=[],
        different_experiment=False,
        change_kind="improvement",
        headline=headline,
        safety=safety,
        format_means={"reasoning": 1.0, "classification": 1.0},
        format_baselines={"reasoning": 1.0, "classification": 1.0},
        format_floors={"reasoning": 0.5, "classification": 0.5},
        claimed_tags=["confabulation", "information security"],
        measured_tags=["confabulation", "information security"],
        tag_cells=tag_cells,
        harness_error_rate=0.0,
        harness_budget=0.0,
        key_order_clean=True,
        coverage_missing=[],
    )
    raw.update(overrides)
    return raw


class ChecklistTests(unittest.TestCase):
    def test_negation_expansion_keeps_the_template_id(self) -> None:
        rows = expand_negation(
            [
                {"name": "resident-17", "clause": "is clear", "label": "issue"},
                {"name": "resident-18", "clause": "is not clear", "label": "refuse"},
            ]
        )
        self.assertTrue(rows)
        self.assertTrue(all(row["template_id"] == "negation.v1" for row in rows))
        self.assertEqual(checklist_failure("mft", {"label": "refuse"}, {"label": "refuse"}), 0)
        self.assertEqual(checklist_failure("mft", {"label": "refuse"}, {"label": "issue"}), 1)

    def test_invariance_and_directional_failures(self) -> None:
        inv_same = checklist_failure("inv", {}, {"label": "issue", "other_label": "issue"})
        inv_flip = checklist_failure("inv", {}, {"label": "issue", "other_label": "refuse"})
        held = checklist_failure("dir", {"direction": "down"}, {"score_before": 0.8, "score_after": 0.2})
        rose = checklist_failure("dir", {"direction": "down"}, {"score_before": 0.2, "score_after": 0.9})
        rates = cell_failure_rates(
            [
                {"capability": "named_entity", "test_type": "inv", "failed": inv_same},
                {"capability": "named_entity", "test_type": "inv", "failed": inv_flip},
                {"capability": "tone", "test_type": "dir", "failed": held},
                {"capability": "tone", "test_type": "dir", "failed": rose},
            ]
        )
        self.assertEqual(rates["named_entity.inv"]["failures"], 1)
        self.assertEqual(rates["tone.dir"]["failures"], 1)
        self.assertAlmostEqual(rates["named_entity.inv"]["failure_rate"], 0.5)

    def test_rising_fail_count_with_a_covering_interval_stays_inconclusive(self) -> None:
        baseline = []
        candidate = []
        clusters = []
        for cluster in range(10):
            fail = 1.0 if cluster < 2 else 0.0
            baseline.extend([0.0, 0.0])
            candidate.extend([fail, fail])
            clusters.extend([f"template-{cluster}", f"template-{cluster}"])
        result = compare_scores(baseline, candidate, clusters, delta=0.5)
        self.assertGreater(sum(candidate), sum(baseline))
        self.assertEqual(result["side"], "covers")
        self.assertEqual(result["label"], "inconclusive")


class BundleTests(unittest.TestCase):
    def test_avgp_and_maxp_rankings_can_disagree(self) -> None:
        candidate = bundle_metrics({"A": 1.0, "B": 0.0})
        baseline = bundle_metrics({"A": 0.6, "B": 0.6})
        systems = {"candidate": candidate, "baseline": baseline}
        self.assertNotEqual(rank_systems(systems, "AvgP"), rank_systems(systems, "MaxP"))
        self.assertAlmostEqual(candidate["Sat"], 0.5)
        self.assertAlmostEqual(candidate["CPS"], candidate["Sat"] * candidate["MaxP"])
        per_template = {
            "A": {"side": "above"},
            "B": {"side": "below"},
        }
        self.assertAlmostEqual(negative_template_fraction(per_template), 0.5)

    def test_held_out_selection_cannot_reuse_the_selection_slice(self) -> None:
        assert_held_out(["a-select"], ["a-confirm"])
        with self.assertRaises(SplitError):
            assert_held_out(["a-select", "b-select"], ["b-select"])


class GateTests(unittest.TestCase):
    def test_green_vector_promotes(self) -> None:
        decision = decide(**_green())
        self.assertTrue(decision["promoted"])
        self.assertEqual(decision["reasons"], [])

    def test_security_drop_blocks_a_task_gain(self) -> None:
        raw = _green()
        raw["safety"]["secret_absent"] = (1.0, 0.0)
        raw["tag_cells"]["information security"] = {"available": True, "baseline_mean": 1.0, "candidate_mean": 0.5}
        decision = decide(**raw)
        self.assertFalse(decision["promoted"])
        self.assertIn("secret_absent_fell", decision["reasons"])
        self.assertIn("risk_tag_regressed:information security", decision["reasons"])

    def test_harness_budget_and_missing_tag(self) -> None:
        harness = decide(**_green(harness_error_rate=0.2, harness_budget=0.0))
        self.assertFalse(harness["promoted"])
        self.assertIn("harness_budget_exceeded", harness["reasons"])
        missing = decide(**_green(claimed_tags=["confabulation", "data privacy"], measured_tags=["confabulation"]))
        self.assertIn("missing_claimed_tag", missing["reasons"])
        self.assertIn("data privacy", missing["missing_tags"])

    def test_underpowered_headline_is_not_a_significance_label(self) -> None:
        raw = _green()
        raw["headline"] = {"available": True, "underpowered": True, "side": "above", "label": "underpowered"}
        decision = decide(**raw)
        self.assertFalse(decision["promoted"])
        self.assertIn("underpowered", decision["reasons"])
        self.assertNotIn("significant_improvement", decision["reasons"])

    def test_format_floor_blocks_perfect_parses_with_wrong_answers(self) -> None:
        decision = decide(**_green(format_means={"reasoning": 0.0, "classification": 1.0}))
        self.assertIn("reasoning_below_floor", decision["reasons"])
        self.assertFalse(decision["promoted"])

    def test_report_must_list_untested_tags_and_the_power_check(self) -> None:
        report = {
            "claimed_risk_tags": ["confabulation"],
            "risk_vector": {"confabulation": {"available": True}},
            "gaps": [tag for tag in NIST_RISK_TAGS if tag != "confabulation"],
            "power": {"underpowered": False},
        }
        validate_report(report)
        broken = dict(report)
        broken["gaps"] = []
        with self.assertRaises(ValueError):
            validate_report(broken)

    def test_temperature_experiment_does_not_promote(self) -> None:
        decision = decide(**_green(different_experiment=True))
        self.assertFalse(decision["promoted"])
        self.assertIn("temperature_different_experiment", decision["reasons"])


if __name__ == "__main__":
    unittest.main()
