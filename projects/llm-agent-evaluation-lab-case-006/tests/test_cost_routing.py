"""Cost identity, tau routing, gamma ties, and negative marginal gain."""

from __future__ import annotations

import random
import unittest

from helpers import card
from tide_route.cost import completion_cost, estimate_cost
from tide_route.routing import next_model, ordered_by_cost, pruned_supermodels, tau


class CostTests(unittest.TestCase):
    def test_three_part_identity(self):
        priced = card(per_request=0.125, input_per_token=0.5, output_per_token=0.25)
        self.assertEqual(completion_cost(priced, 2, 4), 0.25 * 4 + 0.5 * 2 + 0.125)

    def test_estimate_uses_expected_output(self):
        priced = card(per_request=0.0, input_per_token=0.0, output_per_token=0.5)
        self.assertEqual(estimate_cost(priced, 3, 4), 2.0)

    def test_long_output_reorders_price_cards(self):
        input_priced = card(input_per_token=0.01, output_per_token=0.001)
        output_priced = card(input_per_token=0.0001, output_per_token=0.02)
        short = {
            "input_priced": estimate_cost(input_priced, 100, 10),
            "output_priced": estimate_cost(output_priced, 100, 10),
        }
        long = {
            "input_priced": estimate_cost(input_priced, 100, 10000),
            "output_priced": estimate_cost(output_priced, 100, 10000),
        }
        qualities = {"input_priced": 0.5, "output_priced": 0.5}
        rng = random.Random(0)
        short_choice = next_model(
            ["input_priced", "output_priced"], qualities, short, set(), 1.0, 0.0, rng
        )
        long_choice = next_model(
            ["input_priced", "output_priced"], qualities, long, set(), 1.0, 0.0, rng
        )
        frozen = ordered_by_cost(["input_priced", "output_priced"], short)
        self.assertEqual(short_choice, "output_priced")
        self.assertEqual(long_choice, "input_priced")
        self.assertEqual(frozen[0], "output_priced")
        self.assertNotEqual(frozen[0], long_choice)


class RoutingTests(unittest.TestCase):
    def test_published_two_model_tau(self):
        self.assertAlmostEqual(tau(0.5, 0.5, 0.1), 0.45)
        self.assertAlmostEqual(tau(0.8, 1.0, 0.1), 0.7)
        self.assertAlmostEqual(tau(0.5, 0.5, 1.0), 0.0)
        self.assertAlmostEqual(tau(0.8, 1.0, 1.0), -0.2)

    def test_dekoninck_two_model_example(self):
        ids = ["cheap", "expensive"]
        costs = {"cheap": 0.5, "expensive": 1.0}
        qualities = {"cheap": 0.5, "expensive": 0.8}
        rng = random.Random(0)
        self.assertEqual(
            next_model(ids, qualities, costs, set(), 0.1, 0.0, rng),
            "expensive",
        )
        self.assertEqual(
            next_model(ids, qualities, costs, set(), 1.0, 0.0, rng),
            "cheap",
        )
        revised = {"cheap": 0.5, "expensive": 0.1}
        self.assertEqual(
            next_model(ids, revised, costs, {"expensive"}, 0.1, 0.0, rng),
            "cheap",
        )

    def test_gamma_tie_frequency(self):
        ids = ["cheap", "pricey"]
        costs = {"cheap": 1.0, "pricey": 2.0}
        qualities = {"cheap": 0.6, "pricey": 0.8}
        rng = random.Random(7)
        hits = 0
        draws = 4000
        for _ in range(draws):
            chosen = next_model(ids, qualities, costs, set(), 0.2, 0.25, rng)
            if chosen == "cheap":
                hits += 1
        self.assertAlmostEqual(hits / draws, 0.25, delta=0.04)

    def test_negative_marginal_gain_prunes_supersets(self):
        ids = ["A", "P", "C"]
        costs = {"A": 1.0, "P": 10.0, "C": 1.0}
        qualities = {"A": 0.9, "P": 0.91, "C": 0.5}
        pruned = pruned_supermodels(ids, qualities, costs, 0.1, set())
        self.assertIn(("A", "P"), pruned)
        self.assertIn(("A", "C", "P"), pruned)
        chosen = next_model(ids, qualities, costs, set(), 0.1, 0.0, random.Random(0))
        self.assertNotEqual(chosen, "P")


if __name__ == "__main__":
    unittest.main()
