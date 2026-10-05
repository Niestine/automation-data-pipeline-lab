"""Shared adaptive budget: EMA, OVERLOADED, exhaustion, and no second retry."""

from __future__ import annotations

import unittest

import helpers
from bay_notice.budget import ReplayRandom, SharedBudget
from bay_notice.errors import BudgetExhausted, OverloadedSignal, TransientGateError
from bay_notice.inject import Step


class BudgetTests(unittest.TestCase):
    def test_ema_moves_one_tenth_toward_the_new_outcome(self) -> None:
        budget = SharedBudget(base_load=10, rng=ReplayRandom([0.0]))
        budget.observe(True)
        self.assertAlmostEqual(budget.failure_rate, 0.1)
        budget.observe(False)
        self.assertAlmostEqual(budget.failure_rate, 0.09)

    def test_draw_below_probability_admits_and_draw_above_refuses(self) -> None:
        admit = SharedBudget(base_load=10, rng=ReplayRandom([0.19]))
        admit.observe(True)
        self.assertAlmostEqual(admit.admission_probability(), 0.2)
        self.assertTrue(admit.try_admit())
        self.assertEqual(admit.admissions, 1)

        refuse = SharedBudget(base_load=10, rng=ReplayRandom([0.2]))
        refuse.observe(True)
        self.assertFalse(refuse.try_admit())
        self.assertEqual(refuse.admissions, 0)
        self.assertEqual(refuse.budget, 0.2)

    def test_overloaded_lowers_admission_and_then_success_restores_it(self) -> None:
        budget = SharedBudget(base_load=10, rng=ReplayRandom([0.0]))
        probabilities = []
        for _ in range(5):
            budget.observe(True)
            budget.note_status("OVERLOADED")
            budget.tick()
            probabilities.append(budget.admission_probability())
            self.assertFalse(budget.try_admit())
        self.assertTrue(
            all(probabilities[index] > probabilities[index + 1] for index in range(4))
        )
        budget.note_status("OK")
        for _ in range(40):
            budget.observe(False)
            budget.tick()
        self.assertLess(budget.failure_rate, budget.theta_low)
        self.assertGreater(budget.budget, 0)
        self.assertTrue(budget.try_admit())

    def test_high_failure_rate_tightens_without_an_overloaded_signal(self) -> None:
        budget = SharedBudget(base_load=10, rng=ReplayRandom([0.0]))
        budget.failure_rate = 0.4
        budget.note_status("OK")
        before = budget.budget
        budget.tick()
        self.assertAlmostEqual(budget.budget, before * 0.5)

    def test_exhausted_budget_refuses_and_the_desk_does_not_retry_it(self) -> None:
        budget = SharedBudget(base_load=8, rng=ReplayRandom([0.0, 0.0, 0.0, 0.0]))
        budget.budget = 0
        desk = helpers.make_desk(
            steps=[
                Step(kind="error", error_name="transient"),
                Step(kind="success", body="HOLD C-14 40min"),
            ],
            budget=budget,
        )
        with self.assertRaises(BudgetExhausted):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 1)
        self.assertEqual(desk.decisions, ["budget_refuse"])

    def test_a_retry_the_cap_forbids_does_not_spend_shared_budget(self) -> None:
        budget = SharedBudget(base_load=10, rng=ReplayRandom([0.0, 0.0]))
        desk = helpers.make_desk(steps=helpers.errors(2), max_retries=0, budget=budget)
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 1)
        self.assertEqual(desk.decisions, ["stop"])
        self.assertEqual(budget.admissions, 0)
        self.assertEqual(budget.budget, budget.initial)

    def test_desk_successes_bring_the_shared_failure_rate_down(self) -> None:
        budget = SharedBudget(base_load=10, rng=ReplayRandom([]))
        budget.failure_rate = 0.5
        desk = helpers.make_desk(steps=[], budget=budget)
        for number in range(1001, 1004):
            desk.run(helpers.sample_job(f"BAY-{number}"))
        self.assertAlmostEqual(budget.failure_rate, 0.5 * 0.9**3)
        self.assertEqual(len(desk.ledger.entries), 3)

    def test_overloaded_step_is_not_a_tight_loop(self) -> None:
        budget = SharedBudget(base_load=8, rng=ReplayRandom([0.0]))
        desk = helpers.make_desk(
            steps=[Step(kind="overloaded"), Step(kind="overloaded")],
            budget=budget,
        )
        with self.assertRaises(OverloadedSignal):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 1)
        self.assertEqual(desk.clock.waits, [])
        self.assertEqual(desk.decisions, ["budget_refuse"])
        self.assertLess(budget.budget, budget.initial)


if __name__ == "__main__":
    unittest.main()
