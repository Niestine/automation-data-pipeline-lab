"""AIQ, baselines, judge noise, and the validation-split fitter."""

from __future__ import annotations

import random
import unittest

from helpers import EXAMPLES, make_router, model, request
from tide_route.evaluate import (
    aiq,
    benchmark,
    mix_expectation,
    nondecreasing_convex_hull,
    oracle_point,
    simulate_mix,
)
from tide_route.fit import fit_threshold, prune_agreeing
from tide_route.load import read_json
from tide_route.provider import ScriptStep


class MetricTests(unittest.TestCase):
    def test_aiq_of_the_unit_segment_is_one_half(self):
        self.assertAlmostEqual(aiq([(0.0, 0.0), (1.0, 1.0)], 0.0, 1.0), 0.5, places=6)

    def test_extra_spend_without_quality_leaves_the_hull(self):
        hull = nondecreasing_convex_hull([(0.0, 0.5), (0.5, 0.5), (1.0, 0.5)])
        self.assertEqual(hull, [(0.0, 0.5)])

    def test_mix_expectation_is_the_midpoint(self):
        cost, quality = mix_expectation((0.0, 0.0), (2.0, 1.0), 0.5)
        self.assertEqual((cost, quality), (1.0, 0.5))
        sampled_cost, sampled_quality = simulate_mix(
            random.Random(11), (0.0, 0.0), (2.0, 1.0), 0.5, 4000
        )
        self.assertAlmostEqual(sampled_cost, 1.0, delta=0.08)
        self.assertAlmostEqual(sampled_quality, 0.5, delta=0.04)

    def test_oracle_on_the_three_model_fixture(self):
        fixture = read_json(EXAMPLES / "eval_fixture.json")
        costs = {row["model_id"]: row["per_request"] for row in fixture["models"]}
        cost, quality = oracle_point(fixture["queries"], costs)
        self.assertAlmostEqual(cost, (0.25 + 0.5 + 1.0) / 3, places=6)
        self.assertEqual(quality, 1.0)

    def test_oracle_and_benchmark_price_token_cards(self):
        fixture = read_json(EXAMPLES / "eval_fixture.json")
        for row in fixture["models"]:
            row["input_per_token"] = 0.01
        report = benchmark(fixture, seed=1)
        # Each query has 8 input tokens, so every card costs 0.08 more.
        self.assertAlmostEqual(report["oracle_cost"], (0.25 + 0.5 + 1.0) / 3 + 0.08, places=6)
        self.assertTrue(all(row["within_budget"] for row in report["epsilon_rows"]))


class BenchmarkTests(unittest.TestCase):
    def test_product_beats_zero_and_respects_caps(self):
        fixture = read_json(EXAMPLES / "eval_fixture.json")
        report = benchmark(fixture, seed=1)
        routes = {row["query_id"]: row for row in report["routes"]}
        self.assertEqual(routes["Q1"]["model_id"], "skiff")
        self.assertEqual(routes["Q2"]["model_id"], "yawl")
        self.assertEqual(routes["Q3"]["model_id"], "barque")
        self.assertGreater(report["product_aiq"], report["zero_aiq"])
        self.assertAlmostEqual(report["oracle_quality"], 1.0)
        ladder_cost, ladder_quality = report["ladder_point"]
        product_cost, product_quality = report["product_points"][0]
        self.assertGreater(ladder_cost, product_cost)
        self.assertAlmostEqual(ladder_quality, 1.0)
        self.assertAlmostEqual(product_quality, 1.0)
        self.assertLessEqual(product_quality, report["oracle_quality"])
        for point in report["single_points"]:
            self.assertLessEqual(point[1], report["oracle_quality"] + 1e-9)
        zero_noise = report["epsilon_rows"][0]
        self.assertEqual(zero_noise["epsilon"], 0.0)
        self.assertGreater(zero_noise["product_aiq"], zero_noise["zero_aiq"])
        noisy = report["epsilon_rows"][-1]
        self.assertEqual(noisy["epsilon"], 0.4)
        self.assertTrue(noisy["within_budget"])
        self.assertTrue(noisy["within_caps"])
        self.assertLessEqual(noisy["max_quality"], report["oracle_quality"] + 1e-9)
        if report["crossover_epsilon"] is not None:
            self.assertIn(report["crossover_epsilon"], (0.1, 0.2, 0.4))

    def test_noise_axes(self):
        cheap = model("cobble", per_request=0.25, ex_ante={"Q": 0.2})
        rich = model("freighter", per_request=1.0, ex_ante={"Q": 0.9})
        scores = {("Q", "cobble"): 0.2, ("Q", "freighter"): 0.9}
        clean = make_router([cheap, rich], scores=scores, lambda_=0.1, threshold=0.5)
        clean_shot = clean.route(request(policy="one_shot"))
        self.assertEqual(clean_shot.models_called, ["freighter"])

        flipped = [
            model("cobble", per_request=0.25, ex_ante={"Q": 0.8}),
            model("freighter", per_request=1.0, ex_ante={"Q": 0.1}),
        ]
        noisy_ante = make_router(flipped, scores=scores, lambda_=0.1, threshold=0.5)
        ante = noisy_ante.route(request(policy="one_shot"))
        self.assertEqual(ante.models_called, ["cobble"])

        ladder = make_router([cheap, rich], scores=scores, lambda_=0.1, threshold=0.5)
        clean_ladder = ladder.route(request(policy="threshold_cascade"))
        self.assertEqual(clean_ladder.models_called, ["cobble", "freighter"])

        post = make_router(
            [cheap, rich],
            scores=scores,
            lambda_=0.1,
            threshold=0.5,
            epsilon=1.0,
        )
        post_ladder = post.route(request(policy="threshold_cascade"))
        self.assertEqual(post_ladder.models_called, ["cobble"])
        post_shot = make_router(
            [cheap, rich],
            scores=scores,
            lambda_=0.1,
            threshold=0.5,
            epsilon=1.0,
        )
        self.assertEqual(
            post_shot.route(request(policy="one_shot")).models_called,
            ["freighter"],
        )

        same = make_router(
            flipped,
            scores=scores,
            lambda_=0.1,
            threshold=0.5,
        )
        self.assertEqual(
            same.route(request(policy="threshold_cascade")).models_called,
            ["cobble", "freighter"],
        )


class FitTests(unittest.TestCase):
    def test_agreeing_models_are_pruned(self):
        validation = read_json(EXAMPLES / "validation_split.json")
        kept = prune_agreeing(validation["answers"], validation["costs"])
        self.assertEqual(kept, ["skiff", "barque"])
        self.assertNotIn("yawl", kept)

    def test_threshold_grid_respects_budget(self):
        records = [
            {
                "steps": [
                    {"cost": 0.2, "score": 0.2, "correct": False},
                    {"cost": 1.0, "score": 0.9, "correct": True},
                ]
            }
        ]
        fitted = fit_threshold(records, [0.1, 0.5], budget=0.5)
        self.assertIsNotNone(fitted)
        self.assertEqual(fitted["threshold"], 0.1)
        self.assertLessEqual(fitted["mean_cost"], 0.5)
        blocked = fit_threshold(records, [0.5], budget=0.5)
        self.assertIsNone(blocked)


class ScriptTests(unittest.TestCase):
    def test_validation_and_fault_shape_stay_local(self):
        self.assertIn("answers", read_json(EXAMPLES / "validation_split.json"))
        router = make_router(
            [model("skiff", provider="openai")],
            scripts={"skiff": [ScriptStep(status=400, error_code="service_tier")]},
        )
        outcome = router.route(request())
        self.assertEqual(outcome.problem["failure_class"], "request_defect")


if __name__ == "__main__":
    unittest.main()
